"""L0 情绪 / 风险标志位（§5.2）。

不调模型、不拦截，只置标志。设计取向是**高召回低精度**：误报的代价只是
"回得温和一点 + 附资源"，漏报的代价是危机场景失效——这个不对称决定了
这里宁可错抓。

两个必须处理的反向问题：

1. **谐谑与夸张**：「笑死」「想死你了」「累死了」命中"死"字但不是求助。
   直接按词表判会把日常对话变成危机对话。所以先用语料里的固定搭配把
   这些片段**挖掉**，再做危机匹配。
2. **只是心情不好 ≠ 风险**：单纯一个「累」不该触发安全边界，否则每轮都
   挂安全提示，用户很快就会把它当噪音。要求"负面词 ≥2"或"负面词 + 强度
   副词"才算 L1。
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

# 明确的自伤 / 自杀表达
CRISIS_STRICT = (
    "自杀", "不想活", "不想醒来", "不想醒", "活着没意思", "活着没意义",
    "结束自己", "伤害自己", "自残", "死了算了", "想去死", "一了百了",
    "消失掉", "从这个世界消失",
)

# 隐晦、告别式：真实危机最常见的说法，纯词表最容易漏的一类
CRISIS_VAGUE = (
    "撑不住", "撑不下去", "熬不下去", "顶不住", "以后不用管我",
    "别管我了", "不用找我了", "没什么好留恋", "想睡过去",
)

# 谐谑 / 夸张：命中这些片段的部分不参与危机判定
IDIOM = (
    "笑死", "想死你", "想死我", "死你", "累死", "饿死", "困死", "气死",
    "吓死", "烦死", "热死", "冷死", "死党", "死磕", "死机", "社死",
)

DISTRESS = (
    "难受", "想哭", "哭了", "失眠", "睡不着", "焦虑", "崩溃", "很累", "好累",
    "压力", "被骂", "委屈", "孤独", "难受", "痛苦", "抑郁", "心累",
)

DEPENDENCY = ("只有你懂", "离不开你", "只剩你", "没人懂我", "只有你陪")

INTENSIFIERS = ("很", "太", "特别", "非常", "真的", "快", "彻底", "完全")

_NEGATIVE = DISTRESS + DEPENDENCY + CRISIS_VAGUE + CRISIS_STRICT
_POSITIVE = ("开心", "高兴", "不错", "顺利", "喜欢", "谢谢", "哈哈", "期待")


@dataclass(frozen=True)
class Signals:
    risk: int = 0                 # 0 正常 / 1 轻度敏感 / 2 危机
    valence: str = "neutral"      # negative | neutral | positive
    intensity: int = 0            # 0..2
    matched: tuple[str, ...] = ()
    reasons: tuple[str, ...] = field(default_factory=tuple)

    @property
    def needs_safety(self) -> bool:
        return self.risk > 0


def _mask_idioms(text: str) -> str:
    """把谐谑搭配替换成等长占位，避免「笑死」被当成危机。"""
    masked = text
    for idiom in IDIOM:
        masked = masked.replace(idiom, "＿" * len(idiom))
    return masked


def _hits(text: str, words: tuple[str, ...]) -> list[str]:
    return [w for w in words if w in text]


def scan(text: str) -> Signals:
    if not text or not text.strip():
        return Signals()

    masked = _mask_idioms(text)

    strict = _hits(masked, CRISIS_STRICT)
    vague = _hits(masked, CRISIS_VAGUE)
    if strict or vague:
        return Signals(
            risk=2,
            valence="negative",
            intensity=2,
            matched=tuple(strict + vague),
            reasons=("crisis",),
        )

    dependency = _hits(masked, DEPENDENCY)
    distress = _hits(masked, DISTRESS)
    intensified = any(w in masked for w in INTENSIFIERS)

    intensity = 0
    if distress:
        intensity = 1
        if len(distress) >= 2 or intensified:
            intensity = 2

    if dependency:
        return Signals(risk=1, valence="negative", intensity=max(intensity, 1),
                       matched=tuple(dependency), reasons=("dependency",))

    if distress and (len(distress) >= 2 or intensified):
        return Signals(risk=1, valence="negative", intensity=intensity,
                       matched=tuple(distress), reasons=("distress",))

    positive = _hits(masked, _POSITIVE)
    negative = _hits(masked, _NEGATIVE)
    valence = "positive" if positive and not negative else ("negative" if negative else "neutral")
    return Signals(risk=0, valence=valence, intensity=intensity,
                   matched=tuple(distress), reasons=("distress",) if distress else ())


def describe(signals: Signals) -> str:
    """给会话态 / 日志用的一行摘要。"""
    label = {0: "平静", 1: "轻度敏感", 2: "危机"}[signals.risk]
    return f"{label}（{signals.valence}/{signals.intensity}）"
