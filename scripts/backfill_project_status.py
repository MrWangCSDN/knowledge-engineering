"""一次性 backfill：按 Weaviate 实际解读覆盖率，修正现有工程的 status + stats。

用法：
  dry-run（只打印不改）： ./venv/bin/python -m scripts.backfill_project_status
  apply（落库）：         ./venv/bin/python -m scripts.backfill_project_status --apply

依赖（项目 venv 已含）：
    sqlalchemy、weaviate-client（通过 src 间接引入）
"""
# from __future__ import annotations 让 Python 3.9 也能写 dict[str, ...] 形式的类型注解
from __future__ import annotations

# argparse：标准库，解析命令行参数（--apply）
import argparse

# asyncio：标准库，用于跑 async 协程入口（SQLAlchemy async session 需要 await）
import asyncio

# datetime / timezone：标准库，生成 UTC 时间戳写 pipeline_at
from datetime import datetime, timezone

# select：SQLAlchemy 2.x 推荐的查询构造器，等效于 SELECT * FROM ...
from sqlalchemy import select

# get_session_maker 返回 async_sessionmaker[AsyncSession]，创建数据库 session
from src.service.db import get_session_maker

# Project：projects 表的 ORM 类（首页/工程元数据）
from src.service.db_models_homepage import Project

# interpretation_percent：把 {phase: {done, total}} → 0-100 整型百分比（纯函数）
# methods_total：从进度字典里取 tech.total 作为可解读方法总数（纯函数）
from src.pipeline.interpretation_progress_calc import interpretation_percent, methods_total


# ─── 纯决策函数（可单测，无 IO）────────────────────────────────────────────────

def decide_status(progress: dict[str, dict[str, int]]) -> tuple[str, int, int]:
    """据进度计数定 (status, interpretation_progress%, methods_count)。

    判断规则（边界安全，绝不误标）：
    - total<=0 或 done<=0 → indexing（无数据时维持原状，不误标 ready）
    - done >= total 且 total>0 → ready（全量解读完成）
    - 其余 → partial（有数据但未完成）

    Args:
        progress: 形如 {"tech": {"done": N, "total": M}, "biz": {...}} 的计数字典。
                  来自 get_interpretation_progress_from_weaviate。
    Returns:
        三元组 (status, pct, methods)：
            status  — "indexing" | "partial" | "ready"
            pct     — 0-100 整型百分比（interpretation_percent 的结果）
            methods — tech phase total 方法数（methods_total 的结果）
    """
    # 调用已有纯函数计算百分比和方法数
    pct = interpretation_percent(progress)       # 0-100 整数
    methods = methods_total(progress)            # tech.total 整数

    # 跨所有 phase 汇总 total / done
    # sum(生成器表达式)：对 progress.values() 里每个 phase dict 取 total，求和
    total = sum(p.get("total", 0) for p in progress.values())
    done = sum(p.get("done", 0) for p in progress.values())

    # 无任何已索引方法 → 不改状态，保持 indexing
    if total <= 0 or done <= 0:
        return ("indexing", 0, methods)

    # 全量完成（done 达到或超过 total）→ ready
    if done >= total:
        return ("ready", 100, methods)

    # 有数据但未完成 → partial
    return ("partial", pct, methods)


# ─── 工具函数：查单工程 Weaviate 进度 ─────────────────────────────────────────

def _progress_for(project: Project) -> dict[str, dict[str, int]]:
    """查该工程在 Weaviate 里的解读进度。

    ⚠️ 当前实现用固定 config_path="config/project.yaml"（单工程占位）。
    多工程环境需按 project.repo_local_path 或数据库字段派生各自的 config 路径。
    这是已知弱点，用户跑时请按实际路径调整（超出本轮 backfill 范围）。

    Args:
        project: Project ORM 实例（暂未用于派生 config_path，留作未来扩展）。
    Returns:
        形如 {"tech": {"done": N, "total": M}, ...} 的进度字典。
    """
    # 懒加载：放在函数内避免 import 时触发 Weaviate 连接
    # 仅在真正执行 main 时才会 import，不影响 decide_status 的单测
    from src.pipeline.interpretation_standalone import get_interpretation_progress_from_weaviate

    # ⚠️ 占位：多工程时需按 project.id / repo_local_path 查对应 config
    config_path = "config/project.yaml"
    return get_interpretation_progress_from_weaviate(config_path)


# ─── 主流程（async，需 asyncio.run 驱动）──────────────────────────────────────

async def main(apply: bool) -> None:
    """遍历所有工程，按 Weaviate 实际进度决策 status，dry-run 打印或 apply 落库。

    Args:
        apply: True=落库提交；False=只打印，不写 DB（dry-run 安全模式）。
    """
    # 取 async sessionmaker（工厂函数，每次 async with 创建一个 session）
    SM = get_session_maker()

    # async with SM() as s：异步上下文管理器，等效于 try/finally session.close()
    async with SM() as s:
        # select(Project) → SELECT * FROM projects；await 异步执行
        projects = (await s.execute(select(Project))).scalars().all()

        # 遍历每个工程，计算应有 status
        for p in projects:
            # 查 Weaviate 进度（真实 IO；dry-run 也会查，只是不写 DB）
            prog = _progress_for(p)

            # 纯函数决策（可单测）
            status, pct, methods = decide_status(prog)

            # 打印当前→目标（dry-run 和 apply 都打印，方便审计）
            print(
                f"[{'APPLY' if apply else 'DRY '}] "
                f"{p.id}: {p.status!r} → {status!r} "
                f"(解读 {pct}%, {methods} 方法)"
            )

            # apply 模式且状态确实需要变更时才写 DB
            if apply and status != p.status:
                p.status = status

                # 合并 JSON 字段（dict copy，不 clobber 其他已有 key）
                # dict(p.indexing_progress or {}) 是防 None 的保险写法
                merged = dict(p.indexing_progress or {})
                merged["interpretation_progress"] = pct   # 解读百分比
                merged["methods_count"] = methods          # 方法总数
                p.indexing_progress = merged               # 赋回触发 SQLAlchemy 脏检测

                # ready 时记录 pipeline 完成时间（UTC）
                if status == "ready":
                    p.pipeline_at = datetime.now(timezone.utc)

        # apply 时一次性提交（事务原子性）；dry-run 不提交
        if apply:
            await s.commit()
            print("\n✅ backfill 已提交。")
        else:
            print("\n🔍 dry-run 完成，未写库。加 --apply 才落库。")


# ─── CLI 入口 ─────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    # ArgumentParser：标准库命令行参数解析
    ap = argparse.ArgumentParser(
        description="backfill 工程 status + 解读进度（基于 Weaviate 实际数据）"
    )
    # store_true：--apply 出现时 args.apply=True，不出现时 False（dry-run）
    ap.add_argument("--apply", action="store_true", help="落库；缺省仅 dry-run 打印")
    args = ap.parse_args()

    # asyncio.run：创建事件循环、驱动协程、结束后关闭循环（Python 3.7+ 标准用法）
    asyncio.run(main(args.apply))
