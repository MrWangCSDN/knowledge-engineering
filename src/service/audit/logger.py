"""统一审计日志写入入口。

设计原则：
  1. 不主动 commit —— 让 caller 跟业务事务一起 commit，保证原子性。
  2. 不抛错       —— 审计写入失败仅 log warning，不中断业务。
                    （审计落库 bug 不能让真实业务挂掉）

依赖：
  - SQLAlchemy 2.0 AsyncSession
  - src.service.db_models_groups.AuditLog（ORM 模型）

使用示例：
    from src.service.audit.logger import log_audit
    from src.service.audit import actions

    async def create_project(db: AsyncSession, ...):
        project = Project(...)
        db.add(project)
        # 审计日志和业务操作共用同一个 session，一起 commit
        await log_audit(
            db,
            actor_user_id=current_user.id,
            action=actions.PROJECT_CREATE,
            resource_type="project",
            resource_id=project.id,
            metadata={"name": project.name},
        )
        await db.commit()   # 业务 + 审计 原子提交
"""
from __future__ import annotations  # 延迟注解解析（PEP 563），让 Optional[X] 不需要 X 在运行时可用

import json         # 标准库：dict → JSON 字符串（metadata 序列化用）
import logging      # 标准库：Python 内置日志系统

# Optional[X] 表示"值可以是 X 类型，也可以是 None"
from typing import Optional

# AsyncSession：SQLAlchemy 2.0 异步 session 类型（用于类型注解）
from sqlalchemy.ext.asyncio import AsyncSession

# AuditLog：要写入的 ORM 模型，对应 'audit_logs' 表
from src.service.db_models_groups import AuditLog

# 模块级 logger：遵循 Python logging 最佳实践，用 __name__ 作为 logger 名称
# __name__ 在此模块中值为 "src.service.audit.logger"
# 这样日志可以按模块名精确过滤（如 logging.getLogger("src.service.audit") 会捕获此 logger）
logger = logging.getLogger(__name__)


async def log_audit(
    db: AsyncSession,
    *,                                        # * 后的参数全部为关键字参数（keyword-only）
    actor_user_id: int,                       # 操作人的 User.id
    action: str,                              # 操作动作，建议使用 audit.actions 中的常量
    resource_type: str,                       # 资源类型，如 'project' / 'group' / 'user'
    resource_id: str,                         # 资源 ID（字符串形式，兼容 int ID 和 slug）
    metadata: Optional[dict] = None,          # 附加上下文信息（可选），会被 JSON 序列化
    ip_address: Optional[str] = None,         # 客户端 IP 地址（可选）
) -> None:
    """写入一条审计日志到当前 AsyncSession（不主动 commit）。

    审计失败仅 log warning，不抛错——审计失败 ≠ 业务失败。
    caller 负责与业务事务一起 commit。

    Args:
        db            : 当前请求的 AsyncSession（与业务事务共用，一起 commit）。
        actor_user_id : 执行操作的用户 ID（User.id）。
        action        : 操作字符串，建议使用 audit.actions 中定义的常量。
                        格式：resource_type.verb，如 'project.create'。
        resource_type : 被操作的资源类型，5 种之一：
                        'project' / 'group' / 'user' / 'credential' / 'message'。
        resource_id   : 被操作的资源 ID（字符串）。
        metadata      : 附加上下文 dict（如 {'name': 'PetClinic', 'group_id': 'retail'}）。
                        为 None 时写入 '{}'（空 JSON 对象）。
        ip_address    : 客户端 IP 地址，可为 None。

    Returns:
        None（无返回值，副作用是向 session 添加了一条 AuditLog）。

    Example:
        await log_audit(
            db,
            actor_user_id=1,
            action=actions.PROJECT_CREATE,
            resource_type="project",
            resource_id="petclinic",
            metadata={"name": "PetClinic"},
        )
        await db.commit()   # 调用方负责 commit
    """
    # try/except：捕获 add() 过程中可能发生的任何异常
    # 目的：审计写入失败不能让业务挂掉（容错设计）
    try:
        # 构造 AuditLog ORM 实例
        # json.dumps：将 dict 序列化为 JSON 字符串
        #   - metadata or {}：如果 metadata 是 None，改用空 dict（Python 的短路 or）
        #   - ensure_ascii=False：允许中文等 Unicode 字符直接存储，不转义为 \uXXXX
        db.add(AuditLog(
            actor_user_id=actor_user_id,
            action=action,
            resource_type=resource_type,
            resource_id=str(resource_id),   # str()：统一转为字符串（兼容 int ID 传入）
            metadata_json=json.dumps(metadata or {}, ensure_ascii=False),
            ip_address=ip_address,
        ))
        # 注意：这里只调用 db.add()，不调用 await db.commit()
        # 让 caller（业务层）把业务操作 + 审计日志一起原子提交

    except Exception as e:
        # Exception：捕获所有标准异常（不包括 BaseException 的系统级异常）
        # logger.warning：记录警告级别日志（不是 error，因为审计失败不应触发告警）
        # %s 是 logging 的惰性格式化占位符（比 f-string 更高效，只在需要输出时格式化）
        logger.warning(
            "audit log 写入失败 action=%s resource=%s:%s err=%s",
            action,           # 操作名
            resource_type,    # 资源类型
            resource_id,      # 资源 ID
            e,                # 异常对象（str(e) 会自动调用）
        )
        # 不 raise：吞掉异常，让业务逻辑继续执行
