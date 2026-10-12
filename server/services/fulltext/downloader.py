"""Download-only full-text ingestion used by the paper fetch pipeline.

Downloading and parsing are deliberately separate. A successful download is a
valid imported paper even when a later parser cannot extract text.
"""

from __future__ import annotations

import re
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from sqlalchemy.orm import Session

from services.fulltext.document_store import HttpxFetcher, SourceCandidate, document_sources
from services.fulltext.records import PaperRef, sha256_hex
from services.fulltext.repository import SqlDocumentRepository
from services.storage import knowledge_root


def _extension(candidate: SourceCandidate, content_type: str | None, content: bytes) -> str:
    content_type = (content_type or "").lower()
    if candidate.source_type == "pdf" or "application/pdf" in content_type or content.startswith(b"%PDF"):
        return "pdf"
    if "html" in content_type or re.search(br"<html|<!doctype", content[:2048], re.I):
        return "html"
    return "bin"


async def download_for_paper(
    session: Session,
    paper_id: int,
    *,
    fetcher: Any | None = None,
) -> dict[str, Any]:
    """Try the configured HTML/PDF candidates and persist the first valid payload."""
    repository = SqlDocumentRepository(session, commit=False)
    paper = await repository.get_paper(int(paper_id))
    if paper is None:
        return {"ok": False, "paper_id": int(paper_id), "error": "paper_not_found"}
    client = fetcher or HttpxFetcher()
    attempts: list[dict[str, Any]] = []
    for candidate in document_sources(paper, html_first=True):
        fetched = await client.fetch(candidate.url)
        attempts.append({"url": candidate.url, "status": fetched.status, "error": fetched.error})
        if not fetched.ok:
            continue
        content = fetched.content or b""
        # Reject empty/obvious error pages. A full-text import must contain a real payload.
        if len(content) < 128:
            continue
        ext = _extension(candidate, fetched.content_type, content)
        digest = sha256_hex(content)
        root = Path(knowledge_root()) / "papers" / "fulltext"
        root.mkdir(parents=True, exist_ok=True)
        path = root / f"{int(paper_id)}__{digest[:12]}.{ext}"
        path.write_bytes(content)

        from db.models.paper import Paper

        row = session.get(Paper, int(paper_id))
        if row is not None:
            raw = dict(row.raw or {})
            raw["fulltext"] = {
                "downloaded": True,
                "format": ext,
                "path": str(path.relative_to(Path(knowledge_root()))).replace("\\", "/"),
                "url": fetched.url or candidate.url,
                "sha256": digest,
                "downloaded_at": datetime.now(UTC).isoformat(),
            }
            row.raw = raw
            session.flush()
        return {
            "ok": True,
            "paper_id": int(paper_id),
            "format": ext,
            "path": str(path),
            "url": fetched.url or candidate.url,
            "sha256": digest,
            "attempts": attempts,
        }
    return {"ok": False, "paper_id": int(paper_id), "error": "fulltext_download_failed", "attempts": attempts}


__all__ = ["download_for_paper"]
