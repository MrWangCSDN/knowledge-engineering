# ke_search project_id 闭包注入修复 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 修复 `ke_search` 工具 schema 强制 LLM 输入 `project_id` 导致 LLM 猜错租户（mall-swarm 实测被猜成 "pms"）的 bug；改为由 `build_tools_for_project` 闭包注入 project_id，与 `Neo4jGraphAdapter` 一致。

**Architecture:** 8 个工具里只有 `ke_search` 一个需要修（其他 7 个用 `entity_id` 走 adapter 闭包已经隔离）。修复 3 个文件 + 3 个测试 + 1 段 system prompt + Obsidian 设计文档。Schema 强制约束去除后，LLM 不再有机会"猜"，由 URL path 唯一来源决定 tenant。

**Tech Stack:** Python 3.12 + FastAPI + pytest + Weaviate 1.33 Native Multi-Tenancy。

**仓库 / 分支:** `/Users/java/knowledge-engineering-auth`，分支 `release-0513`（领先 main 226 commits）。

**Run tests:** `cd /Users/java/knowledge-engineering-auth && ./venv/bin/python -m pytest tests/test_auth -q`。

**关键现状（已确认）：**
- `src/service/qa_engine/tools/ke_search.py:14-34`：schema `required=["query","project_id"]`，handler `input.get("project_id")`。
- 其他 7 个 `ke_*` 工具 schema `required=["entity_id"]`，project_id 走 adapter 闭包（如 `Neo4jGraphAdapter(neo4j_backend, project_id=project_id)`）。
- `src/service/qa_engine/tools/__init__.py:59`：`registry.register(build_ke_search_tool(business_store))` — 没传 project_id。
- `src/service/qa_router.py:122-130`：`build_tools_for_project(project_id, request)` 已经有 project_id 在手，但只传给 `Neo4jGraphAdapter`，没传给 `build_default_registry`。
- `src/service/qa_engine/react_synthesizer.py:399-400`：system prompt 第 3 条 "project_id 跟用户当前会话保持一致（默认从用户问题语境推断，比如 petclinic / deposit）" — 误导 LLM 自己猜。
- startup 时 `app.state.qa_synthesizer` 构造时 `tool_registry=tools_hint`（可能 None）；per-request 才调 `build_tools_for_project`（见 `_inject_per_request_tool_registry`，Task 24 commit 74f1462）。所以 `build_default_registry` 实际只在 per-request 调用，加 project_id 参数不影响 startup。
- 已有测试：`test_qa_tools_default_registry.py`、`test_qa_router_tools_injection.py`、`test_qa_tool_registry.py`。

**设计文档参考：** Obsidian `[[代码解读Agent引擎-设计]]` §3.6（多租户缺口）— 本次新发现的"上层 schema 缺口"与 §3.6 描述的"底层 collection 缺口"是不同层面的 bug，但同属多租户加固范畴。

---

## File Structure

| 文件 | 改动 | 责任 |
|---|---|---|
| `src/service/qa_engine/tools/ke_search.py` | Modify | schema 删 project_id；`build_ke_search_tool(store, project_id)` 闭包注入；handler 不再读 input.project_id |
| `src/service/qa_engine/tools/__init__.py` | Modify | `build_default_registry(... project_id: str)` 加必填参数，透传给 ke_search builder |
| `src/service/qa_router.py` | Modify | `build_tools_for_project` 把 project_id 透传给 `build_default_registry`；更新行 100 注释 |
| `src/service/qa_engine/react_synthesizer.py` | Modify | 行 382 / 399-400：删/改 "project_id 自己推断" 那段 prompt，改成 "工具已绑定当前工程，无需提供 project_id" |
| `tests/test_auth/test_qa_tool_ke_search.py` | Create | 新文件，专测 ke_search 闭包行为 + schema 不含 project_id |
| `tests/test_auth/test_qa_tools_default_registry.py` | Modify | 适配 `build_default_registry` 新增 `project_id` 必填参数 |
| `tests/test_auth/test_qa_router_tools_injection.py` | Modify | 验证 per-request 注入路径携带正确 project_id 给 ke_search |
| `/Users/java/obsidian/01 Engineering/knowledge-engineering/代码解读Agent引擎-设计.md` | Modify | §3.6 附加描述 schema 层缺口已修；§13 决策日志加 entry #8 |

---

## Task 1: 改 ke_search 工具签名 — closure project_id

**Files:**
- Modify: `src/service/qa_engine/tools/ke_search.py`
- Test: Create `tests/test_auth/test_qa_tool_ke_search.py`

- [ ] **Step 1: 写失败测试 — `tests/test_auth/test_qa_tool_ke_search.py`**

```python
"""ke_search 工具闭包注入 project_id 行为测试。

修复前：schema required=["query","project_id"]，handler 从 input.get("project_id") 取，LLM 猜对就用，猜错就查不到。
修复后：schema 只 required=["query"]，handler 从 build_ke_search_tool(store, project_id) 闭包注入。
"""
import asyncio
import pytest
from unittest.mock import MagicMock

from src.service.qa_engine.tools.ke_search import build_ke_search_tool


def _run(coro):
    """同步跑 async — pytest-asyncio 没启用时用这个 helper。"""
    return asyncio.get_event_loop().run_until_complete(coro)


def test_ke_search_schema_drops_project_id():
    """schema 不再 require project_id（修 LLM 猜 tenant 的 bug）。"""
    store = MagicMock()
    tool = build_ke_search_tool(store, project_id="mall-swarm")
    # schema 必填只剩 query
    assert tool.input_schema["required"] == ["query"]
    # properties 里也不再有 project_id（不让 LLM 误以为可以传）
    assert "project_id" not in tool.input_schema["properties"]


def test_ke_search_handler_uses_closure_project_id():
    """handler 用 builder 闭包的 project_id，不读 input dict。"""
    store = MagicMock()
    store.search_method_hits_by_text.return_value = [{"entity_id": "method//abc", "score": 0.9}]

    tool = build_ke_search_tool(store, project_id="mall-swarm")
    # LLM 即便恶意传 project_id="wrong"，也要被忽略
    result = _run(tool.handler({"query": "OrderService", "project_id": "wrong-tenant"}))

    # 验证 store 被以闭包 project_id 调用，不是 LLM 输入
    store.search_method_hits_by_text.assert_called_once_with(
        text="OrderService", project_id="mall-swarm", limit=5
    )
    assert result["results"][0]["entity_id"] == "method//abc"


def test_ke_search_handler_missing_query_returns_error():
    """缺 query 立即返回错误（不调 store）。"""
    store = MagicMock()
    tool = build_ke_search_tool(store, project_id="mall-swarm")
    result = _run(tool.handler({"query": ""}))
    assert "error" in result
    assert result["results"] == []
    store.search_method_hits_by_text.assert_not_called()


def test_ke_search_builder_rejects_empty_project_id():
    """builder 阶段就拒绝空 project_id（避免运行时 with_tenant('') 的隐蔽 bug）。"""
    store = MagicMock()
    with pytest.raises(ValueError, match="project_id"):
        build_ke_search_tool(store, project_id="")
```

- [ ] **Step 2: 跑测试确认失败**

Run: `cd /Users/java/knowledge-engineering-auth && ./venv/bin/python -m pytest tests/test_auth/test_qa_tool_ke_search.py -v`
Expected: FAIL — `build_ke_search_tool() got unexpected keyword argument 'project_id'`（当前 signature 只接 store）

- [ ] **Step 3: 改 ke_search.py — schema + builder + handler**

完整新内容：

```python
"""ke_search 工具：BusinessInterpretation 向量库语义检索。

v1.3 修复（2026-05-26）：schema 去掉 project_id 必填；project_id 改由
build_ke_search_tool(store, project_id) 闭包注入，与 Neo4jGraphAdapter 的设计对齐。
原因：让 LLM 自己猜 project_id 会出错（mall-swarm 实测被猜成 "pms"/"pms-product"
→ tenant 不匹配 → 0 结果）。
"""
from __future__ import annotations

from typing import Any

from src.service.qa_engine.retriever import BusinessStoreProto
from src.service.qa_engine.tools.base import Tool


# schema：只 require query；project_id 走闭包不暴露给 LLM
_KE_SEARCH_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "query": {
            "type": "string",
            "description": "自然语言查询（中文优先）",
        },
        "limit": {
            "type": "integer",
            "description": "返回结果上限",
            "default": 5,
            "minimum": 1,
            "maximum": 50,
        },
    },
    "required": ["query"],
}


def build_ke_search_tool(store: BusinessStoreProto, project_id: str) -> Tool:
    """构造一个绑定到指定 store + project_id 的 ke_search Tool。

    :param store: BusinessInterpretation 向量库 store（实现 BusinessStoreProto）
    :param project_id: 当前请求的工程 ID（Weaviate tenant 标识）。
        必须非空，由 build_tools_for_project 从 URL path 传入。
    :raises ValueError: project_id 为空字符串。
    """
    # 防御：空 project_id 等于跨租户查询，必须在 builder 阶段就拒
    if not project_id or not project_id.strip():
        raise ValueError("build_ke_search_tool: project_id 不能为空")
    bound_project_id = project_id.strip()

    async def handler(input: dict[str, Any]) -> dict[str, Any]:
        # query 仍从 input 取（LLM 提供的查询语句）
        query = (input.get("query") or "").strip()
        if not query:
            return {
                "query": query,
                "results": [],
                "error": "missing required field: query",
            }

        try:
            limit = int(input.get("limit", 5))
        except (TypeError, ValueError):
            limit = 5

        # project_id 用闭包绑定值，不从 input 取（即使 LLM 误传也忽略）
        try:
            results = store.search_method_hits_by_text(
                text=query, project_id=bound_project_id, limit=limit
            )
        except Exception as e:
            return {
                "query": query,
                "results": [],
                "error": f"search backend error: {e}",
            }

        return {"query": query, "results": list(results)}

    return Tool(
        name="ke_search",
        description="在 BusinessInterpretation 向量库语义检索代码实体（method / class / module / api）；project_id 已由后端绑定，无需提供。",
        input_schema=_KE_SEARCH_SCHEMA,
        handler=handler,
    )
```

- [ ] **Step 4: 跑测试确认通过**

Run: `cd /Users/java/knowledge-engineering-auth && ./venv/bin/python -m pytest tests/test_auth/test_qa_tool_ke_search.py -v`
Expected: PASS（4 个测试全过）

- [ ] **Step 5: 提交**

```bash
cd /Users/java/knowledge-engineering-auth
git add src/service/qa_engine/tools/ke_search.py tests/test_auth/test_qa_tool_ke_search.py
git commit -m "fix(qa-engine): ke_search project_id 改用 builder 闭包注入

修 LLM 猜 tenant 导致 mall-swarm 查不到数据的 bug（猜成 'pms' / 'pms-product'）。
schema 去掉 project_id required + properties；build_ke_search_tool(store, project_id)
闭包绑定，与 Neo4jGraphAdapter 设计对齐。

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>"
```

---

## Task 2: 改 build_default_registry — 加 project_id 参数透传

**Files:**
- Modify: `src/service/qa_engine/tools/__init__.py`
- Modify: `tests/test_auth/test_qa_tools_default_registry.py`

- [ ] **Step 1: 写失败测试 — 修改 `tests/test_auth/test_qa_tools_default_registry.py`**

先 Read 现有文件确认结构，然后在文件**末尾**追加：

```python
def test_build_default_registry_requires_project_id():
    """build_default_registry 现在必填 project_id（透传给 ke_search 闭包）。"""
    import pytest
    from unittest.mock import MagicMock
    from src.service.qa_engine.tools import build_default_registry

    graph = MagicMock()
    business = MagicMock()
    # 不传 project_id 应当报错（旧 signature 兼容性破坏，意在强制升级调用方）
    with pytest.raises(TypeError):
        build_default_registry(graph=graph, business_store=business)  # type: ignore[call-arg]


def test_build_default_registry_passes_project_id_to_ke_search():
    """build_default_registry 把 project_id 闭包传到 ke_search。"""
    from unittest.mock import MagicMock
    from src.service.qa_engine.tools import build_default_registry

    graph = MagicMock()
    business = MagicMock()
    business.search_method_hits_by_text.return_value = []

    registry = build_default_registry(
        graph=graph, business_store=business, project_id="mall-swarm"
    )
    # 拿 ke_search 工具并调一次 handler，验证 project_id 闭包到位
    import asyncio
    tool = registry.get("ke_search")
    asyncio.get_event_loop().run_until_complete(
        tool.handler({"query": "X"})
    )
    business.search_method_hits_by_text.assert_called_once_with(
        text="X", project_id="mall-swarm", limit=5
    )
```

注意：现有测试里调用 `build_default_registry(graph=..., business_store=...)` 的所有用例都要补 `project_id="test"`。先 grep 找出来：
```bash
grep -n "build_default_registry(" tests/test_auth/test_qa_tools_default_registry.py
```
逐一在调用处加 `project_id="test"` 参数。

- [ ] **Step 2: 跑测试确认失败**

Run: `cd /Users/java/knowledge-engineering-auth && ./venv/bin/python -m pytest tests/test_auth/test_qa_tools_default_registry.py -v`
Expected: FAIL — 新加的两个测试会因为 `build_default_registry` 还接受无 project_id 调用而失败，老测试已经补 `project_id="test"` 也会失败（因为 builder signature 还没改）。

- [ ] **Step 3: 改 __init__.py — build_default_registry 加 project_id 必填**

把 `src/service/qa_engine/tools/__init__.py:45-71` 改为：

```python
def build_default_registry(
    *,
    graph: GraphProto,
    business_store: BusinessStoreProto,
    project_id: str,
    code_store: Any | None = None,
    method_interp_store: Any | None = None,
) -> ToolRegistry:
    """构造预装 ke_* 工具 + todo_write 元工具的 ToolRegistry。

    v1.3 修复（2026-05-26）：新增必填 project_id，闭包透传给 ke_search（修 LLM 猜 tenant bug）。
    其他 ke_* 工具的 project_id 由 graph adapter（如 Neo4jGraphAdapter）实例化时绑定，
    本函数不直接经手。

    :param graph: 已绑 project_id 的 GraphProto adapter（如 Neo4jGraphAdapter(..., project_id=...)）
    :param business_store: BusinessInterpretation store 的 adapter
    :param project_id: 当前请求工程 ID — 透传给 ke_search 闭包
    :param code_store: 可选 CodeEntity store
    :param method_interp_store: 可选 MethodInterpretation store
    """
    registry = ToolRegistry()
    # ke_search 闭包绑 project_id（与 Neo4jGraphAdapter 设计对齐）
    registry.register(build_ke_search_tool(business_store, project_id))
    registry.register(build_ke_business_interp_tool(business_store))
    registry.register(build_ke_callees_tool(graph))
    registry.register(build_ke_callers_tool(graph))
    registry.register(build_ke_table_access_tool(graph))
    registry.register(build_ke_impact_tool(graph))
    registry.register(build_todo_write_tool())
    if code_store is not None:
        registry.register(build_ke_read_entity_tool(code_store))
    if method_interp_store is not None:
        registry.register(build_ke_method_interp_tool(method_interp_store))
    return registry
```

- [ ] **Step 4: 跑测试确认通过**

Run: `cd /Users/java/knowledge-engineering-auth && ./venv/bin/python -m pytest tests/test_auth/test_qa_tools_default_registry.py -v`
Expected: PASS（所有测试通过）

- [ ] **Step 5: 提交**

```bash
cd /Users/java/knowledge-engineering-auth
git add src/service/qa_engine/tools/__init__.py tests/test_auth/test_qa_tools_default_registry.py
git commit -m "fix(qa-engine): build_default_registry 加 project_id 必填透传 ke_search

闭包注入 project_id 给 ke_search，配合 Task1 schema 改动。
其他 ke_* 工具 project_id 走 adapter 已隔离，本函数不经手。

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>"
```

---

## Task 3: qa_router build_tools_for_project 透传 project_id

**Files:**
- Modify: `src/service/qa_router.py:96-130`
- Modify: `tests/test_auth/test_qa_router_tools_injection.py`

- [ ] **Step 1: 写失败测试 — 修改 `tests/test_auth/test_qa_router_tools_injection.py`**

Read 现有文件，在末尾追加：

```python
def test_build_tools_for_project_passes_project_id_to_registry():
    """build_tools_for_project 把 URL path 的 project_id 闭包给 ke_search。

    关键不变量：ke_search 收到 LLM 的 input 即使**没**含 project_id 也能正常用闭包查；
    即使 LLM 误传 project_id="wrong" 也被忽略。
    """
    from unittest.mock import MagicMock
    from fastapi import Request
    from src.service.qa_router import build_tools_for_project

    # 准备 fake request.app.state
    request = MagicMock(spec=Request)
    request.app.state.weaviate_business_store = MagicMock()
    request.app.state.neo4j_backend = MagicMock()
    request.app.state.weaviate_code_store = None
    request.app.state.weaviate_method_interp_store = None
    # business store 的搜索返回固定值便于断言
    request.app.state.weaviate_business_store.search_method_hits_by_text.return_value = []

    registry = build_tools_for_project("mall-swarm", request)
    ke_search = registry.get("ke_search")

    # LLM 误传 wrong-tenant 也忽略，用 URL path 闭包的 mall-swarm
    import asyncio
    asyncio.get_event_loop().run_until_complete(
        ke_search.handler({"query": "X", "project_id": "wrong-tenant"})
    )
    # 真实调用应该用 mall-swarm 而不是 wrong-tenant
    biz_store_call = request.app.state.weaviate_business_store.search_method_hits_by_text
    biz_store_call.assert_called_once()
    call_kwargs = biz_store_call.call_args.kwargs
    assert call_kwargs["project_id"] == "mall-swarm"
```

如果文件还有用 `build_default_registry()` 不带 `project_id=` 的旧测试调用，逐一补上 `project_id="test"`。

- [ ] **Step 2: 跑测试确认失败**

Run: `cd /Users/java/knowledge-engineering-auth && ./venv/bin/python -m pytest tests/test_auth/test_qa_router_tools_injection.py -v`
Expected: FAIL — `build_tools_for_project` 内部 `build_default_registry(...)` 没传 project_id，会 TypeError。

- [ ] **Step 3: 改 qa_router.py — build_tools_for_project 透传 project_id**

把 `src/service/qa_router.py:125-130` 改为：

```python
    return build_default_registry(
        graph=graph_adapter,
        business_store=biz_adapter,
        project_id=project_id,
        code_store=code_store,
        method_interp_store=method_interp_store,
    )
```

同时更新 `src/service/qa_router.py:100` 注释（旧注释误导）：

旧：
```
工具里的 handler 通过 input dict 的 project_id 字段传递隔离信息（ke_search 等）。
```

改为：
```
工具里的 project_id 由 adapter（Neo4jGraphAdapter）和 build_default_registry 闭包传入；
LLM 不再需要在 input 里指定 project_id（修 2026-05-26 mall-swarm 实测 LLM 猜错 tenant 的 bug）。
```

- [ ] **Step 4: 跑测试确认通过**

Run: `cd /Users/java/knowledge-engineering-auth && ./venv/bin/python -m pytest tests/test_auth/test_qa_router_tools_injection.py -v`
Expected: PASS

- [ ] **Step 5: 提交**

```bash
cd /Users/java/knowledge-engineering-auth
git add src/service/qa_router.py tests/test_auth/test_qa_router_tools_injection.py
git commit -m "fix(qa-router): build_tools_for_project 透传 project_id 给 default_registry

URL path 的 project_id 闭包到 ke_search，LLM 输入的 project_id 被忽略。
更新行 100 注释，去掉误导信息。

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>"
```

---

## Task 4: 改 ReActSynthesizer system prompt — 删 "project_id 自己推断" 段

**Files:**
- Modify: `src/service/qa_engine/react_synthesizer.py:382-400`

- [ ] **Step 1: Read 现状确认行号未漂移**

Run: `grep -n "project_id 跟用户当前会话保持一致" /Users/java/knowledge-engineering-auth/src/service/qa_engine/react_synthesizer.py`
预期：1 行命中。

- [ ] **Step 2: 改 prompt 第 3 条**

把 `react_synthesizer.py:399-400` 这两行：

```
3. **project_id 跟用户当前会话保持一致**（默认从用户问题语境推断，比如 petclinic / deposit）；
   不要拿模块 id（如 petclinic-root）当 project_id —— 它们不是一回事。
```

改为：

```
3. **不要在 tool_call 输入里指定 project_id**。工具已经由后端绑定到当前会话的工程，
   你提供 project_id 会被忽略（schema 也不再包含该字段）。
```

同时把 `react_synthesizer.py:382` 那行注释也对齐：

旧：
```
#   3. 强调 project_id 从用户问题或上下文里来 → 解决"猜成 petclinic-root 这种模块名"
```

改为：
```
#   3. 提示 LLM 不要在 input 里塞 project_id（后端闭包绑定，已不再 schema 暴露）
```

- [ ] **Step 3: 跑相关测试看是否有 prompt 字符串 assert**

Run: `cd /Users/java/knowledge-engineering-auth && ./venv/bin/python -m pytest tests/test_auth -q -k "synthes or prompt" 2>&1 | tail -20`
Expected: 全 PASS（若有断言"project_id 跟用户当前会话保持一致"的源码不变量测试，需要更新；先跑看有没有）。如果有 FAIL，修对应测试断言。

- [ ] **Step 4: 提交**

```bash
cd /Users/java/knowledge-engineering-auth
git add src/service/qa_engine/react_synthesizer.py
# 若 step3 改了测试，把测试也加上
git commit -m "fix(qa-engine): system prompt 删 'project_id 自己推断' 误导段

修复 LLM 看到 PmsBrandController 猜 project_id='pms' 的根因。
改为明确告知 LLM 不要在 tool_call 提供 project_id（后端闭包已绑定）。

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>"
```

---

## Task 5: tests/test_auth 整体回归

- [ ] **Step 1: 跑全套测试**

Run: `cd /Users/java/knowledge-engineering-auth && ./venv/bin/python -m pytest tests/test_auth -q 2>&1 | tail -15`
Expected: 整体通过；之前 Phase 8 跑通的 625 个测试应当**不少于 625 + 4（Task1 新增）= 629** 个通过；新增的 ke_search 闭包测试也在通过列表。

- [ ] **Step 2: 若有 FAIL，逐一定位**

可能 FAIL 来源：
- 任何用 `build_default_registry(...)` 不带 `project_id=` 的老测试（grep 找）
- 任何用 `build_ke_search_tool(store)` 不带 `project_id=` 的老测试
- 任何用 `"project_id"` 校验 schema 的老测试

Run: 
```bash
cd /Users/java/knowledge-engineering-auth
grep -rn "build_default_registry(" tests/test_auth/ | grep -v "project_id"
grep -rn "build_ke_search_tool(" tests/test_auth/
```
逐一补 `project_id="test"`。

- [ ] **Step 3: 再跑确认 PASS**

Run: `cd /Users/java/knowledge-engineering-auth && ./venv/bin/python -m pytest tests/test_auth -q 2>&1 | tail -5`
Expected: `XXX passed, 2 deselected`，无 failed。

- [ ] **Step 4: 提交（若 step 2 改了测试）**

```bash
cd /Users/java/knowledge-engineering-auth
git add tests/test_auth/
git commit -m "test(qa-engine): 老测试补 project_id 参数适配 build_default_registry 改动

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>"
```

---

## Task 6: 端到端验证 — mall-swarm 实问 + 真数据查到

**Files:** （无代码变更，仅手动 + 截图）

- [ ] **Step 1: 确认后端 uvicorn 还在跑（KE_QA_USE_REACT=1）**

Run: `lsof -nP -iTCP:8000 -sTCP:LISTEN 2>/dev/null | head -3`
Expected: Python 进程监听 8000。如果不在，重启：
```bash
cd /Users/java/knowledge-engineering-auth
KE_QA_USE_REACT=1 nohup ./venv/bin/uvicorn src.service.api:app --host 127.0.0.1 --port 8000 --reload > /tmp/uvicorn-react.log 2>&1 &
```

uvicorn `--reload` 应该已经识别 Task 1-4 文件改动自动 reload。验证：
Run: `tail -20 /tmp/uvicorn-react.log | grep -E "reload|reloading|complete"`
Expected: 看到 reload 标志或最新 Application startup complete。

- [ ] **Step 2: 浏览器测试 — alice 登录 mall-swarm 发问**

打开 http://localhost:5173/，登录 alice/test12345，URL 切到 /project/mall-swarm，问 "列出 PmsBrandController 这个类的所有方法"。

Expected: 
- ke_search ToolCallCard 显示 `query: "PmsBrandController..."` **且** `project_id` 字段**不在** args 显示里（schema 已删除该字段）
- "查看结果" 展开看到 `results: [...]`，**非空**（生产 Neo4j 有 mall-swarm 7687 节点 + Weaviate 业务解读 store）
- ThinkingBlock 灰字记录 agent 推理
- 答案里出现真实的 PmsBrandController 方法名（如 createBrand / updateBrand / deleteBrand / listBrand 等）

- [ ] **Step 3: 后端日志确认 project_id 一致**

Run: `tail -50 /tmp/uvicorn-react.log | grep -E "search_method_hits_by_text|tenant"`
Expected: 看到 `tenant(project_id)=mall-swarm`（之前 LLM 猜错时是 `pms` / `pms-product`）。

- [ ] **Step 4: 截图记录**

把浏览器截图保存到 `/tmp/mall-swarm-verify-after-fix.png`（手动截图工具或浏览器自带）。截图应展示：
- ke_search 卡片 args 不含 project_id
- results 非空带真实 entity_id
- 答案文字包含真实 mall-swarm 方法名

---

## Task 7: 更新 Obsidian 设计文档

**Files:**
- Modify: `/Users/java/obsidian/01 Engineering/knowledge-engineering/代码解读Agent引擎-设计.md`

- [ ] **Step 1: §3.6 末尾追加修复 entry**

在 §3.6 末段（line 165 之后）追加：

```
#### Schema 层缺口已修（2026-05-26）

**新发现并独立修复**：`ke_search` 工具的 input schema 历史上把 `project_id` 列为 LLM 必填字段
（line 33 `required=["query","project_id"]`），由 LLM 自行从问题语境推断。mall-swarm 实测中
LLM 看到 "PmsBrandController" 猜 `project_id="pms"` / `"pms-product"`，导致 Weaviate
`with_tenant("pms")` 查询返回 0 结果。

修复（commit TBD）：`build_ke_search_tool(store, project_id)` 闭包注入 project_id，与
`Neo4jGraphAdapter(neo4j_backend, project_id=project_id)` 设计对齐；schema 去掉 project_id
字段；handler 完全忽略 LLM 输入的 project_id。`build_default_registry` 加必填 `project_id`
参数，`build_tools_for_project` 从 URL path 透传。

仍待处理（§3.6 主体）：CodeEntity / MethodInterpretation collection 写入时未带 tenant
（基础设施级），需重建索引数据。本次 schema 修复**不**触及。
```

替换 commit TBD 为本次 Task 1-4 真实 commit SHA（用 `git log -4 --oneline | head -4` 取）。

- [ ] **Step 2: §13 决策日志追加 entry #8**

在 §13 表末追加：

```
| 8 | ke_search project_id 闭包注入（builder 阶段）| LLM 在 input 里继续传 / adapter 加 project_id 实例字段 | LLM 猜 tenant 实测失败（"pms"）；adapter 改动牵涉多调用方且本次只需修这一个工具 |
```

- [ ] **Step 3: 提交 Obsidian 改动**

Obsidian vault 是否 git track 由用户决定。若 track：
```bash
cd /Users/java/obsidian
git add "01 Engineering/knowledge-engineering/代码解读Agent引擎-设计.md"
git commit -m "docs(ke): §3.6 schema 层 project_id 闭包修复记录 + §13 决策 #8"
```
若不 track 就只保存即可。

---

## Self-Review Checklist

- ✅ Task 1 改 ke_search.py + 测试 → 单文件 closure 注入
- ✅ Task 2 改 build_default_registry signature → 上游 closure
- ✅ Task 3 改 qa_router → URL path 闭包源头打通
- ✅ Task 4 改 system prompt → 删 LLM 误导
- ✅ Task 5 全套回归 → 确保不破坏 624 已通过测试
- ✅ Task 6 端到端 → 真实验证 mall-swarm 查到数据
- ✅ Task 7 设计文档同步 → 宪法要求接口变更同步设计
- ✅ 每 task 都有 TDD 顺序（红→绿→提交）
- ✅ 每 task 都有 exact commit message
- ✅ 没有 placeholder（每 step 都有具体代码 / 命令）
- ✅ Signature 一致：`build_ke_search_tool(store, project_id)` / `build_default_registry(..., project_id, ...)` 全程一致
