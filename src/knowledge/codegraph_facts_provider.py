"""把 CodeGraph(.codegraph.db) 投影成 StructureFacts，供 TopologicalInterpreter 消费。

设计 [[2b解读重生-设计]] §4.1。核心价值：entity.id = CodeGraph qualified_name，
让重生的解读与 QA 召回/导航/代码片段同一套 id（消除 canonical_v1 ↔ qualified_name 漂移）。
"""
from __future__ import annotations

# 标准库 logging：记录读片段失败等诊断（不打断批量）
import logging

# StructureFacts 三件套（pydantic 模型）+ 枚举；注意关系类名是 StructureRelation
from src.models.structure import (
    StructureFacts,
    StructureEntity,
    StructureRelation,
    EntityType,
    RelationType,
)
# CodeGraph 只读访问层 + CgNode 投影 + 读源码片段
from src.integrations.codegraph.db import CodeGraphDB, CgNode
from src.integrations.codegraph.source_reader import read_snippet

# 模块级 logger
_LOG = logging.getLogger(__name__)


class CodeGraphFactsProvider:
    """CodeGraph → StructureFacts 投影器（纯读、可单测）。"""

    def __init__(self, *, db_path: str, repo_local_path: str) -> None:
        """
        Args:
            db_path: .codegraph.db 路径
            repo_local_path: 源码仓库根（read_snippet 用）
        """
        # CodeGraphDB 只读句柄；repo 根用于按 file_path + 行号读片段
        self._db = CodeGraphDB(db_path)
        self._repo = repo_local_path

    @staticmethod
    def _module_of(file_path: str) -> str:
        """file_path 顶层目录 = 模块（mall-portal/mall-admin/...）。与 ML module_of 同口径。"""
        # split('/', 1)[0]：取第一个 '/' 前的部分；无 '/' 时返回整串
        return (file_path or "").split("/", 1)[0]

    @staticmethod
    def _class_qn(method_qn: str) -> str:
        """从 'Class::method' 取 'Class'（CONTAINS 的 source）。无 '::' 时返回原串。"""
        # rsplit('::', 1)[0]：从右切一刀，取类全名部分
        return method_qn.rsplit("::", 1)[0] if "::" in method_qn else method_qn

    @staticmethod
    def _short_class_name(class_qn: str) -> str:
        """'com.x.OmsPortalOrderController' → 'OmsPortalOrderController'（短类名，attrs.class_name 用）。"""
        # rsplit('.', 1)[-1]：取最后一个 '.' 后的短名；无 '.' 时返回原串
        return class_qn.rsplit(".", 1)[-1]

    def _read_snippet_safe(self, node: CgNode) -> str:
        """读方法源码片段；任何异常（文件缺失/越界）→ 返回 ""（不中断批量）。"""
        try:
            # read_snippet 读 repo 下 file_path 的 [start_line, end_line]；or "" 兜 None
            return read_snippet(self._repo, node.file_path, node.start_line, node.end_line) or ""
        except Exception as e:  # noqa: BLE001 — best-effort：失败降级为空片段
            _LOG.debug("read_snippet 失败 %s: %s", node.qualified_name, e)
            return ""

    def build_structure_facts(self, *, module_filter: set[str] | None = None) -> StructureFacts:
        """遍历 CodeGraph method 节点，产出 StructureFacts（entity.id = qualified_name）。

        Args:
            module_filter: 只发射这些模块的 method 实体（Phase1 业务层白名单）；None = 全量。
                被过滤模块仍可作为 CALLS 目标出现在 relations 里（供上游解读文本引用），
                只是不单独成 entity / 不单独生成解读。
        Returns:
            StructureFacts
        """
        entities: list[StructureEntity] = []      # method + class 实体
        relations: list[StructureRelation] = []   # CALLS + CONTAINS
        seen_class_qns: set[str] = set()           # class 实体去重

        # 遍历所有 method 节点（CodeGraphDB 已只挑 kind='method'）
        for node in self._db.iter_method_nodes():
            module = self._module_of(node.file_path)
            # Phase1：模块白名单过滤（被过滤的方法不单独成 entity）
            if module_filter is not None and module not in module_filter:
                continue

            qn = node.qualified_name
            class_qn = self._class_qn(qn)

            # ① method 实体：id=qualified_name；attrs 填解读器读取的 key
            entities.append(StructureEntity(
                id=qn,
                type=EntityType.METHOD,
                name=node.name,
                module_id=module,
                attributes={
                    "code_snippet": self._read_snippet_safe(node),   # 解读器 _filter_meaningful 要求非空
                    "class_name": self._short_class_name(class_qn),  # 解读器 attrs["class_name"]
                    "signature": node.signature or "",               # 解读器 attrs["signature"]
                    "qualified_name": qn,
                },
            ))

            # ② CALLS 边：source/target 都投影成 qualified_name（与 entity.id 对齐）
            #    successors 入参是 CgNode.id（内部 id），返回的 callee 带 qualified_name
            for callee in self._db.successors(node.id, kind="calls"):
                relations.append(StructureRelation(
                    type=RelationType.CALLS,
                    source_id=qn,
                    target_id=callee.qualified_name,
                ))

            # ③ CONTAINS 边：class → method（解读器据此解析 class_entity_id）
            relations.append(StructureRelation(
                type=RelationType.CONTAINS,
                source_id=class_qn,
                target_id=qn,
            ))
            seen_class_qns.add(class_qn)

        # ④ 补 class 实体（去重；attrs.class_name 供解读器/写入用）
        for class_qn in seen_class_qns:
            entities.append(StructureEntity(
                id=class_qn,
                type=EntityType.CLASS,
                name=self._short_class_name(class_qn),
                attributes={"class_name": self._short_class_name(class_qn)},
            ))

        return StructureFacts(
            entities=entities,
            relations=relations,
            meta={
                "source": "codegraph",
                "module_filter": sorted(module_filter) if module_filter else "all",
            },
        )
