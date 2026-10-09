# Copyright 2026 SciLoop contributors
# Licensed under the Apache License, Version 2.0 (the "License");
"""API 层门面：协议方法 -> 领域调用。

边界（计划书 §4.2）：

> API 只负责认证、参数校验、调用 ThreadManager、订阅事件和返回协议结果，不得在
> 路由函数里写模型循环、工具审批或压缩逻辑。

因此本文件里**没有**任何模型循环、工具执行、审批判定或摘要生成代码——它们全部在
Agent 1 的运行时、Agent 2 的工具层与本包的 :class:`~api.v2.agent.host.FakeRuntimeHost`
（离线替身）里。门面只做四件事：

1. 校验参数与 ID（非法 ID / 非法状态 / 过期游标都给结构化错误）；
2. 调用领域对象并把结果转成协议形状；
3. 维护连接级订阅与游标；
4. 处理幂等键，保证断线重试不产生第二次副作用。
"""

from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from typing import Any

from api.v2.agent_protocol import (
    PROTOCOL_VERSION,
    ErrorCode,
    Notification,
    ProtocolError,
    Request,
    Response,
    SubscriptionRegistry,
    control_notification,
    describe_methods,
    method_spec,
    validate_params,
)
from api.v2.agent_protocol.idempotency import GLOBAL_SCOPE, IdempotencyStore
from contracts.agent_v2.clock import Clock, SystemClock
from contracts.agent_v2.enums import (
    ACTIVE_TURN_STATUSES,
    ApprovalStatus,
    EventType,
    MemoryScope,
    ToolCallStatus,
    TurnStatus,
)
from contracts.agent_v2.ids import is_valid_id
from contracts.agent_v2.models import (
    ApprovalRequest,
    Event,
    Item,
    MemoryRecord,
    ToolCall,
    Turn,
)
from contracts.agent_v2.version import CONTRACT_FREEZE_TAG, CONTRACT_VERSION
from services.agent_compaction_v2 import CompactionService
from services.agent_memory_v2 import MemoryStore
from services.agent_threads_v2 import AgentTree, ThreadRepository, ThreadState

from .host import (
    FULL_ACCESS_KEY,
    PERMISSION_RULES_KEY,
    FakeRuntimeHost,
    RuntimeUnavailable,
    normalize_command_prefix,
)

#: 默认重放上限与硬上限（防止一次拉爆连接）。
DEFAULT_REPLAY_LIMIT = 500
MAX_REPLAY_LIMIT = 5000

#: 审批决策 -> （是否批准, 授权作用域）
DECISION_TABLE: dict[str, tuple[bool, str]] = {
    "approve_once": (True, "once"),
    "approve_conversation": (True, "conversation"),
    "full_access": (True, "full_access"),
    "deny": (False, "denied"),
    "cancel": (False, "cancelled"),
}

MEMORY_SCOPES: tuple[str, ...] = tuple(scope.value for scope in MemoryScope)


@dataclass
class CallContext:
    """一次调用的连接上下文（订阅集合 + 待推送通知 + 连接序号）。"""

    connection_id: str = "local"
    subscriptions: SubscriptionRegistry = field(default_factory=SubscriptionRegistry)
    next_connection_sequence: Callable[[], int] = field(default=lambda: 0)
    notifications: list[Notification] = field(default_factory=list)
    request_id: str = ""
    #: 本条请求的客户端幂等键（由 :meth:`AgentFacade.dispatch` 填入）。
    #: 透传到领域层后，Turn 记录携带的就是客户端的键，断线重试的审计线索不会断。
    idempotency_key: str | None = None
    #: 是否具备写权限。只读访问面（public_demo 匿名）为 False，变更类方法会被拒绝。
    owner: bool = True

    def notify(
        self,
        method: str,
        *,
        params: dict[str, Any] | None = None,
        thread_id: str | None = None,
    ) -> Notification:
        note = control_notification(
            method,
            sequence=self.next_connection_sequence(),
            params=params,
            thread_id=thread_id,
        )
        self.notifications.append(note)
        return note


def _thread_scope(params: Mapping[str, Any]) -> str:
    """线程相关方法的幂等作用域（按线程隔离，避免不同会话的键互相干扰）。"""
    thread_id = params.get("thread_id")
    return str(thread_id) if thread_id else GLOBAL_SCOPE


class AgentFacade:
    """协议方法集合。所有 handler 都是 ``async def handler(params, ctx) -> dict``。"""

    def __init__(
        self,
        *,
        repo: ThreadRepository,
        tree: AgentTree,
        host: FakeRuntimeHost,
        compaction: CompactionService,
        memory: MemoryStore,
        idempotency: IdempotencyStore | None = None,
        clock: Clock | None = None,
        closing: Callable[[], bool] | None = None,
    ) -> None:
        self.repo = repo
        self.tree = tree
        self.host = host
        self.compaction = compaction
        self.memory = memory
        self.idempotency = idempotency if idempotency is not None else IdempotencyStore()
        self.clock = clock or repo.clock or SystemClock()
        self._closing = closing or (lambda: False)
        #: 每个线程可重放的起始序号（事件流被修复/重写后抬高，用于「游标过期」判定）。
        self._replay_floors: dict[str, int] = {}

    # ================================================================== 调度
    async def dispatch(self, request: Request, ctx: CallContext) -> Response:
        """校验 -> 幂等 -> 执行 -> 记录幂等结果。"""
        if self._closing():
            return Response.failure(request.id, ProtocolError.shutting_down(request.method))
        try:
            spec = method_spec(request.method)
            validate_params(spec, request.params)
            if spec.mutating and not ctx.owner:
                raise ProtocolError.permission_denied(
                    "当前是只读浏览模式，这个操作需要研究者身份。到「设置」里切换后重试。",
                    method=spec.name,
                    required="owner",
                )
            if spec.mutating and not request.idempotency_key:
                raise ProtocolError.invalid_params(
                    f"{spec.name}: mutating method requires idempotency_key",
                    method=spec.name,
                )
            fingerprint = request.fingerprint()
            self._guard_request_id(ctx, request, fingerprint)
            scope = _thread_scope(request.params) if spec.mutating else GLOBAL_SCOPE
            if spec.mutating:
                cached = self.idempotency.lookup(
                    request.idempotency_key, scope=scope, fingerprint=fingerprint
                )
                if cached is not None:
                    self.idempotency.hit(request.idempotency_key, scope=scope)
                    return Response(
                        id=request.id,
                        ok=True,
                        result=dict(cached.result),
                        replayed=True,
                    )
            ctx.request_id = request.id
            ctx.idempotency_key = request.idempotency_key
            handler = getattr(self, spec.handler)
            result = await handler(request.params, ctx)
            if spec.mutating:
                self.idempotency.record(
                    request.idempotency_key,
                    result=result,
                    fingerprint=fingerprint,
                    scope=scope,
                )
            return Response.success(request.id, result)
        except ProtocolError as exc:
            return Response.failure(request.id, exc)
        except RuntimeUnavailable as exc:
            return Response.failure(
                request.id,
                ProtocolError(ErrorCode.RUNTIME_UNAVAILABLE, str(exc), data={"method": request.method}),
            )
        except Exception as exc:  # noqa: BLE001 - 任何异常都必须变成结构化响应，不能断连接
            from api.v2.agent_protocol.errors import error_from_exception

            return Response.failure(request.id, error_from_exception(exc))

    def _guard_request_id(self, ctx: CallContext, request: Request, fingerprint: str) -> None:
        """同一条连接内复用 request id 时，载荷必须完全一致。

        只做一致性校验，**不**缓存只读方法的结果——重复的只读请求照常执行，
        避免客户端拿到过期快照；变更类方法的去重由幂等键负责。
        """
        if not request.id:
            return
        scope = f"__requests__:{ctx.connection_id}"
        entry = self.idempotency.get(request.id, scope=scope)
        if entry is None:
            self.idempotency.record(request.id, result={}, fingerprint=fingerprint, scope=scope)
            return
        if entry.fingerprint != fingerprint:
            raise ProtocolError(
                ErrorCode.IDEMPOTENCY_CONFLICT,
                f"request id {request.id!r} was already used with a different payload "
                f"on connection {ctx.connection_id!r}",
                data={
                    "request_id": request.id,
                    "method": request.method,
                    "connection_id": ctx.connection_id,
                },
            )

    # ================================================================ 校验工具
    def _require_id(self, kind: str, value: Any, *, param: str | None = None) -> str:
        if not is_valid_id(kind, value):
            raise ProtocolError.invalid_id(kind, value, param=param)
        return str(value)

    def _state(self, thread_id: str, *, param: str = "thread_id") -> ThreadState:
        checked = self._require_id("thread", thread_id, param=param)
        return self.repo.state(checked)

    def _parse_int(
        self, value: Any, *, param: str, minimum: int = 0, maximum: int | None = None
    ) -> int:
        if isinstance(value, bool) or not isinstance(value, int):
            raise ProtocolError.invalid_params(f"{param} must be an integer, got {value!r}")
        if value < minimum:
            raise ProtocolError.invalid_params(f"{param} must be >= {minimum}, got {value}")
        if maximum is not None and value > maximum:
            raise ProtocolError.invalid_params(f"{param} must be <= {maximum}, got {value}")
        return int(value)

    @staticmethod
    def _text_of(params: Mapping[str, Any], key: str = "text") -> str:
        value = params.get(key)
        if value is None:
            return ""
        if not isinstance(value, str):
            raise ProtocolError.invalid_params(f"{key} must be a string")
        return value

    # ================================================================== 视图
    @staticmethod
    def _event_view(event: Event) -> dict[str, Any]:
        return event.to_dict()

    @staticmethod
    def _turn_view(turn: Turn) -> dict[str, Any]:
        return turn.to_dict()

    @staticmethod
    def _item_view(item: Item) -> dict[str, Any]:
        return item.to_dict()

    @staticmethod
    def _call_view(call: ToolCall) -> dict[str, Any]:
        return call.to_dict()

    @staticmethod
    def _approval_view(approval: ApprovalRequest) -> dict[str, Any]:
        return approval.to_dict()

    @staticmethod
    def _memory_view(record: MemoryRecord) -> dict[str, Any]:
        return record.to_dict()

    def _snapshot(self, thread_id: str) -> dict[str, Any]:
        """线程全量视图：刷新页面后据此重建界面（**不调用模型**）。"""
        state = self.repo.state(thread_id)
        return {
            "thread": state.thread.to_dict(),
            "turns": [state.turns[tid].to_dict() for tid in state.turn_order],
            "items": [state.items[iid].to_dict() for iid in state.item_order],
            "tool_calls": [call.to_dict() for call in state.tool_calls.values()],
            "approvals": [ap.to_dict() for ap in state.approvals.values()],
            "compactions": list(state.compactions),
            "active_summary": state.active_compaction(),
            "children": list(state.children),
            "mailbox": list(state.mailbox),
            "active_turn_id": state.thread.active_turn_id,
            "last_sequence": state.last_sequence,
            "runtime": self.host.runtime_info(thread_id),
        }

    # ============================================================== 游标重放
    def _read_cursor(
        self, thread_id: str, after_sequence: int, *, limit: int, call_id: str | None = None
    ) -> dict[str, Any]:
        store = self.repo.store(thread_id)
        last = store.last_sequence()
        floor = self.replay_floor(thread_id)
        if after_sequence < floor:
            raise ProtocolError.cursor_expired(
                after_sequence, last_sequence=last, reason="below_replay_floor", floor=floor
            )
        if after_sequence > last:
            raise ProtocolError.cursor_expired(
                after_sequence, last_sequence=last, reason="cursor_ahead", floor=floor
            )
        events = store.read_cursor(after_sequence)
        if call_id:
            events = [event for event in events if event.call_id == call_id]
        has_more = len(events) > limit
        page = events[:limit]
        return {
            "thread_id": thread_id,
            "events": [self._event_view(event) for event in page],
            "count": len(page),
            "last_sequence": last,
            "next_after": page[-1].sequence if page else after_sequence,
            "has_more": has_more,
            "floor": floor,
        }

    #: 可重放的起始序号（重写/修复事件流后可抬高，用于「游标过期」判定）。
    def set_replay_floor(self, thread_id: str, sequence: int) -> None:
        self._replay_floors[thread_id] = int(sequence)

    def replay_floor(self, thread_id: str) -> int:
        return int(self._replay_floors.get(thread_id, 0))

    # ============================================================== 协议方法
    async def protocol_describe(self, params: Mapping[str, Any], ctx: CallContext) -> dict[str, Any]:
        return {
            "protocol_version": PROTOCOL_VERSION,
            "contract_version": CONTRACT_VERSION,
            "contract_freeze_tag": CONTRACT_FREEZE_TAG,
            "shutting_down": bool(self._closing()),
            "methods": describe_methods(),
            "scenarios": self.host.scenario_names(),
            "limits": {"default_replay_limit": DEFAULT_REPLAY_LIMIT, "max_replay_limit": MAX_REPLAY_LIMIT},
        }

    # ------------------------------------------------------------------ thread
    async def thread_start(self, params: Mapping[str, Any], ctx: CallContext) -> dict[str, Any]:
        name = self._text_of(params, "name").strip()
        if not name:
            raise ProtocolError.invalid_params("thread/start: name must be a non-empty string")
        settings = params.get("settings") or {}
        if not isinstance(settings, dict):
            raise ProtocolError.invalid_params("settings must be an object")
        permission_summary = params.get("permission_summary") or {}
        if not isinstance(permission_summary, dict):
            raise ProtocolError.invalid_params("permission_summary must be an object")
        thread = self.repo.create_thread(
            name,
            settings=dict(settings),
            cwd=params.get("cwd"),
            model=params.get("model"),
            permission_summary=dict(permission_summary),
        )
        state = self.repo.state(thread.thread_id)
        return {
            "thread": thread.to_dict(),
            "last_sequence": state.last_sequence,
            "scenario": self.host.scenario_for(thread.thread_id).name,
        }

    async def thread_list(self, params: Mapping[str, Any], ctx: CallContext) -> dict[str, Any]:
        include_archived = bool(params.get("include_archived") or False)
        limit = self._parse_int(params.get("limit") or 200, param="limit", minimum=1, maximum=1000)
        items: list[dict[str, Any]] = []
        for thread_id in self.repo.list_threads():
            state = self.repo.state(thread_id)
            thread = state.thread
            if thread.status == "archived" and not include_archived:
                continue
            items.append(
                {
                    "thread_id": thread.thread_id,
                    "name": thread.name,
                    "status": thread.status,
                    "cwd": thread.cwd,
                    "model": thread.model,
                    "created_at": thread.created_at,
                    "updated_at": thread.updated_at,
                    "active_turn_id": thread.active_turn_id,
                    "last_sequence": state.last_sequence,
                    "parent_thread_id": thread.parent_thread_id,
                    "forked_from": thread.forked_from,
                    "running": self.host.is_running(thread.thread_id),
                }
            )
        items.sort(key=lambda row: str(row.get("updated_at") or ""), reverse=True)
        return {"threads": items[:limit], "count": len(items)}

    async def thread_resume(self, params: Mapping[str, Any], ctx: CallContext) -> dict[str, Any]:
        thread_id = self._require_id("thread", params.get("thread_id"))
        snapshot = self._snapshot(thread_id)
        snapshot["floor"] = self.replay_floor(thread_id)
        return snapshot

    async def thread_settings_update(
        self, params: Mapping[str, Any], ctx: CallContext
    ) -> dict[str, Any]:
        thread_id = self._require_id("thread", params.get("thread_id"))
        patch: dict[str, Any] = {}
        for key in ("cwd", "model"):
            if params.get(key) is not None:
                patch[key] = params[key]
        for key in ("settings", "permission_summary"):
            incoming = params.get(key)
            if incoming is None:
                continue
            if not isinstance(incoming, dict):
                raise ProtocolError.invalid_params(f"{key} must be an object")
            current = dict(getattr(self.repo.state(thread_id).thread, key) or {})
            current.update(incoming)
            patch[key] = current
        if not patch:
            raise ProtocolError.invalid_params(
                "thread/settings/update: nothing to update",
                allowed=["cwd", "model", "settings", "permission_summary"],
            )
        self.repo.emit_event(thread_id, EventType.THREAD_UPDATED, payload={"patch": patch})
        state = self.repo.state(thread_id)
        return {"thread": state.thread.to_dict(), "patch": patch}

    async def thread_delete(self, params: Mapping[str, Any], ctx: CallContext) -> dict[str, Any]:
        thread_id = self._require_id("thread", params.get("thread_id"))
        state = self.repo.state(thread_id)
        if state.thread.status != "archived":
            self.repo.emit_event(
                thread_id, EventType.THREAD_ARCHIVED, payload={"reason": "user_deleted"}
            )
        state = self.repo.state(thread_id)
        ctx.notify(
            "thread/closed",
            thread_id=thread_id,
            params={"thread_id": thread_id, "reason": "deleted", "status": state.thread.status},
        )
        return {"thread": state.thread.to_dict(), "archived": True}

    # -------------------------------------------------------------------- turn
    async def turn_start(self, params: Mapping[str, Any], ctx: CallContext) -> dict[str, Any]:
        thread_id = self._require_id("thread", params.get("thread_id"))
        inputs = self._collect_inputs(params)
        before = set(self.repo.state(thread_id).turns)
        turn = self.repo.start_turn(
            thread_id,
            inputs=inputs,
            idempotency_key=params.get("idempotency_key") or ctx.idempotency_key
            or self._fallback_key(ctx),
            model=params.get("model"),
        )
        created = turn.turn_id not in before
        if created and turn.status is TurnStatus.RUNNING:
            self._start_host(thread_id, turn.turn_id)
        return {
            "turn": self._turn_view(self.repo.state(thread_id).turn(turn.turn_id)),
            "created": created,
            "reused": not created,
        }

    def _start_host(self, thread_id: str, turn_id: str) -> None:
        """把 Turn 交给执行宿主。

        宿主不可用时（未装配 / 已关闭 / 场景声明不可用），**先把 Turn 收成失败终态**
        再把结构化错误抛出去——否则会留下一个没人执行的 running Turn，
        把线程卡死（后续请求全部 concurrency 冲突）。
        """
        try:
            self.host.start(thread_id, turn_id)
        except RuntimeUnavailable:
            current = self.repo.state(thread_id).turns.get(turn_id)
            if current is not None and current.status in ACTIVE_TURN_STATUSES:
                self.repo.fail_turn(
                    thread_id,
                    turn_id,
                    error={
                        "code": "runtime_unavailable",
                        "error_class": "fatal",
                        "message": "运行时不可用，这一轮没有开始执行。",
                    },
                )
            raise

    def _collect_inputs(self, params: Mapping[str, Any]) -> list[dict[str, Any]]:
        raw_inputs = params.get("inputs")
        inputs: list[dict[str, Any]] = []
        if raw_inputs is not None:
            if not isinstance(raw_inputs, list):
                raise ProtocolError.invalid_params("inputs must be an array")
            for entry in raw_inputs:
                if isinstance(entry, str):
                    inputs.append({"text": entry})
                elif isinstance(entry, dict):
                    inputs.append({"text": str(entry.get("text") or "")})
                else:
                    raise ProtocolError.invalid_params("inputs entries must be strings or objects")
        text = params.get("text")
        if text is not None:
            if not isinstance(text, str):
                raise ProtocolError.invalid_params("text must be a string")
            if text:
                inputs.append({"text": text})
        if not inputs:
            raise ProtocolError.invalid_params(
                "turn/start: provide a non-empty text or inputs",
                allowed=["text", "inputs"],
            )
        return inputs

    def _fallback_key(self, ctx: CallContext) -> str | None:
        """若调用方漏传幂等键，用请求 id 兜底（同一连接内可重放）。"""
        return f"req:{ctx.request_id}" if ctx.request_id else None

    async def turn_steer(self, params: Mapping[str, Any], ctx: CallContext) -> dict[str, Any]:
        thread_id = self._require_id("thread", params.get("thread_id"))
        turn_id = self._require_id("turn", params.get("turn_id"))
        text = self._text_of(params, "text")
        if not text:
            raise ProtocolError.invalid_params("turn/steer: text must be non-empty")
        state = self.repo.state(thread_id)
        turn = state.turn(turn_id)
        self._require_control_ready(thread_id, turn)
        if turn.status is not TurnStatus.RUNNING:
            raise ProtocolError.invalid_state(
                "这个回合当前不接受追加输入。",
                kind="turn_not_running",
                turn_id=turn_id,
                status=turn.status.value,
                allowed=[TurnStatus.RUNNING.value],
            )
        item = self.repo.append_input(
            thread_id, turn_id, text, idempotency_key=ctx.idempotency_key
        )
        self.repo.emit_event(
            thread_id, EventType.INPUT_PROVIDED, payload={"text": text, "item_id": item.item_id},
            turn_id=turn_id,
        )
        state = self.repo.state(thread_id)
        return {"turn": self._turn_view(state.turn(turn_id)), "item": self._item_view(item)}

    async def turn_continue(self, params: Mapping[str, Any], ctx: CallContext) -> dict[str, Any]:
        thread_id = self._require_id("thread", params.get("thread_id"))
        turn_id = self._require_id("turn", params.get("turn_id"))
        text = self._text_of(params, "text")
        state = self.repo.state(thread_id)
        turn = state.turn(turn_id)
        self._require_control_ready(thread_id, turn)
        if turn.status is not TurnStatus.WAITING_INPUT:
            raise ProtocolError.invalid_state(
                "这个回合当前不需要补充输入。",
                kind="turn_not_waiting_input",
                turn_id=turn_id,
                status=turn.status.value,
                allowed=[TurnStatus.WAITING_INPUT.value],
            )
        updated = self.repo.continue_turn(thread_id, turn_id, text)
        if not self.host.is_running(thread_id):
            self._start_host(thread_id, turn_id)
        return {"turn": self._turn_view(updated)}

    def _require_control_ready(self, thread_id: str, turn: Turn) -> None:
        """控制类操作的前置判定：**按状态给出可行动的错误码**，而不是笼统的非法迁移。

        前端据此能直接给出下一步动作（去审批 / 重试 / 恢复 / 直接发新消息）。
        """
        status = turn.status
        if status is TurnStatus.WAITING_APPROVAL:
            pending = [
                approval.approval_id
                for approval in self.repo.state(thread_id).approvals.values()
                if approval.turn_id == turn.turn_id and approval.status is ApprovalStatus.PENDING
            ]
            raise ProtocolError.approval_pending(
                thread_id=thread_id,
                turn_id=turn.turn_id,
                approval_id=pending[0] if pending else None,
            )
        if status in (TurnStatus.INTERRUPTED, TurnStatus.FAILED):
            raise ProtocolError.turn_terminated(
                status.value, turn_id=turn.turn_id, reason=turn.cancel_reason
            )
        if status is TurnStatus.COMPLETED:
            raise ProtocolError.invalid_state(
                "这个回合已经结束，直接发送新消息继续即可。",
                kind="turn_completed",
                turn_id=turn.turn_id,
                status=status.value,
            )

    async def turn_interrupt(self, params: Mapping[str, Any], ctx: CallContext) -> dict[str, Any]:
        thread_id = self._require_id("thread", params.get("thread_id"))
        turn_id = self._require_id("turn", params.get("turn_id"))
        reason = str(params.get("reason") or "interrupted")
        turn = await self._interrupt(thread_id, turn_id, reason)
        return {
            "turn": self._turn_view(turn),
            "interrupted": turn.status is TurnStatus.INTERRUPTED,
            "reason": turn.cancel_reason,
        }

    async def _interrupt(self, thread_id: str, turn_id: str, reason: str) -> Turn:
        """中断收尾：先发取消信号，等运行时落中断事件；仍未收尾时门面兜底。

        语义分层（验收书 §3「重复控制请求不会产生重复状态」）：

        - 已经是 ``interrupted``：**幂等**返回，不再写第二个中断事件；
        - 活动态或 ``queued``：执行中断（``queued -> interrupted`` 由状态机允许）；
        - 终态（``completed`` / ``failed``）：非法迁移，返回结构化错误。
        """
        state = self.repo.state(thread_id)
        turn = state.turn(turn_id)
        if turn.status is TurnStatus.INTERRUPTED:
            return turn
        if turn.status not in ACTIVE_TURN_STATUSES and turn.status is not TurnStatus.QUEUED:
            raise ProtocolError(
                ErrorCode.INVALID_STATE,
                f"turn {turn_id} is {turn.status.value}; nothing to interrupt",
                data={
                    "turn_id": turn_id,
                    "from": turn.status.value,
                    "to": TurnStatus.INTERRUPTED.value,
                    "allowed": sorted(
                        status.value for status in (*ACTIVE_TURN_STATUSES, TurnStatus.QUEUED)
                    ),
                },
            )
        self.host.interrupt(thread_id, reason)
        if self.host.is_running(thread_id):
            await self.host.await_idle(thread_id, timeout_s=5.0)
        state = self.repo.state(thread_id)
        turn = state.turn(turn_id)
        if turn.status in ACTIVE_TURN_STATUSES or turn.status is TurnStatus.QUEUED:
            turn = self.repo.interrupt_turn(thread_id, turn_id, reason=reason)
        return turn

    async def turn_recover(self, params: Mapping[str, Any], ctx: CallContext) -> dict[str, Any]:
        thread_id = self._require_id("thread", params.get("thread_id"))
        turn_id = self._require_id("turn", params.get("turn_id"))
        reason = str(params.get("reason") or "resume")
        state = self.repo.state(thread_id)
        turn = state.turn(turn_id)
        if turn.status is TurnStatus.INTERRUPTED:
            resumed = self.repo.resume_turn(thread_id, turn_id, reason=reason)
            if not self.host.is_running(thread_id):
                self._start_host(thread_id, turn_id)
            return {"turn": self._turn_view(resumed), "mode": "resume", "retried_from": None}
        if turn.status in (TurnStatus.FAILED,):
            inputs = [
                {"text": str(item.payload.get("text", ""))}
                for item in state.items_for_turn(turn_id)
                if item.type.value == "user_input"
            ]
            new_turn = self.repo.start_turn(
                thread_id,
                inputs=inputs or [{"text": ""}],
                idempotency_key=params.get("idempotency_key") or ctx.idempotency_key
                or self._fallback_key(ctx),
            )
            if new_turn.status is TurnStatus.RUNNING:
                self._start_host(thread_id, new_turn.turn_id)
            return {
                "turn": self._turn_view(new_turn),
                "mode": "retry",
                "retried_from": turn_id,
            }
        raise ProtocolError(
            ErrorCode.INVALID_STATE,
            f"turn {turn_id} is {turn.status.value}; recover requires interrupted or failed",
            data={"turn_id": turn_id, "from": turn.status.value,
                  "allowed": [TurnStatus.INTERRUPTED.value, TurnStatus.FAILED.value]},
        )

    # --------------------------------------------------------- subscribe/replay
    async def thread_subscribe(self, params: Mapping[str, Any], ctx: CallContext) -> dict[str, Any]:
        thread_id = self._require_id("thread", params.get("thread_id"))
        after = self._parse_int(params.get("after_sequence") or 0, param="after_sequence", minimum=0)
        limit = self._parse_int(
            params.get("limit") or DEFAULT_REPLAY_LIMIT,
            param="limit",
            minimum=1,
            maximum=MAX_REPLAY_LIMIT,
        )
        last = self.repo.store(thread_id).last_sequence()
        floor = self.replay_floor(thread_id)
        if after < floor:
            raise ProtocolError.cursor_expired(
                after, last_sequence=last, reason="below_replay_floor", floor=floor
            )
        if after > last:
            raise ProtocolError.cursor_expired(
                after, last_sequence=last, reason="cursor_ahead", floor=floor
            )
        sub = ctx.subscriptions.subscribe(
            thread_id, after_sequence=after, limit=limit, now=self.clock.now_iso()
        )
        ctx.notify(
            "subscription/started",
            thread_id=thread_id,
            params={"thread_id": thread_id, "cursor": sub.cursor, "limit": limit},
        )
        return {
            "thread_id": thread_id,
            "cursor": sub.cursor,
            "pending": max(0, last - sub.cursor),
            "last_sequence": last,
        }

    async def thread_unsubscribe(self, params: Mapping[str, Any], ctx: CallContext) -> dict[str, Any]:
        thread_id = self._require_id("thread", params.get("thread_id"))
        removed = ctx.subscriptions.unsubscribe(thread_id)
        if removed is None:
            raise ProtocolError(
                ErrorCode.NOT_SUBSCRIBED,
                f"connection {ctx.connection_id} is not subscribed to thread {thread_id}",
                data={"thread_id": thread_id},
            )
        ctx.notify(
            "subscription/cancelled",
            thread_id=thread_id,
            params={"thread_id": thread_id, "cursor": removed.cursor, "reason": "unsubscribed"},
        )
        return {"thread_id": thread_id, "subscribed": False, "cursor": removed.cursor}

    async def thread_events_replay(
        self, params: Mapping[str, Any], ctx: CallContext
    ) -> dict[str, Any]:
        thread_id = self._require_id("thread", params.get("thread_id"))
        after = self._parse_int(params.get("after_sequence"), param="after_sequence", minimum=0)
        limit = self._parse_int(
            params.get("limit") or DEFAULT_REPLAY_LIMIT,
            param="limit",
            minimum=1,
            maximum=MAX_REPLAY_LIMIT,
        )
        call_id = params.get("call_id")
        if call_id is not None:
            call_id = self._require_id("call", call_id, param="call_id")
        return self._read_cursor(thread_id, after, limit=limit, call_id=call_id)

    # --------------------------------------------------------------- approval
    async def approval_resolve(self, params: Mapping[str, Any], ctx: CallContext) -> dict[str, Any]:
        thread_id = self._require_id("thread", params.get("thread_id"))
        turn_id = self._require_id("turn", params.get("turn_id"))
        approval_id = self._require_id("approval", params.get("approval_id"), param="approval_id")
        decision = str(params.get("decision") or "")
        if decision not in DECISION_TABLE:
            raise ProtocolError.invalid_params(
                f"unknown decision {decision!r}",
                allowed=sorted(DECISION_TABLE),
            )
        granted, scope = DECISION_TABLE[decision]
        state = self.repo.state(thread_id)
        approval = state.approvals.get(approval_id)
        if approval is None:
            from contracts.agent_v2.errors import ApprovalNotFound

            raise ApprovalNotFound(f"approval {approval_id} not found in thread {thread_id}")
        if approval.status is not ApprovalStatus.PENDING:
            # 重复提交：返回既有结论，不产生第二个副作用
            return {
                "approval": self._approval_view(approval),
                "turn": self._turn_view(state.turn(turn_id)) if turn_id in state.turns else None,
                "replayed": True,
                "decision": approval.decision_scope,
            }
        # 待处理的动作已经失败结束 / 回合已不在等待审批：给精确原因，别让前端猜
        call = state.tool_calls.get(approval.call_id) if approval.call_id else None
        failed_call = (
            call is not None
            and call.status
            not in (ToolCallStatus.REQUESTED, ToolCallStatus.RUNNING, ToolCallStatus.SUCCEEDED)
        )
        if failed_call and call is not None:
            raise ProtocolError.tool_failed(call.call_id, call.error or {})
        turn = state.turn(turn_id)
        if turn.status is TurnStatus.FAILED:
            raise ProtocolError.turn_terminated("failed", turn_id=turn_id)
        if turn.status is TurnStatus.INTERRUPTED:
            raise ProtocolError.turn_terminated("interrupted", turn_id=turn_id)
        if turn.status is not TurnStatus.WAITING_APPROVAL:
            raise ProtocolError.invalid_state(
                "这个回合当前没有等待审批的请求。",
                kind="turn_not_waiting_approval",
                turn_id=turn_id,
                status=turn.status.value,
                allowed=[TurnStatus.WAITING_APPROVAL.value],
            )
        turn = self.repo.resolve_approval(
            thread_id, turn_id, approval_id, granted=granted, scope=scope, decided_by="user"
        )
        if granted and scope in ("conversation", "full_access"):
            self._record_permission_rule(
                thread_id, approval=approval, scope=scope, full_access=scope == "full_access"
            )
        if decision == "cancel":
            turn = await self._interrupt(thread_id, turn_id, "approval_cancelled")
        else:
            await self._resume_after_approval(thread_id, turn_id)
            turn = self.repo.state(thread_id).turn(turn_id)
        state = self.repo.state(thread_id)
        return {
            "approval": self._approval_view(state.approvals[approval_id]),
            "turn": self._turn_view(turn),
            "replayed": False,
            "decision": decision,
        }

    async def _resume_after_approval(self, thread_id: str, turn_id: str) -> None:
        """审批结论落库后让 Turn 继续跑（批准与拒绝都要回喂模型）。

        拒绝不是「什么都不做」：挂起的工具调用会带着 ``approval_denied`` 错误结果
        回喂模型，由模型决定下一步（见 host 的 ``denial_for``）。
        """
        if self.host.is_running(thread_id):
            return
        try:
            self._start_host(thread_id, turn_id)
        except Exception:  # noqa: BLE001 - 运行时不可用不应让审批响应失败
            return

    def _record_permission_rule(
        self, thread_id: str, *, approval: ApprovalRequest, scope: str, full_access: bool
    ) -> None:
        """保存「当前对话始终批准当前命令」或「完全访问」规则。"""
        action = approval.action or {}
        tool_name = str(action.get("tool") or "")
        arguments = dict(action.get("arguments") or {})
        state = self.repo.state(thread_id)
        settings = dict(state.thread.settings or {})
        rules = [rule for rule in (settings.get(PERMISSION_RULES_KEY) or []) if isinstance(rule, dict)]
        if full_access:
            settings[FULL_ACCESS_KEY] = True
        if tool_name:
            rules.append(
                {
                    "tool": tool_name,
                    "prefix": normalize_command_prefix(arguments),
                    "cwd": state.thread.cwd,
                    "scope": scope,
                    "approval_id": approval.approval_id,
                    "created_at": self.clock.now_iso(),
                }
            )
        settings[PERMISSION_RULES_KEY] = rules
        self.repo.emit_event(
            thread_id,
            EventType.THREAD_UPDATED,
            payload={"patch": {"settings": settings, "permission_summary": {FULL_ACCESS_KEY: full_access}}},
        )

    # ------------------------------------------------------------- compaction
    async def thread_compact(self, params: Mapping[str, Any], ctx: CallContext) -> dict[str, Any]:
        thread_id = self._require_id("thread", params.get("thread_id"))
        trigger = str(params.get("trigger") or "manual")
        result = await self.compaction.compact(thread_id, trigger=trigger)
        return {"result": result.to_dict(), "summaries": self.compaction.summaries(thread_id)}

    async def thread_compaction_list(
        self, params: Mapping[str, Any], ctx: CallContext
    ) -> dict[str, Any]:
        thread_id = self._require_id("thread", params.get("thread_id"))
        return {"thread_id": thread_id, "summaries": self.compaction.summaries(thread_id)}

    async def thread_compaction_edit(
        self, params: Mapping[str, Any], ctx: CallContext
    ) -> dict[str, Any]:
        thread_id = self._require_id("thread", params.get("thread_id"))
        summary_id = self._require_id("summary", params.get("summary_id"), param="summary_id")
        text = self._text_of(params, "text")
        if not text:
            raise ProtocolError.invalid_params("thread/compaction/edit: text must be non-empty")
        summary = self.compaction.edit_summary(thread_id, summary_id, text, actor="user")
        return {"summary": summary, "summaries": self.compaction.summaries(thread_id)}

    async def thread_compaction_restore(
        self, params: Mapping[str, Any], ctx: CallContext
    ) -> dict[str, Any]:
        thread_id = self._require_id("thread", params.get("thread_id"))
        summary_id = self._require_id("summary", params.get("summary_id"), param="summary_id")
        summary = self.compaction.restore_summary(thread_id, summary_id)
        return {"summary": summary, "summaries": self.compaction.summaries(thread_id)}

    # ----------------------------------------------------------------- memory
    async def memory_list(self, params: Mapping[str, Any], ctx: CallContext) -> dict[str, Any]:
        scope = str(params.get("scope") or "")
        if scope not in MEMORY_SCOPES:
            raise ProtocolError.invalid_params(
                f"unknown memory scope {scope!r}", allowed=list(MEMORY_SCOPES)
            )
        scope_id = str(params.get("scope_id") or "default")
        records = self.memory.list(
            scope, scope_id, include_deleted=bool(params.get("include_deleted") or False)
        )
        return {
            "scope": scope,
            "scope_id": scope_id,
            "records": [self._memory_view(record) for record in records],
            "layout": self.memory.layout(),
        }

    async def memory_update(self, params: Mapping[str, Any], ctx: CallContext) -> dict[str, Any]:
        scope = str(params.get("scope") or "")
        if scope not in MEMORY_SCOPES:
            raise ProtocolError.invalid_params(
                f"unknown memory scope {scope!r}", allowed=list(MEMORY_SCOPES)
            )
        scope_id = str(params.get("scope_id") or "default")
        text = self._text_of(params, "text")
        if not text:
            raise ProtocolError.invalid_params("memory/update: text must be non-empty")
        memory_id = params.get("memory_id")
        tags = params.get("tags") or []
        if not isinstance(tags, list):
            raise ProtocolError.invalid_params("tags must be an array")
        source_ids = params.get("source_event_ids") or []
        if not isinstance(source_ids, list):
            raise ProtocolError.invalid_params("source_event_ids must be an array")
        if memory_id:
            memory_id = self._require_id("memory", memory_id, param="memory_id")
            record = self.memory.update(
                memory_id, text, actor="user", source_event_ids=[str(v) for v in source_ids]
            )
            event_type = EventType.MEMORY_UPDATED
        else:
            record = self.memory.write(
                scope,
                scope_id,
                text,
                origin="user",
                tags=[str(v) for v in tags],
                source_event_ids=[str(v) for v in source_ids],
            )
            event_type = EventType.MEMORY_WRITTEN
        thread_id = params.get("thread_id")
        if thread_id:
            thread_id = self._require_id("thread", thread_id)
        elif scope == MemoryScope.CONVERSATION.value:
            thread_id = scope_id if is_valid_id("thread", scope_id) else None
        if thread_id:
            self.repo.emit_event(
                str(thread_id),
                event_type,
                payload={"memory_id": record.memory_id, "scope": scope, "scope_id": scope_id},
            )
        return {
            "record": self._memory_view(record),
            "records": [
                self._memory_view(row)
                for row in self.memory.list(record.scope, record.scope_id)
            ],
        }

    async def memory_delete(self, params: Mapping[str, Any], ctx: CallContext) -> dict[str, Any]:
        memory_id = self._require_id("memory", params.get("memory_id"), param="memory_id")
        record = self.memory.delete(memory_id, actor="user")
        thread_id = params.get("thread_id")
        if thread_id:
            thread_id = self._require_id("thread", thread_id)
            self.repo.emit_event(
                thread_id,
                EventType.MEMORY_DELETED,
                payload={"memory_id": record.memory_id, "scope": record.scope.value},
            )
        return {"record": self._memory_view(record), "deleted": True}

    # ------------------------------------------------------------- agent tree
    def _child_view(self, parent_id: str, child_id: str) -> dict[str, Any]:
        child_state = self.repo.state(child_id)
        summary = None
        result_item_id = None
        error = None
        for item in self.repo.state(parent_id).items_of_type("subagent_result"):
            if item.subagent_thread_id == child_id:
                summary = item.payload.get("summary")
                result_item_id = item.item_id
                error = item.payload.get("error")
        return {
            "thread_id": child_id,
            "name": child_state.thread.name,
            "parent_thread_id": parent_id,
            "path": list(child_state.thread.path),
            "status": self.tree.child_status(child_id),
            "running": self.host.is_running(child_id),
            "summary": summary,
            "result_item_id": result_item_id,
            "error": error,
            "last_sequence": child_state.last_sequence,
            "mailbox": list(child_state.mailbox),
        }

    async def agent_list(self, params: Mapping[str, Any], ctx: CallContext) -> dict[str, Any]:
        thread_id = self._require_id("thread", params.get("thread_id"))
        children = [
            self._child_view(thread_id, child_id) for child_id in self.tree.children(thread_id)
        ]
        return {"thread_id": thread_id, "children": children, "count": len(children)}

    async def agent_wait(self, params: Mapping[str, Any], ctx: CallContext) -> dict[str, Any]:
        thread_id = self._require_id("thread", params.get("thread_id"))
        raw_ids = params.get("child_thread_ids")
        if not isinstance(raw_ids, list) or not raw_ids:
            raise ProtocolError.invalid_params("child_thread_ids must be a non-empty array")
        child_ids = [self._require_id("thread", value, param="child_thread_ids[]") for value in raw_ids]
        timeout_s = params.get("timeout_s")
        if timeout_s is not None and not isinstance(timeout_s, (int, float)):
            raise ProtocolError.invalid_params("timeout_s must be a number")
        outcomes = await self.tree.wait_for(child_ids, timeout_s=timeout_s)
        return {
            "thread_id": thread_id,
            "outcomes": {child_id: outcome.to_dict() for child_id, outcome in outcomes.items()},
            "results": {
                child_id: self._child_view(thread_id, child_id) for child_id in child_ids
            },
        }

    async def agent_interrupt(self, params: Mapping[str, Any], ctx: CallContext) -> dict[str, Any]:
        child_id = self._require_id("thread", params.get("child_thread_id"), param="child_thread_id")
        reason = str(params.get("reason") or "parent_interrupt")
        self.host.interrupt(child_id, reason)
        self.tree.interrupt_child(child_id, reason=reason)
        parent_id = params.get("thread_id")
        if parent_id is None:
            parent_id = self.tree.parent_of(child_id)
        view = self._child_view(str(parent_id), child_id) if parent_id else None
        return {"child_thread_id": child_id, "child": view}
