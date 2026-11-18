"""Deterministic transcript helpers (no LLM, no database):
- owner identity: names that mean "me" in views without left/right bubbles;
- text copied from WeChat for Mac ("名字 / 2026年11月06日 10:46 / 内容" blocks), parsed exactly;
- overlap between consecutive screenshots of the same chat, so a batch becomes one transcript."""

import re
from dataclasses import dataclass
from datetime import datetime

from .schemas import ChatTranscript, TranscriptSegment

CUT_MARK = "[被截断]"
_PAREN = re.compile(r"\s*[（(].*$")


def is_owner(name: str | None, owners: list[str]) -> bool:
    """'胡思成' and '胡思成(备注)' / '胡思成（群昵称）' both match owner '胡思成'."""
    if not name or not owners:
        return False
    n = name.strip()
    base = _PAREN.sub("", n)
    return any(n == o or base == o for o in owners)


def apply_owner(t: ChatTranscript, owners: list[str]) -> ChatTranscript:
    """Whatever the model guessed, a message sent under one of the owner's names is "me"."""
    if owners:
        for s in t.segments:
            if s.speaker != "system" and is_owner(s.speaker_name, owners):
                s.speaker = "me"
    return t


_QUOTE = re.compile(r"^\s*(?P<name>[^:：\n]{1,40}?)\s*[:：]\s*(?P<text>.+)$", re.S)


def split_quote(quote: str | None) -> tuple[str, str] | None:
    """'小王子： 明天的分享' -> ('小王子', '明天的分享')."""
    m = _QUOTE.match(quote or "")
    return (m["name"].strip(), m["text"].strip()) if m else None


def names_quoting_owner(segments: list[dict], me_texts: set[str], owners: list[str]) -> set[str]:
    """Names under which other people quote the owner's own messages.

    WeChat shows the owner's messages right-aligned without a name, but a quote of one reads
    '<the owner's nickname in this group>: <text>'. When that text is a message the owner sent in this chat,
    the name is the owner's group nickname."""
    found = set()
    for s in segments:
        parsed = split_quote(s.get("quote_text"))
        if not parsed or s.get("speaker") == "me":
            continue
        name, text = parsed
        if name == "我" or is_owner(name, owners):
            continue
        quoted = _norm(text)
        if len(quoted) < MIN_FRAGMENT:
            continue
        if any(m == quoted or (len(m) >= MIN_FRAGMENT and (m.startswith(quoted) or quoted.startswith(m))) for m in me_texts):
            found.add(name)
    return found


def owner_texts(texts: list[str]) -> set[str]:
    return {n for n in (_norm(t) for t in texts) if n}


# --- text copied from WeChat for Mac -----------------------------------------------------------------

_TIME_LINE = re.compile(
    r"^(?:(?P<y>\d{4})[年/-](?P<mo>\d{1,2})[月/-](?P<d>\d{1,2})日?\s*|(?:昨天|前天|星期[一二三四五六日天]|周[一二三四五六日天])\s*)?"
    r"(?P<h>\d{1,2}):(?P<mi>\d{2})(?::(?P<s>\d{2}))?$"
)
_PLACEHOLDER = {
    "图片": "image", "视频": "other", "文件": "file", "语音": "voice",
    "动画表情": "sticker", "表情": "sticker", "链接": "link", "转账": "transfer",
}
_LINKISH = re.compile(r"https?://\S+|\b[\w-]+(?:\.[\w-]+)*\.(?:com|cn|org|net|io|ai|edu)\b/\S*", re.IGNORECASE)


@dataclass
class ParsedCopy:
    transcript: ChatTranscript
    sent_at: list[datetime | None]


def _is_header(lines: list[str], i: int) -> bool:
    name = lines[i].strip()
    return (
        bool(name)
        and len(name) <= 64
        and i + 1 < len(lines)
        and _TIME_LINE.match(lines[i + 1].strip()) is not None
        and (i == 0 or not lines[i - 1].strip())
    )


def _content_kind(text: str) -> str:
    if m := re.fullmatch(r"\[([^\]]+)\].*", text, re.S):
        if kind := _PLACEHOLDER.get(m.group(1).split(":")[0].split("：")[0]):
            return kind
    return "link" if _LINKISH.search(text) else "text"


def parse_copied_chat(text: str, owners: list[str]) -> ParsedCopy | None:
    """Parse text from WeChat for Mac's multi-select copy. None when the text is not in that format."""
    lines = (text or "").replace("\r\n", "\n").split("\n")
    first = next((i for i, ln in enumerate(lines) if ln.strip()), None)
    if first is None or not _is_header(lines, first):
        return None
    starts = [i for i in range(first, len(lines)) if _is_header(lines, i)]
    blocks = []
    for k, start in enumerate(starts):
        end = starts[k + 1] if k + 1 < len(starts) else len(lines)
        body = "\n".join(lines[start + 2 : end]).strip()
        if body:
            blocks.append((lines[start].strip(), lines[start + 1].strip(), body))
    if not blocks:
        return None
    others = {name for name, _, _ in blocks if not is_owner(name, owners)}
    is_group = len(others) > 1
    segments, sent_at = [], []
    for name, time_text, body in blocks:
        speaker = "me" if is_owner(name, owners) else ("other" if is_group else "them")
        segments.append(
            TranscriptSegment(
                speaker=speaker, speaker_name=name, time_text=time_text,
                content_kind=_content_kind(body), text=body, quote=None,
            )
        )
        m = _TIME_LINE.match(time_text)
        sent_at.append(
            datetime(int(m["y"]), int(m["mo"]), int(m["d"]), int(m["h"]), int(m["mi"]), int(m["s"] or 0)).astimezone()
            if m and m["y"] else None
        )
    return ParsedCopy(
        ChatTranscript(chat_title=None, is_group=is_group, segments=segments, unreadable_notes=None), sent_at
    )


# --- consecutive screenshots ------------------------------------------------------------------------


@dataclass
class SegKey:
    speaker: str
    speaker_name: str | None
    text: str


def _norm(text: str) -> str:
    return re.sub(r"\s+", "", text or "").rstrip("…").rstrip(".")


MIN_FRAGMENT = 4  # characters a partial line needs before it may stand for a whole message


def is_cut(k: SegKey) -> bool:
    return CUT_MARK in k.text or k.text.rstrip().endswith("…")


def sender_unknown(k: SegKey) -> bool:
    # In group chats every bubble carries its sender's name, so a group message without one had its
    # name cut off at the top edge.
    return k.speaker == "unknown" or (k.speaker == "other" and not k.speaker_name)


def is_weak(k: SegKey) -> bool:
    """A line from a screenshot edge: unreadable, visibly truncated, or with its sender out of frame."""
    return is_cut(k) or sender_unknown(k)


def same_message(a: SegKey, b: SegKey) -> bool:
    if CUT_MARK in a.text or CUT_MARK in b.text:
        return True  # an unreadable stub at an edge stands for whatever the other screenshot shows there
    unknown = sender_unknown(a) or sender_unknown(b)
    if not unknown:
        if a.speaker != b.speaker:
            return False
        if a.speaker_name and b.speaker_name and _PAREN.sub("", a.speaker_name) != _PAREN.sub("", b.speaker_name):
            return False
    na, nb = _norm(a.text), _norm(b.text)
    if na == nb:
        return bool(na)
    short = min(len(na), len(nb))
    if short < MIN_FRAGMENT:
        return False
    if a.text.rstrip().endswith("…") or b.text.rstrip().endswith("…"):
        if na.startswith(nb) or nb.startswith(na):  # cut at the bottom edge: the visible start
            return True
    # Cut at the top edge (under the title bar): only the end of the bubble is visible, sender unknown.
    return unknown and (na.endswith(nb) or nb.endswith(na))


def find_overlap(prev: list[SegKey], new: list[SegKey], max_scan: int = 40) -> int:
    """Number of leading messages of `new` that repeat the tail of `prev` (0 = no overlap found).

    Screenshots are taken scrolling down, so the next one usually starts with the last few messages of the
    previous one. Takes the largest run that reaches the end of `prev`."""
    for j in range(max(0, len(prev) - max_scan), len(prev)):
        m = 0
        while j + m < len(prev) and m < len(new) and same_message(prev[j + m], new[m]):
            m += 1
        if m and j + m == len(prev):
            return m
    return 0
