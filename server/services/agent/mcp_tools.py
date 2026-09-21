# Copyright 2026 SciLoop contributors
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
"""把 SciLoop 的 MCP 工具接到对话循环上。

分工：`mcp_server` 是**工具侧**（工具声明 + 四道边界门），本模块是**对话侧**——
负责「把哪些工具摆给模型」「模型的 tool_calls 怎么执行」「执行过程怎么回报给前端」。

**三类工具，三种待遇**（已确认的产品口径）：

- `AUTONOMOUS_TOOLS`：只读工具，模型自主调，自动放行。
- `APPROVABLE_TOOLS`：写盘/执行类，**摆给模型、但绝不自主执行** —— 模型可以"提出"，
  必须拿到研究者的批准令牌才真正跑。令牌由 `api/v1/chat.py` 的裁决端点在研究者点批准后签发，
  本模块只负责"怎么把令牌送进 Guard"。
- 其余：不摆给模型（连声明都不给）。

把需批准的工具摆给模型，前提是**批准链路已经存在**：没有链路就摆，等于把边界开了个洞。
链路见 `services/agent/approvals.py`（请求落盘 + 一次性令牌）与
`api/v1/chat.py::decide_approval`（研究者裁决）。
"""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any

from mcp_server.client import call_tool, server_params

#: 单轮对话里最多允许的「模型要求调工具」轮数。上限存在的意义是**防死循环**：
#: 模型可能反复要求调同一个工具，没有上限就会一直烧 token。
MAX_TOOL_ROUNDS = 3

#: 摆给模型**自主**调用的工具白名单（**只读**）。写/执行类不在这里，见 APPROVABLE_TOOLS。
AUTONOMOUS_TOOLS = ("query_library", "fetch_url")

#: 摆给模型但**必须研究者批准**才执行的工具（写盘/执行类）。
APPROVABLE_TOOLS = ("run_command",)

#: 工具名 → 对话里那句话（给用户看的，不是给模型看的）
TOOL_LABELS = {
    "query_library": "查询论文库",
    "fetch_url": "抓取网页",
    "run_command": "执行命令",
}


def agent_workspace() -> Path:
    """agent 的工作区（也是它唯一被允许写入的根）。

    放在 `server/.cache/agent-workspace`：与 ingest / reader / translate 的 `.cache`
    口径一致（`parents[2]` == server 根），且容器内该目录已由 compose 挂载。
    """

    return Path(__file__).resolve().parents[2] / ".cache" / "agent-workspace"


def _allowed_hosts() -> tuple[str, ...]:
    """出网白名单。**空 = 不摆出联网工具** —— 没配就默认没有联网能力。"""

    raw = os.environ.get("SCILOOP_AGENT_ALLOWED_HOSTS", "")
    return tuple(h.strip().lower() for h in raw.split(",") if h.strip())


def mcp_params(
    *,
    approval_tokens: tuple[str, ...] = (),
    allow_exec: bool = False,
    allowed_hosts: tuple[str, ...] | None = None,
) -> Any:
    """给 MCP client 的启动参数。授权在这里给出（不来自调用方）。

    - `approval_tokens`：**只在研究者批准后**由裁决端点传入，一次调用一个令牌。
    - `allow_exec`：需批准的工具要过「能力门」才谈得上「审批门」，故批准执行时同时开 exec；
      **开了 exec 也不等于能执行** —— 没有有效令牌仍会被门 3 拒。
    - `allowed_hosts`：默认取 `SCILOOP_AGENT_ALLOWED_HOSTS`（与「摆不摆 fetch_url」同一来源，
      避免出现"摆得出去、调用必被拒"的错位）。空白名单 = 不许出网。
    """

    import sys

    workspace = agent_workspace()
    workspace.mkdir(parents=True, exist_ok=True)
    hosts = _allowed_hosts() if allowed_hosts is None else tuple(allowed_hosts)
    return server_params(
        python=sys.executable,
        repo_server_dir=Path(__file__).resolve().parents[2],
        workspace=workspace,
        actor="agent:chat",
        allow_exec=allow_exec,
        allow_net=bool(hosts),
        allowed_hosts=hosts,
        approval_tokens=tuple(approval_tokens),
    )


def requires_approval(name: str) -> bool:
    """这个工具是否属于「必须研究者批准」那一类。"""

    return name in APPROVABLE_TOOLS


async def tool_schemas() -> list[dict[str, Any]]:
    """OpenAI 兼容的工具声明（只含当前允许摆给模型的那两类）。

    工具清单以**运行中的 MCP server** 为准（不硬编码参数），这样工具改了声明这里自动跟上；
    拿不到就返回空列表 —— 摆不出工具不该让整段对话失败。

    **必须是 async**：调用点在对话的 async 生成器里，用 `asyncio.run()` 会直接抛
    `RuntimeError: asyncio.run() cannot be called from a running event loop`。
    """

    from mcp_server.client import list_tools

    wanted = set(AUTONOMOUS_TOOLS) | set(APPROVABLE_TOOLS)
    if not _allowed_hosts():
        # 没配出网白名单 → 不摆 fetch_url（摆了也一定会被边界拒，不如不摆）
        wanted.discard("fetch_url")
    try:
        listed = await list_tools(mcp_params())
    except Exception:  # noqa: BLE001 - 工具侧不可用不该拖垮对话
        return []

    import json as _json

    schemas: list[dict[str, Any]] = []
    for item in listed:
        name = item.get("name")
        if name not in wanted:
            continue
        # MCP 的 inputSchema 就是 JSON Schema，直接当 OpenAI 的 parameters 用
        raw = item.get("inputSchema") or item.get("input_schema") or {"type": "object"}
        schemas.append(
            {
                "type": "function",
                "function": {
                    "name": name,
                    "description": item.get("description") or TOOL_LABELS.get(name, name),
                    "parameters": _json.loads(_json.dumps(_public_schema(raw))),
                },
            }
        )
    return schemas


def _public_schema(raw: dict[str, Any]) -> dict[str, Any]:
    """抹掉**不该给模型看**的入参：批准令牌。

    令牌是研究者的凭据，只能由服务端在获批后注入。如果把它留在声明里，
    模型就可能自己编一个塞进来（那正是"让调用方自己声明我允许自己执行"的同一种错），
    或者被提示词注入骗着去填。**把它从声明里拿掉，就没什么可填的。**
    """

    if not isinstance(raw, dict):
        return {"type": "object"}
    properties = raw.get("properties")
    if not isinstance(properties, dict) or "approval_token" not in properties:
        return raw
    cleaned = dict(raw)
    cleaned["properties"] = {
        key: value for key, value in properties.items() if key != "approval_token"
    }
    required = cleaned.get("required")
    if isinstance(required, list):
        cleaned["required"] = [item for item in required if item != "approval_token"]
    return cleaned


def tool_row(call: dict[str, Any], phase: str, detail: str = "") -> dict[str, Any]:
    """工具调用在对话里的呈现（走现有 SSE `row` 事件，前端已能渲染）。"""

    fn = call.get("function") or {}
    name = str(fn.get("name") or "")
    label = TOOL_LABELS.get(name, name or "工具")
    if phase == "start":
        return {"kind": "tool", "tone": "info", "text": f"调用「{label}」{detail}".strip()}
    if phase == "ok":
        return {"kind": "tool", "tone": "ok", "text": f"「{label}」完成：{detail}"}
    if phase == "waiting":
        # tone=warn 而不是 err：**这不是失败，是停下来等人** —— 把两者混起来，
        # 研究者会以为工具已经出错了，而实际上它一次都没跑。
        return {"kind": "tool", "tone": "warn", "text": f"「{label}」等待研究者批准{detail}"}
    return {"kind": "tool", "tone": "warn", "text": f"「{label}」未完成：{detail}"}


#: 批准请求在行里的状态 → 卡片语气（前端只按这个上色，不自己猜）
APPROVAL_TONES = {
    "pending": "warn",
    "approved": "ok",
    "denied": "err",
    "expired": "idle",
}


def approval_row(request: dict[str, Any]) -> dict[str, Any]:
    """批准请求在对话里的呈现 —— **行即卡片**。

    卡片的所有字段都放进这一行（而不是只发一个内存态事件），是为了让它跟其它过程行一样
    **随会话落盘**：刷新后前端能凭这一行把卡片按原状态重建出来。
    只发 SSE 不落盘的话，刷新后卡片刻凭空消失，而库里留着一句"等待批准" ——
    正是「显示与落盘必须一致」要防的那种不一致。
    """

    status = str(request.get("status") or "pending")
    tool = str(request.get("tool") or "")
    label = TOOL_LABELS.get(tool, tool or "工具")
    preview = str(request.get("preview") or "")
    return {
        "kind": "approval",
        "tone": APPROVAL_TONES.get(status, "warn"),
        "text": f"「{label}」需要研究者批准" + (f"：{preview}" if preview else ""),
        "request_id": str(request.get("id") or ""),
        "tool": tool,
        "label": label,
        "preview": preview,
        "cwd": request.get("cwd"),
        "args_digest": request.get("args_digest"),
        "status": status,
        "created_at": request.get("created_at"),
        "expires_at": request.get("expires_at"),
        "decided_at": request.get("decided_at"),
        "note": request.get("note"),
    }


def _arguments(call: dict[str, Any]) -> dict[str, Any]:
    raw = (call.get("function") or {}).get("arguments")
    if isinstance(raw, dict):
        return raw
    if not isinstance(raw, str) or not raw.strip():
        return {}
    try:
        parsed = json.loads(raw)
    except json.JSONDecodeError:
        return {}
    return parsed if isinstance(parsed, dict) else {}


def arguments_of(call: dict[str, Any]) -> dict[str, Any]:
    """`_arguments` 的公开入口。

    对话循环要拿**原始参数**去建批准请求（卡片预览、指纹、以及批准后"按原样执行"都靠它），
    所以不能只有一个下划线开头的私有函数。
    """

    return _arguments(call)


async def run_tool_call(
    call: dict[str, Any], *, approval_token: str | None = None
) -> tuple[dict[str, Any], str]:
    """执行一次工具调用，返回 `(给模型看的结果, 一句话摘要)`。

    `approval_token` **只有裁决端点在研究者点「批准」后才会传进来**。
    需批准的工具在没有令牌时**根本不会发起 MCP 调用** —— 不是"调了被拒"，
    而是"压根没发出"。少一次进程往返，也少一次"差点就执行了"的机会。

    **失败也要把结果回给模型**：模型只有知道"这个工具失败了、原因是什么"，
    才会换个方式或如实告知用户；把失败吞掉会让它继续胡编。
    """

    fn = call.get("function") or {}
    name = str(fn.get("name") or "")
    arguments = _arguments(call)

    if requires_approval(name):
        if not approval_token:
            return (
                {"ok": False, "error": f"工具「{name}」需要研究者批准，未获批准前不执行"},
                f"{TOOL_LABELS.get(name, name)} 等待研究者批准",
            )
        # 令牌**由服务端注入参数**：`Guard` 既要求"
        # 该令牌在本次启动的授权名单里"，也要求"这次调用自己带上它"
        # （名单挡"没被授权过的令牌"，参数挡"没批准就调"）。
        # 模型看不到这个字段（已从声明里抹掉），所以它无从伪造。
        arguments = {**arguments, "approval_token": approval_token}
        # 批准后：能力门（exec）与审批门（令牌）同时具备，才谈得上执行
        params = mcp_params(approval_tokens=(approval_token,), allow_exec=True)
    elif name in AUTONOMOUS_TOOLS:
        params = mcp_params()
    else:
        return {"ok": False, "error": f"工具 {name} 不允许自主调用"}, f"{name} 不在自主白名单内"

    result = await call_tool(name, arguments, params)
    if result.ok:
        data = result.data
        summary = _summarize(name, data)
        return {"ok": True, "tool": name, "result": data}, summary
    return (
        {"ok": False, "tool": name, "error": result.text or result.raw_error},
        result.raw_error or "工具返回错误",
    )


def _summarize(name: str, data: dict[str, Any]) -> str:
    """摘要只说事实（条数、状态码、字节数、退出码），不替模型解释内容。"""

    if name == "query_library":
        for key in ("papers", "items", "results", "matches"):
            value = data.get(key)
            if isinstance(value, list):
                return f"命中 {len(value)} 条"
        return "查询完成"
    if name == "fetch_url":
        status = data.get("status_code")
        size = data.get("bytes")
        return f"HTTP {status}，{size} 字节" if status is not None else "抓取完成"
    if name == "run_command":
        # 非零退出码**不是工具失败**（命令跑成了、只是返回非零），摘要要如实分开说
        code = data.get("exit_code")
        if code is None:
            return "执行完成"
        return f"退出码 {code}" + ("（成功）" if code == 0 else "（命令返回非零，不是工具失败）")
    return "完成"


def normalize_tool_calls(calls: Any) -> list[dict[str, Any]]:
    """把模型给的 tool_calls 收拾成**回喂时能被供应商接受**的形状。

    两处实测踩出来的坑（caused `bad_request`，日志里表现为"流式调用 ds 建连失败（bad_request）"）：
    - `id` 缺失时会带上 `null`，部分兼容端直接判参数错 → **补一个稳定的 id**；
      我们随后写 `tool_call_id` 用的是同一个值，配对仍然成立。
    - 缺 `name` 的条目无法执行 → 直接丢掉（留着只会让回喂永远失败）。
    """

    out: list[dict[str, Any]] = []
    if not isinstance(calls, list):
        return out
    for index, raw in enumerate(calls):
        if not isinstance(raw, dict):
            continue
        fn = raw.get("function")
        fn = fn if isinstance(fn, dict) else {}
        name = str(fn.get("name") or "").strip()
        if not name:
            continue
        args = fn.get("arguments")
        out.append(
            {
                "id": str(raw.get("id") or f"call_{index}"),
                "type": str(raw.get("type") or "function"),
                "function": {
                    "name": name,
                    "arguments": args if isinstance(args, str) else json.dumps(args or {}, ensure_ascii=False),
                },
            }
        )
    return out


def assistant_tool_message(content: str, calls: list[dict[str, Any]]) -> dict[str, Any]:
    """构造「模型要求调工具」那一轮的 assistant 消息。

    **content 为空时整个字段都不写**：写成 `content: ""` 会被部分兼容端判为 `bad_request`
    （实测：紧接着的降级链会切到没配 key 的供应商，最后报成一句与真因无关的
    「供应商 env 未配置 API Key」，极难定位）。
    """

    message: dict[str, Any] = {"role": "assistant", "tool_calls": calls}
    if content:
        message["content"] = content
    return message


def tool_message_content(payload: dict[str, Any]) -> str:
    """把工具结果转成 `role=tool` 消息的正文。**截断但不撒谎**。"""

    text = json.dumps(payload, ensure_ascii=False)
    if len(text) > 8000:
        return text[:8000] + "…（内容过长已截断）"
    return text
