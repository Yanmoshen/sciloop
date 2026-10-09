# Copyright 2026 SciLoop contributors
# Licensed under the Apache License, Version 2.0 (the "License");
"""固定 JSONL fixture 的加载。

fixture 用**行式 JSON**（每行一个独立对象）表达一个可复现场景，避免嵌套树带来的
解析歧义。支持的行类型：

.. code-block:: jsonc

    {"type": "meta", "thread_name": "多工具并行", "cwd": "/work", "model": "fake-1"}
    {"type": "model_response", "items": [{"kind": "text_delta", "text": "…"}, {"kind": "completed"}]}
    {"type": "model_delay", "seconds": 0.02}
    {"type": "tool_spec", "name": "read_file", "kind": "read_only", "description": "读取文件"}
    {"type": "tool_result", "name": "read_file", "status": "succeeded", "output": {"text": "…"}}
    {"type": "tool_output", "name": "read_file", "chunks": ["已读取 1/2", "已读取 2/2"]}
    {"type": "approval_required", "name": "run_command"}
    {"type": "tool_delay", "name": "read_file", "seconds": 0.05}

``model_response`` 的顺序即模型被调用顺序；用尽后重复最后一条（可构造多轮对话）。
``model_delay`` 给模型流的每个条目之间插入等待，用于制造**可中断的时间窗**。
"""

from __future__ import annotations

import json
from collections.abc import Iterable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from contracts.agent_v2.enums import StopReason, ToolCallStatus, ToolKind
from contracts.agent_v2.fake import FakeProvider
from contracts.agent_v2.models import (
    ReasoningDelta,
    StreamCompleted,
    StreamError,
    StreamItem,
    TextDelta,
    ToolCallCompleted,
    ToolCallDelta,
    ToolSpec,
    Usage,
)

FIXTURE_DIRNAME = "fixtures"


class FixtureError(ValueError):
    """fixture 文件格式错误（定位到行号，便于修复）。"""


@dataclass
class Scenario:
    """一个可复现的假运行场景。"""

    name: str
    thread_name: str = "会话"
    cwd: str | None = None
    model: str | None = None
    script: list[list[StreamItem]] = field(default_factory=list)
    tool_specs: list[ToolSpec] = field(default_factory=list)
    tool_results: dict[str, tuple[str, dict[str, Any]]] = field(default_factory=dict)
    tool_outputs: dict[str, list[str]] = field(default_factory=dict)
    approval_required: list[str] = field(default_factory=list)
    tool_delays: dict[str, float] = field(default_factory=dict)
    provider_delay_s: float = 0.0
    #: 运行时是否可用（``false`` 时启动 Turn 会得到 ``runtime_unavailable``）。
    runtime_available: bool = True
    source: str | None = None

    def provider(self) -> FakeProvider:
        """每次构造新的 provider，避免场景之间共享可变脚本。"""
        return FakeProvider(
            script=[list(batch) for batch in self.script], delay_s=self.provider_delay_s
        )

    def specs(self) -> list[ToolSpec]:
        if self.tool_specs:
            return list(self.tool_specs)
        return list(DEFAULT_TOOL_SPECS)

    def to_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "thread_name": self.thread_name,
            "cwd": self.cwd,
            "model": self.model,
            "model_calls": len(self.script),
            "tools": [spec.name for spec in self.specs()],
            "approval_required": list(self.approval_required),
            "provider_delay_s": self.provider_delay_s,
            "runtime_available": self.runtime_available,
            "source": self.source,
        }


#: 场景未声明工具时的默认工具集（只读两个 + 副作用三个，覆盖并行/串行两条路径）。
DEFAULT_TOOL_SPECS: tuple[ToolSpec, ...] = (
    ToolSpec(name="read_file", kind=ToolKind.READ_ONLY, description="读取文件"),
    ToolSpec(name="list_dir", kind=ToolKind.READ_ONLY, description="列出目录"),
    ToolSpec(name="write_file", kind=ToolKind.SIDE_EFFECT, description="写入文件"),
    ToolSpec(name="run_command", kind=ToolKind.SIDE_EFFECT, description="执行命令"),
    ToolSpec(name="update_plan", kind=ToolKind.SIDE_EFFECT, description="更新任务计划"),
)


def _stream_item(raw: Any, *, where: str) -> StreamItem:
    if not isinstance(raw, dict):
        raise FixtureError(f"{where}: stream item must be an object, got {type(raw).__name__}")
    kind = raw.get("kind")
    if kind == "text_delta":
        return TextDelta(text=str(raw.get("text", "")))
    if kind == "reasoning_delta":
        return ReasoningDelta(text=str(raw.get("text", "")))
    if kind == "tool_call_delta":
        return ToolCallDelta(
            call_id=str(raw.get("call_id", "")),
            name=raw.get("name"),
            arguments_delta=str(raw.get("arguments_delta", "")),
        )
    if kind == "tool_call_completed":
        return ToolCallCompleted(
            call_id=str(raw.get("call_id", "")),
            name=str(raw.get("name", "")),
            arguments=dict(raw.get("arguments") or {}),
        )
    if kind == "usage":
        return Usage(
            input_tokens=int(raw.get("input_tokens", 0)),
            output_tokens=int(raw.get("output_tokens", 0)),
            cached_tokens=int(raw.get("cached_tokens", 0)),
            cost_usd=raw.get("cost_usd"),
        )
    if kind == "completed":
        return StreamCompleted(stop_reason=str(raw.get("stop_reason") or StopReason.END_TURN))
    if kind == "error":
        return StreamError(
            error_class=str(raw.get("error_class") or "fatal"),
            message=str(raw.get("message", "")),
            retry_after_s=float(raw.get("retry_after_s") or 0.0),
        )
    raise FixtureError(f"{where}: unknown stream item kind {kind!r}")


def parse_lines(lines: Iterable[str], *, name: str, source: str | None = None) -> Scenario:
    """把 JSONL 行解析成一个 :class:`Scenario`。"""
    scenario = Scenario(name=name, source=source)
    for line_no, raw_line in enumerate(lines, start=1):
        text = raw_line.strip()
        if not text or text.startswith("#"):
            continue
        where = f"{source or name}:{line_no}"
        try:
            row = json.loads(text)
        except ValueError as exc:
            raise FixtureError(f"{where}: not valid JSON ({exc})") from exc
        if not isinstance(row, dict):
            raise FixtureError(f"{where}: each line must be a JSON object")
        row_type = row.get("type")
        if row_type == "meta":
            scenario.thread_name = str(row.get("thread_name") or scenario.thread_name)
            scenario.cwd = row.get("cwd", scenario.cwd)
            scenario.model = row.get("model", scenario.model)
        elif row_type == "model_response":
            items = row.get("items")
            if not isinstance(items, list):
                raise FixtureError(f"{where}: model_response.items must be an array")
            scenario.script.append([_stream_item(item, where=where) for item in items])
        elif row_type == "tool_spec":
            scenario.tool_specs.append(
                ToolSpec(
                    name=str(row.get("name", "")),
                    kind=str(row.get("kind") or ToolKind.SIDE_EFFECT),
                    description=str(row.get("description", "")),
                    timeout_s=row.get("timeout_s"),
                )
            )
        elif row_type == "tool_result":
            tool_name = str(row.get("name", ""))
            status = str(row.get("status") or ToolCallStatus.SUCCEEDED)
            if status not in {member.value for member in ToolCallStatus}:
                raise FixtureError(f"{where}: unknown tool status {status!r}")
            scenario.tool_results[tool_name] = (status, dict(row.get("output") or {}))
        elif row_type == "approval_required":
            scenario.approval_required.append(str(row.get("name", "")))
        elif row_type == "tool_delay":
            scenario.tool_delays[str(row.get("name", ""))] = float(row.get("seconds") or 0.0)
        elif row_type == "tool_output":
            chunks = row.get("chunks")
            if not isinstance(chunks, list):
                raise FixtureError(f"{where}: tool_output.chunks must be an array")
            scenario.tool_outputs[str(row.get("name", ""))] = [str(chunk) for chunk in chunks]
        elif row_type == "model_delay":
            scenario.provider_delay_s = float(row.get("seconds") or 0.0)
        elif row_type == "runtime":
            scenario.runtime_available = bool(row.get("available", True))
        else:
            raise FixtureError(f"{where}: unknown line type {row_type!r}")
    return scenario


def scenario_dir() -> Path:
    """内置 fixture 目录（与运行时代码同包，便于离线演示直接取用）。"""
    return Path(__file__).resolve().parent / FIXTURE_DIRNAME


def load_scenario(name: str, *, directory: Path | None = None) -> Scenario:
    """按名字加载 fixture（``name`` 可带或不带 ``.jsonl``）。"""
    base = directory or scenario_dir()
    filename = name if name.endswith(".jsonl") else f"{name}.jsonl"
    path = base / filename
    if not path.is_file():
        raise FixtureError(f"fixture not found: {path}")
    with path.open("r", encoding="utf-8") as fh:
        return parse_lines(fh, name=path.stem, source=str(path))


def available_scenarios(*, directory: Path | None = None) -> list[str]:
    """已安装的场景名（排序）。"""
    base = directory or scenario_dir()
    if not base.is_dir():
        return []
    return sorted(path.stem for path in base.glob("*.jsonl"))


__all__ = [
    "FixtureError",
    "Scenario",
    "DEFAULT_TOOL_SPECS",
    "FIXTURE_DIRNAME",
    "parse_lines",
    "scenario_dir",
    "load_scenario",
    "available_scenarios",
]
