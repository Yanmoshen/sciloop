# Copyright 2026 SciLoop contributors
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
"""AI 高亮：把译文句子分类为三类重点句并在 PDF 上着色标注。

分类标签（契约固定值域）::

    core_conclusion    核心结论
    method_innovation  方法创新
    key_data           关键数据

流程（对应 EasyPaper §3.6）::

    从**已回写**的译文块提取句子
      → 分类（真实模型走 llm；无凭据时用**本地确定性规则**并如实披露）
      → 在 mono.pdf 上 search_for 句子坐标并写三种颜色标注
      → 统计 core_conclusion / method_innovation / key_data 数量

诚实性约束
----------

- 高亮失败**绝不破坏已生成的翻译 PDF**：失败时返回 ``pdf_bytes=None``，
  调用方保留未高亮版本，并把 ``highlight_summary.status`` 记为 ``failed``；
  部分句子定位失败记为 ``partial`` 并在 warnings 中给出数量。
- 无真实模型时使用本地规则分类（不是模型语义判断），通过
  ``layout_warnings`` 与 ``llm_call_logs(provider=local-stub)`` 双重披露。
"""

from __future__ import annotations

import asyncio
import logging
import re
from collections.abc import Sequence
from dataclasses import dataclass, field
from typing import Any

from services.translate import prompts
from services.translate.engine import BlockOutcome, LLMTranslator, Translator

logger = logging.getLogger("sciloop.translate.highlight")

#: 标注颜色（按标签）
COLORS: dict[str, tuple[float, float, float]] = {
    prompts.LABEL_CORE: (1.0, 0.83, 0.25),  # 黄：核心结论
    prompts.LABEL_METHOD: (0.55, 0.80, 1.0),  # 蓝：方法创新
    prompts.LABEL_DATA: (0.60, 0.93, 0.60),  # 绿：关键数据
}

#: 单次分类的句子数上限（控制 token 与调用次数）
BATCH_SIZE = 20
#: 参与高亮的句子总数上限
MAX_SENTENCES = 400
#: 最小句长（过短句子不参与分类，避免噪声）
MIN_SENTENCE_CHARS = 24
#: 定位用的片段长度
SNIPPET_CHARS = 60

_SENTENCE_SPLIT = re.compile(r"(?<=[.!?。！？；;])\s+")
_WS = re.compile(r"\s+")

#: 本地规则分类（仅在无真实模型时使用；确定性、可复现、逐条披露）
_RULES: tuple[tuple[str, re.Pattern[str]], ...] = (
    (
        prompts.LABEL_DATA,
        re.compile(
            r"\d+(?:\.\d+)?\s*(?:%|percent\b|x\b|×|B\b|M\b|K\b|k\b|GB\b|MB\b|ms\b|s\b)"
            r"|\b(?:accuracy|f1|bleu|rouge|perplexity|ppl|precision|recall|latency|throughput)\b"
            r"|\bTable\s*\d|\bFig(?:ure)?\.?\s*\d",
            re.IGNORECASE,
        ),
    ),
    (
        prompts.LABEL_METHOD,
        re.compile(
            r"\bwe\s+(?:propose|present|introduce|develop|design|build)\b"
            r"|\bnovel\b|\b(?:our|this)\s+(?:method|approach|framework|model|architecture)\b"
            r"|本文提出|我们提出|创新",
            re.IGNORECASE,
        ),
    ),
    (
        prompts.LABEL_CORE,
        re.compile(
            r"\b(?:conclude|conclusion|we\s+find|findings?)\b"
            r"|\bresults?\s+(?:show|indicate|demonstrate|suggest|reveal)\b"
            r"|\b(?:therefore|thus|in\s+summary|overall)\b"
            r"|结论|表明|综上",
            re.IGNORECASE,
        ),
    ),
)


@dataclass
class HighlightResult:
    """高亮结果（``pdf_bytes=None`` 表示高亮失败，调用方必须保留未高亮版本）。"""

    pdf_bytes: bytes | None = None
    counts: dict[str, int] = field(
        default_factory=lambda: dict.fromkeys(prompts.HIGHLIGHT_LABELS, 0)
    )
    status: str = "skipped"
    warnings: list[str] = field(default_factory=list)
    classified_sentences: int = 0
    located_sentences: int = 0
    used_local_rules: bool = False

    def summary(self) -> dict[str, Any]:
        return {
            "core_conclusion": int(self.counts.get(prompts.LABEL_CORE, 0)),
            "method_innovation": int(self.counts.get(prompts.LABEL_METHOD, 0)),
            "key_data": int(self.counts.get(prompts.LABEL_DATA, 0)),
            "status": self.status,
        }


def split_sentences(text: str) -> list[str]:
    """按中英文句末标点切句（保留标点，压缩空白）。"""
    normalized = _WS.sub(" ", str(text or "")).strip()
    if not normalized:
        return []
    pieces = [piece.strip() for piece in _SENTENCE_SPLIT.split(normalized)]
    return [piece for piece in pieces if piece]


def rule_classify(sentence: str) -> str | None:
    """本地规则分类（无模型时的诚实替代；顺序：关键数据 > 方法创新 > 核心结论）。"""
    for label, pattern in _RULES:
        if pattern.search(sentence):
            return label
    return None


def collect_sentences(outcomes: Sequence[BlockOutcome]) -> list[dict[str, Any]]:
    """从**已回写**的译文块提取候选句子（未回写块在 PDF 中仍是原文，不参与高亮）。"""
    items: list[dict[str, Any]] = []
    for index, outcome in enumerate(outcomes):
        if not outcome.written or not outcome.target_text:
            continue
        for sentence in split_sentences(outcome.target_text):
            if len(sentence) < MIN_SENTENCE_CHARS:
                continue
            items.append(
                {
                    "id": f"b{index}-s{len(items)}",
                    "page": int(outcome.page),
                    "text": sentence,
                }
            )
            if len(items) >= MAX_SENTENCES:
                return items
    return items


async def classify_sentences(
    sentences: Sequence[dict[str, Any]],
    *,
    translator: Translator | None,
    project_id: int | None,
) -> tuple[list[str | None], list[str], bool]:
    """返回 ``(labels, warnings, used_local_rules)``；真实模型失败时如实回落本地规则。"""
    warnings: list[str] = []
    use_llm = isinstance(translator, LLMTranslator) and bool(sentences)
    if use_llm:
        try:
            labels = await _classify_with_llm(
                sentences, translator=translator, project_id=project_id
            )
            return labels, warnings, False
        except Exception as exc:  # noqa: BLE001 - 分类失败不阻断翻译产物
            warnings.append(
                f"highlight: 模型分类失败（{type(exc).__name__}: {exc}），已回落本地规则分类"
            )
    else:
        warnings.append(
            "highlight: 无真实 LLM 凭据，句子分类由**本地规则**（关键词/数值模式）完成，"
            "不是模型语义判断，请研究者自行复核"
        )
    return [rule_classify(str(item.get("text") or "")) for item in sentences], warnings, True


async def _classify_with_llm(
    sentences: Sequence[dict[str, Any]],
    *,
    translator: Translator,
    project_id: int | None,
) -> list[str | None]:
    from llm import chat

    labels: list[str | None] = []
    model_ref = getattr(translator, "model_ref", "") or None
    for start in range(0, len(sentences), BATCH_SIZE):
        batch = list(sentences[start : start + BATCH_SIZE])
        payload = [{"id": item["id"], "text": item["text"]} for item in batch]
        result = await chat(
            prompts.build_highlight_messages(payload),
            model_ref=model_ref,
            stage="translate",
            purpose="highlight_classify",
            project_id=project_id,
            temperature=0.0,
            # 不设输出上限（同口径统一）：限死会让契约 JSON 被截断在中间
            max_tokens=None,
            json_schema=prompts.highlight_json_schema(),
            allow_fallback=True,
        )
        parsed = result.parsed if isinstance(result.parsed, dict) else {}
        mapping: dict[str, str | None] = {}
        for item in parsed.get("items") or []:
            if not isinstance(item, dict):
                continue
            key = str(item.get("id") or "")
            label = item.get("label")
            mapping[key] = str(label) if label in prompts.HIGHLIGHT_LABELS else None
        for item in batch:
            labels.append(mapping.get(str(item["id"])))
    return labels


def _get_pymupdf():  # pragma: no cover - 依赖运行环境
    try:
        import pymupdf  # type: ignore
    except ImportError:
        try:
            import fitz as pymupdf  # type: ignore
        except ImportError as exc:
            raise RuntimeError("缺少 pymupdf：AI 高亮需要 pymupdf 依赖") from exc
    return pymupdf


def _apply_marks(
    pdf_bytes: bytes,
    items: Sequence[tuple[int, str, str]],
) -> tuple[bytes, list[tuple[int, str, str]], int]:
    """在 PDF 上写标注，返回 ``(bytes, applied, unlocated)``（applied 逐条精确记录）。"""
    pymupdf = _get_pymupdf()
    document = pymupdf.open(stream=pdf_bytes, filetype="pdf")
    applied: list[tuple[int, str, str]] = []
    unlocated = 0
    try:
        for page_number, sentence, label in items:
            if page_number < 1 or page_number > document.page_count:
                unlocated += 1
                continue
            page = document.load_page(page_number - 1)
            snippet = sentence[:SNIPPET_CHARS]
            rects = page.search_for(snippet) or page.search_for(snippet[:24])
            if not rects:
                unlocated += 1
                continue
            for rect in rects[:1]:
                annot = page.add_highlight_annot(rect)
                annot.set_colors(stroke=COLORS.get(label, (1.0, 0.83, 0.25)))
                annot.update()
            applied.append((page_number, sentence, label))
        return document.tobytes(deflate=True, garbage=3), applied, unlocated
    finally:
        document.close()


async def run_highlight(
    pdf_bytes: bytes,
    outcomes: Sequence[BlockOutcome],
    *,
    translator: Translator | None,
    project_id: int | None,
) -> HighlightResult:
    """执行 AI 高亮；任何异常都返回 ``status=failed`` 且 ``pdf_bytes=None``。"""
    result = HighlightResult()
    try:
        sentences = collect_sentences(outcomes)
        if not sentences:
            result.status = "skipped"
            result.warnings.append("highlight: 没有可高亮的译文句子（未回写或句子过短）")
            return result

        labels, warnings, used_local_rules = await classify_sentences(
            sentences, translator=translator, project_id=project_id
        )
        result.warnings.extend(warnings)
        result.used_local_rules = used_local_rules

        items: list[tuple[int, str, str]] = []
        for sentence, label in zip(sentences, labels, strict=False):
            if not label:
                continue
            items.append((int(sentence["page"]), str(sentence["text"]), str(label)))

        result.classified_sentences = len(items)
        if not items:
            result.status = "skipped"
            result.warnings.append("highlight: 没有句子命中三类标签（core/method/key_data）")
            return result

        marked, applied, unlocated = await asyncio.to_thread(_apply_marks, pdf_bytes, items)
        if not applied:
            # 一句都没定位到：如实记 failed，并保留未高亮 PDF
            result.pdf_bytes = None
            result.status = "failed"
            result.warnings.append(
                f"highlight: {len(items)} 句已分类但无一句能在 PDF 中定位，高亮失败（已保留未高亮版本）"
            )
            return result

        per_label: dict[str, int] = dict.fromkeys(prompts.HIGHLIGHT_LABELS, 0)
        for _page, _sentence, label in applied:
            per_label[label] = per_label.get(label, 0) + 1
        result.pdf_bytes = marked
        result.located_sentences = len(applied)
        result.counts = per_label
        result.status = "partial" if unlocated else "ok"
        if unlocated:
            result.warnings.append(
                f"highlight: {len(items)} 句中 {unlocated} 句未能在 PDF 中定位（status=partial）"
            )
        if used_local_rules:
            result.warnings.append("highlight: 本次分类为本地规则而非模型判断，已如实标注")
        return result
    except Exception as exc:  # noqa: BLE001 - 高亮失败绝不破坏翻译 PDF
        logger.exception("highlight_failed")
        result.pdf_bytes = None
        result.status = "failed"
        result.warnings.append(f"highlight: 高亮执行异常（{type(exc).__name__}: {exc}），已保留未高亮版本")
        return result


__all__ = [
    "BATCH_SIZE",
    "COLORS",
    "MAX_SENTENCES",
    "HighlightResult",
    "classify_sentences",
    "collect_sentences",
    "rule_classify",
    "run_highlight",
    "split_sentences",
]
