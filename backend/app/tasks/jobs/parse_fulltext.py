# Copyright 2026 SciLoop contributors
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
"""批量全文解析任务（WP05-T6）。

做什么
------
把 ``papers`` 表里**尚未解析出可用全文**的论文逐篇喂给
``DocumentStore.ensure_fulltext``（HTML(ar5iv) 优先 → arXiv HTML5 → PDF(pymupdf)
回退），把 ``paper_documents`` / ``paper_spans`` 落库，并产出一份可审计报告。

设计要点
--------
- **断点续跑**：候选集来自 ``SqlDocumentRepository.list_paper_ids(only_pending=True)``，
  已存在 ``parse_status='ok'`` 记录的论文天然被跳过；单篇内部 ``ensure_fulltext``
  还会按 ``document_version`` 去重复用，重复跑同一批不会产生重复记录。
- **限速**：每篇之间 ``await asyncio.sleep(delay_seconds)``，避免对 ar5iv/arXiv 形成突发压力。
- **失败留痕**：单篇异常只影响该篇，写入 ``errors`` 并继续；全文拿不到时由服务层
  如实写 ``parse_status ∈ {partial, unavailable, failed}`` 与 ``parse_error``，
  **绝不静默标成已解析**。
- **证据门禁**：只有 ``parse_status='ok'`` 且 ``coverage>=0.60`` 才产出正文 span
  （contracts.evidence_rules.fulltext_gate）。报告里 ``spans_written`` 只统计
  **本轮真正写入**的片段，``spans_total`` 含复用记录已有的片段（断点续跑时为 0 写入）。

入口
----
::

    await run_parse_fulltext(limit=20)          # 异步（同一事件循环内）
    run_parse_fulltext_sync(limit=20)           # 同步（脚本 / 后台线程）
    python -m app.tasks.jobs.parse_fulltext --limit 5
"""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import sys
import threading
import time
import uuid
from collections.abc import Callable, Iterable, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any

from app.db.session import SessionLocal
from app.services.fulltext import (
    DocumentStore,
    PaperNotFoundError,
    SqlDocumentRepository,
)
from app.services.fulltext.coverage import min_coverage_threshold

logger = logging.getLogger("sciloop.wp05.parse_fulltext")

JOB_NAME = "parse_fulltext"
DEFAULT_LIMIT = 20
#: 每篇之间的最小间隔（秒），可用 ``--delay-seconds`` 覆盖
DEFAULT_DELAY_SECONDS = 1.5
#: 非 ok 的终态：默认的候选来源是 only_pending，这里用于「只重试这些状态」模式
RETRY_STATUSES: tuple[str, ...] = ("partial", "unavailable", "failed")

FULLTEXT_GATE_NOTE = (
    "只有 parse_status='ok' 且 coverage>=0.60 才产出正文 paper_span；"
    "其余状态只允许摘要级证据（contracts.evidence_rules.fulltext_gate）"
)


# --------------------------------------------------------------------------------------
# 报告与任务登记
# --------------------------------------------------------------------------------------
@dataclass
class ParseReport:
    """一轮批量解析的可审计报告（API 与日志共用）。"""

    task_id: str
    status: str = "running"
    started_at: str = field(default_factory=lambda: datetime.now(UTC).isoformat())
    finished_at: str | None = None
    selection: str = "pending"
    limit: int | None = None
    force: bool = False
    only_pending: bool = True
    delay_seconds: float = DEFAULT_DELAY_SECONDS
    retry_statuses: list[str] = field(default_factory=list)
    min_coverage: float = 0.60
    candidates: int = 0
    attempted: int = 0
    reused: int = 0
    ok: int = 0
    partial: int = 0
    unavailable: int = 0
    failed: int = 0
    #: 本轮**真正写入**的 span 数（复用的记录不重复计数）
    spans_written: int = 0
    #: 本轮处理结果里可见的 span 总数（含复用记录已有片段）
    spans_total: int = 0
    results: list[dict[str, Any]] = field(default_factory=list)
    errors: list[dict[str, Any]] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)

    def duration_seconds(self) -> float | None:
        if not self.finished_at:
            return None
        try:
            start = datetime.fromisoformat(self.started_at)
            end = datetime.fromisoformat(self.finished_at)
        except ValueError:  # pragma: no cover - 时间戳一定由本模块写入
            return None
        return round((end - start).total_seconds(), 3)

    def to_dict(self) -> dict[str, Any]:
        return {
            "task_id": self.task_id,
            "job": JOB_NAME,
            "status": self.status,
            "started_at": self.started_at,
            "finished_at": self.finished_at,
            "duration_seconds": self.duration_seconds(),
            "params": {
                "selection": self.selection,
                "limit": self.limit,
                "force": self.force,
                "only_pending": self.only_pending,
                "delay_seconds": self.delay_seconds,
                "retry_statuses": self.retry_statuses,
                "min_coverage": self.min_coverage,
            },
            "counts": {
                "candidates": self.candidates,
                "attempted": self.attempted,
                "reused": self.reused,
                "ok": self.ok,
                "partial": self.partial,
                "unavailable": self.unavailable,
                "failed": self.failed,
                "spans_written": self.spans_written,
                "spans_total": self.spans_total,
                "errors": len(self.errors),
            },
            "results": self.results,
            "errors": self.errors,
            "notes": self.notes,
            "fulltext_gate": FULLTEXT_GATE_NOTE,
        }


_TASKS: dict[str, dict[str, Any]] = {}
_TASKS_LOCK = threading.Lock()


def get_task(task_id: str) -> dict[str, Any] | None:
    with _TASKS_LOCK:
        task = _TASKS.get(task_id)
        return dict(task) if task is not None else None


def list_tasks() -> list[dict[str, Any]]:
    with _TASKS_LOCK:
        return [dict(item) for item in _TASKS.values()]


def _register_task(task_id: str, payload: dict[str, Any]) -> None:
    with _TASKS_LOCK:
        _TASKS[task_id] = payload


def _update_task(task_id: str, **changes: Any) -> None:
    with _TASKS_LOCK:
        if task_id in _TASKS:
            _TASKS[task_id].update(changes)


# --------------------------------------------------------------------------------------
# 执行
# --------------------------------------------------------------------------------------
def _open_session() -> Any:
    """同步会话（``psycopg``）；工厂不可用时返回 ``None``（由调用方如实报错）。"""
    if SessionLocal is None:
        return None
    return SessionLocal()


def _now() -> str:
    return datetime.now(UTC).isoformat()


def _resolve_html_first(html_first: bool | None) -> bool:
    """``None`` → ``FULLTEXT_HTML_FIRST``（默认 true）。

    注意：必须显式解析，不能把 ``None`` 直接传给 ``DocumentStore.html_first``，
    否则其真值判定会退化成「PDF 优先」，破坏 §2.8 的分级策略。
    """
    if html_first is not None:
        return bool(html_first)
    try:  # pragma: no cover - 依赖运行环境
        from app.core.config import get_settings

        return bool(get_settings().fulltext_html_first)
    except Exception:  # pragma: no cover
        return True


def _entry_from_result(paper_id: int, result: Any, duration_ms: int) -> dict[str, Any]:
    document = result.document
    return {
        "paper_id": paper_id,
        "document_version": document.document_version,
        "source_type": document.source_type,
        "source_url": document.source_url,
        "parser": document.parser,
        "parser_version": document.parser_version,
        "page_count": document.page_count,
        "text_sha256": document.text_sha256,
        "char_count": document.char_count,
        "locatable_chars": document.locatable_chars,
        "coverage": document.coverage,
        "parse_status": document.parse_status,
        "parse_error": document.parse_error,
        "spans_allowed": bool(result.spans_allowed),
        "evidence_scope": result.evidence_scope,
        "span_count": len(result.spans),
        "reused": bool(result.reused),
        "truncated": bool(result.truncated),
        "duration_ms": duration_ms,
        "attempts": [attempt.describe() for attempt in result.attempts],
        "warnings": list(result.warnings),
    }


async def run_parse_fulltext(
    *,
    paper_ids: Sequence[int] | None = None,
    limit: int | None = None,
    force: bool = False,
    delay_seconds: float | None = None,
    retry_statuses: Iterable[str] = (),
    only_pending: bool = True,
    task_id: str | None = None,
    html_first: bool | None = None,
    on_progress: Callable[[dict[str, Any]], None] | None = None,
) -> ParseReport:
    """批量解析全文并落库，返回 :class:`ParseReport`。

    :param paper_ids: 显式指定论文（``POST /papers/{id}/parse`` 与重跑场景）；
        给定时**忽略** ``only_pending`` / ``retry_statuses`` 的候选筛选。
    :param limit: 候选上限（``None`` = 不限）。
    :param force: 忽略已有记录强制重新抓取解析。
    :param delay_seconds: 每篇之间的限速间隔；``None`` → :data:`DEFAULT_DELAY_SECONDS`。
    :param retry_statuses: 只重试处于这些 ``parse_status`` 的论文（显式重试模式）；
        为空则按 ``only_pending`` 选「从未解析 + 非 ok」的全部论文。
    """
    retry = tuple(str(item) for item in retry_statuses if str(item).strip())
    delay = DEFAULT_DELAY_SECONDS if delay_seconds is None else max(0.0, float(delay_seconds))
    report = ParseReport(
        task_id=task_id or uuid.uuid4().hex[:12],
        limit=limit,
        force=bool(force),
        only_pending=bool(only_pending),
        delay_seconds=delay,
        retry_statuses=list(retry),
        min_coverage=min_coverage_threshold(),
        selection="explicit" if paper_ids else ("pending" if only_pending else "all"),
    )
    if retry and paper_ids:
        report.notes.append("已指定 paper_ids：retry_statuses 不参与候选筛选")
    if retry and only_pending:
        report.notes.append("retry_statuses 已给出：仅重试这些状态的旧记录，不含从未解析的论文")

    session = _open_session()
    if session is None:
        report.status = "failed"
        report.finished_at = _now()
        report.errors.append(
            {
                "code": "database_unavailable",
                "message": "同步会话工厂不可用（DATABASE_URL / psycopg 未就绪）",
            }
        )
        logger.error("parse_fulltext 无法启动：数据库会话工厂不可用")
        return report

    try:
        repository = SqlDocumentRepository(session)
        if paper_ids:
            candidates = [int(pid) for pid in paper_ids]
            if limit:
                candidates = candidates[: int(limit)]
        else:
            candidates = await repository.list_paper_ids(
                limit=int(limit) if limit else None,
                only_pending=bool(only_pending),
                retry_statuses=retry,
            )
    except Exception as exc:  # noqa: BLE001 - 候选查询失败必须如实报告
        report.status = "failed"
        report.finished_at = _now()
        report.errors.append(
            {"code": "candidate_query_failed", "message": f"{type(exc).__name__}: {exc}"}
        )
        logger.exception("parse_fulltext 候选查询失败")
        return report
    finally:
        session.close()

    report.candidates = len(candidates)
    resolved_html_first = _resolve_html_first(html_first)
    if resolved_html_first:
        report.notes.append("抓取分级：HTML(ar5iv → arXiv HTML5) 优先，失败回退 PDF(pymupdf)")
    else:
        report.notes.append("抓取分级：PDF(pymupdf) 优先（FULLTEXT_HTML_FIRST=false）")
    logger.info(
        "parse_fulltext start task_id=%s selection=%s candidates=%d limit=%s force=%s delay=%.2fs html_first=%s",
        report.task_id,
        report.selection,
        report.candidates,
        limit,
        force,
        delay,
        resolved_html_first,
    )

    for index, paper_id in enumerate(candidates):
        if index and delay:
            await asyncio.sleep(delay)  # 限速：串行 + 篇间间隔
        started = time.perf_counter()
        session = _open_session()
        if session is None:
            report.errors.append(
                {
                    "paper_id": paper_id,
                    "code": "database_unavailable",
                    "message": "同步会话工厂不可用，本轮提前结束",
                }
            )
            break
        try:
            store = DocumentStore(
                repository=SqlDocumentRepository(session),
                html_first=resolved_html_first,
            )
            result = await store.ensure_fulltext(paper_id, force=force)
        except PaperNotFoundError as exc:
            report.attempted += 1
            report.errors.append(
                {"paper_id": paper_id, "code": "paper_not_found", "message": str(exc)}
            )
            logger.warning("parse_fulltext paper_not_found paper_id=%s", paper_id)
            continue
        except Exception as exc:  # noqa: BLE001 - 单篇失败不阻断整批
            report.attempted += 1
            report.failed += 1
            report.errors.append(
                {
                    "paper_id": paper_id,
                    "code": "parse_exception",
                    "message": f"{type(exc).__name__}: {exc}",
                }
            )
            logger.exception("parse_fulltext 单篇异常 paper_id=%s", paper_id)
            continue
        finally:
            session.close()

        duration_ms = int((time.perf_counter() - started) * 1000)
        entry = _entry_from_result(paper_id, result, duration_ms)
        report.results.append(entry)
        report.attempted += 1
        if result.reused:
            report.reused += 1
        status = entry["parse_status"]
        if status == "ok":
            report.ok += 1
        elif status == "partial":
            report.partial += 1
        elif status == "unavailable":
            report.unavailable += 1
        else:
            report.failed += 1
        report.spans_total += int(entry["span_count"])
        if not entry["reused"]:
            report.spans_written += int(entry["span_count"])
        if on_progress is not None:
            try:
                on_progress(entry)
            except Exception:  # noqa: BLE001 - 进度回调不得影响主流程
                logger.warning("parse_fulltext 进度回调异常 paper_id=%s", paper_id)

    report.finished_at = _now()
    if report.errors and report.attempted == 0:
        report.status = "failed"
    elif report.errors:
        report.status = "partial"
    else:
        report.status = "done"
    logger.info(
        "parse_fulltext done task_id=%s status=%s ok=%d partial=%d unavailable=%d failed=%d reused=%d spans=%d",
        report.task_id,
        report.status,
        report.ok,
        report.partial,
        report.unavailable,
        report.failed,
        report.reused,
        report.spans_written,
    )
    return report


def run_parse_fulltext_sync(**kwargs: Any) -> ParseReport:
    """同步跑一轮（脚本 / APScheduler / 后台线程用）。"""
    return asyncio.run(run_parse_fulltext(**kwargs))


def submit_parse(
    *,
    paper_id: int | None = None,
    paper_ids: Sequence[int] | None = None,
    force: bool = False,
    limit: int | None = None,
    delay_seconds: float | None = None,
    retry_statuses: Iterable[str] = (),
    only_pending: bool = True,
    runner: Callable[..., ParseReport] = run_parse_fulltext_sync,
) -> dict[str, Any]:
    """把一轮解析丢到后台线程并立即返回 ``task_id``（``POST /papers/{id}/parse`` 用）。

    :param paper_id: 单篇模式（``POST /papers/{id}/parse``）；与 ``paper_ids`` 二选一。
    """
    ids: list[int] = []
    if paper_id is not None:
        ids.append(int(paper_id))
    if paper_ids:
        ids.extend(int(item) for item in paper_ids)

    task_id = uuid.uuid4().hex[:12]
    _register_task(
        task_id,
        {
            "task_id": task_id,
            "job": JOB_NAME,
            "status": "accepted",
            "paper_id": int(paper_id) if paper_id is not None else None,
            "started_at": _now(),
            "params": {
                "paper_ids": ids or None,
                "limit": limit,
                "force": bool(force),
                "retry_statuses": [str(item) for item in retry_statuses],
                "only_pending": bool(only_pending),
                "delay_seconds": delay_seconds,
            },
        },
    )

    def _worker() -> None:
        _update_task(task_id, status="running")
        try:
            report = runner(
                paper_ids=ids or None,
                limit=limit,
                force=force,
                delay_seconds=delay_seconds,
                retry_statuses=retry_statuses,
                only_pending=only_pending,
                task_id=task_id,
            )
            _update_task(
                task_id,
                status=report.status,
                report=report.to_dict(),
                finished_at=report.finished_at,
            )
        except Exception as exc:  # noqa: BLE001 - 后台失败要能被查询到
            logger.exception("parse_task_failed task_id=%s", task_id)
            _update_task(
                task_id,
                status="failed",
                error=f"{type(exc).__name__}: {exc}",
                finished_at=_now(),
            )

    thread = threading.Thread(target=_worker, name=f"sciloop-{JOB_NAME}-{task_id}", daemon=True)
    thread.start()
    return {
        "task_id": task_id,
        "status": "accepted",
        "job": JOB_NAME,
        "paper_ids": ids or None,
        "thread": thread.name,
    }


# --------------------------------------------------------------------------------------
# CLI
# --------------------------------------------------------------------------------------
def _parse_args(argv: Sequence[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="SciLoop 批量全文解析（WP05-T6）")
    parser.add_argument("--limit", type=int, default=DEFAULT_LIMIT, help="本轮解析上限")
    parser.add_argument(
        "--paper-ids",
        default=None,
        help="逗号分隔的 paper_id；给定时忽略候选筛选（重跑指定论文）",
    )
    parser.add_argument("--force", action="store_true", help="忽略已有记录强制重新解析")
    parser.add_argument(
        "--delay-seconds",
        type=float,
        default=DEFAULT_DELAY_SECONDS,
        help=f"每篇之间的限速间隔（默认 {DEFAULT_DELAY_SECONDS}s）",
    )
    parser.add_argument(
        "--retry-statuses",
        default="",
        help=(
            "只重试这些 parse_status（逗号分隔，如 partial,unavailable,failed）；"
            "传空表示按 only_pending 选『从未解析 + 非 ok』的全部论文"
        ),
    )
    parser.add_argument(
        "--no-only-pending",
        action="store_true",
        help="候选集包含已有 ok 记录的论文（配合 --force 才能重解析）",
    )
    parser.add_argument("--out", default=None, help="把报告写到该 JSON 文件")
    return parser.parse_args(argv)


def main(argv: Sequence[str] | None = None) -> int:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")
    args = _parse_args(argv)
    paper_ids = (
        [int(item) for item in args.paper_ids.split(",") if item.strip()]
        if args.paper_ids
        else None
    )
    retry_statuses = tuple(
        item.strip() for item in (args.retry_statuses or "").split(",") if item.strip()
    )
    report = run_parse_fulltext_sync(
        paper_ids=paper_ids,
        limit=args.limit,
        force=bool(args.force),
        delay_seconds=max(0.0, float(args.delay_seconds)),
        retry_statuses=retry_statuses,
        only_pending=not args.no_only_pending,
    )
    payload = json.dumps(report.to_dict(), ensure_ascii=False, indent=2)
    print(payload)
    if args.out:
        with open(args.out, "w", encoding="utf-8") as handle:
            handle.write(payload)
    return 0 if report.status == "done" else 1


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
