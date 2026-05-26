"""基础设施健康检查 — 5 个 critical 依赖的轻量 ping。

设计文档：Obsidian `[[基础设施健康检查与产品不可用-设计]]` §3.1

每个 ping function：
1. 先做 config sanity check（None / 空字符串 → 短路返回，不真连）
2. 真连用 5s timeout，失败/超时 → 返回 ok=False + error 字符串
3. 不抛异常，永远返回 dict

公开 API:
- check_all_deps(app_state) -> dict[str, DepStatus]  # Task 3 实现
- DepStatus / InfraStatus TypedDict
"""
from __future__ import annotations

# asyncio：用 asyncio.wait_for 给每个 ping 限定 5s timeout，避免拖慢 startup
import asyncio
# typing：TypedDict 给 dict 一个静态结构，IDE / mypy 能识别
from typing import TypedDict, NotRequired


# 每个 ping 的 timeout 秒数；5s 是经验值（远端 SSH tunnel + 公网 API 都够用）
PING_TIMEOUT_SEC = 5


# ─── 类型定义（用 TypedDict 给 dict 结构化）─────────────────────────────────

class DepStatus(TypedDict):
    """单个依赖的健康状态。

    ok=True 时只有 ok 字段；ok=False 时附 error 字符串说明原因。
    """
    ok: bool
    # NotRequired 表示 ok=True 时可省略 error；ok=False 时必填
    error: NotRequired[str]


class InfraStatus(TypedDict):
    """5 个 critical 依赖的整体状态。"""
    mysql: DepStatus
    neo4j: DepStatus
    weaviate: DepStatus
    dashscope: DepStatus
    ollama: DepStatus


# ─── _ping_mysql ─────────────────────────────────────────────────────────

# 模块顶层 import：让测试 monkeypatch "src.service.infra_health.create_async_engine" 能生效
from sqlalchemy.ext.asyncio import create_async_engine
from sqlalchemy import text


async def _ping_mysql(db_url: str | None) -> DepStatus:
    """ping MySQL：SELECT 1 验证连接 + 鉴权可用。

    :param db_url: SQLAlchemy URL，如 'mysql+asyncmy://user:pw@host:3307/db'
    :returns: {"ok": True} 或 {"ok": False, "error": "..."}
    """
    # config sanity：URL 为 None / 空 → 短路（不阻塞 startup 5s）
    if not db_url:
        return {"ok": False, "error": "KE_DB_URL not configured"}

    try:
        async def _do_ping() -> DepStatus:
            # echo=False 不打印 SQL；future=True 让 engine 使用 2.x API
            engine = create_async_engine(db_url, echo=False, future=True)
            try:
                async with engine.connect() as conn:
                    # SELECT 1 是最轻量的 connection-alive 查询
                    await conn.execute(text("SELECT 1"))
                return {"ok": True}
            finally:
                # dispose 释放连接池，不留 socket 泄露
                await engine.dispose()

        # asyncio.wait_for 限 5s timeout；超时抛 asyncio.TimeoutError 由 except 抓
        return await asyncio.wait_for(_do_ping(), timeout=PING_TIMEOUT_SEC)

    except asyncio.TimeoutError:
        return {"ok": False, "error": f"MySQL ping timeout (>{PING_TIMEOUT_SEC}s)"}
    except Exception as e:
        # 捕获所有连接 / 认证 / 协议错误，返回简短字符串
        return {"ok": False, "error": f"{type(e).__name__}: {e}"}


# ─── _ping_neo4j ─────────────────────────────────────────────────────────

# 顶层 import 让测试 monkeypatch "src.service.infra_health.GraphDatabase" 生效
from neo4j import GraphDatabase


async def _ping_neo4j(uri: str | None, user: str | None, password: str | None) -> DepStatus:
    """ping Neo4j：RETURN 1 验证 bolt 连接 + 鉴权。

    :param uri: bolt:// URI，如 'bolt://host:7687'
    :param user: 用户名，通常 'neo4j'
    :param password: 密码
    """
    if not uri:
        return {"ok": False, "error": "NEO4J_URI not configured"}
    if not password:
        return {"ok": False, "error": "NEO4J_PASSWORD not configured"}

    def _sync_ping() -> DepStatus:
        # GraphDatabase.driver 是同步 driver；driver.session() 也同步
        # 包在 _sync_ping 里给 asyncio.to_thread 调度到线程池
        driver = GraphDatabase.driver(uri, auth=(user or "neo4j", password))
        try:
            with driver.session() as s:
                s.run("RETURN 1").single()
            return {"ok": True}
        finally:
            driver.close()

    try:
        # asyncio.to_thread：把同步代码扔到默认 executor 跑，让 asyncio.wait_for 能控住
        return await asyncio.wait_for(asyncio.to_thread(_sync_ping), timeout=PING_TIMEOUT_SEC)
    except asyncio.TimeoutError:
        return {"ok": False, "error": f"Neo4j ping timeout (>{PING_TIMEOUT_SEC}s)"}
    except Exception as e:
        return {"ok": False, "error": f"{type(e).__name__}: {e}"}
