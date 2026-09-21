# Copyright 2026 SciLoop contributors
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
"""环节注册表（WP09-T2）。

六环节顺序**固定**为 ``survey → plan → plan_review → experiment → writing → review``
（``contracts.enums.pipeline_stage``）。其他工作包通过 :func:`register_stage` 注入实现：

- WP12 → ``plan_review``
- WP11 → ``experiment``
- WP14 → ``writing`` / ``review``

:func:`get_stage` 在环节缺失时抛 :class:`StageNotImplementedError`（**禁止静默跳过**）。
"""

from __future__ import annotations

import contextlib
import logging
from typing import Any

from services.pipeline.stages.base import (
    STAGE_DECISION_POINT,
    STAGE_INTERVENTION_NODE,
    STAGE_ORDER,
    Stage,
    StageContext,
    StageNotImplementedError,
    StageResult,
)

logger = logging.getLogger("sciloop.pipeline.registry")

_REGISTRY: dict[str, Stage] = {}
_BUILTINS_LOADED = False

#: 本包自带的环节实现（其余由 WP11/WP12/WP14 注入）
_BUILTIN_MODULES: tuple[tuple[str, str], ...] = (
    ("survey", "services.pipeline.stages.survey"),
    ("plan", "services.pipeline.stages.plan"),
)


def _validate_name(stage_name: str) -> str:
    name = str(stage_name or "").strip()
    if name not in STAGE_ORDER:
        raise ValueError(
            f"未知环节 '{stage_name}'：合法值仅 {list(STAGE_ORDER)}（contracts.enums.pipeline_stage）"
        )
    return name


def register_stage(stage_name: str, handler: Stage | Any) -> Stage:
    """注册（或覆盖）一个环节实现，返回被注册的处理器。

    ``handler`` 可以是任意带 ``name`` 与 ``async run(ctx)`` 的对象；若缺少 ``name``
    属性，会被自动补上（便于用普通类/函数对象注入）。
    """
    name = _validate_name(stage_name)
    if handler is None or not hasattr(handler, "run"):
        raise TypeError(f"环节 '{name}' 的实现必须提供 async run(ctx) 方法，收到 {handler!r}")
    with contextlib.suppress(AttributeError, TypeError):  # pragma: no cover - frozen 对象
        handler.name = name  # type: ignore[attr-defined]
    if getattr(handler, "decision_point", "missing") == "missing":
        with contextlib.suppress(AttributeError, TypeError):  # pragma: no cover
            handler.decision_point = STAGE_DECISION_POINT.get(name)  # type: ignore[attr-defined]
    previous = _REGISTRY.get(name)
    _REGISTRY[name] = handler
    logger.info(
        "环节已注册 stage=%s handler=%s replaced=%s",
        name,
        type(handler).__name__,
        previous is not None,
    )
    return handler  # type: ignore[return-value]


def unregister_stage(stage_name: str) -> bool:
    """移除环节实现（供测试/验收清理使用），返回是否原本存在。"""
    return _REGISTRY.pop(str(stage_name or "").strip(), None) is not None


def _load_builtins() -> None:
    global _BUILTINS_LOADED
    if _BUILTINS_LOADED:
        return
    _BUILTINS_LOADED = True
    import importlib

    for name, module_path in _BUILTIN_MODULES:
        if name in _REGISTRY:
            continue
        try:
            module = importlib.import_module(module_path)
        except ModuleNotFoundError:  # pragma: no cover - 交付不完整时才发生
            logger.warning("内置环节模块缺失 stage=%s module=%s", name, module_path)
            continue
        handler = getattr(module, "STAGE", None) or getattr(module, "HANDLER", None)
        if handler is None:
            logger.warning("环节模块未导出 STAGE stage=%s module=%s", name, module_path)
            continue
        register_stage(name, handler)


def get_stage(stage_name: str) -> Stage:
    """取环节实现；未注册则抛 :class:`StageNotImplementedError`。"""
    name = _validate_name(stage_name)
    _load_builtins()
    handler = _REGISTRY.get(name)
    if handler is None:
        raise StageNotImplementedError(name, detail={"registered": sorted(_REGISTRY)})
    return handler


def has_stage(stage_name: str) -> bool:
    _load_builtins()
    return str(stage_name or "").strip() in _REGISTRY


def registered_stages() -> list[str]:
    _load_builtins()
    return [name for name in STAGE_ORDER if name in _REGISTRY]


def missing_stages() -> list[str]:
    """尚未实现的环节（用于 status 端点与验收审计）。"""
    _load_builtins()
    return [name for name in STAGE_ORDER if name not in _REGISTRY]


def registry_snapshot() -> dict[str, Any]:
    """注册表快照（`GET /pipelines/{pid}/status` 内返回，透明可见）。"""
    _load_builtins()
    return {
        "stage_order": list(STAGE_ORDER),
        "registered": registered_stages(),
        "missing": missing_stages(),
        "decision_points": dict(STAGE_DECISION_POINT),
        "intervention_nodes": dict(STAGE_INTERVENTION_NODE),
    }


__all__ = [
    "STAGE_DECISION_POINT",
    "STAGE_INTERVENTION_NODE",
    "STAGE_ORDER",
    "Stage",
    "StageContext",
    "StageNotImplementedError",
    "StageResult",
    "get_stage",
    "has_stage",
    "missing_stages",
    "register_stage",
    "registered_stages",
    "registry_snapshot",
    "unregister_stage",
]
