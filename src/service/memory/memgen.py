"""文件式记忆 S2：L0/L1 自底向上生成管线（MemoryGen）。

设计：[[文件式记忆重构-设计]] §3。纯逻辑，不依赖 FastAPI（与 vfs.py 并列）。
- L0 = `.abstract.md`（≤100 tok，S3 向量检索目标）：记忆文件与目录都有。
- L1 = `.overview.md`（≤1–2k tok，导航图）：仅目录有。
- 自底向上：先各变更记忆文件 L0，再其祖先目录按深度降序逐级 L0+L1。
- 内容哈希（SHA-256 hex 存 frontmatter）幂等跳过：哈希一致零 LLM；
  S6 整树 / 崩溃中途下一轮按哈希只补不一致项 = 幂等自愈，无 mtime。
仅 S2：不含 S3 召回/向量、S4 抽取/接线、S5 会话、S6 迁移、跨实例。
"""
from __future__ import annotations

import hashlib
import logging

import yaml  # pyproject 已声明依赖 pyyaml>=6.0

from src.service.memory.vfs import MemoryFS, MemoryNotFound
from src.service.qa_engine.prompts import _MEM_L0_SYSTEM, _MEM_L1_SYSTEM

_log = logging.getLogger(__name__)

# 生成文件名（固定，§3.3）：
#   记忆文件 {slug}.md  → 同目录 {slug}.abstract.md（L0）
#   目录              → 目录内 .abstract.md（L0）+ .overview.md（L1）
_ABSTRACT_SUFFIX = ".abstract.md"
_OVERVIEW_NAME = ".overview.md"
_MD_SUFFIX = ".md"


def _sha256_hex(text: str) -> str:
    """文本 UTF-8 的 SHA-256 十六进制摘要。

    纯内容派生的陈旧判定基元（§3.3）：无 mtime、无时钟，
    同输入恒同输出 → S6 整树/崩溃中途下一轮按哈希自愈、幂等。
    """
    # hashlib.sha256 接收 bytes，故先按 UTF-8 编码；hexdigest() 返回 64 位小写十六进制 str
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _split_frontmatter(text: str) -> tuple[dict, str]:
    """拆 ``---\\nYAML\\n---\\n正文`` → (meta dict, 正文 str)。

    不以 ``---\\n`` 起（无 frontmatter）→ ``({}, 原文)``；
    YAML 段为空或非 dict → ``({}, 正文)``。用 PyYAML 安全解析
    （``yaml.safe_load`` 只认基本类型，杜绝任意对象构造）。

    入口自动将 CRLF/CR 归一为 LF，故 S4/S6 迁移或外部撰写的 ``\\r\\n``
    文件仍能正确探测 frontmatter，Task 2 的 ``src_hash`` 跨行尾恒同（§3.3）。
    约定（§3.2）：frontmatter 为简单 ``key: value``；S2 生成文件天然满足，
    S4/S6 记忆正文使用 ``## `` 节标题，不会产生裸 ``\\n---`` 行，无冲突。
    ``body`` 末尾换行由调用方约定（传入 ``summary.strip() + "\\n"``），
    本函数不增减。
    """
    # CRLF/CR → LF 归一（置于最前）：S4/S6 迁移/外部撰写的 .md 可能是 \r\n，
    # 不归一则 frontmatter 探测失败、Task 2 会把 frontmatter+正文一起算进
    # src_hash，破坏 §3.3「src_hash 仅覆盖正文」的幂等不变量。归一后正文
    # 行尾也稳定（同一逻辑内容跨行尾恒同哈希）。
    text = text.replace("\r\n", "\n").replace("\r", "\n")
    # 没有起始分隔符 → 视为纯正文
    if not text.startswith("---\n"):
        return {}, text
    rest = text[4:]                       # 去掉开头的 "---\n"
    end = rest.find("\n---")              # 第一处 "\n---" 即 frontmatter 闭合
    if end == -1:                         # 没有闭合 → 容错为纯正文
        return {}, text
    yaml_src = rest[:end]                 # 两个 --- 之间的 YAML 源
    after = rest[end + 4:]               # 跳过 "\n---" 之后
    # 闭合行后常跟一个换行（"---\n正文"），剥掉它得到纯正文
    if after.startswith("\n"):
        after = after[1:]
    # 空 YAML 段不调用 safe_load（避免 None）
    meta = yaml.safe_load(yaml_src) if yaml_src.strip() else {}
    # safe_load 可能返回非 dict（如纯标量）→ 归一为 {}
    if not isinstance(meta, dict):
        meta = {}
    return meta, after


def _render_frontmatter(meta: dict, body: str) -> str:
    """``(meta, body)`` → ``---\\nYAML\\n---\\n{body}``。

    ``sort_keys=False`` 保持插入序；``allow_unicode=True`` 让中文按原样
    输出（不转义成 ``\\uXXXX``，便于人读与 S3 向量化）。
    """
    # yaml.safe_dump 输出已自带末尾换行，形如 "src_hash: abc\n"
    fm = yaml.safe_dump(meta, sort_keys=False, allow_unicode=True)
    # f-string 拼装：开分隔符 + YAML + 闭分隔符 + 正文
    return f"---\n{fm}---\n{body}"


class MemoryGen:
    """L0/L1 自底向上生成引擎（§3.1）。

    llm：KE 既有 provider 鸭子接口 ``async complete(system,user,**kw)->str``，
    构造注入便于单测 fake。fs 每次调用传入（同一引擎可服务不同 MemoryFS
    实例/测试）。S2 不自起后台任务、不接 SSE（异步点由 S4/S6 持有）。
    """

    def __init__(self, llm) -> None:
        # 仅持有 llm；fs 走 regenerate 形参（§3.1）
        self._llm = llm
