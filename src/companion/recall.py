"""L1 检索层：三路候选 + 分层排序（§3.5）。

三路
----
* **tier 0 结构化日期** —— 事件轴查 ``memory_items.event_start`` 区间相交，
  消息轴查 ``messages.message_time``。**不参与 bm25 门限**：纯日期查询与
  记忆文本往往零词面重叠，bm25≈0，若也被 τ 砍掉，"置顶"就形同虚设。
* **tier 1 文本** —— 记忆 FTS 与原文 FTS，各自过自己的 τ，按 relevance 排序。

配额
----
``tier0 ≤ 2 / 记忆文本 ≤ 3 / 原文 ≤ 1``，融合后总量 ≤5。原文的 1 个位置
**先预留**，否则 tier0 和记忆文本会把 5 个位置吃光，原文路永远命中不到
（这正是"各路 2/3/1 相加是 6"那个问题的正确解法：不是简单截到 5）。

``no_send`` 的消息不进 ``hits``，只进 ``local_only``（§9.2）。
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass, field

from . import db
from .datewin import overlaps_sql
from .timequery import CONF_MID, TimeQuery, resolve

TIER0_LIMIT = 2
TEXT_LIMIT_MEMORY = 3
TEXT_LIMIT_MESSAGE = 1
TOTAL_LIMIT = 5


@dataclass(frozen=True)
class Hit:
    kind: str       # memory | message
    id: int
    tier: int       # 0 = 结构化日期，1 = 文本
    score: float
    source: str     # date_event | date_message | fts_memory | fts_message
    no_send: bool = False


@dataclass(frozen=True)
class RecallResult:
    query: TimeQuery
    hits: tuple[Hit, ...] = ()
    local_only: tuple[Hit, ...] = ()
    notes: dict[str, int] = field(default_factory=dict)


# ───────────────────────────────────────── tier 0

def _event_hits(conn: sqlite3.Connection, q: TimeQuery) -> list[Hit]:
    if q.conf < CONF_MID or q.axis not in ("event", "ambiguous") or not q.windows:
        return []
    hits: list[Hit] = []
    seen: set[int] = set()
    for window in q.windows:            # exact / candidate 在 expanded 之前
        rows = conn.execute(
            f"""
            SELECT id FROM memory_items
            WHERE status = 'active' AND superseded_by IS NULL
              AND {overlaps_sql()}
            ORDER BY event_start DESC
            """,
            {"q_start": window.start, "q_end": window.end},
        ).fetchall()
        for row in rows:
            if row["id"] in seen:
                continue
            seen.add(row["id"])
            hits.append(Hit("memory", row["id"], 0, window.conf, "date_event"))
            if len(hits) >= TIER0_LIMIT:
                return hits
    return hits


def _message_hits(conn: sqlite3.Connection, q: TimeQuery) -> list[Hit]:
    """消息轴：「昨天我们聊了什么」查的是说话时间，不是事件时间。"""
    if q.conf < CONF_MID or q.axis not in ("message", "ambiguous") or not q.windows:
        return []
    hits: list[Hit] = []
    seen: set[int] = set()
    for window in q.windows:
        rows = conn.execute(
            """
            SELECT id, no_send FROM messages
            WHERE message_time >= ? AND message_time <= ?
            ORDER BY message_time DESC
            """,
            (f"{window.start}T00:00:00Z", f"{window.end}T23:59:59Z"),
        ).fetchall()
        for row in rows:
            if row["id"] in seen:
                continue
            seen.add(row["id"])
            hits.append(
                Hit("message", row["id"], 0, window.conf, "date_message",
                    bool(row["no_send"]))
            )
            if len(hits) >= TIER0_LIMIT:
                return hits
    return hits


# ───────────────────────────────────────── 组装

def _no_send_map(conn: sqlite3.Connection, ids: list[int]) -> dict[int, bool]:
    if not ids:
        return {}
    marks = ", ".join("?" * len(ids))
    rows = conn.execute(
        f"SELECT id, no_send FROM messages WHERE id IN ({marks})", ids
    ).fetchall()
    return {r["id"]: bool(r["no_send"]) for r in rows}


def recall(
    conn: sqlite3.Connection,
    text: str,
    *,
    reference_time=None,
    profile: dict[str, str] | None = None,
    tau_memory: float = 0.0,
    tau_message: float = 0.0,
) -> RecallResult:
    """Run the three candidate paths and fuse them under the §3.5 quotas."""
    if profile is None:
        profile = db.get_profile(conn)
    query = resolve(text, reference_time=reference_time, profile=profile)

    tier0: list[Hit] = []
    tier0.extend(_event_hits(conn, query))
    if len(tier0) < TIER0_LIMIT:
        tier0.extend(_message_hits(conn, query)[: TIER0_LIMIT - len(tier0)])

    # no_send 对所有出网路径一视同仁：tier0 命中的消息也必须离开 hits（§9.2）
    tier0_local = [h for h in tier0 if h.no_send]
    tier0 = [h for h in tier0 if not h.no_send]

    taken_memory = {h.id for h in tier0 if h.kind == "memory"}
    taken_message = {h.id for h in tier0 if h.kind == "message"}

    memory_text = [
        Hit("memory", i, 1, s, "fts_memory")
        for i, s in db.search_memories(
            conn, text, min_relevance=tau_memory, limit=TEXT_LIMIT_MEMORY + len(taken_memory)
        )
        if i not in taken_memory
    ][:TEXT_LIMIT_MEMORY]

    message_scored = [
        (i, s)
        for i, s in db.search_messages(
            conn, text, min_relevance=tau_message,
            limit=TEXT_LIMIT_MESSAGE + len(taken_message),
        )
        if i not in taken_message
    ][:TEXT_LIMIT_MESSAGE]
    flags = _no_send_map(conn, [i for i, _ in message_scored])
    message_text = [
        Hit("message", i, 1, s, "fts_message", flags.get(i, False))
        for i, s in message_scored
    ]

    # 原文的 1 个位置先预留，避免被 tier0 与记忆文本挤掉
    injectable_message = [h for h in message_text if not h.no_send]
    local_only = tuple(tier0_local + [h for h in message_text if h.no_send])

    message_slots = min(TEXT_LIMIT_MESSAGE, len(injectable_message))
    memory_slots = min(TEXT_LIMIT_MEMORY, TOTAL_LIMIT - len(tier0) - message_slots)

    hits = tuple(tier0 + memory_text[:memory_slots] + injectable_message[:message_slots])
    return RecallResult(
        query=query,
        hits=hits[:TOTAL_LIMIT],
        local_only=local_only,
        notes={"tier0": len(tier0), "memory": min(memory_slots, len(memory_text)),
               "message": message_slots, "local_only": len(local_only)},
    )
