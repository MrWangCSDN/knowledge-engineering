"""权限解析 + FastAPI dependency 工厂。

v2.0 起强制：
- 每个 /projects/{pid}/* 路由用 Depends(require_project_role("..."))
- 每个 /groups/{gid}/* 路由用 Depends(require_group_role("..."))
- /admin/* 用 Depends(require_admin)（已存在）

核心设计：
  用户在某个工程的最终 role = max(
      Instance Admin override（is_admin=True → owner）,
      工程直接成员 role（user_project_access 表），
      Group 继承链中的最高 role（向上遍历 parent_group_id）
  )

Python 语法提示：
  - from __future__ import annotations：让函数签名里的类型注解全部变成"字符串"，
    延迟求值（在 Python 3.9 之前的版本中，类型注解里用到还未定义的类时会报 NameError）
  - Optional[str]：等价于 Union[str, None]，表示可能是字符串也可能是 None
  - async def：定义协程函数，必须用 await 调用；不能在普通函数里直接 await

依赖：
  pip install fastapi sqlalchemy aiosqlite
"""

# from __future__ import annotations：启用"延迟注解求值"，让类型注解支持前向引用
# 比如函数里用到 Optional[str] 不需要在模块顶部先定义 str 类型（str 是内置类型，不需要；但对自定义类有用）
from __future__ import annotations

# Optional 是 typing 模块提供的泛型工具
# Optional[X] 等价于 Union[X, None]，常用于"可能没有值"的场景
from typing import Optional

# FastAPI 的核心依赖注入工具和异常类
# Depends(fn)：把 fn 声明为路由函数的依赖项，FastAPI 会自动调用它并注入结果
# HTTPException：用于向客户端返回带 HTTP 状态码的错误响应
from fastapi import Depends, HTTPException

# SQLAlchemy 2.0 的 select 函数：构造 SELECT 查询语句
# 用法：select(Model.column).filter_by(field=value) 生成 SQL 对象，再 await db.scalar(...) 执行
from sqlalchemy import select

# AsyncSession：异步数据库会话类型（仅用于类型注解）
from sqlalchemy.ext.asyncio import AsyncSession

# 导入项目内模块（已在 Task 1+2 实现）
from src.service.auth_dependencies import get_current_user   # FastAPI dependency：从 JWT 解析当前用户
from src.service.auth_models import User                     # 用户 ORM 模型（users 表）
from src.service.db import get_db                            # FastAPI dependency：提供 DB session
from src.service.db_models_groups import Group, GroupMember  # 分组 / 成员 ORM 模型
from src.service.db_models_homepage import Project, UserProjectAccess  # 工程 / 直接成员 ORM 模型


# ─── Role 等级字典 ──────────────────────────────────────────────────────────

# ROLE_RANK 是一个 dict（字典），键是 role 名称（str），值是整数等级
# 字典字面量语法：{key: value, ...}；整数越大表示权限越高
# 用途：比较两个 role 的高低（无需一串 if-elif）
ROLE_RANK: dict[str, int] = {
    "reporter": 1,    # 只读角色（可以查看，不能修改）
    "maintainer": 2,  # 读写角色（可以操作，不能管理成员）
    "owner": 3,       # 最高角色（完全控制，包括管理成员）
}


# ─── 工具函数 ────────────────────────────────────────────────────────────────

def _pick_higher(a: Optional[str], b: Optional[str]) -> Optional[str]:
    """取两个 role 中权限较高者；None 表示"无 role"。

    Args:
        a: 第一个 role（可以是 None）
        b: 第二个 role（可以是 None）

    Returns:
        权限更高的 role；若两者都是 None，返回 None

    示例：
        _pick_higher('reporter', 'owner') → 'owner'
        _pick_higher(None, 'maintainer') → 'maintainer'
        _pick_higher(None, None)         → None

    Python 提示：
        函数名以下划线 _ 开头是 Python 约定，表示"模块内部私有函数"（module-level private）
        外部代码不应该直接调用，但 Python 语言层面并不强制（没有访问控制关键字）
    """
    # if a is None：用 is 而不是 == 来判断 None
    # 原因：None 在 Python 里是单例（只有一个 None 对象），is 是身份检查（更快更准确）
    # == 是相等性检查，可能被重载；is None 是惯用写法
    if a is None:
        return b  # a 没有 role，直接返回 b（即使 b 也是 None）
    if b is None:
        return a  # b 没有 role，直接返回 a
    # 两者都有 role：比较 ROLE_RANK 字典中的等级，返回等级 >= b 的一方
    # ROLE_RANK[a] 是字典取值操作（O(1) 平均时间复杂度）
    return a if ROLE_RANK[a] >= ROLE_RANK[b] else b


# ─── 核心权限解析函数 ────────────────────────────────────────────────────────

async def resolve_role(
    user: User,
    project: Project,
    db: AsyncSession,
) -> Optional[str]:
    """计算 user 在 project 的最终 role。

    取 max(Instance Admin, Project 直接成员, Group 继承链所有 role)。

    Args:
        user:    当前用户 ORM 对象
        project: 目标工程 ORM 对象
        db:      异步数据库 session

    Returns:
        最终 role 字符串（'reporter' / 'maintainer' / 'owner'），
        或 None（用户无任何权限）

    Python / SQLAlchemy 语法提示：
        async def：定义异步函数（协程），调用时需要 await
        await db.scalar(query)：
            - db.scalar 执行 SQL 查询并返回"第一行第一列"的值（标量）
            - 等价于：result = await db.execute(query); return result.scalar()
            - 如果没有匹配行，返回 None
        select(Model.column).filter_by(field=value)：
            - 构造 "SELECT column FROM table WHERE field=value" 的 SQL
            - filter_by 是 SQLAlchemy 的关键字参数过滤，等价于 .where(Model.field == value)
    """
    # Instance Admin（实例级管理员）无条件是 owner
    # is_admin 是 User ORM 对象上的 Python 属性，从数据库读取后映射到这里
    if user.is_admin:
        return "owner"  # 直接返回，不需要查数据库

    # ── 1. Project 直接成员角色（user_project_access 表） ──────────────────
    # await db.scalar(select(...))：异步执行 SELECT，返回单个标量值
    # select(UserProjectAccess.role)：只查 role 列，不查整行（更高效）
    # .filter_by(user_id=user.id, project_id=project.id)：
    #   等价于 WHERE user_id=? AND project_id=?（位置参数由 SQLAlchemy 绑定，防 SQL 注入）
    direct: Optional[str] = await db.scalar(
        select(UserProjectAccess.role).filter_by(
            user_id=user.id,        # 当前用户的整数 id
            project_id=project.id,  # 目标工程的字符串 id
        )
    )

    # ── 2. Group 继承链：向上遍历 parent_group_id，取最高 role ────────────
    # 算法：从 project.group_id 开始，顺着 parent_group_id 向上走，收集沿途的 role
    # 防护 1：visited set 防止循环引用（虽然 DB 层有 RESTRICT 约束，但程序层也要安全）
    # 防护 2：depth 计数器限制最多走 3 层（spec 要求：直接 group + 父 + 爷，第 4 层截断）

    inherited: Optional[str] = None  # 从 group 继承来的最高 role（初始为 None）
    cur_gid: Optional[str] = project.group_id  # 当前遍历到的 group id，从 project 的 group 开始
    visited: set[str] = set()  # 已访问过的 group id 集合（set 的 in 操作是 O(1)）
    depth: int = 0  # 当前层深度：0=直接 group，1=父 group，2=爷 group，≥3 截断

    # while 循环：只要还有 group 没遍历，且没有循环引用，且未超出深度限制就继续
    # cur_gid not in visited：防止循环（防御性编程）
    while cur_gid and cur_gid not in visited:
        # spec 要求 group 嵌套上限 3 层（depth 0/1/2），第 4 层（depth >= 3）防御性截断
        if depth >= 3:
            break

        visited.add(cur_gid)  # 把当前 group 标记为已访问

        # 查当前 group 是否有该用户的成员记录（GroupMember 的复合主键是 (user_id, group_id)）
        role: Optional[str] = await db.scalar(
            select(GroupMember.role).filter_by(user_id=user.id, group_id=cur_gid)
        )

        # 如果有 role，用 _pick_higher 取当前已知最高 vs 刚查到的，保留更高的
        if role:
            inherited = _pick_higher(inherited, role)  # 取继承链中最高的 role

        # 向上走一层：查当前 group 的父 group id
        # 如果是根 group（parent_group_id IS NULL），db.scalar 返回 None，循环自然结束
        cur_gid = await db.scalar(
            select(Group.parent_group_id).filter_by(id=cur_gid)
        )
        depth += 1  # 计完这一层再 +1，进入下一轮循环时检查

    # ── 3. 取 max(直接成员 role, 继承 role) ───────────────────────────────
    # _pick_higher 处理 None 的情况，不需要额外的 if None 检查
    return _pick_higher(direct, inherited)


async def resolve_group_role(
    user_id: int,
    group_id: str,
    db: AsyncSession,
) -> Optional[str]:
    """计算 user 在 group 的最终 role（Group 继承链，不含 Instance Admin override）。

    Args:
        user_id:  用户整数 id
        group_id: 目标 group 的字符串 id
        db:       异步数据库 session

    Returns:
        最终 role，或 None

    注意：
        这里不处理 is_admin 覆盖，is_admin 的处理在 require_group_role dependency 里。
        这样设计是为了让这个函数专注于"纯 group role 查询"，可复用于其他场景。
    """
    # 初始化：从指定 group 开始向上遍历继承链
    inherited: Optional[str] = None  # 目前为止的最高 role
    cur_gid: Optional[str] = group_id  # 当前遍历的 group id
    visited: set[str] = set()  # 防循环

    # 遍历逻辑与 resolve_role 里的 Group 继承段完全相同
    while cur_gid and cur_gid not in visited:
        visited.add(cur_gid)

        # 查用户在当前 group 的成员 role
        role: Optional[str] = await db.scalar(
            select(GroupMember.role).filter_by(user_id=user_id, group_id=cur_gid)
        )

        # 有 role 就和已知最高比较，保留较高者
        if role:
            inherited = _pick_higher(inherited, role)

        # 向上走一层
        cur_gid = await db.scalar(
            select(Group.parent_group_id).filter_by(id=cur_gid)
        )

    return inherited  # 返回继承链中最高的 role（None 表示无权限）


async def list_accessible_projects(
    user: User,
    db: AsyncSession,
) -> list[Project]:
    """列出 user 能访问的所有工程。

    规则：
      - Instance Admin（is_admin=True）：可见所有工程
      - 普通用户：可见 = 直接成员工程（user_project_access） + group 继承工程

    Args:
        user: 当前用户 ORM 对象
        db:   异步数据库 session

    Returns:
        Project 对象列表（可能为空列表）

    Python 语法提示：
        list[Project]：Python 3.9+ 支持直接用内置 list 做类型注解（不需要 from typing import List）
        (await db.scalars(select(Project))).all()：
            - db.scalars 执行查询，返回 ScalarResult（游标）
            - .all() 把游标里所有行一次取出来，返回 list
    """
    # Instance Admin 可以看到所有工程
    if user.is_admin:
        # db.scalars(select(Project))：SELECT * FROM projects，返回 Project 对象的游标
        # .all()：把游标里所有行一次取出，返回 list
        # list(...)：把 .all() 结果（通常是 Sequence）转换为标准 list
        return list((await db.scalars(select(Project))).all())

    # ── 普通用户：直接成员工程 ────────────────────────────────────────────
    # 查 user_project_access 表，找用户直接成员的所有 project_id
    # .all() 返回一个 project_id 字符串的列表，set() 去重（理论上不重复，但防御性编程）
    direct_pids: set[str] = set(
        (await db.scalars(
            select(UserProjectAccess.project_id).filter_by(user_id=user.id)
        )).all()
    )

    # ── Group 继承工程 ─────────────────────────────────────────────────────
    # _expand_user_groups：找用户直接成员 + 子孙所有 group 的 id 集合（BFS 向下）
    # 注意：这里是向下扩展（找所有子孙 group），与 resolve_role 里向上走父链不同
    accessible_groups: set[str] = await _expand_user_groups(user.id, db)

    # 合并：从直接成员 project_id 开始
    pids: set[str] = set(direct_pids)

    # 如果用户在某些 group 里，查这些 group 下的所有工程
    if accessible_groups:
        # Project.group_id.in_(accessible_groups)：生成 WHERE group_id IN (...) SQL
        # .in_() 是 SQLAlchemy 的集合过滤方法，等价于 SQL IN 操作符
        group_pids = (await db.scalars(
            select(Project.id).filter(Project.group_id.in_(accessible_groups))
        )).all()
        # update() 把 group_pids 里所有 id 加入 pids 集合（set 的并集操作）
        pids.update(group_pids)

    # 如果没有任何可访问工程，返回空列表
    if not pids:
        return []  # 空列表，不需要查数据库

    # 批量查询所有可访问工程（一次 SELECT ... WHERE id IN (...) 比多次 SELECT 高效）
    return list(
        (await db.scalars(
            select(Project).filter(Project.id.in_(pids))  # WHERE id IN (...)
        )).all()
    )


async def _expand_user_groups(user_id: int, db: AsyncSession) -> set[str]:
    """找用户直接成员的 group + 所有子孙 group（深度上限 3，BFS 算法）。

    Args:
        user_id: 用户整数 id
        db:      异步数据库 session

    Returns:
        可访问的所有 group id 集合（包含直接成员 group + 子孙 group）

    设计：
        BFS（广度优先搜索）向下扩展：
          - 第 0 层：用户直接成员的 group
          - 第 1 层：这些 group 的直接子 group
          - 第 2 层：第 1 层 group 的子 group
          - 第 3 层：第 2 层 group 的子 group（MAX_GROUP_DEPTH=3，不再继续）

    Python 语法提示：
        set（集合）：无序、不重复的元素集合
          - .add(x)：添加单个元素
          - .update(iterable)：批量添加
          - set1 - set2（或 difference）：集合差集，找"在 set1 但不在 set2"的元素
        for _ in range(n)：下划线 _ 是约定的"不关心的变量名"，表示循环变量本身不用
    """
    # 先查用户直接成员的 group id 集合（第 0 层）
    direct: set[str] = set(
        (await db.scalars(
            select(GroupMember.group_id).filter_by(user_id=user_id)
        )).all()
    )

    # 如果用户不在任何 group，直接返回空集合
    if not direct:
        return set()  # 空集合

    all_accessible: set[str] = set(direct)  # 所有已知可访问 group（初始为直接成员 group）
    frontier: set[str] = set(direct)         # BFS 当前层（初始为直接成员 group）

    # BFS：最多向下扩展 3 层（MAX_GROUP_DEPTH）
    # range(3) 生成 [0, 1, 2]，循环 3 次
    # for _ in range(3)：_ 表示循环次数不重要，重要的是循环 3 次
    for _ in range(3):  # MAX_GROUP_DEPTH = 3
        # 查所有父 group 在 frontier 集合里的子 group
        # Group.parent_group_id.in_(frontier)：找 parent_group_id 在 frontier 里的所有 group
        children: set[str] = set(
            (await db.scalars(
                select(Group.id).filter(Group.parent_group_id.in_(frontier))  # WHERE parent IN (...)
            )).all()
        )

        # new = children - all_accessible：只取"尚未访问过的新子 group"（集合差集，防重复）
        # 如果没有新的子 group，BFS 结束（frontier 为空，继续也无意义）
        new: set[str] = children - all_accessible
        if not new:
            break  # break 立即退出 for 循环

        all_accessible.update(new)  # 把新子 group 加入可访问集合
        frontier = new              # 下一层 BFS 从新发现的子 group 开始

    return all_accessible  # 返回所有可访问 group id 的集合


# ─── FastAPI dependency 工厂 ─────────────────────────────────────────────────

def require_project_role(min_role: str = "reporter"):
    """权限 dependency 工厂：验证当前用户在 path 里 project_id 对应工程的 role ≥ min_role。

    "工厂函数"（Factory function）是一种设计模式：
      - 外层函数（require_project_role）接收配置参数（min_role）
      - 返回一个闭包（checker）作为实际的 FastAPI dependency
      - 闭包通过"捕获" min_role 变量来记住配置（Python 闭包特性）

    用法：
        @router.post("/projects/{project_id}/files",
                     dependencies=[Depends(require_project_role("maintainer"))])
        async def upload_file(...):
            ...

    Args:
        min_role: 最低要求 role，默认 'reporter'（只读）

    Returns:
        一个 async 函数（checker），作为 FastAPI Depends 的参数

    Python 语法提示：
        闭包（Closure）：内层函数（checker）可以访问外层函数（require_project_role）的变量（min_role）
        即使外层函数已经返回，内层函数依然持有 min_role 的引用（这就是"捕获"）
    """
    # checker 是一个内层 async 函数（闭包），捕获了外层的 min_role 变量
    # FastAPI 会自动检测函数参数的类型注解和 Depends(...)，完成依赖注入
    async def checker(
        project_id: str,                           # 从 URL path 自动提取（{project_id}）
        user: User = Depends(get_current_user),    # FastAPI 注入：从 JWT 解析当前用户
        db: AsyncSession = Depends(get_db),        # FastAPI 注入：提供 DB session
    ) -> str:
        """检查用户在指定工程的权限是否满足 min_role 要求。

        Returns:
            用户实际 role（供路由函数进一步使用，如判断是否显示某些按钮）

        Raises:
            HTTPException(404): 工程不存在
            HTTPException(403): 无权限
        """
        # db.get(Model, primary_key)：按主键查询单条记录（比 select 更简洁）
        # 等价于：SELECT * FROM projects WHERE id = project_id LIMIT 1
        # 返回 ORM 对象或 None
        project = await db.get(Project, project_id)
        if not project:
            # HTTPException：FastAPI 的标准异常，自动转成 JSON 错误响应
            # status_code=404：HTTP 404 Not Found
            # detail：JSON 响应里的错误信息字段
            raise HTTPException(status_code=404, detail="工程不存在")

        # 计算用户在这个工程的最终 role
        role = await resolve_role(user, project, db)

        if role is None:
            # 无任何成员关系 → 403 Forbidden
            raise HTTPException(status_code=403, detail="无权访问此工程")

        # 权限不足：用户 role 的等级 < 要求的最低等级
        # ROLE_RANK[role] < ROLE_RANK[min_role]：数字比较，避免字符串比较的歧义
        if ROLE_RANK[role] < ROLE_RANK[min_role]:
            raise HTTPException(
                status_code=403,
                # f-string：Python 3.6+ 的字符串格式化语法，{变量} 直接嵌入变量值
                detail=f"需要 {min_role} 及以上权限（当前 {role}）",
            )

        return role  # 返回实际 role，供路由函数使用（如权限细化控制）

    return checker  # 返回闭包函数（不是调用它！）


def require_group_role(min_role: str = "reporter"):
    """权限 dependency 工厂：验证当前用户在 path 里 group_id 对应 group 的 role ≥ min_role。

    用法：
        @router.post("/groups/{group_id}/members",
                     dependencies=[Depends(require_group_role("owner"))])

    与 require_project_role 同理，但作用于 group 而非 project。

    Args:
        min_role: 最低要求 role，默认 'reporter'

    Returns:
        async checker 闭包
    """
    async def checker(
        group_id: str,                             # 从 URL path 自动提取（{group_id}）
        user: User = Depends(get_current_user),    # FastAPI 注入：当前用户
        db: AsyncSession = Depends(get_db),        # FastAPI 注入：DB session
    ) -> str:
        """检查用户在指定 group 的权限。"""
        # 查 group 是否存在（按主键查询）
        group = await db.get(Group, group_id)
        if not group:
            raise HTTPException(status_code=404, detail="组不存在")

        # Instance Admin：直接 owner，不需要查成员表
        # 这里处理 is_admin，而 resolve_group_role 函数本身不处理（职责分离）
        if user.is_admin:
            return "owner"

        # 普通用户：通过继承链计算 group role
        role = await resolve_group_role(user.id, group_id, db)

        if role is None:
            raise HTTPException(status_code=403, detail="无权访问此组")

        if ROLE_RANK[role] < ROLE_RANK[min_role]:
            raise HTTPException(
                status_code=403,
                detail=f"需要 {min_role} 及以上权限（当前 {role}）",
            )

        return role  # 返回实际 role

    return checker  # 返回闭包
