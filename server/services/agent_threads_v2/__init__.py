"""agent_threads_v2：Thread / Turn / Item 领域层与 Agent Tree。

    from services.agent_threads_v2 import ThreadRepository, AgentTree

    repo = ThreadRepository(conversations_root)
    thread = repo.create_thread("我的研究")
    turn = repo.start_turn(thread.thread_id, inputs=[{"text": "开始"}])
    state = repo.state(thread.thread_id)
"""

from __future__ import annotations

from .repository import (
    INDEX_FILENAME,
    THREAD_STATE_FILENAME,
    TOOL_EVENT_TYPES,
    TURN_EVENT_TYPES,
    ThreadRepository,
    ThreadState,
    slugify,
)
from .tree import FINAL_CHILD_STATUSES, AgentTree, ChildOutcome

__all__ = [
    "ThreadRepository",
    "ThreadState",
    "slugify",
    "AgentTree",
    "ChildOutcome",
    "FINAL_CHILD_STATUSES",
    "INDEX_FILENAME",
    "THREAD_STATE_FILENAME",
    "TURN_EVENT_TYPES",
    "TOOL_EVENT_TYPES",
]
