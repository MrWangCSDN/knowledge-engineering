"""BaseWeaviateStore.clear(tenant=...) 单元测试（多工程隔离 Phase1-T2）。

Mock-based，不连真 Weaviate。验证：
  - clear(tenant="p1")：只清该租户 —— 调 collection.tenants.remove(["p1"])，
    且**不**调 client.collections.delete（不删整集合）、不重建 schema。
  - clear()（无参 / tenant=None / tenant=""）：保持现状全集合删 —— 调
    client.collections.delete(集合名)，向后兼容单租户 / legacy。
  - tenants.remove 抛异常时被吞掉（best-effort，与现有语义一致）。

不另造 mock 框架，沿用项目里 MagicMock 直接注入假 client 的风格。
"""
# unittest.mock：标准库 mock 工具
# MagicMock：万能假对象，访问任意属性 / 方法都自动生成
# patch.object：上下文管理器，临时替换对象的方法，退出 with 自动还原
from unittest.mock import MagicMock, patch

# 被测基类
from src.knowledge.base_weaviate_store import BaseWeaviateStore


class _FakeStore(BaseWeaviateStore):
    """最小可实例化子类：提供抽象方法 _schema_properties 的空实现。

    BaseWeaviateStore 是 ABC（抽象基类），不能直接实例化；
    子类必须实现 @abstractmethod 标注的 _schema_properties 才能 new。
    这里只为测试 clear()，schema 返回空列表即可。
    """

    def _schema_properties(self):
        """返回空属性列表（测试 clear 用不到真实 schema）。"""
        return []


def _make_store():
    """构造一个 _FakeStore，但 mock 掉 _ensure_client_and_schema 避免真连 Weaviate。

    构造函数末尾会调用 _ensure_client_and_schema()（真去连 weaviate），
    用 patch.object 在 __init__ 期间把它替换成空 lambda，构造完再注入假 client。
    """
    # patch.object 作为上下文管理器：with 块内替换方法，退出后还原
    with patch.object(BaseWeaviateStore, "_ensure_client_and_schema", lambda self: None):
        store = _FakeStore(
            url="http://fake:8080",
            grpc_port=50051,
            collection_name="CodeEntity",
            dimension=1024,
        )
    # 注入假 client（MagicMock 会自动生成 collections / get / delete / exists 等链）
    store._client = MagicMock()
    return store


def test_clear_with_tenant_removes_only_that_tenant():
    """clear(tenant="p1")：只清该租户 —— 调 tenants.remove(["p1"])，不删整集合。"""
    store = _make_store()
    # _get_collection() 返回的假 collection，用它捕获 tenants.remove 调用
    fake_collection = MagicMock()
    store._client.collections.get.return_value = fake_collection

    store.clear(tenant="p1")

    # ① 用 Multi-Tenancy API 按租户删：tenants.remove(["p1"])（list[str]）
    fake_collection.tenants.remove.assert_called_once_with(["p1"])
    # ② 绝不删整个集合（删整集合会清掉所有工程的向量）
    store._client.collections.delete.assert_not_called()


def test_clear_with_tenant_does_not_rebuild_schema():
    """clear(tenant=...) 不应调用 _ensure_client_and_schema（不重建集合）。

    auto_tenant_creation=True 保证下次 with_tenant(tenant) 写入自动重建租户，
    所以无需在 clear 里重建 schema；重建反而多一次连接开销 / 副作用。
    """
    store = _make_store()
    store._client.collections.get.return_value = MagicMock()

    # patch.object 监视 _ensure_client_and_schema 是否被调用
    with patch.object(store, "_ensure_client_and_schema") as ensure:
        store.clear(tenant="p1")
    ensure.assert_not_called()


def test_clear_no_arg_deletes_whole_collection():
    """clear()（无参）：保持现状全集合删 —— 调 collections.delete(集合名)。"""
    store = _make_store()
    # 集合存在 → 才会走 delete 分支
    store._client.collections.exists.return_value = True

    # _ensure_client_and_schema 在无参路径会被调用重建；这里 mock 掉只测 delete
    with patch.object(store, "_ensure_client_and_schema"):
        store.clear()

    # 全集合删除，参数是集合名
    store._client.collections.delete.assert_called_once_with("CodeEntity")


def test_clear_none_deletes_whole_collection():
    """clear(tenant=None)：显式 None 也走全集合删（向后兼容）。"""
    store = _make_store()
    store._client.collections.exists.return_value = True

    with patch.object(store, "_ensure_client_and_schema"):
        store.clear(tenant=None)

    store._client.collections.delete.assert_called_once_with("CodeEntity")


def test_clear_empty_string_deletes_whole_collection():
    """clear(tenant="")：空字符串视为假值，走全集合删（与 force_full 真值判断一致）。"""
    store = _make_store()
    store._client.collections.exists.return_value = True

    with patch.object(store, "_ensure_client_and_schema"):
        store.clear(tenant="")

    store._client.collections.delete.assert_called_once_with("CodeEntity")


def test_clear_tenant_swallows_remove_exception():
    """tenants.remove 抛异常（如租户不存在）时被吞掉，不向上抛。"""
    store = _make_store()
    fake_collection = MagicMock()
    # 模拟 tenants.remove 抛异常（best-effort 语义要求吞掉）
    fake_collection.tenants.remove.side_effect = RuntimeError("tenant not found")
    store._client.collections.get.return_value = fake_collection

    # 不应抛出异常
    store.clear(tenant="p1")

    # 仍然没有误删整集合
    store._client.collections.delete.assert_not_called()
