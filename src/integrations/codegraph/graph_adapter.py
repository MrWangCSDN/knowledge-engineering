# src/integrations/codegraph/graph_adapter.py
"""把 CodeGraphDB 包成 KE 的 GraphProto（successors/predecessors）。

GraphProto 的 entity_id 用 KE 的持久身份(durable_key)；
本 adapter 负责 durable_key ↔ CodeGraph 节点 的解析，返回也用 durable_key。
"""
from __future__ import annotations  # PEP 563：允许类型注解里提前引用未定义的类名

import logging   # 标准库日志模块；_LOG.warning 用于降级时留下可观测痕迹
import sqlite3   # 标准库 SQLite3 接口；捕获 sqlite3.Error 做优雅降级
from typing import Optional  # Optional[X] 表示值可以是 X 或 None
from src.integrations.codegraph.db import CodeGraphDB, CgNode  # 只读 DB 访问层与节点数据类
from src.integrations.codegraph.durable_key import durable_key  # 持久身份派生函数

# 模块级 logger：logging.getLogger(__name__) 以本模块全路径命名，方便按模块过滤日志
_LOG = logging.getLogger(__name__)

# 字典：GraphProto 的 rel_type（KE 语义）→ CodeGraph 边的 kind 字段
# None 表示调用方没传 rel_type，默认也走 'calls' 边
_REL_TO_KIND = {None: "calls", "calls": "calls", "CALLS": "calls"}


class CodeGraphGraphAdapter:
    """实现 src.service.qa_engine.retriever.GraphProto，后端是只读 SQLite。

    接口约定：
    - successors(entity_id, rel_type=None) -> list[str]   出边（callees）的 durable_key 列表
    - predecessors(entity_id, rel_type=None) -> list[str] 入边（callers）的 durable_key 列表
    """

    def __init__(self, db: CodeGraphDB) -> None:
        """构造器：注入只读 DB 访问层（依赖注入模式，方便单测传 mock）。"""
        self._db = db  # 保存对 DB 的引用，后续查询都走这个对象

    def _resolve(self, entity_id: str) -> list[CgNode]:
        """把 durable_key 解析回 CodeGraph 节点列表。

        策略：
        1. '#' 前缀是 qualified_name → 用 DB 按 qualified_name 查找全部候选
        2. 候选唯一 → 直接返回（最常见情况）
        3. 候选多个（重载）→ 用完整 durable_key 精筛；精筛为空则退回全部（宁多不漏）

        Args:
            entity_id: 持久身份 key，格式为 'Cls::method#(params)' 或 'Cls::method'
        Returns:
            匹配的 CgNode 列表
        """
        # 先剥 ke:// scheme（如 'method://Cls::m#(p)' → 'Cls::m#(p)'）：调用图节点/前端 EntityRef
        # 传入的 entityId 带 'method://' 等 scheme，而 qualified_name 列里不含它。split('://',1)[-1]
        # 对裸 qualified_name（'Cls::m'，含 '::' 但不含 '://'）无副作用，对带 scheme 的精确剥离。
        without_scheme = entity_id.split("://", 1)[-1]
        # split('#', 1)：最多切 1 刀，[0] 取 '#' 前的 qualified_name 部分
        qn = without_scheme.split("#", 1)[0]
        # 按 qualified_name 精确查找所有候选节点（可能包含重载）
        cands = self._db.find_nodes_by_qualified_name(qn)
        if len(cands) <= 1:             # 唯一或无结果 → 直接返回，无需精筛
            return cands
        # 多个候选（重载场景）：用完整 durable_key 再过滤一次
        # 列表推导式：保留 durable_key(n) 与去 scheme 后的 entity_id 完全匹配的节点
        # （用 without_scheme 而非 entity_id：durable_key 不含 scheme，否则带 scheme 时精筛恒空）
        exact = [n for n in cands if durable_key(n) == without_scheme]
        # or 短路：精筛有结果则用精筛结果；否则退回全部候选（防漏）
        return exact or cands

    def _walk(self, entity_id: str, rel_type: Optional[str], direction: str) -> list[str]:
        """successors/predecessors 公共逻辑，direction ∈ {'succ','pred'}。"""
        # 未知 rel_type（如 accesses_table，CodeGraph 无此边）→ get 返回 None → 直接返 []，不查图。
        # 不能 fallback 到 'calls'，否则会把 callees 误当成该类型边返回（修 Phase 1 引入的 bug）。
        kind = _REL_TO_KIND.get(rel_type, None)
        if kind is None:
            return []
        out: list[str] = []
        seen: set[str] = set()              # 去重（不同 source 节点可能指向同一目标）
        try:
            for node in self._resolve(entity_id):
                # 根据方向选 successors（出边）或 predecessors（入边）
                neighbors = (self._db.successors(node.id, kind) if direction == "succ"
                             else self._db.predecessors(node.id, kind))
                for nb in neighbors:
                    key = durable_key(nb)
                    if key not in seen:
                        seen.add(key)
                        out.append(key)
        except sqlite3.Error as e:          # 库缺失/损坏/锁 → 优雅降级返空，不整体崩（设计 §8，对齐旧 Neo4j 契约）
            _LOG.warning("[codegraph] 导航查询失败，降级返回空 (entity_id=%s): %s", entity_id, e)
            return []
        return out

    def successors(self, entity_id: str, rel_type: Optional[str] = None) -> list[str]:
        """返回出边(callees)的 durable_key 列表（即 entity_id 调用了哪些节点）。

        Args:
            entity_id: 调用方节点的持久 key
            rel_type:  关系类型，默认 None（等效 'calls'）
        Returns:
            被调用节点的 durable_key 列表
        """
        return self._walk(entity_id, rel_type, "succ")  # 'succ' 方向 = 出边

    def predecessors(self, entity_id: str, rel_type: Optional[str] = None) -> list[str]:
        """返回入边(callers)的 durable_key 列表（即谁调用了 entity_id）。

        Args:
            entity_id: 被调用节点的持久 key
            rel_type:  关系类型，默认 None（等效 'calls'）
        Returns:
            调用方节点的 durable_key 列表
        """
        return self._walk(entity_id, rel_type, "pred")  # 'pred' 方向 = 入边

    def resolve_first(self, entity_id: str) -> Optional[CgNode]:
        """把 entity_id 解析为单个 CgNode（重载/多命中取首个）；无命中或 sqlite 异常 → None。

        Args:
            entity_id: 持久身份 key，如 'OmsCtrl::generateOrder' 或 'OmsCtrl::generateOrder#(OrderParam)'
        Returns:
            首个匹配的 CgNode，或 None（无命中/DB 异常）
        """
        try:
            # 复用 _resolve：split '#' 取 qualified_name → find_nodes_by_qualified_name（重载已处理）
            nodes = self._resolve(entity_id)
            # 取首个；同 qualified_name 的重载处于同一文件/模块，首个即代表性片段
            return nodes[0] if nodes else None
        except sqlite3.Error as e:
            # 库缺失/锁/损坏 → 降级返回 None，与 _walk / module_of 降级风格保持一致
            _LOG.warning("[codegraph] resolve_first 失败，返回 None (entity_id=%s): %s", entity_id, e)
            return None

    def successors_with_locations(self, entity_id: str) -> list[dict]:
        """entity_id 的 callees + 调用点位置：[{entity_id, name, line, col}, ...]。

        不去重（每个调用点一项），供前端在片段里逐个标可点击跳转。sqlite 异常 → []（降级）。

        Args:
            entity_id: 调用方节点的持久 key
        Returns:
            每个调用点的字典列表，字段：entity_id(durable_key)、name、line、col
        """
        try:
            # 解析出节点列表（可能有重载）
            nodes = self._resolve(entity_id)
            if not nodes:                       # entity_id 在图中不存在 → 空列表
                return []
            out: list[dict] = []
            # 只取首个节点的出边（与 resolve_first 选的片段一致，保证调用点落在该片段内）
            for tgt, line, col in self._db.successors_with_locations(nodes[0].id, "calls"):
                # durable_key(tgt)：把目标 CgNode 转为持久身份（qualified_name + 签名），供前端跳转
                out.append({"entity_id": durable_key(tgt), "name": tgt.name, "line": line, "col": col})
            return out
        except sqlite3.Error as e:
            # 库缺失/锁/损坏 → 降级返 []，避免整体崩溃
            _LOG.warning("[codegraph] successors_with_locations 失败，返回 [] (entity_id=%s): %s", entity_id, e)
            return []

    def callers(self, entity_id: str) -> list[dict]:
        """谁调用了 entity_id：[{entity_id, name}, ...]（按 entity_id 去重）。sqlite 异常 → []。

        与 successors_with_locations 不去重不同，callers 以 caller 为粒度去重：
        同一 caller 多次调用 entity_id 只计一次，避免"A 被 B 调用了 3 次"导致 B 出现 3 次。

        Args:
            entity_id: 被调用节点的持久 key
        Returns:
            去重后的调用方字典列表，字段：entity_id(durable_key)、name
        """
        try:
            # 解析出节点列表（可能有重载）
            nodes = self._resolve(entity_id)
            if not nodes:                       # entity_id 在图中不存在 → 空列表
                return []
            out: list[dict] = []
            seen: set[str] = set()              # 按 durable_key 去重：同一 caller 多次调用只记一次
            # predecessors 返回 CgNode 列表（含 .name），逐个转换为 durable_key
            for src in self._db.predecessors(nodes[0].id, "calls"):
                key = durable_key(src)          # 持久身份：qualified_name + 签名
                if key not in seen:             # 去重判断
                    seen.add(key)
                    out.append({"entity_id": key, "name": src.name})
            return out
        except sqlite3.Error as e:
            # 库缺失/锁/损坏 → 降级返 []，不整体崩溃
            _LOG.warning("[codegraph] callers 失败，返回 [] (entity_id=%s): %s", entity_id, e)
            return []

    def resolve_at_position(
        self, file_path: str, line: int, col: int
    ) -> Optional[str]:
        """IDE 化光标位置 → 实体 durable_key（设计 §4.1 ①）；无命中/异常 → None。

        Args:
            file_path: 源文件相对路径（同 nodes.file_path）
            line: 光标行（1-indexed）
            col:  光标列（1-indexed）
        Returns:
            最近一条 calls/references/instantiates 边的 target durable_key；
            无命中或 sqlite 异常返回 None（fail-soft，与 resolve_first 同风格）
        """
        try:
            # db 层已按距离排序，取首个即"光标最近的边"的 target
            hits = self._db.edges_at(file_path, line, col)
            if not hits:                            # 无任何边命中 → 返 None
                return None
            target, _line, _col, _kind = hits[0]    # 元组解包：拿目标节点
            return durable_key(target)              # 转持久身份返回，让上层做后续路由
        except sqlite3.Error as e:
            # 库缺失/锁/损坏 → 静默降级 None，留 warning 便于诊断
            _LOG.warning(
                "[codegraph] resolve_at_position 失败，返回 None (%s:%s:%s): %s",
                file_path, line, col, e,
            )
            return None

    def find_by_name(self, token: str, limit: int = 10) -> list[str]:
        """名字回退查找：FTS 命中节点的 durable_key 列表；CamelCase 优先 class/interface。

        位置解析（resolve_at_position）落空时，用光标处的 token 走全文索引兜底。
        启发式：token 首字母大写 → 用户多半在选类型名 → 优先返回 class/interface 节点。

        Args:
            token: 用户在光标处选中的词
            limit: 最多返回多少个 durable_key
        Returns:
            durable_key 列表（CamelCase 优先 class/interface；其它情况按 FTS 原序）；
            sqlite 异常 → 空列表
        """
        try:
            nodes = self._db.search_nodes_by_name(token, limit)
            # token[:1] 切片取首字符（空串安全，得 ''），isupper() 判 CamelCase
            # 用切片而非索引：token='' 时 token[0] 会 IndexError，token[:1] 得 '' 安全
            if token[:1].isupper():
                # 列表推导式：筛 kind 在偏好集合内的节点
                preferred = [n for n in nodes if n.kind in ("class", "interface")]
                # or 短路：偏好列表非空就用偏好；否则回退原序列（不丢候选）
                nodes = preferred or nodes
            return [durable_key(n) for n in nodes]
        except sqlite3.Error as e:
            _LOG.warning("[codegraph] find_by_name 失败，返回 [] (token=%s): %s", token, e)
            return []

    def resolve_impl(self, entity_id: str) -> Optional[str]:
        """接口方法 → 实现方法 durable_key（设计 §4.1 ③）；非接口方法返 None（不改写）。

        判定逻辑：
          1. 剥 '://' scheme 和 '#' 签名 → 得 qualified_name（如 'ISvc::m'）
          2. 拆 class 部分 → 在 CodeGraph 找该类节点；任一 kind='interface' 才视为接口方法
          3. 调 db.nodes_implementing → 找该接口方法的所有实现，取首个返回
          4. 任何异常 / 非接口 / 无实现 → None（fail-soft）

        Args:
            entity_id: 持久 key（如 'ISvc::m#()' 或带 scheme 的 'method://ISvc::m#()'）
        Returns:
            首个实现方法的 durable_key；非接口方法或无实现返回 None
        """
        try:
            # 剥 scheme：'method://ISvc::m#()' → 'ISvc::m#()'
            without_scheme = entity_id.split("://", 1)[-1]
            # split('#', 1)[0]：'ISvc::m#()' → 'ISvc::m'（'#' 分隔符不会出现在 qn 内部）
            qn = without_scheme.split("#", 1)[0]
            # 必须含 '::' 才是 method qn；否则可能是类名等，直接返 None
            if "::" not in qn:
                return None
            # rpartition 从右切首个 '::'，[0]=class 部分（如 'ISvc'）
            class_part = qn.rpartition("::")[0]
            # 查 class 节点：必须存在且至少一个 kind='interface'，才认为该方法是接口方法
            class_nodes = self._db.find_nodes_by_qualified_name(class_part)
            # any(...)：任一节点是接口即可（防御重名 / 重定义）；空列表也走 False 分支
            if not any(n.kind == "interface" for n in class_nodes):
                return None
            # 走 db 层 4 表 JOIN → 实现方法 node 列表
            impls = self._db.nodes_implementing(qn)
            if not impls:                            # 接口无实现（罕见，但 schema 允许）
                return None
            # 取首个实现（多实现取首个；调用方若需全部可后续扩 list 接口）
            return durable_key(impls[0])
        except sqlite3.Error as e:
            _LOG.warning("[codegraph] resolve_impl 失败 (%s): %s", entity_id, e)
            return None

    def module_of(self, entity_id: str) -> Optional[str]:
        """返回 entity 所属模块（CodeGraph file_path 顶层目录，如 'mall-portal'/'mall-admin'）。

        查不到节点 / sqlite 异常 → None（best-effort，同 _walk 的降级契约，绝不抛）。
        设计 [[模块标签-设计]] §3。
        """
        try:
            # 复用 _resolve：split '#' 取 qualified_name → find_nodes_by_qualified_name（重载多命中已处理）
            nodes = self._resolve(entity_id)
            if not nodes:                              # 无命中（qn 不在 CodeGraph）
                return None
            # 同一 qualified_name 的重载都在同一文件/同一模块，取首个即可
            fp = nodes[0].file_path or ""
            # 顶层目录即模块名：'mall-portal/src/...' → 'mall-portal'；无 '/' 时退化为整串或 None
            return fp.split("/", 1)[0] if "/" in fp else (fp or None)
        except sqlite3.Error as e:
            # 库缺失/锁/损坏 → 降级返回 None，不整体崩（对齐图导航降级风格）
            _LOG.warning("[codegraph] module_of 查询失败，返回 None (entity_id=%s): %s", entity_id, e)
            return None
