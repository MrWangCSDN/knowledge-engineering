# 代码解读 Agent 引擎 — Plan B：agent loop 就绪 + ke_impact 工具

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 让 ReAct agent loop 具备「跑到收敛」的能力（放开 3 轮硬上限到 12 + per-request 工具隔离），并加上影响分析工具 `ke_impact`（多跳调用闭包）—— 这是 agent 真正"动起来"的核心 + 第一个杀手级工具。

**Architecture:** 改 `ReActSynthesizer` 默认 `max_iterations` 3→12（停止条件"无 tool_calls 即 final"已存在，无需改）；qa_router explain 端点按 `project_id` 注入 per-request 工具 registry（修 `TODO(Task 24)` 多租户隔离）；新增 `ke_impact` 工具，用已有的 project_id 隔离 `GraphProto.successors/predecessors` 在 handler 内做 BFS 闭包（零新依赖）。

**Tech Stack:** Python 3.12 / pytest（仓库 venv：`./venv/bin/python -m pytest`）。

**设计来源（单一真相）:** Obsidian `[[代码解读Agent引擎-设计]]` §2（loop 改造）+ §3（ke_impact）。

**前置:** Plan A + A-cont 已落地（模型层双 provider thinking 流式齐活）。

**范围边界 + 为何这样切:** 经投研，Phase 3 另两个工具 `ke_read_entity`（需 Weaviate CodeEntity 代码片段 store）+ `ke_method_interp`（需 Weaviate MethodInterpretation store）依赖当前 app **未连接**的两个 store（`api.py::_try_connect_backends` 只连 business_store + neo4j），需先做 DI 接线 → 拆到后续「Plan B2：工具 store 接线 + 2 工具」。本计划只含零新依赖、零臆造的部分：loop 改造 + `ke_impact`（BFS 走已连的 neo4j 隔离查询）。**不含**：开关上线（KE_QA_USE_REACT 仍默认关，待 Plan C 的 thinking SSE + 自由格式齐了再开）、SSE 事件、前端。

**仓库 / 分支:** `/Users/java/knowledge-engineering-auth`，分支 `release-0513`（逐任务 commit 已授权）。

---

## Task 1: 放开 ReAct 循环上限 3 → 12

**目标:** `ReActSynthesizer` 默认 `max_iterations` 从 3 提到 12（安全阀，防死循环；多跳分析需要更多轮）。停止条件「无 tool_calls 即返回 final」已存在（`react_synthesizer.py:113`），不动。api.py 读的 env 默认也同步。

**Files:**
- Modify: `src/service/qa_engine/react_synthesizer.py:61`（`__init__` 默认值）
- Modify: `src/service/api.py:170`（`KE_QA_REACT_MAX_ITER` env 默认 "3"→"12"）
- Test: `tests/test_auth/test_qa_react_synthesizer.py`（追加一个默认值断言）

- [ ] **Step 1: 写失败测试**

在 `tests/test_auth/test_qa_react_synthesizer.py` 末尾追加：

```python
def test_react_synthesizer_default_max_iterations_is_12():
    """安全阀：默认循环上限 12（放开旧的 3，支撑多跳分析跑到收敛）。"""
    from src.service.qa_engine.react_synthesizer import ReActSynthesizer

    # 构造只需 llm + tool_registry（鸭子类型，传占位即可；本测试不跑循环）
    class _DummyLLM:
        async def complete_with_tools(self, *, messages, tools):
            raise NotImplementedError

    from src.service.qa_engine.tools.base import ToolRegistry

    synth = ReActSynthesizer(llm_provider=_DummyLLM(), tool_registry=ToolRegistry())
    assert synth.max_iterations == 12
```

- [ ] **Step 2: 跑测试确认失败**

Run: `./venv/bin/python -m pytest tests/test_auth/test_qa_react_synthesizer.py::test_react_synthesizer_default_max_iterations_is_12 -v`
Expected: FAIL，`assert 3 == 12`

- [ ] **Step 3: 改默认值**

3a. `src/service/qa_engine/react_synthesizer.py` 第 61 行，`__init__` 签名里：

```python
        max_iterations: int = 3,
```

改为：

```python
        max_iterations: int = 12,
```

3b. 同步把该参数的 docstring（`react_synthesizer.py:66` 附近 `:param max_iterations:`）更新表述（把"3 轮通常够"类描述改为反映 12）。找到：

```python
        :param max_iterations: ReAct 循环最多跑几轮；防止 LLM 死循环调工具
```

其下若有「3 轮通常够」的注释（`self.max_iterations = max_iterations` 上方），更新为：

```python
        # 上限保护：12 轮安全阀，支撑多跳调用链/影响分析跑到收敛；
        # 停止靠"模型给最终答案（无 tool_calls）即 return"，正常远不到 12
        self.max_iterations = max_iterations
```

（若原注释文本不同，按此意修订即可；不要改逻辑，只改默认值 + 注释。）

3c. `src/service/api.py` 第 170 行：

```python
        max_iter = int(os.environ.get("KE_QA_REACT_MAX_ITER", "3"))
```

改为：

```python
        max_iter = int(os.environ.get("KE_QA_REACT_MAX_ITER", "12"))
```

- [ ] **Step 4: 跑测试确认通过**

Run: `./venv/bin/python -m pytest tests/test_auth/test_qa_react_synthesizer.py -v`
Expected: PASS（含新断言 + 既有 ReAct 测试全过）

- [ ] **Step 5: commit**

```bash
git add src/service/qa_engine/react_synthesizer.py src/service/api.py tests/test_auth/test_qa_react_synthesizer.py
git commit -m "$(cat <<'EOF'
feat(qa-engine): ReAct 循环上限 3→12（多跳分析跑到收敛）

agent 引擎 Plan B Phase 2：放开 max_iterations 默认 3→12 安全阀，停止条件
"无 tool_calls 即 final" 已存在不变；api.py KE_QA_REACT_MAX_ITER 默认同步。

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>
EOF
)"
```

---

## Task 2: qa_router explain 端点 per-request 工具 registry 注入（修 Task 24）

**目标:** ReAct 模式下，工具 registry 当前在 api.py startup 用单例绑定（`TODO(Task 24)`，多租户会串项目）。改为 explain 端点按 `project_id` 用已存在的 `build_tools_for_project(project_id, request)` 构造 per-request registry，注入到浅复制的 synthesizer（与现有 per-request `.llm` 切换同模式）。非 ReAct（QASynthesizer 单次 RAG）不需工具，跳过。

**Files:**
- Modify: `src/service/qa_router.py`（新增 helper `_inject_per_request_tool_registry` + 在 explain 端点 `.llm` swap 后调用）
- Test: `tests/test_auth/test_qa_router_tools_injection.py`（新建）

- [ ] **Step 1: 写失败测试**

```python
# tests/test_auth/test_qa_router_tools_injection.py
"""
qa_router._inject_per_request_tool_registry：ReAct synthesizer 按 project_id
注入 per-request 工具 registry（修 Task 24 多租户隔离）；非 ReAct 跳过；失败不抛。
"""
from src.service.qa_engine.tools.base import ToolRegistry
from src.service.qa_engine.react_synthesizer import ReActSynthesizer
from src.service import qa_router


class _DummyLLM:
    async def complete_with_tools(self, *, messages, tools):
        raise NotImplementedError


def test_injects_registry_into_react_synthesizer(monkeypatch):
    sentinel = ToolRegistry()
    # monkeypatch build_tools_for_project 返回 sentinel（不碰真实 Weaviate/Neo4j）
    monkeypatch.setattr(
        qa_router, "build_tools_for_project",
        lambda project_id, request: sentinel,
    )
    synth = ReActSynthesizer(llm_provider=_DummyLLM(), tool_registry=ToolRegistry())
    out = qa_router._inject_per_request_tool_registry(synth, "proj-a", object())
    # registry 被换成 per-request 的 sentinel
    assert out.tool_registry is sentinel


def test_skips_non_react_synthesizer(monkeypatch):
    # 非 ReActSynthesizer（用普通对象模拟 QASynthesizer）→ 原样返回，不调 builder
    called = {"n": 0}

    def _builder(project_id, request):
        called["n"] += 1
        return ToolRegistry()

    monkeypatch.setattr(qa_router, "build_tools_for_project", _builder)

    class _PlainSynth:
        pass

    plain = _PlainSynth()
    out = qa_router._inject_per_request_tool_registry(plain, "proj-a", object())
    assert out is plain
    assert called["n"] == 0  # 非 ReAct 不构造 registry


def test_builder_failure_does_not_raise(monkeypatch):
    # builder 抛错（后端未就绪）→ 不抛，沿用 synthesizer 原 registry
    original = ToolRegistry()

    def _boom(project_id, request):
        raise RuntimeError("backend down")

    monkeypatch.setattr(qa_router, "build_tools_for_project", _boom)
    synth = ReActSynthesizer(llm_provider=_DummyLLM(), tool_registry=original)
    out = qa_router._inject_per_request_tool_registry(synth, "proj-a", object())
    # 失败兜底：registry 仍是原来的，没被清成 None
    assert out.tool_registry is original
```

- [ ] **Step 2: 跑测试确认失败**

Run: `./venv/bin/python -m pytest tests/test_auth/test_qa_router_tools_injection.py -v`
Expected: FAIL，`AttributeError: module 'src.service.qa_router' has no attribute '_inject_per_request_tool_registry'`

- [ ] **Step 3: 实现 helper + 在 explain 调用**

3a. 在 `src/service/qa_router.py` 的 `build_tools_for_project` 函数定义之后，新增 helper：

```python
def _inject_per_request_tool_registry(synthesizer, project_id: str, request: Request):
    """ReAct synthesizer 按 project_id 注入 per-request 工具 registry（修 Task 24）。

    - 非 ReActSynthesizer（如 QASynthesizer 单次 RAG）不需工具 → 原样返回，不构造。
    - build_tools_for_project 失败（后端未就绪）→ 不抛，沿用 synthesizer 已有 registry，
      主流程不挂（与 explain 内其它 per-request 构造的容错语义一致）。

    :return: 同一个 synthesizer 实例（就地改 tool_registry）
    """
    # 局部 import 避免顶部循环依赖
    from src.service.qa_engine.react_synthesizer import ReActSynthesizer
    if not isinstance(synthesizer, ReActSynthesizer):
        return synthesizer
    try:
        synthesizer.tool_registry = build_tools_for_project(project_id, request)
    except Exception as exc:
        _log.warning("explain: per-request 工具注册失败，沿用默认 registry: %r", exc)
    return synthesizer
```

（确认文件顶部已 `import` 了 `Request`（fastapi）与 `_log`；`build_tools_for_project` 在同模块，直接调。）

3b. 在 explain 端点里，找到现有的 per-request `.llm` 切换块（`synthesizer = _copy.copy(synthesizer)` + `synthesizer.llm = chosen_llm`，在 `try/except Exception as exc: _log.warning("explain: 切换模型失败...")` 内）。在那个 `try` 块的 `synthesizer.llm = chosen_llm` 之后、`except` 之前，加一行调用：

```python
        synthesizer = _copy.copy(synthesizer)
        synthesizer.llm = chosen_llm
        # per-request 工具 registry（修 Task 24）：ReAct 模式按 project_id 隔离注入
        synthesizer = _inject_per_request_tool_registry(synthesizer, project_id, request)
```

（只加这一行 `_inject_per_request_tool_registry(...)`；不动该块其它行。）

- [ ] **Step 4: 跑测试确认通过**

Run: `./venv/bin/python -m pytest tests/test_auth/test_qa_router_tools_injection.py -v`
Expected: PASS（3 passed）

- [ ] **Step 5: commit**

```bash
git add src/service/qa_router.py tests/test_auth/test_qa_router_tools_injection.py
git commit -m "$(cat <<'EOF'
feat(qa-engine): explain 端点 per-request 工具 registry 注入（修 Task 24 多租户隔离）

agent 引擎 Plan B Phase 2：ReAct synthesizer 的工具 registry 改为按 project_id
per-request 构造（build_tools_for_project），不再用 startup 单例（会串项目）。
非 ReAct 跳过；builder 失败不抛沿用默认。新增 helper + explain 调用点一行接入。

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>
EOF
)"
```

---

## Task 3: 新增 `ke_impact` 工具（多跳影响闭包，BFS）

**目标:** 影响分析杀手级工具——给一个 entity_id，沿调用关系做多跳 BFS 闭包，返回可达实体集。`direction="down"` 用 `successors`（它调了谁，影响下游）；`direction="up"` 用 `predecessors`（谁调了它，受影响方）。用已有 project_id 隔离的 `GraphProto` 在 handler 内 BFS，零新依赖。

**Files:**
- Create: `src/service/qa_engine/tools/ke_impact.py`
- Modify: `src/service/qa_engine/tools/__init__.py`（import + 注册 + `__all__`）
- Test: `tests/test_auth/test_qa_tool_ke_impact.py`（新建）

- [ ] **Step 1: 写失败测试**

```python
# tests/test_auth/test_qa_tool_ke_impact.py
"""
ke_impact：多跳影响闭包（BFS over GraphProto.successors/predecessors）。
direction down=下游闭包（影响谁），up=上游闭包（被谁影响）。
"""
import pytest

from src.service.qa_engine.tools.ke_impact import build_ke_impact_tool


class _FakeGraph:
    """简单有向图：A->B->C，A->D。successors/predecessors 同 GraphProto。"""
    _edges = {"A": ["B", "D"], "B": ["C"], "C": [], "D": []}

    def successors(self, entity_id, rel_type=None):
        return list(self._edges.get(entity_id, []))

    def predecessors(self, entity_id, rel_type=None):
        # 反向边
        return [src for src, dsts in self._edges.items() if entity_id in dsts]


@pytest.mark.asyncio
async def test_impact_down_closure():
    tool = build_ke_impact_tool(_FakeGraph())
    out = await tool.handler({"entity_id": "A", "direction": "down"})
    # A 的下游闭包：B, C, D（不含起点 A 自身）
    assert set(out["nodes"]) == {"B", "C", "D"}
    assert out["count"] == 3
    assert out["direction"] == "down"
    assert out["entity_id"] == "A"


@pytest.mark.asyncio
async def test_impact_up_closure():
    tool = build_ke_impact_tool(_FakeGraph())
    out = await tool.handler({"entity_id": "C", "direction": "up"})
    # 谁能到达 C：B（B->C）、A（A->B->C）
    assert set(out["nodes"]) == {"A", "B"}
    assert out["count"] == 2


@pytest.mark.asyncio
async def test_impact_max_depth_limits_bfs():
    tool = build_ke_impact_tool(_FakeGraph())
    # depth=1：A 只到直接下游 B, D（不含 2 跳的 C）
    out = await tool.handler({"entity_id": "A", "direction": "down", "max_depth": 1})
    assert set(out["nodes"]) == {"B", "D"}


@pytest.mark.asyncio
async def test_impact_missing_entity_id_returns_error():
    tool = build_ke_impact_tool(_FakeGraph())
    out = await tool.handler({"direction": "down"})
    assert out["nodes"] == []
    assert "error" in out


@pytest.mark.asyncio
async def test_impact_invalid_direction_defaults_down():
    tool = build_ke_impact_tool(_FakeGraph())
    # 非法 direction → 兜底当 down
    out = await tool.handler({"entity_id": "A", "direction": "sideways"})
    assert set(out["nodes"]) == {"B", "C", "D"}
    assert out["direction"] == "down"


def test_impact_tool_metadata():
    tool = build_ke_impact_tool(_FakeGraph())
    assert tool.name == "ke_impact"
    assert "entity_id" in tool.input_schema["properties"]
    assert tool.input_schema["required"] == ["entity_id"]
```

- [ ] **Step 2: 跑测试确认失败**

Run: `./venv/bin/python -m pytest tests/test_auth/test_qa_tool_ke_impact.py -v`
Expected: FAIL，`ModuleNotFoundError: No module named 'src.service.qa_engine.tools.ke_impact'`

- [ ] **Step 3: 实现 `ke_impact` 工具**

Create `src/service/qa_engine/tools/ke_impact.py`:

```python
"""ke_impact 工具：多跳影响闭包（BFS over GraphProto）。

影响分析杀手级能力——代码知识库能做、通用 LLM 做不到。
direction='down'：沿 successors（它调用谁）做闭包 = 改这个实体会影响哪些下游。
direction='up'  ：沿 predecessors（谁调用它）做闭包 = 哪些上游依赖这个实体。

复用已有 project_id 隔离的 GraphProto.successors/predecessors，在 handler 内 BFS，
不引新后端依赖。
"""
from __future__ import annotations

from collections import deque
from typing import Any

from src.service.qa_engine.retriever import GraphProto
from src.service.qa_engine.tools.base import Tool

# 默认 / 上限：防超大图把闭包跑爆
_DEFAULT_MAX_DEPTH = 5
_MAX_DEPTH_CAP = 20
_DEFAULT_MAX_NODES = 200

_KE_IMPACT_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "entity_id": {
            "type": "string",
            "description": "起始实体 ID，形如 method//xxx 或 class//xxx",
        },
        "direction": {
            "type": "string",
            "description": "down=下游影响闭包（改它影响谁）；up=上游依赖闭包（谁依赖它）",
            "enum": ["down", "up"],
            "default": "down",
        },
        "max_depth": {
            "type": "integer",
            "description": "BFS 最大跳数",
            "default": _DEFAULT_MAX_DEPTH,
            "minimum": 1,
            "maximum": _MAX_DEPTH_CAP,
        },
    },
    "required": ["entity_id"],
}


def build_ke_impact_tool(graph: GraphProto) -> Tool:
    """构造绑定到指定 GraphProto 的 ke_impact Tool。"""

    async def handler(input: dict[str, Any]) -> dict[str, Any]:
        entity_id = input.get("entity_id")
        if not entity_id:
            return {
                "entity_id": None,
                "direction": "down",
                "count": 0,
                "nodes": [],
                "error": "missing required field: entity_id",
            }

        # direction 兜底：非 'up' 一律当 'down'
        direction = input.get("direction")
        if direction != "up":
            direction = "down"

        # max_depth 容错（LLM 可能传 string）+ 夹到 [1, cap]
        try:
            max_depth = int(input.get("max_depth", _DEFAULT_MAX_DEPTH))
        except (TypeError, ValueError):
            max_depth = _DEFAULT_MAX_DEPTH
        max_depth = max(1, min(max_depth, _MAX_DEPTH_CAP))

        # 选邻居函数：down=successors / up=predecessors
        neighbors = graph.successors if direction == "down" else graph.predecessors

        # BFS 闭包：visited 不含起点；按 max_depth / max_nodes 双重封顶
        try:
            visited: set[str] = set()
            # 队列元素 = (node, depth)；起点 depth 0
            queue: deque[tuple[str, int]] = deque([(entity_id, 0)])
            seen = {entity_id}
            while queue:
                node, depth = queue.popleft()
                if depth >= max_depth:
                    continue
                for nxt in neighbors(node):
                    if nxt in seen:
                        continue
                    seen.add(nxt)
                    visited.add(nxt)
                    if len(visited) >= _DEFAULT_MAX_NODES:
                        break
                    queue.append((nxt, depth + 1))
                if len(visited) >= _DEFAULT_MAX_NODES:
                    break
        except Exception as e:
            return {
                "entity_id": entity_id,
                "direction": direction,
                "count": 0,
                "nodes": [],
                "error": f"graph backend error: {e}",
            }

        nodes = sorted(visited)
        return {
            "entity_id": entity_id,
            "direction": direction,
            "count": len(nodes),
            "nodes": nodes,
        }

    return Tool(
        name="ke_impact",
        description=(
            "影响分析：给一个代码实体（method/class），沿调用关系做多跳 BFS 闭包。"
            "direction=down 求下游影响面（改它会波及谁）；up 求上游依赖面（谁依赖它）。"
        ),
        input_schema=_KE_IMPACT_SCHEMA,
        handler=handler,
    )
```

- [ ] **Step 4: 跑测试确认通过**

Run: `./venv/bin/python -m pytest tests/test_auth/test_qa_tool_ke_impact.py -v`
Expected: PASS（6 passed）

- [ ] **Step 5: 注册到 build_default_registry**

修改 `src/service/qa_engine/tools/__init__.py`：

5a. import 区（其它 `build_ke_*` import 旁）加：

```python
from src.service.qa_engine.tools.ke_impact import build_ke_impact_tool
```

5b. `__all__` 列表里加 `"build_ke_impact_tool"`（放在 `"build_ke_table_access_tool"` 之后）。

5c. `build_default_registry` 函数体里，`registry.register(build_ke_callers_tool(graph))` 之后加一行：

```python
    registry.register(build_ke_impact_tool(graph))
```

- [ ] **Step 6: 跑工具包测试确认注册无误**

Run: `./venv/bin/python -m pytest tests/test_auth/ -k "tool or registry or ke_impact" -v`
Expected: PASS（含既有工具测试 + ke_impact；若有断言"registry 工具数量"的既有测试因新增工具失败，更新其期望数字 +1，并在 commit message 注明）

- [ ] **Step 7: commit**

```bash
git add src/service/qa_engine/tools/ke_impact.py src/service/qa_engine/tools/__init__.py tests/test_auth/test_qa_tool_ke_impact.py
git commit -m "$(cat <<'EOF'
feat(qa-engine): 新增 ke_impact 工具（多跳影响闭包 BFS）

agent 引擎 Plan B Phase 3：影响分析杀手级工具——给 entity_id 沿 successors(down)/
predecessors(up) 做 BFS 闭包，复用 project_id 隔离的 GraphProto，零新后端依赖。
注册进 build_default_registry（per-request 工具集 +1）。

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>
EOF
)"
```

---

## Task 4: 回归 + 设计文档进度更新

- [ ] **Step 1: 跑 qa/tool/react/registry/stream 测试全集**

Run: `./venv/bin/python -m pytest tests/test_auth/ -k "qa or tool or react or registry or stream or think or minimax or dashscope or impact" -q`
Expected: 全 PASS

- [ ] **Step 2: import 自检**

Run: `./venv/bin/python -c "from src.service.qa_engine.tools import build_ke_impact_tool, build_default_registry; from src.service.qa_engine.react_synthesizer import ReActSynthesizer; from src.service.qa_router import _inject_per_request_tool_registry; print('OK')"`
Expected: 打印 `OK`

- [ ] **Step 3: 更新 Obsidian 设计文档 §11**

把 `[[代码解读Agent引擎-设计]]` §11：Phase 2 标 ✅（cap 12 + per-request registry，commit refs）；Phase 3 标 🔵（ke_impact 完成；ke_read_entity + ke_method_interp 待 Plan B2 工具 store 接线）。

---

## Plan B 完成定义（验收）

1. ✅ `ReActSynthesizer` 默认 `max_iterations == 12`，停止条件不变
2. ✅ explain 端点 ReAct 模式按 project_id 注入 per-request 工具 registry，非 ReAct 跳过，失败不抛
3. ✅ `ke_impact` BFS 闭包工具（down/up + max_depth），注册进默认 registry，6 测试过
4. ✅ qa/tool/react/stream 测试全过，无回归

## 后续计划（不在本 Plan）

- **Plan B2**：DI 接线 Weaviate CodeEntity store + MethodInterpretation store → `ke_read_entity`（attrs via Neo4j + code via CodeEntity store）+ `ke_method_interp`（new MethodInterpretationAdapter）
- **Plan C**：Phase 4-6 SSE 事件（thinking/todo/citation）+ 前端组件 + Phase 7 自由格式 + 开关上线（KE_QA_USE_REACT 默认开）+ Phase 8 回归
