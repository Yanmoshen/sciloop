# Copyright 2026 SciLoop contributors
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
"""草稿句级拆分与事实性判定（WP13-T3，供 WP14 的 ``writing`` 环节调用）。

对外稳定签名::

    claims = split_claims(content_md, draft_id=draft_id)      # 纯函数，不连库
    stored = await persist_claims(session, draft_id, claims)  # 写 draft_claims

设计口径（与 contracts.evidence_rules.claim_rule 一致）
------------------------------------------------------
- **句级切分**：按行扫描，跳过 ``` 围栏代码块；标题行只用于更新
  ``section_heading``，本身不作为 Claim；**引用块（``> …``）与**
  **「引用索引 / 参考文献 / 生成说明 / 合规声明」小节整体跳过**——
  前者是 AI 辅助声明与标注，后者是「引用编号 → 证据键」的映射表与生成元信息，
  都不是研究主张，计入分母会污染 ``claim_coverage``。
- **``is_factual`` 判定纯规则化**：过渡句 / 结构句 / 方法描述句 / 图表指代句 /
  疑问句 / 过短片段标 ``False``，**不参与** ``claim_coverage`` 分母；
  其余陈述句标 ``True``。每条都带 ``factual_reason`` 便于人工抽样复核。
- **偏移如实**：``char_start`` / ``char_end`` 指向 ``content_md`` 原文中该句的区间；
  ``claim_text`` 是去除 markdown 装饰后的可读形式。
  ``draft_claims`` 表没有偏移列，因此偏移只出现在 API 响应里（不为此改表）。
- 不调用 LLM：拆分必须可离线、可复现，同一草稿两次拆分结果逐字节一致。
"""

from __future__ import annotations

import inspect
import logging
import re
from collections.abc import Awaitable, Callable, Iterable, Sequence
from typing import Any

from sqlalchemy import text

from app.services.evidence.records import Claim, EvidenceError

logger = logging.getLogger("sciloop.wp13.claim_splitter")

#: 短于此长度的片段不视为可核验事实（页码残留、表头、半截句）
MIN_FACTUAL_CHARS = 12

#: 句末终止符：中英文标点 + 换行 + 句号后接大写字母的英文断句
#: 句号前若是常见缩写（vs. / e.g. / etc. / Fig. / Eq. / No.）则不断句——
#: 否则 ``Objective vs. Search`` 会被切成两条 Claim（真实夹具里踩到过）。
_TERMINATOR_RE = re.compile(
    r"([。！？；!?;]+"
    r"|(?<=[A-Za-z0-9)%\]\"'’”])(?<!vs)(?<!eg)(?<!ie)(?<!etc)(?<!al)"
    r"(?<!Fig)(?<!Eq)(?<!No)\.(?=\s+[A-Z0-9(\"“‘])"
    r"|\n+)"
)
#: markdown 标题
_HEADING_RE = re.compile(r"^\s{0,3}(#{1,6})\s+(.*?)\s*#*\s*$")
#: 围栏代码块起始
_FENCE_RE = re.compile(r"^\s*(```+|~~~+)")
#: **非正文小节**：引用索引 / 生成说明。标题命中后其后内容一律不作为 Claim：
#: 前者是「引用编号 → 证据键」的映射表，后者是生成过程的元信息，
#: 二者都不是研究主张，计入分母会污染 ``claim_coverage``。
_SKIP_SECTION_RE = re.compile(
    r"引用索引|证据池映射|参考文献|references|生成说明|生成方式|合规声明",
    re.IGNORECASE,
)
#: 引用前缀
_PREFIX_RE = re.compile(r"^\s*(?:[-*+]|\d{1,3}[.)])\s+|^\s*>\s?")
#: 表格分隔行（| --- | --- |）
_TABLE_SEP_RE = re.compile(r"^\s*\|?[\s:|-]{4,}\|?\s*$")
#: 段落里的 markdown 装饰
_IMAGE_RE = re.compile(r"!\[([^\]]*)\]\([^)]*\)")
_LINK_RE = re.compile(r"\[([^\]]+)\]\([^)]*\)")
_CODE_SPAN_RE = re.compile(r"`([^`]+)`")
_BOLD3_RE = re.compile(r"\*\*\*(.+?)\*\*\*")
_BOLD_RE = re.compile(r"\*\*(.+?)\*\*")
_ITALIC_RE = re.compile(r"(?<!\*)\*(?!\s)(.+?)(?<!\s)\*(?!\*)")
_STRIKE_RE = re.compile(r"~~(.+?)~~")
_WS_RE = re.compile(r"[ \t\u00a0\u3000]+")

#: 过渡 / 结构句（描述行文顺序，不含事实主张）
#: 注：结尾不能用 ``\b?``——``\b`` 是零宽断言，Python re 会报
#: "nothing to repeat"（本文件曾因此无法导入，导致 WP13 路由长期缺席）。
_TRANSITION_RE = re.compile(
    r"^\s*(?:"
    r"本节|本章|本文将|本文首先|接下来|下面|首先|其次|再者|最后|综上|总之|同时|此外|"
    r"另一方面|值得注意的是|上一节|下一节|如前所述|"
    r"this\s+(?:section|paper|work)|in\s+this\s+(?:section|paper)|"
    r"we\s+(?:now|next|first|then|begin|organize)|the\s+rest\s+of|"
    r"next,|then,|finally,|overall,|in\s+summary|as\s+(?:shown|described)\s+(?:above|below)"
    r")",
    re.IGNORECASE,
)
#: 方法描述句（"我们采用 X 做 Y" —— 描述做法而非断言结果）
_METHOD_RE = re.compile(
    r"^\s*(?:我们|本文|本章|本研究|作者)?\s*"
    r"(?:采用|使用|基于|借助|通过|选用|构建|搭建|we\s+(?:use|adopt|employ|apply|"
    r"leverage|build\s+on|implement|propose))",
    re.IGNORECASE,
)
#: 结果 / 对比措辞：出现即说明该句断言了可核验事实，方法描述句豁免
_RESULT_RE = re.compile(
    r"(?:提升|提高|降低|下降|达到|超过|优于|低于|相比|相较|减少|增加|"
    r"outperform|improve[sd]?|reduce[sd]?|achieve[sd]?|surpass|"
    r"\d%%|\d+\s*%|\d+\.\d+)",
    re.IGNORECASE,
)
#: 图表指代句（无独立事实主张）
_FIGURE_RE = re.compile(
    r"^\s*(?:图|表|式|Fig(?:ure)?\.?|Table|Eq(?:uation)?\.?)\s*\d", re.IGNORECASE
)
#: 仅含引用标记的片段
_CITATION_ONLY_RE = re.compile(r"^\s*\[?\d+(?:\s*[-,–]\s*\d+)*\]?\s*[.。]?\s*$")
#: 明显的非正文行
_NON_BODY_RE = re.compile(r"^\s*(?:致谢|参考文献|References|Acknowledg\w*)\b", re.IGNORECASE)
#: 句尾疑问
_QUESTION_RE = re.compile(r"[?？]\s*$")


def clean_inline(text: str) -> str:
    """去除行内 markdown 装饰（保留文字与数字，去掉链接地址/强调符号）。"""
    value = _IMAGE_RE.sub(r"\1", text or "")
    value = _LINK_RE.sub(r"\1", value)
    value = _BOLD3_RE.sub(r"\1", value)
    value = _BOLD_RE.sub(r"\1", value)
    value = _ITALIC_RE.sub(r"\1", value)
    value = _STRIKE_RE.sub(r"\1", value)
    value = _CODE_SPAN_RE.sub(r"\1", value)
    value = _WS_RE.sub(" ", value)
    return value.strip()


def _has_content(text: str) -> bool:
    """含字母或数字才算候选句（过滤纯标点/装饰残渣）。"""
    return any(ch.isalnum() or "\u4e00" <= ch <= "\u9fff" for ch in text)


def _iter_sentences(line: str, base: int) -> Iterable[tuple[int, int, str]]:
    """把一行切成句，产出 ``(char_start, char_end, raw)``（含终止符）。"""
    cursor = 0
    for match in _TERMINATOR_RE.finditer(line):
        end = match.end()
        segment = line[cursor:end]
        if segment.strip():
            yield base + cursor, base + end, segment
        cursor = end
    tail = line[cursor:]
    if tail.strip():
        yield base + cursor, base + len(line), tail


def classify_factual(cleaned: str) -> tuple[bool, str]:
    """判定一句是否属于「事实性 Claim」，返回 ``(is_factual, reason)``。

    规则顺序即优先级，纯字符串判定、可复现、无 LLM。
    """
    text_value = (cleaned or "").strip()
    if len(text_value) < MIN_FACTUAL_CHARS:
        return False, f"过短片段（<{MIN_FACTUAL_CHARS} 字符）：不构成可核验的事实主张"
    if _CITATION_ONLY_RE.match(text_value):
        return False, "仅引用标记：无独立事实主张"
    if _NON_BODY_RE.match(text_value):
        return False, "非正文段落（致谢/参考文献/附录标题）"
    if _QUESTION_RE.search(text_value):
        return False, "疑问句：不构成可核验的事实主张"
    if _TRANSITION_RE.match(text_value):
        return False, "过渡句：描述行文结构，不参与覆盖率统计"
    if _FIGURE_RE.match(text_value):
        return False, "图表指代句：无独立事实主张，不参与覆盖率统计"
    if _METHOD_RE.match(text_value) and not _RESULT_RE.search(text_value):
        return False, "方法描述句：描述做法而非断言结果，不参与覆盖率统计"
    return True, "陈述句：参与覆盖率统计"


def split_claims(
    content_md: str,
    *,
    draft_id: int | None = None,
    max_claims: int = 0,
    min_factual_chars: int = MIN_FACTUAL_CHARS,
) -> list[Claim]:
    """把草稿 markdown 拆成 Claim 列表（纯函数，不连库）。

    :param content_md: ``paper_drafts.content_md``
    :param draft_id: 仅用于 ``factual_reason`` 日志上下文，不影响切分结果
    :param max_claims: ``>0`` 时按出现顺序截断（默认 0 = 不限）
    :param min_factual_chars: 事实性判定的最短长度
    """
    claims: list[Claim] = []
    if not content_md:
        return claims

    in_fence = False
    skip_section = False
    section_heading: str | None = None
    index = 0
    offset = 0

    for raw_line in content_md.split("\n"):
        line_start = offset
        offset += len(raw_line) + 1  # +1 为被 split 掉的 '\n'
        line = raw_line.replace("\r", "")

        if _FENCE_RE.match(line):
            in_fence = not in_fence
            continue
        if in_fence:
            continue

        heading = _HEADING_RE.match(line)
        if heading:
            title = clean_inline(heading.group(2))
            skip_section = bool(_SKIP_SECTION_RE.search(title))
            section_heading = None if skip_section else (title or section_heading)
            continue
        if skip_section:
            continue
        # 引用块（`> …`）：AI 辅助声明 / 合规声明 / 夹具标注，不是研究主张
        if line.lstrip().startswith(">"):
            continue
        if not line.strip() or _TABLE_SEP_RE.match(line):
            continue

        body = _PREFIX_RE.sub("", line, count=1)
        body_offset = line_start + (len(line) - len(body))

        for char_start, char_end, raw in _iter_sentences(body, body_offset):
            cleaned = clean_inline(raw)
            if not _has_content(cleaned):
                continue
            # 纯引用角标残留（句末标点之后的 ``[1][2]``）：无独立语义，
            # 不产 Claim，避免噪声条目污染 claim_coverage 分母
            if _CITATION_ONLY_RE.match(cleaned):
                continue
            is_factual, reason = classify_factual(cleaned)
            if len(cleaned) < min_factual_chars and is_factual:
                is_factual, reason = False, (
                    f"过短片段（<{min_factual_chars} 字符）：不构成可核验的事实主张"
                )
            claims.append(
                Claim(
                    index=index,
                    claim_text=cleaned,
                    is_factual=is_factual,
                    section_heading=section_heading,
                    factual_reason=reason,
                    char_start=char_start,
                    char_end=char_end,
                )
            )
            index += 1
            if max_claims and len(claims) >= max_claims:
                logger.info(
                    "split_claims 截断 draft_id=%s max_claims=%s", draft_id, max_claims
                )
                return claims

    logger.info(
        "split_claims draft_id=%s total=%d factual=%d",
        draft_id,
        len(claims),
        sum(1 for claim in claims if claim.is_factual),
    )
    return claims


async def _maybe_await(value: Any) -> Any:
    if inspect.isawaitable(value):
        return await value
    return value


async def persist_claims(
    session: Any,
    draft_id: int,
    claims: Sequence[Claim],
    *,
    commit: bool = True,
) -> list[Claim]:
    """把 Claim 落库到 ``draft_claims``（**全量替换**，重跑安全）。

    返回带 ``claim_id`` 的 Claim 列表；``support_status`` 先写 ``insufficient``
    （"尚未判定"），由 :mod:`app.services.evidence.integrity_checker` 覆写为三态。
    """
    if not claims:
        if commit:
            await _maybe_await(
                session.execute(
                    text("DELETE FROM draft_claims WHERE draft_id = :draft_id"),
                    {"draft_id": int(draft_id)},
                )
            )
            await _maybe_await(session.commit())
        return []

    await _maybe_await(
        session.execute(
            text("DELETE FROM draft_claims WHERE draft_id = :draft_id"),
            {"draft_id": int(draft_id)},
        )
    )

    stored: list[Claim] = []
    for claim in claims:
        result = await _maybe_await(
            session.execute(
                text(
                    """
                    INSERT INTO draft_claims (
                        draft_id, section_heading, claim_text, is_factual,
                        support_status, status_reason, evidence_count
                    ) VALUES (
                        :draft_id, :section_heading, :claim_text, :is_factual,
                        :support_status, :status_reason, :evidence_count
                    ) RETURNING id
                    """
                ),
                {
                    "draft_id": int(draft_id),
                    "section_heading": claim.section_heading,
                    "claim_text": claim.claim_text,
                    "is_factual": bool(claim.is_factual),
                    "support_status": claim.support_status,
                    "status_reason": claim.status_reason
                    or ("待证据判定" if claim.is_factual else claim.factual_reason),
                    "evidence_count": int(claim.evidence_count),
                },
            )
        )
        claim.claim_id = int(result.scalar_one())
        stored.append(claim)

    if commit:
        await _maybe_await(session.commit())
    logger.info("persist_claims draft_id=%s rows=%d", draft_id, len(stored))
    return stored


async def split_and_persist(
    session: Any,
    draft_id: int,
    content_md: str,
    *,
    max_claims: int = 0,
    commit: bool = True,
) -> list[Claim]:
    """``split_claims`` + ``persist_claims`` 的便捷组合（WP14 直接调用）。"""
    claims = split_claims(content_md, draft_id=draft_id, max_claims=max_claims)
    return await persist_claims(session, draft_id, claims, commit=commit)


#: 允许注入的 LLM 句切分器（默认不启用：拆分必须离线可复现）
SentenceSplitter = Callable[[str], list[str] | Awaitable[list[str]]]

__all__ = [
    "MIN_FACTUAL_CHARS",
    "SentenceSplitter",
    "clean_inline",
    "classify_factual",
    "persist_claims",
    "split_and_persist",
    "split_claims",
    "EvidenceError",
]
