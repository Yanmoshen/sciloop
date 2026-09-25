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
    "COMMAND_HINTS_EN",
    "COMMAND_HINTS_ZH",
    "GUIDE_WORDS",
    "Intent",
    "NODE_ALIASES",
    "QUERY_ACTIONS",
    "QUERY_TOPICS",
    "ROUTE_TIMEOUT_SECONDS",
    "classify_with_model",
    "detect_query_topic",
    "looks_like_command",
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


# --------------------------------------------------------------------------- #
# 规则兜明确的 + **模型兜底**（2026-09-25 用户口径）
#
# 为什么要有模型这一层：规则是**词表匹配**，中英混排与口语说法盖不住 ——
# 实测 `_find_node` 是纯子串匹配（只做 lower），别名表里是 `literature_review`（下划线）、
# 动作词只有 `开始/跑/执行/启动/continue/run/start`，于是
# 「start a literature review」这类英文祈使句**必然掉进通用回答**。
#
# 但兜底不能无条件：它要花一次模型调用、也加延迟。所以先判**像不像指令**：
# 纯闲聊（「你好」「谢谢」）与提问（「文献调研该怎么做」）都不过这一关 ——
# 不为闲聊付钱、也不给它加延迟。
# --------------------------------------------------------------------------- #

#: 中文祈使/委托线索
COMMAND_HINTS_ZH: tuple[str, ...] = (
    "帮我",
    "请你",
    "请帮",
    "给我",
    "替我",
    "麻烦",
    "做一下",
    "做个",
    "来一下",
    "帮我做",
    "开始",
    "跑",
    "执行",
    "启动",
    "搞",
    "进行",
    "继续",
    "接着",
)

#: 英文祈使/委托线索（用户口径：中英文都有）
COMMAND_HINTS_EN: tuple[str, ...] = (
    "please",
    "help me",
    "run",
    "start",
    "begin",
    "execute",
    "launch",
    "go ahead",
    "carry on",
    "continue",
    "do a",
    "make a",
)


def looks_like_command(text: str) -> bool:
    """这句话像不像「要我做事」（而不是闲聊或提问）。

    只决定**值不值得**多花一次模型调用去判意图，不决定意图本身。
    提问一律不算指令：带问号 / 以「怎么、如何、是什么、吗」收尾的都不走模型兜底。
    """

    raw = (text or "").strip()
    if not raw:
        return False
    if _looks_like_question(raw):
        return False
    lowered = raw.lower()
    if _has_action(raw) or _find_node(raw) is not None:
        return True
    if any(word in raw for word in COMMAND_HINTS_ZH):
        return True
    return any(word in lowered for word in COMMAND_HINTS_EN)


#: 模型兜底的超时（用户口径：超时就回退规则结果，绝不把对话卡在路由上）
ROUTE_TIMEOUT_SECONDS = 1.5

ROUTE_SYSTEM = (
    "你是一个**意图判定器**。只输出一个 JSON 对象，"
    "不要输出解释、思考过程、前后缀或代码块围栏。"
)

ROUTE_TEMPLATE = (
    "判断下面这句话属于哪一类，只输出 JSON（**花括号照抄，不要加围栏**）：\n"
    '{{"kind": "node|query|chat", "node": "<节点 id，仅 kind=node 时>", '
    '"topic": "<主题，仅 kind=query 时>", "reason": "<一句话依据>"}}\n\n'
    "判定边界（**严格照此，别热情过头**）：\n"
    "- node：**明确要求开始执行**某个环节（祈使句、有动作词）。只是提到某个环节名、"
    "或者拿不准 → **一律不要 node**；\n"
    "- query：在问本地已有的数据（库里有什么论文、我的项目、上次的分析结果）；\n"
    "- chat：其余全部（闲聊、提问、想法讨论）。\n\n"
    "可用节点 id 与含义：\n{nodes}\n\n"
    "本对话是否已经开过研究流程：{chained}\n"
    "要判断的话：{text}"
)


def _extract_json_object(text: str) -> dict[str, Any] | None:
    """从模型输出里取出第一个 JSON 对象（容忍它前后夹带文字/围栏）。"""

    import json

    raw = text or ""
    start = raw.find("{")
    while start >= 0:
        try:
            value, _end = json.JSONDecoder().raw_decode(raw[start:])
        except ValueError:
            start = raw.find("{", start + 1)
            continue
        return value if isinstance(value, dict) else None
    return None


async def classify_with_model(
    text: str,
    *,
    model_ref: str | None,
    has_chain: bool,
) -> Intent | None:
    """让模型判一次意图。**任何失败/超时都返回 None** → 调用方回落到规则结果。

    只接受**清单里的**节点 id 与**已知的**查询主题 —— 模型给的值一律当"不可信输入"，
    不认识的节点名直接降级成 chat，绝不让它凭一个字符串把某个流程跑起来。
    """

    if not model_ref:
        return None

    from llm import adapter

    nodes = "\n".join(f"- {key}：{label}" for key, label in graph.NODE_LABELS.items())
    prompt = ROUTE_TEMPLATE.format(
        nodes=nodes, chained="是" if has_chain else "否", text=str(text or "").strip()
    )
    try:
        result = await adapter.chat(
            [
                {"role": "system", "content": ROUTE_SYSTEM},
                {"role": "user", "content": prompt},
            ],
            model_ref=model_ref,
            temperature=0.0,
            purpose="intent_route",
            allow_fallback=False,
            strict_logging=False,
            timeout=ROUTE_TIMEOUT_SECONDS,
        )
    except Exception:  # noqa: BLE001 - 路由失败不该把对话带崩
        return None

    payload = _extract_json_object(str(getattr(result, "content", "") or ""))
    if payload is None:
        return None

    kind = str(payload.get("kind") or "").strip()
    reason = str(payload.get("reason") or "").strip()[:120]
    if kind == "node":
        node = str(payload.get("node") or "").strip()
        if node not in graph.NODE_LABELS:
            # 模型编了个不存在的节点名 → 降级，绝不放行
            return Intent(kind="chat", reason=f"模型给的节点名不在清单里（{node or '空'}），按普通对话处理")
        return Intent(
            kind="node",
            node=node,
            keyword=node,
            reason=f"模型判定为执行意图：{graph.NODE_LABELS.get(node, node)}" + (f"（{reason}）" if reason else ""),
        )
    if kind == "query":
        topic = str(payload.get("topic") or "").strip()
        if topic not in QUERY_TOPICS:
            return Intent(kind="chat", reason="模型给的查询主题不认识，按普通对话处理")
        return Intent(kind="query", topic=topic, reason=f"模型判定为查询本地数据：{topic}")
    if kind == "chat":
        return Intent(kind="chat", reason="模型判定为普通对话" + (f"（{reason}）" if reason else ""))
    return None
