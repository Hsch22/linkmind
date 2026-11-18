import json
from types import SimpleNamespace

from linkmind import connect
from linkmind.schemas import FoundConnection, FoundConnections


def fact(person_id, person, kind, content, new=False):
    return {"person_id": person_id, "person": person, "aliases": None, "kind": kind, "content": content,
            "epistemic": "stated_by_them", "new": new}


FACTS = [
    fact("a", "张三", "need", "在找 GPU 算力跑实验", new=True),
    fact("b", "李四", "offer", "实验室有空闲的 A100"),
    fact("b", "李四", "need", "想招一个前端工程师"),
    fact("c", "王五", "profile", "做前端开发，五年经验"),
    fact("a", "张三", "profile", "在做机器人方向的博士", new=True),
]


def found(*items):
    return FoundConnections(connections=[FoundConnection(need=n, helper=h, reason=r, strength=s) for n, h, r, s in items])


def test_render_groups_by_person_and_marks_new():
    text = connect.render(FACTS)
    assert text.splitlines()[0] == "张三"
    assert "  1. [需求] 在找 GPU 算力跑实验 ★" in text
    assert "  5. [身份] 在做机器人方向的博士 ★" in text
    assert "  2. [能提供] 实验室有空闲的 A100" in text and "A100 ★" not in text


def test_valid_pairs_keeps_strong_new_cross_person_matches_once():
    pairs = connect.valid_pairs(found(
        (1, 2, "李四有空闲 GPU", "strong"),
        (1, 2, "重复", "strong"),            # same pair again
        (3, 4, "王五做前端", "strong"),       # neither fact is new
        (1, 5, "自己", "strong"),             # same person
        (2, 1, "方向反了", "strong"),         # 'need' is not a need
        (1, 4, "沾边", "weak"),
        (9, 1, "越界", "strong"),
    ), FACTS)
    assert [(n["person"], h["person"], r) for n, h, r in pairs] == [("张三", "李四", "李四有空闲 GPU")]


def test_message_lists_each_connection_with_sources():
    row = {"need_name": "张三", "helper_name": "李四", "need": "在找 GPU 算力", "helper_fact": "实验室有空闲的 A100",
           "reason": "对方正好有空闲卡", "need_source": "口述11月9日", "helper_source": "「组会」11月2日"}
    one = connect.message([row])
    assert one.startswith("发现一个可以对接的机会：\n\n张三 在找：在找 GPU 算力\n李四：实验室有空闲的 A100")
    assert one.endswith("来源：口述11月9日、「组会」11月2日")
    two = connect.message([row, {**row, "helper_source": "口述11月9日"}])
    assert two.startswith("发现 2 个可以对接的机会：\n\n1. 张三")
    assert two.endswith("来源：口述11月9日")  # duplicate source shown once


def test_owner_chat_reads_the_login_and_rejects_odd_ids(tmp_path, monkeypatch):
    base = tmp_path / "openclaw-weixin"
    (base / "accounts").mkdir(parents=True)
    monkeypatch.setattr(connect, "OPENCLAW_HOME", tmp_path)
    assert connect.owner_chat() is None
    (base / "accounts.json").write_text(json.dumps(["bot-1"]))
    (base / "accounts" / "bot-1.json").write_text(json.dumps({"token": "x", "userId": "abc_123@im.wechat"}))
    assert connect.owner_chat() == ("bot-1", "abc_123@im.wechat")
    (base / "accounts.json").write_text(json.dumps(["../evil"]))
    assert connect.owner_chat() is None


def test_owner_chat_with_several_bots_needs_its_own_account(tmp_path, monkeypatch):
    base = tmp_path / "openclaw-weixin"
    (base / "accounts").mkdir(parents=True)
    monkeypatch.setattr(connect, "OPENCLAW_HOME", tmp_path)
    (base / "accounts.json").write_text(json.dumps(["bot-1", "bot-2"]))
    for n in (1, 2):
        (base / "accounts" / f"bot-{n}.json").write_text(json.dumps({"userId": f"user{n}@im.wechat"}))
    monkeypatch.setattr(connect, "settings", lambda: SimpleNamespace(linkmind_wechat_account="", linkmind_tenant=""))
    assert connect.owner_chat() is None  # never guess whose bot this Core serves
    monkeypatch.setattr(connect, "settings", lambda: SimpleNamespace(linkmind_wechat_account="bot-2", linkmind_tenant="alice"))
    assert connect.owner_chat() == ("bot-2", "user2@im.wechat")
    (base / "accounts.json").write_text(json.dumps(["bot-1"]))  # only the owner's bot is logged in
    monkeypatch.setattr(connect, "settings", lambda: SimpleNamespace(linkmind_wechat_account="", linkmind_tenant="alice"))
    assert connect.owner_chat() is None  # a tester without a bot never falls back to the owner's


def test_send_params_target_the_owner_conversation():
    p = connect.send_params("bot-1", "o9cq80AbC@im.wechat", "hi")
    assert (p["to"], p["accountId"], p["channel"], p["message"]) == ("o9cq80AbC@im.wechat", "bot-1", "openclaw-weixin", "hi")
    assert p["sessionKey"] == "agent:main:openclaw-weixin:direct:o9cq80abc@im.wechat"
    assert connect.send_params("b", "U@im.wechat", "hi", "alice")["sessionKey"] == "agent:alice:openclaw-weixin:direct:u@im.wechat"
    assert p["idempotencyKey"] != connect.send_params("bot-1", "o9cq80AbC@im.wechat", "hi")["idempotencyKey"]
