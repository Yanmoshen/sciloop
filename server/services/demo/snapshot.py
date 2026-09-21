# Copyright 2026 SciLoop contributors
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
"""论文库快照（WP16-T2）。

做什么
------
把**当前**三视图（``recommended`` / ``influence`` / ``latest``）的排序结果固化成
``paper_feed_snapshots`` 中 ``is_demo=true`` 的记录；``GET /papers/feed?snapshot=demo``
（WP04 已实现读取端）随后即可在**断源**（S2/OpenAlex 无 key、无外网）情况下正常展示。

口径一致是硬要求
----------------
排序口径**唯一来源**是 ``app/api/v1/feed.py`` 的 ``VIEW_ORDER`` / ``order_by_for_view``，
本模块刻意复用该函数而不是复制一份排序逻辑：任何复制都会在 WP04 调整权重或
NULLS LAST 语义后产生静默漂移，出现「快照顺序与实时顺序不一致」的假回放。

幂等与安全
----------
- 默认 ``--replace``：同视图的旧 ``is_demo=true`` 快照先删后写，避免快照无限增长
  且保证「最新一条即当前口径」。
- ``--dry-run``：只统计不写库。
- 只写 ``is_demo=true`` 的行，**绝不触碰**非 demo 快照。

断源可用性的诚实边界
--------------------
快照只固化 ``paper_ids`` 顺序，论文元数据仍从本库 ``papers`` 读取（本库是本地真数据）。
因此快照能覆盖「外部数据源不可用」，不能覆盖「本库被清空」——后者属于部署问题，
按 contracts 的诚实原则如实记录在报错里，不假装成功。

入口
----
::

    python -m services.demo.snapshot --dry-run          # 体检
    python -m services.demo.snapshot --replace          # 固化三视图快照
"""

from __future__ import annotations

import argparse
import json
import logging
import sys
from collections.abc import Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any

from db.models.paper import Paper, PaperFeedSnapshot
from db.session import SessionLocal

logger = logging.getLogger("sciloop.wp16.snapshot")

VIEWS: tuple[str, ...] = ("recommended", "influence", "latest")
DEFAULT_LIMIT = 200

SNAPSHOT_CAVEAT = (
    "快照固化的是三视图的 paper_ids 顺序（口径复用 feed.py 的 order_by_for_view）；"
    "论文元数据仍读本库 papers，因此覆盖『外部源不可用』而非『本库被清空』。"
)


@dataclass
class SnapshotReport:
    """一轮快照生成的可审计报告。"""

    status: str = "running"
    dry_run: bool = False
    replace: bool = False
    started_at: str = field(default_factory=lambda: datetime.now(UTC).isoformat())
    finished_at: str | None = None
    views: list[dict[str, Any]] = field(default_factory=list)
    deleted: int = 0
    written: int = 0
    errors: list[dict[str, Any]] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "job": "build_feed_snapshots",
            "status": self.status,
            "dry_run": self.dry_run,
            "replace": self.replace,
            "started_at": self.started_at,
            "finished_at": self.finished_at,
            "counts": {
                "views": len(self.views),
                "written": self.written,
                "deleted": self.deleted,
                "errors": len(self.errors),
                "paper_ids_total": sum(int(v.get("paper_ids") or 0) for v in self.views),
            },
            "views": self.views,
            "errors": self.errors,
            "notes": [*self.notes, SNAPSHOT_CAVEAT],
        }


# --------------------------------------------------------------------------- #
# 会话
# --------------------------------------------------------------------------- #
def _open_session() -> Any:
    """同步会话（``psycopg``）；工厂不可用返回 ``None``，由调用方如实报错。"""
    if SessionLocal is None:
        return None
    return SessionLocal()


# --------------------------------------------------------------------------- #
# 生成
# --------------------------------------------------------------------------- #
def _ordered_ids(session: Any, view: str, limit: int) -> list[int]:
    """按 feed.py 的视图口径取有序 ``paper_id``（口径唯一来源，禁止复制排序逻辑）。"""
    from sqlalchemy import select

    from api.v1.feed import order_by_for_view

    statement = select(Paper.id).order_by(*order_by_for_view(view)).limit(int(limit))
    return [int(row[0]) for row in session.execute(statement).all()]


def build_snapshots(
    *,
    views: Sequence[str] = VIEWS,
    limit: int = DEFAULT_LIMIT,
    replace: bool = True,
    dry_run: bool = False,
) -> SnapshotReport:
    """把当前三视图结果固化为 ``is_demo=true`` 的 ``paper_feed_snapshots``。"""
    report = SnapshotReport(dry_run=dry_run, replace=replace)
    session = _open_session()
    if session is None:
        report.status = "failed"
        report.finished_at = datetime.now(UTC).isoformat()
        report.errors.append(
            {
                "code": "database_unavailable",
                "message": "同步会话工厂不可用（DATABASE_URL / psycopg 未就绪）",
            }
        )
        return report

    try:
        from sqlalchemy import delete

        for view in views:
            entry: dict[str, Any] = {"view": view, "paper_ids": 0, "written": False}
            try:
                ids = _ordered_ids(session, view, limit)
            except Exception as exc:  # noqa: BLE001 - 单视图失败不阻断其余视图
                report.errors.append(
                    {"view": view, "code": "query_failed", "message": f"{type(exc).__name__}: {exc}"}
                )
                entry["error"] = f"{type(exc).__name__}: {exc}"
                report.views.append(entry)
                continue

            entry["paper_ids"] = len(ids)
            entry["head"] = ids[:5]
            if not ids:
                entry["note"] = "该视图当前无数据，跳过写入（不写空快照，避免展示空列表）"
                report.views.append(entry)
                continue
            if dry_run:
                report.views.append(entry)
                continue

            if replace:
                removed = session.execute(
                    delete(PaperFeedSnapshot).where(
                        PaperFeedSnapshot.view_type == view, PaperFeedSnapshot.is_demo.is_(True)
                    )
                )
                report.deleted += int(removed.rowcount or 0)

            session.add(
                PaperFeedSnapshot(
                    view_type=view,
                    # 显式记录「无筛选」口径：feed.py 的 _pick_snapshot 会据此匹配请求
                    filters={"field": None, "from": None, "to": None, "venue_only": False},
                    paper_ids=ids,
                    is_demo=True,
                )
            )
            session.flush()
            entry["written"] = True
            report.written += 1
            report.views.append(entry)
        session.commit()
    except Exception as exc:  # noqa: BLE001
        session.rollback()
        report.errors.append({"code": "commit_failed", "message": f"{type(exc).__name__}: {exc}"})
        logger.exception("快照写入失败")
    finally:
        session.close()

    report.finished_at = datetime.now(UTC).isoformat()
    if report.errors and report.written == 0:
        report.status = "failed"
    elif report.errors:
        report.status = "partial"
    else:
        report.status = "done"
    if dry_run:
        report.notes.append("dry_run=true：未写库，仅统计当前三视图的有序 paper_id 数量")
    return report


def build_snapshots_sync(**kwargs: Any) -> SnapshotReport:
    """与 :func:`build_snapshots` 同义（供任务/接口调用，命名保持与既有 job 一致）。"""
    return build_snapshots(**kwargs)


# --------------------------------------------------------------------------- #
# 库存查询（供 GET /demo/status 与验收）
# --------------------------------------------------------------------------- #
def snapshot_inventory() -> dict[str, Any]:
    """快照库存：总数、demo 数、按视图分布与最新时间。"""
    from sqlalchemy import func, select

    session = _open_session()
    if session is None:
        return {"ok": False, "error": "同步会话工厂不可用"}
    try:
        total = int(session.execute(select(func.count()).select_from(PaperFeedSnapshot)).scalar_one())
        demo_total = int(
            session.execute(
                select(func.count())
                .select_from(PaperFeedSnapshot)
                .where(PaperFeedSnapshot.is_demo.is_(True))
            ).scalar_one()
        )
        rows = session.execute(
            select(
                PaperFeedSnapshot.view_type,
                func.count().label("rows"),
                func.max(PaperFeedSnapshot.created_at).label("latest"),
            )
            .where(PaperFeedSnapshot.is_demo.is_(True))
            .group_by(PaperFeedSnapshot.view_type)
        ).all()
        by_view = {
            str(view): {
                "rows": int(count),
                "latest_created_at": latest.isoformat() if latest else None,
            }
            for view, count, latest in rows
        }
        latest_any = session.execute(
            select(func.max(PaperFeedSnapshot.created_at)).where(
                PaperFeedSnapshot.is_demo.is_(True)
            )
        ).scalar_one()
        return {
            "ok": True,
            "table_rows": total,
            "is_demo_rows": demo_total,
            "views_covered": sorted(by_view),
            "missing_views": [view for view in VIEWS if view not in by_view],
            "by_view": by_view,
            "latest_is_demo_at": latest_any.isoformat() if latest_any else None,
            "ready_for_snapshot_demo": all(view in by_view for view in VIEWS),
        }
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "error": f"{type(exc).__name__}: {exc}"}
    finally:
        session.close()


def demo_project_inventory() -> dict[str, Any]:
    """示例项目库存（``projects.is_demo IS TRUE``），用于首屏「即见成果」判定。"""
    from sqlalchemy import func, select

    from db.models.project import Project

    session = _open_session()
    if session is None:
        return {"ok": False, "error": "同步会话工厂不可用"}
    try:
        total = int(session.execute(select(func.count()).select_from(Project)).scalar_one())
        demo_rows = session.execute(
            select(Project.id, Project.name, Project.status, Project.mode)
            .where(Project.is_demo.is_(True))
            .order_by(Project.id)
        ).all()
        return {
            "ok": True,
            "projects_total": total,
            "is_demo_total": len(demo_rows),
            "items": [
                {"id": int(pid), "name": name, "status": status, "mode": mode}
                for pid, name, status, mode in demo_rows
            ],
            "ready_for_seed_demo": bool(demo_rows),
        }
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "error": f"{type(exc).__name__}: {exc}"}
    finally:
        session.close()


def list_projects(*, include_all: bool = False) -> dict[str, Any]:
    """项目列表：``is_demo DESC`` 优先（WP16-T4：新环境 seed 后首屏即见示例成果）。"""
    from sqlalchemy import select

    from db.models.project import Project

    session = _open_session()
    if session is None:
        return {"ok": False, "items": [], "total": 0, "error": "同步会话工厂不可用"}
    try:
        statement = select(Project)
        if not include_all:
            statement = statement.where(Project.is_demo.is_(True))
        statement = statement.order_by(Project.is_demo.desc().nulls_last(), Project.id)
        rows = session.execute(statement).scalars().all()
        items = [
            {
                "id": int(row.id),
                "name": row.name,
                "status": row.status,
                "mode": row.mode,
                "is_demo": bool(row.is_demo),
                "current_iteration": row.current_iteration,
                "created_at": row.created_at.isoformat() if row.created_at else None,
            }
            for row in rows
        ]
        return {
            "ok": True,
            "items": items,
            "total": len(items),
            "sort_by": "is_demo DESC NULLS LAST, id ASC",
            "include_all": include_all,
        }
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "items": [], "total": 0, "error": f"{type(exc).__name__}: {exc}"}
    finally:
        session.close()


# --------------------------------------------------------------------------- #
# CLI
# --------------------------------------------------------------------------- #
def _parse_args(argv: Sequence[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="SciLoop 论文库快照生成（WP16-T2）")
    parser.add_argument("--views", default=",".join(VIEWS), help="逗号分隔视图名")
    parser.add_argument("--limit", type=int, default=DEFAULT_LIMIT, help="每个视图固化多少篇")
    parser.add_argument("--dry-run", action="store_true", help="只统计不写库")
    parser.add_argument(
        "--no-replace", action="store_true", help="保留旧 demo 快照（默认先删后写）"
    )
    parser.add_argument("--out", default=None, help="把报告写到该 JSON 文件")
    return parser.parse_args(argv)


def main(argv: Sequence[str] | None = None) -> int:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")
    args = _parse_args(argv)
    views = tuple(item.strip() for item in (args.views or "").split(",") if item.strip())
    report = build_snapshots(
        views=views or VIEWS,
        limit=args.limit,
        replace=not args.no_replace,
        dry_run=bool(args.dry_run),
    )
    payload = json.dumps(report.to_dict(), ensure_ascii=False, indent=2)
    print(payload)
    if args.out:
        with open(args.out, "w", encoding="utf-8") as handle:
            handle.write(payload)
    return 0 if report.status in {"done", "partial"} else 1


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
