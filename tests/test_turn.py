from __future__ import annotations

import json
from datetime import datetime

import pytest

from companion import db
from companion.provider import FakeProvider
from companion.turn import run_turn

NOW = datetime(2026, 11, 4, 19, 50)
REF = "2026-11-04T09:00:00Z"
PERSONA = "你是 Mochi，一个安静、记性好的陪伴者。"
BASE = dict(
    persona=PERSONA,
    boundaries=("不假装是人类",),
    taboo=("前女友的名字",),
)


def seed(conn):
    cur = conn.execute(
        "INSERT INTO messages (role, content, message_time, created_at, no_send) "
        "VALUES ('user', ?, ?, ?, 1)",
        ("我朋友说中介坑了他一笔押金", REF, REF),
    )
    db.upsert_message_fts(conn, cur.lastrowid, "我朋友说中介坑了他一笔押金")

    cur = conn.execute(
        "INSERT INTO memory_items (content, message_time, created_at, updated_at, event_start) "
        "VALUES (?, ?, ?, ?, ?)",
        ("三月初搬了一次家，累坏了", REF, REF, REF, "2026-03-01"),
    )
    db.upsert_memory_fts(conn, cur.lastrowid, "三月初搬了一次家，累坏了")


def test_one_turn_persists_both_messages(conn):
    seed(conn)
    provider = FakeProvider(["嗯，搬家确实很累。"])
    result = run_turn(conn, provider, "还记得我搬家那次吗",
                      now=NOW, reference_time=REF, **BASE)

    assert result.reply == "嗯，搬家确实很累。"
    assert result.persisted is True
    rows = conn.execute("SELECT role, content FROM messages ORDER BY id").fetchall()
    assert [r["role"] for r in rows][-2:] == ["user", "assistant"]
    assert rows[-1]["content"] == result.reply
    assert db.verify_fts(conn).ok


def test_prefix_is_stable_across_two_turns(conn):
    seed(conn)
    provider = FakeProvider(["第一句", "第二句"])
    first = run_turn(conn, provider, "今天好累", now=NOW, reference_time=REF, **BASE)
    second = run_turn(conn, provider, "还是睡不着", now=NOW, reference_time=REF, **BASE)

    assert first.bundle.system_prefix == second.bundle.system_prefix
    assert provider.calls[0]["system"] == provider.calls[1]["system"]


def test_second_turn_sees_first_turn_in_recent(conn):
    seed(conn)
    provider = FakeProvider(["我在", "继续"])
    run_turn(conn, provider, "第一句", now=NOW, reference_time=REF, **BASE)
    run_turn(conn, provider, "第二句", now=NOW, reference_time=REF, **BASE)

    messages = provider.calls[1]["messages"]
    assert {"role": "user", "content": "第一句"} in messages
    assert {"role": "assistant", "content": "我在"} in messages
    assert messages[-1] == {"role": "user", "content": "第二句"}


def test_turn_context_message_sits_between_recent_and_input(conn):
    seed(conn)
    provider = FakeProvider(["好"])
    run_turn(conn, provider, "今天好累", now=NOW, reference_time=REF, **BASE)

    messages = provider.calls[0]["messages"]
    assert messages[-1]["content"] == "今天好累"
    assert messages[-2]["content"].startswith("<turn_context>")
    assert "19:50" in messages[-2]["content"]


def test_recall_reaches_the_dynamic_block(conn):
    seed(conn)
    provider = FakeProvider(["嗯"])
    # 用连续短语检索：bigram 匹配不了「搬家」对「搬了一次家」这种非连续词素，
    # 那类缺口由 §1.2 的 alias 归一化补，不在检索层兜
    result = run_turn(conn, provider, "搬了一次家", now=NOW, reference_time=REF, **BASE)
    assert "三月初搬了一次家" in result.bundle.dynamic_block


def test_no_send_content_never_reaches_the_model(conn):
    seed(conn)
    provider = FakeProvider(["嗯"])
    run_turn(conn, provider, "说说搬家的事", now=NOW, reference_time=REF, **BASE)

    payload = json.dumps(provider.calls[0], ensure_ascii=False)
    # 那条 no_send 消息既不能作为召回片段，也不能作为"近期对话"外发
    assert "我朋友说中介坑了他一笔押金" not in payload


def test_on_text_streams_deltas(conn):
    seed(conn)
    seen: list[str] = []
    provider = FakeProvider(["流式回复"])
    run_turn(conn, provider, "在吗", now=NOW, reference_time=REF,
             on_text=seen.append, **BASE)
    assert seen == ["流式回复"]


def test_empty_reply_is_not_persisted(conn):
    seed(conn)
    provider = FakeProvider([""])
    result = run_turn(conn, provider, "在吗", now=NOW, reference_time=REF, **BASE)
    assert result.persisted is False
    assert "empty_reply" in result.notes
    assert conn.execute("SELECT count(*) FROM messages WHERE role='assistant'").fetchone()[0] == 0


def test_session_turn_index_increments(conn):
    seed(conn)
    provider = FakeProvider(["一", "二"])
    first = run_turn(conn, provider, "a", now=NOW, reference_time=REF, session_id="s1", **BASE)
    second = run_turn(conn, provider, "b", now=NOW, reference_time=REF, session_id="s1", **BASE)
    assert (first.turn_index, second.turn_index) == (1, 2)

    row = conn.execute("SELECT turn_index, last_recall_ids FROM session_state WHERE session_id='s1'").fetchone()
    assert row["turn_index"] == 2


def test_risk_injects_safety_section(conn):
    seed(conn)
    provider = FakeProvider(["我在"])
    result = run_turn(
        conn, provider, "有点撑不住了", now=NOW, reference_time=REF,
        risk=2, crisis_resources=("心理援助热线 12356",), **BASE,
    )
    assert "安全边界" in result.bundle.dynamic_block
    assert "12356" in json.dumps(provider.calls[0], ensure_ascii=False)
