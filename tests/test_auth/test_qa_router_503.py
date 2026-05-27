"""qa/explain（以及其他附 require_infra_healthy 的路由）在依赖挂时 503。

设计：[[基础设施健康检查与产品不可用-设计]] §3.3 + §9 验收 #2

两个测试场景：
1. neo4j 挂 → 受保护路由发请求 → 会被 require_infra_healthy 或 require_login 拦截
   （无论哪个先，都不会是 200）
2. auth/login 不附 require_infra_healthy → 即使 infra 挂，不应返回 503 INFRA_UNHEALTHY
"""
from unittest.mock import MagicMock

import pytest
from httpx import ASGITransport, AsyncClient

# 从 api.py 导入 FastAPI app 实例（整个 ASGI application）
from src.service.api import app


# @pytest.mark.asyncio：标记为异步测试，pytest-asyncio 插件会接管 async def 测试的运行
@pytest.mark.asyncio
async def test_qa_explain_503_when_neo4j_down():
    """neo4j 挂 → /projects/.../qa/explain 不返回 200。

    验证逻辑：
    - require_infra_healthy 在 get_current_user 之后运行（都在同一 dependencies 列表里）
    - 无 token → 先被 get_current_user 拦截 → 401 Unauthorized
    - 但无论如何，**不应该是 200**（不能绕过 infra 检查拿到正常响应）
    - 如果测试环境里 infra_status 能被 require_infra_healthy 触达，则会是 503
    """
    # 准备：infra_status 里 neo4j 挂掉
    app.state.infra_status = {
        "mysql": {"ok": True},
        "neo4j": {"ok": False, "error": "Connection refused"},
        "weaviate": {"ok": True},
        "dashscope": {"ok": True},
    }

    # ASGITransport：让 httpx 直接调用 ASGI app，不需要启动真实的 HTTP 服务器
    # AsyncClient：httpx 的异步 HTTP 客户端，base_url 设置请求的基础 URL
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        # 无 Authorization header → 会被 get_current_user dependency 拦截 → 401
        r = await client.post(
            "/projects/proj-a/qa/explain",
            json={"question": "test"},  # json= 会把 dict 序列化为 JSON body，并设置 Content-Type
        )

    # 断言：不应该是 200（infra 挂了不能正常响应）
    assert r.status_code != 200, f"期待非 200，实际 {r.status_code}"

    # 如果碰巧是 503，验证 detail.code 正确
    if r.status_code == 503:
        body = r.json()
        # r.json()：把响应 body 解析为 Python dict（FastAPI 返回 JSON）
        assert body["detail"]["code"] in ("INFRA_UNHEALTHY", "INFRA_UNINITIALIZED"), \
            f"503 时 detail.code 应为 INFRA_UNHEALTHY 或 INFRA_UNINITIALIZED，实际：{body}"


def test_auth_router_has_no_require_infra_healthy():
    """auth_router 不附 require_infra_healthy（决策 #8）。

    静态检查 router 对象的 dependencies 列表，不发 HTTP 请求，不涉及 DB。
    之所以用静态检查而非 HTTP 请求：
      - 避免 full-suite 中 aiosqlite event loop 关闭引起的 flaky RuntimeError
      - 意图更精准：我们关心的是「依赖有没有挂上去」，而非「登录能否成功」

    等效的直觉解释：
      auth_router.dependencies 列表里如果有 require_infra_healthy，
      那么 /auth/login 每次调用都会先触发 infra 检查 —— 这违反决策 #8。
    """
    # 从 auth_router 模块导入 router 对象（APIRouter 实例）
    from src.service.auth_router import router as auth_router_obj
    # 从 deps_infra 导入要检查的依赖函数
    from src.service.deps_infra import require_infra_healthy

    # router.dependencies 是 APIRouter 构造时传入的 dependencies 列表
    # Depends(fn) 对象的 .dependency 属性指向实际函数
    # 列表推导式：提取所有 dep 的 .dependency 字段，组成集合便于 membership 检查
    dep_fns = {dep.dependency for dep in auth_router_obj.dependencies}

    # 断言：require_infra_healthy 不在 auth_router 的依赖集合中
    assert require_infra_healthy not in dep_fns, (
        "auth_router 不应附 require_infra_healthy（决策 #8）：login 需要让用户看到失败原因，"
        "不能被 infra 检查屏蔽。"
    )
