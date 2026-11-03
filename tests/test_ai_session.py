"""AI 会话持久化：新建 / 写消息 / 列表 / 删除（db + web API 层）。"""
from app import db
from app.web import app as webapp


def test_ai_session_crud_lifecycle():
    """新建会话 → 写消息 → 列表含条数 → 读消息顺序 → 删除清空。"""
    sid = db.create_ai_session()
    assert sid > 0
    db.add_ai_message(sid, "user", "你好")
    db.add_ai_message(sid, "assistant", "回复内容")
    db.update_ai_session_title(sid, "诊断")

    msgs = db.list_ai_messages(sid)
    assert [m["role"] for m in msgs] == ["user", "assistant"]
    assert msgs[0]["content"] == "你好"

    sess = db.list_ai_sessions()[0]
    assert sess["id"] == sid
    assert sess["title"] == "诊断"
    assert sess["msg_count"] == 2

    db.delete_ai_session(sid)
    assert db.list_ai_sessions() == []
    assert db.list_ai_messages(sid) == []


def test_list_messages_limit_keeps_latest():
    """limit 只保留最近 N 条且顺序为旧→新。"""
    sid = db.create_ai_session()
    for i in range(20):
        db.add_ai_message(sid, "user", f"消息{i}")
    recent = db.list_ai_messages(sid, limit=5)
    assert len(recent) == 5
    assert [m["content"] for m in recent] == [f"消息{i}" for i in range(15, 20)]


def test_api_session_endpoints():
    """Web API：建会话 / 列表 / 读消息 / 删除。"""
    sid = webapp.api_ai_session_new()["id"]
    db.add_ai_message(sid, "user", "Q")
    db.add_ai_message(sid, "assistant", "A")
    items = webapp.api_ai_sessions()["items"]
    assert any(i["id"] == sid and i["msg_count"] == 2 for i in items)
    msgs = webapp.api_ai_session_messages(sid)["items"]
    assert [m["role"] for m in msgs] == ["user", "assistant"]
    assert webapp.api_ai_session_del(sid)["ok"]
    assert webapp.api_ai_sessions()["items"] == []