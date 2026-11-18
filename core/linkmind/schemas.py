"""Structured-output schemas. Every field is required (nullable where absence is legitimate),
so the model must state "unknown" explicitly instead of silently omitting it."""

from typing import Literal

from pydantic import BaseModel, Field

Speaker = Literal["me", "them", "other", "system", "unknown"]
ContentKind = Literal["text", "image", "voice", "file", "link", "quote", "transfer", "sticker", "other"]


class TranscriptSegment(BaseModel):
    speaker: Speaker = Field(
        description="me = the account owner (right-aligned bubbles); them = the 1:1 counterpart (left-aligned); "
        "other = a non-owner member of a group chat; system = centered gray notices; unknown = cannot tell"
    )
    speaker_name: str | None = Field(description="Sender name shown above the bubble (group chats); null otherwise")
    time_text: str | None = Field(
        description="The nearest timestamp label shown above this message, verbatim (e.g. '昨天 21:03'); null if none visible"
    )
    content_kind: ContentKind
    text: str = Field(
        description="Verbatim message text, not translated or corrected. Non-text content becomes a short bracketed "
        "description such as [图片: 会议海报] or [语音 12\"]. Only the reply itself, never the quoted text."
    )
    quote: str | None = Field(
        description="If this message is a quoted reply: the gray quote box shown directly under its bubble, verbatim "
        "as '<name>: <quoted text>'. null for ordinary messages."
    )


class ChatTranscript(BaseModel):
    chat_title: str | None = Field(description="Title in the top bar (contact remark/name or group name), verbatim")
    is_group: bool
    segments: list[TranscriptSegment] = Field(description="Messages in chronological order, overlaps removed")
    unreadable_notes: str | None = Field(description="What could not be read or attributed, if anything")


# --- extraction: people and facts --------------------------------------------------------------------

FactKind = Literal["profile", "need", "offer", "commitment", "event", "opinion", "relation"]


class ExtractedPerson(BaseModel):
    ref: str = Field(description="Local id used by facts in this output, e.g. p1")
    name: str = Field(
        description="Best name to show: a real name if visible (WeChat shows 'group nickname(owner's remark)', "
        "and the remark usually holds the real name, e.g. '小王子(王子涵 12.6)' -> 王子涵), else the nickname"
    )
    aliases: list[str] = Field(description="Every name string used for this person in the material, verbatim")


class ExtractedFact(BaseModel):
    person_ref: str
    kind: FactKind = Field(
        description="profile = who they are (work, org, role, field, city, background); need = what they look for; "
        "offer = what they can provide (skills, resources, contacts); commitment = a plan or promise between them "
        "and the owner; event = something that happened or will happen to them; opinion = a view or preference "
        "worth knowing; relation = how they relate to someone else"
    )
    content: str = Field(description="One concise Chinese statement about the person, without their name")
    epistemic: Literal["stated_by_them", "stated_by_me", "inferred"] = Field(
        description="stated_by_them: they said it; stated_by_me: the owner said it; inferred: you derived it"
    )
    confidence: float = Field(description="0 to 1")
    evidence: list[int] = Field(description="Numbers of the messages this fact rests on")
    quote: str = Field(description="Shortest verbatim span from those messages that supports the fact")


class Extraction(BaseModel):
    people: list[ExtractedPerson] = Field(description="Only people who have at least one fact below")
    facts: list[ExtractedFact]


# --- questions ------------------------------------------------------------------------------------------


class QueryPlan(BaseModel):
    names: list[str] = Field(description="Names or nicknames of people mentioned in the question")
    keywords: list[str] = Field(
        description="2-8 short search terms for the topic (not the names), with synonyms and both Chinese and "
        "English forms where natural, e.g. ['世界模型', 'world model']; empty if the question is only about a person"
    )


class Answer(BaseModel):
    answer: str = Field(description="The reply to the owner, in Chinese, without reference tags")
    cited: list[str] = Field(description="Tags of the records the answer relies on, e.g. ['C3', 'S1']")


# --- connections ----------------------------------------------------------------------------------------


class FoundConnection(BaseModel):
    need: int = Field(description="Number of the 需求 fact")
    helper: int = Field(description="Number of the other person's fact that shows they can meet it")
    reason: str = Field(description="Why they fit, one short Chinese sentence (at most 40 characters), no names")
    strength: Literal["strong", "weak"] = Field(
        description="strong: the helper clearly has or does the specific thing needed; weak: only a related field or a guess"
    )


class FoundConnections(BaseModel):
    connections: list[FoundConnection] = Field(description="Usually empty")
