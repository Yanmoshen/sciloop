# Copyright 2026 SciLoop contributors
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
"""技能库的状态：装了哪些、开了没、挂了哪些外部目录。

**存在文件里**（`<server_root>/.cache/skills/state.json`），不建表：
与会话、产物一样，是这个产品的"本地资产"；也避免为一件小事动迁移
（本仓库的规矩是**同一时间只允许一条线加迁移**，能不碰就不碰）。

默认全部**启用** —— 装了就是要用的；研究者可以逐个关掉。
"""

from __future__ import annotations

import json
import time
from pathlib import Path
from typing import Any

__all__ = [
    "STATE_NAME",
    "add_mount",
    "cached_health",
    "is_enabled",
    "load_state",
    "mounts",
    "remove_mount",
    "save_health",
    "set_enabled",
    "state_path",
]

STATE_NAME = "skills-state.json"


def state_path(root: Path | str | None = None) -> Path:
    """状态文件位置：**必须在挂载卷里**，否则重建容器就全丢（2026-09-23 实测踩到）。

    `.cache/artifacts` 是挂着的（宿主 `./.data/artifacts`），所以放在它下面；
    下划线开头标明它**不是产物**，只是技能库自己的状态。
    """

    if root is not None:
        return Path(root) / STATE_NAME
    from services.translate.artifacts import default_artifact_root

    return default_artifact_root() / "_skills" / STATE_NAME


def load_state(root: Path | str | None = None) -> dict[str, Any]:
    path = state_path(root)
    if not path.is_file():
        return {"loaded": True, "enabled": {}, "mounts": []}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        # 坏文件不许把技能库整个带崩：当作默认状态，界面能照常显示
        return {"loaded": False, "enabled": {}, "mounts": []}
    if not isinstance(data, dict):
        return {"loaded": False, "enabled": {}, "mounts": []}
    data.setdefault("enabled", {})
    data.setdefault("mounts", [])
    data["loaded"] = True
    return data


def save_state(state: dict[str, Any], root: Path | str | None = None) -> Path:
    path = state_path(root)
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "enabled": dict(state.get("enabled") or {}),
        "mounts": [str(item) for item in (state.get("mounts") or [])],
        # 依赖体检结果（有就跑过，没有就是"还没体检"——界面不许假装知道）
        "health": dict(state.get("health") or {}),
        "updated_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
    }
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    return path


def cached_health(*, root: Path | str | None = None) -> dict[str, Any]:
    """上次体检的结果（没体检过就是空字典）。"""

    return dict(load_state(root).get("health") or {})


def save_health(health: dict[str, Any], *, root: Path | str | None = None) -> Path:
    state = load_state(root)
    state["health"] = health
    return save_state(state, root)


def is_enabled(name: str, *, root: Path | str | None = None) -> bool:
    """**默认启用**：只有显式关掉的才是 False。"""

    state = load_state(root)
    return bool((state.get("enabled") or {}).get(name, True))


def set_enabled(name: str, enabled: bool, *, root: Path | str | None = None) -> dict[str, Any]:
    state = load_state(root)
    enabled_map = dict(state.get("enabled") or {})
    enabled_map[name] = bool(enabled)
    state["enabled"] = enabled_map
    save_state(state, root)
    return {"name": name, "enabled": bool(enabled)}


def mounts(*, root: Path | str | None = None) -> list[str]:
    state = load_state(root)
    return [str(item) for item in (state.get("mounts") or [])]


def add_mount(path: str, *, root: Path | str | None = None) -> dict[str, Any]:
    target = Path(path).expanduser()
    if not target.is_dir():
        return {"ok": False, "message": f"这个目录不存在或不是目录：{path}"}
    state = load_state(root)
    items = [str(item) for item in (state.get("mounts") or [])]
    if str(target) not in items:
        items.append(str(target))
    state["mounts"] = items
    save_state(state, root)
    return {"ok": True, "mounts": items, "message": f"已挂载：{target}"}


def remove_mount(path: str, *, root: Path | str | None = None) -> dict[str, Any]:
    state = load_state(root)
    items = [str(item) for item in (state.get("mounts") or [])]
    remaining = [item for item in items if item != str(path)]
    state["mounts"] = remaining
    save_state(state, root)
    return {"ok": True, "mounts": remaining, "message": f"已取消挂载：{path}"}
