"""回写 project.status 流转 + 进度统计。

设计要点：
- 核心 async（注入 session_maker，便于内存 SQLite 单测）。
- sync 包装供同步 pipeline 调用（asyncio.run），与离线脚本同范式。
- 只改 status + indexing_progress(JSON)，字段 merge 不 clobber。无 Alembic 迁移。

使用方式（sync pipeline）：
    from src.service.project_status_writer import update_project_status_sync
    update_project_status_sync(session_maker, project_id="deposit-system", status="ready",
                               methods_count=200, classes_count=40, interpretation_progress=100,
                               set_pipeline_at=True)

使用方式（async context）：
    await update_project_status(session_maker, project_id, status, ...)
"""
from __future__ import annotations  # 延迟类型注解求值，允许注解里引用还未完整定义的类型

import asyncio  # Python 标准库：事件循环与协程调度（async/await 的底层基础）
from datetime import datetime, timezone  # datetime：日期时间对象；timezone：时区常量
from typing import Optional  # Optional[X] 等价于 Union[X, None]，表示该参数可为 None

# SQLAlchemy 异步 session 工厂类型（用于类型注解，表明参数期望一个 async_sessionmaker）
from sqlalchemy.ext.asyncio import async_sessionmaker

# flag_modified：显式标记 JSON/JSONB 字段已变更。
# 原因：SQLAlchemy 对可变对象（dict/list）的原地修改无法自动检测变更，
# 不调用 flag_modified 时 dict 的内容修改不会被追踪到，commit 后不写库。
from sqlalchemy.orm.attributes import flag_modified

# Project ORM 模型：映射到 projects 表（字段：id/name/status/indexing_progress/pipeline_at）
from src.service.db_models_homepage import Project


async def update_project_status(
    session_maker: async_sessionmaker,  # 注入的 session 工厂（便于单测替换为内存 SQLite）
    project_id: str,                    # 目标工程 ID（对应 projects.id）
    status: str,                        # 新状态值，如 "indexing" / "partial" / "ready" / "failed"
    *,  # * 之后的参数全部为 keyword-only（调用时必须写参数名，避免位置参数顺序混淆）
    methods_count: Optional[int] = None,           # 索引到的方法总数
    classes_count: Optional[int] = None,           # 索引到的类总数
    interpretation_progress: Optional[int] = None, # 解读完成百分比（0-100）
    phase: Optional[str] = None,                   # 当前阶段名，如 "embedding" / "graph"
    percent: Optional[int] = None,                 # 当前阶段进度百分比
    eta_seconds: Optional[int] = None,             # 预计剩余秒数
    set_pipeline_at: bool = False,                 # True 时将 pipeline_at 更新为当前 UTC 时间
) -> None:
    """更新一个工程的 status + indexing_progress JSON（merge 语义）。

    project 不存在则静默 no-op（不抛错，不写库）。
    JSON 字段采用 merge 写入：只更新传入的键，不传入的键保留原值（不 clobber）。

    Args:
        session_maker: SQLAlchemy async_sessionmaker，可注入内存 SQLite 用于单测。
        project_id: 目标工程 ID。
        status: 新状态字符串。
        methods_count: 可选，方法数统计。
        classes_count: 可选，类数统计。
        interpretation_progress: 可选，解读进度 0-100。
        phase: 可选，当前 pipeline 阶段名。
        percent: 可选，当前阶段完成百分比。
        eta_seconds: 可选，预计剩余时间（秒）。
        set_pipeline_at: 若为 True，将 pipeline_at 设为当前 UTC 时间。
    """
    # async with session_maker() as s：异步上下文管理器，自动管理 session 生命周期
    # session_maker() 返回一个 AsyncSession；with 块结束时自动 close（不自动 commit）
    async with session_maker() as s:
        # s.get(Model, primary_key)：按主键查询，返回 ORM 对象或 None（不存在时）
        # await 是 Python async/await 语法：挂起当前协程，等待异步操作完成后继续
        p = await s.get(Project, project_id)

        # 若 project 不存在，静默返回（no-op 设计：pipeline 可能传错 ID，不应崩溃）
        if p is None:
            return

        # 更新 status 字段（直接赋值，SQLAlchemy 会追踪此变更）
        p.status = status

        # ─── JSON merge 写入 ────────────────────────────────────────
        # dict(p.indexing_progress or {})：
        #   - p.indexing_progress 可能是 None（首次写入），用 or {} 提供默认空 dict
        #   - dict(...) 浅拷贝：避免直接修改原对象引用，保证 SQLAlchemy 能检测变更
        prog = dict(p.indexing_progress or {})

        # 遍历所有可选统计键，只把"调用方传入了值（非 None）"的键更新到 prog
        # for key, val in (...)：元组列表解包，每次迭代拿到 (字段名, 值) 对
        for key, val in (
            ("methods_count", methods_count),
            ("classes_count", classes_count),
            ("interpretation_progress", interpretation_progress),
            ("phase", phase),
            ("percent", percent),
            ("eta_seconds", eta_seconds),
        ):
            # if val is not None：只在明确传入值时才写入，None 表示"不更新这个键"
            # 这实现了 merge 语义：旧键不被 None 参数 clobber
            if val is not None:
                prog[key] = val

        # 把 merge 后的 dict 重新赋回字段
        p.indexing_progress = prog

        # flag_modified 显式告知 SQLAlchemy：这个 JSON 字段已变更，需要写库
        # 原因：SQLAlchemy 无法自动感知 dict 内部的原地修改（可变对象陷阱）
        # 即使我们做了 dict() 拷贝 + 重新赋值，JSON 列有时仍需要此标记才可靠追踪
        flag_modified(p, "indexing_progress")

        # ─── 可选：更新 pipeline_at ──────────────────────────────────
        if set_pipeline_at:
            # datetime.now(timezone.utc)：获取当前时间（带 UTC 时区信息）
            # 推荐始终用 timezone.utc 而不是 datetime.utcnow()（后者无时区信息，易混淆）
            p.pipeline_at = datetime.now(timezone.utc)

        # 提交事务：将所有变更持久化到数据库
        await s.commit()


def update_project_status_sync(
    session_maker: async_sessionmaker,
    project_id: str,
    status: str,
    **kwargs,  # **kwargs：收集所有额外关键字参数，转发给 update_project_status
) -> None:
    """同步包装：供 sync pipeline 调用。

    离线 pipeline 无运行中的 event loop，用 asyncio.run() 创建一次性事件循环安全。
    注意：若调用方已在 async context 中（如 FastAPI 路由），请直接 await update_project_status。

    Args:
        session_maker: 同 update_project_status。
        project_id: 同 update_project_status。
        status: 同 update_project_status。
        **kwargs: 透传给 update_project_status 的可选参数（methods_count 等）。
    """
    # asyncio.run(coro)：创建新的事件循环 → 运行协程 → 关闭事件循环
    # 等价于：loop = asyncio.new_event_loop(); loop.run_until_complete(coro); loop.close()
    # **kwargs 解包字典作为关键字参数传入，与调用方传的参数名一一对应
    asyncio.run(update_project_status(session_maker, project_id, status, **kwargs))
