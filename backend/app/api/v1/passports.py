# Copyright 2026 SciLoop contributors
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.
"""Experiment Passport 复现 API（WP11-T6/T7）。

=====================  ================================  ==========  ==============================
方法                    路径                              鉴权        说明
=====================  ================================  ==========  ==============================
POST                   ``/passports/{passport_id}/replay``  公开(限额)  用原 Prompt/输入/回放源重算
POST                   ``/passports/{passport_id}/rerun``    Owner      用当前实时模型重跑
GET                    ``/passports/{passport_id}``         公开        读取单条 Passport（见文件尾注）
=====================  ================================  ==========  ==============================

契约红线
--------
- ``POST /passports/{id}/replay`` 是 **public_demo 面唯一允许的匿名写操作**，
  必须限额（``PUBLIC_REPLAY_RATE_LIMIT_PER_HOUR``）且返回 ``is_replay=true`` 与差异报告
- ``POST /passports/{id}/rerun`` 必须 Owner 鉴权（``app/core/security.py: require_owner``）
- 两者都生成**子 Passport**（``parent_passport_id`` 指向父）；
  ``experiment_passports`` 创建后不可 UPDATE
- 回放缺源时**如实失败**（``replay_source_unavailable``），绝不静默改走实时调用
- 桩结果与回放结果都会在 Passport provenance 与返回体 ``degradations`` 中如实标注
"""

from __future__ import annotations

import json
import logging
import time
from collections import deque
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Annotated, Any

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy import select

from app.core.security import require_owner
from app.db.models.pipeline import Experiment, ExperimentRun, PipelineRun, StageOutput
from app.db.session import AsyncSessionLocal

logger = logging.getLogger("sciloop.api.passports")

router = APIRouter(prefix="/passports", tags=["passports"])

OwnerDep = Annotated[None, Depends(require_owner)]

#: 匿名回放速率限制的进程内滑动窗口（单实例部署；见返回体 ``rate_limit``）
_REPLAY_HITS: deque[float] = deque(maxlen=1000)
RATE_WINDOW_SECONDS = 3600.0


def _register_experiment_stage() -> str:
    """注册 ``experiment`` 环节（幂等；与 experiments 路由同一注入点，双保险）。"""
    try:
        from app.services.pipeline.stages.experiment import register

        register()
        return "registered"
    except Exception as exc:  # noqa: BLE001
        logger.warning("experiment 环节注册失败（passports 模块）：%s", exc, exc_info=True)
        return f"failed:{type(exc).__name__}:{exc}"


STAGE_REGISTRATION = _register_experiment_stage()


# --------------------------------------------------------------------------- #
# 限流
# --------------------------------------------------------------------------- #
def _rate_limit() -> dict[str, Any]:
    """匿名回放限额（``PUBLIC_REPLAY_RATE_LIMIT_PER_HOUR``，默认 10）。"""
    from app.core.config import get_settings

    try:
        limit = int(getattr(get_settings(), "public_replay_rate_limit_per_hour", 10) or 10)
    except Exception:  # noqa: BLE001 - 配置不可用时取保守值
        limit = 10
    return {"limit": max(1, limit), "window_seconds": int(RATE_WINDOW_SECONDS)}


def _consume_replay_quota() -> dict[str, Any]:
    """尝试占用一次匿名回放名额；超限抛 429（``rate_limited``）。"""
    now = time.monotonic()
    while _REPLAY_HITS and now - _REPLAY_HITS[0] >= RATE_WINDOW_SECONDS:
        _REPLAY_HITS.popleft()
    config = _rate_limit()
    limit = int(config["limit"])
    if len(_REPLAY_HITS) >= limit:
        raise HTTPException(
            status_code=status.HTTP_429_TOO_MANY_REQUESTS,
            detail={
                "code": "rate_limited",
                "message": (
                    f"匿名回放已达限额（{limit} 次/{config['window_seconds']}s）："
                    "请稍后再试，或使用 Owner 面 rerun"
                ),
                "detail": {
                    "limit": limit,
                    "used": len(_REPLAY_HITS),
                    "window_seconds": config["window_seconds"],
                },
            },
        )
    _REPLAY_HITS.append(now)
    return {
        "limit": limit,
        "used": len(_REPLAY_HITS),
        "remaining": max(0, limit - len(_REPLAY_HITS)),
        "window_seconds": config["window_seconds"],
    }


# --------------------------------------------------------------------------- #
# 错误体
# --------------------------------------------------------------------------- #
def _error(code: str, message: str, http_status: int, detail: Any = None) -> HTTPException:
    return HTTPException(
        status_code=http_status, detail={"code": code, "message": message, "detail": detail}
    )


def _wrap(exc: Exception) -> HTTPException:
    """Passport / 执行器异常 → 契约错误体。"""
    from app.executor.errors import ExecutorError
    from app.services.experiment import passport as P

    if isinstance(exc, P.PassportNotFoundError):
        return _error("passport_not_found", str(exc), status.HTTP_404_NOT_FOUND, exc.detail)
    if isinstance(exc, P.PassportImmutableError):
        return _error("passport_immutable", str(exc), status.HTTP_409_CONFLICT, exc.detail)
    if isinstance(exc, P.PassportError):
        return _error(exc.code, str(exc), status.HTTP_422_UNPROCESSABLE_ENTITY, exc.detail)
    if isinstance(exc, ExecutorError):
        http_status = (
            status.HTTP_409_CONFLICT
            if exc.code in {"egress_denied", "model_unavailable", "template_rejected"}
            else status.HTTP_422_UNPROCESSABLE_ENTITY
        )
        return _error(exc.code, str(exc), http_status, exc.detail)
    logger.exception("Passport 复现接口未预期异常")
    return _error("internal_error", str(exc), status.HTTP_500_INTERNAL_SERVER_ERROR, None)


# --------------------------------------------------------------------------- #
# 父 Passport → 复现配置
# --------------------------------------------------------------------------- #
async def _resolve_project_id(run_id: int, *, session: Any) -> int | None:
    stmt = (
        select(PipelineRun.project_id)
        .select_from(ExperimentRun)
        .join(Experiment, ExperimentRun.experiment_id == Experiment.id)
        .join(StageOutput, Experiment.stage_output_id == StageOutput.id)
        .join(PipelineRun, StageOutput.pipeline_run_id == PipelineRun.id)
        .where(ExperimentRun.id == int(run_id))
    )
    return (await session.execute(stmt)).scalar_one_or_none()


def _records_of(raw_output: str | None) -> list[dict[str, Any]]:
    """解析 ``experiment_runs.raw_output``（``{"records":[...]}``），失败即回空。"""
    text = str(raw_output or "").strip()
    if not text:
        return []
    try:
        payload = json.loads(text)
    except json.JSONDecodeError:
        logger.warning("raw_output 不是合法 JSON，无法用于回放（如实失败，不猜测）")
        return []
    records = payload.get("records") if isinstance(payload, Mapping) else payload
    return [dict(item) for item in records or [] if isinstance(item, Mapping)]


def _read_artifact(path: str | None) -> list[dict[str, Any]]:
    """工件文件兜底（``<artifact_root>/<run>/output/raw_output.json``）。"""
    if not path:
        return []
    from app.executor.limits import get_limits

    root = Path(get_limits().artifact_root)
    target = Path(str(path))
    candidate = target if target.is_absolute() else root / target
    if candidate.is_dir():
        candidate = candidate / "raw_output.json"
    if not candidate.is_file():
        return []
    try:
        payload = json.loads(candidate.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:  # noqa: BLE001
        logger.warning("读取回放工件失败 %s：%s", candidate, exc)
        return []
    records = payload.get("records") if isinstance(payload, Mapping) else payload
    return [dict(item) for item in records or [] if isinstance(item, Mapping)]


def _fixture_source(records: Sequence[Mapping[str, Any]]) -> dict[str, Any]:
    """逐样本原始输出 → 回放源 ``{group|sample_id: record}``。"""
    from app.services.experiment.templates.common import fixture_key

    source: dict[str, Any] = {}
    for record in records:
        group = str(record.get("group") or record.get("model_ref") or "")
        sample_id = str(record.get("sample_id") or "")
        if not group or not sample_id:
            continue
        source[fixture_key(group, sample_id)] = dict(record)
    return source


def _reproduce_config(parent: Mapping[str, Any]) -> tuple[dict[str, Any], list[str]]:
    """用父 Passport 的**原始输入**构造复现配置（样本 + 参数完全相同）。

    优先走「内置数据集切片」：父 Passport 记录了 ``template_config.dataset_source_file``
    与 ``params.sample_offset`` 时，用同一数据集重新取同一段样本 —— 这样
    ``dataset_name`` / ``dataset_version`` / ``template_config`` 与父完全一致，
    差异报告里的 ``config_changes`` 才真正反映「有没有改配置」。

    只有当内置切片按 id 比对**与父不一致**（数据集被改动过）时，才退回显式 ``samples``，
    并如实记一条 notes（此时 dataset 身份会记为 ``caller_supplied``）。
    """
    manifest = dict(parent.get("sample_manifest") or {})
    samples = [dict(item) for item in manifest.get("samples") or [] if isinstance(item, Mapping)]
    template_config = dict(parent.get("template_config") or {})
    params = dict(template_config.get("params") or {})
    params = {key: value for key, value in params.items() if value is not None}
    config: dict[str, Any] = {
        "template_id": str(parent.get("template_id") or ""),
        "sample_size": len(samples),
        "params": params,
    }
    notes: list[str] = []
    source_file = template_config.get("dataset_source_file")
    dataset_name = str(parent.get("dataset_name") or "")
    if source_file and dataset_name and dataset_name != "caller_supplied":
        expected = [str(item.get("id")) for item in samples]
        got: list[str] = []
        try:
            from app.services.experiment import registry as registry_mod

            items = registry_mod.dataset_items(str(source_file))
            offset = int(params.get("sample_offset") or 0)
            got = [str(item.get("id")) for item in items[offset : offset + len(samples)]]
        except Exception as exc:  # noqa: BLE001 - 数据集不可读取时如实退回显式样本
            notes.append(f"内置数据集 {source_file} 不可读取（{type(exc).__name__}: {exc}）")
        if got and got == expected:
            config["dataset"] = str(source_file)
            notes.append(
                f"复现走内置数据集切片（dataset={source_file}, offset={params.get('sample_offset') or 0}, "
                f"sample_size={len(samples)}），样本 id 与父 Passport 完全一致"
            )
            return config, notes
        if got:
            notes.append(
                "内置数据集的同偏移切片与父 Passport 的 sample_ids 不一致（数据集已被改动）："
                "改用显式 samples 复现，dataset_name 会记为 caller_supplied，差异报告的 config_changes 会如实列出"
            )
    config["samples"] = samples
    if not config.get("dataset"):
        notes.append(
            "父 Passport 未记录可复算的内置数据集来源：改用显式 samples 复现"
            "（dataset 身份记为 caller_supplied，禁止凭猜测重造输入）"
        )
    return config, notes


# --------------------------------------------------------------------------- #
# 复现主流程
# --------------------------------------------------------------------------- #
async def _reproduce(
    passport_id: int,
    *,
    replay: bool,
    note: str,
) -> dict[str, Any]:
    from app.executor.runner import execute_template
    from app.services.experiment import diff as diff_mod
    from app.services.experiment import metrics as M
    from app.services.experiment import passport as P

    parent = await P.get_passport(int(passport_id))
    config, config_notes = _reproduce_config(parent)
    if not config["template_id"]:
        raise _error(
            "invalid_parent_passport",
            f"父 Passport {passport_id} 缺少 template_id，无法复现",
            status.HTTP_422_UNPROCESSABLE_ENTITY,
            {"passport_id": int(passport_id)},
        )
    if int(config["sample_size"] or 0) <= 0 or not (
        config.get("dataset") or config.get("samples")
    ):
        raise _error(
            "invalid_parent_passport",
            f"父 Passport {passport_id} 的 sample_manifest 为空，无法复现（禁止凭猜测重造输入）",
            status.HTTP_422_UNPROCESSABLE_ENTITY,
            {"passport_id": int(passport_id)},
        )

    degradations: list[str] = []
    notes: list[str] = [note, *config_notes]
    fixture_source: dict[str, Any] | None = None
    project_id: int | None = None
    async with AsyncSessionLocal() as session:
        parent_run = (
            await session.execute(
                select(ExperimentRun).where(ExperimentRun.id == int(parent["experiment_run_id"]))
            )
        ).scalar_one_or_none()
        project_id = await _resolve_project_id(int(parent["experiment_run_id"]), session=session)
    if parent_run is None:
        raise _error(
            "run_not_found",
            f"父 Passport 指向的 Run {parent.get('experiment_run_id')} 不存在",
            status.HTTP_409_CONFLICT,
            {"passport_id": int(passport_id)},
        )

    if replay:
        records = _records_of(parent_run.raw_output) or _read_artifact(parent_run.artifact_path)
        fixture_source = _fixture_source(records)
        if not fixture_source:
            raise _error(
                "replay_source_unavailable",
                (
                    f"Passport {passport_id} 对应的 Run {parent_run.id} 没有可用的原始输出；"
                    "回放缺源时**不会**改走实时调用（契约：禁止把非回放结果标成 is_replay）"
                ),
                status.HTTP_409_CONFLICT,
                {"run_id": int(parent_run.id), "artifact_path": parent_run.artifact_path},
            )
        notes.append(f"回放源：父 Run 原始输出 {len(fixture_source)} 条")

    exec_config: dict[str, Any] = {
        "sample_size": config["sample_size"],
        "params": config["params"],
    }
    if config.get("dataset"):
        exec_config["dataset"] = config["dataset"]
    else:
        exec_config["samples"] = list(config.get("samples") or [])

    outcome = await execute_template(
        config["template_id"],
        exec_config,
        project_id=project_id,
        stage="experiment",
        attempt=1,
        replay_only=bool(replay),
        fixture_source=fixture_source,
    )
    degradations.extend(list(outcome.degradations or []))

    metric_meta = {
        "template_id": config["template_id"],
        "run_id": outcome.run_id,
        "experiment_id": outcome.experiment_id,
        "sample_size": config["sample_size"],
        "parent_passport_id": int(passport_id),
        "action": "replay" if replay else "rerun",
        "is_replay": outcome.is_replay,
        "status": outcome.status,
    }
    metric_rows = 0
    if outcome.metrics:
        persisted = await M.persist_run_metrics(
            run_id=outcome.run_id, metrics=outcome.metrics, meta=metric_meta
        )
        metric_rows = int(persisted.get("rows") or 0)

    child = await P.create_passport(
        outcome,
        parent_passport_id=int(passport_id),
        note=(
            f"{note}；父 Passport={passport_id}（重跑生成新记录，禁止 UPDATE 父凭证）"
            + ("" if outcome.ok else f"；本 Run 未成功：status={outcome.status}")
        ),
        metric_meta=metric_meta,
    )
    report = diff_mod.diff(parent, child)

    if not outcome.ok:
        level = "L1" if outcome.status == "timeout" else "L2"
        notes.append(f"复现 Run 未成功（status={outcome.status}, error={outcome.error}, 建议分级 {level}）")
    if child.get("is_replay") != bool(replay):
        # 理论不可达（回放源命中必然 is_replay=true）；这里显式暴露不一致而不是掩盖
        degradations.append(
            f"is_replay 与请求不一致：请求 replay={replay}，Passport={child.get('is_replay')}"
        )

    logger.info(
        "Passport %s 完成 action=%s child=%s status=%s is_replay=%s verdict=%s",
        passport_id,
        "replay" if replay else "rerun",
        child.get("id"),
        child.get("status"),
        child.get("is_replay"),
        report.get("verdict"),
    )
    return {
        "action": "replay" if replay else "rerun",
        "is_replay": bool(child.get("is_replay")),
        "parent_passport_id": int(passport_id),
        "passport": child,
        "child": child,
        "diff": report,
        "run": outcome.to_dict(),
        "metrics": dict(outcome.metrics or {}),
        "metric_rows": metric_rows,
        "artifact_path": outcome.artifact_path,
        "notes": notes + list(outcome.notes or []),
        "degradations": degradations,
        "compliance": {
            "disclaimer": "本内容由 AI 辅助生成，需研究者自行核验",
            "is_replay": bool(child.get("is_replay")),
            "note": (
                "回放结果来自父 Run 的原始输出，**不是**实时模型结果"
                if replay
                else "重跑使用当前可用模型（无凭据时为本地桩）重新执行，非回放"
            ),
        },
    }


# --------------------------------------------------------------------------- #
# 端点
# --------------------------------------------------------------------------- #
@router.post("/{passport_id}/replay", summary="匿名回放（public_demo 唯一允许的写操作，限额）")
async def replay_passport(passport_id: int) -> dict[str, Any]:
    """用原 Prompt / 输入 / 回放源重算，生成子 Passport 并返回差异报告（``is_replay=true``）。"""
    quota = _consume_replay_quota()
    try:
        payload = await _reproduce(
            passport_id, replay=True, note="匿名回放（public_demo，限额端点）"
        )
    except HTTPException:
        raise
    except Exception as exc:  # noqa: BLE001
        raise _wrap(exc) from exc
    payload["rate_limit"] = quota
    return payload


@router.post("/{passport_id}/rerun", summary="Owner 重跑（当前实时模型）")
async def rerun_passport(passport_id: int, _owner: OwnerDep) -> dict[str, Any]:
    """Owner 面用当前实时模型重跑同一输入，生成子 Passport 与差异报告。"""
    try:
        return await _reproduce(
            passport_id, replay=False, note="Owner 重跑（使用当前实时模型/路由）"
        )
    except HTTPException:
        raise
    except Exception as exc:  # noqa: BLE001
        raise _wrap(exc) from exc


@router.get("/{passport_id}", summary="读取单条 Passport（读操作，公开）")
async def read_passport(passport_id: int) -> dict[str, Any]:
    """读取单条 Passport（含 ``missing_fields``；供 WP15 看板一跳展示）。

    契约补充项：``contracts.api_contract.key_endpoints.passports`` 只定义了 replay/rerun，
    本 GET 为 WP15 集成所需的只读端点，已记入 ``_progress.json`` 的
    ``contract_changes_requested``（不改变既有契约语义）。
    """
    from app.services.experiment import passport as P

    try:
        return await P.get_passport(int(passport_id))
    except Exception as exc:  # noqa: BLE001
        raise _wrap(exc) from exc


__all__ = ["RATE_WINDOW_SECONDS", "STAGE_REGISTRATION", "router"]
