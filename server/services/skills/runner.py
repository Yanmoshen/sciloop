# Copyright 2026 SciLoop contributors
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
"""按技能声明的流程跑脚本：**走宿主执行器**（研究者电脑上跑，获批之后才跑）。

研究者 2026-09-23 定的口径：
- **按技能声明的流程跑**（`SKILL.md` 的 `steps`），不是让模型零散点脚本；
- 脚本跑在**宿主执行器**上 —— 复用现有「研究者批准后才执行」那套，看得见输出、有留痕；
- 产物落**项目产物目录**（`<server_root>/.cache/artifacts/<task_id>/skills/<技能>/`，compose 已把
  宿主 `./.data/artifacts` 绑过来），并写一份 `run.json` 执行记录。

边界（写死在代码里，别指望调用方记得）
----------------------------------------
1. **本模块不负责批准**：`run_skill()` 假定"已经获批"。批准发生在它**上游** ——
   技能执行是"执行类动作"，走 `mcp_tools.judge()` → 批准卡 → 冻结参数 → 才轮到这里。
   这样"谁批的、批的什么"仍然只有一处账。
2. **路径必须换成宿主视角**：裁决层（`policy`）与应用层看到的都是宿主路径，
   而技能包在容器里是 `/app/server/...`。用宿主执行器自报的 `host_root` 换过去
   （`policy.sciloop_root()` 是容器视角，**不能**直接递给宿主去跑）。
3. **按序、失败即停**：`required` 的步骤失败就停在那儿，如实回报，**不假装跑完**。
4. **产物按清单收**：声明了 `outputs` 就收那些；没声明就把工作目录里新出现的文件收走；
   每个产物记大小与 sha256（与 `executor/artifact_collector.py` 同一套口径）。
"""

from __future__ import annotations

import json
import os
import re
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from services.agent import host_runner, policy
from services.skills.registry import SkillPack
from services.translate.artifacts import default_artifact_root, sha256_file, task_dir

__all__ = [
    "PYTHON_ENV",
    "RUN_RECORD_NAME",
    "RunResult",
    "StepResult",
    "host_pack_dir",
    "python_bin",
    "render_args",
    "run_skill",
]

#: 跑技能脚本用的解释器（宿主上的）。默认 `python`；宿主 PATH 里不叫这个就配环境变量。
PYTHON_ENV = "SCILOOP_SKILL_PYTHON"
#: 单步默认超时（秒）；技能里可以覆盖
DEFAULT_STEP_TIMEOUT_S = 300
#: 执行记录文件名
RUN_RECORD_NAME = "run.json"

PLACEHOLDERS = ("topic", "in", "out", "skill_dir", "project_dir", "task_id")

_SAFE_NAME = re.compile(r"[^0-9A-Za-z._\-\u4e00-\u9fff]+")


def python_bin(executor_python: str | None = None) -> str:
    """跑技能脚本用哪个解释器。

    ⚠️ 优先用**执行环境自报**的那个（2026-09-24 踩过）：真机执行器报的是
    "启动它的那个 python"，容器执行器报的是自己镜像里的 python —— 两者本来就不是一个，
    让一个环境变量去压过它，就会出现"找不到这个程序：C:/Users/.../python.exe"。
    `SCILOOP_SKILL_PYTHON` 只当"执行环境没报"时的兜底。
    """

    explicit = (executor_python or "").strip()
    if explicit:
        return explicit
    return (os.environ.get(PYTHON_ENV) or "python").strip() or "python"


#: 产物目录的**卷映射**：容器 `<container>` ←→ 宿主 `<host_root>/<host_suffix>`
#: （compose：`./.data/artifacts:/app/server/.cache/artifacts`）
HOST_ARTIFACT_SUFFIX = Path(".data") / "artifacts"


def host_mirror(container_path: Path | str, *, host_root: str | None, artifacts_view: str | None = None) -> Path | None:
    """把**容器里的产物路径**换成**宿主上的同一路径**。

    ⚠️ 不加这一步，脚本在宿主上跑时会拿到 `/app/server/...` 这种路径 → "工作目录不存在"（实测踩过）。
    拿不到宿主根就返回 None，由调用方决定怎么办（**不猜**）。
    """

    if not host_root:
        return None
    path = Path(container_path)
    container_root = default_artifact_root()
    try:
        relative = path.resolve().relative_to(container_root.resolve())
    except ValueError:
        return None
    # 执行环境**自报**它把产物目录挂在哪（容器执行器是 /artifacts；宿主机执行器没报，就用默认的 .data/artifacts）
    view = str(artifacts_view or "").strip()
    if view:
        return Path(view) / relative
    return Path(host_root) / HOST_ARTIFACT_SUFFIX / relative


def _safe(value: str) -> str:
    """把技能名/主题收成安全的目录名（中文保留，其余危险字符换掉）。"""

    cleaned = _SAFE_NAME.sub("-", (value or "").strip()).strip("-")
    return cleaned[:60] or "unnamed"


def host_pack_dir(pack: SkillPack, *, host_root: str | None = None) -> Path | None:
    """技能包在**宿主**上的路径。

    技能包在容器里是 `/app/server/services/skills/packs/<名>`，而执行器跑在宿主上 ——
    直接把容器路径递过去必然找不到。这里用宿主自报的根做换算（拿不到就返回 None，由调用方决定怎么办）。
    """

    if pack.path is None:
        return None
    root = Path(host_root) if host_root else None
    if root is None:
        return None
    try:
        relative = pack.path.resolve().relative_to(policy.sciloop_root().resolve())
    except ValueError:
        return None
    return root / relative


def render_args(
    template: list[str],
    *,
    topic: str,
    in_path: str,
    out_dir: str,
    skill_dir: str,
    project_dir: str,
    task_id: str,
) -> list[str]:
    """把 `{{topic}}` 这类占位换成真实值（只认白名单里的四个键，避免误替换）。"""

    values = {
        "topic": topic,
        "in": in_path,
        "out": out_dir,
        "skill_dir": skill_dir,
        "project_dir": project_dir,
        "task_id": task_id,
    }
    rendered: list[str] = []
    for item in template:
        text = str(item)
        for key, value in values.items():
            text = text.replace("{{" + key + "}}", value)
        rendered.append(text)
    return rendered


@dataclass
class StepResult:
    id: str
    title: str
    script: str
    argv: list[str]
    cwd: str
    ok: bool
    exit_code: int | None = None
    stdout: str = ""
    error: str = ""
    duration_s: float = 0.0

    def as_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "title": self.title,
            "script": self.script,
            "argv": self.argv,
            "cwd": self.cwd,
            "ok": self.ok,
            "exit_code": self.exit_code,
            "stdout_tail": self.stdout[-2000:],
            "error": self.error,
            "duration_s": round(self.duration_s, 2),
        }


@dataclass
class RunResult:
    """一次技能执行的全貌（**要落盘**，界面与审计都用它）。"""

    skill: str
    topic: str
    ok: bool
    steps: list[StepResult] = field(default_factory=list)
    outputs: list[dict[str, Any]] = field(default_factory=list)
    work_dir: str = ""
    artifact_dir: str = ""
    record_path: str = ""
    stopped_at: str = ""
    message: str = ""
    plan: list[dict[str, Any]] = field(default_factory=list)

    def as_dict(self) -> dict[str, Any]:
        return {
            "skill": self.skill,
            "topic": self.topic,
            "ok": self.ok,
            "steps": [step.as_dict() for step in self.steps],
            "outputs": self.outputs,
            "work_dir": self.work_dir,
            "artifact_dir": self.artifact_dir,
            "stopped_at": self.stopped_at,
            "message": self.message,
        }


def plan_commands(
    pack: SkillPack,
    *,
    topic: str,
    host_dir: Path,
    work_dir: Path,
    out_dir: Path,
    project_dir: str,
    task_id: str,
    executor_python: str | None = None,
) -> list[dict[str, Any]]:
    """把 `steps` 渲染成"将要执行什么" —— **批准卡上给人看的就是这份**。"""

    plan: list[dict[str, Any]] = []
    for step in pack.steps:
        argv = [python_bin(executor_python)]
        argv.append(str(host_dir / step.script))
        argv.extend(
            render_args(
                step.args,
                topic=topic,
                in_path=str(work_dir),
                out_dir=str(out_dir),
                skill_dir=str(host_dir),
                project_dir=project_dir,
                task_id=task_id,
            )
        )
        plan.append({"id": step.id, "title": step.title, "argv": argv, "outputs": list(step.outputs)})
    return plan


async def run_skill(
    pack: SkillPack,
    *,
    topic: str,
    task_id: str,
    project_dir: str = "",
    timeout_s: int = DEFAULT_STEP_TIMEOUT_S,
    host_info: dict[str, Any] | None = None,
    artifacts: Path | None = None,
    executor: Any | None = None,
) -> RunResult:
    """按 `steps` 顺序跑一个技能。**不负责批准**（见模块开头第 1 条）。

    ``executor`` 可注入（测试用）：它得是一个 ``async def (argv, cwd, timeout_s) -> dict``。
    默认走 `host_runner.call_exec`。
    """

    info = host_info if host_info is not None else await host_runner.ensure_host_roots()
    host_root = str(info.get("host_root") or "") or None
    host_dir = host_pack_dir(pack, host_root=host_root)

    result = RunResult(skill=pack.name, topic=topic, ok=False)

    root = artifacts if artifacts is not None else default_artifact_root()
    try:
        base = task_dir(task_id, root=root) / "skills" / _safe(pack.name)
    except Exception as exc:  # noqa: BLE001 - 非法 task_id 之类的口径问题，说清楚就好，别抛
        result.message = f"这个任务编号没法用来归档产物（{task_id!r}）：{exc}"
        return result
    work_dir = base / "work"
    work_dir.mkdir(parents=True, exist_ok=True)
    result.work_dir = str(work_dir)
    result.artifact_dir = str(base)

    if host_dir is None:
        result.message = (
            "找不到这个技能在研究者电脑上的位置（执行器没连上，或技能包不在代码树里）。"
            "先确认执行器是连着的，再重试。"
        )
        return result

    project = project_dir or str(info.get("default_cwd") or host_root or "")
    # ⚠️ 给脚本的参数必须是**宿主路径**（脚本在宿主上跑）；读产物仍用容器路径（卷映射，同一份文件）
    view = str(info.get("artifacts_root") or "")
    host_work = host_mirror(work_dir, host_root=host_root, artifacts_view=view) or work_dir
    host_out = host_mirror(base, host_root=host_root, artifacts_view=view) or base
    plan = plan_commands(
        pack,
        topic=topic,
        host_dir=host_dir,
        work_dir=host_work,
        out_dir=host_out,
        project_dir=project,
        task_id=task_id,
        executor_python=str(info.get("python") or "") or None,
    )
    result.plan = plan

    def run_one(argv: list[str], step_timeout: int) -> Any:
        if executor is not None:
            return executor(argv=argv, cwd=str(work_dir), timeout_s=step_timeout)
        return host_runner.call_exec(argv=argv, cwd=str(host_work), timeout_s=step_timeout)

    before = {path.name for path in work_dir.rglob("*") if path.is_file()}
    ok_all = True
    for step, item in zip(pack.steps, plan, strict=False):
        started = time.monotonic()
        payload: dict[str, Any] = {}
        error = ""
        try:
            payload = await run_one(item["argv"], timeout_s)
        except Exception as exc:  # noqa: BLE001 - 任何一种失败都要如实记下来
            error = f"{type(exc).__name__}: {exc}"[:300]
        exit_code = payload.get("exit_code")
        step_ok = bool(payload.get("ok")) and (exit_code in (0, None))
        stdout = str(payload.get("stdout") or payload.get("output") or "")
        if not step_ok and not error:
            error = str(payload.get("error") or payload.get("stderr") or "")[:300] or "这一步没跑成"

        result.steps.append(
            StepResult(
                id=step.id,
                title=step.title,
                script=step.script,
                argv=item["argv"],
                cwd=str(host_work),
                ok=step_ok,
                exit_code=int(exit_code) if isinstance(exit_code, int) else None,
                stdout=stdout,
                error=error,
                duration_s=time.monotonic() - started,
            )
        )
        if not step_ok and step.required:
            ok_all = False
            result.stopped_at = step.id
            result.message = f"第 {step.id} 步（{step.title or step.script}）没跑成，后面没继续。"
            break

    # ---- 收产物：声明的 outputs 优先；没声明就收工作目录里新出现的文件 ----
    collected: list[Path] = []
    declared = [item for step in pack.steps for item in step.outputs]
    if declared:
        for name in declared:
            candidate = base / name
            if candidate.is_file():
                collected.append(candidate)
    else:
        collected = [path for path in work_dir.rglob("*") if path.is_file() and path.name not in before]

    result.outputs = [
        {
            "name": str(path.relative_to(base)) if base in path.parents or path.parent == base else path.name,
            "bytes": path.stat().st_size,
                "sha256": sha256_file(path) or "",
        }
        for path in collected
    ]

    result.ok = ok_all and bool(result.steps)
    if result.ok and not result.message:
        result.message = f"跑完 {len(result.steps)} 步，产出 {len(result.outputs)} 个文件。"

    record = {
        **result.as_dict(),
        "planned": plan,
        "python": python_bin(str(info.get("python") or "") or None),
        "host_pack_dir": str(host_dir),
        "finished_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
    }
    record_path = base / RUN_RECORD_NAME
    record_path.write_text(json.dumps(record, ensure_ascii=False, indent=2), encoding="utf-8")
    result.record_path = str(record_path)
    return result
