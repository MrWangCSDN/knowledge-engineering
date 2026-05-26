# 代码解读 Agent 引擎 — Plan A-cont：MiniMax thinking + 工具流式实现计划

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 让 MiniMax-M2 在「流式 + 工具」路径里把 `<think>...</think>` 推理段提取为 `StreamThinkingDelta`（而非现在的丢弃），与正文 `StreamTextDelta`、`ToolCall` 并行 —— 补齐 Phase 1 双 provider 的最后一块。

**Architecture:** 抽出一个有状态的 `ThinkSplitter` 把「逐 chunk 文本流」切成 think/text 段（处理跨 chunk 切碎的标签）。MiniMax 现有 `complete_stream`（丢弃 think）改用它（顺带补上当前缺失的测试覆盖）；MiniMax 新 override `complete_stream_with_tools` 用它把父类吐出的 `StreamTextDelta` 转成 `StreamThinkingDelta`/`StreamTextDelta`，`ToolCall` 原样透传（MiniMax 工具调用走标准 OpenAI 协议，de-risk 已证，复用父类累积逻辑）。

**Tech Stack:** Python 3.12 / 异步生成器 / pytest（仓库 venv：`./venv/bin/python -m pytest`，homebrew python3 无 pytest-asyncio）。

**设计来源（单一真相）:** Obsidian `[[代码解读Agent引擎-设计]]` §4.1/§4.2（MiniMax `<think>` 提取，de-risk 已证原生支持工具）+ §5（thinking 灰字）。

**前置:** Plan A 已落地（`StreamThinkingDelta` 类型存在于 `src/service/qa_engine/llm_types.py`；DashScope 侧 `reasoning_content`→thinking 已通）。本计划只动 MiniMax 侧 + 新增 splitter。

**范围边界:** 仅 MiniMax 模型层 thinking 提取。**不含**：agent loop（Phase 2）、新工具（Phase 3）、SSE 事件、前端、开关。

**仓库 / 分支:** `/Users/java/knowledge-engineering-auth`，分支 `release-0513`（逐任务 commit 已授权，直接在分支干，无 worktree）。

**关键背景 — 现有 MiniMax `<think>` 状态机（`llm_minimax.py:91-137` 的 `complete_stream`）:** 当前用 `in_think` + `buf` 跨 chunk 缓冲，找 `<think>` / `</think>`，think 段**直接丢弃**。本计划把这套逻辑抽成可复用、可单测的 `ThinkSplitter`，并让它**同时 emit think 段**（tools 路径要用），`complete_stream` 消费时只取 text 段（保持"丢弃 think"现状）。

---

## Task 1: 抽出有状态 `ThinkSplitter` 助手（最难的逻辑，重点 TDD）

**Files:**
- Create: `src/service/qa_engine/think_splitter.py`
- Test: `tests/test_auth/test_qa_think_splitter.py`（新建）

- [ ] **Step 1: 写失败测试**

```python
# tests/test_auth/test_qa_think_splitter.py
"""
ThinkSplitter：把逐 chunk 文本流切成 think/text 段。
处理 <think>...</think> 跨 chunk 被切碎的标签（'<thi'+'nk>' 类）。
MiniMax 流式（含工具）路径用它把推理段路由到 StreamThinkingDelta。
"""
from src.service.qa_engine.think_splitter import ThinkSplitter, Segment


def _drain(splitter: ThinkSplitter, chunks: list[str]) -> list[Segment]:
    """喂入所有 chunk + flush，收集全部 Segment。"""
    out: list[Segment] = []
    for c in chunks:
        out.extend(splitter.feed(c))
    out.extend(splitter.flush())
    return out


def test_plain_text_no_think():
    # 没有 think 标签 → 全是 text 段
    segs = _drain(ThinkSplitter(), ["你好", "世界"])
    assert all(s.kind == "text" for s in segs)
    assert "".join(s.text for s in segs) == "你好世界"


def test_single_think_segment_one_chunk():
    # 一个 chunk 内含完整 <think>...</think>
    segs = _drain(ThinkSplitter(), ["答案前<think>推理中</think>答案后"])
    think = "".join(s.text for s in segs if s.kind == "think")
    text = "".join(s.text for s in segs if s.kind == "text")
    assert think == "推理中"
    assert text == "答案前答案后"


def test_open_tag_split_across_chunks():
    # 开标签被切碎：'<thi' + 'nk>推理</think>正文'
    segs = _drain(ThinkSplitter(), ["正文A<thi", "nk>推理X</think>正文B"])
    think = "".join(s.text for s in segs if s.kind == "think")
    text = "".join(s.text for s in segs if s.kind == "text")
    assert think == "推理X"
    assert text == "正文A正文B"


def test_close_tag_split_across_chunks():
    # 闭标签被切碎：'<think>推理</thi' + 'nk>正文'
    segs = _drain(ThinkSplitter(), ["<think>推理Y</thi", "nk>正文C"])
    think = "".join(s.text for s in segs if s.kind == "think")
    text = "".join(s.text for s in segs if s.kind == "text")
    assert think == "推理Y"
    assert text == "正文C"


def test_think_content_spanning_multiple_chunks():
    # think 段内容跨多个 chunk（无标签的中段）
    segs = _drain(ThinkSplitter(), ["<think>推", "理", "Z</think>尾"])
    think = "".join(s.text for s in segs if s.kind == "think")
    text = "".join(s.text for s in segs if s.kind == "text")
    assert think == "推理Z"
    assert text == "尾"


def test_unclosed_think_flushes_as_think():
    # 流结束时仍在 think 段（没等到闭标签）→ 残余作为 think 段 flush
    segs = _drain(ThinkSplitter(), ["正文<think>未闭合推理"])
    think = "".join(s.text for s in segs if s.kind == "think")
    text = "".join(s.text for s in segs if s.kind == "text")
    assert text == "正文"
    assert think == "未闭合推理"


def test_segment_is_frozen():
    import dataclasses
    s = Segment(kind="text", text="x")
    try:
        s.text = "y"  # type: ignore[misc]
        assert False, "应抛 FrozenInstanceError"
    except dataclasses.FrozenInstanceError:
        pass
```

- [ ] **Step 2: 跑测试确认失败**

Run: `./venv/bin/python -m pytest tests/test_auth/test_qa_think_splitter.py -v`
Expected: FAIL，`ModuleNotFoundError: No module named 'src.service.qa_engine.think_splitter'`

- [ ] **Step 3: 实现 `ThinkSplitter`**

Create `src/service/qa_engine/think_splitter.py`:

```python
"""有状态的 <think>...</think> 流式分离器（MiniMax-M2 推理链路）。

MiniMax 把推理过程内联在 content 里用 <think>...</think> 包裹，且标签可能被
流式切碎（'<thi' + 'nk>'）。本类把逐 chunk 文本切成 think/text 段，跨 chunk
状态由内部 buffer 维护。

两个消费方：
  - complete_stream（无工具）：只取 text 段，丢弃 think（保持历史行为）
  - complete_stream_with_tools：think 段 → StreamThinkingDelta，text 段 → StreamTextDelta

逻辑改写自原 llm_minimax.py complete_stream 的 in_think/buf 状态机，
区别：本类**也 emit think 段**（原逻辑直接丢弃）。
"""
from __future__ import annotations

from dataclasses import dataclass

# '</think>' 长度 = 8；闭标签可能被切碎，缓冲尾部至少保留这么多字符
_CLOSE_TAG = "</think>"
_OPEN_TAG = "<think>"


@dataclass(frozen=True, slots=True)
class Segment:
    """一段已归类文本。kind='think'（推理段内）或 'text'（正文）。"""
    kind: str
    text: str


class ThinkSplitter:
    """喂 chunk（feed）吐 Segment；流末调 flush() 取残余。"""

    def __init__(self) -> None:
        # 是否处在 <think>...</think> 段内
        self._in_think = False
        # 跨 chunk 缓冲：可能含被切碎的标签碎片
        self._buf = ""

    def feed(self, chunk: str) -> list[Segment]:
        """喂入一个文本 chunk，返回本次能确定归类的 Segment 列表。"""
        self._buf += chunk
        out: list[Segment] = []
        while True:
            if not self._in_think:
                idx = self._buf.find(_OPEN_TAG)
                if idx == -1:
                    # 没开标签：保留最后一个 '<' 起的尾部（可能是半截 '<think>'），其余作 text emit
                    last_lt = self._buf.rfind("<")
                    if last_lt == -1:
                        if self._buf:
                            out.append(Segment("text", self._buf))
                        self._buf = ""
                    else:
                        head = self._buf[:last_lt]
                        if head:
                            out.append(Segment("text", head))
                        self._buf = self._buf[last_lt:]
                    break
                # 找到 <think>：之前是正文，进入 think 段
                if idx > 0:
                    out.append(Segment("text", self._buf[:idx]))
                self._buf = self._buf[idx + len(_OPEN_TAG):]
                self._in_think = True
            else:
                idx = self._buf.find(_CLOSE_TAG)
                if idx == -1:
                    # 没闭标签：保留尾部 8 字符（防 '</think>' 被切碎），其余作 think emit
                    if len(self._buf) > len(_CLOSE_TAG):
                        out.append(Segment("think", self._buf[: -len(_CLOSE_TAG)]))
                        self._buf = self._buf[-len(_CLOSE_TAG):]
                    # 否则 buf <= 8，可能是半截闭标签，全留，等下个 chunk
                    break
                # 找到 </think>：之前是推理，退出 think 段
                if idx > 0:
                    out.append(Segment("think", self._buf[:idx]))
                self._buf = self._buf[idx + len(_CLOSE_TAG):]
                self._in_think = False
        return out

    def flush(self) -> list[Segment]:
        """流结束：buf 残余按当前状态归类输出（清空状态）。"""
        out: list[Segment] = []
        if self._buf:
            kind = "think" if self._in_think else "text"
            out.append(Segment(kind, self._buf))
        self._buf = ""
        return out
```

- [ ] **Step 4: 跑测试确认通过**

Run: `./venv/bin/python -m pytest tests/test_auth/test_qa_think_splitter.py -v`
Expected: PASS（7 passed）

- [ ] **Step 5: commit**

```bash
git add src/service/qa_engine/think_splitter.py tests/test_auth/test_qa_think_splitter.py
git commit -m "$(cat <<'EOF'
feat(qa-engine): 抽出有状态 ThinkSplitter 分离 <think> 流式段

agent 引擎 Plan A-cont：把 MiniMax <think>...</think> 解析从 complete_stream 内联
逻辑抽成可复用、可单测的 ThinkSplitter（同时 emit think 段，原逻辑只丢弃）。
处理跨 chunk 切碎的开/闭标签。后续 complete_stream + complete_stream_with_tools 共用。

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>
EOF
)"
```

---

## Task 2: `complete_stream` 改用 `ThinkSplitter`（DRY + 补测试覆盖）

**目标:** MiniMax 现有 `complete_stream`（`llm_minimax.py:76-137`）的内联 `<think>` 状态机改用 Task 1 的 `ThinkSplitter`，只 yield text 段（保持"丢弃 think"现状）。当前这段逻辑**无测试**，先补特征测试锁住行为，再重构。

**Files:**
- Modify: `src/service/qa_engine/llm_minimax.py:76-137`（`complete_stream` 方法体重构）
- Test: `tests/test_auth/test_qa_minimax_stream.py`（新建）

- [ ] **Step 1: 写特征测试（锁当前"剥 think"行为）**

```python
# tests/test_auth/test_qa_minimax_stream.py
"""
MiniMaxProvider.complete_stream：剥掉 <think>...</think> 段只吐正文（历史行为）。
此前无测试覆盖；本文件先锁行为，再支撑 ThinkSplitter 重构不回归。
"""
import pytest

from src.service.qa_engine.llm_minimax import MiniMaxProvider


async def _fake_parent_stream(chunks):
    """模拟父类 DashScopeProvider.complete_stream 逐 chunk yield 字符串。"""
    for c in chunks:
        yield c


@pytest.mark.asyncio
async def test_complete_stream_strips_think(monkeypatch):
    # 构造 provider（api_key 给假值绕过 __init__ 校验）
    provider = MiniMaxProvider(api_key="test-key")

    # monkeypatch 父类 complete_stream，让它吐带 <think> 的分片
    chunks = ["答案前<think>这是推", "理过程</think>答案后"]

    # 替换父类绑定方法（MiniMax.complete_stream 内部调 super().complete_stream）
    # super() 沿 MRO 查到被 patch 的 DashScopeProvider.complete_stream
    monkeypatch.setattr(
        "src.service.qa_engine.llm_dashscope.DashScopeProvider.complete_stream",
        lambda self, *, system, user, **kwargs: _fake_parent_stream(chunks),
    )

    out = []
    async for tok in provider.complete_stream(system="s", user="u"):
        out.append(tok)

    # think 段被剥掉，只剩正文
    assert "".join(out) == "答案前答案后"
    assert "<think>" not in "".join(out)
    assert "推理过程" not in "".join(out)
```

- [ ] **Step 2: 跑测试确认通过（当前内联逻辑应已满足）**

Run: `./venv/bin/python -m pytest tests/test_auth/test_qa_minimax_stream.py -v`
Expected: PASS（1 passed）—— 锁住当前行为。若 FAIL 说明对现状理解有误，**停下来报告**，不要改代码。

- [ ] **Step 3: 重构 `complete_stream` 用 `ThinkSplitter`**

把 `src/service/qa_engine/llm_minimax.py` 的 `complete_stream` 方法体（从 `in_think = False` 到方法结束）整体替换为：

```python
    async def complete_stream(
        self,
        *,
        system: str,
        user: str,
        **kwargs: Any,
    ):
        """流式 yield，剥 <think>...</think> 段（只吐正文）。

        2026-05-22 重构：内联状态机抽到 ThinkSplitter（见 think_splitter.py）。
        本方法只消费 text 段、丢弃 think 段，保持历史"剥 think"语义不变。
        """
        # 局部 import 避免顶部循环依赖风险（与仓库其它 provider 同模式）
        from src.service.qa_engine.think_splitter import ThinkSplitter

        splitter = ThinkSplitter()
        async for tok in super().complete_stream(system=system, user=user, **kwargs):
            for seg in splitter.feed(tok):
                # 只吐正文；think 段丢弃（历史语义）
                if seg.kind == "text" and seg.text:
                    yield seg.text
        # 流末残余
        for seg in splitter.flush():
            if seg.kind == "text" and seg.text:
                yield seg.text
```

同时把方法顶部的 import 区确认有 `from typing import Any`（文件已 import，无需重复）。

- [ ] **Step 4: 跑测试确认仍通过（行为不回归）**

Run: `./venv/bin/python -m pytest tests/test_auth/test_qa_minimax_stream.py -v`
Expected: PASS（1 passed）—— 重构后剥 think 行为不变。

- [ ] **Step 5: commit**

```bash
git add src/service/qa_engine/llm_minimax.py tests/test_auth/test_qa_minimax_stream.py
git commit -m "$(cat <<'EOF'
refactor(qa-engine): MiniMax complete_stream 改用 ThinkSplitter（DRY + 补测试）

agent 引擎 Plan A-cont：把 complete_stream 内联的 <think> 状态机替换为复用
ThinkSplitter（只取 text 段、丢弃 think，行为不变）。顺带补上此前缺失的特征测试，
为 complete_stream_with_tools override 复用同一 splitter 铺路。

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>
EOF
)"
```

---

## Task 3: MiniMax override `complete_stream_with_tools`（think 段 → StreamThinkingDelta）

**目标:** MiniMax 当前继承父类 `complete_stream_with_tools`，其 `delta.content` 里的 `<think>` 标签会原样混进 `StreamTextDelta`。override 它：用 `ThinkSplitter` 把 `StreamTextDelta` 的文本切成 think/text，分别 emit `StreamThinkingDelta`/`StreamTextDelta`；`ToolCall` 等其它事件原样透传。

**Files:**
- Modify: `src/service/qa_engine/llm_minimax.py`（新增 `complete_stream_with_tools` override + import）
- Test: `tests/test_auth/test_qa_minimax_stream.py`（追加用例）

- [ ] **Step 1: 写失败测试**

在 `tests/test_auth/test_qa_minimax_stream.py` 末尾追加：

```python
@pytest.mark.asyncio
async def test_stream_with_tools_routes_think_to_thinking_delta(monkeypatch):
    from src.service.qa_engine.llm_types import (
        StreamTextDelta,
        StreamThinkingDelta,
        ToolCall,
    )

    provider = MiniMaxProvider(api_key="test-key")

    # 模拟父类 complete_stream_with_tools 吐出的事件序列：
    # 正文 StreamTextDelta（含跨片 <think>）+ 一个 ToolCall
    async def fake_parent_events(*, messages, tools, **kwargs):
        yield StreamTextDelta(text="答案前<think>推")
        yield StreamTextDelta(text="理段</think>答案后")
        yield ToolCall(id="c1", name="ke_search", arguments={"query": "x"})

    monkeypatch.setattr(
        "src.service.qa_engine.llm_dashscope.DashScopeProvider.complete_stream_with_tools",
        lambda self, *, messages, tools, **kwargs: fake_parent_events(
            messages=messages, tools=tools, **kwargs
        ),
    )

    events = []
    async for ev in provider.complete_stream_with_tools(messages=[], tools=[]):
        events.append(ev)

    # think 段被路由到 StreamThinkingDelta
    think = "".join(e.text for e in events if isinstance(e, StreamThinkingDelta))
    text = "".join(e.text for e in events if isinstance(e, StreamTextDelta))
    tool_calls = [e for e in events if isinstance(e, ToolCall)]

    assert think == "推理段"
    assert text == "答案前答案后"
    # ToolCall 原样透传
    assert len(tool_calls) == 1
    assert tool_calls[0].name == "ke_search"
    assert tool_calls[0].arguments == {"query": "x"}
```

- [ ] **Step 2: 跑测试确认失败**

Run: `./venv/bin/python -m pytest tests/test_auth/test_qa_minimax_stream.py -k tools -v`
Expected: FAIL —— 父类 override 还没加，`<think>` 标签会原样出现在 StreamTextDelta（`think` 断言失败 / `text` 含标签）。

- [ ] **Step 3: 实现 override**

3a. 在 `src/service/qa_engine/llm_minimax.py` 顶部 import 区（`from src.service.qa_engine.llm_dashscope import DashScopeProvider` 之后）加：

```python
from src.service.qa_engine.llm_types import StreamTextDelta, StreamThinkingDelta
```

3b. 在 `MiniMaxProvider` 类里（`complete_stream` 方法之后）新增：

```python
    async def complete_stream_with_tools(
        self,
        *,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]],
        **kwargs: Any,
    ):
        """流式 + 工具：把父类吐的 StreamTextDelta 经 ThinkSplitter 切成
        think/text 段 → StreamThinkingDelta / StreamTextDelta；其它事件（ToolCall）原样透传。

        MiniMax-M2 把推理内联在 content 里用 <think>...</think>（不像 qwen 走
        reasoning_content 独立字段），所以必须在事件流层面再切一次。
        工具调用走标准 OpenAI 协议（de-risk 已证），父类累积逻辑直接复用。
        """
        from src.service.qa_engine.think_splitter import ThinkSplitter

        splitter = ThinkSplitter()

        def _emit(seg) -> Any:
            # think 段 → StreamThinkingDelta；text 段 → StreamTextDelta
            if seg.kind == "think":
                return StreamThinkingDelta(text=seg.text)
            return StreamTextDelta(text=seg.text)

        async for ev in super().complete_stream_with_tools(
            messages=messages, tools=tools, **kwargs
        ):
            if isinstance(ev, StreamTextDelta):
                for seg in splitter.feed(ev.text):
                    if seg.text:
                        yield _emit(seg)
            else:
                # ToolCall / 其它事件原样透传
                yield ev
        # 流末残余
        for seg in splitter.flush():
            if seg.text:
                yield _emit(seg)
```

- [ ] **Step 4: 跑测试确认通过**

Run: `./venv/bin/python -m pytest tests/test_auth/test_qa_minimax_stream.py -v`
Expected: PASS（含 Task 2 的 strip 测试 + 本 Task 的 tools 路由测试，全过）

- [ ] **Step 5: commit**

```bash
git add src/service/qa_engine/llm_minimax.py tests/test_auth/test_qa_minimax_stream.py
git commit -m "$(cat <<'EOF'
feat(qa-engine): MiniMax complete_stream_with_tools 把 <think> 路由到 StreamThinkingDelta

agent 引擎 Plan A-cont：MiniMax-M2 推理内联在 content 的 <think> 段，override
工具流式路径用 ThinkSplitter 切成 StreamThinkingDelta/StreamTextDelta，ToolCall 透传。
至此 Phase 1 双 provider（qwen reasoning_content + MiniMax <think>）thinking 流式齐活。

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>
EOF
)"
```

---

## Task 4: 模型层回归 + 设计文档进度更新

- [ ] **Step 1: 跑 qa/stream/think/minimax/dashscope 测试全集**

Run: `./venv/bin/python -m pytest tests/test_auth/ -k "qa or stream or think or minimax or dashscope or react" -q`
Expected: 全 PASS（含 think_splitter 7 + minimax_stream 2 + 既有 dashscope/react/qa）

- [ ] **Step 2: import 自检**

Run: `./venv/bin/python -c "from src.service.qa_engine.think_splitter import ThinkSplitter, Segment; from src.service.qa_engine.llm_minimax import MiniMaxProvider; print('OK')"`
Expected: 打印 `OK`

- [ ] **Step 3: 更新 Obsidian 设计文档 §11**

把 `[[代码解读Agent引擎-设计]]` §11 的 Phase 1 行从「🔵 qwen 侧已完成…MiniMax 待 Plan A-cont」更新为「✅ 双 provider 完成」，注明 commit。

---

## Plan A-cont 完成定义（验收）

1. ✅ `ThinkSplitter` 处理跨 chunk 切碎标签，emit think + text 段，7 测试过
2. ✅ MiniMax `complete_stream` 改用 splitter，剥 think 行为不回归（补了特征测试）
3. ✅ MiniMax `complete_stream_with_tools` 把 `<think>` 路由到 `StreamThinkingDelta`，ToolCall 透传
4. ✅ qa/stream/think/minimax/dashscope/react 测试全过，无回归
5. ✅ 设计文档 §11 Phase 1 标记双 provider 完成

## 后续计划（不在本 Plan）

- **Plan B**：Phase 2 agent loop 改造（cap 3→12 + 停止条件 + per-request registry）+ Phase 3 三个新工具（ke_read_entity / ke_impact / ke_method_interp，含 GraphProto + Neo4jGraphAdapter 扩展）
- **Plan C**：Phase 4-6 SSE 事件（thinking/todo/citation）+ 前端组件 + Phase 7 自由格式 + 开关上线 + Phase 8 回归
