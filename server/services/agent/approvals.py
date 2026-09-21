# Copyright 2026 SciLoop contributors
# Licensed under the Apache License, Version 2.0 (http://www.apache.org/licenses/LICENSE-2.0)
"""研究者批准：把「模型想执行」变成「研究者批准后才执行」。

为什么要有这一层
----------------
合规 8.3：关键动作需要人工接管。`mcp_server.guard` 的门 3 已经实现了「必须带批准令牌」，
但**令牌从哪来**是产品问题——没有签发入口，门 3 就只能靠"不摆工具给模型"来规避，
能力等于没接上。本模块就是那个入口的存储层。

三条口径（已确认，别再改）
--------------------------
1. **批准随会话记录落盘**，不加表、不动迁移（`EXPECTED_TABLES` 保持 37）。
   批准是"这一轮对话里发生的事"，与 turns 同源，天然自洽。
2. **一次性、带有效期、绑死具体调用**：令牌绑 `request_id`，`request_id` 绑
   conversation + 工具 + 那次具体参数（`args_digest`）。批准的是**这一次调用**，
   不是"以后这类调用都放行"——后者等于把批准变成了一次性开关。
3. **不存明文令牌**：会话文件只留指纹（`token_fingerprint`）。明文只在"执行的那一瞬间"
   存在于内存里并被送进 MCP 子进程的启动环境。会话文件是可以被人工打开查看的（这是它的
   设计目的），把可用凭据写进去就等于把它交出去了。

请求的样子
----------
``{"id", "tool", "args_digest", "preview", "cwd", "status", "created_at", "expires_at",
   "decided_at", "decided_by", "note", "token_fingerprint", "consumed_at"}``

状态机：``pending → approved | denied | expired``；``approved`` 执行后加 ``consumed_at``。
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
    """给人看的一行预览：**研究者要看的就是这一行**，所以它必须说清"到底要跑什么"。"""

    if tool == "run_command":
        argv = args.get("argv")
        # argv 逐项传入、不经过 shell，所以这里也**不做任何转义/拼接猜测**：
        # 原样用空格连接即可 —— 加引号反而会让研究者以为会走 shell。
        text = " ".join(str(item) for item in argv) if isinstance(argv, list) else str(argv or "")
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
    """把"到点没批"如实算成 `expired` —— 过期是**读出来的**事实，不依赖谁去巡检。"""

    status = str(request.get("status") or "pending")
    if status != "pending":
        return status
    expires = _parse(request.get("expires_at"))
    if expires is not None and (now or _now()) >= expires:
        return "expired"
    return "pending"


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
    "PREVIEW_MAX_CHARS",
    "REQUEST_TTL_SECONDS",
    "TOKEN_PREFIX",
    "VALID_STATUSES",
    "args_digest",
    "attach",
    "decide",
    "effective_status",
    "find",
    "find_pending",
    "issue_token",
    "load",
    "mark_consumed",
    "new_request",
    "save",
    "token_fingerprint",
]
