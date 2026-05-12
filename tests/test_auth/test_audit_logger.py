"""审计日志 helper 的单元测试。

测试内容：
  1. log_audit 能将一行 AuditLog 写入 session（主路径）
  2. log_audit 本身不抛错（容错设计：审计失败 ≠ 业务失败）
  3. actions 模块的关键常量值正确

运行方式：
    venv/bin/python -m pytest tests/test_auth/test_audit_logger.py -v
"""
import json  # 标准库：JSON 序列化/反序列化

# pytest 是 Python 测试框架；asyncio 支持 async def 测试函数
import pytest
import pytest_asyncio  # pytest 的 asyncio 扩展，用于 async fixture

# SQLAlchemy 2.0 select：构造 SELECT 查询
from sqlalchemy import select

# 创建异步引擎 + sessionmaker
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

# 被测模块：audit logger 和 actions 常量
from src.service.audit.logger import log_audit
from src.service.audit import actions

# User 模型：测试中要先插入一个用户（满足外键约束）
from src.service.auth_models import User

# Base：所有 ORM 模型共用的 DeclarativeBase，create_all 时需要
from src.service.db import Base

# 被写入的目标 ORM 模型
from src.service.db_models_groups import AuditLog


@pytest_asyncio.fixture
async def db():
    """提供一个内存 SQLite 异步 session，并预置一个 user 行（满足 FK）。

    使用 aiosqlite 驱动（纯内存 DB，测试间互不干扰）。
    yield 相当于 setUp / tearDown 的结合：yield 前 = setUp，yield 后 = tearDown（此处无 tearDown）。
    """
    # create_async_engine：创建异步数据库引擎
    # "sqlite+aiosqlite:///:memory:" 表示：驱动=aiosqlite，地址=内存（每次运行全新）
    eng = create_async_engine("sqlite+aiosqlite:///:memory:")

    # async with eng.begin() as conn：异步上下文管理器，自动 begin/commit 一个连接
    async with eng.begin() as conn:
        # run_sync 让同步函数（create_all）在异步事件循环中安全运行
        # Base.metadata.create_all 会创建所有 ORM 模型对应的表
        await conn.run_sync(Base.metadata.create_all)

    # async_sessionmaker：创建异步 session 工厂
    # expire_on_commit=False：commit 后不自动过期对象（避免额外的懒加载查询）
    SM = async_sessionmaker(eng, expire_on_commit=False)

    # 创建一个 session，插入测试用 user 行
    async with SM() as s:
        # 添加一个最小化 User，满足 AuditLog.actor_user_id 的 FK 约束
        s.add(User(
            id=1,
            email="a@x.com",
            username="a",
            hashed_password="x",
            is_active=True,
            is_admin=True,
        ))
        await s.commit()  # 提交插入事务

        # yield s：把 session 交给测试函数使用；测试结束后 session 自动关闭
        yield s


@pytest.mark.asyncio  # 标记此测试为异步测试，pytest-asyncio 会管理事件循环
async def test_log_audit_writes_row(db):
    """主路径：log_audit 将一条 AuditLog 写入 db.session，commit 后可查到。

    验证点：
    - action 字段值等于 'project.create'
    - resource_id 正确存储
    - metadata_json 可反序列化回 dict 且内容正确
    """
    # 调用被测函数：写入审计日志（只 add 到 session，不 commit）
    await log_audit(
        db,
        actor_user_id=1,                         # 操作人（上面 fixture 插入的 user）
        action=actions.PROJECT_CREATE,           # 使用常量，值应为 "project.create"
        resource_type="project",                 # 资源类型
        resource_id="petclinic",                 # 资源标识符
        metadata={"name": "PetClinic", "group_id": "retail"},  # 附加上下文
    )

    # commit：将 log_audit 写入的行持久化到 DB（业务方负责 commit，这里手动模拟）
    await db.commit()

    # 查询 AuditLog 表，期望恰好 1 行
    # db.scalars(select(AuditLog)).one()：返回唯一一行，不存在或多行都会抛错
    row = (await db.scalars(select(AuditLog))).one()

    # 断言 action 字段值
    assert row.action == "project.create"

    # 断言 resource_id 字段
    assert row.resource_id == "petclinic"

    # 将 metadata_json 字符串反序列化为 dict，验证内容
    meta = json.loads(row.metadata_json)
    assert meta["name"] == "PetClinic"


@pytest.mark.asyncio
async def test_log_audit_does_not_raise_on_failure(db):
    """容错测试：log_audit 本身不应抛出任何异常。

    设计原则：审计写入失败（如 FK 违反、DB 异常）不能让业务挂掉。
    注意：FK 违反在 commit 阶段才触发，这里只测 add() 过程本身不抛错。
    """
    # actor_user_id=999999 是一个不存在的用户 ID
    # FK 违反会在 commit() 时触发（这里我们不 commit），但 add() 阶段不应抛错
    await log_audit(
        db,
        actor_user_id=999999,   # 不存在的 user，理论上 FK 违反在 commit 时触发
        action="bad.action",    # 非常量 action 字符串（测试健壮性）
        resource_type="x",
        resource_id="y",
    )
    # 能走到这里说明 log_audit 没有抛错，容错设计正确
    assert True


def test_action_constants_present():
    """同步测试：验证关键 action 常量的名称和值均正确。

    这是一个纯枚举检查（不需要 DB），所以用普通同步函数，不加 @pytest.mark.asyncio。
    """
    # 验证工程相关常量
    assert actions.PROJECT_CREATE == "project.create"

    # 验证 Group 成员相关常量
    assert actions.GROUP_MEMBER_ADD == "group_member.add"

    # 验证认证相关常量
    assert actions.AUTH_LOGIN_SUCCESS == "auth.login_success"

    # 验证消息导出相关常量
    assert actions.MESSAGE_EXPORT_DOCX == "message.export_docx"
