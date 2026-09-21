# Copyright 2026 SciLoop contributors
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
"""研究节点图：节点定义、允许的迁移边、三类闸门、状态映射。

设计口径（重要）
----------------
**回退由模型决定，程序只拦「退得规不规范」。** 因此这里的闸门不判断
「该不该回退」，只做三件事：

``G1 必带信息``  回退必须带齐：重合证据 / 仍存差异 / 是否值得继续。缺项 → 拒绝迁移，
                 把缺项回灌提示词让模型补齐。
``G2 次数上限``  单节点进入内重试 ≤2；单节点被回退 ≤2；总回退 ≤4。超出 → 拒绝并置
                 ``waiting_human``（不是静默继续）。
``G3 落痕``      每次迁移无条件写 ``research_node_transitions`` 一行——这不是可选项。

这条边界是「程序主控」在回退路径上唯一的抓手，也是可审计叙事能成立的前提：
模型可以说「我要退」，但退得没法核对就不许退。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from app.db.models.research import IMPLEMENTED_NODES, RESEARCH_NODES

__all__ = [
    "ALLOWED_ADVANCE",
    "ALLOWED_REVERT",
    "DOC_STATUS_DISPLAY",
    "GATE_ORDER",
    "MAX_RETRY_PER_ENTRY",
    "MAX_REVISIT_PER_NODE",
    "MAX_TOTAL_REVERTS",
    "NODE_LABELS",
    "NODE_ORDER",
    "PURPOSE_COLUMN_MAX",
    "REQUIRED_REVERT_FIELDS",
    "REVERT_REQUIREMENTS",
    "STAGE_ALIASES",
    "STAGE_COLUMN_MAX",
    "GateResult",
    "check_revert_gate",
    "display_status",
    "is_implemented",
    "next_node",
    "revert_targets",
    "stage_alias",
]

#: 七个研究节点（顺序即推荐推进顺序）。
#: **单一事实来源在 ``app/db/models/research.py``**，这里只做转出，
#: 避免节点清单出现在两处后悄悄漂移。
NODE_ORDER: tuple[str, ...] = RESEARCH_NODES

#: 展示名（前端与事件载荷统一用它，不要在别处再写一份）
NODE_LABELS: dict[str, str] = {
    "literature_review": "文献调研",
    "idea_and_feasibility": "Idea 与可行性",
    "experiment_and_data_preparation": "实验准备",
    "experiment_execution_and_retries": "执行实验",
    "results_analysis": "结果分析",
    "paper_writing": "论文写作",
    "paper_review": "论文评审",
    "research_retrospective": "研究复盘",
    "end": "已结束",
}

#: 写 ``llm_call_logs.stage`` 用的短别名。
#: 该列是 ``varchar(24)``（既有表结构，本层不改它），而
#: ``experiment_and_data_preparation`` / ``experiment_execution_and_retries`` 都是 31 字符，
#: 直接写会 ``StringDataRightTruncationError`` —— 调用已经发生、日志却写不进去，
#: 整次调用会被判为失败。节点全名仍完整保留在 ``purpose``（varchar(64)）里。
STAGE_ALIASES: dict[str, str] = {
    "literature_review": "literature_review",
    "idea_and_feasibility": "idea_and_feasibility",
    "experiment_and_data_preparation": "experiment_prep",
    "experiment_execution_and_retries": "experiment_run",
    "results_analysis": "results_analysis",
    "paper_writing": "paper_writing",
    "paper_review": "paper_review",
}

#: 既有表的列宽上限（用于自检，避免以后再踩）
STAGE_COLUMN_MAX = 24
PURPOSE_COLUMN_MAX = 64

#: 首版实装校验的三个节点（来源同 ``app/db/models/research.py``）
_IMPLEMENTED: frozenset[str] = frozenset(IMPLEMENTED_NODES)

#: 允许的推进边（from → to）
ALLOWED_ADVANCE: dict[str, str] = {
    "literature_review": "idea_and_feasibility",
    "idea_and_feasibility": "experiment_and_data_preparation",
    "experiment_and_data_preparation": "experiment_execution_and_retries",
    "experiment_execution_and_retries": "results_analysis",
    "results_analysis": "paper_writing",
    "paper_writing": "paper_review",
}

#: 允许的回退边（当前节点 → 可退回的目标节点）。**只允许向后**：
#: 起点节点（文献调研）没有回退目标。
#: 文献与 idea 的双向性由两条边共同表达：idea → literature（发现相关工作不足回去补），
#: 以及补完后从 literature 正常 advance 回 idea——不是把 advance 伪装成 revert。
ALLOWED_REVERT: dict[str, tuple[str, ...]] = {
    "idea_and_feasibility": ("literature_review",),
    "experiment_and_data_preparation": ("idea_and_feasibility",),
    "experiment_execution_and_retries": ("experiment_and_data_preparation",),
    "results_analysis": ("experiment_and_data_preparation",),
    "paper_writing": ("literature_review", "results_analysis"),
}

#: 回退必带字段（闸门 G1）
REQUIRED_REVERT_FIELDS: tuple[str, ...] = (
    "overlap_evidence",
    "remaining_difference",
    "worth_continuing",
)

#: 人话版的必带信息说明（回灌给模型时用它，不暴露字段名即可读）
REVERT_REQUIREMENTS: dict[str, str] = {
    "overlap_evidence": "重合证据：哪篇论文在什么地方与当前方案重合",
    "remaining_difference": "仍存差异：退回去之后还有哪些差异没被解决",
    "worth_continuing": "是否值得继续：true / false",
}

#: 闸门上限
MAX_RETRY_PER_ENTRY = 2
MAX_REVISIT_PER_NODE = 2
MAX_TOTAL_REVERTS = 4

#: 闸门顺序（G1 先于 G2：先看退得对不对，再看还退不退得动）
GATE_ORDER: tuple[str, ...] = ("G1", "G2", "G3")

#: 程序内部六态 → 文档五态（展示层映射；存储里只用程序态）
DOC_STATUS_DISPLAY: dict[str, str] = {
    "pending": "pending",
    "running": "running",
    "waiting_human": "needs_input",
    "done": "ready",
    "failed": "needs_revision",
    "blocked": "blocked",
}


def is_implemented(node: str) -> bool:
    """该节点首版是否实装校验（未实装 = 占位，允许进入但不假装校验过）。"""

    return node in _IMPLEMENTED


def next_node(node: str) -> str | None:
    return ALLOWED_ADVANCE.get(node)


def revert_targets(node: str) -> tuple[str, ...]:
    return ALLOWED_REVERT.get(node, ())


def display_status(status: str) -> str:
    """程序状态 → 文档五态（界面与交接块用）。"""

    return DOC_STATUS_DISPLAY.get(status, status)


def stage_alias(node: str) -> str:
    """写 ``llm_call_logs.stage`` 用的短别名（保证 ≤ 24 字符）。"""

    alias = STAGE_ALIASES.get(node) or node
    return alias[:STAGE_COLUMN_MAX]


def purpose_for(node: str) -> str:
    """写 ``llm_call_logs.purpose`` 用的完整标记（保证 ≤ 64 字符）。"""

    return f"research_{node}"[:PURPOSE_COLUMN_MAX]


@dataclass(frozen=True)
class GateResult:
    """闸门判定结果。``gate`` 为 None 表示全部通过。"""

    ok: bool
    gate: str | None = None
    message: str = ""
    missing_fields: tuple[str, ...] = ()

    def to_dict(self) -> dict[str, Any]:
        return {
            "ok": self.ok,
            "gate": self.gate,
            "message": self.message,
            "missing_fields": list(self.missing_fields),
        }


def check_revert_gate(
    *,
    current_node: str,
    target: str,
    carried: dict[str, Any] | None,
    retry_count: int = 0,
    revisit_count: int = 0,
    total_reverts: int = 0,
) -> GateResult:
    """回退三类闸门判定。

    参数都是「已经查好的事实」，本函数不碰数据库，便于单测。
    """

    # G1：必带信息 —— 先看退得对不对
    payload = carried if isinstance(carried, dict) else {}
    missing = tuple(f for f in REQUIRED_REVERT_FIELDS if payload.get(f) in (None, ""))
    # worth_continuing 是布尔，False 也算「已填写」，只有缺键才算缺
    if "worth_continuing" in missing and "worth_continuing" in payload:
        missing = tuple(f for f in missing if f != "worth_continuing")
    if missing:
        readable = "；".join(REVERT_REQUIREMENTS.get(f, f) for f in missing)
        return GateResult(
            ok=False,
            gate="G1",
            message=f"回退信息不完整，请补齐后重新申请：{readable}",
            missing_fields=missing,
        )

    # 目标节点必须是允许的回退边
    allowed = revert_targets(current_node)
    if target not in allowed:
        return GateResult(
            ok=False,
            gate="G1",
            message=f"不允许从「{NODE_LABELS.get(current_node, current_node)}」回退到"
            f"「{NODE_LABELS.get(target, target)}」",
        )

    # G2：次数上限 —— 再看还退不退得动
    if retry_count >= MAX_RETRY_PER_ENTRY:
        return GateResult(
            ok=False,
            gate="G2",
            message=f"本节点已修复重试 {retry_count} 次，达到上限（{MAX_RETRY_PER_ENTRY} 次），"
            "需要人工介入",
        )
    if revisit_count >= MAX_REVISIT_PER_NODE:
        return GateResult(
            ok=False,
            gate="G2",
            message=f"本节点已被回退 {revisit_count} 次，达到上限（{MAX_REVISIT_PER_NODE} 次），"
            "需要人工介入",
        )
    if total_reverts >= MAX_TOTAL_REVERTS:
        return GateResult(
            ok=False,
            gate="G2",
            message=f"整条研究链已回退 {total_reverts} 次，达到上限（{MAX_TOTAL_REVERTS} 次），"
            "需要人工介入",
        )

    # G3：落痕由调用方无条件执行（这里只声明它属于闸门序列，不是可选项）
    return GateResult(ok=True)
