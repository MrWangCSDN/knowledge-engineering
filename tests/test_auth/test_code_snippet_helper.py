# tests/test_auth/test_code_snippet_helper.py
"""build_snippet_response：组装 entity_id → 片段 JSON 的纯逻辑（无 HTTP/鉴权）。设计 [[代码片段查看器-设计]] §3。"""
from src.service.code_router import build_snippet_response, _lang_from_path


class _Node:
    qualified_name = "OmsPortalOrderController::confirmReceiveOrder"
    kind = "method"
    file_path = "mall-portal/src/.../OmsPortalOrderController.java"
    start_line = 1
    end_line = 2


class _Adapter:
    """假图适配器：resolve_first/successors_with_locations/callers。"""
    def __init__(self, node):
        self._node = node
    def resolve_first(self, entity_id):
        return self._node
    def successors_with_locations(self, entity_id):
        return [{"entity_id": "OmsPortalOrderService::confirmReceiveOrder#(Long)", "name": "confirmReceiveOrder", "line": 2, "col": 8}]
    def callers(self, entity_id):
        return [{"entity_id": "X::y#()", "name": "y"}]


def test_lang_from_path():
    assert _lang_from_path("a/B.java") == "java"
    assert _lang_from_path("a/M.xml") == "xml"
    assert _lang_from_path("a/s.py") == "python"
    assert _lang_from_path("a/app.yml") == "yaml"
    assert _lang_from_path("a/x.unknownext") == "plaintext"


def test_build_ok(tmp_path):
    f = tmp_path / "mall-portal" / "src"
    f.mkdir(parents=True)
    (f / "OmsPortalOrderController.java").write_text("line1\nline2\n", encoding="utf-8")
    node = _Node()
    node.file_path = "mall-portal/src/OmsPortalOrderController.java"
    out = build_snippet_response(_Adapter(node), str(tmp_path), "OmsPortalOrderController::confirmReceiveOrder#(Long)")
    assert out["language"] == "java"
    assert out["code"] == "line1\nline2"
    assert out["start_line"] == 1 and out["end_line"] == 2
    assert out["qualified_name"] == "OmsPortalOrderController::confirmReceiveOrder"
    assert out["callees"][0]["line"] == 2 and out["callees"][0]["col"] == 8
    assert out["callers"] == [{"entity_id": "X::y#()", "name": "y"}]


def test_build_none_when_entity_missing(tmp_path):
    class _NullAdp:
        def resolve_first(self, entity_id): return None
        def successors_with_locations(self, entity_id): return []
        def callers(self, entity_id): return []
    assert build_snippet_response(_NullAdp(), str(tmp_path), "Ghost::x#()") is None


def test_build_none_when_path_escapes(tmp_path):
    class _EscapeNode(_Node):
        file_path = "../../etc/passwd"
    assert build_snippet_response(_Adapter(_EscapeNode()), str(tmp_path), "X::y#()") is None


def test_build_empty_code_when_file_missing(tmp_path):
    """源码文件不存在 → read_snippet 返 ''；仍返回 dict（code=''）而非 None，让前端显示空片段。"""
    node = _Node()
    node.file_path = "nonexistent/Foo.java"   # tmp_path 下不存在该文件
    out = build_snippet_response(_Adapter(node), str(tmp_path), "X::y#()")
    assert out is not None                     # 文件读不到不代表实体不存在 → 仍返 dict
    assert out["code"] == ""                   # 空片段，前端可显示"源码不可用"


def test_lang_properties_is_ini():
    """.properties → ini（最不直观的一条映射，单独锁住）。"""
    assert _lang_from_path("a/app.properties") == "ini"
