# 召回降噪 + 加权 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) 或 superpowers:executing-plans。Steps 用 checkbox（`- [ ]`）语法。

**Goal:** 在 CodeEntity 召回兜底处 query-time 过滤 MyBatis 样板（Base_Column_List/Example）、降权 getter/Mapper-CRUD、加权 Controller/ServiceImpl，让业务实体浮上来——修评测发现的 Q3/Q5/Q6 召回偏/薄。

**Architecture:** 新增纯函数 `recall_rerank.py`（classify_entity + rerank_and_filter）；`composite_knowledge_store._code_fallback` over-fetch（limit×4）→ rerank_and_filter → 现有 dedup/归一化。返回 dict 的 `score` 保持原始 cosine（加权分只用于排序，不写回），召回门控 top1 仍诚实。不重灌、不重 embed；retriever/门控/sse/qa_router 不动。

**Tech Stack:** Python · pytest。

**设计 spec（已审批）:** `/Users/java/obsidian/01 Engineering/knowledge-engineering/召回降噪加权-设计.md`

**用户偏好:** Python 中文逐行注释；设计文档 Obsidian 不双写仓库。

**探索已确认的事实（实现照此）:**
- entity_id 是 qualified_name，形如 `OmsCartItemController::add#(@RequestBodyOmsCartItemcartItem)` 或 `com.macro.mall.mapper.PmsProductVertifyRecordMapper::Base_Column_List#()`。`split('#',1)[0]` 去参数 → `partition('::')` 得 (qualified_class, method) → qualified_class `rsplit('.',1)[-1]` 得简单类名。
- mall-swarm CodeEntity：15625 method，其中 *Example 类 10739、getter/setter 3086、Mapper 1948、Base_Column_List 76、Controller 246、ServiceImpl 341。
- `composite_knowledge_store._code_fallback`（约 L254-307）当前：`hits = self._code_store.search_by_text(text, top_k=limit, tenant=self._project_id)` → `seen` 去重 → append `{entity_id, summary_text:"", level:"code_entity", score}`。`search_by_text(query, top_k, tenant)` 返回 `list[tuple[entity_id, score]]`，score=1-cos距离。
- 召回门控用 `top1=max(c.get("score",1.0) for c in candidates)`（retriever.py），故 `_code_fallback` 返回的 score 必须仍是原始 cosine。

---

## File Structure

| 文件 | 职责 | 动作 |
|---|---|---|
| `src/knowledge/recall_rerank.py` | classify_entity + rerank_and_filter（纯函数）| Create |
| `src/knowledge/composite_knowledge_store.py` | `_code_fallback` over-fetch + rerank + env 配置 | Modify |
| `tests/test_knowledge/test_recall_rerank.py` | classify/rerank 单测 | Create |
| `tests/test_knowledge/test_composite_knowledge_store.py` | 集成：兜底过滤接线 | Modify（追加）|

---

## Task 1：recall_rerank.py（classify_entity + rerank_and_filter）

**Files:** Create `src/knowledge/recall_rerank.py`；Create `tests/test_knowledge/test_recall_rerank.py`

- [ ] **Step 1: 写失败测试**

```python
# tests/test_knowledge/test_recall_rerank.py
"""召回降噪+加权纯函数单测。设计 [[召回降噪加权-设计]]。"""
from src.knowledge.recall_rerank import classify_entity, rerank_and_filter


def test_classify_drop_boilerplate():
    # Base_Column_List 方法 + Example 类 = 纯样板 → drop
    assert classify_entity("com.macro.mall.mapper.OmsOrderMapper::Base_Column_List#()") == "drop"
    assert classify_entity("com.macro.mall.model.OmsOrderExample::andIdEqualTo#(Longvalue)") == "drop"


def test_classify_demote_lowvalue():
    # getter/setter/is 访问器 + Mapper 的 *ByExample 生成 CRUD → demote
    assert classify_entity("OmsOrder::getStatus#()") == "demote"
    assert classify_entity("OmsOrder::setStatus#(Integerstatus)") == "demote"
    assert classify_entity("OmsOrder::isDeleted#()") == "demote"
    assert classify_entity("com.macro.mall.mapper.OmsOrderMapper::selectByExample#(OmsOrderExampleex)") == "demote"


def test_classify_boost_business():
    # Controller / ServiceImpl / Service → boost
    assert classify_entity("OmsPortalOrderController::generateOrder#(OrderParamp)") == "boost"
    assert classify_entity("OmsPortalOrderServiceImpl::cancelOrder#(Longid)") == "boost"
    assert classify_entity("OmsPortalOrderService::generateOrder#(OrderParamp)") == "boost"


def test_classify_neutral_keeps_real_db_writes():
    # Mapper 的真实操作（非 *ByExample）保持中性，不删不降
    assert classify_entity("com.macro.mall.mapper.OmsOrderMapper::insert#(OmsOrderrow)") == "neutral"
    assert classify_entity("OmsOrderMapper::updateByPrimaryKeySelective#(OmsOrderrow)") == "neutral"
    assert classify_entity("OmsOrderMapper::selectByPrimaryKey#(Longid)") == "neutral"


def test_classify_malformed_is_neutral():
    # 格式意外（无 ::）→ neutral，安全
    assert classify_entity("weirdid") == "neutral"
    assert classify_entity("") == "neutral"


def test_classify_issue_prefix_not_demoted():
    # "issueRefund" 不是 is-访问器（is 后非大写）→ 不应误降为 demote
    assert classify_entity("RefundService::issueRefund#()") == "boost"  # 类名 Service → boost


def test_rerank_drops_boilerplate():
    hits = [
        ("com.macro.mall.mapper.OmsOrderMapper::Base_Column_List#()", 0.70),  # drop
        ("OmsOrderExample::andIdEqualTo#(Longv)", 0.69),                       # drop
        ("OmsPortalOrderServiceImpl::generateOrder#(OrderParamp)", 0.60),      # boost
    ]
    out = rerank_and_filter(hits, limit=5)
    ids = [e for e, _ in out]
    assert "OmsPortalOrderServiceImpl::generateOrder#(OrderParamp)" in ids
    assert all("Base_Column_List" not in e and "Example::" not in e for e in ids)


def test_rerank_boost_outranks_demote_on_close_scores():
    # cosine 略低的 Controller(0.58) 经 +0.05 → 0.63；getter(0.60) 经 -0.05 → 0.55；Controller 应排前
    hits = [
        ("OmsOrder::getStatus#()", 0.60),                              # demote → 0.55
        ("OmsPortalOrderController::generateOrder#(OrderParamp)", 0.58),  # boost → 0.63
    ]
    out = rerank_and_filter(hits, limit=5)
    assert out[0][0] == "OmsPortalOrderController::generateOrder#(OrderParamp)"


def test_rerank_preserves_original_score():
    # 返回的 score 必须是原始 cosine，不是 adj
    hits = [("OmsPortalOrderController::generateOrder#(OrderParamp)", 0.58)]
    out = rerank_and_filter(hits, limit=5)
    assert out[0] == ("OmsPortalOrderController::generateOrder#(OrderParamp)", 0.58)


def test_rerank_truncates_to_limit():
    hits = [(f"X::m{i}#()", 0.5 - i * 0.01) for i in range(10)]  # 全 neutral
    out = rerank_and_filter(hits, limit=3)
    assert len(out) == 3


def test_rerank_empty_after_filter_falls_back_to_original():
    # 全是 drop 类 → 过滤后空 → 回退原始前 limit（绝不比现状差）
    hits = [
        ("com.macro.mall.mapper.AMapper::Base_Column_List#()", 0.40),
        ("BExample::andXEqualTo#(Xv)", 0.39),
    ]
    out = rerank_and_filter(hits, limit=5)
    assert out == hits[:5]  # 回退原始（含原始 score）
```

- [ ] **Step 2: 运行确认失败**

Run: `cd /Users/java/knowledge-engineering-auth && source venv/bin/activate && python -m pytest tests/test_knowledge/test_recall_rerank.py -v`
Expected: FAIL（`ModuleNotFoundError: No module named 'src.knowledge.recall_rerank'`）

- [ ] **Step 3: 实现 recall_rerank.py**

```python
# src/knowledge/recall_rerank.py
"""召回降噪 + 加权（query-time 纯函数）。

设计 [[召回降噪加权-设计]]：CodeEntity 召回里 ~96% 是 MyBatis 样板/getter/Mapper-CRUD，
business 实体仅 ~4%。本模块按 entity_id(qualified_name) 把候选分四类——硬删纯样板、
降权低价值、加权业务、其余中性——在 composite 兜底处过滤+重排，让业务实体浮上来。
"""
from __future__ import annotations  # PEP 563：注解延迟求值

# MyBatisGenerator 生成的"按 Example 条件"的 CRUD 方法名（纯样板，降权）
_GENERATED_BY_EXAMPLE = frozenset({
    "selectByExample", "countByExample", "deleteByExample",
    "updateByExample", "updateByExampleSelective",
})


def _is_accessor(method: str) -> bool:
    """判断是否 getter/setter/is 访问器（camelCase：前缀后紧跟大写字母）。

    `getStatus`/`setStatus`/`isDeleted` 命中；`issueRefund`（is 后是小写 s）不命中——
    避免把业务方法误判成访问器。
    """
    # str.startswith 判前缀；len 与 [idx].isupper() 确保是 camelCase 访问器而非碰巧前缀相同的业务名
    for pre in ("get", "set", "is"):
        n = len(pre)
        if method.startswith(pre) and len(method) > n and method[n].isupper():
            return True
    return False


def classify_entity(entity_id: str) -> str:
    """把一个 entity_id(qualified_name) 分类为 drop|demote|boost|neutral。

    entity_id 形如 'OmsXxxController::add#(params)' 或 'com.x.mapper.YMapper::Base_Column_List#()'。
    解析：split '#' 去参数 → partition '::' 得 (qualified_class, method) → 简单类名取末段。
    """
    head = (entity_id or "").split("#", 1)[0]      # 去掉 '#(params)' 重载签名部分
    if "::" not in head:                            # 格式意外（无类名分隔）→ 中性，安全
        return "neutral"
    qualified_class, _, method = head.partition("::")  # partition 只切第一个 '::'
    simple_class = qualified_class.rsplit(".", 1)[-1]  # 'com.x.YMapper' → 'YMapper'

    # 1) DROP：纯样板，零业务价值，永不返回
    if method == "Base_Column_List" or simple_class.endswith("Example"):
        return "drop"
    # 2) BOOST：业务实体（Controller / ServiceImpl / Service 接口）
    if simple_class.endswith(("Controller", "ServiceImpl", "Service")):
        return "boost"
    # 3) DEMOTE：低价值（访问器 / Mapper 的 *ByExample 生成 CRUD）
    if _is_accessor(method) or method in _GENERATED_BY_EXAMPLE:
        return "demote"
    # 4) 其余中性（含 Mapper insert/insertSelective/updateByPrimaryKeySelective/selectByPrimaryKey 等真实操作）
    return "neutral"


def rerank_and_filter(
    hits: list[tuple[str, float]],
    limit: int,
    *,
    boost: float = 0.05,
    demote: float = 0.05,
) -> list[tuple[str, float]]:
    """过滤纯样板 + 按"调整分"重排，返回前 limit 个（score 保持原始 cosine）。

    Args:
        hits: [(entity_id, 原始cosine分), ...]（通常已 over-fetch 到 limit*N）
        limit: 最终返回上限
        boost/demote: 业务加权 / 低价值降权的分数增量（小幅，语义仍主导）
    Returns:
        [(entity_id, 原始score), ...]，长度 ≤ limit；adj 仅用于排序、不外泄
    """
    # 1) 分类 + 丢掉 drop 类；保留 (eid, 原始score, adj分) 三元组用于排序
    scored: list[tuple[str, float, float]] = []
    for eid, score in hits:
        cat = classify_entity(eid)
        if cat == "drop":                          # 纯样板：直接丢
            continue
        # adj：业务 +boost，低价值 -demote，其余不变；adj 只用于排序
        adj = score + boost if cat == "boost" else (score - demote if cat == "demote" else score)
        scored.append((eid, score, adj))

    # 2) 空兜底：全被丢 → 回退原始 hits 前 limit（绝不比现状差；设计 §6）
    if not scored:
        return list(hits[:limit])

    # 3) 按 adj 降序排序（sorted 是稳定排序，同 adj 保留原顺序）
    scored.sort(key=lambda t: t[2], reverse=True)
    # 4) 取前 limit，返回 (eid, 原始score)——adj 不外泄，保证召回门控 top1 诚实
    return [(eid, score) for (eid, score, _adj) in scored[:limit]]
```

- [ ] **Step 4: 运行确认通过**

Run: `python -m pytest tests/test_knowledge/test_recall_rerank.py -v`
Expected: PASS（全部用例）

- [ ] **Step 5: 提交**

```bash
cd /Users/java/knowledge-engineering-auth
git add src/knowledge/recall_rerank.py tests/test_knowledge/test_recall_rerank.py
git commit -m "feat(retrieval): recall_rerank — classify+filter MyBatis boilerplate, boost business entities

Co-Authored-By: Claude Opus 4.8 (1M context) <noreply@anthropic.com>"
```

---

## Task 2：composite `_code_fallback` 接线（over-fetch + rerank + env 配置）

**Files:** Modify `src/knowledge/composite_knowledge_store.py`；Test `tests/test_knowledge/test_composite_knowledge_store.py`（追加）

- [ ] **Step 1: 写失败测试**

在 `tests/test_knowledge/test_composite_knowledge_store.py` 末尾追加：
```python
def test_code_fallback_filters_boilerplate_and_boosts_business():
    """_code_fallback over-fetch 后过滤样板、business 在前；score 保持原始。"""
    # 假 code_store：top_k 传 limit*4=20 时返回混入样板的列表（business cosine 略低于样板）
    class _FakeCodeStore:
        def __init__(self):
            self.last_top_k = None
        def search_by_text(self, text, top_k, tenant=None):
            self.last_top_k = top_k
            return [
                ("com.macro.mall.mapper.OmsOrderMapper::Base_Column_List#()", 0.70),  # drop
                ("OmsOrderExample::andIdEqualTo#(Longv)", 0.69),                        # drop
                ("OmsOrder::getStatus#()", 0.66),                                       # demote
                ("OmsPortalOrderServiceImpl::generateOrder#(OrderParamp)", 0.62),       # boost
                ("OmsOrderMapper::insert#(OmsOrderrow)", 0.55),                         # neutral
            ]

    class _EmptyInterp:
        def search_method_hits_by_text(self, *, text, project_id, limit=5):
            return []

    from src.knowledge.composite_knowledge_store import CompositeKnowledgeStore
    fake = _FakeCodeStore()
    store = CompositeKnowledgeStore(
        interpretation_store=_EmptyInterp(), code_store=fake, project_id="mall-swarm")
    hits = store.search_method_hits_by_text(text="下单", project_id="mall-swarm", limit=5)
    ids = [h["entity_id"] for h in hits]
    # 样板被过滤
    assert all("Base_Column_List" not in e and "Example::" not in e for e in ids)
    # business 排第一（boost 后超过被 demote 的 getter）
    assert ids[0] == "OmsPortalOrderServiceImpl::generateOrder#(OrderParamp)"
    # over-fetch：top_k 应为 limit*4=20
    assert fake.last_top_k == 20
    # score 仍是原始 cosine
    top = next(h for h in hits if h["entity_id"].endswith("generateOrder#(OrderParamp)"))
    assert top["score"] == 0.62
```

- [ ] **Step 2: 运行确认失败**

Run: `python -m pytest tests/test_knowledge/test_composite_knowledge_store.py::test_code_fallback_filters_boilerplate_and_boosts_business -v`
Expected: FAIL（当前 top_k=limit=5 且不过滤；样板会进结果、ids[0] 不是 business、last_top_k=5）

- [ ] **Step 3: 改 `_code_fallback`**

`src/knowledge/composite_knowledge_store.py`：在文件顶部已有 import 区加：
```python
import os  # 读 env 配置 over-fetch / boost / demote（若文件顶部已 import os 则跳过）
from src.knowledge.recall_rerank import rerank_and_filter  # query-time 降噪+加权
```
把 `_code_fallback` 里取 hits 那段（约 L271-282）：
```python
        # 2. 调 code_store，catch 所有异常实现 fail-soft
        try:
            # WeaviateVectorStore.search_by_text(query_text, top_k, tenant) -> [(eid, score), ...]
            # v2.x: tenant 参数透传 project_id，确保查到当前工程的 multi-tenant 分区
            hits = self._code_store.search_by_text(text, top_k=limit, tenant=self._project_id)
        except Exception as exc:
            # code_store 异常不应该阻断整个查询，记录警告后返空
            _LOG.warning(
                "CodeEntity fallback 失败 (project_id=%s): %s: %s",
                self._project_id, type(exc).__name__, exc,
            )
            return []
```
改为：
```python
        # 2. 调 code_store（over-fetch）+ 降噪重排，catch 所有异常实现 fail-soft
        # 召回降噪+加权（设计 [[召回降噪加权-设计]]）：mall-swarm CodeEntity ~96% 是 MyBatis 样板/
        # getter/Mapper-CRUD，business 仅 ~4%。over-fetch limit*OVERFETCH 后，过滤纯样板、
        # 降权低价值、加权 Controller/ServiceImpl，再截到 limit。
        # 配置走 env，缺省 over-fetch=4 / boost=0.05 / demote=0.05；坏值兜底默认。
        def _num_env(key: str, default: float) -> float:
            try:
                return type(default)(os.getenv(key, str(default)))  # int(...)/float(...) 按 default 类型
            except (ValueError, TypeError):
                return default
        overfetch = int(_num_env("KE_QA_RECALL_OVERFETCH", 4))
        boost = _num_env("KE_QA_RECALL_BOOST", 0.05)
        demote = _num_env("KE_QA_RECALL_DEMOTE", 0.05)
        try:
            # over-fetch：多取 limit*overfetch 条，给降噪重排留出空间
            hits = self._code_store.search_by_text(
                text, top_k=limit * overfetch, tenant=self._project_id
            )
        except Exception as exc:
            # code_store 异常不应该阻断整个查询，记录警告后返空
            _LOG.warning(
                "CodeEntity fallback 失败 (project_id=%s): %s: %s",
                self._project_id, type(exc).__name__, exc,
            )
            return []
        # 过滤纯样板 + 加权业务 + 截到 limit（返回的 score 仍是原始 cosine，门控 top1 诚实）
        hits = rerank_and_filter(hits, limit, boost=boost, demote=demote)
```
（其后的 `# 3. 归一化 + dedup` 整段**保持不变**——它现在作用于过滤+截断后的 hits，dedup/归一化逻辑照旧。）

- [ ] **Step 4: 运行确认通过 + 回归**

Run: `python -m pytest tests/test_knowledge/test_composite_knowledge_store.py -q`
Expected: PASS（新集成测试过 + 既有 composite 测试不破。注意：既有 `test_code_fallback_surfaces_score` 等用 limit 调用、其 mock 返回的都是非样板 entity_id，过滤后保留、score 不变，应仍通过）

- [ ] **Step 5: 全量回归（确认 retriever/门控链不破）**

Run: `python -m pytest tests/ -k "composite or recall or retriever or rerank" -q`
Expected: 全绿

- [ ] **Step 6: 提交**

```bash
cd /Users/java/knowledge-engineering-auth
git add src/knowledge/composite_knowledge_store.py tests/test_knowledge/test_composite_knowledge_store.py
git commit -m "feat(retrieval): wire recall denoise+rerank into _code_fallback (over-fetch + filter + env config)

Co-Authored-By: Claude Opus 4.8 (1M context) <noreply@anthropic.com>"
```

---

## Task 3：部署 + 服务器侧 E2E 复核（⚠️ 需用户授权部署）

> 前置：用户授权部署到蓝队云（`git pull + systemctl restart ke-api`）。本任务**不自动部署**。

- [ ] **Step 1: 推送** `cd /Users/java/knowledge-engineering-auth && git push origin release-0513`
- [ ] **Step 2:（授权后）服务器拉取 + 重启**
  `ssh -p 26666 root@103.47.81.50 'cd /opt/knowledge-engineering && git -c safe.directory=/opt/knowledge-engineering pull --ff-only origin release-0513 && systemctl restart ke-api && sleep 4 && systemctl is-active ke-api'`
- [ ] **Step 3: 服务器侧 E2E 复核**（放仓库根跑避开 /tmp/inspect.py 遮蔽）：对 Q3「退货申请怎么提交和审核处理？」、Q5「后台订单管理 vs 前台下单」、Q6「后台怎么新增和编辑商品？」跑 `retrieve` 打印候选 entity_id；
  - 期望：候选里**无** `Base_Column_List`/`Example`；business（Controller/ServiceImpl）排前；Q3 能召回到退货 create、Q6 能召回到 PmsProductController.create（若仍在 over-fetch 范围内）。
  - 回归：Q1「下单流程」/ Q4「应付金额」候选不退化（business 仍在前）。
- [ ] **Step 4: 回填 Obsidian §9** —— `召回降噪加权-设计.md` §9 记 commit、E2E 候选对比（前后）、部署 commit。不双写仓库。

---

## Self-Review

**1. Spec 覆盖（§2-§8）：** 分类规则（§3）→ Task1 classify_entity ✅；rerank（§4）→ Task1 rerank_and_filter ✅；composite over-fetch+接线（§4）→ Task2 ✅；env 配置（§5）→ Task2 _num_env ✅；score 保持原始（§2.4）→ Task1 test_rerank_preserves_original_score + Task2 断言 ✅；空兜底（§6）→ Task1 test_rerank_empty...falls_back ✅；测试（§7）→ Task1/2 单测+集成 ✅，E2E → Task3 ✅；不动 retriever/门控/sse（§8）→ 仅改 recall_rerank+composite ✅。

**2. 占位符扫描：** Task1/2 给了完整代码 + 完整测试 + 精确 before/after；Task3 E2E 给了具体问题 + 期望（候选无样板、business 在前），endpoint 复用既有服务器侧 retrieve 脚本模式（前几轮已验证）。无 TBD/TODO。

**3. 类型一致性：** `classify_entity(str)->str∈{drop,demote,boost,neutral}` Task1 定义、Task1 rerank + 测试一致；`rerank_and_filter(list[tuple[str,float]], limit, *, boost, demote)->list[tuple[str,float]]` Task1 定义、Task2 调用签名一致（boost/demote kwarg）；composite 返回 dict 仍 `{entity_id, summary_text, level, score}`（score 原始）与下游 retriever `c.get("score",1.0)` 一致。
