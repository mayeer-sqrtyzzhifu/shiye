"""一轮对话的端到端编排（M1 的主链路）。

```
文本 → recall（三路）→ 取原文/记忆内容 → assemble（前缀 + 动态段）
     → provider.stream → 落库（消息 + FTS 同步）→ 更新会话态
```

刻意的边界：
* 这里**不做**记忆抽取。抽取是 L4 异步任务，发生在回复发出之后（§2.3），
  它的失败不能影响这一轮。
* 这里**不判定**情绪 / 风险。`risk` 与 `crisis_resources` 由调用方（L0 标志位）
  传入；没有标志位时就是 0，不猜测。
"""

from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Callable

from . import db
from . import lexicon as lex_mod
from . import signals as sig_mod
from .prompt import (
    LexiconLine,
    day_key,
    format_local_moment,
    local_day_bounds,
    PromptBundle,
    RecallLine,
    Turn,
    assemble,
)
from .provider import Completion, Provider
from .recall import RecallResult, recall

RECENT_LIMIT = 8

# 危机语境的内置兜底资源（§5.5：必须离线可用，不能依赖网络）
DEFAULT_CRISIS_RESOURCES = (
    "心理援助热线 12356（全国统一，24 小时）",
    "北京心理危机研究与干预中心 010-82951332",
    "紧急情况请拨 120 / 110",
)


@dataclass(frozen=True)
class TurnResult:
    reply: str
    completion: Completion
    bundle: PromptBundle
    recall: RecallResult
    session_id: str
    turn_index: int
    persisted: bool
    signals: sig_mod.Signals = field(default_factory=sig_mod.Signals)
    notes: dict[str, str] = field(default_factory=dict)


def _materialize_recall(
    conn: sqlite3.Connection, result: RecallResult, tz_name: str | None = None
) -> tuple[RecallLine, ...]:
    """把命中 id 换成可注入的文本。``local_only`` 永远不进这里。

    时间一律经 ``format_local_moment`` 转成本地时区：直接挂 UTC 时间戳会让
    模型把「今天中午」说错（真实踩过）。
    """
    lines: list[RecallLine] = []
    for hit in result.hits:
        if hit.kind == "memory":
            row = conn.execute(
                "SELECT content, event_start, message_time FROM memory_items WHERE id=?",
                (hit.id,),
            ).fetchone()
            if row:
                when = format_local_moment(row["event_start"] or row["message_time"], tz_name)
                lines.append(RecallLine(row["content"], when, "memory"))
        else:
            row = conn.execute(
                "SELECT content, message_time FROM messages WHERE id=?", (hit.id,)
            ).fetchone()
            if row:
                lines.append(
                    RecallLine(row["content"],
                               format_local_moment(row["message_time"], tz_name),
                               "message")
                )
    return tuple(lines)


def _recent_turns(
    conn: sqlite3.Connection,
    start_utc: str,
    end_utc: str,
    limit: int = RECENT_LIMIT,
) -> tuple[Turn, ...]:
    """当天（本地自然日）的近期对话，并排除 no_send（§9.2）。

    会话单位是**天**而不是 session_id：同一天里刷新过几次页面、旧数据带着
    随机 session_id，都应该被当成同一段对话。
    """
    rows = conn.execute(
        "SELECT role, content FROM messages "
        "WHERE role IN ('user','assistant') AND no_send = 0 "
        "  AND message_time >= ? AND message_time < ? "
        "ORDER BY id DESC LIMIT ?",
        (start_utc, end_utc, limit),
    ).fetchall()
    return tuple(Turn(r["role"], r["content"]) for r in reversed(rows))


def _load_session(conn: sqlite3.Connection, session_id: str) -> dict:
    row = conn.execute("SELECT * FROM session_state WHERE session_id=?", (session_id,)).fetchone()
    if row is None:
        return {"turn_index": 0}
    try:
        affect = json.loads(row["affect_json"]) if row["affect_json"] else None
    except ValueError:
        affect = None
    return {
        "turn_index": row["turn_index"],
        "affect": (affect or {}).get("label"),
        "topic": row["topic_id"],
        "locked_memory_ids": row["locked_memory_ids"],
        "last_recall_ids": row["last_recall_ids"],
    }


def _save_session(
    conn: sqlite3.Connection,
    session_id: str,
    *,
    turn_index: int,
    recall_ids: list[int],
    lexicon_ids: list[int],
) -> None:
    now = db.utc_now()
    conn.execute(
        "INSERT INTO session_state (session_id, turn_index, last_recall_ids, injected_lexicon_ids, updated_at) "
        "VALUES (?, ?, ?, ?, ?) "
        "ON CONFLICT(session_id) DO UPDATE SET "
        "turn_index=excluded.turn_index, last_recall_ids=excluded.last_recall_ids, "
        "injected_lexicon_ids=excluded.injected_lexicon_ids, updated_at=excluded.updated_at",
        (session_id, turn_index, json.dumps(recall_ids), json.dumps(lexicon_ids), now),
    )


def run_turn(
    conn: sqlite3.Connection,
    provider: Provider,
    text: str,
    *,
    persona: str,
    session_id: str | None = None,
    now: datetime | None = None,
    reference_time: str | None = None,
    boundaries: tuple[str, ...] = (),
    taboo: tuple[str, ...] = (),
    capabilities: tuple[str, ...] = (),
    lexicon: tuple[LexiconLine, ...] = (),
    risk: int | None = None,
    crisis_resources: tuple[str, ...] = (),
    gap_hint: str | None = None,
    max_tokens: int = 800,
    on_text: Callable[[str], None] | None = None,
    persist: bool = True,
) -> TurnResult:
    """Run one complete turn and (by default) persist it.

    ``risk=None`` 时由 L0 规则自己判定；显式传入则覆盖（便于测试与手工降级）。
    """
    now = now or datetime.now(timezone.utc)
    # 存储一律用 UTC；展示由 prompt 层按用户时区转换（naive 输入按本机本地时间理解）
    aware = now.astimezone() if now.tzinfo is None else now
    stamp = reference_time or aware.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")

    signals = sig_mod.scan(text)
    effective_risk = signals.risk if risk is None else risk
    # 危机语境下必须有资源可给；调用方没提供时用内置兜底（§5.5 离线可用）
    resources = crisis_resources
    if effective_risk >= 2 and not resources:
        resources = DEFAULT_CRISIS_RESOURCES

    profile = db.get_profile(conn)
    tz_name = profile.get("timezone")
    # 会话单位是"天"：session_id 默认就是当天，刷新/重开都回到同一段
    session_id = session_id or f"day:{day_key(now, tz_name)}"
    day_start, day_end = local_day_bounds(now, tz_name)
    session = _load_session(conn, session_id)

    recalled = recall(conn, text, reference_time=reference_time, profile=profile)
    recall_lines = _materialize_recall(conn, recalled, tz_name)
    turns = _recent_turns(conn, day_start, day_end)

    # 命中的语汇进动态段；调用方显式传入时以调用方为准（便于测试/手工注入）
    matched_lexicon = tuple(
        LexiconLine(surface=m.surface, meaning=m.meaning, kind=m.kind,
                    usage_note=m.usage_note)
        for m in recalled.lexicon
    )
    lexicon_lines = lexicon or matched_lexicon

    bundle = assemble(
        persona=persona,
        now=now,
        user_message=text,
        boundaries=boundaries,
        profile=profile,
        taboo=taboo,
        capabilities=capabilities,
        recent=turns,
        recall=recall_lines,
        lexicon=lexicon_lines,
        session={**session, "timezone": profile.get("timezone")},
        risk=effective_risk,
        crisis_resources=resources,
        gap_hint=gap_hint,
    )

    messages = [{"role": t.role, "content": t.content} for t in bundle.recent]
    messages.append({"role": "user", "content": f"<turn_context>\n{bundle.dynamic_block}\n</turn_context>"})
    messages.append({"role": "user", "content": text})

    completion = provider.stream(
        bundle.system_prefix,
        messages,
        on_text=on_text,
        max_tokens=max_tokens,
    )

    notes: dict[str, str] = dict(bundle.notes)
    if completion.finish_reason == "length":
        notes["finish_length"] = "达到 max_tokens，正文可能被截断（reasoning 也计入）"
    if not completion.text.strip():
        notes["empty_reply"] = "模型只产出 reasoning，正文为空"

    persisted = False
    turn_index = session.get("turn_index", 0)
    if persist and completion.text.strip():
        with db.tx(conn):
            for role, content in (("user", text), ("assistant", completion.text)):
                cur = conn.execute(
                    "INSERT INTO messages (role, content, message_time, created_at, session_id) "
                    "VALUES (?, ?, ?, ?, ?)",
                    (role, content, stamp, db.utc_now(), session_id),
                )
                db.upsert_message_fts(conn, cur.lastrowid, content)
            turn_index += 1
            _save_session(
                conn,
                session_id,
                turn_index=turn_index,
                recall_ids=[h.id for h in recalled.hits if h.kind == "memory"],
                lexicon_ids=[m.id for m in recalled.lexicon],
            )
            # 记账是"被注入"，不是"被模型采用"（§1.2 闸门四）
            lex_mod.note_use(conn, [m.id for m in recalled.lexicon], injected=True)
        persisted = True

    return TurnResult(
        reply=completion.text,
        completion=completion,
        bundle=bundle,
        recall=recalled,
        session_id=session_id,
        turn_index=turn_index,
        persisted=persisted,
        signals=signals,
        notes=notes,
    )



