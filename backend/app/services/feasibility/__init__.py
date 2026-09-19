# Copyright 2026 SciLoop contributors
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.
"""可行性与任务书服务出口（WP08-T5 / T6）。

模块布局：

- :mod:`scorer`             四维评分卡（每维 ``{score, rationale, evidence[], signals, formula}``）
- :mod:`risk_analyzer`      风险清单 ``[{risk, level, mitigation}]``（规则可复核）
- :mod:`mve_planner`        最小可行实验建议（含 P0 模板、可照做的步骤、人工核对清单）
- :mod:`taskbook_service`   任务书创建 / 编辑 / 锁定（锁定后只读，PATCH → 409）
- :mod:`feasibility_service` 编排与落库（四维分 + 风险 + MVE + 逐维证据绑定后写回）

两条硬红线在本包内强制：

1. 四维分**无黑箱总分**——每维都有 ``signals``（真实输入）与 ``formula``（可手算），
   总分是显式加权和；LLM 只能给 ``llm_suggestion``，不参与计算。
2. 任务书 ``compute_budget`` **不可放宽**成本护栏（硬线 8.0 USD），试图放宽直接 422。
"""

from __future__ import annotations

from app.services.feasibility.feasibility_service import (
    FEASIBILITY_OWNER_TYPE,
    FeasibilityError,
    create_feasibility,
    get_feasibility,
    latest_feasibility_for_idea,
)
from app.services.feasibility.mve_planner import (
    DEFAULT_ITERATIONS,
    SAMPLE_SIZE_LIMIT,
    TEMPLATE_P0,
    build_mve_plan,
    choose_template,
)
from app.services.feasibility.risk_analyzer import (
    LEVEL_HIGH,
    LEVEL_LOW,
    LEVEL_MEDIUM,
    LEVELS,
    THRESHOLDS,
    analyze,
)
from app.services.feasibility.scorer import (
    DIMENSIONS,
    WEIGHTS,
    build_scoring_payload,
    build_signal_bundle,
    extract_dataset_mentions,
    field_terms,
    real_terms,
    score_compute_cost,
    score_data_availability,
    score_method_maturity,
    score_novelty_gap,
)
from app.services.feasibility.taskbook_service import (
    DELIVERABLE_OPTIONS,
    STATUS_DRAFT,
    STATUS_LOCKED,
    STATUSES,
    TaskbookError,
    TaskbookLockedError,
    create_taskbook,
    default_compute_budget,
    default_rounds,
    get_taskbook,
    list_taskbooks,
    lock_taskbook,
    update_taskbook,
)

__all__ = [
    # 四维评分卡（T5）
    "DIMENSIONS",
    "WEIGHTS",
    "build_scoring_payload",
    "build_signal_bundle",
    "extract_dataset_mentions",
    "field_terms",
    "real_terms",
    "score_compute_cost",
    "score_data_availability",
    "score_method_maturity",
    "score_novelty_gap",
    # 风险清单（T5）
    "LEVELS",
    "LEVEL_HIGH",
    "LEVEL_LOW",
    "LEVEL_MEDIUM",
    "THRESHOLDS",
    "analyze",
    # MVE（T5）
    "DEFAULT_ITERATIONS",
    "SAMPLE_SIZE_LIMIT",
    "TEMPLATE_P0",
    "build_mve_plan",
    "choose_template",
    # 任务书（T6）
    "DELIVERABLE_OPTIONS",
    "STATUSES",
    "STATUS_DRAFT",
    "STATUS_LOCKED",
    "TaskbookError",
    "TaskbookLockedError",
    "create_taskbook",
    "default_compute_budget",
    "default_rounds",
    "get_taskbook",
    "list_taskbooks",
    "lock_taskbook",
    "update_taskbook",
    # 编排（T5）
    "FEASIBILITY_OWNER_TYPE",
    "FeasibilityError",
    "create_feasibility",
    "get_feasibility",
    "latest_feasibility_for_idea",
]
