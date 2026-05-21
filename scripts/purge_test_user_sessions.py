"""清 qa_sessions 表中 alice/bob/carol 三个测试用户的 ghost rows。

背景：S6 把 messages 迁到 fs，但 qa_sessions 元数据仍在 DB。手动 rm -rf .ke-memory
清空 messages 后，sidebar 仍列出 ghost session（DB 元数据未清）→ 点进去 messages
为空 → 显示 EmptyState（看似"未跳转"）。

用法：./venv/bin/python -m scripts.purge_test_user_sessions

幂等：可重复跑（无 session 时跳过）。
"""
# from __future__ import annotations 让类型注解延后求值（Python 3.12+ 仍是 best practice）
from __future__ import annotations

# asyncio：跑 async 函数；标准库
import asyncio

# dotenv 加载 .env.local（含 KE_DB_URL）
from dotenv import load_dotenv
load_dotenv(".env.local")  # 必须在 from src... 之前加载，否则 get_session_maker() 拿不到 DB URL

# select / delete：SQLAlchemy 2.x 风格的 SQL 构造函数
from sqlalchemy import select, delete

# KE 既有 DB session 工厂
from src.service.db import get_session_maker
# 用户模型（用来查 user_id）
from src.service.auth_models import User
# 会话元数据 ORM 模型（QASession 定义在 db_models_homepage.py，与 Project/UserProjectAccess 同模块）
from src.service.db_models_homepage import QASession

# 测试用户名单（与 scripts/setup_test_users.py 一致）
_TEST_USERNAMES = ("alice", "bob", "carol")


async def main() -> None:
    """主函数：列出 → 删除 → 验证。"""
    # get_session_maker() 返回 SQLAlchemy 异步 session 工厂
    SM = get_session_maker()
    # async with：异步上下文管理器，确保 session 用完自动关闭
    async with SM() as s:
        # ── 找三个测试用户的 id ──────────────────────────────
        # select(User.id, User.username)：只取需要的列（避免拉全表字段）
        # .where(User.username.in_([...])): 等价 SQL `WHERE username IN (...)`
        result = await s.execute(
            select(User.id, User.username).where(User.username.in_(_TEST_USERNAMES))
        )
        # .all() 拿所有行；每行是 namedtuple，可以用 .id / .username 访问
        users = result.all()
        # dict comprehension：把 [(id, username), ...] 转 {username: id}
        uid_map = {u.username: u.id for u in users}
        print(f"测试用户: {uid_map}")

        if not uid_map:
            print("⚠️  未找到任何测试用户，请先跑 setup_test_users.py")
            return

        # list(uid_map.values()) 取所有 user_id（int 列表）
        uids = list(uid_map.values())

        # ── 列出每个用户的 session（删前 snapshot）────────────
        for uname, uid in uid_map.items():
            rows = (await s.execute(
                # select 多列：sessions 表的 id / title / project_id
                select(QASession.id, QASession.title, QASession.project_id)
                .where(QASession.user_id == uid)
            )).all()
            print(f"\n[{uname} uid={uid}] 现有 {len(rows)} sessions:")
            for r in rows:
                # f-string 嵌入表达式；r.title 可能 None（未命名 session）
                print(f"  {r.id} | project={r.project_id} | title={r.title!r}")

        # ── 执行删除 ──────────────────────────────────────────
        # delete(QASession).where(...) 生成 DELETE FROM qa_sessions WHERE user_id IN (...)
        # 注意：之前 alembic s6_drop_memory_tables 已 drop qa_messages 表，
        # qa_sessions 没有外键引用，DELETE 直接成功（不需级联）
        result = await s.execute(
            delete(QASession).where(QASession.user_id.in_(uids))
        )
        # result.rowcount 返回受影响行数（int）
        print(f"\n✅ 删除 {result.rowcount} 行 qa_sessions")
        # commit：把事务里的 DELETE 真正落库
        await s.commit()

        # ── 验证：删后每用户应剩 0 session ────────────────────
        print("\n=== 删除后验证 ===")
        for uname, uid in uid_map.items():
            count = (await s.execute(
                select(QASession).where(QASession.user_id == uid)
            )).all()
            # f"{var}"  右侧自动 str() 转换
            print(f"  {uname}: 剩 {len(count)} sessions")


# Python 入口惯用法：模块直接运行时执行 main()；被 import 时不跑
if __name__ == "__main__":
    # asyncio.run(coroutine) 启动事件循环跑 async 函数到结束，自动 close loop
    asyncio.run(main())
