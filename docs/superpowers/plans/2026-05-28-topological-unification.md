# 拓扑解读统一化 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 把"业务解读 / 技术解读 / 拓扑解读"三个交织概念收敛成一个——**拓扑解读**。删除独立 BusinessInterpretation 管线（文件 + collection），把 MethodInterpretation 改名 TopologicalInterpretation；CompositeKnowledgeStore 改造为"拓扑解读 → CodeEntity 兜底"。

**Architecture:** 1) Phase A 代码层：删 5 个 BI 文件 + ke_business_interp 工具；rename WeaviateMethodInterpretStore → WeaviateTopologicalInterpretStore + collection 字符串；BusinessStoreProto → InterpretationStoreProto；CompositeKnowledgeStore.business_store kwarg → interpretation_store。2) Phase B 配置 + Pipeline：删 yaml `include_business_interpretation_build`、cli `--with-business-interpretation`、pipeline stage_runtime 里 BI stage 调用。3) Phase C Weaviate 数据迁移：备份 BI petclinic 24 + Method mall-swarm 10 → 创建新 collection + 迁移 mall-swarm 数据 → 删旧 collection。4) Phase D 全套回归 + mall-swarm E2E。5) Phase E Obsidian §10。

**Tech Stack:** Python 3.12 / pytest / Weaviate v4 client / 现有依赖。

**仓库 / 分支:** `/Users/java/knowledge-engineering-auth` 分支 `release-0513`（继续本会话长期分支，无 worktree）。

**Spec 来源:** Obsidian [[拓扑解读统一化-设计]]（已批准，2026-05-28）。

**关键背景**（来自实际探索，无估算）：

- 待删 BI 代码：5 个 src + 1 个 tool + 3 个 tests = ~1890 行
  - `src/knowledge/business_interpretation_runner.py` (386)
  - `src/knowledge/business_interpretation_context.py` (314)
  - `src/knowledge/business_question_lexical_rerank.py` (572)
  - `src/knowledge/interpretation_runner_inputs.py` (32)
  - `src/knowledge/weaviate_business_store.py` (399)
  - `src/service/qa_engine/tools/ke_business_interp.py` (68)
  - `tests/test_business_interpretation_context.py` (6)
  - `tests/test_business_interpretation_runner.py` (58)
  - `tests/test_business_question_lexical_rerank.py` (55)

- Rename 焦点位置：
  - `src/knowledge/weaviate_interpretation_store.py:20` — class `WeaviateMethodInterpretStore` → `WeaviateTopologicalInterpretStore`
  - `src/core/weaviate_defaults.py` — `DEFAULT_COLLECTION_METHOD_INTERPRETATION` → `DEFAULT_COLLECTION_TOPOLOGICAL_INTERPRETATION`
  - `src/config/models.py:75` — class `MethodInterpretationConfig` → `TopologicalInterpretationConfig`
  - `src/service/qa_engine/retriever.py` — class `BusinessStoreProto` → `InterpretationStoreProto`
  - `src/service/qa_engine/adapters.py` — class `WeaviateBusinessAdapter` → `WeaviateTopologicalAdapter`
  - `src/service/api.py` — `app.state.weaviate_business_store` 删除；`app.state.weaviate_method_interp_store` → `app.state.weaviate_interp_store`

- yaml `config/project.yaml`：line 150 `include_method_interpretation_build` + line 151 `include_business_interpretation_build` 合并；line 178 `collection_name: "MethodInterpretation"` → `"TopologicalInterpretation"`；line 186 `collection_name: "BusinessInterpretation"` + line 186-228 整段 `business_interpretation` 配置删除

- cli `src/pipeline/cli.py`：line 34 `--with-interpretation` 保留语义；line 45 `--with-business-interpretation` flag 删除（+ 对应 `--without-business-interpretation`）

- pipeline business stage 引用：
  - `src/pipeline/interpretation_standalone.py`（include_business_interpretation 参数 + run_business_interpretations）
  - `src/pipeline/interpretation_policy.py`（条件判断）
  - `src/pipeline/stage_runtime.py`（ctx.biz_stats + run_business_interpretations 调用）
  - `src/pipeline/full_pipeline_orchestrator.py`（透传）
  - `src/pipeline/commands.py`（参数）

- prompts.py 字眼位置：`src/service/qa_engine/prompts.py:13/136/153/165/171/172`

- Weaviate 实测：
  - `BusinessInterpretation` collection: petclinic tenant 24 条
  - `MethodInterpretation` collection: mall-swarm tenant 10 条（新业务视角 prompt 跑的）
  - `CodeEntity` collection: mall-swarm 7789 条（不动）

**Run tests:** `cd /Users/java/knowledge-engineering-auth && ./venv/bin/python -m pytest tests/test_auth tests/test_knowledge tests/test_structure tests/test_semantic -q`

---

## File Structure

| 文件 | 改动 | 责任 |
|---|---|---|
| 5 个 BI src 文件 | Delete | 业务解读管线全删 |
| 1 个 BI tool (`ke_business_interp.py`) | Delete | 业务解读工具删 |
| 3 个 BI 测试 | Delete | 测试同步删 |
| `src/knowledge/weaviate_interpretation_store.py` | Modify | Method → Topological 类名 + collection 字符串 |
| `src/core/weaviate_defaults.py` | Modify | DEFAULT 常量改名 |
| `src/config/models.py` | Modify | MethodInterpretationConfig → TopologicalInterpretationConfig |
| `src/service/qa_engine/retriever.py` | Modify | BusinessStoreProto → InterpretationStoreProto |
| `src/service/qa_engine/adapters.py` | Modify | WeaviateBusinessAdapter → WeaviateTopologicalAdapter；删 WeaviateBusinessInterpretStore import |
| `src/knowledge/composite_knowledge_store.py` | Modify | 参数 business_store → interpretation_store |
| `src/service/qa_router.py` | Modify | DI 改用新名字；删 BI store 注入 |
| `src/service/api.py` | Modify | startup 删 BI store；rename app.state.weaviate_method_interp_store → weaviate_interp_store |
| `src/service/qa_engine/tools/__init__.py` | Modify | 删 ke_business_interp 注册；改 business_store 参数名 |
| `src/service/qa_engine/tools/ke_search.py` | Modify | store: BusinessStoreProto → InterpretationStoreProto |
| `src/service/qa_engine/tools/ke_method_interp.py` | Modify | 改名 ke_topological_interp.py + 类内引用 |
| `src/service/qa_engine/prompts.py` | Modify | 删 "技术解读" / "业务解读" 字眼 |
| `src/pipeline/cli.py` | Modify | 删 --with-business-interpretation flag |
| `src/pipeline/interpretation_standalone.py` | Modify | 删 run_business_interpretations 调用 |
| `src/pipeline/interpretation_policy.py` | Modify | 删 business 条件分支 |
| `src/pipeline/stage_runtime.py` | Modify | 删 biz_stats + business stage |
| `src/pipeline/full_pipeline_orchestrator.py` | Modify | 删 include_business_interpretation 透传 |
| `src/pipeline/commands.py` | Modify | 删 BI 参数 |
| `config/project.yaml` | Modify | 删 BI 配置块 + 合并 interpretation 开关 |
| 相关测试 | Modify | mock 改名 + 删 BI tests |
| Weaviate `BusinessInterpretation` collection | Delete | 含 petclinic 24 条 |
| Weaviate `MethodInterpretation` collection | Migrate → `TopologicalInterpretation` 后删除 | mall-swarm 10 条迁移 |
| Weaviate `TopologicalInterpretation` collection | Create + import | 新 collection，schema 同原 Method |

---

## Task 1: 删除 BI 相关代码文件 + 测试（不动 Weaviate）

**Files:**
- Delete: `src/knowledge/business_interpretation_runner.py`
- Delete: `src/knowledge/business_interpretation_context.py`
- Delete: `src/knowledge/business_question_lexical_rerank.py`
- Delete: `src/knowledge/interpretation_runner_inputs.py`
- Delete: `src/knowledge/weaviate_business_store.py`
- Delete: `src/service/qa_engine/tools/ke_business_interp.py`
- Delete: `tests/test_business_interpretation_context.py`
- Delete: `tests/test_business_interpretation_runner.py`
- Delete: `tests/test_business_question_lexical_rerank.py`

- [ ] **Step 1: 确认 git status 干净**

```bash
cd /Users/java/knowledge-engineering-auth && git status --short
```

Expected: 空输出（或仅 untracked /tmp/ 文件）。

- [ ] **Step 2: 删 9 个文件**

```bash
cd /Users/java/knowledge-engineering-auth
git rm src/knowledge/business_interpretation_runner.py \
       src/knowledge/business_interpretation_context.py \
       src/knowledge/business_question_lexical_rerank.py \
       src/knowledge/interpretation_runner_inputs.py \
       src/knowledge/weaviate_business_store.py \
       src/service/qa_engine/tools/ke_business_interp.py \
       tests/test_business_interpretation_context.py \
       tests/test_business_interpretation_runner.py \
       tests/test_business_question_lexical_rerank.py
```

- [ ] **Step 3: grep 检查残留 import**

```bash
cd /Users/java/knowledge-engineering-auth && grep -rn "from src.knowledge.business_interpretation\|from src.knowledge.weaviate_business_store\|business_interpretation_runner\|business_interpretation_context\|business_question_lexical_rerank\|interpretation_runner_inputs\|ke_business_interp" src/ tests/ 2>&1 | head -20
```

Expected: 仍能命中若干 import 行（这些是 Task 2-5 要清理的）。**不要在 Task 1 里清理 — 留给后续 task**。本 step 仅打印列表确认范围。

- [ ] **Step 4: 跑测试 expect 大量 ImportError**

```bash
cd /Users/java/knowledge-engineering-auth && ./venv/bin/python -m pytest tests/test_auth tests/test_knowledge --collect-only -q 2>&1 | tail -10
```

Expected: 多处 ImportError（因 import 路径还未清理）。记下错误数量，作为 Task 2-5 修复目标。

- [ ] **Step 5: Commit 删除（半 broken 状态，Task 2-5 修复）**

```bash
cd /Users/java/knowledge-engineering-auth
git commit -m "$(cat <<'EOF'
refactor(knowledge): 删除 BI（业务解读）5 个核心文件 + ke_business_interp 工具 + 测试

设计：[[拓扑解读统一化-设计]] §2 决策 1（业务解读 collection + runner 全删）

删除范围：
- 5 个 src 文件：business_interpretation_runner / context / lexical_rerank /
  interpretation_runner_inputs / weaviate_business_store
- 1 个 tool: ke_business_interp.py
- 3 个测试: test_business_interpretation_* + test_business_question_lexical_*

约 1890 行。注意：本 commit 留下大量 import 残留，Task 2-5 修复（半 broken 状态，
分阶段重构标准做法）。

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>
EOF
)"
```

---

## Task 2: Rename Method → Topological（store / collection / Config / defaults）

**Files:**
- Modify: `src/knowledge/weaviate_interpretation_store.py`（类名 + collection_name 默认值）
- Modify: `src/core/weaviate_defaults.py`（DEFAULT 常量）
- Modify: `src/config/models.py`（MethodInterpretationConfig）

- [ ] **Step 1: 改 `src/knowledge/weaviate_interpretation_store.py`**

```bash
# 读 line 20 周边看类名 + collection_name 默认值
sed -n '15,40p' /Users/java/knowledge-engineering-auth/src/knowledge/weaviate_interpretation_store.py
```

把 `class WeaviateMethodInterpretStore` 改成 `class WeaviateTopologicalInterpretStore`。

把 `DEFAULT_COLLECTION_METHOD_INTERPRETATION` 改成 `DEFAULT_COLLECTION_TOPOLOGICAL_INTERPRETATION`（Task 2 Step 2 修常量）。

如果 collection_name 字符串字面量 `"MethodInterpretation"` 出现在 default 参数，改成 `"TopologicalInterpretation"`。

- [ ] **Step 2: 改 `src/core/weaviate_defaults.py`**

```bash
grep -n "DEFAULT_COLLECTION_METHOD_INTERPRETATION\|MethodInterpretation" /Users/java/knowledge-engineering-auth/src/core/weaviate_defaults.py
```

把常量 `DEFAULT_COLLECTION_METHOD_INTERPRETATION = "MethodInterpretation"` 改成 `DEFAULT_COLLECTION_TOPOLOGICAL_INTERPRETATION = "TopologicalInterpretation"`。

如果原文件有 `DEFAULT_COLLECTION_BUSINESS_INTERPRETATION` 常量，也一并删除。

- [ ] **Step 3: 改 `src/config/models.py:75`**

```bash
sed -n '70,90p' /Users/java/knowledge-engineering-auth/src/config/models.py
```

把 `class MethodInterpretationConfig` 改成 `class TopologicalInterpretationConfig`。如果有 `class BusinessInterpretationConfig` 也删除。

- [ ] **Step 4: grep 找所有 import + 引用**

```bash
cd /Users/java/knowledge-engineering-auth && grep -rn "WeaviateMethodInterpretStore\|MethodInterpretationConfig\|DEFAULT_COLLECTION_METHOD_INTERPRETATION\|DEFAULT_COLLECTION_BUSINESS_INTERPRETATION" src/ tests/ 2>&1 | head -30
```

对每个命中点同步修改类名。用 `sed -i ''` 批量替换：

```bash
cd /Users/java/knowledge-engineering-auth
grep -rln "WeaviateMethodInterpretStore" src/ tests/ | xargs sed -i '' 's/WeaviateMethodInterpretStore/WeaviateTopologicalInterpretStore/g'
grep -rln "MethodInterpretationConfig" src/ tests/ | xargs sed -i '' 's/MethodInterpretationConfig/TopologicalInterpretationConfig/g'
grep -rln "DEFAULT_COLLECTION_METHOD_INTERPRETATION" src/ tests/ | xargs sed -i '' 's/DEFAULT_COLLECTION_METHOD_INTERPRETATION/DEFAULT_COLLECTION_TOPOLOGICAL_INTERPRETATION/g'
```

- [ ] **Step 5: 跑测试看 collection-time 错误数减少**

```bash
cd /Users/java/knowledge-engineering-auth && ./venv/bin/python -m pytest tests/test_auth tests/test_knowledge --collect-only -q 2>&1 | tail -8
```

Expected: 剩余错误减半（BusinessStoreProto + WeaviateBusinessAdapter 还未改）。

- [ ] **Step 6: Commit**

```bash
cd /Users/java/knowledge-engineering-auth
git add -A
git commit -m "$(cat <<'EOF'
refactor(knowledge): WeaviateMethodInterpretStore → WeaviateTopologicalInterpretStore + 配套常量改名

设计：[[拓扑解读统一化-设计]] §4 重命名清单

改动：
- src/knowledge/weaviate_interpretation_store.py: 类名 + collection_name 默认值
- src/core/weaviate_defaults.py: DEFAULT_COLLECTION_METHOD_INTERPRETATION
  → DEFAULT_COLLECTION_TOPOLOGICAL_INTERPRETATION；删 DEFAULT_COLLECTION_BUSINESS_INTERPRETATION
- src/config/models.py: MethodInterpretationConfig → TopologicalInterpretationConfig
- 批量 sed 替换所有 import / 引用

注：collection 字符串 "MethodInterpretation" 在 Weaviate 实际数据迁移在 Task 6 处理；
本 task 仅改代码层，运行时若连旧 collection 会报错（预期，Task 6 修）。

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>
EOF
)"
```

---

## Task 3: Rename Proto + Adapter + Composite 参数

**Files:**
- Modify: `src/service/qa_engine/retriever.py`（BusinessStoreProto → InterpretationStoreProto）
- Modify: `src/service/qa_engine/adapters.py`（WeaviateBusinessAdapter → WeaviateTopologicalAdapter；删 WeaviateBusinessInterpretStore import）
- Modify: `src/knowledge/composite_knowledge_store.py`（参数 business_store → interpretation_store）
- Modify: `src/service/qa_engine/tools/ke_search.py`（store: BusinessStoreProto → InterpretationStoreProto）
- Modify: `src/service/qa_engine/tools/__init__.py`（business_store 参数名 + 删 ke_business_interp 注册）

- [ ] **Step 1: Rename BusinessStoreProto → InterpretationStoreProto**

```bash
cd /Users/java/knowledge-engineering-auth && grep -rln "BusinessStoreProto" src/ tests/ | xargs sed -i '' 's/BusinessStoreProto/InterpretationStoreProto/g'
```

- [ ] **Step 2: Rename WeaviateBusinessAdapter → WeaviateTopologicalAdapter**

```bash
cd /Users/java/knowledge-engineering-auth && grep -rln "WeaviateBusinessAdapter" src/ tests/ | xargs sed -i '' 's/WeaviateBusinessAdapter/WeaviateTopologicalAdapter/g'
```

- [ ] **Step 3: 改 `src/service/qa_engine/adapters.py` 的实现**

打开文件，找到 `class WeaviateTopologicalAdapter`（Step 2 已 rename）。它原来包 `WeaviateBusinessInterpretStore`，现在应包 `WeaviateTopologicalInterpretStore`。

把 `from src.knowledge.weaviate_business_store import WeaviateBusinessInterpretStore` 这一行 import 改成：

```python
from src.knowledge.weaviate_interpretation_store import WeaviateTopologicalInterpretStore
```

把 `WeaviateBusinessInterpretStore` 在该文件内的所有 reference 改成 `WeaviateTopologicalInterpretStore`。

注意：原 Adapter 的 `search_method_hits_by_text` 内部用 `self._store._get_collection().with_tenant(project_id)` + `near_vector_property_hits` 查 BusinessInterpretation collection；现在 collection 是 TopologicalInterpretation，但 schema 字段（entity_id / summary_text / level）一致，**逻辑应能直接复用**。检查 `near_vector_property_hits` 的 collection_name 参数是否硬编码，如有需要改成 `self._store._collection_name`。

- [ ] **Step 4: 改 `src/knowledge/composite_knowledge_store.py` 参数名**

打开文件，把所有 `business_store` 参数名 / 实例变量改成 `interpretation_store`：
- `__init__(*, business_store, code_store, project_id)` → `__init__(*, interpretation_store, code_store, project_id)`
- `self._business_store` → `self._interpretation_store`
- Protocol `_BusinessStoreLike` → `_InterpretationStoreLike`
- 类 docstring 中 `business_store` 描述 → `interpretation_store`

注意：本文件未导入 BusinessStoreProto 类，只是用结构化 Protocol，所以 Step 1 的批量替换不会动它，需要手工改这几处。

- [ ] **Step 5: 改 `src/service/qa_router.py` DI 注入**

```bash
sed -n '95,120p' /Users/java/knowledge-engineering-auth/src/service/qa_router.py
sed -n '155,175p' /Users/java/knowledge-engineering-auth/src/service/qa_router.py
```

找到 `build_retriever_for_project` 和 `build_tools_for_project`：

把：
```python
biz_store = getattr(request.app.state, "weaviate_business_store", None)
...
biz_adapter = WeaviateTopologicalAdapter(biz_store)  # Step 2 已 rename
composite_store = CompositeKnowledgeStore(
    business_store=biz_adapter,    # ← 改这里
    code_store=code_store,
    project_id=project_id,
)
```

改成：
```python
interp_store = getattr(request.app.state, "weaviate_interp_store", None)
...
interp_adapter = WeaviateTopologicalAdapter(interp_store)
composite_store = CompositeKnowledgeStore(
    interpretation_store=interp_adapter,   # ← 改
    code_store=code_store,
    project_id=project_id,
)
```

同样改 `build_tools_for_project` 函数里同样的位置。

- [ ] **Step 6: 改 `src/service/api.py` startup**

```bash
grep -n "weaviate_business_store\|weaviate_method_interp_store\|WeaviateBusinessInterpretStore\|_try_connect_backends" /Users/java/knowledge-engineering-auth/src/service/api.py | head -10
```

找到 `_try_connect_backends`（启动时连后端的函数）：
- 删除连接 `WeaviateBusinessInterpretStore` 那一段代码
- 把 `app.state.weaviate_business_store = ...` 删除
- 把 `app.state.weaviate_method_interp_store = ...` 改成 `app.state.weaviate_interp_store = ...`
- 同时删/改对应的 log 消息（如 "Weaviate BusinessInterpretation store 连接成功"）

- [ ] **Step 7: 改 `src/service/qa_engine/tools/__init__.py`**

```bash
grep -n "business_store\|ke_business_interp" /Users/java/knowledge-engineering-auth/src/service/qa_engine/tools/__init__.py | head -10
```

- 删 `ke_business_interp` 的 import 和注册
- 把 `business_store: BusinessStoreProto` 参数（Step 1 已 rename Proto）改名 `interpretation_store: InterpretationStoreProto`
- 函数体内所有 `business_store` 引用同步改

- [ ] **Step 8: 改 ke_search 工具的 store 参数名**

```bash
grep -n "store:\|business_store" /Users/java/knowledge-engineering-auth/src/service/qa_engine/tools/ke_search.py
```

确认 `def build_ke_search_tool(store: InterpretationStoreProto, project_id: str)` 中的 `store` 是参数名，不用改。description 字符串里如有"BusinessInterpretation"或"业务解读"字眼，改成"拓扑解读 / 解读库"。

- [ ] **Step 9: 跑全套测试**

```bash
cd /Users/java/knowledge-engineering-auth && ./venv/bin/python -m pytest tests/test_auth tests/test_knowledge -q --tb=line 2>&1 | tail -10
```

Expected: 集合时不应 ImportError；个别测试可能因 mock 字段名变化而 fail，Task 4 处理。

- [ ] **Step 10: Commit**

```bash
cd /Users/java/knowledge-engineering-auth
git add -A
git commit -m "$(cat <<'EOF'
refactor(qa-engine): BusinessStoreProto → InterpretationStoreProto + Adapter + Composite 参数

设计：[[拓扑解读统一化-设计]] §4

改动：
- retriever.py: class BusinessStoreProto → InterpretationStoreProto
- adapters.py: WeaviateBusinessAdapter → WeaviateTopologicalAdapter；内部 store 类
  从 WeaviateBusinessInterpretStore 改 WeaviateTopologicalInterpretStore
- composite_knowledge_store.py: 构造参数 business_store → interpretation_store；
  内部字段 _business_store → _interpretation_store；Protocol 名称同步
- qa_router.py: build_retriever / build_tools 注入新名字 + app.state.weaviate_interp_store
- api.py: startup 删除 WeaviateBusinessInterpretStore 连接段；app.state.weaviate_method_interp_store
  rename weaviate_interp_store
- tools/__init__.py: 删 ke_business_interp 注册；business_store 参数 → interpretation_store
- tools/ke_search.py: store description 文字调整

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>
EOF
)"
```

---

## Task 4: 测试调整 + 全套回归通过

**Files:**
- Modify: `tests/test_knowledge/test_composite_knowledge_store.py`（参数名 business_store → interpretation_store + Mock 字段）
- Modify: `tests/test_auth/test_qa_router_tools_injection.py`（如有 BI 相关 mock 删除）
- Modify: `tests/test_auth/test_qa_tools_default_registry.py`（如有 ke_business_interp 引用删除）
- Modify: `tests/test_auth/test_qa_tool_ke_method_interp.py`（如还有 ke_method_interp 相关需调整命名）
- Modify: 其他 BusinessStoreProto / WeaviateBusinessAdapter mock 用法

- [ ] **Step 1: 改 composite 测试**

```bash
grep -n "business_store=\|_business_store\|bi_results\|bi_exc\|business_store)" /Users/java/knowledge-engineering-auth/tests/test_knowledge/test_composite_knowledge_store.py | head -20
```

把所有 `business_store=` 改为 `interpretation_store=`，`_business_store` 改 `_interpretation_store`。 `_make_composite` fixture 的内部参数命名可保留（bi/bi_results 作为短名意义清晰，不强制改）。但传入 CompositeKnowledgeStore 的 kwarg 必须改。

- [ ] **Step 2: 跑 composite 测试 expect PASS**

```bash
cd /Users/java/knowledge-engineering-auth && ./venv/bin/python -m pytest tests/test_knowledge -v 2>&1 | tail -15
```

Expected: 15 PASS（原 15 个全过）。

- [ ] **Step 3: 跑全套测试**

```bash
cd /Users/java/knowledge-engineering-auth && ./venv/bin/python -m pytest tests/test_auth tests/test_knowledge tests/test_structure tests/test_semantic -q --tb=short 2>&1 | tail -10
```

Expected: 大部分 PASS。可能有 ~5-10 个测试因 mock 字段名 / collection_name 字面量需要调整。**对每个 fail 单独修，不要批量改**。

- [ ] **Step 4: Commit**

```bash
cd /Users/java/knowledge-engineering-auth
git add -A
git commit -m "$(cat <<'EOF'
test(refactor): 全套测试适配 BusinessStoreProto → InterpretationStoreProto 改名

改动：
- composite 测试 mock 用 interpretation_store kwarg
- ke_search / ke_method_interp / qa_router 测试调整 mock 字段命名
- 删除 ke_business_interp 相关测试 mock

回归：~720 pass（基线 720 + 删 BI 测试约 -3 + 适配）。

设计：[[拓扑解读统一化-设计]] §4 §5

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>
EOF
)"
```

---

## Task 5: 清理 Pipeline + CLI + yaml + prompts.py

**Files:**
- Modify: `src/pipeline/cli.py:34,45`（合并 interpretation flag，删 BI flag）
- Modify: `src/pipeline/stage_runtime.py`（删 biz_stats + run_business_interpretations 调用）
- Modify: `src/pipeline/interpretation_standalone.py`（删 run_business_interpretations 函数 + 参数）
- Modify: `src/pipeline/interpretation_policy.py`（删 business 条件分支）
- Modify: `src/pipeline/full_pipeline_orchestrator.py`（删 include_business_interpretation 透传）
- Modify: `src/pipeline/commands.py`（删 BI 参数）
- Modify: `config/project.yaml:150,151,178,186-228`（合并 interp 开关 + 删 BI 配置块）
- Modify: `src/service/qa_engine/prompts.py:13,136,153,165,171,172`（删 "技术解读" / "业务解读" 字眼）

- [ ] **Step 1: 改 cli.py 删 BI flag**

```bash
sed -n '30,50p' /Users/java/knowledge-engineering-auth/src/pipeline/cli.py
```

找到 `--with-business-interpretation` 和 `--without-business-interpretation`（约 line 45）。整段 mutually_exclusive_group 删除。如果原 `--with-interpretation`（line 34）描述提到 "method interpretation"，改成 "topological interpretation"。

- [ ] **Step 2: pipeline 删 business stage 调用**

```bash
grep -rn "run_business_interpretations\|include_business_interpretation\|biz_stats\|business_interpretation_runner" /Users/java/knowledge-engineering-auth/src/pipeline/ 2>&1 | head -20
```

对每个命中文件：
- `stage_runtime.py`: 删 `ctx.biz_stats` 赋值段 + `run_business_interpretations` 调用 + 相关 step_callback 文字（"业务解读"等）
- `interpretation_standalone.py`: 删 `run_business_interpretations` 函数定义 + 调用 + `include_business_interpretation` 参数
- `interpretation_policy.py`: 删 business 条件分支
- `full_pipeline_orchestrator.py`: 删 `include_business_interpretation` 透传字段（FullPipelineScope dataclass field + scope 构造调用）
- `commands.py`: 同上

- [ ] **Step 3: yaml 合并 interpretation 开关 + 删 BI 配置块**

```bash
sed -n '145,230p' /Users/java/knowledge-engineering-auth/config/project.yaml
```

修改如下：

- `include_method_interpretation_build: false` 和 `include_business_interpretation_build: false` 合并成：
  ```yaml
  include_topological_interpretation_build: false  # 是否跑拓扑解读
  ```
- line 178 `collection_name: "MethodInterpretation"` 改成 `"TopologicalInterpretation"`
- line 186-228 `business_interpretation` 整段配置删除
- 原 `method_interpretation` 整段 rename 成 `topological_interpretation`

- [ ] **Step 4: prompts.py 删字眼**

```bash
sed -n '10,20p;130,180p' /Users/java/knowledge-engineering-auth/src/service/qa_engine/prompts.py
```

- line 13: "结构化技术解读" → "结构化拓扑解读"
- line 136: 同上
- line 153, 171, 172: "业务解读缺失" → "拓扑解读缺失" 或 "解读缺失"
- line 165: "想看技术解读 → ke_method_interp" → "想看拓扑解读 → ke_method_interp"（工具名 Task 6 可能改）

- [ ] **Step 5: 跑全套回归**

```bash
cd /Users/java/knowledge-engineering-auth && ./venv/bin/python -m pytest tests/test_auth tests/test_knowledge tests/test_structure tests/test_semantic -q --tb=short 2>&1 | tail -8
```

Expected: 全 PASS。

- [ ] **Step 6: Commit**

```bash
cd /Users/java/knowledge-engineering-auth
git add -A
git commit -m "$(cat <<'EOF'
refactor(pipeline): 清理 Pipeline / CLI / yaml / prompts 中 BI（业务解读）残留

设计：[[拓扑解读统一化-设计]] §4 §7C

改动：
- cli.py: 删 --with-business-interpretation / --without-business-interpretation 整段
- pipeline/stage_runtime.py: 删 biz_stats 赋值 + run_business_interpretations 调用 + step 文字
- pipeline/interpretation_standalone.py: 删 run_business_interpretations 函数 + 参数
- pipeline/interpretation_policy.py: 删 business 条件分支
- pipeline/full_pipeline_orchestrator.py: 删 include_business_interpretation 透传 +
  FullPipelineScope 字段
- pipeline/commands.py: 删 BI 参数
- config/project.yaml: 合并 include_method/business_interpretation_build →
  include_topological_interpretation_build；删 business_interpretation 配置块；
  method_interpretation 段 rename topological_interpretation
- prompts.py: 删/改 "技术解读" / "业务解读" 字眼为 "拓扑解读"

回归：~720 pass。

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>
EOF
)"
```

---

## Task 6: Weaviate 数据迁移 — 备份 + 创建新 collection + 迁移 + 删旧

**Files:** (无代码改动，只 Weaviate 数据操作)

- [ ] **Step 1: 备份现有数据到本地 JSON**

```bash
cd /Users/java/knowledge-engineering-auth && ./venv/bin/python << 'PYEOF' 2>&1 | tail -10
import os, json
from pathlib import Path
from dotenv import load_dotenv
load_dotenv(Path('.env.local'), override=False)
import weaviate
from weaviate.auth import Auth

c = weaviate.connect_to_custom(
    http_host='43.228.76.163', http_port=8080, http_secure=False,
    grpc_host='43.228.76.163', grpc_port=50051, grpc_secure=False,
    auth_credentials=Auth.api_key(os.environ['WEAVIATE_API_KEY']),
    skip_init_checks=True,
)
try:
    # 备份 BI petclinic
    bi = c.collections.get('BusinessInterpretation').with_tenant('petclinic')
    bi_objs = []
    for o in bi.iterator(include_vector=True):
        bi_objs.append({
            'uuid': str(o.uuid),
            'properties': dict(o.properties),
            'vector': o.vector.get('default') if isinstance(o.vector, dict) else (o.vector or []),
        })
    Path('/tmp/backup_bi_petclinic.json').write_text(json.dumps(bi_objs, ensure_ascii=False))
    print(f'BI petclinic backup: {len(bi_objs)} 条 → /tmp/backup_bi_petclinic.json')

    # 备份 Method mall-swarm
    mi = c.collections.get('MethodInterpretation').with_tenant('mall-swarm')
    mi_objs = []
    for o in mi.iterator(include_vector=True):
        mi_objs.append({
            'uuid': str(o.uuid),
            'properties': dict(o.properties),
            'vector': o.vector.get('default') if isinstance(o.vector, dict) else (o.vector or []),
        })
    Path('/tmp/backup_method_mall_swarm.json').write_text(json.dumps(mi_objs, ensure_ascii=False))
    print(f'Method mall-swarm backup: {len(mi_objs)} 条 → /tmp/backup_method_mall_swarm.json')
finally:
    c.close()
PYEOF
```

Expected: BI 24 条 + Method 10 条备份成功。

- [ ] **Step 2: 创建 TopologicalInterpretation collection**

启动 uvicorn 让代码自动创建（schema 由 `WeaviateTopologicalInterpretStore._ensure_client_and_schema` 注册）：

```bash
pkill -f "uvicorn src.service.api:app" 2>/dev/null
sleep 2
cd /Users/java/knowledge-engineering-auth && KE_QA_USE_REACT=1 nohup ./venv/bin/uvicorn src.service.api:app --host 127.0.0.1 --port 8000 --reload > /tmp/uvicorn-react.log 2>&1 &
sleep 6
grep "TopologicalInterpretation\|interp_store" /tmp/uvicorn-react.log | tail -5
```

Expected: 启动 log 含 "TopologicalInterpretation 连接成功" 或类似。

- [ ] **Step 3: 从备份恢复 mall-swarm 10 条到新 collection**

```bash
cd /Users/java/knowledge-engineering-auth && ./venv/bin/python << 'PYEOF' 2>&1 | tail -8
import os, json
from pathlib import Path
from dotenv import load_dotenv
load_dotenv(Path('.env.local'), override=False)
import weaviate
from weaviate.auth import Auth

c = weaviate.connect_to_custom(
    http_host='43.228.76.163', http_port=8080, http_secure=False,
    grpc_host='43.228.76.163', grpc_port=50051, grpc_secure=False,
    auth_credentials=Auth.api_key(os.environ['WEAVIATE_API_KEY']),
    skip_init_checks=True,
)
try:
    objs = json.loads(Path('/tmp/backup_method_mall_swarm.json').read_text())
    coll = c.collections.get('TopologicalInterpretation').with_tenant('mall-swarm')
    for o in objs:
        coll.data.insert(
            uuid=o['uuid'],
            properties=o['properties'],
            vector=o['vector'],
        )
    cnt = coll.aggregate.over_all(total_count=True).total_count
    print(f'TopologicalInterpretation mall-swarm: {cnt} 条')
finally:
    c.close()
PYEOF
```

Expected: 输出 `TopologicalInterpretation mall-swarm: 10 条`。

- [ ] **Step 4: 删除 BusinessInterpretation + MethodInterpretation collections**

```bash
cd /Users/java/knowledge-engineering-auth && ./venv/bin/python -c "
import os
from pathlib import Path
from dotenv import load_dotenv
load_dotenv(Path('.env.local'), override=False)
import weaviate
from weaviate.auth import Auth
c = weaviate.connect_to_custom(
    http_host='43.228.76.163', http_port=8080, http_secure=False,
    grpc_host='43.228.76.163', grpc_port=50051, grpc_secure=False,
    auth_credentials=Auth.api_key(os.environ['WEAVIATE_API_KEY']),
    skip_init_checks=True,
)
try:
    for name in ('BusinessInterpretation', 'MethodInterpretation'):
        if c.collections.exists(name):
            c.collections.delete(name)
            print(f'deleted {name}')
        else:
            print(f'{name}: not exists')
    rem = list(c.collections.list_all().keys())
    print(f'remaining: {rem}')
finally:
    c.close()
" 2>&1 | tail -5
```

Expected: 输出 `deleted BusinessInterpretation` + `deleted MethodInterpretation` + remaining 列表含 `TopologicalInterpretation` + `CodeEntity`。

- [ ] **Step 5: 验证 TopologicalInterpretation mall-swarm 数据 OK**

```bash
cd /Users/java/knowledge-engineering-auth && ./venv/bin/python -c "
import os
from pathlib import Path
from dotenv import load_dotenv
load_dotenv(Path('.env.local'), override=False)
import weaviate
from weaviate.auth import Auth
c = weaviate.connect_to_custom(
    http_host='43.228.76.163', http_port=8080, http_secure=False,
    grpc_host='43.228.76.163', grpc_port=50051, grpc_secure=False,
    auth_credentials=Auth.api_key(os.environ['WEAVIATE_API_KEY']),
    skip_init_checks=True,
)
try:
    coll = c.collections.get('TopologicalInterpretation').with_tenant('mall-swarm')
    cnt = coll.aggregate.over_all(total_count=True).total_count
    print(f'mall-swarm: {cnt}')
    # 抽样 1 条
    res = coll.query.fetch_objects(limit=1)
    if res.objects:
        p = res.objects[0].properties
        print(f'sample: method={p.get(\"method_name\")} class={p.get(\"class_name\")} text_len={len(p.get(\"interpretation_text\",\"\"))}')
finally:
    c.close()
" 2>&1 | tail -3
```

Expected: `mall-swarm: 10` + 抽样 sample 有真实数据。

- [ ] **Step 6: 无代码改动 commit，仅记录数据操作**

如果代码层没产生任何改动，无 commit。如果 Step 2 uvicorn 启动暴露了配置 bug 需要修，单独 commit fixup。

---

## Task 7: mall-swarm E2E 验证

**Files:** (无代码改动)

- [ ] **Step 1: 重启 uvicorn 确认走新 store**

```bash
pkill -f "uvicorn src.service.api:app" 2>/dev/null
sleep 2
cd /Users/java/knowledge-engineering-auth && KE_QA_USE_REACT=1 nohup ./venv/bin/uvicorn src.service.api:app --host 127.0.0.1 --port 8000 --reload > /tmp/uvicorn-react.log 2>&1 &
sleep 7
grep "qa_engine ready\|TopologicalInterpretation\|interp_store" /tmp/uvicorn-react.log | tail -5
```

- [ ] **Step 2: 登录 alice**

```bash
TOKEN=$(curl -sS -X POST http://localhost:8000/auth/login -H "Content-Type: application/json" -d '{"username":"alice","password":"test12345"}' | python3 -c "import sys,json; print(json.load(sys.stdin)['access_token'])")
echo "$TOKEN" > /tmp/alice.token
echo "TOKEN len=${#TOKEN}"
```

- [ ] **Step 3: 跑 mall-swarm 问答**

```bash
curl -sS -N -X POST http://localhost:8000/projects/mall-swarm/qa/explain \
  -H "Authorization: Bearer $(cat /tmp/alice.token)" \
  -H "Content-Type: application/json" \
  -d '{"question":"OssServiceImpl.policy 这个方法做什么？依赖关系是什么？"}' \
  --max-time 180 > /tmp/qa-mall-swarm-postref.sse 2>&1

echo "---events---"
grep "^event:" /tmp/qa-mall-swarm-postref.sse | sort | uniq -c
echo "---cited_entities---"
grep "cited_entities" /tmp/qa-mall-swarm-postref.sse | tail -1
```

**验收**：
- ✅ tool_call ≥ 2
- ✅ cited_entities 含 ≥ 1 个真实 mall-swarm entity_id
- ✅ ke_search 命中 ≥ 1（拿到 level="method" 的 TopologicalInterpretation 候选）—— 因为现在 10 条 method 解读已迁移过来
- ✅ 答案有 markdown 形式的内容

- [ ] **Step 4: Commit fixup（如有）**

如果 Step 3 暴露 bug，单独 commit fixup。否则无 commit。

---

## Task 8: Obsidian §10 实施完成标记

**Files:**
- Modify: `/Users/java/obsidian/01 Engineering/knowledge-engineering/拓扑解读统一化-设计.md`（追加 §10）

- [ ] **Step 1: 收集 commit SHA**

```bash
cd /Users/java/knowledge-engineering-auth && git log --oneline release-0513..HEAD | head -10
```

- [ ] **Step 2: 追加 §10 实施完成**

打开 spec 文件，在 `*文档创建：2026-05-28*` 之前追加 §10：

```markdown
---

## §10 实施完成（2026-05-28）

7 个 Task 完成（不含本文档更新），全套回归 ~720 pass，mall-swarm E2E 通过。

### Commits 列表

| Task | Commit | 内容 |
|---|---|---|
| 1 | `<sha1>` | 删 5 BI src + ke_business_interp + 3 BI tests |
| 2 | `<sha2>` | Rename WeaviateMethodInterpretStore → WeaviateTopologicalInterpretStore + 配套 |
| 3 | `<sha3>` | BusinessStoreProto → InterpretationStoreProto + Adapter + Composite 参数 |
| 4 | `<sha4>` | 测试适配 |
| 5 | `<sha5>` | Pipeline + CLI + yaml + prompts 清理 |
| 6 | （Weaviate 操作）| BI 24 条删除；Method 10 条迁移至 TopologicalInterpretation |
| 7 | （E2E 手测）| mall-swarm 验证通过 |

### 实测数据

| 指标 | 数据 |
|---|---|
| 删除代码总行数 | ~1890（5 src + 1 tool + 3 tests） |
| Rename 影响文件数 | ~12（store / adapter / Proto / Config / defaults / composite / qa_router / api / tools / prompts）|
| Weaviate collection 变化 | -2（BI + Method）+1（Topological）|
| mall-swarm TopologicalInterpretation 条数 | 10（迁移完整）|
| 全套测试 pass | ~720（baseline 720 - 3 BI 测试 = 717，与新增/适配测试平衡）|

### follow-up

1. 跑 mall-swarm 全量拓扑解读：`--with-interpretation` 补齐 7000+ 方法
2. 历史 plans / Obsidian 文档里多处 "业务解读 / 技术解读" 字样后续可批量改名（保留历史语境也可）
```

填实际 commit SHA + 测试数。

- [ ] **Step 3: 文档落盘即可（vault 非 git）**

---

## Self-Review

### Spec 覆盖

| Spec §  | Task |
|---|---|
| §1 目标 | Plan Goal |
| §2 决策表 4 条 | Task 1（决策 1, 3）+ Task 2-3（决策 2, 4）|
| §3 架构（改造后） | Task 3 实现 |
| §4 改动文件清单 | File Structure 表 + Task 1-5 详细 step |
| §5 测试策略 | Task 4 |
| §6 风险 + 缓解 | Task 6 备份步骤覆盖 |
| §7 执行顺序 5 phase | Task 1-7 对齐 5 phase |
| §8 决策日志 | Plan Architecture 引用 |
| §9 follow-up | Task 8 §10 引用 |

**全覆盖** ✅。

### Placeholder scan

每个 step 都含真实 shell / python code 或具体修改位置；无 TBD / TODO。Task 8 实测数据待执行后填，属于预期占位。

### Type / signature consistency

- `WeaviateTopologicalInterpretStore` — Task 2 定义，Task 3-7 使用
- `InterpretationStoreProto` — Task 3 Step 1 rename，后续使用
- `WeaviateTopologicalAdapter` — Task 3 Step 2 rename，后续使用
- `CompositeKnowledgeStore(interpretation_store=..., code_store=..., project_id=...)` — Task 3 Step 4 定义，Task 3 Step 5 在 qa_router 使用
- `app.state.weaviate_interp_store` — Task 3 Step 6 定义；Task 3 Step 5 使用
- collection name `TopologicalInterpretation` — Task 2 Step 1（store 默认）+ Task 6 Step 2（自动创建）+ Task 6 Step 3（数据迁移）一致

一致 ✅。

---

## Execution Handoff

Plan 完整 + 落盘 `docs/superpowers/plans/2026-05-28-topological-unification.md`。8 个 Task。

两种执行方式：

**1. Subagent-Driven（推荐）**：每个 Task fresh subagent + 两段式 review，预计完整跑完 ~3-4 小时（含 LLM API rate + Weaviate IO + 测试时间）

**2. Inline Execution**：本 session 直接走 executing-plans，串行跑

哪种？
