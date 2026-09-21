# Copyright 2026 SciLoop contributors
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
"""持久化层（WP05-T1 / T5 的落库部分）。

分成三块，保证"解析算法可脱离数据库单测"：

- ``DocumentRepository``：接口（Protocol），解析核心只依赖它；
- ``InMemoryDocumentRepository``：纯内存实现，单测与离线演示用；
- ``SqlDocumentRepository``：SQLAlchemy 实现，读写 WP01 的
  ``paper_documents`` / ``paper_spans`` / ``papers``（附录 A.2）。

``SqlDocumentRepository`` 同时兼容 ``AsyncSession`` 与同步 ``Session``
（内部用 ``inspect.isawaitable`` 兼容），因此 FastAPI 路由、批量任务、
脚本三种调用方式共用一套代码。每次写入自行提交 —— 解析状态绝不允许
因为调用方忘记 commit 而丢失。
"""

from __future__ import annotations

import inspect
from collections.abc import Iterable, Sequence
from typing import Any, Protocol, runtime_checkable

from services.fulltext.records import (
    PaperDocumentRecord,
    PaperRef,
    PaperSpanRecord,
)

#: 视为"已解析完成"的状态
DONE_STATUSES: frozenset[str] = frozenset({"ok"})


@runtime_checkable
class DocumentRepository(Protocol):
    """解析服务所需的最小持久化接口。"""

    async def get_paper(self, paper_id: int) -> PaperRef | None: ...

    async def list_documents(self, paper_id: int) -> list[PaperDocumentRecord]: ...

    async def get_document(
        self, paper_id: int, document_version: str
    ) -> PaperDocumentRecord | None: ...

    async def upsert_document(self, record: PaperDocumentRecord) -> PaperDocumentRecord: ...

    async def replace_spans(
        self,
        paper_id: int,
        document_version: str,
        spans: Sequence[PaperSpanRecord],
    ) -> int: ...

    async def list_spans(
        self,
        paper_id: int,
        *,
        document_version: str | None = None,
        section: str | None = None,
    ) -> list[PaperSpanRecord]: ...

    async def list_paper_ids(
        self,
        *,
        limit: int | None = None,
        offset: int = 0,
        only_pending: bool = True,
        retry_statuses: Iterable[str] = (),
    ) -> list[int]: ...


async def _maybe_await(value: Any) -> Any:
    if inspect.isawaitable(value):
        return await value
    return value


class InMemoryDocumentRepository:
    """内存实现（单测 / 离线回放）。"""

    def __init__(
        self,
        papers: Iterable[PaperRef] = (),
        documents: Iterable[PaperDocumentRecord] = (),
        spans: Iterable[PaperSpanRecord] = (),
    ) -> None:
        self.papers: dict[int, PaperRef] = {paper.id: paper for paper in papers}
        self.documents: dict[tuple[int, str], PaperDocumentRecord] = {
            (doc.paper_id, doc.document_version): doc for doc in documents
        }
        self.spans: dict[tuple[int, str], list[PaperSpanRecord]] = {}
        for span in spans:
            self.spans.setdefault((span.paper_id, span.document_version), []).append(span)
        self._next_document_id = 1
        self._next_span_id = 1

    async def get_paper(self, paper_id: int) -> PaperRef | None:
        return self.papers.get(int(paper_id))

    async def list_documents(self, paper_id: int) -> list[PaperDocumentRecord]:
        return [doc for (pid, _v), doc in self.documents.items() if pid == int(paper_id)]

    async def get_document(
        self, paper_id: int, document_version: str
    ) -> PaperDocumentRecord | None:
        return self.documents.get((int(paper_id), str(document_version)))

    async def upsert_document(self, record: PaperDocumentRecord) -> PaperDocumentRecord:
        key = (int(record.paper_id), str(record.document_version))
        existing = self.documents.get(key)
        if existing is not None:
            record.id = existing.id
        else:
            record.id = self._next_document_id
            self._next_document_id += 1
        self.documents[key] = record
        return record

    async def replace_spans(
        self,
        paper_id: int,
        document_version: str,
        spans: Sequence[PaperSpanRecord],
    ) -> int:
        key = (int(paper_id), str(document_version))
        self.spans[key] = []
        for span in spans:
            span.id = self._next_span_id
            self._next_span_id += 1
            self.spans[key].append(span)
        return len(self.spans[key])

    async def list_spans(
        self,
        paper_id: int,
        *,
        document_version: str | None = None,
        section: str | None = None,
    ) -> list[PaperSpanRecord]:
        result: list[PaperSpanRecord] = []
        for (pid, version), rows in self.spans.items():
            if pid != int(paper_id):
                continue
            if document_version is not None and version != document_version:
                continue
            result.extend(rows)
        if section:
            result = [row for row in result if row.section_name == section]
        result.sort(key=lambda row: (row.document_version, row.char_start))
        return result

    async def list_paper_ids(
        self,
        *,
        limit: int | None = None,
        offset: int = 0,
        only_pending: bool = True,
        retry_statuses: Iterable[str] = (),
    ) -> list[int]:
        retry = {str(s) for s in retry_statuses}
        candidates: list[int] = []
        for paper_id in sorted(self.papers):
            docs = await self.list_documents(paper_id)
            has_done = any(doc.parse_status in DONE_STATUSES for doc in docs)
            has_retryable = any(doc.parse_status in retry for doc in docs)
            if only_pending and has_done:
                continue
            if retry and not has_retryable:
                continue
            candidates.append(paper_id)
        window = candidates[offset:]
        return window[:limit] if limit else window


class SqlDocumentRepository:
    """SQLAlchemy 实现，直接读写 WP01 的 ORM 模型。"""

    def __init__(self, session: Any, *, commit: bool = True) -> None:
        self.session = session
        self._commit_enabled = commit
        self._models = self._load_models()

    @staticmethod
    def _load_models() -> dict[str, Any]:
        from db.models.paper import Paper, PaperDocument, PaperSpan

        return {"Paper": Paper, "PaperDocument": PaperDocument, "PaperSpan": PaperSpan}

    # -- 内部工具 --------------------------------------------------------
    async def _execute(self, statement: Any) -> Any:
        return await _maybe_await(self.session.execute(statement))

    async def _commit(self) -> None:
        if not self._commit_enabled:
            return
        await _maybe_await(self.session.commit())

    # -- 读 --------------------------------------------------------------
    async def get_paper(self, paper_id: int) -> PaperRef | None:
        from sqlalchemy import select

        Paper = self._models["Paper"]
        result = await self._execute(select(Paper).where(Paper.id == int(paper_id)))
        row = result.scalar_one_or_none()
        if row is None:
            return None
        return PaperRef(
            id=int(row.id),
            source=row.source or "",
            external_id=row.external_id or "",
            title=row.title or "",
            abstract=row.abstract,
            pdf_url=row.pdf_url,
            doi=row.doi,
        )

    async def list_documents(self, paper_id: int) -> list[PaperDocumentRecord]:
        from sqlalchemy import select

        PaperDocument = self._models["PaperDocument"]
        result = await self._execute(
            select(PaperDocument)
            .where(PaperDocument.paper_id == int(paper_id))
            .order_by(PaperDocument.created_at.desc(), PaperDocument.id.desc())
        )
        return [_document_from_orm(row) for row in result.scalars().all()]

    async def get_document(
        self, paper_id: int, document_version: str
    ) -> PaperDocumentRecord | None:
        from sqlalchemy import select

        PaperDocument = self._models["PaperDocument"]
        result = await self._execute(
            select(PaperDocument).where(
                PaperDocument.paper_id == int(paper_id),
                PaperDocument.document_version == str(document_version),
            )
        )
        row = result.scalar_one_or_none()
        return _document_from_orm(row) if row is not None else None

    async def list_spans(
        self,
        paper_id: int,
        *,
        document_version: str | None = None,
        section: str | None = None,
        limit: int | None = None,
        offset: int = 0,
    ) -> list[PaperSpanRecord]:
        from sqlalchemy import select

        PaperSpan = self._models["PaperSpan"]
        statement = select(PaperSpan).where(PaperSpan.paper_id == int(paper_id))
        if document_version:
            statement = statement.where(PaperSpan.document_version == document_version)
        if section:
            statement = statement.where(PaperSpan.section_name == section)
        statement = statement.order_by(PaperSpan.document_version, PaperSpan.char_start)
        if offset:
            statement = statement.offset(int(offset))
        if limit:
            statement = statement.limit(int(limit))
        result = await self._execute(statement)
        return [_span_from_orm(row) for row in result.scalars().all()]

    # -- 写 --------------------------------------------------------------
    async def upsert_document(self, record: PaperDocumentRecord) -> PaperDocumentRecord:
        PaperDocument = self._models["PaperDocument"]
        existing = await self.get_document(record.paper_id, record.document_version)
        values = _document_values(record)
        if existing is None:
            row = PaperDocument(**values)
            self.session.add(row)
            await _maybe_await(self.session.flush())
            record.id = int(row.id)
        else:
            statement = (
                PaperDocument.__table__.update()
                .where(PaperDocument.paper_id == int(record.paper_id))
                .where(PaperDocument.document_version == str(record.document_version))
                .values(**values)
            )
            await self._execute(statement)
            record.id = existing.id
        await self._commit()
        return record

    async def replace_spans(
        self,
        paper_id: int,
        document_version: str,
        spans: Sequence[PaperSpanRecord],
    ) -> int:
        from sqlalchemy import delete

        PaperSpan = self._models["PaperSpan"]
        await self._execute(
            delete(PaperSpan)
            .where(PaperSpan.paper_id == int(paper_id))
            .where(PaperSpan.document_version == str(document_version))
        )
        if spans:
            self.session.add_all([PaperSpan(**_span_values(span)) for span in spans])
            await _maybe_await(self.session.flush())
        await self._commit()
        return len(spans)

    # -- 选paper ---------------------------------------------------------
    async def list_paper_ids(
        self,
        *,
        limit: int | None = None,
        offset: int = 0,
        only_pending: bool = True,
        retry_statuses: Iterable[str] = (),
    ) -> list[int]:
        from sqlalchemy import select

        Paper = self._models["Paper"]
        PaperDocument = self._models["PaperDocument"]
        retry = {str(status) for status in retry_statuses}

        result = await self._execute(select(Paper.id).order_by(Paper.id))
        paper_ids = [int(row[0]) for row in result.all()]
        if not paper_ids:
            return []

        result = await self._execute(
            select(PaperDocument.paper_id, PaperDocument.parse_status)
        )
        statuses: dict[int, set[str]] = {}
        for paper_id, status in result.all():
            statuses.setdefault(int(paper_id), set()).add(str(status))

        candidates: list[int] = []
        for paper_id in paper_ids:
            seen = statuses.get(paper_id, set())
            if only_pending and seen & DONE_STATUSES:
                continue
            if retry and not (seen & retry):
                continue
            candidates.append(paper_id)
        window = candidates[int(offset) :]
        return window[:limit] if limit else window


# --------------------------------------------------------------------------- #
def _document_from_orm(row: Any) -> PaperDocumentRecord:
    return PaperDocumentRecord(
        id=int(row.id),
        paper_id=int(row.paper_id),
        document_version=str(row.document_version),
        source_type=str(row.source_type),
        source_url=str(row.source_url),
        parser=str(row.parser),
        parser_version=str(row.parser_version),
        page_count=row.page_count,
        text_sha256=str(row.text_sha256),
        char_count=row.char_count,
        locatable_chars=row.locatable_chars,
        coverage=float(row.coverage) if row.coverage is not None else None,
        parse_status=str(row.parse_status),
        parse_error=row.parse_error,
        parsed_at=row.parsed_at,
        created_at=row.created_at,
    )


def _document_values(record: PaperDocumentRecord) -> dict[str, Any]:
    return {
        "paper_id": int(record.paper_id),
        "document_version": str(record.document_version),
        "source_type": str(record.source_type),
        "source_url": str(record.source_url),
        "parser": str(record.parser),
        "parser_version": str(record.parser_version),
        "page_count": record.page_count,
        "text_sha256": str(record.text_sha256),
        "char_count": record.char_count,
        "locatable_chars": record.locatable_chars,
        "coverage": record.coverage,
        "parse_status": str(record.parse_status),
        "parse_error": record.parse_error,
    }


def _span_from_orm(row: Any) -> PaperSpanRecord:
    return PaperSpanRecord(
        id=int(row.id),
        paper_id=int(row.paper_id),
        document_version=str(row.document_version),
        section_name=row.section_name,
        page_number=row.page_number,
        bbox=row.bbox,
        char_start=int(row.char_start),
        char_end=int(row.char_end),
        quote_text=str(row.quote_text),
        quote_sha256=str(row.quote_sha256),
        created_at=row.created_at,
    )


def _span_values(span: PaperSpanRecord) -> dict[str, Any]:
    return {
        "paper_id": int(span.paper_id),
        "document_version": str(span.document_version),
        "section_name": span.section_name,
        "page_number": span.page_number,
        "bbox": span.bbox,
        "char_start": int(span.char_start),
        "char_end": int(span.char_end),
        "quote_text": span.quote_text,
        "quote_sha256": span.quote_sha256,
    }
