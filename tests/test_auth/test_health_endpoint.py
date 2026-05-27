"""/health endpoint 测试。

设计：[[基础设施健康检查与产品不可用-设计]] §3.2.3 + §6.2

验证：
- 未登录 → 200，只含 healthy + ts，无 deps
- 全挂时 → healthy=False，仍 200（端点不自我熔断）
"""
# unittest.mock 是 Python 标准库里的 mock 框架，patch 用于替换模块属性
from unittest.mock import patch

# pytest 是 Python 最流行的测试框架，asyncio 标记让 pytest 支持 async 测试函数
import pytest

# httpx 的 AsyncClient 是异步 HTTP 客户端；ASGITransport 让它直接调用 ASGI app，不需要真实监听端口
from httpx import AsyncClient, ASGITransport

# 导入 FastAPI app 对象，测试时直接注入
from src.service.api import app


@pytest.mark.asyncio  # 标记这是一个异步测试，pytest-asyncio 会负责运行它
async def test_health_unauthenticated_returns_basic_status():
    """未登录调 /health → 200 + 基本字段（healthy + ts），无 deps。"""
    # 伪造 5 个依赖全 ok 的状态字典
    fake_status = {
        "mysql": {"ok": True},
        "neo4j": {"ok": True},
        "weaviate": {"ok": True},
        "dashscope": {"ok": True},
    }

    # async def 定义异步函数；*a/**k 接收任意参数，这里只是为了签名兼容
    async def fake_check(*a, **k):
        """直接返回 fake_status，不真正 ping 任何服务。"""
        return fake_status

    # patch 替换 src.service.infra_health 模块中的 check_all_deps，测试结束后自动恢复
    # /health 函数内部用 "from src.service.infra_health import check_all_deps" 局部导入，
    # 因此必须 patch 源模块（infra_health），而不是 api 模块中的引用
    # side_effect=fake_check 意思是：每次调用 check_all_deps 时，执行 fake_check
    with patch("src.service.infra_health.check_all_deps", side_effect=fake_check):
        # async with ... as client：异步上下文管理器，自动管理 client 的生命周期
        # ASGITransport(app=app) 让 httpx 直接调用 FastAPI app，不走真实网络
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
            # 发送 GET /health，无 Authorization header（模拟未登录用户）
            r = await client.get("/health")

    # assert 断言：失败时抛 AssertionError，pytest 捕获并报告为 FAIL
    assert r.status_code == 200
    body = r.json()
    # 全 ok → healthy 应该是 True
    assert body["healthy"] is True
    # ts 字段（ISO 时间戳）必须存在
    assert "ts" in body
    # 未登录不暴露 deps（deps 字段不应该出现在响应里）
    assert "deps" not in body


@pytest.mark.asyncio
async def test_health_unhealthy_still_returns_200():
    """/health 自身不附 require_infra_healthy；任一依赖挂仍返 200。"""
    # 伪造 5 个依赖全挂的状态字典
    fake_status = {
        "mysql": {"ok": False, "error": "down"},
        "neo4j": {"ok": False, "error": "down"},
        "weaviate": {"ok": False, "error": "down"},
        "dashscope": {"ok": False, "error": "down"},
    }

    async def fake_check(*a, **k):
        """返回全挂状态。"""
        return fake_status

    with patch("src.service.infra_health.check_all_deps", side_effect=fake_check):
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
            r = await client.get("/health")

    # 即使全部挂掉，/health 本身仍然返回 200（不自我熔断）
    assert r.status_code == 200
    # healthy 应该是 False（因为有依赖挂了）
    assert r.json()["healthy"] is False
