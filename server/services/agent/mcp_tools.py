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
from services.agent import host_runner, policy, web_search

#: 单轮对话里最多允许的「模型要求调工具」轮数。上限存在的意义是**防死循环**：
#: 模型可能反复要求调同一个工具，没有上限就会一直烧 token。
MAX_TOOL_ROUNDS = 3

#: 摆给模型**自主**调用的工具白名单（**只读**）。写/执行类不在这里，见 APPROVABLE_TOOLS。
#: 摆给模型**自主**调用的工具白名单（**只读**）。写/执行类不在这里，见 APPROVABLE_TOOLS。
#: `search_web` 走项目自建的 SearXNG（见 `services/agent/web_search.py`），也是只读。
AUTONOMOUS_TOOLS = ("query_library", "search_web", "fetch_url")

#: 摆给模型但**必须研究者批准**才执行的工具（写盘/执行类）。
APPROVABLE_TOOLS = ("run_command",)

#: 「在研究者自己的电脑上干活」的两个工具（走宿主执行器）。
#: 它们**不是** MCP server 提供的，声明写在下面 `_HOST_TOOL_SCHEMAS` 里；
#: 是否放行由 `judge()` 按三层边界与四类高危逐次裁决，**不是按名字一刀切**。
HOST_TOOLS = ("run_on_computer", "files_on_computer")

#: 工具名 → 对话里那句话（给用户看的，不是给模型看的）
TOOL_LABELS = {
    "query_library": "查询论文库",
    "search_web": "联网搜索",
    "fetch_url": "抓取网页",
    "run_command": "执行命令",
    "run_on_computer": "在你的电脑上执行命令",
    "files_on_computer": "在你电脑上读写或整理文件",
}

#: 「联网搜索」的工具声明（本地声明，不经过 MCP server）。
#: 它走项目自建的 SearXNG：免费开源、不需要商业 API Key。
_SEARCH_TOOL_SCHEMA: dict[str, Any] = {
    "type": "function",
    "function": {
        "name": "search_web",
        "description": (
            "到互联网上搜索资料，返回网页标题、链接与摘要（不是论文全文，引用前自己核对原始链接）。"
            "当论文库里找不到相关材料、或需要最新进展时用它。搜到什么由你判断怎么用；"
            "要不要把某条结果存进论文库，也由你决定。"
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "query": {"type": "string", "description": "搜索词（用研究对象最可能被怎么写，英文通常更好搜）"},
                "limit": {"type": "integer", "description": "最多要几条结果，默认 8"},
            },
            "required": ["query"],
        },
    },
}

#: 宿主工具的工具声明（OpenAI 兼容）。参数尽量少而直白，让模型好填、让人好读。
_HOST_TOOL_SCHEMAS: list[dict[str, Any]] = [
    {
        "type": "function",
        "function": {
            "name": "run_on_computer",
            "description": (
                "在研究者自己的电脑上执行一条命令，返回真实输出与退出码。"
                "cwd 用研究者电脑上的真实路径。删除类/改系统/破坏数据库/下载即执行属于高危，"
                "执行前会先请研究者确认。"
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "command": {"type": "string", "description": "要执行的命令行文本"},
                    "cwd": {"type": "string", "description": "在哪个目录下执行（绝对路径）"},
                    "timeout_s": {"type": "integer", "description": "最长允许跑多少秒，默认 120"},
                },
                "required": ["command"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "files_on_computer",
            "description": (
                "在研究者自己的电脑上读、列、新建、移动、删除文件或目录。"
                "删除与覆盖已有文件属于高危，会先请研究者确认。"
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "action": {
                        "type": "string",
                        "enum": ["list", "read", "write", "mkdir", "move", "delete"],
                        "description": "要做的动作",
                    },
                    "path": {"type": "string", "description": "目标路径（绝对路径）"},
                    "to": {"type": "string", "description": "移动的目标路径（仅 move 用）"},
                    "content": {"type": "string", "description": "要写入的内容（仅 write 用）"},
                    "recursive": {"type": "boolean", "description": "删除目录时必须为 true"},
                },
                "required": ["action", "path"],
            },
        },
    },
]


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

    两类来源：
    - **MCP server 提供**的（`query_library` / `fetch_url` / `run_command`）——
      清单以运行中的 server 为准，不硬编码参数；
    - **本地声明**的宿主工具（`_HOST_TOOL_SCHEMAS`）—— 它们走宿主执行器，不经过 MCP。

    ⚠️ MCP 侧拿不到**不再等于"没有工具"**：宿主工具仍要摆出去，否则执行器连不上时
    模型会以为自己什么都不能做，而不是告诉研究者"先启动执行器"。

    **必须是 async**：调用点在对话的 async 生成器里，用 `asyncio.run()` 会直接抛
    `RuntimeError: asyncio.run() cannot be called from a running event loop`。
    """

    import json as _json

    schemas: list[dict[str, Any]] = _json.loads(_json.dumps(_HOST_TOOL_SCHEMAS))
    schemas.append(_json.loads(_json.dumps(_SEARCH_TOOL_SCHEMA)))

    # 把"你在这台电脑上的默认工作目录"写进工具描述：模型据此决定要不要显式指定目录，
    # 也免得它去猜容器里的路径（容器路径在宿主上根本不存在）。
    default_dir = await host_runner.default_cwd()
    if default_dir:
        for item in schemas:
            function = item["function"]
            function["description"] = (
                f"{function['description']} 你在这台电脑上的默认工作目录是 {default_dir}。"
            )

    from mcp_server.client import list_tools

    wanted = set(AUTONOMOUS_TOOLS) | set(APPROVABLE_TOOLS)
    if not _allowed_hosts():
        wanted.discard("fetch_url")
    try:
        listed = await list_tools(mcp_params())
    except Exception:  # noqa: BLE001 - 工具侧不可用不该拖垮对话
        return schemas

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


async def judge(call: dict[str, Any]) -> policy.Verdict:
    """逐次裁决一次工具调用该「放行 / 要研究者点头 / 直接拒绝」。

    与 `requires_approval(name)` 的区别：那个只看**工具名**（写盘类一律弹卡）；
    这个看**这一次要干什么** —— 同一句 `rm`，删研究项目里的临时文件是"高危待批"，
    删 SciLoop 自己的代码是"直接拒绝"；调 `files_on_computer` 读文件则根本不用打扰研究者。
    """

    name = str((call.get("function") or {}).get("name") or "")
    args = arguments_of(call)

    if name == "run_on_computer":
        # 先确保裁决层知道宿主路径（删代码=硬拒要靠它）；拿不到也不拦着走流程
        await host_runner.ensure_host_roots()
        argv = args.get("argv") if isinstance(args.get("argv"), list) else None
        command = args.get("command") if isinstance(args.get("command"), str) else None
        cwd = args.get("cwd") if isinstance(args.get("cwd"), str) else None
        return policy.judge_command(argv=argv, command=command, cwd=cwd)

    if name == "files_on_computer":
        await host_runner.ensure_host_roots()
        action = str(args.get("action") or "")
        path = str(args.get("path") or "")
        recursive = bool(args.get("recursive"))
        to = args.get("to") if isinstance(args.get("to"), str) else None
        # 覆盖已有文件才算高危 → 先问一眼"它现在在不在"，问不到就按"在"处理（宁可多问一次）
        exists: bool | None = None
        if action in ("write", "move", "copy") and path:
            listing = await host_runner.call_fs(action="list", path=path)
            exists = bool(listing.get("ok"))
        return policy.judge_fs(
            action=action, path=path, to=to, exists=exists, recursive=recursive
        )

    if name in APPROVABLE_TOOLS:
        return policy.Verdict(
            policy.DECISION_APPROVE,
            "sandbox",
            "这一步要在 SciLoop 自己的工作区里执行，请你确认后我再动手。",
            ("执行命令",),
        )
    if name in AUTONOMOUS_TOOLS:
        return policy.Verdict(
            policy.DECISION_ALLOW, "read", "只读查询，可以直接做", harmless=True
        )
    return policy.Verdict(
        policy.DECISION_FORBID, "unknown", f"我不认识这个工具（{name}），不执行。"
    )


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

    # 「联网搜索」：只读，走自建 SearXNG；连不上就如实回一句人话（不假装搜过）
    if name == "search_web":
        result = await web_search.search(
            str(arguments.get("query") or ""),
            limit=int(arguments.get("limit") or 8),
        )
        return {"ok": bool(result.get("ok")), "tool": name, "result": result}, _summarize(name, result)

    # 「在研究者的电脑上干活」这一类：逐次裁决 + 走宿主执行器。
    # ⚠️ 这里**再判一次**（`_agent_loop` 已判过）：批准链路可以被别的入口复用，
    # 把边界放在执行点上，才不会因为"某个调用方忘了先判"而放水。
    if name in HOST_TOOLS:
        verdict = await judge(call)
        if verdict.forbidden:
            return {"ok": False, "error": verdict.message, "refused": True}, verdict.message
        if verdict.needs_approval and not approval_token:
            return (
                {"ok": False, "error": "这一步属于高危操作，需要研究者确认后才会执行"},
                "等待研究者确认",
            )
        if name == "run_on_computer":
            argv = arguments.get("argv") if isinstance(arguments.get("argv"), list) else None
            raw_cwd = arguments.get("cwd") if isinstance(arguments.get("cwd"), str) else None
            # 模型没说在哪儿跑 → 用研究者电脑上的研究项目目录（不是容器里的沙箱）
            cwd = raw_cwd or await host_runner.default_cwd()
            result = await host_runner.call_exec(
                argv=argv,
                command=arguments.get("command") if isinstance(arguments.get("command"), str) else None,
                cwd=cwd,
                timeout_s=int(arguments.get("timeout_s") or 120),
            )
        else:
            result = await host_runner.call_fs(
                action=str(arguments.get("action") or ""),
                path=str(arguments.get("path") or ""),
                to=arguments.get("to") if isinstance(arguments.get("to"), str) else None,
                content=arguments.get("content") if isinstance(arguments.get("content"), str) else None,
                recursive=bool(arguments.get("recursive")),
            )
        summary = _summarize(name, result)
        return {"ok": bool(result.get("ok")), "tool": name, "result": result}, summary

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

    # 宿主工具：摘要会显示在对话里的过程行上（研究者看得到），所以只说人话与事实
    if name == "search_web":
        if data.get("unavailable"):
            return "没连上搜索服务"
        count = data.get("count")
        if not isinstance(count, int):
            return "搜索完成"
        return f"搜到 {count} 条结果" if count else "没搜到相关结果"

    if name == "run_on_computer":
        if data.get("unreachable"):
            return "还没连上这台电脑的执行器"
        if data.get("timed_out"):
            return "跑超时了，已停下"
        code = data.get("exit_code")
        if code is None:
            return f"没跑起来：{str(data.get('error') or '')[:60]}"
        return f"执行完成（退出码 {code}）" if code == 0 else f"执行结束但返回了错误码 {code}"

    if name == "files_on_computer":
        if data.get("unreachable"):
            return "还没连上这台电脑的执行器"
        if not data.get("ok"):
            return f"没做成：{str(data.get('error') or '')[:60]}"
        action = str(data.get("action") or "")
        children = data.get("children")
        if isinstance(children, list):
            return f"这个目录下有 {len(children)} 项"
        if action == "write":
            return f"已写入 {data.get('bytes') or 0} 个字符"
        if action == "read":
            return f"读到 {len(str(data.get('content') or ''))} 个字符"
        if action == "delete":
            return "已删除"
        if action == "move":
            return "已移动"
        return "完成"

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
