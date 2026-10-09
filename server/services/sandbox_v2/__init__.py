# Copyright 2026 SciLoop contributors
# Licensed under the Apache License, Version 2.0 (the "License")
"""sandbox_v2：路径边界与策略裁决（Agent 2 / WP-05）。

    from services.sandbox_v2 import SandboxManager, SandboxPolicy, AccessKind

    sandbox = SandboxManager(workspace_root, policy=SandboxPolicy.WORKSPACE_WRITE)
    verdict = sandbox.check_path("out.txt", AccessKind.WRITE)
"""

from __future__ import annotations

from .manager import DEFAULT_TEST_POLICY, HOST_PROGRAMS, SandboxManager, WorkspaceCheck
from .paths import (
    PROTECTED_DIRS,
    PROTECTED_FILES,
    PROTECTED_SUFFIXES,
    extract_argv_paths,
    is_protected,
    is_within,
    looks_like_path,
    normalize,
    redirect_targets,
)
from .policy import (
    DEFAULT_POLICY,
    TEST_POLICY,
    AccessKind,
    AccessVerdict,
    SandboxDecision,
    SandboxPolicy,
)

__all__ = [
    "SandboxManager",
    "WorkspaceCheck",
    "HOST_PROGRAMS",
    "DEFAULT_TEST_POLICY",
    "PROTECTED_DIRS",
    "PROTECTED_FILES",
    "PROTECTED_SUFFIXES",
    "extract_argv_paths",
    "is_protected",
    "is_within",
    "looks_like_path",
    "normalize",
    "redirect_targets",
    "DEFAULT_POLICY",
    "TEST_POLICY",
    "AccessKind",
    "AccessVerdict",
    "SandboxDecision",
    "SandboxPolicy",
]
