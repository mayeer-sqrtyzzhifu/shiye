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
from dataclasses import dataclass
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


def _validate_key(key_hex: str) -> None:
    if len(key_hex) != 64 or any(c not in "0123456789abcdefABCDEF" for c in key_hex):
        raise ValueError("key_hex 必须是 64 位十六进制（32 字节）")


def connect(path: str | Path, key_hex: str | None = None) -> sqlite3.Connection:
    """Open a connection with the project's PRAGMA baseline.

    WAL 允许读写并行，但同一时刻**只有一个 writer**，所以调用方仍需
    保持写事务短小并设置 busy_timeout（这里默认 5s）。

    加密路径是 **fail-closed** 的：要加密但没有绑定、或密钥不对，都直接抛错。
    """
    path = Path(path)
    row_factory = sqlite3.Row
    if key_hex:
        _validate_key(key_hex)
        if _sqlcipher is None:
            raise RuntimeError(
                "请求了加密库但本机没有 SQLCipher 绑定（sqlcipher3）。"
                "拒绝静默降级为明文。"
            )
        conn = _sqlcipher.connect(str(path), isolation_level=None)
        # sqlcipher3 的 Cursor 与 stdlib sqlite3.Row 不兼容，必须用各自的 Row
        row_factory = getattr(_sqlcipher, "Row", sqlite3.Row)
        conn.execute(f'PRAGMA key = "x\'{key_hex}\'"')
        try:
            # 坏密钥要到真正读页面时才报错，这里主动触发一次校验
            conn.execute("SELECT count(*) FROM sqlite_master").fetchone()
        except Exception as exc:  # noqa: BLE001
            conn.close()
            raise RuntimeError(
                "无法用该密钥打开数据库：密钥错误、文件损坏，或它不是加密库。"
            ) from exc
    else:
        conn = sqlite3.connect(str(path), isolation_level=None)

    conn.row_factory = row_factory
    conn.execute("PRAGMA journal_mode = WAL").fetchone()
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute("PRAGMA synchronous = NORMAL")
    conn.execute("PRAGMA busy_timeout = 5000")
    return conn


def connect_secure(
    path: str | Path,
    *,
    key_file: str | Path | None = None,
) -> sqlite3.Connection:
    """打开（或首次创建）加密库；密钥自动生成并用 DPAPI 保护。"""
    from . import crypto  # 避免循环导入

    key_path = Path(key_file) if key_file else Path(path).with_suffix(".key")
    return connect(path, key_hex=crypto.load_or_create_key(key_path))


def migrations() -> list[Path]:
    """按 001 / 002 … 顺序返回迁移文件。"""
    return sorted(MIGRATIONS_DIR.glob("[0-9][0-9][0-9]_*.sql"))


def latest_version() -> int:
    files = migrations()
    return int(files[-1].name.split("_", 1)[0]) if files else 0


def schema_version(conn: sqlite3.Connection) -> int:
    try:
        row = conn.execute(
            "SELECT value FROM schema_meta WHERE key = 'schema_version'"
        ).fetchone()
    except Exception:  # noqa: BLE001
        # schema_meta 还不存在；注意 sqlcipher3 抛的是自己的异常类，
        # 不是 sqlite3.OperationalError，所以这里不能只捕 sqlite3 的异常。
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


# ───────────────────────────────────────── FTS 同步（无触发器分词，靠 wrapper + 脏标记）

_FTS_TABLES: dict[str, tuple[str, str]] = {
    "memory_items": ("memories_fts", "content"),
    "messages": ("messages_fts", "content"),
}


def _clear_dirty(conn: sqlite3.Connection, table: str, ids: list[int] | None) -> None:
    if ids is None:
        conn.execute("DELETE FROM fts_dirty WHERE table_name = ?", (table,))
        return
    for chunk in range(0, len(ids), 500):
        part = ids[chunk : chunk + 500]
        marks = ", ".join("?" * len(part))
        conn.execute(
            f"DELETE FROM fts_dirty WHERE table_name = ? AND rowid_value IN ({marks})",
            [table, *part],
        )


def upsert_message_fts(conn: sqlite3.Connection, message_id: int, content: str) -> None:
    conn.execute("DELETE FROM messages_fts WHERE rowid = ?", (message_id,))
    conn.execute(
        "INSERT INTO messages_fts(rowid, tok) VALUES (?, ?)",
        (message_id, fts.tokenized_text(content)),
    )
    _clear_dirty(conn, "messages", [message_id])


def upsert_memory_fts(conn: sqlite3.Connection, memory_id: int, content: str) -> None:
    conn.execute("DELETE FROM memories_fts WHERE rowid = ?", (memory_id,))
    conn.execute(
        "INSERT INTO memories_fts(rowid, tok) VALUES (?, ?)",
        (memory_id, fts.tokenized_text(content)),
    )
    _clear_dirty(conn, "memory_items", [memory_id])


def delete_memory_fts(conn: sqlite3.Connection, memory_ids: list[int]) -> None:
    for memory_id in memory_ids:
        conn.execute("DELETE FROM memories_fts WHERE rowid = ?", (memory_id,))
    _clear_dirty(conn, "memory_items", memory_ids)


def delete_message_fts(conn: sqlite3.Connection, message_ids: list[int]) -> None:
    for message_id in message_ids:
        conn.execute("DELETE FROM messages_fts WHERE rowid = ?", (message_id,))
    _clear_dirty(conn, "messages", message_ids)


# ───────────────────────────────────────── FTS 一致性校验

@dataclass(frozen=True)
class TableFtsReport:
    table: str
    expected: int          # 主表行数
    indexed: int           # 索引行数
    missing: tuple[int, ...]   # 主表有、索引没有
    orphan: tuple[int, ...]    # 索引有、主表没有 ← "已删内容重现"的来源
    stale: tuple[int, ...]     # 两边都有，但分词结果与当前正文不一致

    @property
    def ok(self) -> bool:
        return not (self.missing or self.orphan or self.stale)


@dataclass(frozen=True)
class FtsReport:
    tables: dict[str, TableFtsReport]
    dirty: int                     # 绕过 wrapper 留下的未同步标记

    @property
    def ok(self) -> bool:
        return self.dirty == 0 and all(t.ok for t in self.tables.values())

    def summary(self) -> str:
        parts = [
            f"{name}: expected={r.expected} indexed={r.indexed} "
            f"missing={len(r.missing)} orphan={len(r.orphan)} stale={len(r.stale)}"
            for name, r in self.tables.items()
        ]
        return f"dirty={self.dirty} | " + " | ".join(parts)


def _content_map(conn: sqlite3.Connection, table: str, column: str) -> dict[int, str]:
    return {
        r["id"]: (r[column] or "")
        for r in conn.execute(f"SELECT id, {column} FROM {table}")
    }


def _index_map(conn: sqlite3.Connection, fts_table: str) -> dict[int, str]:
    return {r["rowid"]: r["tok"] for r in conn.execute(f"SELECT rowid, tok FROM {fts_table}")}


def verify_fts(conn: sqlite3.Connection, *, check_content: bool = True) -> FtsReport:
    """Compare the index against the authoritative tables.

    ``orphan`` 是最危险的一类：主表已删、索引还在，检索就会把已删内容捞回来。
    """
    tables: dict[str, TableFtsReport] = {}
    for table, (fts_table, column) in _FTS_TABLES.items():
        content = _content_map(conn, table, column)
        index = _index_map(conn, fts_table)
        missing = tuple(sorted(set(content) - set(index)))
        orphan = tuple(sorted(set(index) - set(content)))
        stale = ()
        if check_content:
            stale = tuple(sorted(
                i for i in set(content) & set(index)
                if fts.tokenized_text(content[i]) != index[i]
            ))
        tables[table] = TableFtsReport(
            table=table,
            expected=len(content),
            indexed=len(index),
            missing=missing,
            orphan=orphan,
            stale=stale,
        )
    dirty = conn.execute("SELECT count(*) FROM fts_dirty").fetchone()[0]
    return FtsReport(tables=tables, dirty=dirty)


def backfill_fts(conn: sqlite3.Connection, *, full: bool = False) -> FtsReport:
    """Repair the index from the authoritative tables, then re-verify.

    ``full=True`` 重建整张索引；否则只修不一致的行（增量修复）。
    """
    with tx(conn):
        for table, (fts_table, column) in _FTS_TABLES.items():
            content = _content_map(conn, table, column)
            if full:
                conn.execute(f"DELETE FROM {fts_table}")
                for row_id, text in content.items():
                    conn.execute(
                        f"INSERT INTO {fts_table}(rowid, tok) VALUES (?, ?)",
                        (row_id, fts.tokenized_text(text)),
                    )
            else:
                index = _index_map(conn, fts_table)
                for row_id, text in content.items():
                    wanted = fts.tokenized_text(text)
                    if index.get(row_id) != wanted:
                        conn.execute(f"DELETE FROM {fts_table} WHERE rowid = ?", (row_id,))
                        conn.execute(
                            f"INSERT INTO {fts_table}(rowid, tok) VALUES (?, ?)",
                            (row_id, wanted),
                        )
                for row_id in set(index) - set(content):
                    conn.execute(f"DELETE FROM {fts_table} WHERE rowid = ?", (row_id,))
            _clear_dirty(conn, table, None)
    return verify_fts(conn)


def rebuild_fts(conn: sqlite3.Connection) -> FtsReport:
    return backfill_fts(conn, full=True)


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
    主路径是 bigram；一轮零命中时退到单字 OR 兜底（见 ``fts.match_expr_unigrams``）。
    """
    sql = """
        SELECT memories_fts.rowid AS id, -bm25(memories_fts) AS relevance
        FROM memories_fts
        JOIN memory_items m ON m.id = memories_fts.rowid
        WHERE memories_fts.tok MATCH ?
          AND m.user_id = ?
          AND m.status = 'active'
          AND m.superseded_by IS NULL
        ORDER BY relevance DESC
        LIMIT ?
    """
    expr = fts.match_expr(query)
    if expr is None:
        return []
    rows = conn.execute(sql, (expr, user_id, limit)).fetchall()
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
    sql = """
        SELECT messages_fts.rowid AS id, -bm25(messages_fts) AS relevance
        FROM messages_fts
        JOIN messages msg ON msg.id = messages_fts.rowid
        WHERE messages_fts.tok MATCH ?
          AND msg.user_id = ?
        ORDER BY relevance DESC
        LIMIT ?
    """
    expr = fts.match_expr(query)
    if expr is None:
        return []
    rows = conn.execute(sql, (expr, user_id, limit)).fetchall()
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
