# Copyright 2026 SciLoop contributors
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
"""上传落盘与文件名安全化（论文导入的持久化地基）。

安全规则（硬约束）
------------------

1. **只取 basename**：``../../etc/passwd`` 与 ``C:\\Windows\\x.pdf`` 一律被压缩成
   最后一段文件名，任何目录成分都不会进入路径；
2. **白名单字符**：stem 只保留 ``[A-Za-z0-9._-]``，其余字符折叠为 ``_``；
3. **落盘前二次校验**：目标路径 ``resolve()`` 后必须仍位于上传根目录之下，
   否则抛 :class:`UnsafeUploadPath`（纵深防御，防止未来改动绕过第 1/2 步）；
4. 同名但内容不同的文件追加内容哈希前缀，**不覆盖**已有文件。

持久化目录
----------

默认 ``<server_root>/.cache/uploads``（容器内即 ``/app/server/.cache/uploads``，
与 docker-compose 的 bind mount 一一对应；宿主侧为 ``./.data/uploads``）。
可用环境变量 ``IMPORT_UPLOAD_DIR`` 覆盖（测试与离线验证使用）。
"""

from __future__ import annotations

import contextlib
import hashlib
import os
import re
import tempfile
from dataclasses import dataclass
from pathlib import Path

#: 单文件上限 20MB（契约）
MAX_FILE_BYTES = 20 * 1024 * 1024
#: 单次请求文件数上限 20（契约）
MAX_FILES_PER_REQUEST = 20
#: 单次请求请求体总量上限：20 × 20MB + 2MB 表单开销
MAX_REQUEST_BYTES = MAX_FILES_PER_REQUEST * MAX_FILE_BYTES + 2 * 1024 * 1024
#: 校验收到的 content-type 白名单
PDF_CONTENT_TYPES: frozenset[str] = frozenset(
    {"application/pdf", "application/x-pdf", "application/acrobat", "applications/pdf"}
)

_UNSAFE_CHARS = re.compile(r"[^A-Za-z0-9._-]+")
_MAX_STEM = 80


class UnsafeUploadPath(ValueError):
    """目标路径逃逸出上传根目录（不允许任意路径写入）。"""


@dataclass(slots=True)
class SavedFile:
    """一次落盘的结果（``name`` 为磁盘上的真实文件名）。"""

    name: str
    path: Path
    sha256: str
    size_bytes: int
    reused_existing: bool = False

    @property
    def source_url(self) -> str:
        """``paper_documents.source_url`` 口径：``upload://<文件名>``。"""
        return upload_source_url(self.name)


def default_upload_dir() -> Path:
    """默认上传目录：``<server_root>/.cache/uploads``。

    ``services/ingest/storage.py`` → parents[2] == ``<server_root>``，
    容器内即 ``/app/server``（与 compose 的挂载点一致），本地跑则是 ``server/``。
    """
    override = os.environ.get("IMPORT_UPLOAD_DIR")
    if override:
        return Path(override)
    return Path(__file__).resolve().parents[2] / ".cache" / "uploads"


def ensure_upload_dir(root: Path | str | None = None) -> Path:
    """确保上传目录存在并返回（不写入任何东西）。"""
    target = Path(root) if root is not None else default_upload_dir()
    target.mkdir(parents=True, exist_ok=True)
    return target


def upload_source_url(stored_name: str) -> str:
    return f"upload://{stored_name}"


def sanitize_filename(raw: str | None) -> str:
    """把任意用户文件名压成安全的单段文件名。

    只做形态归一，**不做合法性业务判断**（是否 PDF 由 :func:`has_pdf_hint` 判定）。
    """
    text = str(raw or "")
    text = text.replace("\\", "/").replace("\x00", "")
    text = text.split("/")[-1].strip().strip(".")
    stem, ext = os.path.splitext(text)
    stem = _UNSAFE_CHARS.sub("_", stem).strip("._-") or "upload"
    ext = _UNSAFE_CHARS.sub("", ext)[:10]
    return f"{stem[:_MAX_STEM]}{ext}"


def has_pdf_hint(filename: str | None, content_type: str | None) -> bool:
    """扩展名 ``.pdf`` **或** content-type 为 PDF 即视为 PDF（契约口径）。"""
    if str(filename or "").lower().strip().endswith(".pdf"):
        return True
    return str(content_type or "").split(";")[0].strip().lower() in PDF_CONTENT_TYPES


def safe_target_path(root: Path | str, name: str) -> Path:
    """在 ``root`` 下解析出安全的落盘路径；越界立即抛错。"""
    base = Path(root).resolve()
    candidate = (base / sanitize_filename(name)).resolve()
    if candidate.parent != base:
        raise UnsafeUploadPath(f"文件名 {name!r} 解析后逃逸上传目录，已拒绝")
    return candidate


def save_upload(
    root: Path | str | None,
    filename: str | None,
    content: bytes,
    *,
    force_suffix: str = ".pdf",
) -> SavedFile:
    """把字节写入上传目录，返回 :class:`SavedFile`。

    - 内容与已存在的同名文件一致 → 直接复用（不重复占盘，``reused_existing=True``）；
    - 同名但内容不同 → 追加内容哈希前缀，绝不覆盖。
    """
    base = ensure_upload_dir(root)
    digest = hashlib.sha256(content).hexdigest()
    safe_name = sanitize_filename(filename)

    if force_suffix and not safe_name.lower().endswith(force_suffix):
        stem = os.path.splitext(safe_name)[0] or "upload"
        safe_name = f"{stem}{force_suffix}"

    target = safe_target_path(base, safe_name)
    if target.exists():
        try:
            existing = target.read_bytes()
        except OSError:
            existing = b""
        if existing == content:
            return SavedFile(safe_name, target, digest, len(content), reused_existing=True)
        stem, ext = os.path.splitext(safe_name)
        safe_name = f"{stem[: _MAX_STEM - 9]}-{digest[:8]}{ext}"
        target = safe_target_path(base, safe_name)

    fd, tmp_name = tempfile.mkstemp(dir=str(base), suffix=".part")
    try:
        with os.fdopen(fd, "wb") as handle:
            handle.write(content)
        os.replace(tmp_name, target)
    except OSError:
        with contextlib.suppress(OSError):
            os.unlink(tmp_name)
        raise
    return SavedFile(safe_name, target, digest, len(content), reused_existing=False)


def stored_files(root: Path | str | None = None) -> list[dict[str, object]]:
    """列出上传目录内已落盘的文件（持久化验收用；只读）。"""
    base = Path(root) if root is not None else default_upload_dir()
    if not base.is_dir():
        return []
    items: list[dict[str, object]] = []
    for entry in sorted(base.iterdir()):
        if not entry.is_file() or entry.suffix == ".part":
            continue
        stat = entry.stat()
        items.append(
            {
                "name": entry.name,
                "size_bytes": int(stat.st_size),
                "modified_at": int(stat.st_mtime),
            }
        )
    return items


__all__ = [
    "MAX_FILES_PER_REQUEST",
    "MAX_FILE_BYTES",
    "MAX_REQUEST_BYTES",
    "PDF_CONTENT_TYPES",
    "SavedFile",
    "UnsafeUploadPath",
    "default_upload_dir",
    "ensure_upload_dir",
    "has_pdf_hint",
    "safe_target_path",
    "sanitize_filename",
    "save_upload",
    "stored_files",
    "upload_source_url",
]
