"""agent.v2 契约版本声明。

契约版本是跨 Agent 协作的唯一锚点：Agent 2 / Agent 3 从冻结标记
`agent-v2-contract-v1` 创建 worktree，因此版本字符串必须显式出现在
每一条事件里，便于日后做向后兼容判定。

破坏性变更（需要提升主版本）：
- 删除字段；
- 改变字段含义；
- 枚举重命名或语义改变；
- 收紧必填约束。
"""

from __future__ import annotations

#: 当前契约版本。事件、模型流、记忆记录等所有持久化对象都必须携带该值。
CONTRACT_VERSION: str = "agent.v2.contract.v1"

#: 契约的 Git 冻结标记名（由协调 Agent 验证后打上）。
CONTRACT_FREEZE_TAG: str = "agent-v2-contract-v1"

#: 允许读取的契约版本集合（向前兼容读取用）。
READABLE_CONTRACT_VERSIONS: frozenset[str] = frozenset({CONTRACT_VERSION})


def is_readable(version: str) -> bool:
    """判断某个持久化对象的契约版本是否可读。"""
    return version in READABLE_CONTRACT_VERSIONS


__all__ = ["CONTRACT_VERSION", "CONTRACT_FREEZE_TAG", "READABLE_CONTRACT_VERSIONS", "is_readable"]
