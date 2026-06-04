# 召回二次语义重排（gte-rerank + 门控式护栏）Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 在召回里加一层语义二次重排（DashScope gte-rerank），门控式护栏只在 cosine 不自信时重排候选顺序，提升自然提问的 recall@1，不伤 cosine 已对的简单题、不动召回门控。

**Architecture:** 新增纯模块 `semantic_rerank.py`（`should_rerank` 门控判定 + `rerank_candidates` 调 gte-rerank 重排 + 失败降级）。retriever 的 architecture 分支在算完 `recall_score`（原始 cosine top1）后、设 `entry_candidates` 前接 2 行：不自信才 rerank。门控/score/composite 全不动（解耦）。

**Tech Stack:** Python 3.11 / FastAPI / pytest / requests；DashScope `gte-rerank-v2` HTTP（复用 embedding 的 Bearer-key 模式）。

设计 spec：`/Users/java/obsidian/01 Engineering/knowledge-engineering/召回二次重排-设计.md`
验证：`/Users/java/obsidian/01 Engineering/knowledge-engineering/召回二次重排-验证与决策.md`

---

## File Structure

| 文件 | 职责 | 改动 |
|---|---|---|
| `src/service/qa_engine/semantic_rerank.py` | 语义重排（门控判定 + gte-rerank 调用 + 降级）| **新建** |
| `src/service/qa_engine/retriever.py` | 召回上下文装配 | architecture 分支接 2 行 + import |
| `tests/test_auth/test_semantic_rerank.py` | semantic_rerank 单测 | **新建** |
| `tests/test_auth/test_qa_retriever.py` | retriever 单测 | 追加 2 例（rerank 接线 + 门控解耦）|

**不动**：composite_knowledge_store / 召回门控逻辑与阈值(0.45) / recall_rerank.py / sse_emitter / qa_router / 前端。

---

## Task 1: `should_rerank` 门控式护栏（纯函数）

**Files:**
- Create: `src/service/qa_engine/semantic_rerank.py`
- Test: `tests/test_auth/test_semantic_rerank.py`

- [ ] **Step 1: 写失败测试**

创建 `tests/test_auth/test_semantic_rerank.py`：

```python
"""语义二次重排单测（[[召回二次重排-设计]]）。"""
from src.service.qa_engine.semantic_rerank import should_rerank


def test_should_rerank_confident_top1_skips():
    """top1 高(0.72)且与 top2 拉开(gap 0.12) → cosine 自信 → 不 rerank。"""
    assert should_rerank([{"score": 0.72}, {"score": 0.60}]) is False


def test_should_rerank_low_top1():
    """top1 低(0.5 < 0.6) → 不确定 → rerank。"""
    assert should_rerank([{"score": 0.5}, {"score": 0.3}]) is True


def test_should_rerank_small_margin():
    """top1 高(0.70)但与 top2 太近(gap 0.02 < 0.05) → 谁第一不明确 → rerank。"""
    assert should_rerank([{"score": 0.70}, {"score": 0.68}]) is True


def test_should_rerank_too_few_candidates():
    """候选 < 2 → 无可重排 → False。"""
    assert should_rerank([{"score": 0.5}]) is False
    assert should_rerank([]) is False
```

- [ ] **Step 2: 跑测试确认失败**

Run: `./venv/bin/python -m pytest tests/test_auth/test_semantic_rerank.py -v`
Expected: FAIL（`ModuleNotFoundError: semantic_rerank`）

- [ ] **Step 3: 实现 should_rerank（新建文件）**

创建 `src/service/qa_engine/semantic_rerank.py`：

```python
"""召回二次语义重排（gte-rerank + 门控式护栏）。

设计 [[召回二次重排-设计]]：cosine 不自信时才调 DashScope gte-rerank 重排候选顺序，
吃自然提问 recall@1 翻倍的上行、不伤 cosine 已对的简单题（门控式护栏）。门控（architecture/
chit-chat）解耦：本模块只改候选顺序，不碰 score、不参与门控。
"""
from __future__ import annotations  # 注解延迟求值

import logging  # 标准库：降级时记 warning
import os       # 读 env 阈值/开关/API key

_log = logging.getLogger(__name__)

# 门控式护栏阈值（设计 §4）：判 cosine 是否"自信"
_DEFAULT_CONFIDENT_TOP1 = 0.6   # top1 cosine ≥ 此值才算 top1 够强
_DEFAULT_MARGIN = 0.05          # top1 - top2 ≥ 此值才算与 top2 拉开


def _env_float(key: str, default: float) -> float:
    """读 env 浮点阈值；缺失 / 非法 → default。"""
    raw = os.environ.get(key)
    if not raw:
        return default
    try:
        return float(raw)
    except ValueError:
        return default


def should_rerank(candidates: list[dict]) -> bool:
    """门控式护栏：cosine 不自信时才需要 rerank（候选已按 cosine score 降序）。

    规则（设计 §4）：
      - 候选 < 2 → False（无可重排）
      - top1 < CONFIDENT_TOP1 → True（top1 本身不高，不确定）
      - top1 - top2 < MARGIN → True（top1/top2 接近，谁第一不明确）
      - 否则 → False（top1 高且拉开，信 cosine，避翻车 + 省调用）
    """
    if len(candidates) < 2:
        return False
    confident = _env_float("KE_RERANK_CONFIDENT_TOP1", _DEFAULT_CONFIDENT_TOP1)
    margin = _env_float("KE_RERANK_MARGIN", _DEFAULT_MARGIN)
    # .get("score", 0.0)：缺分数（理论上不会，adapter 必填）→ 0.0 → 视为不自信偏保守
    top1 = candidates[0].get("score", 0.0)
    top2 = candidates[1].get("score", 0.0)
    if top1 < confident:
        return True
    if (top1 - top2) < margin:
        return True
    return False
```

- [ ] **Step 4: 跑测试确认通过**

Run: `./venv/bin/python -m pytest tests/test_auth/test_semantic_rerank.py -v`
Expected: PASS（4 例）

- [ ] **Step 5: 提交**

```bash
git add src/service/qa_engine/semantic_rerank.py tests/test_auth/test_semantic_rerank.py
git commit -m "feat(rerank): semantic_rerank.should_rerank 门控式护栏判定"
```

---

## Task 2: `_gte_rerank` HTTP + `rerank_candidates`（调用 + 降级）

**Files:**
- Modify: `src/service/qa_engine/semantic_rerank.py`
- Test: `tests/test_auth/test_semantic_rerank.py`

- [ ] **Step 1: 写失败测试**

追加到 `tests/test_auth/test_semantic_rerank.py`：

```python
import src.service.qa_engine.semantic_rerank as sr


def test_rerank_candidates_reorders_by_gte(monkeypatch):
    """env 开 + mock gte-rerank 返回新顺序 → 候选按之重排；score 字段不变。"""
    monkeypatch.setenv("KE_RECALL_RERANK", "1")
    monkeypatch.setenv("DASHSCOPE_API_KEY", "k")
    monkeypatch.setattr(sr, "_gte_rerank", lambda q, d, k: [2, 0, 1])
    cands = [
        {"entity_id": "A", "summary_text": "a", "score": 0.6},
        {"entity_id": "B", "summary_text": "b", "score": 0.55},
        {"entity_id": "C", "summary_text": "c", "score": 0.5},
    ]
    out = sr.rerank_candidates("问题", cands)
    assert [c["entity_id"] for c in out] == ["C", "A", "B"]
    # score 字段不被改写（门控/前端显示用原始 cosine）
    by_id = {c["entity_id"]: c["score"] for c in out}
    assert by_id == {"A": 0.6, "B": 0.55, "C": 0.5}


def test_rerank_candidates_fallback_on_error(monkeypatch):
    """gte-rerank 抛异常 → 回退原 cosine 序，不抛。"""
    monkeypatch.setenv("KE_RECALL_RERANK", "1")
    monkeypatch.setenv("DASHSCOPE_API_KEY", "k")

    def boom(*a, **k):
        raise RuntimeError("http down")

    monkeypatch.setattr(sr, "_gte_rerank", boom)
    cands = [{"entity_id": "A", "score": 0.6}, {"entity_id": "B", "score": 0.5}]
    assert sr.rerank_candidates("q", cands) == cands


def test_rerank_candidates_env_off(monkeypatch):
    """KE_RECALL_RERANK=0 → 直接原序。"""
    monkeypatch.setenv("KE_RECALL_RERANK", "0")
    cands = [{"entity_id": "A", "score": 0.6}, {"entity_id": "B", "score": 0.5}]
    assert sr.rerank_candidates("q", cands) == cands


def test_rerank_candidates_no_api_key(monkeypatch):
    """无 DASHSCOPE_API_KEY → 原序（不调用）。"""
    monkeypatch.setenv("KE_RECALL_RERANK", "1")
    monkeypatch.delenv("DASHSCOPE_API_KEY", raising=False)
    cands = [{"entity_id": "A", "score": 0.6}, {"entity_id": "B", "score": 0.5}]
    assert sr.rerank_candidates("q", cands) == cands


def test_rerank_candidates_too_few(monkeypatch):
    """候选 < 2 → 原序。"""
    monkeypatch.setenv("KE_RECALL_RERANK", "1")
    monkeypatch.setenv("DASHSCOPE_API_KEY", "k")
    one = [{"entity_id": "A", "score": 0.6}]
    assert sr.rerank_candidates("q", one) == one
```

- [ ] **Step 2: 跑测试确认失败**

Run: `./venv/bin/python -m pytest tests/test_auth/test_semantic_rerank.py -k "rerank_candidates" -v`
Expected: FAIL（`_gte_rerank` / `rerank_candidates` 未定义）

- [ ] **Step 3: 实现 `_gte_rerank` + `rerank_candidates`**

追加到 `src/service/qa_engine/semantic_rerank.py`（文件末尾，import 处加 `import requests`）：

在文件顶部 import 段补 `import requests`（HTTP 调用），然后追加：

```python
# DashScope gte-rerank（cross-encoder）HTTP 端点 + 模型（与 embedding 同账号 key）
_RERANK_URL = "https://dashscope.aliyuncs.com/api/v1/services/rerank/text-rerank/text-rerank"
_RERANK_MODEL = "gte-rerank-v2"
_RERANK_TIMEOUT = 5  # 秒；超时即降级回退 cosine 序，不拖慢召回


def _gte_rerank(query: str, documents: list[str], api_key: str) -> list[int]:
    """调 DashScope gte-rerank，返回按相关度降序的"原始 index"列表。

    Args:
        query: 用户原始问题（cross-encoder 吃完整意图）
        documents: 候选文本列表（与候选同序，index 一一对应）
        api_key: DASHSCOPE_API_KEY
    Returns:
        原始 index 的相关度降序列表（如 [3,0,5,...]）；调用方据此重排候选。
    Raises:
        requests / 解析异常透传（由 rerank_candidates 捕获降级）。
    """
    payload = {
        "model": _RERANK_MODEL,
        "input": {"query": query, "documents": documents},
        # return_documents=False：只要排序不要回传文本；top_n 全量
        "parameters": {"return_documents": False, "top_n": len(documents)},
    }
    resp = requests.post(
        _RERANK_URL,
        headers={"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"},
        json=payload,
        timeout=_RERANK_TIMEOUT,
    )
    resp.raise_for_status()  # 非 2xx 抛 → 上层降级
    # output.results 已按 relevance_score 降序；每项 {"index": i, "relevance_score": s}
    results = resp.json()["output"]["results"]
    return [r["index"] for r in results]


def _rerank_enabled() -> bool:
    """env 开关 KE_RECALL_RERANK：默认 on；"0"/"false"/"off"/"no" 关。"""
    raw = (os.environ.get("KE_RECALL_RERANK") or "").strip().lower()
    return raw not in ("0", "false", "off", "no")


def rerank_candidates(question: str, candidates: list[dict]) -> list[dict]:
    """用 gte-rerank 重排候选顺序（best-effort，绝不抛、绝不改 score 字段）。

    降级条件（任一 → 原样返回 cosine 序）：env 关 / 候选<2 / 无 API key / 任何异常。
    """
    if not _rerank_enabled():
        return candidates
    if len(candidates) < 2:
        return candidates
    api_key = os.environ.get("DASHSCOPE_API_KEY")
    if not api_key:
        return candidates
    # 候选配对文本：2b 解读优先，空则 entity_id；截 ≤500 控 payload
    docs = [(c.get("summary_text") or c.get("entity_id") or "")[:500] for c in candidates]
    try:
        order = _gte_rerank(question, docs, api_key)
    except Exception as e:
        # 网络 / 超时 / 返回格式异常都降级回退原序，绝不阻断召回
        _log.warning("gte-rerank 失败，回退 cosine 序: %s", e)
        return candidates
    # 按相关度降序 index 重排；防御越界/重复/缺失 index（漏掉的候选按原序补末尾）
    seen: set[int] = set()
    reranked: list[dict] = []
    for i in order:
        if isinstance(i, int) and 0 <= i < len(candidates) and i not in seen:
            seen.add(i)
            reranked.append(candidates[i])
    for i, c in enumerate(candidates):
        if i not in seen:
            reranked.append(c)
    return reranked
```

- [ ] **Step 4: 跑测试确认通过**

Run: `./venv/bin/python -m pytest tests/test_auth/test_semantic_rerank.py -v`
Expected: PASS（9 例）

- [ ] **Step 5: 提交**

```bash
git add src/service/qa_engine/semantic_rerank.py tests/test_auth/test_semantic_rerank.py
git commit -m "feat(rerank): gte-rerank HTTP + rerank_candidates（env开关+失败降级）"
```

---

## Task 3: retriever 接线（门控式 rerank，门控解耦）

**Files:**
- Modify: `src/service/qa_engine/retriever.py`（import 段 + architecture 分支 L156-157 前）
- Test: `tests/test_auth/test_qa_retriever.py`

- [ ] **Step 1: 写失败测试**

追加到 `tests/test_auth/test_qa_retriever.py` 末尾：

```python
# ───────── 召回二次语义重排接线（门控解耦）─────────


@pytest.mark.asyncio
async def test_retrieve_applies_rerank_but_keeps_recall_score(monkeypatch):
    """architecture 分支：rerank 重排 entry_candidates 顺序，但 recall_score 仍=原始 cosine top1。"""
    import src.service.qa_engine.retriever as rmod
    bs = MagicMock()
    bs.search_method_hits_by_text.return_value = [
        {"entity_id": "M1", "level": "method", "summary_text": "x", "score": 0.7},
        {"entity_id": "M2", "level": "method", "summary_text": "y", "score": 0.55},
    ]
    g = MagicMock()
    g.successors.return_value = []
    g.predecessors.return_value = []
    g.module_of.return_value = None
    # 强制 rerank，且把候选倒序作为"重排结果"
    monkeypatch.setattr(rmod, "should_rerank", lambda c: True)
    monkeypatch.setattr(rmod, "rerank_candidates", lambda q, c: list(reversed(c)))
    r = QARetriever(interpretation_store=bs, graph=g, recall_threshold=0.45)
    ctx = await r.retrieve(question="q", project_id="p", top_k=5)
    # 门控解耦不变量：recall_score 仍是原始 cosine top1（0.7），不被 rerank 改
    assert ctx.recall_score == 0.7
    # entry_candidates 顺序来自 rerank（倒序 → M2, M1）
    assert [c["entity_id"] for c in ctx.entry_candidates] == ["M2", "M1"]


@pytest.mark.asyncio
async def test_retrieve_chitchat_skips_rerank(monkeypatch):
    """chit-chat 分支（top1<0.45）不调 rerank。"""
    import src.service.qa_engine.retriever as rmod
    bs = MagicMock()
    bs.search_method_hits_by_text.return_value = [{"entity_id": "M1", "score": 0.2}]
    g = MagicMock()
    called = {"n": 0}

    def _spy(q, c):
        called["n"] += 1
        return c

    monkeypatch.setattr(rmod, "rerank_candidates", _spy)
    r = QARetriever(interpretation_store=bs, graph=g, recall_threshold=0.45)
    ctx = await r.retrieve(question="你好", project_id="p", top_k=5)
    assert ctx.skill_id == "chit-chat"
    assert called["n"] == 0  # chit-chat 不进 architecture 分支 → 不 rerank
```

- [ ] **Step 2: 跑测试确认失败**

Run: `./venv/bin/python -m pytest tests/test_auth/test_qa_retriever.py -k "rerank" -v`
Expected: FAIL（retriever 模块无 `should_rerank`/`rerank_candidates` 名字，monkeypatch.setattr 抛 AttributeError）

- [ ] **Step 3: retriever import + 接线**

在 `src/service/qa_engine/retriever.py` 的 import 段（现有 `from src.service.qa_engine.query_preprocess import clean_recall_query` 那行附近）追加：

```python
from src.service.qa_engine.semantic_rerank import should_rerank, rerank_candidates
```

然后在 architecture 分支，找到这段（当前 L156-157）：

```python
        # entry_candidates 存全量召回结果，synthesizer 可按需截断
        ctx.entry_candidates = candidates
```

替换为（在赋值前插入门控式 rerank）：

```python
        # 召回二次语义重排（[[召回二次重排-设计]]）：门控式护栏——cosine 不自信时才 gte-rerank
        # 重排候选顺序。recall_score 已于上方用原始 cosine top1 算好（门控解耦，不受影响）；
        # 这里只改候选呈现顺序（→ 影响 LLM 候选 + call-chain top-3 入口展开）。用原始 question
        # 而非 recall_text（cross-encoder 吃完整意图）。rerank 内部 best-effort，失败回退原序。
        if should_rerank(candidates):
            candidates = rerank_candidates(question, candidates)
        # entry_candidates 存全量召回结果，synthesizer 可按需截断
        ctx.entry_candidates = candidates
```

- [ ] **Step 4: 跑测试确认通过**

Run: `./venv/bin/python -m pytest tests/test_auth/test_qa_retriever.py -v`
Expected: PASS（含 2 个新测 + 原有 architecture/门控/富集测试）

- [ ] **Step 5: 提交**

```bash
git add src/service/qa_engine/retriever.py tests/test_auth/test_qa_retriever.py
git commit -m "feat(rerank): retriever architecture 分支接门控式语义重排（门控解耦）"
```

---

## Task 4: 全量回归 + 部署 + 离线复测（需用户授权部署）

**Files:** 无（验证 + 部署）

- [ ] **Step 1: 全量回归**

Run: `./venv/bin/python -m pytest tests/test_auth tests/test_knowledge tests/test_integrations -q`
Expected: 全 PASS（基线 837 + 本次新增约 11 例）

- [ ] **Step 2: 部署（需用户显式授权）**

GitHub 服务器侧 443 不通 → git bundle 走 SSH：
```bash
cd /Users/java/knowledge-engineering-auth
git bundle create /tmp/cc_rerank.bundle <上次部署HEAD>..release-0513
scp -P 26666 /tmp/cc_rerank.bundle root@103.47.81.50:/tmp/cc_rerank.bundle
ssh -p 26666 root@103.47.81.50 'cd /opt/knowledge-engineering && git fetch /tmp/cc_rerank.bundle release-0513 && git merge --ff-only FETCH_HEAD && systemctl restart ke-api && sleep 6 && systemctl is-active ke-api && curl -s -m 10 http://127.0.0.1:8000/health'
```

- [ ] **Step 3: 离线复测（需授权，确认护栏有效）**

把 `should_rerank` + `rerank_candidates` 接进验证脚本 `_rerank20gold.py`（rerank 阶段改为：先 `should_rerank(cands)`，True 才 `rerank_candidates`），在服务器跑：
- 确认 recall@1 仍明显高于 cosine（门控式没把上行吃掉）；
- 确认 `generateOrder` 那题 `should_rerank` 判 **False**（cosine 自信 → 不 rerank → 不翻车）；
- 若 generateOrder 仍被 rerank/翻车，按实际分数标定 `KE_RERANK_CONFIDENT_TOP1` / `KE_RERANK_MARGIN`（dump 该题 top1/top2 分数定阈值）。

- [ ] **Step 4: 更新 Obsidian 完成标记**

`召回二次重排-设计.md` 顶部 frontmatter `状态:` 改「已实施+部署」；正文追加实施小结 + 离线复测结果 + 最终阈值；更新 `_overview.md` / `index.md` / `log.md`。

---

## Self-Review（计划 vs spec）

- **§2 门控解耦** → Task 3（recall_score 在 rerank 前算好；测 `test_retrieve_applies_rerank_but_keeps_recall_score` 断言 recall_score 不变）✓
- **§2/§4 门控式护栏** → Task 1（should_rerank：top1<0.6 或 gap<0.05 才 True）✓
- **§5 rerank 调用** → Task 2（_gte_rerank 原始 question × summary_text，gte-rerank-v2 格式）✓
- **§6 工程护栏** → Task 2（env 开关 _rerank_enabled、失败降级、超时 5s）✓
- **§7 与 recall_rerank 关系** → 不动 recall_rerank（File Structure 已声明）✓
- **§8 模块** → semantic_rerank.py（should_rerank/rerank_candidates/_gte_rerank）+ retriever 接线 ✓
- **§9 测试** → should_rerank 4 例 / rerank_candidates 5 例 / retriever 2 例 / 离线复测 ✓
- **类型一致**：`should_rerank(candidates)->bool`、`rerank_candidates(question, candidates)->list[dict]`、`_gte_rerank(query, documents, api_key)->list[int]` 跨 Task 一致 ✓
- 占位符扫描：无 TBD/TODO；每步含完整代码 ✓
- 不变量：rerank 不改 score 字段（Task 2 测 by_id 断言）、recall_score 解耦（Task 3 测）✓
