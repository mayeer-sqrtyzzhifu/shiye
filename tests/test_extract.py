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


def test_run_pending_keeps_failure_for_retry(conn):
    mid = add_message(conn, "在吗")
    extract.enqueue_extract(conn, "t21", {
        "user_text": "在吗", "assistant_text": "在", "evidence_ids": [mid], "turn_id": "t21",
    })
    extract.run_pending(conn, provider_replying("坏掉的输出"))
    row = conn.execute("SELECT status, retry_count FROM async_task WHERE turn_id='t21'").fetchone()
    assert row["status"] == "failed"
    assert row["retry_count"] == 1




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
