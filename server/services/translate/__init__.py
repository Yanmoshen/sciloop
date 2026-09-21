# Copyright 2026 SciLoop contributors
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
"""论文翻译服务（PyMuPDF「块级翻译 + 原位回写」自研实现）。

对外入口（契约，供 ``app/api/v1/translate.py`` 使用）::

    from services.translate import artifacts, engine, highlight, jobs

    task = jobs.submit(source=..., mode="translate", highlight=True)  # -> task_id
    snap = jobs.get_task(task_id)                                     # -> 轮询快照

子模块职责
----------

- ``artifacts``：产物目录与 ``manifest.json`` 读写（``.cache/artifacts/<task_id>/``）。
- ``prompts``：translate / simplify / highlight 三套提示词（走 ``app/llm`` 适配层）。
- ``engine``：PyMuPDF 文本块提取、LLM/stub 翻译、原位覆盖回写、mono/dual 拼装、预览 HTML。
- ``highlight``：AI 高亮（句子分类 → PDF 标注 → 统计）。
- ``jobs``：内存任务注册表 + 后台线程执行（**任务状态不持久化**，进程重启即丢失）。

与 EasyPaper 的关系
-------------------

EasyPaper 用 ``pdf2zh / PDFMathTranslate`` 做版式保留翻译。本模块**不引入该依赖**，
改用 PyMuPDF 自研的 ``pymupdf-block-v1`` 引擎：逐块送 LLM 翻译后原位覆盖回写。
它是 pdf2zh 的可替换替代实现，后续可按 ``engine`` 字段插拔更换。

设计约束（见 assets/tasks/contracts.json）
-----------------------------------------

- 禁止编造：翻译失败/版式放不下/高亮失败一律**逐条**写进 ``layout_warnings``，绝不假装版式完美。
- 无真实 LLM 凭据时走**诚实桩**：桩产出的 ``provider/model`` 以 ``local-stub`` 自证，
  并补写一行 ``llm_call_logs``；回放由适配层负责标 ``is_replay=true``。
- 任务状态存内存，重启丢失；但磁盘上的产物与 manifest **必须保留**（见 :data:`RESTART_NOTE`）。
"""

from __future__ import annotations

import importlib
from typing import Any

__all__ = ["artifacts", "engine", "highlight", "jobs", "prompts"]

#: 产物引擎标识（写进 manifest 顶层 ``engine``，便于后续插拔替换）
ENGINE_NAME = "pymupdf-block-v1"

#: 引擎说明：本模块是 pdf2zh / PDFMathTranslate 的可替换替代实现
ENGINE_NOTE = (
    "engine=pymupdf-block-v1：用 PyMuPDF 做「块级翻译 + 原位回写」的自研实现，"
    "不依赖 pdf2zh / PDFMathTranslate；该字段用于后续插拔更换翻译引擎。"
)

#: 所有翻译任务共享的“重启即丢”说明（响应体与 manifest 共用同一措辞）
RESTART_NOTE = (
    "翻译任务状态存放于进程内存，服务重启后 task_id 不再出现在任务列表中；"
    "但磁盘产物（.cache/artifacts/<task_id>/ 下的 source.pdf / mono.pdf / dual.pdf / "
    "preview.html / manifest.json）会保留，可据 manifest 追溯与下载。"
)


def __getattr__(name: str) -> Any:  # PEP 562：惰性导入，避免包导入期连锁失败
    if name in __all__:
        return importlib.import_module(f"{__name__}.{name}")
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
