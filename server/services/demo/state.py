# Copyright 2026 SciLoop contributors
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
"""演示运行时状态（WP16-T5）。

三个开关，环境变量提供**进程启动时的默认值**，运行期可被 ``POST /demo/mode`` 覆盖：

============== ================================ ============================
开关            环境变量                          含义
============== ================================ ============================
access_mode    ``APP_ACCESS_MODE``               public_demo（只读）/ owner_mode
snapshot       ``DEMO_SNAPSHOT_ENABLED``         论文库是否读 ``paper_feed_snapshots``
replay         ``LLM_REPLAY``                    LLM 是否走 ``demo_fixtures`` 回放
============== ================================ ============================

覆盖如何真正生效（诚实说明）
----------------------------
``replay`` 开关会同步写入 ``os.environ["LLM_REPLAY"]``，而
``llm.replay.is_replay_enabled()`` 正是**先读环境变量**再回落配置对象，
因此**服务端即时生效**（下一次 LLM 调用即走回放）。

``snapshot`` 开关会同步写入 ``os.environ["DEMO_SNAPSHOT_ENABLED"]``，但
``app/api/v1/feed.py`` 的 ``_snapshot_enabled()`` 读的是
``core.config.settings``（进程启动时从 ``.env`` 装载的**不可变快照**），
所以运行期关掉 snapshot **不会**让 feed 拒绝 ``?snapshot=demo``。
本包不修改 ``feed.py``（WP04 所有），因此采取双保险并如实披露：

1. ``GET /demo/status`` 返回的 ``snapshot`` 是**前端唯一口径**：前端据此决定是否带
   ``?snapshot=demo``，关掉即回实时；
2. 环境变量同步写入，任何按 ``os.environ`` 读取的消费者即刻生效；
3. 残余风险写进 ``contract_changes_requested``（建议 feed 改为读运行态）。

``reset()`` 恢复**进程启动时**的环境值（不是当前值），保证「演示开关不会被上一次
演示的覆盖残留污染」。

进程内不落库
------------
演示模式是运行态而非业务数据；写入数据库会与「禁止把演示标记当业务事实」冲突，
也会让多副本部署出现状态分叉（当前部署为单容器，已在文档中声明）。
"""

from __future__ import annotations

import logging
import os
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any, Literal

from core.config import get_settings

logger = logging.getLogger("sciloop.wp16.state")

AccessMode = Literal["public_demo", "owner_mode"]

_TRUE = {"1", "true", "yes", "on"}

#: 进程启动时的环境值（``reset()`` 的还原目标）
_INITIAL_ENV: dict[str, str | None] = {
    "DEMO_SNAPSHOT_ENABLED": os.environ.get("DEMO_SNAPSHOT_ENABLED"),
    "LLM_REPLAY": os.environ.get("LLM_REPLAY"),
}


def _as_flag(raw: str | None, default: bool) -> bool:
    if raw is None or raw.strip() == "":
        return default
    return raw.strip().lower() in _TRUE


def resolve_access_mode() -> AccessMode:
    """当前访问面：``.env`` 装载的配置为权威来源（``owner`` 归一为 ``owner_mode``）。"""
    try:
        return get_settings().app_access_mode  # type: ignore[return-value]
    except Exception:  # noqa: BLE001 - 配置中心未就绪时退回环境变量
        raw = (os.environ.get("APP_ACCESS_MODE") or "public_demo").strip().lower()
        return "owner_mode" if raw in {"owner_mode", "owner"} else "public_demo"


def env_snapshot_default() -> bool:
    """进程启动时的 snapshot 默认值（``source_defaults`` 展示用）。"""
    if _INITIAL_ENV["DEMO_SNAPSHOT_ENABLED"] is None:
        try:
            return bool(get_settings().demo_snapshot_enabled)
        except Exception:  # noqa: BLE001
            return True
    return _as_flag(_INITIAL_ENV["DEMO_SNAPSHOT_ENABLED"], True)


def env_replay_default() -> bool:
    """进程启动时的 replay 默认值（``source_defaults`` 展示用）。"""
    if _INITIAL_ENV["LLM_REPLAY"] is None:
        try:
            return bool(get_settings().llm_replay)
        except Exception:  # noqa: BLE001
            return False
    return _as_flag(_INITIAL_ENV["LLM_REPLAY"], False)


def effective_replay_enabled() -> bool:
    """**服务端实际生效**的 replay 开关（与 ``llm.replay`` 同口径）。"""
    from llm.replay import is_replay_enabled

    return is_replay_enabled()


def effective_snapshot_enabled() -> bool:
    """**feed 实际生效**的 snapshot 开关（读 ``settings``，运行期覆盖不到）。"""
    try:
        return bool(get_settings().demo_snapshot_enabled)
    except Exception:  # noqa: BLE001
        return _as_flag(os.environ.get("DEMO_SNAPSHOT_ENABLED"), True)


@dataclass
class DemoRuntimeState:
    """进程内的演示开关状态（可被 Owner 覆盖，不落库、不下发令牌）。"""

    snapshot_override: bool | None = None
    replay_override: bool | None = None
    updated_at: str | None = None
    updated_by: str | None = None

    # -- 读取（覆盖优先，其次进程启动默认）-------------------------------- #
    @property
    def snapshot(self) -> bool:
        return env_snapshot_default() if self.snapshot_override is None else self.snapshot_override

    @property
    def replay(self) -> bool:
        return env_replay_default() if self.replay_override is None else self.replay_override

    @property
    def access_mode(self) -> AccessMode:
        return resolve_access_mode()

    # -- 写入 -------------------------------------------------------------- #
    def apply(
        self,
        *,
        snapshot: bool | None = None,
        replay: bool | None = None,
        actor: str | None = None,
    ) -> dict[str, Any]:
        """覆盖开关、同步环境变量，并返回变更前后状态（供审计与 SSE 广播）。"""
        before = {"snapshot": self.snapshot, "replay": self.replay, "access_mode": self.access_mode}
        if snapshot is not None:
            self.snapshot_override = bool(snapshot)
            os.environ["DEMO_SNAPSHOT_ENABLED"] = "1" if self.snapshot_override else "0"
        if replay is not None:
            self.replay_override = bool(replay)
            os.environ["LLM_REPLAY"] = "1" if self.replay_override else "0"
        self.updated_at = datetime.now(UTC).isoformat()
        self.updated_by = actor or "owner"
        after = {"snapshot": self.snapshot, "replay": self.replay, "access_mode": self.access_mode}
        logger.info("演示开关切换 actor=%s before=%s after=%s", self.updated_by, before, after)
        return {"before": before, "after": after}

    def reset(self) -> None:
        """清除覆盖并还原**进程启动时**的环境值。"""
        self.snapshot_override = None
        self.replay_override = None
        for key, value in _INITIAL_ENV.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value
        self.updated_at = datetime.now(UTC).isoformat()
        self.updated_by = "owner(reset)"


_state = DemoRuntimeState()


def get_demo_state() -> DemoRuntimeState:
    """进程内单例（与 ``llm.replay.is_replay_enabled`` 共用同一份语义）。"""
    return _state


def demo_mode_label() -> str:
    """三态展示口径：``live`` / ``snapshot`` / ``replay``（回放优先，两者同开时标 replay）。"""
    current = get_demo_state()
    if current.replay:
        return "replay"
    if current.snapshot:
        return "snapshot"
    return "live"


def _effect_block() -> dict[str, Any]:
    """逐开关说明「谁在读这个开关、运行期覆盖是否立刻生效」。"""
    current = get_demo_state()
    return {
        "replay": {
            "runtime_lever": "os.environ[LLM_REPLAY]",
            "consumed_by": "llm.replay.is_replay_enabled（先读环境变量，运行期生效）",
            "effective_now": effective_replay_enabled(),
            "instant": True,
        },
        "snapshot": {
            "runtime_lever": "os.environ[DEMO_SNAPSHOT_ENABLED]",
            "consumed_by": (
                "app/api/v1/feed.py::_snapshot_enabled 读 settings（进程启动快照），"
                "运行期关闭不会让 feed 拒绝 ?snapshot=demo"
            ),
            "effective_now": effective_snapshot_enabled(),
            "instant": False,
            "mitigation": (
                "GET /demo/status 是前端唯一口径：snapshot=false 时前端不再带 ?snapshot=demo，"
                "界面回到实时展示；服务端硬拒绝需 WP04 改读运行态（已写入 contract_changes_requested）"
            ),
        },
        "access_mode": {
            "runtime_lever": "APP_ACCESS_MODE（.env，需重启）",
            "consumed_by": "core.security.require_owner + services.demo.access.evaluate_request",
            "effective_now": current.access_mode,
            "instant": False,
            "mitigation": "访问面不提供运行期切换，避免演示中途把只读面变成可写面（安全默认）",
        },
    }


def state_public_view() -> dict[str, Any]:
    """对外状态视图（**不含任何密钥**，只含布尔/枚举/时间戳）。"""
    current = get_demo_state()
    label = demo_mode_label()
    return {
        "access_mode": current.access_mode,
        "snapshot": current.snapshot,
        "replay": current.replay,
        "demo_mode": label,
        "is_demo": label != "live",
        "source_defaults": {
            "snapshot_from_env": env_snapshot_default(),
            "replay_from_env": env_replay_default(),
            "access_mode_from_env": resolve_access_mode(),
        },
        "overrides": {
            "snapshot": current.snapshot_override,
            "replay": current.replay_override,
        },
        "effect": _effect_block(),
        "updated_at": current.updated_at,
        "updated_by": current.updated_by,
        "notes": [
            "snapshot=1 时论文库读 paper_feed_snapshots 并返回 data_source=snapshot",
            "replay=1 时 LLM 从 demo_fixtures 回放并标 is_replay=true（未命中即报错，不静默走实时）",
            "任一为真都必须在界面常驻 DemoBadge，禁止当作实时结果",
            "演示开关不下发令牌、不落库；reset 还原进程启动时的环境值",
        ],
    }
