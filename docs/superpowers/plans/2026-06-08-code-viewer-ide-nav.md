# 代码查看器 IDE 化导航 实施计划

> **For agentic workers:** REQUIRED SUB-SKILL: superpowers:executing-plans。Steps 用 checkbox 跟踪。Phase-0 已完成（schema 见 spec §五）。

**Goal:** 代码片段 Monaco 查看器 IDE 化：接口→实现跳转、go-to-def 任意符号、hover 悬浮签名+解读、无源码"暂无源码"。

**Architecture:** 核心原语后端 `POST /code/resolve-symbol`（光标位置→实体，三级解析：位置→边命中 / nodes_fts 名字回退 / implements 边接口→impl）+ 前端 Monaco Hover/cmd-click provider。

**Tech Stack:** 后端 FastAPI + CodeGraph sqlite（`/opt/mall-swarm/.codegraph/codegraph.db`：nodes 带 file/line/col/signature，edges 带 kind/line/col，含 implements/references/calls，nodes_fts 全文索引）。前端 React + @monaco-editor/react。设计 [[代码查看器-IDE化导航-设计]]。

**约束:** TDD bite-sized；Python 中文逐行注释；前端 light/dark token；frequent commits；Obsidian 不双写；部署 git bundle + rsync 需授权。**不动** 6段/QA/画图/召回/CallChainFlow/MethodNode。

---

### Task 1: 后端 adapter 解析原语（位置→边 / 名字 fts / 接口→impl）

**Files:**
- Modify: `src/integrations/codegraph/db.py`（+ `edges_at(file_path, line, col)` 位置查边；+ `search_nodes_by_name(token, limit)` 走 nodes_fts；+ `nodes_implementing(iface_qn)` 查 implements 边）
- Modify: `src/integrations/codegraph/graph_adapter.py`（+ `resolve_at_position` / `find_by_name` / `resolve_impl` 暴露给上层；返回 durable_key）
- Modify: `src/integrations/codegraph/graph_factory.py`（NullGraphAdapter 同步加这三个方法返空，满足 GraphProto 降级）
- Test: `tests/test_auth/test_codegraph_resolve_primitives.py`（用现有 mini fixture db 扩展：加带 line/col 的 references 边 + 一对 interface/impl + implements 边）

- [ ] **Step 1: 扩测试夹具** — 在 mini `.codegraph.db` fixture（见 CG-T1.1 `tests/.../conftest` 或夹具构造处，先读）加：①一个方法 node 在 (file=A.java) 内有一条 `references` 边指向 类型 node `Foo`，edge.line/col 已知；②接口 `ISvc::m` + 实现 `SvcImpl::m` + 一条 `implements` 边（SvcImpl→ISvc）。
- [ ] **Step 2: 写失败测试（红）**

```python
def test_edges_at_returns_target_for_position(db):
    # (A.java, line, col) 命中 references 边 → 返回 target durable_key（Foo 类型）
    hit = adapter.resolve_at_position("A.java", LINE, COL)
    assert hit and "Foo" in hit
def test_find_by_name_via_fts(db):
    assert any("Foo" in k for k in adapter.find_by_name("Foo", limit=5))
def test_resolve_impl_iface_method_to_impl(db):
    # 接口方法 ISvc::m → 经 implements 边 → SvcImpl::m
    assert "SvcImpl" in adapter.resolve_impl("ISvc::m#()")
def test_resolve_impl_non_iface_returns_none(db):
    assert adapter.resolve_impl("SvcImpl::m#()") is None
```

- [ ] **Step 3: 跑红** — `pytest tests/test_auth/test_codegraph_resolve_primitives.py -x`
- [ ] **Step 4: 实现 db.py 三方法** —（先读 db.py 现有 `successors_with_locations` SQL 范式照搬）
  - `edges_at(file_path,line,col)`: `SELECT target,kind,line,col FROM edges e JOIN nodes n ON e.source=n.id WHERE n.file_path=? AND e.kind IN ('calls','references','instantiates') ORDER BY abs(e.line-?)+...` 取最近一条（同行优先、col 最近）。
  - `search_nodes_by_name(token,limit)`: `SELECT id,... FROM nodes WHERE id IN (SELECT id FROM nodes_fts WHERE nodes_fts MATCH ?) ...`（FTS 查询转义 token）。CamelCase → 偏好 kind='class'/'interface'。
  - `nodes_implementing(iface_qn)`: 查 `implements` 边 source（实现类）→ 其下同名方法 node。
- [ ] **Step 5: adapter 包装** — `resolve_at_position`/`find_by_name`/`resolve_impl` 调 db + `durable_key(node)`；sqlite 异常 → None/[]（降级，与现有风格一致）。NullGraphAdapter 同名方法返 None/[]。
- [ ] **Step 6: 跑绿 + Commit** — `pytest ... -x` → `git commit -m "feat(codegraph): adapter 加位置/名字/接口-impl 解析原语"`

---

### Task 2: 后端 `resolve_symbol_at` 三级解析编排（纯函数）

**Files:**
- Create: `src/service/qa_engine/symbol_resolver.py`（`resolve_symbol_at(graph, interp_store, *, file_path, line, col, token, context_entity_id) -> dict | None`）
- Test: `tests/test_auth/test_symbol_resolver.py`

- [ ] **Step 1: 写失败测试（红）** —— mock graph（三原语）+ interp_store：
  - 位置命中 → 返 `{entity_id, has_source, kind}`；
  - 位置落空 + token → 走 find_by_name；
  - 命中接口方法 → resolve_impl 改写 entity_id 为 impl；
  - hover 变体：带 `signature`（node）+ `summary`（interp_store 2b 首句）；
  - 全落空 → None；无源码 → `has_source=False`。
- [ ] **Step 2: 跑红**
- [ ] **Step 3: 实现编排** — 三级顺序调原语；`has_source` = resolve_first 命中 file_path 且文件存在；`signature`/`summary` 仅 hover 需要（参数 `want_doc: bool`）。fail-soft 全 try→None。
- [ ] **Step 4: 跑绿 + Commit**

---

### Task 3: 后端 `POST /code/resolve-symbol` 路由

**Files:**
- Modify: `src/service/code_router.py`（+ 路由，复用现有 `require_project_role` 鉴权 + per-request graph adapter，照搬 getCodeSnippet 那条）
- Test: `tests/test_auth/test_code_router_resolve.py`（或扩现有 code_router 测）

- [ ] **Step 1-2: 写红测试** — 200 返解析结果；缺参 422；无命中 200+null。先读 code_router 现有 snippet 路由的依赖注入模式照搬。
- [ ] **Step 3: 实现路由** — 调 `resolve_symbol_at`；注入 graph adapter（resolve_graph_adapter）+ interp store。
- [ ] **Step 4: 跑绿 + Commit**

---

### Task 4: 前端 resolveSymbol API + 类型

**Files:**
- Modify: `src/api/codeSnippets.ts`（+ `resolveSymbol`）；`src/types/codeSnippet.ts`（+ `ResolvedSymbol`）
- Test: `src/api/codeSnippets.test.ts`（扩）

- [ ] **Step 1-4: 红 → 实现 → 绿 → commit** — `resolveSymbol(projectId, {file_path,line,col,token,context_entity_id,want_doc}) → ResolvedSymbol|null`，拼 URL/body 同现有 getCodeSnippet。

---

### Task 5: 前端 Monaco HoverProvider（签名 + 2b解读 + 暂无源码）

**Files:**
- Modify: `src/components/code/MonacoSnippet.tsx`（onMount 注册 `monaco.languages.registerHoverProvider('java', …)`；provider 内取词 → resolveSymbol(want_doc) → markdown tooltip；缓存）
- Test: `src/components/code/MonacoSnippet.test.tsx`（源码不变量：注册 hover provider + 缓存）

- [ ] **Step 1-4: 红 → 实现 → 绿 → commit** — tooltip 渲染 `signature` + `summary`；`has_source=false` 显示"暂无源码"；按 (file,line,col) LRU 缓存。注意 provider 注册一次（不随 snippet 重注册——用 ref 持 dispose）。

---

### Task 6: 前端 cmd/ctrl+click 跳转 + 暂无源码 + 404 文案

**Files:**
- Modify: `src/components/code/MonacoSnippet.tsx`（onMouseDown 检测 cmd/ctrl 修饰键 → resolveSymbol → 有源码 openEntity(带 file_path/line 让现有 reveal 定位) / 无源码 toast）
- Modify: `src/store/codeViewer.ts`（404 文案 "未找到该实体的源码" → "暂无源码"；openEntity 支持按 file+line 定位非整 entity 的位置——若需要）
- Test: 源码不变量 + store 测

- [ ] **Step 1-4: 红 → 实现 → 绿 → commit** — cmd+click 任意符号跳转；复用 callee 单击（保留）；toast 复用现有提示组件或轻量实现。

---

### Task 7: 全量回归
- [ ] 后端 `pytest tests/test_auth/ -q` 全绿；前端 `npx vitest run` 全绿；`tsc` 干净。修任何回归。

---

### Task 8: 部署 + E2E（需授权，不自动）
- [ ] 后端 bundle（`HEAD` 自上次部署）+ rsync 前端 dist（需授权）。
- [ ] mall-swarm E2E 四项：① cmd-click 接口方法 → 跳 Impl；② hover 方法 → 签名+解读；③ 点无源码符号 → "暂无源码"；④ 现有 callee 单击不回归。
- [ ] Obsidian spec 标"已实施"。

## Self-Review
- Spec §九 待办 0-8 全覆盖（0 Phase-0 已做）。
- 类型一致：adapter 三原语返 durable_key（str）/ None；resolve_symbol_at 返 `{entity_id,has_source,kind,signature?,summary?}`；前端 ResolvedSymbol 对齐。
- 占位：db.py/code_router/MonacoSnippet 的精确现有代码在对应 Step 标"先读"——inline 同会话执行时读后改。
