"""日期窗口谓词（§3.4 / §3.5）。

两个容易写错的地方，这里给出唯一实现，避免各处手写 SQL：

1. **区间相交**：事件是 ``[event_start, event_end]``，查询一个点或一个窗口时
   必须用相交判断。``event_start BETWEEN q_start AND q_end`` 会漏掉
   「3 月 1—10 日持续的事件」在 3 月 5 日的查询。
2. **跨年周年窗口**：月日字符串 ``BETWEEN`` 在 ``12-28`` → ``01-05`` 这类
   跨年区间上恒为假（``'01-05' < '12-28'``），必须拆成两段。
"""

from __future__ import annotations


def overlaps_sql(start_col: str = "event_start", end_col: str = "event_end") -> str:
    """SQL fragment for "event interval intersects [q_start, q_end]"."""
    return f"({start_col} <= :q_end AND COALESCE({end_col}, {start_col}) >= :q_start)"


def annual_window_hit(md: str, start_md: str, end_md: str) -> bool:
    """True when a ``MM-DD`` occurrence falls inside an annual window (may cross a year)."""
    if start_md <= end_md:
        return start_md <= md <= end_md
    # 跨年：12-28 → 01-05
    return md >= start_md or md <= end_md


def annual_window_sql(expr: str, start_md: str, end_md: str) -> str:
    """Inlined SQL for the same rule (needed because FTS query plans can't call Python)."""
    if start_md <= end_md:
        return f"({expr} BETWEEN '{start_md}' AND '{end_md}')"
    return f"({expr} >= '{start_md}' OR {expr} <= '{end_md}')"
