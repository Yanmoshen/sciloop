# Copyright 2026 SciLoop contributors
# Licensed under the Apache License, Version 2.0 (the "License")
"""权限事实渲染给模型（Agent 2 / WP-04）。

只说明**事实**，不给出"已批准"这类的结论字符串：

- 当前 sandbox mode；
- workspace 根目录；
- workspace 外是否只读；
- 当前会话已批准的命令前缀（**已规范化，且不含令牌**）；
- 当前工具能否执行、是否需要审批；
- 不可读路径类别与原因。

禁止出现：审批令牌、审批 ID、内部审批存储路径、系统 ACL 细节、
任何可被模型伪造的"approved / 已批准"字样（"是否需要审批"用中性描述表达）。
"""

from __future__ import annotations

import re
from typing import Any

#: 禁止出现在模型可见文本里的东西
FORBIDDEN_PATTERNS: tuple[tuple[str, re.Pattern[str]], ...] = (
    ("审批ID", re.compile(r"\bap_[0-9a-f]{10,}\b")),
    ("调用ID", re.compile(r"\bcall_[0-9a-f]{10,}\b")),
    ("令牌字段", re.compile(r"\btoken\b", re.IGNORECASE)),
    ("令牌字面", re.compile(r"\beyJ[A-Za-z0-9_\-]{10,}")),
    ("审批存储路径", re.compile(r"approvals\.json|approval_store|\btokens\.json\b", re.IGNORECASE)),
    ("ACL细节", re.compile(r"\bicacls\b|\bsetfacl\b|\bchmod\s+[0-7]{3,4}\b", re.IGNORECASE)),
    ("放行结论", re.compile(r"已批准|approved", re.IGNORECASE)),
)

#: 类别 → 中文说明（给模型看的是**类别**，不是具体文件名）
CATEGORY_LABELS: dict[str, str] = {
    "version_control_metadata": "版本库元数据",
    "credentials": "凭据与环境变量",
    "private_keys": "私钥与证书",
}


def permission_facts(
    *,
    sandbox_facts: Any,
    tools: list[dict[str, Any]] | None = None,
    approved_prefixes: list[str] | None = None,
    denied_read_categories: list[str] | None = None,
) -> dict[str, Any]:
    """结构化事实（供 Agent 1 拼进系统提示或单独作为一条事实消息）。"""
    facts = sandbox_facts.to_dict() if hasattr(sandbox_facts, "to_dict") else dict(sandbox_facts)
    tool_rows = list(tools or [])
    return {
        "sandbox_mode": facts.get("sandbox_mode"),
        "workspace_root": facts.get("workspace_root"),
        "outside_workspace_read_only": not bool(facts.get("outside_readable", True)),
        "writable_roots": list(facts.get("writable_roots") or []),
        "approved_command_prefixes": list(approved_prefixes or []),
        "tools_requiring_approval": sorted(
            item["name"] for item in tool_rows if item.get("permission_class") in {"exec", "dangerous"}
        ),
        "tools_auto_run": sorted(
            item["name"] for item in tool_rows if item.get("permission_class") == "read"
        ),
        "unreadable_category_reasons": {
            category: f"{CATEGORY_LABELS.get(category, category)}：访问被拒绝"
            for category in (denied_read_categories or [])
        },
        "platform_capability": dict(facts.get("capability") or {}),
    }


def render_permission_facts(
    *,
    sandbox_facts: Any,
    tools: list[dict[str, Any]] | None = None,
    approved_prefixes: list[str] | None = None,
    denied_read_categories: list[str] | None = None,
) -> str:
    """渲染成给模型读的自然语言块（**脱敏**）。"""
    data = permission_facts(
        sandbox_facts=sandbox_facts,
        tools=tools,
        approved_prefixes=approved_prefixes,
        denied_read_categories=denied_read_categories,
    )
    lines = [
        "运行时权限事实（由沙箱与审批层生成，供你判断可行性）：",
        f"- 沙箱模式：{data['sandbox_mode']}",
        f"- 工作区根目录：{data['workspace_root']}",
        "- 工作区外："
        + ("只读（写入或执行需要研究者决定）" if data["outside_workspace_read_only"] else "可读"),
    ]
    if data["writable_roots"]:
        lines.append(f"- 可写根：{'；'.join(str(item) for item in data['writable_roots'])}")
    if data["approved_command_prefixes"]:
        lines.append(
            "- 本对话内已长期放行的命令前缀（仍然逐次审计）："
            + "；".join(str(item) for item in data["approved_command_prefixes"])
        )
    if data["tools_auto_run"]:
        lines.append("- 可直接调用的只读工具：" + "、".join(data["tools_auto_run"]))
    if data["tools_requiring_approval"]:
        lines.append("- 需要研究者逐次决定的工具：" + "、".join(data["tools_requiring_approval"]))
    for category, reason in data["unreadable_category_reasons"].items():
        lines.append(f"- 不可读类别：{CATEGORY_LABELS.get(category, category)}（{reason}）")
    capability = data["platform_capability"]
    if capability:
        mechanisms = "、".join(str(item) for item in capability.get("mechanisms", []))
        lines.append(
            "- 平台隔离能力：" + mechanisms
            + ("（未启用系统级强隔离）" if not capability.get("strong_isolation") else "")
        )
    lines.append("- 说明：最终是否放行由执行层决定，以上事实不构成任何执行许可。")
    text = "\n".join(lines)
    assert_no_secrets(text)
    return text


def assert_no_secrets(text: str) -> None:
    """发现禁止内容立即抛错（防止令牌/审批 ID/ACL 细节泄漏到模型上下文）。"""
    for label, pattern in FORBIDDEN_PATTERNS:
        hit = pattern.search(text or "")
        if hit:
            raise ValueError(f"permission facts 泄漏了{label}：{hit.group(0)!r}")


def check_no_secrets(text: str) -> list[str]:
    """不抛错的版本（测试用）：返回命中的标签列表。"""
    return [label for label, pattern in FORBIDDEN_PATTERNS if pattern.search(text or "")]


__all__ = [
    "FORBIDDEN_PATTERNS",
    "CATEGORY_LABELS",
    "permission_facts",
    "render_permission_facts",
    "assert_no_secrets",
    "check_no_secrets",
]
