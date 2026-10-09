# Copyright 2026 SciLoop contributors
# Licensed under the Apache License, Version 2.0 (the "License");
"""File-backed knowledge base for host deployments.

The manifest is deliberately human-readable JSON while the payloads remain ordinary
files. This keeps backups, inspection, and migration possible without the database.
"""

from __future__ import annotations

import json
import mimetypes
import secrets
import threading
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from services.storage import ensure_knowledge_root, safe_folder, safe_name

_LOCK = threading.RLock()
_MANIFEST = "entries.json"
_FOLDERS = "folders.json"


def _now() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds")


def _manifest_path() -> Path:
    return ensure_knowledge_root() / "metadata" / _MANIFEST


def _folder_path() -> Path:
    return ensure_knowledge_root() / "metadata" / _FOLDERS


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


def _payload_path(entry: dict[str, Any]) -> Path:
    root = ensure_knowledge_root()
    relative_path = entry.get("relative_path")
    if isinstance(relative_path, str) and relative_path.strip():
        candidate = (root / relative_path).resolve()
        if candidate != root and root in candidate.parents:
            return candidate
    folder = safe_folder(entry.get("folder"))
    bucket = _bucket_dir(str(entry.get("bucket") or "uploads"))
    return root / bucket / Path(*folder) / str(entry["stored_name"])


def _public_entry(entry: dict[str, Any]) -> dict[str, Any]:
    result = dict(entry)
    path = _payload_path(entry)
    result["file_url"] = f"/api/v1/knowledge/files/{entry['id']}" if path.is_file() else None
    if not result.get("content") and path.is_file() and path.stat().st_size <= 2 * 1024 * 1024:
        mime = mimetypes.guess_type(path.name)[0] or ""
        if mime.startswith("text/") or path.suffix.lower() in {".md", ".txt", ".csv", ".json", ".py", ".js", ".ts", ".vue", ".yaml", ".yml"}:
            try:
                result["content"] = path.read_text(encoding="utf-8")
            except (OSError, UnicodeDecodeError):
                pass
    result.pop("stored_name", None)
    result.pop("relative_path", None)
    return result


def snapshot(*, q: str = "", bucket: str | None = None, folder: list[str] | None = None, project_id: int | None = None) -> dict[str, Any]:
    needle = q.strip().lower()
    folder_key = "/".join(safe_folder(folder)) if folder is not None else None
    with _LOCK:
        entries = []
        for item in _entries():
            if bucket and item.get("bucket") != bucket:
                continue
            if folder_key is not None and "/".join(item.get("folder") or []) != folder_key:
                continue
            if project_id is not None and item.get("project_id") != project_id:
                continue
            if needle and needle not in json.dumps(item, ensure_ascii=False).lower():
                continue
            entries.append(_public_entry(item))
        return {"items": entries, "folders": sorted(_folders())}


def get(entry_id: str) -> dict[str, Any] | None:
    with _LOCK:
        for item in _entries():
            if item.get("id") == entry_id:
                return _public_entry(item)
    return None


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
            target["folder"] = safe_folder(patch.get("folder"))
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
                try:
                    _payload_path(item).unlink(missing_ok=True)
                except OSError:
                    pass
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
