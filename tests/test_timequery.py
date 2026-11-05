from __future__ import annotations

from datetime import date

import pytest

from companion.timequery import CONF_HIGH, CONF_MID, resolve, resolve_interval

REF = "2026-11-04T09:00:00Z"   # 2026-11-04，周三
REF_DATE = date(2026, 11, 4)


def test_full_date_is_high_confidence():
    q = resolve("2025年3月5日我们去了海边", reference_time=REF)
    assert q.matched == "full_date"
    assert q.conf >= 0.9
    assert q.exact is not None and q.exact.start == "2025-03-05"
    assert q.usable


def test_iso_date_also_parses():
    q = resolve("2025-03-05 那天", reference_time=REF)
    assert q.exact is not None and q.exact.start == "2025-03-05"


def test_bare_month_day_keeps_ambiguity():
    """只有月日时年份是真歧义：两个候选都要交出，不能一律取过去、也不该武断取最近。"""
    march = resolve("3月5号", reference_time=REF)
    assert march.matched == "month_day"
    starts = {w.start for w in march.windows if w.label == "candidate"}
    assert starts == {"2026-03-05", "2027-03-05"}
    # 最近的候选其实是未来那个（121 天），证明没有"一律取最近过去"
    assert march.exact.start == "2027-03-05"

    december = resolve("12月25号", reference_time=REF)
    starts = {w.start for w in december.windows if w.label == "candidate"}
    assert starts == {"2025-12-25", "2026-12-25"}


def test_relative_day_is_anchored_to_message_time():
    q = resolve("昨天我们聊了什么", reference_time=REF)
    assert q.matched == "day_word"
    assert q.exact is not None and q.exact.start == "2026-11-03"
    assert q.axis == "message"

    other = resolve("昨天我们聊了什么", reference_time="2026-01-02T00:00:00Z")
    assert other.exact is not None and other.exact.start == "2026-01-01"


def test_tomorrow_goes_forward():
    q = resolve("明天提醒我", reference_time=REF)
    assert q.exact is not None and q.exact.start == "2026-11-05"


def test_n_days_ago():
    q = resolve("三天前那件事", reference_time=REF)
    assert q.matched == "n_ago"
    assert q.exact is not None and q.exact.start == "2026-11-01"


def test_last_weekday():
    q = resolve("上周三你说过", reference_time=REF)
    assert q.matched == "weekday"
    hit = date.fromisoformat(q.exact.start)
    assert hit.weekday() == 2                      # 周三
    assert 7 <= (REF_DATE - hit).days <= 13        # 落在上一周


def test_year_part_keeps_month_and_day():
    q = resolve("去年这个时候", reference_time=REF)
    assert q.matched == "year_part"
    assert q.exact is not None and q.exact.start == "2025-11-04"


def test_bare_year_gives_coarse_window():
    q = resolve("去年的事", reference_time=REF)
    assert q.matched == "year_only"
    assert q.conf == pytest.approx(0.5)
    assert q.windows[0].start == "2025-01-01"
    assert q.windows[0].end == "2025-12-31"


def test_month_only_is_coarse():
    q = resolve("今年3月", reference_time=REF)
    assert q.matched == "month_only"
    assert q.windows[0].label == "coarse"
    assert q.windows[0].start == "2026-03-01"
    assert q.windows[0].end == "2026-03-31"


@pytest.mark.parametrize("text", ["那阵子", "上次", "那天", "前段时间"])
def test_vague_pronouns_must_not_query_date_path(text):
    q = resolve(text, reference_time=REF)
    assert q.matched == "vague"
    assert q.conf < CONF_MID
    assert q.usable is False
    assert q.windows == ()


def test_high_confidence_offers_exact_then_expanded():
    q = resolve("昨天", reference_time=REF)
    labels = [w.label for w in q.windows]
    assert labels[0] == "exact"
    assert "expanded" in labels
    assert q.conf >= CONF_HIGH


@pytest.mark.parametrize(
    ("text", "axis"),
    [
        ("昨天我们聊了什么", "message"),
        ("去年生日那天我们去了海边", "event"),
        ("昨天", "ambiguous"),
        ("上次说的那件事，后来发生了什么", "ambiguous"),
    ],
)
def test_time_axis(text, axis):
    assert resolve(text, reference_time=REF).axis == axis


def test_recurring_flag():
    assert resolve("每年生日", reference_time=REF).is_recurring is True
    assert resolve("昨天", reference_time=REF).is_recurring is False


def test_interval_builds_one_span():
    q = resolve_interval("3月1号到3月10号", reference_time=REF)
    assert q.matched == "interval"
    assert len(q.windows) == 1
    # 两端各自取最近候选（从 11-04 看 3 月最近的是下一年），并保持同一年、顺序正确
    assert q.windows[0].start == "2027-03-01"
    assert q.windows[0].end == "2027-03-10"
    assert q.windows[0].start[:4] == q.windows[0].end[:4]


def test_interval_falls_back_to_single_resolve():
    q = resolve_interval("昨天", reference_time=REF)
    assert q.matched == "day_word"


def test_empty_text_is_not_usable():
    q = resolve("", reference_time=REF)
    assert q.conf == 0.0
    assert q.usable is False


def test_source_time_never_leaks_to_now():
    """同一句"昨天"在不同 reference_time 下必须给出不同结果（硬规则 1）。"""
    a = resolve("昨天", reference_time="2026-01-02T00:00:00Z")
    b = resolve("昨天", reference_time="2026-06-15T00:00:00Z")
    assert a.exact.start != b.exact.start


def test_month_only_window_does_not_leak_into_next_year():
    """「12月」应该是 12-01..12-31，不是到次年 11 月底。"""
    from datetime import datetime

    from companion import timequery

    q = timequery.resolve("12月", reference_time=datetime(2026, 11, 4, 21, 0), profile={})
    assert [(w.start, w.end) for w in q.windows] == [("2026-12-01", "2026-12-31")]


def test_impossible_dates_are_rejected_not_silently_today():
    """「2月30日」不存在，不能悄悄返回"今天"。"""
    from datetime import datetime

    from companion import timequery

    ref = datetime(2026, 11, 4, 21, 0)
    for text in ("2月30日", "2月29日", "2027年2月29日"):
        q = timequery.resolve(text, reference_time=ref, profile={})
        assert q.windows == (), f"{text} 不该有窗口"
        assert q.conf == 0.0, f"{text} 置信度应为 0"
        assert q.matched == "invalid_date"


def test_message_date_path_uses_local_day_bounds(conn):
    """UTC+8 的「今天」= UTC 昨天的 16:00 到今天 16:00。

    直接把本地日期拼成 "T00:00:00Z" 会错开 8 小时，凌晨的消息就查不到。
    """
    from datetime import datetime

    from companion import db, recall

    db.set_profile(conn, "timezone", "Asia/Shanghai")
    # 本地 2026-11-05 01:00 = UTC 2026-11-04 17:00
    conn.execute(
        "INSERT INTO messages (role, content, message_time, created_at, session_id, no_send) "
        "VALUES ('user','凌晨说的话','2026-11-04T17:00:00Z','2026-11-04T17:00:00Z','s',0)"
    )
    conn.commit()

    result = recall.recall(
        conn, "今天我们聊了什么",
        reference_time=datetime(2026, 11, 5, 9, 0),
        profile={"timezone": "Asia/Shanghai"},
    )
    ids = [h.id for h in result.hits if h.source == "date_message"]
    assert ids, "本地凌晨的消息应被算进「今天」"


