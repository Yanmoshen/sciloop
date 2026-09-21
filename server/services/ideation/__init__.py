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
"""构思域服务出口（WP08-T4）。

模块布局：

- :mod:`idea_generator`  —— 生成（LLM / 规则模板两种模式，如实标注）+ 证据接地
- :mod:`evidence_binder` —— **证据强制绑定**（走 WP13 冻结契约，不另写一套）
- :mod:`idea_service`    —— 手动创建 / 读取 / 列表 / 选中 / 证据读取

硬红线（contracts.evidence_rules.ideation_rule）：
**无 Evidence 的 idea 必须丢弃，不允许输出**——由
:func:`evidence_binder.enforce_evidence_or_drop` 在服务端强制，非前端过滤。
"""

from __future__ import annotations

from services.ideation.evidence_binder import (
    IDEA_OWNER_TYPE,
    REQUIRED_REFS,
    bind_idea_evidence,
    clear_idea_evidence,
    dedupe_candidates,
    enforce_evidence_or_drop,
    enforce_manual_idea,
    gap_candidates,
    idea_evidence_ids,
    normalize_candidates,
)
from services.ideation.idea_generator import (
    MAX_IDEAS,
    MECHANISMS,
    MODE_AUTO,
    MODE_LLM,
    MODE_TEMPLATE,
    ORIGIN_AI,
    ORIGIN_USER,
    IdeaGenerationError,
    build_prompt,
    candidates_for_idea,
    generate_ideas,
)
from services.ideation.idea_service import (
    IdeaError,
    bind_idea_evidences,
    create_manual_idea,
    idea_evidences,
    list_ideas,
    load_idea,
    select_idea,
)

__all__ = [
    # 生成（T4）
    "MAX_IDEAS",
    "MECHANISMS",
    "MODE_AUTO",
    "MODE_LLM",
    "MODE_TEMPLATE",
    "ORIGIN_AI",
    "ORIGIN_USER",
    "IdeaGenerationError",
    "build_prompt",
    "candidates_for_idea",
    "generate_ideas",
    # 证据强制绑定（T4 硬约束）
    "IDEA_OWNER_TYPE",
    "REQUIRED_REFS",
    "bind_idea_evidence",
    "clear_idea_evidence",
    "dedupe_candidates",
    "enforce_evidence_or_drop",
    "enforce_manual_idea",
    "gap_candidates",
    "idea_evidence_ids",
    "normalize_candidates",
    # 读写
    "IdeaError",
    "bind_idea_evidences",
    "create_manual_idea",
    "idea_evidences",
    "list_ideas",
    "load_idea",
    "select_idea",
]
