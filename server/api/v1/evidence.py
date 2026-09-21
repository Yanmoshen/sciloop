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
"""证据解析端点（WP13-T1 / T5，附录 B.5、E.3）。

===============================  =====================================================
``GET /evidence/{type}/{id}``    把一条多态证据解析为可跳转结构（附录 E.3）
===============================  =====================================================

``type`` 受控值域：``paper_span`` / ``card_field`` / ``experiment_run`` /
``experiment_passport`` / ``decision``（contracts.evidence_rules.evidence_types）。

``id`` 语义由 ``?id_kind=`` 决定
--------------------------------
- ``id_kind=evidence``（默认）：``id`` 是 ``evidences.id``（证据行主键），返回里带
  ``owner_type`` / ``owner_id`` / ``weight``，便于从证据行反查归属。
- ``id_kind=native``：``id`` 是**该类型自身的主键**（``paper_spans.id`` /
  ``paper_cards.id`` / ``experiment_runs.id`` / ``experiment_passports.id`` /
  ``decision_logs.id``）。此时 ``evidences`` 表里可能还没有对应行——
  这正是「证据尚未绑定但原文可定位」的场景，供 WP15 的证据抽屉直接查看原文。

红线（逐条落实）
----------------
- 返回值里 ``document_version`` 必填，定位由 ``span{section_name,page_number,bbox,
  char_start,char_end}`` 复合给出；**禁止把裸字符偏移当作唯一定位依据**。
- ``verification`` 直接来自 WP05 的 ``verify_span``，判定顺序是**哈希优先**：
  文本一致但偏移漂移 → ``valid_by_hash``；``quote_sha256`` 未命中 → ``invalid``。
- ``gate`` 如实披露 ``fulltext_gate``：``parse_status != 'ok'`` 或 ``coverage<0.60``
  时 ``ok=false`` 且 ``evidence_scope='abstract_only'``，**绝不宣称正文级证据**。
- 查不到的字段一律 ``null``（不编造），并用 ``warnings`` 说明原因。
- 纯 GET，``public_demo`` 面可用，无写操作。
"""

from __future__ import annotations

import logging
from collections.abc import AsyncIterator
from typing import Annotated, Any

from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy.ext.asyncio import AsyncSession

from db.session import AsyncSessionLocal
from services.evidence import (
    EVIDENCE_TYPES,
    EvidenceError,
    EvidenceNotFoundError,
    InvalidEvidenceTypeError,
    resolve_evidence,
)

logger = logging.getLogger("sciloop.wp13.evidence_api")

router = APIRouter(tags=["evidence"])

TYPE_HELP = "受控证据类型之一：" + " / ".join(EVIDENCE_TYPES)


async def _session() -> AsyncIterator[AsyncSession]:
    if AsyncSessionLocal is None:  # pragma: no cover - 部署期驱动缺失
        raise _error(
            status.HTTP_503_SERVICE_UNAVAILABLE,
            "database_unavailable",
            "异步数据库会话工厂不可用（DATABASE_URL / asyncpg 未就绪）",
        )
    async with AsyncSessionLocal() as session:
        yield session


DbSession = Annotated[AsyncSession, Depends(_session)]


def _error(status_code: int, code: str, message: str, detail: Any = None) -> HTTPException:
    return HTTPException(
        status_code=status_code,
        detail={"code": code, "message": message, "detail": detail},
    )


def _flatten(payload: dict[str, Any]) -> dict[str, Any]:
    """补一组**平铺别名**，让前端证据抽屉不必层层解包（附录 E.3 结构保持不变）。

    ``DraftViewer``（WP15）按平铺字段读取 ``verdict`` / ``coverage`` /
    ``section_name`` / ``char_start`` 等；此处集中转换，避免前端复制解析逻辑。
    """
    span = payload.get("span") or {}
    verification = payload.get("verification") or {}
    gate = payload.get("gate") or {}
    paper = payload.get("paper") or {}
    payload["id"] = payload.get("evidence_id")
    payload["paper_id"] = paper.get("id") if paper else None
    payload["verdict"] = verification.get("verdict")
    payload["coverage"] = gate.get("coverage")
    payload["spans_allowed"] = bool(gate.get("ok"))
    payload["section_name"] = span.get("section_name") if span else None
    payload["page_number"] = span.get("page_number") if span else None
    payload["char_start"] = span.get("char_start") if span else None
    payload["char_end"] = span.get("char_end") if span else None
    payload["note"] = gate.get("coverage_note")
    return payload


@router.get(
    "/evidence/{evidence_type}/{evidence_id}",
    summary="多态证据解析（附录 E.3：paper / span / quote / verification / jump_url）",
)
async def get_evidence(
    evidence_type: str,
    evidence_id: int,
    session: DbSession,
    id_kind: str = Query(
        "evidence",
        description=(
            "evidence=evidences.id（默认）；native=该类型自身主键"
            "（paper_spans.id / paper_cards.id / experiment_runs.id / "
            "experiment_passports.id / decision_logs.id）"
        ),
        pattern="^(evidence|native)$",
    ),
    card_field: str = Query(
        "core_method",
        description=(
            "仅 id_kind=native 且 evidence_type=card_field 时生效："
            "paper_cards 是 8 字段宽表，需显式指明引用哪个字段"
            "（research_problem/core_method/key_innovation/technical_route/"
            "experimental_setup/main_conclusions/limitations/transferable）"
        ),
    ),
) -> dict[str, Any]:
    """解析一条证据；``paper_span`` / ``card_field`` 一律走哈希优先校验。"""
    try:
        payload = await resolve_evidence(
            evidence_type,
            int(evidence_id),
            session=session,
            id_kind=id_kind,
            card_field=card_field,
        )
    except InvalidEvidenceTypeError as exc:
        raise _error(
            status.HTTP_400_BAD_REQUEST, exc.code, exc.message, exc.detail
        ) from exc
    except EvidenceNotFoundError as exc:
        raise _error(
            status.HTTP_404_NOT_FOUND, exc.code, exc.message, exc.detail
        ) from exc
    except EvidenceError as exc:
        raise _error(
            status.HTTP_422_UNPROCESSABLE_ENTITY, exc.code, exc.message, exc.detail
        ) from exc

    payload = _flatten(payload)
    payload["id_kind"] = id_kind
    logger.info(
        "get_evidence type=%s id=%s id_kind=%s verdict=%s scope=%s",
        evidence_type,
        evidence_id,
        id_kind,
        payload.get("verdict"),
        payload.get("evidence_scope"),
    )
    return payload


@router.get(
    "/evidence/types",
    summary="证据类型受控值域（供前端渲染类型选择器）",
)
async def list_evidence_types() -> dict[str, Any]:
    """返回受控值域与论文片段级证据的准入闸门，避免前端硬编码。"""
    from services.parsing.locator import FULLTEXT_GATE_COVERAGE

    return {
        "evidence_types": list(EVIDENCE_TYPES),
        "id_kinds": ["evidence", "native"],
        "fulltext_gate": {
            "parse_status": "ok",
            "min_coverage": FULLTEXT_GATE_COVERAGE,
            "rule": "只有 parse_status='ok' 且 coverage>=0.60 才允许生成 paper_span 证据；"
            "其余只允许摘要级证据并在 UI 明示",
        },
    }
