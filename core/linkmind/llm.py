"""Structured LLM calls behind one function, for two provider styles:

- openai_compat (default): any OpenAI-compatible Chat Completions endpoint, e.g. the Vectron
  gateway serving DeepSeek / Kimi / GLM / MiniMax. Uses strict json_schema response_format.
- anthropic: Claude via the Anthropic SDK, with server-side refusal fallback.

Callers pass provider-neutral content blocks:
    {"type": "text", "text": "..."}
    {"type": "image", "media_type": "image/png", "data": "<base64>"}
"""

import time
from functools import lru_cache
from typing import TypeVar

from pydantic import BaseModel, ValidationError

from .config import settings

T = TypeVar("T", bound=BaseModel)

ANTHROPIC_FALLBACK_BETA = "server-side-fallback-2026-07-01"


class LLMError(RuntimeError):
    pass


@lru_cache
def _openai_client():
    from openai import OpenAI

    s = settings()
    if not s.linkmind_api_key:
        raise LLMError("no API key: set VECTRON_API_KEY (or LINKMIND_API_KEY) in ~/.openclaw/.env")
    return OpenAI(base_url=s.linkmind_base_url, api_key=s.linkmind_api_key, timeout=120, max_retries=2)


@lru_cache
def _anthropic_client():
    import anthropic

    key = settings().anthropic_api_key
    return anthropic.Anthropic(api_key=key) if key else anthropic.Anthropic()


def _to_openai(block: dict) -> dict:
    if block["type"] == "image":
        return {"type": "image_url", "image_url": {"url": f"data:{block['media_type']};base64,{block['data']}"}}
    return {"type": "text", "text": block["text"]}


def _to_anthropic(block: dict) -> dict:
    if block["type"] == "image":
        return {"type": "image", "source": {"type": "base64", "media_type": block["media_type"], "data": block["data"]}}
    return {"type": "text", "text": block["text"]}


def _parse_openai(model: str, system: str, content: list[dict], output: type[T], max_tokens: int):
    resp = _openai_client().chat.completions.parse(
        model=model,
        max_tokens=max_tokens,
        response_format=output,
        messages=[
            {"role": "system", "content": system},
            {"role": "user", "content": [_to_openai(b) for b in content]},
        ],
    )
    choice = resp.choices[0]
    meta = {
        "request_id": resp.id,
        "stop_reason": choice.finish_reason,
        "input_tokens": getattr(resp.usage, "prompt_tokens", None),
        "output_tokens": getattr(resp.usage, "completion_tokens", None),
    }
    if choice.finish_reason == "length":
        raise LLMError("output hit max_tokens; transcript would be truncated")
    if choice.message.refusal:
        raise LLMError(f"model refused: {choice.message.refusal}")
    parsed = choice.message.parsed
    if parsed is None:
        # Some gateways return valid JSON without the SDK attaching .parsed; validate it ourselves.
        try:
            parsed = output.model_validate_json(choice.message.content or "")
        except ValidationError as e:
            raise LLMError(f"output did not match schema: {e.errors()[:3]}") from e
    return parsed, meta


def _parse_anthropic(model: str, system: str, content: list[dict], output: type[T], max_tokens: int):
    resp = _anthropic_client().beta.messages.parse(
        model=model,
        max_tokens=max_tokens,
        betas=[ANTHROPIC_FALLBACK_BETA],
        fallbacks="default",
        output_config={"effort": settings().linkmind_effort},
        output_format=output,
        system=system,
        messages=[{"role": "user", "content": [_to_anthropic(b) for b in content]}],
    )
    meta = {
        "request_id": resp._request_id,
        "stop_reason": resp.stop_reason,
        "input_tokens": resp.usage.input_tokens,
        "output_tokens": resp.usage.output_tokens,
    }
    if resp.stop_reason == "refusal":
        raise LLMError(f"model declined the request: {getattr(resp.stop_details, 'category', None)}")
    if resp.stop_reason == "max_tokens":
        raise LLMError("output hit max_tokens; transcript would be truncated")
    if resp.parsed_output is None:
        raise LLMError(f"no parsed output (stop_reason={resp.stop_reason})")
    return resp.parsed_output, meta


def parse(
    *, system: str, content: list[dict], output: type[T], max_tokens: int = 16000, model: str | None = None
) -> tuple[T, dict]:
    """One structured call. Returns (parsed object, call metadata for logging)."""
    s = settings()
    model = model or s.linkmind_model
    started = time.monotonic()
    fn = _parse_anthropic if s.linkmind_provider == "anthropic" else _parse_openai
    parsed, meta = fn(model, system, content, output, max_tokens)
    meta.update(provider=s.linkmind_provider, model=model, latency_ms=int((time.monotonic() - started) * 1000))
    return parsed, meta
