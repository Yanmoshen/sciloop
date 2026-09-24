# Copyright 2026 SciLoop contributors
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
"""实验模板注册表（WP11-T2）。

红线（``contracts.guardrails.safety`` / 计划书 §4.7）：

1. **模板 ID 白名单**（``contracts.enums.template_id``），未注册的 ID 一律拒绝
2. **参数 schema 校验**：未知键、类型不符、越界值一律拒绝；不做隐式纠正
3. **``sample_size <= 50``**，且不得小于模板下限；拒绝而非截断
4. 占位模板（T2/T4/T5，P1 范围）**必须显式抛错**，禁止静默返回空结果

注册表同时是执行入口的唯一分发点：``execute(template_id, ctx)``。
执行器（:mod:`executor.runner`）只会通过这里调用模板。
"""

from __future__ import annotations

import json
import logging
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from executor.errors import (
    SampleSizeExceededError,
    TemplateNotImplementedError,
    TemplateRejectedError,
)

logger = logging.getLogger("sciloop.experiment.registry")

#: 契约白名单（与 ``contracts.enums.template_id`` 逐字一致）
TEMPLATE_IDS: tuple[str, ...] = (
    "T1_prompt_variant",
    "T2_fewshot_ablation",
    "T3_model_compare",
    "T4_llm_as_judge",
    "T5_rag_ablation",
)

#: P0 实装模板（计划书 §4.7：P0 跑通 T1 与 T3）
P0_TEMPLATES: tuple[str, ...] = ("T1_prompt_variant", "T3_model_compare")

#: 样本量上限（契约硬约束；与执行器 limits.SAMPLE_SIZE_MAX 同源）
SAMPLE_SIZE_MAX = 50
SAMPLE_SIZE_MIN = 1

DATA_DIR = Path(__file__).resolve().parent / "templates" / "data"
DEFAULT_DATASET = "eval_set_v1.json"


# --------------------------------------------------------------------------- #
# 规格
# --------------------------------------------------------------------------- #
@dataclass(frozen=True)
class TemplateSpec:
    """模板元数据（对外 ``GET /experiments/templates`` 如实暴露实现状态）。"""

    template_id: str
    title: str
    paradigm: str
    metrics: tuple[str, ...]
    description: str
    version: str
    prompt_version: str | None
    status: str  # implemented | placeholder
    sample_size_min: int
    sample_size_max: int
    sample_size_default: int
    params_schema: Mapping[str, Mapping[str, Any]] = field(default_factory=dict)
    dataset: str | None = None
    module: str | None = None
    placeholder_reason: str | None = None

    @property
    def implemented(self) -> bool:
        return self.status == "implemented"

    def to_dict(self) -> dict[str, Any]:
        return {
            "template_id": self.template_id,
            "title": self.title,
            "paradigm": self.paradigm,
            "metrics": list(self.metrics),
            "description": self.description,
            "version": self.version,
            "prompt_version": self.prompt_version,
            "status": self.status,
            "implemented": self.implemented,
            "sample_size": {
                "min": self.sample_size_min,
                "max": self.sample_size_max,
                "default": self.sample_size_default,
                "max_hard_limit": SAMPLE_SIZE_MAX,
            },
            "params_schema": {key: dict(value) for key, value in self.params_schema.items()},
            "dataset": self.dataset,
            "p0": self.template_id in P0_TEMPLATES,
            "placeholder_reason": self.placeholder_reason,
        }


#: 参数 schema 的公共片段：温度 / max_tokens / 显式模型
COMMON_PARAMS: dict[str, dict[str, Any]] = {
    "temperature": {
        "type": "number",
        "min": 0,
        "max": 2,
        "default": 0.0,
        "description": "采样温度（默认 0，保证可比性）",
    },
    "max_tokens": {
        "type": "integer",
        "min": 1,
        # 默认 None = **不设上限**（2026-09-24 用户口径：不设限制）；
        # 这里**保留可显式指定**，因为实验平台要靠"固定它"来保证变体之间的可比性
        # （T1/T3 的设计就是"除自变量外全部固定"）—— 那是实验控制，不是渠道差异。
        "default": None,
        "description": "单次回答的 token 上限；留空 = 不设上限",
    },
    "sample_offset": {
        "type": "integer",
        "min": 0,
        "max": 10000,
        "default": 0,
        "description": "从数据集（按 id 升序）取样本的起始偏移，便于换一批样本复跑",
    },
}

#: 模板注册表（唯一事实来源）
TEMPLATES: dict[str, TemplateSpec] = {
    "T1_prompt_variant": TemplateSpec(
        template_id="T1_prompt_variant",
        title="提示词变体对比",
        paradigm="同一任务下多套 prompt 变体对比",
        metrics=("accuracy", "answer_consistency", "latency_ms", "cost_usd"),
        description=(
            "对同一批样本执行 3 套提示词变体（零样本直答 / 先推理后作答 / 强格式约束），"
            "逐变体计算 accuracy，并计算变体两两一致率作为一致性指标。"
        ),
        version="1.0.0",
        prompt_version="T1-prompts-v1",
        status="implemented",
        sample_size_min=20,
        sample_size_max=SAMPLE_SIZE_MAX,
        sample_size_default=24,
        dataset=DEFAULT_DATASET,
        module="services.experiment.templates.T1_prompt_variant",
        params_schema={
            **COMMON_PARAMS,
            "model_ref": {
                "type": "string",
                "default": None,
                "description": "显式模型 provider:model_id；留空按 stage='experiment' 路由解析",
            },
        },
    ),
    "T3_model_compare": TemplateSpec(
        template_id="T3_model_compare",
        title="多模型横向对照",
        paradigm="同一任务与同一 Prompt 下两个模型对比",
        metrics=("accuracy", "cost_usd", "latency_ms"),
        description=(
            "以完全相同的 Prompt 与样本，对两个模型横向对比 accuracy / 成本（由 llm_call_logs 汇总）"
            "/ 延迟，产出可作盲评校准对照的表格。"
        ),
        version="1.0.0",
        prompt_version="T3-prompt-v1",
        status="implemented",
        sample_size_min=20,
        sample_size_max=SAMPLE_SIZE_MAX,
        sample_size_default=20,
        dataset=DEFAULT_DATASET,
        module="services.experiment.templates.T3_model_compare",
        params_schema={
            **COMMON_PARAMS,
            "model_refs": {
                "type": "list[string]",
                "min_items": 2,
                "max_items": 2,
                "default": None,
                "description": "待对比的两个模型 provider:model_id；留空取 stage='experiment' 路由链前两个",
            },
        },
    ),
    "T2_fewshot_ablation": TemplateSpec(
        template_id="T2_fewshot_ablation",
        title="few-shot 数量消融",
        paradigm="few-shot 示例数量消融（0/1/3/5）",
        metrics=("accuracy",),
        description="P1 范围：仅在注册表保留占位条目，执行时显式抛未实现。",
        version="0.0.0",
        prompt_version=None,
        status="placeholder",
        sample_size_min=1,
        sample_size_max=SAMPLE_SIZE_MAX,
        sample_size_default=20,
        placeholder_reason="P1 路线图；WP11 本轮只实装 P0 的 T1/T3（计划书 §4.7）",
    ),
    "T4_llm_as_judge": TemplateSpec(
        template_id="T4_llm_as_judge",
        title="LLM 作为裁判的一致性评测",
        paradigm="LLM 裁判与人工标注的一致率",
        metrics=("agreement_rate",),
        description="P1 范围：仅在注册表保留占位条目，执行时显式抛未实现。",
        version="0.0.0",
        prompt_version=None,
        status="placeholder",
        sample_size_min=1,
        sample_size_max=SAMPLE_SIZE_MAX,
        sample_size_default=20,
        placeholder_reason="P1 路线图；与 WP12 的盲评校准口径需要另行统一后再实装",
    ),
    "T5_rag_ablation": TemplateSpec(
        template_id="T5_rag_ablation",
        title="检索增强消融",
        paradigm="是否引入检索增强的对比",
        metrics=("accuracy", "citation_accuracy"),
        description="P1 范围：仅在注册表保留占位条目，执行时显式抛未实现。",
        version="0.0.0",
        prompt_version=None,
        status="placeholder",
        sample_size_min=1,
        sample_size_max=SAMPLE_SIZE_MAX,
        sample_size_default=20,
        placeholder_reason="P1 路线图；依赖全文检索与引用校验链路稳定后再实装",
    ),
}


# --------------------------------------------------------------------------- #
# 数据集
# --------------------------------------------------------------------------- #
_DATASET_CACHE: dict[str, dict[str, Any]] = {}


def load_dataset(name: str = DEFAULT_DATASET) -> dict[str, Any]:
    """载入评测数据集（只读 JSON 资产；文件缺失即如实失败）。"""
    safe = str(name or "").strip()
    if not safe.endswith(".json") or "/" in safe or "\\" in safe or ".." in safe:
        raise TemplateRejectedError(
            f"非法的数据集名：{name!r}（只允许 templates/data/ 下的 json 文件）",
            detail={"dataset": name},
        )
    if safe in _DATASET_CACHE:
        return _DATASET_CACHE[safe]
    path = DATA_DIR / safe
    if not path.is_file():
        raise TemplateRejectedError(
            f"数据集资产缺失：{path}",
            detail={"dataset": safe, "expected_dir": str(DATA_DIR)},
        )
    payload = json.loads(path.read_text(encoding="utf-8"))
    items = payload.get("items")
    if not isinstance(items, list) or not items:
        raise TemplateRejectedError(
            f"数据集 {safe} 缺少非空的 items 数组", detail={"dataset": safe}
        )
    _DATASET_CACHE[safe] = payload
    return payload


def dataset_items(name: str = DEFAULT_DATASET) -> list[dict[str, Any]]:
    """按 ``id`` 升序返回样本（**顺序固定**，保证跨环境同一样本序列）。"""
    payload = load_dataset(name)
    items = [dict(item) for item in payload["items"] if isinstance(item, Mapping)]
    return sorted(items, key=lambda item: str(item.get("id") or ""))


def dataset_manifest(
    name: str = DEFAULT_DATASET, *, sample_ids: Sequence[str] | None = None
) -> dict[str, Any]:
    """数据集 manifest（Passport 的 ``dataset_sha256`` / ``sample_manifest`` 来源）。"""
    payload = load_dataset(name)
    items = dataset_items(name)
    selected = {str(sid) for sid in (sample_ids or [])}
    rows = [
        {
            "id": str(item.get("id")),
            "task": item.get("task"),
            "input_sha256_source": "inline",
        }
        for item in items
        if not selected or str(item.get("id")) in selected
    ]
    return {
        "dataset_name": payload.get("dataset_name"),
        "dataset_version": payload.get("dataset_version"),
        "provenance": payload.get("provenance"),
        "source_file": name,
        "total_items": len(items),
        "selected_items": len(rows),
        "sample_ids": [row["id"] for row in rows],
        "tasks": sorted({str(item.get("task")) for item in items}),
    }


# --------------------------------------------------------------------------- #
# 参数校验
# --------------------------------------------------------------------------- #
def _type_ok(value: Any, expected: str) -> bool:
    if expected == "string":
        return isinstance(value, str)
    if expected == "integer":
        return isinstance(value, int) and not isinstance(value, bool)
    if expected == "number":
        return isinstance(value, (int, float)) and not isinstance(value, bool)
    if expected == "list[string]":
        return isinstance(value, list) and all(isinstance(item, str) for item in value)
    if expected == "boolean":
        return isinstance(value, bool)
    return True


def validate_params(spec: TemplateSpec, params: Any) -> dict[str, Any]:
    """按 ``spec.params_schema`` 校验并归一参数（未知键/类型错/越界一律拒绝）。"""
    raw = dict(params) if isinstance(params, Mapping) else {}
    unknown = sorted(set(raw) - set(spec.params_schema))
    if unknown:
        raise TemplateRejectedError(
            f"模板 {spec.template_id} 不接受参数 {unknown}；"
            f"允许的键：{sorted(spec.params_schema)}",
            detail={"template_id": spec.template_id, "unknown_params": unknown},
        )
    resolved: dict[str, Any] = {}
    for key, schema in spec.params_schema.items():
        if key in raw and raw[key] is not None:
            value = raw[key]
            expected = str(schema.get("type") or "")
            if not _type_ok(value, expected):
                raise TemplateRejectedError(
                    f"参数 {key} 类型不符：期望 {expected}，收到 {type(value).__name__}",
                    detail={"template_id": spec.template_id, "param": key, "expected": expected},
                )
            if expected == "list[string]":
                min_items = schema.get("min_items")
                max_items = schema.get("max_items")
                if min_items is not None and len(value) < int(min_items):
                    raise TemplateRejectedError(
                        f"参数 {key} 至少需要 {min_items} 项（收到 {len(value)}）",
                        detail={"template_id": spec.template_id, "param": key},
                    )
                if max_items is not None and len(value) > int(max_items):
                    raise TemplateRejectedError(
                        f"参数 {key} 至多 {max_items} 项（收到 {len(value)}）",
                        detail={"template_id": spec.template_id, "param": key},
                    )
            if isinstance(value, (int, float)) and not isinstance(value, bool):
                low, high = schema.get("min"), schema.get("max")
                if low is not None and float(value) < float(low):
                    raise TemplateRejectedError(
                        f"参数 {key}={value} 小于下限 {low}",
                        detail={"template_id": spec.template_id, "param": key, "min": low},
                    )
                if high is not None and float(value) > float(high):
                    raise TemplateRejectedError(
                        f"参数 {key}={value} 超过上限 {high}",
                        detail={"template_id": spec.template_id, "param": key, "max": high},
                    )
            resolved[key] = value
        else:
            resolved[key] = schema.get("default")
    return resolved


def _validate_samples(samples: Any, *, sample_size: int, template_id: str) -> list[dict[str, Any]]:
    if not isinstance(samples, Sequence) or isinstance(samples, (str, bytes)):
        raise TemplateRejectedError(
            "samples 必须是数组（每项含 id 与 input）",
            detail={"template_id": template_id, "samples_type": type(samples).__name__},
        )
    rows: list[dict[str, Any]] = []
    for index, item in enumerate(samples):
        if not isinstance(item, Mapping):
            raise TemplateRejectedError(
                f"samples[{index}] 必须是对象", detail={"template_id": template_id, "index": index}
            )
        row = dict(item)
        if not str(row.get("id") or "").strip():
            raise TemplateRejectedError(
                f"samples[{index}] 缺少 id", detail={"template_id": template_id, "index": index}
            )
        if not str(row.get("input") or row.get("question") or "").strip():
            raise TemplateRejectedError(
                f"samples[{index}] 缺少 input（题干）",
                detail={"template_id": template_id, "index": index},
            )
        rows.append(row)
    if len(rows) != sample_size:
        raise TemplateRejectedError(
            f"samples 数量（{len(rows)}）与 sample_size（{sample_size}）不一致",
            detail={"template_id": template_id, "samples": len(rows), "sample_size": sample_size},
        )
    return rows


def validate_config(template_id: str, config: Any) -> tuple[TemplateSpec, dict[str, Any]]:
    """模板白名单 → schema → sample_size → 样本装配（返回 ``(spec, validated)``）。

    ``validated`` 结构：``{template_id, sample_size, params, samples, model_refs,
    dataset, prompt_version, template_version}``。
    """
    name = str(template_id or "").strip()
    if name not in TEMPLATE_IDS:
        raise TemplateRejectedError(
            f"模板 {template_id!r} 不在白名单内；合法值：{list(TEMPLATE_IDS)}",
            detail={"template_id": template_id, "whitelist": list(TEMPLATE_IDS)},
        )
    spec = TEMPLATES[name]
    if not spec.implemented:
        raise TemplateNotImplementedError(
            f"模板 {name} 尚未实现（{spec.placeholder_reason or 'P1 路线图'}）："
            "占位模板禁止执行，请改用 T1_prompt_variant 或 T3_model_compare",
            detail={"template_id": name, "status": spec.status, "p0_templates": list(P0_TEMPLATES)},
        )

    cfg = dict(config) if isinstance(config, Mapping) else {}
    raw_size = cfg.get("sample_size", spec.sample_size_default)
    if isinstance(raw_size, bool) or not isinstance(raw_size, int):
        raise TemplateRejectedError(
            f"sample_size 必须是整数（收到 {type(raw_size).__name__}）",
            detail={"template_id": name, "sample_size": raw_size},
        )
    if raw_size > SAMPLE_SIZE_MAX:
        raise SampleSizeExceededError(
            f"sample_size={raw_size} 超过硬上限 {SAMPLE_SIZE_MAX}"
            "（contracts.guardrails.safety，阈值不可放宽、不做隐式截断）",
            detail={
                "template_id": name,
                "sample_size": raw_size,
                "max": SAMPLE_SIZE_MAX,
                "env_key": "PASSPORT_* / 契约固定值",
            },
        )
    if raw_size < spec.sample_size_min:
        raise TemplateRejectedError(
            f"sample_size={raw_size} 低于模板下限 {spec.sample_size_min}"
            "（计划书 §4.7：模板样本量必须为 20–50 条）",
            detail={"template_id": name, "sample_size": raw_size, "min": spec.sample_size_min},
        )

    params = validate_params(spec, cfg.get("params"))

    dataset_name = str(cfg.get("dataset") or spec.dataset or DEFAULT_DATASET)
    if cfg.get("samples") is not None:
        samples = _validate_samples(cfg.get("samples"), sample_size=raw_size, template_id=name)
        dataset_info = {
            "dataset_name": "caller_supplied",
            "dataset_version": "caller_supplied",
            "source_file": None,
            "selected_items": len(samples),
            "provenance": "由调用方显式提供的 samples（未使用内置评测集）",
        }
    else:
        offset = int(params.get("sample_offset") or 0)
        all_items = dataset_items(dataset_name)
        selected = all_items[offset : offset + raw_size]
        if len(selected) < raw_size:
            raise TemplateRejectedError(
                f"数据集 {dataset_name} 可用样本不足：需要 {raw_size} 条"
                f"（offset={offset}），实际只有 {len(selected)} 条",
                detail={
                    "template_id": name,
                    "dataset": dataset_name,
                    "available": len(all_items),
                    "offset": offset,
                },
            )
        samples = [
            {
                "id": str(item.get("id")),
                "task": item.get("task"),
                "input": item.get("input"),
                "reference": item.get("reference"),
            }
            for item in selected
        ]
        dataset_info = dataset_manifest(
            dataset_name, sample_ids=[str(item["id"]) for item in selected]
        )

    model_refs: list[str] = []
    if params.get("model_ref"):
        model_refs = [str(params["model_ref"])]
    elif params.get("model_refs"):
        model_refs = [str(ref) for ref in params["model_refs"]]

    validated: dict[str, Any] = {
        "template_id": name,
        "template_version": spec.version,
        "prompt_version": spec.prompt_version,
        "sample_size": raw_size,
        "params": params,
        "samples": samples,
        "model_refs": model_refs,
        "dataset": dataset_info,
    }
    logger.info(
        "模板配置通过校验 template=%s sample_size=%s model_refs=%s dataset=%s",
        name,
        raw_size,
        model_refs or "auto",
        dataset_info.get("dataset_version"),
    )
    return spec, validated


# --------------------------------------------------------------------------- #
# 执行分发
# --------------------------------------------------------------------------- #
async def execute(template_id: str, ctx: Any) -> dict[str, Any]:
    """调用模板实现（唯一分发点；模板实现由 ``spec.module`` 指定）。"""
    import importlib

    name = str(template_id or "").strip()
    spec = TEMPLATES.get(name)
    if spec is None or not spec.implemented or not spec.module:
        raise TemplateNotImplementedError(
            f"模板 {name} 无可执行实现", detail={"template_id": name}
        )
    module = importlib.import_module(spec.module)
    runner = getattr(module, "run", None)
    if not callable(runner):
        raise TemplateNotImplementedError(
            f"模板模块 {spec.module} 未导出 run(ctx)", detail={"template_id": name}
        )
    return await runner(ctx)


def list_templates(*, implemented_only: bool = False) -> list[dict[str, Any]]:
    """模板清单（``GET /experiments/templates``）。"""
    rows = [TEMPLATES[key].to_dict() for key in TEMPLATE_IDS]
    if implemented_only:
        rows = [row for row in rows if row["implemented"]]
    return rows


def get_spec(template_id: str) -> TemplateSpec:
    spec = TEMPLATES.get(str(template_id or "").strip())
    if spec is None:
        raise TemplateRejectedError(
            f"模板 {template_id!r} 不在白名单内", detail={"whitelist": list(TEMPLATE_IDS)}
        )
    return spec


def registry_snapshot() -> dict[str, Any]:
    """注册表快照（验收与审计用）。"""
    return {
        "whitelist": list(TEMPLATE_IDS),
        "p0_implemented": list(P0_TEMPLATES),
        "implemented": [key for key in TEMPLATE_IDS if TEMPLATES[key].implemented],
        "placeholders": [key for key in TEMPLATE_IDS if not TEMPLATES[key].implemented],
        "sample_size_max": SAMPLE_SIZE_MAX,
        "dataset_dir": str(DATA_DIR),
    }


def iter_implemented() -> Iterable[TemplateSpec]:
    for key in TEMPLATE_IDS:
        if TEMPLATES[key].implemented:
            yield TEMPLATES[key]


__all__ = [
    "COMMON_PARAMS",
    "DATA_DIR",
    "DEFAULT_DATASET",
    "P0_TEMPLATES",
    "SAMPLE_SIZE_MAX",
    "SAMPLE_SIZE_MIN",
    "TEMPLATES",
    "TEMPLATE_IDS",
    "TemplateSpec",
    "dataset_items",
    "dataset_manifest",
    "execute",
    "get_spec",
    "iter_implemented",
    "list_templates",
    "load_dataset",
    "registry_snapshot",
    "validate_config",
    "validate_params",
]
