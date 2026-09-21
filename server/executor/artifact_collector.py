# Copyright 2026 SciLoop contributors
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
"""工件回收（WP11-T1）。

每次 Run 在 ``<artifact_root>/run_{run_id}/output/`` 下落盘：

- ``result.json``：模板结构化产出（指标 + 逐样本记录）
- ``raw_output.json``：原始模型输出（replay 的数据源，逐样本一行）
- ``metrics.json``：指标明细
- ``manifest.json``：工件清单（文件名 / 字节数 / sha256）

安全约束：所有路径必须落在 ``artifact_root`` 之内（拒绝 ``..`` 与绝对路径逃逸）；
累计写入字节不得超过 ``EXECUTOR_MAX_OUTPUT_BYTES``。
"""

from __future__ import annotations

import hashlib
import json
import logging
import shutil
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from executor.errors import OutputLimitExceededError
from executor.limits import get_limits

logger = logging.getLogger("sciloop.executor.artifacts")


def sha256_hex(payload: bytes | str) -> str:
    """SHA-256 十六进制摘要（字符串按 UTF-8 编码）。"""
    data = payload.encode("utf-8") if isinstance(payload, str) else payload
    return hashlib.sha256(data).hexdigest()


def canonical_json(payload: Any) -> str:
    """规范化 JSON：键排序、禁止 NaN、紧凑分隔符（哈希可复现的前提）。"""
    return json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False)


@dataclass
class ArtifactEntry:
    """单个工件条目。"""

    name: str
    path: str
    bytes: int
    sha256: str
    truncated: bool = False

    def to_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "path": self.path,
            "bytes": self.bytes,
            "sha256": self.sha256,
            "truncated": self.truncated,
        }


@dataclass
class ArtifactCollector:
    """受限额约束的工件写入器（单次 Run 一个实例）。"""

    run_id: int
    root: str | None = None
    max_bytes: int | None = None
    experiment_id: int | None = None
    entries: list[ArtifactEntry] = field(default_factory=list)
    total_bytes: int = 0
    notes: list[str] = field(default_factory=list)

    # ------------------------------------------------------------------ #
    @property
    def root_path(self) -> Path:
        limits = get_limits()
        return Path(self.root or limits.artifact_root).resolve()

    @property
    def run_dir(self) -> Path:
        return self.root_path / f"run_{int(self.run_id)}"

    @property
    def output_dir(self) -> Path:
        return self.run_dir / "output"

    @property
    def limit_bytes(self) -> int:
        return int(self.max_bytes if self.max_bytes is not None else get_limits().max_output_bytes)

    @property
    def relative_dir(self) -> str:
        """入库用的相对路径（跨机器可移植，见验收 A7）。"""
        return str(Path(f"run_{int(self.run_id)}") / "output")

    def prepare(self) -> Path:
        """创建输出目录（幂等；清理同目录残留，避免旧工件混入本次 manifest）。"""
        if self.run_dir.exists():
            shutil.rmtree(self.run_dir, ignore_errors=True)
        self.output_dir.mkdir(parents=True, exist_ok=True)
        return self.output_dir

    def _resolve(self, name: str) -> Path:
        """把文件名解析到输出目录内（拒绝路径逃逸）。"""
        safe = str(name or "").strip().replace("\\", "/")
        if not safe or safe.startswith("/") or ".." in safe.split("/"):
            raise OutputLimitExceededError(
                f"非法工件名（禁止绝对路径与目录穿越）：{name!r}",
                detail={"name": name, "output_dir": str(self.output_dir)},
            )
        target = (self.output_dir / safe).resolve()
        if not str(target).startswith(str(self.output_dir.resolve())):
            raise OutputLimitExceededError(
                f"工件路径逃逸出输出目录：{name!r}",
                detail={"name": name, "output_dir": str(self.output_dir)},
            )
        return target

    def write_text(self, name: str, text: str) -> ArtifactEntry:
        """写入文本工件并累计字节配额（超限即抛错，不做静默截断）。"""
        data = text.encode("utf-8")
        entry = self.write_bytes(name, data)
        return entry

    def write_json(self, name: str, payload: Any) -> ArtifactEntry:
        """写入规范化 JSON 工件（键排序，哈希稳定）。"""
        return self.write_text(name, canonical_json(payload))

    def write_bytes(self, name: str, data: bytes) -> ArtifactEntry:
        target = self._resolve(name)
        self.output_dir.mkdir(parents=True, exist_ok=True)
        projected = self.total_bytes + len(data)
        if projected > self.limit_bytes:
            raise OutputLimitExceededError(
                f"输出超过执行器上限（{projected} > {self.limit_bytes} bytes，"
                f"本次写入 {name}）",
                detail={
                    "limit_bytes": self.limit_bytes,
                    "projected_bytes": projected,
                    "artifact": name,
                },
            )
        target.write_bytes(data)
        entry = ArtifactEntry(
            name=str(name),
            path=str(target),
            bytes=len(data),
            sha256=sha256_hex(data),
        )
        self.entries.append(entry)
        self.total_bytes = projected
        return entry

    def manifest(self, *, extra: dict[str, Any] | None = None) -> dict[str, Any]:
        """工件清单（写入 Passport 的 ``artifact_manifest``）。"""
        payload: dict[str, Any] = {
            "run_id": int(self.run_id),
            "experiment_id": self.experiment_id,
            "root": str(self.root_path),
            "relative_dir": self.relative_dir,
            "files": [entry.to_dict() for entry in self.entries],
            "total_bytes": self.total_bytes,
            "max_output_bytes": self.limit_bytes,
            "notes": list(self.notes),
        }
        if extra:
            payload.update(extra)
        return payload


__all__ = ["ArtifactCollector", "ArtifactEntry", "canonical_json", "sha256_hex"]
