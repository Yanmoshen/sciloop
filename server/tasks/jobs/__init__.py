# Copyright 2026 SciLoop contributors
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
"""``tasks.jobs`` —— 批量任务包（显式注册表，避免「同名包空文件」造成的 import 歧义）。

为什么需要这个 ``__init__.py``
-----------------------------

仓库里同时存在多个 ``jobs`` 目录（前端构建产物、第三方依赖树里都可能有），
在**没有** ``__init__.py`` 的情况下 Python 会把本目录当成 namespace package，
``import jobs`` / ``from jobs import fetch_papers`` 可能命中别的同名包；
同时工具链（pytest 的 rootdir 收集、ruff 的 per-file ignore）也无法稳定识别。
本文件把本包**显式声明为常规包**，并给出唯一的任务注册表。

三个调度任务（由 :mod:`tasks.scheduler` 注册，默认关闭）
----------------------------------------------------------

=================  ====================================  =========================
任务名             模块                                  同步入口
=================  ====================================  =========================
``fetch_papers``   ``tasks.jobs.fetch_papers``       ``run_fetch_sync``
``score_papers``   ``tasks.jobs.score_papers``       ``run_score_job``
``parse_fulltext`` ``tasks.jobs.parse_fulltext``      ``run_parse_fulltext_sync``
=================  ====================================  =========================

``python -m`` 入口约定
----------------------

每个任务模块都必须提供 ``main(argv) -> int`` 并带 ``if __name__ == "__main__"`` 守卫，
因此统一用模块方式执行（**不要**用 ``python app/tasks/jobs/x.py``，那样会丢包上下文）::

    docker compose exec backend python -m tasks.jobs.fetch_papers --limit 50
    docker compose exec backend python -m tasks.jobs.score_papers 200 --force
    docker compose exec backend python -m tasks.jobs.parse_fulltext --limit 50
    docker compose exec backend python -m tasks.jobs.seed_demo --help

本包的 ``__init__`` **不** eager import 任何任务模块：任务模块会拉起 httpx / pymupdf /
LLM 适配层等重依赖，任何一个缺依赖都不应该让 ``import tasks.jobs`` 失败
（调度器按模块粒度做惰性 import 并只告警，见 ``tasks.scheduler``）。
需要模块对象时用 :func:`load_job`，或直接 ``from tasks.jobs import fetch_papers``
（由模块级 ``__getattr__`` 惰性解析）。
"""

from __future__ import annotations

import importlib
from collections.abc import Sequence
from typing import Any

__all__ = [
    "AUX_MODULES",
    "JOB_MODULES",
    "SCHEDULED_JOBS",
    "job_names",
    "load_job",
    "main",
    "run_job",
]

#: 任务名 → 模块路径（任务名与 :mod:`tasks.scheduler` 的 ``job_id`` 同名同义）
JOB_MODULES: dict[str, str] = {
    "fetch_papers": "tasks.jobs.fetch_papers",
    "score_papers": "tasks.jobs.score_papers",
    "parse_fulltext": "tasks.jobs.parse_fulltext",
}

#: 非调度型运维模块（手工执行，不进 APScheduler）
AUX_MODULES: dict[str, str] = {
    "seed_demo": "tasks.jobs.seed_demo",
}

#: 由调度器注册的三个任务（顺序即调度器注册顺序）
SCHEDULED_JOBS: tuple[str, ...] = ("fetch_papers", "score_papers", "parse_fulltext")


def job_names(*, include_aux: bool = True) -> tuple[str, ...]:
    """返回本包登记的任务名。"""
    names = list(JOB_MODULES)
    if include_aux:
        names.extend(AUX_MODULES)
    return tuple(names)


def _module_path(name: str) -> str:
    try:
        return JOB_MODULES[name]
    except KeyError:
        try:
            return AUX_MODULES[name]
        except KeyError:
            raise KeyError(f"未知任务 {name!r}；可用：{', '.join(job_names())}") from None


def load_job(name: str) -> Any:
    """按名惰性导入任务模块（缺依赖时抛出原始 ImportError，由调用方决定告警策略）。"""
    return importlib.import_module(_module_path(name))


def run_job(name: str, argv: Sequence[str] | None = None) -> int:
    """调用任务模块的 ``main(argv)``，统一返回退出码。"""
    module = load_job(name)
    entry = getattr(module, "main", None)
    if not callable(entry):
        raise RuntimeError(f"{_module_path(name)} 未实现约定的 main(argv) -> int 入口")
    return int(entry(list(argv) if argv is not None else []))


def main(argv: Sequence[str] | None = None) -> int:
    """索引入口：无参时列出全部任务与 ``python -m`` 用法。

    约定：``python -m tasks.jobs.<name>`` 直接跑某个任务；
    ``run_job(name, argv)`` 供脚本/Python 侧调用同一入口。
    （若需要 ``python -m tasks.jobs`` 这种「包级索引」入口，需在同目录新增
    ``__main__.py``——该文件不在 WP01 的 owned_paths，故此处只提供注册表与索引打印。）
    """
    args = list(argv) if argv is not None else []
    if args:
        return run_job(args[0], args[1:])
    lines = [
        "SciLoop 批量任务索引（python -m 入口）",
        "",
        "调度任务（SCHEDULER_ENABLED=1 时由 tasks.scheduler 注册）：",
    ]
    for name in SCHEDULED_JOBS:
        lines.append(f"  - {name:15s} {JOB_MODULES[name]:40s} python -m {JOB_MODULES[name]}")
    lines.extend(["", "运维模块（手工执行）："])
    for name, path in AUX_MODULES.items():
        lines.append(f"  - {name:15s} {path:40s} python -m {path}")
    lines.extend(["", "索引调用：python -m tasks.jobs <job_name> [args...]"])
    print("\n".join(lines))
    return 0


def __getattr__(name: str) -> Any:
    """让 ``from tasks.jobs import fetch_papers`` 惰性可用（PEP 562）。"""
    if name in JOB_MODULES or name in AUX_MODULES:
        return load_job(name)
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")


if __name__ == "__main__":  # pragma: no cover - 需要同目录 __main__.py 才会走到
    raise SystemExit(main())
