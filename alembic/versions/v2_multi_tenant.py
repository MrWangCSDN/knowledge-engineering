"""v2 multi-tenant: groups, group_members, audit_logs + projects.group_id + credentials.owner_user_id

v2.0 多租户权限改造：新建 3 张表 + 给 projects / git_credentials 加列。

新增表：
  · groups         —— 工程分组（支持最多 3 层嵌套，service 层校验）
  · group_members  —— 组成员关系（user_id + group_id 复合主键，role 三级）
  · audit_logs     —— 统一审计日志（actor/action/resource/metadata）

新增列：
  · projects.group_id        —— 工程所属 group（可为空，允许工程不属于任何 group）
  · git_credentials.owner_user_id —— 凭证归属用户（先 nullable，后续 Task 做数据迁移再改 NOT NULL）

Revision ID: v2_multi_tenant
Revises: 59e1dde21b76
Create Date: 2026-05-12
"""
# 从 alembic 包导入 op（Alembic 的操作对象，提供 DDL 操作方法如 create_table/drop_table 等）
from alembic import op
# 导入 sqlalchemy 的类型和函数系统（sa 是约定俗成的别名）
import sqlalchemy as sa

# revision: 本次迁移的唯一 ID（Alembic 用它追踪迁移链）
revision = 'v2_multi_tenant'
# down_revision: 上一个迁移的 revision ID（形成链式依赖；None 表示无前置）
down_revision = '59e1dde21b76'
# branch_labels / depends_on: 高级特性，多分支迁移用；本次不需要
branch_labels = None
depends_on = None


def upgrade() -> None:
    """执行正向迁移：创建新表、新列。

    按依赖顺序操作：
      1. groups（无依赖）
      2. group_members（依赖 groups + users）
      3. audit_logs（依赖 users）
      4. projects.group_id（依赖 groups）
      5. git_credentials.owner_user_id（依赖 users）
    """
    # ─── 1. groups 表 ─────────────────────────────────────────────────────
    # op.create_table() 接受表名 + 列定义列表，返回新建的 Table 对象
    op.create_table(
        'groups',
        # sa.Column(列名, 类型, 约束...) 定义一列
        # String(64) 表示最大长度 64 的 VARCHAR 类型
        sa.Column('id', sa.String(64), primary_key=True),  # 主键：用户自定义的 slug 字符串（如 "retail-bank"）
        sa.Column('name', sa.String(128), nullable=False),   # 显示名称，非空
        sa.Column('description', sa.String(512), nullable=True),  # 可选描述
        # parent_group_id: 自引用外键，实现树状嵌套结构
        # sa.ForeignKey(引用表.列, ondelete=策略) 定义外键约束
        # ondelete='RESTRICT' 表示有子 group 时禁止删除父 group
        sa.Column('parent_group_id', sa.String(64),
                  sa.ForeignKey('groups.id', ondelete='RESTRICT'),
                  nullable=True),  # 根 group 为 None，子 group 指向父 ID
        # created_by_user_id: 创建者用户 ID（外键 → users.id）
        # ondelete='SET NULL'：用户注销后仅将此字段置 NULL，不阻止删除用户（审计留痕，非强依赖）
        # nullable=True：允许 NULL，与 ondelete='SET NULL' 配合使用（否则 DB 置 NULL 会违反 NOT NULL 约束）
        sa.Column('created_by_user_id', sa.Integer,
                  sa.ForeignKey('users.id', ondelete='SET NULL'), nullable=True),
        # server_default=sa.func.now() 表示数据库服务端自动填入当前时间
        # （区别于 Python 层的 default=datetime.now()，server_default 更可靠）
        sa.Column('created_at', sa.DateTime, server_default=sa.func.now(), nullable=False),
    )
    # op.create_index(索引名, 表名, 列列表) 创建普通索引，加速按 parent_group_id 查询
    op.create_index('ix_groups_parent', 'groups', ['parent_group_id'])

    # ─── 2. group_members 表 ──────────────────────────────────────────────
    op.create_table(
        'group_members',
        # user_id + group_id 共同构成复合主键（primary_key=True 在两列都设置）
        # ondelete='CASCADE' 表示用户/组被删除时，自动删除对应成员记录
        sa.Column('user_id', sa.Integer,
                  sa.ForeignKey('users.id', ondelete='CASCADE'),
                  primary_key=True),  # 复合主键之一
        sa.Column('group_id', sa.String(64),
                  sa.ForeignKey('groups.id', ondelete='CASCADE'),
                  primary_key=True),  # 复合主键之二
        sa.Column('role', sa.String(16), nullable=False),  # 角色：'reporter' / 'maintainer' / 'owner'
        # added_by_user_id: 邀请人 ID（外键 → users.id）
        # ondelete='SET NULL'：邀请人账号注销后置 NULL，不阻止删除用户（此字段仅供审计追溯）
        # nullable=True：允许 NULL，与 ondelete='SET NULL' 配合（理由同 created_by_user_id）
        sa.Column('added_by_user_id', sa.Integer,
                  sa.ForeignKey('users.id', ondelete='SET NULL'), nullable=True),  # 是谁把这个人加进来的
        sa.Column('added_at', sa.DateTime, server_default=sa.func.now(), nullable=False),
        # sa.CheckConstraint(SQL表达式, name=约束名) 在数据库层面限制 role 只能是三个值之一
        # 这是 DB 级约束，比 Python 层校验更可靠（防止绕过 API 直接写库）
        sa.CheckConstraint("role IN ('reporter','maintainer','owner')", name='ck_group_members_role'),
    )

    # ─── 3. audit_logs 表 ─────────────────────────────────────────────────
    op.create_table(
        'audit_logs',
        # autoincrement=True 表示 MySQL/SQLite 自动递增整数主键
        sa.Column('id', sa.Integer, primary_key=True, autoincrement=True),
        sa.Column('actor_user_id', sa.Integer,
                  sa.ForeignKey('users.id', ondelete='SET NULL'),
                  nullable=True),  # v2.0：用户注销后审计 actor 置 NULL，记录保留（合规存档不可删）
        sa.Column('action', sa.String(64), nullable=False),    # 动作（如 "project.create"）
        sa.Column('resource_type', sa.String(32), nullable=False),  # 资源类型（如 "project"）
        sa.Column('resource_id', sa.String(128), nullable=False),   # 资源 ID
        # sa.Text() 是不限长度的大文本类型（MySQL: TEXT，SQLite: TEXT）
        # metadata_json 存 JSON 字符串，记录操作上下文（如改了哪些字段）
        sa.Column('metadata_json', sa.Text, nullable=True),
        # ip_address: IPv4 最长 15 位，IPv6 最长 39 位，String(45) 足够容纳 IPv6
        sa.Column('ip_address', sa.String(45), nullable=True),
        sa.Column('created_at', sa.DateTime, server_default=sa.func.now(), nullable=False),
    )
    # 联合索引：常见查询是"某用户在某段时间内的操作"，按 actor_user_id + created_at 建联合索引
    op.create_index('ix_audit_actor_time', 'audit_logs', ['actor_user_id', 'created_at'])
    # 另一个常见查询："某资源的操作历史"，按 resource_type + resource_id 建联合索引
    op.create_index('ix_audit_resource', 'audit_logs', ['resource_type', 'resource_id'])

    # ─── 4. projects 表新增 group_id 列 ───────────────────────────────────
    # op.batch_alter_table() 是 Alembic 提供的上下文管理器
    # 在 SQLite 中，ALTER TABLE 不支持直接加 FK 约束；batch 操作通过"重建表"实现
    # with ... as batch: 是 Python 上下文管理器语法，确保操作结束后正确清理
    with op.batch_alter_table('projects') as batch:
        # batch.add_column() 给现有表新增一列
        batch.add_column(sa.Column('group_id', sa.String(64), nullable=True))  # 可为空：已有工程不属于任何组
        # batch.create_foreign_key(约束名, 引用表, 本表列列表, 引用列列表, ondelete=策略)
        # v2.0：删 group 时把 project 的 group_id 置 NULL（不阻塞 group 删除）
        # 跟 ORM (db_models_groups.py 里 Project.group_id 的 ondelete) 保持一致
        batch.create_foreign_key(
            'fk_projects_group', 'groups', ['group_id'], ['id'],
            ondelete='SET NULL',
        )

    # ─── 5. git_credentials 表新增 owner_user_id 列 ───────────────────────
    # 先加 nullable=True，待后续 Task 把存量凭证关联到用户后，再改为 NOT NULL
    # 这是"滚动迁移"的典型模式：避免对有数据的表一步到位加 NOT NULL 导致报错
    with op.batch_alter_table('git_credentials') as batch:
        batch.add_column(sa.Column('owner_user_id', sa.Integer, nullable=True))  # 可为空
        batch.create_foreign_key('fk_credentials_owner', 'users', ['owner_user_id'], ['id'])


def downgrade() -> None:
    """执行逆向迁移：删列、删表（与 upgrade 顺序相反）。

    必须先删依赖方，再删被依赖的表；
    否则外键约束会阻止删除。
    """
    # 先撤销 git_credentials 的修改（依赖最少，最先回滚）
    with op.batch_alter_table('git_credentials') as batch:
        # drop_constraint(约束名, type_='foreignkey') 删除指定外键约束
        # type_ 参数必填，用于区分 foreignkey / unique / check / primary 等约束类型
        batch.drop_constraint('fk_credentials_owner', type_='foreignkey')
        batch.drop_column('owner_user_id')  # 删列

    # 再撤销 projects 的修改
    with op.batch_alter_table('projects') as batch:
        batch.drop_constraint('fk_projects_group', type_='foreignkey')
        batch.drop_column('group_id')

    # 删除 audit_logs 表（先删索引再删表，虽然 drop_table 会自动删索引，但显式写更清晰）
    op.drop_index('ix_audit_resource', 'audit_logs')
    op.drop_index('ix_audit_actor_time', 'audit_logs')
    op.drop_table('audit_logs')

    # 删除 group_members 表（先于 groups，因为 group_members FK → groups）
    op.drop_table('group_members')

    # 最后删 groups 表
    op.drop_index('ix_groups_parent', 'groups')
    op.drop_table('groups')
