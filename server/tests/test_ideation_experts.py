from __future__ import annotations

from types import SimpleNamespace

import jsonschema
import pytest

from services.ideation_experts import (
    EXPERT_ROLES,
    SCORE_WEIGHTS,
    EvidenceRef,
    IdeationExpertRunner,
    ResearchContext,
    topological_levels,
    validate_role_payload,
)
from services.ideation_experts.schemas import output_schema


def _idea(role_id: str, direction: str) -> dict:
    return {
        "idea_id": f"{role_id}-1",
        "direction": direction,
        "title": "A grounded idea",
        "research_question": "Can the stated limitation be addressed?",
        "hypothesis": "A testable hypothesis",
        "method": "A described method",
        "innovation": "A concrete distinction",
        "gap_ids": ["gap-1"],
        "paper_ids": [1],
        "evidence_refs": ["E1"],
        "experiment_direction": "Compare with a baseline",
        "unknowns": [],
        "risks": [],
    }


def _response(role_id: str) -> dict:
    if role_id == "idea_seed_generator":
        return {"seeds": [_idea(role_id, direction) for direction in ("refinement", "transfer", "combination", "paradigm") for _ in range(2)]}
    direction_roles = {
        "method_iteration": "refinement",
        "scenario_transfer": "transfer",
        "technology_fusion": "combination",
        "paradigm_extension": "paradigm",
    }
    if role_id in direction_roles:
        return {"ideas": [_idea(role_id, direction_roles[role_id]) | {"idea_id": f"{role_id}-{i}"} for i in range(2)]}
    if role_id == "independent_scorer":
        return {"scores": []}
    if role_id == "decision_ranker":
        return {"ranking": []}
    return {"findings": []}


def test_registry_has_20_roles_and_acyclic_stable_dag():
    levels = topological_levels()
    assert len(EXPERT_ROLES) == 20
    assert sum(map(len, levels)) == 20
    positions = {role_id: index for index, level in enumerate(levels) for role_id in level}
    for role in EXPERT_ROLES:
        assert all(positions[dependency] < positions[role.id] for dependency in role.dependencies)
    assert sum(SCORE_WEIGHTS.values()) == 100


def test_idea_evidence_references_must_be_selected_paper_evidence():
    idea = _idea("method_iteration", "refinement")
    assert validate_role_payload("method_iteration", {"ideas": [idea, idea]}, {"E1"}) == []
    idea["evidence_refs"] = ["made-up"]
    assert "unknown evidence" in validate_role_payload("method_iteration", {"ideas": [idea, idea]}, {"E1"})[0]


@pytest.mark.asyncio
async def test_runner_calls_each_role_once_with_same_explicit_model():
    called: list[tuple[str, str, dict]] = []

    async def fake_call(messages, **kwargs):
        role_id = kwargs["purpose"].split(":", 1)[1]
        called.append((role_id, kwargs["model_ref"], kwargs))
        return SimpleNamespace(parsed=_response(role_id), model_ref=kwargs["model_ref"], duration_ms=1, cost_usd=0.0)

    context = ResearchContext(
        selected_papers=({"paper_id": 1, "title": "Selected paper"},),
        evidence=(EvidenceRef(id="E1", evidence_type="source_evidence", paper_id=1, quote="A source passage"),),
    )
    result = await IdeationExpertRunner(call=fake_call).run(context, model_ref="provider:model")
    assert len(called) == 20
    assert {role_id for role_id, _, _ in called} == {role.id for role in EXPERT_ROLES}
    assert all(model_ref == "provider:model" for _, model_ref, _ in called)
    assert all(kwargs["allow_fallback"] is False and kwargs["json_retry"] == 0 for _, _, kwargs in called)
    assert result.status == "completed"
    assert len(result.ideas) == 8


@pytest.mark.asyncio
async def test_failed_critical_expert_is_not_retried_and_blocks_decision():
    counts: dict[str, int] = {}

    async def fake_call(messages, **kwargs):
        role_id = kwargs["purpose"].split(":", 1)[1]
        counts[role_id] = counts.get(role_id, 0) + 1
        if role_id == "evidence_extractor":
            raise RuntimeError("provider unavailable")
        return SimpleNamespace(parsed=_response(role_id), model_ref=kwargs["model_ref"], duration_ms=1, cost_usd=0.0)

    context = ResearchContext(
        selected_papers=({"paper_id": 1},),
        evidence=(EvidenceRef(id="E1", evidence_type="source_evidence", paper_id=1, quote="source"),),
    )
    result = await IdeationExpertRunner(call=fake_call).run(context, model_ref="provider:model")
    assert len(counts) == 20
    assert set(counts.values()) == {1}
    assert result.results["evidence_extractor"].status == "failed"
    assert result.status == "blocked"
    assert result.ranking == []


@pytest.mark.asyncio
async def test_runner_resumes_completed_roles_without_calling_them_again():
    called: list[str] = []

    async def fake_call(messages, **kwargs):
        role_id = kwargs["purpose"].split(":", 1)[1]
        called.append(role_id)
        return SimpleNamespace(parsed=_response(role_id), model_ref=kwargs["model_ref"], duration_ms=1, cost_usd=0.0)

    context = ResearchContext(
        selected_papers=({"paper_id": 1},),
        evidence=(EvidenceRef(id="E1", evidence_type="source_evidence", paper_id=1, quote="source"),),
    )
    completed = _response("evidence_extractor")
    initial = {"evidence_extractor": SimpleNamespace(role_id="evidence_extractor", status="succeeded", payload=completed)}
    result = await IdeationExpertRunner(call=fake_call).run(context, model_ref="provider:model", initial_results=initial)
    assert "evidence_extractor" not in called
    assert len(called) == 19
    assert result.status == "completed"


def test_idea_schema_allows_non_semantic_note_fields():
    idea = _idea("method_iteration", "refinement")
    idea["unknowns_note"] = "附加说明"
    payload = {"ideas": [idea, {**idea, "idea_id": "method_iteration-2", "idea_id_note": "附加说明"}]}
    jsonschema.validate(payload, output_schema("method_iteration"))
