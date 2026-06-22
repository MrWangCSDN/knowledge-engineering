"""测试：DELETE /scm/connections/{id} 触发 cache_invalidate，清除该连接的缓存项。

P4b-1 收尾测试：效果断言——缓存中该 connection_id 的所有条目在删除后消失。
"""
# pytest：Python 最流行的测试框架，提供 assert 语句增强、fixture 机制、测试发现等
import pytest
# pytest_asyncio：pytest 的 asyncio 插件，支持 async fixture 和 async test
import pytest_asyncio
# FastAPI：Python 异步 Web 框架，这里用于构建轻量 app 给 TestClient 使用
from fastapi import FastAPI
# TestClient：FastAPI/Starlette 提供的同步测试客户端，内部会运行 async 事件循环
from fastapi.testclient import TestClient
# async_sessionmaker / create_async_engine：SQLAlchemy 异步 ORM 核心工具
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
# Base：所有 ORM 模型的基类，用于 create_all 建表
from src.service.db import Base
# ScmConnection：SCM 连接 ORM 模型
from src.service.db_models_homepage import ScmConnection
# create_scm_routes：scm_router 的注入式工厂函数，返回 APIRouter
from src.service.scm_router import create_scm_routes
# scm_perm_cache 模块：缓存字典 _CACHE、cache_clear、cache_invalidate 均在此
from src.service.scm import scm_perm_cache


class _User:
    """最简用户存根：is_admin=True 让删除操作通过权限检查。"""
    username = "alice"
    is_admin = True


@pytest_asyncio.fixture
async def maker():
    """异步 fixture：创建内存 SQLite 引擎并建表，返回 async_sessionmaker。

    async def fixture 配合 @pytest_asyncio.fixture 才能被 pytest-asyncio 识别；
    普通 @pytest.fixture 不支持 await。
    """
    # create_async_engine：创建异步数据库引擎；":memory:" 表示纯内存 SQLite，测试完自动销毁
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    # async with engine.begin() as c：异步上下文管理器，自动提交/回滚事务
    async with engine.begin() as c:
        # run_sync 在 async 上下文中运行同步函数（create_all 是同步的）
        await c.run_sync(Base.metadata.create_all)
    # async_sessionmaker：异步 session 工厂；expire_on_commit=False 防止访问已提交对象时触发懒加载
    return async_sessionmaker(engine, expire_on_commit=False)


@pytest.mark.asyncio
async def test_delete_connection_invalidates_cache(maker):
    """DELETE /scm/connections/{id} 执行后，_CACHE 中该连接的所有缓存项应被清除。

    测试步骤：
      1. cache_clear() 确保隔离
      2. 手动向 _CACHE 注入一条伪缓存项（模拟 can_query 命中）
      3. 向内存 DB 写入对应的 ScmConnection 记录
      4. 用 TestClient 发起 DELETE 请求，断言返回 204
      5. 断言 _CACHE 中不再有 key[1] == "c1" 的条目
    """
    # 先清空缓存，确保本测试从干净状态开始（防其他用例残留影响）
    scm_perm_cache.cache_clear()

    # 手动植入一条「正向缓存」：key=(user_id=1, connection_id="c1", repo_id=42)
    # value=("can_query", 9e18) 表示角色字符串 + 超大过期时间（不会自然过期）
    # 这模拟 resolve_repo_role_cached 写入的缓存项
    scm_perm_cache._CACHE[(1, "c1", 42)] = ("can_query", 9e18)   # 占位正向缓存项

    # 向内存 DB 写入 ScmConnection，确保删除端点能找到该记录（否则 404）
    async with maker() as s:
        # 构造最简 ScmConnection：id="c1"，其他字段填最小合法值
        s.add(ScmConnection(id="c1", provider="github", auth_type="github_app",
                            github_installation_id=1, account_login="o", status="active", created_by="alice"))
        # await s.commit()：提交事务，将记录持久化到内存 DB
        await s.commit()

    # 构造最简 FastAPI app，挂载 scm_router（注入式，get_provider 传 None 即可，删除不用 provider）
    app = FastAPI()

    # 定义异步 DB 依赖生成器：每次请求产生一个新 session（yield 后自动关闭）
    # async def + yield = 异步生成器，FastAPI 把它当成异步上下文管理器使用
    async def _get_db():
        # async with maker() as s：使用 async_sessionmaker 的上下文管理器语法
        async with maker() as s:
            yield s  # yield 把 session 交给路由函数，with 块退出时自动 close

    # include_router：把工厂生成的路由注册到 app
    # get_current_user=lambda: _User()：每次调用直接返回 admin 用户（跳过 JWT 验证）
    # get_provider=lambda: None：删除操作不调用 provider，传 None 安全
    app.include_router(create_scm_routes(get_current_user=lambda: _User(), get_db=_get_db,
                                         get_provider=lambda: None, app_slug="x"))

    # TestClient：同步测试客户端；with 块自动触发 startup/shutdown 事件
    c = TestClient(app)

    # 发起 DELETE 请求，期望 204 No Content（确认删除接口本身正常）
    assert c.delete("/scm/connections/c1").status_code == 204

    # 效果断言：_CACHE 中不应再有 key[1] == "c1" 的条目
    # any(...) 若可迭代对象中有任意一个 True 就返回 True；取反 = 没有任何匹配 → 缓存已清
    assert not any(k[1] == "c1" for k in scm_perm_cache._CACHE)   # 该连接缓存已清

    # 清理：确保本测试的残留不污染后续用例
    scm_perm_cache.cache_clear()
