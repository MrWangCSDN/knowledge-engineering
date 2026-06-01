"""验证 Neo4j 退役后不再计入致命依赖：只有 neo4j down 时 require_infra_healthy 不 503；
关键依赖(weaviate/mysql/dashscope) down 仍 503。设计 [[CodeGraph-结构引擎集成-设计]] §7。
"""
import pytest                                # pytest 框架；pytest.raises 断言异常
from fastapi import HTTPException            # require_infra_healthy 不健康时抛它
from src.service.deps_infra import require_infra_healthy


class _FakeUser:
    """假 User：require_infra_healthy 只读 user.is_admin。"""
    is_admin = False


def _fake_request(infra_status):
    """造最小化假 Request：只需 .app.state.infra_status 这条链。

    用 type(name, bases, namespace) 动态建匿名类的实例，省去定义一堆类。
    """
    state = type("S", (), {"infra_status": infra_status})()   # 带 infra_status 属性的对象
    app = type("A", (), {"state": state})()                   # 带 state 属性的对象
    return type("R", (), {"app": app})()                      # 带 app 属性的对象（= 假 request）


@pytest.mark.asyncio                          # pytest-asyncio：标记该协程测试由事件循环跑
async def test_neo4j_down_does_not_503():
    """只有 neo4j 不健康，其它都 ok → 不应 503（Neo4j 退役，非致命）。"""
    status = {
        "mysql": {"ok": True}, "weaviate": {"ok": True},
        "dashscope": {"ok": True}, "neo4j": {"ok": False, "error": "down"},
    }
    # 不抛异常即通过
    await require_infra_healthy(_fake_request(status), user=_FakeUser())


@pytest.mark.asyncio
async def test_weaviate_down_still_503():
    """关键依赖 weaviate 不健康 → 仍 503（确认没把所有 dep 都放行）。"""
    status = {
        "mysql": {"ok": True}, "weaviate": {"ok": False, "error": "down"},
        "dashscope": {"ok": True}, "neo4j": {"ok": True},
    }
    with pytest.raises(HTTPException) as ei:   # 期望抛 HTTPException
        await require_infra_healthy(_fake_request(status), user=_FakeUser())
    assert ei.value.status_code == 503
