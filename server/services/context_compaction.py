# Copyright 2026 SciLoop contributors
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
"""上下文压缩：超预算时**先压工具结果**，再考虑摘要早期轮次。

用户口径（2026-09-25）
----------------------
单次请求固定 200k 预算（`llm_context_limit_tokens`），超了就压缩；
**但不因为超窗就停止** —— 压完继续干。压缩顺序：**先压工具结果，再摘要早期轮次**。

为什么先压工具结果
------------------
- 它**体积最大**（一次读文件/一次检索的报告动辄几十 KB）；
- 它**时效最弱**（模型只关心结论，不必回看原始大段）；
- 压它是**确定性的**：不额外花一次模型调用，也不会引入新的不确定性 ——
  而"摘要早期轮次"要花模型调用，所以排在后面。

⚠️ 与「单条工具结果不截断」的区别（同一条用户口径的另一半）
----------------------------------------------------------
"不截断"说的是**新结果回喂时**保持原样（模型要看到完整事实，不能替它裁）；
这里压的是**历史里较早的结果** —— 两者不冲突，压的时候必须**如实标注**被压过，
绝不能让模型以为它看到的还是全文。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

__all__ = [
    "EARLY_KEEP_TAIL",
    "SUMMARY_MAX_CHARS",
    "SUMMARY_SYSTEM",
    "TOOL_RESULT_KEEP_CHARS",
    "CompactionOutcome",
    "apply_turn_summary",
    "build_summary_prompt",
    "compress_tool_results",
    "placeholder_for",
    "select_summary_span",
    "summarize_early_turns",
]

#: 压工具结果时保留的前缀长度：够模型认出"这是什么结果"，又不至于重新占满预算
TOOL_RESULT_KEEP_CHARS = 400
#: 摘要早期轮次时，**尾部保留多少条**原样（正在被推理的上下文不能动）
EARLY_KEEP_TAIL = 10
#: 摘要正文的长度上限（字符）：摘要本身也要便宜，否则白压
SUMMARY_MAX_CHARS = 1200

SUMMARY_SYSTEM = (
    "你是对话压缩器。把较早的一段对话压成**事实摘要**：保留研究问题、已确认的口径、"
    "已得到的结论与出处线索、未完成的待办；丢掉寒暄与重复。"
    "只输出摘要正文，不要输出思考过程、前后缀或代码块围栏。"
)


@dataclass
class CompactionOutcome:
    """一次压缩的结果（给调用方落盘 / 告知研究者用）。"""

    #: 真的改了内容才为 True
    changed: bool = False
    #: 被压的消息条数
    count: int = 0
    #: 被压消息原本的字符总数
    original_chars: int = 0
    #: 压完剩下的字符总数
    kept_chars: int = 0
    #: 被压消息在 `messages` 里的下标（从旧到新），落盘时用来描述"遮蔽区间"
    indexes: list[int] = field(default_factory=list)

    @property
    def freed_chars(self) -> int:
        return max(0, self.original_chars - self.kept_chars)

    def as_record(self) -> dict[str, Any]:
        """落进会话文件的 `compactions[]` 一条记录。"""

        return {
            "kind": "tool_results",
            "count": self.count,
            "indexes": list(self.indexes),
            "original_chars": self.original_chars,
            "kept_chars": self.kept_chars,
            "freed_chars": self.freed_chars,
        }


def placeholder_for(original_chars: int, keep_chars: int) -> str:
    """被压掉的那段换成什么 —— **必须如实说明**，不能让模型以为看到了全文。"""

    return (
        f"\n…（这条较早的工具结果**已被压缩以释放上下文**：原文 {original_chars:,} 字符，"
        f"上面保留了前 {keep_chars:,} 字符。需要原始内容请重新执行一次该工具。）"
    )


def compress_tool_results(
    messages: list[dict[str, Any]],
    *,
    keep_chars: int = TOOL_RESULT_KEEP_CHARS,
) -> CompactionOutcome:
    """把**较早的**工具结果压成"前 N 字符 + 如实说明"。

    只动 `role == "tool"` 的消息，且**从旧到新**压 —— 保留最近的结果完整，
    因为模型正在基于它们推理。已经是压缩态的（带上占位说明）不重复处理。

    ⚠️ 就地修改传入的 `messages`：它本来就是"这次请求要发出去的那份"，
    而会话文件里的原始轮次**不受影响**（压缩只影响模型视图）。
    """

    outcome = CompactionOutcome()
    for index, message in enumerate(messages or []):
        if str(message.get("role") or "") != "tool":
            continue
        content = message.get("content")
        if not isinstance(content, str) or len(content) <= keep_chars:
            continue
        if "已被压缩以释放上下文" in content:
            continue  # 已经是压缩态，别二次套娃
        kept = content[:keep_chars]
        message["content"] = kept + placeholder_for(len(content), keep_chars)
        outcome.changed = True
        outcome.count += 1
        outcome.original_chars += len(content)
        outcome.kept_chars += len(message["content"])
        outcome.indexes.append(index)
    return outcome


# --------------------------------------------------------------------------- #
# 第二步：摘要早期轮次（压完工具结果仍超预算时）
#
# ⚠️ 只能从**前缀**里裁，而且裁点要前推 ——
# `assistant.tool_calls` 与它后面的 `tool` 消息是**成对**的协议结构：
# 把中间一段抽掉、让 `tool` 消息失去前驱，供应商会直接 400
# （报的还是"参数错误"，看不出是自家裁出来的）。所以：
#   · 开头的 system 消息永不压（它承载角色与输出规范）；
#   · 尾部 keep_tail 条原样保留（模型正在基于它们推理）；
#   · 首个**保留**的消息若是 `tool`，就把裁点往前推一条（把它前面的 assistant 也放进保留区）。
# --------------------------------------------------------------------------- #
def select_summary_span(
    messages: list[dict[str, Any]],
    *,
    keep_tail: int = EARLY_KEEP_TAIL,
) -> tuple[int, int] | None:
    """选出可摘要的**前缀区间** ``[start, end)``；不值得压时返回 None。"""

    total = len(messages or [])
    start = 0
    while start < total and str(messages[start].get("role") or "") == "system":
        start += 1  # 开头的 system 消息永不压

    end = total - max(0, keep_tail)
    if end - start < 2:
        # 尾部预算比可用消息还多（短历史 / 首轮就超预算）→ **退让**：只留最后一条，
        # 其余全压。否则"保留 10 条"会让短对话永远压不动，而它们恰恰是最容易超预算的。
        end = total - 1
    # 首个保留项不能是 tool（会失去它的 assistant 前驱）
    # ⚠️ `end < total` 不能省：`keep_tail=0` 时 `end == total`，下标越界。
    while start < end < total and str(messages[end].get("role") or "") == "tool":
        end -= 1
    # 区间里至少要有两条消息才值得压（一条的话省不下什么，还要搭一次模型调用）
    if end - start < 2:
        return None
    return start, end


def build_summary_prompt(messages: list[dict[str, Any]], span: tuple[int, int]) -> str:
    """把待摘要的区间拼成提示词正文（角色 + 内容，工具结果按"工具返回"标记）。"""

    start, end = span
    lines: list[str] = []
    for message in messages[start:end]:
        role = str(message.get("role") or "")
        content = message.get("content")
        if not isinstance(content, str):
            content = str(content or "")
        label = {"user": "研究者", "assistant": "助手", "tool": "工具返回"}.get(role, role or "未知")
        lines.append(f"[{label}] {content.strip()}")
    return "\n\n".join(lines).strip()


async def summarize_early_turns(
    messages: list[dict[str, Any]],
    *,
    span: tuple[int, int],
    model_ref: str | None,
) -> dict[str, Any] | None:
    """用一次模型调用把区间压成摘要。失败/没有可用模型 → None（调用方保持原样继续）。

    返回的记录直接进会话级 `compactions[]`：**摘要正文 + 遮蔽区间 + 遮蔽字符数**。
    """

    if not model_ref:
        return None
    body = build_summary_prompt(messages, span)
    if not body:
        return None

    from llm import adapter

    try:
        result = await adapter.chat(
            [
                {"role": "system", "content": SUMMARY_SYSTEM},
                {"role": "user", "content": body},
            ],
            model_ref=model_ref,
            temperature=0.0,
            purpose="context_summary",
            allow_fallback=False,
            strict_logging=False,
        )
    except Exception:  # noqa: BLE001 - 摘要失败不该把对话带崩，保持原样继续
        return None

    summary = str(getattr(result, "content", "") or "").strip()
    if not summary:
        return None
    if len(summary) > SUMMARY_MAX_CHARS:
        summary = summary[:SUMMARY_MAX_CHARS] + "…"
    start, end = span
    return {
        "kind": "turns",
        "indexes": list(range(start, end)),
        "count": end - start,
        "original_chars": len(body),
        "summary_chars": len(summary),
        "summary": summary,
    }


def apply_turn_summary(
    messages: list[dict[str, Any]],
    *,
    span: tuple[int, int],
    summary: str,
) -> int:
    """把区间替换成**一条**摘要消息，返回释放掉的字符数。

    用 `user` 角色承载：部分供应商不接受对话中途出现 `system` 消息，
    而 `user` 角色各处都收（摘要里也明说了它是什么）。
    """

    start, end = span
    original = sum(
        len(str(message.get("content") or "")) for message in messages[start:end]
    )
    note = {
        "role": "user",
        "content": (
            "（较早的对话**已压缩为下面这段摘要**，原始轮次仍完整保存在会话记录里。"
            "需要细节时可以重新发起对应操作。）\n\n"
            f"{summary}"
        ),
    }
    messages[start:end] = [note]
    return max(0, original - len(note["content"]))
