from __future__ import annotations

import json
import threading

import httpx
import pytest

from companion import db
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
    thread = threading.Thread(
        target=httpd.serve_forever, kwargs={"poll_interval": 0.05}, daemon=True
    )
    thread.start()
    try:
        yield f"http://127.0.0.1:{port}", app
    finally:
        httpd.shutdown()
        app.watchdog.stop()
        httpd.server_close()
        thread.join(timeout=5)


def _insert(app, role, text, when, session="s", sync=True):
    conn = app.connect()
    try:
        cur = conn.execute(
            "INSERT INTO messages (role, content, message_time, created_at, session_id) "
            "VALUES (?, ?, ?, ?, ?)",
            (role, text, when, when, session),
        )
        if sync:
            db.upsert_message_fts(conn, cur.lastrowid, text)
        return cur.lastrowid
    finally:
        conn.close()


# ───────────────────────────────────────── 页面

def test_index_serves_html(live):
    base, _ = live
    response = httpx.get(base + "/", timeout=10)
    assert response.status_code == 200
    assert "拾夜" in response.text
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
    assert ".typing::after" not in body
    assert "@keyframes" not in body


def test_served_page_matches_file(live):
    base, _ = live
    from web.server import INDEX

    assert httpx.get(base + "/", timeout=10).content == INDEX.read_bytes()


def test_health_reports_mode(live):
    base, _ = live
    payload = httpx.get(base + "/api/health", timeout=10).json()
    assert payload["ok"] is True
    assert payload["security"] in ("sqlcipher", "plaintext-dev")


def test_unknown_path_is_404(live):
    base, _ = live
    assert httpx.get(base + "/nope", timeout=10).status_code == 404


# ───────────────────────────────────────── 生命周期

def test_hello_registers_a_page(live):
    base, app = live
    payload = httpx.get(base + "/api/hello", timeout=10).json()
    assert payload["page_id"]
    assert payload["pages"] == 1
    assert payload["bye_grace"] > 0
    assert app.presences.count() == 1


def test_closing_one_of_two_tabs_keeps_service(live):
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
    payload = httpx.get(f"{base}/api/ping?page={page}", timeout=10).json()
    assert payload["ok"] is True
    assert payload["pages"] == 1
    assert app.watchdog.last_seen is not None


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
    app.presences._pages[ghost] = 0.0

    payload = httpx.post(f"{base}/api/bye?page={alive}", timeout=10).json()
    assert payload["pages"] == 0
    assert payload["stop_in"] is not None


# ───────────────────────────────────────── 对话

def test_chat_streams_delta_then_done(live):
    base, _ = live
    events = []
    with httpx.stream(
        "POST", base + "/api/chat", timeout=30, json={"text": "在吗"},
    ) as response:
        assert response.status_code == 200
        assert response.headers["content-type"].startswith("text/event-stream")
        for line in response.iter_lines():
            if line.startswith("data: "):
                events.append(json.loads(line[6:]))

    kinds = [e["type"] for e in events]
    assert "delta" in kinds
    assert kinds[-1] == "done"
    assert events[-1]["reply"] == "嗯，我在。"


def test_chat_persists_today(live):
    base, app = live
    with httpx.stream("POST", base + "/api/chat", timeout=30,
                      json={"text": "还记得我吗"}) as response:
        list(response.iter_lines())

    conn = app.connect()
    try:
        rows = conn.execute("SELECT role, content, session_id FROM messages ORDER BY id").fetchall()
    finally:
        conn.close()
    assert [r["role"] for r in rows][-2:] == ["user", "assistant"]
    assert rows[-2]["content"] == "还记得我吗"
    assert rows[-1]["session_id"].startswith("day:")   # 会话单位是天


def test_empty_text_is_rejected(live):
    base, _ = live
    assert httpx.post(base + "/api/chat", json={"text": "   "}, timeout=10).status_code == 400


# ───────────────────────────────────────── 日期栏

def test_days_group_by_local_date(live):
    base, app = live
    _insert(app, "user", "今天第一句", "2026-11-04T12:00:00Z", session="a")
    _insert(app, "assistant", "今天回一句", "2026-11-04T12:01:00Z", session="a")
    _insert(app, "user", "同一句别的 session", "2026-11-04T13:00:00Z", session="b")
    _insert(app, "user", "昨天那句", "2026-11-03T12:00:00Z", session="c")

    days = httpx.get(base + "/api/days", timeout=10).json()
    by_day = {d["day"]: d for d in days}
    assert set(by_day) == {"2026-11-04", "2026-11-03"}
    # 同一天里不同 session 合并成一天
    assert by_day["2026-11-04"]["messages"] == 3
    assert by_day["2026-11-04"]["turns"] == 2
    assert by_day["2026-11-04"]["preview"] == "同一句别的 session"


def test_history_merges_multiple_days(live):
    base, app = live
    _insert(app, "user", "昨天的话", "2026-11-03T12:00:00Z")
    _insert(app, "user", "今天的话", "2026-11-04T12:00:00Z")
    _insert(app, "assistant", "今天的回应", "2026-11-04T12:01:00Z")
    _insert(app, "user", "另一天", "2026-11-01T12:00:00Z")

    items = httpx.get(base + "/api/history?days=2026-11-04,2026-11-03", timeout=10).json()
    assert [i["text"] for i in items] == ["昨天的话", "今天的话", "今天的回应"]
    assert {i["day"] for i in items} == {"2026-11-04", "2026-11-03"}
    assert items[0]["at"] and items[0]["id"]


def test_history_without_days_returns_everything(live):
    base, app = live
    _insert(app, "user", "唯一一句", "2026-11-04T12:00:00Z")
    assert len(httpx.get(base + "/api/history?days=", timeout=10).json()) == 1


# ───────────────────────────────────────── 删除

def test_delete_whole_day(live):
    base, app = live
    _insert(app, "user", "要删的", "2026-11-04T12:00:00Z")
    _insert(app, "assistant", "也要删", "2026-11-04T12:01:00Z")
    _insert(app, "user", "留着的", "2026-11-03T12:00:00Z")

    out = httpx.post(base + "/api/delete", json={"days": ["2026-11-04"]}, timeout=10).json()
    assert out["deleted"] == 2
    assert out["fts_ok"] is True

    left = httpx.get(base + "/api/history?days=", timeout=10).json()
    assert [i["text"] for i in left] == ["留着的"]


def test_delete_multiple_days(live):
    base, app = live
    _insert(app, "user", "四号", "2026-11-04T12:00:00Z")
    _insert(app, "user", "三号", "2026-11-03T12:00:00Z")
    _insert(app, "user", "一号", "2026-11-01T12:00:00Z")

    out = httpx.post(base + "/api/delete",
                     json={"days": ["2026-11-04", "2026-11-03"]}, timeout=10).json()
    assert out["deleted"] == 2
    left = httpx.get(base + "/api/history?days=", timeout=10).json()
    assert [i["text"] for i in left] == ["一号"]


def test_delete_single_message(live):
    base, app = live
    _insert(app, "user", "第一条", "2026-11-04T12:00:00Z")
    _insert(app, "user", "第二条", "2026-11-04T12:01:00Z")

    msg_id = httpx.get(base + "/api/history?days=2026-11-04", timeout=10).json()[0]["id"]
    out = httpx.post(base + "/api/delete", json={"message_ids": [msg_id]}, timeout=10).json()
    assert out["deleted"] == 1

    left = httpx.get(base + "/api/history?days=2026-11-04", timeout=10).json()
    assert [i["text"] for i in left] == ["第二条"]


def test_delete_multiple_selected_messages(live):
    """同一界面里勾选多条再一起删。"""
    base, app = live
    for i, text in enumerate(["一", "二", "三", "四"]):
        _insert(app, "user", text, f"2026-11-04T12:0{i}:00Z")

    items = httpx.get(base + "/api/history?days=2026-11-04", timeout=10).json()
    ids = [i["id"] for i in items]
    out = httpx.post(base + "/api/delete",
                     json={"message_ids": [ids[0], ids[2]]}, timeout=10).json()
    assert out["deleted"] == 2
    assert out["fts_ok"] is True

    left = httpx.get(base + "/api/history?days=2026-11-04", timeout=10).json()
    assert [i["text"] for i in left] == ["二", "四"]

def test_delete_keeps_index_clean(live):
    base, app = live
    _insert(app, "user", "会被删掉的敏感内容", "2026-11-04T12:00:00Z")
    httpx.post(base + "/api/delete", json={"days": ["2026-11-04"]}, timeout=10)

    conn = app.connect()
    try:
        assert db.verify_fts(conn).ok
        assert db.search_messages(conn, "敏感内容") == []
    finally:
        conn.close()


def test_delete_requires_a_target(live):
    base, _ = live
    assert httpx.post(base + "/api/delete", json={}, timeout=10).status_code == 400




def test_deleting_messages_purges_derived_memories(live):
    """删消息必须连带删掉从它抽取出来的记忆。

    否则"删掉的话"会从记忆里被复述回来 —— 实测过：删掉「我养了只猫叫团子」
    之后，召回仍然命中那条记忆。
    """
    base, app = live
    conn = app.connect()
    try:
        cur = conn.execute(
            "INSERT INTO messages (role, content, message_time, created_at, session_id) "
            "VALUES ('user','我养了只猫叫团子','2026-11-04T12:00:00Z','2026-11-04T12:00:00Z','s')"
        )
        mid = cur.lastrowid
        db.upsert_message_fts(conn, mid, "我养了只猫叫团子")
        cur = conn.execute(
            "INSERT INTO memory_items (content, source, source_role, epistemic_status, "
            "evidence_message_ids, created_at, updated_at) VALUES (?,?,?,?,?,?,?)",
            ("用户养了一只猫叫团子。", "extracted", "user", "explicit",
             f"[{mid}]", "2026-11-04T12:00:00Z", "2026-11-04T12:00:00Z"),
        )
        db.upsert_memory_fts(conn, cur.lastrowid, "用户养了一只猫叫团子。")
        conn.commit()
    finally:
        conn.close()

    out = httpx.post(base + "/api/delete", json={"days": ["2026-11-04"]}, timeout=10).json()
    assert out["memories_deleted"] == 1

    conn = app.connect()
    try:
        assert conn.execute("SELECT count(*) c FROM memory_items").fetchone()["c"] == 0
        assert db.search_memories(conn, "团子") == []
    finally:
        conn.close()


def test_delete_keeps_unrelated_memories(live):
    """只删证据被删的记忆，别误伤别的。"""
    base, app = live
    conn = app.connect()
    try:
        cur = conn.execute(
            "INSERT INTO messages (role, content, message_time, created_at, session_id) "
            "VALUES ('user','今天被领导骂了','2026-11-04T12:00:00Z','2026-11-04T12:00:00Z','s')"
        )
        mid = cur.lastrowid
        db.upsert_message_fts(conn, mid, "今天被领导骂了")
        for content, evidence in (("用户被领导骂了。", f"[{mid}]"),
                                  ("用户最近失眠。", "[]")):
            cur = conn.execute(
                "INSERT INTO memory_items (content, source, source_role, epistemic_status, "
                "evidence_message_ids, created_at, updated_at) VALUES (?,?,?,?,?,?,?)",
                (content, "extracted", "user", "explicit", evidence,
                 "2026-11-04T12:00:00Z", "2026-11-04T12:00:00Z"),
            )
            db.upsert_memory_fts(conn, cur.lastrowid, content)
        conn.commit()
    finally:
        conn.close()

    httpx.post(base + "/api/delete", json={"days": ["2026-11-04"]}, timeout=10)
    conn = app.connect()
    try:
        left = [r["content"] for r in conn.execute("SELECT content FROM memory_items")]
        assert left == ["用户最近失眠。"]
    finally:
        conn.close()


def test_history_survives_thousands_of_older_messages(live):
    """库变大后当天历史不能消失。

    旧实现是"先取最早 3000 条再按日期过滤"：一旦超过 3000 条，最近的记录
    根本进不了候选，表现为"当天的历史是空的"。
    """
    base, app = live
    conn = app.connect()
    try:
        conn.executemany(
            "INSERT INTO messages (role, content, message_time, created_at, session_id) "
            "VALUES ('user', ?, ?, ?, 'old')",
            [(f"旧消息 {i}", "2025-01-01T12:00:00Z", "2025-01-01T12:00:00Z")
             for i in range(3005)],
        )
        conn.execute(
            "INSERT INTO messages (role, content, message_time, created_at, session_id) "
            "VALUES ('user','今天最新的一句','2026-11-04T12:00:00Z','2026-11-04T12:00:00Z','new')"
        )
        conn.commit()
    finally:
        conn.close()

    items = httpx.get(base + "/api/history?days=2026-11-04", timeout=20).json()
    assert [i["text"] for i in items] == ["今天最新的一句"]


def test_chat_enqueues_extraction_atomically(live):
    """对话落库时必须在同一事务里入队，不能事后补。"""
    base, app = live
    with httpx.stream("POST", base + "/api/chat", timeout=30,
                      json={"text": "我养了只猫叫团子"}) as response:
        list(response.iter_lines())

    conn = app.connect()
    try:
        row = conn.execute(
            "SELECT status, payload_ref FROM async_task WHERE kind='extract' ORDER BY rowid DESC LIMIT 1"
        ).fetchone()
    finally:
        conn.close()
    assert row is not None, "对话后应产生抽取任务"
    payload = json.loads(row["payload_ref"])
    assert payload["evidence_ids"], "任务里必须带证据消息 id"


def test_extract_worker_processes_jobs(live):
    """worker 会自动把任务跑完，不需要手动触发。"""
    import time

    base, app = live
    with httpx.stream("POST", base + "/api/chat", timeout=30,
                      json={"text": "我养了只猫叫团子"}) as response:
        list(response.iter_lines())

    deadline = time.time() + 15
    status = None
    while time.time() < deadline:
        conn = app.connect()
        try:
            row = conn.execute(
                "SELECT status FROM async_task WHERE kind='extract' ORDER BY rowid DESC LIMIT 1"
            ).fetchone()
        finally:
            conn.close()
        status = row["status"] if row else None
        if status in ("done", "failed"):
            break
        time.sleep(0.3)
    assert status == "done", f"worker 没跑完任务，最终状态 {status}"
