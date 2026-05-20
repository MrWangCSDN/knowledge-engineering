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


def test_overview_uri_for_dir_l0_rejects_file_uri():
    """防御性断言：传入文件 L0 uri（不以 "/.abstract.md" 结尾）→ AssertionError。"""
    # 文件 L0：末段是 "user-name.abstract.md"（带 slug 前缀，不是裸 ".abstract.md"）
    # 此调用违反 _overview_uri_for_dir_l0 的契约，应被 assert 兜住
    with pytest.raises(AssertionError):
        _overview_uri_for_dir_l0("ke://u/7/global/identity/user-name.abstract.md")


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
    """构造一份 S2 产物 .abstract.md：frontmatter {src_hash} + body + "\n"。"""
    return _render_frontmatter({"src_hash": src_hash}, body + "\n")


def _dir_l0_md(inputs_hash: str, body: str) -> str:
    """构造一份 S2 产物目录 .abstract.md：frontmatter {inputs_hash} + body + "\n"。"""
    return _render_frontmatter({"inputs_hash": inputs_hash}, body + "\n")


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
