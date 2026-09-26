"""让模型说人话 —— 对话正文一律由模型生成（用户口径 2026-09-26）。

为什么单独收成一个模块：这条口径要在好几处落地（节点收尾 / 引导词 / 普通对话确认 /
拒绝批准 / 模型失败兜底）。与其每处各写一遍"调模型 + 失败怎么办"，
不如收成一个入口 —— 哪天口径再动，只改这里。

铁律：**失败一律返回空串**。调用方必须自己决定"空"怎么呈现（留空 + 一条过程行如实说明），
**绝不允许拿程序话术冒充模型说话**。
"""

from __future__ import annotations

import logging

from llm import adapter

logger = logging.getLogger("sciloop.phrasing")

__all__ = ["PHRASING_SYSTEM", "say"]

#: 通用措辞提示词。两条硬要求：不许编造、**严禁内部术语**。
PHRASING_SYSTEM = (
    "你是 SciLoop 的科研助手，正在对研究者说话。"
    "只能用给你的事实，不要编造没有被记录的产出、数字或结论。"
    "**严禁内部术语**：不要出现「校验」「重试」「策略层」「节点」「流水线」「熔断」「落库」"
    "这类词，也不要用机器状态名（例如 waiting_human）；"
    "一切用研究者能听懂的说法，例如「这一步」「跑」这个版本」「需要你定」。"
    "不要用 Markdown 标题，正常分段把话说清楚就行。"
)


async def say(
    *,
    facts: str,
    model_ref: str | None,
    purpose: str,
    temperature: float = 0.3,
    system: str | None = None,
) -> str:
    """让模型按**事实**写一段给研究者看的话；写不出来就返回空串。"""

    if not model_ref:
        return ""
    try:
        result = await adapter.chat(
            [
                {"role": "system", "content": system or PHRASING_SYSTEM},
                {"role": "user", "content": facts},
            ],
            model_ref=model_ref,
            temperature=temperature,
            purpose=purpose,
            allow_fallback=True,
            strict_logging=False,
        )
    except Exception as exc:  # noqa: BLE001 - 措辞失败不该把对话带崩
        logger.warning("模型措辞失败（%s）：%s", purpose, exc)
        return ""
    return str(getattr(result, "content", "") or "").strip()
