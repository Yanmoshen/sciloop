# Copyright 2026 SciLoop contributors
# Licensed under the Apache License, Version 2.0 (the "License")
"""路径解析与敏感目标规则（Agent 2 / WP-02）。

文档要求：

- 所有路径先**绝对化、realpath、大小写与分隔符归一化**，再判边界；
- 不存在目标的**父目录**也要 realpath（否则"新建文件"永远判不了边界）；
- junction / symlink / reparse point 检查解析后的**实际目标**；
- 敏感文件、凭据、密钥、`.git` 元数据用 **deny-read / deny-write 规则**，且规则可配置、带原因；
- 不把"参数看起来像路径"的启发式当作唯一安全边界（可疑命令升级到审批，见 manager）。

本模块只做解析与匹配，不做裁决。
"""

from __future__ import annotations

import os
import re
from pathlib import Path

from .models import ProtectedRule

#: Windows 上路径比较不区分大小写；macOS 默认也是不敏感（保守起见一并按不敏感处理）
CASE_INSENSITIVE = os.name == "nt" or os.sys.platform == "darwin"


# ---------------------------------------------------------------------------- 敏感规则
#: 默认敏感规则（可配置；每条都带审计原因）
DEFAULT_PROTECTED_RULES: tuple[ProtectedRule, ...] = (
    ProtectedRule("vcs.git", "dir", ".git", reason="版本库元数据"),
    ProtectedRule("vcs.hg", "dir", ".hg", reason="版本库元数据"),
    ProtectedRule("vcs.svn", "dir", ".svn", reason="版本库元数据"),
    ProtectedRule("cred.ssh", "dir", ".ssh", reason="SSH 凭据目录"),
    ProtectedRule("cred.aws", "dir", ".aws", reason="云凭据目录"),
    ProtectedRule("cred.gnupg", "dir", ".gnupg", reason="GPG 凭据目录"),
    ProtectedRule("cred.kube", "dir", ".kube", reason="Kubernetes 凭据目录"),
    ProtectedRule("cred.env", "file", ".env", reason="环境变量凭据", deny_read=True),
    ProtectedRule("cred.env-prefix", "prefix", ".env.", reason="环境变量凭据", deny_read=True),
    ProtectedRule("cred.netrc", "file", ".netrc", reason="凭据文件", deny_read=True),
    ProtectedRule("cred.npmrc", "file", ".npmrc", reason="凭据文件", deny_read=True),
    ProtectedRule("cred.pypirc", "file", ".pypirc", reason="凭据文件", deny_read=True),
    ProtectedRule("cred.git-credentials", "file", ".git-credentials", reason="凭据文件", deny_read=True),
    ProtectedRule("cred.id-rsa", "file", "id_rsa", reason="私钥", deny_read=True),
    ProtectedRule("cred.id-ed25519", "file", "id_ed25519", reason="私钥", deny_read=True),
    ProtectedRule("cred.credentials", "file", "credentials", reason="凭据文件", deny_read=True),
    ProtectedRule("cred.credentials-json", "file", "credentials.json", reason="凭据文件", deny_read=True),
    ProtectedRule("cred.secrets-json", "file", "secrets.json", reason="凭据文件", deny_read=True),
    ProtectedRule("key.pem", "suffix", ".pem", reason="私钥/证书", deny_read=True),
    ProtectedRule("key.key", "suffix", ".key", reason="私钥", deny_read=True),
    ProtectedRule("key.pfx", "suffix", ".pfx", reason="私钥", deny_read=True),
    ProtectedRule("key.p12", "suffix", ".p12", reason="私钥", deny_read=True),
    ProtectedRule("key.keystore", "suffix", ".keystore", reason="私钥", deny_read=True),
)

#: 常见「像路径」的后缀——用于识别命令参数里的相对路径（例如 ``out.json``）
_PATHLIKE_SUFFIXES: frozenset[str] = frozenset(
    {
        ".txt", ".json", ".jsonl", ".md", ".py", ".js", ".ts", ".vue", ".csv", ".tsv",
        ".log", ".pdf", ".docx", ".xlsx", ".png", ".jpg", ".jpeg", ".gif", ".svg",
        ".sh", ".ps1", ".bat", ".cmd", ".exe", ".zip", ".tar", ".gz", ".db",
        ".sqlite", ".sqlite3", ".yaml", ".yml", ".toml", ".ini", ".cfg", ".conf",
    }
)

_DRIVE_RE = re.compile(r"^[A-Za-z]:[\\/]")
_REDIRECT_TOKENS = frozenset({">", ">>", "1>", "1>>", "2>", "2>>", "&>", "&>>", "<", "2<"})

#: 这些命令的**参数路径是写入目标**（不是只读）——否则 `mkdir /tmp/x` 会被当成"读 /tmp/x"
WRITE_COMMANDS: frozenset[str] = frozenset(
    {
        "mkdir", "mkd", "md", "touch", "rm", "rmdir", "del", "erase", "rd", "unlink",
        "cp", "copy", "xcopy", "robocopy", "mv", "move", "tee", "chmod", "chown", "chgrp",
        "ln", "truncate", "dd", "install", "new-item", "set-content", "add-content",
        "out-file", "remove-item", "move-item", "copy-item", "rename-item", "clear-content",
        "expand-archive", "compress-archive",
    }
)

#: 允许"内联代码"的解释器（出现这些且代码里带破坏性/路径特征 → 升级审批）
INLINE_CODE_FLAGS: frozenset[str] = frozenset({"-c", "-e", "--command", "-command", "/c"})
#: 内联代码里的可疑特征（无法静态判定的写入/网络/进程动作）
SUSPICIOUS_CODE_MARKERS: tuple[str, ...] = (
    "open(",
    "write",
    "write_text",
    "write_bytes",
    "shutil",
    "unlink",
    "rmtree",
    "remove(",
    "subprocess",
    "os.system",
    "popen",
    "invoke-",
    "curl ",
    "wget ",
    "start-process",
    "iex",
    "remove-item",
    "set-content",
    ">",
    "|",
)


# ---------------------------------------------------------------------------- 规范化
def strip_extended_prefix(raw: str) -> str:
    r"""去掉 Windows 扩展长度前缀（``\\?\`` / ``\\?\UNC\``），否则比较会失真。"""
    if raw.startswith("\\\\?\\UNC\\"):
        return "\\\\" + raw[len("\\\\?\\UNC\\") :]
    if raw.startswith("\\\\?\\"):
        return raw[len("\\\\?\\") :]
    return raw


def to_absolute(path: str | os.PathLike[str], *, base: str | os.PathLike[str] | None = None) -> Path:
    """绝对化（不解析链接），并展开 ``~``、统一分隔符。"""
    raw = strip_extended_prefix(str(path)).replace("/", os.sep)
    candidate = Path(raw).expanduser()
    if not candidate.is_absolute():
        anchor = Path(base) if base is not None else Path.cwd()
        candidate = anchor / candidate
    return Path(os.path.normpath(str(candidate)))


def normalize(path: str | os.PathLike[str], *, base: str | os.PathLike[str] | None = None) -> Path:
    """绝对化 + **realpath（解析 symlink/junction/reparse point）**。目标不存在时也安全。"""
    return Path(os.path.realpath(str(to_absolute(path, base=base))))


def resolve_for_check(
    path: str | os.PathLike[str], *, base: str | os.PathLike[str] | None = None
) -> tuple[Path, str]:
    """按文档要求解析路径，返回 ``(解析后路径, 解析方式)``。

    - 目标存在 → 直接 realpath（symlink / junction / reparse point 都会被解开）；
    - 目标不存在 → **解析最近的已存在祖先**再拼回剩余部分（新建文件的正确边界判定）；
    - 都不存在（例如整个盘符都不存在）→ 回退到 normpath 结果。
    """
    raw = to_absolute(path, base=base)
    if raw.exists():
        return Path(os.path.realpath(str(raw))), "existing"

    trailing: list[str] = []
    current = raw
    while not current.exists() and current != current.parent:
        trailing.append(current.name)
        current = current.parent
    if not current.exists():  # pragma: no cover - 盘符不存在时
        return raw, "normpath-only"
    resolved = Path(os.path.realpath(str(current)))
    for name in reversed(trailing):
        resolved = resolved / name
    return resolved, "parent-resolved"


def normalize_key(path: str | os.PathLike[str] | Path) -> str:
    """比较用的归一化键：统一分隔符 + （不敏感平台）大小写折叠 + 去掉尾分隔符。"""
    key = str(path).replace("/", os.sep).rstrip(os.sep) or os.sep
    return key.casefold() if CASE_INSENSITIVE else key


def is_within(path: str | os.PathLike[str], root: str | os.PathLike[str]) -> bool:
    """``path`` 是否位于 ``root`` 之内（含 root 本身）；两侧都按真实路径 + 大小写不敏感比较。"""
    resolved = normalize_key(resolve_for_check(path)[0])
    anchor = normalize_key(resolve_for_check(root)[0])
    if resolved == anchor:
        return True
    return resolved.startswith(anchor + os.sep)


def matched_root(path: str | os.PathLike[str], roots: list[Path]) -> Path | None:
    """返回第一个包含 ``path`` 的根（按 key 比较，避免大小写差异误判）。"""
    key = normalize_key(resolve_for_check(path)[0])
    for root in roots:
        anchor = normalize_key(root)
        if key == anchor or key.startswith(anchor + os.sep):
            return root
    return None


def match_protected_rule(
    path: str | os.PathLike[str], rules: tuple[ProtectedRule, ...] = DEFAULT_PROTECTED_RULES
) -> ProtectedRule | None:
    """命中哪条敏感规则（按解析后的真实路径逐段比较，软链也逃不掉）。"""
    resolved = resolve_for_check(path)[0]
    parts = tuple(part.lower() for part in resolved.parts)
    name = resolved.name.lower()
    for rule in rules:
        if rule.matches(parts, name):
            return rule
    return None


def is_protected(path: str | os.PathLike[str]) -> bool:
    return match_protected_rule(path) is not None


# ---------------------------------------------------------------------------- 命令参数
def looks_like_path(token: str) -> bool:
    """这个命令行参数是否是"像路径"的东西（宁严勿宽）。"""
    if not token or token in _REDIRECT_TOKENS:
        return False
    if token.startswith("-"):
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
    if token.startswith("-") and "=" in token:
        _, _, value = token.partition("=")
        if value:
            return [value]
    return [token]


def extract_argv_paths(
    argv: list[str] | tuple[str, ...], *, cwd: str | os.PathLike[str] | None = None
) -> list[tuple[str, str]]:
    """从命令参数里抽出路径，返回 ``[(原始 token, 规范化路径), ...]``。"""
    found: list[tuple[str, str]] = []
    tokens = [str(item) for item in argv]
    for raw_index, token in enumerate(tokens):
        for piece in _split_flag(token):
            if not looks_like_path(piece):
                continue
            found.append((piece, str(normalize(piece, base=cwd))))
        if token in _REDIRECT_TOKENS and raw_index + 1 < len(tokens):
            target = tokens[raw_index + 1]
            if looks_like_path(target):
                found.append((target, str(normalize(target, base=cwd))))
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
    tokens = [str(item) for item in argv]
    targets: list[str] = []
    for index, token in enumerate(tokens):
        if token in _REDIRECT_TOKENS and not token.startswith("<") and index + 1 < len(tokens):
            targets.append(tokens[index + 1])
    return targets


def is_write_command(program: str) -> bool:
    """这个可执行文件的参数路径是否应视为**写入目标**。"""
    return Path(program).name.lower() in WRITE_COMMANDS


def inline_code_suspicion(argv: list[str] | tuple[str, ...]) -> str | None:
    """内联代码的可疑性：返回升级原因或 None。

    ``python -c "print(1)"`` 这类无害内联代码**不升级**；一旦代码字符串里出现
    写入 / 删除 / 网络 / 进程 / 管道等特征，就说明"参数看起来像路径"的启发式已经不够，
    必须升级到审批（文档 WP-02 明确要求）。
    """
    tokens = [str(item) for item in argv]
    if len(tokens) < 3:
        return None
    flag_index = next(
        (index for index, token in enumerate(tokens[1:], start=1) if token.lower() in INLINE_CODE_FLAGS),
        None,
    )
    if flag_index is None or flag_index + 1 >= len(tokens):
        return None
    code = " ".join(tokens[flag_index + 1 :]).lower()
    hits = [marker for marker in SUSPICIOUS_CODE_MARKERS if marker in code]
    if not hits:
        return None
    return f"内联代码含可疑特征（{', '.join(hits[:3])}），静态无法判定 → 升级审批"


__all__ = [
    "DEFAULT_PROTECTED_RULES",
    "WRITE_COMMANDS",
    "INLINE_CODE_FLAGS",
    "SUSPICIOUS_CODE_MARKERS",
    "CASE_INSENSITIVE",
    "strip_extended_prefix",
    "to_absolute",
    "normalize",
    "resolve_for_check",
    "normalize_key",
    "is_within",
    "matched_root",
    "match_protected_rule",
    "is_protected",
    "looks_like_path",
    "extract_argv_paths",
    "redirect_targets",
    "is_write_command",
    "inline_code_suspicion",
]
