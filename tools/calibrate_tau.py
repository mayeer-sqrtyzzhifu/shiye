"""标定检索的"相对阈值"。

为什么需要它
------------
``recall.py`` 里的 ``tau_memory`` / ``tau_message`` 决定"多低的 BM25 分算命中"。
设成 0 等于"沾一点词就召回"，噪声会挤掉真正相关的条目；设得太高又会漏。
这个脚本用带标注的查询扫一遍阈值，给出精度/召回/F1 曲线，挑一个可解释的值。

用法::

    python tools/calibrate_tau.py                       # 用 eval/tau_fixture.json
    python tools/calibrate_tau.py --db <路径> --labels my.json

标注格式见 ``eval/tau_fixture.json``：每条查询给出 kind（memory/message）和
relevant 的 id 列表；relevant 为空表示"这条不该召回任何东西"。

挑选规则（不是纯 F1 最大）
--------------------------
陪伴场景里**假召回比漏召回更贵**：说"我记得你养了只猫"但其实记错了，
比"我不太记得"糟糕得多。所以要求 precision ≥ 0.8，在此前提下取 F1 最高；
再平就取更高的 τ（更保守）。
"""

from __future__ import annotations

import argparse
import json
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from companion import db  # noqa: E402

DEFAULT_FIXTURE = Path(__file__).resolve().parents[1] / "eval" / "tau_fixture.json"
PRECISION_FLOOR = 0.8


def build_fixture_db(fixture: dict) -> "db.sqlite3.Connection":
    tmp = Path(tempfile.mkdtemp()) / "tau.db"
    conn = db.connect(tmp)
    db.apply_schema(conn)
    now = db.utc_now()
    for doc in fixture["docs"]:
        if doc["kind"] == "memory":
            conn.execute(
                "INSERT INTO memory_items (id, content, source, source_role, epistemic_status, "
                "created_at, updated_at) VALUES (?, ?, 'fixture', 'user', 'explicit', ?, ?)",
                (doc["id"], doc["text"], now, now),
            )
            db.upsert_memory_fts(conn, doc["id"], doc["text"])
        else:
            conn.execute(
                "INSERT INTO messages (id, role, content, message_time, created_at, session_id, no_send) "
                "VALUES (?, 'user', ?, ?, ?, 'fixture', 0)",
                (doc["id"], doc["text"], now, now),
            )
            db.upsert_message_fts(conn, doc["id"], doc["text"])
    conn.commit()
    return conn


def collect_hits(conn, fixture: dict) -> list[dict]:
    """对每条查询取全部命中（不设阈值），并标注 相关 / 不相关。"""
    collected = []
    for query in fixture["queries"]:
        kind = query["kind"]
        gold = set(query["relevant"])
        search = db.search_memories if kind == "memory" else db.search_messages
        hits = search(conn, query["q"], limit=50, min_relevance=-1e9)
        for doc_id, score in hits:
            collected.append({
                "kind": kind,
                "query": query["q"],
                "doc_id": doc_id,
                "score": score,
                "relevant": doc_id in gold,
            })
    return collected


def sweep(hits: list[dict], kind: str) -> list[dict]:
    """按"相对最强命中的比例"扫一遍，而不是绝对分。

    绝对阈值不可迁移：同一对文本在 3 篇语料的库里和在 34 篇语料的库里，
    BM25 分数能差几倍（IDF 随文档数变）。比例阈值只表达
    "这条够不够格跟最强的候选并列"。
    """
    subset = [h for h in hits if h["kind"] == kind]
    total_relevant = sum(1 for h in subset if h["relevant"])
    if not subset:
        return []

    by_query: dict[str, list[dict]] = {}
    for hit in subset:
        by_query.setdefault(hit["query"], []).append(hit)

    rows = []
    for ratio in sorted({0.0, 0.1, 0.2, 0.3, 0.4, 0.5, 0.6, 0.7, 0.8, 0.9}, reverse=True):
        kept = []
        for group in by_query.values():
            best = max(h["score"] for h in group)
            if best <= 0:
                continue
            kept.extend(h for h in group if h["score"] >= best * ratio)
        kept_relevant = sum(1 for h in kept if h["relevant"])
        precision = kept_relevant / len(kept) if kept else 1.0
        recall = kept_relevant / total_relevant if total_relevant else 1.0
        f1 = 2 * precision * recall / (precision + recall) if (precision + recall) else 0.0
        rows.append({
            "ratio": ratio, "kept": len(kept), "relevant": kept_relevant,
            "precision": precision, "recall": recall, "f1": f1,
        })
    return rows


def choose(rows: list[dict]) -> dict | None:
    eligible = [r for r in rows if r["precision"] >= PRECISION_FLOOR and r["relevant"] > 0]
    if not eligible:
        eligible = rows
    # F1 最高；平手时取更大的比例（保留得更少 = 更保守）
    return max(eligible, key=lambda r: (r["f1"], r["ratio"]))


def report(rows: list[dict], kind: str, best: dict | None) -> None:
    print(f"\n=== {kind} ===")
    print(f"{'比例':>6} {'保留':>5} {'相关':>5} {'精度':>6} {'召回':>6} {'F1':>6}")
    for row in rows:
        mark = "  ←" if best and abs(row["ratio"] - best["ratio"]) < 1e-9 else ""
        print(f"{row['ratio']:>6.1f} {row['kept']:>5} {row['relevant']:>5} "
              f"{row['precision']:>6.2f} {row['recall']:>6.2f} {row['f1']:>6.2f}{mark}")
    if best:
        print(f"选中比例 {best['ratio']:.1f}：精度 {best['precision']:.2f} / 召回 {best['recall']:.2f} "
              f"/ F1 {best['f1']:.2f}，保留 {best['kept']} 条，其中相关 {best['relevant']} 条")

def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--fixture", default=str(DEFAULT_FIXTURE))
    parser.add_argument("--db", help="改用真实数据库（需配合 --labels）")
    parser.add_argument("--labels", help="标注文件（与 fixture 同格式）")
    args = parser.parse_args(argv)

    if args.db:
        if not args.labels:
            print("用 --db 时必须同时给 --labels", file=sys.stderr)
            return 2
        fixture = json.loads(Path(args.labels).read_text(encoding="utf-8"))
        conn = db.connect(Path(args.db))
    else:
        fixture = json.loads(Path(args.fixture).read_text(encoding="utf-8"))
        conn = build_fixture_db(fixture)

    try:
        hits = collect_hits(conn, fixture)
        results: dict[str, dict] = {}
        for kind in ("memory", "message"):
            rows = sweep(hits, kind)
            best = choose(rows)
            report(rows, kind, best)
            if best:
                results[kind] = best
    finally:
        conn.close()

    print("\n建议写入 recall.py：")
    for kind, best in results.items():
        print(f'    KEEP_RATIO_{kind.upper()} = {best["ratio"]:.2f}')
    return 0


if __name__ == "__main__":
    raise SystemExit(main())


