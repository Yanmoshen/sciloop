# Copyright 2026 SciLoop contributors
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
"""流水线调度引擎（WP09-T2 / T5 / T6 / T7 的编排层）。

职责
----
- 六环节固定顺序执行：``survey → plan → plan_review → experiment → writing → review``
- 每环节执行前调用 **WP10 的风险策略** ``evaluate_decision(project_id, decision_point, context)``：
  ``auto_execute`` 继续 / ``need_human`` 转 ``WAIT_HUMAN`` / ``circuit_break`` 熔断
- 断点续跑（以 ``stage_outputs.status`` 为唯一依据）
- 停止条件（三重取先满足者）与失败分级（L1 重试 / L2 降级 / L3 熔断 + 报告）
- 状态机迁移与 SSE 事件推送

WP10 尚未落地时的降级（**透明可审计，绝不假装已有风险判断**）
------------------------------------------------------------
策略不可用时使用 :class:`PolicyStub`：``policy_action="auto_execute"``、
``policy_version="stub-0"``、``rationale`` 以 ``stub:policy_not_mounted`` 开头，
三分（risk/confidence/reversibility）**置 0 并标注不可用**（表列 NOT NULL，故用 0 占位，
并写进 ``guardrail_checks.policy_mounted=false``），全部写入 ``decision_logs``。

禁止：Celery / Redis（用进程内 asyncio 任务）；内存只做缓存，状态一律落库。
"""

from __future__ import annotations

import asyncio
import contextlib
import inspect
import json
import logging
import time
from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from db.models import DecisionLog, Intervention, PipelineRun, Project, StageOutput, Taskbook
from services.pipeline import resume as resume_mod
from services.pipeline import sse as sse_mod
from services.pipeline.failure_handler import (
    D4_OPTIONS,
    assess_failure,
    build_failure_report,
    persist_failure_report,
    suggest_human_actions,
)
from services.pipeline.stages import STAGE_INTERVENTION_NODE, STAGE_ORDER, get_stage
from services.pipeline.stages.base import (
    STAGE_DECISION_POINT,
    StageContext,
    StageError,
    StageResult,
    StageTimeoutError,
)
from services.pipeline.state_machine import (
    ACTIVE_STATUSES,
    TERMINAL_STATUSES,
    IllegalTransition,
    structured_log,
    transition,
)
from services.pipeline.stop_conditions import (
    StopThresholds,
    evaluate_stop_conditions,
    extract_score,
    resolve_thresholds,
)

logger = logging.getLogger("sciloop.pipeline.engine")

WP_ID = "WP09"

#: 策略来源标记（写入 decision_logs.policy_version / guardrail_checks）
POLICY_STUB_VERSION = "stub-0"
STUB_REASON = "stub:policy_not_mounted"

#: ``pipeline_runs.status`` 取值（附录 A.5）
RUN_STATUSES = ("running", "paused", "failed", "circuit_break", "done")


class PipelineError(RuntimeError):
    """引擎级错误（携带契约错误码）。"""

    code = "pipeline_error"

    def __init__(self, code: str, message: str, detail: Any = None) -> None:
        self.code = code
        self.detail = detail
        super().__init__(message)

    def to_dict(self) -> dict[str, Any]:
        return {"code": self.code, "message": str(self), "detail": self.detail}


# --------------------------------------------------------------------------- #
# 策略接口（WP10）与降级桩
# --------------------------------------------------------------------------- #
@dataclass
class PolicyDecision:
    """一次风险策略判定的结果（WP10 返回或桩返回值）。"""

    policy_action: str
    risk_score: float | None = None
    confidence_score: float | None = None
    reversibility_score: float | None = None
    chosen: str | None = None
    rationale: str = ""
    policy_version: str = POLICY_STUB_VERSION
    guardrail_checks: dict[str, Any] = field(default_factory=dict)
    options_considered: list[str] = field(default_factory=list)
    decision_log_id: int | None = None
    degraded: bool = False
    reason: str | None = None
    raw: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "policy_action": self.policy_action,
            "risk_score": self.risk_score,
            "confidence_score": self.confidence_score,
            "reversibility_score": self.reversibility_score,
            "chosen": self.chosen,
            "rationale": self.rationale,
            "policy_version": self.policy_version,
            "guardrail_checks": self.guardrail_checks,
            "options_considered": self.options_considered,
            "decision_log_id": self.decision_log_id,
            "degraded": self.degraded,
            "reason": self.reason,
        }


class PolicyStub:
    """WP10 未挂载时的降级桩。

    **不假装已有风险判断**：动作固定 ``auto_execute``，三分置 ``None``（写库时占位为 0），
    ``rationale`` 以 :data:`STUB_REASON` 开头，``policy_version='stub-0'``。
    """

    name = "policy_stub"
    version = POLICY_STUB_VERSION
    mounted = False

    async def evaluate_decision(
        self, project_id: int, decision_point: str, context: dict[str, Any] | None = None
    ) -> PolicyDecision:
        return PolicyDecision(
            policy_action="auto_execute",
            risk_score=None,
            confidence_score=None,
            reversibility_score=None,
            chosen="auto_execute",
            rationale=STUB_REASON,
            policy_version=POLICY_STUB_VERSION,
            guardrail_checks={
                "safety_ok": None,
                "time_ok": None,
                "cost_ok": None,
                "policy_mounted": False,
                "note": "WP10 风险策略/护栏未挂载：本次未做任何风险判定，三分不可用",
                "reason": STUB_REASON,
            },
            options_considered=["auto_execute"],
            degraded=True,
            reason=STUB_REASON,
            raw={"decision_point": decision_point, "context": context or {}},
        )


def _resolve_policy() -> tuple[Any, bool, str]:
    """懒加载 WP10 的策略实现；返回 ``(policy, mounted, source)``。"""
    import importlib

    for module_path in (
        "services.pipeline.decision_engine",
        "services.pipeline.risk_policy",
    ):
        try:
            module = importlib.import_module(module_path)
        except ModuleNotFoundError:
            continue
        getter = getattr(module, "get_policy", None)
        if callable(getter):
            try:
                policy = getter()
            except Exception:  # noqa: BLE001 - 策略初始化失败即视为未挂载
                logger.warning("策略 get_policy() 调用失败 module=%s", module_path, exc_info=True)
                continue
            if policy is not None:
                return policy, True, f"{module_path}.get_policy"
        for attr in ("POLICY", "policy", "default_policy"):
            policy = getattr(module, attr, None)
            if policy is not None and hasattr(policy, "evaluate_decision"):
                return policy, True, f"{module_path}.{attr}"
        fn = getattr(module, "evaluate_decision", None)
        if callable(fn):
            return fn, True, f"{module_path}.evaluate_decision"
    return PolicyStub(), False, "policy_stub"


def _normalize_policy_result(result: Any, decision_point: str) -> PolicyDecision:
    """把 WP10 的任意返回形态归一为 :class:`PolicyDecision`。"""
    if result is None:
        return PolicyDecision(
            policy_action="auto_execute",
            rationale=STUB_REASON,
            policy_version=POLICY_STUB_VERSION,
            guardrail_checks={"policy_mounted": False, "reason": STUB_REASON},
            degraded=True,
            reason=STUB_REASON,
        )
    if isinstance(result, PolicyDecision):
        return result
    if hasattr(result, "to_dict") and not isinstance(result, dict):
        try:
            result = result.to_dict()
        except Exception:  # noqa: BLE001
            result = {
                key: getattr(result, key)
                for key in (
                    "policy_action",
                    "action",
                    "risk_score",
                    "confidence_score",
                    "reversibility_score",
                    "rationale",
                    "policy_version",
                    "guardrail_checks",
                )
                if hasattr(result, key)
            }
    if not isinstance(result, dict):
        result = {
            "policy_action": str(getattr(result, "policy_action", "") or getattr(result, "action", "")),
            "risk_score": getattr(result, "risk_score", None),
            "confidence_score": getattr(result, "confidence_score", None),
            "reversibility_score": getattr(result, "reversibility_score", None),
            "rationale": getattr(result, "rationale", ""),
            "policy_version": getattr(result, "policy_version", ""),
        }

    action = str(result.get("policy_action") or result.get("action") or "auto_execute").strip()
    if action not in {"auto_execute", "need_human", "circuit_break"}:
        logger.warning("策略返回未知动作 %r，按 need_human 处理（不猜测）decision_point=%s", action, decision_point)
        action = "need_human"
    return PolicyDecision(
        policy_action=action,
        risk_score=_as_float(result.get("risk_score")),
        confidence_score=_as_float(result.get("confidence_score")),
        reversibility_score=_as_float(result.get("reversibility_score")),
        chosen=result.get("chosen"),
        rationale=str(result.get("rationale") or result.get("reason") or "").strip(),
        policy_version=str(result.get("policy_version") or "unknown"),
        guardrail_checks=dict(result.get("guardrail_checks") or {}),
        options_considered=list(result.get("options_considered") or []),
        decision_log_id=_as_int(result.get("decision_log_id") or result.get("decision_id")),
        degraded=bool(result.get("degraded", False)),
        reason=result.get("reason"),
        raw=dict(result),
    )


def _as_float(value: Any) -> float | None:
    if value is None or isinstance(value, bool):
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _as_int(value: Any) -> int | None:
    if value is None or isinstance(value, bool):
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


# --------------------------------------------------------------------------- #
# 引擎
# --------------------------------------------------------------------------- #
@dataclass
class EngineOutcome:
    """一次 ``_run_project`` 的结束原因（供 API 与日志使用）。"""

    reason: str
    detail: str = ""
    stage: str | None = None
    #: 未达停止条件时，下一轮的 ``pipeline_runs.id``（``reason='continue'`` 时有值）
    next_run_id: int | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "reason": self.reason,
            "detail": self.detail,
            "stage": self.stage,
            "next_run_id": self.next_run_id,
        }


class PipelineEngine:
    """进程内 asyncio 调度的流水线引擎（状态全部落库，可断点续跑）。"""

    def __init__(self, *, session_factory: Any = None, policy: Any = None) -> None:
        self._session_factory = session_factory
        self._policy = policy
        self._tasks: dict[int, asyncio.Task[Any]] = {}
        self._locks: dict[int, asyncio.Lock] = {}

    # ---------------- 基础设施 ----------------
    @property
    def session_factory(self) -> Any:
        if self._session_factory is not None:
            return self._session_factory
        from db.session import AsyncSessionLocal

        if AsyncSessionLocal is None:  # pragma: no cover - 数据库不可用
            raise PipelineError("db_unavailable", "数据库会话工厂不可用（AsyncSessionLocal=None）")
        return AsyncSessionLocal

    def policy(self) -> tuple[Any, bool, str]:
        if self._policy is None:
            self._policy, mounted, source = _resolve_policy()
            if not mounted:
                logger.warning(
                    "WP10 风险策略未挂载，使用 PolicyStub（action=auto_execute, reason=%s）；"
                    "所有决策仍写入 decision_logs 以便审计",
                    STUB_REASON,
                )
            else:
                logger.info("已加载 WP10 风险策略 source=%s", source)
            self._policy_source = source
            self._policy_mounted = mounted
        return self._policy, getattr(self, "_policy_mounted", False), getattr(self, "_policy_source", "?")

    def is_running(self, project_id: int) -> bool:
        task = self._tasks.get(project_id)
        return task is not None and not task.done()

    def _lock(self, project_id: int) -> asyncio.Lock:
        if project_id not in self._locks:
            self._locks[project_id] = asyncio.Lock()
        return self._locks[project_id]

    # ---------------- 对外控制面 ----------------
    async def start(
        self,
        project_id: int,
        *,
        mode: str | None = None,
        resume: bool = False,
        iteration: int | None = None,
    ) -> dict[str, Any]:
        """启动（或断点续跑）流水线；返回 run 与项目快照。"""
        async with self._lock(project_id), self.session_factory() as session:
            project = (
                await session.execute(select(Project).where(Project.id == project_id))
            ).scalar_one_or_none()
            if project is None:
                raise PipelineError("project_not_found", f"project {project_id} 不存在", {"project_id": project_id})

            if mode is not None:
                normalized = _normalize_mode(mode)
                if project.mode != normalized:
                    project.mode = normalized
                    structured_log(
                        "pipeline_mode_changed",
                        project_id=project_id,
                        mode=normalized,
                        reason="start(mode=...)",
                    )

            if project.status == "DRAFT":
                raise PipelineError(
                    "taskbook_not_locked",
                    "项目处于 DRAFT：请先锁定任务书（N1 前置门禁，DRAFT → TASKBOOK_LOCKED）",
                    {"project_status": project.status},
                )
            if project.status == "ABORTED":
                raise PipelineError("project_aborted", "项目已 ABORTED（终态），无法启动", None)
            if project.status == "DONE":
                raise PipelineError("project_done", "项目已完成（DONE 终态），如需再跑请新建项目", None)

            if resume and project.status in ACTIVE_STATUSES:
                transition(project, "WAIT_HUMAN", reason="resume：先收敛到待人工态再续跑")
            if project.status in {"WAIT_HUMAN"}:
                transition(project, "RUNNING", reason="断点续跑（跳过 done 环节）" if resume else "启动流水线")
            elif project.status == "TASKBOOK_LOCKED":
                transition(project, "RUNNING", reason="启动流水线")
            elif project.status == "CIRCUIT_BREAK":
                transition(project, "WAIT_HUMAN", reason="熔断后用户选择继续（先转 WAIT_HUMAN）")
                transition(project, "RUNNING", reason="用户选择继续执行")
            elif project.status == "REVIEWING":
                transition(project, "RUNNING", reason="评审完成未达停止条件，轮次+1 续跑")
            elif project.status in ACTIVE_STATUSES:
                pass  # 已在运行态（RUNNING / RISK_EVALUATING），无需迁移

            run = await resume_mod.active_run(session, project_id)
            if run is None or run.status in {"done", "failed", "circuit_break"} or iteration is not None:
                run = await self._create_run(session, project, iteration=iteration)
            else:
                run.status = "running"
                run.finished_at = None
                if mode is not None:
                    run.mode = project.mode

            await session.commit()
            run_payload = _run_to_dict(run)
            project_payload = _project_to_dict(project)

        self._spawn(project_id, run_payload["id"])
        return {
            "project": project_payload,
            "run": run_payload,
            "mode": run_payload["mode"],
            "status": run_payload["status"],
            "resume": bool(resume),
            "task_id": f"pipeline:{project_id}:{run_payload['id']}",
        }

    async def pause(self, project_id: int) -> dict[str, Any]:
        async with self.session_factory() as session:
            run = await self._require_run(session, project_id)
            if run.status != "running":
                raise PipelineError(
                    "run_not_running",
                    f"当前 run 状态为 {run.status}，无法暂停（仅在 running 时可暂停）",
                    {"run_status": run.status},
                )
            run.status = "paused"
            structured_log("pipeline_paused", project_id=project_id, run_id=int(run.id))
            await session.commit()
            payload = _run_to_dict(run)
        return {
            "run": payload,
            "status": "paused",
            "note": "暂停在环节边界生效（当前环节执行完后停止，状态已落库；不中断进行中的 LLM 调用）",
            "in_process_task": self.is_running(project_id),
        }

    async def resume(self, project_id: int) -> dict[str, Any]:
        """断点续跑：从第一个 ``status != done`` 的环节继续。"""
        async with self.session_factory() as session:
            run = await resume_mod.active_run(session, project_id)
            if run is None:
                run = await resume_mod.latest_run(session, project_id)
            if run is None:
                raise PipelineError("run_not_found", "该项目尚无流水线运行记录，请先 run", None)
            reset = await resume_mod.reset_stale_running(session, int(run.id))
            plan = await resume_mod.build_resume_plan(session, int(run.id))
            await session.commit()
        started = await self.start(project_id, resume=True)
        started["reset_stale_running"] = reset
        started["resume_plan"] = [item.to_dict() for item in plan]
        started["next_stage"] = next((item.stage for item in plan if item.needs_execution), None)
        return started

    async def stop(self, project_id: int, *, reason: str = "用户中止流水线") -> dict[str, Any]:
        async with self.session_factory() as session:
            project = (
                await session.execute(select(Project).where(Project.id == project_id))
            ).scalar_one_or_none()
            if project is None:
                raise PipelineError("project_not_found", f"project {project_id} 不存在", None)
            run = await resume_mod.active_run(session, project_id)
            reset: list[dict[str, Any]] = []
            if run is not None:
                run.status = "failed"
                run.stop_reason = "manual"
                run.finished_at = datetime.now(UTC)
                reset = await resume_mod.reset_stale_running(session, int(run.id))
            if project.status not in {"DONE", "ABORTED"}:
                transition(project, "ABORTED", reason=reason)
            await session.commit()
            payload = {"run": _run_to_dict(run) if run else None, "project_status": project.status}
        await self._cancel(project_id)
        payload["reset_stale_running"] = reset
        payload["stop_reason"] = "manual"
        return payload

    async def switch_mode(self, project_id: int, mode: str) -> dict[str, Any]:
        normalized = _normalize_mode(mode)
        async with self.session_factory() as session:
            project = (
                await session.execute(select(Project).where(Project.id == project_id))
            ).scalar_one_or_none()
            if project is None:
                raise PipelineError("project_not_found", f"project {project_id} 不存在", None)
            previous = project.mode
            project.mode = normalized
            run = await resume_mod.active_run(session, project_id)
            if run is not None:
                run.mode = normalized
            structured_log(
                "pipeline_mode_switched",
                project_id=project_id,
                previous_mode=previous,
                mode=normalized,
                run_id=int(run.id) if run is not None else None,
            )
            await session.commit()
        return {
            "project_id": project_id,
            "previous_mode": previous,
            "mode": normalized,
            "note": "模式切换不清空已有产出，也不允许绕过安全护栏（在下一个环节边界生效）",
        }

    async def intervene(
        self,
        project_id: int,
        *,
        node: str,
        action: str,
        payload: dict[str, Any] | None = None,
        note: str | None = None,
    ) -> dict[str, Any]:
        """人工介入（N1–N4）：写 ``interventions`` 并施加动作。"""
        if node not in {"N1", "N2", "N3", "N4"}:
            raise PipelineError("invalid_node", f"未知介入节点 {node!r}（合法 N1–N4）", {"node": node})
        if action not in {"approve", "modify", "reject", "rerun", "downgrade", "abort", "switch_mode"}:
            raise PipelineError(
                "invalid_action",
                f"未知介入动作 {action!r}（合法 approve/modify/reject/rerun/downgrade/abort/switch_mode）",
                {"action": action},
            )

        async with self.session_factory() as session:
            project = (
                await session.execute(select(Project).where(Project.id == project_id))
            ).scalar_one_or_none()
            if project is None:
                raise PipelineError("project_not_found", f"project {project_id} 不存在", None)
            run = await resume_mod.active_run(session, project_id) or await resume_mod.latest_run(
                session, project_id
            )

            # P0-1 幂等护栏：终态项目不再接受人工处置；同一 decision 不重复落库
            decision_log_id = _as_int((payload or {}).get("decision_log_id"))
            if str(project.status) in TERMINAL_STATUSES:
                raise PipelineError(
                    "decision_already_resolved",
                    f"项目 {project_id} 已处于终态 {project.status}：人工处置已生效，"
                    "重复处置不再落库（幂等拦截）",
                    {
                        "project_id": project_id,
                        "project_status": str(project.status),
                        "decision_log_id": decision_log_id,
                        "terminal": True,
                    },
                )
            if decision_log_id is not None:
                existing_id = (
                    await session.execute(
                        select(Intervention.id)
                        .where(
                            Intervention.project_id == project_id,
                            Intervention.payload["decision_log_id"].as_string()
                            == str(decision_log_id),
                        )
                        .limit(1)
                    )
                ).scalar_one_or_none()
                if existing_id is not None:
                    raise PipelineError(
                        "decision_already_resolved",
                        f"decision_log {decision_log_id} 已在项目 {project_id} 上处置"
                        f"（intervention {int(existing_id)}），重复提交被幂等拦截",
                        {
                            "project_id": project_id,
                            "project_status": str(project.status),
                            "decision_log_id": decision_log_id,
                            "existing_intervention_id": int(existing_id),
                        },
                    )

            record = Intervention(
                project_id=project_id,
                pipeline_run_id=int(run.id) if run is not None else None,
                node=node,
                action=action,
                payload=payload or {},
                note=note,
            )
            session.add(record)

            effect: dict[str, Any] = {"node": node, "action": action}
            if action in {"reject", "abort"}:
                if project.status not in {"DONE", "ABORTED"}:
                    transition(project, "ABORTED", reason=f"人工介入 {node}={action}", actor="human")
                if run is not None:
                    run.status = "failed"
                    run.stop_reason = "manual"
                    run.finished_at = datetime.now(UTC)
                effect["project_status"] = project.status
            elif action == "switch_mode":
                mode = _normalize_mode(str((payload or {}).get("mode") or "manual"))
                project.mode = mode
                if run is not None:
                    run.mode = mode
                if project.status == "WAIT_HUMAN":
                    transition(project, "RUNNING", reason=f"人工介入 {node}=switch_mode", actor="human")
                effect.update({"mode": mode, "project_status": project.status})
            else:  # approve / modify / rerun / downgrade → 记录并转 RUNNING（由调用方 resume）
                if project.status == "WAIT_HUMAN":
                    transition(project, "RUNNING", reason=f"人工介入 {node}={action}", actor="human")
                elif project.status == "CIRCUIT_BREAK":
                    transition(project, "WAIT_HUMAN", reason=f"人工介入 {node}={action}", actor="human")
                    transition(project, "RUNNING", reason=f"人工介入 {node}={action}", actor="human")
                elif project.status not in ACTIVE_STATUSES:
                    raise IllegalTransition(
                        project.status, "RUNNING", project_id=project_id, reason=f"介入 {node}={action}"
                    )
                if run is not None and project.status == "RUNNING":
                    run.status = "running"
                    run.finished_at = None
                effect["project_status"] = project.status
                effect["pending_degrade"] = payload if action == "downgrade" else None
                effect["hints"] = payload if action == "modify" else None
                effect["rerun_stage"] = bool(action == "rerun")

            if action == "rerun" and run is not None:
                # 把最新 attempt 标为 failed，使 resume 计划以 attempt+1 重跑该环节
                latest = (
                    await session.execute(
                        select(StageOutput)
                        .where(
                            StageOutput.pipeline_run_id == int(run.id),
                            StageOutput.status.in_(("done", "failed", "waiting_human")),
                        )
                        .order_by(StageOutput.updated_at.desc())
                        .limit(1)
                    )
                ).scalar_one_or_none()
                if latest is not None:
                    effect["rerun_stage"] = str(latest.stage)
                    await resume_mod.upsert_stage_output(
                        session,
                        pipeline_run_id=int(run.id),
                        stage=str(latest.stage),
                        attempt=int(latest.attempt or 1),
                        status="failed",
                        error=f"人工介入 {node}=rerun：该环节将被重跑（attempt+1）",
                    )

            if project.status == "RUNNING" and run is not None:
                await session.flush()
            await session.commit()
            intervention_id = int(record.id)
            project_status = project.status
            run_id = int(run.id) if run is not None else None

        sse_mod.publish(
            "decision",
            {
                "decision_point": {"N1": None, "N2": "D2", "N3": "D4", "N4": "D6"}.get(node),
                "chosen": f"human:{action}",
                "rationale": f"人工介入 {node}={action}：{note or '无备注'}",
            },
            project_id=project_id,
        )
        should_resume = effect.get("project_status") == "RUNNING"
        return {
            "intervention_id": intervention_id,
            "project_id": project_id,
            "pipeline_run_id": run_id,
            "project_status": project_status,
            "effect": effect,
            "ready_to_resume": should_resume,
            "note": "如需立即续跑，请调用 POST /pipelines/{pid}/resume（断点续跑，跳过 done 环节）",
        }

    # ---------------- 状态查询 ----------------
    async def snapshot(self, project_id: int, *, include_stages: bool = False) -> dict[str, Any]:
        async with self.session_factory() as session:
            project = (
                await session.execute(select(Project).where(Project.id == project_id))
            ).scalar_one_or_none()
            if project is None:
                raise PipelineError("project_not_found", f"project {project_id} 不存在", None)
            run = await resume_mod.active_run(session, project_id) or await resume_mod.latest_run(
                session, project_id
            )
            stages: list[dict[str, Any]] = []
            plan: list[dict[str, Any]] = []
            run_cost = 0.0
            if run is not None:
                stages = await resume_mod.stage_summary(session, int(run.id))
                plan = [item.to_dict() for item in await resume_mod.build_resume_plan(session, int(run.id))]
                run_cost = await resume_mod.run_cost_total(session, int(run.id))
            taskbook = None
            if project.taskbook_id:
                taskbook = (
                    await session.execute(select(Taskbook).where(Taskbook.id == int(project.taskbook_id)))
                ).scalar_one_or_none()

        thresholds = resolve_thresholds(project=project, taskbook=taskbook)
        cost = await _cost_snapshot(project_id)
        _, mounted, source = self.policy()
        pending_decision = await self._pending_decision(project_id)
        payload: dict[str, Any] = {
            "project_id": project_id,
            "project_name": project.name,
            "project_status": str(project.status),
            "mode": str(project.mode),
            "current_iteration": int(project.current_iteration or 0),
            "is_demo": bool(project.is_demo),
            "taskbook_id": int(project.taskbook_id) if project.taskbook_id else None,
            "run": _run_to_dict(run) if run is not None else None,
            "stop_reason": run.stop_reason if run is not None else None,
            "run_cost_usd": run_cost,
            "current_stage": _current_stage(stages) if run is not None else None,
            "stages_progress": [
                {"stage": item["stage"], "status": item["status"], "attempt": item["attempt"]}
                for item in stages
            ],
            "resume_plan": plan,
            "next_stage": next((item["stage"] for item in plan if not item["reusable"]), None),
            "stop_conditions": thresholds.to_dict(),
            "cost": cost,
            "risk_policy": {
                "mounted": mounted,
                "source": source,
                "policy_version": getattr(self._policy, "version", None),
                "degrade_reason": None if mounted else STUB_REASON,
                "note": None
                if mounted
                else "WP10 风险策略未挂载：决策以自动执行占位并透明记账，不作风险判断",
            },
            "stage_registry": _registry_snapshot(),
            "in_process_task": self.is_running(project_id),
            "pending_decision": pending_decision,
            "updated_at": datetime.now(UTC).isoformat(),
        }
        if include_stages:
            payload["stages"] = stages
        return payload

    async def _pending_decision(self, project_id: int) -> dict[str, Any] | None:
        """WAIT_HUMAN 时给出待人工处理的决策（来自最近的等待态环节）。"""
        async with self.session_factory() as session:
            run = await resume_mod.active_run(session, project_id)
            if run is None:
                return None
            rows = list(
                (
                    await session.execute(
                        select(StageOutput)
                        .where(
                            StageOutput.pipeline_run_id == int(run.id),
                            StageOutput.status == "waiting_human",
                        )
                        .order_by(StageOutput.updated_at.desc())
                        .limit(1)
                    )
                )
                .scalars()
                .all()
            )
        if not rows:
            return None
        row = rows[0]
        payload = row.output_json if isinstance(row.output_json, dict) else {}
        failure = payload.get("_failure") if isinstance(payload, dict) else None
        return {
            "stage": str(row.stage),
            "attempt": int(row.attempt or 1),
            "decision_point": (failure or {}).get("decision_point") or STAGE_DECISION_POINT.get(str(row.stage)),
            "failure": failure,
            "error": row.error,
            "options": ["approve", "modify", "reject", "rerun", "downgrade", "abort"],
        }

    # ---------------- 内部：run/任务管理 ----------------
    def lock_project(self, project_id: int) -> _SessionLock:
        return _SessionLock(self, project_id)

    async def _require_run(self, session: AsyncSession, project_id: int) -> PipelineRun:
        run = await resume_mod.active_run(session, project_id) or await resume_mod.latest_run(
            session, project_id
        )
        if run is None:
            raise PipelineError("run_not_found", "该项目尚无流水线运行记录，请先 run", None)
        return run

    async def _create_run(
        self, session: AsyncSession, project: Project, *, iteration: int | None = None
    ) -> PipelineRun:
        next_iteration = int(iteration) if iteration is not None else int(project.current_iteration or 0) + 1
        run = PipelineRun(
            project_id=int(project.id),
            iteration=next_iteration,
            mode=str(project.mode),
            status="running",
            started_at=datetime.now(UTC),
        )
        session.add(run)
        project.current_iteration = next_iteration
        await session.flush()
        structured_log(
            "pipeline_run_created",
            project_id=int(project.id),
            run_id=int(run.id),
            iteration=next_iteration,
            mode=str(project.mode),
        )
        return run

    def _spawn(self, project_id: int, run_id: int) -> None:
        task = self._tasks.get(project_id)
        if task is not None and not task.done():
            logger.info("项目已有运行中的任务，跳过重复启动 project_id=%s", project_id)
            return
        self._tasks[project_id] = asyncio.create_task(
            self._run_project(project_id, run_id), name=f"pipeline:{project_id}:{run_id}"
        )

    async def _cancel(self, project_id: int) -> None:
        task = self._tasks.get(project_id)
        if task is not None and not task.done():
            task.cancel()
            # 取消失败不应阻断清理：超时/取消/任务自身异常一律吞掉（与原 except-pass 等价）
            with contextlib.suppress(TimeoutError, asyncio.CancelledError, Exception):
                await asyncio.wait_for(asyncio.shield(task), timeout=5)
        self._tasks.pop(project_id, None)

    async def _drain(self, project_id: int) -> None:
        task = self._tasks.get(project_id)
        if task is None:
            return
        deadline = time.monotonic() + 60
        while not task.done() and time.monotonic() < deadline:
            await asyncio.sleep(0.2)
        if task.done():
            self._tasks.pop(project_id, None)

    async def wait_for_idle(self, project_id: int, timeout: float = 120.0) -> str:
        """等待后台任务结束（测试/验收用），返回结束原因。"""
        task = self._tasks.get(project_id)
        if task is None:
            return "no_task"
        try:
            await asyncio.wait_for(asyncio.shield(task), timeout=timeout)
        except TimeoutError:
            return "timeout"
        except asyncio.CancelledError:
            return "cancelled"
        except Exception as exc:  # noqa: BLE001
            return f"error:{exc}"
        return "finished"

    # ---------------- 主循环 ----------------
    async def _run_project(self, project_id: int, run_id: int) -> None:
        outcome = EngineOutcome(reason="unknown")
        try:
            outcome = await self._loop(project_id, run_id)
        except asyncio.CancelledError:
            structured_log("pipeline_task_cancelled", project_id=project_id, run_id=run_id)
            raise
        except Exception as exc:  # noqa: BLE001 - 兜底：任何异常都必须落库，不能静默
            logger.exception("流水线主循环异常 project_id=%s run_id=%s", project_id, run_id)
            outcome = EngineOutcome(reason="internal_error", detail=f"{type(exc).__name__}: {exc}")
            await self._safe_finalize_error(project_id, run_id, exc)
        finally:
            self._tasks.pop(project_id, None)
            structured_log(
                "pipeline_loop_finished",
                project_id=project_id,
                run_id=run_id,
                reason=outcome.reason,
                stage=outcome.stage,
                detail=outcome.detail,
            )

    async def _loop(self, project_id: int, run_id: int) -> EngineOutcome:
        while True:
            control = await self._check_control(project_id, run_id)
            if control is not None:
                return control

            async with self.session_factory() as session:
                plan = await resume_mod.build_resume_plan(session, run_id)
                run = (
                    await session.execute(select(PipelineRun).where(PipelineRun.id == run_id))
                ).scalar_one_or_none()
                if run is None:
                    return EngineOutcome(reason="run_missing", detail=f"run {run_id} 不存在")

            item = next((entry for entry in plan if entry.needs_execution), None)
            if item is None:
                outcome = await self._finalize_iteration(project_id, run_id, plan)
                if outcome.reason == "continue" and outcome.next_run_id:
                    structured_log(
                        "pipeline_next_iteration",
                        project_id=project_id,
                        previous_run_id=run_id,
                        run_id=outcome.next_run_id,
                    )
                    run_id = int(outcome.next_run_id)
                    continue
                return outcome

            structured_log(
                "pipeline_stage_start",
                project_id=project_id,
                run_id=run_id,
                stage=item.stage,
                attempt=item.attempt,
                iteration=int(run.iteration) if run is not None else None,
            )
            result = await self._execute_stage(project_id, run_id, item)
            if result.reason in {"wait_human", "circuit_break", "paused", "stop", "run_missing"}:
                return result
            # reason == "continue" ⇒ 进入下一环节（或同环节下一个 attempt）

    async def _check_control(self, project_id: int, run_id: int) -> EngineOutcome | None:
        """环节边界检查控制信号（pause/stop/等人工）。"""
        async with self.session_factory() as session:
            project = (
                await session.execute(select(Project).where(Project.id == project_id))
            ).scalar_one_or_none()
            if project is None:
                return EngineOutcome(reason="project_missing", detail=f"project {project_id} 不存在")
            run = (
                await session.execute(select(PipelineRun).where(PipelineRun.id == run_id))
            ).scalar_one_or_none()
        if run is None:
            return EngineOutcome(reason="run_missing", detail=f"run {run_id} 不存在")
        if str(project.status) == "ABORTED":
            return EngineOutcome(reason="stop", detail="项目已 ABORTED")
        if str(project.status) == "WAIT_HUMAN":
            return EngineOutcome(reason="wait_human", detail="项目处于 WAIT_HUMAN，等待人工介入")
        if str(project.status) == "CIRCUIT_BREAK":
            return EngineOutcome(reason="circuit_break", detail="项目处于 CIRCUIT_BREAK，等待人工")
        if str(run.status) == "paused":
            return EngineOutcome(reason="paused", detail="运行已暂停（环节边界），状态已落库")
        if str(run.status) in {"failed", "circuit_break", "done"}:
            return EngineOutcome(reason=str(run.status), detail=f"run 状态为 {run.status}")
        if str(project.status) not in ACTIVE_STATUSES:
            return EngineOutcome(reason="not_active", detail=f"项目状态 {project.status} 非运行态")
        return None

    # ---------------- 单个环节执行 ----------------
    async def _execute_stage(
        self, project_id: int, run_id: int, item: resume_mod.StagePlanItem
    ) -> EngineOutcome:
        stage = item.stage
        attempt = item.attempt

        # 1) 环节实现是否就绪（缺实现必须显式报错，禁止静默跳过）
        try:
            handler = get_stage(stage)
        except StageError as exc:
            return await self._handle_failure(
                project_id, run_id, stage, attempt, exc, started=time.perf_counter(), degrade={}
            )

        # 2) 清理残留 running（保证同一 run 至多一条 running）
        async with self.session_factory() as session:
            await resume_mod.reset_stale_running(session, run_id, keep=(stage, attempt))
            await resume_mod.upsert_stage_output(
                session,
                pipeline_run_id=run_id,
                stage=stage,
                attempt=attempt,
                status="running",
                started_at=datetime.now(UTC),
                error=None,
            )
            await session.commit()

        started = time.perf_counter()
        ctx = await self._build_context(project_id, run_id, stage, attempt)
        sse_mod.publish(
            "stage_progress",
            {
                "stage": stage,
                "percent": 0,
                "message": f"环节 {stage} 开始（attempt={attempt}, mode={ctx.mode}）",
            },
            project_id=project_id,
        )

        # 3) 风险策略评估（每环节执行前；plan 无决策点则不评估，见 def_lineage）
        policy_decision: PolicyDecision | None = None
        decision_log_id: int | None = None
        if ctx.decision_point is None:
            structured_log(
                "risk_policy_skipped",
                project_id=project_id,
                run_id=run_id,
                stage=stage,
                reason="该环节无决策点（contracts.decision_points：plan 不触发 D 决策，选型在 D2）",
            )
        else:
            policy_decision, decision_log_id, gate = await self._evaluate_policy(
                project_id, run_id, stage, attempt, ctx
            )
            if gate is not None:
                return gate

        # 4) 执行环节（环节使用自己的 session，避免长事务与跨环节复用）
        stage_session = self.session_factory()
        ctx.session = stage_session
        try:
            outcome, result, exc = await self._invoke_handler(ctx, handler)
        finally:
            await stage_session.close()
        duration_ms = int((time.perf_counter() - started) * 1000)

        if exc is not None:
            return await self._handle_failure(
                project_id,
                run_id,
                stage,
                attempt,
                exc,
                started=started,
                degrade=ctx.degrade,
                duration_ms=duration_ms,
            )

        assert result is not None
        quality_failed = result.quality_ok is False or bool(result.revert_to)
        if quality_failed:
            reason = "; ".join(result.quality_issues) or (
                f"环节判定需回退 {result.revert_to}" if result.revert_to else "环节产出未达质量门槛"
            )
            return await self._handle_failure(
                project_id,
                run_id,
                stage,
                attempt,
                None,
                level="L2",
                reason=reason,
                revert_to_hint=result.revert_to,
                started=started,
                degrade=ctx.degrade,
                duration_ms=duration_ms,
                partial_result=result,
            )

        # 5) 落库 done（幂等）
        payload = dict(result.output or {})
        if result.degradations:
            payload.setdefault("_degradations", result.degradations)
        if ctx.degrade:
            payload.setdefault("_degrade_applied", ctx.degrade)
        if decision_log_id is not None:
            payload.setdefault("_decision_log_id", decision_log_id)
        async with self.session_factory() as session:
            await resume_mod.upsert_stage_output(
                session,
                pipeline_run_id=run_id,
                stage=stage,
                attempt=attempt,
                status="done",
                output_json=payload,
                output_text=result.text,
                cost_usd=round(float(result.cost_usd or 0), 6),
                duration_ms=duration_ms,
                error=None,
                finished_at=datetime.now(UTC),
            )
            await session.commit()

        metric_payload = dict(result.metrics or {})
        _ = metric_payload  # 指标已并入 stage_done 事件；此处保留以便后续扩展
        sse_mod.publish(
            "stage_done",
            {**result.stage_done_payload(stage), "attempt": attempt},
            project_id=project_id,
        )
        if result.decision or (policy_decision is not None and policy_decision.chosen):
            sse_mod.publish(
                "decision",
                {
                    "decision_point": (result.decision or {}).get("decision_point") or ctx.decision_point,
                    "chosen": (result.decision or {}).get("chosen") or policy_decision.chosen,
                    "rationale": (result.decision or {}).get("rationale") or policy_decision.rationale,
                },
                project_id=project_id,
            )
        await self._emit_guardrail(project_id, stage)
        structured_log(
            "pipeline_stage_done",
            project_id=project_id,
            run_id=run_id,
            stage=stage,
            attempt=attempt,
            cost=round(float(result.cost_usd or 0), 6),
            duration_ms=duration_ms,
        )

        # 6) 人工优先模式：到介入节点暂停（N2/N3/N4）
        #    注意：本环节已 done，**不得**把它改成 waiting_human（否则断点续跑会重跑）；
        #    等待标记写在「下一个待执行环节」上，人工 approve 后从该环节继续。
        node = STAGE_INTERVENTION_NODE.get(stage)
        if ctx.mode == "manual" and node is not None:
            next_item = await self._next_pending_after(run_id, stage)
            return await self._wait_for_human(
                project_id,
                run_id,
                stage,
                attempt,
                node=node,
                decision_point=STAGE_DECISION_POINT.get(stage),
                payload={
                    "stage": stage,
                    "attempt": attempt,
                    "mode": "manual",
                    "reason": f"manual 模式：{stage} 产出后按 §2.4.3 停留在 {node} 等待人工确认",
                    "options": ["approve", "modify", "reject", "rerun", "downgrade", "abort"],
                },
                note=f"manual 模式介入节点 {node}（环节已 done，等待点在下一环节前）",
                mark=next_item,
            )
        return EngineOutcome(reason="continue", stage=stage)

    async def _next_pending_after(self, run_id: int, stage: str) -> tuple[str, int] | None:
        """当前环节之后第一个待执行环节 ``(stage, attempt)``。"""
        async with self.session_factory() as session:
            plan = await resume_mod.build_resume_plan(session, run_id)
        index = STAGE_ORDER.index(stage) if stage in STAGE_ORDER else -1
        for item in plan[index + 1 :]:
            if item.needs_execution:
                return (item.stage, item.attempt)
        return None

    async def _invoke_handler(
        self, ctx: StageContext, handler: Any
    ) -> tuple[str, StageResult | None, BaseException | None]:
        async def emit(event: str, payload: dict[str, Any]) -> None:
            await sse_mod.publish_event(ctx.project_id, event, payload)

        ctx.emit = emit
        try:
            result = await asyncio.wait_for(handler.run(ctx), timeout=float(ctx.timeout_seconds))
        except TimeoutError:
            return "error", None, StageTimeoutError(
                f"环节 {ctx.stage} 执行超过 {ctx.timeout_seconds}s（时长护栏由 WP10 判定，本处按超时中断）",
                stage=ctx.stage,
                detail={"timeout_seconds": ctx.timeout_seconds},
            )
        except BaseException as exc:  # noqa: BLE001 - 交给失败分级
            if isinstance(exc, (KeyboardInterrupt, SystemExit)):
                raise
            return "error", None, exc
        if not isinstance(result, StageResult):
            return (
                "error",
                None,
                StageError(
                    f"环节 {ctx.stage} 未返回 StageResult（收到 {type(result).__name__}）",
                    level="L1",
                    stage=ctx.stage,
                ),
            )
        return "ok", result, None

    async def _build_context(
        self, project_id: int, run_id: int, stage: str, attempt: int
    ) -> StageContext:
        async with self.session_factory() as session:
            project = (
                await session.execute(select(Project).where(Project.id == project_id))
            ).scalar_one()
            run = (
                await session.execute(select(PipelineRun).where(PipelineRun.id == run_id))
            ).scalar_one()
            taskbook = None
            if project.taskbook_id:
                taskbook = (
                    await session.execute(select(Taskbook).where(Taskbook.id == int(project.taskbook_id)))
                ).scalar_one_or_none()
            inputs = await resume_mod.completed_outputs(session, run_id)
            rows = await resume_mod.load_stage_rows(session, run_id)
            degrade, l2_used, revert_used = _extract_failure_hints(rows, stage)

        from core.config import get_settings

        settings = get_settings()
        hints: dict[str, Any] = {}
        if taskbook is not None:
            hints["taskbook_locked_at"] = (
                taskbook.locked_at.isoformat() if getattr(taskbook, "locked_at", None) else None
            )
        return StageContext(
            project_id=project_id,
            pipeline_run_id=run_id,
            stage=stage,
            iteration=int(run.iteration or 1),
            mode=str(project.mode),
            attempt=attempt,
            session=None,  # 环节自有 session（在 handler 内按需开），避免长事务
            taskbook=taskbook,
            project=project,
            inputs=inputs,
            degrade=degrade,
            hints=hints,
            emit=None,
            timeout_seconds=int(settings.pipeline_max_stage_minutes or 20) * 60,
            extras={
                "max_retry": int(settings.pipeline_max_retry or 0),
                "l2_used": l2_used,
                "revert_used": revert_used,
                "settings": settings,
                "policy_mounted": self.policy()[1],
            },
        )

    # ---------------- 风险策略 ----------------
    async def _evaluate_policy(
        self,
        project_id: int,
        run_id: int,
        stage: str,
        attempt: int,
        ctx: StageContext,
    ) -> tuple[PolicyDecision, int | None, EngineOutcome | None]:
        """环节执行前调用风险策略（WP10），必要时把决策写入 ``decision_logs``。"""
        decision_point = ctx.decision_point or "D1"
        policy, mounted, source = self.policy()
        context = {
            "stage": stage,
            "iteration": ctx.iteration,
            "attempt": attempt,
            "mode": ctx.mode,
            "pipeline_run_id": run_id,
            "decision_point": decision_point,
            "action_domain": _derive_action_domain(ctx, decision_point),
            "taskbook_id": getattr(ctx.taskbook, "id", None),
            "upstream_stages": sorted(ctx.inputs),
            "failure_hint": ctx.degrade or None,
        }
        # RUNNING → RISK_EVALUATING（状态机要求由策略驱动进入/退出）
        async with self.session_factory() as session:
            project = (
                await session.execute(select(Project).where(Project.id == project_id))
            ).scalar_one_or_none()
            if project is not None and str(project.status) == "RUNNING":
                transition(project, "RISK_EVALUATING", reason=f"{stage}/{decision_point} 进入风险评估")
                await session.commit()

        started = time.perf_counter()
        try:
            raw = await _call_policy(policy, project_id, decision_point, context)
        except Exception as exc:  # noqa: BLE001 - 策略故障不得让流水线失控：降级为 need_human
            logger.exception("风险策略调用失败，按 need_human 处理（不猜测风险）")
            decision = PolicyDecision(
                policy_action="need_human",
                rationale=f"风险策略调用异常（{type(exc).__name__}: {exc}），保守转人工",
                policy_version="policy_error",
                guardrail_checks={"policy_mounted": mounted, "error": str(exc)},
                options_considered=["need_human"],
                degraded=False,
                reason="policy_call_failed",
            )
        else:
            decision = _normalize_policy_result(raw, decision_point)
        duration_ms = int((time.perf_counter() - started) * 1000)

        # 退出 RISK_EVALUATING（auto_execute → RUNNING；其余由后续处理接管）
        async with self.session_factory() as session:
            project = (
                await session.execute(select(Project).where(Project.id == project_id))
            ).scalar_one_or_none()
            if project is not None and str(project.status) == "RISK_EVALUATING":
                if decision.policy_action == "auto_execute":
                    transition(project, "RUNNING", reason=f"{stage}/{decision_point} auto_execute")
            elif project is not None and str(project.status) == "RUNNING" and decision.policy_action != "auto_execute":
                transition(project, "RISK_EVALUATING", reason=f"{stage}/{decision_point} 策略非自动执行")

            decision_log_id = decision.decision_log_id
            if decision_log_id is None:
                decision_log_id = await self._write_decision_log(
                    session,
                    project_id=project_id,
                    run_id=run_id,
                    decision_point=decision_point,
                    stage=stage,
                    context_digest=(
                        f"环节={stage} 第{attempt}次尝试，iteration={ctx.iteration}，mode={ctx.mode}，"
                        f"上游已完成={sorted(ctx.inputs)}，"
                        f"{'降级提示=' + json.dumps(ctx.degrade, ensure_ascii=False) if ctx.degrade else '无降级提示'}"
                    ),
                    options=list(decision.options_considered or [decision.policy_action]),
                    chosen=decision.chosen or decision.policy_action,
                    decision=decision,
                    cost_usd=0.0,
                )
            await session.commit()

        sse_mod.publish_risk_policy(
            project_id,
            {
                "decision_log_id": decision_log_id,
                "risk_score": decision.risk_score,
                "confidence_score": decision.confidence_score,
                "reversibility_score": decision.reversibility_score,
                "policy_action": decision.policy_action,
                "policy_version": decision.policy_version,
                "degraded": decision.degraded,
                "decision_point": decision_point,
                "reason": decision.reason,
            },
        )
        structured_log(
            "risk_policy_evaluated",
            project_id=project_id,
            run_id=run_id,
            stage=stage,
            decision_point=decision_point,
            action=decision.policy_action,
            policy_version=decision.policy_version,
            policy_mounted=mounted,
            policy_source=source,
            reason=decision.reason,
            duration_ms=duration_ms,
            cost=0.0,
        )

        if decision.policy_action == "need_human":
            node = STAGE_INTERVENTION_NODE.get(stage)
            gate = await self._wait_for_human(
                project_id,
                run_id,
                stage,
                attempt,
                node=node,
                decision_point=decision_point,
                payload={
                    "stage": stage,
                    "decision_point": decision_point,
                    "risk": decision.risk_score,
                    "confidence": decision.confidence_score,
                    "reversibility": decision.reversibility_score,
                    "rationale": decision.rationale,
                    "reason": decision.reason,
                    "policy_version": decision.policy_version,
                    "decision_log_id": decision_log_id,
                    "options": ["approve", "modify", "reject", "rerun", "abort"],
                },
                note=f"风险策略 need_human（{decision.rationale or decision.reason}）",
                mark=(stage, attempt),
            )
            return decision, decision_log_id, gate

        if decision.policy_action == "circuit_break":
            gate = await self._circuit_break(
                project_id,
                run_id,
                stage,
                attempt,
                reason=f"风险策略熔断：{decision.rationale or decision.reason or 'circuit_break'}",
                decision_point=decision_point,
                decision=decision,
                decision_log_id=decision_log_id,
            )
            return decision, decision_log_id, gate

        return decision, decision_log_id, None

    async def _write_decision_log(
        self,
        session: AsyncSession,
        *,
        project_id: int,
        run_id: int,
        decision_point: str,
        stage: str,
        context_digest: str,
        options: list[str],
        chosen: str,
        decision: PolicyDecision,
        cost_usd: float,
    ) -> int:
        """写 ``decision_logs``（契约必填字段齐全）。

        ``risk/confidence/reversibility`` 不可用时（WP10 未挂载）**置 0 并在 rationale 与
        guardrail_checks 中显式标注不可用**——表列为 NOT NULL，禁止用 0 冒充真实风险分。
        """
        stub = decision.degraded or decision.policy_version == POLICY_STUB_VERSION
        rationale = decision.rationale or decision.reason or ""
        if stub and STUB_REASON not in rationale:
            rationale = f"{STUB_REASON}；{rationale}".strip("；")
        if stub:
            rationale = (
                f"{rationale}｜风险三分不可用（risk/confidence/reversibility 占位 0，"
                "非真实评分，WP10 挂载后自动替换）"
            )
        guardrail_checks = {
            "safety_ok": (decision.guardrail_checks or {}).get("safety_ok"),
            "time_ok": (decision.guardrail_checks or {}).get("time_ok"),
            "cost_ok": (decision.guardrail_checks or {}).get("cost_ok"),
        }
        guardrail_checks.update(
            {key: value for key, value in (decision.guardrail_checks or {}).items() if key not in guardrail_checks}
        )
        guardrail_checks.setdefault("policy_mounted", not stub)
        if stub:
            guardrail_checks["stub_reason"] = STUB_REASON

        record = DecisionLog(
            project_id=project_id,
            pipeline_run_id=run_id,
            decision_point=decision_point,
            stage=stage,
            context_digest=context_digest[:4000],
            options_considered=list(options or [chosen]),
            chosen=str(chosen)[:64],
            rationale=rationale[:4000],
            risk_score=_clamp(decision.risk_score if decision.risk_score is not None else 0.0, 0, 100, 2),
            confidence_score=_clamp(
                decision.confidence_score if decision.confidence_score is not None else 0.0, 0, 1, 3
            ),
            reversibility_score=_clamp(
                decision.reversibility_score if decision.reversibility_score is not None else 0.0, 0, 1, 3
            ),
            policy_action=decision.policy_action,
            policy_version=decision.policy_version[:32],
            guardrail_checks=guardrail_checks,
            cost_usd=round(float(cost_usd or 0), 4),
        )
        session.add(record)
        await session.flush()
        structured_log(
            "decision_log_written",
            project_id=project_id,
            run_id=run_id,
            stage=stage,
            decision_point=decision_point,
            chosen=chosen,
            policy_action=decision.policy_action,
            policy_version=decision.policy_version,
            decision_log_id=int(record.id),
            policy_mounted=not stub,
            cost=round(float(cost_usd or 0), 6),
        )
        return int(record.id)

    # ---------------- 失败处理（L1/L2/L3 + D4） ----------------
    async def _handle_failure(
        self,
        project_id: int,
        run_id: int,
        stage: str,
        attempt: int,
        exc: BaseException | None,
        *,
        level: str | None = None,
        reason: str | None = None,
        revert_to_hint: str | None = None,
        started: float | None = None,
        degrade: dict[str, Any] | None = None,
        duration_ms: int | None = None,
        partial_result: StageResult | None = None,
    ) -> EngineOutcome:
        ctx = await self._build_context(project_id, run_id, stage, attempt)
        if degrade:
            ctx.degrade = {**(ctx.degrade or {}), **degrade}
        assessment = assess_failure(exc, ctx=ctx, level=level, reason=reason)
        if revert_to_hint and assessment.action == "downgrade":
            assessment.revert_to = revert_to_hint
        if duration_ms is None and started is not None:
            duration_ms = int((time.perf_counter() - started) * 1000)

        cost = float(partial_result.cost_usd) if partial_result is not None else 0.0
        payload: dict[str, Any] = {}
        if partial_result is not None and partial_result.output:
            payload.update(partial_result.output)
        payload["_failure"] = assessment.to_dict()
        if assessment.revert_to:
            payload["revert_to"] = assessment.revert_to
            payload["revert_reason"] = assessment.reason

        async with self.session_factory() as session:
            project = (
                await session.execute(select(Project).where(Project.id == project_id))
            ).scalar_one_or_none()
            row_status = "failed"
            await resume_mod.upsert_stage_output(
                session,
                pipeline_run_id=run_id,
                stage=stage,
                attempt=attempt,
                status=row_status,
                output_json=payload,
                cost_usd=round(cost, 6),
                duration_ms=duration_ms,
                error=assessment.reason[:4000],
                finished_at=datetime.now(UTC),
            )
            # D4 决策：交风险策略裁决（WP10）；未挂载时桩返回 auto_execute，
            # 此时按 failure_handler 的确定性分级结论执行（重试/降级/熔断）
            d4 = await self._policy_for_failure(project_id, run_id, stage, attempt, assessment, ctx)
            decision_log_id = None
            if d4.decision_log_id is None:
                decision_log_id = await self._write_decision_log(
                    session,
                    project_id=project_id,
                    run_id=run_id,
                    decision_point="D4",
                    stage=stage,
                    context_digest=assessment.d4_context_digest(),
                    options=list(D4_OPTIONS),
                    chosen=assessment.action,
                    decision=d4,
                    cost_usd=cost,
                )
            if project is not None:
                if assessment.action == "circuit_break" or d4.policy_action == "circuit_break":
                    if str(project.status) not in {"CIRCUIT_BREAK", "WAIT_HUMAN"}:
                        transition(project, "CIRCUIT_BREAK", reason=f"{stage} L3 熔断", extra={"level": "L3"})
                elif (
                    d4.policy_action == "need_human" and str(project.status) not in {"WAIT_HUMAN"}
                ):
                    if str(project.status) == "RISK_EVALUATING":
                        transition(project, "WAIT_HUMAN", reason=f"{stage} D4 need_human")
                    else:
                        transition(project, "RISK_EVALUATING", reason=f"{stage} D4 转人工评估")
                        transition(project, "WAIT_HUMAN", reason=f"{stage} D4 need_human")
            await session.commit()

        sse_mod.publish_risk_policy(
            project_id,
            {
                "decision_log_id": decision_log_id,
                "risk_score": d4.risk_score,
                "confidence_score": d4.confidence_score,
                "reversibility_score": d4.reversibility_score,
                "policy_action": d4.policy_action,
                "policy_version": d4.policy_version,
                "degraded": d4.degraded,
                "decision_point": "D4",
                "reason": d4.reason,
            },
        )
        sse_mod.publish(
            "decision",
            {
                "decision_point": "D4",
                "chosen": assessment.action,
                "rationale": f"[{assessment.level}] {assessment.reason} → {assessment.action}"
                + (f"（degrade={assessment.degrade}）" if assessment.degrade else ""),
            },
            project_id=project_id,
        )
        structured_log(
            "pipeline_stage_failed",
            project_id=project_id,
            run_id=run_id,
            stage=stage,
            attempt=attempt,
            level=assessment.level,
            action=assessment.action,
            error_code=assessment.error_code,
            cost=round(cost, 6),
        )

        if assessment.action == "circuit_break" or d4.policy_action == "circuit_break":
            return await self._circuit_break(
                project_id,
                run_id,
                stage,
                attempt,
                reason=assessment.reason,
                decision_point="D4",
                decision=d4,
                decision_log_id=decision_log_id,
                assessment=assessment,
            )
        if d4.policy_action == "need_human":
            node = STAGE_INTERVENTION_NODE.get(stage)
            return await self._wait_for_human(
                project_id,
                run_id,
                stage,
                attempt,
                node=node,
                decision_point="D4",
                payload={
                    "stage": stage,
                    "level": assessment.level,
                    "action": assessment.action,
                    "reason": assessment.reason,
                    "decision_log_id": decision_log_id,
                    "options": ["approve", "modify", "reject", "rerun", "downgrade", "abort"],
                },
                note="D4 失败处置转人工（风险策略 need_human）",
                mark=(stage, attempt),
            )
        # 重试 / 降级：继续循环，由 resume 计划以 attempt+1 重新进入该环节
        detail = f"{assessment.action}（level={assessment.level}, attempt={attempt}）"
        structured_log(
            "pipeline_stage_retry",
            project_id=project_id,
            run_id=run_id,
            stage=stage,
            attempt=attempt,
            action=assessment.action,
            degrade=assessment.degrade,
        )
        return EngineOutcome(reason="continue", stage=stage, detail=detail)

    async def _policy_for_failure(
        self,
        project_id: int,
        run_id: int,
        stage: str,
        attempt: int,
        assessment: Any,
        ctx: StageContext,
    ) -> PolicyDecision:
        policy, mounted, _source = self.policy()
        context = {
            "stage": stage,
            "attempt": attempt,
            "level": assessment.level,
            "error_code": assessment.error_code,
            "reason": assessment.reason,
            "proposed_action": assessment.action,
            "degrade": assessment.degrade,
            "pipeline_run_id": run_id,
            "retries_used": max(0, attempt - 1),
            "policy_mounted": mounted,
        }
        try:
            raw = await _call_policy(policy, project_id, "D4", context)
        except Exception as exc:  # noqa: BLE001
            logger.exception("D4 风险策略调用失败，按 need_human 处理")
            return PolicyDecision(
                policy_action="need_human",
                rationale=f"D4 策略调用异常（{type(exc).__name__}: {exc}）",
                policy_version="policy_error",
                options_considered=list(D4_OPTIONS),
                reason="policy_call_failed",
            )
        decision = _normalize_policy_result(raw, "D4")
        if not decision.options_considered:
            decision.options_considered = list(D4_OPTIONS)
        if decision.degraded and not decision.rationale.startswith(STUB_REASON):
            decision.rationale = f"{STUB_REASON}；{decision.rationale}".strip("；")
        _ = ctx
        return decision

    # ---------------- 人工等待 / 熔断 ----------------
    async def _wait_for_human(
        self,
        project_id: int,
        run_id: int,
        stage: str,
        attempt: int,
        *,
        node: str | None,
        decision_point: str | None,
        payload: dict[str, Any],
        note: str,
        mark: tuple[str, int] | None = None,
    ) -> EngineOutcome:
        """转 ``WAIT_HUMAN`` 并推 ``need_human``。

        ``mark`` 指向需要写入 ``waiting_human`` 状态的环节（``None`` 表示不改动任何环节行，
        例如 manual 模式在最后一个环节之后等待人工确认）。
        """
        async with self.session_factory() as session:
            project = (
                await session.execute(select(Project).where(Project.id == project_id))
            ).scalar_one_or_none()
            if (
                project is not None
                and str(project.status) != "WAIT_HUMAN"
                and (
                    str(project.status) in ACTIVE_STATUSES
                    or str(project.status) == "CIRCUIT_BREAK"
                )
            ):
                transition(project, "WAIT_HUMAN", reason=note)
            run = (
                await session.execute(select(PipelineRun).where(PipelineRun.id == run_id))
            ).scalar_one_or_none()
            if run is not None and str(run.status) != "circuit_break":
                run.status = "paused"
            if mark is not None:
                await resume_mod.upsert_stage_output(
                    session,
                    pipeline_run_id=run_id,
                    stage=mark[0],
                    attempt=mark[1],
                    status="waiting_human",
                    output_json={"_waiting": {"reason": note, "node": node, "decision_point": decision_point}},
                )
            await session.commit()

        sse_mod.publish(
            "need_human",
            {
                "node": node,
                "node_defined": node is not None,
                "payload": {
                    **payload,
                    "stage": stage,
                    "attempt": attempt,
                    "decision_point": decision_point,
                    "note": note,
                },
            },
            project_id=project_id,
        )
        structured_log(
            "need_human",
            project_id=project_id,
            run_id=run_id,
            stage=stage,
            attempt=attempt,
            node=node,
            decision_point=decision_point,
        )
        return EngineOutcome(reason="wait_human", stage=stage, detail=note)

    async def _circuit_break(
        self,
        project_id: int,
        run_id: int,
        stage: str,
        attempt: int,
        *,
        reason: str,
        decision_point: str = "D4",
        decision: PolicyDecision | None = None,
        decision_log_id: int | None = None,
        assessment: Any = None,
    ) -> EngineOutcome:
        """L3 熔断：生成《失败分析报告》→ 项目转 WAIT_HUMAN → 推 ``circuit_break``。"""
        async with self.session_factory() as session:
            project = (
                await session.execute(select(Project).where(Project.id == project_id))
            ).scalar_one_or_none()
            run = (
                await session.execute(select(PipelineRun).where(PipelineRun.id == run_id))
            ).scalar_one_or_none()
            report = await build_failure_report(session, project_id, run_id=run_id)
            if isinstance(report.get("failure_chain"), list) and assessment is not None:
                report["failure_chain"].append(
                    {
                        "stage": stage,
                        "attempt": attempt,
                        "status": "circuit_break",
                        "level": assessment.level,
                        "action": assessment.action,
                        "error_code": assessment.error_code,
                        "reason": assessment.reason,
                        "at": datetime.now(UTC).isoformat(),
                    }
                )
            if decision_log_id is not None:
                report["decision_log_ids"] = sorted(
                    {*report.get("decision_log_ids", []), decision_log_id}
                )
            report["trigger"] = {
                "stage": stage,
                "attempt": attempt,
                "decision_point": decision_point,
                "reason": reason,
                "policy_action": decision.policy_action if decision else "circuit_break",
                "policy_version": decision.policy_version if decision else None,
            }
            await persist_failure_report(
                session, run_id=run_id, stage=stage, attempt=attempt, report=report
            )
            if run is not None:
                run.status = "circuit_break"
                run.finished_at = datetime.now(UTC)
            if project is not None and str(project.status) != "CIRCUIT_BREAK":
                if str(project.status) in ACTIVE_STATUSES:
                    transition(project, "CIRCUIT_BREAK", reason=f"L3 熔断：{reason}")
                elif str(project.status) == "WAIT_HUMAN":
                    pass
            if project is not None and str(project.status) == "CIRCUIT_BREAK":
                # 计划书 §2.3：CIRCUIT_BREAK → WAIT_HUMAN（产出失败报告，等待人工）
                transition(project, "WAIT_HUMAN", reason="失败分析报告已产出，等待人工处置")
            await session.commit()

        sse_mod.publish(
            "circuit_break",
            {"reason": reason, "report_url": f"/api/v1/reports/{project_id}/failure"},
            project_id=project_id,
        )
        sse_mod.publish(
            "need_human",
            {
                "node": "N3",
                "node_defined": True,
                "payload": {
                    "stage": stage,
                    "attempt": attempt,
                    "decision_point": "D4",
                    "level": "L3",
                    "reason": reason,
                    "report_url": f"/api/v1/reports/{project_id}/failure",
                    "options": ["approve", "rerun", "downgrade", "abort"],
                    "suggested_human_actions": suggest_human_actions(
                        stage, "L3", (assessment.error_code if assessment else "circuit_break")
                    ),
                },
            },
            project_id=project_id,
        )
        structured_log(
            "circuit_break",
            project_id=project_id,
            run_id=run_id,
            stage=stage,
            attempt=attempt,
            reason=reason,
            report_url=f"/api/v1/reports/{project_id}/failure",
        )
        return EngineOutcome(reason="circuit_break", stage=stage, detail=reason)

    async def _safe_finalize_error(self, project_id: int, run_id: int, exc: BaseException) -> None:
        try:
            async with self.session_factory() as session:
                run = (
                    await session.execute(select(PipelineRun).where(PipelineRun.id == run_id))
                ).scalar_one_or_none()
                if run is not None and str(run.status) == "running":
                    run.status = "failed"
                    run.finished_at = datetime.now(UTC)
                await resume_mod.reset_stale_running(session, run_id)
                await session.commit()
        except Exception:  # noqa: BLE001 - 兜底路径本身失败只记日志
            logger.exception("兜底收尾失败 project_id=%s run_id=%s", project_id, run_id)
        sse_mod.publish(
            "need_human",
            {
                "node": "N3",
                "node_defined": True,
                "payload": {
                    "level": "L3",
                    "reason": f"引擎内部异常：{type(exc).__name__}: {exc}",
                    "report_url": f"/api/v1/reports/{project_id}/failure",
                    "options": ["rerun", "abort"],
                    "note": "运行状态已收尾为 failed；重启后可用 POST /pipelines/{pid}/resume 断点续跑",
                },
            },
            project_id=project_id,
        )

    # ---------------- 迭代收尾与停止条件 ----------------
    async def _finalize_iteration(
        self, project_id: int, run_id: int, plan: list[resume_mod.StagePlanItem]
    ) -> EngineOutcome:
        async with self.session_factory() as session:
            project = (
                await session.execute(select(Project).where(Project.id == project_id))
            ).scalar_one_or_none()
            run = (
                await session.execute(select(PipelineRun).where(PipelineRun.id == run_id))
            ).scalar_one_or_none()
            if project is None or run is None:
                return EngineOutcome(reason="run_missing", detail="project/run 不存在")
            taskbook = None
            if project.taskbook_id:
                taskbook = (
                    await session.execute(select(Taskbook).where(Taskbook.id == int(project.taskbook_id)))
                ).scalar_one_or_none()

            review_output = next(
                (item.output_json for item in plan if item.stage == "review" and item.output_json), None
            )
            score = extract_score(review_output, (review_output or {}).get("metrics"))
            previous_score = await self._previous_score(session, project_id, int(run.iteration or 1))
            thresholds: StopThresholds = resolve_thresholds(project=project, taskbook=taskbook)
            decision = evaluate_stop_conditions(
                iteration=int(run.iteration or 1),
                score=score,
                previous_score=previous_score,
                thresholds=thresholds,
            )

            if str(project.status) in ACTIVE_STATUSES:
                transition(project, "REVIEWING", reason="六环节执行完毕")
            run.status = "done"
            run.stop_reason = decision.reason
            run.finished_at = datetime.now(UTC)
            run.total_cost_usd = await resume_mod.run_cost_total(session, run_id)
            next_iteration: int | None = None
            next_run_id: int | None = None
            if decision.should_stop:
                if str(project.status) == "REVIEWING":
                    transition(project, "DONE", reason=f"停止条件满足：{decision.reason}")
                await session.commit()
            else:
                if str(project.status) == "REVIEWING":
                    transition(project, "RUNNING", reason="未达停止条件，轮次+1")
                next_iteration = int(run.iteration or 1) + 1
                await session.commit()
                new_run = await self._create_run(session, project, iteration=next_iteration)
                next_run_id = int(new_run.id)
                await session.commit()

        sse_mod.publish(
            "decision",
            {
                "decision_point": "D5",
                "chosen": "stop" if decision.should_stop else "continue",
                "rationale": decision.detail,
            },
            project_id=project_id,
        )
        if decision.should_stop:
            structured_log(
                "pipeline_finished",
                project_id=project_id,
                run_id=run_id,
                stop_reason=decision.reason,
                score=decision.inputs.get("score"),
            )
            return EngineOutcome(
                reason="done", stage="review", detail=f"stop_reason={decision.reason}；{decision.detail}"
            )
        structured_log(
            "pipeline_iteration_advanced",
            project_id=project_id,
            previous_run_id=run_id,
            iteration=next_iteration,
            detail=decision.detail,
        )
        return EngineOutcome(
            reason="continue",
            detail=f"未达停止条件，轮次+1 → {next_iteration}（{decision.detail}）",
            next_run_id=next_run_id,
        )

    async def _previous_score(
        self, session: AsyncSession, project_id: int, iteration: int
    ) -> float | None:
        """上一轮的 review 总分（真实值；无上一轮则 ``None``）。"""
        if iteration <= 1:
            return None
        rows = list(
            (
                await session.execute(
                    select(StageOutput)
                    .join(PipelineRun, PipelineRun.id == StageOutput.pipeline_run_id)
                    .where(
                        PipelineRun.project_id == project_id,
                        PipelineRun.iteration == iteration - 1,
                        StageOutput.stage == "review",
                        StageOutput.status == "done",
                    )
                    .order_by(StageOutput.attempt.desc())
                    .limit(1)
                )
            )
            .scalars()
            .all()
        )
        if not rows:
            return None
        payload = rows[0].output_json if isinstance(rows[0].output_json, dict) else None
        return extract_score(payload, (payload or {}).get("metrics"))

    async def _emit_guardrail(self, project_id: int, stage: str) -> None:
        """推送 ``guardrail`` 事件（成本双线，真实数据；WP10 未挂载时不发风险判定）。"""
        snapshot = await _cost_snapshot(project_id)
        if not snapshot:
            return
        sse_mod.publish_guardrail(project_id, snapshot)
        structured_log(
            "guardrail_snapshot",
            project_id=project_id,
            stage=stage,
            cost=snapshot.get("used_usd"),
        )


# --------------------------------------------------------------------------- #
# 辅助
# --------------------------------------------------------------------------- #
class _SessionLock:
    """``async with engine.lock_project(pid) as session``：串行化同一项目的控制操作。"""

    def __init__(self, engine: PipelineEngine, project_id: int) -> None:
        self._engine = engine
        self._project_id = project_id
        self._lock: asyncio.Lock | None = None
        self._session: Any = None

    async def __aenter__(self) -> AsyncSession:
        self._lock = self._engine._lock(self._project_id)
        await self._lock.acquire()
        self._session = self._engine.session_factory()()
        return self._session

    async def __aexit__(self, exc_type: Any, exc: Any, tb: Any) -> None:
        try:
            if self._session is not None:
                await self._session.close()
        finally:
            if self._lock is not None and self._lock.locked():
                self._lock.release()


async def _call_policy(
    policy: Any, project_id: int, decision_point: str, context: dict[str, Any]
) -> Any:
    """调用 WP10 策略（兼容对象方法 / 纯函数、同步 / 异步、关键字 / 位置参数）。"""
    target = policy if callable(policy) and not hasattr(policy, "evaluate_decision") else policy.evaluate_decision
    candidates = (
        {"project_id": project_id, "decision_point": decision_point, "context": context},
        {"project_id": project_id, "decision_point": decision_point, "context": context, "session": None},
        (project_id, decision_point, context),
        (project_id, decision_point),
    )
    last_error: Exception | None = None
    for args in candidates:
        try:
            if isinstance(args, dict):
                args = {key: value for key, value in args.items() if key != "session"}
                result = target(**args)
            else:
                result = target(*args)
        except TypeError as exc:
            last_error = exc
            continue
        if inspect.isawaitable(result):
            result = await result
        return result
    raise TypeError(f"无法调用风险策略 evaluate_decision：{last_error}")


def _normalize_mode(mode: str) -> str:
    value = str(mode or "").strip().lower()
    if value not in {"auto", "manual"}:
        raise PipelineError("invalid_mode", f"未知模式 {mode!r}（合法 auto|manual）", {"mode": mode})
    return value


def _clamp(value: float, low: float, high: float, digits: int) -> float:
    try:
        number = float(value)
    except (TypeError, ValueError):
        number = low
    number = max(low, min(high, number))
    return round(number, digits)


#: 各决策点的动作域字段（contracts.decision_points.action_domain，与 WP10
#: ``risk_policy.ACTION_DOMAIN_FIELDS`` 同源；此处本地声明以避免引擎 → 策略的硬依赖）
ACTION_DOMAIN_KEYS: dict[str, tuple[str, ...]] = {
    "D1": ("queries", "fields", "paper_limit"),
    "D2": ("method_index",),
    "D3": ("template_id", "params", "sample_size"),
    "D4": ("failure_action",),
    "D5": ("iteration_decision",),
    "D6": ("outline", "section_focus"),
}

_ALL_ACTION_DOMAIN_KEYS = frozenset(
    key for keys in ACTION_DOMAIN_KEYS.values() for key in keys
) | {"template_id", "params", "sample_size"}


def _derive_action_domain(ctx: StageContext, decision_point: str) -> dict[str, Any] | None:
    """从**上游真实产出**派生本次决策的动作域（供 WP10 硬护栏校验）。

    只在能取到真实值时才声明字段；取不到就不声明——由 WP10 按「未声明动作」处理
    （安全护栏对 ``template_id`` 这类必填字段会直接判失败），**绝不编造模板或样本量**。

    L2 降级的动作覆盖（缩小样本量 / 换模板）优先于派生值，保证降级真的生效。
    """
    domain: dict[str, Any] = {}

    if decision_point == "D2":
        index = _as_int((ctx.upstream("plan_review") or {}).get("selected_method_index"))
        if index is not None:
            domain["method_index"] = index
    elif decision_point == "D3":
        # D3 实验配置：取上游 plan_review 选中的方案（真实选型结果的 template_id/params）
        selected = (ctx.upstream("plan_review") or {}).get("selected_method")
        if isinstance(selected, Mapping):
            template_id = selected.get("template_id")
            if isinstance(template_id, str) and template_id.strip():
                domain["template_id"] = template_id.strip()
            params = selected.get("params")
            if isinstance(params, Mapping) and params:
                domain["params"] = dict(params)
                sample_size = params.get("sample_size")
                if isinstance(sample_size, int) and not isinstance(sample_size, bool):
                    domain["sample_size"] = sample_size
    elif decision_point == "D6":
        outline = (ctx.upstream("plan_review") or {}).get("outline")
        if isinstance(outline, list) and outline:
            domain["outline"] = outline

    degrade = ctx.degrade if isinstance(ctx.degrade, Mapping) else {}
    for key, value in degrade.items():
        if str(key) in _ALL_ACTION_DOMAIN_KEYS and value is not None:
            domain[str(key)] = value

    return domain or None


def _extract_failure_hints(
    rows: list[StageOutput], stage: str
) -> tuple[dict[str, Any], int, int]:
    """从历史失败记录提取 降级提示 / L2 已用次数 / plan 回退已用次数（重启后仍有效）。"""
    degrade: dict[str, Any] = {}
    l2_used = 0
    revert_used = 0
    for row in rows:
        payload = row.output_json if isinstance(row.output_json, dict) else None
        if not payload:
            if str(row.stage) == stage and str(row.status) == "failed":
                l2_used += 0
            continue
        failure = payload.get("_failure")
        if isinstance(failure, dict):
            if payload.get("revert_to") == "plan" or failure.get("revert_to") == "plan":
                revert_used += 1
            if str(row.stage) == stage and failure.get("level") == "L2":
                l2_used += 1
            if str(row.stage) == stage and isinstance(failure.get("degrade"), dict) and failure["degrade"]:
                degrade = dict(failure["degrade"])
    return degrade, l2_used, revert_used


def _run_to_dict(run: PipelineRun | None) -> dict[str, Any] | None:
    if run is None:
        return None
    return {
        "id": int(run.id),
        "project_id": int(run.project_id),
        "iteration": int(run.iteration or 1),
        "mode": str(run.mode),
        "status": str(run.status),
        "started_at": run.started_at.isoformat() if run.started_at else None,
        "finished_at": run.finished_at.isoformat() if run.finished_at else None,
        "total_cost_usd": float(run.total_cost_usd or 0),
        "stop_reason": run.stop_reason,
        "created_at": run.created_at.isoformat() if run.created_at else None,
    }


def _project_to_dict(project: Project) -> dict[str, Any]:
    return {
        "id": int(project.id),
        "name": project.name,
        "status": str(project.status),
        "mode": str(project.mode),
        "current_iteration": int(project.current_iteration or 0),
        "is_demo": bool(project.is_demo),
        "taskbook_id": int(project.taskbook_id) if project.taskbook_id else None,
    }


def _current_stage(stages: list[dict[str, Any]]) -> dict[str, Any] | None:
    for item in stages:
        if item["status"] in {"running", "waiting_human"}:
            return {"stage": item["stage"], "status": item["status"], "attempt": item["attempt"]}
    for item in stages:
        if item["status"] == "pending":
            return {"stage": item["stage"], "status": "pending", "attempt": item["attempt"]}
    return None


def _registry_snapshot() -> dict[str, Any]:
    from services.pipeline.stages import registry_snapshot

    return registry_snapshot()


async def _cost_snapshot(project_id: int) -> dict[str, Any]:
    try:
        from services.cost import accumulate

        summary = await accumulate(project_id)
        return summary.to_dict()
    except Exception as exc:  # noqa: BLE001 - 成本服务不可用不阻塞流水线
        logger.warning("成本快照不可用 project_id=%s err=%s", project_id, exc)
        return {}


#: 进程级单例（API 层与测试共用）
engine = PipelineEngine()


__all__ = [
    "ACTIVE_STATUSES",
    "EngineOutcome",
    "PipelineEngine",
    "PipelineError",
    "PolicyDecision",
    "PolicyStub",
    "POLICY_STUB_VERSION",
    "RUN_STATUSES",
    "STAGE_ORDER",
    "STUB_REASON",
    "engine",
]
