"""SSE（Server-Sent Events）事件流式生成器。

设计文档：[[首页设计]] §6.4 SSE 事件协议

事件序列（v1 同步式，逐段 dump 不做 token 流）：
  meta            一次（含 session_id / message_id / plan_steps）
  step *          多次（searching / chain_extraction / synthesizing）
  section_start \\
  content        > 每段 3 个事件（v1 一次性 dump，v1.5 改 token 流）
  section_done  /
  done            一次（含 token_usage / cost / latency）

错误时：
  error           替代 done，data 含 recoverable 字段

v1 简化点：
  - content 事件一次性 dump 完整段（不切 token）
  - 没有 token-by-token 打字机效果（v1.5 接通流式 LLM provider 后再做）
"""
from __future__ import annotations

import json
import time
import uuid
from typing import AsyncIterator, Awaitable, Callable

from src.service.qa_engine.retriever import QARetriever
from src.service.qa_engine.synthesizer import QASynthesizer


# ─── 工具：format SSE 行 ────────────────────────────────────────────────────

def format_sse(event_type: str, data: object) -> str:
    """格式化为 SSE 单条事件。

    SSE 协议：
      event: <type>\\n
      data: <json one line>\\n
      \\n      ← 双换行结束本事件
    """
    payload = json.dumps(data, ensure_ascii=False, separators=(",", ":"))
    return f"event: {event_type}\ndata: {payload}\n\n"


# ─── 类型：on_complete 回调 ─────────────────────────────────────────────────

OnCompleteCallback = Callable[
    [str, list[dict], dict],   # (question, sections, metadata)
    Awaitable[None],
]


# ─── 主生成器 ──────────────────────────────────────────────────────────────

async def stream_qa_answer(
    *,
    question: str,
    project_id: str,
    session_id: str,
    retriever: QARetriever,
    synthesizer: QASynthesizer,
    history: list[dict] | None = None,
    on_complete: OnCompleteCallback | None = None,
) -> AsyncIterator[str]:
    """流式产出 SSE 事件文本。

    Args:
        on_complete: 答案合成成功后的回调（router 用它来持久化消息到 DB）。
                     失败时不调用。
    """
    message_id = "msg_" + uuid.uuid4().hex[:12]
    start = time.monotonic()

    # 1. meta
    yield format_sse("meta", {
        "session_id": session_id,
        "message_id": message_id,
        "plan_steps": ["searching", "chain_extraction", "synthesizing"],
    })

    # 2. step: searching
    yield format_sse("step", {"phase": "searching", "desc": "检索相关代码实体"})

    try:
        ctx = await retriever.retrieve(
            question=question, project_id=project_id, top_k=5
        )
    except Exception as e:
        yield format_sse("error", {
            "code": "RETRIEVE_FAILED",
            "message": f"检索失败：{e}",
            "recoverable": True,
        })
        return

    # 3. step: chain_extraction（retriever 已经做完，事件只是 UI 反馈）
    yield format_sse("step", {"phase": "chain_extraction", "desc": "提取调用链路"})

    # 4. step: synthesizing
    yield format_sse("step", {"phase": "synthesizing", "desc": "合成业务文档"})

    try:
        answer = await synthesizer.synthesize(ctx, history=history)
    except Exception as e:
        yield format_sse("error", {
            "code": "LLM_FAILED",
            "message": f"LLM 调用失败：{e}",
            "recoverable": True,
        })
        return

    # 5. 按段 dump（v1：每段 section_start + content + section_done）
    for section in answer.sections:
        section_type = section.get("type", "unknown")
        yield format_sse("section_start", {
            "section": section_type,
            "title": section.get("title", ""),
        })
        yield format_sse("content", {
            "section": section_type,
            "delta": section.get("content", ""),
        })
        yield format_sse("section_done", {
            "section": section_type,
            "references": section.get("references", []),
        })

    # 6. 持久化（router 注入的 callback）
    latency_ms = int((time.monotonic() - start) * 1000)
    metadata = {
        "token_usage": answer.token_usage,
        "cost_yuan": answer.cost_yuan,
        "latency_ms": latency_ms,
        "entry_points": [
            c.get("entity_id") for c in ctx.entry_candidates[:3] if c.get("entity_id")
        ],
        "cited_entities": _collect_cited_entities(answer.sections),
    }
    if on_complete:
        try:
            await on_complete(question, answer.sections, metadata)
        except Exception:
            # 持久化失败不影响给前端的答案
            pass

    # 7. done
    yield format_sse("done", {
        "session_id": session_id,
        "message_id": message_id,
        "total_tokens": answer.token_usage,
        "cost_yuan": answer.cost_yuan,
        "latency_ms": latency_ms,
    })


# ─── 工具 ───────────────────────────────────────────────────────────────────

def _collect_cited_entities(sections: list[dict]) -> list[str]:
    """从 sections.references 抽取所有 entity_id（去重保序）。"""
    seen: set[str] = set()
    result: list[str] = []
    for s in sections:
        for ref in s.get("references", []) or []:
            eid = ref.get("entity_id") if isinstance(ref, dict) else None
            if eid and eid not in seen:
                seen.add(eid)
                result.append(eid)
    return result
