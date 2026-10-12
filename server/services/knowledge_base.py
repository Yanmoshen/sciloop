# Copyright 2026 SciLoop contributors
# Licensed under the Apache License, Version 2.0 (the "License");
"""File-backed knowledge base for host deployments.

The manifest is deliberately human-readable JSON while the payloads remain ordinary
files. This keeps backups, inspection, and migration possible without the database.
"""

from __future__ import annotations

import contextlib
import hashlib
import json
import mimetypes
import os
import secrets
import stat
import threading
from collections import Counter
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


def _entry_identity(entry: dict[str, Any]) -> str:
    """Return the stable identity used to disambiguate legacy duplicate IDs."""
    relative = str(entry.get("relative_path") or "").replace("\\", "/").strip()
    if relative:
        return relative
    payload = {key: value for key, value in entry.items() if key != "id"}
    return json.dumps(payload, ensure_ascii=False, sort_keys=True, default=str)


def _unique_ids(entries: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Make IDs unique in memory without moving or deleting any payload files.

    Early directory migrations assigned ``sha256(name)``-style IDs, so unrelated
    files with the same legacy key can coexist in ``entries.json``.  The API uses
    the ID in URLs and front-end keys; returning duplicates makes both ambiguous.
    Keep modern unique IDs untouched and derive deterministic IDs for only the
    colliding records.  The manifest is not written during reads.
    """
    counts = Counter(str(item.get("id") or "") for item in entries)
    used: set[str] = set()
    for item in entries:
        raw = str(item.get("id") or "").strip()
        if counts[raw] <= 1 and raw and raw not in used:
            item["id"] = raw
            used.add(raw)
            continue

        identity = _entry_identity(item)
        digest = hashlib.sha256(identity.encode("utf-8")).hexdigest()[:16]
        base = f"{raw or 'kb-entry'}-path-{digest}"
        candidate = base
        suffix = 2
        while candidate in used:
            candidate = f"{base}-{suffix}"
            suffix += 1
        item["id"] = candidate
        used.add(candidate)
    return entries


def _all_entries() -> list[dict[str, Any]]:
    """发现项目 Agent 写入的自产材料；只登记元数据，不代写论文或复制文件。"""
    entries = _entries()
    known = {str(item.get("relative_path") or "").replace("\\", "/") for item in entries}
    root = knowledge_root()
    generated_roots = (
        root / "projects" / "generated-papers",
        root / "projects" / "research-preflight",
    )
    if not any(path.is_dir() for path in generated_roots):
        return _unique_ids(entries)
    resolved_root = root.resolve()
    manifest_project_ids: dict[Path, int | None] = {}

    def declared_project_id(project_dir: Path) -> int | None:
        """Read only an explicit numeric mapping from a project manifest."""
        if project_dir in manifest_project_ids:
            return manifest_project_ids[project_dir]
        project_id: int | None = None
        manifest = project_dir / "project_manifest.json"
        try:
            raw_manifest = json.loads(manifest.read_text(encoding="utf-8"))
            raw_project_id = raw_manifest.get("db_project_id")
            if isinstance(raw_project_id, int) and raw_project_id > 0:
                project_id = raw_project_id
        except (OSError, ValueError, TypeError):
            pass
        manifest_project_ids[project_dir] = project_id
        return project_id

    for generated in generated_roots:
        if not generated.is_dir():
            continue
        for path in generated.rglob("*"):
            if not path.is_file() or path.is_symlink() or any(part.startswith(".") or part == "__pycache__" for part in path.relative_to(generated).parts):
                continue
            try:
                path.resolve().relative_to(resolved_root)
                relative = path.relative_to(root).as_posix()
                info = path.stat()
            except (OSError, ValueError):
                continue
            if relative in known:
                continue
            parts = relative.split("/")
            # A generated project may opt into the numeric project filter by
            # declaring an explicit mapping in its manifest.  Do not infer IDs
            # from display names: the mapping must be auditable and intentional.
            project_dir = generated / path.relative_to(generated).parts[0]
            project_id = declared_project_id(project_dir)
            external = "sources" in parts and "external" in parts
            manuscript = "manuscript" in parts
            bucket = "literature" if external else "paper" if manuscript and path.suffix.lower() in {".pdf", ".md", ".tex", ".docx", ".html"} else "experiment"
            stamp = datetime.fromtimestamp(info.st_mtime, UTC).isoformat(timespec="seconds")
            entries.append({
                "id": "kb-generated-" + hashlib.sha256(relative.encode("utf-8")).hexdigest()[:16],
                "name": path.name, "stored_name": path.name, "relative_path": relative,
                "bucket": bucket, "content": "", "size": info.st_size,
                "folder": parts[:-1], "tags": [], "project_id": project_id,
                "provenance": "external" if external else "generated",
                "source_label": "外部文献" if external else "自产论文" if bucket == "paper" else "自产研究材料",
                "source_route": None, "imported_at": stamp, "modified_at": stamp,
                "moved_at": None, "trashed_at": None,
            })
    # Existing manifest rows predate the explicit mapping and are skipped by
    # ``known`` above. Enrich their in-memory projection so project filtering
    # works immediately without rewriting entries.json.
    for item in entries:
        relative = str(item.get("relative_path") or "").replace("\\", "/")
        for generated in generated_roots:
            prefix = generated.relative_to(root).as_posix() + "/"
            if not relative.startswith(prefix):
                continue
            suffix = relative[len(prefix):]
            project_name = suffix.split("/", 1)[0]
            project_id = declared_project_id(generated / project_name)
            if project_id is not None:
                item["project_id"] = project_id
            break
    return _unique_ids(entries)


_DISK_INDEX_EXCLUDED_DIRS = {"metadata", "trash"}


def _disk_entry_bucket(relative: str, path: Path) -> str:
    """Choose a display bucket for a file imported from the knowledge root."""
    parts = relative.split("/")
    top = parts[0] if parts else ""
    if top == "literature":
        return "literature"
    if top == "memories":
        return "memory"
    if top == "projects":
        if "generated-papers" in parts:
            if "sources" in parts and "external" in parts:
                return "literature"
            if "manuscript" in parts and path.suffix.lower() in {".pdf", ".md", ".tex", ".docx", ".html"}:
                return "paper"
        return "experiment"
    if top == "conversations":
        return "memory"
    return "literature"


def _disk_stamp(path: Path) -> tuple[int, str]:
    info = path.stat()
    return int(info.st_size), datetime.fromtimestamp(info.st_mtime, UTC).isoformat(timespec="seconds")


def sync_disk_index() -> dict[str, int]:
    """Persist a manifest projection of the files and folders on disk.

    The manifest remains the place for user metadata, while ``relative_path`` is
    the stable identity of a physical file.  Missing historical rows are kept so
    a sync cannot silently discard tags, project links, or trash state.
    """
    with _LOCK:
        root = knowledge_root()
        root.mkdir(parents=True, exist_ok=True)
        entries = _entries()
        by_relative: dict[str, dict[str, Any]] = {}
        for item in entries:
            relative = str(item.get("relative_path") or "").replace("\\", "/").strip("/")
            if not relative:
                with contextlib.suppress(OSError, ValueError):
                    relative = _payload_path(item).relative_to(root).as_posix()
            if relative:
                by_relative.setdefault(relative, item)

        physical_folders: set[str] = set()
        scanned = added = updated = skipped = 0
        for current, dirnames, filenames in os.walk(root, topdown=True, followlinks=False):
            current_path = Path(current)
            try:
                current_relative = current_path.relative_to(root).as_posix()
            except ValueError:
                dirnames[:] = []
                continue
            if current_relative and current_relative.split("/", 1)[0] in _DISK_INDEX_EXCLUDED_DIRS:
                dirnames[:] = []
                continue
            dirnames[:] = [
                name for name in dirnames
                if not name.startswith(".") and name != "__pycache__" and name not in _DISK_INDEX_EXCLUDED_DIRS
                and not (current_path / name).is_symlink()
            ]
            for name in dirnames:
                relative_dir = (current_path / name).relative_to(root).as_posix()
                physical_folders.update(_folder_prefixes(relative_dir.split("/")))
            for name in filenames:
                path = current_path / name
                if name.startswith(".") or name.endswith(".tmp") or path.is_symlink():
                    skipped += 1
                    continue
                try:
                    relative = path.relative_to(root).as_posix()
                    size, stamp = _disk_stamp(path)
                except (OSError, ValueError):
                    skipped += 1
                    continue
                scanned += 1
                parts = relative.split("/")
                physical_folders.update(_folder_prefixes(parts[:-1]))
                item = by_relative.get(relative)
                if item is None:
                    item = {
                        "id": "kb-disk-" + hashlib.sha256(relative.encode("utf-8")).hexdigest()[:16],
                        "name": path.name,
                        "stored_name": path.name,
                        "relative_path": relative,
                        "bucket": _disk_entry_bucket(relative, path),
                        "content": "",
                        "size": size,
                        "folder": parts[:-1],
                        "tags": [],
                        "project_id": None,
                        "provenance": "disk",
                        "source_label": "磁盘索引",
                        "source_route": None,
                        "imported_at": stamp,
                        "modified_at": stamp,
                        "moved_at": None,
                        "trashed_at": None,
                    }
                    entries.append(item)
                    by_relative[relative] = item
                    added += 1
                    continue
                changed = (
                    item.get("relative_path") != relative
                    or item.get("stored_name") != path.name
                    or _folder_list(item.get("folder")) != parts[:-1]
                    or item.get("size") != size
                    or item.get("modified_at") != stamp
                )
                item["relative_path"] = relative
                item["stored_name"] = path.name
                item["folder"] = parts[:-1]
                item["size"] = size
                item["modified_at"] = stamp
                if changed:
                    updated += 1

        old_folders = set(_folders())
        _save(_unique_ids(entries))
        _write_json(_folder_path(), sorted(physical_folders))
        return {
            "files_scanned": scanned,
            "files_added": added,
            "files_updated": updated,
            "folders_found": len(physical_folders),
            "folders_added": len(physical_folders - old_folders),
            "folders_removed": len(old_folders - physical_folders),
            "skipped": skipped,
        }


def _project_classification(entry: dict[str, Any]) -> dict[str, Any]:
    """修正旧迁移分类的公共投影；不会删除文件或改用户主动上传的同名资料。"""
    result = dict(entry)
    relative = str(entry.get("relative_path") or "").replace("\\", "/")
    parts = relative.split("/")
    if str(entry.get("id") or "").startswith("kb-migrated-"):
        # 历史迁移把解析缓存、任务状态和翻译元数据统一标成了论文。
        in_pipeline = parts[0] in {"translations", "parses"}
        metadata = Path(relative).suffix.lower() in {".json", ".jsonl"} or Path(relative).name == "preview.html"
        known_internal = relative in {
            "projects/tasks/fetch_tasks.jsonl",
            "parses/fulltext-cache/999999__wp01-probe#persist.txt",
        }
        if (in_pipeline and metadata) or known_internal or relative.startswith(("translations/_probe/", "translations/_skills/")):
            result["provenance"] = "internal"
        elif in_pipeline:
            result["bucket"] = "literature" if Path(relative).suffix.lower() == ".pdf" else "experiment"
            result["provenance"] = "derived" if parts[0] == "translations" or result["bucket"] == "experiment" else "external"
            result["source_label"] = "翻译产物" if parts[0] == "translations" else "外部文献" if result["bucket"] == "literature" else "解析产物"
        result["source_route"] = None
    result.setdefault("provenance", "external" if entry.get("source_label") == "本地上传" else "generated" if entry.get("source_label") == "手动新建" else "unknown")
    return result


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
_INLINE_TEXT_SUFFIXES = {
    ".md",
    ".txt",
    ".csv",
    ".json",
    ".py",
    ".js",
    ".mjs",
    ".cjs",
    ".ts",
    ".tsx",
    ".jsx",
    ".vue",
    ".yaml",
    ".yml",
    ".toml",
    ".ini",
    ".xml",
    ".html",
    ".css",
    ".sql",
    ".sh",
    ".bash",
    ".r",
    ".m",
    ".tex",
    ".bib",
}


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
    result = _project_classification(entry)
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
        all_entries = _all_entries()
        entries = []
        for item in all_entries:
            classified = _project_classification(item)
            if classified.get("provenance") == "internal":
                continue
            if bucket and classified.get("bucket") != bucket:
                continue
            if folder_key is not None and "/".join(_folder_list(item.get("folder"))) != folder_key:
                continue
            if project_id is not None and item.get("project_id") != project_id:
                continue
            if needle and needle not in json.dumps(item, ensure_ascii=False).lower():
                continue
            entries.append(_public_entry(item, with_content=False))
        return {"items": entries, "folders": _folder_tree([item for item in all_entries if _project_classification(item).get("provenance") != "internal"])}


def get(entry_id: str) -> dict[str, Any] | None:
    """单条详情（**含正文**）。"""
    with _LOCK:
        for item in _all_entries():
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
        for item in _all_entries():
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
        "provenance": "external" if data is not None or source_label == "本地上传" else "generated",
        "imported_at": stamp,
        "modified_at": stamp,
        "moved_at": None,
        "trashed_at": None,
    }
    if bucket == "paper":
        # 导入原文与自产手稿各自拥有物理目录，不能只换前端标签。
        base = ["literature", "imported-papers"] if entry["provenance"] == "external" else ["projects", "generated-papers"]
        path_parts = clean_folder if clean_folder[:len(base)] == base else [*base, *clean_folder]
        entry["folder"] = path_parts
        entry["relative_path"] = "/".join([*path_parts, stored_name])
        entry["source_label"] = "外部导入论文" if entry["provenance"] == "external" else "自产论文"
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
        for depth in range(1, len(entry["folder"]) + 1):
            folders.add("/".join(entry["folder"][:depth]))
        _write_json(_folder_path(), sorted(folders))
    return _public_entry(entry)


def update(entry_id: str, patch: dict[str, Any]) -> dict[str, Any] | None:
    with _LOCK:
        entries = _all_entries()
        target = next((item for item in entries if item.get("id") == entry_id), None)
        if target is None:
            return None
        old_path = _payload_path(target)
        migrated_path = bool(target.get("relative_path"))
        old_name = target.get("name")
        old_folder = _folder_list(target.get("folder"))
        if "name" in patch:
            target["name"] = safe_name(str(patch["name"]), fallback=target["name"])
        if "folder" in patch:
            target["folder"] = _folder_list(patch.get("folder"))
        for key in ("bucket", "tags", "project_id", "source_label", "source_route", "content"):
            if key in patch:
                target[key] = patch[key]
        target["modified_at"] = _now()
        if migrated_path and (target.get("name") != old_name or _folder_list(target.get("folder")) != old_folder):
            relative = Path(str(target["relative_path"]))
            clean_folder = _folder_list(target.get("folder"))
            parent = Path(*clean_folder) if clean_folder != old_folder else relative.parent
            stored_name = safe_name(str(target.get("name") or "资料"), fallback="资料") if target.get("name") != old_name else str(target["stored_name"])
            target["relative_path"] = (parent / stored_name).as_posix()
            target["stored_name"] = stored_name
        new_path = _payload_path(target)
        new_path.parent.mkdir(parents=True, exist_ok=True)
        if old_path != new_path and old_path.exists():
            if new_path.exists():
                raise ValueError("目标文件夹中已有同名文件，请更换文件名")
            old_path.replace(new_path)
        if "content" in patch:
            new_path.write_text(str(patch["content"] or ""), encoding="utf-8")
            target["size"] = new_path.stat().st_size
        _save(entries)
        return _public_entry(target)


def trash(entry_ids: list[str], restore: bool = False) -> int:
    with _LOCK:
        entries = _all_entries()
        changed = 0
        for item in entries:
            if item.get("id") in entry_ids and bool(item.get("trashed_at")) is restore:
                item["trashed_at"] = None if restore else _now()
                changed += 1
        _save(entries)
        return changed


def delete(entry_ids: list[str]) -> int:
    with _LOCK:
        entries = _all_entries()
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
        if clean:
            (knowledge_root() / Path(*clean)).mkdir(parents=True, exist_ok=True)
        folders = set(_folders())
        for depth in range(1, len(clean) + 1):
            folders.add("/".join(clean[:depth]))
        _write_json(_folder_path(), sorted(folders))
        return sorted(folders)


def payload_path(entry_id: str) -> Path | None:
    with _LOCK:
        for item in _all_entries():
            if item.get("id") == entry_id:
                path = _payload_path(item)
                return path if path.is_file() else None
    return None
