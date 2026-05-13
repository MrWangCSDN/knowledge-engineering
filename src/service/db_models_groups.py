"""v2.0 多租户 ORM 模型：Group / GroupMember / AuditLog。

设计背景：
  知识工程平台 v2.0 支持多租户，工程（Project）可以归属到一个 Group；
  每个 Group 可以有若干成员（GroupMember），成员有三种角色：
  reporter（只读）/ maintainer（读写）/ owner（管理员）。
  所有管理操作都写入 AuditLog，供合规审计用。

表与 migration 的对应关系：
  migration 文件：alembic/versions/v2_multi_tenant.py
  这里的 ORM 类与 migration 里的 op.create_table() 必须字段完全一致。

依赖：
  - SQLAlchemy >= 2.0（使用 Mapped + mapped_column 的新式写法）
  - Base 从 src.service.db 导入（与其他 ORM 文件保持一致）
"""

# __future__.annotations 让类型注解支持"前向引用"——
# 在类还没定义完时就可以在注解里用类名字符串，如 Mapped["Group"]
from __future__ import annotations

# datetime 是 Python 标准库的日期时间类，用于类型注解
from datetime import datetime

# Optional[X] 等价于 Union[X, None]，表示这个字段可以是 X 类型，也可以是 None（NULL）
from typing import Optional

# 从 SQLAlchemy 导入需要的类型和工具
from sqlalchemy import (
    CheckConstraint,   # DB 层 CHECK 约束（确保字段值满足某条件）
    DateTime,          # 日期时间类型，对应 MySQL DATETIME / SQLite TEXT
    ForeignKey,        # 外键约束，建立表间关联
    Index,             # 数据库索引定义（加速查询）
    Integer,           # 整数类型，对应 MySQL INT
    String,            # 变长字符串类型，对应 MySQL VARCHAR
    Text,              # 不限长文本类型，对应 MySQL TEXT
    func,              # SQL 函数（如 func.now() 表示数据库的当前时间函数）
)

# Mapped：SQLAlchemy 2.0 新式类型注解标记，告诉 ORM 这个属性对应一个数据库列
# mapped_column：2.0 新式列定义函数，替代旧版的 Column
# relationship：定义两个 ORM 类之间的关联关系（非数据库外键，是 Python 层的导航属性）
from sqlalchemy.orm import Mapped, mapped_column, relationship

# 导入项目内共享的 DeclarativeBase 子类（Base）
# 所有 ORM 类都必须继承自同一个 Base，SQLAlchemy 才能统一管理它们的 metadata
from src.service.db import Base


# ─── 1. groups 表 ────────────────────────────────────────────────────────────

class Group(Base):
    """工程分组 ORM 模型，对应数据库 'groups' 表。

    一个 Group 可以包含多个 Project，也可以有子 Group（最多 3 层，由 service 层校验）。
    ID 用业务可读字符串（如 'retail-bank'），而不是自增整数，方便 URL 里直接用。
    """

    # __tablename__ 是 SQLAlchemy 约定的类属性，指定对应的数据库表名
    __tablename__ = "groups"

    # ─── 列定义 ─────────────────────────────────────────────────────────────

    # Mapped[str] 表示这个字段在 Python 里是 str 类型，且非 NULL（非 Optional）
    # mapped_column(String(64), primary_key=True) 定义这是一个最长 64 字符的字符串主键
    # 例如：'retail-bank', 'core-system', 'deposit-backend'
    id: Mapped[str] = mapped_column(String(64), primary_key=True)

    # 分组显示名称，必填（nullable=False 是 SQLAlchemy 的默认值，但显式写出更清晰）
    name: Mapped[str] = mapped_column(String(128), nullable=False)

    # 可选描述；Mapped[Optional[str]] 对应 SQL 的 VARCHAR(512) NULL
    # Optional[str] = Union[str, None]，告诉类型检查器这里可以是 None
    description: Mapped[Optional[str]] = mapped_column(String(512), nullable=True)

    # 父分组 ID：自引用外键（FK 指向同一张表的 id 列）
    # 根 Group 此字段为 None；子 Group 填父 Group 的 id
    # ondelete='RESTRICT' 表示：如果这个 Group 还有子 Group，数据库拒绝删除（防止孤儿节点）
    parent_group_id: Mapped[Optional[str]] = mapped_column(
        String(64),
        ForeignKey("groups.id", ondelete="RESTRICT"),  # 自引用外键，禁止删有子组的 group
        nullable=True,  # 根 group 无父节点，允许 NULL
    )

    # 创建者用户 ID：FK → users.id
    # ondelete='SET NULL' 表示：用户注销后，此字段自动置 NULL，不阻止删除用户
    # nullable=True 必须与 ondelete='SET NULL' 配合——否则数据库置 NULL 时会违反 NOT NULL 约束
    created_by_user_id: Mapped[Optional[int]] = mapped_column(
        Integer,
        ForeignKey("users.id", ondelete="SET NULL"),  # 用户注销 → 置 NULL，不删 group
        nullable=True,  # 与 SET NULL 策略配合，允许 NULL
    )

    # server_default=func.now() 让数据库自动填入当前时间（比 Python 层的 default=datetime.now 更可靠）
    # 区别：Python default 在 INSERT 时由 Python 进程计算，server_default 由数据库计算（避免时区/时钟漂移问题）
    created_at: Mapped[datetime] = mapped_column(
        DateTime,
        server_default=func.now(),  # SQL: DEFAULT CURRENT_TIMESTAMP
        nullable=False,
    )

    # ─── 表级约束与索引 ────────────────────────────────────────────────────
    # __table_args__ 是 SQLAlchemy 约定属性，接受 tuple；Table 选项（dict）放最后
    # 显式声明索引名 ix_groups_parent 与 migration 保持一致，避免 autogenerate drift
    __table_args__ = (
        # 加速"列出某父 group 的子 group"查询（parent_group_id IS NOT NULL 的过滤场景）
        # 索引名必须与 migration 里 op.create_index('ix_groups_parent', ...) 完全一致
        Index("ix_groups_parent", "parent_group_id"),
    )

    # ─── 关系导航 ─────────────────────────────────────────────────────────
    # relationship 是 SQLAlchemy ORM 层的"导航属性"，不对应 DB 列，只是 Python 层的快捷访问
    # cascade="all, delete-orphan"：删 Group 时，其下所有 GroupMember 记录也自动从 session 中删
    # back_populates="group"：在 GroupMember 里有对应的 group 属性，形成双向导航
    # Mapped[list["GroupMember"]] 是 SQLAlchemy 2.0 写法，表示"零到多个 GroupMember"
    # 用字符串 "GroupMember" 前置引用（forward reference），因为该类在下面才定义
    members: Mapped[list["GroupMember"]] = relationship(
        "GroupMember",
        cascade="all, delete-orphan",  # 删 Group 级联删所有 GroupMember
        back_populates="group",        # 与 GroupMember.group 双向绑定
    )


# ─── 2. group_members 表 ─────────────────────────────────────────────────────

class GroupMember(Base):
    """工程分组成员 ORM 模型，对应数据库 'group_members' 表。

    记录"哪个用户属于哪个组、以什么角色"。
    主键是 (user_id, group_id) 复合主键，每个用户在每个组里只能有一条记录。
    role 受 DB 层 CheckConstraint 约束，只能是三个值之一。
    """

    # 表名
    __tablename__ = "group_members"

    # ─── 列定义 ─────────────────────────────────────────────────────────────

    # 复合主键之一：用户 ID（整数，FK → users.id）
    # primary_key=True 在多列同时设置时，SQLAlchemy 自动将它们合并为复合主键
    # ondelete='CASCADE' 表示：用户被删时，此用户在所有组里的成员记录也自动删除
    user_id: Mapped[int] = mapped_column(
        Integer,
        ForeignKey("users.id", ondelete="CASCADE"),  # 用户删 → 成员记录级联删
        primary_key=True,  # 复合主键第一部分
    )

    # 复合主键之二：组 ID（字符串，FK → groups.id）
    # ondelete='CASCADE' 表示：组被删时，该组内所有成员记录自动删除
    group_id: Mapped[str] = mapped_column(
        String(64),
        ForeignKey("groups.id", ondelete="CASCADE"),  # 组删 → 成员记录级联删
        primary_key=True,  # 复合主键第二部分
    )

    # 成员角色，非空；具体允许的值由下面的 CheckConstraint 在 DB 层保证
    role: Mapped[str] = mapped_column(String(16), nullable=False)

    # 邀请人用户 ID：FK → users.id，用于审计"是谁把这个人加进来的"
    # ondelete='SET NULL'：邀请人账号注销后此字段置 NULL，不阻止删除用户
    # nullable=True：与 SET NULL 配合
    added_by_user_id: Mapped[Optional[int]] = mapped_column(
        Integer,
        ForeignKey("users.id", ondelete="SET NULL"),  # 邀请人注销 → 置 NULL，不删成员记录
        nullable=True,
    )

    # 加入时间，数据库自动填入
    added_at: Mapped[datetime] = mapped_column(
        DateTime,
        server_default=func.now(),  # SQL: DEFAULT CURRENT_TIMESTAMP
        nullable=False,
    )

    # ─── 关系导航 ─────────────────────────────────────────────────────────
    # group 是从 GroupMember 到 Group 的多对一导航属性（每条成员记录归属一个组）
    # back_populates="members"：与 Group.members 双向绑定
    # Mapped["Group"] 使用字符串前置引用（虽然 Group 在上面定义，但加 from __future__ import annotations
    # 后所有注解默认是字符串，这里保持风格一致）
    group: Mapped["Group"] = relationship(
        "Group",
        back_populates="members",  # 与 Group.members 双向绑定
    )

    # ─── 表级约束与索引 ────────────────────────────────────────────────────
    # __table_args__ 是 SQLAlchemy 约定属性，接受一个元组（tuple），包含表级约束和选项
    # 元组最后一个元素如果是 dict，表示 Table(...) 的额外选项（这里不需要，但写法是约定）
    __table_args__ = (
        # CheckConstraint：DB 层校验 role 只能是 3 个合法值之一
        # 格式：CheckConstraint("SQL 条件表达式", name="约束名")
        # name 要与 migration 里 sa.CheckConstraint(..., name='ck_group_members_role') 完全一致
        # 这样 Alembic autogenerate 比对时才能识别为"同一个约束"，不会误判为需要新增
        CheckConstraint(
            "role IN ('reporter','maintainer','owner')",  # 只允许这 3 个字符串值
            name="ck_group_members_role",  # 约束名，必须与 migration 一致
        ),
    )


# ─── 3. audit_logs 表 ────────────────────────────────────────────────────────

class AuditLog(Base):
    """统一审计日志 ORM 模型，对应数据库 'audit_logs' 表。

    记录所有管理操作（创建/删除/修改工程、用户权限变更等），供合规审计用。
    日志只增不改，不支持更新/删除（在 service 层保证）。
    actor_user_id 允许 NULL：用户注销后，历史审计记录依然保留（合规要求），只是 actor 置空。
    """

    # 表名
    __tablename__ = "audit_logs"

    # ─── 列定义 ─────────────────────────────────────────────────────────────

    # 自增整数主键，与 Group.id（字符串）不同
    # autoincrement=True 告诉 SQLAlchemy 这是自增列（MySQL: AUTO_INCREMENT，SQLite: ROWID）
    # Mapped[int] 非 Optional，说明 id 不会为 NULL
    id: Mapped[int] = mapped_column(
        Integer,
        primary_key=True,
        autoincrement=True,  # MySQL: AUTO_INCREMENT；SQLite: ROWID 自动映射
    )

    # 操作人用户 ID：nullable + ondelete SET NULL
    # 审计记录是合规存档，不能删；但用户注销后 actor 可以置 NULL
    actor_user_id: Mapped[Optional[int]] = mapped_column(
        Integer,
        ForeignKey("users.id", ondelete="SET NULL"),  # 用户注销 → actor 置 NULL，日志保留
        nullable=True,  # 允许 NULL（用户注销后的历史记录）
    )

    # 操作动作，如 'project.create' / 'member.add' / 'group.delete'
    action: Mapped[str] = mapped_column(String(64), nullable=False)

    # 操作的资源类型，如 'project' / 'group' / 'credential'
    resource_type: Mapped[str] = mapped_column(String(32), nullable=False)

    # 操作的资源 ID（字符串，兼容 int ID 和 slug 字符串 ID）
    resource_id: Mapped[str] = mapped_column(String(128), nullable=False)

    # 操作上下文，存 JSON 字符串，记录"改了哪些字段"等详情
    # Text 是不限长文本（MySQL: TEXT，SQLite: TEXT）
    # 注意：这里用 Text + JSON 字符串，而不是 JSON 类型——保证跨 DB 兼容性
    metadata_json: Mapped[Optional[str]] = mapped_column(Text, nullable=True)

    # 客户端 IP 地址；IPv6 最长 39 字符，用 String(45) 可以兼容 IPv4 + IPv6
    ip_address: Mapped[Optional[str]] = mapped_column(String(45), nullable=True)

    # 操作时间，数据库自动填入
    created_at: Mapped[datetime] = mapped_column(
        DateTime,
        server_default=func.now(),  # SQL: DEFAULT CURRENT_TIMESTAMP
        nullable=False,
    )

    # ─── 表级索引 ─────────────────────────────────────────────────────────
    # 两个常用查询场景各一个联合索引，与 migration 里的 op.create_index 完全对应
    __table_args__ = (
        # 索引 1："某用户在某时间段内的操作" → 按 (actor_user_id, created_at) 查
        # Index(索引名, 字段1, 字段2, ...) 定义复合索引
        # 索引名必须与 migration 里 op.create_index('ix_audit_actor_time', ...) 完全一致
        Index("ix_audit_actor_time", "actor_user_id", "created_at"),

        # 索引 2："某资源的操作历史" → 按 (resource_type, resource_id) 查
        # 名字对应 migration 里的 'ix_audit_resource'
        Index("ix_audit_resource", "resource_type", "resource_id"),
    )
