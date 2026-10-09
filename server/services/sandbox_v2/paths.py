# Copyright 2026 SciLoop contributors
# Licensed under the Apache License, Version 2.0 (the "License")
"""路径规范化、根边界判定与「命令参数里的路径」识别（Agent 2 / WP-05）。

三条硬要求（验收书 §4）：

1. 所有路径**先规范化、解析符号链接**，再判根边界——软链不能绕过边界；
2. **命令参数里的路径也要检查**，不能只看 cwd；
3. 对敏感文件、凭据和版本库元数据提供策略边界。

本模块只做判断，不做裁决（裁决在 :mod:`services.sandbox_v2.manager`）。
"""

from __future__ import annotations

import os
import re
from pathlib import Path

#: 目录级敏感目标（版本库元数据与凭据目录）
PROTECTED_DIRS: frozenset[str] = frozenset(
    {".git", ".hg", ".svn", ".ssh", ".aws", ".gnupg", ".kube", ".azure", ".config/gcloud"}
)

#: 文件级敏感目标（凭据与环境变量）
PROTECTED_FILES: frozenset[str] = frozenset(
    {
        ".env",
        ".env.local",
        ".env.production",
        ".netrc",
        ".npmrc",
        ".pypirc",
        ".git-credentials",
        "id_rsa",
        "id_ed25519",
        "id_ecdsa",
        "credentials",
        "credentials.json",
        "secrets.json",
    }
)

#: 后缀级敏感目标（私钥 / 证书）
PROTECTED_SUFFIXES: frozenset[str] = frozenset({".pem", ".key", ".pfx", ".p12", ".keystore"})

#: 常见「像路径」的后缀——用于识别命令参数里的相对路径（例如 ``out.json``）
_PATHLIKE_SUFFIXES: frozenset[str] = frozenset(
    {
        ".txt",
        ".json",
        ".jsonl",
        ".md",
        ".py",
        ".js",
        ".ts",
        ".vue",
        ".csv",
        ".tsv",
        ".log",
        ".pdf",
        ".docx",
        ".xlsx",
        ".png",
        ".jpg",
        ".jpeg",
        ".gif",
        ".svg",
        ".sh",
        ".ps1",
        ".bat",
        ".cmd",
        ".exe",
        ".zip",
        ".tar",
        ".gz",
        ".db",
        ".sqlite",
        ".sqlite3",
        ".yaml",
        ".yml",
        ".toml",
        ".ini",
        ".cfg",
        ".conf",
    }
)

_DRIVE_RE = re.compile(r"^[A-Za-z]:[\\/]")
_REDIRECT_TOKENS = frozenset({">", ">>", "1>", "1>>", "2>", "2>>", "&>", "&>>", "<", "2<"})

#: 这些命令的**参数路径是写入目标**（不是只读）——否则 `mkdir /tmp/x` 会被当成"读 /tmp/x"。
WRITE_COMMANDS: frozenset[str] = frozenset(
    {
        "mkdir",
        "mkd",
        "md",
        "touch",
        "rm",
        "rmdir",
        "del",
        "erase",
        "rd",
        "unlink",
        "cp",
        "copy",
        "xcopy",
        "robocopy",
        "mv",
        "move",
        "tee",
        "chmod",
        "chown",
        "chgrp",
        "ln",
        "truncate",
        "dd",
        "install",
        "new-item",
        "set-content",
        "add-content",
        "out-file",
        "remove-item",
        "move-item",
        "copy-item",
        "rename-item",
        "clear-content",
        "expand-archive",
        "compress-archive",
    }
)


def is_write_command(program: str) -> bool:
    """这个可执行文件的参数路径是否应视为**写入目标**。"""
    return Path(program).name.lower() in WRITE_COMMANDS


def normalize(path: str | os.PathLike[str], *, base: str | os.PathLike[str] | None = None) -> Path:
    """规范化路径：展开 `~`、拼上基准目录、**解析符号链接**、转绝对路径。

    用 :func:`os.path.realpath` 而不是 ``resolve(strict=True)``——目标还不存在时
    （例如即将新建的文件）也必须能得到规范化结果，否则"写新文件"永远无法判边界。
    """
    raw = str(path)
    candidate = Path(raw).expanduser()
    if not candidate.is_absolute():
        anchor = Path(base) if base is not None else Path.cwd()
        candidate = anchor / candidate
    return Path(os.path.realpath(candidate))


def is_within(path: str | os.PathLike[str], root: str | os.PathLike[str]) -> bool:
    """``path`` 是否位于 ``root`` 之内（含 root 本身）；两侧都按真实路径比较。"""
    resolved = normalize(path)
    anchor = normalize(root)
    if resolved == anchor:
        return True
    try:
        resolved.relative_to(anchor)
    except ValueError:
        return False
    return True


def is_protected(path: str | os.PathLike[str]) -> bool:
    """命中敏感文件 / 凭据 / 版本库元数据。

    判定按**规范化后的路径逐段**比较目录名，因此软链解析后的真实位置也逃不掉
    （例如 ``workspace/link -> /home/u/.ssh``）。
    """
    resolved = normalize(path)
    parts = [part.lower() for part in resolved.parts]
    # 目录名命中即可（``.git/config`` 的 ``.git`` 段也算）
    if any(part in PROTECTED_DIRS for part in parts):
        return True
    name = resolved.name.lower()
    if name in PROTECTED_FILES:
        return True
    if resolved.suffix.lower() in PROTECTED_SUFFIXES:
        return True
    # ``.env.something`` 变体
    return name.startswith(".env.")


def looks_like_path(token: str) -> bool:
    """这个命令行参数是否是"像路径"的东西。

    刻意**宁严勿宽**：误判只会让检查更严格，漏判才会放过越界。
    """
    if not token or token in _REDIRECT_TOKENS:
        return False
    if token.startswith("-"):
        # 开关本身不是路径（``--out=/x`` 由调用方拆出 value 后再判断）
        return False
    if token in {".", "..", "./", "../"}:
        return True
    if token.startswith(("/", "\\", "~", "./", "../", ".\\", "..\\")):
        return True
    if _DRIVE_RE.match(token):
        return True
    if "/" in token or "\\" in token:
        return True
    return Path(token).suffix.lower() in _PATHLIKE_SUFFIXES


def _split_flag(token: str) -> list[str]:
    """``--out=/tmp/x`` → ``['--out', '/tmp/x']``；其余原样返回。"""
    if token.startswith("-") and "=" in token:
        _, _, value = token.partition("=")
        if value:
            return [value]
    return [token]


def extract_argv_paths(
    argv: list[str] | tuple[str, ...], *, cwd: str | os.PathLike[str] | None = None
) -> list[tuple[str, str]]:
    """从命令参数里抽出路径，返回 ``[(原始 token, 规范化路径), ...]``。

    覆盖四类来源：

    - 普通参数里的路径（``python x.py /tmp/data``）；
    - ``--flag=path``；
    - shell 重定向目标（``> out.txt`` / ``2>> log.txt``）——由调用方按**写**访问判定；
    - 参数内部的绝对路径片段（``--dir=/etc`` 已由上面覆盖）。

    :param argv: **不含 argv[0]** 的参数列表（可执行文件本身不在这里判边界）。
    """
    found: list[tuple[str, str]] = []
    base = cwd
    for raw_index, token in enumerate(argv):
        for piece in _split_flag(token):
            if not looks_like_path(piece):
                continue
            found.append((piece, str(normalize(piece, base=base))))
        if token in _REDIRECT_TOKENS and raw_index + 1 < len(argv):
            target = argv[raw_index + 1]
            if looks_like_path(target):
                found.append((target, str(normalize(target, base=base))))
    # 去重但保持顺序
    seen: set[str] = set()
    unique: list[tuple[str, str]] = []
    for token, resolved in found:
        if resolved in seen:
            continue
        seen.add(resolved)
        unique.append((token, resolved))
    return unique


def redirect_targets(argv: list[str] | tuple[str, ...]) -> list[str]:
    """命令里被**写入**的重定向目标（``>`` / ``>>`` / ``2>``）。"""
    targets: list[str] = []
    for index, token in enumerate(argv):
        if token in _REDIRECT_TOKENS and not token.startswith("<") and index + 1 < len(argv):
            targets.append(argv[index + 1])
    return targets


__all__ = [
    "PROTECTED_DIRS",
    "PROTECTED_FILES",
    "PROTECTED_SUFFIXES",
    "WRITE_COMMANDS",
    "normalize",
    "is_within",
    "is_protected",
    "is_write_command",
    "looks_like_path",
    "extract_argv_paths",
    "redirect_targets",
]
