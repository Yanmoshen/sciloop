# Copyright 2026 SciLoop contributors
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
"""小规模预检执行器（三级预检的 L1 / L2 落地）。

三级口径
--------
``template_smoke``     既有注册模板跑小样本（由既有执行器负责，本模块不接管）
``researcher_script``  研究者 / 模型给出的命令，走**白名单 + 超时 + 资源上限**
``isolated_runner``    独立 runner 服务（部署形态，本模块预留 ``level`` 取值）

铁律：预检记录由**程序生成**，不是模型声明的
------------------------------------------
模型输出里的 ``preflight`` 字段在编排层会被**真实记录覆盖**：退出码、耗时、
产物路径一律以这里实际执行的结果为准。没有跑过预检的话，节点不可能通过校验
（规则 R13）。这正是「模型说 completed 不改变状态」在本层的具体落地。

安全边界（首版如实说明）
------------------------
- 不做 Linux 命名空间 / 容器级隔离；靠**命令白名单 + 无 shell 执行 + 超时 + 资源上限**。
- 出现 ``pip install`` / ``npm install`` / ``apt`` / 联网下载类命令 → **必须显式授权**。
- Windows 开发环境没有 POSIX ``rlimit``，此时资源上限降级为「超时 + 输出截断」，
  并在记录里如实标注 ``limits_enforced=false``，不假装限制生效。
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import shlex
import shutil
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

logger = logging.getLogger("sciloop.research.preflight")

__all__ = [
    "ALLOWED_BINARIES",
    "PreflightResult",
    "classify_command",
    "latest_preflight",
    "list_preflights",
    "preflight_dir",
    "run_preflight",
]

#: 允许直接执行的程序（无 shell，故不存在通配符展开与管道）
ALLOWED_BINARIES: frozenset[str] = frozenset(
    {
        "python",
        "python3",
        "pytest",
        "node",
        "npx",
        "make",
        "test",
        "echo",
        "dir",
    }
)

#: 需要人工授权的动作（装依赖 / 联网 / 系统级）
_NEEDS_APPROVAL_MARKERS: tuple[tuple[str, str], ...] = (
    ("pip install", "安装 Python 依赖"),
    ("pip3 install", "安装 Python 依赖"),
    ("npm install", "安装 Node 依赖"),
    ("npm i ", "安装 Node 依赖"),
    ("apt-get", "系统级包管理"),
    ("apt ", "系统级包管理"),
    ("apk ", "系统级包管理"),
    ("curl ", "联网下载"),
    ("wget ", "联网下载"),
    ("git clone", "联网拉取代码"),
    ("docker ", "容器操作"),
)

#: 明确的注入风险字符（禁止出现，也不做转义——直接拒绝）
_SHELL_METACHARS = (";", "&&", "||", "|", "`", "$(", ">", "<", "&")


@dataclass
class PreflightResult:
    """一次预检的真实记录（可落盘、可回灌进节点契约）。"""

    level: str = "researcher_script"
    command: str = ""
    exit_code: int = -1
    duration_ms: int = 0
    artifact_path: str | None = None
    log_path: str = ""
    note: str = ""
    stdout_tail: str = ""
    stderr_tail: str = ""
    limits_enforced: bool = False
    approved: bool = False
    started_at: float = field(default_factory=time.time)

    def to_dict(self) -> dict[str, Any]:
        data = asdict(self)
        data["ok"] = self.exit_code == 0
        return data

    def to_contract(self) -> dict[str, Any]:
        """转成节点契约里 ``preflight`` 字段的形状（程序注入，覆盖模型声明）。"""

        return {
            "level": self.level,
            "command": self.command,
            "exit_code": int(self.exit_code),
            "duration_ms": int(self.duration_ms),
            "artifact_path": self.artifact_path,
            "log_path": self.log_path,
            "note": self.note,
        }


def preflight_dir(project_id: int) -> Path:
    """预检产物目录（与 conversations 同样的 .cache 约定）。"""

    base = os.environ.get("RESEARCH_PREFLIGHT_DIR") or str(
        Path(__file__).resolve().parents[3] / ".cache" / "research"
    )
    path = Path(base) / str(project_id)
    path.mkdir(parents=True, exist_ok=True)
    return path


def classify_command(command: str) -> dict[str, Any]:
    """判定命令是否可执行、是否需要人工授权。"""

    text = (command or "").strip()
    if not text:
        return {"ok": False, "reason": "命令为空", "requires_approval": False, "reasons": []}

    for meta in _SHELL_METACHARS:
        if meta in text:
            return {
                "ok": False,
                "reason": f"命令包含不允许的字符「{meta}」：预检不经过 shell 执行",
                "requires_approval": False,
                "reasons": [],
            }
    try:
        argv = shlex.split(text, posix=False)
    except ValueError as exc:
        return {"ok": False, "reason": f"命令解析失败：{exc}", "requires_approval": False, "reasons": []}
    if not argv:
        return {"ok": False, "reason": "命令为空", "requires_approval": False, "reasons": []}

    binary = Path(argv[0]).name.lower()
    if binary.endswith(".exe"):
        binary = binary[:-4]
    if binary not in ALLOWED_BINARIES:
        return {
            "ok": False,
            "reason": f"「{argv[0]}」不在允许执行的程序清单内",
            "requires_approval": False,
            "reasons": [],
        }

    lowered = f" {text.lower()} "
    reasons = [label for marker, label in _NEEDS_APPROVAL_MARKERS if marker in lowered]
    return {
        "ok": True,
        "reason": "",
        "requires_approval": bool(reasons),
        "reasons": reasons,
        "binary": binary,
    }


def _apply_limits() -> bool:
    """给子进程套资源上限。返回是否真的生效（Windows 上为 False）。"""

    try:
        import resource  # type: ignore[import-not-found]
    except ImportError:
        return False

    soft_cpu = 300
    soft_as = 4 * 1024 * 1024 * 1024
    try:
        resource.setrlimit(resource.RLIMIT_CPU, (soft_cpu, soft_cpu + 60))
        resource.setrlimit(resource.RLIMIT_AS, (soft_as, soft_as))
    except (ValueError, OSError) as exc:  # pragma: no cover - 平台差异
        logger.warning("资源上限设置失败：%s", exc)
        return False
    return True


async def run_preflight(
    *,
    project_id: int,
    command: str,
    cwd: str | None = None,
    timeout_s: int = 120,
    level: str = "researcher_script",
    approved: bool = False,
) -> PreflightResult:
    """执行一次预检。**所有失败都如实返回，不抛异常伪装成成功。**"""

    verdict = classify_command(command)
    if not verdict.get("ok"):
        return PreflightResult(
            level=level,
            command=command,
            exit_code=-1,
            note=str(verdict.get("reason") or "命令被拒绝"),
        )
    if verdict.get("requires_approval") and not approved:
        why = "、".join(verdict.get("reasons") or []) or "该操作需要人工授权"
        return PreflightResult(
            level=level,
            command=command,
            exit_code=-1,
            note=f"需要人工授权后才能执行：{why}",
        )

    try:
        argv = shlex.split(command, posix=False)
    except ValueError as exc:  # pragma: no cover - 上面已校验
        return PreflightResult(level=level, command=command, exit_code=-1, note=str(exc))

    workdir = cwd or str(preflight_dir(project_id))
    if not Path(workdir).is_dir():
        workdir = str(preflight_dir(project_id))

    started = time.perf_counter()
    limits_ok = _apply_limits()
    preexec = _apply_limits if limits_ok else None

    try:
        proc = await asyncio.create_subprocess_exec(
            *argv,
            cwd=workdir,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            preexec_fn=preexec,
        )
    except (FileNotFoundError, OSError, NotImplementedError) as exc:
        return PreflightResult(
            level=level,
            command=command,
            exit_code=-1,
            note=f"无法启动子进程：{exc}",
        )

    try:
        stdout_b, stderr_b = await asyncio.wait_for(proc.communicate(), timeout=timeout_s)
        exit_code = int(proc.returncode or 0)
        timed_out = False
    except TimeoutError:
        proc.kill()
        stdout_b, stderr_b = await proc.communicate()
        exit_code = -9
        timed_out = True

    duration_ms = int((time.perf_counter() - started) * 1000)
    stdout = (stdout_b or b"").decode("utf-8", errors="replace")
    stderr = (stderr_b or b"").decode("utf-8", errors="replace")

    directory = preflight_dir(project_id)
    stamp = time.strftime("%Y%m%d-%H%M%S", time.localtime())
    log_path = directory / f"preflight-{stamp}.log"
    log_path.write_text(
        f"$ {command}\ncwd={workdir}\nexit_code={exit_code}\n"
        f"duration_ms={duration_ms}\ntimeout={timed_out}\n\n"
        f"--- stdout ---\n{stdout}\n\n--- stderr ---\n{stderr}\n",
        encoding="utf-8",
    )

    note_parts = []
    if timed_out:
        note_parts.append(f"超过 {timeout_s}s 超时被终止")
    if not limits_ok:
        note_parts.append("当前平台未启用资源上限（仅超时与输出截断生效）")
    if exit_code != 0 and stderr.strip():
        note_parts.append(f"stderr: {stderr.strip().splitlines()[-1][:200]}")

    result = PreflightResult(
        level=level,
        command=command,
        exit_code=exit_code,
        duration_ms=duration_ms,
        artifact_path=str(log_path),
        log_path=str(log_path),
        note="；".join(note_parts),
        stdout_tail=stdout[-1500:],
        stderr_tail=stderr[-1500:],
        limits_enforced=limits_ok,
        approved=bool(approved),
    )

    (directory / f"preflight-{stamp}.json").write_text(
        json.dumps(result.to_dict(), ensure_ascii=False, indent=2), encoding="utf-8"
    )
    logger.info(
        "预检完成 project=%s level=%s exit=%s duration=%sms",
        project_id,
        level,
        exit_code,
        duration_ms,
    )
    return result


def list_preflights(project_id: int, *, limit: int = 10) -> list[dict[str, Any]]:
    """列出该项目历史预检记录（新到旧）。"""

    directory = preflight_dir(project_id)
    files = sorted(directory.glob("preflight-*.json"), reverse=True)[: max(1, min(limit, 50))]
    out: list[dict[str, Any]] = []
    for path in files:
        try:
            out.append(json.loads(path.read_text(encoding="utf-8")))
        except (OSError, json.JSONDecodeError) as exc:
            logger.warning("预检记录读取失败 %s：%s", path, exc)
    return out


def latest_preflight(project_id: int) -> dict[str, Any] | None:
    """最近一次预检记录；没有则 None（节点会因此过不了 R13）。"""

    records = list_preflights(project_id, limit=1)
    return records[0] if records else None


def which(binary: str) -> str | None:
    """该程序是否可用（供 API 展示环境就绪情况）。"""

    return shutil.which(binary)
