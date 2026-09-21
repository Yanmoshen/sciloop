# Copyright 2026 SciLoop contributors
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
"""翻译模块的三套提示词（中文翻译 / 英文简化 / AI 高亮分类）。

与 EasyPaper §3.4 的差异口径保持一致：

- ``mode=translate``：目标语言**中文**；
- ``mode=simplify``：目标语言**仍是英文**，要求 CEFR A2/B1 级别用词，
  保持原意、保留公式占位符与数学符号、不输出解释或总结
  （因此 simplify **不产出双语 PDF**——英文-英文双语没有意义）。

提示词只做「输入 → 输出」的文本变换约束，不引入任何新事实；
本模块所有 LLM 调用都必须经过 ``app/llm`` 适配层（成本登记 + 降级 + 回放）。
"""

from __future__ import annotations

from typing import Any

from llm.types import Message

#: 高亮标签（契约固定值域）
LABEL_CORE = "core_conclusion"
LABEL_METHOD = "method_innovation"
LABEL_DATA = "key_data"
HIGHLIGHT_LABELS: tuple[str, ...] = (LABEL_CORE, LABEL_METHOD, LABEL_DATA)

_TRANSLATE_SYSTEM = (
    "你是科研论文翻译引擎，只做逐段翻译，输出将直接写回 PDF 文本块。\n"
    "必须严格遵守：1) 只输出译文本身，不要任何解释、前言、Markdown 围栏或注释；\n"
    "2) 保持与原文相同的语义与信息量，不得增删事实、不得编造数据；\n"
    "3) 保留数学符号、公式、变量名、缩写、数字与单位原样；\n"
    "4) 原文是标题时译成短语，不要加句末标点；原文是图表/表格时保持简短；\n"
    "5) 无法确定含义的专有名词保留原文英文并原样夹在译文中。"
)

_SIMPLIFY_SYSTEM = (
    "You are a research-paper simplification engine. Output will be written back into PDF text blocks.\n"
    "Strict rules: 1) Output ONLY the simplified English text - no explanation, no preface, "
    "no markdown fences, no notes;\n"
    "2) Target CEFR A2/B1 vocabulary and short sentences, but keep the original meaning exactly;\n"
    "3) Keep formulas, math symbols, variable names, numbers and units unchanged;\n"
    "4) Never add interpretation, summary, or commentary; never invent facts;\n"
    "5) Keep it as short as the original - do not expand."
)

_HIGHLIGHT_SYSTEM = (
    "你是科研论文重点句标注器，输出将被程序消费。\n"
    "必须严格遵守：1) 只输出合法 JSON，不要任何解释性文字；\n"
    "2) 每条输入的句子必须原样返回其 id，不得新增、删除或改写句子；\n"
    "3) label 只能取 core_conclusion / method_innovation / key_data 三者之一，"
    "不属于这三类的句子 label 置为 null；\n"
    "4) 判断依据必须是句子本身的文字，不要基于外部知识猜测。"
)


def build_translate_messages(text: str, *, mode: str, page: int | None = None) -> list[Message]:
    """构造单块翻译消息（``mode`` 决定目标语言与风格）。"""
    system = _SIMPLIFY_SYSTEM if mode == "simplify" else _TRANSLATE_SYSTEM
    target = "Simplified English (CEFR A2/B1)" if mode == "simplify" else "简体中文"
    hint: dict[str, Any] = {
        "task": "translate_or_simplify_one_block",
        "target_language": target,
        "mode": mode,
    }
    if page is not None:
        hint["page"] = int(page)
    user = (
        f"{hint}\n"
        "---- 原文（仅此一段，不要翻译其它内容）----\n"
        f"{text}\n"
        "---- 输出 ----"
    )
    return [
        {"role": "system", "content": system},
        {"role": "user", "content": user},
    ]


def build_highlight_messages(sentences: list[dict[str, Any]]) -> list[Message]:
    """构造高亮分类消息；``sentences`` 为 ``[{"id": "s1", "text": "..."}]``。"""
    user = {
        "task": "classify_each_sentence",
        "labels": list(HIGHLIGHT_LABELS),
        "sentences": sentences,
        "output_format": {"items": [{"id": "s1", "label": "core_conclusion 或 method_innovation 或 key_data 或 null"}]},
    }
    return [
        {"role": "system", "content": _HIGHLIGHT_SYSTEM},
        {"role": "user", "content": _json(user)},
    ]


def highlight_json_schema() -> dict[str, Any]:
    """高亮分类的结构化输出 schema（本地 jsonschema 校验用）。"""
    return {
        "type": "object",
        "properties": {
            "items": {
                "type": "array",
                "items": {
                    "type": "object",
                    "properties": {
                        "id": {"type": "string"},
                        "label": {
                            "type": ["string", "null"],
                            "enum": [*HIGHLIGHT_LABELS, None],
                        },
                    },
                    "required": ["id", "label"],
                    "additionalProperties": False,
                },
            }
        },
        "required": ["items"],
        "additionalProperties": False,
    }


def _json(payload: Any) -> str:
    import json

    return json.dumps(payload, ensure_ascii=False)


__all__ = [
    "HIGHLIGHT_LABELS",
    "LABEL_CORE",
    "LABEL_DATA",
    "LABEL_METHOD",
    "build_highlight_messages",
    "build_translate_messages",
    "highlight_json_schema",
]
