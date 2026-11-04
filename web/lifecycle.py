"""服务生命周期（§「什么时候停」）。

三个独立的停止条件，任何一个成立就退出：

1. **页面说再见** —— `beforeunload` 发 `sendBeacon('/api/bye')`，延迟 3 秒再停，
   给"刷新页面"留出撤销机会（新页面的第一次 ping 会取消它）。
2. **心跳超时** —— 页面每 20 秒 ping 一次；90 秒没动静就认为页面已经关了。
   后台标签页的定时器会被浏览器降频，所以阈值不能太紧。
3. **硬上限** —— 12 小时。防的是"心跳一直没断但人早走了"（比如锁屏没关浏览器）。

另外还有一条：**启动后一直没人来**（默认 120 秒）也停 —— 避免用户双击了
启动器却忘了看浏览器，留下一个空跑的进程。

规则写成纯函数，便于单测：`decide_stop(...)`。
"""

from __future__ import annotations

import secrets
import threading
import time
from dataclasses import dataclass

IDLE_TIMEOUT = 90.0
STARTUP_GRACE = 120.0
MAX_LIFETIME = 12 * 3600.0
BYE_GRACE = 1.5


@dataclass(frozen=True)
class Lifecycle:
    idle_timeout: float = IDLE_TIMEOUT
    startup_grace: float = STARTUP_GRACE
    max_lifetime: float = MAX_LIFETIME
    bye_grace: float = BYE_GRACE
    enabled: bool = True


def decide_stop(
    *,
    now: float,
    started: float,
    last_seen: float | None,
    pending_stop_at: float | None,
    policy: Lifecycle,
) -> str | None:
    """返回停止原因，或 None 表示继续跑。纯函数，便于测试。"""
    if not policy.enabled:
        return None

    if pending_stop_at is not None and now >= pending_stop_at:
        return "bye"

    if now - started >= policy.max_lifetime:
        return "max-lifetime"

    if last_seen is None:
        if now - started >= policy.startup_grace:
            return "never-opened"
        return None

    if now - last_seen >= policy.idle_timeout:
        return "idle"
    return None


class Presences:
    """登记当前开着的页面。

    为什么要登记而不是"收到 bye 就停"：
    浏览器可以开多个标签页，关掉其中一个不该把服务带走。只有**最后一个**
    页面离开时才是"网页关闭"。同时页面会崩溃/被强杀，所以 idle 仍然是兜底。
    """

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._pages: dict[str, float] = {}

    def hello(self, page_id: str | None = None) -> str:
        pid = page_id or f"p{secrets.token_hex(4)}"
        with self._lock:
            self._pages[pid] = time.monotonic()
        return pid

    def touch(self, page_id: str | None) -> int:
        """记一次活跃；未知页面自动登记。返回当前页面数。"""
        with self._lock:
            if page_id:
                self._pages[page_id] = time.monotonic()
            return len(self._pages)

    def bye(self, page_id: str | None) -> int:
        """页面离开；返回剩下的页面数。"""
        with self._lock:
            if page_id:
                self._pages.pop(page_id, None)
            return len(self._pages)

    def count(self) -> int:
        with self._lock:
            return len(self._pages)

    def expired(self, idle: float) -> int:
        """清掉长时间没心跳的页面（崩溃/强杀），返回清理数量。"""
        now = time.monotonic()
        with self._lock:
            dead = [p for p, seen in self._pages.items() if now - seen >= idle]
            for pid in dead:
                self._pages.pop(pid, None)
            return len(dead)


class Watchdog:
    """按 `decide_stop` 的结论触发 shutdown。"""

    def __init__(self, policy: Lifecycle, on_stop, poll_interval: float = 0.5) -> None:
        self.policy = policy
        self.poll_interval = poll_interval
        self.on_stop = on_stop
        self.started = time.monotonic()
        self.last_seen: float | None = None
        self.pending_stop_at: float | None = None
        self.reason: str | None = None
        self._lock = threading.Lock()
        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._run, daemon=True, name="lifecycle")

    # ── 事件
    def touch(self) -> None:
        """任何请求或心跳都算"人还在"，并取消待执行的停止。"""
        with self._lock:
            self.last_seen = time.monotonic()
            self.pending_stop_at = None

    def schedule_stop(self, delay: float | None = None) -> None:
        """排一次延迟停止。

        用定时器而不是等轮询：否则"关页面 → 进程退出"会多出一个轮询周期的
        延迟（实测 3.2s，看起来像没停）。到期时核对 token —— 期间只要有人
        ping，`pending_stop_at` 已经被清空，这次停止就作废。
        """
        wait = self.policy.bye_grace if delay is None else delay
        with self._lock:
            self.pending_stop_at = time.monotonic() + wait
            token = self.pending_stop_at
        threading.Timer(wait, self._fire_if_current, args=(token,)).start()

    def _fire_if_current(self, token: float) -> None:
        with self._lock:
            if self.pending_stop_at != token:
                return                      # 被 ping / hello 撤销了
            self.pending_stop_at = None
            self.reason = "bye"
        self.on_stop("bye")

    def start(self) -> None:
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()

    # ── 主循环
    def _run(self) -> None:
        while not self._stop.wait(self.poll_interval):
            reason = decide_stop(
                now=time.monotonic(),
                started=self.started,
                last_seen=self.last_seen,
                pending_stop_at=self.pending_stop_at,
                policy=self.policy,
            )
            if reason:
                self.reason = reason
                self.on_stop(reason)
                return



