# Copyright 2026 SciLoop contributors
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
"""LLM 响应录制与回放管理（WP16-T3）。

回放读取端由 WP02 交付（``app.llm.replay`` + ``LLM_REPLAY=1``）。本模块负责**录制端**
与**覆盖率体检**，两者都必须与 ``app.llm.replay`` 的口径**逐字段一致**：

- ``fixture_key`` 一律取 ``LLMResult.prompt_hash`` —— 该值由 ``adapter.chat`` 调用
  :func:`app.llm.replay.compute_prompt_hash` 算出，**不要自己复算**：
  自己拼参数（尤其是 ``temperature`` / ``max_tokens`` 用「解析后的默认值」而非
  调用方传入的原始值）会让哈希永远对不上，回放 100% 未命中。
- ``payload`` 一律用 :func:`app.llm.replay.build_fixture_payload` 构造
  （``fixture_type='llm_response'``），并写入 ``demo_fixtures``。

三种用法
--------
1. **录制**（需真实 LLM 凭据）::

       python -m app.services.demo.replay --record --project-id 1

   它会把 ``app.llm.adapter.chat`` 临时包一层：强制 ``allow_replay=False``（走实时），
   每次成功调用后把响应写进 ``demo_fixtures``；跑完打印每个 ``prompt_hash`` 的落库结果。
   六环节的录制就是「用这个包装跑一次完整流水线」。

2. **体检**（无需凭据）::

       python -m app.services.demo.replay --inventory      # 库存与按环节分布
       python -m app.services.demo.replay --verify         # 逐条校验 payload 结构
       python -m app.services.demo.replay --replay-check --project-id 1
                                                          # 回放模式跑一次，列出**缺失键**

3. 作为库被 ``GET /demo/status`` 调用：:func:`fixture_inventory`。

为什么用「临时包装 adapter.chat」而不是改流水线
---------------------------------------------
``app/services/pipeline/stages/base.py`` 在函数体内 ``from app.llm.adapter import chat``，
因此运行期替换 ``app.llm.adapter.chat`` 能覆盖全部环节调用，且**不修改任何他包文件**。
包装只在录制进程内生效，录制结束即还原。

红线
----
- fixture 里**只存模型返回内容与用量计数**，绝不写 API Key / base_url / 请求头。
- 回放结果 ``is_replay=true`` 由 WP02 标记；本模块不提供任何「把回放洗成实时」的开关。
"""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import sys
from collections.abc import Awaitable, Callable, Iterator, Sequence
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any

from app.llm.replay import PROMPT_HASH_VERSION, build_fixture_payload, payload_to_content
from app.llm.store import FIXTURE_TYPE_LLM_RESPONSE, get_store
from app.llm.types import LLMResult

logger = logging.getLogger("sciloop.wp16.replay")

FIXTURE_TYPE = FIXTURE_TYPE_LLM_RESPONSE
#: 六环节 + 前置环节，用于覆盖率报告的分组
PIPELINE_STAGES: tuple[str, ...] = (
    "survey",
    "plan",
    "plan_review",
    "experiment",
    "writing",
    "review",
)


# --------------------------------------------------------------------------- #
# 会话（直接读 demo_fixtures，只用于库存与校验）
# --------------------------------------------------------------------------- #
def _open_session() -> Any:
    from app.db.session import SessionLocal

    if SessionLocal is None:
        return None
    return SessionLocal()


# --------------------------------------------------------------------------- #
# 录制
# --------------------------------------------------------------------------- #
@dataclass
class RecordOutcome:
    prompt_hash: str | None
    model_id: str | None
    stage: str | None
    purpose: str | None
    chars: int
    written: bool
    error: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "prompt_hash": self.prompt_hash,
            "model_id": self.model_id,
            "stage": self.stage,
            "purpose": self.purpose,
            "chars": self.chars,
            "written": self.written,
            "error": self.error,
        }


def build_note(*, stage: str | None, purpose: str | None, model_ref: str) -> str:
    """fixture 的 ``note``（人读；结构化字段进 ``payload.note_extra``）。

    **不含任何凭据**：只有环节、用途、模型引用与录制时间。
    """
    return (
        f"WP16 录制：stage={stage or '-'} purpose={purpose or '-'} "
        f"model_ref={model_ref} hash_version={PROMPT_HASH_VERSION} "
        f"recorded_at={datetime.now(UTC).isoformat()}"
    )


def build_fixture_record(
    result: LLMResult,
    *,
    stage: str | None = None,
    purpose: str | None = None,
) -> dict[str, Any] | None:
    """把一次实时调用的结果转成 ``demo_fixtures`` 的（key, payload, note）。

    ``prompt_hash`` 缺失时返回 ``None``：那是 adapter 口径变更的信号，
    **必须显式失败**而不是写一个猜出来的 key。
    """
    if not result.prompt_hash:
        return None
    effective_stage = stage or result.stage
    effective_purpose = purpose or result.purpose
    payload = build_fixture_payload(
        content=result.content,
        model=result.model_id,
        prompt_tokens=result.usage.prompt_tokens,
        completion_tokens=result.usage.completion_tokens,
        finish_reason=result.finish_reason,
        note_extra={
            "stage": effective_stage,
            "purpose": effective_purpose,
            "model_ref": result.model_ref,
            "provider": result.provider,
            "recorded_at": datetime.now(UTC).isoformat(),
            "source": "live_recording",
            "cost_usd": result.cost_usd,
            "attempts": result.attempts,
            "duration_ms": result.duration_ms,
        },
    )
    return {
        "fixture_key": result.prompt_hash,
        "payload": payload,
        "note": build_note(
            stage=effective_stage, purpose=effective_purpose, model_ref=result.model_ref
        ),
    }


async def record_result(
    result: LLMResult,
    *,
    stage: str | None = None,
    purpose: str | None = None,
    store: Any | None = None,
) -> RecordOutcome:
    """把一次调用结果写入 ``demo_fixtures``（``fixture_type='llm_response'``）。"""
    record = build_fixture_record(result, stage=stage, purpose=purpose)
    if record is None:
        message = (
            "LLMResult.prompt_hash 为空：无法生成 fixture_key（adapter 口径变更？）"
            "，拒绝猜测哈希"
        )
        logger.error(message)
        return RecordOutcome(None, result.model_id, stage or result.stage, purpose, 0, False, message)
    active_store = store or get_store()
    try:
        await active_store.upsert_fixture(
            FIXTURE_TYPE, record["fixture_key"], record["payload"], note=record["note"]
        )
    except Exception as exc:  # noqa: BLE001 - 单条失败要如实上报
        return RecordOutcome(
            record["fixture_key"],
            result.model_id,
            stage or result.stage,
            purpose,
            len(result.content or ""),
            False,
            f"{type(exc).__name__}: {exc}",
        )
    return RecordOutcome(
        record["fixture_key"],
        result.model_id,
        stage or result.stage,
        purpose,
        len(result.content or ""),
        True,
    )


@dataclass
class Recorder:
    """录制会话：统计每次调用与落库结果（供报告与覆盖率自查）。"""

    stage: str | None = None
    purpose: str | None = None
    store: Any | None = None
    outcomes: list[RecordOutcome] = field(default_factory=list)
    failures: list[dict[str, Any]] = field(default_factory=list)
    started_at: str = field(default_factory=lambda: datetime.now(UTC).isoformat())

    async def on_result(self, result: LLMResult) -> RecordOutcome:
        outcome = await record_result(
            result, stage=self.stage, purpose=self.purpose, store=self.store
        )
        self.outcomes.append(outcome)
        if not outcome.written:
            self.failures.append(outcome.to_dict())
        return outcome

    def report(self) -> dict[str, Any]:
        written = [o for o in self.outcomes if o.written]
        return {
            "job": "record_llm_fixtures",
            "started_at": self.started_at,
            "finished_at": datetime.now(UTC).isoformat(),
            "counts": {
                "calls": len(self.outcomes),
                "written": len(written),
                "failed": len(self.outcomes) - len(written),
                "distinct_prompt_hash": len({o.prompt_hash for o in written if o.prompt_hash}),
            },
            "stages": sorted({o.stage for o in written if o.stage}),
            "records": [o.to_dict() for o in self.outcomes],
            "failures": self.failures,
            "prompt_hash_version": PROMPT_HASH_VERSION,
        }


@contextmanager
def recording_adapter(recorder: Recorder) -> Iterator[Recorder]:
    """在上下文中把 ``app.llm.adapter.chat`` 包一层：实时调用 + 落 fixture。

    包装**强制** ``allow_replay=False``：录制时若命中回放会把回放结果再录一遍，
    形成「回放套回放」的自证循环，破坏 fixture 的真实来源。
    """
    from app.llm import adapter as adapter_module

    original: Callable[..., Awaitable[LLMResult]] = adapter_module.chat

    async def _wrapped(*args: Any, **kwargs: Any) -> LLMResult:
        kwargs["allow_replay"] = False
        result = await original(*args, **kwargs)
        try:
            await recorder.on_result(result)
        except Exception:  # noqa: BLE001 - 录制失败不得让业务调用失败
            logger.exception("录制 fixture 失败（调用结果本身不变）")
        return result

    adapter_module.chat = _wrapped  # type: ignore[assignment]
    logger.info("录制模式已开启：app.llm.adapter.chat 已包装")
    try:
        yield recorder
    finally:
        adapter_module.chat = original  # type: ignore[assignment]
        logger.info("录制模式已关闭：adapter.chat 已还原")


# --------------------------------------------------------------------------- #
# 回放体检
# --------------------------------------------------------------------------- #
async def lookup(prompt_hash: str, *, store: Any | None = None) -> dict[str, Any]:
    """单个 key 的命中体检（复用 WP02 的读取口径）。"""
    from app.llm.replay import load_replay

    hit = await load_replay(prompt_hash, store=store)
    if hit is None:
        return {"prompt_hash": prompt_hash, "hit": False, "content_chars": 0, "model": None}
    return {
        "prompt_hash": prompt_hash,
        "hit": True,
        "content_chars": len(hit.content or ""),
        "model": hit.model_returned,
        "finish_reason": hit.finish_reason,
    }


def fixture_inventory() -> dict[str, Any]:
    """fixture 库存：总数、按环节分布、空内容条数、最新时间、hash 版本分布。"""
    from sqlalchemy import text

    session = _open_session()
    if session is None:
        return {"ok": False, "error": "同步会话工厂不可用"}
    try:
        rows = session.execute(
            text(
                """
                SELECT
                    COUNT(*) AS total,
                    COUNT(*) FILTER (WHERE COALESCE(length(payload->>'content'), 0) = 0)
                        AS empty_content,
                    MAX(created_at) AS latest,
                    MAX(payload->>'prompt_hash_version') AS hash_version
                FROM demo_fixtures
                WHERE fixture_type = :fixture_type
                """
            ),
            {"fixture_type": FIXTURE_TYPE},
        ).mappings().one()
        by_stage = {
            str(stage or "-"): int(count)
            for stage, count in session.execute(
                text(
                    """
                    SELECT payload->'note_extra'->>'stage' AS stage, COUNT(*) AS count
                    FROM demo_fixtures
                    WHERE fixture_type = :fixture_type
                    GROUP BY 1
                    ORDER BY 1
                    """
                ),
                {"fixture_type": FIXTURE_TYPE},
            ).all()
        }
        total_all = int(session.execute(text("SELECT COUNT(*) FROM demo_fixtures")).scalar_one())
        stages_covered = [stage for stage in PIPELINE_STAGES if stage in by_stage]
        return {
            "ok": True,
            "table_rows": total_all,
            "llm_response_total": int(rows["total"] or 0),
            "empty_content": int(rows["empty_content"] or 0),
            "by_stage": by_stage,
            "stages_covered": stages_covered,
            "missing_stages": [stage for stage in PIPELINE_STAGES if stage not in stages_covered],
            "latest_recorded_at": rows["latest"].isoformat() if rows["latest"] else None,
            "prompt_hash_version_in_table": rows["hash_version"],
            "prompt_hash_version_expected": PROMPT_HASH_VERSION,
            "hash_version_consistent": (rows["hash_version"] or PROMPT_HASH_VERSION)
            == PROMPT_HASH_VERSION,
            "ready_for_replay_demo": int(rows["total"] or 0) > 0,
        }
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "error": f"{type(exc).__name__}: {exc}"}
    finally:
        session.close()


def verify_fixtures(*, limit: int | None = None) -> dict[str, Any]:
    """逐条校验 fixture payload 能否被回放读取口径还原成非空文本。"""
    from sqlalchemy import text

    session = _open_session()
    if session is None:
        return {"ok": False, "error": "同步会话工厂不可用"}
    checked: list[dict[str, Any]] = []
    invalid: list[dict[str, Any]] = []
    try:
        statement = text(
            """
            SELECT fixture_key, payload, note, created_at
            FROM demo_fixtures
            WHERE fixture_type = :fixture_type
            ORDER BY id
            """
            + (" LIMIT :limit" if limit else "")
        )
        params: dict[str, Any] = {"fixture_type": FIXTURE_TYPE}
        if limit:
            params["limit"] = int(limit)
        for row in session.execute(statement, params).mappings():
            payload = row["payload"]
            if isinstance(payload, str):
                payload = json.loads(payload)
            content = payload_to_content(payload or {})
            entry = {
                "fixture_key": row["fixture_key"],
                "content_chars": len(content),
                "has_prompt_hash_version": bool((payload or {}).get("prompt_hash_version")),
                "payload_keys": sorted((payload or {}).keys()),
                "created_at": row["created_at"].isoformat() if row["created_at"] else None,
            }
            if not content.strip():
                entry["problem"] = "payload 无法还原出非空 content（回放会返回空串）"
                invalid.append(entry)
            elif not entry["has_prompt_hash_version"]:
                entry["problem"] = "payload 缺少 prompt_hash_version（无法判断哈希口径）"
                invalid.append(entry)
            checked.append(entry)
        return {
            "ok": not invalid and bool(checked),
            "checked": len(checked),
            "invalid_total": len(invalid),
            "invalid": invalid,
            "samples": checked[:5],
            "prompt_hash_version_expected": PROMPT_HASH_VERSION,
            "note": (
                "结构校验通过只说明 payload 可被回放读取；是否真正命中取决于 fixture_key "
                "是否等于运行期 prompt_hash（口径见模块 docstring）"
            ),
        }
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "error": f"{type(exc).__name__}: {exc}"}
    finally:
        session.close()


async def collect_replay_misses(awaitable: Awaitable[Any]) -> tuple[Any, list[dict[str, Any]]]:
    """执行一个 awaitable，并收集期间广播的 ``replay_miss`` 事件（未命中键清单）。"""
    from app.llm.events import default_bus

    misses: list[dict[str, Any]] = []

    def _collector(event_type: str, payload: dict[str, Any]) -> None:
        if event_type == "replay_miss":
            misses.append(dict(payload))

    default_bus.subscribe(_collector)
    try:
        result = await awaitable
    finally:
        default_bus.unsubscribe(_collector)
    return result, misses


def replay_check_sync(*, project_id: int) -> dict[str, Any]:
    """回放模式（``LLM_REPLAY=1``）跑一次流水线，产出缺失键清单。

    这是 T3「未命中时给出明确错误与缺失键列表」与风险清单「演示前抽查覆盖率」的
    可执行答案：**不猜**覆盖率，直接跑一遍看哪些 prompt_hash 没命中。
    """
    from app.services.demo.state import get_demo_state

    state = get_demo_state()
    before = state.replay
    state.apply(replay=True, actor="cli:replay-check")
    try:
        return asyncio.run(_replay_check(project_id))
    finally:
        state.apply(replay=before, actor="cli:replay-check:restore")


async def _replay_check(project_id: int) -> dict[str, Any]:
    try:
        from app.services.pipeline.engine import engine
    except Exception as exc:  # noqa: BLE001 - 六环节未就绪时如实报告
        return {
            "ok": False,
            "status": "blocked",
            "blocker": f"流水线引擎不可用：{type(exc).__name__}: {exc}",
        }

    outcome, misses = await collect_replay_misses(engine.start(project_id, resume=True))
    missing_keys = sorted({str(item.get("prompt_hash")) for item in misses})
    return {
        "ok": not missing_keys,
        "status": "done" if not missing_keys else "incomplete",
        "project_id": project_id,
        "outcome": {
            "reason": getattr(outcome, "reason", None),
            "stage": getattr(outcome, "stage", None),
            "detail": getattr(outcome, "detail", None),
        },
        "replay_misses": len(misses),
        "missing_keys": missing_keys,
        "missing_key_detail": misses,
        "next_step": (
            "缺失键需在录制模式下补录：python -m app.services.demo.replay "
            f"--record --project-id {project_id}"
        )
        if missing_keys
        else "全部命中，可离线演示",
    }


def replay_record_sync(*, project_id: int, resume: bool = True) -> dict[str, Any]:
    """录制模式跑一次流水线（需真实 LLM 凭据），把六环节响应写进 ``demo_fixtures``。"""
    from app.services.demo.state import get_demo_state

    state = get_demo_state()
    before = state.replay
    state.apply(replay=False, actor="cli:record")
    try:
        return asyncio.run(_replay_record(project_id, resume=resume))
    finally:
        state.apply(replay=before, actor="cli:record:restore")


async def _replay_record(project_id: int, *, resume: bool) -> dict[str, Any]:
    recorder = Recorder()
    try:
        from app.services.pipeline.engine import engine
    except Exception as exc:  # noqa: BLE001
        return {
            "ok": False,
            "status": "blocked",
            "blocker": f"流水线引擎不可用：{type(exc).__name__}: {exc}",
            "report": recorder.report(),
        }
    with recording_adapter(recorder):
        try:
            outcome = await engine.start(project_id, resume=resume)
        except Exception as exc:  # noqa: BLE001 - 失败也要交出已录制部分
            report = recorder.report()
            report.update({"ok": False, "status": "failed", "error": f"{type(exc).__name__}: {exc}"})
            return report
    report = recorder.report()
    report.update(
        {
            "ok": report["counts"]["written"] > 0,
            "status": "done" if report["counts"]["written"] > 0 else "incomplete",
            "project_id": project_id,
            "outcome": {
                "reason": getattr(outcome, "reason", None),
                "stage": getattr(outcome, "stage", None),
                "detail": getattr(outcome, "detail", None),
            },
        }
    )
    return report


# --------------------------------------------------------------------------- #
# CLI
# --------------------------------------------------------------------------- #
def _parse_args(argv: Sequence[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="SciLoop LLM 响应录制/回放体检（WP16-T3）")
    parser.add_argument("--inventory", action="store_true", help="打印 fixture 库存")
    parser.add_argument("--verify", action="store_true", help="逐条校验 payload 结构")
    parser.add_argument("--limit", type=int, default=None, help="--verify 的条数上限")
    parser.add_argument("--record", action="store_true", help="录制模式跑一次流水线（需 LLM 凭据）")
    parser.add_argument("--replay-check", action="store_true", help="回放模式跑一次并列出缺失键")
    parser.add_argument("--project-id", type=int, default=None, help="流水线所属 project_id")
    parser.add_argument("--no-resume", action="store_true", help="录制时不续跑（从当前状态继续）")
    parser.add_argument("--out", default=None, help="把报告写到该 JSON 文件")
    return parser.parse_args(argv)


def _emit(payload: dict[str, Any], out: str | None) -> None:
    text = json.dumps(payload, ensure_ascii=False, indent=2)
    print(text)
    if out:
        with open(out, "w", encoding="utf-8") as handle:
            handle.write(text)


def main(argv: Sequence[str] | None = None) -> int:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")
    args = _parse_args(argv)
    if args.record or args.replay_check:
        if args.project_id is None:
            print(
                json.dumps(
                    {"ok": False, "error": "--record / --replay-check 必须给 --project-id"},
                    ensure_ascii=False,
                    indent=2,
                )
            )
            return 2
        payload = (
            replay_record_sync(project_id=args.project_id, resume=not args.no_resume)
            if args.record
            else replay_check_sync(project_id=args.project_id)
        )
        _emit(payload, args.out)
        return 0 if payload.get("ok") else 1
    if args.verify:
        payload = verify_fixtures(limit=args.limit)
        _emit(payload, args.out)
        return 0 if payload.get("ok") else 1
    payload = fixture_inventory()
    _emit(payload, args.out)
    return 0 if payload.get("ok") else 1


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
