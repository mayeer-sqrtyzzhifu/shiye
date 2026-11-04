from __future__ import annotations

from web.lifecycle import Lifecycle, Watchdog, decide_stop

P = Lifecycle(idle_timeout=90.0, startup_grace=120.0, max_lifetime=3600.0, bye_grace=3.0)


def check(**kwargs):
    base = dict(started=0.0, last_seen=None, pending_stop_at=None, policy=P)
    base.update(kwargs)
    return decide_stop(**base)


def test_disabled_never_stops():
    policy = Lifecycle(enabled=False)
    assert decide_stop(now=10**9, started=0.0, last_seen=None,
                       pending_stop_at=0.0, policy=policy) is None


def test_nobody_opened_the_page():
    assert check(now=10.0) is None                 # 还在宽限期内
    assert check(now=121.0) == "never-opened"      # 一直没人来


def test_idle_after_page_closed():
    assert check(now=100.0, last_seen=90.0) is None
    assert check(now=200.0, last_seen=90.0) == "idle"


def test_bye_is_delayed_then_fires():
    assert check(now=10.0, last_seen=9.0, pending_stop_at=13.0) is None
    assert check(now=14.0, last_seen=9.0, pending_stop_at=13.0) == "bye"


def test_hard_lifetime_cap():
    assert check(now=3600.0, last_seen=3599.0) == "max-lifetime"


def test_touch_cancels_pending_stop():
    """刷新页面时：pagehide 排了停止，新页面的第一次 ping 必须撤销它。"""
    dog = Watchdog(P, on_stop=lambda reason: None)
    dog.schedule_stop()
    assert dog.pending_stop_at is not None
    dog.touch()
    assert dog.pending_stop_at is None
    assert dog.last_seen is not None


def test_watchdog_fires_once_and_names_the_reason():
    import time

    seen: list[str] = []
    dog = Watchdog(Lifecycle(max_lifetime=0.05), on_stop=seen.append, poll_interval=0.02)
    dog.start()
    time.sleep(0.4)
    dog.stop()
    assert seen == ["max-lifetime"]

