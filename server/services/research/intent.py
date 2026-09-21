# Copyright 2026 SciLoop contributors
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
"""对话意图路由：**对话就是 agent 的入口**。

四类意图，优先级从高到低
------------------------
``node``   **明确的执行意图**：「开始/跑/执行/启动 + 节点名」或明显祈使句
           → 直接执行该节点，不再反问。
``guide``  **模糊的引导性词**（怎么开始 / 开始 / 研究 / 怎么研究 / 怎么文献调研 /
           文献调研 / 怎么调研）**且本对话还没有研究链** → 输出引导词让用户选一条路，
           而不是掉进通用回答、也不是擅自开跑。
``query``  查询意图：查本地数据（论文 / 项目 / 产出 / 研究链）。
``chat``   其余 → 通用回答。

为什么要把 ``guide`` 单列
-------------------------
「文献调研」这四个字既可能是提问（「文献调研该怎么做」），也可能是命令（「开始文献调研」）。
之前两种情况都被当成普通聊天，用户看到的是通用助手式的反问清单。
现在把**模糊词**导向一段引导语（三个可点出口），把**带节点名的明确命令**直接执行——
既不擅自花钱，也不把想干活的人挡在门外。
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any, Literal

from services.research import graph

__all__ = [
    "GUIDE_WORDS",
    "Intent",
    "NODE_ALIASES",
    "QUERY_ACTIONS",
    "QUERY_TOPICS",
    "detect_query_topic",
    "resolve_intent",
    "strip_command_words",
]

IntentKind = Literal["node", "guide", "query", "chat"]

#: 执行动作词：与节点名组合即视为**明确命令**
_ACTION_WORDS: tuple[str, ...] = (
    "开始",
    "跑",
    "执行",
    "启动",
    "来做",
    "做一下",
    "搞",
    "进行",
    "continue",
    "run",
    "start",
)

#: 节点别名（含口语说法）。键必须都能映射到 ``graph.NODE_ORDER`` 里的节点。
NODE_ALIASES: dict[str, str] = {
    "文献调研": "literature_review",
    "文献综述": "literature_review",
    "文献检索": "literature_review",
    "调研": "literature_review",
    "查找文献": "literature_review",
    "literature_review": "literature_review",
    "idea": "idea_and_feasibility",
    "想法": "idea_and_feasibility",
    "研究想法": "idea_and_feasibility",
    "假设": "idea_and_feasibility",
    "可行性": "idea_and_feasibility",
    "可行性分析": "idea_and_feasibility",
    "创新点": "idea_and_feasibility",
    "idea_and_feasibility": "idea_and_feasibility",
    "实验准备": "experiment_and_data_preparation",
    "数据准备": "experiment_and_data_preparation",
    "实验设计": "experiment_and_data_preparation",
    "实验方案": "experiment_and_data_preparation",
    "experiment_and_data_preparation": "experiment_and_data_preparation",
    "执行实验": "experiment_execution_and_retries",
    "跑实验": "experiment_execution_and_retries",
    "做实验": "experiment_execution_and_retries",
    "结果分析": "results_analysis",
    "分析结果": "results_analysis",
    "论文写作": "paper_writing",
    "写论文": "paper_writing",
    "写稿": "paper_writing",
    "论文评审": "paper_review",
    "评审": "paper_review",
    "审稿": "paper_review",
}

#: 模糊引导词：**单独出现**时才引导（且本对话还没链）
GUIDE_WORDS: tuple[str, ...] = (
    "怎么开始",
    "开始",
    "研究",
    "怎么研究",
    "怎么文献调研",
    "文献调研",
    "怎么调研",
)

#: 查询动词
QUERY_ACTIONS: tuple[str, ...] = (
    "查",
    "查询",
    "看看",
    "看一下",
    "列出",
    "列一下",
    "搜",
    "有多少",
    "几个",
    "是什么",
    "有哪些",
    "统计",
    "show",
    "list",
    "find",
)

#: 查询主题（决定查哪一类）
QUERY_TOPICS: dict[str, tuple[str, ...]] = {
    "papers": ("论文", "文献", "paper", "文章", "解析卡片", "片段"),
    "projects": ("项目", "project"),
    "artifacts": ("产出", "成果", "证据", "idea", "假设", "可行性", "任务书", "passport", "草稿"),
    "chain": ("研究链", "节点", "流程", "进度", "留痕", "迁移", "研究流程"),
}


@dataclass
class Intent:
    """一次路由的结果。"""

    kind: IntentKind
    #: kind == "node" 时给出目标节点
    node: str | None = None
    #: 命中的词（回显给用户，让他知道判定依据）
    keyword: str = ""
    #: kind == "query" 时给出主题
    topic: str | None = None
    #: 判定说明（面向研究者，不含内部字段名）
    reason: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "kind": self.kind,
            "node": self.node,
            "node_label": graph.NODE_LABELS.get(self.node, "") if self.node else "",
            "keyword": self.keyword,
            "topic": self.topic,
            "reason": self.reason,
        }


def _find_node(text: str) -> tuple[str, str] | None:
    """在文本里找节点别名；返回 ``(node, 命中的别名)``。

    取**最长匹配**："文献调研" 优先于 "调研"，避免长词被短词抢走。
    """

    lowered = text.lower()
    best: tuple[str, str, int] = ("", "", 0)
    for alias, node in NODE_ALIASES.items():
        if alias in lowered and len(alias) > best[2]:
            best = (node, alias, len(alias))
    if best[0]:
        return best[0], best[1]
    return None


def _has_action(text: str) -> str:
    """是否带执行动作词（返回命中的动作词，无则空串）。"""

    lowered = text.lower()
    for word in _ACTION_WORDS:
        if word in lowered:
            return word
    return ""


def _is_bare_guide_word(text: str) -> str:
    """整句是否就是一个模糊引导词（允许少量修饰）。"""

    stripped = re.sub(r"[\s，。！？、,.!?~]+", "", text or "")
    for word in GUIDE_WORDS:
        if stripped == word:
            return word
    return ""


def _looks_like_question(text: str) -> bool:
    """是不是在**提问**（而不是下命令）。"""

    return bool(re.search(r"(怎么|如何|怎样|为什么|是什么|能不能|可不可以|吗|呢)\s*[?？]?$", text or "")) or (
        "?" in (text or "") or "？" in (text or "")
    )


def detect_query_topic(text: str) -> str | None:
    """查询主题：需要**同时**出现查询动词与主题词。"""

    lowered = (text or "").lower()
    if not any(action in lowered for action in QUERY_ACTIONS):
        return None
    for topic, words in QUERY_TOPICS.items():
        for word in words:
            if word in lowered:
                return topic
    return None


def strip_command_words(text: str) -> str:
    """剥掉执行动作词与节点别名，留下**真正的研究问题**。

    为什么需要：「开始文献调研」这七个字如果原样拿去检索，命中一定是 0 ——
    命令词不是研究内容。剥完之后如果什么都不剩，说明用户只下了命令、没给问题，
    调用方应当去别处找研究问题（对话上下文），而不是拿命令当检索词。
    """

    result = text or ""
    for word in _ACTION_WORDS:
        result = result.replace(word, " ")
    for alias in sorted(NODE_ALIASES, key=len, reverse=True):
        result = result.replace(alias, " ")
    for word in GUIDE_WORDS:
        result = result.replace(word, " ")
    result = re.sub(r"[\s，。！？、,.!?：:~的了吧呢吗]+", " ", result)
    return result.strip()


def resolve_intent(
    text: str,
    *,
    has_chain: bool = False,
) -> Intent:
    """判定一句话属于哪一类。

    ``has_chain`` = 本对话是否已经开了研究链。只影响 ``guide`` 的触发：
    已经开了链的对话里再说「开始」这种模糊词，不再反复问，直接当普通对话处理
    （用户想跑哪个节点会说清楚）。
    """

    raw = (text or "").strip()
    if not raw:
        return Intent(kind="chat", reason="空输入")

    found = _find_node(raw)
    action = _has_action(raw)

    # ① 明确执行意图：动作词 + 节点名（顺序不限），且不是在提问
    if found is not None and action and not _looks_like_question(raw):
        node, alias = found
        return Intent(
            kind="node",
            node=node,
            keyword=f"{action}{alias}",
            reason=f"识别为执行意图：准备执行「{graph.NODE_LABELS.get(node, node)}」",
        )

    # ② 模糊引导词 + 还没开链 → 引导
    #    **必须排在「整句即节点名」之前**：「文献调研」这四个字是模糊词（可能是提问，
    #    也可能是要开始），不能因为它是节点别名就直接跑起来——用户没让你花钱。
    guide = _is_bare_guide_word(raw)
    if guide and not has_chain:
        return Intent(
            kind="guide",
            keyword=guide,
            reason=f"「{guide}」既可能是提问也可能是要开始，先问一句想怎么走",
        )

    # ③ 整句就是节点名，且本对话**已经在研究模式**里 → 直接执行
    #    （已经开了链的对话再说「文献调研」，语义就是要跑它，不必反复确认）
    stripped = re.sub(r"[\s，。！？、,.!?~]+", "", raw)
    if found is not None and stripped == found[1] and has_chain:
        node, alias = found
        return Intent(
            kind="node",
            node=node,
            keyword=alias,
            reason=f"整句即节点名（已在研究模式）：执行「{graph.NODE_LABELS.get(node, node)}」",
        )

    # ④ 查询意图
    topic = detect_query_topic(raw)
    if topic is not None:
        return Intent(kind="query", topic=topic, reason=f"识别为查询本地数据：{topic}")

    # ⑤ 其余
    return Intent(kind="chat", reason="普通对话")
