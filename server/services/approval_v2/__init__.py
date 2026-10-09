# Copyright 2026 SciLoop contributors
# Licensed under the Apache License, Version 2.0 (the "License")
"""approval_v2：权限事实、审批状态机、持续批准与令牌（Agent 2 / WP-03、WP-04）。

    from services.approval_v2 import ApprovalManager
    from services.sandbox_v2 import SandboxManager

    sandbox = SandboxManager(workspace)
    manager = ApprovalManager(sandbox=sandbox)
    check = sandbox.check_command_paths(["rm", "-rf", "build"])
    verdict = manager.assess_command(["rm", "-rf", "build"], cwd=workspace, thread_id=th,
                                     sandbox_verdict=check.verdict.to_dict())
    if verdict.required:
        view = manager.request_for_assessment(verdict, thread_id=th, turn_id=tu, call_id=call)
"""

from __future__ import annotations

from .grants import (
    ARG_EXACT,
    ARG_PREFIX,
    CWD_EXACT,
    CWD_WITHIN,
    SCOPE_THREAD,
    ArgConstraints,
    CommandGrant,
    GrantStore,
)
from .manager import (
    ALLOW,
    DEFAULT_APPROVAL_TTL_S,
    REQUIRE,
    ApprovalManager,
)
from .models import (
    ApprovalView,
    DecisionScope,
    RiskAssessment,
    RiskCategory,
    RiskLevel,
    analysis_view,
)
from .prompt_facts import (
    CATEGORY_LABELS,
    FORBIDDEN_PATTERNS,
    assert_no_secrets,
    check_no_secrets,
    permission_facts,
    render_permission_facts,
)
from .rules import (
    DEFAULT_ALLOW_PREFIXES,
    KNOWN_HOST_PROGRAMS,
    CommandAnalysis,
    RiskPolicy,
    basename,
    normalize_argv,
)
from .store import ApprovalStore
from .tokens import (
    DEFAULT_TOKEN_TTL_S,
    ApprovalToken,
    ApprovalTokenStore,
    shift_iso,
)

__all__ = [
    "ApprovalManager",
    "ApprovalView",
    "DecisionScope",
    "RiskAssessment",
    "RiskPolicy",
    "RiskLevel",
    "RiskCategory",
    "CommandAnalysis",
    "analysis_view",
    "ALLOW",
    "REQUIRE",
    "DEFAULT_APPROVAL_TTL_S",
    "ArgConstraints",
    "CommandGrant",
    "GrantStore",
    "SCOPE_THREAD",
    "ARG_EXACT",
    "ARG_PREFIX",
    "CWD_EXACT",
    "CWD_WITHIN",
    "ApprovalStore",
    "ApprovalToken",
    "ApprovalTokenStore",
    "DEFAULT_TOKEN_TTL_S",
    "shift_iso",
    "permission_facts",
    "render_permission_facts",
    "assert_no_secrets",
    "check_no_secrets",
    "FORBIDDEN_PATTERNS",
    "CATEGORY_LABELS",
    "DEFAULT_ALLOW_PREFIXES",
    "KNOWN_HOST_PROGRAMS",
    "basename",
    "normalize_argv",
]
