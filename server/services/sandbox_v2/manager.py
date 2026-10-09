# Copyright 2026 SciLoop contributors
# Licensed under the Apache License, Version 2.0 (the "License")
"""SandboxManager：策略 × 路径 × 命令的事实层（Agent 2 / WP-02）。

对外四个命名入口（文档 WP-02 要求）：

    check_read(path)            -> AccessVerdict
    check_write(path)           -> AccessVerdict
    check_execute(path|cwd)     -> AccessVerdict
    check_command_paths(argv)   -> CommandVerdict（cwd + 每个参数路径 + 重定向 + 升级）

判定顺序固定，测试据此断言：

1. ``danger-full-access`` → 一律放行，但标 ``audited=True``（**只改根边界与批准默认值**）；
2. 命中敏感规则 → 写一律拒绝；标了 ``deny_read`` 的（凭据/密钥）读也拒绝，其余读要授权；
3. 工作区（或额外根）内 → 按策略放行；
4. 工作区外 → ``workspace-write``：读放行、写/执行要授权；``read-only``：写拒绝；
5. 命令级额外升级：内联代码含破坏/网络/进程特征时 → 要授权（启发式不是唯一安全边界）。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from . import linux as linux_probe
from . import windows as windows_probe
from .models import (
    SEVERITY,
    AccessVerdict,
    CommandVerdict,
    PlatformCapability,
    ProtectedRule,
)
from .policy import (
    DEFAULT_POLICY,
    TEST_POLICY,
    AccessKind,
    SandboxDecision,
    SandboxPolicy,
)
from .resolver import (
    DEFAULT_PROTECTED_RULES,
    extract_argv_paths,
    inline_code_suspicion,
    is_write_command,
    match_protected_rule,
    matched_root,
    normalize,
    redirect_targets,
    resolve_for_check,
)

#: 运行宿主机程序的常见解释器（argv[0] 不做边界判定）
HOST_PROGRAMS: frozenset[str] = frozenset(
    {
        "python", "python3", "py", "node", "npm", "npx", "pnpm", "yarn",
        "powershell", "powershell.exe", "pwsh", "pwsh.exe", "cmd", "cmd.exe",
        "bash", "sh", "git", "uv", "uvicorn", "pytest", "python.exe", "node.exe",
    }
)


@dataclass
class SandboxFacts:
    """可以安全交给模型的事实摘要（不含 token / 内部路径 / ACL 细节）。"""

    sandbox_mode: str
    workspace_root: str
    outside_readable: bool
    writable_roots: list[str] = field(default_factory=list)
    read_only_roots: list[str] = field(default_factory=list)
    protected_categories: list[str] = field(default_factory=list)
    capability: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "sandbox_mode": self.sandbox_mode,
            "workspace_root": self.workspace_root,
            "outside_readable": self.outside_readable,
            "writable_roots": list(self.writable_roots),
            "read_only_roots": list(self.read_only_roots),
            "protected_categories": list(self.protected_categories),
            "capability": dict(self.capability),
        }


class SandboxManager:
    """策略持有者与事实计算者。"""

    def __init__(
        self,
        workspace_root: str | Path,
        *,
        policy: SandboxPolicy | str = DEFAULT_POLICY,
        extra_read_roots: tuple[str | Path, ...] = (),
        extra_write_roots: tuple[str | Path, ...] = (),
        protected_rules: tuple[ProtectedRule, ...] = DEFAULT_PROTECTED_RULES,
        escalate_inline_code: bool = True,
    ) -> None:
        self.workspace_root = normalize(workspace_root)
        self.policy = SandboxPolicy(policy)
        self.extra_read_roots = tuple(normalize(item) for item in extra_read_roots)
        self.extra_write_roots = tuple(normalize(item) for item in extra_write_roots)
        self.protected_rules = tuple(protected_rules)
        self.escalate_inline_code = bool(escalate_inline_code)

    # ------------------------------------------------------------------ 策略
    def set_policy(self, policy: SandboxPolicy | str) -> SandboxPolicy:
        """切换策略。**只有用户显式操作才允许升级到 danger-full-access**（见 approval_v2）。"""
        self.policy = SandboxPolicy(policy)
        return self.policy

    @property
    def full_access(self) -> bool:
        return self.policy is SandboxPolicy.DANGER_FULL_ACCESS

    def capability(self) -> PlatformCapability:
        """平台隔离能力探测（如实返回，不伪造）。"""
        return windows_probe.probe() if _is_windows() else linux_probe.probe()

    def writable_roots(self) -> list[Path]:
        return [self.workspace_root, *self.extra_write_roots]

    def facts(self, *, approved_prefixes: list[str] | None = None) -> SandboxFacts:
        """沙箱事实（供 prompt_facts 渲染给模型）。"""
        return SandboxFacts(
            sandbox_mode=self.policy.value,
            workspace_root=str(self.workspace_root),
            outside_readable=self.policy is not SandboxPolicy.READ_ONLY,
            writable_roots=[str(item) for item in self.writable_roots()],
            read_only_roots=[str(item) for item in self.extra_read_roots],
            protected_categories=_protected_categories(self.protected_rules),
            capability=self.capability().to_dict(),
        )

    def summary(self) -> dict[str, Any]:
        """写进 ``Thread.permission_summary`` 的摘要。"""
        return {
            "policy": self.policy.value,
            "workspace_root": str(self.workspace_root),
            "extra_read_roots": [str(item) for item in self.extra_read_roots],
            "extra_write_roots": [str(item) for item in self.extra_write_roots],
            "capability": self.capability().to_dict(),
        }

    # ------------------------------------------------------------------ 命名入口
    def check_read(self, path: str | Path, *, cwd: str | Path | None = None) -> AccessVerdict:
        return self._check(path, AccessKind.READ, cwd=cwd)

    def check_write(self, path: str | Path, *, cwd: str | Path | None = None) -> AccessVerdict:
        return self._check(path, AccessKind.WRITE, cwd=cwd)

    def check_execute(
        self, path: str | Path | None = None, *, cwd: str | Path | None = None
    ) -> AccessVerdict:
        target = path if path is not None else (cwd if cwd is not None else self.workspace_root)
        return self._check(target, AccessKind.EXECUTE, cwd=cwd)

    def check_command_paths(
        self, argv: list[str] | tuple[str, ...], *, cwd: str | Path | None = None
    ) -> CommandVerdict:
        """检查 cwd + 参数里的每个路径 + 重定向目标 + 可疑内联代码。"""
        tokens = [str(item) for item in argv]
        work_dir = normalize(cwd if cwd is not None else self.workspace_root)
        entries: list[AccessVerdict] = [self.check_execute(work_dir)]

        arguments = tokens[1:] if tokens else []
        writes = {str(normalize(item, base=work_dir)) for item in redirect_targets(arguments)}
        command_writes = bool(tokens) and is_write_command(tokens[0])
        for _token, resolved in extract_argv_paths(arguments, cwd=work_dir):
            access = AccessKind.WRITE if (resolved in writes or command_writes) else AccessKind.READ
            entries.append(self._check(resolved, access, cwd=work_dir))

        if self.escalate_inline_code and not self.full_access:
            suspicion = inline_code_suspicion(tokens)
            if suspicion:
                entries.append(
                    AccessVerdict(
                        decision=SandboxDecision.REQUIRE_APPROVAL,
                        access=AccessKind.EXECUTE,
                        reason=suspicion,
                        path=str(work_dir),
                        resolved_paths=(str(work_dir),),
                        inside_workspace=True,
                        policy=self.policy.value,
                        escalation=suspicion,
                    )
                )

        return CommandVerdict(cwd=str(work_dir), verdict=_strictest(entries), entries=entries)

    # ------------------------------------------------------------------ 兼容入口
    def check_path(
        self,
        path: str | Path,
        access: AccessKind | str = AccessKind.READ,
        *,
        cwd: str | Path | None = None,
    ) -> AccessVerdict:
        """``check_read/write/execute`` 的通用形式（供集成方按 access 变量调用）。"""
        return self._check(path, AccessKind(access), cwd=cwd)

    def check_argv(
        self, argv: list[str] | tuple[str, ...], *, cwd: str | Path | None = None
    ) -> CommandVerdict:
        """``check_command_paths`` 的旧名（保持既有调用可用）。"""
        return self.check_command_paths(argv, cwd=cwd)

    def is_host_program(self, program: str) -> bool:
        return Path(program).name.lower() in HOST_PROGRAMS

    # ------------------------------------------------------------------ 内部
    def _check(
        self, path: str | Path, access: AccessKind, *, cwd: str | Path | None = None
    ) -> AccessVerdict:
        base = cwd if cwd is not None else self.workspace_root
        resolved, how = resolve_for_check(path, base=base)
        resolved_str = str(resolved)
        inside = matched_root(resolved, [self.workspace_root]) is not None
        root = matched_root(resolved, self.writable_roots())
        read_root = matched_root(resolved, list(self.extra_read_roots))
        rule = match_protected_rule(resolved, self.protected_rules)

        common = {
            "access": access,
            "path": resolved_str,
            "resolved_paths": (resolved_str,),
            "inside_workspace": inside,
            "policy": self.policy.value,
        }

        if self.full_access:
            return AccessVerdict(
                decision=SandboxDecision.ALLOW,
                reason="danger-full-access：任意目录放行（仍记账）",
                matched_root=str(root) if root else None,
                protected=rule is not None,
                protected_rule=rule.rule_id if rule else None,
                audited=True,
                **common,
            )

        if rule is not None:
            if access is AccessKind.WRITE:
                return AccessVerdict(
                    decision=SandboxDecision.DENY,
                    reason=f"{rule.reason}：写保护（规则 {rule.rule_id}）",
                    matched_root=str(root) if root else None,
                    protected=True,
                    protected_rule=rule.rule_id,
                    **common,
                )
            if rule.deny_read:
                return AccessVerdict(
                    decision=SandboxDecision.DENY,
                    reason=f"{rule.reason}：拒绝读取（规则 {rule.rule_id}）",
                    matched_root=str(root) if root else None,
                    protected=True,
                    protected_rule=rule.rule_id,
                    **common,
                )
            if access is AccessKind.READ:
                return AccessVerdict(
                    decision=SandboxDecision.ALLOW,
                    reason=f"{rule.reason}：可读但记录审计（规则 {rule.rule_id}）",
                    matched_root=str(root) if root else None,
                    protected=True,
                    protected_rule=rule.rule_id,
                    audited=True,
                    **common,
                )

        if root is not None:
            if access is AccessKind.WRITE and root != self.workspace_root:
                return AccessVerdict(
                    decision=SandboxDecision.REQUIRE_APPROVAL,
                    reason="额外可写根之外的目录需要授权",
                    matched_root=str(root),
                    **common,
                )
            if access is AccessKind.WRITE and self.policy is SandboxPolicy.READ_ONLY:
                return AccessVerdict(
                    decision=SandboxDecision.DENY,
                    reason="read-only 策略：禁止写入",
                    matched_root=str(root),
                    **common,
                )
            return AccessVerdict(
                decision=SandboxDecision.ALLOW,
                reason="工作区内可读写执行" if root == self.workspace_root else "已授权的额外根",
                matched_root=str(root),
                **common,
            )

        if read_root is not None:
            if access is AccessKind.WRITE:
                return AccessVerdict(
                    decision=(
                        SandboxDecision.DENY
                        if self.policy is SandboxPolicy.READ_ONLY
                        else SandboxDecision.REQUIRE_APPROVAL
                    ),
                    reason="只读根内的写入需要授权" if self.policy is not SandboxPolicy.READ_ONLY else "read-only 策略：禁止写入",
                    matched_root=str(read_root),
                    **common,
                )
            return AccessVerdict(
                decision=SandboxDecision.ALLOW,
                reason="已授权的只读根",
                matched_root=str(read_root),
                **common,
            )

        # 工作区外
        if access is AccessKind.READ:
            return AccessVerdict(
                decision=SandboxDecision.ALLOW,
                reason="工作区外只读可用",
                **common,
            )
        if self.policy is SandboxPolicy.READ_ONLY:
            return AccessVerdict(
                decision=SandboxDecision.DENY,
                reason="read-only 策略：禁止写入",
                **common,
            )
        del how
        return AccessVerdict(
            decision=SandboxDecision.REQUIRE_APPROVAL,
            reason="工作区外写入/执行需要授权",
            **common,
        )


def _strictest(entries: list[AccessVerdict]) -> AccessVerdict:
    best = entries[0]
    for item in entries[1:]:
        if SEVERITY[item.decision] > SEVERITY[best.decision]:
            best = item
    return best


def _protected_categories(rules: tuple[ProtectedRule, ...]) -> list[str]:
    """给模型看的**类别**（不泄漏具体文件名与内部路径）。"""
    categories = {
        "version_control_metadata": {"vcs."},
        "credentials": {"cred."},
        "private_keys": {"key."},
    }
    found: set[str] = set()
    for rule in rules:
        for category, prefixes in categories.items():
            if any(rule.rule_id.startswith(prefix) for prefix in prefixes):
                found.add(category)
    return sorted(found)


def _is_windows() -> bool:
    import os

    return os.name == "nt"


#: 默认测试用策略（供测试与集成方复用）
DEFAULT_TEST_POLICY = TEST_POLICY

__all__ = [
    "SandboxManager",
    "SandboxFacts",
    "HOST_PROGRAMS",
    "DEFAULT_TEST_POLICY",
]
