from __future__ import annotations

from services.research import messages


def _blocks(*, node: str, node_label: str, has_evidence: bool) -> list[dict[str, object]]:
    return messages.waiting_human_blocks(
        node=node,
        node_label=node_label,
        has_evidence=has_evidence,
    )


def test_empty_waiting_node_has_no_program_generated_choices() -> None:
    assert _blocks(node="literature_review", node_label="文献调研", has_evidence=False) == []


def test_explicit_model_choices_are_preserved() -> None:
    blocks = messages.waiting_human_blocks(
        summary="A. 继续检索\nB. 先整理已有资料",
        node="results_analysis",
        node_label="结果分析",
        has_evidence=False,
    )
    assert [item["id"] for item in blocks[0]["options"]] == ["proposal_A", "proposal_B"]
