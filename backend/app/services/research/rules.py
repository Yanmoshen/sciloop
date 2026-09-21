# Copyright 2026 SciLoop contributors
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
"""研究节点产出校验：R1–R14。

分级口径（与既有 ``StageError.level`` 一致）
------------------------------------------
``L1`` 格式 / 解析 / 外键类 —— **自动重跑**该节点，把缺项注入下一轮提示词。
``L2`` 质量 / 硬规则类 —— **驳回重跑**，同一节点最多 2 次；第 3 次置 ``waiting_human``。

明确不判什么
------------
本模块**不判断创新性是否成立、科学结论是否正确**。程序只查「字段齐不齐」与
「可核实的事实对不对」（例如引用的 ``paper_id`` 是否真的在库里）。学术质量由
模型交叉评审与研究者在本节点判断——把结构校验说成学术评审是虚假宣称。

校验器保持**纯函数**：需要的事实（合法 ``paper_id`` 集合）由调用方查好后传入，
便于单测与复用。
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

from app.services.research.contracts import (
    NODE_OUTPUT_MODELS,
    ExperimentPrepOutput,
    IdeaAndFeasibilityOutput,
    LiteratureReviewOutput,
)

__all__ = [
    "MIN_EVIDENCE_COUNT",
    "LibraryFacts",
    "RuleHit",
    "ValidationResult",
    "min_evidence_count",
    "rule_catalog",
    "validate_node_output",
]

#: 「证据充分」阈值：≥3 条且均带可定位来源，其中至少 1 条是最接近工作
MIN_EVIDENCE_COUNT = 3

#: 解析卡片的 8 个字段名（`card_field` 只允许取这些值）
CARD_FIELDS: tuple[str, ...] = (
    "research_problem",
    "core_method",
    "key_innovation",
    "technical_route",
    "experimental_setup",
    "main_conclusions",
    "limitations",
    "transferable",
)


@dataclass
class LibraryFacts:
    """校验器需要的事实（由调用方查库后传入，校验器保持纯函数）。

    ``locators_known`` 的意义：只有当调用方**真的把卡片字段与原文片段查出来**时，
    才能核对「这条证据指的地方存在吗」。单测里只给编号时该标志为 False，
    只做编号存在性检查。生产路径必须传全量（``store.library_facts``）。

    为什么必须有这一项：只检查「card_field 这个字段名填了没有」，模型随手写一个
    ``core_method`` 就能过——而那张卡片可能根本不存在。这样证据链看着齐全，
    实际指向空处，正是本项目最不能出的问题。
    """

    paper_ids: set[int] = field(default_factory=set)
    card_fields: dict[int, frozenset[str]] = field(default_factory=dict)
    span_ids: dict[int, frozenset[int]] = field(default_factory=dict)
    locators_known: bool = False

    def has_card_field(self, paper_id: int, name: str) -> bool:
        return name in self.card_fields.get(paper_id, frozenset())

    def has_span(self, paper_id: int, span_id: int) -> bool:
        return span_id in self.span_ids.get(paper_id, frozenset())

#: 判据标记：可证伪条件必须带数值阈值或**明确比较关系**。
#: 注意这里**不包含**「若 / 如果 / 当」——那些只是条件连词，不构成判据：
#: 「如果效果不好就不成立」有连词、没判据，等同于没有可证伪条件。
_CRITERION_MARKERS = (
    "低于",
    "高于",
    "超过",
    "不超过",
    "小于",
    "大于",
    "提升",
    "下降",
    "减少",
    "增加",
    "百分",
    "%",
    "≥",
    "≤",
    ">",
    "<",
    "=",
)


@dataclass(frozen=True)
class RuleHit:
    """一条命中的规则。``message`` 供研究者阅读，``rule`` 只在日志/技术详情出现。"""

    rule: str
    level: str
    message: str
    path: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return {"rule": self.rule, "level": self.level, "message": self.message, "path": self.path}


@dataclass
class ValidationResult:
    """校验结果。``ok=True`` 时 ``hits`` 必为空。"""

    ok: bool
    level: str | None = None
    hits: list[RuleHit] = field(default_factory=list)

    @property
    def failed_rules(self) -> list[str]:
        return [h.rule for h in self.hits]

    def to_dict(self) -> dict[str, Any]:
        return {
            "ok": self.ok,
            "level": self.level,
            "rules": self.failed_rules,
            "items": [h.to_dict() for h in self.hits],
        }

    def repair_instruction(self) -> str:
        """给模型的修复指令：只说缺什么，不说废话。"""

        lines = [f"- [{h.rule}] {h.message}" for h in self.hits]
        return "上一轮产出未通过校验，请仅修正以下问题后重新提交完整 JSON：\n" + "\n".join(lines)


def _blank(value: Any) -> bool:
    return value is None or (isinstance(value, str) and not value.strip())


def _has_locator(item: Any) -> bool:
    """证据是否带可定位来源：必须有 card_field 或 paper_span_id。

    只有 ``quote_text`` 不算——引文没有稳定锚点（换版本就漂），无法回到原文核对。
    """

    return not _blank(getattr(item, "card_field", None)) or getattr(item, "paper_span_id", None)


def _has_criterion(text: str) -> bool:
    if _blank(text):
        return False
    if any(ch.isdigit() for ch in text):
        return True
    return any(marker in text for marker in _CRITERION_MARKERS)


# --------------------------------------------------------------------------- #
# 各节点规则
# --------------------------------------------------------------------------- #
def _check_literature(
    out: LiteratureReviewOutput, facts: LibraryFacts | None
) -> list[RuleHit]:
    hits: list[RuleHit] = []

    if _blank(out.research_question):
        hits.append(RuleHit("R1", "L1", "研究问题为空，无法界定调研范围", "research_question"))

    if len(out.evidence) < MIN_EVIDENCE_COUNT:
        hits.append(
            RuleHit(
                "R2",
                "L2",
                f"证据只有 {len(out.evidence)} 条，至少需要 {MIN_EVIDENCE_COUNT} 条",
                "evidence",
            )
        )
    else:
        missing = [
            f"第 {i + 1} 条（论文 {e.paper_id}）"
            for i, e in enumerate(out.evidence)
            if not _has_locator(e)
        ]
        if missing:
            hits.append(
                RuleHit(
                    "R2",
                    "L2",
                    "以下证据缺少可定位来源（需填写解析卡片字段或原文片段）：" + "、".join(missing),
                    "evidence",
                )
            )

    hits.extend(_check_locators_real(out.evidence, facts))

    if not out.closest_work:
        hits.append(RuleHit("R3", "L2", "未给出最接近的工作，无法判断差异与空白", "closest_work"))
    else:
        bad = [
            f"论文 {c.paper_id}"
            for c in out.closest_work
            if _blank(c.overlap) or _blank(c.remaining_difference)
        ]
        if bad:
            hits.append(
                RuleHit(
                    "R3",
                    "L2",
                    "最接近工作必须同时说明重合点与仍存差异：" + "、".join(bad),
                    "closest_work",
                )
            )

    if out.evidence and all(e.verification == "inferred" for e in out.evidence):
        hits.append(
            RuleHit("R4", "L2", "全部证据都标为「推断」，没有任何已核验证据", "evidence")
        )

    if out.gaps and _blank(out.coverage_note):
        hits.append(
            RuleHit(
                "R5",
                "L2",
                "提到了研究空白，但没有说明检索覆盖范围与盲区（没查到不等于不存在）",
                "coverage_note",
            )
        )

    hits.extend(_check_paper_refs(out, facts))
    return hits


def _check_locators_real(evidence: list[Any], facts: LibraryFacts | None) -> list[RuleHit]:
    """R2 的加强项：声称的卡片字段 / 原文片段必须**真实存在**。

    只在调用方查过库时执行（``locators_known``）；否则会误伤「库信息未知」的场景。
    这一条拦的是「字段名填得像真的、但那张卡片根本不存在」——编造可定位来源。
    """

    if facts is None or not facts.locators_known:
        return []

    unknown_field: list[str] = []
    unknown_span: list[str] = []

    for i, e in enumerate(evidence):
        paper_id = getattr(e, "paper_id", None)
        name = getattr(e, "card_field", None)
        span_id = getattr(e, "paper_span_id", None)
        if name and not facts.has_card_field(int(paper_id), str(name)):
            unknown_field.append(f"第 {i + 1} 条（论文 {paper_id} 的 {name}）")
        if span_id is not None and not facts.has_span(int(paper_id), int(span_id)):
            unknown_span.append(f"第 {i + 1} 条（论文 {paper_id} 的片段 {span_id}）")

    hits: list[RuleHit] = []
    if unknown_field:
        hits.append(
            RuleHit(
                "R2",
                "L2",
                "以下证据指向的解析卡片字段在论文库中不存在或为空（疑似编造来源）："
                + "、".join(unknown_field),
                "evidence",
            )
        )
    if unknown_span:
        hits.append(
            RuleHit(
                "R2",
                "L2",
                "以下证据引用的原文片段不属于该论文（疑似编造来源）：" + "、".join(unknown_span),
                "evidence",
            )
        )
    return hits


def _check_paper_refs(out: Any, facts: LibraryFacts | None) -> list[RuleHit]:
    """R6：所有引用的论文必须真实存在于论文库。"""

    if facts is None or not facts.paper_ids:
        return []
    paper_ids = facts.paper_ids
    referenced: list[tuple[str, int]] = []
    for i, e in enumerate(getattr(out, "evidence", []) or []):
        referenced.append((f"evidence[{i}]", e.paper_id))
    for i, c in enumerate(getattr(out, "closest_work", []) or []):
        referenced.append((f"closest_work[{i}]", c.paper_id))
    for i, d in enumerate(getattr(out, "novelty_delta", []) or []):
        referenced.append((f"novelty_delta[{i}]", d.paper_id))
    for i, g in enumerate(getattr(out, "gaps", []) or []):
        for pid in getattr(g, "raised_by_paper_ids", []) or []:
            referenced.append((f"gaps[{i}]", pid))

    unknown = [f"{path} 引用论文 {pid}" for path, pid in referenced if pid not in paper_ids]
    if not unknown:
        return []
    return [
        RuleHit(
            "R6",
            "L1",
            "引用了论文库中不存在的论文（可能是编造的编号）：" + "；".join(unknown[:5]),
            "paper_id",
        )
    ]


def _check_idea(out: IdeaAndFeasibilityOutput, facts: LibraryFacts | None) -> list[RuleHit]:
    hits: list[RuleHit] = []

    condition = out.hypothesis.falsification_condition
    if _blank(condition):
        hits.append(
            RuleHit("R7", "L2", "假设缺少可证伪条件，无法判断什么结果算被否定", "hypothesis")
        )
    elif not _has_criterion(condition):
        hits.append(
            RuleHit(
                "R7",
                "L2",
                "可证伪条件缺少可检验判据（需要数值阈值或明确比较关系）："
                f"“{condition[:40]}”",
                "hypothesis.falsification_condition",
            )
        )

    if not out.novelty_delta:
        hits.append(
            RuleHit("R8", "L2", "未给出与最接近工作的差异，新颖性无法成立", "novelty_delta")
        )
    else:
        bad = [
            f"论文 {n.paper_id}"
            for n in out.novelty_delta
            if _blank(n.overlap) or _blank(n.difference)
        ]
        if bad:
            hits.append(
                RuleHit(
                    "R8",
                    "L2",
                    "差异说明不完整（需同时写重合与差异）：" + "、".join(bad),
                    "novelty_delta",
                )
            )

    hits.extend(_check_paper_refs(out, facts))
    return hits


def _check_experiment_prep(
    out: ExperimentPrepOutput, facts: LibraryFacts | None
) -> list[RuleHit]:
    hits: list[RuleHit] = []

    if not out.datasets:
        hits.append(RuleHit("R9", "L2", "未列出任何数据集", "datasets"))
    else:
        bad = [
            f"数据集 {d.name or '（未命名）'}"
            for d in out.datasets
            if _blank(d.version) or not d.access_ok
        ]
        if bad:
            hits.append(
                RuleHit(
                    "R9",
                    "L2",
                    "以下数据集缺少版本或当前不可访问（不可访问必须如实标注）：" + "、".join(bad),
                    "datasets",
                )
            )

    if not out.baselines:
        hits.append(RuleHit("R10", "L2", "未列出任何基线", "baselines"))
    elif not any(b.runnable for b in out.baselines):
        hits.append(
            RuleHit("R10", "L2", "没有任何一条基线是可运行的，无法形成比较", "baselines")
        )

    if not out.metrics:
        hits.append(RuleHit("R11", "L2", "未定义任何指标", "metrics"))
    else:
        primary = [m for m in out.metrics if m.is_primary] or out.metrics[:1]
        bad = [f"指标 {m.name or '（未命名）'}" for m in primary if not m.unit or not m.direction]
        if bad:
            hits.append(
                RuleHit(
                    "R11",
                    "L2",
                    "主指标必须写明单位与方向（越大越好 / 越小越好）：" + "、".join(bad),
                    "metrics",
                )
            )

    if _blank(out.falsification.success_criteria) or _blank(out.falsification.failure_criteria):
        hits.append(
            RuleHit(
                "R12",
                "L2",
                "缺少成功判据或失败判据，实验无法判定完成",
                "falsification",
            )
        )

    if _blank(out.resources.compute_budget):
        hits.append(RuleHit("R14", "L2", "未给出计算或费用预算", "resources"))

    if out.preflight.exit_code != 0:
        hits.append(
            RuleHit(
                "R13",
                "L2",
                "小规模预检未通过（退出码 "
                f"{out.preflight.exit_code}），准备阶段不能判定完成",
                "preflight",
            )
        )

    hits.extend(_check_paper_refs(out, facts))
    return hits


_CHECKERS: dict[str, Callable[[Any, LibraryFacts | None], list[RuleHit]]] = {
    "literature_review": _check_literature,
    "idea_and_feasibility": _check_idea,
    "experiment_and_data_preparation": _check_experiment_prep,
}


def min_evidence_count(node: str) -> int:
    """该节点的证据条数阈值（非文献节点为 0）。"""

    return MIN_EVIDENCE_COUNT if node == "literature_review" else 0


def rule_catalog(node: str) -> list[dict[str, str]]:
    """给提示词用：列出该节点会被校验的规则（规则号 + 级别 + 人话）。"""

    catalog = {
        "literature_review": [
            ("R1", "格式", "研究问题不能为空"),
            ("R2", "质量", f"证据至少 {MIN_EVIDENCE_COUNT} 条，且每条都要带解析卡片字段或原文片段"),
            ("R3", "质量", "至少 1 条最接近的工作，且同时说明重合点与仍存差异"),
            ("R4", "质量", "证据不能全部标为「推断」"),
            ("R5", "质量", "提到研究空白时必须说明检索覆盖范围与盲区"),
            ("R6", "格式", "引用的论文必须真实存在于论文库，不得编造编号"),
        ],
        "idea_and_feasibility": [
            ("R7", "质量", "必须有可证伪条件，且含数值阈值或明确比较关系"),
            ("R8", "质量", "至少 1 条与最接近工作的差异，需同时写重合与差异"),
            ("R6", "格式", "引用的论文必须真实存在于论文库"),
        ],
        "experiment_and_data_preparation": [
            ("R9", "质量", "每个数据集都要带版本且可访问"),
            ("R10", "质量", "至少 1 条可运行的基线"),
            ("R11", "质量", "主指标必须写单位与方向"),
            ("R12", "质量", "必须同时给出成功判据与失败判据"),
            ("R13", "质量", "小规模预检必须真实跑通（退出码 0）"),
            ("R14", "质量", "必须给出预算"),
        ],
    }
    return [
        {"rule": rid, "level": level, "message": msg}
        for rid, level, msg in catalog.get(node, [])
    ]


def validate_node_output(
    node: str,
    payload: dict[str, Any] | None,
    *,
    facts: LibraryFacts | None = None,
    paper_ids: set[int] | None = None,
) -> ValidationResult:
    """校验某节点的产出。

    ``facts`` 由调用方查库后传入（``store.library_facts``），用于核对
    「引用的论文 / 卡片字段 / 原文片段是否真实存在」。
    ``paper_ids`` 是只给编号的简写（单测用），此时不核对卡片与片段。
    两者都不传表示跳过外键类检查。
    """

    if facts is None and paper_ids:
        facts = LibraryFacts(paper_ids=set(paper_ids), locators_known=False)

    model_cls = NODE_OUTPUT_MODELS.get(node)
    if model_cls is None:
        # 占位节点（首版未实装校验）：放行，但不假装校验过
        return ValidationResult(ok=True)

    if not isinstance(payload, dict):
        return ValidationResult(
            ok=False,
            level="L1",
            hits=[RuleHit("R1", "L1", "产出不是可解析的 JSON 对象")],
        )

    try:
        out = model_cls.model_validate(payload)
    except Exception as exc:  # noqa: BLE001 - 形状不对一律 L1，交给重跑修复
        return ValidationResult(
            ok=False,
            level="L1",
            hits=[RuleHit("R1", "L1", f"产出结构不符合契约：{str(exc)[:240]}")],
        )

    checker = _CHECKERS.get(node)
    hits = checker(out, facts) if checker else []
    if not hits:
        return ValidationResult(ok=True)

    level = "L1" if any(h.level == "L1" for h in hits) else "L2"
    return ValidationResult(ok=False, level=level, hits=hits)
