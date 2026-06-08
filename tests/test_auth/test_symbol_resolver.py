"""resolve_symbol_at 三级解析编排单测（纯函数，mock graph + tmp_path 真文件）。

设计 [[代码查看器-IDE化导航-设计]] §4.1 三级流程：
  1. 位置 → 边命中（graph.resolve_at_position）
  2. 落空 + token → 名字 FTS（graph.find_by_name）
  3. 命中接口方法 → graph.resolve_impl 改写为 impl
出：{entity_id, has_source, kind, signature?, summary?} 或 None。

Task 2 / Step 1-2。
"""
# unittest.mock.Mock：标准库的通用 mock 对象，可自动响应任意属性访问与方法调用
from unittest.mock import Mock

# 被测节点数据类（dataclass 实例直接构造，用于 mock 返回）
from src.integrations.codegraph.db import CgNode

# 被测函数：Task 2 待实现
from src.service.qa_engine.symbol_resolver import resolve_symbol_at


def _make_node(
    *,
    kind: str = "class",
    qn: str = "Foo",
    file_path: str = "Foo.java",
    sig: str | None = None,
) -> CgNode:
    """构造 CgNode（frozen dataclass），降低 mock 样板。

    qn.split('::')[-1]：从右取方法/类短名（'Foo::m' → 'm'；'Foo' → 'Foo'）。
    """
    return CgNode(
        id="n_" + qn.replace("::", "_"),  # id 不参与断言，仅占位
        kind=kind,                         # 'class'/'interface'/'method'
        name=qn.split("::")[-1],           # 短名
        qualified_name=qn,                 # 全限定名
        file_path=file_path,               # 相对 repo 根的路径
        start_line=1,                      # 起始行（占位）
        end_line=10,                       # 结束行（占位）
        signature=sig,                     # 仅 hover 用；其它测试可传 None
    )


def _graph_mock(
    *,
    pos: str | None = None,
    name_hits: tuple[str, ...] = (),
    impl: str | None = None,
    first_node: CgNode | None = None,
):
    """造一个 graph mock，按需返回三原语 + resolve_first 的结果。

    Mock() 默认所有属性都自动生成可调用 Mock；这里显式设 return_value 让断言可控。
    """
    g = Mock()
    g.resolve_at_position.return_value = pos          # 位置级返回
    g.find_by_name.return_value = list(name_hits)     # 名字级返回（list 拷贝防 mutation）
    g.resolve_impl.return_value = impl                # 接口→impl 改写
    g.resolve_first.return_value = first_node         # node 元数据（kind/signature/file_path）
    return g


# ───────────────────────────────────────────────────────────────────────────────
# Test 1: 位置命中（最常见路径）
# ───────────────────────────────────────────────────────────────────────────────
def test_position_hit_returns_entity(tmp_path):
    """位置命中 → 返 {entity_id, has_source=True, kind}；磁盘文件存在 → has_source=True。"""
    # 在 tmp_path 内造源文件，让 os.path.exists(repo + node.file_path) 命中
    (tmp_path / "Foo.java").write_text("class Foo {}")
    foo = _make_node(kind="class", qn="Foo", file_path="Foo.java")
    graph = _graph_mock(pos="Foo", first_node=foo)    # impl=None 默认不改写
    out = resolve_symbol_at(
        graph, None,                                  # interp_store=None：非 hover 路径无需
        file_path="Bar.java", line=15, col=8,
        token=None, context_entity_id=None,
        repo_local_path=str(tmp_path),
    )
    # want_doc=False 默认 → 不带 signature/summary 字段，dict 严格等价
    assert out == {"entity_id": "Foo", "has_source": True, "kind": "class"}


# ───────────────────────────────────────────────────────────────────────────────
# Test 2: 位置落空 → token 名字 FTS 回退
# ───────────────────────────────────────────────────────────────────────────────
def test_position_miss_falls_back_to_name(tmp_path):
    """位置 None → 用 token 走 find_by_name → 取首个 entity_id。"""
    (tmp_path / "Foo.java").write_text("class Foo {}")
    foo = _make_node(kind="class", qn="Foo", file_path="Foo.java")
    # pos=None 模拟位置级落空；name_hits=("Foo",) 名字级有候选
    graph = _graph_mock(pos=None, name_hits=("Foo",), first_node=foo)
    out = resolve_symbol_at(
        graph, None,
        file_path=None, line=None, col=None,           # 位置参数全 None
        token="Foo", context_entity_id=None,
        repo_local_path=str(tmp_path),
    )
    assert out is not None
    assert out["entity_id"] == "Foo"                   # 首个候选
    assert out["has_source"] is True                   # 文件存在
    assert out["kind"] == "class"


# ───────────────────────────────────────────────────────────────────────────────
# Test 3: 接口方法 → 改写为 impl entity_id
# ───────────────────────────────────────────────────────────────────────────────
def test_iface_method_rewritten_to_impl(tmp_path):
    """位置命中接口方法 → resolve_impl 改写为 impl entity_id；元数据用 impl 节点。"""
    (tmp_path / "SvcImpl.java").write_text("class SvcImpl {}")
    impl_node = _make_node(kind="method", qn="SvcImpl::m", file_path="SvcImpl.java")
    graph = _graph_mock(
        pos="ISvc::m#()",                              # 位置命中接口方法
        impl="SvcImpl::m#()",                          # resolve_impl 改写为 impl
        first_node=impl_node,                          # 元数据查 impl（改写后的 entity_id）
    )
    out = resolve_symbol_at(
        graph, None,
        file_path="X.java", line=1, col=1,
        token=None, context_entity_id=None,
        repo_local_path=str(tmp_path),
    )
    assert out is not None
    assert out["entity_id"] == "SvcImpl::m#()"         # entity_id 已改写
    assert out["has_source"] is True
    assert out["kind"] == "method"


# ───────────────────────────────────────────────────────────────────────────────
# Test 4: hover（want_doc=True）→ 附 signature + summary（解读首句）
# ───────────────────────────────────────────────────────────────────────────────
def test_hover_includes_signature_and_summary(tmp_path):
    """want_doc=True → 附 signature（节点）+ summary（interpretation_text 首句）。"""
    (tmp_path / "Foo.java").write_text("class Foo {}")
    # 方法节点带 signature='(Long id)'，hover 渲染时给前端用
    foo = _make_node(
        kind="method", qn="Foo::m", file_path="Foo.java", sig="(Long id)"
    )
    graph = _graph_mock(pos="Foo::m#(Longid)", first_node=foo)
    # composite/interpretation store mock：get_by_entity 返回 2b 解读全文 dict
    interp = Mock()
    interp.get_by_entity.return_value = {
        # interpretation_text 是 2b 解读字段名（与 weaviate_interpretation_store 对齐）
        "interpretation_text": "返回订单。多句继续。",
    }
    out = resolve_symbol_at(
        graph, interp,
        file_path="Bar.java", line=1, col=1,
        token=None, context_entity_id=None,
        repo_local_path=str(tmp_path),
        want_doc=True,                                 # 启用 hover 路径
    )
    assert out is not None
    assert out["signature"] == "(Long id)"             # 节点 signature 透传
    assert out["summary"] == "返回订单"                 # 中文句号前的首句


# ───────────────────────────────────────────────────────────────────────────────
# Test 5: 命中实体但文件不在磁盘 → has_source=False（暂无源码占位驱动）
# ───────────────────────────────────────────────────────────────────────────────
def test_no_source_when_file_missing(tmp_path):
    """位置命中 entity 但磁盘上无该文件 → has_source=False（前端显示"暂无源码"）。"""
    # foo.file_path='Missing.java'，但 tmp_path 下没造这文件
    foo = _make_node(kind="class", qn="Foo", file_path="Missing.java")
    graph = _graph_mock(pos="Foo", first_node=foo)
    out = resolve_symbol_at(
        graph, None,
        file_path="Bar.java", line=15, col=8,
        token=None, context_entity_id=None,
        repo_local_path=str(tmp_path),                 # 真实存在但无 Missing.java
    )
    assert out is not None
    assert out["entity_id"] == "Foo"
    assert out["has_source"] is False                  # 关键断言


# ───────────────────────────────────────────────────────────────────────────────
# Test 6: 三级全落空 → None
# ───────────────────────────────────────────────────────────────────────────────
def test_all_miss_returns_none(tmp_path):
    """位置 None + token 空 + find_by_name 返 [] → 整体返 None。"""
    graph = _graph_mock(pos=None, name_hits=(), first_node=None)
    out = resolve_symbol_at(
        graph, None,
        file_path=None, line=None, col=None,
        token=None, context_entity_id=None,
        repo_local_path=str(tmp_path),
    )
    assert out is None                                 # 全落空


# ───────────────────────────────────────────────────────────────────────────────
# Test 7: graph 抛异常 → 视为该级未命中（fail-soft）
# ───────────────────────────────────────────────────────────────────────────────
def test_graph_exception_treated_as_miss(tmp_path):
    """resolve_at_position 抛 sqlite 异常 → 不传染，走名字回退或返 None。"""
    graph = Mock()
    graph.resolve_at_position.side_effect = RuntimeError("boom")  # 抛异常
    graph.find_by_name.return_value = []                          # 回退也空
    graph.resolve_impl.return_value = None
    graph.resolve_first.return_value = None
    out = resolve_symbol_at(
        graph, None,
        file_path="Bar.java", line=1, col=1,
        token="Foo", context_entity_id=None,
        repo_local_path=str(tmp_path),
    )
    # 位置级抛错 → 视为未命中 → 走 token 回退 → 也空 → None
    assert out is None
