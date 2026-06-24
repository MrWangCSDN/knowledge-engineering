"""多工程 clear 隔离 —— gated 真实数据库集成测试（Phase1-T3）。

实证「索引工程 B 不会清掉工程 A 的图/向量」这条多工程隔离止血：
  - Neo4j：backend.clear(project_id="ke-iso-test-B") 只清 B 的节点，A 的节点仍在。
  - Weaviate：store.clear(tenant="ke-iso-test-B") 只清 B 租户，A 租户的对象仍在。

⚠️ 安全设计（最重要）——本测试可能在生产库上跑，绝不能误删真实数据：
  1. Neo4j 只用合成 project_id（前缀 ke-iso-test- 几乎不可能撞真实工程），
     且**只调用带 project_id 的 scoped clear**，绝不调用无参 clear()（无参 = 全库清）。
     断言用 has_node(具体合成节点 id)，不用全局 node_count()（共享库里有真实数据，全局计数无意义）。
  2. Weaviate 用独立测试集合 CodeEntityIsoTest，绝不碰真实 CodeEntity 集合；
     teardown 直接删整个测试集合（专属测试集合，整删安全）。
  3. 测试幂等可重跑：开头先 best-effort 清掉自己的合成数据/测试集合再开始。

Gate：默认 skip，只有设了环境变量 KE_GATED_ISOLATION 才连真库跑。
本地无该 env 时整文件 skip，pytest 收集不报错。

如何在服务器实证（一句话）：
  KE_GATED_ISOLATION=1 KE_NEO4J_URI=bolt://<host>:7687 KE_NEO4J_PASSWORD=<pw> \
  KE_WEAVIATE_URL=http://<host>:8080 \
  python -m pytest tests/test_auth/test_multiproject_clear_isolation.py -q
"""
# os：读环境变量（gate 开关 + 连接参数）
import os
# time：Weaviate 写入到可检索之间有索引延迟，需要给一点等待 / 轮询时间
import time
# pytest：测试框架，用 mark.skipif 实现 gate
import pytest

# 被测后端（按现有 tests 的 import 习惯：直接从模块导入类，见 test_graph_neo4j_clear_scoped.py）
from src.knowledge.graph_neo4j import Neo4jGraphBackend
from src.knowledge.vector_store_weaviate import WeaviateVectorStore


# ─────────────────────────────────────────────────────────────────────────────
# 合成常量（加 ke-iso-test- 前缀，几乎不可能撞真实工程 / 真实集合）
# ─────────────────────────────────────────────────────────────────────────────
# 两个合成工程 id（Neo4j project_id 属性值 / Weaviate tenant 名）
PROJECT_A = "ke-iso-test-A"
PROJECT_B = "ke-iso-test-B"

# Neo4j 合成节点 id：用 method:// 形态贴近真实 entity id，内嵌工程名避免与真实 id 撞
NODE_A_1 = f"method://{PROJECT_A}/foo"          # 工程 A 的节点 1
NODE_A_2 = f"method://{PROJECT_A}/bar"          # 工程 A 的节点 2（加一条边验证 DETACH）
NODE_B_1 = f"method://{PROJECT_B}/foo"          # 工程 B 的节点 1
NODE_B_2 = f"method://{PROJECT_B}/bar"          # 工程 B 的节点 2

# Weaviate 专属测试集合名（绝不碰真实 CodeEntity 集合）
ISO_COLLECTION = "CodeEntityIsoTest"
# Weaviate 合成 entity_id（写入用，租户隔离）
EID_A_1 = "eid-A-1"
EID_B_1 = "eid-B-1"

# gate 标记：未设 KE_GATED_ISOLATION 时整文件每个测试都 skip（不连库）
gated = pytest.mark.skipif(
    not os.getenv("KE_GATED_ISOLATION"),
    reason="gated：需真 Neo4j+Weaviate；设 KE_GATED_ISOLATION=1 启用",
)


# ─────────────────────────────────────────────────────────────────────────────
# 连接参数 helper：从 env 取，给默认（与现有 staging 默认一致）
# ─────────────────────────────────────────────────────────────────────────────
def _neo4j_conn() -> dict:
    """返回 Neo4jGraphBackend 构造所需的连接参数字典（从 env 取，给默认）。

    Returns:
        dict：含 uri / user / password / database 四个键。
    """
    # os.getenv(key, default)：取环境变量，缺失时返默认值
    return dict(
        uri=os.getenv("KE_NEO4J_URI", "bolt://localhost:7687"),
        user=os.getenv("KE_NEO4J_USER", "neo4j"),
        password=os.getenv("KE_NEO4J_PASSWORD", "12345678"),
        database=os.getenv("KE_NEO4J_DATABASE", "neo4j"),
    )


def _make_neo4j_backend(project_id=None) -> Neo4jGraphBackend:
    """构造一个真连的 Neo4jGraphBackend。

    Args:
        project_id: 可选；非空时 add_node 会自动把 project_id 注入节点属性
                    （见 graph_neo4j.add_node），从而让 scoped clear 能按工程过滤。
    Returns:
        Neo4jGraphBackend 实例（已建好 driver，连真库）。
    """
    # ** 解包：把 _neo4j_conn() 返回的字典展开成关键字参数传给构造函数
    return Neo4jGraphBackend(project_id=project_id, **_neo4j_conn())


def _make_weaviate_store() -> WeaviateVectorStore:
    """构造一个真连的 WeaviateVectorStore，指向**独立测试集合** CodeEntityIsoTest。

    绝不传真实 CodeEntity 集合名，确保 teardown 整删集合是安全的。
    dimension=64 与默认 embedding 维度一致。

    Returns:
        WeaviateVectorStore 实例（已建好 client + schema）。
    """
    return WeaviateVectorStore(
        collection_name=ISO_COLLECTION,                                   # 专属测试集合
        url=os.getenv("KE_WEAVIATE_URL", "http://localhost:8080"),        # HTTP 地址
        grpc_port=int(os.getenv("KE_WEAVIATE_GRPC_PORT", "50051")),       # gRPC 端口（int 强转）
        api_key=os.getenv("KE_WEAVIATE_API_KEY"),                         # 可能为 None（无鉴权）
        dimension=64,                                                     # 向量维度
    )


# ─────────────────────────────────────────────────────────────────────────────
# 测试 1：Neo4j scoped clear 不碰其它工程
# ─────────────────────────────────────────────────────────────────────────────
@gated
def test_neo4j_clear_scoped_does_not_touch_other_project():
    """索引工程 B（clear B 的图）不应清掉工程 A 的图。

    步骤：
      1. 幂等开场：scoped 清掉 A、B 的合成节点（确保从干净起点开始）。
      2. 给工程 A、B 各写若干节点（带 project_id），各加一条边验证 DETACH 删边。
      3. clear(project_id="ke-iso-test-B")。
      4. 断言：A 的节点 has_node 仍 True；B 的节点 has_node 为 False。
      5. teardown：scoped 清掉 A、B。
    """
    # 三个 backend：A 写库 / B 写库 / 一个无 project_id 的「读+scoped clear」句柄
    # 注意：has_node / clear(project_id=...) 不依赖构造时的 project_id，
    # 任意 backend 都能调；这里用 backend_a / backend_b 各自写自己工程的节点。
    backend_a = _make_neo4j_backend(project_id=PROJECT_A)   # A 句柄：add_node 自动注入 project_id=A
    backend_b = _make_neo4j_backend(project_id=PROJECT_B)   # B 句柄：add_node 自动注入 project_id=B

    try:
        # ── 1) 幂等开场：先 scoped 清掉自己的合成数据（绝不调无参 clear()！）──
        backend_a.clear(project_id=PROJECT_A)
        backend_b.clear(project_id=PROJECT_B)

        # ── 2) 写入：A 两节点 + 一条边；B 两节点 + 一条边 ──
        backend_a.add_node(NODE_A_1, entity_type="method", name="foo")
        backend_a.add_node(NODE_A_2, entity_type="method", name="bar")
        backend_a.add_edge(NODE_A_1, NODE_A_2, "calls")     # A 内部一条边，验证 scoped clear 不误伤

        backend_b.add_node(NODE_B_1, entity_type="method", name="foo")
        backend_b.add_node(NODE_B_2, entity_type="method", name="bar")
        backend_b.add_edge(NODE_B_1, NODE_B_2, "calls")     # B 内部一条边，验证 DETACH 删边

        # 写入后先确认两工程节点都在（否则后面的「B 被清」断言无意义）
        assert backend_a.has_node(NODE_A_1) is True, "前置：A 的节点应已写入"
        assert backend_b.has_node(NODE_B_1) is True, "前置：B 的节点应已写入"

        # ── 3) 索引工程 B 的清空动作：只清 B ──
        # 关键：传 project_id=B，走 MATCH (n:Entity {project_id: $pid}) DETACH DELETE n
        backend_b.clear(project_id=PROJECT_B)

        # ── 4) 断言隔离：A 全在，B 全没 ──
        # A 的节点不应被 B 的 clear 波及（这是本测试的核心止血点）
        assert backend_a.has_node(NODE_A_1) is True, "隔离失败：B 的 clear 清掉了 A 的节点 NODE_A_1"
        assert backend_a.has_node(NODE_A_2) is True, "隔离失败：B 的 clear 清掉了 A 的节点 NODE_A_2"
        # B 的节点应被清掉（含 DETACH 删边）
        assert backend_b.has_node(NODE_B_1) is False, "B 的 scoped clear 未生效：NODE_B_1 仍在"
        assert backend_b.has_node(NODE_B_2) is False, "B 的 scoped clear 未生效：NODE_B_2 仍在"
    finally:
        # ── 5) teardown：scoped 清掉 A、B 两个合成工程（绝不全库清）──
        # try/except 各自吞掉异常，保证两个工程都尽量清干净，再关连接
        try:
            backend_a.clear(project_id=PROJECT_A)
        except Exception:
            pass
        try:
            backend_b.clear(project_id=PROJECT_B)
        except Exception:
            pass
        # 关闭 driver，释放连接池
        try:
            backend_a.close()
        except Exception:
            pass
        try:
            backend_b.close()
        except Exception:
            pass


# ─────────────────────────────────────────────────────────────────────────────
# 测试 2：Weaviate tenant clear 不碰其它租户
# ─────────────────────────────────────────────────────────────────────────────
def _poll_get(store: WeaviateVectorStore, entity_id: str, tenant: str, *, expect_present: bool, attempts: int = 20):
    """轮询 get_by_entity_id，等待 Weaviate 写入/删除生效（应对索引延迟）。

    Weaviate 写入到可检索之间、以及租户删除生效之间都有异步延迟，
    直接断言容易 flaky。这里轮询最多 attempts 次、每次间隔 0.5s。

    Args:
        store: 被测 store。
        entity_id: 要查的 entity_id。
        tenant: 租户名。
        expect_present: True 表示期望最终能取到（非 None）；False 表示期望最终取不到（None）。
        attempts: 最大轮询次数。
    Returns:
        最后一次 get_by_entity_id 的结果（dict 或 None）。
    """
    # 记录最后一次结果，循环结束后返回给调用方做断言
    last = None
    # range(attempts)：从 0 到 attempts-1，固定次数轮询
    for _ in range(attempts):
        last = store.get_by_entity_id(entity_id, tenant=tenant)
        # 命中期望就提前返回（present 期望非 None；absent 期望 None）
        if expect_present and last is not None:
            return last
        if (not expect_present) and last is None:
            return last
        # time.sleep：阻塞当前线程 0.5 秒，给 Weaviate 索引/删除一点生效时间
        time.sleep(0.5)
    # 轮询用尽仍未达期望，返回最后一次结果（让调用方的 assert 暴露真实情况）
    return last


@gated
def test_weaviate_clear_tenant_does_not_touch_other_tenant():
    """索引工程 B（clear B 租户）不应清掉工程 A 租户的向量。

    步骤：
      1. 幂等开场：best-effort 删掉残留的测试集合，再新建 store（重建 schema）。
      2. 给 A、B 两租户各写一条对象（vec64 非零向量）。
      3. 先确认两租户各自能取到自己的对象（写入成功，否则后面断言无意义）。
      4. store.clear(tenant="ke-iso-test-B")。
      5. 断言：A 仍非空（未被清）；B 为 None（已清）。
      6. teardown：删整个测试集合 + close。
    """
    # vec64：长度 64 的非零向量（add() 要求 len(vector) >= dimension，否则静默跳过）
    # [0.1] * 64：列表乘法，把单元素列表重复 64 次，得到长度 64 的列表
    vec_a = [0.1] * 64
    vec_b = [0.2] * 64

    # ── 1) 幂等开场：先删掉可能残留的测试集合，再构造（构造时会按需重建 schema）──
    # best-effort：用一个临时 store 句柄删集合（删不存在的集合也无所谓，已 try 吞）
    try:
        # 临时 store 只为拿到 client 删残留集合
        tmp = _make_weaviate_store()
        try:
            # _client：BaseWeaviateStore 持有的 weaviate 连接（题面指明用 store._client）
            if tmp._client and tmp._client.collections.exists(ISO_COLLECTION):
                tmp._client.collections.delete(ISO_COLLECTION)
        finally:
            tmp.close()
    except Exception:
        pass

    # 正式 store：构造时若集合不存在会建（启用 multi-tenancy + auto_tenant_creation）
    store = _make_weaviate_store()

    try:
        # ── 2) 写入：A、B 两租户各一条（add 内部 with_tenant 写对应分区）──
        store.add(EID_A_1, vec_a, entity_type="method", name="foo", tenant=PROJECT_A)
        store.add(EID_B_1, vec_b, entity_type="method", name="foo", tenant=PROJECT_B)

        # ── 3) 前置确认：两租户各自都能取到自己的对象（轮询等索引生效）──
        got_a = _poll_get(store, EID_A_1, PROJECT_A, expect_present=True)
        got_b = _poll_get(store, EID_B_1, PROJECT_B, expect_present=True)
        assert got_a is not None, "前置：A 租户写入应可检索到（写入失败则后续断言无意义）"
        assert got_b is not None, "前置：B 租户写入应可检索到（写入失败则后续断言无意义）"

        # ── 4) 索引工程 B 的清空动作：只清 B 租户 ──
        # clear(tenant=B) 走 _get_collection().tenants.remove([B])，只移除 B 分区
        store.clear(tenant=PROJECT_B)

        # ── 5) 断言隔离：A 仍在，B 已清 ──
        # A 租户的对象不应被「清 B 租户」波及（核心止血点）
        still_a = _poll_get(store, EID_A_1, PROJECT_A, expect_present=True)
        assert still_a is not None, "隔离失败：清 B 租户清掉了 A 租户的对象"
        # B 租户的对象应被清掉（轮询等待删除生效，期望最终 None）
        gone_b = _poll_get(store, EID_B_1, PROJECT_B, expect_present=False)
        assert gone_b is None, "B 租户的 clear 未生效：B 的对象仍可检索"
    finally:
        # ── 6) teardown：删整个专属测试集合（安全）+ 关连接 ──
        try:
            # 题面指明用 store._client.collections.delete(...)
            if store._client and store._client.collections.exists(ISO_COLLECTION):
                store._client.collections.delete(ISO_COLLECTION)
        except Exception:
            pass
        try:
            store.close()
        except Exception:
            pass
