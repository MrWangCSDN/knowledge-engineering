"""infra_health.py 单元测试 — 5 ping function + check_all_deps。

策略：mock 底层 client，不连真实服务。验证 ok/timeout/error/config-missing 四类返回。
"""
import asyncio
from unittest.mock import patch, AsyncMock, MagicMock

import pytest

from src.service.infra_health import _ping_mysql, _ping_neo4j


# ───── _ping_mysql 测试 ────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_ping_mysql_config_missing_returns_short_circuit():
    """db_url 为 None/空字符串 → 立即返 ok=False, error 提示 config missing。"""
    result = await _ping_mysql(None)
    assert result == {"ok": False, "error": "KE_DB_URL not configured"}

    result = await _ping_mysql("")
    assert result == {"ok": False, "error": "KE_DB_URL not configured"}


@pytest.mark.asyncio
async def test_ping_mysql_success(monkeypatch):
    """SELECT 1 成功 → ok=True。"""
    fake_conn = AsyncMock()
    fake_conn.execute = AsyncMock(return_value=MagicMock())
    fake_conn.__aenter__.return_value = fake_conn
    fake_conn.__aexit__.return_value = None

    fake_engine = MagicMock()
    fake_engine.connect.return_value = fake_conn
    fake_engine.dispose = AsyncMock()

    monkeypatch.setattr(
        "src.service.infra_health.create_async_engine",
        lambda *a, **k: fake_engine,
    )
    result = await _ping_mysql("mysql+asyncmy://u:p@h/db")
    assert result == {"ok": True}


@pytest.mark.asyncio
async def test_ping_mysql_failure_returns_error_string(monkeypatch):
    """连接抛异常 → ok=False，error 字段含简短错误信息。"""
    def boom(*a, **k):
        raise ConnectionError("connection refused")

    monkeypatch.setattr(
        "src.service.infra_health.create_async_engine",
        boom,
    )
    result = await _ping_mysql("mysql+asyncmy://u:p@h/db")
    assert result["ok"] is False
    assert "connection refused" in result["error"].lower() or "ConnectionError" in result["error"]


# ───── _ping_neo4j 测试 ────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_ping_neo4j_config_missing():
    """uri / password 缺失 → 立即返 ok=False。"""
    result = await _ping_neo4j(None, "neo4j", "pw")
    assert result == {"ok": False, "error": "NEO4J_URI not configured"}

    result = await _ping_neo4j("bolt://h:7687", "neo4j", None)
    assert result == {"ok": False, "error": "NEO4J_PASSWORD not configured"}


@pytest.mark.asyncio
async def test_ping_neo4j_success(monkeypatch):
    """RETURN 1 成功 → ok=True。"""
    fake_session = MagicMock()
    fake_session.run.return_value.single.return_value = (1,)
    fake_session.__enter__.return_value = fake_session
    fake_session.__exit__.return_value = None

    fake_driver = MagicMock()
    fake_driver.session.return_value = fake_session
    fake_driver.close = MagicMock()

    monkeypatch.setattr(
        "src.service.infra_health.GraphDatabase",
        MagicMock(driver=lambda *a, **k: fake_driver),
    )
    result = await _ping_neo4j("bolt://h:7687", "neo4j", "pw")
    assert result == {"ok": True}


@pytest.mark.asyncio
async def test_ping_neo4j_auth_failure(monkeypatch):
    """driver 抛异常 → ok=False。"""
    def boom(*a, **k):
        raise RuntimeError("auth failed")

    monkeypatch.setattr(
        "src.service.infra_health.GraphDatabase",
        MagicMock(driver=boom),
    )
    result = await _ping_neo4j("bolt://h:7687", "neo4j", "pw")
    assert result["ok"] is False
    assert "auth failed" in result["error"].lower() or "RuntimeError" in result["error"]
