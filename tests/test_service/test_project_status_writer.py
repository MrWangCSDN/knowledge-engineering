"""project_status_writer 单元测试（内存 SQLite，注入 session_maker）。

验证点：
  1. update_to_partial：写 status + indexing_progress 统计键
  2. update_to_ready：写 status + pipeline_at（set_pipeline_at=True）
  3. merge 不 clobber：第二次写新键时旧键保留
  4. 不存在的 project_id：no-op，不抛错
"""

# pytest 是 Python 测试框架
import pytest
# pytest_asyncio 扩展：支持 async fixture
import pytest_asyncio

# SQLAlchemy 异步引擎 + session 工厂
from sqlalchemy.ext.asyncio import create_async_engine, async_sessionmaker

# Base 定义在 src.service.db（db_models_homepage 从那里 re-export）
from src.service.db import Base
# Project ORM 模型，字段：id/name/status/indexing_progress/pipeline_at
from src.service.db_models_homepage import Project
# 被测函数
from src.service.project_status_writer import update_project_status


# ───────── fixture ──────────────────────────────────────────────────────────

@pytest_asyncio.fixture  # 声明这是一个 async fixture（pytest_asyncio 专用装饰器）
async def session_maker():
    """提供内存 SQLite 的 async_sessionmaker，并预置一个 project 行。

    每个测试拿到一个全新的内存库，测试间完全隔离。
    """
    # create_async_engine：创建异步数据库引擎，使用 aiosqlite 驱动
    # "sqlite+aiosqlite:///:memory:" → 纯内存 SQLite，进程退出即消失
    eng = create_async_engine("sqlite+aiosqlite:///:memory:", echo=False)

    # 用 begin() 上下文管理器自动 commit/rollback 一个连接事务
    async with eng.begin() as conn:
        # run_sync 在异步上下文中运行同步函数（create_all 是同步的）
        # Base.metadata.create_all 会按 ORM 定义创建所有表
        await conn.run_sync(Base.metadata.create_all)

    # async_sessionmaker：session 工厂；expire_on_commit=False = commit 后对象不过期
    SM = async_sessionmaker(eng, expire_on_commit=False)

    # 预置一个测试用的 project 行（id="p1"）
    async with SM() as s:
        # Project 必填：id(主键) + name(NOT NULL)，其余字段有 default 或 nullable=True
        s.add(Project(id="p1", name="proj", status="indexing", indexing_progress=None))
        await s.commit()  # 提交插入

    # 把 session_maker 工厂返回给测试函数
    return SM


# ───────── 测试用例 ──────────────────────────────────────────────────────────

@pytest.mark.asyncio  # 告知 pytest-asyncio：此函数是 async 测试，需要事件循环
async def test_update_to_partial_merges_stats(session_maker):
    """更新到 partial 时，统计键被正确写入 indexing_progress。"""
    # 调用被测函数，传入统计参数
    await update_project_status(
        session_maker, "p1", "partial",
        methods_count=150, classes_count=30, interpretation_progress=0,
    )

    # 重新打开 session 读取结果，验证写入正确
    async with session_maker() as s:
        # s.get(Model, pk)：按主键查询单行；返回 None 表示不存在
        p = await s.get(Project, "p1")
        assert p.status == "partial"
        # indexing_progress 是 JSON 字段，读出来是 dict
        assert p.indexing_progress["methods_count"] == 150
        assert p.indexing_progress["interpretation_progress"] == 0


@pytest.mark.asyncio
async def test_update_to_ready_sets_pipeline_at_and_100(session_maker):
    """更新到 ready 且 set_pipeline_at=True 时，pipeline_at 被写入且不为 None。"""
    await update_project_status(
        session_maker, "p1", "ready",
        methods_count=150, classes_count=30, interpretation_progress=100,
        set_pipeline_at=True,
    )

    async with session_maker() as s:
        p = await s.get(Project, "p1")
        assert p.status == "ready"
        assert p.indexing_progress["interpretation_progress"] == 100
        # pipeline_at 应该被设置为当前 UTC 时间（非 None）
        assert p.pipeline_at is not None


@pytest.mark.asyncio
async def test_merge_does_not_clobber_existing_keys(session_maker):
    """两次写入时，第一次写的键不被第二次覆盖（merge 语义）。"""
    # 第一次写：phase + percent + eta_seconds
    await update_project_status(session_maker, "p1", "indexing",
                                phase="embedding", percent=40, eta_seconds=120)
    # 第二次写：只传 methods_count，不传 phase
    await update_project_status(session_maker, "p1", "partial", methods_count=10)

    async with session_maker() as s:
        p = await s.get(Project, "p1")
        # 第二次写的新键正确写入
        assert p.indexing_progress["methods_count"] == 10
        # 第一次写的键未被 clobber（merge 保留旧键）
        assert p.indexing_progress["phase"] == "embedding"


@pytest.mark.asyncio
async def test_missing_project_is_noop(session_maker):
    """project_id 不存在时，函数静默 no-op，不抛任何异常。"""
    # "nope" 不存在于 DB，预期：不报错、不抛异常
    await update_project_status(session_maker, "nope", "ready")
