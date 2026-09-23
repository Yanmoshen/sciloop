# Copyright 2026 SciLoop contributors
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
"""技能库接口：装了什么、开关、挂载外部目录、跑一个、看执行记录、新建/编辑。

口径（研究者 2026-09-23 确认）：
- 技能库是**独立入口**（与知识库平级），读接口公开、写操作要研究者身份（`X-Owner-Token`）；
- 跑技能是**执行类动作** —— 这里只负责"跑"这一层，**批准在对话/节点的上游**；
- 产物与执行记录都落在项目产物目录，前端读 `GET /skills/runs` 展示。
"""

from __future__ import annotations

from typing import Annotated, Any

from fastapi import APIRouter, Body, Depends, HTTPException, status
from pydantic import BaseModel, Field

from core.security import require_owner
from services.skills import service, state

router = APIRouter(tags=["skills"])

OwnerDep = Annotated[None, Depends(require_owner)]


class EnableBody(BaseModel):
    enabled: bool = True


class MountBody(BaseModel):
    path: str = Field(description="要挂载的目录（研究者电脑上或容器里能看到的路径）")


class RunBody(BaseModel):
    name: str = Field(description="技能名")
    topic: str = Field(default="", description="这一步要解决什么（写进 {{topic}} 占位符）")
    task_id: str = Field(description="任务编号：产物按它归档")
    project_dir: str = Field(default="", description="项目目录（不填就用执行器报的默认目录）")


class SkillBody(BaseModel):
    content: str = Field(description="完整的 SKILL.md 内容（含 frontmatter）")


@router.get("/skills", summary="技能库全貌（公开只读）")
async def get_library() -> dict[str, Any]:
    return service.library()


@router.post("/skills/{name}/enable", summary="启用/停用一个技能（需研究者身份）", dependencies=[Depends(require_owner)])
async def set_enabled(name: str, payload: EnableBody = Body(...)) -> dict[str, Any]:
    return state.set_enabled(name, payload.enabled)


@router.post("/skills/{name}/disable", summary="停用一个技能（需研究者身份）", dependencies=[Depends(require_owner)])
async def set_disabled(name: str) -> dict[str, Any]:
    """与 `/enable` 对称的一支：前端点「关」时直接打这里，不用传 body。"""

    return state.set_enabled(name, False)


@router.post(
    "/skills/health-check",
    summary="体检：哪些技能在这台机器上跑不了（需研究者身份）",
    dependencies=[Depends(require_owner)],
)
async def health_check() -> dict[str, Any]:
    """算一遍依赖、并在研究者电脑上跑一条**只读**探针（看包在不在），结论缓存下来。

    跑技能是执行类动作，所以这条也要研究者身份；但它只是 import 检查，不改任何东西。
    """

    return await service.health_check()


@router.post("/skills/mounts", summary="挂载一个外部技能目录（需研究者身份）", dependencies=[Depends(require_owner)])
async def add_mount(payload: MountBody = Body(...)) -> dict[str, Any]:
    result = state.add_mount(payload.path)
    if not result.get("ok"):
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail={"code": "skill_mount_failed", "message": result.get("message"), "detail": None},
        )
    return result | {"library": service.library()}


@router.delete("/skills/mounts", summary="取消挂载（需研究者身份）", dependencies=[Depends(require_owner)])
async def remove_mount(path: str) -> dict[str, Any]:
    return state.remove_mount(path)


@router.put("/skills/{name}", summary="新建或编辑一个技能（需研究者身份）", dependencies=[Depends(require_owner)])
async def save_skill(name: str, payload: SkillBody = Body(...)) -> dict[str, Any]:
    result = service.save_skill(name, payload.content)
    if not result.get("ok"):
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail={"code": "skill_save_failed", "message": result.get("message"), "detail": None},
        )
    return result


@router.post("/skills/run", summary="跑一个技能（需研究者身份；批准在上游）", dependencies=[Depends(require_owner)])
async def run_skill(payload: RunBody = Body(...)) -> dict[str, Any]:
    return await service.run(
        payload.name,
        topic=payload.topic,
        task_id=payload.task_id,
        project_dir=payload.project_dir,
    )


@router.get("/skills/runs", summary="某个任务下的技能执行记录（公开只读）")
async def list_runs(task_id: str) -> dict[str, Any]:
    return {"task_id": task_id, "runs": service.runs(task_id)}


@router.get("/skills/{name}", summary="某个技能的完整说明（公开只读）")
async def get_skill(name: str) -> dict[str, Any]:
    """编辑技能时要拿到**当前内容**：不然"编辑"会把原内容覆盖成空壳。

    ⚠️ 本路由必须排在 `/skills/runs` 之后（字面路径先于同前缀参数路由）。
    """

    from services.skills import registry, service

    pack = next((item for item in service.all_packs() if item.name == name), None)
    if pack is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail={"code": "skill_not_found", "message": f"没有这个技能：{name}", "detail": None},
        )
    content = ""
    if pack.path is not None and (pack.path / "SKILL.md").is_file():
        content = (pack.path / "SKILL.md").read_text(encoding="utf-8", errors="replace")
    return registry.load(pack) | {"ok": True, "content": content}
