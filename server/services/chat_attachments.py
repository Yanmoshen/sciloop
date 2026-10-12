# Copyright 2026 SciLoop contributors
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
"""Validate and normalize files attached to a home-chat message."""

from __future__ import annotations

import base64
import binascii
import csv
import io
import re
import zipfile
from dataclasses import dataclass
from typing import Any
from xml.etree import ElementTree

MAX_ATTACHMENT_BYTES = 10 * 1024 * 1024
MAX_TOTAL_ATTACHMENT_BYTES = 20 * 1024 * 1024
MAX_EXTRACTED_TEXT_CHARS = 100_000
MAX_TOTAL_EXTRACTED_TEXT_CHARS = 200_000
MAX_ARCHIVE_ENTRIES = 2_000
MAX_ARCHIVE_UNCOMPRESSED_BYTES = 30 * 1024 * 1024
MAX_ARCHIVE_COMPRESSION_RATIO = 200

_IMAGE_MIMES = {
    ".png": ("image/png", b"\x89PNG\r\n\x1a\n"),
    ".jpg": ("image/jpeg", b"\xff\xd8\xff"),
    ".jpeg": ("image/jpeg", b"\xff\xd8\xff"),
    ".gif": ("image/gif", (b"GIF87a", b"GIF89a")),
    ".webp": ("image/webp", None),
}
_TEXT_MIMES = {
    ".txt": {"text/plain"},
    ".md": {"text/markdown", "text/plain"},
    ".markdown": {"text/markdown", "text/plain"},
    ".csv": {"text/csv", "application/csv", "text/plain"},
    ".tsv": {"text/tab-separated-values", "text/plain"},
    ".json": {"application/json", "text/json", "text/plain"},
    ".html": {"text/html"},
    ".htm": {"text/html"},
    ".xml": {"application/xml", "text/xml"},
    ".yaml": {"application/yaml", "text/yaml", "text/plain"},
    ".yml": {"application/yaml", "text/yaml", "text/plain"},
    ".tex": {"application/x-tex", "text/x-tex", "text/plain"},
    ".py": {"text/x-python", "text/plain", "application/x-python"},
    ".js": {"text/javascript", "application/javascript", "text/plain"},
    ".ts": {"text/typescript", "application/typescript", "text/plain"},
    ".tsx": {"text/plain", "text/typescript"},
    ".jsx": {"text/plain", "text/javascript"},
    ".css": {"text/css", "text/plain"},
    ".java": {"text/x-java-source", "text/plain"},
    ".c": {"text/x-c", "text/plain"},
    ".h": {"text/x-c", "text/plain"},
    ".cpp": {"text/x-c++src", "text/plain"},
    ".rs": {"text/plain", "text/x-rust"},
    ".go": {"text/plain", "text/x-go"},
    ".sh": {"text/x-shellscript", "text/plain"},
    ".sql": {"application/sql", "text/sql", "text/plain"},
    ".log": {"text/plain"},
}
_OFFICE_MIMES = {
    ".docx": "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
    ".pptx": "application/vnd.openxmlformats-officedocument.presentationml.presentation",
    ".xlsx": "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
}
_PDF_MIMES = {"application/pdf"}
_XML_NS = "{http://schemas.openxmlformats.org/wordprocessingml/2006/main}"
_SHEET_NS = "{http://schemas.openxmlformats.org/spreadsheetml/2006/main}"


@dataclass(frozen=True)
class _Attachment:
    name: str
    mime_type: str
    data: bytes


def _decode_attachment(raw: Any, index: int) -> _Attachment:
    if not isinstance(raw, dict):
        raise ValueError(f"附件 {index + 1}: 必须是对象")
    name = raw.get("name")
    mime_type = raw.get("mime_type")
    encoded = raw.get("data_base64")
    if not isinstance(name, str) or not name.strip() or len(name) > 255:
        raise ValueError(f"附件 {index + 1}: 文件名无效")
    # Strip path components so uploaded metadata can never imply a local path.
    name = name.replace("\\", "/").rsplit("/", 1)[-1].strip()
    if not name or name in {".", ".."}:
        raise ValueError(f"附件 {index + 1}: 文件名无效")
    if not isinstance(mime_type, str) or len(mime_type) > 128:
        raise ValueError(f"附件 {name}: MIME 类型无效")
    mime_type = mime_type.split(";", 1)[0].strip().lower()
    if not re.fullmatch(r"[a-z0-9.+-]+/[a-z0-9.+-]+", mime_type):
        raise ValueError(f"附件 {name}: MIME 类型无效")
    if not isinstance(encoded, str) or not encoded or len(encoded) > (MAX_ATTACHMENT_BYTES * 4 // 3 + 8):
        raise ValueError(f"附件 {name}: 文件为空或超过单文件大小限制")
    try:
        data = base64.b64decode(encoded, validate=True)
    except (binascii.Error, ValueError) as exc:
        raise ValueError(f"附件 {name}: Base64 数据无效") from exc
    if not data:
        raise ValueError(f"附件 {name}: 文件为空")
    if len(data) > MAX_ATTACHMENT_BYTES:
        raise ValueError(f"附件 {name}: 单文件不能超过 10 MiB")
    return _Attachment(name=name, mime_type=mime_type, data=data)


def _is_image_signature(ext: str, data: bytes) -> bool:
    mime, signature = _IMAGE_MIMES[ext]
    if ext == ".webp":
        return len(data) >= 12 and data[:4] == b"RIFF" and data[8:12] == b"WEBP"
    if isinstance(signature, tuple):
        return data.startswith(signature)
    return data.startswith(signature or b"") and mime == _IMAGE_MIMES[ext][0]


def _safe_xml(data: bytes, name: str) -> ElementTree.Element:
    content = data.upper()
    if b"<!DOCTYPE" in content or b"<!ENTITY" in content:
        raise ValueError(f"附件 {name}: XML 包含不支持的声明")
    try:
        return ElementTree.fromstring(data)
    except ElementTree.ParseError as exc:
        raise ValueError(f"附件 {name}: OOXML 内容损坏") from exc


def _zip_xml_text(attachment: _Attachment, ext: str) -> str:
    try:
        archive = zipfile.ZipFile(io.BytesIO(attachment.data))
    except (zipfile.BadZipFile, OSError) as exc:
        raise ValueError(f"附件 {attachment.name}: Office 文件不是有效的 OOXML 压缩包") from exc
    with archive:
        infos = archive.infolist()
        if len(infos) > MAX_ARCHIVE_ENTRIES:
            raise ValueError(f"附件 {attachment.name}: Office 文件目录过大")
        expanded = sum(item.file_size for item in infos)
        compressed = max(1, sum(item.compress_size for item in infos))
        if expanded > MAX_ARCHIVE_UNCOMPRESSED_BYTES or expanded / compressed > MAX_ARCHIVE_COMPRESSION_RATIO:
            raise ValueError(f"附件 {attachment.name}: Office 文件解压后超过安全限制")
        files = {item.filename: item for item in infos if not item.is_dir()}

        def parse(path: str) -> ElementTree.Element:
            info = files.get(path)
            if info is None:
                raise ValueError(f"附件 {attachment.name}: Office 文件缺少 {path}")
            return _safe_xml(archive.read(info), attachment.name)

        texts: list[str] = []
        if ext == ".docx":
            root = parse("word/document.xml")
            for paragraph in root.iter(f"{_XML_NS}p"):
                line = "".join(node.text or "" for node in paragraph.iter(f"{_XML_NS}t"))
                if line.strip():
                    texts.append(line)
        elif ext == ".pptx":
            paths = sorted(
                (path for path in files if re.fullmatch(r"ppt/slides/slide\d+\.xml", path)),
                key=lambda path: int(re.search(r"slide(\d+)", path).group(1)),  # type: ignore[union-attr]
            )
            if not paths:
                raise ValueError(f"附件 {attachment.name}: 演示文稿没有幻灯片")
            for index, path in enumerate(paths, 1):
                root = parse(path)
                slide_text = [node.text or "" for node in root.iter() if node.tag.endswith("}t")]
                texts.append(f"[幻灯片 {index}] " + " ".join(item for item in slide_text if item))
        else:
            shared: list[str] = []
            if "xl/sharedStrings.xml" in files:
                root = parse("xl/sharedStrings.xml")
                shared = ["".join(node.text or "" for node in item.iter(f"{_SHEET_NS}t")) for item in root]
            sheets = sorted(
                (path for path in files if re.fullmatch(r"xl/worksheets/sheet\d+\.xml", path)),
                key=lambda path: int(re.search(r"sheet(\d+)", path).group(1)),  # type: ignore[union-attr]
            )
            if not sheets:
                raise ValueError(f"附件 {attachment.name}: 工作簿没有工作表")
            for index, path in enumerate(sheets, 1):
                root = parse(path)
                rows = []
                for row in root.iter(f"{_SHEET_NS}row"):
                    cells = []
                    for cell in row.findall(f"{_SHEET_NS}c"):
                        value = cell.find(f"{_SHEET_NS}v")
                        text = value.text if value is not None else ""
                        if cell.get("t") == "s" and text.isdigit() and int(text) < len(shared):
                            text = shared[int(text)]
                        inline = cell.find(f"{_SHEET_NS}is")
                        if inline is not None:
                            text = "".join(node.text or "" for node in inline.iter(f"{_SHEET_NS}t"))
                        cells.append(text or "")
                    if any(cells):
                        rows.append("\t".join(cells))
                texts.append(f"[工作表 {index}]\n" + "\n".join(rows))
        return "\n".join(texts)


def _extract_text(attachment: _Attachment, ext: str) -> str:
    if ext in _TEXT_MIMES:
        if attachment.mime_type not in _TEXT_MIMES[ext]:
            raise ValueError(f"附件 {attachment.name}: 文件扩展名与 MIME 类型不匹配")
        try:
            text = attachment.data.decode("utf-8-sig", errors="strict")
        except UnicodeDecodeError as exc:
            raise ValueError(f"附件 {attachment.name}: 文本文件必须使用 UTF-8 编码") from exc
        if "\x00" in text:
            raise ValueError(f"附件 {attachment.name}: 文件不是有效文本")
        if ext == ".json":
            import json

            try:
                json.loads(text)
            except json.JSONDecodeError as exc:
                raise ValueError(f"附件 {attachment.name}: JSON 格式无效") from exc
        elif ext in {".csv", ".tsv"}:
            try:
                list(csv.reader(io.StringIO(text), delimiter="\t" if ext == ".tsv" else ","))
            except csv.Error as exc:
                raise ValueError(f"附件 {attachment.name}: 表格格式无效") from exc
        return text
    if ext == ".pdf":
        if attachment.mime_type not in _PDF_MIMES or not attachment.data.startswith(b"%PDF-"):
            raise ValueError(f"附件 {attachment.name}: PDF 文件格式无效")
        try:
            import fitz

            document = fitz.open(stream=attachment.data, filetype="pdf")
            pages: list[str] = []
            char_count = 0
            for page in document:
                page_text = page.get_text()
                pages.append(page_text)
                char_count += len(page_text)
                if char_count > MAX_EXTRACTED_TEXT_CHARS:
                    raise ValueError(
                        f"附件 {attachment.name}: 提取文本不能超过 {MAX_EXTRACTED_TEXT_CHARS} 个字符"
                    )
            text = "\n".join(pages)
            document.close()
        except ValueError:
            raise
        except Exception as exc:
            raise ValueError(f"附件 {attachment.name}: PDF 无法读取") from exc
        return text
    if ext in _OFFICE_MIMES:
        if attachment.mime_type != _OFFICE_MIMES[ext]:
            raise ValueError(f"附件 {attachment.name}: 文件扩展名与 MIME 类型不匹配")
        if not attachment.data.startswith(b"PK\x03\x04"):
            raise ValueError(f"附件 {attachment.name}: Office 文件格式无效")
        return _zip_xml_text(attachment, ext)
    raise ValueError(f"附件 {attachment.name}: 不支持此文件格式")


def normalize_chat_attachments(
    attachments: list[dict[str, Any]],
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """Return OpenAI-style content blocks and safe attachment display metadata.

    Input entries contain ``name``, ``mime_type`` and ``data_base64``. Images
    become ``image_url`` blocks; supported document text becomes plain text
    blocks. Limits apply to encoded files and the combined extracted text.
    """
    if not isinstance(attachments, list):
        raise ValueError("附件必须是列表")
    model_content: list[dict[str, Any]] = []
    display_metadata: list[dict[str, Any]] = []
    total_bytes = 0
    total_text_chars = 0
    for index, raw in enumerate(attachments):
        item = _decode_attachment(raw, index)
        total_bytes += len(item.data)
        if total_bytes > MAX_TOTAL_ATTACHMENT_BYTES:
            raise ValueError("附件总大小不能超过 20 MiB")
        ext = "." + item.name.rsplit(".", 1)[-1].lower() if "." in item.name else ""
        if ext in _IMAGE_MIMES:
            expected_mime, _ = _IMAGE_MIMES[ext]
            if item.mime_type != expected_mime or not _is_image_signature(ext, item.data):
                raise ValueError(f"附件 {item.name}: 图片扩展名、MIME 类型或文件内容不匹配")
            model_content.append(
                {"type": "image_url", "image_url": {"url": f"data:{expected_mime};base64,{raw['data_base64']}"}}
            )
            display_metadata.append({"name": item.name, "mime_type": expected_mime, "size": len(item.data)})
            continue
        text = _extract_text(item, ext)
        if len(text) > MAX_EXTRACTED_TEXT_CHARS:
            raise ValueError(f"附件 {item.name}: 提取文本不能超过 {MAX_EXTRACTED_TEXT_CHARS} 个字符")
        total_text_chars += len(text)
        if total_text_chars > MAX_TOTAL_EXTRACTED_TEXT_CHARS:
            raise ValueError("附件提取文本总量不能超过 200000 个字符")
        model_content.append({"type": "text", "text": f"[附件: {item.name}]\n{text}"})
        display_metadata.append({"name": item.name, "mime_type": item.mime_type, "size": len(item.data)})
    return model_content, display_metadata
