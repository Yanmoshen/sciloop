"""model_gateway_v2：供应商无关的模型网关。

    from services.model_gateway_v2 import ModelGateway, RetryPolicy

    gateway = ModelGateway(provider, classifier=my_classifier)
    # 重试由 TurnRuntime 负责（见 runtime.py 的说明）
"""

from __future__ import annotations

from .gateway import ModelGateway, RetryPolicy, classify_default, error_class_of, is_error

__all__ = [
    "ModelGateway",
    "RetryPolicy",
    "classify_default",
    "error_class_of",
    "is_error",
]
