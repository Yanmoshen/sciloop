# Copyright 2026 SciLoop contributors
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
"""论文翻译端点（EasyPaper 四核心模块之 ②，自研 ``pymupdf-block-v1`` 引擎）。

============================  ==========================================================
``POST /translate/jobs``                 multipart 上传 PDF（≤20MB / ≤100 页），202
``POST /translate/jobs/from-paper``      复用已入库论文的本地原文，202
``GET  /translate/jobs``                 任务列表（内存优先，空时回落到磁盘 manifest）
``GET  /translate/jobs/{id}``            任务详情（轮询口）
``GET  /translate/jobs/{id}/pdf``        ``?format=mono|dual`` → application/pdf（Attachment）
``GET  /translate/jobs/{id}/preview``    text/html（原文/译文逐块对照）
``POST /translate/jobs/{id}/cancel``     Owner；非终态 → 200 cancelled
``POST /translate/jobs/{id}/retry``      Owner；error/cancelled → 202
============================  ==========================================================

口径（硬约束）
--------------

- **写接口全部 Owner 面**：``require_owner``，匿名一律 ``403``（``{code,message,detail}``）。
  为不让「请求体先于鉴权被解析」破坏这条，本模块**不用 FastAPI 的 ``File`` / ``Form``**，
  而是先过 owner 依赖、再在函数内用标准库 ``email`` 手工解析 multipart
  —— 这也是**不引入 python-multipart 新依赖**的原因（依赖清单由其它工作包所有）。
- **长任务语义**：两个 POST 立即返回 ``202 + task_id``；进度只能轮询 ``poll_url``。
  任务状态只存内存，**重启后内存中查不到**，但磁盘产物与 manifest 保留，
  ``GET /translate/jobs/{id}`` 会回落到 manifest 恢复只读快照（见 ``note``）。
- **HTTP 状态码**：请求本身不合法（非 PDF / 空文件 / >20MB / >100 页 / mode 非法）→ ``422``；
  未知 task_id → ``404``；终态任务再次 cancel/retry → ``409``；结果缺失 → ``404 result_not_found``。
"""

from __future__ import annotations

import email
import json
import logging
from collections.abc import AsyncIterator, Mapping
from email import policy
from typing import Annotated, Any

from fastapi import APIRouter, Depends, HTTPException, Query, Request, status
from fastapi.responses import Response
from sqlalchemy.ext.asyncio import AsyncSession

from core.security import require_owner
from db.session import AsyncSessionLocal
from services.ingest import storage
from services.translate import ENGINE_NAME, RESTART_NOTE, artifacts, engine, jobs

logger = logging.getLogger("sciloop.translate.api")

router = APIRouter(tags=["translate"])

#: 单次上传的字段名（契约）
FILE_FIELD = "file"
MODE_FIELD = "mode"
HIGHLIGHT_FIELD = "highlight"
PAPER_ID_FIELD = "paper_id"

#: 双语 PDF 只在 translate 模式产出（simplify 的英文-英文双语无语义）
MONO_FORMAT = "mono"
DUAL_FORMAT = "dual"
VALID_FORMATS: tuple[str, ...] = (MONO_FORMAT, DUAL_FORMAT)


# --------------------------------------------------------------------------------------
# 会话、错误体、请求体解析
# --------------------------------------------------------------------------------------
async def _session() -> AsyncIterator[AsyncSession]:
    if AsyncSessionLocal is None:  # pragma: no cover - 部署期驱动缺失
        raise _error(
            status.HTTP_503_SERVICE_UNAVAILABLE,
            "database_unavailable",
            "异步数据库会话工厂不可用（DATABASE_URL / asyncpg 未就绪）",
        )
    async with AsyncSessionLocal() as session:
        yield session


DbSession = Annotated[AsyncSession, Depends(_session)]


def _error(status_code: int, code: str, message: str, detail: Any = None) -> HTTPException:
    return HTTPException(
        status_code=status_code, detail={"code": code, "message": message, "detail": detail}
    )


def _parse_multipart(content_type: str, body: bytes) -> tuple[list[dict[str, Any]], dict[str, str]]:
    """用标准库解析 ``multipart/form-data``，返回 ``(文件部件, 文本字段)``。"""
    lowered = content_type.lower()
    if "multipart/form-data" not in lowered:
        raise _error(
            status.HTTP_422_UNPROCESSABLE_ENTITY,
            "invalid_content_type",
            "POST /translate/jobs 需要 multipart/form-data",
            {"content_type": content_type or None},
        )
    if "boundary=" not in lowered:
        raise _error(
            status.HTTP_422_UNPROCESSABLE_ENTITY,
            "invalid_multipart",
            "multipart/form-data 缺少 boundary",
        )
    raw = f"Content-Type: {content_type}\r\nMIME-Version: 1.0\r\n\r\n".encode() + body
    try:
        message = email.message_from_bytes(raw, policy=policy.default)
        parts = list(message.iter_parts()) if message.is_multipart() else []
    except Exception as exc:  # noqa: BLE001 - 解析失败一律 422，不 500
        raise _error(
            status.HTTP_422_UNPROCESSABLE_ENTITY,
            "invalid_multipart",
            f"multipart 请求体无法解析：{type(exc).__name__}",
        ) from exc
    if not parts:
        raise _error(
            status.HTTP_422_UNPROCESSABLE_ENTITY,
            "invalid_multipart",
            "multipart 请求体无法解析（boundary 与内容不匹配）",
        )

    files: list[dict[str, Any]] = []
    fields: dict[str, str] = {}
    for part in parts:
        disposition = str(part.get("Content-Disposition") or "")
        if "form-data" not in disposition.lower():
            continue
        name = str(part.get_param("name", header="content-disposition") or "")
        filename = part.get_filename()
        payload = part.get_payload(decode=True) or b""
        if filename is None:
            fields[name.lower()] = payload.decode("utf-8", "replace").strip()
            continue
        files.append(
            {
                "field": name,
                "filename": filename,
                "content_type": part.get_content_type(),
                "content": payload,
            }
        )
    return files, fields


async def _read_multipart(request: Request) -> tuple[list[dict[str, Any]], dict[str, str]]:
    content_type = request.headers.get("content-type") or ""
    declared = request.headers.get("content-length")
    if declared and declared.isdigit() and int(declared) > artifacts.MAX_REQUEST_BYTES:
        raise _error(
            status.HTTP_413_REQUEST_ENTITY_TOO_LARGE,
            "request_too_large",
            f"请求体超过上限 {artifacts.MAX_REQUEST_BYTES} 字节",
            {"content_length": int(declared)},
        )
    body = await request.body()
    if len(body) > artifacts.MAX_REQUEST_BYTES:
        raise _error(
            status.HTTP_413_REQUEST_ENTITY_TOO_LARGE,
            "request_too_large",
            f"请求体超过上限 {artifacts.MAX_REQUEST_BYTES} 字节",
            {"size_bytes": len(body)},
        )
    return _parse_multipart(content_type, body)


async def _read_json_object(request: Request) -> dict[str, Any]:
    content_type = request.headers.get("content-type") or ""
    if content_type and "json" not in content_type.lower():
        raise _error(
            status.HTTP_422_UNPROCESSABLE_ENTITY,
            "invalid_content_type",
            "该端点需要 application/json",
            {"content_type": content_type},
        )
    raw = await request.body()
    if not raw:
        raise _error(
            status.HTTP_422_UNPROCESSABLE_ENTITY, "empty_body", "请求体为空，需要 JSON 对象"
        )
    try:
        payload = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise _error(
            status.HTTP_422_UNPROCESSABLE_ENTITY,
            "invalid_json",
            "请求体不是合法 JSON",
            {"error": str(exc)},
        ) from exc
    if not isinstance(payload, dict):
        raise _error(
            status.HTTP_422_UNPROCESSABLE_ENTITY, "invalid_body", "请求体必须是 JSON 对象"
        )
    return payload


# --------------------------------------------------------------------------------------
# 参数校验
# --------------------------------------------------------------------------------------
def _validated_mode(raw: Any) -> str:
    text = str(raw if raw is not None and raw != "" else "translate").strip().lower()
    if text not in jobs.VALID_MODES:
        raise _error(
            status.HTTP_422_UNPROCESSABLE_ENTITY,
            "invalid_mode",
            f"mode 只能是 {' | '.join(jobs.VALID_MODES)}，收到 {raw!r}",
            {"allowed": list(jobs.VALID_MODES)},
        )
    return text


def _validated_bool(raw: Any, *, default: bool) -> bool:
    if raw is None or raw == "":
        return default
    if isinstance(raw, bool):
        return raw
    text = str(raw).strip().lower()
    if text in {"1", "true", "yes", "on"}:
        return True
    if text in {"0", "false", "no", "off"}:
        return False
    raise _error(
        status.HTTP_422_UNPROCESSABLE_ENTITY,
        "invalid_highlight",
        f"highlight 只能是 true | false，收到 {raw!r}",
    )


def _optional_paper_id(raw: Any) -> int | None:
    if raw is None or raw == "":
        return None
    if isinstance(raw, bool):
        raise _error(
            status.HTTP_422_UNPROCESSABLE_ENTITY, "invalid_paper_id", "paper_id 必须是整数"
        )
    try:
        return int(str(raw).strip())
    except (TypeError, ValueError) as exc:
        raise _error(
            status.HTTP_422_UNPROCESSABLE_ENTITY,
            "invalid_paper_id",
            "paper_id 必须是整数（可选，仅作关联留痕）",
            {"paper_id": raw},
        ) from exc


def _required_paper_id(payload: Mapping[str, Any]) -> int:
    if PAPER_ID_FIELD not in payload:
        raise _error(
            status.HTTP_422_UNPROCESSABLE_ENTITY, "missing_paper_id", "缺少必填字段 paper_id"
        )
    value = _optional_paper_id(payload.get(PAPER_ID_FIELD))
    if value is None:
        raise _error(
            status.HTTP_422_UNPROCESSABLE_ENTITY, "invalid_paper_id", "paper_id 不能为空"
        )
    return value


def _inspect_pdf(content: bytes) -> int:
    """校验 PDF 可打开并返回页数（不可打开 → 422 ``invalid_pdf``）。"""
    try:
        document = engine.load_document(content)
    except Exception as exc:  # noqa: BLE001 - 打不开即用户输入问题
        raise _error(
            status.HTTP_422_UNPROCESSABLE_ENTITY,
            "invalid_pdf",
            f"PDF 无法打开：{type(exc).__name__}",
        ) from exc
    try:
        pages = int(document.page_count)
    finally:
        document.close()
    if pages <= 0:
        raise _error(status.HTTP_422_UNPROCESSABLE_ENTITY, "empty_pdf", "PDF 页数为 0")
    if pages > artifacts.MAX_PAGES:
        raise _error(
            status.HTTP_422_UNPROCESSABLE_ENTITY,
            "too_many_pages",
            f"PDF 共 {pages} 页，超过上限 {artifacts.MAX_PAGES} 页",
            {"pages": pages, "max_pages": artifacts.MAX_PAGES},
        )
    return pages


def _urlsafe_task_id(task_id: str) -> str:
    try:
        return artifacts.safe_task_id(task_id)
    except artifacts.UnsafeTaskId as exc:
        raise _error(
            status.HTTP_404_NOT_FOUND,
            "task_not_found",
            f"未找到翻译任务 {task_id}",
            {"task_id": task_id},
        ) from exc


# --------------------------------------------------------------------------------------
# POST /translate/jobs（上传 PDF）
# --------------------------------------------------------------------------------------
@router.post(
    "/translate/jobs",
    summary="上传 PDF 创建翻译任务（长任务，owner 面）",
    dependencies=[Depends(require_owner)],
    status_code=status.HTTP_202_ACCEPTED,
)
async def create_translate_job(request: Request) -> dict[str, Any]:
    """``multipart/form-data``：``file``（必填）、``mode``、``highlight``、``paper_id``。

    校验顺序：multipart 形态 → 文件存在 → 类型（仅 PDF）→ 空文件 → 大小（≤20MB）→
    mode → highlight → 页数（≤100）。任一步失败 → ``422``（``{code,message,detail}``）。
    """
    files, fields = await _read_multipart(request)
    if not files:
        raise _error(
            status.HTTP_422_UNPROCESSABLE_ENTITY,
            "missing_file",
            f"multipart 中未找到名为 {FILE_FIELD} 的 PDF 文件字段",
        )
    upload = next((item for item in files if item["field"] == FILE_FIELD), files[0])
    filename = upload.get("filename") or ""
    content_type = str(upload.get("content_type") or "")
    if not storage.has_pdf_hint(filename, content_type):
        raise _error(
            status.HTTP_422_UNPROCESSABLE_ENTITY,
            "unsupported_type_not_pdf",
            "仅接受 application/pdf 或 .pdf 文件",
            {"filename": filename, "content_type": content_type},
        )
    content = upload.get("content") or b""
    if not content:
        raise _error(
            status.HTTP_422_UNPROCESSABLE_ENTITY, "empty_file", "上传文件为空", {"filename": filename}
        )
    if len(content) > artifacts.MAX_FILE_BYTES:
        raise _error(
            status.HTTP_422_UNPROCESSABLE_ENTITY,
            "file_too_large_over_20mb",
            f"文件 {len(content)} 字节，超过上限 {artifacts.MAX_FILE_BYTES} 字节（20MB）",
            {"size_bytes": len(content), "max_bytes": artifacts.MAX_FILE_BYTES},
        )

    mode = _validated_mode(fields.get(MODE_FIELD))
    highlight_requested = _validated_bool(fields.get(HIGHLIGHT_FIELD), default=False)
    paper_id = _optional_paper_id(fields.get(PAPER_ID_FIELD))
    pages = _inspect_pdf(content)

    submitted = jobs.submit(
        source_bytes=content,
        filename=storage.sanitize_filename(filename),
        mode=mode,
        highlight_requested=highlight_requested,
        paper_id=paper_id,
    )
    logger.info(
        "translate_submitted task_id=%s mode=%s highlight=%s pages=%d bytes=%d",
        submitted["task_id"],
        mode,
        highlight_requested,
        pages,
        len(content),
    )
    return {**submitted, "pages": pages, "size_bytes": len(content)}


# --------------------------------------------------------------------------------------
# POST /translate/jobs/from-paper（复用已入库论文的原文）
# --------------------------------------------------------------------------------------
@router.post(
    "/translate/jobs/from-paper",
    summary="用已入库论文的原文创建翻译任务（长任务，owner 面）",
    dependencies=[Depends(require_owner)],
    status_code=status.HTTP_202_ACCEPTED,
)
async def create_translate_job_from_paper(request: Request, session: DbSession) -> dict[str, Any]:
    """``{paper_id, mode?, highlight?}`` → 202；无可用本地原文 → ``409 no_source_document``。

    只复用**已落盘**的上传原文（不联网下载）；库里只有 HTML/摘要级记录时如实返回 409。
    """
    payload = await _read_json_object(request)
    paper_id = _required_paper_id(payload)
    mode = _validated_mode(payload.get("mode"))
    highlight_requested = _validated_bool(payload.get("highlight"), default=False)

    source, code = await jobs.resolve_paper_source(session, paper_id)
    if source is None:
        if code == "paper_not_found":
            raise _error(
                status.HTTP_404_NOT_FOUND, "paper_not_found", f"库内不存在 paper_id={paper_id}"
            )
        if code == "source_too_large":
            raise _error(
                status.HTTP_422_UNPROCESSABLE_ENTITY,
                "source_too_large",
                "库内原文超过 20MB 上限，无法创建翻译任务",
                {"paper_id": paper_id},
            )
        raise _error(
            status.HTTP_409_CONFLICT,
            "no_source_document",
            f"paper_id={paper_id} 没有可用的本地原文 PDF（仅摘要/HTML 记录，或原文文件已不存在）",
            {"paper_id": paper_id},
        )

    content = bytes(source["bytes"])
    try:
        pages = _inspect_pdf(content)
    except HTTPException as exc:
        detail = exc.detail if isinstance(exc.detail, dict) else {}
        raise _error(
            status.HTTP_409_CONFLICT,
            "source_document_unusable",
            f"库内原文无法用于翻译：{detail.get('message') or exc.status_code}",
            {"paper_id": paper_id, "via": source.get("via")},
        ) from exc

    submitted = jobs.submit(
        source_bytes=content,
        filename=storage.sanitize_filename(str(source.get("filename") or f"paper-{paper_id}.pdf")),
        mode=mode,
        highlight_requested=highlight_requested,
        paper_id=paper_id,
    )
    logger.info(
        "translate_from_paper_submitted task_id=%s paper_id=%s mode=%s pages=%d",
        submitted["task_id"],
        paper_id,
        mode,
        pages,
    )
    return {**submitted, "paper_id": paper_id, "pages": pages, "source_via": source.get("via")}


# --------------------------------------------------------------------------------------
# GET 列表 / 详情
# --------------------------------------------------------------------------------------
async def _paper_titles(session: AsyncSession, paper_ids: set[int]) -> dict[int, str]:
    """批量取论文标题（一次查询，避免 N+1）。取不到就返回空，由调用方退回其它标识。"""

    if not paper_ids:
        return {}
    from sqlalchemy import select

    from db.models.paper import Paper

    rows = await session.execute(
        select(Paper.id, Paper.title).where(Paper.id.in_(sorted(paper_ids)))
    )
    return {
        int(row[0]): str(row[1] or "").strip() for row in rows.all() if str(row[1] or "").strip()
    }


@router.get("/translate/jobs", summary="翻译任务列表")
async def list_translate_jobs(session: DbSession) -> dict[str, Any]:
    items = jobs.list_tasks()
    # 「任务」那一列要显示**论文标题**（用户口径 2026-09-26：不要显示任务编号）。
    # 界面据此渲染首列；查不到标题时前端会退回文件名，仍然不编。
    titles = await _paper_titles(
        session, {int(item["paper_id"]) for item in items if item.get("paper_id")}
    )
    for item in items:
        paper_id = item.get("paper_id")
        item["paper_title"] = titles.get(int(paper_id)) if paper_id else None
    return {
        "items": items,
        "total": len(items),
        "engine": ENGINE_NAME,
        "note": RESTART_NOTE,
        "source": "memory" if items else "artifacts",
    }


@router.get("/translate/jobs/{task_id}", summary="翻译任务详情（轮询口）")
async def get_translate_job(task_id: str) -> dict[str, Any]:
    safe_id = _urlsafe_task_id(task_id)
    detail = jobs.get_task(safe_id)
    if detail is None:
        raise _error(
            status.HTTP_404_NOT_FOUND,
            "task_not_found",
            f"未找到翻译任务 {task_id}（任务状态只存内存，服务重启后按磁盘 manifest 恢复）",
            {"task_id": task_id},
        )
    return detail


# --------------------------------------------------------------------------------------
# GET 结果（mono / dual PDF）
# --------------------------------------------------------------------------------------
@router.get("/translate/jobs/{task_id}/pdf", summary="下载单语/双语翻译 PDF")
async def download_translate_pdf(
    task_id: str,
    format: str = Query(MONO_FORMAT, description="mono=单语译文，dual=双语对照（仅 translate 模式）"),
) -> Response:
    safe_id = _urlsafe_task_id(task_id)
    if format not in VALID_FORMATS:
        raise _error(
            status.HTTP_422_UNPROCESSABLE_ENTITY,
            "invalid_format",
            f"format 只能是 {' | '.join(VALID_FORMATS)}，收到 {format!r}",
            {"allowed": list(VALID_FORMATS)},
        )
    detail = jobs.get_task(safe_id)
    if detail is None:
        raise _error(
            status.HTTP_404_NOT_FOUND,
            "task_not_found",
            f"未找到翻译任务 {task_id}",
            {"task_id": task_id},
        )
    available = bool((detail.get("formats") or {}).get(format, {}).get("available"))
    if not available:
        if detail.get("status") not in jobs.TERMINAL_STATUSES:
            raise _error(
                status.HTTP_409_CONFLICT,
                "task_not_completed",
                f"任务当前状态为 {detail.get('status')}，结果尚未生成",
                {"task_id": task_id, "status": detail.get("status")},
            )
        reason = (
            "simplify 模式不产出双语 PDF（英文-英文双语无语义）"
            if format == DUAL_FORMAT
            else "该格式结果不存在"
        )
        raise _error(
            status.HTTP_404_NOT_FOUND,
            "result_not_found",
            f"任务 {task_id} 没有 {format} 结果：{reason}",
            {"task_id": task_id, "format": format, "mode": detail.get("mode")},
        )
    name = artifacts.MONO_NAME if format == MONO_FORMAT else artifacts.DUAL_NAME
    path = artifacts.artifact_path(safe_id, name)
    if not path.is_file():
        raise _error(
            status.HTTP_404_NOT_FOUND,
            "result_not_found",
            f"任务 {task_id} 的 {format} 产物文件不存在",
            {"task_id": task_id, "format": format},
        )
    payload = path.read_bytes()
    return Response(
        content=payload,
        media_type="application/pdf",
        headers={
            "Content-Disposition": f'attachment; filename="{safe_id}-{format}.pdf"',
            "Content-Length": str(len(payload)),
            "X-SciLoop-Engine": ENGINE_NAME,
        },
    )


@router.get("/translate/jobs/{task_id}/preview", summary="原文/译文逐块对照预览")
async def preview_translate(task_id: str) -> Response:
    safe_id = _urlsafe_task_id(task_id)
    detail = jobs.get_task(safe_id)
    if detail is None:
        raise _error(
            status.HTTP_404_NOT_FOUND,
            "task_not_found",
            f"未找到翻译任务 {task_id}",
            {"task_id": task_id},
        )
    path = artifacts.artifact_path(safe_id, artifacts.PREVIEW_NAME)
    if not path.is_file():
        if detail.get("status") not in jobs.TERMINAL_STATUSES:
            raise _error(
                status.HTTP_409_CONFLICT,
                "task_not_completed",
                f"任务当前状态为 {detail.get('status')}，预览尚未生成",
                {"task_id": task_id, "status": detail.get("status")},
            )
        raise _error(
            status.HTTP_404_NOT_FOUND,
            "result_not_found",
            f"任务 {task_id} 没有预览文件",
            {"task_id": task_id},
        )
    return Response(
        content=path.read_bytes(),
        media_type="text/html; charset=utf-8",
        headers={"Cache-Control": "no-store"},
    )


# --------------------------------------------------------------------------------------
# POST 取消 / 重试
# --------------------------------------------------------------------------------------
@router.post(
    "/translate/jobs/{task_id}/cancel",
    summary="取消翻译任务（owner 面）",
    dependencies=[Depends(require_owner)],
)
async def cancel_translate_job(task_id: str) -> dict[str, Any]:
    safe_id = _urlsafe_task_id(task_id)
    ok, code = jobs.cancel(safe_id)
    if not ok:
        if code == "task_not_found":
            raise _error(
                status.HTTP_404_NOT_FOUND,
                "task_not_found",
                f"未找到翻译任务 {task_id}",
                {"task_id": task_id},
            )
        raise _error(
            status.HTTP_409_CONFLICT,
            "task_already_finished",
            f"任务 {task_id} 已处于终态，无法取消",
            {"task_id": task_id},
        )
    return {
        "task_id": safe_id,
        "status": jobs.STATUS_CANCELLED,
        "message": "已取消；已生成的产物（若有）保留在 artifacts 目录",
        "note": RESTART_NOTE,
    }


@router.post(
    "/translate/jobs/{task_id}/retry",
    summary="重跑失败的翻译任务（owner 面）",
    dependencies=[Depends(require_owner)],
    status_code=status.HTTP_202_ACCEPTED,
)
async def retry_translate_job(task_id: str) -> dict[str, Any]:
    safe_id = _urlsafe_task_id(task_id)
    ok, code = jobs.retry(safe_id)
    if not ok:
        if code == "task_not_found":
            raise _error(
                status.HTTP_404_NOT_FOUND,
                "task_not_found",
                f"未找到翻译任务 {task_id}（任务状态只存内存）",
                {"task_id": task_id},
            )
        raise _error(
            status.HTTP_409_CONFLICT,
            "task_already_finished",
            f"任务 {task_id} 当前状态不可重跑（仅 error / cancelled 允许重试）",
            {"task_id": task_id},
        )
    detail = jobs.get_task(safe_id) or {}
    return {
        "task_id": safe_id,
        "status": jobs.STATUS_PENDING,
        "mode": detail.get("mode"),
        "highlight": detail.get("highlight"),
        "engine": ENGINE_NAME,
        "poll_url": f"/api/v1/translate/jobs/{safe_id}",
        "note": RESTART_NOTE,
    }


__all__ = ["router"]
