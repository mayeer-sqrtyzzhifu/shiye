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
    assert db.schema_version(conn) == db.latest_version()
    assert conn.execute(
        "SELECT count(*) FROM sqlite_master WHERE name='profile'"
    ).fetchone()[0] == 1


def test_migration_is_idempotent(conn):
    latest = db.latest_version()
    assert db.apply_schema(conn) == latest
    assert db.apply_schema(conn) == latest


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
    # 这条只验证 no_send 契约；阈值会随着 τ 标定变化，不该让它来干扰判定
    result = recall(conn, "中介坑了", reference_time=REF, keep_ratio_message=0.0)
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

def test_keep_ratio_passes_calibration():
    """相关度比例必须仍然达标 —— 改分词器或 BM25 用法后，这里会先红。

    标定集见 eval/tau_fixture.json，工具见 tools/calibrate_tau.py。
    """
    import json
    import sys
    from pathlib import Path

    root = Path(__file__).resolve().parents[1]
    sys.path.insert(0, str(root / "tools"))
    import calibrate_tau as cal

    from companion import recall

    fixture = json.loads((root / "eval" / "tau_fixture.json").read_text(encoding="utf-8"))
    conn = cal.build_fixture_db(fixture)
    try:
        hits = cal.collect_hits(conn, fixture)
    finally:
        conn.close()

    for kind, ratio in (("memory", recall.KEEP_RATIO_MEMORY),
                        ("message", recall.KEEP_RATIO_MESSAGE)):
        groups: dict[str, list[dict]] = {}
        for hit in hits:
            if hit["kind"] == kind:
                groups.setdefault(hit["query"], []).append(hit)
        kept, relevant_total = [], 0
        for group in groups.values():
            best = max(h["score"] for h in group)
            kept.extend(h for h in group if h["score"] >= best * ratio)
            relevant_total += sum(1 for h in group if h["relevant"])
        kept_relevant = sum(1 for h in kept if h["relevant"])
        precision = kept_relevant / len(kept) if kept else 1.0
        found = kept_relevant / relevant_total if relevant_total else 1.0
        assert precision >= 0.8, f"{kind} 精度掉到 {precision:.2f}（比例={ratio}）"
        assert found >= 0.8, f"{kind} 召回掉到 {found:.2f}（比例={ratio}）"


def test_keep_strong_drops_weak_hits():
    """相对阈值的语义：低于最强命中的一半就丢掉。"""
    from companion.recall import _keep_strong

    assert _keep_strong([], 0.5) == []
    assert _keep_strong([(1, 4.0), (2, 1.0)], 0.5) == [(1, 4.0)]
    assert _keep_strong([(1, 4.0), (2, 2.5)], 0.5) == [(1, 4.0), (2, 2.5)]
    # 0 表示不过滤
    assert _keep_strong([(1, 4.0), (2, 0.1)], 0.0) == [(1, 4.0), (2, 0.1)]


def test_keep_ratio_is_sane():
    """0 等于不筛，1 等于只留最强那条；都不该是默认。"""
    from companion import recall

    for ratio in (recall.KEEP_RATIO_MEMORY, recall.KEEP_RATIO_MESSAGE):
        assert 0 < ratio < 1

