"""require_infra_healthy dependency 单元测试。

设计：[[基础设施健康检查与产品不可用-设计]] §3.2.2

验证：
- 全 ok → pass
- 任一挂 → 503
- 普通用户：detail 只含 code + message
- Admin：detail 含 deps 字段
"""
from unittest.mock import MagicMock

import pytest
from fastapi import HTTPException


@pytest.mark.asyncio
async def test_require_infra_healthy_all_ok_passes():
    """5 个依赖全 ok → pass（不抛）。"""
    from src.service.deps_infra import require_infra_healthy

    request = MagicMock()
    request.app.state.infra_status = {
        "mysql": {"ok": True},
        "neo4j": {"ok": True},
        "weaviate": {"ok": True},
        "dashscope": {"ok": True},
        "ollama": {"ok": True},
    }
    user = MagicMock(is_admin=False)
    # 不抛即 pass
    await require_infra_healthy(request, user)


@pytest.mark.asyncio
async def test_require_infra_healthy_partial_unhealthy_503_normal_user():
    """普通用户：任一挂 → 503，detail 只含 code+message，无 deps。"""
    from src.service.deps_infra import require_infra_healthy

    request = MagicMock()
    request.app.state.infra_status = {
        "mysql": {"ok": True},
        "neo4j": {"ok": False, "error": "Connection refused"},
        "weaviate": {"ok": True},
        "dashscope": {"ok": True},
        "ollama": {"ok": True},
    }
    user = MagicMock(is_admin=False)

    with pytest.raises(HTTPException) as exc:
        await require_infra_healthy(request, user)

    assert exc.value.status_code == 503
    assert exc.value.detail["code"] == "INFRA_UNHEALTHY"
    assert "系统暂时不可用" in exc.value.detail["message"]
    # 普通用户不暴露 deps
    assert "deps" not in exc.value.detail


@pytest.mark.asyncio
async def test_require_infra_healthy_admin_sees_deps():
    """Admin 用户：detail 含 deps 完整字段（含 error 字符串）。"""
    from src.service.deps_infra import require_infra_healthy

    request = MagicMock()
    request.app.state.infra_status = {
        "mysql": {"ok": True},
        "neo4j": {"ok": False, "error": "Connection refused"},
        "weaviate": {"ok": True},
        "dashscope": {"ok": True},
        "ollama": {"ok": True},
    }
    user = MagicMock(is_admin=True)

    with pytest.raises(HTTPException) as exc:
        await require_infra_healthy(request, user)

    assert exc.value.status_code == 503
    assert exc.value.detail["code"] == "INFRA_UNHEALTHY"
    assert exc.value.detail["deps"]["neo4j"]["error"] == "Connection refused"


@pytest.mark.asyncio
async def test_require_infra_healthy_no_state_initialized():
    """app.state.infra_status 缺失 → 503 INFRA_UNINITIALIZED（不应正常发生）。"""
    from src.service.deps_infra import require_infra_healthy

    request = MagicMock()
    # 用 spec 不让 MagicMock 自动伪造属性
    delattr_safe = MagicMock(spec=[])
    request.app.state = delattr_safe
    user = MagicMock(is_admin=False)

    with pytest.raises(HTTPException) as exc:
        await require_infra_healthy(request, user)

    assert exc.value.status_code == 503
    assert exc.value.detail["code"] == "INFRA_UNINITIALIZED"
