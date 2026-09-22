# Copyright 2026 SciLoop contributors
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
"""节点主循环的"报事实"辅助（纯函数，不需要数据库/模型）。

背景（2026-09-22 研究者要求）：
- 「不要让材料不足卡住任何步骤，程序不能强制卡住，都是由大模型决定」；
- 「不要设置模型重跑 3 次上限，让模型自己决定什么时候停下」；
- 「不要程序做决策、不要什么 3 条硬规则」。

因此主循环里的两个程序判定点被拆掉，只剩两个**结构性**检查：
① 产出能不能落库（契约）；② 回退申请是否带齐信息。研究质量类判定一律降级为"提醒"。
"""

from __future__ import annotations

from services.research import orchestrator
from services.research.rules import RuleHit, ValidationResult


def test_contract_payload_accepts_valid_output() -> None:
    candidate = {
        "research_question": "某个问题",
        "queries": [],
        "evidence": [],
        "closest_work": [],
        "coverage_note": "",
        "gaps": [],
        "recommended_queries": [],
        "limits": [],
    }
    usable, normalized, why = orchestrator._contract_payload("literature_review", candidate)
    assert usable and why == ""
    assert normalized["research_question"] == "某个问题"


def test_contract_payload_rejects_broken_output_with_reason() -> None:
    usable, normalized, why = orchestrator._contract_payload("literature_review", {"evidence": "不是列表"})
    assert not usable and normalized == {}
    assert "契约" in why, "拒收必须给得出原因，模型才能修"


def test_contract_payload_rejects_empty_or_unparsable() -> None:
    for bad in (None, {}, "字符串", 42):
        usable, _, why = orchestrator._contract_payload("literature_review", bad)  # type: ignore[arg-type]
        assert not usable
        assert why


def test_placeholder_nodes_pass_through() -> None:
    """占位节点没有契约 → 原样放行（它们本来就只做占位，不该被卡住）。"""

    usable, normalized, why = orchestrator._contract_payload("paper_writing", {"anything": 1})
    assert usable and normalized == {"anything": 1} and why == ""


def test_advisory_notes_are_human_facts_not_verdicts() -> None:
    validation = ValidationResult(
        ok=False,
        level="L1",
        hits=[
            RuleHit("R2", "L1", "第 3 条证据引用的论文不在库里", "evidence"),
            RuleHit("R5", "L1", "研究问题与证据方向不一致", "research_question"),
        ],
    )
    notes = orchestrator._advisory_notes(validation)
    assert len(notes) == 2
    assert notes[0].startswith("· ")
    assert "R2" not in notes[0], "给研究者看的是事实，不是规则编号"
    assert "不在库里" in notes[0]


def test_advisory_notes_empty_when_nothing_to_say() -> None:
    assert orchestrator._advisory_notes(None) == []
    assert orchestrator._advisory_notes(ValidationResult(ok=True, level=None, hits=[])) == []


def test_node_loop_has_no_retry_cap() -> None:
    """节点循环里**不能再有次数上限**（研究者 2026-09-22 明确要求）。

    这是结构性断言：直接看源码里还有没有 `range(max_retry`。
    留着它是因为这类"上限"极容易被后来的人以"保险起见"重新加回来 ——
    而一旦加回来，"由模型决定何时停"就名存实亡了。
    """

    import inspect

    source = inspect.getsource(orchestrator)
    assert "range(max_retry" not in source, "节点循环不许再有重试次数上限"
    assert "while True:" in source, "主循环应当是「跑到模型说停为止」"


def test_decision_fields_are_part_of_the_contract() -> None:
    """模型的决定跟着产出一起回来：state / pending / state_reason 必须在契约里。"""

    for node in ("literature_review", "idea_and_feasibility", "experiment_and_data_preparation"):
        model = orchestrator.NODE_OUTPUT_MODELS[node]
        fields = set(model.model_fields)
        assert {"state", "pending", "state_reason"} <= fields, node
        assert model.model_fields["state"].default == "done"
