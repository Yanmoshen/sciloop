"""agent_memory_v2：用户 / 项目 / 对话三作用域记忆（append-only records.jsonl）。

    from services.agent_memory_v2 import MemoryStore

    store = MemoryStore(knowledge_base / "memories")
    store.write("user", user_id, "偏好中文回答", origin="user", creator="user")

落盘布局：

    memories/user/<user-id>/records.jsonl
    memories/projects/<project-id>/records.jsonl
    memories/threads/<thread-id>/records.jsonl

对外仍返回契约 ``MemoryRecord``；信封字段（创建者 / 置信度 / 内容哈希 / 修订号）
通过 ``store.envelopes(...)`` 与 ``store.history(memory_id)`` 暴露。
"""

from __future__ import annotations

from .models import ENVELOPE_VERSION, MemoryEnvelope, content_hash
from .store import RECORDS_FILENAME, SCOPE_DIRS, MemoryStore

__all__ = [
    "MemoryStore",
    "MemoryEnvelope",
    "SCOPE_DIRS",
    "RECORDS_FILENAME",
    "ENVELOPE_VERSION",
    "content_hash",
]
