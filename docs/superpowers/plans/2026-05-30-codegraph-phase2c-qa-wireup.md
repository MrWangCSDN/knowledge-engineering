# CodeGraph Phase 2c — 检索器接通 + 端到端中文 QA 验证 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans. Steps use checkbox (`- [ ]`) 语法。

**Goal:** 让完整中文 QA 一圈端到端跑通——中文问题 → 检索器命中(qualified_name) → CodeGraph 图导航(callers/callees) → LLM 中文作答；扫清半迁移期残留（老解读 shadow + prompt 里的 canonical_v1 格式示例）。

**Architecture:** Phase 1（图导航走 CodeGraph）+ 2a（CodeEntity 已 qualified_name）已就绪。2c 只做 3 件小事：① 清掉 mall-swarm 残留的老 TopologicalInterpretation（10 条 canonical_v1），让 CompositeKnowledgeStore 干净兜底到 CodeEntity（qualified_name）；② 改 ReAct prompt 里写死的 canonical_v1 格式示例 → qualified_name；③ 起栈跑端到端中文 QA 验证。

**Tech Stack:** Python · Weaviate(v4 client) · DashScope · pytest。

**设计 spec（已审批）:** `/Users/java/obsidian/01 Engineering/knowledge-engineering/CodeGraph-结构引擎集成-设计.md`。本计划只覆盖 **2c**（不含 2b 解读重生）。

**用户偏好:** Python 代码中文逐行注释。

**探索已确认的事实（实现照此）:**
- `QARetriever.retrieve`（src/service/qa_engine/retriever.py:148-176）：对 top-N candidates 取 `entity_id` → `_bfs_chain` → `graph.successors/predecessors`。**entity_id 必须是 qualified_name** 才喂得对 CodeGraphGraphAdapter。
- `CompositeKnowledgeStore.search_method_hits_by_text`（src/knowledge/composite_knowledge_store.py:143-181）：先查 interpretation_store(TopologicalInterpretation)，**空/异常才兜底 CodeEntity**。
- mall-swarm 的 `TopologicalInterpretation` 租户**有 10 条老 canonical_v1 记录** → 命中会 shadow CodeEntity、返回 canonical_v1 → 喂 CodeGraph 图导航对不上 → callees 空。
- `react_synthesizer.py:394 / 396-397` 的 prompt 写死了 canonical_v1 格式示例（`method//abc123` / `class//def456` / `method://xxx`）。
- Weaviate 实际在 `43.228.76.163:8080`（.env.local），project.yaml 写的是 localhost:8080。

**⚠️ E2E 前置（2c.3 需用户起）:** MySQL(:3307 隧道，app 查 Project.repo_local_path 用，**当前 down**) + Weaviate(✓已通) + DashScope(✓) + mall-swarm codegraph.db(✓) + KE app(--reload 在跑)。

---

## File Structure

| 文件 | 职责 | 动作 |
|---|---|---|
| （数据操作，无代码文件） | 清 mall-swarm TopologicalInterpretation 租户 | 运行 + 验证 |
| `src/service/qa_engine/react_synthesizer.py` | prompt 的 entity_id 格式示例 canonical_v1 → qualified_name | Modify |
| `tests/test_auth/test_react_prompt_entity_id.py` | 断言 prompt 用 qualified_name 形态、无 canonical_v1 示例 | Create |

---

## Task 2c.1：清 mall-swarm 的老 TopologicalInterpretation（让 composite 干净兜底 CodeEntity）

> 这 10 条是早先 canonical_v1 解读，已被 CodeEntity(qualified_name) 取代；留着会 shadow 检索、喂错 key 给图。2b（解读重生）以后再灌。属数据清理，非 TDD 代码。

- [x] **Step 1: 清空 mall-swarm 的 TopologicalInterpretation 租户**

```bash
cd /Users/java/knowledge-engineering-auth && source venv/bin/activate
export WEAVIATE_API_KEY="$(grep -E '^WEAVIATE_API_KEY=' .env.local | cut -d= -f2-)"
python - <<'PY'
import os, weaviate
from weaviate.classes.init import Auth
from weaviate.classes.tenants import Tenant
c = weaviate.connect_to_custom(http_host="43.228.76.163", http_port=8080, http_secure=False,
    grpc_host="43.228.76.163", grpc_port=50051, grpc_secure=False,
    auth_credentials=Auth.api_key(os.environ["WEAVIATE_API_KEY"]))
coll = c.collections.get("TopologicalInterpretation")
before = coll.with_tenant("mall-swarm").aggregate.over_all(total_count=True).total_count
print("清空前 mall-swarm 解读数:", before)
coll.tenants.remove(["mall-swarm"])                 # 删租户(连同 10 条数据)
coll.tenants.create([Tenant(name="mall-swarm")])    # 重建空租户(ACTIVE)
after = coll.with_tenant("mall-swarm").aggregate.over_all(total_count=True).total_count
print("清空后:", after, "✅" if after == 0 else "⚠️")
c.close()
PY
```
Expected: `清空前 10 → 清空后 0`。

- [x] **Step 2: 记录** —— 在设计文档 §12 标注「mall-swarm TopologicalInterpretation 已清空，待 2b 重生」。无 commit（纯数据操作）。

---

## Task 2c.2：改 ReAct prompt 的 entity_id 格式示例（canonical_v1 → qualified_name）

**Files:** Modify `src/service/qa_engine/react_synthesizer.py`（约 L393-397）；Test `tests/test_auth/test_react_prompt_entity_id.py`

- [x] **Step 1: 写失败测试**

```python
# tests/test_auth/test_react_prompt_entity_id.py
"""验证 ReAct system prompt 的 entity_id 格式指引已切到 qualified_name，不再误导 LLM 用 canonical_v1。"""
import inspect
import src.service.qa_engine.react_synthesizer as rs


def test_prompt_uses_qualified_name_not_canonical_v1():
    # 取 react_synthesizer 整个模块源码（prompt 是模块内的 f-string 模板）
    src = inspect.getsource(rs)
    # 不该再出现误导性的 canonical_v1 形态示例
    assert "method//abc123" not in src, "prompt 还在用 canonical_v1 示例 method//abc123"
    assert "class//def456" not in src, "prompt 还在用 canonical_v1 示例 class//def456"
    # 应出现 qualified_name 形态的说明（含 '::' 和 '#(' 的示例）
    assert "::" in src and "#(" in src, "prompt 应给出 qualified_name 形态示例(Class::method#(params))"
```

- [x] **Step 2: 运行确认失败**

Run: `cd /Users/java/knowledge-engineering-auth && python -m pytest tests/test_auth/test_react_prompt_entity_id.py -v`
Expected: FAIL（源码里还有 `method//abc123`）

- [x] **Step 3: 改 prompt（精确 before/after）**

在 `src/service/qa_engine/react_synthesizer.py` 把这两段（约 L393-397）：
```
1. **优先用【可用 context】里已经检索好的 candidates**。我（系统）在你看到的 prompt 里已经做过初步语义检索，
   candidates 里的 entity_id 是**真实存在**的；别自己编 `method://xxx` / `class://xxx`，那样工具会查不到。

2. **entity_id 要照搬**。candidates 里给你的形如 `method//abc123` 或 `class//def456`，
   `://`/`//` 全部保留原样，**不要**把它改成 `method://xxx_imagined` 之类。
```
改为：
```
1. **优先用【可用 context】里已经检索好的 candidates**。我（系统）在你看到的 prompt 里已经做过初步语义检索，
   candidates 里的 entity_id 是**真实存在**的；别自己编不存在的符号，那样工具会查不到。

2. **entity_id 要照搬**。candidates 里给你的形如 `OmsPortalOrderServiceImpl::generateOrder#(OrderParam)`
   （`类名::方法名#(参数)` 形态，可能含注解/泛型文本），**原样照抄**、一个字符都别改。
```

- [x] **Step 4: 运行确认通过**

Run: `python -m pytest tests/test_auth/test_react_prompt_entity_id.py -v`
Expected: PASS

- [x] **Step 5: 排查其它残留的 canonical_v1 示例并一并改**

Run: `grep -rnE "method//|class//|field//|method://|class://" src/service/qa_engine/ src/service/ | grep -iv test`
- 对每处「教 LLM / 给用户看」的 canonical_v1 格式示例，改成 qualified_name 形态（与 Step 3 同风格）。纯内部代码里用到的不用动。改完重跑上面的测试 + `grep` 确认 prompt 类文本已无 canonical_v1 示例。

- [x] **Step 6: 提交**

```bash
cd /Users/java/knowledge-engineering-auth
git add src/service/qa_engine/react_synthesizer.py tests/test_auth/test_react_prompt_entity_id.py
git commit -m "fix(qa): ReAct prompt entity_id examples canonical_v1 -> qualified_name"
```

---

## Task 2c.3：端到端中文 QA 验证（⚠️ 需用户起 MySQL 隧道）

> 前置：用户起 **MySQL :3307 隧道**（app 查 Project.repo_local_path 用）；Weaviate(43.228.76.163)/DashScope/codegraph.db 已就绪；KE app 在跑（--reload）。

- [x] **Step 1: 确认 app 连的是对的 Weaviate**

`project.yaml` 的 `weaviate_url` 是 `localhost:8080`，但实际 Weaviate 在 `43.228.76.163:8080`。确认 app 启动时用的是 **`.env.local` 的 `WEAVIATE_URL`(43.228.76.163)** 还是 project.yaml：
```bash
cd /Users/java/knowledge-engineering-auth
grep -rnE "WEAVIATE_URL|weaviate_url|weaviate_interp_store|connect" src/service/api.py src/service/*startup* 2>/dev/null | head
```
- 若 app 用 .env.local 的 WEAVIATE_URL → 已对，跳过。
- 若 app 读 project.yaml 的 localhost → 把 project.yaml `vectordb-code`/`vectordb-interpret` 的 `weaviate_url` 改成 `http://43.228.76.163:8080`（与 .env.local 一致），重启 app。

- [x] **Step 2: 起栈后，问一个走 callees 的中文问题**

```bash
# MySQL 隧道起好后；通过 KE API 发问（替换为真实 endpoint/鉴权方式）
curl -s -X POST http://localhost:8000/api/qa/<project>/ask \
  -H "Authorization: Bearer <token>" -H "Content-Type: application/json" \
  -d '{"question":"下单流程是怎么实现的？generateOrder 调用了哪些方法？"}' | tail -40
```
（确切 endpoint/鉴权见 `src/service/qa_router.py` 路由定义；也可用前端 chat 页面发问。）

- [x] **Step 3: 验证整条链**

确认：
- 答案里的 callees/callers 是 **mall-swarm 真实方法**（来自 CodeGraph）；
- 引用/candidates 的 entity_id 是 **qualified_name 形态**（`Class::method#()`），无 `method//`/`field//`；
- 日志**无 Neo4j 图查询、无 "对不上/空 callees"**；
- 答案中文、合理。

- [x] **Step 4: 记录** —— E2E 结果（问题、callees 是否真实、是否纯 qualified_name）记到设计文档 §12 实施完成标记；至此 Phase 2「中文 QA 接通 CodeGraph」打通（解读质量待 2b）。

---

## Self-Review

**1. Spec 覆盖（§10 Phase 2c）：** 检索器接通（清老解读让 composite 兜底 CodeEntity qualified_name，2c.1）✅；端到端中文 QA 验证（2c.3）✅；附带修 prompt 的 canonical_v1 误导（2c.2，探索新发现）✅。2b 解读重生明确不在本计划。

**2. 占位符扫描：** 2c.2 Step5「排查其它残留」给了具体 grep + 处理原则（改示例类、留内部用），非占位；2c.3 的 endpoint 标注"见 qa_router 路由"——这是真实的待查（路由鉴权细节因 app 而异），给了定位方法。其余有完整命令/代码。

**3. 类型一致性：** 不新增类型；改的是 prompt 文本 + 数据清理 + E2E。测试断言基于模块源码字符串，稳定。

**已知依赖：** 2c.1 需 Weaviate(✓)；2c.2 纯本地；2c.3 需 MySQL :3307 隧道(用户起) + 全栈。
