"""M3 删除链路（§4）。

三件事：

1. **事件簇圈定** —— 删除单位不是单条记忆，而是"那件事"波及的一组内容。
   默认**只做规则一**（`evidence_message_ids` 连通分量）：它是硬证据，
   不会把「同一条消息里提到的两件事」误合并。规则二（事件窗口 ±3 天 +
   同一关系图实体）需要 kg 表，缺了实体就是纯时间窗，必然过合并 —— 所以
   保留参数但默认关闭，等 kg 落地再开。
2. **三态** —— hide（保留原文、不可召回）/ blur（两阶段，中间态不可召回）/
   purge（物理删除 + 级联）。三态都发一次 `data_revision`。
3. **单一入口** —— 所有删除走 `apply()`，并登记 `deletion_target`。

「删除完成」的判据不是"事务提交了"，而是：**后续检索不再返回目标内容，
且 FTS 索引与主表一致**（`db.verify_fts().ok`）。
"""

from __future__ import annotations

import json
import sqlite3
from collections import defaultdict
from dataclasses import dataclass
from uuid import uuid4

from . import db

LEVELS = ("hide", "blur", "purge")
_SAMPLE_LIMIT = 5
_SAMPLE_CHARS = 40


# ───────────────────────────────────────── 版本号 / 在途失效

@dataclass(frozen=True)
class Snapshot:
    revision: int


def snapshot(conn: sqlite3.Connection) -> Snapshot:
    row = conn.execute("SELECT revision FROM data_revision WHERE id = 1").fetchone()
    return Snapshot(row["revision"])


def is_stale(conn: sqlite3.Connection, snap: Snapshot) -> bool:
    return snapshot(conn).revision != snap.revision


def _bump_revision(conn: sqlite3.Connection) -> int:
    conn.execute(
        "UPDATE data_revision SET revision = revision + 1, updated_at = ? WHERE id = 1",
        (db.utc_now(),),
    )
    return snapshot(conn).revision


# ───────────────────────────────────────── 事件簇

@dataclass(frozen=True)
class Cluster:
    memory_ids: tuple[int, ...]
    message_ids: tuple[int, ...]
    lexicon_ids: tuple[int, ...]
    derivation_ids: tuple[int, ...]
    samples: tuple[str, ...]

    @property
    def empty(self) -> bool:
        return not (self.memory_ids or self.message_ids)

    def describe(self) -> str:
        head = f"将影响 {len(self.memory_ids)} 条记忆 / {len(self.message_ids)} 条消息"
        if self.lexicon_ids:
            head += f" / {len(self.lexicon_ids)} 条语汇"
        lines = [head]
        lines += [f"  · {s}" for s in self.samples]
        return "\n".join(lines)


def _evidence_map(conn: sqlite3.Connection):
    """memory_id -> set(message_id)，以及反向索引。"""
    by_memory: dict[int, set[int]] = {}
    by_message: dict[int, set[int]] = defaultdict(set)
    for row in conn.execute("SELECT id, evidence_message_ids FROM memory_items"):
        try:
            ids = set(json.loads(row["evidence_message_ids"] or "[]"))
        except ValueError:
            ids = set()
        by_memory[row["id"]] = ids
        for message_id in ids:
            by_message[message_id].add(row["id"])
    return by_memory, by_message


def resolve_cluster(
    conn: sqlite3.Connection,
    seed_memory_ids: list[int],
    *,
    expand_events: bool = False,
    window_days: int = 3,
) -> Cluster:
    """圈定与 seed 属于同一事件的记忆 / 消息 / 语汇 / 派生。

    ``expand_events`` 默认关闭：没有关系图实体时，"同窗口"会把同一条消息里
    提到的两件事并成一个簇（评审指出过的过合并风险）。
    """
    by_memory, by_message = _evidence_map(conn)
    cluster: set[int] = set()
    frontier = [i for i in seed_memory_ids if i in by_memory]
    while frontier:
        current = frontier.pop()
        if current in cluster:
            continue
        cluster.add(current)
        for message_id in by_memory.get(current, ()):
            for other in by_message.get(message_id, ()):
                if other not in cluster:
                    frontier.append(other)

    if expand_events and cluster:
        rows = conn.execute(
            "SELECT id, event_start FROM memory_items WHERE event_start IS NOT NULL"
        ).fetchall()
        starts = {
            r["id"]: r["event_start"]
            for r in rows
            if r["id"] in cluster
        }
        for row in rows:
            if row["id"] in cluster or not starts:
                continue
            for start in starts.values():
                if abs(_days_between(row["event_start"], start)) <= window_days:
                    cluster.add(row["id"])
                    break

    message_ids: set[int] = set()
    for memory_id in cluster:
        message_ids |= by_memory.get(memory_id, set())

    lexicon_ids: set[int] = set()
    if message_ids:
        for row in conn.execute("SELECT id, evidence_message_ids FROM lexicon_entry"):
            try:
                ids = set(json.loads(row["evidence_message_ids"] or "[]"))
            except ValueError:
                continue
            if ids & message_ids:
                lexicon_ids.add(row["id"])

    derivation_ids: set[int] = set()
    if cluster:
        marks = ", ".join("?" * len(cluster))
        derivation_ids = {
            r["id"]
            for r in conn.execute(
                f"SELECT id FROM memory_derivation WHERE memory_id IN ({marks})",
                sorted(cluster),
            )
        }

    samples = _samples(conn, cluster, message_ids)
    return Cluster(
        memory_ids=tuple(sorted(cluster)),
        message_ids=tuple(sorted(message_ids)),
        lexicon_ids=tuple(sorted(lexicon_ids)),
        derivation_ids=tuple(sorted(derivation_ids)),
        samples=samples,
    )


def _days_between(a: str, b: str) -> int:
    from datetime import date

    try:
        return (date.fromisoformat(a[:10]) - date.fromisoformat(b[:10])).days
    except ValueError:
        return 10**6


def _samples(conn, memory_ids: set[int], message_ids: set[int]) -> tuple[str, ...]:
    """展示**具体内容**，而不是只给数量 —— 用户要能判断自己删的是什么。"""
    out: list[str] = []
    if memory_ids:
        marks = ", ".join("?" * len(memory_ids))
        for row in conn.execute(
            f"SELECT content FROM memory_items WHERE id IN ({marks}) ORDER BY id LIMIT ?",
            [*sorted(memory_ids), _SAMPLE_LIMIT],
        ):
            out.append(_clip(row["content"]))
    if message_ids and len(out) < _SAMPLE_LIMIT:
        marks = ", ".join("?" * len(message_ids))
        for row in conn.execute(
            f"SELECT role, content FROM messages WHERE id IN ({marks}) ORDER BY id LIMIT ?",
            [*sorted(message_ids), _SAMPLE_LIMIT - len(out)],
        ):
            out.append(f"[{row['role']}] " + _clip(row["content"]))
    return tuple(out)


def _clip(text: str) -> str:
    text = " ".join((text or "").split())
    return text if len(text) <= _SAMPLE_CHARS else text[:_SAMPLE_CHARS] + "…"


# ───────────────────────────────────────── 执行删除

@dataclass(frozen=True)
class DeleteResult:
    level: str
    memory_ids: tuple[int, ...]
    message_ids: tuple[int, ...]
    revision: int
    fts_ok: bool


def apply(conn: sqlite3.Connection, cluster: Cluster, level: str) -> DeleteResult:
    if level not in LEVELS:
        raise ValueError(f"unknown delete level: {level}")

    with db.tx(conn):
        if level == "hide":
            _hide(conn, cluster)
        elif level == "blur":
            _blur(conn, cluster)
        else:
            _purge(conn, cluster)
        _record_targets(conn, cluster)
        _scrub_session_state(conn, cluster)
        revision = _bump_revision(conn)

    report = db.verify_fts(conn)
    return DeleteResult(
        level=level,
        memory_ids=cluster.memory_ids,
        message_ids=cluster.message_ids,
        revision=revision,
        fts_ok=report.ok,
    )


def _hide(conn: sqlite3.Connection, cluster: Cluster) -> None:
    """保留原文，只从可召回集合里拿掉。

    索引行**不删**：检索 SQL 已经按 ``status='active'`` 过滤，留着反而让
    ``verify_fts`` 仍然自洽（索引与主表行数一致）。真删才动索引。
    """
    if not cluster.memory_ids:
        return
    marks = ", ".join("?" * len(cluster.memory_ids))
    conn.execute(
        f"UPDATE memory_items SET status='hidden', updated_at=? WHERE id IN ({marks})",
        [db.utc_now(), *cluster.memory_ids],
    )


def _blur(conn: sqlite3.Connection, cluster: Cluster) -> None:
    """阶段一：进入 blurring，**立即不可召回**（靠 status 过滤）。

    改写由 ``complete_blur`` 完成；中间态不需要动索引。
    """
    if not cluster.memory_ids:
        return
    marks = ", ".join("?" * len(cluster.memory_ids))
    conn.execute(
        f"UPDATE memory_items SET status='blurring', updated_at=? WHERE id IN ({marks})",
        [db.utc_now(), *cluster.memory_ids],
    )


def complete_blur(conn: sqlite3.Connection, memory_id: int, new_content: str) -> None:
    """阶段二：写入泛化后的内容并恢复可召回。调用方负责真的把细节抹掉。"""
    with db.tx(conn):
        conn.execute(
            "UPDATE memory_items SET content=?, status='active', updated_at=? WHERE id=?",
            (new_content, db.utc_now(), memory_id),
        )
    db.upsert_memory_fts(conn, memory_id, new_content)
    _bump_revision(conn)


def _purge(conn: sqlite3.Connection, cluster: Cluster) -> None:
    memory_ids = list(cluster.memory_ids)
    message_ids = list(cluster.message_ids)

    if memory_ids:
        marks = ", ".join("?" * len(memory_ids))
        # 自引用必须先断开，否则外键会挡住删除
        conn.execute(
            f"UPDATE memory_items SET superseded_by=NULL WHERE superseded_by IN ({marks})",
            memory_ids,
        )
        conn.execute(
            f"DELETE FROM memory_derivation WHERE memory_id IN ({marks})", memory_ids
        )
        conn.execute(f"DELETE FROM memory_items WHERE id IN ({marks})", memory_ids)
        db.delete_memory_fts(conn, memory_ids)

    if message_ids:
        marks = ", ".join("?" * len(message_ids))
        conn.execute(f"DELETE FROM messages WHERE id IN ({marks})", message_ids)
        db.delete_message_fts(conn, message_ids)

    _retire_lexicon(conn, cluster)


def _retire_lexicon(conn: sqlite3.Connection, cluster: Cluster) -> None:
    """证据全被删的语汇必须退场；证据被共享的保留（降级为"还知道有这回事"）。"""
    for lexicon_id in cluster.lexicon_ids:
        row = conn.execute(
            "SELECT evidence_message_ids FROM lexicon_entry WHERE id=?", (lexicon_id,)
        ).fetchone()
        if row is None:
            continue
        try:
            evidence = set(json.loads(row["evidence_message_ids"] or "[]"))
        except ValueError:
            evidence = set()
        if evidence and evidence <= set(cluster.message_ids):
            conn.execute(
                "UPDATE lexicon_entry SET status='retired' WHERE id=?", (lexicon_id,)
            )


def _record_targets(conn: sqlite3.Connection, cluster: Cluster) -> None:
    cluster_id = "c" + uuid4().hex[:12]
    now = db.utc_now()
    rows = [("memory", i) for i in cluster.memory_ids]
    rows += [("message", i) for i in cluster.message_ids]
    rows += [("lexicon", i) for i in cluster.lexicon_ids]
    for kind, target in rows:
        conn.execute(
            "INSERT INTO deletion_target (cluster_id, target_kind, target_id, deleted_at) "
            "VALUES (?, ?, ?, ?)",
            (cluster_id, kind, str(target), now),
        )


def _scrub_session_state(conn: sqlite3.Connection, cluster: Cluster) -> None:
    """会话态里缓存的 id 也要清 —— 否则下一轮还会把已删内容摆到模型面前。

    注意 JSON 里的 id 可能是 int 也可能是 str，两边统一按 str 比对，
    但保留原类型写回，避免把 ``[1]`` 变成 ``["1"]``。
    """
    gone_memory = {str(i) for i in cluster.memory_ids}
    gone_lexicon = {str(i) for i in cluster.lexicon_ids}
    if not (gone_memory or gone_lexicon):
        return
    for row in conn.execute(
        "SELECT session_id, locked_memory_ids, last_recall_ids, injected_lexicon_ids "
        "FROM session_state"
    ).fetchall():
        updates: dict[str, str] = {}
        for column, gone in (
            ("locked_memory_ids", gone_memory),
            ("last_recall_ids", gone_memory),
            ("injected_lexicon_ids", gone_lexicon),
        ):
            try:
                raw = json.loads(row[column] or "[]")
            except ValueError:
                continue
            kept = [x for x in raw if str(x) not in gone]
            if len(kept) != len(raw):
                updates[column] = json.dumps(kept)
        for column, value in updates.items():
            conn.execute(
                f"UPDATE session_state SET {column}=?, updated_at=? WHERE session_id=?",
                (value, db.utc_now(), row["session_id"]),
            )
