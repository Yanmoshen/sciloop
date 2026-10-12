"""Expert run data contracts and local validation helpers."""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from typing import Any

from .registry import EXPERT_ROLES, SCORE_WEIGHTS

EVIDENCE_TYPES = frozenset({"source_evidence", "model_inference", "model_prior", "unknown"})


@dataclass(frozen=True)
class EvidenceRef:
    """Reference to a selected-paper fact or a clearly labeled model statement."""

    id: str
    evidence_type: str
    paper_id: int | str | None = None
    source_id: str | None = None
    quote: str | None = None
    claim: str = ""


@dataclass(frozen=True)
class ResearchContext:
    """Immutable input snapshot supplied by the API/service boundary."""

    selected_papers: tuple[dict[str, Any], ...]
    paper_cards: tuple[dict[str, Any], ...] = ()
    summaries: tuple[dict[str, Any], ...] = ()
    comparison: dict[str, Any] = field(default_factory=dict)
    evidence: tuple[EvidenceRef, ...] = ()
    constraints: dict[str, Any] = field(default_factory=dict)
    input_hash: str | None = None

    def as_payload(self) -> dict[str, Any]:
        return {
            "selected_papers": list(self.selected_papers),
            "paper_cards": list(self.paper_cards),
            "summaries": list(self.summaries),
            "comparison": self.comparison,
            "evidence": [asdict(item) for item in self.evidence],
            "constraints": self.constraints,
            "input_hash": self.input_hash,
            "network_search_allowed": False,
            "source_policy": "Only selected papers and explicitly labeled model knowledge may be used.",
        }


@dataclass
class ExpertCallResult:
    role_id: str
    status: str
    payload: dict[str, Any] | None = None
    error: str | None = None
    model_ref: str | None = None
    duration_ms: int | None = None
    cost_usd: float | None = None
    prompt_hash: str | None = None


@dataclass
class IdeationRunResult:
    status: str
    results: dict[str, ExpertCallResult]
    ideas: list[dict[str, Any]] = field(default_factory=list)
    ranking: list[dict[str, Any]] = field(default_factory=list)
    score_weights: dict[str, int] = field(default_factory=lambda: dict(SCORE_WEIGHTS))
    model_ref: str | None = None
    input_hash: str | None = None

    def as_dict(self) -> dict[str, Any]:
        return {
            "status": self.status,
            "results": {key: asdict(value) for key, value in self.results.items()},
            "ideas": self.ideas,
            "ranking": self.ranking,
            "score_weights": self.score_weights,
            "model_ref": self.model_ref,
            "input_hash": self.input_hash,
        }


def output_schema(role_id: str) -> dict[str, Any]:
    role = next((item for item in EXPERT_ROLES if item.id == role_id), None)
    if role is None:
        raise KeyError(f"unknown expert role: {role_id}")
    if role.output_kind == "seeds":
        return {"type": "object", "required": ["seeds"], "additionalProperties": False, "properties": {"seeds": {"type": "array", "minItems": 8, "items": {"$ref": "#/$defs/idea"}}}, "$defs": {"idea": _idea_schema()}}
    if role.output_kind == "ideas":
        return {"type": "object", "required": ["ideas"], "additionalProperties": False, "properties": {"ideas": {"type": "array", "minItems": 2, "maxItems": 2, "items": _idea_schema()}}}
    if role.output_kind == "scores":
        return {"type": "object", "required": ["scores"], "additionalProperties": False, "properties": {"scores": {"type": "array", "items": {"type": "object", "required": ["idea_id", "dimensions"], "additionalProperties": False, "properties": {"idea_id": {"type": "string"}, "dimensions": {"type": "object", "required": list(SCORE_WEIGHTS), "additionalProperties": False, "properties": {key: _score_dimension_schema() for key in SCORE_WEIGHTS}}, "total_score": {"type": "number", "minimum": 0, "maximum": 100}}}}}}
    if role.output_kind == "ranking":
        return {"type": "object", "required": ["ranking"], "additionalProperties": False, "properties": {"ranking": {"type": "array", "items": {"type": "object", "required": ["idea_id", "rank", "recommendation", "reason", "conditions", "next_experiment"], "additionalProperties": False, "properties": {"idea_id": {"type": "string"}, "rank": {"type": "integer", "minimum": 1}, "recommendation": {"type": "string", "enum": ["recommend", "conditional", "defer", "reject"]}, "reason": {"type": "string"}, "conditions": {"type": "array", "items": {"type": "string"}}, "next_experiment": {"type": "string"}, "opportunity_cost": {"type": "string"}}}}}}
    return {"type": "object", "required": ["findings"], "additionalProperties": False, "properties": {"findings": {"type": "array", "items": {"type": "object", "required": ["idea_id", "claim", "evidence_type", "evidence_refs", "confidence", "unknowns"], "additionalProperties": False, "properties": {"idea_id": {"type": "string"}, "claim": {"type": "string"}, "evidence_type": {"type": "string", "enum": sorted(EVIDENCE_TYPES)}, "evidence_refs": {"type": "array", "items": {"type": "string"}}, "confidence": {"type": "number", "minimum": 0, "maximum": 1}, "unknowns": {"type": "array", "items": {"type": "string"}}, "severity": {"type": "string"}}}}}}


def _idea_schema() -> dict[str, Any]:
    return {"type": "object", "required": ["idea_id", "direction", "title", "research_question", "hypothesis", "method", "innovation", "gap_ids", "paper_ids", "evidence_refs", "experiment_direction", "unknowns", "risks"], "additionalProperties": False, "properties": {"idea_id": {"type": "string"}, "direction": {"type": "string", "enum": ["refinement", "transfer", "combination", "paradigm"]}, "title": {"type": "string"}, "research_question": {"type": "string"}, "hypothesis": {"type": "string"}, "method": {"type": "string"}, "innovation": {"type": "string"}, "gap_ids": {"type": "array", "items": {"type": "string"}}, "paper_ids": {"type": "array", "items": {"type": ["integer", "string"]}}, "evidence_refs": {"type": "array", "minItems": 1, "items": {"type": "string"}}, "experiment_direction": {"type": "string"}, "unknowns": {"type": "array", "items": {"type": "string"}}, "risks": {"type": "array", "items": {"type": "string"}}}}


def _score_dimension_schema() -> dict[str, Any]:
    return {"type": "object", "required": ["score", "reason", "evidence_refs", "confidence", "deductions", "unknowns"], "additionalProperties": False, "properties": {"score": {"type": "number", "minimum": 0, "maximum": 100}, "reason": {"type": "string"}, "evidence_refs": {"type": "array", "items": {"type": "string"}}, "confidence": {"type": "number", "minimum": 0, "maximum": 1}, "deductions": {"type": "array", "items": {"type": "string"}}, "unknowns": {"type": "array", "items": {"type": "string"}}}}


def validate_role_payload(role_id: str, payload: dict[str, Any], evidence_ids: set[str]) -> list[str]:
    """Validate semantic invariants not expressible with JSON Schema."""
    errors: list[str] = []
    kind = next((item.output_kind for item in EXPERT_ROLES if item.id == role_id), None)
    if kind is None:
        return [f"unknown expert role: {role_id}"]
    if kind in {"ideas", "seeds"}:
        key = "ideas" if kind == "ideas" else "seeds"
        if kind == "ideas" and len(payload.get(key, [])) != 2:
            errors.append(f"{role_id} must return exactly 2 ideas")
        expected_direction = {
            "method_iteration": "refinement",
            "scenario_transfer": "transfer",
            "technology_fusion": "combination",
            "paradigm_extension": "paradigm",
        }.get(role_id)
        for index, idea in enumerate(payload.get(key, [])):
            invalid = sorted(set(idea.get("evidence_refs", [])) - evidence_ids)
            if not idea.get("evidence_refs"):
                errors.append(f"{key}[{index}] has no source evidence")
            if invalid:
                errors.append(f"{key}[{index}] references unknown evidence: {', '.join(invalid)}")
            if expected_direction and idea.get("direction") != expected_direction:
                errors.append(f"{key}[{index}] direction must be {expected_direction}")
    for finding in payload.get("findings", []):
        invalid = sorted(set(finding.get("evidence_refs", [])) - evidence_ids)
        if finding.get("evidence_type") == "source_evidence" and not finding.get("evidence_refs"):
            errors.append("source_evidence finding has no evidence reference")
        if invalid:
            errors.append(f"finding references unknown evidence: {', '.join(invalid)}")
    if kind == "scores":
        for score in payload.get("scores", []):
            dims = score.get("dimensions", {})
            for name, dimension in dims.items():
                invalid = sorted(set(dimension.get("evidence_refs", [])) - evidence_ids)
                if invalid:
                    errors.append(f"score {score.get('idea_id')}:{name} references unknown evidence: {', '.join(invalid)}")
            total = sum(float(dims.get(name, {}).get("score", 0)) * weight / 100 for name, weight in SCORE_WEIGHTS.items())
            score["total_score"] = round(total, 2)
    return errors


__all__ = ["EvidenceRef", "ResearchContext", "ExpertCallResult", "IdeationRunResult", "output_schema", "validate_role_payload", "EVIDENCE_TYPES"]
