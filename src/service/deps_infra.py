"""require_infra_healthy FastAPI dependency — 单独文件避免 router 循环 import。

router 文件（qa_router 等）→ api.py 会循环（因为 api.py 又 include_router 这些 router）。
所以把 dep 抽到独立模块，router 直接 import deps_infra 即可，不经过 api.py。

设计：[[基础设施健康检查与产品不可用-设计]] §3.2.2
"""
from __future__ import annotations

# fastapi 的 Depends 用于声明依赖注入；HTTPException 用于抛出 HTTP 错误；
# Request 用于访问 app.state（挂在 ASGI application 上的全局状态）
from fastapi import Depends, HTTPException, Request

# get_current_user：JWT → User dependency，解析 Authorization header 中的 token
# 所有需要登录才能用的接口都会用到它
from src.service.auth_dependencies import get_current_user

# User：Pydantic / SQLAlchemy 用户模型，包含 is_admin 字段
from src.service.auth_models import User

# 非致命依赖集合：这些 dep 即使 down 也不让产品 503（仅 check_all_deps 上报供观测）。
# neo4j：CodeGraph 迁移后图导航不依赖它（设计 [[CodeGraph-结构引擎集成-设计]] §7 退役清单），退役中。
_NON_CRITICAL_DEPS = {"neo4j"}


async def require_infra_healthy(
    request: Request,
    user: User = Depends(get_current_user),  # Depends：FastAPI 会自动把 get_current_user 的返回值注入进来
) -> None:
    """FastAPI dependency — 任一 critical 依赖不可用则 503。

    设计：[[基础设施健康检查与产品不可用-设计]] §3.2.2

    detail body:
      - 普通用户：{"code": "INFRA_UNHEALTHY", "message": "系统暂时不可用，请联系管理员"}
      - Admin   ：上同 + "deps": app.state.infra_status

    用法：在 APIRouter 构造里 dependencies=[Depends(require_infra_healthy)]。
    """
    # getattr(obj, name, default)：安全地取 obj.name，不存在时返回 default（这里是 None）
    # app.state 是 FastAPI 提供的全局状态容器，startup 阶段会往里写 infra_status
    state = getattr(request.app.state, "infra_status", None)

    # infra_status 缺失说明 startup 还没跑完（或被跳过），视作未初始化
    if state is None:
        raise HTTPException(
            status_code=503,
            detail={
                "code": "INFRA_UNINITIALIZED",
                "message": "系统正在初始化，请稍后重试",
            },
        )

    # 列表推导式：筛出所有 ok=False 的依赖名（排除非致命依赖，如退役中的 neo4j）
    # state.items() 返回 (key, value) 对；v.get("ok") 取字典里的 ok 字段
    unhealthy = [
        k for k, v in state.items()
        if k not in _NON_CRITICAL_DEPS and not v.get("ok")
    ]

    # 全部 ok → 直接返回，不拦截
    if not unhealthy:
        return

    # 构造 detail：普通用户只给友好文案，admin 额外暴露 deps 字段便于排查
    # dict 是 Python 内置字典类型，{key: value} 语法创建字典字面量
    detail: dict = {
        "code": "INFRA_UNHEALTHY",
        "message": "系统暂时不可用，请联系管理员",
    }

    # user.is_admin 为 True 时才追加 deps 字段（admin 可以看到完整的依赖状态）
    if user.is_admin:
        detail["deps"] = state  # state 是 dict，直接赋值（引用，不复制）

    # raise 关键字：抛出异常；FastAPI 会把 HTTPException 转成对应 HTTP 响应
    raise HTTPException(status_code=503, detail=detail)
