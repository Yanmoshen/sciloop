# Copyright 2026 SciLoop contributors
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
"""跨篇综述：多篇聚合的**那一段文字**（对比矩阵之外的另一半产物）。

为什么只用卡片与速览
--------------------
产品口径明确：**多篇聚合解析不吃原论文**，只把各篇的「解析卡片 + 总结速览」放一起再解析一次。
好处是成本与可追溯性 —— 综述里出现的每条判断都能追溯到某篇论文的卡片字段，
而不是重新去读一遍全文。

为什么单独一个模块
------------------
它与 :mod:`services.parsing.summary`（单篇速览）是**两种东西**：速览讲一篇，
综述要**跨篇比较与归纳**（谁解决了什么、分歧在哪、共同缺口是什么）。
提示词、长度区间、失败口径都不该混用。

存储：不新增表
--------------
挂在 ``aggregations.comparison_matrix["synthesis"]`` 下 ——
与既有的 ``comparison_matrix["missing"]`` 同一做法（矩阵里本就允许附加键），
避免加列（那要动迁移链，而迁移链同一时间只允许一条线动）。

失败口径：与速览一致 —— **从不抛异常**，失败只落 ``status='failed'``；
聚合是主体，综述是增强，综述失败不得连累聚合落库。
"""

from __future__ import annotations

import json
import logging
import re
from collections.abc import Sequence
from datetime import UTC, datetime
from typing import Any

# 字数口径与单篇速览**共用一处实现**（中文按字、英文术语按词），避免两处慢慢走样
from services.parsing.summary import summary_char_count

logger = logging.getLogger("sciloop.aggregation.synthesis")

__all__ = [
    "MAX_CHARS",
    "MIN_CHARS",
    "SYNTHESIS_PURPOSE",
    "SYNTHESIS_SYSTEM",
    "build_synthesis_messages",
    "generate_synthesis",
]

#: ``llm_call_logs.purpose``（与卡片的 card_build / 速览的 paper_summary 分开，便于成本追溯）
SYNTHESIS_PURPOSE = "aggregation_synthesis"

#: 跨篇综述比单篇速览长：要容下"比较 + 归纳 + 缺口"三层
MIN_CHARS = 200
MAX_CHARS = 400

MAX_ATTEMPTS = 2

#: 与速览同一套禁用开头（用户明确点名过）
BANNED_OPENERS: tuple[str, ...] = ("本文", "首先", "其次", "综上所述", "这些论文", "以上论文")

SYNTHESIS_SYSTEM = (
    "你是科研辅助系统的「跨篇综述」模块：把多篇论文放一起，写出一段**跨篇**的综述。\n"
    "硬性要求（不满足即视为失败）：\n"
    "1. 只输出**一段连续文字**：不要标题、不要分点、不要换行、不要代码块；\n"
    f"2. 长度 {MIN_CHARS}–{MAX_CHARS} 字（**中文按字算、英文术语按词算 1 字**）；\n"
    "3. **必须跨篇**：讲清它们共同解决什么、彼此的分歧或差异在哪、合起来还缺什么；"
    "不要一篇一句地罗列摘要；\n"
    "4. 专业术语、方法名、数据集名保留英文原样，其余用中文；\n"
    "5. 可以用材料里出现的具体数字，不必标注来源；\n"
    "6. 允许用 **加粗** 标出 1–2 个核心术语，其余不要加任何标记；\n"
    "7. 不要用「本文」「首先」「其次」「综上所述」这类套话开头，直接讲这批论文的整体图景；\n"
    "8. **只用给定材料**：卡片与速览里没有的内容不要推断、不要补充想象的研究；\n"
    "9. 不要写任何免责声明、评价或建议（例如「需进一步验证」「值得关注」）。"
)


def _paper_brief(card: dict[str, Any]) -> dict[str, Any]:
    """从卡片行里抽出综述需要的字段（含挂在 evidence_meta 下的速览）。"""

    setup = card.get("experimental_setup") or {}
    meta = setup.get("evidence_meta") if isinstance(setup, dict) else None
    summary = (meta or {}).get("summary") if isinstance(meta, dict) else None
    summary_text = ""
    if isinstance(summary, dict) and summary.get("status") == "ok":
        summary_text = str(summary.get("text") or "")

    return {
        "paper_id": int(card.get("paper_id") or 0),
        "title": card.get("title") or "",
        "research_problem": card.get("research_problem"),
        "core_method": card.get("core_method"),
        "key_innovation": card.get("key_innovation"),
        "main_conclusions": card.get("main_conclusions"),
        "limitations": card.get("limitations"),
        "summary": summary_text or None,
        "available_scope": meta.get("available_scope") if isinstance(meta, dict) else None,
    }


def build_synthesis_messages(cards: Sequence[dict[str, Any]]) -> list[dict[str, Any]]:
    """把各篇的「卡片 + 速览」交给模型（**不含原文** —— 聚合不吃原论文）。"""

    payload = {
        "papers": [_paper_brief(card) for card in cards],
        "instruction": "请按 system 的要求输出这一段跨篇综述，只输出这一段文字。",
    }
    return [
        {"role": "system", "content": SYNTHESIS_SYSTEM},
        {"role": "user", "content": json.dumps(payload, ensure_ascii=False, default=str)},
    ]


def _failed(reason: str) -> dict[str, Any]:
    return {"status": "failed", "reason": reason, "generated_at": _utc_now()}


def _utc_now() -> str:
    return datetime.now(UTC).isoformat()


def _issue_of(text: str) -> tuple[str | None, int]:
    chars = summary_char_count(text)
    if not text:
        return "empty", chars
    if chars < MIN_CHARS:
        return f"too_short:{chars}", chars
    if chars > MAX_CHARS:
        return f"too_long:{chars}", chars
    if text.startswith(BANNED_OPENERS):
        return "banned_opener", chars
    return None, chars


def _corrective(issue: str, chars: int, previous: str) -> dict[str, str]:
    hint = {
        "too_short": f"太短了（{chars} 字），请补到 {MIN_CHARS} 字以上",
        "too_long": f"太长了（{chars} 字），请压到 {MAX_CHARS} 字以内",
        "banned_opener": "不要用「本文/首先/其次/综上所述」开头，直接从这批论文的整体图景讲起",
        "empty": "上一次输出是空的，请重新输出这一段",
    }
    reason = next((text for key, text in hint.items() if issue.startswith(key)), "上一次输出不符合要求")
    return {
        "role": "user",
        "content": (
            f"{reason}。请重写并**只输出这一段文字**，不要解释、不要分点、不要换行。\n"
            f"上一次的输出是：{previous[:500]}"
        ),
    }


async def generate_synthesis(
    cards: Sequence[dict[str, Any]],
    *,
    project_id: int | None = None,
) -> dict[str, Any]:
    """生成跨篇综述；**任何失败都返回 ``status='failed'``，绝不抛异常**。

    少于两篇时直接判失败 —— 单篇没有"跨篇"可言（产品的「单篇解析」是另一条路径）。
    """

    from llm import chat
    from llm.errors import LLMError

    if len(cards) < 2:
        logger.info("跨篇综述判定失败：参与论文不足两篇（%s 篇）", len(cards))
        return _failed("not_enough_papers")

    messages = build_synthesis_messages(cards)
    conversation = list(messages)
    last_reason = "not_attempted"

    for attempt in range(1, MAX_ATTEMPTS + 1):
        try:
            result = await chat(
                conversation,
                temperature=0.3,
                # 不设输出上限（原先 1200；推理类模型的推理与正文共用同一份预算，限死会截断正文）
                max_tokens=None,
                project_id=project_id,
                stage="parse",
                purpose=SYNTHESIS_PURPOSE,
                metadata={
                    "paper_ids": [int(card.get("paper_id") or 0) for card in cards],
                    "paper_count": len(cards),
                    "attempt": attempt,
                },
            )
        except LLMError as exc:
            last_reason = f"llm_error:{type(exc).__name__}"
            logger.warning("跨篇综述调用失败（第 %s 次）：%s", attempt, exc)
            continue

        text = _normalize(result.content)
        issue, chars = _issue_of(text)
        if issue is None:
            logger.info("跨篇综述生成成功 papers=%s chars=%s model=%s", len(cards), chars, result.model_ref)
            return {
                "status": "ok",
                "text": text,
                "chars": chars,
                "purpose": SYNTHESIS_PURPOSE,
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
                "paper_count": len(cards),
                "generated_at": _utc_now(),
            }

        last_reason = issue
        logger.warning(
            "跨篇综述不合格（第 %s/%s 次）issue=%s chars=%s", attempt, MAX_ATTEMPTS, issue, chars
        )
        if attempt < MAX_ATTEMPTS:
            conversation = [*messages, _corrective(issue, chars, text)]

    return _failed(last_reason)


def _normalize(text: str) -> str:
    """压成一段：折换行、剥代码围栏、去行首列表符号。"""

    plain = str(text or "").strip()
    plain = re.sub(r"^```[A-Za-z0-9]*\s*", "", plain)
    plain = re.sub(r"\s*```$", "", plain)
    plain = re.sub(r"\s*\n+\s*", " ", plain)
    plain = re.sub(r"^\s*[-*•]\s+", "", plain)
    return plain.strip()
