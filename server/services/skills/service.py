# Copyright 2026 SciLoop contributors
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
"""技能库门面：界面读它、模型读它、执行也走它。

三件事：
- :func:`library` —— 技能库全貌（清单 + 开关 + 挂载 + 问题 + 缺哪些环境变量），界面用；
- :func:`prompt_catalog` —— **两级披露的第一级**：给模型的"名字 + 一句话"清单（省上下文的关键）；
- :func:`run` / :func:`runs` —— 跑一个技能、读执行记录（真正干活的是 `runner`）。

⚠️ 只有**启用**的技能才进模型的清单 —— 关掉的技能不该被模型选中（这是"开关"的意义）。
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from services.skills import registry, runner, state

__all__ = [
    "library",
    "prompt_catalog",
    "run",
    "runs",
    "save_skill",
]

#: 模型清单里每条技能的描述截断长度（40 多个技能，别把提示词撑爆）
CATALOG_DESC_CHARS = 160


def all_packs(*, root: Path | str | None = None) -> list[registry.SkillPack]:
    return registry.scan(*state.mounts(root=root))


def _requires_ready(pack: registry.SkillPack) -> list[str]:
    """还缺哪些环境变量（缺了就该在界面上说"这个技能现在跑不了"）。"""

    import os

    missing: list[str] = []
    for item in getattr(pack, "requires_env", []) or []:  # 兼容旧 pack 对象
        name = str(item.get("name") or "")
        if name and not (os.environ.get(name) or "").strip():
            missing.append(name)
    return missing


def library(*, root: Path | str | None = None) -> dict[str, Any]:
    """技能库全貌（界面用）：技能清单 + 开关状态 + 挂载目录 + 环节分组。"""

    packs = all_packs(root=root)
    enabled_map = state.load_state(root).get("enabled") or {}
    by_stage: dict[str, list[str]] = {}
    items: list[dict[str, Any]] = []
    for pack in packs:
        enabled = bool(enabled_map.get(pack.name, True))
        item = {
            "name": pack.name,
            "description": pack.description,
            "stage": pack.stage,
            "stage_label": pack.stage_label,
            "mode": pack.mode,
            "version": pack.version,
            "source": pack.source,
            "license": pack.license,
            "enabled": enabled,
            "usable": pack.usable,
            "runnable": pack.runnable,
            "steps": len(pack.steps),
            "scripts": len(pack.scripts),
            "problems": list(pack.problems),
            "requires_env": list(getattr(pack, "requires_env", []) or []),
            "missing_env": _requires_ready(pack),
        }
        items.append(item)
        by_stage.setdefault(pack.stage_label, []).append(pack.name)
    return {
        "items": items,
        "stages": [{"label": label, "names": names} for label, names in by_stage.items()],
        "mounts": state.mounts(root=root),
        "total": len(items),
        "enabled": sum(1 for item in items if item["enabled"]),
        "runnable": sum(1 for item in items if item["runnable"]),
        "state_loaded": bool(state.load_state(root).get("loaded", True)),
    }


def prompt_catalog(*, root: Path | str | None = None) -> list[dict[str, str]]:
    """给模型的清单：**只给名字 + 一句话 + 环节**，只给**启用**的。"""

    packs = [pack for pack in all_packs(root=root) if state.is_enabled(pack.name, root=root)]
    lines: list[dict[str, str]] = []
    for entry in registry.catalog(packs):
        description = entry.description
        if len(description) > CATALOG_DESC_CHARS:
            description = description[:CATALOG_DESC_CHARS].rstrip() + "…"
        lines.append({"name": entry.name, "description": description, "stage": entry.stage})
    return lines


async def run(
    name: str,
    *,
    topic: str,
    task_id: str,
    project_dir: str = "",
    timeout_s: int = runner.DEFAULT_STEP_TIMEOUT_S,
    root: Path | str | None = None,
    executor: Any | None = None,
) -> dict[str, Any]:
    """跑一个技能（**批准在上游**，见 `runner` 模块开头）。"""

    pack = next((item for item in all_packs(root=root) if item.name == name), None)
    if pack is None:
        return {"ok": False, "skill": name, "message": f"没有这个技能：{name}"}
    if not state.is_enabled(name, root=root):
        return {"ok": False, "skill": name, "message": f"技能「{name}」现在是关着的，先在技能库里打开。"}
    if pack.mode != "scripts" or not pack.steps:
        return {
            "ok": False,
            "skill": name,
            "message": f"技能「{name}」是说明书型（没有可跑的脚本）—— 直接按它的说明做就行，不用跑。",
        }
    if pack.problems:
        return {"ok": False, "skill": name, "message": "这个技能还有问题没解决：" + "；".join(pack.problems[:3])}

    result = await runner.run_skill(
        pack,
        topic=topic,
        task_id=task_id,
        project_dir=project_dir,
        timeout_s=timeout_s,
        executor=executor,
    )
    return result.as_dict() | {"skill": name}


def runs(task_id: str, *, artifacts: Path | None = None) -> list[dict[str, Any]]:
    """读某个任务下所有技能的执行记录（界面/审计用）。"""

    from services.translate.artifacts import default_artifact_root, task_dir

    root = artifacts if artifacts is not None else default_artifact_root()
    try:
        base = task_dir(task_id, root=root) / "skills"
    except Exception:  # noqa: BLE001 - 非法 task_id 就当没有记录
        return []
    if not base.is_dir():
        return []
    records: list[dict[str, Any]] = []
    for path in sorted(base.glob(f"*/{runner.RUN_RECORD_NAME}")):
        try:
            records.append(json.loads(path.read_text(encoding="utf-8")))
        except (json.JSONDecodeError, OSError):
            continue
    return records


def save_skill(name: str, content: str, *, root: Path | str | None = None) -> dict[str, Any]:
    """新建/编辑一个技能（写 `packs/<名>/SKILL.md`）。

    ⚠️ 只允许写在**内置技能包目录**里（研究者自己写的技能也放这儿）。
    文件名与技能名都做形态校验 —— 这是**写盘**动作，不许被 `../` 之类的东西越界。
    """

    from services.skills.registry import PACKS_DIR

    cleaned = registry.SAFE_SKILL_NAME.sub("-", (name or "").strip()).strip("-")
    if not cleaned:
        return {"ok": False, "message": "技能名不能为空（建议用英文小写与连字符，例如 my-skill）"}
    if not (content or "").strip().startswith("---"):
        return {"ok": False, "message": "SKILL.md 必须以 frontmatter（--- 开头的那一段）开始"}
    head, _body, problem = registry.split_frontmatter(content)
    if problem:
        return {"ok": False, "message": f"frontmatter 有问题：{problem}"}
    if str(head.get("name") or "").strip() != cleaned:
        return {"ok": False, "message": f"frontmatter 里的 name 必须与技能名一致（都写成 {cleaned}）"}

    target = PACKS_DIR / cleaned
    target.mkdir(parents=True, exist_ok=True)
    (target / "scripts").mkdir(exist_ok=True)
    (target / "SKILL.md").write_text(content, encoding="utf-8", newline="\n")
    pack = registry.parse_pack(target)
    return {
        "ok": not pack.problems,
        "name": pack.name,
        "path": str(target),
        "problems": pack.problems,
        "message": "已保存。" if not pack.problems else "保存了，但还有问题要修：" + "；".join(pack.problems[:3]),
    }
