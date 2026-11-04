"""网页版前端（最小实现）。

刻意只用标准库：本机自用、单进程、无框架依赖。

* `GET  /`           → web/index.html
* `GET  /api/health` → 状态与安全模式
* `POST /api/chat`   → SSE 流式回复

三个与架构相关的点：

1. **每次请求一个独立连接**。SQLite 同一时刻只有一个 writer，长连接共享会
   在并发下互相锁死；每次开/关最省心，代价可以忽略。
2. **流式靠回调**。`run_turn(on_text=...)` 在生成过程中把片段推给浏览器，
   所以 SSE 是在同一线程里写的 —— 不需要额外的队列。
3. **抽取在回复之后**。回复发完再跑 L4 抽取，失败不影响这一轮（§2.3）。
"""

from __future__ import annotations

import argparse
import json
import sys
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from datetime import datetime, timedelta, timezone
from urllib.parse import parse_qs, urlsplit
from uuid import uuid4
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
# 既能 `python web/server.py` 直接跑，也能 `import web.server`（测试用）
for _p in (str(ROOT), str(ROOT / "src")):
    if _p not in sys.path:
        sys.path.insert(0, _p)

from companion import db, extract, paths, persona  # noqa: E402
from companion import prompt as prompt_mod  # noqa: E402
from companion.prompt import format_local_moment, local_datetime  # noqa: E402
from companion.provider import OpenAICompatProvider  # noqa: E402
from companion.turn import run_turn  # noqa: E402
from web.lifecycle import Lifecycle, Presences, Watchdog  # noqa: E402

INDEX = Path(__file__).with_name("index.html")

# 人格正文来自 companion.persona（可切换），这里只留安全边界
PERSONA = persona.DEFAULT_PERSONA.render()
BOUNDARIES = (
    "不假装是人类",
    "不替代真实的人际关系",
    "不制造独占感，不宣称只有你懂对方",
    "不阻止对方求助",
)


def _utc_windows(days: set[str], tz_name: str | None) -> list[tuple[str, str]]:
    """本地自然日 → UTC 区间。日期非法就跳过，不抛。"""
    windows: list[tuple[str, str]] = []
    for day in sorted(days):
        try:
            start_local = prompt_mod._localize(
                datetime.strptime(day, "%Y-%m-%d"), tz_name
            )
        except ValueError:
            continue
        end_local = start_local + timedelta(days=1)
        fmt = "%Y-%m-%dT%H:%M:%SZ"
        windows.append((
            start_local.astimezone(timezone.utc).strftime(fmt),
            end_local.astimezone(timezone.utc).strftime(fmt),
        ))
    return windows


def _memories_referencing(conn, message_ids: set[int]) -> list[int]:
    """找出证据链里引用了这些消息的记忆。

    删除口径偏保守：**只要引用了被删消息就一起删**。宁可多删一条记忆，
    也不能让删掉的话从记忆里复述出来。
    """
    found: list[int] = []
    for row in conn.execute("SELECT id, evidence_message_ids FROM memory_items"):
        try:
            refs = {int(x) for x in json.loads(row["evidence_message_ids"] or "[]")}
        except (ValueError, TypeError):
            continue
        if refs & message_ids:
            found.append(int(row["id"]))
    return found


class App:
    def __init__(
        self,
        data_dir: str | Path | None = None,
        provider_factory=None,
        lifecycle: Lifecycle | None = None,
    ) -> None:
        self.data_dir = Path(data_dir) if data_dir is not None else paths.data_dir()
        self.data_dir.mkdir(parents=True, exist_ok=True)
        self.db_path = self.data_dir / "companion.db"
        self.key_path = self.data_dir / "companion.key"
        self.provider_factory = provider_factory or OpenAICompatProvider
        self.write_lock = threading.Lock()
        self._provider = None
        self._provider_lock = threading.Lock()
        self._server = None
        self.stop_reason: str | None = None
        self.watchdog = Watchdog(lifecycle or Lifecycle(), self._on_stop)
        self.presences = Presences(stale_after=(lifecycle or Lifecycle()).idle_timeout)

        conn = self.connect()
        try:
            db.apply_schema(conn)
        finally:
            conn.close()

    # ── 生命周期
    def attach_server(self, httpd) -> None:
        self._server = httpd
        self.watchdog.start()

    def _on_stop(self, reason: str) -> None:
        print(f"\n[lifecycle] 停止原因：{reason}")
        self.stop_reason = reason
        if self._server is not None:
            threading.Thread(target=self._server.shutdown, daemon=True).start()

    def connect(self):
        return db.connect_secure(self.db_path, key_file=self.key_path)

    def days(self, limit: int = 180) -> list[dict]:
        """按**本地自然日**聚合。一天就是一段对话，不再往下分 session。"""
        conn = self.connect()
        try:
            tz_name = db.get_profile(conn).get("timezone")
            rows = conn.execute(
                "SELECT role, content, message_time FROM messages ORDER BY id"
            ).fetchall()
        finally:
            conn.close()

        buckets: dict[str, dict] = {}
        for row in rows:
            local = local_datetime(row["message_time"], tz_name)
            if local is None:
                continue
            day = local.strftime("%Y-%m-%d")
            bucket = buckets.setdefault(
                day, {"day": day, "turns": 0, "messages": 0,
                      "preview": "", "first": "", "last": ""}
            )
            bucket["messages"] += 1
            stamp = local.strftime("%H:%M")
            bucket["first"] = bucket["first"] or stamp
            bucket["last"] = stamp
            if row["role"] == "user":
                bucket["turns"] += 1
                bucket["preview"] = " ".join((row["content"] or "").split())[:48]

        return sorted(buckets.values(), key=lambda b: b["day"], reverse=True)[:limit]

    def history(self, days: list[str], limit: int = 3000) -> list[dict]:
        """把若干天的对话合并成一条时间线。

        注意：不能"先取最早 N 条再按日期过滤"——库一旦超过 N 条，最近的记录
        永远进不了候选，表现为"当天历史是空的"。这里改成先算出目标日期的
        UTC 区间，用 SQL 只捞这段时间。
        """
        wanted = set(days)
        conn = self.connect()
        try:
            tz_name = db.get_profile(conn).get("timezone")
            windows = _utc_windows(wanted, tz_name)
            if windows:
                clause = " OR ".join(
                    "(message_time >= ? AND message_time < ?)" for _ in windows
                )
                params = [bound for pair in windows for bound in pair]
                rows = conn.execute(
                    "SELECT id, role, content, message_time FROM messages "
                    f"WHERE {clause} ORDER BY id",
                    params,
                ).fetchall()
            else:
                rows = conn.execute(
                    "SELECT id, role, content, message_time FROM messages ORDER BY id LIMIT ?",
                    (limit,),
                ).fetchall()
        finally:
            conn.close()

        out = []
        for row in rows:
            local = local_datetime(row["message_time"], tz_name)
            if local is None:
                continue
            day = local.strftime("%Y-%m-%d")
            if wanted and day not in wanted:
                continue
            out.append({
                "id": row["id"],
                "role": row["role"],
                "text": row["content"],
                "day": day,
                "at": local.strftime("%H:%M"),
            })
        return out

    def delete_messages(self, days: list[str], message_ids: list[int]) -> dict:
        """删掉指定的消息，或整天的消息。返回删除条数与索引状态。"""
        conn = self.connect()
        try:
            tz_name = db.get_profile(conn).get("timezone")
            targets = {int(i) for i in message_ids}
            if days:
                wanted = set(days)
                for row in conn.execute("SELECT id, message_time FROM messages"):
                    local = local_datetime(row["message_time"], tz_name)
                    if local and local.strftime("%Y-%m-%d") in wanted:
                        targets.add(int(row["id"]))
            if not targets:
                return {"deleted": 0, "fts_ok": db.verify_fts(conn).ok}

            ids = sorted(targets)
            cluster = f"msg-{uuid4().hex[:10]}"
            now = db.utc_now()
            with db.tx(conn):
                # 先清掉从这些消息派生出来的记忆。
                # 只删原文是不够的：记忆里有同一句话的内容，删了消息它还会被召回
                # （实测：删掉「我养了只猫叫团子」后，从记忆里照样召回到猫）。
                orphan_memories = _memories_referencing(conn, set(ids))
                if orphan_memories:
                    conn.executemany(
                        "DELETE FROM memory_items WHERE id = ?",
                        [(i,) for i in orphan_memories],
                    )
                    db.delete_memory_fts(conn, orphan_memories)
                    for memory_id in orphan_memories:
                        conn.execute(
                            "INSERT INTO deletion_target "
                            "(cluster_id, target_kind, target_id, deleted_at) "
                            "VALUES (?, 'memory', ?, ?)",
                            (cluster, str(memory_id), now),
                        )

                for start in range(0, len(ids), 400):
                    part = ids[start : start + 400]
                    marks = ", ".join("?" * len(part))
                    conn.execute(f"DELETE FROM messages WHERE id IN ({marks})", part)
                db.delete_message_fts(conn, ids)
                for message_id in ids:
                    conn.execute(
                        "INSERT INTO deletion_target (cluster_id, target_kind, target_id, deleted_at) "
                        "VALUES (?, 'message', ?, ?)",
                        (cluster, str(message_id), now),
                    )
                conn.execute(
                    "UPDATE data_revision SET revision = revision + 1, updated_at = ? WHERE id = 1",
                    (now,),
                )
            report = db.verify_fts(conn)
            return {
                "deleted": len(ids),
                "memories_deleted": len(orphan_memories),
                "fts_ok": report.ok,
            }
        finally:
            conn.close()

    def provider(self):
        with self._provider_lock:
            if self._provider is None:
                self._provider = self.provider_factory()
            return self._provider

    def extract_async(self, conn_factory, payload: dict) -> None:
        def run() -> None:
            try:
                conn = conn_factory()
                try:
                    extract.extract_turn(
                        conn, self.provider(),
                        user_text=payload["user_text"],
                        assistant_text=payload["assistant_text"],
                        evidence_ids=payload["evidence_ids"],
                        turn_id=payload["turn_id"],
                    )
                finally:
                    conn.close()
            except Exception:  # noqa: BLE001 - 抽取失败绝不影响这一轮
                pass

        threading.Thread(target=run, daemon=True).start()


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.0"
    server_version = "companion-web/0.1"
    app: App = None  # type: ignore[assignment]

    def log_message(self, fmt, *args):  # 静音默认访问日志
        pass

    def _split(self) -> tuple[str, dict[str, str]]:
        parsed = urlsplit(self.path)
        query = {k: v[0] for k, v in parse_qs(parsed.query).items()}
        return parsed.path, query

    # ── GET
    def do_GET(self) -> None:  # noqa: N802
        route, query = self._split()

        if route in ("/", "/index.html"):
            body = INDEX.read_bytes()
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            return

        if route == "/api/hello":
            page_id = self.app.presences.hello(query.get("page"))
            self.app.watchdog.touch()
            self._json({
                "page_id": page_id,
                "pages": self.app.presences.count(),
                "bye_grace": self.app.watchdog.policy.bye_grace,
            })
            return

        if route == "/api/days":
            self._json(self.app.days())
            return

        if route == "/api/history":
            raw = query.get("days", "")
            days = [d for d in (x.strip() for x in raw.split(",")) if d]
            self._json(self.app.history(days))
            return

        if route == "/api/health":
            import os

            self.app.watchdog.touch()
            payload = {
                "ok": True,
                "security": db.security_mode("keyed") if db.sqlcipher_available() else "plaintext-dev",
                "schema": db.latest_version(),
                "model": getattr(self.app._provider, "model", None)
                or os.environ.get("COMPANION_MODEL", "deepseek-flash"),
                "pages": self.app.presences.count(),
            }
            self._json(payload)
            return

        if route == "/api/ping":
            self.app.watchdog.touch()
            pages = self.app.presences.touch(query.get("page"))
            self._json({
                "ok": True,
                "pages": pages,
                "idle_timeout": self.app.watchdog.policy.idle_timeout,
            })
            return

        self._json({"error": "not found"}, status=404)

    # ── POST
    def do_POST(self) -> None:  # noqa: N802
        route, query = self._split()

        if route == "/api/bye":
            # 最后一个页面离开时才停；多标签页关掉一个不影响
            remaining = self.app.presences.bye(query.get("page"))
            stop_in = None
            if remaining == 0:
                self.app.watchdog.schedule_stop()
                stop_in = self.app.watchdog.policy.bye_grace
                print(f"[presence] 页面已关闭，{stop_in:.1f}s 后停止服务")
            else:
                print(f"[presence] 一个页面离开，还剩 {remaining} 个")
            self._json({"ok": True, "pages": remaining, "stop_in": stop_in})
            return

        if route == "/api/delete":
            length = int(self.headers.get("Content-Length") or 0)
            try:
                body = json.loads(self.rfile.read(length) or b"{}")
            except json.JSONDecodeError:
                self._json({"error": "bad json"}, status=400)
                return
            days = [str(d) for d in (body.get("days") or [])]
            ids = [int(i) for i in (body.get("message_ids") or []) if str(i).isdigit()]
            if not days and not ids:
                self._json({"error": "nothing to delete"}, status=400)
                return
            result = self.app.delete_messages(days, ids)
            print(f"[delete] days={days} ids={ids} -> {result}")
            self._json(result)
            return

        if route != "/api/chat":
            self._json({"error": "not found"}, status=404)
            return

        self.app.watchdog.touch()
        self.app.presences.touch(query.get("page"))

        length = int(self.headers.get("Content-Length") or 0)
        try:
            payload = json.loads(self.rfile.read(length) or b"{}")
        except json.JSONDecodeError:
            self._json({"error": "bad json"}, status=400)
            return

        text = (payload.get("text") or "").strip()
        if not text:
            self._json({"error": "empty text"}, status=400)
            return
        session_id = payload.get("session_id") or "default"

        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream; charset=utf-8")
        self.send_header("Cache-Control", "no-cache")
        self.send_header("X-Accel-Buffering", "no")
        self.send_header("Connection", "close")
        self.end_headers()

        conn = self.app.connect()
        disconnected = False
        try:
            def on_text(chunk: str) -> None:
                nonlocal disconnected
                if disconnected:
                    return
                try:
                    self._event({"type": "delta", "text": chunk})
                except (BrokenPipeError, ConnectionResetError):
                    disconnected = True

            with self.app.write_lock:
                # 会话单位是"天"：不传 session_id，由 run_turn 按本地日期决定
                result = run_turn(
                    conn, self.app.provider(), text,
                    persona=PERSONA,
                    boundaries=BOUNDARIES,
                    on_text=on_text,
                )

            usage = result.completion.usage
            self._event({
                "type": "done",
                "reply": result.reply,
                "risk": result.signals.risk,
                "recall_count": len(result.recall.hits),
                "lexicon_count": len(result.recall.lexicon),
                "cache_hit": usage.cache_hit_tokens,
                "prompt_tokens": usage.prompt_tokens,
                "notes": result.notes,
            })

            if result.persisted:
                rows = conn.execute(
                    "SELECT id FROM messages ORDER BY id DESC LIMIT 2"
                ).fetchall()
                ids = [r["id"] for r in reversed(rows)][:1]
                self.app.extract_async(self.app.connect, {
                    "user_text": text,
                    "assistant_text": result.reply,
                    "evidence_ids": ids,
                    "turn_id": f"{session_id}:{result.turn_index}",
                })
        except (BrokenPipeError, ConnectionResetError):
            pass
        except Exception as exc:  # noqa: BLE001
            try:
                self._event({"type": "error", "message": f"{type(exc).__name__}: {exc}"})
            except Exception:  # noqa: BLE001
                pass
        finally:
            conn.close()

    # ── helpers
    def _event(self, payload: dict) -> None:
        self.wfile.write(f"data: {json.dumps(payload, ensure_ascii=False)}\n\n".encode("utf-8"))
        self.wfile.flush()

    def _json(self, payload: dict, *, status: int = 200) -> None:
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


def create_server(
    host: str = "127.0.0.1",
    port: int = 8765,
    *,
    data_dir: str | Path | None = None,
    provider_factory=None,
    lifecycle: Lifecycle | None = None,
) -> tuple[ThreadingHTTPServer, App]:
    app = App(data_dir, provider_factory=provider_factory, lifecycle=lifecycle)
    handler = type("BoundHandler", (Handler,), {"app": app})
    httpd = ThreadingHTTPServer((host, port), handler)
    app.attach_server(httpd)
    return httpd, app


def _existing_instance(port: int, host: str = "127.0.0.1") -> bool:
    """端口上已经有我们的服务？有的话就别再起一个。"""
    import urllib.error
    import urllib.request

    try:
        with urllib.request.urlopen(f"http://{host}:{port}/api/health", timeout=1.5) as resp:
            return b'"ok"' in resp.read(200)
    except (urllib.error.URLError, OSError):
        return False


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="companion 最小网页前端")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8765)
    parser.add_argument("--data", default=str(paths.data_dir()), help="运行期数据目录")
    parser.add_argument("--open", action="store_true", help="启动后自动打开浏览器")
    parser.add_argument("--idle-timeout", type=float, default=90.0,
                        help="页面多久没心跳就停（秒）")
    parser.add_argument("--startup-grace", type=float, default=120.0,
                        help="启动后多久没人访问就停（秒）")
    parser.add_argument("--max-lifetime", type=float, default=12 * 3600.0,
                        help="硬上限（秒）")
    parser.add_argument("--no-lifecycle", action="store_true",
                        help="关掉自动停止，只能 Ctrl+C 结束")
    args = parser.parse_args(argv)

    url = f"http://{args.host}:{args.port}/"

    # 已经有一个在跑：直接开浏览器，不再起第二个
    if _existing_instance(args.port, args.host):
        print(f"服务已在运行：{url}")
        if args.open:
            import webbrowser

            webbrowser.open(url)
        return 0

    policy = Lifecycle(
        idle_timeout=args.idle_timeout,
        startup_grace=args.startup_grace,
        max_lifetime=args.max_lifetime,
        enabled=not args.no_lifecycle,
    )
    try:
        httpd, app = create_server(
            args.host, args.port, data_dir=args.data, lifecycle=policy
        )
    except OSError as exc:
        print(f"端口 {args.port} 无法绑定：{exc}", file=sys.stderr)
        print("可能被别的程序占用；换个端口：--port 8766", file=sys.stderr)
        return 1

    print(f"db      : {app.db_path}（{db.security_mode('keyed') if db.sqlcipher_available() else 'plaintext-dev'}）")
    print(f"listen  : {url}")
    print("提示    : 请访问上面的 http 地址；直接双击 web/index.html 是连不上的。")
    if policy.enabled:
        print(f"自动停止: 页面关闭/刷新后退 {policy.bye_grace:.0f}s；"
              f"心跳断 {policy.idle_timeout:.0f}s；最长 {policy.max_lifetime/3600:.0f}h")
    else:
        print("自动停止: 已关闭（Ctrl+C 结束）")

    if args.open:
        import webbrowser

        threading.Timer(0.6, lambda: webbrowser.open(url)).start()

    try:
        httpd.serve_forever(poll_interval=0.2)
    except KeyboardInterrupt:
        print("\n[lifecycle] 停止原因：ctrl-c")
    finally:
        httpd.server_close()
        app.watchdog.stop()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())














