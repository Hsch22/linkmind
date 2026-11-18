from datetime import UTC, datetime

from linkmind.ask import format_records, sources_line, strip_tags
from linkmind.extract import alias_variants, render, verified_quote


def test_alias_variants_from_group_label():
    v = alias_variants("小王子(王子涵 12.6)")
    assert v["小王子"] == "wechat_nickname"
    assert v["王子涵 12.6"] == "wechat_remark"
    assert v["王子涵"] == "real_name"
    assert "小王子(王子涵 12.6)" in v


def test_alias_variants_full_width_parens_and_plain_names():
    assert "周明远" in alias_variants("叮叮当当！（周明远 6.27）")
    assert set(alias_variants("林知夏 8.27")) == {"林知夏 8.27", "林知夏"}
    assert alias_variants("王") == {}  # single characters are too ambiguous to match on


def test_verified_quote():
    assert verified_quote("世界模型", "我们在做 世界 模型的论文") == "世界模型"
    assert verified_quote("编出来的话", "原文是这个") == "原文是这个"


def test_render_marks_owner_and_quotes():
    segs = [
        {"seq": 0, "speaker": "me", "speaker_name": None, "time_text": "10:00", "text": "在吗", "quote_text": None},
        {"seq": 1, "speaker": "other", "speaker_name": "张三", "time_text": None, "text": "好", "quote_text": "我: 在吗"},
    ]
    assert render(segs) == "#0 我 [10:00]: 在吗\n#1 张三: 好（引用 我: 在吗）"


def test_strip_tags():
    tags = {f"C{i}" for i in range(1, 9)} | {f"S{i}" for i in range(1, 6)}
    assert strip_tags("觉得太长（S4「太长了」）", tags) == "觉得太长（「太长了」）"
    assert strip_tags("越短越好 [C3]\n参与讨论（S3）", tags) == "越短越好\n参与讨论"
    assert strip_tags("见 C3–C8 和 S1、S2", tags) == "见  和"
    assert strip_tags("GPU 型号 C200，B3 芯片", tags) == "GPU 型号 C200，B3 芯片"


def test_records_and_sources_line():
    when = datetime(2026, 11, 9, 12, tzinfo=UTC)
    claims = [{"kind": "event", "content": "在写世界模型论文", "epistemic": "inferred", "person": "王子涵",
               "aliases": "小王子", "title": "群聊的聊天记录", "received_at": when}]
    segments = [{"speaker": "me", "speaker_name": None, "text": "x" * 250, "kind": "link", "received_at": when,
                 "title": "两台 DGX Spark"}]
    text, refs = format_records(claims, segments)
    lines = text.split("\n")
    assert lines[0].startswith("[C1] 王子涵（小王子） · 动态 · 在写世界模型论文（群聊的聊天记录 ")
    assert lines[0].endswith("，推测）")
    assert lines[1].startswith("[S1] 文章《两台 DGX Spark》") and lines[1].endswith("…")
    assert sources_line(["C1", "[S1]", "C9"], refs).startswith("来源：群聊的聊天记录（")
    assert sources_line([], refs) is None
