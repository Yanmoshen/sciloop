# Copyright 2026 SciLoop contributors
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
"""定时增量抓取任务（WP03-T9）：arXiv 发现 → 身份映射 → 多源富化 → 留痕。

一轮抓取做了什么
----------------
1. **发现**：按 ``ARXIV_FIELDS`` 分类 + ``submittedDate`` 增量窗口分页拉 arXiv；
2. **身份映射**：每篇走 ``identity.upsert_paper``（命中复用 / 未命中新建），
   同时写 ``paper_identities``（含 title_hash）；
3. **富化**：Semantic Scholar（带 key，1 QPS）取引用数/venue；
   **S2 不可用（429/5xx/无 key）→ 断路器打开 → 本批次全部降级 OpenAlex**，
   降级后引用数仍必须取得到；再由 comment 抽出的 GitHub 链接取 stars；
4. **留痕**：每个取数字段一行 ``paper_source_records``（含 request_url / http_status /
   confidence）；原始响应合并进 ``papers.raw``（``arxiv`` / ``openalex`` / ``github`` 分键）；
5. **批量写**：每 ``BATCH_COMMIT_SIZE`` 篇提交一次，避免逐条提交；
6. **去重**：``published_at`` 早于库内该领域的最大值时跳过（增量），批次内按标识去重。

调用方式
--------
- ``await run_fetch(...)``（异步核心）
- ``run_fetch_sync(...)``（脚本/APScheduler 用）
- ``submit_fetch(...) -> task_id``（API 的 ``POST /papers/fetch`` 用，后台线程执行，返回 task_id）
- ``python -m tasks.jobs.fetch_papers --limit 100``（手动跑一轮并打印报告）
"""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import os
import sys
import threading
import time
import uuid
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import UTC, date, datetime
from pathlib import Path
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from core.config import get_settings
from db.models.paper import Paper
from db.session import SessionLocal
from services.paper_source import arxiv_client, github_client, identity, openalex_client
from services.paper_source import s2_client as s2_module
from services.paper_source import source_records as sr
from services.paper_source.cache import cache_stats, close_all_clients
from services.paper_source.openalex_client import OpenAlexUnavailable
from services.paper_source.s2_client import S2Unavailable

logger = logging.getLogger("sciloop.wp03.fetch_papers")

JOB_NAME = "fetch_papers"
DEFAULT_LIMIT = 100
BATCH_COMMIT_SIZE = 20
DEFAULT_INTERVAL_HOURS = 6
ALL_SOURCES: tuple[str, ...] = ("arxiv", "semantic_scholar", "openalex", "github")


# --------------------------------------------------------------------------------------
# 报告与任务登记
# --------------------------------------------------------------------------------------
@dataclass
class FetchReport:
    """一轮抓取的可审计报告（API 与日志共用）。"""

    task_id: str
    status: str = "running"
    started_at: str = field(default_factory=lambda: datetime.now(UTC).isoformat())
    finished_at: str | None = None
    fields: list[str] = field(default_factory=list)
    date_from: str | None = None
    date_to: str | None = None
    limit: int = DEFAULT_LIMIT
    sources: list[str] = field(default_factory=list)
    discovered: int = 0
    created: int = 0
    reused: int = 0
    enriched: int = 0
    skipped_duplicate: int = 0
    source_records_written: int = 0
    citation_count_filled: int = 0
    citation_count_null: int = 0
    github_stars_filled: int = 0
    by_source: dict[str, dict[str, Any]] = field(default_factory=dict)
    degraded: list[dict[str, Any]] = field(default_factory=list)
    errors: list[dict[str, Any]] = field(default_factory=list)
    papers_total: int | None = None
    cache: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "task_id": self.task_id,
            "job": JOB_NAME,
            "status": self.status,
            "started_at": self.started_at,
            "finished_at": self.finished_at,
            "params": {
                "fields": self.fields,
                "date_from": self.date_from,
                "date_to": self.date_to,
                "limit": self.limit,
                "sources": self.sources,
            },
            "counts": {
                "discovered": self.discovered,
                "created": self.created,
                "reused": self.reused,
                "enriched": self.enriched,
                "skipped_duplicate": self.skipped_duplicate,
                "source_records_written": self.source_records_written,
                "citation_count_filled": self.citation_count_filled,
                "citation_count_null": self.citation_count_null,
                "github_stars_filled": self.github_stars_filled,
            },
            "by_source": self.by_source,
            "degraded": self.degraded,
            "errors": self.errors,
            "papers_total": self.papers_total,
            "cache": self.cache,
        }


@dataclass
class SourceState:
    """单源在本轮的状态（断路器 + 冷却半开 + 限流余量）。

    - 源级故障（429/5xx/网络/缺配置）→ 打开断路器；
    - 打开后进入**冷却期**：到点后允许一次半开试探（成功即恢复，失败则冷却翻倍）；
    - 缺配置（``missing_api_key`` / ``missing_mailto``）视为不可自动恢复 → 不再重试。
    """

    name: str
    available: bool = True
    reason: str | None = None
    http_status: int | None = None
    requests: int = 0
    ok: int = 0
    failed: int = 0
    skipped: int = 0
    empty: int = 0
    breaker_openings: int = 0
    half_open_success: int = 0
    cooldown_seconds: float = 0.0
    cooldown_until: float | None = None
    non_retryable: bool = False
    ratelimit_remaining: int | None = None
    ratelimit_limit: int | None = None
    ratelimit_reset: int | None = None
    last_error: str | None = None
    last_request_url: str | None = None

    # 冷却基数与上限（秒）
    COOLDOWN_BASE = 15.0
    COOLDOWN_MAX = 120.0

    def should_try(self, now: float) -> bool:
        """当前是否允许再调用该源（含半开试探）。"""
        if self.available:
            return True
        if self.non_retryable:
            return False
        if self.cooldown_until is not None and now >= self.cooldown_until:
            self.cooldown_until = None
            logger.info("source_half_open source=%s openings=%d", self.name, self.breaker_openings)
            return True
        return False

    def open_breaker(
        self,
        *,
        reason: str,
        http_status: int | None,
        error: str | None,
        url: str | None,
        now: float | None = None,
        non_retryable: bool = False,
    ) -> None:
        if self.available:
            logger.warning(
                "source_circuit_opened source=%s reason=%s http_status=%s url=%s",
                self.name,
                reason,
                http_status,
                url,
            )
        was_half_open = not self.available and self.cooldown_until is None
        self.available = False
        self.breaker_openings += 1
        self.reason = reason
        self.http_status = http_status
        self.last_error = error
        self.last_request_url = url
        self.non_retryable = bool(non_retryable)
        if self.non_retryable:
            self.cooldown_seconds = 0.0
            self.cooldown_until = None
            return
        base = self.COOLDOWN_BASE * (2 ** max(0, self.breaker_openings - 1))
        if was_half_open and self.cooldown_seconds:
            base = self.cooldown_seconds * 2
        self.cooldown_seconds = min(self.COOLDOWN_MAX, base)
        self.cooldown_until = (now if now is not None else time.monotonic()) + self.cooldown_seconds
        logger.info(
            "source_cooldown source=%s seconds=%.1f openings=%d",
            self.name,
            self.cooldown_seconds,
            self.breaker_openings,
        )

    def mark_success(self) -> None:
        if not self.available:
            self.half_open_success += 1
            logger.info("source_recovered source=%s openings=%d", self.name, self.breaker_openings)
        self.available = True
        self.reason = None
        self.non_retryable = False
        self.cooldown_seconds = 0.0
        self.cooldown_until = None

    def observe_headers(self, headers: Mapping[str, str] | None) -> None:
        """记录限流余量响应头（OpenAlex / GitHub 都会带）。"""
        if not headers:
            return

        def _int(key: str) -> int | None:
            raw = headers.get(key)
            try:
                return int(str(raw).strip())
            except (TypeError, ValueError):
                return None

        remaining = _int("x-ratelimit-remaining")
        if remaining is not None:
            self.ratelimit_remaining = remaining
        limit = _int("x-ratelimit-limit")
        if limit is not None:
            self.ratelimit_limit = limit
        reset = _int("x-ratelimit-reset")
        if reset is not None:
            self.ratelimit_reset = reset

    def to_dict(self) -> dict[str, Any]:
        return {
            "available": self.available,
            "reason": self.reason,
            "http_status": self.http_status,
            "requests": self.requests,
            "ok": self.ok,
            "failed": self.failed,
            "skipped": self.skipped,
            "empty": self.empty,
            "breaker_openings": self.breaker_openings,
            "half_open_success": self.half_open_success,
            "cooldown_seconds": self.cooldown_seconds,
            "non_retryable": self.non_retryable,
            "ratelimit_remaining": self.ratelimit_remaining,
            "ratelimit_limit": self.ratelimit_limit,
            "ratelimit_reset_seconds": self.ratelimit_reset,
            "last_error": self.last_error,
            "last_request_url": self.last_request_url,
        }


_TASKS: dict[str, dict[str, Any]] = {}
_TASKS_LOCK = threading.Lock()


def get_task(task_id: str) -> dict[str, Any] | None:
    with _TASKS_LOCK:
        task = _TASKS.get(task_id)
        return dict(task) if task else None


def list_tasks(*, include_history: bool = True, limit: int = 50) -> list[dict[str, Any]]:
    """任务列表（新的在前）：内存中的任务为准，再用落盘历史补齐重启前的记录。"""
    with _TASKS_LOCK:
        live = [dict(item) for item in _TASKS.values()]
    merged: dict[str, dict[str, Any]] = {str(item.get("task_id")): item for item in live}
    if include_history:
        for record in _read_history():
            tid = str(record.get("task_id") or "")
            if not tid or tid in merged:
                continue
            merged[tid] = record

    def sort_key(item: dict[str, Any]) -> str:
        return str(item.get("started_at") or "")

    ordered = sorted(merged.values(), key=sort_key, reverse=True)
    return ordered[: max(1, int(limit))]


# --------------------------------------------------------------------------------------
# 任务历史落盘（JSONL）：任务登记只存在进程内存，容器重启即丢；这里留一份快照
# --------------------------------------------------------------------------------------
_HISTORY_FILE_NAME = "fetch_tasks.jsonl"
_HISTORY_LOCK = threading.Lock()


def history_path() -> Path:
    """任务历史文件路径（环境变量 FETCH_TASK_HISTORY_DIR > settings > <backend>/.cache/tasks）。"""
    raw = os.getenv("FETCH_TASK_HISTORY_DIR") or get_settings().fetch_task_history_dir
    base = Path(raw) if raw else Path(__file__).resolve().parents[2] / ".cache" / "tasks"
    return base / _HISTORY_FILE_NAME


def _read_history() -> list[dict[str, Any]]:
    path = history_path()
    if not path.exists():
        return []
    records: list[dict[str, Any]] = []
    try:
        for line in path.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                item = json.loads(line)
            except json.JSONDecodeError:
                continue
            if isinstance(item, dict) and item.get("task_id"):
                records.append(item)
    except OSError:
        return []
    return records


def _persist_task(task: dict[str, Any]) -> None:
    """终态任务写一行历史（同 task_id 覆盖式重写，避免重复行）。"""
    task_id = str(task.get("task_id") or "")
    if not task_id or str(task.get("status")) not in {"done", "failed", "cancelled"}:
        return
    report = task.get("report") or {}
    counts = (report.get("counts") or {}) if isinstance(report, dict) else {}
    by_source = (report.get("by_source") or {}) if isinstance(report, dict) else {}
    failed_total = sum(int((state or {}).get("failed") or 0) for state in by_source.values())
    record = {
        "task_id": task_id,
        "job": task.get("job"),
        "status": task.get("status"),
        "started_at": task.get("started_at"),
        "finished_at": task.get("finished_at"),
        "params": task.get("params") or {},
        "counts": counts,
        "failed_total": failed_total,
        "source_states": {
            name: {
                "requests": (state or {}).get("requests"),
                "ok": (state or {}).get("ok"),
                "failed": (state or {}).get("failed"),
                "http_status": (state or {}).get("http_status"),
                "last_error": (state or {}).get("last_error"),
            }
            for name, state in by_source.items()
        },
        "events": (task.get("events") or [])[-50:],
        "persisted_at": datetime.now(UTC).isoformat(),
    }
    path = history_path()
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        with _HISTORY_LOCK:
            kept = [item for item in _read_history() if str(item.get("task_id")) != task_id]
            kept.append(record)
            kept = kept[-200:]
            tmp = path.with_suffix(".tmp")
            tmp.write_text(
                "\n".join(json.dumps(item, ensure_ascii=False) for item in kept) + "\n",
                encoding="utf-8",
            )
            tmp.replace(path)
    except OSError as exc:  # pragma: no cover - 只影响历史可查性，不阻断抓取
        logger.warning("fetch_history_persist_failed task_id=%s error=%s", task_id, exc)


def _register_task(task_id: str, payload: dict[str, Any]) -> None:
    with _TASKS_LOCK:
        _TASKS[task_id] = payload


def _update_task(task_id: str, **changes: Any) -> None:
    snapshot: dict[str, Any] | None = None
    with _TASKS_LOCK:
        if task_id in _TASKS:
            _TASKS[task_id].update(changes)
            snapshot = dict(_TASKS[task_id])
    if snapshot is not None:
        _persist_task(snapshot)


# --------------------------------------------------------------------------------------
# 任务控制（暂停 / 终止）与事件流、进度 —— 供前端「任务监控」窗口消费
# --------------------------------------------------------------------------------------
class FetchCancelled(Exception):
    """协作式终止：任务在阶段边界收到终止指令后主动退出（已入库的数据保留）。"""


# 阶段 → 完成度（用于进度环；抓取 25% / 富化 75% 的大头）
_STAGE_PROGRESS: dict[str, float] = {
    "accepted": 0.02,
    "discovery": 0.20,
    "enrich": 0.55,
    "persist": 0.92,
    "done": 1.0,
}
_STAGE_LABEL: dict[str, str] = {
    "accepted": "准备中",
    "discovery": "抓取 arXiv 列表",
    "enrich": "富化（补引用与代码）",
    "persist": "写入数据库",
    "done": "已完成",
}


def set_task_control(
    task_id: str, *, pause: bool | None = None, cancel: bool | None = None
) -> dict[str, Any] | None:
    """下发暂停/恢复/终止指令；任务不存在返回 None。"""
    with _TASKS_LOCK:
        task = _TASKS.get(task_id)
        if task is None:
            return None
        control = task.setdefault("control", {"pause": False, "cancel": False})
        if pause is not None:
            control["pause"] = bool(pause)
        if cancel is not None:
            control["cancel"] = bool(cancel)
        return dict(task)


def _control_of(task_id: str | None) -> dict[str, bool]:
    if not task_id:
        return {"pause": False, "cancel": False}
    with _TASKS_LOCK:
        task = _TASKS.get(task_id) or {}
        stored = task.get("control") or {}
        return {"pause": bool(stored.get("pause")), "cancel": bool(stored.get("cancel"))}


def _snapshot_progress(task: dict[str, Any]) -> dict[str, Any]:
    """按阶段给出百分比与预计剩余秒数（estimate=true 表示是估算，不是后端实测）。"""
    stage = str(task.get("stage") or "accepted")
    pct = _STAGE_PROGRESS.get(stage, 0.02)
    started = task.get("started_at")
    elapsed = 0.0
    if isinstance(started, str) and started:
        try:
            elapsed = max(
                0.0, (datetime.now(UTC) - datetime.fromisoformat(started)).total_seconds()
            )
        except ValueError:
            elapsed = 0.0
    eta: float | None
    if pct <= 0.02 or task.get("status") in {"done", "failed", "cancelled"} or elapsed <= 0.5:
        eta = None
    else:
        eta = max(0.0, elapsed / pct - elapsed)
    return {
        "stage": stage,
        "label": _STAGE_LABEL.get(stage, "进行中"),
        "pct": round(pct * 100),
        "elapsed_seconds": round(elapsed, 1),
        "eta_seconds": None if eta is None else round(eta),
        "estimate": True,
    }


def emit_event(task_id: str | None, level: str, text: str, *, stage: str | None = None) -> None:
    """往任务里追加一条事件流记录（level: ok / warn / fail / run）。任务不存在时静默忽略。"""
    if not task_id:
        return
    with _TASKS_LOCK:
        task = _TASKS.get(task_id)
        if task is None:
            return
        if stage:
            task["stage"] = stage
        events = task.setdefault("events", [])
        events.append(
            {"at": datetime.now(UTC).isoformat(), "level": level, "text": text, "stage": task.get("stage")}
        )
        task["events"] = events[-200:]
        task["progress"] = _snapshot_progress(task)
        if task.get("status") in {"accepted", "running"}:
            task["progress"]["pct"] = task["progress"]["pct"]


async def check_control(task_id: str | None) -> None:
    """阶段边界调用：收到终止就抛 FetchCancelled；处于暂停就等（最长 10 分钟）。"""
    control = _control_of(task_id)
    if control["cancel"]:
        raise FetchCancelled("任务已收到终止指令")
    if not control["pause"]:
        return
    emit_event(task_id, "warn", "已暂停，等待恢复")
    waited = 0.0
    while True:
        await asyncio.sleep(0.5)
        waited += 0.5
        control = _control_of(task_id)
        if control["cancel"]:
            raise FetchCancelled("暂停期间收到终止指令")
        if not control["pause"]:
            emit_event(task_id, "ok", "已恢复执行")
            return
        if waited >= 600:
            emit_event(task_id, "warn", "暂停超过 10 分钟，自动恢复")
            set_task_control(task_id, pause=False)
            return


# --------------------------------------------------------------------------------------
# 抓取主体
# --------------------------------------------------------------------------------------
class FetchRun:
    """一轮抓取的执行体（同步 Session + 异步 HTTP）。"""

    def __init__(
        self,
        *,
        fields: Sequence[str] | None = None,
        date_from: date | datetime | str | None = None,
        date_to: date | datetime | str | None = None,
        limit: int = DEFAULT_LIMIT,
        sources: Sequence[str] | None = None,
        task_id: str | None = None,
        session: Session | None = None,
        s2: Any | None = None,
        openalex: Any | None = None,
        github: Any | None = None,
        commit_every: int = BATCH_COMMIT_SIZE,
        skip_existing: bool = True,
    ) -> None:
        self.task_id = task_id or uuid.uuid4().hex[:12]
        self.report = FetchReport(
            task_id=self.task_id,
            fields=list(fields or arxiv_client.parse_fields()),
            date_from=str(date_from) if date_from else None,
            date_to=str(date_to) if date_to else None,
            limit=int(limit),
            sources=[s for s in (sources or ALL_SOURCES) if s],
        )
        self.date_from = date_from
        self.date_to = date_to
        self.session = session
        self._owns_session = session is None
        self.commit_every = max(1, int(commit_every))
        self.skip_existing = skip_existing
        self.s2 = s2 if s2 is not None else s2_module.S2Client()
        self.openalex = openalex if openalex is not None else openalex_client.OpenAlexClient()
        self.github = github if github is not None else github_client.GitHubClient()
        self.states: dict[str, SourceState] = {
            name: SourceState(name=name) for name in self.report.sources
        }
        # 缺配置的源直接算"降级"，避免 100 篇论文各撞一次墙
        if "semantic_scholar" in self.states and not self.s2.configured:
            self.states["semantic_scholar"].reason = "missing_api_key"
        if "openalex" in self.states and not self.openalex.configured:
            self.states["openalex"].available = False
            self.states["openalex"].reason = "missing_mailto"

    # ---------------------------------------------------------------- 工具
    def _state(self, name: str) -> SourceState | None:
        return self.states.get(name)

    def _degrade(
        self,
        name: str,
        *,
        reason: str,
        http_status: int | None,
        error: str | None,
        url: str | None,
        open_breaker: bool,
    ) -> None:
        """登记一次降级事件。

        ``open_breaker=False``（单条请求被拒，如 400）只记录事件、**不熔断整源**——
        否则一个坏标题就能让整批论文失去引用数（实测踩过）。
        """
        state = self._state(name)
        if state is None:
            return
        if open_breaker:
            state.open_breaker(
                reason=reason,
                http_status=http_status,
                error=error,
                url=url,
                non_retryable=reason in {"missing_api_key", "missing_mailto"},
            )
        else:
            state.last_error = error or state.last_error
            state.last_request_url = url or state.last_request_url
        self.report.degraded.append(
            {
                "source": name,
                "reason": reason,
                "http_status": http_status,
                "detail": error,
                "request_url": url,
                "breaker_opened": bool(open_breaker),
                "at": datetime.now(UTC).isoformat(),
            }
        )

    def _record_not_found(
        self,
        session: Session,
        paper_id: int,
        source: str,
        fields: Sequence[str],
        *,
        request_url: str | None,
        http_status: int | None,
    ) -> int:
        """源可用但没查到此论文：写"确实没取到"的留痕（raw_value 全为 NULL + 真实状态码）。"""
        return sr.record_fields_for_fetch(
            session,
            paper_id=paper_id,
            source=source,
            request_url=request_url,
            http_status=http_status,
            fields=dict.fromkeys(fields),
        )

    def _record_attempt_failure(
        self,
        session: Session,
        paper_id: int,
        source: str,
        fields: Sequence[str],
        *,
        request_url: str | None,
        http_status: int | None,
    ) -> int:
        """请求真的发出但失败了（429/5xx）→ 同样留痕（raw_value=NULL + 真实状态码）。

        ``http_status`` 为空（连请求都没发出去，如网络层异常）时不写，避免留下
        "无状态码的留痕"（A4 要求每条留痕都有 http_status）。
        """
        if http_status is None:
            return 0
        return self._record_not_found(
            session,
            paper_id,
            source,
            fields,
            request_url=request_url,
            http_status=http_status,
        )

    # ---------------------------------------------------------------- 主流程
    async def execute(self) -> FetchReport:
        session = self.session or (SessionLocal() if SessionLocal is not None else None)
        if session is None:
            raise RuntimeError("数据库会话工厂不可用（DATABASE_URL / psycopg 未就绪）")
        own_session = self.session is None
        try:
            self.report.papers_total = int(
                session.execute(select(func.count()).select_from(Paper)).scalar_one() or 0
            )
            emit_event(
                self.task_id,
                "ok",
                f"任务开始：limit={self.report.limit}，来源 {'/'.join(self.report.sources)}",
                stage="discovery",
            )
            await check_control(self.task_id)
            floor = self._published_floor(session)
            papers, results = await arxiv_client.iter_papers(
                fields=self.report.fields,
                date_from=self.date_from,
                date_to=self.date_to,
                limit=self.report.limit,
            )
            self._record_arxiv_requests(results)
            self.report.discovered = len(papers)
            arxiv_state = self._state("arxiv")
            if papers:
                emit_event(
                    self.task_id,
                    "ok",
                    f"arXiv 返回 {len(papers)} 条候选论文",
                    stage="enrich",
                )
            else:
                emit_event(
                    self.task_id,
                    "fail",
                    "arXiv 本轮没有返回任何论文"
                    + (f"（{arxiv_state.last_error}）" if arxiv_state and arxiv_state.last_error else ""),
                    stage="enrich",
                )

            # 发现阶段结束、进入逐篇处理前再查一次控制指令（空结果时也能及时响应终止）
            await check_control(self.task_id)

            pending = 0
            seen_ids: set[str] = set()
            for paper in papers:
                if paper.arxiv_id in seen_ids:
                    self.report.skipped_duplicate += 1
                    continue
                seen_ids.add(paper.arxiv_id)
                if (
                    self.skip_existing
                    and floor is not None
                    and paper.published
                    and paper.published < floor
                ):
                    self.report.skipped_duplicate += 1
                    logger.info(
                        "skip_by_published_at arxiv_id=%s published=%s floor=%s",
                        paper.arxiv_id,
                        paper.published,
                        floor,
                    )
                    continue
                await check_control(self.task_id)
                await self._process_paper(session, paper)
                pending += 1
                if pending % self.commit_every == 0:
                    session.commit()
                    logger.info(
                        "fetch_batch_committed size=%d created=%d", pending, self.report.created
                    )

            session.commit()
            self.report.papers_total = int(
                session.execute(select(func.count()).select_from(Paper)).scalar_one() or 0
            )
            self.report.by_source = {name: state.to_dict() for name, state in self.states.items()}
            self.report.cache = cache_stats()
            self.report.status = "done"
            emit_event(
                self.task_id,
                "ok",
                f"入库完成：实际拉取 {self.report.created} · 重复 {self.report.reused}",
                stage="persist",
            )
            for name, state in self.states.items():
                if state.failed:
                    emit_event(
                        self.task_id,
                        "warn",
                        f"{name} 有 {state.failed} 次请求失败（{state.last_error or '见来源明细'}）",
                    )
            emit_event(
                self.task_id,
                "ok",
                f"任务完成，写取数留痕 {self.report.source_records_written} 条",
                stage="done",
            )
        except FetchCancelled as exc:
            session.rollback()
            self.report.status = "cancelled"
            self.report.errors.append({"stage": "run", "error": str(exc)})
            emit_event(self.task_id, "fail", "任务已终止（已入库的数据保留）", stage="done")
            logger.warning("fetch_run_cancelled task_id=%s", self.task_id)
            return self.report
        except Exception as exc:  # noqa: BLE001 - 任务失败也要给出可读报告
            session.rollback()
            self.report.status = "failed"
            emit_event(
                self.task_id,
                "fail",
                f"任务失败：{type(exc).__name__}: {exc}",
                stage="done",
            )
            self.report.errors.append({"stage": "run", "error": f"{type(exc).__name__}: {exc}"})
            logger.exception("fetch_run_failed task_id=%s", self.task_id)
            raise
        finally:
            self.report.finished_at = datetime.now(UTC).isoformat()
            if own_session:
                session.close()
            logger.info(
                "fetch_run_done %s", json.dumps(self.report.to_dict()["counts"], ensure_ascii=False)
            )
        return self.report

    def _published_floor(self, session: Session) -> date | None:
        """增量下限：库内已有论文的最近 ``published_at``（仅用于跳过更老的条目）。"""
        if not self.skip_existing:
            return None
        if self.date_from:
            # 显式给了时间窗就以窗口起点为下限（历史回填不会被库内最新日期挡住）
            return identity.coerce_date(self.date_from)
        value = session.execute(select(func.max(Paper.published_at))).scalar_one_or_none()
        return identity.coerce_date(value)

    def _record_arxiv_requests(self, results: Sequence[Any]) -> None:
        state = self._state("arxiv")
        if state is None:
            return
        for result in results:
            state.requests += 1
            state.last_request_url = result.request_url
            if result.ok:
                state.ok += 1
            else:
                state.failed += 1
                state.last_error = result.error
            if result.from_cache:
                self.report.cache.setdefault("arxiv_cache_hits", 0)
                self.report.cache["arxiv_cache_hits"] += 1

    # ---------------------------------------------------------------- 单篇处理
    async def _process_paper(self, session: Session, paper: arxiv_client.ArxivPaper) -> None:
        meta = paper.to_meta()
        result = identity.upsert_paper(session, meta)
        if result.created:
            self.report.created += 1
        else:
            self.report.reused += 1

        # arXiv 自身元数据留痕（权威源，confidence 1.0）
        self.report.source_records_written += sr.record_fields_for_fetch(
            session,
            paper_id=result.paper_id,
            source="arxiv",
            request_url=paper.request_url,
            http_status=paper.http_status,
            fields={
                "title": paper.title,
                "abstract": paper.abstract,
                "authors": paper.authors or None,
                "published_at": paper.published,
                "updated_at_src": paper.updated,
                "pdf_url": paper.pdf_url,
                "comment": paper.comment,
                "doi": paper.doi,
                "primary_category": paper.primary_category,
            },
        )
        # comment 派生的 venue 线索与代码链接（confidence 0.6）
        self.report.source_records_written += sr.record_fields_for_fetch(
            session,
            paper_id=result.paper_id,
            source="arxiv_comment",
            request_url=paper.request_url,
            http_status=paper.http_status,
            fields={"venue": paper.venue_hint, "code_url": paper.code_url},
        )

        enriched = await self._enrich_citations(session, result.paper_id, paper)
        enriched = await self._enrich_github(session, result.paper_id, paper) or enriched
        if enriched:
            self.report.enriched += 1

        # 新建且所有源都没给出引用数 → 显式 NULL（避免 DB DEFAULT 0 被误读成"零引用"）
        if result.created:
            # 必须先 flush：WP01 的 SessionLocal 是 autoflush=False，
            # 否则上面 add 的留痕对下面的查询不可见，会把真实取到的引用数又置成 NULL（实测踩过）
            session.flush()
            has_citation = session.execute(
                select(func.count())
                .select_from(sr.PaperSourceRecord)
                .where(
                    sr.PaperSourceRecord.paper_id == result.paper_id,
                    sr.PaperSourceRecord.field_name == sr.FIELD_CITATION_COUNT,
                    sr.PaperSourceRecord.raw_value.is_not(None),
                )
            ).scalar_one()
            if not has_citation:
                identity.force_null(session, result.paper_id, "citation_count")
                self.report.citation_count_null += 1
            else:
                self.report.citation_count_filled += 1

        session.flush()

    # ---------------------------------------------------------------- 引用数 / venue
    async def _enrich_citations(
        self, session: Session, paper_id: int, paper: arxiv_client.ArxivPaper
    ) -> bool:
        now = time.monotonic()
        s2_state = self._state("semantic_scholar")
        if s2_state is not None and s2_state.should_try(now):
            fetched = await self._try_s2(session, paper_id, paper)
            if fetched:
                return True
        openalex_state = self._state("openalex")
        if openalex_state is not None and openalex_state.should_try(time.monotonic()):
            return await self._try_openalex(session, paper_id, paper)
        return False

    async def _try_s2(
        self, session: Session, paper_id: int, paper: arxiv_client.ArxivPaper
    ) -> bool:
        state = self._state("semantic_scholar")
        assert state is not None  # noqa: S101 - 由调用方保证
        state.requests += 1
        try:
            item = await self.s2.get_paper(arxiv_id=paper.arxiv_id, doi=paper.doi)
        except S2Unavailable as exc:
            state.failed += 1
            state.last_request_url = exc.request_url
            self.report.source_records_written += self._record_attempt_failure(
                session,
                paper_id,
                "semantic_scholar",
                ("citation_count", "venue", "publication_date"),
                request_url=exc.request_url,
                http_status=exc.status_code,
            )
            self._degrade(
                "semantic_scholar",
                reason=exc.reason,
                http_status=exc.status_code,
                error=exc.detail,
                url=exc.request_url,
                open_breaker=exc.is_source_outage,
            )
            return False
        except Exception as exc:  # noqa: BLE001 - 未知异常同样降级，不阻断主流程
            state.failed += 1
            self._degrade(
                "semantic_scholar",
                reason="unexpected_error",
                http_status=None,
                error=f"{type(exc).__name__}: {exc}",
                url=state.last_request_url,
                open_breaker=True,
            )
            return False

        if item is None:
            state.empty += 1
            last = self.s2.last_lookup
            state.observe_headers(last.get("headers"))
            state.mark_success()  # 404 也说明源是通的
            self.report.source_records_written += self._record_not_found(
                session,
                paper_id,
                "semantic_scholar",
                ("citation_count", "venue", "publication_date"),
                request_url=last.get("request_url"),
                http_status=last.get("http_status"),
            )
            return False
        state.ok += 1
        state.last_request_url = item.request_url
        state.observe_headers(self.s2.last_lookup.get("headers"))
        state.mark_success()
        payload = item.as_dict()
        # force_paper_id：富化步骤只允许并入已解析出的主记录，绝不新建论文
        # 注意：raw 直接传 payload 本体——identity._merge_raw 会按 source 分键
        # （曾误传 {"semantic_scholar": payload} 导致 raw 双层嵌套，WP04 读不到）
        identity.upsert_paper(
            session,
            {
                "source": "semantic_scholar",
                "external_id": item.s2_id or paper.arxiv_id,
                "identifiers": item.identifiers(),
                "title": item.title or paper.title,
                "venue": item.venue,
                "venue_source": "s2" if item.venue else None,
                "published_at": item.publication_date,
                "citation_count": item.citation_count,
                "raw": payload,
            },
            force_paper_id=paper_id,
        )
        self.report.source_records_written += sr.record_fields_for_fetch(
            session,
            paper_id=paper_id,
            source="semantic_scholar",
            request_url=item.request_url,
            http_status=item.http_status,
            fields={
                "citation_count": item.citation_count,
                "venue": item.venue,
                "publication_date": item.publication_date,
                "fields_of_study": item.fields_of_study or None,
                "external_ids": item.external_ids or None,
            },
        )
        if item.citation_count is not None:
            self.report.citation_count_filled += 1
        return True

    async def _try_openalex(
        self, session: Session, paper_id: int, paper: arxiv_client.ArxivPaper
    ) -> bool:
        state = self._state("openalex")
        assert state is not None  # noqa: S101 - 由调用方保证
        state.requests += 1
        try:
            work = await self.openalex.get_work(
                arxiv_id=paper.arxiv_id, doi=paper.doi, title=paper.title
            )
        except OpenAlexUnavailable as exc:
            state.failed += 1
            state.last_request_url = exc.request_url
            self.report.source_records_written += self._record_attempt_failure(
                session,
                paper_id,
                "openalex",
                ("citation_count", "venue", "publication_date", "counts_by_year", "institutions"),
                request_url=exc.request_url,
                http_status=exc.status_code,
            )
            self._degrade(
                "openalex",
                reason=exc.reason,
                http_status=exc.status_code,
                error=exc.detail,
                url=exc.request_url,
                open_breaker=exc.is_source_outage,
            )
            return False
        except Exception as exc:  # noqa: BLE001
            state.failed += 1
            self._degrade(
                "openalex",
                reason="unexpected_error",
                http_status=None,
                error=f"{type(exc).__name__}: {exc}",
                url=state.last_request_url,
                open_breaker=True,
            )
            return False

        if work is None:
            state.empty += 1
            last = self.openalex.last_lookup
            state.observe_headers(last.get("headers"))
            state.mark_success()  # 查不到 ≠ 源不可用
            self.report.source_records_written += self._record_not_found(
                session,
                paper_id,
                "openalex",
                ("citation_count", "venue", "publication_date", "counts_by_year", "institutions"),
                request_url=last.get("request_url"),
                http_status=last.get("http_status"),
            )
            return False
        state.ok += 1
        state.last_request_url = work.request_url
        state.observe_headers(self.openalex.last_lookup.get("headers"))
        state.mark_success()
        payload = work.raw_payload()
        # raw 传 payload 本体（identity._merge_raw 按 source 分键），避免双层嵌套
        identity.upsert_paper(
            session,
            {
                "source": "openalex",
                "external_id": work.openalex_id or work.doi or paper.arxiv_id,
                "identifiers": work.identifiers(),
                "title": work.title or paper.title,
                "venue": work.venue,
                "venue_source": "openalex" if work.venue else None,
                "published_at": work.publication_date,
                "citation_count": work.cited_by_count,
                "authors": work.authors or None,
                "raw": payload,
            },
            force_paper_id=paper_id,
        )
        self.report.source_records_written += sr.record_fields_for_fetch(
            session,
            paper_id=paper_id,
            source="openalex",
            request_url=work.request_url,
            http_status=work.http_status,
            fields={
                "citation_count": work.cited_by_count,
                "venue": work.venue,
                "publication_date": work.publication_date,
                "counts_by_year": work.counts_by_year or None,
                "institutions": work.institutions or None,
                "matched_by": work.matched_by,
            },
        )
        if work.cited_by_count is not None:
            self.report.citation_count_filled += 1
        return True

    # ---------------------------------------------------------------- GitHub
    async def _enrich_github(
        self, session: Session, paper_id: int, paper: arxiv_client.ArxivPaper
    ) -> bool:
        state = self._state("github")
        if state is None or not state.should_try(time.monotonic()):
            return False
        if not paper.code_url:
            # 没有代码链接就不发请求，也就不写 GitHub 留痕（留痕只记录真实发生过的请求）
            state.skipped += 1
            return False
        state.requests += 1
        try:
            repo = await self.github.get_repo(paper.code_url)
        except Exception as exc:  # noqa: BLE001 - GitHub 失败不致命
            state.failed += 1
            logger.warning("github_lookup_failed paper_id=%s error=%s", paper_id, exc)
            return False
        if repo is None:
            state.empty += 1
            return False
        state.last_request_url = repo.request_url
        state.observe_headers(repo.http_headers)
        if repo.ok:
            state.ok += 1
            state.mark_success()
        elif repo.error == "rate_limited":
            state.failed += 1
            state.last_error = repo.error
            self._degrade(
                "github",
                reason="rate_limited",
                http_status=repo.http_status,
                error=repo.error,
                url=repo.request_url,
                open_breaker=True,
            )
        else:
            state.failed += 1
            state.last_error = repo.error
        # 代码仓库不是论文身份：只并入 raw，绝不新建 papers
        identity.merge_source_payload(session, paper_id, "github", repo.raw_payload())
        self.report.source_records_written += sr.record_fields_for_fetch(
            session,
            paper_id=paper_id,
            source="github",
            request_url=repo.request_url,
            http_status=repo.http_status,
            fields={
                "code_url": repo.html_url or paper.code_url,
                "stargazers_count": repo.stargazers_count,
            },
        )
        if repo.stargazers_count is not None:
            self.report.github_stars_filled += 1
            return True
        return False


# --------------------------------------------------------------------------------------
# 对外入口
# --------------------------------------------------------------------------------------
async def run_fetch(
    *,
    fields: Sequence[str] | None = None,
    date_from: date | datetime | str | None = None,
    date_to: date | datetime | str | None = None,
    limit: int = DEFAULT_LIMIT,
    sources: Sequence[str] | None = None,
    task_id: str | None = None,
    skip_existing: bool = True,
) -> FetchReport:
    """异步跑一轮抓取（同一事件循环内）。"""
    run = FetchRun(
        fields=fields,
        date_from=date_from,
        date_to=date_to,
        limit=limit,
        sources=sources,
        task_id=task_id,
        skip_existing=skip_existing,
    )
    try:
        return await run.execute()
    finally:
        await close_all_clients()


def run_fetch_sync(
    *,
    fields: Sequence[str] | None = None,
    date_from: date | datetime | str | None = None,
    date_to: date | datetime | str | None = None,
    limit: int = DEFAULT_LIMIT,
    sources: Sequence[str] | None = None,
    skip_existing: bool = True,
    task_id: str | None = None,
) -> FetchReport:
    """同步跑一轮（脚本 / APScheduler / 后台线程用）。``task_id`` 用于回填事件流与进度。"""
    return asyncio.run(
        run_fetch(
            fields=fields,
            date_from=date_from,
            date_to=date_to,
            limit=limit,
            sources=sources,
            skip_existing=skip_existing,
            task_id=task_id,
        )
    )


def submit_fetch(
    *,
    fields: Sequence[str] | None = None,
    date_from: date | datetime | str | None = None,
    date_to: date | datetime | str | None = None,
    limit: int = DEFAULT_LIMIT,
    sources: Sequence[str] | None = None,
    skip_existing: bool = True,
    runner: Callable[..., FetchReport] = run_fetch_sync,
) -> dict[str, Any]:
    """把一轮抓取丢到后台线程并立即返回 ``task_id``（``POST /papers/fetch`` 用）。"""
    task_id = uuid.uuid4().hex[:12]
    _register_task(
        task_id,
        {
            "task_id": task_id,
            "job": JOB_NAME,
            "status": "accepted",
            "stage": "accepted",
            "control": {"pause": False, "cancel": False},
            "events": [],
            "progress": {"stage": "accepted", "label": "准备中", "pct": 2, "eta_seconds": None, "estimate": True},
            "started_at": datetime.now(UTC).isoformat(),
            "params": {
                "fields": list(fields or arxiv_client.parse_fields()),
                "date_from": str(date_from) if date_from else None,
                "date_to": str(date_to) if date_to else None,
                "limit": int(limit),
                "sources": [s for s in (sources or ALL_SOURCES) if s],
                "skip_existing": bool(skip_existing),
            },
        },
    )

    def _worker() -> None:
        _update_task(task_id, status="running")
        try:
            report = runner(
                fields=fields,
                date_from=date_from,
                date_to=date_to,
                limit=limit,
                sources=sources,
                skip_existing=skip_existing,
                task_id=task_id,
            )
            _update_task(
                task_id,
                status=report.status,
                report=report.to_dict(),
                finished_at=report.finished_at,
            )
        except Exception as exc:  # noqa: BLE001 - 后台失败要能被查询到
            logger.exception("fetch_task_failed task_id=%s", task_id)
            _update_task(
                task_id,
                status="failed",
                error=f"{type(exc).__name__}: {exc}",
                finished_at=datetime.now(UTC).isoformat(),
            )

    thread = threading.Thread(target=_worker, name=f"sciloop-{JOB_NAME}-{task_id}", daemon=True)
    thread.start()
    return {"task_id": task_id, "status": "accepted", "job": JOB_NAME, "thread": thread.name}


# --------------------------------------------------------------------------------------
# 调度注册（WP01 的 app/tasks/scheduler.py 若存在则调用本函数）
# --------------------------------------------------------------------------------------
def register_jobs(
    scheduler: Any,
    *,
    interval_hours: int = DEFAULT_INTERVAL_HOURS,
    limit: int = DEFAULT_LIMIT,
    fields: Sequence[str] | None = None,
    job_id: str = JOB_NAME,
) -> Any:
    """把增量抓取注册到 APScheduler 实例；返回 job 对象。"""
    from apscheduler.triggers.interval import IntervalTrigger

    def _job() -> None:
        report = run_fetch_sync(fields=fields, limit=limit)
        logger.info(
            "scheduled_fetch_done created=%d reused=%d records=%d",
            report.created,
            report.reused,
            report.source_records_written,
        )

    job = scheduler.add_job(
        _job,
        trigger=IntervalTrigger(hours=max(1, int(interval_hours))),
        id=job_id,
        name="WP03 论文增量抓取",
        replace_existing=True,
        max_instances=1,
        coalesce=True,
    )
    logger.info("fetch_papers_job_registered interval_hours=%d limit=%d", interval_hours, limit)
    return job


def build_scheduler(
    *, interval_hours: int = DEFAULT_INTERVAL_HOURS, limit: int = DEFAULT_LIMIT
) -> Any:
    """构建一个仅含本任务的 BlockingScheduler（``--serve`` 用）。"""
    from apscheduler.schedulers.blocking import BlockingScheduler

    scheduler = BlockingScheduler(timezone="UTC")
    register_jobs(scheduler, interval_hours=interval_hours, limit=limit)
    return scheduler


# --------------------------------------------------------------------------------------
# CLI
# --------------------------------------------------------------------------------------
def _parse_args(argv: Sequence[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="SciLoop 论文增量抓取（WP03-T9）")
    parser.add_argument("--fields", default=None, help="逗号分隔的 arXiv 分类，默认取 ARXIV_FIELDS")
    parser.add_argument("--limit", type=int, default=DEFAULT_LIMIT, help="本轮抓取上限")
    parser.add_argument("--date-from", default=None, help="增量起点（YYYY-MM-DD）")
    parser.add_argument("--date-to", default=None, help="时间窗终点（YYYY-MM-DD，历史回填场景用）")
    parser.add_argument("--sources", default=",".join(ALL_SOURCES), help="启用哪些富化源")
    parser.add_argument(
        "--no-skip-existing",
        action="store_true",
        help="忽略现有 published_at 增量下限（首次全量灌数/验收用）",
    )
    parser.add_argument("--out", default=None, help="把报告写到该 JSON 文件")
    parser.add_argument("--serve", action="store_true", help="按 IntervalTrigger 常驻调度")
    parser.add_argument("--interval-hours", type=int, default=DEFAULT_INTERVAL_HOURS)
    return parser.parse_args(argv)


def main(argv: Sequence[str] | None = None) -> int:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")
    args = _parse_args(argv)
    fields = [f.strip() for f in args.fields.split(",")] if args.fields else None
    sources = [s.strip() for s in args.sources.split(",") if s.strip()]

    if args.serve:
        scheduler = build_scheduler(interval_hours=args.interval_hours, limit=args.limit)
        logger.info("fetch_papers_scheduler_started interval_hours=%d", args.interval_hours)
        scheduler.start()
        return 0

    report = run_fetch_sync(
        fields=fields,
        date_from=args.date_from,
        date_to=args.date_to,
        limit=args.limit,
        sources=sources,
        skip_existing=not args.no_skip_existing,
    )
    payload = json.dumps(report.to_dict(), ensure_ascii=False, indent=2)
    print(payload)
    if args.out:
        with open(args.out, "w", encoding="utf-8") as handle:
            handle.write(payload)
    return 0 if report.status == "done" else 1


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
