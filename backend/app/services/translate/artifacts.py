# Copyright 2026 SciLoop contributors
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
"""翻译产物目录与 ``manifest.json`` 读写（与“全文阅读器”模块的冻结接口）。

目录布局（**字段名与文件名不可改**，阅读器模块按此读取）::

    .cache/artifacts/<task_id>/
        manifest.json
        source.pdf          # 原始 PDF（from-paper 时为其原文）
        mono.pdf            # 必产（成功时）
        dual.pdf            # 仅 translate 模式
        preview.html        # 可选

容器内根目录为 ``/app/backend/.cache/artifacts``（docker-compose 已把宿主
``./.data/artifacts`` 绑到此处）；可用环境变量 ``TRANSLATE_ARTIFACT_DIR`` 覆盖
（测试与离线验证使用）。**任务状态存内存会丢，但这里落盘的产物与 manifest 必须保留。**
"""

from __future__ import annotations

import contextlib
import hashlib
import json
import os
import re
import tempfile
from pathlib import Path
from typing import Any

#: manifest schema 版本（阅读器据此做兼容）
SCHEMA_VERSION = 1
#: 产物文件名（冻结）
MANIFEST_NAME = "manifest.json"
SOURCE_NAME = "source.pdf"
MONO_NAME = "mono.pdf"
DUAL_NAME = "dual.pdf"
PREVIEW_NAME = "preview.html"

#: 单文件上限 20MB；页数上限 100（契约）
MAX_FILE_BYTES = 20 * 1024 * 1024
MAX_PAGES = 100
#: 单次请求体上限：20MB 文件 + 2MB 表单开销
MAX_REQUEST_BYTES = MAX_FILE_BYTES + 2 * 1024 * 1024

_TASK_ID_RE = re.compile(r"^[A-Za-z0-9_-]{4,64}$")


class UnsafeTaskId(ValueError):
    """task_id 形态非法（不允许用它拼出任意路径）。"""


def default_artifact_root() -> Path:
    """默认产物根目录：``<backend_root>/.cache/artifacts``。

    ``app/services/translate/artifacts.py`` → ``parents[3]`` == ``<backend_root>``，
    容器内即 ``/app/backend``（与 compose 的挂载点一致）。
    """
    override = os.environ.get("TRANSLATE_ARTIFACT_DIR")
    if override:
        return Path(override)
    return Path(__file__).resolve().parents[3] / ".cache" / "artifacts"


def safe_task_id(task_id: str) -> str:
    """校验 task_id 形态（拒绝 ``..`` / 路径分隔符等一切越界可能）。"""
    text = str(task_id or "").strip()
    if not _TASK_ID_RE.match(text):
        raise UnsafeTaskId(f"非法 task_id: {task_id!r}")
    return text


def task_dir(task_id: str, *, root: Path | str | None = None) -> Path:
    """返回 ``<root>/<task_id>``（已做形态校验；不创建目录）。"""
    base = Path(root) if root is not None else default_artifact_root()
    return Path(base) / safe_task_id(task_id)


def ensure_task_dir(task_id: str, *, root: Path | str | None = None) -> Path:
    """确保产物目录存在并返回。"""
    target = task_dir(task_id, root=root)
    target.mkdir(parents=True, exist_ok=True)
    return target


def sha256_bytes(payload: bytes) -> str:
    return hashlib.sha256(payload).hexdigest()


def sha256_file(path: Path | str) -> str | None:
    """计算文件 SHA-256；文件不存在或不可读时返回 ``None``（不编造）。"""
    try:
        digest = hashlib.sha256()
        with Path(path).open("rb") as handle:
            for chunk in iter(lambda: handle.read(1024 * 1024), b""):
                digest.update(chunk)
        return digest.hexdigest()
    except OSError:
        return None


def write_atomic(path: Path | str, payload: bytes) -> Path:
    """原子写入（先写同目录临时文件再 ``os.replace``），避免半截产物被读到。"""
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


def write_manifest(task_id: str, manifest: dict[str, Any], *, root: Path | str | None = None) -> Path:
    """写 ``manifest.json``（UTF-8、缩进 2、``ensure_ascii=False``）。"""
    target = ensure_task_dir(task_id, root=root) / MANIFEST_NAME
    payload = json.dumps(manifest, ensure_ascii=False, indent=2, sort_keys=False).encode("utf-8")
    return write_atomic(target, payload)


def read_manifest(task_id: str, *, root: Path | str | None = None) -> dict[str, Any] | None:
    """读取 ``manifest.json``；不存在或损坏时返回 ``None``（不猜测内容）。"""
    try:
        path = task_dir(task_id, root=root) / MANIFEST_NAME
    except UnsafeTaskId:
        return None
    if not path.is_file():
        return None
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    return payload if isinstance(payload, dict) else None


def list_task_ids(*, root: Path | str | None = None, limit: int = 200) -> list[str]:
    """列出磁盘上已有产物的 task_id（用于重启后恢复查询）。"""
    base = Path(root) if root is not None else default_artifact_root()
    if not base.is_dir():
        return []
    found: list[str] = []
    for entry in base.iterdir():
        if not entry.is_dir() or not _TASK_ID_RE.match(entry.name):
            continue
        if (entry / MANIFEST_NAME).is_file():
            found.append(entry.name)
        if len(found) >= max(1, int(limit)):
            break
    return sorted(found)


def artifact_exists(task_id: str, name: str, *, root: Path | str | None = None) -> bool:
    """判断某个产物文件是否真实存在。"""
    try:
        path = task_dir(task_id, root=root) / name
    except UnsafeTaskId:
        return False
    return path.is_file()


def artifact_path(task_id: str, name: str, *, root: Path | str | None = None) -> Path:
    try:
        path = task_dir(task_id, root=root) / name
    except UnsafeTaskId:
        raise
    return path


__all__ = [
    "DUAL_NAME",
    "MANIFEST_NAME",
    "MAX_FILE_BYTES",
    "MAX_PAGES",
    "MAX_REQUEST_BYTES",
    "MONO_NAME",
    "PREVIEW_NAME",
    "SCHEMA_VERSION",
    "SOURCE_NAME",
    "UnsafeTaskId",
    "artifact_exists",
    "artifact_path",
    "default_artifact_root",
    "ensure_task_dir",
    "list_task_ids",
    "read_manifest",
    "safe_task_id",
    "sha256_bytes",
    "sha256_file",
    "task_dir",
    "write_atomic",
    "write_manifest",
]
