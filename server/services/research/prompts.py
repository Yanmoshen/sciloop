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

import json
import logging
import os
from pathlib import Path
from typing import Any

from services.research import graph
from services.research.rules import MIN_EVIDENCE_COUNT, rule_catalog
from services.system_prompt import SCILOOP_SYSTEM_PROMPT

logger = logging.getLogger("sciloop.research.prompts")

__all__ = ["NODE_GUIDES", "SELF_CHECK_SYSTEM", "SYSTEM_PROMPT", "build_messages", "build_self_check_messages"]

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
    "experiment_execution_and_retries": (
        "这一站要**真的跑**，不是复述计划：\n"
        "能跑的命令写进 commands_to_run，程序会替你跑（**只有开了「完全访问模式」的对话才真跑**，"
        "没开时它会如实告诉研究者，并把命令摆给他）。\n"
        "跑完的每一次都要在 runs 里如实记：命令原文、status、退出码、输出末尾几行。\n"
        "**没跑就写 skipped 并说明为什么** —— 把「计划要跑」写成「跑成功了」是本节点最严重的错误。\n"
        "失败要写进 failures_and_fixes（失败原因 + 你怎么处置的），一次没成就改参数再来，"
        "改了什么也要写下来；始终跑不通就如实说跑不通，别编结果。\n"
        "跑出来的产物路径写 artifacts，没做到的写 limits。"
    ),
    "results_analysis": (
        "只根据**真的跑出来的东西**下结论：每条 finding 都要在 based_on 里写清依据"
        "（哪次运行 / 哪条证据），让人能回查。\n"
        "支持、反对、无法判定都是有效结论——**负结果和无法判定必须如实写**，不要为了好看往支持上靠。\n"
        "与预期不符的地方写进 deviations（这往往是最有信息量的部分）。\n"
        "结论的适用条件与不确定性写进 caveat / limitations；下一步建议写 next_steps。\n"
        "样本量、重复次数不足以支撑结论时，confidence 写 low 并说明。"
    ),
    "paper_writing": (
        "把已验收的上游成果写成结构完整、核心正文完成的完整论文初稿，不是提纲或短稿。\n"
        "遵循共享指令的论文写作技能与交付标准，使用实际可用的技能和产物工具。\n"
        "默认产物为 LaTeX 源文件、BibTeX 及可编译时生成并检查的 PDF；"
        "没有编译环境时如实说明未验证项，不宣称已生成 PDF。\n"
        "本节点的现有 JSON 契约仍以 content_md 承载完整可读正文；"
        "不得自造产物字段，真实文件位置按现有契约记录，无法交付源文件包时说明限制。\n"
        "正文成段成节，sections 给出章节标题；引用用 [#编号]，仅用材料中真实的 paper_id。\n"
        "全文实际写出的事实性主张逐条列入 claims，供评审核验，不另造主张。\n"
        "缺口如实写入 open_items / limitations，不将完整初稿称为已可投稿。\n"
        "content_md 中不要出现裸换行；换行、引号和反斜杠按 JSON 标准转义；不自行设置短稿字数上限。\n"
        "关键证据或实验缺口列出影响、补齐方案和回退目标，等待用户确认；"
        "缺口未补齐或运行时无法承载完整交付时，不得声明写作完成。"
    ),
    "paper_review": (
        "逐条核对写作站给出的主张：证据支持就写 supported，被证据反驳就写 contradicted，"
        "证据不足或找不到来源就写 insufficient——**insufficient 是完全正常的结论，不要为了好看往 supported 靠**。\n"
        "每条判定都要写 status_reason（能追到具体来源或说明为什么找不到），并填 evidence_count。\n"
        "整体评估写 overall：哪里站得住、哪里站不住；必须改的写 required_revisions，没有就留空。\n"
        "本次评审没覆盖到的范围写 limits（例如「只核对了库内证据，没核对最新外部进展」）。"
    ),
}

SYSTEM_PROMPT = SCILOOP_SYSTEM_PROMPT + "\n\n" + (
    "## 当前研究节点的执行契约\n"
    "本次只执行运行时指定的当前节点。只输出一个 JSON 对象，并确保它符合下方 schema，"
    "不要解释文字或代码围栏。对用户的展示由对话层处理，不能改用自然语言代替 JSON。\n"
    "用 state 声明本节点的工作状态：\n"
    "- state=done：本节点的产出达到完成标准；不表示用户已经验收或允许推进下一节点。\n"
    "- state=continue：还有可自主完成的工作，将缺项写入 pending。\n"
    "- state=need_human：需要用户决定、授权或提供材料，用 state_reason 说明具体需要。\n"
    "程序反馈的验收情况是事实；据此核对完成标准，不能忽略关键缺口而宣称完成。\n"
    "材料不足也可以 done，但仅限已完成的诚实阶段调查：写清范围、盲区和缺口；"
    "论文写作等依赖关键证据或实验结果的节点，关键缺口未补齐不得 done。\n"
    "绝不为了凑数而编造：仅引用实际提供的 paper_id、原文片段和卡片字段，"
    "模型已有知识必须标记未经文献核验，不能充当实测结果或核验引用。\n"
    "同一来源连续两次无结果或超时，应换来源或检索词；"
    "没查到不等于方向不存在，先完成可做的检索与分析。\n"
    "联网检索可通过 academic_queries 和 web_queries 请求，"
    "程序执行后在下一轮提供真实结果；请求已提交不等于已经获得证据。\n"
    "宽泛主题先提出有依据的推荐切口；未经授权不能替用户锁定方向。\n"
    "命令和技能仅使用运行时实际提供的工具或契约字段，不能假装调用成功。\n"
    "负结果、混合结果和无法判定都是有效成果；state 必须与实际工作和缺口一致。"
)

#: 同模型自检（先出结论，再让同一个模型回头自查一遍）用的系统提示。
SELF_CHECK_SYSTEM = (
    "你是刚才产出这份成果的同一个模型。现在请**回头检查自己刚交的东西**，不要重写、不要客套。\n\n"
    "检查三件事：\n"
    "1. 契约要求的字段是不是真的都填了、填的是不是真事实（有没有编造论文编号或结果）；\n"
    "2. 程序给出的「本轮验收情况」里那些缺项，是不是真的无法在本节点内解决"
    "（如果你其实还能自己再做一步，就不算无法解决）；\n"
    "3. 有没有明显漏掉的东西（该说明的缺口没说、该给的建议没给）。\n\n"
    "给出你的判断：state=done（确实完成）/ continue（还有你该做的没做）/ "
    "need_human（缺只有研究者能提供的东西）。\n"
    "只输出一个 JSON 对象，含 state、summary（一句话）、issues（逐条列出问题，没有就空数组）。"
)


def _candidate_dirs() -> list[Path]:
    dirs: list[Path] = []
    env = os.environ.get("RESEARCH_PROMPTS_DIR")
    if env:
        dirs.append(Path(env))
    here = Path(__file__).resolve()
    # server/services/research/prompts.py → 逐级上溯找 prompts/（抗目录深度变化）
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
    """给模型看的"进展与花费"。

    ⚠️ **不要再写"修复重试上限"**（2026-09-22）：节点重试已经不设上限、由模型决定何时停，
    再摆一个"上限 2"给它，它会据此判断"我只能再试一次" —— 实测它真的在结论里写了
    「本次修复重试已用 1/2」，等于程序用一个不存在的规则左右了它的决定。
    回退次数上限仍然保留（那是结构性预算，由 `graph.check_revert_gate` 真的在拦）。
    """

    lines = [
        f"- 本节点本次进入内：已进行 {budget.get('retry_count', 0)} 轮修复"
        "（**没有次数上限**：做没做完由你判断，需要停就说 need_human）",
        f"- 本节点被回退次数：{budget.get('revisit_count', 0)} / "
        f"上限 {budget.get('max_revisit', graph.MAX_REVISIT_PER_NODE)}",
        f"- 整条链回退次数：{budget.get('total_reverts', 0)} / "
        f"上限 {budget.get('max_total_reverts', graph.MAX_TOTAL_REVERTS)}",
        f"- 已发生费用：{budget.get('cost_usd', 0)}（只记账，不拦截）",
    ]
    # 检索连续空手时**如实说清事实**（2026-09-26）：仍然不给"重试上限"这个伪规则，
    # 但要把"同样的检索已经连续 N 轮没命中"摆到它面前，让它自己决定换关键词/换来源，
    # 还是直接转人工说明缺什么。（极端情况下另有程序防呆线兜底，见 orchestrator。）
    zero_hits = int(budget.get("zero_hit_streak") or 0)
    if zero_hits > 0:
        lines.append(
            f"- **最近连续 {zero_hits} 轮检索都没有命中任何材料**："
            "沿用同样的检索词大概率还是空手；请换关键词、换来源，"
            "或直接转人工说明需要研究者补什么。"
        )
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
    search_results: list[dict[str, Any]] | None = None,
    command_results: list[dict[str, Any]] | None = None,
    research_facts: dict[str, Any] | None = None,
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
        f"\n## 程序会如实报出的验收情况（**不是判决**，是事实；做没做完由你判断）\n{_format_rules(node)}",
        f"\n## 方法说明\n{NODE_GUIDES.get(node, '（无）')}",
    ]

    if repair:
        parts.append(f"\n## 上一轮未通过的原因（只需修正这些）\n{repair}")

    if search_results:
        parts.append(f"\n## 联网检索结果（你上一轮要求查的）\n{_format_search(search_results)}")

    skills_block = _skills_block()
    if skills_block:
        parts.append(skills_block)

    if command_results:
        parts.append(
            f"\n## 你上一轮要求跑的命令与真实输出\n{_format_commands(command_results)}"
        )

    if research_facts:
        confirmed_scope = str(research_facts.get("confirmed_scope") or "").strip()
        confirmed_question = str(research_facts.get("research_question") or "").strip()
        prior = research_facts.get("facts") or []
        lines = []
        if confirmed_scope:
            lines.append(f"- 已确认研究范围：{confirmed_scope[:1800]}")
        if confirmed_question:
            lines.append(f"- 已确认研究问题：{confirmed_question[:1800]}")
        for item in prior[-8:]:
            if not isinstance(item, dict):
                continue
            role = str(item.get("role") or "")
            content = str(item.get("content") or "").strip()
            if role and content:
                lines.append(f"- {role}：{content[:1800]}")
        if lines:
            parts.append(
                "\n## 同一研究回合已确认事实（持久化输入，勿重复索要）\n"
                + "\n".join(lines)
            )

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


#: 能力 → 给人看/给模型看的名字
_CAPABILITY_LABELS = {"academic": "查学术", "web": "搜网页"}


#: 技能清单（两级披露第一级）：只给名字 + 一句话，正文要模型自己 load_skill
_SKILLS_HEADER = (
    "\n## 你可以调用的技能（先看名字判断要不要用，要用哪个再 load_skill 看全文）\n"
)


def _skills_block() -> str:
    """技能清单（只给名字 + 一句话 + 环节）；读不出来就返回空串，绝不影响出题。"""

    from services.skills import service

    try:
        items = service.prompt_catalog()
    except Exception:  # noqa: BLE001
        return ""
    if not items:
        return ""
    by_stage: dict[str, list[dict[str, str]]] = {}
    for item in items:
        by_stage.setdefault(item["stage"], []).append(item)
    lines = [_SKILLS_HEADER.rstrip()]
    for stage, stage_items in by_stage.items():
        lines.append(f"### {stage}")
        for item in stage_items:
            lines.append(f"- {item['name']}：{item['description']}")
    return "\n".join(lines)


def _format_search(blocks: list[dict[str, Any]]) -> str:
    """把联网检索结果排成人能读、模型也好用的清单。

    ⚠️ 只说事实：**哪个能力、哪个来源、搜索词、标题 / 链接 / 摘要原文**。
    不要替模型总结、不要替它判断相关性 —— 那正是"程序替模型做判断"的老毛病。
    """

    lines: list[str] = []
    for block in blocks:
        capability = _CAPABILITY_LABELS.get(str(block.get("capability") or ""), "联网检索")
        query = str(block.get("query") or "")
        results = block.get("results") or []
        sources = "、".join(str(item) for item in (block.get("sources_used") or []))
        head = f"### {capability}｜搜索词：{query}（{len(results)} 条"
        head += f"；来源：{sources}）" if sources else "）"
        lines.append(head)
        if not results:
            failed = "、".join(
                f"{item.get('source')}" for item in (block.get("sources_failed") or [])
            )
            lines.append(f"（没有拿到结果{f'；未响应的来源：{failed}' if failed else ''}）")
        for index, item in enumerate(results, start=1):
            title = str(item.get("title") or "").strip() or "（无标题）"
            url = str(item.get("url") or "").strip()
            snippet = str(item.get("snippet") or "").strip()
            source = str(item.get("source") or "").strip()
            tag = f"［{source}］" if source else ""
            lines.append(f"{index}. {tag}{title}\n   {url}\n   {snippet}")
        lines.append("")
    lines.append(
        "（以上是题录或网页摘要，不是论文全文；要素材就打开原始链接核对，或说明还缺什么。）"
    )
    return "\n".join(lines)


def _format_commands(records: list[dict[str, Any]]) -> str:
    """模型点名的命令跑成什么样，原样摆出来。

    ⚠️ 只说事实：命令、退出码、输出末尾几行、拒跑的原因。
    **不要替它解释输出是什么意思、也不要替它判断这算成功还是失败**。
    """

    lines: list[str] = []
    for item in records:
        command = str(item.get("command") or "")
        if item.get("refused"):
            lines.append(f"- `{command}` → **没有执行**：{item.get('error')}")
            continue
        if item.get("needs_approval"):
            lines.append(f"- `{command}` → **没有执行**：{item.get('error')}")
            continue
        code = item.get("exit_code")
        lines.append(f"- `{command}` → 退出码 {code}" if code is not None else f"- `{command}` → 没跑起来")
        if item.get("reason") == "empty_output" or item.get("stdout_empty"):
            lines.append("    标记：empty_output（退出成功但标准输出为空，不构成证据）")
        elif item.get("reason"):
            lines.append(f"    错误类别：{item['reason']}")
        if item.get("error"):
            lines.append(f"    错误：{item['error']}")
        stdout_tail = str(item.get("stdout_tail") or "").strip()
        if stdout_tail:
            lines.append("    输出末尾：\n" + "\n".join("      " + row for row in stdout_tail.splitlines()[-12:]))
        stderr_tail = str(item.get("stderr_tail") or "").strip()
        if stderr_tail:
            lines.append("    错误输出末尾：\n" + "\n".join("      " + row for row in stderr_tail.splitlines()[-8:]))
    lines.append("（以上是命令的真实输出，截取末尾若干行；请如实写进 runs，不要改写。）")
    return "\n".join(lines)


def build_self_check_messages(
    *,
    node: str,
    payload: dict[str, Any],
    research_question: str = "",
    advisories: list[str] | None = None,
    library: dict[str, Any] | None = None,
) -> list[dict[str, str]]:
    """同模型自检：把刚交的产出和程序看到的事实原样交回给**同一个模型**，让它回头自查。

    为什么值得多花这一次调用：模型在"刚写完"的状态下容易收工了事；
    让它换一个"审查者"视角看同一份东西，能抓到漏填、编造、漏说明的缺口 ——
    而**判定权仍在模型手里**（自检说 done 才算完，说 continue 就接着做）。
    """

    label = graph.NODE_LABELS.get(node, node)
    facts = advisories or []
    parts = [
        f"# 你刚交出的成果（节点：{label}）",
        f"\n## 研究问题\n{research_question or '（未确定）'}",
        f"\n## 论文库实际可访问材料\n{_format_library(library or {})}",
        "\n## 你交出的 JSON",
        "```json\n" + _truncate(json.dumps(payload, ensure_ascii=False, indent=2), 12000) + "\n```",
        "\n## 程序本轮如实报出的验收情况（事实，不是判决）",
        ("\n".join(facts) if facts else "（没有报出缺项）"),
        "\n请回头检查上面这份成果，并给出你的判断（state / summary / issues）。",
    ]
    return [
        {"role": "system", "content": SELF_CHECK_SYSTEM},
        {"role": "user", "content": "\n".join(parts)},
    ]
