# Copyright 2026 SciLoop contributors
# Licensed under the Apache License, Version 2.0 (the "License")
"""工具 Schema 片段（Agent 2 / WP-01）。

工具声明里的输入/输出 Schema 重复度很高，这里集中定义片段，避免各文件抄写出错，
也让「参数校验用官方 jsonschema」这条要求只有一个来源。
"""

from __future__ import annotations

from typing import Any

#: 任意对象（输出 Schema 用：结构字段已固定，附加字段允许）
ANY_OBJECT: dict[str, Any] = {"type": "object", "additionalProperties": True}

#: 严格对象（输入 Schema 用：多一个字段就拒绝，避免模型塞私有参数）
STRICT_OBJECT: dict[str, Any] = {"type": "object", "additionalProperties": False}

PATH_PROP: dict[str, Any] = {"type": "string", "minLength": 1}
OPTIONAL_PATH_PROP: dict[str, Any] = {"type": "string"}
ARGV_PROP: dict[str, Any] = {
    "type": "array",
    "minItems": 1,
    "items": {"type": "string", "minLength": 1},
}
STRING_LIST_PROP: dict[str, Any] = {"type": "array", "items": {"type": "string"}}
TIMEOUT_PROP: dict[str, Any] = {"type": "number", "exclusiveMinimum": 0}
BYTES_PROP: dict[str, Any] = {"type": "integer", "minimum": 1}

HOST_EXEC_OUTPUT: dict[str, Any] = {
    "type": "object",
    "additionalProperties": True,
    "properties": {
        "execution_id": {"type": "string"},
        "argv": {"type": "array"},
        "cwd": {"type": "string"},
        "status": {"type": "string"},
        "exit_code": {"type": ["integer", "null"]},
        "stdout": {"type": "string"},
        "stderr": {"type": "string"},
        "duration_ms": {"type": ["integer", "null"]},
        "truncated": {"type": "boolean"},
    },
}


def strict_object(
    *,
    required: tuple[str, ...] = (),
    properties: dict[str, Any] | None = None,
    description: str | None = None,
) -> dict[str, Any]:
    """构造严格输入 Schema（``additionalProperties=False``）。"""
    schema: dict[str, Any] = {
        "type": "object",
        "additionalProperties": False,
        "properties": dict(properties or {}),
    }
    if required:
        schema["required"] = list(required)
    if description:
        schema["description"] = description
    return schema


#: 宿主文件工具共用的输出字段
FILE_ENTRY: dict[str, Any] = {
    "type": "object",
    "properties": {
        "name": {"type": "string"},
        "path": {"type": "string"},
        "is_dir": {"type": "boolean"},
        "size": {"type": "integer"},
    },
}


__all__ = [
    "ANY_OBJECT",
    "STRICT_OBJECT",
    "PATH_PROP",
    "OPTIONAL_PATH_PROP",
    "ARGV_PROP",
    "STRING_LIST_PROP",
    "TIMEOUT_PROP",
    "BYTES_PROP",
    "HOST_EXEC_OUTPUT",
    "FILE_ENTRY",
    "strict_object",
]
