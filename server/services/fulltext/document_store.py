# Copyright 2026 SciLoop contributors
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
"""文档获取、版本化与落库编排（WP05-T1，附录 D.0）。

分级策略（照抄 §2.8，不自行发挥）：

1. **L1 HTML**：``ar5iv`` 优先，失败退 ``arXiv HTML5``；
2. **L2 PDF**：``pymupdf``，按坐标聚类恢复双栏阅读顺序；
3. **L3**：无文本层 → ``parse_status='unavailable'``；异常 → ``'failed'``。
   两种情况都必须写 ``parse_error``，且只允许摘要级证据。

版本化：``document_version = 源 URL + 内容 SHA-256 前 12 位``，
按 ``(paper_id, document_version)`` 天然去重 —— 同一内容重复解析直接复用，
内容变了则自然产生新版本记录（同一论文允许多版本）。
"""

from __future__ import annotations

import logging
from collections.abc import Iterable, Sequence
from dataclasses import dataclass, field
from typing import Any, Protocol

from services.fulltext.coverage import (
    classify_parse_status,
    compute_coverage,
    coverage_note,
    evidence_scope,
    min_coverage_threshold,
    spans_allowed,
)
from services.fulltext.html_parser import PARSER_VERSION as HTML_PARSER_VERSION
from services.fulltext.html_parser import parse_html
from services.fulltext.locator import build_spans
from services.fulltext.pdf_parser import (
    PARSER_VERSION as PDF_PARSER_VERSION,
)
from services.fulltext.pdf_parser import (
    NoTextLayerError,
    parse_pdf,
)
from services.fulltext.records import (
    FulltextResult,
    PaperDocumentRecord,
    PaperRef,
    PaperSpanRecord,
    ParseAttempt,
    ParsedDocument,
    build_document_version,
    sha256_hex,
)
from services.fulltext.repository import DocumentRepository
from services.fulltext.text_cache import TextCache, default_text_cache

logger = logging.getLogger(__name__)

USER_AGENT = "SciLoop/0.1 (fulltext parser; +https://arxiv.org)"
DEFAULT_TIMEOUT_SECONDS = 30
#: 单篇文档最大下载体积（防止误下载超大文件）
MAX_CONTENT_BYTES = 80 * 1024 * 1024
#: 段落进入 paper_spans 的最小长度（标题不受限）
DEFAULT_MIN_SPAN_CHARS = 40


class PaperNotFoundError(LookupError):
    """``papers`` 表中没有该 ``paper_id``。"""


@dataclass(slots=True)
class FetchResult:
    """一次抓取的结果（含 HTTP 状态，失败原因必须可解释）。"""

    url: str
    status: int | None = None
    content: bytes | None = None
    content_type: str | None = None
    error: str | None = None

    @property
    def ok(self) -> bool:
        return self.error is None and self.status == 200 and bool(self.content)

    def describe(self) -> str:
        if self.error:
            return self.error
        if self.status != 200:
            return f"HTTP {self.status}"
        if not self.content:
            return "响应为空"
        return "ok"


class Fetcher(Protocol):
    """抓取接口（便于在单测中换成离线实现）。"""

    async def fetch(self, url: str) -> FetchResult: ...


class HttpxFetcher:
    """生产抓取实现：httpx + 超时 + 体积上限。"""

    def __init__(self, *, timeout: float | None = None) -> None:
        self.timeout = float(timeout or self._settings_timeout())

    @staticmethod
    def _settings_timeout() -> float:
        try:  # pragma: no cover - 依赖运行环境
            from core.config import settings

            return float(getattr(settings, "source_fetch_timeout_seconds", 20) or 20)
        except Exception:  # pragma: no cover
            return float(DEFAULT_TIMEOUT_SECONDS)

    async def fetch(self, url: str) -> FetchResult:
        try:
            import httpx
        except ImportError as exc:  # pragma: no cover
            return FetchResult(url=url, error=f"缺少 httpx 依赖：{exc}")
        try:
            async with httpx.AsyncClient(
                timeout=self.timeout,
                follow_redirects=True,
                headers={"User-Agent": USER_AGENT},
            ) as client:
                response = await client.get(url)
                content = response.content
                if content and len(content) > MAX_CONTENT_BYTES:
                    return FetchResult(
                        url=str(response.url),
                        status=response.status_code,
                        error=f"内容超过上限 {MAX_CONTENT_BYTES} 字节",
                    )
                return FetchResult(
                    url=str(response.url),
                    status=response.status_code,
                    content=content,
                    content_type=response.headers.get("content-type"),
                )
        except Exception as exc:  # 网络异常必须留痕，不能静默
            return FetchResult(url=url, error=f"{type(exc).__name__}: {exc}")


class LocalFileFetcher:
    """离线抓取实现：把 URL 映射到本地文件（测试与离线验证用）。"""

    def __init__(self, mapping: dict[str, str | bytes] | None = None, *, default: bytes | None = None):
        self.mapping = dict(mapping or {})
        self.default = default

    async def fetch(self, url: str) -> FetchResult:
        payload = self.mapping.get(url, self.default)
        if payload is None:
            return FetchResult(url=url, status=404, error="离线抓取器未命中该 URL")
        if isinstance(payload, str):
            payload = payload.encode("utf-8")
        return FetchResult(
            url=url,
            status=200,
            content=payload,
            content_type="text/html" if payload[:1] in (b"<", b"\n") else "application/pdf",
        )


def _parser_version_for(parser: str) -> str:
    """候选源 → 解析器版本。

    **必须在"解析前"就能算出来**：它要进 ``document_version``（用于查"这份解析是否已存在"），
    而那个判断发生在真正解析之前。
    """
    return PDF_PARSER_VERSION if parser == "pymupdf" else HTML_PARSER_VERSION


@dataclass(frozen=True, slots=True)
class SourceCandidate:
    """一个候选来源（按顺序尝试，前一个失败才试下一个）。"""

    source_type: str
    url: str
    parser: str

    def describe(self) -> str:
        return f"{self.source_type}:{self.parser} {self.url}"


def document_sources(paper_ref: PaperRef, *, html_first: bool = True) -> list[SourceCandidate]:
    """按 L1→L2 顺序列出候选来源（去重且保持顺序）。"""
    arxiv_id = paper_ref.arxiv_id
    version = paper_ref.arxiv_version

    html_candidates: list[SourceCandidate] = []
    pdf_candidates: list[SourceCandidate] = []
    if arxiv_id:
        if version:
            html_candidates.append(
                SourceCandidate(
                    "html", f"https://ar5iv.labs.arxiv.org/html/{arxiv_id}{version}", "ar5iv_html"
                )
            )
        html_candidates.append(
            SourceCandidate("html", f"https://ar5iv.labs.arxiv.org/html/{arxiv_id}", "ar5iv_html")
        )
        if version:
            html_candidates.append(
                SourceCandidate("html", f"https://arxiv.org/html/{arxiv_id}{version}", "arxiv_html")
            )
        html_candidates.append(
            SourceCandidate("html", f"https://arxiv.org/html/{arxiv_id}", "arxiv_html")
        )
        if paper_ref.pdf_url:
            pdf_candidates.append(SourceCandidate("pdf", paper_ref.pdf_url, "pymupdf"))
            # ``papers.pdf_url`` 通常带版本号（``…/pdf/2401.00001v2``）。arXiv 对个别
            # 版本号返回 406（实测 2508.00117v2 / 2508.00135v2），而**去版本号**的同一
            # 地址可正常下载，因此再挂一个同源候选。只在同一 arxiv.org 主机下追加，
            # 避免把第三方全文混进来；最终采用哪个 URL 会如实写进 source_url。
            unversioned_pdf = f"https://arxiv.org/pdf/{arxiv_id}"
            if "arxiv.org" in paper_ref.pdf_url and unversioned_pdf != paper_ref.pdf_url:
                pdf_candidates.append(SourceCandidate("pdf", unversioned_pdf, "pymupdf"))
        else:
            if version:
                pdf_candidates.append(
                    SourceCandidate("pdf", f"https://arxiv.org/pdf/{arxiv_id}{version}", "pymupdf")
                )
            pdf_candidates.append(
                SourceCandidate("pdf", f"https://arxiv.org/pdf/{arxiv_id}", "pymupdf")
            )
    elif paper_ref.pdf_url:
        pdf_candidates.append(SourceCandidate("pdf", paper_ref.pdf_url, "pymupdf"))

    ordered = html_candidates + pdf_candidates if html_first else pdf_candidates + html_candidates
    seen: set[str] = set()
    unique: list[SourceCandidate] = []
    for candidate in ordered:
        if candidate.url in seen:
            continue
        seen.add(candidate.url)
        unique.append(candidate)
    return unique


@dataclass(slots=True)
class DocumentStore:
    """获取 + 解析 + 落库的编排入口（WP05-T1/T5）。"""

    repository: DocumentRepository
    fetcher: Fetcher | None = None
    text_cache: TextCache | None = None
    html_first: bool = True
    max_pages: int | None = None
    min_coverage: float | None = None
    min_span_chars: int = DEFAULT_MIN_SPAN_CHARS
    max_spans: int = 0
    _attempts: list[ParseAttempt] = field(default_factory=list, init=False, repr=False)

    def __post_init__(self) -> None:
        if self.fetcher is None:
            self.fetcher = HttpxFetcher()
        if self.text_cache is None:
            self.text_cache = default_text_cache()
        if self.min_coverage is None:
            self.min_coverage = min_coverage_threshold()

    # --------------------------------------------------------------- 接口
    async def ensure_document(self, paper_id: int, *, force: bool = False) -> PaperDocumentRecord:
        """``ensure_document(paper_id) -> PaperDocumentRecord``（WP05 对外契约）。"""
        result = await self.ensure_fulltext(paper_id, force=force)
        return result.document

    async def ensure_fulltext(self, paper_id: int, *, force: bool = False) -> FulltextResult:
        """保证存在 ``paper_documents`` 记录，并生成允许的 ``paper_spans``。"""
        paper_ref = await self.repository.get_paper(int(paper_id))
        if paper_ref is None:
            raise PaperNotFoundError(f"papers 表中不存在 paper_id={paper_id}")

        if not force:
            reusable = await self._reusable_result(paper_ref)
            if reusable is not None:
                return reusable

        attempts: list[ParseAttempt] = []
        collected: list[FulltextResult] = []
        unexpected_error: str | None = None

        for candidate in document_sources(paper_ref, html_first=self.html_first):
            fetched = await self._fetcher().fetch(candidate.url)
            if not fetched.ok:
                attempts.append(
                    ParseAttempt(candidate.source_type, candidate.url, True, False, fetched.describe())
                )
                continue

            content = fetched.content or b""
            content_sha256 = sha256_hex(content)
            source_url = fetched.url or candidate.url
            document_version = build_document_version(
                source_url,
                content_sha256,
                candidate.parser,
                _parser_version_for(candidate.parser),
            )

            if not force:
                existing = await self.repository.get_document(paper_ref.id, document_version)
                if existing is not None:
                    spans = await self.repository.list_spans(
                        paper_ref.id, document_version=document_version
                    )
                    attempts.append(
                        ParseAttempt(
                            candidate.source_type,
                            source_url,
                            True,
                            True,
                            "内容哈希命中已有 document_version，直接复用",
                        )
                    )
                    return FulltextResult(
                        document=existing,
                        spans=spans,
                        full_text=self._cached_text(paper_ref.id, document_version),
                        reused=True,
                        attempts=attempts,
                        truncated=False,
                    )

            try:
                parsed = self._parse(candidate, content, source_url, content_sha256)
            except NoTextLayerError as exc:
                attempts.append(
                    ParseAttempt(candidate.source_type, source_url, True, False, str(exc))
                )
                unavailable = await self._store_unavailable(
                    paper_ref,
                    source_url=source_url,
                    content_sha256=content_sha256,
                    source_type=candidate.source_type,
                    parser=candidate.parser,
                    parse_status="unavailable",
                    parse_error=str(exc),
                    page_count=getattr(exc, "page_count", None),
                )
                collected.append(
                    FulltextResult(document=unavailable, attempts=list(attempts))
                )
                continue
            except ValueError as exc:
                # 抓到的不是有效全文（如 ar5iv 的 404 页面），换下一级来源
                attempts.append(
                    ParseAttempt(candidate.source_type, source_url, True, False, str(exc))
                )
                continue
            except Exception as exc:  # 解析器内部异常：留痕为 failed，禁止静默
                message = f"{type(exc).__name__}: {exc}"
                unexpected_error = message
                attempts.append(ParseAttempt(candidate.source_type, source_url, True, False, message))
                logger.warning("fulltext parse failed paper_id=%s url=%s: %s", paper_id, source_url, message)
                continue

            result = await self._persist(paper_ref, parsed, attempts=list(attempts))
            attempts.append(
                ParseAttempt(
                    candidate.source_type,
                    source_url,
                    True,
                    True,
                    f"parse_status={result.document.parse_status} coverage={result.document.coverage}",
                )
            )
            if result.document.parse_status == "ok":
                return result
            collected.append(result)

        if collected:
            best = max(collected, key=lambda item: item.document.coverage or 0.0)
            best.attempts = list(attempts)
            return best

        # 全部候选都拿不到可用全文 → 摘要级兜底记录（必须写明失败原因）
        fallback = await self._store_abstract_only(paper_ref, attempts, unexpected_error)
        return FulltextResult(document=fallback, attempts=attempts)

    async def list_spans(
        self,
        paper_id: int,
        *,
        section: str | None = None,
        document_version: str | None = None,
    ) -> list[PaperSpanRecord]:
        """``list_spans(paper_id, section=None) -> [PaperSpanRecord]``。"""
        return await self.repository.list_spans(
            int(paper_id), document_version=document_version, section=section
        )

    # --------------------------------------------------------- 解析与落库
    def _fetcher(self) -> Fetcher:
        assert self.fetcher is not None
        return self.fetcher

    def _cache(self) -> TextCache:
        assert self.text_cache is not None
        return self.text_cache

    def _cached_text(self, paper_id: int, document_version: str) -> str:
        return self._cache().get(paper_id, document_version) or ""

    def _parse(
        self,
        candidate: SourceCandidate,
        content: bytes,
        source_url: str,
        content_sha256: str,
    ) -> ParsedDocument:
        if candidate.source_type == "html":
            return parse_html(
                content,
                source_url=source_url,
                content_sha256=content_sha256,
                parser=candidate.parser,
                parser_version=HTML_PARSER_VERSION,
            )
        return parse_pdf(
            content,
            source_url=source_url,
            content_sha256=content_sha256,
            max_pages=self.max_pages,
            parser=candidate.parser,
            parser_version=PDF_PARSER_VERSION,
        )

    async def parse_bytes(
        self,
        paper_ref: PaperRef,
        content: bytes,
        *,
        source_url: str,
        source_type: str,
        parser: str | None = None,
        force: bool = False,
    ) -> FulltextResult:
        """直接对给定字节做解析并落库（离线验证 / 重放使用）。"""
        content_sha256 = sha256_hex(content)
        candidate = SourceCandidate(
            source_type=source_type,
            url=source_url,
            parser=parser or ("ar5iv_html" if source_type == "html" else "pymupdf"),
        )
        try:
            parsed = self._parse(candidate, content, source_url, content_sha256)
        except NoTextLayerError as exc:
            # 注意：``_store_unavailable`` 是协程，必须 await；漏 await 会把
            # coroutine 当成 PaperDocumentRecord 返回（RuntimeWarning + 空结果）。
            record = await self._store_unavailable(
                paper_ref,
                source_url=source_url,
                content_sha256=content_sha256,
                source_type=source_type,
                parser=candidate.parser,
                parse_status="unavailable",
                parse_error=str(exc),
                page_count=getattr(exc, "page_count", None),
            )
            return FulltextResult(document=record)
        return await self._persist(paper_ref, parsed, attempts=[])

    async def _persist(
        self,
        paper_ref: PaperRef,
        parsed: ParsedDocument,
        *,
        attempts: list[ParseAttempt],
    ) -> FulltextResult:
        char_count = parsed.char_count or len(parsed.full_text)
        locatable_chars = parsed.locatable_chars
        coverage = compute_coverage(locatable_chars, char_count)
        status = classify_parse_status(
            char_count=char_count,
            locatable_chars=locatable_chars,
            coverage=coverage,
            min_coverage=self.min_coverage,
        )
        document_version = parsed.document_version

        record = PaperDocumentRecord(
            paper_id=int(paper_ref.id),
            document_version=document_version,
            source_type=parsed.source_type,
            source_url=parsed.source_url,
            parser=parsed.parser,
            parser_version=parsed.parser_version,
            page_count=parsed.page_count,
            text_sha256=parsed.content_sha256,
            char_count=char_count,
            locatable_chars=locatable_chars,
            coverage=coverage,
            parse_status=status,  # type: ignore[arg-type]
            parse_error=None,
        )
        stored = await self.repository.upsert_document(record)

        allowed = spans_allowed(status, coverage, min_coverage=self.min_coverage)
        spans: list[PaperSpanRecord] = []
        if allowed:
            spans = build_spans(
                paper_id=int(paper_ref.id),
                document_version=document_version,
                blocks=parsed.blocks,
                min_paragraph_chars=self.min_span_chars,
                max_spans=self.max_spans,
            )
        # 不允许正文 span 时必须清空历史片段，避免残留过期证据
        await self.repository.replace_spans(int(paper_ref.id), document_version, spans)
        if parsed.full_text:
            self._cache().put(int(paper_ref.id), document_version, parsed.full_text)

        logger.info(
            "fulltext parsed paper_id=%s version=%s status=%s coverage=%.3f spans=%d",
            paper_ref.id,
            document_version,
            status,
            coverage,
            len(spans),
        )
        return FulltextResult(
            document=stored,
            spans=spans,
            full_text=parsed.full_text,
            reused=False,
            attempts=list(attempts),
            warnings=list(parsed.warnings),
            truncated=parsed.truncated,
        )

    async def _store_unavailable(
        self,
        paper_ref: PaperRef,
        *,
        source_url: str,
        content_sha256: str,
        source_type: str,
        parser: str,
        parse_status: str,
        parse_error: str,
        page_count: int | None,
    ) -> PaperDocumentRecord:
        document_version = build_document_version(
            source_url,
            content_sha256,
            parser,
            PDF_PARSER_VERSION if source_type == "pdf" else HTML_PARSER_VERSION,
        )
        record = PaperDocumentRecord(
            paper_id=int(paper_ref.id),
            document_version=document_version,
            source_type=source_type,
            source_url=source_url,
            parser=parser,
            parser_version=PDF_PARSER_VERSION if source_type == "pdf" else HTML_PARSER_VERSION,
            page_count=page_count,
            text_sha256=content_sha256,
            char_count=0,
            locatable_chars=0,
            coverage=0.0,
            parse_status=parse_status,  # type: ignore[arg-type]
            parse_error=parse_error,
        )
        stored = await self.repository.upsert_document(record)
        await self.repository.replace_spans(int(paper_ref.id), document_version, [])
        return stored

    async def _store_abstract_only(
        self,
        paper_ref: PaperRef,
        attempts: Sequence[ParseAttempt],
        unexpected_error: str | None,
    ) -> PaperDocumentRecord:
        abstract = paper_ref.abstract or ""
        payload = abstract.encode("utf-8")
        content_sha256 = sha256_hex(payload) if payload else sha256_hex(b"")
        source_url = paper_ref.canonical_url()
        lines = [attempt.describe() for attempt in attempts] or ["没有任何可用来源"]
        if unexpected_error:
            lines.append(f"异常：{unexpected_error}")
        parse_error = "全文不可用，仅摘要级证据可用；尝试记录：" + " | ".join(lines)
        status = "failed" if unexpected_error else "unavailable"

        record = PaperDocumentRecord(
            paper_id=int(paper_ref.id),
            document_version=build_document_version(source_url, content_sha256, "none", "0"),
            source_type="abstract_only",
            source_url=source_url,
            parser="none",
            parser_version="0",
            page_count=None,
            text_sha256=content_sha256,
            char_count=len(abstract),
            locatable_chars=0,
            coverage=0.0,
            parse_status=status,  # type: ignore[arg-type]
            parse_error=parse_error,
        )
        stored = await self.repository.upsert_document(record)
        await self.repository.replace_spans(
            int(paper_ref.id), stored.document_version, []
        )
        return stored

    # ------------------------------------------------------------ 复用
    async def _reusable_result(self, paper_ref: PaperRef) -> FulltextResult | None:
        documents = await self.repository.list_documents(paper_ref.id)
        eligible = [
            doc
            for doc in documents
            if spans_allowed(doc.parse_status, doc.coverage, min_coverage=self.min_coverage)
        ]
        if not eligible:
            return None
        best = max(eligible, key=lambda doc: doc.coverage or 0.0)
        spans = await self.repository.list_spans(
            paper_ref.id, document_version=best.document_version
        )
        return FulltextResult(
            document=best,
            spans=spans,
            full_text=self._cached_text(paper_ref.id, best.document_version),
            reused=True,
            warnings=["复用已有 paper_documents 记录（parse_status=ok 且 coverage 达标）"],
        )


def pick_primary_document(
    documents: Iterable[PaperDocumentRecord],
) -> PaperDocumentRecord | None:
    """从同一论文的多版本记录里挑"最能支撑证据"的那条。

    优先级：``spans_allowed`` 且 coverage 最高 > coverage 最高 > 最近一条。
    """
    docs = list(documents)
    if not docs:
        return None
    eligible = [doc for doc in docs if doc.spans_allowed]
    if eligible:
        return max(eligible, key=lambda doc: doc.coverage or 0.0)
    return max(docs, key=lambda doc: doc.coverage or 0.0)


def summarize_documents(documents: Iterable[PaperDocumentRecord]) -> dict[str, Any]:
    """给论文库/详情页用的 ``fulltext`` 摘要（附录 B.1 ``fulltext`` 字段口径）。"""
    primary = pick_primary_document(documents)
    if primary is None:
        return {
            "parse_status": None,
            "coverage": None,
            "document_version": None,
            "parser": None,
            "spans_allowed": False,
            "evidence_scope": "abstract_only",
            "coverage_note": "尚未解析全文，证据覆盖范围：仅摘要",
        }
    return {
        "parse_status": primary.parse_status,
        "coverage": primary.coverage,
        "document_version": primary.document_version,
        "parser": primary.parser,
        "page_count": primary.page_count,
        "char_count": primary.char_count,
        "locatable_chars": primary.locatable_chars,
        "spans_allowed": primary.spans_allowed,
        "evidence_scope": evidence_scope(primary.parse_status, primary.coverage),
        "coverage_note": coverage_note(primary.parse_status, primary.coverage),
    }
