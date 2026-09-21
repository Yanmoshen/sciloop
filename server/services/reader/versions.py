# Copyright 2026 SciLoop contributors
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
"""不可变阅读版本：登记、读取、文本索引（EasyPaper §4.4 ReaderVersion）。

版本种类
--------
===========  ===========================================================
``original``  创建阅读文档时自动登记（原文 PDF，证据源，**永不被译文替代**）
``chinese``   取自翻译 manifest 的 ``files.mono``（+ 记录 ``dual`` 是否可用）
``simple``    取自 ``files.mono``
``bilingual`` 取自 ``files.dual``（manifest 未产出 ``dual.pdf`` → 拒绝登记，不降级）
===========  ===========================================================

不可变性（硬约束）
------------------
- **ORM 层**：本模块注册 ``before_update`` 事件，任何 UPDATE 抛
  :class:`ReaderVersionImmutableError`（``code=reader_version_immutable``）；
- **数据库层**：迁移 ``0003_reader_library`` 的 ``BEFORE UPDATE`` 触发器同码拒绝；
- **重复登记**：同一 ``(document_id, kind, task_id)`` → 显式 409 ``version_already_registered``
  ——**选择 409 而不是「静默返回旧版本」**：用户登记的是另一个 ``task_id`` 的产物，
  返回旧记录会让「我登记成功了」变成假象，属于取证上的撒谎。既有版本 id 放在
  ``detail.existing_version_id``。
- **同 kind 可追加**（迁移 ``0004_reader_version_kind_history``）：同一 ``kind`` 的**不同**
  ``task_id`` 会追加为更高 ``version_no`` 的新版本，旧版本一行不动。"当前版本" = 同 kind 的
  最大 ``version_no``（阅读器按倒序展示）。这条是「引擎修好后新译文登记不进来」的解药：
  旧口径下 ``chinese`` 被早期产物占用后，修正后的译文永远进不了阅读器。

翻译 manifest（与并行 agent 的冻结接口）
----------------------------------------
``.cache/artifacts/<task_id>/manifest.json``（容器内 ``/app/server/.cache/artifacts``）
的 ``sha256`` / ``layout_warnings`` / ``highlight_summary`` / ``created_at`` /
``finished_at`` / ``mode`` / ``engine`` / ``status`` **原样落库**（``manifest_snapshot``），
便于事后审计；本模块只做「读取 + 校验 + 复制」，不改写 manifest 任何字段。
"""

from __future__ import annotations

import logging
import os
import re
from pathlib import Path
from typing import Any

from sqlalchemy import event, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from db.models.reader import (
    REGISTRABLE_KINDS,
    VERSION_KINDS,
    ReaderDocument,
    ReaderVersion,
)
from services.reader import parsing, storage
from services.reader.errors import (
    ManifestInvalidError,
    ManifestMismatchError,
    ManifestNotCompletedError,
    ReaderVersionImmutableError,
    VersionAlreadyRegisteredError,
    VersionArtifactMissingError,
    VersionNotFoundError,
)

logger = logging.getLogger("sciloop.reader.versions")

MANIFEST_SCHEMA_VERSION = 1
INDEX_SCHEMA_VERSION = 1
TASK_ID_RE = re.compile(r"^[A-Za-z0-9_-]{4,64}$")
#: ``files`` 中 kind → 需要的产物键
REQUIRED_FILE_KEY: dict[str, str] = {
    "chinese": "mono",
    "simple": "mono",
    "bilingual": "dual",
}


# --------------------------------------------------------------------------- #
# 不可变性护栏：把「禁止 UPDATE」变成运行期错误（与实验 Passport 同一做法）
# --------------------------------------------------------------------------- #
@event.listens_for(ReaderVersion, "before_update")
def _block_version_update(mapper: Any, connection: Any, target: Any) -> None:  # noqa: ARG001
    """ORM 层拦截 UPDATE（迁移脚本走原生 SQL，不受影响）。"""
    raise ReaderVersionImmutableError(
        "阅读版本创建后不可 UPDATE（EasyPaper §4.8）：请重新登记新版本",
        detail={"version_id": getattr(target, "id", None), "table": "reader_versions"},
    )


def assert_immutable_update() -> None:
    """供测试/验收显式断言「UPDATE 被拒」的入口。"""
    raise ReaderVersionImmutableError(
        "阅读版本创建后不可 UPDATE（EasyPaper §4.8 reader_versions 为不可变凭证）"
    )


# --------------------------------------------------------------------------- #
# 翻译产物目录与 manifest 读取
# --------------------------------------------------------------------------- #
def artifacts_root() -> Path:
    """翻译产物根：``<server_root>/.cache/artifacts``。

    与 ``services/translate/artifacts.py`` 使用**同一环境变量**
    ``TRANSLATE_ARTIFACT_DIR``（便于离线验证），默认路径与 compose 的 bind mount 一致。
    """
    override = os.environ.get("TRANSLATE_ARTIFACT_DIR")
    if override:
        return Path(override)
    return Path(__file__).resolve().parents[2] / ".cache" / "artifacts"


def load_manifest(task_id: str) -> dict[str, Any]:
    """读取并校验翻译 manifest（``schema_version=1``）。"""
    if not TASK_ID_RE.match(str(task_id or "").strip()):
        raise ManifestInvalidError(
            f"task_id='{task_id}' 形态非法（只允许 [A-Za-z0-9_-]{{4,64}}）",
            detail={"task_id": task_id},
        )
    path = artifacts_root() / str(task_id).strip() / "manifest.json"
    manifest = storage.read_json(path)
    if manifest is None:
        raise ManifestInvalidError(
            f"未找到翻译产物 manifest：{path}（产物目录被清理或 task_id 有误）",
            detail={"task_id": task_id, "expected": "manifest.json"},
        )
    if int(manifest.get("schema_version") or 0) != MANIFEST_SCHEMA_VERSION:
        raise ManifestInvalidError(
            f"manifest.schema_version 不是 {MANIFEST_SCHEMA_VERSION}，拒绝解释未知结构",
            detail={"schema_version": manifest.get("schema_version")},
        )
    return manifest


# --------------------------------------------------------------------------- #
# 文本索引与页面对应关系
# --------------------------------------------------------------------------- #
def build_page_map(
    *,
    source_page_count: int,
    target_page_count: int | None,
    kind: str,
) -> dict[str, Any]:
    """页面对应关系：只有两边页数**真实一致**才声明 ``identity``。"""
    if target_page_count is None:
        return {
            "mode": "unavailable",
            "source_page_count": source_page_count,
            "target_page_count": None,
            "entries": [
                {"source_page": page, "target_page": None}
                for page in range(1, source_page_count + 1)
            ],
            "note": "目标 PDF 页数不可读取：不做任何页码映射，跨版本对齐走文本匹配",
        }
    if target_page_count == source_page_count:
        return {
            "mode": "identity",
            "source_page_count": source_page_count,
            "target_page_count": target_page_count,
            "entries": [
                {"source_page": page, "target_page": page}
                for page in range(1, source_page_count + 1)
            ],
            "note": "源与目标页数一致，按页号 1:1 映射（仅用于定位提示，不作为唯一依据）",
        }
    return {
        "mode": "unavailable",
        "source_page_count": source_page_count,
        "target_page_count": target_page_count,
        "entries": [
            {"source_page": page, "target_page": None}
            for page in range(1, source_page_count + 1)
        ],
        "note": (
            f"源 {source_page_count} 页 / 目标 {target_page_count} 页不一致，"
            "拒绝按比例猜测页码映射，跨版本对齐走文本匹配"
        ),
    }


def build_text_index(
    *,
    kind: str,
    document_id: int,
    version_no: int,
    source_page_count: int,
    target_page_count: int | None,
    page_map: dict[str, Any],
    blocks: list[dict[str, Any]],
    extra: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """文本索引：逐页列出 ``source_text`` / ``target_text`` / ``bbox``。

    ``original`` 版本的 ``target_text`` 恒为 ``None``（**原文没有译文**，
    不使用任何翻译内容顶替，避免把译文当证据）。
    """
    pages: dict[int, list[dict[str, Any]]] = {}
    for index, block in enumerate(blocks):
        page = int(block.get("page") or 1)
        bbox = block.get("bbox")
        pages.setdefault(page, []).append(
            {
                "index": index,
                "source_text": str(block.get("source_text") or ""),
                "target_text": (
                    None if kind == "original" else str(block.get("target_text") or "")
                ),
                "bbox": bbox if isinstance(bbox, list) else None,
            }
        )
    payload: dict[str, Any] = {
        "schema_version": INDEX_SCHEMA_VERSION,
        "kind": kind,
        "document_id": int(document_id),
        "version_no": int(version_no),
        "source_page_count": int(source_page_count),
        "target_page_count": target_page_count,
        "page_map": page_map,
        "block_count": len(blocks),
        "pages": [{"page": page, "blocks": pages[page]} for page in sorted(pages)],
        "note": (
            "文本索引由原文解析结果（original）或翻译 manifest.blocks（译文版本）生成；"
            "未匹配到的位置不补造"
        ),
    }
    if extra:
        payload.update(extra)
    return payload


def _index_path(version: ReaderVersion) -> Path:
    return storage.versions_dir(version.document_id) / version.index_file_name


def read_index(version: ReaderVersion) -> dict[str, Any]:
    """读取版本文本索引；缺失/损坏 → 404 ``version_artifact_missing``（不返回空对象）。"""
    payload = storage.read_json(_index_path(version))
    if not isinstance(payload, dict):
        raise VersionArtifactMissingError(
            f"版本 {version.id} 的文本索引文件缺失或不可解析：{version.index_file_name}",
            detail={"version_id": version.id, "index_file_name": version.index_file_name},
        )
    return payload


def pdf_path(version: ReaderVersion) -> Path:
    """版本 PDF 路径；文件缺失 → 404（不返回空 PDF）。"""
    path = storage.versions_dir(version.document_id) / version.file_name
    if not path.is_file():
        raise VersionArtifactMissingError(
            f"版本 {version.id} 的 PDF 文件缺失：{version.file_name}"
            "（reader-library 目录未持久化会导致此错误）",
            detail={"version_id": version.id, "file_name": version.file_name},
        )
    return path


def to_api_dict(version: ReaderVersion) -> dict[str, Any]:
    """版本对外结构（``sha256`` / ``layout_warnings`` 等审计字段原样透出）。"""
    snapshot = version.manifest_snapshot if isinstance(version.manifest_snapshot, dict) else {}
    return {
        "id": version.id,
        "document_id": version.document_id,
        "version_no": version.version_no,
        "kind": version.kind,
        "task_id": version.task_id,
        "engine": version.engine,
        "is_immutable": True,
        "file_name": version.file_name,
        "file_sha256": version.file_sha256,
        "file_size_bytes": version.file_size_bytes,
        "index_file_name": version.index_file_name,
        "index_sha256": version.index_sha256,
        "source_sha256": version.source_sha256,
        "pdf_sha256": version.pdf_sha256,
        "hash_verified": bool(version.hash_verified),
        "sha256": snapshot.get("sha256"),
        "page_map": version.page_map,
        "block_count": version.block_count,
        "layout_warnings": version.layout_warnings,
        "highlight_summary": snapshot.get("highlight_summary"),
        "manifest": {
            "schema_version": snapshot.get("schema_version"),
            "mode": snapshot.get("mode"),
            "highlight": snapshot.get("highlight"),
            "engine": snapshot.get("engine"),
            "status": snapshot.get("status"),
            "pages": snapshot.get("pages"),
            "created_at": snapshot.get("created_at"),
            "finished_at": snapshot.get("finished_at"),
        }
        if snapshot
        else None,
        "content_fingerprint": version.content_fingerprint,
        "created_at": version.created_at.isoformat()
        if hasattr(version.created_at, "isoformat")
        else version.created_at,
        "pdf_url": f"/api/v1/reader/documents/{version.document_id}/versions/{version.id}/pdf",
        "text_url": f"/api/v1/reader/documents/{version.document_id}/versions/{version.id}/text",
        "note": (
            "版本文件不可变；重复登记同一 kind 返回 409，"
            "原始 PDF 始终作为证据源（译文不替代原文）"
        ),
    }


# --------------------------------------------------------------------------- #
# 登记
# --------------------------------------------------------------------------- #
async def list_versions(session: AsyncSession, document: ReaderDocument) -> list[ReaderVersion]:
    rows = (
        await session.execute(
            select(ReaderVersion)
            .where(ReaderVersion.document_id == int(document.id))
            .order_by(ReaderVersion.version_no)
        )
    ).scalars().all()
    return list(rows)


async def get_version(
    session: AsyncSession, document: ReaderDocument, version_id: int
) -> ReaderVersion:
    row = (
        await session.execute(
            select(ReaderVersion).where(
                ReaderVersion.id == int(version_id),
                ReaderVersion.document_id == int(document.id),
            )
        )
    ).scalar_one_or_none()
    if row is None:
        raise VersionNotFoundError(
            f"阅读版本 {version_id} 不存在或不属于阅读文档 {document.id}",
            detail={"document_id": int(document.id), "version_id": int(version_id)},
        )
    return row


async def _assert_kind_free(
    session: AsyncSession, document: ReaderDocument, kind: str
) -> None:
    """``kind`` 在该文档下**完全没有**版本时才通过（仅用于 ``original``）。

    ``original`` 每文档有且仅有一个，且由创建文档时自动登记。
    """
    existing = (
        await session.execute(
            select(ReaderVersion).where(
                ReaderVersion.document_id == int(document.id),
                ReaderVersion.kind == kind,
            )
        )
    ).scalar_one_or_none()
    if existing is not None:
        raise VersionAlreadyRegisteredError(
            f"阅读文档 {document.id} 已登记 kind='{kind}' 的版本 {existing.id}"
            "（版本不可变，不覆盖既有记录）",
            detail={
                "document_id": int(document.id),
                "kind": kind,
                "existing_version_id": existing.id,
                "existing_task_id": existing.task_id,
            },
        )


async def _assert_source_not_registered(
    session: AsyncSession, document: ReaderDocument, *, kind: str, task_id: str
) -> None:
    """同一 ``(kind, task_id)`` 已登记 → 409（幂等重入防护）。

    与 :func:`_assert_kind_free` 的区别：同 ``kind`` 的**不同**产物允许追加为更高
    ``version_no`` 的新版本（迁移 0004），只有「同一份产物重复登记」才是错误。
    """
    normalized = str(task_id).strip()
    existing = (
        await session.execute(
            select(ReaderVersion).where(
                ReaderVersion.document_id == int(document.id),
                ReaderVersion.kind == kind,
                ReaderVersion.task_id == normalized,
            )
        )
    ).scalar_one_or_none()
    if existing is not None:
        raise VersionAlreadyRegisteredError(
            f"阅读文档 {document.id} 已登记 kind='{kind}' 的 task_id='{normalized}' 版本 "
            f"{existing.id}（同一份产物重复登记，不覆盖既有记录）",
            detail={
                "document_id": int(document.id),
                "kind": kind,
                "task_id": normalized,
                "existing_version_id": existing.id,
                "existing_task_id": existing.task_id,
            },
        )


async def _next_version_no(session: AsyncSession, document: ReaderDocument) -> int:
    current = (
        await session.execute(
            select(func.max(ReaderVersion.version_no)).where(
                ReaderVersion.document_id == int(document.id)
            )
        )
    ).scalar_one_or_none()
    return int(current or 0) + 1


async def register_original(
    session: AsyncSession,
    document: ReaderDocument,
    *,
    content: bytes,
    source_url: str,
) -> ReaderVersion:
    """登记 ``original`` 版本（创建阅读文档时自动调用，PDF 即原文）。"""
    await _assert_kind_free(session, document, "original")
    payload = document.document
    version_no = await _next_version_no(session, document)
    page_count = int(document.page_count)
    blocks = [
        {
            "page": block.get("page"),
            "source_text": block.get("source_text"),
            "bbox": block.get("bbox"),
        }
        for block in parsing.iter_blocks(payload)
    ]
    page_map = build_page_map(
        source_page_count=page_count, target_page_count=page_count, kind="original"
    )
    index = build_text_index(
        kind="original",
        document_id=int(document.id),
        version_no=version_no,
        source_page_count=page_count,
        target_page_count=page_count,
        page_map=page_map,
        blocks=blocks,
        extra={
            "engine": parsing.PARSER_NAME,
            "fingerprint": document.fingerprint,
            "title": document.title,
            "parse_status": document.parse_status,
            "warnings": document.warnings,
            "note": "original 版本的 source_text 即原文正文；target_text 恒为 null",
        },
    )

    pdf_name = storage.version_file_name(version_no, "original", suffix=".pdf")
    index_name = storage.version_file_name(version_no, "original", suffix=".index.json")
    target = storage.versions_dir(int(document.id)) / pdf_name
    storage.write_bytes_atomic(target, content)
    stored_sha = storage.sha256_file(target)
    index_target = storage.versions_dir(int(document.id)) / index_name
    index_bytes = storage.canonical_json_bytes(index)
    storage.write_bytes_atomic(index_target, index_bytes)

    version = ReaderVersion(
        document_id=int(document.id),
        version_no=version_no,
        kind="original",
        task_id=None,
        engine=parsing.PARSER_NAME,
        file_name=pdf_name,
        file_sha256=stored_sha or storage.sha256_bytes(content),
        file_size_bytes=int(len(content)),
        index_file_name=index_name,
        index_sha256=storage.sha256_bytes(index_bytes),
        source_sha256=document.fingerprint,
        pdf_sha256=None,
        hash_verified=(stored_sha == document.fingerprint),
        page_map=page_map,
        block_count=len(blocks),
        layout_warnings=[],
        manifest_snapshot=None,
        content_fingerprint=document.payload_sha256,
    )
    session.add(version)
    await session.flush()
    logger.info(
        "reader_original_registered document_id=%s version_id=%s source_url=%s",
        document.id,
        version.id,
        source_url,
    )
    return version


async def register_from_manifest(
    session: AsyncSession,
    document: ReaderDocument,
    *,
    kind: str,
    task_id: str,
) -> ReaderVersion:
    """从翻译 manifest 登记不可变版本（``chinese`` / ``simple`` / ``bilingual``）。

    同一 ``kind`` 可以登记**多份不同** ``task_id`` 的产物（追加为更高 ``version_no``）；
    同一 ``(kind, task_id)`` 重复登记 → 409。
    """
    if kind not in REGISTRABLE_KINDS:
        raise ManifestInvalidError(
            f"kind='{kind}' 不允许通过该接口登记（original 由创建文档时自动生成）",
            detail={"allowed": list(REGISTRABLE_KINDS), "all": list(VERSION_KINDS)},
        )
    await _assert_source_not_registered(session, document, kind=kind, task_id=task_id)
    manifest = load_manifest(task_id)

    manifest_paper = manifest.get("paper_id")
    if manifest_paper is not None and int(manifest_paper) != int(document.paper_id):
        raise ManifestMismatchError(
            f"manifest.paper_id={manifest_paper} 与阅读文档的 paper_id={document.paper_id} 不一致",
            detail={
                "manifest_paper_id": manifest_paper,
                "paper_id": int(document.paper_id),
                "task_id": task_id,
            },
        )
    if str(manifest.get("status") or "") != "completed":
        raise ManifestNotCompletedError(
            f"manifest.status='{manifest.get('status')}' 不是 completed：产物不完整，拒绝登记",
            detail={"task_id": task_id, "status": manifest.get("status")},
        )

    files = manifest.get("files") if isinstance(manifest.get("files"), dict) else {}
    file_key = REQUIRED_FILE_KEY[kind]
    file_name = files.get(file_key)
    if not file_name:
        raise ManifestMismatchError(
            f"manifest.files.{file_key} 为空：kind='{kind}' 缺少对应产物，拒绝降级登记",
            detail={"task_id": task_id, "kind": kind, "required": file_key, "files": files},
        )
    source_pdf = artifacts_root() / str(task_id).strip() / storage.sanitize_segment(str(file_name))
    try:
        pdf_bytes = source_pdf.read_bytes()
    except OSError as exc:
        raise ManifestMismatchError(
            f"manifest 声明的产物文件不可读：{source_pdf}（{exc}）",
            detail={"task_id": task_id, "file": str(file_name)},
        ) from exc
    if not pdf_bytes:
        raise ManifestMismatchError(
            f"manifest 声明的产物文件为空：{source_pdf}", detail={"task_id": task_id}
        )

    sha256_map = manifest.get("sha256") if isinstance(manifest.get("sha256"), dict) else {}
    declared_sha = str(sha256_map.get(file_key) or "").strip().lower() or None
    manifest_blocks = manifest.get("blocks")
    blocks = [
        {
            "page": entry.get("page"),
            "source_text": entry.get("source_text"),
            "target_text": entry.get("target_text"),
            "bbox": entry.get("bbox"),
        }
        for entry in manifest_blocks
        if isinstance(entry, dict)
    ] if isinstance(manifest_blocks, list) else []

    version_no = await _next_version_no(session, document)
    source_page_count = int(document.page_count)
    manifest_pages = manifest.get("pages")
    target_page_count = parsing.pdf_page_count(pdf_bytes)
    page_map = build_page_map(
        source_page_count=source_page_count,
        target_page_count=target_page_count,
        kind=kind,
    )
    if isinstance(manifest_pages, int) and manifest_pages != source_page_count:
        page_map = {
            **page_map,
            "manifest_pages": manifest_pages,
            "note": (
                f"{page_map['note']}；manifest.pages={manifest_pages} 与阅读文档 "
                f"page_count={source_page_count} 不一致（已如实记录，不做页码猜测）"
            ),
        }

    index = build_text_index(
        kind=kind,
        document_id=int(document.id),
        version_no=version_no,
        source_page_count=source_page_count,
        target_page_count=target_page_count,
        page_map=page_map,
        blocks=blocks,
        extra={
            "task_id": task_id,
            "engine": manifest.get("engine"),
            "mode": manifest.get("mode"),
            "highlight": manifest.get("highlight"),
            "source_sha256": sha256_map.get("source"),
            "pdf_sha256": declared_sha,
            "highlight_summary": manifest.get("highlight_summary"),
            "layout_warnings": manifest.get("layout_warnings"),
            "manifest_status": manifest.get("status"),
            "note": (
                "译文版本的 source_text 为原文、target_text 为译文（来自 manifest.blocks，原样保存）"
            ),
        },
    )

    pdf_name = storage.version_file_name(version_no, kind, suffix=".pdf")
    index_name = storage.version_file_name(version_no, kind, suffix=".index.json")
    target = storage.versions_dir(int(document.id)) / pdf_name
    storage.write_bytes_atomic(target, pdf_bytes)
    stored_sha = storage.sha256_file(target) or storage.sha256_bytes(pdf_bytes)
    index_target = storage.versions_dir(int(document.id)) / index_name
    index_bytes = storage.canonical_json_bytes(index)
    storage.write_bytes_atomic(index_target, index_bytes)

    layout_warnings = manifest.get("layout_warnings")
    version = ReaderVersion(
        document_id=int(document.id),
        version_no=version_no,
        kind=kind,
        task_id=str(task_id).strip(),
        engine=str(manifest.get("engine") or "") or None,
        file_name=pdf_name,
        file_sha256=stored_sha,
        file_size_bytes=int(len(pdf_bytes)),
        index_file_name=index_name,
        index_sha256=storage.sha256_bytes(index_bytes),
        source_sha256=str(sha256_map.get("source") or "") or None,
        pdf_sha256=declared_sha,
        hash_verified=bool(declared_sha) and declared_sha == stored_sha,
        page_map=page_map,
        block_count=len(blocks),
        layout_warnings=layout_warnings if isinstance(layout_warnings, list) else [],
        # manifest 原样保存（审计用）：不做字段裁剪、不做二次解释
        manifest_snapshot=manifest,
        content_fingerprint=document.payload_sha256,
    )
    session.add(version)
    await session.flush()
    logger.info(
        "reader_version_registered document_id=%s version_id=%s kind=%s task_id=%s hashes_verified=%s",
        document.id,
        version.id,
        kind,
        task_id,
        version.hash_verified,
    )
    return version


__all__ = [
    "MANIFEST_SCHEMA_VERSION",
    "assert_immutable_update",
    "artifacts_root",
    "build_page_map",
    "build_text_index",
    "get_version",
    "list_versions",
    "load_manifest",
    "pdf_path",
    "read_index",
    "register_from_manifest",
    "register_original",
    "to_api_dict",
]
