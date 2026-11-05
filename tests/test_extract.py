from __future__ import annotations

import json

import pytest

from companion import db, extract, lexicon
from companion.provider import FakeProvider
from companion.recall import recall

NOW = "2026-11-04T09:00:00Z"


def add_message(conn, content, role="user"):
    cur = conn.execute(
        "INSERT INTO messages (role, content, message_time, created_at) VALUES (?,?,?,?)",
        (role, content, NOW, NOW),
    )
    db.upsert_message_fts(conn, cur.lastrowid, content)
    return cur.lastrowid


def add_memory(conn, content):
    cur = conn.execute(
        "INSERT INTO memory_items (content, message_time, created_at, updated_at) VALUES (?,?,?,?)",
        (content, NOW, NOW, NOW),
    )
    db.upsert_memory_fts(conn, cur.lastrowid, content)
    return cur.lastrowid


# ───────────────────────────────────────── 语汇消费方

def test_alias_expands_query(conn):
    add_memory(conn, "三月初搬了一次家，累到一周没缓过来")
    conn.execute(
        "INSERT INTO lexicon_entry (surface, kind, canonical, meaning) VALUES (?,?,?,?)",
        ("搬家", "alias", "搬了一次家", "搬家这件事"),
    )
    # 没有归一化时「搬家」匹配不到「搬了一次家」
    # 只验证别名归一化这一环；阈值属于检索策略，见 test_tau_defaults_still_pass_calibration
    hit = recall(conn, "搬家", reference_time=NOW, keep_ratio_memory=0.0)
    assert any(h.kind == "memory" for h in hit.hits)


def test_lexicon_match_is_word_surface_only(conn):
    conn.execute(
        "INSERT INTO lexicon_entry (surface, kind, meaning) VALUES (?,?,?)",
        ("进厂", "inside_joke", "加班"),
    )
    assert lexicon.match(conn, "今天又进厂了")
    assert not lexicon.match(conn, "今天天气不错")


def test_taboo_is_not_returned_by_match(conn):
    conn.execute(
        "INSERT INTO lexicon_entry (surface, kind, meaning) VALUES (?,?,?)",
        ("那个人", "taboo", "禁区称呼"),
    )
    assert lexicon.match(conn, "那个人怎么样") == ()


def test_retired_and_superseded_entries_are_ignored(conn):
    conn.execute(
        "INSERT INTO lexicon_entry (surface, kind, meaning, status) VALUES (?,?,?,?)",
        ("进厂", "inside_joke", "加班", "retired"),
    )
    assert lexicon.match(conn, "进厂") == ()


def test_longer_surface_wins(conn):
    conn.execute("INSERT INTO lexicon_entry (surface, kind, meaning) VALUES ('班','alias','工作')")
    conn.execute("INSERT INTO lexicon_entry (surface, kind, meaning) VALUES ('加班','alias','超时工作')")
    hits = lexicon.match(conn, "今天加班")
    assert hits[0].surface == "加班"


def test_only_alias_expands_query(conn):
    conn.execute(
        "INSERT INTO lexicon_entry (surface, kind, canonical, meaning) VALUES (?,?,?,?)",
        ("进厂", "inside_joke", "加班", "自嘲"),
    )
    matches = lexicon.match(conn, "进厂")
    assert lexicon.expand_query("进厂", matches) == "进厂"


def test_note_use_separates_injected_from_used(conn):
    lid = conn.execute(
        "INSERT INTO lexicon_entry (surface, kind, meaning) VALUES ('进厂','inside_joke','加班')"
    ).lastrowid
    lexicon.note_use(conn, [lid], injected=True)
    row = conn.execute(
        "SELECT use_count, injected_count FROM lexicon_entry WHERE id=?", (lid,)
    ).fetchone()
    assert row["injected_count"] == 1
    assert row["use_count"] == 0      # 注入 ≠ 模型采用


# ───────────────────────────────────────── 抽取

VALID = json.dumps({
    "memories": [{
        "content": "用户当前单位是 B 公司",
        "source_message_ids": [1],
        "epistemic_status": "explicit",
        "slot_key": "employer",
        "valid_from": "2026-03-01",
    }],
    "lexicon": [{
        "surface": "进厂", "kind": "alias", "canonical": "加班",
        "meaning": "自嘲加班", "confidence": 0.9,
    }],
}, ensure_ascii=False)


def provider_replying(text: str) -> FakeProvider:
    return FakeProvider([text])


def test_valid_extraction_writes_memory_with_evidence(conn):
    mid = add_message(conn, "我三月起在 B 公司上班")
    outcome = extract.extract_turn(
        conn, provider_replying(VALID),
        user_text="我三月起在 B 公司上班", assistant_text="记下了",
        evidence_ids=[mid], turn_id="t1",
    )
    assert outcome.ok and outcome.memory_ids
    row = conn.execute("SELECT * FROM memory_items").fetchone()
    assert row["content"] == "用户当前单位是 B 公司"
    assert json.loads(row["evidence_message_ids"]) == [mid]
    assert row["epistemic_status"] == "explicit"
    assert row["slot_key"] == "employer"
    assert db.verify_fts(conn).ok
    assert outcome.lexicon_ids


def test_assistant_text_cannot_be_evidence(conn):
    """助手猜的内容被当成用户事实，是这套系统最危险的污染链。"""
    mid = add_message(conn, "最近还行吧")
    payload = json.dumps({"memories": [{
        "content": "用户最近换了工作",
        "source_message_ids": [mid + 1],       # 指向助手消息
        "epistemic_status": "explicit",
    }]}, ensure_ascii=False)

    outcome = extract.extract_turn(
        conn, provider_replying(payload),
        user_text="最近还行吧", assistant_text="你是不是换了工作",
        evidence_ids=[mid], turn_id="t2",
    )
    assert outcome.memory_ids == ()
    assert any("evidence outside" in r for r in outcome.rejected)


def test_missing_evidence_is_rejected(conn):
    mid = add_message(conn, "随便聊聊")
    payload = json.dumps({"memories": [{"content": "用户喜欢猫"}]}, ensure_ascii=False)
    outcome = extract.extract_turn(
        conn, provider_replying(payload), user_text="随便聊聊",
        assistant_text="嗯", evidence_ids=[mid],
    )
    assert outcome.memory_ids == ()
    assert any("no evidence" in r for r in outcome.rejected)


def test_invalid_json_marks_failure_without_partial_write(conn):
    mid = add_message(conn, "在吗")
    outcome = extract.extract_turn(
        conn, provider_replying("这不是 JSON"), user_text="在吗",
        assistant_text="在的", evidence_ids=[mid], turn_id="t3",
    )
    assert outcome.ok is False
    assert conn.execute("SELECT count(*) FROM memory_items").fetchone()[0] == 0


def test_truncated_json_is_rejected(conn):
    mid = add_message(conn, "在吗")
    outcome = extract.extract_turn(
        conn, provider_replying('{"memories": [{"content": "半条'), user_text="在吗",
        assistant_text="在", evidence_ids=[mid],
    )
    assert outcome.ok is False
    assert outcome.memory_ids == ()


def test_code_fences_are_tolerated(conn):
    mid = add_message(conn, "我三月起在 B 公司上班")
    fenced = "```json\n" + VALID + "\n```"
    outcome = extract.extract_turn(
        conn, provider_replying(fenced), user_text="我三月起在 B 公司上班",
        assistant_text="记下了", evidence_ids=[mid],
    )
    assert outcome.ok and outcome.memory_ids


def test_taboo_is_not_auto_written(conn):
    mid = add_message(conn, "别叫我小名")
    payload = json.dumps({"memories": [], "lexicon": [{
        "surface": "小名", "kind": "taboo", "meaning": "不要叫", "confidence": 0.9,
    }]}, ensure_ascii=False)
    outcome = extract.extract_turn(
        conn, provider_replying(payload), user_text="别叫我小名",
        assistant_text="好", evidence_ids=[mid],
    )
    assert outcome.lexicon_ids == ()
    assert any("needs confirmation" in r for r in outcome.rejected)


def test_same_turn_is_idempotent(conn):
    mid = add_message(conn, "我三月起在 B 公司上班")
    kwargs = dict(user_text="我三月起在 B 公司上班", assistant_text="记下了",
                  evidence_ids=[mid], turn_id="t9")
    first = extract.extract_turn(conn, provider_replying(VALID), **kwargs)
    second = extract.extract_turn(conn, provider_replying(VALID), **kwargs)
    assert first.memory_ids and second.memory_ids == ()
    assert conn.execute("SELECT count(*) FROM memory_items").fetchone()[0] == 1


def test_lexicon_candidate_accumulates_occurrences(conn):
    mid = add_message(conn, "又进厂了")
    payload = json.dumps({"memories": [], "lexicon": [{
        "surface": "进厂", "kind": "inside_joke", "meaning": "加班", "confidence": 0.7,
    }]}, ensure_ascii=False)
    for turn in ("t10", "t11"):
        extract.extract_turn(
            conn, provider_replying(payload), user_text="又进厂了",
            assistant_text="辛苦", evidence_ids=[mid], turn_id=turn,
        )
    row = conn.execute("SELECT occurrence_count FROM lexicon_candidate").fetchone()
    assert row[0] == 2


def test_run_pending_marks_task_done(conn):
    mid = add_message(conn, "我三月起在 B 公司上班")
    extract.enqueue_extract(conn, "t20", {
        "user_text": "我三月起在 B 公司上班", "assistant_text": "记下了",
        "evidence_ids": [mid], "turn_id": "t20",
    })
    results = extract.run_pending(conn, provider_replying(VALID))
    assert len(results) == 1 and results[0].ok
    row = conn.execute("SELECT status FROM async_task WHERE turn_id='t20'").fetchone()
    assert row["status"] == "done"


def test_failed_job_retries_then_gives_up(conn):
    """解析失败要重试，但不能无限重试 —— 到上限就停在 failed，失败可见。"""
    mid = add_message(conn, "在吗")
    extract.enqueue_extract(conn, "t21", {
        "user_text": "在吗", "assistant_text": "在", "evidence_ids": [mid], "turn_id": "t21",
    })

    extract.run_queue(conn, provider_replying("坏掉的输出"))
    row = conn.execute("SELECT status, retry_count FROM async_task WHERE turn_id='t21'").fetchone()
    assert row["status"] == "pending"      # 还能再试
    assert row["retry_count"] == 1

    for _ in range(extract.MAX_ATTEMPTS):
        extract.run_queue(conn, provider_replying("坏掉的输出"))
    row = conn.execute("SELECT status, retry_count, last_error FROM async_task WHERE turn_id='t21'").fetchone()
    assert row["status"] == "failed"
    assert row["retry_count"] >= extract.MAX_ATTEMPTS
    assert row["last_error"]


def test_enqueue_is_idempotent(conn):
    mid = add_message(conn, "在吗")
    payload = {"user_text": "在吗", "assistant_text": "在", "evidence_ids": [mid]}
    extract.enqueue_extract(conn, "t22", payload)
    extract.enqueue_extract(conn, "t22", payload)
    assert conn.execute(
        "SELECT count(*) c FROM async_task WHERE turn_id='t22'"
    ).fetchone()["c"] == 1


def test_recover_stuck_resets_running(conn):
    """进程被杀时 running 的任务必须回到 pending，否则永远没人捡。"""
    mid = add_message(conn, "在吗")
    extract.enqueue_extract(conn, "t23", {
        "user_text": "在吗", "assistant_text": "在", "evidence_ids": [mid],
    })
    conn.execute("UPDATE async_task SET status='running' WHERE turn_id='t23'")
    assert extract.recover_stuck(conn) == 1
    assert conn.execute(
        "SELECT status FROM async_task WHERE turn_id='t23'"
    ).fetchone()["status"] == "pending"




def test_extraction_skips_when_evidence_deleted(conn):
    """证据被删了就不该再抽取 —— 否则删掉的内容会被重新写成记忆。"""
    from companion.provider import FakeProvider
    from companion.extract import extract_turn

    provider = FakeProvider(["不该被调用"])
    outcome = extract_turn(
        conn, provider,
        user_text="我养了只猫叫团子", assistant_text="记住了",
        evidence_ids=[9999], turn_id="t-deleted",
    )
    assert outcome.ok
    assert outcome.notes.get("skipped") == "evidence deleted"
    assert provider.calls == []          # 连模型调用都省了
    assert conn.execute("SELECT count(*) c FROM memory_items").fetchone()["c"] == 0


def test_same_slot_twice_supersedes_instead_of_crashing(conn):
    """同一槽位写第二次不能撞唯一索引，而是把旧值置为 superseded。"""
    mid = add_message(conn, "我换工作了，现在在 B 公司")
    payload = {
        "memories": [
            {"content": "用户在 A 公司上班。", "epistemic_status": "explicit",
             "source_message_ids": [mid], "slot_key": "employer"},
            {"content": "用户在 B 公司上班。", "epistemic_status": "explicit",
             "source_message_ids": [mid], "slot_key": "employer"},
        ]
    }
    import json as _json
    from companion.provider import FakeProvider

    provider = FakeProvider([_json.dumps(payload, ensure_ascii=False)])
    outcome = extract.extract_turn(
        conn, provider, user_text="我换工作了", assistant_text="嗯",
        evidence_ids=[mid], turn_id="t-slot",
    )
    assert outcome.ok
    rows = conn.execute(
        "SELECT id, content, status, valid_to, superseded_by FROM memory_items ORDER BY id"
    ).fetchall()
    assert len(rows) == 2
    assert rows[0]["status"] == "superseded"
    assert rows[0]["valid_to"] is not None
    assert rows[0]["superseded_by"] == rows[1]["id"]
    assert rows[1]["status"] == "active"
    # 只有新值在召回范围内
    hits = db.search_memories(conn, "公司")
    assert [h[0] for h in hits] == [rows[1]["id"]]


def test_slot_supersede_keeps_fts_clean(conn):
    mid = add_message(conn, "我换工作了")
    import json as _json
    from companion.provider import FakeProvider

    for company in ("A 公司", "B 公司"):
        provider = FakeProvider([_json.dumps(
            {"memories": [{"content": f"用户在 {company} 上班。", "source_message_ids": [mid],
                           "slot_key": "employer"}]}, ensure_ascii=False)])
        extract.extract_turn(conn, provider, user_text="我换工作了", assistant_text="嗯",
                             evidence_ids=[mid], turn_id=f"t-{company}")
    assert db.verify_fts(conn).ok



def test_extraction_persists_event_dates(conn):
    """带时间的事实要写 event_start/end，否则"去年年底"这类查询永远查不到。"""
    import json as _json

    from companion.provider import FakeProvider

    mid = add_message(conn, "去年年底搬的家，搬完一个月才缓过来")
    payload = {"memories": [{
        "content": "用户去年年底搬过一次家。",
        "source_message_ids": [mid],
        "epistemic_status": "explicit",
        "event_start": "2025-12-01",
        "event_end": "2025-12-31",
    }]}
    provider = FakeProvider([_json.dumps(payload, ensure_ascii=False)])
    outcome = extract.extract_turn(
        conn, provider, user_text="去年年底搬的家", assistant_text="嗯",
        evidence_ids=[mid], turn_id="t-date",
    )
    assert outcome.ok
    row = conn.execute(
        "SELECT event_start, event_end FROM memory_items WHERE id=?", (outcome.memory_ids[0],)
    ).fetchone()
    assert row["event_start"] == "2025-12-01"
    assert row["event_end"] == "2025-12-31"


def test_event_dates_are_validated(conn):
    """模型给的日期必须校验：不存在的日期丢掉，end 早于 start 时纠正。"""
    import json as _json

    from companion.provider import FakeProvider

    mid = add_message(conn, "聊了点事")
    payload = {"memories": [
        {"content": "不存在的日期", "source_message_ids": [mid], "event_start": "2026-02-30"},
        {"content": "顺序颠倒", "source_message_ids": [mid],
         "event_start": "2026-05-10", "event_end": "2026-05-01"},
        {"content": "只有起点", "source_message_ids": [mid], "event_start": "2026-03-02"},
        {"content": "只有终点", "source_message_ids": [mid], "event_end": "2026-03-03"},
    ]}
    provider = FakeProvider([_json.dumps(payload, ensure_ascii=False)])
    outcome = extract.extract_turn(
        conn, provider, user_text="聊了点事", assistant_text="嗯",
        evidence_ids=[mid], turn_id="t-date2",
    )
    rows = conn.execute(
        "SELECT content, event_start, event_end FROM memory_items ORDER BY id"
    ).fetchall()
    by_content = {r["content"]: r for r in rows}
    assert by_content["不存在的日期"]["event_start"] is None
    assert (by_content["顺序颠倒"]["event_start"], by_content["顺序颠倒"]["event_end"]) == ("2026-05-01", "2026-05-10")
    assert (by_content["只有起点"]["event_start"], by_content["只有起点"]["event_end"]) == ("2026-03-02", "2026-03-02")
    assert (by_content["只有终点"]["event_start"], by_content["只有终点"]["event_end"]) == ("2026-03-03", "2026-03-03")


def test_date_tier_finds_memory_by_event_window(conn):
    """写进 event_start 之后，tier0 的日期路径应该能命中。"""
    import json as _json

    from companion import recall
    from companion.provider import FakeProvider

    mid = add_message(conn, "去年年底搬的家")
    payload = {"memories": [{
        "content": "用户去年年底搬过一次家。",
        "source_message_ids": [mid],
        "event_start": "2025-12-01", "event_end": "2025-12-31",
    }]}
    extract.extract_turn(
        conn, FakeProvider([_json.dumps(payload, ensure_ascii=False)]),
        user_text="去年年底搬的家", assistant_text="嗯", evidence_ids=[mid], turn_id="t-date3",
    )
    from datetime import datetime

    result = recall.recall(conn, "去年年底我干了什么", reference_time=datetime(2026, 11, 4, 21, 0))
    assert any(h.source == "date_event" for h in result.hits), result.hits



def test_candidate_is_not_active_until_promoted(conn):
    """候选永远不自动生效 —— 这是 §1.2 闸门二。"""
    from companion import lexicon

    conn.execute(
        "INSERT INTO lexicon_candidate (surface, kind, meaning, occurrence_count, "
        "confidence, evidence_message_ids, first_seen, last_seen) "
        "VALUES ('进厂','inside_joke','加班',2,0.8,'[]','2026-11-01','2026-11-02')"
    )
    assert lexicon.list_entries(conn) == []
    assert lexicon.match(conn, "又要进厂了") == ()


def test_promote_makes_the_term_usable(conn):
    from companion import lexicon

    conn.execute(
        "INSERT INTO lexicon_candidate (surface, kind, canonical, meaning, occurrence_count, "
        "confidence, evidence_message_ids, first_seen, last_seen) "
        "VALUES ('搬家','alias','搬了一次家','搬家这件事',3,0.9,'[]','2026-11-01','2026-11-02')"
    )
    cid = conn.execute("SELECT id FROM lexicon_candidate").fetchone()["id"]

    entry_id = lexicon.promote(conn, cid)
    assert entry_id

    matches, query = lexicon.load_for_prompt(conn, "又要搬家了")
    assert any(m.surface == "搬家" for m in matches)
    assert "搬了一次家" in query
    assert conn.execute(
        "SELECT status FROM lexicon_candidate WHERE id=?", (cid,)
    ).fetchone()["status"] == "promoted"


def test_promote_twice_is_rejected(conn):
    from companion import lexicon

    conn.execute(
        "INSERT INTO lexicon_candidate (surface, kind, meaning, confidence, "
        "evidence_message_ids, first_seen, last_seen) "
        "VALUES ('进厂','inside_joke','加班',0.8,'[]','2026-11-01','2026-11-02')"
    )
    cid = conn.execute("SELECT id FROM lexicon_candidate").fetchone()["id"]
    lexicon.promote(conn, cid)
    with pytest.raises(ValueError):
        lexicon.promote(conn, cid)


def test_reject_keeps_it_out_of_pending(conn):
    from companion import lexicon

    conn.execute(
        "INSERT INTO lexicon_candidate (surface, kind, meaning, confidence, "
        "evidence_message_ids, first_seen, last_seen) "
        "VALUES ('摸鱼','inside_joke','偷懒',0.4,'[]','2026-11-01','2026-11-02')"
    )
    cid = conn.execute("SELECT id FROM lexicon_candidate").fetchone()["id"]
    lexicon.reject(conn, cid)
    assert lexicon.list_candidates(conn) == []
    assert conn.execute(
        "SELECT status FROM lexicon_candidate WHERE id=?", (cid,)
    ).fetchone()["status"] == "rejected"


def test_promoting_an_existing_entry_updates_it(conn):
    """同一个 surface+kind 再确认一次，应该更新而不是撞唯一索引。"""
    from companion import lexicon

    for meaning in ("加班", "加班（更累的那种）"):
        conn.execute(
            "INSERT INTO lexicon_candidate (surface, kind, meaning, confidence, "
            "evidence_message_ids, first_seen, last_seen) "
            "VALUES ('进厂','inside_joke',?,0.8,'[]','2026-11-01','2026-11-02') "
            "ON CONFLICT(surface, kind) DO UPDATE SET meaning=excluded.meaning, "
            "status='pending'",
            (meaning,),
        )
        cid = conn.execute("SELECT id FROM lexicon_candidate WHERE surface='进厂'").fetchone()["id"]
        lexicon.promote(conn, cid)

    entries = lexicon.list_entries(conn)
    assert len(entries) == 1
    assert entries[0]["meaning"] == "加班（更累的那种）"
