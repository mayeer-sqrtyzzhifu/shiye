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

