import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from src.service.scm_router import create_scm_routes


class _User:
    username = "alice"; is_admin = True


def _app(provider=None):
    app = FastAPI()
    app.include_router(create_scm_routes(
        get_current_user=lambda: _User(),
        get_db=None,
        get_provider=lambda: provider,
        app_slug="ke-test-app",
    ))
    return app


def test_install_url():
    c = TestClient(_app())
    r = c.get("/scm/github/install-url")
    assert r.status_code == 200
    body = r.json()
    assert "github.com/apps/ke-test-app/installations/new" in body["install_url"]
    assert body["state"]
