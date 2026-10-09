# Copyright 2026 SciLoop contributors
# Licensed under the Apache License, Version 2.0 (the "License")
"""SandboxManager：把策略应用到路径与命令参数上（Agent 2 / WP-05）。

判定顺序（固定，测试据此断言）：

1. ``danger-full-access`` → 一律放行，但标 ``audited=True``（完全访问仍要记账）；
2. 命中敏感目标（凭据 / 版本库元数据）→ 普通模式：读要授权、写直接拒绝；
3. 工作区内 → 读写执行放行；
4. 工作区外 → ``workspace-write``：读放行、写/执行要授权；``read-only``：写拒绝。

命令参数检查（:meth:`check_argv`）刻意与上面的"写"判定分开：

- ``argv[0]`` **不判边界**——宿主机程序（python / node / powershell）本来就在工作区外，
  否则任何命令都无法执行；
- 参数里的路径按**读**判定（``python x.py /tmp/data``），越界 → 要授权；
- shell 重定向目标（``> out.txt``）按**写**判定（``>>`` 追加也是写）；
- 返回最严的一条裁决，并保留全部明细供审计。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from .paths import (
    extract_argv_paths,
    is_protected,
    is_within,
    is_write_command,
    normalize,
    redirect_targets,
)
from .policy import (
    DEFAULT_POLICY,
    TEST_POLICY,
    AccessKind,
    AccessVerdict,
    SandboxDecision,
    SandboxPolicy,
)

#: 运行宿主机程序的常见解释器名（argv[0] 不做边界判定）
HOST_PROGRAMS: frozenset[str] = frozenset(
    {
        "python",
        "python3",
        "py",
        "node",
        "npm",
        "npx",
        "pnpm",
        "yarn",
        "powershell",
        "powershell.exe",
        "pwsh",
        "pwsh.exe",
        "cmd",
        "cmd.exe",
        "bash",
        "sh",
        "git",
        "uv",
        "uvicorn",
        "pytest",
    }
)


@dataclass
class WorkspaceCheck:
    """一次命令上下文检查的明细。"""

    cwd: str
    verdict: AccessVerdict
    paths: list[AccessVerdict] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "cwd": self.cwd,
            "verdict": self.verdict.to_dict(),
            "paths": [item.to_dict() for item in self.paths],
        }


class SandboxManager:
    """策略持有者与裁决入口。"""

    def __init__(
        self,
        workspace_root: str | Path,
        *,
        policy: SandboxPolicy | str = DEFAULT_POLICY,
        extra_read_roots: tuple[str | Path, ...] = (),
        extra_write_roots: tuple[str | Path, ...] = (),
    ) -> None:
        self.workspace_root = normalize(workspace_root)
        self.policy = SandboxPolicy(policy)
        self.extra_read_roots = tuple(normalize(item) for item in extra_read_roots)
        self.extra_write_roots = tuple(normalize(item) for item in extra_write_roots)

    # ------------------------------------------------------------------ 策略
    def set_policy(self, policy: SandboxPolicy | str) -> SandboxPolicy:
        self.policy = SandboxPolicy(policy)
        return self.policy

    @property
    def full_access(self) -> bool:
        return self.policy is SandboxPolicy.DANGER_FULL_ACCESS

    def summary(self) -> dict[str, Any]:
        """写进 ``Thread.permission_summary`` 的策略摘要。"""
        return {
            "policy": self.policy.value,
            "workspace_root": str(self.workspace_root),
            "extra_read_roots": [str(item) for item in self.extra_read_roots],
            "extra_write_roots": [str(item) for item in self.extra_write_roots],
        }

    # ------------------------------------------------------------------ 路径裁决
    def check_path(
        self,
        path: str | Path,
        access: AccessKind | str = AccessKind.READ,
        *,
        cwd: str | Path | None = None,
    ) -> AccessVerdict:
        """判定一次路径访问。"""
        kind = AccessKind(access)
        base = cwd if cwd is not None else self.workspace_root
        resolved = normalize(path, base=base)
        inside = is_within(resolved, self.workspace_root)
        protected = is_protected(resolved)

        if self.full_access:
            return AccessVerdict(
                decision=SandboxDecision.ALLOW,
                access=kind,
                reason="danger-full-access：任意目录放行（仍记账）",
                path=str(resolved),
                inside_workspace=inside,
                protected=protected,
                audited=True,
                policy=self.policy.value,
            )

        if protected:
            if kind is AccessKind.READ:
                return AccessVerdict(
                    decision=SandboxDecision.REQUIRE_APPROVAL,
                    access=kind,
                    reason="读取凭据/版本库元数据需要授权",
                    path=str(resolved),
                    inside_workspace=inside,
                    protected=True,
                    policy=self.policy.value,
                )
            return AccessVerdict(
                decision=SandboxDecision.DENY,
                access=kind,
                reason="凭据/版本库元数据写保护",
                path=str(resolved),
                inside_workspace=inside,
                protected=True,
                policy=self.policy.value,
            )

        if inside or any(is_within(resolved, root) for root in self.extra_read_roots):
            if kind is AccessKind.WRITE and not self._writable_root(resolved):
                return AccessVerdict(
                    decision=SandboxDecision.REQUIRE_APPROVAL,
                    access=kind,
                    reason="该只读根内的写入需要授权",
                    path=str(resolved),
                    inside_workspace=inside,
                    policy=self.policy.value,
                )
            if self.policy is SandboxPolicy.READ_ONLY and kind is AccessKind.WRITE:
                return AccessVerdict(
                    decision=SandboxDecision.DENY,
                    access=kind,
                    reason="read-only 策略：禁止写入",
                    path=str(resolved),
                    inside_workspace=inside,
                    policy=self.policy.value,
                )
            return AccessVerdict(
                decision=SandboxDecision.ALLOW,
                access=kind,
                reason="工作区内可读写执行" if inside else "已授权的只读根",
                path=str(resolved),
                inside_workspace=inside,
                policy=self.policy.value,
            )

        # 工作区外
        if kind is AccessKind.READ:
            return AccessVerdict(
                decision=SandboxDecision.ALLOW,
                access=kind,
                reason="工作区外只读可用",
                path=str(resolved),
                policy=self.policy.value,
            )
        if self.policy is SandboxPolicy.READ_ONLY:
            return AccessVerdict(
                decision=SandboxDecision.DENY,
                access=kind,
                reason="read-only 策略：禁止写入",
                path=str(resolved),
                policy=self.policy.value,
            )
        return AccessVerdict(
            decision=SandboxDecision.REQUIRE_APPROVAL,
            access=kind,
            reason="工作区外写入/执行需要授权",
            path=str(resolved),
            policy=self.policy.value,
        )

    def _writable_root(self, resolved: Path) -> bool:
        if is_within(resolved, self.workspace_root):
            return True
        return any(is_within(resolved, root) for root in self.extra_write_roots)

    # ------------------------------------------------------------------ 命令裁决
    def check_argv(
        self,
        argv: list[str] | tuple[str, ...],
        *,
        cwd: str | Path | None = None,
    ) -> WorkspaceCheck:
        """检查命令参数与 cwd。``argv[0]``（可执行文件）不判边界。

        参数路径的访问性质按**命令语义**判定：

        - 写命令（``mkdir`` / ``rm`` / ``cp`` / ``New-Item`` …）的参数是**写入目标**；
        - 重定向目标（``> out.txt``）是写入；
        - 其余参数按**读取**（``python x.py /etc/passwd`` 只是读）。
        """
        sequence = [str(item) for item in argv]
        work_dir = normalize(cwd if cwd is not None else self.workspace_root)
        verdicts: list[AccessVerdict] = [self.check_path(work_dir, AccessKind.EXECUTE)]

        arguments = sequence[1:] if sequence else []
        writes = {str(normalize(item, base=work_dir)) for item in redirect_targets(arguments)}
        command_writes = bool(sequence) and is_write_command(sequence[0])
        for _token, resolved in extract_argv_paths(arguments, cwd=work_dir):
            access = (
                AccessKind.WRITE
                if resolved in writes or command_writes
                else AccessKind.READ
            )
            verdicts.append(self.check_path(resolved, access))

        return WorkspaceCheck(cwd=str(work_dir), verdict=AccessVerdict.strictest(verdicts), paths=verdicts)

    def is_host_program(self, program: str) -> bool:
        """可执行文件是否是"常见的宿主机程序"（只做提示，不参与放行）。"""
        return Path(program).name.lower() in HOST_PROGRAMS


#: 默认测试用策略（供测试与集成方复用，避免各自写死字符串）
DEFAULT_TEST_POLICY = TEST_POLICY

__all__ = ["SandboxManager", "WorkspaceCheck", "HOST_PROGRAMS", "DEFAULT_TEST_POLICY"]
