# Copyright 2026 SciLoop contributors
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
"""进程内受限执行器（WP11-T1）。

**这不是 Docker 沙箱**（Docker 沙箱已降级为 P1 路线图）：可执行动作只有一件事——
「通过模板注册表调用已注册模板」。执行器负责把这些硬约束落成代码：

1. 模板白名单 + 参数 schema + ``sample_size<=50``（``registry.validate_config``）
2. ``asyncio.Semaphore(EXECUTOR_MAX_CONCURRENCY)`` 全局并发限流（默认 2）
3. 单 Run 超时 ``EXECUTOR_RUN_TIMEOUT_SECONDS``（默认 300s，严格小于单环节 1200s）
4. 输出累计上限 ``EXECUTOR_MAX_OUTPUT_BYTES``（默认 5 MiB）
5. 执行前出网预检：模板将访问的模型 base_url 主机必须过白名单且非内网
6. 禁止任意 shell / eval / exec / 运行时安装依赖（模板注册表内不存在此类入口）

每次 Run 落 ``experiments`` / ``experiment_runs`` 两行，并把原始输出与指标写进
``<artifact_root>/run_{id}/output/``（见 :mod:`executor.artifact_collector`）。
"""

from __future__ import annotations

import asyncio
import functools
import logging
import time
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any

from executor.artifact_collector import ArtifactCollector, canonical_json, sha256_hex
from executor.egress_proxy import get_policy, resolve_model_hosts
from executor.errors import (
    ExecutorError,
    ExecutorTimeoutError,
    LimitsConfigError,
    ModelUnavailableError,
    OutputLimitExceededError,
)
from executor.limits import ExecutorLimits, get_limits

logger = logging.getLogger("sciloop.executor.runner")

# --------------------------------------------------------------------------- #
# 并发限流（进程内全局）
# --------------------------------------------------------------------------- #
_STATE: dict[str, Any] = {"size": None, "sem": None, "active": 0, "max_observed": 0}


def _semaphore(size: int) -> asyncio.Semaphore:
    if _STATE["sem"] is None or _STATE["size"] != size:
        _STATE.update(size=size, sem=asyncio.Semaphore(size), active=0, max_observed=0)
    return _STATE["sem"]  # type: ignore[return-value]


def reset_concurrency_stats() -> None:
    """重置并发统计（验收 A2 在测试前调用）。"""
    _STATE.update(size=None, sem=None, active=0, max_observed=0)


def concurrency_snapshot() -> dict[str, Any]:
    """当前并发状态（``max_observed`` 是验收 A2 的关键证据）。"""
    return {
        "limit": _STATE["size"],
        "active": _STATE["active"],
        "max_observed": _STATE["max_observed"],
    }


async def run_with_limits(fn: Callable[[], Awaitable[Any]], *, timeout_seconds: int | float) -> Any:
    """在并发名额与单 Run 超时约束下执行 ``fn``。

    - 并发：全局信号量，最多 ``EXECUTOR_MAX_CONCURRENCY`` 个任务同时执行
    - 超时：``asyncio.wait_for``，超时抛 :class:`ExecutorTimeoutError`
    """
    limits = get_limits()
    limits.assert_hierarchy()
    semaphore = _semaphore(limits.max_concurrency)
    async with semaphore:
        _STATE["active"] = int(_STATE["active"]) + 1
        _STATE["max_observed"] = max(int(_STATE["max_observed"]), int(_STATE["active"]))
        try:
            return await asyncio.wait_for(fn(), timeout=float(timeout_seconds))
        except TimeoutError as exc:  # Python 3.11: asyncio.TimeoutError 即 TimeoutError
            raise ExecutorTimeoutError(
                f"单 Run 超时：{timeout_seconds}s（EXECUTOR_RUN_TIMEOUT_SECONDS）",
                detail={"timeout_seconds": float(timeout_seconds)},
            ) from exc
        finally:
            _STATE["active"] = int(_STATE["active"]) - 1


# --------------------------------------------------------------------------- #
# 上下文与产出
# --------------------------------------------------------------------------- #
@dataclass
class RunContext:
    """交给模板的执行上下文（模板只读）。"""

    run_id: int
    template_id: str
    params: dict[str, Any]
    samples: list[dict[str, Any]]
    limits: ExecutorLimits
    experiment_id: int | None = None
    project_id: int | None = None
    stage: str = "experiment"
    attempt: int = 1
    purpose_prefix: str = "experiment"
    #: 每个样本的总超时预算（秒）——模板据此自限，避免整 Run 被 wait_for 硬砍
    deadline_seconds: float = 300.0
    started_at: datetime = field(default_factory=lambda: datetime.now(UTC))
    #: 数据集 manifest（注册表归一后传入；Passport 的 dataset_* 字段来源）
    dataset: dict[str, Any] = field(default_factory=dict)
    #: 模板版本 / Prompt 版本（注册表提供）
    template_version: str | None = None
    prompt_version: str | None = None
    #: 降级说明（透明记录，禁止静默）
    degradations: list[str] = field(default_factory=list)
    #: 回放源：``{fixture_key: 原始输出文本}``（来自父 Run 的工件或 demo_fixtures）。
    #: 非空即表示本次 Run 为**回放**：模板不得发起任何实时调用。
    fixture_source: Mapping[str, Any] | None = None
    #: 回放模式标记（写入 Passport 的 ``is_replay``，禁止冒充实时）
    replay_only: bool = False

    @property
    def sample_size(self) -> int:
        return len(self.samples)

    def purpose(self, suffix: str) -> str:
        """``llm_call_logs.purpose``（<=64 字符）。"""
        text = f"{self.template_id}:{suffix}"[:64]
        return text

    def fixture_for(self, key: str) -> Any | None:
        """取回放源的原始输出（无回放源时返回 ``None``）。"""
        if not self.fixture_source:
            return None
        return self.fixture_source.get(str(key))

    def degrade(self, note: str) -> None:
        """记录一次降级（去重，保持顺序）。"""
        if note and note not in self.degradations:
            self.degradations.append(note)


@dataclass
class RunOutcome:
    """执行器产出（service 层据此落指标与 Passport）。"""

    run_id: int
    experiment_id: int | None
    template_id: str
    status: str
    started_at: datetime
    finished_at: datetime
    duration_ms: int
    metrics: dict[str, Any] = field(default_factory=dict)
    records: list[dict[str, Any]] = field(default_factory=list)
    cost_usd: float | None = None
    degradations: list[str] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)
    error: str | None = None
    error_code: str | None = None
    artifact_manifest: dict[str, Any] = field(default_factory=dict)
    artifact_path: str | None = None
    #: Passport 溯源载荷（Prompt 正文 / 生成参数 / 数据集 manifest；模板提供）
    prompt_payload: dict[str, Any] = field(default_factory=dict)
    concurrency: dict[str, Any] = field(default_factory=dict)
    contract: dict[str, Any] = field(default_factory=dict)
    #: 本次 Run 实际使用的样本（已归一；Passport 的 dataset_sha256 / sample_manifest 来源）
    samples: list[dict[str, Any]] = field(default_factory=list)
    #: 数据集 manifest（``registry.dataset_manifest``）
    dataset: dict[str, Any] = field(default_factory=dict)
    #: 实际执行的模板配置（**不含** samples，保证 replay/rerun 可比对）
    template_config: dict[str, Any] = field(default_factory=dict)
    #: 是否为回放 Run（Passport 的 ``is_replay`` 必须如实反映）
    replay_only: bool = False

    @property
    def ok(self) -> bool:
        return self.status == "success"

    @property
    def is_replay(self) -> bool:
        """回放判定：显式回放模式，或**任一**样本输出来自回放源。

        宁可把混合 Run 判为回放，也不得让回放结果冒充实时实验
        （``contracts.forbidden_actions``）。
        """
        if self.replay_only:
            return True
        return any(bool(record.get("is_replay")) for record in self.records)

    def to_dict(self) -> dict[str, Any]:
        return {
            "run_id": self.run_id,
            "experiment_id": self.experiment_id,
            "template_id": self.template_id,
            "status": self.status,
            "started_at": self.started_at.isoformat(),
            "finished_at": self.finished_at.isoformat(),
            "duration_ms": self.duration_ms,
            "metrics": self.metrics,
            "cost_usd": self.cost_usd,
            "degradations": self.degradations,
            "notes": self.notes,
            "error": self.error,
            "error_code": self.error_code,
            "artifact_path": self.artifact_path,
            "concurrency": self.concurrency,
            "sample_size": len(self.samples),
            "is_replay": self.is_replay,
        }


def _truncate(text: str, limit: int) -> tuple[str, bool]:
    raw = text.encode("utf-8")
    if len(raw) <= limit:
        return text, False
    return raw[:limit].decode("utf-8", errors="ignore"), True


# --------------------------------------------------------------------------- #
# 主流程
# --------------------------------------------------------------------------- #
def _template_config(
    *,
    template_id: str,
    spec_version: str,
    prompt_version: str | None,
    sample_size: int,
    params: dict[str, Any],
    dataset: dict[str, Any],
) -> dict[str, Any]:
    """Passport 的 ``template_config``：**只含可比对的配置**（不含样本正文）。

    replay / rerun 复用同一份配置 → ``config_changes`` 为空即为「配置未变」，
    指标差异只能来自模型行为，不会被序列化噪声污染。
    """
    return {
        "template_id": template_id,
        "template_version": spec_version,
        "prompt_version": prompt_version,
        "sample_size": sample_size,
        "sample_ids": list(dataset.get("sample_ids") or []),
        "dataset_name": dataset.get("dataset_name"),
        "dataset_version": dataset.get("dataset_version"),
        "dataset_source_file": dataset.get("source_file"),
        "params": dict(params),
    }


async def execute_template(
    template_id: str,
    config: dict[str, Any],
    *,
    project_id: int | None = None,
    stage: str = "experiment",
    attempt: int = 1,
    run_id: int | None = None,
    experiment_id: int | None = None,
    persist: bool = True,
    session: Any = None,
    replay_only: bool = False,
    fixture_source: Mapping[str, Any] | None = None,
) -> RunOutcome:
    """执行一次模板 Run（唯一入口；模板不允许被绕过注册表直接调用）。

    :param config: 模板配置（``params`` / ``sample_size`` / ``samples`` 等，schema 由注册表校验）
    :param persist: 是否落 ``experiments`` / ``experiment_runs``（验收脚本可置 False）
    :param replay_only: 回放模式——**不得发起实时调用**（出网预检失败不再阻断）
    :param fixture_source: 回放源 ``{fixture_key: 原始输出文本}``
    """
    from services.experiment import registry as registry_mod

    limits = get_limits()
    limits.assert_hierarchy()

    started_at = datetime.now(UTC)
    wall_start = time.perf_counter()

    # ① 模板白名单 + 参数 schema + sample_size（越界即拒，不做隐式截断）
    spec, validated = registry_mod.validate_config(template_id, config)
    sample_size = limits.clamp_sample_size(validated["sample_size"])
    params = dict(validated["params"])
    samples = list(validated["samples"])

    # ② 出网预检：模板将访问的模型主机必须过白名单（解析不到即如实失败）
    #    回放模式例外：本次 Run 不产生任何出网（回放源缺失会直接失败，不会静默走实时），
    #    因此预检降级为「记录 + 审计」，不阻断回放。
    model_refs: list[str] = list(validated.get("model_refs") or [])
    egress_note: str | None = None
    hosts: dict[str, str] = {}
    granted_hosts: list[str] = []
    egress_policy = get_policy()
    try:
        hosts = await resolve_model_hosts(model_refs) if model_refs else {}
        unresolved = [ref for ref in model_refs if ref not in hosts]
        if unresolved:
            raise ModelUnavailableError(
                f"模型无法解析（无路由 / 无密钥）：{unresolved}。"
                "请先在「设置 → 模型配置」中配置供应商与密钥，或设置 LLM_DEFAULT_* 环境变量。",
                detail={"model_refs": model_refs, "unresolved": unresolved, "template_id": template_id},
            )
        granted_hosts = egress_policy.require_all(hosts.values()) if hosts else []
    except ExecutorError as exc:
        if not replay_only:
            raise
        egress_note = (
            f"回放模式跳过出网预检（{exc.code}: {exc}）——本 Run 不发起任何实时调用，"
            "回放源缺失会直接失败而不会静默走实时"
        )

    outcome = RunOutcome(
        run_id=int(run_id or 0),
        experiment_id=experiment_id,
        template_id=spec.template_id,
        status="running",
        started_at=started_at,
        finished_at=started_at,
        duration_ms=0,
        contract={
            "limits": limits.to_dict(),
            "egress": egress_policy.report(),
            "egress_hosts_granted": sorted(set(granted_hosts)),
            "template_version": spec.version,
            "sample_size_requested": sample_size,
            "replay_only": bool(replay_only),
            "egress_note": egress_note,
        },
        samples=[dict(sample) for sample in samples],
        dataset=dict(validated.get("dataset") or {}),
        template_config=_template_config(
            template_id=spec.template_id,
            spec_version=spec.version,
            prompt_version=validated.get("prompt_version"),
            sample_size=sample_size,
            params=params,
            dataset=validated.get("dataset") or {},
        ),
        replay_only=bool(replay_only),
    )

    # ③ 落 experiments / experiment_runs（running），拿到 run_id 以定位工件目录
    if persist:
        outcome.experiment_id, outcome.run_id = await _persist_start(
            session, template_id=template_id, config=config, stage=stage, attempt=attempt,
            experiment_id=experiment_id, run_id=run_id,
        )
    if not outcome.run_id:
        raise LimitsConfigError(
            "无法确定 run_id：persist=False 时必须显式传入 run_id（工件目录命名依赖它）",
            detail={"template_id": template_id},
        )

    collector = ArtifactCollector(
        run_id=outcome.run_id, experiment_id=outcome.experiment_id
    )
    collector.prepare()

    ctx = RunContext(
        run_id=outcome.run_id,
        template_id=spec.template_id,
        params=params,
        samples=samples,
        limits=limits,
        experiment_id=outcome.experiment_id,
        project_id=project_id,
        stage=stage,
        attempt=attempt,
        purpose_prefix=f"{template_id}:run{outcome.run_id}",
        deadline_seconds=max(1.0, float(limits.run_timeout_seconds) - 5.0),
        dataset=dict(validated.get("dataset") or {}),
        template_version=spec.version,
        prompt_version=validated.get("prompt_version"),
        fixture_source=fixture_source,
        replay_only=bool(replay_only),
    )
    if egress_note:
        ctx.degrade(egress_note)

    try:
        result = await run_with_limits(
            functools.partial(registry_mod.execute, spec.template_id, ctx),
            timeout_seconds=limits.run_timeout_seconds,
        )
    except ExecutorError as exc:
        return await _finalize_error(
            outcome, collector, exc, wall_start, persist=persist, session=session
        )
    except Exception as exc:  # noqa: BLE001 - 模板异常不得让进程失控
        logger.exception("模板执行异常 template=%s run=%s", template_id, outcome.run_id)
        wrapped = ExecutorError(
            f"模板执行异常：{type(exc).__name__}: {exc}", detail={"template_id": template_id}
        )
        return await _finalize_error(
            outcome, collector, wrapped, wall_start, persist=persist, session=session
        )

    metrics = dict(result.get("metrics") or {})
    records = list(result.get("records") or [])
    cost_usd = result.get("cost_usd")
    outcome.metrics = metrics
    outcome.records = records
    outcome.cost_usd = float(cost_usd) if isinstance(cost_usd, (int, float)) else None
    outcome.degradations = list(result.get("degradations") or []) or list(ctx.degradations)
    outcome.notes = list(result.get("notes") or [])
    outcome.status = str(result.get("status") or "success")
    outcome.prompt_payload = dict(result.get("prompt_payload") or {})

    # ④ 工件回收（结果 / 原始输出 / 指标 / meta）；超限即失败，不静默截断
    try:
        collector.write_json("result.json", result)
        collector.write_json("metrics.json", metrics)
        collector.write_json("raw_output.json", {"records": records})
        collector.write_json(
            "run_meta.json",
            {
                "run_id": outcome.run_id,
                "experiment_id": outcome.experiment_id,
                "template_id": spec.template_id,
                "template_version": spec.version,
                "status": outcome.status,
                "started_at": started_at.isoformat(),
                "finished_at": datetime.now(UTC).isoformat(),
                "limits": limits.to_dict(),
                "concurrency": concurrency_snapshot(),
                "egress": egress_policy.report(),
                "egress_hosts_granted": sorted(set(granted_hosts)),
                "sample_size": sample_size,
                "degradations": outcome.degradations,
                "replay_only": bool(replay_only),
                "fixture_source_size": len(fixture_source or {}),
            },
        )
    except OutputLimitExceededError as exc:
        return await _finalize_error(
            outcome, collector, exc, wall_start, persist=persist, session=session
        )

    outcome.finished_at = datetime.now(UTC)
    outcome.duration_ms = int((time.perf_counter() - wall_start) * 1000)
    outcome.concurrency = concurrency_snapshot()
    raw_output_text, truncated = _truncate(
        canonical_json({"records": records}), limits.max_output_bytes
    )
    if truncated:
        outcome.notes.append("raw_output 超过执行器上限，已截断入库；完整输出见工件文件 raw_output.json")
    outcome.artifact_manifest = collector.manifest(
        extra={
            "truncated_raw_output_in_db": truncated,
            "cost_usd": outcome.cost_usd,
        }
    )
    outcome.artifact_path = collector.relative_dir

    if persist:
        await _persist_finish(session, outcome, raw_output_text)

    logger.info(
        "模板 Run 完成 template=%s run=%s status=%s duration_ms=%s samples=%s max_concurrency_observed=%s",
        template_id,
        outcome.run_id,
        outcome.status,
        outcome.duration_ms,
        sample_size,
        outcome.concurrency.get("max_observed"),
    )
    return outcome


# --------------------------------------------------------------------------- #
# 落库
# --------------------------------------------------------------------------- #
async def _session_or_new(session: Any) -> tuple[Any, bool]:
    if session is not None:
        return session, False
    from db.session import AsyncSessionLocal

    if AsyncSessionLocal is None:  # pragma: no cover - 部署期驱动缺失
        raise LimitsConfigError("数据库会话不可用（DATABASE_URL 未就绪）")
    return AsyncSessionLocal(), True


async def _persist_start(
    session: Any,
    *,
    template_id: str,
    config: dict[str, Any],
    stage: str,
    attempt: int,
    experiment_id: int | None,
    run_id: int | None,
) -> tuple[int | None, int]:
    """写 ``experiments`` 与 ``experiment_runs(running)``，返回 ``(experiment_id, run_id)``。"""
    from sqlalchemy import select

    from db.models.pipeline import Experiment, ExperimentRun

    own_session, should_close = await _session_or_new(session)
    if run_id is not None:
        # 复用既有 run（如重跑同一 experiment 的第 N 次 attempt）
        row = (
            await own_session.execute(select(ExperimentRun).where(ExperimentRun.id == int(run_id)))
        ).scalar_one_or_none()
        if row is not None:
            row.status = "running"
            row.attempt = attempt or row.attempt
            row.error = None
            await own_session.commit()
            return (int(row.experiment_id), int(row.id))
    if experiment_id is None:
        experiment = Experiment(template_id=template_id, config=config)
        own_session.add(experiment)
        await own_session.flush()
        experiment_id = int(experiment.id)
    row = ExperimentRun(
        experiment_id=int(experiment_id),
        attempt=attempt,
        executor_task_id=f"inproc-{template_id}-{datetime.now(UTC).strftime('%H%M%S%f')}",
        status="running",
    )
    own_session.add(row)
    await own_session.flush()
    new_run_id = int(row.id)
    await own_session.commit()
    if should_close:
        await own_session.close()
    logger.info(
        "实验已创建 experiment_id=%s run_id=%s template=%s stage=%s",
        experiment_id,
        new_run_id,
        template_id,
        stage,
    )
    return (experiment_id, new_run_id)


async def _persist_finish(session: Any, outcome: RunOutcome, raw_output: str) -> None:
    from sqlalchemy import select

    from db.models.pipeline import ExperimentRun

    own_session, should_close = await _session_or_new(session)
    row = (
        await own_session.execute(select(ExperimentRun).where(ExperimentRun.id == outcome.run_id))
    ).scalar_one_or_none()
    if row is None:  # pragma: no cover - 并发删除
        logger.warning("experiment_runs 行缺失，跳过更新 run_id=%s", outcome.run_id)
        return
    row.status = outcome.status
    row.raw_output = raw_output
    row.artifact_path = outcome.artifact_path
    row.error = outcome.error
    row.duration_ms = outcome.duration_ms
    await own_session.commit()
    if should_close:
        await own_session.close()


async def _finalize_error(
    outcome: RunOutcome,
    collector: ArtifactCollector,
    exc: ExecutorError,
    wall_start: float,
    *,
    persist: bool,
    session: Any,
) -> RunOutcome:
    """失败收尾：状态/错误如实落库 + 失败工件留痕（禁止静默）。"""
    outcome.status = exc.status
    outcome.error = f"{exc.code}: {exc}"
    outcome.error_code = exc.code
    outcome.finished_at = datetime.now(UTC)
    outcome.duration_ms = int((time.perf_counter() - wall_start) * 1000)
    outcome.concurrency = concurrency_snapshot()
    try:
        collector.write_json(
            "error.json",
            {
                "code": exc.code,
                "status": exc.status,
                "message": str(exc),
                "detail": exc.detail,
                "finished_at": outcome.finished_at.isoformat(),
            },
        )
    except OutputLimitExceededError as nested:  # pragma: no cover - 极端情况
        outcome.error = f"{outcome.error}；失败工件写入亦超限：{nested}"
    outcome.artifact_manifest = collector.manifest(
        extra={"error_code": exc.code, "status": outcome.status}
    )
    outcome.artifact_path = collector.relative_dir
    if persist:
        try:
            await _persist_finish(session, outcome, "")
        except Exception:  # noqa: BLE001 - 落库失败不应掩盖原始错误
            logger.exception("失败状态落库异常 run_id=%s", outcome.run_id)
    logger.warning("模板 Run 失败 run=%s code=%s: %s", outcome.run_id, exc.code, exc)
    return outcome


def config_digest(config: dict[str, Any]) -> str:
    """配置摘要（哈希稳定，供 Passport 的 ``template_config`` 溯源）。"""
    return sha256_hex(canonical_json(config))


__all__ = [
    "RunContext",
    "RunOutcome",
    "concurrency_snapshot",
    "config_digest",
    "execute_template",
    "reset_concurrency_stats",
    "run_with_limits",
]
