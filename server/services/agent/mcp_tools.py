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
import time
from pathlib import Path
from typing import Any

from mcp_server.client import call_tool, server_params
from services.agent import host_runner, policy, web_search
from services.skills import runner as runner_mod

#: 单轮对话里最多允许的「模型要求调工具」轮数。上限存在的意义是**防死循环**：
#: 模型可能反复要求调同一个工具，没有上限就会一直烧 token。
#: 单次回答里的**模型调用**次数上限（用户口径 2026-09-25：600）。
MAX_MODEL_CALLS_PER_TURN = 600
#: 单次回答里的**工具调用**次数上限（用户口径 2026-09-25：500）。
MAX_TOOL_CALLS_PER_TURN = 500

#: 摆给模型**自主**调用的工具白名单（**只读**）。写/执行类不在这里，见 APPROVABLE_TOOLS。
#: 摆给模型**自主**调用的工具白名单（**只读**）。写/执行类不在这里，见 APPROVABLE_TOOLS。
#: `search_web` 走项目自建的 SearXNG（见 `services/agent/web_search.py`），也是只读。
#: 只读的联网检索**两个并列能力**（模型自己选）：
#: `search_academic` 查学术（官方接口，稳）、`search_web` 搜网页（覆盖广，可能被限流）。
AUTONOMOUS_TOOLS = ("query_library", "search_academic", "search_web", "fetch_url", "load_skill")

#: 摆给模型但**必须研究者批准**才执行的工具（写盘/执行类）。
APPROVABLE_TOOLS = ("run_command",)

#: 「在研究者自己的电脑上干活」的两个工具（走宿主执行器）。
#: 它们**不是** MCP server 提供的，声明写在下面 `_HOST_TOOL_SCHEMAS` 里；
#: 是否放行由 `judge()` 按三层边界与四类高危逐次裁决，**不是按名字一刀切**。
HOST_TOOLS = ("run_on_computer", "files_on_computer")

#: 工具名 → 对话里那句话（给用户看的，不是给模型看的）
TOOL_LABELS = {
    "query_library": "查询论文库",
    "search_academic": "查学术",
    "search_web": "搜网页",
    "fetch_url": "抓取网页",
    "load_skill": "加载技能",
    "run_skill": "跑技能",
    "run_command": "执行命令",
    "run_on_computer": "在你的电脑上执行命令",
    "files_on_computer": "在你电脑上读写或整理文件",
}

#: 「搜网页」的工具声明（本地声明，不经过 MCP server）。
#: 走项目自建的 SearXNG：免费开源、不需要商业 API Key。
_SEARCH_WEB_TOOL_SCHEMA: dict[str, Any] = {
    "type": "function",
    "function": {
        "name": "search_web",
        "description": (
            "到互联网上搜资料（网页/博客/问答/文档都可能有），返回标题、链接与摘要。"
            "覆盖面广，但**可能被上游搜索引擎限流或要验证码**（那种情况我会如实告诉你）。"
            "找论文、找开源实现，优先用 search_academic（它走官方接口，更稳）。"
            "引用前自己打开原始链接核对；结果要不要存进论文库由你决定。"
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "query": {
                    "type": "string",
                    "description": "搜索词（用研究对象最可能被怎么写；英文通常更好搜）",
                },
                "limit": {"type": "integer", "description": "最多要几条结果，默认 8"},
            },
            "required": ["query"],
        },
    },
}

#: 「查学术」的工具声明：直连官方接口（arXiv / Crossref / GitHub），不抓页面。
_SEARCH_ACADEMIC_TOOL_SCHEMA: dict[str, Any] = {
    "type": "function",
    "function": {
        "name": "search_academic",
        "description": (
            "查学术资料：论文题录（arXiv 预印本、Crossref 正式期刊）与开源代码（GitHub）。"
            "走官方接口，**稳、不受反爬影响**，是找相关工作/找实现的主力。"
            "返回标题、链接与摘要；引用前打开链接核对。结果要不要存进论文库由你决定。"
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "query": {"type": "string", "description": "检索词（英文通常更准）"},
                "limit": {
                    "type": "integer",
                    "description": "每个来源最多要几条，默认 5",
                },
                "sources": {
                    "type": "array",
                    "items": {"type": "string", "enum": ["arxiv", "crossref", "github"]},
                    "description": "只查这几个来源（不填就三个都查）",
                },
            },
            "required": ["query"],
        },
    },
}

#: 技能相关的两个工具：`load_skill` 只读（自动放行）；`run_skill` 是执行类（要研究者点头）。
_SKILL_TOOL_SCHEMAS: list[dict[str, Any]] = [
    {
        "type": "function",
        "function": {
            "name": "load_skill",
            "description": (
                "把一个技能的完整说明加载进来（工作流程、要跑哪些脚本、注意事项）。"
                "技能清单在系统提示里，只能看到名字与一句话；**觉得要用哪个，就先 load_skill 看全文**，"
                "再按它的说明做事。只读操作，不会在研究者电脑上执行任何东西。"
            ),
            "parameters": {
                "type": "object",
                "properties": {"name": {"type": "string", "description": "技能名（清单里的那个）"}},
                "required": ["name"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "run_skill",
            "description": (
                "按某个技能**声明的流程**在研究者电脑上跑它的脚本（一次跑完它声明的步骤）。"
                "属于执行类动作：**会先请研究者确认**，卡上会列出将要执行的命令。"
                "跑出来的产物会落到项目产物目录，供引用与审计。"
                "注意：说明书型技能（没有脚本）不需要这个，直接按说明做就行。"
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "name": {"type": "string", "description": "技能名"},
                    "topic": {"type": "string", "description": "这一步要解决什么（会填进技能的 {{topic}} 占位符）"},
                    "task_id": {"type": "string", "description": "任务编号：产物按它归档（不填就用当前会话）"},
                    "project_dir": {"type": "string", "description": "在哪个项目目录下干活（不填用默认目录）"},
                },
                "required": ["name"],
            },
        },
    },
]

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


#: 工具声明的缓存（用户口径 2026-09-25：缓存 1 小时）。
#: 为什么必须缓存：`list_tools()` 每次都做一遍 **MCP stdio 握手**
#: （起子进程 → initialize → list_tools → 关），实测 947ms / 846ms，且**完全没有缓存**。
#: 指纹（mcp_server 源码 mtime + 宿主默认目录 + 出网白名单）一变立即失效。
SCHEMAS_TTL_SECONDS = 3600.0
_SCHEMAS_CACHE: dict[str, Any] = {"signature": "", "at": 0.0, "json": ""}


def _schemas_signature(default_dir: str | None) -> str:
    """工具声明的"新鲜度指纹"：MCP 子进程源码 + 宿主默认目录 + 出网白名单。

    任何一个变了就立即失效 —— 所以缓存**不牺牲正确性**，只是不再每次请求都握手一次。
    """

    root = Path(__file__).resolve().parents[2] / "mcp_server"
    newest = 0
    try:
        if root.is_dir():
            for item in root.rglob("*.py"):
                newest = max(newest, item.stat().st_mtime_ns)
    except OSError:
        return ""  # 指纹算不出来 → 不缓存
    return f"{root}={newest}|cwd={default_dir or ''}|hosts={_allowed_hosts()}"


async def tool_schemas() -> list[dict[str, Any]]:
    """OpenAI 兼容的工具声明（只含当前允许摆给模型的那两类）。**带 1 小时缓存。**

    两类来源：
    - **MCP server 提供**的（`query_library` / `fetch_url` / `run_command`）——
      清单以运行中的 server 为准，不硬编码参数；
    - **本地声明**的宿主工具（`_HOST_TOOL_SCHEMAS`）—— 它们走宿主执行器，不经过 MCP。

    ⚠️ MCP 侧拿不到**不再等于"没有工具"**：宿主工具仍要摆出去，否则执行器连不上时
    模型会以为自己什么都不能做，而不是告诉研究者"先启动执行器"。

    **必须是 async**：调用点在对话的 async 生成器里，用 `asyncio.run()` 会直接抛
    `RuntimeError: asyncio.run() cannot be called from a running event loop`。
    """

    signature = _schemas_signature(await host_runner.default_cwd())
    now = time.monotonic()
    if (
        signature
        and _SCHEMAS_CACHE["signature"] == signature
        and now - float(_SCHEMAS_CACHE["at"]) < SCHEMAS_TTL_SECONDS
    ):
        # 每次返回**全新副本**：调用方可能改动这份声明，不能让缓存被污染
        return json.loads(str(_SCHEMAS_CACHE["json"]))
    schemas, mcp_ok = await _build_tool_schemas()
    # ⚠️ **只在 MCP 握手成功时才缓存**：握手瞬时失败若被缓存下来，就会把"工具缺失"
    # 钉住一小时（研究者会看到模型突然什么工具都没有）——宁可那时每次重试。
    if signature and mcp_ok:
        _SCHEMAS_CACHE.update(
            {"signature": signature, "at": now, "json": json.dumps(schemas, ensure_ascii=False)}
        )
    return schemas


async def _build_tool_schemas() -> tuple[list[dict[str, Any]], bool]:
    """真正去装配工具声明（只在缓存未命中时走这里）。

    返回 ``(声明列表, MCP 握手是否拿到工具)`` —— 第二项决定这次结果能不能进缓存。
    """

    import json as _json

    schemas: list[dict[str, Any]] = _json.loads(_json.dumps(_HOST_TOOL_SCHEMAS))
    schemas.append(_json.loads(_json.dumps(_SEARCH_WEB_TOOL_SCHEMA)))
    schemas.append(_json.loads(_json.dumps(_SEARCH_ACADEMIC_TOOL_SCHEMA)))
    for schema in _SKILL_TOOL_SCHEMAS:
        schemas.append(_json.loads(_json.dumps(schema)))

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
        return schemas, False  # 握手失败 → 不缓存（否则会把"工具缺失"钉住一小时）
    if not listed:
        return schemas, False

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
    return schemas, True


async def judge(call: dict[str, Any]) -> policy.Verdict:
    """逐次裁决一次工具调用该「放行 / 要研究者点头 / 直接拒绝」。

    与 `requires_approval(name)` 的区别：那个只看**工具名**（写盘类一律弹卡）；
    这个看**这一次要干什么** —— 同一句 `rm`，删研究项目里的临时文件是"高危待批"，
    删 SciLoop 自己的代码是"直接拒绝"；调 `files_on_computer` 读文件则根本不用打扰研究者。
    """

    name = str((call.get("function") or {}).get("name") or "")
    args = arguments_of(call)

    if name == "run_skill":
        # 跑技能 = 在研究者电脑上按技能声明的流程真跑脚本。
        # 把**将要执行的命令**逐条送去裁决：有任一条被硬拒 → 整次拒绝；
        # 有任一条不能直接放行 → 弹卡（卡上写明这些命令），批了才跑。
        await host_runner.ensure_host_roots()
        skill_name = str(args.get("name") or "").strip()
        from services.skills import service

        pack = next((item for item in service.all_packs() if item.name == skill_name), None)
        if pack is None:
            return policy.Verdict(
                layer=policy.LAYER_OTHER,
                allowed=False,
                needs_approval=False,
                harmless=True,
                forbidden=True,
                message=f"没有这个技能：{skill_name}",
                categories=(),
            )
        info = await host_runner.ensure_host_roots()
        host_dir = runner_mod.host_pack_dir(pack, host_root=str(info.get("host_root") or ""))
        if host_dir is None:
            return policy.Verdict(
                layer=policy.LAYER_OTHER,
                allowed=False,
                needs_approval=False,
                harmless=True,
                forbidden=True,
                message="执行器没连上，跑不了技能脚本（先让研究者电脑上的执行器连上）。",
                categories=(),
            )
        plan = runner_mod.plan_commands(
            pack,
            topic=str(args.get("topic") or ""),
            host_dir=host_dir,
            work_dir=host_dir,  # 计划只为裁决路径，工作目录在执行时才定
            out_dir=host_dir,
            project_dir=str(args.get("project_dir") or ""),
            task_id=str(args.get("task_id") or "manual-run"),
        )
        commands = [" ".join(item["argv"]) for item in plan]
        verdicts = [policy.judge_command(argv=item["argv"], cwd=str(host_dir)) for item in plan]
        for verdict in verdicts:
            if verdict.forbidden:
                return verdict
        if any(not item.harmless for item in verdicts):
            return policy._approve(
                verdicts[0].layer,
                "将按技能「{}」声明的流程跑 {} 个脚本：{}".format(skill_name, len(commands), " ｜ ".join(commands)[:600]),
                "跑技能脚本",
            )
        return verdicts[0]

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


#: 错误详情里的「内部话 → 人话」映射。
#: 过程行是**给研究者看的**，不该出现工具名、MCP 的 `Error executing tool …` 包装前缀、
#: 以及整份可执行白名单（2026-09-22 实测：界面上原样出现
#: `Error executing tool run_command: {tool_denied}「pwd」不在可执行白名单内；允许：[…]`）。
#: 注意：**只改这一行**；回给模型的错误原文照旧（模型需要具体信息才能自己改正）。
_HUMAN_ERRORS: tuple[tuple[str, str], ...] = (
    ("不在可执行白名单内", "这条命令不在允许范围内（只允许读取与分析类命令）"),
    ("tool_denied", "这条命令不在允许范围内（只允许读取与分析类命令）"),
    ("workdir_outside", "不允许在这个目录下执行"),
    ("path_outside", "不允许访问工作区以外的路径"),
    ("forbidden_name", "不允许读写这个文件"),
)


def _human_error(detail: str) -> str:
    """把内部报错折叠成一句人话（识别不出就原样返回，绝不吞信息）。"""

    text = detail.strip()
    # 去掉 MCP SDK 的包装前缀：`Error executing tool run_command: …`
    if text.startswith("Error executing tool "):
        _, _, tail = text.partition(": ")
        if tail:
            text = tail.strip()
    for needle, human in _HUMAN_ERRORS:
        if needle in text:
            return human
    # 其余情况只去掉 `{code}` 花括号编码，保留具体原因
    return text.replace("{tool_denied}", "").replace("{tool_failed}", "").strip()


def search_row_payload(
    tool: str, arguments: dict[str, Any], result: dict[str, Any]
) -> dict[str, Any] | None:
    """把一次搜索的结果收成"面板能画"的形状；不是搜索工具就返回 None。

    ⚠️ 只留**面板要用的字段**：搜索词、来源、条数、标题、链接。
    摘要（snippet）留给模型用，不往会话文件里灌 —— 过程行是会落盘的。
    """

    if tool not in ("search_web", "search_academic"):
        return None
    capability = web_search.CAPABILITY_ACADEMIC if tool == "search_academic" else web_search.CAPABILITY_WEB
    label = TOOL_LABELS.get(tool, tool)
    rows = [
        {
            "title": str(item.get("title") or ""),
            "url": str(item.get("url") or ""),
            "source": str(item.get("source") or ""),
        }
        for item in (result.get("results") or [])
    ]
    return {
        "capability": capability,
        "label": label,
        "query": str(arguments.get("query") or result.get("query") or ""),
        "count": int(result.get("count") or 0),
        "sources_used": list(result.get("sources_used") or []),
        "sources_failed": list(result.get("sources_failed") or []),
        "reason": result.get("reason"),
        "results": rows,
    }


def tool_row(
    call: dict[str, Any],
    phase: str,
    detail: str = "",
    *,
    search: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """工具调用在对话里的呈现（走现有 SSE `row` 事件，前端已能渲染）。

    ``search`` 非空时挂在这一行上：界面据此画"来源 · 搜索词 · 结果"的折叠面板
    （与批准卡同一套思路：**行即卡片**，刷新后还在）。
    """

    fn = call.get("function") or {}
    name = str(fn.get("name") or "")
    label = TOOL_LABELS.get(name, name or "工具")
    if phase == "start":
        row = {"kind": "tool", "tone": "info", "text": f"调用「{label}」{detail}".strip()}
    elif phase == "ok":
        row = {"kind": "tool", "tone": "ok", "text": f"「{label}」完成：{detail}"}
    elif phase == "waiting":
        # tone=warn 而不是 err：**这不是失败，是停下来等人** —— 把两者混起来，
        # 研究者会以为工具已经出错了，而实际上它一次都没跑。
        row = {"kind": "tool", "tone": "warn", "text": f"「{label}」等待研究者批准{detail}"}
    else:
        row = {"kind": "tool", "tone": "warn", "text": f"「{label}」未完成：{_human_error(detail)}"}
    if search:
        row["search"] = search
    return row


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
    if name == "load_skill":
        from services.skills import registry, service

        skill_name = str(arguments.get("name") or "").strip()
        pack = next((item for item in service.all_packs() if item.name == skill_name), None)
        if pack is None:
            result: dict[str, Any] = {"ok": False, "error": f"没有这个技能：{skill_name}"}
        else:
            result = registry.load(pack) | {"ok": True}
        return {"ok": bool(result.get("ok")), "tool": name, "result": result}, _summarize(name, result)

    if name == "run_skill":
        from services.skills import service

        result = await service.run(
            str(arguments.get("name") or "").strip(),
            topic=str(arguments.get("topic") or ""),
            task_id=str(arguments.get("task_id") or "manual-run"),
            project_dir=str(arguments.get("project_dir") or ""),
        )
        return {"ok": bool(result.get("ok")), "tool": name, "result": result}, _summarize(name, result)

    if name == "search_academic":
        sources = arguments.get("sources")
        picked = (
            tuple(str(item) for item in sources if str(item) in web_search.SUPPORTED_SOURCES)
            if isinstance(sources, list)
            else web_search.SUPPORTED_SOURCES
        )
        result = await web_search.search_academic(
            str(arguments.get("query") or ""),
            limit=int(arguments.get("limit") or 5),
            sources=picked or web_search.SUPPORTED_SOURCES,
        )
        return {"ok": bool(result.get("ok")), "tool": name, "result": result}, _summarize(name, result)

    if name == "search_web":
        result = await web_search.search_web(
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
    if name == "load_skill":
        return "加载了技能说明" if data.get("ok") else str(data.get("error") or "技能说明没取到")

    if name == "run_skill":
        if data.get("ok"):
            return f"按技能声明的流程跑了 {len(data.get('steps') or [])} 步，产出 {len(data.get('outputs') or [])} 个文件"
        return str(data.get("message") or "这次没跑成")

    if name in ("search_web", "search_academic"):
        count = data.get("count")
        label = web_search.REASON_LABELS.get(str(data.get("reason") or ""), "")
        if not data.get("ok") and label:
            # 失败的三种原因各有各的修法，过程行里直接说清是哪一种
            return f"{label}"
        if not isinstance(count, int):
            return "搜索完成"
        used = data.get("sources_used") or []
        suffix = f"（{'、'.join(str(item) for item in used[:3])}）" if used else ""
        return f"搜到 {count} 条结果{suffix}" if count else "没搜到相关结果"

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
