import os
from functools import lru_cache
from pathlib import Path
from typing import Literal

from pydantic import AliasChoices, Field
from pydantic_settings import BaseSettings, SettingsConfigDict

PROJECT_ROOT = Path(__file__).resolve().parents[2]
OPENCLAW_HOME = Path.home() / ".openclaw"
# Runtime data (database, raw chat material, logs) lives outside the repo: ~/Documents is iCloud-synced,
# which can corrupt a live database and would upload other people's chats to iCloud.
LINKMIND_HOME = Path(os.environ.get("LINKMIND_HOME", Path.home() / ".linkmind")).expanduser()
# The login service runs an installed copy of Core from here and never reads the repo (launchd jobs may not
# open ~/Documents); `scripts/dev.sh up` refreshes it, including a copy of the project .env.
APP_DIR = LINKMIND_HOME / "app"


class Settings(BaseSettings):
    # ~/.openclaw/.env holds the shared API key; later files win: the service's copy of the project .env,
    # then the project .env itself when running from the repo (tests, scripts).
    model_config = SettingsConfigDict(
        env_file=(OPENCLAW_HOME / ".env", APP_DIR / ".env", PROJECT_ROOT / ".env"),
        env_file_encoding="utf-8",
        extra="ignore",
    )

    # LLM: OpenAI-compatible gateway by default (same endpoint and key OpenClaw uses).
    linkmind_provider: Literal["openai_compat", "anthropic"] = "openai_compat"
    linkmind_base_url: str = "https://api.vectron.meta-stone.com/v1"
    linkmind_api_key: str | None = Field(default=None, validation_alias=AliasChoices("LINKMIND_API_KEY", "VECTRON_API_KEY"))
    linkmind_model: str = "DeepSeek/DeepSeek-V4.1-Flash"  # background extraction: quality over speed
    # Screenshot reading, which the user waits for. Empty = linkmind_model. 2026-11-09 on the Vectron gateway:
    # DeepSeek-V4-Flash read two real screenshots identically to V4.1-Flash in 10-11.5 s instead of 14-15.6 s.
    linkmind_vision_model: str = ""
    # Only used when linkmind_provider == "anthropic".
    anthropic_api_key: str | None = None
    linkmind_effort: str = "medium"

    linkmind_database_url: str = "postgresql://127.0.0.1:5544/linkmind"
    linkmind_mcp_host: str = "127.0.0.1"
    linkmind_mcp_port: int = 8765
    linkmind_sync_budget_s: float = 45.0
    # The only hosts Core will fetch shared links from (exact match, comma-separated).
    linkmind_fetch_hosts: str = "mp.weixin.qq.com"
    # How the account owner appears in WeChat: nickname and any group nicknames, comma-separated. In views
    # without left/right bubbles (merged chat-record pages, text copied on Mac) names are the only cue for "me".
    linkmind_owner_names: str = ""
    # A screenshot of the same chat arriving within this many seconds of the previous one joins its source.
    linkmind_batch_window_s: float = 180.0
    # Push newly found connections (one person's need another can meet) to the owner in WeChat.
    linkmind_push_connections: bool = True
    # Media paths handed over by the agent must live under one of these roots.
    linkmind_media_roots: str = f"{OPENCLAW_HOME},/tmp,/private/tmp,/var/folders,/private/var/folders,{LINKMIND_HOME}"
    # One Core runs per person using this Mac: the owner (tenant "") and each tester added with
    # `scripts/dev.sh add-tester NAME`, each with its own database, port, files, OpenClaw agent and WeChat bot.
    linkmind_tenant: str = Field(default="", pattern=r"^[a-z0-9]{0,16}$")
    # The WeChat bot account (OpenClaw accountId) this Core serves; pushes go to whoever logged that bot in.
    # May stay empty while this Mac has a single bot.
    linkmind_wechat_account: str = ""

    @property
    def agent_id(self) -> str:
        return self.linkmind_tenant or "main"

    @property
    def raw_dir(self) -> Path:
        return LINKMIND_HOME / "tenants" / self.linkmind_tenant / "raw" if self.linkmind_tenant else LINKMIND_HOME / "raw"

    @property
    def media_roots(self) -> list[Path]:
        return [Path(p).expanduser().resolve() for p in self.linkmind_media_roots.split(",") if p.strip()]

    @property
    def owner_names(self) -> list[str]:
        return [n.strip() for n in self.linkmind_owner_names.split(",") if n.strip()]

    @property
    def llm_key_configured(self) -> bool:
        return bool(self.anthropic_api_key if self.linkmind_provider == "anthropic" else self.linkmind_api_key)


@lru_cache
def settings() -> Settings:
    return Settings()
