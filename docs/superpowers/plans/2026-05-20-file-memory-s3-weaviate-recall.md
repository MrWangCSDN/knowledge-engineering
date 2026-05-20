# 文件式记忆重构 — S3：Weaviate 向量召回 on L0 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 把 S2 产出的 `.abstract.md`（文件 L0 + 目录 L0）灌入 Weaviate `memory_l0` collection 做向量召回，命中目录 L0 时同目录 `.overview.md`(L1) 经 `fs.read` 展开；drop-in 替换 §22 DB-backed `recall_memory_block` 的 contract（同名同形态返回 str，注入链零改动）。

**Architecture:** 新模块 `src/service/memory/recall.py` 同时持有 `MemoryL0Store(BaseWeaviateStore)`（subclass，复用 KE Weaviate 客户端创建 + Multi-Tenancy 配置）与 `MemoryRecaller`（高层引擎，构造注入 embedder + weaviate_client，两个公开 async API：`index_changed(fs, changed_uris)` 与 `recall_memory_block(fs, query, user_id, *, top_k=5)`）。S3 自包失败语义（embed/query/hit-assemble 全 try → 返 `""`），S2 不知 S3、S3 不知 S2，由 S4/S6 在 post-turn / migration 点顺序串接。

**Tech Stack:** Python 3.10+，stdlib + 项目已声明依赖（`pyyaml>=6.0`）+ **新增** `weaviate-client>=4.4` 显式声明；S1 `MemoryFS`（`src/service/memory/vfs.py`）；S2 `_split_frontmatter`/`_render_frontmatter`/`_sha256_hex` 复用 import（`src/service/memory/memgen.py`）；KE `BaseWeaviateStore`（`src/knowledge/base_weaviate_store.py`）；KE `get_embedding`（`src/semantic/embedding.py`，sync → S3 thin async wrapper）。测试 pytest + `pytest-asyncio`，`./venv/bin/python -m pytest`（homebrew python3 无 pytest-asyncio）。

**单一来源设计：** Obsidian `/Users/java/obsidian/01 Engineering/knowledge-engineering/文件式记忆重构-设计.md` §4（§4.0–§4.8）。本计划不得引入 §4 之外的设计决策。

**仓库 / 分支：** `/Users/java/knowledge-engineering-auth` @ `release-0513`（S1+S2 已落盘 HEAD `29628ff`；本计划逐任务 commit 已授权；push/merge/部署须用户拍板，**不在本计划范围**）。

**测试命令前缀（务必用仓库 venv）：** `cd /Users/java/knowledge-engineering-auth && ./venv/bin/python -m pytest`

---

## File Structure

| 文件 | 职责 | 创建/修改 |
|---|---|---|
| `src/service/memory/recall.py` | `MemoryL0Store(BaseWeaviateStore)` collection schema + `MemoryRecaller` 引擎（含 `_DefaultEmbedder` 默认实现）+ helpers `_kind_of_uri` / `_overview_uri_for_dir_l0`。 | **Create** |
| `src/service/memory/service.py` | `recall_memory_block` 第 97 行起整函数替换为新签名 + 委托 S3。不动其他记忆函数（`detect_explicit_memory` / `write_explicit_memory` / `maybe_compact_session` / `parse_user_memory_intent` 等）。 | Modify (line 97-section) |
| `src/service/qa_router.py` | 第 277-279 行调用点改 1 行（DB 参数换 `fs+query`）。 | Modify (line 277-279) |
| `tests/test_auth/test_memory_recall.py` | S3 全部测试（§4.7 十二条场景）。fake embedder + fake weaviate + 真 `MemoryFS(root=tmp_path)`。 | **Create** |
| `tests/test_auth/test_memory_service.py` | 删除 §22 旧 `recall_memory_block` 相关测试函数（`test_recall_empty_when_nothing` 等，其底层 DB 三层语义已被 S3 替换）。其他记忆测试保留不动。 | Modify (delete obsolete tests) |
| `pyproject.toml` | `[project.dependencies]` 显式追加 `weaviate-client>=4.4`（既有 KE Weaviate 模块运行时依赖，原先隐式存在 venv 中，未声明）。 | Modify (line 11-21 dependencies block) |

**边界（§4.6 YAGNI，不做）：** S4 ReAct 抽取 + 轮末接线、S5 会话级、S6 DB → 文件迁移、S7 多租户加固、长度守护 / observable counters、LLM-as-router 多跳召回、L1 入向量索引、S2/S3 互引（仍由 S4/S6 顺序调）。

**关键既有接口（已核对真实代码，照此调用，勿臆造）：**
- `from src.service.memory.vfs import MemoryFS, MemoryNotFound`：`MemoryFS(root: str | None)`；`async read/write/exists/ls/rm/mv`；同步 `resolve`；异常 `MemoryPathError` / `MemoryNotFound`；`_parse_uri` 静态方法解析 `ke://u/{uid}/...` 返回 `(uid, segs)`。
- `from src.service.memory.memgen import _split_frontmatter, _render_frontmatter, _sha256_hex, _ABSTRACT_SUFFIX, _OVERVIEW_NAME`：S2 已实现，**S3 直接 import 复用**（同包内 import 私名合理；CRLF 已归一；YAML 解析对损坏 frontmatter 已 `yaml.YAMLError → {}` 自愈）。
- `from src.knowledge.base_weaviate_store import BaseWeaviateStore`：抽象基类，`__init__(*, url, grpc_port, collection_name, dimension, api_key=None)` 同步打开 Weaviate 连接、检查 / 创建 collection、默认启 Multi-Tenancy。子类只需实现 `_schema_properties() -> list[Property]`。`_to_uuid(s: str) -> str` 静态方法在 line 30-33（SHA-256[:32] 拼 UUID5 格式）—— S3 **复用此方法**，不自造 UUID 派生。
- `from src.semantic.embedding import get_embedding`：`get_embedding(text: str, dimension: int = 1024) -> list[float]`，**同步**；空文本返 `[0.0]*dimension`；失败 fallback 到 `_hash_vector(text, dim)`（确定性伪向量，便于测试复现）。**S3 用 thin async wrapper**：`async def embed(self, text): return get_embedding(text, 1024)`。
- 测试 fake LLM 形态（与 S2 一致）：`class _Fake...: async def complete(self, *, system: str, user: str, **kw) -> str:` —— S3 不直接用 LLM，但 fake embedder 模式相似。

---

### Task 1: 模块骨架 + Weaviate collection schema + helpers + `MemoryRecaller.__init__`

**Files:**
- Create: `src/service/memory/recall.py`
- Modify: `pyproject.toml`（line 11-21 dependencies block 加 `weaviate-client>=4.4`）
- Create: `tests/test_auth/test_memory_recall.py`

- [ ] **Step 1: 在 pyproject.toml 追加 `weaviate-client>=4.4` 到 `[project.dependencies]`**

读 `/Users/java/knowledge-engineering-auth/pyproject.toml` 找到 `[project] dependencies` 列表（line 11-21）。在最后一项 `"streamlit-autorefresh>=0.0.6",` 后追加一行：

```toml
    "weaviate-client>=4.4",
```

完成后该块应形如：

```toml
dependencies = [
    "pydantic>=2.0",
    "pydantic-settings>=2.0",
    "pyyaml>=6.0",
    "networkx>=3.0",
    "fastapi>=0.100.0",
    "uvicorn[standard]>=0.22.0",
    "httpx>=0.24.0",
    "streamlit>=1.28.0",
    "streamlit-autorefresh>=0.0.6",
    "weaviate-client>=4.4",
]
```

验证：`cd /Users/java/knowledge-engineering-auth && ./venv/bin/python -c "import weaviate; print('weaviate', weaviate.__version__)"` —— 期望输出形如 `weaviate 4.x.x`（已在 venv 中可用；本步骤只是让依赖显式声明）。

- [ ] **Step 2: 写失败测试（纯函数 helpers：`_kind_of_uri` / `_overview_uri_for_dir_l0`）**

创建 `tests/test_auth/test_memory_recall.py`，内容：

```python
"""文件式记忆 S3：Weaviate 向量召回测试。设计：[[文件式记忆重构-设计]] §4。

fake embedder + fake weaviate + 真 MemoryFS(root=tmp_path)，沿用 tests/test_auth
既有 fake / tmp_path / @pytest.mark.asyncio 风格。
"""
# 导入 pytest（项目测试框架，pytest-asyncio 在 venv 中已安装）
import pytest

# 从 S1 vfs 导入：真 MemoryFS（其 root 由 tmp_path 注入做隔离）
from src.service.memory.vfs import MemoryFS
# 从 S2 memgen 导入：frontmatter 工具与哈希函数，S3 测试用来构造 .abstract.md 内容
from src.service.memory.memgen import (
    _split_frontmatter,           # 拆 frontmatter / body（CRLF 归一、YAMLError 自愈）
    _render_frontmatter,           # 用 PyYAML 序列化 frontmatter 拼回 markdown
    _sha256_hex,                   # 字符串 → SHA-256 hex（计算 src_hash / inputs_hash）
    _ABSTRACT_SUFFIX,              # ".abstract.md" 常量
    _OVERVIEW_NAME,                # ".overview.md" 常量
)
# 从被测模块导入（本 Task 实现）
from src.service.memory.recall import (
    MemoryRecaller,                # S3 主引擎（含 index_changed / recall_memory_block）
    MemoryL0Store,                 # Weaviate collection schema 子类
    _kind_of_uri,                  # helper：判定 uri 是 "file" / "dir" L0
    _overview_uri_for_dir_l0,      # helper：dir L0 uri → 同目录 overview uri
)


def _fs(tmp_path):
    """tests 通用 fixture：用 tmp_path 给 MemoryFS 提供隔离根目录。"""
    # MemoryFS 接受 str；pytest 的 tmp_path 是 pathlib.Path，str() 即可
    return MemoryFS(root=str(tmp_path))


# ── Task 1：纯函数 helpers ───────────────────────────────────────
def test_kind_of_uri_file_vs_dir():
    """判定 .abstract.md uri 是文件 L0（带 slug 前缀）还是目录 L0（裸 .abstract.md）。"""
    # 目录 L0：末段恰为 ".abstract.md"
    assert _kind_of_uri("ke://u/7/global/identity/.abstract.md") == "dir"
    # 文件 L0：末段为 "{slug}.abstract.md"
    assert _kind_of_uri("ke://u/7/global/identity/user-name.abstract.md") == "file"
    # 租户根目录 L0 也属于 "dir"（uri 末段仍是裸 ".abstract.md"）
    assert _kind_of_uri("ke://u/7/.abstract.md") == "dir"


def test_overview_uri_for_dir_l0():
    """目录 L0 uri → 同目录 .overview.md uri（用于 recall 时 fs.read L1 展开）。"""
    # 标准 identity 目录
    assert _overview_uri_for_dir_l0(
        "ke://u/7/global/identity/.abstract.md"
    ) == "ke://u/7/global/identity/.overview.md"
    # 租户根目录
    assert _overview_uri_for_dir_l0(
        "ke://u/7/.abstract.md"
    ) == "ke://u/7/.overview.md"


def test_memory_recaller_construction_accepts_embedder_and_client():
    """MemoryRecaller 构造注入 embedder + weaviate_client，便于 fake 测试。"""
    # 用 None 占位（本步不调任何方法、只验证签名）；后续测试用真 fake
    rec = MemoryRecaller(embedder=None, weaviate_client=None)
    # __init__ 仅保存引用，不应抛错
    assert rec._embedder is None
    assert rec._weaviate_client is None
```

- [ ] **Step 3: 跑测试确认失败**

Run: `cd /Users/java/knowledge-engineering-auth && ./venv/bin/python -m pytest tests/test_auth/test_memory_recall.py -q`
Expected: FAIL — `ModuleNotFoundError: No module named 'src.service.memory.recall'`（collection error）。

- [ ] **Step 4: 创建 `src/service/memory/recall.py` 骨架（helpers + `MemoryL0Store` + `MemoryRecaller.__init__`）**

```python
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


class _DefaultEmbedder:
    """KE 既有 get_embedding 的 thin async wrapper。

    get_embedding 是同步函数（src/semantic/embedding.py:19）；S3 内一律 await embed(...)
    保持调用面一致。注：embedding 实际通过 Ollama HTTP 调用，不是 CPU 密集 → 同步
    包裹在 async 函数里语义上等价（不阻塞事件循环超过单次 HTTP 调用时长）。
    """

    async def embed(self, text: str) -> list[float]:
        # 直接调用 KE 同步函数，返回 1024 维向量
        return get_embedding(text, _VECTOR_DIM)
```

- [ ] **Step 5: 跑测试确认通过**

Run: `cd /Users/java/knowledge-engineering-auth && ./venv/bin/python -m pytest tests/test_auth/test_memory_recall.py -q`
Expected: PASS（3 passed）。

- [ ] **Step 6: import 自检（确认 weaviate-client 在 venv、S3 模块可被加载）**

Run: `cd /Users/java/knowledge-engineering-auth && ./venv/bin/python -c "import weaviate; from src.service.memory.recall import MemoryRecaller, MemoryL0Store, _kind_of_uri, _overview_uri_for_dir_l0, _DefaultEmbedder; print('weaviate', weaviate.__version__); print('ok')"`
Expected: 输出形如 `weaviate 4.x.x\nok`。

- [ ] **Step 7: Commit**

```bash
cd /Users/java/knowledge-engineering-auth && git add pyproject.toml src/service/memory/recall.py tests/test_auth/test_memory_recall.py && git commit -m "$(cat <<'EOF'
feat(memory-s3): MemoryRecaller 骨架 + memory_l0 schema + helpers

新增 src/service/memory/recall.py：MemoryL0Store(BaseWeaviateStore) 提供
collection schema（uri/kind/hash/body 4 个 TEXT property + 1024 维 self-
provided 向量 + 多租户）；MemoryRecaller(__init__ 构造注入 embedder+
weaviate_client)；helpers _kind_of_uri / _overview_uri_for_dir_l0；
_DefaultEmbedder thin async wrapper around KE get_embedding。pyproject
显式声明 weaviate-client>=4.4 运行时依赖。设计：文件式记忆重构-设计 §4.1/§4.2。

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>
EOF
)" && git status
```

---

### Task 2: `index_changed` 算法（upsert + delete + 哈希幂等 + 单条目失败隔离）

**Files:**
- Modify: `src/service/memory/recall.py`（在 `MemoryRecaller` 内追加 `index_changed` 与私有 helper）
- Modify: `tests/test_auth/test_memory_recall.py`（追加 fake embedder + fake weaviate + 7 个场景测试：①②③④⑤⑨⑩）

实现 §4.7 场景 ①（单文件 L0 索引）/②（哈希幂等零增量）/③（正文变更重生）/④（fs 文件不存在→删 Weaviate）/⑤（fs.mv 经 [old,new] 对账）/⑨ 文件层（embed 抛错隔离）/⑩（S6 整树场景）。

- [ ] **Step 1: 在测试文件追加 `_FakeEmbedder` + `_FakeWeaviate` 假体**

在 `tests/test_auth/test_memory_recall.py` 末尾追加：

```python
# ── Task 2：fake embedder / fake weaviate（in-memory，模拟 v4 client 子集）─
class _FakeEmbedder:
    """固定/确定性 embedder：text → 1024 维向量；记录 calls 计数验证幂等零增量。"""
    def __init__(self):
        # 记录所有 embed 调用次数（验证「同输入再调零增量」）
        self.calls = 0
        # 记录最近一次 embed 入参（验证传入文本是 .abstract.md 正文）
        self.last_text = None

    async def embed(self, text: str) -> list[float]:
        # 计数 +1（每次 embed 调用都计；用于断言幂等零增量）
        self.calls += 1
        # 记录最近入参，便于断言
        self.last_text = text
        # 确定性 1024 维向量：text 的 hash 拆成 16 个 64-bit int，依次映射前 16 维
        # 其他维 0.0；同输入恒同向量；不同输入足够区分（hash 散列）
        import hashlib
        h = hashlib.sha256(text.encode("utf-8")).digest()
        # 前 16 维 = 16 字节 × 4（让 hash 占满前 64 维），后 960 维 = 0
        vec = [0.0] * 1024
        for i, b in enumerate(h[:16]):
            # 字节 [0,255] 归一到 [-1, 1]
            vec[i] = (b / 127.5) - 1.0
        return vec


class _FakeTenantData:
    """tenant-scoped .data 命名空间：insert / replace / delete_by_id。"""
    def __init__(self, tenant_store: dict):
        # tenant_store: uuid -> {"properties": {...}, "vector": [...]}
        self._store = tenant_store

    def insert(self, *, uuid, properties, vector):
        # weaviate v4: insert 用关键字 uuid / properties / vector
        # 若 uuid 已存在，v4 抛 ObjectAlreadyExistsError；fake 这里宽松：直接覆盖
        # （S3 实现是先 fetch 判 hash 再决定 insert/replace，故 fake 此处不强校）
        self._store[str(uuid)] = {"properties": dict(properties), "vector": list(vector)}

    def replace(self, *, uuid, properties, vector):
        # replace 行为同 insert 在 fake 里（v4 真实 client 会校验 uuid 已存在）
        self._store[str(uuid)] = {"properties": dict(properties), "vector": list(vector)}

    def delete_by_id(self, uuid):
        # delete 不存在则静默忽略（v4 client 行为：不存在不抛）
        self._store.pop(str(uuid), None)


class _FakeQueryResult:
    """near_vector 返回值占位：.objects 是 hit 列表，每 hit 有 .properties / .uuid。"""
    def __init__(self, objects):
        self.objects = objects


class _FakeHit:
    """单个 hit 对象：.uuid / .properties / .vector / .metadata.distance。"""
    def __init__(self, uuid, properties, vector, distance):
        self.uuid = uuid
        self.properties = properties
        self.vector = vector
        # v4 把 distance 放 metadata；fake 简化：仅暴露 distance 数值
        # S3 实现读 hit.properties["uri"/"kind"/"body"]，不读 distance
        self.metadata = type("M", (), {"distance": distance})()


class _FakeTenantQuery:
    """tenant-scoped .query 命名空间：fetch_object_by_id / near_vector。"""
    def __init__(self, tenant_store: dict):
        self._store = tenant_store

    def fetch_object_by_id(self, uuid):
        # 返回对象快照（含 properties），不存在返 None（v4 真客户端行为一致）
        rec = self._store.get(str(uuid))
        if rec is None:
            return None
        return _FakeHit(uuid=str(uuid), properties=rec["properties"],
                        vector=rec["vector"], distance=0.0)

    def near_vector(self, near_vector, limit=5):
        # 按 cosine 相似度排序，返回 top-k；fake 用简单点积（向量已归一假设）
        # 余弦相似度 = 点积（两向量都归一）；distance = 1 - similarity
        import math
        q = list(near_vector)
        q_norm = math.sqrt(sum(x * x for x in q)) or 1.0
        scored = []
        for uuid, rec in self._store.items():
            v = rec["vector"]
            v_norm = math.sqrt(sum(x * x for x in v)) or 1.0
            dot = sum(a * b for a, b in zip(q, v))
            cos = dot / (q_norm * v_norm)
            distance = 1.0 - cos
            scored.append((distance, uuid, rec))
        # 升序：distance 越小越相似
        scored.sort(key=lambda t: t[0])
        objects = [
            _FakeHit(uuid=u, properties=r["properties"], vector=r["vector"], distance=d)
            for d, u, r in scored[:limit]
        ]
        return _FakeQueryResult(objects)


class _FakeCollectionTenantView:
    """tenant-scoped collection view，由 collection.with_tenant(t) 返回。"""
    def __init__(self, tenant_store: dict):
        # data 命名空间（insert / replace / delete_by_id）
        self.data = _FakeTenantData(tenant_store)
        # query 命名空间（fetch_object_by_id / near_vector）
        self.query = _FakeTenantQuery(tenant_store)


class _FakeCollection:
    """collection（含多个 tenant store）；with_tenant(t) → tenant view。"""
    def __init__(self):
        # tenant 名 → 该 tenant 的 uuid→object 字典
        self.tenants: dict[str, dict] = {}

    def with_tenant(self, tenant: str):
        # 自动创建 tenant（模拟 base_weaviate_store.py 的 auto_tenant_creation=True 行为）
        if tenant not in self.tenants:
            self.tenants[tenant] = {}
        return _FakeCollectionTenantView(self.tenants[tenant])


class _FakeCollections:
    """client.collections 命名空间：exists / get / create。"""
    def __init__(self):
        # collection 名 → _FakeCollection 实例
        self._cols: dict[str, _FakeCollection] = {}

    def exists(self, name: str) -> bool:
        return name in self._cols

    def get(self, name: str) -> _FakeCollection:
        return self._cols[name]

    def create(self, *, name, **kwargs):
        # fake 不校验 schema，只确保 collection 存在
        self._cols[name] = _FakeCollection()


class _FakeWeaviateClient:
    """v4 weaviate.Client 假体子集：.collections.{exists,get,create}。"""
    def __init__(self):
        self.collections = _FakeCollections()
        # 预先创建 memory_l0（模拟 BaseWeaviateStore.__init__ 已建好）
        self.collections.create(name="memory_l0")


def _make_recaller():
    """tests 通用 fixture-style 工厂：fake embedder + fake weaviate + 真 Recaller。"""
    emb = _FakeEmbedder()
    wv = _FakeWeaviateClient()
    rec = MemoryRecaller(embedder=emb, weaviate_client=wv)
    return rec, emb, wv


def _file_l0_md(src_hash: str, body: str) -> str:
    """构造一份 S2 产物 .abstract.md：frontmatter {src_hash} + body + "\\n"。"""
    return _render_frontmatter({"src_hash": src_hash}, body + "\n")


def _dir_l0_md(inputs_hash: str, body: str) -> str:
    """构造一份 S2 产物目录 .abstract.md：frontmatter {inputs_hash} + body + "\\n"。"""
    return _render_frontmatter({"inputs_hash": inputs_hash}, body + "\n")
```

- [ ] **Step 2: 追加 7 个 index_changed 场景测试**

继续在文件末尾追加（在上一步的 fake 类之后）：

```python
# ── Task 2：index_changed 场景 ①②③④⑤⑨⑩ ─────────────────────
@pytest.mark.asyncio
async def test_index_single_file_l0(tmp_path):
    """场景①：单文件 L0 索引 → tenant 中对象存在、kind/hash/body 与磁盘一致。"""
    fs = _fs(tmp_path)
    rec, emb, wv = _make_recaller()
    # 文件 L0 uri 与 S2 产物
    abs_uri = "ke://u/7/global/identity/user-name.abstract.md"
    body = "用户的名字是李龙飞"
    src_hash = _sha256_hex("用户的名字是李龙飞\n")            # 与 S2 中 src_hash 计算一致
    await fs.write(abs_uri, _file_l0_md(src_hash, body))

    await rec.index_changed(fs, [abs_uri])

    # tenant=str(user_id=7) 中有且仅一条对象（uuid 由 BaseWeaviateStore._to_uuid 派生）
    tenant_store = wv.collections.get("memory_l0").tenants["7"]
    assert len(tenant_store) == 1
    obj = next(iter(tenant_store.values()))
    assert obj["properties"]["uri"] == abs_uri
    assert obj["properties"]["kind"] == "file"
    assert obj["properties"]["hash"] == src_hash
    # body 上一行写入是 body + "\n"，_split_frontmatter 后 body 仍带 "\n"
    assert obj["properties"]["body"].strip() == body
    # embed 调用 1 次（且入参为 .abstract.md 正文）
    assert emb.calls == 1


@pytest.mark.asyncio
async def test_index_hash_idempotent_no_second_embed(tmp_path):
    """场景②：同输入再调 index_changed → embedder.calls 零增量、对象未变。"""
    fs = _fs(tmp_path)
    rec, emb, wv = _make_recaller()
    abs_uri = "ke://u/7/global/identity/user-name.abstract.md"
    body = "用户的名字是李龙飞"
    src_hash = _sha256_hex("用户的名字是李龙飞\n")
    await fs.write(abs_uri, _file_l0_md(src_hash, body))

    await rec.index_changed(fs, [abs_uri])
    first_calls = emb.calls
    snapshot = dict(wv.collections.get("memory_l0").tenants["7"])

    # 同输入再调（哈希命中跳过）→ 零增量 + 对象逐字节不变
    await rec.index_changed(fs, [abs_uri])
    assert emb.calls == first_calls
    assert wv.collections.get("memory_l0").tenants["7"] == snapshot


@pytest.mark.asyncio
async def test_index_body_change_reindex(tmp_path):
    """场景③：正文变更（src_hash 变）→ 重新 embed + upsert，对象 hash 更新。"""
    fs = _fs(tmp_path)
    rec, emb, wv = _make_recaller()
    abs_uri = "ke://u/7/global/identity/user-name.abstract.md"
    body_v1 = "用户的名字是李龙飞"
    h1 = _sha256_hex("用户的名字是李龙飞\n")
    await fs.write(abs_uri, _file_l0_md(h1, body_v1))
    await rec.index_changed(fs, [abs_uri])
    calls_v1 = emb.calls

    # 正文改变 → 重写 .abstract.md（新 src_hash）
    body_v2 = "用户改名为王山河"
    h2 = _sha256_hex("用户改名为王山河\n")
    await fs.write(abs_uri, _file_l0_md(h2, body_v2))
    await rec.index_changed(fs, [abs_uri])

    # embedder 被再调一次（哈希未命中）
    assert emb.calls == calls_v1 + 1
    tenant_store = wv.collections.get("memory_l0").tenants["7"]
    obj = next(iter(tenant_store.values()))
    assert obj["properties"]["hash"] == h2
    assert obj["properties"]["body"].strip() == body_v2


@pytest.mark.asyncio
async def test_index_file_missing_deletes_weaviate(tmp_path):
    """场景④：fs 文件不存在（已删）→ Weaviate 同 uri 对象被删除。"""
    fs = _fs(tmp_path)
    rec, emb, wv = _make_recaller()
    abs_uri = "ke://u/7/global/identity/user-name.abstract.md"
    src_hash = _sha256_hex("body\n")
    # 先索引一次（让 Weaviate 有对象）
    await fs.write(abs_uri, _file_l0_md(src_hash, "body"))
    await rec.index_changed(fs, [abs_uri])
    assert len(wv.collections.get("memory_l0").tenants["7"]) == 1

    # 删磁盘文件 + 调 index_changed（同 uri）→ 期望 Weaviate 对象被删
    await fs.rm(abs_uri)
    await rec.index_changed(fs, [abs_uri])
    assert len(wv.collections.get("memory_l0").tenants["7"]) == 0


@pytest.mark.asyncio
async def test_index_supports_mv_via_old_and_new_uris(tmp_path):
    """场景⑤：fs.mv 后调用方传 [old, new] → old 删 / new 加。"""
    fs = _fs(tmp_path)
    rec, emb, wv = _make_recaller()
    old_uri = "ke://u/7/global/identity/old-name.abstract.md"
    new_uri = "ke://u/7/global/identity/new-name.abstract.md"
    h = _sha256_hex("body\n")
    await fs.write(old_uri, _file_l0_md(h, "body"))
    await rec.index_changed(fs, [old_uri])
    assert len(wv.collections.get("memory_l0").tenants["7"]) == 1

    # 模拟 mv：删 old、写 new（实际 S1 fs.mv 接口可用，但此处直接 write/rm 简化）
    await fs.rm(old_uri)
    await fs.write(new_uri, _file_l0_md(h, "body"))
    # 调用方传两个 uri 给 index_changed → old 删 / new 加
    await rec.index_changed(fs, [old_uri, new_uri])
    tenant_store = wv.collections.get("memory_l0").tenants["7"]
    assert len(tenant_store) == 1
    obj = next(iter(tenant_store.values()))
    assert obj["properties"]["uri"] == new_uri


@pytest.mark.asyncio
async def test_index_embed_error_isolated(tmp_path, caplog):
    """场景⑨（index 侧）：embed 抛错 → 该 uri 跳过、_log.debug、其余 uri 正常索引。"""
    fs = _fs(tmp_path)

    class _BoomEmbedder:
        """对特定文本抛错，其他文本正常。"""
        def __init__(self):
            self.calls = 0

        async def embed(self, text: str) -> list[float]:
            self.calls += 1
            # 命中 "boom" 文本时抛错
            if "boom" in text:
                raise RuntimeError("embed boom")
            # 其他文本：用 _FakeEmbedder 同算法的简化版
            return [0.0] * 1024

    boom = _BoomEmbedder()
    wv = _FakeWeaviateClient()
    rec = MemoryRecaller(embedder=boom, weaviate_client=wv)

    bad = "ke://u/7/global/identity/bad.abstract.md"
    good = "ke://u/7/global/identity/good.abstract.md"
    await fs.write(bad, _file_l0_md(_sha256_hex("boom body\n"), "boom body"))
    await fs.write(good, _file_l0_md(_sha256_hex("good body\n"), "good body"))

    import logging
    with caplog.at_level(logging.DEBUG, logger="src.service.memory.recall"):
        await rec.index_changed(fs, [bad, good])

    # bad 因 embed 抛错被隔离 → tenant 中只有 good
    tenant_store = wv.collections.get("memory_l0").tenants["7"]
    assert len(tenant_store) == 1
    obj = next(iter(tenant_store.values()))
    assert obj["properties"]["uri"] == good
    # 日志含 "index failed"
    assert any("index failed" in r.message for r in caplog.records)


@pytest.mark.asyncio
async def test_index_s6_whole_tree_one_call(tmp_path):
    """场景⑩：S6 整树场景 — 直接 fs.write 一棵树（文件 L0 + 目录 L0），一次
    index_changed(整树) → 全部对象就位、tenant 隔离正确。
    """
    fs = _fs(tmp_path)
    rec, emb, wv = _make_recaller()
    # 模拟 S2 已生成的 .abstract.md 集合（文件 L0 + 目录 L0）
    tree = {
        # 文件 L0
        "ke://u/9/global/identity/name.abstract.md":
            _file_l0_md(_sha256_hex("名字是李龙飞\n"), "名字是李龙飞"),
        "ke://u/9/global/identity/alias.abstract.md":
            _file_l0_md(_sha256_hex("别名老李\n"), "别名老李"),
        # 目录 L0（identity 目录的 .abstract.md，frontmatter 用 inputs_hash）
        "ke://u/9/global/identity/.abstract.md":
            _dir_l0_md(_sha256_hex("aggregated\n"), "identity 子树聚合摘要"),
        # 目录 L0（global 目录）
        "ke://u/9/global/.abstract.md":
            _dir_l0_md(_sha256_hex("agg2\n"), "global 子树聚合摘要"),
    }
    for uri, content in tree.items():
        await fs.write(uri, content)

    await rec.index_changed(fs, list(tree.keys()))

    # tenant=9 中应有 4 个对象，无 user 7 等其他租户
    tenant_store = wv.collections.get("memory_l0").tenants["9"]
    assert len(tenant_store) == 4
    # 每个 uri 都被索引（kind 正确）
    by_uri = {o["properties"]["uri"]: o for o in tenant_store.values()}
    assert by_uri["ke://u/9/global/identity/name.abstract.md"]["properties"]["kind"] == "file"
    assert by_uri["ke://u/9/global/identity/.abstract.md"]["properties"]["kind"] == "dir"
    assert by_uri["ke://u/9/global/.abstract.md"]["properties"]["kind"] == "dir"
    # embed 调用 4 次（每个 uri 1 次）
    assert emb.calls == 4
```

- [ ] **Step 3: 跑测试确认失败**

Run: `cd /Users/java/knowledge-engineering-auth && ./venv/bin/python -m pytest tests/test_auth/test_memory_recall.py -q -k "index"`
Expected: FAIL — `MemoryRecaller` 无 `index_changed` 方法（`AttributeError`）。

- [ ] **Step 4: 在 `MemoryRecaller` 内实现 `index_changed` 与私有 helpers**

在 `src/service/memory/recall.py` 的 `class MemoryRecaller` 内（`__init__` 之后）追加：

```python
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
```

- [ ] **Step 5: 跑测试确认通过**

Run: `cd /Users/java/knowledge-engineering-auth && ./venv/bin/python -m pytest tests/test_auth/test_memory_recall.py -q`
Expected: PASS（Task 1 的 3 条 + Task 2 的 7 条 = 10 passed）。

- [ ] **Step 6: Commit**

```bash
cd /Users/java/knowledge-engineering-auth && git add src/service/memory/recall.py tests/test_auth/test_memory_recall.py && git commit -m "$(cat <<'EOF'
feat(memory-s3): index_changed 算法 + 哈希幂等 + 单条目失败隔离

MemoryRecaller.index_changed 对去重后的 changed_uris 各自对账：仅识别
.abstract.md 后缀；fs.exists 走 read frontmatter → 比 src_hash/inputs_hash
→ 命中跳过 / 不命中 embed+upsert；不 exists 走 delete。Weaviate uuid 复用
BaseWeaviateStore._to_uuid(uri)。单条目失败 _log.debug 跳过、不连累整批。
覆盖 §4.7①②③④⑤⑨⑩：单文件索引 / 哈希幂等零增量 / 正文变更重生 / 删 /
mv 双 uri 对账 / embed 抛错隔离 / S6 整树一次性。设计：文件式记忆重构-设计 §4.3。

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>
EOF
)" && git status
```

---

### Task 3: `recall_memory_block` 算法（embed query + tenant near_vector + L1 fs.read 展开 + 全 try/except 返 ""）

**Files:**
- Modify: `src/service/memory/recall.py`（在 `MemoryRecaller` 内追加 `recall_memory_block`）
- Modify: `tests/test_auth/test_memory_recall.py`（追加 5 个场景：⑥多租户隔离 / ⑦目录 L0 命中 L1 展开 / ⑧L1 缺失 fallback / ⑨recall 侧 embed 抛错 → ""）

- [ ] **Step 1: 在测试文件末尾追加 5 个 recall 场景测试**

```python
# ── Task 3：recall_memory_block 场景 ⑥⑦⑧⑨(recall) ─────────────
@pytest.mark.asyncio
async def test_recall_multi_tenant_physical_isolation(tmp_path):
    """场景⑥：tenant 物理隔离 — user 7 索引的内容、user 8 召回零结果。"""
    fs = _fs(tmp_path)
    rec, emb, wv = _make_recaller()
    # user 7 写入 + 索引
    abs7 = "ke://u/7/global/identity/u7-name.abstract.md"
    h7 = _sha256_hex("u7 secret\n")
    await fs.write(abs7, _file_l0_md(h7, "u7 secret"))
    await rec.index_changed(fs, [abs7])

    # user 8 完全没有自己的索引；用 query="secret" 召回
    block = await rec.recall_memory_block(fs, "secret", user_id=8, top_k=5)
    # 物理隔离 = user 8 看不到 user 7 的任何对象 → block 为 ""
    assert block == ""
    # 反之，user 7 自己召回有结果
    block7 = await rec.recall_memory_block(fs, "secret", user_id=7, top_k=5)
    assert "u7 secret" in block7


@pytest.mark.asyncio
async def test_recall_dir_l0_hit_expands_l1(tmp_path):
    """场景⑦：目录 L0 命中 → 同目录 .overview.md(L1) 经 fs.read 展开拼接。"""
    fs = _fs(tmp_path)
    rec, emb, wv = _make_recaller()
    # 在 identity 子树写：目录 L0（.abstract.md）+ 目录 L1（.overview.md）
    dir_l0_uri = "ke://u/7/global/identity/.abstract.md"
    dir_l1_uri = "ke://u/7/global/identity/.overview.md"
    inputs_hash = _sha256_hex("aggregated\n")
    await fs.write(dir_l0_uri, _dir_l0_md(inputs_hash, "identity 摘要"))
    await fs.write(dir_l1_uri, _render_frontmatter(
        {"inputs_hash": inputs_hash}, "L1 导航：name / alias\n"
    ))
    await rec.index_changed(fs, [dir_l0_uri])    # 只把目录 L0 灌入 Weaviate

    block = await rec.recall_memory_block(fs, "identity", user_id=7, top_k=5)
    # 应同时含 L0（"identity 摘要"）与 L1（"L1 导航：name / alias"）
    assert "identity 摘要" in block
    assert "L1 导航：name / alias" in block


@pytest.mark.asyncio
async def test_recall_l1_missing_fallback_to_l0_only(tmp_path):
    """场景⑧：dir L0 命中但 .overview.md 不存在 → 该 hit fallback 到仅 L0；
    其他 hits 不受影响。
    """
    fs = _fs(tmp_path)
    rec, emb, wv = _make_recaller()
    dir_l0_uri = "ke://u/7/global/identity/.abstract.md"
    file_l0_uri = "ke://u/7/global/identity/name.abstract.md"
    h_dir = _sha256_hex("dir agg\n")
    h_file = _sha256_hex("名字是李龙飞\n")
    # 写目录 L0 + 文件 L0；**不写**目录 .overview.md（模拟 L1 缺失）
    await fs.write(dir_l0_uri, _dir_l0_md(h_dir, "identity 子树摘要"))
    await fs.write(file_l0_uri, _file_l0_md(h_file, "名字是李龙飞"))
    await rec.index_changed(fs, [dir_l0_uri, file_l0_uri])

    block = await rec.recall_memory_block(fs, "identity", user_id=7, top_k=5)
    # block 含 dir L0 内容（fallback：仅 L0，无 L1 段）+ file L0 内容（正常）
    assert "identity 子树摘要" in block
    assert "名字是李龙飞" in block
    # block 不应含 "L1 导航" 类字样（因为 L1 缺失）
    assert "L1 导航" not in block


@pytest.mark.asyncio
async def test_recall_embed_error_returns_empty_string(tmp_path):
    """场景⑨（recall 侧）：embed 抛错 → recall_memory_block 返 ""（不抛）。"""
    fs = _fs(tmp_path)

    class _AlwaysBoomEmbedder:
        async def embed(self, text: str) -> list[float]:
            raise RuntimeError("embed always boom")

    rec = MemoryRecaller(embedder=_AlwaysBoomEmbedder(), weaviate_client=_FakeWeaviateClient())
    # 不需要先 index，直接 recall（embed query 时抛错）
    block = await rec.recall_memory_block(fs, "anything", user_id=7, top_k=5)
    assert block == ""


@pytest.mark.asyncio
async def test_recall_returns_empty_when_no_hits(tmp_path):
    """recall 兜底：tenant 中无对象 → near_vector 返空 → block 为 ""（with_memory_block
    走零开销不注入路径，§4.4 末尾设计）。"""
    fs = _fs(tmp_path)
    rec, emb, wv = _make_recaller()
    # 完全没有 index_changed 任何 uri
    block = await rec.recall_memory_block(fs, "anything", user_id=7, top_k=5)
    assert block == ""
```

- [ ] **Step 2: 跑测试确认失败**

Run: `cd /Users/java/knowledge-engineering-auth && ./venv/bin/python -m pytest tests/test_auth/test_memory_recall.py -q -k "recall"`
Expected: FAIL — `MemoryRecaller` 无 `recall_memory_block` 方法。

- [ ] **Step 3: 实现 `recall_memory_block`**

在 `src/service/memory/recall.py` 的 `class MemoryRecaller` 内（`_index_one` 之后）追加：

```python
    # ── 公开 API #2 ──────────────────────────────────────────────
    async def recall_memory_block(
        self,
        fs: MemoryFS,
        query: str,
        user_id: int,
        *,
        top_k: int = 5,
    ) -> str:
        """召回当前用户的 L0 → 拼装 system-prompt 记忆块（§4.4）。

        S3 自包失败语义：embed / Weaviate query / 单 hit L1.read 失败全 try/except
        → 返 ""（with_memory_block 见空跳过注入路径 = 主答零开销）。
        「记忆失败绝不影响主答」在 S3 自身保证，调用方 try/except 不再必需（但
        §22 的 qa_router 防御性 try 仍保留，§4.5 设计）。
        """
        tenant = str(user_id)

        # 1) embed query（失败即返 ""）
        try:
            q_vec = await self._embedder.embed(query)
        except Exception as exc:
            _log.debug("recall: embed failed %r", exc)
            return ""

        # 2) tenant-scoped 近邻搜索（失败即返 ""）
        try:
            coll = self._weaviate_client.collections.get(_COLLECTION_NAME)
            view = coll.with_tenant(tenant)
            result = view.query.near_vector(near_vector=q_vec, limit=top_k)
            hits = result.objects
        except Exception as exc:
            _log.debug("recall: weaviate query failed %r", exc)
            return ""

        # 3) 装配 parts：file hit 直拼 body；dir hit 同目录 fs.read .overview.md 展开
        parts: list[str] = []
        for h in hits:
            try:
                props = h.properties
                kind = props.get("kind")
                body = (props.get("body") or "").strip()
                if not body:
                    # 空 body 极少发生（S3 不写空 body；防御性跳过）
                    continue
                if kind == "file":
                    parts.append(body)
                else:
                    # kind == "dir"：派生同目录 .overview.md 路径
                    dir_l0_uri = props.get("uri") or ""
                    if not dir_l0_uri.endswith("/" + _ABSTRACT_SUFFIX):
                        # 防御：理论上 dir 类 uri 必以 "/.abstract.md" 结尾
                        parts.append(body)
                        continue
                    ovr_uri = _overview_uri_for_dir_l0(dir_l0_uri)
                    try:
                        ovr_raw = await fs.read(ovr_uri)
                        _meta, ovr_body = _split_frontmatter(ovr_raw)
                        parts.append(body + "\n---\n" + ovr_body.strip())
                    except MemoryNotFound:
                        # L1 缺失：fallback 仅用 L0（不连累其他 hits）
                        _log.debug("recall: overview missing %r, fallback L0", ovr_uri)
                        parts.append(body)
            except Exception as exc:
                _log.debug("recall: hit assemble failed: %r", exc)
                # 单 hit 装配失败不连累其他 hits
                continue

        # 4) 空 → "" ；非空 → "- " 起首的 bullet 拼装（§4.4 末尾设计）
        if not parts:
            return ""
        return "\n\n".join(f"- {p}" for p in parts)
```

- [ ] **Step 4: 跑测试确认通过**

Run: `cd /Users/java/knowledge-engineering-auth && ./venv/bin/python -m pytest tests/test_auth/test_memory_recall.py -q`
Expected: PASS（Task1 3 + Task2 7 + Task3 5 = 15 passed）。

- [ ] **Step 5: Commit**

```bash
cd /Users/java/knowledge-engineering-auth && git add src/service/memory/recall.py tests/test_auth/test_memory_recall.py && git commit -m "$(cat <<'EOF'
feat(memory-s3): recall_memory_block 算法 + 多租户隔离 + L1 展开 + 失败返 ""

MemoryRecaller.recall_memory_block 实现 §4.4 召回 + 装配：embed query →
with_tenant(uid).near_vector top_k → file hit 直拼 body / dir hit 派生
同目录 .overview.md 经 fs.read 展开（L1 缺失 fallback 仅 L0）。整函数自包
失败语义：embed/query/单 hit 装配全 try → 返 ""（with_memory_block 零开销
路径），「记忆失败绝不影响主答」在 S3 自身保证。覆盖 §4.7⑥⑦⑧⑨(recall)：
多租户物理隔离 / 目录 L0 命中 L1 展开 / L1 缺失 fallback / embed 抛错返 ""。
设计：文件式记忆重构-设计 §4.4/§4.5。

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>
EOF
)" && git status
```

---

### Task 4: Drop-in 替换 §22 `recall_memory_block` + 删除旧 DB 测试 + 调用点改 1 行 + 广回归

**Files:**
- Modify: `src/service/memory/service.py`（line 97 起整函数体替换为新签名 + 委托 S3）
- Modify: `src/service/qa_router.py`（line 277-279 调用点改 1 行）
- Modify: `tests/test_auth/test_memory_service.py`（删除 §22 旧 `recall_memory_block` 测试函数）

实现 §4.7 场景 ⑪（drop-in 替换链路）与 ⑫（既有套件不回归）。

- [ ] **Step 1: 删除 `test_memory_service.py` 中 §22 旧 `recall_memory_block` 测试**

读 `/Users/java/knowledge-engineering-auth/tests/test_auth/test_memory_service.py`，**精确定位**所有使用旧签名 `recall_memory_block(db, user_id=..., session_id=..., project_id=...)` 的测试函数，整函数删除（含 `@pytest.mark.asyncio` 装饰器与 docstring 这些上下文行）。已知锚点：

- `test_recall_empty_when_nothing`（约 line 221-225）：`db = _FakeMemDB(); block = await recall_memory_block(db, user_id=1, session_id="s1"); assert block == ""`
- `test_recall_combines_session_then_user`（约 line 228-238）：组合 session + user 记忆断言
- 另外可能的 line 360 / 375 附近的两处（控制器 grep 提示）

实现者动作：
1. `cd /Users/java/knowledge-engineering-auth && grep -n "recall_memory_block(db" tests/test_auth/test_memory_service.py` 列出所有 callsite 行号。
2. 对每个 callsite，向上找最近的 `@pytest.mark.asyncio` 或 `def test_` 边界，向下找下一个 `def test_` 或文件末尾 / 类边界，**整函数删除**（包含其 docstring 与装饰器）。
3. 同时删除文件顶部 `from src.service.memory.service import (...)` 块内的 `recall_memory_block,` 一行（如果该 import 仅服务于刚删的测试）。如果 `service.py` 中 `recall_memory_block` 已不存在或仅作为 deprecated import（取决于 Task 4 Step 2 怎么改），import 行必须删除避免 ImportError。

约束：**只删 `recall_memory_block` 相关测试**，其他记忆测试函数（`test_write_explicit_adds_user_memory_row` / `test_compact_*` / `test_detect_*` / `test_user_mem_intent_*` 等）一律保留不动。

- [ ] **Step 2: 确认删除生效**

Run: `cd /Users/java/knowledge-engineering-auth && grep -n "recall_memory_block" tests/test_auth/test_memory_service.py`
Expected: 空输出（无任何匹配）。

Run: `cd /Users/java/knowledge-engineering-auth && ./venv/bin/python -m pytest tests/test_auth/test_memory_service.py -q`
Expected: PASS — 其他记忆测试仍全过（`recall_memory_block` 测试已不在）。

- [ ] **Step 3: 替换 `src/service/memory/service.py:97` 的 `recall_memory_block` 整函数**

读 `/Users/java/knowledge-engineering-auth/src/service/memory/service.py` 找到 `async def recall_memory_block(db: Any, *, user_id: int, session_id: str, project_id: str | None = None) -> str:` 开头（line 97）；整函数体（直到下一个 `async def` 或 `def` 出现）替换为：

```python
async def recall_memory_block(
    fs,                                          # 类型由 import 决定（避免循环导入 → 不在签名标注 MemoryFS）
    query: str,
    user_id: int,
    *,
    top_k: int = 5,
) -> str:
    """召回当前用户的记忆 L0 拼装为 system-prompt 块（S3 实现）。

    新签名（drop-in 替换 §22 DB 版）：fs + query + user_id；返回 str（空字符串
    表示无记忆，调用方 with_memory_block 据此跳过注入零开销）。
    内部委托 MemoryRecaller；S3 自包失败语义（embed/query 失败返 ""），
    调用方 try/except 不再必需（但 qa_router 既有 try 保留为防御性深度防护）。

    设计：[[文件式记忆重构-设计]] §4.1（drop-in 替换契约）+ §4.4（召回算法）。
    """
    # 局部 import：避免模块顶层循环引入（recall.py → memgen.py → 不回环 service.py）
    from src.service.memory.recall import MemoryRecaller, MemoryL0Store, _DefaultEmbedder
    # 单例 / 注入：本函数走 KE 部署期既定 Weaviate 配置；在生产里由 KE 框架装配
    # 注入；此处为简化集成层调用，按 KE 既有 Weaviate 客户端创建惯例就地构造。
    # 注：MemoryL0Store(BaseWeaviateStore) 的 __init__ 同步打开连接 / 检查 schema，
    # 重复构造代价不大（KE 既有 weaviate_*_store.py 同模式）；如需性能优化（连接池），
    # 由 S7 / 独立 hardening pass 引入单例（不在 S3 MVP）。
    # 为可测：本函数若被现存测试以 fake fs 调入但无 Weaviate 部署，会因
    # MemoryL0Store 构造抛 ConnectionError → 进入 S3 自包 try 路径返 ""。
    try:
        # 读 KE 既有部署配置（与 KE 既有 weaviate stores 一致的入口）
        import os
        url = os.getenv("WEAVIATE_URL", "http://127.0.0.1:8080")
        grpc_port = int(os.getenv("WEAVIATE_GRPC_PORT", "50051"))
        api_key = os.getenv("WEAVIATE_API_KEY") or None
        # 构造 MemoryL0Store（同步初始化连接 + 必要时建 collection）
        store = MemoryL0Store(
            url=url,
            grpc_port=grpc_port,
            collection_name="memory_l0",
            dimension=1024,
            api_key=api_key,
        )
        # MemoryL0Store._client 是 weaviate v4 client；MemoryRecaller 直接接收 client
        recaller = MemoryRecaller(embedder=_DefaultEmbedder(), weaviate_client=store._client)
        return await recaller.recall_memory_block(fs, query, user_id, top_k=top_k)
    except Exception:
        # S3 自包失败语义：任何异常 → 返 ""（with_memory_block 走零开销不注入路径）
        return ""
```

注：原函数体（约 line 98-180 的 DB 三层查询 + 拼装）整体删除。如果原函数体之后是别的 `async def detect_explicit_project_memory(...)` 或类似，保留不动；只替换 `recall_memory_block` 这一个函数。

- [ ] **Step 4: 修改 `src/service/qa_router.py:277-279` 调用点**

读该文件 line 270-285。把以下既有代码：

```python
    # 5. 记忆召回（spec §7）：进流前查用户级+会话级，拼 memory_block。
    # 失败静默 → 空串，不影响主答。
    try:
        memory_block = await recall_memory_block(
            db, user_id=user.id, session_id=session_id, project_id=project_id
        )
    except Exception:
        memory_block = ""
```

替换为：

```python
    # 5. 记忆召回（文件式记忆 S3：Weaviate 向量召回 on L0）：embed body.question
    # 在用户租户内做 near_vector top-k，命中目录 L0 时同目录 fs.read .overview.md
    # 展开；失败 S3 自包返 ""（with_memory_block 零开销不注入路径）。
    # 防御性 try 保留（深度防护；S3 自身已 try → 返 ""，此层是冗余兜底）。
    try:
        # fs 由 KE 部署期单例提供；按 KE 既有 memory.vfs 入口构造
        from src.service.memory.vfs import MemoryFS
        memory_block = await recall_memory_block(
            MemoryFS(),                         # 默认 root 由 KE_MEM_ROOT 环境变量或仓库根派生
            body.question,                       # query 来自 body.question（当前用户问题）
            user_id=user.id,
            top_k=5,
        )
    except Exception:
        memory_block = ""
```

注意：`MemoryFS()` 默认 root 走 S1 `mem_root()` 函数（`KE_MEM_ROOT` 环境变量或仓库根 `/.ke-memory`）；本调用点不传 root，让运行期决定。

- [ ] **Step 5: import 自检 + 行为 smoke**

Run（确认所有顶层 import 仍 work、recall_memory_block 新签名可被外部加载）：
```
cd /Users/java/knowledge-engineering-auth && ./venv/bin/python -c "
from src.service.memory.service import recall_memory_block
from src.service.memory.recall import MemoryRecaller, MemoryL0Store, _DefaultEmbedder
from src.service.qa_router import router
import inspect
sig = inspect.signature(recall_memory_block)
print('recall_memory_block signature:', sig)
print('ok')
"
```
Expected: 输出 `recall_memory_block signature: (fs, query: str, user_id: int, *, top_k: int = 5) -> str` + `ok`。

- [ ] **Step 6: 跑 S3 全套确认通过 + 广回归**

Run S3 套件：`cd /Users/java/knowledge-engineering-auth && ./venv/bin/python -m pytest tests/test_auth/test_memory_recall.py -q`
Expected: 15 passed（同 Task 3 结束）。

Run 广回归：`cd /Users/java/knowledge-engineering-auth && ./venv/bin/python -m pytest tests/test_auth -q -k "memory or qa or prompt or chitchat or sse or vfs"`
Expected: 0 failed（删除的旧 recall 测试已不在；其他记忆/QA/prompt 用例仍全过；既有 vfs/memgen 套件不动）。

若任何用例 FAIL：STOP 并报告（不能绕过）。常见预期之一：`qa_router.py:277` 调用点改造可能影响某些集成测试（例如 `test_qa_router_classifier.py`），需精读其期望调用面并按需调整 mock；不允许直接 skip 已有用例。

- [ ] **Step 7: Commit**

```bash
cd /Users/java/knowledge-engineering-auth && git add src/service/memory/service.py src/service/qa_router.py tests/test_auth/test_memory_service.py && git commit -m "$(cat <<'EOF'
feat(memory-s3): drop-in 替换 §22 recall_memory_block - DB → 文件+Weaviate

service.py:97 整函数替换：新签名 (fs, query, user_id, *, top_k=5) -> str，
内部委托 MemoryRecaller；S3 自包失败语义返 ""，调用方 try 保留为深度防护。
qa_router.py:277 调用点改 1 行：DB 参数换 (fs=MemoryFS(), query=body.question)。
synthesizer.py 4 处 with_memory_block 注入链零改动。
tests/test_memory_service.py 删除 §22 旧 recall_memory_block 测试（其 DB
三层 contract 已被 S3 替换；新测试在 test_memory_recall.py）。设计：文件式
记忆重构-设计 §4.1 drop-in 契约。

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>
EOF
)" && git status
```

---

## 实现者备注（避免常见返工）

1. **测试解释器**：homebrew `python3` 无 `pytest-asyncio`；必须用仓库 venv：`./venv/bin/python -m pytest ...`（与 S1/S2 同；strict 模式需 `@pytest.mark.asyncio`）。
2. **不重复实现 frontmatter / 哈希工具**：S3 import 复用 S2 `memgen` 的 `_split_frontmatter` / `_render_frontmatter` / `_sha256_hex` / `_ABSTRACT_SUFFIX` / `_OVERVIEW_NAME`。S2 已处理 CRLF / 损坏 YAML / 全数字 hash → int 等 corner case，S3 不重复实现。
3. **不重复实现 Weaviate 连接 / collection 创建**：S3 `MemoryL0Store` 继承 `BaseWeaviateStore`，复用其 `_ensure_client_and_schema()` 与 `_to_uuid(s)`。Multi-Tenancy 由基类默认开启（`auto_tenant_creation=True`），S3 直接 `with_tenant(str(uid))`，**写入时无需预先创建 tenant**。
4. **`_to_uuid` 派生**：用 `BaseWeaviateStore._to_uuid(uri)` 静态方法（SHA-256[:32] 拼 UUID5 格式），**不要**自造 `sha1(uri)[:16]`。
5. **embedder 是 thin async wrapper**：`_DefaultEmbedder.embed(text)` 调用 KE 同步 `get_embedding(text, 1024)`。底层是 Ollama HTTP（或配置切换 Aliyun text-embedding-v4），1024 维。S3 不关心底层模型，只信 1024 维契约。
6. **S3 自包失败语义**：`recall_memory_block` 与 `index_changed` 全面 try/except → debug log → 返 "" 或 continue。**调用方不再依赖 try 包裹**，但既有 `qa_router.py:277` 防御性 try 保留为深度防护，不强制移除。
7. **不动 §22 其他记忆函数**：S3 只替换 `recall_memory_block`；`detect_explicit_memory` / `write_explicit_memory` / `parse_user_memory_intent` / `maybe_compact_session` 等保留不动（其归属由 S4 / S5 / S6 后续 brainstorm 决定）。
8. **不在 S3 范围**：S4 ReAct 抽取 + 轮末接线（**S3 不在 post-turn 注册任何调度**；future S4 在 router post-turn 顺序 `await S2.regenerate(...); await S3.index_changed(...)`）；S5 会话；S6 迁移；S7 多租户加固；长度守护 / observable metrics（独立 hardening pass）。
9. **测试 fake 与真 Weaviate 调用面贴合**：`_FakeWeaviateClient` 的 `.collections.get(name).with_tenant(t).data.{insert,replace,delete_by_id}` 与 `.query.{fetch_object_by_id,near_vector}` 调用面与 v4 client 子集严格一致；切真客户端时 S3 代码 0 改写。

## 自检（writing-plans Self-Review）

- **Spec 覆盖**（§4.7 十二条 ↔ task 映射）：
  - ①单文件 L0 索引 → T2 `test_index_single_file_l0`
  - ②哈希幂等零增量 → T2 `test_index_hash_idempotent_no_second_embed`
  - ③正文变更重生 → T2 `test_index_body_change_reindex`
  - ④fs 文件不存在删 Weaviate → T2 `test_index_file_missing_deletes_weaviate`
  - ⑤fs.mv 经 [old,new] 对账 → T2 `test_index_supports_mv_via_old_and_new_uris`
  - ⑥多租户物理隔离 → T3 `test_recall_multi_tenant_physical_isolation`
  - ⑦目录 L0 命中 L1 展开 → T3 `test_recall_dir_l0_hit_expands_l1`
  - ⑧L1 缺失 fallback → T3 `test_recall_l1_missing_fallback_to_l0_only`
  - ⑨embed/query 抛错隔离 → T2 `test_index_embed_error_isolated`（index 侧）+ T3 `test_recall_embed_error_returns_empty_string`（recall 侧）
  - ⑩S6 整树场景 → T2 `test_index_s6_whole_tree_one_call`
  - ⑪drop-in 替换链路 → T4 Step 5/6（signature + qa_router 调用 + import 全链路自检 + 广回归）
  - ⑫既有套件不回归 → T4 Step 6（broad regression）
  设计 §4.1 / §4.2 / §4.3 / §4.4 / §4.5 / §4.6 全覆盖（schema → T1；index 算法 → T2；recall 算法 → T3；drop-in → T4；失败语义 / YAGNI → 备注 6/7/8）。无遗漏。
- **占位扫描**：无 TBD / TODO / "fill in" / "similar to Task N"。每步含完整可运行代码块 / 确切 `./venv/bin/python -m pytest ...` 命令 / 期望输出。
- **类型一致性**：`MemoryRecaller.__init__(embedder, weaviate_client)` 与 `index_changed(fs, changed_uris) -> None` 与 `recall_memory_block(fs, query, user_id, *, top_k=5) -> str` 贯穿 T1-T4 一致；helpers `_kind_of_uri` / `_overview_uri_for_dir_l0` / `_DefaultEmbedder.embed` 命名跨任务统一；常量 `_VECTOR_DIM=1024` / `_COLLECTION_NAME="memory_l0"` 全程同名；测试 fake 一律 `async def embed(self, text)` 鸭子接口（与 KE LLM provider `complete` 同 keyword-only `*` 风格）。
