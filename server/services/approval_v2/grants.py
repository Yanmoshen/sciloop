# Copyright 2026 SciLoop contributors
# Licensed under the Apache License, Version 2.0 (the "License")
"""持续批准规则（Agent 2 / WP-03）。

文档要求：``approve_for_thread`` 保存**规范化 executable、argv 前缀、参数限制、cwd 范围和
thread_id**，并且「不能因为命令名相同就放行任意参数」。

因此这里的匹配是四段式合取：

1. executable（basename，大小写归一）必须相等；
2. 规范化 argv 必须满足 **参数限制**——默认 ``exact``（全量相等）；
   只有显式要求 ``prefix`` 模式才允许额外的自由参数，且额外参数**不得是路径**
   （否则等于放开了"随便指向哪个文件"），并且数量不超过 ``max_extra_args``；
3. cwd 必须落在记录的范围（默认要求完全一致）；
4. 作用域必须是同一个 thread。
"""

from __future__ import annotations

import json
import threading
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from services.sandbox_v2 import looks_like_path, normalize, normalize_key, resolve_for_check

#: 作用域：只有"当前对话"一种
SCOPE_THREAD = "thread"

#: 参数限制模式
ARG_EXACT = "exact"
ARG_PREFIX = "prefix"
#: cwd 范围模式
CWD_EXACT = "exact"
CWD_WITHIN = "within"


@dataclass(frozen=True)
class ArgConstraints:
    """参数限制。``exact`` 是全量相等；``prefix`` 允许有限个非路径额外参数。"""

    mode: str = ARG_EXACT
    argv: tuple[str, ...] = ()
    max_extra_args: int = 0

    def allows(self, normalized_argv: tuple[str, ...]) -> bool:
        if self.mode == ARG_EXACT:
            return tuple(normalized_argv) == tuple(self.argv)
        if self.mode == ARG_PREFIX:
            head = self.prefix_tokens()
            if tuple(normalized_argv[: len(head)]) != head:
                return False
            extra = tuple(normalized_argv[len(head) :])
            if len(extra) > self.max_extra_args:
                return False
            # 额外参数里出现路径 → 可能指向别的文件，拒绝
            return not any(looks_like_path(token) for token in extra)
        return False

    def prefix_tokens(self) -> tuple[str, ...]:
        """前缀模式的基准 = **已批准的完整规范化 argv**。

        额外参数只允许出现在它之后，并且不得是路径（见 :meth:`allows`）——
        这样 `rm -rf <已批准目录>` 命中，而 `rm -rf <别的目录>` 不会。
        """
        return tuple(self.argv)

    def to_dict(self) -> dict[str, Any]:
        return {"mode": self.mode, "argv": list(self.argv), "max_extra_args": self.max_extra_args}

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> ArgConstraints:
        return cls(
            mode=str(data.get("mode") or ARG_EXACT),
            argv=tuple(str(item) for item in (data.get("argv") or ())),
            max_extra_args=int(data.get("max_extra_args") or 0),
        )

    @classmethod
    def exact(cls, normalized_argv: tuple[str, ...]) -> ArgConstraints:
        return cls(mode=ARG_EXACT, argv=tuple(normalized_argv))

    @classmethod
    def prefix(cls, prefix: tuple[str, ...], *, max_extra_args: int = 2) -> ArgConstraints:
        return cls(mode=ARG_PREFIX, argv=tuple(prefix), max_extra_args=int(max_extra_args))


@dataclass(frozen=True)
class CommandGrant:
    """一条持续批准记录。"""

    executable: str
    prefix: tuple[str, ...]
    arg_constraints: ArgConstraints
    cwd: str
    scope: str
    scope_id: str
    created_at: str
    approval_id: str | None = None
    tool: str = "host.command"
    cwd_mode: str = CWD_EXACT
    uses: int = 0

    # ------------------------------------------------------------------ 匹配
    def matches(
        self,
        *,
        executable: str,
        normalized_argv: tuple[str, ...],
        cwd: str | Path,
        scope_id: str,
    ) -> bool:
        if self.scope_id != scope_id:
            return False
        if self.executable != (executable or "").casefold():
            return False
        if not self.arg_constraints.allows(normalized_argv):
            return False
        return self._cwd_ok(cwd)

    def _cwd_ok(self, cwd: str | Path) -> bool:
        resolved = str(resolve_for_check(cwd)[0])
        if self.cwd_mode == CWD_WITHIN:
            recorded = normalize_key(resolve_for_check(self.cwd)[0])
            candidate = normalize_key(resolved)
            return candidate == recorded or candidate.startswith(recorded.rstrip("\\/") + "\\") or candidate.startswith(
                recorded.rstrip("/") + "/"
            )
        return normalize_key(resolved) == normalize_key(resolve_for_check(self.cwd)[0])

    # ------------------------------------------------------------------ 序列化
    def to_dict(self) -> dict[str, Any]:
        return {
            "executable": self.executable,
            "prefix": list(self.prefix),
            "arg_constraints": self.arg_constraints.to_dict(),
            "cwd": self.cwd,
            "cwd_mode": self.cwd_mode,
            "scope": self.scope,
            "scope_id": self.scope_id,
            "created_at": self.created_at,
            "approval_id": self.approval_id,
            "tool": self.tool,
            "uses": self.uses,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> CommandGrant:
        return cls(
            executable=str(data.get("executable") or ""),
            prefix=tuple(str(item) for item in (data.get("prefix") or ())),
            arg_constraints=ArgConstraints.from_dict(dict(data.get("arg_constraints") or {})),
            cwd=str(data.get("cwd") or ""),
            cwd_mode=str(data.get("cwd_mode") or CWD_EXACT),
            scope=str(data.get("scope") or SCOPE_THREAD),
            scope_id=str(data.get("scope_id") or ""),
            created_at=str(data.get("created_at") or ""),
            approval_id=data.get("approval_id"),
            tool=str(data.get("tool") or "host.command"),
            uses=int(data.get("uses") or 0),
        )

    def with_use(self) -> CommandGrant:
        return CommandGrant(
            executable=self.executable,
            prefix=self.prefix,
            arg_constraints=self.arg_constraints,
            cwd=self.cwd,
            cwd_mode=self.cwd_mode,
            scope=self.scope,
            scope_id=self.scope_id,
            created_at=self.created_at,
            approval_id=self.approval_id,
            tool=self.tool,
            uses=self.uses + 1,
        )


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

    # ------------------------------------------------------------------ 写
    def add(self, grant: CommandGrant) -> CommandGrant:
        with self._lock:
            for existing in self.grants:
                if self._same_rule(existing, grant):
                    return existing
            self.grants.append(grant)
            self._save()
            return grant

    def revoke(self, *, scope_id: str) -> int:
        with self._lock:
            before = len(self.grants)
            self.grants = [item for item in self.grants if item.scope_id != scope_id]
            if len(self.grants) != before:
                self._save()
            return before - len(self.grants)

    def clear(self) -> int:
        with self._lock:
            count = len(self.grants)
            self.grants.clear()
            self._save()
            return count

    # ------------------------------------------------------------------ 读
    def match(
        self,
        *,
        executable: str,
        normalized_argv: tuple[str, ...],
        cwd: str | Path,
        scope_id: str,
    ) -> CommandGrant | None:
        name = (executable or "").casefold()
        with self._lock:
            for grant in self.grants:
                if grant.matches(
                    executable=name, normalized_argv=normalized_argv, cwd=cwd, scope_id=scope_id
                ):
                    return grant
        return None

    def list(self, *, scope_id: str | None = None) -> list[CommandGrant]:
        with self._lock:
            if scope_id is None:
                return list(self.grants)
            return [item for item in self.grants if item.scope_id == scope_id]

    def prefixes(self, *, scope_id: str) -> list[str]:
        """给模型看的前缀摘要（**不泄漏审批 ID**）。"""
        return [" ".join(item.prefix) for item in self.list(scope_id=scope_id)]

    # ------------------------------------------------------------------ 持久化
    def _save(self) -> None:
        if self.path is None:
            return
        payload = {"grants": [item.to_dict() for item in self.grants]}
        temp = self.path.with_suffix(self.path.suffix + ".tmp")
        temp.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
        temp.replace(self.path)

    def _load(self) -> None:
        if self.path is None or not self.path.is_file():
            return
        try:
            raw = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):  # pragma: no cover
            return
        self.grants = [CommandGrant.from_dict(item) for item in raw.get("grants", [])]

    @staticmethod
    def signature(
        argv: list[str] | tuple[str, ...], *, cwd: str | Path
    ) -> tuple[str, tuple[str, ...], str]:
        """命令签名：``(可执行文件 basename, 规范化 argv, 规范化 cwd)``。"""
        from services.approval_v2.rules import basename, normalize_argv

        tokens = [str(item) for item in argv]
        return (
            basename(tokens[0]) if tokens else "",
            normalize_argv(tokens, cwd=cwd),
            str(normalize(cwd)),
        )

    @staticmethod
    def _same_rule(left: CommandGrant, right: CommandGrant) -> bool:
        return (
            left.executable == right.executable
            and left.arg_constraints == right.arg_constraints
            and normalize_key(left.cwd) == normalize_key(right.cwd)
            and left.scope == right.scope
            and left.scope_id == right.scope_id
        )


__all__ = [
    "ArgConstraints",
    "CommandGrant",
    "GrantStore",
    "SCOPE_THREAD",
    "ARG_EXACT",
    "ARG_PREFIX",
    "CWD_EXACT",
    "CWD_WITHIN",
]
