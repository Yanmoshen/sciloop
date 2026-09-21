# Copyright 2026 SciLoop contributors
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
"""GitHub 客户端（WP03-T4）：从代码仓库链接取 ``stargazers_count``。

- 由 ``code_url``（arXiv comment/abstract 抽出的 GitHub 链接）解析 ``owner/repo``；
- 调用 ``GET /repos/{owner}/{repo}`` 取 star / fork / archived 等元数据；
- 可选 ``GITHUB_TOKEN`` 提升限额（未配置时匿名 60 次/小时）；
- **失败一律返回 ``stargazers_count=None`` 并写明原因，绝不编造**；
  仓库不存在 / 超限 / 网络故障都不致命（是否开源由 WP04 降级为布尔值判断）。

``raw_payload()`` 的输出形状与 WP04 的 ``influence_score.stars_from_raw`` 对齐
（``raw['github']['stargazers_count']``）。
"""

from __future__ import annotations

import logging
import re
from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Any

from core.config import get_settings
from services.paper_source.cache import (
    HttpResult,
    ascii_safe_headers,
    clean_env_value,
    get_source_http,
    safe_float,
)

logger = logging.getLogger("sciloop.wp03.github")

SOURCE_NAME = "github"
GITHUB_API_BASE = "https://api.github.com"
# 匿名 60/h、带 token 5000/h；这里按"带 token"设 2 QPS，匿名时靠上层少量调用
GITHUB_QPS = 2.0

_GITHUB_URL_RE = re.compile(
    r"(?:https?://)?(?:www\.)?github\.com/(?P<owner>[A-Za-z0-9](?:[A-Za-z0-9._-]*[A-Za-z0-9])?)"
    r"/(?P<repo>[A-Za-z0-9._-]+)",
    re.IGNORECASE,
)
_RESERVED_PATHS = frozenset(
    {
        "about",
        "actions",
        "blob",
        "branches",
        "collections",
        "commits",
        "compare",
        "dashboard",
        "discussions",
        "events",
        "explore",
        "features",
        "issues",
        "join",
        "login",
        "marketplace",
        "network",
        "new",
        "notifications",
        "organizations",
        "orgs",
        "pricing",
        "projects",
        "pulls",
        "pulse",
        "raw",
        "releases",
        "search",
        "security",
        "settings",
        "signin",
        "signup",
        "site",
        "sponsors",
        "stargazers",
        "stars",
        "tags",
        "topics",
        "trending",
        "tree",
        "watchers",
        "wiki",
    }
)


@dataclass
class GitHubRepo:
    """仓库元数据（取不到就是 None）。"""

    owner: str
    repo: str
    code_url: str
    stargazers_count: int | None = None
    forks_count: int | None = None
    open_issues_count: int | None = None
    archived: bool | None = None
    html_url: str | None = None
    description: str | None = None
    pushed_at: str | None = None
    license_spdx: str | None = None
    request_url: str | None = None
    http_status: int | None = None
    http_headers: dict[str, str] = field(default_factory=dict)
    error: str | None = None
    error_kind: str | None = None
    from_cache: bool = False
    raw: dict[str, Any] = field(default_factory=dict)

    @property
    def ok(self) -> bool:
        return self.error is None and self.stargazers_count is not None

    def raw_payload(self) -> dict[str, Any]:
        """写入 ``papers.raw['github']`` 的形状（WP04 直接读 ``stargazers_count``）。"""
        return {
            "owner": self.owner,
            "repo": self.repo,
            "code_url": self.code_url,
            "stargazers_count": self.stargazers_count,
            "forks_count": self.forks_count,
            "open_issues_count": self.open_issues_count,
            "archived": self.archived,
            "html_url": self.html_url,
            "description": self.description,
            "pushed_at": self.pushed_at,
            "license_spdx": self.license_spdx,
            "request_url": self.request_url,
            "http_status": self.http_status,
            "error": self.error,
            "error_kind": self.error_kind,
        }


def parse_owner_repo(code_url: Any) -> tuple[str, str] | None:
    """解析 ``https://github.com/owner/repo`` → ``(owner, repo)``；无法解析返回 None。"""
    if not code_url:
        return None
    match = _GITHUB_URL_RE.search(str(code_url))
    if match is None:
        return None
    owner = match.group("owner")
    repo = re.sub(r"\.git$", "", match.group("repo")).strip(".,;:)]}\"'`")
    if not owner or not repo:
        return None
    if owner.lower() in _RESERVED_PATHS or repo.lower() in _RESERVED_PATHS:
        return None
    return owner, repo


def _to_int(value: Any) -> int | None:
    if value is None or isinstance(value, bool):
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


class GitHubClient:
    """GitHub REST 客户端（只读、容错，不抛异常）。"""

    def __init__(
        self,
        *,
        token: str | None = None,
        base_url: str = GITHUB_API_BASE,
        qps: float | None = None,
        timeout_seconds: float | None = None,
        http: Any | None = None,
    ) -> None:
        settings = get_settings()
        # clean_env_value：.env 里 `GITHUB_TOKEN=   # 注释` 会被 compose 当成值（实测污染成中文注释，
        # 进请求头后 httpx 直接 UnicodeEncodeError，把整轮抓取打挂）
        self.token = clean_env_value(token if token is not None else settings.github_token)
        self.base_url = base_url.rstrip("/")
        headers = ascii_safe_headers(
            {
                "Accept": "application/vnd.github+json",
                "X-GitHub-Api-Version": "2022-11-28",
                "Authorization": f"Bearer {self.token}" if self.token else "",
            }
        )
        self._http = http or get_source_http(
            SOURCE_NAME,
            headers=headers,
            qps=safe_float(qps, GITHUB_QPS),
            timeout_seconds=timeout_seconds,
        )

    @property
    def configured(self) -> bool:
        """是否配置了 token（决定限流预算，不决定功能可用性）。"""
        return bool(self.token)

    async def get_repo(self, code_url: str, *, use_cache: bool = True) -> GitHubRepo | None:
        """取仓库元数据；``code_url`` 解析不出 owner/repo 时返回 None。"""
        parsed = parse_owner_repo(code_url)
        if parsed is None:
            logger.info("github_url_unparsable code_url=%r", code_url)
            return None
        owner, repo = parsed
        url = f"{self.base_url}/repos/{owner}/{repo}"
        result = await self._http.get_json(
            url,
            None,
            headers={"Authorization": f"Bearer {self.token}"} if self.token else None,
            use_cache=use_cache,
            accept_statuses=frozenset({404, 403, 451}),
        )
        return self._to_repo(owner, repo, code_url, result, from_cache=result.from_cache)

    async def get_stars(self, code_url: str, *, use_cache: bool = True) -> int | None:
        """便捷方法：只要 star 数（取不到返回 None）。"""
        repo = await self.get_repo(code_url, use_cache=use_cache)
        return None if repo is None else repo.stargazers_count

    # ---------------------------------------------------------------- 结果归一
    def _to_repo(
        self,
        owner: str,
        repo: str,
        code_url: str,
        result: HttpResult,
        *,
        from_cache: bool,
    ) -> GitHubRepo:
        item = GitHubRepo(
            owner=owner,
            repo=repo,
            code_url=code_url,
            request_url=result.request_url,
            http_status=result.status_code,
            http_headers=dict(result.headers),
            from_cache=from_cache,
        )
        if result.status_code == 404:
            item.error, item.error_kind = "repo_not_found", "not_found"
            return item
        if result.status_code in (403, 429):
            limited = result.headers.get("x-ratelimit-remaining") == "0"
            item.error = (
                "rate_limited" if limited or result.status_code == 429 else "access_forbidden"
            )
            item.error_kind = "rate_limited" if item.error == "rate_limited" else "forbidden"
            item.raw = {"message": self._message(result)}
            return item
        if not result.ok or not isinstance(result.payload, Mapping):
            item.error = result.error or "unavailable"
            item.error_kind = result.error_kind or "unavailable"
            return item

        payload = result.payload
        license_info = payload.get("license") or {}
        item.stargazers_count = _to_int(payload.get("stargazers_count"))
        item.forks_count = _to_int(payload.get("forks_count"))
        item.open_issues_count = _to_int(payload.get("open_issues_count"))
        item.archived = (
            payload.get("archived") if isinstance(payload.get("archived"), bool) else None
        )
        item.html_url = payload.get("html_url")
        item.description = payload.get("description")
        item.pushed_at = payload.get("pushed_at")
        item.license_spdx = (
            license_info.get("spdx_id") if isinstance(license_info, Mapping) else None
        )
        item.raw = {
            "id": payload.get("id"),
            "full_name": payload.get("full_name"),
            "stargazers_count": item.stargazers_count,
            "forks_count": item.forks_count,
            "subscribers_count": _to_int(payload.get("subscribers_count")),
            "created_at": payload.get("created_at"),
            "pushed_at": item.pushed_at,
            "archived": item.archived,
        }
        return item

    @staticmethod
    def _message(result: HttpResult) -> str | None:
        try:
            payload = result.payload
        except Exception:  # noqa: BLE001 - payload 已解析好，这里只做尽力而为
            return None
        if isinstance(payload, Mapping):
            message = payload.get("message")
            return str(message) if message else None
        return None


__all__ = [
    "GITHUB_API_BASE",
    "GITHUB_QPS",
    "SOURCE_NAME",
    "GitHubClient",
    "GitHubRepo",
    "parse_owner_repo",
]
