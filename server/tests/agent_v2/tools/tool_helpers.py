"""工具线测试辅助（Agent 2）。

约定：

- 所有 fake 后端都在这里，**测试不联网**；
- ``make_call`` 统一构造契约 ``ToolCall``（kind 与真实类别一致，避免调度语义失真）。
"""

from __future__ import annotations

import asyncio
import contextlib
import os
import subprocess
import sys
from pathlib import Path
from typing import Any

from contracts.agent_v2 import ToolCall, ToolCallStatus, ToolKind, new_id
from services.tool_registry_v2.models import (
    IdempotencyMode,
    PermissionClass,
    SideEffect,
    ToolDefinition,
)


def run(coro: Any) -> Any:
    """在同步测试里跑协程（不引入额外插件依赖）。"""
    return asyncio.run(coro)


def make_call(
    name: str,
    arguments: dict[str, Any] | None = None,
    *,
    kind: ToolKind | str = ToolKind.SIDE_EFFECT,
    call_id: str | None = None,
    thread_id: str | None = None,
    turn_id: str | None = None,
) -> ToolCall:
    return ToolCall(
        call_id=call_id or new_id("call"),
        name=name,
        kind=ToolKind(kind),
        status=ToolCallStatus.REQUESTED,
        thread_id=thread_id or new_id("thread"),
        turn_id=turn_id or new_id("turn"),
        arguments=dict(arguments or {}),
    )


def read_only_call(name: str, arguments: dict[str, Any] | None = None, **kw: Any) -> ToolCall:
    return make_call(name, arguments, kind=ToolKind.READ_ONLY, **kw)


# ---------------------------------------------------------------------------- fake 后端
def fake_backends() -> dict[str, Any]:
    """全部依赖注入型后端的离线实现。"""

    async def search(query: str, mode: str, limit: int) -> dict[str, Any]:
        return {
            "query": query,
            "mode": mode,
            "limit": limit,
            "results": [
                {"title": f"{mode}-result-1", "url": "https://example.invalid/1"},
            ],
        }

    async def fetch(url: str, max_bytes: int) -> dict[str, Any]:
        return {"url": url, "max_bytes": max_bytes, "text": "fetched body", "truncated": False}

    async def kb_query(payload: dict[str, Any]) -> dict[str, Any]:
        return {"items": [{"id": "kb-1", "name": "示例.md"}], "query": payload}

    async def kb_write(payload: dict[str, Any]) -> dict[str, Any]:
        return {"id": "kb-new", "name": payload.get("name"), "created": True}

    async def skill_load(name: str, version: str | None) -> dict[str, Any]:
        return {"name": name, "version": version or "1.0.0", "body": "# 技能说明"}

    async def skill_run(name: str, args: list[str], cwd: str | None) -> dict[str, Any]:
        return {"name": name, "args": args, "cwd": cwd, "exit_code": 0}

    async def mcp_call(tool: str, arguments: dict[str, Any], server: str | None) -> dict[str, Any]:
        return {"tool": tool, "server": server, "result": arguments}

    return {
        "search": search,
        "fetch": fetch,
        "kb_query": kb_query,
        "kb_write": kb_write,
        "skill_load": skill_load,
        "skill_run": skill_run,
        "mcp_call": mcp_call,
    }


def delayed_tool(
    name: str, *, delay_s: float, permission: PermissionClass = PermissionClass.READ
) -> ToolDefinition:
    """带延时的工具（用于观测并行/串行）。

    元数据如实声明：只读 + 无副作用才允许并行；写/执行类必须串行并声明副作用性质。
    """

    async def handler(args: dict[str, Any], ctx: Any) -> dict[str, Any]:
        await asyncio.sleep(delay_s)
        return {"name": name, "slept": delay_s}

    side_effect = SideEffect.NONE if permission is PermissionClass.READ else SideEffect.FILESYSTEM
    return ToolDefinition(
        name=name,
        permission=permission,
        description=f"delayed {name}",
        input_schema={"type": "object", "additionalProperties": True},
        output_schema={"type": "object", "additionalProperties": True},
        handler=handler,
        side_effect=side_effect,
        parallel_safe=permission is PermissionClass.READ,
        idempotency=IdempotencyMode.NONE,
    )


def python_bin() -> str:
    """当前解释器（子进程测试用，避免依赖 PATH）。"""
    return sys.executable or "python"


def make_escape_link(link: Path, target: Path) -> str:
    """在工作区内造一个指向区外的**目录链接**，返回实际用上的方式。

    Windows 的现实情况（实测）：
    - ``Path.symlink_to`` 在无权限/受限环境里可能"返回成功但没建出链接"
      （``islink=False``、``reparse_tag=0``），因此**必须验证结果**；
    - ``mklink /J``（junction）不需要管理员，是更现实的越界向量，且
      ``os.path.realpath`` 能正确解析它。

    返回 ``"symlink"`` / ``"junction"`` / ``"none"``（都失败时调用方 skip）。
    """
    link.parent.mkdir(parents=True, exist_ok=True)
    with contextlib.suppress(OSError):
        link.symlink_to(target, target_is_directory=True)
        if os.path.islink(link):
            return "symlink"

    if os.name == "nt":
        result = subprocess.run(
            ["cmd", "/c", "mklink", "/J", str(link), str(target)],
            capture_output=True,
            check=False,
        )
        del result  # 只看结果，不看输出（中文系统是 GBK）
        if link.exists() and (os.path.islink(link) or _is_reparse_point(link)):
            return "junction"
    return "none"


def _is_reparse_point(path: Path) -> bool:
    try:
        return bool(os.lstat(path).st_reparse_tag)
    except (AttributeError, OSError):
        return False


def write_heartbeat_script(directory: Path, filename: str = "hb.txt", period_s: float = 0.15) -> Path:
    """写一个"心跳"脚本：孙进程每 period_s 秒改写一次文件。

    进程树被终止后，文件 mtime 必须**停止变化**——这是"孙进程真的死了"的可复核证据。
    """
    script = directory / "heartbeat.py"
    script.write_text(
        "import sys, time, pathlib\n"
        "target = pathlib.Path(sys.argv[1])\n"
        "period = float(sys.argv[2])\n"
        "while True:\n"
        "    target.write_text(str(time.time()))\n"
        "    time.sleep(period)\n",
        encoding="utf-8",
    )
    return script


def spawn_tree_script(directory: Path) -> Path:
    """写一个"父进程起孙进程然后自己长睡"的脚本（用来验证进程树终止）。"""
    script = directory / "tree_parent.py"
    script.write_text(
        "import subprocess, sys, time, pathlib\n"
        "heart = sys.argv[1]\n"
        "period = sys.argv[2]\n"
        "child = subprocess.Popen([sys.executable, sys.argv[3], heart, period])\n"
        "pathlib.Path(heart + '.childpid').write_text(str(child.pid))\n"
        "time.sleep(60)\n",
        encoding="utf-8",
    )
    return script
