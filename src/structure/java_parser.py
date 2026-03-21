"""Java 结构抽取：基于 javalang 的 AST 解析，产出结构事实。"""
from __future__ import annotations

import hashlib
from pathlib import Path
from typing import Optional

import javalang
from javalang.tree import (
    ClassDeclaration,
    MethodDeclaration,
    InterfaceDeclaration,
    MethodInvocation,
    Annotation,
    ReturnStatement,
    MemberReference,
    This,
    StatementExpression,
    Assignment,
)

from src.models import (
    CodeInputSource,
    FileItem,
    StructureFacts,
    StructureEntity,
    StructureRelation,
    EntityType,
    RelationType,
)
from src.structure import method_calls


def _stable_id(prefix: str, *parts: str) -> str:
    """
    稳定实体 ID（canonical_v1）：对固定输入永远得到同一字符串，与构建次数无关。
    - file:    file://{仓库内相对路径}
    - package: package//sha12(包名)
    - class:   class//sha12(class|相对路径|类型简单名)
    - method:  method//sha12(method|class_id|Java签名)
    同一方法在「路径、类名、签名」不变时 method_id 不变，可与解读库、代码向量长期对齐。
    文件移动/重命名会导致 class_id、method_id 变化（需重新生成解读）。
    """
    raw = "|".join(str(p) for p in parts)
    h = hashlib.sha256(raw.encode()).hexdigest()[:12]
    return f"{prefix}//{h}"


def _is_trivial_getter_ast(m: MethodDeclaration) -> bool:
    """
    AST 级 getter 识别：
    - 方法体仅包含一个 ReturnStatement
    - 返回表达式是字段访问（x 或 this.x）
    """
    body = getattr(m, "body", None) or []
    stmts = [s for s in body if s is not None]
    if len(stmts) != 1:
        return False
    if not isinstance(stmts[0], ReturnStatement):
        return False
    expr = getattr(stmts[0], "expression", None)
    if isinstance(expr, MemberReference):
        # x 或 this.x 均视为字段
        return True
    if isinstance(expr, This) and hasattr(expr, "qualifier") and isinstance(expr.qualifier, MemberReference):
        return True
    return False


def _is_trivial_setter_ast(m: MethodDeclaration) -> bool:
    """
    AST 级 setter 识别：
    - 方法体 1~2 条语句
    - 至少有一条赋值：字段 = 参数
    - 可选一个 return this 或裸 return
    """
    body = getattr(m, "body", None) or []
    stmts = [s for s in body if s is not None]
    if not stmts or len(stmts) > 3:
        return False
    has_assign = False
    for s in stmts:
        # 赋值语句
        if isinstance(s, StatementExpression) and isinstance(getattr(s, "expression", None), Assignment):
            assign = s.expression
            lhs = getattr(assign, "expressionl", None) or getattr(assign, "lhs", None)
            rhs = getattr(assign, "value", None) or getattr(assign, "rhs", None)
            # 左边是字段、右边是标识符（参数）
            if isinstance(lhs, MemberReference) and getattr(lhs, "member", None):
                # 右边是标识符（参数），用 MemberReference 简单近似
                if isinstance(rhs, MemberReference) or getattr(rhs, "name", None):
                    has_assign = True
                    continue
        # 允许存在简单 return 语句（如 return this 或 return;）
        if isinstance(s, ReturnStatement):
            continue
    return has_assign


class JavaStructureExtractor:
    def __init__(self, repo_path: str, extract_cross_service: bool = True):
        self.repo_root = Path(repo_path)
        self.extract_cross_service = extract_cross_service
        self.entities: list[StructureEntity] = []
        self.relations: list[StructureRelation] = []
        self._file_ids: dict[str, str] = {}  # rel_path -> file entity id
        self._class_ids: dict[str, str] = {}  # (rel_path, class_name) -> class id
        self._method_ids: dict[str, str] = {}  # (class_id, method_sig) -> method id

    def extract(self, source: CodeInputSource) -> StructureFacts:
        self.entities = []
        self.relations = []
        self._file_ids = {}
        self._class_ids = {}
        self._method_ids = {}
        self._package_by_path: dict[str, str] = {}
        self._deferred_calls: list[tuple[str, str, str, str]] = []  # (caller_method_id, caller_class_id, target_fqn, callee_method_name)

        java_files = [f for f in source.files if (f.language or "").lower() == "java"]
        if not java_files:
            java_files = [
                f for f in source.files
                if f.path.endswith(".java")
            ]
        if not java_files and source.files:
            java_files = source.files

        for fitem in java_files:
            self._process_file(fitem, source)

        self._resolve_deferred_calls()

        if self.extract_cross_service:
            self._extract_cross_service_edges(source)
            self._extract_feign_bindings(source)

        self._enrich_relation_attributes()

        return StructureFacts(
            entities=self.entities,
            relations=self.relations,
            meta={
                "repo_path": source.repo_path,
                "language": "java",
                "entity_id_scheme": "canonical_v1",
                "entity_id_notes": (
                    "method_id 由 class_id + 方法 Java 签名确定性哈希；class_id 由文件相对路径 + 类型名哈希。"
                    "路径或签名不变则 ID 不变，解读库可与多次「仅图谱+代码」构建对齐。"
                ),
            },
        )

    def _process_file(self, fitem: FileItem, source: CodeInputSource) -> None:
        full_path = source.resolve_file_path(fitem.path)
        if not full_path.exists():
            return
        try:
            text = full_path.read_text(encoding="utf-8", errors="replace")
        except Exception:
            return

        file_id = f"file://{fitem.path}"
        if file_id not in self._file_ids:
            self._file_ids[fitem.path] = file_id
            self.entities.append(
                StructureEntity(
                    id=file_id,
                    type=EntityType.FILE,
                    name=full_path.name,
                    location=fitem.path,
                    module_id=fitem.module_id,
                    language="java",
                )
            )

        try:
            tree = javalang.parse.parse(text)
        except Exception:
            return

        pkg_name = getattr(tree, "package", None)
        pkg_name = pkg_name.name if pkg_name and hasattr(pkg_name, "name") else ""
        self._package_by_path[fitem.path] = pkg_name
        # 包按「包名」唯一，同一包名只对应一个节点（多文件可同属一包）
        pkg_id = _stable_id("package", pkg_name) if pkg_name else None
        import_list = [imp.path for imp in (getattr(tree, "imports", None) or [])]

        if pkg_name and pkg_id and not any(e.id == pkg_id for e in self.entities):
            self.entities.append(
                StructureEntity(
                    id=pkg_id,
                    type=EntityType.PACKAGE,
                    name=pkg_name,
                    module_id=None,  # 包与模块无关系，不写入 module_id
                    language="java",
                )
            )
            # 本体建模：file 与 package 无任何关系；class/interface 与 package 为 BELONGS_TO（见 _process_type_decl）

        for type_decl in tree.types or []:
            if isinstance(type_decl, (ClassDeclaration, InterfaceDeclaration)):
                self._process_type_decl(
                    type_decl, fitem, source, file_id, pkg_id,
                    source_code=text, package_name=pkg_name, import_list=import_list,
                )

    def _process_type_decl(
        self,
        type_decl: ClassDeclaration | InterfaceDeclaration,
        fitem: FileItem,
        source: CodeInputSource,
        file_id: str,
        pkg_id: Optional[str],
        *,
        source_code: str = "",
        package_name: str = "",
        import_list: Optional[list] = None,
    ) -> None:
        name = type_decl.name
        is_interface = isinstance(type_decl, InterfaceDeclaration)
        type_entity = EntityType.INTERFACE if is_interface else EntityType.CLASS
        class_id = _stable_id("class", fitem.path, name)
        self._class_ids[(fitem.path, name)] = class_id

        modifiers = getattr(type_decl, "modifiers", None) or []
        attrs: dict = {"visibility": list(modifiers) if isinstance(modifiers, set) else modifiers}
        feign_target = _extract_feign_client_target(type_decl)
        if feign_target:
            attrs["feign_target_service"] = feign_target
        class_path = _extract_mapping_path_from_annotations(getattr(type_decl, "annotations", None) or [])
        if class_path:
            attrs["path"] = class_path
        if not any(e.id == class_id for e in self.entities):
            line = type_decl.position.line if type_decl.position else None
            loc = f"{fitem.path}:{line}" if line else fitem.path
            self.entities.append(
                StructureEntity(
                    id=class_id,
                    type=type_entity,
                    name=name,
                    location=loc,
                    module_id=fitem.module_id,
                    language="java",
                    attributes=attrs,
                )
            )
            # 类与文件仅保留 class RELATES_TO file，不建 file CONTAINS class
            if pkg_id:
                self.relations.append(StructureRelation(type=RelationType.BELONGS_TO, source_id=class_id, target_id=pkg_id))
            self.relations.append(StructureRelation(type=RelationType.RELATES_TO, source_id=class_id, target_id=file_id))

        # 继承
        if getattr(type_decl, "extends", None):
            for ext in (type_decl.extends if isinstance(type_decl.extends, list) else [type_decl.extends]):
                if ext and hasattr(ext, "name"):
                    ext_id = _stable_id("class_ref", fitem.path, ext.name)
                    self.relations.append(StructureRelation(type=RelationType.EXTENDS, source_id=class_id, target_id=ext_id))
        # 实现
        if getattr(type_decl, "implements", None):
            for impl in type_decl.implements:
                if hasattr(impl, "name"):
                    impl_id = _stable_id("class_ref", fitem.path, impl.name)
                    self.relations.append(StructureRelation(type=RelationType.IMPLEMENTS, source_id=class_id, target_id=impl_id))

        import_list = import_list or []
        parent_class_full = method_calls.get_parent_class_full(
            getattr(type_decl, "extends", None), package_name or None, import_list
        )
        # 方法
        for member in type_decl.body or []:
            if isinstance(member, MethodDeclaration):
                self._process_method(
                    member, class_id, fitem,
                    source_code=source_code,
                    package_name=package_name or None,
                    import_list=import_list,
                    current_class_name=name,
                    parent_class_full_name=parent_class_full,
                )
            # 不再抽取 field 节点，仅处理方法

    def _process_method(
        self,
        method: MethodDeclaration,
        class_id: str,
        fitem: FileItem,
        *,
        source_code: str = "",
        package_name: Optional[str] = None,
        import_list: Optional[list] = None,
        current_class_name: str = "",
        parent_class_full_name: Optional[str] = None,
    ) -> None:
        name = method.name
        sig = _method_signature(method)
        method_id = _stable_id("method", class_id, sig)
        self._method_ids[(class_id, sig)] = method_id

        attrs: dict = {"signature": sig, "class_name": current_class_name}
        # AST 级 getter/setter 识别标记，供后续技术/业务解读与 UI 使用
        try:
            attrs["is_getter"] = bool(_is_trivial_getter_ast(method))
            attrs["is_setter"] = bool(_is_trivial_setter_ast(method))
        except Exception:
            # AST 识别失败时不影响后续处理
            attrs["is_getter"] = False
            attrs["is_setter"] = False
        path = _extract_mapping_path(method)
        if path:
            attrs["path"] = path
        # 方法体代码片段，供写入 Weaviate 向量库并与图谱方法节点关联
        body = method_calls.get_method_body(source_code, name)
        if body and body.strip():
            _MAX_SNIPPET = 12_000
            attrs["code_snippet"] = (body.strip()[: _MAX_SNIPPET] + ("..." if len(body) > _MAX_SNIPPET else ""))

        if not any(e.id == method_id for e in self.entities):
            line = method.position.line if method.position else None
            loc = f"{fitem.path}:{line}" if line else None
            self.entities.append(
                StructureEntity(
                    id=method_id,
                    type=EntityType.METHOD,
                    name=name,
                    location=loc,
                    module_id=fitem.module_id,
                    language="java",
                    attributes=attrs,
                )
            )
            self.relations.append(StructureRelation(type=RelationType.CONTAINS, source_id=class_id, target_id=method_id))
            self.relations.append(StructureRelation(type=RelationType.BELONGS_TO, source_id=method_id, target_id=class_id))

        method_params = {}
        for p in method.parameters or []:
            param_type = getattr(p.type, "name", None) or str(p.type)
            method_params[p.name] = param_type
        calls = method_calls.extract_method_calls(
            source_code, name,
            package_name, import_list or [],
            current_class_name, parent_class_full_name,
            method_params,
        )
        for target_fqn, callee_method in calls:
            self._deferred_calls.append((method_id, class_id, target_fqn, callee_method))

    def _entity_name(self, eid: str) -> str:
        """按 id 取实体 name，取不到返回空字符串。"""
        for e in self.entities:
            if e.id == eid:
                return e.name or ""
        return ""

    def _entity_by_id(self, eid: str) -> Optional[StructureEntity]:
        """按 id 取实体。"""
        for e in self.entities:
            if e.id == eid:
                return e
        return None

    def _enrich_relation_attributes(self) -> None:
        """为所有关系补充两端属性：source_name, target_name, source_type, target_type（属于方/被属于方）。"""
        for r in self.relations:
            src = self._entity_by_id(r.source_id)
            tgt = self._entity_by_id(r.target_id)
            r.attributes["source_name"] = src.name if src else r.source_id
            r.attributes["target_name"] = tgt.name if tgt else r.target_id
            r.attributes["source_type"] = src.type.value if src else "unknown"
            r.attributes["target_type"] = tgt.type.value if tgt else "unknown"

    def _resolve_deferred_calls(self) -> None:
        """根据全限定类名解析延迟的方法调用，仅对能解析到真实方法节点的调用建立 CALLS；无法解析的不建节点、不建边。"""
        fqn_to_class: dict[str, str] = {}
        for (path, class_name), class_id in self._class_ids.items():
            pkg = self._package_by_path.get(path, "")
            fqn = f"{pkg}.{class_name}" if pkg else class_name
            fqn_to_class[fqn] = class_id
        methods_by_class: dict[str, dict[str, list[str]]] = {}
        for (cid, sig), mid in self._method_ids.items():
            mname = sig.split("(")[0]
            methods_by_class.setdefault(cid, {}).setdefault(mname, []).append(mid)
        seen_edges: set[tuple[str, str]] = set()
        for caller_id, caller_class_id, target_fqn, callee_method in self._deferred_calls:
            callee_id = None
            target_class_id = fqn_to_class.get(target_fqn)
            if target_class_id:
                mids = methods_by_class.get(target_class_id, {}).get(callee_method)
                if mids:
                    callee_id = mids[0]
            if callee_id is None:
                continue
            if caller_id == callee_id:
                continue
            if (caller_id, callee_id) not in seen_edges:
                seen_edges.add((caller_id, callee_id))
                caller_class_name = self._entity_name(caller_class_id)
                caller_method_name = self._entity_name(caller_id)
                callee_method_name = self._entity_name(callee_id) or callee_method
                callee_class_name = self._entity_name(target_class_id)
                rel_attrs = {
                    "caller_class": caller_class_name,
                    "caller_method": caller_method_name,
                    "callee_class": callee_class_name,
                    "callee_method": callee_method_name,
                }
                self.relations.append(
                    StructureRelation(
                        type=RelationType.CALLS,
                        source_id=caller_id,
                        target_id=callee_id,
                        attributes=rel_attrs,
                    )
                )

    def _add_call_relation(
        self,
        caller_method_id: str,
        caller_class_id: str,
        fitem: Optional[FileItem],
        callee_name: str,
        callee_qualifier: Optional[str],
    ) -> None:
        """建立 CALLS 关系：仅当被调方法能解析到本类中的 method 节点时才连边；无法解析则不建节点、不建边。"""
        callee_id = None
        for (cid, sig), mid in self._method_ids.items():
            if cid != caller_class_id:
                continue
            if sig.startswith(callee_name + "("):
                callee_id = mid
                break
        if callee_id is None:
            return
        if caller_method_id == callee_id:
            return
        caller_class_name = self._entity_name(caller_class_id)
        caller_method_name = self._entity_name(caller_method_id)
        callee_method_name = self._entity_name(callee_id) or callee_name
        callee_class_name = ""
        for (cid, _), mid in self._method_ids.items():
            if mid == callee_id:
                callee_class_name = self._entity_name(cid)
                break
        if not callee_class_name and callee_qualifier:
            callee_class_name = callee_qualifier.rsplit(".", 1)[-1] if "." in (callee_qualifier or "") else (callee_qualifier or "")
        rel_attrs = {
            "caller_class": caller_class_name,
            "caller_method": caller_method_name,
            "callee_class": callee_class_name,
            "callee_method": callee_method_name,
        }
        self.relations.append(
            StructureRelation(type=RelationType.CALLS, source_id=caller_method_id, target_id=callee_id, attributes=rel_attrs)
        )

    def _extract_cross_service_edges(self, source: CodeInputSource) -> None:
        """服务边、服务—暴露—API、Feign 绑定。"""
        module_ids = {m.id for m in source.modules}

        def ensure_service_node(module_id: str) -> str:
            sid = f"service://{module_id}"
            if not any(x.id == sid for x in self.entities):
                self.entities.append(
                    StructureEntity(
                        id=sid,
                        type=EntityType.SERVICE,
                        name=module_id,
                        module_id=module_id,
                        language="java",
                    )
                )
            return sid

        for e in self.entities:
            if e.type == EntityType.CLASS or e.type == EntityType.INTERFACE:
                if e.module_id and e.module_id in module_ids:
                    sid = ensure_service_node(e.module_id)
                    self.relations.append(
                        StructureRelation(type=RelationType.BELONGS_TO, source_id=e.id, target_id=sid)
                    )

        for e in self.entities:
            if e.type == EntityType.METHOD and (e.attributes or {}).get("path") and e.module_id:
                api_id = e.id + "#api"
                if not any(x.id == api_id for x in self.entities):
                    attrs = e.attributes or {}
                    self.entities.append(
                        StructureEntity(
                            id=api_id,
                            type=EntityType.API_ENDPOINT,
                            name=attrs.get("path", ""),
                            module_id=e.module_id,
                            language="java",
                            attributes={
                                "method_entity_id": e.id,
                                "class_name": attrs.get("class_name", ""),
                                "method_name": e.name or "",
                            },
                        )
                    )
                sid = ensure_service_node(e.module_id)
                if not any(
                    r.source_id == sid and r.target_id == api_id and r.type == RelationType.SERVICE_EXPOSES
                    for r in self.relations
                ):
                    self.relations.append(
                        StructureRelation(type=RelationType.SERVICE_EXPOSES, source_id=sid, target_id=api_id)
                    )

    def _extract_feign_bindings(self, source: CodeInputSource) -> None:
        """为带 feign_target_service 的类/接口建立 BINDS_TO_SERVICE，并可选 SERVICE_CALLS。"""
        module_ids = {m.id for m in source.modules}
        for e in self.entities:
            if e.type not in (EntityType.CLASS, EntityType.INTERFACE):
                continue
            target = (e.attributes or {}).get("feign_target_service")
            if not target:
                continue
            target_sid = f"service://{target}"
            if not any(x.id == target_sid for x in self.entities):
                self.entities.append(
                    StructureEntity(
                        id=target_sid,
                        type=EntityType.SERVICE,
                        name=target,
                        module_id=target,
                        language="java",
                    )
                )
            self.relations.append(
                StructureRelation(type=RelationType.BINDS_TO_SERVICE, source_id=e.id, target_id=target_sid)
            )
            if e.module_id and e.module_id in module_ids:
                caller_sid = f"service://{e.module_id}"
                if any(x.id == caller_sid for x in self.entities):
                    self.relations.append(
                        StructureRelation(type=RelationType.SERVICE_CALLS, source_id=caller_sid, target_id=target_sid)
                    )


def _method_signature(m: MethodDeclaration) -> str:
    params = ",".join(
        getattr(p.type, "name", str(p.type)) if getattr(p, "type", None) else "?"
        for p in (m.parameters or [])
    )
    return f"{m.name}({params})"


def _extract_feign_client_target(type_decl) -> Optional[str]:
    """从类/接口上的 @FeignClient(name=...) 或 value=... 提取被调服务名。"""
    for ann in getattr(type_decl, "annotations", None) or []:
        if not isinstance(ann, Annotation):
            continue
        name = getattr(ann, "name", None)
        simple = name.split(".")[-1] if isinstance(name, str) else (getattr(name, "name", "") if hasattr(name, "name") else "")
        if simple != "FeignClient":
            continue
        for elem in _annotation_elements(ann):
            n = getattr(elem, "name", None)
            if n not in ("name", "value"):
                continue
            val = getattr(elem, "value", None)
            if isinstance(val, str):
                return val.strip('"')
            if val is not None and hasattr(val, "value"):
                return getattr(val, "value", "").strip('"')
        elts = _annotation_elements(ann)
        if elts:
            val = getattr(elts[0], "value", None)
            if isinstance(val, str):
                return val.strip('"')
            if val is not None and hasattr(val, "value"):
                return getattr(val, "value", "").strip('"')
    return None


def _annotation_elements(ann: Annotation) -> list:
    """将注解的 element 规范为 list（javalang 中可能是 list 或单个 Literal 等）。"""
    raw = getattr(ann, "element", None)
    if raw is None:
        return []
    if isinstance(raw, list):
        return raw
    return [raw]


def _extract_mapping_path_from_annotations(annotations: list) -> Optional[str]:
    """从注解列表中提取 @RequestMapping 等 path/value（类或方法通用）。"""
    path_annotations = ("RequestMapping", "GetMapping", "PostMapping", "PutMapping", "DeleteMapping", "PatchMapping")
    for ann in annotations or []:
        if not isinstance(ann, Annotation):
            continue
        name = getattr(ann, "name", None) or ""
        if isinstance(name, str):
            simple = name.split(".")[-1]
        else:
            simple = getattr(name, "name", "") if hasattr(name, "name") else ""
        if simple not in path_annotations:
            continue
        elts = _annotation_elements(ann)
        for elem in elts:
            if getattr(elem, "name", None) in ("value", "path"):
                val = getattr(elem, "value", None)
                if val is not None and isinstance(val, str):
                    return val.strip('"')
                if val is not None and hasattr(val, "value"):
                    return getattr(val, "value", "").strip('"')
        if elts:
            val = getattr(elts[0], "value", None)
            if isinstance(val, str):
                return val.strip('"')
            if val is not None and hasattr(val, "value"):
                return getattr(val, "value", "").strip('"')
    return None


def _extract_mapping_path(method: MethodDeclaration) -> str | None:
    """从方法上的 @RequestMapping / @GetMapping / @PostMapping 等提取 path/value。"""
    return _extract_mapping_path_from_annotations(getattr(method, "annotations", None) or [])


def _qualifier_str(node) -> str | None:
    """从 AST 节点取 qualifier 的字符串（如 b.c() 中的 b）。"""
    if node is None:
        return None
    if hasattr(node, "name"):
        return getattr(node, "name", None)
    if hasattr(node, "member"):
        return getattr(node, "member", None)
    return str(node) if node else None


def _collect_invocations_by_method(tree) -> dict:
    """从编译单元收集每个方法体内的 MethodInvocation，返回 id(method_decl) -> [(member, qualifier_str), ...]。"""
    out: dict = {}
    try:
        for path, node in tree.filter(MethodInvocation):
            if not isinstance(node, MethodInvocation):
                continue
            member = getattr(node, "member", None) or ""
            qual = getattr(node, "qualifier", None)
            qs = _qualifier_str(qual)
            for i in range(len(path) - 1, -1, -1):
                if isinstance(path[i], MethodDeclaration):
                    key = id(path[i])
                    out.setdefault(key, []).append((member, qs))
                    break
    except Exception:
        pass
    return out


