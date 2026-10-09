"""agent.v2 契约包公开接口。

统一从本模块导入，避免下游依赖内部文件布局：

    from contracts.agent_v2 import (
        CONTRACT_VERSION, Event, Thread, Turn, Item, ToolCall, MemoryRecord,
        EventType, TurnStatus, ItemType, ToolKind, ModelStreamKind, ErrorClass,
        ContractViolation, CorruptedEventError,
        new_id, CancelToken, FakeProvider, FakeToolExecutor,
    )
"""

from __future__ import annotations

from .cancellation import CancelledError, CancelToken, never_cancelled
from .clock import Clock, FakeClock, SystemClock, iso_ms
from .enums import (
    ACTIVE_TURN_STATUSES,
    EVENT_TYPE_PREFIXES,
    TERMINAL_TURN_STATUSES,
    TURN_TRANSITIONS,
    ApprovalStatus,
    ErrorClass,
    EventType,
    ItemType,
    MemoryOrigin,
    MemoryScope,
    ModelStreamKind,
    StopReason,
    ToolCallStatus,
    ToolKind,
    TurnStatus,
    can_transition,
)
from .errors import (
    AgentV2Error,
    ApprovalNotFound,
    ConcurrentTurnError,
    ContractViolation,
    CorruptedEventError,
    IllegalTurnTransition,
    LeaseError,
    MemoryOverwriteDenied,
    ModelStreamError,
    SummaryNotFound,
    ThreadNotFound,
    TurnNotFound,
)
from .fake import (
    FakeProvider,
    FakeToolExecutor,
    ModelProvider,
    ToolExecutor,
    error_response,
    reasoning_response,
    text_response,
    tool_call_response,
    usage_item,
)
from .ids import ID_PREFIXES, id_sort_key, is_valid_id, new_id, parse_id
from .models import (
    ApprovalRequest,
    ContractModel,
    Event,
    Item,
    MemoryRecord,
    ModelRequest,
    ReasoningDelta,
    StreamCompleted,
    StreamError,
    StreamItem,
    TextDelta,
    Thread,
    ToolCall,
    ToolCallCompleted,
    ToolCallDelta,
    ToolResult,
    ToolSpec,
    Turn,
    Usage,
)
from .validate import (
    SCHEMA_FILES,
    SchemaValidator,
    load_schema,
    schema_enum,
    validate_all_present,
    validator_for,
)
from .version import CONTRACT_FREEZE_TAG, CONTRACT_VERSION, READABLE_CONTRACT_VERSIONS, is_readable

__all__ = [
    # 版本
    "CONTRACT_VERSION",
    "CONTRACT_FREEZE_TAG",
    "READABLE_CONTRACT_VERSIONS",
    "is_readable",
    # ID
    "new_id",
    "parse_id",
    "is_valid_id",
    "id_sort_key",
    "ID_PREFIXES",
    # 枚举
    "TurnStatus",
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
    "ACTIVE_TURN_STATUSES",
    "TERMINAL_TURN_STATUSES",
    "TURN_TRANSITIONS",
    "EVENT_TYPE_PREFIXES",
    "can_transition",
    # 错误
    "AgentV2Error",
    "ContractViolation",
    "IllegalTurnTransition",
    "ConcurrentTurnError",
    "ThreadNotFound",
    "TurnNotFound",
    "ApprovalNotFound",
    "SummaryNotFound",
    "CorruptedEventError",
    "LeaseError",
    "MemoryOverwriteDenied",
    "ModelStreamError",
    "CancelledError",
    # 取消与时钟
    "CancelToken",
    "never_cancelled",
    "Clock",
    "SystemClock",
    "FakeClock",
    "iso_ms",
    # 模型
    "ContractModel",
    "Thread",
    "Turn",
    "Item",
    "ToolCall",
    "ToolSpec",
    "ToolResult",
    "Event",
    "ApprovalRequest",
    "MemoryRecord",
    "ModelRequest",
    "StreamItem",
    "TextDelta",
    "ReasoningDelta",
    "ToolCallDelta",
    "ToolCallCompleted",
    "Usage",
    "StreamCompleted",
    "StreamError",
    # 协议与 fake
    "ModelProvider",
    "ToolExecutor",
    "FakeProvider",
    "FakeToolExecutor",
    "text_response",
    "reasoning_response",
    "tool_call_response",
    "error_response",
    "usage_item",
    # schema
    "SCHEMA_FILES",
    "SchemaValidator",
    "load_schema",
    "validator_for",
    "validate_all_present",
    "schema_enum",
]
