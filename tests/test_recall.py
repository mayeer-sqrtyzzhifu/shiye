from __future__ import annotations

from companion import db
from companion.recall import TOTAL_LIMIT, recall
from companion.timequery import resolve

REF = "2026-11-04T09:00:00Z"


def seed(conn):
    conn.execute(
        "INSERT INTO messages (role, content, message_time, created_at) VALUES (?,?,?,?)",
        ("user", "昨天搬家搬到凌晨", "2026-11-03T22:10:00Z", "2026-11-03T22:10:00Z"),
    )
    conn.execute(
        "INSERT INTO messages (role, content, message_time, created_at, no_send) VALUES (?,?,?,?,?)",
        ("user", "我朋友说中介坑了他一笔押金", "2026-11-02T12:00:00Z", "2026-11-02T12:00:00Z", 1),
    )
    now = db.utc_now()
    # messages_fts 没有触发器，写入后必须显式同步（README「已知取舍」）
    for mid, content in conn.execute("SELECT id, content FROM messages").fetchall():
        db.upsert_message_fts(conn, mid, content)
    rows = [
        ("三月初搬了一次家，累坏了", "2026-03-01", "2026-03-10"),
        ("去年生日那天我们去了海边", None, None),
        ("用户养了一只叫麻薯的猫", None, None),
    ]
    for content, ev_start, ev_end in rows:
        cur = conn.execute(
            "INSERT INTO memory_items "
            "(content, message_time, created_at, updated_at, event_start, event_end) "
            "VALUES (?,?,?,?,?,?)",
            (content, now, now, now, ev_start, ev_end),
        )
        db.upsert_memory_fts(conn, cur.lastrowid, content)


def test_migrations_reach_latest(conn):
    assert db.schema_version(conn) == 2
    assert conn.execute(
        "SELECT count(*) FROM sqlite_master WHERE name='profile'"
    ).fetchone()[0] == 1


def test_migration_is_idempotent(conn):
    assert db.apply_schema(conn) == 2
    assert db.apply_schema(conn) == 2


def test_profile_slots_are_whitelisted(conn):
    db.set_profile(conn, "birthday", "03-05")
    assert db.get_profile(conn)["birthday"] == "03-05"
    try:
        db.set_profile(conn, "favourite_colour", "blue")
    except ValueError:
        pass
    else:  # pragma: no cover
        raise AssertionError("unknown profile slot must be rejected")


def test_birthday_from_profile_beats_coarse_window(conn):
    db.set_profile(conn, "birthday", "03-05")
    profile = db.get_profile(conn)

    with_profile = resolve("去年生日那天我们聊了什么", reference_time=REF, profile=profile)
    assert with_profile.matched == "birthday"
    assert with_profile.conf == 0.9
    assert with_profile.exact.start == "2025-03-05"

    without = resolve("去年生日那天我们聊了什么", reference_time=REF, profile=None)
    assert without.matched != "birthday"
    assert without.conf < 0.6


def test_message_axis_hit_is_not_an_event_hit(conn):
    seed(conn)
    result = recall(conn, "昨天我们聊了什么", reference_time=REF)
    assert result.query.axis == "message"
    sources = {h.source for h in result.hits}
    assert "date_message" in sources
    assert "date_event" not in sources


def test_event_axis_finds_interval_span(conn):
    seed(conn)
    result = recall(conn, "3月5号发生了什么", reference_time=REF)
    assert any(h.source == "date_event" for h in result.hits)


def test_no_send_messages_stay_local(conn):
    seed(conn)
    result = recall(conn, "中介坑了", reference_time=REF)
    assert all(not h.no_send for h in result.hits)
    assert any(h.no_send for h in result.local_only)
    assert {h.id for h in result.local_only} == {2}


def test_no_send_applies_to_date_hits_too(conn):
    """tier0 命中的消息同样不能进上下文 —— no_send 对所有出网路径一视同仁。"""
    seed(conn)
    result = recall(conn, "昨天我们聊了什么", reference_time=REF)
    assert all(not h.no_send for h in result.hits)
    assert {h.id for h in result.local_only} == {2}
    assert all(h.source == "date_message" for h in result.local_only)


def test_quotas_and_total_cap(conn):
    seed(conn)
    result = recall(conn, "搬家", reference_time=REF)
    assert len(result.hits) <= TOTAL_LIMIT
    per_source: dict[str, int] = {}
    for hit in result.hits:
        per_source[hit.source] = per_source.get(hit.source, 0) + 1
    assert per_source.get("date_event", 0) <= 2
    assert per_source.get("fts_memory", 0) <= 3
    assert per_source.get("fts_message", 0) <= 1


def test_superseded_memory_never_recalled(conn):
    seed(conn)
    row = conn.execute("SELECT id FROM memory_items WHERE content LIKE '%麻薯%'").fetchone()
    conn.execute("UPDATE memory_items SET status='hidden' WHERE id=?", (row["id"],))
    result = recall(conn, "麻薯", reference_time=REF)
    assert all(h.id != row["id"] for h in result.hits)


def test_low_confidence_query_skips_date_path(conn):
    seed(conn)
    result = recall(conn, "那阵子", reference_time=REF)
    assert result.query.usable is False
    assert not any(h.tier == 0 for h in result.hits)


def test_empty_query_returns_nothing(conn):
    seed(conn)
    result = recall(conn, "", reference_time=REF)
    assert result.hits == ()
    assert result.query.conf == 0.0
