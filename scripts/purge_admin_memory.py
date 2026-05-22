"""清 admin (uid=1) 的所有记忆数据（含 session）。

与 purge_test_user_memory.py 同语义，但 target=admin。
慎用：admin 可能是真实管理员账户，确认 admin 是测试身份再跑！

清除范围：
  1. 本地 fs：.ke-memory/u/1/ 整目录
  2. Weaviate Memory_l0 collection：tenant=1 的全部对象
  3. DB qa_sessions 表：user_id=1 的全部行

用法：./venv/bin/python -m scripts.purge_admin_memory

幂等：可重跑。
"""
from __future__ import annotations

import asyncio
import shutil
from pathlib import Path

from dotenv import load_dotenv
load_dotenv(".env.local")  # mac 端用 .env.local；服务器 .env 也兼容（dotenv 找不到 .env.local 时静默）

from sqlalchemy import select, delete
from src.service.db import get_session_maker
from src.service.auth_models import User
from src.service.db_models_homepage import QASession

# 目标用户名（hardcode 为 admin；用 username 查 uid，避免假设 uid=1）
_TARGET_USERNAME = "admin"


def _purge_local_fs(uid: int) -> None:
    """清 .ke-memory/u/{uid}/ 整个用户目录。"""
    from src.service.memory.vfs import MemoryFS
    fs = MemoryFS()
    root = Path(fs._root)
    print(f"=== 本地 fs ({root}) ===")
    user_dir = root / "u" / str(uid)
    if not user_dir.exists():
        print(f"  u/{uid}: 不存在，跳过")
        return
    file_count = sum(1 for _ in user_dir.rglob("*") if _.is_file())
    shutil.rmtree(user_dir)
    print(f"  u/{uid}: 已删 ({file_count} 文件)")


async def _purge_weaviate_tenant(uid: int) -> None:
    """清 Memory_l0 collection 中 tenant=str(uid) 的对象。"""
    print("\n=== Weaviate Memory_l0 tenant ===")
    import os
    import weaviate
    from weaviate.classes.init import Auth, AdditionalConfig, Timeout
    from urllib.parse import urlparse

    url = os.getenv("WEAVIATE_URL", "http://127.0.0.1:8080")
    api_key = os.getenv("WEAVIATE_API_KEY") or None
    parsed = urlparse(url)
    http_host = parsed.hostname or "127.0.0.1"
    http_port = parsed.port or 8080
    grpc_port = int(os.getenv("WEAVIATE_GRPC_PORT", "50051"))

    client = weaviate.connect_to_custom(
        http_host=http_host, http_port=http_port, http_secure=False,
        grpc_host=http_host, grpc_port=grpc_port, grpc_secure=False,
        auth_credentials=Auth.api_key(api_key) if api_key else None,
        skip_init_checks=True,
        additional_config=AdditionalConfig(timeout=Timeout(query=120, insert=120, init=30)),
    )
    try:
        col = client.collections.get("Memory_l0")
        all_tenants = col.tenants.get()
        existing = (
            list(all_tenants.keys()) if isinstance(all_tenants, dict)
            else [t.name for t in all_tenants]
        )
        target = str(uid)
        if target in existing:
            print(f"  清前 tenants: {existing}")
            col.tenants.remove([target])
            after = col.tenants.get()
            after_keys = (
                list(after.keys()) if isinstance(after, dict)
                else [t.name for t in after]
            )
            print(f"  已删 [{target!r}]，剩余 tenants: {after_keys}")
        else:
            print(f"  当前 tenants: {existing} — 无需清理（uid={uid} 不存在）")
    finally:
        client.close()


async def _purge_db_sessions(uid: int) -> int:
    """清 qa_sessions 中 user_id=uid 的所有行。"""
    print("\n=== DB qa_sessions ===")
    SM = get_session_maker()
    async with SM() as s:
        rows = (await s.execute(
            select(QASession.id, QASession.title)
            .where(QASession.user_id == uid)
        )).all()
        print(f"  uid={uid}: 现有 {len(rows)} sessions")
        for r in rows[:5]:
            print(f"    {r.id} | title={r.title!r}")
        if len(rows) > 5:
            print(f"    ... (+{len(rows) - 5} more)")

        result = await s.execute(
            delete(QASession).where(QASession.user_id == uid)
        )
        await s.commit()
        deleted = result.rowcount
        print(f"  ✅ 删除 {deleted} 行")
        return deleted


async def main() -> None:
    # 1) 查 admin 真实 uid（不假设 uid=1）
    SM = get_session_maker()
    async with SM() as s:
        result = await s.execute(
            select(User.id, User.username).where(User.username == _TARGET_USERNAME)
        )
        row = result.first()

    if row is None:
        print(f"⚠️  未找到用户名 '{_TARGET_USERNAME}'。请先确认 admin 是否存在。")
        return

    uid = row.id
    print(f"目标用户：{_TARGET_USERNAME} (uid={uid})")
    print(f"⚠️  即将清空此用户的全部记忆 + 会话数据。Ctrl+C 5 秒内取消。")
    await asyncio.sleep(5)

    _purge_local_fs(uid)
    await _purge_weaviate_tenant(uid)
    deleted_rows = await _purge_db_sessions(uid)

    print("\n=== 清理完成 ===")
    print(f"  用户: {_TARGET_USERNAME} (uid={uid})")
    print(f"  本地 fs u/{uid}/ 整目录已删")
    print(f"  Weaviate tenant '{uid}' 已删")
    print(f"  DB qa_sessions: {deleted_rows} 行已删")


if __name__ == "__main__":
    asyncio.run(main())
