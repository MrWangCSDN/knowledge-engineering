import pytest, pytest_asyncio
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from src.service.db import Base
from src.service.db_models_homepage import Project, ScmConnection
from src.service.scm.base import ScmRole
from src.service.scm_binding_router import create_scm_binding_routes


class _User:
    id = 1; username = "alice"; is_admin = True


@pytest_asyncio.fixture
async def maker():
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as c:
        await c.run_sync(Base.metadata.create_all)
    return async_sessionmaker(engine, expire_on_commit=False)


def _app(maker, *, authorize_scm=None):
    app = FastAPI()
    async def _get_db():
        async with maker() as s:
            yield s
    app.include_router(create_scm_binding_routes(
        get_current_user=lambda: _User(), get_db=_get_db,
        require_role=lambda role: (lambda: None), authorize_scm=authorize_scm))
    return app


_BODY = {"connection_id": "c1", "repo_external_id": 42, "repo_full_name": "o/r",
         "ref": "master", "ref_type": "branch", "subpath": None}


async def _seed(maker, *, auth_type="github_app"):
    async with maker() as s:
        s.add(Project(id="p1", name="P1"))
        s.add(ScmConnection(id="c1", provider="github", auth_type=auth_type,
                            github_installation_id=(None if auth_type == "pat" else 7),
                            account_login="o", status="active", created_by="alice"))
        await s.commit()


@pytest.mark.asyncio
async def test_flag_off_no_gate(maker, monkeypatch):
    monkeypatch.delenv("KE_SCM_BIND_AUTHZ", raising=False)
    async with maker() as s:
        s.add(Project(id="p1", name="P1")); await s.commit()   # 无连接也 200（同今天）
    async def _boom(*a, **k): raise AssertionError("gate should not run")
    c = TestClient(_app(maker, authorize_scm=_boom))
    assert c.post("/projects/p1/bind", json=_BODY).status_code == 200


@pytest.mark.asyncio
async def test_flag_on_can_bind_200(maker, monkeypatch):
    monkeypatch.setenv("KE_SCM_BIND_AUTHZ", "1")
    await _seed(maker)
    async def _authz(db, **k): return ScmRole.CAN_BIND
    c = TestClient(_app(maker, authorize_scm=_authz))
    assert c.post("/projects/p1/bind", json=_BODY).status_code == 200


@pytest.mark.asyncio
async def test_flag_on_can_query_403(maker, monkeypatch):
    monkeypatch.setenv("KE_SCM_BIND_AUTHZ", "1")
    await _seed(maker)
    async def _authz(db, **k): return ScmRole.CAN_QUERY
    c = TestClient(_app(maker, authorize_scm=_authz))
    assert c.post("/projects/p1/bind", json=_BODY).status_code == 403


@pytest.mark.asyncio
async def test_flag_on_missing_conn_404(maker, monkeypatch):
    monkeypatch.setenv("KE_SCM_BIND_AUTHZ", "1")
    async with maker() as s:
        s.add(Project(id="p1", name="P1")); await s.commit()   # 无连接行
    async def _authz(db, **k): return ScmRole.CAN_BIND
    c = TestClient(_app(maker, authorize_scm=_authz))
    assert c.post("/projects/p1/bind", json=_BODY).status_code == 404


@pytest.mark.asyncio
async def test_flag_on_pat_skips_gate(maker, monkeypatch):
    monkeypatch.setenv("KE_SCM_BIND_AUTHZ", "1")
    await _seed(maker, auth_type="pat")
    async def _boom(*a, **k): raise AssertionError("PAT should skip SCM gate")
    c = TestClient(_app(maker, authorize_scm=_boom))
    assert c.post("/projects/p1/bind", json=_BODY).status_code == 200


@pytest.mark.asyncio
async def test_flag_on_but_unwired_warns(maker, monkeypatch):
    import logging
    # flag 翻开但工厂没传 authorize_scm（模拟装配漏了）
    monkeypatch.setenv("KE_SCM_BIND_AUTHZ", "1")
    async with maker() as s:
        s.add(Project(id="p1", name="P1")); await s.commit()
    # 直接在 ke.scm.bind logger 上挂 handler 捕获——不依赖 caplog（caplog 受全局 logging 状态污染，整套跑时会空）
    # 注意：某些测试会调用 logging.config.dictConfig(disable_existing_loggers=True)，
    # 导致已注册 logger 的 disabled 标志被置 True，从而根本不执行任何 handler。
    # 必须在挂 handler 的同时强制 re-enable，才能保证跨 suite 顺序的隔离性。
    records: list[str] = []
    class _Capture(logging.Handler):
        def emit(self, rec): records.append(rec.getMessage())
    lg = logging.getLogger("ke.scm.bind")
    handler = _Capture(); handler.setLevel(logging.WARNING)
    lg.addHandler(handler)
    old_level = lg.level
    old_disabled = lg.disabled
    lg.setLevel(logging.WARNING)
    lg.disabled = False          # re-enable：防止 dictConfig disable_existing_loggers 污染
    try:
        c = TestClient(_app(maker, authorize_scm=None))
        r = c.post("/projects/p1/bind", json=_BODY)
    finally:
        lg.removeHandler(handler)
        lg.setLevel(old_level)
        lg.disabled = old_disabled
    assert r.status_code == 200            # 门未生效 → 仍走 KE-RBAC（不 403/500）
    assert any("未接线" in m for m in records)   # 但确实打了 WARNING
