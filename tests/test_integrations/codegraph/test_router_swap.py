"""验证 CodeGraphGraphAdapter 满足 GraphProto 形状（可被注入到 retriever/tools 的 graph 位）。"""
from src.integrations.codegraph.graph_adapter import CodeGraphGraphAdapter


def test_codegraph_adapter_satisfies_graphproto():
    # hasattr 检查类是否定义了方法（不需要实例化）
    # GraphProto 约定：successors + predecessors 两个方法就是全部接口
    assert hasattr(CodeGraphGraphAdapter, "successors")
    assert hasattr(CodeGraphGraphAdapter, "predecessors")
