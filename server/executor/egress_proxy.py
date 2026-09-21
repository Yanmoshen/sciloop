# Copyright 2026 SciLoop contributors
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
"""出网白名单代理（WP11-T1 / ``contracts.executor_limits``）。

规则（硬约束，**不可由模板参数或 LLM 覆盖**）：

1. 目标主机必须显式落在 ``EXECUTOR_ALLOWED_HOSTS``（或其子域）内，否则拒绝
2. ``EXECUTOR_DENY_PRIVATE_NETWORK=true`` 时拒绝字面量私网/回环/链路本地/保留地址、
   ``localhost``、``*.local`` / ``*.internal`` 别名
3. 无 DNS 解析（避免执行期网络依赖）：判定只看主机名字面量；
   域名是否指向内网由运维在配置白名单时保证（配置即信任边界）

每次判定都会记入进程内环形缓冲（``recent_decisions``），供验收与审计核对。
"""

from __future__ import annotations

import ipaddress
import logging
from collections import deque
from collections.abc import Iterable
from dataclasses import dataclass
from typing import Any
from urllib.parse import urlparse

import httpx

from executor.errors import EgressDenied
from executor.limits import get_limits

logger = logging.getLogger("sciloop.executor.egress")

#: 判定审计（最近 200 条；进程内，非持久化）
_DECISIONS: deque[dict[str, Any]] = deque(maxlen=200)


def host_of(url_or_host: str) -> str:
    """从 URL 或裸主机名提取小写主机名（含端口时去掉端口）。"""
    text = str(url_or_host or "").strip()
    if not text:
        return ""
    if "://" not in text:
        text = f"//{text}"
    try:
        parsed = urlparse(text)
    except ValueError:
        return ""
    return (parsed.hostname or "").lower()


def is_private_host(host: str) -> bool:
    """字面量内网/回环/别名判定（不做 DNS 解析）。"""
    text = str(host or "").strip().lower().strip("[]")
    if not text:
        return False
    if text in {"localhost", "localhost.localdomain", "host.docker.internal", "gateway.docker.internal"}:
        return True
    if text.endswith((".local", ".internal", ".localhost")):
        return True
    try:
        address = ipaddress.ip_address(text)
    except ValueError:
        return False
    return bool(
        address.is_private
        or address.is_loopback
        or address.is_link_local
        or address.is_reserved
        or address.is_multicast
    )


def _host_matches(host: str, allowed: str) -> bool:
    """``allowed`` 精确匹配或作为父域匹配（``api.deepseek.com`` 允许其子域）。"""
    entry = str(allowed).strip().lower()
    if not entry:
        return False
    entry = host_of(entry) or entry
    return host == entry or host.endswith(f".{entry}")


@dataclass(frozen=True)
class EgressDecision:
    """一次出网判定结果（可审计）。"""

    allowed: bool
    host: str
    reason: str
    policy: str

    def to_dict(self) -> dict[str, Any]:
        return {
            "allowed": self.allowed,
            "host": self.host,
            "reason": self.reason,
            "policy": self.policy,
        }


def configured_llm_hosts() -> list[str]:
    """**配置层**声明的 LLM 域名（``LLM_DEFAULT_BASE_URL`` / ``LLM_FALLBACK_BASE_URL``）。

    与 WP10 ``guardrails.egress_whitelist()`` 同口径：出网白名单 = 运维在
    ``EXECUTOR_ALLOWED_HOSTS`` 里显式列出的域名 ∪ 已配置的 LLM 供应商域名。
    这两者都来自**运维配置**，不可由模板参数或 LLM 输出放宽。
    """
    try:
        from core.config import get_settings

        settings = get_settings()
    except Exception:  # noqa: BLE001 - 配置不可用时白名单只剩显式项（fail-closed）
        logger.warning("读取 LLM 域名配置失败，出网白名单仅含 EXECUTOR_ALLOWED_HOSTS")
        return []
    hosts: list[str] = []
    for key in ("llm_default_base_url", "llm_fallback_base_url"):
        host = host_of(str(getattr(settings, key, "") or ""))
        if host:
            hosts.append(host)
    return hosts


class EgressPolicy:
    """白名单 + 禁内网判定器。"""

    name = "wp11_egress_whitelist"

    def __init__(
        self,
        allowed_hosts: Iterable[str],
        *,
        deny_private_network: bool = True,
        llm_hosts: Iterable[str] = (),
    ) -> None:
        self.allowed_hosts: tuple[str, ...] = tuple(
            sorted({str(item).strip().lower() for item in allowed_hosts if str(item).strip()})
        )
        self.deny_private_network = bool(deny_private_network)
        self.llm_hosts: tuple[str, ...] = tuple(
            sorted({str(item).strip().lower() for item in llm_hosts if str(item).strip()})
        )

    # ------------------------------------------------------------------ #
    @classmethod
    def from_limits(cls) -> EgressPolicy:
        limits = get_limits()
        llm_hosts = configured_llm_hosts()
        merged = list(limits.allowed_hosts) + llm_hosts
        return cls(
            merged, deny_private_network=limits.deny_private_network, llm_hosts=llm_hosts
        )

    def check(self, target: str) -> EgressDecision:
        """判定单个目标（URL 或主机名）。"""
        host = host_of(target) or str(target or "").strip().lower()
        if not host:
            decision = EgressDecision(False, "", "empty_host", self.name)
        elif not any(_host_matches(host, entry) for entry in self.allowed_hosts):
            decision = EgressDecision(False, host, "not_in_egress_whitelist", self.name)
        elif self.deny_private_network and is_private_host(host):
            # 白名单里写了内网别名/私网 IP 也不放行：契约要求 ``deny_private_network=true``
            decision = EgressDecision(False, host, "private_network_denied", self.name)
        else:
            decision = EgressDecision(True, host, "allowed", self.name)
        _DECISIONS.append(
            {
                **decision.to_dict(),
                "requested": str(target or ""),
                "deny_private_network": self.deny_private_network,
            }
        )
        if not decision.allowed:
            logger.warning(
                "出网被拒 host=%s reason=%s policy=%s", decision.host, decision.reason, self.name
            )
        return decision

    def require(self, target: str) -> str:
        """判定并要求放行；被拒时抛 :class:`EgressDenied`（附白名单，便于排障）。"""
        decision = self.check(target)
        if not decision.allowed:
            raise EgressDenied(
                f"出网目标被拒（{decision.reason}）：{target!r}。"
                f"当前白名单={list(self.allowed_hosts)}，"
                f"deny_private_network={self.deny_private_network}",
                detail={
                    **decision.to_dict(),
                    "whitelist": list(self.allowed_hosts),
                    "deny_private_network": self.deny_private_network,
                    "env_key": "EXECUTOR_ALLOWED_HOSTS",
                },
            )
        return decision.host

    def require_all(self, targets: Iterable[str]) -> list[str]:
        """批量判定（任一无权即抛错，返回放行主机列表）。"""
        granted: list[str] = []
        for target in targets:
            granted.append(self.require(target))
        return granted

    def report(self) -> dict[str, Any]:
        return {
            "policy": self.name,
            "allowed_hosts": list(self.allowed_hosts),
            "allowed_hosts_sources": {
                "EXECUTOR_ALLOWED_HOSTS": [
                    host for host in self.allowed_hosts if host not in set(self.llm_hosts)
                ],
                "LLM_DEFAULT_BASE_URL/LLM_FALLBACK_BASE_URL": list(self.llm_hosts),
            },
            "deny_private_network": self.deny_private_network,
            "env_keys": [
                "EXECUTOR_ALLOWED_HOSTS",
                "EXECUTOR_DENY_PRIVATE_NETWORK",
                "LLM_DEFAULT_BASE_URL",
                "LLM_FALLBACK_BASE_URL",
            ],
        }


def get_policy() -> EgressPolicy:
    """按当前配置构造策略（每次重新读取环境，便于验收按进程覆盖）。"""
    return EgressPolicy.from_limits()


def recent_decisions(limit: int = 20) -> list[dict[str, Any]]:
    """最近若干条判定记录（验收 A4 的证据来源）。"""
    return list(_DECISIONS)[-max(1, int(limit)) :]


async def resolve_model_hosts(model_refs: Iterable[str]) -> dict[str, str]:
    """解析 ``provider:model_id`` 的 base_url 主机（用于执行前出网预检）。

    依赖 WP02 的路由层；解析不到时**如实返回空**，由调用方决定失败或降级。
    """
    from llm.router import get_router

    router = get_router()
    hosts: dict[str, str] = {}
    for ref in model_refs:
        try:
            resolved = await router.resolve_explicit(str(ref))
        except Exception as exc:  # noqa: BLE001 - 解析失败即视为不可用（不猜测）
            logger.warning("模型无法解析 model_ref=%s: %s", ref, exc)
            continue
        host = host_of(str(getattr(resolved, "base_url", "") or ""))
        if host:
            hosts[str(ref)] = host
    return hosts


async def guarded_request(
    method: str,
    url: str,
    *,
    max_bytes: int | None = None,
    timeout_seconds: float = 20.0,
    **kwargs: Any,
) -> httpx.Response:
    """受控出网请求：先判白名单，再限制超时与响应体大小。

    模板**只能**通过本函数做 HTTP 出网（LLM 调用除外，由 WP02 适配层负责）。
    """
    policy = get_policy()
    policy.require(url)
    limits = get_limits()
    cap = int(max_bytes if max_bytes is not None else limits.max_output_bytes)
    timeout = httpx.Timeout(min(float(timeout_seconds), float(limits.run_timeout_seconds)))

    async with (
        httpx.AsyncClient(timeout=timeout, follow_redirects=False) as client,
        client.stream(method, url, **kwargs) as response,
    ):
        chunks: list[bytes] = []
        total = 0
        async for chunk in response.aiter_bytes():
            total += len(chunk)
            if total > cap:
                raise EgressDenied(
                    f"响应体超过执行器上限（>{cap} bytes）：{url}",
                    detail={"url": url, "max_bytes": cap, "received_bytes": total},
                )
            chunks.append(chunk)
        return httpx.Response(
            status_code=response.status_code,
            headers=response.headers,
            content=b"".join(chunks),
            request=response.request,
        )


__all__ = [
    "EgressDecision",
    "EgressPolicy",
    "configured_llm_hosts",
    "get_policy",
    "guarded_request",
    "host_of",
    "is_private_host",
    "recent_decisions",
    "resolve_model_hosts",
]
