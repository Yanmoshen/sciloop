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
    "NODE_ONLY_REASONING_TEXT",
    "SYSTEM_ROW_LABELS",
    "guide_blocks",
    "node_summary",
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


def node_summary(
    *,
    node_label: str,
    status: str,
    events: list[tuple[str, dict[str, Any]]],
    refs: dict[str, Any],
) -> str:
    """节点执行完后的**确定性结论**（不再花一次模型调用去措辞）。

    为什么不让模型写这段：结论必须与落库事实一致。「花了多少钱、产出了几条证据、
    下一步是哪」这些都是程序已经知道的事实，交给模型复述只会引入不一致。
    """

    lines: list[str] = []
    done = next((d for e, d in events if e == "done"), {})
    if status == "done":
        lines.append(f"「{node_label}」已通过校验。")
    elif status == "waiting_human":
        lines.append(f"「{node_label}」多次修复仍未通过校验，已转入人工介入。")
    elif status == "failed":
        lines.append(f"「{node_label}」执行失败。")
    else:
        lines.append(f"「{node_label}」本轮结束（{status}）。")

    produced: list[str] = []
    if refs.get("evidence_ids"):
        produced.append(f"证据 {len(refs['evidence_ids'])} 条")
    if refs.get("idea_id"):
        produced.append(f"假设 #{refs['idea_id']}")
    if refs.get("feasibility_id"):
        produced.append(f"可行性报告 #{refs['feasibility_id']}")
    if refs.get("taskbook_id"):
        produced.append(f"任务书 #{refs['taskbook_id']}（已锁定）")
    if refs.get("taskbook_skipped"):
        produced.append("任务书未落库（本对话未挂项目，任务书需要项目归属）")
    if produced:
        lines.append("产出：" + "、".join(produced) + "。")

    if done.get("llm_call_count"):
        cost = done.get("cost_usd")
        cost_text = f"${cost:.4f}" if isinstance(cost, (int, float)) else "未知"
        lines.append(f"本次调用模型 {done['llm_call_count']} 次，费用 {cost_text}。")

    next_node = done.get("next_node")
    if next_node and next_node != "end" and status == "done":
        lines.append(f"下一步可以执行「{_label_of(next_node)}」——说一句「继续」我就往下走。")
    return "\n\n".join(lines)


def _label_of(node: str) -> str:
    from services.research import graph

    return graph.NODE_LABELS.get(node, node)


def guide_blocks() -> list[dict[str, Any]]:
    return [dict(block) for block in GUIDE_BLOCKS]
