# Copyright 2026 SciLoop contributors
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
"""取数缓存、限流退避与统一 HTTP 客户端（WP03-T7）。

设计要点
--------
1. **TTL 缓存**：键 = ``source + 请求指纹``（SHA-256 前 24 位），默认 TTL 来自
   ``SOURCE_CACHE_TTL_HOURS``；同一请求在 TTL 内只产生一次真实外部调用，
   缓存命中/未命中计数可通过 :func:`cache_stats` 读取（验收要求"日志计数验证"）。
2. **统一超时与重试**：``SOURCE_FETCH_TIMEOUT_SECONDS`` 作为默认超时；对
   429/5xx 与传输层异常做**指数退避**（带抖动、尊重 ``Retry-After``）。
3. **QPS 限流**：每个源一个 :class:`RateLimiter`，保证 S2 在
   ``SEMANTIC_SCHOLAR_QPS=1`` 下串行节流。
4. **脱敏**：``request_url`` 落库前去掉 ``api_key`` 之类的查询参数，并把
   ``mailto`` 值替换为 ``***``（OpenAlex 必带 mailto，但不该把邮箱写进可公开展示的表）。

本模块只负责"怎么取"，不负责"取到的值属于哪篇论文/哪个字段"——后者在
``identity.py`` / ``source_records.py``。
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import random
import re
import time
from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Any
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

import httpx

from app.core.config import get_settings

logger = logging.getLogger("sciloop.wp03.cache")

# 命中 429/5xx 时的默认退避基数（秒）与上限
DEFAULT_BACKOFF_BASE_SECONDS = 0.5
DEFAULT_BACKOFF_MAX_SECONDS = 4.0
# 传输层异常（DNS/连接/超时）同样参与重试
RETRYABLE_STATUSES: frozenset[int] = frozenset({408, 425, 429, 500, 502, 503, 504})

# 需要从留痕 URL 中抹除的查询参数（大小写不敏感）
REDACT_PARAM_KEYWORDS: tuple[str, ...] = ("api_key", "apikey", "key", "token", "secret")
# 保留键名但抹除值（OpenAlex 的 mailto 是个人邮箱，不该出现在公开响应里）
MASK_PARAM_KEYWORDS: tuple[str, ...] = ("mailto",)


# --------------------------------------------------------------------------------------
# 请求指纹与 URL 脱敏
# --------------------------------------------------------------------------------------
def request_fingerprint(method: str, url: str, params: Mapping[str, Any] | None = None) -> str:
    """构造请求指纹：同 method + URL + 参数（顺序无关）→ 同一指纹。"""
    payload = json.dumps(
        {
            "method": method.upper(),
            "url": url,
            "params": sorted((str(k), str(v)) for k, v in (params or {}).items()),
        },
        ensure_ascii=False,
        separators=(",", ":"),
    )
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()[:24]


def redact_url(url: str) -> str:
    """抹除 URL 中的密钥类查询参数；``mailto`` 只保留键名。"""
    if not url:
        return url
    parts = urlsplit(url)
    if not parts.query:
        return url
    kept: list[tuple[str, str]] = []
    for key, value in parse_qsl(parts.query, keep_blank_values=True):
        lowered = key.lower()
        if any(word in lowered for word in REDACT_PARAM_KEYWORDS):
            continue
        if any(word in lowered for word in MASK_PARAM_KEYWORDS):
            kept.append((key, "***"))
            continue
        kept.append((key, value))
    return urlunsplit((parts.scheme, parts.netloc, parts.path, urlencode(kept), parts.fragment))


def build_url(url: str, params: Mapping[str, Any] | None = None) -> str:
    """把参数拼进 URL（用于展示与留痕），跳过值为 None 的参数。"""
    if not params:
        return url
    clean = {k: v for k, v in params.items() if v is not None}
    if not clean:
        return url
    parts = urlsplit(url)
    query = (
        parts.query + "&" + urlencode(clean, doseq=True)
        if parts.query
        else urlencode(clean, doseq=True)
    )
    return urlunsplit((parts.scheme, parts.netloc, parts.path, query, parts.fragment))


# --------------------------------------------------------------------------------------
# TTL 缓存
# --------------------------------------------------------------------------------------
@dataclass
class CacheEntry:
    """一条缓存条目（同时保留原始响应文本，便于写入 ``papers.raw``）。"""

    key: str
    payload: Any
    text: str
    status_code: int
    request_url: str
    stored_at: float
    source: str
    headers: dict[str, str] = field(default_factory=dict)

    def age_seconds(self, *, clock: Any = time.monotonic) -> float:
        return max(0.0, clock() - self.stored_at)


class TTLCache:
    """进程内 TTL 缓存（键 = source + 请求指纹）。

    - 只缓存 **2xx** 响应：错误响应绝不缓存，避免把一次限流固化成一小时的错误结论。
    - ``hits`` / ``misses`` / ``external_calls`` 三个计数器用于验收核对。
    """

    def __init__(
        self,
        *,
        ttl_hours: float | None = None,
        max_entries: int = 4096,
        clock: Any = time.monotonic,
        name: str = "source",
    ) -> None:
        settings = get_settings()
        self.ttl_seconds = (
            float(ttl_hours if ttl_hours is not None else settings.source_cache_ttl_hours) * 3600.0
        )
        self.max_entries = max_entries
        self._clock = clock
        self.name = name
        self._entries: dict[str, CacheEntry] = {}
        self.hits = 0
        self.misses = 0
        self.external_calls = 0
        self.evictions = 0

    # ---------------------------------------------------------------- 基本读写
    def key_for(self, source: str, fingerprint: str) -> str:
        return f"{source}:{fingerprint}"

    def get(self, key: str) -> CacheEntry | None:
        entry = self._entries.get(key)
        if entry is None:
            self.misses += 1
            return None
        if entry.age_seconds(clock=self._clock) > self.ttl_seconds:
            self._entries.pop(key, None)
            self.evictions += 1
            self.misses += 1
            logger.info("source_cache_expired key=%s ttl_s=%.0f", key, self.ttl_seconds)
            return None
        self.hits += 1
        logger.info(
            "source_cache_hit key=%s hits=%d misses=%d external=%d",
            key,
            self.hits,
            self.misses,
            self.external_calls,
        )
        return entry

    def set(self, key: str, entry: CacheEntry) -> None:
        if len(self._entries) >= self.max_entries:
            self._purge_oldest()
        self._entries[key] = entry

    def invalidate(self, prefix: str | None = None) -> int:
        """清除缓存（``prefix`` 为 ``source:`` 前缀时只清该源）。返回清除条数。"""
        if prefix is None:
            removed = len(self._entries)
            self._entries.clear()
            return removed
        keys = [k for k in self._entries if k.startswith(prefix)]
        for key in keys:
            self._entries.pop(key, None)
        return len(keys)

    def _purge_oldest(self) -> None:
        ordered = sorted(self._entries.items(), key=lambda kv: kv[1].stored_at)
        drop = max(1, len(ordered) // 10)
        for key, _ in ordered[:drop]:
            self._entries.pop(key, None)
            self.evictions += 1

    def purge_expired(self) -> int:
        now = self._clock()
        expired = [k for k, e in self._entries.items() if now - e.stored_at > self.ttl_seconds]
        for key in expired:
            self._entries.pop(key, None)
        return len(expired)

    def stats(self) -> dict[str, Any]:
        total = self.hits + self.misses
        return {
            "name": self.name,
            "ttl_hours": round(self.ttl_seconds / 3600.0, 3),
            "entries": len(self._entries),
            "hits": self.hits,
            "misses": self.misses,
            "external_calls": self.external_calls,
            "evictions": self.evictions,
            "hit_rate": round(self.hits / total, 4) if total else None,
        }


_DEFAULT_CACHE: TTLCache | None = None


def get_cache() -> TTLCache:
    """进程内默认缓存单例。"""
    global _DEFAULT_CACHE
    if _DEFAULT_CACHE is None:
        _DEFAULT_CACHE = TTLCache()
    return _DEFAULT_CACHE


def reset_cache() -> None:
    """测试用：重建默认缓存单例。"""
    global _DEFAULT_CACHE
    _DEFAULT_CACHE = None


def cache_stats() -> dict[str, Any]:
    return get_cache().stats()


# --------------------------------------------------------------------------------------
# 限流
# --------------------------------------------------------------------------------------
class RateLimiter:
    """按 QPS 串行节流（S2 默认 1 QPS；arXiv/OpenAlex 可放宽）。"""

    def __init__(
        self, qps: float | None, *, clock: Any = time.monotonic, sleep: Any = asyncio.sleep
    ) -> None:
        self.qps = float(qps or 0.0)
        self.min_interval = 1.0 / self.qps if self.qps > 0 else 0.0
        self._lock: asyncio.Lock | None = None
        self._lock_loop: Any = None
        self._last_call = 0.0
        self._clock = clock
        self._sleep = sleep
        self.waits = 0

    def _ensure_lock(self) -> asyncio.Lock:
        """asyncio.Lock 首次 await 时会绑定事件循环；换循环时必须换锁。"""
        loop = current_loop()
        if self._lock is None or (self._lock_loop is not None and loop is not self._lock_loop):
            self._lock = asyncio.Lock()
            self._lock_loop = loop
        return self._lock

    async def acquire(self) -> float:
        """获取一次调用许可，返回实际等待秒数。"""
        if self.min_interval <= 0:
            return 0.0
        async with self._ensure_lock():
            now = self._clock()
            elapsed = now - self._last_call
            wait = self.min_interval - elapsed
            if wait > 0:
                self.waits += 1
                await self._sleep(wait)
                now = self._clock()
            self._last_call = now
            return max(0.0, wait)


# --------------------------------------------------------------------------------------
# HTTP 结果
# --------------------------------------------------------------------------------------
@dataclass
class HttpResult:
    """一次取数的结果（成功与失败共用同一形状，便于统一写留痕）。"""

    source: str
    method: str
    request_url: str
    status_code: int | None
    payload: Any | None
    text: str
    from_cache: bool
    attempts: int
    elapsed_ms: float
    error: str | None = None
    error_kind: str | None = None
    headers: dict[str, str] = field(default_factory=dict)
    fingerprint: str = ""

    @property
    def ok(self) -> bool:
        return self.status_code is not None and 200 <= self.status_code < 300 and self.error is None

    def to_dict(self) -> dict[str, Any]:
        return {
            "source": self.source,
            "request_url": self.request_url,
            "http_status": self.status_code,
            "ok": self.ok,
            "from_cache": self.from_cache,
            "attempts": self.attempts,
            "elapsed_ms": round(self.elapsed_ms, 2),
            "error": self.error,
            "error_kind": self.error_kind,
        }


# --------------------------------------------------------------------------------------
# 环境变量清洗与请求头安全化
# --------------------------------------------------------------------------------------
def clean_env_value(value: Any) -> str:
    """清洗环境变量值：去掉行尾注释与首尾空白，非 ASCII 视为未配置。

    背景（实测 2026-09-17）：``.env`` 里形如 ``GITHUB_TOKEN=    # 注释`` 的**空值行**，
    经 docker compose ``env_file`` 解析后，注释文本被当成了值
    （容器内 ``GITHUB_TOKEN="# 可选：提升代码热度查询限额"``）。非 ASCII 的请求头
    会让 httpx 直接抛 ``UnicodeEncodeError``，把整轮抓取打挂；
    而被污染成"非空"的密钥还会让 ``configured`` 误报为真。这里统一做防御性清洗。
    """
    if value is None:
        return ""
    text = str(value).strip()
    if not text or text.startswith("#"):
        return ""
    match = re.match(r"^(?P<value>.*?)\s+#", text)
    if match:
        text = match.group("value").strip()
    if not text:
        return ""
    try:
        text.encode("ascii")
    except UnicodeEncodeError:
        logger.warning(
            "环境变量值含非 ASCII 字符（疑似 .env 行尾注释被当作值），已按未配置处理：%r",
            text[:40],
        )
        return ""
    return text


def ascii_safe_headers(headers: Mapping[str, str] | None) -> dict[str, str]:
    """过滤掉非 ASCII 的请求头（HTTP 头必须是 ASCII；脏配置不应打挂整轮抓取）。"""
    safe: dict[str, str] = {}
    for key, value in (headers or {}).items():
        if not value:
            continue
        try:
            str(value).encode("ascii")
            key.encode("ascii")
        except UnicodeEncodeError:
            logger.warning("丢弃非 ASCII 请求头 %r（疑似配置污染）", key)
            continue
        safe[str(key)] = str(value)
    return safe


def safe_float(value: Any, default: float) -> float:
    """宽松解析浮点配置：脏值时回退默认并告警。"""
    text = clean_env_value(value) if isinstance(value, str) else value
    try:
        return float(text)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        if value not in (None, ""):
            logger.warning("浮点配置非法 %r，回退默认 %s", value, default)
        return default


def current_loop() -> Any:
    """当前事件循环（无运行中循环时返回 None）。"""
    try:
        return asyncio.get_running_loop()
    except RuntimeError:
        return None


# --------------------------------------------------------------------------------------
# 统一 HTTP 客户端
# --------------------------------------------------------------------------------------
class SourceHTTP:
    """带超时、重试、退避、限流与 TTL 缓存的 httpx 客户端包装。"""

    def __init__(
        self,
        *,
        source: str,
        headers: Mapping[str, str] | None = None,
        timeout_seconds: float | None = None,
        qps: float | None = None,
        max_retries: int = 2,
        retry_statuses: frozenset[int] | tuple[int, ...] = RETRYABLE_STATUSES,
        cache: TTLCache | None = None,
        use_cache: bool = True,
        backoff_base: float = DEFAULT_BACKOFF_BASE_SECONDS,
        backoff_max: float = DEFAULT_BACKOFF_MAX_SECONDS,
        client: httpx.AsyncClient | None = None,
    ) -> None:
        settings = get_settings()
        self.source = source
        self.timeout = float(
            timeout_seconds
            if timeout_seconds is not None
            else settings.source_fetch_timeout_seconds
        )
        self.max_retries = max(0, int(max_retries))
        self.retry_statuses = frozenset(int(s) for s in retry_statuses)
        self.cache = cache if cache is not None else get_cache()
        self.use_cache = use_cache
        self.backoff_base = backoff_base
        self.backoff_max = backoff_max
        self.limiter = RateLimiter(qps)
        self.headers = {
            "User-Agent": "SciLoop/0.1 (+https://github.com/sciloop; research paper retrieval)",
            **ascii_safe_headers(headers),
        }
        self._client = client
        self._owns_client = client is None
        self._client_loop: Any = None
        self.last_retry_after: float | None = None
        self.loop_switches = 0

    # ---------------------------------------------------------------- 客户端管理
    def _ensure_client(self) -> httpx.AsyncClient:
        """返回可用的 httpx 客户端；**跨事件循环时自动重建**。

        httpx 的连接池绑定在创建它的事件循环上。同一个进程里既可能在 FastAPI
        主循环里调用（探活/同步端点），也可能在后台线程自己的循环里调用
        （``POST /papers/fetch``）——复用旧客户端会抛
        ``RuntimeError: ... is bound to a different event loop``（实测踩过）。
        """
        loop = current_loop()
        if (
            self._client is not None
            and self._client_loop is not None
            and loop is not self._client_loop
        ):
            logger.info(
                "source_client_loop_changed source=%s：旧连接池绑定在别的事件循环，重建客户端",
                self.source,
            )
            self._client = None
            self.loop_switches += 1
        if self._client is None:
            self._client = httpx.AsyncClient(
                timeout=httpx.Timeout(self.timeout),
                follow_redirects=True,
                headers=self.headers,
            )
            self._client_loop = loop
        return self._client

    async def aclose(self) -> None:
        if self._owns_client and self._client is not None:
            await self._client.aclose()
            self._client = None

    async def __aenter__(self) -> SourceHTTP:
        self._ensure_client()
        return self

    async def __aexit__(self, *exc: Any) -> None:
        await self.aclose()

    # ---------------------------------------------------------------- 主请求
    async def get_json(
        self,
        url: str,
        params: Mapping[str, Any] | None = None,
        *,
        headers: Mapping[str, str] | None = None,
        use_cache: bool | None = None,
        max_retries: int | None = None,
        retry_statuses: frozenset[int] | tuple[int, ...] | None = None,
        accept_statuses: frozenset[int] | tuple[int, ...] | None = None,
        expect_json: bool = True,
    ) -> HttpResult:
        """GET 并解析响应。

        ``expect_json=False`` 时不解析 JSON（arXiv 返回的是 Atom XML，用 ``text`` 即可），
        2xx 即视为成功。

        返回 :class:`HttpResult`；**不抛 HTTP 错误**（网络/超时/非 2xx 都编码进结果），
        让上层按来源决定降级策略（S2 → OpenAlex 等）。
        """
        cache_on = self.use_cache if use_cache is None else bool(use_cache)
        retries = self.max_retries if max_retries is None else max(0, int(max_retries))
        statuses = frozenset(
            int(s) for s in (self.retry_statuses if retry_statuses is None else retry_statuses)
        )
        accepted = frozenset(int(s) for s in (accept_statuses or ()))
        shown_url = redact_url(build_url(url, params))
        fingerprint = request_fingerprint("GET", url, params)
        cache_key = self.cache.key_for(self.source, fingerprint)

        if cache_on:
            entry = self.cache.get(cache_key)
            if entry is not None:
                return HttpResult(
                    source=self.source,
                    method="GET",
                    request_url=entry.request_url,
                    status_code=entry.status_code,
                    payload=entry.payload,
                    text=entry.text,
                    from_cache=True,
                    attempts=0,
                    elapsed_ms=0.0,
                    headers=dict(entry.headers),
                    fingerprint=fingerprint,
                )

        client = self._ensure_client()
        started = time.monotonic()
        attempts = 0
        last_error: str | None = None
        last_kind: str | None = None
        response: httpx.Response | None = None

        for attempt in range(retries + 1):
            attempts = attempt + 1
            await self.limiter.acquire()
            try:
                response = await client.get(
                    url, params=dict(params or {}), headers=dict(headers or {})
                )
            except httpx.TimeoutException as exc:
                last_error, last_kind = f"timeout: {exc}", "timeout"
                response = None
            except httpx.TransportError as exc:
                last_error, last_kind = f"transport_error: {exc}", "transport"
                response = None
            except httpx.HTTPError as exc:  # noqa: BLE001 - 兜底为可识别错误
                last_error, last_kind = f"http_error: {exc}", "http"
                response = None
            else:
                if response.status_code in statuses and attempt < retries:
                    last_error = f"retryable_status_{response.status_code}"
                    last_kind = "retryable_status"
                elif 200 <= response.status_code < 300 or response.status_code in accepted:
                    break
                else:
                    last_error = f"status_{response.status_code}"
                    last_kind = "http_status"
                    break

            if attempt < retries:
                delay = self._backoff_delay(attempt, response)
                logger.warning(
                    "source_retry source=%s attempt=%d/%d kind=%s error=%s sleep_s=%.2f url=%s",
                    self.source,
                    attempts,
                    retries + 1,
                    last_kind,
                    last_error,
                    delay,
                    shown_url,
                )
                await asyncio.sleep(delay)

        elapsed_ms = (time.monotonic() - started) * 1000.0
        self.cache.external_calls += 1

        status_code = response.status_code if response is not None else None
        text = ""
        payload: Any | None = None
        resp_headers: dict[str, str] = {}
        if response is not None:
            text = response.text or ""
            resp_headers = {
                k: v
                for k, v in response.headers.items()
                if k.lower()
                in {
                    "content-type",
                    "retry-after",
                    "x-ratelimit-remaining",
                    "x-ratelimit-limit",
                    "x-ratelimit-reset",
                    "x-ratelimit-remaining-usd",
                }
            }
            if status_code is not None and (200 <= status_code < 300 or status_code in accepted):
                last_error, last_kind = None, None
                if expect_json:
                    try:
                        payload = response.json()
                    except ValueError:
                        payload = None
                        if text:
                            last_error = "invalid_json"
                            last_kind = "parse"

        result = HttpResult(
            source=self.source,
            method="GET",
            request_url=shown_url,
            status_code=status_code,
            payload=payload,
            text=text,
            from_cache=False,
            attempts=attempts,
            elapsed_ms=elapsed_ms,
            error=last_error,
            error_kind=last_kind,
            headers=resp_headers,
            fingerprint=fingerprint,
        )

        if result.ok and cache_on:
            self.cache.set(
                cache_key,
                CacheEntry(
                    key=cache_key,
                    payload=payload,
                    text=text,
                    status_code=int(status_code or 0),
                    request_url=shown_url,
                    stored_at=time.monotonic(),
                    source=self.source,
                    headers=resp_headers,
                ),
            )
        if not result.ok:
            logger.warning(
                "source_fetch_failed source=%s status=%s kind=%s attempts=%d elapsed_ms=%.1f url=%s",
                self.source,
                status_code,
                last_kind,
                attempts,
                elapsed_ms,
                shown_url,
            )
        return result

    # ---------------------------------------------------------------- 退避
    def _backoff_delay(self, attempt: int, response: httpx.Response | None) -> float:
        """指数退避 + 抖动；若有 ``Retry-After`` 则尊重它（上限 8s）。"""
        retry_after = None
        if response is not None:
            raw = response.headers.get("retry-after")
            if raw:
                try:
                    retry_after = float(raw.strip())
                except ValueError:
                    retry_after = None
        self.last_retry_after = retry_after
        if retry_after is not None:
            return min(8.0, max(0.0, retry_after))
        base = min(self.backoff_max, self.backoff_base * (2**attempt))
        return base + random.uniform(0, base * 0.25)  # noqa: S311 - 抖动无需密码学强度


# --------------------------------------------------------------------------------------
# 每源单例（同一进程内共享缓存与限流器）
# --------------------------------------------------------------------------------------
_CLIENTS: dict[str, SourceHTTP] = {}


def get_source_http(source: str, **kwargs: Any) -> SourceHTTP:
    """获取（或创建）某来源的共享 HTTP 客户端。

    首个调用者可传入 ``headers`` / ``qps`` / ``timeout_seconds`` 等参数决定该源画像；
    后续调用复用同一实例，保证缓存与限流在整个进程内是同一份。
    """
    existing = _CLIENTS.get(source)
    if existing is not None:
        return existing
    client = SourceHTTP(source=source, **kwargs)
    _CLIENTS[source] = client
    return client


async def close_all_clients() -> None:
    """关闭全部共享客户端（测试/关机用）。

    客户端可能绑定在**已关闭的事件循环**上（例如脚本里多次 ``asyncio.run``），
    此时关闭会抛 ``Event loop is closed``；这里吞掉该异常并只记 debug 日志，
    避免"清理失败"掩盖真正的业务结果。
    """
    for client in list(_CLIENTS.values()):
        try:
            await client.aclose()
        except RuntimeError as exc:  # pragma: no cover - 仅脚本多次 asyncio.run 会触发
            logger.debug("关闭共享客户端跳过（事件循环已关闭）: %s", exc)
        except Exception as exc:  # noqa: BLE001 - 清理阶段不应抛异常
            logger.warning("关闭共享客户端失败: %s", exc)
    _CLIENTS.clear()


__all__ = [
    "CacheEntry",
    "HttpResult",
    "RateLimiter",
    "SourceHTTP",
    "TTLCache",
    "ascii_safe_headers",
    "build_url",
    "cache_stats",
    "clean_env_value",
    "close_all_clients",
    "current_loop",
    "get_cache",
    "get_source_http",
    "redact_url",
    "request_fingerprint",
    "reset_cache",
    "safe_float",
]
