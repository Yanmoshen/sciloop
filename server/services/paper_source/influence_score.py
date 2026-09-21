# Copyright 2026 SciLoop contributors
# Licensed under the Apache License, Version 2.0 (http://www.apache.org/licenses/LICENSE-2.0)
"""影响力辅助分（WP04-T4）与 LLM 新颖性标签（WP04-T5）。

定位（计划书 §2.7.3）
--------------------
三项 ``venue(0.40) / citation_velocity(0.40) / code_heat(0.20)`` 只是**辅助展示分**：
只用于「影响力视图」排序与卡片展开说明，**不参与推荐视图的 ``rank_score``**。

红线
----
- ``institution_score``（机构声望）与 ``llm_novelty``（LLM 绝对分）**已退出所有分数计算**：
  ``institution_score`` 仅作展示元数据；``llm_novelty`` 仅作带不确定性区间的标签。
  ``_weighted_influence`` 只接受 ``INFLUENCE_DIMENSIONS``，传入其它维度直接抛错。
- 分项缺失一律置 ``null`` 并按剩余权重归一，同时给出 ``influence_coverage``；
  禁止用默认值或猜测值填充。
- 本模块不直接读数据库；GitHub stars 与引用数由调用方（feed.py / score_papers.py）注入。
"""

from __future__ import annotations

import importlib
import json
import logging
import math
import os
import re
from collections.abc import Mapping, Sequence
from statistics import mean
from typing import Any

from .ranking import coerce_date, dim_score, utc_today

logger = logging.getLogger("sciloop.wp04.influence")

INFLUENCE_DIMENSIONS: tuple[str, ...] = ("venue", "citation_velocity", "code_heat")

DEFAULT_INFLUENCE_WEIGHTS: dict[str, float] = {
    "venue": 0.40,
    "citation_velocity": 0.40,
    "code_heat": 0.20,
}

# venue_level(0-4) -> 0/25/50/75/100
VENUE_LEVEL_SCORES: dict[int, float] = {0: 0.0, 1: 25.0, 2: 50.0, 3: 75.0, 4: 100.0}

# venue 字符串来源 -> confidence（与 WP03 source_records 的来源分级一致）
VENUE_SOURCE_CONFIDENCE: dict[str, float] = {
    "semantic_scholar": 1.0,
    "s2": 1.0,
    "openalex": 0.8,
    "arxiv_comment": 0.6,
    "whitelist": 0.6,
}

DAYS_PER_MONTH = 30.4375
# 降级对数映射锚点：12 次引用/月（约 144 次/年）视为 100 分
CITATION_VELOCITY_LOG_ANCHOR = 12.0
# GitHub stars 对数归一锚点：1 万星视为 100 分
CODE_HEAT_STAR_ANCHOR = 10_000.0
# 分位数归一所需最少样本数，不足则降级为固定对数映射并降低 confidence
MIN_PERCENTILE_SAMPLES = 5

NOVELTY_PROMPT_VERSION = "novelty_v1"
DEFAULT_NOVELTY_TOLERANCE = 15.0
_NOVELTY_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "value": {"type": "number"},
        "low": {"type": "number"},
        "high": {"type": "number"},
        "note": {"type": "string"},
    },
    "required": ["value", "low", "high", "note"],
}

_RAW_STAR_PATHS: tuple[tuple[str, ...], ...] = (
    ("github", "stargazers_count"),
    ("github", "stars"),
    ("github", "stargazers"),
    ("code_stars",),
    ("github_stars",),
    ("sources", "github", "stargazers_count"),
    ("_sources", "github", "stargazers_count"),
)

_JSON_BLOCK_RE = re.compile(r"\{.*\}", re.DOTALL)


# --------------------------------------------------------------------------------------
# 三项辅助分
# --------------------------------------------------------------------------------------
def venue_dim(venue_level: Any, venue_source: Any = None) -> dict[str, Any]:
    """venue 分项：0-4 级 -> 0/25/50/75/100；等级未知（None）-> null。"""
    source = str(venue_source or "whitelist")
    if venue_level is None:
        return dim_score(None, source, 0.0)
    try:
        level = int(venue_level)
    except (TypeError, ValueError):
        return dim_score(None, source, 0.0)
    if level not in VENUE_LEVEL_SCORES:
        return dim_score(None, source, 0.0)
    return dim_score(VENUE_LEVEL_SCORES[level], source, VENUE_SOURCE_CONFIDENCE.get(source, 0.6))


def citation_velocity_value(citation_count: Any, published_at: Any, *, now: Any = None) -> float | None:
    """引用增速 = 引用数 / 已发表月数；引用数或发表时间缺失 -> None。"""
    if citation_count is None:
        return None
    published = coerce_date(published_at)
    if published is None:
        return None
    today = coerce_date(now) or utc_today()
    months = max(0.5, (today - published).days / DAYS_PER_MONTH)
    try:
        count = float(citation_count)
    except (TypeError, ValueError):
        return None
    return max(0.0, count) / months


def velocity_dim(
    velocity: float | None,
    *,
    reference_velocities: Sequence[float] | None = None,
    data_source: str = "semantic_scholar",
) -> dict[str, Any]:
    """citation_velocity 分项：样本充足时按批次分位数归一，否则降级为固定对数映射。"""
    source = str(data_source or "semantic_scholar")
    if velocity is None:
        return dim_score(None, source, 0.0)
    refs = [float(v) for v in (reference_velocities or []) if v is not None]
    if len(refs) >= MIN_PERCENTILE_SAMPLES:
        rank = sum(1 for item in refs if item <= velocity) / len(refs)
        return dim_score(100.0 * rank, f"{source}(percentile n={len(refs)})", 1.0)
    fallback = 100.0 * min(1.0, math.log1p(max(0.0, velocity)) / math.log1p(CITATION_VELOCITY_LOG_ANCHOR))
    return dim_score(fallback, f"{source}(log_fallback n={len(refs)})", 0.5)


def stars_from_raw(raw: Any) -> int | None:
    """从 ``papers.raw`` 里尽力提取 GitHub stars；取不到返回 None（不猜测）。"""
    if not isinstance(raw, Mapping):
        return None
    for path in _RAW_STAR_PATHS:
        node: Any = raw
        for key in path:
            if not isinstance(node, Mapping) or key not in node:
                node = None
                break
            node = node[key]
        if node is None:
            continue
        try:
            value = int(node)
        except (TypeError, ValueError):
            continue
        if value >= 0:
            return value
    return None


def code_heat_dim(stars: Any = None, *, code_url: Any = None) -> dict[str, Any]:
    """code_heat 分项。

    - 有 stars：对数归一（1 万星 = 100），confidence 1.0；
    - 无 stars 但解析到仓库链接：降级为布尔"是否开源"，100 / confidence 0.5；
    - 无链接：布尔 0 / confidence 0.5（明确标注来源，避免被当成"已核实不开源"）。
    """
    if stars is not None:
        try:
            star_count = max(0, int(stars))
        except (TypeError, ValueError):
            star_count = None
        if star_count is not None:
            value = 100.0 * min(1.0, math.log1p(star_count) / math.log1p(CODE_HEAT_STAR_ANCHOR))
            return dim_score(value, "github.stargazers_count", 1.0)
    if code_url:
        return dim_score(100.0, "code_url.boolean", 0.5)
    return dim_score(0.0, "code_url_absent.boolean", 0.5)


def parse_influence_weights(raw: Any = None) -> dict[str, float]:
    """解析影响力权重：显式入参 > ``INFLUENCE_WEIGHTS``（JSON）> 默认 0.40/0.40/0.20。"""
    payload = raw if raw is not None else os.getenv("INFLUENCE_WEIGHTS")
    weights: dict[str, float] = dict(DEFAULT_INFLUENCE_WEIGHTS)
    if isinstance(payload, str) and payload.strip():
        try:
            parsed = json.loads(payload)
        except json.JSONDecodeError:
            logger.warning("INFLUENCE_WEIGHTS 不是合法 JSON，回退默认权重")
            parsed = None
        payload = parsed
    if isinstance(payload, Mapping):
        merged = dict(DEFAULT_INFLUENCE_WEIGHTS)
        for key, value in payload.items():
            try:
                merged[str(key)] = float(value)
            except (TypeError, ValueError):
                logger.warning("忽略非法影响力权重项 %s=%r", key, value)
        weights = merged
    total = sum(weights.values())
    if total <= 0:
        weights = dict(DEFAULT_INFLUENCE_WEIGHTS)
        total = sum(weights.values())
    return {key: value / total for key, value in weights.items()}


def _weighted_influence(
    dims: Mapping[str, dict[str, Any]], weights: Mapping[str, float]
) -> tuple[float | None, float]:
    """按剩余权重归一求影响力分，返回 ``(influence_score, influence_coverage)``。

    守卫：只允许 ``INFLUENCE_DIMENSIONS`` 参与加权——``institution_score`` /
    ``llm_novelty`` 一旦被误接进来会立刻抛错。
    """
    unknown = set(dims) - set(INFLUENCE_DIMENSIONS)
    if unknown:
        raise ValueError(f"influence_score 出现不允许参与加权的维度: {sorted(unknown)}")
    total_weight = sum(weights.get(key, 0.0) for key in INFLUENCE_DIMENSIONS)
    weighted_sum = 0.0
    available_weight = 0.0
    for key in INFLUENCE_DIMENSIONS:
        value = (dims.get(key) or {}).get("value")
        if value is None:
            continue
        weight = weights.get(key, 0.0)
        available_weight += weight
        weighted_sum += weight * float(value)
    if available_weight <= 0:
        return None, 0.0
    score = weighted_sum / available_weight
    coverage = available_weight / total_weight if total_weight > 0 else 0.0
    return round(score, 3), round(coverage, 3)


def compute_influence(
    paper: Mapping[str, Any] | None,
    *,
    stars: Any = None,
    reference_velocities: Sequence[float] | None = None,
    now: Any = None,
    weights: Mapping[str, float] | str | None = None,
    citation_source: str | None = None,
) -> dict[str, Any]:
    """计算单篇论文的辅助影响力分：``{influence_score, score_breakdown, influence_coverage}``。"""
    paper = paper or {}
    resolved_weights = parse_influence_weights(weights)
    if stars is None:
        stars = stars_from_raw(paper.get("raw"))

    velocity = paper.get("citation_velocity")
    if velocity is None:
        velocity = citation_velocity_value(
            paper.get("citation_count"), paper.get("published_at"), now=now
        )
    else:
        try:
            velocity = float(velocity)
        except (TypeError, ValueError):
            velocity = citation_velocity_value(
                paper.get("citation_count"), paper.get("published_at"), now=now
            )

    breakdown = {
        "venue": venue_dim(paper.get("venue_level"), paper.get("venue_source")),
        "citation_velocity": velocity_dim(
            velocity,
            reference_velocities=reference_velocities,
            data_source=str(citation_source or paper.get("citation_source") or "semantic_scholar"),
        ),
        "code_heat": code_heat_dim(stars, code_url=paper.get("code_url")),
    }
    score, coverage = _weighted_influence(breakdown, resolved_weights)
    return {
        "influence_score": score,
        "score_breakdown": breakdown,
        "influence_coverage": coverage,
        "citation_velocity_value": None if velocity is None else round(float(velocity), 3),
    }


def compute_influence_batch(
    papers: Sequence[Mapping[str, Any]],
    *,
    stars: Sequence[Any] | None = None,
    now: Any = None,
    weights: Mapping[str, float] | str | None = None,
) -> list[dict[str, Any]]:
    """批量计算：citation_velocity 以本批次为参照做分位数归一。"""
    items = list(papers)
    velocities: list[float | None] = []
    for paper in items:
        raw_velocity = paper.get("citation_velocity")
        if raw_velocity is not None:
            try:
                velocities.append(float(raw_velocity))
                continue
            except (TypeError, ValueError):
                pass
        velocities.append(
            citation_velocity_value(paper.get("citation_count"), paper.get("published_at"), now=now)
        )
    reference = [v for v in velocities if v is not None]
    results: list[dict[str, Any]] = []
    for index, paper in enumerate(items):
        item_stars = stars[index] if stars is not None else None
        resolved = dict(paper)
        if velocities[index] is not None:
            resolved["citation_velocity"] = velocities[index]
        results.append(
            compute_influence(
                resolved,
                stars=item_stars,
                reference_velocities=reference,
                now=now,
                weights=weights,
            )
        )
    return results


# --------------------------------------------------------------------------------------
# LLM 新颖性标签（仅展示，不参与任何加权）
# --------------------------------------------------------------------------------------
def novelty_enabled() -> bool:
    """``LLM_NOVELTY_ENABLED`` 开关（默认开启）。"""
    return str(os.getenv("LLM_NOVELTY_ENABLED", "1")).strip().lower() not in {"0", "false", "no", "off", ""}


def novelty_tolerance() -> float:
    """两次调用允许的最大差值；超过则 ``stable=False``。"""
    raw = os.getenv("LLM_NOVELTY_STABILITY_TOLERANCE")
    if raw:
        try:
            return float(raw)
        except ValueError:
            logger.warning("LLM_NOVELTY_STABILITY_TOLERANCE 非法: %r", raw)
    return DEFAULT_NOVELTY_TOLERANCE


def build_novelty_messages(title: Any, abstract: Any) -> list[dict[str, str]]:
    """构造新颖性评估消息；要求 LLM 同时给出区间与一句话依据。"""
    system = (
        "你是科研文献评估助手。请针对论文标题与摘要预估其新颖性，输出 JSON："
        '{"value": 0-100 的整数, "low": 下界, "high": 上界, "note": "一句话依据"}。'
        "note 必须写具体依据（与哪些已有工作相比新在哪里），不得为空；"
        "不确定时请拉大 low/high 区间，禁止编造事实。"
    )
    user = f"标题：{title or ''}\n\n摘要：{abstract or ''}"
    return [{"role": "system", "content": system}, {"role": "user", "content": user}]


def _resolve_chat_fn() -> Any:
    """解析 WP02 的统一 LLM 入口（只读，不修改其实现）。"""
    for module_name, attr in (
        ("llm", "chat"),
        ("llm.adapter", "chat"),
        ("llm", "achat"),
        ("llm.router", "chat"),
    ):
        try:
            module = importlib.import_module(module_name)
        except Exception:  # noqa: BLE001 - 适配层未就绪时必须降级而不是崩溃
            continue
        candidate = getattr(module, attr, None)
        if callable(candidate):
            return candidate
    return None


def _attempt_kwargs(model_ref: Any) -> list[dict[str, Any]]:
    """按"参数由多到少"生成 chat 调用参数，兼容 WP02 适配层的签名演进。"""
    full: dict[str, Any] = {"temperature": 0.0, "max_tokens": 512, "json_schema": _NOVELTY_SCHEMA}
    if model_ref:
        full["model_ref"] = model_ref
    variants: list[dict[str, Any]] = [dict(full)]
    no_schema = {key: value for key, value in full.items() if key != "json_schema"}
    variants.append(no_schema)
    variants.append({key: value for key, value in no_schema.items() if key == "model_ref"})
    variants.append({})
    unique: list[dict[str, Any]] = []
    for variant in variants:
        if variant not in unique:
            unique.append(variant)
    return unique


async def _invoke_chat(chat_fn: Any, messages: Sequence[Mapping[str, str]], model_ref: Any) -> Any:
    """调用 WP02 的 chat；签名差异时逐级降参，避免并行开发期互相阻塞。"""
    last_error: Exception | None = None
    for kwargs in _attempt_kwargs(model_ref):
        try:
            return await chat_fn(list(messages), **kwargs)
        except TypeError as exc:  # 签名不匹配 -> 降参重试
            last_error = exc
            continue
    raise last_error or RuntimeError("WP02 chat 入口签名不兼容，无法调用")


def _result_text(result: Any) -> str | None:
    """从 LLMResult（对象或 dict）中取出文本内容（WP02 的字段名是 content）。"""
    if result is None:
        return None
    if isinstance(result, str):
        return result
    for attr in ("text", "content", "output_text", "message"):
        value = getattr(result, attr, None) if not isinstance(result, Mapping) else result.get(attr)
        if isinstance(value, str) and value.strip():
            return value
        if isinstance(value, Mapping) and isinstance(value.get("content"), str):
            return str(value["content"])
    for attr in ("choices", "outputs"):
        value = getattr(result, attr, None) if not isinstance(result, Mapping) else result.get(attr)
        if isinstance(value, Sequence) and value:
            first = value[0]
            if isinstance(first, Mapping):
                message = first.get("message") or {}
                if isinstance(message, Mapping) and isinstance(message.get("content"), str):
                    return str(message["content"])
                if isinstance(first.get("text"), str):
                    return str(first["text"])
            text = getattr(first, "text", None)
            if isinstance(text, str):
                return text
    return None


def _result_model(result: Any) -> str | None:
    """从 LLMResult 中取出实际使用的模型（用于如实标注标签来源）。"""
    for attr in ("model_ref", "model_id"):
        value = getattr(result, attr, None) if not isinstance(result, Mapping) else result.get(attr)
        if isinstance(value, str) and value.strip():
            return value
    return None


def _result_parsed(result: Any) -> Any:
    """取出 json_schema 校验后的结构化结果（WP02 ``LLMResult.parsed``）。"""
    if isinstance(result, Mapping):
        return result.get("parsed")
    return getattr(result, "parsed", None)


def parse_novelty_payload(payload: Any) -> dict[str, Any] | None:
    """解析 LLM 返回的新颖性 JSON（接受已解析的 Mapping 或原始文本）；失败返回 None。"""
    if isinstance(payload, Mapping):
        candidate: Any = payload
    elif payload:
        raw = str(payload).strip()
        if raw.startswith("```"):
            raw = raw.strip("`")
            raw = raw.split("\n", 1)[1] if "\n" in raw else raw
        candidate = None
        try:
            candidate = json.loads(raw)
        except json.JSONDecodeError:
            match = _JSON_BLOCK_RE.search(raw)
            if match is not None:
                try:
                    candidate = json.loads(match.group(0))
                except json.JSONDecodeError:
                    candidate = None
    else:
        return None
    if not isinstance(candidate, Mapping):
        return None
    try:
        value = float(candidate["value"])
    except (KeyError, TypeError, ValueError):
        return None

    def _bound(key: str, fallback: float) -> float:
        try:
            return float(candidate[key])  # type: ignore[index]
        except (KeyError, TypeError, ValueError):
            return fallback

    note = candidate.get("note")
    low = _bound("low", value)
    high = _bound("high", value)
    low, high = min(low, value, high), max(low, value, high)
    return {
        "value": max(0.0, min(100.0, value)),
        "low": max(0.0, min(100.0, low)),
        "high": max(0.0, min(100.0, high)),
        "note": str(note).strip() if note else None,
    }


def _unavailable_tag(reason: str, model_ref: Any = None) -> dict[str, Any]:
    return {
        "value": None,
        "low": None,
        "high": None,
        "note": None,
        "model": model_ref,
        "stable": None,
        "display": "unavailable",
        "prompt_version": NOVELTY_PROMPT_VERSION,
        "reason": reason,
    }


def merge_novelty_runs(
    payloads: Sequence[Mapping[str, Any]],
    *,
    model_ref: Any = None,
    tolerance: float | None = None,
) -> dict[str, Any]:
    """合并两次调用的结果：差值 > tolerance -> ``stable=False``（前端只展示区间）。"""
    valid = [item for item in payloads if item and item.get("value") is not None]
    if not valid:
        return _unavailable_tag("llm_returned_no_valid_payload", model_ref)
    limit = novelty_tolerance() if tolerance is None else float(tolerance)
    values = [float(item["value"]) for item in valid]
    delta = (max(values) - min(values)) if len(values) > 1 else None
    stable: bool | None = None if delta is None else delta <= limit
    note = next((item.get("note") for item in valid if item.get("note")), None)
    low = min(float(item["low"]) for item in valid)
    high = max(float(item["high"]) for item in valid)
    return {
        "value": round(mean(values), 3),
        "low": round(min(low, min(values)), 3),
        "high": round(max(high, max(values)), 3),
        "note": note,
        "model": model_ref,
        "stable": stable,
        "display": "point" if stable else "range",
        "prompt_version": NOVELTY_PROMPT_VERSION,
        "delta": None if delta is None else round(delta, 3),
        "calls": len(valid),
    }


async def compute_llm_novelty(
    title: Any,
    abstract: Any,
    *,
    chat_fn: Any = None,
    model_ref: Any = None,
    tolerance: float | None = None,
    calls: int = 2,
) -> dict[str, Any]:
    """LLM 新颖性标签。结果只写入 papers.llm_novelty 系列字段，**不参与任何加权**。

    同一输入调用 ``calls`` 次（默认 2）：差值 > ``LLM_NOVELTY_STABILITY_TOLERANCE``(15) 时
    ``stable=False``，调用方/前端只展示区间。任何异常都降级为 unavailable，绝不编造数值。
    """
    if not novelty_enabled():
        return _unavailable_tag("llm_novelty_disabled", model_ref)
    if not title and not abstract:
        return _unavailable_tag("no_title_or_abstract", model_ref)
    resolved_chat = chat_fn if callable(chat_fn) else _resolve_chat_fn()
    if resolved_chat is None:
        return _unavailable_tag("llm_adapter_unavailable", model_ref)
    messages = build_novelty_messages(title, abstract)
    payloads: list[dict[str, Any]] = []
    observed_model: str | None = None
    for _ in range(max(1, int(calls))):
        try:
            result = await _invoke_chat(resolved_chat, messages, model_ref)
        except Exception as exc:  # noqa: BLE001 - 外部依赖失败只降级，不伪造分数
            logger.warning("llm novelty 调用失败: %s", exc)
            continue
        observed_model = _result_model(result) or observed_model
        parsed = parse_novelty_payload(_result_parsed(result))
        if parsed is None:
            parsed = parse_novelty_payload(_result_text(result))
        if parsed is not None:
            payloads.append(parsed)
    if not payloads:
        return _unavailable_tag("llm_call_failed", model_ref)
    return merge_novelty_runs(payloads, model_ref=observed_model or model_ref, tolerance=tolerance)


def novelty_tag_for_response(tag: Mapping[str, Any] | None) -> dict[str, Any]:
    """转成 API 形状：不稳定时**只给区间**（``value=null``），并显式给出 display。"""
    if not tag:
        return _unavailable_tag("not_computed")
    stable = tag.get("stable")
    payload = {
        "value": None if stable is False else tag.get("value"),
        "low": tag.get("low"),
        "high": tag.get("high"),
        "stable": stable,
        "note": tag.get("note"),
        "model": tag.get("model"),
        "display": tag.get("display") or ("point" if stable else "range"),
        "prompt_version": tag.get("prompt_version") or NOVELTY_PROMPT_VERSION,
    }
    if stable is False:
        payload["note"] = payload["note"] or "两次调用差异超过阈值，仅展示区间，不展示单点分"
    return payload
