# Copyright 2026 SciLoop contributors
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
"""翻译任务注册表与后台执行（长任务语义）。

契约
----

- 两个 POST **立即**返回 ``202 + task_id``，翻译在后台线程执行；
- 进度只能轮询 ``GET /translate/jobs/{task_id}``；
- 状态机：``pending → parsing → rewriting → rendering → highlighting → completed``，
  异常路径 ``error``，取消路径 ``cancelled``（终态：``completed`` / ``error`` / ``cancelled``）；
- ``cancel`` 只对非终态有效，``retry`` 只对 ``error`` / ``cancelled`` 有效；
- **任务状态只存内存，进程重启即丢失**；磁盘产物与 manifest 保留（见 :data:`RESTART_NOTE`），
  ``get_task`` 在内存未命中时会回落到 manifest 恢复只读快照；
- 并发上限 ``min(2, EXECUTOR_MAX_CONCURRENCY)``（契约：≤2）。

实现说明
--------

与 ``app/services/ingest/jobs.py`` 不同的一点：翻译链路**必须**写 ``llm_call_logs``
（LLM 记账红线），而异步连接池（asyncpg）与事件循环绑定。因此本模块按
``app/services/parsing/card_builder.py`` 的口径，**在 FastAPI 的既有事件循环内**
用 ``asyncio.create_task`` 调度，把 CPU 密集段（pymupdf 解析/回写/高亮）用
``asyncio.to_thread`` 丢到线程里，**避免后台线程与主循环争用同一个异步连接池**。
只有在没有事件循环的同步上下文（CLI）里才退化为「后台线程 + 自建循环」。
"""

from __future__ import annotations

import asyncio
import logging
import threading
import uuid
from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from services.translate import ENGINE_NAME, RESTART_NOTE, artifacts, engine, highlight

logger = logging.getLogger("sciloop.translate.jobs")

#: 对外契约状态值
STATUS_PENDING = "pending"
STATUS_PARSING = "parsing"
STATUS_REWRITING = "rewriting"
STATUS_RENDERING = "rendering"
STATUS_HIGHLIGHTING = "highlighting"
STATUS_COMPLETED = "completed"
STATUS_ERROR = "error"
STATUS_CANCELLED = "cancelled"

ALL_STATUSES: tuple[str, ...] = (
    STATUS_PENDING,
    STATUS_PARSING,
    STATUS_REWRITING,
    STATUS_RENDERING,
    STATUS_HIGHLIGHTING,
    STATUS_COMPLETED,
    STATUS_ERROR,
    STATUS_CANCELLED,
)

#: 终态（不可再取消/推进）
TERMINAL_STATUSES: frozenset[str] = frozenset(
    {STATUS_COMPLETED, STATUS_ERROR, STATUS_CANCELLED}
)
#: 可重跑的状态
RETRYABLE_STATUSES: frozenset[str] = frozenset({STATUS_ERROR, STATUS_CANCELLED})

VALID_MODES: tuple[str, ...] = ("translate", "simplify")


def _now() -> str:
    return datetime.now(UTC).isoformat()


def _empty_highlight_summary() -> dict[str, Any]:
    return {"core_conclusion": 0, "method_innovation": 0, "key_data": 0, "status": "skipped"}


def _empty_formats() -> dict[str, Any]:
    return {"mono": {"available": False}, "dual": {"available": False}}


def max_concurrency() -> int:
    """并发上限（契约 ≤2；取 ``EXECUTOR_MAX_CONCURRENCY`` 与环境的最小值）。"""
    try:
        from core.config import get_settings

        configured = int(getattr(get_settings(), "executor_max_concurrency", 2) or 2)
    except Exception:  # noqa: BLE001 - 配置不可用时按契约上限
        configured = 2
    return max(1, min(2, configured))


@dataclass
class TranslateTask:
    """一个翻译任务的运行态快照（内存对象 + 磁盘产物引用）。"""

    task_id: str
    mode: str
    highlight_requested: bool
    filename: str
    paper_id: int | None = None
    source_bytes: bytes | None = field(default=None, repr=False)
    source_pages: int | None = None
    source_sha256: str | None = None
    status: str = STATUS_PENDING
    percent: int = 0
    stage: str = STATUS_PENDING
    message: str = "已受理，等待执行"
    error: str | None = None
    created_at: str = field(default_factory=_now)
    finished_at: str | None = None
    formats: dict[str, Any] = field(default_factory=_empty_formats)
    highlight_summary: dict[str, Any] = field(default_factory=_empty_highlight_summary)
    layout_warnings: list[str] = field(default_factory=list)
    cancel_event: threading.Event = field(default_factory=threading.Event, repr=False)
    #: 仅内存（不落 manifest）：本次运行的过程产物
    outcomes: list[engine.BlockOutcome] = field(default_factory=list, repr=False)
    translator_name: str | None = None
    stub_used: bool = False

    def to_dict(self) -> dict[str, Any]:
        """任务详情（**字段固定**，与前端/阅读器冻结）。"""
        return {
            "task_id": self.task_id,
            "status": self.status,
            "percent": int(self.percent),
            "stage": self.stage,
            "message": self.message,
            "error": self.error,
            "mode": self.mode,
            "highlight": bool(self.highlight_requested),
            "paper_id": self.paper_id,
            "source": {
                "filename": self.filename,
                "pages": self.source_pages,
                "sha256": self.source_sha256,
            },
            "formats": {
                "mono": dict(self.formats.get("mono") or {"available": False}),
                "dual": dict(self.formats.get("dual") or {"available": False}),
            },
            "highlight_summary": dict(self.highlight_summary),
            "layout_warnings": list(self.layout_warnings),
            "created_at": self.created_at,
            "finished_at": self.finished_at,
            "engine": ENGINE_NAME,
            "translator": self.translator_name,
            "note": RESTART_NOTE,
        }


_TASKS: dict[str, TranslateTask] = {}
_TASKS_LOCK = threading.RLock()
_SEMAPHORE: asyncio.Semaphore | None = None
_SEMAPHORE_LOCK = threading.Lock()
#: 主循环内的后台任务引用（防止被 GC 回收）
_BACKGROUND_TASKS: set[asyncio.Task] = set()


def _semaphore() -> asyncio.Semaphore:
    global _SEMAPHORE
    with _SEMAPHORE_LOCK:
        if _SEMAPHORE is None:
            _SEMAPHORE = asyncio.Semaphore(max_concurrency())
        return _SEMAPHORE


# --------------------------------------------------------------------------- #
# 注册表操作
# --------------------------------------------------------------------------- #
def _get_raw(task_id: str) -> TranslateTask | None:
    with _TASKS_LOCK:
        return _TASKS.get(str(task_id))


def _set_progress(
    task_id: str,
    *,
    status: str | None = None,
    stage: str | None = None,
    percent: int | None = None,
    message: str | None = None,
) -> None:
    """更新进度；任务已进入终态时**一律不再改写**（避免取消后被复活）。"""
    with _TASKS_LOCK:
        task = _TASKS.get(task_id)
        if task is None:
            return
        if task.status in TERMINAL_STATUSES:
            return
        if status is not None:
            task.status = status
            if status in TERMINAL_STATUSES:
                task.finished_at = _now()
        if stage is not None:
            task.stage = stage
        if percent is not None:
            task.percent = max(0, min(100, int(percent)))
        if message is not None:
            task.message = message


def _mark_terminal(task_id: str, status: str, *, message: str, error: str | None = None) -> None:
    """强制置终态（取消/失败路径；只对尚未终态的任务生效）。"""
    with _TASKS_LOCK:
        task = _TASKS.get(task_id)
        if task is None or task.status in TERMINAL_STATUSES:
            return
        task.status = status
        task.stage = status
        task.finished_at = _now()
        task.message = message
        if error is not None:
            task.error = error


def new_task(
    *,
    source_bytes: bytes,
    filename: str,
    mode: str,
    highlight_requested: bool,
    paper_id: int | None = None,
) -> TranslateTask:
    """登记一个翻译任务（状态 ``pending``）并启动后台线程。"""
    task = TranslateTask(
        task_id=uuid.uuid4().hex[:12],
        mode=mode,
        highlight_requested=bool(highlight_requested),
        filename=str(filename or "source.pdf"),
        paper_id=paper_id,
        source_bytes=bytes(source_bytes),
    )
    with _TASKS_LOCK:
        _TASKS[task.task_id] = task
    logger.info(
        "translate_task_created task_id=%s mode=%s highlight=%s paper_id=%s bytes=%d",
        task.task_id,
        mode,
        highlight_requested,
        paper_id,
        len(source_bytes),
    )
    _start_thread(task.task_id)
    return task


def submit(
    *,
    source_bytes: bytes,
    filename: str,
    mode: str,
    highlight_requested: bool,
    paper_id: int | None = None,
) -> dict[str, Any]:
    """登记 + 启动，返回对外响应体（``202`` 用）。"""
    task = new_task(
        source_bytes=source_bytes,
        filename=filename,
        mode=mode,
        highlight_requested=highlight_requested,
        paper_id=paper_id,
    )
    return {
        "task_id": task.task_id,
        "status": task.status,
        "mode": task.mode,
        "highlight": bool(task.highlight_requested),
        "engine": ENGINE_NAME,
        "poll_url": f"/api/v1/translate/jobs/{task.task_id}",
        "note": RESTART_NOTE,
    }


def _start_thread(task_id: str) -> None:
    """调度后台执行。

    优先在**既有事件循环**里 ``create_task``（LLM 记账需要主循环的异步连接池）；
    无事件循环的同步上下文（CLI / 脚本）退化为后台线程 + 自建事件循环。
    """
    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        loop = None
    if loop is not None:
        handle = loop.create_task(_run(task_id))
        _BACKGROUND_TASKS.add(handle)
        handle.add_done_callback(_BACKGROUND_TASKS.discard)
        return

    def _worker() -> None:
        try:
            asyncio.run(_run(task_id))
        except Exception as exc:  # noqa: BLE001 - 任务级异常必须落到任务状态
            logger.exception("translate_task_thread_failed task_id=%s", task_id)
            _mark_terminal(
                task_id, STATUS_ERROR, message="任务线程异常终止", error=f"{type(exc).__name__}: {exc}"
            )
            _write_manifest_safe(task_id)

    thread = threading.Thread(target=_worker, name=f"sciloop-translate-{task_id}", daemon=True)
    thread.start()


def get_task(task_id: str) -> dict[str, Any] | None:
    """内存快照；未命中时回落到磁盘 manifest（进程重启后的只读恢复）。"""
    task = _get_raw(task_id)
    if task is not None:
        return task.to_dict()
    manifest = artifacts.read_manifest(task_id)
    if manifest is None:
        return None
    return _detail_from_manifest(manifest)


def list_tasks() -> list[dict[str, Any]]:
    """任务列表：优先内存（含进行中）；内存为空时从磁盘 manifest 恢复只读列表。"""
    with _TASKS_LOCK:
        snapshot = [task.to_dict() for task in _TASKS.values()]
    if snapshot:
        return sorted(snapshot, key=lambda item: str(item.get("created_at") or ""), reverse=True)
    recovered: list[dict[str, Any]] = []
    for task_id in artifacts.list_task_ids(limit=50):
        manifest = artifacts.read_manifest(task_id)
        if manifest is None:
            continue
        recovered.append(_detail_from_manifest(manifest))
    return sorted(recovered, key=lambda item: str(item.get("created_at") or ""), reverse=True)


def cancel(task_id: str) -> tuple[bool, str]:
    """取消任务；返回 ``(是否成功, 原因码)``。"""
    with _TASKS_LOCK:
        task = _TASKS.get(str(task_id))
        if task is None:
            return False, "task_not_found"
        if task.status in TERMINAL_STATUSES:
            return False, "task_already_finished"
        task.cancel_event.set()
        task.status = STATUS_CANCELLED
        task.stage = STATUS_CANCELLED
        task.finished_at = _now()
        task.message = "任务已取消（内存任务状态；已生成的产物保留）"
    logger.info("translate_task_cancelled task_id=%s", task_id)
    _write_manifest_safe(str(task_id))
    return True, STATUS_CANCELLED


def retry(task_id: str) -> tuple[bool, str]:
    """从 ``error`` / ``cancelled`` 重跑；其它状态拒绝。"""
    with _TASKS_LOCK:
        task = _TASKS.get(str(task_id))
        if task is None:
            return False, "task_not_found"
        if task.status not in RETRYABLE_STATUSES:
            return False, "task_already_finished"
        task.cancel_event = threading.Event()
        task.status = STATUS_PENDING
        task.stage = STATUS_PENDING
        task.percent = 0
        task.message = "已重新排队（本次运行将覆盖同名产物）"
        task.error = None
        task.finished_at = None
        task.formats = _empty_formats()
        task.highlight_summary = _empty_highlight_summary()
        task.outcomes = []
        task.layout_warnings = [
            item for item in task.layout_warnings if not item.startswith("retry:")
        ]
        task.layout_warnings.append(
            "retry: 已重置上次运行的内存状态；source.pdf 保持原样，mono/dual/preview 将被本次覆盖"
        )
    logger.info("translate_task_retried task_id=%s", task_id)
    _start_thread(str(task_id))
    return True, STATUS_PENDING


# --------------------------------------------------------------------------- #
# from-paper：复用已入库论文的本地原文
# --------------------------------------------------------------------------- #
async def resolve_paper_source(session: Any, paper_id: int) -> tuple[dict[str, Any] | None, str]:
    """为 ``from-paper`` 找出可用的本地原文 PDF。

    返回 ``(source, code)``：``source`` 为 ``{"filename", "bytes", "via"}``；
    找不到时 ``code ∈ {"paper_not_found", "no_source_document", "source_too_large"}``。
    **不联网下载**：只复用已落盘的上传原文（拒绝把「没原文」伪装成「能翻译」）。
    """
    from sqlalchemy import select

    from db.models.paper import Paper, PaperDocument
    from services.ingest import storage

    paper = (
        await session.execute(select(Paper).where(Paper.id == int(paper_id)))
    ).scalar_one_or_none()
    if paper is None:
        return None, "paper_not_found"

    candidates: list[Path] = []
    raw = paper.raw if isinstance(paper.raw, Mapping) else {}
    upload = raw.get("upload") if isinstance(raw.get("upload"), Mapping) else {}
    stored_path = upload.get("stored_path")
    if stored_path:
        candidates.append(Path(str(stored_path)))

    documents = (
        await session.execute(
            select(PaperDocument)
            .where(PaperDocument.paper_id == int(paper_id))
            .order_by(PaperDocument.id)
        )
    ).scalars().all()
    upload_dir = storage.default_upload_dir()
    for document in documents:
        url = str(document.source_url or "")
        if url.startswith("upload://"):
            candidates.append(upload_dir / url.removeprefix("upload://"))

    for path in candidates:
        try:
            if not path.is_file():
                continue
            size = path.stat().st_size
        except OSError:
            continue
        if size > artifacts.MAX_FILE_BYTES:
            return None, "source_too_large"
        try:
            payload = path.read_bytes()
        except OSError:
            continue
        if not payload:
            continue
        return {"filename": path.name, "bytes": payload, "via": str(path)}, ""

    # 本地候选（上传残留 / upload:// 记录）都没命中 → **委派给阅读模块**。
    #
    # 为什么必须委派而不是自己再写一份（2026-09-26 用户实测反馈）：
    # 论文库里**抓取入库**的论文（arXiv / S2 / OpenAlex）本地从来没有落文件，
    # 阅读模块为此做了"按 papers.pdf_url 现拉一份并缓存"的兜底，翻译模块却没有 ——
    # 于是同一篇论文，阅读页能正常渲染，点「翻译该论文」却回 409「没有可用的本地原文」。
    # 用户看到的就是"这个不是原文是啥？"。
    #
    # 委派之后，「找到原文」只有**一份**实现：缓存命中也好、现拉也好、以后加新来源也好，
    # 两边自动一致（也顺手满足了"没有本地 PDF 就联网取"的口径）。
    from services.reader import documents as reader_documents

    try:
        resolved = await reader_documents.resolve_paper_source(session, paper_id)
    except Exception as exc:  # noqa: BLE001 - 按异常类型映射成翻译模块的 code
        name = type(exc).__name__
        if name == "PaperNotFoundError":
            return None, "paper_not_found"
        if name == "SourceTooLargeError":
            return None, "source_too_large"
        logger.warning("阅读模块也没能拿到原文 paper=%s：%s", paper_id, exc)
        return None, "no_source_document"

    payload = resolved.get("bytes") or b""
    if not payload:
        return None, "no_source_document"
    if len(payload) > artifacts.MAX_FILE_BYTES:
        return None, "source_too_large"
    return {
        "filename": str(resolved.get("filename") or f"paper-{paper_id}.pdf"),
        "bytes": payload,
        # 记清来源，便于排查"这份原文到底哪来的"
        "via": f"reader.{resolved.get('via') or 'source'}",
    }, ""


# --------------------------------------------------------------------------- #
# 执行
# --------------------------------------------------------------------------- #
def _check_cancel(task: TranslateTask) -> None:
    if task.cancel_event.is_set():
        raise engine.TranslationCancelled("任务已取消")


async def _run(task_id: str) -> None:
    """执行入口：占用并发额度 → 执行 → 落 manifest（无论如何都落）。"""
    task = _get_raw(task_id)
    if task is None:
        return
    async with _semaphore():
        try:
            if task.cancel_event.is_set():
                _mark_terminal(task_id, STATUS_CANCELLED, message="任务在开始前被取消")
                return
            await _execute(task)
        except engine.TranslationCancelled:
            _mark_terminal(task_id, STATUS_CANCELLED, message="任务已取消（产物为部分结果，已落盘）")
        except Exception as exc:  # noqa: BLE001 - 任务级异常如实上报
            logger.exception("translate_task_failed task_id=%s", task_id)
            _mark_terminal(
                task_id,
                STATUS_ERROR,
                message=f"翻译失败：{type(exc).__name__}",
                error=f"{type(exc).__name__}: {exc}",
            )
        finally:
            _write_manifest_safe(task_id)


async def _execute(task: TranslateTask) -> None:
    task_id = task.task_id
    art_dir = artifacts.ensure_task_dir(task_id)

    source_bytes = task.source_bytes
    if source_bytes is None:
        source_bytes = (art_dir / artifacts.SOURCE_NAME).read_bytes()
        task.source_bytes = source_bytes
    artifacts.write_atomic(art_dir / artifacts.SOURCE_NAME, source_bytes)
    task.source_sha256 = artifacts.sha256_bytes(source_bytes)

    _set_progress(
        task_id, status=STATUS_PARSING, stage=STATUS_PARSING, percent=4, message="解析 PDF 文本块"
    )

    def _parse_pdf() -> tuple[int, list[engine.TextBlock], list[str]]:
        document = engine.load_document(source_bytes)
        try:
            pages = int(document.page_count)
            extracted, parse_warnings = engine.extract_blocks(
                document, max_pages=artifacts.MAX_PAGES
            )
        finally:
            document.close()
        return pages, extracted, parse_warnings

    page_count, blocks, warnings = await asyncio.to_thread(_parse_pdf)
    task.source_pages = page_count
    task.layout_warnings.extend(warnings)
    _check_cancel(task)

    translator: engine.Translator = await engine.resolve_translator(
        project_id=None, mode=task.mode
    )
    task.translator_name = str(getattr(translator, "name", "unknown"))
    task.stub_used = bool(getattr(translator, "is_stub", False))
    if task.stub_used:
        reason = str(getattr(translator, "reason", "") or "")
        task.layout_warnings.append(f"{engine.STUB_NOTE}（触发原因：{reason}）")

    _set_progress(
        task_id,
        status=STATUS_REWRITING,
        stage=STATUS_REWRITING,
        percent=10,
        message=f"逐块翻译（{len(blocks)} 块，mode={task.mode}）",
    )
    targets: list[str | None] = []
    for index, block in enumerate(blocks):
        _check_cancel(task)
        try:
            targets.append(
                await translator.translate(block.source_text, mode=task.mode, page=block.page)
            )
        except engine.TranslatorUnavailable as exc:
            if isinstance(translator, engine.StubTranslator):
                targets.append(None)
                task.layout_warnings.append(
                    f"page={block.page} 本地桩翻译失败：{type(exc).__name__}: {exc}"
                )
            else:
                # 真实模型不可用 → **只降级一次**，并如实披露（绝不静默改写状态）
                translator = engine.StubTranslator(
                    reason=f"真实模型不可用：{exc}", mode_hint=task.mode
                )
                task.stub_used = True
                task.translator_name = translator.name
                task.layout_warnings.append(
                    f"translate: 真实模型调用失败（{exc}），已如实降级为本地桩"
                    f"（provider={engine.STUB_PROVIDER}）；本次产物不含真实模型译文"
                )
                try:
                    targets.append(
                        await translator.translate(block.source_text, mode=task.mode, page=block.page)
                    )
                except Exception as inner:  # noqa: BLE001
                    targets.append(None)
                    task.layout_warnings.append(
                        f"page={block.page} 降级后仍翻译失败：{type(inner).__name__}: {inner}"
                    )
        except Exception as exc:  # noqa: BLE001 - 单块失败不阻断整篇
            targets.append(None)
            task.layout_warnings.append(
                f"page={block.page} 块翻译失败（保留原文）：{type(exc).__name__}: {exc}"
            )
        _set_progress(
            task_id, percent=10 + int(52 * (index + 1) / max(1, len(blocks)))
        )
    _check_cancel(task)

    _set_progress(
        task_id,
        status=STATUS_RENDERING,
        stage=STATUS_RENDERING,
        percent=66,
        message="原位覆盖回写，生成单语 PDF",
    )
    mono_bytes, outcomes = await asyncio.to_thread(
        engine.render_mono,
        source_bytes,
        blocks,
        targets,
        warnings=task.layout_warnings,
        cancel_check=lambda: task.cancel_event.is_set(),
    )
    await asyncio.to_thread(artifacts.write_atomic, art_dir / artifacts.MONO_NAME, mono_bytes)
    task.formats["mono"] = {"available": True, "bytes": len(mono_bytes)}
    task.outcomes = outcomes

    if task.mode == "translate":
        dual_bytes = await asyncio.to_thread(engine.render_dual, source_bytes, mono_bytes)
        await asyncio.to_thread(artifacts.write_atomic, art_dir / artifacts.DUAL_NAME, dual_bytes)
        task.formats["dual"] = {"available": True, "bytes": len(dual_bytes)}
    else:
        task.formats["dual"] = {
            "available": False,
            "note": "simplify 模式不生成双语 PDF（英文-英文双语无语义）",
        }

    _set_progress(task_id, percent=78, message="生成原文/译文对照预览")
    preview = engine.build_preview(
        task_id=task_id,
        mode=task.mode,
        engine_name=ENGINE_NAME,
        page_count=task.source_pages or 0,
        outcomes=outcomes,
        warnings=task.layout_warnings,
        stub_used=task.stub_used,
        stub_note=engine.STUB_NOTE,
        highlight_summary=task.highlight_summary,
    )
    await asyncio.to_thread(
        artifacts.write_atomic, art_dir / artifacts.PREVIEW_NAME, preview.encode("utf-8")
    )

    if task.highlight_requested:
        _set_progress(
            task_id,
            status=STATUS_HIGHLIGHTING,
            stage=STATUS_HIGHLIGHTING,
            percent=86,
            message="AI 高亮（核心结论/方法创新/关键数据）",
        )
        result = await highlight.run_highlight(
            mono_bytes, outcomes, translator=translator, project_id=None
        )
        task.layout_warnings.extend(result.warnings)
        task.highlight_summary = result.summary()
        if result.pdf_bytes is not None:
            await asyncio.to_thread(
                artifacts.write_atomic, art_dir / artifacts.MONO_NAME, result.pdf_bytes
            )
            task.formats["mono"] = {"available": True, "bytes": len(result.pdf_bytes)}
    else:
        task.highlight_summary = _empty_highlight_summary()

    # 预览里补上最终高亮统计
    preview = engine.build_preview(
        task_id=task_id,
        mode=task.mode,
        engine_name=ENGINE_NAME,
        page_count=task.source_pages or 0,
        outcomes=outcomes,
        warnings=task.layout_warnings,
        stub_used=task.stub_used,
        stub_note=engine.STUB_NOTE,
        highlight_summary=task.highlight_summary,
    )
    await asyncio.to_thread(
        artifacts.write_atomic, art_dir / artifacts.PREVIEW_NAME, preview.encode("utf-8")
    )

    written = sum(1 for item in outcomes if item.written)
    #: "按原样保留"的块（竖排/水印/重叠/无译文）**不进分母** —— 见 engine.PRESERVED_REASONS。
    preserved = sum(
        1
        for item in outcomes
        if not item.written and (item.reason is None or item.reason in engine.PRESERVED_REASONS)
    )
    translatable = len(outcomes) - preserved
    message = f"完成：{written}/{translatable} 块已回写译文"
    if preserved:
        message += f"（另有 {preserved} 块竖排/水印/重叠/无译文，按原样保留、不计入）"
    _set_progress(
        task_id,
        status=STATUS_COMPLETED,
        stage=STATUS_COMPLETED,
        percent=100,
        message=message,
    )


# --------------------------------------------------------------------------- #
# manifest
# --------------------------------------------------------------------------- #
def _manifest_blocks(task: TranslateTask) -> list[dict[str, Any]]:
    blocks: list[dict[str, Any]] = []
    for outcome in task.outcomes[: engine.MAX_BLOCKS_TOTAL]:
        blocks.append(
            {
                "page": int(outcome.page),
                "bbox": [round(float(value), 2) for value in outcome.bbox[:4]],
                "source_text": outcome.source_text,
                "target_text": outcome.target_text,
            }
        )
    return blocks


def _build_manifest(task: TranslateTask) -> dict[str, Any]:
    art_dir = artifacts.task_dir(task.task_id)
    files: dict[str, Any] = {
        "source": artifacts.SOURCE_NAME if (art_dir / artifacts.SOURCE_NAME).is_file() else None,
        "mono": artifacts.MONO_NAME if (art_dir / artifacts.MONO_NAME).is_file() else None,
        "dual": artifacts.DUAL_NAME if (art_dir / artifacts.DUAL_NAME).is_file() else None,
        "preview": artifacts.PREVIEW_NAME
        if (art_dir / artifacts.PREVIEW_NAME).is_file()
        else None,
    }
    shas: dict[str, str] = {}
    for key, name in (("source", artifacts.SOURCE_NAME), ("mono", artifacts.MONO_NAME)):
        if files.get(key):
            digest = artifacts.sha256_file(art_dir / name)
            if digest:
                shas[key] = digest
    if files.get("dual"):
        digest = artifacts.sha256_file(art_dir / artifacts.DUAL_NAME)
        if digest:
            shas["dual"] = digest
    return {
        "schema_version": artifacts.SCHEMA_VERSION,
        "task_id": task.task_id,
        "paper_id": task.paper_id,
        "mode": task.mode,
        "highlight": bool(task.highlight_requested),
        "engine": ENGINE_NAME,
        "status": task.status,
        "created_at": task.created_at,
        "finished_at": task.finished_at,
        "files": files,
        "pages": int(task.source_pages or 0),
        "sha256": shas,
        "blocks": _manifest_blocks(task),
        "highlight_summary": dict(task.highlight_summary),
        "layout_warnings": list(task.layout_warnings),
    }


def _write_manifest_safe(task_id: str) -> None:
    task = _get_raw(task_id)
    if task is None:
        return
    try:
        artifacts.write_manifest(task_id, _build_manifest(task))
    except Exception as exc:  # noqa: BLE001 - 落盘失败不得掩盖任务本身的结果
        logger.warning("translate_manifest_write_failed task_id=%s err=%s", task_id, exc)


def _detail_from_manifest(manifest: Mapping[str, Any]) -> dict[str, Any]:
    """从 manifest 恢复只读任务详情（重启后）。"""
    files = manifest.get("files") if isinstance(manifest.get("files"), Mapping) else {}
    sha = manifest.get("sha256") if isinstance(manifest.get("sha256"), Mapping) else {}
    status = str(manifest.get("status") or STATUS_COMPLETED)
    mono_name = files.get("mono") if isinstance(files, Mapping) else None
    dual_name = files.get("dual") if isinstance(files, Mapping) else None
    mono_bytes: int | None = None
    mono_path = artifacts.artifact_path(str(manifest.get("task_id") or ""), artifacts.MONO_NAME)
    try:
        if mono_name and mono_path.is_file():
            mono_bytes = int(mono_path.stat().st_size)
    except (OSError, artifacts.UnsafeTaskId):
        mono_bytes = None
    return {
        "task_id": manifest.get("task_id"),
        "status": status,
        "percent": 100 if status == STATUS_COMPLETED else 0,
        "stage": status,
        "message": "从磁盘 manifest 恢复（进程内存状态已丢失）",
        "error": None,
        "mode": manifest.get("mode"),
        "highlight": bool(manifest.get("highlight")),
        "paper_id": manifest.get("paper_id"),
        "source": {
            "filename": files.get("source") if isinstance(files, Mapping) else None,
            "pages": manifest.get("pages"),
            "sha256": sha.get("source") if isinstance(sha, Mapping) else None,
        },
        "formats": {
            "mono": {"available": bool(mono_name), "bytes": mono_bytes} if mono_name else {"available": False},
            "dual": {"available": bool(dual_name)} if dual_name else {"available": False},
        },
        "highlight_summary": dict(manifest.get("highlight_summary") or _empty_highlight_summary()),
        "layout_warnings": list(manifest.get("layout_warnings") or []),
        "created_at": manifest.get("created_at"),
        "finished_at": manifest.get("finished_at"),
        "engine": manifest.get("engine") or ENGINE_NAME,
        "translator": None,
        "note": RESTART_NOTE,
    }


__all__ = [
    "ALL_STATUSES",
    "RETRYABLE_STATUSES",
    "STATUS_CANCELLED",
    "STATUS_COMPLETED",
    "STATUS_ERROR",
    "STATUS_HIGHLIGHTING",
    "STATUS_PARSING",
    "STATUS_PENDING",
    "STATUS_RENDERING",
    "STATUS_REWRITING",
    "TERMINAL_STATUSES",
    "VALID_MODES",
    "TranslateTask",
    "cancel",
    "get_task",
    "list_tasks",
    "max_concurrency",
    "new_task",
    "resolve_paper_source",
    "retry",
    "submit",
]
