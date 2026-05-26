"""为真实工程 mall-swarm 创建 DB 记录 + 给 alice/bob/carol 配 owner 权限。

设计：
- admin 用户的 is_admin=True，已经全局可见所有 project（见 commit 618a432），
  所以无需为 admin 单独建 UserProjectAccess 行。
- alice/bob/carol 各自被显式设为 mall-swarm 的 owner（用户决策）。
- Project 记录附带 git_url=https://github.com/MrWangCSDN/mall-swarm（v1.0 仓库管理字段）。
- pipeline source path 由 config/project.yaml::repo.path 提供，这里不重复存。

幂等：已存在跳过，可重跑。

用法：
    cd /Users/java/knowledge-engineering-auth
    ./venv/bin/python -m scripts.seed_mall_swarm
"""
from __future__ import annotations  # 兼容 Python 3.10+ 的 PEP 563 注解延迟求值

import asyncio  # asyncio：跑异步 main 入口，因为 SQLAlchemy async session 需要 await
from sqlalchemy import select  # select(...) 是 SQLAlchemy 2.x 推荐的查询构造器

# 复用项目内的 DB session 工厂与 ORM 模型
from src.service.db import get_session_maker  # 返回 async sessionmaker
from src.service.auth_models import User  # auth 用户 ORM
from src.service.db_models_homepage import Project, UserProjectAccess  # 首页/工程 ORM


# ─── 配置常量 ────────────────────────────────────────────────────────────────
# 工程 ID 必须匹配 Project.id 的 pattern（小写起头，2-64 字符）
PROJECT_ID = "mall-swarm"
PROJECT_NAME = "mall-swarm（电商微服务示例）"
GIT_URL = "https://github.com/MrWangCSDN/mall-swarm"
# OWNERS：要赋予 owner 角色的 username 列表
# admin 不放这里——靠 is_admin=True 全局可见
OWNERS = ["alice", "bob", "carol"]


async def main() -> None:
    """主入口：建 project + 给 OWNERS 配 owner access。幂等可重跑。"""
    # get_session_maker() 返回 async_sessionmaker[AsyncSession]
    SM = get_session_maker()
    async with SM() as s:  # async with：进入异步上下文，确保 session 正确关闭
        # ── 1. 创建 / 校验 mall-swarm Project ─────────────────────────────
        # db.get(Model, pk) 是 SQLAlchemy 2.x 按主键加载的快捷方式
        existing = await s.get(Project, PROJECT_ID)
        if existing is not None:
            # 已存在就跳过创建，但仍打印当前状态便于排查
            print(f"  proj[skip] {PROJECT_ID} name={existing.name} status={existing.status}")
        else:
            # status='indexing' 表示工程已配置、等待 pipeline 跑完
            # （'configured' 是 v1.0 仓库管理用的；我们已经准备马上跑 pipeline，所以直接 indexing）
            p = Project(
                id=PROJECT_ID,
                name=PROJECT_NAME,
                language="java",
                status="indexing",
                git_url=GIT_URL,
                git_branch="master",  # mall-swarm 默认分支是 master 而非 main
                created_by="admin",
            )
            s.add(p)  # 将新对象加入 session 的 pending 队列
            print(f"  proj[new ] {PROJECT_ID} git_url={GIT_URL}")

        # ── 2. 查 OWNERS 这几个 username 对应的 user_id ───────────────────
        # in_ 是 SQL IN 操作符的 SQLAlchemy 写法
        users = (await s.execute(
            select(User).where(User.username.in_(OWNERS))
        )).scalars().all()
        # 构造 {username: id} 映射，方便下一步按名取 id
        uid_map = {u.username: u.id for u in users}
        # 校验：3 个 owner 都要存在，否则 setup_test_users 没跑过
        missing = [name for name in OWNERS if name not in uid_map]
        if missing:
            raise RuntimeError(
                f"以下 owner 用户不存在: {missing}；先跑 scripts/setup_test_users.py 建测试用户"
            )

        # ── 3. 为每个 owner 建 UserProjectAccess（owner 角色）─────────────
        for username in OWNERS:
            uid = uid_map[username]
            # UserProjectAccess 复合主键 (user_id, project_id)
            existing_acc = await s.get(UserProjectAccess, (uid, PROJECT_ID))
            if existing_acc is not None:
                # 幂等：已存在跳过；不强制改 role（保留人工调整空间）
                print(f"  acc [skip] {username:6s}@{PROJECT_ID} role={existing_acc.role}")
                continue
            a = UserProjectAccess(user_id=uid, project_id=PROJECT_ID, role="owner")
            s.add(a)
            print(f"  acc [new ] {username:6s}@{PROJECT_ID} role=owner")

        # commit 一次性提交所有变更（事务）
        await s.commit()

    # ── 4. 打印最终状态确认 ────────────────────────────────────────────────
    print("\n=== mall-swarm 接入结果 ===")
    async with SM() as s:
        # 重新查一次确认 commit 成功
        p = await s.get(Project, PROJECT_ID)
        print(f"\n[Project]\n  {p.id}  name={p.name}  status={p.status}  git_url={p.git_url}")
        # 拉所有 mall-swarm 的成员
        accs = (await s.execute(
            select(UserProjectAccess, User)
            .join(User, UserProjectAccess.user_id == User.id)
            .where(UserProjectAccess.project_id == PROJECT_ID)
            .order_by(User.username)
        )).all()
        print(f"\n[Members] (直接成员；admin 通过 is_admin=True 全局可见，未列于此)")
        for acc, user in accs:
            print(f"  {user.username:6s}  role={acc.role}")


if __name__ == "__main__":
    # asyncio.run 创建事件循环执行协程，结束后关闭
    asyncio.run(main())
