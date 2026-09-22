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

import asyncio

import pytest

from services.research import orchestrator, prompts
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


# --------------------------------------------------------------------------- #
# 节点里也能联网搜索（搜什么、搜不搜都由模型定）
# --------------------------------------------------------------------------- #
def test_search_queries_is_part_of_the_contract() -> None:
    """模型要上网找资料，就在产出里给搜索词 —— 契约里必须有这个字段。"""

    model = orchestrator.NODE_OUTPUT_MODELS["literature_review"]
    assert "search_queries" in model.model_fields
    assert model.model_fields["search_queries"].default_factory() == []


def test_hand_written_schema_carries_the_decision_fields() -> None:
    """⚠️ 回归护栏：手写 schema 必须带上决策字段。

    2026-09-22 真事故：契约 schema 是手写的，给 Pydantic 模型加字段**不会**自动同步，
    于是渲染给模型的契约里没有 `pending` / `search_queries` —— 模型从不知道可以这么说，
    自检甚至回"我没有联网工具"。这条测试就是防它再脱钩。
    """

    from services.research import contracts

    for node in ("literature_review", "idea_and_feasibility", "experiment_and_data_preparation"):
        properties = (contracts.NODE_OUTPUT_SCHEMAS.get(node) or {}).get("properties") or {}
        for name in ("state", "pending", "state_reason", "search_queries"):
            assert name in properties, f"{node} 的契约里缺 {name}"
        # 决策字段不该被塞进 required（它们都有默认值）
        required = (contracts.NODE_OUTPUT_SCHEMAS[node] or {}).get("required") or []
        assert not {"state", "pending", "search_queries"} & set(required), node


def test_prompt_does_not_show_a_retry_cap_to_the_model() -> None:
    """提示词里不许再给模型看"重试上限" —— 它真的会据此少做尝试。

    2026-09-22 实测：额度块里写着「修复重试：已用 1 / 上限 2」，
    模型在结论里就写了「本节点本次修复重试已用 1/2」，用一条已经不存在的规则限制了自己。
    """

    body = prompts.build_messages(node="literature_review", user_text="x")[-1]["content"]
    retry_lines = [line for line in body.splitlines() if "轮修复" in line]
    assert retry_lines, "额度块里应当有修复轮次的说明"
    assert "没有次数上限" in retry_lines[0]
    assert "上限" not in retry_lines[0].split("（")[0], "修复那一行不许再摆一个上限"


def test_rendered_prompt_actually_shows_the_decision_fields() -> None:
    """真正的检查点：**渲染出来的提示词**里要能看到这些字段名与搜索能力。"""

    body = prompts.build_messages(node="literature_review", user_text="x")[-1]["content"]
    for name in ("state", "pending", "search_queries"):
        assert name in body, f"提示词里看不到 {name}"
    assert "联网搜索结果" in body or "search_queries" in body


def test_prompt_renders_search_results_as_facts() -> None:
    messages = prompts.build_messages(
        node="literature_review",
        user_text="x",
        search_results=[
            {
                "query": "gnn recommendation",
                "ok": True,
                "results": [{"title": "A Survey", "url": "https://a", "snippet": "摘要甲"}],
            },
            {"query": "没搜到的词", "ok": True, "results": []},
        ],
    )
    body = messages[-1]["content"]
    assert "联网搜索结果" in body
    assert "A Survey" in body and "https://a" in body and "摘要甲" in body
    assert "没有搜到结果" in body
    # 必须讲清"这是网页摘要、不是全文"，免得它当论文引用
    assert "不是论文全文" in body


def test_run_searches_returns_blocks_even_when_it_fails(monkeypatch: pytest.MonkeyPatch) -> None:
    """搜不到也要如实返回（带 error），不能吞掉。"""

    from services.agent import web_search

    async def fake_search(query: str, *, limit: int = 5, client: object = None):
        if "坏" in query:
            return {"ok": False, "error": "连不上搜索服务"}
        return {"ok": True, "results": [{"title": "T", "url": "https://t", "snippet": "s"}]}

    monkeypatch.setattr(web_search, "search", fake_search)
    blocks = asyncio.run(orchestrator._run_searches(["好", "坏"]))
    assert [block["ok"] for block in blocks] == [True, False]
    assert blocks[1]["error"] == "连不上搜索服务"
    assert len(blocks[0]["results"]) == 1


def test_search_is_model_decided_and_bounded() -> None:
    """结构断言：搜索接在节点循环里，且一轮有上限（别把上游打爆）。"""

    import inspect

    source = inspect.getsource(orchestrator)
    assert "_run_searches(" in source
    assert 1 <= orchestrator.MAX_SEARCH_QUERIES <= 8


# --------------------------------------------------------------------------- #
# ④⑤ 执行实验 / 结果分析（2026-09-22 补的两种站）
# --------------------------------------------------------------------------- #
def test_two_more_nodes_are_implemented() -> None:
    """后四站里先补两站：执行实验、结果分析。已实现清单是单一事实来源。"""

    from db.models.research import IMPLEMENTED_NODES

    assert "experiment_execution_and_retries" in IMPLEMENTED_NODES
    assert "results_analysis" in IMPLEMENTED_NODES
    for node in ("experiment_execution_and_retries", "results_analysis"):
        assert node in orchestrator.NODE_OUTPUT_MODELS
        assert node in orchestrator.NODE_OUTPUT_SCHEMAS
        properties = orchestrator.NODE_OUTPUT_SCHEMAS[node]["properties"]
        assert "state" in properties and "search_queries" in properties, "决策字段要自动并进去"


def test_execution_node_contract_carries_the_real_run_record() -> None:
    """「真跑」的证据形状：命令 / 状态 / 退出码 / 输出末尾都要在契约里。"""

    schema = orchestrator.NODE_OUTPUT_SCHEMAS["experiment_execution_and_retries"]
    run_item = schema["properties"]["runs"]["items"]["properties"]
    assert {"command", "status", "exit_code", "stdout_tail", "stderr_tail"} <= set(run_item)
    assert run_item["status"]["enum"] == ["success", "failed", "timeout", "skipped"]
    assert "commands_to_run" in schema["properties"]


def test_guides_tell_the_model_to_be_honest_about_not_running() -> None:
    """提示词必须把"没跑就写 skipped"和"负结果如实写"讲清楚。"""

    execution = prompts.NODE_GUIDES["experiment_execution_and_retries"]
    assert "skipped" in execution
    assert "真的跑" in execution
    analysis = prompts.NODE_GUIDES["results_analysis"]
    assert "负结果" in analysis and "无法判定" in analysis


@pytest.mark.parametrize(
    "command,expect_key",
    [
        ("rm -rf /tmp/whatever", "needs_approval"),  # 高危：不静默跑
        ("sudo reboot", "needs_approval"),
    ],
)
def test_node_commands_never_run_high_risk(
    monkeypatch: pytest.MonkeyPatch, command: str, expect_key: str
) -> None:
    """高危命令即使在获授权的对话里也不静默跑 —— 要在对话里单独确认。"""

    from services.agent import approvals, host_runner

    called: list[str] = []

    async def fake_exec(**_kwargs):
        called.append("ran")
        return {"ok": True, "exit_code": 0, "stdout": "", "stderr": ""}

    monkeypatch.setattr(host_runner, "call_exec", fake_exec)
    monkeypatch.setattr(
        approvals, "load", lambda _cid: {"id": "c1", "grants": {"full_access": True}}
    )
    records, granted = asyncio.run(orchestrator._run_node_commands("c1", [command]))
    assert granted is True
    assert records and records[0].get(expect_key) is True
    assert called == [], "高危命令不该真的执行"


def test_node_commands_do_not_run_without_grant(monkeypatch: pytest.MonkeyPatch) -> None:
    """没开「完全访问模式」→ 一条都不跑（这是节点里唯一的闸门）。"""

    from services.agent import approvals, host_runner

    called: list[str] = []

    async def fake_exec(**_kwargs):
        called.append("ran")
        return {"ok": True, "exit_code": 0, "stdout": "", "stderr": ""}

    monkeypatch.setattr(host_runner, "call_exec", fake_exec)
    monkeypatch.setattr(approvals, "load", lambda _cid: {"id": "c1", "grants": {}})
    records, granted = asyncio.run(orchestrator._run_node_commands("c1", ["python --version"]))
    assert granted is False and records == []
    assert called == []


def test_node_commands_refuse_deleting_own_code(monkeypatch: pytest.MonkeyPatch) -> None:
    """硬拒（删 SciLoop 自己的代码）连跑都不跑，且如实说明。"""

    from services.agent import approvals, host_runner, policy

    called: list[str] = []

    async def fake_exec(**_kwargs):
        called.append("ran")
        return {"ok": True}

    monkeypatch.setattr(host_runner, "call_exec", fake_exec)
    monkeypatch.setattr(approvals, "load", lambda _cid: {"id": "c1", "grants": {"allow_exec": True}})
    target = f"rm -rf {policy.sciloop_root()}/web"
    records, granted = asyncio.run(orchestrator._run_node_commands("c1", [target]))
    assert granted is True
    assert records[0]["refused"] is True
    assert called == []


def test_node_commands_actually_run_when_granted(monkeypatch: pytest.MonkeyPatch) -> None:
    from services.agent import approvals, host_runner

    seen: list[dict] = []

    async def fake_exec(**kwargs):
        seen.append(kwargs)
        return {"ok": True, "exit_code": 0, "stdout": "hello\n", "stderr": ""}

    monkeypatch.setattr(host_runner, "call_exec", fake_exec)
    monkeypatch.setattr(
        approvals, "load", lambda _cid: {"id": "c1", "grants": {"full_access": True}}
    )
    records, granted = asyncio.run(
        orchestrator._run_node_commands("c1", ["python --version", "echo hi"])
    )
    assert granted is True and len(records) == 2
    assert seen and "python --version" in seen[0]["command"]
    assert records[0]["stdout_tail"] == "hello\n"


def test_command_results_are_rendered_as_facts() -> None:
    messages = prompts.build_messages(
        node="experiment_execution_and_retries",
        user_text="x",
        command_results=[
            {"command": "python --version", "ok": True, "exit_code": 0, "stdout_tail": "Python 3.13"},
            {"command": "sudo reboot", "ok": False, "needs_approval": True, "error": "高危"},
        ],
    )
    body = messages[-1]["content"]
    assert "你上一轮要求跑的命令与真实输出" in body
    assert "python --version" in body and "退出码 0" in body
    assert "**没有执行**" in body


def test_decision_fields_are_part_of_the_contract() -> None:
    """模型的决定跟着产出一起回来：state / pending / state_reason 必须在契约里。"""

    for node in ("literature_review", "idea_and_feasibility", "experiment_and_data_preparation"):
        model = orchestrator.NODE_OUTPUT_MODELS[node]
        fields = set(model.model_fields)
        assert {"state", "pending", "state_reason"} <= fields, node
        assert model.model_fields["state"].default == "done"


# --------------------------------------------------------------------------- #
# 提示词口径（2026-09-22：判不判定由模型说了算，所以口径必须写清楚）
# --------------------------------------------------------------------------- #
def test_system_prompt_no_longer_claims_program_controls_everything() -> None:
    """旧的系统提示写着"当前节点完全由程序控制…由程序校验后决定"—— 已经不成立。"""

    assert "完全由程序控制" not in prompts.SYSTEM_PROMPT
    assert "state" in prompts.SYSTEM_PROMPT
    for word in ("done", "continue", "need_human"):
        assert word in prompts.SYSTEM_PROMPT, word


def test_system_prompt_allows_done_with_thin_material_but_forbids_fabrication() -> None:
    """材料不足也可以如实说完成；但绝不许编造 —— 这两句必须同时在。"""

    assert "材料不足也可以 done" in prompts.SYSTEM_PROMPT
    assert "绝不为了凑数而编造" in prompts.SYSTEM_PROMPT


def test_rules_are_described_as_facts_not_a_verdict() -> None:
    """提示词里那一段不能再写"不满足将被驳回重跑"。"""

    messages = prompts.build_messages(node="literature_review", user_text="x")
    user_text = messages[-1]["content"]
    assert "不满足将被驳回重跑" not in user_text
    assert "不是判决" in user_text


def test_self_check_messages_carry_payload_and_facts() -> None:
    payload = {"research_question": "某问题", "evidence": []}
    messages = prompts.build_self_check_messages(
        node="literature_review",
        payload=payload,
        research_question="某问题",
        advisories=["· 证据只有 0 条，至少需要 3 条"],
    )
    assert messages[0]["content"] == prompts.SELF_CHECK_SYSTEM
    body = messages[1]["content"]
    assert "某问题" in body
    assert "证据只有 0 条" in body
    assert "research_question" in body, "自检要看到自己交的原件，不能只给摘要"


def test_self_check_schema_is_strict_and_small() -> None:
    schema = orchestrator.SELF_CHECK_SCHEMA
    assert schema["properties"]["state"]["enum"] == ["done", "continue", "need_human"]
    assert schema["additionalProperties"] is False
    assert orchestrator.SELF_CHECK_LIMIT <= 5, "自检是防乒乓，不该变成又一道循环"


def test_self_check_is_wired_into_the_done_branch() -> None:
    """结构断言：模型说 done 之后必须真去自检，且自检结论能改变流程。"""

    import inspect

    source = inspect.getsource(orchestrator)
    assert "_run_self_check(" in source
    assert 'code": "self_check"' in source
    assert "自检认为需要研究者介入" in source
    assert "回头自查时发现还没做完" in source
