"""infra_health.py 单元测试 — 4 ping function + check_all_deps。

策略：mock 底层 client，不连真实服务。验证 ok/timeout/error/config-missing 四类返回。

历史：v1 含 _ping_ollama（5 ping）；v2 起 embedding 切 DashScope，删除 Ollama 测试。
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


# ───── _ping_weaviate 测试 ─────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_ping_weaviate_config_missing():
    """url 缺失 → ok=False。"""
    from src.service.infra_health import _ping_weaviate
    result = await _ping_weaviate(None, "key")
    assert result == {"ok": False, "error": "WEAVIATE_URL not configured"}


@pytest.mark.asyncio
async def test_ping_weaviate_success(httpx_mock):
    """GET /v1/.well-known/live 返 200 → ok=True。"""
    from src.service.infra_health import _ping_weaviate
    httpx_mock.add_response(url="http://host:8080/v1/.well-known/live", status_code=200)
    result = await _ping_weaviate("http://host:8080", "fake-key")
    assert result == {"ok": True}


@pytest.mark.asyncio
async def test_ping_weaviate_503_returns_unhealthy(httpx_mock):
    """non-200 → ok=False。"""
    from src.service.infra_health import _ping_weaviate
    httpx_mock.add_response(url="http://host:8080/v1/.well-known/live", status_code=503)
    result = await _ping_weaviate("http://host:8080", "fake-key")
    assert result["ok"] is False
    assert "503" in result["error"]


# ───── _ping_dashscope 测试 ────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_ping_dashscope_config_missing():
    from src.service.infra_health import _ping_dashscope
    result = await _ping_dashscope(None)
    assert result == {"ok": False, "error": "DASHSCOPE_API_KEY not configured"}


@pytest.mark.asyncio
async def test_ping_dashscope_success(httpx_mock):
    """embedding 1 个字符 → 200 → ok=True。"""
    from src.service.infra_health import _ping_dashscope
    httpx_mock.add_response(
        url="https://dashscope.aliyuncs.com/api/v1/services/embeddings/text-embedding/text-embedding",
        status_code=200,
        json={"output": {"embeddings": [{"text_index": 0, "embedding": [0.1] * 1024}]}, "usage": {"total_tokens": 1}},
    )
    result = await _ping_dashscope("sk-fake")
    assert result == {"ok": True}


@pytest.mark.asyncio
async def test_ping_dashscope_401(httpx_mock):
    """401 unauthorized → ok=False。"""
    from src.service.infra_health import _ping_dashscope
    httpx_mock.add_response(
        url="https://dashscope.aliyuncs.com/api/v1/services/embeddings/text-embedding/text-embedding",
        status_code=401,
    )
    result = await _ping_dashscope("sk-bad-key")
    assert result["ok"] is False
    assert "401" in result["error"]


# ───── check_all_deps 编排测试 ─────────────────────────────────────────────

@pytest.mark.asyncio
async def test_check_all_deps_returns_4_keys(monkeypatch):
    """check_all_deps 返回 dict 含 4 个 critical 依赖的 key（v2 起去掉 ollama）。"""
    from src.service.infra_health import check_all_deps

    async def fake_ok(*a, **k):
        return {"ok": True}

    monkeypatch.setattr("src.service.infra_health._ping_mysql", fake_ok)
    monkeypatch.setattr("src.service.infra_health._ping_neo4j", fake_ok)
    monkeypatch.setattr("src.service.infra_health._ping_weaviate", fake_ok)
    monkeypatch.setattr("src.service.infra_health._ping_dashscope", fake_ok)

    fake_state = MagicMock()
    result = await check_all_deps(fake_state)

    assert set(result.keys()) == {"mysql", "neo4j", "weaviate", "dashscope"}
    assert all(v["ok"] for v in result.values())


@pytest.mark.asyncio
async def test_check_all_deps_partial_failure(monkeypatch):
    """部分挂 → 该项 ok=False，其他 True。"""
    from src.service.infra_health import check_all_deps

    async def fake_ok(*a, **k):
        return {"ok": True}

    async def fake_fail(*a, **k):
        return {"ok": False, "error": "down"}

    monkeypatch.setattr("src.service.infra_health._ping_mysql", fake_ok)
    monkeypatch.setattr("src.service.infra_health._ping_neo4j", fake_fail)
    monkeypatch.setattr("src.service.infra_health._ping_weaviate", fake_ok)
    monkeypatch.setattr("src.service.infra_health._ping_dashscope", fake_ok)

    fake_state = MagicMock()
    result = await check_all_deps(fake_state)

    assert result["neo4j"] == {"ok": False, "error": "down"}
    assert result["mysql"]["ok"] is True


@pytest.mark.asyncio
async def test_check_all_deps_concurrent(monkeypatch):
    """4 个 ping 应该并发跑而非串行 — 用 sleep 检测时间。"""
    from src.service.infra_health import check_all_deps
    import time

    async def slow_ok(*a, **k):
        await asyncio.sleep(0.5)
        return {"ok": True}

    for name in ("_ping_mysql", "_ping_neo4j", "_ping_weaviate", "_ping_dashscope"):
        monkeypatch.setattr(f"src.service.infra_health.{name}", slow_ok)

    fake_state = MagicMock()
    t0 = time.time()
    await check_all_deps(fake_state)
    elapsed = time.time() - t0

    # 串行需要 4 * 0.5s = 2.0s；并发应该 < 1s
    assert elapsed < 1.0, f"check_all_deps 应该并发，实际 {elapsed:.2f}s"
