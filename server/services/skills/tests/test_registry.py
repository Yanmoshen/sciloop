# Copyright 2026 SciLoop contributors
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
"""技能引擎单测：扫描、两级披露、坏技能不许把整库带崩。

要钉住的事实：
1. **两级披露真的分两级** —— 清单里绝不能把技能正文带出去（那是省上下文的关键）；
2. 坏技能只记 ``problems`` 并标 ``runnable=False``，**不影响其它技能**；
3. 内置技能**优先于**挂载目录里的同名技能（演示环境不该被外部目录意外改掉）；
4. 脚本语法错误在扫描阶段就暴露，别等跑的时候才发现。
"""

from __future__ import annotations

from pathlib import Path

from services.skills import registry


def _write_skill(root: Path, name: str, frontmatter: str, body: str = "正文说明", script: str | None = None) -> Path:
    skill_dir = root / name
    (skill_dir / "scripts").mkdir(parents=True, exist_ok=True)
    (skill_dir / "SKILL.md").write_text(f"---\n{frontmatter}\n---\n\n{body}\n", encoding="utf-8")
    if script is not None:
        (skill_dir / "scripts" / "run.py").write_text(script, encoding="utf-8")
    return skill_dir


GOOD = """name: __NAME__
description: 一句话说明这个技能干什么
stage: literature
metadata:
  version: "1.0"
  source: K-Dense-AI/scientific-agent-skills
  license: MIT
steps:
  - id: strategy
    title: 生成检索策略
    script: scripts/run.py
    args: ["--topic", "{{topic}}"]
    outputs: ["search-strategy.md"]
"""


def test_scan_parses_frontmatter_and_steps(tmp_path: Path) -> None:
    _write_skill(tmp_path, "literature-review", GOOD.replace("__NAME__", "literature-review"), script="print('ok')")
    packs = registry.scan(tmp_path, packs=tmp_path)
    assert len(packs) == 1
    pack = packs[0]
    assert pack.name == "literature-review"
    assert pack.stage == "literature" and pack.stage_label == "文献调研"
    assert pack.version == "1.0" and pack.source == "K-Dense-AI/scientific-agent-skills"
    assert pack.license == "MIT"
    assert pack.problems == []
    assert pack.runnable is True
    assert len(pack.steps) == 1
    step = pack.steps[0]
    assert step.script == "scripts/run.py" and step.outputs == ["search-strategy.md"]
    assert step.args == ["--topic", "{{topic}}"]
    assert pack.scripts == ["scripts/run.py"]


def test_catalog_is_two_level_disclosure(tmp_path: Path) -> None:
    """第一级只给名字/一句话/环节 —— **正文绝不能出现在清单里**。"""

    _write_skill(
        tmp_path,
        "scientific-writing",
        GOOD.replace("__NAME__", "scientific-writing"),
        body="这一段是很长的操作说明，不该出现在清单里",
        script="print('ok')",
    )
    packs = registry.scan(tmp_path, packs=tmp_path)
    entries = registry.catalog(packs)
    assert [entry.name for entry in entries] == ["scientific-writing"]
    assert entries[0].as_dict() == {
        "name": "scientific-writing",
        "description": "一句话说明这个技能干什么",
        "stage": "literature",
    }
    assert "操作说明" not in str(entries[0].as_dict())

    detail = registry.load(packs[0])
    assert "操作说明" in detail["body"], "第二级才给正文"
    assert detail["steps"][0]["script"] == "scripts/run.py"
    assert detail["runnable"] is True


def test_missing_description_is_reported_not_fatal(tmp_path: Path) -> None:
    _write_skill(tmp_path, "no-desc", "name: no-desc\nstage: figure", script="print('ok')")
    _write_skill(tmp_path, "good-one", GOOD.replace("__NAME__", "good-one"), script="print('ok')")
    packs = {pack.name: pack for pack in registry.scan(tmp_path, packs=tmp_path)}
    assert set(packs) == {"no-desc", "good-one"}, "坏技能不许把好技能一起带崩"
    assert any("description" in problem for problem in packs["no-desc"].problems)
    assert packs["no-desc"].runnable is False
    assert packs["good-one"].runnable is True


def test_step_pointing_at_missing_script_is_reported(tmp_path: Path) -> None:
    (tmp_path / "broken").mkdir()
    (tmp_path / "broken" / "SKILL.md").write_text(
        "---\n" + GOOD.replace("__NAME__", "broken") + "---\n\n正文\n", encoding="utf-8"
    )
    pack = registry.scan(tmp_path, packs=tmp_path)[0]
    assert pack.name == "broken"
    assert any("不存在" in problem for problem in pack.problems)
    assert pack.runnable is False
    assert pack.steps == [], "脚本不存在的 step 不该进步骤清单"


def test_script_with_syntax_error_is_caught_at_scan(tmp_path: Path) -> None:
    _write_skill(tmp_path, "badscript", GOOD.replace("__NAME__", "badscript"), script="def broken(:\n")
    pack = registry.scan(tmp_path, packs=tmp_path)[0]
    assert any("语法错误" in problem for problem in pack.problems)
    assert pack.runnable is False


def test_builtin_wins_over_mounted_same_name(tmp_path: Path) -> None:
    """内置优先：挂载目录里放个同名技能，不该覆盖内置的那份。"""

    builtin = tmp_path / "builtin"
    mounted = tmp_path / "mounted"
    builtin.mkdir()
    mounted.mkdir()
    _write_skill(builtin, "pptx", GOOD.replace("__NAME__", "pptx"), body="内置版正文", script="print('ok')")
    _write_skill(mounted, "pptx", GOOD.replace("__NAME__", "pptx"), body="外部版正文", script="print('ok')")
    packs = registry.scan(mounted, packs=builtin)
    assert len(packs) == 1
    assert "内置版正文" in registry.load(packs[0])["body"]


def test_scan_of_empty_or_missing_dir_is_safe(tmp_path: Path) -> None:
    assert registry.scan(tmp_path / "not-exists", packs=tmp_path / "not-exists") == []
    assert registry.catalog([]) == []


def test_builtin_packs_dir_is_scannable() -> None:
    """内置技能目录必须能被扫（现在是空的也要不不报错）。"""

    assert registry.packs_dir().name == "packs"
    registry.scan()  # 不抛异常即通过


def test_instructions_only_skill_is_usable_not_broken(tmp_path: Path) -> None:
    """说明书型技能（上游本来就没脚本）：能用、但不算"能跑"，更不是坏技能。

    2026-09-23 实测踩到：42 个技能里 12 个是这类（networkx / polars / sympy …），
    我一开始把它们报成"这个技能跑不了" —— 那是引擎误报。
    """

    skill_dir = tmp_path / "sympy"
    skill_dir.mkdir()
    (skill_dir / "SKILL.md").write_text(
        "---\nname: sympy\ndescription: 符号计算\nstage: experiment\n---\n\n用 sympy 怎么推公式…\n",
        encoding="utf-8",
    )
    pack = registry.scan(tmp_path, packs=tmp_path)[0]
    assert pack.mode == "instructions"
    assert pack.problems == [], "没有脚本不是问题"
    assert pack.usable is True
    assert pack.runnable is False, "它确实不需要跑脚本"

    detail = registry.load(pack)
    assert detail["mode"] == "instructions" and detail["usable"] is True
    assert detail["runnable"] is False


def test_scripts_skill_is_runnable(tmp_path: Path) -> None:
    _write_skill(tmp_path, "with-script", GOOD.replace("__NAME__", "with-script"), script="print('ok')")
    pack = registry.scan(tmp_path, packs=tmp_path)[0]
    assert pack.mode == "scripts" and pack.runnable is True
