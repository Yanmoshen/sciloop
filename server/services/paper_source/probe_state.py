# Copyright 2026 SciLoop contributors
# Licensed under the Apache License, Version 2.0 (http://www.apache.org/licenses/LICENSE-2.0)
"""数据源探活结果的**逐来源**持久化留痕（``GET /sources/health`` 的数据来源之一）。

为什么单独存一份、不复用 ``paper_source_records``
------------------------------------------------
``paper_source_records`` 记的是「抓取论文时某字段取到了什么、什么时候取到的」，
它的 ``last_success_at`` 是**论文抓取**时间；而「立即检测」探的是**源本身是否连通**。
两者不是一回事：用户点一次检测，抓取时间的语义不该被顶掉，也不该假装更新了。
因此这里独立记一轮探活的结论（本文件）。

为什么是文件不是表
------------------
本仓库规矩是「同一时间只允许一条线加迁移」，这类状态属于本地运行资产
（与技能库的 ``skills-state.json`` 同类），放 ``knowledge-base/metadata/`` 下即可，
重启保留、不碰迁移链。写入走「临时文件 + ``os.replace``」原子替换，避免半截文件。
"""

from __future__ import annotations

import json
import os
import threading
from pathlib import Path
from typing import Any

from services.storage import knowledge_root

__all__ = [
    "FIELDS",
    "STATE_NAME",
    "load_state",
    "record_probe",
    "state_path",
]

STATE_NAME = "source-probe-state.json"

#: 逐来源保留的探活字段（白名单：只写这些，其余一律丢弃）
FIELDS: tuple[str, ...] = (
    "last_probe_at",
    "last_probe_ok",
    "last_probe_http_status",
    "last_probe_latency_ms",
    "last_probe_detail",
    "last_probe_success_at",
    "last_probe_failure_at",
)

_LOCK = threading.Lock()


def state_path() -> Path:
    return knowledge_root() / "metadata" / STATE_NAME


def _empty() -> dict[str, Any]:
    return {"checked_at": None, "sources": {}}


def load_state() -> dict[str, Any]:
    """读持久化状态；文件缺失/损坏时返回空态（不编造时间与状态）。"""
    path = state_path()
    if not path.is_file():
        return _empty()
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return _empty()
    if not isinstance(data, dict):
        return _empty()
    sources = data.get("sources")
    if not isinstance(sources, dict):
        sources = {}
    cleaned: dict[str, dict[str, Any]] = {}
    for name, node in sources.items():
        if isinstance(node, dict):
            cleaned[str(name)] = {key: node.get(key) for key in FIELDS}
    checked_at = data.get("checked_at")
    return {"checked_at": checked_at if isinstance(checked_at, str) else None, "sources": cleaned}


def _write(state: dict[str, Any]) -> None:
    path = state_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(json.dumps(state, ensure_ascii=False, indent=2), encoding="utf-8")
    os.replace(tmp, path)


def record_probe(results: dict[str, dict[str, Any]], *, checked_at: str) -> dict[str, Any]:
    """把一轮探活结果写进持久化状态，返回写后的完整状态。

    ``results[name]`` 至少含 ``ok``（``True`` / ``False`` / ``None``）、``http_status``、
    ``detail``、``latency_ms``：

    - 成功（``ok is True``）→ 同时更新 ``last_probe_success_at``；
    - 失败（``ok is False``）→ 保留上一次成功时间，只更新 ``last_probe_failure_at``；
    - 未知（``ok is None``，如未配置来源）→ 两个成功/失败时间都不动。
    """
    with _LOCK:
        state = load_state()
        sources: dict[str, dict[str, Any]] = state["sources"]
        for name, item in results.items():
            node = dict(sources.get(name) or {})
            ok = item.get("ok")
            node["last_probe_at"] = checked_at
            node["last_probe_ok"] = None if ok is None else bool(ok)
            node["last_probe_http_status"] = item.get("http_status")
            node["last_probe_latency_ms"] = item.get("latency_ms")
            node["last_probe_detail"] = item.get("detail")
            if ok is True:
                node["last_probe_success_at"] = checked_at
            elif ok is False:
                node["last_probe_failure_at"] = checked_at
            sources[str(name)] = {key: node.get(key) for key in FIELDS}
        state["checked_at"] = checked_at
        _write(state)
        return state
