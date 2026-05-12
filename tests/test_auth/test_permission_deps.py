"""权限解析算法测试（Task 3）。

测试策略：
  - 使用 in-memory aiosqlite 数据库，不需要真实 MySQL
  - 通过 Base.metadata.create_all 创建所有表（包括 users 表，因为 FK 依赖）
  - 每个测试独立 db fixture，数据互不干扰
  - 5 个测试覆盖 plan 里要求的所有核心场景

覆盖：
  1. test_instance_admin_always_owner    - is_admin=True 的用户永远是 owner
  2. test_no_membership_returns_none     - 无任何成员关系时返回 None
  3. test_group_role_inherits_to_project - group 成员角色继承到 project
  4. test_nested_group_inheritance       - 嵌套 group（root→child→project）继承
  5. test_pick_higher_takes_max          - _pick_higher 取两 role 中较高者
"""

# pytest 是 Python 最流行的测试框架，提供 assert 增强、fixture 注入、mark 等能力
import pytest

# pytest_asyncio 提供 async fixture 支持；@pytest_asyncio.fixture 是装饰 async fixture 的标准写法
import pytest_asyncio

# SQLAlchemy 2.0 异步 session 工厂和引擎创建函数
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

# 导入 ORM 模型（测试里需要直接创建数据库记录）
from src.service.auth_models import User                         # 用户模型（users 表）
from src.service.db import Base                                  # ORM 基类，含 metadata（所有表定义）
from src.service.db_models_groups import Group, GroupMember      # 分组 + 成员模型
from src.service.db_models_homepage import Project               # 工程模型

# 从待实现的模块导入被测函数（RED 阶段：模块不存在 → ImportError）
from src.service.permission_deps import (
    ROLE_RANK,              # role 等级字典，如 {'reporter': 1, 'maintainer': 2, 'owner': 3}
    resolve_role,           # 计算用户在工程上的最终 role
    list_accessible_projects,  # 列出用户可访问的所有工程（本测试文件暂不覆盖）
    _pick_higher,           # 取两个 role 中权限较高者
)


# ─── Fixture：每个测试独享一个 in-memory DB session ─────────────────────────

@pytest_asyncio.fixture
async def db():
    """提供一个 in-memory aiosqlite 异步 DB session。

    每次 yield 之前都创建全新的内存数据库并初始化所有表，
    yield 之后 session 自动关闭、内存数据库销毁，测试间完全隔离。

    为什么用 aiosqlite：
      - 不需要启动真实 MySQL/PostgreSQL，CI/本地都能直接跑
      - aiosqlite 是 sqlite3 的异步包装，API 与真实 AsyncSession 完全相同
      - sqlite+aiosqlite:///:memory: 是纯内存数据库，每次 fixture 都是全新状态

    create_async_engine(url)：
      - 创建异步数据库引擎（管理连接池）
      - url 格式：<dialect>+<driver>://<connection>

    async with eng.begin() as conn：
      - begin() 打开一个事务上下文（connection-level transaction）
      - conn.run_sync(Base.metadata.create_all)：在异步上下文里运行同步的建表操作
      - Base.metadata.create_all 会创建所有继承自 Base 的 ORM 类对应的表

    async_sessionmaker：
      - SQLAlchemy 2.0 的异步 session 工厂，expire_on_commit=False 防止 commit 后访问属性失效
    """
    # 创建纯内存异步引擎（sqlite+aiosqlite 驱动）
    eng = create_async_engine("sqlite+aiosqlite:///:memory:")

    # 在事务上下文里执行 DDL（建表）
    # run_sync 把同步函数包成协程，在异步 event loop 里执行
    async with eng.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)  # 创建所有表（users / groups / projects 等）

    # 创建 session 工厂（等价于 sessionmaker，但是异步版本）
    # expire_on_commit=False：commit 后不让 ORM 对象过期（允许继续访问属性，不触发额外 SELECT）
    SM = async_sessionmaker(eng, expire_on_commit=False)

    # 打开 session，yield 给测试函数，测试结束后自动关闭
    # async with SM() as s: 等价于 s = SM(); try: ... finally: await s.close()
    async with SM() as s:
        yield s  # yield 把 session 注入到测试函数的 db 参数


# ─── 测试 1：is_admin=True 的用户永远是 owner ────────────────────────────────

@pytest.mark.asyncio  # 告诉 pytest-asyncio 这是一个 async 测试函数，要在 event loop 里跑
async def test_instance_admin_always_owner(db):
    """实例 admin（is_admin=True）在任何工程上的 role 都是 'owner'。

    这是 Instance Admin 权限凌驾于一切的设计：无论 project 成员关系如何，
    is_admin=True 的用户都有最高权限，避免管理员被意外锁在门外。
    """
    # 创建一个 admin 用户（is_admin=True）
    u = User(
        email="a@x.com",
        username="a",
        hashed_password="x",     # 测试里不需要真实加密 hash
        is_admin=True,            # 关键：实例级别管理员
        is_active=True,
    )
    # 创建一个工程，不归属任何 group（group_id=None）
    p = Project(id="p1", name="P1", status="ready", group_id=None)

    # add_all 一次添加多个对象到 session（等价于多次 add，更简洁）
    db.add_all([u, p])
    await db.commit()  # 把对象写入数据库（异步 commit）

    # 断言：admin 用户在工程上的 role 必须是 'owner'
    assert await resolve_role(u, p, db) == "owner"


# ─── 测试 2：无成员关系时返回 None ───────────────────────────────────────────

@pytest.mark.asyncio
async def test_no_membership_returns_none(db):
    """普通用户（is_admin=False）且对工程无任何成员关系时，resolve_role 返回 None。

    None 表示"此用户无权访问该工程"，调用方应据此返回 403。
    """
    # 创建普通用户（is_admin=False，无任何 group/project 成员记录）
    u = User(email="b@x.com", username="b", hashed_password="x", is_admin=False, is_active=True)
    p = Project(id="p1", name="P1", status="ready", group_id=None)
    db.add_all([u, p])
    await db.commit()

    # 断言：无成员关系 → 返回 None（不是 0，不是 ''，是 Python 的 None）
    assert await resolve_role(u, p, db) is None


# ─── 测试 3：group 成员角色继承到 project ────────────────────────────────────

@pytest.mark.asyncio
async def test_group_role_inherits_to_project(db):
    """用户是 group G 的 reporter → project（group_id=G）上也是 reporter。

    设计：工程归属于 group 时，用户在 group 的 role 自动继承到工程。
    这是 RBAC "组继承" 的核心能力：管理一次 group 成员就能覆盖 group 下所有工程。
    """
    # 先创建用户，要 flush 让 DB 分配 u.id，后面 GroupMember 需要用到
    u = User(email="c@x.com", username="c", hashed_password="x", is_admin=False, is_active=True)
    db.add(u)
    # flush 是"半提交"：把 INSERT 发给 DB 以分配 id，但不 commit（事务未结束）
    # 等价于：让 DB 生成 AUTO_INCREMENT id，并更新 Python 对象的 u.id 属性
    await db.flush()

    # 创建 group（created_by_user_id 需要有效的 user.id）
    g = Group(id="g1", name="G1", created_by_user_id=u.id)
    db.add(g)

    # 创建工程，group_id 指向 g1
    p = Project(id="p1", name="P1", status="ready", group_id="g1")
    db.add(p)

    # 将用户加入 group，角色为 reporter
    # GroupMember 的复合主键是 (user_id, group_id)
    db.add(GroupMember(user_id=u.id, group_id="g1", role="reporter", added_by_user_id=u.id))
    await db.commit()

    # 断言：group 成员角色继承到 project → reporter
    assert await resolve_role(u, p, db) == "reporter"


# ─── 测试 4：嵌套 group 继承 ─────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_nested_group_inheritance(db):
    """g_root（owner）→ g_child → project：用户在 g_root 是 owner，project 也是 owner。

    设计：group 继承链最多 3 层深。子孙 group 下的工程可以继承祖先 group 的角色。
    测试场景：
      - 用户是 g_root 的 owner
      - project 归属于 g_child（g_child 的 parent = g_root）
      - 预期：用户通过 g_root → g_child 继承链，在 project 上得到 owner
    """
    u = User(email="d@x.com", username="d", hashed_password="x", is_admin=False, is_active=True)
    db.add(u)
    await db.flush()  # 分配 u.id

    # 创建根 group（无 parent）
    db.add(Group(id="root", name="Root", created_by_user_id=u.id))

    # 创建子 group，parent_group_id 指向 root
    db.add(Group(id="child", name="Child", parent_group_id="root", created_by_user_id=u.id))

    # 用户在 root group 是 owner
    db.add(GroupMember(user_id=u.id, group_id="root", role="owner", added_by_user_id=u.id))

    # 工程归属于 child group（不是直接在 root）
    p = Project(id="p1", name="P1", status="ready", group_id="child")
    db.add(p)
    await db.commit()

    # 断言：通过继承链 child → root，得到 root 的 owner 角色
    assert await resolve_role(u, p, db) == "owner"


# ─── 测试 5：_pick_higher 取两个 role 中较高者 ──────────────────────────────

@pytest.mark.asyncio
async def test_pick_higher_takes_max():
    """_pick_higher(a, b) 返回等级更高的 role；None 表示无 role。

    这是一个纯函数（不依赖 DB），直接调用验证逻辑正确性。

    ROLE_RANK = {'reporter': 1, 'maintainer': 2, 'owner': 3}
    规则：
      - 两者都有 role → 取 ROLE_RANK 值更大的
      - 一方为 None → 取另一方
      - 两者都 None → 返回 None
    """
    # reporter(1) vs maintainer(2) → maintainer 赢
    assert _pick_higher("reporter", "maintainer") == "maintainer"

    # owner(3) vs reporter(1) → owner 赢
    assert _pick_higher("owner", "reporter") == "owner"

    # None vs reporter → reporter（None 表示"无 role"，有 role 总比没有强）
    assert _pick_higher(None, "reporter") == "reporter"

    # owner vs None → owner
    assert _pick_higher("owner", None) == "owner"

    # 两者都 None → None（用户无任何权限）
    # is None 检查：用 is 而不是 ==，因为 None 是单例（Python 惯用法）
    assert _pick_higher(None, None) is None
