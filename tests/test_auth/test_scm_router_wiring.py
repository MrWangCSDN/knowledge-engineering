"""校验 api.py 给 create_scm_routes(...) 这一处 mount 装配了 oauth_cfg/get_login_provider（callback 闭包参）。
用 AST 锚定到 create_scm_routes 调用，而非全文件子串——后者在本仓恒为真（行 108/115 已含同名 kwarg）。"""
import ast
import inspect


def test_create_scm_routes_mount_wires_callback_params():
    import src.service.api as api                       # import 冒烟：装配期不抛即通过这一半
    tree = ast.parse(inspect.getsource(api))
    calls = [n for n in ast.walk(tree)
             if isinstance(n, ast.Call) and getattr(n.func, "id", None) == "create_scm_routes"]
    assert len(calls) == 1, f"期望恰好一处 create_scm_routes 调用，实得 {len(calls)}"
    kw = {k.arg for k in calls[0].keywords}
    assert "oauth_cfg" in kw, "create_scm_routes mount 缺 oauth_cfg 接线"
    assert "get_login_provider" in kw, "create_scm_routes mount 缺 get_login_provider 接线"
