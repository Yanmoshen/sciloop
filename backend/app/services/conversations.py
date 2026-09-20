# Copyright 2026 SciLoop contributors
# Licensed under the Apache License, Version 2.0 (http://www.apache.org/licenses/LICENSE-2.0)
"""首页对话的「会话」：一轮轮对话落成 JSON 文件，刷新/重开页面都能接着上次继续。

为什么落文件而不是落库
----------------------
用户口径：**单独一个文件夹、JSON 格式保存对话记录**，并且刷新后要能恢复。
文件是纯追加式的留痕，读回来也不依赖任何表结构，便于人工查看与备份。

存储位置
--------
容器内 ``/app/backend/.cache/conversations/<会话id>.json``（由 docker-compose.yml
把宿主 ``./.data/conversations`` 挂到这里）——与全文缓存 / 上传 / 产物同一套惯例。

文件结构
--------
``{"id", "title", "model_ref", "project_id", "created_at", "updated_at", "turns": [...]}``
其中 ``turns`` 元素：``{"role": "user"|"assistant", "content", "ts"}``，
assistant 额外带 ``model_id`` / ``duration_ms``。

红线
----
读写失败**不吞掉**：列表读不到就给空列表并记日志，但**写入失败要抛**（否则用户以为存住了）。
"""

from __future__ import annotations

import json
import logging
import os
import re
import uuid
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

logger = logging.getLogger("sciloop.conversations")

DEFAULT_DIR = "/app/backend/.cache/conversations"
ID_PATTERN = re.compile(r"^[0-9a-zA-Z_-]{6,64}$")
MAX_TURNS_FOR_CONTEXT = 12


def conversations_dir() -> Path:
    """会话目录（可用 ``CONVERSATIONS_DIR`` 覆盖，便于测试）。"""
    path = Path(os.environ.get("CONVERSATIONS_DIR") or DEFAULT_DIR)
    path.mkdir(parents=True, exist_ok=True)
    return path


def _now() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds")


def _path(conversation_id: str) -> Path:
    if not ID_PATTERN.match(conversation_id):
        raise ValueError("会话 id 非法")
    return conversations_dir() / f"{conversation_id}.json"


def write(record: dict[str, Any]) -> None:
    """原子落盘：先写临时文件再替换，避免读到半截 JSON。"""
    target = _path(str(record["id"]))
    tmp = target.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(record, ensure_ascii=False, indent=2), encoding="utf-8")
    tmp.replace(target)


def read(conversation_id: str) -> dict[str, Any] | None:
    try:
        path = _path(conversation_id)
    except ValueError:
        return None
    if not path.exists():
        return None
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        logger.warning("会话文件读取失败（按不存在处理）：%s", path.name)
        return None


def create(*, title: str, model_ref: str) -> dict[str, Any]:
    stamp = _now()
    record: dict[str, Any] = {
        "id": uuid.uuid4().hex[:16],
        "title": title,
        "model_ref": model_ref,
        "project_id": None,
        "created_at": stamp,
        "updated_at": stamp,
        "turns": [],
    }
    write(record)
    return record


def append_turns(record: dict[str, Any], new_turns: list[dict[str, Any]]) -> dict[str, Any]:
    record["turns"] = [*(record.get("turns") or []), *new_turns]
    record["updated_at"] = _now()
    write(record)
    return record


def summary(record: dict[str, Any]) -> dict[str, Any]:
    """列表用摘要：不带 turns，避免列表接口过大。"""
    return {
        "id": record.get("id"),
        "title": record.get("title"),
        "model_ref": record.get("model_ref"),
        "project_id": record.get("project_id"),
        "created_at": record.get("created_at"),
        "updated_at": record.get("updated_at"),
        "turn_count": len(record.get("turns") or []),
    }


def list_all(limit: int = 20) -> list[dict[str, Any]]:
    """按最近更新时间倒序（新会话/刚聊过的排最前）。"""
    items: list[dict[str, Any]] = []
    for path in conversations_dir().glob("*.json"):
        try:
            items.append(json.loads(path.read_text(encoding="utf-8")))
        except (json.JSONDecodeError, OSError):
            logger.warning("跳过损坏的会话文件：%s", path.name)
    items.sort(key=lambda item: str(item.get("updated_at") or ""), reverse=True)
    return [summary(item) for item in items[:limit]]


def context_messages(record: dict[str, Any], limit: int = MAX_TURNS_FOR_CONTEXT) -> list[dict[str, str]]:
    """把最近若干轮整理成 OpenAI 兼容的 messages（实现「接着上次继续」）。"""
    turns = record.get("turns") or []
    messages: list[dict[str, str]] = []
    for turn in turns[-limit:]:
        role = turn.get("role")
        content = turn.get("content")
        if role in {"user", "assistant"} and isinstance(content, str) and content.strip():
            messages.append({"role": role, "content": content})
    return messages
