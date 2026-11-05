"""L4 异步抽取（§2.3）。

只做一件事：把一轮对话变成**带证据**的记忆与语汇候选。

四条硬规则（都来自 §2.3 的证据契约）：

1. **助手输出不能独立作为用户事实的证据。** 抽取可以读它来理解上下文，
   但 `source_message_ids` 必须落在**用户消息**上，否则整条丢弃。
2. **推断保留推断身份。** `epistemic_status=inferred` 的条目不进稳定前缀。
3. **模型输出是数据不是指令。** 解析失败、字段缺失、证据不在批次里，
   一律拒绝整条，不做"尽力修补"。
4. **幂等。** 同一个 `turn_id` 重复执行不会写出两份记忆。
"""

from __future__ import annotations

import json
import re
import sqlite3
from dataclasses import dataclass, field

from . import db
from .provider import Provider

MAX_ATTEMPTS = 3
EPISTEMIC = ("explicit", "confirmed", "inferred")
KINDS = ("alias", "inside_joke", "nick_name", "taboo")

PROMPT = """你在做记忆抽取。只输出一个 JSON 对象，不要解释、不要代码围栏。

从这一轮对话中抽取**关于用户本人的、长期有用的事实**，以及用户特有的语汇。

规则：
- 只记录用户自己说过的或明确确认过的内容；助手猜测的内容不得写成事实。
- source_message_ids 只能填用户消息的 id，至少一个。
- epistemic_status: explicit（用户直说）/ confirmed（用户确认了助手的说法）/ inferred（你的推断）
- 没有可记的就返回空数组。
- 语汇只在用户给出**特定说法**时记录；kind 只能是 alias / inside_joke / nick_name / taboo。

输出格式：
{"memories":[{"content":"...","source_message_ids":[1],"epistemic_status":"explicit",
"slot_key":null,"valid_from":null}],
"lexicon":[{"surface":"...","canonical":null,"kind":"inside_joke","meaning":"...",
"usage_scene":null,"usage_note":null,"confidence":0.8}]}
"""


@dataclass(frozen=True)
class Outcome:
    memory_ids: tuple[int, ...] = ()
    lexicon_ids: tuple[int, ...] = ()
    rejected: tuple[str, ...] = ()
    ok: bool = True
    raw: str = ""
    notes: dict[str, str] = field(default_factory=dict)


def _strip_fences(text: str) -> str:
    text = text.strip()
    text = re.sub(r"^```[a-zA-Z]*\s*", "", text)
    text = re.sub(r"\s*```$", "", text)
    return text.strip()


def parse_payload(raw: str) -> tuple[dict, str | None]:
    body = _strip_fences(raw)
    start, end = body.find("{"), body.rfind("}")
    if start == -1 or end <= start:
        return {}, "no json object"
    try:
        payload = json.loads(body[start : end + 1])
    except json.JSONDecodeError as exc:
        return {}, f"json error: {exc.msg}"
    if not isinstance(payload, dict):
        return {}, "json root is not an object"
    return payload, None


def _clean(text: str | None, limit: int = 400) -> str:
    return " ".join((text or "").split())[:limit]


def extract_turn(
    conn: sqlite3.Connection,
    provider: Provider,
    *,
    user_text: str,
    assistant_text: str,
    evidence_ids: list[int],
    turn_id: str | None = None,
    max_tokens: int = 700,
) -> Outcome:
    """抽取一轮。``evidence_ids`` 必须是本轮**用户消息**的 id。"""
    if not user_text.strip():
        return Outcome(ok=False, notes={"reason": "empty user text"})

    # 幂等：同一 turn 已经抽取过就直接返回
    if turn_id and conn.execute(
        "SELECT 1 FROM memory_items WHERE source LIKE ? LIMIT 1", (f"turn:{turn_id}",)
    ).fetchone():
        return Outcome(ok=True, notes={"skipped": "already extracted"})

    # 证据还在吗？用户可能在抽取跑完之前就把这几条消息删了。
    # 不查这一步的话，删掉的内容会被抽取器重新写成一条记忆，等于删除失效。
    wanted = {int(i) for i in evidence_ids}
    if wanted:
        marks = ", ".join("?" * len(wanted))
        alive = {
            row["id"]
            for row in conn.execute(
                f"SELECT id FROM messages WHERE id IN ({marks})", sorted(wanted)
            )
        }
        if alive != wanted:
            return Outcome(
                ok=True,
                notes={"skipped": "evidence deleted", "missing": sorted(wanted - alive)},
            )

    messages = [
        {"role": "user", "content": PROMPT},
        {
            "role": "user",
            "content": (
                f"用户消息 id: {sorted(evidence_ids)}\n"
                f"用户: {user_text}\n助手: {assistant_text}"
            ),
        },
    ]
    completion = provider.stream("你是抽取器。", messages, max_tokens=max_tokens, temperature=0)
    payload, error = parse_payload(completion.text)
    if error:
        return Outcome(ok=False, raw=completion.text, notes={"parse": error})

    allowed = set(evidence_ids)
    rejected: list[str] = []
    memory_ids: list[int] = []
    now = db.utc_now()
    source = f"turn:{turn_id}" if turn_id else "extract"

    with db.tx(conn):
        for item in payload.get("memories") or []:
            if not isinstance(item, dict):
                rejected.append("memory: not an object")
                continue
            content = _clean(item.get("content"))
            status = item.get("epistemic_status") or "explicit"
            cited = {int(x) for x in (item.get("source_message_ids") or []) if str(x).isdigit()}

            if not content:
                rejected.append("memory: empty content")
                continue
            if status not in EPISTEMIC:
                rejected.append(f"memory: bad status {status!r}")
                continue
            if not cited:
                rejected.append(f"memory: no evidence ({content[:20]})")
                continue
            if not cited <= allowed:
                # 引用了助手消息或不存在的 id —— 这正是"把自己的猜测写成用户事实"
                rejected.append(f"memory: evidence outside user turn ({content[:20]})")
                continue

            slot_key = _clean(item.get("slot_key"), 40) or None
            if slot_key:
                # 同槽位旧值必须先失效，否则唯一索引 ux_memory_slot_current
                # 在写第二条同槽位事实时直接抛约束异常
                conn.execute(
                    "UPDATE memory_items SET status='superseded', valid_to=?, updated_at=? "
                    "WHERE slot_key=? AND status='active' AND valid_to IS NULL",
                    (now, now, slot_key),
                )
            cur = conn.execute(
                "INSERT INTO memory_items "
                "(content, source, source_role, epistemic_status, evidence_message_ids, "
                " slot_key, valid_from, message_time, created_at, updated_at) "
                "VALUES (?,?,?,?,?,?,?,?,?,?)",
                (
                    content,
                    source,
                    "user",
                    status,
                    json.dumps(sorted(cited)),
                    slot_key,
                    _clean(item.get("valid_from"), 20) or None,
                    now,
                    now,
                    now,
                ),
            )
            db.upsert_memory_fts(conn, cur.lastrowid, content)
            if slot_key:
                conn.execute(
                    "UPDATE memory_items SET superseded_by=? WHERE slot_key=? "
                    "AND status='superseded' AND superseded_by IS NULL AND valid_to=?",
                    (cur.lastrowid, slot_key, now),
                )
            memory_ids.append(cur.lastrowid)
        lexicon_ids: list[int] = []
        for item in payload.get("lexicon") or []:
            if not isinstance(item, dict):
                rejected.append("lexicon: not an object")
                continue
            surface = _clean(item.get("surface"), 40)
            kind = item.get("kind")
            meaning = _clean(item.get("meaning"), 200)
            if not surface or kind not in KINDS or not meaning:
                rejected.append(f"lexicon: invalid ({surface!r}/{kind!r})")
                continue
            if kind == "taboo":
                # taboo 常驻稳定前缀，必须人工确认，不能自动写（§1.2 闸门二）
                rejected.append(f"lexicon: taboo needs confirmation ({surface})")
                continue

            scope = json.dumps(sorted(allowed))
            try:
                conn.execute(
                    "INSERT INTO lexicon_candidate "
                    "(surface, kind, canonical, meaning, usage_scene, usage_note, "
                    " occurrence_count, confidence, evidence_message_ids, first_seen, last_seen) "
                    "VALUES (?,?,?,?,?,?,1,?,?,?,?) "
                    "ON CONFLICT(surface, kind) DO UPDATE SET "
                    "occurrence_count = occurrence_count + 1, last_seen = excluded.last_seen, "
                    "confidence = MAX(confidence, excluded.confidence)",
                    (
                        surface, kind, _clean(item.get("canonical"), 40) or None, meaning,
                        _clean(item.get("usage_scene"), 80) or None,
                        _clean(item.get("usage_note"), 80) or None,
                        float(item.get("confidence") or 0.5), scope, now, now,
                    ),
                )
                row = conn.execute(
                    "SELECT id FROM lexicon_candidate WHERE surface=? AND kind=?", (surface, kind)
                ).fetchone()
                lexicon_ids.append(row["id"])
            except Exception as exc:  # noqa: BLE001
                rejected.append(f"lexicon: write failed {exc}")

    return Outcome(
        memory_ids=tuple(memory_ids),
        lexicon_ids=tuple(lexicon_ids),
        rejected=tuple(rejected),
        ok=True,
        raw=completion.text,
    )


# ───────────────────────────────────────── 任务队列

def enqueue_extract(conn: sqlite3.Connection, turn_id: str, payload: dict) -> str:
    """把抽取排进 async_task；同一 turn 只排一次（idempotency_key 唯一）。"""
    task_id = f"extract:{turn_id}"
    conn.execute(
        "INSERT OR IGNORE INTO async_task "
        "(task_id, turn_id, kind, status, payload_ref, idempotency_key, created_at) "
        "VALUES (?, ?, 'extract', 'pending', ?, ?, ?)",
        (task_id, turn_id, json.dumps(payload, ensure_ascii=False), f"{turn_id}:extract",
         db.utc_now()),
    )
    return task_id


def recover_stuck(conn: sqlite3.Connection) -> int:
    """进程重启时把"跑到一半"的任务放回 pending。

    不做这一步，进程被杀时正在跑的任务会永远停在 running，再也没有 worker 认领。
    """
    cur = conn.execute(
        "UPDATE async_task SET status='pending' WHERE kind='extract' AND status='running'"
    )
    return cur.rowcount


def _mark_failed(conn: sqlite3.Connection, task_id: str, retry_count: int, error: str) -> None:
    attempts = retry_count + 1
    status = "pending" if attempts < MAX_ATTEMPTS else "failed"
    conn.execute(
        "UPDATE async_task SET status=?, retry_count=?, last_error=? WHERE task_id=?",
        (status, attempts, error[:500], task_id),
    )


def run_queue(
    conn: sqlite3.Connection, provider: Provider, *, limit: int = 5
) -> list[Outcome]:
    """跑一批待处理任务，**每个任务每轮只试一次**。

    和旧 run_pending 的区别：
    - 认领先把整批置为 running，再逐个跑：失败回到 pending 后不会被同一轮
      立刻重新捡起来（否则一个坏任务会把重试次数一次烧光，还饿死后面的任务）
    - 失败有上限（MAX_ATTEMPTS 之后停在 failed，而不是无限重试）
    - 单任务一个事务：一条写失败不会污染同批其它任务
    """
    with db.tx(conn):
        rows = conn.execute(
            "SELECT task_id, turn_id, payload_ref, retry_count FROM async_task "
            "WHERE kind='extract' AND status='pending' ORDER BY created_at, task_id LIMIT ?",
            (limit,),
        ).fetchall()
        for row in rows:
            conn.execute(
                "UPDATE async_task SET status='running' WHERE task_id=?", (row["task_id"],)
            )

    results: list[Outcome] = []
    for row in rows:
        payload = json.loads(row["payload_ref"] or "{}")
        try:
            outcome = extract_turn(
                conn, provider,
                user_text=payload.get("user_text", ""),
                assistant_text=payload.get("assistant_text", ""),
                evidence_ids=payload.get("evidence_ids", []),
                turn_id=row["turn_id"] or payload.get("turn_id"),
            )
        except Exception as exc:  # noqa: BLE001
            with db.tx(conn):
                _mark_failed(conn, row["task_id"], row["retry_count"], f"{type(exc).__name__}: {exc}")
            results.append(Outcome(ok=False, notes={"exception": str(exc)[:200]}))
            continue

        with db.tx(conn):
            if outcome.ok:
                conn.execute(
                    "UPDATE async_task SET status='done', last_error=NULL WHERE task_id=?",
                    (row["task_id"],),
                )
            else:
                _mark_failed(
                    conn, row["task_id"], row["retry_count"],
                    str(outcome.notes.get("parse") or outcome.notes),
                )
        results.append(outcome)
    return results


# 兼容旧名字（评估与测试里有用到）
def run_pending(conn: sqlite3.Connection, provider: Provider, *, limit: int = 5) -> list[Outcome]:
    return run_queue(conn, provider, limit=limit)

