"""密钥与备份（§9.1）。

三件事：

1. **密钥生成与保护**：数据库密钥是 32 字节随机值（64 hex），落盘前用
   Windows DPAPI 包一层——DPAPI 绑当前用户，别的进程/别的机器读到密文也解不开。
2. **备份 = 加密导出**：`ATTACH ... KEY '口令'` + `sqlcipher_export()`，
   用**用户口令**重新加密，使备份自包含。**绝不直接拷库文件**（DPAPI 绑机器，
   换机打不开；WAL 下拷贝还会得到不一致快照）。
3. **fail-closed**：要加密却没有绑定 / 密钥错误，一律抛错，不静默降级明文。

DPAPI 走 ctypes，不引第三方依赖；非 Windows 平台退化为"密钥文件 + 明文 hex"，
并在 ``key_protection()`` 里如实报告，避免假装安全。
"""

from __future__ import annotations

import ctypes
import os
import secrets
import sys
from ctypes import wintypes
from pathlib import Path

ENTROPY = b"companion.db.v1"


# ───────────────────────────────────────── 密钥

def generate_key() -> str:
    """32 字节随机密钥，hex 编码（SQLCipher raw key 用）。"""
    return secrets.token_hex(32)


def key_protection() -> str:
    return "dpapi" if sys.platform == "win32" else "plainfile"


if sys.platform == "win32":  # pragma: no cover - 平台相关
    class _Blob(ctypes.Structure):
        _fields_ = [("cbData", wintypes.DWORD), ("pbData", ctypes.POINTER(ctypes.c_char))]

    _crypt32 = ctypes.windll.crypt32
    _kernel32 = ctypes.windll.kernel32
    _crypt32.CryptProtectData.argtypes = [
        ctypes.POINTER(_Blob), wintypes.LPCWSTR, ctypes.POINTER(_Blob),
        ctypes.c_void_p, ctypes.c_void_p, wintypes.DWORD, ctypes.POINTER(_Blob),
    ]
    _crypt32.CryptProtectData.restype = wintypes.BOOL
    _crypt32.CryptUnprotectData.argtypes = [
        ctypes.POINTER(_Blob), ctypes.POINTER(wintypes.LPWSTR), ctypes.POINTER(_Blob),
        ctypes.c_void_p, ctypes.c_void_p, wintypes.DWORD, ctypes.POINTER(_Blob),
    ]
    _crypt32.CryptUnprotectData.restype = wintypes.BOOL
    _kernel32.LocalFree.argtypes = [ctypes.c_void_p]
    _kernel32.LocalFree.restype = ctypes.c_void_p

    def _blob(data: bytes) -> tuple[_Blob, ctypes.Array]:
        buf = ctypes.create_string_buffer(data, len(data))
        return _Blob(len(data), ctypes.cast(buf, ctypes.POINTER(ctypes.c_char))), buf

    def dpapi_protect(data: bytes) -> bytes:
        blob_in, _keep = _blob(data)
        entropy_in, _keep2 = _blob(ENTROPY)
        blob_out = _Blob()
        ok = _crypt32.CryptProtectData(
            ctypes.byref(blob_in), None, ctypes.byref(entropy_in),
            None, None, 0, ctypes.byref(blob_out),
        )
        if not ok:
            raise OSError("CryptProtectData failed")
        try:
            return ctypes.string_at(blob_out.pbData, blob_out.cbData)
        finally:
            _kernel32.LocalFree(ctypes.cast(blob_out.pbData, ctypes.c_void_p))

    def dpapi_unprotect(blob: bytes) -> bytes:
        blob_in, _keep = _blob(blob)
        entropy_in, _keep2 = _blob(ENTROPY)
        blob_out = _Blob()
        ok = _crypt32.CryptUnprotectData(
            ctypes.byref(blob_in), None, ctypes.byref(entropy_in),
            None, None, 0, ctypes.byref(blob_out),
        )
        if not ok:
            raise OSError("CryptUnprotectData failed（换用户或换机器后无法解开）")
        try:
            return ctypes.string_at(blob_out.pbData, blob_out.cbData)
        finally:
            _kernel32.LocalFree(ctypes.cast(blob_out.pbData, ctypes.c_void_p))
else:  # pragma: no cover
    def dpapi_protect(data: bytes) -> bytes:
        return b"PLAIN:" + data

    def dpapi_unprotect(blob: bytes) -> bytes:
        if not blob.startswith(b"PLAIN:"):
            raise OSError("非 DPAPI 保护的密钥，且本平台无 DPAPI")
        return blob[len(b"PLAIN:"):]


def save_key(path: str | Path, key_hex: str) -> None:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(dpapi_protect(bytes.fromhex(key_hex)))
    if sys.platform != "win32":
        os.chmod(path, 0o600)


def load_key(path: str | Path) -> str | None:
    path = Path(path)
    if not path.exists():
        return None
    return dpapi_unprotect(path.read_bytes()).hex()


def load_or_create_key(path: str | Path) -> str:
    key = load_key(path)
    if key:
        return key
    key = generate_key()
    save_key(path, key)
    return key


# ───────────────────────────────────────── 备份（加密导出）

def _quote(passphrase: str) -> str:
    """SQLCipher 的 KEY 不能参数化，只能拼进 SQL；单引号加倍转义。"""
    return "'" + passphrase.replace("'", "''") + "'"


def export_encrypted(conn, out_path: str | Path, passphrase: str) -> Path:
    """导出一份**用口令重新加密**的备份。库文件本身不直接拷贝。"""
    out_path = Path(out_path)
    if out_path.exists():
        raise FileExistsError(f"备份目标已存在：{out_path}")
    out_path.parent.mkdir(parents=True, exist_ok=True)

    conn.execute(f"ATTACH DATABASE '{out_path.as_posix()}' AS backup KEY {_quote(passphrase)}")
    try:
        conn.execute("SELECT sqlcipher_export('backup')")
        # sqlcipher_export 不复制 user_version
        version = conn.execute("PRAGMA user_version").fetchone()[0]
        conn.execute(f"PRAGMA backup.user_version = {int(version)}")
    finally:
        conn.execute("DETACH DATABASE backup")
    return out_path


def restore_from_backup(
    backup_path: str | Path,
    passphrase: str,
    target_path: str | Path,
    key_hex: str,
) -> Path:
    """把口令备份恢复成一台新机器上的加密库（用本机 key 重新加密）。"""
    backup_path, target_path = Path(backup_path), Path(target_path)
    if target_path.exists():
        raise FileExistsError(f"目标已存在：{target_path}")

    import sqlcipher3  # noqa: PLC0415

    source = sqlcipher3.connect(str(backup_path), isolation_level=None)
    try:
        source.execute(f"PRAGMA key = {_quote(passphrase)}")
        source.execute("SELECT count(*) FROM sqlite_master").fetchone()  # 触发校验
        source.execute(
            f"ATTACH DATABASE '{target_path.as_posix()}' AS restored KEY \"x'{key_hex}'\""
        )
        try:
            source.execute("SELECT sqlcipher_export('restored')")
            version = source.execute("PRAGMA user_version").fetchone()[0]
            source.execute(f"PRAGMA restored.user_version = {int(version)}")
        finally:
            source.execute("DETACH DATABASE restored")
    finally:
        source.close()
    return target_path
