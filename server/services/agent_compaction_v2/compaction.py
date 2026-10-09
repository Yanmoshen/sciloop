"""压缩（Compaction）领域流程（Agent 1 / WP-06）。

计划书 §4.6 的要求：

- token 预算触发 + 用户手动触发；
- ``compaction/started`` / ``compaction/completed`` / ``compaction/failed`` 三类事件齐全；
- **摘要成功前不替换活动上下文**——失败时只写 failed 事件，生效摘要不变；
- 原始历史与「压缩前快照」都保留（快照走 ``snapshots/<sequence>.json``）；
- 摘要可查看、可编辑、可恢复；
- 压缩调用模型只经过 ModelGateway，不依赖前端或 API。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from contracts.agent_v2.cancellation import CancelToken
from contracts.agent_v2.clock import Clock, SystemClock
from contracts.agent_v2.enums import EventType, ModelStreamKind
from contracts.agent_v2.errors import ModelStreamError, SummaryNotFound
from contracts.agent_v2.ids import new_id
from contracts.agent_v2.models import ModelRequest
from services.agent_runtime_v2.context import (
    build_context,
    estimate_tokens,
    item_to_message,
    transcript,
)

from .policy import CompactionPolicy, CompactionTrigger
from .recap import RECAP_INSTRUCTIONS, Recap, coerce_recap

DEFAULT_SUMMARIZER_PROMPT = (
    "你是一个对话压缩器。请把给定的对话历史压缩成一份信息密度高的摘要，"
    "必须保留：用户的明确诉求与约束、已经确认的决定、未完成的事项、"
    "关键事实与数字、以及仍然悬而未决的风险。不要引入新信息，不要编造。"
)


@dataclass
class CompactionResult:
    """一次压缩的结果。``ok=False`` 时活动上下文**保持不变**。"""

    ok: bool
    trigger: str
    summary_id: str | None = None
    covered_until: int | None = None
    snapshot_sequence: int | None = None
    text: str | None = None
    error: dict[str, Any] | None = None
    tokens_before: int = 0
    tokens_after: int = 0

    def to_dict(self) -> dict[str, Any]:
        return {
            "ok": self.ok,
            "trigger": self.trigger,
            "summary_id": self.summary_id,
            "covered_until": self.covered_until,
            "snapshot_sequence": self.snapshot_sequence,
            "tokens_before": self.tokens_before,
            "tokens_after": self.tokens_after,
            "error": self.error,
        }


class CompactionService:
    """压缩服务。"""

    def __init__(
        self,
        *,
        repo: Any,
        gateway: Any,
        clock: Clock | None = None,
        token_budget: int = 6000,
        policy: CompactionPolicy | None = None,
        system_prompt: str | None = None,
        summarizer_prompt: str = DEFAULT_SUMMARIZER_PROMPT,
    ) -> None:
        self.repo = repo
        self.gateway = gateway
        self.clock = clock or SystemClock()
        self.token_budget = int(token_budget)
        # 策略与执行解耦：策略只回答「该不该压」，执行仍在本服务
        self.policy = policy or CompactionPolicy(token_budget=int(token_budget))
        self.system_prompt = system_prompt
        self.summarizer_prompt = summarizer_prompt

    # ------------------------------------------------------------------ 触发判定
    def estimate(self, thread_id: str) -> int:
        state = self.repo.state(thread_id)
        return estimate_tokens(build_context(state, system_prompt=self.system_prompt))

    def should_compact(self, thread_id: str) -> bool:
        """token 预算触发判定（压缩生效后上下文自然回落，不会反复触发）。"""
        return self.policy.should_compact(
            estimated_tokens=self.estimate(thread_id),
            new_events_since_last=self.new_events_since_last_compaction(thread_id),
        )

    def new_events_since_last_compaction(self, thread_id: str) -> int:
        """距上次压缩有多少条**非压缩**事件（用于防止空转）。"""
        state = self.repo.state(thread_id)
        active = state.active_compaction()
        if not active:
            return len(self.repo.store(thread_id).read_all())
        anchor = int(active.get("sequence") or 0)
        return sum(
            1
            for event in self.repo.store(thread_id).read_all()
            if event.sequence > anchor and not event.type.startswith("compaction/")
        )

    def decide_trigger(self, thread_id: str, *, manual: bool = False, context_overflow: bool = False):
        """返回建议的触发来源（或 None）。供路由/运行时调用。"""
        return self.policy.decide(
            estimated_tokens=self.estimate(thread_id),
            new_events_since_last=self.new_events_since_last_compaction(thread_id),
            manual_request=manual,
            context_overflow=context_overflow,
        )

    def recap(self, thread_id: str, summary_id: str) -> Recap:
        """取回某条摘要的结构化 recap（若无结构信息则回落到解析文本）。"""
        entry = self.summary(thread_id, summary_id)
        raw = entry.get("recap")
        if isinstance(raw, dict):
            return Recap.from_dict(raw)
        return coerce_recap(entry.get("text"))

    # ------------------------------------------------------------------ 执行
    async def compact(
        self,
        thread_id: str,
        *,
        trigger: str = "auto",
        cancel: CancelToken | None = None,
        turn_id: str | None = None,
    ) -> CompactionResult:
        """执行一次压缩。

        :param turn_id: 触发本次压缩的 Turn（可选）。带上它之后，
            「哪一轮触发了压缩」「压缩失败导致哪一轮受影响」都能从事件流里查出来。
        """
        state = self.repo.state(thread_id)
        store = self.repo.store(thread_id)
        events = store.read_all()
        if not events:
            return CompactionResult(
                ok=False,
                trigger=trigger,
                error={"code": "empty_thread", "message": "thread has no events to compact"},
            )
        covered_until = events[-1].sequence
        active = state.active_compaction()
        if active is not None:
            # 判定「有没有新内容」必须**排除压缩自身产生的事件**，
            # 否则 compaction/completed 会让序号一直前进，导致反复压缩空转。
            active_seq = int(active.get("sequence") or 0)
            has_new_content = any(
                e.sequence > active_seq and not e.type.startswith("compaction/") for e in events
            )
            if not has_new_content:
                return CompactionResult(
                    ok=False,
                    trigger=trigger,
                    covered_until=covered_until,
                    error={"code": "nothing_new", "message": "no new events since last compaction"},
                )

        messages = build_context(state, system_prompt=self.system_prompt)
        tokens_before = estimate_tokens(messages)

        store.emit(
            EventType.COMPACTION_STARTED,
            payload={
                "trigger": trigger,
                "trigger_policy": self.policy.describe(
                    _trigger_enum(trigger), estimated_tokens=tokens_before
                ),
                "covered_until": covered_until,
                "tokens_before": tokens_before,
            },
            turn_id=turn_id,
        )
        # 压缩前快照：序号对齐 covered_until，恢复时其后的压缩事件仍会被重放
        snapshot_sequence = store.write_snapshot(state.to_dict(), sequence=covered_until)

        try:
            text = await self._summarize(thread_id, messages, cancel=cancel)
        except Exception as exc:  # noqa: BLE001 - 任何失败都必须落 failed 事件
            error = {
                "code": "summarize_failed",
                "message": f"{type(exc).__name__}: {exc}",
                "error_class": getattr(exc, "error_class", "fatal"),
            }
            store.emit(
                EventType.COMPACTION_FAILED,
                payload={
                    "trigger": trigger,
                    "covered_until": covered_until,
                    "snapshot_sequence": snapshot_sequence,
                    "error": error,
                },
                turn_id=turn_id,
            )
            return CompactionResult(
                ok=False,
                trigger=trigger,
                covered_until=covered_until,
                snapshot_sequence=snapshot_sequence,
                tokens_before=tokens_before,
                error=error,
            )

        summary_id = new_id("summary")
        # tokens_after 按「压缩后真实上下文」估算：摘要 + covered_until 之后的 Item
        # 结构化 recap：模型若按结构输出，则摘要即为 recap 渲染结果；
        # 若模型没给结构（例如纯文本摘要），回落原文，保证信息不丢。
        recap: Recap = coerce_recap(text)
        rendered = recap.render() or text
        tail_messages: list[dict[str, Any]] = []
        for item_id in state.item_order:
            item = state.items[item_id]
            if item.sequence <= covered_until:
                continue
            message = item_to_message(item)
            if message is not None:
                tail_messages.append(message)
        tokens_after = estimate_tokens(
            [{"role": "system", "content": rendered}, *tail_messages]
        )
        store.emit(
            EventType.COMPACTION_COMPLETED,
            payload={
                "summary_id": summary_id,
                "summary": rendered,
                "recap": recap.to_dict(),
                "covered_until": covered_until,
                "snapshot_sequence": snapshot_sequence,
                "trigger": trigger,
                "tokens_before": tokens_before,
                "tokens_after": tokens_after,
            },
            turn_id=turn_id,
        )
        return CompactionResult(
            ok=True,
            trigger=trigger,
            summary_id=summary_id,
            covered_until=covered_until,
            snapshot_sequence=snapshot_sequence,
            text=rendered,
            tokens_before=tokens_before,
            tokens_after=tokens_after,
        )

    async def _summarize(
        self, thread_id: str, messages: list[dict[str, Any]], *, cancel: CancelToken | None
    ) -> str:
        """调用模型产出摘要（只记录 model/* 事件到本线程，不改动 Turn 状态）。"""
        body = transcript(messages)
        request = ModelRequest(
            request_id=f"{thread_id}#compaction",
            messages=[
                {"role": "system", "content": f"{self.summarizer_prompt}\n\n{RECAP_INSTRUCTIONS}"},
                {"role": "user", "content": body},
            ],
            model=None,
            thread_id=thread_id,
        )
        collected: list[str] = []
        async for item in self.gateway.stream(request, cancel):
            kind = getattr(item, "kind", None)
            if kind == ModelStreamKind.TEXT_DELTA.value:
                collected.append(item.text)
            elif kind == ModelStreamKind.ERROR.value:
                raise ModelStreamError(
                    str(getattr(item, "error_class", "fatal")),
                    str(getattr(item, "message", "summarize failed")),
                )
        text = "".join(collected).strip()
        if not text:
            raise ModelStreamError("fatal", "summarizer produced empty summary")
        return text

    # ------------------------------------------------------------------ 查看 / 编辑 / 恢复
    def summaries(self, thread_id: str) -> list[dict[str, Any]]:
        """所有摘要（含被编辑后的生效文本）。"""
        state = self.repo.state(thread_id)
        out: dict[str, dict[str, Any]] = {}
        for entry in state.compactions:
            summary_id = entry.get("summary_id")
            if not summary_id:
                continue
            phase = entry.get("phase")
            if phase == "completed":
                out[summary_id] = {
                    "summary_id": summary_id,
                    "text": entry.get("summary"),
                    "covered_until": entry.get("covered_until"),
                    "snapshot_sequence": entry.get("snapshot_sequence"),
                    "trigger": entry.get("trigger"),
                    "created_at": entry.get("created_at"),
                    "edited": False,
                    "active": False,
                }
            elif phase == "edited" and summary_id in out:
                out[summary_id]["text"] = entry.get("text")
                out[summary_id]["edited"] = True
                out[summary_id]["updated_at"] = entry.get("created_at")
                out[summary_id]["edited_by"] = entry.get("actor")
        active = state.active_compaction() or {}
        active_id = active.get("summary_id")
        if active_id in out:
            out[active_id]["active"] = True
        return sorted(out.values(), key=lambda item: str(item.get("created_at") or ""))

    def summary(self, thread_id: str, summary_id: str) -> dict[str, Any]:
        for entry in self.summaries(thread_id):
            if entry["summary_id"] == summary_id:
                return entry
        raise SummaryNotFound(f"summary {summary_id} not found in thread {thread_id}")

    def edit_summary(
        self,
        thread_id: str,
        summary_id: str,
        text: str,
        *,
        actor: str = "user",
    ) -> dict[str, Any]:
        """编辑摘要（追加 ``compaction/edited``，不修改历史事件）。"""
        current = self.summary(thread_id, summary_id)
        self.repo.emit_event(
            thread_id,
            EventType.COMPACTION_EDITED,
            payload={
                "summary_id": summary_id,
                "text": text,
                "previous_text": current.get("text"),
                "actor": actor,
            },
        )
        return self.summary(thread_id, summary_id)

    def restore_summary(self, thread_id: str, summary_id: str) -> dict[str, Any]:
        """把某个历史摘要恢复为生效摘要。"""
        self.summary(thread_id, summary_id)
        self.repo.emit_event(
            thread_id,
            EventType.COMPACTION_RESTORED,
            payload={"summary_id": summary_id},
        )
        return self.summary(thread_id, summary_id)

    def snapshot_path(self, thread_id: str, sequence: int) -> str:
        """压缩前快照的路径（审计用）。"""
        store = self.repo.store(thread_id)
        return str(store.snapshots_dir / f"{int(sequence)}.json")


def _trigger_enum(trigger: str) -> CompactionTrigger:
    try:
        return CompactionTrigger(trigger)
    except ValueError:
        return CompactionTrigger.AUTO


__all__ = ["CompactionService", "CompactionResult", "DEFAULT_SUMMARIZER_PROMPT"]
