# Copyright 2026 SciLoop contributors
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
"""访问面策略与护栏（WP16-T1）。

口径来源：``contracts.api_contract.public_demo_allowed`` / ``owner_only``。

- ``public_demo``：匿名**只读**。允许全部 GET/HEAD/OPTIONS，外加
  ``POST /passports/{id}/replay``（按 ``PUBLIC_REPLAY_RATE_LIMIT_PER_HOUR`` 限额）。
  **其余写操作一律拒绝**（403 ``owner_token_required``）。
- ``owner_mode`` / 携带有效 ``X-Owner-Token``：写操作放行，逐路由仍由
  ``app.core.security.require_owner`` 二次校验（常量时间比较）。

本模块提供三样东西
------------------
1. :func:`evaluate_request` —— 纯函数策略判定（可单测、可复现，不依赖 FastAPI）
2. :class:`AccessModeGuardMiddleware` + :func:`install_access_guard` —— 全局兜底中间件
   （**兜底**：即使某个写路由忘了挂 ``require_owner``，public_demo 面也不会出现匿名写）
3. :func:`audit_routes` —— 路由审计：逐条列出写路由与其 Owner 守卫来源，
   用于交付验收与回归自查（``GET /demo/access-audit`` 与命令行共用）。

为什么要「路由级 require_owner + 全局兜底中间件」两层
----------------------------------------------------
路由级依赖是各工作包自己声明的第一道门；中间件是不依赖任何工作包的**第二道门**。
两层都失败才会漏，且漏了会被 :func:`audit_routes` 与
``app/fixtures/verify_public_demo_writes.sh`` 的匿名 curl 矩阵立刻发现。

中间件是**可选安装**的：它需要 ``app.main`` 里加一行 ``install_access_guard(app)``
（``main.py`` 归 WP01，本包不修改）。未安装时不影响任何现有行为——当前所有写路由
都已带 ``require_owner``，安装只是把「忘挂依赖」这一类回归堵死。

``OWNER_TOKEN`` 只在 :mod:`app.core.security` 里与请求头比较，本模块**从不读取、
从不记录、从不返回**其内容；所有对外结构只含布尔与计数。
"""

from __future__ import annotations

import json
import logging
import re
import time
from collections import deque
from collections.abc import Iterable, Mapping, MutableMapping
from dataclasses import dataclass, field
from typing import Any

from app.core.config import get_settings
from app.core.security import OWNER_HEADER, owner_token_matches

logger = logging.getLogger("sciloop.wp16.access")

# --------------------------------------------------------------------------- #
# 策略常量（与 contracts.api_contract 逐条对齐；改这里必须同步改契约）
# --------------------------------------------------------------------------- #
READ_METHODS: frozenset[str] = frozenset({"GET", "HEAD", "OPTIONS"})

#: public_demo 面**唯一**允许的写操作（限额）
PUBLIC_WRITE_ALLOWLIST: tuple[tuple[str, re.Pattern[str]], ...] = (
    ("POST", re.compile(r"^/api/v1/passports/[^/]+/replay/?$")),
)

#: 例外：以 POST 形式暴露但**只读**的端点（不改库）。
#: 逐条给出理由，避免「顺手加白名单」把真正的写操作放进来。
PUBLIC_READ_ONLY_WRITES: tuple[tuple[str, re.Pattern[str], str], ...] = (
    (
        "POST",
        re.compile(r"^/api/v1/decisions/[^/]+/evaluate-risk/?$"),
        "WP10 的只读计算端点：persist 默认 false 只算不落库；"
        "persist=true 会写 decision_logs，由路由层 require_owner 拦截（实测 403）",
    ),
)

#: ``contracts.api_contract.owner_only`` 清单的机器可读副本（用于验收逐条打勾）
OWNER_ONLY_CONTRACT: tuple[dict[str, str], ...] = (
    {"spec": "POST /passports/{id}/rerun", "method": "POST", "path": "/api/v1/passports/1/rerun"},
    {"spec": "POST /pipelines/{pid}/run", "method": "POST", "path": "/api/v1/pipelines/1/run"},
    {
        "spec": "POST /pipelines/{pid}/switch-mode",
        "method": "POST",
        "path": "/api/v1/pipelines/1/switch-mode",
    },
    {"spec": "POST /models/configs（全部写操作）", "method": "POST", "path": "/api/v1/models/configs"},
    {"spec": "POST /demo/mode", "method": "POST", "path": "/api/v1/demo/mode"},
    {
        "spec": "POST /demo/projects/seed",
        "method": "POST",
        "path": "/api/v1/demo/projects/seed",
    },
    {
        "spec": "POST /pipelines/{pid}/human-labels",
        "method": "POST",
        "path": "/api/v1/pipelines/1/human-labels",
    },
    {"spec": "POST /decisions/{id}/approve", "method": "POST", "path": "/api/v1/decisions/1/approve"},
    {"spec": "DELETE 任意资源", "method": "DELETE", "path": "/api/v1/models/configs/1"},
)

#: 判定「路由是否挂了 Owner 守卫」时识别的依赖函数名
OWNER_GUARD_NAMES: frozenset[str] = frozenset(
    {"require_owner", "require_owner_token", "owner_required", "_local_owner_guard"}
)
OWNER_GUARD_SUFFIXES: tuple[str, ...] = ("_owner_guard", "require_owner")


def _settings_rate_limit() -> int:
    try:
        return max(0, int(get_settings().public_replay_rate_limit_per_hour))
    except Exception:  # noqa: BLE001 - 配置异常时退化为最保守的 0（全部拒绝）
        logger.warning("读取 PUBLIC_REPLAY_RATE_LIMIT_PER_HOUR 失败，按 0 处理（拒绝匿名回放）")
        return 0


# --------------------------------------------------------------------------- #
# 请求判定（纯函数）
# --------------------------------------------------------------------------- #
@dataclass(frozen=True)
class AccessDecision:
    """一次请求的访问判定结果（``allowed=False`` 时由调用方按 ``code`` 返回）。"""

    allowed: bool
    action: str  # read | public_write | owner_write | denied
    status: int = 200
    code: str = "ok"
    message: str = ""
    detail: dict[str, Any] = field(default_factory=dict)

    def to_error_body(self) -> dict[str, Any]:
        """``contracts.api_contract.error_shape``：``{code,message,detail}``。"""
        return {"code": self.code, "message": self.message, "detail": self.detail}


def normalize_path(path: str) -> str:
    """去掉重复斜杠与结尾斜杠，便于与白名单正则匹配。"""
    cleaned = re.sub(r"/{2,}", "/", (path or "/").split("?", 1)[0])
    if len(cleaned) > 1:
        cleaned = cleaned.rstrip("/")
    return cleaned


def is_public_demo_allowed_write(method: str, path: str) -> bool:
    """public_demo 面是否允许该写请求（唯一例外：passport 回放）。"""
    upper = (method or "").upper()
    target = normalize_path(path)
    if any(
        upper == allowed_method and pattern.match(target)
        for allowed_method, pattern in PUBLIC_WRITE_ALLOWLIST
    ):
        return True
    return any(
        upper == allowed_method and pattern.match(target)
        for allowed_method, pattern, _reason in PUBLIC_READ_ONLY_WRITES
    )


def read_only_write_reason(method: str, path: str) -> str | None:
    """若该写请求命中「只读例外」白名单，返回其理由（用于审计输出）。"""
    upper = (method or "").upper()
    target = normalize_path(path)
    for allowed_method, pattern, reason in PUBLIC_READ_ONLY_WRITES:
        if upper == allowed_method and pattern.match(target):
            return reason
    return None


def evaluate_request(
    *,
    method: str,
    path: str,
    access_mode: str,
    owner_ok: bool,
    http_only: bool = True,
) -> AccessDecision:
    """判定一次请求是否放行（不访问数据库、不看请求体）。

    :param owner_ok: 调用方用 :func:`app.core.security.owner_token_matches` 算好的结果
    :param http_only: 非 HTTP 场景（内部调用）传 ``False`` 时只校验写操作与令牌
    """
    upper = (method or "GET").upper()
    target = normalize_path(path)
    if not target.startswith("/api/"):
        # 非业务面（/docs、/openapi.json、/ 等）不参与写操作管控
        return AccessDecision(True, "read")

    if upper in READ_METHODS:
        return AccessDecision(True, "read")

    if is_public_demo_allowed_write(upper, target):
        return AccessDecision(True, "public_write")

    if owner_ok:
        return AccessDecision(True, "owner_write")

    mode = (access_mode or "public_demo").strip().lower()
    return AccessDecision(
        allowed=False,
        action="denied",
        status=403,
        code="owner_token_required",
        message=(
            f"{upper} {target} 属于写操作：public_demo 面匿名只读"
            f"（当前 APP_ACCESS_MODE={mode}）。"
            f"请携带有效的 {OWNER_HEADER}，或改用只读端点。"
        ),
        detail={
            "access_mode": mode,
            "method": upper,
            "path": target,
            "owner_header": OWNER_HEADER,
            "public_demo_allowed": ["全部 GET/HEAD/OPTIONS", "POST /passports/{id}/replay（限额）"],
            "contract": "contracts.api_contract.owner_only",
        },
    )


# --------------------------------------------------------------------------- #
# 匿名回放限额（PUBLIC_REPLAY_RATE_LIMIT_PER_HOUR）
# --------------------------------------------------------------------------- #
@dataclass
class RateLimitResult:
    allowed: bool
    limit: int
    remaining: int
    retry_after_seconds: int
    window_seconds: int = 3600


class SlidingWindowRateLimiter:
    """进程内滑动窗口计数（演示场景单进程足够；多 worker 时每 worker 独立计数）。"""

    def __init__(self, limit_per_hour: int | None = None, window_seconds: int = 3600) -> None:
        self._limit_override = limit_per_hour
        self.window_seconds = window_seconds
        self._hits: dict[str, deque[float]] = {}

    @property
    def limit(self) -> int:
        if self._limit_override is not None:
            return max(0, int(self._limit_override))
        return _settings_rate_limit()

    def check(self, key: str, *, now: float | None = None) -> RateLimitResult:
        limit = self.limit
        current = time.monotonic() if now is None else now
        bucket = self._hits.setdefault(key, deque())
        while bucket and current - bucket[0] > self.window_seconds:
            bucket.popleft()
        if limit <= 0:
            return RateLimitResult(False, 0, 0, self.window_seconds, self.window_seconds)
        if len(bucket) >= limit:
            oldest = bucket[0]
            retry_after = max(1, int(self.window_seconds - (current - oldest)) + 1)
            return RateLimitResult(False, limit, 0, retry_after, self.window_seconds)
        bucket.append(current)
        return RateLimitResult(True, limit, max(0, limit - len(bucket)), 0, self.window_seconds)

    def peek(self, key: str, *, now: float | None = None) -> RateLimitResult:
        """只读查询（不消耗配额），供 ``GET /demo/status`` 展示余量。"""
        limit = self.limit
        current = time.monotonic() if now is None else now
        bucket = self._hits.get(key) or deque()
        used = sum(1 for ts in bucket if current - ts <= self.window_seconds)
        if limit <= 0:
            return RateLimitResult(False, 0, 0, self.window_seconds, self.window_seconds)
        remaining = max(0, limit - used)
        retry_after = 0
        if remaining == 0 and bucket:
            retry_after = max(1, int(self.window_seconds - (current - bucket[0])) + 1)
        return RateLimitResult(remaining > 0, limit, remaining, retry_after, self.window_seconds)

    def reset(self) -> None:
        self._hits.clear()


_replay_limiter = SlidingWindowRateLimiter()


def get_replay_limiter() -> SlidingWindowRateLimiter:
    """匿名回放限额单例。"""
    return _replay_limiter


def client_key_from_scope(scope: Mapping[str, Any]) -> str:
    """从 ASGI scope 取客户端标识（优先 ``X-Forwarded-For``，回落 remote addr）。"""
    headers = {k.decode("latin-1").lower(): v.decode("latin-1") for k, v in scope.get("headers") or []}
    forwarded = headers.get("x-forwarded-for") or ""
    if forwarded.strip():
        return forwarded.split(",")[0].strip()
    client = scope.get("client")
    if isinstance(client, (list, tuple)) and client:
        return str(client[0])
    return "anonymous"


async def require_public_replay_quota(request: Any) -> None:
    """FastAPI 依赖：匿名回放限额（``POST /passports/{id}/replay`` 应挂上本依赖）。

    超限返回 429 与 ``Retry-After``；Owner 面不受限（Owner 演示不受配额阻塞）。
    本依赖归属 WP16，由 ``app/api/v1/passports.py``（WP11）引用即可，
    无需修改 ``app.core.security``。
    """
    from fastapi import HTTPException, status

    if await _request_is_owner(request):
        return
    key = _request_client_key(request)
    result = get_replay_limiter().check(key)
    if result.allowed:
        return
    raise HTTPException(
        status_code=status.HTTP_429_TOO_MANY_REQUESTS,
        detail={
            "code": "public_replay_rate_limited",
            "message": (
                f"匿名回放次数已达上限（{result.limit} 次/小时，"
                "PUBLIC_REPLAY_RATE_LIMIT_PER_HOUR）；请稍后再试或使用 Owner 面"
            ),
            "detail": {
                "limit_per_hour": result.limit,
                "retry_after_seconds": result.retry_after_seconds,
                "window_seconds": result.window_seconds,
            },
        },
        headers={"Retry-After": str(result.retry_after_seconds)},
    )


async def _request_is_owner(request: Any) -> bool:
    headers = getattr(request, "headers", None)
    token = headers.get(OWNER_HEADER) if headers is not None else None
    return owner_token_matches(token)


def _request_client_key(request: Any) -> str:
    headers = getattr(request, "headers", None)
    if headers is not None:
        forwarded = headers.get("x-forwarded-for") or ""
        if forwarded.strip():
            return forwarded.split(",")[0].strip()
    client = getattr(request, "client", None)
    host = getattr(client, "host", None)
    return str(host) if host else "anonymous"


# --------------------------------------------------------------------------- #
# 全局兜底中间件（可选安装；见模块 docstring）
# --------------------------------------------------------------------------- #
class AccessModeGuardMiddleware:
    """纯 ASGI 中间件：public_demo 面拒绝除白名单外的所有写请求。

    用纯 ASGI（而非 ``BaseHTTPMiddleware``）实现，避免 SSE 长连接被缓冲。
    """

    def __init__(self, app: Any, *, enforce_in_owner_mode: bool = False) -> None:
        self.app = app
        self.enforce_in_owner_mode = enforce_in_owner_mode

    async def __call__(self, scope: MutableMapping[str, Any], receive: Any, send: Any) -> None:
        if scope.get("type") != "http":
            await self.app(scope, receive, send)
            return

        from app.services.demo.state import resolve_access_mode

        method = str(scope.get("method") or "GET")
        path = str(scope.get("path") or "/")
        header_map = {
            k.decode("latin-1").lower(): v.decode("latin-1") for k, v in scope.get("headers") or []
        }
        owner_ok = owner_token_matches(header_map.get(OWNER_HEADER.lower()))
        access_mode = resolve_access_mode()

        if access_mode == "owner_mode" and not self.enforce_in_owner_mode and not owner_ok:
            # owner_mode 下仍由路由层 require_owner 判定；这里只挡住匿名写，语义一致
            pass

        decision = evaluate_request(
            method=method, path=path, access_mode=access_mode, owner_ok=owner_ok
        )
        if decision.allowed:
            await self.app(scope, receive, send)
            return

        if method.upper() not in READ_METHODS:
            logger.warning(
                "访问面拦截匿名写请求 method=%s path=%s access_mode=%s client=%s",
                method,
                path,
                access_mode,
                client_key_from_scope(scope),
            )
        await _send_json(send, decision.status, decision.to_error_body())


async def _send_json(send: Any, status_code: int, body: dict[str, Any]) -> None:
    payload = json.dumps(body, ensure_ascii=False).encode("utf-8")
    await send(
        {
            "type": "http.response.start",
            "status": status_code,
            "headers": [
                (b"content-type", b"application/json; charset=utf-8"),
                (b"content-length", str(len(payload)).encode("latin-1")),
                (b"cache-control", b"no-store"),
            ],
        }
    )
    await send({"type": "http.response.body", "body": payload})


def install_access_guard(app: Any, *, enforce_in_owner_mode: bool = False) -> bool:
    """把兜底中间件装到 FastAPI 应用上（需在首个请求前调用）。

    供 ``app.main.restore_registry`` 之后加一行使用；**未安装也不影响现状**
    （所有写路由已带 ``require_owner``，本中间件是防「忘挂依赖」的第二道门）。
    返回是否安装成功（应用已启动时 Starlette 会拒绝，此时返回 ``False`` 并记日志）。
    """
    try:
        app.add_middleware(
            AccessModeGuardMiddleware, enforce_in_owner_mode=enforce_in_owner_mode
        )
    except Exception as exc:  # noqa: BLE001 - 已启动的应用无法再加中间件
        logger.warning("安装访问面兜底中间件失败（应用可能已启动）：%s", exc)
        return False
    logger.info("已安装访问面兜底中间件（public_demo 面拒绝匿名写操作）")
    return True


# --------------------------------------------------------------------------- #
# 路由审计（交付验收与回归自查）
# --------------------------------------------------------------------------- #
def _iter_dependants(dependant: Any) -> Iterable[Any]:
    stack = [dependant]
    while stack:
        current = stack.pop()
        if current is None:
            continue
        yield current
        stack.extend(getattr(current, "dependencies", None) or [])


def _owner_guard_from_dependant(route: Any) -> str | None:
    dependant = getattr(route, "dependant", None)
    if dependant is None:
        return None
    for dep in _iter_dependants(dependant):
        call = getattr(dep, "call", None)
        name = getattr(call, "__name__", "") or ""
        if name in OWNER_GUARD_NAMES:
            return f"dependency:{name}"
        if name and any(name.endswith(suffix) for suffix in OWNER_GUARD_SUFFIXES):
            return f"dependency:{name}"
    return None


def _router_prefix(router: Any) -> str:
    return str(getattr(router, "prefix", "") or "")


def _iter_api_routes(app: Any) -> Iterable[tuple[str, Any, str | None]]:
    """产出 ``(full_path, route, router_level_guard)``。

    FastAPI 新版 ``include_router`` 在 ``app.routes`` 里放的是 ``_IncludedRouter``
    包装对象（不暴露 ``path``/``dependant``），因此这里**按注册表还原**：
    逐个 import ``app.main.ROUTER_REGISTRY`` 里的模块，取 ``module.router.routes``
    （平铺的 ``APIRoute``，带 ``dependant``），再拼上 ``/api/v1`` + router 前缀。
    这样审计结果与真实挂载一一对应，且不依赖 FastAPI 内部实现细节。
    """
    try:
        from app.main import ROUTER_REGISTRY
    except Exception:  # noqa: BLE001 - 拿不到注册表就退回 app.routes 遍历
        yield from _iter_routes_from_app(app)
        return

    import importlib

    for module_path, prefix, _required in ROUTER_REGISTRY:
        try:
            module = importlib.import_module(module_path)
        except Exception:  # noqa: BLE001 - 未就绪模块没有路由可审
            continue
        router = getattr(module, "router", None)
        if router is None:
            continue
        router_guard = None
        for dependency in getattr(router, "dependencies", None) or []:
            name = getattr(getattr(dependency, "dependency", None), "__name__", "") or ""
            if name in OWNER_GUARD_NAMES or (
                name and any(name.endswith(suffix) for suffix in OWNER_GUARD_SUFFIXES)
            ):
                router_guard = f"router_dependency:{name}"
                break
        router_prefix = _router_prefix(router)
        for route in getattr(router, "routes", None) or []:
            path = getattr(route, "path", None)
            if not path:
                continue
            # FastAPI 的 ``APIRouter.add_api_route`` 会把 ``router.prefix`` 拼进
            # ``route.path``（实测 /passports 路由的 path 已是
            # ``/passports/{id}/replay``）。因此**不能**再拼一次 router 前缀，
            # 否则会出现 ``/api/v1/passports/passports/...`` 的双前缀幻影路径，
            # 与运行时 openapi.json 不一致，且会让白名单正则失配 → 误报 unguarded。
            # 仅当 route.path 不含该前缀时（旧版 FastAPI 行为）才补拼。
            full = (
                f"{_mount_prefix(prefix)}{path}"
                if not router_prefix or path.startswith(router_prefix)
                else f"{_mount_prefix(prefix)}{router_prefix}{path}"
            )
            yield normalize_path(full), route, router_guard


def _mount_prefix(prefix: str) -> str:
    return str(prefix or "")


def _iter_routes_from_app(app: Any) -> Iterable[tuple[str, Any, str | None]]:
    """兜底：直接遍历 ``app.routes``（FastAPI 版本把路由平铺在 app.routes 时可用）。"""
    for route in getattr(app, "routes", []) or []:
        path = getattr(route, "path", None)
        if path:
            yield normalize_path(path), route, None


def audit_routes(app: Any) -> dict[str, Any]:
    """列出全部写路由、其 Owner 守卫来源，以及契约清单的落实情况。"""
    routes: list[dict[str, Any]] = []
    seen: set[tuple[str, str]] = set()
    for path, route, router_guard in _iter_api_routes(app):
        methods = getattr(route, "methods", None) or set()
        writes = sorted(m for m in methods if m.upper() not in READ_METHODS)
        if not writes:
            continue
        signature = (path, ",".join(writes))
        if signature in seen:
            continue
        seen.add(signature)
        guard = _owner_guard_from_dependant(route) or router_guard
        reason = read_only_write_reason(writes[0], path) if len(writes) == 1 else None
        allowed = all(is_public_demo_allowed_write(method, path) for method in writes)
        if guard:
            status = "owner_required"
        elif reason:
            # 以 POST 暴露但只读的端点（``evaluate-risk``，persist=true 时路由层自带
            # 令牌校验）。reason 比 allowed 更具体，优先展示，避免丢失「为什么只读」。
            status = "read_only_exception"
        elif allowed:
            # 契约明确允许匿名写的少数端点（``POST /passports/{id}/replay``，限额）。
            # 它们**本就不该**挂 require_owner，因此不是 unguarded（误报来源之一）。
            status = "public_demo_allowed"
        else:
            status = "unguarded"
        routes.append(
            {
                "path": path,
                "write_methods": writes,
                "guard": guard,
                "guard_source": "route_dependency" if guard else "none",
                "owner_required": bool(guard),
                "public_demo_allowed": allowed,
                "read_only_exception": reason,
                "status": status,
            }
        )
    routes.sort(key=lambda item: (item["status"] != "unguarded", item["path"]))
    unguarded = [item for item in routes if item["status"] == "unguarded"]
    return {
        "write_route_total": len(routes),
        "owner_required_total": sum(1 for item in routes if item["owner_required"]),
        "read_only_exception_total": sum(1 for item in routes if item["read_only_exception"]),
        "public_demo_allowed_total": sum(
            1 for item in routes if item["status"] == "public_demo_allowed"
        ),
        "unguarded_total": len(unguarded),
        "unguarded": unguarded,
        "routes": routes,
        "access_mode": _current_access_mode(),
        "middleware_installed": has_access_guard(app),
        "owner_only_contract": owner_only_checklist(app),
        "notes": [
            "guard_source=route_dependency 表示该路由自带 require_owner（各工作包声明）",
            "read_only_exception 是以 POST 暴露但只读的端点，逐条在 PUBLIC_READ_ONLY_WRITES 里给了理由",
            "public_demo_allowed 是契约显式允许匿名写的端点（POST /passports/{id}/replay，限额），"
            "属于放行项而非 unguarded",
            "unguarded 应当始终为空；非空即说明需要为该路由补 require_owner 或全局安装兜底中间件",
            "本审计是静态分析（依赖树，按 app.main.ROUTER_REGISTRY 还原挂载路径），"
            "运行时的匿名拒绝证据由 app/fixtures/verify_public_demo_writes.sh 的 curl 矩阵产出",
        ],
    }


def has_access_guard(app: Any) -> bool:
    for middleware in getattr(app, "user_middleware", []) or []:
        if getattr(middleware, "cls", None) is AccessModeGuardMiddleware:
            return True
    return False


def _mounted_paths(app: Any) -> set[str]:
    return {path for path, _route, _guard in _iter_api_routes(app)}


def _path_template(path: str) -> str:
    """把 ``/pipelines/1/run`` 与 ``/pipelines/{project_id}/run`` 归一到同一模板。

    注意：路径参数替换为 ``{}``（**不带前导斜杠**），否则
    ``/pipelines/{project_id}/run`` 会被替换成 ``/pipelines//{}/run``（双斜杠），
    与 ``/pipelines/1/run`` 归一化出的 ``/pipelines/{}/run`` 不相等，
    导致 ``owner_only_checklist`` 把已实现的路由误判为 ``pending_module``。
    """
    without_params = re.sub(r"\{[^}]+\}", "{}", normalize_path(path))
    return re.sub(r"/\d+(?=/|$)", "/{}", without_params)


def owner_only_checklist(app: Any) -> list[dict[str, Any]]:
    """把 ``contracts.owner_only`` 清单与已挂载路由对齐，逐条给出可验收状态。"""
    templates = {_path_template(path) for path in _mounted_paths(app)}
    checklist: list[dict[str, Any]] = []
    for item in OWNER_ONLY_CONTRACT:
        template = _path_template(item["path"])
        implemented = template in templates
        checklist.append(
            {
                "spec": item["spec"],
                "probe": f"{item['method']} {item['path']}",
                "implemented": implemented,
                "expected_status": [401, 403],
                "status": "to_verify_by_curl" if implemented else "pending_module",
                "verify_with": "app/fixtures/verify_public_demo_writes.sh",
            }
        )
    return checklist


def _current_access_mode() -> str:
    from app.services.demo.state import resolve_access_mode

    return resolve_access_mode()


__all__ = [
    "AccessDecision",
    "AccessModeGuardMiddleware",
    "OWNER_HEADER",
    "OWNER_ONLY_CONTRACT",
    "PUBLIC_READ_ONLY_WRITES",
    "PUBLIC_WRITE_ALLOWLIST",
    "READ_METHODS",
    "RateLimitResult",
    "SlidingWindowRateLimiter",
    "audit_routes",
    "client_key_from_scope",
    "evaluate_request",
    "get_replay_limiter",
    "has_access_guard",
    "install_access_guard",
    "is_public_demo_allowed_write",
    "normalize_path",
    "owner_only_checklist",
    "public_demo_summary",
    "read_only_write_reason",
    "require_public_replay_quota",
]


def public_demo_summary() -> dict[str, Any]:
    """对外披露的访问面摘要（**不含令牌**，供前端 OwnerBadge 与 /demo/status 使用）。"""
    limiter = get_replay_limiter()
    return {
        "access_mode": _current_access_mode(),
        "read_methods": sorted(READ_METHODS),
        "public_write_allowlist": [
            f"{method} {pattern.pattern}" for method, pattern in PUBLIC_WRITE_ALLOWLIST
        ],
        "public_read_only_writes": [
            {"endpoint": f"{method} {pattern.pattern}", "reason": reason}
            for method, pattern, reason in PUBLIC_READ_ONLY_WRITES
        ],
        "owner_header": OWNER_HEADER,
        "owner_token_source": "env:OWNER_TOKEN（服务端环境变量；不下发、不入库、不写前端）",
        "replay_rate_limit_per_hour": limiter.limit,
        "owner_only_contract": [item["spec"] for item in OWNER_ONLY_CONTRACT],
    }
