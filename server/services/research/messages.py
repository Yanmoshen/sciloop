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

import re
from typing import Any

from services.system_prompt import SCILOOP_SYSTEM_PROMPT

__all__ = [
    "SYSTEM_ROW_LABELS",
    "guide_blocks",
    "waiting_human_blocks",
    "blocks_for_turn",
    "NODE_CONCLUSION_SYSTEM",
    "NODE_CONCLUSION_TEMPLATE",
    "system_row",
]

#: 节点收尾的提示词 —— **措辞由模型来，程序只给事实**。
#:
#: 为什么不让程序拼这句话（用户口径 2026-09-26）：对话里的正文必须是人话，
#: 而程序拼出来的必然是"状态机口吻"（「多次修复仍未通过校验」「已转入人工介入」这种），
#: 研究者读到的是一句内部结论，而不是"现在怎么了、要我做啥"。
#:
#: ⚠️ 严禁出现内部术语（校验 / 重试 / 策略层 / 节点 id / 熔断 / 落库 等）——
#: 说人话是硬要求，不是风格偏好。
NODE_CONCLUSION_SYSTEM = SCILOOP_SYSTEM_PROMPT + "\n\n" + (
    "## 本轮节点成果展示\n"
    "将提供的真实结果、产物和证据组织成研究者可理解的说明，不照抄 JSON 字段，"
    "不编造未记录的工作。区分产出中的建议、核实事实和未经验证的判断。\n"
    "说明本节点成果、实际验证范围、剩余问题及建议下一步。"
    "用自然语言解释完成的工作；仅在确实需要用户决定时才提出问题，"
    "不要向用户展示节点、验收、修复次数、编号等内部流程信息。"
    "运行时不支持的操作如实说明，不宣称已执行。"
    "只有确实存在互斥决定时才提出简短行动列表；不要为了填充界面制造选项，"
    "也不要把内部流程状态写进正文。"
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
    """旧 SSE 接口的兼容函数；新的研究入口不生成启动菜单。"""

    return []


def _summary_options(summary: str) -> list[dict[str, Any]]:
    """提取正文中的显式 A/B 行动列表；不依赖节点名或证据数量猜测意图。"""

    options: list[dict[str, Any]] = []
    for line in (summary or "").splitlines():
        plain = line.strip().replace("**", "").replace("__", "")
        match = re.match(r"^(?:[-*+]\s+)?([A-E])[.．、:：)）]\s*(.+)$", plain)
        if not match:
            continue
        letter, action = match.groups()
        title = re.split(r"[:：]", action, maxsplit=1)[0].strip()
        # 长段落不能当作按钮标题；只有从 A 起连续排列的至少两项才是选择组。
        if not title or len(title) > 60 or letter != chr(ord("A") + len(options)):
            continue
        options.append({
            "id": f"proposal_{letter}",
            "label": f"{letter}：{title}",
            "send": f"选择 {letter}：{title}",
            "tone": "primary" if letter == "A" else "default",
        })
    return options if len(options) >= 2 else []


def waiting_human_blocks(
    *,
    summary: str = "",
    node_label: str = "",
    node: str | None = None,
    has_evidence: bool | None = None,
) -> list[dict[str, Any]]:
    """只渲染模型正文明确给出的行动选项。

    研究流程不能凭空替模型制造“继续/重跑/普通对话”按钮。没有明确的
    模型选项时，正文就是完整的用户界面，等待下一条自然语言消息。
    """

    options = _summary_options(summary)
    if options:
        return [{"kind": "choice", "prompt": "想怎么走？", "options": options}]

    return []



def blocks_for_turn(turn: dict[str, Any]) -> list[dict[str, Any]]:
    """读旧会话时修复通用按钮与正文不一致的问题，保留原始历史文件。"""

    blocks = turn.get("blocks") or []
    if turn.get("role") != "assistant" or turn.get("node_status") != "waiting_human":
        return blocks
    legacy = any(
        block.get("kind") == "choice"
        and {item.get("id") for item in block.get("options", [])} == {"continue", "redo", "plain"}
        for block in blocks
    )
    if not legacy:
        return blocks
    replacement = waiting_human_blocks(summary=str(turn.get("content") or ""), node_label="")
    return [*replacement, *(block for block in blocks if block.get("kind") != "choice")]
