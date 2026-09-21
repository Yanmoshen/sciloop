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
"""聚合域服务出口（WP08-T1 / T2 / T3）。

模块布局：

- :mod:`cards`            真实卡片 / span 读取层（一次查询，避免 N+1）
- :mod:`span_locator`     实词覆盖式 span 定位（只做定位，不做事实验证）
- :mod:`matrix`           对比矩阵（附录 A.3 ``comparison_matrix``）
- :mod:`evolution`        方法演进关系（附录 A.3 ``method_evolution``）
- :mod:`gap_finder`       空白清单（附录 A.3 ``gaps``）
- :mod:`aggregation_service` 编排与落库（``aggregations`` + ``gaps`` 同事务）

证据纪律：三份产物都只消费 ``paper_cards``（WP06 已核对）与 ``paper_spans``
（WP05 已校验）的真实数据；取不到就置 ``None`` 或标注缺失，**从不编造**。
"""

from __future__ import annotations

from services.aggregation.aggregation_service import (
    AggregationError,
    build_aggregation_payload,
    create_aggregation,
    delete_aggregation,
    get_aggregation,
    list_aggregations,
    load_gaps,
)
from services.aggregation.cards import (
    CARD_FIELDS,
    FULLTEXT_GATE_COVERAGE,
    SCOPE_ABSTRACT_ONLY,
    SCOPE_FULLTEXT,
    card_field_candidate,
    coverage_tag,
    entry_note,
    entry_span,
    entry_text,
    existing_paper_ids,
    load_cards,
    scope_of,
)
from services.aggregation.evolution import (
    build_evolution,
    build_evolution_payload,
)
from services.aggregation.gap_finder import (
    CUE_RE,
    UNSOLVED_SCOPE_NOTE,
    build_gaps,
    build_gaps_payload,
)
from services.aggregation.matrix import (
    DIMENSIONS,
    MAX_PAPERS,
    MIN_PAPERS,
    build_matrix,
    build_matrix_payload,
)
from services.aggregation.span_locator import (
    LOCATOR_CARD,
    LOCATOR_EXACT,
    LOCATOR_OVERLAP,
    locate_in_spans,
    span_payload,
)

__all__ = [
    # 卡片读取层
    "CARD_FIELDS",
    "FULLTEXT_GATE_COVERAGE",
    "SCOPE_ABSTRACT_ONLY",
    "SCOPE_FULLTEXT",
    "card_field_candidate",
    "coverage_tag",
    "entry_note",
    "entry_span",
    "entry_text",
    "existing_paper_ids",
    "load_cards",
    "scope_of",
    # 定位层
    "LOCATOR_CARD",
    "LOCATOR_EXACT",
    "LOCATOR_OVERLAP",
    "locate_in_spans",
    "span_payload",
    # 对比矩阵（T1）
    "DIMENSIONS",
    "MAX_PAPERS",
    "MIN_PAPERS",
    "build_matrix",
    "build_matrix_payload",
    # 方法演进（T2）
    "build_evolution",
    "build_evolution_payload",
    # 空白清单（T3）
    "CUE_RE",
    "UNSOLVED_SCOPE_NOTE",
    "build_gaps",
    "build_gaps_payload",
    # 编排与落库
    "AggregationError",
    "build_aggregation_payload",
    "create_aggregation",
    "delete_aggregation",
    "get_aggregation",
    "list_aggregations",
    "load_gaps",
]
