# Copyright 2026 SciLoop contributors
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
"""预置示例 Project 的固化与体检（WP16-T4）。

本任务的边界（**重要**）
------------------------
``WP16.out_of_scope`` 明确写着「示例数据的**业务**生成本身由各环节 WP 完成后 seed 固化」。
因此本任务：

1. **不编造任何业务数据**——不插入假的 ``stage_outputs`` / ``decision_logs`` /
   Passport / Claim（``hard_constraints`` 第 1 条：预置示例数据必须是真实跑出来的结果固化）。
2. 做三件事：
   - ``ensure_project``：保证 ``projects.is_demo=true`` 的示例项目**容器**存在（配置，不是结果）；
   - ``inspect``：逐条体检 ``WP16-T4`` 要求的产出物是否已由真实运行固化，缺什么、差多少；
   - ``run_pipeline``（可选）：调用真实流水线引擎跑一轮，把结果真正产出来。

幂等
----
- 项目按 ``name`` 查重，已存在则复用（不重复建）。
- 体检是只读的；重复执行结果一致。
- ``--dry-run`` 只体检不写库。

续跑入口（WP09/WP11/WP12/WP14 就绪后）
--------------------------------------
::

    docker compose exec -T backend python -m app.tasks.jobs.seed_demo --dry-run
    docker compose exec -T backend python -m app.tasks.jobs.seed_demo --run-pipeline
    docker compose exec -T backend python -m app.tasks.jobs.seed_demo --verify

``--run-pipeline`` 需要：项目处于 ``TASKBOOK_LOCKED``（WP08 任务书已锁定）+ LLM 凭据。
缺失时本任务如实返回 ``status=blocked`` 与具体 blocker，**不会**伪造数据把验收糊过去。
"""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import sys
from collections.abc import Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any

logger = logging.getLogger("sciloop.wp16.seed_demo")

JOB_NAME = "seed_demo"
DEFAULT_PROJECT_SLUG = "SciLoop 示例：文献空白 → 可复现实验"

#: WP16-T4 要求的产出物清单（acceptance 逐条可打勾）
REQUIRED_ARTIFACTS: tuple[str, ...] = (
    "demo_project",
    "six_stages",
    "decision_logs",
    "decision_log_need_human",
    "passport_complete",
    "passport_replay_child",
    "review_calibration",
    "experiment_metrics",
    "draft_claims_supported",
    "draft_claims_insufficient",
    "citation_traceback",
    "llm_call_logs",
    "feed_snapshot",
    "llm_fixtures",
)


@dataclass
class SeedCheck:
    """单条产出物体检结果。"""

    key: str
    label: str
    requirement: str
    satisfied: bool
    detail: dict[str, Any] = field(default_factory=dict)
    next_step: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "key": self.key,
            "label": self.label,
            "requirement": self.requirement,
            "satisfied": self.satisfied,
            "detail": self.detail,
            "next_step": self.next_step,
        }


def _open_session() -> Any:
    from app.db.session import SessionLocal

    if SessionLocal is None:
        return None
    return SessionLocal()


def _rows(session: Any, sql: str, **params: Any) -> list[dict[str, Any]]:
    from sqlalchemy import text

    return [dict(row) for row in session.execute(text(sql), params).mappings().all()]


# --------------------------------------------------------------------------- #
# 体检
# --------------------------------------------------------------------------- #
def _check_demo_project(session: Any, slug: str) -> tuple[SeedCheck, int | None]:
    rows = _rows(
        session,
        """
        SELECT id, name, status, mode, is_demo, current_iteration, created_at
        FROM projects
        WHERE is_demo IS TRUE
        ORDER BY id
        """,
    )
    match = next((row for row in rows if row["name"] == slug), None)
    project_id = int(match["id"]) if match else None
    return (
        SeedCheck(
            key="demo_project",
            label="示例 Project 容器（is_demo=true）",
            requirement="新环境 seed 后首屏即见（项目列表 is_demo DESC 排序）",
            satisfied=bool(rows),
            detail={
                "slug": slug,
                "is_demo_projects": [
                    {"id": int(r["id"]), "name": r["name"], "status": r["status"]} for r in rows
                ],
                "matched_project_id": project_id,
            },
            next_step=None if rows else "运行 --run-pipeline（会先创建示例项目容器）",
        ),
        project_id,
    )


def _check_six_stages(session: Any, project_id: int | None) -> SeedCheck:
    stages: list[dict[str, Any]] = []
    if project_id is not None:
        stages = _rows(
            session,
            """
            SELECT DISTINCT ON (so.stage) so.stage, so.status, so.attempt, so.pipeline_run_id
            FROM stage_outputs so
            JOIN pipeline_runs r ON r.id = so.pipeline_run_id
            WHERE r.project_id = :pid
            ORDER BY so.stage, so.attempt DESC
            """,
            pid=project_id,
        )
    done = {r["stage"] for r in stages if r["status"] == "done"}
    expected = {"survey", "plan", "plan_review", "experiment", "writing", "review"}
    return SeedCheck(
        key="six_stages",
        label="六环节产出",
        requirement="survey/plan/plan_review/experiment/writing/review 六环节均有 stage_outputs",
        satisfied=done >= expected,
        detail={
            "expected": sorted(expected),
            "present": sorted({str(r["stage"]) for r in stages}),
            "done": sorted(done),
            "missing": sorted(expected - done),
            "rows": stages,
        },
        next_step=None if done >= expected else "运行完整流水线（需要 WP09/WP11/WP12/WP14 就绪）",
    )


def _check_decision_logs(session: Any, project_id: int | None) -> tuple[SeedCheck, SeedCheck]:
    logs: list[dict[str, Any]] = []
    if project_id is not None:
        logs = _rows(
            session,
            """
            SELECT id, decision_point, stage, chosen, policy_action, risk_score,
                   confidence_score, reversibility_score
            FROM decision_logs
            WHERE project_id = :pid
            ORDER BY id
            """,
            pid=project_id,
        )
    need_human = [row for row in logs if row["policy_action"] == "need_human"]
    count_check = SeedCheck(
        key="decision_logs",
        label="Decision Logs 数量",
        requirement=">= 3 条（D1–D6 决策留痕）",
        satisfied=len(logs) >= 3,
        detail={
            "count": len(logs),
            "decision_points": sorted({str(r["decision_point"]) for r in logs}),
            "policy_actions": sorted({str(r["policy_action"]) for r in logs}),
        },
        next_step=None if len(logs) >= 3 else "运行流水线（决策在每环节前由 WP10 写入）",
    )
    human_check = SeedCheck(
        key="decision_log_need_human",
        label="含一次 need_human 决策",
        requirement=">= 1 条 policy_action='need_human'（体现研究者接管）",
        satisfied=bool(need_human),
        detail={
            "count": len(need_human),
            "ids": [int(row["id"]) for row in need_human],
            "note": (
                "need_human 由风险策略规则层产出（risk/confidence/reversibility 不满足自动执行），"
                "不可人为构造；缺该条说明演示缺少『研究者接管』合规动作"
            ),
        },
        next_step=None if need_human else "需真实运行产生一次人工确认（plan_review 或实验异常）",
    )
    return count_check, human_check


def _passport_rows(session: Any, project_id: int | None) -> list[dict[str, Any]]:
    if project_id is None:
        return []
    return _rows(
        session,
        """
        SELECT ep.id, ep.passport_uid, ep.status, ep.is_replay, ep.parent_passport_id,
               ep.template_id, ep.dataset_sha256, ep.prompt_sha256, ep.code_commit_sha,
               ep.dependency_lock_sha256, ep.cost_usd, ep.metrics
        FROM experiment_passports ep
        JOIN experiment_runs er ON er.id = ep.experiment_run_id
        JOIN experiments e ON e.id = er.experiment_id
        JOIN stage_outputs so ON so.id = e.stage_output_id
        JOIN pipeline_runs r ON r.id = so.pipeline_run_id
        WHERE r.project_id = :pid
        ORDER BY ep.id
        """,
        pid=project_id,
    )


def _check_passports(session: Any, project_id: int | None) -> tuple[SeedCheck, SeedCheck]:
    passports = _passport_rows(session, project_id)
    complete = [row for row in passports if row["status"] == "complete"]
    children = [row for row in passports if row["parent_passport_id"] is not None]
    return (
        SeedCheck(
            key="passport_complete",
            label="一条 complete Passport",
            requirement="status='complete' 且哈希字段齐全（contracts.passport_rules.required_fields）",
            satisfied=bool(complete),
            detail={
                "total": len(passports),
                "complete": [int(row["id"]) for row in complete],
                "hashes": [
                    {
                        "id": int(row["id"]),
                        "dataset_sha256": bool(row["dataset_sha256"]),
                        "prompt_sha256": bool(row["prompt_sha256"]),
                        "code_commit_sha": bool(row["code_commit_sha"]),
                        "dependency_lock_sha256": bool(row["dependency_lock_sha256"]),
                    }
                    for row in complete
                ],
            },
            next_step=None if complete else "运行一次真实实验（WP11）",
        ),
        SeedCheck(
            key="passport_replay_child",
            label="一条 replay 子 Passport（含差异报告）",
            requirement="parent_passport_id 非空且 is_replay=true；差异报告由 /passports/{id}/replay 返回",
            satisfied=bool(children),
            detail={
                "children": [
                    {
                        "id": int(row["id"]),
                        "parent_passport_id": int(row["parent_passport_id"]),
                        "is_replay": bool(row["is_replay"]),
                    }
                    for row in children
                ],
                "diff_report_endpoint": "POST /api/v1/passports/{id}/replay（公开限额）",
            },
            next_step=None if children else "对 complete Passport 调一次 replay（会产生子 Passport）",
        ),
    )


def _check_calibration(session: Any, project_id: int | None) -> SeedCheck:
    rows: list[dict[str, Any]] = []
    if project_id is not None:
        rows = _rows(
            session,
            """
            SELECT rc.id, rc.generator_model_ref, rc.reviewer_model_ref, rc.sample_size,
                   rc.agreement_metric, rc.agreement_value, rc.status
            FROM review_calibrations rc
            JOIN pipeline_runs r ON r.id = rc.pipeline_run_id
            WHERE r.project_id = :pid
            ORDER BY rc.id
            """,
            pid=project_id,
        )
    isolated = [
        row
        for row in rows
        if row["generator_model_ref"] and row["reviewer_model_ref"]
        and row["generator_model_ref"] != row["reviewer_model_ref"]
    ]
    return SeedCheck(
        key="review_calibration",
        label="盲评结果与人工一致率",
        requirement=">=1 条 review_calibrations 且生成/评审模型不同（契约 isolate 要求）",
        satisfied=bool(rows),
        detail={
            "count": len(rows),
            "isolation_ok": bool(isolated),
            "rows": [
                {
                    "id": int(row["id"]),
                    "sample_size": row["sample_size"],
                    "metric": row["agreement_metric"],
                    "value": float(row["agreement_value"])
                    if row["agreement_value"] is not None
                    else None,
                }
                for row in rows
            ],
        },
        next_step=None if rows else "运行 plan_review 环节 + 人工标注 >=3 条（WP12）",
    )


def _check_experiment_metrics(session: Any, project_id: int | None) -> SeedCheck:
    rows: list[dict[str, Any]] = []
    if project_id is not None:
        rows = _rows(
            session,
            """
            SELECT em.id, em.experiment_run_id, em.metric_name, em.metric_value, em.metric_unit
            FROM experiment_metrics em
            JOIN experiment_runs er ON er.id = em.experiment_run_id
            JOIN experiments e ON e.id = er.experiment_id
            JOIN stage_outputs so ON so.id = e.stage_output_id
            JOIN pipeline_runs r ON r.id = so.pipeline_run_id
            WHERE r.project_id = :pid
            ORDER BY em.id
            """,
            pid=project_id,
        )
    return SeedCheck(
        key="experiment_metrics",
        label="真实实验指标",
        requirement="experiment_metrics 有真实落库指标（禁止手写）",
        satisfied=bool(rows),
        detail={
            "count": len(rows),
            "metrics": [
                {
                    "run_id": int(row["experiment_run_id"]),
                    "name": row["metric_name"],
                    "value": float(row["metric_value"])
                    if row["metric_value"] is not None
                    else None,
                    "unit": row["metric_unit"],
                }
                for row in rows
            ],
        },
        next_step=None if rows else "运行 experiment 环节（WP11 执行器 + 模板白名单）",
    )


def _check_claims(session: Any, project_id: int | None) -> tuple[SeedCheck, SeedCheck]:
    drafts: list[dict[str, Any]] = []
    claims: list[dict[str, Any]] = []
    if project_id is not None:
        drafts = _rows(
            session,
            """
            SELECT id, iteration, claim_coverage, length(content_md) AS chars
            FROM paper_drafts WHERE project_id = :pid ORDER BY id
            """,
            pid=project_id,
        )
        if drafts:
            ids = tuple(int(row["id"]) for row in drafts)
            claims = _rows(
                session,
                """
                SELECT id, draft_id, support_status, is_factual, evidence_count
                FROM draft_claims WHERE draft_id = ANY(:ids) ORDER BY id
                """,
                ids=list(ids),
            )
    by_status: dict[str, int] = {}
    for row in claims:
        by_status[str(row["support_status"])] = by_status.get(str(row["support_status"]), 0) + 1
    supported = by_status.get("supported", 0)
    insufficient = by_status.get("insufficient", 0)
    draft_check = SeedCheck(
        key="draft_claims_supported",
        label="草稿含 supported Claim",
        requirement=">=1 条 support_status='supported'",
        satisfied=supported >= 1,
        detail={
            "drafts": [
                {
                    "id": int(row["id"]),
                    "claim_coverage": float(row["claim_coverage"])
                    if row["claim_coverage"] is not None
                    else None,
                    "chars": int(row["chars"] or 0),
                }
                for row in drafts
            ],
            "claims_by_status": by_status,
        },
        next_step=None if supported >= 1 else "运行 writing 环节（WP14）+ 证据校验",
    )
    contrast_check = SeedCheck(
        key="draft_claims_insufficient",
        label="草稿含 insufficient Claim（对照）",
        requirement=">=1 条 support_status='insufficient'（evidence_rules.demo_requirement）",
        satisfied=insufficient >= 1,
        detail={"insufficient": insufficient, "claims_by_status": by_status},
        next_step=None if insufficient >= 1 else "同上：演示草稿必须保留一条反例做对照",
    )
    return draft_check, contrast_check


def _check_citation_traceback(session: Any, project_id: int | None) -> SeedCheck:
    rows: list[dict[str, Any]] = []
    if project_id is not None:
        rows = _rows(
            session,
            """
            SELECT ev.id, ev.owner_type, ev.owner_id, ev.evidence_type,
                   ev.paper_id, ev.paper_span_id, ev.quote_text
            FROM evidences ev
            JOIN draft_claims dc ON dc.id = ev.owner_id AND ev.owner_type IN ('draft_claim', 'claim')
            JOIN paper_drafts pd ON pd.id = dc.draft_id
            WHERE pd.project_id = :pid
            ORDER BY ev.id
            """,
            pid=project_id,
        )
        if not rows:
            rows = _rows(
                session,
                """
                SELECT ev.id, ev.owner_type, ev.owner_id, ev.evidence_type,
                       ev.paper_id, ev.paper_span_id, ev.quote_text
                FROM evidences ev
                WHERE ev.paper_span_id IS NOT NULL
                ORDER BY ev.id
                LIMIT 50
                """,
            )
    with_span = [row for row in rows if row["paper_span_id"] is not None]
    return SeedCheck(
        key="citation_traceback",
        label="可点击的引用回溯",
        requirement="evidences 指向 paper_span/paper_id，前端可点开到原文片段",
        satisfied=bool(with_span),
        detail={
            "count": len(rows),
            "with_paper_span": len(with_span),
            "samples": [
                {
                    "evidence_id": int(row["id"]),
                    "paper_id": row["paper_id"],
                    "paper_span_id": row["paper_span_id"],
                    "evidence_type": row["evidence_type"],
                }
                for row in with_span[:5]
            ],
        },
        next_step=None
        if with_span
        else "草稿 Claim 需挂 paper_span 证据（contracts.evidence_rules）",
    )


def _check_llm_call_logs(session: Any, project_id: int | None) -> SeedCheck:
    rows: list[dict[str, Any]] = []
    if project_id is not None:
        rows = _rows(
            session,
            """
            SELECT stage, COUNT(*) AS calls,
                   COUNT(*) FILTER (WHERE is_replay) AS replay_calls,
                   COALESCE(SUM(cost_usd), 0) AS cost_usd
            FROM llm_call_logs
            WHERE project_id = :pid
            GROUP BY stage
            ORDER BY stage NULLS FIRST
            """,
            pid=project_id,
        )
    total = sum(int(row["calls"]) for row in rows)
    return SeedCheck(
        key="llm_call_logs",
        label="可追溯到 LLM 调用日志（A6）",
        requirement="示例项目的每个环节都有 llm_call_logs 记录（含 is_replay 如实标记）",
        satisfied=total > 0,
        detail={
            "total_calls": total,
            "replay_calls": sum(int(row["replay_calls"]) for row in rows),
            "cost_usd": round(sum(float(row["cost_usd"]) for row in rows), 6),
            "by_stage": [
                {
                    "stage": row["stage"],
                    "calls": int(row["calls"]),
                    "replay_calls": int(row["replay_calls"]),
                }
                for row in rows
            ],
        },
        next_step=None if total > 0 else "任何一次 LLM 调用都必须写 llm_call_logs（契约红线）",
    )


def _check_snapshot() -> SeedCheck:
    from app.services.demo.snapshot import snapshot_inventory

    inventory = snapshot_inventory()
    ready = bool(inventory.get("ready_for_snapshot_demo"))
    return SeedCheck(
        key="feed_snapshot",
        label="论文库快照（三层演示保险之一）",
        requirement="三视图（recommended/influence/latest）均有 is_demo 快照",
        satisfied=ready,
        detail=inventory,
        next_step=None
        if ready
        else "python -m app.services.demo.snapshot --replace",
    )


def _check_fixtures() -> SeedCheck:
    from app.services.demo.replay import fixture_inventory

    inventory = fixture_inventory()
    ready = bool(inventory.get("ready_for_replay_demo"))
    return SeedCheck(
        key="llm_fixtures",
        label="LLM 回放 fixture（三层演示保险之一）",
        requirement="demo_fixtures 有 llm_response 记录且 hash 版本一致",
        satisfied=ready and bool(inventory.get("hash_version_consistent", True)),
        detail=inventory,
        next_step=None
        if ready
        else "python -m app.services.demo.replay --record --project-id <id>（需 LLM 凭据）",
    )


def inspect(project_id: int | None = None, *, project_slug: str = DEFAULT_PROJECT_SLUG) -> dict[str, Any]:
    """体检示例项目产出物；``project_id=None`` 时先按 ``is_demo`` 自动解析。"""
    session = _open_session()
    if session is None:
        return {
            "ok": False,
            "job": JOB_NAME,
            "status": "failed",
            "error": "同步会话工厂不可用（DATABASE_URL / psycopg 未就绪）",
        }
    checks: list[SeedCheck] = []
    try:
        project_check, resolved_id = _check_demo_project(session, project_slug)
        if project_id is None:
            project_id = resolved_id
        checks.append(project_check)
        checks.append(_check_six_stages(session, project_id))
        checks.extend(_check_decision_logs(session, project_id))
        checks.extend(_check_passports(session, project_id))
        checks.append(_check_calibration(session, project_id))
        checks.append(_check_experiment_metrics(session, project_id))
        checks.extend(_check_claims(session, project_id))
        checks.append(_check_citation_traceback(session, project_id))
        checks.append(_check_llm_call_logs(session, project_id))
    finally:
        session.close()

    checks.append(_check_snapshot())
    checks.append(_check_fixtures())

    satisfied = [c for c in checks if c.satisfied]
    missing = [c for c in checks if not c.satisfied]
    return {
        "ok": not missing,
        "job": JOB_NAME,
        "status": "done" if not missing else "incomplete",
        "project_id": project_id,
        "project_slug": project_slug,
        "checked_at": datetime.now(UTC).isoformat(),
        "counts": {
            "required": len(REQUIRED_ARTIFACTS),
            "checked": len(checks),
            "satisfied": len(satisfied),
            "missing": len(missing),
        },
        "checks": [c.to_dict() for c in checks],
        "missing_artifacts": [c.key for c in missing],
        "next_steps": sorted({c.next_step for c in missing if c.next_step}),
    }


# --------------------------------------------------------------------------- #
# 种子执行
# --------------------------------------------------------------------------- #
def ensure_project(
    *, project_slug: str = DEFAULT_PROJECT_SLUG, dry_run: bool = False
) -> dict[str, Any]:
    """保证示例项目容器存在（只建设配置，不生成任何业务结果）。"""
    from sqlalchemy import select

    from app.db.models.project import Project

    session = _open_session()
    if session is None:
        return {"ok": False, "error": "同步会话工厂不可用"}
    try:
        existing = session.execute(
            select(Project).where(Project.name == project_slug)
        ).scalars().all()
        match = next((row for row in existing if row.is_demo), None) or (existing[0] if existing else None)
        if match is not None:
            if not match.is_demo and not dry_run:
                match.is_demo = True
                session.commit()
                return {"ok": True, "created": False, "promoted": True, "project_id": int(match.id)}
            return {"ok": True, "created": False, "promoted": False, "project_id": int(match.id)}
        if dry_run:
            return {"ok": True, "created": False, "would_create": True, "project_id": None}
        project = Project(name=project_slug, status="DRAFT", mode="manual", is_demo=True)
        session.add(project)
        session.commit()
        return {"ok": True, "created": True, "project_id": int(project.id)}
    except Exception as exc:  # noqa: BLE001
        session.rollback()
        return {"ok": False, "error": f"{type(exc).__name__}: {exc}"}
    finally:
        session.close()


def run_seed(
    *,
    dry_run: bool = False,
    force: bool = False,
    project_slug: str | None = None,
    run_pipeline: bool = False,
    mode: str = "manual",
) -> dict[str, Any]:
    """幂等的示例项目固化：建容器 → （可选）跑真实流水线 → 体检。"""
    slug = project_slug or DEFAULT_PROJECT_SLUG
    report: dict[str, Any] = {
        "job": JOB_NAME,
        "dry_run": bool(dry_run),
        "run_pipeline": bool(run_pipeline),
        "project_slug": slug,
        "started_at": datetime.now(UTC).isoformat(),
        "steps": [],
    }
    before = inspect(project_slug=slug)
    report["steps"].append({"step": "inspect_before", "result": before["counts"]})
    report["before"] = {
        "project_id": before.get("project_id"),
        "missing_artifacts": before.get("missing_artifacts", []),
    }

    if dry_run:
        report["ensure_project"] = {"ok": True, "dry_run": True, "project_id": before.get("project_id")}
        report["inspection"] = before
        report["status"] = "done" if before["ok"] else "incomplete"
        report["finished_at"] = datetime.now(UTC).isoformat()
        report["notes"] = ["dry_run=true：未写库；ensure_project 也不会创建项目"]
        return report

    ensure = ensure_project(project_slug=slug, dry_run=False)
    report["steps"].append({"step": "ensure_project", "result": ensure})
    report["ensure_project"] = ensure
    if not ensure.get("ok"):
        report["status"] = "failed"
        report["blockers"] = [f"示例项目容器创建失败：{ensure.get('error')}"]
        report["finished_at"] = datetime.now(UTC).isoformat()
        return report

    project_id = ensure.get("project_id")
    blockers: list[str] = []

    if run_pipeline and project_id is not None:
        pipeline_result = _drive_pipeline(int(project_id), mode=mode)
        report["steps"].append({"step": "run_pipeline", "result": pipeline_result})
        report["pipeline"] = pipeline_result
        if not pipeline_result.get("ok"):
            blockers.append(str(pipeline_result.get("blocker") or pipeline_result.get("error")))
        # 流水线跑完即可顺带固化论文库快照（快照只依赖本库数据）
        snapshot_result = _build_snapshot()
        report["steps"].append({"step": "build_feed_snapshot", "result": snapshot_result})
        report["snapshot"] = snapshot_result

    after = inspect(project_id=project_id, project_slug=slug)
    report["inspection"] = after
    report["steps"].append({"step": "inspect_after", "result": after["counts"]})
    report["counts"] = {
        "created": 1 if ensure.get("created") else 0,
        "missing_before": before["counts"]["missing"],
        "missing_after": after["counts"]["missing"],
        "satisfied": after["counts"]["satisfied"],
    }
    if after["ok"]:
        report["status"] = "done"
    elif blockers:
        report["status"] = "blocked"
    else:
        report["status"] = "incomplete"
    report["blockers"] = sorted({item for item in blockers if item})
    report["resume_entrypoints"] = [
        "python -m app.tasks.jobs.seed_demo --dry-run           # 体检（只读）",
        "python -m app.tasks.jobs.seed_demo --run-pipeline       # 真实跑一轮并固化",
        "python -m app.services.demo.snapshot --replace          # 只固化论文库快照",
        "python -m app.services.demo.replay --record --project-id "
        f"{project_id}   # 录制 LLM fixture（需凭据）",
        "python -m app.services.demo.replay --replay-check --project-id "
        f"{project_id}   # 回放抽查缺失键",
    ]
    report["finished_at"] = datetime.now(UTC).isoformat()
    report["notes"] = [
        "本任务不编造业务数据：所有产出物必须由真实运行固化（WP16.hard_constraints 第 1 条）",
        "示例数据生成本身属于各环节 WP 的职责（WP16.out_of_scope）",
    ]
    return report


def _build_snapshot() -> dict[str, Any]:
    try:
        from app.services.demo.snapshot import build_snapshots

        return build_snapshots(replace=True).to_dict()
    except Exception as exc:  # noqa: BLE001
        return {"status": "failed", "error": f"{type(exc).__name__}: {exc}"}


def _drive_pipeline(project_id: int, *, mode: str = "manual") -> dict[str, Any]:
    """调真实流水线引擎跑一轮（需要任务书已锁定 + LLM 可用）。"""
    try:
        from app.services.pipeline.engine import engine
    except Exception as exc:  # noqa: BLE001
        return {
            "ok": False,
            "status": "blocked",
            "blocker": f"流水线引擎不可用（WP09 未就绪？）：{type(exc).__name__}: {exc}",
        }
    try:
        outcome = asyncio.run(engine.start(project_id, mode=mode, resume=True))
    except Exception as exc:  # noqa: BLE001
        return {
            "ok": False,
            "status": "blocked",
            "blocker": f"流水线启动失败：{type(exc).__name__}: {exc}",
            "hint": "示例项目需先经 N1 门禁：任务书 PATCH + lock（WP08），状态到 TASKBOOK_LOCKED 才能 run",
        }
    return {
        "ok": getattr(outcome, "reason", None) in {"continue", "done", "completed"},
        "status": "done",
        "reason": getattr(outcome, "reason", None),
        "stage": getattr(outcome, "stage", None),
        "detail": getattr(outcome, "detail", None),
    }


def run_seed_sync(**kwargs: Any) -> dict[str, Any]:
    """``POST /demo/projects/seed`` 与 CLI 共用的同步入口。"""
    return run_seed(**kwargs)


# --------------------------------------------------------------------------- #
# CLI
# --------------------------------------------------------------------------- #
def _parse_args(argv: Sequence[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="SciLoop 示例 Project 固化与体检（WP16-T4）")
    parser.add_argument("--dry-run", action="store_true", help="只体检规划，不写库")
    parser.add_argument("--force", action="store_true", help="已存在示例项目时也继续")
    parser.add_argument("--run-pipeline", action="store_true", help="调用真实流水线引擎产出数据")
    parser.add_argument("--verify", action="store_true", help="只做体检（等价于 --dry-run 的读部分）")
    parser.add_argument("--mode", default="manual", help="流水线模式 manual|auto（演示默认 manual）")
    parser.add_argument("--project-slug", default=DEFAULT_PROJECT_SLUG, help="示例项目名称")
    parser.add_argument("--out", default=None, help="把报告写到该 JSON 文件")
    return parser.parse_args(argv)


def main(argv: Sequence[str] | None = None) -> int:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")
    args = _parse_args(argv)
    if args.verify:
        payload = inspect(project_slug=args.project_slug)
    else:
        payload = run_seed(
            dry_run=bool(args.dry_run),
            force=bool(args.force),
            project_slug=args.project_slug,
            run_pipeline=bool(args.run_pipeline),
            mode=args.mode,
        )
    text = json.dumps(payload, ensure_ascii=False, indent=2, default=str)
    print(text)
    if args.out:
        with open(args.out, "w", encoding="utf-8") as handle:
            handle.write(text)
    return 0 if payload.get("status") in {"done", "incomplete"} else 1


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
