# Copyright 2026 SciLoop contributors
# Licensed under the Apache License, Version 2.0.
"""Fixed 20-expert Idea generation pipeline with durable audit records."""

from __future__ import annotations

import datetime as dt
import hashlib
import json
import logging
from collections.abc import Callable
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from db.models.idea_pipeline import IdeaPipelineCandidate, IdeaPipelineExpertRun, IdeaPipelineRun
from db.models.paper import Paper
from services.aggregation.aggregation_service import load_gaps
from services.aggregation.cards import load_cards
from services.ideation_experts import (
    EvidenceRef,
    ExpertCallResult,
    IdeationExpertRunner,
    ResearchContext,
)
from services.ideation_experts.registry import EXPERT_ROLES, SCORE_WEIGHTS

logger = logging.getLogger("sciloop.ideation.expert_pipeline")

DIRECTIONS = ("refinement", "transfer", "combination", "paradigm")
DIRECTION_LABELS = {
    "refinement": "方法迭代型", "transfer": "场景迁移型",
    "combination": "技术融合型", "paradigm": "范式拓展型",
}
WEIGHTS = SCORE_WEIGHTS
CRITICAL_EXPERTS = {index for index, role in enumerate(EXPERT_ROLES, start=1) if role.critical}
EXPERTS: tuple[tuple[str, str], ...] = tuple((role.id, role.description) for role in EXPERT_ROLES)

_GENERIC_SCHEMA: dict[str, Any] = {"type": "object", "additionalProperties": True}


class IdeaPipelineError(Exception):
    def __init__(self, code: str, message: str, detail: Any = None) -> None:
        super().__init__(message)
        self.code, self.message, self.detail = code, message, detail


def _json_default(value: Any) -> str:
    if hasattr(value, "isoformat"):
        return value.isoformat()
    return str(value)


async def build_input_snapshot(
    session: AsyncSession, paper_ids: list[int], aggregation_id: int | None
) -> dict[str, Any]:
    ids = list(dict.fromkeys(int(pid) for pid in paper_ids))
    if not ids or len(ids) > 20:
        raise IdeaPipelineError("invalid_paper_count", "paper_ids must contain 1-20 unique IDs")
    known = set((await session.execute(select(Paper.id).where(Paper.id.in_(ids)))).scalars().all())
    missing = [pid for pid in ids if pid not in known]
    if missing:
        raise IdeaPipelineError("unknown_paper_id", "Selected papers do not exist", {"paper_ids": missing})
    cards = await load_cards(session, ids)
    if not cards:
        raise IdeaPipelineError("parsed_cards_required", "At least one selected paper needs a parsed card")
    cards_by_id = {int(card["paper_id"]): card for card in cards}
    evidence: list[dict[str, Any]] = []
    for pid in ids:
        card = cards_by_id.get(pid)
        if not card:
            continue
        for field in ("research_problem", "core_method", "key_innovation", "technical_route", "experimental_setup", "main_conclusions", "limitations", "transferable"):
            value = card.get(field)
            if value in (None, "", [], {}):
                continue
            quote = json.dumps(value, ensure_ascii=False, default=_json_default) if not isinstance(value, str) else value
            evidence.append({"ref_id": f"E{len(evidence)+1}", "paper_id": pid, "kind": "card_field", "card_field": field, "quote_text": quote[:2500]})
        for span in card.get("spans", []):
            quote = str(span.get("quote_text") or "").strip()
            if quote:
                evidence.append({"ref_id": f"E{len(evidence)+1}", "paper_id": pid, "kind": "paper_span", "paper_span_id": int(span["id"]), "quote_text": quote[:2500], "section_name": span.get("section_name"), "page_number": span.get("page_number")})
    gaps = await load_gaps(session, aggregation_id) if aggregation_id is not None else []
    if aggregation_id is not None:
        from db.models import Aggregation
        aggregation = (await session.execute(select(Aggregation).where(Aggregation.id == int(aggregation_id)))).scalars().first()
        if aggregation is None:
            raise IdeaPipelineError("aggregation_not_found", f"Aggregation {aggregation_id} does not exist")
        if not {int(x) for x in (aggregation.paper_ids or [])}.issubset(set(ids)):
            raise IdeaPipelineError("aggregation_paper_mismatch", "aggregation_id must use only the selected papers")
    if not evidence:
        raise IdeaPipelineError("no_evidence_available", "Selected papers have no usable card fields or quote evidence")
    safe_cards = json.loads(json.dumps([{k: v for k, v in c.items() if k != "spans"} for c in cards], ensure_ascii=False, default=_json_default))
    payload = {
        "paper_ids": ids,
        "aggregation_id": aggregation_id,
        "cards": safe_cards,
        "gaps": gaps,
        "evidence_catalog": evidence,
        "external_search": "forbidden",
        "provenance_rule": "Selected paper material is source_evidence; model knowledge is model_prior and never a citation.",
    }
    payload["sha256"] = hashlib.sha256(json.dumps(payload, ensure_ascii=False, sort_keys=True, default=_json_default).encode()).hexdigest()
    return payload


async def create_run(
    session: AsyncSession, *, paper_ids: list[int], aggregation_id: int | None = None, project_id: int | None = None
) -> dict[str, Any]:
    snapshot = await build_input_snapshot(session, paper_ids, aggregation_id)
    run = IdeaPipelineRun(project_id=project_id, aggregation_id=aggregation_id, paper_ids=snapshot["paper_ids"], input_snapshot=snapshot, status="queued")
    session.add(run)
    await session.flush()
    for number, (key, _prompt) in enumerate(EXPERTS, start=1):
        session.add(IdeaPipelineExpertRun(run_id=int(run.id), expert_number=number, expert_key=key, status="queued", input_snapshot={"depends_on": []}))
    await session.commit()
    return await get_run(session, int(run.id)) or {}


async def execute_run(session: AsyncSession, run_id: int, *, chat_fn: Callable[..., Any] | None = None) -> dict[str, Any]:
    """Execute the fixed chain sequentially, issuing one model call for each role."""
    run = (await session.execute(select(IdeaPipelineRun).where(IdeaPipelineRun.id == int(run_id)))).scalars().first()
    if run is None:
        raise IdeaPipelineError("run_not_found", f"Idea run {run_id} does not exist")
    if run.status not in {"queued", "running"}:
        return await get_run(session, run_id) or {}
    if chat_fn is None:
        from llm.adapter import chat as chat_fn
    try:
        from llm.router import get_router
        model_ref = (await get_router().resolve("plan", project_id=run.project_id)).model_ref
        snapshot = dict(run.input_snapshot or {})
        papers = tuple({k: v for k, v in card.items() if k != "spans"} for card in snapshot["cards"])
        refs = tuple(EvidenceRef(id=item["ref_id"], evidence_type="source_evidence", paper_id=item["paper_id"], source_id=item.get("paper_span_id") or item.get("card_field"), quote=item.get("quote_text"), claim=item.get("card_field") or "paper span") for item in snapshot["evidence_catalog"])
        context = ResearchContext(selected_papers=papers, paper_cards=papers, summaries=tuple({"paper_id": card["paper_id"], "summary": card.get("abstract")} for card in papers), comparison={"gaps": snapshot["gaps"], "aggregation_id": snapshot["aggregation_id"]}, evidence=refs, constraints={"network_search_allowed": False, "paper_ids": snapshot["paper_ids"]}, input_hash=snapshot["sha256"])
        experts = (await session.execute(select(IdeaPipelineExpertRun).where(IdeaPipelineExpertRun.run_id == run_id).order_by(IdeaPipelineExpertRun.expert_number))).scalars().all()
        by_key = {role.id: experts[index] for index, role in enumerate(EXPERT_ROLES)}
        run.status, run.model_ref = "running", model_ref
        initial_results: dict[str, ExpertCallResult] = {}
        for expert in experts:
            expert.input_snapshot = {"source_snapshot_sha256": snapshot["sha256"], "context": context.as_payload(), "dependencies": list(EXPERT_ROLES[expert.expert_number - 1].dependencies)}
            if expert.status == "completed" and expert.output is not None:
                initial_results[expert.expert_key] = ExpertCallResult(
                    role_id=expert.expert_key,
                    status="succeeded",
                    payload=expert.output,
                    model_ref=expert.model_ref or model_ref,
                    cost_usd=expert.cost_usd,
                )
            elif expert.status not in {"running", "queued"}:
                expert.status = "queued"
            elif expert.status == "running":
                # A process restart can leave a row in running without a
                # result. It is safe to issue its one unfinished call again.
                expert.status = "queued"
                expert.completed_at = None
        run.current_expert = None
        await session.commit()

        runner = IdeationExpertRunner(call=chat_fn)
        async def persist_role_event(role_id: str, phase: str, result: Any | None) -> None:
            row = by_key[role_id]
            if phase == "started":
                row.status = "running"
                row.started_at = dt.datetime.now(dt.UTC)
                run.current_expert = row.expert_number
            else:
                row.status = "completed" if result.status == "succeeded" else "failed"
                row.output = result.payload
                row.raw_output = json.dumps(result.payload, ensure_ascii=False, default=_json_default) if result.payload is not None else None
                row.model_ref, row.cost_usd = result.model_ref, result.cost_usd
                row.attempts = 1
                row.error = result.error
                row.completed_at = dt.datetime.now(dt.UTC)
            await session.commit()

        outcome = await runner.run(
            context,
            project_id=run.project_id,
            model_ref=model_ref,
            initial_results=initial_results,
            on_role_event=persist_role_event,
        )
        run.current_expert = None
        if outcome.status != "completed":
            failed = [role.id for role in EXPERT_ROLES if role.critical and outcome.results[role.id].status != "succeeded"]
            run.status, run.error = "failed", f"critical_experts_failed:{','.join(failed)}"
            run.completed_at = dt.datetime.now(dt.UTC)
            await session.commit()
            return await get_run(session, run_id) or {}

        idea_list = outcome.ideas
        catalog = {item["ref_id"]: item for item in snapshot["evidence_catalog"]}
        candidate_by_idea: dict[str, IdeaPipelineCandidate] = {}
        direction_counts = dict.fromkeys(DIRECTIONS, 0)
        for idea in idea_list:
            direction = str(idea.get("direction") or "")
            refs_used = [ref for ref in idea.get("evidence_refs", []) if ref in catalog]
            if direction not in direction_counts or not refs_used:
                raise ValueError("candidate missing supported direction or selected-paper evidence")
            direction_counts[direction] += 1
            candidate = IdeaPipelineCandidate(run_id=run_id, direction=direction, ordinal=direction_counts[direction], title=str(idea.get("title") or ""), payload=idea, evidence=[catalog[ref] for ref in refs_used])
            session.add(candidate)
            candidate_by_idea[str(idea["idea_id"])] = candidate
        if any(count != 2 for count in direction_counts.values()):
            raise ValueError(f"expected 2 candidates per direction, got {direction_counts}")

        score_payload = outcome.results["independent_scorer"].payload or {}
        for score in score_payload.get("scores", []):
            candidate = candidate_by_idea.get(str(score.get("idea_id")))
            if candidate:
                candidate.score = {"dimensions": score.get("dimensions", {}), "total": score.get("total_score"), "computed_by": "server"}
        rankings = (outcome.results["decision_ranker"].payload or {}).get("ranking", [])
        rank_by_id = {str(item.get("idea_id")): item for item in rankings}
        # Server recomputes the weighted total and makes it the deterministic ordering basis.
        for candidate_id, candidate in candidate_by_idea.items():
            dimensions = (candidate.score or {}).get("dimensions", {})
            total = sum(float(dimensions.get(name, {}).get("score", 0)) * weight / 100 for name, weight in WEIGHTS.items())
            candidate.score = {**(candidate.score or {}), "total": round(total, 2), "computed_by": "server"}
            advice = rank_by_id.get(candidate_id, {})
            candidate.recommendation = advice.get("recommendation", "defer")
            if not (candidate.score.get("dimensions") or {}) or candidate.score["total"] <= 0:
                candidate.recommendation = "defer"
            candidate.payload = {**candidate.payload, "decision_advice": advice}
        sorted_candidates = sorted(candidate_by_idea.values(), key=lambda item: (-float((item.score or {}).get("total", 0)), str(item.title)))
        for rank, candidate in enumerate(sorted_candidates, start=1):
            candidate.rank = rank
        await session.flush()
        run.status, run.completed_at = "completed", dt.datetime.now(dt.UTC)
        run.decision = {"ranked_candidate_ids": [int(candidate.id) for candidate in sorted_candidates], "recommendation_policy": "expert 20 advice constrained by server evidence and scoring gates", "human_confirmation_required": True}
        await session.commit()
    except Exception as exc:
        await session.rollback()
        run = (await session.execute(select(IdeaPipelineRun).where(IdeaPipelineRun.id == int(run_id)))).scalars().first()
        if run:
            run.status, run.error, run.completed_at = "failed", f"{type(exc).__name__}: {exc}", dt.datetime.now(dt.UTC)
            await session.commit()
    return await get_run(session, run_id) or {}


async def get_run(session: AsyncSession, run_id: int) -> dict[str, Any] | None:
    run = (await session.execute(select(IdeaPipelineRun).where(IdeaPipelineRun.id == int(run_id)))).scalars().first()
    if run is None:
        return None
    experts = (await session.execute(select(IdeaPipelineExpertRun).where(IdeaPipelineExpertRun.run_id == int(run_id)).order_by(IdeaPipelineExpertRun.expert_number))).scalars().all()
    candidates = (await session.execute(select(IdeaPipelineCandidate).where(IdeaPipelineCandidate.run_id == int(run_id)).order_by(IdeaPipelineCandidate.rank, IdeaPipelineCandidate.id))).scalars().all()
    failed_experts = [x for x in experts if x.status == "failed"]
    completed_experts = sum(1 for x in experts if x.status == "completed")
    failure_reason = None
    failure_text = " ".join(str(x.error or "") for x in failed_experts)
    if "Insufficient Balance" in failure_text or "Payment Required" in failure_text:
        failure_reason = "模型服务余额不足，请充值默认模型后重新运行。"
    elif "database is locked" in failure_text:
        failure_reason = "任务写入发生数据库锁冲突，请稍后重试。"
    elif "LLMJSONValidationError" in failure_text:
        failure_reason = "部分专家返回格式不合规，已记录失败专家；可重新运行。"
    elif failed_experts:
        failure_reason = "部分专家调用失败，请查看失败专家详情后重试。"
    generation_seconds = None
    if run.created_at and run.completed_at:
        created_at = run.created_at
        completed_at = run.completed_at
        # SQLite returns server timestamps without tzinfo while Python writes
        # completion timestamps in UTC. Normalize both before subtraction.
        if created_at.tzinfo is None:
            created_at = created_at.replace(tzinfo=dt.UTC)
        if completed_at.tzinfo is None:
            completed_at = completed_at.replace(tzinfo=dt.UTC)
        generation_seconds = round((completed_at - created_at).total_seconds(), 3)
    return {"run": {"id": int(run.id), "project_id": run.project_id, "aggregation_id": run.aggregation_id, "paper_ids": run.paper_ids, "input_snapshot_sha256": (run.input_snapshot or {}).get("sha256"), "model_ref": run.model_ref, "status": run.status, "current_expert": run.current_expert, "confirmation_status": run.confirmation_status, "decision": run.decision, "error": run.error, "failure_reason": failure_reason, "completed_experts": completed_experts, "failed_experts": [x.expert_key for x in failed_experts], "experts_total": len(experts), "created_at": run.created_at.isoformat() if run.created_at else None, "completed_at": run.completed_at.isoformat() if run.completed_at else None, "generation_seconds": generation_seconds, "experts": [{"id": int(x.id), "expert_number": x.expert_number, "expert_key": x.expert_key, "status": x.status, "output": x.output, "raw_output": x.raw_output, "model_ref": x.model_ref, "prompt_version": x.prompt_version, "cost_usd": x.cost_usd, "attempts": x.attempts, "started_at": x.started_at.isoformat() if x.started_at else None, "completed_at": x.completed_at.isoformat() if x.completed_at else None, "error": x.error} for x in experts], "candidates": [{"id": int(c.id), "direction": c.direction, "ordinal": c.ordinal, "title": c.title, "payload": c.payload, "evidence": c.evidence, "score": c.score, "rank": c.rank, "recommendation": c.recommendation, "is_selected": c.is_selected} for c in candidates]}}


async def list_runs(session: AsyncSession, *, aggregation_id: int) -> list[dict[str, Any]]:
    """Return the newest expert runs for a research task, newest first."""
    rows = (
        await session.execute(
            select(IdeaPipelineRun)
            .where(IdeaPipelineRun.aggregation_id == int(aggregation_id))
            .order_by(IdeaPipelineRun.id.desc())
            .limit(20)
        )
    ).scalars().all()
    items: list[dict[str, Any]] = []
    for row in rows:
        payload = await get_run(session, int(row.id))
        if payload and payload.get("run"):
            summary = payload["run"]
            # Keep the history endpoint lightweight while exposing enough
            # information for the task list to identify a completed run.
            summary = {
                **summary,
                "candidate_count": len(summary.get("candidates") or []),
                "expert_count": len(summary.get("experts") or []),
            }
            summary.pop("candidates", None)
            summary.pop("experts", None)
            items.append(summary)
    return items


async def confirm_candidate(session: AsyncSession, run_id: int, candidate_id: int, *, note: str | None = None) -> dict[str, Any]:
    run = (await session.execute(select(IdeaPipelineRun).where(IdeaPipelineRun.id == int(run_id)))).scalars().first()
    if run is None:
        raise IdeaPipelineError("run_not_found", f"Idea run {run_id} does not exist")
    if run.status != "completed":
        raise IdeaPipelineError("run_not_completed", "Only completed runs can be confirmed")
    candidate = (await session.execute(select(IdeaPipelineCandidate).where(IdeaPipelineCandidate.id == int(candidate_id), IdeaPipelineCandidate.run_id == int(run_id)))).scalars().first()
    if candidate is None:
        raise IdeaPipelineError("candidate_not_found", f"Candidate {candidate_id} does not belong to run {run_id}")
    if not candidate.evidence:
        raise IdeaPipelineError("candidate_has_no_evidence", "Candidate cannot be confirmed without selected-paper evidence")
    if candidate.recommendation == "reject":
        raise IdeaPipelineError("candidate_rejected", "Rejected candidates cannot enter the research workflow")
    candidates = (await session.execute(select(IdeaPipelineCandidate).where(IdeaPipelineCandidate.run_id == int(run_id)))).scalars().all()
    for item in candidates:
        item.is_selected = item.id == candidate.id
    run.confirmation_status = "confirmed"
    run.decision = {**(run.decision or {}), "confirmed_candidate_id": int(candidate.id), "human_note": note, "confirmed_at": dt.datetime.now(dt.UTC).isoformat()}
    await session.commit()
    return await get_run(session, run_id) or {}


__all__ = ["CRITICAL_EXPERTS", "DIRECTIONS", "DIRECTION_LABELS", "EXPERTS", "IdeaPipelineError", "WEIGHTS", "confirm_candidate", "create_run", "execute_run", "get_run", "list_runs"]
