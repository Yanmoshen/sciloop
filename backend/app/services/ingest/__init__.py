# Copyright 2026 SciLoop contributors
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
"""论文导入服务（PDF 上传 + DOI/arXiv ID 批量导入）。

对外入口（契约，供 ``app/api/v1/imports.py`` 使用）::

    from app.services.ingest import jobs, pdf_import, storage

    submitted = jobs.submit_pdf_import(files=[...], project_id=1)   # -> {task_id, ...}
    task = jobs.get_task(task_id)                                   # -> 轮询快照

子模块职责
----------

- ``storage``：落盘与文件名安全化（禁止任意路径写入）。
- ``jobs``：内存任务注册表 + 后台线程执行（任务状态**不持久化**，进程重启即丢失）。
- ``pdf_import``：PDF 解析（复用 WP05 ``parse_pdf``）+ 身份映射
  （复用 WP03 ``upsert_paper``）+ ``paper_documents`` 落库。
- ``identifier_import``：DOI / arXiv ID 单条与批量导入（**由并行工作包实现**，
  本包只调用；因此**不在此处顶层 import**，避免其缺失时连锁 ImportError）。

设计约束（见 assets/tasks/contracts.json）
-----------------------------------------

- 禁止编造：解析/取数失败一律如实写 ``parse_status`` / ``parse_error`` / 失败原因。
- 任务状态存内存，重启丢失；响应与文档必须明示（``RESTART_NOTE``）。
"""

from __future__ import annotations

import importlib
from typing import Any

__all__ = ["jobs", "pdf_import", "storage"]

#: 所有导入任务共享的“重启即丢”说明（响应体与文档共用同一措辞）
RESTART_NOTE = (
    "导入任务状态存放于进程内存，服务重启后 task_id 将查不到（返回 404）；"
    "已落库的 papers / paper_documents 记录与已落盘的上传文件不受影响。"
)


def __getattr__(name: str) -> Any:  # PEP 562：惰性导入，避免包导入期连锁失败
    if name in __all__:
        return importlib.import_module(f"{__name__}.{name}")
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
