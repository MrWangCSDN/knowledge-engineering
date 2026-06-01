"""sse_emitter 不再调 router；retrieve 不传 skill_id；召回后 emit route 事件带 skill_id+recall_score。

测试目的：
  - 验证 stream_qa_answer 即使传入 router 也不会调用 router.route
  - 验证 retriever.retrieve 的 kwargs 里没有 skill_id
  - 验证 SSE 事件流里包含 route 事件，且带有 skill_id 和 recall_score
"""
import json
import pytest
from unittest.mock import AsyncMock, MagicMock  # AsyncMock 模拟异步函数，MagicMock 模拟普通对象
from src.service.qa_engine.retriever import RetrievedContext  # 召回上下文数据类


def _events(sse_text_list):
    """把若干 SSE 文本块解析成 [(event_name, data_dict)]。

    SSE 格式每块形如：
      event: <type>
      data: <json>

    Args:
        sse_text_list: list[str]，每个元素是一次 yield 的 SSE 文本块
    Returns:
        list[tuple[str, dict]]，每个元素是 (event_type, data_dict)
    """
    out = []  # 结果列表
    for chunk in sse_text_list:
        ev, data = None, None  # 初始化本块的 event 和 data
        for line in chunk.splitlines():  # 逐行解析 SSE 文本
            if line.startswith("event:"):
                # 截取 "event:" 后面的内容并去除首尾空白
                ev = line[len("event:"):].strip()
            elif line.startswith("data:"):
                # 截取 "data:" 后面的 JSON 字符串并解析
                data = json.loads(line[len("data:"):].strip())
        if ev:
            out.append((ev, data))  # 只有有 event 字段的块才记录
    return out


@pytest.mark.asyncio  # 标记为异步测试，pytest-asyncio 负责运行事件循环
async def test_emitter_uses_recall_not_router():
    """核心断言：
    1. router.route 不被调用（sse_emitter 已去掉 router 调用路径）
    2. retriever.retrieve 的 kwargs 里没有 skill_id（召回门控自己决定策略）
    3. SSE 事件流里包含 route 事件，且 skill_id="architecture"、recall_score=0.66
    """
    # 导入被测模块（延迟 import 可以在测试内部 mock 后再用，避免模块级副作用）
    from src.service.qa_engine import sse_emitter as mod

    # 构造 RetrievedContext 模拟召回结果，recall_score=0.66 代表 top1 相似度
    ctx = RetrievedContext(
        question="下单",
        project_id="mall-swarm",
        skill_id="architecture",
        recall_score=0.66,
    )

    # retriever mock：retrieve 是异步方法，用 AsyncMock 包装，直接返回 ctx
    retriever = MagicMock()
    retriever.retrieve = AsyncMock(return_value=ctx)

    # synthesizer mock：synthesize_stream 是异步生成器，用空的 async generator 模拟
    synthesizer = MagicMock()
    async def _fake_stream(*a, **k):
        """空的异步生成器：不产出任何 token，直接结束。
        `if False: yield ""` 是 Python 把普通 async def 变成 async generator 的惯用法。
        """
        if False:
            yield ""  # 这行永远不执行，但它让函数成为 async generator
    synthesizer.synthesize_stream = _fake_stream

    # router mock：传入但期望绝对不被调用
    router = MagicMock()

    # 收集所有 SSE 文本块
    chunks = []
    # `async for` 用于遍历 async generator（stream_qa_answer 是 async generator 函数）
    async for c in mod.stream_qa_answer(
        question="下单",
        project_id="mall-swarm",
        session_id="s1",
        retriever=retriever,
        synthesizer=synthesizer,
        router=router,
    ):
        chunks.append(c)

    # 断言 1：router.route 绝对没被调用
    router.route.assert_not_called()

    # 断言 2：retrieve 的 kwargs 里没有 skill_id
    # call_args 是 (args, kwargs) 的 tuple，取 [1] 或 .kwargs 得到关键字参数字典
    _, kwargs = retriever.retrieve.call_args
    assert "skill_id" not in kwargs, (
        f"retrieve 不应收到 skill_id，但实际 kwargs={kwargs}"
    )

    # 断言 3：SSE 流里有 route 事件，且携带正确的 skill_id 和 recall_score
    # dict() 把 [(event, data), ...] 转成 {event: data}，同名事件取最后一个
    evs = dict((e, d) for e, d in _events(chunks))
    assert "route" in evs, f"SSE 流里缺少 route 事件，实际事件列表：{list(evs.keys())}"
    assert evs["route"]["recall_score"] == 0.66, (
        f"recall_score 期望 0.66，实际 {evs['route']['recall_score']}"
    )
    assert evs["route"]["skill_id"] == "architecture", (
        f"skill_id 期望 'architecture'，实际 {evs['route']['skill_id']}"
    )


@pytest.mark.asyncio  # 标记为异步测试，pytest-asyncio 负责运行事件循环
async def test_emitter_chit_chat_route_event():
    """chit-chat 分支：gate 判低召回时，emitter 应 emit route 事件且 skill_id=="chit-chat"。

    核心断言：
    1. router.route 不被调用（sse_emitter 已去掉 router 调用路径）
    2. SSE 事件流里包含 route 事件，且 skill_id="chit-chat"、recall_score==0.3
    """
    # 导入被测模块（延迟 import 可以在测试内部 mock 后再用，避免模块级副作用）
    from src.service.qa_engine import sse_emitter as mod

    # 构造 chit-chat 分支的 RetrievedContext：
    #   recall_score=0.30（低于阈值，门控判为闲聊）
    #   entry_candidates=[]（空列表，无 KE 内容）
    ctx = RetrievedContext(
        question="今天天气怎么样",
        project_id="mall-swarm",
        skill_id="chit-chat",     # 召回门控产出的 skill_id
        recall_score=0.30,        # 低于阈值（如 0.45）的模拟分数
        entry_candidates=[],      # 无候选，chit-chat 不查图
    )

    # retriever mock：retrieve 是异步方法，用 AsyncMock 包装，直接返回 chit-chat ctx
    retriever = MagicMock()
    retriever.retrieve = AsyncMock(return_value=ctx)

    # synthesizer mock：synthesize_stream 是异步生成器，用空的 async generator 模拟
    # chit-chat 路径不产出 token（简化验证 route 事件即可）
    synthesizer = MagicMock()
    async def _fake_stream(*a, **k):
        """空的异步生成器：不产出任何 token，直接结束。
        `if False: yield ""` 是 Python 把普通 async def 变成 async generator 的惯用法。
        """
        if False:
            yield ""  # 这行永远不执行，但它让函数成为 async generator
    synthesizer.synthesize_stream = _fake_stream

    # router mock：传入但期望绝对不被调用（sse_emitter 已去掉 router 调用路径）
    router = MagicMock()

    # 收集所有 SSE 文本块
    chunks = []
    # `async for` 用于遍历 async generator（stream_qa_answer 是 async generator 函数）
    async for c in mod.stream_qa_answer(
        question="今天天气怎么样",
        project_id="mall-swarm",
        session_id="s2",
        retriever=retriever,
        synthesizer=synthesizer,
        router=router,
    ):
        chunks.append(c)

    # 断言 1：router.route 绝对没被调用（chit-chat 分支不走 router）
    router.route.assert_not_called()

    # 断言 2：SSE 流里有 route 事件，且 skill_id="chit-chat"、recall_score=0.3
    # dict() 把 [(event, data), ...] 转成 {event: data}，同名事件取最后一个
    evs = dict((e, d) for e, d in _events(chunks))
    assert "route" in evs, (
        f"SSE 流里缺少 route 事件，实际事件列表：{list(evs.keys())}"
    )
    assert evs["route"]["skill_id"] == "chit-chat", (
        f"skill_id 期望 'chit-chat'，实际 {evs['route']['skill_id']}"
    )
    assert evs["route"]["recall_score"] == 0.3, (
        f"recall_score 期望 0.3，实际 {evs['route']['recall_score']}"
    )
