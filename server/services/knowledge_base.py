# Copyright 2026 SciLoop contributors
# Licensed under the Apache License, Version 2.0 (the "License");
"""File-backed knowledge base for host deployments.

The manifest is deliberately human-readable JSON while the payloads remain ordinary
files. This keeps backups, inspection, and migration possible without the database.
"""

from __future__ import annotations

import contextlib
import json
import mimetypes
import os
import secrets
import stat
import threading
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from services.storage import knowledge_root, safe_folder, safe_name

_LOCK = threading.RLock()
_MANIFEST = "entries.json"
_FOLDERS = "folders.json"


def _now() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds")


def _manifest_path() -> Path:
    # 读路径不 mkdir：目录由 `_write_json`（写入时）保证存在，避免每个请求都做一轮建目录
    return knowledge_root() / "metadata" / _MANIFEST


def _folder_path() -> Path:
    return knowledge_root() / "metadata" / _FOLDERS


def _read_json(path: Path, fallback: Any) -> Any:
    try:
        return json.loads(path.read_text(encoding="utf-8-sig"))
    except (OSError, json.JSONDecodeError):
        return fallback


def _write_json(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_suffix(path.suffix + ".tmp")
    temp.write_text(json.dumps(value, ensure_ascii=False, indent=2), encoding="utf-8")
    temp.replace(path)


def _entries() -> list[dict[str, Any]]:
    value = _read_json(_manifest_path(), [])
    return value if isinstance(value, list) else []


def _folders() -> list[str]:
    value = _read_json(_folder_path(), [])
    return value if isinstance(value, list) else []


def _stamp_id() -> str:
    return f"kb-{datetime.now().strftime('%Y%m%d%H%M%S')}-{secrets.token_hex(3)}"


def _bucket_dir(bucket: str) -> str:
    return {"literature": "literature", "idea": "projects", "experiment": "projects", "paper": "projects", "memory": "memories"}.get(bucket, "uploads")


def _folder_list(value: Any) -> list[str]:
    """把 ``folder`` 统一成 ``list[str]``。

    ⚠️ 迁移进来的历史数据里有 ``folder: "uploads"`` 这类**字符串**（实测 915 条中 18 条），
    而前端按 ``string[]`` 消费（``folder.join('/')``）——**一条坏数据就能让整页渲染抛错**，
    界面永远停在骨架态（2026-10-09 实测的"知识库一直在闪烁"就是这个）。
    因此出口统一在这里归一，同时兼容路径拼接与文件夹筛选两处用法。
    """
    if value is None:
        return []
    if isinstance(value, str):
        parts = [piece for piece in value.replace("\\", "/").split("/") if piece.strip()]
        return safe_folder(parts)
    if isinstance(value, (list, tuple)):
        return safe_folder([str(piece) for piece in value])
    return []


def _payload_path(entry: dict[str, Any]) -> Path:
    """条目对应的磁盘路径。

    ⚠️ 这个函数在**列表**路径上会被逐条调用（本机实测 915 条），因此这里**只做纯字符串
    运算**：既不 mkdir（早期版本调用 `ensure_knowledge_root()`，每条目 10 次 mkdir，
    是列表 10s 里的一大块），也不做 `.resolve()`（Windows 上每次都会打 FS）。
    越界判断改用 `normpath` 折叠 `..` 后再做前缀比较，安全性与 resolve 版等价。
    """
    root = knowledge_root()
    relative_path = entry.get("relative_path")
    if isinstance(relative_path, str) and relative_path.strip():
        candidate = Path(os.path.normpath(root / relative_path))
        if candidate != root:
            try:
                candidate.relative_to(root)
                return candidate
            except ValueError:
                pass
    folder = _folder_list(entry.get("folder"))
    bucket = _bucket_dir(str(entry.get("bucket") or "uploads"))
    return root / bucket / Path(*folder) / str(entry["stored_name"])


#: 能在列表里直接内联正文的文本后缀（超过 2MB 的一律不读，避免列表被大文件拖死）
_INLINE_TEXT_SUFFIXES = {".md", ".txt", ".csv", ".json", ".py", ".js", ".ts", ".vue", ".yaml", ".yml"}


def _stat_file(path: Path) -> tuple[bool, int]:
    """一次 stat 同时拿到「是不是普通文件」和「字节数」（列表路径上每条都要问，省 syscall）。"""
    try:
        info = path.stat()
    except OSError:
        return False, 0
    return stat.S_ISREG(info.st_mode), int(info.st_size)


def _inline_text_ok(path: Path, size: int) -> bool:
    """这个文件**是否有一段可读的文本正文**（不读内容，只看后缀与大小）。"""
    if size == 0 or size > 2 * 1024 * 1024:
        return False
    if path.suffix.lower() in _INLINE_TEXT_SUFFIXES:
        return True
    mime = mimetypes.guess_type(path.name)[0] or ""
    return mime.startswith("text/")


def _public_entry(entry: dict[str, Any], *, with_content: bool = True) -> dict[str, Any]:
    """条目对外形态。

    ``with_content=False`` 用于**列表**：正文一律不回（本机实测 915 条会把响应撑到 46MB，
    前端因此长期卡在骨架态），改为给一个 ``has_content`` 布尔量让界面知道"这条有没有正文"，
    真正要看正文时再走单条详情接口。``file_url`` 两种情况都保留。
    """
    result = dict(entry)
    # folder 一律归一成 list[str]（历史数据里有字符串形态，见 _folder_list）
    result["folder"] = _folder_list(entry.get("folder"))
    path = _payload_path(entry)
    is_file, size = _stat_file(path)
    result["file_url"] = f"/api/v1/knowledge/files/{entry['id']}" if is_file else None
    result["has_content"] = bool(result.get("content")) or (is_file and _inline_text_ok(path, size))
    if not with_content:
        result["content"] = ""
    elif not result.get("content") and is_file and _inline_text_ok(path, size):
        with contextlib.suppress(OSError, UnicodeDecodeError):
            result["content"] = path.read_text(encoding="utf-8")
    result.pop("stored_name", None)
    result.pop("relative_path", None)
    return result


def _folder_prefixes(folder: list[str]) -> list[str]:
    return ["/".join(folder[:depth]) for depth in range(1, len(folder) + 1)]


def _folder_tree(all_entries: list[dict[str, Any]]) -> list[str]:
    """完整文件夹树（含每一级前缀）。

    ⚠️ `folders.json` 里可能只登记了**叶子路径**（实测本机只有 `conversations/100`、
    没有 `conversations`），而界面在根目录只列「深度正好 +1」的直接下级 →
    结果根目录几乎什么都不显示（只剩一个 uploads），看起来像"知识库里没东西"。
    因此这里按文件夹自身的每一级前缀补全。
    """
    paths: set[str] = set()
    for value in _folders():
        paths.update(_folder_prefixes(_folder_list(value)))
    for item in all_entries:
        paths.update(_folder_prefixes(_folder_list(item.get("folder"))))
    paths.discard("")
    return sorted(paths)


def snapshot(
    *, q: str = "", bucket: str | None = None, folder: list[str] | None = None, project_id: int | None = None
) -> dict[str, Any]:
    """列表快照：**只回元数据**（见 :func:`_public_entry` 的 ``with_content``）。"""
    needle = q.strip().lower()
    folder_key = "/".join(safe_folder(folder)) if folder is not None else None
    with _LOCK:
        all_entries = _entries()
        entries = []
        for item in all_entries:
            if bucket and item.get("bucket") != bucket:
                continue
            if folder_key is not None and "/".join(_folder_list(item.get("folder"))) != folder_key:
                continue
            if project_id is not None and item.get("project_id") != project_id:
                continue
            if needle and needle not in json.dumps(item, ensure_ascii=False).lower():
                continue
            entries.append(_public_entry(item, with_content=False))
        return {"items": entries, "folders": _folder_tree(all_entries)}


def get(entry_id: str) -> dict[str, Any] | None:
    """单条详情（**含正文**）。"""
    with _LOCK:
        for item in _entries():
            if item.get("id") == entry_id:
                return _public_entry(item)
    return None


def details(entry_ids: list[str]) -> list[dict[str, Any]]:
    """批量详情（**含正文**），供导出这类"要正文"的操作一次取回。"""
    wanted = {str(value) for value in entry_ids if str(value)}
    if not wanted:
        return []
    found: list[dict[str, Any]] = []
    with _LOCK:
        for item in _entries():
            if item.get("id") in wanted:
                found.append(_public_entry(item))
    return found


def _save(entries: list[dict[str, Any]]) -> None:
    _write_json(_manifest_path(), entries)


def create(*, name: str, bucket: str, content: str = "", data: bytes | None = None, folder: list[str] | None = None, tags: list[str] | None = None, project_id: int | None = None, source_label: str = "本地上传", source_route: str | None = None) -> dict[str, Any]:
    stamp = _now()
    entry_id = _stamp_id()
    clean_folder = safe_folder(folder)
    readable = safe_name(name, fallback="资料")
    suffix = Path(readable).suffix
    stem = safe_name(Path(readable).stem, fallback="资料")
    stored_name = f"{stem}__{datetime.now().strftime('%Y%m%d%H')}-{secrets.token_hex(2)}{suffix}"
    entry = {
        "id": entry_id,
        "name": readable,
        "stored_name": stored_name,
        "bucket": bucket if bucket in {"literature", "idea", "experiment", "paper", "memory"} else "literature",
        "content": content or "",
        "size": len(data) if data is not None else len((content or "").encode("utf-8")),
        "folder": clean_folder,
        "tags": [str(tag).strip() for tag in (tags or []) if str(tag).strip()],
        "project_id": project_id,
        "source_label": source_label,
        "source_route": source_route,
        "imported_at": stamp,
        "modified_at": stamp,
        "moved_at": None,
        "trashed_at": None,
    }
    path = _payload_path(entry)
    path.parent.mkdir(parents=True, exist_ok=True)
    if data is not None:
        path.write_bytes(data)
        if not content and path.suffix.lower() in {".md", ".txt", ".csv", ".json", ".py", ".js", ".ts", ".vue", ".yaml", ".yml"} and len(data) <= 2 * 1024 * 1024:
            entry["content"] = data.decode("utf-8", errors="replace")
    elif content:
        path.write_text(content, encoding="utf-8")
    with _LOCK:
        entries = _entries()
        entries.insert(0, entry)
        _save(entries)
        folders = set(_folders())
        for depth in range(1, len(clean_folder) + 1):
            folders.add("/".join(clean_folder[:depth]))
        _write_json(_folder_path(), sorted(folders))
    return _public_entry(entry)


def update(entry_id: str, patch: dict[str, Any]) -> dict[str, Any] | None:
    with _LOCK:
        entries = _entries()
        target = next((item for item in entries if item.get("id") == entry_id), None)
        if target is None:
            return None
        old_path = _payload_path(target)
        migrated_path = bool(target.get("relative_path"))
        if "name" in patch:
            target["name"] = safe_name(str(patch["name"]), fallback=target["name"])
        if "folder" in patch:
            target["folder"] = _folder_list(patch.get("folder"))
        for key in ("bucket", "tags", "project_id", "source_label", "source_route", "content"):
            if key in patch:
                target[key] = patch[key]
        target["modified_at"] = _now()
        if migrated_path and any(key in patch for key in ("name", "folder", "bucket")):
            target.pop("relative_path", None)
            target["stored_name"] = safe_name(str(target.get("name") or "资料"), fallback="资料")
        new_path = _payload_path(target)
        new_path.parent.mkdir(parents=True, exist_ok=True)
        if old_path != new_path and old_path.exists():
            old_path.replace(new_path)
        if "content" in patch:
            new_path.write_text(str(patch["content"] or ""), encoding="utf-8")
            target["size"] = new_path.stat().st_size
        _save(entries)
        return _public_entry(target)


def trash(entry_ids: list[str], restore: bool = False) -> int:
    with _LOCK:
        entries = _entries()
        changed = 0
        for item in entries:
            if item.get("id") in entry_ids and bool(item.get("trashed_at")) is not restore:
                item["trashed_at"] = None if restore else _now()
                changed += 1
        _save(entries)
        return changed


def delete(entry_ids: list[str]) -> int:
    with _LOCK:
        entries = _entries()
        remain = []
        deleted = 0
        for item in entries:
            if item.get("id") in entry_ids:
                with contextlib.suppress(OSError):
                    _payload_path(item).unlink(missing_ok=True)
                deleted += 1
            else:
                remain.append(item)
        _save(remain)
        return deleted


def create_folder(folder: list[str]) -> list[str]:
    clean = safe_folder(folder)
    with _LOCK:
        folders = set(_folders())
        for depth in range(1, len(clean) + 1):
            folders.add("/".join(clean[:depth]))
        _write_json(_folder_path(), sorted(folders))
        return sorted(folders)


def payload_path(entry_id: str) -> Path | None:
    with _LOCK:
        for item in _entries():
            if item.get("id") == entry_id:
                path = _payload_path(item)
                return path if path.is_file() else None
    return None
