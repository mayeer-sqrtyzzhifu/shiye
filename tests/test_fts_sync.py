from __future__ import annotations

import sqlite3

import pytest

from companion import db

NOW = "2026-11-04T09:00:00Z"


def insert_memory(conn, content, *, sync=True, **cols):
    payload = {"content": content, "created_at": NOW, "updated_at": NOW, **cols}
    keys = ", ".join(payload)
    marks = ", ".join("?" * len(payload))
    cur = conn.execute(
        f"INSERT INTO memory_items ({keys}) VALUES ({marks})", list(payload.values())
    )
    if sync:
        db.upsert_memory_fts(conn, cur.lastrowid, content)
    return cur.lastrowid


def insert_message(conn, content, *, sync=True):
    cur = conn.execute(
        "INSERT INTO messages (role, content, message_time, created_at) VALUES (?,?,?,?)",
        ("user", content, NOW, NOW),
    )
    if sync:
        db.upsert_message_fts(conn, cur.lastrowid, content)
    return cur.lastrowid


def test_clean_db_verifies(conn):
    insert_memory(conn, "用户养了一只叫麻薯的猫")
    insert_message(conn, "今天加班到很晚")
    report = db.verify_fts(conn)
    assert report.ok, report.summary()
    assert report.dirty == 0


def test_bypassing_wrapper_is_detected(conn):
    """不调 wrapper 直接写 → 脏标记留下、索引缺失，verify 必须报出来。"""
    insert_memory(conn, "用户养了一只叫麻薯的猫", sync=False)
    report = db.verify_fts(conn)
    assert report.ok is False
    assert report.dirty == 1
    assert report.tables["memory_items"].missing  # 索引里没有
    assert report.tables["memory_items"].indexed == 0


def test_status_update_does_not_dirty(conn):
    mid = insert_memory(conn, "用户养了一只叫麻薯的猫")
    conn.execute("UPDATE memory_items SET status='hidden' WHERE id=?", (mid,))
    assert db.verify_fts(conn).ok


def test_content_update_without_resync_is_stale(conn):
    mid = insert_memory(conn, "用户在 A 公司上班")
    conn.execute("UPDATE memory_items SET content='用户在 B 公司上班' WHERE id=?", (mid,))
    report = db.verify_fts(conn)
    assert report.ok is False
    assert mid in report.tables["memory_items"].stale


def test_orphan_rows_are_shielded_by_join_but_must_still_be_cleaned(conn):
    """主表删了、索引还在：JOIN 是第二道防线，但它挡不住所有路径。

    检索 SQL 与主表 JOIN，所以残影不会真的被召回 —— 这是侥幸，不是设计。
    索引仍必须清干净：否则索引膨胀，且任何不走 JOIN 的路径（统计、未来的
    纯索引查询、备份重建）都会把它漏出来。
    """
    mid = insert_memory(conn, "用户养了一只叫麻薯的猫")
    conn.execute("DELETE FROM memory_items WHERE id=?", (mid,))

    report = db.verify_fts(conn)
    assert report.tables["memory_items"].orphan == (mid,)
    assert report.dirty == 1

    # JOIN 挡住了，所以此刻检索是安全的
    assert db.search_memories(conn, "麻薯") == []

    db.backfill_fts(conn)
    assert db.verify_fts(conn).ok
    assert db.verify_fts(conn).tables["memory_items"].indexed == 0


def test_backfill_repairs_everything(conn):
    keep = insert_memory(conn, "用户养了一只叫麻薯的猫")
    gone = insert_memory(conn, "用户曾在 A 公司上班")
    conn.execute("DELETE FROM memory_items WHERE id=?", (gone,))
    insert_memory(conn, "用户最近在学做饭", sync=False)

    before = db.verify_fts(conn)
    assert before.ok is False

    after = db.backfill_fts(conn)
    assert after.ok, after.summary()
    assert after.dirty == 0
    assert db.search_memories(conn, "麻薯") != []


def test_full_rebuild_matches_incremental(conn):
    insert_memory(conn, "用户养了一只叫麻薯的猫")
    insert_message(conn, "今天加班到很晚")
    incremental = db.backfill_fts(conn)
    full = db.rebuild_fts(conn)
    assert incremental.ok and full.ok
    assert full.tables["memory_items"].indexed == 1
    assert full.tables["messages"].indexed == 1


def test_verify_reports_counts(conn):
    insert_memory(conn, "一")
    insert_memory(conn, "二")
    insert_message(conn, "三")
    report = db.verify_fts(conn)
    assert report.tables["memory_items"].expected == 2
    assert report.tables["messages"].expected == 1
    assert "dirty=0" in report.summary()


def test_backfill_is_safe_when_already_clean(conn):
    insert_memory(conn, "用户养了一只叫麻薯的猫")
    assert db.backfill_fts(conn).ok
    assert db.backfill_fts(conn).ok


def test_deleted_memory_never_reappears_after_backfill(conn):
    """删除 + 修复之后，索引与主表一致，检索不再返回被删内容。"""
    mid = insert_memory(conn, "用户养了一只叫团子的猫")
    conn.execute("DELETE FROM memory_items WHERE id=?", (mid,))
    db.backfill_fts(conn)

    assert db.search_memories(conn, "团子") == []
    assert db.verify_fts(conn).ok
