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
from .prompt import (
    LexiconLine,
    PromptBundle,
    RecallLine,
    Turn,
    assemble,
)
from .provider import Completion, Provider
from .recall import RecallResult, recall

RECENT_LIMIT = 8


@dataclass(frozen=True)
class TurnResult:
    reply: str
    completion: Completion
    bundle: PromptBundle
    recall: RecallResult
    session_id: str
    turn_index: int
    persisted: bool
    notes: dict[str, str] = field(default_factory=dict)


def _materialize_recall(conn: sqlite3.Connection, result: RecallResult) -> tuple[RecallLine, ...]:
    """把命中 id 换成可注入的文本。``local_only`` 永远不进这里。"""
    lines: list[RecallLine] = []
    for hit in result.hits:
        if hit.kind == "memory":
            row = conn.execute(
                "SELECT content, event_start, message_time FROM memory_items WHERE id=?",
                (hit.id,),
            ).fetchone()
            if row:
                lines.append(RecallLine(row["content"], row["event_start"] or row["message_time"], "memory"))
        else:
            row = conn.execute(
                "SELECT content, message_time FROM messages WHERE id=?", (hit.id,)
            ).fetchone()
            if row:
                lines.append(RecallLine(row["content"], row["message_time"], "message"))
    return tuple(lines)


def _recent_turns(conn: sqlite3.Connection, limit: int = RECENT_LIMIT) -> tuple[Turn, ...]:
    """近期对话。**必须排除 no_send**：它连"历史上下文"这条路径也不许走（§9.2）。"""
    rows = conn.execute(
        "SELECT role, content FROM messages "
        "WHERE role IN ('user','assistant') AND no_send = 0 "
        "ORDER BY id DESC LIMIT ?",
        (limit,),
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
    session_id: str = "default",
    now: datetime | None = None,
    reference_time: str | None = None,
    boundaries: tuple[str, ...] = (),
    taboo: tuple[str, ...] = (),
    capabilities: tuple[str, ...] = (),
    lexicon: tuple[LexiconLine, ...] = (),
    risk: int = 0,
    crisis_resources: tuple[str, ...] = (),
    gap_hint: str | None = None,
    max_tokens: int = 800,
    on_text: Callable[[str], None] | None = None,
    persist: bool = True,
) -> TurnResult:
    """Run one complete turn and (by default) persist it."""
    now = now or datetime.now(timezone.utc)
    stamp = reference_time or now.strftime("%Y-%m-%dT%H:%M:%SZ")

    profile = db.get_profile(conn)
    session = _load_session(conn, session_id)

    recalled = recall(conn, text, reference_time=reference_time, profile=profile)
    recall_lines = _materialize_recall(conn, recalled)
    turns = _recent_turns(conn)

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
        risk=risk,
        crisis_resources=crisis_resources,
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
                    "INSERT INTO messages (role, content, message_time, created_at) "
                    "VALUES (?, ?, ?, ?)",
                    (role, content, stamp, db.utc_now()),
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
        notes=notes,
    )
