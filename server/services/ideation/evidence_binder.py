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
"""idea 证据强制绑定（WP08-T4，硬约束「无 Evidence 的 idea 必须丢弃」）。

实现纪律
--------
- **绑定一律走 WP13 冻结契约** :func:`services.evidence.bind_evidence_detailed`，
  本模块**不自己写 Evidence 落库逻辑**，也不绕过哈希优先校验与 ``fulltext_gate``。
- 服务端硬校验发生在两个时刻：
  1. 生成阶段：``generate`` 返回前逐条检查 ``bound_ids``，为空即**丢弃**并
     以 ``logger.warning`` 记录（含丢弃原因，可审计）；
  2. 手动阶段：``POST /ideas`` 落库后若无证据，只能作为「草稿 idea」存在，
     ``enforce_manual_idea`` 会返回 ``needs_evidence=true``，UI 必须提示绑定证据。
- 候选字段白名单对齐 WP13 ``CANDIDATE_FIELDS``：多出的键在进入绑定前被显式剥离并
  记入 ``stripped_fields``，避免 WP13 直接整条拒绝（可审计，不静默）。
"""

from __future__ import annotations

import logging
from collections.abc import Iterable, Mapping, Sequence
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from services.evidence import (
    CANDIDATE_FIELDS,
    InvalidOwnerTypeError,
    bind_evidence_detailed,
    list_evidence,
)

logger = logging.getLogger("sciloop.wp08.evidence_binder")

#: 本包使用的 evidence owner 类型（WP13 `OWNER_TYPES` 含 ``idea``）
IDEA_OWNER_TYPE = "idea"

#: 生成阶段允许出现的候选字段（= WP13 白名单，保持同源）
_ALLOWED = set(CANDIDATE_FIELDS)

#: 各证据类型的必填定位字段（与 WP13 ``_REQUIRED_REFS`` 同口径，用于提前丢弃明显残缺项）
REQUIRED_REFS: dict[str, tuple[str, ...]] = {
    "paper_span": ("paper_span_id",),
    "card_field": ("paper_id", "card_field"),
    "experiment_run": ("experiment_run_id",),
    "experiment_passport": ("experiment_passport_id",),
    "decision": ("decision_log_id",),
}


def normalize_candidates(
    candidates: Iterable[Mapping[str, Any]] | None,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """剥离白名单外字段并丢弃必填项残缺的候选。

    返回 ``(usable, rejected)``；``rejected`` 中每条含 ``code`` 与 ``reason``，
    供 API 层如实回显（不静默吞掉）。
    """
    usable: list[dict[str, Any]] = []
    rejected: list[dict[str, Any]] = []
    for raw in candidates or []:
        if not isinstance(raw, Mapping):
            rejected.append(
                {"candidate": raw, "code": "invalid_candidate", "reason": "候选必须是对象"}
            )
            continue
        stripped = sorted(set(raw) - _ALLOWED)
        candidate = {key: value for key, value in raw.items() if key in _ALLOWED}
        evidence_type = str(candidate.get("evidence_type") or "")
        if evidence_type not in REQUIRED_REFS:
            rejected.append(
                {
                    "candidate": dict(raw),
                    "code": "invalid_evidence_type",
                    "reason": f"evidence_type='{evidence_type}' 不在受控值域内",
                }
            )
            continue
        missing = [
            field
            for field in REQUIRED_REFS[evidence_type]
            if candidate.get(field) is None
            or (isinstance(candidate.get(field), str) and not str(candidate[field]).strip())
        ]
        if missing:
            rejected.append(
                {
                    "candidate": dict(raw),
                    "code": "missing_required_ref",
                    "reason": f"{evidence_type} 证据缺少必填字段 {missing}",
                }
            )
            continue
        if stripped:
            candidate["_stripped_fields"] = stripped
        usable.append(candidate)
    return usable, rejected


def _strip_internal(candidates: Sequence[Mapping[str, Any]]) -> list[dict[str, Any]]:
    """移除仅用于审计的内部键，避免 WP13 因未知字段整条拒绝。"""
    cleaned: list[dict[str, Any]] = []
    for candidate in candidates:
        cleaned.append({key: value for key, value in candidate.items() if not key.startswith("_")})
    return cleaned


def gap_candidates(gap: Mapping[str, Any]) -> list[dict[str, Any]]:
    """把一条 Gap 的真实证据转成 idea 的**接地候选**。

    空白清单里的 ``unsolved_evidence``（真实 ``paper_span``）与
    ``raised_by[].evidence``（``card_field``）都是已存在的数据，可直接绑定为
    idea 证据——这正是「idea 必须由证据支撑」的落地方式。
    """
    candidates: list[dict[str, Any]] = []
    for span in gap.get("unsolved_evidence") or []:
        if not isinstance(span, Mapping) or span.get("paper_span_id") is None:
            continue
        candidate: dict[str, Any] = {
            "evidence_type": "paper_span",
            "paper_span_id": int(span["paper_span_id"]),
            "paper_id": span.get("paper_id"),
            "weight": 1.0,
        }
        if span.get("quote_text"):
            candidate["quote_text"] = span["quote_text"]
        candidates.append(candidate)

    for ref in gap.get("raised_by") or []:
        if not isinstance(ref, Mapping):
            continue
        evidence = ref.get("evidence")
        if not isinstance(evidence, Mapping):
            continue
        candidate = evidence.get("candidate")
        if isinstance(candidate, Mapping):
            candidates.append(dict(candidate))
        elif ref.get("paper_id") is not None:
            candidates.append(
                {
                    "evidence_type": "card_field",
                    "paper_id": int(ref["paper_id"]),
                    "card_field": str(ref.get("card_field") or "limitations"),
                }
            )
    return candidates


def dedupe_candidates(candidates: Sequence[Mapping[str, Any]]) -> list[dict[str, Any]]:
    """按自然键去重（同一 id 重复提交会让 WP13 报 duplicate 噪声）。"""
    seen: set[tuple[Any, ...]] = set()
    unique: list[dict[str, Any]] = []
    for candidate in candidates:
        key = (
            candidate.get("evidence_type"),
            candidate.get("paper_span_id"),
            candidate.get("paper_id"),
            candidate.get("card_field"),
            candidate.get("experiment_run_id"),
            candidate.get("experiment_passport_id"),
            candidate.get("decision_log_id"),
            candidate.get("metric_name"),
        )
        if key in seen:
            continue
        seen.add(key)
        unique.append(dict(candidate))
    return unique


async def bind_idea_evidence(
    session: AsyncSession,
    idea_id: int,
    candidates: Sequence[Mapping[str, Any]] | None,
    *,
    replace: bool = False,
) -> dict[str, Any]:
    """绑定 idea 证据（走 WP13），返回 ``{bound, bound_ids, rejected, warnings, ...}``。"""
    usable, pre_rejected = normalize_candidates(candidates)
    payload = _strip_internal(dedupe_candidates(usable))
    detail = await bind_evidence_detailed(
        IDEA_OWNER_TYPE, int(idea_id), payload, session=session, replace=replace
    )
    detail["pre_rejected"] = pre_rejected
    detail["rejected_total"] = len(pre_rejected) + len(detail.get("rejected") or [])
    detail["submitted"] = len(payload)
    detail["has_evidence"] = bool(detail.get("bound_ids"))
    if not detail["has_evidence"]:
        logger.warning(
            "idea#%s 绑定后仍无 Evidence：submitted=%d rejected=%d codes=%s",
            idea_id,
            detail["submitted"],
            detail["rejected_total"],
            [item.get("code") for item in (detail.get("rejected") or [])]
            + [item.get("code") for item in pre_rejected],
        )
    return detail


async def idea_evidence_ids(session: AsyncSession, idea_id: int) -> list[int]:
    """读该 idea 已绑定的证据 id（WP13 ``list_evidence``，含坏行容错）。"""
    rows = await list_evidence(IDEA_OWNER_TYPE, int(idea_id), session=session)
    return [int(row["evidence_id"]) for row in rows if row.get("evidence_id") is not None]


async def enforce_evidence_or_drop(
    session: AsyncSession, idea_ids: Sequence[int], *, context: str = "generate"
) -> dict[str, Any]:
    """服务端硬校验：无 Evidence 的 idea 一律丢弃并记 warning。

    返回 ``{kept, dropped, details}``；``dropped`` 里带 ``idea_id`` 与
    ``reason``，供 API 如实回显 ``discarded`` 明细（**不静默过滤**）。
    """
    kept: list[int] = []
    dropped: list[dict[str, Any]] = []
    details: list[dict[str, Any]] = []
    for idea_id in idea_ids:
        ids = await idea_evidence_ids(session, int(idea_id))
        details.append({"idea_id": int(idea_id), "evidence_ids": ids, "evidence_count": len(ids)})
        if ids:
            kept.append(int(idea_id))
        else:
            dropped.append(
                {
                    "idea_id": int(idea_id),
                    "reason": "no_evidence",
                    "message": "无 Evidence 的 idea 必须丢弃，不允许输出（contracts.evidence_rules.ideation_rule）",
                }
            )
            logger.warning(
                "丢弃无 Evidence 的 idea idea_id=%s context=%s reason=no_evidence",
                idea_id,
                context,
            )
    if dropped:
        logger.warning(
            "evidence_gate context=%s kept=%d dropped=%d dropped_ids=%s",
            context,
            len(kept),
            len(dropped),
            [item["idea_id"] for item in dropped],
        )
    return {"kept": kept, "dropped": dropped, "details": details}


async def enforce_manual_idea(session: AsyncSession, idea_id: int) -> dict[str, Any]:
    """手动 idea 的证据状态（手动创建允许先无证据，但必须显式暴露待绑定态）。"""
    ids = await idea_evidence_ids(session, int(idea_id))
    return {
        "idea_id": int(idea_id),
        "evidence_ids": ids,
        "evidence_count": len(ids),
        "has_evidence": bool(ids),
        "needs_evidence": not ids,
        "message": (
            None
            if ids
            else "手动 idea 尚无 Evidence：可进入可行性流程，但产出前必须绑定至少 1 条证据"
        ),
    }


async def clear_idea_evidence(session: AsyncSession, idea_id: int) -> int:
    """清空该 idea 的证据（重跑前调用；返回删除行数）。"""
    from services.evidence import clear_evidence  # 局部导入，避免循环依赖

    return await clear_evidence(IDEA_OWNER_TYPE, int(idea_id), session=session)


__all__ = [
    "IDEA_OWNER_TYPE",
    "REQUIRED_REFS",
    "InvalidOwnerTypeError",
    "bind_idea_evidence",
    "clear_idea_evidence",
    "dedupe_candidates",
    "enforce_evidence_or_drop",
    "enforce_manual_idea",
    "gap_candidates",
    "idea_evidence_ids",
    "normalize_candidates",
]
