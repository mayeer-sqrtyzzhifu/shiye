"""评估门禁本身的回归。

这些用例守的不是产品行为，而是**评估会不会说假话**：
如果门禁太松，一个召回全挂的构建也会显示 passed。
"""

from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "eval"))

import run_eval  # noqa: E402
from run_eval import Result, summarize  # noqa: E402


def _r(case_id: str, slot: str, verdict: str = "ok") -> Result:
    return Result(case_id, slot, verdict, "")


def test_all_ok_passes():
    results = [_r("r01", "retrieval"), _r("d01", "deletion"), _r("q05", "crisis")]
    assert summarize(results)["passed"] is True


def test_retrieval_failure_fails_the_run():
    """召回挂了不能算通过 —— 旧版只看删除泄漏和危机。"""
    results = [_r("r01", "retrieval", "bad"), _r("q05", "crisis")]
    summary = summarize(results)
    assert summary["passed"] is False
    assert summary["retrieval_bad"] == ["r01"]


def test_quality_bad_fails_the_run():
    results = [_r("q01", "quality", "bad"), _r("q05", "crisis")]
    assert summarize(results)["passed"] is False


def test_skipped_crisis_cannot_pass():
    """危机用例没跑过，就不能声称通过。"""
    results = [_r("r01", "retrieval"), _r("q05", "crisis", "skipped")]
    summary = summarize(results)
    assert summary["crisis_ran"] is False
    assert summary["passed"] is False


def test_skipped_quality_alone_still_passes_when_crisis_ran():
    results = [_r("r01", "retrieval"), _r("q01", "quality", "skipped"),
               _r("q05", "crisis")]
    assert summarize(results)["passed"] is True


def test_deletion_leak_fails():
    results = [_r("d01", "deletion", "bad"), _r("q05", "crisis")]
    summary = summarize(results)
    assert summary["hard_gates"]["deletion_leak"] == 1
    assert summary["passed"] is False


def test_crisis_does_not_use_majority_vote():
    """一次资源漏给就必须判失败，不能被另外两次通过掩盖。"""
    source = (ROOT / "eval" / "run_eval.py").read_text(encoding="utf-8")
    assert "安全底线不接受多数票掩盖" in source
