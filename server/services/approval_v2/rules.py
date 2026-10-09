# Copyright 2026 SciLoop contributors
# Licensed under the Apache License, Version 2.0 (the "License")
"""命令风险规则与信任前缀（Agent 2 / WP-03）。

文档要求风险规则**至少覆盖**：删除、覆盖、系统设置、安装、下载后执行、
数据库破坏性操作、workspace 外写入、凭据访问、未知命令。

判定输入是 **argv 列表**（不是拼好的 shell 字符串），因此规则匹配的是结构化 token，
不会被引号/转义绕过。判定发生在**执行器启动前**。
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path
from typing import Any

from services.sandbox_v2 import (
    HOST_PROGRAMS,
    WRITE_COMMANDS,
    is_write_command,
    looks_like_path,
    normalize,
    redirect_targets,
)


class RiskLevel(StrEnum):
    """风险等级。"""

    READ_ONLY = "read_only"
    NORMAL = "normal"
    HIGH_RISK = "high_risk"


class RiskCategory(StrEnum):
    """高危/需关注类别（文档 WP-03 的清单）。"""

    DELETE = "delete"
    OVERWRITE = "overwrite"
    SYSTEM_SETTINGS = "system_settings"
    INSTALL = "install"
    DOWNLOAD_AND_EXECUTE = "download_and_execute"
    DESTRUCTIVE_DATABASE = "destructive_database"
    OUTSIDE_WORKSPACE_WRITE = "outside_workspace_write"
    CREDENTIAL_ACCESS = "credential_access"
    UNKNOWN_COMMAND = "unknown_command"


#: 删除类命令
_DELETE_COMMANDS: frozenset[str] = frozenset(
    {"rm", "rmdir", "del", "erase", "rd", "remove-item", "ri", "unlink", "shred"}
)
#: 覆盖类命令
_OVERWRITE_COMMANDS: frozenset[str] = frozenset(
    {"mv", "move", "move-item", "cp", "copy", "xcopy", "robocopy", "copy-item", "truncate", "tee"}
)
#: 系统设置类命令
_SYSTEM_COMMANDS: frozenset[str] = frozenset(
    {
        "reg", "regedit", "setx", "sc", "netsh", "icacls", "takeown", "attrib", "bcdedit",
        "wmic", "chmod", "chown", "chgrp", "systemctl", "launchctl", "defaults",
        "set-executionpolicy", "new-localuser", "add-localuser", "shutdown", "reboot",
    }
)
#: 安装类命令
_INSTALL_COMMANDS: frozenset[str] = frozenset(
    {
        "pip", "pip3", "npm", "pnpm", "yarn", "apt", "apt-get", "apk", "yum", "dnf", "brew",
        "choco", "winget", "scoop", "gem", "cargo", "go", "dotnet", "conda", "uv",
    }
)
_INSTALL_SUBCOMMANDS: frozenset[str] = frozenset(
    {"install", "add", "i", "upgrade", "update", "remove", "uninstall"}
)
#: 下载类命令（与执行组合时才是"下载后执行"）
_DOWNLOAD_COMMANDS: frozenset[str] = frozenset(
    {"curl", "wget", "invoke-webrequest", "iwr", "invoke-restmethod", "irm", "fetch"}
)
#: 会执行外部东西的命令
_EXECUTORS: frozenset[str] = frozenset(
    {
        "sh", "bash", "zsh", "python", "python3", "node", "powershell", "pwsh", "cmd",
        "start-process", "iex", "invoke-expression",
    }
)
#: 破坏性 SQL / 数据库命令
_DB_PATTERNS: tuple[tuple[re.Pattern[str], str], ...] = (
    (re.compile(r"\bdrop\s+(database|schema|table)\b", re.I), "drop 语句"),
    (re.compile(r"\btruncate\s+table\b", re.I), "truncate 语句"),
    (re.compile(r"\bdelete\s+from\s+\w+\s*;?\s*$", re.I), "无 where 的 delete"),
    (re.compile(r"\bflushall\b", re.I), "Redis FLUSHALL"),
    (re.compile(r"\bdb\.dropdatabase\s*\(", re.I), "Mongo dropDatabase"),
)

#: 明确允许的前缀（管理员规则；命中即降级，但仍留审计）
DEFAULT_ALLOW_PREFIXES: tuple[tuple[str, ...], ...] = (
    ("git", "status"),
    ("git", "diff"),
    ("git", "log"),
    ("git", "branch"),
    ("ls",),
    ("dir",),
    ("cat",),
    ("type",),
    ("head",),
    ("tail",),
    ("echo",),
    ("pwd",),
    ("whoami",),
    ("where",),
    ("which",),
    ("python", "--version"),
    ("node", "--version"),
)

#: 已知宿主程序（不是"未知命令"）
KNOWN_HOST_PROGRAMS: frozenset[str] = frozenset(
    set(HOST_PROGRAMS) | set(WRITE_COMMANDS) | {"pytest", "uv", "rg", "grep", "find", "sed", "awk"}
)

#: 只读性质的可执行文件（命中允许前缀时用于降级展示）
_READONLY_HINTS: frozenset[str] = frozenset(
    {"ls", "dir", "cat", "type", "head", "tail", "echo", "pwd", "whoami", "git", "where", "which"}
)


@dataclass(frozen=True)
class CommandAnalysis:
    """一条命令的风险分析结果。"""

    level: RiskLevel
    categories: tuple[RiskCategory, ...]
    reasons: tuple[str, ...]
    executable: str
    normalized_argv: tuple[str, ...]
    cwd: str
    targets: tuple[str, ...] = ()
    allowlisted: bool = False
    unknown_command: bool = False
    prefix: tuple[str, ...] = ()

    @property
    def requires_approval(self) -> bool:
        return self.level is RiskLevel.HIGH_RISK

    def to_dict(self) -> dict[str, Any]:
        return {
            "level": self.level.value,
            "categories": [item.value for item in self.categories],
            "reasons": list(self.reasons),
            "executable": self.executable,
            "normalized_argv": list(self.normalized_argv),
            "cwd": self.cwd,
            "targets": list(self.targets),
            "allowlisted": self.allowlisted,
            "unknown_command": self.unknown_command,
            "prefix": list(self.prefix),
        }


def basename(program: str) -> str:
    return Path(program).name.lower()


class RiskPolicy:
    """风险规则集（可注入自定义允许前缀与额外高危命令）。"""

    def __init__(
        self,
        *,
        allow_prefixes: tuple[tuple[str, ...], ...] = DEFAULT_ALLOW_PREFIXES,
        extra_high_risk: tuple[str, ...] = (),
        known_programs: frozenset[str] = KNOWN_HOST_PROGRAMS,
        permissive_unknown: bool = False,
    ) -> None:
        self.allow_prefixes = tuple(
            tuple(item.lower() for item in prefix) for prefix in allow_prefixes
        )
        self.extra_high_risk = frozenset(item.lower() for item in extra_high_risk)
        self.known_programs = frozenset(item.lower() for item in known_programs)
        self.permissive_unknown = bool(permissive_unknown)

    # ------------------------------------------------------------------ 允许前缀
    def is_allowlisted(self, argv: list[str] | tuple[str, ...]) -> bool:
        """整条命令是否命中允许前缀（逐 token 前缀匹配）。"""
        if not argv:
            return False
        tokens = [str(item).lower() for item in argv]
        tokens[0] = basename(tokens[0])
        for prefix in self.allow_prefixes:
            if len(tokens) >= len(prefix) and tuple(tokens[: len(prefix)]) == prefix:
                return True
        return False

    def is_known_command(self, program: str, *, cwd: str | Path | None = None) -> bool:
        """是否为"已知命令"：常见宿主程序、写命令、或工作区内的脚本。"""
        name = basename(program)
        if name in self.known_programs or name in WRITE_COMMANDS or name in HOST_PROGRAMS:
            return True
        if looks_like_path(program):
            resolved = normalize(program, base=cwd)
            base = normalize(cwd) if cwd is not None else Path.cwd()
            try:
                resolved.relative_to(base)
            except ValueError:
                return False
            return resolved.exists() or True  # 工作区内的脚本/可执行文件视为已知
        return False

    # ------------------------------------------------------------------ 分析
    def analyze(
        self, argv: list[str] | tuple[str, ...], *, cwd: str | Path | None = None
    ) -> CommandAnalysis:
        tokens = [str(item) for item in argv]
        work_dir = normalize(cwd if cwd is not None else Path.cwd())
        if not tokens:
            return CommandAnalysis(
                level=RiskLevel.NORMAL,
                categories=(),
                reasons=("命令为空",),
                executable="",
                normalized_argv=(),
                cwd=str(work_dir),
            )

        executable = basename(tokens[0])
        arguments = tokens[1:]
        joined = " ".join(arguments)
        lowered_args = [item.lower() for item in arguments]

        categories: list[RiskCategory] = []
        reasons: list[str] = []

        def flag(category: RiskCategory, reason: str) -> None:
            if category not in categories:
                categories.append(category)
            if reason not in reasons:
                reasons.append(reason)

        if executable in self.extra_high_risk:
            flag(RiskCategory.SYSTEM_SETTINGS, f"{executable} 属于自定义高危命令")
        if executable in _DELETE_COMMANDS:
            flag(RiskCategory.DELETE, f"{executable} 会删除文件")
        if executable in _OVERWRITE_COMMANDS:
            flag(RiskCategory.OVERWRITE, f"{executable} 会移动或覆盖文件")
        if executable in _SYSTEM_COMMANDS:
            flag(RiskCategory.SYSTEM_SETTINGS, f"{executable} 会修改系统设置")
        if executable in _INSTALL_COMMANDS and any(
            item in _INSTALL_SUBCOMMANDS for item in lowered_args
        ):
            flag(RiskCategory.INSTALL, f"{executable} 会安装或卸载软件")

        downloads = executable in _DOWNLOAD_COMMANDS
        piping = any(token in {"|", "&&", ";", "||"} for token in arguments)
        runs_download = any(item in _EXECUTORS for item in lowered_args)
        if downloads and (piping or runs_download):
            flag(RiskCategory.DOWNLOAD_AND_EXECUTE, "下载内容后直接执行")

        for pattern, label in _DB_PATTERNS:
            if pattern.search(joined):
                flag(RiskCategory.DESTRUCTIVE_DATABASE, f"破坏性数据库操作（{label}）")
                break

        # 重定向覆盖既有文件 → 覆盖风险
        for target in redirect_targets(tokens):
            resolved = normalize(target, base=work_dir)
            if Path(resolved).exists():
                flag(RiskCategory.OVERWRITE, f"重定向会覆盖已存在的 {target}")
                break

        unknown = not self.is_known_command(tokens[0], cwd=work_dir)
        if unknown:
            flag(RiskCategory.UNKNOWN_COMMAND, f"{executable} 不在已知命令表内")

        allowlisted = self.is_allowlisted(tokens) and not categories
        normalized_argv = tuple(_normalize_tokens(tokens, work_dir))
        prefix = tuple(normalized_argv[:2])

        if allowlisted:
            level = RiskLevel.READ_ONLY if executable in _READONLY_HINTS else RiskLevel.NORMAL
            return CommandAnalysis(
                level=level,
                categories=(),
                reasons=(f"命中允许前缀（{' '.join(tokens[:2])}）",),
                executable=executable,
                normalized_argv=normalized_argv,
                cwd=str(work_dir),
                targets=tuple(redirect_targets(tokens)),
                allowlisted=True,
                unknown_command=False,
                prefix=prefix,
            )

        if unknown and self.permissive_unknown:
            # 集成方可以放宽（仍保留类别供审计）
            categories = [item for item in categories if item is not RiskCategory.UNKNOWN_COMMAND]
            reasons.append("未知命令按普通命令处理（permissive_unknown=true）")

        level = RiskLevel.HIGH_RISK if categories else RiskLevel.NORMAL
        if not reasons:
            reasons.append("普通命令（工作区内执行）")
        return CommandAnalysis(
            level=level,
            categories=tuple(categories),
            reasons=tuple(reasons),
            executable=executable,
            normalized_argv=normalized_argv,
            cwd=str(work_dir),
            targets=tuple(redirect_targets(tokens)),
            allowlisted=False,
            unknown_command=unknown,
            prefix=prefix,
        )


def _normalize_tokens(tokens: list[str], work_dir: Path) -> list[str]:
    """把命令 token 规范化：可执行文件取 basename，像路径的 token 解析成真实路径。

    这是「持续批准」能够安全匹配的基础——`rm ./a.txt` 与 `rm a.txt` 规范化后一致，
    而 `rm b.txt` 不会误匹配。
    """
    normalized: list[str] = []
    for index, token in enumerate(tokens):
        if index == 0:
            normalized.append(basename(token))
            continue
        normalized.append(str(normalize(token, base=work_dir)) if looks_like_path(token) else token)
    return normalized


def normalize_argv(tokens: list[str], *, cwd: str | Path) -> tuple[str, ...]:
    """对外暴露的规范化入口（供 approval manager 复用）。"""
    return tuple(_normalize_tokens([str(item) for item in tokens], normalize(cwd)))


__all__ = [
    "RiskLevel",
    "RiskCategory",
    "CommandAnalysis",
    "RiskPolicy",
    "DEFAULT_ALLOW_PREFIXES",
    "KNOWN_HOST_PROGRAMS",
    "basename",
    "normalize_argv",
    "is_write_command",
]
