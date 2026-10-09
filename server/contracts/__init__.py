"""跨 Agent 共享的契约包（Agent 1 首次建立，之后冻结）。

本目录只放**机器可读契约**：JSON Schema、Python 类型、校验器、最小 fake 实现。
不允许放业务实现——实现在 ``server/services/agent_*_v2/`` 下。

冻结标记：``agent-v2-contract-v1``（由协调 Agent 验证后打上）。
其他 Agent 只读；若必须变更，先提交契约变更说明。
"""

from __future__ import annotations

__all__ = ["agent_v2"]
