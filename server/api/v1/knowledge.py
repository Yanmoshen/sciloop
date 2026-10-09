# Copyright 2026 SciLoop contributors
# Licensed under the Apache License, Version 2.0 (the "License");
"""Knowledge base filesystem API."""

from __future__ import annotations

import json
from typing import Annotated

from fastapi import APIRouter, Body, Depends, File, Form, HTTPException, Query, UploadFile
from fastapi.responses import FileResponse

from core.security import require_owner
from services import knowledge_base

router = APIRouter(prefix="/knowledge", tags=["knowledge"])
OwnerDep = Annotated[None, Depends(require_owner)]


@router.get("/entries")
async def list_entries(q: str = "", bucket: str | None = None, folder: str | None = None, project_id: int | None = None) -> dict:
    return knowledge_base.snapshot(q=q, bucket=bucket, folder=folder.split("/") if folder else None, project_id=project_id)


@router.post("/entries")
async def create_entry(_owner: OwnerDep, payload: Annotated[dict, Body(...)]) -> dict:
    return knowledge_base.create(
        name=str(payload.get("name") or "资料"), bucket=str(payload.get("bucket") or "literature"), content=str(payload.get("content") or ""),
        folder=payload.get("folder") or [], tags=payload.get("tags") or [], project_id=payload.get("project_id"), source_label=str(payload.get("source_label") or "手动新建"), source_route=payload.get("source_route"),
    )


@router.post("/files")
async def upload_file(
    _owner: OwnerDep,
    file: UploadFile = File(...),
    bucket: str = Form("literature"),
    folder: str = Form(""),
    project_id: int | None = Form(None),
    tags: str = Form(""),
) -> dict:
    data = await file.read()
    try:
        parsed_tags = json.loads(tags) if tags.strip().startswith("[") else tags.split(",")
    except json.JSONDecodeError:
        parsed_tags = tags.split(",")
    return knowledge_base.create(name=file.filename or "上传资料", bucket=bucket, data=data, folder=folder.split("/") if folder else [], tags=parsed_tags, project_id=project_id)


@router.get("/files/{entry_id}")
async def download_file(entry_id: str):
    path = knowledge_base.payload_path(entry_id)
    if path is None:
        raise HTTPException(status_code=404, detail={"code": "knowledge_not_found", "message": "文件不存在"})
    return FileResponse(path, filename=path.name)


@router.patch("/entries/{entry_id}")
async def update_entry(entry_id: str, _owner: OwnerDep, payload: Annotated[dict, Body(...)]) -> dict:
    result = knowledge_base.update(entry_id, payload)
    if result is None:
        raise HTTPException(status_code=404, detail={"code": "knowledge_not_found", "message": "条目不存在"})
    return result


@router.delete("/entries")
async def delete_entries(_owner: OwnerDep, ids: str = Query(...)) -> dict[str, int]:
    return {"deleted": knowledge_base.delete([value for value in ids.split(",") if value])}


@router.post("/entries/trash")
async def trash_entries(_owner: OwnerDep, payload: Annotated[dict, Body(...)]) -> dict[str, int]:
    return {"changed": knowledge_base.trash([str(value) for value in payload.get("ids") or []])}


@router.post("/entries/restore")
async def restore_entries(_owner: OwnerDep, payload: Annotated[dict, Body(...)]) -> dict[str, int]:
    return {"changed": knowledge_base.trash([str(value) for value in payload.get("ids") or []], restore=True)}


@router.post("/entries/move")
async def move_entries(_owner: OwnerDep, payload: Annotated[dict, Body(...)]) -> dict[str, int]:
    changed = 0
    for entry_id in payload.get("ids") or []:
        if knowledge_base.update(str(entry_id), {"folder": payload.get("folder") or []}):
            changed += 1
    return {"changed": changed}


@router.post("/entries/tag")
async def tag_entries(_owner: OwnerDep, payload: Annotated[dict, Body(...)]) -> dict[str, int]:
    changed = 0
    tag = str(payload.get("tag") or "").strip()
    for entry_id in payload.get("ids") or []:
        item = knowledge_base.get(str(entry_id))
        if (
            item
            and tag not in item.get("tags", [])
            and knowledge_base.update(str(entry_id), {"tags": [*item.get("tags", []), tag]})
        ):
            changed += 1
    return {"changed": changed}


@router.post("/entries/details")
async def entry_details(payload: Annotated[dict, Body(...)]) -> dict[str, list[dict]]:
    """批量取**含正文**的条目（导出这类"要正文"的操作走这里，一次取回）。

    列表接口只回元数据：本机实测 915 条把全文内联会把响应撑到 46MB、前端卡在骨架态。
    """
    return {"items": knowledge_base.details([str(value) for value in payload.get("ids") or []])}


@router.get("/entries/{entry_id}")
async def entry_detail(entry_id: str) -> dict:
    """单条详情（含正文）。"""
    item = knowledge_base.get(entry_id)
    if item is None:
        raise HTTPException(status_code=404, detail={"code": "knowledge_not_found", "message": "条目不存在"})
    return item


@router.post("/folders")
async def create_folder(_owner: OwnerDep, payload: Annotated[dict, Body(...)]) -> dict[str, list[str]]:
    return {"folders": knowledge_base.create_folder(payload.get("folder") or [])}
