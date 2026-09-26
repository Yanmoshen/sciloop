# Copyright 2026 SciLoop contributors
# Licensed under the Apache License, Version 2.0 (http://www.apache.org/licenses/LICENSE-2.0)
"""七个维度的可行性评审：**分数 + 约 50 字分析**，由模型一次生成。

口径（2026-09-26 研究者定稿，技能「论文创新Idea生成与专业可行性分析」）
------------------------------------------------------------------------
- 前四维（技术成熟度 / 数据可行性 / 算力与工程成本 / 创新增量）规则层已经能算分
  → 把**规则分与依据**一并交给模型，让它在同一尺度上复核；
- 后三维（落地风险 / 应用价值 / 伦理与合规）**规则层没有信号** → 完全由模型给分；
- 每维**约 50 字分析**：界面直接展示且**不允许研究者修改**，所以必须说清判断依据，
  不许写"较好/一般"这种没有信息量的套话；
- **分数含义统一为「越高越好」** —— ``landing_risk`` 的高分表示风险可控。

失败时**不编造**：返回 ``None``，由调用方如实降级（用规则分 + 规则依据充当分析，
并在返回体里标注未经模型复核）。这与项目「禁止把不确定说成确定」的口径一致。
"""

from __future__ import annotations

import logging
from collections.abc import Mapping, Sequence
from typing import Any

from services.feasibility.scorer import REVIEW_DIMENSIONS

logger = logging.getLogger("sciloop.feasibility.dimension_review")

#: 写入 ``llm_call_logs.stage`` / ``purpose``
LLM_STAGE = "feasibility"
LLM_PURPOSE = "dimension_review"

#: 分析文字长度口径（约 50 字；超长截断到 120，避免界面被挤爆）
ANALYSIS_TARGET_CHARS = 50
ANALYSIS_MAX_CHARS = 120

_DIM_KEYS: tuple[str, ...] = tuple(key for key, _ in REVIEW_DIMENSIONS)
_DIM_LABELS: dict[str, str] = dict(REVIEW_DIMENSIONS)

_REVIEW_SCHEMA: dict[str, Any] = {
    "type": "object",
    "additionalProperties": False,
    "required": ["dimensions"],
    "properties": {
        "dimensions": {
            "type": "array",
            "minItems": len(_DIM_KEYS),
            "maxItems": len(_DIM_KEYS),
            "items": {
                "type": "object",
                "additionalProperties": False,
                "required": ["key", "score", "analysis"],
                "properties": {
                    "key": {"type": "string", "enum": list(_DIM_KEYS)},
                    "score": {
                        "type": "integer",
                        "minimum": 0,
                        "maximum": 100,
                        "description": "该维度得分，0–100，越高越好",
                    },
                    "analysis": {
                        "type": "string",
                        "description": f"约 {ANALYSIS_TARGET_CHARS} 字，说清这个分是怎么来的",
                    },
                },
            },
        }
    },
}

SYSTEM_PROMPT = (
    "你是资深科研项目评审人，负责给研究 idea 做**可行性评估**。硬性要求：\n"
    f"1) 必须对下列 {len(_DIM_KEYS)} 个维度**每个都给一个 0–100 的整数分**，一个都不能漏：\n"
    + "".join(f"   {key}＝{label}；\n" for key, label in REVIEW_DIMENSIONS)
    + "2) **所有维度的分数含义统一为「越高越好」** —— 注意 landing_risk（落地风险）"
    "的高分表示**风险可控**，不是风险大；\n"
    f"3) 每个维度配一段**约 {ANALYSIS_TARGET_CHARS} 字的分析**，必须说清判断依据"
    "（引用材料里的具体事实：数据集、基线、算力量级、合规约束等），"
    "禁止写「较高/一般/待评估」这类没有信息量的套话；\n"
    "4) 只能依据给定材料判断；材料没有的信息**不要编造**，写「材料未提供」"
    "并据此给保守分；\n"
    "5) 只输出 JSON，符合给定 schema。"
)


def build_prompt(
    *,
    idea: Mapping[str, Any],
    rule_dimensions: Sequence[Mapping[str, Any]],
    evidence_digest: str,
) -> str:
    """构造评审 prompt（纯函数，便于离线核对）。"""
    lines: list[str] = [
        "## 待评审的研究 idea",
        f"标题：{idea.get('title') or '（未命名）'}",
        "内容：",
        str(idea.get("content") or "（无）")[:3000],
        "",
        "## 材料证据摘要",
        evidence_digest or "（本聚合没有可用的论文卡片信号，请对无依据的维度给保守分）",
        "",
        "## 需要你给分的七个维度（每个都要给，0–100 分，越高越好）",
    ]
    rule_by_key = {str(dim.get("key")): dim for dim in rule_dimensions}
    for key, label in REVIEW_DIMENSIONS:
        ref = rule_by_key.get(key)
        if ref:
            lines.append(
                f"- {key}＝{label}：规则分参考 {ref.get('score')}，"
                f"依据：{str(ref.get('rationale') or '未提供')[:200]}"
            )
        else:
            lines.append(f"- {key}＝{label}：规则层算不出这一维，请你自己判断")
    lines += [
        "",
        "## 输出要求",
        "JSON：{dimensions:[{key, score, analysis}]}，"
        f"共 {len(_DIM_KEYS)} 项、每项 analysis 约 {ANALYSIS_TARGET_CHARS} 字。",
    ]
    return "\n".join(lines)


def _clip(text: Any) -> str:
    value = " ".join(str(text or "").split())
    return value[:ANALYSIS_MAX_CHARS]


async def review_dimensions(
    *,
    idea: Mapping[str, Any],
    rule_dimensions: Sequence[Mapping[str, Any]],
    evidence_digest: str = "",
    model_ref: str | None,
    project_id: int | None,
    temperature: float = 0.3,
    max_tokens: int | None = None,
) -> dict[str, Any] | None:
    """一次模型调用产出七维评审；**失败返回 None**（不编造、由调用方降级）。"""
    from llm.adapter import chat  # 局部导入：无 LLM 场景下不引入导入期依赖

    prompt = build_prompt(
        idea=idea, rule_dimensions=rule_dimensions, evidence_digest=evidence_digest
    )
    try:
        result = await chat(
            [
                {"role": "system", "content": SYSTEM_PROMPT},
                {"role": "user", "content": prompt},
            ],
            model_ref,
            temperature,
            max_tokens,
            _REVIEW_SCHEMA,
            project_id=project_id,
            stage=LLM_STAGE,
            purpose=LLM_PURPOSE,
        )
    except Exception as exc:  # noqa: BLE001 - 评审失败必须降级而不是让整个可行性失败
        logger.warning("七维评审调用失败，将由调用方如实降级：%s", exc)
        return None

    parsed = result.parsed if isinstance(result.parsed, Mapping) else None
    raw_items = parsed.get("dimensions") if isinstance(parsed, Mapping) else None
    if not isinstance(raw_items, list):
        logger.warning("七维评审返回不符合 schema，按失败处理")
        return None

    by_key: dict[str, dict[str, Any]] = {}
    for item in raw_items:
        if not isinstance(item, Mapping):
            continue
        key = str(item.get("key") or "").strip()
        if key not in _DIM_KEYS:
            continue
        try:
            score = int(item.get("score"))
        except (TypeError, ValueError):
            continue
        by_key[key] = {
            "key": key,
            "label": _DIM_LABELS[key],
            "score": max(0, min(100, score)),
            "analysis": _clip(item.get("analysis")),
        }
    if len(by_key) != len(_DIM_KEYS):
        logger.warning(
            "七维评审缺少维度（得到 %d / %d），按失败处理：%s",
            len(by_key),
            len(_DIM_KEYS),
            sorted(by_key),
        )
        return None

    return {
        "dimensions": [by_key[key] for key in _DIM_KEYS],
        "model_ref": result.model_ref,
        "provider": result.provider,
        "model_id": result.model_id,
        "cost_usd": result.cost_usd,
        "is_replay": result.is_replay,
        "duration_ms": result.duration_ms,
        "prompt_hash": result.prompt_hash,
    }


__all__ = [
    "ANALYSIS_MAX_CHARS",
    "ANALYSIS_TARGET_CHARS",
    "LLM_PURPOSE",
    "LLM_STAGE",
    "SYSTEM_PROMPT",
    "build_prompt",
    "review_dimensions",
]
