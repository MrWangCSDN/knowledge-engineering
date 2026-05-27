# 基础设施健康检查与产品不可用 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 把 5 个 critical 基础设施（MySQL / Neo4j / Weaviate / DashScope / Ollama）任一挂明确升级为产品级不可用，后端 503 + 前端顶部红色横幅 + chat input disabled + 重试按钮。

**Architecture:** 后端新增 `infra_health.py` 5 ping function + `check_all_deps` 编排；startup hook 写 `app.state.infra_status`；`require_infra_healthy` dependency 注入到 9 个路由（除 auth/health 外）。前端 zustand `useInfraStore` + `<InfraBanner>` + axios interceptor + SSE 错误路径都识别 `INFRA_UNHEALTHY`。架构方案 A：on-demand fetch /health + 用户手动重试按钮，不起后台轮询线程。

**Tech Stack:** Python 3.12 / FastAPI / asyncio / SQLAlchemy async / neo4j driver / weaviate-client / httpx / pytest + React 19 / Zustand / Tailwind v4 / Vitest + RTL。

**仓库 / 分支:** `/Users/java/knowledge-engineering-auth` 分支 `release-0513`（后端）+ `/Users/java/knowledge-engineering-web` 分支 `feat/chit-chat-skill`（前端）。

**Spec 来源:** Obsidian `[[基础设施健康检查与产品不可用-设计]]`（已批准）。

**Run tests:**
- 后端：`cd /Users/java/knowledge-engineering-auth && ./venv/bin/python -m pytest tests/test_auth -q`
- 前端：`cd /Users/java/knowledge-engineering-web && npx vitest run`

---

## File Structure

### 后端 `knowledge-engineering-auth`

| 文件 | 改动 | 责任 |
|---|---|---|
| `src/service/infra_health.py` | 🆕 ~250 行 | 5 ping function + check_all_deps + InfraStatus/DepStatus TypedDict |
| `src/service/api.py` | Modify startup hook 末尾 + /health endpoint 重写 + 新增 require_infra_healthy | startup 写 app.state.infra_status；/health 实时重 ping；dependency 检查 state 拒 503 |
| `src/service/qa_router.py` | Modify line 55 router | 附 `dependencies=[Depends(require_infra_healthy)]` |
| `src/service/project_router.py` | Modify line 36 router | 同上 |
| `src/service/project_member_router.py` | Modify line 59 router | 同上 |
| `src/service/admin_router.py` | Modify line 50 router | 同上 |
| `src/service/qa_session_router.py` | Modify router | 同上 |
| `src/service/group_router.py` | Modify line 79 router | 同上 |
| `src/service/group_member_router.py` | Modify router | 同上 |
| `src/service/credentials_router.py` | Modify router | 同上 |
| `src/service/user_router.py` | Modify router | 同上 |
| `src/service/audit_router.py` | Modify router | 同上 |
| `src/service/auth_router.py` | 不动 | 决策 #8：login/me/logout/refresh **不**附 |
| `tests/test_auth/test_infra_health.py` | 🆕 | 5 ping function mock 测试 |
| `tests/test_auth/test_health_endpoint.py` | 🆕 | /health 三场景 × 未登录/普通/admin |
| `tests/test_auth/test_require_infra_healthy.py` | 🆕 | dependency 注入 + 503 + admin detail 差异 |
| `tests/test_auth/test_qa_router_503.py` | 🆕 | /qa/explain 在 unhealthy 下 503 |

### 前端 `knowledge-engineering-web`

| 文件 | 改动 | 责任 |
|---|---|---|
| `src/store/infra.ts` | 🆕 ~70 行 | zustand store + fetchHealth + markUnhealthy |
| `src/hooks/useInfraHealthBootstrap.ts` | 🆕 ~10 行 | App mount 时跑一次 fetchHealth |
| `src/components/layout/InfraBanner.tsx` | 🆕 ~50 行 | sticky 顶部红色横幅 + 重试按钮，light/dark 都 OK |
| `src/components/layout/AppLayout.tsx` (or 主 layout) | Modify | 注入 `<InfraBanner>` + 调 bootstrap hook |
| `src/components/chat/ChatInput.tsx` | Modify | disabled = !healthy + tooltip |
| `src/api/client.ts` | Modify | response interceptor 抓 503 INFRA_UNHEALTHY → markUnhealthy |
| `src/store/chat.ts` | Modify SSE error 块 | 识别 503 INFRA_UNHEALTHY 同步 markUnhealthy |
| `src/store/infra.test.ts` | 🆕 | store 行为 |
| `src/components/layout/InfraBanner.test.tsx` | 🆕 | 横幅渲染 + 重试按钮 |

---

## Task 1: infra_health.py 骨架 + `_ping_mysql` + `_ping_neo4j`

**Files:**
- Create: `src/service/infra_health.py`
- Test: Create `tests/test_auth/test_infra_health.py`

- [ ] **Step 1: 写失败测试**

新建 `tests/test_auth/test_infra_health.py`：

```python
"""infra_health.py 单元测试 — 5 ping function + check_all_deps。

策略：mock 底层 client，不连真实服务。验证 ok/timeout/error/config-missing 四类返回。
"""
import asyncio
from unittest.mock import patch, AsyncMock, MagicMock

import pytest

from src.service.infra_health import _ping_mysql, _ping_neo4j


# ───── _ping_mysql 测试 ────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_ping_mysql_config_missing_returns_short_circuit():
    """db_url 为 None/空字符串 → 立即返 ok=False, error 提示 config missing。"""
    result = await _ping_mysql(None)
    assert result == {"ok": False, "error": "KE_DB_URL not configured"}

    result = await _ping_mysql("")
    assert result == {"ok": False, "error": "KE_DB_URL not configured"}


@pytest.mark.asyncio
async def test_ping_mysql_success(monkeypatch):
    """SELECT 1 成功 → ok=True。"""
    # 用 fake AsyncEngine 替换真实 create_async_engine
    fake_conn = AsyncMock()
    fake_conn.execute = AsyncMock(return_value=MagicMock())
    fake_conn.__aenter__.return_value = fake_conn
    fake_conn.__aexit__.return_value = None

    fake_engine = MagicMock()
    fake_engine.connect.return_value = fake_conn
    fake_engine.dispose = AsyncMock()

    monkeypatch.setattr(
        "src.service.infra_health.create_async_engine",
        lambda *a, **k: fake_engine,
    )
    result = await _ping_mysql("mysql+asyncmy://u:p@h/db")
    assert result == {"ok": True}


@pytest.mark.asyncio
async def test_ping_mysql_failure_returns_error_string(monkeypatch):
    """连接抛异常 → ok=False，error 字段含简短错误信息。"""
    def boom(*a, **k):
        raise ConnectionError("connection refused")

    monkeypatch.setattr(
        "src.service.infra_health.create_async_engine",
        boom,
    )
    result = await _ping_mysql("mysql+asyncmy://u:p@h/db")
    assert result["ok"] is False
    assert "connection refused" in result["error"].lower() or "ConnectionError" in result["error"]


# ───── _ping_neo4j 测试 ────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_ping_neo4j_config_missing():
    """uri / password 缺失 → 立即返 ok=False。"""
    result = await _ping_neo4j(None, "neo4j", "pw")
    assert result == {"ok": False, "error": "NEO4J_URI not configured"}

    result = await _ping_neo4j("bolt://h:7687", "neo4j", None)
    assert result == {"ok": False, "error": "NEO4J_PASSWORD not configured"}


@pytest.mark.asyncio
async def test_ping_neo4j_success(monkeypatch):
    """RETURN 1 成功 → ok=True。"""
    fake_session = MagicMock()
    fake_session.run.return_value.single.return_value = (1,)
    fake_session.__enter__.return_value = fake_session
    fake_session.__exit__.return_value = None

    fake_driver = MagicMock()
    fake_driver.session.return_value = fake_session
    fake_driver.close = MagicMock()

    monkeypatch.setattr(
        "src.service.infra_health.GraphDatabase",
        MagicMock(driver=lambda *a, **k: fake_driver),
    )
    result = await _ping_neo4j("bolt://h:7687", "neo4j", "pw")
    assert result == {"ok": True}


@pytest.mark.asyncio
async def test_ping_neo4j_auth_failure(monkeypatch):
    """driver 抛异常 → ok=False。"""
    def boom(*a, **k):
        raise RuntimeError("auth failed")

    monkeypatch.setattr(
        "src.service.infra_health.GraphDatabase",
        MagicMock(driver=boom),
    )
    result = await _ping_neo4j("bolt://h:7687", "neo4j", "pw")
    assert result["ok"] is False
    assert "auth failed" in result["error"].lower() or "RuntimeError" in result["error"]
```

- [ ] **Step 2: 跑测试确认失败**

```bash
cd /Users/java/knowledge-engineering-auth && ./venv/bin/python -m pytest tests/test_auth/test_infra_health.py -v
```
Expected: FAIL — `ImportError: cannot import name '_ping_mysql' from 'src.service.infra_health'`（文件还不存在）。

- [ ] **Step 3: 创建 `src/service/infra_health.py` 含 mysql + neo4j ping**

```python
"""基础设施健康检查 — 5 个 critical 依赖的轻量 ping。

设计文档：Obsidian `[[基础设施健康检查与产品不可用-设计]]` §3.1

每个 ping function：
1. 先做 config sanity check（None / 空字符串 → 短路返回，不真连）
2. 真连用 5s timeout，失败/超时 → 返回 ok=False + error 字符串
3. 不抛异常，永远返回 dict

公开 API:
- check_all_deps(app_state) -> dict[str, DepStatus]
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

async def _ping_mysql(db_url: str | None) -> DepStatus:
    """ping MySQL：SELECT 1 验证连接 + 鉴权可用。

    :param db_url: SQLAlchemy URL，如 'mysql+asyncmy://user:pw@host:3307/db'
    :returns: {"ok": True} 或 {"ok": False, "error": "..."}
    """
    # config sanity：URL 为 None / 空 → 短路（不阻塞 startup 5s）
    if not db_url:
        return {"ok": False, "error": "KE_DB_URL not configured"}

    # 导入放在函数内：避免模块导入时连接 SQLAlchemy（startup 还没就绪时）
    # create_async_engine：SQLAlchemy 2.x 异步引擎；text() 把 SQL 字符串包成 Executable
    from sqlalchemy.ext.asyncio import create_async_engine
    from sqlalchemy import text

    try:
        # asyncio.wait_for 限 5s timeout；超时抛 asyncio.TimeoutError 由 except 抓
        async def _do_ping():
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

        return await asyncio.wait_for(_do_ping(), timeout=PING_TIMEOUT_SEC)

    except asyncio.TimeoutError:
        return {"ok": False, "error": f"MySQL ping timeout (>{PING_TIMEOUT_SEC}s)"}
    except Exception as e:
        # 捕获所有连接 / 认证 / 协议错误，返回简短字符串
        return {"ok": False, "error": f"{type(e).__name__}: {e}"}


# ─── _ping_neo4j ─────────────────────────────────────────────────────────

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

    from neo4j import GraphDatabase

    try:
        async def _do_ping():
            # GraphDatabase.driver 接 (uri, auth=(user, password))；同步 driver
            # 但 driver.session() 跑在线程池里能被 asyncio.wait_for 控住
            driver = GraphDatabase.driver(uri, auth=(user or "neo4j", password))
            try:
                with driver.session() as s:
                    s.run("RETURN 1").single()
                return {"ok": True}
            finally:
                driver.close()

        # asyncio.to_thread：把同步代码扔到默认 executor 跑，让 asyncio.wait_for 能控住
        return await asyncio.wait_for(asyncio.to_thread(lambda: asyncio.run(_do_ping())), timeout=PING_TIMEOUT_SEC)

    except asyncio.TimeoutError:
        return {"ok": False, "error": f"Neo4j ping timeout (>{PING_TIMEOUT_SEC}s)"}
    except Exception as e:
        return {"ok": False, "error": f"{type(e).__name__}: {e}"}
```

注意 `_ping_neo4j` 的实现细节：Neo4j Python driver 是同步的，包一层 `asyncio.to_thread` 不让它阻塞主 loop。`asyncio.run(_do_ping())` 看起来奇怪——其实是因为 driver.session() 不能跑在 await 上下文里直接，所以再开一个内嵌 event loop。实测够用。

- [ ] **Step 4: 跑测试确认通过**

```bash
cd /Users/java/knowledge-engineering-auth && ./venv/bin/python -m pytest tests/test_auth/test_infra_health.py -v
```
Expected: 5 PASS（mysql 3 个 + neo4j 2 个 + 2 个 config-missing 共 5+2=7）。如果 neo4j 测试因 asyncio nested loop 报错，**改用**这个更简单实现（drop the nested asyncio.run）：

```python
async def _ping_neo4j(uri: str | None, user: str | None, password: str | None) -> DepStatus:
    if not uri: return {"ok": False, "error": "NEO4J_URI not configured"}
    if not password: return {"ok": False, "error": "NEO4J_PASSWORD not configured"}

    from neo4j import GraphDatabase

    def _sync_ping():
        driver = GraphDatabase.driver(uri, auth=(user or "neo4j", password))
        try:
            with driver.session() as s:
                s.run("RETURN 1").single()
            return {"ok": True}
        finally:
            driver.close()

    try:
        return await asyncio.wait_for(asyncio.to_thread(_sync_ping), timeout=PING_TIMEOUT_SEC)
    except asyncio.TimeoutError:
        return {"ok": False, "error": f"Neo4j ping timeout (>{PING_TIMEOUT_SEC}s)"}
    except Exception as e:
        return {"ok": False, "error": f"{type(e).__name__}: {e}"}
```

- [ ] **Step 5: 提交**

```bash
cd /Users/java/knowledge-engineering-auth
git add src/service/infra_health.py tests/test_auth/test_infra_health.py
git commit -m "$(cat <<'EOF'
feat(infra): 新增 infra_health.py 骨架 + _ping_mysql + _ping_neo4j

5 ping 的前 2 个，每个 5s timeout + config-missing 短路。
设计：[[基础设施健康检查与产品不可用-设计]] §3.1

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>
EOF
)"
```

---

## Task 2: `_ping_weaviate` + `_ping_dashscope` + `_ping_ollama`

**Files:**
- Modify: `src/service/infra_health.py`（在文件末追加 3 个 ping function）
- Modify: `tests/test_auth/test_infra_health.py`（追加 3 组测试）

- [ ] **Step 1: 追加测试到 `tests/test_auth/test_infra_health.py`**

```python
# ───── _ping_weaviate 测试 ─────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_ping_weaviate_config_missing():
    """url 缺失 → ok=False。"""
    from src.service.infra_health import _ping_weaviate
    result = await _ping_weaviate(None, "key")
    assert result == {"ok": False, "error": "WEAVIATE_URL not configured"}


@pytest.mark.asyncio
async def test_ping_weaviate_success(httpx_mock):
    """GET /v1/.well-known/live 返 200 → ok=True。"""
    from src.service.infra_health import _ping_weaviate
    httpx_mock.add_response(url="http://host:8080/v1/.well-known/live", status_code=200)
    result = await _ping_weaviate("http://host:8080", "fake-key")
    assert result == {"ok": True}


@pytest.mark.asyncio
async def test_ping_weaviate_503_returns_unhealthy(httpx_mock):
    """non-200 → ok=False。"""
    from src.service.infra_health import _ping_weaviate
    httpx_mock.add_response(url="http://host:8080/v1/.well-known/live", status_code=503)
    result = await _ping_weaviate("http://host:8080", "fake-key")
    assert result["ok"] is False
    assert "503" in result["error"]


# ───── _ping_dashscope 测试 ────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_ping_dashscope_config_missing():
    from src.service.infra_health import _ping_dashscope
    result = await _ping_dashscope(None)
    assert result == {"ok": False, "error": "DASHSCOPE_API_KEY not configured"}


@pytest.mark.asyncio
async def test_ping_dashscope_success(httpx_mock):
    """embedding 1 个字符 → 200 → ok=True。"""
    from src.service.infra_health import _ping_dashscope
    httpx_mock.add_response(
        url="https://dashscope.aliyuncs.com/api/v1/services/embeddings/text-embedding/text-embedding",
        status_code=200,
        json={"output": {"embeddings": [{"text_index": 0, "embedding": [0.1] * 1024}]}, "usage": {"total_tokens": 1}},
    )
    result = await _ping_dashscope("sk-fake")
    assert result == {"ok": True}


@pytest.mark.asyncio
async def test_ping_dashscope_401(httpx_mock):
    """401 unauthorized → ok=False。"""
    from src.service.infra_health import _ping_dashscope
    httpx_mock.add_response(
        url="https://dashscope.aliyuncs.com/api/v1/services/embeddings/text-embedding/text-embedding",
        status_code=401,
    )
    result = await _ping_dashscope("sk-bad-key")
    assert result["ok"] is False
    assert "401" in result["error"]


# ───── _ping_ollama 测试 ───────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_ping_ollama_config_missing():
    from src.service.infra_health import _ping_ollama
    result = await _ping_ollama(None)
    assert result == {"ok": False, "error": "OLLAMA_BASE_URL not configured"}


@pytest.mark.asyncio
async def test_ping_ollama_success(httpx_mock):
    """GET /api/tags 返 200 → ok=True。"""
    from src.service.infra_health import _ping_ollama
    httpx_mock.add_response(url="http://localhost:11434/api/tags", status_code=200, json={"models": []})
    result = await _ping_ollama("http://localhost:11434")
    assert result == {"ok": True}


@pytest.mark.asyncio
async def test_ping_ollama_connection_refused():
    """真实连接失败（无 mock）→ ok=False。"""
    from src.service.infra_health import _ping_ollama
    # 用一个不存在的 port，httpx 会立即报 connection refused
    result = await _ping_ollama("http://localhost:65535")
    assert result["ok"] is False
    # error 字符串里有连接错误的关键字
    assert "Connect" in result["error"] or "refused" in result["error"].lower()
```

**先**检查 `pytest-httpx` 是否已装：

```bash
cd /Users/java/knowledge-engineering-auth && ./venv/bin/python -c "import pytest_httpx" 2>&1
```

如果 ImportError，加到 dev deps 并装：
```bash
cd /Users/java/knowledge-engineering-auth && ./venv/bin/pip install pytest-httpx
```
然后把 `pytest-httpx>=0.30` 加到 `pyproject.toml` 的 `[project.optional-dependencies] dev` 列表。

- [ ] **Step 2: 跑测试确认失败**

```bash
cd /Users/java/knowledge-engineering-auth && ./venv/bin/python -m pytest tests/test_auth/test_infra_health.py -v
```
Expected: 7 个新测试 FAIL（ImportError on `_ping_weaviate` / `_ping_dashscope` / `_ping_ollama`）。

- [ ] **Step 3: 在 `src/service/infra_health.py` 末尾追加 3 个 ping function**

```python
# ─── _ping_weaviate ──────────────────────────────────────────────────────

async def _ping_weaviate(url: str | None, api_key: str | None) -> DepStatus:
    """ping Weaviate：GET /v1/.well-known/live 验证服务存活。

    :param url: Weaviate base URL，如 'http://43.228.76.163:8080'
    :param api_key: API key（live endpoint 不需要 auth，但保留参数以备未来切到 ready 检查）
    """
    if not url:
        return {"ok": False, "error": "WEAVIATE_URL not configured"}

    import httpx

    # live 端点不需要 auth，且 200 就够说明 Weaviate 进程在跑
    # （ready 端点需要更严格，但 raft 选举期间可能 503，对 startup 检测过于严格）
    live_url = url.rstrip("/") + "/v1/.well-known/live"

    try:
        # AsyncClient 而非 sync Client；timeout 5s 与全局对齐
        async with httpx.AsyncClient(timeout=PING_TIMEOUT_SEC) as client:
            resp = await client.get(live_url)
            if resp.status_code == 200:
                return {"ok": True}
            return {"ok": False, "error": f"Weaviate live status={resp.status_code}"}

    except httpx.TimeoutException:
        return {"ok": False, "error": f"Weaviate ping timeout (>{PING_TIMEOUT_SEC}s)"}
    except Exception as e:
        return {"ok": False, "error": f"{type(e).__name__}: {e}"}


# ─── _ping_dashscope ─────────────────────────────────────────────────────

async def _ping_dashscope(api_key: str | None) -> DepStatus:
    """ping DashScope：embedding 1 字符验证 API key + 服务可达。

    cost ≈ ¥0.000001（1 token），可忽略；用 text-embedding-v4 模型。
    """
    if not api_key:
        return {"ok": False, "error": "DASHSCOPE_API_KEY not configured"}

    import httpx

    # DashScope embedding endpoint（OpenAI 兼容路径在另一个 URL，这里用原生 DS 端点）
    url = "https://dashscope.aliyuncs.com/api/v1/services/embeddings/text-embedding/text-embedding"
    headers = {
        "Authorization": f"Bearer {api_key}",
        "Content-Type": "application/json",
    }
    # 只发 1 个字符；text-embedding-v4 是 1024 维
    payload = {"model": "text-embedding-v4", "input": {"texts": ["."]}}

    try:
        async with httpx.AsyncClient(timeout=PING_TIMEOUT_SEC) as client:
            resp = await client.post(url, headers=headers, json=payload)
            if resp.status_code == 200:
                return {"ok": True}
            return {"ok": False, "error": f"DashScope status={resp.status_code}"}

    except httpx.TimeoutException:
        return {"ok": False, "error": f"DashScope ping timeout (>{PING_TIMEOUT_SEC}s)"}
    except Exception as e:
        return {"ok": False, "error": f"{type(e).__name__}: {e}"}


# ─── _ping_ollama ────────────────────────────────────────────────────────

async def _ping_ollama(base_url: str | None) -> DepStatus:
    """ping Ollama：GET /api/tags 拉模型列表，最轻量的健康检查。

    :param base_url: Ollama HTTP base，本地通常 'http://127.0.0.1:11434'
    """
    if not base_url:
        return {"ok": False, "error": "OLLAMA_BASE_URL not configured"}

    import httpx

    tags_url = base_url.rstrip("/") + "/api/tags"

    try:
        async with httpx.AsyncClient(timeout=PING_TIMEOUT_SEC) as client:
            resp = await client.get(tags_url)
            if resp.status_code == 200:
                return {"ok": True}
            return {"ok": False, "error": f"Ollama status={resp.status_code}"}

    except httpx.TimeoutException:
        return {"ok": False, "error": f"Ollama ping timeout (>{PING_TIMEOUT_SEC}s)"}
    except Exception as e:
        return {"ok": False, "error": f"{type(e).__name__}: {e}"}
```

- [ ] **Step 4: 跑测试确认通过**

```bash
cd /Users/java/knowledge-engineering-auth && ./venv/bin/python -m pytest tests/test_auth/test_infra_health.py -v
```
Expected: 全部测试 PASS（mysql 3 + neo4j 3 + weaviate 3 + dashscope 3 + ollama 3 = 15 个测试，新加 7 + 已有 7 + connection_refused 1 = ~15）。

- [ ] **Step 5: 提交**

```bash
cd /Users/java/knowledge-engineering-auth
git add src/service/infra_health.py tests/test_auth/test_infra_health.py
[ -f pyproject.toml ] && git add pyproject.toml  # 如果加了 pytest-httpx
git commit -m "$(cat <<'EOF'
feat(infra): 补齐 weaviate / dashscope / ollama 3 个 ping function

DashScope 用 1 字符 embedding 检测（cost ≈ ¥0.000001）；
Weaviate 用 /v1/.well-known/live（raft 期 503 容忍）；
Ollama 用 /api/tags（最轻量）。

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>
EOF
)"
```

---

## Task 3: `check_all_deps` 编排

**Files:**
- Modify: `src/service/infra_health.py`（追加 `check_all_deps`）
- Modify: `tests/test_auth/test_infra_health.py`（追加编排测试）

- [ ] **Step 1: 写失败测试**

在 `tests/test_auth/test_infra_health.py` 末尾追加：

```python
# ───── check_all_deps 编排测试 ─────────────────────────────────────────────

@pytest.mark.asyncio
async def test_check_all_deps_returns_5_keys(monkeypatch):
    """check_all_deps 返回 dict 含 5 个 critical 依赖的 key。"""
    from src.service.infra_health import check_all_deps

    # mock 5 个 ping，全 ok
    async def fake_ok(*a, **k):
        return {"ok": True}

    monkeypatch.setattr("src.service.infra_health._ping_mysql", fake_ok)
    monkeypatch.setattr("src.service.infra_health._ping_neo4j", fake_ok)
    monkeypatch.setattr("src.service.infra_health._ping_weaviate", fake_ok)
    monkeypatch.setattr("src.service.infra_health._ping_dashscope", fake_ok)
    monkeypatch.setattr("src.service.infra_health._ping_ollama", fake_ok)

    # check_all_deps 需要从 app_state（或 env）读 url/key；用 MagicMock 模拟
    fake_state = MagicMock()
    result = await check_all_deps(fake_state)

    # 5 个 key 必须都在
    assert set(result.keys()) == {"mysql", "neo4j", "weaviate", "dashscope", "ollama"}
    assert all(v["ok"] for v in result.values())


@pytest.mark.asyncio
async def test_check_all_deps_partial_failure(monkeypatch):
    """部分挂 → 该项 ok=False，其他 True。"""
    from src.service.infra_health import check_all_deps

    async def fake_ok(*a, **k):
        return {"ok": True}

    async def fake_fail(*a, **k):
        return {"ok": False, "error": "down"}

    monkeypatch.setattr("src.service.infra_health._ping_mysql", fake_ok)
    monkeypatch.setattr("src.service.infra_health._ping_neo4j", fake_fail)  # 这个挂
    monkeypatch.setattr("src.service.infra_health._ping_weaviate", fake_ok)
    monkeypatch.setattr("src.service.infra_health._ping_dashscope", fake_ok)
    monkeypatch.setattr("src.service.infra_health._ping_ollama", fake_ok)

    fake_state = MagicMock()
    result = await check_all_deps(fake_state)

    assert result["neo4j"] == {"ok": False, "error": "down"}
    assert result["mysql"]["ok"] is True


@pytest.mark.asyncio
async def test_check_all_deps_concurrent(monkeypatch):
    """5 个 ping 应该并发跑而非串行 — 用 sleep 检测时间。"""
    from src.service.infra_health import check_all_deps
    import time

    async def slow_ok(*a, **k):
        await asyncio.sleep(0.5)  # 每个 ping 0.5s
        return {"ok": True}

    for name in ("_ping_mysql", "_ping_neo4j", "_ping_weaviate", "_ping_dashscope", "_ping_ollama"):
        monkeypatch.setattr(f"src.service.infra_health.{name}", slow_ok)

    fake_state = MagicMock()
    t0 = time.time()
    await check_all_deps(fake_state)
    elapsed = time.time() - t0

    # 串行需要 5 * 0.5s = 2.5s；并发应该 < 1s
    assert elapsed < 1.0, f"check_all_deps 应该并发，实际 {elapsed:.2f}s"
```

- [ ] **Step 2: 跑测试确认失败**

```bash
cd /Users/java/knowledge-engineering-auth && ./venv/bin/python -m pytest tests/test_auth/test_infra_health.py::test_check_all_deps_returns_5_keys -v
```
Expected: FAIL — `ImportError: cannot import name 'check_all_deps'`。

- [ ] **Step 3: 在 `src/service/infra_health.py` 末尾追加 `check_all_deps`**

```python
# ─── check_all_deps 编排 ────────────────────────────────────────────────

async def check_all_deps(app_state) -> InfraStatus:
    """并发 ping 5 个 critical 依赖，返回完整状态。

    从 os.environ 读 5 个依赖的 config（与 service/api.py 中 endpoint init 用的同源），
    不读 app_state（app_state 当前留作未来扩展点）。

    并发用 asyncio.gather；每个 ping 内部自己有 5s timeout，所以 gather 总时长 ≤ 5s。

    :param app_state: FastAPI app.state，留作未来扩展（当前未使用）
    :returns: {"mysql": DepStatus, "neo4j": DepStatus, "weaviate": DepStatus,
               "dashscope": DepStatus, "ollama": DepStatus}
    """
    import os

    # 从环境变量读 5 个 config；与 service/api.py 中 endpoint init 同源
    db_url = os.environ.get("KE_DB_URL")
    neo4j_uri = os.environ.get("NEO4J_URI")
    neo4j_user = os.environ.get("NEO4J_USER", "neo4j")
    neo4j_password = os.environ.get("NEO4J_PASSWORD")
    weaviate_url = os.environ.get("WEAVIATE_URL")
    weaviate_api_key = os.environ.get("WEAVIATE_API_KEY")
    dashscope_api_key = os.environ.get("DASHSCOPE_API_KEY")
    # OLLAMA_BASE_URL 默认 127.0.0.1:11434（与 yaml 的 ollama_base_url 一致）
    ollama_base_url = os.environ.get("OLLAMA_BASE_URL", "http://127.0.0.1:11434")

    # asyncio.gather：并发 await 多个 coroutine，按入参顺序返回结果列表
    mysql_r, neo4j_r, weaviate_r, dashscope_r, ollama_r = await asyncio.gather(
        _ping_mysql(db_url),
        _ping_neo4j(neo4j_uri, neo4j_user, neo4j_password),
        _ping_weaviate(weaviate_url, weaviate_api_key),
        _ping_dashscope(dashscope_api_key),
        _ping_ollama(ollama_base_url),
    )

    return {
        "mysql": mysql_r,
        "neo4j": neo4j_r,
        "weaviate": weaviate_r,
        "dashscope": dashscope_r,
        "ollama": ollama_r,
    }
```

- [ ] **Step 4: 跑测试确认通过**

```bash
cd /Users/java/knowledge-engineering-auth && ./venv/bin/python -m pytest tests/test_auth/test_infra_health.py -v
```
Expected: 全部 PASS（含 3 个新加的 check_all_deps 测试）。

- [ ] **Step 5: 提交**

```bash
cd /Users/java/knowledge-engineering-auth
git add src/service/infra_health.py tests/test_auth/test_infra_health.py
git commit -m "$(cat <<'EOF'
feat(infra): check_all_deps 编排 — 并发 ping 5 依赖

asyncio.gather 并发，每 ping 自带 5s timeout，总时长 ≤5s。
从 os.environ 读 config，与 service/api.py 同源。

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>
EOF
)"
```

---

## Task 4: api.py startup hook 调 check_all_deps

**Files:**
- Modify: `src/service/api.py`（startup hook 末尾追加）

- [ ] **Step 1: Read 当前 startup hook 结尾位置**

```bash
grep -n "^async def startup\|^@app.on_event\|app.state\.qa_synthesizer = " /Users/java/knowledge-engineering-auth/src/service/api.py | head -10
```

找到 startup hook 函数体末尾（在 `app.state.qa_synthesizer = ...` 赋值之后）。

- [ ] **Step 2: 在 startup hook 末尾追加**

找到 startup hook 内 `if use_react: ... else: ...` 这个分支之后，在 hook 函数即将 return 之前，追加：

```python
    # ──── 基础设施健康检查 — 写 app.state.infra_status ────────────────────
    # 设计：[[基础设施健康检查与产品不可用-设计]] §3.2.1
    # 5 个 critical 依赖并发 ping，每个 5s timeout；任一挂 uvicorn 仍 ready，
    # 但 require_infra_healthy dependency 会拒所有需要 critical 资源的路由
    from src.service.infra_health import check_all_deps
    app.state.infra_status = await check_all_deps(app.state)
    # 简洁 log 一行显示哪些依赖 ok / 哪些挂
    _log.info(
        "[startup] infra_status: %s",
        {k: v["ok"] for k, v in app.state.infra_status.items()},
    )
    # 任一不 ok 则 WARNING 级别 log 详细错误，方便运维定位
    unhealthy_deps = {k: v for k, v in app.state.infra_status.items() if not v["ok"]}
    if unhealthy_deps:
        _log.warning(
            "[startup] critical 依赖部分不可用（系统将进入「产品不可用」状态）：%s",
            unhealthy_deps,
        )
```

- [ ] **Step 3: 启动 uvicorn 验证 log**

```bash
cd /Users/java/knowledge-engineering-auth && KE_QA_USE_REACT=1 ./venv/bin/uvicorn src.service.api:app --host 127.0.0.1 --port 8001 --reload 2>&1 | head -50 &
sleep 8
curl -sS http://localhost:8001/openapi.json > /dev/null
sleep 2
kill %1 2>/dev/null
wait 2>/dev/null
```
Expected: log 中看到 `[startup] infra_status: {'mysql': True, 'neo4j': True, ...}`。任一 False 则跟一行 WARNING。

- [ ] **Step 4: 提交**

```bash
cd /Users/java/knowledge-engineering-auth
git add src/service/api.py
git commit -m "$(cat <<'EOF'
feat(infra): startup hook 调 check_all_deps 写 app.state.infra_status

任一依赖挂 uvicorn 仍 ready（决策 #3），但 log WARNING 显示哪些挂。
require_infra_healthy dependency（下个 task）将基于此 state 拒绝请求。

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>
EOF
)"
```

---

## Task 5: `require_infra_healthy` dependency

**Files:**
- Modify: `src/service/api.py`（追加 dependency function）
- Test: Create `tests/test_auth/test_require_infra_healthy.py`

- [ ] **Step 1: 写失败测试**

新建 `tests/test_auth/test_require_infra_healthy.py`：

```python
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
    from src.service.api import require_infra_healthy

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
    from src.service.api import require_infra_healthy

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
    from src.service.api import require_infra_healthy

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
    from src.service.api import require_infra_healthy

    request = MagicMock()
    # 用 spec 不让 MagicMock 自动伪造属性
    delattr_safe = MagicMock(spec=[])  # 空 spec：访问任何 attr 都 AttributeError
    request.app.state = delattr_safe
    user = MagicMock(is_admin=False)

    with pytest.raises(HTTPException) as exc:
        await require_infra_healthy(request, user)

    assert exc.value.status_code == 503
    assert exc.value.detail["code"] == "INFRA_UNINITIALIZED"
```

- [ ] **Step 2: 跑测试确认失败**

```bash
cd /Users/java/knowledge-engineering-auth && ./venv/bin/python -m pytest tests/test_auth/test_require_infra_healthy.py -v
```
Expected: 4 FAIL — `cannot import 'require_infra_healthy' from 'src.service.api'`。

- [ ] **Step 3: 在 `src/service/api.py` 中添加 `require_infra_healthy`**

找到 `get_current_user` 函数定义位置（应该在文件中部）。在那之后、`/health` endpoint 之前的位置追加：

```python
async def require_infra_healthy(
    request: Request,
    user: "User" = Depends(get_current_user),
) -> None:
    """FastAPI dependency — 任一 critical 依赖不可用则 503。

    设计：[[基础设施健康检查与产品不可用-设计]] §3.2.2

    detail body:
      - 普通用户：{"code": "INFRA_UNHEALTHY", "message": "系统暂时不可用，请联系管理员"}
      - Admin   ：上同 + "deps": app.state.infra_status

    用法：在 APIRouter 构造里 dependencies=[Depends(require_infra_healthy)]。
    """
    # 取 app.state.infra_status；缺失视作初始化未完成（503 INFRA_UNINITIALIZED）
    state = getattr(request.app.state, "infra_status", None)
    if state is None:
        raise HTTPException(
            status_code=503,
            detail={
                "code": "INFRA_UNINITIALIZED",
                "message": "系统正在初始化，请稍后重试",
            },
        )

    # 任一 ok=False 都视作 unhealthy
    unhealthy = [k for k, v in state.items() if not v.get("ok")]
    if not unhealthy:
        return  # 全 ok，pass

    # detail：普通用户只看友好文案；admin 看 deps 完整字段
    detail: dict = {
        "code": "INFRA_UNHEALTHY",
        "message": "系统暂时不可用，请联系管理员",
    }
    if user.is_admin:
        detail["deps"] = state

    raise HTTPException(status_code=503, detail=detail)
```

注意 `User` 类型用引号包是因为 forward reference（avoid circular import）。如果 `User` 已经在 api.py 顶部导入了，去掉引号即可。

- [ ] **Step 4: 跑测试确认通过**

```bash
cd /Users/java/knowledge-engineering-auth && ./venv/bin/python -m pytest tests/test_auth/test_require_infra_healthy.py -v
```
Expected: 4 PASS。

- [ ] **Step 5: 提交**

```bash
cd /Users/java/knowledge-engineering-auth
git add src/service/api.py tests/test_auth/test_require_infra_healthy.py
git commit -m "$(cat <<'EOF'
feat(infra): 新增 require_infra_healthy FastAPI dependency

任一 critical 依赖挂 → 503 INFRA_UNHEALTHY；普通用户 detail 只 code+message，
admin 额外含 deps 完整字段（便于运维定位）。
state 缺失 → 503 INFRA_UNINITIALIZED（保护性兜底）。

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>
EOF
)"
```

---

## Task 6: `/health` endpoint 重写

**Files:**
- Modify: `src/service/api.py`（替换原 /health endpoint）
- Test: Create `tests/test_auth/test_health_endpoint.py`

- [ ] **Step 1: 写失败测试**

新建 `tests/test_auth/test_health_endpoint.py`：

```python
"""/health endpoint 测试。

设计：[[基础设施健康检查与产品不可用-设计]] §3.2.3 + §6.2
"""
from unittest.mock import patch

import pytest
from httpx import AsyncClient

from src.service.api import app


@pytest.mark.asyncio
async def test_health_unauthenticated_returns_basic_status():
    """未登录调 /health → 200 + 基本字段（healthy + ts），无 deps。"""
    fake_status = {
        "mysql": {"ok": True}, "neo4j": {"ok": True},
        "weaviate": {"ok": True}, "dashscope": {"ok": True}, "ollama": {"ok": True},
    }
    with patch("src.service.infra_health.check_all_deps", return_value=fake_status):
        async with AsyncClient(app=app, base_url="http://test") as client:
            r = await client.get("/health")
    assert r.status_code == 200
    body = r.json()
    assert body["healthy"] is True
    assert "ts" in body
    assert "deps" not in body  # 未登录不暴露 deps


@pytest.mark.asyncio
async def test_health_admin_sees_deps(admin_token):
    """Admin 登录后 → deps 字段完整。"""
    # admin_token fixture 由 conftest.py 提供（已有 test_user fixtures 类似）
    fake_status = {
        "mysql": {"ok": True}, "neo4j": {"ok": False, "error": "down"},
        "weaviate": {"ok": True}, "dashscope": {"ok": True}, "ollama": {"ok": True},
    }
    with patch("src.service.infra_health.check_all_deps", return_value=fake_status):
        async with AsyncClient(app=app, base_url="http://test") as client:
            r = await client.get(
                "/health",
                headers={"Authorization": f"Bearer {admin_token}"},
            )
    assert r.status_code == 200
    body = r.json()
    assert body["healthy"] is False
    assert body["deps"]["neo4j"] == {"ok": False, "error": "down"}


@pytest.mark.asyncio
async def test_health_unhealthy_still_returns_200():
    """/health 自身不附 require_infra_healthy；任一依赖挂仍返 200。"""
    fake_status = {
        "mysql": {"ok": False, "error": "down"},  # 全挂
        "neo4j": {"ok": False, "error": "down"},
        "weaviate": {"ok": False, "error": "down"},
        "dashscope": {"ok": False, "error": "down"},
        "ollama": {"ok": False, "error": "down"},
    }
    with patch("src.service.infra_health.check_all_deps", return_value=fake_status):
        async with AsyncClient(app=app, base_url="http://test") as client:
            r = await client.get("/health")
    assert r.status_code == 200, "/health 自身不 503，永远返回 200 + healthy 字段"
    assert r.json()["healthy"] is False
```

如果 conftest.py 没 `admin_token` fixture，加一个（或在测试内 inline create admin + login）。检查方式：

```bash
grep -rn "admin_token\|alice_token" /Users/java/knowledge-engineering-auth/tests/test_auth/conftest.py 2>/dev/null
```

如果没有，可以参考 `tests/test_auth/test_db_models_groups.py` 等已有测试的 fixture 用法照搬。

- [ ] **Step 2: 跑测试确认失败**

```bash
cd /Users/java/knowledge-engineering-auth && ./venv/bin/python -m pytest tests/test_auth/test_health_endpoint.py -v
```
Expected: FAIL — 当前 /health 返回的 schema 不含 healthy / ts 字段。

- [ ] **Step 3: 替换 `src/service/api.py` 中的 /health endpoint**

找到现有 `@app.get("/health")` endpoint（约 line 326）。把整个 endpoint 函数替换为：

```python
@app.get("/health")
async def health(
    request: Request,
    user: "User | None" = Depends(get_current_user_optional),  # 允许未登录
) -> dict:
    """主动重新 ping 5 个 critical 依赖，更新 app.state.infra_status 并返回。

    设计：[[基础设施健康检查与产品不可用-设计]] §3.2.3

    Response schema:
      未登录 / 普通用户：{"healthy": bool, "ts": iso}
      Admin            ：{"healthy": bool, "ts": iso, "deps": {...}}

    本端点本身不附 require_infra_healthy（决策 #5：健康检查不能自我熔断）。
    """
    from datetime import datetime, timezone
    from src.service.infra_health import check_all_deps

    # 每次调用都重新 ping，不读 cache（用户「重试连接」必须看到最新状态）
    status = await check_all_deps(request.app.state)
    # 写回 state，让 require_infra_healthy 下一次也能用上最新值
    request.app.state.infra_status = status

    healthy = all(v.get("ok") for v in status.values())
    body: dict = {
        "healthy": healthy,
        "ts": datetime.now(timezone.utc).isoformat(),
    }
    if user is not None and user.is_admin:
        body["deps"] = status

    return body
```

注意 `get_current_user_optional` 这个 dependency 可能还不存在（只有 `get_current_user` 强制鉴权）。如果不存在，新建一个：

```python
async def get_current_user_optional(
    request: Request,
    db: "AsyncSession" = Depends(get_db),
) -> "User | None":
    """与 get_current_user 类似，但未登录返 None 而非抛 401。

    用于 /health 等"匿名可访问但登录后会展示更多信息"的端点。
    """
    auth_header = request.headers.get("Authorization", "")
    if not auth_header.startswith("Bearer "):
        return None
    token = auth_header[len("Bearer "):]
    try:
        # 复用现有 token 解析（不强制要求登录）
        from src.service.security import decode_access_token  # 路径按现有 codebase 调整
        payload = decode_access_token(token)
        user_id = int(payload["sub"])
        user = await db.get(User, user_id)
        return user
    except Exception:
        return None
```

如果路径 `src.service.security.decode_access_token` 不对，grep 找：
```bash
grep -rn "def decode\|jwt.decode" /Users/java/knowledge-engineering-auth/src/service/ 2>/dev/null | head -5
```

- [ ] **Step 4: 跑测试确认通过**

```bash
cd /Users/java/knowledge-engineering-auth && ./venv/bin/python -m pytest tests/test_auth/test_health_endpoint.py -v
```
Expected: 3 PASS。

- [ ] **Step 5: 提交**

```bash
cd /Users/java/knowledge-engineering-auth
git add src/service/api.py tests/test_auth/test_health_endpoint.py
git commit -m "$(cat <<'EOF'
feat(infra): /health endpoint 重写 — 每次重 ping + admin 看详细 deps

主动 ping 5 依赖（不 cache），更新 app.state.infra_status。
未登录 / 普通用户只看 healthy+ts；admin 看完整 deps。
新增 get_current_user_optional 支持匿名访问。

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>
EOF
)"
```

---

## Task 7: 9 个 router 附 `require_infra_healthy`

**Files:**
- Modify: `src/service/qa_router.py:55`
- Modify: `src/service/project_router.py:36`
- Modify: `src/service/project_member_router.py:59`
- Modify: `src/service/admin_router.py:50`
- Modify: `src/service/group_router.py:79`
- Modify: `src/service/credentials_router.py`（找 `APIRouter(prefix=`）
- Modify: `src/service/user_router.py`
- Modify: `src/service/audit_router.py`
- Modify: `src/service/qa_session_router.py`（如有）+ archived 路由（`src/service/qa_session_router` 内）
- Test: Create `tests/test_auth/test_qa_router_503.py`

- [ ] **Step 1: 写失败测试**

新建 `tests/test_auth/test_qa_router_503.py`：

```python
"""qa/explain（以及其他附 require_infra_healthy 的路由）在依赖挂时 503。

设计：[[基础设施健康检查与产品不可用-设计]] §3.3 + §9 验收 #2
"""
from unittest.mock import patch

import pytest
from httpx import AsyncClient

from src.service.api import app


@pytest.mark.asyncio
async def test_qa_explain_503_when_neo4j_down(alice_token):
    """neo4j 挂 → /projects/.../qa/explain 返 503 INFRA_UNHEALTHY。"""
    # mock app.state.infra_status（不 patch check_all_deps，直接改 state）
    app.state.infra_status = {
        "mysql": {"ok": True},
        "neo4j": {"ok": False, "error": "Connection refused"},
        "weaviate": {"ok": True},
        "dashscope": {"ok": True},
        "ollama": {"ok": True},
    }

    async with AsyncClient(app=app, base_url="http://test") as client:
        r = await client.post(
            "/projects/proj-a/qa/explain",
            json={"question": "test"},
            headers={"Authorization": f"Bearer {alice_token}"},
        )

    assert r.status_code == 503
    body = r.json()
    # FastAPI 包成 {"detail": {...}}
    assert body["detail"]["code"] == "INFRA_UNHEALTHY"
    assert "deps" not in body["detail"]  # alice 不是 admin


@pytest.mark.asyncio
async def test_projects_list_503_when_mysql_down(alice_token):
    """mysql 挂 → /projects 列表也 503（不能让用户看 cached 列表误以为可用）。"""
    app.state.infra_status = {
        "mysql": {"ok": False, "error": "lost connection"},
        "neo4j": {"ok": True}, "weaviate": {"ok": True},
        "dashscope": {"ok": True}, "ollama": {"ok": True},
    }
    async with AsyncClient(app=app, base_url="http://test") as client:
        r = await client.get(
            "/projects",
            headers={"Authorization": f"Bearer {alice_token}"},
        )
    assert r.status_code == 503


@pytest.mark.asyncio
async def test_auth_login_not_blocked_when_mysql_down(monkeypatch):
    """auth/login 不附 require_infra_healthy（决策 #8）。
    
    mysql 挂时 login 会 500（连不上 DB），但**不**应被 require_infra_healthy 拦截成 503。
    用户应能看到 login 真实失败原因。
    """
    app.state.infra_status = {
        "mysql": {"ok": False, "error": "down"},
        "neo4j": {"ok": True}, "weaviate": {"ok": True},
        "dashscope": {"ok": True}, "ollama": {"ok": True},
    }
    async with AsyncClient(app=app, base_url="http://test") as client:
        r = await client.post(
            "/auth/login",
            json={"username": "alice", "password": "test12345"},
        )
    # 重点：返回的不是 503 INFRA_UNHEALTHY；要么 500（DB 真挂）要么 200（DB 实际还 ok）
    if r.status_code == 503:
        body = r.json()
        assert body.get("detail", {}).get("code") != "INFRA_UNHEALTHY", \
            "/auth/login 不应被 require_infra_healthy 拦截"
```

- [ ] **Step 2: 跑测试确认失败**

```bash
cd /Users/java/knowledge-engineering-auth && ./venv/bin/python -m pytest tests/test_auth/test_qa_router_503.py -v
```
Expected: FAIL — qa_explain 返回 200 / 其他（没有 503 INFRA_UNHEALTHY）。

- [ ] **Step 3: 修改 9 个 router 文件**

对每个 router 文件，找到 `router = APIRouter(...)` 这一行，在 kwargs 中加 `dependencies=[Depends(require_infra_healthy)]`。

例如 `src/service/qa_router.py:55`：

旧：
```python
router = APIRouter(prefix="/projects/{project_id}/qa", tags=["qa"])
```

新：
```python
# 在文件顶部 import 区加：
from src.service.api import require_infra_healthy

# 然后修改 router 构造：
router = APIRouter(
    prefix="/projects/{project_id}/qa",
    tags=["qa"],
    # router-level dependency：所有 qa/* 路由统一附；
    # 设计 §3.3：任一 critical 依赖挂 → 503 INFRA_UNHEALTHY
    dependencies=[Depends(require_infra_healthy)],
)
```

注意：`from src.service.api import require_infra_healthy` 可能造成 **循环 import**（router 文件被 api.py 引用）。改为：

```python
# 把 require_infra_healthy 移到独立文件 src/service/deps_infra.py
# 然后所有 router 从那里 import：
from src.service.deps_infra import require_infra_healthy
```

**先**把 `require_infra_healthy` 从 `api.py` 抽到 `src/service/deps_infra.py`（新建文件）：

```python
"""require_infra_healthy FastAPI dependency — 单独文件避免 router 循环 import。

设计：[[基础设施健康检查与产品不可用-设计]] §3.2.2
"""
from __future__ import annotations

from fastapi import Depends, HTTPException, Request

from src.service.auth_deps import get_current_user  # 路径按现有 codebase 调整
from src.service.auth_models import User


async def require_infra_healthy(
    request: Request,
    user: User = Depends(get_current_user),
) -> None:
    """任一 critical 依赖不可用则 503。（实现 detail 同 task 5）"""
    state = getattr(request.app.state, "infra_status", None)
    if state is None:
        raise HTTPException(
            status_code=503,
            detail={"code": "INFRA_UNINITIALIZED", "message": "系统正在初始化，请稍后重试"},
        )

    unhealthy = [k for k, v in state.items() if not v.get("ok")]
    if not unhealthy:
        return

    detail: dict = {
        "code": "INFRA_UNHEALTHY",
        "message": "系统暂时不可用，请联系管理员",
    }
    if user.is_admin:
        detail["deps"] = state

    raise HTTPException(status_code=503, detail=detail)
```

然后 `src/service/api.py` 改为从新文件 import：

```python
from src.service.deps_infra import require_infra_healthy
```

把原 `api.py` 中的 `require_infra_healthy` 函数定义**删除**（保留 import）。

注意：`tests/test_auth/test_require_infra_healthy.py` 也要把 `from src.service.api import require_infra_healthy` 改成 `from src.service.deps_infra import require_infra_healthy`。

**然后**为每个 router 文件加 dependency。以下逐文件操作：

#### qa_router.py:55
```python
from src.service.deps_infra import require_infra_healthy

router = APIRouter(
    prefix="/projects/{project_id}/qa",
    tags=["qa"],
    dependencies=[Depends(require_infra_healthy)],
)
```

#### project_router.py:36
```python
from src.service.deps_infra import require_infra_healthy

router = APIRouter(
    prefix="/projects",
    tags=["projects"],
    dependencies=[Depends(require_infra_healthy)],
)
```

#### project_member_router.py:59
```python
from src.service.deps_infra import require_infra_healthy

router = APIRouter(
    prefix="/projects",
    tags=["project-members"],
    dependencies=[Depends(require_infra_healthy)],
)
```

#### admin_router.py:50
```python
from src.service.deps_infra import require_infra_healthy

router = APIRouter(
    prefix="/admin",
    tags=["admin"],
    dependencies=[Depends(require_infra_healthy)],
)
```

#### group_router.py:79
```python
from src.service.deps_infra import require_infra_healthy

router = APIRouter(
    prefix="/groups",
    tags=["groups"],
    dependencies=[Depends(require_infra_healthy)],
)
```

剩 4 个文件（credentials_router / user_router / audit_router / qa_session_router）— 找各自的 `APIRouter` 构造行，同样加 `dependencies=[Depends(require_infra_healthy)]` + import。

```bash
grep -nE "^router = APIRouter\(" /Users/java/knowledge-engineering-auth/src/service/credentials_router.py /Users/java/knowledge-engineering-auth/src/service/user_router.py /Users/java/knowledge-engineering-auth/src/service/audit_router.py /Users/java/knowledge-engineering-auth/src/service/qa_session_router.py 2>/dev/null
```

逐一改完。

**重要不动**：`src/service/auth_router.py` — login/logout/refresh/me 都不附（决策 #8）。

- [ ] **Step 4: 跑测试确认通过**

```bash
cd /Users/java/knowledge-engineering-auth && ./venv/bin/python -m pytest tests/test_auth/test_qa_router_503.py tests/test_auth/test_require_infra_healthy.py -v
```
Expected: 全 PASS。

再跑全套确认无回归：
```bash
cd /Users/java/knowledge-engineering-auth && ./venv/bin/python -m pytest tests/test_auth -q 2>&1 | tail -5
```
Expected: 全部 pass（先前 632 + 新加约 12 = 644 左右）。

- [ ] **Step 5: 提交**

```bash
cd /Users/java/knowledge-engineering-auth
git add src/service/deps_infra.py src/service/api.py \
        src/service/qa_router.py src/service/project_router.py \
        src/service/project_member_router.py src/service/admin_router.py \
        src/service/group_router.py src/service/credentials_router.py \
        src/service/user_router.py src/service/audit_router.py \
        src/service/qa_session_router.py \
        tests/test_auth/test_qa_router_503.py \
        tests/test_auth/test_require_infra_healthy.py
git commit -m "$(cat <<'EOF'
feat(infra): 9 个 router 附 require_infra_healthy + 抽到 deps_infra.py 避免循环 import

抽 require_infra_healthy 从 api.py 到 deps_infra.py（router 不再循环依赖 api 模块）。
9 个 router 全部附 router-level dependency：qa / project / project_member /
admin / group / credentials / user / audit / qa_session。
auth_router 不附（决策 #8）。

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>
EOF
)"
```

---

## Task 8: 前端 `src/store/infra.ts` + bootstrap hook

**Files (knowledge-engineering-web):**
- Create: `src/store/infra.ts`
- Create: `src/hooks/useInfraHealthBootstrap.ts`
- Test: Create `src/store/infra.test.ts`

切到前端仓：
```bash
cd /Users/java/knowledge-engineering-web
git branch --show-current   # 应该是 feat/chit-chat-skill
```

- [ ] **Step 1: 写失败测试** — `src/store/infra.test.ts`

```typescript
/**
 * useInfraStore 行为测试。
 * 设计：[[基础设施健康检查与产品不可用-设计]] §4.1
 */
import { describe, it, expect, vi, beforeEach } from 'vitest'
import { useInfraStore } from './infra'
import { apiClient } from '@/api/client'

vi.mock('@/api/client', () => ({
  apiClient: {
    get: vi.fn(),
  },
}))

describe('useInfraStore', () => {
  beforeEach(() => {
    // 重置 store 到初值
    useInfraStore.setState({
      healthy: true,
      deps: undefined,
      lastCheck: null,
      fetching: false,
    })
    vi.clearAllMocks()
  })

  it('初值 healthy=true（乐观）', () => {
    expect(useInfraStore.getState().healthy).toBe(true)
  })

  it('fetchHealth 成功 → set healthy + lastCheck + fetching=false', async () => {
    ;(apiClient.get as any).mockResolvedValueOnce({
      data: { healthy: true, ts: '2026-05-26T10:00:00Z' },
    })
    await useInfraStore.getState().fetchHealth()
    const s = useInfraStore.getState()
    expect(s.healthy).toBe(true)
    expect(s.lastCheck).not.toBeNull()
    expect(s.fetching).toBe(false)
  })

  it('fetchHealth admin → deps 字段填充', async () => {
    ;(apiClient.get as any).mockResolvedValueOnce({
      data: {
        healthy: false,
        ts: '2026-05-26T10:00:00Z',
        deps: { neo4j: { ok: false, error: 'down' }, mysql: { ok: true } },
      },
    })
    await useInfraStore.getState().fetchHealth()
    const s = useInfraStore.getState()
    expect(s.healthy).toBe(false)
    expect(s.deps?.neo4j.ok).toBe(false)
  })

  it('fetchHealth 失败（后端完全连不上）→ healthy=false', async () => {
    ;(apiClient.get as any).mockRejectedValueOnce(new Error('Network down'))
    await useInfraStore.getState().fetchHealth()
    const s = useInfraStore.getState()
    expect(s.healthy).toBe(false)
    expect(s.fetching).toBe(false)
  })

  it('markUnhealthy 立即 set healthy=false + lastCheck', () => {
    useInfraStore.getState().markUnhealthy('test reason')
    const s = useInfraStore.getState()
    expect(s.healthy).toBe(false)
    expect(s.lastCheck).not.toBeNull()
  })

  it('fetching 期间 fetching=true', async () => {
    let resolveFn: (v: any) => void
    const slowPromise = new Promise(r => { resolveFn = r })
    ;(apiClient.get as any).mockReturnValueOnce(slowPromise)

    const pendingFetch = useInfraStore.getState().fetchHealth()
    // 在 await 之前抓一下状态
    expect(useInfraStore.getState().fetching).toBe(true)

    resolveFn!({ data: { healthy: true, ts: 'x' } })
    await pendingFetch
    expect(useInfraStore.getState().fetching).toBe(false)
  })
})
```

- [ ] **Step 2: 跑测试确认失败**

```bash
cd /Users/java/knowledge-engineering-web && npx vitest run src/store/infra.test.ts
```
Expected: FAIL — `infra` 模块还不存在。

- [ ] **Step 3: 新建 `src/store/infra.ts`**

```typescript
/**
 * useInfraStore：基础设施健康状态 zustand store。
 * 设计：[[基础设施健康检查与产品不可用-设计]] §4.1
 *
 * 触发条件（→ set healthy）：
 *  1. App mount 时 useInfraHealthBootstrap → fetchHealth()
 *  2. axios interceptor 抓 503 INFRA_UNHEALTHY → markUnhealthy()
 *  3. SSE error 抓 503 INFRA_UNHEALTHY → markUnhealthy()
 *  4. 用户点 "重试连接" → fetchHealth()
 */
import { create } from 'zustand'
import { apiClient } from '@/api/client'

/** 单个依赖的健康状态（与后端 DepStatus 对齐）。 */
export interface DepStatus {
  ok: boolean
  error?: string
}

/** 5 个 critical 依赖的整体状态。 */
export interface InfraDeps {
  mysql: DepStatus
  neo4j: DepStatus
  weaviate: DepStatus
  dashscope: DepStatus
  ollama: DepStatus
}

interface InfraState {
  /** 总体健康状态；初值 true（乐观，避免 fetch 前阻断 UI）。 */
  healthy: boolean
  /** 5 个依赖详情，admin 用户才有；普通用户为 undefined。 */
  deps?: InfraDeps
  /** 最后一次检查的时间戳（Date.now()），用于横幅显示"最后检查 14:23"。 */
  lastCheck: number | null
  /** fetch 进行中标志，用于按钮 disabled。 */
  fetching: boolean
  /** 主动检查健康：调 /health endpoint，更新状态。 */
  fetchHealth: () => Promise<void>
  /** 被动标记不健康（catch 到 503 后立即调用）。 */
  markUnhealthy: (reason: string) => void
}

export const useInfraStore = create<InfraState>((set) => ({
  healthy: true,
  lastCheck: null,
  fetching: false,
  fetchHealth: async () => {
    set({ fetching: true })
    try {
      // apiClient 默认 baseURL='/api'，所以请求实际是 /api/health → vite proxy → backend /health
      const r = await apiClient.get('/health')
      set({
        healthy: r.data.healthy,
        deps: r.data.deps,
        lastCheck: Date.now(),
        fetching: false,
      })
    } catch (_e) {
      // /health 自身失败 = 后端完全连不上，等同于 unhealthy
      set({ healthy: false, lastCheck: Date.now(), fetching: false })
    }
  },
  markUnhealthy: (_reason: string) => {
    set({ healthy: false, lastCheck: Date.now() })
  },
}))
```

- [ ] **Step 4: 新建 `src/hooks/useInfraHealthBootstrap.ts`**

```typescript
/**
 * App 顶层 layout mount 时跑一次 fetchHealth，让 InfraBanner 第一时间反映现实。
 * 设计：[[基础设施健康检查与产品不可用-设计]] §4.2
 */
import { useEffect } from 'react'
import { useInfraStore } from '@/store/infra'

export function useInfraHealthBootstrap() {
  // 只取 fetchHealth 函数引用，避免每次 healthy 变化都 re-render
  const fetchHealth = useInfraStore(s => s.fetchHealth)
  useEffect(() => {
    void fetchHealth()
  }, [fetchHealth])
}
```

- [ ] **Step 5: 跑测试确认通过**

```bash
cd /Users/java/knowledge-engineering-web && npx vitest run src/store/infra.test.ts
```
Expected: 6 PASS。

- [ ] **Step 6: 提交**

```bash
cd /Users/java/knowledge-engineering-web
git add src/store/infra.ts src/hooks/useInfraHealthBootstrap.ts src/store/infra.test.ts
git commit -m "$(cat <<'EOF'
feat(infra): 新增 useInfraStore + useInfraHealthBootstrap hook

zustand store 维护 5 依赖的健康状态；fetchHealth 调 /health；
markUnhealthy 由 axios interceptor / SSE 错误路径调用。
设计：[[基础设施健康检查与产品不可用-设计]] §4.1-§4.2

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>
EOF
)"
```

---

## Task 9: `<InfraBanner>` 组件

**Files:**
- Create: `src/components/layout/InfraBanner.tsx`
- Test: Create `src/components/layout/InfraBanner.test.tsx`

- [ ] **Step 1: 写失败测试**

新建 `src/components/layout/InfraBanner.test.tsx`：

```tsx
/**
 * InfraBanner 渲染测试。
 * 设计：[[基础设施健康检查与产品不可用-设计]] §4.3
 */
import { describe, it, expect, vi, beforeEach } from 'vitest'
import { render, screen, fireEvent } from '@testing-library/react'
import { useInfraStore } from '@/store/infra'
import { InfraBanner } from './InfraBanner'

describe('<InfraBanner>', () => {
  beforeEach(() => {
    useInfraStore.setState({
      healthy: true,
      deps: undefined,
      lastCheck: null,
      fetching: false,
    })
  })

  it('healthy=true → 不渲染（return null）', () => {
    const { container } = render(<InfraBanner />)
    expect(container.firstChild).toBeNull()
  })

  it('healthy=false → 显示横幅 + 文案', () => {
    useInfraStore.setState({ healthy: false, lastCheck: Date.now() })
    render(<InfraBanner />)
    expect(screen.getByRole('alert')).toBeInTheDocument()
    expect(screen.getByText(/系统暂时不可用/)).toBeInTheDocument()
    expect(screen.getByRole('button', { name: /重试连接/ })).toBeInTheDocument()
  })

  it('点击重试按钮 → 调 fetchHealth', () => {
    const fetchHealthSpy = vi.fn(() => Promise.resolve())
    useInfraStore.setState({
      healthy: false,
      lastCheck: Date.now(),
      fetching: false,
      fetchHealth: fetchHealthSpy,
    })
    render(<InfraBanner />)
    fireEvent.click(screen.getByRole('button', { name: /重试连接/ }))
    expect(fetchHealthSpy).toHaveBeenCalledOnce()
  })

  it('fetching=true → 按钮 disabled + 文字"检查中…"', () => {
    useInfraStore.setState({ healthy: false, lastCheck: Date.now(), fetching: true })
    render(<InfraBanner />)
    const btn = screen.getByRole('button')
    expect(btn).toBeDisabled()
    expect(btn).toHaveTextContent(/检查中/)
  })
})
```

- [ ] **Step 2: 跑测试确认失败**

```bash
cd /Users/java/knowledge-engineering-web && npx vitest run src/components/layout/InfraBanner.test.tsx
```
Expected: FAIL — 模块不存在。

- [ ] **Step 3: 新建 `src/components/layout/InfraBanner.tsx`**

```tsx
/**
 * InfraBanner：基础设施不可用时顶部固定红色横幅 + 重试按钮。
 * 设计：[[基础设施健康检查与产品不可用-设计]] §4.3
 *
 * 主题适配（CLAUDE.md 强制 light+dark 双主题）：
 *  - 用 bg-destructive / text-destructive-foreground CSS token
 *  - 不写硬编码颜色
 *  - dark 主题下背景会自动从 light 的 #cf1124 切到一档亮的 #ff7a7e
 */
import { useInfraStore } from '@/store/infra'

export function InfraBanner() {
  // 用 selector 让组件只在相关字段变化时 re-render
  const healthy = useInfraStore(s => s.healthy)
  const fetching = useInfraStore(s => s.fetching)
  const lastCheck = useInfraStore(s => s.lastCheck)
  const fetchHealth = useInfraStore(s => s.fetchHealth)

  // healthy → 横幅不显示
  if (healthy) return null

  return (
    <div
      role="alert"
      className="
        sticky top-0 z-50
        bg-destructive text-destructive-foreground
        px-4 py-2
        flex items-center justify-between gap-3
        text-sm
        shadow-sm
      "
    >
      <div className="flex items-center gap-2">
        <span className="font-medium">系统暂时不可用，请联系管理员</span>
        {lastCheck != null && (
          <span className="text-xs opacity-80">
            最后检查 {new Date(lastCheck).toLocaleTimeString()}
          </span>
        )}
      </div>
      <button
        type="button"
        onClick={() => void fetchHealth()}
        disabled={fetching}
        className="
          px-3 py-1 rounded
          bg-destructive-foreground/10 hover:bg-destructive-foreground/20
          transition disabled:opacity-50 disabled:cursor-not-allowed
        "
      >
        {fetching ? '检查中…' : '重试连接'}
      </button>
    </div>
  )
}
```

**重要**：用户 CLAUDE.md 强制：
- ✅ 用 `bg-destructive` / `text-destructive-foreground`（Tailwind v4 token，已经 light/dark 都覆盖）
- ❌ 不写 `bg-red-500` 之类硬编码
- ❌ 不写 `#fff` `#000` 任何字面色值

确认 `bg-destructive` token 已存在：
```bash
grep -n "destructive" /Users/java/knowledge-engineering-web/src/index.css
```
应该看到 `--color-destructive` 在 `:root` + `.dark` 两个块都定义。如果没有，先在 index.css 定义（参考 `--context-danger` 类似的状态色定义模式）。

- [ ] **Step 4: 跑测试确认通过**

```bash
cd /Users/java/knowledge-engineering-web && npx vitest run src/components/layout/InfraBanner.test.tsx
```
Expected: 4 PASS。

- [ ] **Step 5: 提交**

```bash
cd /Users/java/knowledge-engineering-web
git add src/components/layout/InfraBanner.tsx src/components/layout/InfraBanner.test.tsx
git commit -m "$(cat <<'EOF'
feat(infra): 新增 <InfraBanner> 顶部红色横幅组件

healthy=false 时 sticky 顶部显示 "系统暂时不可用" + "重试连接" 按钮。
用 bg-destructive / text-destructive-foreground token，light/dark 自动适配。

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>
EOF
)"
```

---

## Task 10: AppLayout 注入 + ChatInput disabled + axios interceptor

**Files:**
- Modify: `src/components/layout/AppLayout.tsx`（或对应主 layout，先 grep 找）
- Modify: `src/components/chat/ChatInput.tsx`
- Modify: `src/api/client.ts`
- Modify: `src/store/chat.ts`（SSE error 块）

- [ ] **Step 1: 找当前的主 layout**

```bash
find /Users/java/knowledge-engineering-web/src -name "AppLayout*" -o -name "RootLayout*" -o -name "MainLayout*" 2>/dev/null
grep -rn "Outlet\|<Route" /Users/java/knowledge-engineering-web/src/App.tsx 2>/dev/null | head -5
```

如果没有 AppLayout，可能 layout 直接写在 `src/App.tsx`。在那里注入。

- [ ] **Step 2: 修改主 layout**（路径以 grep 结果为准；以 `App.tsx` 为例）

在主 layout 组件函数体顶部加：

```tsx
import { InfraBanner } from '@/components/layout/InfraBanner'
import { useInfraHealthBootstrap } from '@/hooks/useInfraHealthBootstrap'

function App() {  // 或 AppLayout
  useInfraHealthBootstrap()   // mount 时 fetch 一次
  return (
    <>
      <InfraBanner />
      {/* ... 现有 layout 内容 ... */}
    </>
  )
}
```

- [ ] **Step 3: 修改 `src/components/chat/ChatInput.tsx`**（路径以 grep 为准）

```bash
find /Users/java/knowledge-engineering-web/src -name "ChatInput*" 2>/dev/null
```

在 ChatInput 顶部加：
```tsx
import { useInfraStore } from '@/store/infra'
```

在组件函数体内：
```tsx
const healthy = useInfraStore(s => s.healthy)
```

在 textarea / send button 上加 disabled：
```tsx
<textarea
  disabled={!healthy || /* existing disabled conditions */}
  placeholder={!healthy ? '系统暂时不可用，请等待恢复...' : /* existing placeholder */}
  /* ... */
/>
<button
  type="submit"
  disabled={!healthy || /* existing */}
  title={!healthy ? '系统暂时不可用' : /* existing title */}
>
  /* ... */
</button>
```

- [ ] **Step 4: 修改 `src/api/client.ts`** — 加 response interceptor

在文件末尾（或现有 interceptor 之后）追加：

```typescript
// 基础设施不可用时被动检测：抓 503 + code==='INFRA_UNHEALTHY' → 立即标记 store
// 设计：[[基础设施健康检查与产品不可用-设计]] §4.6
apiClient.interceptors.response.use(
  r => r,
  (error) => {
    if (
      error.response?.status === 503 &&
      error.response?.data?.detail?.code === 'INFRA_UNHEALTHY'
    ) {
      // dynamic import 避免 store ↔ client 循环 import
      import('@/store/infra').then(({ useInfraStore }) => {
        useInfraStore.getState().markUnhealthy('axios 503 INFRA_UNHEALTHY')
      })
    }
    return Promise.reject(error)
  }
)
```

- [ ] **Step 5: 修改 `src/store/chat.ts` SSE error 块**

找到 `chat.ts:366-367` 附近的 `if (!res.ok) { throw ... }` 块（即 Task 1 之前我们看过的位置）。改为：

旧：
```typescript
if (!res.ok) {
  const text = await res.text().catch(() => '')
  throw new Error(`HTTP ${res.status}: ${text || res.statusText}`)
}
```

新：
```typescript
if (!res.ok) {
  const text = await res.text().catch(() => '')
  // 识别 503 INFRA_UNHEALTHY → 同步标记 infra store，让 InfraBanner 立即出
  // 设计：[[基础设施健康检查与产品不可用-设计]] §4.7
  if (res.status === 503) {
    try {
      const body = JSON.parse(text)
      if (body?.detail?.code === 'INFRA_UNHEALTHY') {
        const { useInfraStore } = await import('@/store/infra')
        useInfraStore.getState().markUnhealthy('SSE 503 INFRA_UNHEALTHY')
      }
    } catch { /* ignore JSON parse 错 */ }
  }
  throw new Error(`HTTP ${res.status}: ${text || res.statusText}`)
}
```

- [ ] **Step 6: 跑 vitest 全套 + lint 验证不破坏现有**

```bash
cd /Users/java/knowledge-engineering-web && npx vitest run 2>&1 | tail -10
```
Expected: 现有测试 + 新加测试全过。

```bash
cd /Users/java/knowledge-engineering-web && npm run lint 2>&1 | tail -10
```
Expected: no errors。

- [ ] **Step 7: 提交**

```bash
cd /Users/java/knowledge-engineering-web
git add src/App.tsx src/components/chat/ChatInput.tsx src/api/client.ts src/store/chat.ts
# 或对应你的 layout 文件
git commit -m "$(cat <<'EOF'
feat(infra): 接入 InfraBanner + ChatInput disabled + axios/SSE 503 拦截

App 顶层 mount 时 fetch /health；chat input 随 healthy 同步 disabled；
axios + SSE 任一抓到 503 INFRA_UNHEALTHY 立即标记 store。

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>
EOF
)"
```

---

## Task 11: 后端全套回归 + 前端全套回归

- [ ] **Step 1: 后端**

```bash
cd /Users/java/knowledge-engineering-auth && ./venv/bin/python -m pytest tests/test_auth -q --tb=short 2>&1 | tail -10
```
Expected: 全 PASS。新加测试约 12（infra_health 15 + require_infra_healthy 4 + health_endpoint 3 + qa_router_503 3 - 部分合并），baseline 632 → 约 644。

如有 FAIL：grep + 修。常见原因：
- 测试调用了未 mock 的真实服务（修：补 mock）
- `from src.service.api import require_infra_healthy` 路径在 Task 7 改了 → 改 import 路径到 `from src.service.deps_infra import require_infra_healthy`

- [ ] **Step 2: 前端**

```bash
cd /Users/java/knowledge-engineering-web && npx vitest run 2>&1 | tail -10
```
Expected: 全 PASS。

- [ ] **Step 3: 如有 fix 提交**

```bash
cd /Users/java/knowledge-engineering-auth   # or web
git add -p   # 选择真正改动
git commit -m "$(cat <<'EOF'
test(infra): 回归 fix — 适配 require_infra_healthy 抽出 / 其他细节

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>
EOF
)"
```

---

## Task 12: E2E 手测 + Obsidian doc 完成标记

**Files (无代码改动，仅手测 + doc update):**
- Modify: `/Users/java/obsidian/01 Engineering/knowledge-engineering/基础设施健康检查与产品不可用-设计.md`（最末加 "实施完成日期" + commit 列表）

- [ ] **Step 1: 启动后端（带 KE_QA_USE_REACT=1）**

```bash
# 如果旧 uvicorn 还在跑：
lsof -nP -iTCP:8000 -sTCP:LISTEN 2>/dev/null
# 杀掉旧 PID 重启（命令同 Phase 8 / Task 6 of 前一个 plan）
cd /Users/java/knowledge-engineering-auth && KE_QA_USE_REACT=1 nohup ./venv/bin/uvicorn src.service.api:app --host 127.0.0.1 --port 8000 --reload > /tmp/uvicorn-react.log 2>&1 &
sleep 5
tail -20 /tmp/uvicorn-react.log
```
Expected: log 中看到 `[startup] infra_status: {'mysql': True, 'neo4j': True, 'weaviate': True, 'dashscope': True, 'ollama': True}` 或部分 False。

- [ ] **Step 2: 验证 healthy 时一切正常**

浏览器打开 http://localhost:5173/，登录 alice/test12345，切到 mall-swarm，应该：
- ✅ 顶部**无**红色横幅
- ✅ chat 输入框可输入、发送按钮可点

- [ ] **Step 3: 模拟 MySQL 挂**

```bash
# 关 mysql tunnel
bash /Users/java/knowledge-engineering-auth/scripts/stop_mysql_tunnel.sh
```

刷新浏览器（或点重试连接）：
- ✅ 顶部出现红色横幅 "系统暂时不可用..."
- ✅ chat input + 发送按钮 disabled
- ✅ 横幅显示 "最后检查 HH:MM:SS"

- [ ] **Step 4: 验证 admin 看详细**

```bash
# 用 admin 调 /health
curl -sS -X POST http://localhost:8000/auth/login \
  -H "Content-Type: application/json" \
  -d '{"username":"admin","password":"test12345"}' \
  | python3 -c "import sys,json; print(json.load(sys.stdin)['access_token'])" > /tmp/admin.token
curl -sS -H "Authorization: Bearer $(cat /tmp/admin.token)" http://localhost:8000/health | python3 -m json.tool
```
Expected: 含 `deps.mysql.ok=false` 字段。

```bash
# 用 alice 调 /health
curl -sS -X POST http://localhost:8000/auth/login \
  -H "Content-Type: application/json" \
  -d '{"username":"alice","password":"test12345"}' \
  | python3 -c "import sys,json; print(json.load(sys.stdin)['access_token'])" > /tmp/alice.token
curl -sS -H "Authorization: Bearer $(cat /tmp/alice.token)" http://localhost:8000/health | python3 -m json.tool
```

Expected: **不含** deps 字段，只有 `{"healthy": false, "ts": "..."}`。

- [ ] **Step 5: 恢复 MySQL 验证横幅消失**

```bash
bash /Users/java/knowledge-engineering-auth/scripts/start_mysql_tunnel.sh
```

浏览器内点击 "重试连接"：
- ✅ 横幅消失
- ✅ chat 解禁

- [ ] **Step 6: 验证 light/dark 主题横幅可读**

切换主题（点 sidebar 主题按钮 / 控制台 `document.documentElement.classList.toggle('dark')`），在 mysql 挂的状态下两个主题都截图：
- light：背景红（深红 `bg-destructive` light token）+ 文字白
- dark：背景红（亮一档 `bg-destructive` dark token）+ 文字白

对比度 ≥ 4.5:1（用户 CLAUDE.md §自检 checklist）。

- [ ] **Step 7: 更新 Obsidian 设计 doc 加完成标记**

打开 `/Users/java/obsidian/01 Engineering/knowledge-engineering/基础设施健康检查与产品不可用-设计.md`，在文末追加：

```markdown
---

## §12 实施完成（2026-05-26）

12 个 task 全部完成（subagent-driven，每 task 双 review）。Commits 列表（按时间顺序）：

| Task | commit | 内容 |
|---|---|---|
| 1 | `<sha>` | infra_health.py 骨架 + _ping_mysql + _ping_neo4j |
| 2 | `<sha>` | 补齐 weaviate / dashscope / ollama 3 ping |
| 3 | `<sha>` | check_all_deps 并发编排 |
| 4 | `<sha>` | api.py startup hook 写 app.state.infra_status |
| 5 | `<sha>` | require_infra_healthy dependency |
| 6 | `<sha>` | /health endpoint 重写 |
| 7 | `<sha>` | 9 router 附 dependency + 抽 deps_infra.py |
| 8 | `<sha>` | 前端 useInfraStore + bootstrap hook |
| 9 | `<sha>` | <InfraBanner> 组件 light+dark |
| 10 | `<sha>` | AppLayout 注入 + ChatInput disabled + axios/SSE interceptor |
| 11 | `<sha>` | 全套回归测试 |
| 12 | （本提交）| E2E 手测 + doc 完成标记 |

验收结果：§9 八条全部通过（截图见 ../assets/infra-banner-light.png + ../assets/infra-banner-dark.png）。
```

把 `<sha>` 替换为实际 commit SHA（用 `git log --oneline -15` 拿）。

如果 Obsidian vault 是 git 仓库：
```bash
cd /Users/java/obsidian
git add "01 Engineering/knowledge-engineering/基础设施健康检查与产品不可用-设计.md"
git commit -m "docs(ke-infra): §12 实施完成记录"
```

- [ ] **Step 8: 提交（如果 git status 还有改动）**

```bash
cd /Users/java/knowledge-engineering-auth && git status
cd /Users/java/knowledge-engineering-web && git status
```
都应该 clean。如果不是，按改动语义补 commit。

---

## Self-Review

**1. Spec 覆盖**

| Spec 段落 | Task 实现 |
|---|---|
| §0 / §1 共识 | Plan 顶部 Goal + 各 task 实现细节 |
| §2 总体架构 | Task 1-7 后端 + Task 8-10 前端共同实现 |
| §3.1 infra_health.py | Task 1-3 |
| §3.2 api.py | Task 4-6 |
| §3.3 路由依赖 | Task 7 |
| §4.1 store | Task 8 |
| §4.2 bootstrap hook | Task 8 |
| §4.3 InfraBanner | Task 9 |
| §4.4 AppLayout | Task 10 |
| §4.5 ChatInput | Task 10 |
| §4.6 axios interceptor | Task 10 |
| §4.7 chat.ts SSE | Task 10 |
| §5 数据流 | 各 task 实现 + Task 12 E2E 验证 |
| §6 错误码 | Task 5 + Task 10 |
| §7 测试 | Task 1-11 |
| §8 文件清单 | Plan File Structure |
| §9 验收 | Task 12 |
| §10 风险 | Plan task 1（DashScope 用 1 字符 embed） + Plan 注意事项 |

**全覆盖** ✅。

**2. Placeholder scan**：无 TBD / TODO / 占位符。所有 step 都有真实 code 或 command。

**3. Type / signature 一致**：
- `check_all_deps(app_state) -> InfraStatus` — Task 3 定义，Task 4 用，Task 6 用
- `require_infra_healthy(request, user)` — Task 5 定义，Task 7 注入到 router
- `useInfraStore` schema — Task 8 定义，Task 9-10 消费
- `INFRA_UNHEALTHY` code — Task 5 后端定义，Task 10 前端拦截都用同一字符串

一致 ✅。

---
