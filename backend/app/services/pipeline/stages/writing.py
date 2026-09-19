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
"""``writing`` 环节：大纲决策（D6）→ 分节撰写 → Claim 校验与草稿落库（WP14-T1/T2/T3，附录 D.5）。

环节契约
--------

======== ==================================================================
输入      任务书 + 上游环节产出摘要（survey / plan / plan_review / experiment）
          + 证据池（WP13 绑定的真实证据：原文片段 / 实验指标 / Passport / 决策留痕）
输出      ``{outline[], sections:[{heading, claims:[{claim_id, text,
          evidence_ids[], support_status}]}], draft_id, claim_coverage,
          counts{}, unsupported[], citations{}}``
决策      D6 写作结构（动作域 ``{outline[], section_focus{}}``）
人工节点  N4（大纲产出后；``mode=manual`` 由引擎在本环节 done 后暂停）
落库      ``paper_drafts``（content_md + claim_coverage）+ ``draft_claims``（三态）
======== ==================================================================

红线（WP14.hard_constraints / contracts.evidence_rules）
------------------------------------------------------
- 每个事实性句子在生成时**同步产出** ``evidence_refs``；引用编号由服务端按证据池分配，
  池外 ref 一律丢弃并留痕（**禁止编造引用编号**）
- 无证据段落一律判 ``insufficient``（由 :mod:`app.services.writing.integrity_checker`
  统一判定），**不得隐藏**
- 草稿与 prompt 均不含任何指向外部出版渠道的表述（``drafter.FORBIDDEN_PUBLISH_PHRASES``）
- LLM 不可用时用本地桩（``is_stub=true`` 且写入 ``degradations``），桩只转述证据池原文，
  未引入任何新事实
"""

from __future__ import annotations

import json
import logging
import re
from collections.abc import Mapping, Sequence
from typing import Any

from app.services.pipeline.stages.base import (
    StageContext,
    StageResult,
    call_llm,
    result_cost_of,
    unknown_cost_notes,
)
from app.services.writing import drafter, integrity_checker

logger = logging.getLogger("sciloop.pipeline.stage.writing")

WP_ID = "WP14"

#: 章节数上限（成本护栏：写作是 token 压力最大的环节，WP14.risks）
MAX_SECTIONS = 6
#: 每节 Claim 数上限
MAX_CLAIMS_PER_SECTION = 6

#: 可进入 prompt 的上游字段白名单（避免把整份上游产出灌进 prompt）
UPSTREAM_FIELDS: dict[str, tuple[str, ...]] = {
    "survey": ("queries", "fields", "paper_limit", "paper_count", "papers", "notes"),
    "plan": ("methods", "rationale", "notes"),
    "plan_review": ("verdict", "selected_method_index", "selected_method", "scores", "concerns"),
    "experiment": ("template_id", "runs", "metrics", "passport", "status", "notes"),
}

#: 单阶段上游摘要的字符上限
UPSTREAM_LIMIT = 1200

_HEADING_NUMBER_RE = re.compile(r"^\s*\d{1,2}\s*[.、)．]\s*")


def _payload_of(result: Any) -> dict[str, Any]:
    """从 LLMResult 取结构化 JSON（``parsed`` 优先，其次解析 ``content``）。"""
    for attribute in ("parsed", "json", "data"):
        value = getattr(result, attribute, None)
        if isinstance(value, Mapping):
            return dict(value)
    content = getattr(result, "content", None)
    if isinstance(content, Mapping):
        return dict(content)
    if isinstance(content, str) and content.strip():
        try:
            decoded = json.loads(content)
        except json.JSONDecodeError:
            return {}
        return dict(decoded) if isinstance(decoded, Mapping) else {}
    return {}


def _short(value: Any, limit: int = 400) -> Any:
    if isinstance(value, str):
        return value[:limit]
    if isinstance(value, Mapping):
        return {str(key): _short(item, limit) for key, item in list(value.items())[:12]}
    if isinstance(value, Sequence) and not isinstance(value, (str, bytes)):
        return [_short(item, limit) for item in list(value)[:8]]
    if isinstance(value, (int, float, bool)) or value is None:
        return value
    return str(value)[:limit]


def _upstream_digest(ctx: StageContext) -> dict[str, Any]:
    """上游产出摘要（白名单字段 + 截断，不含隐式信息）。"""
    digest: dict[str, Any] = {}
    for stage, fields in UPSTREAM_FIELDS.items():
        payload = ctx.upstream(stage)
        if not payload:
            continue
        picked = {name: _short(payload.get(name)) for name in fields if payload.get(name) not in (None, "", [], {})}
        if picked:
            digest[stage] = picked
    return digest


def _llm_unavailable_reason(settings: Any) -> str | None:
    """LLM 是否可用；不可用时给出**如实**原因（不用桩冒充实时结果）。"""
    if settings is None:
        return "settings_unavailable"
    if bool(getattr(settings, "llm_replay", False)):
        return None  # 回放模式：由适配层按 fixture 回答，is_replay 在 llm_call_logs 标记
    if str(getattr(settings, "llm_default_api_key", "") or "").strip():
        return None
    if str(getattr(settings, "llm_fallback_api_key", "") or "").strip():
        return None
    return "LLM_DEFAULT_API_KEY 未配置（无可用模型凭据）"


def _settings_of(ctx: StageContext) -> Any:
    settings = (ctx.extras or {}).get("settings")
    if settings is not None:
        return settings
    try:
        from app.core.config import get_settings

        return get_settings()
    except Exception as exc:  # noqa: BLE001 - 配置不可用不得阻断环节
        logger.warning("读取配置失败：%s", exc)
        return None


async def _call_json(
    ctx: StageContext,
    messages: list[dict[str, Any]],
    *,
    json_schema: dict[str, Any],
    purpose: str,
    costs: list[Any],
) -> dict[str, Any]:
    """经 ``base.call_llm`` 调用（保证写 ``llm_call_logs``）并返回结构化 payload。"""
    result = await call_llm(
        ctx,
        messages,
        json_schema=json_schema,
        purpose=purpose,
        temperature=0.2,
    )
    costs.append(result)
    return _payload_of(result)


# --------------------------------------------------------------------------- #
# 大纲 / 分节
# --------------------------------------------------------------------------- #
async def _build_outline(
    ctx: StageContext,
    *,
    taskbook: Mapping[str, Any],
    pool: drafter.EvidencePool,
    upstream: Mapping[str, Any],
    skip_reason: str | None,
    costs: list[Any],
) -> tuple[dict[str, Any], list[str]]:
    """产出大纲：LLM 优先；不可用或校验失败则回落本地桩（如实记录原因）。"""
    problems: list[str] = []
    if skip_reason is None:
        try:
            raw = await _call_json(
                ctx,
                drafter.build_outline_messages(
                    taskbook=taskbook, pool=pool, upstream=upstream, section_hint=MAX_SECTIONS
                ),
                json_schema=drafter.OUTLINE_SCHEMA,
                purpose="writing.outline",
                costs=costs,
            )
            outline = drafter.normalize_outline(raw, max_sections=MAX_SECTIONS)
            return outline, problems
        except Exception as exc:  # noqa: BLE001 - 失败必须可见并降级，不得阻断写作
            problems.append(
                f"大纲 LLM 调用/校验失败（{type(exc).__name__}: {exc}），改用本地桩骨架"
            )
    else:
        problems.append(f"LLM 不可用（{skip_reason}），大纲改用本地桩骨架")
    stub = drafter.stub_outline(max_sections=MAX_SECTIONS, reasons=problems)
    return stub, problems


async def _build_section_claims(
    ctx: StageContext,
    *,
    heading: str,
    purpose: str,
    taskbook: Mapping[str, Any],
    pool: drafter.EvidencePool,
    upstream: Mapping[str, Any],
    skip_reason: str | None,
    costs: list[Any],
) -> tuple[list[dict[str, Any]], list[str]]:
    """产出单节 Claim：LLM 优先；失败回落本地桩（只转述证据池原文）。"""
    problems: list[str] = []
    if skip_reason is None:
        try:
            raw = await _call_json(
                ctx,
                drafter.build_section_messages(
                    heading=heading,
                    purpose=purpose,
                    taskbook=taskbook,
                    pool=pool,
                    upstream=upstream,
                    max_claims=MAX_CLAIMS_PER_SECTION,
                ),
                json_schema=drafter.SECTION_SCHEMA,
                purpose=f"writing.section.{heading[:24]}",
                costs=costs,
            )
            return (
                drafter.normalize_sections(
                    raw,
                    heading=heading,
                    purpose=purpose,
                    pool=pool,
                    max_claims=MAX_CLAIMS_PER_SECTION,
                ),
                problems,
            )
        except Exception as exc:  # noqa: BLE001 - 单节失败不得让整篇草稿丢失
            problems.append(f"章节「{heading}」LLM 失败（{type(exc).__name__}），改用本地桩转述证据池")
    else:
        problems.append(f"章节「{heading}」使用本地桩（{skip_reason}）")
    return (
        drafter.stub_sections(
            heading=heading, pool=pool, taskbook=taskbook, max_claims=MAX_CLAIMS_PER_SECTION
        ),
        problems,
    )


# --------------------------------------------------------------------------- #
# D6 写作结构决策
# --------------------------------------------------------------------------- #
async def _request_d6(
    ctx: StageContext,
    *,
    outline: Sequence[Mapping[str, Any]],
    pool: drafter.EvidencePool,
    section_count: int,
) -> dict[str, Any]:
    """把**真实大纲**交给 WP10 的 D6 写作结构决策（规则层拥有最终决定权）。

    ``persist=True``：本环节留痕的动作域是真实 ``outline``/``section_focus``；
    引擎在环节前的门禁另有一条 D6 记录（动作域为空，WP09 行为），两者互补、都可查。
    """
    try:
        from app.services.pipeline.decision_engine import evaluate_decision
    except Exception as exc:  # noqa: BLE001 - WP10 未挂载属正常并行期状态
        return {"available": False, "reason": f"policy_not_mounted:{type(exc).__name__}"}

    section_focus = {
        str(item.get("heading")): str(item.get("focus") or item.get("purpose") or "")
        for item in outline
    }
    context = {
        "stage": "writing",
        "decision_point": "D6",
        "attempt": ctx.attempt,
        "iteration": int(ctx.iteration or 1),
        "mode": ctx.mode,
        "pipeline_run_id": ctx.pipeline_run_id,
        "taskbook_id": getattr(ctx.taskbook, "id", None),
        "action_domain": {
            "outline": [dict(item) for item in outline],
            "section_focus": section_focus,
        },
        "options_considered": ["keep_outline", "revise_outline"],
        "evidence_pool_size": len(pool),
        "section_count": int(section_count),
    }
    try:
        outcome = await evaluate_decision(ctx.project_id, "D6", context, persist=True)
    except Exception as exc:  # noqa: BLE001 - 策略层异常不得让写作环节失败
        logger.warning("D6 策略调用失败（不影响草稿产出）：%s", exc, exc_info=True)
        return {"available": False, "reason": f"policy_call_failed:{type(exc).__name__}: {exc}"}

    if not isinstance(outcome, Mapping):
        return {"available": False, "reason": "policy_returned_non_mapping"}
    return {
        "available": True,
        "decision_point": "D6",
        "policy_action": outcome.get("policy_action"),
        "chosen": outcome.get("chosen"),
        "risk_score": outcome.get("risk_score"),
        "confidence_score": outcome.get("confidence_score"),
        "reversibility_score": outcome.get("reversibility_score"),
        "rationale": outcome.get("rationale"),
        "policy_version": outcome.get("policy_version"),
        "decision_log_id": outcome.get("decision_log_id"),
        "action_specified": bool(section_focus) and bool(outline),
        "note": (
            "本结论基于真实大纲（action_domain=outline/section_focus）；"
            "引擎在环节前门禁另有一条 D6 留痕（动作域为空）"
        ),
    }


# --------------------------------------------------------------------------- #
# 落库
# --------------------------------------------------------------------------- #
async def _upsert_draft(ctx: StageContext, *, content_md: str) -> int:
    """按 (pipeline_run_id, iteration) 幂等写 ``paper_drafts``（重试不产生重复草稿）。"""
    from sqlalchemy import text as sql_text

    session = ctx.session
    existing = (
        await session.execute(
            sql_text(
                """
                SELECT id FROM paper_drafts
                WHERE pipeline_run_id = :rid AND iteration = :iteration
                ORDER BY id DESC LIMIT 1
                """
            ),
            {"rid": int(ctx.pipeline_run_id), "iteration": int(ctx.iteration or 1)},
        )
    ).scalar_one_or_none()
    if existing is not None:
        draft_id = int(existing)
        await session.execute(
            sql_text("DELETE FROM draft_claims WHERE draft_id = :draft_id"), {"draft_id": draft_id}
        )
        await session.execute(
            sql_text(
                "UPDATE paper_drafts SET content_md = :content_md, claim_coverage = NULL WHERE id = :draft_id"
            ),
            {"content_md": content_md, "draft_id": draft_id},
        )
        return draft_id

    row = await session.execute(
        sql_text(
            """
            INSERT INTO paper_drafts (project_id, pipeline_run_id, iteration, content_md, claim_coverage)
            VALUES (:project_id, :pipeline_run_id, :iteration, :content_md, NULL)
            RETURNING id
            """
        ),
        {
            "project_id": int(ctx.project_id),
            "pipeline_run_id": int(ctx.pipeline_run_id),
            "iteration": int(ctx.iteration or 1),
            "content_md": content_md,
        },
    )
    return int(row.scalar_one())


def _normalize_heading(value: Any) -> str:
    return _HEADING_NUMBER_RE.sub("", str(value or "").strip()).strip()


def _sections_output(
    outline: Sequence[Mapping[str, Any]], claims: Sequence[Mapping[str, Any]]
) -> list[dict[str, Any]]:
    """把 Claim 明细按大纲顺序归组（Claim 的 section_heading 来自渲染后的 Markdown 标题）。"""
    buckets: dict[str, list[dict[str, Any]]] = {}
    leftovers: list[dict[str, Any]] = []
    for claim in claims:
        key = _normalize_heading(claim.get("section_heading"))
        if key:
            buckets.setdefault(key, []).append(dict(claim))
        else:
            leftovers.append(dict(claim))

    sections: list[dict[str, Any]] = []
    for item in outline:
        heading = str(item.get("heading") or "")
        grouped = buckets.pop(_normalize_heading(heading), [])
        if not grouped and leftovers:
            grouped = [leftovers.pop(0)]
        sections.append(
            {
                "heading": heading,
                "purpose": item.get("purpose"),
                "focus": item.get("focus"),
                "claims": [
                    {
                        "claim_id": claim.get("claim_id"),
                        "text": claim.get("claim_text"),
                        "evidence_ids": list(claim.get("evidence_ids") or []),
                        "support_status": claim.get("support_status"),
                        "status_reason": claim.get("status_reason"),
                        "is_factual": bool(claim.get("is_factual")),
                        "index": claim.get("index"),
                        "char_start": claim.get("char_start"),
                        "char_end": claim.get("char_end"),
                        "dropped_refs": list(claim.get("dropped_refs") or []),
                    }
                    for claim in grouped
                ],
            }
        )
    # 未归入大纲的 Claim（理论上不会出现）单独成节，禁止丢失
    for key, grouped in buckets.items():
        sections.append(
            {
                "heading": key or "（未归节）",
                "purpose": None,
                "focus": None,
                "claims": [
                    {
                        "claim_id": claim.get("claim_id"),
                        "text": claim.get("claim_text"),
                        "evidence_ids": list(claim.get("evidence_ids") or []),
                        "support_status": claim.get("support_status"),
                        "status_reason": claim.get("status_reason"),
                        "is_factual": bool(claim.get("is_factual")),
                        "index": claim.get("index"),
                        "char_start": claim.get("char_start"),
                        "char_end": claim.get("char_end"),
                        "dropped_refs": list(claim.get("dropped_refs") or []),
                    }
                    for claim in grouped
                ],
            }
        )
    return sections


def _title_of(taskbook: Mapping[str, Any], project_id: int) -> str:
    question = " ".join(str(taskbook.get("research_question") or "").split())
    if question:
        return question[:96]
    return f"项目 {project_id} 研究草稿"


# --------------------------------------------------------------------------- #
# 环节实现
# --------------------------------------------------------------------------- #
async def run(ctx: StageContext) -> StageResult:
    """执行写作环节：大纲（D6）→ 分节撰写 → Claim 三态校验 → 草稿落库。"""
    if ctx.session is None:
        raise RuntimeError("writing 环节缺少数据库会话（engine 必须注入 ctx.session）")

    notes: list[str] = []
    degradations: list[str] = []
    costs: list[Any] = []

    await ctx.progress(5, "加载任务书、上游产出与证据池")
    taskbook = ctx.taskbook_payload
    pool = await drafter.build_evidence_pool(ctx.session, ctx.project_id)
    upstream = _upstream_digest(ctx)
    if len(pool) == 0:
        degradations.append(
            "证据池为空（无已解析全文/指标/决策留痕）：本稿将大面积 insufficient，已如实标注、不隐藏"
        )
    else:
        notes.append(f"证据池 {len(pool)} 条：{json.dumps(pool.summary(), ensure_ascii=False)}")

    settings = _settings_of(ctx)
    skip_reason = _llm_unavailable_reason(settings)
    if skip_reason:
        degradations.append(f"{skip_reason}：写作使用本地桩（is_stub=true，只转述证据池原文）")

    # 1) 大纲
    await ctx.progress(15, "生成大纲")
    outline_payload, outline_problems = await _build_outline(
        ctx, taskbook=taskbook, pool=pool, upstream=upstream, skip_reason=skip_reason, costs=costs
    )
    outline = [dict(item) for item in outline_payload.get("outline") or []]
    degradations.extend(outline_problems)
    if not outline:
        raise RuntimeError("大纲产出为空：写作环节无法继续（禁止产出无结构草稿）")
    is_stub = bool(outline_payload.get("is_stub")) or skip_reason is not None

    # 2) D6 写作结构决策（真实动作域）
    await ctx.progress(30, "D6 写作结构决策")
    d6 = await _request_d6(ctx, outline=outline, pool=pool, section_count=len(outline))
    if d6.get("available"):
        notes.append(
            f"D6 决策：policy_action={d6.get('policy_action')} chosen={d6.get('chosen')} "
            f"decision_log_id={d6.get('decision_log_id')}"
        )
    else:
        degradations.append(f"D6 策略层不可用（{d6.get('reason')}）：大纲按默认结构继续，未编造策略结论")
    human_gate_required = str(d6.get("policy_action") or "") == "need_human"
    if human_gate_required:
        notes.append("D6 判定 need_human：等待人工在 N4 介入（approve / modify / reject / rerun）")

    # 3) 分节撰写
    section_drafts: list[drafter.SectionDraft] = []
    for position, item in enumerate(outline, start=1):
        heading = str(item.get("heading") or f"第 {position} 节")
        purpose = str(item.get("purpose") or "")
        await ctx.progress(30 + int(40 * position / max(1, len(outline))), f"撰写章节：{heading[:20]}")
        claims, problems = await _build_section_claims(
            ctx,
            heading=heading,
            purpose=purpose,
            taskbook=taskbook,
            pool=pool,
            upstream=upstream,
            skip_reason=skip_reason,
            costs=costs,
        )
        degradations.extend(problems)
        section_drafts.append(
            drafter.SectionDraft(
                heading=heading,
                purpose=purpose,
                claims=[
                    drafter.ClaimDraft(
                        local_id=f"s{position}c{index + 1}",
                        text=str(claim.get("text") or ""),
                        evidence_refs=list(claim.get("evidence_refs") or []),
                        dropped_refs=list(claim.get("dropped_refs") or []),
                    )
                    for index, claim in enumerate(claims)
                ],
            )
        )

    # 4) 渲染 Markdown（引用编号由服务端按首次出现顺序分配）
    await ctx.progress(75, "渲染草稿并分配引用编号")
    document = drafter.DraftDocument(
        title=_title_of(taskbook, ctx.project_id),
        outline=list(outline),
        sections=section_drafts,
        extra_notes=[
            "生成方式：LLM 分节撰写 + 服务端引用编号分配" if not is_stub else "生成方式：本地桩（见 degradations）",
            "所有事实性句子必须挂载证据；无证据段落已被标记为 insufficient，未做隐藏",
            "本文档仅用于研究者内部核验与技术审计留痕",
        ],
        is_stub=is_stub,
        stub_reasons=list(degradations),
    )
    content_md = document.render_markdown(number=1)
    dropped_refs = list(document.dropped_refs)
    if dropped_refs:
        degradations.append(
            f"模型给出的 {len(dropped_refs)} 个引用键不在证据池内，已丢弃（禁止编造引用）："
            + ", ".join(sorted(set(dropped_refs))[:10])
        )
    forbidden_hits = drafter.scan_forbidden(content_md)
    if forbidden_hits:
        # render_markdown 已替换违规表述；此处再次扫描留痕（合规红线必须可审计）
        degradations.append(f"合规扫描命中并已替换：{sorted(set(forbidden_hits))}")

    # 5) 草稿落库 + Claim 三态校验
    await ctx.progress(85, "落库草稿并校验 Claim 证据")
    draft_id = await _upsert_draft(ctx, content_md=content_md)
    report = await integrity_checker.verify_and_persist(
        ctx.session, draft_id=draft_id, content_md=content_md, pool=pool, commit=True
    )
    counts = dict(report.get("counts") or {})
    coverage = report.get("claim_coverage")
    sections_payload = _sections_output(outline, report.get("claims") or [])

    citations = {str(number): ref for number, ref in sorted((report.get("citations") or {}).items())}
    unresolved = sorted({int(number) for number in (report.get("unknown_citations") or [])})

    quality_issues: list[str] = []
    if unresolved:
        quality_issues.append(f"引用编号无法在证据池解析：{unresolved}（禁止编造引用编号）")
    if int(counts.get("factual") or 0) == 0:
        quality_issues.append("草稿无可核验的事实性 Claim：claim_coverage 无定义（不用 0 或 1 冒充）")
    if len(citations) == 0:
        quality_issues.append("草稿未产生任何引用编号：证据挂载缺失")

    payload = {
        "draft_id": int(draft_id),
        "title": document.title,
        "outline": outline,
        "sections": sections_payload,
        "claim_coverage": coverage,
        "counts": counts,
        "unsupported": list(report.get("unsupported") or []),
        "citations": citations,
        "unresolved_citations": unresolved,
        "dropped_refs": sorted(set(dropped_refs)),
        "evidence_pool": pool.summary(),
        "evidence_pool_scope_note": pool.scope_note,
        "splitter_source": report.get("splitter_source"),
        "binder_source": report.get("binder_source"),
        "is_stub": is_stub,
        "stub_reasons": list(degradations),
        "d6_decision": d6,
        "human_gate": {
            "node": "N4",
            "decision_point": "D6",
            "required": human_gate_required,
            "note": "manual 模式下引擎在本环节 done 后把下一个待执行环节标记为 waiting_human（N4）",
        },
        "compliance": {
            "disclaimer": drafter.DISCLAIMER,
            "forbidden_scan_hits": sorted(set(forbidden_hits)),
            "forbidden_scan_clean": not forbidden_hits,
        },
        "generation": {"prompt_version": drafter.PROMPT_VERSION, "is_stub": is_stub},
        "notes": notes,
    }

    cost_usd = result_cost_of(*costs)
    notes.extend(unknown_cost_notes(*costs))
    logger.info(
        "writing done draft_id=%s sections=%d factual=%s supported=%s insufficient=%s coverage=%s stub=%s cost=%s",
        draft_id,
        len(sections_payload),
        counts.get("factual"),
        counts.get("supported"),
        counts.get("insufficient"),
        coverage,
        is_stub,
        cost_usd,
    )
    await ctx.progress(100, "写作环节完成（草稿已落库并完成 Claim 校验）")

    return StageResult(
        output=payload,
        text=content_md,
        cost_usd=cost_usd,
        metrics={
            "draft_id": int(draft_id),
            "claim_coverage": coverage,
            "factual_claims": int(counts.get("factual") or 0),
            "supported_claims": int(counts.get("supported") or 0),
            "insufficient_claims": int(counts.get("insufficient") or 0),
            "contradicted_claims": int(counts.get("contradicted") or 0),
            "section_count": len(sections_payload),
            "citation_count": len(citations),
            "evidence_pool_size": len(pool),
            "is_stub": is_stub,
        },
        quality_ok=not quality_issues,
        quality_issues=quality_issues,
        degradations=degradations,
        notes=notes,
        decision={
            "decision_point": "D6",
            "chosen": d6.get("chosen") or "keep_outline",
            "rationale": d6.get("rationale") or (d6.get("reason") or "策略层不可用，按默认结构继续"),
        },
    )


class WritingStage:
    """``writing`` 环节处理器（写作结构决策 D6，人工节点 N4）。"""

    name = "writing"
    decision_point = "D6"

    async def run(self, ctx: StageContext) -> StageResult:
        return await run(ctx)


STAGE = WritingStage()


def register() -> WritingStage:
    """把本环节注册进 WP09 的注册表（幂等；供 API 模块导入时调用）。"""
    from app.services.pipeline.stages import register_stage

    return register_stage("writing", STAGE)  # type: ignore[return-value]


__all__ = [
    "MAX_CLAIMS_PER_SECTION",
    "MAX_SECTIONS",
    "STAGE",
    "UPSTREAM_FIELDS",
    "WritingStage",
    "register",
    "run",
]
