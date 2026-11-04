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
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
# 既能 `python web/server.py` 直接跑，也能 `import web.server`（测试用）
for _p in (str(ROOT), str(ROOT / "src")):
    if _p not in sys.path:
        sys.path.insert(0, _p)

from companion import db, extract  # noqa: E402
from companion.provider import OpenAICompatProvider  # noqa: E402
from companion.turn import run_turn  # noqa: E402
from web.lifecycle import Lifecycle, Watchdog  # noqa: E402

INDEX = Path(__file__).with_name("index.html")

PERSONA = (
    "你是 Mochi，一个安静、记性好的陪伴者。说话简短、具体，不堆形容词。"
    "不急着给建议，除非对方明确要。记得住很久以前的小事，也记得自己说过什么。"
)
BOUNDARIES = (
    "不假装是人类",
    "不替代真实的人际关系",
    "不制造独占感，不宣称只有你懂对方",
    "不阻止对方求助",
)


class App:
    def __init__(
        self,
        data_dir: str | Path,
        provider_factory=None,
        lifecycle: Lifecycle | None = None,
    ) -> None:
        self.data_dir = Path(data_dir)
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

    # ── GET
    def do_GET(self) -> None:  # noqa: N802
        if self.path in ("/", "/index.html"):
            body = INDEX.read_bytes()
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            return
        if self.path == "/api/health":
            import os

            self.app.watchdog.touch()
            payload = {
                "ok": True,
                "security": db.security_mode("keyed") if db.sqlcipher_available() else "plaintext-dev",
                "schema": db.latest_version(),
                "model": getattr(self.app._provider, "model", None)
                or os.environ.get("COMPANION_MODEL", "deepseek-flash"),
            }
            self._json(payload)
            return
        if self.path == "/api/ping":
            self.app.watchdog.touch()
            self._json({
                "ok": True,
                "idle_timeout": self.app.watchdog.policy.idle_timeout,
            })
            return
        self._json({"error": "not found"}, status=404)

    # ── POST
    def do_POST(self) -> None:  # noqa: N802
        if self.path == "/api/bye":
            # 页面关闭时用 sendBeacon 发过来；延迟几秒再停，刷新页面可以撤销
            self.app.watchdog.schedule_stop()
            self._json({"ok": True, "stop_in": self.app.watchdog.policy.bye_grace})
            return

        if self.path != "/api/chat":
            self._json({"error": "not found"}, status=404)
            return

        self.app.watchdog.touch()

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
                result = run_turn(
                    conn, self.app.provider(), text,
                    persona=PERSONA,
                    boundaries=BOUNDARIES,
                    session_id=session_id,
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
    data_dir: str | Path = ROOT / "data",
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
    parser.add_argument("--data", default=str(ROOT / "data"))
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
        httpd.serve_forever()
    except KeyboardInterrupt:
        print("\n[lifecycle] 停止原因：ctrl-c")
    finally:
        httpd.server_close()
        app.watchdog.stop()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
