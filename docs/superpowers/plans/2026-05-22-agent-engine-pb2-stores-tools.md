# 代码解读 Agent 引擎 — Plan B2：接线 CodeEntity + MethodInterpretation store + 2 工具

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 把 Weaviate CodeEntity（代码片段）+ MethodInterpretation（方法技术解读）两个 store 接入 DI，并加 `ke_read_entity` / `ke_method_interp` 两个只读工具，补齐设计 §3 的 8 工具。

**Architecture:** 两个 store 已在主仓存在（`WeaviateVectorStore` / `WeaviateMethodInterpretStore`），但 app 启动时没连。本计划：① `_try_connect_backends` 增连这两个 store（2-tuple→4-tuple）+ 存 app.state；② `build_default_registry` 加可选 `code_store` / `method_interp_store` 参数（向后兼容：传了才注册对应工具）；③ `build_tools_for_project` 把 app.state 的两个 store 透传；④ 两个工具直接调 store 的 `get_by_entity_id` / `get_by_method_id`（store 返回已是干净 dict，**不套 adapter 层**——无 tenant fallback 逻辑可加，YAGNI）。

**Tech Stack:** Python 3.12 / Weaviate v4 client / pytest（仓库 venv：`./venv/bin/python -m pytest`）。

**设计来源（单一真相）:** Obsidian `[[代码解读Agent引擎-设计]]` §3（ke_read_entity / ke_method_interp）。

**前置:** Plan A/A-cont/B 已落地。

**⚠️ 已知多租户缺口（记入设计，本计划不修）:** CodeEntity 与 MethodInterpretation store 的 `get_by_entity_id` / `get_by_method_id` 是**全局按 entity_id 精确查，无 project_id 隔离**（与已做 tenant 隔离的 graph/business adapter 不同）。canonical_v1 entity_id 跨工程理论可碰撞 → 多租户下可能返回他工程的代码/解读。修复需给这两个 collection 加 tenant 重建数据 → 属 S7 加固，不在本计划。本计划按现状全局查接入，并在设计 §4.2 旁记录此缺口。

**范围边界:** 仅这两个 store 接线 + 两个工具。**不含**：开关上线、SSE、前端（Plan C）；多租户隔离修复（S7）。

**仓库 / 分支:** `/Users/java/knowledge-engineering-auth`，分支 `release-0513`。

**Store 事实（已投研确认）:**
- `WeaviateVectorStore.get_by_entity_id(entity_id) -> Optional[dict]`，返回 `{entity_id, name, entity_type, code_snippet}`（`src/knowledge/vector_store_weaviate.py:235`）
- `WeaviateMethodInterpretStore.get_by_method_id(method_entity_id) -> Optional[dict]`，返回 `{method_entity_id, method_name, signature, interpretation_text, class_entity_id, class_name, language, context_summary, related_entity_ids_json}`（`src/knowledge/weaviate_interpretation_store.py:169`）

---

## Task 1: `ke_read_entity` 工具（读代码片段）

**目标:** 给 entity_id，从 CodeEntity store 取代码片段 + 名称 + 类型。工具直接调一个鸭子类型的 store（有 `.get_by_entity_id`），便于单测注 fake。

**Files:**
- Create: `src/service/qa_engine/tools/ke_read_entity.py`
- Test: `tests/test_auth/test_qa_tool_ke_read_entity.py`（新建）

- [ ] **Step 1: 写失败测试**

```python
# tests/test_auth/test_qa_tool_ke_read_entity.py
"""
ke_read_entity：按 entity_id 从 CodeEntity store 取代码片段（代码片段展示）。
"""
import pytest

from src.service.qa_engine.tools.ke_read_entity import build_ke_read_entity_tool


class _FakeCodeStore:
    """模拟 WeaviateVectorStore.get_by_entity_id。"""
    _data = {
        "method//abc": {
            "entity_id": "method//abc",
            "name": "createOrder",
            "entity_type": "method",
            "code_snippet": "public void createOrder() { ... }",
        }
    }

    def get_by_entity_id(self, entity_id):
        return self._data.get(entity_id)


@pytest.mark.asyncio
async def test_read_entity_returns_code_snippet():
    tool = build_ke_read_entity_tool(_FakeCodeStore())
    out = await tool.handler({"entity_id": "method//abc"})
    assert out["entity_id"] == "method//abc"
    assert out["name"] == "createOrder"
    assert out["entity_type"] == "method"
    assert out["code_snippet"] == "public void createOrder() { ... }"
    assert "error" not in out


@pytest.mark.asyncio
async def test_read_entity_not_found_returns_error():
    tool = build_ke_read_entity_tool(_FakeCodeStore())
    out = await tool.handler({"entity_id": "method//missing"})
    assert out["code_snippet"] is None
    assert "error" in out


@pytest.mark.asyncio
async def test_read_entity_missing_id_returns_error():
    tool = build_ke_read_entity_tool(_FakeCodeStore())
    out = await tool.handler({})
    assert out["entity_id"] is None
    assert "error" in out


@pytest.mark.asyncio
async def test_read_entity_store_exception_returns_error():
    class _BoomStore:
        def get_by_entity_id(self, entity_id):
            raise RuntimeError("weaviate down")

    tool = build_ke_read_entity_tool(_BoomStore())
    out = await tool.handler({"entity_id": "method//abc"})
    assert out["code_snippet"] is None
    assert "error" in out


def test_read_entity_tool_metadata():
    tool = build_ke_read_entity_tool(_FakeCodeStore())
    assert tool.name == "ke_read_entity"
    assert tool.input_schema["required"] == ["entity_id"]
```

- [ ] **Step 2: 跑测试确认失败**

Run: `./venv/bin/python -m pytest tests/test_auth/test_qa_tool_ke_read_entity.py -v`
Expected: FAIL，`ModuleNotFoundError: No module named 'src.service.qa_engine.tools.ke_read_entity'`

- [ ] **Step 3: 实现工具**

Create `src/service/qa_engine/tools/ke_read_entity.py`:

```python
"""ke_read_entity 工具：按 entity_id 读源代码片段（代码片段展示能力）。

从 Weaviate CodeEntity store 取 {name, entity_type, code_snippet}。
鸭子类型注入：store 只需有 get_by_entity_id(entity_id) -> dict | None。

⚠️ 多租户：CodeEntity store 全局按 entity_id 查，无 project_id 隔离（见设计 §4.2 缺口）。
"""
from __future__ import annotations

from typing import Any, Optional, Protocol

from src.service.qa_engine.tools.base import Tool


class _CodeStoreProto(Protocol):
    """ke_read_entity 依赖的最小 store 接口。"""
    def get_by_entity_id(self, entity_id: str) -> Optional[dict[str, Any]]: ...


_KE_READ_ENTITY_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "entity_id": {
            "type": "string",
            "description": "实体 ID，形如 method//xxx 或 class//xxx",
        },
    },
    "required": ["entity_id"],
}


def build_ke_read_entity_tool(code_store: _CodeStoreProto) -> Tool:
    """构造绑定到 CodeEntity store 的 ke_read_entity Tool。"""

    async def handler(input: dict[str, Any]) -> dict[str, Any]:
        entity_id = input.get("entity_id")
        if not entity_id:
            return {
                "entity_id": None,
                "name": None,
                "entity_type": None,
                "code_snippet": None,
                "error": "missing required field: entity_id",
            }
        try:
            record = code_store.get_by_entity_id(entity_id)
        except Exception as e:
            return {
                "entity_id": entity_id,
                "name": None,
                "entity_type": None,
                "code_snippet": None,
                "error": f"code store error: {e}",
            }
        if record is None:
            return {
                "entity_id": entity_id,
                "name": None,
                "entity_type": None,
                "code_snippet": None,
                "error": "entity not found in code store",
            }
        return {
            "entity_id": entity_id,
            "name": record.get("name"),
            "entity_type": record.get("entity_type"),
            "code_snippet": record.get("code_snippet"),
        }

    return Tool(
        name="ke_read_entity",
        description="读取某个代码实体（method/class）的源代码片段、名称与类型。",
        input_schema=_KE_READ_ENTITY_SCHEMA,
        handler=handler,
    )
```

- [ ] **Step 4: 跑测试确认通过**

Run: `./venv/bin/python -m pytest tests/test_auth/test_qa_tool_ke_read_entity.py -v`
Expected: PASS（5 passed）

- [ ] **Step 5: commit**

```bash
git add src/service/qa_engine/tools/ke_read_entity.py tests/test_auth/test_qa_tool_ke_read_entity.py
git commit -m "$(cat <<'EOF'
feat(qa-engine): 新增 ke_read_entity 工具（读代码片段）

agent 引擎 Plan B2：从 Weaviate CodeEntity store 按 entity_id 取 name/type/code_snippet，
鸭子类型注入便于单测。多租户全局查缺口见设计 §4.2。

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>
EOF
)"
```

---

## Task 2: `ke_method_interp` 工具（读方法级技术解读）

**目标:** 给 method entity_id，从 MethodInterpretation store 取技术解读文本。直接调鸭子类型 store（有 `.get_by_method_id`）。

**Files:**
- Create: `src/service/qa_engine/tools/ke_method_interp.py`
- Test: `tests/test_auth/test_qa_tool_ke_method_interp.py`（新建）

- [ ] **Step 1: 写失败测试**

```python
# tests/test_auth/test_qa_tool_ke_method_interp.py
"""
ke_method_interp：按 method entity_id 取方法级技术解读（Mode A）。
"""
import pytest

from src.service.qa_engine.tools.ke_method_interp import build_ke_method_interp_tool


class _FakeInterpStore:
    """模拟 WeaviateMethodInterpretStore.get_by_method_id。"""
    _data = {
        "method//abc": {
            "method_entity_id": "method//abc",
            "method_name": "createOrder",
            "signature": "void createOrder(Order o)",
            "interpretation_text": "创建订单：校验库存后落库并发事件。",
            "class_entity_id": "class//svc",
            "class_name": "OrderService",
            "language": "zh",
            "context_summary": "订单域核心写入",
            "related_entity_ids_json": "[]",
        }
    }

    def get_by_method_id(self, method_entity_id):
        return self._data.get(method_entity_id)


@pytest.mark.asyncio
async def test_method_interp_returns_interpretation():
    tool = build_ke_method_interp_tool(_FakeInterpStore())
    out = await tool.handler({"entity_id": "method//abc"})
    assert out["entity_id"] == "method//abc"
    assert out["interpretation"]["interpretation_text"].startswith("创建订单")
    assert out["interpretation"]["method_name"] == "createOrder"
    assert "error" not in out


@pytest.mark.asyncio
async def test_method_interp_not_found_returns_error():
    tool = build_ke_method_interp_tool(_FakeInterpStore())
    out = await tool.handler({"entity_id": "method//missing"})
    assert out["interpretation"] is None
    assert "error" in out


@pytest.mark.asyncio
async def test_method_interp_missing_id_returns_error():
    tool = build_ke_method_interp_tool(_FakeInterpStore())
    out = await tool.handler({})
    assert out["entity_id"] is None
    assert "error" in out


@pytest.mark.asyncio
async def test_method_interp_store_exception_returns_error():
    class _BoomStore:
        def get_by_method_id(self, method_entity_id):
            raise RuntimeError("weaviate down")

    tool = build_ke_method_interp_tool(_BoomStore())
    out = await tool.handler({"entity_id": "method//abc"})
    assert out["interpretation"] is None
    assert "error" in out


def test_method_interp_tool_metadata():
    tool = build_ke_method_interp_tool(_FakeInterpStore())
    assert tool.name == "ke_method_interp"
    assert tool.input_schema["required"] == ["entity_id"]
```

- [ ] **Step 2: 跑测试确认失败**

Run: `./venv/bin/python -m pytest tests/test_auth/test_qa_tool_ke_method_interp.py -v`
Expected: FAIL，`ModuleNotFoundError`

- [ ] **Step 3: 实现工具**

Create `src/service/qa_engine/tools/ke_method_interp.py`:

```python
"""ke_method_interp 工具：按 method entity_id 读方法级技术解读（Mode A）。

从 Weaviate MethodInterpretation store 取完整解读记录。
鸭子类型注入：store 只需有 get_by_method_id(method_entity_id) -> dict | None。

⚠️ 多租户：store 全局按 method_entity_id 查，无 project_id 隔离（见设计 §4.2 缺口）。
"""
from __future__ import annotations

from typing import Any, Optional, Protocol

from src.service.qa_engine.tools.base import Tool


class _MethodInterpStoreProto(Protocol):
    """ke_method_interp 依赖的最小 store 接口。"""
    def get_by_method_id(self, method_entity_id: str) -> Optional[dict[str, Any]]: ...


_KE_METHOD_INTERP_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "entity_id": {
            "type": "string",
            "description": "方法实体 ID，形如 method//xxx",
        },
    },
    "required": ["entity_id"],
}


def build_ke_method_interp_tool(interp_store: _MethodInterpStoreProto) -> Tool:
    """构造绑定到 MethodInterpretation store 的 ke_method_interp Tool。"""

    async def handler(input: dict[str, Any]) -> dict[str, Any]:
        entity_id = input.get("entity_id")
        if not entity_id:
            return {
                "entity_id": None,
                "interpretation": None,
                "error": "missing required field: entity_id",
            }
        try:
            record = interp_store.get_by_method_id(entity_id)
        except Exception as e:
            return {
                "entity_id": entity_id,
                "interpretation": None,
                "error": f"method interp store error: {e}",
            }
        if record is None:
            return {
                "entity_id": entity_id,
                "interpretation": None,
                "error": "method interpretation not found",
            }
        return {"entity_id": entity_id, "interpretation": record}

    return Tool(
        name="ke_method_interp",
        description="读取某个方法（method//xxx）的技术解读：它做什么、关键逻辑、上下文。",
        input_schema=_KE_METHOD_INTERP_SCHEMA,
        handler=handler,
    )
```

- [ ] **Step 4: 跑测试确认通过**

Run: `./venv/bin/python -m pytest tests/test_auth/test_qa_tool_ke_method_interp.py -v`
Expected: PASS（5 passed）

- [ ] **Step 5: commit**

```bash
git add src/service/qa_engine/tools/ke_method_interp.py tests/test_auth/test_qa_tool_ke_method_interp.py
git commit -m "$(cat <<'EOF'
feat(qa-engine): 新增 ke_method_interp 工具（读方法级技术解读）

agent 引擎 Plan B2：从 Weaviate MethodInterpretation store 按 method_entity_id 取
完整技术解读记录，鸭子类型注入。多租户全局查缺口见设计 §4.2。

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>
EOF
)"
```

---

## Task 3: `build_default_registry` 加可选 store 参数 + 条件注册

**目标:** `build_default_registry` 新增可选 `code_store` / `method_interp_store` 参数（默认 None）；传了才注册 `ke_read_entity` / `ke_method_interp`。向后兼容（现有只传 graph+business_store 的调用 / 测试不破）。

**Files:**
- Modify: `src/service/qa_engine/tools/__init__.py`
- Test: `tests/test_auth/test_qa_tools_default_registry.py`（追加用例）

- [ ] **Step 1: 写失败测试**

在 `tests/test_auth/test_qa_tools_default_registry.py` 末尾追加：

```python
def test_default_registry_registers_optional_stores_when_provided():
    """传入 code_store / method_interp_store 时，注册 ke_read_entity / ke_method_interp。"""
    from src.service.qa_engine.tools import build_default_registry

    class _FakeGraph:
        def successors(self, e, rel_type=None): return []
        def predecessors(self, e, rel_type=None): return []

    class _FakeBiz:
        def get_by_entity(self, entity_id, *, project_id, level=None): return None
        def search_method_hits_by_text(self, *, text, project_id, limit=5): return []

    class _FakeCodeStore:
        def get_by_entity_id(self, entity_id): return None

    class _FakeInterpStore:
        def get_by_method_id(self, method_entity_id): return None

    reg = build_default_registry(
        graph=_FakeGraph(),
        business_store=_FakeBiz(),
        code_store=_FakeCodeStore(),
        method_interp_store=_FakeInterpStore(),
    )
    names = {t.name for t in reg.list_tools()}
    assert "ke_read_entity" in names
    assert "ke_method_interp" in names


def test_default_registry_skips_optional_stores_when_absent():
    """不传 code_store / method_interp_store 时，不注册这两个工具（向后兼容）。"""
    from src.service.qa_engine.tools import build_default_registry

    class _FakeGraph:
        def successors(self, e, rel_type=None): return []
        def predecessors(self, e, rel_type=None): return []

    class _FakeBiz:
        def get_by_entity(self, entity_id, *, project_id, level=None): return None
        def search_method_hits_by_text(self, *, text, project_id, limit=5): return []

    reg = build_default_registry(graph=_FakeGraph(), business_store=_FakeBiz())
    names = {t.name for t in reg.list_tools()}
    assert "ke_read_entity" not in names
    assert "ke_method_interp" not in names
```

- [ ] **Step 2: 跑测试确认失败**

Run: `./venv/bin/python -m pytest tests/test_auth/test_qa_tools_default_registry.py -k "optional" -v`
Expected: FAIL，`TypeError: build_default_registry() got an unexpected keyword argument 'code_store'`

- [ ] **Step 3: 实现**

在 `src/service/qa_engine/tools/__init__.py`：

3a. import 区加：

```python
from src.service.qa_engine.tools.ke_read_entity import build_ke_read_entity_tool
from src.service.qa_engine.tools.ke_method_interp import build_ke_method_interp_tool
```

3b. `__all__` 加 `"build_ke_read_entity_tool"`, `"build_ke_method_interp_tool"`（放在 `"build_ke_impact_tool"` 之后）。

3c. 把 `build_default_registry` 的签名 + body 改为（新增两个可选参数 + 条件注册）：

```python
def build_default_registry(
    *,
    graph: GraphProto,
    business_store: BusinessStoreProto,
    code_store: Any | None = None,
    method_interp_store: Any | None = None,
) -> ToolRegistry:
    """构造预装 ke_* 工具的 ToolRegistry。

    graph + business_store 必填（核心工具）；code_store / method_interp_store 可选，
    传入才注册 ke_read_entity / ke_method_interp（后端未连这两个 store 时优雅缺省）。
    """
    registry = ToolRegistry()
    registry.register(build_ke_search_tool(business_store))
    registry.register(build_ke_business_interp_tool(business_store))
    registry.register(build_ke_callees_tool(graph))
    registry.register(build_ke_callers_tool(graph))
    registry.register(build_ke_table_access_tool(graph))
    registry.register(build_ke_impact_tool(graph))
    if code_store is not None:
        registry.register(build_ke_read_entity_tool(code_store))
    if method_interp_store is not None:
        registry.register(build_ke_method_interp_tool(method_interp_store))
    return registry
```

3d. 文件顶部确认 import 了 `Any`（`from typing import Any`）；若没有则加（检查文件现有 import）。

- [ ] **Step 4: 跑测试确认通过**

Run: `./venv/bin/python -m pytest tests/test_auth/test_qa_tools_default_registry.py -v`
Expected: PASS（含既有 6-工具断言测试——它不传可选 store，仍是 6 个核心工具，不破；+ 2 个新用例）

- [ ] **Step 5: commit**

```bash
git add src/service/qa_engine/tools/__init__.py tests/test_auth/test_qa_tools_default_registry.py
git commit -m "$(cat <<'EOF'
feat(qa-engine): build_default_registry 加可选 code_store/method_interp_store + 条件注册

agent 引擎 Plan B2：传入两个可选 store 才注册 ke_read_entity / ke_method_interp，
向后兼容（不传仍是 6 个核心工具）。后端未连这两 store 时优雅缺省。

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>
EOF
)"
```

---

## Task 4: DI 接线 —— 启动连两个 store + build_tools_for_project 透传

**目标:** `_try_connect_backends` 增连 CodeEntity + MethodInterpretation store（2-tuple→4-tuple），startup 存 app.state；`build_tools_for_project` 把两个 store 透传给 `build_default_registry`。连接失败时这两个 store 为 None（优雅降级，工具不注册）。

**Files:**
- Modify: `src/service/api.py`（`_try_connect_backends` + startup app.state 赋值）
- Modify: `src/service/qa_router.py`（`build_tools_for_project` 透传两个 store）
- Test: `tests/test_auth/test_qa_router_tools_injection.py`（追加 build_tools_for_project 透传用例）

- [ ] **Step 1: 写失败测试**

在 `tests/test_auth/test_qa_router_tools_injection.py` 末尾追加：

```python
def test_build_tools_for_project_passes_optional_stores(monkeypatch):
    """build_tools_for_project 把 app.state 的 code_store / method_interp_store
    透传给 build_default_registry（有则注册对应工具）。"""
    captured = {}

    code_sentinel = object()
    interp_sentinel = object()

    def _fake_build_default_registry(*, graph, business_store, code_store=None, method_interp_store=None):
        captured["code_store"] = code_store
        captured["method_interp_store"] = method_interp_store
        from src.service.qa_engine.tools.base import ToolRegistry
        return ToolRegistry()

    # 伪造 request.app.state：含 4 个后端
    class _State:
        weaviate_business_store = object()
        neo4j_backend = object()
        weaviate_code_store = code_sentinel
        weaviate_method_interp_store = interp_sentinel

    class _App:
        state = _State()

    class _Req:
        app = _App()

    # monkeypatch build_default_registry 捕获透传；adapter 构造换轻量替身（不真连后端）
    # 注意：build_tools_for_project 内部是局部 import
    #   `from src.service.qa_engine.tools import build_default_registry`
    #   `from src.service.qa_engine.adapters import Neo4jGraphAdapter, WeaviateBusinessAdapter`
    # 局部 import 在调用时按模块属性取值 → 必须 patch 这两个**源模块**的属性，
    # patch qa_router 上的同名属性无效（那里没有该绑定）。
    import src.service.qa_engine.tools as _tools_mod
    import src.service.qa_engine.adapters as _adapters
    monkeypatch.setattr(_tools_mod, "build_default_registry", _fake_build_default_registry)
    monkeypatch.setattr(_adapters, "Neo4jGraphAdapter", lambda backend, project_id: object())
    monkeypatch.setattr(_adapters, "WeaviateBusinessAdapter", lambda store: object())

    qa_router.build_tools_for_project("proj-a", _Req())
    assert captured["code_store"] is code_sentinel
    assert captured["method_interp_store"] is interp_sentinel
```

- [ ] **Step 2: 跑测试确认失败**

Run: `./venv/bin/python -m pytest tests/test_auth/test_qa_router_tools_injection.py -k "passes_optional" -v`
Expected: FAIL（`build_tools_for_project` 还没读 / 透传两个 store → captured 里是 None，断言失败）

- [ ] **Step 3a: `build_tools_for_project` 透传两个 store**

把 `src/service/qa_router.py` 的 `build_tools_for_project` body 改为（在取 biz/neo4j 后增取两个可选 store，传给 build_default_registry）：

```python
    biz_store = getattr(request.app.state, "weaviate_business_store", None)
    neo4j_backend = getattr(request.app.state, "neo4j_backend", None)

    if biz_store is None or neo4j_backend is None:
        raise RuntimeError(
            "底层存储资源未就绪（weaviate_business_store / neo4j_backend）"
        )

    # 可选 store（未连时为 None → build_default_registry 不注册对应工具）
    code_store = getattr(request.app.state, "weaviate_code_store", None)
    method_interp_store = getattr(request.app.state, "weaviate_method_interp_store", None)

    from src.service.qa_engine.adapters import Neo4jGraphAdapter, WeaviateBusinessAdapter
    from src.service.qa_engine.tools import build_default_registry

    biz_adapter = WeaviateBusinessAdapter(biz_store)
    graph_adapter = Neo4jGraphAdapter(neo4j_backend, project_id=project_id)

    return build_default_registry(
        graph=graph_adapter,
        business_store=biz_adapter,
        code_store=code_store,
        method_interp_store=method_interp_store,
    )
```

- [ ] **Step 3b: 跑透传测试确认通过**

Run: `./venv/bin/python -m pytest tests/test_auth/test_qa_router_tools_injection.py -k "passes_optional" -v`
Expected: PASS

- [ ] **Step 3c: `_try_connect_backends` 增连两个 store + startup 存 app.state**

在 `src/service/api.py` 的 `_try_connect_backends()`：找到构造 `biz_store = WeaviateBusinessInterpretStore(...)` 之后、`return biz_store, neo4j_backend`（约 line 262）之前。在 Neo4j 连接成功之后、return 之前，增连两个 store（复用同一组 weaviate_url / grpc_port / api_key / dimension 变量；它们在 biz_store 构造处已定义）：

```python
    # ─── 3) CodeEntity + MethodInterpretation store（Plan B2：ke_read_entity / ke_method_interp 用）───
    # 连接失败不致命：返回 None，对应工具优雅不注册（build_default_registry 条件注册）
    code_store = None
    method_interp_store = None
    try:
        from src.knowledge.vector_store_weaviate import WeaviateVectorStore
        code_store = WeaviateVectorStore(
            url=weaviate_url,
            grpc_port=weaviate_grpc_port,
            dimension=weaviate_dimension,
            api_key=weaviate_api_key,
        )
        _log.info("[startup] Weaviate CodeEntity store 连接成功")
    except Exception as e:
        _log.warning("[startup] Weaviate CodeEntity store 连接失败（ke_read_entity 不可用）: %s", e)
    try:
        from src.knowledge.weaviate_interpretation_store import WeaviateMethodInterpretStore
        method_interp_store = WeaviateMethodInterpretStore(
            url=weaviate_url,
            grpc_port=weaviate_grpc_port,
            dimension=weaviate_dimension,
            api_key=weaviate_api_key,
        )
        _log.info("[startup] Weaviate MethodInterpretation store 连接成功")
    except Exception as e:
        _log.warning("[startup] Weaviate MethodInterpretation store 连接失败（ke_method_interp 不可用）: %s", e)

    return biz_store, neo4j_backend, code_store, method_interp_store
```

把原来的 `return biz_store, neo4j_backend` 这一行删除（被上面新的 4-tuple return 取代）。同时把 `_try_connect_backends` 的返回类型注解从 `-> tuple[Any, Any]` 改为 `-> tuple[Any, Any, Any, Any]`。

3d. 更新 startup 里调用 `_try_connect_backends()` 的解包 + app.state 赋值。找到（约 line 142）：

```python
    biz_store, neo4j_backend = _try_connect_backends()
```

改为：

```python
    biz_store, neo4j_backend, code_store, method_interp_store = _try_connect_backends()
```

并在其下 `app.state.weaviate_business_store = biz_store` / `app.state.neo4j_backend = neo4j_backend` 附近（约 line 146-148）增两行：

```python
        app.state.weaviate_code_store = code_store
        app.state.weaviate_method_interp_store = method_interp_store
```

⚠️ 注意：`_try_connect_backends` 有**两处** early `return None, None`（Weaviate 连接失败 / Neo4j 密码缺失）。把这两处也改成 `return None, None, None, None`（保持 4-tuple 形状，否则解包报错）。grep `return None, None` 定位全部并改。

- [ ] **Step 4: import 自检（语法 + 模块可加载）**

`_try_connect_backends` 的真实连接走 startup 打真 Weaviate，单测不覆盖（由你后续一起测验证）。这里只确认改完后两个模块能正常 import、4-tuple 解包语法无误。

Run: `./venv/bin/python -c "import src.service.api as a; import src.service.qa_router as q; print('import OK')"`
Expected: `import OK`（无 SyntaxError / ImportError）

并跑一次 grep 确认没有遗漏的旧 2-tuple return：

Run: `grep -n "return None, None$\|return biz_store, neo4j_backend$" src/service/api.py`
Expected: 无输出（所有 `_try_connect_backends` 的 return 都已是 4-tuple）

- [ ] **Step 5: commit**

```bash
git add src/service/api.py src/service/qa_router.py tests/test_auth/test_qa_router_tools_injection.py
git commit -m "$(cat <<'EOF'
feat(qa-engine): DI 接线 CodeEntity + MethodInterpretation store（ke_read_entity / ke_method_interp）

agent 引擎 Plan B2：_try_connect_backends 增连两个 Weaviate store（2-tuple→4-tuple，
连接失败优雅降级 None），startup 存 app.state，build_tools_for_project 透传给
build_default_registry。两 store 未连时对应工具不注册。

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>
EOF
)"
```

---

## Task 5: 回归 + 设计文档更新（含多租户缺口记录）

- [ ] **Step 1: 跑 qa/tool/registry/router/stream 测试全集**

Run: `./venv/bin/python -m pytest tests/test_auth/ -k "qa or tool or registry or react or router or stream or think or minimax or dashscope or impact or read_entity or method_interp" -q`
Expected: 全 PASS

- [ ] **Step 2: import 自检**

Run: `./venv/bin/python -c "from src.service.qa_engine.tools import build_ke_read_entity_tool, build_ke_method_interp_tool, build_default_registry; import src.service.api; import src.service.qa_router; print('OK')"`
Expected: `OK`

- [ ] **Step 3: 更新 Obsidian 设计文档**

3a. §11 Phase 3 行从 🔵 改 ✅，注明三工具齐（ke_impact + ke_read_entity + ke_method_interp，commit refs）。

3b. §4.2 附近（或新增 §3.6「多租户缺口」小节）记录：CodeEntity / MethodInterpretation store 全局按 entity_id 查、无 project_id 隔离，多租户下 canonical_v1 碰撞可能跨工程返回 → 转交 S7 加固。

---

## Plan B2 完成定义（验收）

1. ✅ `ke_read_entity` 从 CodeEntity store 取代码片段（5 测试），`ke_method_interp` 从 MethodInterpretation store 取解读（5 测试）
2. ✅ `build_default_registry` 可选 store 参数 + 条件注册，向后兼容（不传仍 6 工具）
3. ✅ `_try_connect_backends` 4-tuple 增连两 store + app.state + build_tools_for_project 透传，连接失败优雅降级
4. ✅ 全测试集无回归，import OK
5. ✅ 设计 §11 标 Phase 3 完成 + §多租户缺口记录

## 后续计划（不在本 Plan）

- **Plan C**：Phase 4-6 SSE 事件（thinking/todo/citation）+ 前端组件 + Phase 7 自由格式 + 开关上线（KE_QA_USE_REACT 默认开）+ Phase 8 回归 —— 这步后 agent 才对用户可见可用
- **S7 加固**：CodeEntity / MethodInterpretation store 多租户 tenant 隔离
