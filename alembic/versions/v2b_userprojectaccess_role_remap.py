"""v2b: remap UserProjectAccess.role from v1 enum to v2 GitLab-style enum.

背景：
  v1 的 user_project_access.role 使用 reader / writer / admin 三值。
  v2.0 改为 GitLab 风格：reporter / maintainer / owner。
  ROLE_RANK 字典只认 v2 值，若存量数据保持 v1 值则 ROLE_RANK["reader"] → KeyError → 500。

本次迁移：
  1. 数据迁移：把现有 reader→reporter / writer→maintainer / admin→owner
  2. 加 CHECK 约束：强制 role 只能是 v2 三选之一，防止外部代码再写 v1 值

映射逻辑：
  reader  → reporter   (只读 → 只读；语义完全对应)
  writer  → maintainer (能改 → 管理员；v2 用 maintainer 表示读写)
  admin   → owner      (最高 → 最高；语义完全对应)

Revision ID: v2b_remap_role
Revises: v2_multi_tenant
Create Date: 2026-05-12
"""
# 从 alembic 包导入 op（Alembic 的 DDL 操作对象）
from alembic import op
# 导入 sqlalchemy 供类型声明使用（sa 是约定别名）
import sqlalchemy as sa

# revision: 本次迁移的唯一 ID，Alembic 用它追踪版本链
revision = 'v2b_remap_role'
# down_revision: 指向上一个迁移 ID，形成有向链表（链式迁移）
down_revision = 'v2_multi_tenant'
# branch_labels / depends_on: 多分支迁移高级特性，本次不需要
branch_labels = None
depends_on = None


def upgrade() -> None:
    """正向迁移：数据迁移 + 加 CHECK 约束。

    步骤：
      1. UPDATE：把 v1 role 映射到 v2 role（CASE WHEN）
      2. 加 CHECK 约束：强制 role 在 v2 三值之内

    注意：步骤 1 先跑，步骤 2 再加约束，顺序不可颠倒（否则迁移中存量 v1 数据会违反约束）。
    """
    # ── 步骤 1：数据迁移 ──────────────────────────────────────────────────────
    # op.execute(sql) 直接执行原始 SQL（用于批量更新，比 ORM 更高效）
    # SQL CASE WHEN ... ELSE role END：对每行 role 做映射；ELSE role 保留未知值不动（防御性）
    op.execute("""
        UPDATE user_project_access
        SET role = CASE role
            WHEN 'reader'  THEN 'reporter'
            WHEN 'writer'  THEN 'maintainer'
            WHEN 'admin'   THEN 'owner'
            ELSE role
        END
    """)

    # ── 步骤 2：加 CHECK 约束 ─────────────────────────────────────────────────
    # op.batch_alter_table()：SQLite 不支持 ALTER TABLE ADD CONSTRAINT，
    # batch 操作通过"临时建新表 → 复制数据 → 重命名"来实现 DDL 变更
    # with ... as batch_op: 是 Python 上下文管理器语法，确保 batch 操作结束后正确释放资源
    with op.batch_alter_table('user_project_access') as batch_op:
        # create_check_constraint(约束名, SQL 表达式字符串)
        # 约束在数据库层面强制 role 只能是 v2 三值之一
        # 比 Python 层校验更可靠：即使绕过 API 直接写库，DB 也会拒绝非法值
        batch_op.create_check_constraint(
            'ck_user_project_access_role',
            "role IN ('reporter','maintainer','owner')",
        )


def downgrade() -> None:
    """逆向迁移：删 CHECK 约束 + 把 v2 role 映射回 v1 role。

    步骤（与 upgrade 相反）：
      1. 删 CHECK 约束（否则接下来的 UPDATE 会写入 v1 值，违反约束）
      2. UPDATE：把 v2 role 映射回 v1 role
    """
    # ── 步骤 1：删 CHECK 约束 ─────────────────────────────────────────────────
    with op.batch_alter_table('user_project_access') as batch_op:
        # drop_constraint(约束名, type_='check') 删除指定约束
        # type_ 参数必须填写，用于区分 check / foreignkey / unique / primary 等约束类型
        batch_op.drop_constraint('ck_user_project_access_role', type_='check')

    # ── 步骤 2：数据回滚 ──────────────────────────────────────────────────────
    # 把 v2 role 映射回 v1 role（与 upgrade 的 CASE WHEN 方向相反）
    op.execute("""
        UPDATE user_project_access
        SET role = CASE role
            WHEN 'reporter'   THEN 'reader'
            WHEN 'maintainer' THEN 'writer'
            WHEN 'owner'      THEN 'admin'
            ELSE role
        END
    """)
