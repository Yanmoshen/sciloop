# Copyright 2026 SciLoop contributors
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
"""MCP 工具的边界层：**授权来自 server 进程的启动配置，不来自调用方**。

为什么这样定
------------
stdio 传输下，调用方就是我们自己拉起的后端进程——如果让**调用方**在参数里声明"我允许自己
写盘"，那等于没有边界。所以授权（`Grant`）在进程启动时由配置给出，调用方只能"在已授予的
范围内请求"：越界即拒。

四道门，缺一不可
----------------
1. **能力**：工具声明的 `kinds` 必须都在授权里出现（read/write/exec/net）。
2. **路径**：先 `resolve()` 再比对授权根 —— 挡掉 `..` 穿越与软链逃逸；
   字符串前缀比较会被 `workspace/../../etc/passwd` 直接绕过。
3. **审批**：写盘/执行类工具必须带**研究者批准令牌**，令牌由授权里列出；没给就是
   `approval_required`，给了但不对就是 `approval_invalid`。这是合规 8.3「研究者接管/批准」的落点。
4. **审计**：每次调用（含被拒）落一条；**只记参数键名与长度，不记原文**（参数可能是论文正文或凭据）。

错误码故意做得很细，因为「没跑成」和「跑了但没通过」必须能分开 ——
把两者混为一谈，是安全类检查长期「假装通过」的同一种病。
"""

from __future__ import annotations

import os
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Literal

Capability = Literal["read", "write", "exec", "net"]

#: 写盘时禁止触碰的名字（凭据与版本库元数据）。放行等于把边界写在沙上。
FORBIDDEN_NAMES = (".env", ".git", ".ssh", ".aws", ".npmrc", ".pypirc",
                   "id_rsa", "id_ed25519", "credentials")
FORBIDDEN_SUFFIXES = (".pem", ".key", ".p12", ".pfx", ".keystore")


class GuardError(Exception):
    """带错误码的拒绝。MCP 侧会把 message 原样回给调用方，故带上可判读的前缀。"""

    def __init__(self, code: str, message: str, **detail: Any) -> None:
        super().__init__(f"[{code}] {message}")
        self.code = code
        self.message = message
        self.detail = detail


def _coerce(value: str | os.PathLike[str] | None) -> Path | None:
    if value in (None, ""):
        return None
    try:
        return Path(value).expanduser().resolve()
    except OSError:  # pragma: no cover
        return None


def grant_from_env(env: dict[str, str] | None = None) -> Grant:
    """从启动环境读授权。**这是唯一的授权来源**，调用方无法覆盖。

    约定（`SCILOOP_MCP_*`）：
      - `WORKSPACE`：唯一可写根；不给则完全不可写
      - `READ_ROOTS`：额外可读根（`os.pathsep` 分隔）
      - `ALLOW_EXEC=1`：允许执行白名单程序
      - `APPROVAL_TOKENS`：研究者批准令牌（逗号分隔），写/执行类工具必须命中其一
    """

    source = dict(os.environ if env is None else env)
    workspace = _coerce(source.get("SCILOOP_MCP_WORKSPACE"))
    extra = tuple(
        item for item in (_coerce(p) for p in source.get("SCILOOP_MCP_READ_ROOTS", "").split(os.pathsep))
        if item is not None
    )
    tokens = tuple(t.strip() for t in source.get("SCILOOP_MCP_APPROVAL_TOKENS", "").split(",") if t.strip())
    return Grant(
        actor=source.get("SCILOOP_MCP_ACTOR", "agent"),
        workspace=workspace,
        extra_read_roots=extra,
        allow_exec=source.get("SCILOOP_MCP_ALLOW_EXEC") == "1",
        approval_tokens=tokens,
    )


@dataclass(frozen=True)
class Grant:
    """进程级授权。空字段表示"该项能力不适用"，而不是"不限制"。"""

    actor: str = "agent"
    workspace: Path | None = None
    extra_read_roots: tuple[Path, ...] = ()
    allow_exec: bool = False
    allow_net: bool = False
    write_limit_bytes: int = 1 << 20
    approval_tokens: tuple[str, ...] = ()

    def read_roots(self) -> tuple[Path, ...]:
        roots = list(self.extra_read_roots)
        if self.workspace is not None:
            roots.append(self.workspace)
        return tuple(dict.fromkeys(roots))


@dataclass
class Guard:
    """执行四道门。工具实现只调它，不自己判权限。"""

    grant: Grant
    audit_sink: Callable[[dict[str, Any]], None] | None = None
    records: list[dict[str, Any]] = field(default_factory=list)

    # ---- 门 1：能力 ----
    def require(self, tool: str, kinds: tuple[Capability, ...]) -> None:
        for kind in kinds:
            if kind == "read":
                continue
            if kind == "write" and self.grant.workspace is None:
                raise GuardError("tool_denied", f"工具「{tool}」需要写盘权限，当前授权未授予")
            if kind == "exec" and not self.grant.allow_exec:
                raise GuardError("tool_denied", f"工具「{tool}」需要执行权限，当前授权未授予")
            if kind == "net" and not self.grant.allow_net:
                raise GuardError("tool_denied", f"工具「{tool}」需要出网权限，当前授权未授予")

    # ---- 门 2：路径 ----
    def resolve(self, tool: str, raw: str, *, for_write: bool) -> Path:
        if for_write and self.grant.workspace is None:
            raise GuardError("tool_denied", f"工具「{tool}」需要写盘权限，当前授权未授予")
        roots = (self.grant.workspace,) if for_write else self.grant.read_roots()
        roots = tuple(r for r in roots if r is not None)
        if not roots:
            raise GuardError("tool_denied", f"当前授权（{self.grant.actor}）没有任何可读根")

        candidate = Path(raw).expanduser()
        if not candidate.is_absolute():
            candidate = roots[0] / candidate
        resolved = candidate.resolve()

        for root in roots:
            if resolved == root or root in resolved.parents:
                if for_write:
                    self._assert_writable(tool, resolved)
                return resolved
        raise GuardError(
            "tool_denied",
            f"路径越出授权范围：{resolved} 不在 {[str(r) for r in roots]} 之内",
            path=str(resolved),
        )

    def _assert_writable(self, tool: str, path: Path) -> None:
        for part in path.parts:
            if part in FORBIDDEN_NAMES:
                raise GuardError("tool_denied", f"「{part}」属于凭据/版本库元数据，禁止由工具写入")
        if path.suffix.lower() in FORBIDDEN_SUFFIXES:
            raise GuardError("tool_denied", f"「{path.suffix}」密钥类文件禁止由工具写入")

    def assert_within_limit(self, tool: str, size: int) -> None:
        if size > self.grant.write_limit_bytes:
            raise GuardError(
                "tool_limit",
                f"内容 {size} 字节超过上限 {self.grant.write_limit_bytes} 字节，已拒绝（未产生任何写入）",
            )

    def assert_binary(self, tool: str, name: str, allowed: tuple[str, ...]) -> None:
        if name not in allowed:
            raise GuardError("tool_denied", f"「{name}」不在可执行白名单内；允许：{list(allowed)}")

    # ---- 门 3：审批 ----
    def require_approval(self, tool: str, token: str | None) -> None:
        """写盘/执行类工具必须带研究者批准令牌。

        **没给 ≠ 给错**：`approval_required` 表示"还需要人去批"，
        `approval_invalid` 表示"批了但不是有效凭据"。上层据此决定是去请人还是去报警。
        """

        if not self.grant.approval_tokens:
            raise GuardError(
                "approval_required",
                f"工具「{tool}」需要研究者批准，但当前授权未配置任何批准令牌；"
                "请由研究者在本机签发后再调用（合规 8.3：关键动作需人工接管）",
            )
        if not token:
            raise GuardError(
                "approval_required",
                f"工具「{tool}」需要研究者批准：请带上 approval_token（当前未提供）",
            )
        if token not in self.grant.approval_tokens:
            raise GuardError("approval_invalid", f"工具「{tool}」收到的批准令牌无效")

    # ---- 门 4：审计 ----
    def audit(self, *, tool: str, params: dict[str, Any], ok: bool, code: str | None, ms: float, **extra: Any) -> None:
        record = {
            "actor": self.grant.actor,
            "tool": tool,
            "param_keys": sorted(params),
            "param_bytes": sum(len(str(v)) for v in params.values()),
            "ok": ok,
            "code": code,
            "duration_ms": round(ms, 1),
            **extra,
        }
        self.records.append(record)
        if self.audit_sink is not None:
            self.audit_sink(record)
