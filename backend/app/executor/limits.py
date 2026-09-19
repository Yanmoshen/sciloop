# Copyright 2026 SciLoop contributors
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
"""执行器限额集中管理（WP11-T1）。

限额来源只有一个：环境变量（``contracts.env_keys`` / ``contracts.executor_limits``）。
**禁止**由模板参数、LLM 输出或调用方覆盖这些阈值。

| 限额 | 环境变量 | 契约值 |
| --- | --- | --- |
| 并发上限 | ``EXECUTOR_MAX_CONCURRENCY`` | 2 |
| 单 Run 超时 | ``EXECUTOR_RUN_TIMEOUT_SECONDS`` | 300s |
| 单环节超时 | ``PIPELINE_MAX_STAGE_MINUTES`` * 60 | 1200s |
| 输出字节上限 | ``EXECUTOR_MAX_OUTPUT_BYTES`` | 5 MiB |
| 出网白名单 | ``EXECUTOR_ALLOWED_HOSTS`` | 逗号分隔 |
| 禁内网 | ``EXECUTOR_DENY_PRIVATE_NETWORK`` | true |

层级约束：**单 Run 超时 < 单环节超时**，且两者不得相等（``contracts.guardrails.time``）。
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from typing import Any

from app.executor.errors import LimitsConfigError

#: 样本量上限（``contracts.guardrails.safety``：sample_size <= 50）
SAMPLE_SIZE_MAX = 50
SAMPLE_SIZE_MIN = 1

#: 硬护栏默认值（配置不可用时兜底；与 contracts 一致）
DEFAULT_MAX_CONCURRENCY = 2
DEFAULT_RUN_TIMEOUT_SECONDS = 300
DEFAULT_STAGE_TIMEOUT_SECONDS = 1200
DEFAULT_MAX_OUTPUT_BYTES = 5_242_880

#: artifact 根目录（相对 backend 工作目录；可用环境变量改到挂载卷）
ARTIFACT_ROOT_ENV = "EXECUTOR_ARTIFACT_ROOT"
DEFAULT_ARTIFACT_ROOT = "var/executor_runs"


@dataclass(frozen=True)
class ExecutorLimits:
    """一次执行会话的全部限额（不可变）。"""

    max_concurrency: int
    run_timeout_seconds: int
    stage_timeout_seconds: int
    max_output_bytes: int
    allowed_hosts: tuple[str, ...]
    deny_private_network: bool
    artifact_root: str
    sample_size_min: int = SAMPLE_SIZE_MIN
    sample_size_max: int = SAMPLE_SIZE_MAX

    # ------------------------------------------------------------------ #
    def assert_hierarchy(self) -> None:
        """校验超时层级：``run_timeout < stage_timeout``（不得相等，不得倒挂）。"""
        if self.run_timeout_seconds >= self.stage_timeout_seconds:
            raise LimitsConfigError(
                "超时层级非法：单 Run 超时必须严格小于单环节超时"
                f"（当前 run={self.run_timeout_seconds}s, stage={self.stage_timeout_seconds}s）。"
                "请调整 EXECUTOR_RUN_TIMEOUT_SECONDS / PIPELINE_MAX_STAGE_MINUTES。",
                detail={
                    "run_timeout_seconds": self.run_timeout_seconds,
                    "stage_timeout_seconds": self.stage_timeout_seconds,
                },
            )
        if self.max_concurrency < 1:
            raise LimitsConfigError(
                f"EXECUTOR_MAX_CONCURRENCY 必须 >= 1（当前 {self.max_concurrency}）",
                detail={"max_concurrency": self.max_concurrency},
            )
        if self.max_output_bytes < 1:
            raise LimitsConfigError(
                f"EXECUTOR_MAX_OUTPUT_BYTES 必须 >= 1（当前 {self.max_output_bytes}）",
                detail={"max_output_bytes": self.max_output_bytes},
            )

    def clamp_sample_size(self, sample_size: Any) -> int:
        """样本量校验：必须是 1..50 的整数，越界即拒（**不做隐式截断**）。"""
        if isinstance(sample_size, bool) or not isinstance(sample_size, int):
            raise LimitsConfigError(
                f"sample_size 必须是整数（收到 {type(sample_size).__name__}）",
                detail={"sample_size": sample_size, "max": self.sample_size_max},
            )
        if not (self.sample_size_min <= sample_size <= self.sample_size_max):
            raise LimitsConfigError(
                f"sample_size={sample_size} 超出允许区间 "
                f"[{self.sample_size_min}, {self.sample_size_max}]（契约硬约束，禁止放宽）",
                detail={"sample_size": sample_size, "max": self.sample_size_max},
            )
        return sample_size

    def to_dict(self) -> dict[str, Any]:
        return {
            "max_concurrency": self.max_concurrency,
            "run_timeout_seconds": self.run_timeout_seconds,
            "stage_timeout_seconds": self.stage_timeout_seconds,
            "max_output_bytes": self.max_output_bytes,
            "allowed_hosts": list(self.allowed_hosts),
            "deny_private_network": self.deny_private_network,
            "artifact_root": self.artifact_root,
            "sample_size_min": self.sample_size_min,
            "sample_size_max": self.sample_size_max,
            "source": "env:EXECUTOR_* / PIPELINE_MAX_STAGE_MINUTES",
        }


def _int_env(name: str, default: int) -> int:
    raw = os.environ.get(name)
    if raw is None or str(raw).strip() == "":
        return default
    try:
        return int(str(raw).strip())
    except ValueError:
        return default


def get_limits() -> ExecutorLimits:
    """从环境配置构造限额（每次调用重新读取，便于验收时按进程覆盖环境变量）。"""
    from app.core.config import get_settings

    settings = get_settings()
    stage_timeout = int(getattr(settings, "pipeline_max_stage_minutes", 20) or 20) * 60
    run_timeout = int(
        getattr(settings, "executor_run_timeout_seconds", DEFAULT_RUN_TIMEOUT_SECONDS)
    )
    limits = ExecutorLimits(
        max_concurrency=max(1, int(getattr(settings, "executor_max_concurrency", DEFAULT_MAX_CONCURRENCY))),
        run_timeout_seconds=max(1, run_timeout),
        # 契约：单环节超时必须严格大于单 Run 超时
        stage_timeout_seconds=max(stage_timeout, run_timeout + 1),
        max_output_bytes=max(1, _int_env("EXECUTOR_MAX_OUTPUT_BYTES", DEFAULT_MAX_OUTPUT_BYTES)),
        allowed_hosts=tuple(getattr(settings, "allowed_hosts", []) or []),
        deny_private_network=bool(getattr(settings, "executor_deny_private_network", True)),
        artifact_root=(os.environ.get(ARTIFACT_ROOT_ENV) or DEFAULT_ARTIFACT_ROOT).strip(),
    )
    limits.assert_hierarchy()
    return limits


__all__ = [
    "ARTIFACT_ROOT_ENV",
    "DEFAULT_ARTIFACT_ROOT",
    "DEFAULT_MAX_CONCURRENCY",
    "DEFAULT_MAX_OUTPUT_BYTES",
    "DEFAULT_RUN_TIMEOUT_SECONDS",
    "DEFAULT_STAGE_TIMEOUT_SECONDS",
    "ExecutorLimits",
    "SAMPLE_SIZE_MAX",
    "SAMPLE_SIZE_MIN",
    "get_limits",
]
