"""验证 db.py：缺 KE_DB_URL 抛 RuntimeError；有 URL 时能 build engine。"""
import pytest

from src.service import db


def test_missing_url_raises(monkeypatch):
    monkeypatch.delenv("KE_DB_URL", raising=False)
    db._engine = None  # 重置缓存
    with pytest.raises(RuntimeError, match="KE_DB_URL not set"):
        db.get_engine()


def test_engine_singleton(monkeypatch):
    # 用 sqlite 本地 in-memory 测试 engine 单例（不需要真 MySQL）
    monkeypatch.setenv("KE_DB_URL", "sqlite+aiosqlite:///:memory:")
    db._engine = None
    db._SessionMaker = None
    e1 = db.get_engine()
    e2 = db.get_engine()
    assert e1 is e2  # 单例
