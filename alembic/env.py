"""alembic 环境配置；走 KE_DB_URL 环境变量 + Base.metadata 自动检测 model。"""
import asyncio
import os
import sys
from logging.config import fileConfig
from pathlib import Path

from alembic import context
from sqlalchemy import pool
from sqlalchemy.engine import Connection
from sqlalchemy.ext.asyncio import async_engine_from_config

# 把项目根加入 sys.path 让 import src.* 工作
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from src.service.db import Base               # noqa: E402
from src.service import auth_models            # noqa: F401, E402  让 Base.metadata 看到 User

config = context.config
if config.config_file_name is not None:
    fileConfig(config.config_file_name)

# 注入 DB URL
db_url = os.getenv("KE_DB_URL", "")
if not db_url:
    raise RuntimeError("alembic: KE_DB_URL not set")
config.set_main_option("sqlalchemy.url", db_url)

target_metadata = Base.metadata


def do_run_migrations(connection: Connection) -> None:
    context.configure(
        connection=connection,
        target_metadata=target_metadata,
        compare_type=True,
        compare_server_default=True,
    )
    with context.begin_transaction():
        context.run_migrations()


async def run_migrations_online() -> None:
    cfg = config.get_section(config.config_ini_section, {})
    cfg["sqlalchemy.url"] = db_url
    eng = async_engine_from_config(cfg, prefix="sqlalchemy.", poolclass=pool.NullPool)
    async with eng.connect() as conn:
        await conn.run_sync(do_run_migrations)
    await eng.dispose()


def run_migrations_offline() -> None:
    context.configure(url=db_url, target_metadata=target_metadata, literal_binds=True,
                      dialect_opts={"paramstyle": "named"})
    with context.begin_transaction():
        context.run_migrations()


if context.is_offline_mode():
    run_migrations_offline()
else:
    asyncio.run(run_migrations_online())
