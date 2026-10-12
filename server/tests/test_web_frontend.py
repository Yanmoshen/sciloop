import json
from pathlib import Path

from fastapi import FastAPI
from fastapi.testclient import TestClient

from services.web_frontend import install


def test_frontend_deep_links_assets_and_api_404(tmp_path):
    (tmp_path / "index.html").write_text(
        '<html><script src="/assets/main.js"></script></html>', encoding="utf-8"
    )
    (tmp_path / "assets").mkdir()
    (tmp_path / "assets" / "main.js").write_text("console.log('ok')", encoding="utf-8")
    (tmp_path / "build-manifest.json").write_text(
        json.dumps({"build_id": "test-build", "assets": ["index.html", "assets/main.js"]}),
        encoding="utf-8",
    )
    app = FastAPI()

    @app.get("/api/health")
    def health():
        return {"ok": True}

    assert install(app, directory=tmp_path)
    with TestClient(app) as client:
        assert "/assets/main.js" in client.get("/").text
        assert client.get("/c/a-conversation", headers={"accept": "text/html"}).status_code == 200
        assert client.get("/assets/main.js").status_code == 200
        assert client.get("/assets/missing.js", headers={"accept": "text/html"}).status_code == 404
        assert client.get("/api/health").json() == {"ok": True}
        assert client.get("/api/missing", headers={"accept": "text/html"}).status_code == 404
        assert client.get("/unknown", headers={"accept": "application/json"}).status_code == 404
        assert client.post("/c/a-conversation").status_code == 405


def test_missing_build_does_not_install_frontend(tmp_path):
    app = FastAPI()
    assert not install(app, directory=tmp_path)


def test_stale_or_incomplete_build_manifest_is_rejected(tmp_path):
    (tmp_path / "index.html").write_text(
        '<script src="/assets/missing.js"></script>', encoding="utf-8"
    )
    (tmp_path / "build-manifest.json").write_text(
        json.dumps({"build_id": "test-build", "assets": ["index.html"]}),
        encoding="utf-8",
    )
    app = FastAPI()
    assert not install(app, directory=tmp_path)


def test_welcome_page_served_from_source_without_build(tmp_path):
    """作品介绍页 /welcome 走仓库源文件（web/public/welcome.html），**不需要前端构建**。"""

    app = FastAPI()
    # tmp_path 里没有 index.html/build-manifest.json → SPA 不挂载，但介绍页仍应可用
    assert not install(app, directory=tmp_path)
    with TestClient(app) as client:
        resp = client.get("/welcome")
        assert resp.status_code == 200, resp.text[:200]
        assert resp.headers["content-type"].startswith("text/html")
        assert resp.headers["cache-control"] == "no-cache, must-revalidate"
        assert "SciLoop" in resp.text
        # 带 ETag 回源校验必须 304（改完刷新即可见、没改走缓存）
        etag = resp.headers.get("etag")
        assert etag
        cached = client.get("/welcome", headers={"if-none-match": etag})
        assert cached.status_code == 304
        # 尾斜杠同样可用，且 SPA catch-all 不会把它吃掉
        assert client.get("/welcome/").status_code == 200


def test_welcome_page_missing_file_returns_404(tmp_path, monkeypatch):
    """源文件与构建产物都不在时，/welcome 明确 404（不静默返回 SPA）。"""

    from services import web_frontend

    monkeypatch.setattr(web_frontend, "WELCOME_RELATIVE", Path("web/public/__missing__.html"))
    app = FastAPI()
    install(app, directory=tmp_path)
    with TestClient(app) as client:
        resp = client.get("/welcome")
        assert resp.status_code == 404
        assert resp.json()["code"] == "welcome_not_found"
