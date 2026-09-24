# Copyright 2026 SciLoop contributors
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.
"""多格式导出：EasyPaper ↔ SciLoop 字段映射声明与公共工具。

本模块是导出域的**唯一映射事实来源**：所有导出格式（JSON / BibTeX / CSL-JSON /
Obsidian ZIP / CSV ZIP）共用这里的元数据构造、占位值清洗、跳过空字段与安全化工具。

红线（contracts.forbidden_actions）
----------------------------------
- **禁止编造数据**：EasyPaper 有、SciLoop 没有的字段一律输出空值并在
  :data:`MAPPING_NOTES` / ``notes`` 里说明；取不到的标量写 ``null``，
  **绝不填占位字符串**（``"unknown"`` 是卡片域对"论文未报告"的如实标注，
  导出时会被 :func:`real_value` 清洗为 ``None``）。
- 文件名/条目名安全化：禁止路径分隔符、控制字符、``..`` 与绝对路径（防 zip 路径穿越）。
"""

from __future__ import annotations

import hashlib
import json
import re
import unicodedata
from collections.abc import Mapping, Sequence
from datetime import date, datetime
from typing import Any

#: 导出 JSON 结构版本（EasyPaper §5.4 同形；本字段为 SciLoop 自有版本号）
SCHEMA_VERSION = "1.0.0"

#: SciLoop 当前访问面（单 Owner 演示面，无多用户隔离）
ACCESS_MODE = "public_demo"

#: 访问面说明（每个导出的顶层都要如实声明）
SCOPE_NOTE = (
    "本导出为单 Owner 演示面（public_demo）的公开数据，不含用户私有数据"
    "——SciLoop 无多用户/私有数据概念，故不按用户过滤，也不存在越权读取。"
)

#: 合规声明（contracts.ui_mandatory_elements.ComplianceBanner）
DISCLAIMER = "本内容由 AI 辅助生成，需研究者自行核验"

#: 卡片域对"论文未报告"的如实占位（导出时必须清洗掉，不得当作真实值）。
#: ⚠️ 卡片自 2026-09-24 起用中文占位「未提及」（原为 ``unknown``）—— 漏一个的后果是
#: 占位值被当成真实数据导出给下游。改占位口径时**必须同步** ``locator.UNKNOWN_VALUES``、
#: 本集合、以及 ``feasibility.scorer`` 的占位集合这三处。
PLACEHOLDER_TOKENS: frozenset[str] = frozenset(
    {"unknown", "未知", "未提及", "n/a", "na", "none", "null", "-", "--", ""}
)

#: EasyPaper ↔ SciLoop 字段映射表（每个导出产物都会带上）
MAPPING_NOTES: tuple[dict[str, str], ...] = (
    {
        "easypaper_field": "metadata",
        "sciloop_source": "papers（title/authors/published_at/doi/venue/source/external_id/pdf_url/citation_count）",
        "status": "mapped",
        "note": "arxiv_id 取自 paper_identities(id_type='arxiv') 或 papers.source='arxiv' 时的 external_id",
    },
    {
        "easypaper_field": "structure",
        "sciloop_source": "paper_documents + paper_spans",
        "status": "mapped",
        "note": "sections 由 paper_spans.section_name 聚合派生；定位以 quote_sha256 优先于字符偏移",
    },
    {
        "easypaper_field": "findings",
        "sciloop_source": "draft_claims + evidences（owner_type='draft_claim'）",
        "status": "mapped",
        "note": "按 evidences.paper_id 把 Claim 归属到论文；三态 supported/contradicted/insufficient + status_reason 原样输出",
    },
    {
        "easypaper_field": "methods",
        "sciloop_source": "paper_cards.core_method / key_innovation / technical_route（该论文最新版本）",
        "status": "mapped",
        "note": "methods 为卡片字段的结构化视图，非独立方法表",
    },
    {
        "easypaper_field": "datasets",
        "sciloop_source": "paper_cards.experimental_setup.datasets",
        "status": "mapped",
        "note": "无卡片时输出 []，不推断",
    },
    {
        "easypaper_field": "metrics",
        "sciloop_source": "paper_cards.experimental_setup.metrics",
        "status": "mapped",
        "note": "指标名为论文自述名称，不含数值（SciLoop 无数值指标表）",
    },
    {
        "easypaper_field": "flashcards",
        "sciloop_source": "无（SciLoop 无 Flashcard/SRS 域）",
        "status": "empty",
        "note": "SciLoop 不存在闪卡与间隔重复（SRS）数据表，本字段恒为 []，不做任何补造",
    },
    {
        "easypaper_field": "annotations",
        "sciloop_source": "无（SciLoop 无用户批注表）",
        "status": "empty",
        "note": "SciLoop 不存在 UserAnnotation/阅读器批注表，本字段恒为 []",
    },
    {
        "easypaper_field": "global_entities",
        "sciloop_source": "派生自 paper_cards.experimental_setup.{datasets,baselines,metrics}",
        "status": "derived",
        "note": "每条带 derived_from（表名 + 行 id + 字段路径）；entity_id 为派生标识，非表行 id",
    },
    {
        "easypaper_field": "global_relationships",
        "sciloop_source": "派生自 paper_cards（论文→数据集/基线/指标）与 evidences（Claim→论文）",
        "status": "derived",
        "note": "只输出有真实来源行的关系；paper_cards 无 confidence 列时 confidence 如实置 null",
    },
    {
        "easypaper_field": "PaperKnowledge / KnowledgeEntity / KnowledgeRelationship / ObsidianSyncMapping",
        "sciloop_source": "无对应表",
        "status": "empty",
        "note": "SciLoop 无知识库根对象/实体表/关系表/本地 Vault 同步表；导出内容全部由上述真实表当场派生，不落库",
    },
)

#: ``evidences.owner_type`` 中草稿 Claim 的取值（WP13 契约）
CLAIM_OWNER_TYPE = "draft_claim"

#: 全文闸门阈值（contracts.evidence_rules.fulltext_gate）
FULLTEXT_GATE_COVERAGE = 0.60

_LOCATOR_NOTE = (
    "定位契约（contracts.evidence_rules）：先比对 quote_sha256，再比对字符偏移；"
    "哈希一致即可用，偏移漂移不影响证据有效性。"
)


def mapping_notes() -> list[dict[str, str]]:
    """返回映射表的可序列化副本（避免调用方误改模块级常量）。"""
    return [dict(item) for item in MAPPING_NOTES]


def locator_note() -> str:
    """定位口径说明（哈希优先）。"""
    return _LOCATOR_NOTE


# --------------------------------------------------------------------------- #
# 基础类型转换：取不到就 None，不填默认值
# --------------------------------------------------------------------------- #
def iso(value: Any) -> str | None:
    """日期/时间 → ISO 字符串；``None`` 保持 ``None``。"""
    if value is None:
        return None
    if isinstance(value, datetime | date):
        return value.isoformat()
    if hasattr(value, "isoformat"):
        return str(value.isoformat())
    return str(value)


def to_int(value: Any) -> int | None:
    """尽力转 int；失败返回 ``None``（不填 0 冒充）。"""
    if value is None or isinstance(value, bool):
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def to_float(value: Any) -> float | None:
    """尽力转 float；失败返回 ``None``（不填 0.0 冒充）。"""
    if value is None or isinstance(value, bool):
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def decode_json(value: Any, default: Any) -> Any:
    """JSONB 列解码：asyncpg 下可能返回 ``str``，psycopg 下已解析。"""
    if value is None:
        return default
    if isinstance(value, str):
        try:
            return json.loads(value)
        except (TypeError, ValueError):
            return default
    return value


def real_value(value: Any) -> Any:
    """占位值（``unknown`` / ``n/a`` / 空串 / ``-``）→ ``None``。

    卡片域用 ``"unknown"`` 如实标注"论文未报告"；导出时必须清洗，
    否则会把占位串当成真实数据。
    """
    if value is None:
        return None
    if isinstance(value, str):
        stripped = value.strip()
        return None if stripped.lower() in PLACEHOLDER_TOKENS else stripped
    return value


def string_list(value: Any) -> list[str]:
    """把 JSONB 值规整为去占位的字符串列表（支持 ``str``/``list``/``dict``）。"""
    decoded = value if isinstance(value, list) else decode_json(value, [])
    if isinstance(decoded, str):
        cleaned = real_value(decoded)
        return [cleaned] if cleaned else []
    if not isinstance(decoded, Sequence):
        return []
    items: list[str] = []
    for entry in decoded:
        if isinstance(entry, Mapping):
            candidate = None
            for key in ("name", "label", "title", "id", "value"):
                candidate = real_value(entry.get(key))
                if candidate is not None:
                    break
            entry = candidate
        cleaned = real_value(entry)
        if cleaned is not None:
            items.append(str(cleaned))
    return items


# --------------------------------------------------------------------------- #
# 作者与年份
# --------------------------------------------------------------------------- #
_NAME_PARTICLES: frozenset[str] = frozenset(
    {
        "van", "von", "de", "der", "den", "la", "le", "di", "da", "del", "della",
        "bin", "al", "el", "ter", "ten", "st", "st.", "mac", "mc", "ibn", "du",
    }
)


def author_rows(authors: Any) -> list[dict[str, Any]]:
    """``papers.authors`` → ``[{name, affiliation}]``（保留真实值，缺失置 null）。"""
    decoded = authors if isinstance(authors, list) else decode_json(authors, [])
    if not isinstance(decoded, Sequence):
        return []
    rows: list[dict[str, Any]] = []
    for entry in decoded:
        if isinstance(entry, Mapping):
            name = real_value(entry.get("name") or entry.get("display_name"))
            if name is None:
                continue
            rows.append(
                {
                    "name": str(name),
                    "affiliation": real_value(entry.get("affiliation")),
                }
            )
        else:
            name = real_value(entry)
            if name is not None:
                rows.append({"name": str(name), "affiliation": None})
    return rows


def split_author_name(name: Any) -> dict[str, str]:
    """作者全名 → CSL 的 ``family``/``given``；拆不出来退回 ``literal``。

    规则（保守，不做语言猜测）：
    ``"Vaswani, Ashish"`` → 逗号前为姓；``"Ashish Vaswani"`` → 末段为姓；
    姓中含常见介词（van/von/de…）时一并纳入；单段名 → ``literal``。
    """
    raw = str(name or "").strip()
    if not raw:
        return {}
    if "," in raw:
        family, _, given = raw.partition(",")
        family, given = family.strip(), given.strip()
        if family and given:
            return {"family": family, "given": given}
        if family:
            return {"family": family}
        return {"literal": raw}
    tokens = raw.split()
    if len(tokens) < 2:
        return {"literal": raw}
    index = len(tokens) - 1
    while index - 1 > 0 and tokens[index - 1].lower() in _NAME_PARTICLES:
        index -= 1
    family = " ".join(tokens[index:])
    given = " ".join(tokens[:index])
    if not family or not given:
        return {"literal": raw}
    return {"family": family, "given": given}


def csl_authors(authors: Any) -> list[dict[str, str]]:
    """CSL-JSON 的 ``author`` 数组。"""
    parsed: list[dict[str, str]] = []
    for row in author_rows(authors):
        name = split_author_name(row["name"])
        if name:
            parsed.append(name)
    return parsed


def first_family_name(authors: Any) -> str | None:
    """第一作者的姓（用于生成 cite key）。"""
    for row in author_rows(authors):
        parsed = split_author_name(row["name"])
        family = parsed.get("family") or parsed.get("literal")
        if family:
            return str(family)
    return None


def paper_year(published_at: Any) -> int | None:
    """``papers.published_at`` → 年份（取不到返回 ``None``）。"""
    if isinstance(published_at, datetime | date):
        return int(published_at.year)
    value = real_value(published_at)
    if value is None:
        return None
    match = re.match(r"^(\d{4})", str(value))
    return int(match.group(1)) if match else None


# --------------------------------------------------------------------------- #
# BibTeX 转义
# --------------------------------------------------------------------------- #
#: 单遍字符映射（**不做顺序替换**：顺序替换会把新引入的 ``\`` / ``{}`` 再转义一次）
_BIBTEX_CHAR_MAP: dict[str, str] = {
    "\\": r"\textbackslash{}",
    "&": r"\&",
    "%": r"\%",
    "$": r"\$",
    "#": r"\#",
    "_": r"\_",
    "{": r"\{",
    "}": r"\}",
    "~": r"\textasciitilde{}",
    "^": r"\textasciicircum{}",
}


def escape_bibtex(text: Any) -> str:
    """转义 BibTeX 特殊字符 ``& % $ # _ { } ~ ^ \\``。

    采用**单遍字符映射**：每个字符只被替换一次，避免把替换结果里新引入的
    ``\\``、``{}`` 再转义一遍（例如 ``\\`` → ``\\textbackslash{}`` 而不是
    ``\\textbackslash\\{\\}``）。
    """
    source = "" if text is None else str(text)
    return "".join(_BIBTEX_CHAR_MAP.get(char, char) for char in source)


_ALNUM_RE = re.compile(r"[^0-9a-zA-Z]+")


def _slug(text: Any, *, limit: int = 24) -> str:
    return _ALNUM_RE.sub("", str(text or "")).lower()[:limit]


def bibtex_cite_key(meta: Mapping[str, Any], *, used: set[str] | None = None) -> str:
    """稳定且唯一的 cite key：``姓+年`` + arXiv id（无 arXiv 用 external_id）。

    稳定性：同一篇论文（同一 paper_id）永远得到同一个 base key。
    唯一性：base key 与既有 key 冲突时追加 ``-<paper_id>``（paper_id 固定，
    因此仍然稳定；不会因导出顺序不同而漂移）。
    """
    surname = _slug(first_family_name(meta.get("authors")), limit=20) or "anon"
    year = str(meta.get("year") or "nd")
    tail_source = meta.get("arxiv_id") or meta.get("external_id") or ""
    tail = _slug(tail_source)
    base = f"{surname}{year}{tail}" or f"paper{meta.get('paper_id')}"
    if used is None:
        return base
    key = base
    if key in used:
        key = f"{base}-{meta.get('paper_id')}"
    suffix = 2
    while key in used:
        key = f"{base}-{meta.get('paper_id')}-{suffix}"
        suffix += 1
    used.add(key)
    return key


# --------------------------------------------------------------------------- #
# 文件名 / zip 条目安全化（防路径穿越）
# --------------------------------------------------------------------------- #
_UNSAFE_FILENAME_RE = re.compile(r'[\\/:*?"<>|\x00-\x1f\x7f]')


def safe_filename(name: Any, *, fallback: str = "untitled", max_length: int = 80) -> str:
    """文件名安全化：去路径分隔符与控制字符、压平空白、截断长度、拒绝 ``..``。"""
    text = unicodedata.normalize("NFKC", str(name or ""))
    text = _UNSAFE_FILENAME_RE.sub("_", text)
    text = text.replace("..", "_")
    text = re.sub(r"\s+", " ", text).strip()
    text = text.strip(". ")
    if len(text) > max_length:
        text = text[:max_length].strip(". ")
    if not text or text in {".", ".."}:
        return fallback
    return text


def short_hash(text: Any) -> str:
    """短哈希（重名去重后缀用的稳定摘要）。"""
    return hashlib.sha1(str(text).encode("utf-8")).hexdigest()[:6]


def unique_name(name: str, used: set[str], *, salt: Any = "") -> str:
    """重名处理：首次直接用，冲突时追加短哈希后缀；再冲突追加序号。"""
    if name not in used:
        used.add(name)
        return name
    digest = short_hash(salt if salt != "" else name)
    candidate = f"{name}-{digest}"
    counter = 2
    while candidate in used:
        candidate = f"{name}-{digest}-{counter}"
        counter += 1
    used.add(candidate)
    return candidate


def safe_zip_entry(path: str) -> str:
    """zip 内条目名安全化：丢弃绝对路径前缀与 ``..`` 段。"""
    normalized = str(path or "").replace("\\", "/").lstrip("/")
    parts = [part for part in normalized.split("/") if part not in {"", ".", ".."}]
    return "/".join(parts) or "entry"


__all__ = [
    "ACCESS_MODE",
    "CLAIM_OWNER_TYPE",
    "DISCLAIMER",
    "FULLTEXT_GATE_COVERAGE",
    "MAPPING_NOTES",
    "PLACEHOLDER_TOKENS",
    "SCHEMA_VERSION",
    "SCOPE_NOTE",
    "author_rows",
    "bibtex_cite_key",
    "csl_authors",
    "decode_json",
    "escape_bibtex",
    "first_family_name",
    "iso",
    "locator_note",
    "mapping_notes",
    "paper_year",
    "real_value",
    "safe_filename",
    "safe_zip_entry",
    "short_hash",
    "split_author_name",
    "string_list",
    "to_float",
    "to_int",
    "unique_name",
]
