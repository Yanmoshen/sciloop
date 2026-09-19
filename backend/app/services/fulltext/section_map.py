# Copyright 2026 SciLoop contributors
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
"""章节名归一（WP05-T2）。

把 HTML/PDF 里千奇百怪的章节标题归一到 7 个受控值：

``abstract`` / ``introduction`` / ``method`` / ``experiment`` /
``conclusion`` / ``related_work`` / ``other``

归一结果写入 ``paper_spans.section_name``，因此必须是**确定性纯函数**：
同一输入永远得到同一输出，且不依赖任何外部状态。
"""

from __future__ import annotations

import re
import unicodedata

#: 受控章节名（paper_spans.section_name 的取值域）
SECTION_NAMES: tuple[str, ...] = (
    "abstract",
    "introduction",
    "method",
    "experiment",
    "conclusion",
    "related_work",
    "other",
)

#: 关键词 -> 受控章节名。顺序即优先级，先命中先返回。
_KEYWORD_RULES: tuple[tuple[str, tuple[str, ...]], ...] = (
    (
        "abstract",
        ("abstract", "摘要", "summary"),
    ),
    (
        "related_work",
        (
            "related work",
            "related works",
            "prior work",
            "previous work",
            "literature review",
            "background and related",
            "related literature",
        ),
    ),
    (
        "conclusion",
        (
            "conclusion",
            "conclusions",
            "concluding remarks",
            "concluding",
            "conclusion and future work",
            "discussion and conclusion",
            "discussion",
            "future work",
            "limitations",
            "summary and outlook",
        ),
    ),
    (
        "experiment",
        (
            "experiment",
            "experiments",
            "experimental",
            "experimental setup",
            "experimental results",
            "evaluation",
            "evaluations",
            "empirical",
            "empirical study",
            "ablation",
            "ablations",
            "ablation study",
            "results",
            "results and analysis",
            "analysis",
            "benchmark",
            "benchmarks",
            "case study",
            "case studies",
            "study",
            "findings",
        ),
    ),
    (
        "method",
        (
            "method",
            "methods",
            "methodology",
            "approach",
            "approaches",
            "our approach",
            "proposed method",
            "proposed approach",
            "proposed framework",
            "proposed model",
            "model",
            "models",
            "framework",
            "architecture",
            "preliminaries",
            "preliminary",
            "problem formulation",
            "problem definition",
            "problem statement",
            "system design",
            "algorithm",
            "algorithms",
            "technical approach",
            "the proposed",
            "overview",
        ),
    ),
    (
        "introduction",
        (
            "introduction",
            "motivation",
            "introduction and motivation",
        ),
    ),
)

#: 章节标题前缀编号：``1`` / ``1.`` / ``2.3`` / ``III.`` / ``A.``
_NUMBER_PREFIX_RE = re.compile(
    r"^\s*(?:\d{1,2}(?:\.\d{1,2})*|[IVXLC]{1,5}|[A-H])?\s*[.、)]?\s*$"
)

#: 判定"看起来像章节标题"的正则（PDF 解析用它从文本块里挑标题）
NUMBERED_HEADING_RE = re.compile(
    r"^\s*(?:\d{1,2}(?:\.\d{1,2}){0,2}\.?|[IVXLC]{1,5}\.|[A-H]\.)\s+\S{2,80}$"
)

_WHITESPACE_RE = re.compile(r"\s+")


def _clean(heading: str) -> str:
    """去编号、去标点、折叠空白、转小写，供关键词匹配。"""
    text = unicodedata.normalize("NFKC", heading or "")
    text = _WHITESPACE_RE.sub(" ", text).strip()
    # 去掉行首编号（1 / 1. / 2.3 / III. / A.）
    text = re.sub(r"^\s*(?:\d{1,2}(?:\.\d{1,2}){0,2}\.?\s+|[IVXLC]{1,5}\.\s+|[A-H]\.\s+)", "", text)
    # 去掉包裹的标点与常见装饰
    text = text.strip(" .:;·—-–_*#")
    return text.lower()


def normalize_section_name(heading: str | None, *, default: str = "other") -> str:
    """把标题文本归一为受控章节名。

    >>> normalize_section_name("3. Method")
    'method'
    >>> normalize_section_name("2.1 Related Work")
    'related_work'
    >>> normalize_section_name("References")
    'other'
    """
    if not heading:
        return default
    cleaned = _clean(heading)
    if not cleaned:
        return default

    # 整串精确/包含匹配
    for section, keywords in _KEYWORD_RULES:
        for keyword in keywords:
            if cleaned == keyword:
                return section
    for section, keywords in _KEYWORD_RULES:
        for keyword in keywords:
            # 词边界匹配，避免 "methods" 命中 "method" 之外的误判
            if re.search(rf"(?:^|[^a-z]){re.escape(keyword)}(?:$|[^a-z])", cleaned):
                return section
    return default


def looks_like_heading(text: str, *, max_len: int = 120) -> bool:
    """启发式判断一段纯文本是否像章节标题（PDF 无标签时使用）。

    规则（全部满足才判定为标题）：
    1. 长度 <= ``max_len`` 且不含换行后仍成立的句末标点；
    2. 要么带编号前缀（``1 Introduction`` / ``2.3 Experiments`` / ``III. Method``），
       要么整串命中受控章节关键词。
    """
    if not text:
        return False
    collapsed = _WHITESPACE_RE.sub(" ", text).strip()
    if not collapsed or len(collapsed) > max_len:
        return False
    # 标题不以句号/问号/分号结尾；摘要常以 "Abstract—" 开头，单独放行
    if collapsed.endswith((".", "?", ";", "!", ",")):
        return False
    if NUMBERED_HEADING_RE.match(collapsed):
        return True
    cleaned = _clean(collapsed)
    if not cleaned:
        return False
    # 关键词整串命中（标题不应含过多词）
    if len(cleaned.split()) > 8:
        return False
    return any(cleaned in keywords for _section, keywords in _KEYWORD_RULES)


def section_order(section_name: str | None) -> int:
    """受控章节名的规范顺序（用于按章节排序展示）。"""
    try:
        return SECTION_NAMES.index(section_name or "other")
    except ValueError:
        return len(SECTION_NAMES)
