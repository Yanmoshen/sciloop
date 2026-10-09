"""API 层测试的同步驱动外壳。

为什么需要它：Turn 的执行是**后台 asyncio 任务**（``FakeRuntimeHost.start``），
而项目的测试约定是同步用例（容器里不依赖 pytest-asyncio 的隐式循环管理）。
如果每断言一次就 ``asyncio.run`` 一次，后台任务所属的事件循环会被反复关闭，
任务随之夭折，根本测不到「流式推送 -> 中断 -> 审批恢复」这条链路。

因此 :class:`Harness` 自持一个常驻事件循环：
``call`` 走循环执行一次 ``dispatch``，``settle``/``wait_*`` 让循环继续跑一会儿，
**测试代码保持同步可读**。
"""

from __future__ import annotations

import asyncio
import contextlib
import time
from typing import Any

from api.v2.agent.service import AgentV2Service, Connection
from api.v2.agent_protocol import PROTOCOL_VERSION, Request
from api.v2.agent_protocol.idempotency import IdempotencyStore
from contracts.agent_v2.enums import TurnStatus
from contracts.agent_v2.models import Event, Turn


class Harness:
    """同步测试客户端。"""

    def __init__(self, service: AgentV2Service) -> None:
        self.service = service
        self.loop = asyncio.new_event_loop()
        self.conn: Connection = service.connect()
        self.notifications: list[dict[str, Any]] = []
        self._counter = 0

    # ------------------------------------------------------------------ 基础
    def run(self, coro: Any) -> Any:
        return self.loop.run_until_complete(coro)

    def next_request_id(self, prefix: str = "req") -> str:
        self._counter += 1
        return f"{prefix}-{self._counter}"

    def raw(
        self,
        method: str,
        params: dict[str, Any] | None = None,
        *,
        request_id: str | None = None,
        idem: str | None = None,
    ) -> dict[str, Any]:
        """发一条请求，返回响应字典（未做任何断言）。"""
        rid = request_id or self.next_request_id()
        request = Request(
            id=rid,
            method=method,
            params=dict(params or {}),
            idempotency_key=idem,
            contract=PROTOCOL_VERSION,
        )
        ctx = self.service.connect(self.conn.connection_id).context()
        ctx.request_id = rid
        response = self.run(self.service.facade.dispatch(request, ctx))
        self.notifications.extend(note.to_dict() for note in ctx.notifications)
        self.collect()
        return response.to_dict()

    def call(
        self,
        method: str,
        params: dict[str, Any] | None = None,
        *,
        request_id: str | None = None,
        idem: str | None = None,
    ) -> dict[str, Any]:
        """发一条请求并要求成功，返回 ``result``。"""
        payload = self.raw(method, params, request_id=request_id, idem=idem)
        assert payload["ok"] is True, payload.get("error")
        assert payload["result"] is not None
        return payload["result"]

    def fails(
        self,
        method: str,
        params: dict[str, Any] | None = None,
        *,
        expect: str | None = None,
        request_id: str | None = None,
        idem: str | None = None,
    ) -> dict[str, Any]:
        """发一条请求并要求失败，返回结构化错误字典。"""
        payload = self.raw(method, params, request_id=request_id, idem=idem)
        assert payload["ok"] is False, payload
        error = payload["error"]
        assert error and isinstance(error.get("code"), str)
        if expect is not None:
            assert error["code"] == expect, error
        return error

    # ------------------------------------------------------------------ 推送
    def collect(self) -> list[dict[str, Any]]:
        """把订阅线程的新事件收成通知（模拟 WebSocket 的推送节拍）。"""
        self.service.pump(self.conn)
        for note in self.service.drain_outbound(self.conn):
            self.notifications.append(note.to_dict())
        return self.notifications

    def settle(self, seconds: float = 0.05) -> None:
        """让常驻循环跑一会儿，后台 Turn 任务得以推进。"""
        self.loop.run_until_complete(asyncio.sleep(seconds))
        self.collect()

    def events_notifications(self, thread_id: str | None = None) -> list[dict[str, Any]]:
        return [
            note
            for note in self.notifications
            if note["method"] == "event" and (thread_id is None or note["thread_id"] == thread_id)
        ]

    def notifications_of(self, method: str) -> list[dict[str, Any]]:
        return [note for note in self.notifications if note["method"] == method]

    def clear_notifications(self) -> None:
        self.notifications.clear()

    def subscribe(self, thread_id: str, *, after_sequence: int = 0) -> dict[str, Any]:
        return self.call(
            "thread/subscribe", {"thread_id": thread_id, "after_sequence": after_sequence}
        )

    # -------------------------------------------------------------- 事件流视图
    def events(self, thread_id: str) -> list[Event]:
        return self.service.repo.store(thread_id).read_all()

    def event_types(self, thread_id: str) -> list[str]:
        return [event.type for event in self.events(thread_id)]

    def sequences(self, thread_id: str) -> list[int]:
        return [event.sequence for event in self.events(thread_id)]

    def find_events(self, thread_id: str, *types: str) -> list[Event]:
        wanted = set(types)
        return [event for event in self.events(thread_id) if event.type in wanted]

    def last_sequence(self, thread_id: str) -> int:
        return self.service.repo.store(thread_id).last_sequence()

    def state(self, thread_id: str):
        return self.service.repo.state(thread_id)

    # -------------------------------------------------------------- 生命周期等待
    def wait_status(
        self,
        thread_id: str,
        turn_id: str,
        statuses: set[TurnStatus] | set[str],
        *,
        timeout: float = 8.0,
    ) -> Turn:
        """等到某个 Turn 进入指定状态之一。"""
        wanted = {status.value if hasattr(status, "value") else str(status) for status in statuses}
        deadline = time.monotonic() + timeout
        last: Turn | None = None
        while time.monotonic() < deadline:
            self.settle(0.01)
            state = self.service.repo.state(thread_id)
            last = state.turns.get(turn_id)
            if last is not None and last.status.value in wanted:
                return last
        raise AssertionError(
            f"turn {turn_id} did not reach {sorted(wanted)} within {timeout}s; "
            f"last status = {last.status.value if last else None}"
        )

    def wait_idle(self, thread_id: str, *, timeout: float = 8.0) -> None:
        """等到线程没有活动 Turn（Turn 已进入终态）。"""
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            self.settle(0.01)
            if self.service.repo.state(thread_id).active_turn is None:
                return
        raise AssertionError(f"thread {thread_id} still has an active turn after {timeout}s")

    def wait_sequence(self, thread_id: str, minimum: int, *, timeout: float = 8.0) -> None:
        """等到事件序号至少达到 ``minimum``。"""
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            self.settle(0.01)
            if self.last_sequence(thread_id) >= minimum:
                return
        raise AssertionError(
            f"thread {thread_id} sequence {self.last_sequence(thread_id)} < {minimum}"
        )

    # ------------------------------------------------------------------ 便捷动作
    def start_thread(
        self,
        name: str,
        *,
        scenario: str | None = None,
        idem: str | None = None,
        **extra: Any,
    ) -> dict[str, Any]:
        params: dict[str, Any] = {"name": name}
        if scenario is not None:
            params["settings"] = {"scenario": scenario}
        params.update(extra)
        return self.call(
            "thread/start", params, idem=idem or f"start-{self.next_request_id('thread')}"
        )

    def start_turn(
        self,
        thread_id: str,
        text: str,
        *,
        idem: str | None = None,
        wait: bool = True,
    ) -> dict[str, Any]:
        """启动一个 Turn（默认等到它进入终态或挂起）。"""
        result = self.call(
            "turn/start",
            {"thread_id": thread_id, "text": text},
            idem=idem or f"turn-{self.next_request_id('t')}",
        )
        if wait:
            self.wait_idle(thread_id)
        return result

    def run_turn(self, thread_id: str, text: str, *, idem: str | None = None) -> dict[str, Any]:
        """``start_turn`` 的别名（保留可读性）。"""
        return self.start_turn(thread_id, text, idem=idem)

    # ------------------------------------------------------------------ 关闭
    def close(self) -> None:
        with contextlib.suppress(Exception):
            self.run(self.service.host.shutdown())
        with contextlib.suppress(Exception):
            self.loop.run_until_complete(self.loop.shutdown_asyncgens())
        self.loop.close()


def make_service(**kwargs: Any) -> AgentV2Service:
    """构造一个不落盘在仓库里的服务（少量测试直接用它）。"""
    import tempfile
    from pathlib import Path

    root = Path(tempfile.mkdtemp(prefix="agent-v2-api-"))
    return AgentV2Service(
        conversations_root=root / "conversations",
        memories_root=root / "memories",
        poll_interval_s=0.01,
        heartbeat_s=0.05,
        token_budget=200,
        **kwargs,
    )


def fresh_idempotency() -> IdempotencyStore:
    return IdempotencyStore()


__all__ = ["Harness", "make_service", "fresh_idempotency"]
