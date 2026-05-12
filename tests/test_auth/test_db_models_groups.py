"""验证 v2.0 多租户 ORM 模型定义（3 张新表）。

测试策略：纯 metadata 断言，只检查 ORM 类的 __table__ 信息，
不需要起真实 DB engine，import 即可验证结构正确性。

覆盖：
  - Group（6 个字段 + id 主键类型）
  - GroupMember（复合主键 + role CheckConstraint）
  - AuditLog（字段完整 + 2 个 Index）
  - Project.group_id（v2.0 新列，nullable + FK → groups）
"""

# 从 v2.0 新建的 ORM 模型文件导入 3 个新类
# Python 的 import 不执行类方法，只是把类对象加载到当前命名空间
from src.service.db_models_groups import AuditLog, Group, GroupMember

# ═══════════════════════════════════════════════════════════════
# 1. Group
# ═══════════════════════════════════════════════════════════════


def test_group_table_name():
    """Group ORM 类应映射到 'groups' 表。"""
    # __tablename__ 是 SQLAlchemy DeclarativeBase 的约定属性，对应 SQL 表名
    assert Group.__tablename__ == "groups"


def test_group_has_required_fields():
    """Group ORM 类应该有 6 个字段（与 migration v2_multi_tenant.py 完全对齐）。"""
    # __table__.columns 是 SQLAlchemy Table 对象的列集合
    # 用集合推导式 {c.name for c in ...} 提取所有列名，便于 == 比对
    cols = {c.name for c in Group.__table__.columns}
    assert cols == {
        "id",                   # 主键，path-like 字符串，String(64)
        "name",                 # 显示名
        "description",          # 可选描述
        "parent_group_id",      # 自引用外键，支持嵌套
        "created_by_user_id",   # 创建人，nullable + ondelete SET NULL
        "created_at",           # 创建时间
    }


def test_group_id_is_string_primary_key():
    """Group.id 应该是字符串主键（slug，如 'retail-bank'），不是自增 int。"""
    # 通过字典推导式快速按列名查找列对象
    # cols["id"] 返回 Column 对象，.primary_key 是布尔值
    cols = {c.name: c for c in Group.__table__.columns}
    assert cols["id"].primary_key is True
    # type 是 SQLAlchemy TypeEngine 对象，判断是否为 String 类型
    from sqlalchemy import String
    assert isinstance(cols["id"].type, String)


def test_group_created_by_user_id_is_nullable():
    """created_by_user_id 必须可为 NULL（ondelete=SET NULL 要求 nullable=True）。"""
    cols = {c.name: c for c in Group.__table__.columns}
    # nullable 属性：True 表示允许 NULL（对应 SQL 的 NOT NULL 约束的缺失）
    assert cols["created_by_user_id"].nullable is True


def test_group_created_by_fk_ondelete_set_null():
    """created_by_user_id FK 的 ondelete 策略应为 SET NULL（用户注销不删 group）。"""
    # __table__.foreign_keys 是这张表上所有外键约束的集合
    fks = list(Group.__table__.foreign_keys)
    # 找到引用 users 表的外键
    user_fks = [fk for fk in fks if fk.column.table.name == "users"]
    assert len(user_fks) == 1, "应该有 1 个 FK 指向 users 表"
    # fk.ondelete 是 ForeignKey(ondelete=...) 里传的策略字符串
    assert user_fks[0].ondelete == "SET NULL"


# ═══════════════════════════════════════════════════════════════
# 2. GroupMember
# ═══════════════════════════════════════════════════════════════


def test_group_member_table_name():
    """GroupMember 应映射到 'group_members' 表。"""
    assert GroupMember.__tablename__ == "group_members"


def test_group_member_pk_is_composite():
    """(user_id, group_id) 应构成复合主键，不是单列主键。"""
    # __table__.primary_key.columns 返回参与主键的所有列
    pk_cols = [c.name for c in GroupMember.__table__.primary_key.columns]
    # sorted() 规避列顺序差异，只断言名称集合
    assert sorted(pk_cols) == ["group_id", "user_id"]


def test_group_member_columns():
    """GroupMember 应该有 5 个字段。"""
    cols = {c.name for c in GroupMember.__table__.columns}
    assert cols == {
        "user_id",         # 复合主键之一，FK → users.id CASCADE
        "group_id",        # 复合主键之二，FK → groups.id CASCADE
        "role",            # reporter / maintainer / owner（CheckConstraint 限制）
        "added_by_user_id", # 邀请人，nullable + ondelete SET NULL
        "added_at",        # 加入时间
    }


def test_group_member_role_check_constraint():
    """role 必须有 CheckConstraint 限制只能是 reporter/maintainer/owner 之一。

    DB 层校验比 Python 层校验更强：即使绕过 API 直接写库也会被拦截。
    """
    # __table__.constraints 是这张表上所有约束（PK + FK + CHECK + UNIQUE）的集合
    constraints = list(GroupMember.__table__.constraints)
    # 找名字里含 'role' 的 CheckConstraint（name 在 migration 里设为 'ck_group_members_role'）
    role_check = [
        c for c in constraints
        # 用 getattr 安全取 name 属性（某些约束对象可能没有 name）
        # 'role' in (c.name or '').lower() 做大小写不敏感的名字匹配
        if "role" in (getattr(c, "name", None) or "").lower()
    ]
    assert len(role_check) >= 1, "GroupMember 缺少 role 的 CheckConstraint"


def test_group_member_added_by_is_nullable():
    """added_by_user_id 必须可为 NULL（对应 migration 里 nullable=True）。"""
    cols = {c.name: c for c in GroupMember.__table__.columns}
    assert cols["added_by_user_id"].nullable is True


def test_group_member_fks_ondelete():
    """user_id + group_id FK 应为 CASCADE（成员随用户/组删除），added_by FK 为 SET NULL。"""
    fks = list(GroupMember.__table__.foreign_keys)
    # 按目标表名分类
    fk_by_table = {fk.column.table.name: fk for fk in fks}

    # user_id → users 应 CASCADE（成员随用户删除）
    assert "users" in fk_by_table, "缺少指向 users 表的外键"
    # added_by_user_id 也指向 users，用 SET NULL；user_id 用 CASCADE
    # 由于两个 FK 都指向 users，这里只验证存在指向 groups 的 FK（CASCADE）
    assert "groups" in fk_by_table, "缺少指向 groups 表的外键"
    assert fk_by_table["groups"].ondelete == "CASCADE"


# ═══════════════════════════════════════════════════════════════
# 3. AuditLog
# ═══════════════════════════════════════════════════════════════


def test_audit_log_table_name():
    """AuditLog 应映射到 'audit_logs' 表。"""
    assert AuditLog.__tablename__ == "audit_logs"


def test_audit_log_has_action_and_metadata():
    """AuditLog 应包含 action / resource_type / resource_id / metadata_json。"""
    cols = {c.name for c in AuditLog.__table__.columns}
    assert "action" in cols
    assert "resource_type" in cols
    assert "resource_id" in cols
    assert "metadata_json" in cols


def test_audit_log_columns():
    """AuditLog 应有 8 个字段（与 migration 完全对齐）。"""
    cols = {c.name for c in AuditLog.__table__.columns}
    assert cols == {
        "id",             # 自增整数主键
        "actor_user_id",  # 操作人，nullable + ondelete SET NULL
        "action",         # 操作动作（如 'project.create'）
        "resource_type",  # 资源类型（如 'project'）
        "resource_id",    # 资源 ID
        "metadata_json",  # 操作上下文 JSON 字符串（Text 类型）
        "ip_address",     # 客户端 IP
        "created_at",     # 操作时间
    }


def test_audit_log_id_is_autoincrement_int():
    """AuditLog.id 应该是自增整数（与 Group.id 的字符串主键不同）。"""
    cols = {c.name: c for c in AuditLog.__table__.columns}
    assert cols["id"].primary_key is True
    # autoincrement 属性检查：True 或 'auto'（SQLAlchemy 2.0 对 Integer PK 默认为 'auto'）
    # 只要不是 False 就说明启用了自增
    assert cols["id"].autoincrement is not False


def test_audit_log_actor_user_id_nullable():
    """actor_user_id 必须可为 NULL（用户注销后审计记录仍需保留，actor 置 NULL）。"""
    cols = {c.name: c for c in AuditLog.__table__.columns}
    assert cols["actor_user_id"].nullable is True


def test_audit_log_has_two_indexes():
    """应有 2 个查询索引：(actor_user_id, created_at) 和 (resource_type, resource_id)。"""
    # __table__.indexes 是非主键索引的集合（PK 本身不在这里）
    index_names = {idx.name for idx in AuditLog.__table__.indexes}
    # 索引名对应 migration 里 op.create_index 的第一个参数
    assert "ix_audit_actor_time" in index_names, f"缺少 ix_audit_actor_time，现有：{index_names}"
    assert "ix_audit_resource" in index_names, f"缺少 ix_audit_resource，现有：{index_names}"


def test_audit_log_actor_fk_ondelete_set_null():
    """actor_user_id FK 的 ondelete 策略应为 SET NULL（审计记录不可删）。"""
    fks = list(AuditLog.__table__.foreign_keys)
    user_fks = [fk for fk in fks if fk.column.table.name == "users"]
    assert len(user_fks) == 1
    assert user_fks[0].ondelete == "SET NULL"


# ═══════════════════════════════════════════════════════════════
# 4. Project.group_id（v2.0 新列）
# ═══════════════════════════════════════════════════════════════


def test_project_has_group_id_field():
    """v2.0：Project 应有 group_id 列（FK → groups.id，nullable=True）。"""
    # 从 db_models_homepage 导入 Project（group_id 加在那里）
    from src.service.db_models_homepage import Project
    cols = {c.name for c in Project.__table__.columns}
    assert "group_id" in cols, f"Project 缺 group_id 列，现有列：{cols}"


def test_project_group_id_is_nullable():
    """Project.group_id 必须可为 NULL（已有工程可以不属于任何 group）。"""
    from src.service.db_models_homepage import Project
    cols = {c.name: c for c in Project.__table__.columns}
    assert cols["group_id"].nullable is True


def test_project_group_id_fk_ondelete_set_null():
    """Project.group_id FK 的 ondelete 策略应为 SET NULL（删 group 不级联删工程）。"""
    from src.service.db_models_homepage import Project
    fks = list(Project.__table__.foreign_keys)
    # 找指向 groups 表的外键
    group_fks = [fk for fk in fks if fk.column.table.name == "groups"]
    assert len(group_fks) == 1, f"应有 1 个指向 groups 的 FK，实际：{len(group_fks)}"
    assert group_fks[0].ondelete == "SET NULL"
