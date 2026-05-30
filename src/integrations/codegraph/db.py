# src/integrations/codegraph/db.py
"""只读访问 CodeGraph 生成的 .codegraph.db。

CodeGraph 把代码结构索引进单文件 SQLite；KE 以 mode=ro 只读查它，
拿稳定身份(qualified_name)与精确导航(successors/predecessors)。KE 永不写库。
"""
from __future__ import annotations  # PEP 563：允许在类型注解里提前引用未定义的类名

import sqlite3                       # 标准库 SQLite
from dataclasses import dataclass    # 用 dataclass 做轻量数据载体
from typing import Optional          # Optional[X] 等价于 Union[X, None]，表示可为 None

# 一次性 SELECT 的列清单，避免到处重复
_COLS = "id,kind,name,qualified_name,file_path,start_line,end_line,signature"


@dataclass(frozen=True)              # frozen=True → 不可变，安全当字典键/传递
class CgNode:
    """CodeGraph 节点的精简投影（只取 KE 用得到的列）。"""
    id: str                          # 节点唯一 ID（CodeGraph 内部生成）
    kind: str                        # 节点类型，如 'method'/'class'
    name: str                        # 短名，如 'generateOrder'
    qualified_name: str              # 全限定名，如 'OmsService::generateOrder'
    file_path: str                   # 相对源文件路径
    start_line: int                  # 定义起始行
    end_line: int                    # 定义结束行
    signature: Optional[str]         # 方法签名，可为 None


class CodeGraphDB:
    """只读 SQLite 访问层。

    职责：把 .codegraph.db 的 nodes/edges 表暴露为 Python 对象，
    不包含业务逻辑，不写入任何数据。
    """

    def __init__(self, db_path: str) -> None:
        """初始化，记录路径，不立即打开连接（用时再开，查完关）。"""
        self._db_path = db_path
        # file: URI + mode=ro：只读打开，KE 误写也写不进去，杜绝弄坏 CodeGraph 索引
        self._uri = f"file:{db_path}?mode=ro"

    def _connect(self) -> sqlite3.Connection:
        """建立只读连接并配置按列名取值。"""
        # uri=True 让 sqlite3 把字符串当 URI 解析（才认 mode=ro）
        conn = sqlite3.connect(self._uri, uri=True)
        conn.row_factory = sqlite3.Row   # 查询结果可按列名取值，如 row["name"]
        return conn

    def find_nodes_by_qualified_name(self, qualified_name: str) -> list[CgNode]:
        """按 qualified_name 精确匹配，返回所有命中节点（重载方法可能多行）。

        Args:
            qualified_name: 全限定名，如 'OmsService::generateOrder'
        Returns:
            匹配的 CgNode 列表，无匹配返回空列表
        """
        # with 语句（上下文管理器）：自动在块结束时关闭连接，等效 try/finally close()
        with self._connect() as conn:
            rows = conn.execute(
                f"SELECT {_COLS} FROM nodes WHERE qualified_name = ?",
                (qualified_name,),   # 用元组传参，? 占位符防止 SQL 注入
            ).fetchall()
        # 列表推导式：把每一行 sqlite3.Row 转换为 CgNode 数据类实例
        return [self._row_to_node(r) for r in rows]

    def successors(self, node_id: str, kind: str = "calls") -> list[CgNode]:
        """出边目标(callees)：找所有 edges.source = node_id 且 kind 匹配的目标节点。

        Args:
            node_id: 起点节点 ID
            kind:    边类型，默认 'calls'（调用边）
        Returns:
            被调用节点的 CgNode 列表
        """
        # JOIN：把 edges 表中的 target 列关联到 nodes.id，拿到完整节点信息
        with self._connect() as conn:
            rows = conn.execute(
                f"SELECT {self._prefixed('t')} FROM edges e "
                "JOIN nodes t ON e.target = t.id WHERE e.source = ? AND e.kind = ?",
                (node_id, kind),
            ).fetchall()
        return [self._row_to_node(r) for r in rows]

    def predecessors(self, node_id: str, kind: str = "calls") -> list[CgNode]:
        """入边来源(callers)：找所有 edges.target = node_id 且 kind 匹配的来源节点。

        Args:
            node_id: 终点节点 ID
            kind:    边类型，默认 'calls'
        Returns:
            调用方节点的 CgNode 列表
        """
        # 与 successors 对称：这次 JOIN 的是 source 列（来源），过滤条件是 target
        with self._connect() as conn:
            rows = conn.execute(
                f"SELECT {self._prefixed('s')} FROM edges e "
                "JOIN nodes s ON e.source = s.id WHERE e.target = ? AND e.kind = ?",
                (node_id, kind),
            ).fetchall()
        return [self._row_to_node(r) for r in rows]

    def get_node(self, node_id: str) -> Optional[CgNode]:
        """按主键 ID 精确取一个节点；不存在返回 None。

        Args:
            node_id: 节点 ID
        Returns:
            CgNode 或 None
        """
        with self._connect() as conn:
            row = conn.execute(
                f"SELECT {_COLS} FROM nodes WHERE id = ?", (node_id,)
            ).fetchone()   # fetchone：只取第一行；没有匹配返回 None
        return self._row_to_node(row) if row else None  # 条件表达式（三元）

    def iter_method_nodes(self) -> list[CgNode]:
        """列出所有 kind='method' 的节点（重灌 CodeEntity 用）。

        一次性把全部方法节点读进内存；mall-swarm ~1.5 万条，可接受。
        Returns:
            method 节点 CgNode 列表
        """
        # 一次性查全部方法节点；mall-swarm ~1.5万，全量读进内存可接受
        with self._connect() as conn:
            # WHERE kind = 'method'：只取方法节点，过滤掉类/接口/字段等
            rows = conn.execute(
                f"SELECT {_COLS} FROM nodes WHERE kind = 'method'"
            ).fetchall()   # fetchall：把结果集全部取回成列表
        # 列表推导式：把每一行 sqlite3.Row 转换为 CgNode 数据类实例
        return [self._row_to_node(r) for r in rows]

    @staticmethod
    def _prefixed(alias: str) -> str:
        """把 _COLS 每列加表别名前缀，如 't.id,t.kind,...'，用于 JOIN 查询避免列名冲突。

        @staticmethod：不依赖实例(self)也不依赖类(cls)，纯工具函数，挂在类里方便命名空间管理。
        """
        # str.join(iterable)：用指定分隔符拼接可迭代对象的元素
        return ",".join(f"{alias}.{c}" for c in _COLS.split(","))

    @staticmethod
    def _row_to_node(r: sqlite3.Row) -> CgNode:
        """把 sqlite3.Row 对象转换为 CgNode 数据类实例。

        sqlite3.Row 可按列名索引（r["name"]），这里逐一映射到 CgNode 的字段。
        """
        return CgNode(
            id=r["id"],
            kind=r["kind"],
            name=r["name"],
            qualified_name=r["qualified_name"],
            file_path=r["file_path"],
            start_line=r["start_line"],
            end_line=r["end_line"],
            signature=r["signature"],   # 可为 None（数据库中该列允许空）
        )
