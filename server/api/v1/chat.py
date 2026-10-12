# Copyright 2026 SciLoop contributors
# Licensed under the Apache License, Version 2.0 (http://www.apache.org/licenses/LICENSE-2.0)
"""首页「开始使用」的对话端点。

做什么
------
- ``POST /api/v1/chat/home``：一次请求内完成「生成标题」+「生成正式回答」（非流式）。
- ``POST /api/v1/chat/home/stream``：**真流式**（SSE），只给首页对话用。

为什么需要独立的一条流式路径
----------------------------
六环节流水线走 ``llm.adapter.chat()``（非流式 + 结构化输出 + JSON 校验重试）。
首页对话要的是「边生成边显示」，两者对重试/降级的要求互相冲突，因此**新增**
``adapter.chat_stream()`` 与 ``http_client.chat_completions_stream()``，
``chat()`` 那条主链路一行不改（风险最低）。

红线与口径
----------
- **外层只做校验与转发**，所有出网、路由、计价、记账、回放都复用 ``llm.adapter``
  —— 不自己拼 HTTP、不自己算钱（单价缺失时 ``cost_usd`` 如实为 null）。
- 属于写操作（会产生真实费用）→ **Owner 专属**，匿名 403 ``owner_token_required``。
- 标题失败**不让整次请求失败**：退化为「取用户输入前 20 字」，并在 ``title_source`` 里如实标注。
- 流中断/出错**不丢已生成部分**：落盘保留正文，SSE 尾部如实推 ``error`` 事件。
"""

from __future__ import annotations

import asyncio
import json
import logging
import re
import time
from collections.abc import AsyncIterator
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Literal

from fastapi import APIRouter, Depends, HTTPException, Request, status
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, Field
from sqlalchemy import select

from core.security import require_owner
from db.models import Project
from db.session import AsyncSessionLocal
from llm import adapter
from llm.errors import LLMError
from llm.registry import get_registry
from llm.types import slugify_provider
from services import conversations, home_turns
from services.agent import approvals as agent_approvals
from services.chat_attachments import normalize_chat_attachments
from services.research import dialog, phrasing
from services.research import intent as intent_mod
from services.system_prompt import SCILOOP_SYSTEM_PROMPT

logger = logging.getLogger("sciloop.chat")

router = APIRouter(tags=["chat"])

# Active generic homepage turns are separate from research-node subscriptions.
# The stop endpoint uses this registry to cancel the owning task before the
# browser closes its SSE connection.
_ACTIVE_HOME_TURNS: dict[str, tuple[asyncio.Event, asyncio.Task[Any]]] = {}


def request_home_turn_interrupt(conversation_id: str) -> bool:
    active = _ACTIVE_HOME_TURNS.get(str(conversation_id))
    if active is None:
        return False
    event, task = active
    event.set()
    current = asyncio.current_task()
    if task is not current and not task.done():
        task.cancel()
    return True

TITLE_MAX_CHARS = 20
#: 思考过程的落盘上限。**``None`` = 不截断**（2026-09-24 用户口径：不设限制）。
#: 原先 6000 —— 它只截存储、不改模型输出，且会**如实标出**"已截断"；
#: 但同为"悄悄变短"，一并放开。
REASONING_MAX_CHARS: int | None = None
#: 输出上限统一为 ``None``（不设限）：所有渠道所有模型一致，不按节点/供应商区别对待。
#: 历史上这里写 400，理由是"思考型模型会把预算花在 reasoning 上、content 为空"——
#: 那个症状的根因就是**给了预算上限**；不设限之后 reasoning 与正文各得其所。
TITLE_MAX_TOKENS: int | None = None

#: 触顶后的**收尾提示**：不摆工具，只要它把「完成了什么 / 还差什么」说清楚（用户口径 2026-09-25）。
WRAP_UP_PROMPT = (
    "本次回答已经达到执行上限，你现在**不能再调用任何工具**。"
    "请只根据已经取得的事实，简洁说明真实成果、未完成项及原因和必要的下一步。"
    "不要编造没做过的步骤，也不要再请求调用工具。"
)

#: 标题调用的 user 提示（约束在 `conversations.TITLE_SYSTEM` 里，两边都留着更稳）
TITLE_PROMPT = "为下面这段研究需求拟一个标题。\n\n研究需求：\n{text}"

REPLY_SYSTEM = SCILOOP_SYSTEM_PROMPT + "\n\n"

PLAIN_CHAT_SYSTEM = (
    "本对话已经明确按普通对话处理。请像正常助手一样直接回答当前问题，"
    "不要发送、执行或再次提起研究链邀请，也不要把普通交流改成研究流程。"
    "只有研究者明确说「开始文献调研」等执行指令时，程序才会切回研究模式。\n"
)


def reply_system_prompt(conversation: dict[str, Any] | None = None) -> str:
    """Return the shared prompt with the conversation-specific chain policy."""

    if conversation and conversation.get("plain_chat"):
        return REPLY_SYSTEM + PLAIN_CHAT_SYSTEM
    return REPLY_SYSTEM


#: 技能目录块的缓存（用户口径 2026-09-25：缓存 1 小时 + 技能库一变立即失效）。
#:
#: 为什么必须缓存：`all_packs()` 会对**每个技能包的每个脚本**做一次 `ast.parse` 语法体检
#: （实测本机 42 个技能包、175 个脚本、2.2MB）——**每次请求 8.3 秒**（首帧前那 8.6s 的真凶）。
#: 而给模型看目录只需要 name / 一句话 / 环节，"能不能跑"的结论应该等到 `load_skill` /
#: `run_skill` 时再要。缓存把这段从"每次请求"降到"一小时一次或技能库变更时一次"。
SKILLS_BLOCK_TTL_SECONDS = 3600.0
_SKILLS_BLOCK_CACHE: dict[str, Any] = {"signature": "", "at": 0.0, "text": ""}


def skills_signature() -> str:
    """技能库的「新鲜度指纹」：包目录 + 每个技能的 SKILL.md + 挂载目录 + 状态文件的 mtime。

    覆盖四种变更，任何一个都会让指纹变掉、缓存立即失效，所以**不牺牲新鲜度**：
    - 新增 / 删除技能包 → 包目录（`packs`）的 mtime 变；
    - 改某个技能的 SKILL.md（描述 / 环节 / 正文）→ 该文件的 mtime 与大小变；
    - 拨技能开关 → 状态文件的 mtime 变；
    - 动挂载目录 → 挂载项或其 mtime 变。

    ⚠️ 只看包**目录**的 mtime 是不够的：改 SKILL.md 不会动目录 mtime ——
    2026-09-25 实测踩到（指纹判"没变"，于是改了描述也不生效）。
    指纹算不出来（凭空串）时**不缓存**。
    """

    from services.skills import registry, state

    parts: list[str] = []
    try:
        packs_dir = Path(registry.PACKS_DIR)
        parts.append(f"packs={packs_dir.stat().st_mtime_ns if packs_dir.exists() else 0}")
        if packs_dir.is_dir():
            for item in sorted(packs_dir.iterdir()):
                skill_md = item / "SKILL.md"
                if item.is_dir() and skill_md.is_file():
                    info = skill_md.stat()
                    parts.append(f"{item.name}={info.st_mtime_ns}:{info.st_size}")
        state_file = state.state_path()
        parts.append(f"state={state_file.stat().st_mtime_ns if state_file.exists() else 0}")
        for mount in state.mounts():
            mount_dir = Path(mount)
            parts.append(
                f"mount:{mount}={mount_dir.stat().st_mtime_ns if mount_dir.exists() else 0}"
            )
    except Exception as exc:  # noqa: BLE001 - 指纹算不出来就退化为"每次都算"，不能因此少给技能
        logger.warning("技能目录指纹计算失败，本轮不做缓存：%s", exc)
        return ""
    return "|".join(parts)


async def research_state_block(conversation_id: str, conversation: dict[str, Any]) -> str:
    """把「研究链现在到哪了」如实告诉模型 —— **事实块，不是指令**。

    模型需要真实阶段来判断当前授权范围和是否需要用户验收。
    实测（2026-09-26）没有这块时它会**猜错**：把"还没挂链"当成"已经在跑"，
    于是整轮都在做实验准备，而研究者看不到任何阶段分界。

    口径：**程序只报事实**（有没有链、当前第几站、是否已声明普通对话）；
    跑不跑、推不推进仍由模型决定。
    """

    if conversation.get("plain_chat"):
        state = "研究者已声明本对话**不做研究链**（当普通对话用）。"
    else:
        snapshot = await dialog.chain_snapshot(conversation_id)
        if snapshot["chained"]:
            state = (
                f"已经在研究链上，当前节点：**{snapshot['label']}**"
                f"（第 {snapshot['index']}/{snapshot['total']} 步）。"
            )
        else:
            state = "本对话**还没开始研究链**（还没有任何节点跑过）。"

    return (
        "\n\n【当前状态（系统实测，照它说，别猜）】\n"
        f"- {state}\n"
        "- 这是程序查出来的事实；要不要推进、推进到哪，仍由你决定。\n"
    )


async def _chained(conversation_id: str) -> bool:
    """本对话是否已经在研究链上（读不到就按"没有"处理，不阻塞对话）。"""

    return bool((await dialog.chain_snapshot(conversation_id))["chained"])


def skills_system_block() -> str:
    """技能清单（两级披露的**第一级**）：只给名字 + 一句话 + 环节。**带 1 小时缓存。**

    ⚠️ 正文**不能**放进来 —— 40 多个技能的全文会把提示词撑爆，而且大多数跟当前这一步无关。
    模型需要哪个，就 `load_skill` 哪个（那才是第二级）。
    只列**启用**的技能（研究者关掉的不该被模型选中）。
    """

    signature = skills_signature()
    now = time.monotonic()
    if (
        signature
        and _SKILLS_BLOCK_CACHE["signature"] == signature
        and now - float(_SKILLS_BLOCK_CACHE["at"]) < SKILLS_BLOCK_TTL_SECONDS
    ):
        return str(_SKILLS_BLOCK_CACHE["text"])

    text = _build_skills_system_block()
    if signature:
        _SKILLS_BLOCK_CACHE.update({"signature": signature, "at": now, "text": text})
    return text


def _build_skills_system_block() -> str:
    """真正去扫技能库并拼清单（只在缓存未命中时走这里）。"""

    from services.skills import service

    try:
        items = service.prompt_catalog()
    except Exception:  # noqa: BLE001 - 技能库读不出来不该把对话带崩
        return ""
    if not items:
        return ""
    by_stage: dict[str, list[dict[str, str]]] = {}
    for item in items:
        by_stage.setdefault(item["stage"], []).append(item)
    lines = [
        "",
        "",
        "## 你可以调用的「技能」",
        "下面每个技能都是一套已经写好的做法（有的还带脚本）。**先看名字与一句话，判断要不要用**；",
        "要用哪个就用 `load_skill` 把它的完整说明加载进来，再照它做 —— 不要凭名字猜内容。",
        "其中带脚本的技能可以用 `run_skill` 按它声明的流程在研究者电脑上跑（会先请研究者确认）。",
    ]
    for stage, stage_items in by_stage.items():
        lines.append(f"### {stage}")
        for item in stage_items:
            lines.append(f"- {item['name']}：{item['description']}")
    return "\n".join(lines)

#: 通用回答的输出上限：``None`` = **不设限**（2026-09-24 用户口径）。
#: 历史上这里从 1536 放宽到 4096，每次都是同一个症状——思考型模型的推理与正文
#: **共用**这份预算，推理一多吃，正文就被截断（实测有一轮只落了 26 个字符）。
#: 修正方向不是"再放宽一点"，而是**不给上限**。
REPLY_MAX_TOKENS: int | None = None

#: 模型这一跳失败时的**自动重试次数**（用户口径 2026-09-26：「不再允许所有的强制中断」）。
#:
#: 为什么要有它：旧行为是模型这一跳一旦抛错就整轮 `interrupted=true` 收场 ——
#: 研究者拿到的是一个错误码，而不是一个说完的答复。而实测里最常见的那个 400
#: （`reasoning_content ... must be passed back`）**修一下报文就能过**，根本不该判死。
#:
#: 重试只在「这一次尝试还没吐过任何增量」时进行：已经上屏的字不能再来一遍，
#: 否则研究者会看到重复的两段正文。
MODEL_ROUND_MAX_ATTEMPTS = 3


class HomeChatRequest(BaseModel):
    """首页单轮对话入参。"""

    text: str = Field(default="", max_length=4000)
    model_config_id: int
    model_id: str = Field(min_length=1, max_length=200)
    #: 不传 = 新建会话；传了 = 接着这个会话继续（会带上最近若干轮作为上下文）
    conversation_id: str | None = Field(default=None, max_length=64)
    #: 不传 = 未分组；传了 = 这条新会话直接归到该项目下
    project_id: int | None = None
    #: **编辑重开**：把这轮当成"改写第 N 条用户消息"——先丢弃该条及其后的所有轮次，
    #: 再以 ``text`` 作为新的第 N 条重问一次。不传 = 普通追加一轮。
    #: 只接受指向 user 轮次的下标（指到 assistant 轮次直接 422，不做猜测）。
    replace_from: int | None = Field(default=None, ge=0)
    attachments: list[dict[str, Any]] = Field(default_factory=list, max_length=10)


def _turn_timestamp() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds")


def _prepare_home_message(payload: HomeChatRequest) -> tuple[str, Any, list[dict[str, Any]]]:
    """Validate attachments and build the model-facing multimodal user message."""
    text = payload.text.strip()
    try:
        attachment_blocks, display_metadata = normalize_chat_attachments(payload.attachments)
    except ValueError as exc:
        raise _error(422, "invalid_attachment", str(exc)) from exc
    if not text and not attachment_blocks:
        raise _error(422, "empty_text", "请输入内容或添加附件后再发送")
    if not attachment_blocks:
        return text, text, display_metadata
    blocks: list[dict[str, Any]] = []
    if text:
        blocks.append({"type": "text", "text": text})
    blocks.extend(attachment_blocks)
    return text or "已上传附件", blocks, display_metadata


def _previous_attachment_content(
    conversation: dict[str, Any] | None, replace_from: int | None
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    if conversation is None or replace_from is None:
        return [], []
    turns = conversation.get("turns") or []
    if replace_from >= len(turns):
        return [], []
    user_number = sum(1 for turn in turns[:replace_from] if (turn or {}).get("role") == "user")
    for message in conversations.context_messages(conversation):
        if not isinstance(message, dict) or message.get("role") != "user":
            continue
        if user_number:
            user_number -= 1
            continue
        content = message.get("content")
        if not isinstance(content, list):
            break
        blocks = [
            block for block in content
            if isinstance(block, dict)
            and (block.get("type") == "image_url" or (
                block.get("type") == "text"
                and str(block.get("text") or "").startswith("[附件:")
            ))
        ]
        metadata = [item for item in turns[replace_from].get("attachments", []) if isinstance(item, dict)]
        return blocks, metadata
    return [], []


class HomeInterruptRequest(BaseModel):
    turn_id: str | None = None
    conversation_id: str = Field(min_length=1, max_length=64)


class HomeChatResponse(BaseModel):
    title: str
    #: ``model`` = 模型给出；``fallback`` = 标题调用失败后按用户输入截断
    title_source: str
    #: 降级原因（仅在 ``title_source == "fallback"`` 时有值）——**如实回传，不静默**
    title_note: str | None = None
    reply: str
    #: 本次会话 id（后端 JSON 落盘），前端据此接着继续
    conversation_id: str
    project_id: int | None = None
    model_ref: str
    provider: str
    model_id: str
    cost_usd: float | None = None
    cost_unknown_reason: str | None = None
    usage: dict[str, Any] = Field(default_factory=dict)


class CompactChatRequest(BaseModel):
    conversation_id: str = Field(min_length=1, max_length=64)
    model_config_id: int
    model_id: str = Field(min_length=1, max_length=200)


class CompactChatResponse(BaseModel):
    ok: bool
    conversation_id: str
    changed: bool = False
    reason: str | None = None
    checkpoint_id: str | None = None
    summary: str | None = None
    context: dict[str, int] = Field(default_factory=dict)


def _thinking_provider(model_ref: str) -> bool:
    """是不是"思考型"供应商 —— 带 `tool_calls` 的 assistant 消息**必须回传**
    `reasoning_content` 的那种（实测 deepseek 系列缺了直接 400）。
    """

    return "deepseek" in (model_ref or "").lower()


#: 思考型供应商要求 `reasoning_content` **非空**：实测带空串仍被
#: `bad_request: The reasoning_content in the thinking mode must be passed back`
#: （2026-09-25 19:16 复现）—— 所以拿不到真实思考时用一个**占位**。
REASONING_PLACEHOLDER = "（本轮没有输出思考文本）"


def reasoning_echo(reasoning: str, model_ref: str) -> str | None:
    """要回传的 `reasoning_content`；``None`` = 这个供应商不要求这个字段。

    真实思考优先；拿不到时，思考型供应商给**非空占位**（空串不算回传）。
    """

    if reasoning:
        return reasoning
    return REASONING_PLACEHOLDER if _thinking_provider(model_ref) else None


def repair_reasoning_echo(messages: list[dict[str, Any]], model_ref: str) -> int:
    """给缺 `reasoning_content` 的 assistant 消息补上。返回补了几条。

    **发送前先修**，比「等 400 再重发」更直接：请求永远不会以不合规的形状发出去，
    也就不会再把对话打断。

    ⚠️ 2026-09-26 受控实验（`probe_reasoning_rule.py`：四种报文形状各跑一次真实调用）
    发现**旧实现漏了一半**。DeepSeek thinking 模式的真实规则不是「带 `tool_calls` 的
    assistant 要回传」，而是 —— **只要请求里出现带 `tool_calls` 的 assistant 消息，
    请求里所有 assistant 消息都必须带 `reasoning_content`**：

    | 报文形状 | 实测结果 |
    |---|---|
    | 历史 assistant 无该字段 + 工具助理有 | **400 `The reasoning_content … must be passed back`** |
    | 历史 assistant 补上真实思考 | 通过 |
    | 干脆没有那条 assistant | 通过 |
    | 历史 assistant 补**空串** | 通过 |

    旧实现只补带 `tool_calls` 的那些；而历史轮走 `conversations.context_messages()`，
    只回 `role`/`content`（不带思考）→ **批准后续答、以及任何已在进行的多轮工具调用必 400**，
    表现就是整轮被强制中断（`interrupted=true`）。用户 2026-09-26 报的就是这个。

    两档取值，都是实测过的：

    - 带 `tool_calls` 的 assistant：**非空占位**（空串不算回传，2026-09-25 实测）；
    - 其余 assistant（历史里说过的一句话）：**空串**即可 —— 不塞占位句，否则模型会
      以为它当时想的就是那句占位。这一步只对思考型供应商做：非思考型供应商不需要
      这个字段，多传反而可能被拒。
    """

    thinking = _thinking_provider(model_ref)
    if not thinking:
        # 非思考型供应商**根本不需要**这个字段 —— 多传反而可能被当成未知字段拒收。
        # 这条守卫沿用旧实现（有回归用例守着），别为了"统一"拆掉它。
        return 0
    fixed = 0
    for message in messages:
        if str(message.get("role") or "") != "assistant":
            continue
        value = message.get("reasoning_content")
        if isinstance(value, str) and value.strip():
            continue
        if not message.get("tool_calls"):
            # 普通历史轮：补空串即可（实测被接受）。不塞占位句 —— 那会让模型
            # 以为它当时想的就是那句占位；已补过就不重复计数。
            if "reasoning_content" in message:
                continue
            message["reasoning_content"] = ""
            fixed += 1
            continue
        message["reasoning_content"] = REASONING_PLACEHOLDER
        fixed += 1
    return fixed


def _error(status_code: int, code: str, message: str, detail: Any = None) -> HTTPException:
    return HTTPException(
        status_code=status_code,
        detail={"code": code, "message": message, "detail": detail},
    )


def _turn_in_progress_detail(conversation_id: str | None) -> dict[str, Any] | None:
    """Return the durable owner identity so clients can attach instead of resending."""
    if not conversation_id:
        return None
    owner = home_turns.active(conversation_id)
    if owner is None:
        return None
    return {
        "conversation_id": owner.conversation_id,
        "turn_id": owner.turn_id,
        "status": owner.snapshot.get("status", "inProgress"),
        "event_seq": owner.snapshot.get("event_seq", 0),
        "resume_url": f"/api/v1/chat/turns/{owner.conversation_id}/{owner.turn_id}/stream",
    }


def _public_stream_error(exc: BaseException, *, interrupted: bool = False) -> str:
    """Return reader-facing text without exposing provider diagnostics."""

    if interrupted:
        return "这轮回答已停止，已经生成的内容已保留。"
    kind = str(getattr(exc, "kind", "") or "").lower()
    if kind in {"timeout", "rate_limit"}:
        return "模型响应时间较长，请稍后重试。"
    if kind in {"auth", "config", "routing", "model_routing"}:
        return "当前模型暂时不可用，请检查模型配置。"
    if kind in {"bad_request", "validation"}:
        return "当前请求无法由模型处理，请换一种说法。"
    if kind in {"server", "network", "connection"}:
        return "模型服务暂时不可用，已经生成的内容已保留。"
    return "这次回答没有完整完成，已经生成的内容已保留。"


def _sse(event: str, payload: dict[str, Any]) -> str:
    return f"event: {event}\ndata: {json.dumps(payload, ensure_ascii=False)}\n\n"


def _reasoning_sse(update: Any, *, status: str = "streaming") -> str:
    """Serialize reasoning separately from tool rows.

    ``text`` remains the raw provider stream for backwards compatibility;
    ``summary`` is included only when a provider explicitly supplied one.
    """

    payload: dict[str, Any] = {
        "text": str(getattr(update, "text", "") or ""),
        "status": status,
    }
    summary = str(getattr(update, "summary", "") or "").strip()
    if summary:
        payload["summary"] = summary
    return _sse("reasoning", payload)


def _annotate_tool_row(
    row: dict[str, Any], call: dict[str, Any], status: str
) -> dict[str, Any]:
    """Add stable tool identity/status while preserving legacy row fields."""

    function = call.get("function") or {}
    name = str(function.get("name") or "")
    _annotate_tool_identity(row, name, status)
    # The human-readable summary is deliberately localized, but the collapsed
    # activity row must retain the exact command/action from the model call.
    # Recompute it here for every lifecycle row so an end row can never fall
    # back to text such as "在你的电脑上执行命令".
    if name in {"run_command", "run_on_computer", "files_on_computer"}:
        from services.agent import mcp_tools as agent_tools

        action = agent_tools._tool_action(name, agent_tools.arguments_of(call))
        if action:
            row["action"] = action
    call_id = str(call.get("id") or "").strip()
    if call_id:
        row["call_id"] = call_id
    return row


def _annotate_tool_identity(row: dict[str, Any], name: str, status: str) -> dict[str, Any]:
    """Attach canonical identity and lifecycle status to a tool activity row."""

    from services.agent.mcp_tools import TOOL_LABELS

    row["tool_name"] = name
    row["tool_label"] = TOOL_LABELS.get(name, name or "工具")
    row["tool_status"] = {
        "start": "running",
        "ok": "completed",
        "err": "failed",
        "waiting": "waiting_approval",
    }.get(status, status)
    return row


def _repair_tool_call_pairs(
    messages: list[dict[str, Any]],
    replacements: dict[str, str] | None = None,
) -> list[dict[str, Any]]:
    """Return a provider-valid history with every tool call answered exactly once."""

    tool_messages: dict[str, list[dict[str, Any]]] = {}
    for message in messages:
        if message.get("role") != "tool":
            continue
        call_id = str(message.get("tool_call_id") or "")
        if call_id:
            tool_messages.setdefault(call_id, []).append(message)

    repaired: list[dict[str, Any]] = []
    replacement_map = replacements or {}
    for message in messages:
        if message.get("role") == "tool":
            continue
        repaired.append(message)
        if message.get("role") != "assistant" or not message.get("tool_calls"):
            continue
        for call in message.get("tool_calls") or []:
            call_id = str(call.get("id") or "")
            if not call_id:
                continue
            replacement = replacement_map.get(call_id)
            if replacement is not None:
                repaired.append({"role": "tool", "tool_call_id": call_id, "content": replacement})
                tool_messages.pop(call_id, None)
                continue
            existing = tool_messages.get(call_id) or []
            if existing:
                repaired.append(existing.pop(0))
                continue
            repaired.append(
                {
                    "role": "tool",
                    "tool_call_id": call_id,
                    "content": json.dumps(
                        {"ok": False, "error": "这次工具调用没有执行，已安全结束。", "interrupted": True},
                        ensure_ascii=False,
                    ),
                }
            )
    return repaired


async def _workspace_dir(project_id: int | None) -> str | None:
    if project_id is None or AsyncSessionLocal is None:
        return None
    async with AsyncSessionLocal() as session:
        project = (await session.execute(select(Project).where(Project.id == int(project_id)))).scalar_one_or_none()
        settings = project.settings if project is not None and isinstance(project.settings, dict) else {}
        value = settings.get("workspace_dir")
        return str(value) if value else None


def _is_simple_question(text: str) -> bool:
    """Recognize short conversational questions that never need tools or research routing."""

    compact = " ".join((text or "").split())
    if not compact or len(compact) > 80:
        return False
    research_markers = (
        "研究",
        "调研",
        "文献",
        "论文",
        "搜索",
        "检索",
        "文件",
        "目录",
        "命令",
        "运行",
        "创建",
        "写入",
        "读取",
        "分析",
        "总结",
        "知识库",
        "工具",
        "子agent",
        "子 Agent",
    )
    if any(marker in compact for marker in research_markers):
        return False
    return bool(
        re.fullmatch(r"(?:你好|您好|嗨|hello|hi|谢谢|辛苦了|再见)[！!。.!？? ]*", compact, re.I)
        or re.search(r"(?:等于|是多少|是什么|哪个国家|什么颜色|几岁|几点|怎么样|能做什么|你是谁|介绍一下|怎么开始|如何开始|陪我聊|保持简洁|可以开始|你擅长|演示入口|演示模式)", compact)
        or re.fullmatch(r"[\d\s+\-*/().=]+[？?。.!！ ]*", compact)
    )


def _research_gate_blocks_tools(text: str, conversation: dict[str, Any]) -> bool:
    """Return whether this turn must run without model tools.

    The prompt explains the confirmation policy, but the policy also needs a
    deterministic server-side guard so a provider cannot search before the
    user confirms the research scope.
    """

    return intent_mod.defers_research(text) or bool(
        conversation.get("research_confirmation_pending")
    )


_INTERNAL_FAILURE_REPLY = "这次回答没有完整完成，请重新发送；已经完成的内容会保留。"
_INTERNAL_DIAGNOSTIC_REPLACEMENTS = (
    ("all connection attempts failed", "服务连接失败"),
    ("error executing tool", "工具执行失败"),
    ("traceback (most recent call last):", "执行异常："),
    ("unreachable: true", "服务当前不可用"),
    ("unreachable:true", "服务当前不可用"),
    ("unreachable： true", "服务当前不可用"),
    ("unreachable：true", "服务当前不可用"),
)
# Mentioning a tool or a protocol field is ordinary answer content. Only
# explicit diagnostics are cleaned; prose cannot establish executor health.
_INTERNAL_DIAGNOSTIC_RE = re.compile(
    "|".join(re.escape(marker) for marker, _ in _INTERNAL_DIAGNOSTIC_REPLACEMENTS),
    re.IGNORECASE,
)


def _clean_diagnostic_text(text: str) -> str:
    for marker, replacement in _INTERNAL_DIAGNOSTIC_REPLACEMENTS:
        text = re.sub(re.escape(marker), replacement, text, flags=re.IGNORECASE)
    return text


class _DiagnosticTextFilter:
    """Clean diagnostics across deltas without swallowing the answer suffix."""

    def __init__(self) -> None:
        self.pending = ""

    def feed(self, chunk: str, *, final: bool = False) -> str:
        self.pending += chunk
        held = 0
        if not final:
            lower = self.pending.lower()
            for marker, _ in _INTERNAL_DIAGNOSTIC_REPLACEMENTS:
                for size in range(1, min(len(lower), len(marker) - 1) + 1):
                    if lower.endswith(marker[:size]):
                        held = max(held, size)
        boundary = len(self.pending) - held
        for match in _INTERNAL_DIAGNOSTIC_RE.finditer(self.pending):
            if match.start() < boundary < match.end():
                boundary = match.end()
        ready, self.pending = self.pending[:boundary], self.pending[boundary:]
        return _clean_diagnostic_text(ready)
# Some providers put the model's scratchpad in ``delta.content`` instead of
# the dedicated reasoning field.  These phrases are unusually specific to
# that leak (the screenshot that motivated this guard contained most of them)
# and are therefore safer than trying to redact arbitrary first-person text.
_INTERNAL_REASONING_MARKERS_RE = re.compile(
    r"the user asks|per instructions|i should|should i\b|"
    r"don't reveal (?:the )?thinking|do not reveal (?:the )?thinking|"
    r"let me (?:organize|think|check)|we need to answer|"
    r"用户(?:要求|只问了)|按(?:照)?指令|不要透露思考|"
    r"我(?:需要|应该)回答|让我(?:想想|整理)",
    re.IGNORECASE,
)


def _looks_like_internal_reasoning(text: str) -> bool:
    """Return whether visible text is a provider scratchpad, not an answer.

    A single phrase can occur in a legitimate answer, so require at least two
    markers once the text has enough context.  The start-of-scratchpad forms
    are allowed with one marker because they are unambiguous and let the
    streaming caller stop before more private text reaches the browser.
    """

    value = " ".join(str(text or "").split())
    if len(value) < 36:
        return False
    matches = _INTERNAL_REASONING_MARKERS_RE.findall(value)
    if len(matches) >= 2:
        return True
    return bool(
        re.match(
            r"^(?:the user asks|per instructions|should i\b|用户(?:要求|只问了)|按(?:照)?指令)",
            value,
            re.IGNORECASE,
        )
    )

def _sanitize_assistant_content(content: str) -> str:
    """净化明确诊断，保留工具介绍和诊断之后的正常正文。"""

    text = _clean_diagnostic_text(str(content or ""))
    reasoning_match = _INTERNAL_REASONING_MARKERS_RE.search(text)
    if not _looks_like_internal_reasoning(text):
        return text
    match_start = reasoning_match.start() if reasoning_match is not None else len(text)
    prefix = text[:match_start].rstrip()
    # A provider may return only its private scratchpad and no user-facing
    # answer. Persist a short, actionable message instead of an empty turn;
    # an empty assistant item makes the completed response appear to vanish.
    return prefix or _INTERNAL_FAILURE_REPLY


def _stream_history(state: dict[str, Any]) -> list[dict[str, Any]]:
    history = conversations.protocol_messages(state.get("agent_history") or [])
    offset = state.get("partial_text_from")
    if isinstance(offset, int):
        partial = _sanitize_assistant_content("".join((state.get("text") or [])[offset:]))
        if partial:
            history.append({"role": "assistant", "content": partial})
    return history


def _save_stream_progress(
    conversation: dict[str, Any], *, base_turns: list[dict[str, Any]], text: str,
    state: dict[str, Any], rows: list[dict[str, Any]], approvals: list[dict[str, Any]],
    model_id: str, duration_ms: int, interrupted: bool = False,
) -> None:
    """Checkpoint a single turn in place; never duplicate it on completion."""
    if conversation.get("id"):
        latest = conversations.read(str(conversation["id"]))
        if latest is not None and "grants" in latest:
            # Access switches may change while the model streams. A checkpoint
            # must not overwrite that newer explicit user choice with old grants.
            conversation["grants"] = latest["grants"]
    pending = state.get("pending")
    # A stream can be checkpointed after the approval endpoint has consumed
    # the request.  Never persist a closed request as if this turn still
    # needed a decision; doing so leaves the UI blocked after a refresh.
    if isinstance(pending, dict) and str(pending.get("status") or "pending") != "pending":
        pending = None
        state["pending"] = None
    conversation["agent_history"] = _stream_history(state)
    conversation["turns"] = [*base_turns, {"role": "user", "content": text,
        "ts": state.get("user_ts"), "attachments": state.get("attachments") or []}, {
        "role": "assistant", "content": _sanitize_assistant_content("".join(state["text"])),
        "reasoning": "".join(state["reasoning"]) or None,
        "reasoning_summary": "".join(state.get("reasoning_summary") or []) or None,
        "model_id": model_id, "duration_ms": duration_ms, "rows": list(rows),
        "approvals": list(approvals), "awaiting_approval": pending["id"] if pending else None,
        "interrupted": interrupted, "interruption_reason": state.get("terminal_reason") if interrupted else None,
    }]
    conversations.write(conversation)


def _sanitize_compaction_summary(summary: str) -> str:
    """Keep compaction useful while removing protocol and transport diagnostics."""

    text = str(summary or "")
    text = re.sub(r"`?(?:run_on_computer|files_on_computer)`?", "本地执行服务", text, flags=re.IGNORECASE)
    text = re.sub(r"unreachable\s*[:：]\s*true", "当前不可用", text, flags=re.IGNORECASE)
    text = re.sub(r"(?:All connection attempts failed|连不上执行器[^。\n]*)", "本地执行服务暂时不可用", text, flags=re.IGNORECASE)
    text = re.sub(r"\b(?:tool_call_id|request_id|reasoning_content)\b", "内部字段", text, flags=re.IGNORECASE)
    return text


async def _agent_loop(
    messages_now: list[dict[str, Any]],
    *,
    conversation_id: str,
    ref: str,
    tool_defs: list[dict[str, Any]],
    rows: list[dict[str, Any]],
    approvals_out: list[dict[str, Any]],
    state: dict[str, Any],
    allow_fallback_first: bool = True,
    workspace_root: str | None = None,
    max_attempts: int | None = None,
) -> AsyncIterator[str]:
    """agent 循环：模型 → 工具 → 结果回喂 → 再模型（轮数有上限，防死循环）。

    每一次工具调用都走同一道裁决（`agent_tools.judge` → `policy`），再叠上**本对话的授权**：

    - 硬拒（删代码这类）：直接回绝，不进批准队列（没得商量）；
    - 无害（列目录 / 读文件 / 查论文库）：直接做 —— 它不改变任何状态；
    - 动手（跑命令 / 写文件 / 删东西）：
      · 本对话选了「默认允许执行」（高危也放行）→ 直接做；
      · 开了「完全访问模式」且不是高危 → 直接做；
      · 否则 → **发一张批准卡，一次都不执行**。模型可以提出，但没有资格替自己批准。

    主对话与批准后续答共用本函数。**状态通过 `state` 写回**（`text` / `reasoning` /
    `result` / `pending`），而不是靠生成器返回值 —— 调用方在两处需要同一份口径，
    复制一份出来迟早会分叉。
    """

    from services.agent import mcp_tools as agent_tools

    # 两个**独立**计数器（用户口径 2026-09-25）：模型调用 600 次、工具调用 500 次。
    # 之前只按「轮」算（3 轮），复杂任务会在第 4 轮被静默截断。
    model_calls = 0
    tool_calls_done = 0
    attempt_limit = max(1, int(max_attempts or MODEL_ROUND_MAX_ATTEMPTS))
    #: 非空 = 因触顶而停（值说明哪一边触的顶），用于触发收尾交代
    stopped_by: str | None = None

    from services import context_compaction as compaction_mod
    from services import context_meter

    meter = context_meter.get_meter()
    compactions: list[dict[str, Any]] = state.setdefault("compactions", [])
    snapshots: list[dict[str, Any]] = state.setdefault("compaction_snapshots", [])
    reasoning_summaries: list[str] = state.setdefault("reasoning_summary", [])
    state["agent_history"] = conversations.protocol_messages(messages_now)

    def save_history() -> None:
        # Keep the dynamic system prompt out of the durable history.  It is
        # rebuilt on every request so changed skills/project state take effect.
        state["agent_history"] = conversations.protocol_messages(messages_now)
        state["partial_text_from"] = None

    def checkpoint(
        kind: str,
        record: dict[str, Any],
        before_messages: list[dict[str, Any]] | None = None,
    ) -> None:
        checkpoint_id = f"cp-{time.time_ns()}"
        record["checkpoint_id"] = checkpoint_id
        record["trigger"] = "auto"
        snapshots.append(
            {
                "id": checkpoint_id,
                "created_at": time.time(),
                "reason": kind,
                "messages": conversations.protocol_messages(before_messages or messages_now),
            }
        )

    while model_calls < agent_tools.MAX_MODEL_CALLS_PER_TURN:
        model_calls += 1
        # ---------------------------------------------------------------- #
        # 预算判定（用户口径 2026-09-25）：调模型**前**看一眼，超了**先压工具结果**。
        # 压的是"历史里较早的结果"，与新结果回喂时"不截断"是两件事（见模块文档）。
        # 压缩只影响**这次请求的模型视图**，会话文件里的原始轮次一字不动。
        # 压不动了（已无可压的工具结果）也不停：如实记一条日志，继续往下走。
        # ---------------------------------------------------------------- #
        projected = meter.project(messages_now, tools=tool_defs)
        if projected > meter.limit_tokens:
            before = conversations.protocol_messages(messages_now)
            outcome = compaction_mod.compress_tool_results(messages_now)
            if outcome.changed:
                budget_row = {
                    "kind": "system",
                    "tone": "warn",
                    "text": (
                        f"上下文已达 {projected:,} / {meter.limit_tokens:,} token："
                        f"已压缩 {outcome.count} 条较早的工具结果"
                        f"（释放约 {outcome.freed_chars:,} 字符）后继续。"
                    ),
                }
                rows.append(budget_row)
                yield _sse("row", {"row": budget_row})
                compacted = outcome.as_record()
                checkpoint("tool_results", compacted, before)
                compacted["before_message_count"] = len(before)
                compactions.append(compacted)
                save_history()
                logger.info(
                    "上下文压缩 conversation=%s count=%d freed_chars=%d projected=%d",
                    conversation_id,
                    outcome.count,
                    outcome.freed_chars,
                    projected,
                )
            else:
                logger.warning(
                    "上下文 %d 已超预算 %d，但已无可压缩的工具结果",
                    projected,
                    meter.limit_tokens,
                )
            # 第二步：压完工具结果**仍超预算** → 摘要早期轮次（这一步要花一次模型调用，
            # 所以排在后面；失败就保持原样继续 —— 口径是"不因超窗停止"）。
            if meter.project(messages_now, tools=tool_defs) > meter.limit_tokens:
                span = compaction_mod.select_summary_span(messages_now)
                # ⚠️ 这一步**本身是一次模型调用**，所以它会失败（供应商 400/超时/额度）。
                # 口径是「不因超窗停止」→ 摘要失败也必须**保持原样继续**，
                # 不能让它把整轮拖成强制中断（用户口径 2026-09-26）。
                try:
                    summary_record = (
                        await compaction_mod.summarize_early_turns(
                            messages_now, span=span, model_ref=ref
                        )
                        if span is not None
                        else None
                    )
                except Exception as exc:  # noqa: BLE001 - 摘要只是优化，失败不该拖垮对话
                    logger.warning("上下文摘要失败，保持原样继续：%s", exc)
                    summary_record = None
                if summary_record is not None and span is not None:
                    before = conversations.protocol_messages(messages_now)
                    freed_chars = compaction_mod.apply_turn_summary(
                        messages_now, span=span, summary=str(summary_record["summary"])
                    )
                    budget_row = {
                        "kind": "system",
                        "tone": "warn",
                        "text": (
                            f"上下文仍超预算：已把最早的 {summary_record['count']} 条对话"
                            f"摘要化（释放约 {freed_chars:,} 字符）后继续。"
                        ),
                    }
                    rows.append(budget_row)
                    yield _sse("row", {"row": budget_row})
                    checkpoint("turns", summary_record, before)
                    summary_record["before_message_count"] = len(before)
                    compactions.append(summary_record)
                    save_history()
                    logger.info(
                        "上下文摘要 conversation=%s count=%d freed_chars=%d",
                        conversation_id,
                        summary_record["count"],
                        freed_chars,
                    )
                elif not outcome.changed:
                    logger.warning(
                        "上下文超预算但无可压缩内容（工具结果与早期轮次都没有可动的），继续执行"
                    )
        # 发送前先把消息修好（用户口径 2026-09-25：400 修法 A+C；2026-09-26 补全规则）：
        # **所有**缺 reasoning_content 的 assistant 消息都补上，而不只是带 tool_calls 的那些 ——
        # 这样就不会再因协议字段被供应商拒收而**打断"让模型重新决定"**。
        repaired = repair_reasoning_echo(messages_now, ref)
        if repaired:
            logger.info("补了 %d 条 assistant 消息的 reasoning_content 后继续", repaired)
        estimate_before = meter.estimate(messages_now, tools=tool_defs)
        round_result: Any = None
        #: 这一轮的思考过程从 `state["reasoning"]` 的哪个下标开始 —— 用于把**本轮**的
        #: reasoning_content 跟着 assistant.tool_calls 一起回喂（见下面 append 处）。
        reasoning_from = len(state["reasoning"])
        text_from = len(state["text"])
        state["partial_text_from"] = text_from
        # ---------------------------------------------------------------- #
        # 「不再强制中断」（用户口径 2026-09-26）：模型这一跳失败**不再直接判死整轮**。
        # 先自愈（把报文形状修好）再重试；重试仍不成，就把失败如实摊开、
        # 让这一轮**正常收尾**（由调用方落成一句说完的话），而不是甩一个错误码就断开。
        # ⚠️ 只有在「这一次尝试还没吐过任何增量」时才重试 —— 已经上屏的字不能再来一遍。
        # ---------------------------------------------------------------- #
        attempt = 0
        while True:
            attempt += 1
            emitted_before = len(state["text"])
            internal_text_blocked = False
            diagnostic_filter = _DiagnosticTextFilter()
            try:
                async for update in adapter.chat_stream(
                    messages_now,
                    model_ref=ref,
                    purpose="home_reply",
                    max_tokens=REPLY_MAX_TOKENS,
                    # 首轮与原有行为一致；**工具轮回喂时关掉降级**：实测降级链会切到
                    # 没配 key 的供应商，把真正的 `bad_request` 掩盖成一句无关的
                    # 「env 未配置 API Key」。宁可如实报第一跳的错，也不要换一家继续跑。
                    #
                    # 批准后的续答同理：它**不是**用户的第一句，`allow_fallback_first=False`
                    # 才能把真因如实报出来（实测这里真因是"assistant 的 tool_calls 没带
                    # reasoning_content"，被降级链掩盖成了一句无关的 auth 错误）。
                    allow_fallback=allow_fallback_first and model_calls == 1,
                    tools=tool_defs or None,
                ):
                    if update.kind == "delta":
                        # A few gateways incorrectly label scratchpad text as
                        # normal content.  Stop forwarding it as soon as the
                        # distinctive combination appears; otherwise the UI
                        # shows a long private monologue and then replaces it
                        # during final sanitization.
                        if internal_text_blocked:
                            state["reasoning"].append(update.text)
                            yield _sse("reasoning", {"text": update.text})
                            continue
                        clean_delta = diagnostic_filter.feed(update.text)
                        if not clean_delta:
                            continue
                        state["text"].append(clean_delta)
                        visible = "".join(state["text"])
                        reasoning_match = _looks_like_internal_reasoning(visible)
                        if reasoning_match:
                            # Scratchpad is private; ordinary tool descriptions
                            # and diagnostics must never stop further deltas.
                            internal_text_blocked = True
                            match_start = max(0, len(visible) - len(clean_delta))
                            prefix = visible[:match_start].rstrip()
                            state["text"][:] = [prefix] if prefix else []
                            state["reasoning"].append(visible[match_start:])
                            yield _sse("reasoning", {"text": visible[match_start:]})
                        else:
                            yield _sse("delta", {"text": clean_delta})
                    elif update.kind == "reasoning":
                        # **思考过程走独立通道**：它不是答复。前端折叠展示在耗时那一行下面，
                        # 落盘也单独存一个字段。此前它只被收集、最后被塞进 content 冒充正文，
                        # 结果「界面上看到的回答」和「库里存的」不是同一个东西。
                        state["reasoning"].append(update.text)
                        summary = str(getattr(update, "summary", "") or "").strip()
                        if summary:
                            reasoning_summaries.append(summary)
                        yield _reasoning_sse(update)
                    elif update.kind == "done":
                        round_result = update.result
                if not internal_text_blocked:
                    tail = diagnostic_filter.feed("", final=True)
                    if tail:
                        state["text"].append(tail)
                        yield _sse("delta", {"text": tail})
                break
            except LLMError as exc:
                if not internal_text_blocked:
                    tail = diagnostic_filter.feed("", final=True)
                    if tail:
                        state["text"].append(tail)
                        yield _sse("delta", {"text": tail})
                emitted = len(state["text"]) > emitted_before
                # 自愈：实测最常见的 400（reasoning_content 没回传）修完就能过。
                healed = repair_reasoning_echo(messages_now, ref) if not emitted else 0
                logger.warning(
                    "模型这一跳失败（第 %d/%d 次，已上屏=%s，自愈修了 %d 条）：%s",
                    attempt,
                    attempt_limit,
                    emitted,
                    healed,
                    exc,
                )
                if attempt < attempt_limit and not emitted:
                    retry_row = {
                        "kind": "system",
                        "tone": "warn",
                        "text": "模型这一跳没有完成，正在自动修正请求并重试本轮回答。",
                    }
                    rows.append(retry_row)
                    yield _sse("row", {"row": retry_row})
                    continue
                # 不再抛给外层（抛出去 = 整轮 `interrupted` 强制中断）：
                # 如实记下，交给调用方落成一句说完的话。
                state["soft_failure"] = str(exc)
                break
            except Exception as exc:  # noqa: BLE001 - provider 边界异常也必须可恢复
                # 传输层通常会把网络错误归一为 LLMError，但第三方适配器或自定义
                # provider 仍可能直接抛出 TimeoutError/协议解析异常。它们不应绕过
                # 同一套重试与模型收尾路径，否则用户会看到半截正文后直接中断。
                if not internal_text_blocked:
                    tail = diagnostic_filter.feed("", final=True)
                    if tail:
                        state["text"].append(tail)
                        yield _sse("delta", {"text": tail})
                emitted = len(state["text"]) > emitted_before
                logger.warning(
                    "模型这一跳出现未归类异常（第 %d/%d 次，已上屏=%s）：%s",
                    attempt,
                    attempt_limit,
                    emitted,
                    exc,
                )
                if attempt < attempt_limit and not emitted:
                    retry_row = {
                        "kind": "system",
                        "tone": "warn",
                        "text": "模型连接没有完成，正在自动重试本轮回答。",
                    }
                    rows.append(retry_row)
                    yield _sse("row", {"row": retry_row})
                    continue
                # 不把第三方异常原样暴露给前端；交给 finally 中的 phrasing.say
                # 生成面向研究者的正文收尾，保持与 LLMError 路径一致。
                state["soft_failure"] = f"{type(exc).__name__}: {exc}"
                break

        if state.get("soft_failure"):
            # The failure is an implementation detail.  The caller asks the
            # model for a reader-facing recovery paragraph; exposing a fixed
            # backend warning here made the UI look like a debug console and
            # could be mistaken for the assistant's answer.
            break

        if round_result is not None:
            state["result"] = round_result
            # 真实 prompt_tokens 是**唯一可信**的用量：拿它校正估算比例
            # （Cherry 用事件溯源做"真实基准 + 估算增量"，我们用比例校正达到同样效果）。
            usage = getattr(round_result, "usage", None)
            prompt_tokens = int(getattr(usage, "prompt_tokens", 0) or 0)
            if prompt_tokens:
                meter.calibrate(prompt_tokens=prompt_tokens, estimated=estimate_before)
            # Some providers send reasoning only in the final message rather
            # than as deltas. Surface it through the same independent channel
            # without copying it into the tool activity rows.
            raw_result = getattr(round_result, "raw", None) or {}
            final_reasoning = str(raw_result.get("reasoning") or "").strip()
            if final_reasoning and not state["reasoning"][reasoning_from:]:
                state["reasoning"].append(final_reasoning)
                final_summary = str(raw_result.get("reasoning_summary") or "").strip()
                if final_summary:
                    reasoning_summaries.append(final_summary)
                payload = {"text": final_reasoning, "status": "completed"}
                if final_summary:
                    payload["summary"] = final_summary
                yield _sse("reasoning", payload)
        calls = agent_tools.normalize_tool_calls(getattr(round_result, "tool_calls", None) or [])
        if not calls:
            # A completed assistant message is part of the resumable protocol
            # history even when the model did not request a tool.
            content = str(getattr(round_result, "content", "") or "")
            if not content:
                content = "".join(state["text"][text_from:])
            assistant_message: dict[str, Any] = {
                "role": "assistant",
                "content": content,
            }
            reasoning = "".join(state["reasoning"][reasoning_from:]).strip()
            reasoning_value = reasoning_echo(reasoning, ref)
            if reasoning_value is not None:
                assistant_message["reasoning_content"] = reasoning_value
            messages_now.append(assistant_message)
            save_history()
            break  # 模型不再要工具 = 这一轮答完了
        if tool_calls_done >= agent_tools.MAX_TOOL_CALLS_PER_TURN:
            stopped_by = "tool_calls"
            break

        # 模型要求调工具：先把这一轮如实记进消息（含它已说的话），再逐个处理。
        #
        # ⚠️ **带 tool_calls 的 assistant 消息必须把本轮 reasoning_content 一起回传**：
        # 思考型供应商（实测 deepseek 系列）缺了它直接 400 ——
        # `The reasoning_content in the thinking mode must be passed back to the API`。
        # 2026-09-22 修：这里原先只拼 content/tool_calls，于是「批准后续答里模型再提一条命令」
        # 的**第二轮必然 400** —— 表现是批准卡点完之后整轮中断（`interrupted=true` 落盘），
        # 批准链在第一次工具回合后就断掉。批准路径手拼的那条消息早先已补上该字段，
        # 但循环自己 append 的这一条漏了 —— 补丁只补了一半。
        round_reasoning = "".join(state["reasoning"][reasoning_from:]).strip()
        if not round_reasoning:
            # 有的供应商只在收尾帧给思考过程（delta 为空）→ 从原始结果兜底取
            round_reasoning = str((getattr(round_result, "raw", None) or {}).get("reasoning") or "").strip()
        assistant_call = agent_tools.assistant_tool_message(
            getattr(round_result, "content", "") or "", calls
        )
        # 思考型供应商要求该字段**非空**（空串不算回传，实测仍 400）。
        reasoning_value = reasoning_echo(round_reasoning, ref)
        if reasoning_value is not None:
            assistant_call["reasoning_content"] = reasoning_value
        messages_now.append(assistant_call)
        save_history()

        stopped_for_approval = False
        for call in calls:
            tool_calls_done += 1
            tool_name = str((call.get("function") or {}).get("name") or "")
            # 逐次裁决（不是按工具名一刀切）：同一句删除命令，删研究数据是"待批准"，
            # 删 SciLoop 自己的代码是"直接拒绝"，读文件则根本不用打扰研究者。
            verdict = await agent_tools.judge(call)
            grants_now = agent_approvals.grants(
                agent_approvals.load(conversation_id) or {}
            )
            if verdict.forbidden:
                # **没有商量余地的事不进批准队列**：直接拒绝，把原因如实回给模型，
                # 并落一行过程行 —— 研究者看得到"它想干什么、为什么被挡"。
                refused = {"ok": False, "error": verdict.message, "refused": True}
                row = _annotate_tool_row(
                    agent_tools.tool_row(call, "err", verdict.message), call, "err"
                )
                rows.append(row)
                yield _sse("row", {"row": row})
                messages_now.append(
                    {
                        "role": "tool",
                        "tool_call_id": str(call.get("id") or ""),
                        "content": agent_tools.tool_message_content(refused),
                    }
                )
                continue
            if verdict.needs_approval or not verdict.harmless:
                # 本对话当前的授权（**每轮重读**：研究者中途拨开关要立刻生效）
                high_risk = verdict.needs_approval
                # 两种执行语义：普通模式逐次人工批准，完全访问模式自动批准；执行范围一致。
                allowed_by_grant = agent_approvals.allows(
                    grants_now, high_risk=high_risk, tool=tool_name
                )
                if not allowed_by_grant:
                    # 动手类：**只建请求，不执行**。连一次执行都不发出去。
                    arguments = agent_tools.arguments_of(call)
                    cwd = arguments.get("cwd")
                    request = agent_approvals.new_request(
                        tool=tool_name,
                        args=arguments,
                        cwd=cwd if isinstance(cwd, str) else None,
                        call_id=str(call.get("id") or "") or None,
                    )
                    approvals_out.append(request)
                    # **行即卡片**：卡片本身就是那一条过程行。同一份内容既发 row（用来落盘，
                    # 刷新后凭它重建）也发 approval（前端据此渲染按钮）——
                    # 只发 SSE 不落盘的话，刷新后卡片刻凭空消失，而库里留着一句"等待批准"。
                    card = agent_tools.approval_row(request)
                    card["tool_name"] = tool_name
                    card["tool_label"] = agent_tools.TOOL_LABELS.get(tool_name, tool_name or "工具")
                    card["tool_status"] = "waiting_approval"
                    rows.append(card)
                    yield _sse("row", {"row": card})
                    yield _sse("approval", card)
                    state["pending"] = request
                    stopped_for_approval = True
                    break
                # 已获授权：这一行如实写明"是按你在本对话里的授权直接执行的"，
                # 让研究者事后对得上账（而不是看到一次没人批准的执行）。
                start_detail = "按你在这个对话里的授权直接执行（无需再确认）"
                grant_token = agent_approvals.issue_token(f"grant-{conversation_id[:8]}")
            else:
                start_detail = ""
                grant_token = None
            start_row = _annotate_tool_row(
                agent_tools.tool_row(call, "start", start_detail), call, "start"
            )
            rows.append(start_row)
            yield _sse("row", {"row": start_row})
            payload_out, summary = await agent_tools.run_tool_call(
                call, approval_token=grant_token,
                access_mode="full" if grants_now.get(agent_approvals.GRANT_FULL_ACCESS) else "workspace",
                workspace_root=workspace_root,
            )
            end_status = "ok" if payload_out.get("ok") else "err"
            end_row = _annotate_tool_row(agent_tools.tool_row(
                call,
                end_status,
                summary,
                # 搜索结果挂在这一行上，界面才能画折叠面板（行随会话落盘）
                search=agent_tools.search_row_payload(
                    str((call.get("function") or {}).get("name") or ""),
                    _tool_arguments(call),
                    (payload_out.get("result") or {}) if isinstance(payload_out.get("result"), dict) else {},
                ),
            ), call, end_status)
            end_row.update(agent_tools.tool_execution_details(payload_out))
            rows.append(end_row)
            yield _sse("row", {"row": end_row})
            messages_now.append(
                {
                    "role": "tool",
                    "tool_call_id": str(call.get("id") or ""),
                    "content": agent_tools.tool_message_content(payload_out),
                }
            )
            save_history()
        if stopped_for_approval:
            # 等研究者裁决：这一轮到此为止，绝不"先跑了再补批准"
            break
    else:
        stopped_by = "model_calls"

    if stopped_by is not None:
        # 触顶**不静默**（用户口径 2026-09-25）：先落一行过程行如实告知（随会话落盘），
        # 再给模型**最后一次收尾机会** —— 这次调用不计入上限、且不摆工具，
        # 让它产出「已完成 / 未完成 / 建议下一步」。
        limit_row = {
            "kind": "system",
            "tone": "warn",
            "text": (
                "已达本次回答的执行上限"
                f"（模型调用 {model_calls} 次 / 工具调用 {tool_calls_done} 次）"
                "，正在汇总已完成与未完成的工作。"
            ),
        }
        rows.append(limit_row)
        yield _sse("row", {"row": limit_row})
        messages_now.append({"role": "user", "content": WRAP_UP_PROMPT})
        async for update in adapter.chat_stream(
            messages_now,
            model_ref=ref,
            purpose="home_wrap_up",
            max_tokens=REPLY_MAX_TOKENS,
            allow_fallback=False,
            tools=None,  # 收尾不再动手，只要它把话说清楚
        ):
            if update.kind == "delta":
                state["text"].append(update.text)
                yield _sse("delta", {"text": update.text})
            elif update.kind == "reasoning":
                state["reasoning"].append(update.text)
                summary = str(getattr(update, "summary", "") or "").strip()
                if summary:
                    reasoning_summaries.append(summary)
                yield _reasoning_sse(update)
            elif update.kind == "done":
                state["result"] = update.result
        wrap_content = str(getattr(state.get("result"), "content", "") or "")
        if not wrap_content:
            wrap_content = "".join(state["text"][-1:])
        if wrap_content:
            messages_now.append({"role": "assistant", "content": wrap_content})
        save_history()


def _streaming(source: AsyncIterator[str]) -> StreamingResponse:
    """统一的 SSE 响应包装（对话各条分支共用同一组响应头）。"""

    return StreamingResponse(
        source,
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache, no-transform", "Connection": "keep-alive"},
    )


async def _resolve_model_ref(model_config_id: int, model_id: str) -> str:
    """校验供应商与模型登记，返回 ``provider:model_id``。"""
    record = await get_registry().get_config(model_config_id)
    if record is None:
        raise _error(
            404,
            "config_not_found",
            f"供应商 {model_config_id} 不存在",
            {"model_config_id": model_config_id},
        )
    ref = f"{slugify_provider(record.name)}:{model_id}"
    if not any(entry.get("model_id") == model_id for entry in (record.models or [])):
        raise _error(
            409,
            "no_model",
            f"供应商「{record.name}」未登记模型 {model_id}",
            {"model_ref": ref},
        )
    return ref


async def _generate_title(text: str, ref: str) -> tuple[str, str, str | None]:
    """生成标题。返回 ``(title, title_source, title_note)``；失败只降级，不抛。"""
    title = text[:TITLE_MAX_CHARS]
    title_source = "fallback"
    title_note: str | None = None
    try:
        title_result = await adapter.chat(
            [
                # ⚠️ 必须有 system 约束：只有 user 提示时，爱"自言自语"的模型
                # 会把推理写进正文，第一行就被当标题存下来（2026-09-22 实测 17 例）
                {"role": "system", "content": conversations.TITLE_SYSTEM},
                {"role": "user", "content": TITLE_PROMPT.format(text=text)},
            ],
            model_ref=ref,
            max_tokens=TITLE_MAX_TOKENS,
            temperature=0.2,
            purpose="home_title",
            allow_fallback=False,
            strict_logging=False,
        )
        candidate = conversations.pick_title_line(
            title_result.content or "", max_chars=TITLE_MAX_CHARS
        )
        if candidate:
            title = candidate[:TITLE_MAX_CHARS]
            title_source = "model"
        elif (title_result.content or "").strip():
            # 有正文但不含标题（整段是思考过程）：如实说明，用需求前 20 字兜底
            title_note = "模型返回的是思考过程而不是标题，已沿用需求前 20 字"
        else:
            title_note = "模型未给出标题（返回空正文），已沿用需求前 20 字"
    except Exception as exc:  # noqa: BLE001 - 标题是锦上添花，任何失败都降级
        logger.warning("首页标题生成失败，降级为用户输入截断：%s", exc)
        title_note = f"标题生成失败：{exc}"[:200]
    return title, title_source, title_note


def _truncate_for_edit(conversation: dict[str, Any] | None, replace_from: int | None) -> bool:
    """``replace_from`` 非空 = 编辑重开：校验下标并丢弃该条及其后的所有轮次（不落盘）。

    返回 True 表示本次走"编辑重开"（调用方据此决定：① 不重新生成标题；② 即使一个字都没生成，
    也要把用户改过的内容落盘 —— 用户改了就一定留痕）。

    校验从严、不做猜测：会话不存在 → 404；下标越界 → 422；指到 assistant 轮次 → 422。
    """
    if replace_from is None:
        return False
    if conversation is None:
        raise _error(404, "conversation_not_found", "要编辑的会话不存在（可能已被删除）")
    turns = conversation.get("turns") or []
    if replace_from >= len(turns):
        raise _error(
            422,
            "turn_index_out_of_range",
            f"第 {replace_from} 条消息不存在（当前共 {len(turns)} 条）",
        )
    if (turns[replace_from] or {}).get("role") != "user":
        raise _error(422, "not_a_user_turn", "只能编辑你自己发过的消息")
    last_user_index = max(
        (index for index, turn in enumerate(turns) if (turn or {}).get("role") == "user"),
        default=-1,
    )
    if replace_from != last_user_index:
        raise _error(422, "only_latest_user_turn_editable", "只能编辑最后一条用户消息")
    user_turn_index = sum(1 for turn in turns[:replace_from] if (turn or {}).get("role") == "user")
    conversations.truncate_agent_history_from_user(conversation, user_turn_index)
    conversations.truncate_from(conversation, replace_from)
    return True


@router.post(
    "/chat/home",
    response_model=HomeChatResponse,
    summary="首页对话：生成标题 + 正式回答（需 X-Owner-Token）",
    dependencies=[Depends(require_owner)],
)
async def home_chat(payload: HomeChatRequest) -> HomeChatResponse:
    async with home_turns.write_lock(payload.conversation_id):
        if payload.conversation_id and home_turns.active(payload.conversation_id):
            raise _error(409, "turn_in_progress", "当前对话仍在运行")
        text, user_content, attachment_metadata = _prepare_home_message(payload)
        user_ts = _turn_timestamp()

        ref = await _resolve_model_ref(payload.model_config_id, payload.model_id)

        conversation = conversations.read(payload.conversation_id) if payload.conversation_id else None
        prior_attachment_blocks, prior_attachment_metadata = _previous_attachment_content(
            conversation, payload.replace_from
        )
        if prior_attachment_blocks and not attachment_metadata:
            user_content = ([{"type": "text", "text": text}] if text else []) + prior_attachment_blocks
            attachment_metadata = prior_attachment_metadata
        # 编辑重开：先校验并截断（会话不存在/下标越界都不会先建出空会话）
        is_edit = _truncate_for_edit(conversation, payload.replace_from)

        # 1) 标题：失败只降级，不让整次请求失败。编辑重开不动标题 —— 用户改的是正文，
        #    这次会话的主题没变（也避免"编辑一下标题就换了"）。
        if is_edit and conversation is not None:
            title = str(conversation.get("title") or text[:TITLE_MAX_CHARS])
            title_source, title_note = "kept", None
        else:
            title, title_source, title_note = await _generate_title(text, ref)

        # 2) 正式回答：失败必须如实抛出（不能伪装成功）
        #    带上下文：同一会话的历史轮次（实现「接着上次继续」；编辑重开后即为截断后的历史）
        history = (
            _repair_tool_call_pairs(conversations.context_messages(conversation))
            if conversation
            else []
        )
        request_messages = [
            {"role": "system", "content": reply_system_prompt(conversation) + skills_system_block()},
            *history,
            {"role": "user", "content": user_content},
        ]
        try:
            reply_result = await adapter.chat(
                request_messages,
                model_ref=ref,
                purpose="home_reply",
                allow_fallback=False,
            )
        except LLMError as exc:
            raise _error(
                status.HTTP_502_BAD_GATEWAY,
                getattr(exc, "code", "llm_failed") or "llm_failed",
                str(exc),
                {"model_ref": ref, "type": type(exc).__name__},
            ) from exc

        # 3) 落盘：新会话用模型给的标题建，已知会话则只补这一轮（编辑重开时标题保持不变）
        if conversation is None:
            workspace_dir = await _workspace_dir(payload.project_id)
            conversation = conversations.create(
                title=title,
                model_ref=ref,
                project_id=payload.project_id,
                workspace_dir=workspace_dir,
            )
        elif not is_edit:
            conversations.rename(str(conversation["id"]), title)
        reply_content = _sanitize_assistant_content(reply_result.content or "")
        raw_reasoning = str((getattr(reply_result, "raw", None) or {}).get("reasoning") or "").strip()
        reasoning_summary = str(
            (getattr(reply_result, "raw", None) or {}).get("reasoning_summary") or ""
        ).strip()
        conversations.append_turns(
            conversation,
            [
                {
                    "role": "user",
                    "content": text,
                    "ts": user_ts,
                    "attachments": attachment_metadata,
                },
                {
                    "role": "assistant",
                    "content": reply_content,
                    "model_id": reply_result.model_id,
                    "duration_ms": reply_result.duration_ms,
                    "reasoning": raw_reasoning or None,
                    "reasoning_summary": reasoning_summary or None,
                    "ts": _turn_timestamp(),
                },
            ],
        )
        assistant_message: dict[str, Any] = {
            "role": "assistant",
            "content": reply_content,
        }
        if raw_reasoning:
            assistant_message["reasoning_content"] = raw_reasoning
        conversations.update_agent_state(
            conversation,
            [*history, {"role": "user", "content": user_content}, assistant_message],
        )
        conversations.write(conversation)

        usage = reply_result.usage
        return HomeChatResponse(
            title=title,
            title_source=title_source,
            title_note=title_note,
            conversation_id=str(conversation["id"]),
            project_id=conversation.get("project_id"),
            reply=reply_content,
            model_ref=reply_result.model_ref,
            provider=reply_result.provider,
            model_id=reply_result.model_id,
            cost_usd=reply_result.cost_usd,
            cost_unknown_reason=reply_result.cost_unknown_reason,
            usage={
                "prompt_tokens": getattr(usage, "prompt_tokens", None),
                "completion_tokens": getattr(usage, "completion_tokens", None),
                "total_tokens": getattr(usage, "total_tokens", None),
            },
        )


@router.post(
    "/chat/compact",
    response_model=CompactChatResponse,
    summary="手动压缩当前对话上下文（保留原始轮次与检查点）",
    dependencies=[Depends(require_owner)],
)
async def compact_chat(payload: CompactChatRequest) -> CompactChatResponse:
    async with home_turns.write_lock(payload.conversation_id):
        """Codex-style manual compaction.

        The active history is replaced by a short, model-readable summary while
        the pre-compaction protocol messages remain in a checkpoint snapshot.
        """

        record = conversations.read(payload.conversation_id)
        if record is None:
            raise _error(status.HTTP_404_NOT_FOUND, "conversation_not_found", "会话不存在")
        ref = await _resolve_model_ref(payload.model_config_id, payload.model_id)
        if home_turns.active(payload.conversation_id):
            raise _error(409, "turn_in_progress", "请等待当前轮次结束后再压缩上下文")
        from services import context_compaction as compaction_mod
        from services import context_meter

        messages = conversations.context_messages(record)
        span = compaction_mod.select_summary_span(messages)
        if span is None:
            return CompactChatResponse(
                ok=True,
                conversation_id=payload.conversation_id,
                changed=False,
                reason="nothing_to_compact",
                context=context_meter.get_meter().breakdown(messages),
            )
        summary_record = await compaction_mod.summarize_early_turns(
            messages,
            span=span,
            model_ref=ref,
        )
        if summary_record is None:
            raise _error(
                status.HTTP_502_BAD_GATEWAY,
                "compaction_failed",
                "上下文摘要没有返回有效内容，原始会话保持不变",
            )
        summary_record["summary"] = _sanitize_compaction_summary(str(summary_record.get("summary") or ""))
        before = conversations.protocol_messages(messages)
        freed_chars = compaction_mod.apply_turn_summary(
            messages,
            span=span,
            summary=str(summary_record["summary"]),
        )
        checkpoint_id = f"cp-manual-{time.time_ns()}"
        summary_record.update(
            {
                "checkpoint_id": checkpoint_id,
                "trigger": "manual",
                "before_message_count": len(before),
                "freed_chars": freed_chars,
            }
        )
        record["compactions"] = [*(record.get("compactions") or []), summary_record]
        conversations.update_agent_state(
            record,
            messages,
            checkpoints=[summary_record],
            snapshots=[
                {
                    "id": checkpoint_id,
                    "created_at": time.time(),
                    "reason": "manual",
                    "messages": before,
                }
            ],
        )
        conversations.write(record)
        return CompactChatResponse(
            ok=True,
            conversation_id=payload.conversation_id,
            changed=True,
            checkpoint_id=checkpoint_id,
            summary=str(summary_record["summary"]),
            context=context_meter.get_meter().breakdown(messages),
        )


@router.post(
    "/chat/home/stream",
    summary="首页对话（真流式 SSE：meta / delta / done / title / error，需 X-Owner-Token）",
    dependencies=[Depends(require_owner)],
)
async def home_chat_stream(payload: HomeChatRequest, request: Request) -> StreamingResponse:
    async with home_turns.write_lock(payload.conversation_id):
        """首页对话的流式版本。

        事件序列：

        - ``meta``：会话 id / 模型 / 项目（前端据此立刻把「新对话」挂到左栏）
        - ``delta``：正文增量（**逐段追加渲染**）
        - ``done``：耗时 / 用量 / 成本（``cost_usd`` 单价缺失时为 null，不估算）
        - ``title``：标题（模型生成完成或如实降级为需求前 20 字）
        - ``error``：建连失败或流中途断线；**此时 delta 已推的部分仍然有效并已落盘**

        落盘在 ``finally`` 里做：正常结束、报错、客户端断开三种情况都会把已生成的部分写进
        会话文件——**不白花 token**，也不允许"界面显示了但记录里没有"。

        ``replace_from`` 非空时为**编辑重开**：先校验（会话必须存在、下标必须指向 user 轮次，
        否则 404 / 422）再丢弃该条及其后的所有轮次，然后以 ``text`` 作为新的该条重问一次。
        这种情况下即使一个字都没生成也会落盘 —— 用户改过的正文必须留痕。
        """
        if payload.conversation_id and home_turns.active(payload.conversation_id):
            raise _error(
                409, "turn_in_progress", "当前对话仍在运行，请重新连接或停止当前轮次",
                _turn_in_progress_detail(payload.conversation_id),
            )
        text, user_content, attachment_metadata = _prepare_home_message(payload)
        user_ts = _turn_timestamp()

        ref = await _resolve_model_ref(payload.model_config_id, payload.model_id)

        conversation = conversations.read(payload.conversation_id) if payload.conversation_id else None
        prior_attachment_blocks, prior_attachment_metadata = _previous_attachment_content(
            conversation, payload.replace_from
        )
        if prior_attachment_blocks and not attachment_metadata:
            user_content = ([{"type": "text", "text": text}] if text else []) + prior_attachment_blocks
            attachment_metadata = prior_attachment_metadata
        # 编辑重开：先校验并截断，再建会话（会话不存在/下标越界都不会先建出空会话）
        is_edit = _truncate_for_edit(conversation, payload.replace_from)
        is_new = conversation is None
        if conversation is None:
            workspace_dir = await _workspace_dir(payload.project_id)
            conversation = conversations.create(
                title=text[:TITLE_MAX_CHARS],
                model_ref=ref,
                project_id=payload.project_id,
                workspace_dir=workspace_dir,
                persist=False,
            )
        conversation_id = str(conversation["id"])
        history = _repair_tool_call_pairs(conversations.context_messages(conversation))
        simple_mode = _is_simple_question(text)
        # Natural-language mentions of creating projects or writing files are not
        # execution requests. Executor recovery belongs to the actual tool call;
        # never terminate a model answer based on a guessed intent/health probe.

        # ------------------------------------------------------------------ #
        # 意图路由：**对话就是 agent 的入口**
        #   guide  → 明确启动研究但缺少唯一必要的主题：只问一个问题
        #   node   → 明确执行意图：直接跑真实节点
        #   query  → 查询本地数据：确定性只读查询 + 结果卡片
        #   其余    → 通用回答（下面原有的流式路径）
        # ------------------------------------------------------------------ #
        base_count = len(conversation.get("turns") or [])

        def owned(source: AsyncIterator[str], kind: str = "chat") -> StreamingResponse:
            try:
                owner = home_turns.start(conversation_id, source, kind=kind, base_count=base_count)
            except ValueError:
                raise _error(
                    409, "turn_in_progress", "当前对话仍在运行，请先重新连接",
                    _turn_in_progress_detail(conversation_id),
                ) from None
            return _streaming(home_turns.subscribe(conversation_id, owner.turn_id))

        scope: dict[str, Any] = {
            "conversation": conversation,
            "project_id": conversation.get("project_id"),
            "text": text,
        }

        if not attachment_metadata and dialog.is_plain_chat_reply(text):
            return owned(
                dialog.stream_plain_chat(conversation_id=conversation_id, scope=scope)
            )

        if not attachment_metadata and not simple_mode:
            routing = await dialog.route(
                text,
                conversation_id,
                # 规则兜底要用模型判一次 —— 用**这次对话选的模型**（不硬编码供应商）
                model_ref=ref,
                conversation=conversation,
            )
            if routing.kind == "guide":
                return owned(dialog.stream_guide(conversation_id=conversation_id, scope=scope))
            if routing.kind == "node" and routing.node in {"literature_review", "idea_and_feasibility"}:
                return owned(
                    dialog.stream_node(
                        conversation_id=conversation_id, text=text, scope=scope, routing=routing
                    )
                )
            if routing.kind == "query":
                return owned(
                    dialog.stream_query(
                        conversation_id=conversation_id, text=text, scope=scope, routing=routing
                    )
                )

        async def event_source() -> AsyncIterator[str]:
            cancel_event = asyncio.Event()
            owner_task = asyncio.current_task()
            if owner_task is not None:
                _ACTIVE_HOME_TURNS[conversation_id] = (cancel_event, owner_task)
            # 首帧只建立连接，不写入新会话。浏览器可能在收到首帧后立即切页，
            # 这时生成器会在下一个 yield 处被关闭；占位记录必须等 meta 已交付后再落盘。
            yield ": connected\n\n"
            yield _sse(
                "meta",
                {
                    "conversation_id": conversation_id,
                    "project_id": conversation.get("project_id"),
                    "model_ref": ref,
                    "model_id": payload.model_id,
                    "title": conversation.get("title"),
                    "turn_count": len(conversation.get("turns") or []),
                },
            )

            # 只有会话 id 已经交给前端后才落下新会话占位记录。若客户端在首帧或
            # meta 帧之间切页，生成器不会执行到这里，也就不会留下空会话。
            if is_new:
                conversations.write(conversation)

            # 标题与正文并行：标题只在「新会话」时生成（续聊不改标题）
            title_task: asyncio.Task[tuple[str, str, str | None]] | None = None
            if is_new:
                title_task = asyncio.create_task(_generate_title(text, ref))

            buffer: list[str] = []
            reasoning_buffer: list[str] = []
            reasoning_summary_buffer: list[str] = []
            # 工具调用过程行也要**落盘**：只发 SSE 不落盘的话，刷新后这段过程凭空消失，
            # 而库里只剩一句结论 —— 正是「显示与落盘必须一致」要防的那种不一致。
            tool_rows: list[dict[str, Any]] = []
            #: 本轮发生的上下文压缩记录（会话级 `compactions[]`，与过程行一起落盘）
            compactions_out: list[dict[str, Any]] = []
            #: 本轮里模型的**写盘/执行请求**（尚未执行）。与正文同源落进这一轮，
            #: 这样刷新后卡片还能按原状态重建，而不是"界面上一张卡、库里什么都没有"。
            approval_requests: list[dict[str, Any]] = []
            #: 停下来等研究者裁决的那一条（非空 = 这一轮没有答完，在等人）
            pending_approval: dict[str, Any] | None = None
            #: 本轮正文是**系统说明**（模型只给了思考过程）而不是模型说的话
            note_only = False
            #: 非空 = 模型这一跳彻底失败、但**没有**把整轮判死（见 `MODEL_ROUND_MAX_ATTEMPTS`）。
            #: 在 try 之前先初始化：`finally` 里要读它，不能因为"异常发生在赋值之前"而 NameError。
            soft_failure: str | None = None
            started = time.perf_counter()
            result: Any = None
            error_info: dict[str, Any] | None = None
            aborted = False
            generator_closing = False
            state: dict[str, Any] = {
                "text": buffer,
                "reasoning": reasoning_buffer,
                "reasoning_summary": reasoning_summary_buffer,
                "result": None,
                "pending": None,
                "compactions": compactions_out,
                "compaction_snapshots": [],
                "agent_history": [],
                "user_ts": user_ts,
                "attachments": attachment_metadata,
            }
            base_turns = list(conversation.get("turns") or [])
            last_checkpoint = 0.0
            state["agent_history"] = conversations.protocol_messages(history + [{"role": "user", "content": user_content}])
            _save_stream_progress(conversation, base_turns=base_turns, text=text, state=state,
                                  rows=tool_rows, approvals=approval_requests, model_id=payload.model_id, duration_ms=0)
            try:
                # ------------------------------------------------------------ #
                # agent 循环：把工具摆给模型 → 收 tool_calls → 只读的经 MCP 执行、
                # 写/执行类**只建批准请求** → 结果以 role=tool 喂回 → 再调一次，
                # 直到模型不再要求调工具（上限防死循环）。
                # 工具跑在 `mcp_server` 那个进程里（stdio 标准协议），边界也在那边。
                # 循环体在 `_agent_loop`，与批准后续答共用同一份口径。
                # 局部导入：与六环节的写法一致，避免无工具场景引入导入期依赖。
                # ------------------------------------------------------------ #
                from services.agent import mcp_tools as agent_tools

                messages_now: list[dict[str, Any]] = [
                    {
                        "role": "system",
                        "content": (
                            # Short questions skip intent routing, but they still
                            # receive the normal agent prompt and tool policy.
                            reply_system_prompt(conversation)
                            + await research_state_block(conversation_id, conversation)
                            + skills_system_block()
                            + ("\n当前对话工作目录：" + str(conversation.get("workspace_dir"))
                               + "。所有相对路径和命令默认目录均以此为准。不要扫描其他项目；工作区拒绝时不得换到旧项目写入。"
                               if conversation.get("workspace_dir") else "")
                        ),
                    },
                    *history,
                    {"role": "user", "content": user_content},
                ]
                try:
                    # Short turns use the same model path. Read-only tools such as
                    # the local clock remain available for factual questions.
                    tool_defs = await agent_tools.tool_schemas(workspace_root=conversation.get("workspace_dir"))
                except Exception as exc:  # noqa: BLE001 - 工具侧不可用不该拖垮整段对话
                    logger.warning("工具声明获取失败，本轮按无工具回答：%s", exc)
                    tool_defs = []

                # 研究确认门是服务端边界，不只是一条给模型的提示词。用户明确要求
                # 先讨论/确认范围时，或会话仍在等待研究启动确认时，本轮不得把任何
                # 搜索、抓取、文件或执行工具交给模型；这样即使模型误判，也不会产生
                # 只读搜索副作用。确认后下一轮会重新发现完整工具清单。
                if _research_gate_blocks_tools(text, conversation):
                    tool_defs = []

                state.update(
                    {
                        "agent_history": conversations.protocol_messages(messages_now),
                    }
                )
                async for frame in _agent_loop(
                    messages_now,
                    conversation_id=conversation_id,
                    ref=ref,
                    tool_defs=tool_defs,
                    rows=tool_rows,
                    approvals_out=approval_requests,
                    state=state,
                    max_attempts=None,
                    workspace_root=str(conversation.get("workspace_dir") or "") or None,
                ):
                    now = time.monotonic()
                    if now - last_checkpoint >= 1.0:
                        _save_stream_progress(
                            conversation, base_turns=base_turns, text=text, state=state,
                            rows=tool_rows, approvals=approval_requests, model_id=payload.model_id,
                            duration_ms=int((time.perf_counter() - started) * 1000),
                        )
                        last_checkpoint = now
                    yield frame
                result = state.get("result")
                pending_approval = state.get("pending")
                soft_failure = state.get("soft_failure")
            except LLMError as exc:
                # Provider failures are recoverable at the conversation boundary.
                # Keep the stream in a normal terminal state so the UI receives a
                # reader-facing paragraph and one ``done`` event instead of a
                # backend-looking error frame.
                soft_failure = str(exc)
                error_info = {"code": "llm_failed", "message": _public_stream_error(exc)}
                logger.warning("首页流式调用失败 conversation=%s：%s", conversation_id, exc)
            except GeneratorExit:
                # ASGI subscriber shutdown is not a turn cancellation. The
                # owner keeps running; this generator must never yield from
                # its finally path after aclose/GeneratorExit.
                generator_closing = True
                raise
            except asyncio.CancelledError:
                # Only the durable owner is cancelled by the explicit stop
                # endpoint. A detached subscriber never reaches this worker.
                aborted = True
                error_info = {
                    "code": "turn_interrupted",
                    "message": "本轮已停止",
                    "kind": "Cancelled",
                }
                raise
            except Exception as exc:  # noqa: BLE001 - 兜底：未预期异常也不该"甩个码就断"
                # 用户口径 2026-09-26：不允许把整轮变成强制中断。分两种情况处理最诚实：
                #   · 已经有字上屏 → 确实答了一半，如实标中断（不假装答完）；
                #   · 一个字都没说出去 → 交给模型写一句收尾（见 finally 里的 phrasing.say）；写不出来就留空 + 一条过程行。
                logger.exception("首页流式出现未预期异常 conversation=%s", conversation_id)
                # Treat unexpected provider/adapter failures the same way as a
                # classified LLM error.  Already emitted text stays visible and a
                # local recovery paragraph is used only when there is no text.
                soft_failure = str(exc)
                error_info = {"code": "worker_failed", "message": _public_stream_error(exc)}
            finally:
                if _ACTIVE_HOME_TURNS.get(conversation_id, (None, None))[1] is owner_task:
                    _ACTIVE_HOME_TURNS.pop(conversation_id, None)
                # Synchronous atomic save precedes any await: a cancelled ASGI
                # scope cannot skip it, nor can a slow follow-up model erase it.
                try:
                    _save_stream_progress(
                        conversation, base_turns=base_turns, text=text, state=state,
                        rows=tool_rows, approvals=approval_requests, model_id=payload.model_id,
                        duration_ms=int((time.perf_counter() - started) * 1000),
                        interrupted=bool(error_info),
                    )
                except OSError:
                    logger.exception("保存流式检查点失败 conversation=%s", conversation_id)
                if aborted and title_task is not None and not title_task.done():
                    # 本轮已经失败/断开，标题结果用不上了，别留悬挂任务
                    title_task.cancel()
                duration_ms = int((time.perf_counter() - started) * 1000)
                generated = _sanitize_assistant_content("".join(buffer))
                # **显示与落盘必须一致**：部分供应商只在收尾帧给正文（delta 为空），
                # 旧代码只存 delta buffer，于是界面上有内容、库里是空串。
                if not generated and result is not None:
                    generated = _sanitize_assistant_content(str(getattr(result, "content", "") or ""))
                reasoning_text = "".join(reasoning_buffer)
                if not reasoning_text and result is not None:
                    reasoning_text = str((getattr(result, "raw", None) or {}).get("reasoning") or "")
                if REASONING_MAX_CHARS is not None and len(reasoning_text) > REASONING_MAX_CHARS:
                    reasoning_text = reasoning_text[:REASONING_MAX_CHARS] + "\n…（思考过程过长，已截断）"
                if not generated and (soft_failure or reasoning_text) and error_info is None:
                    # A failed provider step must not trigger a second model call
                    # from HTTP ``finally``.  Preserve the turn and expose one
                    # stable, reader-facing status block; explicit recovery is a
                    # new turn owned by the runtime.
                    generated = _public_stream_error(
                        RuntimeError("provider step did not produce an answer")
                    )
                    note_only = True
                if generated and not buffer and error_info is None and not generator_closing:
                    buffer.append(generated)
                    history_for_resume = _stream_history(state)
                    if history_for_resume and history_for_resume[-1].get("role") == "assistant" and not history_for_resume[-1].get("content"):
                        history_for_resume[-1]["content"] = generated
                    else:
                        history_for_resume.append({"role": "assistant", "content": generated})
                    state["agent_history"] = history_for_resume
                    state["partial_text_from"] = None
                    yield _sse("delta", {"text": generated})
                # Do not add a canned warning row when both recovery attempts are
                # empty.  The visible transcript must contain model prose or the
                # already generated content, never an internal diagnostic.
                # 有正文 → 追加本轮；无正文且无错误 → 模型真的返回空，也要如实记一条。
                # 编辑重开时**无条件落盘**：用户改过的正文必须留痕，否则刷新后改动就凭空消失了。
                # 等批准时也**无条件落盘**：卡片本身就是这一轮的产出（哪怕一个字都没有），
                # 不落盘就会出现"界面上有一张待批的卡、刷新后它不见了、而工具也永远没跑"。
                try:
                    if compactions_out:
                        # 会话级 `compactions[]`（用户口径 2026-09-25：**新字段存摘要、原始轮次一字不改**）。
                        # 先挂到记录上再 `append_turns`，这样"压缩 + 本轮"**一次写入**，不留半成品状态。
                        conversation["compactions"] = [
                            *(conversation.get("compactions") or []),
                            *compactions_out,
                        ]
                    # Persist the model-facing protocol history separately from the
                    # readable turns.  This is the durable resume point used by the
                    # next request, while checkpoints retain the pre-compaction view.
                    conversations.update_agent_state(
                        conversation,
                        _stream_history(state),
                        checkpoints=compactions_out,
                        snapshots=state.get("compaction_snapshots") or [],
                    )
                    #: 研究者主动停止（前端 abort → 客户端断开）。这一轮**没有正文也得留痕**：
                    #  思考过程、过程行、待批卡片都是"已产出"的东西，更别说用户那句话本身 ——
                    #  2026-09-26 实测：按原来的条件（无正文 + 有错误）会被判成"没什么可记的"，
                    #  于是整轮跳过落盘，接着被下面那条 delete 把**整个会话**清掉，
                    #  表现就是"点了停止、刷新后那一轮连同自己的提问一起消失"（前端保留、后端没有）。
                    stopped_by_client = bool(error_info) and error_info.get("code") == "turn_interrupted"
                    if (
                        generated
                        or error_info is None
                        or is_edit
                        or pending_approval is not None
                        or stopped_by_client
                        or error_info is not None
                    ):
                        conversation["turns"] = base_turns
                        conversations.append_turns(
                            conversation,
                            [
                                {
                                    "role": "user",
                                    "content": text,
                                    "ts": user_ts,
                                    "attachments": attachment_metadata,
                                },
                                {
                                    "role": "assistant",
                                    "content": generated,
                                    "model_id": getattr(result, "model_id", payload.model_id)
                                    if result is not None
                                    else payload.model_id,
                                    # ⚠️ 这里必须是**整轮耗时**（`started` 是请求进来时打的表），
                                    # 不能拿 `result.duration_ms` —— 那是**最后一次模型调用**的耗时：
                                    # 多轮工具调用时它只有整轮的零头，研究者会看到"跑了半天显示 10s"。
                                    "duration_ms": duration_ms,
                                    "reasoning": reasoning_text or None,
                                    "reasoning_summary": "".join(state.get("reasoning_summary") or []) or None,
                                    "rows": tool_rows,
                                    # 批准请求随轮次落盘：裁决端点要凭它认账（一次性、带有效期）
                                    "approvals": approval_requests,
                                    "awaiting_approval": pending_approval["id"] if pending_approval else None,
                                    # 正文是系统说明（不是模型说的话）→ 别让它进下一轮的上下文
                                    "note_only": note_only or None,
                                    "interrupted": bool(error_info),
                                    "interruption_reason": "user_stop" if aborted else "worker_failed" if error_info else None,
                                    "ts": _turn_timestamp(),
                                },
                            ],
                            sync_history=False,
                        )
                except Exception as exc:  # noqa: BLE001 - persistence must end the stream cleanly
                    logger.exception("首页对话持久化失败 conversation=%s", conversation_id)
                    if error_info is None:
                        error_info = {
                            "code": "conversation_persist_failed",
                            "message": f"对话保存失败：{type(exc).__name__}",
                            "kind": type(exc).__name__,
                        }

                if error_info is not None and not generator_closing:
                    # 已生成部分照旧显示；尾部用 error 事件让前端标「已中断 / 出错」
                    yield _sse(
                        "error",
                        {
                            **error_info,
                            "interrupted": True,
                            "generated_chars": len("".join(buffer)),
                        },
                    )
                    return  # noqa: B012 - preserve one terminal SSE error frame

            usage = getattr(result, "usage", None)
            yield _sse(
                "done",
                {
                    "conversation_id": conversation_id,
                    "content": generated
                    or (getattr(result, "content", "") if result is not None else ""),
                    # 同上：整轮耗时（前端在 done 里会用它覆盖那一轮的显示）
                    "duration_ms": duration_ms,
                    "model_id": getattr(result, "model_id", payload.model_id),
                    "model_ref": getattr(result, "model_ref", ref),
                    "provider": getattr(result, "provider", ""),
                    "cost_usd": getattr(result, "cost_usd", None) if result is not None else None,
                    "cost_unknown_reason": getattr(result, "cost_unknown_reason", None)
                    if result is not None
                    else None,
                    "finish_reason": getattr(result, "finish_reason", None) if result is not None else None,
                    # 停在「等研究者批准」：前端据此把这一轮标成待裁决，**而不是**当成正常答完。
                    # 两者混起来，界面会显示一个"答完了但什么都没有"的空回复。
                    "awaiting_approval": agent_tools.approval_row(pending_approval)
                    if pending_approval is not None
                    else None,
                    "usage": {
                        "prompt_tokens": getattr(usage, "prompt_tokens", None),
                        "completion_tokens": getattr(usage, "completion_tokens", None),
                        "total_tokens": getattr(usage, "total_tokens", None),
                    },
                },
            )

            if title_task is not None and not title_task.done():
                def save_late_title(task: asyncio.Task[Any]) -> None:
                    if task.cancelled():
                        return
                    try:
                        title, _source, _note = task.result()
                        conversations.rename(conversation_id, title)
                    except Exception:
                        logger.exception("标题保存失败 conversation=%s", conversation_id)
                title_task.add_done_callback(save_late_title)
                return
            if title_task is not None:
                try:
                    title, title_source, title_note = title_task.result()
                except Exception as exc:  # noqa: BLE001 - 标题失败不能影响这一轮对话
                    logger.warning("标题任务异常：%s", exc)
                    title, title_source, title_note = text[:TITLE_MAX_CHARS], "fallback", str(exc)[:200]
                if title != conversation.get("title"):
                    try:
                        conversations.rename(conversation_id, title)
                    except Exception as exc:  # noqa: BLE001 - title is ancillary to the completed turn
                        logger.warning("标题保存失败 conversation=%s：%s", conversation_id, exc)
                yield _sse(
                    "title",
                    {
                        "conversation_id": conversation_id,
                        "title": title,
                        "title_source": title_source,
                        "title_note": title_note,
                    },
                )

        return owned(event_source())


@router.post(
    "/chat/research/interrupt",
    summary="停止当前对话中的研究节点",
    dependencies=[Depends(require_owner)],
)
async def interrupt_research(payload: HomeInterruptRequest) -> dict[str, Any]:
    """Signal the server-side research iterator before the browser closes SSE."""
    terminal = await home_turns.interrupt(payload.conversation_id, payload.turn_id)
    if terminal is not None:
        return {"ok": True, "requested": True, "status": terminal["status"],
                "conversation_id": payload.conversation_id, "turn": terminal}
    if payload.turn_id:
        terminal = home_turns.snapshot(payload.conversation_id, payload.turn_id)
        return {"ok": terminal is not None, "requested": False,
                "status": terminal["status"] if terminal else "not_running", "turn": terminal}
    requested = request_home_turn_interrupt(payload.conversation_id)
    research_requested = dialog.request_node_interrupt(payload.conversation_id)
    requested = requested or research_requested
    if not requested:
        requested = await dialog.request_persisted_node_interrupt(payload.conversation_id)
    return {
        "ok": True,
        "requested": requested,
        "status": "cancel_requested" if requested else "not_running",
        "conversation_id": payload.conversation_id,
    }


class ApprovalDecisionRequest(BaseModel):
    """研究者对一条**写盘/执行请求**的裁决。

    谁有资格裁决：`X-Owner-Token` 的唯一持有人（即研究者本人）。所以本端点与
    ``/chat/home/stream`` 一样是 Owner 专属 —— 批准入口如果对匿名开放，那道门就白设了。
    """

    conversation_id: str = Field(min_length=1, max_length=64)
    #: 待裁决请求的 id（来自 `approval` 事件 / 过程行里的 `request_id`）
    request_id: str = Field(min_length=1, max_length=64)
    decision: Literal["approve", "approve_conversation", "deny"]
    #: 可选备注，随裁决一起留痕（例如"只跑这次，下次同样的再说"）
    note: str | None = Field(default=None, max_length=500)
    #: 续答用哪个模型；不传 = 沿用会话记录里的 `model_ref`（通常够用）
    model_config_id: int | None = None
    model_id: str | None = Field(default=None, max_length=200)


#: 请求"已经不在待批状态"时的如实说明。**不能合并成一句"无效"** ——
#: 「批过了」「拒过了」「过期了」对研究者是三件不同的事，混起来他会以为是自己点错了。
_APPROVAL_CLOSED_REASONS = {
    "approved": "这条请求已经批准过了（一次性：同一批准不能重复执行）",
    "denied": "这条请求已被拒绝",
    "expired": "这条请求已过期（超过有效期未裁决）；需要的话让助手重新发起一次",
}

#: 拒绝后的确定性答复。**不花一次模型调用**：这句话不是模型的观点，是系统在陈述事实。


def _replace_approval_card(record: dict[str, Any], turn_index: int, card: dict[str, Any]) -> None:
    """把轮次里那张卡**换成新状态**（同一 request_id 只留一张，不追加第二张）。

    否则一次流程下来会攒出三四张卡，每一张都是同一次调用 —— 那不是留痕，那是噪声。
    """

    turns = record.get("turns") or []
    if turn_index >= len(turns):
        return
    turn = turns[turn_index]
    rows = list(turn.get("rows") or [])
    for index, row in enumerate(rows):
        if row.get("kind") == "approval" and row.get("request_id") == card.get("request_id"):
            rows[index] = card
            break
    else:  # pragma: no cover - 正常路径上卡片一定已经由主流程写进这一轮
        rows.append(card)
    turn["rows"] = rows


async def _approval_stream(
    record: dict[str, Any],
    turn_index: int,
    request: dict[str, Any],
    payload: ApprovalDecisionRequest,
    ref: str,
) -> AsyncIterator[str]:
    """裁决事件的流：批准就真的执行并继续回答，拒绝就如实记下来。

    **执行与否只由这里决定**，模型没有任何路径能自己把写盘/执行类工具跑掉。
    """

    from services.agent import mcp_tools as agent_tools

    conversation_id = str(record["id"])
    workspace_root = str(record.get("workspace_dir") or "") or None
    request_id = str(request["id"])
    tool = str(request["tool"])
    label = agent_tools.TOOL_LABELS.get(tool, tool)

    yield ": connected\n\n"
    yield _sse(
        "meta",
        {
            "conversation_id": conversation_id,
            "project_id": record.get("project_id"),
            "model_ref": ref,
            "title": record.get("title"),
            "turn_count": len(record.get("turns") or []),
            "routing": "approval",
        },
    )

    rows: list[dict[str, Any]] = []
    buffer: list[str] = []
    reasoning_buffer: list[str] = []
    reasoning_summary_buffer: list[str] = []
    #: 续答里模型**新提出**的批准请求：它们属于这一轮（新的一轮），不属于原来那一轮
    approvals_out: list[dict[str, Any]] = []
    pending: dict[str, Any] | None = None
    #: 本次裁决后那张卡的新状态（在 finally 里无条件换上去，**哪怕后面续答失败了**）
    card: dict[str, Any] | None = None
    result: Any = None
    error_info: dict[str, Any] | None = None
    #: 非空 = 模型这一跳彻底失败但没把整轮判死（见 `MODEL_ROUND_MAX_ATTEMPTS`）。
    #: 在 try 之前初始化：`finally` 里要读它。
    soft_failure: str | None = None
    #: 正文是**系统说明**（模型彻底失败后的如实交代），不是模型说的话
    note_only = False
    started = time.perf_counter()
    state: dict[str, Any] = {}
    agent_history: list[dict[str, Any]] = conversations.context_messages(record)
    compactions_out: list[dict[str, Any]] = []

    try:
        if payload.decision == "deny":
            agent_approvals.decide(
                record, request_id, status="denied", actor="owner", note=payload.note
            )
            # Preserve the exact frozen action even though it never executes.
            denied_call_id = str(request.get("call_id") or f"call_{request_id}")
            denied_call = {
                "id": denied_call_id,
                "type": "function",
                "function": {
                    "name": tool,
                    "arguments": json.dumps(request.get("args") or {}, ensure_ascii=False),
                },
            }
            card = agent_tools.approval_row(request)
            card["tool_name"] = tool
            card["tool_label"] = label
            card["tool_status"] = "failed"
            decision_row = {
                "kind": "tool",
                "tone": "warn",
                "text": f"研究者已拒绝「{label}」" + (f"：{payload.note}" if payload.note else ""),
            }
            _annotate_tool_row(decision_row, denied_call, "err")
            rows.append(decision_row)
            yield _sse("row", {"row": decision_row})
            yield _sse("approval", card)
            # Close the persisted assistant/tool-call pair with a synthetic
            # refusal result so the next turn remains protocol-valid.
            denied_history = conversations.context_messages(record)
            if not any(
                denied_call_id == str(call.get("id") or "")
                for message in denied_history
                if isinstance(message, dict)
                for call in (message.get("tool_calls") or [])
            ):
                denied_history.append(agent_tools.assistant_tool_message("", [denied_call]))
            agent_history = _repair_tool_call_pairs(
                denied_history,
                {
                    denied_call_id: agent_tools.tool_message_content(
                        {"ok": False, "error": "研究者拒绝了这次工具调用", "refused": True}
                    )
                },
            )
            # 拒绝的确认话由**模型**说（口径 2026-09-26：对话正文一律模型生成）。
            text = await phrasing.say(
                facts=(
                    f"研究者拒绝了这次要执行的动作：「{label}」。"
                    + (f"他给的说明：{payload.note}" if payload.note else "")
                    + "\n请用你自己的话告诉他：这次不执行了；想换个做法的话直接说一句要怎么做就行。"
                    "两三句话，不要提任何内部术语。"
                ),
                model_ref=ref,
                purpose="approval_denied_reply",
            )
            if text:
                buffer.append(text)
                agent_history.append({"role": "assistant", "content": text})
                yield _sse("delta", {"text": text})
        else:
            # ① 先签发**一次性**令牌并存指纹：会话文件里永远不会出现可用凭据
            token = agent_approvals.issue_token(request_id)
            agent_approvals.decide(
                record,
                request_id,
                status="approved",
                actor="owner",
                note=payload.note,
                token=token,
            )
            # 「批准此次执行」只放行当前调用；「本对话默认允许」是显式的持续授权。
            if payload.decision == agent_approvals.DECISION_APPROVE:
                grant_row = {
                    "kind": "tool",
                    "tone": "ok",
                    # 用户 2026-09-25：过程行不再带括号说明（界面上只留一句结论）
                    "text": f"已批准此次「{label}」",
                }
                _annotate_tool_identity(grant_row, tool, "waiting_approval")
                rows.append(grant_row)
                yield _sse("row", {"row": grant_row})
                logger.info("会话 %s 已批准单次调用：%s", conversation_id, tool)
            if payload.decision == agent_approvals.DECISION_APPROVE_CONVERSATION:
                summary = agent_approvals.set_grants(record, allow_exec=True)
                agent_approvals.save(record)
                grant_row = {
                    "kind": "tool",
                    "tone": "ok",
                    "text": "已允许在本对话内直接执行",
                }
                _annotate_tool_identity(grant_row, tool, "waiting_approval")
                rows.append(grant_row)
                yield _sse("row", {"row": grant_row})
                logger.info("会话 %s 获得执行授权：%s", conversation_id, summary)
            card = agent_tools.approval_row(request)
            card["tool_name"] = tool
            card["tool_label"] = label
            card["tool_status"] = "running"
            yield _sse("approval", card)
            decision_row = {"kind": "tool", "tone": "ok", "text": f"研究者已批准「{label}」"}
            _annotate_tool_identity(decision_row, tool, "waiting_approval")
            rows.append(decision_row)
            yield _sse("row", {"row": decision_row})

            # ② 按**原样参数**执行（参数在批准时就冻结了，不能在这之后再被改）
            call = {
                # 沿用模型当时给的 id：`tool_calls` 与 `tool` 结果严格配对，
                # 也是"批准的到底是哪一次调用"最直接的凭证
                "id": str(request.get("call_id") or f"call_{request_id}"),
                "type": "function",
                "function": {
                    "name": tool,
                    "arguments": json.dumps(request.get("args") or {}, ensure_ascii=False),
                },
            }
            start_row = _annotate_tool_row(agent_tools.tool_row(call, "start"), call, "start")
            rows.append(start_row)
            yield _sse("row", {"row": start_row})
            payload_out, summary = await agent_tools.run_tool_call(
                call,
                approval_token=token,
                access_mode="full" if agent_approvals.grants(record).get(agent_approvals.GRANT_FULL_ACCESS) else "workspace",
                workspace_root=workspace_root,
            )
            # 无论跑成没跑成，这次批准都**已经用掉了**（一次性）：不让"失败就再来一次"
            # 变成不受限的重试 —— 那等于把一次性批准变成了长期开关。
            agent_approvals.mark_consumed(record, request_id)
            end_status = "ok" if payload_out.get("ok") else "err"
            end_row = _annotate_tool_row(
                agent_tools.tool_row(call, end_status, summary), call, end_status
            )
            end_row.update(agent_tools.tool_execution_details(payload_out))
            rows.append(end_row)
            yield _sse("row", {"row": end_row})

            # ③ 把「提了 → 批了 → 跑了」整条链补进消息再让模型回答：
            # 直接把结果塞成一句用户消息，模型就不知道这是工具跑出来的，会当成用户说的话。
            calls = agent_tools.normalize_tool_calls([call])
            # **把上一跳的思考过程原样带回**：思考型供应商（实测 deepseek 系列）在
            # `assistant.tool_calls` 回合会硬性要求 `reasoning_content`，缺了直接 400。
            # 它就在这一轮记录里（`turn["reasoning"]`），没理由不带。
            history = conversations.context_messages(record)
            target_id = str(calls[0]["id"])
            if not any(
                target_id == str(item.get("id") or "")
                for message in history
                if isinstance(message, dict)
                for item in (message.get("tool_calls") or [])
            ):
                history.append(agent_tools.assistant_tool_message("", calls))
            messages_now: list[dict[str, Any]] = [
                {
                    "role": "system",
                    "content": reply_system_prompt(record)
                    + await research_state_block(conversation_id, record)
                    + skills_system_block(),
                },
                *_repair_tool_call_pairs(
                    history,
                    {target_id: agent_tools.tool_message_content(payload_out)},
                ),
            ]
            # 续答**照旧把工具摆上**：模型看到结果后常常要提下一条命令，
            # 那就再冒一张卡、再等一次裁决 —— 每条命令各批一次，这正是我们要的节奏。
            # （实测：不摆工具时，思考型供应商会因为"assistant 的 tool_calls 没带
            #   reasoning_content"直接 400，把续答整段打断。）
            try:
                tool_defs = await agent_tools.tool_schemas(workspace_root=record.get("workspace_dir"))
            except Exception as exc:  # noqa: BLE001 - 工具侧不可用不该拖垮续答
                logger.warning("续答时工具声明获取失败，本轮按无工具回答：%s", exc)
                tool_defs = []
            state: dict[str, Any] = {
                "text": buffer,
                "reasoning": reasoning_buffer,
                "reasoning_summary": reasoning_summary_buffer,
                "result": None,
                "pending": None,
                #: 本轮发生的上下文压缩（会话级落盘，刷新后研究者仍能看到"压过什么"）
                "compactions": compactions_out,
                "compaction_snapshots": [],
                "agent_history": conversations.protocol_messages(messages_now),
            }
            async for frame in _agent_loop(
                messages_now,
                conversation_id=conversation_id,
                ref=ref,
                tool_defs=tool_defs,
                rows=rows,
                approvals_out=approvals_out,
                state=state,
                allow_fallback_first=False,
                workspace_root=workspace_root,
            ):
                yield frame
            result = state["result"]
            pending = state["pending"]
            soft_failure = state.get("soft_failure")
            agent_history = state.get("agent_history") or conversations.protocol_messages(messages_now)
    except LLMError as exc:
        error_info = {
            "code": getattr(exc, "code", "llm_failed") or "llm_failed",
            "message": _public_stream_error(exc),
            "kind": type(exc).__name__,
        }
        logger.warning("批准裁决后续答失败 conversation=%s：%s", conversation_id, exc)
    except asyncio.CancelledError:
        error_info = {"code": "client_disconnected", "message": "客户端已断开", "kind": "Cancelled"}
        raise
    except Exception as exc:  # noqa: BLE001 - 兜底：未预期异常也不该"甩个码就断"
        logger.exception("批准裁决后续答出现未预期异常 conversation=%s", conversation_id)
        if buffer:
            error_info = {
                "code": getattr(exc, "code", "unexpected_error") or "unexpected_error",
                "message": _public_stream_error(exc),
                "kind": type(exc).__name__,
            }
        else:
            soft_failure = f"服务端异常 {type(exc).__name__}：{exc}"
    finally:
        duration_ms = int((time.perf_counter() - started) * 1000)
        # **裁决这件事必须留在记录里，哪怕续答失败**：卡片的终态先换上。
        # 放在 finally 而不是分支末尾，是因为"续答炸了"和"裁决没发生"完全是两件事，
        # 不能让前者的异常把后者的痕迹一起抹掉。
        if card is not None:
            _replace_approval_card(record, turn_index, card)
        answer = "".join(buffer)
        if not answer and result is not None:
            answer = str(getattr(result, "content", "") or "")
        if not answer and soft_failure and error_info is None:
            # Approval follow-up failures are settled by this turn owner.  A
            # second model call in ``finally`` could race cancellation and
            # produce a second terminal answer for the same turn.
            answer = _public_stream_error(
                RuntimeError("approval follow-up did not produce an answer")
            )
            note_only = True
        reasoning_text = "".join(reasoning_buffer)
        if not reasoning_text and result is not None:
            reasoning_text = str((getattr(result, "raw", None) or {}).get("reasoning") or "")
        if REASONING_MAX_CHARS is not None and len(reasoning_text) > REASONING_MAX_CHARS:
            reasoning_text = reasoning_text[:REASONING_MAX_CHARS] + "\n…（思考过程过长，已截断）"
        conversations.update_agent_state(
            record,
            agent_history,
            checkpoints=compactions_out,
            snapshots=state.get("compaction_snapshots") or [],
        )
        # 裁决 + 卡片状态 + 这一轮答复**一次写入**：`append_turns` 写的就是整份记录，
        # 所以不会出现"批准记下了、卡片没更新"或反过来的半成品状态。
        if answer or rows:
            conversations.append_turns(
                record,
                [
                    {
                        "role": "assistant",
                        "content": answer,
                        "model_id": getattr(result, "model_id", "") if result is not None else "",
                        # 同上：整轮耗时
                        "duration_ms": duration_ms,
                        "reasoning": reasoning_text or None,
                        "reasoning_summary": "".join(reasoning_summary_buffer) or None,
                        "rows": rows,
                        # 续答里模型又提出的请求，随**这一轮**落盘（可能要再批一次）
                        "approvals": approvals_out,
                        "awaiting_approval": pending["id"] if pending else None,
                        "routing": "approval",
                        # 正文是系统说明（不是模型说的话）→ 别让它进下一轮的上下文
                        "note_only": note_only or None,
                        "interrupted": bool(error_info),
                    }
                ],
                sync_history=False,
            )
        else:  # pragma: no cover - 兜底：一个字都没有、也没有行，至少把裁决写下
            conversations.write(record)

    if error_info is not None:
        yield _sse("error", {**error_info, "interrupted": True, "generated_chars": len("".join(buffer))})
        return

    usage = getattr(result, "usage", None)
    yield _sse(
        "done",
        {
            "conversation_id": conversation_id,
            "content": "".join(buffer) or (getattr(result, "content", "") if result is not None else ""),
            "duration_ms": duration_ms,
            "model_id": getattr(result, "model_id", "") if result is not None else "",
            "model_ref": getattr(result, "model_ref", ref) if result is not None else ref,
            "provider": getattr(result, "provider", "") if result is not None else "",
            "cost_usd": getattr(result, "cost_usd", None) if result is not None else None,
            "cost_unknown_reason": getattr(result, "cost_unknown_reason", None)
            if result is not None
            else None,
            "routing": "approval",
            "decision": payload.decision,
            "request_id": request_id,
            # 本次裁决已收口；但**续答里模型可能又提了一条**，那就继续等下一次裁决
            "awaiting_approval": agent_tools.approval_row(pending) if pending else None,
            "usage": {
                "prompt_tokens": getattr(usage, "prompt_tokens", None),
                "completion_tokens": getattr(usage, "completion_tokens", None),
                "total_tokens": getattr(usage, "total_tokens", None),
            },
        },
    )


@router.post(
    "/chat/approvals/decide",
    summary="研究者裁决一次写盘/执行请求（批准此次 / 此对话默认允许 / 拒绝）",
    dependencies=[Depends(require_owner)],
)
async def decide_approval(payload: ApprovalDecisionRequest) -> StreamingResponse:
    async with home_turns.write_lock(payload.conversation_id):
        """批准入口 —— 合规 8.3「关键动作需人工接管」的落点。

        校验从严，不做猜测：

        - 会话不存在 → 404；请求不存在 → 404
        - 请求**已批准/已拒绝/已过期** → 409，且**分别给出不同的 code 与说明**
          （合并成一句"无效"会让研究者以为是自己点错了）
        - 只有 `pending` 且未过期才可裁决

        裁决成功后由 ``_approval_stream`` 决定后续动作：批准 → 签发一次性令牌 → 经 MCP
        执行（令牌进 Guard 的门 3）→ 结果回喂模型 → 继续回答；拒绝 → 确定性答复，不花模型调用。
        """

        if home_turns.active(payload.conversation_id):
            raise _error(409, "turn_in_progress", "当前对话仍在运行，请先停止或等待本轮结束")
        record = agent_approvals.load(payload.conversation_id)
        if record is None:
            raise _error(404, "conversation_not_found", "会话不存在（可能已被删除）")

        found = agent_approvals.find_pending(record, payload.request_id)
        if found is None:
            existing = agent_approvals.find(record, payload.request_id)
            if existing is None:
                raise _error(404, "approval_not_found", "这条批准请求不存在")
            status = agent_approvals.effective_status(existing[1])
            raise _error(
                409,
                f"approval_{status}",
                _APPROVAL_CLOSED_REASONS.get(status, f"这条请求当前不可裁决（状态：{status}）"),
                {"request_id": payload.request_id, "status": status},
            )

        turn_index, request = found
        ref = str(record.get("model_ref") or "")
        if payload.model_config_id is not None and payload.model_id:
            ref = await _resolve_model_ref(payload.model_config_id, payload.model_id)
        if not ref:
            raise _error(409, "no_model", "会话没有记录可用模型，请重新发送一次对话再裁决")

        try:
            owner = home_turns.start(
                payload.conversation_id, _approval_stream(record, turn_index, request, payload, ref),
                kind="approval", base_count=len(record.get("turns") or []),
            )
        except ValueError:
            raise _error(409, "turn_in_progress", "当前对话仍在运行") from None
        return _streaming(home_turns.subscribe(payload.conversation_id, owner.turn_id))


@router.get("/chat/turns/{conversation_id}/{turn_id}", dependencies=[Depends(require_owner)])
async def read_home_turn(conversation_id: str, turn_id: str) -> dict[str, Any]:
    try:
        state = home_turns.snapshot(conversation_id, turn_id)
    except ValueError:
        raise _error(422, "invalid_turn", "轮次标识无效") from None
    if state is None:
        raise _error(404, "turn_not_found", "轮次不存在")
    return state


@router.get("/chat/turns/{conversation_id}/{turn_id}/stream", dependencies=[Depends(require_owner)])
async def resume_home_turn(conversation_id: str, turn_id: str, after: int = 0) -> StreamingResponse:
    await read_home_turn(conversation_id, turn_id)
    if after < 0:
        raise _error(422, "invalid_cursor", "事件位置无效")
    return _streaming(home_turns.subscribe(conversation_id, turn_id, after))


class AccessModeRequest(BaseModel):
    """完全访问模式开关（**按对话**）。

    为什么按对话而不是全局：研究者说的是「都是盖当前对话」——
    换一个对话就该重新决定，避免"某一次图省事"变成永久开关。
    """

    conversation_id: str = Field(min_length=1, max_length=64)
    full_access: bool


@router.post(
    "/chat/access-mode",
    summary="切换本对话的完全访问模式（开启后所有可执行操作自动批准）",
    dependencies=[Depends(require_owner)],
)
async def set_access_mode(payload: AccessModeRequest) -> dict[str, Any]:
    """开 / 关当前对话的「完全访问模式」。普通模式人工批准后具有相同执行范围。

    它管的是"动手类操作"（跑命令、写文件、删文件）要不要逐一确认：
    开着 → 所有可执行操作自动批准；关着 → 需要人工批准。两者允许的执行范围相同。
    只看东西的操作（列目录、读文件、查论文库）任何时候都不打扰研究者。
    """

    record = agent_approvals.load(payload.conversation_id)
    if record is None:
        raise _error(404, "conversation_not_found", "会话不存在（可能已被删除）")
    agent_approvals.set_grants(record, full_access=payload.full_access)
    agent_approvals.save(record)
    summary = agent_approvals.grants_summary(record)
    logger.info("会话 %s 完全访问模式=%s", payload.conversation_id, payload.full_access)
    return {"ok": True, "conversation_id": payload.conversation_id, **summary}


@router.get(
    "/chat/access-mode/{conversation_id}",
    summary="读本对话当前的授权状态（公开只读）",
)
async def read_access_mode(conversation_id: str) -> dict[str, Any]:
    """给界面显示用：现在是"动手前都先问"还是"直接做"。"""

    record = agent_approvals.load(conversation_id)
    if record is None:
        raise _error(404, "conversation_not_found", "会话不存在（可能已被删除）")
    return {"conversation_id": conversation_id, **agent_approvals.grants_summary(record)}


__all__ = ["router"]


def _tool_arguments(call: dict[str, Any]) -> dict[str, Any]:
    """取工具调用里的参数（解析失败就当空，不影响主流程）。"""

    raw = (call.get("function") or {}).get("arguments")
    if isinstance(raw, dict):
        return raw
    if isinstance(raw, str) and raw.strip():
        try:
            parsed = json.loads(raw)
        except json.JSONDecodeError:
            return {}
        return parsed if isinstance(parsed, dict) else {}
    return {}
