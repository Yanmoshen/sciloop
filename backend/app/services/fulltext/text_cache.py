# Copyright 2026 SciLoop contributors
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
"""归一全文的本地文本缓存（字符偏移校验用）。

为什么需要它
------------

``paper_documents`` 表里没有正文列（附录 A.2 的字段是固定的），
但 ``verify_span`` 要做"偏移比对"就必须能拿到该 ``document_version`` 的
归一全文。这里把全文落到磁盘缓存，键为 ``(paper_id, document_version)``：

- 缓存命中 → ``offset_match`` 可以真实判定，可能得到 ``valid``；
- 缓存缺失（如容器重启、``/tmp`` 被清理）→ ``offset_match=None``，
  verdict 退化为 ``valid_by_hash``，**绝不伪报通过**。

缓存目录默认放在系统临时目录（不污染仓库），可用
``FULLTEXT_TEXT_CACHE_DIR`` 覆盖；写入是原子替换，避免半截文件。
"""

from __future__ import annotations

import contextlib
import os
import re
import tempfile
from pathlib import Path

_UNSAFE_RE = re.compile(r"[^A-Za-z0-9._#-]")

#: 单篇全文缓存上限（字节），超过则不落盘（避免异常大文件占用磁盘）
MAX_CACHE_BYTES = 8 * 1024 * 1024


def default_cache_dir() -> Path:
    override = os.environ.get("FULLTEXT_TEXT_CACHE_DIR")
    if override:
        return Path(override)
    return Path(tempfile.gettempdir()) / "sciloop_fulltext_text"


class TextCache:
    """按 ``(paper_id, document_version)`` 存取归一全文的磁盘缓存。"""

    def __init__(self, root: Path | str | None = None) -> None:
        self.root = Path(root) if root is not None else default_cache_dir()

    def path_for(self, paper_id: int, document_version: str) -> Path:
        safe_version = _UNSAFE_RE.sub("_", str(document_version))[:80]
        return self.root / f"{int(paper_id)}__{safe_version}.txt"

    def put(self, paper_id: int, document_version: str, text: str) -> Path | None:
        if text is None:
            return None
        payload = text.encode("utf-8")
        if not payload or len(payload) > MAX_CACHE_BYTES:
            return None
        target = self.path_for(paper_id, document_version)
        try:
            target.parent.mkdir(parents=True, exist_ok=True)
            fd, tmp_name = tempfile.mkstemp(dir=str(target.parent), suffix=".tmp")
            with os.fdopen(fd, "wb") as handle:
                handle.write(payload)
            os.replace(tmp_name, target)
        except OSError:
            return None
        return target

    def get(self, paper_id: int, document_version: str) -> str | None:
        target = self.path_for(paper_id, document_version)
        try:
            return target.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError):
            return None

    def drop(self, paper_id: int, document_version: str) -> None:
        with contextlib.suppress(OSError):
            self.path_for(paper_id, document_version).unlink()

    def has(self, paper_id: int, document_version: str) -> bool:
        return self.path_for(paper_id, document_version).exists()


_default: TextCache | None = None


def default_text_cache() -> TextCache:
    """进程内默认缓存实例（延迟创建，便于测试改环境变量）。"""
    global _default
    if _default is None:
        _default = TextCache()
    return _default


def reset_default_text_cache() -> None:
    """测试辅助：清空默认实例。"""
    global _default
    _default = None
