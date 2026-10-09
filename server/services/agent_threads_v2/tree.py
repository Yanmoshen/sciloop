"""Agent Tree：根 Thread 与子 Thread 的领域接口（Agent 1 / WP-05）。

计划书 §4.5 的能力清单在这里逐条落地：

- 创建子 Agent（独立 Thread/Turn/Item）；
- 父子关系与路径；
- 父子消息邮箱（带来源与顺序）；
- 等待一个或多个子 Agent；
- 中断子 Agent；
- 子 Agent 结果作为结构化 Item 回传父线程；
- **子 Agent 失败或中断不直接终止父 Agent**——:meth:`AgentTree.wait_for` 只返回结果，
  从不抛"子失败"异常。

具体工具入口（例如 ``spawn_agent`` 工具）由 Agent 2 负责；本模块只提供运行时能力。
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from typing import Any

from contracts.agent_v2.cancellation import CancelToken
from contracts.agent_v2.clock import Clock
from contracts.agent_v2.enums import ItemType
from contracts.agent_v2.errors import ThreadNotFound
from contracts.agent_v2.ids import new_id

from .repository import ThreadRepository

#: 视为「子 Agent 已有结论」的终态。
FINAL_CHILD_STATUSES: frozenset[str] = frozenset({"completed", "failed", "interrupted"})


@dataclass
class ChildOutcome:
    """一个子 Agent 的收敛结果。"""

    thread_id: str
    status: str
    summary: str | None = None
    result_item_id: str | None = None
    error: dict[str, Any] | None = None

    @property
    def ok(self) -> bool:
        return self.status == "completed"

    def to_dict(self) -> dict[str, Any]:
        return {
            "thread_id": self.thread_id,
            "status": self.status,
            "summary": self.summary,
            "result_item_id": self.result_item_id,
            "error": self.error,
        }


class AgentTree:
    """父子 Agent 的结构与通信。"""

    def __init__(self, repo: ThreadRepository, *, clock: Clock | None = None) -> None:
        self.repo = repo
        self.clock = clock or repo.clock
        self._tokens: dict[str, Any] = {}

    # ------------------------------------------------------------------ 结构
    def create_child(
        self,
        parent_thread_id: str,
        name: str,
        *,
        settings: dict[str, Any] | None = None,
        cwd: str | None = None,
        model: str | None = None,
    ) -> Any:
        """创建子 Agent（独立 Thread），并在父线程里登记父子关系。"""
        parent_state = self.repo.state(parent_thread_id)
        parent_path = list(parent_state.thread.path) or [parent_thread_id]
        child_id = new_id("thread")
        self.repo.create_thread(
            name,
            settings=settings,
            cwd=cwd,
            model=model or parent_state.thread.model,
            parent_thread_id=parent_thread_id,
            path=[*parent_path, child_id],
            thread_id=child_id,
        )
        child = self.repo.state(child_id).thread
        self.repo.emit_event(
            parent_thread_id,
            "agent/child_created",
            payload={
                "child_thread_id": child.thread_id,
                "name": child.name,
                "path": child.path,
            },
        )
        return child

    def children(self, thread_id: str) -> list[str]:
        return list(self.repo.state(thread_id).children)

    def path(self, thread_id: str) -> list[str]:
        return list(self.repo.state(thread_id).thread.path)

    def parent_of(self, thread_id: str) -> str | None:
        return self.repo.state(thread_id).thread.parent_thread_id

    def is_ancestor(self, ancestor_id: str, thread_id: str) -> bool:
        return ancestor_id in (self.repo.state(thread_id).thread.path or [])

    # ------------------------------------------------------------------ 邮箱
    def send_message(
        self,
        from_thread_id: str,
        to_thread_id: str,
        content: str,
        *,
        kind: str = "message",
        idempotency_key: str | None = None,
    ) -> dict[str, Any]:
        """投递消息到**收件线程**的邮箱；事件里记录来源与顺序。"""
        if not self.repo.exists(to_thread_id):
            raise ThreadNotFound(f"recipient thread {to_thread_id} not found")
        event = self.repo.emit_event(
            to_thread_id,
            "agent/message",
            payload={
                "from_thread_id": from_thread_id,
                "to_thread_id": to_thread_id,
                "content": content,
                "kind": kind,
            },
            idempotency_key=idempotency_key,
        )
        return {
            "sequence": event.sequence,
            "created_at": event.created_at,
            "from_thread_id": from_thread_id,
            "to_thread_id": to_thread_id,
            "content": content,
            "kind": kind,
        }

    def mailbox(self, thread_id: str, *, after_sequence: int = 0) -> list[dict[str, Any]]:
        """收件线程的邮箱（按事件顺序）。"""
        return [m for m in self.repo.state(thread_id).mailbox if m["sequence"] > after_sequence]

    def reply(self, child_thread_id: str, content: str, **kw: Any) -> dict[str, Any]:
        """子 Agent 回复父 Agent。"""
        parent = self.parent_of(child_thread_id)
        if parent is None:
            raise ThreadNotFound(f"thread {child_thread_id} has no parent")
        return self.send_message(child_thread_id, parent, content, **kw)

    # ------------------------------------------------------------------ 等待 / 中断
    def child_status(self, child_thread_id: str) -> str:
        """子 Agent 的收敛状态：终态取自最后一个 Turn，空闲无 Turn 记为 idle。"""
        state = self.repo.state(child_thread_id)
        active = state.active_turn
        if active is not None:
            return active.status.value
        if state.turn_order:
            return state.turns[state.turn_order[-1]].status.value
        return "idle"

    async def wait_for(
        self,
        thread_ids: list[str],
        *,
        timeout_s: float | None = None,
        poll_s: float = 0.005,
    ) -> dict[str, ChildOutcome]:
        """等待一个或多个子 Agent 收敛。

        **不因某个子 Agent 失败/中断而中止等待**，也从不抛出子失败异常：
        所有结果都以 :class:`ChildOutcome` 形式返回，由父 Agent 自行决策。
        """
        remaining = list(dict.fromkeys(thread_ids))
        loop = asyncio.get_running_loop()
        deadline = None if timeout_s is None else loop.time() + float(timeout_s)
        outcomes: dict[str, ChildOutcome] = {}
        while remaining:
            for child_id in list(remaining):
                status = self.child_status(child_id)
                if status in FINAL_CHILD_STATUSES:
                    outcomes[child_id] = self._outcome(child_id, status)
                    remaining.remove(child_id)
                elif status == "idle" and self._has_result(child_id):
                    # 子线程没有 Turn 但已经把结果回传（例如直接 report_result）
                    outcomes[child_id] = self._outcome(child_id, "completed")
                    remaining.remove(child_id)
            if not remaining:
                break
            if deadline is not None and loop.time() >= deadline:
                for child_id in remaining:
                    outcomes[child_id] = ChildOutcome(
                        thread_id=child_id,
                        status="timeout",
                        error={"code": "wait_timeout", "message": "child did not converge in time"},
                    )
                break
            await asyncio.sleep(poll_s)
        return outcomes

    def interrupt_child(self, child_thread_id: str, reason: str = "parent_interrupt") -> None:
        """中断子 Agent 的当前活动 Turn，并通知父 Agent（父 Agent 状态不变）。

        中断分两路下发，二者都需要：

        1. **取消令牌**——让正在执行的工具/模型流立刻收到取消信号
           （执行器接口据此向真实进程发终止指令）；
        2. **领域状态**——把活动 Turn 置为 ``interrupted``，保证不产生成功事件。
        """
        if not self.cancel(child_thread_id, reason):
            state = self.repo.state(child_thread_id)
            turn = state.active_turn
            if turn is not None:
                self.repo.interrupt_turn(child_thread_id, turn.turn_id, reason=reason)
        parent = self.parent_of(child_thread_id)
        if parent:
            self.repo.emit_event(
                parent,
                "agent/child_interrupted",
                payload={"child_thread_id": child_thread_id, "reason": reason},
            )

    # ------------------------------------------------------------------ 取消令牌登记
    def register_cancel(self, thread_id: str, token: CancelToken) -> None:
        """登记某个线程正在使用的取消令牌（由驱动该线程的一方调用）。"""
        self._tokens[thread_id] = token

    def unregister_cancel(self, thread_id: str) -> None:
        self._tokens.pop(thread_id, None)

    def cancel(self, thread_id: str, reason: str = "cancelled") -> bool:
        """向已登记的令牌发取消信号。返回是否存在令牌。"""
        token = self._tokens.get(thread_id)
        if token is None:
            return False
        token.cancel(reason)
        return True

    def report_result(
        self,
        child_thread_id: str,
        *,
        summary: str,
        status: str = "completed",
        error: dict[str, Any] | None = None,
        idempotency_key: str | None = None,
    ) -> Any:
        """把子 Agent 结果作为**结构化 Item** 回传父线程。"""
        parent = self.parent_of(child_thread_id)
        if parent is None:
            raise ThreadNotFound(f"thread {child_thread_id} has no parent")
        event_type = {
            "completed": "agent/child_completed",
            "failed": "agent/child_failed",
            "interrupted": "agent/child_interrupted",
        }.get(status, "agent/child_completed")
        item = self.repo.add_item(
            parent,
            ItemType.SUBAGENT_RESULT,
            {
                "child_thread_id": child_thread_id,
                "status": status,
                "summary": summary,
                "error": error,
            },
            subagent_thread_id=child_thread_id,
            idempotency_key=idempotency_key,
        )
        self.repo.emit_event(
            parent,
            event_type,
            payload={
                "child_thread_id": child_thread_id,
                "status": status,
                "summary": summary,
                "item_id": item.item_id,
            },
        )
        return item

    # ------------------------------------------------------------------ 内部
    def _has_result(self, child_thread_id: str) -> bool:
        parent = self.parent_of(child_thread_id)
        if parent is None:
            return False
        return any(
            item.type is ItemType.SUBAGENT_RESULT
            and item.subagent_thread_id == child_thread_id
            for item in self.repo.state(parent).items_of_type(ItemType.SUBAGENT_RESULT)
        )

    def _outcome(self, child_thread_id: str, status: str) -> ChildOutcome:
        parent = self.parent_of(child_thread_id)
        summary = None
        item_id = None
        error = None
        if parent is not None:
            for item in self.repo.state(parent).items_of_type(ItemType.SUBAGENT_RESULT):
                if item.subagent_thread_id == child_thread_id:
                    summary = item.payload.get("summary")
                    item_id = item.item_id
                    error = item.payload.get("error")
        if summary is None:
            state = self.repo.state(child_thread_id)
            if state.turn_order:
                last = state.turns[state.turn_order[-1]]
                if last.error:
                    error = last.error
                summary = state.assistant_text(last.turn_id) or None
        return ChildOutcome(
            thread_id=child_thread_id,
            status=status,
            summary=summary,
            result_item_id=item_id,
            error=error,
        )


__all__ = ["AgentTree", "ChildOutcome", "FINAL_CHILD_STATUSES"]
