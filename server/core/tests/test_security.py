"""管理员测试面与公开只读面的 HTTP 权限回归。"""

import pytest
from fastapi import Depends, FastAPI, Request
from fastapi.testclient import TestClient

from core import security
from core.config import Settings
from services.demo.access import install_access_guard


@pytest.mark.parametrize(
    ("mode", "provided", "expected"),
    [
        ("owner_mode", None, 200),
        ("owner_mode", "invalid", 200),
        ("public_demo", None, 403),
        ("public_demo", "invalid", 403),
        ("public_demo", "test-owner-key", 200),
    ],
)
def test_owner_permissions_match_write_guard(monkeypatch, mode, provided, expected):
    from services.demo import state

    settings = Settings(_env_file=None, app_access_mode=mode, owner_token="test-owner-key")
    monkeypatch.setattr(security, "get_settings", lambda: settings)
    monkeypatch.setattr(state, "resolve_access_mode", lambda: mode)
    app = FastAPI()

    @app.get("/api/permissions")
    def permissions(request: Request):
        return {"is_owner": security.is_owner(request)}

    @app.post("/api/write", dependencies=[Depends(security.require_owner)])
    def write():
        return {"ok": True}

    install_access_guard(app)
    headers = {security.OWNER_HEADER: provided} if provided else {}
    with TestClient(app) as client:
        assert client.get("/api/permissions", headers=headers).json()["is_owner"] is (expected == 200)
        assert client.post("/api/write", headers=headers).status_code == expected
