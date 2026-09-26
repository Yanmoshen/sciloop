# Copyright 2026 SciLoop contributors
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
"""对话里面向研究者的文案（后端侧的 ``utils/messages.ts``）。

为什么单独一个文件：与前端同一条纪律——**用户可见的文本不出现实现细节**
（内部字段名、表名、SSE、token 之类的词）。此前这些句子散在 ``chat.py`` 里，
改一处漏一处；集中在这里便于逐句审。
"""

from __future__ import annotations

from typing import Any

__all__ = [
    "GUIDE_BLOCKS",
    "SYSTEM_ROW_LABELS",
    "guide_blocks",
    "NODE_CONCLUSION_SYSTEM",
    "NODE_CONCLUSION_TEMPLATE",
    "system_row",
]

#: 引导词的可点选项（前端渲染成按钮）
GUIDE_BLOCKS: list[dict[str, Any]] = [
    {
        "kind": "choice",
        "prompt": "这次想怎么做？",
        "options": [
            {
                "id": "literature_review",
                "label": "从文献调研开始",
                "send": "开始文献调研",
                "tone": "primary",
            },
            {
                "id": "idea_and_feasibility",
                "label": "从 idea 生成开始",
                "send": "开始 idea 生成",
                "tone": "default",
            },
            {
                "id": "plain_chat",
                "label": "只是普通对话",
                "send": "只是普通对话",
                "tone": "quiet",
            },
        ],
    }
]

#: 节点收尾的提示词 —— **措辞由模型来，程序只给事实**。
#:
#: 为什么不让程序拼这句话（用户口径 2026-09-26）：对话里的正文必须是人话，
#: 而程序拼出来的必然是"状态机口吻"（「多次修复仍未通过校验」「已转入人工介入」这种），
#: 研究者读到的是一句内部结论，而不是"现在怎么了、要我做啥"。
#:
#: ⚠️ 严禁出现内部术语（校验 / 重试 / 策略层 / 节点 id / 熔断 / 落库 等）——
#: 说人话是硬要求，不是风格偏好。
NODE_CONCLUSION_SYSTEM = (
    "你是 SciLoop 的科研助手。刚刚跑完了研究流程里的一步，请你**用研究者看得懂的话**"
    "把这一步交代清楚。\n"
    "只说你拿得到事实的部分，不要编造未被记录的产出。\n"
    "**事实里如果有「这一步自己写在产出里的内容」，那就是它自己的判断 —— 一定要用自己的话讲给研究者听**\n"
    "（例如它选定的切口、给出的空白假设、检索式、覆盖范围与缺口）。不要念字段名、不要照抄 JSON。\n"
    "**有判断就要说出来**：宁可说「我的判断是…（你可以推翻）」，也不要只说「没有产出」。\n"
    "**严禁出现内部术语**：不要写「校验」「重试」「策略层」「节点」「流水线」「熔断」"
    "「落库」「闸门」这类词，也不要用机器状态名（例如 waiting_human）。\n"
    "按顺序说清四件事（自然分段，不要用「第一/第二」这种编号腔，也不要写小标题）：\n"
    "1. 这一步现在是什么结果 —— 成了、还是没成、还是需要他来定；\n"
    "2. 具体产出了什么（有就写清楚数量与名称；没有就直说没有）；\n"
    "3. 如果没成或需要他定：卡在什么地方、**因为他能做的事**是什么；\n"
    "4. 接下来可以怎么走，并明确告诉他可以直接回一句什么话来继续。\n"
    "全程不要提系统、程序、调用了模型几次、花了多少钱。"
    "不要用 Markdown 标题，就用正常段落。控制在 400 字以内（判断部分不要因为字数被砍）。"
)

#: 收尾提示词的载荷模板（`{facts}` 由程序填**事实**）
NODE_CONCLUSION_TEMPLATE = (
    "【这一步的事实（照它说，不要自己加戏）】\n{facts}\n\n"
    "请写一段给研究者看的话，交代清楚这一步的结果与接下来怎么办。"
)

#: 系统行的标签（前端按这个渲染紧凑行）
SYSTEM_ROW_LABELS: dict[str, str] = {
    "entered": "进入节点",
    "attempt": "本轮尝试",
    "validation": "产出被驳回",
    "notice": "降级说明",
    "revert": "回退",
    "migrated": "进入下一节点",
    "waiting_human": "转人工介入",
    "stopped": "已中止",
    "salvaged": "结构化输出降级为宽松解析",
}


def system_row(kind: str, text: str, *, tone: str = "idle") -> dict[str, Any]:
    """构造一条对话内的系统行（紧凑展示节点过程）。"""

    return {
        "kind": "system",
        "label": SYSTEM_ROW_LABELS.get(kind, kind),
        "text": text,
        "tone": tone,
    }


def result_card(result: Any) -> dict[str, Any]:
    """把查询结果转成对话里的结果卡片块。"""

    return {
        "kind": "result",
        "title": getattr(result, "title", ""),
        "summary": getattr(result, "summary", ""),
        "columns": list(getattr(result, "columns", []) or []),
        "rows": list(getattr(result, "rows", []) or []),
        "total": int(getattr(result, "total", 0) or 0),
    }


def _label_of(node: str) -> str:
    from services.research import graph

    return graph.NODE_LABELS.get(node, node)


def guide_blocks() -> list[dict[str, Any]]:
    return [dict(block) for block in GUIDE_BLOCKS]
