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

from . import db, lexicon
from .datewin import overlaps_sql
from .timequery import CONF_MID, TimeQuery, resolve

TIER0_LIMIT = 2

# 相关性下限。**用相对比例，不用绝对分。**
#
# 最初标定的是绝对阈值（τ=2.5），在 34 篇语料上精度 0.88 / 召回 1.00。
# 但搬到评估集（只有几篇文档）就废了：BM25 的分数依赖语料规模和 IDF，
# 同一对文本在不同规模的库里分差可以到几倍。绝对阈值等于把语料规模
# 焊进了业务逻辑。
#
# 改成"保留最高分的 KEEP_RATIO 以上"：与语料规模无关，只表达
# "这条命中的强度够不够格跟最强的候选并列"。标定见 tools/calibrate_tau.py。
KEEP_RATIO_MEMORY = 0.5
KEEP_RATIO_MESSAGE = 0.5
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
    lexicon: tuple[lexicon.LexiconMatch, ...] = ()
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


def _local_range_utc(start_day: str, end_day: str, tz_name: str | None) -> tuple[str, str]:
    """本地日期区间 → UTC 区间（左闭右开）。

    直接从 "2026-11-04" 拼 "T00:00:00Z" 会把本地日界当成 UTC 日界，
    对 UTC+8 的用户来说，"今天"会错开 8 小时。
    """
    from datetime import datetime, timedelta, timezone

    from .prompt import _localize

    start = _localize(datetime.strptime(start_day, "%Y-%m-%d"), tz_name)
    end = _localize(datetime.strptime(end_day, "%Y-%m-%d"), tz_name) + timedelta(days=1)
    fmt = "%Y-%m-%dT%H:%M:%SZ"
    return (
        start.astimezone(timezone.utc).strftime(fmt),
        end.astimezone(timezone.utc).strftime(fmt),
    )


def _message_hits(
    conn: sqlite3.Connection, q: TimeQuery, tz_name: str | None = None
) -> list[Hit]:
    """消息轴：「昨天我们聊了什么」查的是说话时间，不是事件时间。"""
    if q.conf < CONF_MID or q.axis not in ("message", "ambiguous") or not q.windows:
        return []
    hits: list[Hit] = []
    seen: set[int] = set()
    for window in q.windows:
        rows = conn.execute(
            """
            SELECT id, no_send FROM messages
            WHERE message_time >= ? AND message_time < ?
            ORDER BY message_time DESC
            """,
            _local_range_utc(window.start, window.end, tz_name),
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

def _keep_strong(hits: list[tuple[int, float]], ratio: float) -> list[tuple[int, float]]:
    """只保留接近最强命中的那些。

    相对阈值而不是绝对分：BM25 的绝对值随语料规模浮动，写死会把
    "语料多大"焊进业务逻辑。ratio=0 表示不过滤。
    """
    if not hits:
        return []
    best = max(score for _, score in hits)
    if ratio <= 0 or best <= 0:
        return list(hits)
    return [(i, s) for i, s in hits if s >= best * ratio]


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
    keep_ratio_memory: float = KEEP_RATIO_MEMORY,
    keep_ratio_message: float = KEEP_RATIO_MESSAGE,
) -> RecallResult:
    """Run the three candidate paths and fuse them under the §3.5 quotas."""
    if profile is None:
        profile = db.get_profile(conn)
    query = resolve(text, reference_time=reference_time, profile=profile)

    # 语汇命中 + alias 归一：扩展**检索**用的文本，时间解析仍用原句
    lexicon_matches, query_text = lexicon.load_for_prompt(conn, text)

    tier0: list[Hit] = []
    tier0.extend(_event_hits(conn, query))
    if len(tier0) < TIER0_LIMIT:
        tier0.extend(
            _message_hits(conn, query, profile.get("timezone"))[: TIER0_LIMIT - len(tier0)]
        )

    # no_send 对所有出网路径一视同仁：tier0 命中的消息也必须离开 hits（§9.2）
    tier0_local = [h for h in tier0 if h.no_send]
    tier0 = [h for h in tier0 if not h.no_send]

    taken_memory = {h.id for h in tier0 if h.kind == "memory"}
    taken_message = {h.id for h in tier0 if h.kind == "message"}

    memory_raw = [
        (i, s)
        for i, s in db.search_memories(
            conn, query_text, min_relevance=0.0,
            limit=TEXT_LIMIT_MEMORY + len(taken_memory),
        )
        if i not in taken_memory
    ]
    memory_text = [
        Hit("memory", i, 1, s, "fts_memory")
        for i, s in _keep_strong(memory_raw, keep_ratio_memory)
    ][:TEXT_LIMIT_MEMORY]

    message_raw = [
        (i, s)
        for i, s in db.search_messages(
            conn, query_text, min_relevance=0.0,
            limit=TEXT_LIMIT_MESSAGE + len(taken_message),
        )
        if i not in taken_message
    ]
    message_scored = _keep_strong(message_raw, keep_ratio_message)[:TEXT_LIMIT_MESSAGE]
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
        lexicon=lexicon_matches,
        notes={"tier0": len(tier0), "memory": min(memory_slots, len(memory_text)),
               "message": message_slots, "local_only": len(local_only),
               "lexicon": len(lexicon_matches)},
    )





