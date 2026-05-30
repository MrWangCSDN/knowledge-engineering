# tests/test_integrations/codegraph/test_durable_key.py
"""DurableKey 单测：方法带归一化参数签名、非方法只用 qualified_name、签名格式无关。"""
from src.integrations.codegraph.db import CgNode
from src.integrations.codegraph.durable_key import durable_key


def _m(qn, sig, kind="method"):
    # 造一个最小 CgNode 用于测 durable_key
    return CgNode(id="x", kind=kind, name="m", qualified_name=qn,
                  file_path="f", start_line=1, end_line=2, signature=sig)


def test_method_key_includes_normalized_signature():
    # 方法：qualified_name + '#' + 归一化参数（去空白/去泛型）
    assert durable_key(_m("A::m", "List<X> (Long id)")) == "A::m#(Longid)"


def test_signature_formatting_does_not_change_key():
    # 同一方法不同空白/泛型写法 → 同 key（格式无关，稳）
    a = durable_key(_m("A::m", "List<X> (Long id)"))
    b = durable_key(_m("A::m", "List< X >  (Long  id)"))
    assert a == b


def test_overload_keys_differ():
    # 参数不同 → key 不同（重载可分）
    assert durable_key(_m("A::m", "(Long id)")) != durable_key(_m("A::m", "(String s)"))


def test_non_method_uses_qualified_name_only():
    # 非方法(类等)：只用 qualified_name
    assert durable_key(_m("A::Foo", None, kind="class")) == "A::Foo"
