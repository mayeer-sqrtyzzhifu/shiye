"""真实 API 冒烟 + prompt cache 门槛实测（回答规格的 O1）。

用法::

    python tools/live_smoke.py            # 两轮真实对话
    python tools/live_smoke.py --cache    # 附带缓存门槛扫描

不会打印 key。费用很小（每次几十到几千 prompt token）。
"""

from __future__ import annotations

import argparse
import sys
import tempfile
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from companion import db  # noqa: E402
from companion.provider import OpenAICompatProvider  # noqa: E402
from companion.turn import run_turn  # noqa: E402

PERSONA = (
    "你是 Mochi，一个安静、记性好的陪伴者。你说话简短、具体，不堆形容词，"
    "不给人生建议，除非对方明确要。你记得住很久以前的小事，也记得自己说过什么。"
)

FILLER = (
    "记住以下边界：不假装是人类；不替代真实的人际关系；不制造独占感；"
    "不宣称只有我懂你；不阻止对方求助；不评判对方的选择；"
)


def seed(conn):
    now = db.utc_now()
    cur = conn.execute(
        "INSERT INTO memory_items (content, message_time, created_at, updated_at, event_start) "
        "VALUES (?,?,?,?,?)",
        ("三月初搬了一次家，累到一周没缓过来", "2026-03-01T20:00:00Z", now, now, "2026-03-01"),
    )
    db.upsert_memory_fts(conn, cur.lastrowid, "三月初搬了一次家，累到一周没缓过来")
    db.set_profile(conn, "birthday", "03-05")


def smoke() -> int:
    provider = OpenAICompatProvider()
    print(f"endpoint = {provider.endpoint}")
    print(f"model    = {provider.model}")

    with tempfile.TemporaryDirectory() as tmp:
        conn = db.connect(Path(tmp) / "live.db")
        db.apply_schema(conn)
        seed(conn)

        common = dict(
            persona=PERSONA,
            boundaries=(FILLER,),
            taboo=("前任的名字",),
            session_id="live",
        )
        prefixes = []
        for text in ("搬了一次家之后我一直没缓过来", "还记得我当时怎么说的吗"):
            print(f"\n>>> {text}")
            result = run_turn(conn, provider, text, **common)
            prefixes.append(result.bundle.system_prefix)
            usage = result.completion.usage
            print(f"    reply : {result.reply.strip()[:80]!r}")
            print(f"    usage : prompt={usage.prompt_tokens} completion={usage.completion_tokens} "
                  f"reasoning={usage.reasoning_tokens} cache_hit={usage.cache_hit_tokens} "
                  f"cache_miss={usage.cache_miss_tokens}")
            print(f"    recall: {[(h.source, h.id) for h in result.recall.hits]}")
            print(f"    notes : {result.notes or '-'}")

        print(f"\n前缀逐字节一致: {prefixes[0] == prefixes[1]}")
        conn.close()
    return 0


def cache_sweep() -> int:
    """同一段 system 前缀连发两次，看第二次是否有 cache 命中。

    这是 §6.3 的实测：文档假设「稳定前缀能命中 cache」，但门槛取决于服务商。
    """
    provider = OpenAICompatProvider()
    print("\n=== prompt cache 门槛扫描（同一前缀连发两次）===")
    print(f"{'fill_tokens':>12} {'prompt':>8} {'hit(1st)':>9} {'hit(2nd)':>9} {'miss(2nd)':>10}")

    for fill in (200, 600, 1200, 2000, 3000):
        system = PERSONA + "".join(FILLER for _ in range(1 + fill // max(1, len(FILLER) // 2)))
        messages = [{"role": "user", "content": "只回一个字：好"}]
        first = provider.stream(system, messages, max_tokens=32, temperature=0)
        second = provider.stream(system, messages, max_tokens=32, temperature=0)
        print(f"{fill:>12} {first.usage.prompt_tokens:>8} "
              f"{first.usage.cache_hit_tokens:>9} {second.usage.cache_hit_tokens:>9} "
              f"{second.usage.cache_miss_tokens:>10}")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--cache", action="store_true", help="run the cache threshold sweep")
    args = parser.parse_args()
    rc = smoke()
    if args.cache:
        rc = rc or cache_sweep()
    return rc


if __name__ == "__main__":
    raise SystemExit(main())
