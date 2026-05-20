"""文件式记忆 S3：Weaviate 向量召回 on L0（MemoryRecaller）。

设计：[[文件式记忆重构-设计]] §4。纯逻辑，不依赖 FastAPI；与 vfs.py / memgen.py
/ service.py 并列。
- 把 S2 产出的 .abstract.md（文件 L0 + 目录 L0）灌入 Weaviate memory_l0
  collection（multi-tenancy，tenant=str(user_id)）。
- 召回时 embed 查询向量 → with_tenant(uid).near_vector(top_k)；命中目录 L0
  时同目录 fs.read .overview.md(L1) 展开拼接。
- 哈希幂等：upsert 前比 frontmatter 内 src_hash/inputs_hash，命中跳过零调用。
- 生命周期：fs.exists False → 删 Weaviate 对象；fs.mv 由调用方传 [old,new] 触发。
- S3 自包失败语义：embed/query/L1.read 全 try/except → 返 ""（with_memory_block
  零开销不注入路径），「记忆失败绝不影响主答」在 S3 自身保证。

仅 S3：不含 S4 抽取/接线、S5 会话、S6 迁移、S7 多租户加固、LLM-as-router、
L1 入向量索引、长度守护 / observable counters（独立 hardening pass）。
"""
# from __future__ import annotations 让 type hints 字符串化，避免运行期类型评估开销
from __future__ import annotations

# stdlib：日志（_log 模块级单例，沿用 KE 既有模式）
import logging
# stdlib：UUID 类型（Weaviate 对象 ID 用 UUID）
import uuid as _uuid_mod
# typing：变量类型注解 list[float] 等
from typing import Any

# S1 存储层：MemoryFS（async API）+ MemoryNotFound（fs.read 不存在抛此）
from src.service.memory.vfs import MemoryFS, MemoryNotFound
# S2 已有的 frontmatter / 哈希工具（同包私名 import 合理；避免双份实现）
from src.service.memory.memgen import (
    _split_frontmatter,            # 拆 frontmatter / body（CRLF 归一、YAMLError 自愈）
    _ABSTRACT_SUFFIX,              # ".abstract.md"
    _OVERVIEW_NAME,                # ".overview.md"
)
# KE 既有 Weaviate 客户端基类（持连接 + 多租户配置 + UUID 派生）
from src.knowledge.base_weaviate_store import BaseWeaviateStore
# KE 既有 embedding 入口（同步函数；S3 用 thin async wrapper）
from src.semantic.embedding import get_embedding

# 模块级 logger（与 vfs.py / memgen.py 同模式）
_log = logging.getLogger(__name__)

# 向量维度常量（§4.2 schema 钉死 1024 维，与 _overview.md 项目默认一致）
_VECTOR_DIM = 1024
# Weaviate collection 名（§4.2 设计钉死）
_COLLECTION_NAME = "memory_l0"


def _kind_of_uri(uri: str) -> str:
    """判定 .abstract.md uri 是文件 L0 还是目录 L0。

    - "dir" L0：uri 末段恰为 ".abstract.md"（目录摘要）
    - "file" L0：uri 末段形如 "{slug}.abstract.md"（记忆文件摘要）

    §4.2 schema 的 `kind` property 用此判定。
    """
    # endswith("/" + _ABSTRACT_SUFFIX) 即末段恰为 ".abstract.md"（前面有 "/"）
    # 注：单纯 endswith(_ABSTRACT_SUFFIX) 会让 "x.abstract.md" 也匹配 → 必须加 "/" 前缀区分
    if uri.endswith("/" + _ABSTRACT_SUFFIX):
        return "dir"
    return "file"


def _overview_uri_for_dir_l0(dir_l0_uri: str) -> str:
    """目录 L0 uri → 同目录 .overview.md(L1) uri。

    用于 recall 时 fs.read 展开命中目录的导航图。
    输入约定：dir_l0_uri 必须以 "/.abstract.md" 结尾（调用方确保 _kind_of_uri == "dir"）。
    """
    # 防御性断言：契约要求末段恰为 "/.abstract.md"；若上游调用点 _kind_of_uri
    # 判定漂移导致传入文件 L0 uri，这里抛 AssertionError 而非默默产出错误 uri
    # （如 user-nam/.overview.md from user-name.abstract.md）。零运行时成本。
    assert dir_l0_uri.endswith("/" + _ABSTRACT_SUFFIX), (
        f"_overview_uri_for_dir_l0: uri must end with '/.abstract.md': {dir_l0_uri!r}"
    )
    # 把末段 "/.abstract.md" 替换为 "/.overview.md"
    # 等价于：去掉 "/.abstract.md" 后缀，拼上 "/.overview.md"
    base = dir_l0_uri[: -len("/" + _ABSTRACT_SUFFIX)]   # 去掉 "/.abstract.md"
    return base + "/" + _OVERVIEW_NAME                   # 拼上 "/.overview.md"


class MemoryL0Store(BaseWeaviateStore):
    """`memory_l0` collection 的 schema 子类。

    继承 BaseWeaviateStore 复用：连接管理 / collection 创建 / 默认开启 Multi-Tenancy
    / `_to_uuid(s)` 静态方法。本子类只需提供 `_schema_properties()` 字段定义。
    Properties（§4.2 钉死）：
      - uri  (TEXT)：完整 ke:// 路径，对象逻辑主键
      - kind (TEXT)："file" or "dir"
      - hash (TEXT)：frontmatter 内 src_hash（file）或 inputs_hash（dir），幂等判定基元
      - body (TEXT)：.abstract.md 正文（已脱 frontmatter；≤100 tok）
    """

    def _schema_properties(self) -> list[Any]:
        # 在方法内 import：weaviate.classes.config 仅在 collection 创建期需要，
        # 与 base_weaviate_store.py 同模式
        from weaviate.classes.config import Property, DataType
        return [
            # uri：对象逻辑主键，便于审计 / 删 / mv 对账
            Property(name="uri", data_type=DataType.TEXT),
            # kind：file / dir 区分，便于 recall 时只对 dir 展开 L1
            Property(name="kind", data_type=DataType.TEXT),
            # hash：与 frontmatter 内哈希字段一致 → upsert 前比 → 命中跳过零 embedding
            Property(name="hash", data_type=DataType.TEXT),
            # body：.abstract.md 正文，用于 recall 拼装记忆块（不重复 fs.read）
            Property(name="body", data_type=DataType.TEXT),
        ]


class MemoryRecaller:
    """S3 主引擎：把 L0 灌入 Weaviate + 召回为 system-prompt 记忆块。

    构造注入 embedder + weaviate_client，便于单测 fake。embedder 鸭子接口：
        async def embed(text: str) -> list[float]   # 1024 维
    weaviate_client 鸭子接口（v4 client.collections 子集）：
        collections.get("memory_l0").with_tenant(t).{data,query}.{...}
    """

    def __init__(self, embedder: Any, weaviate_client: Any) -> None:
        # 仅保存引用；不做连接 / schema 初始化（这些由 weaviate_client 携带，
        # 通常已通过 MemoryL0Store(BaseWeaviateStore) 完成）
        self._embedder = embedder
        self._weaviate_client = weaviate_client

    # ── 公开 API #1 ──────────────────────────────────────────────
    async def index_changed(self, fs: MemoryFS, changed_uris: list[str]) -> None:
        """对去重后的 changed_uris 各自做生命周期对账（§4.3）：
        - 仅识别 .abstract.md 后缀的 uri（其余 debug skip）；
        - fs.exists 走「读 frontmatter → 比 hash → 命中跳过 / 不命中 upsert」；
        - 不 exists 走 delete（不存在则静默忽略）。
        单条目失败 try/except → _log.debug → continue（同 S2 §3.5 隔离）。
        """
        # 去重保持稳定顺序（dict.fromkeys 是 Python 3.7+ 保序去重的惯用法）
        seen: set[str] = set()
        ordered: list[str] = []
        for uri in changed_uris:
            if uri in seen:
                continue
            seen.add(uri)
            ordered.append(uri)

        for uri in ordered:
            try:
                # 非 .abstract.md 后缀 → 跳过（debug log，便于追问题）
                if not uri.endswith(_ABSTRACT_SUFFIX):
                    _log.debug("index_changed: skip non-abstract uri %r", uri)
                    continue
                await self._index_one(fs, uri)
            except Exception as exc:           # noqa: BLE001 单条目隔离（与 S2 §3.5 一致）
                _log.debug("index_changed: index failed %r: %r", uri, exc)

    async def _index_one(self, fs: MemoryFS, uri: str) -> None:
        """单 uri 的索引动作：fs.exists 走 upsert / 不存在走 delete。"""
        # 解析 user_id → tenant 字符串
        # vfs._parse_uri 静态方法已校验 uri 合法性，返回 (uid, segs)
        uid, _segs = MemoryFS._parse_uri(uri)
        tenant = uid
        # Weaviate 对象主键 uuid：复用 BaseWeaviateStore._to_uuid（SHA-256[:32] 拼 UUID5）
        obj_uuid = _uuid_mod.UUID(BaseWeaviateStore._to_uuid(uri))

        # 取 collection + tenant view
        coll = self._weaviate_client.collections.get(_COLLECTION_NAME)
        view = coll.with_tenant(tenant)

        if await fs.exists(uri):
            # 走 upsert 路径
            raw = await fs.read(uri)
            meta, body = _split_frontmatter(raw)
            # frontmatter 内哈希：文件 L0 用 src_hash / 目录 L0 用 inputs_hash
            fresh_hash = meta.get("src_hash") or meta.get("inputs_hash")
            if fresh_hash is None:
                # frontmatter 损坏 / 缺失哈希 → 跳过（让上层下轮自愈；S2 自愈机制兜底）
                _log.debug("index_changed: missing hash in frontmatter %r", uri)
                return
            # 把 hash 转 str（防 YAML 把全数字 hash 解析为 int，与 S2 同模式）
            fresh_hash = str(fresh_hash)

            # 查既有对象：命中且 hash 相同 → 跳过（零 embedding API 调用）
            existing = view.query.fetch_object_by_id(obj_uuid)
            if existing is not None and existing.properties.get("hash") == fresh_hash:
                _log.debug("index_changed: hash hit, skip %r", uri)
                return

            # 不命中：调 embedder 取向量
            vec = await self._embedder.embed(body.strip())
            # 整理 properties
            kind = _kind_of_uri(uri)
            props = {
                "uri": uri,
                "kind": kind,
                "hash": fresh_hash,
                "body": body,                  # 保留 frontmatter 后的完整 body（含末尾 "\n"）
            }
            # 已存在则 replace；不存在则 insert（fake 这两个语义相同；真 v4 client 行为有别）
            if existing is None:
                view.data.insert(uuid=obj_uuid, properties=props, vector=vec)
            else:
                view.data.replace(uuid=obj_uuid, properties=props, vector=vec)
        else:
            # 文件不存在 → 删 Weaviate 对象（v4 client 对不存在的 uuid 是静默忽略）
            view.data.delete_by_id(obj_uuid)


class _DefaultEmbedder:
    """KE 既有 get_embedding 的 thin async wrapper。

    get_embedding 是同步函数（src/semantic/embedding.py:19），底层走 urllib.urlopen
    (Ollama HTTP) timeout=60s。直接在 async def 里 return 会阻塞事件循环最长 60s，
    挡掉所有并发协程（多租户召回 / S4 post-turn 索引等）。用 asyncio.to_thread
    把同步调用放线程池，立刻让出事件循环。stdlib-only，零额外依赖。
    """

    async def embed(self, text: str) -> list[float]:
        # 用 asyncio.to_thread 把同步阻塞调用搬到线程池，主事件循环立刻让出
        import asyncio
        return await asyncio.to_thread(get_embedding, text, _VECTOR_DIM)
