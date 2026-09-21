# Copyright 2026 SciLoop contributors
# Licensed under the Apache License, Version 2.0 (http://www.apache.org/licenses/LICENSE-2.0)
"""排序四维（WP04-T1）：``relevance`` / ``recency`` / ``citation_trend`` / ``evidence_completeness``。

主口径（计划书 §2.7.2）
----------------------
``rank_score = Σ(w_i · v_i) / Σw_available``，缺失分项**置 null 并按剩余权重归一**，
同时记录 ``score_coverage = Σw_available / Σw_all``。**禁止用默认值或猜测值填充缺失项。**

红线
----
- ``institution_score`` 与 ``llm_novelty`` **不参与**本模块任何加权计算；
  ``_weighted_total`` 只接受 ``RANK_DIMENSIONS`` 内的维度，出现其它维度直接报错。
- 本模块不访问数据库、不发网络请求：所有输入（引用曲线、解析状态、时间）由调用方注入，
  便于单元测试与复算。
"""

from __future__ import annotations

import json
import logging
import math
import os
import re
from collections import Counter
from collections.abc import Iterable, Mapping, Sequence
from datetime import UTC, date, datetime
from typing import Any

logger = logging.getLogger("sciloop.wp04.ranking")

RANK_DIMENSIONS: tuple[str, ...] = (
    "relevance",
    "recency",
    "citation_trend",
    "evidence_completeness",
)

DEFAULT_RANK_WEIGHTS: dict[str, float] = {
    "relevance": 0.40,
    "recency": 0.25,
    "citation_trend": 0.20,
    "evidence_completeness": 0.15,
}

DEFAULT_RECENCY_HALFLIFE_DAYS = 180

# citation_trend 的斜率饱和尺度：近两年每年净增 20 次引用 => 50 分，净增 60 次 => 75 分。
# 采用固定饱和映射而非批次内 min-max，保证同一篇论文在任何批次里得分可复算（可复核性要求）。
CITATION_TREND_SLOPE_SCALE = 20.0

# evidence_completeness 的取数覆盖度考察字段（与附录 A.2 的 papers 列一致）
EVIDENCE_METADATA_FIELDS: tuple[str, ...] = (
    "abstract",
    "authors",
    "published_at",
    "venue",
    "doi",
    "citation_count",
    "code_url",
)

# 解析状态 -> 全文可用度系数
_PARSE_STATUS_FACTOR: dict[str, float] = {
    "ok": 1.0,
    "partial": 0.6,
    "unavailable": 0.15,
    "failed": 0.0,
}

_FULLTEXT_WEIGHT = 0.70
_METADATA_WEIGHT = 0.30

_TOKEN_RE = re.compile(r"[a-z0-9][a-z0-9\-_]{1,}")


# --------------------------------------------------------------------------------------
# 基础工具
# --------------------------------------------------------------------------------------
def _clamp(value: float, low: float = 0.0, high: float = 100.0) -> float:
    return max(low, min(high, value))


def _round(value: float | None, digits: int = 3) -> float | None:
    return None if value is None else round(float(value), digits)


def dim_score(value: float | None, source: str, confidence: float) -> dict[str, Any]:
    """构造 ``{value, source, confidence}`` 分项；value 为 None 时 confidence 必须为 0。"""
    if value is None:
        return {"value": None, "source": source, "confidence": 0.0}
    return {"value": _round(_clamp(value)), "source": source, "confidence": _round(confidence, 3)}


def coerce_date(value: Any) -> date | None:
    """把 date / datetime / ISO 字符串（含 ``2025-01``）转成 date，失败返回 None。"""
    if value is None or value == "":
        return None
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    text = str(value).strip()
    if not text:
        return None
    normalized = text.replace("Z", "+00:00")
    try:
        parsed = datetime.fromisoformat(normalized)
    except ValueError:
        parsed = None
    if parsed is not None:
        return parsed.date()
    for fmt in ("%Y-%m-%d", "%Y/%m/%d", "%Y-%m", "%Y", "%d %B %Y", "%B %d, %Y"):
        try:
            return datetime.strptime(text, fmt).date()
        except ValueError:
            continue
    return None


def utc_today() -> date:
    return datetime.now(UTC).date()


def tokenize(text: Any) -> list[str]:
    """小写化并切成 token（保留字母数字与连字符）。"""
    if not text:
        return []
    return _TOKEN_RE.findall(str(text).lower())


def bm25_scores(
    query_tokens: Sequence[str],
    doc_tokens: Sequence[Sequence[str]],
    *,
    k1: float = 1.5,
    b: float = 0.75,
) -> list[float]:
    """纯 Python BM25（Okapi）实现，返回每个文档的原始得分。"""
    if not query_tokens or not doc_tokens:
        return [0.0 for _ in doc_tokens]
    total = len(doc_tokens)
    doc_freq: Counter[str] = Counter()
    for tokens in doc_tokens:
        doc_freq.update(set(tokens))
    lengths = [len(tokens) for tokens in doc_tokens]
    avgdl = (sum(lengths) / total) if total else 0.0
    if avgdl <= 0:
        return [0.0 for _ in doc_tokens]
    idf = {
        term: math.log(1 + (total - freq + 0.5) / (freq + 0.5)) for term, freq in doc_freq.items()
    }
    scores: list[float] = []
    for tokens, length in zip(doc_tokens, lengths, strict=True):
        term_freq = Counter(tokens)
        doc_len = length or 1
        score = 0.0
        for term in query_tokens:
            freq = term_freq.get(term, 0)
            if not freq:
                continue
            denominator = freq + k1 * (1 - b + b * doc_len / avgdl)
            score += idf.get(term, 0.0) * (freq * (k1 + 1)) / denominator
        scores.append(score)
    return scores


# --------------------------------------------------------------------------------------
# 四维
# --------------------------------------------------------------------------------------
def compute_relevance(
    text: Any,
    query: Any,
    *,
    reference_texts: Sequence[Any] | None = None,
) -> dict[str, Any]:
    """检索相关性 0-100。

    - 无查询（``query`` 为空）→ ``value=None``（前端提示"未提供查询，相关性不参与排序"）。
    - 有查询且有参照集合 → BM25 得分按本批次最大值归一（跨论文可比）。
    - 有查询但只有单篇 → BM25 无法自归一，退化为查询词覆盖率，confidence=0.5 并如实标注来源。
    """
    query_tokens = tokenize(query)
    if not query_tokens:
        return dim_score(None, "no_query", 0.0)
    doc_tokens = tokenize(text)
    if not doc_tokens:
        return dim_score(None, "no_text", 0.0)

    if reference_texts:
        corpus = [doc_tokens] + [tokenize(item) for item in reference_texts]
        scores = bm25_scores(query_tokens, corpus)
        best = max(scores) if scores else 0.0
        if best > 0:
            return dim_score(100.0 * max(0.0, scores[0]) / best, "bm25(batch_normalized)", 0.9)
        return dim_score(0.0, "bm25(batch_normalized)", 0.9)

    unique_query = set(query_tokens)
    covered = len(unique_query & set(doc_tokens))
    return dim_score(100.0 * covered / len(unique_query), "term_overlap(single_doc)", 0.5)


def compute_recency(
    published_at: Any,
    *,
    now: Any = None,
    halflife_days: float | None = None,
) -> dict[str, Any]:
    """时效性：``exp(-Δdays / halflife) * 100``；缺 ``published_at`` → null。"""
    published = coerce_date(published_at)
    if published is None:
        return dim_score(None, "published_at", 0.0)
    today = coerce_date(now) or utc_today()
    halflife = float(halflife_days or DEFAULT_RECENCY_HALFLIFE_DAYS)
    if halflife <= 0:
        halflife = float(DEFAULT_RECENCY_HALFLIFE_DAYS)
    delta_days = max(0, (today - published).days)
    return dim_score(100.0 * math.exp(-delta_days / halflife), "published_at", 1.0)


def normalize_counts_by_year(counts_by_year: Any) -> list[tuple[int, int]]:
    """把 OpenAlex ``counts_by_year`` 归一为 ``[(year, cited_by_count), ...]`` 升序列表。"""
    if not counts_by_year:
        return []
    pairs: list[tuple[int, int]] = []
    if isinstance(counts_by_year, Mapping):
        items: Iterable[Any] = counts_by_year.items()
        for year, count in items:
            try:
                pairs.append((int(year), int(count)))
            except (TypeError, ValueError):
                continue
    elif isinstance(counts_by_year, Sequence) and not isinstance(counts_by_year, (str, bytes)):
        for item in counts_by_year:
            # 同时接受 OpenAlex 原始形状 [{year, cited_by_count}] 与已归一形状 [(year, count)]
            if isinstance(item, Mapping):
                year: Any = item.get("year")
                count: Any = item.get("cited_by_count", item.get("citedByCount"))
            elif isinstance(item, (tuple, list)) and len(item) >= 2:
                year, count = item[0], item[1]
            else:
                continue
            try:
                pairs.append((int(year), int(count)))
            except (TypeError, ValueError):
                continue
    return sorted(dict(pairs).items())


def compute_citation_trend(
    counts_by_year: Any,
    *,
    reference_year: int | None = None,
    slope_scale: float | None = None,
) -> dict[str, Any]:
    """引用趋势 0-100：取**最近两个有数据的年份**的引用数斜率（次/年）做饱和归一。

    - 数据少于 2 个年份（新论文还没有跨年引用曲线）→ ``value=None``（如实缺失）。
    - 斜率 ≤ 0 → 0 分；``value = 100 · slope / (slope + slope_scale)``，恒定可复算。
    """
    series = normalize_counts_by_year(counts_by_year)
    if len(series) < 2:
        return dim_score(None, "openalex.counts_by_year", 0.0)
    (prev_year, prev_count), (last_year, last_count) = series[-2], series[-1]
    span = max(1, last_year - prev_year)
    slope = (last_count - prev_count) / span
    scale = float(slope_scale or CITATION_TREND_SLOPE_SCALE)
    if scale <= 0:
        scale = CITATION_TREND_SLOPE_SCALE
    value = 0.0 if slope <= 0 else 100.0 * slope / (slope + scale)
    confidence = 0.8 if len(series) >= 3 else 0.6
    return dim_score(value, "openalex.counts_by_year", confidence)


# 原始响应里 counts_by_year 可能出现的路径（WP03 多源合并后 papers.raw 的形状差异）
_RAW_COUNTS_PATHS: tuple[tuple[str, ...], ...] = (
    ("openalex", "counts_by_year"),
    ("counts_by_year",),
    ("openalex", "work", "counts_by_year"),
    ("sources", "openalex", "counts_by_year"),
    ("_sources", "openalex", "counts_by_year"),
    ("raw_openalex", "counts_by_year"),
)


def counts_by_year_from_raw(raw: Any) -> list[tuple[int, int]]:
    """从 ``papers.raw`` 中尽力提取 OpenAlex ``counts_by_year``；取不到返回空列表。"""
    if not isinstance(raw, Mapping):
        return []
    for path in _RAW_COUNTS_PATHS:
        node: Any = raw
        for key in path:
            if not isinstance(node, Mapping) or key not in node:
                node = None
                break
            node = node[key]
        if node:
            series = normalize_counts_by_year(node)
            if series:
                return series
    return []


def metadata_coverage(paper: Mapping[str, Any] | None) -> float:
    """取数覆盖度：关键元数据字段中"取到值"的比例（0-1）。"""
    if not paper:
        return 0.0
    if not EVIDENCE_METADATA_FIELDS:
        return 0.0
    present = 0
    for field in EVIDENCE_METADATA_FIELDS:
        value = paper.get(field)
        if value is None or value == "" or value == [] or value == {}:
            continue
        present += 1
    return present / len(EVIDENCE_METADATA_FIELDS)


def compute_evidence_completeness(
    *,
    parse_status: Any = None,
    coverage: Any = None,
    metadata_coverage_value: float | None = None,
    has_document: bool = False,
    is_parsed: bool = False,
) -> dict[str, Any]:
    """证据完整度 0-100 = ``(0.7 × 全文可用度 + 0.3 × 取数覆盖度)``。

    全文可用度来自 WP05 的 ``paper_documents.parse_status`` 与 ``coverage``；
    当 ``paper_documents`` 尚未就绪时按 ``papers.is_parsed`` 兜底并把 confidence 降为 0.5、
    在 source 上如实标注，绝不假装全文可用。
    """
    meta = 0.0 if metadata_coverage_value is None else _clamp(metadata_coverage_value, 0.0, 1.0)
    status = str(parse_status).lower() if parse_status else None
    source = "fulltext_parser+metadata_fields"
    confidence = 1.0
    fulltext_factor: float

    if has_document and status in _PARSE_STATUS_FACTOR:
        base = _PARSE_STATUS_FACTOR[status]
        ratio = None if coverage is None else _clamp(float(coverage), 0.0, 1.0)
        if status == "ok":
            fulltext_factor = base * (1.0 if ratio is None else 0.4 + 0.6 * ratio)
        else:
            fulltext_factor = base * (1.0 if ratio is None else ratio)
    elif is_parsed:
        # 兜底：WP05 记录未就绪，只能按 is_parsed 布尔值保守估计
        fulltext_factor = 0.7
        source = "papers.is_parsed+metadata_fields"
        confidence = 0.5
    else:
        fulltext_factor = 0.0
        source = "papers.is_parsed+metadata_fields"
        confidence = 0.5

    value = 100.0 * (_FULLTEXT_WEIGHT * fulltext_factor + _METADATA_WEIGHT * meta)
    return dim_score(value, source, confidence)


# --------------------------------------------------------------------------------------
# 加权
# --------------------------------------------------------------------------------------
def parse_rank_weights(raw: Any = None) -> dict[str, float]:
    """解析权重：显式入参 > 环境变量 ``RANK_WEIGHTS``（JSON）> 默认 0.40/0.25/0.20/0.15。"""
    payload = raw if raw is not None else os.getenv("RANK_WEIGHTS")
    weights: dict[str, float] = dict(DEFAULT_RANK_WEIGHTS)
    if isinstance(payload, str) and payload.strip():
        try:
            parsed = json.loads(payload)
        except json.JSONDecodeError:
            logger.warning("RANK_WEIGHTS 不是合法 JSON，回退默认权重")
            parsed = None
        payload = parsed
    if isinstance(payload, Mapping):
        merged = dict(DEFAULT_RANK_WEIGHTS)
        for key, value in payload.items():
            try:
                merged[str(key)] = float(value)
            except (TypeError, ValueError):
                logger.warning("忽略非法权重项 %s=%r", key, value)
        weights = merged
    total = sum(weights.values())
    if total <= 0:
        logger.warning("RANK_WEIGHTS 权重和 <=0，回退默认权重")
        weights = dict(DEFAULT_RANK_WEIGHTS)
        total = sum(weights.values())
    return {key: value / total for key, value in weights.items()}


def _weighted_total(
    dims: Mapping[str, dict[str, Any]],
    weights: Mapping[str, float],
    allowed: Sequence[str],
    label: str,
) -> tuple[float | None, float, dict[str, float], list[str]]:
    """按剩余权重归一求加权总分，返回 ``(总分, 可用权重占比, 实际权重, 缺失维度)``。

    红线守卫：只允许 ``allowed`` 中的维度参与加权；传入 ``institution_score`` /
    ``llm_novelty`` 等展示型字段会直接抛错，防止日后被误接进排序。
    """
    unknown = set(dims) - set(allowed)
    if unknown:
        raise ValueError(f"{label} 出现不允许参与加权的维度: {sorted(unknown)}")
    available: dict[str, float] = {}
    missing: list[str] = []
    total_weight = sum(weights.get(key, 0.0) for key in allowed)
    weighted_sum = 0.0
    for key in allowed:
        weight = weights.get(key, 0.0)
        item = dims.get(key) or {}
        value = item.get("value")
        if value is None:
            missing.append(key)
            continue
        available[key] = weight
        weighted_sum += weight * float(value)
    available_weight = sum(available.values())
    if available_weight <= 0:
        return None, 0.0, available, missing
    score = weighted_sum / available_weight
    coverage = available_weight / total_weight if total_weight > 0 else 0.0
    return _round(score), _round(coverage), available, missing


def compute_rank(
    paper: Mapping[str, Any] | None,
    *,
    query: Any = None,
    precomputed: Mapping[str, dict[str, Any]] | None = None,
    counts_by_year: Any = None,
    document: Mapping[str, Any] | None = None,
    metadata_coverage_value: float | None = None,
    now: Any = None,
    weights: Mapping[str, float] | str | None = None,
    halflife_days: float | None = None,
    reference_texts: Sequence[Any] | None = None,
) -> dict[str, Any]:
    """计算单篇论文的 ``rank_score`` 与四维明细。

    返回 ``{rank_score, rank_breakdown, score_coverage, missing_dimensions, weights_used}``。
    ``rank_breakdown`` 永远包含四个维度，每项都是 ``{value, source, confidence}``；
    取不到的维度 ``value=None, confidence=0``。
    """
    paper = paper or {}
    resolved_weights = parse_rank_weights(weights)
    halflife = halflife_days
    if halflife is None:
        env_halflife = os.getenv("RECENCY_HALFLIFE_DAYS")
        if env_halflife:
            try:
                halflife = float(env_halflife)
            except ValueError:
                logger.warning("RECENCY_HALFLIFE_DAYS 非法: %r", env_halflife)

    pre = precomputed or {}
    if "relevance" in pre:
        relevance = pre["relevance"]
    else:
        text = " ".join(
            str(part) for part in (paper.get("title"), paper.get("abstract")) if part
        )
        relevance = compute_relevance(text, query, reference_texts=reference_texts)

    recency = pre.get("recency") or compute_recency(
        paper.get("published_at"), now=now, halflife_days=halflife
    )

    if "citation_trend" in pre:
        citation_trend = pre["citation_trend"]
    else:
        series = counts_by_year
        if series is None:
            series = counts_by_year_from_raw(paper.get("raw"))
        reference_year = (coerce_date(now) or utc_today()).year
        citation_trend = compute_citation_trend(series, reference_year=reference_year)

    if "evidence_completeness" in pre:
        evidence = pre["evidence_completeness"]
    else:
        doc = document if document is not None else paper.get("document")
        doc = doc if isinstance(doc, Mapping) else {}
        meta = (
            metadata_coverage_value
            if metadata_coverage_value is not None
            else paper.get("metadata_coverage")
        )
        if meta is None:
            meta = metadata_coverage(paper)
        evidence = compute_evidence_completeness(
            parse_status=doc.get("parse_status"),
            coverage=doc.get("coverage"),
            metadata_coverage_value=float(meta),
            has_document=bool(doc),
            is_parsed=bool(paper.get("is_parsed")),
        )

    breakdown: dict[str, dict[str, Any]] = {
        "relevance": relevance,
        "recency": recency,
        "citation_trend": citation_trend,
        "evidence_completeness": evidence,
    }
    score, coverage, available, missing = _weighted_total(
        breakdown, resolved_weights, RANK_DIMENSIONS, "rank_score"
    )
    return {
        "rank_score": score,
        "rank_breakdown": breakdown,
        "score_coverage": coverage,
        "missing_dimensions": missing,
        "weights_used": dict(available.items()),
    }


def compute_rank_batch(
    papers: Sequence[Mapping[str, Any]],
    *,
    query: Any = None,
    documents: Sequence[Mapping[str, Any] | None] | None = None,
    counts_by_year: Sequence[Any] | None = None,
    metadata_coverage_values: Sequence[float | None] | None = None,
    now: Any = None,
    weights: Mapping[str, float] | str | None = None,
    halflife_days: float | None = None,
) -> list[dict[str, Any]]:
    """批量计算（推荐用法）：``relevance`` 的 BM25 得分在本批次内归一，跨论文可比。"""
    items = list(papers)
    query_tokens = tokenize(query)
    relevance_dims: list[dict[str, Any] | None] = [None] * len(items)
    if query_tokens:
        corpus = [
            tokenize(" ".join(str(part) for part in (p.get("title"), p.get("abstract")) if part))
            for p in items
        ]
        scores = bm25_scores(query_tokens, corpus) if corpus else []
        best = max(scores) if scores else 0.0
        for index, tokens in enumerate(corpus):
            if not tokens:
                relevance_dims[index] = dim_score(None, "no_text", 0.0)
            elif best > 0:
                relevance_dims[index] = dim_score(
                    100.0 * max(0.0, scores[index]) / best, "bm25(batch_normalized)", 0.9
                )
            else:
                relevance_dims[index] = dim_score(0.0, "bm25(batch_normalized)", 0.9)

    results: list[dict[str, Any]] = []
    for index, paper in enumerate(items):
        precomputed: dict[str, dict[str, Any]] = {}
        if relevance_dims[index] is not None:
            precomputed["relevance"] = relevance_dims[index]  # type: ignore[assignment]
        document = documents[index] if documents is not None else None
        series = counts_by_year[index] if counts_by_year is not None else None
        meta = metadata_coverage_values[index] if metadata_coverage_values is not None else None
        results.append(
            compute_rank(
                paper,
                query=query,
                precomputed=precomputed,
                counts_by_year=series,
                document=document,
                metadata_coverage_value=meta,
                now=now,
                weights=weights,
                halflife_days=halflife_days,
            )
        )
    return results
