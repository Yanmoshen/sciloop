# Copyright 2026 SciLoop contributors
# Licensed under the Apache License, Version 2.0 (the "License");
"""Readable identifiers shared by project, chat, and file naming."""

from datetime import datetime
import re

_TIME_ID = re.compile(r"__\d{10}(?:-\d+)?$")


def time_id() -> str:
    return datetime.now().strftime("%Y%m%d%H")


def readable_name(value: str, *, identifier: str | None = None, max_length: int = 180) -> str:
    clean = " ".join(str(value or "").strip().split()) or "未命名"
    clean = clean[:max_length]
    if _TIME_ID.search(clean):
        return clean
    return f"{clean}__{identifier or time_id()}"
