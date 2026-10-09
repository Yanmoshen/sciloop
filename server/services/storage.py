# Copyright 2026 SciLoop contributors
# Licensed under the Apache License, Version 2.0 (the "License");
"""Paths for durable host deployments."""

from pathlib import Path

from core.config import get_settings


BUCKET_DIRS = (
    "projects",
    "conversations",
    "parses",
    "translations",
    "literature",
    "memories",
    "uploads",
    "exports",
    "metadata",
    "trash",
)


def repo_root() -> Path:
    return Path(__file__).resolve().parents[2]


def knowledge_root() -> Path:
    configured = Path(get_settings().knowledge_base_dir).expanduser()
    return (repo_root() / configured).resolve() if not configured.is_absolute() else configured.resolve()


def ensure_knowledge_root() -> Path:
    root = knowledge_root()
    root.mkdir(parents=True, exist_ok=True)
    for name in BUCKET_DIRS:
        (root / name).mkdir(parents=True, exist_ok=True)
    return root


def safe_name(value: str, *, fallback: str = "未命名") -> str:
    value = "".join(ch for ch in str(value or "").strip() if ch not in '<>:/\\|?*\"' and ord(ch) >= 32)
    value = value.strip(" .")
    return value[:180] or fallback


def safe_folder(folder: list[str] | tuple[str, ...] | None) -> list[str]:
    return [safe_name(part, fallback="文件夹") for part in (folder or []) if str(part).strip()]
