from __future__ import annotations

import json

import pytest

from companion import db
from companion.deletion import (
    apply,
    complete_blur,
    is_stale,
    resolve_cluster,
    snapshot,
)
from companion.recall import recall

NOW = "2026-11-04T09:00:00Z"


def add_message(conn, content, *, no_send=0):
    cur = conn.execute(
        "INSERT INTO messages (role, content, message_time, created_at, no_send) "
        "VALUES ('user', ?, ?, ?, ?)",
        (content, NOW, NOW, no_send),
    )
    db.upsert_message_fts(conn, cur.lastrowid, content)
    return cur.lastrowid


def add_memory(conn, content, *, evidence=(), **cols):
    payload = {
        "content": content,
        "created_at": NOW,
        "updated_at": NOW,
        "evidence_message_ids": json.dumps(list(evidence)),
        **cols,
    }
    keys = ", ".join(payload)
    marks = ", ".join("?" * len(payload))
    cur = conn.execute(
        f"INSERT INTO memory_items ({keys}) VALUES ({marks})", list(payload.values())
    )
    db.upsert_memory_fts(conn, cur.lastrowid, content)
    return cur.lastrowid


@pytest.fixture()
def scene(conn):
    """一条消息同时提到搬家与猫，但只有搬家的记忆引用了它。"""
    m1 = add_message(conn, "昨天搬家搬到凌晨，猫也跟着折腾")
    m2 = add_message(conn, "朋友说中介坑了他一笔押金")
    m3 = add_message(conn, "今天给猫换了新的猫砂")

    moving = add_memory(conn, "三月初搬了一次家", evidence=[m1], event_start="2026-03-01")
    moving2 = add_memory(conn, "搬家那天猫一直叫", evidence=[m1, m2])
    cat = add_memory(conn, "用户养了一只叫麻薯的猫", evidence=[m3])
    return conn, {"messages": (m1, m2, m3), "moving": moving, "moving2": moving2, "cat": cat}


def test_cluster_follows_evidence_connectivity(scene):
    conn, ids = scene
    cluster = resolve_cluster(conn, [ids["moving"]])
    assert set(cluster.memory_ids) == {ids["moving"], ids["moving2"]}
    assert set(cluster.message_ids) == set(ids["messages"][:2])
    assert ids["cat"] not in cluster.memory_ids


def test_cluster_does_not_merge_separate_events(scene):
    """同一句里提到猫与搬家，但没共享证据的记忆不能被并进来。"""
    conn, ids = scene
    cluster = resolve_cluster(conn, [ids["cat"]])
    assert cluster.memory_ids == (ids["cat"],)


def test_preview_shows_content_not_only_counts(scene):
    conn, ids = scene
    text = resolve_cluster(conn, [ids["moving"]]).describe()
    assert "将影响 2 条记忆 / 2 条消息" in text
    assert "三月初搬了一次家" in text
    assert "[user]" in text


def test_purge_removes_memory_messages_and_index(scene):
    conn, ids = scene
    cluster = resolve_cluster(conn, [ids["moving"]])
    result = apply(conn, cluster, "purge")

    assert result.fts_ok is True
    assert conn.execute(
        "SELECT count(*) FROM memory_items WHERE id IN (?,?)",
        (ids["moving"], ids["moving2"]),
    ).fetchone()[0] == 0
    assert conn.execute(
        "SELECT count(*) FROM messages WHERE id IN (?,?)", ids["messages"][:2]
    ).fetchone()[0] == 0
    assert db.verify_fts(conn).ok
    assert db.search_memories(conn, "搬家") == []


def test_purge_is_not_recallable(scene):
    conn, ids = scene
    apply(conn, resolve_cluster(conn, [ids["moving"]]), "purge")
    result = recall(conn, "搬家", reference_time=NOW)
    assert all(h.id not in {ids["moving"], ids["moving2"]} for h in result.hits)
    assert all(h.id not in set(ids["messages"][:2]) for h in result.hits)


def test_hide_keeps_content_but_excludes_recall(scene):
    conn, ids = scene
    apply(conn, resolve_cluster(conn, [ids["moving"]]), "hide")

    row = conn.execute(
        "SELECT content, status FROM memory_items WHERE id=?", (ids["moving"],)
    ).fetchone()
    assert row["status"] == "hidden"
    assert "搬了一次家" in row["content"]     # 原文还在
    assert db.search_memories(conn, "搬家") == []
    assert db.verify_fts(conn).ok


def test_blur_is_two_phase(scene):
    conn, ids = scene
    apply(conn, resolve_cluster(conn, [ids["moving2"]]), "blur")

    # 阶段一：立即不可召回
    assert conn.execute(
        "SELECT status FROM memory_items WHERE id=?", (ids["moving2"],)
    ).fetchone()["status"] == "blurring"
    assert db.search_memories(conn, "猫一直叫") == []

    # 阶段二：改写并恢复
    complete_blur(conn, ids["moving2"], "那段时间搬过一次家")
    assert db.search_memories(conn, "搬过一次家")
    assert db.search_memories(conn, "猫一直叫") == []


def test_lexicon_retired_only_when_evidence_fully_deleted(scene):
    """证据全被删 → 退场；还有一条证据活着 → 保留（降级为"还知道有这回事"）。"""
    conn, ids = scene
    m1, m2, m3 = ids["messages"]

    shared = conn.execute(
        "INSERT INTO lexicon_entry (surface, kind, meaning, evidence_message_ids) "
        "VALUES ('进厂','alias','加班',?)",
        (json.dumps([m1, m3]),),          # m3 不在删除范围内 → 还有证据活着
    ).lastrowid
    exclusive = conn.execute(
        "INSERT INTO lexicon_entry (surface, kind, meaning, evidence_message_ids) "
        "VALUES ('团子','nick_name','旧的称呼',?)",
        (json.dumps([m1]),),              # 唯一证据会被删 → 应退场
    ).lastrowid

    apply(conn, resolve_cluster(conn, [ids["moving"]]), "purge")

    rows = {r["id"]: r["status"] for r in conn.execute("SELECT id, status FROM lexicon_entry")}
    assert rows[exclusive] == "retired"
    assert rows[shared] == "active"


def test_session_state_is_scrubbed(scene):
    conn, ids = scene
    conn.execute(
        "INSERT INTO session_state (session_id, locked_memory_ids, last_recall_ids, updated_at) "
        "VALUES ('s1', ?, ?, ?)",
        (json.dumps([ids["moving"], 999]), json.dumps([ids["moving2"]]), NOW),
    )
    apply(conn, resolve_cluster(conn, [ids["moving"]]), "purge")

    row = conn.execute("SELECT * FROM session_state WHERE session_id='s1'").fetchone()
    assert json.loads(row["locked_memory_ids"]) == [999]
    assert json.loads(row["last_recall_ids"]) == []


def test_deletion_target_records_cluster(scene):
    conn, ids = scene
    apply(conn, resolve_cluster(conn, [ids["moving"]]), "purge")
    rows = conn.execute("SELECT cluster_id, target_kind, target_id FROM deletion_target").fetchall()
    kinds = {r["target_kind"] for r in rows}
    assert {"memory", "message"} <= kinds
    assert len({r["cluster_id"] for r in rows}) == 1


def test_derivation_rows_removed_on_purge(scene):
    conn, ids = scene
    conn.execute(
        "INSERT INTO memory_derivation (memory_id, target_kind, target_id, created_at) "
        "VALUES (?, 'diary', '2026-03-01', ?)",
        (ids["moving"], NOW),
    )
    apply(conn, resolve_cluster(conn, [ids["moving"]]), "purge")
    assert conn.execute("SELECT count(*) FROM memory_derivation").fetchone()[0] == 0


def test_revision_guard_detects_stale(scene):
    conn, ids = scene
    snap = snapshot(conn)
    assert is_stale(conn, snap) is False

    apply(conn, resolve_cluster(conn, [ids["moving"]]), "purge")
    assert is_stale(conn, snap) is True


def test_unknown_level_is_rejected(scene):
    conn, ids = scene
    with pytest.raises(ValueError):
        apply(conn, resolve_cluster(conn, [ids["moving"]]), "nuke")
