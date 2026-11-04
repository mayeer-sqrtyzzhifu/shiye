from __future__ import annotations

import json
import threading

import httpx
import pytest

from companion.provider import FakeProvider
from web.server import create_server


@pytest.fixture()
def live(tmp_path):
    replies = ["嗯，我在。", '{"memories":[],"lexicon":[]}']
    httpd, app = create_server(
        "127.0.0.1", 0, data_dir=tmp_path,
        provider_factory=lambda: FakeProvider(replies),
    )
    port = httpd.server_address[1]
    thread = threading.Thread(target=httpd.serve_forever, kwargs={"poll_interval": 0.05}, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{port}", app
    finally:
        httpd.shutdown()
        httpd.server_close()
        thread.join(timeout=5)


def test_index_serves_html(live):
    base, _ = live
    response = httpx.get(base + "/", timeout=10)
    assert response.status_code == 200
    assert "Mochi" in response.text
    assert "text/event-stream" not in response.headers.get("content-type", "")


def test_index_html_is_structurally_unique():
    """防止样式/脚本块被重复插入 —— 那会把 CSS 文本直接显示在页面上。"""
    from web.server import INDEX

    html = INDEX.read_text(encoding="utf-8")
    assert html.count("<style>") == 1
    assert html.count("</style>") == 1
    assert html.count("</head>") == 1
    assert html.count("<body>") == 1
    assert html.count("</html>") == 1
    assert html.count("<script>") == 1
    assert html.count("<script>") == html.count("</script>")

    body = html[html.index("<body>"):]
    assert ".typing::after" not in body      # CSS 不许漏进 body
    assert "@keyframes" not in body


def test_served_page_matches_file(live):
    base, _ = live
    from web.server import INDEX

    response = httpx.get(base + "/", timeout=10)
    assert response.content == INDEX.read_bytes()


def test_health_reports_mode(live):
    base, _ = live
    payload = httpx.get(base + "/api/health", timeout=10).json()
    assert payload["ok"] is True
    assert payload["security"] in ("sqlcipher", "plaintext-dev")


def test_chat_streams_delta_then_done(live):
    base, _ = live
    events = []
    with httpx.stream(
        "POST", base + "/api/chat", timeout=30,
        json={"text": "在吗", "session_id": "web-test"},
    ) as response:
        assert response.status_code == 200
        assert response.headers["content-type"].startswith("text/event-stream")
        for line in response.iter_lines():
            if line.startswith("data: "):
                events.append(json.loads(line[6:]))

    kinds = [e["type"] for e in events]
    assert "delta" in kinds
    assert kinds[-1] == "done"
    done = events[-1]
    assert done["reply"] == "嗯，我在。"
    assert set(done) >= {"risk", "recall_count", "cache_hit"}


def test_chat_persists_messages(live):
    base, app = live
    with httpx.stream(
        "POST", base + "/api/chat", timeout=30,
        json={"text": "还记得我吗", "session_id": "web-2"},
    ) as response:
        list(response.iter_lines())

    conn = app.connect()
    try:
        rows = conn.execute("SELECT role, content FROM messages ORDER BY id").fetchall()
    finally:
        conn.close()
    assert [r["role"] for r in rows][-2:] == ["user", "assistant"]
    assert rows[-2]["content"] == "还记得我吗"


def test_empty_text_is_rejected(live):
    base, _ = live
    response = httpx.post(base + "/api/chat", json={"text": "   "}, timeout=10)
    assert response.status_code == 400


def test_unknown_path_is_404(live):
    base, _ = live
    assert httpx.get(base + "/nope", timeout=10).status_code == 404


def test_hello_registers_a_page(live):
    base, app = live
    payload = httpx.get(base + "/api/hello", timeout=10).json()
    assert payload["page_id"]
    assert payload["pages"] == 1
    assert payload["bye_grace"] > 0
    assert app.presences.count() == 1


def test_closing_one_of_two_tabs_keeps_service(live):
    """多开标签页时，关掉一个不该把服务带走（这是"关网页就停"最容易被做坏的地方）。"""
    base, app = live
    a = httpx.get(base + "/api/hello", timeout=10).json()["page_id"]
    b = httpx.get(base + "/api/hello", timeout=10).json()["page_id"]

    first = httpx.post(f"{base}/api/bye?page={a}", timeout=10).json()
    assert first["pages"] == 1
    assert first["stop_in"] is None
    assert app.watchdog.pending_stop_at is None

    last = httpx.post(f"{base}/api/bye?page={b}", timeout=10).json()
    assert last["pages"] == 0
    assert last["stop_in"] is not None
    assert app.watchdog.pending_stop_at is not None


def test_ping_keeps_page_alive(live):
    base, app = live
    page = httpx.get(base + "/api/hello", timeout=10).json()["page_id"]
    before = app.watchdog.last_seen
    payload = httpx.get(f"{base}/api/ping?page={page}", timeout=10).json()
    assert payload["ok"] is True
    assert payload["pages"] == 1
    assert app.watchdog.last_seen is not None
    assert before is None or app.watchdog.last_seen >= before


def test_ping_cancels_pending_stop(live):
    base, app = live
    page = httpx.get(base + "/api/hello", timeout=10).json()["page_id"]
    httpx.post(f"{base}/api/bye?page={page}", timeout=10)
    assert app.watchdog.pending_stop_at is not None

    httpx.get(f"{base}/api/ping?page={page}", timeout=10)
    assert app.watchdog.pending_stop_at is None


def test_stale_page_does_not_block_shutdown(live):
    """崩溃/强杀的标签页会留下幽灵页面；不清掉的话"关页面即停"永久失效。"""
    base, app = live
    alive = httpx.get(base + "/api/hello", timeout=10).json()["page_id"]
    ghost = httpx.get(base + "/api/hello", timeout=10).json()["page_id"]

    # 把幽灵页面的心跳时间推到很久以前
    app.presences._pages[ghost] = 0.0

    payload = httpx.post(f"{base}/api/bye?page={alive}", timeout=10).json()
    assert payload["pages"] == 0
    assert payload["stop_in"] is not None
    assert app.watchdog.pending_stop_at is not None


def test_sessions_are_grouped_with_preview(live):
    """左侧日期栏的数据源：每个会话一条，带本地日期与预览。"""
    base, app = live
    conn = app.connect()
    try:
        for sid, text in (("s-b", "第二个会话"), ("s-a", "第一个会话")):
            conn.execute(
                "INSERT INTO messages (role, content, message_time, created_at, session_id) "
                "VALUES ('user', ?, '2026-11-04T12:00:00Z', '2026-11-04T12:00:00Z', ?)",
                (text, sid),
            )
    finally:
        conn.close()

    items = httpx.get(base + "/api/sessions", timeout=10).json()
    by_id = {i["session_id"]: i for i in items}
    assert {"s-a", "s-b"} <= set(by_id)
    assert by_id["s-b"]["preview"] == "第二个会话"
    assert len(by_id["s-b"]["day"]) == 10          # YYYY-MM-DD
    assert by_id["s-b"]["turns"] >= 1


def test_history_returns_messages_in_order(live):
    base, app = live
    conn = app.connect()
    try:
        for role, text in (("user", "第一句"), ("assistant", "回应一"),
                           ("user", "第二句"), ("assistant", "回应二")):
            conn.execute(
                "INSERT INTO messages (role, content, message_time, created_at, session_id) "
                "VALUES (?, ?, '2026-11-04T12:00:00Z', '2026-11-04T12:00:00Z', 'h1')",
                (role, text),
            )
    finally:
        conn.close()

    items = httpx.get(base + "/api/history?session=h1", timeout=10).json()
    assert [i["text"] for i in items] == ["第一句", "回应一", "第二句", "回应二"]
    assert items[0]["role"] == "user"
    assert items[1]["at"]                          # 转成了本地时间字符串


def test_history_of_unknown_session_is_empty(live):
    base, _ = live
    assert httpx.get(base + "/api/history?session=nope", timeout=10).json() == []

