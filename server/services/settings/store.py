# Copyright 2026 SciLoop contributors
# Licensed under the Apache License, Version 2.0 (http://www.apache.org/licenses/LICENSE-2.0)
"""全局设置服务层：默认值 + 白名单校验 + 幂等 upsert。

设计要点
--------
1. **默认值即契约**：``SCOPES`` 里每个 scope 的 ``defaults`` 就是该 scope 的完整字段表，
   ``GET`` 返回的永远是「默认值 ⊕ 已存值」，所以**前端不需要处理缺字段**。
2. **白名单在服务层，不写进数据库约束**：新增一个设置项只改这一个文件，不必再发迁移。
3. **校验一起做**：未知键、类型不符、取值越界一律 422，并带可读的 ``detail``；
   绝不静默丢弃未知键（静默丢弃会让前端以为保存成功）。
"""

from __future__ import annotations

from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from db.models.settings import AppSetting

#: 允许的阅读版本（与 reader_versions.kind 对齐）
READING_KINDS: tuple[str, ...] = ("original", "chinese", "simple", "bilingual")
#: 版本缺失时的行为
MISSING_ACTIONS: tuple[str, ...] = ("fallback_original", "prompt_generate")
#: 行距档位（与「阅读设置」界面的三档一致）
LINE_HEIGHTS: tuple[float, ...] = (1.45, 1.6, 1.85)
#: 正文字号区间（与界面滑块一致）
FONT_SIZE_MIN, FONT_SIZE_MAX = 14, 24


class SettingsError(Exception):
    """服务层错误；由 API 层映射成 ``{code,message,detail}`` + 自带状态码。"""

    def __init__(
        self,
        status_code: int,
        code: str,
        message: str,
        detail: Any = None,
    ) -> None:
        super().__init__(message)
        self.status_code = status_code
        self.code = code
        self.message = message
        self.detail = detail


# --------------------------------------------------------------------------- #
# scope 定义：defaults 即该 scope 的字段白名单
# --------------------------------------------------------------------------- #
SCOPES: dict[str, dict[str, Any]] = {
    "reading": {
        "default_kind": "original",
        "missing_version_action": "fallback_original",
        "font_size": 16,
        "line_height": 1.6,
        "pair_view": False,
        "annotations_visible": True,
        "anchor_highlight": True,
        "favorite_terms": [],
    },
}

#: scope 的一句话说明（返回体里带上，便于前端直接渲染，不必在前端重复维护文案）
SCOPE_TITLES: dict[str, str] = {
    "reading": "阅读设置",
}


def _require_scope(scope: str) -> dict[str, Any]:
    defaults = SCOPES.get(scope)
    if defaults is None:
        raise SettingsError(
            404,
            "unknown_scope",
            f"未知的设置分组：{scope}",
            {"known_scopes": sorted(SCOPES)},
        )
    return defaults


def _validate(scope: str, defaults: dict[str, Any], patch: dict[str, Any]) -> dict[str, Any]:
    """校验并归一化一个 patch；返回可直接入库的完整值。"""
    unknown = sorted(set(patch) - set(defaults))
    if unknown:
        raise SettingsError(
            422,
            "unknown_setting_key",
            f"分组 {scope} 不接受这些字段：{', '.join(unknown)}",
            {"allowed_keys": sorted(defaults)},
        )

    merged = dict(defaults)
    for key, raw in patch.items():
        merged[key] = _coerce(scope, key, raw)
    return merged


def _coerce(scope: str, key: str, raw: Any) -> Any:
    """逐字段做类型与取值校验（严格：不猜、不强转字符串）。"""
    if key == "default_kind":
        if raw not in READING_KINDS:
            raise _bad(scope, key, raw, list(READING_KINDS))
        return raw
    if key == "missing_version_action":
        if raw not in MISSING_ACTIONS:
            raise _bad(scope, key, raw, list(MISSING_ACTIONS))
        return raw
    if key == "font_size":
        if not isinstance(raw, int) or isinstance(raw, bool) or not (FONT_SIZE_MIN <= raw <= FONT_SIZE_MAX):
            raise _bad(scope, key, raw, f"整数 {FONT_SIZE_MIN}–{FONT_SIZE_MAX}")
        return raw
    if key == "line_height":
        if isinstance(raw, bool) or not isinstance(raw, (int, float)) or float(raw) not in LINE_HEIGHTS:
            raise _bad(scope, key, raw, list(LINE_HEIGHTS))
        return float(raw)
    if key in ("pair_view", "annotations_visible", "anchor_highlight"):
        if not isinstance(raw, bool):
            raise _bad(scope, key, raw, "布尔值")
        return raw
    if key == "favorite_terms":
        if not isinstance(raw, list) or any(not isinstance(x, str) for x in raw):
            raise _bad(scope, key, raw, "字符串数组")
        if len(raw) > 50:
            raise _bad(scope, key, f"{len(raw)} 项", "最多 50 项")
        return raw
    # 兜底：defaults 里出现新字段但忘了写校验分支时，如实报错而不是放行
    raise SettingsError(
        500,
        "setting_validator_missing",
        f"字段 {key} 缺少校验分支（服务层缺陷，请补 _coerce）",
        {"scope": scope, "key": key},
    )


def _bad(scope: str, key: str, raw: Any, expected: Any) -> SettingsError:
    return SettingsError(
        422,
        "invalid_setting_value",
        f"字段 {key} 取值不合法",
        {"scope": scope, "key": key, "got": raw, "expected": expected},
    )


# --------------------------------------------------------------------------- #
# 读写
# --------------------------------------------------------------------------- #
async def get_scope(session: AsyncSession, scope: str) -> dict[str, Any]:
    """读一个 scope：**默认值 ⊕ 已存值**（缺字段永远有默认值兜底）。"""
    defaults = _require_scope(scope)
    row = await session.get(AppSetting, scope)
    stored = dict(row.value or {}) if row is not None else {}
    # 只回默认表里存在的键：即便库里残留了历史字段也不外泄（表结构演进时更安全）
    merged = {key: stored.get(key, defaults[key]) for key in defaults}
    return {
        "scope": scope,
        "title": SCOPE_TITLES.get(scope, scope),
        "settings": merged,
        "defaults": defaults,
        "updated_at": row.updated_at.isoformat() if row is not None and row.updated_at else None,
    }


async def put_scope(session: AsyncSession, scope: str, patch: dict[str, Any]) -> dict[str, Any]:
    """写一个 scope（**部分更新**：只覆盖 patch 里出现的键），返回写入后的完整值。"""
    defaults = _require_scope(scope)
    if not isinstance(patch, dict):
        raise SettingsError(
            422,
            "invalid_body",
            "请求体必须是一个 JSON 对象",
            {"got": type(patch).__name__},
        )

    current = await get_scope(session, scope)
    merged = _validate(scope, defaults, {**current["settings"], **patch})

    row = await session.get(AppSetting, scope)
    if row is None:
        row = AppSetting(scope=scope, value=merged)
        session.add(row)
    else:
        row.value = merged
    await session.commit()
    await session.refresh(row)

    result = await get_scope(session, scope)
    return result


async def list_scopes(session: AsyncSession) -> list[dict[str, Any]]:
    """列出全部 scope 的当前值（供设置页一次性拉取，避免 N 次请求）。"""
    return [await get_scope(session, scope) for scope in sorted(SCOPES)]


__all__ = [
    "LINE_HEIGHTS",
    "MISSING_ACTIONS",
    "READING_KINDS",
    "SCOPES",
    "SCOPE_TITLES",
    "SettingsError",
    "get_scope",
    "list_scopes",
    "put_scope",
]
