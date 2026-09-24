# Copyright 2026 SciLoop contributors
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.
"""可行性四维评分卡（WP08-T5，附录 A.4 ``feasibilities`` 四个 JSONB 列）。

四维与方向（**每个 score 都是 0–100「越大越有利」**，方向写在 rationale 里）
--------------------------------------------------------------------------
====================  ==========================================================
``data_availability`` 数据可得性：目标数据越易获得分越高
``compute_cost``      算力/预算友好度：越省算力、越贴合成本护栏分越高
``method_maturity``   方法成熟度：本次聚合中已有越多相近做法分越高
``novelty_gap``       与已有工作差异度：与聚合内工作越不重叠分越高
====================  ==========================================================

反黑箱（WP08 hard_constraints / risks）
---------------------------------------
- 每个维度都落 ``{score, rationale, evidence[], signals, formula, weights}``；
  ``signals`` 是**真实参与计算的输入值**，``formula`` 是**可手算的表达式**，
  任何人拿 signals 都能复算出同一个 score。
- **规则层拥有最终决定权**：``score`` 只由 signals 与固定公式产生；
  LLM（``use_llm=True`` 时）只写 ``llm_rationale`` 与 ``llm_suggested_score``，
  **不参与 total_score 计算**（与 contracts.risk_policy_rules 同口径）。
- 取不到的信号一律进 ``signals_missing`` 并在 rationale 中说明，**不编造数值**；
  例如真实卡片里 ``experimental_setup.datasets`` 全为 ``"unknown"`` 占位，
  本模块将其视为「材料未提供」而非数据集名。
"""

from __future__ import annotations

import logging
import re
from collections.abc import Mapping, Sequence
from typing import Any

from services.aggregation.cards import entry_text
from services.aggregation.span_locator import content_words

logger = logging.getLogger("sciloop.wp08.scorer")

#: 维度键 → 中文标签（附录 A.4 四列）
DIMENSIONS: tuple[tuple[str, str], ...] = (
    ("data_availability", "数据可得性"),
    ("compute_cost", "算力需求友好度"),
    ("method_maturity", "方法成熟度"),
    ("novelty_gap", "与已有工作差异度"),
)

#: total_score 权重（写入 scoring.weights，可手算复核）
WEIGHTS: dict[str, float] = {
    "data_availability": 0.30,
    "compute_cost": 0.20,
    "method_maturity": 0.25,
    "novelty_gap": 0.25,
}

#: 每个维度使用的卡片字段（**互不重复**，避免 WP13 自然键重复导致该维证据为空）
DIMENSION_CARD_FIELD: dict[str, str] = {
    "data_availability": "experimental_setup",
    "compute_cost": "technical_route",
    "method_maturity": "core_method",
    "novelty_gap": "limitations",
}

#: 卡片占位值（WP06 在材料未提供时会写这些词，不可当作真实取值）。
#: ⚠️ 卡片自 2026-09-24 起用中文占位「未提及」（原为 ``unknown``）—— 漏掉的话
#: 占位值会被当成真实数据集/基线参与可行性打分（等于拿"没数据"去算分）。
PLACEHOLDERS: frozenset[str] = frozenset(
    {
        "unknown",
        "未知",
        "未提及",
        "n/a",
        "na",
        "none",
        "null",
        "-",
        "",
        "not specified",
        "unspecified",
        "待定",
    }
)

#: 成本护栏兜底（与 contracts.guardrails.cost 一致，实际值由 runtime 传入）
DEFAULT_MAX_LLM_COST_USD = 8.0
DEFAULT_DEMO_QUOTA_USD = 3.0

#: 数据集体名抽取（只在真实 span 上跑，命中即携带原文与 span id）
DATASET_RE = re.compile(
    r"([A-Z][A-Za-z0-9]{2,}(?:-[A-Za-z0-9]+)*)\s+(?:dataset|benchmark|corpus)",
)

#: span 至少要有这么多词（用于排除 "Tokeniser-training corpus." 这类标题行）
MIN_DATASET_SPAN_WORDS = 12

#: 命中所在**句子**至少要有这么多词（标题/表头句会被滤掉，正文句保留）
MIN_DATASET_SENTENCE_WORDS = 8

#: 句首虚词/泛化词黑名单（避免把 "The dataset" / "Our dataset" 当数据集名）
DATASET_STOPWORDS: frozenset[str] = frozenset(
    {
        "the", "our", "this", "that", "these", "those", "both", "each", "all",
        "prior", "existing", "new", "same", "other", "their", "its", "his",
        "results", "result", "following", "proposed", "given", "such", "some",
        "any", "several", "many", "most", "more", "less", "first", "second",
        "third", "final", "main", "primary", "single", "every", "a", "an",
    }
)


# --------------------------------------------------------------------------- #
# 信号抽取
# --------------------------------------------------------------------------- #
def real_terms(values: Any) -> list[str]:
    """从卡片字段里取出**真实**取值（过滤占位词 ``unknown`` 等）。"""
    collected: list[str] = []
    items = values if isinstance(values, list) else ([values] if values else [])
    for item in items:
        text = entry_text(item)
        if not text:
            continue
        cleaned = text.strip()
        if cleaned.lower() in PLACEHOLDERS:
            continue
        if cleaned not in collected:
            collected.append(cleaned)
    return collected


def field_terms(card: Mapping[str, Any], card_field: str, sub_key: str | None = None) -> list[str]:
    raw = card.get(card_field)
    if sub_key is not None:
        if not isinstance(raw, Mapping):
            return []
        raw = raw.get(sub_key)
    return real_terms(raw)


def _paper_label(card: Mapping[str, Any]) -> str:
    return str(card.get("title") or f"论文 {card.get('paper_id')}")


def extract_dataset_mentions(
    cards: Sequence[Mapping[str, Any]], *, limit: int = 6
) -> list[dict[str, Any]]:
    """从**真实 span** 中抽取数据集名候选（带原文与 span id 供核验）。

    这是「材料未提供结构化数据集」时的补救：不做任何猜测，只把原文里
    写成 ``Xxx dataset / benchmark / corpus`` 的实体挑出来，并标注
    ``confidence='candidate_needs_confirmation'``——最终是否采用由研究者确认。
    """
    mentions: list[dict[str, Any]] = []
    seen: set[str] = set()
    for card in cards:
        if card.get("scope") != "fulltext":
            continue
        for span in card.get("spans") or []:
            quote = str(span.get("quote_text") or "")
            # 短段落多为标题/表格标题（如 "Tokeniser-training corpus."），不作为数据集来源
            if len(quote.split()) < MIN_DATASET_SPAN_WORDS:
                continue
            for match in DATASET_RE.finditer(quote):
                name = match.group(1)
                if name.lower() in DATASET_STOPWORDS or len(name) < 3:
                    continue
                sentence = _sentence_around(quote, match.start())
                if len(sentence.split()) < MIN_DATASET_SENTENCE_WORDS:
                    continue
                if name in seen:
                    bucket = _mention_index(mentions, name)
                    if bucket is not None and int(card["paper_id"]) not in bucket["paper_ids"]:
                        bucket["paper_ids"].append(int(card["paper_id"]))
                        bucket["paper_count"] = len(bucket["paper_ids"])
                    continue
                seen.add(name)
                start = max(0, match.start() - 80)
                mentions.append(
                    {
                        "name": name,
                        "paper_id": int(card["paper_id"]),
                        "paper_ids": [int(card["paper_id"])],
                        "paper_count": 1,
                        "paper_title": card.get("title"),
                        "paper_span_id": span.get("id"),
                        "document_version": span.get("document_version"),
                        "section_name": span.get("section_name"),
                        "page_number": span.get("page_number"),
                        "char_start": span.get("char_start"),
                        "char_end": span.get("char_end"),
                        "quote_text": quote[start : match.end() + 80],
                        "sentence": sentence,
                        "matched_phrase": match.group(0),
                        "evidence_type": "paper_span",
                        "extraction": "regex:dataset_mention",
                        "confidence": "candidate_needs_confirmation",
                        "jump_url": f"/papers/{int(card['paper_id'])}#span-{span.get('id')}",
                    }
                )
                if len(mentions) >= limit:
                    return mentions
    # 被多篇论文提到的名称更可能是**已确立**的公开数据集，优先推荐
    mentions.sort(key=lambda item: (-int(item.get("paper_count") or 1), item["name"]))
    return mentions


def _mention_index(mentions: list[dict[str, Any]], name: str) -> dict[str, Any] | None:
    for item in mentions:
        if item["name"] == name:
            return item
    return None


def _sentence_around(text: str, position: int) -> str:
    """取命中位置所在的句子（按句末标点切分），用于过滤标题/表头行。"""
    left = max(text.rfind(". ", 0, position), text.rfind("; ", 0, position), text.rfind("\n", 0, position))
    right_candidates = [pos for pos in (text.find(". ", position), text.find("\n", position)) if pos != -1]
    right = min(right_candidates) if right_candidates else len(text)
    return text[left + 1 : right + 1].strip()


def build_signal_bundle(
    cards: Sequence[Mapping[str, Any]],
    *,
    idea: Mapping[str, Any],
    max_llm_cost_usd: float = DEFAULT_MAX_LLM_COST_USD,
    demo_cost_quota_usd: float = DEFAULT_DEMO_QUOTA_USD,
    estimated_cost_usd: float | None = None,
    used_cost_usd: float | None = None,
    sample_size: int = 20,
) -> dict[str, Any]:
    """汇总四维打分所需的**全部真实信号**（纯函数）。"""
    datasets: list[dict[str, Any]] = []
    metrics: list[str] = []
    baselines: list[str] = []
    methods: list[str] = []
    limitations: list[str] = []
    dataset_field_values: list[str] = []
    for card in cards:
        for item in field_terms(card, "experimental_setup", "datasets"):
            if item not in dataset_field_values:
                dataset_field_values.append(item)
        for item in field_terms(card, "experimental_setup", "metrics"):
            if item not in metrics:
                metrics.append(item)
        for item in field_terms(card, "experimental_setup", "baselines"):
            if item not in baselines:
                baselines.append(item)
        for item in field_terms(card, "core_method"):
            if item not in methods:
                methods.append(item)
        for item in field_terms(card, "limitations"):
            if item not in limitations:
                limitations.append(item)
    datasets = extract_dataset_mentions(cards)

    fulltext_papers = [int(c["paper_id"]) for c in cards if c.get("scope") == "fulltext"]
    abstract_only = [int(c["paper_id"]) for c in cards if c.get("scope") != "fulltext"]

    idea_terms = content_words(
        " ".join([str(idea.get("title") or ""), str(idea.get("content") or "")])
    )
    per_paper_overlap: list[dict[str, Any]] = []
    term_paper_count: dict[str, int] = {}
    for card in cards:
        text = " ".join(
            filter(
                None,
                [
                    str(card.get("core_method") or ""),
                    str(card.get("key_innovation") or ""),
                    str(card.get("research_problem") or ""),
                    str(card.get("technical_route") or ""),
                ],
            )
        )
        shared = idea_terms & content_words(text)
        coverage = len(shared) / len(idea_terms) if idea_terms else 0.0
        for term in shared:
            term_paper_count[term] = term_paper_count.get(term, 0) + 1
        per_paper_overlap.append(
            {
                "paper_id": int(card["paper_id"]),
                "title": card.get("title"),
                "shared_terms": sorted(shared)[:12],
                "shared_count": len(shared),
                "coverage": round(coverage, 3),
            }
        )
    max_overlap = max((item["coverage"] for item in per_paper_overlap), default=0.0)
    cross_paper_terms = sorted(term for term, count in term_paper_count.items() if count >= 2)

    return {
        "idea_terms": sorted(idea_terms)[:24],
        # 仅内存使用：供四维构造 card_field / paper_span 证据候选；落库前必须剔除
        "_cards": list(cards),
        "papers": [
            {
                "paper_id": int(card["paper_id"]),
                "title": card.get("title"),
                "scope": card.get("scope"),
                "coverage": card.get("coverage"),
                "coverage_tag": card.get("coverage_tag"),
            }
            for card in cards
        ],
        "paper_ids": [int(card["paper_id"]) for card in cards],
        "fulltext_paper_ids": fulltext_papers,
        "abstract_only_paper_ids": abstract_only,
        "dataset_field_values": dataset_field_values,
        "dataset_mentions": datasets,
        "dataset_mentions_regex_used": True,
        "metrics": metrics,
        "baselines": baselines,
        "methods": methods,
        "limitations": limitations,
        "overlap": per_paper_overlap,
        "max_overlap": round(max_overlap, 3),
        "cross_paper_terms": cross_paper_terms,
        "cross_paper_term_count": len(cross_paper_terms),
        "budget": {
            "max_llm_cost_usd": float(max_llm_cost_usd),
            "demo_cost_quota_usd": float(demo_cost_quota_usd),
            "estimated_cost_usd": (
                None if estimated_cost_usd is None else round(float(estimated_cost_usd), 6)
            ),
            "used_cost_usd": (
                None if used_cost_usd is None else round(float(used_cost_usd), 6)
            ),
            "sample_size": int(sample_size),
        },
    }


def _clamp(value: float) -> int:
    return int(max(0, min(100, round(value))))


def _evidence_candidate(
    card: Mapping[str, Any], card_field: str, *, span: Mapping[str, Any] | None = None
) -> dict[str, Any]:
    candidate: dict[str, Any] = {
        "evidence_type": "card_field",
        "paper_id": int(card["paper_id"]),
        "card_field": card_field,
        "weight": 1.0,
    }
    if span is not None and span.get("paper_span_id") is not None:
        candidate["paper_span_id"] = int(span["paper_span_id"])
        if span.get("quote_text"):
            candidate["quote_text"] = span["quote_text"]
    return candidate


def _span_evidence_candidate(span: Mapping[str, Any]) -> dict[str, Any]:
    candidate: dict[str, Any] = {
        "evidence_type": "paper_span",
        "paper_span_id": int(span["paper_span_id"]),
        "paper_id": span.get("paper_id"),
        "weight": 1.0,
    }
    if span.get("quote_text"):
        candidate["quote_text"] = span["quote_text"]
    return candidate


# --------------------------------------------------------------------------- #
# 四维打分
# --------------------------------------------------------------------------- #
def score_data_availability(bundle: Mapping[str, Any]) -> dict[str, Any]:
    """数据可得性：公式 ``25 + 10*min(候选数据集,2) + 20*min(跨论文数据集,2) + 10*min(全文论文,3)``。

    候选数据集只是**从原文抽出的名字**（未确认可获取），故只给部分分；
    只有被 ≥2 篇论文共同提到（= 更像公开标准数据集）才计入 ``cross_paper_datasets`` 权重。
    """
    mentions = list(bundle.get("dataset_mentions") or [])
    fulltext = list(bundle.get("fulltext_paper_ids") or [])
    field_values = list(bundle.get("dataset_field_values") or [])
    cross_paper = [item for item in mentions if int(item.get("paper_count") or 1) >= 2]
    missing: list[str] = []
    if not field_values:
        missing.append("paper_cards.experimental_setup.datasets（本次聚合的卡片全为占位值 unknown）")
    if not mentions:
        missing.append("原文中的数据集提及（未从 span 中抽到 Xxx dataset/benchmark/corpus）")

    score = _clamp(
        25
        + 10 * min(len(mentions), 2)
        + 20 * min(len(cross_paper), 2)
        + 10 * min(len(fulltext), 3)
    )
    rationale = (
        f"【方向：越高越易获得数据】从真实原文 span 抽到 {len(mentions)} 个数据集候选"
        f"（{', '.join(item['name'] for item in mentions[:4]) or '无'}），其中被 >=2 篇论文共同提到的 "
        f"{len(cross_paper)} 个（={', '.join(item['name'] for item in cross_paper) or '无'}）；"
        f"本次聚合中通过全文闸门（parse_status=ok 且 coverage>=0.60）的论文 {len(fulltext)} 篇。"
        f"卡片结构化字段 datasets 视为「材料未提供」"
        f"（取值 {field_values or '空'} 属占位词，未当作数据集名）；"
        f"候选名均标记 confirmed=false，仅按「候选」给分。"
        + (f" 未提供信号：{'；'.join(missing)}——已按可得性较低计分，未编造数据集名。" if missing else "")
    )
    evidence: list[dict[str, Any]] = []
    for card in bundle.get("_cards") or []:
        if card.get("scope") == "fulltext":
            evidence.append(_evidence_candidate(card, "experimental_setup"))
    for mention in mentions[:3]:
        if mention.get("paper_span_id") is not None:
            evidence.append(_span_evidence_candidate(mention))
    return {
        "key": "data_availability",
        "label": "数据可得性",
        "score": score,
        "rationale": rationale,
        "signals": {
            "dataset_candidates": [item["name"] for item in mentions],
            "dataset_candidate_count": len(mentions),
            "cross_paper_datasets": [item["name"] for item in cross_paper],
            "cross_paper_dataset_count": len(cross_paper),
            "candidates_confirmed": False,
            "fulltext_paper_ids": fulltext,
            "fields_datasets_value": field_values,
            "fields_source": "paper_cards.experimental_setup.datasets",
        },
        "signals_missing": missing,
        "formula": (
            "score = clamp(25 + 10*min(dataset_candidates,2) + 20*min(cross_paper_datasets,2) "
            "+ 10*min(fulltext_papers,3))"
        ),
        "evidence_candidates": evidence[:4],
    }


def score_compute_cost(bundle: Mapping[str, Any]) -> dict[str, Any]:
    """算力/预算友好度：公式 ``100 - 100*已耗/硬护栏``，并扣样本量惩罚。"""
    budget = dict(bundle.get("budget") or {})
    limit = float(budget.get("max_llm_cost_usd") or DEFAULT_MAX_LLM_COST_USD)
    quota = float(budget.get("demo_cost_quota_usd") or DEFAULT_DEMO_QUOTA_USD)
    estimated = budget.get("estimated_cost_usd")
    used = budget.get("used_cost_usd")
    sample_size = int(budget.get("sample_size") or 20)
    missing: list[str] = []
    if used is None:
        missing.append("项目已耗成本（llm_call_logs 累计，成本唯一权威口径）")
    if estimated is None:
        missing.append(
            "MVE 预估成本（本机无可用单价，mve_plan.expected_cost_usd 如实为 null，禁止编造）"
        )
    consumed = float(used or 0.0) + float(estimated or 0.0)

    ratio = 0.0 if limit <= 0 else min(1.0, consumed / limit)
    penalty = max(0, sample_size - 50) * 1.0  # sample_size<=50 为契约硬上限
    unknown_estimate_penalty = 10.0 if estimated is None else 0.0
    score = _clamp(100 - 100 * ratio - penalty - unknown_estimate_penalty)
    rationale = (
        f"【方向：越高越省算力/越贴合预算】本次判据用**真实记账值**：项目累计 LLM 成本 "
        f"{0.0 if used is None else used:.4f} USD"
        + (f" + MVE 预估 {float(estimated):.4f}" if estimated is not None else "（MVE 预估未提供）")
        + f" = {consumed:.4f} USD，对硬护栏 {limit:.2f} USD 占比 {ratio * 100:.2f}%；"
        f"演示配额 {quota:.2f} USD。契约硬约束 sample_size<=50，本方案 sample_size={sample_size}。"
        + (f" 预估缺失 → 按保守口径扣 {unknown_estimate_penalty:.0f} 分。" if estimated is None else "")
        + (f" 未提供信号：{'；'.join(missing)}。" if missing else "")
    )
    evidence: list[dict[str, Any]] = []
    for card in bundle.get("_cards") or []:
        evidence.append(_evidence_candidate(card, "technical_route"))
    return {
        "key": "compute_cost",
        "label": "算力需求友好度",
        "score": score,
        "rationale": rationale,
        "signals": {
            "used_cost_usd": None if used is None else round(float(used), 6),
            "estimated_cost_usd": None if estimated is None else round(float(estimated), 6),
            "consumed_cost_usd": round(consumed, 6),
            "max_llm_cost_usd": limit,
            "demo_cost_quota_usd": quota,
            "budget_ratio": round(ratio, 4),
            "sample_size": sample_size,
            "sample_size_hard_limit": 50,
            "sample_size_penalty": penalty,
            "unknown_estimate_penalty": unknown_estimate_penalty,
            "cost_source": "llm_call_logs 累计（WP02 记账）",
        },
        "signals_missing": missing,
        "formula": (
            "score = clamp(100 - 100*min(1, (已耗成本+预估成本)/硬护栏) "
            "- max(0, sample_size-50) - (10 if 预估缺失 else 0))"
        ),
        "evidence_candidates": evidence[:4],
    }


def score_method_maturity(bundle: Mapping[str, Any]) -> dict[str, Any]:
    """方法成熟度：公式 ``20 + 15*min(相近论文,3) + 10*min(跨论文共识词,3) + 10*min(全文论文,3)``。

    与 ``novelty_gap`` **刻意使用不同信号**：本维看的是「聚合里有多少现成的、
    术语已经共识化的做法可以借用」，novelty_gap 看的是「与已有工作的相似度」，
    两者不是同一个数的镜像。
    """
    overlap = list(bundle.get("overlap") or [])
    close = [item for item in overlap if item["shared_count"] >= 3]
    cross_terms = list(bundle.get("cross_paper_terms") or [])
    fulltext = list(bundle.get("fulltext_paper_ids") or [])
    missing: list[str] = []
    if not bundle.get("methods"):
        missing.append("paper_cards.core_method（本次聚合卡片未提供可比较的方法描述）")

    score = _clamp(
        20
        + 15 * min(len(close), 3)
        + 10 * min(len(cross_terms), 3)
        + 10 * min(len(fulltext), 3)
    )
    best = max(overlap, key=lambda item: item["coverage"], default=None)
    rationale = (
        f"【方向：越高表示可借用的现成做法越多】idea 与 {len(overlap)} 篇论文的卡片字段做"
        f"实词比对：共享实词 >=3 的论文 {len(close)} 篇"
        + (f"（最相近论文 {best['paper_id']}，共享实词 {best['shared_terms'][:6]}）" if best else "")
        + f"；idea 术语中横跨 >=2 篇论文的共识词 {len(cross_terms)} 个"
        + (f"（{cross_terms[:6]}）" if cross_terms else "")
        + f"；通过全文闸门的论文 {len(fulltext)} 篇（可读到实现细节）。"
        + (f" 未提供信号：{'；'.join(missing)}。" if missing else "")
    )
    evidence: list[dict[str, Any]] = []
    for card in bundle.get("_cards") or []:
        evidence.append(_evidence_candidate(card, "core_method"))
    return {
        "key": "method_maturity",
        "label": "方法成熟度",
        "score": score,
        "rationale": rationale,
        "signals": {
            "max_word_coverage": round(float(bundle.get("max_overlap") or 0.0), 3),
            "close_paper_count": len(close),
            "close_paper_ids": [item["paper_id"] for item in close],
            "cross_paper_terms": cross_terms[:12],
            "cross_paper_term_count": len(cross_terms),
            "fulltext_paper_ids": fulltext,
            "per_paper_overlap": overlap,
            "idea_terms": bundle.get("idea_terms"),
            "methods_seen": list(bundle.get("methods") or [])[:6],
        },
        "signals_missing": missing,
        "formula": (
            "score = clamp(20 + 15*min(close_papers,3) + 10*min(cross_paper_terms,3) "
            "+ 10*min(fulltext_papers,3))"
        ),
        "evidence_candidates": evidence[:4],
    }


def score_novelty_gap(bundle: Mapping[str, Any]) -> dict[str, Any]:
    """与已有工作差异度：公式 ``100 - 100*最大实词覆盖``。"""
    max_overlap = float(bundle.get("max_overlap") or 0.0)
    missing: list[str] = []
    if not bundle.get("limitations"):
        missing.append("paper_cards.limitations（卡片未提供局限描述时差异度缺少参照）")
    score = _clamp(100 - 100 * max_overlap)
    rationale = (
        f"【方向：越高表示与聚合内已有工作差异越大】idea 与聚合内论文的最大实词覆盖为 "
        f"{max_overlap * 100:.1f}%，故差异度 = 100 - {max_overlap * 100:.1f} = {score}。"
        f"参照的局限条数：{len(bundle.get('limitations') or [])}。"
        + (f" 未提供信号：{'；'.join(missing)}。" if missing else "")
    )
    evidence: list[dict[str, Any]] = []
    for card in bundle.get("_cards") or []:
        evidence.append(_evidence_candidate(card, "limitations"))
    return {
        "key": "novelty_gap",
        "label": "与已有工作差异度",
        "score": score,
        "rationale": rationale,
        "signals": {
            "max_word_coverage": round(max_overlap, 3),
            "novelty_formula": "100 - 100*max_word_coverage",
            "limitation_count": len(bundle.get("limitations") or []),
        },
        "signals_missing": missing,
        "formula": "score = clamp(100 - 100*max_word_coverage)",
        "evidence_candidates": evidence[:4],
    }


def build_scoring_payload(bundle: Mapping[str, Any]) -> dict[str, Any]:
    """四维打分 + 显式加权总分（纯函数）。"""
    dims = [
        score_data_availability(bundle),
        score_compute_cost(bundle),
        score_method_maturity(bundle),
        score_novelty_gap(bundle),
    ]
    total = 0.0
    contributions: list[dict[str, Any]] = []
    for dim in dims:
        weight = WEIGHTS[dim["key"]]
        contribution = dim["score"] * weight
        total += contribution
        contributions.append(
            {
                "key": dim["key"],
                "label": dim["label"],
                "score": dim["score"],
                "weight": weight,
                "contribution": round(contribution, 4),
            }
        )
    return {
        "dimensions": dims,
        "total_score": round(total, 2),
        "scoring": {
            "weights": dict(WEIGHTS),
            "contributions": contributions,
            "formula": "total_score = Σ(维度分 × 权重)：" + " + ".join(
                f"{item['score']}×{item['weight']}" for item in contributions
            ),
            "owner": "rule_layer",
            "note": (
                "每维 score 均由 signals 与 formula 可手算复现；"
                "LLM（如启用）只提供建议分与说明，不参与 total_score"
            ),
            "dimension_direction": "四维均为 0–100 且越大越有利",
        },
        "weight_sum": round(sum(WEIGHTS.values()), 4),
    }


def llm_suggestion_prompt(bundle: Mapping[str, Any], dim: Mapping[str, Any]) -> str:
    """（可选）LLM 复核用 prompt：只让它给建议分与说明，不影响规则分。"""
    return (
        f"维度：{dim['label']}（规则分 {dim['score']}，依据：{dim['rationale']}）\n"
        f"真实信号：{dim['signals']}\n"
        "请只输出 JSON：{suggested_score:0-100, comment:'不超过 200 字的复核意见'}。"
        "禁止编造材料中没有的数据；信息不足时在 comment 中说明。"
    )


__all__ = [
    "DATASET_RE",
    "DATASET_STOPWORDS",
    "DEFAULT_DEMO_QUOTA_USD",
    "DEFAULT_MAX_LLM_COST_USD",
    "DIMENSIONS",
    "DIMENSION_CARD_FIELD",
    "MIN_DATASET_SPAN_WORDS",
    "PLACEHOLDERS",
    "WEIGHTS",
    "build_scoring_payload",
    "build_signal_bundle",
    "extract_dataset_mentions",
    "field_terms",
    "llm_suggestion_prompt",
    "real_terms",
    "score_compute_cost",
    "score_data_availability",
    "score_method_maturity",
    "score_novelty_gap",
]
