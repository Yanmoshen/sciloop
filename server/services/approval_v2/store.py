# Copyright 2026 SciLoop contributors
# Licensed under the Apache License, Version 2.0 (the "License")
"""审批与持续批准规则的持久化（Agent 2 / WP-03）。

文档要求「中断、过期和服务重启能收敛 pending 状态」，因此裁决之外的状态必须落盘：

- :class:`ApprovalStore`：审批视图（pending / decided）的落盘与重启收敛；
- :class:`GrantStore` 已在上层（``grants.py``）负责持续批准规则。

落盘用**原子替换**（临时文件 + replace），避免半截 JSON。
"""

from __future__ import annotations

import json
import threading
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from contracts.agent_v2.enums import ApprovalStatus

from .models import ApprovalView, DecisionScope


@dataclass
class ApprovalStore:
    """审批状态落盘（path=None 时纯内存）。"""

    path: Path | None = None
    _items: dict[str, ApprovalView] = field(default_factory=dict, repr=False)
    _lock: threading.RLock = field(default_factory=threading.RLock, repr=False)

    def __post_init__(self) -> None:
        if self.path is not None:
            self.path = Path(self.path)
            self.path.parent.mkdir(parents=True, exist_ok=True)
            self._load()

    # ------------------------------------------------------------------ 读写
    def save(self, view: ApprovalView) -> ApprovalView:
        with self._lock:
            self._items[view.approval_id] = view
            self._persist()
            return view

    def get(self, approval_id: str) -> ApprovalView | None:
        with self._lock:
            return self._items.get(approval_id)

    def list(self, *, status: str | None = None) -> list[ApprovalView]:
        with self._lock:
            items = list(self._items.values())
        if status is None:
            return items
        return [item for item in items if item.status == status]

    def pending(self, *, thread_id: str | None = None) -> list[ApprovalView]:
        return [
            item
            for item in self.list(status=ApprovalStatus.PENDING.value)
            if thread_id is None or item.thread_id == thread_id
        ]

    def __len__(self) -> int:
        with self._lock:
            return len(self._items)

    # ------------------------------------------------------------------ 收敛
    def converge_on_restart(self, *, reason: str = "service_restart") -> list[ApprovalView]:
        """服务重启：所有仍 pending 的审批收敛为过期（**不放行、不重放**）。"""
        converged: list[ApprovalView] = []
        for item in self.pending():
            data = item.to_dict()
            data["status"] = ApprovalStatus.EXPIRED.value
            data["decision_scope"] = f"{DecisionScope.EXPIRED.value}:{reason}"
            data["decided_by"] = "system"
            data["summary"] = (item.summary or "") + f"（{reason}：未裁决的审批已过期）"
            converged.append(self.save(ApprovalView.from_dict(data)))
        return converged

    # ------------------------------------------------------------------ 内部
    def _persist(self) -> None:
        if self.path is None:
            return
        payload = {"approvals": [item.to_dict() for item in self._items.values()]}
        temp = self.path.with_suffix(self.path.suffix + ".tmp")
        temp.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
        temp.replace(self.path)

    def _load(self) -> None:
        if self.path is None or not self.path.is_file():
            return
        try:
            raw = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):  # pragma: no cover - 坏文件跳过
            return
        for item in raw.get("approvals", []):
            try:
                view = ApprovalView.from_dict(item)
            except (TypeError, ValueError):  # pragma: no cover
                continue
            self._items[view.approval_id] = view

    def audit(self) -> list[dict[str, Any]]:
        return [item.to_dict() for item in self.list()]


__all__ = ["ApprovalStore"]
