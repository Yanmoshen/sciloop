# Copyright 2026 SciLoop contributors
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
"""覆盖度计算与 ``parse_status`` 判定（WP05-T4 / 附录 D.0 硬约束①②）。

定义（与 §2.8 一致，可人工核对）：

- ``char_count``：本次解析得到的**整篇文档**字符数（含被 ``FULLTEXT_MAX_PAGES``
  截断掉的部分），即"分母"。
- ``locatable_chars``：能被 ``(char_start, char_end)`` 精确定位的字符数之和，
  即"分子"。
- ``coverage = locatable_chars / char_count``，保留 3 位小数（列类型 NUMERIC(4,3)）。

状态判定：

===============  ==========================================
``ok``           ``coverage >= 0.60`` 且存在正文文本
``partial``      有文本但 ``coverage < 0.60``（截断/部分不可定位）
``unavailable``  无文本层（char_count == 0 或无任何可定位字符）
``failed``       抓取或解析抛异常
===============  ==========================================
"""

from __future__ import annotations

from typing import Any

#: 允许生成正文 paper_span 证据的最低覆盖率（contracts.evidence_rules.fulltext_gate）
DEFAULT_MIN_COVERAGE = 0.60
#: coverage 落到 NUMERIC(4,3)
COVERAGE_SCALE = 3

#: 允许生成正文 span 的 parse_status
SPAN_ELIGIBLE_STATUS: frozenset[str] = frozenset({"ok"})


def _settings_attr(name: str, default: Any) -> Any:
    """读取 ``core.config.settings``，缺失时退回默认值。

    解析算法允许脱离完整应用栈运行（单测不装 pydantic-settings 也能跑）。
    """
    try:  # pragma: no cover - 依赖运行环境
        from core.config import settings

        value = getattr(settings, name, None)
        return default if value is None else value
    except Exception:  # pragma: no cover - 无应用配置时的降级路径
        return default


def min_coverage_threshold() -> float:
    """``FULLTEXT_MIN_COVERAGE``（默认 0.60）。"""
    try:
        value = float(_settings_attr("fulltext_min_coverage", DEFAULT_MIN_COVERAGE))
    except (TypeError, ValueError):  # pragma: no cover - 配置错误兜底
        return DEFAULT_MIN_COVERAGE
    if value <= 0 or value > 1:
        return DEFAULT_MIN_COVERAGE
    return value


def compute_coverage(locatable_chars: int, char_count: int) -> float:
    """``coverage = locatable_chars ÷ char_count``，保留 3 位小数。

    ``char_count <= 0`` 时返回 0.0（无文本层场景），绝不产生 NaN 或编造值。
    """
    if char_count is None or char_count <= 0:
        return 0.0
    locatable = max(0, int(locatable_chars or 0))
    ratio = locatable / float(char_count)
    if ratio > 1.0:  # 理论上不会发生；出现说明偏移统计 bug，钳制并暴露
        ratio = 1.0
    return round(ratio, COVERAGE_SCALE)


def classify_parse_status(
    *,
    char_count: int,
    locatable_chars: int,
    coverage: float | None = None,
    failed: bool = False,
    min_coverage: float | None = None,
) -> str:
    """判定 ``parse_status ∈ {ok, partial, unavailable, failed}``。

    顺序即优先级：``failed`` > ``unavailable`` > ``ok`` > ``partial``。
    """
    if failed:
        return "failed"
    if not char_count or char_count <= 0:
        return "unavailable"
    if not locatable_chars or locatable_chars <= 0:
        return "unavailable"
    threshold = min_coverage_threshold() if min_coverage is None else float(min_coverage)
    effective = compute_coverage(locatable_chars, char_count) if coverage is None else float(coverage)
    return "ok" if effective >= threshold else "partial"


def spans_allowed(
    parse_status: str | None,
    coverage: float | None,
    *,
    min_coverage: float | None = None,
) -> bool:
    """正文 ``paper_span`` 是否允许生成（两道门：status 与 coverage）。"""
    if parse_status not in SPAN_ELIGIBLE_STATUS:
        return False
    if coverage is None:
        return False
    threshold = min_coverage_threshold() if min_coverage is None else float(min_coverage)
    return float(coverage) >= threshold


def evidence_scope(parse_status: str | None, coverage: float | None) -> str:
    """证据覆盖范围：``fulltext`` 或 ``abstract_only``（前端 CoverageTag 展示）。"""
    return "fulltext" if spans_allowed(parse_status, coverage) else "abstract_only"


def coverage_note(parse_status: str | None, coverage: float | None) -> str:
    """面向研究者的中文说明，禁止把不可用说成可用。"""
    if parse_status == "failed":
        return "解析失败，证据覆盖范围：仅摘要"
    if parse_status == "unavailable":
        return "全文不可用（无文本层），证据覆盖范围：仅摘要"
    if parse_status == "partial":
        pct = f"{(coverage or 0) * 100:.0f}%"
        return f"全文仅部分可定位（覆盖 {pct}%），证据覆盖范围：仅摘要"
    if parse_status == "ok":
        pct = f"{(coverage or 0) * 100:.0f}%"
        return f"全文可用（覆盖 {pct}%）"
    return "解析状态未知，证据覆盖范围：仅摘要"
