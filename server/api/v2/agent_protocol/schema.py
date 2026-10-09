# Copyright 2026 SciLoop contributors
# Licensed under the Apache License, Version 2.0 (the "License");
"""协议 JSON Schema 的加载与校验。

单一事实来源是 ``jsonrpc.schema.json``；校验用官方 ``jsonschema``（与冻结契约
``server/contracts/agent_v2/validate.py`` 同一引擎、同一依赖），**不自研校验器**。
"""

from __future__ import annotations

import json
from functools import cache
from pathlib import Path
from typing import Any

from contracts.agent_v2.validate import SchemaValidator

from .errors import ErrorCode, ProtocolError

SCHEMA_FILENAME = "jsonrpc.schema.json"
_SCHEMA_PATH = Path(__file__).resolve().parent / SCHEMA_FILENAME

#: 可校验的帧分支（对应 schema 内的 ``$defs``）。
FRAME_KINDS: tuple[str, ...] = ("request", "response", "notification", "error", "frame")


@cache
def load_protocol_schema() -> dict[str, Any]:
    """读取协议 schema（进程内缓存）。"""
    with _SCHEMA_PATH.open("r", encoding="utf-8") as fh:
        return json.load(fh)


def frame_validator(kind: str) -> SchemaValidator:
    """取某个帧分支的校验器；``$ref`` 从文档根解析。"""
    doc = load_protocol_schema()
    branch = (doc.get("$defs") or {}).get(kind)
    if not isinstance(branch, dict):
        raise ProtocolError(
            ErrorCode.INTERNAL_ERROR,
            f"protocol schema has no branch {kind!r}",
            data={"kind": kind},
        )
    return SchemaValidator(branch, root=doc)


def validate_frame(frame: Any, kind: str, *, label: str | None = None) -> None:
    """校验一帧；失败抛 :class:`ProtocolError`（``invalid_request`` / ``internal_error``）。"""
    problems = frame_validator(kind).errors(frame)
    if not problems:
        return
    raise ProtocolError(
        ErrorCode.INVALID_REQUEST,
        f"{label or kind} frame does not satisfy {SCHEMA_FILENAME}: " + "; ".join(sorted(problems)[:6]),
        data={"kind": kind, "problems": sorted(problems)[:6]},
    )


def try_parse_json(raw: str | bytes) -> Any:
    """解析 JSON 文本；失败抛 :class:`ProtocolError`（``invalid_request``）。"""
    try:
        if isinstance(raw, bytes):
            raw = raw.decode("utf-8")
        return json.loads(raw)
    except (ValueError, UnicodeDecodeError) as exc:
        raise ProtocolError(
            ErrorCode.INVALID_REQUEST,
            f"frame is not valid JSON: {exc}",
            data={"exception": type(exc).__name__},
        ) from exc


__all__ = [
    "SCHEMA_FILENAME",
    "FRAME_KINDS",
    "load_protocol_schema",
    "frame_validator",
    "validate_frame",
    "try_parse_json",
]
