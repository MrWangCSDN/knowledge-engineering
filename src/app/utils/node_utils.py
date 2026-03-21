"""节点显示相关工具。"""
from __future__ import annotations


def format_node_display_label(n: dict) -> str:
    """统一的节点显示标签格式化，供多个步骤复用。"""
    name = n.get("name") or n.get("id") or ""
    et = (n.get("entity_type") or "").lower()
    if et == "method" and n.get("class_name"):
        sig = (n.get("signature") or "").strip()
        if sig:
            return f"{sig}（{n.get('class_name')}）"
        return f"{name}（{n.get('class_name')}）"
    if et == "api_endpoint":
        mod = n.get("module_id") or ""
        cls = n.get("class_name") or ""
        method = n.get("method_name") or ""
        if mod or cls or method:
            return f"{name} ({mod}：{cls}：{method})"
    return name or str(n)
