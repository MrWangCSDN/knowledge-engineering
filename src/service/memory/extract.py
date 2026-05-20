"""文件式记忆 S4：ReAct 抽取引擎（MemoryExtractor）。

设计：[[文件式记忆重构-设计]] §5。纯逻辑，不依赖 FastAPI；与 vfs.py /
memgen.py / recall.py / service.py 并列。
- 单次 LLM 调用从一轮对话抽取所有可记忆事实（preference/identity/style_feedback）
- 单 JSON 数组输出含 kind / content / supersedes_kind
- identity 类通过 archive/ 子目录归档老 .md 实现 supersede
- 每条 memory 写入独立 try/except 隔离；引擎自身清晰抛错（§5.7 失败由调用方闭包包 try）
- 一轮所有 memory 写完后一次性串接 S2.regenerate + S3.index_changed

仅 S4：不含 S5 会话级文件化、S6 DB→文件迁移、S7 多租户加固、archive/ 召回忽略开关。
"""
# from __future__ import annotations 让 type hints 字符串化，避免运行期类型评估开销
from __future__ import annotations

# stdlib：日志（_log 模块级单例，沿用 KE 既有模式）
import datetime as _dt
import hashlib
import json
import logging
import re
from typing import Any

# S1 存储层：MemoryFS（async API）+ 异常类
from src.service.memory.vfs import MemoryFS, MemoryNotFound, MemoryPathError
# S2 已有 frontmatter / 哈希工具（同包私名 import 合理）
from src.service.memory.memgen import (
    MemoryGen,
    _render_frontmatter,
    _ABSTRACT_SUFFIX,
    _OVERVIEW_NAME,
    _MD_SUFFIX,
)
# S3 召回引擎（写入后 index_changed 同步 Weaviate）
from src.service.memory.recall import MemoryRecaller
# S4 自己的 prompt 常量（在 Step 4 加进 prompts.py）
from src.service.qa_engine.prompts import _MEM_EXTRACT_SYSTEM

# 模块级 logger（与 vfs.py / memgen.py / recall.py 同模式）
_log = logging.getLogger(__name__)

# kind 白名单（§5.3 D2 锁定 taxonomy）
_VALID_KINDS = ("preference", "identity", "style_feedback")
# slug 长度（sha256 hex 前缀，§5.3：抗碰撞 + 确定性 + 同 content 同 slug 幂等）
_SLUG_HEX_LEN = 12
# source 字段值（§5.3：区分 S4 ReAct vs §22 历史 explicit，S6 迁移辨识）
_SOURCE_REACT = "react"
# archive/ 子目录名（§5.4 identity-supersede 归档目录）
_ARCHIVE_DIRNAME = "archive"

# LLM 输出有时包 ```json ... ``` —— 解析前剥掉
_CODE_FENCE_RE = re.compile(r"^`{1,4}(?:json)?\s*(.*?)\s*`{1,4}$", re.DOTALL)


def _compute_slug(content: str) -> str:
    """content → sha256 hex 前 12 字符。

    §5.3 设计：S4 内部生成、不让 LLM 出 slug。
    - 抗碰撞：12-hex = 48-bit 空间，碰撞概率忽略
    - 确定性：同 content 同 slug → 同 path → fs.write 覆盖
    - 幂等天然：同 content 不重写（src_hash 未变，S2 不重生 L0，S3 不重 embed）
    """
    # SHA-256 hex 取前 _SLUG_HEX_LEN（12）字符
    return hashlib.sha256(content.encode("utf-8")).hexdigest()[:_SLUG_HEX_LEN]


def _now_iso_z() -> str:
    """当前 UTC 时间，ISO 8601 Z 格式：YYYY-MM-DDTHH:MM:SSZ。

    与 §22 既有 frontmatter timestamps 格式一致；写入 .md 的 created_at 字段。
    """
    # datetime.utcnow() 已 deprecated（PEP 615 / 3.12 +）；用 timezone-aware 形式
    now = _dt.datetime.now(_dt.timezone.utc)
    # strftime 输出 YYYY-MM-DDTHH:MM:SS；末尾补 Z 表示 UTC
    return now.strftime("%Y-%m-%dT%H:%M:%SZ")


def _parse_react_json(raw: str) -> list[dict]:
    """LLM ReAct 输出 → memories 列表（容错 + 过滤非法）。

    输入：LLM 返回的字符串（可能带 ```json 代码栅栏）
    输出：list[dict] 每 dict 含 kind / content / supersedes_kind 三字段
    非法 JSON / dict 无 memories 字段 → 抛 ValueError（§5.7 引擎自身清晰抛错）
    单条 entry 缺 content / kind 非法 → 跳过该条（容错容忍部分输出）
    """
    # 空字符串 / 全空白直接拒
    s = (raw or "").strip()
    if not s:
        raise ValueError("empty LLM response")
    # 剥 ```json ... ``` 代码栅栏（LLM 偶尔不听 prompt 加栅栏）
    m = _CODE_FENCE_RE.match(s)
    if m:
        s = m.group(1).strip()
    # json.loads 失败抛 JSONDecodeError，转 ValueError 让上层统一兜
    try:
        obj = json.loads(s)
    except json.JSONDecodeError as exc:
        raise ValueError(f"invalid JSON: {exc}") from exc
    # 顶层必须是 dict 且含 memories 列表字段
    if not isinstance(obj, dict):
        raise ValueError(f"expected dict at top, got {type(obj).__name__}")
    memories = obj.get("memories")
    if not isinstance(memories, list):
        raise ValueError(f"missing 'memories' list field, got {type(memories).__name__}")
    # 过滤非法 entry：缺 kind / kind 非白名单 / content 非字符串 / content 空
    out: list[dict] = []
    for item in memories:
        if not isinstance(item, dict):
            _log.debug("parse_react_json: skip non-dict entry %r", item)
            continue
        kind = item.get("kind")
        content = item.get("content")
        sk = item.get("supersedes_kind")
        if kind not in _VALID_KINDS:
            _log.debug("parse_react_json: skip bad kind %r", kind)
            continue
        if not isinstance(content, str) or not content.strip():
            _log.debug("parse_react_json: skip missing/empty content for kind %r", kind)
            continue
        # supersedes_kind 仅允许 "identity" 或 None（§5.2 schema）
        if sk not in ("identity", None):
            sk = None
        out.append({
            "kind": kind,
            "content": content.strip(),
            "supersedes_kind": sk,
        })
    return out


class MemoryExtractor:
    """S4 主引擎：从一轮对话 ReAct 抽取记忆 + 落地全链（§5.1）。

    llm：KE 既有 provider 鸭子接口 ``async complete(system,user,**kw)->str``，
    构造注入便于单测 fake。fs/memgen/recaller 每次调用传入（同一引擎可服务
    不同 MemoryFS 实例 / 测试）。

    引擎自身清晰抛错（LLM/JSON/fs.write/S2/S3 调用失败都 raise）；
    「记忆失败不影响主答」（§22 交接 #2）由调用方 _writer 闭包包 try/except。
    """

    def __init__(self, llm: Any) -> None:
        # 仅持有 llm；fs/memgen/recaller 走 extract_and_persist 形参（§5.1）
        self._llm = llm
