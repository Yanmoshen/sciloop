# Copyright 2026 SciLoop contributors
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
"""执行器错误类型（WP11-T1）。

错误分级与 ``contracts`` 对齐：

- :class:`ExecutorError` —— 执行器可预期失败的基类（携带 ``status`` 供 ``experiment_runs`` 落库）
- :class:`LimitsConfigError` —— 限额配置非法（如单 Run 超时 >= 单环节超时）→ 拒绝启动
- :class:`EgressDenied` —— 出网目标不在白名单或属内网（安全红线，**不可降级**）
"""

from __future__ import annotations

from typing import Any


class ExecutorError(RuntimeError):
    """执行器可预期失败（会被记录到 ``experiment_runs.status/error``，不得静默吞掉）。"""

    #: 写入 ``experiment_runs.status``
    status = "failed"
    code = "executor_error"

    def __init__(self, message: str, *, detail: Any = None) -> None:
        self.detail = detail
        super().__init__(message)

    def to_dict(self) -> dict[str, Any]:
        return {"code": self.code, "status": self.status, "message": str(self), "detail": self.detail}


class LimitsConfigError(ExecutorError):
    """限额配置非法（配置层错误，不执行任何模板）。"""

    code = "limits_config_invalid"
    status = "failed"


class TemplateRejectedError(ExecutorError):
    """模板被拒（未注册 / 未实现 / 参数不合 schema / 样本量越界）。"""

    code = "template_rejected"
    status = "rejected"


class TemplateNotImplementedError(TemplateRejectedError):
    """模板仍为占位条目（T2/T4/T5，P1 范围）：**必须显式抛错**，禁止静默返回空结果。"""

    code = "template_not_implemented"
    status = "rejected"


class SampleSizeExceededError(TemplateRejectedError):
    """样本量越界（``contracts.guardrails.safety``：``sample_size<=50``，**不可放宽**）。"""

    code = "sample_size_exceeded"
    status = "rejected"


class ExecutorTimeoutError(ExecutorError):
    """单 Run 超时（``EXECUTOR_RUN_TIMEOUT_SECONDS``）。"""

    code = "run_timeout"
    status = "timeout"


class OutputLimitExceededError(ExecutorError):
    """输出超过 ``EXECUTOR_MAX_OUTPUT_BYTES``。"""

    code = "output_too_large"
    status = "failed"


class EgressDenied(ExecutorError):
    """出网被拒（白名单外或内网地址）——安全红线。"""

    code = "egress_denied"
    status = "failed"


class ModelUnavailableError(ExecutorError):
    """模板所需模型无法解析（无路由 / 无 key）——如实失败，不伪造结果。"""

    code = "model_unavailable"
    status = "failed"


class ReplaySourceMissError(ExecutorError):
    """回放源缺少某样本的原始输出——**禁止用实时调用顶替**（``is_replay`` 必须如实）。"""

    code = "replay_source_miss"
    status = "failed"


__all__ = [
    "EgressDenied",
    "ExecutorError",
    "ExecutorTimeoutError",
    "LimitsConfigError",
    "ModelUnavailableError",
    "OutputLimitExceededError",
    "ReplaySourceMissError",
    "SampleSizeExceededError",
    "TemplateNotImplementedError",
    "TemplateRejectedError",
]
