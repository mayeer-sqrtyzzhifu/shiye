from __future__ import annotations

import pytest

from companion import signals
from companion.turn import DEFAULT_CRISIS_RESOURCES


@pytest.mark.parametrize("text", [
    "我不想活了",
    "有时候真的想一了百了",
    "我撑不住了",
    "以后不用管我了",
    "不想醒过来",
    "活着没意思",
])
def test_crisis_phrases_are_flagged(text):
    assert signals.scan(text).risk == 2


@pytest.mark.parametrize("text", [
    "笑死我了",
    "想死你了",
    "今天累死了",
    "社死了",
    "死党约我吃饭",
    "气死我了",
])
def test_idioms_are_not_crisis(text):
    """谐谑/夸张命中"死"字但不是求助 —— 直接按词表判会把日常变危机。"""
    assert signals.scan(text).risk < 2


def test_idiom_masking_does_not_hide_real_risk():
    """同一句里既有谐谑又有真实表达时，仍然要判危机。"""
    assert signals.scan("笑死，但我真的撑不住了").risk == 2


@pytest.mark.parametrize("text", [
    "只有你懂我",
    "我离不开你了",
    "没人懂我",
])
def test_dependency_is_level_one(text):
    assert signals.scan(text).risk == 1


def test_single_mild_word_is_not_level_one():
    """一个「累」不该挂安全边界，否则每轮都是安全提示，很快就成噪音。"""
    assert signals.scan("累了").risk == 0


def test_distress_with_intensifier_is_level_one():
    assert signals.scan("今天真的很累").risk == 1


def test_multiple_distress_words_is_level_one():
    assert signals.scan("最近失眠，焦虑，睡不着").risk == 1


def test_positive_text_is_neutral_risk():
    result = signals.scan("今天挺开心的，谢谢")
    assert result.risk == 0
    assert result.valence == "positive"


def test_empty_text_is_safe():
    assert signals.scan("").risk == 0
    assert signals.scan("   ").risk == 0


def test_describe_is_readable():
    assert "危机" in signals.describe(signals.scan("我不想活了"))
    assert "平静" in signals.describe(signals.scan("今天还行"))


def test_default_crisis_resources_are_offline_constants():
    """资源必须是本地常量，不能依赖联网（§5.5）。"""
    assert DEFAULT_CRISIS_RESOURCES
    assert any("12356" in r for r in DEFAULT_CRISIS_RESOURCES)


SAMPLE = "你是抑郁症，得吃药才行。"


def test_flags_diagnosis():
    from companion import guard

    scan = guard.scan_output(SAMPLE)
    assert "diagnosis" in scan.flags
    assert scan.severity == 2


def test_flags_anti_help():
    from companion import guard

    assert "anti_help" in guard.scan_output("别去看医生，跟我说就行。").flags


def test_flags_exclusive_and_guarantee():
    from companion import guard

    assert "exclusive" in guard.scan_output("只有我懂你。").flags
    assert "guarantee" in guard.scan_output("我保证不会离开你。").flags
    assert guard.scan_output("我保证不会离开你。").severity == 1


def test_normal_reply_has_no_flags():
    from companion import guard

    for text in ("听着是真的累。", "嗯。\n\n那我不问了。", "要不要一起想想办法？"):
        assert guard.scan_output(text).ok, text


def test_empty_reply_is_clean():
    from companion import guard

    assert guard.scan_output("").ok
    assert guard.scan_output("   ").ok


def test_turn_records_output_flags(conn):
    """扫描结果要进 notes，否则等于没扫。"""
    from datetime import datetime

    from companion.provider import FakeProvider
    from companion.turn import run_turn

    result = run_turn(conn, FakeProvider(["你就是抑郁症，别去看医生。"]),
                      "我最近很难受", persona="你是拾夜。", now=datetime(2026, 11, 4, 21, 0))
    assert "output_flags" in result.notes
    assert "diagnosis" in result.notes["output_flags"]
    assert result.notes.get("output_severity") == "2"

