# Copyright 2026 SciLoop contributors
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
"""全文总结速览：解析详情页「标题下面、卡片上面」那一段 100–200 字。

为什么要有独立提示词
--------------------
速览与 8 字段卡片是两种东西：卡片逐条带证据、可定位、要过结构契约；
速览是一段**给研究者 30 秒读懂这篇论文**的导读，按产品口径**不参与证据核验、不标任何来源**。
所以它有独立的 system 提示词与独立的模型调用（``purpose='paper_summary'``），
不寄生在卡片提示词里 —— 混在一起会把"结论性字段必须带 evidence"这条契约稀释掉。

存储：**不新增表、不加迁移**
----------------------------
沿用本项目已有的做法（见 :mod:`services.parsing.card_builder` 的「审计字段落点」）：
把速览挂进 ``experimental_setup.evidence_meta["summary"]``，
从而不改动任何他人拥有的表结构。理由：建表/加列要改迁移链，
而迁移链同一时间只允许一条线动 —— 代价远大于收益。

失败口径（**产品口径：只允许成功，不允许失败**）
------------------------------------------------
没有全文 / 模型调用失败 / 长度不合规 / 用了套话开头 → 一律 ``status='failed'``，
页面如实显示「生成失败」，**原因只进 ``reason``**（落库与日志），不显示给研究者。
本模块**从不抛异常**：速览是卡片之上的增强，它失败不能连累卡片是否建成。
"""

from __future__ import annotations

import json
import logging
import re
from collections.abc import Sequence
from datetime import UTC, datetime
from typing import Any

logger = logging.getLogger("sciloop.parsing.summary")

__all__ = [
    "MAX_ATTEMPTS",
    "MAX_CHARS",
    "MIN_CHARS",
    "SUMMARY_PURPOSE",
    "SUMMARY_SYSTEM",
    "build_summary_messages",
    "generate_summary",
    "summary_char_count",
]

#: ``llm_call_logs.purpose`` —— 与卡片的 ``card_build`` 分开，便于成本追溯与计费区分
SUMMARY_PURPOSE = "paper_summary"

MIN_CHARS = 100
MAX_CHARS = 200

#: 长度/格式不合规时最多再让模型改一次（自然语言约束，不像 JSON 那样能靠 schema 兜住）
MAX_ATTEMPTS = 2

#: 禁用的套话开头（用户明确点名：首先 / 其次 / 综上所述 / 本文）
BANNED_OPENERS: tuple[str, ...] = ("本文", "首先", "其次", "综上所述", "这篇论文旨在")

SUMMARY_SYSTEM = (
    "你是科研辅助系统的「论文速览」模块：把一篇论文压缩成研究者 30 秒能读完的一段话。\n"
    "硬性要求（不满足即视为失败）：\n"
    "1. 只输出**一段连续文字**：不要标题、不要分点、不要换行、不要代码块；\n"
    f"2. 长度 {MIN_CHARS}–{MAX_CHARS} 字（**中文按字算、英文术语按词算 1 字**）；\n"
    "3. 专业术语、方法名、数据集名保留英文原样，其余用中文；\n"
    "4. 可以直接写论文里的具体数字，不必标注来源；\n"
    "5. 允许用 **加粗** 标出 1–2 个核心术语，其余不要加任何标记；\n"
    "6. 不要用「本文」「首先」「其次」「综上所述」这类套话开头，直接讲这篇论文做了什么；\n"
    "7. 不要写任何免责声明、评价或建议（例如「需进一步验证」「值得关注」）。"
)

_CJK = re.compile(r"[\u3400-\u4dbf\u4e00-\u9fff\uf900-\ufaff]")
_TOKEN = re.compile(r"[A-Za-z0-9][A-Za-z0-9._\-]*")


def summary_char_count(text: str) -> int:
    """按产品口径数字数：**中文按字、英文（含数字）按词**。

    中文标点不计（"按字"指汉字）；``**`` 这类排版标记也不计。
    例：``"我们用 BPE 做了 3 组消融"`` → 汉字 7 + 词 2 = 9。
    """

    plain = str(text or "").replace("**", "")
    return len(_CJK.findall(plain)) + len(_TOKEN.findall(plain))


def _normalize(text: str) -> str:
    """压成一段：折换行、剥代码围栏、去行首列表符号（模型偶尔会带出来）。"""

    plain = str(text or "").strip()
    plain = re.sub(r"^```[A-Za-z0-9]*\s*", "", plain)
    plain = re.sub(r"\s*```$", "", plain)
    plain = re.sub(r"\s*\n+\s*", " ", plain)
    plain = re.sub(r"^\s*[-*•]\s+", "", plain)
    return plain.strip()


def _has_banned_opener(text: str) -> bool:
    return text.startswith(BANNED_OPENERS)


def _issue_of(text: str) -> tuple[str | None, int]:
    """返回 ``(问题码, 字数)``；``问题码 is None`` 表示合格。"""

    chars = summary_char_count(text)
    if not text:
        return "empty", chars
    if chars < MIN_CHARS:
        return f"too_short:{chars}", chars
    if chars > MAX_CHARS:
        return f"too_long:{chars}", chars
    if _has_banned_opener(text):
        return "banned_opener", chars
    return None, chars


def _corrective(issue: str, chars: int, previous: str) -> dict[str, str]:
    hint = {
        "too_short": f"太短了（{chars} 字），请补到 {MIN_CHARS} 字以上",
        "too_long": f"太长了（{chars} 字），请压到 {MAX_CHARS} 字以内",
        "banned_opener": "不要用「本文/首先/其次/综上所述」开头，直接从这篇论文做了什么讲起",
        "empty": "上一次输出是空的，请重新输出这一段",
    }
    reason = next(
        (text for key, text in hint.items() if issue.startswith(key)), "上一次输出不符合要求"
    )
    return {
        "role": "user",
        "content": (
            f"{reason}。请重写并**只输出这一段文字**，不要解释、不要分点、不要换行。\n"
            f"上一次的输出是：{previous[:400]}"
        ),
    }


def build_summary_messages(
    *, title: str, abstract: str | None, blocks: Sequence[dict[str, Any]]
) -> list[dict[str, Any]]:
    """题录 + 已解析的正文章节。**只用全文**（没有全文时调用方直接判失败，不调模型）。"""

    payload = {
        "paper": {"title": title, "abstract": abstract},
        "full_text_sections": list(blocks),
        "instruction": "请按 system 的要求输出这一段速览，只输出这一段文字。",
    }
    return [
        {"role": "system", "content": SUMMARY_SYSTEM},
        {"role": "user", "content": json.dumps(payload, ensure_ascii=False, default=str)},
    ]


def _failed(reason: str) -> dict[str, Any]:
    return {"status": "failed", "reason": reason, "generated_at": _utc_now()}


def _utc_now() -> str:
    return datetime.now(UTC).isoformat()


async def generate_summary(
    *,
    paper_id: int,
    document_version: str | None,
    title: str,
    abstract: str | None,
    blocks: Sequence[dict[str, Any]],
    project_id: int | None = None,
) -> dict[str, Any]:
    """生成速览；**任何失败都返回 ``status='failed'``，绝不抛异常**。

    没有全文（``blocks`` 为空或没有 ``document_version``）时**直接判失败且不调模型** ——
    产品口径是"速览必须基于全文"，不做摘要级降级，也就不该为此花一次调用。
    """

    from llm import chat
    from llm.errors import LLMError

    if not blocks or not document_version:
        logger.info(
            "速览判定失败：paper %s 没有可用全文（document_version=%s, blocks=%s）",
            paper_id,
            document_version,
            len(blocks),
        )
        return _failed("no_fulltext")

    messages = build_summary_messages(title=title, abstract=abstract, blocks=blocks)
    conversation = list(messages)
    last_reason = "not_attempted"

    for attempt in range(1, MAX_ATTEMPTS + 1):
        try:
            result = await chat(
                conversation,
                temperature=0.3,
                max_tokens=512,
                project_id=project_id,
                stage="parse",
                purpose=SUMMARY_PURPOSE,
                metadata={
                    "paper_id": paper_id,
                    "document_version": document_version,
                    "attempt": attempt,
                },
            )
        except LLMError as exc:
            last_reason = f"llm_error:{type(exc).__name__}"
            logger.warning("速览调用失败（第 %s 次）paper_id=%s：%s", attempt, paper_id, exc)
            continue

        text = _normalize(result.content)
        issue, chars = _issue_of(text)
        if issue is None:
            logger.info(
                "速览生成成功 paper_id=%s chars=%s attempt=%s model=%s",
                paper_id,
                chars,
                attempt,
                result.model_ref,
            )
            return {
                "status": "ok",
                "text": text,
                "chars": chars,
                "purpose": SUMMARY_PURPOSE,
                "model_ref": result.model_ref,
                "provider": result.provider,
                "prompt_hash": result.prompt_hash,
                "is_replay": bool(result.is_replay),
                "cost_usd": result.cost_usd,
                "usage": {
                    "prompt_tokens": result.usage.prompt_tokens,
                    "completion_tokens": result.usage.completion_tokens,
                },
                "attempts": attempt,
                "generated_at": _utc_now(),
            }

        last_reason = issue
        logger.warning(
            "速览不合格（第 %s/%s 次）paper_id=%s issue=%s chars=%s",
            attempt,
            MAX_ATTEMPTS,
            paper_id,
            issue,
            chars,
        )
        if attempt < MAX_ATTEMPTS:
            conversation = [*messages, _corrective(issue, chars, text)]

    return _failed(last_reason)
