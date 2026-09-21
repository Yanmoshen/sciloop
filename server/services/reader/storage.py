# Copyright 2026 SciLoop contributors
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
"""阅读器库目录（``reader-library``）与归档 ZIP 的落盘/读取。

目录布局（持久化，禁止放临时目录）::

    .cache/reader-library/                       # 容器内 /app/server/.cache/reader-library
        documents/<document_id>/
            versions/<version_no>-<kind>.pdf          # 不可变版本 PDF
            versions/<version_no>-<kind>.index.json   # 文本索引 + 页面对应关系
            archives/<timestamp>-<document_id>.zip    # 归档（批注 JSON + 版本 PDF + 索引）

为什么必须持久化
----------------
数据库里的版本记录指向这里的文件；目录丢失会让「版本」变成悬空指针
（EasyPaper §4.8：阅读器文件目录必须持久化）。因此：

- 落盘用 **同目录临时文件 + os.replace**，避免半截文件被读到；
- 每个落盘动作都返回真实 sha256 与字节数，**不猜测、不编造**；
- 路径一律由整型 id 与白名单 ``kind`` 拼出，再经 :func:`safe_join` 二次校验
  （``resolve()`` 后必须仍在库根之下），杜绝路径穿越。
"""

from __future__ import annotations

import contextlib
import hashlib
import io
import json
import os
import re
import tempfile
import zipfile
from pathlib import Path, PurePosixPath
from typing import Any

from services.reader.errors import (
    ArchiveTooLargeError,
    UnsafeArchivePathError,
)

#: 单份原文/版本 PDF 体积上限 50MB（EasyPaper §7 ``max_upload_mb``）
MAX_SOURCE_BYTES = 50 * 1024 * 1024
#: 归档体积上限 100MB
MAX_ARCHIVE_BYTES = 100 * 1024 * 1024
#: 归档内单个条目上限（防止 zip bomb 式解压）
MAX_ARCHIVE_ENTRY_BYTES = 50 * 1024 * 1024
#: 归档条目总数上限
MAX_ARCHIVE_ENTRIES = 500

_UNSAFE_CHARS = re.compile(r"[^A-Za-z0-9._-]+")
_MAX_SEGMENT = 80


class UnsafeReaderPath(ValueError):
    """路径逃逸出库根目录（不允许任意路径写入）。"""


def default_library_dir() -> Path:
    """默认库根：``<server_root>/.cache/reader-library``。

    ``services/reader/storage.py`` → ``parents[2]`` == ``<server_root>``，
    容器内即 ``/app/server``（与 docker-compose 的 bind mount 完全对应：
    宿主 ``./.data/reader-library``）。可用 ``READER_LIBRARY_DIR`` 覆盖（测试/离线验证）。
    """
    override = os.environ.get("READER_LIBRARY_DIR")
    if override:
        return Path(override)
    return Path(__file__).resolve().parents[2] / ".cache" / "reader-library"


def ensure_library_dir(root: Path | str | None = None) -> Path:
    """确保库根存在并返回（不写入任何业务文件）。"""
    target = Path(root) if root is not None else default_library_dir()
    target.mkdir(parents=True, exist_ok=True)
    return target


def sanitize_segment(raw: str | None, *, fallback: str = "item") -> str:
    """把任意字符串压成**单段**安全文件名（只做形态归一，不做业务判断）。"""
    text = str(raw or "").replace("\\", "/").replace("\x00", "")
    text = text.split("/")[-1].strip().strip(".")
    stem, ext = os.path.splitext(text)
    stem = _UNSAFE_CHARS.sub("_", stem).strip("._-") or fallback
    ext = _UNSAFE_CHARS.sub("", ext)[:10]
    return f"{stem[:_MAX_SEGMENT]}{ext}"


def safe_join(root: Path | str, *segments: str) -> Path:
    """在 ``root`` 下解析出安全路径；越界立即抛 :class:`UnsafeReaderPath`。"""
    base = Path(root).resolve()
    candidate = base
    for segment in segments:
        safe = sanitize_segment(segment)
        candidate = candidate / safe
    resolved = candidate.resolve()
    if resolved != base and base not in resolved.parents:
        raise UnsafeReaderPath(f"路径 {segments!r} 解析后逃逸库根 {base}，已拒绝")
    return resolved


def document_dir(document_id: int, *, root: Path | str | None = None) -> Path:
    """``<root>/documents/<document_id>``（整型 id，形态天然安全，仍经 safe_join）。"""
    base = Path(root) if root is not None else default_library_dir()
    return safe_join(base, "documents", str(int(document_id)))


def versions_dir(document_id: int, *, root: Path | str | None = None) -> Path:
    return safe_join(document_dir(document_id, root=root), "versions")


def archives_dir(document_id: int, *, root: Path | str | None = None) -> Path:
    return safe_join(document_dir(document_id, root=root), "archives")


def version_file_name(version_no: int, kind: str, *, suffix: str = ".pdf") -> str:
    """``<version_no 两位>-<kind><suffix>``，例如 ``01-original.pdf``。"""
    return sanitize_segment(f"{int(version_no):02d}-{kind}{suffix}", fallback="version")


def sha256_bytes(payload: bytes) -> str:
    return hashlib.sha256(payload).hexdigest()


def canonical_json_bytes(payload: Any) -> bytes:
    """规范化 JSON 字节（``sort_keys`` + 紧凑分隔符）——哈希可复算的前提。"""
    return json.dumps(
        payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")
    ).encode("utf-8")


def payload_sha256(payload: Any) -> str:
    return sha256_bytes(canonical_json_bytes(payload))


def sha256_file(path: Path | str) -> str | None:
    """计算文件 SHA-256；文件不存在或不可读返回 ``None``（不编造）。"""
    try:
        digest = hashlib.sha256()
        with Path(path).open("rb") as handle:
            for chunk in iter(lambda: handle.read(1024 * 1024), b""):
                digest.update(chunk)
        return digest.hexdigest()
    except OSError:
        return None


def write_bytes_atomic(path: Path | str, payload: bytes) -> Path:
    """原子写（同目录临时文件 + ``os.replace``）。"""
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(dir=str(target.parent), suffix=".part")
    try:
        with os.fdopen(fd, "wb") as handle:
            handle.write(payload)
        os.replace(tmp_name, target)
    except OSError:
        with contextlib.suppress(OSError):
            os.unlink(tmp_name)
        raise
    return target


def write_json_atomic(path: Path | str, payload: Any) -> Path:
    return write_bytes_atomic(path, canonical_json_bytes(payload))


def read_json(path: Path | str) -> Any | None:
    """读取 JSON **文件**；不存在 / 不可解析返回 ``None``（不猜测内容）。"""
    try:
        return json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None


def loads_json(payload: bytes | str) -> Any | None:
    """解析 JSON **字节/文本**（归档条目用）；不可解析返回 ``None``。

    与 :func:`read_json` 的区别是入参不是路径——归档里的条目是内存字节，
    误用 ``read_json`` 会把 JSON 文本当路径去 ``Path(...).read_text()``，
    结果是「一切归档都不可解析」（容器内验收实测踩过这个坑）。
    """
    try:
        return json.loads(payload)
    except (TypeError, ValueError):
        return None


# --------------------------------------------------------------------------- #
# 归档 ZIP：条目名安全化（无 `..`、无绝对路径、无盘符、无反斜杠）
# --------------------------------------------------------------------------- #
def safe_zip_name(name: str) -> str:
    """校验并归一 ZIP 条目名；不安全的名字直接拒绝。"""
    text = str(name or "").replace("\\", "/").strip()
    if not text:
        raise UnsafeArchivePathError("归档条目名为空")
    if text.startswith("/") or text.startswith("~"):
        raise UnsafeArchivePathError(f"归档条目名不允许绝对路径：{name!r}")
    if re.match(r"^[A-Za-z]:", text):
        raise UnsafeArchivePathError(f"归档条目名不允许盘符：{name!r}")
    parts = PurePosixPath(text).parts
    if any(part in ("..", ".") for part in parts):
        raise UnsafeArchivePathError(f"归档条目名包含上级或当前目录：{name!r}")
    if len(parts) > 6:
        raise UnsafeArchivePathError(f"归档条目层级过深：{name!r}")
    for part in parts:
        if _UNSAFE_CHARS.search(part) and part != sanitize_segment(part):
            raise UnsafeArchivePathError(f"归档条目名含不安全字符：{name!r}")
    return "/".join(parts)


def build_zip(entries: dict[str, bytes]) -> bytes:
    """按 ``{条目名: 字节}`` 生成 ZIP（``ZIP_DEFLATED``，条目名先安全化）。

    超过 :data:`MAX_ARCHIVE_BYTES` 抛 :class:`ArchiveTooLargeError`（附实际体积）。
    """
    if len(entries) > MAX_ARCHIVE_ENTRIES:
        raise ArchiveTooLargeError(
            f"归档条目数 {len(entries)} 超过上限 {MAX_ARCHIVE_ENTRIES}",
            detail={"entries": len(entries), "limit": MAX_ARCHIVE_ENTRIES},
        )
    normalized: dict[str, bytes] = {}
    for name, payload in entries.items():
        key = safe_zip_name(name)
        if len(payload) > MAX_ARCHIVE_ENTRY_BYTES:
            raise ArchiveTooLargeError(
                f"归档条目 {key} 体积 {len(payload)} 超过上限 {MAX_ARCHIVE_ENTRY_BYTES}",
                detail={"entry": key, "limit": MAX_ARCHIVE_ENTRY_BYTES},
            )
        normalized[key] = payload

    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        for name in sorted(normalized):
            archive.writestr(name, normalized[name])
    payload = buffer.getvalue()
    if len(payload) > MAX_ARCHIVE_BYTES:
        raise ArchiveTooLargeError(
            f"归档体积 {len(payload)} 字节超过上限 {MAX_ARCHIVE_BYTES} 字节",
            detail={
                "archive_bytes": len(payload),
                "limit_bytes": MAX_ARCHIVE_BYTES,
                "entries": sorted(normalized),
            },
        )
    return payload


def read_zip(payload: bytes) -> dict[str, bytes]:
    """读取 ZIP；条目名逐个安全化校验，异常结构拒绝（不使用 extractall）。"""
    entries: dict[str, bytes] = {}
    try:
        with zipfile.ZipFile(io.BytesIO(payload)) as archive:
            names = archive.namelist()
            if len(names) > MAX_ARCHIVE_ENTRIES:
                raise ArchiveTooLargeError(
                    f"归档条目数 {len(names)} 超过上限 {MAX_ARCHIVE_ENTRIES}",
                    detail={"entries": len(names), "limit": MAX_ARCHIVE_ENTRIES},
                )
            for info in archive.infolist():
                if info.is_dir():
                    continue
                name = safe_zip_name(info.filename)
                if info.file_size > MAX_ARCHIVE_ENTRY_BYTES:
                    raise ArchiveTooLargeError(
                        f"归档条目 {name} 声明体积超过上限",
                        detail={"entry": name, "limit": MAX_ARCHIVE_ENTRY_BYTES},
                    )
                with archive.open(info) as handle:
                    data = handle.read(MAX_ARCHIVE_ENTRY_BYTES + 1)
                if len(data) > MAX_ARCHIVE_ENTRY_BYTES:
                    raise ArchiveTooLargeError(
                        f"归档条目 {name} 实际体积超过上限",
                        detail={"entry": name, "limit": MAX_ARCHIVE_ENTRY_BYTES},
                    )
                entries[name] = data
    except zipfile.BadZipFile as exc:
        raise UnsafeArchivePathError(f"归档不是合法 ZIP：{exc}") from exc
    if not entries:
        raise UnsafeArchivePathError("归档为空（无任何条目）")
    return entries


def archive_file_name(document_id: int, stamp: str) -> str:
    """归档文件名：``<UTC 时间戳>-<document_id>.zip``（时间戳由调用方给出，便于复现）。"""
    return sanitize_segment(f"{stamp}-{int(document_id)}.zip", fallback="archive.zip")


__all__ = [
    "MAX_ARCHIVE_BYTES",
    "MAX_ARCHIVE_ENTRIES",
    "MAX_ARCHIVE_ENTRY_BYTES",
    "MAX_SOURCE_BYTES",
    "UnsafeReaderPath",
    "archive_file_name",
    "archives_dir",
    "build_zip",
    "canonical_json_bytes",
    "default_library_dir",
    "document_dir",
    "ensure_library_dir",
    "payload_sha256",
    "loads_json",
    "read_json",
    "read_zip",
    "safe_join",
    "safe_zip_name",
    "sanitize_segment",
    "sha256_bytes",
    "sha256_file",
    "version_file_name",
    "versions_dir",
    "write_bytes_atomic",
    "write_json_atomic",
]
