"""composite.get_code_snippet：fail-soft 取真实 code_snippet（source-first grounding P1）。

P1 设计见 Obsidian [[业务问答-源码优先接地-P1设计]]：retriever 召回后预读 top-3 候选真实源码注入
prompt，治 agent/6段 自由展开代码细节时的臆造。本文件测最底层取数：composite.get_code_snippet。
"""
# 从被测模块导入 CompositeKnowledgeStore（复合知识库：解读库优先、CodeEntity 兜底）
from src.knowledge.composite_knowledge_store import CompositeKnowledgeStore


def _mk(code_store):
    """构造一个 CompositeKnowledgeStore 测试替身。

    interpretation_store 在 __init__ 里只被赋值、不被调用，故传占位 object() 即可；
    code_store 是本测试的关注点（get_code_snippet 从它取源码）。
    """
    # __init__ 是 keyword-only（签名里有 `*`），三个参数都必须用关键字传
    return CompositeKnowledgeStore(
        interpretation_store=object(),  # 占位：构造期不调用，避免引真实依赖
        code_store=code_store,          # 被测数据源
        project_id="p",
    )


def test_returns_snippet_when_code_store_has_it():
    """code_store 能按 entity_id 取到记录 → 返回其 code_snippet 字段。"""
    # 内部类充当 code_store 替身：proto 声明的 search_by_text + P1 用到的 get_by_entity_id
    class _CS:
        def search_by_text(self, *a, **k):
            return []
        def get_by_entity_id(self, eid):
            # 真实 code_store 的返回形如 {name, entity_type, code_snippet}
            return {"name": "m", "code_snippet": "public void m(){...}"}
    # 断言：取到真实源码片段原样返回
    assert _mk(_CS()).get_code_snippet("A::m#()") == "public void m(){...}"


def test_none_when_code_store_is_none():
    """解读-only 部署（code_store 未注入）→ 优雅返回 None，不抛。"""
    assert _mk(None).get_code_snippet("A::m#()") is None


def test_none_when_code_store_lacks_get_by_entity_id():
    """_CodeStoreLike proto 只声明 search_by_text；store 实例无 get_by_entity_id 时不能崩。"""
    class _CS:
        def search_by_text(self, *a, **k):
            return []
    # hasattr/getattr 防御：无此方法 → None
    assert _mk(_CS()).get_code_snippet("A::m#()") is None


def test_none_when_get_by_entity_id_raises_or_empty():
    """后端异常 / 查不到 → 一律 fail-soft 返回 None。"""
    class _Raise:
        def search_by_text(self, *a, **k):
            return []
        def get_by_entity_id(self, eid):
            raise RuntimeError("boom")  # 模拟后端异常
    class _Empty:
        def search_by_text(self, *a, **k):
            return []
        def get_by_entity_id(self, eid):
            return None                 # 模拟查不到该实体
    assert _mk(_Raise()).get_code_snippet("A::m#()") is None   # 异常 fail-soft
    assert _mk(_Empty()).get_code_snippet("A::m#()") is None   # 查不到
