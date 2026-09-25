# Copyright 2026 SciLoop contributors
# Licensed under the Apache License, Version 2.0 (http://www.apache.org/licenses/LICENSE-2.0)
"""研究者批准：把「模型想执行」变成「研究者批准后才执行」。

为什么要有这一层
----------------
合规 8.3：关键动作需要人工接管。`mcp_server.guard` 的门 3 已经实现了「必须带批准令牌」，
但**令牌从哪来**是产品问题——没有签发入口，门 3 就只能靠"不摆工具给模型"来规避，
能力等于没接上。本模块就是那个入口的存储层。

三条口径（2026-09-22 按研究者要求改过，别再改回去）
--------------------------------------------------
1. **批准随会话记录落盘**，不加表、不动迁移（`EXPECTED_TABLES` 保持 37）。
   批准是"这一轮对话里发生的事"，与 turns 同源，天然自洽。
2. **批准分三种**（研究者的原话：「做成三个选择，批准此次执行，此对话中默认允许执行，拒绝执行」）：
   - `approve` —— 只批这一次调用；
   - `approve_conversation` —— 在本对话里以后这类（**包括高危**）都直接放行（写进会话的授权块）；
   - `deny` —— 拒绝。
   ⚠️ **不再有 15 分钟自动失效**（研究者明确说"没必要"）：一条待批请求会一直等着，
   直到研究者自己处理它。批准仍绑死具体调用（`request_id` + `args_digest`），
   所以"批准了 A"不会被拿去执行 B。
3. **不存明文令牌**：会话文件只留指纹（`token_fingerprint`）。明文只在"执行的那一瞬间"
   存在于内存里并被送进 MCP 子进程的启动环境。会话文件是可以被人工打开查看的（这是它的
   设计目的），把可用凭据写进去就等于把它交出去了。

按对话的授权块（`record["grants"]`）
-----------------------------------
``{"full_access": bool, "allow_exec": bool}``
- `full_access` = 输入栏那个「完全访问模式」开关：**低危动作免批准**（高危仍要点头）；
- `allow_exec` = 批准卡里选「此对话中默认允许执行」：**连高危也免批准**。
两个都是**按对话**的 —— 换个对话就要重新决定，避免"某一次心软变成了永久开关"。

请求的样子
----------
``{"id", "tool", "args_digest", "preview", "cwd", "status", "created_at", "expires_at",
   "decided_at", "decided_by", "note", "token_fingerprint", "consumed_at"}``

状态机：``pending → approved | denied``；``approved`` 执行后加 ``consumed_at``。
（`expires_at` 字段保留但**不再自动作废**，只为兼容旧记录与前端展示。）
**没有"审批通过但没跑"以外的中间态**——不给"已批准但还在排队"这种模糊状态留位置。
"""

from __future__ import annotations

import hashlib
import json
import logging
import secrets
from datetime import UTC, datetime, timedelta
from typing import Any

from services import conversations

logger = logging.getLogger("sciloop.approvals")

#: 请求有效期。到点未批即作废：一个躺了半小时的批准请求，研究者多半已经忘了上下文，
#: 此时点"批准"批准的其实是自己想象出来的东西。
REQUEST_TTL_SECONDS = 900

#: 令牌前缀。带上它，日志/环境里一眼能认出这是批准令牌而不是别的 key。
TOKEN_PREFIX = "apv"

VALID_STATUSES = ("pending", "approved", "denied", "expired")

#: 三种裁决（研究者的原话：批准此次执行 / 此对话中默认允许执行 / 拒绝执行）
DECISION_APPROVE = "approve"
DECISION_APPROVE_CONVERSATION = "approve_conversation"
DECISION_DENY = "deny"
VALID_DECISIONS = (DECISION_APPROVE, DECISION_APPROVE_CONVERSATION, DECISION_DENY)

#: 按对话的授权键
GRANT_FULL_ACCESS = "full_access"
GRANT_ALLOW_EXEC = "allow_exec"
#: 按**工具名**授权的清单（用户口径 2026-09-25：批准一条命令 = 本对话内允许这个工具）。
#: 例如 `["run_command"]` —— 之后同一工具换参数不再逐条问。
#: ⚠️ **高危不在此列**：`policy` 里的四类高危永远逐次批准，任何一档授权都不放行它。
GRANT_TOOLS = "tools"

#: 预览的最大长度（对话里那一行放不下更长的）
PREVIEW_MAX_CHARS = 200


def _now() -> datetime:
    return datetime.now(UTC)


def _iso(moment: datetime) -> str:
    return moment.isoformat(timespec="seconds")


def _parse(value: Any) -> datetime | None:
    if not isinstance(value, str) or not value:
        return None
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError:
        return None
    return parsed if parsed.tzinfo is not None else parsed.replace(tzinfo=UTC)


def args_digest(tool: str, args: dict[str, Any]) -> str:
    """这次调用的稳定指纹。**批准绑的是它**，所以必须包含工具名与全部参数。"""

    canonical = json.dumps({"tool": tool, "args": args}, ensure_ascii=False, sort_keys=True)
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()[:32]


def token_fingerprint(token: str) -> str:
    """令牌指纹（存这个，不存明文）。"""

    return hashlib.sha256(token.encode("utf-8")).hexdigest()[:16]


def issue_token(request_id: str) -> str:
    """签发一次性批准令牌。

    返回值**只应该流向两处**：MCP 子进程的启动环境、以及 `token_fingerprint()`。
    不要放进响应体、不要放进会话文件、不要写日志。
    """

    return f"{TOKEN_PREFIX}_{request_id}_{secrets.token_hex(16)}"


def _preview(tool: str, args: dict[str, Any]) -> str:
    """给人看的一行预览：**研究者要看的就是这一行**，所以它必须说清"到底要跑什么"。

    ⚠️ 只给"要跑的东西本身"，**不要 JSON 外壳**（研究者 2026-09-22）：
    原先宿主工具走 `json.dumps(args)`，卡片上就显示成
    `{"command": "pwd && echo ..."}` —— 括号和 `"command"` 是给机器看的，
    人只需要看到 `pwd && echo ...`。
    """

    if tool == "run_command":
        argv = args.get("argv")
        # argv 逐项传入、不经过 shell，所以这里也**不做任何转义/拼接猜测**：
        # 原样用空格连接即可 —— 加引号反而会让研究者以为会走 shell。
        text = " ".join(str(item) for item in argv) if isinstance(argv, list) else str(argv or "")
    elif tool == "run_on_computer":
        text = str(args.get("command") or "")
    elif tool == "files_on_computer":
        action = str(args.get("action") or "")
        path = str(args.get("path") or "")
        to = str(args.get("to") or "")
        text = f"{action} {path}".strip() + (f" → {to}" if to else "")
    else:
        text = json.dumps(args, ensure_ascii=False)
    text = text.strip() or "(无参数)"
    return text[:PREVIEW_MAX_CHARS] + ("…" if len(text) > PREVIEW_MAX_CHARS else "")


def new_request(
    *,
    tool: str,
    args: dict[str, Any],
    cwd: str | None = None,
    call_id: str | None = None,
    ttl_seconds: int = REQUEST_TTL_SECONDS,
) -> dict[str, Any]:
    """构造一条批准请求（**只构造，不落盘** —— 落盘由调用方随轮次一起写，只写一次文件）。"""

    created = _now()
    return {
        "id": secrets.token_hex(8),
        "tool": tool,
        "args_digest": args_digest(tool, args),
        "preview": _preview(tool, args),
        # 参数本体也要留着：批准之后要按**原样**执行，不能靠预览字符串反推
        "args": args,
        # 模型当时给出的 tool_call id：执行时沿用它，tool_calls 与 tool 结果才严格配对
        "call_id": call_id,
        "cwd": cwd,
        "status": "pending",
        "created_at": _iso(created),
        "expires_at": _iso(created + timedelta(seconds=max(0, ttl_seconds))),
        "decided_at": None,
        "decided_by": None,
        "note": None,
        "token_fingerprint": None,
        "consumed_at": None,
    }


def effective_status(request: dict[str, Any], *, now: datetime | None = None) -> str:
    """一条批准请求现在是什么状态。

    ⚠️ **不再按时间自动作废**（研究者 2026-09-22：「一次性和 15 分钟自动失效没必要」）。
    待批就是待批，直到研究者自己处理；`expires_at` 仅作展示用，不参与判定。
    因此这里只会返回 `pending` / `approved` / `denied`（旧记录里的 `expired` 原样返回）。
    """

    return str(request.get("status") or "pending")


# --------------------------------------------------------------------------- #
# 按对话的授权（完全访问模式 / 本对话默认允许）
# --------------------------------------------------------------------------- #
def grants(record: dict[str, Any]) -> dict[str, Any]:
    """读这个对话当前的授权块。

    形状：``{full_access: bool, allow_exec: bool, tools: [工具名…]}``。
    ``tools`` 是**按工具授权**那一档（用户口径 2026-09-25）：批准一次 ``run_command`` 之后，
    同一工具换参数不再逐条问（高危仍然每次都问）。
    """

    raw = record.get("grants")
    raw = raw if isinstance(raw, dict) else {}
    tools = raw.get(GRANT_TOOLS)
    return {
        GRANT_FULL_ACCESS: bool(raw.get(GRANT_FULL_ACCESS)),
        GRANT_ALLOW_EXEC: bool(raw.get(GRANT_ALLOW_EXEC)),
        GRANT_TOOLS: [str(item) for item in tools] if isinstance(tools, list) else [],
    }


def set_grants(
    record: dict[str, Any],
    *,
    full_access: bool | None = None,
    allow_exec: bool | None = None,
    tool: str | None = None,
) -> dict[str, Any]:
    """改这个对话的授权块（**只改内存**，落盘由调用方 `save`）。

    `None` = 不动这一项。关掉 `allow_exec` 时**不连带关掉** `full_access` ——
    它们是两件事（一个是"免点头动手"，一个是"连高危也放行"），各有各的开关。
    `tool` 非空 = 把某个工具加进"按工具授权"清单（用户口径 2026-09-25）。
    """

    current = grants(record)
    if full_access is not None:
        current[GRANT_FULL_ACCESS] = bool(full_access)
    if allow_exec is not None:
        current[GRANT_ALLOW_EXEC] = bool(allow_exec)
    if tool:
        tools = list(current.get(GRANT_TOOLS) or [])
        if tool not in tools:
            tools.append(tool)
        current[GRANT_TOOLS] = tools
    record["grants"] = dict(current)
    return current


def grants_summary(record: dict[str, Any]) -> dict[str, Any]:
    """给界面看的授权摘要（人话 + 布尔值 + 已授权的工具清单）。"""

    current = grants(record)
    tools = list(current.get(GRANT_TOOLS) or [])
    if current[GRANT_ALLOW_EXEC]:
        note = "本对话内：连高危操作也直接执行"
    elif current[GRANT_FULL_ACCESS]:
        note = "本对话内：普通操作直接执行，高危操作仍会先问你"
    elif tools:
        note = "本对话内：" + "、".join(tools) + " 直接执行，高危操作仍会先问你"
    else:
        note = "本对话内：动手前都会先问你"
    return {**current, "note": note}


def allows(grant: dict[str, Any], *, high_risk: bool, tool: str | None = None) -> bool:
    """这份授权是否允许**直接执行**（不发批准卡）。

    规则（研究者 2026-09-22 定，2026-09-25 加"按工具"一档）：
    - 「此对话中默认允许执行」= 最宽的一档，**连高危也直接做**；
    - 「完全访问模式」= 普通动手操作直接做，**高危仍然先问**；
    - 「本对话允许这个工具」（``tool`` 命中 ``grants.tools``）= 同一工具换参数不再问，
      **高危仍然先问**；
    - 都没命中 → 一概先问。

    ⚠️ **高危只认第一档**：这是"会不会不打招呼就动研究者机器"的边界，穷举测试钉住它。
    """

    if grant.get(GRANT_ALLOW_EXEC):
        return True
    if high_risk:
        return False
    if grant.get(GRANT_FULL_ACCESS):
        return True
    tools = grant.get(GRANT_TOOLS)
    return bool(tool) and isinstance(tools, list) and tool in tools


def find(record: dict[str, Any], request_id: str) -> tuple[int, dict[str, Any]] | None:
    """在会话里定位一条批准请求，返回 `(轮次下标, 请求)`；找不到返回 None。"""

    for index, turn in enumerate(record.get("turns") or []):
        for request in turn.get("approvals") or []:
            if str(request.get("id")) == request_id:
                return index, request
    return None


def find_pending(record: dict[str, Any], request_id: str) -> tuple[int, dict[str, Any]] | None:
    """只认**还能批**的那一条：已批准/已拒绝/已过期的都不算。

    这条区分是要紧的：把"已经批过的请求"当成可再批，等于**同一个批准能反复执行**。
    """

    found = find(record, request_id)
    if found is None:
        return None
    index, request = found
    return (index, request) if effective_status(request) == "pending" else None


def attach(record: dict[str, Any], turn_index: int, request: dict[str, Any]) -> None:
    """把请求挂到某个轮次上（**只改内存**，落盘由调用方统一 `conversations.write`）。"""

    turn = (record.get("turns") or [])[turn_index]
    turn["approvals"] = [*(turn.get("approvals") or []), request]


def decide(
    record: dict[str, Any],
    request_id: str,
    *,
    status: str,
    actor: str,
    note: str | None = None,
    token: str | None = None,
) -> dict[str, Any] | None:
    """记一次裁决（**只改内存**）。

    `actor` 是"谁批的"。stdio 下调用方就是后端自己，所以这里**不去假装有多个主体**：
    它如实记成 `owner`（Owner 令牌的唯一持有人），而不是编一个审计上站不住的假身份。
    """

    if status not in VALID_STATUSES:
        raise ValueError(f"非法裁决状态：{status}")
    found = find(record, request_id)
    if found is None:
        return None
    _, request = found
    request["status"] = status
    request["decided_at"] = _iso(_now())
    request["decided_by"] = actor
    if note:
        request["note"] = note[:500]
    if token:
        # 存指纹不存明文：会话文件是给人看的，不能把可用凭据放在里面
        request["token_fingerprint"] = token_fingerprint(token)
    # `args` 保留不删：批准之后要按**原样**执行，靠预览字符串反推是不诚实的还原。
    return request


def mark_consumed(record: dict[str, Any], request_id: str) -> None:
    """标记这次批准已经用掉了（**一次性**：第二次调用找不到可批请求）。"""

    found = find(record, request_id)
    if found is None:
        return
    _, request = found
    request["consumed_at"] = _iso(_now())


def load(conversation_id: str) -> dict[str, Any] | None:
    """读会话（找不到返回 None）——只做转发，免得调用方两处 import。"""

    return conversations.read(conversation_id)


def save(record: dict[str, Any]) -> None:
    """落盘（写失败会抛，不吞）。"""

    conversations.write(record)


__all__ = [
    "DECISION_APPROVE",
    "DECISION_APPROVE_CONVERSATION",
    "DECISION_DENY",
    "GRANT_ALLOW_EXEC",
    "GRANT_FULL_ACCESS",
    "PREVIEW_MAX_CHARS",
    "REQUEST_TTL_SECONDS",
    "TOKEN_PREFIX",
    "VALID_DECISIONS",
    "VALID_STATUSES",
    "allows",
    "args_digest",
    "attach",
    "decide",
    "effective_status",
    "find",
    "find_pending",
    "grants",
    "grants_summary",
    "issue_token",
    "load",
    "mark_consumed",
    "new_request",
    "save",
    "set_grants",
    "token_fingerprint",
]
