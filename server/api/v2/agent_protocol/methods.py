# Copyright 2026 SciLoop contributors
# Licensed under the Apache License, Version 2.0 (the "License");
"""方法注册表。

一张表同时承担三件事：

1. **方法白名单**——不在表里的方法一律 ``unknown_method``；
2. **参数契约**——``required`` 缺一即 ``invalid_params``，``optional`` 之外的多余参数
   也报错（``invalid_params``），避免拼错的参数被静默忽略；
3. **幂等纪律**——``mutating=True`` 的方法必须带 ``idempotency_key``。

``handler`` 是 :class:`~api.v2.agent.facade.AgentFacade` 上的方法名，全部为
``async def handler(params, context) -> dict``。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from .errors import ProtocolError

#: 参数名 -> 是否必填（表驱动，便于文档与校验共用）
Params = tuple[tuple[str, ...], tuple[str, ...]]


@dataclass(frozen=True)
class MethodSpec:
    """一个方法的元数据。"""

    name: str
    handler: str
    summary: str
    mutating: bool = False
    required: tuple[str, ...] = ()
    optional: tuple[str, ...] = ()
    idempotent: bool = field(default=False)

    def __post_init__(self) -> None:
        # 变更类方法必须幂等，否则断线重试会重复产生副作用
        if self.mutating and not self.idempotent:
            raise ValueError(f"mutating method {self.name} must be idempotent")

    @property
    def allowed(self) -> frozenset[str]:
        return frozenset(self.required) | frozenset(self.optional)

    def to_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "summary": self.summary,
            "mutating": self.mutating,
            "requires_idempotency_key": self.mutating,
            "required": list(self.required),
            "optional": list(self.optional),
        }


def _spec(
    name: str,
    handler: str,
    summary: str,
    *,
    mutating: bool = False,
    required: tuple[str, ...] = (),
    optional: tuple[str, ...] = (),
) -> MethodSpec:
    return MethodSpec(
        name=name,
        handler=handler,
        summary=summary,
        mutating=mutating,
        required=required,
        optional=optional,
        idempotent=mutating,
    )


METHOD_TABLE: dict[str, MethodSpec] = {
    spec.name: spec
    for spec in (
        _spec(
            "protocol/describe",
            "protocol_describe",
            "返回协议版本、冻结契约版本与可用方法清单",
        ),
        # ---------------------------------------------------------------- thread
        _spec(
            "thread/start",
            "thread_start",
            "创建线程（创建本身不调用模型）",
            mutating=True,
            required=("name",),
            optional=("cwd", "model", "settings", "permission_summary"),
        ),
        _spec(
            "thread/list",
            "thread_list",
            "列出线程（默认过滤已归档线程）",
            optional=("include_archived", "limit"),
        ),
        _spec(
            "thread/resume",
            "thread_resume",
            "读取线程全量状态快照，用于刷新页面后重建界面",
            required=("thread_id",),
        ),
        _spec(
            "thread/settings/update",
            "thread_settings_update",
            "更新线程设置（cwd / model / settings / 权限摘要）",
            mutating=True,
            required=("thread_id",),
            optional=("cwd", "model", "settings", "permission_summary"),
        ),
        _spec(
            "thread/delete",
            "thread_delete",
            "归档线程（软删除，事件流与审计记录保留）",
            mutating=True,
            required=("thread_id",),
        ),
        # ------------------------------------------------------------------ turn
        _spec(
            "turn/start",
            "turn_start",
            "启动新 Turn（同一线程只允许一个活动 Turn）",
            mutating=True,
            required=("thread_id",),
            optional=("text", "inputs", "model"),
        ),
        _spec(
            "turn/steer",
            "turn_steer",
            "向正在执行的 Turn 追加输入",
            mutating=True,
            required=("thread_id", "turn_id", "text"),
        ),
        _spec(
            "turn/continue",
            "turn_continue",
            "回答等待输入中的 Turn 并使其继续运行",
            mutating=True,
            required=("thread_id", "turn_id", "text"),
        ),
        _spec(
            "turn/interrupt",
            "turn_interrupt",
            "中断 Turn（不产生成功事件）",
            mutating=True,
            required=("thread_id", "turn_id"),
            optional=("reason",),
        ),
        _spec(
            "turn/recover",
            "turn_recover",
            "恢复 Turn：中断态直接续跑，失败态按原输入重试",
            mutating=True,
            required=("thread_id", "turn_id"),
            optional=("reason",),
        ),
        # ------------------------------------------------------- subscribe / replay
        _spec(
            "thread/subscribe",
            "thread_subscribe",
            "订阅线程事件；带 after_sequence 时先补齐缺失事件",
            required=("thread_id",),
            optional=("after_sequence", "limit"),
        ),
        _spec(
            "thread/unsubscribe",
            "thread_unsubscribe",
            "取消线程订阅",
            required=("thread_id",),
        ),
        _spec(
            "thread/events/replay",
            "thread_events_replay",
            "按游标重放事件（不调用模型）；可选按 call_id 过滤单个工具调用的事件",
            required=("thread_id", "after_sequence"),
            optional=("limit", "call_id"),
        ),
        # --------------------------------------------------------------- approval
        _spec(
            "approval/resolve",
            "approval_resolve",
            "研究者审批决策：批准一次 / 本对话始终批准 / 完全访问 / 拒绝 / 取消",
            mutating=True,
            required=("thread_id", "turn_id", "approval_id", "decision"),
            optional=("note",),
        ),
        # ------------------------------------------------------------- compaction
        _spec(
            "thread/compact",
            "thread_compact",
            "执行一次上下文压缩",
            mutating=True,
            required=("thread_id",),
            optional=("trigger",),
        ),
        _spec(
            "thread/compaction/list",
            "thread_compaction_list",
            "列出线程的所有摘要",
            required=("thread_id",),
        ),
        _spec(
            "thread/compaction/edit",
            "thread_compaction_edit",
            "编辑摘要文本",
            mutating=True,
            required=("thread_id", "summary_id", "text"),
        ),
        _spec(
            "thread/compaction/restore",
            "thread_compaction_restore",
            "把历史摘要恢复为生效摘要",
            mutating=True,
            required=("thread_id", "summary_id"),
        ),
        # ----------------------------------------------------------------- memory
        _spec(
            "memory/list",
            "memory_list",
            "列出记忆（scope 为 user / project / conversation）",
            required=("scope",),
            optional=("scope_id", "include_deleted"),
        ),
        _spec(
            "memory/update",
            "memory_update",
            "新建或更新一条记忆",
            mutating=True,
            required=("scope", "scope_id", "text"),
            optional=("memory_id", "tags", "source_event_ids"),
        ),
        _spec(
            "memory/delete",
            "memory_delete",
            "软删除一条记忆",
            mutating=True,
            required=("memory_id",),
        ),
        # ------------------------------------------------------------- agent tree
        _spec(
            "agent/list",
            "agent_list",
            "列出一个线程的子 Agent 与收敛状态",
            required=("thread_id",),
        ),
        _spec(
            "agent/wait",
            "agent_wait",
            "等待一个或多个子 Agent 收敛（不因单个子 Agent 失败而中止）",
            required=("thread_id", "child_thread_ids"),
            optional=("timeout_s",),
        ),
        _spec(
            "agent/interrupt",
            "agent_interrupt",
            "中断子 Agent",
            mutating=True,
            required=("child_thread_id",),
            optional=("thread_id", "reason"),
        ),
    )
}


def method_spec(name: str) -> MethodSpec:
    """取方法元数据；未注册的方法抛 ``unknown_method``。"""
    spec = METHOD_TABLE.get(name)
    if spec is None:
        raise ProtocolError.unknown_method(name)
    return spec


def validate_params(spec: MethodSpec, params: dict[str, Any]) -> None:
    """校验参数集合：缺必填、出现未知参数都报 ``invalid_params``。"""
    missing = [name for name in spec.required if params.get(name) is None]
    if missing:
        raise ProtocolError.invalid_params(
            f"{spec.name}: missing required parameter(s) {missing}",
            method=spec.name,
            missing=missing,
        )
    unknown = sorted(set(params) - spec.allowed)
    if unknown:
        raise ProtocolError.invalid_params(
            f"{spec.name}: unknown parameter(s) {unknown}",
            method=spec.name,
            unknown=unknown,
            allowed=sorted(spec.allowed),
        )


def describe_methods() -> list[dict[str, Any]]:
    """方法清单（供 ``protocol/describe`` 与前端联调使用）。"""
    return [METHOD_TABLE[name].to_dict() for name in sorted(METHOD_TABLE)]


def method_names() -> list[str]:
    return sorted(METHOD_TABLE)


__all__ = [
    "MethodSpec",
    "METHOD_TABLE",
    "method_spec",
    "method_names",
    "validate_params",
    "describe_methods",
]
