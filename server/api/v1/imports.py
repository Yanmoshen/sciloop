# Copyright 2026 SciLoop contributors
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
"""论文导入端点（PDF 上传 + DOI/arXiv ID 批量导入）。

============================  ==========================================================
``POST /papers/import``               multipart/form-data 上传 PDF（≤20MB × ≤20 个），202
``POST /papers/import/identifiers``   JSON 批量导入 DOI / arXiv ID，202
``GET  /papers/import-jobs/{id}``     轮询导入任务（running / done / failed）
``GET  /papers/imports``              导入历史（分页，见下）
============================  ==========================================================

口径（硬约束）
--------------

- **写接口全部 Owner 面**：``require_owner``，匿名一律 ``403``（错误体 ``{code,message,detail}``）。
  为了不让「请求体先于鉴权被解析」破坏这条，本模块**不用 FastAPI 的
  ``File`` / ``Form`` / 请求体模型**，而是先过 owner 依赖、再在函数内手工解析
  multipart / JSON —— 这也是**不引入 python-multipart 新依赖**的原因
  （容器与 pyproject 依赖清单由其它工作包所有，本包不得改动）。
- **长任务语义**：两个 POST 立即返回 ``202 + task_id``，执行在后台线程；
  进度只能轮询 ``poll_url``。任务状态只存内存，**重启后 task_id 查不到（404）**，
  已落库记录与已落盘文件不受影响。
- **禁止编造**：解析/取数失败一律如实记录原因；批量导入中单条失败不影响整体状态。
- **HTTP 状态码口径**：请求本身不合法（不是 multipart、缺 ``files`` 字段、
  ``identifiers`` 非法、单次 >20 个文件）→ ``422``；请求合法但**全部条目都不可用**
  → 同样 ``422``（``detail.rejected`` 给出逐条原因，便于前端直接展示）；
  部分可用 → ``202``，不可用条目进 ``rejected``。
- ``GET /papers/imports``：库里**没有**独立的导入历史表（本包不得新增迁移），
  因此按 ``papers.raw['upload']`` 聚合——它由每次上传导入真实写入，
  绝不编造。``status`` 是「该论文最近一次上传导入的落库结果」。
"""

from __future__ import annotations

import email
import json
import logging
from collections.abc import AsyncIterator, Mapping
from email import policy
from typing import Annotated, Any

from fastapi import APIRouter, Depends, HTTPException, Query, Request, status
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from core.security import require_owner
from db.models.paper import Paper
from db.session import AsyncSessionLocal
from services.ingest import RESTART_NOTE, jobs, storage

logger = logging.getLogger("sciloop.ingest.imports")

router = APIRouter(tags=["imports"])

MAX_PAGE_SIZE = 200
DEFAULT_PAGE_SIZE = 20
#: 单次标识符条数上限（contracts.subagent_sla.batch_size = 50）
MAX_IDENTIFIERS = 50
#: 单条标识符字符串长度上限
MAX_IDENTIFIER_CHARS = 512
#: 标识符批量导入模块（由并行工作包实现；此处只登记名字，缺失时如实报 503）
_IDENTIFIER_MODULE = "services.ingest.identifier_import"

#: 逐条拒绝原因（给人读的 code）
REASON_UNSUPPORTED_TYPE = "unsupported_type_not_pdf"
REASON_EMPTY_FILE = "empty_file"
REASON_TOO_LARGE = "file_too_large_over_20mb"
REASON_MISSING_FILENAME = "missing_filename"
REASON_UNSAFE_NAME = "unsafe_filename"


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


def _parse_multipart_files(content_type: str, body: bytes) -> list[dict[str, Any]]:
    """用标准库解析 ``multipart/form-data``（不依赖 python-multipart）。

    只抽取文件部件（``files`` 字段或带 filename 的 form-data 部分）；
    返回顺序 = 表单顺序。
    """
    if "multipart/form-data" not in content_type.lower():
        raise _error(
            status.HTTP_422_UNPROCESSABLE_ENTITY,
            "invalid_content_type",
            "POST /papers/import 需要 multipart/form-data",
            {"content_type": content_type or None},
        )
    if "boundary=" not in content_type.lower():
        raise _error(
            status.HTTP_422_UNPROCESSABLE_ENTITY,
            "invalid_multipart",
            "multipart/form-data 缺少 boundary",
        )
    raw = f"Content-Type: {content_type}\r\nMIME-Version: 1.0\r\n\r\n".encode() + body
    try:
        message = email.message_from_bytes(raw, policy=policy.default)
        is_multipart = message.is_multipart()
        parts = list(message.iter_parts()) if is_multipart else []
    except Exception as exc:  # noqa: BLE001 - 解析失败一律 422，不 500
        raise _error(
            status.HTTP_422_UNPROCESSABLE_ENTITY,
            "invalid_multipart",
            f"multipart 请求体无法解析：{type(exc).__name__}",
        ) from exc
    if not is_multipart or not parts:
        raise _error(
            status.HTTP_422_UNPROCESSABLE_ENTITY,
            "invalid_multipart",
            "multipart 请求体无法解析（boundary 与内容不匹配）",
        )
    uploads: list[dict[str, Any]] = []
    for part in parts:
        disposition = str(part.get("Content-Disposition") or "")
        if "form-data" not in disposition.lower():
            continue
        field_name = part.get_param("name", header="content-disposition")
        filename = part.get_filename()
        if filename is None and field_name != "files":
            continue
        uploads.append(
            {
                "field": field_name,
                "filename": filename,
                "content_type": part.get_content_type(),
                "content": part.get_payload(decode=True) or b"",
            }
        )
    return uploads


async def _read_multipart(request: Request) -> list[dict[str, Any]]:
    """读取并解析 multipart 请求体；先按 ``Content-Length`` 做总量闸门。"""
    content_type = request.headers.get("content-type") or ""
    declared = request.headers.get("content-length")
    if declared and declared.isdigit() and int(declared) > storage.MAX_REQUEST_BYTES:
        raise _error(
            status.HTTP_413_REQUEST_ENTITY_TOO_LARGE,
            "request_too_large",
            f"请求体超过上限 {storage.MAX_REQUEST_BYTES} 字节"
            f"（{storage.MAX_FILES_PER_REQUEST} × {storage.MAX_FILE_BYTES} 字节）",
            {"content_length": int(declared)},
        )
    body = await request.body()
    if len(body) > storage.MAX_REQUEST_BYTES:
        raise _error(
            status.HTTP_413_REQUEST_ENTITY_TOO_LARGE,
            "request_too_large",
            f"请求体超过上限 {storage.MAX_REQUEST_BYTES} 字节",
            {"size_bytes": len(body)},
        )
    return _parse_multipart_files(content_type, body)


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


def _optional_project_id(payload: Mapping[str, Any]) -> int | None:
    value = payload.get("project_id")
    if value is None or value == "":
        return None
    if isinstance(value, bool) or not isinstance(value, int):
        try:
            return int(str(value))
        except (TypeError, ValueError) as exc:
            raise _error(
                status.HTTP_422_UNPROCESSABLE_ENTITY,
                "invalid_project_id",
                "project_id 必须是整数（可选，仅作留痕）",
                {"project_id": value},
            ) from exc
    return int(value)


def _validated_identifiers(payload: Mapping[str, Any]) -> list[str]:
    raw = payload.get("identifiers")
    if raw is None:
        raise _error(
            status.HTTP_422_UNPROCESSABLE_ENTITY,
            "missing_identifiers",
            "缺少必填字段 identifiers（字符串数组）",
        )
    if not isinstance(raw, list) or isinstance(raw, (str, bytes)):
        raise _error(
            status.HTTP_422_UNPROCESSABLE_ENTITY,
            "invalid_identifiers",
            "identifiers 必须是字符串数组",
            {"type": type(raw).__name__},
        )
    values = [str(item).strip() for item in raw]
    if not values:
        raise _error(
            status.HTTP_422_UNPROCESSABLE_ENTITY, "empty_identifiers", "identifiers 不能为空数组"
        )
    if len(values) > MAX_IDENTIFIERS:
        raise _error(
            status.HTTP_422_UNPROCESSABLE_ENTITY,
            "too_many_identifiers",
            f"单次最多导入 {MAX_IDENTIFIERS} 条标识符，收到 {len(values)} 条",
            {"max": MAX_IDENTIFIERS, "received": len(values)},
        )
    too_long = [item for item in values if len(item) > MAX_IDENTIFIER_CHARS]
    if too_long:
        raise _error(
            status.HTTP_422_UNPROCESSABLE_ENTITY,
            "identifier_too_long",
            f"单条标识符不能超过 {MAX_IDENTIFIER_CHARS} 字符",
            {"samples": [item[:60] for item in too_long[:3]]},
        )
    return values


# --------------------------------------------------------------------------------------
# POST /papers/import
# --------------------------------------------------------------------------------------
@router.post(
    "/papers/import",
    summary="上传 PDF 批量导入（长任务，owner 面）",
    dependencies=[Depends(require_owner)],
    status_code=status.HTTP_202_ACCEPTED,
)
async def import_papers(request: Request) -> dict[str, Any]:
    """接收 multipart 上传的 PDF，落盘后交后台解析入库，立即返回 ``task_id``。

    校验顺序：文件个数（>20 → 422）→ 逐个校验（非 PDF / 空文件 / >20MB → ``rejected``）；
    **全部被拒**时返回 422 并在 ``detail.rejected`` 给出逐条原因；部分可用则 202。
    """
    uploads = await _read_multipart(request)
    if not uploads:
        raise _error(
            status.HTTP_422_UNPROCESSABLE_ENTITY,
            "missing_files",
            "multipart 中未找到名为 files 的文件字段",
        )
    if len(uploads) > storage.MAX_FILES_PER_REQUEST:
        raise _error(
            status.HTTP_422_UNPROCESSABLE_ENTITY,
            "too_many_files",
            f"单次最多上传 {storage.MAX_FILES_PER_REQUEST} 个文件，收到 {len(uploads)} 个",
            {"max": storage.MAX_FILES_PER_REQUEST, "received": len(uploads)},
        )

    root = storage.default_upload_dir()
    accepted: list[dict[str, Any]] = []
    rejected: list[dict[str, str]] = []
    for item in uploads:
        raw_name = item.get("filename") or ""
        if not str(raw_name).strip():
            rejected.append({"name": "", "reason": REASON_MISSING_FILENAME})
            continue
        name = storage.sanitize_filename(raw_name)
        if not storage.has_pdf_hint(raw_name, item.get("content_type")):
            rejected.append({"name": name, "reason": REASON_UNSUPPORTED_TYPE})
            continue
        content = item.get("content") or b""
        if not content:
            rejected.append({"name": name, "reason": REASON_EMPTY_FILE})
            continue
        if len(content) > storage.MAX_FILE_BYTES:
            rejected.append({"name": name, "reason": REASON_TOO_LARGE})
            continue
        try:
            saved = storage.save_upload(root, raw_name, content)
        except storage.UnsafeUploadPath as exc:
            logger.warning("upload_unsafe_name raw=%r err=%s", raw_name, exc)
            rejected.append({"name": name, "reason": REASON_UNSAFE_NAME})
            continue
        except OSError as exc:  # pragma: no cover - 磁盘异常
            logger.exception("upload_save_failed name=%s", name)
            raise _error(
                status.HTTP_500_INTERNAL_SERVER_ERROR,
                "upload_write_failed",
                f"上传文件落盘失败：{exc}",
                {"name": name},
            ) from exc
        accepted.append(
            {
                "name": saved.name,
                "path": str(saved.path),
                "sha256": saved.sha256,
                "size_bytes": saved.size_bytes,
                "reused_existing": saved.reused_existing,
            }
        )

    if not accepted:
        raise _error(
            status.HTTP_422_UNPROCESSABLE_ENTITY,
            "no_valid_files",
            "没有任何文件通过校验（仅接受 application/pdf 或 .pdf，单个 ≤20MB，单次 ≤20 个）",
            {"rejected": rejected},
        )

    submitted = jobs.submit_pdf_import(accepted, project_id=None)
    logger.info(
        "pdf_import_submitted task_id=%s accepted=%d rejected=%d",
        submitted["task_id"],
        len(accepted),
        len(rejected),
    )
    return {
        "task_id": submitted["task_id"],
        "status": submitted["status"],
        "job": submitted["job"],
        "accepted_files": len(accepted),
        "accepted_file_names": [item["name"] for item in accepted],
        "rejected": rejected,
        "store_dir": str(root),
        "poll_url": f"/api/v1/papers/import-jobs/{submitted['task_id']}",
        "note": RESTART_NOTE,
    }


# --------------------------------------------------------------------------------------
# POST /papers/import/identifiers
# --------------------------------------------------------------------------------------
@router.post(
    "/papers/import/identifiers",
    summary="批量导入 DOI / arXiv ID（长任务，owner 面）",
    dependencies=[Depends(require_owner)],
    status_code=status.HTTP_202_ACCEPTED,
)
async def import_identifiers(request: Request) -> dict[str, Any]:
    """``{"identifiers": [...], "project_id"?: int}`` → 202 + ``task_id``。

    先用 ``classify`` 分出可识别/不可识别：不可识别的**不进入后台任务**，
    直接在响应 ``rejected`` 里给出 ``rejection_reason``；可识别的交 ``import_many`` 后台执行。
    全部不可识别 → 422（``detail.rejected``）。
    """
    payload = await _read_json_object(request)
    values = _validated_identifiers(payload)
    project_id = _optional_project_id(payload)

    try:
        from services.ingest import identifier_import
    except ModuleNotFoundError as exc:
        logger.error("identifier_module_missing name=%s", exc.name)
        raise _error(
            status.HTTP_503_SERVICE_UNAVAILABLE,
            "identifier_import_unavailable",
            f"{_IDENTIFIER_MODULE} 未就绪（缺失模块 {exc.name}），标识符导入暂不可用",
            {"module": _IDENTIFIER_MODULE, "missing": exc.name},
        ) from exc

    accepted: list[str] = []
    rejected: list[dict[str, str]] = []
    for raw in values:
        classified = identifier_import.classify(raw)
        if not classified:
            rejected.append({"value": raw, "reason": identifier_import.rejection_reason(raw)})
            continue
        accepted.append(raw)

    if not accepted:
        raise _error(
            status.HTTP_422_UNPROCESSABLE_ENTITY,
            "no_valid_identifiers",
            "没有任何标识符可识别（支持 DOI 与 arXiv ID / arXiv 链接）",
            {"rejected": rejected},
        )

    submitted = jobs.submit_identifier_import(accepted, project_id=project_id)
    logger.info(
        "identifier_import_submitted task_id=%s accepted=%d rejected=%d",
        submitted["task_id"],
        len(accepted),
        len(rejected),
    )
    return {
        "task_id": submitted["task_id"],
        "status": submitted["status"],
        "job": submitted["job"],
        "accepted": len(accepted),
        "accepted_identifiers": accepted,
        "rejected": rejected,
        "project_id": project_id,
        "poll_url": f"/api/v1/papers/import-jobs/{submitted['task_id']}",
        "note": RESTART_NOTE,
    }


# --------------------------------------------------------------------------------------
# GET /papers/import-jobs/{task_id}
# --------------------------------------------------------------------------------------
@router.get("/papers/import-jobs/{task_id}", summary="查询导入任务状态（轮询口）")
async def import_job_status(task_id: str) -> dict[str, Any]:
    """返回任务快照；任务状态只存内存，进程重启后一律 404（见 ``note``）。"""
    task = jobs.get_task(task_id)
    if task is None:
        raise _error(
            status.HTTP_404_NOT_FOUND,
            "task_not_found",
            f"未找到导入任务 {task_id}（任务状态只存内存，服务重启后会丢失）",
            {"task_id": task_id},
        )
    task["note"] = RESTART_NOTE
    return task


# --------------------------------------------------------------------------------------
# GET /papers/imports
# --------------------------------------------------------------------------------------
@router.get("/papers/imports", summary="导入历史（按 papers.raw.upload 聚合，分页）")
async def list_imports(
    session: DbSession,
    page: int = Query(1, ge=1),
    page_size: int = Query(DEFAULT_PAGE_SIZE, ge=1, le=MAX_PAGE_SIZE),
) -> dict[str, Any]:
    """上传导入历史（真实落库数据，非编造）。

    ``papers.raw['upload']`` 由每次上传导入真实写入（文件名、来源 URL、文件 SHA-256、
    标题来源、导入时间与本次结果），因此这里能给出**可核对**的历史；
    ``status`` = 该论文最近一次上传导入的落库结果（``created`` / ``reused``）。
    标识符导入（DOI / arXiv ID）不写该键，故不在本列表中——库里没有独立的导入历史表，
    本包不允许新增迁移，这一点如实披露而不伪造记录。
    """
    where = Paper.raw["upload"].isnot(None)
    total = int(
        (await session.execute(select(func.count()).select_from(Paper).where(where))).scalar_one()
        or 0
    )
    rows = (
        await session.execute(
            select(Paper.id, Paper.title, Paper.source, Paper.raw, Paper.created_at)
            .where(where)
            .order_by(Paper.created_at.desc(), Paper.id.desc())
            .offset((page - 1) * page_size)
            .limit(page_size)
        )
    ).all()

    items: list[dict[str, Any]] = []
    for paper_id, title, source, raw, created_at in rows:
        upload = raw.get("upload") if isinstance(raw, Mapping) else None
        upload = upload if isinstance(upload, Mapping) else {}
        items.append(
            {
                # 没有独立导入历史表 → 无独立历史主键，用 papers.id 定位论文
                "id": None,
                "source_type": "upload",
                "name_or_identifier": upload.get("filename") or title,
                "paper_id": int(paper_id),
                "status": upload.get("status"),
                "created_at": upload.get("imported_at")
                or (created_at.isoformat() if created_at is not None else None),
                "paper_source": source,
                "title": title,
                "file_sha256": upload.get("file_sha256"),
                "title_source": upload.get("title_source"),
                "extracted_doi": upload.get("extracted_doi"),
                "extracted_arxiv_id": upload.get("extracted_arxiv_id"),
            }
        )

    return {
        "items": items,
        "total": total,
        "page": page,
        "page_size": page_size,
        "source_type": "upload",
        "scope_note": (
            "数据来自 papers.raw['upload']（每次上传导入真实写入，可用 file_sha256 核对）；"
            "库内无独立导入历史表，本包不新增迁移，故标识符导入记录不在本列表中"
        ),
        "restart_note": RESTART_NOTE,
    }


__all__ = [
    "DEFAULT_PAGE_SIZE",
    "MAX_IDENTIFIERS",
    "MAX_PAGE_SIZE",
    "router",
]
