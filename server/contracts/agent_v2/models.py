"""agent.v2 契约数据模型（纯标准库实现）。

设计约束：
- **不引入第三方依赖**（不新增 requirements 条目），用 dataclasses + 显式类型提示；
- **反序列化严格**：缺失必填字段、非法枚举、未知字段一律抛 :class:`ContractViolation`；
- **JSON 安全**：``to_dict()`` 输出的对象可直接 ``json.dumps``；
- 所有持久化对象都携带 ``contract`` 字段（版本锚点）。
"""

from __future__ import annotations

import dataclasses
import types
import typing
from dataclasses import MISSING, dataclass, field
from typing import Any, ClassVar, Optional, Union, get_args, get_origin, get_type_hints

from .enums import (
    ApprovalStatus,
    ErrorClass,
    ItemType,
    MemoryOrigin,
    MemoryScope,
    ModelStreamKind,
    StopReason,
    ToolCallStatus,
    ToolKind,
    TurnStatus,
)
from .errors import ContractViolation
from .version import CONTRACT_VERSION, is_readable

_NONE_TYPE = type(None)


# --------------------------------------------------------------------------------------
# JSON 编解码基础设施
# --------------------------------------------------------------------------------------
def _to_json(value: Any) -> Any:
    """把模型对象递归转换为 JSON 安全结构。

    注意：``_StrEnum`` 继承自 ``str``，因此**必须先判枚举再判 str**，
    否则枚举成员会以自身（而非 ``.value``）参与序列化。
    """
    if value is None or isinstance(value, (int, float, bool)):
        return value
    if isinstance(value, StrEnumTypes):
        return value.value
    if isinstance(value, str):
        return value
    if isinstance(value, ContractModel):
        return value.to_dict()
    if isinstance(value, (list, tuple)):
        return [_to_json(v) for v in value]
    if isinstance(value, dict):
        return {str(k): _to_json(v) for k, v in value.items()}
    raise ContractViolation(f"cannot serialize value of type {type(value).__name__}")


# str 枚举的类型联合（避免循环导入）
StrEnumTypes: tuple[type, ...] = ()


def _register_str_enum_types() -> None:
    global StrEnumTypes
    from . import enums as _enums  # noqa: PLC0415 - 延迟导入避免循环

    StrEnumTypes = tuple(
        obj
        for obj in vars(_enums).values()
        if isinstance(obj, type) and issubclass(obj, _enums._StrEnum)
    )


_register_str_enum_types()


def _unwrap_optional(hint: Any) -> tuple[Any, bool]:
    """拆掉 Optional，返回 ``(内层类型, 是否可空)``。"""
    origin = get_origin(hint)
    if origin is Union or origin is types.UnionType:
        args = [a for a in get_args(hint) if a is not _NONE_TYPE]
        if len(args) == 1:
            return args[0], True
        return hint, False
    return hint, False


def _coerce(name: str, fname: str, hint: Any, value: Any, enum_cls: Optional[type]) -> Any:
    """按类型提示做严格强制转换。"""
    inner, nullable = _unwrap_optional(hint)
    if value is None:
        if nullable:
            return None
        raise ContractViolation(f"{name}.{fname}: null is not allowed")
    if enum_cls is not None:
        try:
            return enum_cls(value)
        except ValueError as exc:
            allowed = sorted(str(m.value) for m in enum_cls)
            raise ContractViolation(
                f"{name}.{fname}: {value!r} is not a valid {enum_cls.__name__}; allowed: {allowed}"
            ) from exc
    origin = get_origin(inner)
    if origin in (list, tuple, set):
        if not isinstance(value, (list, tuple)):
            raise ContractViolation(f"{name}.{fname}: expected array, got {type(value).__name__}")
        (elem_hint,) = get_args(inner) or (Any,)
        return [_coerce(name, f"{fname}[]", elem_hint, v, None) for v in value]
    if inner is dict or origin is dict:
        if not isinstance(value, dict):
            raise ContractViolation(f"{name}.{fname}: expected object, got {type(value).__name__}")
        return dict(value)
    if inner is bool:
        if not isinstance(value, bool):
            raise ContractViolation(f"{name}.{fname}: expected boolean")
        return value
    if inner is int:
        if isinstance(value, bool) or not isinstance(value, int):
            raise ContractViolation(f"{name}.{fname}: expected integer")
        return value
    if inner is float:
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            raise ContractViolation(f"{name}.{fname}: expected number")
        return float(value)
    if inner is str:
        if not isinstance(value, str):
            raise ContractViolation(f"{name}.{fname}: expected string, got {type(value).__name__}")
        return value
    return value


class ContractModel:
    """所有契约对象的基类：``to_dict()`` / ``from_dict()``。

    子类若自定义 ``__post_init__``，**必须先调用** ``super().__post_init__()``，
    否则会漏掉枚举强制与契约版本校验。
    """

    #: 字段名 -> 封闭枚举类型
    _enums: ClassVar[dict[str, type]] = {}

    def __post_init__(self) -> None:
        self._coerce_enums()
        self._require_contract_version()

    # ---- 构造期校验（直接构造与 from_dict 走同一套规则）----
    def _coerce_enums(self) -> None:
        for fname, enum_cls in self._enums.items():
            value = getattr(self, fname, None)
            if value is None or isinstance(value, enum_cls):
                continue
            try:
                object.__setattr__(self, fname, enum_cls(value))
            except ValueError as exc:
                allowed = sorted(str(m.value) for m in enum_cls)
                raise ContractViolation(
                    f"{type(self).__name__}.{fname}: {value!r} is not a valid "
                    f"{enum_cls.__name__}; allowed: {allowed}"
                ) from exc

    def _require_contract_version(self) -> None:
        version = getattr(self, "contract", None)
        if version is not None:
            _require_contract(version, type(self).__name__)

    def to_dict(self) -> dict[str, Any]:
        if not dataclasses.is_dataclass(self):  # pragma: no cover - 防御
            raise ContractViolation(f"{type(self).__name__} is not a dataclass")
        return {f.name: _to_json(getattr(self, f.name)) for f in dataclasses.fields(self)}

    @classmethod
    def from_dict(
        cls,
        data: Any,
        *,
        strict: bool = True,
        partial: bool = False,
    ) -> "ContractModel":
        """反序列化。

        :param strict: True 时未知字段直接失败（默认；契约漂移必须显式暴露）。
        :param partial: True 时允许缺省必填字段（用于构造补丁），默认 False。
        """
        if not isinstance(data, dict):
            raise ContractViolation(f"{cls.__name__}: expected object, got {type(data).__name__}")
        declared = {f.name for f in dataclasses.fields(cls)}
        if strict:
            extra = sorted(set(data) - declared)
            if extra:
                raise ContractViolation(f"{cls.__name__}: unexpected field(s) {extra}")
        hints = get_type_hints(cls)
        kwargs: dict[str, Any] = {}
        for f in dataclasses.fields(cls):
            if f.name not in data:
                if not partial and f.default is MISSING and f.default_factory is MISSING:
                    raise ContractViolation(f"{cls.__name__}: missing required field '{f.name}'")
                continue
            enum_cls = cls._enums.get(f.name)
            kwargs[f.name] = _coerce(cls.__name__, f.name, hints[f.name], data[f.name], enum_cls)
        return cls(**kwargs)  # type: ignore[return-value]


def _require_contract(version: str, name: str) -> None:
    if not is_readable(version):
        raise ContractViolation(
            f"{name}.contract: version {version!r} is not readable by this build "
            f"(supported: {CONTRACT_VERSION!r})"
        )


# --------------------------------------------------------------------------------------
# Thread / Turn / Item
# --------------------------------------------------------------------------------------
@dataclass
class Thread(ContractModel):
    """长期线程：跨 Turn 存活的会话容器。"""

    thread_id: str
    name: str
    created_at: str
    updated_at: str
    status: str = "active"
    settings: dict[str, Any] = field(default_factory=dict)
    cwd: Optional[str] = None
    model: Optional[str] = None
    permission_summary: dict[str, Any] = field(default_factory=dict)
    active_turn_id: Optional[str] = None
    last_sequence: int = 0
    parent_thread_id: Optional[str] = None
    path: list[str] = field(default_factory=list)
    forked_from: Optional[dict[str, Any]] = None
    contract: str = CONTRACT_VERSION

    _enums: ClassVar[dict[str, type]] = {}

    def __post_init__(self) -> None:
        super().__post_init__()
        if self.status not in ("active", "archived"):
            raise ContractViolation(f"Thread.status: {self.status!r} is not active|archived")


@dataclass
class Turn(ContractModel):
    """独立回合：一次「输入 -> 模型/工具循环 -> 终态」的完整生命周期。"""

    turn_id: str
    thread_id: str
    status: TurnStatus
    created_at: str
    updated_at: str
    sequence_start: int
    sequence_end: Optional[int] = None
    input_item_ids: list[str] = field(default_factory=list)
    idempotency_key: Optional[str] = None
    attempt: int = 1
    error: Optional[dict[str, Any]] = None
    waiting: Optional[dict[str, Any]] = None
    cancel_reason: Optional[str] = None
    contract: str = CONTRACT_VERSION

    _enums: ClassVar[dict[str, type]] = {"status": TurnStatus}

    def __post_init__(self) -> None:
        super().__post_init__()
        if self.sequence_start < 1:
            raise ContractViolation("Turn.sequence_start must be >= 1")


@dataclass
class Item(ContractModel):
    """Turn 内最小可审计单元。"""

    item_id: str
    thread_id: str
    type: ItemType
    created_at: str
    sequence: int
    payload: dict[str, Any] = field(default_factory=dict)
    turn_id: Optional[str] = None
    call_id: Optional[str] = None
    subagent_thread_id: Optional[str] = None
    contract: str = CONTRACT_VERSION

    _enums: ClassVar[dict[str, type]] = {"type": ItemType}

    def __post_init__(self) -> None:
        super().__post_init__()
        if self.sequence < 1:
            raise ContractViolation("Item.sequence must be >= 1")


# --------------------------------------------------------------------------------------
# 工具调用（输入 / 输出 / 失败 / 取消 结构统一）
# --------------------------------------------------------------------------------------
@dataclass
class ToolCall(ContractModel):
    """统一的工具调用记录：同一结构承载请求、成功、失败与取消。"""

    call_id: str
    name: str
    kind: ToolKind
    status: ToolCallStatus
    thread_id: str
    turn_id: str
    item_id: Optional[str] = None
    arguments: dict[str, Any] = field(default_factory=dict)
    output: Optional[dict[str, Any]] = None
    error: Optional[dict[str, Any]] = None
    requested_at: Optional[str] = None
    started_at: Optional[str] = None
    finished_at: Optional[str] = None
    duration_ms: Optional[int] = None
    attempt: int = 1
    truncated: bool = False
    contract: str = CONTRACT_VERSION

    _enums: ClassVar[dict[str, type]] = {"kind": ToolKind, "status": ToolCallStatus}

    def __post_init__(self) -> None:
        super().__post_init__()
        if self.status is ToolCallStatus.SUCCEEDED and self.error is not None:
            raise ContractViolation("ToolCall: succeeded call must not carry error")
        if self.status in (
            ToolCallStatus.FAILED,
            ToolCallStatus.TIMEOUT,
            ToolCallStatus.INVALID_ARGUMENTS,
        ) and self.error is None:
            raise ContractViolation(f"ToolCall: status {self.status.value} requires error detail")


@dataclass
class ToolSpec(ContractModel):
    """工具声明：决定调度策略（只读可并行，副作用必须串行）。"""

    name: str
    kind: ToolKind
    description: str = ""
    parameters: dict[str, Any] = field(default_factory=dict)
    timeout_s: Optional[float] = None
    contract: str = CONTRACT_VERSION

    _enums: ClassVar[dict[str, type]] = {"kind": ToolKind}


@dataclass
class ToolResult(ContractModel):
    """工具执行器返回值（Agent 1 只定义结构，不实现执行）。"""

    call_id: str
    name: str
    status: ToolCallStatus
    output: Optional[dict[str, Any]] = None
    error: Optional[dict[str, Any]] = None
    duration_ms: Optional[int] = None
    started_at: Optional[str] = None
    finished_at: Optional[str] = None
    contract: str = CONTRACT_VERSION

    _enums: ClassVar[dict[str, type]] = {"status": ToolCallStatus}


# --------------------------------------------------------------------------------------
# 事件
# --------------------------------------------------------------------------------------
@dataclass
class Event(ContractModel):
    """追加式事件存储中的一条事件。"""

    event_id: str
    sequence: int
    type: str
    created_at: str
    thread_id: str
    payload: dict[str, Any] = field(default_factory=dict)
    turn_id: Optional[str] = None
    item_id: Optional[str] = None
    call_id: Optional[str] = None
    idempotency_key: Optional[str] = None
    contract: str = CONTRACT_VERSION

    def __post_init__(self) -> None:
        super().__post_init__()
        if not isinstance(self.sequence, int) or self.sequence < 1:
            raise ContractViolation(f"Event.sequence must be a positive integer, got {self.sequence!r}")
        if not self.thread_id:
            raise ContractViolation("Event.thread_id is required (every event belongs to a thread)")
        if not isinstance(self.type, str) or "/" not in self.type:
            raise ContractViolation(f"Event.type: {self.type!r} is not a namespaced event type")

    def is_type(self, *types_: Any) -> bool:
        wanted = {t.value if hasattr(t, "value") else str(t) for t in types_}
        return self.type in wanted


# --------------------------------------------------------------------------------------
# 审批
# --------------------------------------------------------------------------------------
@dataclass
class ApprovalRequest(ContractModel):
    """审批请求：风险自适应人机协同的持久化载体。"""

    approval_id: str
    thread_id: str
    turn_id: str
    created_at: str
    status: ApprovalStatus
    action: dict[str, Any] = field(default_factory=dict)
    call_id: Optional[str] = None
    risk: str = "unknown"
    decided_at: Optional[str] = None
    decided_by: Optional[str] = None
    decision_scope: Optional[str] = None
    contract: str = CONTRACT_VERSION

    _enums: ClassVar[dict[str, type]] = {"status": ApprovalStatus}


# --------------------------------------------------------------------------------------
# 记忆
# --------------------------------------------------------------------------------------
@dataclass
class MemoryRecord(ContractModel):
    """记忆记录：用户 / 项目 / 对话三作用域隔离。"""

    memory_id: str
    scope: MemoryScope
    scope_id: str
    text: str
    version: int
    created_at: str
    updated_at: str
    origin: MemoryOrigin = MemoryOrigin.AUTO
    edited_by_user: bool = False
    deleted: bool = False
    source_event_ids: list[str] = field(default_factory=list)
    tags: list[str] = field(default_factory=list)
    contract: str = CONTRACT_VERSION

    _enums: ClassVar[dict[str, type]] = {
        "scope": MemoryScope,
        "origin": MemoryOrigin,
    }

    def __post_init__(self) -> None:
        super().__post_init__()
        if self.version < 1:
            raise ContractViolation("MemoryRecord.version must be >= 1")


# --------------------------------------------------------------------------------------
# 模型请求与统一流条目
# --------------------------------------------------------------------------------------
@dataclass
class ModelRequest(ContractModel):
    """供应商无关的模型请求。"""

    request_id: str
    messages: list[dict[str, Any]]
    model: Optional[str] = None
    tools: list[dict[str, Any]] = field(default_factory=list)
    params: dict[str, Any] = field(default_factory=dict)
    thread_id: Optional[str] = None
    turn_id: Optional[str] = None
    contract: str = CONTRACT_VERSION


class StreamItem:
    """统一模型流条目基类（供应商无关）。"""

    kind: ClassVar[str] = ""

    def to_dict(self) -> dict[str, Any]:  # pragma: no cover - 子类实现
        raise NotImplementedError


@dataclass
class TextDelta(StreamItem):
    text: str
    kind: ClassVar[str] = ModelStreamKind.TEXT_DELTA

    def to_dict(self) -> dict[str, Any]:
        return {"kind": self.kind, "text": self.text}


@dataclass
class ReasoningDelta(StreamItem):
    text: str
    kind: ClassVar[str] = ModelStreamKind.REASONING_DELTA

    def to_dict(self) -> dict[str, Any]:
        return {"kind": self.kind, "text": self.text}


@dataclass
class ToolCallDelta(StreamItem):
    call_id: str
    name: Optional[str] = None
    arguments_delta: str = ""
    kind: ClassVar[str] = ModelStreamKind.TOOL_CALL_DELTA

    def to_dict(self) -> dict[str, Any]:
        return {
            "kind": self.kind,
            "call_id": self.call_id,
            "name": self.name,
            "arguments_delta": self.arguments_delta,
        }


@dataclass
class ToolCallCompleted(StreamItem):
    call_id: str
    name: str
    arguments: dict[str, Any] = field(default_factory=dict)
    kind: ClassVar[str] = ModelStreamKind.TOOL_CALL_COMPLETED

    def to_dict(self) -> dict[str, Any]:
        return {
            "kind": self.kind,
            "call_id": self.call_id,
            "name": self.name,
            "arguments": _to_json(self.arguments),
        }


@dataclass
class Usage(StreamItem):
    input_tokens: int = 0
    output_tokens: int = 0
    cached_tokens: int = 0
    cost_usd: Optional[float] = None
    kind: ClassVar[str] = ModelStreamKind.USAGE

    def to_dict(self) -> dict[str, Any]:
        out = {
            "kind": self.kind,
            "input_tokens": self.input_tokens,
            "output_tokens": self.output_tokens,
            "cached_tokens": self.cached_tokens,
        }
        if self.cost_usd is not None:
            out["cost_usd"] = self.cost_usd
        return out


@dataclass
class StreamCompleted(StreamItem):
    stop_reason: str = StopReason.END_TURN
    kind: ClassVar[str] = ModelStreamKind.COMPLETED

    def to_dict(self) -> dict[str, Any]:
        reason = self.stop_reason.value if hasattr(self.stop_reason, "value") else str(self.stop_reason)
        return {"kind": self.kind, "stop_reason": reason}


@dataclass
class StreamError(StreamItem):
    error_class: str = ErrorClass.FATAL
    message: str = ""
    retry_after_s: float = 0.0
    kind: ClassVar[str] = ModelStreamKind.ERROR

    def to_dict(self) -> dict[str, Any]:
        cls = self.error_class.value if hasattr(self.error_class, "value") else str(self.error_class)
        return {
            "kind": self.kind,
            "error_class": cls,
            "message": self.message,
            "retry_after_s": self.retry_after_s,
        }


__all__ = [
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
]
