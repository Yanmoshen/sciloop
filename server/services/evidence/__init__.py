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
"""证据链与 Claim 服务（WP13）。

**本模块导出的签名是并行工作包的集成契约，一经发布不得中途修改。**
（WP08 的 idea/可行性证据绑定、WP14 的草稿写作与 Claim 落库、WP15 的草稿阅读器
都在消费它。若确需变更，必须同步 ``contract_changes_requested``。）

对外契约
--------
解析（附录 E.3 统一结构）::

    from services.evidence import resolve_evidence
    payload = await resolve_evidence("paper_span", evidence_id, session=session)
    # → {evidence_id, evidence_type, paper{...}, document_version,
    #    span{section_name,page_number,bbox,char_start,char_end},
    #    quote_text, quote_sha256, verification{hash_match,offset_match,verdict},
    #    gate{ok,parse_status,coverage,evidence_scope}, jump_url, warnings[...]}

绑定（供 WP08 / WP14）::

    from services.evidence import bind_evidence, bind_evidence_detailed
    bound = await bind_evidence("idea", idea_id, candidates, session=session)   # list[dict]
    detail = await bind_evidence_detailed("idea", idea_id, candidates, session=session)
    # → {bound, bound_ids, evidence_ids, rejected[{candidate,code,reason}], warnings}

    候选字段（白名单，多余字段会被拒，避免"静默绑定成功"）:
      {"evidence_type": "paper_span",  "paper_span_id": 8821, "quote_text": "…"（可选）}
      {"evidence_type": "card_field",  "paper_id": 512, "card_field": "core_method",
                                       "paper_span_id": 88（可选）}
      {"evidence_type": "experiment_run", "experiment_run_id": 7,
                                       "metric_name": "accuracy"（可选）}
      {"evidence_type": "experiment_passport", "experiment_passport_id": 3}
      {"evidence_type": "decision",    "decision_log_id": 21}

拆分与判定::

    from services.evidence import split_claims, split_and_persist, verify_claims
    claims = split_claims(content_md, draft_id=draft_id)              # 纯函数，可离线
    stored = await split_and_persist(session, draft_id, content_md)   # 写 draft_claims
    report = await verify_claims(draft_id, session=session)           # 三态 + claim_coverage
    # → {draft_id, draft_found, claim_coverage, counts{...}, claims[...],
    #    unsupported_spans[...], citation_map, warnings[...]}

度量::

    from services.evidence import claim_coverage        # supported ÷ 事实性总数
    # 事实性总数为 0 → None（不用 0/1 冒充）

红线（contracts.evidence_rules / forbidden_actions，全部在实现内强制）
--------------------------------------------------------------------
- ``paper_span`` / 带定位的 ``card_field`` 入库前一定跑 WP05 ``verify_span``：
  **先比 ``quote_sha256`` 再比字符偏移**，``verdict='invalid'`` 直接拒绝绑定。
- 定位必须携带 ``document_version``；**禁止把裸字符偏移当作唯一定位依据**。
- ``fulltext_gate``：只有 ``parse_status='ok'`` 且 ``coverage>=0.60`` 才允许生成正文级
  ``paper_span`` 证据；其余如实标 ``evidence_scope='abstract_only'``。
- 事实性 Claim 必给三态，**不得留空**；无证据必须出现在 ``unsupported_spans`` 里。
- 取不到的数值一律 ``null`` 并披露，禁止编造。

模块布局：
:mod:`records` 纯数据结构与受控值域 ·
:mod:`resolver` 多态解析 ·
:mod:`binder` 绑定与哈希优先校验 ·
:mod:`claim_splitter` 句级拆分 ·
:mod:`integrity_checker` 三态判定与覆盖率统计
"""

from __future__ import annotations

from services.evidence.binder import (
    CANDIDATE_FIELDS,
    bind_evidence,
    bind_evidence_detailed,
    clear_evidence,
    list_evidence,
)
from services.evidence.claim_splitter import (
    MIN_FACTUAL_CHARS,
    classify_factual,
    clean_inline,
    persist_claims,
    split_and_persist,
    split_claims,
)
from services.evidence.integrity_checker import (
    CITATION_RE,
    load_draft,
    make_llm_conflict_detector,
    parse_citation_index,
    parse_evidence_ref,
    ref_to_candidate,
    verify_claims,
    verify_content,
)
from services.evidence.records import (
    CLAIM_STATUSES,
    COVERAGE_SCALE,
    EVIDENCE_TYPES,
    OWNER_TYPES,
    VERDICTS,
    Claim,
    EvidenceError,
    EvidenceNotFoundError,
    EvidenceRow,
    InvalidEvidenceTypeError,
    InvalidOwnerTypeError,
    claim_coverage,
    is_claim_status,
    is_evidence_type,
    is_owner_type,
    status_counts,
)
from services.evidence.resolver import (
    ID_KINDS,
    load_evidence_row,
    resolve_evidence,
    resolve_many,
)

__all__ = [
    # 值域与数据结构
    "CLAIM_STATUSES",
    "COVERAGE_SCALE",
    "EVIDENCE_TYPES",
    "OWNER_TYPES",
    "VERDICTS",
    "Claim",
    "EvidenceRow",
    # 错误
    "EvidenceError",
    "EvidenceNotFoundError",
    "InvalidEvidenceTypeError",
    "InvalidOwnerTypeError",
    # 解析（T1）
    "ID_KINDS",
    "load_evidence_row",
    "resolve_evidence",
    "resolve_many",
    # 绑定（T2）
    "CANDIDATE_FIELDS",
    "bind_evidence",
    "bind_evidence_detailed",
    "clear_evidence",
    "list_evidence",
    # 拆分（T3）
    "MIN_FACTUAL_CHARS",
    "classify_factual",
    "clean_inline",
    "persist_claims",
    "split_and_persist",
    "split_claims",
    # 判定与统计（T4）
    "CITATION_RE",
    "load_draft",
    "make_llm_conflict_detector",
    "parse_citation_index",
    "parse_evidence_ref",
    "ref_to_candidate",
    "verify_claims",
    "verify_content",
    # 度量
    "claim_coverage",
    "is_claim_status",
    "is_evidence_type",
    "is_owner_type",
    "status_counts",
]
