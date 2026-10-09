"""契约 JSON Schema 加载与校验（纯标准库实现）。

为什么自己实现而不用 `jsonschema` 包：计划书 §2 明确禁止修改依赖锁文件，
且本契约包要被 Agent 2 / Agent 3 直接 import，越少的环境耦合越好。

支持的 JSON Schema 关键字子集（覆盖本契约全部用法）：
``type`` / ``const`` / ``enum`` / ``required`` / ``properties`` /
``additionalProperties``(bool|schema) / ``items`` / ``minLength`` / ``minimum`` /
``exclusiveMinimum`` / ``maximum`` / ``pattern`` / ``oneOf`` / ``anyOf`` / ``allOf`` /
``$ref``(仅本地 ``#/$defs/*``) / ``$defs``。

``format`` 只作为文档性标注，不做校验（date-time 由模型层保证）。
"""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any, Iterable, Optional

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


def _type_ok(expected: str, value: Any) -> bool:
    if expected == "object":
        return isinstance(value, dict)
    if expected == "array":
        return isinstance(value, (list, tuple))
    if expected == "string":
        return isinstance(value, str)
    if expected == "integer":
        return isinstance(value, int) and not isinstance(value, bool)
    if expected == "number":
        return isinstance(value, (int, float)) and not isinstance(value, bool)
    if expected == "boolean":
        return isinstance(value, bool)
    if expected == "null":
        return value is None
    return True  # 未知类型不做判定


class SchemaValidator:
    """针对单份 schema 文档的校验器。

    :param schema: 当前生效的子模式（可以是文档本身，也可以是 ``$defs`` 里的分支）。
    :param root: ``$ref`` 的解析根。抽取分支校验时**必须**传入文档根，
        否则 ``#/$defs/...`` 无法解析——用 :meth:`branch` 最省事。
    """

    def __init__(self, schema: dict[str, Any], *, root: Optional[dict[str, Any]] = None) -> None:
        if not isinstance(schema, dict):
            raise ContractViolation("schema must be an object")
        self.schema = schema
        self.root = root if root is not None else schema

    @classmethod
    def branch(cls, name: str, pointer: str) -> "SchemaValidator":
        """按 JSON Pointer 校验文档内的子模式，``$ref`` 仍从文档根解析。

        例：``SchemaValidator.branch("tool_call", "$defs/toolSpec")``
        """
        doc = load_schema(name)
        node: Any = doc
        for part in pointer.strip("/").split("/"):
            part = part.replace("~1", "/").replace("~0", "~")
            if not isinstance(node, dict) or part not in node:
                raise ContractViolation(f"schema {name!r} has no branch {pointer!r}")
            node = node[part]
        if not isinstance(node, dict):
            raise ContractViolation(f"branch {pointer!r} of {name!r} is not a schema object")
        return cls(node, root=doc)

    # ---- 公开接口 ----
    def errors(self, instance: Any, *, path: str = "$") -> list[str]:
        """返回错误消息列表，空列表表示通过。"""
        out: list[str] = []
        self._walk(self.schema, instance, path, out)
        return out

    def check(self, instance: Any, *, label: str = "instance") -> None:
        """校验失败时抛 :class:`ContractViolation`。"""
        errs = self.errors(instance)
        if errs:
            raise ContractViolation(f"{label} does not satisfy schema: " + "; ".join(errs[:6]))

    # ---- 内部 ----
    def _resolve(self, ref: str) -> dict[str, Any]:
        if not ref.startswith("#/"):
            raise ContractViolation(f"only local $ref is supported, got {ref!r}")
        node: Any = self.root
        for part in ref[2:].split("/"):
            part = part.replace("~1", "/").replace("~0", "~")
            if not isinstance(node, dict) or part not in node:
                raise ContractViolation(f"unresolvable $ref {ref!r}")
            node = node[part]
        if not isinstance(node, dict):
            raise ContractViolation(f"$ref {ref!r} does not point to a schema object")
        return node

    def _walk(self, schema: dict[str, Any], value: Any, path: str, out: list[str]) -> None:
        if "$ref" in schema:
            merged = dict(self._resolve(schema["$ref"]))
            for k, v in schema.items():
                if k != "$ref" and k not in merged:
                    merged[k] = v
            schema = merged

        for kw in ("allOf",):
            for sub in schema.get(kw, []) or []:
                self._walk(sub, value, path, out)

        if "anyOf" in schema:
            if not any(not self._sub_errors(sub, value) for sub in schema["anyOf"]):
                out.append(f"{path}: matches none of anyOf branches")
        if "oneOf" in schema:
            hits = sum(1 for sub in schema["oneOf"] if not self._sub_errors(sub, value))
            if hits != 1:
                out.append(f"{path}: expected exactly 1 matching oneOf branch, got {hits}")

        if "const" in schema and value != schema["const"]:
            out.append(f"{path}: expected const {schema['const']!r}, got {value!r}")

        if "enum" in schema and value not in schema["enum"]:
            out.append(f"{path}: {value!r} not in enum {schema['enum']}")

        if "type" in schema:
            expected = schema["type"]
            names: Iterable[str] = [expected] if isinstance(expected, str) else expected
            if not any(_type_ok(t, value) for t in names):
                out.append(f"{path}: expected type {sorted(names)}, got {type(value).__name__}")

        if isinstance(value, str):
            if "minLength" in schema and len(value) < schema["minLength"]:
                out.append(f"{path}: shorter than minLength {schema['minLength']}")
            if "pattern" in schema and not re.search(schema["pattern"], value):
                out.append(f"{path}: does not match pattern {schema['pattern']!r}")

        if isinstance(value, (int, float)) and not isinstance(value, bool):
            if "minimum" in schema and value < schema["minimum"]:
                out.append(f"{path}: below minimum {schema['minimum']}")
            if "exclusiveMinimum" in schema and value <= schema["exclusiveMinimum"]:
                out.append(f"{path}: not greater than exclusiveMinimum {schema['exclusiveMinimum']}")
            if "maximum" in schema and value > schema["maximum"]:
                out.append(f"{path}: above maximum {schema['maximum']}")

        if isinstance(value, dict):
            self._walk_object(schema, value, path, out)

        if isinstance(value, (list, tuple)):
            item_schema = schema.get("items")
            if isinstance(item_schema, dict):
                for idx, item in enumerate(value):
                    self._walk(item_schema, item, f"{path}[{idx}]", out)

    def _walk_object(
        self, schema: dict[str, Any], value: dict[str, Any], path: str, out: list[str]
    ) -> None:
        props = schema.get("properties") or {}
        for req in schema.get("required", []) or []:
            if req not in value:
                out.append(f"{path}: missing required field '{req}'")
        addl = schema.get("additionalProperties", True)
        for key, item in value.items():
            if key in props:
                self._walk(props[key], item, f"{path}.{key}", out)
            elif addl is False:
                out.append(f"{path}: unexpected field '{key}'")
            elif isinstance(addl, dict):
                self._walk(addl, item, f"{path}.{key}", out)

    def _sub_errors(self, schema: dict[str, Any], value: Any) -> list[str]:
        out: list[str] = []
        self._walk(schema, value, "$", out)
        return out


def load_schema(name: str) -> dict[str, Any]:
    """按文件名或裸名加载契约 schema。"""
    filename = name if name.endswith(".json") else f"{name}.schema.json"
    path = _SCHEMA_DIR / filename
    if not path.is_file():
        raise ContractViolation(f"schema file not found: {filename}")
    with path.open("r", encoding="utf-8") as fh:
        return json.load(fh)


def validator_for(name: str) -> SchemaValidator:
    """取得某个契约对象的校验器。"""
    return SchemaValidator(load_schema(name))


def validate_all_present() -> list[str]:
    """确认 8 份 schema 文件都在，返回缺失清单（供测试与启动自检使用）。"""
    return [f for f in SCHEMA_FILES if not (_SCHEMA_DIR / f).is_file()]


def schema_enum(schema: dict[str, Any], field_path: str) -> Optional[list[str]]:
    """从 schema 中取出某个字段的 enum 列表（``field_path`` 形如 ``type`` 或 ``a.b``）。"""
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
