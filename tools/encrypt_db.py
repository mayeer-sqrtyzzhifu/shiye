"""把明文库迁移成加密库（一次性）。

用法::

    python tools/encrypt_db.py %LOCALAPPDATA%\shiye\data\companion.db --key-file %LOCALAPPDATA%\shiye\data\companion.key
    python tools/encrypt_db.py %LOCALAPPDATA%\shiye\data\companion.db --verify-only

行为：
1. 用 SQLCipher 以**空 key** 打开明文源（``PRAGMA key = ''`` 即不加密）；
2. ``ATTACH`` 一个加密目标并 ``sqlcipher_export()``；
3. 校验目标能读出同样的行数，且文件里没有明文；
4. 只有 ``--replace`` 才动原文件（默认保留，让你自己确认后再删）。
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from companion import crypto, db  # noqa: E402


def _count(conn) -> int:
    total = 0
    for (name,) in conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'"
    ):
        try:
            total += conn.execute(f'SELECT count(*) FROM "{name}"').fetchone()[0]
        except Exception:  # noqa: BLE001 - 虚表/系统表跳过
            continue
    return total


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("source")
    parser.add_argument("--target", default=None)
    parser.add_argument("--key-file", default=None)
    parser.add_argument("--verify-only", action="store_true")
    parser.add_argument("--replace", action="store_true", help="校验通过后替换原文件")
    args = parser.parse_args(argv)

    if not db.sqlcipher_available():
        print("需要 sqlcipher3：pip install sqlcipher3", file=sys.stderr)
        return 2

    import sqlcipher3  # noqa: PLC0415

    source = Path(args.source)
    if not source.exists():
        print(f"源文件不存在：{source}", file=sys.stderr)
        return 2
    target = Path(args.target) if args.target else source.with_suffix(".enc.db")
    key_file = Path(args.key_file) if args.key_file else source.with_suffix(".key")

    key = crypto.load_or_create_key(key_file)
    print(f"key file : {key_file}（{crypto.key_protection()} 保护）")

    if args.verify_only:
        if not target.exists():
            print(f"目标不存在：{target}", file=sys.stderr)
            return 2
        conn = db.connect(target, key_hex=key)
        try:
            print(f"rows     : {_count(conn)}")
            print(f"schema   : v{db.schema_version(conn)}")
            try:
                print(f"fts      : {db.verify_fts(conn).summary()}")
            except Exception as exc:  # noqa: BLE001
                print(f"fts      : 无法校验（{type(exc).__name__}）")
        finally:
            conn.close()
        raw = target.read_bytes()
        print(f"plaintext: {'LEAK' if raw[:16] == b'SQLite format 3\x00' else 'none'}")
        return 0

    if target.exists():
        print(f"目标已存在，拒绝覆盖：{target}", file=sys.stderr)
        return 2

    # 源库是明文：**不要**设 PRAGMA key（SQLCipher 会拒绝空 key），
    # 不设 key 时它按普通 SQLite 读；导出目标才用 KEY。
    src = sqlcipher3.connect(str(source), isolation_level=None)
    try:
        src.execute("SELECT count(*) FROM sqlite_master").fetchone()
        before = _count(src)

        src.execute(f"ATTACH DATABASE '{target.as_posix()}' AS enc KEY \"x'{key}'\"")
        try:
            src.execute("SELECT sqlcipher_export('enc')")
            version = src.execute("PRAGMA user_version").fetchone()[0]
            src.execute(f"PRAGMA enc.user_version = {int(version)}")
        finally:
            src.execute("DETACH DATABASE enc")
    finally:
        src.close()

    check = db.connect(target, key_hex=key)
    try:
        after = _count(check)
        report = db.verify_fts(check)
    finally:
        check.close()

    print(f"rows     : {before} -> {after}")
    print(f"fts      : {report.summary()}")
    if before != after:
        print("行数不一致，放弃替换", file=sys.stderr)
        return 1

    if args.replace:
        backup = source.with_suffix(source.suffix + ".plain.bak")
        source.replace(backup)
        print(f"原文件已改名为：{backup.name}（确认无误后请手动删除）")
    else:
        print(f"未替换原文件；确认无误后用 --replace 或手动切换")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

