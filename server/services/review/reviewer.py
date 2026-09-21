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
"""评审环节（WP14-T4/T5，附录 D.6）。

输入：草稿（``paper_drafts`` + ``draft_claims``）+ 实验指标（``experiment_metrics``）+
Passport 摘要（``experiment_passports``）+ 上游 plan_review 分项。
输出：``{novelty, rigor, completeness, reproducibility, total, comments[{dimension,issue,suggestion}]}``。

口径（**服务端权威，模型不可覆盖**）
------------------------------------
- 四维各 0–25，``total`` 由服务端重算 = 四维之和（模型自报的 total 会被校验并忽略）。
- 四维分数来自 :mod:`services.review.scorer` 的可复现 rubric（分项明细落
  ``review_scores.comments.rubric``，可审计复算）；LLM 只提供**评语**（``comments``）与
  **建议分**（``llm_suggested_dims``，单独留痕、不参与 total），避免不可复现的分数抖动。
- 停止条件不在这里决定：本环节把真实分数交给 WP09 ``stop_conditions`` 与 WP10 的 D5 决策，
  ``stop_reason`` 由引擎落 ``pipeline_runs.stop_reason``（可查询）。
"""

from __future__ import annotations

import logging
import re
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass, field
from typing import Any

from services.review import scorer
from services.writing.drafter import scan_forbidden

logger = logging.getLogger("sciloop.wp14.reviewer")

WP_ID = "WP14"
PROMPT_VERSION = "review-v1"

MAX_COMMENTS = 12

REVIEW_SCHEMA: dict[str, Any] = {
    "type": "object",
    "required": ["comments"],
    "properties": {
        "comments": {
            "type": "array",
            "minItems": 1,
            "maxItems": 8,
            "items": {
                "type": "object",
                "required": ["dimension", "issue", "suggestion"],
                "properties": {
                    "dimension": {
                        "type": "string",
                        "enum": ["novelty", "rigor", "completeness", "reproducibility"],
                    },
                    "issue": {"type": "string", "minLength": 4, "maxLength": 300},
                    "suggestion": {"type": "string", "minLength": 4, "maxLength": 300},
                },
            },
        },
        "llm_suggested_dims": {
            "type": "object",
            "properties": {dimension: {"type": "number", "minimum": 0, "maximum": scorer.DIM_MAX} for dimension in scorer.DIMENSIONS},
        },
    },
}

_CITATION_STRIP_RE = re.compile(r"\[\d{1,3}\]")


@dataclass(slots=True)
class ReviewInputs:
    """评审输入摘要（全部来自真实落库数据；取不到即为 None 并在 rubric 中归一）。"""

    draft_id: int | None = None
    iteration: int = 1
    title: str | None = None
    claim_coverage: float | None = None
    claim_counts: dict[str, int] = field(default_factory=dict)
    bound_evidence_count: int = 0
    section_count: int = 0
    evidence_pool_size: int = 0
    distinct_span_papers: int = 0
    gap_count: int | None = None
    metric_count: int = 0
    metrics: list[dict[str, Any]] = field(default_factory=list)
    passport: dict[str, Any] | None = None
    plan_review: dict[str, Any] | None = None
    unsupported_count: int = 0
    previous_total: float | None = None

    def to_dict(self) -> dict[str, Any]:
        payload = {
            "draft_id": self.draft_id,
            "iteration": self.iteration,
            "title": self.title,
            "claim_coverage": self.claim_coverage,
            "claim_counts": dict(self.claim_counts),
            "bound_evidence_count": self.bound_evidence_count,
            "section_count": self.section_count,
            "evidence_pool_size": self.evidence_pool_size,
            "distinct_span_papers": self.distinct_span_papers,
            "gap_count": self.gap_count,
            "metric_count": self.metric_count,
            "metrics": [dict(item) for item in self.metrics],
            "passport": dict(self.passport) if self.passport else None,
            "plan_review": dict(self.plan_review) if self.plan_review else None,
            "unsupported_count": self.unsupported_count,
            "previous_total": self.previous_total,
        }
        return payload

    def scoring_inputs(self) -> dict[str, Any]:
        """给 scorer 的输入（剔除不适合进 prompt 的细节）。"""
        return {
            "claim_coverage": self.claim_coverage,
            "claim_counts": dict(self.claim_counts),
            "bound_evidence_count": self.bound_evidence_count,
            "section_count": self.section_count,
            "evidence_pool_size": self.evidence_pool_size,
            "distinct_span_papers": self.distinct_span_papers,
            "gap_count": self.gap_count,
            "metric_count": self.metric_count,
            "passport": dict(self.passport) if self.passport else None,
            "plan_review": dict(self.plan_review) if self.plan_review else None,
        }


def build_review_messages(inputs: ReviewInputs, *, draft_excerpt: str) -> list[dict[str, Any]]:
    """D.6 评审 prompt：只给真实数据，禁止模型输出分数以外的事实。"""
    system = (
        "你是科研辅助系统的「论文评审」模块。你的输出将作为结构化数据被程序消费。\n"
        "必须严格遵守：1) 只输出合法 JSON，不要任何解释性文字；2) 评语必须引用给定的真实"
        "数据（覆盖率、指标、Passport 字段等），不确定时写 unknown 而不是编造；"
        "3) 不得输出任何面向外部出版渠道的表述。"
    )
    user = {
        "任务": "对以下草稿摘录与实验证据给出分维评语（issue + suggestion）",
        "草稿摘录": draft_excerpt[:4000],
        "claim 统计": inputs.claim_counts,
        "claim_coverage": inputs.claim_coverage,
        "证据池": {
            "条目数": inputs.evidence_pool_size,
            "已绑定证据数": inputs.bound_evidence_count,
            "可引用原文片段论文数": inputs.distinct_span_papers,
            "未支撑段落数": inputs.unsupported_count,
        },
        "实验指标": inputs.metrics[:12],
        "Passport 摘要": inputs.passport,
        "上游盲评摘要": inputs.plan_review,
        "四维定义": "novelty / rigor / completeness / reproducibility（各 0–25，总分由服务端重算）",
        "输出格式": {
            "comments": [{"dimension": "rigor", "issue": "问题", "suggestion": "改进建议"}],
            "llm_suggested_dims": {"novelty": 0, "rigor": 0, "completeness": 0, "reproducibility": 0},
        },
    }
    return [
        {"role": "system", "content": system},
        {"role": "user", "content": _json(user)},
    ]


def collect_llm_comments(raw: Mapping[str, Any] | None) -> tuple[list[dict[str, str]], dict[str, float], list[str]]:
    """校验模型评语与建议分；不合规条目丢弃并留痕（禁止把违规文本落库）。"""
    payload = dict(raw or {})
    issues: list[str] = []
    comments: list[dict[str, str]] = []
    raw_comments = payload.get("comments")
    if not isinstance(raw_comments, list):
        issues.append("comments 不是数组")
        raw_comments = []
    for position, item in enumerate(raw_comments[:MAX_COMMENTS]):
        if not isinstance(item, Mapping):
            issues.append(f"comments[{position}] 不是对象")
            continue
        dimension = str(item.get("dimension") or "").strip()
        issue_text = " ".join(str(item.get("issue") or "").split())
        suggestion = " ".join(str(item.get("suggestion") or "").split())
        if dimension not in scorer.DIMENSIONS:
            issues.append(f"comments[{position}].dimension='{dimension}' 不在四维内")
            continue
        if len(issue_text) < 4 or len(suggestion) < 4:
            issues.append(f"comments[{position}] 的 issue/suggestion 过短")
            continue
        hits = scan_forbidden(issue_text) + scan_forbidden(suggestion)
        if hits:
            issues.append(f"comments[{position}] 命中合规词表 {hits}：已丢弃")
            continue
        comments.append({"dimension": dimension, "issue": issue_text, "suggestion": suggestion})

    suggested: dict[str, float] = {}
    raw_suggested = payload.get("llm_suggested_dims")
    if isinstance(raw_suggested, Mapping):
        for dimension in scorer.DIMENSIONS:
            value = raw_suggested.get(dimension)
            if isinstance(value, (int, float)) and not isinstance(value, bool) and 0 <= float(value) <= scorer.DIM_MAX:
                suggested[dimension] = round(float(value), 2)
            elif value is not None:
                issues.append(f"llm_suggested_dims.{dimension}={value!r} 越界或非数值：已忽略")
    return comments, suggested, issues


async def review(
    inputs: ReviewInputs,
    *,
    draft_excerpt: str = "",
    llm_call: Callable[[list[dict[str, Any]]], Awaitable[Any]] | None = None,
    llm_model_ref: str | None = None,
) -> dict[str, Any]:
    """执行评审：rubric 打分（权威）+ LLM 评语（可缺省）。"""
    scores = scorer.compute_scores(inputs.scoring_inputs())
    dimensions = {dimension: float(scores[dimension]) for dimension in scorer.DIMENSIONS}
    total = float(scores["total"])

    comments = scorer.rule_based_comments(inputs.scoring_inputs(), dimensions)
    comments_source = "rule_based"
    llm_info: dict[str, Any] = {
        "attempted": llm_call is not None,
        "used": False,
        "call_log_id": None,
        "model_ref": llm_model_ref,
        "prompt_version": PROMPT_VERSION,
        "is_stub": False,
        "issues": [],
        "suggested_dims": {},
    }

    if llm_call is not None:
        try:
            result = await llm_call(build_review_messages(inputs, draft_excerpt=draft_excerpt))
            raw = _parsed_payload(result)
            llm_comments, suggested, issues = collect_llm_comments(raw)
            llm_info.update(
                {
                    "used": bool(llm_comments),
                    "call_log_id": getattr(result, "call_log_id", None),
                    "model_ref": getattr(result, "model_ref", None) or llm_model_ref,
                    "is_stub": bool(getattr(result, "is_stub", False)),
                    "issues": issues,
                    "suggested_dims": suggested,
                }
            )
            if llm_comments:
                comments = llm_comments + comments[: max(0, MAX_COMMENTS - len(llm_comments))]
                comments_source = "llm+rule_based"
        except Exception as exc:  # noqa: BLE001 - LLM 不可用不得阻断评审（如实降级）
            logger.warning("评审 LLM 调用失败，降级为规则评语：%s: %s", type(exc).__name__, exc)
            llm_info["issues"] = [f"llm_error: {type(exc).__name__}: {exc}"]

    # 模型建议分只留痕，不进入 total（可复现性优先；评审分抖动会让停止条件不可复现）
    suggested = llm_info.get("suggested_dims") or {}
    if suggested:
        for dimension, value in suggested.items():
            scores["rubric"][f"llm_suggested_{dimension}"] = value
        scores["rubric"]["llm_suggested_note"] = (
            "模型建议分单独留痕，不参与 total：评分口径为可复现 rubric，"
            "避免同轮次重复运行得到不同 stop_reason"
        )

    payload = {
        "novelty": dimensions["novelty"],
        "rigor": dimensions["rigor"],
        "completeness": dimensions["completeness"],
        "reproducibility": dimensions["reproducibility"],
        "total": total,
        "scores": dict(dimensions),
        "comments": comments,
        "comments_source": comments_source,
        "rubric_version": scorer.RUBRIC_VERSION,
        "rubric": scores["rubric"],
        "score_coverage": scores["score_coverage"],
        "llm": llm_info,
        "inputs": inputs.to_dict(),
        "iteration": inputs.iteration,
        "draft_id": inputs.draft_id,
        "disclaimer": "本内容由 AI 辅助生成，需研究者自行核验",
    }
    _assert_total(payload)
    logger.info(
        "review total=%s dims=%s comments=%d source=%s",
        total,
        dimensions,
        len(comments),
        comments_source,
    )
    return payload


def _assert_total(payload: Mapping[str, Any]) -> None:
    """硬约束：四维各 0–25 且 total = 四维之和。"""
    dims = [float(payload[dimension]) for dimension in scorer.DIMENSIONS]
    for dimension, value in zip(scorer.DIMENSIONS, dims, strict=True):
        if value < 0 or value > scorer.DIM_MAX:
            raise ValueError(f"{dimension}={value} 超出 0–{scorer.DIM_MAX:.0f}")
    expected = round(sum(dims), 2)
    if abs(float(payload["total"]) - expected) > 1e-6:
        raise ValueError(f"total={payload['total']} 不等于四维之和 {expected}")


def _parsed_payload(result: Any) -> Mapping[str, Any]:
    for attribute in ("parsed", "json", "data"):
        value = getattr(result, attribute, None)
        if isinstance(value, Mapping):
            return value
    content = getattr(result, "content", None)
    if isinstance(content, Mapping):
        return content
    if isinstance(content, str) and content.strip():
        import json

        try:
            decoded = json.loads(content)
        except json.JSONDecodeError:
            return {}
        return decoded if isinstance(decoded, Mapping) else {}
    return {}


async def persist_review_score(session: Any, *, pipeline_run_id: int, result: Mapping[str, Any]) -> int:
    """写 ``review_scores``（主观量化量分项落库：四维 + total + comments 明细）。"""
    from sqlalchemy import text as sql_text

    comments_payload = {
        "items": list(result.get("comments") or []),
        "source": result.get("comments_source"),
        "rubric": result.get("rubric"),
        "rubric_version": result.get("rubric_version"),
        "score_coverage": result.get("score_coverage"),
        "llm": result.get("llm"),
        "inputs": result.get("inputs"),
        "iteration": result.get("iteration"),
        "draft_id": result.get("draft_id"),
        "disclaimer": result.get("disclaimer"),
        "total_rule": "total = novelty + rigor + completeness + reproducibility",
    }
    row = await session.execute(
        sql_text(
            """
            INSERT INTO review_scores (
                pipeline_run_id, novelty, rigor, completeness, reproducibility, total, comments
            ) VALUES (
                :pipeline_run_id, :novelty, :rigor, :completeness, :reproducibility, :total,
                CAST(:comments AS JSONB)
            ) RETURNING id
            """
        ),
        {
            "pipeline_run_id": int(pipeline_run_id),
            "novelty": float(result["novelty"]),
            "rigor": float(result["rigor"]),
            "completeness": float(result["completeness"]),
            "reproducibility": float(result["reproducibility"]),
            "total": float(result["total"]),
            "comments": _json(comments_payload),
        },
    )
    return int(row.scalar_one())


async def previous_review_total(session: Any, *, project_id: int, iteration: int) -> float | None:
    """上一轮 review 总分（真实值；无上一轮则 ``None``，禁止编造）。"""
    if iteration <= 1:
        return None
    from sqlalchemy import text as sql_text

    rows = (
        await session.execute(
            sql_text(
                """
                SELECT rs.total
                FROM review_scores rs
                JOIN pipeline_runs pr ON pr.id = rs.pipeline_run_id
                WHERE pr.project_id = :project_id AND pr.iteration = :iteration
                ORDER BY rs.id DESC
                LIMIT 1
                """
            ),
            {"project_id": int(project_id), "iteration": int(iteration - 1)},
        )
    ).all()
    if not rows or rows[0][0] is None:
        return None
    return float(rows[0][0])


async def latest_review_score(session: Any, *, pipeline_run_id: int) -> dict[str, Any] | None:
    """本 run 最近一次评审落库结果（供 API/工作台读取）。"""
    from sqlalchemy import text as sql_text

    rows = (
        await session.execute(
            sql_text(
                """
                SELECT id, novelty, rigor, completeness, reproducibility, total, comments, created_at
                FROM review_scores
                WHERE pipeline_run_id = :pipeline_run_id
                ORDER BY id DESC
                LIMIT 1
                """
            ),
            {"pipeline_run_id": int(pipeline_run_id)},
        )
    ).mappings().all()
    if not rows:
        return None
    row = rows[0]
    return {
        "review_score_id": int(row["id"]),
        "pipeline_run_id": int(pipeline_run_id),
        "novelty": float(row["novelty"]) if row["novelty"] is not None else None,
        "rigor": float(row["rigor"]) if row["rigor"] is not None else None,
        "completeness": float(row["completeness"]) if row["completeness"] is not None else None,
        "reproducibility": float(row["reproducibility"]) if row["reproducibility"] is not None else None,
        "total": float(row["total"]) if row["total"] is not None else None,
        "comments": row["comments"],
    }


def draft_excerpt_of(content_md: str | None, *, limit: int = 4000) -> str:
    """喂给评审模型的最小草稿摘录（去引用标记，避免模型被编号误导）。"""
    text_value = _CITATION_STRIP_RE.sub("", str(content_md or ""))
    return " ".join(text_value.split())[:limit]


def _json(payload: Any) -> str:
    import json

    return json.dumps(payload, ensure_ascii=False, default=str)


__all__ = [
    "MAX_COMMENTS",
    "PROMPT_VERSION",
    "REVIEW_SCHEMA",
    "ReviewInputs",
    "build_review_messages",
    "collect_llm_comments",
    "draft_excerpt_of",
    "latest_review_score",
    "persist_review_score",
    "previous_review_total",
    "review",
]
