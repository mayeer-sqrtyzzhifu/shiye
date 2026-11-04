from __future__ import annotations

import sqlite3

import pytest

from companion import db, fts
from companion.datewin import annual_window_hit, annual_window_sql, overlaps_sql

NOW = "2026-11-04T09:00:00Z"


@pytest.fixture()
def conn(tmp_path):
    c = db.connect(tmp_path / "test.db")
    db.apply_schema(c)
    try:
        yield c
    finally:
        c.close()


def add_memory(conn: sqlite3.Connection, content: str, **cols) -> int:
    payload = {
        "user_id": 1,
        "content": content,
        "message_time": cols.pop("message_time", NOW),
        "created_at": NOW,
        "updated_at": NOW,
        **cols,
    }
    keys = ", ".join(payload)
    marks = ", ".join("?" * len(payload))
    cur = conn.execute(f"INSERT INTO memory_items ({keys}) VALUES ({marks})", list(payload.values()))
    mid = cur.lastrowid
    db.upsert_memory_fts(conn, mid, content)
    return mid


# ───────────────────────────────────────── schema 本身

def test_schema_applies_cleanly(conn):
    names = {r["name"] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    assert {"messages", "memory_items", "lexicon_entry", "session_state",
            "async_task", "deletion_target", "data_revision"} <= names

    report = db.integrity_report(conn)
    assert report["integrity"] == ["ok"]
    assert report["foreign_keys"] == []

    rev = conn.execute("SELECT revision FROM data_revision WHERE id = 1").fetchone()
    assert rev["revision"] == 0


def test_plaintext_mode_is_reported(tmp_path):
    c = db.connect(tmp_path / "p.db")
    try:
        db.apply_schema(c)
        assert db.security_mode(None) == "plaintext-dev"
    finally:
        c.close()


# ───────────────────────────────────────── bm25 方向（评审 2.1 的核心指控）

def test_bm25_is_negative_so_ranking_must_negate(conn):
    hit = add_memory(conn, "去年生日那天我们去了海边")
    add_memory(conn, "今天加班到很晚")
    add_memory(conn, "周末给猫换了猫砂")

    expr = fts.match_expr("生日")
    assert expr is not None

    raw = conn.execute(
        "SELECT rowid, bm25(memories_fts) AS r FROM memories_fts "
        "WHERE memories_fts.tok MATCH ? ORDER BY rank",
        (expr,),
    ).fetchall()
    assert len(raw) == 1
    # 原始 bm25 为负值：直接拿它当"相关度正分"用会筛掉全部结果
    assert raw[0]["r"] < 0

    # 升序 -bm25 会把最差排在最前；正确写法是 relevance = -bm25 后降序
    ranked = db.search_memories(conn, "生日")
    assert ranked[0][0] == hit
    assert ranked[0][1] > 0


def test_search_excludes_superseded_and_hidden(conn):
    keep = add_memory(conn, "用户养了一只叫麻薯的猫")
    gone = add_memory(conn, "用户养了一只叫团子的猫", status="hidden")

    ids = [i for i, _ in db.search_memories(conn, "猫")]
    assert keep in ids
    assert gone not in ids


def test_cjk_needs_bigram_not_unicode61(conn):
    """同一句中文：unicode61 零命中，bigram 命中。这就是必须预分词的原因。"""
    text = "去年生日那天我们去了海边"
    conn.execute("CREATE VIRTUAL TABLE raw_fts USING fts5(body)")
    conn.execute("INSERT INTO raw_fts(body) VALUES (?)", (text,))
    raw_hits = conn.execute(
        "SELECT count(*) FROM raw_fts WHERE raw_fts.body MATCH '生日'"
    ).fetchone()[0]
    assert raw_hits == 0

    add_memory(conn, text)
    assert db.search_memories(conn, "生日")


# ───────────────────────────────────────── 区间与跨年（评审 2.2）

def test_interval_overlap_not_between(conn):
    add_memory(
        conn,
        "三月初那十天一直在搬家",
        event_start="2025-03-01",
        event_end="2025-03-10",
    )
    params = {"q_start": "2025-03-05", "q_end": "2025-03-05"}

    naive = conn.execute(
        f"SELECT count(*) FROM memory_items WHERE event_start BETWEEN :q_start AND :q_end",
        params,
    ).fetchone()[0]
    assert naive == 0  # BETWEEN 漏掉持续事件

    fixed = conn.execute(
        f"SELECT count(*) FROM memory_items WHERE {overlaps_sql()}",
        params,
    ).fetchone()[0]
    assert fixed == 1


def test_annual_window_crossing_new_year():
    window = ("12-28", "01-05")
    assert annual_window_hit("12-30", *window) is True
    assert annual_window_hit("01-02", *window) is True
    assert annual_window_hit("06-01", *window) is False

    c = sqlite3.connect(":memory:")
    naive = c.execute("SELECT '12-30' BETWEEN '12-28' AND '01-05'").fetchone()[0]
    assert naive == 0  # 跨年区间上 BETWEEN 恒假

    fixed = c.execute(f"SELECT {annual_window_sql(chr(39) + '12-30' + chr(39), *window)}").fetchone()[0]
    assert fixed == 1
    c.close()


# ───────────────────────────────────────── 取代语义（评审 3 的索引问题）

def test_lexicon_partial_unique_allows_supersede(conn):
    cur = conn.execute(
        "INSERT INTO lexicon_entry (surface, kind, meaning, status) VALUES (?,?,?,?)",
        ("进厂", "alias", "加班", "active"),
    )
    first = cur.lastrowid

    # 同一 (surface, kind) 再插一条 active 会被部分唯一索引挡住
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute(
            "INSERT INTO lexicon_entry (surface, kind, meaning, status) VALUES (?,?,?,?)",
            ("进厂", "alias", "加班（旧义）", "active"),
        )

    # 退掉旧的之后可以新增替代版本，且旧条目保留（"去年怎么叫的"还能查）
    conn.execute("UPDATE lexicon_entry SET status='retired' WHERE id=?", (first,))
    conn.execute(
        "INSERT INTO lexicon_entry (surface, kind, meaning, status, superseded_by) "
        "VALUES (?,?,?,?,?)",
        ("进厂", "alias", "换工作的委婉说法", "active", first),
    )
    assert conn.execute("SELECT count(*) FROM lexicon_entry").fetchone()[0] == 2


def test_slot_holds_one_current_value(conn):
    first = add_memory(
        conn, "用户在 A 公司上班", slot_key="employer", valid_from="2024-01-01"
    )
    # 同一槽位第二个"当前值"必须被挡（否则新旧单位会同时被注入）
    with pytest.raises(sqlite3.IntegrityError):
        add_memory(conn, "用户在 B 公司上班", slot_key="employer", valid_from="2026-03-01")

    # 正确流程：先给旧值收口 valid_to，再写新值
    conn.execute("UPDATE memory_items SET valid_to='2026-03-01' WHERE id=?", (first,))
    add_memory(conn, "用户在 B 公司上班", slot_key="employer", valid_from="2026-03-01")

    current = conn.execute(
        "SELECT content FROM memory_items WHERE slot_key='employer' "
        "AND valid_to IS NULL AND status='active'"
    ).fetchall()
    assert [r["content"] for r in current] == ["用户在 B 公司上班"]

    # 历史仍然可查
    history = conn.execute(
        "SELECT content FROM memory_items WHERE valid_from <= '2025-06-01' "
        "AND (valid_to IS NULL OR valid_to > '2025-06-01')"
    ).fetchall()
    assert [r["content"] for r in history] == ["用户在 A 公司上班"]


def test_message_no_send_defaults_to_sendable(conn):
    conn.execute(
        "INSERT INTO messages (role, content, message_time, created_at) VALUES (?,?,?,?)",
        ("user", "今天好累", NOW, NOW),
    )
    row = conn.execute("SELECT no_send FROM messages").fetchone()
    assert row["no_send"] == 0
