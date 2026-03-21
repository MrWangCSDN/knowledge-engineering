"""方法调用关系提取：AST + 正则，从方法体中解析出 (target_fqn, method_name)。"""
from __future__ import annotations

import re
from typing import Any

import javalang


def get_method_body(src_code: str, method_name: str) -> str | None:
    """根据方法名从源码中截取方法体字符串。优先用 AST 取起止行，否则用正则+大括号平衡。"""
    try:
        tree = javalang.parse.parse(src_code)
        for _path, node in tree:
            if isinstance(node, javalang.tree.MethodDeclaration) and node.name == method_name:
                if not getattr(node, "body", None):
                    return ""
                start_line = node.position.line if node.position else 1
                end_node = node.body[-1]
                end_line = end_node.position.line if getattr(end_node, "position", None) else start_line
                lines = src_code.split("\n")
                return "\n".join(lines[max(0, start_line - 1) : end_line])
    except Exception:
        pass
    return _get_method_body_fallback(src_code, method_name)


def _get_method_body_fallback(src_code: str, method_name: str) -> str | None:
    """通过正则定位方法签名并用大括号平衡截取方法体。"""
    # 匹配方法声明：修饰符 + 返回类型 + 方法名 + (
    pattern = re.compile(
        r"\b(?:public|private|protected|static|final|\s)*(?:[\w<>,\s\[\]]+)\s+" + re.escape(method_name) + r"\s*\(",
        re.MULTILINE,
    )
    match = pattern.search(src_code)
    if not match:
        return None
    start = match.start()
    # 找到方法体开括号
    brace = src_code.find("{", start)
    if brace == -1:
        return None
    depth = 1
    i = brace + 1
    while i < len(src_code) and depth > 0:
        c = src_code[i]
        if c == "{":
            depth += 1
        elif c == "}":
            depth -= 1
        i += 1
    return src_code[brace + 1 : i - 1] if depth == 0 else None


def strip_comments_and_logs(body: str) -> str:
    """去除单行/多行注释及常见日志调用，便于正则匹配。"""
    if not body:
        return ""
    s = re.sub(r"//.*?$", "", body, flags=re.MULTILINE)
    s = re.sub(r"/\*.*?\*/", "", s, flags=re.DOTALL)
    s = re.sub(r"//.*?\n", "\n", s)
    s = re.sub(r"\b(?:log|logger|bizlog)\.[\w.]*\([^)]*\);?", "", s)
    return s


def _resolve_simple_type(simple_name: str, package_name: str | None, import_list: list[str]) -> str | None:
    """将简单类名解析为全限定名。"""
    if not simple_name:
        return None
    for imp in import_list:
        if imp.endswith("." + simple_name) or imp == simple_name:
            return imp
    if package_name:
        return f"{package_name}.{simple_name}"
    return simple_name


def get_parent_class_full(
    extends: Any, package_name: str | None, import_list: list[str]
) -> str | None:
    """从 extends 节点得到父类全限定名。"""
    if not extends:
        return None
    name = None
    if hasattr(extends, "name"):
        name = extends.name
    elif isinstance(extends, list) and extends and hasattr(extends[0], "name"):
        name = extends[0].name
    return _resolve_simple_type(name, package_name, import_list) if name else None


def extract_method_calls(
    src_code: str,
    method_name: str,
    package_name: str | None,
    import_list: list[str],
    current_class_name: str,
    parent_class_full_name: str | None,
    method_params: dict[str, str],
) -> list[tuple[str, str]]:
    """
    从方法体中提取方法调用，返回 [(target_fqn, callee_method_name), ...]。
    不包含任何第三方品牌或专有命名。
    """
    body = get_method_body(src_code, method_name)
    if body is None:
        return []
    body = strip_comments_and_logs(body)
    current_class_full = f"{package_name}.{current_class_name}" if package_name else current_class_name

    result: list[tuple[str, str]] = []
    seen: set[tuple[str, str]] = set()

    def add(fqn: str, m: str) -> None:
        if fqn and m and (fqn, m) not in seen:
            seen.add((fqn, m))
            result.append((fqn, m))

    # 工具类/静态调用：大写开头类名.方法(
    for m in re.finditer(r"([A-Z][a-zA-Z0-9_]*)\.([a-zA-Z0-9_]+)\(", body):
        cls_name, callee = m.group(1), m.group(2)
        fqn = _resolve_simple_type(cls_name, package_name, import_list)
        if fqn:
            add(fqn, callee)

    # 本类静态/无前缀调用：仅当方法名前是行首或空白/分号/括号时匹配，避免把 obj.method( 中的 method 当成无前缀调用
    # 排除关键字与易误判的短名（如 or/and 易与逻辑运算或重载方法混淆导致虚假 CALL）
    _skip_bare_call = frozenset((
        "if", "for", "while", "switch", "catch", "new",
        "or", "and", "not", "eq", "ne", "gt", "lt", "ge", "le",
    ))
    for m in re.finditer(r"(?:^|[\s;(){])([a-zA-Z0-9_]+)\(", body):
        callee = m.group(1)
        if callee not in _skip_bare_call:
            add(current_class_full, callee)

    # this.xxx.yyy(
    for m in re.finditer(r"this\.(\w+)\.(\w+)\(", body):
        obj_name, callee = m.group(1), m.group(2)
        type_m = re.search(rf"this\.{re.escape(obj_name)}\s*=\s*new\s+(\w+)\(", body)
        if type_m:
            obj_type = type_m.group(1)
            fqn = _resolve_simple_type(obj_type, package_name, import_list)
            if fqn:
                add(fqn, callee)

    # super.方法(
    for m in re.finditer(r"super\.(\w+)\(", body):
        if parent_class_full_name:
            add(parent_class_full_name, m.group(1))

    # this.方法(
    for m in re.finditer(r"this\.(\w+)\(", body):
        add(current_class_full, m.group(1))

    # 参数对象.方法(
    for param_name, param_type in method_params.items():
        for m in re.finditer(rf"{re.escape(param_name)}\.(\w+)\(", body):
            fqn = _resolve_simple_type(param_type, package_name, import_list)
            if fqn:
                add(fqn, m.group(1))

    # getBean(Xxx.class).方法(
    for m in re.finditer(r"\w+\.getBean\((\w+)\.class\)\.(\w+)\(", body):
        bean_type, callee = m.group(1), m.group(2)
        fqn = _resolve_simple_type(bean_type, package_name, import_list)
        if fqn:
            add(fqn, callee)

    # 其它 obj.方法( 或 chain.方法(
    obj_method = re.compile(r"([a-zA-Z0-9_]+(?:\.[a-zA-Z0-9_]+)*?)\.([a-zA-Z0-9_]+)\(")
    this_pat = re.compile(r"this\.(\w+)\(")
    getbean_pat = re.compile(r"\w+\.getBean\(\w+\.class\)\.(\w+)\(")
    param_names = set(method_params.keys())
    for m in obj_method.finditer(body):
        obj_chain, callee = m.group(1), m.group(2)
        if this_pat.match(m.group(0)) or getbean_pat.match(m.group(0)):
            continue
        if obj_chain in param_names:
            continue
        # 解析 obj_chain 的类型：可能是局部变量或成员，用源码中的声明推断
        obj_simple = obj_chain.split(".")[0]
        obj_type = _infer_object_type_from_source(src_code, obj_simple, param_names, method_params)
        if obj_type:
            fqn = _resolve_simple_type(obj_type, package_name, import_list)
            if fqn:
                add(fqn, callee)
        # 无法解析类型时不添加 (obj_chain, callee)，避免对同一调用既解析到真实方法又生成 method_ref 造成重复 CALL

    return result


def _infer_object_type_from_source(
    src_code: str,
    var_name: str,
    param_names: set[str],
    method_params: dict[str, str],
) -> str | None:
    """从源码中推断变量/成员的类型（声明、@Autowired 等）。"""
    if var_name in method_params:
        return method_params[var_name]
    type_before_var = rf"(\w+)\s+{re.escape(var_name)}\s*[=;]"
    type_before_assign = rf"(\w+)\s+{re.escape(var_name)}\s*=\s*\w+[\w.]*\("
    autowired = rf"@Autowired\s+(?:.*?)\s+(\w+)\s+{re.escape(var_name)}\b"
    private_field = rf"private\s+(?:final\s+)?(\w+)\s+{re.escape(var_name)}\b"
    patterns = [type_before_var, type_before_assign, autowired, private_field]
    for pat in patterns:
        m = re.search(pat, src_code, re.DOTALL)
        if m:
            return m.group(1)
    return None
