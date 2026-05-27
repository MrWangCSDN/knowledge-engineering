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
    """4 个 critical 依赖的整体状态（v2 起 Ollama 已切 DashScope 不再依赖）。"""
    mysql: DepStatus
    neo4j: DepStatus
    weaviate: DepStatus
    dashscope: DepStatus


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


# ─── _ping_weaviate ──────────────────────────────────────────────────────

# httpx：纯 Python 的异步 HTTP 客户端，比 requests 更适合 asyncio；
# 这里不用 weaviate-client SDK，而是直接 HTTP 探活，更轻量、不需要完整初始化
import httpx


async def _ping_weaviate(url: str | None, api_key: str | None) -> DepStatus:
    """ping Weaviate：GET /v1/.well-known/live 验证服务存活。

    :param url: Weaviate base URL，如 'http://43.228.76.163:8080'
    :param api_key: API key（live endpoint 不需要 auth，保留参数以备未来切到 ready 检查）
    :returns: {"ok": True} 或 {"ok": False, "error": "..."}
    """
    # config sanity：URL 为 None / 空 → 短路，避免连接 5s 后才超时
    if not url:
        return {"ok": False, "error": "WEAVIATE_URL not configured"}

    # /v1/.well-known/live 端点：进程存活就返 200，不需要 auth
    # /v1/.well-known/ready 更严格（等 raft 完成），但 raft 选举期间会 503，对 startup 过严
    live_url = url.rstrip("/") + "/v1/.well-known/live"

    try:
        # httpx.AsyncClient 作为异步上下文管理器（with 块结束自动关连接）
        # timeout= 传给所有请求（connect + read 合计）
        async with httpx.AsyncClient(timeout=PING_TIMEOUT_SEC) as client:
            # await client.get() 发送异步 GET 请求，返回 Response 对象
            resp = await client.get(live_url)
            # status_code 是整型 HTTP 状态码（200 / 503 等）
            if resp.status_code == 200:
                return {"ok": True}
            return {"ok": False, "error": f"Weaviate live status={resp.status_code}"}

    # httpx.TimeoutException：connect timeout 或 read timeout 都会走这里
    except httpx.TimeoutException:
        return {"ok": False, "error": f"Weaviate ping timeout (>{PING_TIMEOUT_SEC}s)"}
    except Exception as e:
        # 其余网络错误（DNS 解析失败、连接被拒）
        return {"ok": False, "error": f"{type(e).__name__}: {e}"}


# ─── _ping_dashscope ─────────────────────────────────────────────────────

async def _ping_dashscope(api_key: str | None) -> DepStatus:
    """ping DashScope：embedding 1 字符验证 API key + 服务可达。

    cost ≈ ¥0.000001（1 token），可忽略；用 text-embedding-v4 模型（1024 维）。

    :param api_key: DashScope API key，如 'sk-...'
    :returns: {"ok": True} 或 {"ok": False, "error": "..."}
    """
    # config sanity：key 为 None / 空 → 短路
    if not api_key:
        return {"ok": False, "error": "DASHSCOPE_API_KEY not configured"}

    # DashScope 原生 embedding endpoint（区别于 /compatible-mode/ OpenAI 兼容路径）
    url = "https://dashscope.aliyuncs.com/api/v1/services/embeddings/text-embedding/text-embedding"

    # HTTP headers：Authorization Bearer 是 DashScope 鉴权方式；Content-Type 告知 server 我们发 JSON
    headers = {
        "Authorization": f"Bearer {api_key}",  # f-string 拼接；f"..." 是 Python 3.6+ 格式化字符串
        "Content-Type": "application/json",
    }

    # payload：最小化请求体，只发 1 个字符"."，token 消耗 ≈ 1
    # dict 嵌套 dict 是 Python 中表达 JSON 结构的惯用写法
    payload = {"model": "text-embedding-v4", "input": {"texts": ["."]}}

    try:
        async with httpx.AsyncClient(timeout=PING_TIMEOUT_SEC) as client:
            # client.post：发 HTTP POST，json= 参数自动序列化 dict → JSON 并设 Content-Type
            resp = await client.post(url, headers=headers, json=payload)
            if resp.status_code == 200:
                return {"ok": True}
            # 401 = Bad API key；非 200 统一返 error
            return {"ok": False, "error": f"DashScope status={resp.status_code}"}

    except httpx.TimeoutException:
        return {"ok": False, "error": f"DashScope ping timeout (>{PING_TIMEOUT_SEC}s)"}
    except Exception as e:
        return {"ok": False, "error": f"{type(e).__name__}: {e}"}


# ─── check_all_deps 编排 ────────────────────────────────────────────────

async def check_all_deps(app_state) -> InfraStatus:
    """并发 ping 4 个 critical 依赖，返回完整状态。

    从 os.environ 读 4 个依赖的 config（与 service/api.py 中 endpoint init 用的同源），
    不读 app_state（app_state 当前留作未来扩展点）。

    并发用 asyncio.gather；每个 ping 内部自己有 5s timeout，所以 gather 总时长 ≤ 5s。

    历史：v1 含 ollama ping；v2 起 embedding 切 DashScope，删除 Ollama 依赖。

    :param app_state: FastAPI app.state，留作未来扩展（当前未使用）
    :returns: {"mysql": DepStatus, "neo4j": DepStatus, "weaviate": DepStatus,
               "dashscope": DepStatus}
    """
    import os  # os 模块提供读取环境变量的 os.environ.get()

    # 从环境变量读 4 个 config；与 service/api.py 中 endpoint init 同源
    db_url = os.environ.get("KE_DB_URL")
    neo4j_uri = os.environ.get("NEO4J_URI")
    neo4j_user = os.environ.get("NEO4J_USER", "neo4j")  # 默认值 "neo4j" 是 Neo4j 的默认用户名
    neo4j_password = os.environ.get("NEO4J_PASSWORD")
    weaviate_url = os.environ.get("WEAVIATE_URL")
    weaviate_api_key = os.environ.get("WEAVIATE_API_KEY")
    dashscope_api_key = os.environ.get("DASHSCOPE_API_KEY")

    # asyncio.gather：并发 await 多个 coroutine，按入参顺序返回结果列表
    # 等效于"同时发出 4 个网络请求，等所有都完成后一次性拿结果"
    # 与串行 await 逐个调用相比，总耗时 = max(单个耗时) 而非 sum(单个耗时)
    mysql_r, neo4j_r, weaviate_r, dashscope_r = await asyncio.gather(
        _ping_mysql(db_url),
        _ping_neo4j(neo4j_uri, neo4j_user, neo4j_password),
        _ping_weaviate(weaviate_url, weaviate_api_key),
        _ping_dashscope(dashscope_api_key),
    )

    # 按 key 名组装返回 dict，与 InfraStatus TypedDict 对应
    return {
        "mysql": mysql_r,
        "neo4j": neo4j_r,
        "weaviate": weaviate_r,
        "dashscope": dashscope_r,
    }
