"""会话上下文 token 预算 / 历史裁剪（spec §18）纯函数测试。"""
from src.service.memory.context_budget import (
    estimate_tokens, model_context_window, history_token_budget,
    trim_history_to_budget,
)


def test_estimate_tokens_ceil_and_empty():
    assert estimate_tokens("") == 0
    assert estimate_tokens("123") == 2          # ceil(3/1.5)=2
    assert estimate_tokens("a" * 150) == 100
    # M1：注解改为 str | None；None 已由 if not text 安全处理，行为不变
    assert estimate_tokens(None) == 0


def test_window_env_override(monkeypatch):
    monkeypatch.delenv("KE_MODEL_CONTEXT_WINDOW", raising=False)
    assert model_context_window() == 1000000  # 缺省/非法/低于 floor → 默认 1M
    monkeypatch.setenv("KE_MODEL_CONTEXT_WINDOW", "1000000")
    assert model_context_window() == 1000000
    monkeypatch.setenv("KE_MODEL_CONTEXT_WINDOW", "garbage")
    assert model_context_window() == 1000000  # 缺省/非法/低于 floor → 默认 1M
    monkeypatch.setenv("KE_MODEL_CONTEXT_WINDOW", "-5")
    assert model_context_window() == 1000000  # 缺省/非法/低于 floor → 默认 1M
    # M2：低于 _MIN_WINDOW(1000) 的值视为运维误配，回退默认 1M
    monkeypatch.setenv("KE_MODEL_CONTEXT_WINDOW", "100")
    assert model_context_window() == 1000000  # 远低于 floor → 默认 1M
    monkeypatch.setenv("KE_MODEL_CONTEXT_WINDOW", "999")
    assert model_context_window() == 1000000  # 恰好低于 floor → 默认 1M
    monkeypatch.setenv("KE_MODEL_CONTEXT_WINDOW", "1000")
    assert model_context_window() == 1000     # 恰好等于 floor → 接受


def test_history_budget_is_window_minus_reserve(monkeypatch):
    monkeypatch.setenv("KE_MODEL_CONTEXT_WINDOW", "100000")
    monkeypatch.delenv("KE_CTX_RESERVE_PCT", raising=False)
    assert history_token_budget() == 55000        # 100000*(1-0.45)
    monkeypatch.setenv("KE_CTX_RESERVE_PCT", "0.5")
    assert history_token_budget() == 50000


def test_trim_keeps_recent_within_budget_drops_older():
    hist = [
        {"role": "user", "content": "x" * 150},      # est 100
        {"role": "assistant", "content": "y" * 150}, # est 100
        {"role": "user", "content": "z" * 75},       # est 50
    ]
    kept, used = trim_history_to_budget(hist, budget=160)
    assert [m["content"][:1] for m in kept] == ["y", "z"]
    assert used == 150


def test_trim_defensive_and_min_one_recent():
    assert trim_history_to_budget(None, 100) == ([], 0)
    assert trim_history_to_budget([], 100) == ([], 0)
    assert trim_history_to_budget("notlist", 100) == ([], 0)
    kept, used = trim_history_to_budget([{"role": "user", "content": "q" * 3000}], 10)
    assert len(kept) == 1 and used > 10
