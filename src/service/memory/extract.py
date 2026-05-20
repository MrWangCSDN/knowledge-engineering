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

# S1 存储层：MemoryFS（async API）+ 异常类（_supersede_identity 用）
from src.service.memory.vfs import MemoryFS, MemoryNotFound, MemoryPathError
# S2 已有 frontmatter / 哈希工具 + 路径常量（同包私名 import 合理）；
# _OVERVIEW_NAME 用于 _supersede_identity 筛 dir 内 .overview.md 子项
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
    # datetime.utcnow() 自 Python 3.12 起 deprecated（无 PEP，CPython 3.12 changelog）；
    # 改用 timezone-aware 形式以兼容未来 Python 版本
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

    # ── 公开唯一 API ─────────────────────────────────────────────
    async def extract_and_persist(
        self,
        fs: MemoryFS,
        memgen: MemoryGen,
        recaller: MemoryRecaller,
        *,
        user_id: int,
        turn_text: str,
    ) -> None:
        """ReAct 抽取本轮记忆 → 写 .md → 一次性串接 S2.regenerate + S3.index_changed。

        §5.7 失败语义：本方法自身清晰抛错（LLM/JSON 调用失败 raise）；
        单条 memory 写入失败独立 try/except 隔离（_log.debug 跳过该条）；
        「记忆失败不影响主答」由调用方 _writer 闭包包 try/except 兜。
        """
        # 1) 调 LLM 跑 ReAct 抽取（_MEM_EXTRACT_SYSTEM 在 prompts.py）
        raw = await self._llm.complete(
            system=_MEM_EXTRACT_SYSTEM,
            user=turn_text,
        )
        # 2) 解析 JSON（容错过滤；非法 JSON / dict 无 memories 抛 ValueError）
        memories = _parse_react_json(raw)
        # 3) 空列表（绝大多数闲聊轮）→ 零 fs.write、零 S2/S3 调用，直接 return
        if not memories:
            _log.debug("extract: empty memories for user %d (闲聊轮)", user_id)
            return
        # 4) 收集本轮所有写入的 uri（含 supersede 归档路径），最终一次性传给 S2/S3
        changed_uris: list[str] = []
        # 5) 对每条 memory 独立 try/except 写入（单条失败不连累其他；§5.7 隔离）
        for mem in memories:
            try:
                await self._write_one_memory(fs, user_id, mem, changed_uris)
            except Exception as exc:           # noqa: BLE001 单条目隔离
                _log.debug(
                    "extract: memory write failed kind=%s content=%r: %r",
                    mem.get("kind"), mem.get("content"), exc,
                )
        # 6) 若有任何 .md 被写入 / mv，调一次 S2.regenerate + 一次 S3.index_changed
        #    （非每条调一次：batched 大幅省 LLM 与 embedding 调用）
        if changed_uris:
            await memgen.regenerate(fs, changed_uris)
            # S3.index_changed 只消费 .abstract.md uri；S2 已产出它们。
            # 从 changed_uris（.md 记忆文件）推导出对应的 abstract uri 列表：
            #   ① 每条文件 .abstract.md：{slug}.md → {slug}.abstract.md
            #   ② 每个祖先目录 .abstract.md：{dir}/.abstract.md
            # _ancestor_dirs 是 MemoryGen 的静态方法，可直接复用。
            abstract_uris: list[str] = [
                u[: -len(_MD_SUFFIX)] + _ABSTRACT_SUFFIX
                for u in changed_uris
            ]
            for dir_uri in memgen._ancestor_dirs(changed_uris):
                abstract_uris.append(dir_uri + "/" + _ABSTRACT_SUFFIX)
            await recaller.index_changed(fs, abstract_uris)

    async def _write_one_memory(
        self,
        fs: MemoryFS,
        user_id: int,
        memory: dict,
        changed_uris: list[str],
    ) -> None:
        """写入单条 memory（含 identity-supersede 归档前置）。

        identity 类（supersedes_kind="identity"）：先 _supersede_identity 归档同
        kind 旧 .md 到 archive/ 子目录、把 src+dst 加入 changed_uris；再写新 .md。
        其他类（preference / style_feedback）：直接写新 .md，不触归档。

        路径：ke://u/{uid}/global/{kind}/{slug}.md
        frontmatter：{kind, slug, source: "react", created_at: <ISO Z>}
        body：content + "\\n"
        """
        kind = memory["kind"]
        content = memory["content"]
        # 路径：先算 slug；identity-supersede 也需要 new_slug 来判断"是否归档自己"
        slug = _compute_slug(content)
        uri = f"ke://u/{user_id}/global/{kind}/{slug}.md"
        # identity-supersede（§5.4）：先归档同 kind=identity 旧 .md 到 archive/
        if memory.get("supersedes_kind") == "identity":
            archived = await self._supersede_identity(fs, user_id, slug)
            # 归档产生的 src/dst 都加入 changed_uris（S3 自洽对账）
            changed_uris.extend(archived)
        # frontmatter（dict 保序：kind 在前，便于人读）
        meta = {
            "kind": kind,
            "slug": slug,
            "source": _SOURCE_REACT,
            "created_at": _now_iso_z(),
        }
        # body：content + "\n"（与 §22 既有惯例一致）
        body = content + "\n"
        # _render_frontmatter 是 S2 已有的：序列化 frontmatter 拼回 markdown
        text = _render_frontmatter(meta, body)
        # 写入（fs.write 是原子的：tempfile.mkstemp + os.replace）
        await fs.write(uri, text)
        changed_uris.append(uri)

    async def _supersede_identity(
        self,
        fs: MemoryFS,
        user_id: int,
        new_slug: str,
    ) -> list[str]:
        """归档同 user 同 kind=identity 的所有旧 .md（slug != new_slug）至 archive/。

        §5.4 设计：
        - 取 identity 目录直接子项
        - 筛真记忆 .md（非 .abstract.md / .overview.md / 末段恰 .md / slug != new_slug）
        - 对每个旧 .md `fs.mv` 到 archive/ 子目录
        - 返回所有发生 mv 的 uri 列表（src + dst），供 changed_uris 收集

        S3.index_changed 见 src 不 exists 删 / 见 dst exists 加 — 自洽。
        """
        # 用户的 identity 目录路径
        base = f"ke://u/{user_id}/global/identity"
        # ls 取直接子项；目录不存在抛 MemoryNotFound（首次写 identity 时正常）；
        # 目录路径解析到非目录抛 MemoryPathError（上游脏数据极端情况）→ 同样视为
        # "无旧可归档"，让新 .md 正常写入；不传播以免单轮记忆整条丢失
        try:
            entries = await fs.ls(base)
        except (MemoryNotFound, MemoryPathError):
            return []

        changed: list[str] = []
        for name in entries:
            # 跳过 S2 生成的 .abstract.md / .overview.md
            if name in (_ABSTRACT_SUFFIX, _OVERVIEW_NAME):
                continue
            if name.endswith(_ABSTRACT_SUFFIX):     # 子文件 L0：{slug}.abstract.md
                continue
            # 非 .md 后缀 → 视为子目录（如 archive/）；不归档子目录本身
            if not name.endswith(_MD_SUFFIX):
                continue
            # 提取 slug（末段去 .md）
            slug = name[: -len(_MD_SUFFIX)]
            if slug == new_slug:
                # 新写入的不归档（同 content 幂等场景）
                continue
            # mv 到 archive/ 子目录（同名文件）
            src = f"{base}/{name}"
            dst = f"{base}/{_ARCHIVE_DIRNAME}/{name}"
            try:
                await fs.mv(src, dst)
            except Exception as exc:           # noqa: BLE001 单条 mv 隔离，与 extract_and_persist 外层模式一致
                # mv 失败（vfs 异常 / 文件系统错误如 OSError / 权限等）— 单条目隔离，记 debug 跳过；
                # 不传播以免 for-loop 中断、_supersede_identity 整体抛错丢失 partial changed 列表
                _log.debug(
                    "extract: archive mv failed src=%r dst=%r: %r",
                    src, dst, exc,
                )
                continue
            # 记录这次 mv 的 src + dst（供 S3.index_changed 对账：
            # src 不 exists → delete Weaviate 对象 / dst exists → upsert）
            changed.append(src)
            changed.append(dst)
            # 同 slug 的 sibling .abstract.md 也必须 archive：否则会留下 orphan：
            # ① S2 _gen_dir_l0_l1 会把旧 abstract 混入 identity 目录 L1，污染新身份
            # ② S3 index_changed 见 orphan abstract 存在 + hash 不变 → 哈希命中跳过
            #    → 旧 Weaviate 向量永不删除 → recall 返回 stale 内容 → 重现「王山河→李龙飞」
            #    leak bug。修复 S4 holistic review Critical 发现。
            abs_name = slug + _ABSTRACT_SUFFIX
            abs_src = f"{base}/{abs_name}"
            abs_dst = f"{base}/{_ARCHIVE_DIRNAME}/{abs_name}"
            try:
                await fs.mv(abs_src, abs_dst)
            except (MemoryNotFound, MemoryPathError):
                # 旧 abstract 缺失（首次写入 .md 时 S2 尚未生成；或被外部清理）
                # 视作"无需 archive"，让 S2/S3 后续重生即可。不是错误。
                continue
            except Exception as exc:           # noqa: BLE001 同 .md mv 隔离模式
                _log.debug(
                    "extract: archive abstract mv failed src=%r dst=%r: %r",
                    abs_src, abs_dst, exc,
                )
                continue
            changed.append(abs_src)
            changed.append(abs_dst)
        return changed
