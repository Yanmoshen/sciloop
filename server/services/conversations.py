# Copyright 2026 SciLoop contributors
# Licensed under the Apache License, Version 2.0 (http://www.apache.org/licenses/LICENSE-2.0)
"""首页对话的「会话」：一轮轮对话落成 JSON 文件，刷新/重开页面都能接着上次继续。

为什么落文件而不是落库
----------------------
用户口径：**单独一个文件夹、JSON 格式保存对话记录**，并且刷新后要能恢复。
文件是纯追加式的留痕，读回来也不依赖任何表结构，便于人工查看与备份。

存储位置（2026-09-20 起按项目分目录）
-------------------------------------
容器内 ``/app/server/.cache/conversations/<项目id>/<会话id>.json``；
**未分组**的对话放 ``conversations/_ungrouped/<会话id>.json``。
（容器目录由 docker-compose.yml 把宿主 ``./.data/conversations`` 挂进来。）

历史遗留的**平铺文件**（``conversations/<会话id>.json``）仍可读，视为未分组；
下一次写入会自动把它落进 ``_ungrouped/``。

文件结构
--------
``{"id", "title", "project_id", "model_ref", "created_at", "updated_at",
   "archived", "turns": [...]}``
其中 ``turns`` 元素：``{"role": "user"|"assistant", "content", "ts"}``，
assistant 额外带 ``model_id`` / ``duration_ms`` / ``reasoning`` / ``rows`` / ``approvals``：

- ``rows``：过程行（节点进度、工具调用、**批准卡**）。它们与正文同等落盘——
  只发 SSE 不落盘的后果，就是刷新后界面上那段过程凭空消失，而库里只剩一句结论。
- ``approvals``：本轮里模型的写盘/执行请求（``services/agent/approvals.py`` 的
  ``new_request``）。**批准本身也是一条要留痕的事实**，与 turns 同源落这里，
  不另建表。

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

DEFAULT_DIR = "/app/server/.cache/conversations"
ID_PATTERN = re.compile(r"^[0-9a-zA-Z_-]{6,64}$")
MAX_TURNS_FOR_CONTEXT = 12

#: 未分组对话所在子目录名（不是数字，因此不会与任何真实项目 id 冲突）
UNGROUPED_KEY = "_ungrouped"


def conversations_dir() -> Path:
    """会话根目录（可用 ``CONVERSATIONS_DIR`` 覆盖，便于测试）。"""
    path = Path(os.environ.get("CONVERSATIONS_DIR") or DEFAULT_DIR)
    path.mkdir(parents=True, exist_ok=True)
    return path


def group_key(project_id: Any) -> str:
    """项目 id → 目录名；``None``/非法值归入 ``_ungrouped``。"""
    if project_id is None:
        return UNGROUPED_KEY
    try:
        value = int(project_id)
    except (TypeError, ValueError):
        return UNGROUPED_KEY
    return str(value) if value > 0 else UNGROUPED_KEY


def normalize_project_id(project_id: Any) -> int | None:
    """把入参归一为 ``int | None``（``None`` / ``<=0`` / 非数字 → None，即未分组）。"""
    if project_id is None:
        return None
    try:
        value = int(project_id)
    except (TypeError, ValueError):
        return None
    return value if value > 0 else None


def _now() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds")


def _group_dir(project_id: Any) -> Path:
    return conversations_dir() / group_key(project_id)


def _path(conversation_id: str, project_id: Any) -> Path:
    if not ID_PATTERN.match(conversation_id):
        raise ValueError("会话 id 非法")
    return _group_dir(project_id) / f"{conversation_id}.json"


def locate(conversation_id: str) -> Path | None:
    """按 id 找到文件（先查未分组，再按项目目录扫；兼容根目录下的历史平铺文件）。"""
    if not ID_PATTERN.match(conversation_id):
        return None
    root = conversations_dir()
    legacy = root / f"{conversation_id}.json"
    if legacy.exists():
        return legacy
    for path in root.glob(f"*/{conversation_id}.json"):
        if path.is_file():
            return path
    return None


def write(record: dict[str, Any]) -> None:
    """原子落盘：先写临时文件再替换，避免读到半截 JSON。

    目标目录由 ``record['project_id']`` 决定；若该会话此前落在别的目录（例如刚「移入项目」），
    旧文件会被清掉，保证一个会话在磁盘上只存在一份。
    """
    conversation_id = str(record["id"])
    target = _path(conversation_id, record.get("project_id"))
    target.parent.mkdir(parents=True, exist_ok=True)
    previous = locate(conversation_id)
    tmp = target.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(record, ensure_ascii=False, indent=2), encoding="utf-8")
    tmp.replace(target)
    if previous is not None and previous != target:
        try:
            previous.unlink()
        except OSError:  # pragma: no cover - 旧文件清理失败不影响写入结果
            logger.warning("会话迁移后旧文件清理失败：%s", previous)


def read(conversation_id: str) -> dict[str, Any] | None:
    path = locate(conversation_id)
    if path is None:
        return None
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        logger.warning("会话文件读取失败（按不存在处理）：%s", path.name)
        return None


def create(
    *,
    title: str,
    model_ref: str,
    project_id: Any = None,
    conversation_id: str | None = None,
) -> dict[str, Any]:
    """新建会话记录（尚未有任何轮次）。``conversation_id`` 可在流式开场时先占位。"""
    stamp = _now()
    record: dict[str, Any] = {
        "id": conversation_id or uuid.uuid4().hex[:16],
        "title": title,
        "model_ref": model_ref,
        "project_id": normalize_project_id(project_id),
        "created_at": stamp,
        "updated_at": stamp,
        "archived": False,
        "turns": [],
    }
    write(record)
    return record


def append_turns(record: dict[str, Any], new_turns: list[dict[str, Any]]) -> dict[str, Any]:
    record["turns"] = [*(record.get("turns") or []), *new_turns]
    record["updated_at"] = _now()
    write(record)
    return record


def truncate_from(record: dict[str, Any], index: int) -> dict[str, Any]:
    """只保留 ``index`` 之前的轮次（第 ``index`` 条及其后全部丢弃），**不落盘**。

    用于「编辑某条用户消息后从此处重开」：调用方负责接着 ``append_turns`` 写入新一轮，
    这样"截断 + 追加"只在同一个 ``finally`` 里写一次文件 —— 中途模型失败也能保住用户改的内容，
    也不会出现"截断写盘了、新一轮没写上"的半成品状态。

    ``index`` 越界时抛 ``IndexError``（调用方转成 422），不静默取边界值。
    """
    turns = list(record.get("turns") or [])
    if index < 0 or index > len(turns):
        raise IndexError(f"截断位置越界：index={index}，当前轮次={len(turns)}")
    record["turns"] = turns[:index]
    return record


def move(conversation_id: str, project_id: Any) -> dict[str, Any] | None:
    """把会话移入某项目（``project_id=None`` 即移回未分组）。"""
    record = read(conversation_id)
    if record is None:
        return None
    record["project_id"] = normalize_project_id(project_id)
    record["updated_at"] = _now()
    write(record)
    return record


def set_archived(conversation_id: str, archived: bool) -> dict[str, Any] | None:
    record = read(conversation_id)
    if record is None:
        return None
    record["archived"] = bool(archived)
    record["updated_at"] = _now()
    write(record)
    return record


def rename(conversation_id: str, title: str) -> dict[str, Any] | None:
    record = read(conversation_id)
    if record is None:
        return None
    record["title"] = title
    record["updated_at"] = _now()
    write(record)
    return record


def set_fields(conversation_id: str, **fields: Any) -> dict[str, Any] | None:
    """局部更新会话记录里的任意字段（读改写，只动传入的键）。

    用途：记录「本对话已声明为普通对话（不走研究流程）」这类**对话级状态**。
    故意做成通用而不是给每个状态各写一个函数——否则每加一个状态就要动这个热点文件。
    """

    record = read(conversation_id)
    if record is None:
        return None
    record.update(fields)
    record["updated_at"] = _now()
    write(record)
    return record


def delete(conversation_id: str) -> bool:
    path = locate(conversation_id)
    if path is None:
        return False
    try:
        path.unlink()
    except OSError:  # pragma: no cover
        logger.warning("会话文件删除失败：%s", path.name)
        return False
    return True


def summary(record: dict[str, Any]) -> dict[str, Any]:
    """列表用摘要：不带 turns，避免列表接口过大。"""
    return {
        "id": record.get("id"),
        "title": record.get("title"),
        "model_ref": record.get("model_ref"),
        "project_id": normalize_project_id(record.get("project_id")),
        "created_at": record.get("created_at"),
        "updated_at": record.get("updated_at"),
        "archived": bool(record.get("archived")),
        "turn_count": len(record.get("turns") or []),
    }


def iter_records() -> list[dict[str, Any]]:
    """扫全部会话文件（含历史平铺文件），坏文件跳过并记日志。"""
    records: list[dict[str, Any]] = []
    seen: set[str] = set()
    for path in sorted(conversations_dir().glob("*.json")):
        record = _load(path)
        if record is not None and str(record.get("id")) not in seen:
            seen.add(str(record.get("id")))
            records.append(record)
    for path in sorted(conversations_dir().glob("*/*.json")):
        record = _load(path)
        if record is not None and str(record.get("id")) not in seen:
            seen.add(str(record.get("id")))
            records.append(record)
    return records


def _load(path: Path) -> dict[str, Any] | None:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        logger.warning("跳过损坏的会话文件：%s", path.name)
        return None


def list_all(
    limit: int | None = None,
    *,
    project_id: Any = "any",
    archived: bool | None = False,
) -> list[dict[str, Any]]:
    """按最近更新时间倒序。

    :param project_id: ``"any"`` = 不过滤；``None`` = 只要未分组；数字 = 只要该项目
    :param archived: ``False`` 只要未归档（默认）；``True`` 只要已归档；``None`` 不限
    """
    items: list[dict[str, Any]] = []
    for record in iter_records():
        if archived is not None and bool(record.get("archived")) is not bool(archived):
            continue
        if project_id != "any" and normalize_project_id(record.get("project_id")) != normalize_project_id(
            project_id
        ):
            continue
        items.append(record)
    items.sort(key=lambda item: str(item.get("updated_at") or ""), reverse=True)
    summaries = [summary(item) for item in items]
    return summaries if limit is None else summaries[:limit]


def latest(project_id: Any = "any") -> dict[str, Any] | None:
    """最近一次会话（未归档）。"""
    items = list_all(1, project_id=project_id, archived=False)
    return items[0] if items else None


def context_messages(record: dict[str, Any], limit: int = MAX_TURNS_FOR_CONTEXT) -> list[dict[str, str]]:
    """把最近若干轮整理成 OpenAI 兼容的 messages（实现「接着上次继续」）。

    两条**不进上下文**的轮次（都是"这一轮并没有真的说话"的情形）：

    - 正文为空（模型只要求调工具 / 只等批准）；
    - 正文是**系统说明**（``note_only``，例如"模型只给了思考过程"）。
      它是界面上的如实说明，不是模型说的话 —— 当成 assistant 的历史喂回去，
      模型会以为自己说过那句话。
    """

    turns = record.get("turns") or []
    messages: list[dict[str, str]] = []
    for turn in turns[-limit:]:
        role = turn.get("role")
        content = turn.get("content")
        if turn.get("note_only"):
            continue
        if role in {"user", "assistant"} and isinstance(content, str) and content.strip():
            messages.append({"role": role, "content": content})
    return messages


def purge_flat_test_files(keep_ids: set[str] | None = None) -> list[str]:
    """清掉根目录下的历史平铺会话文件（``keep_ids`` 内的保留）。返回被删的 id 列表。"""
    removed: list[str] = []
    keep = keep_ids or set()
    for path in sorted(conversations_dir().glob("*.json")):
        if path.stem in keep:
            continue
        try:
            path.unlink()
        except OSError:  # pragma: no cover
            continue
        removed.append(path.stem)
    return removed


__all__ = [
    "TITLE_MONOLOGUE_MARKERS",
    "TITLE_SYSTEM",
    "pick_title_line",
    "MAX_TURNS_FOR_CONTEXT",
    "UNGROUPED_KEY",
    "append_turns",
    "context_messages",
    "conversations_dir",
    "create",
    "delete",
    "group_key",
    "iter_records",
    "latest",
    "list_all",
    "locate",
    "move",
    "normalize_project_id",
    "purge_flat_test_files",
    "read",
    "rename",
    "set_archived",
    "set_fields",
    "summary",
    "write",
]


# --------------------------------------------------------------------------- #
# 会话标题：只输出标题本身，不许把"思考过程"混进来
# --------------------------------------------------------------------------- #
#: 标题调用的系统提示。**这句不能省** —— 标题调用原来只有 user 提示，
#: 于是擅长"自言自语"的模型把推理写进正文，第一行就被当标题存下来了（2026-09-22 实测 17 例）。
TITLE_SYSTEM = (
    "你是科研项目的命名助手。只输出一个标题，不要输出任何思考过程、推理、自我对话、"
    "解释或前后缀——例如「我们需要回答用户……」「用户要求……」「让我看看……」这类句子一律不许出现。"
    "标题用中文，不超过 20 个字，不带引号、不带句号，只输出标题本身。"
)

#: 一眼能看出是"模型在自言自语"的痕迹：命中就不要拿它当标题
TITLE_MONOLOGUE_MARKERS: tuple[str, ...] = (
    "我们需要回答用户",
    "用户要求",
    "用户说",
    "用户希望",
    "让我",
    "我需要",
    "首先",
    "拟一个标题",
    "命名助手",
    "只输出标题",
    "输出标题",
    "标题：",
    "标题:",
    "题目：",
    "以下是",
)

#: 标题里不该出现的标点（标题是一行短语，不是句子）
_TITLE_BAD_PUNCT = ("。", "？", "！", "?", "!", "；", ";")


def pick_title_line(content: str, *, max_chars: int = 20) -> str:
    """从模型回复里挑出**标题那一行**；挑不出来就返回空串。

    三层兜底：
    1. 跳过含独白痕迹的行；
    2. 在剩下的里面优先挑"像标题"的（不超过 max_chars、且没有句末标点）；
    3. 实在没有像样的 → 返回空串，**由调用方降级**（用研究者输入的前若干字），
       而不是硬把一段独白塞进侧栏。
    """

    rows = [row.strip().strip("《》\"'“”") for row in (content or "").splitlines()]
    rows = [row for row in rows if row]
    if not rows:
        return ""

    clean = [row for row in rows if not any(marker in row for marker in TITLE_MONOLOGUE_MARKERS)]
    for row in clean:
        if len(row) <= max_chars and not any(punct in row for punct in _TITLE_BAD_PUNCT):
            return row
    if clean:
        # 次选：最短的一行再截断（比整段独白强得多）
        return min(clean, key=len)[:max_chars]
    return ""
