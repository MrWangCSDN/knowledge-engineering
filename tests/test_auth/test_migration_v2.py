"""测试 v2_multi_tenant Alembic 迁移文件。

验证目标：
  1. 跑完 upgrade head 后，3 张新表（groups / group_members / audit_logs）存在
  2. projects 表有 group_id 列
  3. git_credentials 表有 owner_user_id 列
  4. 使用 SQLite 文件 DB，不依赖 MySQL 生产环境

注意：
  - alembic/env.py 从 KE_DB_URL 环境变量读取 DB URL，测试前需要设置它
  - alembic/env.py 内部调用 asyncio.run()，因此本测试必须是同步函数（非 async def）
    如果测试本身是 async def，pytest-asyncio 已启动事件循环，
    再调 asyncio.run() 会报 "cannot be called from a running event loop"
  - 验证阶段用同步 SQLAlchemy inspect() 读表结构，不需要 async

依赖：pip install aiosqlite pytest（已在 venv 中）
"""
# os 模块：操作系统接口，这里用 os.environ 设置/读取环境变量
import os

# pytest 测试框架
import pytest

# SQLAlchemy 内省工具：用于检查已存在数据库的表结构、列、索引等
from sqlalchemy import inspect

# create_engine: 创建同步 SQLAlchemy 引擎（用于验证阶段的 inspect）
from sqlalchemy import create_engine

# alembic.config.Config: Alembic 配置对象（对应 alembic.ini）
from alembic.config import Config

# alembic.command: 提供 upgrade/downgrade 等操作的 Python API（同步函数）
from alembic import command


def test_migration_v2_creates_tables_and_columns(tmp_path):
    """v2 migration 跑完 head 后，3 张新表和 2 个新列都存在。

    这是一个同步测试（非 async def），原因：
      alembic/env.py 调用 asyncio.run()，如果在已有事件循环的 async 函数里
      再调 asyncio.run()，Python 会抛 RuntimeError。
      同步测试没有事件循环，所以 asyncio.run() 可以正常工作。

    Args:
        tmp_path: pytest 内置 fixture，每次测试提供独立临时目录（pathlib.Path 对象）
    """
    # 构建临时 SQLite 文件路径
    # pathlib.Path 支持 / 运算符拼路径（比字符串拼接更安全、跨平台）
    sqlite_file = tmp_path / "test.db"

    # alembic/env.py 使用 async_engine_from_config + asyncio.run()，
    # 所以 URL 需要用 aiosqlite（async SQLite 驱动）
    # aiosqlite:/// 后面跟绝对路径（3 个斜杠 = 协议前缀 + 绝对路径开头的 /）
    alembic_url = f"sqlite+aiosqlite:///{sqlite_file}"

    # 验证阶段用同步引擎 URL（pysqlite 是 SQLite 内置同步驱动）
    # 验证只需 inspect() 查表结构，不需要 async
    sync_url = f"sqlite+pysqlite:///{sqlite_file}"

    # 保存原有 KE_DB_URL（测试结束后恢复，防止污染其他测试）
    old_url = os.environ.get("KE_DB_URL")

    # 设置 KE_DB_URL 让 alembic/env.py 读取测试专用数据库
    os.environ["KE_DB_URL"] = alembic_url

    try:
        # Config("alembic.ini"): 读取项目根目录的 alembic.ini 配置
        # pytest 执行时 cwd 是项目根目录（pyproject.toml 里 testpaths = ["tests"]）
        cfg = Config("alembic.ini")

        # command.upgrade(cfg, "head"): 同步执行所有迁移，直到最新版本
        # "head" 表示最新的 revision（即 v2_multi_tenant）
        # 内部 env.py 会用 asyncio.run() 跑异步迁移逻辑
        command.upgrade(cfg, "head")

    finally:
        # finally 块：无论 upgrade 是否成功都执行（类似 Java 的 finally）
        # 恢复环境变量，防止污染后续测试
        if old_url is None:
            # 原来没有这个变量，删掉（dict.pop 不存在 key 时返回 None 不报错）
            os.environ.pop("KE_DB_URL", None)
        else:
            os.environ["KE_DB_URL"] = old_url

    # 迁移完成后，用同步引擎 + inspect() 验证表结构
    # create_engine(): 创建同步数据库引擎（SQLite pysqlite，不需要 async）
    engine = create_engine(sync_url)

    # engine.connect(): 建立数据库连接（同步上下文管理器）
    # with ... as conn: 自动关闭连接（上下文管理器，__enter__/__exit__ 协议）
    with engine.connect() as conn:
        # inspect(conn): 返回 Inspector 对象，提供查询数据库 schema 的方法
        insp = inspect(conn)

        # get_table_names(): 返回数据库中所有表名的列表（字符串列表）
        tables = insp.get_table_names()

        # 验证 3 张新表都已创建
        # assert 条件, 消息：条件为 False 时抛 AssertionError（pytest 捕获显示为测试失败）
        assert 'groups' in tables, f"groups 表不存在，实际表：{tables}"
        assert 'group_members' in tables, f"group_members 表不存在，实际表：{tables}"
        assert 'audit_logs' in tables, f"audit_logs 表不存在，实际表：{tables}"

        # get_columns(表名): 返回列信息字典列表，每个字典有 'name'/'type'/'nullable' 等 key
        # 列表推导式 [c['name'] for c in ...]: 从字典列表中提取所有列名
        proj_cols = [c['name'] for c in insp.get_columns('projects')]
        assert 'group_id' in proj_cols, (
            f"projects.group_id 不存在，实际列：{proj_cols}"
        )

        cred_cols = [c['name'] for c in insp.get_columns('git_credentials')]
        assert 'owner_user_id' in cred_cols, (
            f"git_credentials.owner_user_id 不存在，实际列：{cred_cols}"
        )

    # 释放引擎（关闭所有连接池中的连接）
    engine.dispose()
