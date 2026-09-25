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
    "TOOL_RESULT_KEEP_CHARS",
    "CompactionOutcome",
    "compress_tool_results",
    "placeholder_for",
]

#: 压工具结果时保留的前缀长度：够模型认出"这是什么结果"，又不至于重新占满预算
TOOL_RESULT_KEEP_CHARS = 400


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
