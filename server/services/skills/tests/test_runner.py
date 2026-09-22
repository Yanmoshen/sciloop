# Copyright 2026 SciLoop contributors
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
"""技能执行器单测（假执行器，不碰真机器、不跑真脚本）。

要钉住的事实：
1. **按序、失败即停**：required 的步骤失败就不许往下跑，而且要如实说停在哪一步；
2. **不连执行器就不许假装跑过**：拿不到宿主机位置时直接说清，而不是"跑完了没产物"；
3. **产物按声明收，并记 sha256**（与 `executor/artifact_collector.py` 同一套口径）；
4. **占位符替换只认白名单键**，别把 {{topic}} 之外的字符串误替换掉；
5. 执行记录 `run.json` 落盘，含每一步的命令、退出码、输出尾巴。
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path

import pytest

from services.skills import runner
from services.skills.registry import SkillPack, SkillStep


def _pack(path: Path, *, steps: list[SkillStep]) -> SkillPack:
    return SkillPack(
        name="demo-skill",
        description="演示用",
        stage="figure",
        path=path,
        steps=steps,
        scripts=[step.script for step in steps],
        mode="scripts",
    )


def _step(step_id: str, *, outputs: list[str] | None = None, required: bool = True) -> SkillStep:
    return SkillStep(
        id=step_id,
        title=f"第 {step_id} 步",
        script=f"scripts/{step_id}.py",
        args=["--topic", "{{topic}}", "--out", "{{out}}"],
        outputs=outputs or [],
        required=required,
    )


def test_run_skill_happy_path_collects_declared_outputs(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    pack_dir = tmp_path / "container" / "services" / "skills" / "packs" / "demo-skill"
    (pack_dir / "scripts").mkdir(parents=True)
    (pack_dir / "scripts" / "one.py").write_text("print('one')", encoding="utf-8")
    (pack_dir / "scripts" / "two.py").write_text("print('two')", encoding="utf-8")
    monkeypatch.setattr(runner.policy, "sciloop_root", lambda: tmp_path / "container")

    pack = _pack(pack_dir, steps=[_step("one", outputs=["figure.png"]), _step("two")])
    calls: list[dict] = []

    async def fake_exec(*, argv: list[str], cwd: str, timeout_s: int) -> dict:
        calls.append({"argv": argv, "cwd": cwd})
        # 第一步声明产出 figure.png —— 造出来，看收没收
        if "one.py" in " ".join(argv):
            (Path(cwd).parent / "figure.png").write_bytes(b"PNG")
        return {"ok": True, "exit_code": 0, "stdout": f"ran {argv[-2]}"}

    result = asyncio.run(
        runner.run_skill(
            pack,
            topic="子词分词公平性",
            task_id="task-0001",
            host_info={"host_root": str(tmp_path / "host"), "default_cwd": str(tmp_path / "host" / "proj")},
            artifacts=tmp_path / "artifacts",
            executor=fake_exec,
        )
    )

    assert result.ok is True
    assert [step.id for step in result.steps] == ["one", "two"]
    assert len(calls) == 2, "两步都跑了"
    # 路径换成了宿主视角，而不是容器视角
    assert calls[0]["argv"][0] == runner.python_bin()
    assert str(tmp_path / "host" / "services" / "skills" / "packs" / "demo-skill" / "scripts" / "one.py") == calls[0]["argv"][1]
    # 占位符替换
    assert "--topic" in calls[0]["argv"] and "子词分词公平性" in calls[0]["argv"]
    # 产物
    assert [item["name"] for item in result.outputs] == ["figure.png"]
    assert result.outputs[0]["bytes"] == 3 and len(result.outputs[0]["sha256"]) == 64
    # 执行记录
    record = json.loads(Path(result.record_path).read_text(encoding="utf-8"))
    assert record["skill"] == "demo-skill" and record["ok"] is True
    assert record["steps"][0]["exit_code"] == 0 and "ran" in record["steps"][0]["stdout_tail"]
    assert record["planned"][0]["argv"][1].endswith("one.py")


def test_required_step_failure_stops_the_flow(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    pack_dir = tmp_path / "container" / "services" / "skills" / "packs" / "demo-skill"
    pack_dir.mkdir(parents=True)
    monkeypatch.setattr(runner.policy, "sciloop_root", lambda: tmp_path / "container")
    pack = _pack(pack_dir, steps=[_step("one"), _step("two"), _step("three")])

    seen: list[str] = []

    async def fake_exec(*, argv: list[str], cwd: str, timeout_s: int) -> dict:
        script = argv[1]
        seen.append(Path(script).name)
        if script.endswith("two.py"):
            return {"ok": False, "exit_code": 2, "stderr": "boom"}
        return {"ok": True, "exit_code": 0, "stdout": "fine"}

    result = asyncio.run(
        runner.run_skill(
            pack,
            topic="t",
            task_id="task-0002",
            host_info={"host_root": str(tmp_path / "host")},
            artifacts=tmp_path / "artifacts",
            executor=fake_exec,
        )
    )

    assert seen == ["one.py", "two.py"], "第三步不该被跑"
    assert result.ok is False
    assert result.stopped_at == "two"
    assert "没跑成" in result.message
    assert result.steps[1].ok is False and result.steps[1].exit_code == 2


def test_optional_step_failure_does_not_stop(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    pack_dir = tmp_path / "container" / "services" / "skills" / "packs" / "demo-skill"
    pack_dir.mkdir(parents=True)
    monkeypatch.setattr(runner.policy, "sciloop_root", lambda: tmp_path / "container")
    pack = _pack(pack_dir, steps=[_step("one", required=False), _step("two")])

    async def fake_exec(*, argv: list[str], cwd: str, timeout_s: int) -> dict:
        if argv[1].endswith("one.py"):
            return {"ok": False, "exit_code": 1, "stderr": "可选步骤失败"}
        return {"ok": True, "exit_code": 0}

    result = asyncio.run(
        runner.run_skill(
            pack,
            topic="t",
            task_id="task-0003",
            host_info={"host_root": str(tmp_path / "host")},
            artifacts=tmp_path / "artifacts",
            executor=fake_exec,
        )
    )
    assert result.ok is True
    assert [step.ok for step in result.steps] == [False, True]
    assert result.stopped_at == ""


def test_without_host_root_it_says_so_and_runs_nothing(tmp_path: Path) -> None:
    pack = _pack(tmp_path / "packs" / "demo-skill", steps=[_step("one")])
    called = False

    async def fake_exec(*, argv: list[str], cwd: str, timeout_s: int) -> dict:
        nonlocal called
        called = True
        return {"ok": True, "exit_code": 0}

    result = asyncio.run(
        runner.run_skill(
            pack,
            topic="t",
            task_id="task-0004",
            host_info={},
            artifacts=tmp_path / "artifacts",
            executor=fake_exec,
        )
    )
    assert called is False, "没连上执行器就不许跑"
    assert result.ok is False
    assert "执行器" in result.message


def test_render_args_only_replaces_whitelisted_keys() -> None:
    rendered = runner.render_args(
        ["--topic", "{{topic}}", "--keep", "{{unknown}}", "{{out}}/x.md"],
        topic="公平性",
        in_path="/in",
        out_dir="/out",
        skill_dir="/skill",
        project_dir="/proj",
        task_id="task-0009",
    )
    assert rendered == ["--topic", "公平性", "--keep", "{{unknown}}", "/out/x.md"]


def test_outputs_fall_back_to_new_files_in_work_dir(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """技能没声明 outputs 时：把工作目录里**新出现**的文件收走，老文件不碰。"""

    pack_dir = tmp_path / "container" / "services" / "skills" / "packs" / "demo-skill"
    pack_dir.mkdir(parents=True)
    monkeypatch.setattr(runner.policy, "sciloop_root", lambda: tmp_path / "container")
    pack = _pack(pack_dir, steps=[_step("one")])
    artifacts = tmp_path / "artifacts"
    work = artifacts / "task-0005" / "skills" / "demo-skill" / "work"
    work.mkdir(parents=True)
    (work / "old.txt").write_text("本来就有的", encoding="utf-8")

    async def fake_exec(*, argv: list[str], cwd: str, timeout_s: int) -> dict:
        (Path(cwd) / "fresh.svg").write_text("<svg/>", encoding="utf-8")
        return {"ok": True, "exit_code": 0}

    result = asyncio.run(
        runner.run_skill(
            pack,
            topic="t",
            task_id="task-0005",
            host_info={"host_root": str(tmp_path / "host")},
            artifacts=artifacts,
            executor=fake_exec,
        )
    )
    assert [item["name"] for item in result.outputs] == ["work/fresh.svg"], "产物名如实写成相对产物目录的路径"
