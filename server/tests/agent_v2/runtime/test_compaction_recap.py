"""Recap 与压缩策略验收（计划书 WP-06）。

- 摘要字段固定为 goal / decisions / authorizations / completed / in_progress /
  blocked / errors / important_paths / next_steps / research_state / memory_refs；
- 每条事实带 proposed / queued / implemented / tested / published / installed 标记；
- **计划不得被当成已完成**（proposed/queued 出现在 completed 里必须失败）；
- 触发来源可判定且可审计（预算 / 手动 / 上下文超限）；
- 压缩完成后事件里带结构化 recap，且旧行为（摘要文本、可编辑、可恢复）不变。
"""

from __future__ import annotations

import pytest
from _helpers import Harness, run

from contracts.agent_v2 import ErrorClass, error_response
from services.agent_compaction_v2 import (
    RECAP_FIELDS,
    CompactionPolicy,
    CompactionService,
    CompactionTrigger,
    Recap,
    RecapFact,
    RecapStatus,
    RecapViolation,
    build_recap,
    coerce_recap,
    parse_recap,
)
from services.agent_compaction_v2.service import CompactionService as ServiceViaCanonicalPath


# ---------------------------------------------------------------------------- 结构
def test_recap_fields_match_the_plan():
    assert RECAP_FIELDS == (
        "goal",
        "decisions",
        "authorizations",
        "completed",
        "in_progress",
        "blocked",
        "errors",
        "important_paths",
        "next_steps",
        "research_state",
        "memory_refs",
    )
    recap = build_recap(goal="梳理隐私风险")
    assert set(recap.to_dict()) == set(RECAP_FIELDS)


def test_plan_is_never_reported_as_completed():
    with pytest.raises(RecapViolation):
        build_recap(completed=[RecapFact("还没做的事", RecapStatus.PROPOSED)])
    with pytest.raises(RecapViolation):
        build_recap(completed=[RecapFact("排队中的事", RecapStatus.QUEUED)])
    # 合法：已完成项 + 进行中项各归其位
    recap = build_recap(
        completed=[RecapFact("已落地的改动", RecapStatus.TESTED)],
        in_progress=[RecapFact("还没做的计划", RecapStatus.PROPOSED)],
    )
    assert recap.completed[0].status is RecapStatus.TESTED
    assert recap.in_progress[0].status is RecapStatus.PROPOSED


def test_illegal_status_is_rejected():
    with pytest.raises(RecapViolation):
        RecapFact("x", "done")
    with pytest.raises(RecapViolation):
        RecapFact("   ")


# ---------------------------------------------------------------------------- 解析与渲染
def test_parse_recap_from_structured_text():
    text = """### 目标 / goal
梳理联邦学习的隐私风险

### 已完成 / completed
- [tested] 跑通了事件存储的崩溃恢复测试
- [implemented] 接入了记忆存储

### 进行中 / in_progress
- [proposed] 补研究节点的来源校验

### 下一步 / next_steps
- 与 Agent 3 协调事件命名
"""
    recap = parse_recap(text)
    assert recap.goal == "梳理联邦学习的隐私风险"
    assert [f.status for f in recap.completed] == [RecapStatus.TESTED, RecapStatus.IMPLEMENTED]
    assert recap.in_progress[0].status is RecapStatus.PROPOSED
    assert recap.next_steps == ["与 Agent 3 协调事件命名"]
    # 渲染回来仍然带状态标记
    assert "[tested]" in recap.render()


def test_parse_recap_ignores_unknown_sections_without_guessing():
    recap = parse_recap("### 随便什么标题\n- 一些内容\n")
    assert recap.goal == "" and not recap.completed and not recap.next_steps


def test_coerce_recap_accepts_text_dict_and_object():
    assert isinstance(coerce_recap("### 目标 / goal\nA"), Recap)
    assert coerce_recap({"goal": "B"}).goal == "B"
    existing = build_recap(goal="C")
    assert coerce_recap(existing) is existing


# ---------------------------------------------------------------------------- 策略
def test_policy_decides_trigger_and_documents_why():
    policy = CompactionPolicy(token_budget=100, min_new_events=1, max_auto_per_turn=1)

    assert policy.decide(estimated_tokens=50, new_events_since_last=5) is None
    assert policy.decide(estimated_tokens=150, new_events_since_last=5) is CompactionTrigger.AUTO
    # 没有新内容就不压（防止空转）
    assert policy.decide(estimated_tokens=150, new_events_since_last=0) is None
    # 手动请求绕过预算
    assert (
        policy.decide(estimated_tokens=10, new_events_since_last=3, manual_request=True)
        is CompactionTrigger.MANUAL
    )
    # 上下文超限是运行时信号，优先级最高
    assert (
        policy.decide(
            estimated_tokens=10, new_events_since_last=3, context_overflow=True, manual_request=True
        )
        is CompactionTrigger.RUNTIME
    )
    # 单 Turn 自动压缩次数上限
    assert (
        policy.decide(estimated_tokens=999, new_events_since_last=9, auto_count_in_turn=1) is None
    )

    described = policy.describe(CompactionTrigger.AUTO, estimated_tokens=150)
    assert described["trigger"] == "auto" and described["estimated_tokens"] == 150


# ---------------------------------------------------------------------------- 与服务集成
def test_long_output_is_treated_as_a_recap_batch():
    """常见模型输出（长段落）不应因为解析不出结构就丢摘要。"""
    recap = coerce_recap("这是一段没有小标题的普通摘要文本。")
    assert recap.render() == "", "解析不出结构时渲染为空，由服务层回落原文（见下一个测试）"
    assert recap.goal == ""
    assert recap.completed == []


def _seed(h: Harness, thread_id: str, *, rounds: int = 1, filler: int = 60) -> None:
    for i in range(rounds):
        h.play(thread_id, f"第{i}问", h.text_script(f"第{i}轮回答" * filler), key=f"recap-seed-{i}")


def test_compaction_event_carries_structured_recap(tmp_path):
    h = Harness.create(tmp_path)
    thread = h.thread("recap 落事件")
    _seed(h, thread.thread_id)
    structured = """### 目标 / goal
梳理风险
### 已完成 / completed
- [tested] 事件存储崩溃恢复
### 下一步 / next_steps
- 协调命名
"""
    svc = h.compaction(h.provider(h.text_script(structured)), token_budget=10)
    result = run(svc.compact(thread.thread_id, trigger="manual"))
    assert result.ok is True

    completed = [
        e for e in h.store_for(thread.thread_id).read_all() if e.type == "compaction/completed"
    ][0]
    payload = completed.payload
    assert "recap" in payload, "完成事件必须带结构化 recap"
    assert payload["recap"]["goal"] == "梳理风险"
    assert payload["recap"]["completed"][0]["status"] == "tested"
    assert "[tested]" in payload["summary"], "summary 应为 recap 渲染结果"

    recap = svc.recap(thread.thread_id, result.summary_id)
    assert isinstance(recap, Recap)
    assert recap.next_steps == ["协调命名"]


def test_plain_text_summary_still_works(tmp_path):
    """模型没给结构时回落原文，历史行为不变。"""
    h = Harness.create(tmp_path)
    thread = h.thread("纯文本摘要")
    _seed(h, thread.thread_id)
    svc = h.compaction(h.provider(h.text_script("一段普通摘要")), token_budget=10)
    result = run(svc.compact(thread.thread_id, trigger="manual"))
    assert result.ok and result.text == "一段普通摘要"
    assert svc.summaries(thread.thread_id)[0]["text"] == "一段普通摘要"
    assert svc.recap(thread.thread_id, result.summary_id).render() == ""


def test_failed_compaction_keeps_context_and_writes_failed_event(tmp_path):
    h = Harness.create(tmp_path)
    thread = h.thread("失败不改上下文")
    _seed(h, thread.thread_id)
    ok_svc = h.compaction(h.provider(h.text_script("原始摘要")), token_budget=10)
    good = run(ok_svc.compact(thread.thread_id, trigger="manual"))
    assert good.ok

    failing = h.compaction(h.provider([error_response(ErrorClass.FATAL, "summarizer down")]), token_budget=10)
    h.play(thread.thread_id, "新内容", h.text_script("新回答" * 40), key="recap-extra")
    failed = run(failing.compact(thread.thread_id, trigger="auto"))
    assert failed.ok is False
    assert "compaction/failed" in set(h.event_types(thread.thread_id))
    active = [s for s in ok_svc.summaries(thread.thread_id) if s["active"]]
    assert active and active[0]["text"] == "原始摘要", "失败不得改动生效摘要"


def test_started_event_records_trigger_policy(tmp_path):
    h = Harness.create(tmp_path)
    thread = h.thread("触发可审计")
    _seed(h, thread.thread_id)
    svc = h.compaction(
        h.provider(h.text_script("摘要")),
        token_budget=10,
        policy=CompactionPolicy(token_budget=10),
    )
    result = run(svc.compact(thread.thread_id, trigger="manual"))
    assert result.ok
    started = [
        e for e in h.store_for(thread.thread_id).read_all() if e.type == "compaction/started"
    ][0]
    assert started.payload["trigger_policy"]["trigger"] == "manual"
    assert started.payload["trigger_policy"]["token_budget"] == 10


def test_canonical_service_import_path_works():
    assert ServiceViaCanonicalPath is CompactionService
