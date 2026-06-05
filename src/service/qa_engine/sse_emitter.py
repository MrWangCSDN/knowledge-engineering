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
import os
import time
import uuid
from typing import Any, AsyncIterator, Awaitable, Callable

from src.service.qa_engine.react_synthesizer import ReActSynthesizer
from src.service.qa_engine.retriever import QARetriever
from src.service.qa_engine.router import SkillRouter
from src.service.qa_engine.synthesizer import QASynthesizer
from src.service.qa_engine.token_batcher import TokenBatcher


# ─── 召回广度（快赢 B）─────────────────────────────────────────────────────

# 召回候选数默认值（快赢 B：5→15，给流程/架构类问题足够候选池重建调用链）
# 设计 [[召回链路缺陷诊断与修复方案]] 快赢 B；可用环境变量 KE_RECALL_TOP_K 覆盖
_RECALL_TOP_K_DEFAULT = 15


def _recall_top_k() -> int:
    """读召回候选数：环境变量 KE_RECALL_TOP_K，缺失/非法/非正 → 默认 15。"""
    try:
        # int(...) 把环境变量字符串转整数；os.environ.get(k, default) 缺失返回 default
        v = int(os.environ.get("KE_RECALL_TOP_K", str(_RECALL_TOP_K_DEFAULT)))
    except (TypeError, ValueError):
        # 非数字（如 "abc"）→ 回落默认
        return _RECALL_TOP_K_DEFAULT
    # 非正数（0 / 负）无意义 → 回落默认
    return v if v > 0 else _RECALL_TOP_K_DEFAULT


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

# on_title 回调：done 之后调用，返回新标题（str）或 None（跳过/失败）。
# router 用它来：判断是否首轮 + 未被手动改 → 调 LLM 总结 → UPDATE+commit DB → 返回标题。
OnTitleCallback = Callable[
    [],
    Awaitable[str | None],
]

# on_memory 回调：done + session_title 之后调用（镜像 on_title 模式）。
# router 用它来：ReAct 抽取本轮可记忆事实 → 写文件 .md → S2.regenerate + S3.index_changed
# + 视情况压缩会话记忆。返回 None；失败静默（记忆是辅助，绝不影响主答）。
# 入参 answer_text：assistant 本轮答案的拼接文本（S4 ReAct 需要看 user+assistant 两侧）。
OnMemoryCallback = Callable[
    [str],
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
    router: SkillRouter | None = None,
    history: list[dict] | None = None,
    on_complete: OnCompleteCallback | None = None,
    on_title: OnTitleCallback | None = None,
    memory_block: str | None = None,
    on_memory: OnMemoryCallback | None = None,
    context_usage: dict | None = None,
) -> AsyncIterator[str]:
    """流式产出 SSE 事件文本。

    Args:
        router: （已弃用）召回门控后 router 不再参与决策；保留形参仅为向后兼容，函数体不使用它。
        on_complete: 答案合成成功后的回调（router 用它来持久化消息到 DB）。
                     失败时不调用。
        on_title: done 之后调用；返回非空 str 时额外 emit 一个 session_title 事件。
        memory_block: 召回的记忆块（用户级 + 会话级）；非空时由 synthesizer
                      注入到 system prompt 顶部。None/空 → 行为与改前完全一致。
        on_memory: done + session_title 之后调用（镜像 on_title）；router 用它
                   解析显式记忆意图写库 + 视情况压缩会话记忆。失败静默，不影响主答。
        context_usage: 可选；非空时并入 meta 事件（前端画上下文进度条，spec §18）。
    """
    message_id = "msg_" + uuid.uuid4().hex[:12]
    start = time.monotonic()

    # 0. meta 构造（召回门控路由设计 [[召回门控路由-设计]]）
    # router 形参保留在签名里（向后兼容旧调用方），但函数体不再调用它。
    # skill_id/route_source/matched_keywords 改由 retrieve 内部按 top1 相似度决定，
    # 结果通过 "route" SSE 事件告知前端（见下方 yield format_sse("route", ...)）。
    meta_payload: dict[str, object] = {
        "session_id": session_id,
        "message_id": message_id,
        "plan_steps": ["searching", "chain_extraction", "synthesizing"],
    }

    if context_usage is not None:
        meta_payload["context_usage"] = context_usage

    # 1. meta
    yield format_sse("meta", meta_payload)

    # 2. step: searching
    yield format_sse("step", {"phase": "searching", "desc": "检索相关代码实体"})

    try:
        # 召回门控：不传 skill_id，retrieve 内部按 top1 相似度决定 KE/闲聊
        # 设计参见 [[召回门控路由-设计]]；旧的 router.route → skill_id 路径已移除
        # 快赢 B：召回候选数从 env 读（默认 15，旧值 5 太小，入口易被截断）
        ctx = await retriever.retrieve(question=question, project_id=project_id, top_k=_recall_top_k())
    except Exception as e:
        yield format_sse("error", {
            "code": "RETRIEVE_FAILED",
            "message": f"检索失败：{e}",
            "recoverable": True,
        })
        return

    # 召回决策事件：skill_id(architecture/chit-chat) + recall_score(top1 相似度)，
    # 供前端显示"匹配度"进度条等 UI 元素（设计 [[召回门控路由-设计]] §5.2）。
    # 旧前端（不认识 route 事件）会忽略未知事件类型，不受影响。
    # getattr 兜底：万一某个 mock/测试没有 recall_score 字段也不崩
    yield format_sse("route", {
        "skill_id": ctx.skill_id,
        "recall_score": round(getattr(ctx, "recall_score", 0.0), 4),
    })

    # 3. step: chain_extraction（retriever 已经做完，事件只是 UI 反馈）
    yield format_sse("step", {"phase": "chain_extraction", "desc": "提取调用链路"})

    # 4. step: synthesizing
    yield format_sse("step", {"phase": "synthesizing", "desc": "合成业务文档"})

    # ReAct 路径：synthesizer 是 ReActSynthesizer 实例时，注入 on_tool_call 回调
    # 让它每次调工具前后都 emit SSE 事件给前端"实时反馈"
    # 用 nonlocal list 收集事件（async generator 跨 await 的标准做法）
    pending_tool_events: list[tuple[str, dict]] = []
    # 已 emit 的正文字符数（流式偏移）：render 工具的 at 锚点 = 调工具时刻的累计字符数，
    # 让前端把调用图内联插到"模型说到这里时"的位置（而非默认 text.length 末尾）。
    # _on_token 累加；_on_tool_call 读快照。用 list 容器以便闭包内可变。
    _offset = [0]

    async def _on_tool_call(phase: str, call, result=None):
        """ReActSynthesizer 调工具时触发；把事件压栈，主流程 yield 之前 flush。

        因为这个回调是 await 来的，不能直接 yield SSE（yield 必须在 async generator 主体里）；
        所以暂存到 list，主流程在合适的时机 flush。
        """
        # todo_write 元工具（设计 §3.3/§8）：不当普通 tool_call 展示，而是转成专属
        # `todo` 事件（前端渲染 checklist）。只在 starting 阶段发（此时 arguments 已带 items）；
        # complete 的 echo 结果无展示价值，直接跳过。
        if call.name == "todo_write":
            if phase == "starting":
                # 与 todo_write handler 一致的兜底：LLM 是系统边界，可能传 null/非 list，
                # 透传给前端前归一化为 list（§3.4 信号哲学，防前端拿到非数组渲染崩）
                items = call.arguments.get("items", [])
                if not isinstance(items, list):
                    items = []
                pending_tool_events.append(("todo", {"items": items}))
            return
        # 只塞最关键字段：name + arguments / result（截断）+ at 流式偏移（render 内联锚点）
        # at 两阶段都带：starting 让前端按位置插"调用中"占位，complete 把 render 换到同一位置。
        payload: dict = {"phase": phase, "id": call.id, "name": call.name, "at": _offset[0]}
        if phase == "starting":
            payload["arguments"] = call.arguments
        else:  # complete
            # 结果可能很大，截断 600 字以内（足够前端展示概要）
            result_text = json.dumps(result or {}, ensure_ascii=False)
            payload["result_preview"] = result_text[:600]
            # 渲染类工具（render_call_graph 等）：透传 render（图数据）给前端内联渲染（CallChainFlow）；
            # 调查类工具无 render 字段，不受影响。设计 [[业务问答-agent化输出改造-设计]] §5.2
            if isinstance(result, dict) and result.get("render") is not None:
                payload["render"] = result["render"]
        pending_tool_events.append(("tool_call", payload))

    # 判断要不要带 on_tool_call：只有 ReActSynthesizer 才认这个 kwarg
    is_react = isinstance(synthesizer, ReActSynthesizer)

    # v1.7：synthesizer 实现了 synthesize_stream 就走流式路径（QASynthesizer / ReActSynthesizer 都行）
    # `getattr(obj, "synthesize_stream", None)` 是 Python 鸭子类型检查的常规做法：
    # 不强求继承某个 ABC，只看实例有没有这个方法
    supports_stream = (
        hasattr(synthesizer, "synthesize_stream")
        and callable(getattr(synthesizer, "synthesize_stream", None))
    )

    # v1.6 token 流：跟 tool_call 类似，用 list 暂存 token，让 on_token 回调里压栈、主流程 yield
    # 2026-05-22：调到 (1字/10ms) — 等于完全禁用 batching，LLM 每来一个 token 立即压栈，
    # 主流程 tick 也从 20ms 降到 5ms。配合前端 raw_stream 累计渲染，达到 ChatGPT 打字机体感。
    pending_tokens: list[str] = []
    pending_thinking: list[str] = []
    token_batcher = TokenBatcher(min_chars=1, max_ms=10)

    async def _on_token(delta: str) -> None:
        """LLM 一吐 chunk 就经过 batcher；攒够了再压栈让主循环 yield SSE。"""
        # 先累加流式偏移（按原始 delta 字符数，与前端 raw_stream 累计长度对齐）——
        # render 工具的 at 锚点据此快照，确保调用图插在"模型说到这里"的位置。
        _offset[0] += len(delta)
        batch = await token_batcher.add(delta)
        if batch is not None:
            pending_tokens.append(batch)

    async def _on_thinking(delta: str) -> None:
        """LLM 思考增量直接入栈，主循环 flush 成 SSE thinking 事件（设计 §5 灰字）。"""
        if delta:
            pending_thinking.append(delta)

    try:
        if supports_stream:
            # v1.6/v1.7 streaming 路径：
            #   QASynthesizer.synthesize_stream(ctx, history, on_token)
            #   ReActSynthesizer.synthesize_stream(ctx, history, on_token, on_tool_call)
            # 用 asyncio.create_task 把 synthesize_stream 跑在后台，
            # 主循环每 tick 检查 pending_tokens / pending_tool_events 并 flush。
            import asyncio

            # 动态构造调用参数：ReAct 才传 on_tool_call
            stream_kwargs: dict[str, Any] = {
                "history": history,
                "on_token": _on_token,
                "memory_block": memory_block,
            }
            if is_react:
                stream_kwargs["on_tool_call"] = _on_tool_call
                stream_kwargs["on_thinking"] = _on_thinking

            task = asyncio.create_task(
                synthesizer.synthesize_stream(ctx, **stream_kwargs)
            )
            # 边等 task 边 flush 事件：每 20ms 检查一次
            while not task.done():
                # flush 思考增量（设计 §5 灰字；在 token 前出）
                while pending_thinking:
                    delta = pending_thinking.pop(0)
                    yield format_sse("thinking", {"delta": delta})
                # flush pending tool_call events（ReAct 模式才有）
                while pending_tool_events:
                    ev_type, ev_data = pending_tool_events.pop(0)
                    yield format_sse(ev_type, ev_data)
                # flush 当前 pending tokens
                while pending_tokens:
                    delta = pending_tokens.pop(0)
                    yield format_sse("token", {"delta": delta})
                await asyncio.sleep(0.005)   # 5ms tick：每秒最多 200 次 yield，达到逐字流式效果
            # task 完成后 buffer 里可能还有最后几个事件
            while pending_thinking:
                delta = pending_thinking.pop(0)
                yield format_sse("thinking", {"delta": delta})
            while pending_tool_events:
                ev_type, ev_data = pending_tool_events.pop(0)
                yield format_sse(ev_type, ev_data)
            while pending_tokens:
                delta = pending_tokens.pop(0)
                yield format_sse("token", {"delta": delta})
            # v1.7：batcher 里的最后一截残留也要 flush 给前端
            final_batch = await token_batcher.flush()
            if final_batch:
                yield format_sse("token", {"delta": final_batch})
            answer = task.result()
        elif is_react:
            # ReAct 非流式兜底（v1.3 老路径，spec=['synthesize'] mock 走这里）
            answer = await synthesizer.synthesize(
                ctx, history=history, on_tool_call=_on_tool_call,
                memory_block=memory_block,
            )
            for ev_type, ev_data in pending_tool_events:
                yield format_sse(ev_type, ev_data)
        else:
            # 兜底：旧 QASynthesizer 仅有 synthesize
            answer = await synthesizer.synthesize(ctx, history=history, memory_block=memory_block)
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
        # agent 实际查过的 entity_id（ReAct 工具调用轨迹，设计 §6）；
        # ≠ 上方 metadata 的 cited_entities（那是从 answer.sections 引用抽取、持久化用）
        "cited_entities": answer.cited_entities,
    })

    # 8. session_title（v1，2026-05-16）：仅当 router 传了 on_title 且返回非空
    # 设计：[[会话标题-重命名与智能总结-设计]] §3.2
    # 注意：on_title 内部已先 commit DB（DB 是 source of truth），
    # 这里 emit 失败（客户端断开）也无妨——下次进会话能看到新标题。
    if on_title is not None:
        try:
            new_title = await on_title()
            if new_title:
                yield format_sse("session_title", {
                    "session_id": session_id,
                    "title": new_title,
                })
        except Exception:
            # 静默降级：标题总结是辅助功能，绝不影响主流程
            pass

    # 9. on_memory（记忆系统 P1，2026-05-16）：done + session_title 之后调。
    # 设计：[[记忆系统-设计]] §6。回调内部自行 commit DB（DB 是 source of truth）；
    # 这里失败（客户端断开 / 写库异常）静默——记忆是辅助功能，绝不影响主答。
    if on_memory is not None:
        try:
            # 拼 assistant 输出文本：所有 section content 用双换行连接
            # S4 ReAct 需要看 user + assistant 本轮全文
            answer_text = "\n\n".join(
                (s.get("content") or "") for s in answer.sections
            )
            await on_memory(answer_text)
        except Exception:
            pass


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
