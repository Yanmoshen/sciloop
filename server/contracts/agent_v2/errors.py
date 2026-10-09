"""契约层错误类型。

所有错误都带 ``code`` 字段，便于上层（事件流、审批提示、HTTP 层）做稳定判定，
不依赖异常消息文本。
"""

from __future__ import annotations

from typing import Any


class AgentV2Error(Exception):
    """agent.v2 契约与运行时错误的基类。"""

    code: str = "agent_v2_error"

    def to_dict(self) -> dict[str, Any]:
        return {"code": self.code, "message": str(self)}


class ContractViolation(AgentV2Error):
    """反序列化/校验失败：缺字段、非法枚举、未知字段。"""

    code = "contract_violation"


class IllegalTurnTransition(AgentV2Error):
    """非法的 Turn 状态迁移。"""

    code = "illegal_turn_transition"

    def __init__(self, turn_id: str, src: str, dst: str, allowed: Any = None) -> None:
        self.turn_id = turn_id
        self.src = src
        self.dst = dst
        self.allowed = sorted(str(a) for a in (allowed or ()))
        super().__init__(
            f"illegal turn transition for {turn_id}: {src} -> {dst}; allowed: {self.allowed}"
        )


class ConcurrentTurnError(AgentV2Error):
    """同一 Thread 上已经有活动 Turn。"""

    code = "concurrent_turn"


class ThreadNotFound(AgentV2Error):
    """Thread 目录/状态不存在。"""

    code = "thread_not_found"


class TurnNotFound(AgentV2Error):
    """Turn 不存在。"""

    code = "turn_not_found"


class CorruptedEventError(AgentV2Error):
    """事件流损坏：**结构化**报错，绝不静默跳过。

    携带出错的物理行号与原始前缀，便于人工修复或走修复流程。
    """

    code = "corrupted_event"

    def __init__(
        self,
        path: str,
        line_no: int,
        reason: str,
        *,
        raw_prefix: str = "",
        sequence: int | None = None,
    ) -> None:
        self.path = path
        self.line_no = line_no
        self.reason = reason
        self.raw_prefix = raw_prefix[:400]
        self.sequence = sequence
        super().__init__(f"corrupted event at {path}:{line_no} ({reason})")

    def to_dict(self) -> dict[str, Any]:
        return {
            "code": self.code,
            "message": str(self),
            "path": self.path,
            "line_no": self.line_no,
            "reason": self.reason,
            "raw_prefix": self.raw_prefix,
            "sequence": self.sequence,
        }


class ApprovalNotFound(AgentV2Error):
    """审批请求不存在。"""

    code = "approval_not_found"


class SummaryNotFound(AgentV2Error):
    """压缩摘要不存在。"""

    code = "summary_not_found"


class LeaseError(AgentV2Error):
    """Thread 租约被占用或已过期。"""

    code = "lease_error"


class MemoryOverwriteDenied(AgentV2Error):
    """自动流程试图覆盖用户编辑过的记忆。"""

    code = "memory_overwrite_denied"

    def __init__(self, memory_id: str, actor: str) -> None:
        self.memory_id = memory_id
        self.actor = actor
        super().__init__(
            f"memory {memory_id} was edited by user; automatic actor {actor!r} may not overwrite it"
        )


class ModelStreamError(AgentV2Error):
    """模型流错误（由 provider 抛出或作为流条目返回）。"""

    code = "model_stream_error"

    def __init__(self, error_class: str, message: str, *, retry_after_s: float = 0.0, raw: Any = None) -> None:
        self.error_class = error_class
        self.retry_after_s = float(retry_after_s)
        self.raw = raw
        super().__init__(f"[{error_class}] {message}")


__all__ = [
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
]
