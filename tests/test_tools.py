"""命令行工具的端到端回归。

这些工具一旦出错会静默毁数据（比如"加密"备份其实是明文、迁移后原库不见了），
所以用真实子进程跑，而不是直接调函数。
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

from companion import crypto, db

ROOT = Path(__file__).resolve().parents[1]


def _make_plaintext_db(path: Path, rows: int = 1) -> None:
    conn = db.connect(path)      # 不传 key = 明文
    db.apply_schema(conn)
    for i in range(rows):
        conn.execute(
            "INSERT INTO messages (role, content, message_time, created_at, session_id, no_send) "
            "VALUES ('user', ?, '2026-11-04T12:00:00Z', '2026-11-04T12:00:00Z', 's', 0)",
            (f"第 {i} 条明文",),
        )
    conn.commit()
    conn.close()


def _run_tool(*args: str) -> subprocess.CompletedProcess:
    return subprocess.run(
        [sys.executable, str(ROOT / "tools" / "encrypt_db.py"), *args],
        capture_output=True, text=True, encoding="utf-8", errors="replace",
        cwd=str(ROOT),
    )


def test_replace_leaves_encrypted_db_in_place(tmp_path):
    """--replace 之后原路径必须是加密库，而不是消失。"""
    source = tmp_path / "companion.db"
    _make_plaintext_db(source, rows=3)
    assert source.read_bytes()[:16] == b"SQLite format 3\x00"

    result = _run_tool(
        str(source),
        "--key-file", str(tmp_path / "companion.key"),
        "--target", str(tmp_path / "enc.db"),
        "--replace",
    )
    assert result.returncode == 0, result.stdout + result.stderr

    assert source.exists(), "替换后原路径不该消失"
    assert source.read_bytes()[:16] != b"SQLite format 3\x00", "原路径仍是明文"

    key = crypto.load_key(tmp_path / "companion.key")
    conn = db.connect(source, key)
    try:
        assert conn.execute("SELECT count(*) c FROM messages").fetchone()["c"] == 3
    finally:
        conn.close()

    backup = source.with_suffix(source.suffix + ".plain.bak")
    assert backup.exists(), "应保留明文备份供人工确认"


def test_replace_refuses_when_backup_exists(tmp_path):
    source = tmp_path / "companion.db"
    _make_plaintext_db(source)
    backup = source.with_suffix(source.suffix + ".plain.bak")
    backup.write_text("占位", encoding="utf-8")

    result = _run_tool(
        str(source),
        "--key-file", str(tmp_path / "companion.key"),
        "--target", str(tmp_path / "enc.db"),
        "--replace",
    )
    assert result.returncode != 0
    assert source.read_bytes()[:16] == b"SQLite format 3\x00", "拒绝时不该动原库"


def test_verify_only_reports_plaintext(tmp_path):
    source = tmp_path / "companion.db"
    _make_plaintext_db(source)
    result = _run_tool(
        str(source),
        "--key-file", str(tmp_path / "companion.key"),
        "--target", str(source),
        "--verify-only",
    )
    assert result.returncode == 0
    assert "LEAK" in result.stdout

