"""Focused checks for home-chat attachment normalization and limits."""

from __future__ import annotations

import base64
import io
import zipfile

import pytest

from services.chat_attachments import normalize_chat_attachments


def _attachment(name: str, mime_type: str, data: bytes) -> dict[str, str]:
    return {
        "name": name,
        "mime_type": mime_type,
        "data_base64": base64.b64encode(data).decode("ascii"),
    }


def _zip_file(entries: dict[str, str]) -> bytes:
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as archive:
        for path, data in entries.items():
            archive.writestr(path, data)
    return buffer.getvalue()


def test_png_becomes_openai_image_url_block_and_safe_metadata() -> None:
    png = b"\x89PNG\r\n\x1a\n" + b"payload"
    blocks, metadata = normalize_chat_attachments([_attachment("clipboard.png", "image/png", png)])

    assert blocks == [
        {
            "type": "image_url",
            "image_url": {"url": "data:image/png;base64," + base64.b64encode(png).decode("ascii")},
        }
    ]
    assert metadata == [{"name": "clipboard.png", "mime_type": "image/png", "size": len(png)}]


def test_utf8_text_extracts_into_text_block_and_strips_path() -> None:
    blocks, metadata = normalize_chat_attachments(
        [_attachment("C:\\secret\\notes.md", "text/markdown", "研究问题".encode())]
    )

    assert blocks == [{"type": "text", "text": "[附件: notes.md]\n研究问题"}]
    assert metadata[0]["name"] == "notes.md"


def test_docx_extracts_paragraph_text_with_stdlib() -> None:
    docx = _zip_file(
        {
            "word/document.xml": (
                '<w:document xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main">'
                "<w:body><w:p><w:r><w:t>Paper title</w:t></w:r></w:p>"
                "<w:p><w:r><w:t>Abstract</w:t></w:r></w:p></w:body></w:document>"
            )
        }
    )
    blocks, _ = normalize_chat_attachments(
        [_attachment("draft.docx", "application/vnd.openxmlformats-officedocument.wordprocessingml.document", docx)]
    )

    assert blocks == [{"type": "text", "text": "[附件: draft.docx]\nPaper title\nAbstract"}]


def test_pptx_and_xlsx_extract_visible_text() -> None:
    pptx = _zip_file(
        {
            "ppt/slides/slide1.xml": (
                '<p:sld xmlns:p="http://schemas.openxmlformats.org/presentationml/2006/main" '
                'xmlns:a="http://schemas.openxmlformats.org/drawingml/2006/main">'
                "<p:sp><p:txBody><a:p><a:r><a:t>Study results</a:t></a:r></a:p></p:txBody></p:sp>"
                "</p:sld>"
            )
        }
    )
    xlsx = _zip_file(
        {
            "xl/sharedStrings.xml": (
                '<sst xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main">'
                "<si><t>Group</t></si><si><t>Control</t></si></sst>"
            ),
            "xl/worksheets/sheet1.xml": (
                '<worksheet xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main">'
                '<sheetData><row r="1"><c r="A1" t="s"><v>0</v></c>'
                '<c r="B1" t="s"><v>1</v></c></row></sheetData></worksheet>'
            ),
        }
    )

    ppt_blocks, _ = normalize_chat_attachments(
        [_attachment("slides.pptx", "application/vnd.openxmlformats-officedocument.presentationml.presentation", pptx)]
    )
    xlsx_blocks, _ = normalize_chat_attachments(
        [_attachment("data.xlsx", "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet", xlsx)]
    )

    assert ppt_blocks[0]["text"] == "[附件: slides.pptx]\n[幻灯片 1] Study results"
    assert xlsx_blocks[0]["text"] == "[附件: data.xlsx]\n[工作表 1]\nGroup\tControl"


@pytest.mark.parametrize(
    ("name", "mime", "payload", "message"),
    [
        ("photo.png", "image/jpeg", b"\x89PNG\r\n\x1a\n", "不匹配"),
        ("script.exe", "application/octet-stream", b"MZ", "不支持"),
        ("notes.txt", "text/plain", b"\xff", "UTF-8"),
        ("bad.json", "application/json", b"{oops", "JSON"),
        (
            "bad.docx",
            "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
            b"PK\x03\x04broken",
            "OOXML",
        ),
    ],
)
def test_invalid_or_unsupported_attachment_is_rejected(name, mime, payload, message) -> None:
    with pytest.raises(ValueError, match=message):
        normalize_chat_attachments([_attachment(name, mime, payload)])


def test_attachment_count_limit_is_enforced_after_decoding() -> None:
    payload = b"\x89PNG\r\n\x1a\n" + b"a" * (10 * 1024 * 1024 - 8)
    item = _attachment("a.png", "image/png", payload)
    with pytest.raises(ValueError, match="20 MiB"):
        normalize_chat_attachments([item, item, item])


def test_extracted_text_limit_is_enforced() -> None:
    item = _attachment("large.txt", "text/plain", b"x" * (100_001))
    with pytest.raises(ValueError, match="100000"):
        normalize_chat_attachments([item])


def test_zip_expansion_limit_is_enforced() -> None:
    docx = _zip_file(
        {
            "word/document.xml": (
                '<w:document xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main">'
                "<w:body><w:p><w:r><w:t>" + ("x" * 700_000) + "</w:t></w:r></w:p></w:body></w:document>"
            )
        }
    )
    item = _attachment("large.docx", "application/vnd.openxmlformats-officedocument.wordprocessingml.document", docx)
    with pytest.raises(ValueError, match="解压后超过安全限制"):
        normalize_chat_attachments([item])
