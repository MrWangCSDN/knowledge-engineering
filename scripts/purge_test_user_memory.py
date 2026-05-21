"""清 alice/bob/carol 三个测试用户的全部记忆数据（三层）。

清除范围（用户隔离）：
  1. 本地文件系统：.ke-memory/u/{uid}/（含 global/* 真记忆 + session/* 消息正文）
  2. Weaviate Memory_l0 collection：tenant=str(uid) 的全部对象
  3. DB qa_sessions 表：三个用户的会话元数据（与 purge_test_user_sessions.py 同语义）

只清这三个测试用户，绝不动 admin (uid=1) 或其他生产用户数据。

用法：./venv/bin/python -m scripts.purge_test_user_memory

幂等：可重跑（fs 不存在 / Weaviate 无 tenant / DB 无 session 都安全跳过）。
"""
# from __future__ import annotations 让类型注解延后求值（Python 3.12+ best practice）
from __future__ import annotations

# asyncio：跑 async；shutil：递归删目录；pathlib：跨平台路径
import asyncio
import shutil
from pathlib import Path

# dotenv 必须在 import src.* 之前加载，否则 DB URL / WEAVIATE_URL 拿不到
from dotenv import load_dotenv
load_dotenv(".env.local")

# SQLAlchemy 2.x 风格：select / delete
from sqlalchemy import select, delete

# KE 既有 DB session 工厂
from src.service.db import get_session_maker
# 用户 ORM（用来查 user_id）
from src.service.auth_models import User
# 会话元数据 ORM（QASession 定义在 db_models_homepage，与 Project / UserProjectAccess 同模块）
from src.service.db_models_homepage import QASession

# 测试用户名单（与 scripts/setup_test_users.py 矩阵一致；不假设 uid，跑时从 DB 查）
_TEST_USERNAMES = ("alice", "bob", "carol")


def _purge_local_fs(uids: list[int]) -> None:
    """清 .ke-memory/u/{uid}/ 整个用户目录。

    参数:
        uids: 要清的 user_id 列表（int）
    """
    # MemoryFS 默认 root：KE_MEM_ROOT 环境变量优先，缺省 = 仓库根/.ke-memory
    # 这里复用 MemoryFS 的同款解析逻辑，避免硬编码路径
    from src.service.memory.vfs import MemoryFS
    fs = MemoryFS()
    # MemoryFS 内部 _root 字段是 str 类型；转 Path 便于操作
    root = Path(fs._root)
    print(f"=== 本地 fs ({root}) ===")

    for uid in uids:
        # 路径：<root>/u/{uid}/
        user_dir = root / "u" / str(uid)
        if not user_dir.exists():
            # 目录不存在 → 已经是 clean state，跳过（幂等）
            print(f"  u/{uid}: 不存在，跳过")
            continue
        # 删前统计文件数（让用户看清规模）
        file_count = sum(1 for _ in user_dir.rglob("*") if _.is_file())
        # shutil.rmtree 递归删整个目录（含子目录、文件、空目录）
        shutil.rmtree(user_dir)
        print(f"  u/{uid}: 已删 ({file_count} 文件)")


async def _purge_weaviate_tenants(uids: list[int]) -> None:
    """清 Memory_l0 collection 中 tenant in uids 的全部对象。

    参数:
        uids: 要清的 user_id 列表（tenant 命名约定为 str(user_id)，见 recall.py:165）
    """
    print("\n=== Weaviate Memory_l0 tenants ===")
    # 局部 import：避免在 KE 主路径下加载 weaviate client（脚本独立运行）
    import os
    import weaviate
    from weaviate.classes.init import Auth, AdditionalConfig, Timeout

    # 读 KE 既有部署配置（同 recall.py:42-46）
    url = os.getenv("WEAVIATE_URL", "http://127.0.0.1:8080")
    api_key = os.getenv("WEAVIATE_API_KEY") or None

    # 从 URL 拆 host / port（KE 部署惯例 url=http://host:port，无 path）
    # urllib.parse 是 Python 标准库；urlparse 返 ParseResult namedtuple
    from urllib.parse import urlparse
    parsed = urlparse(url)
    # .hostname 自动去掉端口；.port 取端口号（int）；若无 port 则用默认 8080
    http_host = parsed.hostname or "127.0.0.1"
    http_port = parsed.port or 8080
    # gRPC port 由 .env 控制（recall.py 默认 50051）
    grpc_port = int(os.getenv("WEAVIATE_GRPC_PORT", "50051"))

    # weaviate v4 client：connect_to_custom 接受 HTTP + gRPC 双端口；
    # auth 可空（开源 Weaviate 不强制 api key）
    client = weaviate.connect_to_custom(
        http_host=http_host, http_port=http_port, http_secure=False,
        grpc_host=http_host, grpc_port=grpc_port, grpc_secure=False,
        auth_credentials=Auth.api_key(api_key) if api_key else None,
        skip_init_checks=True,
        # tenant remove 在大数据量下需后台清理对象，调长 timeout 防止超时报错
        additional_config=AdditionalConfig(timeout=Timeout(query=120, insert=120, init=30)),
    )
    try:
        # 取 Memory_l0 collection 句柄（不存在时 .get 仍返回对象，操作时才报错）
        col = client.collections.get("Memory_l0")
        # 拿当前所有 tenant；返 dict[tenant_name, Tenant] 或 list（v4 兼容性）
        all_tenants = col.tenants.get()
        # 兼容两种返回形态：dict 或 list of Tenant
        existing = (
            list(all_tenants.keys()) if isinstance(all_tenants, dict)
            else [t.name for t in all_tenants]
        )
        # 只删存在的、且属于这三个用户的 tenant（防误删别的）
        # set 交集 [tenant ∩ uid_strs]：保证只对测试用户操作
        uid_strs = {str(uid) for uid in uids}
        targets = [t for t in existing if t in uid_strs]

        if not targets:
            print(f"  当前 tenants: {existing} — 无需清理（无目标 tenant）")
        else:
            print(f"  清前 tenants: {existing}")
            # v4 API：tenants.remove(list_of_names) 一次批删
            col.tenants.remove(targets)
            # 删后再 query 一次验证
            after = col.tenants.get()
            after_keys = (
                list(after.keys()) if isinstance(after, dict)
                else [t.name for t in after]
            )
            print(f"  已删 {targets}，剩余 tenants: {after_keys}")
    finally:
        # 必须 close，否则 gRPC 连接泄漏（weaviate v4 用 connection pool）
        client.close()


async def _purge_db_sessions(uids: list[int]) -> int:
    """清 qa_sessions 表中 user_id in uids 的全部行。

    参数:
        uids: 要清的 user_id 列表（int）
    返回:
        被删的行数（int）；调用方打印用
    """
    print("\n=== DB qa_sessions ===")
    # get_session_maker() 复用 KE 既有 DB 引擎单例
    SM = get_session_maker()
    async with SM() as s:
        # 先看每个用户有几条 session（删前 snapshot）
        for uid in uids:
            rows = (await s.execute(
                # COUNT 不一定快，但样本小（< 100 条）无所谓；直接 select 行清单
                select(QASession.id, QASession.title)
                .where(QASession.user_id == uid)
            )).all()
            print(f"  uid={uid}: 现有 {len(rows)} sessions")
            for r in rows[:3]:                  # 只列前 3 条避免刷屏
                # r.title 可能为 None（未命名 session）；!r 用 repr 显示
                print(f"    {r.id} | title={r.title!r}")
            if len(rows) > 3:
                print(f"    ... (+{len(rows) - 3} more)")

        # DELETE FROM qa_sessions WHERE user_id IN (...)
        # alembic s6_drop_memory_tables 已 drop qa_messages 表，无 FK 阻塞
        result = await s.execute(
            delete(QASession).where(QASession.user_id.in_(uids))
        )
        await s.commit()
        # result.rowcount 是 SQLAlchemy 返回的受影响行数（int）
        deleted = result.rowcount
        print(f"  ✅ 删除 {deleted} 行")
        return deleted


async def main() -> None:
    """主流程：查 uid → 清 fs → 清 Weaviate → 清 DB → 验证。"""
    # ── 1) 查测试用户 uid（与 setup_test_users.py 矩阵一致；不假设硬编码值）
    SM = get_session_maker()
    async with SM() as s:
        result = await s.execute(
            select(User.id, User.username).where(User.username.in_(_TEST_USERNAMES))
        )
        users = result.all()

    uid_map = {u.username: u.id for u in users}
    print(f"测试用户 uid 映射: {uid_map}")

    if not uid_map:
        print("⚠️  未找到任何测试用户。请先跑 setup_test_users.py 创建。")
        return

    uids = list(uid_map.values())

    # ── 2) 清本地文件系统（同步函数，无 await）
    _purge_local_fs(uids)

    # ── 3) 清 Weaviate tenants（async；网络 IO）
    await _purge_weaviate_tenants(uids)

    # ── 4) 清 DB qa_sessions（async；DB IO）
    deleted_rows = await _purge_db_sessions(uids)

    # ── 5) 总结
    print("\n=== 清理完成 ===")
    print(f"  用户: {list(uid_map.keys())}")
    print(f"  本地 fs: u/{', u/'.join(str(u) for u in uids)} 整目录已删")
    print(f"  Weaviate: tenants {[str(u) for u in uids]} 已删")
    print(f"  DB qa_sessions: {deleted_rows} 行已删")
    print("\n下一步：浏览器硬刷新 (Cmd+Shift+R)，sidebar 应该完全空。")


# Python 入口惯用法：模块直接运行时执行 main()；被 import 时不跑
if __name__ == "__main__":
    # asyncio.run 启动事件循环、跑 async 函数到结束、自动 close loop
    asyncio.run(main())
