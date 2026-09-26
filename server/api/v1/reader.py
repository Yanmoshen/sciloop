# Copyright 2026 SciLoop contributors
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
"""全文阅读器端点（EasyPaper 四核心模块之 ③，自研 PyMuPDF block 解析）。

==================================================================  ==========================
``POST   /reader/documents``                                        Owner；登记阅读文档 → 201
``GET    /reader/documents``                                        列表（?page=&page_size=）
``GET    /reader/documents/{document_id}``                          详情（pages/sections/blocks）
``GET    /reader/documents/{document_id}/versions``                 版本列表（含不可变审计字段）
``GET    /reader/documents/{id}/versions/{vid}/pdf``                application/pdf
``GET    /reader/documents/{id}/versions/{vid}/text``               文本索引 JSON
``POST   /reader/documents/{document_id}/versions``                 Owner；登记不可变版本 → 201
``GET    /reader/state/{document_id}``                              阅读状态
``PATCH  /reader/state/{document_id}``                              Owner；局部更新状态
``GET    /reader/documents/{id}/annotations``                       批注列表（?q= &kind=）
``POST   /reader/documents/{id}/annotations``                       Owner；新建批注 → 201
``PUT    /reader/documents/{id}/annotations/{aid}``                 Owner；`revision` 乐观锁
``DELETE /reader/documents/{id}/annotations/{aid}``                 Owner；204
``POST   /reader/documents/{id}/annotations/{aid}/align``           Owner；跨版本对齐
``GET    /reader/documents/{id}/annotations/export``                application/json
``GET    /reader/documents/{id}/archive``                           application/zip
``POST   /reader/documents/{id}/restore``                           Owner；从归档恢复 → 200
==================================================================  ==========================

口径（硬约束）
--------------
- **写接口一律 Owner 面**（``require_owner``）：匿名 → 403 ``owner_token_required``；
  只读端点公开。路由**不用** ``File`` / ``Form``，避免「请求体先于鉴权被解析」。
- **错误体统一** ``{code,message,detail}``；未知 id → 404
  （``document_not_found`` / ``version_not_found`` / ``annotation_not_found``），
  非法参数 → 422，状态冲突 → 409（``no_source_document`` /
  ``annotation_conflict`` / ``version_already_registered`` / ``reader_version_immutable``）。
- **禁止把不确定说成成功**：``align`` 只在全部目标版本精确命中时才返回 ``success``，
  部分匹配/未覆盖一律 ``partial``，一个都没中或有且仅有单一版本 → ``pending``。
- ``kind`` 只允许 ``chinese``（中文译本）；``original`` 由创建文档时
  自动登记，**不能**通过登记接口提交。

本轮**不做**（明确留给下一轮）
-----------------------------
§4.6 的 LLM 阅读辅助：``POST /reading/{task_id}/blocks/{block_id}/explain`` /
``/aid``、``POST /reading/{task_id}/ask`` / ``/summary``，以及
``GET /api/v1/reading/{task_id}/export``。原因：依赖真实 LLM 链路与更强的证据校验设计
（模型只能引用输入中存在的 block id、引用不存在则删除、原文不足必须返回不确定性），
先把「解析 + 版本 + 状态 + 批注 + 对齐 + 归档」主干做扎实。
"""

from __future__ import annotations

import logging
from collections.abc import AsyncIterator, Iterator
from contextlib import contextmanager
from typing import Annotated, Any, Literal

from fastapi import APIRouter, Depends, HTTPException, Query, status
from fastapi.responses import Response
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy.ext.asyncio import AsyncSession

from core.security import require_owner
from db.models.reader import VERSION_KINDS
from db.session import AsyncSessionLocal
from services.reader import annotations as annotations_service
from services.reader import archive as archive_service
from services.reader import documents as documents_service
from services.reader import state as state_service
from services.reader import storage
from services.reader import versions as versions_service
from services.reader.errors import ReaderError

logger = logging.getLogger("sciloop.reader.api")

router = APIRouter(tags=["reader"])

MAX_PAGE_SIZE = 100
DEFAULT_PAGE_SIZE = 20
MAX_ANNOTATION_PAGE_SIZE = 200
VersionKind = Literal["chinese"]


# --------------------------------------------------------------------------- #
# 会话依赖与统一错误体
# --------------------------------------------------------------------------- #
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
        status_code=status_code,
        detail={"code": code, "message": message, "detail": detail},
    )


@contextmanager
def _guard() -> Iterator[None]:
    """把服务层的 :class:`ReaderError` 映射成 ``{code,message,detail}`` + 其自带状态码。"""
    try:
        yield
    except ReaderError as exc:
        logger.info("reader_error code=%s status=%s", exc.code, exc.status_code)
        raise _error(exc.status_code, exc.code, str(exc), exc.detail) from exc
    except storage.UnsafeReaderPath as exc:
        raise _error(
            status.HTTP_422_UNPROCESSABLE_ENTITY,
            "unsafe_path",
            str(exc),
            {"reason": "路径穿越被拒绝"},
        ) from exc


# --------------------------------------------------------------------------- #
# 请求体
# --------------------------------------------------------------------------- #
class CreateDocumentRequest(BaseModel):
    """``POST /reader/documents``。"""

    model_config = ConfigDict(extra="forbid")

    paper_id: int = Field(..., ge=1, description="已入库论文 id（必须有本地原文，否则 409）")
    force: bool = Field(
        default=False,
        description="true=同一份原文重新解析（block id 为内容寻址，两次结果应完全一致）",
    )


class RegisterVersionRequest(BaseModel):
    """``POST /reader/documents/{id}/versions``。"""

    model_config = ConfigDict(extra="forbid")

    kind: VersionKind = Field(..., description="chinese（中文译本）")
    task_id: str | None = Field(
        default=None,
        description="翻译任务 id（读取 .cache/artifacts/<task_id>/manifest.json）；缺失 → 422",
    )


class StatePatchRequest(BaseModel):
    """``PATCH /reader/state/{document_id}``（未出现的键不改动）。"""

    model_config = ConfigDict(extra="forbid")

    current_block: str | None = Field(default=None, description="稳定 block id；null 表示清空")
    offset: int | None = Field(default=None, ge=0)
    mode: str | None = Field(default=None, description="original|chinese")
    font_size: int | None = Field(default=None, ge=8, le=48)
    understood_blocks: list[str] | None = None
    favorite_terms: list[str] | None = None


class AnnotationCreateRequest(BaseModel):
    """``POST /reader/documents/{id}/annotations``。"""

    model_config = ConfigDict(extra="forbid")

    version_id: int = Field(..., ge=1, description="批注所在版本（必填，用于锚点解析）")
    quote_text: str = Field(..., description="摘录原文/译文；服务端据此解析原文锚点")
    block_id: str | None = None
    note: str | None = None
    kind: Literal["highlight", "note", "question", "summary"] | None = None
    page: int | None = Field(default=None, ge=1)


class AnnotationUpdateRequest(BaseModel):
    """``PUT /reader/documents/{id}/annotations/{aid}``（``revision`` 乐观锁）。"""

    model_config = ConfigDict(extra="forbid")

    revision: int = Field(..., ge=1, description="必须等于当前 revision，否则 409")
    quote_text: str | None = None
    note: str | None = None
    kind: Literal["highlight", "note", "question", "summary"] | None = None
    block_id: str | None = None


class RestoreRequest(BaseModel):
    """``POST /reader/documents/{id}/restore``。"""

    model_config = ConfigDict(extra="forbid")

    archive_name: str | None = Field(
        default=None, description="归档文件名；缺省用该文档最新一份归档"
    )


# --------------------------------------------------------------------------- #
# 文档
# --------------------------------------------------------------------------- #
@router.post(
    "/reader/documents",
    summary="登记阅读文档（解析原文 PDF，自动登记 original 版本）",
    status_code=status.HTTP_201_CREATED,
    dependencies=[Depends(require_owner)],
)
async def create_document(
    body: CreateDocumentRequest, session: DbSession
) -> dict[str, Any]:
    with _guard():
        document, trace = await documents_service.create_or_get_document(
            session, paper_id=body.paper_id, force=body.force
        )
        await session.commit()
        await session.refresh(document)
        payload = documents_service.document_detail(document)
        payload["trace"] = trace
        payload["versions"] = [
            versions_service.to_api_dict(version)
            for version in await versions_service.list_versions(session, document)
        ]
    return payload


@router.get("/reader/documents", summary="阅读文档列表")
async def list_documents(
    session: DbSession,
    page: int = Query(1, ge=1),
    page_size: int = Query(DEFAULT_PAGE_SIZE, ge=1, le=MAX_PAGE_SIZE),
) -> dict[str, Any]:
    rows, total = await documents_service.list_documents(session, page=page, page_size=page_size)
    return {
        "items": [documents_service.document_summary(row) for row in rows],
        "total": total,
        "page": page,
        "page_size": page_size,
    }


@router.get("/reader/documents/{document_id}", summary="阅读文档详情（含稳定 block id）")
async def get_document(document_id: int, session: DbSession) -> dict[str, Any]:
    with _guard():
        document = await documents_service.get_document(session, document_id)
        return documents_service.document_detail(document)


# --------------------------------------------------------------------------- #
# 版本
# --------------------------------------------------------------------------- #
@router.get("/reader/documents/{document_id}/versions", summary="阅读版本列表")
async def list_versions(document_id: int, session: DbSession) -> dict[str, Any]:
    with _guard():
        document = await documents_service.get_document(session, document_id)
        rows = await versions_service.list_versions(session, document)
        return {
            "document_id": document.id,
            "items": [versions_service.to_api_dict(row) for row in rows],
            "total": len(rows),
            "kinds": list(VERSION_KINDS),
            "note": (
                "版本创建后不可修改（ORM before_update + 数据库触发器双保险，"
                "报错信息含 reader_version_immutable）"
            ),
        }


@router.post(
    "/reader/documents/{document_id}/versions",
    summary="登记不可变版本（chinese，来自翻译 manifest）",
    status_code=status.HTTP_201_CREATED,
    dependencies=[Depends(require_owner)],
)
async def register_version(
    document_id: int, body: RegisterVersionRequest, session: DbSession
) -> dict[str, Any]:
    with _guard():
        document = await documents_service.get_document(session, document_id)
        if not body.task_id:
            raise _error(
                status.HTTP_422_UNPROCESSABLE_ENTITY,
                "task_id_required",
                "登记译文版本必须提供 task_id：版本 PDF 来自 "
                ".cache/artifacts/<task_id>/manifest.json（禁止凭空生成版本内容）",
                {"kind": body.kind},
            )
        version = await versions_service.register_from_manifest(
            session, document, kind=body.kind, task_id=body.task_id
        )
        await session.commit()
        await session.refresh(version)
        payload = versions_service.to_api_dict(version)
    return payload


@router.get(
    "/reader/documents/{document_id}/versions/{version_id}/pdf",
    summary="下载版本 PDF（application/pdf）",
    response_class=Response,
)
async def get_version_pdf(document_id: int, version_id: int, session: DbSession) -> Response:
    with _guard():
        document = await documents_service.get_document(session, document_id)
        version = await versions_service.get_version(session, document, version_id)
        path = versions_service.pdf_path(version)
        payload = path.read_bytes()
        safe_name = storage.sanitize_segment(
            f"{document.id}-{version.version_no}-{version.kind}.pdf", fallback="version.pdf"
        )
    return Response(
        content=payload,
        media_type="application/pdf",
        headers={
            "Content-Disposition": f'inline; filename="{safe_name}"',
            "X-Version-Sha256": version.file_sha256,
            "X-Version-Kind": version.kind,
            "X-Version-Immutable": "true",
        },
    )


@router.get(
    "/reader/documents/{document_id}/versions/{version_id}/text",
    summary="版本文本索引（source_text / target_text / 页面对应关系）",
)
async def get_version_text(document_id: int, version_id: int, session: DbSession) -> dict[str, Any]:
    with _guard():
        document = await documents_service.get_document(session, document_id)
        version = await versions_service.get_version(session, document, version_id)
        index = versions_service.read_index(version)
    return {
        "document_id": document.id,
        "version": versions_service.to_api_dict(version),
        "index": index,
    }


# --------------------------------------------------------------------------- #
# 阅读状态
# --------------------------------------------------------------------------- #
@router.get("/reader/state/{document_id}", summary="阅读状态（读取时惰性创建默认行）")
async def get_state(document_id: int, session: DbSession) -> dict[str, Any]:
    with _guard():
        document = await documents_service.get_document(session, document_id)
        row = await state_service.get_or_create_state(session, document)
        await session.commit()
        return state_service.to_api_dict(row)


@router.patch(
    "/reader/state/{document_id}",
    summary="更新阅读状态（位置 / 模式 / 字号 / 已理解 block / 术语）",
    dependencies=[Depends(require_owner)],
)
async def patch_state(
    document_id: int, body: StatePatchRequest, session: DbSession
) -> dict[str, Any]:
    with _guard():
        document = await documents_service.get_document(session, document_id)
        row = await state_service.patch_state(
            session, document, fields=body.model_dump(exclude_unset=True)
        )
        return state_service.to_api_dict(row)


# --------------------------------------------------------------------------- #
# 批注
# --------------------------------------------------------------------------- #
@router.get(
    "/reader/documents/{document_id}/annotations/export",
    summary="导出批注 JSON（可重新解析，含版本审计字段）",
)
async def export_annotations(document_id: int, session: DbSession) -> dict[str, Any]:
    with _guard():
        document = await documents_service.get_document(session, document_id)
        rows, _total = await annotations_service.list_annotations(
            session, document, page=1, page_size=100000
        )
        version_rows = await versions_service.list_versions(session, document)
        return annotations_service.export_payload(document, rows, version_rows)


@router.get("/reader/documents/{document_id}/annotations", summary="批注列表（?q= 搜索摘录/笔记）")
async def list_annotations(
    document_id: int,
    session: DbSession,
    q: str | None = Query(None, description="搜索 quote_text / anchor_text / note"),
    kind: str | None = Query(None, description="highlight | note | question | summary"),
    align_status: str | None = Query(None, description="success | partial | pending"),
    page: int = Query(1, ge=1),
    page_size: int = Query(50, ge=1, le=MAX_ANNOTATION_PAGE_SIZE),
) -> dict[str, Any]:
    with _guard():
        document = await documents_service.get_document(session, document_id)
        rows, total = await annotations_service.list_annotations(
            session,
            document,
            q=q,
            kind=kind,
            align_status=align_status,
            page=page,
            page_size=page_size,
        )
        return {
            "document_id": document.id,
            "items": [annotations_service.serialize(row) for row in rows],
            "total": total,
            "page": page,
            "page_size": page_size,
            "q": q,
            "kind": kind,
            "align_status": align_status,
        }


@router.post(
    "/reader/documents/{document_id}/annotations",
    summary="新建批注（解析原文锚点，初始 align_status=pending）",
    status_code=status.HTTP_201_CREATED,
    dependencies=[Depends(require_owner)],
)
async def create_annotation(
    document_id: int, body: AnnotationCreateRequest, session: DbSession
) -> dict[str, Any]:
    with _guard():
        document = await documents_service.get_document(session, document_id)
        row = await annotations_service.create_annotation(
            session, document, fields=body.model_dump(exclude_unset=True)
        )
        return annotations_service.serialize(row)


@router.post(
    "/reader/documents/{document_id}/annotations/{annotation_id}/align",
    summary="批注跨版本对齐（success | partial | pending）",
    dependencies=[Depends(require_owner)],
)
async def align_annotation(
    document_id: int, annotation_id: int, session: DbSession
) -> dict[str, Any]:
    with _guard():
        document = await documents_service.get_document(session, document_id)
        return await annotations_service.align_annotation(session, document, annotation_id)


@router.put(
    "/reader/documents/{document_id}/annotations/{annotation_id}",
    summary="更新批注（必须携带 revision，冲突 → 409 annotation_conflict）",
    dependencies=[Depends(require_owner)],
)
async def update_annotation(
    document_id: int,
    annotation_id: int,
    body: AnnotationUpdateRequest,
    session: DbSession,
) -> dict[str, Any]:
    with _guard():
        document = await documents_service.get_document(session, document_id)
        row = await annotations_service.update_annotation(
            session, document, annotation_id, fields=body.model_dump(exclude_unset=True)
        )
        return annotations_service.serialize(row)


@router.delete(
    "/reader/documents/{document_id}/annotations/{annotation_id}",
    summary="删除批注",
    status_code=status.HTTP_204_NO_CONTENT,
    dependencies=[Depends(require_owner)],
)
async def delete_annotation(document_id: int, annotation_id: int, session: DbSession) -> Response:
    with _guard():
        document = await documents_service.get_document(session, document_id)
        await annotations_service.delete_annotation(session, document, annotation_id)
    return Response(status_code=status.HTTP_204_NO_CONTENT)


# --------------------------------------------------------------------------- #
# 归档 / 恢复
# --------------------------------------------------------------------------- #
@router.get(
    "/reader/documents/{document_id}/archive",
    summary="导出归档 ZIP（批注 JSON + 版本 PDF + 文本索引 + 页面对应关系）",
    response_class=Response,
)
async def export_archive(document_id: int, session: DbSession) -> Response:
    with _guard():
        document = await documents_service.get_document(session, document_id)
        built = await archive_service.export_archive(session, document)
        await session.commit()
    payload = built["payload"]
    return Response(
        content=payload,
        media_type="application/zip",
        headers={
            "Content-Disposition": f'attachment; filename="{built["name"]}"',
            "X-Archive-Sha256": storage.sha256_bytes(payload),
            "X-Archive-Bytes": str(len(payload)),
            "X-Archive-Manifest": "manifest.json",
        },
    )


@router.post(
    "/reader/documents/{document_id}/restore",
    summary="从归档恢复（补写缺失文件 / 按 uid 升级批注；不删除既有数据）",
    dependencies=[Depends(require_owner)],
)
async def restore_document(
    document_id: int, session: DbSession, body: RestoreRequest | None = None
) -> dict[str, Any]:
    request = body or RestoreRequest()
    with _guard():
        document = await documents_service.get_document(session, document_id)
        report = await archive_service.restore_archive(
            session, document, archive_name=request.archive_name
        )
        report["document_id"] = document.id
        report["available_archives"] = archive_service.list_archives(document.id)
        return report


__all__ = [
    "DEFAULT_PAGE_SIZE",
    "MAX_PAGE_SIZE",
    "router",
]
