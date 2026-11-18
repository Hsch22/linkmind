"""Ingest pipeline, step 1: store raw material and turn it into a speaker-attributed transcript.

Later steps (resolve person -> extract claims with evidence -> reconcile) build on the
source + source_segment rows written here."""

import hashlib
import logging
import mimetypes
import re
import shutil
import threading
import time
import uuid
from concurrent.futures import Future, ThreadPoolExecutor
from concurrent.futures import TimeoutError as FutureTimeout
from datetime import datetime
from pathlib import Path
from typing import Literal

from psycopg.types.json import Jsonb

from . import connect, extract, owner, vision, web
from .config import settings
from .db import pool
from .schemas import ChatTranscript
from .transcript import SegKey, find_overlap, is_weak, parse_copied_chat

log = logging.getLogger(__name__)

MaterialKind = Literal["auto", "chat_text", "screenshots", "dictation", "file", "link"]
LINK_EXCERPT_PARAGRAPHS = 3
LINK_EXCERPT_CHARS = 120
NOTE_WINDOW = "15 minutes"
_LABEL = {"me": "我", "them": "对方", "system": "系统", "unknown": "?"}
# Merged chat-record pages all carry a generic title ("群聊的聊天记录"); two different records sent back to back
# would look like one chat, so those only merge when their messages actually overlap.
_GENERIC_TITLE = re.compile(r"聊天记录$")

_executor = ThreadPoolExecutor(max_workers=2, thread_name_prefix="transcribe")
_extract_executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="extract")
_extract_pending: set[str] = set()
_extract_pending_lock = threading.Lock()
_merge_lock = threading.Lock()
_IMAGE_EXT = {".png", ".jpg", ".jpeg", ".gif", ".webp", ".heic", ".heif", ".bmp"}


class IngestError(ValueError):
    pass


def _uuid(value: str) -> str:
    try:
        return str(uuid.UUID(str(value)))
    except ValueError as e:
        raise IngestError(f"不是有效的 source_id：{value}") from e


def _line(s: dict) -> str:
    who = _LABEL.get(s["speaker"], s["speaker_name"] or "群成员")
    return f"{who}: {s['text']}" + (f"（引用 {s['quote_text']}）" if s.get("quote_text") else "")


def _check_media(paths: list[str]) -> list[Path]:
    roots = settings().media_roots
    out = []
    for raw in paths:
        p = Path(raw).expanduser().resolve()
        if not p.is_file():
            raise IngestError(f"media file not found: {raw}")
        if not any(p.is_relative_to(r) for r in roots):
            raise IngestError(f"media path outside allowed roots: {raw}")
        out.append(p)
    return out


def _is_image(p: Path) -> bool:
    mt, _ = mimetypes.guess_type(p.name)
    return p.suffix.lower() in _IMAGE_EXT or (mt or "").startswith("image/")


def _decide_kind(kind: MaterialKind, text: str, media: list[Path]) -> str:
    if kind == "link" or (kind == "auto" and not media and web.extract_urls(text) == [text]):
        return "link"
    if kind == "screenshots" or (kind == "auto" and media and all(_is_image(p) for p in media)):
        return "screenshot"
    if kind == "chat_text" or (kind == "auto" and text and not media):
        return "text_paste"
    if kind == "dictation":
        return "note"
    if kind == "file" or media:
        return "file" if not text else "mixed"
    raise IngestError("nothing to ingest: provide text or media_paths")


def _sha(text: str, media: list[Path]) -> str:
    h = hashlib.sha256(text.encode())
    for p in media:
        h.update(p.read_bytes())
    return h.hexdigest()


def _write_transcript(
    source_id: str, transcript: ChatTranscript, meta: dict, sent_at: list[datetime | None] | None = None
) -> None:
    times = sent_at or [None] * len(transcript.segments)
    with pool().connection() as conn:
        conn.execute("DELETE FROM source_segment WHERE source_id = %s", (source_id,))
        with conn.cursor() as cur:
            cur.executemany(
                "INSERT INTO source_segment"
                " (source_id, seq, speaker, speaker_name, time_text, sent_at, content_kind, text, quote_text)"
                " VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)",
                [
                    (source_id, i, s.speaker, s.speaker_name, s.time_text, t, s.content_kind, s.text, s.quote)
                    for i, (s, t) in enumerate(zip(transcript.segments, times, strict=True))
                ],
            )
        conn.execute(
            "UPDATE source SET status = 'transcribed', error = NULL, updated_at = now(),"
            " transcript_meta = %s WHERE id = %s",
            (
                Jsonb(
                    {
                        "chat_title": transcript.chat_title,
                        "is_group": transcript.is_group,
                        "unreadable_notes": transcript.unreadable_notes,
                        **meta,
                    }
                ),
                source_id,
            ),
        )


def _fail(source_id: str, err: Exception) -> None:
    with pool().connection() as conn:
        conn.execute(
            "UPDATE source SET status = 'failed', error = %s, updated_at = now() WHERE id = %s",
            (f"{type(err).__name__}: {err}", source_id),
        )


def _transcribe_job(source_id: str, kind: str, text: str, media: list[Path], user_note: str) -> None:
    target = source_id
    try:
        if kind == "screenshot":
            transcript, meta = vision.transcribe_screenshots(media, user_note)
            _write_transcript(source_id, transcript, meta)
            try:  # someone quoting the owner reveals the owner's nickname in this group
                if learned := owner.learn_from_source(source_id):
                    log.info("learned owner names %s from %s", learned, source_id)
                    for sid in owner.relabel(learned):
                        if sid != source_id:
                            _queue_extract(sid)
            except Exception:
                log.exception("learning owner names from %s failed", source_id)
            try:
                target = _merge_into_batch(source_id) or source_id
            except Exception:  # the screenshot stays a source of its own; its transcript is fine
                log.exception("merging %s into its batch failed", source_id)
        elif kind == "note":
            pass  # the dictation segment is already stored
        elif parsed := parse_copied_chat(text, owner.owner_names()):
            # Copied from WeChat for Mac: names and timestamps are exact, no model needed.
            _write_transcript(source_id, parsed.transcript, {"parser": "wechat_copy"}, parsed.sent_at)
        else:
            transcript, meta = vision.structure_pasted_text(text, user_note)
            _write_transcript(source_id, transcript, meta)
    except Exception as e:  # recorded on the row; the agent sees it via get_source
        log.exception("transcription failed for %s", source_id)
        _fail(source_id, e)
        return
    # People and facts are extracted in the background: the reply goes out as soon as the text is ready.
    _queue_extract(target)


def _queue_extract(source_id: str) -> None:
    """Queue (re-)extraction; a source already waiting in the queue is not queued twice, so a burst of
    screenshots joining one batch is extracted once with all of them."""
    with _extract_pending_lock:
        if source_id in _extract_pending:
            return
        _extract_pending.add(source_id)
    _extract_executor.submit(_run_extract, source_id)


def _run_extract(source_id: str) -> None:
    with _extract_pending_lock:
        _extract_pending.discard(source_id)
    try:
        result = extract.extract_source(source_id)
    except Exception:  # the material is saved and searchable as text; facts can be re-extracted later
        log.exception("extraction failed for %s", source_id)
        return
    if result["facts"]:
        try:
            connect.discover(source_id)
        except Exception:
            log.exception("connection discovery failed for %s", source_id)


def _keys(rows) -> list[SegKey]:
    return [SegKey(r["speaker"], r["speaker_name"], r["text"]) for r in rows]


def _merge_into_batch(source_id: str) -> str | None:
    """Fold a freshly transcribed screenshot into the source of the previous screenshot of the same chat.

    WeChat delivers a multi-select of screenshots as separate messages, handled one turn at a time, so each
    image is transcribed on its own (keeps every reply fast) and then joined here, dropping the overlap.
    Returns the batch's source id when merged."""
    window = settings().linkmind_batch_window_s
    with _merge_lock, pool().connection() as conn:
        me = conn.execute(
            "SELECT id, received_at, media_paths, user_note, transcript_meta FROM source WHERE id = %s FOR UPDATE",
            (source_id,),
        ).fetchone()
        me_meta = me["transcript_meta"] or {}
        title = me_meta.get("chat_title")
        if not title:
            return
        prev = conn.execute(
            "SELECT id, merged_into FROM source WHERE kind = 'screenshot' AND id <> %s"
            " AND status IN ('transcribed', 'extracted', 'merged') AND received_at < %s"
            " AND received_at >= %s - make_interval(secs => %s) ORDER BY received_at DESC LIMIT 1",
            (source_id, me["received_at"], me["received_at"], window),
        ).fetchone()
        if not prev:
            return
        root_id = prev["merged_into"] or prev["id"]
        root = conn.execute(
            "SELECT id, status, media_paths, transcript_meta FROM source WHERE id = %s FOR UPDATE", (root_id,)
        ).fetchone()
        root_meta = root["transcript_meta"] or {}
        if root["status"] not in ("transcribed", "extracted") or root_meta.get("chat_title") != title:
            return None
        old = conn.execute(
            "SELECT id, seq, speaker, speaker_name, text FROM source_segment WHERE source_id = %s ORDER BY seq",
            (root_id,),
        ).fetchall()
        new = conn.execute(
            "SELECT speaker, speaker_name, time_text, sent_at, content_kind, text, quote_text FROM source_segment"
            " WHERE source_id = %s ORDER BY seq",
            (source_id,),
        ).fetchall()
        overlap = find_overlap(_keys(old), _keys(new))
        if overlap == 0 and _GENERIC_TITLE.search(title):
            return
        # In the overlapping part, a line cut off at an edge of the earlier screenshot takes the complete
        # version (text and sender) from the later one.
        start = len(old) - overlap
        for o, n in zip(old[start:], new[:overlap], strict=True):
            if is_weak(SegKey(o["speaker"], o["speaker_name"], o["text"])) and not is_weak(
                SegKey(n["speaker"], n["speaker_name"], n["text"])
            ):
                conn.execute(
                    "UPDATE source_segment SET speaker = %s, speaker_name = %s, text = %s, content_kind = %s,"
                    " quote_text = coalesce(%s, quote_text), time_text = coalesce(time_text, %s) WHERE id = %s",
                    (n["speaker"], n["speaker_name"], n["text"], n["content_kind"], n["quote_text"], n["time_text"],
                     o["id"]),
                )
        added = new[overlap:]
        base = old[-1]["seq"] + 1 if old else 0
        with conn.cursor() as cur:
            cur.executemany(
                "INSERT INTO source_segment"
                " (source_id, seq, speaker, speaker_name, time_text, sent_at, content_kind, text, quote_text)"
                " VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)",
                [
                    (root_id, base + i, s["speaker"], s["speaker_name"], s["time_text"], s["sent_at"],
                     s["content_kind"], s["text"], s["quote_text"])
                    for i, s in enumerate(added)
                ],
            )
        notes = [x for x in (root_meta.get("unreadable_notes"), me_meta.get("unreadable_notes")) if x]
        root_meta.update(
            batch_images=root_meta.get("batch_images", len(root["media_paths"])) + len(me["media_paths"]),
            unreadable_notes="\n".join(notes) or None,
        )
        if overlap == 0:
            root_meta["gaps"] = root_meta.get("gaps", 0) + 1  # no shared message: something may be missing between
        conn.execute(
            "UPDATE source SET media_paths = media_paths || %s, transcript_meta = %s,"
            " user_note = nullif(concat_ws(E'\\n', nullif(user_note, ''), %s::text), ''), updated_at = now()"
            " WHERE id = %s",
            (me["media_paths"], Jsonb(root_meta), me["user_note"], root_id),
        )
        me_meta["merge"] = {
            "into": str(root_id),
            "overlap": overlap,
            "added": len(added),
            "added_preview": [_line(s) for s in added[:5]],
        }
        conn.execute("DELETE FROM source_segment WHERE source_id = %s", (source_id,))
        conn.execute(
            "UPDATE source SET status = 'merged', merged_into = %s, transcript_meta = %s, updated_at = now()"
            " WHERE id = %s",
            (root_id, Jsonb(me_meta), source_id),
        )
    return str(root_id)


def _find_recent(conn, digest: str):
    return conn.execute(
        "SELECT id FROM source WHERE content_sha256 = %s AND received_at > now() - interval '30 days'"
        " ORDER BY received_at DESC LIMIT 1",
        (digest,),
    ).fetchone()


def _ingest_link(text: str, user_note: str) -> dict:
    """Fetch an allowlisted article and store its paragraphs as segments. No LLM involved."""
    urls = web.extract_urls(text)
    if len(urls) != 1:
        raise IngestError("一次请只发一个链接" if urls else "没有找到链接")
    url = urls[0]
    rest = text.replace(url, " ").strip()
    note = "\n".join(x for x in (user_note.strip(), rest) if x)
    try:
        web.check_url(url)
    except web.FetchError as e:
        raise IngestError(str(e)) from e
    digest = hashlib.sha256(f"link:{web.canonical_url(url)}".encode()).hexdigest()
    with pool().connection() as conn:
        if dup := _find_recent(conn, digest):
            return {"duplicate": True, **get_source(str(dup["id"]), preview_only=True, preview_n=0)}

    started = time.monotonic()
    try:
        art = web.fetch_article(url)
    except web.FetchError as e:
        raise IngestError(f"抓取失败：{e}") from e
    meta = {
        "title": art.title,
        "account": art.account,
        "author": art.author,
        "published_at": art.published_at,
        "description": art.description,
        "chars": len(art.text),
        "fetch_ms": round((time.monotonic() - started) * 1000),
    }
    with pool().connection() as conn:
        row = conn.execute(
            "INSERT INTO source (kind, url, raw_text, user_note, content_sha256, status, transcript_meta)"
            " VALUES ('link', %s, %s, %s, %s, 'transcribed', %s) RETURNING id",
            (art.url, art.text, note or None, digest, Jsonb(meta)),
        ).fetchone()
        source_id = str(row["id"])
        speaker_name = art.account or art.author
        with conn.cursor() as cur:
            cur.executemany(
                "INSERT INTO source_segment (source_id, seq, speaker, speaker_name, content_kind, text)"
                " VALUES (%s, %s, 'other', %s, 'article', %s)",
                [(source_id, i, speaker_name, p) for i, p in enumerate(art.paragraphs)],
            )
    return get_source(source_id, preview_only=True)


def ingest(text: str, media_paths: list[str], kind: MaterialKind, user_note: str) -> dict:
    """Store material; transcribe within the sync budget, otherwise finish in the background."""
    text = (text or "").strip()
    user_note = (user_note or "").strip()
    media = _check_media(media_paths or [])
    src_kind = _decide_kind(kind, text, media)
    if src_kind == "link":
        if media:
            raise IngestError("链接和图片/文件请分开发送")
        return _ingest_link(text, user_note)
    digest = _sha(text, media)

    with pool().connection() as conn:
        if dup := _find_recent(conn, digest):
            return {"duplicate": True, **get_source(str(dup["id"]), preview_only=True, preview_n=0)}
        row = conn.execute(
            "INSERT INTO source (kind, raw_text, user_note, content_sha256, status)"
            " VALUES (%s, %s, %s, %s, 'received') RETURNING id",
            (src_kind, text or None, user_note or None, digest),
        ).fetchone()
        source_id = str(row["id"])

        dest = settings().raw_dir / source_id
        if media:
            dest.mkdir(parents=True, exist_ok=True)
        stored = []
        for i, p in enumerate(media):
            target = dest / f"{i:02d}{p.suffix.lower()}"
            shutil.copy2(p, target)
            stored.append(str(target))
        conn.execute("UPDATE source SET media_paths = %s WHERE id = %s", (stored, source_id))

        needs_job = src_kind in ("screenshot", "text_paste", "note")
        if src_kind == "note":  # the user's own dictation is evidence as-is: one segment spoken by "me"
            conn.execute(
                "INSERT INTO source_segment (source_id, seq, speaker, content_kind, text) VALUES (%s, 0, 'me', 'text', %s)",
                (source_id, text),
            )
            conn.execute("UPDATE source SET status = 'transcribed' WHERE id = %s", (source_id,))
        elif needs_job:
            conn.execute("UPDATE source SET status = 'transcribing' WHERE id = %s", (source_id,))
        # Files: stored for now; parsing documents comes in a later phase.

    # Committed above; reads below use other pooled connections.
    if not needs_job:
        return get_source(source_id, preview_only=True, preview_n=0)
    started = time.monotonic()
    fut: Future = _executor.submit(_transcribe_job, source_id, src_kind, text, [Path(s) for s in stored], user_note)
    try:
        fut.result(timeout=settings().linkmind_sync_budget_s)
    except FutureTimeout:
        pass  # keeps running; the row flips to transcribed/extracted/failed when done
    result = get_source(source_id, preview_only=True, preview_n=0)
    result["waited_s"] = round(time.monotonic() - started, 1)
    return result


def _people_of(source_id: str) -> list[dict]:
    """Who this material added facts about, for a one-line reply."""
    with pool().connection() as conn:
        rows = conn.execute(
            "SELECT p.display_name AS name, count(*) AS facts FROM claim c JOIN person p ON p.id = c.person_id"
            " WHERE c.source_id = %s GROUP BY p.display_name ORDER BY count(*) DESC",
            (source_id,),
        ).fetchall()
    return [dict(r) for r in rows]


def get_source(source_id: str, preview_only: bool = False, preview_n: int = 8) -> dict:
    source_id = _uuid(source_id)
    with pool().connection() as conn:
        src = conn.execute(
            "SELECT id, kind, url, status, merged_into, user_note, transcript_meta, error, received_at,"
            " cardinality(media_paths) AS n_media FROM source WHERE id = %s",
            (source_id,),
        ).fetchone()
        if not src:
            raise IngestError(f"unknown source_id: {source_id}")
    if src["status"] == "merged":
        # This screenshot joined an earlier one of the same chat; report the batch and what this image added.
        batch = get_source(str(src["merged_into"]), preview_only=True, preview_n=0)
        merge = (src["transcript_meta"] or {}).get("merge", {})
        return {
            **{k: batch[k] for k in ("source_id", "kind", "status", "chat_title", "is_group", "segment_count")},
            "merged": True,
            "batch_images": batch.get("batch_images"),
            "added_count": merge.get("added"),
            "overlap_with_previous": merge.get("overlap"),
            "added_preview": merge.get("added_preview"),
            "participants": batch.get("participants"),
            "unreadable_notes": (src["transcript_meta"] or {}).get("unreadable_notes"),
        }
    with pool().connection() as conn:
        segs = conn.execute(
            "SELECT seq, speaker, speaker_name, time_text, content_kind, text, quote_text FROM source_segment"
            " WHERE source_id = %s ORDER BY seq",
            (source_id,),
        ).fetchall()
    meta = src["transcript_meta"] or {}
    if src["kind"] == "link":
        # The agent only needs to confirm what was saved; the article body stays in the database.
        return {
            "source_id": str(src["id"]),
            "kind": "link",
            "status": src["status"],
            "url": src["url"],
            **{k: meta.get(k) for k in ("title", "account", "author", "published_at", "chars")},
            "paragraph_count": len(segs),
            "excerpt": [
                s["text"][:LINK_EXCERPT_CHARS] + ("…" if len(s["text"]) > LINK_EXCERPT_CHARS else "")
                for s in segs[:LINK_EXCERPT_PARAGRAPHS]
            ],
            "user_note": src["user_note"],
            "error": src["error"],
        }
    lines = [_line(s) for s in (segs[:preview_n] if preview_only else segs)]
    out = {
        "source_id": str(src["id"]),
        "kind": src["kind"],
        "status": src["status"],
        "chat_title": meta.get("chat_title"),
        "is_group": meta.get("is_group"),
        "segment_count": len(segs),
        "transcript": lines,
        "truncated": preview_only and len(segs) > preview_n,
        "unreadable_notes": meta.get("unreadable_notes"),
        "media_count": src["n_media"],
        "user_note": src["user_note"],
        "error": src["error"],
        "participants": list(
            dict.fromkeys(s["speaker_name"] for s in segs if s["speaker"] in ("them", "other") and s["speaker_name"])
        )[:8],
        "people": _people_of(source_id),
    }
    if "batch_images" in meta:
        out["batch_images"] = meta["batch_images"]
    if meta.get("parser"):
        out["parser"] = meta["parser"]  # e.g. wechat_copy: parsed exactly, no model involved
    elif llm := {k: meta[k] for k in ("model", "latency_ms") if k in meta}:
        out["llm"] = llm
    return out


def add_note(note: str, source_id: str = "") -> dict:
    """Attach what the user said about material to it (WeChat delivers a caption as its own message,
    after the images). Without source_id: the most recent material from the last 15 minutes."""
    note = (note or "").strip()
    if not note:
        raise IngestError("备注是空的")
    with pool().connection() as conn:
        if source_id:
            row = conn.execute("SELECT id, merged_into FROM source WHERE id = %s", (_uuid(source_id),)).fetchone()
        else:
            row = conn.execute(
                f"SELECT id, merged_into FROM source WHERE received_at > now() - interval '{NOTE_WINDOW}'"
                " ORDER BY received_at DESC LIMIT 1"
            ).fetchone()
        if not row:
            raise IngestError("没有找到最近 15 分钟内的资料；请说明备注属于哪一份")
        target = str(row["merged_into"] or row["id"])
        conn.execute(
            "UPDATE source SET user_note = concat_ws(E'\\n', nullif(user_note, ''), %s::text), updated_at = now()"
            " WHERE id = %s AND position(%s::text in coalesce(user_note, '')) = 0",
            (note, target, note),
        )
    # The note can say who the chat is with; re-extract in the background so facts land on the right person.
    _queue_extract(target)
    src = get_source(target, preview_only=True, preview_n=0)
    return {k: src.get(k) for k in ("source_id", "kind", "chat_title", "title", "user_note")}


def set_my_name(name: str, chat_title: str = "") -> dict:
    """The user says how they appear somewhere in WeChat (nickname, or nickname in one group). Messages
    already stored under that name become the user's own, and those materials are re-extracted."""
    name = (name or "").strip()
    if not name or len(name) > 40:
        raise IngestError("昵称为空或太长")
    new = owner.add_names([name], chat_title.strip() or None, "user")
    affected = owner.relabel([name])
    for sid in affected:
        _queue_extract(sid)
    return {"my_names": owner.owner_names(), "already_known": not new, "updated_materials": len(affected)}


def recent_sources(limit: int = 5) -> list[dict]:
    with pool().connection() as conn:
        rows = conn.execute(
            "SELECT s.id, s.kind, s.status, s.received_at,"
            " coalesce(s.transcript_meta->>'chat_title', s.transcript_meta->>'title') AS chat_title,"
            " (SELECT count(*) FROM source_segment g WHERE g.source_id = s.id) AS segments,"
            " cardinality(s.media_paths) AS images"
            " FROM source s WHERE s.status <> 'merged' ORDER BY s.received_at DESC LIMIT %s",
            (limit,),
        ).fetchall()
    return [
        {**r, "id": str(r["id"]), "received_at": r["received_at"].isoformat(timespec="seconds")} for r in rows
    ]
