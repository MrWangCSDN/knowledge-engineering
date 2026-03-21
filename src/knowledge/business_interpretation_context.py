"""业务解读：上下文拼装与提示词模板（无 LLM/Weaviate 副作用）。

与 ``business_interpretation_runner`` 分离，便于单测与 pipeline 层复用（如进度统计）。
"""
from __future__ import annotations

from typing import Iterable

from src.models import (
    StructureFacts,
    StructureEntity,
    EntityType,
    RelationType,
    DomainKnowledge,
)


def iter_entities_by_types(facts: StructureFacts, etypes: Iterable[EntityType]) -> list[StructureEntity]:
    ets = set(etypes)
    return [e for e in facts.entities if e.type in ets]


def structure_class_role(e: StructureEntity) -> str:
    name = e.name or ""
    if name.endswith("Controller"):
        return "Controller"
    if name.endswith("Service") or name.endswith("Facade") or name.endswith("Manager"):
        return "Service"
    if name.endswith("Repository") or name.endswith("Dao") or name.endswith("Mapper"):
        return "Repository"
    return "Other"


def build_class_context(
    clazz: StructureEntity,
    facts: StructureFacts,
    domain: DomainKnowledge,
) -> tuple[str, str, list[str], str, str]:
    """返回 (business_domain, capabilities, context_text, role, module_id)。"""
    attrs = clazz.attributes or {}
    module = clazz.module_id or ""
    pkg = ""
    file_loc = clazz.location or ""
    if ":" in file_loc:
        path = file_loc.split(":", 1)[0]
    else:
        path = file_loc
    pkg = attrs.get("package_name") or path.replace("\\", "/").rsplit("/", 2)[-2] if "/" in path else ""
    role = structure_class_role(clazz)
    methods = [
        e for e in facts.entities if e.type == EntityType.METHOD and (e.attributes or {}).get("class_name") == clazz.name
    ]
    method_lines: list[str] = []
    for m in methods[:40]:
        ma = m.attributes or {}
        sig = ma.get("signature") or m.name
        path_attr = ma.get("path") or ""
        method_lines.append(f"- {sig}" + (f"  路由: {path_attr}" if path_attr else ""))

    biz_domain_ids: list[str] = []
    for m in domain.service_domain_mappings:
        if m.service_or_module_id == module:
            biz_domain_ids.extend(m.business_domain_ids or [])
    biz_domain_ids = list(dict.fromkeys(biz_domain_ids))
    biz_domains = [d.name or d.id for d in domain.business_domains if d.id in biz_domain_ids]
    biz_domain = ", ".join(biz_domains)

    caps: list[str] = []
    for m in methods[:30]:
        name = m.name or ""
        ma = m.attributes or {}
        path_attr = ma.get("path") or ""
        if any(k in name.lower() for k in ["create", "add", "save"]):
            caps.append("创建 / 新增")
        if any(k in name.lower() for k in ["update", "modify"]):
            caps.append("修改 / 更新")
        if any(k in name.lower() for k in ["delete", "remove"]):
            caps.append("删除 / 移除")
        if any(k in name.lower() for k in ["list", "page", "query", "search"]):
            caps.append("查询 / 列表 / 搜索")
        if "/order" in path_attr:
            caps.append("订单相关操作")
        if "/product" in path_attr or "/goods" in path_attr:
            caps.append("商品相关操作")
    caps = list(dict.fromkeys(caps))
    caps_str = ", ".join(caps)

    ctx_lines = [
        f"类名: {clazz.name}",
        f"角色: {role}",
        f"模块: {module}",
        f"推测包名/路径: {pkg}",
        f"关联业务域: {biz_domain or '未知'}",
        "",
        "主要方法（含路由信息，节选）:",
        *(method_lines or ["(无方法或未收集)"]),
    ]
    return biz_domain, caps_str, "\n".join(ctx_lines), role, module


def build_api_context(
    method: StructureEntity,
    facts: StructureFacts,
    domain: DomainKnowledge,
) -> tuple[str, str, str, list[str]]:
    """返回 (biz_domain, use_case, context_text, related_ids)。"""
    attrs = method.attributes or {}
    class_name = attrs.get("class_name") or ""
    path_attr = attrs.get("path") or ""
    sig = attrs.get("signature") or method.name
    callers: list[str] = []
    callees: list[str] = []
    for r in facts.relations:
        if r.type == RelationType.CALLS and r.target_id == method.id:
            callers.append(r.source_id)
        if r.type == RelationType.CALLS and r.source_id == method.id:
            callees.append(r.target_id)
    callers = callers[:10]
    callees = callees[:10]
    id_to_name = {e.id: e.name for e in facts.entities}
    callers_n = [id_to_name.get(i, i) for i in callers]
    callees_n = [id_to_name.get(i, i) for i in callees]

    biz_domain_ids: list[str] = []
    clazz = next((e for e in facts.entities if e.type == EntityType.CLASS and e.name == class_name), None)
    module = clazz.module_id if clazz else ""
    for m in domain.service_domain_mappings:
        if m.service_or_module_id == module:
            biz_domain_ids.extend(m.business_domain_ids or [])
    biz_domain_ids = list(dict.fromkeys(biz_domain_ids))
    biz_domains = [d.name or d.id for d in domain.business_domains if d.id in biz_domain_ids]
    biz_domain = ", ".join(biz_domains)

    uc = "通用接口"
    lower_path = (path_attr or "").lower()
    lower_name = (method.name or "").lower()
    if any(k in lower_path for k in ["/order", "order/"]) or "order" in lower_name:
        uc = "订单相关用例"
    if any(k in lower_path for k in ["/cart", "cart/"]) or "cart" in lower_name:
        uc = "购物车相关用例"
    if any(k in lower_path for k in ["/login", "auth", "oauth"]) or "login" in lower_name:
        uc = "登录/认证用例"

    ctx_lines = [
        f"所属类: {class_name}",
        f"方法签名: {sig}",
        f"HTTP 路由: {path_attr or '(未识别)'}",
        f"关联业务域: {biz_domain or '未知'}",
        "",
        f"上游调用方（直接调用本方法，节选）: {', '.join(callers_n) if callers_n else '无'}",
        f"下游依赖（本方法直接调用，节选）: {', '.join(callees_n) if callees_n else '无'}",
    ]
    related_ids = [method.id] + callers + callees + ([clazz.id] if clazz else [])
    related_ids = list(dict.fromkeys(related_ids))
    return biz_domain, uc, "\n".join(ctx_lines), related_ids[:32]


def build_module_context(
    module_id: str,
    facts: StructureFacts,
    domain: DomainKnowledge,
) -> tuple[str, str, str, list[str]]:
    """返回 (biz_domain, capabilities, context_text, related_entity_ids)。"""
    classes = [e for e in facts.entities if e.type == EntityType.CLASS and e.module_id == module_id]
    methods = [e for e in facts.entities if e.type == EntityType.METHOD and e.module_id == module_id]
    controllers = [c for c in classes if structure_class_role(c) == "Controller"]
    services = [c for c in classes if structure_class_role(c) == "Service"]
    biz_domain_ids: list[str] = []
    for m in domain.service_domain_mappings:
        if m.service_or_module_id == module_id:
            biz_domain_ids.extend(m.business_domain_ids or [])
    biz_domain_ids = list(dict.fromkeys(biz_domain_ids))
    biz_domains = [d.name or d.id for d in domain.business_domains if d.id in biz_domain_ids]
    biz_domain = ", ".join(biz_domains)

    caps: list[str] = []
    for m in methods[:200]:
        path_attr = (m.attributes or {}).get("path") or ""
        if "/order" in path_attr:
            caps.append("订单能力")
        if "/product" in path_attr or "/goods" in path_attr:
            caps.append("商品能力")
        if "/search" in path_attr:
            caps.append("搜索能力")
        if "/member" in path_attr or "/user" in path_attr:
            caps.append("会员/用户能力")
    caps = list(dict.fromkeys(caps))
    caps_str = ", ".join(caps)

    ctx_lines = [
        f"模块 ID: {module_id}",
        f"关联业务域: {biz_domain or '未知'}",
        "",
        f"类总数: {len(classes)}，Controller 数: {len(controllers)}，Service 数: {len(services)}",
        "",
        "部分 Controller:",
        *[f"- {c.name}" for c in controllers[:20]],
        "",
        "部分 Service:",
        *[f"- {s.name}" for s in services[:20]],
    ]
    related = [c.id for c in classes[:200]]
    return biz_domain, caps_str, "\n".join(ctx_lines), related


def build_class_prompt(language: str, domain_text: str, ctx: str) -> str:
    if (language or "zh").lower().startswith("en"):
        return f"""You are a senior domain architect. Based on the following CONTEXT, write a business-oriented explanation of this CLASS / SERVICE.

Domain background:
{domain_text}

### Context
{ctx}

### Requirements
- Describe in English: which business domains and capabilities this class mainly belongs to.
- Explain its role in typical business flows (for example, order placement, product management).
- Summarize 3-7 key responsibilities in bullet points.

Business explanation:"""
    return f"""你是一名资深领域架构师。请根据下面的「类上下文信息」和「领域背景」，输出该类/服务的业务解读。

领域背景：
{domain_text}

### 上下文
{ctx}

### 要求
- 使用简体中文说明：该类主要属于哪些业务域、承担哪些业务能力。
- 结合方法与路由信息，概述它在典型业务流程（如下单、商品管理）中的角色。
- 用 3~7 条要点列出业务职责，不要重复贴代码。

业务解读："""


def build_api_prompt(language: str, domain_text: str, ctx: str) -> str:
    if (language or "zh").lower().startswith("en"):
        return f"""You are a senior domain architect. Based on the following API CONTEXT and BACKGROUND, explain the business use case.

Domain background:
{domain_text}

### API Context
{ctx}

### Requirements
- Describe the main business scenario for this API (who uses it and for what).
- Explain the high-level flow from request to persistence, omitting low-level technical details.
- List important preconditions and side effects.

Business explanation:"""
    return f"""你是一名资深领域架构师。请根据下面的「API 上下文」和「领域背景」，输出该接口的业务用例解读。

领域背景：
{domain_text}

### API 上下文
{ctx}

### 要求
- 说明：是谁在什么业务场景下调用这个接口。
- 概述从请求到持久化的大致业务流程，忽略底层技术细节。
- 列出关键前置条件和重要的业务副作用（如库存变化、状态流转等）。

业务解读："""


def build_module_prompt(language: str, domain_text: str, ctx: str) -> str:
    if (language or "zh").lower().startswith("en"):
        return f"""You are a senior domain architect. Based on the module CONTEXT and DOMAIN background, summarize the business responsibilities of this module/service.

Domain background:
{domain_text}

### Module Context
{ctx}

### Requirements
- Describe which business domains this module belongs to and what main capabilities it provides.
- Summarize typical end-to-end flows that cross multiple services within this module.
- Provide a concise textual overview suitable for documentation.

Business explanation:"""
    return f"""你是一名资深领域架构师。请根据下面的「模块上下文」和「领域背景」，输出该模块/服务的业务综述。

领域背景：
{domain_text}

### 模块上下文
{ctx}

### 要求
- 说明本模块主要覆盖哪些业务域，以及提供哪些核心业务能力。
- 概述典型的端到端业务流程（可跨多个类/服务），用文字串起来。
- 输出适合作为架构文档中的模块级业务说明。

业务解读："""


def format_domain_background(domain: DomainKnowledge) -> str:
    lines: list[str] = []
    if domain.business_domains:
        lines.append("业务域：")
        for d in domain.business_domains[:10]:
            caps = ", ".join(d.capability_ids or [])
            lines.append(f"- {d.name or d.id}: {caps}")
    if domain.capabilities:
        lines.append("")
        lines.append("业务能力：")
        for c in domain.capabilities[:15]:
            lines.append(f"- {c.get('name', c.get('id', ''))}: {c.get('path_pattern', '')}")
    return "\n".join(lines)
