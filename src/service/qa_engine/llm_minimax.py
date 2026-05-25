"""MiniMax LLM provider 适配器。

MiniMax 提供 OpenAI 兼容接口（chat/completions），协议层完全等价于 DashScope，
所以本类继承 DashScopeProvider 只 override 三个 class-level 常量 + __init__
读不同的 env vars。complete / complete_with_tools / complete_stream
/ complete_stream_with_tools 全部继承自父类不改动（OpenAI 兼容协议通吃）。

环境变量：
  MINIMAX_API_KEY    必填，从 MiniMax 开放平台拿（国内 / 国际版控制台均可）
  MINIMAX_MODEL      可选，默认 MiniMax-M2；可选 MiniMax-M1 / abab6.5s-chat
  MINIMAX_BASE_URL   可选，默认 https://api.minimaxi.com/v1（国内端）
                     国际版填 https://api.minimax.io/v1

参考文档：
  https://platform.minimaxi.com/document/Chat%20Completion(v2)?key=66701d281d57f38758d581d0
"""
# from __future__ import annotations 让类型注解延迟求值（Python 3.12+ 推荐）
from __future__ import annotations

# os：读环境变量；typing.Any：父类签名要求；都是标准库
import os
from typing import Any

# 复用 DashScope 的全部协议层逻辑（complete/stream/tools/parse 等）
from src.service.qa_engine.llm_dashscope import DashScopeProvider


class MiniMaxProvider(DashScopeProvider):
    """MiniMax OpenAI 兼容 client；与 DashScopeProvider 协议一致，仅 env / 默认 model 不同。

    用法：
        provider = MiniMaxProvider()
        answer = await provider.complete(system="...", user="...")
    """

    # ─── class-level 常量 override（覆盖父类 BASE_URL / DEFAULT_MODEL） ──
    # 注意：父类 complete / complete_stream / complete_with_tools 等所有方法都
    # 通过 self.BASE_URL / self.model 访问，子类 override class var 后这些方法
    # 自动用新值（不需重写方法体）。
    BASE_URL = "https://api.minimaxi.com/v1"   # MiniMax 国内官方端；可由 MINIMAX_BASE_URL 覆盖
    DEFAULT_MODEL = "MiniMax-M2"               # 用户默认型号选择（决策点）

    def __init__(
        self,
        *,
        api_key: str | None = None,
        model: str | None = None,
        base_url: str | None = None,
        timeout: float = 60.0,
    ) -> None:
        """与父类签名兼容；只是读不同的 env vars。

        显式 args 优先 → 缺省时读 MINIMAX_* 系列 env → 仍空则用 class-level 默认。
        """
        # api_key：参数 > env > 空（空时下方校验抛错，与父类同语义）
        self.api_key = api_key or os.getenv("MINIMAX_API_KEY", "")
        # model：参数 > env > class-level DEFAULT_MODEL
        self.model = model or os.getenv("MINIMAX_MODEL", self.DEFAULT_MODEL)
        # base_url：参数 > env > class-level BASE_URL
        # 注：父类把 BASE_URL 写成 class attr；这里让 instance attr 优先（self.BASE_URL）
        # MINIMAX_BASE_URL 让国内 / 国际版无需改代码切换
        self.BASE_URL = base_url or os.getenv("MINIMAX_BASE_URL", self.BASE_URL)
        # timeout：复用父类语义（60s 默认）
        self.timeout = timeout

        # 未配置 API key 直接抛错，避免运行期才发现（与父类同模式）
        if not self.api_key:
            raise RuntimeError(
                "MINIMAX_API_KEY 未设置；请在 .env.local 配置或通过 export 注入",
            )

    # 注：complete / complete_with_tools / complete_stream_with_tools 继承自 DashScope。
    # complete_stream 必须 override —— 处理 MiniMax-M2 的 <think>...</think> 推理链路 token，
    # 在 yield 前剥掉（synthesizer 看不到 think 段，前端打字机零额外延迟）。

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
