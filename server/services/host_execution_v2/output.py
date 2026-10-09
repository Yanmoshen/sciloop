# Copyright 2026 SciLoop contributors
# Licensed under the Apache License, Version 2.0 (the "License")
"""增量输出收集（Agent 2 / WP-05）。

文档要求「stdout/stderr 增量生成事件，**按字节和行数限制**，超过上限产生
``truncated=true``，不能无限缓存」。因此这里同时守两条线：

- 字节上限（默认 64KB/通道）；
- 行数上限（默认 2000 行/通道）。

超限后**继续计数但不再缓存**，并标记 ``truncated``；每个通道给出
``bytes`` / ``lines`` / ``dropped_bytes`` 计数，便于审计与测试断言。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

#: 默认上限
DEFAULT_MAX_OUTPUT_BYTES = 64 * 1024
DEFAULT_MAX_OUTPUT_LINES = 2000


@dataclass
class ChannelBuffer:
    """单通道（stdout 或 stderr）的增量缓冲。"""

    name: str
    max_bytes: int
    max_lines: int
    text: str = ""
    bytes_seen: int = 0
    lines_seen: int = 0
    dropped_bytes: int = 0
    chunks: int = 0
    truncated: bool = False

    def append(self, chunk: str) -> str:
        """追加一个增量块，返回**实际记录**的文本（可能为空）。"""
        self.chunks += 1
        raw = chunk.encode("utf-8", errors="replace")
        self.bytes_seen += len(raw)
        self.lines_seen += chunk.count("\n")

        if self.truncated:
            self.dropped_bytes += len(raw)
            return ""

        kept = chunk
        if self.lines_seen > self.max_lines:
            # 行数超限：只保留尚在额度内的整行
            remaining_lines = max(0, self.max_lines - (self.lines_seen - chunk.count("\n")))
            parts = chunk.splitlines(keepends=True)
            kept = "".join(parts[:remaining_lines])
            self.truncated = True
        if len(self.text.encode("utf-8")) + len(kept.encode("utf-8")) > self.max_bytes:
            room = max(0, self.max_bytes - len(self.text.encode("utf-8")))
            encoded = kept.encode("utf-8")[:room]
            kept = encoded.decode("utf-8", errors="ignore")
            self.truncated = True
        if self.truncated:
            self.dropped_bytes += len(raw) - len(kept.encode("utf-8"))
        self.text += kept
        return kept

    def to_dict(self) -> dict[str, Any]:
        return {
            "channel": self.name,
            "bytes_seen": self.bytes_seen,
            "lines_seen": self.lines_seen,
            "kept_bytes": len(self.text.encode("utf-8")),
            "dropped_bytes": self.dropped_bytes,
            "chunks": self.chunks,
            "truncated": self.truncated,
        }


@dataclass
class OutputCollector:
    """stdout + stderr 的收集器。"""

    max_bytes: int = DEFAULT_MAX_OUTPUT_BYTES
    max_lines: int = DEFAULT_MAX_OUTPUT_LINES
    channels: dict[str, ChannelBuffer] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if not self.channels:
            self.channels = {
                "stdout": ChannelBuffer("stdout", self.max_bytes, self.max_lines),
                "stderr": ChannelBuffer("stderr", self.max_bytes, self.max_lines),
            }

    def append(self, channel: str, chunk: str) -> str:
        buffer = self.channels.get(channel)
        if buffer is None:  # pragma: no cover - 只会有两个通道
            return ""
        return buffer.append(chunk)

    @property
    def truncated(self) -> bool:
        return any(item.truncated for item in self.channels.values())

    @property
    def stdout(self) -> str:
        return self.channels["stdout"].text

    @property
    def stderr(self) -> str:
        return self.channels["stderr"].text

    @property
    def chunks(self) -> int:
        return sum(item.chunks for item in self.channels.values())

    def output_events(self) -> list[dict[str, Any]]:
        """给事件层的增量摘要（每通道一条结束统计）。"""
        return [item.to_dict() for item in self.channels.values()]

    def to_dict(self) -> dict[str, Any]:
        return {
            "stdout": self.stdout,
            "stderr": self.stderr,
            "truncated": self.truncated,
            "channels": self.output_events(),
            "chunks": self.chunks,
        }


__all__ = [
    "ChannelBuffer",
    "OutputCollector",
    "DEFAULT_MAX_OUTPUT_BYTES",
    "DEFAULT_MAX_OUTPUT_LINES",
]
