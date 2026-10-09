"""agent_memory_v2：用户 / 项目 / 对话三作用域记忆存储。

    from services.agent_memory_v2 import MemoryStore

    store = MemoryStore(knowledge_base / "memories")
    store.write("user", "u1", "偏好中文回答", origin="user")
"""

from __future__ import annotations

from .store import SCOPE_DIRS, MemoryStore

__all__ = ["MemoryStore", "SCOPE_DIRS"]
