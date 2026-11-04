"""Connection, schema bootstrap and the FTS sync wrappers.

加密边界（§9.1）
----------------
* 传 ``key_hex`` → 必须走 SQLCipher。**没有 SQLCipher 就报错，绝不静默降级成明文。**
* 不传 ``key_hex`` → 明文库，只允许开发 / 测试使用，``security_mode()`` 会报告。
"""

from __future__ import annotations

import contextlib
import json
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterator

from . import fts

try:  # pragma: no cover - 取决于本机是否装了 SQLCipher
    import sqlcipher3 as _sqlcipher
except ImportError:  # pragma: no cover
    _sqlcipher = None

MIGRATIONS_DIR = Path(__file__).resolve().parents[2] / "schema"
SCHEMA_FILE = MIGRATIONS_DIR / "001_init.sql"


def migratons() -> list[Path]:
    """按 001 / 002 … 顺序返回迁移文件。"""
    return sorted(MIGRATIONS_DIR.glob("[0-9][0-9][0-9]_*.sql"))


def sqlcipher_available() -> bool:
    return _sqlcipher is not None


def security_mode(key_hex: str | None) -> str:
    return "sqlcipher" if (key_hex and _sqlcipher is not None) else "plaintext-dev"


def utc_now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def connect(path: str | Path, key_hex: str | None = None) -> sqlite3.Connection:
    """Open a connection with the project's PRAGMA baseline.

    WAL 允许读写并行，但同一时刻**只有一个 writer**，所以调用方仍需
    保持写事务短小并设置 busy_timeout（这里默认 5s）。
    """
    path = Path(path)
    if key_hex:
        if _sqlcipher is None:
            raise RuntimeError(
                "请求了加密库但本机没有 SQLCipher 绑定（sqlcipher3）。"
                "拒绝静默降级为明文。"
            )
        conn = _sqlcipher.connect(str(path), isolation_level=None)
        conn.execute(f'PRAGMA key = "x\'{key_hex}\'"')
        # 坏密钥要到真正读页面时才报错，这里主动触发一次校验
        conn.execute("SELECT count(*) FROM sqlite_master").fetchone()
    else:
        conn = sqlite3.connect(str(path), isolation_level=None)

    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode = WAL").fetchone()
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute("PRAGMA synchronous = NORMAL")
    conn.execute("PRAGMA busy_timeout = 5000")
    return conn


def migrations() -> list[Path]:
    """按 001 / 002 … 顺序返回迁移文件。"""
    return sorted(MIGRATIONS_DIR.glob("[0-9][0-9][0-9]_*.sql"))


def schema_version(conn: sqlite3.Connection) -> int:
    try:
        row = conn.execute(
            "SELECT value FROM schema_meta WHERE key = 'schema_version'"
        ).fetchone()
    except sqlite3.OperationalError:  # schema_meta 还不存在
        return 0
    return int(row["value"]) if row else 0


def apply_schema(conn: sqlite3.Connection) -> int:
    """Apply pending migrations in order; returns the resulting version.

    不允许原地改已发布的迁移文件 —— 变更一律新增 ``NNN_*.sql``（§16.5）。
    """
    for path in migrations():
        version = int(path.name.split("_", 1)[0])
        if version <= schema_version(conn):
            continue
        conn.executescript(path.read_text(encoding="utf-8"))
        conn.execute(
            "INSERT OR REPLACE INTO schema_meta (key, value) VALUES ('schema_version', ?)",
            (str(version),),
        )
    return schema_version(conn)


def integrity_report(conn: sqlite3.Connection) -> dict[str, list[str]]:
    return {
        "integrity": [r[0] for r in conn.execute("PRAGMA integrity_check")],
        "foreign_keys": [tuple(r) for r in conn.execute("PRAGMA foreign_key_check")],
    }


@contextlib.contextmanager
def tx(conn: sqlite3.Connection) -> Iterator[sqlite3.Connection]:
    """Short write transaction. BEGIN IMMEDIATE so the write lock is taken up front."""
    conn.execute("BEGIN IMMEDIATE")
    try:
        yield conn
    except BaseException:
        conn.execute("ROLLBACK")
        raise
    conn.execute("COMMIT")


# ───────────────────────────────────────── FTS 同步（无触发器，必须走 wrapper）

def upsert_message_fts(conn: sqlite3.Connection, message_id: int, content: str) -> None:
    conn.execute("DELETE FROM messages_fts WHERE rowid = ?", (message_id,))
    conn.execute(
        "INSERT INTO messages_fts(rowid, tok) VALUES (?, ?)",
        (message_id, fts.tokenized_text(content)),
    )


def upsert_memory_fts(conn: sqlite3.Connection, memory_id: int, content: str) -> None:
    conn.execute("DELETE FROM memories_fts WHERE rowid = ?", (memory_id,))
    conn.execute(
        "INSERT INTO memories_fts(rowid, tok) VALUES (?, ?)",
        (memory_id, fts.tokenized_text(content)),
    )


def delete_memory_fts(conn: sqlite3.Connection, memory_ids: list[int]) -> None:
    for memory_id in memory_ids:
        conn.execute("DELETE FROM memories_fts WHERE rowid = ?", (memory_id,))


def rebuild_fts(conn: sqlite3.Connection) -> dict[str, int]:
    """Rebuild both indexes from authoritative tables (also the post-delete check)."""
    conn.execute("DELETE FROM memories_fts")
    conn.execute("DELETE FROM messages_fts")
    memories = conn.execute("SELECT id, content FROM memory_items").fetchall()
    messages = conn.execute("SELECT id, content FROM messages").fetchall()
    for row in memories:
        upsert_memory_fts(conn, row["id"], row["content"])
    for row in messages:
        upsert_message_fts(conn, row["id"], row["content"])
    return {"memories": len(memories), "messages": len(messages)}


# ───────────────────────────────────────── 最小检索（B 路，验证 bm25 契约）

def search_memories(
    conn: sqlite3.Connection,
    query: str,
    *,
    user_id: int = 1,
    min_relevance: float = 0.0,
    limit: int = 20,
) -> list[tuple[int, float]]:
    """Memory text path. Returns [(id, relevance)] with relevance = -bm25 (higher is better).

    ``min_relevance`` is the τ of §3.5, applied to the text path only.
    """
    expr = fts.match_expr(query)
    if expr is None:
        return []
    rows = conn.execute(
        """
        SELECT memories_fts.rowid AS id, -bm25(memories_fts) AS relevance
        FROM memories_fts
        JOIN memory_items m ON m.id = memories_fts.rowid
        WHERE memories_fts.tok MATCH ?
          AND m.user_id = ?
          AND m.status = 'active'
          AND m.superseded_by IS NULL
        ORDER BY relevance DESC
        LIMIT ?
        """,
        (expr, user_id, limit),
    ).fetchall()
    return [(r["id"], r["relevance"]) for r in rows if r["relevance"] >= min_relevance]


def search_messages(
    conn: sqlite3.Connection,
    query: str,
    *,
    user_id: int = 1,
    min_relevance: float = 0.0,
    limit: int = 20,
) -> list[tuple[int, float]]:
    """Conversation-text path. ``no_send`` rows are returned but must not be sent out."""
    expr = fts.match_expr(query)
    if expr is None:
        return []
    rows = conn.execute(
        """
        SELECT messages_fts.rowid AS id, -bm25(messages_fts) AS relevance
        FROM messages_fts
        JOIN messages msg ON msg.id = messages_fts.rowid
        WHERE messages_fts.tok MATCH ?
          AND msg.user_id = ?
        ORDER BY relevance DESC
        LIMIT ?
        """,
        (expr, user_id, limit),
    ).fetchall()
    return [(r["id"], r["relevance"]) for r in rows if r["relevance"] >= min_relevance]


# ───────────────────────────────────────── 档案槽位

def get_profile(conn: sqlite3.Connection) -> dict[str, str]:
    return {r["key"]: r["value"] for r in conn.execute("SELECT key, value FROM profile")}


def set_profile(
    conn: sqlite3.Connection,
    key: str,
    value: str,
    *,
    evidence_message_ids: list[int] | None = None,
) -> None:
    """档案只接受白名单槽位；写入必须带证据（§2.3 的同一原则）。"""
    if key not in _PROFILE_SLOTS:
        raise ValueError(f"unknown profile slot: {key}")
    conn.execute(
        "INSERT INTO profile (key, value, evidence_message_ids, updated_at) "
        "VALUES (?, ?, ?, ?) ON CONFLICT(key) DO UPDATE SET "
        "value=excluded.value, evidence_message_ids=excluded.evidence_message_ids, "
        "updated_at=excluded.updated_at",
        (key, value, json.dumps(evidence_message_ids or []), utc_now()),
    )


_PROFILE_SLOTS = {"birthday", "calendar", "name", "timezone"}
