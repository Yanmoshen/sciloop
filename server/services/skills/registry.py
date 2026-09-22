# Copyright 2026 SciLoop contributors
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
"""技能包：扫描 / 解析 / **两级披露**。

什么叫技能
----------
一个技能 = 一个目录：``SKILL.md``（给模型看的操作说明）+ ``scripts/``（**真能跑的脚本**）+
``references/``（参考资料）。兼容 Agent Skills 标准（frontmatter 里的 ``name`` / ``description``），
本产品另外扩展了四个字段：

- ``stage``：归到哪个环节（文献调研 / 实验设计 / 写作交付 …），界面按它分组；
- ``steps``：**流程声明** —— 这一步要跑哪些脚本、参数模板、产出什么
  （研究者 2026-09-23 定的口径：**按技能声明的流程跑**，而不是让模型零散点脚本）；
- ``source`` / ``license``：来源与许可，界面与 NOTICE 要如实显示，别含糊。

两级披露（省上下文的关键）
--------------------------
- :func:`catalog` 只给模型 **name + description + stage**（几十个技能的清单也就一屏）；
- :func:`load` 才给正文、脚本清单与步骤 —— 模型选中哪个才加载哪个。

⚠️ 扫描**永不抛异常**：坏技能只记 ``problems`` 并标 ``runnable=False``，
界面能看见"哪个技能为什么不能跑"，而不是整个技能库消失。
"""

from __future__ import annotations

import ast
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml

__all__ = [
    "PACKS_DIR",
    "STAGE_LABELS",
    "CatalogEntry",
    "SkillPack",
    "SkillStep",
    "SAFE_SKILL_NAME",
    "catalog",
    "load",
    "packs_dir",
    "scan",
    "split_frontmatter",
]

#: 内置技能包位置（``server/services/skills/packs/``）——
#: 放这里是为了**不新增挂载**：``server/services`` 本来就已经挂进后端容器，镜像也会带上它。
PACKS_DIR = Path(__file__).resolve().parent / "packs"

#: 技能名的安全形态（中文保留；路径分隔符等危险字符一律换掉）—— 新建技能时要校验
SAFE_SKILL_NAME = re.compile(r"[^0-9A-Za-z._\-\u4e00-\u9fff]+")

#: 环节 → 给人看的中文名（界面与提示词共用一份口径）
STAGE_LABELS: dict[str, str] = {
    "literature": "文献调研",
    "ideation": "选题与假设",
    "experiment": "实验与统计",
    "figure": "图表与可视化",
    "writing": "写作与交付",
    "general": "通用",
}


@dataclass
class SkillStep:
    """技能声明的一步：跑哪个脚本、给什么参数、产出什么。"""

    id: str
    title: str
    script: str
    args: list[str] = field(default_factory=list)
    outputs: list[str] = field(default_factory=list)
    required: bool = True


@dataclass
class SkillPack:
    """一个技能包（已解析）。"""

    name: str
    description: str
    stage: str = "general"
    version: str = ""
    source: str = ""
    license: str = ""
    body: str = ""
    path: Path | None = None
    steps: list[SkillStep] = field(default_factory=list)
    scripts: list[str] = field(default_factory=list)
    references: list[str] = field(default_factory=list)
    problems: list[str] = field(default_factory=list)
    #: 上游声明的环境变量：[{"name": "OPENROUTER_API_KEY", "required": False}]
    requires_env: list[dict[str, Any]] = field(default_factory=list)
    #: `scripts` = 带脚本、能真跑；`instructions` = 说明书型（模型读说明自己写代码/自己算）
    mode: str = "instructions"

    @property
    def runnable(self) -> bool:
        """能不能**真跑脚本**（说明书型本来就不需要跑，不算坏）。"""

        return self.mode == "scripts" and not self.problems

    @property
    def usable(self) -> bool:
        """这一份技能能不能用：说明书型也能用，只是不跑脚本。"""

        return not self.problems

    @property
    def stage_label(self) -> str:
        return STAGE_LABELS.get(self.stage, self.stage or STAGE_LABELS["general"])


@dataclass
class CatalogEntry:
    """两级披露里的**第一级**：只给模型看这一小撮字段。"""

    name: str
    description: str
    stage: str
    source: str = ""

    def as_dict(self) -> dict[str, Any]:
        return {"name": self.name, "description": self.description, "stage": self.stage}


def packs_dir() -> Path:
    return PACKS_DIR


def split_frontmatter(text: str) -> tuple[dict[str, Any], str, str | None]:
    """拆出 YAML frontmatter 与正文；没 frontmatter 就返回空头（不报错）。"""

    stripped = text.lstrip("\ufeff")
    if not stripped.startswith("---"):
        return {}, stripped, "缺少 frontmatter（SKILL.md 必须以 --- 开头）"
    parts = stripped.split("---", 2)
    if len(parts) < 3:
        return {}, stripped, "frontmatter 没有闭合的 ---"
    try:
        head = yaml.safe_load(parts[1]) or {}
    except yaml.YAMLError as exc:
        return {}, parts[2], f"frontmatter 不是合法 YAML：{str(exc)[:80]}"
    if not isinstance(head, dict):
        return {}, parts[2], "frontmatter 不是一个映射"
    return head, parts[2], None


def _as_list(value: Any) -> list[str]:
    if value is None:
        return []
    if isinstance(value, str):
        return [value]
    if isinstance(value, list):
        return [str(item) for item in value]
    return []


def _parse_steps(raw: Any, skill_dir: Path, problems: list[str]) -> list[SkillStep]:
    """解析 ``steps``：只认声明清楚、脚本真实存在的步骤。"""

    if not isinstance(raw, list):
        return []
    steps: list[SkillStep] = []
    for index, item in enumerate(raw, start=1):
        if not isinstance(item, dict):
            problems.append(f"第 {index} 个 step 不是映射")
            continue
        script = str(item.get("script") or "").strip()
        if not script:
            problems.append(f"第 {index} 个 step 没写 script")
            continue
        if not (skill_dir / script).is_file():
            problems.append(f"step {item.get('id') or index} 声明的脚本不存在：{script}")
            continue
        steps.append(
            SkillStep(
                id=str(item.get("id") or f"step{index}"),
                title=str(item.get("title") or ""),
                script=script,
                args=[str(arg) for arg in _as_list(item.get("args"))],
                outputs=_as_list(item.get("outputs")),
                required=bool(item.get("required", True)),
            )
        )
    return steps


def parse_pack(skill_dir: Path) -> SkillPack:
    """解析一个技能目录（**只读、不抛异常**）。"""

    skill_md = skill_dir / "SKILL.md"
    problems: list[str] = []
    if not skill_md.is_file():
        return SkillPack(
            name=skill_dir.name,
            description="",
            path=skill_dir,
            problems=[f"缺少 SKILL.md：{skill_md}"],
        )

    head, body, head_problem = split_frontmatter(skill_md.read_text(encoding="utf-8", errors="replace"))
    if head_problem:
        problems.append(head_problem)

    meta = head.get("metadata") if isinstance(head.get("metadata"), dict) else {}
    name = str(head.get("name") or skill_dir.name).strip()
    description = " ".join(str(head.get("description") or "").split())
    if not description:
        problems.append("缺 description（模型选技能只看这一句）")
    if name != skill_dir.name:
        problems.append(f"frontmatter 的 name（{name}）与目录名（{skill_dir.name}）不一致")

    scripts_dir = skill_dir / "scripts"
    scripts = (
        sorted(str(path.relative_to(skill_dir)) for path in scripts_dir.rglob("*") if path.is_file())
        if scripts_dir.is_dir()
        else []
    )
    references_dir = skill_dir / "references"
    references = (
        sorted(str(path.relative_to(skill_dir)) for path in references_dir.rglob("*") if path.is_file())
        if references_dir.is_dir()
        else []
    )

    steps = _parse_steps(head.get("steps"), skill_dir, problems)
    # ⚠️ 上游有些技能**本来就没有脚本**（说明书型）。那不是坏技能，别报成问题
    # （2026-09-23 实测：42 个里 12 个被误报，改完才算准）。
    mode = "scripts" if (steps or scripts) else "instructions"

    requires: list[dict[str, Any]] = []
    raw_env = head.get("requires_env")
    if isinstance(raw_env, list):
        for item in raw_env:
            if isinstance(item, dict) and item.get("name"):
                requires.append({"name": str(item["name"]), "required": bool(item.get("required", False))})

    version = str(meta.get("version") or head.get("version") or "")
    return SkillPack(
        name=name,
        description=description,
        stage=str(head.get("stage") or "general").strip() or "general",
        version=version,
        source=str(meta.get("source") or head.get("source") or ""),
        license=str(meta.get("license") or head.get("license") or ""),
        body=body.strip(),
        path=skill_dir,
        steps=steps,
        scripts=scripts,
        references=references,
        problems=problems,
        mode=mode,
        requires_env=requires,
    )


def check_script_syntax(path: Path) -> str | None:
    """Python 脚本语法自检：坏脚本提前暴露，别等跑的时候才发现。"""

    if path.suffix != ".py":
        return None
    try:
        ast.parse(path.read_text(encoding="utf-8", errors="replace"))
    except SyntaxError as exc:
        return f"{path.name} 语法错误：第 {exc.lineno} 行 {exc.msg}"
    return None


def scan(*extra_dirs: Path | str, packs: Path | None = None) -> list[SkillPack]:
    """扫描内置技能包目录 + 研究者挂载的目录；同名以**先出现的**为准。"""

    roots = [packs or PACKS_DIR, *[Path(item) for item in extra_dirs]]
    found: dict[str, SkillPack] = {}
    for root in roots:
        if not root.is_dir():
            continue
        for skill_dir in sorted(path for path in root.iterdir() if path.is_dir()):
            if not (skill_dir / "SKILL.md").is_file():
                continue
            if skill_dir.name in found:
                # 内置优先：挂载目录里的同名技能不覆盖（免得演示环境被意外改掉）
                continue
            pack = parse_pack(skill_dir)
            for script in pack.scripts:
                problem = check_script_syntax(skill_dir / script)
                if problem:
                    pack.problems.append(problem)
            found[pack.name] = pack
    return sorted(found.values(), key=lambda item: (item.stage, item.name))


def catalog(skills: list[SkillPack]) -> list[CatalogEntry]:
    """两级披露的**第一级**：只给模型 name + description + stage。"""

    return [
        CatalogEntry(name=pack.name, description=pack.description, stage=pack.stage, source=pack.source)
        for pack in skills
    ]


def load(skill: SkillPack) -> dict[str, Any]:
    """两级披露的**第二级**：正文 + 脚本/参考清单 + 步骤声明。"""

    return {
        "name": skill.name,
        "description": skill.description,
        "stage": skill.stage,
        "stage_label": skill.stage_label,
        "version": skill.version,
        "source": skill.source,
        "license": skill.license,
        "body": skill.body,
        "scripts": skill.scripts,
        "references": skill.references,
        "steps": [
            {
                "id": step.id,
                "title": step.title,
                "script": step.script,
                "args": step.args,
                "outputs": step.outputs,
                "required": step.required,
            }
            for step in skill.steps
        ],
        "mode": skill.mode,
        "requires_env": skill.requires_env,
        "runnable": skill.runnable,
        "usable": skill.usable,
        "problems": skill.problems,
    }
