# Copyright 2026 SciLoop contributors
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
"""成本域服务（WP02 独占）。

对外两个消费面：

- WP10 护栏：``from app.services.cost import check_cost`` → 短路判定里的 ``cost_ok``
- 前端/看板：``GET /api/v1/costs/summary``（护栏 8.0 与演示配额 3.0 双线）
"""

from __future__ import annotations

from app.services.cost.accumulator import (
    DEFAULT_LIMIT_USD,
    DEFAULT_QUOTA_USD,
    CostCheck,
    CostSummary,
    accumulate,
    check_cost,
    default_limits,
    get_used_usd,
    guardrail_snapshot,
)
from app.services.cost.classification import (
    DEFAULT_NONBILLABLE_MARKERS,
    MARKERS_ENV,
    classification_rules,
    classify_call,
    nonbillable_markers,
)
from app.services.cost.queries import CostBucket, audit_cost_rows, fetch_cost_buckets

__all__ = [
    "DEFAULT_LIMIT_USD",
    "DEFAULT_NONBILLABLE_MARKERS",
    "DEFAULT_QUOTA_USD",
    "MARKERS_ENV",
    "CostBucket",
    "CostCheck",
    "CostSummary",
    "accumulate",
    "audit_cost_rows",
    "check_cost",
    "classification_rules",
    "classify_call",
    "default_limits",
    "fetch_cost_buckets",
    "get_used_usd",
    "guardrail_snapshot",
    "nonbillable_markers",
]
