# Copyright 2026 SciLoop contributors
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
"""技能引擎（扫描 / 两级披露 / 按技能声明的流程跑）。

- :mod:`services.skills.registry`：技能包解析与两级披露；
- 内置技能内容在 ``services/skills/packs/``（随镜像走，不新增挂载）。
"""

from services.skills.registry import (
    PACKS_DIR,
    STAGE_LABELS,
    CatalogEntry,
    SkillPack,
    SkillStep,
    catalog,
    load,
    packs_dir,
    scan,
)
from services.skills.runner import RunResult, StepResult, run_skill

__all__ = [
    'RunResult',
    'StepResult',
    "PACKS_DIR",
    "STAGE_LABELS",
    "CatalogEntry",
    "SkillPack",
    "SkillStep",
    "catalog",
    "load",
    "packs_dir",
    "run_skill",
    "scan",
]
