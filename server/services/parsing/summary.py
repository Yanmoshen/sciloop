# Copyright 2026 SciLoop contributors
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
"""全文总结速览：解析详情页「标题下面、卡片上面」那 250–350 字。

口径来源：用户 2026-09-24 给定的 skill ``paper-summary-300`` ——
目标 280–320 字（判定按 ±10% 放行）、必须覆盖
「研究痛点 → 核心方案 → 技术创新 → 实验验证 → 性能结论」完整逻辑链、
可以分成 2–3 个自然段但**不用列表**、保留术语标准名称、客观陈述不含主观评价。
并要求首次出现的专业术语先用中文解释，避免整段堆叠英文术语。

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

#: 目标区间 280–320 字，判定时按 **±10%** 放行（252–352）——
#: 用户 2026-09-24 确认：「280-320字，误差不超过10%」当作容差处理，而不是逐字卡死，
#: 免得模型为了凑字数反复重试（自然语言约束不像 JSON 能靠 schema 兜住）。
MIN_CHARS = 252
MAX_CHARS = 352

#: 允许的自然段数上限（用户口径：可以分 2–3 段、但仍不用列表）
MAX_PARAGRAPHS = 3

#: 长度/格式不合规时最多再让模型改一次（自然语言约束，不像 JSON 那样能靠 schema 兜住）
MAX_ATTEMPTS = 2

#: 禁用的套话开头（用户明确点名：首先 / 其次 / 综上所述 / 本文）
BANNED_OPENERS: tuple[str, ...] = (
    "本文",
    "首先",
    "其次",
    "综上所述",
    "这篇论文旨在",
    "该文",
    "本项研究",
)

SUMMARY_SYSTEM = (
    "你是科研辅助系统的「论文速览」模块。请把给定论文压缩成一段约 300 字的中文总结，"
    "帮助研究者快速判断论文做了什么。句子要短，先说清楚再补充细节。\n"
    "硬性要求（任何一项不满足都算失败）：\n"
    f"1. 长度为 {MIN_CHARS}–{MAX_CHARS} 字（中文按字数，英文术语按词数）；\n"
    "2. 必须覆盖完整逻辑链，完整回答五件事：论文要解决的研究痛点或研究问题、采用的核心方案、具体技术创新、"
    "实验如何验证、得到什么性能结论。五项都不能漏；\n"
    "3. 建议按“问题—方法—创新—实验—结论”的顺序写。可分成 2–3 个自然段。每段围绕一个重点；\n"
    "4. 不要用列表、编号或小标题。只写连贯的自然段；\n"
    "5. 第一次出现专业术语时，先给出简短中文解释，再在括号中保留英文标准名称，例如“词元（token）”。"
    "方法名、模型名、数据集名保留原文标准名称。后文只在必要时使用英文，避免连续堆叠英文术语；\n"
    "6. 只用给定材料，只写给定论文材料能够支持的内容。卡片、摘要和全文没有的信息写不出来，不要推断、补充或借用外部知识；\n"
    "7. 可以直接写论文中的具体数字，不必标注来源。数字必须来自给定材料；\n"
    "8. 不要以“本文”“首先”“其次”“综上所述”等套话开头，直接说明论文做了什么；\n"
    "9. 只作客观陈述，不写主观评价、免责声明或建议，例如“值得关注”“需进一步验证”。"
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
    """清理模型输出：剥代码围栏、去行首列表符号、**保留段落分隔**。

    ⚠️ 2026-09-24 改：这里以前把换行**折成空格**（强制"只一段"）。用户确认口径改为
    「允许多段、但仍不用列表」之后，折行只能作用于**段内** —— 否则模型分好的
    2–3 段会被压成一坨，"可以分成 2–3 个自然段"这条要求就白提了。
    """

    plain = str(text or "").strip()
    plain = re.sub(r"^```[A-Za-z0-9]*\s*", "", plain)
    plain = re.sub(r"\s*```$", "", plain)
    # 段内折行折成空格；段间空行保留
    blocks = [re.sub(r"\s*\n\s*", " ", block) for block in re.split(r"\n\s*\n", plain)]
    cleaned: list[str] = []
    for block in blocks:
        line = block.strip()
        # 模型偶尔会带出列表符号或小标题符号：都按"不用列表/不要标题"的要求去掉
        line = re.sub(r"^\s*(?:[-*•·]|\d+[.、)])\s*", "", line)
        line = re.sub(r"^\s*#{1,6}\s*", "", line)
        if line.strip():
            cleaned.append(line.strip())
    return "\n\n".join(cleaned)


def _has_banned_opener(text: str) -> bool:
    return text.startswith(BANNED_OPENERS)


def _paragraph_count(text: str) -> int:
    return len([block for block in str(text or "").split("\n\n") if block.strip()])


def _issue_of(text: str) -> tuple[str | None, int]:
    """返回 ``(问题码, 字数)``；``问题码 is None`` 表示合格。"""

    chars = summary_char_count(text)
    if not text:
        return "empty", chars
    if chars < MIN_CHARS:
        return f"too_short:{chars}", chars
    if chars > MAX_CHARS:
        return f"too_long:{chars}", chars
    if _paragraph_count(text) > MAX_PARAGRAPHS:
        return f"too_many_paragraphs:{_paragraph_count(text)}", chars
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
                # 不设输出上限（推理类模型的推理与正文共用同一份预算，限死会把正文截断成空）
                max_tokens=None,
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
