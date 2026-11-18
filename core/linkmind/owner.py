"""Who "我" is. WeChat shows the owner under different names: the WeChat nickname everywhere, and in each
group the nickname set with "我在本群的昵称". Merged chat-record pages and text copied on Mac only show
names, so every name the owner uses has to be known to label their messages "me".

Names come from three places: LINKMIND_OWNER_NAMES (the WeChat nickname), the user telling the bot
("我在 XX 群叫 YY"), and learning: when someone quotes one of the owner's own messages, the quote shows the
owner's nickname in that group."""

import re

from .config import settings
from .db import pool
from .transcript import names_quoting_owner, owner_texts

_GENERIC_TITLE = re.compile(r"聊天记录$")  # merged-record pages: the title does not identify the chat


def owner_names() -> list[str]:
    with pool().connection() as conn:
        stored = [r["alias"] for r in conn.execute("SELECT alias FROM owner_alias ORDER BY created_at").fetchall()]
    return list(dict.fromkeys([*settings().owner_names, *stored]))


def add_names(names: set[str] | list[str], chat_title: str | None, origin: str) -> list[str]:
    """Store new owner names; returns the ones that were not known yet."""
    known = set(owner_names())
    new = [n.strip() for n in names if n.strip() and n.strip() not in known]
    if new:
        with pool().connection() as conn, conn.cursor() as cur:
            cur.executemany(
                "INSERT INTO owner_alias (alias, chat_title, origin) VALUES (%s, %s, %s) ON CONFLICT DO NOTHING",
                [(n, chat_title, origin) for n in new],
            )
    return new


def relabel(names: list[str]) -> list[str]:
    """Mark stored messages sent under these names as the owner's. Returns the affected source ids."""
    if not names:
        return []
    with pool().connection() as conn:
        rows = conn.execute(
            "UPDATE source_segment SET speaker = 'me' WHERE speaker IN ('them', 'other', 'unknown')"
            " AND (speaker_name = ANY(%s::text[])"
            "      OR regexp_replace(speaker_name, '\\s*[（(].*$', '') = ANY(%s::text[]))"
            " RETURNING source_id",
            (names, names),
        ).fetchall()
    return list(dict.fromkeys(str(r["source_id"]) for r in rows))


def learn_from_source(source_id: str) -> list[str]:
    """Learn the owner's group nickname from quotes of the owner's messages in a transcribed screenshot."""
    with pool().connection() as conn:
        src = conn.execute("SELECT transcript_meta FROM source WHERE id = %s", (source_id,)).fetchone()
        segs = conn.execute(
            "SELECT speaker, speaker_name, text, quote_text FROM source_segment WHERE source_id = %s ORDER BY seq",
            (source_id,),
        ).fetchall()
        title = (src["transcript_meta"] or {}).get("chat_title") if src else None
        mine = [s["text"] for s in segs if s["speaker"] == "me"]
        if title and not _GENERIC_TITLE.search(title):
            # The quoted message may be in an earlier screenshot of the same chat.
            mine += [
                r["text"]
                for r in conn.execute(
                    "SELECT g.text FROM source_segment g JOIN source s ON s.id = g.source_id"
                    " WHERE g.speaker = 'me' AND s.transcript_meta->>'chat_title' = %s AND s.id <> %s",
                    (title, source_id),
                ).fetchall()
            ]
    if not mine or not any(s["quote_text"] for s in segs):
        return []
    found = names_quoting_owner([dict(s) for s in segs], owner_texts(mine), owner_names())
    return add_names(found, title, "learned")
