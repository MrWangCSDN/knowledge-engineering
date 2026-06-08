"""POST /projects/{pid}/code/resolve-symbol 路由单测（IDE 化光标解析端点）。

设计 [[代码查看器-IDE化导航-设计]] §4.1。Task 3 / Step 1-2。

照搬 test_code_snippet_endpoint.py 的鉴权 fixture（alice=admin / bob=普通），
monkeypatch resolve_graph_adapter 注入假 adapter（含三原语 + resolve_first）。
"""
import pytest                   # 测试框架
import pytest_asyncio            # async fixture 支持
from fastapi import FastAPI      # 测试用 app
from fastapi.testclient import TestClient  # 同步 HTTP 测试客户端
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

# 鉴权
from src.service import auth_security as sec
from src.service.auth_models import User
from src.service.auth_router import router as auth_router

# DB
from src.service.db import Base, get_db
from src.service.db_models_homepage import Project as ProjectModel

# 路由
from src.service.project_router import router as project_router
from src.service.code_router import router as code_router


# ───────── 鉴权 + DB fixture（照搬 test_code_snippet_endpoint.py 约定） ─────────

@pytest_asyncio.fixture
async def session_maker(monkeypatch):
    """内存 SQLite，seed alice(admin) + bob(普通用户)。"""
    monkeypatch.setenv("KE_JWT_SECRET", "x" * 32)        # JWT 密钥（测试用）
    monkeypatch.setenv("KE_COOKIE_SECURE", "false")       # 关 HTTPS-only cookie

    eng = create_async_engine("sqlite+aiosqlite:///:memory:", echo=False)
    async with eng.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)

    SM = async_sessionmaker(eng, expire_on_commit=False)
    async with SM() as s:
        # alice：admin → require_project_role 自动通过
        s.add(User(email="alice@x.com", username="alice",
                   hashed_password=sec.hash_password("12345678"),
                   is_active=True, is_admin=True))
        # bob：普通用户 → 403
        s.add(User(email="bob@x.com", username="bob",
                   hashed_password=sec.hash_password("12345678"),
                   is_active=True, is_admin=False))
        await s.commit()
    return SM


@pytest.fixture
def client(session_maker):
    """构造测试 app + TestClient，注入 DB override。"""
    app = FastAPI()
    app.include_router(auth_router)
    app.include_router(project_router)
    app.include_router(code_router)

    async def override_db():
        async with session_maker() as s:
            yield s
            await s.commit()

    app.dependency_overrides[get_db] = override_db
    return TestClient(app)


def _login(client: TestClient, username: str = "alice") -> str:
    """登录并返回 JWT access_token。"""
    r = client.post("/auth/login", json={
        "username": username, "password": "12345678", "remember_me": False
    })
    assert r.status_code == 200, f"login failed: {r.text}"
    return r.json()["access_token"]


def _auth(token: str) -> dict:
    """构造 Authorization header dict。"""
    return {"Authorization": f"Bearer {token}"}


# ───────── 假 adapter（覆盖 resolve_symbol_at 需要的所有原语） ─────────

class _FakeNode:
    """模拟 CgNode（含 resolve_first 路径需要的字段）。"""
    qualified_name = "Bar::callsFoo"  # 全限定名
    kind = "method"                    # 节点类型
    file_path = "src/Bar.java"        # 相对 repo 根的路径
    start_line = 10                    # 起始行
    end_line = 20                      # 结束行
    signature = "()"                   # 签名（hover 可用，但本测试 want_doc=False 不取）


class _HitAdapter:
    """位置级命中：resolve_at_position 返 'Foo'，resolve_first 返 _FakeNode。

    duck typing：Python 不强求显式接口，方法签名一致即可被 resolve_symbol_at 调用。
    """
    def resolve_at_position(self, file_path, line, col):
        """位置 → durable_key，本假适配器固定返 'Foo'。"""
        return "Foo"

    def find_by_name(self, token, limit=10):
        """名字回退：返空列表（位置已命中，本路径不走名字级）。"""
        return []

    def resolve_impl(self, entity_id):
        """非接口方法 → None（不改写）。"""
        return None

    def resolve_first(self, entity_id):
        """元数据：返 _FakeNode（kind / file_path / signature）。"""
        return _FakeNode()


class _MissAdapter:
    """三级全落空：所有原语都返回空/None。"""
    def resolve_at_position(self, file_path, line, col):
        return None

    def find_by_name(self, token, limit=10):
        return []

    def resolve_impl(self, entity_id):
        return None

    def resolve_first(self, entity_id):
        return None


# ───────── 工程种子 fixture ─────────

@pytest_asyncio.fixture
async def project_with_repo(session_maker, tmp_path):
    """创建一个带 repo_local_path 的工程；造源文件让 has_source 命中。

    Returns:
        (project_id, repo_path)
    """
    repo_path = str(tmp_path)
    src_dir = tmp_path / "src"          # 源码子目录
    src_dir.mkdir(parents=True)         # 递归建目录
    # 写入文件让 os.path.exists(repo + 'src/Bar.java') 命中
    (src_dir / "Bar.java").write_text("line1\nline2\n", encoding="utf-8")

    async with session_maker() as s:
        s.add(ProjectModel(id="proj_resolve", name="resolve 测试工程",
                           status="ready", repo_local_path=repo_path))
        await s.commit()
    return "proj_resolve", repo_path


# ───────── 测试用例 ─────────

def test_resolve_position_hit(client, project_with_repo, monkeypatch):
    """位置命中 → 200 + {entity_id, has_source, kind}。"""
    pid, _ = project_with_repo
    # monkeypatch resolve_graph_adapter 返回命中型假适配器
    monkeypatch.setattr(
        "src.service.code_router.resolve_graph_adapter",
        lambda repo_local_path: _HitAdapter(),
    )

    token = _login(client)
    resp = client.post(
        f"/projects/{pid}/code/resolve-symbol",
        json={"file_path": "src/Bar.java", "line": 15, "col": 8},
        headers=_auth(token),
    )

    assert resp.status_code == 200, f"expected 200, got {resp.status_code}: {resp.text}"
    body = resp.json()
    assert body is not None                   # 命中 → 非 null
    assert body["entity_id"] == "Foo"
    assert body["has_source"] is True         # tmp_path/src/Bar.java 文件存在
    assert body["kind"] == "method"


def test_resolve_miss_returns_null(client, project_with_repo, monkeypatch):
    """三级全落空 → 200 + null（前端据此静默或显示"暂无源码"）。"""
    pid, _ = project_with_repo
    monkeypatch.setattr(
        "src.service.code_router.resolve_graph_adapter",
        lambda repo_local_path: _MissAdapter(),
    )

    token = _login(client)
    resp = client.post(
        f"/projects/{pid}/code/resolve-symbol",
        # 提供位置 + token，但 _MissAdapter 三级都返空
        json={"file_path": "src/Bar.java", "line": 15, "col": 8, "token": "Ghost"},
        headers=_auth(token),
    )

    assert resp.status_code == 200, f"expected 200, got {resp.status_code}"
    # 注意 resp.json() 返回 None（HTTP 200 + null body）
    assert resp.json() is None


def test_resolve_missing_file_path_returns_422(client, project_with_repo, monkeypatch):
    """缺 file_path → Pydantic 校验 → 422 Unprocessable Entity。"""
    pid, _ = project_with_repo
    monkeypatch.setattr(
        "src.service.code_router.resolve_graph_adapter",
        lambda repo_local_path: _HitAdapter(),
    )

    token = _login(client)
    resp = client.post(
        f"/projects/{pid}/code/resolve-symbol",
        json={"line": 1, "col": 0},          # 缺 file_path（必填）
        headers=_auth(token),
    )

    # Pydantic 字段缺失 → FastAPI 自动返 422 + 错误详情
    assert resp.status_code == 422, f"expected 422, got {resp.status_code}: {resp.text}"


def test_resolve_requires_reporter(client, project_with_repo, monkeypatch):
    """无 reporter 权限的 bob → 403（require_project_role 拦截）。"""
    pid, _ = project_with_repo
    monkeypatch.setattr(
        "src.service.code_router.resolve_graph_adapter",
        lambda repo_local_path: _HitAdapter(),
    )

    token = _login(client, username="bob")
    resp = client.post(
        f"/projects/{pid}/code/resolve-symbol",
        json={"file_path": "src/Bar.java", "line": 1, "col": 0},
        headers=_auth(token),
    )

    assert resp.status_code == 403, f"expected 403, got {resp.status_code}: {resp.text}"


def test_resolve_requires_auth(client, project_with_repo):
    """未带 Authorization → 401。"""
    pid, _ = project_with_repo
    resp = client.post(
        f"/projects/{pid}/code/resolve-symbol",
        json={"file_path": "src/Bar.java", "line": 1, "col": 0},
    )
    assert resp.status_code == 401, f"expected 401, got {resp.status_code}: {resp.text}"
