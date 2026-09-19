# Copyright 2026 SciLoop contributors
# Licensed under the Apache License, Version 2.0 (http://www.apache.org/licenses/LICENSE-2.0)
"""venue 归一与抽取（WP04-T2 / WP04-T3）。

职责
----
1. 把任意来源的会议/期刊字符串（全称 / 简称 / 带年份 / 带届次与 "Proceedings of" 前缀）
   归一后映射到 ``venue_level`` 0-4 级；**未命中一律返回 None，禁止猜测或默认值**。
2. 从 arXiv ``comment`` / ``abstract`` 文本中正则抽取 venue 候选与代码仓库链接。

设计约束
--------
- 本模块是纯函数 + 只读配置文件，不访问数据库、不发网络请求。
- 白名单数据只来自 ``config/venue_whitelist.json``；本文件不硬编码任何 venue 等级。
- ``_AMBIGUOUS_ALIASES`` 只负责"降级为精确匹配"，不改变任何等级，用于避免把
  "Asian Conference on Machine Learning" 误判成期刊 "Machine Learning" 之类。
"""

from __future__ import annotations

import json
import logging
import os
import re
import unicodedata
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path
from typing import Any

logger = logging.getLogger("sciloop.wp04.venue")

VENUE_SOURCE_ARXIV = "arxiv_comment"
VENUE_MAX_LEN = 128

# 过于通用、只能精确匹配的别名（含于其他字符串中会误判，例如 "Machine Learning"）。
_AMBIGUOUS_ALIASES: frozenset[str] = frozenset(
    {
        "science",
        "nature",
        "findings",
        "preprint",
        "preprints",
        "submitted",
        "under review",
        "technical report",
        "machine learning",
        "pattern recognition",
        "electronics",
        "sensors",
        "oakland",
        "frontiers media",
        "computing research repository",
    }
)

_PREFIX_STRIPPERS: tuple[str, ...] = (
    "proceedings of the",
    "proceedings of",
    "in proceedings of the",
    "in proceedings of",
    "conference proceedings of the",
    "extended abstracts of the",
    "abstracts of the",
    "annual meeting of the",
    "annual meeting on",
    "annual meeting",
    "annual",
    "the",
)

_SUFFIX_STRIPPERS: tuple[str, ...] = (
    "main conference",
    "main track",
    "research track",
    "technical track",
    "conference track",
    "camera ready",
    "camera-ready",
    "poster",
    "oral",
    "spotlight",
    "accepted",
    "to appear",
    "conference",
    "proceedings",
)

# 抽取 venue 线索：Accepted to / Published in / To appear in / Proceedings of / journal ref
_VENUE_PATTERNS: tuple[re.Pattern[str], ...] = (
    re.compile(
        r"(?:accepted|to\s+appear|appears|appearing|accepted\s+for\s+publication)"
        r"\s+(?:at|in|to)\s+([^.;\n]{2,100})",
        re.IGNORECASE,
    ),
    re.compile(
        r"(?:published|presented)\s+(?:at|in)\s+([^.;\n]{2,100})",
        re.IGNORECASE,
    ),
    re.compile(r"proceedings\s+of\s+(?:the\s+)?([^.;\n]{4,100})", re.IGNORECASE),
    re.compile(r"journal\s+(?:ref|reference)\s*[:=]\s*([^.;\n]{2,100})", re.IGNORECASE),
    re.compile(r"camera[-\s]?ready\s+(?:version\s+)?(?:at|of|for)\s+([^.;\n]{2,100})", re.IGNORECASE),
)

# 未正式录用/不可核对的表述：不作为 venue 抽取结果
_REJECTED_VENUE_PHRASES: tuple[str, ...] = (
    "under review",
    "submitted",
    "anonym",
    "in preparation",
    "preprint under",
    "revisions",
    "rejected",
)

_GITHUB_RE = re.compile(
    r"(?:https?://)?(?:www\.)?github\.com/([A-Za-z0-9_.\-]+)/([A-Za-z0-9_.\-]+)",
    re.IGNORECASE,
)
_GITHUB_NON_REPO_OWNERS: frozenset[str] = frozenset(
    {
        "features",
        "about",
        "topics",
        "explore",
        "search",
        "join",
        "login",
        "settings",
        "apps",
        "marketplace",
        "sponsors",
        "collections",
        "orgs",
        "users",
        "site",
        "pricing",
    }
)
_GITHUB_NON_REPO_PATHS: frozenset[str] = frozenset(
    {"tree", "blob", "issues", "pull", "releases", "wiki", "actions", "commits", "discussions"}
)

_ORDINAL_PREFIX_RE = re.compile(r"^(?:\d{1,3}(?:st|nd|rd|th)|\d{4}|\d{2})\s+")
_YEAR_TOKEN_RE = re.compile(r"\b(?:19|20)\d{2}\b")


# --------------------------------------------------------------------------------------
# 归一
# --------------------------------------------------------------------------------------
def normalize_venue_string(value: Any) -> str:
    """把 venue 字符串归一为小写、无标点、单空格的 token 串。

    ``S&P`` -> ``s and p``；``NeurIPS 2025`` -> ``neurips 2025``。
    """
    if value is None:
        return ""
    text = unicodedata.normalize("NFKD", str(value))
    text = text.encode("ascii", "ignore").decode("ascii")
    text = text.lower().replace("&", " and ")
    text = re.sub(r"[^a-z0-9]+", " ", text)
    return re.sub(r"\s+", " ", text).strip()


def _strip_once(norm: str) -> list[str]:
    """返回对归一字符串做一次剥壳（前缀 / 后缀 / 届次 / 年份）后的候选串。"""
    out: list[str] = []
    for prefix in _PREFIX_STRIPPERS:
        if norm.startswith(prefix + " "):
            out.append(norm[len(prefix) + 1 :].strip())
    for suffix in _SUFFIX_STRIPPERS:
        if norm.endswith(" " + suffix):
            out.append(norm[: -(len(suffix) + 1)].strip())
    out.append(_ORDINAL_PREFIX_RE.sub("", norm).strip())
    out.append(re.sub(r"\s+", " ", _YEAR_TOKEN_RE.sub(" ", norm)).strip())
    return [c for c in out if c]


def venue_candidates(value: Any, max_depth: int = 4) -> list[str]:
    """生成 venue 的候选写法（按改动次数由少到多，BFS 顺序）。"""
    norm = normalize_venue_string(value)
    if not norm:
        return []
    ordered = [norm]
    frontier = [norm]
    seen = {norm}
    for _ in range(max_depth):
        nxt: list[str] = []
        for cur in frontier:
            for cand in _strip_once(cur):
                if cand not in seen:
                    seen.add(cand)
                    ordered.append(cand)
                    nxt.append(cand)
        frontier = nxt
        if not frontier:
            break
    return ordered


# --------------------------------------------------------------------------------------
# 白名单
# --------------------------------------------------------------------------------------
@dataclass(frozen=True)
class VenueWhitelist:
    """白名单索引。``exact_index`` 精确匹配，``contained_pattern`` 词边界包含匹配。"""

    version: str
    levels: Mapping[str, int]
    exact_index: Mapping[str, tuple[str, int]]
    contained_pattern: re.Pattern[str] | None
    contained_lengths: Mapping[str, int]
    source_path: str | None = None

    def lookup(self, value: Any) -> tuple[str, int] | None:
        """返回 ``(canonical, level)``；未命中返回 None。"""
        norm = normalize_venue_string(value)
        if not norm:
            return None
        for cand in venue_candidates(norm):
            hit = self.exact_index.get(cand)
            if hit is not None:
                return hit
        if self.contained_pattern is None:
            return None
        match = self.contained_pattern.search(norm)
        if match is None:
            return None
        return self.exact_index.get(match.group(1))


def _candidate_config_paths() -> list[Path]:
    paths: list[Path] = []
    env_path = os.getenv("VENUE_WHITELIST_FILE")
    if env_path:
        paths.append(Path(env_path))
    here = Path(__file__).resolve()
    for parent in list(here.parents)[:6]:
        paths.append(parent / "config" / "venue_whitelist.json")
    paths.append(Path.cwd() / "config" / "venue_whitelist.json")
    return paths


def _build_whitelist(payload: Mapping[str, Any], source_path: str | None) -> VenueWhitelist:
    exact: dict[str, tuple[str, int]] = {}
    contained: list[str] = []
    lengths: dict[str, int] = {}
    levels: dict[str, int] = {}
    for raw_level, desc in (payload.get("level_definitions") or {}).items():
        try:
            levels[str(raw_level)] = int(raw_level)
        except (TypeError, ValueError):  # pragma: no cover - 配置损坏时忽略
            logger.warning("忽略非法 venue level 定义: %r (%s)", raw_level, desc)
    for entry in payload.get("venues") or []:
        canonical = str(entry.get("canonical") or "").strip()
        level = entry.get("level")
        if not canonical or not isinstance(level, int) or not 0 <= level <= 4:
            logger.warning("忽略非法 venue 条目: %r", entry)
            continue
        names = [canonical, *(entry.get("aliases") or [])]
        for name in names:
            norm = normalize_venue_string(name)
            if not norm:
                continue
            exact.setdefault(norm, (canonical, level))
            if norm in _AMBIGUOUS_ALIASES:
                continue
            contained.append(norm)
            lengths[norm] = len(norm)
    pattern: re.Pattern[str] | None = None
    if contained:
        # 长别名优先，保证同一位置命中更具体的写法（ICML 优先于 "machine learning"）
        alternatives = "|".join(re.escape(a) for a in sorted(set(contained), key=len, reverse=True))
        pattern = re.compile(rf"(?<![a-z0-9])({alternatives})(?![a-z0-9])")
    return VenueWhitelist(
        version=str(payload.get("schema_version") or "unknown"),
        levels=levels,
        exact_index=exact,
        contained_pattern=pattern,
        contained_lengths=lengths,
        source_path=source_path,
    )


def load_whitelist(path: str | os.PathLike[str] | None = None) -> VenueWhitelist:
    """读取白名单。文件缺失或损坏时返回空表并告警（永不抛异常）。"""
    resolved: Path | None = Path(path) if path is not None else None
    if resolved is None:
        for candidate in _candidate_config_paths():
            if candidate.is_file():
                resolved = candidate
                break
    if resolved is None or not resolved.is_file():
        logger.warning("venue 白名单文件未找到（VENUE_WHITELIST_FILE 或 config/venue_whitelist.json）")
        return _build_whitelist({}, None)
    try:
        payload = json.loads(resolved.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        logger.warning("venue 白名单解析失败: %s", exc)
        return _build_whitelist({}, str(resolved))
    if not isinstance(payload, dict):
        logger.warning("venue 白名单结构非法（期望对象）: %s", resolved)
        return _build_whitelist({}, str(resolved))
    return _build_whitelist(payload, str(resolved))


@lru_cache(maxsize=4)
def _cached_whitelist(path_key: str) -> VenueWhitelist:
    return load_whitelist(None if path_key == "" else path_key)


def get_whitelist() -> VenueWhitelist:
    """默认白名单（按 ``VENUE_WHITELIST_FILE`` 缓存）。"""
    return _cached_whitelist(os.getenv("VENUE_WHITELIST_FILE") or "")


def reset_whitelist_cache() -> None:
    """清空缓存（测试或运维重新加载白名单后调用）。"""
    _cached_whitelist.cache_clear()


def resolve_venue(
    venue: Any, whitelist: VenueWhitelist | None = None
) -> tuple[str, int] | None:
    """归一匹配 venue，返回 ``(canonical, level)`` 或 None。"""
    return (whitelist or get_whitelist()).lookup(venue)


def venue_level_of(venue: Any, whitelist: VenueWhitelist | None = None) -> int | None:
    """venue 字符串 -> 0-4 级；未命中返回 None（不猜测）。"""
    hit = resolve_venue(venue, whitelist)
    return hit[1] if hit else None


def canonical_venue(venue: Any, whitelist: VenueWhitelist | None = None) -> str | None:
    """venue 字符串 -> 归一后的规范名；未命中返回 None。"""
    hit = resolve_venue(venue, whitelist)
    return hit[0] if hit else None


# --------------------------------------------------------------------------------------
# 从 comment / abstract 抽取
# --------------------------------------------------------------------------------------
def _clean_candidate(text: str) -> str | None:
    candidate = re.sub(r"https?://\S+", " ", text)
    candidate = candidate.split("(")[0]
    candidate = re.sub(r"[\s,;:]+$", "", candidate)
    candidate = re.sub(r"\s+", " ", candidate).strip(" -–—:,'\"")
    if len(candidate) < 3:
        return None
    lowered = candidate.lower()
    if any(phrase in lowered for phrase in _REJECTED_VENUE_PHRASES):
        return None
    if lowered in {"the", "a", "an", "to", "in", "at"}:
        return None
    return candidate


def extract_venue_candidate(text: Any) -> str | None:
    """从任意文本抽取 venue 候选串（未做白名单校验）。"""
    if not text:
        return None
    raw = str(text)
    for pattern in _VENUE_PATTERNS:
        match = pattern.search(raw)
        if match is None:
            continue
        candidate = _clean_candidate(match.group(1))
        if candidate:
            return candidate
    return None


def extract_code_url(text: Any) -> str | None:
    """从 comment / abstract 抽取 GitHub 仓库地址（``https://github.com/owner/repo``）。"""
    if not text:
        return None
    raw = str(text)
    for match in _GITHUB_RE.finditer(raw):
        owner, repo = match.group(1), match.group(2)
        repo = re.sub(r"\.git$", "", repo)
        repo = repo.strip(".,;:)]}\"'")
        if not owner or not repo:
            continue
        if owner.lower() in _GITHUB_NON_REPO_OWNERS:
            continue
        if owner.lower() in _GITHUB_NON_REPO_PATHS or repo.lower() in _GITHUB_NON_REPO_PATHS:
            continue
        return f"https://github.com/{owner}/{repo}"
    return None


def _truncate(value: str, limit: int = VENUE_MAX_LEN) -> str:
    return value if len(value) <= limit else value[:limit].rstrip()


def resolve_venue_from_text(
    *,
    comment: Any = None,
    abstract: Any = None,
    existing_venue: Any = None,
    existing_source: Any = None,
    whitelist: VenueWhitelist | None = None,
) -> dict[str, Any]:
    """合并多来源的 venue 线索，返回可直接落库的字段。

    优先级：能命中白名单的候选 > 已有多源 venue（s2/openalex）> comment > abstract。
    返回 ``{venue, venue_source, venue_level, code_url, venue_candidates}``；
    全部取不到时 ``venue`` 与 ``venue_level`` 均为 ``None``（不猜测、不填默认值）。
    """
    wl = whitelist or get_whitelist()
    candidates: list[tuple[str, str]] = []
    existing = str(existing_venue).strip() if existing_venue else ""
    if existing:
        candidates.append((existing, str(existing_source or "openalex")))
    comment_candidate = extract_venue_candidate(comment)
    if comment_candidate:
        candidates.append((comment_candidate, VENUE_SOURCE_ARXIV))
    abstract_candidate = extract_venue_candidate(abstract)
    if abstract_candidate and abstract_candidate != comment_candidate:
        candidates.append((abstract_candidate, VENUE_SOURCE_ARXIV))

    chosen: tuple[str, str] | None = None
    chosen_level: int | None = None
    chosen_canonical: str | None = None
    for value, source in candidates:
        hit = wl.lookup(value)
        if hit is not None:
            chosen, chosen_level, chosen_canonical = (value, source), hit[1], hit[0]
            break
    if chosen is None and candidates:
        chosen = candidates[0]

    code_url = extract_code_url(comment) or extract_code_url(abstract)
    return {
        # 命中白名单时落规范名（便于筛选与展示一致），否则落抽取到的原始串以供人工核对
        "venue": (chosen_canonical or _truncate(chosen[0])) if chosen else None,
        "venue_source": chosen[1] if chosen else None,
        "venue_level": chosen_level,
        "code_url": code_url,
        "venue_candidates": [value for value, _ in candidates],
    }


def venue_aliases_of(canonical: str, whitelist: VenueWhitelist | None = None) -> list[str]:
    """返回某规范名的全部已知写法（供调试与前端提示）。"""
    wl = whitelist or get_whitelist()
    norm_canonical = normalize_venue_string(canonical)
    return sorted(name for name, (canon, _) in wl.exact_index.items() if normalize_venue_string(canon) == norm_canonical)


def iter_whitelist_names(whitelist: VenueWhitelist | None = None) -> Iterable[tuple[str, int]]:
    """遍历白名单全部 ``(写法, level)``，用于自检与统计。"""
    wl = whitelist or get_whitelist()
    return sorted(wl.exact_index.items(), key=lambda kv: (kv[1][1], kv[0]))
