# Copyright 2026 SciLoop contributors
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
"""依赖体检：先在这儿弄清"哪些技能在**这台机器上**跑不了"。

背景（2026-09-23 真机实测）：技能脚本会缺东西 ——
有的缺 Python 包（`pint` / `sklearn`…），有的缺环境变量（图片 API key），
还有的本机包本身是坏的（本机 numpy 是 MINGW 实验版，Python 3.14 下一跑就崩）。
**跑一半才报错**是最差的体验，所以体检要提前说清。

两件事分开做：
- **静态**（解析脚本的 import）：算出每个技能需要哪些第三方包 —— 不需要执行任何东西；
- **动态**（一次只读探针）：让宿主用**跑技能的那个解释器**报一遍"这些包在不在"——
  一条命令问全部，不逐个跑脚本。
"""

from __future__ import annotations

import ast
import json
import sys
from pathlib import Path
from typing import Any

from services.skills.registry import SkillPack

__all__ = ["PROBE_TIMEOUT_S", "imports_of", "probe_script", "required_packages"]

#: 探针命令的超时（只是 import 检查，给足 60 秒）
PROBE_TIMEOUT_S = 60

#: 这些"包"是本地模块或标准库，不用检查
_SKIP = {"", "__future__", "typing_extensions"}


def imports_of(path: Path) -> set[str]:
    """一个脚本里 import 的**顶层模块名**（解析失败当没有 —— 体检不该把库读挂）。"""

    try:
        tree = ast.parse(path.read_text(encoding="utf-8", errors="replace"))
    except (SyntaxError, OSError):
        return set()
    found: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            found.update(alias.name.split(".")[0] for alias in node.names)
        elif isinstance(node, ast.ImportFrom):
            if node.level:  # 相对导入：技能包内部，不用检查
                continue
            if node.module:
                found.add(node.module.split(".")[0])
    return {name for name in found if name and name not in _SKIP}


def local_modules(pack: SkillPack) -> set[str]:
    """技能包**自己**带的模块名（`scripts/_common.py` → `_common`）。

    ⚠️ 这些不是"缺的包"：脚本跑起来时 `scripts/` 就在 `sys.path` 上，
    `import _common` 能解析到同一目录下的文件（2026-09-23 真机跑已证明）。
    一开始没排掉它们，体检把 12 个技能误报成"缺包"。
    """

    if pack.path is None:
        return set()
    local = {path.stem for path in pack.path.rglob("*.py")}
    # 也收录**子目录名**：`scripts/office/xxx.py` 是以 `office` 这个包名被 import 的
    # （2026-09-23：`docx` 技能就因此被误报"缺 office / helpers / validators"）
    local |= {path.name for path in pack.path.rglob("*") if path.is_dir()}
    return local


def required_packages(pack: SkillPack) -> list[str]:
    """这个技能**声明的脚本**要用到哪些**第三方**包（排掉标准库与包内自己的模块）。"""

    if pack.path is None:
        return []
    stdlib = set(sys.stdlib_module_names)
    local = local_modules(pack)
    names: set[str] = set()
    for relative in pack.scripts:
        if not relative.endswith(".py"):
            continue
        names |= imports_of(pack.path / relative)
    return sorted(name for name in names if name not in stdlib and name not in local)


def probe_script(packages: list[str], env_names: list[str] | None = None) -> str:
    """生成那条**只读**探针命令：让宿主解释器报一遍"这些包在不在、这些 key 配了没"。

    ⚠️ 必须问**宿主**：技能脚本是在研究者电脑上跑的，API key 也得在那台机器上有。
    """

    env_names = env_names or []
    return (
        "import importlib.util, json, os; "
        f"names = {json.dumps(packages, ensure_ascii=False)}; "
        f"envs = {json.dumps(env_names, ensure_ascii=False)}; "
        "print(json.dumps({'packages': {n: importlib.util.find_spec(n) is not None for n in names}, "
        "'env': {e: bool(os.environ.get(e)) for e in envs}}))"
    )


def parse_probe(stdout: str) -> dict[str, dict[str, bool]]:
    """解析探针输出（多一行日志也不怕：取最后一行像 JSON 的）。"""

    for line in reversed((stdout or "").strip().splitlines()):
        line = line.strip()
        if line.startswith("{") and line.endswith("}"):
            try:
                data = json.loads(line)
            except json.JSONDecodeError:
                continue
            if isinstance(data, dict):
                packages = data.get("packages") if isinstance(data.get("packages"), dict) else None
                env = data.get("env") if isinstance(data.get("env"), dict) else None
                return {
                    "packages": {str(k): bool(v) for k, v in (packages or {}).items()},
                    "env": {str(k): bool(v) for k, v in (env or {}).items()},
                }
    return {}


async def check_all(*, packs: list[SkillPack], python: str, model_dir: Path | None = None) -> dict[str, Any]:
    """给一批技能做体检：静态算依赖 + 一次宿主探针。

    返回 `{packages: {...}, skills: {name: {missing_python, missing_env}}, ...}`。
    探针失败（执行器没连上/解释器不对）时**如实记 `probe_error`**，不假装体检通过。
    """

    per_skill: dict[str, list[str]] = {}
    union: set[str] = set()
    env_union: set[str] = set()
    for pack in packs:
        packages = required_packages(pack) if pack.scripts else []
        per_skill[pack.name] = packages
        union |= set(packages)
        env_union |= {
            str(item.get("name"))
            for item in (pack.requires_env or [])
            if item.get("name")
        }

    probe_error = ""
    found: dict[str, dict[str, bool]] = {"packages": {}, "env": {}}
    if union or env_union:
        from services.agent import host_runner

        info = (await host_runner.ensure_host_roots()) or {}
        if not info.get("host_root"):
            probe_error = "执行器没连上，暂时没法体检（技能能不能跑还是未知数）"
        else:
            result = await host_runner.call_exec(
                argv=[python, "-c", probe_script(sorted(union), sorted(env_union))],
                cwd=str(info.get("host_root") or ""),
                timeout_s=PROBE_TIMEOUT_S,
            )
            if not result.get("ok"):
                probe_error = str(result.get("error") or result.get("stderr") or "探针没跑成")[:300]
            else:
                found = parse_probe(str(result.get("stdout") or ""))

    probed_packages = found.get("packages") or {}
    probed_env = found.get("env") or {}

    skills: dict[str, Any] = {}
    for pack in packs:
        missing_python = [name for name in per_skill[pack.name] if probed_packages and not probed_packages.get(name, True)]
        missing_env = [
            str(item.get("name"))
            for item in (pack.requires_env or [])
            if item.get("name") and probed_env and not probed_env.get(str(item["name"]), True)
        ]
        skills[pack.name] = {
            "packages": per_skill[pack.name],
            "missing_python": missing_python,
            "missing_env": missing_env,
            "can_run": not missing_python and not missing_env and pack.runnable,
        }

    return {
        "python": python,
        "probe_error": probe_error,
        "probed_packages": sorted(union),
        "skills": skills,
    }
