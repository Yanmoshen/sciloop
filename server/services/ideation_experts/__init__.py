"""固定 20 专家 Idea 生成流水线。

该包只描述 Idea 专家协议与执行，不负责论文读取或数据库落库。调用方把论文解析卡、
总结和可定位证据组成 :class:`ResearchContext` 后，使用 :class:`IdeationExpertRunner`
执行一次不可静默重试的 20 专家运行。
"""

from .registry import (
    EXPERT_ROLES,
    ROLE_REGISTRY,
    SCORE_WEIGHTS,
    ExpertRole,
    get_role,
    topological_levels,
)
from .runner import IdeationExpertError, IdeationExpertRunner
from .schemas import (
    EvidenceRef,
    ExpertCallResult,
    IdeationRunResult,
    ResearchContext,
    validate_role_payload,
)

__all__ = [
    "EXPERT_ROLES",
    "ROLE_REGISTRY",
    "SCORE_WEIGHTS",
    "ExpertRole",
    "get_role",
    "topological_levels",
    "EvidenceRef",
    "IdeationRunResult",
    "ExpertCallResult",
    "ResearchContext",
    "validate_role_payload",
    "IdeationExpertRunner",
    "IdeationExpertError",
]
