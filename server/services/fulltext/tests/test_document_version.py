# Copyright 2026 SciLoop contributors
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
"""``document_version`` 构成：**必须带解析器标识**。

要钉住的核心事实（2026-09-24 实测出来的真事故）：
不含解析器时，**换了 parser 也算同一个版本** → 重解析会在同一个版本上 DELETE 旧 span 再 INSERT；
而旧 span 可能已被 ``evidences.paper_span_id`` 外键引用（**没有级联删除**）→
整次重解析被数据库拒绝（实测 32 次 FK violation，约占三成），表现为
"`parser_version` 被改了、spans 却还是旧的"这种自相矛盾的状态。
"""

from __future__ import annotations

import pytest

from services.fulltext.records import (
    DOCUMENT_VERSION_MAX_LEN,
    build_document_version,
)

_URL = "https://arxiv.org/html/2609.19145"
_HASH = "a" * 64


def test_version_contains_parser_identity() -> None:
    version = build_document_version(_URL, _HASH, "arxiv_html", "1.1.1")
    assert version.endswith("#aaaaaaaaaaaa-arxiv_html-1.1.1"), version
    assert version.startswith(_URL), version


def test_same_content_different_parser_yields_different_version() -> None:
    """**回归用例**：同一份内容、不同解析器必须是**两个版本**。

    这一条若失效，换解析器就会回到"覆盖同一版本 → 撞 evidences 外键"的老路。
    """

    html = build_document_version(_URL, _HASH, "arxiv_html", "1.1.1")
    pdf = build_document_version(_URL, _HASH, "pymupdf", "1.0.0")
    bumped = build_document_version(_URL, _HASH, "arxiv_html", "1.2.0")

    assert html != pdf, "换解析器必须换版本"
    assert html != bumped, "解析器升版也必须换版本"


def test_version_without_parser_still_supported() -> None:
    """不传解析器时退回旧构成（兼容调用方）。"""

    version = build_document_version(_URL, _HASH)
    assert version == f"{_URL}#{'a' * 12}"


def test_version_is_within_column_limit() -> None:
    """``paper_documents.document_version`` 是 VARCHAR(64)，长 URL 必须靠截断兜住。"""

    long_url = "https://arxiv.org/html/2609.19145?x=" + "y" * 200
    version = build_document_version(long_url, _HASH, "arxiv_html", "1.1.1")
    assert len(version) <= DOCUMENT_VERSION_MAX_LEN, len(version)
    # 尾部哈希与解析器标识必须保住（那是去重与可追溯的关键）
    assert version.endswith("-arxiv_html-1.1.1"), version
    assert "aaaaaaaaaaaa" in version, version


def test_version_tag_keeps_only_safe_characters() -> None:
    version = build_document_version(_URL, _HASH, "arxiv html/../x", "1.1.1 ")
    assert " " not in version and "/" not in version.split("#", 1)[1]
    assert len(version) <= DOCUMENT_VERSION_MAX_LEN


@pytest.mark.parametrize("url", ["", None])
def test_empty_source_url_is_rejected(url: str | None) -> None:
    with pytest.raises(ValueError):
        build_document_version(url, _HASH)  # type: ignore[arg-type]


def test_short_content_hash_is_rejected() -> None:
    with pytest.raises(ValueError):
        build_document_version(_URL, "abc")
