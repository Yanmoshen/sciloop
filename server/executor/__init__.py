# Copyright 2026 SciLoop contributors
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
"""进程内受限执行器（WP11 独占）。

对外入口：``from executor import execute_template, get_limits``。

注意：本包**不是**通用代码执行器——没有任何 ``eval`` / ``exec`` / shell 入口，
可执行动作仅限「通过模板注册表调用已注册模板」。
"""

from __future__ import annotations

from executor.artifact_collector import ArtifactCollector, canonical_json, sha256_hex
from executor.egress_proxy import EgressPolicy, get_policy, guarded_request, recent_decisions
from executor.errors import (
    EgressDenied,
    ExecutorError,
    ExecutorTimeoutError,
    LimitsConfigError,
    ModelUnavailableError,
    OutputLimitExceededError,
    ReplaySourceMissError,
    SampleSizeExceededError,
    TemplateNotImplementedError,
    TemplateRejectedError,
)
from executor.limits import ExecutorLimits, get_limits
from executor.runner import (
    RunContext,
    RunOutcome,
    concurrency_snapshot,
    execute_template,
    reset_concurrency_stats,
    run_with_limits,
)

__all__ = [
    "ArtifactCollector",
    "EgressDenied",
    "EgressPolicy",
    "ExecutorError",
    "ExecutorLimits",
    "ExecutorTimeoutError",
    "LimitsConfigError",
    "ModelUnavailableError",
    "OutputLimitExceededError",
    "ReplaySourceMissError",
    "RunContext",
    "RunOutcome",
    "SampleSizeExceededError",
    "TemplateNotImplementedError",
    "TemplateRejectedError",
    "canonical_json",
    "concurrency_snapshot",
    "execute_template",
    "get_limits",
    "get_policy",
    "guarded_request",
    "recent_decisions",
    "reset_concurrency_stats",
    "run_with_limits",
    "sha256_hex",
]
