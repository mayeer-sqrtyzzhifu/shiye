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

import threading
import time
from dataclasses import dataclass

IDLE_TIMEOUT = 90.0
STARTUP_GRACE = 120.0
MAX_LIFETIME = 12 * 3600.0
BYE_GRACE = 3.0


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


class Watchdog:
    """按 `decide_stop` 的结论触发 shutdown。"""

    def __init__(self, policy: Lifecycle, on_stop, poll_interval: float = 2.0) -> None:
        self.policy = policy
        self.poll_interval = poll_interval
        self.on_stop = on_stop
        self.started = time.monotonic()
        self.last_seen: float | None = None
        self.pending_stop_at: float | None = None
        self.reason: str | None = None
        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._run, daemon=True, name="lifecycle")

    # ── 事件
    def touch(self) -> None:
        """任何请求或心跳都算"人还在"，并取消待执行的停止。"""
        self.last_seen = time.monotonic()
        self.pending_stop_at = None

    def schedule_stop(self, delay: float | None = None) -> None:
        self.pending_stop_at = time.monotonic() + (
            self.policy.bye_grace if delay is None else delay
        )

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
