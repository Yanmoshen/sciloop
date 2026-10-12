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
from services.system_prompt import SCILOOP_SYSTEM_PROMPT

logger = logging.getLogger("sciloop.phrasing")

__all__ = ["PHRASING_SYSTEM", "say"]

#: 通用措辞提示词。两条硬要求：不许编造、**严禁内部术语**。
PHRASING_SYSTEM = SCILOOP_SYSTEM_PROMPT + "\n\n" + (
    "## 本轮措辞任务\n"
    "根据提供的真实事实撰写给研究者看的说明；不调用工具，不改变研究状态，"
    "不编造产物或结论。按用户熟悉程度解释必要术语，避免无关调试细节。"
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
