# Copyright 2026 SciLoop contributors
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
"""项目容器 API（WP09；``contracts.api_contract.key_endpoints.projects`` / 附录 B.5）。

======== ===================  ==========  ================================================
方法      路径                  鉴权        说明
======== ===================  ==========  ================================================
GET      ``/projects``          公开        项目列表（``is_demo`` 排序优先，分页）
POST     ``/projects``          Owner       创建项目
GET      ``/projects/{id}``    公开        项目详情（含迭代历史：pipeline_runs 逐轮）
======== ===================  ==========  ================================================

列表响应形状遵循 ``contracts.api_contract.pagination``：``{items,total,page,page_size}``；
错误形状遵循 ``contracts.api_contract.error_shape``：``{code,message,detail}``。

本模块**由 ``main.ROUTER_REGISTRY`` 第 9 项登记，模块出现即自动挂载**，
因此本文件不得改动 ``main.py``。
"""

from __future__ import annotations

import logging
from typing import Annotated, Any

from fastapi import APIRouter, Body, Depends, HTTPException, Query, status
from sqlalchemy import func, select

from core.security import require_owner
from db.models import Idea, PipelineRun, Project, Taskbook
from db.session import AsyncSessionLocal
from services.naming import readable_name
from services.pipeline.stages import STAGE_ORDER

logger = logging.getLogger("sciloop.api.projects")

router = APIRouter(prefix="/projects", tags=["projects"])

OwnerDep = Annotated[None, Depends(require_owner)]

#: 项目状态取值（``contracts.enums.project_status``）
PROJECT_STATUSES: tuple[str, ...] = (
    "DRAFT",
    "TASKBOOK_LOCKED",
    "RUNNING",
    "RISK_EVALUATING",
    "WAIT_HUMAN",
    "CIRCUIT_BREAK",
    "REVIEWING",
    "DONE",
    "ABORTED",
)

MODES: tuple[str, ...] = ("manual", "auto")

MAX_PAGE_SIZE = 100


def _session_factory() -> Any:
    if AsyncSessionLocal is None:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail={"code": "db_unavailable", "message": "数据库会话不可用", "detail": None},
        )
    return AsyncSessionLocal


def _error(http_status: int, code: str, message: str, detail: Any = None) -> HTTPException:
    return HTTPException(
        status_code=http_status,
        detail={"code": code, "message": message, "detail": detail},
    )


def _iso(value: Any) -> str | None:
    return value.isoformat() if value is not None else None


def _project_dict(project: Project, *, include_settings: bool = True) -> dict[str, Any]:
    """项目基础字段（``is_demo`` / ``taskbook_id`` 如实返回，缺值置 null 不编造）。

    ``workspace_dir`` = 这个项目在研究者电脑上的工作目录（没定过就是 null）；
    ``workspace_note`` = 建目录时如实留下的说明（例如"执行器没连上，目录没建成"）。
    """
    payload: dict[str, Any] = {
        "id": int(project.id),
        "name": project.name,
        "status": project.status,
        "mode": project.mode,
        "current_iteration": int(project.current_iteration or 0),
        "is_demo": bool(project.is_demo),
        "archived": bool(project.archived),
        "idea_id": int(project.idea_id) if project.idea_id is not None else None,
        "taskbook_id": int(project.taskbook_id) if project.taskbook_id is not None else None,
        "created_at": _iso(project.created_at),
        "updated_at": _iso(project.updated_at),
    }
    if include_settings:
        payload["settings"] = project.settings
    # 工作目录单独提出来给界面用（存在 settings 里，不加迁移）
    stored_settings = project.settings if isinstance(project.settings, dict) else {}
    payload["workspace_dir"] = stored_settings.get("workspace_dir")
    payload["workspace_note"] = stored_settings.get("workspace_note")
    return payload


def _run_dict(run: PipelineRun) -> dict[str, Any]:
    return {
        "pipeline_run_id": int(run.id),
        "project_id": int(run.project_id),
        "iteration": int(run.iteration),
        "mode": run.mode,
        "status": run.status,
        "stop_reason": run.stop_reason,
        "total_cost_usd": float(run.total_cost_usd or 0),
        "started_at": _iso(run.started_at),
        "finished_at": _iso(run.finished_at),
        "created_at": _iso(run.created_at),
    }


# --------------------------------------------------------------------------- #
# 读操作（公开）
# --------------------------------------------------------------------------- #
@router.get("", summary="项目列表（is_demo 优先，公开只读）")
async def list_projects(
    page: Annotated[int, Query(ge=1)] = 1,
    page_size: Annotated[int, Query(ge=1, le=MAX_PAGE_SIZE)] = 20,
    status_filter: Annotated[str | None, Query(alias="status", max_length=24)] = None,
    is_demo: Annotated[bool | None, Query()] = None,
    archived: Annotated[bool | None, Query()] = False,
) -> dict[str, Any]:
    """项目列表：``is_demo DESC NULLS LAST, id ASC``（附录 B.5「``is_demo`` 排序优先」）。

    ``archived`` 默认 ``False``：左栏「项目」分组只列未归档项目；传 ``true`` 取「已归档」，
    传 ``null``（省略时无法表达，故用查询串 ``archived=`` 之外的值）不做过滤。
    """
    if status_filter is not None and status_filter not in PROJECT_STATUSES:
        raise _error(
            status.HTTP_422_UNPROCESSABLE_ENTITY,
            "validation_error",
            f"status 非法：{status_filter}",
            {"allowed": list(PROJECT_STATUSES)},
        )

    filters = []
    if status_filter is not None:
        filters.append(Project.status == status_filter)
    if is_demo is not None:
        filters.append(Project.is_demo.is_(is_demo))
    if archived is not None:
        filters.append(Project.archived.is_(archived))

    async with _session_factory()() as session:
        total = (
            await session.execute(select(func.count()).select_from(Project).where(*filters))
        ).scalar_one()
        rows = (
            (
                await session.execute(
                    select(Project)
                    .where(*filters)
                    .order_by(Project.is_demo.desc().nulls_last(), Project.id.asc())
                    .offset((page - 1) * page_size)
                    .limit(page_size)
                )
            )
            .scalars()
            .all()
        )
        items = [_project_dict(row, include_settings=False) for row in rows]

    return {
        "items": items,
        "total": int(total or 0),
        "page": page,
        "page_size": page_size,
        "sort_by": "is_demo DESC NULLS LAST, id ASC",
    }


@router.get("/{project_id}", summary="项目详情（含迭代历史，公开只读）")
async def get_project(project_id: int) -> dict[str, Any]:
    """项目详情 + ``pipeline_runs`` 迭代历史（按 iteration 升序，如实返回 ``stop_reason``）。"""
    async with _session_factory()() as session:
        project = (
            await session.execute(select(Project).where(Project.id == project_id))
        ).scalar_one_or_none()
        if project is None:
            raise _error(
                status.HTTP_404_NOT_FOUND, "project_not_found", f"project {project_id} 不存在"
            )
        runs = (
            (
                await session.execute(
                    select(PipelineRun)
                    .where(PipelineRun.project_id == project_id)
                    .order_by(PipelineRun.iteration.asc(), PipelineRun.id.asc())
                )
            )
            .scalars()
            .all()
        )
        payload = _project_dict(project)
        payload["stage_order"] = list(STAGE_ORDER)
        payload["iteration_history"] = [_run_dict(run) for run in runs]
        payload["run_count"] = len(runs)
        latest = runs[-1] if runs else None
        payload["latest_run"] = _run_dict(latest) if latest is not None else None

    return payload


# --------------------------------------------------------------------------- #
# 写操作（Owner）
# --------------------------------------------------------------------------- #
@router.post("", status_code=status.HTTP_201_CREATED, summary="创建项目（Owner）")
async def create_project(
    _owner: OwnerDep,
    payload: Annotated[dict[str, Any], Body(...)],
) -> dict[str, Any]:
    """创建项目（``status=DRAFT``，``current_iteration=0``）。

    允许字段：``name``（必填）、``mode``（``manual``/``auto``，默认 ``manual``）、
    ``idea_id`` / ``taskbook_id``（可选外键）、``settings``（JSONB）、``is_demo``、
    ``workspace_dir``（可选：这个项目在研究者电脑上的工作目录；不填则使用
    ``knowledge-base/projects/<项目名>``，同名目录用项目号消歧）。目录会真的创建。
    """
    name = str(payload.get("name") or "").strip()
    if not name:
        raise _error(status.HTTP_422_UNPROCESSABLE_ENTITY, "validation_error", "name 必填", payload)
    if len(name) > 200:
        raise _error(
            status.HTTP_422_UNPROCESSABLE_ENTITY,
            "validation_error",
            "name 长度不得超过 200",
            {"length": len(name)},
        )

    mode = str(payload.get("mode") or "manual")
    if mode not in MODES:
        raise _error(
            status.HTTP_422_UNPROCESSABLE_ENTITY,
            "validation_error",
            f"mode 必须为 {list(MODES)} 之一",
            {"mode": mode},
        )

    settings = payload.get("settings")
    if settings is not None and not isinstance(settings, dict):
        raise _error(
            status.HTTP_422_UNPROCESSABLE_ENTITY,
            "validation_error",
            "settings 必须为 JSON 对象",
            {"settings": settings},
        )

    idea_id = payload.get("idea_id")
    taskbook_id = payload.get("taskbook_id")
    for label, value in (("idea_id", idea_id), ("taskbook_id", taskbook_id)):
        if value is not None and not isinstance(value, int):
            raise _error(
                status.HTTP_422_UNPROCESSABLE_ENTITY,
                "validation_error",
                f"{label} 必须为整数或 null",
                {label: value},
            )

    async with _session_factory()() as session:
        # 外键存在性校验：宁可 422 也不让 IntegrityError 变成 500
        if idea_id is not None:
            exists = (
                await session.execute(select(Idea.id).where(Idea.id == int(idea_id)))
            ).scalar_one_or_none()
            if exists is None:
                raise _error(
                    status.HTTP_422_UNPROCESSABLE_ENTITY,
                    "validation_error",
                    f"idea_id={idea_id} 不存在（``ideas`` 无此记录）",
                )
        if taskbook_id is not None:
            exists = (
                await session.execute(select(Taskbook.id).where(Taskbook.id == int(taskbook_id)))
            ).scalar_one_or_none()
            if exists is None:
                raise _error(
                    status.HTTP_422_UNPROCESSABLE_ENTITY,
                    "validation_error",
                    f"taskbook_id={taskbook_id} 不存在（``taskbooks`` 无此记录）",
                )

        project = Project(
            name=readable_name(name),
            mode=mode,
            status="DRAFT",
            current_iteration=0,
            settings=settings,
            is_demo=bool(payload.get("is_demo", False)),
            idea_id=int(idea_id) if idea_id is not None else None,
            taskbook_id=int(taskbook_id) if taskbook_id is not None else None,
        )
        session.add(project)
        await session.flush()

        # 工作目录：用户选择优先，默认保存到知识库项目目录。
        # 统一走宿主执行器，让目录创建与后续文件操作的权限边界一致；
        # 执行器没连上也不拦着建项目，但要如实说明目录没建成。
        chosen = str(payload.get("workspace_dir") or "").strip()
        workspace_dir, workspace_note = await _ensure_workspace(
            project_id=int(project.id), name=project.name, chosen=chosen
        )
        merged_settings = dict(project.settings) if isinstance(project.settings, dict) else {}
        merged_settings["workspace_dir"] = workspace_dir
        if workspace_note:
            merged_settings["workspace_note"] = workspace_note
        project.settings = merged_settings
        await session.flush()

        result = _project_dict(project)
        result["stage_order"] = list(STAGE_ORDER)
        result["note"] = (
            "项目已创建（status=DRAFT）；锁定任务书后调用 POST /pipelines/{pid}/run 启动"
        )
        await session.commit()

    logger.info("project_created project_id=%s name=%s mode=%s", result["id"], name, mode)
    return result


def _clean_pick_title(raw: Any) -> str:
    """标题做长度与控制字符过滤（§6.1）：1–80 个可见字符，默认「选择工作目录」。"""

    text = "".join(ch for ch in str(raw or "") if ch.isprintable() and ch not in "\r\n\t").strip()
    if not text:
        return "选择工作目录"
    return text[:80]


@router.post(
    "/pick-folder",
    status_code=status.HTTP_202_ACCEPTED,
    summary="创建目录选择任务（Owner；立刻返回 request_id）",
)
async def pick_project_folder(
    _owner: OwnerDep, payload: Annotated[dict[str, Any] | None, Body()] = None
) -> dict[str, Any]:
    """请**执行环境**在研究员屏幕上弹出系统自带的文件夹选择框 —— **但不等他选完**。

    返回 202 + `request_id`；前端轮询 `GET /projects/pick-folder/{request_id}` 取结果。
    这样"研究员挑了几分钟"就只是任务 `pending`，**不会**撞上前端 30 秒默认超时 ✓。

    ⚠️ 这条路由必须排在 `/{project_id}` 这类参数路由**之前**（字面路径优先）。
    目录选择由本机服务进程直接弹出原生窗口，返回绝对路径；不会读取或上传目录内容。
    """

    from services.agent import folder_picker

    body = payload or {}
    title = _clean_pick_title(body.get("title"))
    # `initial` 只做规范化：**不能**把宿主路径换算成容器路径再交给执行器 ✗（§4.1）
    initial = str(body.get("initial") or "").strip()[:500]
    # The project dialog needs the real local absolute path. Use the local
    # desktop picker directly; the remote host-runner picker is reserved for
    # environments without a desktop session.
    return await folder_picker.get_picker(local=True).create(title=title, initial=initial)


@router.get("/pick-folder/{request_id}", summary="读目录选择任务状态（Owner）")
async def pick_project_folder_status(request_id: str, _owner: OwnerDep) -> dict[str, Any]:
    """读任务状态：`pending / selected / canceled / unavailable / failed / expired`。"""

    from services.agent import folder_picker

    return await folder_picker.get_picker(local=True).status(request_id)


@router.delete("/pick-folder/{request_id}", summary="取消目录选择任务（Owner）")
async def pick_project_folder_cancel(request_id: str, _owner: OwnerDep) -> dict[str, Any]:
    """取消任务：让执行器关掉同一个系统窗口；重复取消与迟到结果都幂等 ✓。"""

    from services.agent import folder_picker

    return await folder_picker.get_picker(local=True).cancel(request_id)


@router.patch("/{project_id}", summary="重命名 / 归档项目（Owner）")
async def rename_project(
    project_id: int,
    _owner: OwnerDep,
    payload: Annotated[dict[str, Any], Body(...)],
) -> dict[str, Any]:
    """**只允许修改 ``name``（1–200 字符）与 ``archived``（布尔）**。

    刻意不开放 status / current_iteration / 关联字段：这些由流水线状态机与决策留痕
    维护，从管理端直改会绕过状态机（非法迁移）并破坏审计链。

    ``archived`` 是左栏「项目」分组的收起位，与状态机无关，因此允许直改。
    """
    unknown = set(payload) - {"name", "archived"}
    if unknown:
        raise _error(
            status.HTTP_422_UNPROCESSABLE_ENTITY,
            "validation_error",
            f"不支持修改字段：{sorted(unknown)}",
            {"allowed": ["name", "archived"]},
        )

    name: str | None = None
    if "name" in payload:
        name = str(payload.get("name") or "").strip()
        if not name:
            raise _error(
                status.HTTP_422_UNPROCESSABLE_ENTITY, "validation_error", "name 必填", payload
            )
        if len(name) > 200:
            raise _error(
                status.HTTP_422_UNPROCESSABLE_ENTITY,
                "validation_error",
                "name 长度不得超过 200",
                {"length": len(name)},
            )

    archived: bool | None = None
    if "archived" in payload:
        if not isinstance(payload["archived"], bool):
            raise _error(
                status.HTTP_422_UNPROCESSABLE_ENTITY,
                "validation_error",
                "archived 必须为布尔值",
                {"archived": payload["archived"]},
            )
        archived = payload["archived"]

    if name is None and archived is None:
        raise _error(
            status.HTTP_422_UNPROCESSABLE_ENTITY,
            "validation_error",
            "name 与 archived 至少要提供一个",
            None,
        )

    async with _session_factory()() as session:
        project = (
            await session.execute(select(Project).where(Project.id == project_id))
        ).scalar_one_or_none()
        if project is None:
            raise _error(
                status.HTTP_404_NOT_FOUND, "project_not_found", f"project {project_id} 不存在"
            )
        previous = project.name
        if name is not None:
            project.name = readable_name(name)
        if archived is not None:
            project.archived = archived
        project.updated_at = func.now()
        await session.commit()
        # 先提交再 refresh：`updated_at` 由服务端生成，flush 后属性会过期，
        # 在异步会话里直接读过期属性会触发隐式 IO（MissingGreenlet → 500）。
        await session.refresh(project)
        result = _project_dict(project)

    logger.info(
        "project_updated project_id=%s from=%s to=%s archived=%s",
        project_id,
        previous,
        result["name"],
        result["archived"],
    )
    return result


__all__ = [
    "MODES",
    "PROJECT_STATUSES",
    "create_project",
    "get_project",
    "list_projects",
    "rename_project",
    "router",
]


def _slug(name: str) -> str:
    """保留中文可读名称，排除跨目录和 Windows 非法文件名字符。"""
    import re

    label = re.sub(r'[<>:"/\\|?*\x00-\x1f]', '_', name.strip()).rstrip(' .')
    label = label[:160].rstrip(' .') or '未命名项目'
    if label.split('.')[0].upper() in {'CON', 'PRN', 'AUX', 'NUL', *(f'COM{i}' for i in range(1, 10)), *(f'LPT{i}' for i in range(1, 10))}:
        label = '_' + label
    return label


async def _ensure_workspace(*, project_id: int, name: str, chosen: str) -> tuple[str | None, str | None]:
    """确定并创建项目的工作目录，返回 `(目录, 如实说明或 None)`。

    - 用户给了路径 → 创建并使用该宿主目录；
    - 没给 → 落在知识库 `projects/<项目名>`；同名目录用项目号消歧；
    - 建目录走宿主执行器；连不上就返回说明，**不让建项目失败**。
    """

    from services.agent import host_runner

    if chosen:
        target = chosen
    else:
        from services.storage import knowledge_root

        # 默认目录保留项目名称；同名目录用项目号消歧。
        directory_name = _slug(readable_name(name))
        target_path = knowledge_root() / 'projects' / directory_name
        if target_path.exists():
            target_path = target_path.with_name(f'{directory_name}-{project_id}')
        target = str(target_path)

    # 统一成正斜杠再存：Windows 上两种都认，但混着显示（D:\a\b/96-demo）会让人以为路径不对。
    target = target.replace("\\", "/")

    created = await host_runner.call_fs(action="mkdir", path=target, workspace_root=target, access_mode="workspace")
    if created.get("ok"):
        return target, None
    reason = str(created.get("error") or "目录没能创建")
    if created.get("unreachable"):
        return target, f"目录还没建成：{reason}（连上执行器后可以再试）"
    return target, f"目录还没建成：{reason}"
