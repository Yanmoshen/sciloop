# Copyright 2026 SciLoop contributors
# Licensed under the Apache License, Version 2.0 (the "License")
"""沙箱策略枚举（Agent 2 / WP-02）。"""

from __future__ import annotations

from enum import StrEnum


class SandboxPolicy(StrEnum):
    """三类沙箱（文档 WP-02 / §1）。"""

    #: 文件、目录和命令都不能产生写入副作用
    READ_ONLY = "read-only"
    #: 选定工作区可读写执行，工作区外只读
    WORKSPACE_WRITE = "workspace-write"
    #: 宿主机任意目录；只改变根边界和批准默认值
    DANGER_FULL_ACCESS = "danger-full-access"


#: 默认策略
DEFAULT_POLICY = SandboxPolicy.WORKSPACE_WRITE
#: 测试基线策略（刻意不是 read-only：测试必须覆盖工作区可写这条主路径）
TEST_POLICY = SandboxPolicy.WORKSPACE_WRITE


class AccessKind(StrEnum):
    """访问意图。"""

    READ = "read"
    WRITE = "write"
    EXECUTE = "execute"


class SandboxDecision(StrEnum):
    """裁决结果（文档要求的三个取值）。"""

    ALLOW = "allow"
    REQUIRE_APPROVAL = "require_approval"
    DENY = "deny"


__all__ = [
    "SandboxPolicy",
    "DEFAULT_POLICY",
    "TEST_POLICY",
    "AccessKind",
    "SandboxDecision",
]
