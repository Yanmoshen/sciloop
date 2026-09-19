# Copyright 2026 SciLoop contributors
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
"""导入任务注册表与后台执行（长任务语义）。

契约（附录 B / 本工作包）
-------------------------

- 两个 POST **立即**返回 ``202 + task_id``，真正执行放后台线程；
- 进度只能通过 ``GET /papers/import-jobs/{task_id}`` 轮询；
- ``status ∈ {running, done, failed}``：批处理跑完即 ``done``
  （**单条失败不影响整体状态**，失败明细在 ``items[].status='failed'``），
  只有任务级异常（如数据库不可用）才置 ``failed``；
- ``succeeded`` = ``created`` + ``reused``；``duplicated`` 单列；``failed`` 单列。
- **任务状态只存内存，进程重启即丢失**（见 :data:`app.services.ingest.RESTART_NOTE`）。

实现说明
--------

后台线程里跑 ``asyncio.run(...)``，与 WP05 的 ``parse_fulltext.submit_parse`` 同构：
数据库操作是同步 ``Session``（``app.db.session.SessionLocal``），
``SqlDocumentRepository`` 对同步会话同样可用（内部 ``inspect.isawaitable`` 兼容）。
"""

from __future__ import annotations

import asyncio
import logging
import threading
import uuid
from collections.abc import Awaitable, Callable, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any

logger = logging.getLogger("sciloop.ingest.jobs")

JOB_PDF_UPLOAD = "paper_upload"
JOB_IDENTIFIERS = "identifier_import"

#: 任务级状态（对外契约值）
STATUS_RUNNING = "running"
STATUS_DONE = "done"
STATUS_FAILED = "failed"

#: 单条结果状态（对外契约值）
ITEM_CREATED = "created"
ITEM_REUSED = "reused"
ITEM_DUPLICATE = "duplicate"
ITEM_FAILED = "failed"

_SUCCESS_ITEM_STATUSES: frozenset[str] = frozenset({ITEM_CREATED, ITEM_REUSED})


def _now() -> str:
    return datetime.now(UTC).isoformat()


@dataclass
class ImportTask:
    """一个导入任务的运行态快照（内存对象）。"""

    task_id: str
    job: str
    total: int
    project_id: int | None = None
    status: str = STATUS_RUNNING
    started_at: str = field(default_factory=_now)
    finished_at: str | None = None
    params: dict[str, Any] = field(default_factory=dict)
    items: list[dict[str, Any]] = field(default_factory=list)
    error: str | None = None
    notes: list[str] = field(default_factory=list)

    # -- 计数（每次读取时按 items 现算，避免增量更新漏记）-------------------
    def counts(self) -> dict[str, int]:
        succeeded = sum(1 for item in self.items if item.get("status") in _SUCCESS_ITEM_STATUSES)
        failed = sum(1 for item in self.items if item.get("status") == ITEM_FAILED)
        duplicated = sum(1 for item in self.items if item.get("status") == ITEM_DUPLICATE)
        created = sum(1 for item in self.items if item.get("status") == ITEM_CREATED)
        reused = sum(1 for item in self.items if item.get("status") == ITEM_REUSED)
        return {
            "total": len(self.items),
            "succeeded": succeeded,
            "failed": failed,
            "duplicated": duplicated,
            "created": created,
            "reused": reused,
        }

    def to_dict(self) -> dict[str, Any]:
        return {
            "task_id": self.task_id,
            "job": self.job,
            "status": self.status,
            "started_at": self.started_at,
            "finished_at": self.finished_at,
            "project_id": self.project_id,
            # 契约四件套：total / succeeded / failed / duplicated
            **self.counts(),
            "items": [dict(item) for item in self.items],
            "params": dict(self.params),
            "error": self.error,
            "notes": list(self.notes),
        }


_TASKS: dict[str, ImportTask] = {}
_TASKS_LOCK = threading.Lock()


def new_task(
    job: str,
    total: int,
    *,
    project_id: int | None = None,
    params: dict[str, Any] | None = None,
    notes: Sequence[str] = (),
) -> ImportTask:
    """登记一个新任务并返回（状态为 ``running``）。"""
    task = ImportTask(
        task_id=uuid.uuid4().hex[:12],
        job=job,
        total=int(total),
        project_id=project_id,
        params=dict(params or {}),
        notes=[str(note) for note in notes],
    )
    with _TASKS_LOCK:
        _TASKS[task.task_id] = task
    logger.info("import_task_created task_id=%s job=%s total=%d", task.task_id, job, total)
    return task


def add_item(task_id: str, item: dict[str, Any]) -> None:
    """追加一条单条结果（``items`` 顺序即入参顺序）。"""
    with _TASKS_LOCK:
        task = _TASKS.get(task_id)
        if task is None:
            return
        task.items.append(dict(item))


def finish_task(task_id: str, *, status: str = STATUS_DONE, error: str | None = None) -> None:
    """标记任务终态（幂等：重复调用只覆盖终态信息）。"""
    with _TASKS_LOCK:
        task = _TASKS.get(task_id)
        if task is None:
            return
        task.status = status
        task.finished_at = _now()
        if error:
            task.error = error
    logger.info("import_task_finished task_id=%s status=%s", task_id, status)


def add_note(task_id: str, note: str) -> None:
    with _TASKS_LOCK:
        task = _TASKS.get(task_id)
        if task is not None:
            task.notes.append(str(note))


def get_task(task_id: str) -> dict[str, Any] | None:
    with _TASKS_LOCK:
        task = _TASKS.get(task_id)
        return task.to_dict() if task is not None else None


def list_tasks() -> list[dict[str, Any]]:
    with _TASKS_LOCK:
        return [task.to_dict() for task in _TASKS.values()]


# --------------------------------------------------------------------------------------
# 后台执行
# --------------------------------------------------------------------------------------
Runner = Callable[[str], Awaitable[None]]


def _start_thread(task_id: str, job: str, runner: Callable[[], Awaitable[None]]) -> threading.Thread:
    """把异步 runner 放到后台线程里跑；异常必须能被查询到（置 failed）。"""

    def _worker() -> None:
        try:
            asyncio.run(runner())
        except Exception as exc:  # noqa: BLE001 - 任务级异常要落到任务状态里
            logger.exception("import_task_failed task_id=%s", task_id)
            finish_task(task_id, status=STATUS_FAILED, error=f"{type(exc).__name__}: {exc}")

    thread = threading.Thread(target=_worker, name=f"sciloop-{job}-{task_id}", daemon=True)
    thread.start()
    return thread


def submit_pdf_import(
    files: Sequence[dict[str, Any]],
    *,
    project_id: int | None = None,
) -> dict[str, Any]:
    """登记并后台执行 PDF 导入，立即返回 ``task_id``。

    ``files`` 每项为 ``{"name", "path"?|"content", "sha256", "size_bytes"}``。
    """
    from app.services.ingest import pdf_import

    task = new_task(
        JOB_PDF_UPLOAD,
        len(files),
        project_id=project_id,
        params={"filenames": [item.get("name") for item in files], "project_id": project_id},
    )
    payload = [dict(item) for item in files]
    _start_thread(
        task.task_id,
        JOB_PDF_UPLOAD,
        lambda: pdf_import.run_pdf_import_batch(task.task_id, payload, project_id=project_id),
    )
    return {
        "task_id": task.task_id,
        "job": JOB_PDF_UPLOAD,
        "status": "accepted",
        "total": len(files),
        "thread": f"sciloop-{JOB_PDF_UPLOAD}-{task.task_id}",
    }


def submit_identifier_import(
    values: Sequence[str],
    *,
    project_id: int | None = None,
) -> dict[str, Any]:
    """登记并后台执行标识符批量导入（调用并行工作包提供的能力）。"""
    task = new_task(
        JOB_IDENTIFIERS,
        len(values),
        project_id=project_id,
        params={"identifiers": [str(item) for item in values], "project_id": project_id},
    )
    payload = [str(item) for item in values]
    _start_thread(
        task.task_id,
        JOB_IDENTIFIERS,
        lambda: _run_identifier_batch(task.task_id, payload, project_id=project_id),
    )
    return {
        "task_id": task.task_id,
        "job": JOB_IDENTIFIERS,
        "status": "accepted",
        "total": len(values),
        "thread": f"sciloop-{JOB_IDENTIFIERS}-{task.task_id}",
    }


async def _run_identifier_batch(
    task_id: str, values: Sequence[str], *, project_id: int | None
) -> None:
    """标识符批处理的编排层：**此处才 import** ``identifier_import``。

    该模块由并行工作包实现；缺失时任务如实置 ``failed`` 并给出可读原因
    （绝不把“模块没落地”伪装成“论文不存在”）。
    """
    try:
        from app.services.ingest import identifier_import
    except ModuleNotFoundError as exc:
        message = (
            f"identifier_import_unavailable：app.services.ingest.identifier_import 未就绪"
            f"（{exc.name}）"
        )
        logger.error("identifier_batch_missing_module task_id=%s %s", task_id, exc.name)
        add_note(task_id, message)
        finish_task(task_id, status=STATUS_FAILED, error=message)
        return

    try:
        results = await identifier_import.import_many(list(values), project_id=project_id)
    except Exception as exc:  # noqa: BLE001 - 任务级异常如实上报
        message = f"{type(exc).__name__}: {exc}"
        logger.exception("identifier_batch_failed task_id=%s", task_id)
        add_note(task_id, f"批量导入执行异常：{message}")
        finish_task(task_id, status=STATUS_FAILED, error=message)
        return

    for index, raw in enumerate(values):
        result = results[index] if index < len(results) else None
        if not isinstance(result, dict):
            add_item(
                task_id,
                {
                    "input": str(raw),
                    "status": ITEM_FAILED,
                    "paper_id": None,
                    "reason": "no_result_returned",
                    "source": None,
                },
            )
            continue
        add_item(
            task_id,
            {
                "input": str(result.get("input") or raw),
                "status": str(result.get("status") or ITEM_FAILED),
                "paper_id": result.get("paper_id"),
                "reason": result.get("reason"),
                "source": result.get("source"),
            },
        )
    finish_task(task_id, status=STATUS_DONE)


__all__ = [
    "ITEM_CREATED",
    "ITEM_DUPLICATE",
    "ITEM_FAILED",
    "ITEM_REUSED",
    "JOB_IDENTIFIERS",
    "JOB_PDF_UPLOAD",
    "STATUS_DONE",
    "STATUS_FAILED",
    "STATUS_RUNNING",
    "ImportTask",
    "add_item",
    "add_note",
    "finish_task",
    "get_task",
    "list_tasks",
    "new_task",
    "submit_identifier_import",
    "submit_pdf_import",
]
