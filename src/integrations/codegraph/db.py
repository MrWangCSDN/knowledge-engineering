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

    def successors_with_locations(
        self, node_id: str, kind: str = "calls"
    ) -> list[tuple[CgNode, Optional[int], Optional[int]]]:
        """出边目标(callees) + 每次调用的调用点位置 (line, col)。

        与 successors 的区别：① 多带 edges.line/col；② **不去重**——同一目标被调用多次
        会返回多行（每个调用点一行），供前端在代码片段里逐个标成可点击跳转。

        Args:
            node_id: 起点（caller）节点 ID
            kind:    边类型，默认 'calls'
        Returns:
            [(callee CgNode, line, col), ...]，按 (line, col) 升序。
            line：真实库中调用点行号几乎必填，但 edges 列无 NOT NULL 约束，理论可为 None。
            col：真实库约 10% 为 NULL（解析未定位到列）→ None；ORDER BY 下 SQLite 把
            col=NULL 的行排在同一 line 内最前（NULL < 任何值）。
        """
        # with 语句（上下文管理器）：块结束自动关闭连接，等效 try/finally close()
        with self._connect() as conn:
            # SELECT t.* 投影目标节点列（_row_to_node 按列名读）+ edges 的 line/col
            # e.line AS edge_line：起别名 edge_line，避免与 nodes 表中同名列冲突
            # e.col  AS edge_col ：同理
            # ORDER BY e.line, e.col：按调用点位置升序，前端展示时从上到下排列
            rows = conn.execute(
                f"SELECT {self._prefixed('t')}, e.line AS edge_line, e.col AS edge_col "
                "FROM edges e JOIN nodes t ON e.target = t.id "
                "WHERE e.source = ? AND e.kind = ? "
                "ORDER BY e.line, e.col",
                (node_id, kind),  # 参数化查询，? 占位符防止 SQL 注入
            ).fetchall()   # fetchall：一次取回所有结果行（list of sqlite3.Row）
        # 列表推导式：把每一行转换为 (CgNode, line, col) 三元组
        # r["edge_line"] / r["edge_col"]：按别名取 edges 的位置列（不与 nodes 列冲突）
        return [(self._row_to_node(r), r["edge_line"], r["edge_col"]) for r in rows]

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

    def edges_at(
        self, file_path: str, line: int, col: int
    ) -> list[tuple[CgNode, Optional[int], Optional[int], str]]:
        """位置→边命中：source 节点在 file_path 内的 calls/references/instantiates 边，
        按 (|edge_line - target_line|, |edge_col - target_col|) 字典序排序，取最近若干条。

        用于「IDE 化光标位置 → 实体」解析（设计 [[代码查看器-IDE化导航-设计]] §4.1 ①）。
        kind 集合刻意只含 'calls'/'references'/'instantiates'：覆盖方法调用 + 类型引用 +
        实例化，避免 contains/imports 干扰光标命中。

        排序按 (line_dist, col_dist) 字典序：line 距离优先（同行所有 col 都优于跨行最近 col），
        见 _walk 风格的 ORDER BY 套路。SQLite 中 NULL 在 ORDER BY 升序时排最前，所以
        coalesce(e.line, 0) 给 NULL 一个稳定基线，避免无意义的"NULL 优先"假命中。

        Args:
            file_path: 源 node 所在文件相对路径（同 nodes.file_path）
            line: 鼠标所在行（1-indexed）
            col: 鼠标所在列（1-indexed；UTF-16 列约定与 Monaco 一致）
        Returns:
            [(target CgNode, edge.line, edge.col, edge.kind), ...]，按距离升序，最多 8 条。
            file_path 不存在/无可命中边时返回空列表。
        """
        # with 语句：上下文管理器自动关闭 conn，等效 try/finally close()
        with self._connect() as conn:
            # 三表 JOIN：edges 起点 nodes（用于 file_path 过滤）+ edges 终点 nodes（投影目标列）
            # edge_line/edge_col/edge_kind 起别名，避免与 nodes 表中潜在同名列冲突
            # coalesce(x, 0)：x 为 NULL 时取 0，给距离排序一个稳定基线
            # ORDER BY 两列：字典序自动处理"同行优先"——line 距离为 0 的行全部排在前
            rows = conn.execute(
                f"SELECT {self._prefixed('t')}, "
                "e.line AS edge_line, e.col AS edge_col, e.kind AS edge_kind "
                "FROM edges e "
                "JOIN nodes s ON e.source = s.id "
                "JOIN nodes t ON e.target = t.id "
                "WHERE s.file_path = ? AND e.kind IN ('calls','references','instantiates') "
                "ORDER BY abs(coalesce(e.line, 0) - ?), abs(coalesce(e.col, 0) - ?) "
                "LIMIT 8",
                (file_path, line, col),
            ).fetchall()
        # 列表推导式：每行转 (CgNode, edge_line, edge_col, edge_kind) 四元组
        return [
            (self._row_to_node(r), r["edge_line"], r["edge_col"], r["edge_kind"])
            for r in rows
        ]

    def search_nodes_by_name(self, token: str, limit: int = 10) -> list[CgNode]:
        """nodes_fts 全文索引按名字模糊匹配（位置解析落空时按 token 回退查找）。

        Args:
            token: 用户在光标处选中的词（如 'OmsService'、'Foo'）
            limit: 最多返回多少个候选（防 FTS 命中爆炸）
        Returns:
            匹配的 CgNode 列表（顺序由 FTS5 内部排序，对调用方而言只取若干候选）
        """
        # 不传 token 或空串：直接返回空列表，避免 FTS5 MATCH '""' 行为异常
        if not token:
            return []
        # FTS5 短语转义：用双引号包裹 token，并把 token 内的双引号自身改成 ""
        # 避免用户输入含 '/' '"' '.' 等特殊字符时 FTS5 解析失败抛 OperationalError
        safe = '"' + token.replace('"', '""') + '"'
        with self._connect() as conn:
            # 子查询 nodes_fts MATCH ? → 命中的 id 集合；外层按 _COLS 投影完整节点列
            # LIMIT 在子查询/外层均可，放外层简单清晰
            rows = conn.execute(
                f"SELECT {_COLS} FROM nodes "
                "WHERE id IN (SELECT id FROM nodes_fts WHERE nodes_fts MATCH ?) "
                "LIMIT ?",
                (safe, limit),
            ).fetchall()
        return [self._row_to_node(r) for r in rows]

    def nodes_implementing(self, iface_qn: str) -> list[CgNode]:
        """接口方法 qn → 经 implements 边找实现类，再取同名方法 node。

        SQL 一次跑完 4 表 JOIN：
          1. iface 表（接口类节点，按 qualified_name 过滤）
          2. implements 边（target = iface.id → source = impl_class.id）
          3. contains 边（impl_class → 其下所有方法）
          4. m 表（contains.target → 同名方法节点）

        Args:
            iface_qn: 接口方法的 qualified_name，如 'ISvc::m'（已剥 '#' 签名后缀）
        Returns:
            实现该接口方法的方法节点列表；接口不存在/无实现/qn 格式异常 → 空列表
        """
        # rpartition：从右往左切第一个 '::'，[0]=class_part、[2]=method_name
        # 若 iface_qn 不含 '::'，rpartition 返回 ('', '', original) → class_part 为 ''，方法名落空
        # 这种异常输入下，下面的 WHERE qualified_name='' 自然无命中 → 返回 [] 兜底
        class_part, sep, method_name = iface_qn.rpartition("::")
        # 不含 '::' 或方法名空 → 直接返回 []，省一次 SQL 调用
        if not sep or not method_name:
            return []
        with self._connect() as conn:
            # 4 表 JOIN：
            #   iface 表 → implements 边 → impl 类的 contains 边 → 实现方法节点 m
            # 投影 m.* 的列（用 _prefixed('m') 复用列清单），供 _row_to_node 解析
            rows = conn.execute(
                f"SELECT {self._prefixed('m')} "
                "FROM nodes iface "
                "JOIN edges impl_edge "
                "  ON impl_edge.target = iface.id AND impl_edge.kind = 'implements' "
                "JOIN edges contains_edge "
                "  ON contains_edge.source = impl_edge.source "
                "  AND contains_edge.kind = 'contains' "
                "JOIN nodes m "
                "  ON m.id = contains_edge.target "
                "  AND m.kind = 'method' "
                "  AND m.name = ? "
                "WHERE iface.qualified_name = ?",
                (method_name, class_part),
            ).fetchall()
        return [self._row_to_node(r) for r in rows]

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
