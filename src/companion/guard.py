"""输出侧扫描（§5.3）。

输入侧有 `signals` 管风险等级，输出侧同样需要一道检查：模型自己说的话也可能
越界——把情绪问题说成诊断、给出做不到的保证、制造独占感、劝人别求助。

这里**不改写模型的话**。理由：自动改写容易把语气改坏，而且改完的东西不再
可信。扫描只负责标记，把问题暴露出来（写进 notes、在网页上显示），
让人和日志能看到；真正的语气约束应该在人格与提示里解决。
"""

from __future__ import annotations

import re
from dataclasses import dataclass

# severity: 2 = 必须处理（越界/危险建议），1 = 需要留意（越界倾向）
_PATTERNS: tuple[tuple[str, str, int], ...] = (
    (
        "diagnosis",
        r"(你|您)[^。！？\n]{0,4}(这是|是|得了|就是|属于|有)[^。！？\n]{0,6}"
        r"(抑郁症|焦虑症|强迫症|双相|躁郁|精神分裂|人格障碍|进食障碍)",
        2,
    ),
    (
        "anti_help",
        r"(不用|不必|没必要|别|不要去)[^。！？\n]{0,6}(看医生|看心理|找咨询师|去医院|吃药)",
        2,
    ),
    (
        "exclusive",
        r"只有我(懂|理解|在乎|知道)你|你(只)?需(要|得)我|别(人|人)都不懂你",
        1,
    ),
    (
        "guarantee",
        r"我(保证|发誓|承诺)|我(会|将)?永远(不)?(离开|陪着你|在)|你(可以)?永远(相信|依赖)我",
        1,
    ),
    (
        "dependency",
        r"离不开我|没有我(你)?(怎么办|不行)",
        1,
    ),
)

_COMPILED = tuple((name, re.compile(pattern), level) for name, pattern, level in _PATTERNS)


@dataclass(frozen=True)
class OutputScan:
    flags: tuple[str, ...] = ()
    severity: int = 0

    @property
    def ok(self) -> bool:
        return not self.flags

    def summary(self) -> str:
        return ",".join(self.flags)


def scan_output(text: str) -> OutputScan:
    """扫描模型输出，返回命中的越界类型。"""
    if not text or not text.strip():
        return OutputScan()
    flags: list[str] = []
    severity = 0
    for name, pattern, level in _COMPILED:
        if pattern.search(text):
            flags.append(name)
            severity = max(severity, level)
    return OutputScan(tuple(flags), severity)
