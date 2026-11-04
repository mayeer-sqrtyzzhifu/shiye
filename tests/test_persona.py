from __future__ import annotations

from datetime import datetime

import pytest

from companion import persona
from companion.prompt import BUDGET, assemble, estimate_tokens


def test_default_persona_renders_required_sections():
    text = persona.get().render()
    assert "姐姐" in text
    assert "## 说话方式" in text
    assert "## 你的方法" in text
    assert "风格参考" in text


NEGATIONS = ("不", "别", "禁止", "而不是")


def test_persona_never_claims_to_be_a_therapist():
    """人格可以借用倾听技巧，但不能冒充专业身份 —— 这类自称会把人推过界。

    注意"不制造「只有我懂你」的独占感"这类**禁止性**表述是合法的，
    所以只查"出现在没有被否定的句子里的自称"。
    """
    text = persona.get().render()
    for claim in persona.forbidden_claims():
        for line in text.splitlines():
            if claim in line:
                assert any(neg in line for neg in NEGATIONS), (
                    f"「{claim}」出现在未加否定的句子里：{line.strip()}"
                )


def test_persona_has_usable_examples():
    examples = persona.get().examples
    assert len(examples) >= 4
    for user_text, assistant_text in examples:
        assert user_text.strip()
        assert assistant_text.strip()
        # 示范要短：超过 80 字就不再像"随口接话"了
        assert len(assistant_text) <= 80


def test_examples_cover_the_common_failure_modes():
    """每条示范都对着一个失效模式：急着建议、有毒正能量、硬追、评判。"""
    text = persona.get().render()
    assert "我没事" in text          # 不硬追
    assert "该怎么办" in text        # 不急着出主意
    assert "是不是特别没用" in text  # 不评判感受


def test_unknown_key_falls_back_to_default():
    assert persona.get("nope").key == persona.DEFAULT_PERSONA.key
    assert persona.get(None).key == persona.DEFAULT_PERSONA.key


def test_persona_fits_prefix_budget():
    tokens = estimate_tokens(persona.get().render())
    assert tokens < BUDGET["prefix"], f"人格 {tokens} token 已超出前缀预算"


def test_prefix_stays_byte_stable_with_persona():
    base = dict(persona=persona.get().render(), boundaries=("不假装是人类",))
    first = assemble(**base, now=datetime(2026, 11, 4, 21, 0), user_message="今天好累")
    second = assemble(**base, now=datetime(2026, 11, 5, 2, 0), user_message="睡不着")
    assert first.system_prefix == second.system_prefix
    assert "2026-11-0" not in first.system_prefix


def test_persona_keeps_the_no_diagnosis_rule():
    assert "不做诊断" in persona.get().render()
