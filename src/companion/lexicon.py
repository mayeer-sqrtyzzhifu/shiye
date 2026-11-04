"""关系语汇的**消费方**（§1.2）。

表已经存在很久了，但一直没人用它。这里补三件事：

1. **命中**：本轮文本里出现了哪个 `surface`（词面子串匹配，O(n) 够用）；
2. **查询归一**：`alias` 的 `canonical` 追加到检索 query 里，补上「搬家」
   匹配不到「搬了一次家」这类非连续词素的漏召回；
3. **注入**：命中的条目连同 `usage_note` 一起进 per-turn 动态段。

`taboo` 不进这里 —— 它属于稳定前缀（§6.2），由 `build_system_prefix` 处理。
"""

from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass


@dataclass(frozen=True)
class LexiconMatch:
    id: int
    surface: str
    kind: str
    canonical: str | None
    meaning: str
    usage_note: str | None


def match(
    conn: sqlite3.Connection,
    text: str,
    *,
    user_id: int = 1,
    limit: int = 4,
) -> tuple[LexiconMatch, ...]:
    """本轮文本命中的有效语汇（不含 taboo）。"""
    if not text:
        return ()
    rows = conn.execute(
        "SELECT id, surface, kind, canonical, meaning, usage_note FROM lexicon_entry "
        "WHERE status='active' AND superseded_by IS NULL AND kind != 'taboo' "
        "ORDER BY use_count DESC, id"
    ).fetchall()

    hits = [r for r in rows if r["surface"] and r["surface"] in text]
    # 长词优先：避免「加班」被更短的「班」抢先
    hits.sort(key=lambda r: -len(r["surface"]))
    return tuple(
        LexiconMatch(
            id=r["id"],
            surface=r["surface"],
            kind=r["kind"],
            canonical=r["canonical"],
            meaning=r["meaning"],
            usage_note=r["usage_note"],
        )
        for r in hits[:limit]
    )


def expand_query(text: str, matches: tuple[LexiconMatch, ...]) -> str:
    """把 alias 的归一目标追加到查询里，其余类型不动检索。

    只扩 alias：inside_joke / nick_name 是"说话方式"，不该改变检索语义。
    """
    extras = [m.canonical for m in matches if m.kind == "alias" and m.canonical]
    if not extras:
        return text
    return text + " " + " ".join(dict.fromkeys(extras))


def note_use(conn: sqlite3.Connection, ids: list[int], *, injected: bool) -> None:
    """记账：``use_count`` 是"被注入"，不是"被模型采用"（§1.2 闸门四）。"""
    if not ids:
        return
    marks = ", ".join("?" * len(ids))
    column = "injected_count" if injected else "use_count"
    conn.execute(
        f"UPDATE lexicon_entry SET {column} = {column} + 1, last_used = ? WHERE id IN ({marks})",
        [db_now(), *ids],
    )


def db_now() -> str:
    from .db import utc_now

    return utc_now()


def load_for_prompt(
    conn: sqlite3.Connection,
    text: str,
    *,
    user_id: int = 1,
) -> tuple[tuple[LexiconMatch, ...], str]:
    """返回（命中条目，扩展后的检索 query）。`recall` 用这一个入口。"""
    matches = match(conn, text, user_id=user_id)
    return matches, expand_query(text, matches)


def evidence_ids(conn: sqlite3.Connection, lexicon_id: int) -> list[int]:
    row = conn.execute(
        "SELECT evidence_message_ids FROM lexicon_entry WHERE id=?", (lexicon_id,)
    ).fetchone()
    if row is None:
        return []
    try:
        return list(json.loads(row["evidence_message_ids"] or "[]"))
    except ValueError:
        return []
