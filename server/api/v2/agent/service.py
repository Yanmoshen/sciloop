# Copyright 2026 SciLoop contributors
# Licensed under the Apache License, Version 2.0 (the "License");
"""Agent v2 服务装配与生命周期。

一个进程只需一个 :class:`AgentV2Service`。它把领域对象（Agent 1 的仓库 / 树 / 记忆，
本包的离线运行时宿主）装在一起，向上提供：

- :meth:`AgentV2Service.new_call_context` —— 每个 WebSocket 连接一个上下文；
- :meth:`AgentV2Service.pump` —— 把订阅线程的新事件推成通知（游标由此单调前进）；
- :meth:`AgentV2Service.shutdown` —— 关闭语义：拒绝新请求 + 给所有连接发
  ``server/shutting_down`` + 取消运行中的任务。

存储根默认 ``knowledge-base/conversations``、记忆根默认 ``knowledge-base/memories``
（与冻结契约 §4 的落盘布局一致）。
"""

from __future__ import annotations

import asyncio
import contextlib
from collections.abc import AsyncIterator
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from api.v2.agent_protocol import (
    PROTOCOL_VERSION,
    Notification,
    SubscriptionRegistry,
    control_notification,
    notification_from_event,
)
from api.v2.agent_protocol.idempotency import IdempotencyStore
from contracts.agent_v2.clock import Clock, SystemClock
from contracts.agent_v2.models import ModelRequest, StreamItem, TextDelta
from contracts.agent_v2.version import CONTRACT_FREEZE_TAG, CONTRACT_VERSION
from services.agent_compaction_v2 import CompactionService
from services.agent_memory_v2 import MemoryStore
from services.agent_threads_v2 import AgentTree, ThreadRepository
from services.model_gateway_v2 import ModelGateway

from .facade import DEFAULT_REPLAY_LIMIT, AgentFacade, CallContext
from .host import FakeRuntimeHost

#: 长轮询间隔：接收超时后做一次「推送 + 心跳」。
DEFAULT_POLL_INTERVAL_S = 0.05

#: 心跳间隔（秒）。
DEFAULT_HEARTBEAT_S = 30.0

#: 一次推送最多投递多少条事件（超出由客户端继续拉取）。
DEFAULT_PUMP_LIMIT = 200


class SummarizerProvider:
    """确定性假摘要供应商（压缩专用，不依赖 fixture）。

    压缩必须真实走 ``ModelGateway``，但在离线环境里没有真模型；这里用一个**可复现**
    的摘要器：把待压缩对话的规模与开头内容压成一条有界的结构化摘要。
    接入真实模型时只需换掉这个 provider。
    """

    def __init__(self, *, max_chars: int = 400) -> None:
        self.max_chars = int(max_chars)
        self.calls: list[ModelRequest] = []

    @property
    def call_count(self) -> int:
        return len(self.calls)

    async def stream(
        self, request: ModelRequest, cancel: Any = None
    ) -> AsyncIterator[StreamItem]:
        self.calls.append(request)
        body = ""
        for message in request.messages:
            if message.get("role") == "user":
                body = str(message.get("content") or "")
        if cancel is not None and getattr(cancel, "cancelled", False):
            return
        size = len(body)
        head = " ".join(body.split())[: self.max_chars]
        summary = (
            f"【自动摘要】压缩覆盖约 {size} 字符的既有上下文。"
            f"要点保留：{head}"
        )
        await asyncio.sleep(0)
        yield TextDelta(text=summary)


@dataclass
class Connection:
    """一个 WebSocket 连接的服务端视图。"""

    connection_id: str
    subscriptions: SubscriptionRegistry = field(default_factory=SubscriptionRegistry)
    outbound: list[Notification] = field(default_factory=list)
    sequence: int = 0
    closed: bool = False
    received: int = 0
    sent: int = 0

    def next_sequence(self) -> int:
        self.sequence += 1
        return self.sequence

    def context(self) -> CallContext:
        return CallContext(
            connection_id=self.connection_id,
            subscriptions=self.subscriptions,
            next_connection_sequence=self.next_sequence,
        )


class AgentV2Service:
    """v2 agent 服务（进程单例）。"""

    def __init__(
        self,
        *,
        conversations_root: str | Path,
        memories_root: str | Path | None = None,
        fixtures_dir: str | Path | None = None,
        clock: Clock | None = None,
        default_scenario: str | None = None,
        system_prompt: str | None = None,
        token_budget: int = 6000,
        poll_interval_s: float = DEFAULT_POLL_INTERVAL_S,
        heartbeat_s: float = DEFAULT_HEARTBEAT_S,
        pump_limit: int = DEFAULT_PUMP_LIMIT,
    ) -> None:
        self.clock = clock or SystemClock()
        self.conversations_root = Path(conversations_root)
        base = self.conversations_root.parent
        self.memories_root = Path(memories_root) if memories_root else base / "memories"
        self.poll_interval_s = float(poll_interval_s)
        self.heartbeat_s = float(heartbeat_s)
        self.pump_limit = int(pump_limit)
        self.started_at = self.clock.now_iso()

        self.repo = ThreadRepository(self.conversations_root, clock=self.clock)
        self.tree = AgentTree(self.repo, clock=self.clock)
        self.host = FakeRuntimeHost(
            repo=self.repo,
            tree=self.tree,
            clock=self.clock,
            fixtures_dir=Path(fixtures_dir) if fixtures_dir else None,
            default_scenario=default_scenario,
            system_prompt=system_prompt,
            token_budget=token_budget,
        )
        self.memory = MemoryStore(self.memories_root, clock=self.clock)
        self.summarizer = SummarizerProvider()
        self.compaction = CompactionService(
            repo=self.repo,
            gateway=ModelGateway(self.summarizer),
            clock=self.clock,
            token_budget=token_budget,
            system_prompt=system_prompt,
        )
        self.idempotency = IdempotencyStore()
        self.facade = AgentFacade(
            repo=self.repo,
            tree=self.tree,
            host=self.host,
            compaction=self.compaction,
            memory=self.memory,
            idempotency=self.idempotency,
            clock=self.clock,
            closing=lambda: self.shutting_down,
        )
        self._connections: dict[str, Connection] = {}
        self._closing = False
        self._last_heartbeat = self.clock.now_ms()
        self._connection_counter = 0

    # ------------------------------------------------------------------ 状态
    @property
    def shutting_down(self) -> bool:
        return self._closing

    @property
    def connections(self) -> list[Connection]:
        return list(self._connections.values())

    def describe(self) -> dict[str, Any]:
        return {
            "protocol_version": PROTOCOL_VERSION,
            "contract_version": CONTRACT_VERSION,
            "contract_freeze_tag": CONTRACT_FREEZE_TAG,
            "host": "fake",
            "shutting_down": self._closing,
            "started_at": self.started_at,
            "conversations_root": str(self.conversations_root),
            "memories_root": str(self.memories_root),
            "threads": len(self.repo.list_threads()),
            "connections": len(self._connections),
            "scenarios": self.host.scenario_names(),
            "poll_interval_s": self.poll_interval_s,
            "heartbeat_s": self.heartbeat_s,
            "default_replay_limit": DEFAULT_REPLAY_LIMIT,
        }

    # -------------------------------------------------------------- 连接管理
    def new_call_context(self, connection_id: str | None = None) -> CallContext:
        return self.connect(connection_id).context()

    def next_connection_id(self) -> str:
        """分配一个新的连接 id（HTTP 单请求通道用，避免连接之间互相串订阅）。"""
        self._connection_counter += 1
        return f"conn-{self._connection_counter}"

    def connect(self, connection_id: str | None = None) -> Connection:
        cid = connection_id or self.next_connection_id()
        conn = self._connections.get(cid) or Connection(connection_id=cid)
        self._connections[cid] = conn
        return conn

    def disconnect(self, connection_id: str) -> list[str]:
        """断开连接：清空订阅，返回被取消订阅的线程清单。"""
        conn = self._connections.pop(connection_id, None)
        if conn is None:
            return []
        conn.closed = True
        return conn.subscriptions.clear()

    # ------------------------------------------------------------------ 推送
    def pump(self, conn: Connection) -> list[Notification]:
        """把订阅线程的新事件转成通知，并推进游标。

        游标失效（历史被重写等）时不静默跳过：发 ``thread/closed`` 明确告知原因，
        并取消该订阅，客户端据此走全量 ``thread/resume`` 重建。
        """
        notes: list[Notification] = []
        for thread_id in conn.subscriptions.subscribed():
            sub = conn.subscriptions.get(thread_id)
            if sub is None:  # pragma: no cover - 防御
                continue
            stale = self._stale_reason(thread_id, sub.cursor)
            if stale is not None:
                conn.subscriptions.unsubscribe(thread_id)
                notes.append(
                    self._control(
                        conn,
                        "thread/closed",
                        thread_id=thread_id,
                        params={"thread_id": thread_id, "reason": stale, "cursor": sub.cursor},
                    )
                )
                continue
            limit = int(sub.limit or DEFAULT_PUMP_LIMIT)
            events = self.repo.store(thread_id).read_cursor(sub.cursor)[:limit]
            for event in events:
                note = notification_from_event(event)
                sub.advance(event.sequence)
                sub.delivered += 1
                notes.append(note)
        conn.outbound.extend(notes)
        return notes

    def _stale_reason(self, thread_id: str, cursor: int) -> str | None:
        store = self.repo.store(thread_id)
        last = store.last_sequence()
        floor = self.facade.replay_floor(thread_id)
        if cursor < floor:
            return "below_replay_floor"
        if cursor > last:
            return "cursor_ahead"
        return None

    def _control(
        self, conn: Connection, method: str, *, thread_id: str | None, params: dict[str, Any]
    ) -> Notification:
        return control_notification(
            method, sequence=conn.next_sequence(), params=params, thread_id=thread_id
        )

    def heartbeat_due(self) -> bool:
        return (self.clock.now_ms() - self._last_heartbeat) >= int(self.heartbeat_s * 1000)

    def heartbeat(self, conn: Connection) -> Notification | None:
        self._last_heartbeat = self.clock.now_ms()
        return self._control(
            conn, "heartbeat", thread_id=None, params={"at": self.clock.now_iso()}
        )

    def drain_outbound(self, conn: Connection) -> list[Notification]:
        """取出待推送队列（含关闭通知等连接级消息）。"""
        notes = list(conn.outbound)
        conn.outbound.clear()
        conn.sent += len(notes)
        return notes

    # ------------------------------------------------------------------ 关闭
    async def shutdown(self, *, reason: str = "server_shutdown") -> list[str]:
        """服务关闭：拒绝新请求、通知所有连接、取消运行中的任务。"""
        self._closing = True
        notified: list[str] = []
        for conn in self._connections.values():
            conn.subscriptions.clear()
            conn.outbound.append(
                self._control(
                    conn,
                    "server/shutting_down",
                    thread_id=None,
                    params={"reason": reason, "at": self.clock.now_iso()},
                )
            )
            notified.append(conn.connection_id)
        await self.host.shutdown()
        return notified

    def reopen(self) -> None:
        """测试与热重启使用：清除关闭标记并重建运行时宿主。"""
        self._closing = False
        self.host = FakeRuntimeHost(
            repo=self.repo,
            tree=self.tree,
            clock=self.clock,
            fixtures_dir=self.host.fixtures_dir,
            default_scenario=self.host.default_scenario,
            system_prompt=self.host.system_prompt,
            token_budget=self.host.token_budget,
        )
        self.facade.host = self.host

    async def aclose(self) -> None:
        with contextlib.suppress(Exception):
            await self.host.shutdown()


# ---------------------------------------------------------------------------- #
# 进程单例
# ---------------------------------------------------------------------------- #
def repo_root() -> Path:
    """仓库根：``server/api/v2/agent/service.py`` 向上四级是 ``server/``。"""
    return Path(__file__).resolve().parents[3].parent


def default_conversations_root() -> Path:
    """默认事件流根目录（冻结契约 §4 的落盘布局）。"""
    return repo_root() / "knowledge-base" / "conversations"


def default_memories_root() -> Path:
    return repo_root() / "knowledge-base" / "memories"


_SERVICE: AgentV2Service | None = None


def get_service(**overrides: Any) -> AgentV2Service:
    """取得（必要时创建）进程单例。"""
    global _SERVICE
    if _SERVICE is None:
        _SERVICE = AgentV2Service(
            conversations_root=overrides.pop("conversations_root", default_conversations_root()),
            memories_root=overrides.pop("memories_root", default_memories_root()),
            **overrides,
        )
    return _SERVICE


def set_service(service: AgentV2Service | None) -> None:
    """注入/清空单例（测试与热重启使用）。"""
    global _SERVICE
    _SERVICE = service


def require_service() -> AgentV2Service:
    """取单例，未初始化即报错（路由工厂用）。"""
    if _SERVICE is None:
        raise RuntimeError("AgentV2Service 尚未初始化；先调用 get_service() 或 mount.install()")
    return _SERVICE


__all__ = [
    "AgentV2Service",
    "Connection",
    "SummarizerProvider",
    "DEFAULT_POLL_INTERVAL_S",
    "DEFAULT_HEARTBEAT_S",
    "DEFAULT_PUMP_LIMIT",
    "repo_root",
    "default_conversations_root",
    "default_memories_root",
    "get_service",
    "set_service",
    "require_service",
]
