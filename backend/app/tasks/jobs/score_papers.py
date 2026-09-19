# Copyright 2026 SciLoop contributors
# Licensed under the Apache License, Version 2.0 (http://www.apache.org/licenses/LICENSE-2.0)
"""批量打分任务（WP04-T7）：为 papers 计算排序四维与影响力三项并落库。

要点
----
- **可断点**：候选集按 ``rank_score IS NULL AND score_coverage IS NULL``（未打分标记）过滤，
  重复执行不会重复计算；``force=True`` 时全量重算。
- **限速**：分批处理并 sleep 节流；LLM 新颖性调用另有单独节流（``SCORE_LLM_THROTTLE_SECONDS``）。
- **不编造**：四维/三项的缺失分项一律写 null，``score_coverage`` 如实记录完整度。
- ``llm_novelty`` 仅作为带不确定性区间的标签落库，**不参与任何加权**。
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import time
from collections.abc import Iterable, Mapping, Sequence
from concurrent.futures import ThreadPoolExecutor
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

# 会话工厂与 feed 端点共用，避免出现两个连接池 / 两套驱动探测逻辑。
# 说明：并行开发期受 owned_paths 限制无法新建共享模块；WP01 的 app.db.session 到位后二者都会复用它。
from app.api.v1.feed import get_session_factory
from app.db.models.paper import Paper, PaperDocument
from app.services.paper_source import influence_score as influence
from app.services.paper_source import ranking

logger = logging.getLogger("sciloop.wp04.score_papers")

WP_ID = "WP04"
DEFAULT_BATCH_SIZE = 200
DEFAULT_THROTTLE_SECONDS = 0.2
DEFAULT_LLM_THROTTLE_SECONDS = 0.5
MAX_RUN_BATCHES = 10_000  # 防御性上限，避免条件写错时死循环


# --------------------------------------------------------------------------------------
# 纯函数：批量算出待落库字段（不访问数据库、不调 LLM）
# --------------------------------------------------------------------------------------
def _nullable(value: Any) -> Any:
    return None if value in ("", [], {}) else value


def _json_safe(payload: Mapping[str, Any]) -> dict[str, Any]:
    """深拷贝为 JSONB 可存结构；出现不可序列化类型时直接抛错（禁止静默转字符串造假）。"""
    return json.loads(json.dumps(payload, ensure_ascii=False))


def paper_payload(paper: Paper) -> dict[str, Any]:
    """ORM 行 -> 纯函数可消费的 Mapping（只取打分需要的字段）。"""
    return {
        "id": paper.id,
        "title": paper.title,
        "abstract": paper.abstract,
        "authors": paper.authors,
        "published_at": paper.published_at,
        "venue": _nullable(paper.venue),
        "venue_source": _nullable(paper.venue_source),
        "venue_level": paper.venue_level,
        "doi": _nullable(paper.doi),
        "citation_count": paper.citation_count,
        "citation_velocity": paper.citation_velocity,
        "code_url": _nullable(paper.code_url),
        "is_parsed": bool(paper.is_parsed),
        "raw": paper.raw,
    }


def document_payload(document: PaperDocument | None) -> dict[str, Any]:
    if document is None:
        return {}
    return {
        "parse_status": document.parse_status,
        "coverage": None if document.coverage is None else float(document.coverage),
        "document_version": document.document_version,
    }


def score_rows(
    papers: Sequence[Mapping[str, Any]],
    *,
    documents: Sequence[Mapping[str, Any] | None] | None = None,
    stars: Sequence[Any] | None = None,
    now: Any = None,
    weights: Mapping[str, float] | str | None = None,
    influence_weights: Mapping[str, float] | str | None = None,
) -> list[dict[str, Any]]:
    """纯函数批量打分，返回每篇论文的待落库字段。

    产出字段：``rank_score`` / ``rank_breakdown`` / ``score_coverage`` /
    ``influence_score`` / ``score_breakdown`` / ``citation_velocity``。
    """
    items = list(papers)
    docs = list(documents) if documents is not None else None
    rank_results = ranking.compute_rank_batch(
        items, documents=docs, now=now, weights=weights
    )
    influence_results = influence.compute_influence_batch(
        items, stars=stars, now=now, weights=influence_weights
    )
    payloads: list[dict[str, Any]] = []
    for paper, rank_result, influence_result in zip(
        items, rank_results, influence_results, strict=True
    ):
        payloads.append(
            {
                "paper_id": paper.get("id"),
                "rank_score": rank_result["rank_score"],
                "rank_breakdown": rank_result["rank_breakdown"],
                "score_coverage": rank_result["score_coverage"],
                "influence_score": influence_result["influence_score"],
                "score_breakdown": influence_result["score_breakdown"],
                "citation_velocity": influence_result["citation_velocity_value"],
                "missing_dimensions": rank_result["missing_dimensions"],
            }
        )
    return payloads


# --------------------------------------------------------------------------------------
# 任务主体
# --------------------------------------------------------------------------------------
def _int_env(name: str, default: int) -> int:
    raw = os.getenv(name)
    if not raw:
        return default
    try:
        return int(raw)
    except ValueError:
        logger.warning("%s 非法: %r，使用默认值 %s", name, raw, default)
        return default


def _float_env(name: str, default: float) -> float:
    raw = os.getenv(name)
    if not raw:
        return default
    try:
        return float(raw)
    except ValueError:
        logger.warning("%s 非法: %r，使用默认值 %s", name, raw, default)
        return default


def select_candidates(
    db: Session, *, after_id: int, size: int, force: bool = False
) -> list[Paper]:
    """取下一批待打分论文。

    断点判据：``rank_score`` 与 ``score_coverage`` 同时为 NULL 视为未打分；
    已打分但不可排序（rank_score 为 NULL 而 coverage 非 NULL）不会被反复重算。
    """
    conditions: list[Any] = [Paper.id > after_id]
    if not force:
        conditions.extend([Paper.rank_score.is_(None), Paper.score_coverage.is_(None)])
    statement = (
        select(Paper)
        .where(*conditions)
        .order_by(Paper.id)
        .limit(size)
    )
    return list(db.execute(statement).scalars().all())


def load_documents(db: Session, paper_ids: Sequence[int]) -> dict[int, PaperDocument]:
    """取每篇论文最新的一条解析记录（WP05 未就绪时为空）。"""
    if not paper_ids:
        return {}
    rows = (
        db.execute(
            select(PaperDocument)
            .where(PaperDocument.paper_id.in_(list(paper_ids)))
            .order_by(PaperDocument.paper_id, PaperDocument.parsed_at.desc())
        )
        .scalars()
        .all()
    )
    latest: dict[int, PaperDocument] = {}
    for document in rows:
        latest.setdefault(document.paper_id, document)
    return latest


def _run_async(coro: Any) -> Any:
    """在同步任务里执行协程；若当前已有事件循环则放到独立线程执行。"""
    try:
        asyncio.get_running_loop()
    except RuntimeError:
        return asyncio.run(coro)
    with ThreadPoolExecutor(max_workers=1) as pool:
        return pool.submit(asyncio.run, coro).result()


def apply_novelty(
    db: Session,
    paper: Paper,
    tag: Mapping[str, Any],
    *,
    overwrite: bool = False,
) -> bool:
    """把 LLM 新颖性标签落到 papers（仅展示字段，不参与任何加权）。"""
    if paper.llm_novelty is not None and not overwrite:
        return False
    stable = tag.get("stable")
    paper.llm_novelty = tag.get("value")
    paper.llm_novelty_low = tag.get("low")
    paper.llm_novelty_high = tag.get("high")
    paper.llm_novelty_note = tag.get("note")
    paper.llm_novelty_stable = None if stable is None else bool(stable)
    return True


def run_score_job(
    *,
    limit: int | None = None,
    force: bool = False,
    batch_size: int | None = None,
    include_novelty: bool | None = None,
    throttle_seconds: float | None = None,
    llm_throttle_seconds: float | None = None,
    now: Any = None,
) -> dict[str, Any]:
    """批量打分主入口（可被 APScheduler 直接调度）。

    :param limit: 本次最多处理的论文数（None = 不限，直到没有候选）
    :param force: True 时重算全部论文（忽略已打分标记）
    :param include_novelty: 是否调用 LLM 生成新颖性标签（默认跟随 LLM_NOVELTY_ENABLED）
    """
    started = time.monotonic()
    size = batch_size or _int_env("SCORE_BATCH_SIZE", DEFAULT_BATCH_SIZE)
    throttle = (
        _float_env("SCORE_THROTTLE_SECONDS", DEFAULT_THROTTLE_SECONDS)
        if throttle_seconds is None
        else throttle_seconds
    )
    llm_throttle = (
        _float_env("SCORE_LLM_THROTTLE_SECONDS", DEFAULT_LLM_THROTTLE_SECONDS)
        if llm_throttle_seconds is None
        else llm_throttle_seconds
    )
    with_novelty = influence.novelty_enabled() if include_novelty is None else bool(include_novelty)

    factory = get_session_factory()
    if factory is None:
        raise RuntimeError("数据库不可用：无法运行打分任务（检查 DATABASE_URL）")

    counters: dict[str, Any] = {
        "wp_id": WP_ID,
        "scanned": 0,
        "scored": 0,
        "novelty_scored": 0,
        "novelty_unavailable": 0,
        "errors": 0,
        "batches": 0,
        "force": bool(force),
        "include_novelty": with_novelty,
    }
    after_id = 0
    with factory() as db:
        for _ in range(MAX_RUN_BATCHES):
            remaining = None if limit is None else max(0, limit - counters["scanned"])
            if remaining == 0:
                break
            fetch_size = size if remaining is None else min(size, remaining)
            candidates = select_candidates(db, after_id=after_id, size=fetch_size, force=force)
            if not candidates:
                break
            after_id = candidates[-1].id
            counters["scanned"] += len(candidates)
            counters["batches"] += 1

            documents = load_documents(db, [paper.id for paper in candidates])
            papers = [paper_payload(paper) for paper in candidates]
            payloads = score_rows(
                papers,
                documents=[document_payload(documents.get(paper["id"])) for paper in papers],
                stars=[influence.stars_from_raw(paper.get("raw")) for paper in papers],
                now=now,
            )
            for paper, payload in zip(candidates, payloads, strict=True):
                if payload["paper_id"] is None:  # pragma: no cover - 防御性
                    continue
                try:
                    paper.rank_score = payload["rank_score"]
                    paper.rank_breakdown = _json_safe(payload["rank_breakdown"])
                    paper.score_coverage = payload["score_coverage"]
                    paper.influence_score = payload["influence_score"]
                    paper.score_breakdown = _json_safe(payload["score_breakdown"])
                    paper.citation_velocity = payload["citation_velocity"]
                    counters["scored"] += 1
                except (TypeError, ValueError) as exc:
                    counters["errors"] += 1
                    logger.warning("论文 %s 打分落库失败: %s", paper.id, exc)
            db.commit()

            if with_novelty:
                for paper in candidates:
                    if paper.llm_novelty is not None and not force:
                        continue
                    tag = _run_async(
                        influence.compute_llm_novelty(paper.title, paper.abstract)
                    )
                    if tag.get("value") is None:
                        counters["novelty_unavailable"] += 1
                        continue
                    if apply_novelty(db, paper, tag, overwrite=force):
                        counters["novelty_scored"] += 1
                    if llm_throttle > 0:
                        time.sleep(llm_throttle)
                db.commit()

            if throttle > 0:
                time.sleep(throttle)
            if remaining is not None and counters["scanned"] >= (limit or 0):
                break

    counters["duration_seconds"] = round(time.monotonic() - started, 3)
    logger.info(
        "wp=%s stage=score_papers scanned=%s scored=%s novelty=%s errors=%s duration=%.3fs",
        WP_ID,
        counters["scanned"],
        counters["scored"],
        counters["novelty_scored"],
        counters["errors"],
        counters["duration_seconds"],
    )
    return counters


def main(argv: Iterable[str] | None = None) -> int:
    """命令行入口：``python -m app.tasks.jobs.score_papers [limit] [--force]``。"""
    args = list(argv) if argv is not None else []
    force = "--force" in args
    limit = None
    for arg in args:
        if arg.isdigit():
            limit = int(arg)
            break
    summary = run_score_job(limit=limit, force=force)
    print(json.dumps(summary, ensure_ascii=False, indent=2))
    return 0 if summary["errors"] == 0 else 1


if __name__ == "__main__":  # pragma: no cover - 运维入口
    raise SystemExit(main())
