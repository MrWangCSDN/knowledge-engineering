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
