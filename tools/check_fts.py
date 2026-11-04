"""FTS 索引巡检 / 修复。

用法::

    python tools/check_fts.py data/companion.db
    python tools/check_fts.py data/companion.db --fix
    python tools/check_fts.py data/companion.db --fix --full

退出码：0 = 干净，1 = 有不一致（``--fix`` 后仍不一致也算失败）。

``orphan`` 是「主表已删、索引还在」的行。检索 SQL 与主表 JOIN，所以它不会
真的被召回；但那只是第二道防线，残影仍会撑大索引、并让任何不走 JOIN 的
路径漏出已删内容。
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from companion import db  # noqa: E402


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="verify / repair FTS indexes")
    parser.add_argument("db_path")
    parser.add_argument("--fix", action="store_true", help="repair inconsistencies")
    parser.add_argument("--full", action="store_true", help="full rebuild instead of incremental")
    args = parser.parse_args(argv)

    path = Path(args.db_path)
    if not path.exists():
        print(f"database not found: {path}", file=sys.stderr)
        return 2

    conn = db.connect(path)
    try:
        report = db.verify_fts(conn)
        print("before:", report.summary())
        if args.fix and not report.ok:
            report = db.backfill_fts(conn, full=args.full)
            print("after: ", report.summary())
        return 0 if report.ok else 1
    finally:
        conn.close()


if __name__ == "__main__":
    raise SystemExit(main())
