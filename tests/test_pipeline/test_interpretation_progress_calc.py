# tests/test_pipeline/test_interpretation_progress_calc.py
"""interpretation_percent / methods_total 纯函数单测。"""
from src.pipeline.interpretation_progress_calc import interpretation_percent, methods_total


def test_percent_normal():
    prog = {"tech": {"done": 90, "total": 180}, "biz": {"done": 0, "total": 0}}
    assert interpretation_percent(prog) == 50


def test_percent_zero_total_is_zero_not_div0():
    prog = {"tech": {"done": 0, "total": 0}, "biz": {"done": 0, "total": 0}}
    assert interpretation_percent(prog) == 0


def test_percent_full():
    prog = {"tech": {"done": 100, "total": 100}, "biz": {"done": 50, "total": 50}}
    assert interpretation_percent(prog) == 100


def test_methods_total_sums_phase_totals():
    prog = {"tech": {"done": 10, "total": 120}, "biz": {"done": 0, "total": 0}}
    assert methods_total(prog) == 120
