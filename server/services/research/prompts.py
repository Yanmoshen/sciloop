# Copyright 2026 SciLoop contributors
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
"""提示词渲染：契约（代码）+ 运行时上下文（数据库）+ 可选长文档（md）。

设计口径（「md 降为说明，代码管契约」）
------------------------------------
运行时提示词由三部分拼成，**优先级从高到低**：

1. **输出契约** —— 字段名、字面量取值、必填项，直接来自 ``contracts.py`` 的 schema。
2. **校验规则** —— 来自 ``rules.py`` 的 ``rule_catalog()``；明确告诉模型
   「交不上这些会被驳回」，避免模型猜规则。
3. **方法说明** —— ``NODE_GUIDES``（代码内的短说明）+ 可选的长文档
   （``prompts/*.md``）。

长文档是**补充素材，不是唯一来源**：容器内通常没有仓库根目录，
所以 ``_load_long_guide()`` 找不到就静默返回空串 —— 提示词质量不因此下降，
因为规则已经全部在代码里。这正是不把 md 当契约的原因。

运行时上下文全部来自数据库（研究问题、上游交接块、论文库实际可访问材料、
剩余重试与回退额度），模型**看不到**「清单里没有的东西」，也就无法声称访问过它。
"""

from __future__ import annotations

import logging
import os
from pathlib import Path
from typing import Any

from services.research import graph
from services.research.rules import MIN_EVIDENCE_COUNT, rule_catalog

logger = logging.getLogger("sciloop.research.prompts")

__all__ = ["NODE_GUIDES", "SYSTEM_PROMPT", "build_messages"]

#: 节点 → 长文档文件名（人类可读版在 prompts/）
_NODE_DOC_FILES: dict[str, str] = {
    "literature_review": "01-literature-review.md",
    "idea_and_feasibility": "02-idea-and-feasibility.md",
    "experiment_and_data_preparation": "03-experiment-and-data-preparation.md",
    "experiment_execution_and_retries": "04-experiment-execution-and-retries.md",
    "results_analysis": "05-results-analysis.md",
    "paper_writing": "06-paper-writing.md",
}

#: 代码内的短方法说明（长文档缺失时靠它）
NODE_GUIDES: dict[str, str] = {
    "literature_review": (
        "先按研究问题拆出检索词，在给出论文库材料范围内检索；\n"
        "每条证据必须能回到原文，二选一：\n"
        "  ① 填 card_field —— 只能填该论文「解析卡片」里**真实存在的字段名**；\n"
        "  ② 填 paper_span_id —— **只能从该论文「可引用的原文片段」列表里取**，不得自己编号；\n"
        "材料里没有可定位来源的论文，就不要拿它当证据（改写进 limits 说明缺什么）。\n"
        "必须给出最接近的工作，并同时写明重合点与仍存差异；\n"
        "没有查到合适论文时，如实写出覆盖范围与盲区，并给出后续检索建议——\n"
        "**不要把「没查到」写成「该方向不存在」**；\n"
        "同一篇论文的预印本与正式版视为一篇，不要重复计数。"
    ),
    "idea_and_feasibility": (
        "把兴趣转成可证伪假设，句式：在<限定条件>下，相比<明确基线>，"
        "通过<核心机制>，预期<主指标>发生<方向性变化>；若<反例条件>出现则被否定。\n"
        "可证伪条件必须带数值阈值或明确比较关系，不写「效果更好」这类无判据表述。\n"
        "效果大小没有文献或先导实验依据时写「待确定」，不承诺百分比提升。\n"
        "与最接近工作的差异必须逐条给出，重合与差异都要写。\n"
        "若相关工作仍不足，可填 revert_request 建议回到文献调研，"
        "并带齐：重合证据、仍存差异、是否值得继续。"
    ),
    "experiment_and_data_preparation": (
        "先写实验协议再看结果：分析单位、自变量、因变量、控制变量、混杂因素。\n"
        "数据集必须带版本与可定位来源；不可访问就如实写 access_ok=false，不要假设它能用。\n"
        "基线至少一条可运行；主指标要写公式、单位与方向（越大越好 / 越小越好）。\n"
        "成功判据与失败判据都要写——什么结果算假设被否定。\n"
        "必须有一次真实跑通的小规模预检记录（preflight.exit_code 必须为 0）；\n"
        "**只有计划、脚本或语法检查通过，不等于准备完成**。"
    ),
}

SYSTEM_PROMPT = (
    "你是 SciLoop 科研流程中的一个研究节点执行者。"
    "当前节点完全由程序控制：你只负责产出本节点要求的结构化成果，"
    "由程序校验后决定是否进入下一节点。\n\n"
    "硬性要求：\n"
    "1. 只输出一个 JSON 对象，不要输出任何解释文字、不要用代码围栏包裹。\n"
    "2. 只使用下文材料清单里真实存在的论文编号；**不得编造 paper_id**。\n"
    "3. 没查到的信息如实标注（例如 verification 填 inferred 或 limits 里说明），"
    "不要把不确定写成确定。\n"
    "4. 不得为了通过校验而虚构证据、结果或执行记录；"
    "**声明 completed 或 exit_code=0 必须与实际相符**。\n"
    "5. 负结果、混合结果和无法判定的结果都是有效产出，不需要包装成成功。"
)


def _candidate_dirs() -> list[Path]:
    dirs: list[Path] = []
    env = os.environ.get("RESEARCH_PROMPTS_DIR")
    if env:
        dirs.append(Path(env))
    here = Path(__file__).resolve()
    # backend/app/services/research/prompts.py → 上溯到仓库根
    for base in list(here.parents)[:6]:
        dirs.append(base / "prompts")
    return dirs


def _load_long_guide(node: str) -> str:
    """尝试加载人类可读的长文档；找不到返回空串（不报错、不降级提示词质量）。"""

    filename = _NODE_DOC_FILES.get(node)
    if not filename:
        return ""
    for directory in _candidate_dirs():
        path = directory / filename
        try:
            if path.is_file():
                return path.read_text(encoding="utf-8")
        except OSError as exc:  # pragma: no cover - 权限/编码异常
            logger.warning("读取长文档失败 %s：%s", path, exc)
    return ""


def _truncate(text: str, limit: int) -> str:
    text = text or ""
    return text if len(text) <= limit else text[:limit] + "\n…（已截断）"


def _format_contract(node: str) -> str:
    from services.research.contracts import NODE_OUTPUT_SCHEMAS

    schema = NODE_OUTPUT_SCHEMAS.get(node)
    if schema is None:
        return "（该节点首版为占位，按下方方法说明产出 JSON 对象即可）"
    import json

    return json.dumps(schema, ensure_ascii=False, indent=2)


def _format_rules(node: str) -> str:
    lines = []
    for item in rule_catalog(node):
        lines.append(f"- [{item['rule']}·{item['level']}] {item['message']}")
    if not lines:
        return "（该节点首版无强制校验规则）"
    extra = ""
    if node == "literature_review":
        extra = (
            f"\n注意：证据条数不足 {MIN_EVIDENCE_COUNT} 条、或某条证据没有填 card_field / "
            "paper_span_id，都会被驳回重跑。"
        )
    return "\n".join(lines) + extra


def _format_library(library: dict[str, Any]) -> str:
    if not library:
        return "论文库统计不可用。"
    lines = [
        f"- 库内论文总数：{library.get('paper_total', 0)}"
        f"（已解析 {library.get('paper_parsed', 0)}，未解析 {library.get('paper_unparsed', 0)}）",
        f"- 解析卡片：{library.get('card_count', 0)} 张；原文片段：{library.get('span_count', 0)} 段",
    ]
    if library.get("paper_unparsed"):
        lines.append(
            "- 注意：未解析的论文只能作为「仅摘要」材料使用，"
            "不得声称读过其全文，也不得编造其卡片字段。"
        )
    return "\n".join(lines)


def _format_hits(hits: list[dict[str, Any]]) -> str:
    if not hits:
        return "（按当前关键词在论文库中没有命中任何论文）"
    import json

    blocks: list[str] = []
    for hit in hits[:12]:
        card = hit.get("card")
        card_text = (
            json.dumps(card, ensure_ascii=False, indent=2) if card else "（未解析，无卡片字段）"
        )
        spans = hit.get("spans") or []
        if spans:
            span_lines = "\n".join(
                f"  - paper_span_id={s['paper_span_id']}"
                f"｜{s.get('section_name') or '未标注章节'}"
                f"｜第 {s.get('page_number') if s.get('page_number') is not None else '?'} 页"
                f"｜“{(s.get('quote_text') or '')[:120]}”"
                for s in spans
            )
        else:
            span_lines = "  （这篇没有可引用的原文片段）"
        blocks.append(
            f"### 论文 {hit['paper_id']}｜{hit.get('title') or '（无标题）'}\n"
            f"- 是否已解析：{hit.get('is_parsed')}（共 {hit.get('span_count', 0)} 段原文片段）\n"
            f"- 摘要节选：{_truncate(str(hit.get('abstract_excerpt') or ''), 400)}\n"
            f"- 解析卡片：\n{_truncate(card_text, 1200)}\n"
            f"- **可引用的原文片段**（`paper_span_id` 只能从这个列表里取）：\n{span_lines}"
        )
    return "\n\n".join(blocks)


def _format_upstream(upstream: dict[str, Any]) -> str:
    if not upstream:
        return "（无上游节点成果，本节点为起点）"
    import json

    return json.dumps(upstream, ensure_ascii=False, indent=2)[:6000]


def _format_budget(budget: dict[str, Any]) -> str:
    lines = [
        f"- 本节点本次进入内的修复重试：已用 {budget.get('retry_count', 0)} / "
        f"上限 {budget.get('max_retry', graph.MAX_RETRY_PER_ENTRY)}",
        f"- 本节点被回退次数：{budget.get('revisit_count', 0)} / "
        f"上限 {budget.get('max_revisit', graph.MAX_REVISIT_PER_NODE)}",
        f"- 整条链回退次数：{budget.get('total_reverts', 0)} / "
        f"上限 {budget.get('max_total_reverts', graph.MAX_TOTAL_REVERTS)}",
        f"- 已发生费用：{budget.get('cost_usd', 0)}（只记账，不拦截）",
    ]
    return "\n".join(lines)


def build_messages(
    *,
    node: str,
    user_text: str,
    research_question: str = "",
    project_name: str = "",
    upstream: dict[str, Any] | None = None,
    library: dict[str, Any] | None = None,
    hits: list[dict[str, Any]] | None = None,
    budget: dict[str, Any] | None = None,
    repair: str | None = None,
    include_long_guide: bool = True,
) -> list[dict[str, str]]:
    """渲染某一节点的执行提示词。"""

    label = graph.NODE_LABELS.get(node, node)
    parts: list[str] = [
        f"# 当前节点：{label}（{node}）",
        f"\n## 项目\n{project_name or '（未命名项目）'}",
        f"\n## 研究问题\n{research_question or '（尚未确定，可在本节点提出）'}",
        f"\n## 研究者本轮输入\n{user_text or '（无额外输入）'}",
        f"\n## 上游节点成果\n{_format_upstream(upstream or {})}",
        f"\n## 论文库实际可访问材料\n{_format_library(library or {})}",
        f"\n## 检索命中（仅限以下论文编号可被引用）\n{_format_hits(hits or [])}",
        f"\n## 当前额度\n{_format_budget(budget or {})}",
        f"\n## 输出契约（必须严格符合）\n```json\n{_format_contract(node)}\n```",
        f"\n## 会被校验的规则（不满足将被驳回重跑）\n{_format_rules(node)}",
        f"\n## 方法说明\n{NODE_GUIDES.get(node, '（无）')}",
    ]

    if repair:
        parts.append(f"\n## 上一轮未通过的原因（只需修正这些）\n{repair}")

    if include_long_guide:
        guide = _load_long_guide(node)
        if guide:
            parts.append(
                "\n## 附：该节点的完整方法文档（补充说明，与上面契约冲突时以契约为准）\n"
                + _truncate(guide, 12000)
            )

    parts.append("\n请只输出符合上述契约的 JSON 对象。")
    return [
        {"role": "system", "content": SYSTEM_PROMPT},
        {"role": "user", "content": "\n".join(parts)},
    ]
