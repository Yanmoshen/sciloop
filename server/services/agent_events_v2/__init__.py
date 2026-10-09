"""agent_events_v2：追加式事件存储与租约。

对外只需两个类型：

    from services.agent_events_v2 import EventStore, Lease

    store = EventStore(root, folder, thread_id)
    store.emit("turn/started", turn_id=turn_id)
    result = store.recover()
"""

from __future__ import annotations

from .lease import Lease, LeaseState
from .store import (
    EVENTS_FILENAME,
    SNAPSHOT_DIRNAME,
    Corruption,
    EventStore,
    RecoveryResult,
)

__all__ = [
    "EventStore",
    "RecoveryResult",
    "Corruption",
    "Lease",
    "LeaseState",
    "EVENTS_FILENAME",
    "SNAPSHOT_DIRNAME",
]
