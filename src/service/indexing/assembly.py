# src/service/indexing/assembly.py
"""按 job 的工程绑定装配真实 indexer（job→project→scm_connection→provider→make_real_indexer）。设计 §9。"""
from __future__ import annotations

# select 是 SQLAlchemy 的查询构造函数，用于构建 SELECT 语句
from sqlalchemy import select

# async_sessionmaker 是异步会话工厂类型，用于创建数据库会话
from sqlalchemy.ext.asyncio import async_sessionmaker

# 导入 ORM 模型：Project（工程）和 ScmConnection（SCM 连接）
from src.service.db_models_homepage import Project, ScmConnection

# make_real_indexer：工厂函数，返回真实 IndexerFn（clone + pipeline）
# _default_run_pipeline：默认 shell pipeline 执行器，可在测试中注入替代实现
from src.service.indexing.real_indexer import make_real_indexer, _default_run_pipeline


def build_indexer_for_job(maker: async_sessionmaker, *, provider, repos_root: str,
                          run_pipeline=_default_run_pipeline):
    """返回 IndexerFn：运行时按 job.project_id 解析绑定，再委托 make_real_indexer。

    Args:
        maker:        SQLAlchemy 异步会话工厂，用于打开数据库连接
        provider:     SCM 提供者对象（如 GitHubAppProvider），实现 clone() 方法
        repos_root:   本地仓库根目录（如 '/opt/ke-repos'），clone 目标在此下
        run_pipeline: pipeline 执行函数，默认调用真实 shell；测试可注入 fake

    Returns:
        IndexerFn — 签名为 async (job, progress) -> str（commit sha）
    """
    # 定义内部异步函数作为真正的 IndexerFn，捕获外部参数（闭包）
    async def _indexer(job, progress) -> str:
        """按 job 解析绑定后委托 make_real_indexer 执行 clone + pipeline。"""
        # async with maker() as s: 是 Python 异步上下文管理器语法
        # maker() 创建一个异步数据库会话 s，退出时自动关闭（相当于 finally: s.close()）
        async with maker() as s:
            # select(Project)... 构造 SELECT * FROM projects WHERE id=?
            # await s.execute(...) 异步执行查询，返回 Result 对象
            # .scalar_one() 取唯一一行的第一列（即 Project 对象），不存在则抛 NoResultFound
            p = (await s.execute(select(Project).where(Project.id == job.project_id))).scalar_one()

            # 检查工程是否已绑定 SCM 连接（scm_connection_id 不为 NULL）
            if not p.scm_connection_id:
                # raise RuntimeError 抛出运行时异常，f-string 是 Python 格式化字符串语法
                raise RuntimeError(f"工程 {p.id} 未绑定 SCM 连接")

            # 查询对应的 ScmConnection 记录；用 scalar_one_or_none() 代替 scalar_one()
            # 这样当连接行不存在时得到 None，而不是 SQLAlchemy 抛出难以理解的 NoResultFound
            conn = (await s.execute(
                select(ScmConnection).where(ScmConnection.id == p.scm_connection_id)
            )).scalar_one_or_none()

            # 连接行不存在（如被删除后 FK 未及时置空的时间窗口）→ 给出可读的错误信息
            if conn is None:
                raise RuntimeError(
                    f"工程 {p.id} 绑定的 SCM 连接 {p.scm_connection_id} 不存在（可能已被删除）"
                )

            # 从数据库对象提取 clone 所需的参数，赋给局部变量后会话即可关闭
            installation_id = conn.github_installation_id  # GitHub App 安装 ID（整数）
            full_name, ref, subpath = p.repo_full_name, p.ref, p.subpath  # 仓库全名/分支/子路径

            # PAT 类型连接的 github_installation_id 为 None；传 None 进 App clone 路径会触发
            # 难以定位的 TypeError。在会话块内尽早检查，给出清晰的运维可读错误
            if installation_id is None:
                raise RuntimeError(
                    f"工程 {p.id} 绑定的连接 {conn.id} 不是 GitHub App 类型"
                    "（github_installation_id 为空），当前 worker 仅支持 GitHub App 安装"
                )

        # make_real_indexer 返回一个 IndexerFn（闭包），把 clone 参数预绑定进去
        # 这里用关键字参数（keyword-only）显式传入所有配置，提高可读性
        real = make_real_indexer(
            provider=provider,
            installation_id=installation_id,
            full_name=full_name,
            ref=ref,
            subpath=subpath,
            repos_root=repos_root,
            run_pipeline=run_pipeline,
        )

        # await 是 Python asyncio 协程调用语法；real() 是一个异步函数，需要 await 等待结果
        # 返回 commit sha（40 位十六进制字符串）
        return await real(job, progress)

    # 把内部定义的异步函数作为返回值，调用方得到一个可直接当 IndexerFn 使用的协程函数
    return _indexer
