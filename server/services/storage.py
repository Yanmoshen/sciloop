# Copyright 2026 SciLoop contributors
# Licensed under the Apache License, Version 2.0 (the "License");
"""Paths for durable host deployments."""

from functools import lru_cache
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


@lru_cache(maxsize=1)
def repo_root() -> Path:
    return Path(__file__).resolve().parents[2]


@lru_cache(maxsize=8)
def _resolved_knowledge_root(configured: str) -> Path:
    """按「配置值」缓存解析结果。

    ⚠️ `Path.resolve()` 在 Windows 上会打 FS：知识库列表是**逐条**调用本函数的
    （本机实测 915 条），不做缓存的话光这一项就吃掉近 1 秒（实测 1.85s → 约 0.5s）。
    缓存键取配置字符串本身，因此改了 `KNOWLEDGE_BASE_DIR` 仍会重新解析。
    """
    path = Path(configured).expanduser()
    return (repo_root() / path).resolve() if not path.is_absolute() else path.resolve()


def knowledge_root() -> Path:
    return _resolved_knowledge_root(get_settings().knowledge_base_dir)


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
