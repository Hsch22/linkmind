from linkmind.schemas import ChatTranscript, TranscriptSegment
from linkmind.transcript import (
    CUT_MARK,
    SegKey,
    apply_owner,
    find_overlap,
    is_owner,
    names_quoting_owner,
    owner_texts,
    parse_copied_chat,
    split_quote,
)

OWNERS = ["胡思成"]

# Shape of WeChat for Mac's multi-select copy (2026-11-09 sample, content replaced).
MAC_COPY = """胡思成
2026年11月06日 10:46
PR：github.com/example/repo/pull/1

张三
2026年11月06日 12:08
收到，我看看

你下午有空吗

胡思成
2026年11月06日 12:14
[图片]"""


def seg(speaker, text, name=None):
    return TranscriptSegment(speaker=speaker, speaker_name=name, time_text=None, content_kind="text", text=text, quote=None)


def test_is_owner_matches_name_with_remark():
    assert is_owner("胡思成", OWNERS)
    assert is_owner("胡思成(备注)", OWNERS)
    assert is_owner("胡思成（群昵称）", OWNERS)
    assert not is_owner("胡思", OWNERS)
    assert not is_owner("胡思成成", OWNERS)
    assert not is_owner("胡思成", [])


def test_apply_owner_overrides_model_guess():
    t = ChatTranscript(
        chat_title="群聊的聊天记录", is_group=True, unreadable_notes=None,
        segments=[seg("other", "a", "胡思成"), seg("other", "b", "小王子(王子涵 12.6)"), seg("system", "x", "胡思成")],
    )
    out = apply_owner(t, OWNERS)
    assert [s.speaker for s in out.segments] == ["me", "other", "system"]


def test_parse_mac_copy():
    p = parse_copied_chat(MAC_COPY, OWNERS)
    assert p is not None
    s = p.transcript.segments
    assert [x.speaker for x in s] == ["me", "them", "me"]
    assert s[1].text == "收到，我看看\n\n你下午有空吗"  # blank line inside a message is kept
    assert [x.content_kind for x in s] == ["link", "text", "image"]
    assert p.transcript.is_group is False
    assert p.sent_at[0].strftime("%Y-%m-%d %H:%M") == "2026-11-06 10:46"
    assert p.sent_at[0].tzinfo is not None


def test_parse_mac_copy_group_when_several_others():
    text = MAC_COPY + "\n\n李四\n2026年11月06日 12:20\n我也去"
    p = parse_copied_chat(text, OWNERS)
    assert p.transcript.is_group is True
    assert [x.speaker for x in p.transcript.segments] == ["me", "other", "me", "other"]


def test_parse_mac_copy_rejects_other_text():
    assert parse_copied_chat("张三：你好\n我：在吗", OWNERS) is None
    assert parse_copied_chat("明天 10:30 开会", OWNERS) is None
    assert parse_copied_chat("", OWNERS) is None


def test_split_quote():
    assert split_quote("小王子： 明天的分享") == ("小王子", "明天的分享")
    assert split_quote("小李 + infra: 周四开会") == ("小李 + infra", "周四开会")
    assert split_quote("没有冒号") is None
    assert split_quote(None) is None


def q(speaker, text, quote=None, name=None):
    return {"speaker": speaker, "speaker_name": name, "text": text, "quote_text": quote}


def test_learns_group_nickname_from_quote_of_own_message():
    segs = [
        q("me", "明天下午三点在国贸见面吧"),
        q("other", "好的", "小胡同学: 明天下午三点在国贸见面吧", "张三"),
        q("other", "收到", "李四: 我也去", "王五"),  # quotes someone else
    ]
    mine = owner_texts([s["text"] for s in segs if s["speaker"] == "me"])
    assert names_quoting_owner(segs, mine, ["胡思成"]) == {"小胡同学"}


def test_learning_handles_truncated_quotes_and_skips_known_or_short():
    mine = owner_texts(["这个方案我觉得可以先在小范围试一下，再看数据"])
    segs = [
        q("other", "同意", "小胡: 这个方案我觉得可以先在小范围…", "张三"),
        q("other", "嗯", "胡思成: 这个方案我觉得可以先在小范围试一下，再看数据", "李四"),  # already known
        q("other", "好", "阿强: 好的", "赵六"),  # too short to trust
    ]
    assert names_quoting_owner(segs, mine, ["胡思成"]) == {"小胡"}
    assert names_quoting_owner(segs, set(), ["胡思成"]) == set()


def k(speaker, text, name=None):
    return SegKey(speaker, name, text)


def test_overlap_basic():
    prev = [k("them", "a"), k("me", "b"), k("them", "c")]
    new = [k("me", "b"), k("them", "c"), k("me", "d")]
    assert find_overlap(prev, new) == 2


def test_overlap_none():
    assert find_overlap([k("them", "a"), k("me", "b")], [k("them", "x"), k("me", "y")]) == 0


def test_overlap_with_cut_line_at_bottom_of_previous():
    prev = [k("them", "a"), k("other", CUT_MARK, "oly")]
    new = [k("other", "都卷起来了", "oly"), k("other", "下一条", "oly")]
    assert find_overlap(prev, new) == 1


def test_overlap_with_partial_text():
    prev = [k("them", "a"), k("me", "明天下午三点在…")]
    new = [k("me", "明天下午三点在国贸见"), k("them", "好")]
    assert find_overlap(prev, new) == 1


def test_overlap_ignores_whitespace_and_remark_suffix():
    prev = [k("other", "我们组还缺 gpu，数据", "叮叮当当！(周明远 6.27)"), k("other", "收到", "周明远")]
    new = [k("other", "我们组还缺gpu，数据", "叮叮当当！（周明远 6.27）"), k("other", "收到", "周明远"), k("other", "新", "x")]
    assert find_overlap(prev, new) == 2


def test_overlap_needs_matching_sender_names():
    assert find_overlap([k("other", "好的", "张三")], [k("other", "好的", "李四")]) == 0


def test_overlap_real_edges():
    # Shape seen 2026-11-09 in two crops of one group chat (content replaced): the first ends in an unreadable
    # stub, the second starts with the tail of a bubble hidden under the title bar (sender out of frame).
    prev = [
        k("other", "这周组会改到周四了", "小李"),
        k("other", "我们组还缺 gpu、数据或者标注 人手", "周明远 6.27"),
        k("other", "那我先把实验跑起来", "小李"),
        k("other", "结果出来就发群里", "小李"),
        k("unknown", CUT_MARK),
    ]
    new = [
        k("unknown", "者标注人手"),
        k("other", "那我先把实验跑起来", "小李"),
        k("other", "结果出来就发群里", "小李"),
        k("other", "下周我们也要交中期报告", "阿木在coding"),
        k("other", "PPT 还没开始写", "阿木在coding"),
    ]
    assert find_overlap(prev, new) == 4  # the stub is replaced by 阿木在coding's first message
    # Same crop on another run: the model labelled the top fragment as a group member without a name.
    new[0] = k("other", "者标注 人手")
    assert find_overlap(prev, new) == 4


def test_short_fragments_do_not_match():
    assert find_overlap([k("other", "我觉得好", "a")], [k("unknown", "得好"), k("other", "x", "b")]) == 0


def test_overlap_requires_same_speaker():
    assert find_overlap([k("them", "好的")], [k("me", "好的")]) == 0
