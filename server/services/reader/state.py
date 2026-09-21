# Copyright 2026 SciLoop contributors
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
"""阅读状态：当前位置 / 模式 / 字号 / 已理解 block / 收藏术语（EasyPaper §4.4 ReadingState）。

口径
----
- 每个阅读文档一行（``reader_states.document_id`` UNIQUE），**读取时惰性创建默认行**；
- ``current_block`` 与 ``understood_blocks`` 必须落在该文档**真实存在**的稳定 block id 集合内
  → 否则 422 ``unknown_block``：阅读位置若指向不存在的 block，「刷新后恢复」就是假的；
- ``mode`` / ``font_size`` / ``offset`` 走受控值域与数据库 CHECK 双保险；
- 每次 PATCH ``revision += 1``，供前端判断本地副本是否过期（与批注同一套乐观锁思路）。
"""

from __future__ import annotations

import logging
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from db.models.reader import VERSION_KINDS, ReaderDocument, ReaderState
from services.reader import documents, parsing
from services.reader.errors import StateValidationError, UnknownBlockError

logger = logging.getLogger("sciloop.reader.state")

DEFAULT_MODE = "original"
DEFAULT_FONT_SIZE = 16
FONT_SIZE_MIN = 8
FONT_SIZE_MAX = 48
UNDERSTOOD_MAX = 5000
FAVORITE_TERMS_MAX = 500
FAVORITE_TERM_MAX_CHARS = 128


def to_api_dict(row: ReaderState) -> dict[str, Any]:
    return {
        "id": row.id,
        "document_id": row.document_id,
        "current_block": row.current_block,
        "offset": row.offset,
        "mode": row.mode,
        "font_size": row.font_size,
        "understood_blocks": row.understood_blocks,
        "favorite_terms": row.favorite_terms,
        "revision": row.revision,
        "created_at": row.created_at.isoformat()
        if hasattr(row.created_at, "isoformat")
        else row.created_at,
        "updated_at": row.updated_at.isoformat()
        if hasattr(row.updated_at, "isoformat")
        else row.updated_at,
        "note": "阅读位置以稳定 block id 记录；刷新后按 current_block + offset 恢复",
    }


async def get_or_create_state(session: AsyncSession, document: ReaderDocument) -> ReaderState:
    row = (
        await session.execute(
            select(ReaderState).where(ReaderState.document_id == int(document.id))
        )
    ).scalar_one_or_none()
    if row is not None:
        return row
    default_block = _default_block(document)
    row = ReaderState(
        document_id=int(document.id),
        current_block=default_block,
        offset=0,
        mode=DEFAULT_MODE,
        font_size=DEFAULT_FONT_SIZE,
        understood_blocks=[],
        favorite_terms=[],
        revision=1,
    )
    session.add(row)
    await session.flush()
    logger.info(
        "reader_state_created document_id=%s default_block=%s", document.id, default_block
    )
    return row


def _default_block(document: ReaderDocument) -> str | None:
    """默认位置 = 首个非 scan block（正文起点）；找不到就如实置 ``None``。"""
    for block in parsing.iter_blocks(document.document):
        if str(block.get("type")) != "scan" and block.get("source_text"):
            return str(block.get("id"))
    blocks = parsing.iter_blocks(document.document)
    return str(blocks[0]["id"]) if blocks else None


def _validate_block_id(document: ReaderDocument, block_id: str, *, field: str) -> str:
    text = str(block_id or "").strip()
    if not text:
        raise StateValidationError(f"{field} 不能为空字符串；如需清空请传 null")
    known = documents.block_ids(document)
    if text not in known:
        raise UnknownBlockError(
            f"{field}='{text}' 不在阅读文档 {document.id} 的稳定 block id 集合内",
            detail={"document_id": int(document.id), "block_id": text, "known_blocks": len(known)},
        )
    return text


def _validate_mode(mode: Any) -> str:
    text = str(mode or "").strip()
    if text not in VERSION_KINDS:
        raise StateValidationError(
            f"mode='{mode}' 非法", detail={"allowed": list(VERSION_KINDS)}
        )
    return text


def _validate_font_size(value: Any) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise StateValidationError(f"font_size 必须是整数，收到 {value!r}")
    if not FONT_SIZE_MIN <= value <= FONT_SIZE_MAX:
        raise StateValidationError(
            f"font_size={value} 越界（{FONT_SIZE_MIN}–{FONT_SIZE_MAX}）",
            detail={"min": FONT_SIZE_MIN, "max": FONT_SIZE_MAX},
        )
    return value


def _validate_offset(value: Any) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise StateValidationError(f"offset 必须是整数，收到 {value!r}")
    if value < 0:
        raise StateValidationError(f"offset={value} 不能为负")
    return value


def _validate_understood(document: ReaderDocument, value: Any) -> list[str]:
    if not isinstance(value, list):
        raise StateValidationError("understood_blocks 必须是字符串数组")
    if len(value) > UNDERSTOOD_MAX:
        raise StateValidationError(
            f"understood_blocks 长度 {len(value)} 超过上限 {UNDERSTOOD_MAX}",
            detail={"limit": UNDERSTOOD_MAX},
        )
    seen: list[str] = []
    for item in value:
        block_id = _validate_block_id(document, item, field="understood_blocks[]")
        if block_id not in seen:
            seen.append(block_id)
    return seen


def _validate_terms(value: Any) -> list[str]:
    if not isinstance(value, list):
        raise StateValidationError("favorite_terms 必须是字符串数组")
    if len(value) > FAVORITE_TERMS_MAX:
        raise StateValidationError(
            f"favorite_terms 长度 {len(value)} 超过上限 {FAVORITE_TERMS_MAX}",
            detail={"limit": FAVORITE_TERMS_MAX},
        )
    terms: list[str] = []
    for item in value:
        text = str(item or "").strip()
        if not text:
            raise StateValidationError("favorite_terms 不能包含空字符串")
        if len(text) > FAVORITE_TERM_MAX_CHARS:
            raise StateValidationError(
                f"favorite_terms 单项长度 {len(text)} 超过上限 {FAVORITE_TERM_MAX_CHARS}",
                detail={"limit": FAVORITE_TERM_MAX_CHARS},
            )
        if text not in terms:
            terms.append(text)
    return terms


async def patch_state(
    session: AsyncSession,
    document: ReaderDocument,
    *,
    fields: dict[str, Any],
) -> ReaderState:
    """按 ``fields`` 局部更新阅读状态（未出现的键不动；校验失败不落库）。"""
    row = await get_or_create_state(session, document)
    changed: list[str] = []

    if "current_block" in fields:
        value = fields["current_block"]
        if value is None:
            row.current_block = None
        else:
            row.current_block = _validate_block_id(document, value, field="current_block")
        changed.append("current_block")
    if "offset" in fields:
        row.offset = _validate_offset(fields["offset"])
        changed.append("offset")
    if "mode" in fields:
        row.mode = _validate_mode(fields["mode"])
        changed.append("mode")
    if "font_size" in fields:
        row.font_size = _validate_font_size(fields["font_size"])
        changed.append("font_size")
    if "understood_blocks" in fields:
        row.understood_blocks = _validate_understood(document, fields["understood_blocks"])
        changed.append("understood_blocks")
    if "favorite_terms" in fields:
        row.favorite_terms = _validate_terms(fields["favorite_terms"])
        changed.append("favorite_terms")

    if not changed:
        raise StateValidationError(
            "请求体未包含任何可修改字段"
            "（current_block / offset / mode / font_size / understood_blocks / favorite_terms）"
        )

    row.revision = int(row.revision or 1) + 1
    row.updated_at = datetime.now(UTC)
    await session.commit()
    await session.refresh(row)
    logger.info("reader_state_patched document_id=%s fields=%s", document.id, changed)
    return row


__all__ = [
    "DEFAULT_FONT_SIZE",
    "DEFAULT_MODE",
    "FONT_SIZE_MAX",
    "FONT_SIZE_MIN",
    "get_or_create_state",
    "patch_state",
    "to_api_dict",
]
