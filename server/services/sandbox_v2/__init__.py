# Copyright 2026 SciLoop contributors
# Licensed under the Apache License, Version 2.0 (the "License")
"""sandbox_v2：策略、路径边界与平台能力（Agent 2 / WP-02）。

    from services.sandbox_v2 import SandboxManager, SandboxPolicy, AccessKind

    sandbox = SandboxManager(workspace_root, policy=SandboxPolicy.WORKSPACE_WRITE)
    verdict = sandbox.check_write("out.txt")          # allow / deny / require_approval
    check = sandbox.check_command_paths(["rm", "-rf", "build"])   # cwd + 参数 + 重定向 + 升级
    sandbox.facts()                                   # 可安全交给模型的事实摘要
    sandbox.capability()                              # 平台隔离能力（如实报告）
"""

from __future__ import annotations

from .linux import landlock_available
from .manager import DEFAULT_TEST_POLICY, HOST_PROGRAMS, SandboxFacts, SandboxManager
from .models import (
    SEVERITY,
    AccessVerdict,
    CommandVerdict,
    PlatformCapability,
    ProtectedRule,
)
from .policy import (
    DEFAULT_POLICY,
    TEST_POLICY,
    AccessKind,
    SandboxDecision,
    SandboxPolicy,
)
from .resolver import (
    CASE_INSENSITIVE,
    DEFAULT_PROTECTED_RULES,
    INLINE_CODE_FLAGS,
    SUSPICIOUS_CODE_MARKERS,
    WRITE_COMMANDS,
    extract_argv_paths,
    inline_code_suspicion,
    is_protected,
    is_within,
    is_write_command,
    looks_like_path,
    match_protected_rule,
    matched_root,
    normalize,
    normalize_key,
    redirect_targets,
    resolve_for_check,
    to_absolute,
)
from .windows import is_reparse_point

__all__ = [
    "SandboxManager",
    "SandboxFacts",
    "HOST_PROGRAMS",
    "DEFAULT_TEST_POLICY",
    "AccessVerdict",
    "CommandVerdict",
    "PlatformCapability",
    "ProtectedRule",
    "SEVERITY",
    "SandboxPolicy",
    "DEFAULT_POLICY",
    "TEST_POLICY",
    "AccessKind",
    "SandboxDecision",
    "DEFAULT_PROTECTED_RULES",
    "WRITE_COMMANDS",
    "INLINE_CODE_FLAGS",
    "SUSPICIOUS_CODE_MARKERS",
    "CASE_INSENSITIVE",
    "to_absolute",
    "normalize",
    "resolve_for_check",
    "normalize_key",
    "is_within",
    "matched_root",
    "match_protected_rule",
    "is_protected",
    "looks_like_path",
    "extract_argv_paths",
    "redirect_targets",
    "is_write_command",
    "inline_code_suspicion",
    "is_reparse_point",
    "landlock_available",
]
