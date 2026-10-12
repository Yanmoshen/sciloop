"""Serve the built frontend and API on one port, including SPA navigation."""

import json
import re
from email.utils import formatdate
from pathlib import Path

from fastapi import FastAPI, Request
from fastapi.responses import FileResponse, JSONResponse
from starlette.exceptions import HTTPException
from starlette.responses import Response
from starlette.staticfiles import StaticFiles

#: 作品介绍页（控制台之前的那一页）：源文件在 ``web/public/`` 下。
#: 之所以从**源码目录**而不是 ``web/dist`` 读：这样宿主部署**不需要前端构建**就能访问
#: ``/welcome``（dist 只在构建后才会有 welcome.html，那时按兜底路径使用）。
WELCOME_RELATIVE = Path("web") / "public" / "welcome.html"

#: 介绍页要被反复改：让浏览器每次都回源校验（拿到 304 就走缓存），改完刷新即可见。
WELCOME_CACHE_CONTROL = "no-cache, must-revalidate"


def welcome_candidates(directory: Path | None = None) -> list[Path]:
    """按优先级给出候选路径：仓库源文件 → 构建产物。"""
    root = Path(__file__).resolve().parents[2]
    built = Path(directory) / "welcome.html" if directory is not None else root / "web" / "dist" / "welcome.html"
    return [root / WELCOME_RELATIVE, built]


def welcome_file(directory: Path | None = None) -> Path | None:
    for candidate in welcome_candidates(directory):
        if candidate.is_file():
            return candidate
    return None


def _register_welcome(app: FastAPI, directory: Path | None) -> None:
    """注册 ``/welcome``（含尾斜杠）。**先于 SPA 挂载注册**，否则会被 catch-all 吃掉。"""

    @app.get("/welcome", include_in_schema=False)
    @app.get("/welcome/", include_in_schema=False)
    async def welcome(request: Request) -> Response:
        path = welcome_file(directory)
        if path is None:
            return JSONResponse(
                {"code": "welcome_not_found", "message": "作品介绍页文件缺失"},
                status_code=404,
            )
        # 介绍页会被反复改：用「no-cache + ETag」让浏览器每次回源校验——
        # 没改则 304 省流量，改了立刻可见（不需要 Ctrl+F5 硬刷新）。
        # 注：Starlette 的 FileResponse 不处理条件请求，所以这里自己比 If-None-Match。
        stat = path.stat()
        etag = f'"{stat.st_mtime_ns:x}-{stat.st_size:x}"'
        headers = {
            "Cache-Control": WELCOME_CACHE_CONTROL,
            "ETag": etag,
            "Last-Modified": formatdate(stat.st_mtime, usegmt=True),
        }
        if request.headers.get("if-none-match") == etag:
            return Response(status_code=304, headers=headers)
        return FileResponse(path, media_type="text/html; charset=utf-8", headers=headers)


class FrontendFiles(StaticFiles):
    async def get_response(self, path: str, scope):
        path = path.replace("\\", "/")
        # Unknown API routes must keep their real 404, never return the SPA.
        if path.split("/", 1)[0] in {"api", "docs", "redoc", "openapi.json"}:
            raise HTTPException(404)
        try:
            return await super().get_response(path, scope)
        except HTTPException as exc:
            headers = dict(scope.get("headers", []))
            if (
                exc.status_code != 404
                or scope["method"] not in {"GET", "HEAD"}
                or ".." in Path(path).parts
                or Path(path).suffix
                or b"text/html" not in headers.get(b"accept", b"")
            ):
                raise
            return await super().get_response("index.html", scope)


def install(app: FastAPI, *, directory: Path | None = None) -> bool:
    directory = directory or Path(__file__).resolve().parents[2] / "web" / "dist"
    # 介绍页与构建产物无关：先注册，保证没构建过前端也能打开 /welcome。
    _register_welcome(app, directory)
    index = directory / "index.html"
    manifest_path = directory / "build-manifest.json"
    if not index.is_file() or not manifest_path.is_file():
        return False
    try:
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        assets = set(manifest.get("assets") or [])
        if not manifest.get("build_id") or not assets:
            return False
        html = index.read_text(encoding="utf-8")
        referenced = set(re.findall(r'(?:src|href)="/([^"?#]+)', html))
        if any(
            asset not in assets or not (directory / asset).is_file()
            for asset in referenced
            if asset.startswith("assets/")
        ):
            return False
    except (OSError, ValueError, TypeError):
        return False
    app.mount("/", FrontendFiles(directory=str(directory), html=True), name="frontend")
    return True
