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


# ───────────────────────────────────────── 生命周期（候选 → 确认 → 生效）

def list_candidates(
    conn: sqlite3.Connection, *, status: str = "pending", limit: int = 50
) -> list[dict]:
    rows = conn.execute(
        "SELECT id, surface, kind, canonical, meaning, usage_scene, usage_note, "
        "       occurrence_count, confidence, evidence_message_ids, first_seen, last_seen "
        "FROM lexicon_candidate WHERE status = ? ORDER BY occurrence_count DESC, id LIMIT ?",
        (status, limit),
    ).fetchall()
    out = []
    for row in rows:
        item = dict(row)
        try:
            item["evidence_ids"] = json.loads(item.pop("evidence_message_ids") or "[]")
        except ValueError:
            item["evidence_ids"] = []
        out.append(item)
    return out


def list_entries(conn: sqlite3.Connection, *, status: str = "active") -> list[dict]:
    rows = conn.execute(
        "SELECT id, surface, kind, canonical, meaning, use_count, injected_count "
        "FROM lexicon_entry WHERE status = ? ORDER BY kind, surface",
        (status,),
    ).fetchall()
    return [dict(r) for r in rows]


def promote(conn: sqlite3.Connection, candidate_id: int) -> int:
    """把候选升格为生效条目，返回 lexicon_entry.id。

    设计上**只做人工确认这一条路**：候选永远不自动生效。
    taboo 尤其如此——它会常驻稳定前缀，写错一次就会影响所有后续对话。
    """
    row = conn.execute(
        "SELECT * FROM lexicon_candidate WHERE id = ?", (candidate_id,)
    ).fetchone()
    if row is None:
        raise ValueError(f"candidate not found: {candidate_id}")
    if row["status"] != "pending":
        raise ValueError(f"candidate already {row['status']}")

    surface, kind = row["surface"], row["kind"]
    existing = conn.execute(
        "SELECT id FROM lexicon_entry WHERE surface = ? AND kind = ? AND status = 'active'",
        (surface, kind),
    ).fetchone()

    if existing:
        entry_id = int(existing["id"])
        conn.execute(
            "UPDATE lexicon_entry SET canonical=COALESCE(?, canonical), meaning=?, "
            " usage_scene=COALESCE(?, usage_scene), usage_note=COALESCE(?, usage_note), "
            " confidence=?, evidence_message_ids=? WHERE id=?",
            (row["canonical"], row["meaning"], row["usage_scene"], row["usage_note"],
             row["confidence"], row["evidence_message_ids"], entry_id),
        )
    else:
        cur = conn.execute(
            "INSERT INTO lexicon_entry "
            "(surface, canonical, kind, meaning, usage_scene, usage_note, confidence, "
            " first_seen, evidence_message_ids, status) "
            "VALUES (?,?,?,?,?,?,?,?,?, 'active')",
            (surface, row["canonical"], kind, row["meaning"], row["usage_scene"],
             row["usage_note"], row["confidence"], row["first_seen"],
             row["evidence_message_ids"]),
        )
        entry_id = int(cur.lastrowid)

    conn.execute(
        "UPDATE lexicon_candidate SET status='promoted' WHERE id=?", (candidate_id,)
    )
    return entry_id


def reject(conn: sqlite3.Connection, candidate_id: int) -> None:
    cur = conn.execute(
        "UPDATE lexicon_candidate SET status='rejected' "
        "WHERE id=? AND status='pending'",
        (candidate_id,),
    )
    if cur.rowcount == 0:
        raise ValueError(f"candidate not pending: {candidate_id}")
