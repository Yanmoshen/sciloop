# Copyright 2026 SciLoop contributors
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
"""结构化输出解析与 JSON Schema 校验。

校验一律在**本地**完成：上游的 ``response_format`` 只是「让模型更容易输出对的 JSON」的辅助手段，
真正决定「这次调用算不算成功」的是本地 ``jsonschema`` 校验。这样即便供应商不支持结构化输出，
校验口径也不会变化。
"""

from __future__ import annotations

import json
import logging
import re
from typing import Any

from llm.errors import LLMError

logger = logging.getLogger(__name__)

_FENCE_RE = re.compile(r"^\s*```(?:json|JSON)?\s*(.*?)\s*```\s*$", re.DOTALL)


def strip_code_fences(text: str) -> str:
    """去掉模型习惯性包裹的 ```json 代码块与前后空白。"""
    if not text:
        return ""
    stripped = text.strip()
    match = _FENCE_RE.match(stripped)
    if match:
        return match.group(1).strip()
    # 兜底：截取首个 { 到最后一个 }
    if stripped and not stripped.startswith(("{", "[")):
        start = min(
            (idx for idx in (stripped.find("{"), stripped.find("[")) if idx != -1),
            default=-1,
        )
        end = max(stripped.rfind("}"), stripped.rfind("]"))
        if start != -1 and end > start:
            return stripped[start : end + 1].strip()
    return stripped


def load_json_payload(content: str) -> tuple[Any, str | None]:
    """解析 JSON。返回 ``(payload, error_message)``。"""
    text = strip_code_fences(content or "")
    if not text:
        return None, "模型返回空内容，无法解析为 JSON"
    try:
        return json.loads(text), None
    except json.JSONDecodeError as exc:
        return None, f"JSON 解析失败：{exc.msg} (line {exc.lineno} col {exc.colno})"


def _validator_module() -> Any:
    try:
        import jsonschema
    except ImportError as exc:  # pragma: no cover
        raise LLMError(
            "缺少 jsonschema 依赖，无法执行结构化输出校验。"
            "请确认 server/pyproject.toml 已包含 jsonschema。",
            detail={"missing": "jsonschema"},
        ) from exc
    return jsonschema


def validate_json_schema(payload: Any, json_schema: dict[str, Any]) -> str | None:
    """按 JSON Schema 校验；通过返回 ``None``，失败返回可读错误信息。"""
    jsonschema = _validator_module()
    try:
        validator_cls = jsonschema.validators.validator_for(json_schema)
        validator = validator_cls(json_schema)
        errors = sorted(validator.iter_errors(payload), key=lambda e: list(e.absolute_path))
    except Exception as exc:  # noqa: BLE001 - schema 本身写错
        return f"JSON Schema 自身非法：{exc}"
    if not errors:
        return None
    first = errors[0]
    path = "/".join(str(p) for p in first.absolute_path) or "<root>"
    detail = f"{path}: {first.message}"
    if len(errors) > 1:
        detail += f"（另有 {len(errors) - 1} 处不合规）"
    return detail


def parse_and_validate(content: str, json_schema: dict[str, Any]) -> tuple[bool, Any, str | None]:
    """解析 + 校验一步到位。返回 ``(ok, payload, error_message)``。"""
    payload, parse_error = load_json_payload(content)
    if parse_error:
        return False, None, parse_error
    schema_error = validate_json_schema(payload, json_schema)
    if schema_error:
        return False, payload, schema_error
    return True, payload, None


__all__ = [
    "load_json_payload",
    "parse_and_validate",
    "strip_code_fences",
    "validate_json_schema",
]
