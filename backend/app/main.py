# Copyright 2026 SciLoop contributors
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
"""SciLoop FastAPI 应用入口。

路由注册顺序（**硬约束，见 WP04 的集成请求**）
--------------------------------------------
``GET /papers/feed`` 与 ``GET /papers/{id}`` 前缀冲突，FastAPI 按**注册顺序**匹配：
先注册 feed（字面路径），再注册 papers（含路径参数），否则 ``/papers/feed`` 会被
``/papers/{id}`` 抢占并在 int 解析失败时返回 422。

其他工作包的路由以「可选模块」方式挂载（模块不存在则跳过并记日志），
保证并行开发期 main.py 不需要被反复修改。
"""

from __future__ import annotations

import importlib
import logging
import time
from contextlib import asynccontextmanager
from datetime import UTC, datetime
from typing import Any

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from starlette.exceptions import HTTPException as StarletteHTTPException

from app.core.config import get_settings
from app.db import session as db_session
from app.tasks import scheduler as job_scheduler

APP_NAME = "SciLoop API"
APP_VERSION = "0.1.0"

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s %(message)s",
)
logger = logging.getLogger("sciloop.main")

settings = get_settings()

# --------------------------------------------------------------------------- #
# 路由挂载表：**顺序即匹配优先级**，严禁把 feed 放到 papers 之后
# --------------------------------------------------------------------------- #
# (模块路径, 前缀, 是否必需)
ROUTER_REGISTRY: tuple[tuple[str, str, bool], ...] = (
    # 1) 论文库 —— 必须最先注册，保证 /papers/feed 不被 /papers/{id} 抢占
    ("app.api.v1.feed", "/api/v1", True),
    # 2) 论文导入（本工作包）：/papers/import、/papers/import/identifiers、
    #    /papers/import-jobs/{task_id}、/papers/imports。
    #    其中字面路径 **/papers/imports** 必须早于 papers 的 /papers/{paper_id} 注册，
    #    否则会命中参数路由并因 int 解析失败返回 422（loc=['path','paper_id']，
    #    type=int_parsing）——与下面 parses 的 /papers/card-jobs 属同一类缺陷。
    #    因此本条**插在 feed 之后**：所有既有条目的相对顺序未变、feed 仍是第一条。
    ("app.api.v1.imports", "/api/v1", False),
    # 2.1) EasyPaper 风格核心模块（本轮新增，全部可选）：
    #      /translate/*（论文翻译） /reader/*（全文阅读器） /exports/*（多格式导出）。
    #      三者一律使用**顶层字面前缀**，不挂在 /papers/ 下，从根上避免
    #      「静态段被 /papers/{paper_id} 抢占」这一类 422 缺陷（见上面 2)/3) 的教训）。
    #      插在 papers 之前只是额外保险；模块缺失时仅告警跳过，不影响既有功能。
    ("app.api.v1.translate", "/api/v1", False),
    ("app.api.v1.reader", "/api/v1", False),
    ("app.api.v1.exports", "/api/v1", False),
    # 3) 解析卡片（WP06）：除 /papers/{id}/card* 外还带**静态集合端点** /papers/card-jobs，
    #    该字面路径必须早于 papers 的 /papers/{paper_id} 注册，否则会被参数路由抢走
    #    并返回 422（loc=['path','paper_id'], type=int_parsing）——即「路由注册顺序即匹配优先级」。
    #    证据：tests/.reports/p0_defects.json（severity=high）、docs/README-工程.md §8。
    ("app.api.v1.parses", "/api/v1", False),
    # 4) 论文检索/详情（WP03）：/papers/search /papers/fetch /papers/{id}
    ("app.api.v1.papers", "/api/v1", False),
    # 5) 全文解析记录（WP05）：/papers/{id}/documents /spans /parse
    ("app.api.v1.documents", "/api/v1", False),
    # 6) 构思域（WP08）：聚合 / 空白清单 / idea / 可行性 / 任务书
    ("app.api.v1.aggregations", "/api/v1", False),
    ("app.api.v1.ideas", "/api/v1", False),
    ("app.api.v1.feasibilities", "/api/v1", False),
    ("app.api.v1.taskbooks", "/api/v1", False),
    # 7) 流水线 / 决策 / SSE（WP09 / WP10）
    ("app.api.v1.pipelines", "/api/v1", False),
    ("app.api.v1.runs", "/api/v1", False),
    ("app.api.v1.stream", "/api/v1", False),
    ("app.api.v1.decisions", "/api/v1", False),
    # 8) 实验 / Passport / 盲评校准（WP11 / WP12）
    ("app.api.v1.experiments", "/api/v1", False),
    ("app.api.v1.passports", "/api/v1", False),
    ("app.api.v1.calibration", "/api/v1", False),
    # 9) 证据链与草稿（WP13 / WP14）
    ("app.api.v1.evidence", "/api/v1", False),
    ("app.api.v1.claims", "/api/v1", False),
    ("app.api.v1.drafts", "/api/v1", False),
    # 10) 项目容器（WP09 的 projects 端点；单列避免与 pipelines 前缀混淆）
    ("app.api.v1.projects", "/api/v1", False),
    # 11) 演示与访问面（WP16）
    ("app.api.v1.demo", "/api/v1", False),
    ("app.api.v1.owner", "/api/v1", False),
    # 12) 模型配置与成本（WP02，自带 /models 与 /costs 前缀）
    ("app.api.v1.models_config", "/api/v1", True),
    ("app.api.v1.costs", "/api/v1", True),
)


def _mount_routers(app: FastAPI) -> None:
    """按注册表顺序挂载路由；必需模块缺失直接抛错，可选模块缺失仅告警。"""
    mounted: list[str] = []
    skipped: list[str] = []
    for module_path, prefix, required in ROUTER_REGISTRY:
        try:
            module = importlib.import_module(module_path)
        except ModuleNotFoundError as exc:
            if required:
                raise
            skipped.append(f"{module_path}({exc.name})")
            logger.info("跳过未就绪路由模块: %s（%s）", module_path, exc.name)
            continue
        router = getattr(module, "router", None)
        if router is None:
            raise RuntimeError(f"{module_path} 未导出 APIRouter 实例 'router'")
        app.include_router(router, prefix=prefix)
        mounted.append(module_path)
    logger.info("已挂载路由模块 %d 个: %s", len(mounted), ", ".join(mounted))
    if skipped:
        logger.warning("未挂载（等待并行工作包）: %s", ", ".join(skipped))


@asynccontextmanager
async def lifespan(app: FastAPI):
    """应用生命周期：启动探测数据库 + 按开关挂载批量任务调度器，关闭时释放资源。"""
    probe = await db_session.ping_database()
    if probe["ok"]:
        logger.info("数据库连接正常: %s (%s)", probe.get("database"), probe.get("server_version"))
    else:
        logger.warning("数据库不可用: %s", probe.get("detail"))
    if settings.demo_seed_on_startup:
        logger.info("DEMO_SEED_ON_STARTUP=1：预置示例 Project 由 WP16 种子任务负责导入")
    # WP01-T6：调度器默认关闭（SCHEDULER_ENABLED=0），关闭时由 scheduler 打印「未启用」；
    # 打开时注册 fetch_papers / score_papers / parse_fulltext（任一任务模块缺失只告警）。
    job_scheduler.start_scheduler()
    yield
    job_scheduler.shutdown_scheduler()
    await db_session.dispose_engines()
    logger.info("数据库连接池已释放")


def create_app() -> FastAPI:
    """构建应用实例（工厂形式，便于测试注入）。"""
    application = FastAPI(
        title=APP_NAME,
        version=APP_VERSION,
        description="可审计科研流水线：文献空白 → 可复现实验 → 风险自适应",
        license_info={"name": "Apache-2.0"},
        lifespan=lifespan,
    )

    # 开发期前端跑在 5173，生产由 nginx 同源代理 /api，故这里只放行本地来源
    application.add_middleware(
        CORSMiddleware,
        allow_origins=["http://localhost:5173", "http://127.0.0.1:5173"],
        allow_credentials=True,
        allow_methods=["*"],
        allow_headers=["*"],
    )

    _mount_routers(application)
    _install_error_handlers(application)
    return application


def _error_body(code: str, message: str, detail: Any = None) -> dict[str, Any]:
    """统一错误体 ``{code, message, detail}``（contracts.api_contract.error_shape）。"""
    return {"code": code, "message": message, "detail": detail}


def _install_error_handlers(application: FastAPI) -> None:
    @application.exception_handler(StarletteHTTPException)
    async def _http_exception_handler(request: Request, exc: StarletteHTTPException):
        detail = exc.detail
        if isinstance(detail, dict) and {"code", "message"} <= set(detail):
            body = detail
        else:
            body = _error_body(f"http_{exc.status_code}", str(detail), None)
        return JSONResponse(status_code=exc.status_code, content=body)

    @application.exception_handler(RequestValidationError)
    async def _validation_handler(request: Request, exc: RequestValidationError):
        return JSONResponse(
            status_code=422,
            content=_error_body("validation_error", "请求参数校验失败", exc.errors()),
        )

    @application.exception_handler(Exception)
    async def _unhandled_handler(request: Request, exc: Exception):
        logger.exception("未处理异常: %s %s", request.method, request.url.path)
        return JSONResponse(
            status_code=500,
            content=_error_body("internal_error", "服务内部错误", None),
        )


app = create_app()


# --------------------------------------------------------------------------- #
# 健康检查（WP01-T5）：应用 + 数据库双状态
# --------------------------------------------------------------------------- #
@app.get("/api/v1/health", tags=["system"], summary="应用与数据库健康检查")
async def health() -> JSONResponse:
    probe = await db_session.ping_database()
    payload = {
        "status": "ok" if probe["ok"] else "degraded",
        "app": {
            "name": APP_NAME,
            "version": APP_VERSION,
            "env": settings.app_env,
            "access_mode": settings.app_access_mode,
        },
        "db": probe,
        "checked_at": datetime.now(UTC).isoformat(),
    }
    return JSONResponse(status_code=200 if probe["ok"] else 503, content=payload)


@app.get("/api/v1/health/live", tags=["system"], summary="存活探针（不查库）")
async def health_live() -> dict[str, Any]:
    return {"status": "ok", "uptime_checked_at": datetime.now(UTC).isoformat()}


@app.get("/", include_in_schema=False)
async def root() -> dict[str, str]:
    return {
        "name": APP_NAME,
        "version": APP_VERSION,
        "docs": "/docs",
        "health": "/api/v1/health",
    }


# 供 ``python -m app.main`` 本地起服（生产走 uvicorn 命令）
if __name__ == "__main__":  # pragma: no cover
    import uvicorn

    _start = time.time()
    uvicorn.run("app.main:app", host="0.0.0.0", port=8000, reload=False)  # noqa: S104
    logger.info("服务退出，运行 %.1fs", time.time() - _start)
