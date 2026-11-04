"""加密与备份（§9.1）。没有 SQLCipher 绑定时整体 skip，避免假装测过。"""

from __future__ import annotations

import sys

import pytest

from companion import crypto, db, fts

HAS_SQLCIPHER = db.sqlcipher_available()
needs_cipher = pytest.mark.skipif(not HAS_SQLCIPHER, reason="需要 sqlcipher3 绑定")

KEY = "ab" * 32
OTHER_KEY = "cd" * 32
SECRET = "三月初搬了一次家，一直没缓过来"


def _seed(conn):
    cur = conn.execute(
        "INSERT INTO memory_items (content, created_at, updated_at) VALUES (?,?,?)",
        (SECRET, db.utc_now(), db.utc_now()),
    )
    db.upsert_memory_fts(conn, cur.lastrowid, SECRET)
    cur = conn.execute(
        "INSERT INTO messages (role, content, message_time, created_at) VALUES (?,?,?,?)",
        ("user", SECRET, db.utc_now(), db.utc_now()),
    )
    db.upsert_message_fts(conn, cur.lastrowid, SECRET)


@needs_cipher
def test_encrypted_db_has_no_plaintext_on_disk(tmp_path):
    path = tmp_path / "enc.db"
    conn = db.connect(path, key_hex=KEY)
    db.apply_schema(conn)
    _seed(conn)
    conn.close()

    raw = path.read_bytes()
    assert SECRET.encode("utf-8") not in raw
    assert raw[:16] != b"SQLite format 3\x00"      # 连 SQLite 头都没有
    # WAL 也不能漏
    for extra in tmp_path.glob("enc.db*"):
        assert SECRET.encode("utf-8") not in extra.read_bytes()


@needs_cipher
def test_reopen_with_same_key_works(tmp_path):
    path = tmp_path / "enc.db"
    conn = db.connect(path, key_hex=KEY)
    db.apply_schema(conn)
    _seed(conn)
    conn.close()

    again = db.connect(path, key_hex=KEY)
    try:
        assert again.execute("SELECT count(*) FROM memory_items").fetchone()[0] == 1
    finally:
        again.close()


@needs_cipher
def test_wrong_key_is_rejected(tmp_path):
    path = tmp_path / "enc.db"
    conn = db.connect(path, key_hex=KEY)
    db.apply_schema(conn)
    conn.close()

    with pytest.raises(RuntimeError, match="密钥错误|不是加密库"):
        db.connect(path, key_hex=OTHER_KEY)


@needs_cipher
def test_plaintext_db_cannot_be_opened_with_key(tmp_path):
    path = tmp_path / "plain.db"
    conn = db.connect(path)
    db.apply_schema(conn)
    conn.close()

    with pytest.raises(RuntimeError):
        db.connect(path, key_hex=KEY)


@needs_cipher
def test_encrypted_db_cannot_be_opened_without_key(tmp_path):
    path = tmp_path / "enc.db"
    conn = db.connect(path, key_hex=KEY)
    db.apply_schema(conn)
    conn.close()

    with pytest.raises(Exception):
        plain = db.connect(path)
        plain.execute("SELECT count(*) FROM sqlite_master").fetchone()


def test_key_requested_without_binding_fails_closed(tmp_path, monkeypatch):
    """没有 SQLCipher 时必须抛错，绝不能静默写明文。"""
    from companion import db as db_mod

    monkeypatch.setattr(db_mod, "_sqlcipher", None)
    with pytest.raises(RuntimeError, match="拒绝静默降级"):
        db.connect(tmp_path / "x.db", key_hex=KEY)


def test_bad_key_format_is_rejected(tmp_path):
    for bad in ("", "zz", "ab" * 31, "g" * 64):
        if not bad:
            continue
        with pytest.raises(ValueError):
            db.connect(tmp_path / "y.db", key_hex=bad)


def test_security_mode_reports_truth():
    assert db.security_mode(None) == "plaintext-dev"
    expected = "sqlcipher" if HAS_SQLCIPHER else "plaintext-dev"
    assert db.security_mode(KEY) == expected


def test_key_generation_shape():
    key = crypto.generate_key()
    assert len(key) == 64
    assert key != crypto.generate_key()
    int(key, 16)


def test_key_roundtrip_through_dpapi(tmp_path):
    key_path = tmp_path / "db.key"
    key = crypto.generate_key()
    crypto.save_key(key_path, key)

    blob = key_path.read_bytes()
    assert key.encode() not in blob            # 磁盘上不是明文 hex
    assert crypto.load_key(key_path) == key
    assert crypto.load_key(tmp_path / "missing.key") is None
    assert crypto.key_protection() in ("dpapi", "plainfile")


def test_load_or_create_is_idempotent(tmp_path):
    key_path = tmp_path / "db.key"
    first = crypto.load_or_create_key(key_path)
    assert crypto.load_or_create_key(key_path) == first


@needs_cipher
def test_backup_export_and_restore(tmp_path):
    src = tmp_path / "live.db"
    conn = db.connect(src, key_hex=KEY)
    db.apply_schema(conn)
    _seed(conn)

    backup = crypto.export_encrypted(conn, tmp_path / "backup.db", "口令-123")
    conn.close()

    # 备份自身是加密的，且不含明文
    blob = backup.read_bytes()
    assert SECRET.encode("utf-8") not in blob
    assert blob[:16] != b"SQLite format 3\x00"

    # 恢复成"另一台机器"的库：用本机新 key 重新加密
    new_key = crypto.generate_key()
    crypto.restore_from_backup(backup, "口令-123", tmp_path / "restored.db", new_key)

    restored = db.connect(tmp_path / "restored.db", key_hex=new_key)
    try:
        assert restored.execute("SELECT count(*) FROM memory_items").fetchone()[0] == 1
        assert db.schema_version(restored) == db.latest_version()
        assert db.verify_fts(restored).ok
        assert db.search_memories(restored, "搬了一次家")
    finally:
        restored.close()


@needs_cipher
def test_backup_wrong_passphrase_fails(tmp_path):
    src = tmp_path / "live.db"
    conn = db.connect(src, key_hex=KEY)
    db.apply_schema(conn)
    _seed(conn)
    backup = crypto.export_encrypted(conn, tmp_path / "backup.db", "正确口令")
    conn.close()

    with pytest.raises(Exception):
        crypto.restore_from_backup(backup, "错误口令", tmp_path / "nope.db", KEY)


@needs_cipher
def test_export_refuses_to_overwrite(tmp_path):
    src = tmp_path / "live.db"
    conn = db.connect(src, key_hex=KEY)
    db.apply_schema(conn)
    target = tmp_path / "backup.db"
    crypto.export_encrypted(conn, target, "p")
    with pytest.raises(FileExistsError):
        crypto.export_encrypted(conn, target, "p")
    conn.close()


@needs_cipher
def test_connect_secure_creates_and_reuses_key(tmp_path):
    path = tmp_path / "secure.db"
    conn = db.connect_secure(path)
    db.apply_schema(conn)
    _seed(conn)
    conn.close()

    assert (tmp_path / "secure.key").exists()
    again = db.connect_secure(path)
    try:
        assert again.execute("SELECT count(*) FROM memory_items").fetchone()[0] == 1
    finally:
        again.close()


def test_export_rejects_empty_passphrase(tmp_path):
    """空口令会生成明文库 —— "加密备份"最危险的失效方式。"""
    import pytest

    conn = db.connect_secure(tmp_path / "a.db")
    db.apply_schema(conn)
    try:
        with pytest.raises(ValueError):
            crypto.export_encrypted(conn, tmp_path / "b.db", "")
        assert not (tmp_path / "b.db").exists()
        with pytest.raises(ValueError):
            crypto.export_encrypted(conn, tmp_path / "c.db", "   ")
        assert not (tmp_path / "c.db").exists()
    finally:
        conn.close()


def test_exported_backup_is_not_plaintext(tmp_path):
    conn = db.connect_secure(tmp_path / "a.db")
    db.apply_schema(conn)
    try:
        out = crypto.export_encrypted(conn, tmp_path / "b.db", "correct horse")
        assert out.read_bytes()[:16] != b"SQLite format 3\x00"
    finally:
        conn.close()
