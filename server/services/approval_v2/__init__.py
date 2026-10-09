# Copyright 2026 SciLoop contributors
# Licensed under the Apache License, Version 2.0 (the "License")
"""approval_v2：审批判定、状态机与持续批准（Agent 2 / WP-05）。

    from services.approval_v2 import ApprovalManager

    manager = ApprovalManager()
    assessment = manager.assess_command(["rm", "-rf", "build"], cwd=root, thread_id=th)
    if assessment.required:
        approval = manager.request_for_assessment(assessment, thread_id=th, turn_id=tu, call_id=call)
"""

from __future__ import annotations

from .grants import SCOPE_THREAD, CommandGrant, GrantStore
from .manager import (
    ALLOW,
    DEFAULT_APPROVAL_TTL_S,
    REQUIRE,
    ApprovalManager,
    RiskAssessment,
)
from .risk import (
    DEFAULT_ALLOW_PREFIXES,
    CommandAnalysis,
    RiskCategory,
    RiskLevel,
    RiskPolicy,
)

__all__ = [
    "ApprovalManager",
    "RiskAssessment",
    "ALLOW",
    "REQUIRE",
    "DEFAULT_APPROVAL_TTL_S",
    "CommandGrant",
    "GrantStore",
    "SCOPE_THREAD",
    "RiskPolicy",
    "RiskLevel",
    "RiskCategory",
    "CommandAnalysis",
    "DEFAULT_ALLOW_PREFIXES",
]
