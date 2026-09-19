# Copyright 2026 SciLoop contributors
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
"""批量任务调度（APScheduler）：``fetch_papers`` / ``score_papers`` / ``parse_fulltext``。

设计要点
--------

1. **默认关闭**：``SCHEDULER_ENABLED=0``（默认）时不创建调度器，只打一行明确日志
   「调度器未启用」。演示/答辩期绝不能因为容器重启就自动抓取论文或调用 LLM
   （外部额度与成本护栏都要求这一点）。
2. **惰性 import，缺依赖只告警**：任务模块（``app.tasks.jobs.*``）与 APScheduler 都在
   真正要注册时才 import。任何一个任务模块在当前分支上不存在或依赖不全，只跳过该任务
   并打 warning，**绝不影响服务启动**（并行开发期这正是需要的性质）。
3. **单实例串行**：``max_instances=1`` + ``coalesce=True``，避免上一轮还没跑完就叠下一轮；
   每轮默认处理上限由 ``SCHEDULER_JOB_LIMIT`` 控制，防止一次跑爆额度。
4. **尊重任务模块自有注册函数**：``fetch_papers`` 自带 ``register_jobs()``（WP03 的约定
   「WP01 的 scheduler 若存在则调用本函数」），存在时优先用它，避免两套参数口径。

与 ``main.py`` 的关系：``start_scheduler()`` / ``shutdown_scheduler()`` 由 FastAPI
的 lifespan 调用（见 ``app.main.lifespan``）；本模块 import 时**不产生副作用**。
"""

from __future__ import annotations

import importlib
import inspect
import logging
from typing import Any

from app.core.config import get_settings

__all__ = [
    "JOB_SPECS",
    "active_jobs",
    "register_jobs",
    "shutdown_scheduler",
    "start_scheduler",
]

logger = logging.getLogger("sciloop.scheduler")

#: (任务名, 模块路径, 同步入口函数名, 轮询间隔配置项)
#: 任务名同时是 APScheduler 的 ``job_id``，也是 ``app.tasks.jobs`` 下的模块名。
JOB_SPECS: tuple[tuple[str, str, str, str], ...] = (
    (
        "fetch_papers",
        "app.tasks.jobs.fetch_papers",
        "run_fetch_sync",
        "scheduler_fetch_interval_hours",
    ),
    (
        "score_papers",
        "app.tasks.jobs.score_papers",
        "run_score_job",
        "scheduler_score_interval_hours",
    ),
    (
        "parse_fulltext",
        "app.tasks.jobs.parse_fulltext",
        "run_parse_fulltext_sync",
        "scheduler_parse_interval_hours",
    ),
)

#: 进程内单例调度器（None = 未启动或未启用）
_scheduler: Any = None


def _accepts_limit(func: Any) -> bool:
    """判断入口函数是否接受 ``limit=``（``**kwargs`` 形式也算接受）。"""
    try:
        signature = inspect.signature(func)
    except (TypeError, ValueError):  # pragma: no cover - 内建/装饰器包装的极端情况
        return False
    parameters = signature.parameters
    if "limit" in parameters:
        return True
    return any(p.kind is inspect.Parameter.VAR_KEYWORD for p in parameters.values())


def _make_wrapper(name: str, func: Any, limit: int):
    """把同步入口包装成「失败不影响调度器」的任务函数。"""

    def _job() -> None:
        try:
            result = func(limit=limit) if _accepts_limit(func) else func()
            logger.info("scheduled_job_done job=%s result=%s", name, str(result)[:200])
        except Exception:  # noqa: BLE001 - 单轮失败只记日志，调度器必须活着
            logger.exception("scheduled_job_failed job=%s", name)

    _job.__name__ = f"sciloop_{name}_job"
    return _job


def _register_one(scheduler: Any, spec: tuple[str, str, str, str], limit: int) -> bool:
    """注册单个任务；模块缺失/入口缺失/依赖缺失只告警并返回 False。"""
    name, module_path, func_name, interval_attr = spec
    try:
        module = importlib.import_module(module_path)
    except ImportError as exc:
        logger.warning(
            "任务 %s 未注册：模块 %s 不可导入（%s）；服务继续运行", name, module_path, exc
        )
        return False

    interval_hours = max(1, int(getattr(get_settings(), interval_attr, 12)))

    # 任务模块自带的注册函数优先（例如 WP03 的 fetch_papers.register_jobs）
    register = getattr(module, "register_jobs", None)
    if callable(register):
        try:
            register(scheduler, interval_hours=interval_hours, limit=limit)
            logger.info(
                "任务已注册（模块自带 register_jobs）job=%s interval_hours=%d limit=%d",
                name,
                interval_hours,
                limit,
            )
            return True
        except Exception:  # noqa: BLE001 - 注册失败不能拖垮启动
            logger.exception("任务 %s 注册失败，改用通用入口重试", name)

    func = getattr(module, func_name, None)
    if not callable(func):
        logger.warning("任务 %s 未注册：%s 未导出可调用入口 %s", name, module_path, func_name)
        return False

    from apscheduler.triggers.interval import IntervalTrigger

    scheduler.add_job(
        _make_wrapper(name, func, limit),
        trigger=IntervalTrigger(hours=interval_hours),
        id=name,
        name=f"SciLoop {name}",
        replace_existing=True,
        max_instances=1,
        coalesce=True,
        misfire_grace_time=600,
    )
    logger.info("任务已注册 job=%s interval_hours=%d limit=%d", name, interval_hours, limit)
    return True


def register_jobs(scheduler: Any, *, limit: int | None = None) -> list[str]:
    """把三类任务注册到给定调度器；返回成功注册的任务名列表。"""
    job_limit = int(limit or get_settings().scheduler_job_limit)
    registered = [spec[0] for spec in JOB_SPECS if _register_one(scheduler, spec, job_limit)]
    logger.info(
        "调度任务注册完成 %d/%d: %s", len(registered), len(JOB_SPECS), ", ".join(registered) or "-"
    )
    return registered


def start_scheduler() -> Any | None:
    """按 ``SCHEDULER_ENABLED`` 启动后台调度器。

    :returns: 调度器实例；未启用或依赖缺失时返回 ``None``（服务照常启动）。
    """
    global _scheduler
    settings = get_settings()
    if not settings.scheduler_enabled:
        logger.info(
            "调度器未启用（SCHEDULER_ENABLED=0）：跳过 fetch_papers / score_papers / "
            "parse_fulltext 注册；如需启用请设置 SCHEDULER_ENABLED=1 后重启"
        )
        return None
    if _scheduler is not None:  # pragma: no cover - lifespan 正常只会调一次
        return _scheduler

    try:
        from apscheduler.schedulers.background import BackgroundScheduler
    except ImportError as exc:  # pragma: no cover - 镜像里已固定 apscheduler
        logger.warning("调度器依赖缺失（%s）：已跳过注册，服务继续运行", exc)
        return None

    scheduler = BackgroundScheduler(timezone="UTC")
    registered = register_jobs(scheduler)
    if not registered:
        logger.warning("没有任何任务注册成功，调度器不启动")
        return None
    scheduler.start()
    _scheduler = scheduler
    logger.info("调度器已启动（UTC，后台线程）：jobs=%s", ", ".join(registered))
    return scheduler


def shutdown_scheduler(*, wait: bool = False) -> None:
    """关闭调度器（幂等；未启动时静默返回）。"""
    global _scheduler
    if _scheduler is None:
        logger.info("调度器未运行，无需关闭")
        return
    try:
        _scheduler.shutdown(wait=wait)
        logger.info("调度器已关闭（wait=%s）", wait)
    except Exception:  # noqa: BLE001 - 关闭失败不影响进程退出
        logger.exception("调度器关闭异常")
    finally:
        _scheduler = None


def active_jobs() -> list[str]:
    """当前已注册任务的 id 列表（未启动时为空）——供运维/验收脚本自检。"""
    if _scheduler is None:
        return []
    return [job.id for job in _scheduler.get_jobs()]
