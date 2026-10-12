# Copyright 2026 SciLoop contributors
# Licensed under the Apache License, Version 2.0 (the "License");
"""Readable identifiers shared by project, chat, and file naming."""

from datetime import datetime


def time_id() -> str:
    return datetime.now().strftime("%Y%m%d%H")


def readable_name(value: str, *, max_length: int = 180) -> str:
    """Normalize a display name without adding an internal identifier."""
    clean = " ".join(str(value or "").strip().split()) or "未命名"
    return clean[:max_length]
