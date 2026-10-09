"""契约 JSON Schema 加载与校验。

**校验引擎用官方 `jsonschema` 库**，不自研校验器——`server/pyproject.toml` 已把
`jsonschema>=4.21` 列为硬依赖（项目 WP02 的硬要求就是"结构化输出的本地 JSON Schema
校验，禁止自研校验器"），因此本模块不需要新增任何依赖。

对外接口：

    validator_for("events").check(event.to_dict())        # 失败抛 ContractViolation
    validator_for("events").errors(event.to_dict())       # 返回可读错误列表
    SchemaValidator.branch("tool_call", "$defs/toolSpec") # 校验文档内的子模式
    schema_enum(load_schema("turn"), "status")            # 取字段的合法枚举
"""

from __future__ import annotations

import json
from collections.abc import Iterable
from functools import cache
from pathlib import Path
from typing import Any

from jsonschema import Draft202012Validator
from jsonschema.exceptions import SchemaError

from .errors import ContractViolation

_SCHEMA_DIR = Path(__file__).resolve().parent

#: 契约包内含的全部 schema 文件名。
SCHEMA_FILES: tuple[str, ...] = (
    "events.schema.json",
    "thread.schema.json",
    "turn.schema.json",
    "items.schema.json",
    "tool_call.schema.json",
    "model_stream.schema.json",
    "approval_request.schema.json",
    "memory.schema.json",
)


@cache
def load_schema(name: str) -> dict[str, Any]:
    """按文件名或裸名加载契约 schema（进程内缓存）。"""
    filename = name if name.endswith(".json") else f"{name}.schema.json"
    path = _SCHEMA_DIR / filename
    if not path.is_file():
        raise ContractViolation(f"schema file not found: {filename}")
    with path.open("r", encoding="utf-8") as fh:
        schema = json.load(fh)
    try:
        Draft202012Validator.check_schema(schema)
    except SchemaError as exc:  # pragma: no cover - schema 自身写错才会走到
        raise ContractViolation(f"schema {filename} is invalid: {exc.message}") from exc
    return schema


def _pointer(doc: dict[str, Any], pointer: str) -> dict[str, Any]:
    node: Any = doc
    for part in pointer.strip("/").split("/"):
        part = part.replace("~1", "/").replace("~0", "~")
        if not isinstance(node, dict) or part not in node:
            raise ContractViolation(f"schema has no branch {pointer!r}")
        node = node[part]
    if not isinstance(node, dict):
        raise ContractViolation(f"branch {pointer!r} is not a schema object")
    return node


def _format_error(error: Any) -> str:
    location = "$" + "".join(
        f"[{part}]" if isinstance(part, int) else f".{part}" for part in error.absolute_path
    )
    return f"{location}: {error.message}"


class SchemaValidator:
    """针对某份 schema（或其中某个子模式）的校验器。

    :param schema: 生效的子模式。
    :param root: 文档根。给了 ``root`` 时按子模式校验，但 ``$ref`` 仍从根解析。
        用 :meth:`branch` 构造最省事。
    """

    def __init__(self, schema: dict[str, Any], *, root: dict[str, Any] | None = None) -> None:
        if not isinstance(schema, dict):
            raise ContractViolation("schema must be an object")
        self.schema = schema
        self.root = root if root is not None else schema
        # 校验器建在文档根上，子模式通过 descend 使用——这样 #/$defs/... 才能解析
        self._validator = Draft202012Validator(self.root)

    @classmethod
    def branch(cls, name: str, pointer: str) -> SchemaValidator:
        """按 JSON Pointer 校验文档内的子模式，``$ref`` 从文档根解析。

        例：``SchemaValidator.branch("tool_call", "$defs/toolSpec")``
        """
        doc = load_schema(name)
        return cls(_pointer(doc, pointer), root=doc)

    # ---- 公开接口 ----
    def errors(self, instance: Any) -> list[str]:
        """返回可读错误列表；空列表表示通过。"""
        if self.schema is self.root:
            errors: Iterable[Any] = self._validator.iter_errors(instance)
        else:
            errors = self._validator.descend(instance, self.schema)
        return [_format_error(e) for e in errors]

    def check(self, instance: Any, *, label: str = "instance") -> None:
        """校验失败时抛 :class:`ContractViolation`（消息里带前若干条错误）。"""
        problems = self.errors(instance)
        if problems:
            raise ContractViolation(
                f"{label} does not satisfy schema: " + "; ".join(sorted(problems)[:6])
            )

    def is_valid(self, instance: Any) -> bool:
        return not self.errors(instance)


def validator_for(name: str) -> SchemaValidator:
    """取得某个契约对象的校验器。"""
    return SchemaValidator(load_schema(name))


def validate_all_present() -> list[str]:
    """确认 8 份 schema 文件都在，返回缺失清单（供测试与启动自检使用）。"""
    return [f for f in SCHEMA_FILES if not (_SCHEMA_DIR / f).is_file()]


def schema_enum(schema: dict[str, Any], field_path: str) -> list[str] | None:
    """从 schema 中取出某个字段的 enum 列表（``field_path`` 形如 ``type`` 或 ``$defs.toolKind``）。"""
    node: Any = schema
    for part in field_path.split("."):
        if not isinstance(node, dict):
            return None
        if part in (node.get("properties") or {}):
            node = node["properties"][part]
        elif part in (node.get("$defs") or {}):
            node = node["$defs"][part]
        else:
            return None
    enum = node.get("enum") if isinstance(node, dict) else None
    return list(enum) if isinstance(enum, list) else None


__all__ = [
    "SCHEMA_FILES",
    "SchemaValidator",
    "load_schema",
    "validator_for",
    "validate_all_present",
    "schema_enum",
]
