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
    "GUIDE_TEXT",
    "MODEL_FAILED_TEXT",
    "NODE_ONLY_REASONING_TEXT",
    "SYSTEM_ROW_LABELS",
    "guide_blocks",
    "NODE_CONCLUSION_SYSTEM",
    "NODE_CONCLUSION_TEMPLATE",
    "system_row",
]

#: 引导词正文（模糊引导词 + 本对话还没有研究链时输出）
GUIDE_TEXT = (
    "看起来你想启动研究流程。这次想怎么做？\n\n"
    "**① 从文献调研开始** —— 我在你的论文库里检索相关工作，"
    "产出带可定位来源的证据、比较矩阵与最接近的工作。\n\n"
    "**② 从 idea 生成开始** —— 你已有想法，我直接把它整理成可证伪假设并做可行性分析。"
    "（未经文献核查的 idea，不能宣称创新成立。）\n\n"
    "**③ 只是普通对话** —— 我们就聊，不落研究链。\n\n"
    "选一个，或者直接说「开始文献调研」。"
)

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

#: 选了「只是普通对话」之后的确认（必须告知**可以切回、以及怎么切**）
PLAIN_CHAT_TEXT = (
    "好，这个对话我们就当普通对话，不会再问你要不要开始研究流程。\n\n"
    "想随时切回研究模式：打开输入栏那一行的「自动连续跑 / 研究模式」开关，"
    "然后说一句「开始文献调研」我就会直接跑。"
)

#: 模型只给了思考过程、没有正文时的如实说明（不拿思考过程冒充答复）
NODE_ONLY_REASONING_TEXT = (
    "模型这次只输出了思考过程，没有给出正文。"
    "思考过程已折叠在下面（它**不是结论**，不能当作回答使用）。"
)

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
    "**严禁出现内部术语**：不要写「校验」「重试」「策略层」「节点」「流水线」「熔断」"
    "「落库」「闸门」这类词，也不要用机器状态名（例如 waiting_human）。\n"
    "按顺序说清四件事（自然分段，不要用「第一/第二」这种编号腔，也不要写小标题）：\n"
    "1. 这一步现在是什么结果 —— 成了、还是没成、还是需要他来定；\n"
    "2. 具体产出了什么（有就写清楚数量与名称；没有就直说没有）；\n"
    "3. 如果没成或需要他定：卡在什么地方、**因为他能做的事**是什么；\n"
    "4. 接下来可以怎么走，并明确告诉他可以直接回一句什么话来继续。\n"
    "全程不要提系统、程序、调用了模型几次、花了多少钱。"
    "不要用 Markdown 标题，就用正常段落。控制在 200 字以内。"
)

#: 收尾提示词的载荷模板（`{facts}` 由程序填**事实**）
NODE_CONCLUSION_TEMPLATE = (
    "【这一步的事实（照它说，不要自己加戏）】\n{facts}\n\n"
    "请写一段给研究者看的话，交代清楚这一步的结果与接下来怎么办。"
)

#: 模型这一跳彻底失败（自动重试也没成）时的如实说明。
#:
#: 用户口径 2026-09-26：「不再允许所有的强制中断，必须完整回答之后才能结束」。
#: 所以这里不再甩一个错误码就断开 —— 而是把"为什么没有完整回答"写成一句**说完的话**，
#: 连同「已经做到哪、你可以怎么继续」一起交代清楚。
#: 它同样是**系统在说话**（落盘时会标 `note_only`），不会被当成模型说过的话进下一轮上下文。
MODEL_FAILED_TEXT = (
    "这一轮没能拿到模型的完整回答：调用连续失败（已自动重试）。\n\n"
    "失败原因：{reason}\n\n"
    "**已经做完的部分都在上面的过程行里**（调用了哪些工具、结果如何），没有丢。"
    "你可以直接再发一次让它接着做，或者换个说法；如果是密钥/额度问题，"
    "在「设置 → 模型供应商」里可以看到具体状态。"
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
