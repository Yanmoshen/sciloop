# Copyright 2026 SciLoop contributors
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
"""全文解析的纯数据结构与哈希工具（无 ORM / 无网络依赖）。

这些 dataclass 是解析器与持久化层之间的唯一契约，因此可以脱离数据库、
脱离 FastAPI 单独做单元测试（``server/services/fulltext/tests``）。
"""

from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass, field
from typing import Any, Literal

#: 全文解析层级来源
SourceType = Literal["html", "pdf", "abstract_only"]
ParseStatus = Literal["ok", "partial", "unavailable", "failed"]
SpanVerdict = Literal["valid", "valid_by_hash", "invalid"]

#: 版本号后缀长度：源 URL + 内容 SHA-256 前 12 位
CONTENT_HASH_PREFIX_LEN = 12
#: paper_documents.document_version 的列宽（VARCHAR(64)）
DOCUMENT_VERSION_MAX_LEN = 64
#: paper_spans.quote_sha256 / paper_documents.text_sha256 为 CHAR(64)
SHA256_HEX_LEN = 64

_WHITESPACE_RE = re.compile(r"[ \t\u00a0\u3000]+")
_BLANKLINE_RE = re.compile(r"\n{3,}")
#: 不可落库的控制字符（含 NUL）：Postgres 的 text 字段拒收 NUL，其余控制字符也都是噪声。
#: 保留 ``\t``(09) 与 ``\n``(0a)：它们是正文的一部分（后续由空白折叠统一处理）。
_CONTROL_CHARS_RE = re.compile(r"[\x00-\x08\x0b-\x1f\x7f]")


def sha256_hex(data: bytes) -> str:
    """字节串 SHA-256（64 位十六进制小写）。"""
    return hashlib.sha256(data).hexdigest()


def quote_sha256(quote_text: str) -> str:
    """原文片段的 SHA-256。

    对 ``quote_text`` 的 UTF-8 编码做哈希，**不做任何归一化**——
    这样任何字符级篡改（增删空格、改标点）都会导致哈希不匹配。
    """
    return sha256_hex(quote_text.encode("utf-8"))


def build_document_version(
    source_url: str,
    content_sha256: str,
    parser: str | None = None,
    parser_version: str | None = None,
) -> str:
    """``document_version = 源 URL + 内容 SHA-256 前 12 位 [+ 解析器标识]``（§2.8 硬约束）。

    超过 ``VARCHAR(64)`` 时保留尾部哈希（哈希是去重与审计的关键部分），
    并按需截断 URL 前缀。截断是确定性的，不破坏唯一性。

    **为什么必须带解析器标识**（2026-09-24 实测）：不含解析器时，**换了 parser 也算同一个版本**
    → 重解析会走"已存在"捷径，或在 ``force`` 时 DELETE 旧 span 再 INSERT；
    而旧 span 可能已被 ``evidences.paper_span_id`` 外键引用（该外键**没有级联删除**），
    于是整次重解析被数据库拒绝（实测 32 次 FK violation，约占三成）。
    带上解析器后，**换 parser = 新版本 = 不动旧 span**，证据链自然保住。
    """

    if not source_url:
        raise ValueError("source_url 不能为空：document_version 必须可追溯到源地址")
    digest = (content_sha256 or "").lower()
    if len(digest) < CONTENT_HASH_PREFIX_LEN:
        raise ValueError("content_sha256 不合法：至少需要 12 位十六进制字符")
    suffix = digest[:CONTENT_HASH_PREFIX_LEN]
    if parser:
        # 只保留 [A-Za-z0-9._-]，长度也夹一下：它进的是版本串，不能带出奇怪字符
        tag = _safe_version_tag(parser, parser_version)
        if tag:
            suffix = f"{suffix}-{tag}"
    prefix = source_url
    budget = DOCUMENT_VERSION_MAX_LEN - len(suffix) - 1  # 1 为 '#'
    if budget <= 0:  # pragma: no cover - 列宽远大于 13
        return suffix
    if len(prefix) > budget:
        prefix = prefix[:budget]
    return f"{prefix}#{suffix}"


def _safe_version_tag(parser: str, parser_version: str | None) -> str:
    """``arxiv_html-1.1.1`` 这样的标签；长度夹到 24 字符内，避免吃掉太多 URL 预算。"""

    parts = [str(parser).strip(), str(parser_version).strip() if parser_version else ""]
    tag = "-".join(part for part in parts if part)
    tag = re.sub(r"[^A-Za-z0-9._-]", "", tag)
    return tag[:24]


def normalize_block_text(text: str) -> str:
    """段落文本归一：**剔除不可落库的控制字符**、折叠行内空白、去掉空行、统一换行。

    ``char_start`` / ``char_end`` 全部相对归一后的全文文本，
    因此本函数是偏移可复现的前提。

    ⚠️ 2026-09-24 实测踩到：PDF 抽出的文本里夹着 ``NUL (0x00)``，
    PostgreSQL 的 text 字段**不接受**它 → 整批 ``paper_spans`` 写入失败，
    而 ``paper_documents`` 那行已经提交 → 留下「文档 ok、覆盖 0.97，但一个片段都没有」的
    **不一致态**（全文门槛还会据此冒充 fulltext：卡片能建、却无处可定位）。
    在这里统一剔除 —— 两个解析器共用同一条清洗路径。
    """
    if not text:
        return ""
    collapsed = text.replace("\r\n", "\n").replace("\r", "\n")
    # NUL 与除 \t \n 外的 ASCII 控制字符一律剔除：在任何文档里都是噪声，
    # 而 Postgres 拒收 NUL —— 留着只会让落库在最后一步炸掉。
    collapsed = _CONTROL_CHARS_RE.sub("", collapsed)
    lines = [_WHITESPACE_RE.sub(" ", line).strip() for line in collapsed.split("\n")]
    lines = [line for line in lines if line]
    joined = "\n".join(lines)
    return _BLANKLINE_RE.sub("\n\n", joined).strip()


def bbox_to_json(bbox: tuple[float, float, float, float] | None) -> dict[str, float] | None:
    """``(x0, y0, x1, y1)`` -> ``{x0,y0,x1,y1}``（paper_spans.bbox 为 JSONB）。"""
    if bbox is None:
        return None
    x0, y0, x1, y1 = bbox
    return {
        "x0": round(float(x0), 2),
        "y0": round(float(y0), 2),
        "x1": round(float(x1), 2),
        "y1": round(float(y1), 2),
    }


@dataclass(slots=True)
class ParsedBlock:
    """解析出的一个可定位文本块（标题或段落）。"""

    text: str
    char_start: int
    char_end: int
    section_name: str = "other"
    page_number: int | None = None
    bbox: tuple[float, float, float, float] | None = None
    kind: str = "paragraph"  # heading | paragraph | caption | abstract | li

    @property
    def length(self) -> int:
        return len(self.text)

    @property
    def is_locatable(self) -> bool:
        """文本非空且偏移合法才可定位。"""
        return bool(self.text) and 0 <= self.char_start < self.char_end

    def to_span_dict(self) -> dict[str, Any]:
        """转成 ``paper_spans`` 的字段集合（不含 paper_id / document_version）。"""
        return {
            "section_name": self.section_name,
            "page_number": self.page_number,
            "bbox": bbox_to_json(self.bbox),
            "char_start": self.char_start,
            "char_end": self.char_end,
            "quote_text": self.text,
            "quote_sha256": quote_sha256(self.text),
        }


@dataclass(slots=True)
class ParsedDocument:
    """一次解析的完整产出（落库前的中间态）。"""

    source_type: SourceType
    source_url: str
    parser: str
    parser_version: str
    content_sha256: str
    full_text: str
    blocks: list[ParsedBlock] = field(default_factory=list)
    #: 整篇文档的字符数（含被 FULLTEXT_MAX_PAGES 截断掉的部分）
    char_count: int = 0
    page_count: int | None = None
    truncated: bool = False
    warnings: list[str] = field(default_factory=list)

    @property
    def locatable_chars(self) -> int:
        """可定位字符数 = 全部合法块的文本长度之和。"""
        return sum(block.length for block in self.blocks if block.is_locatable)

    @property
    def document_version(self) -> str:
        """版本串**必须带解析器标识**（否则换 parser 也算同一版本，重解析会撞 evidences 外键）。"""
        return build_document_version(
            self.source_url, self.content_sha256, self.parser, self.parser_version
        )

    @property
    def text_only_sha256(self) -> str:
        """归一全文的 SHA-256（额外审计信息，非 ``paper_documents.text_sha256``）。"""
        return sha256_hex(self.full_text.encode("utf-8"))

    def first_blocks(self, n: int) -> list[ParsedBlock]:
        return self.blocks[:n]


@dataclass(slots=True)
class PaperDocumentRecord:
    """``paper_documents`` 一行的纯数据视图。"""

    paper_id: int
    document_version: str
    source_type: str
    source_url: str
    parser: str
    parser_version: str
    text_sha256: str
    parse_status: ParseStatus
    page_count: int | None = None
    char_count: int | None = None
    locatable_chars: int | None = None
    coverage: float | None = None
    parse_error: str | None = None
    parsed_at: Any | None = None
    created_at: Any | None = None
    id: int | None = None

    @property
    def evidence_scope(self) -> str:
        """证据覆盖范围：``fulltext`` 或 ``abstract_only``（前端 CoverageTag 用）。"""
        return "fulltext" if self.spans_allowed else "abstract_only"

    @property
    def spans_allowed(self) -> bool:
        """是否允许生成正文 ``paper_span`` 证据（contracts.evidence_rules.fulltext_gate）。"""
        from services.fulltext.coverage import spans_allowed

        return spans_allowed(self.parse_status, self.coverage)

    def to_api_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "paper_id": self.paper_id,
            "document_version": self.document_version,
            "source_type": self.source_type,
            "source_url": self.source_url,
            "parser": self.parser,
            "parser_version": self.parser_version,
            "page_count": self.page_count,
            "text_sha256": self.text_sha256,
            "char_count": self.char_count,
            "locatable_chars": self.locatable_chars,
            "coverage": self.coverage,
            "parse_status": self.parse_status,
            "parse_error": self.parse_error,
            "parsed_at": self.parsed_at.isoformat() if hasattr(self.parsed_at, "isoformat") else self.parsed_at,
            "spans_allowed": self.spans_allowed,
            "evidence_scope": self.evidence_scope,
        }


@dataclass(slots=True)
class PaperSpanRecord:
    """``paper_spans`` 一行的纯数据视图。"""

    paper_id: int
    document_version: str
    char_start: int
    char_end: int
    quote_text: str
    quote_sha256: str
    section_name: str | None = None
    page_number: int | None = None
    bbox: dict[str, float] | None = None
    created_at: Any | None = None
    id: int | None = None

    @property
    def char_span(self) -> tuple[int, int]:
        return (self.char_start, self.char_end)

    def to_api_dict(self, verification: dict[str, Any] | None = None) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "id": self.id,
            "paper_id": self.paper_id,
            "document_version": self.document_version,
            "section_name": self.section_name,
            "page_number": self.page_number,
            "bbox": self.bbox,
            "char_start": self.char_start,
            "char_end": self.char_end,
            "quote_text": self.quote_text,
            "quote_sha256": self.quote_sha256,
            "created_at": self.created_at.isoformat()
            if hasattr(self.created_at, "isoformat")
            else self.created_at,
        }
        if verification is not None:
            payload["verification"] = verification
        return payload


@dataclass(slots=True)
class PaperRef:
    """解析所需的论文最小字段集（来自 WP03 的 ``papers`` 表）。"""

    id: int
    source: str = "arxiv"
    external_id: str = ""
    title: str = ""
    abstract: str | None = None
    pdf_url: str | None = None
    doi: str | None = None

    @property
    def arxiv_id(self) -> str | None:
        """从 ``external_id`` 中解析出 arXiv id（不含版本后缀）。"""
        raw = (self.external_id or "").strip()
        if not raw:
            return None
        raw = raw.replace("arXiv:", "").replace("arxiv:", "").strip()
        if raw.lower().startswith("http"):
            raw = raw.rstrip("/").split("/")[-1]
        match = _ARXIV_ID_RE.match(raw)
        return match.group(1) if match else None

    @property
    def arxiv_version(self) -> str | None:
        """arXiv 版本号（如 ``v2``）；无版本时返回 ``None``。"""
        raw = (self.external_id or "").replace("arXiv:", "").replace("arxiv:", "").strip()
        match = _ARXIV_ID_RE.match(raw)
        return f"v{match.group(2)}" if match and match.group(2) else None

    def canonical_url(self) -> str:
        if self.source == "arxiv" and self.arxiv_id:
            return f"https://arxiv.org/abs/{self.arxiv_id}"
        return self.pdf_url or f"paper://{self.id}"


_ARXIV_ID_RE = re.compile(
    r"^(\d{4}\.\d{4,5}|[a-z\-]+(?:\.[A-Z]{2})?/\d{7})(v\d+)?$",
    re.IGNORECASE,
)


@dataclass(slots=True)
class ParseAttempt:
    """单个来源的一次尝试留痕（解析失败必须可解释）。"""

    source_type: str
    source_url: str
    attempted: bool
    ok: bool
    message: str = ""

    def describe(self) -> str:
        flag = "ok" if self.ok else "fail"
        return f"[{flag}] {self.source_type} {self.source_url}: {self.message}"


@dataclass(slots=True)
class FulltextResult:
    """``ensure_fulltext`` 的返回值：文档记录 + 片段 + 可定位全文。"""

    document: PaperDocumentRecord
    spans: list[PaperSpanRecord] = field(default_factory=list)
    full_text: str = ""
    reused: bool = False
    attempts: list[ParseAttempt] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    truncated: bool = False

    @property
    def spans_allowed(self) -> bool:
        return self.document.spans_allowed

    @property
    def evidence_scope(self) -> str:
        return self.document.evidence_scope

    def summary(self) -> dict[str, Any]:
        return {
            "document_version": self.document.document_version,
            "parse_status": self.document.parse_status,
            "coverage": self.document.coverage,
            "parser": self.document.parser,
            "span_count": len(self.spans),
            "spans_allowed": self.spans_allowed,
            "evidence_scope": self.evidence_scope,
            "reused": self.reused,
            "truncated": self.truncated,
            "warnings": list(self.warnings),
            "attempts": [a.describe() for a in self.attempts],
        }
