# Copyright 2026 SciLoop contributors
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
"""全文阅读器域的可预期错误（统一 ``{code,message,detail}`` + 显式 HTTP 状态码）。

设计原则
--------
- **错误码即契约**：路由层不猜状态码，直接读异常的 ``status_code``，
  这样「同一类失败」在服务层与 HTTP 层口径永远一致；
- **不把不确定说成成功**：``no_source_document`` / ``manifest_not_completed`` /
  ``version_already_registered`` 等一律显式失败，绝不静默降级成 200；
- **不可变专属码**：``reader_version_immutable`` 与数据库触发器、ORM 事件
  使用同一字面量，便于「三层同码」断言。
"""

from __future__ import annotations

from typing import Any

# --------------------------------------------------------------------------- #
# 状态码常量（避免在服务层 import fastapi）
# --------------------------------------------------------------------------- #
HTTP_404 = 404
HTTP_409 = 409
HTTP_422 = 422


class ReaderError(RuntimeError):
    """阅读器层可预期错误基类。"""

    code = "reader_error"
    status_code = HTTP_422

    def __init__(self, message: str, *, detail: Any = None) -> None:
        self.detail = detail
        super().__init__(message)

    def to_dict(self) -> dict[str, Any]:
        return {"code": self.code, "message": str(self), "detail": self.detail}


# --------------------------------------------------------------------------- #
# 404：对象不存在（禁止对未知 id 伪造结果）
# --------------------------------------------------------------------------- #
class DocumentNotFoundError(ReaderError):
    """阅读文档不存在。"""

    code = "document_not_found"
    status_code = HTTP_404


class VersionNotFoundError(ReaderError):
    """阅读版本不存在（或不属于该文档）。"""

    code = "version_not_found"
    status_code = HTTP_404


class AnnotationNotFoundError(ReaderError):
    """批注不存在（或不属于该文档）。"""

    code = "annotation_not_found"
    status_code = HTTP_404


class VersionArtifactMissingError(ReaderError):
    """版本 PDF / 文本索引在库目录中缺失（如实报错，不返回空文件）。"""

    code = "version_artifact_missing"
    status_code = HTTP_404


class ArchiveNotFoundError(ReaderError):
    """尚无可用归档（必须先 GET /archive 生成或由运维落盘）。"""

    code = "archive_not_found"
    status_code = HTTP_404


# --------------------------------------------------------------------------- #
# 409：状态冲突（存在性 / 乐观锁 / 引用完整性）
# --------------------------------------------------------------------------- #
class NoSourceDocumentError(ReaderError):
    """该论文没有可用原文（未导入本地 PDF），拒绝登记阅读文档。"""

    code = "no_source_document"
    status_code = HTTP_409


class VersionAlreadyRegisteredError(ReaderError):
    """同 ``kind`` 的版本已登记；版本不可变，不覆盖既有记录。"""

    code = "version_already_registered"
    status_code = HTTP_409


class AnnotationConflictError(ReaderError):
    """``revision`` 不匹配（旧客户端试图覆盖新修改）。"""

    code = "annotation_conflict"
    status_code = HTTP_409


class ReaderVersionImmutableError(ReaderError):
    """试图 UPDATE 已登记的不可变版本（与数据库触发器同码）。"""

    code = "reader_version_immutable"
    status_code = HTTP_409


# --------------------------------------------------------------------------- #
# 422：请求不合法（可读原因必须能指导用户改请求）
# --------------------------------------------------------------------------- #
class PageLimitExceededError(ReaderError):
    """页数超过上限。"""

    code = "page_limit_exceeded"


class SourceTooLargeError(ReaderError):
    """原文体积超过上限。"""

    code = "source_too_large"


class InvalidSourcePdfError(ReaderError):
    """原文不是可解析的 PDF。"""

    code = "invalid_source_pdf"


class UnknownBlockError(ReaderError):
    """block_id 不在该阅读文档的稳定 block 集合内。"""

    code = "unknown_block"


class StateValidationError(ReaderError):
    """阅读状态字段不合法（mode / font_size / offset / 列表上限）。"""

    code = "invalid_state"


class AnnotationValidationError(ReaderError):
    """批注字段不合法（空摘录、kind 非法、类型不符）。"""

    code = "invalid_annotation"


class ManifestInvalidError(ReaderError):
    """翻译产物 manifest 缺失 / 结构不符（schema_version / files / blocks）。"""

    code = "manifest_invalid"


class ManifestNotCompletedError(ReaderError):
    """manifest.status 不是 completed：产物不完整，拒绝登记为不可变版本。"""

    code = "manifest_not_completed"
    status_code = HTTP_409


class ManifestMismatchError(ReaderError):
    """manifest 与目标阅读文档不匹配（论文不一致 / 缺对应文件）。"""

    code = "manifest_mismatch"


class ArchiveTooLargeError(ReaderError):
    """归档超出体积上限。"""

    code = "archive_too_large"


class UnsafeArchivePathError(ReaderError):
    """归档条目名不安全（``..`` / 绝对路径 / 盘符）——拒绝写出与读取。"""

    code = "unsafe_archive_entry"


class ArchiveRestoreError(ReaderError):
    """归档内容无法恢复（结构缺失）——原有数据保持不变。"""

    code = "archive_restore_failed"


__all__ = [
    "ArchiveNotFoundError",
    "ArchiveRestoreError",
    "ArchiveTooLargeError",
    "AnnotationConflictError",
    "AnnotationNotFoundError",
    "AnnotationValidationError",
    "DocumentNotFoundError",
    "InvalidSourcePdfError",
    "ManifestInvalidError",
    "ManifestMismatchError",
    "ManifestNotCompletedError",
    "NoSourceDocumentError",
    "PageLimitExceededError",
    "ReaderError",
    "ReaderVersionImmutableError",
    "SourceTooLargeError",
    "StateValidationError",
    "UnknownBlockError",
    "UnsafeArchivePathError",
    "VersionAlreadyRegisteredError",
    "VersionArtifactMissingError",
    "VersionNotFoundError",
]
