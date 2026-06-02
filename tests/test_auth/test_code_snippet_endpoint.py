# tests/test_auth/test_code_snippet_endpoint.py
"""验证 GET /projects/{pid}/code-snippet 路由。

核心断言：
  - happy path：entity 存在 + 源码可读 → 200 + 完整 JSON 结构
  - entity 不存在（adapter.resolve_first 返 None）→ 404
  - 工程未配 repo_local_path → 404
  - 无 reporter 权限的普通用户（bob）→ 403

鉴权 fixture 照搬 test_qa_session_router.py 的约定：
  - session_maker（pytest_asyncio.fixture）：内存 SQLite，seed alice(admin) + bob(non-admin)
  - alice 是 is_admin=True → require_project_role 自动通过（owner 权限）
  - bob 无任何 user_project_access → 403

Python 语法提示：
  - monkeypatch.setattr("模块.属性名", 替换值)：在测试期间临时替换模块级变量
  - pytest_asyncio.fixture：async fixture 必须用这个装饰器（不能用普通 pytest.fixture + async）
"""
import pytest                   # pytest：Python 最流行的测试框架，提供 fixture / assert 等工具
import pytest_asyncio            # pytest_asyncio：pytest 对 asyncio 的扩展，支持 async fixture
from fastapi import FastAPI      # FastAPI：用于构造测试用的 app 实例
from fastapi.testclient import TestClient  # TestClient：同步 HTTP 测试客户端（封装 httpx）
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine  # SQLAlchemy 异步引擎

# 鉴权/安全模块
from src.service import auth_security as sec   # hash_password + decode_token 等工具函数
from src.service.auth_models import User         # 用户 ORM 模型
from src.service.auth_router import router as auth_router  # /auth/* 路由（login/me）

# 数据库 + ORM 模型
from src.service.db import Base, get_db          # Base：所有 ORM 的元数据基类；get_db：FastAPI 依赖
from src.service.db_models_homepage import (
    Project as ProjectModel,       # 工程 ORM 模型（projects 表）
    UserProjectAccess,             # 成员权限表（user_project_access 表）
)

# 路由
from src.service.project_router import router as project_router   # /projects/* 路由
from src.service.code_router import router as code_router          # 待测路由（GET /projects/{pid}/code-snippet）


# ───────── fixtures（照搬 test_qa_session_router.py 约定）─────────

@pytest_asyncio.fixture
async def session_maker(monkeypatch):
    """内存 SQLite DB，seed alice(admin) + bob(普通用户)。

    monkeypatch.setenv 设置 JWT_SECRET，让 auth 模块在测试期有合法配置。
    create_async_engine("sqlite+aiosqlite:///:memory:")：内存 SQLite，测试结束即销毁。
    """
    # 设置环境变量，auth_security 模块初始化 JWT 用
    monkeypatch.setenv("KE_JWT_SECRET", "x" * 32)   # JWT 密钥（测试用，32 字节随机串）
    monkeypatch.setenv("KE_COOKIE_SECURE", "false")  # 测试环境关 HTTPS-only cookie

    # 创建内存 SQLite 异步引擎
    eng = create_async_engine("sqlite+aiosqlite:///:memory:", echo=False)

    # 创建所有表（同步接口在 async 上下文中用 run_sync 包装）
    async with eng.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)

    # async_sessionmaker：生成 AsyncSession 的工厂（类似 sessionmaker 但异步版本）
    SM = async_sessionmaker(eng, expire_on_commit=False)

    # 写入种子数据：alice（admin）+ bob（普通用户）
    async with SM() as s:
        # alice：is_admin=True，require_project_role 对 admin 自动放行为 owner
        s.add(User(email="alice@x.com", username="alice",
                   hashed_password=sec.hash_password("12345678"),
                   is_active=True, is_admin=True))
        # bob：is_admin=False，无任何 user_project_access 记录 → 会被 403 拒绝
        s.add(User(email="bob@x.com", username="bob",
                   hashed_password=sec.hash_password("12345678"),
                   is_active=True, is_admin=False))
        await s.commit()

    # yield 把 session_maker 工厂返回给测试，测试完成后引擎自动关闭（无需显式 cleanup）
    return SM


@pytest.fixture
def client(session_maker):
    """构造测试 app + TestClient，注入 DB override。

    只挂 auth_router + project_router + code_router（最小依赖，隔离测试）。
    不挂 require_infra_healthy（code_router 设计上不依赖 infra）。
    """
    # 构造最小 FastAPI app（只含测试所需路由，避免 import 副作用）
    app = FastAPI()
    app.include_router(auth_router)       # /auth/login、/auth/me
    app.include_router(project_router)    # /projects/*（require_project_role 需要）
    app.include_router(code_router)       # GET /projects/{pid}/code-snippet（待测）

    # override get_db：把真实 DB session 替换为内存 SQLite session
    # async def override_db()：这是一个异步生成器（有 yield），用 yield 分隔 setup/teardown
    async def override_db():
        async with session_maker() as s:
            yield s           # 把 session 注入到路由函数
            await s.commit()  # 测试结束后提交（避免 session 脏读）

    # dependency_overrides：FastAPI 的依赖替换机制（测试专用）
    app.dependency_overrides[get_db] = override_db

    # TestClient(app)：同步 HTTP 测试客户端，可在同步测试函数中直接调用 .get/.post
    return TestClient(app)


def _login(client: TestClient, username: str = "alice") -> str:
    """登录并返回 JWT access_token。

    Args:
        client:   TestClient 实例
        username: 登录用的用户名，默认 alice

    Returns:
        JWT access_token 字符串（用于 Authorization header）
    """
    # POST /auth/login：发送用户名 + 密码，获取 JWT
    r = client.post("/auth/login", json={
        "username": username, "password": "12345678", "remember_me": False
    })
    # assert 断言：若条件为 False 则测试立即失败（pytest 会给出清晰的 diff 提示）
    assert r.status_code == 200, f"login failed: {r.text}"
    return r.json()["access_token"]


def _auth(token: str) -> dict:
    """构造 Authorization header dict，供 TestClient 请求使用。

    Args:
        token: JWT access_token

    Returns:
        {"Authorization": "Bearer <token>"} 格式的 dict
    """
    # f-string：Python 3.6+ 的字符串格式化，{变量} 直接嵌入值
    return {"Authorization": f"Bearer {token}"}


# ───────── 假图适配器（fixture helper）─────────

class _FakeNode:
    """模拟 CgNode（CodeGraph 节点）——只需要 resolve_first 返回的字段。

    build_snippet_response 会访问：
      node.file_path / node.start_line / node.end_line
      node.qualified_name / node.kind
    """
    qualified_name = "OmsPortalOrderController::confirmReceiveOrder"  # 全限定名
    kind = "method"                # 实体类型
    file_path = "src/Ctrl.java"   # 相对于 repo_local_path 的文件路径（浅层，不会越界）
    start_line = 1                 # 起始行（1-indexed）
    end_line = 2                   # 结束行（含）


class _FakeAdapter:
    """模拟 CodeGraphGraphAdapter，提供 resolve_first / successors_with_locations / callers。

    duck typing：Python 不要求显式继承接口，只要有相应方法即可当作"适配器"使用。
    """
    def __init__(self, node):
        # 保存节点，resolve_first 返回它（或 None，如 _NullAdapter）
        self._node = node

    def resolve_first(self, entity_id: str):
        """按 entity_id 查节点，返回节点（存在）或 None（不存在）。"""
        return self._node  # 总是返回预设节点

    def successors_with_locations(self, entity_id: str) -> list[dict]:
        """返回 callees 列表（带调用点 line/col）。"""
        # 固定列表，让 happy path 断言有数据可验
        return [{"entity_id": "OmsService::confirm#(Long)", "name": "confirm", "line": 1, "col": 4}]

    def callers(self, entity_id: str) -> list[dict]:
        """返回 callers 列表（谁调用了当前实体）。"""
        return [{"entity_id": "FooCtrl::bar#()", "name": "bar"}]


class _NullAdapter(_FakeAdapter):
    """resolve_first 返 None，模拟"实体不在 CodeGraph 中"。"""
    def __init__(self):
        # 不需要 node，直接设 None
        self._node = None  # resolve_first 会返回 None


# ───────── 工程种子 fixture ─────────

@pytest_asyncio.fixture
async def project_with_repo(session_maker, tmp_path):
    """创建一个带 repo_local_path 的工程，并在 tmp_path 下建一个真实源码文件。

    Returns:
        (project_id, repo_path)
    """
    # tmp_path 是 pytest 提供的临时目录（每次测试独立，测试结束自动清理）
    # 在 tmp_path 下创建源码文件，供 build_snippet_response 读取
    repo_path = str(tmp_path)                      # repo_local_path 的值（绝对路径）
    src_dir = tmp_path / "src"                     # 源码子目录（与 _FakeNode.file_path 对应）
    src_dir.mkdir(parents=True)                    # mkdir(parents=True) 递归创建父目录
    # 写入两行 Java 代码（start_line=1, end_line=2 → read_snippet 读这两行）
    (src_dir / "Ctrl.java").write_text("line1\nline2\n", encoding="utf-8")

    # 在数据库里创建工程记录
    async with session_maker() as s:
        # repo_local_path 指向 tmp_path（build_snippet_response 会从这里读文件）
        s.add(ProjectModel(id="proj_with_repo", name="有源码工程", status="ready",
                           repo_local_path=repo_path))
        await s.commit()

    # 返回 (project_id, repo_path) 元组（测试可按需解包）
    return "proj_with_repo", repo_path


@pytest_asyncio.fixture
async def project_without_repo(session_maker):
    """创建一个 repo_local_path=None 的工程（未配置源码路径）。

    Returns:
        project_id
    """
    async with session_maker() as s:
        # repo_local_path 不传 → 默认 None（ORM 的 Optional[str] 字段）
        s.add(ProjectModel(id="proj_no_repo", name="无源码工程", status="ready"))
        await s.commit()
    return "proj_no_repo"


# ───────── 测试用例 ─────────

def test_happy_path(client, project_with_repo, monkeypatch):
    """happy path：entity 存在 + 源码可读 → 200 + 完整 JSON 结构。

    monkeypatch src.service.code_router.resolve_graph_adapter，返回 _FakeAdapter。
    源码文件在 tmp_path 下真实存在（project_with_repo fixture 已创建）。
    """
    # 解包 fixture 返回的元组：(project_id, repo_path)
    pid, _ = project_with_repo

    # monkeypatch.setattr("模块路径.属性", 替换函数)：
    # 测试期间把 code_router 里的 resolve_graph_adapter 替换为我们的 lambda
    # lambda：匿名函数，lambda 参数: 返回值；这里忽略 repo_local_path 参数，直接返回假适配器
    monkeypatch.setattr(
        "src.service.code_router.resolve_graph_adapter",
        lambda repo_local_path: _FakeAdapter(_FakeNode()),  # 不论路径，总返回有效 adapter
    )

    # 登录 alice（admin → require_project_role 自动通过）
    token = _login(client)

    # 发出 GET 请求，params 传 entity_id（FastAPI Query 参数）
    resp = client.get(
        f"/projects/{pid}/code-snippet",
        params={"entity_id": "OmsPortalOrderController::confirmReceiveOrder#(Long)"},
        headers=_auth(token),
    )

    # 断言状态码 200
    assert resp.status_code == 200, f"expected 200, got {resp.status_code}: {resp.text}"

    body = resp.json()
    # set(body) >= {...}：断言响应 JSON 包含所有必须字段（>= 是超集检查，允许多余字段）
    required_keys = {
        "entity_id", "qualified_name", "kind",
        "file_path", "language", "start_line", "end_line",
        "code", "callees", "callers",
    }
    assert set(body) >= required_keys, f"缺少字段: {required_keys - set(body)}"

    # 验证语言（.java → "java"）
    assert body["language"] == "java"
    # 验证代码非空（tmp_path 下文件有两行内容）
    assert body["code"] != ""
    # 验证 callees / callers 是列表
    assert isinstance(body["callees"], list)
    assert isinstance(body["callers"], list)


def test_entity_not_found(client, project_with_repo, monkeypatch):
    """entity 不存在（adapter.resolve_first 返 None）→ 404。

    _NullAdapter.resolve_first 返 None → build_snippet_response 返 None → 路由层 404。
    """
    pid, _ = project_with_repo

    # 替换为 _NullAdapter（resolve_first 总返回 None）
    monkeypatch.setattr(
        "src.service.code_router.resolve_graph_adapter",
        lambda repo_local_path: _NullAdapter(),
    )

    token = _login(client)
    resp = client.get(
        f"/projects/{pid}/code-snippet",
        params={"entity_id": "Ghost::nonexistent#()"},
        headers=_auth(token),
    )

    # 实体不在 CodeGraph → build_snippet_response 返 None → 路由返 404
    assert resp.status_code == 404, f"expected 404, got {resp.status_code}: {resp.text}"


def test_project_without_repo_path(client, project_without_repo, monkeypatch):
    """工程 repo_local_path=None → 404（工程未配置源码路径）。

    路由层在调 build_snippet_response 之前先判 repo_local_path，
    为 None 时直接返 404（不需要进图适配器）。
    """
    # 即使 monkeypatch 了 adapter，路由也应在 p.repo_local_path 为 None 时提前 404
    monkeypatch.setattr(
        "src.service.code_router.resolve_graph_adapter",
        lambda repo_local_path: _FakeAdapter(_FakeNode()),
    )

    token = _login(client)
    resp = client.get(
        f"/projects/{project_without_repo}/code-snippet",
        params={"entity_id": "X::y#()"},
        headers=_auth(token),
    )

    # 工程无 repo_local_path → 路由层提前 404（设计约束：不进图、不读文件）
    assert resp.status_code == 404, f"expected 404, got {resp.status_code}: {resp.text}"


def test_project_not_exist(client, monkeypatch):
    """工程不存在 → 404（require_project_role checker 先于路由函数执行，返 404）。

    require_project_role 内部调 db.get(Project, project_id)，
    project 不存在 → HTTPException(404, "工程不存在")。
    """
    # 不需要 monkeypatch adapter，工程不存在 → 连路由函数都进不去
    token = _login(client)
    resp = client.get(
        "/projects/ghost_project/code-snippet",
        params={"entity_id": "X::y#()"},
        headers=_auth(token),
    )

    # require_project_role 先执行：工程不存在 → 404
    assert resp.status_code == 404, f"expected 404, got {resp.status_code}: {resp.text}"


def test_no_reporter_permission(client, project_with_repo, monkeypatch):
    """无 reporter 权限的普通用户（bob）→ 403。

    bob：is_admin=False，且无 user_project_access 记录。
    require_project_role("reporter") 发现 role=None → HTTPException(403)。
    """
    pid, _ = project_with_repo

    # monkeypatch adapter（虽然 403 发生在路由体外，但保持一致避免误导）
    monkeypatch.setattr(
        "src.service.code_router.resolve_graph_adapter",
        lambda repo_local_path: _FakeAdapter(_FakeNode()),
    )

    # 以 bob 身份登录（非 admin，无 project 成员关系）
    token = _login(client, username="bob")
    resp = client.get(
        f"/projects/{pid}/code-snippet",
        params={"entity_id": "X::y#()"},
        headers=_auth(token),
    )

    # require_project_role：bob 无权限 → 403
    assert resp.status_code == 403, f"expected 403, got {resp.status_code}: {resp.text}"


def test_requires_auth(client, project_with_repo):
    """未携带 Authorization header → 401。

    get_current_user dependency 要求合法 JWT，缺失时返 401。
    """
    pid, _ = project_with_repo
    # 不传 headers（无 Bearer token）
    resp = client.get(
        f"/projects/{pid}/code-snippet",
        params={"entity_id": "X::y#()"},
    )
    # 无 token → get_current_user 返 401
    assert resp.status_code == 401, f"expected 401, got {resp.status_code}: {resp.text}"
