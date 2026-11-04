"""`query_time_resolver` —— L0 同步时间解析（§3.3 / §3.4）。

输入：本轮用户文本 + **源消息时间**（不是 ``now()``，见 §3.4 硬规则 1）
输出：``TimeQuery``，交给 L1 决定查哪一路。

三条硬规则在这里落地：

1. 相对时间一律锚定源消息时间；
2. 分级匹配 —— 高置信给精确窗口、中置信给粗窗口、低置信**不查日期路**；
3. 双时间轴 —— ``message`` / ``event`` / ``ambiguous``，后两者由 L1 两轴都查。

不做的事（P2）：农历与节气换算、"生日"这类需要用户档案才能解析的专属日期。
遇到它们只回落到年度窗口 + 低置信，绝不去猜具体月日。
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import date, datetime, timedelta

# 高置信：可进精确窗口；中置信：只能进粗窗口；低于中置信：不查日期路
CONF_HIGH = 0.6
CONF_MID = 0.35

_MESSAGE_MARKERS = ("聊", "说", "提", "讲", "问", "告诉", "对话", "回复", "谈到")
_EVENT_MARKERS = ("发生", "去了", "做了", "见了", "生日", "见面", "搬家", "入职", "分手", "旅行")

_CN_DIGIT = {"零": 0, "〇": 0, "一": 1, "二": 2, "两": 2, "三": 3, "四": 4,
             "五": 5, "六": 6, "七": 7, "八": 8, "九": 9}
_WEEKDAY = {"一": 0, "二": 1, "三": 2, "四": 3, "五": 4, "六": 5, "日": 6, "天": 6}


@dataclass(frozen=True)
class TimeWindow:
    """闭区间 [start, end]，日期用 ISO 字符串。"""
    start: str
    end: str
    conf: float
    label: str  # exact | expanded | coarse | year


@dataclass(frozen=True)
class TimeQuery:
    axis: str                       # message | event | ambiguous
    windows: tuple[TimeWindow, ...]
    conf: float
    is_recurring: bool
    matched: str | None             # 命中的规则名，便于 trace 与回归
    raw: str                        # 命中的原始片段

    @property
    def usable(self) -> bool:
        return self.conf >= CONF_MID

    @property
    def exact(self) -> TimeWindow | None:
        """最近的那个具体日窗口（可能是 candidate —— 见下面的歧义规则）。"""
        for window in self.windows:
            if window.label in ("exact", "candidate", "expanded"):
                return window
        return None


def _cn_int(text: str) -> int | None:
    text = (text or "").strip()
    if not text:
        return None
    if text.isdigit():
        return int(text)
    if text == "十":
        return 10
    if text.startswith("十"):
        tail = _CN_DIGIT.get(text[1:])
        return 10 + tail if tail is not None else None
    if "十" in text:
        head, _, tail = text.partition("十")
        tens = _CN_DIGIT.get(head)
        if tens is None:
            return None
        ones = _CN_DIGIT.get(tail) if tail else 0
        return tens * 10 + (ones if ones is not None else 0)
    return _CN_DIGIT.get(text)


def _num() -> str:
    """Regex fragment for a number written in Arabic or Chinese numerals (1-99)."""
    return r"(?:\d{1,2}|[一二两三四五六七八九十]{1,3})"


_NUM = _num()


def _iso(d: date) -> str:
    return d.isoformat()


def _span(lo: date, hi: date, conf: float, label: str) -> TimeWindow:
    return TimeWindow(_iso(lo), _iso(hi), conf, label)


def _expand(anchor: date, conf: float, days: int = 7) -> list[TimeWindow]:
    return [
        _span(anchor, anchor, conf, "exact"),
        _span(anchor - timedelta(days=days), anchor + timedelta(days=days), conf * 0.9, "expanded"),
    ]


def _coarse(anchor: date, conf: float) -> list[TimeWindow]:
    return [_span(date(anchor.year, 1, 1), date(anchor.year, 12, 31), conf, "coarse")]


def _axis(text: str) -> str:
    has_message = any(m in text for m in _MESSAGE_MARKERS)
    has_event = any(m in text for m in _EVENT_MARKERS)
    if has_message and has_event:
        return "ambiguous"
    if has_message:
        return "message"
    return "ambiguous" if not has_event else "event"


# ───────────────────────────────────────── 规则：绝对日期

_RE_FULL = re.compile(rf"(\d{{4}})\s*[年\-/.]\s*({_NUM})\s*[月\-/.]\s*({_NUM})\s*[日号]?")
_RE_MD = re.compile(rf"({_NUM})\s*月\s*({_NUM})\s*[日号]?")
_RE_MONTH = re.compile(rf"({_NUM})\s*月(?!\s*[初底])")


def _nearest_year(month: int, day: int, ref: date) -> date:
    """Backwards-compatible alias: the nearest single candidate."""
    return _year_candidates(month, day, ref)[0]


def _year_candidates(month: int, day: int, ref: date) -> list[date]:
    """Return up to two nearest occurrences (not "always the past one").

    只有月日时年份是**真歧义**：2026-11-04 说"3 月 5 号"，最近的候选是
    2027-03-05（121 天），比 2026-03-05（244 天）更近。既不能一律取过去，
    也不该武断取最近 —— 按 §3.4「保留歧义」，两个候选都交给 L1。
    """
    candidates: list[date] = []
    for year in (ref.year - 1, ref.year, ref.year + 1):
        try:
            candidates.append(date(year, month, day))
        except ValueError:  # 2 月 29 日等
            continue
    candidates.sort(key=lambda d: abs((d - ref).days))
    return candidates[:2] or [ref]


# ───────────────────────────────────────── 规则：相对日期

_RE_DAY_WORD = {
    "大前天": -3, "前天": -2, "昨天": -1, "昨日": -1,
    "今天": 0, "今日": 0, "明天": 1, "明日": 1, "后天": 2,
}
_RE_WEEKDAY = re.compile(r"(上上|上|这|本|下)\s*(?:周|星期|礼拜)\s*([一二三四五六日天])")
_RE_N_AGO = re.compile(rf"({_NUM})\s*(天|日|周|星期|个?月|年)\s*(前|以前|之前|后|以后|之后)")
_RE_YEAR_PART = re.compile(r"(去年|前年|今年|明年)\s*(?:的)?\s*(?:这个|那个)?\s*(时候|同期|这会儿)")
_RE_VAGUE = re.compile(r"(那阵子|那段时间|前段时间|前一阵|最近|前两天|早些时候|当时|那时候|上次|那天|那次)")
_RE_BARE_YEAR = re.compile(r"(去年|前年|今年|明年)")


def _resolve_relative(text: str, ref: date) -> tuple[list[TimeWindow], float, str, str] | None:
    for word, delta in _RE_DAY_WORD.items():
        if word in text:
            anchor = ref + timedelta(days=delta)
            return _expand(anchor, 0.95), 0.95, "day_word", word

    m = _RE_WEEKDAY.search(text)
    if m:
        scope, wd = m.group(1), _WEEKDAY[m.group(2)]
        monday = ref - timedelta(days=ref.weekday())
        shift = {"上上": -14, "上": -7, "这": 0, "本": 0, "下": 7}[scope]
        anchor = monday + timedelta(days=shift + wd)
        return _expand(anchor, 0.85), 0.85, "weekday", m.group(0)

    m = _RE_N_AGO.search(text)
    if m:
        amount = _cn_int(m.group(1))
        unit, direction = m.group(2), m.group(3)
        if amount:
            sign = -1 if direction in ("前", "以前", "之前") else 1
            if unit in ("天", "日"):
                anchor = ref + timedelta(days=sign * amount)
            elif unit in ("周", "星期"):
                anchor = ref + timedelta(weeks=sign * amount)
            elif unit.endswith("月"):
                anchor = ref + timedelta(days=sign * amount * 30)
            else:
                try:
                    anchor = date(ref.year + sign * amount, ref.month, ref.day)
                except ValueError:  # 2 月 29 日
                    anchor = date(ref.year + sign * amount, ref.month, 28)
            return _expand(anchor, 0.8), 0.8, "n_ago", m.group(0)

    if _RE_YEAR_PART.search(text):
        year = _year_offset(_RE_YEAR_PART.search(text).group(1), ref)
        try:
            anchor = date(year, ref.month, ref.day)
        except ValueError:
            anchor = date(year, ref.month, 28)
        return _expand(anchor, 0.75), 0.75, "year_part", text

    m = _RE_VAGUE.search(text)
    if m:
        # 不在这里收口：交给 resolve 在绝对/年份规则之后兜底。
        # 否则「去年生日那天」会被"那天"抢先匹配成 0.2 的模糊指代。
        return None

    return None


def _year_offset(word: str, ref: date) -> int:
    if word in ("去年",):
        return ref.year - 1
    if word == "前年":
        return ref.year - 2
    if word == "明年":
        return ref.year + 1
    return int(word) if word.isdigit() else ref.year


# ───────────────────────────────────────── 组合

def _resolve_birthday(text: str, ref: date, profile: dict[str, str] | None):
    """「生日」类表达。没有档案就不猜 —— 只回落到年度粗窗口（§3.4 硬规则 2）。"""
    if not profile or "生日" not in text:
        return None
    md = profile.get("birthday")
    if not md or len(md) < 5:
        return None
    try:
        month, day = int(md[0:2]), int(md[3:5])
    except ValueError:
        return None

    year_word = next((w for w in ("去年", "前年", "今年", "明年") if w in text), None)
    if year_word:
        try:
            anchor = date(_year_offset(year_word, ref), month, day)
        except ValueError:
            return None
        return _expand(anchor, 0.9, days=3), 0.9, "birthday", f"{year_word}生日"

    candidates = _year_candidates(month, day, ref)
    if not candidates:
        return None
    windows = [TimeWindow(_iso(c), _iso(c), 0.8, "candidate") for c in candidates]
    windows.append(
        _span(min(candidates) - timedelta(days=3),
              max(candidates) + timedelta(days=3), 0.72, "expanded")
    )
    return windows, 0.8, "birthday", "生日"


def resolve(
    text: str,
    *,
    reference_time: datetime | str | None = None,
    profile: dict[str, str] | None = None,
) -> TimeQuery:
    """Parse one user utterance into a TimeQuery.

    ``reference_time`` 必须是**源消息时间**。字符串按 ISO 解析；缺省用当天，
    仅用于开发与测试。
    """
    if isinstance(reference_time, str):
        reference_time = datetime.fromisoformat(reference_time.replace("Z", "+00:00"))
    ref_dt = reference_time or datetime.now()
    ref = ref_dt.date()
    text = (text or "").strip()
    axis = _axis(text)

    def build(windows, conf, matched, raw) -> TimeQuery:
        return TimeQuery(
            axis=axis,
            windows=tuple(windows),
            conf=conf,
            is_recurring=bool(re.search(r"每年|周年|纪念日", text)),
            matched=matched,
            raw=raw,
        )

    if not text:
        return build([], 0.0, None, "")

    # 1) 绝对日期：完整年月日
    m = _RE_FULL.search(text)
    if m:
        y, mo, d = int(m.group(1)), _cn_int(m.group(2)), _cn_int(m.group(3))
        if mo and d:
            try:
                anchor = date(y, mo, d)
                return build(_expand(anchor, 0.95), 0.95, "full_date", m.group(0))
            except ValueError:
                pass

    # 2) 档案里能解析的专属日期（生日）。必须在相对/年份规则之前，
    #    否则「去年生日」会被"去年"抢走，降级成年度粗窗口。
    birthday = _resolve_birthday(text, ref, profile)
    if birthday:
        windows, conf, matched, raw = birthday
        return build(windows, conf, matched, raw)

    # 3) 相对表达（含"去年这个时候"、N 天前、上周三等）
    relative = _resolve_relative(text, ref)
    if relative:
        windows, conf, matched, raw = relative
        if matched == "vague":
            return build([], conf, matched, raw)
        return build(windows, conf, matched, raw)

    # 4) 只有月日：年份是真歧义，两个候选都交出
    m = _RE_MD.search(text)
    if m:
        mo, d = _cn_int(m.group(1)), _cn_int(m.group(2))
        if mo and d:
            try:
                cands = _year_candidates(mo, d, ref)
            except ValueError:
                cands = []
            if cands:
                windows = [TimeWindow(_iso(c), _iso(c), 0.65, "candidate") for c in cands]
                lo = min(cands) - timedelta(days=7)
                hi = max(cands) + timedelta(days=7)
                windows.append(_span(lo, hi, 0.585, "expanded"))
                return build(windows, 0.65, "month_day", m.group(0))

    # 5) 只有月份：粗窗口
    m = _RE_MONTH.search(text)
    if m:
        mo = _cn_int(m.group(1))
        if mo and 1 <= mo <= 12:
            lo = date(ref.year, mo, 1)
            hi = date(ref.year + (mo == 12), 12 if mo == 12 else mo + 1, 1) - timedelta(days=1)
            return build([_span(lo, hi, 0.55, "coarse")], 0.55, "month_only", m.group(0))

    # 6) 只有年份
    m = _RE_BARE_YEAR.search(text)
    if m:
        year = _year_offset(m.group(1), ref)
        return build(_coarse(date(year, 6, 1), 0.5), 0.5, "year_only", m.group(0))

    # 7) 模糊指代兜底：能指到"某一段/某一件事"，指不到具体日期
    m = _RE_VAGUE.search(text)
    if m:
        return build([], 0.2, "vague", m.group(0))

    # 8) 什么都没解析出来
    return build([], 0.0, None, text)


def resolve_interval(
    text: str,
    *,
    reference_time: datetime | str | None = None,
    profile: dict[str, str] | None = None,
) -> TimeQuery:
    """区间表达（"3月1号到3月10号""那阵子"）→ 一个覆盖两端的窗口。"""
    parts = re.split(r"\s*(?:到|至|~|—|–)\s*", (text or "").strip())
    if len(parts) == 2 and all(parts):
        left = resolve(parts[0], reference_time=reference_time, profile=profile)
        right = resolve(parts[1], reference_time=reference_time, profile=profile)
        if left.exact and right.exact:
            lo, hi = sorted((left.exact.start, right.exact.end))
            conf = min(left.conf, right.conf) * 0.95
            return TimeQuery(
                axis=_axis(text),
                windows=(TimeWindow(lo, hi, conf, "exact"),),
                conf=conf,
                is_recurring=False,
                matched="interval",
                raw=text,
            )
    return resolve(text, reference_time=reference_time)
