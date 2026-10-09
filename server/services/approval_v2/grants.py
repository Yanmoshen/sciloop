# Copyright 2026 SciLoop contributors
# Licensed under the Apache License, Version 2.0 (the "License")
"""持续批准规则（Agent 2 / WP-05）。

「当前对话始终批准当前命令」的安全性取决于**匹配有多严**。这里采取最保守的口径：

    grant = (可执行文件 basename, 规范化 argv 全量, cwd, 会话作用域)

四者**全部相等**才算命中。因此验收书 §3 的三条否定断言天然成立：

- 参数变化 → 规范化 argv 不同 → 不命中；
- 目录变化 → cwd 不同（同时相对路径解析结果也不同）→ 不命中；
- 可执行文件变化 → basename 不同 → 不命中。

刻意**不做**"只比前缀、参数随便变"的宽松匹配：那等于给模型一张万能通行证。
需要更宽的规则时应走 :mod:`services.approval_v2.risk` 的允许前缀（管理员显式配置）。
"""

from __future__ import annotations

import json
import threading
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from services.sandbox_v2 import normalize

#: 作用域：只有"当前对话"一种（计划书 §4.2）
SCOPE_THREAD = "thread"


@dataclass(frozen=True)
class CommandGrant:
    """一条持续批准记录。"""

    executable: str
    normalized_argv: tuple[str, ...]
    cwd: str
    scope: str
    scope_id: str
    created_at: str
    approval_id: str | None = None
    tool: str = "host.exec"
    uses: int = 0

    @property
    def prefix(self) -> tuple[str, ...]:
        """对外展示用的"命令前缀"（前两个 token，便于研究者核对）。"""
        return tuple(self.normalized_argv[:2])

    def to_dict(self) -> dict[str, Any]:
        return {
            "executable": self.executable,
            "normalized_argv": list(self.normalized_argv),
            "cwd": self.cwd,
            "scope": self.scope,
            "scope_id": self.scope_id,
            "created_at": self.created_at,
            "approval_id": self.approval_id,
            "tool": self.tool,
            "uses": self.uses,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> CommandGrant:
        known = set(cls.__dataclass_fields__)  # type: ignore[attr-defined]
        payload = {key: value for key, value in data.items() if key in known}
        payload["normalized_argv"] = tuple(payload.get("normalized_argv") or ())
        return cls(**payload)  # type: ignore[arg-type]


@dataclass
class GrantStore:
    """持续批准的存取与匹配（可选落盘）。"""

    path: Path | None = None
    grants: list[CommandGrant] = field(default_factory=list)
    _lock: threading.RLock = field(default_factory=threading.RLock, repr=False)

    def __post_init__(self) -> None:
        if self.path is not None:
            self.path = Path(self.path)
            self.path.parent.mkdir(parents=True, exist_ok=True)
            self._load()

    # ------------------------------------------------------------------ 持久化
    def _load(self) -> None:
        if self.path is None or not self.path.is_file():
            return
        try:
            raw = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):  # pragma: no cover - 坏文件跳过
            return
        self.grants = [CommandGrant.from_dict(item) for item in raw.get("grants", [])]

    def _save(self) -> None:
        if self.path is None:
            return
        payload = {"grants": [item.to_dict() for item in self.grants]}
        temp = self.path.with_suffix(self.path.suffix + ".tmp")
        temp.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
        temp.replace(self.path)

    # ------------------------------------------------------------------ 写
    def add(self, grant: CommandGrant) -> CommandGrant:
        with self._lock:
            for existing in self.grants:
                if self._same_rule(existing, grant):
                    # 幂等：同一条规则重复批准不新增记录
                    return existing
            self.grants.append(grant)
            self._save()
            return grant

    def revoke(self, *, scope_id: str) -> int:
        """撤销某个会话的全部持续批准（会话结束后调用）。"""
        with self._lock:
            before = len(self.grants)
            self.grants = [item for item in self.grants if item.scope_id != scope_id]
            if len(self.grants) != before:
                self._save()
            return before - len(self.grants)

    # ------------------------------------------------------------------ 读
    def match(
        self,
        *,
        argv: list[str] | tuple[str, ...],
        cwd: str | Path,
        scope_id: str,
        scope: str = SCOPE_THREAD,
    ) -> CommandGrant | None:
        """按「可执行文件 + 规范化 argv + cwd + 作用域」**全量相等**匹配。"""
        candidate = self.signature(argv, cwd=cwd)
        with self._lock:
            for grant in self.grants:
                if grant.scope != scope or grant.scope_id != scope_id:
                    continue
                if grant.executable != candidate[0]:
                    continue
                if grant.normalized_argv != candidate[1]:
                    continue
                if grant.cwd != candidate[2]:
                    continue
                return grant
        return None

    def list(self, *, scope_id: str | None = None) -> list[CommandGrant]:
        with self._lock:
            if scope_id is None:
                return list(self.grants)
            return [item for item in self.grants if item.scope_id == scope_id]

    # ------------------------------------------------------------------ 工具
    @staticmethod
    def signature(
        argv: list[str] | tuple[str, ...], *, cwd: str | Path
    ) -> tuple[str, tuple[str, ...], str]:
        """命令签名：``(可执行文件 basename, 规范化 argv, 规范化 cwd)``。"""
        from services.approval_v2.risk import _normalize_tokens  # 局部导入避免循环

        work_dir = normalize(cwd)
        tokens = [str(item) for item in argv]
        normalized = tuple(_normalize_tokens(tokens, work_dir)) if tokens else ()
        executable = Path(tokens[0]).name.lower() if tokens else ""
        return executable, normalized, str(work_dir)

    @staticmethod
    def _same_rule(left: CommandGrant, right: CommandGrant) -> bool:
        return (
            left.executable == right.executable
            and left.normalized_argv == right.normalized_argv
            and left.cwd == right.cwd
            and left.scope == right.scope
            and left.scope_id == right.scope_id
        )


__all__ = ["CommandGrant", "GrantStore", "SCOPE_THREAD"]
