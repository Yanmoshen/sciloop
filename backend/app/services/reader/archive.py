# Copyright 2026 SciLoop contributors
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
"""阅读器归档与恢复（EasyPaper §4.1「版本归档和恢复」/ §9 阅读器验证项）。

归档内容（ZIP，条目名全部经 :func:`app.services.reader.storage.safe_zip_name` 校验）::

    manifest.json                    归档自描述 + 逐条目 sha256 / 字节数
    document.json                    解析文档（pages / sections / blocks）
    state.json                       阅读状态（不存在时如实写 null，不伪造默认值）
    annotations.json                 批注（含 uid / revision / projections）
    versions/index.json              版本元数据（含库内文件名、哈希、layout_warnings）
    versions/<no>-<kind>.pdf         版本 PDF（文件缺失则跳过并记入 manifest.missing）
    versions/<no>-<kind>.index.json  文本索引（原样字节）

恢复语义（**不删数据**）
------------------------
- 恢复是「补写 + 按 uid 升级批注」，**从不删除任何既有记录或文件**；
- 归档中原有 ``document.json`` 的 ``fingerprint`` 与当前阅读文档不一致 → 直接拒绝
  （:class:`ArchiveRestoreError`），**在任何写操作之前**失败；
- 既有批注 ``revision`` 比归档更高 → 跳过并在报告中列为 ``skipped_newer``（不覆盖新修改）；
- 既有版本文件存在但 sha256 与归档不一致 → 保留既有文件并报告 ``conflict``，
  不做静默覆盖（原文优先，归档只是副本）。
"""

from __future__ import annotations

import logging
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models.reader import ReaderAnnotation, ReaderDocument, ReaderState, ReaderVersion
from app.services.reader import annotations as annotations_service
from app.services.reader import storage, versions
from app.services.reader.errors import (
    ArchiveNotFoundError,
    ArchiveRestoreError,
    UnsafeArchivePathError,
)

logger = logging.getLogger("sciloop.reader.archive")

ARCHIVE_SCHEMA_VERSION = 1
MANIFEST_NAME = "manifest.json"
DOCUMENT_NAME = "document.json"
STATE_NAME = "state.json"
ANNOTATIONS_NAME = "annotations.json"
VERSIONS_INDEX_NAME = "versions/index.json"
VERSIONS_PREFIX = "versions"


def _file_entry(document_id: int, version: ReaderVersion, *, index: bool) -> str:
    name = version.index_file_name if index else version.file_name
    return f"{VERSIONS_PREFIX}/{storage.sanitize_segment(name, fallback='version')}"


async def _state_payload(session: AsyncSession, document: ReaderDocument) -> dict[str, Any]:
    row = (
        await session.execute(
            select(ReaderState).where(ReaderState.document_id == int(document.id))
        )
    ).scalar_one_or_none()
    if row is None:
        return {
            "document_id": int(document.id),
            "state": None,
            "note": "该阅读文档尚无阅读状态记录（archive 不写入默认值，避免伪造用户行为）",
        }
    return {
        "document_id": int(document.id),
        "state": {
            "current_block": row.current_block,
            "offset": row.offset,
            "mode": row.mode,
            "font_size": row.font_size,
            "understood_blocks": row.understood_blocks,
            "favorite_terms": row.favorite_terms,
            "revision": row.revision,
            "updated_at": row.updated_at.isoformat()
            if hasattr(row.updated_at, "isoformat")
            else row.updated_at,
        },
    }


async def build_archive(session: AsyncSession, document: ReaderDocument) -> dict[str, Any]:
    """生成归档（返回 ``{payload, name, manifest}``；同时落盘到 ``archives/``）。"""
    version_rows = await versions.list_versions(session, document)
    annotation_rows = list(
        (
            await session.execute(
                select(ReaderAnnotation)
                .where(ReaderAnnotation.document_id == int(document.id))
                .order_by(ReaderAnnotation.id)
            )
        ).scalars().all()
    )
    state_payload = await _state_payload(session, document)

    entries: dict[str, bytes] = {
        DOCUMENT_NAME: storage.canonical_json_bytes(document.document),
        STATE_NAME: storage.canonical_json_bytes(state_payload),
        ANNOTATIONS_NAME: storage.canonical_json_bytes(
            {
                "schema_version": ARCHIVE_SCHEMA_VERSION,
                "document_id": int(document.id),
                "total": len(annotation_rows),
                "items": [annotations_service.serialize(row) for row in annotation_rows],
            }
        ),
    }

    version_meta: list[dict[str, Any]] = []
    missing: list[dict[str, Any]] = []
    for version in version_rows:
        files_dir = storage.versions_dir(int(document.id))
        pdf_file = files_dir / version.file_name
        index_file = files_dir / version.index_file_name
        pdf_included = pdf_file.is_file()
        index_included = index_file.is_file()
        if pdf_included:
            entries[_file_entry(int(document.id), version, index=False)] = pdf_file.read_bytes()
        else:
            missing.append({"version_id": version.id, "file": version.file_name})
        if index_included:
            entries[_file_entry(int(document.id), version, index=True)] = index_file.read_bytes()
        else:
            missing.append({"version_id": version.id, "file": version.index_file_name})
        version_meta.append(
            {
                "id": version.id,
                "version_no": version.version_no,
                "kind": version.kind,
                "task_id": version.task_id,
                "engine": version.engine,
                "file_name": version.file_name,
                "file_sha256": version.file_sha256,
                "file_size_bytes": version.file_size_bytes,
                "index_file_name": version.index_file_name,
                "index_sha256": version.index_sha256,
                "source_sha256": version.source_sha256,
                "pdf_sha256": version.pdf_sha256,
                "hash_verified": bool(version.hash_verified),
                "page_map": version.page_map,
                "block_count": version.block_count,
                "layout_warnings": version.layout_warnings,
                "manifest_snapshot": version.manifest_snapshot,
                "content_fingerprint": version.content_fingerprint,
                "created_at": version.created_at.isoformat()
                if hasattr(version.created_at, "isoformat")
                else version.created_at,
                "pdf_entry": _file_entry(int(document.id), version, index=False),
                "index_entry": _file_entry(int(document.id), version, index=True),
                "pdf_included": pdf_included,
                "index_included": index_included,
            }
        )
    entries[VERSIONS_INDEX_NAME] = storage.canonical_json_bytes(
        {
            "schema_version": ARCHIVE_SCHEMA_VERSION,
            "document_id": int(document.id),
            "total": len(version_meta),
            "items": version_meta,
        }
    )

    created_at = datetime.now(UTC).isoformat()
    manifest = {
        "schema_version": ARCHIVE_SCHEMA_VERSION,
        "document_id": int(document.id),
        "paper_id": document.paper_id,
        "title": document.title,
        "fingerprint": document.fingerprint,
        "payload_sha256": document.payload_sha256,
        "page_count": document.page_count,
        "block_count": document.block_count,
        "created_at": created_at,
        "counts": {
            "versions": len(version_rows),
            "annotations": len(annotation_rows),
            "entries": len(entries) + 1,
        },
        "align_status_counts": {
            status: sum(1 for row in annotation_rows if row.align_status == status)
            for status in ("success", "partial", "pending")
        },
        "entries": [
            {
                "name": storage.safe_zip_name(name),
                "sha256": storage.sha256_bytes(payload),
                "bytes": len(payload),
            }
            for name, payload in sorted(entries.items())
        ],
        "missing": missing,
        "note": (
            "条目名已安全化（无 `..`、无绝对路径）；哈希为归档内实际字节的 sha256，"
            "可离线复算"
        ),
    }
    entries[MANIFEST_NAME] = storage.canonical_json_bytes(manifest)
    payload = storage.build_zip(entries)

    stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
    name = storage.archive_file_name(int(document.id), stamp)
    target = storage.archives_dir(int(document.id)) / name
    storage.write_bytes_atomic(target, payload)
    logger.info(
        "reader_archive_created document_id=%s name=%s bytes=%d versions=%d annotations=%d missing=%d",
        document.id,
        name,
        len(payload),
        len(version_rows),
        len(annotation_rows),
        len(missing),
    )
    return {"payload": payload, "name": name, "manifest": manifest, "path": target}


async def export_archive(session: AsyncSession, document: ReaderDocument) -> dict[str, Any]:
    """``GET /archive``：生成并落盘归档。"""
    return await build_archive(session, document)


def list_archives(document_id: int) -> list[str]:
    """列出该文档已有归档文件名（按时间戳倒序）。"""
    directory = storage.archives_dir(int(document_id))
    if not directory.is_dir():
        return []
    return sorted((entry.name for entry in directory.iterdir() if entry.suffix == ".zip"), reverse=True)


def resolve_archive(document_id: int, archive_name: str | None) -> Path:
    """定位归档文件（``archive_name`` 缺省取最新一份）；任何越界名都被拒绝。"""
    directory = storage.archives_dir(int(document_id))
    if archive_name:
        safe = storage.safe_zip_name(archive_name)
        if "/" in safe:
            raise UnsafeArchivePathError(
                f"archive_name='{archive_name}' 只允许单段文件名", detail={"archive_name": archive_name}
            )
        candidate = storage.safe_join(directory, safe)
    else:
        names = list_archives(int(document_id))
        if not names:
            raise ArchiveNotFoundError(
                f"阅读文档 {document_id} 尚无归档：请先调用 GET /api/v1/reader/documents/"
                f"{document_id}/archive 生成",
                detail={"document_id": int(document_id)},
            )
        candidate = storage.safe_join(directory, names[0])
    if not candidate.is_file():
        raise ArchiveNotFoundError(
            f"归档文件不存在：{candidate.name}（现有归档：{list_archives(document_id)[:5]}）",
            detail={"document_id": int(document_id), "archive_name": archive_name},
        )
    return candidate


async def restore_archive(
    session: AsyncSession,
    document: ReaderDocument,
    *,
    archive_name: str | None = None,
) -> dict[str, Any]:
    """从归档恢复（补写缺失文件 / 按 uid 升级批注；**不删除任何既有数据**）。"""
    path = resolve_archive(int(document.id), archive_name)
    payload = path.read_bytes()
    entries = storage.read_zip(payload)

    if DOCUMENT_NAME not in entries or ANNOTATIONS_NAME not in entries:
        raise ArchiveRestoreError(
            f"归档缺少必需条目（{DOCUMENT_NAME} / {ANNOTATIONS_NAME}），拒绝恢复",
            detail={"archive_name": path.name, "entries": sorted(entries)[:20]},
        )
    archived_document = storage.loads_json(entries[DOCUMENT_NAME])
    if not isinstance(archived_document, dict):
        raise ArchiveRestoreError(f"归档 {DOCUMENT_NAME} 不可解析", detail={"archive_name": path.name})
    archived_fingerprint = str(archived_document.get("fingerprint") or "")
    if archived_fingerprint != document.fingerprint:
        raise ArchiveRestoreError(
            "归档的原文指纹与当前阅读文档不一致：拒绝恢复（可能归档属于另一份原文）",
            detail={
                "archive_fingerprint": archived_fingerprint,
                "document_fingerprint": document.fingerprint,
                "archive_name": path.name,
            },
        )

    annotations_doc = storage.loads_json(entries[ANNOTATIONS_NAME])
    archived_annotations = (
        annotations_doc.get("items", [])
        if isinstance(annotations_doc, dict) and isinstance(annotations_doc.get("items"), list)
        else []
    )
    versions_doc = storage.loads_json(entries.get(VERSIONS_INDEX_NAME, b"{}"))
    archived_versions = (
        versions_doc.get("items", [])
        if isinstance(versions_doc, dict) and isinstance(versions_doc.get("items"), list)
        else []
    )

    report: dict[str, Any] = {
        "archive_name": path.name,
        "archive_created_at": (
            (storage.loads_json(entries[MANIFEST_NAME]) or {}).get("created_at")
            if MANIFEST_NAME in entries
            else None
        ),
        "files_written": [],
        "files_kept": [],
        "conflicts": [],
        "restored_versions": [],
        "restored_annotations": [],
        "skipped_newer": [],
        "state_restored": False,
        "note": "恢复只做补写与按 uid 升级；既有更新（revision 更高）的批注不会被覆盖",
    }

    # 1) 版本：既有行保留；缺文件补写；缺行按归档元数据重建（INSERT，不 UPDATE）
    existing_versions = await versions.list_versions(session, document)
    by_no = {version.version_no: version for version in existing_versions}
    by_kind = {version.kind: version for version in existing_versions}
    files_dir = storage.versions_dir(int(document.id))
    for meta in archived_versions:
        if not isinstance(meta, dict):
            continue
        kind = str(meta.get("kind") or "")
        version_no = meta.get("version_no")
        if not kind or not isinstance(version_no, int):
            report["conflicts"].append({"reason": "invalid_version_meta", "meta": meta})
            continue
        target = by_no.get(version_no) or by_kind.get(kind)
        pdf_entry = str(meta.get("pdf_entry") or "")
        index_entry = str(meta.get("index_entry") or "")
        pdf_payload = entries.get(storage.safe_zip_name(pdf_entry)) if pdf_entry else None
        index_payload = entries.get(storage.safe_zip_name(index_entry)) if index_entry else None

        if target is not None and target.kind != kind:
            # 行/号冲突：既有版本占用同一 version_no 但 kind 不同 → 不写、不覆盖，如实报告
            report["conflicts"].append(
                {
                    "reason": "version_slot_taken_by_other_kind",
                    "version_no": version_no,
                    "archived_kind": kind,
                    "existing_kind": target.kind,
                    "note": "既有版本占用该 version_no：保留既有记录，恢复跳过此版本",
                }
            )
            continue

        if target is None:
            stored_sha = storage.sha256_bytes(pdf_payload) if pdf_payload else None
            if pdf_payload is None:
                report["conflicts"].append(
                    {"reason": "version_pdf_missing_in_archive", "version_no": version_no, "kind": kind}
                )
                continue
            row = ReaderVersion(
                document_id=int(document.id),
                version_no=version_no,
                kind=kind,
                task_id=meta.get("task_id"),
                engine=meta.get("engine"),
                file_name=storage.version_file_name(version_no, kind, suffix=".pdf"),
                file_sha256=str(meta.get("file_sha256") or stored_sha),
                file_size_bytes=int(meta.get("file_size_bytes") or len(pdf_payload)),
                index_file_name=storage.version_file_name(version_no, kind, suffix=".index.json"),
                index_sha256=str(meta.get("index_sha256") or (storage.sha256_bytes(index_payload) if index_payload else "")),
                source_sha256=meta.get("source_sha256"),
                pdf_sha256=meta.get("pdf_sha256"),
                hash_verified=bool(meta.get("hash_verified")),
                page_map=meta.get("page_map") or {"mode": "unavailable", "note": "从归档恢复"},
                block_count=int(meta.get("block_count") or 0),
                layout_warnings=meta.get("layout_warnings") or [],
                manifest_snapshot=meta.get("manifest_snapshot"),
                content_fingerprint=str(meta.get("content_fingerprint") or document.payload_sha256),
            )
            session.add(row)
            await session.flush()
            storage.write_bytes_atomic(files_dir / row.file_name, pdf_payload)
            report["files_written"].append(row.file_name)
            if index_payload:
                storage.write_bytes_atomic(files_dir / row.index_file_name, index_payload)
                report["files_written"].append(row.index_file_name)
            report["restored_versions"].append(
                {"id": row.id, "version_no": version_no, "kind": kind, "rebuilt": True}
            )
            continue

        # 既有版本行：只补缺失文件，绝不覆盖已有文件
        for name, file_payload in ((target.file_name, pdf_payload), (target.index_file_name, index_payload)):
            if file_payload is None:
                continue
            destination = files_dir / name
            if not destination.is_file():
                storage.write_bytes_atomic(destination, file_payload)
                report["files_written"].append(name)
                report["restored_versions"].append(
                    {"id": target.id, "version_no": target.version_no, "kind": target.kind, "file": name}
                )
                continue
            if storage.sha256_file(destination) != storage.sha256_bytes(file_payload):
                report["conflicts"].append(
                    {
                        "reason": "existing_file_differs_kept",
                        "version_id": target.id,
                        "file": name,
                        "note": "既有文件与归档字节不同：保留既有文件（不静默覆盖）",
                    }
                )
            else:
                report["files_kept"].append(name)

    # 2) 阅读状态：仅在既有记录不存在或归档 revision 更高时写入
    state_doc = storage.loads_json(entries.get(STATE_NAME, b"{}"))
    archived_state = state_doc.get("state") if isinstance(state_doc, dict) else None
    if isinstance(archived_state, dict):
        current = (
            await session.execute(
                select(ReaderState).where(ReaderState.document_id == int(document.id))
            )
        ).scalar_one_or_none()
        archived_revision = int(archived_state.get("revision") or 0)
        if current is None:
            session.add(
                ReaderState(
                    document_id=int(document.id),
                    current_block=archived_state.get("current_block"),
                    offset=int(archived_state.get("offset") or 0),
                    mode=str(archived_state.get("mode") or "original"),
                    font_size=int(archived_state.get("font_size") or 16),
                    understood_blocks=archived_state.get("understood_blocks") or [],
                    favorite_terms=archived_state.get("favorite_terms") or [],
                    revision=max(1, archived_revision),
                )
            )
            report["state_restored"] = True
        elif archived_revision > int(current.revision or 0):
            current.current_block = archived_state.get("current_block")
            current.offset = int(archived_state.get("offset") or 0)
            current.mode = str(archived_state.get("mode") or current.mode)
            current.font_size = int(archived_state.get("font_size") or current.font_size)
            current.understood_blocks = archived_state.get("understood_blocks") or []
            current.favorite_terms = archived_state.get("favorite_terms") or []
            current.revision = archived_revision + 1
            report["state_restored"] = True
        else:
            report["state_restored"] = False

    # 3) 批注：按 uid 去重，revision 更高的一方取胜
    known_version_ids = {version.id for version in await versions.list_versions(session, document)}
    existing = {
        row.uid: row
        for row in (
            await session.execute(
                select(ReaderAnnotation).where(
                    ReaderAnnotation.document_id == int(document.id)
                )
            )
        ).scalars().all()
    }
    for item in archived_annotations:
        if not isinstance(item, dict):
            continue
        uid = str(item.get("uid") or "").strip()
        if not uid:
            report["conflicts"].append({"reason": "annotation_uid_missing", "item": item.get("id")})
            continue
        current = existing.get(uid)
        archived_revision = int(item.get("revision") or 1)
        if current is None:
            archived_version_id = item.get("version_id")
            if isinstance(archived_version_id, int) and archived_version_id not in known_version_ids:
                # 归档里的来源版本不在本库（可能未随归档恢复）→ 置空并如实记录，不留悬空外键
                report["conflicts"].append(
                    {
                        "reason": "annotation_source_version_missing",
                        "uid": uid,
                        "version_id": archived_version_id,
                    }
                )
                archived_version_id = None
            row = ReaderAnnotation(
                document_id=int(document.id),
                uid=uid,
                version_id=archived_version_id,
                block_id=item.get("block_id"),
                page=item.get("page"),
                bbox=item.get("bbox"),
                quote_text=str(item.get("quote_text") or ""),
                quote_sha256=str(item.get("quote_sha256") or ""),
                anchor_text=str(item.get("anchor_text") or ""),
                anchor_sha256=str(item.get("anchor_sha256") or ""),
                anchor_resolved=bool(item.get("anchor_resolved")),
                kind=str(item.get("kind") or "highlight"),
                note=item.get("note"),
                align_status=str(item.get("align_status") or "pending"),
                projections=item.get("projections") or [],
                content_fingerprint=str(item.get("content_fingerprint") or document.payload_sha256),
                revision=max(1, archived_revision),
            )
            session.add(row)
            report["restored_annotations"].append({"uid": uid, "created": True})
            continue
        if archived_revision > int(current.revision or 0):
            current.quote_text = str(item.get("quote_text") or current.quote_text)
            current.quote_sha256 = str(item.get("quote_sha256") or current.quote_sha256)
            current.anchor_text = str(item.get("anchor_text") or current.anchor_text)
            current.anchor_sha256 = str(item.get("anchor_sha256") or current.anchor_sha256)
            current.anchor_resolved = bool(item.get("anchor_resolved"))
            current.kind = str(item.get("kind") or current.kind)
            current.note = item.get("note")
            current.align_status = str(item.get("align_status") or current.align_status)
            current.projections = item.get("projections") or []
            current.block_id = item.get("block_id")
            current.page = item.get("page")
            current.bbox = item.get("bbox")
            current.revision = archived_revision + 1
            report["restored_annotations"].append({"uid": uid, "created": False, "revision": current.revision})
        else:
            report["skipped_newer"].append(
                {"uid": uid, "current_revision": current.revision, "archived_revision": archived_revision}
            )

    await session.commit()
    logger.info(
        "reader_archive_restored document_id=%s archive=%s versions=%d annotations=%d skipped=%d conflicts=%d",
        document.id,
        path.name,
        len(report["restored_versions"]),
        len(report["restored_annotations"]),
        len(report["skipped_newer"]),
        len(report["conflicts"]),
    )
    report["ok"] = True
    report["counts"] = {
        "restored_versions": len(report["restored_versions"]),
        "restored_annotations": len(report["restored_annotations"]),
        "skipped_newer": len(report["skipped_newer"]),
        "files_written": len(report["files_written"]),
        "conflicts": len(report["conflicts"]),
    }
    return report


__all__ = [
    "ANNOTATIONS_NAME",
    "ARCHIVE_SCHEMA_VERSION",
    "DOCUMENT_NAME",
    "MANIFEST_NAME",
    "STATE_NAME",
    "VERSIONS_INDEX_NAME",
    "build_archive",
    "export_archive",
    "list_archives",
    "resolve_archive",
    "restore_archive",
]
