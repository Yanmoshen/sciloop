"""契约层枚举与状态机常量。

所有枚举均为「封闭集合」：非成员值必须在反序列化时立即失败，
不允许静默降级为默认值。
"""

from __future__ import annotations

from enum import Enum
from typing import Mapping


class _StrEnum(str, Enum):
    """Python 3.9 兼容的 str 枚举基类。"""

    def __str__(self) -> str:  # pragma: no cover - 便于日志
        return str(self.value)


class TurnStatus(_StrEnum):
    """Turn 生命周期状态（计划书 §4.1 的封闭集合）。"""

    QUEUED = "queued"
    RUNNING = "running"
    WAITING_APPROVAL = "waiting_approval"
    WAITING_INPUT = "waiting_input"
    INTERRUPTED = "interrupted"
    FAILED = "failed"
    COMPLETED = "completed"


#: 处于这些状态时，该 Turn 占用 Thread 的「活动 Turn」名额。
ACTIVE_TURN_STATUSES: frozenset[TurnStatus] = frozenset(
    {TurnStatus.RUNNING, TurnStatus.WAITING_APPROVAL, TurnStatus.WAITING_INPUT}
)

#: 终态：不允许再迁移到任何状态。
TERMINAL_TURN_STATUSES: frozenset[TurnStatus] = frozenset(
    {TurnStatus.COMPLETED, TurnStatus.FAILED}
)

#: 合法状态迁移表。表外迁移一律抛 :class:`IllegalTurnTransition`。
#:
#: 注意 ``interrupted -> completed`` 被**刻意禁止**：中断后若允许直接置成功，
#: 就无法保证验收书 §3「中断模型流时不会产生最终成功事件」。
#: 恢复路径必须显式经过 ``interrupted -> running -> completed``。
TURN_TRANSITIONS: Mapping[TurnStatus, frozenset[TurnStatus]] = {
    TurnStatus.QUEUED: frozenset({TurnStatus.RUNNING, TurnStatus.INTERRUPTED, TurnStatus.FAILED}),
    TurnStatus.RUNNING: frozenset(
        {
            TurnStatus.WAITING_APPROVAL,
            TurnStatus.WAITING_INPUT,
            TurnStatus.INTERRUPTED,
            TurnStatus.FAILED,
            TurnStatus.COMPLETED,
        }
    ),
    TurnStatus.WAITING_APPROVAL: frozenset(
        {TurnStatus.RUNNING, TurnStatus.INTERRUPTED, TurnStatus.FAILED}
    ),
    TurnStatus.WAITING_INPUT: frozenset(
        {TurnStatus.RUNNING, TurnStatus.INTERRUPTED, TurnStatus.FAILED}
    ),
    TurnStatus.INTERRUPTED: frozenset({TurnStatus.RUNNING, TurnStatus.FAILED}),
    TurnStatus.FAILED: frozenset(),
    TurnStatus.COMPLETED: frozenset(),
}


def can_transition(src: TurnStatus | str, dst: TurnStatus | str) -> bool:
    """判断状态迁移是否合法。"""
    s = TurnStatus(src)
    d = TurnStatus(dst)
    return d in TURN_TRANSITIONS[s]


class ItemType(_StrEnum):
    """Item 类型（计划书 §4.1）。"""

    USER_INPUT = "user_input"
    ASSISTANT_TEXT = "assistant_text"
    REASONING = "reasoning"
    TOOL_CALL = "tool_call"
    TOOL_RESULT = "tool_result"
    PLAN = "plan"
    COMPACTION = "compaction"
    SUBAGENT_ACTIVITY = "subagent_activity"
    SUBAGENT_RESULT = "subagent_result"
    APPROVAL = "approval"
    ERROR = "error"


class ToolKind(_StrEnum):
    """工具副作用等级，决定调度策略。"""

    READ_ONLY = "read_only"
    SIDE_EFFECT = "side_effect"


class ToolCallStatus(_StrEnum):
    """工具调用状态（输入/输出/失败/取消结构统一）。"""

    REQUESTED = "requested"
    RUNNING = "running"
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    TIMEOUT = "timeout"
    CANCELLED = "cancelled"
    INVALID_ARGUMENTS = "invalid_arguments"


class ErrorClass(_StrEnum):
    """模型/运行时错误分类，决定重试与降级策略。"""

    RETRYABLE = "retryable"
    CONTEXT_OVERFLOW = "context_overflow"
    INVALID_REQUEST = "invalid_request"
    FATAL = "fatal"


class MemoryScope(_StrEnum):
    """记忆作用域。"""

    USER = "user"
    PROJECT = "project"
    CONVERSATION = "conversation"


class MemoryOrigin(_StrEnum):
    """记忆写入来源。``USER`` 写入的记录受自动流程覆盖保护。"""

    AUTO = "auto"
    USER = "user"


class ApprovalStatus(_StrEnum):
    """审批请求状态。"""

    PENDING = "pending"
    GRANTED = "granted"
    DENIED = "denied"
    EXPIRED = "expired"


class ModelStreamKind(_StrEnum):
    """统一模型流条目类型（供应商无关）。"""

    TEXT_DELTA = "text_delta"
    REASONING_DELTA = "reasoning_delta"
    TOOL_CALL_DELTA = "tool_call_delta"
    TOOL_CALL_COMPLETED = "tool_call_completed"
    USAGE = "usage"
    COMPLETED = "completed"
    ERROR = "error"


class StopReason(_StrEnum):
    """模型流正常结束原因。"""

    END_TURN = "end_turn"
    TOOL_USE = "tool_use"
    MAX_TOKENS = "max_tokens"
    STOP_SEQUENCE = "stop_sequence"


class EventType(_StrEnum):
    """事件类型总表（追加式事件存储的唯一合法取值域）。"""

    # Thread
    THREAD_CREATED = "thread/created"
    THREAD_UPDATED = "thread/updated"
    THREAD_FORKED = "thread/forked"
    THREAD_ARCHIVED = "thread/archived"

    # Turn
    TURN_QUEUED = "turn/queued"
    TURN_STARTED = "turn/started"
    TURN_PAUSED = "turn/paused"
    TURN_RESUMED = "turn/resumed"
    TURN_INTERRUPTED = "turn/interrupted"
    TURN_WAITING_APPROVAL = "turn/waiting_approval"
    TURN_WAITING_INPUT = "turn/waiting_input"
    TURN_COMPLETED = "turn/completed"
    TURN_FAILED = "turn/failed"

    # Item
    ITEM_ADDED = "item/added"

    # Model
    MODEL_REQUESTED = "model/requested"
    MODEL_DELTA = "model/delta"
    MODEL_REASONING_DELTA = "model/reasoning_delta"
    MODEL_TOOL_CALL_STARTED = "model/tool_call_started"
    MODEL_TOOL_CALL_DELTA = "model/tool_call_delta"
    MODEL_TOOL_CALL_COMPLETED = "model/tool_call_completed"
    MODEL_COMPLETED = "model/completed"
    MODEL_FAILED = "model/failed"
    MODEL_RETRY_SCHEDULED = "model/retry_scheduled"
    MODEL_USAGE = "model/usage"

    # Tool
    TOOL_STARTED = "tool/started"
    TOOL_OUTPUT = "tool/output"
    TOOL_COMPLETED = "tool/completed"
    TOOL_FAILED = "tool/failed"
    TOOL_TIMEOUT = "tool/timeout"
    TOOL_CANCELLED = "tool/cancelled"
    TOOL_INVALID_ARGUMENTS = "tool/invalid_arguments"

    # Approval / input
    APPROVAL_REQUESTED = "approval/requested"
    APPROVAL_GRANTED = "approval/granted"
    APPROVAL_DENIED = "approval/denied"
    INPUT_REQUESTED = "input/requested"
    INPUT_PROVIDED = "input/provided"

    # Compaction
    COMPACTION_STARTED = "compaction/started"
    COMPACTION_COMPLETED = "compaction/completed"
    COMPACTION_FAILED = "compaction/failed"
    COMPACTION_EDITED = "compaction/edited"
    COMPACTION_RESTORED = "compaction/restored"

    # Memory
    MEMORY_WRITTEN = "memory/written"
    MEMORY_UPDATED = "memory/updated"
    MEMORY_DELETED = "memory/deleted"

    # Agent tree
    AGENT_CHILD_CREATED = "agent/child_created"
    AGENT_MESSAGE = "agent/message"
    AGENT_CHILD_COMPLETED = "agent/child_completed"
    AGENT_CHILD_FAILED = "agent/child_failed"
    AGENT_CHILD_INTERRUPTED = "agent/child_interrupted"


#: 事件类型分组前缀（用于 schema 里的 pattern 校验）。
EVENT_TYPE_PREFIXES: frozenset[str] = frozenset(
    {"thread", "turn", "item", "model", "tool", "approval", "input", "compaction", "memory", "agent"}
)

__all__ = [
    "TurnStatus",
    "ACTIVE_TURN_STATUSES",
    "TERMINAL_TURN_STATUSES",
    "TURN_TRANSITIONS",
    "can_transition",
    "ItemType",
    "ToolKind",
    "ToolCallStatus",
    "ErrorClass",
    "MemoryScope",
    "MemoryOrigin",
    "ApprovalStatus",
    "ModelStreamKind",
    "StopReason",
    "EventType",
    "EVENT_TYPE_PREFIXES",
]
