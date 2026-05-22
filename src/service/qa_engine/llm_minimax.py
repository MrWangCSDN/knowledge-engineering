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
        """流式 yield，stateful filter 剥 <think>...</think> 段。

        状态机：
            in_think=False  → 普通输出，逐 token yield
            in_think=True   → 推理段内，token 全 skip
        buffer：跨 chunk 拼接以检测被切碎的标签（`<thi` + `nk>` 类）。
        快路径：buf 空 + 当前 tok 不含 '<' → 直接 yield 不进 buffer（零延迟）。
        """
        in_think = False
        buf = ""
        async for tok in super().complete_stream(system=system, user=user, **kwargs):
            # 快路径：常规字符 + buf 为空 + 不在 think 段 → 直接 yield（最常见，零延迟）
            if not in_think and not buf and "<" not in tok:
                yield tok
                continue

            # 慢路径：进入 stateful buffer 处理 <think>...</think>
            buf += tok
            while True:
                if not in_think:
                    idx = buf.find("<think>")
                    if idx == -1:
                        # 没找到开标签：找最后一个 '<' 位置，之前的内容安全 yield
                        # 仅保留可能含未完整 '<think>' 标签碎片的尾部
                        last_lt = buf.rfind("<")
                        if last_lt == -1:
                            # buf 完全不含 '<' → 全 yield
                            if buf:
                                yield buf
                            buf = ""
                        else:
                            chunk = buf[:last_lt]
                            if chunk:
                                yield chunk
                            buf = buf[last_lt:]
                        break
                    # 找到 <think> 开标签：前面内容 yield，进入 think 段
                    if idx > 0:
                        yield buf[:idx]
                    buf = buf[idx + len("<think>"):]
                    in_think = True
                else:
                    # 在 think 段：找 </think> 闭标签
                    idx = buf.find("</think>")
                    if idx == -1:
                        # 没闭标签：丢 think 段内已知部分，保留尾部 ≤8 字（'</think>' 长度）防碎片
                        keep = min(len(buf), 8)
                        buf = buf[-keep:] if keep > 0 else ""
                        break
                    # 找到 </think>：跳过整段，从闭标签后继续（可能马上又遇到普通文本）
                    buf = buf[idx + len("</think>"):]
                    in_think = False
        # 流结束后 buf 可能还有残余（最后一段未确定是否标签起点）
        if not in_think and buf:
            yield buf
