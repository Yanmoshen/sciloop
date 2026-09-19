# Copyright 2026 SciLoop contributors
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
"""盲评隔离与匿名化（WP12-T1，附录 D.3 / contracts.blind_review_rules）。

红线（**不得放宽**）
--------------------

1. **隔离**：``generator_model_ref != reviewer_model_ref`` 必须成立。校验**复用**
   WP02 的 :func:`app.llm.router.resolve_pair`，本模块不重写隔离逻辑；无法满足时抛
   :class:`IsolationViolation`（同时是 :class:`StageError`）让 ``plan_review`` 环节
   **直接失败**，绝不静默回退同一模型。
2. **匿名化**：送进评审 prompt 的载荷不得包含 provider / model / 生成时间 /
   原始下标 / 推荐项。服务端生成 ``candidate_alias``（A/B/C…），保存
   ``shuffle_seed`` 与 alias→原始下标映射；模型只看别名。
3. **可复现**：同一 ``shuffle_seed`` 与同一输入必须得到完全一致的别名顺序
   （``random.Random(seed).shuffle``，Mersenne Twister，跨进程稳定）。

本模块**不调用 LLM**：只负责「解析隔离路由 + 构造匿名载荷 + 还原别名→下标」。
"""

from __future__ import annotations

import hashlib
import json
import logging
import random
import re
from collections.abc import Mapping, Sequence
from datetime import UTC, datetime
from typing import Any

from app.llm.errors import IsolationViolation
from app.services.pipeline.stages.base import StageError

logger = logging.getLogger("sciloop.review.blind")

#: 匿名化口径版本（写入 ``review_calibrations.anonymization_version``）
ANONYMIZATION_VERSION = "blind-v1"

#: 候选别名前缀（A/B/C…；超过 26 个时退化为 C27、C28…）
ALIAS_LETTERS = "ABCDEFGHIJKLMNOPQRSTUVWXYZ"

#: 生成模型 / 评审模型的「路由」两侧环节名（附录 D.3：plan 生成、plan_review 评审）
GENERATOR_STAGE = "plan"
REVIEWER_STAGE = "plan_review"

#: 候选方法中**只允许**透传给评审模型的字段（白名单，其余一律剥离）
ALLOWED_METHOD_FIELDS: tuple[str, ...] = (
    "title",
    "summary",
    "template_id",
    "params",
    "expected_metrics",
    "rationale",
)

#: 明确禁止出现在评审载荷中的字段名（命中即视为泄漏）
FORBIDDEN_FIELD_NAMES: tuple[str, ...] = (
    "provider",
    "provider_name",
    "model",
    "model_id",
    "model_name",
    "model_ref",
    "models",
    "base_url",
    "api_key",
    "generated_at",
    "created_at",
    "published_at",
    "timestamp",
    "method_index",
    "original_index",
    "index",
    "recommended_index",
    "recommended",
    "is_recommended",
    "recommendation",
    "ranking",
    "score",
    "scores",
    "model_config_id",
    "llm_prompt_hash",
    "is_replay",
)

#: 需要打码而非删除的 params 键（T3_model_compare 的对照模型属于实验对象，
#: 语义要保留、身份要去掉 → 替换为 ``<MODEL_1>`` 这类占位符，映射留服务端）
MASKED_MODEL_PARAM_KEYS: tuple[str, ...] = ("model", "model_ref", "model_id", "model_name", "models")

#: 打码后 params 使用的**中性键名**（原键名 ``models`` / ``model`` 本身即命中禁用字段名，
#: 因此必须连键名一起换掉，否则「字段名检索」仍会命中）
MASKED_PARAM_KEY_ALIAS = "comparison_targets"

#: 自由文本中模型标识被替换为的占位符
REDACTED_MODEL = "<redacted-model>"

#: 时间戳正则（ISO8601 片段），用于在自由文本里擦除生成时间
_TIMESTAMP_RE = re.compile(
    r"\d{4}-\d{2}-\d{2}(?:[T ]\d{2}:\d{2}(?::\d{2}(?:\.\d+)?)?(?:Z|[+-]\d{2}:?\d{2})?)?"
)

#: 别名占位符（映射留在服务端 mapping.masked_tokens）
_MASK_TEMPLATE = "<MODEL_{n}>"

#: 自由文本里出现这些词即视为把「谁生成的」写进了正文（口语化兜底）
_RECOMMENDATION_WORDS: tuple[str, ...] = (
    "recommended",
    "推荐方案",
    "推荐项",
    "首选方案",
    "原始下标",
)


class AnonymizationLeakError(StageError):
    """匿名化后仍能检索到可识别信息（**宁可失败也不放行**）。"""

    code = "anonymization_leak"
    default_level = "L3"

    def __init__(
        self,
        message: str,
        *,
        hits: Sequence[str] | None = None,
        stage: str = REVIEWER_STAGE,
        detail: Any = None,
    ) -> None:
        self.hits = list(hits or [])
        super().__init__(
            message,
            level="L3",
            stage=stage,
            detail=detail or {"hits": self.hits},
            retryable=False,
        )

    def to_dict(self) -> dict[str, Any]:
        payload = super().to_dict()
        payload["detail"] = {**(payload.get("detail") or {}), "hits": self.hits}
        return payload


class IsolationStageError(StageError, IsolationViolation):
    """盲评隔离失败（**同时**是 ``StageError`` 与 ``IsolationViolation``）。

    这样做的原因有两个，缺一不可：

    - 引擎（WP09）按 :class:`StageError` 的 ``level`` 分级处置，必须是 StageError 才能
      被正确判为环节失败（L3，不重试）；
    - 契约与验收要求「报 ``IsolationViolation``」，因此保留
      :class:`~app.llm.errors.IsolationViolation` 的 ``isinstance`` 语义与
      ``generator_ref`` / ``reviewer_ref`` 字段，便于接口与前端直接识别。
    """

    code = "isolation_violation"
    default_level = "L3"

    def __init__(
        self,
        message: str,
        *,
        stage: str = REVIEWER_STAGE,
        generator_stage: str | None = GENERATOR_STAGE,
        reviewer_stage: str | None = REVIEWER_STAGE,
        generator_ref: str | None = None,
        reviewer_ref: str | None = None,
        detail: Any = None,
        retryable: bool = False,
    ) -> None:
        self.generator_stage = generator_stage
        self.reviewer_stage = reviewer_stage
        self.generator_ref = generator_ref
        self.reviewer_ref = reviewer_ref
        StageError.__init__(
            self,
            message,
            level="L3",
            stage=stage,
            detail=detail,
            retryable=retryable,
        )

    def to_dict(self) -> dict[str, Any]:
        payload = super().to_dict()
        detail = dict(payload.get("detail") or {}) if isinstance(payload.get("detail"), dict) else {}
        detail.update(
            {
                "generator_stage": self.generator_stage,
                "reviewer_stage": self.reviewer_stage,
                "generator_ref": self.generator_ref,
                "reviewer_ref": self.reviewer_ref,
                "isolation": "generator_model_ref != reviewer_model_ref",
            }
        )
        payload["detail"] = detail
        return payload


# --------------------------------------------------------------------------- #
# 1) 隔离校验（复用 WP02，不重写）
# --------------------------------------------------------------------------- #
async def resolve_isolated_pair(project_id: int | None = None) -> tuple[Any, Any]:
    """解析「生成 / 评审」路由并强制隔离；失败抛 :class:`IsolationStageError`。

    直接复用 WP02 的 ``app.llm.router.resolve_pair('plan', 'plan_review', project_id)``：
    它已实现「主路由相同 → 抛 IsolationViolation，且不会自动拿备用模型顶替」。
    本函数只做两件事：把异常转成环节可处置的 :class:`IsolationStageError`，
    以及把配置指引写清楚（附录 D.2 要求显式提示需要两个不同模型）。
    """
    from app.llm.errors import ModelRoutingError
    from app.llm.router import resolve_pair

    try:
        generator, reviewer = await resolve_pair(GENERATOR_STAGE, REVIEWER_STAGE, project_id)
    except IsolationViolation as exc:
        raise IsolationStageError(
            f"IsolationViolation：{exc}\n"
            "盲评隔离是硬约束：请在「设置 → 模型路由」为 plan 与 plan_review "
            "配置两个不同的 model_ref（可以是不同供应商，或同一供应商的不同模型）。"
            "系统不会自动回退到同一模型来「凑」出隔离。",
            generator_ref=getattr(exc, "generator_ref", None) or None,
            reviewer_ref=getattr(exc, "reviewer_ref", None) or None,
            detail={"project_id": project_id, "cause": type(exc).__name__},
        ) from exc
    except ModelRoutingError as exc:
        raise IsolationStageError(
            f"盲评隔离无法校验：路由解析失败（{exc}）。plan_review 直接失败，"
            "请先在「设置 → 模型路由」配置 plan 与 plan_review 两条路由。",
            detail={"project_id": project_id, "cause": type(exc).__name__},
        ) from exc

    gen_ref = str(getattr(generator, "model_ref", "") or "")
    rev_ref = str(getattr(reviewer, "model_ref", "") or "")
    if not gen_ref or not rev_ref or _norm_ref(gen_ref) == _norm_ref(rev_ref):
        raise IsolationStageError(
            f"IsolationViolation：隔离校验后两侧 model_ref 仍相同或为空"
            f"（plan={gen_ref or '空'} / plan_review={rev_ref or '空'}）",
            generator_ref=gen_ref or None,
            reviewer_ref=rev_ref or None,
            detail={"project_id": project_id, "cause": "post_check"},
        )
    return generator, reviewer


def norm_ref(value: str) -> str:
    """``model_ref`` 归一化（比较隔离时的唯一口径；大小写与首尾空格不敏感）。"""
    return str(value or "").strip().lower()


#: 兼容内部旧名（模块内使用）
_norm_ref = norm_ref


def deterministic_seed(pipeline_run_id: int | None, attempt: int | None = None, iteration: int = 1) -> int:
    """服务端生成的可复现 ``shuffle_seed``（由运行标识派生，与模型无关）。

    ``review_calibrations.shuffle_seed`` 落库后，任何人可用同一 seed 复原候选顺序。
    这里用 SHA-256 派生而非 ``random``：同一 (run, attempt, iteration) 永久得到同一
    顺序（重启、换进程都不变），同时与「模型输出」完全无关——模型无法影响乱序。
    """
    material = f"sciloop.blind.v1|run={pipeline_run_id}|attempt={attempt}|iter={iteration}"
    digest = hashlib.sha256(material.encode("utf-8")).hexdigest()
    return int(digest[:12], 16)  # 48bit，落库为 BIGINT


# --------------------------------------------------------------------------- #
# 2) 匿名化 + 固定种子乱序
# --------------------------------------------------------------------------- #
def anonymize_and_shuffle(
    methods: Sequence[Mapping[str, Any]],
    *,
    seed: int | None = None,
    forbidden_tokens: Sequence[str] = (),
    strict: bool = True,
) -> dict[str, Any]:
    """把 ``plan`` 的候选方案匿名化并按固定种子乱序。

    参数
    ----
    methods
        ``plan`` 环节产出中的 ``methods``（原顺序即「原始下标」0..n-1）。
    seed
        ``shuffle_seed``。``None`` 时服务端随机生成（真随机，模型无法预测）。
    forbidden_tokens
        额外禁词（通常是生成模型 / 评审模型的 ``model_ref``、``model_id``、
        供应商名）。命中即视为泄漏。
    strict
        ``True``（默认）时，构造完成后立即做一次字符串检索，发现泄漏就抛
        :class:`AnonymizationLeakError`——**宁可直接失败，也不把可识别信息发出去**。

    返回（对应 WP12 ``interfaces.provides`` 的 ``{aliased, mapping, seed}``）
    -------
    ``{"aliased": [...], "mapping": {...}, "seed": int, "candidate_order": [...],
       "anonymization_version": str, "masked_tokens": {...}, "leak_check": {...}}``

    - ``aliased``：评审模型实际看到的内容（只有别名 + 白名单字段）
    - ``mapping``：``{alias: 原始下标}``（**只在服务端保存**）
    - ``candidate_order``：按展示顺序的 ``[{alias, method_index}]``（落库用）
    """
    ordered_indexes = list(range(len(methods)))
    actual_seed = int(seed) if seed is not None else random.SystemRandom().randrange(1, 2**48)
    rng = random.Random(actual_seed)
    rng.shuffle(ordered_indexes)

    masked_tokens: dict[str, str] = {}
    alias_to_index: dict[str, int] = {}
    aliased: list[dict[str, Any]] = []
    candidate_order: list[dict[str, Any]] = []

    for position, original_index in enumerate(ordered_indexes):
        alias = _alias_for(position)
        alias_to_index[alias] = original_index
        candidate_order.append({"alias": alias, "method_index": original_index, "position": position})
        aliased.append(
            _sanitize_method(methods[original_index], alias, masked_tokens, forbidden_tokens)
        )

    pack: dict[str, Any] = {
        "aliased": aliased,
        "mapping": alias_to_index,
        "seed": actual_seed,
        "candidate_order": candidate_order,
        "anonymization_version": ANONYMIZATION_VERSION,
        "masked_tokens": masked_tokens,
        "candidate_count": len(aliased),
    }
    pack["leak_check"] = assert_anonymized(pack["aliased"], forbidden_tokens=forbidden_tokens)
    if strict and not pack["leak_check"]["clean"]:
        raise AnonymizationLeakError(
            "匿名化校验未通过：评审载荷仍可检索到可识别信息 "
            f"{pack['leak_check']['hits']}（禁止把可识别信息送给评审模型）",
            hits=pack["leak_check"]["hits"],
            detail={"anonymization_version": ANONYMIZATION_VERSION, "seed": actual_seed},
        )
    return pack


def build_review_payload(
    pack: Mapping[str, Any],
    *,
    taskbook_constraints: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """构造送给评审模型的匿名载荷（附录 D.3 的「输入」）。

    只包含：别名候选列表 + 任务书约束 + 打分口径。**不含**任务书 id、项目 id、
    生成时间、模型信息与任何原始下标。
    """
    payload: dict[str, Any] = {
        "review_scope": "隔离盲评：候选用别名指代，评委不知道任何候选的生成来源",
        "candidates": list(pack.get("aliased") or []),
        "scoring": {
            "dimensions": ["novelty", "feasibility", "rigor", "cost_reasonableness", "risk_control"],
            "scale": "每维 0-20（整数）",
            "total": "total = 五维之和（0-100），由服务端复核重算",
        },
        "verdict_rule": (
            "approve：全部候选可用且必须给出 selected_alias；"
            "revise：需给出至少 1 条 improvement_suggestions；"
            "reject：全部候选不可用（触发 D4 失败处置）"
        ),
        "alias_policy": (
            "只能引用别名（A/B/C…）；不得推断候选的来源、生成前后次序，"
            "也不得假设服务端或生成方已指定某个候选项"
        ),
    }
    if taskbook_constraints:
        payload["taskbook_constraints"] = dict(taskbook_constraints)
    return payload


def assert_anonymized(
    payload: Any,
    *,
    forbidden_tokens: Sequence[str] = (),
    check_phrases: bool = True,
) -> dict[str, Any]:
    """对（即将送出的）评审载荷做可识别信息检索。

    返回 ``{"clean": bool, "hits": [...], "checked_tokens": [...], "chars": int}``。
    检查三类命中：

    1. 禁用**字段名**（``model_ref`` / ``provider`` / ``generated_at`` /
       ``recommended_index`` …）出现在任意键上；
    2. 传入的 ``forbidden_tokens``（真实模型名 / 供应商名，大小写不敏感）；
    3. 自由文本里的 ISO 时间戳与「推荐项」措辞（``check_phrases=False`` 可关掉，
       仅用于「只关心模型标识」的补充审计）。
    """
    text = json.dumps(payload, ensure_ascii=False, default=str)
    lowered = text.lower()
    hits: set[str] = set()

    for key in iter_keys(payload):
        if key.lower() in {name.lower() for name in FORBIDDEN_FIELD_NAMES}:
            hits.add(f"field:{key}")

    for token in forbidden_tokens:
        needle = str(token or "").strip()
        if len(needle) < 2:
            continue
        if needle.lower() in lowered:
            hits.add(f"token:{needle}")

    for match in _TIMESTAMP_RE.findall(text):
        hits.add(f"timestamp:{match}")

    if check_phrases:
        for word in _RECOMMENDATION_WORDS:
            if word.lower() in lowered:
                hits.add(f"phrase:{word}")

    return {
        "clean": not hits,
        "hits": sorted(hits),
        "checked_tokens": [str(item) for item in forbidden_tokens],
        "chars": len(text),
    }


def iter_keys(payload: Any) -> list[str]:
    """递归收集 JSON 结构里出现的所有键名。"""
    found: list[str] = []

    def walk(node: Any) -> None:
        if isinstance(node, Mapping):
            for key, value in node.items():
                found.append(str(key))
                walk(value)
        elif isinstance(node, (list, tuple)):
            for item in node:
                walk(item)

    walk(payload)
    return found


def verify_shuffle_reproducible(
    methods: Sequence[Mapping[str, Any]] | None = None,
    seed: int | None = None,
    *,
    count: int = 4,
) -> dict[str, Any]:
    """固定 ``seed`` 的乱序可复现性自检（验收 A3 的可执行证据）。

    同一 seed 生成两次，比较 ``candidate_order`` 与 ``aliased`` 是否逐字节一致。
    """
    source: Sequence[Mapping[str, Any]]
    if methods is None:
        source = [{"name": f"candidate_{i}", "description": f"desc_{i}"} for i in range(count)]
    else:
        source = methods
    actual_seed = int(seed) if seed is not None else 20260917
    first = anonymize_and_shuffle(source, seed=actual_seed)
    second = anonymize_and_shuffle(source, seed=actual_seed)
    other = anonymize_and_shuffle(source, seed=actual_seed + 1)
    return {
        "seed": actual_seed,
        "first_order": first["candidate_order"],
        "second_order": second["candidate_order"],
        "different_seed_order": other["candidate_order"],
        "reproducible": first["candidate_order"] == second["candidate_order"]
        and first["aliased"] == second["aliased"],
        "seed_matters": first["candidate_order"] != other["candidate_order"],
    }


# --------------------------------------------------------------------------- #
# 3) 别名 → 原始下标还原（服务端侧，评审后使用）
# --------------------------------------------------------------------------- #
def resolve_alias(mapping: Mapping[str, Any], alias: Any) -> int | None:
    """把评审模型给出的别名还原为原始 ``method_index``（查不到返回 ``None``）。"""
    if alias is None:
        return None
    key = str(alias).strip().upper()
    for candidate_key, value in mapping.items():
        if str(candidate_key).strip().upper() == key:
            try:
                return int(value)
            except (TypeError, ValueError):
                return None
    return None


def forbidden_tokens_for(*models: Any) -> list[str]:
    """从 ``ResolvedModel`` 提取禁词（``model_ref`` / ``model_id`` / 供应商名）。

    评审载荷里出现其中任何一个都算泄漏——包括「同一供应商的不同模型」。
    """
    tokens: list[str] = []
    for model in models:
        if model is None:
            continue
        for attr in ("model_ref", "model_id", "provider_name", "provider"):
            value = getattr(model, attr, None)
            if isinstance(value, str) and len(value.strip()) >= 2:
                tokens.append(value.strip())
        ref = str(getattr(model, "model_ref", "") or "")
        if ":" in ref:
            provider, _, model_id = ref.partition(":")
            if len(provider.strip()) >= 2:
                tokens.append(provider.strip())
            if len(model_id.strip()) >= 2:
                tokens.append(model_id.strip())
    seen: set[str] = set()
    unique: list[str] = []
    for token in tokens:
        key = token.lower()
        if key in seen:
            continue
        seen.add(key)
        unique.append(token)
    return unique


# --------------------------------------------------------------------------- #
# 内部
# --------------------------------------------------------------------------- #
def _alias_for(position: int) -> str:
    if position < len(ALIAS_LETTERS):
        return ALIAS_LETTERS[position]
    return f"C{position + 1}"


def _sanitize_method(
    method: Mapping[str, Any],
    alias: str,
    masked_tokens: dict[str, str],
    forbidden_tokens: Sequence[str] = (),
) -> dict[str, Any]:
    """白名单裁剪 + 身份打码，产出评审模型看到的候选对象。"""
    if not isinstance(method, Mapping):
        method = {}
    title = _scrub_text(method.get("name") or method.get("title") or "untitled", forbidden_tokens)
    summary = _scrub_text(
        method.get("description") or method.get("summary") or "", forbidden_tokens
    )
    params = _sanitize_params(method.get("params"), masked_tokens, forbidden_tokens)
    sanitized: dict[str, Any] = {
        "candidate_alias": alias,
        "title": title,
        "summary": summary,
        "template_id": _scrub_text(method.get("template_id") or "unknown", forbidden_tokens),
        "params": params,
        "expected_metrics": [
            _scrub_text(item, forbidden_tokens)
            for item in (method.get("expected_metrics") or [])
            if str(item).strip()
        ],
        "rationale": _scrub_text(method.get("rationale") or "", forbidden_tokens),
    }
    return {key: sanitized[key] for key in ("candidate_alias", *ALLOWED_METHOD_FIELDS)}


def _sanitize_params(
    value: Any,
    masked_tokens: dict[str, str],
    forbidden_tokens: Sequence[str] = (),
) -> dict[str, Any]:
    """``params`` 按 key 递归清洗：对照模型名打码 + 键名中性化 + 非法键丢弃。"""
    if not isinstance(value, Mapping):
        return {}
    result: dict[str, Any] = {}
    for key, raw in value.items():
        name = str(key)
        lowered = name.lower()
        if lowered in MASKED_MODEL_PARAM_KEYS:
            # 键名一并换成中性名（``models`` / ``model`` 本身就是禁用字段名）
            target_key = MASKED_PARAM_KEY_ALIAS
            counter = 1
            while target_key in result:
                counter += 1
                target_key = f"{MASKED_PARAM_KEY_ALIAS}_{counter}"
            result[target_key] = _mask_model_value(raw, masked_tokens)
            continue
        if lowered in {item.lower() for item in FORBIDDEN_FIELD_NAMES}:
            # 直接丢弃：不允许把可识别字段透传（宁缺毋滥）
            continue
        if isinstance(raw, Mapping):
            result[name] = _sanitize_params(raw, masked_tokens, forbidden_tokens)
        elif isinstance(raw, (list, tuple)):
            result[name] = [
                (
                    _sanitize_params(item, masked_tokens, forbidden_tokens)
                    if isinstance(item, Mapping)
                    else _scrub_value(item, forbidden_tokens)
                )
                for item in raw
            ]
        else:
            result[name] = _scrub_value(raw, forbidden_tokens)
    return result


def _mask_model_value(value: Any, masked_tokens: dict[str, str]) -> Any:
    """把「对照模型名」替换为服务端占位符，语义保留、身份去掉。"""
    if isinstance(value, (list, tuple)):
        return [_mask_model_value(item, masked_tokens) for item in value]
    if isinstance(value, Mapping):
        return {str(k): _mask_model_value(v, masked_tokens) for k, v in value.items()}
    text = str(value or "").strip()
    if not text:
        return text
    token = masked_tokens.get(text)
    if token is None:
        token = _MASK_TEMPLATE.format(n=len(masked_tokens) + 1)
        masked_tokens[text] = token
    return token


def _scrub_value(value: Any, forbidden_tokens: Sequence[str] = ()) -> Any:
    if isinstance(value, str):
        return _scrub_text(value, forbidden_tokens)
    if isinstance(value, (int, float, bool)) or value is None:
        return value
    return _scrub_text(str(value), forbidden_tokens)


def _scrub_text(value: Any, forbidden_tokens: Sequence[str] = ()) -> str:
    """擦除自由文本里的模型标识、时间戳与「推荐项」措辞。

    三件事都必须做，否则会从「正文」而不是「字段」泄漏：

    1. 生成模型 / 供应商名（``gpt-4o``、``openai``…）——按**长串优先**替换，
       避免 ``openai:gpt-4o`` 只被替换掉一半；
    2. ISO 时间戳（生成时间属于可识别信息）；
    3. 「推荐 X」措辞——一旦出现就等于把生成方的推荐项泄漏给了评委
       （附录 D.3 明令不得包含推荐项）。
    """
    text = str(value or "")
    if not text:
        return ""
    needles = sorted(
        {str(token).strip() for token in forbidden_tokens if len(str(token).strip()) >= 2},
        key=len,
        reverse=True,
    )
    for needle in needles:
        text = re.sub(re.escape(needle), REDACTED_MODEL, text, flags=re.IGNORECASE)
    text = _TIMESTAMP_RE.sub("<redacted-time>", text)
    for word in _RECOMMENDATION_WORDS:
        text = re.sub(re.escape(word), "<redacted>", text, flags=re.IGNORECASE)
    return text.strip()


def utc_now_iso() -> str:
    return datetime.now(UTC).isoformat()


__all__ = [
    "ALIAS_LETTERS",
    "ALLOWED_METHOD_FIELDS",
    "ANONYMIZATION_VERSION",
    "AnonymizationLeakError",
    "GENERATOR_STAGE",
    "IsolationStageError",
    "REVIEWER_STAGE",
    "anonymize_and_shuffle",
    "assert_anonymized",
    "build_review_payload",
    "deterministic_seed",
    "forbidden_tokens_for",
    "iter_keys",
    "norm_ref",
    "resolve_alias",
    "resolve_isolated_pair",
    "utc_now_iso",
    "verify_shuffle_reproducible",
]
