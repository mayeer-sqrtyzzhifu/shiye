from __future__ import annotations

from datetime import datetime

import pytest

from companion import persona
from companion.prompt import BUDGET, assemble, estimate_tokens


def test_default_persona_renders_required_sections():
    text = persona.get().render()
    assert "姐姐" in text
    assert "## 关于你自己" in text
    assert "## 开头" in text
    assert "## 不要写成什么样" in text
    assert "## 回复的形态" in text
    assert "## 量化参照" in text
    assert "不要这样说" in text
    assert "风格参考" in text


def test_examples_do_not_all_start_with_interjection():
    """示范本身就是模型照抄的模板：如果一半示范以语气词开头，它就会每句都这么开。"""
    starts = ("嗯", "诶", "唔", "哎呀", "啊呀", "欸")
    examples = persona.get().examples
    leading = sum(1 for _, a in examples if a.startswith(starts))
    assert leading <= len(examples) * 0.25, f"{leading}/{len(examples)} 条以语气词开头，太多了"
    # 但至少要保留一条"只应一声"的极短形态
    assert leading >= 1


def test_no_odd_mandarin_interjections():
    """「啊呀」不是中文里的说法，写进人格会被模型学走。"""
    assert "啊呀" not in persona.get().render()


def test_persona_forbids_the_two_beat_template():
    """「共情一句 + 问一句」是最强的 AI 标记，人格里必须点名禁止。"""
    avoid = persona.get().avoid
    assert "两段式" in avoid
    assert "问句" in avoid
    assert "不是…而是…" in avoid
    assert "三连排比" in avoid
    assert "复述" in avoid


def test_examples_vary_in_shape():
    """示范不能全是「接住 + 提问」，否则模型只会这一种。"""
    examples = persona.get().examples
    with_question = sum(1 for _, a in examples if "？" in a)
    assert with_question < len(examples), "至少有一条不带问句"
    assert with_question <= len(examples) * 0.6, "带问句的比例不该超过六成"
    # 至少有一条极短回复（只应一声）
    assert any(len(a) <= 12 for _, a in examples)


def test_dimensions_are_quantified():
    """“温柔一点”没法执行；参数得带数字。"""
    dims = persona.get().dimensions
    assert len(dims) >= 6
    numeric = [d for d in dims if any(ch.isdigit() for ch in d)]
    assert len(numeric) >= 5
    joined = "\n".join(dims)
    assert "长度" in joined and "问句" in joined and "建议" in joined


def test_contrasts_pair_bad_with_good():
    pairs = persona.get().contrasts
    assert len(pairs) >= 4
    for bad, good in pairs:
        assert bad.strip() and good.strip()
        assert bad != good
        assert len(good) <= 30


def test_at_least_a_dozen_examples():
    assert len(persona.get().examples) >= 12


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

