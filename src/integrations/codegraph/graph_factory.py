# src/integrations/codegraph/graph_factory.py
"""根据 repo_local_path 解析图导航适配器；缺 CodeGraph 索引时优雅降级为 NullGraphAdapter。

设计 [[CodeGraph-结构引擎集成-设计]] §8 错误处理&降级：
工程没配 repo_local_path、或 .codegraph/codegraph.db 还没建好时，
QA 不应整体报错——图导航降级为空（callees/callers 返 []），语义检索（Weaviate）照常工作。
"""
from __future__ import annotations  # PEP 563：注解延迟求值，允许提前引用类型名

import logging   # 降级时打 warning，留可观测痕迹
import os        # os.path.exists 判断索引文件在不在
from typing import Optional  # Optional[str] = str | None

from src.integrations.codegraph.db import CodeGraphDB, CgNode  # CgNode：resolve_first 返回类型注解用
from src.integrations.codegraph.graph_adapter import CodeGraphGraphAdapter
from src.integrations.codegraph.paths import codegraph_db_path

_LOG = logging.getLogger(__name__)


class NullGraphAdapter:
    """GraphProto 的空实现：无 CodeGraph 索引时占位，图导航一律返回 []。

    实现 successors / predecessors 两个方法即满足 GraphProto 协议（duck typing）。
    语义检索（CodeEntity 向量库）不经过图，故仍可用——只是少了调用链上下文。
    """

    def successors(self, entity_id: str, rel_type: Optional[str] = None) -> list[str]:
        """出边（callees）：无索引 → 空列表。"""
        return []

    def predecessors(self, entity_id: str, rel_type: Optional[str] = None) -> list[str]:
        """入边（callers）：无索引 → 空列表。"""
        return []

    def module_of(self, entity_id: str) -> Optional[str]:
        """无 CodeGraph 索引 → 无模块信息（降级返 None，不报错）。"""
        return None

    def resolve_first(self, entity_id: str) -> Optional[CgNode]:
        """无 CodeGraph 索引 → 解析不出节点，返回 None。"""
        return None

    def successors_with_locations(self, entity_id: str) -> list[dict]:
        """无 CodeGraph 索引 → 无调用点，返回空列表。"""
        return []

    def callers(self, entity_id: str) -> list[dict]:
        """无 CodeGraph 索引 → 无调用者，返回空列表。"""
        return []

    # ── IDE 化符号解析三原语（设计 [[代码查看器-IDE化导航-设计]] §4.1）──
    # 无 CodeGraph 索引时，三原语均降级返 None/空，让上层 resolve_symbol_at 返回 null，
    # 前端据此显示"暂无源码"，保持与原 successors/callers 同款降级契约。
    def resolve_at_position(
        self, file_path: str, line: int, col: int
    ) -> Optional[str]:
        """无 CodeGraph 索引 → 位置解析无目标，返 None。"""
        return None

    def find_by_name(self, token: str, limit: int = 10) -> list[str]:
        """无 CodeGraph 索引 → 名字回退无候选，返空列表。"""
        return []

    def resolve_impl(self, entity_id: str) -> Optional[str]:
        """无 CodeGraph 索引 → 接口→impl 解析无结果，返 None。"""
        return None


def resolve_graph_adapter(repo_local_path: Optional[str]):
    """按 repo_local_path 返回合适的图适配器（GraphProto）。

    - repo_local_path 为空（None/""）→ NullGraphAdapter（工程没配本地路径，常见于 stub 工程）
    - 路径有了但 .codegraph/codegraph.db 不存在 → NullGraphAdapter（索引还没建/没传到本机）
    - 索引文件存在 → 真正的 CodeGraphGraphAdapter（懒打开，查询时才连 SQLite）

    这样部署到尚未建索引的环境时，QA 走"语义检索 + 空图导航"的降级路径而非报错。

    Args:
        repo_local_path: 工程在本机的仓库根路径（来自 DB Project.repo_local_path），可能为 None
    Returns:
        实现 GraphProto 的适配器实例（CodeGraphGraphAdapter 或 NullGraphAdapter）
    """
    # 1) 没配 repo_local_path → 降级
    if not repo_local_path:
        _LOG.warning("repo_local_path 为空 → 图导航降级（NullGraphAdapter）；语义检索不受影响")
        return NullGraphAdapter()

    # 2) 算出 .codegraph.db 路径（repo_local_path 非空，这里不会抛 ValueError）
    db_path = codegraph_db_path(repo_local_path)

    # 3) 索引文件不存在 → 降级（部署到未建索引的服务器时走这条）
    if not os.path.exists(db_path):
        _LOG.warning("CodeGraph 索引不存在 %s → 图导航降级（NullGraphAdapter）", db_path)
        return NullGraphAdapter()

    # 4) 一切就绪 → 真正的图适配器
    return CodeGraphGraphAdapter(CodeGraphDB(db_path))
