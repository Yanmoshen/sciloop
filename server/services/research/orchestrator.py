# Copyright 2026 SciLoop contributors
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
"""研究节点编排器：程序主控的执行闭环。

一次节点执行
------------
``取上下文 → 渲染提示词 → 调模型 → 解析 → 校验 → 通过则迁移 / 不通过则驳回重跑``

模型是被调方。它输出的任何 ``completed`` / ``exit_code`` 之类声明**都不改变状态**：
``preflight`` 字段一律由程序用**真实预检记录覆盖**，校验器通过之后才可能迁移。

两类拒绝，处置不同
------------------
``L1``（格式 / 解析 / 外键）  自动重跑，缺项回灌提示词
``L2``（质量 / 硬规则）       驳回重跑，本节点本次进入最多 2 次；第 3 次置 ``waiting_human``

回退路径
--------
模型可以在产出里填 ``revert_request`` 建议回退。程序**不判断该不该退**，
只过三类闸门（G1 必带信息 / G2 次数上限 / G3 落痕）。G1 不通过时，
回退申请本身变成一条**校验缺项**（回灌模型补齐），而不是静默放行或静默拒绝。
"""

from __future__ import annotations

import asyncio
import datetime
import json
import logging
from collections.abc import AsyncIterator
from dataclasses import dataclass
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from db.models.aggregation import Evidence, Idea
from db.models.feasibility import Feasibility, Taskbook
from db.models.research import IMPLEMENTED_NODES, ResearchNodeRun
from llm.adapter import chat
from llm.errors import LLMAuthError, LLMBadRequestError
from llm.providers import looks_like_schema_rejection
from llm.schema import load_json_payload
from services.research import graph, prompts, store
from services.research import preflight as preflight_mod
from services.research.contracts import (
    NODE_OUTPUT_MODELS,
    NODE_OUTPUT_SCHEMAS,
    ExperimentPrepOutput,
    IdeaAndFeasibilityOutput,
    LiteratureReviewOutput,
)
from services.research.rules import (
    LibraryFacts,
    RuleHit,
    ValidationResult,
    validate_node_output,
)

logger = logging.getLogger("sciloop.research.orchestrator")

__all__ = [
    "NODE_KEYWORDS",
    "chain_state",
    "current_node",
    "detect_node",
    "resolve_entry_index",
    "resolve_model_ref",
    "run_node",
]

#: 自然语言 → 节点（自动判定；判定结果前端必须回显，且可手动改）
NODE_KEYWORDS: dict[str, tuple[str, ...]] = {
    "literature_review": ("文献", "调研", "检索", "相关论文", "综述", "literature", "survey", "查论文"),
    "idea_and_feasibility": ("idea", "想法", "假设", "可行性", "创新点", "构想", "选题"),
    "experiment_and_data_preparation": (
        "实验准备", "数据准备", "实验设计", "协议", "基线", "数据集", "预检",
    ),
    "experiment_execution_and_retries": ("执行实验", "跑实验", "运行实验", "重试"),
    "results_analysis": ("结果分析", "分析结果", "结果解读"),
    "paper_writing": ("写论文", "撰写", "草稿", "论文写作"),
    "paper_review": ("评审", "审稿", "同行评议", "review"),
}

#: 推进顺序（单一事实来源在 graph，也就是 app/db/models/research.py）
_DEFAULT_ORDER: tuple[str, ...] = graph.NODE_ORDER

#: 节点输出的 token 上限。
#: 为什么需要显式给：默认 1536 会把契约 JSON 截断在中间（实测 finish_reason=length），
#: 截断的 JSON 一定解析失败，于是节点会在「模型其实答得挺好」的情况下被反复驳回。
NODE_MAX_TOKENS = 8000

#: 已探明「不支持 response_format=json_schema」的模型（进程内记忆）。
#: 实测：某些供应商会直接拒绝 schema 档，适配层随即降级去试 env 兜底模型；
#: 而 env 的 Key 可能是空的 —— 一次本来能成的调用就这样变成硬失败。
#: 因此首次带 schema 试一次，失败就把该模型记下来、本轮改用纯文本模式重试，
#: **不占用节点的修复重试额度**。能从上游拿到结构化输出时仍然优先用它。
#: ⚠️ 只记「**确实是上游拒绝 schema 档**」这一种失败（见 ``_is_schema_rejection``）：
#: 早先无差别地把任何异常都记进来，于是网络抖动 / 认证失败 / 解析失败都会让该模型
#: 在进程余下的生命周期里**永久失去**结构化输出档位。
_SCHEMA_UNSUPPORTED: set[str] = set()


def _is_schema_rejection(exc: BaseException) -> bool:
    """这次失败是不是「上游不接受 ``response_format`` 档位」。"""

    if not isinstance(exc, LLMBadRequestError):
        return False
    detail = getattr(exc, "detail", None)
    body = f"{exc} {detail if isinstance(detail, str) else ''}"
    return looks_like_schema_rejection(getattr(exc, "status_code", None) or 400, body)


#: 「没被收尾的 running」多久算已中断（**读侧**判定用）。
#: 量级参考：单次尝试实测上限约 40s（8000 token 上限），最多 3 次尝试加每层降档重发，
#: 正常最坏也在 3 分钟内。15 分钟足够宽，不会把真在跑的节点误报成中断。
STALE_RUN_SECONDS = 15 * 60


def _interrupted_if_stale(
    row: dict[str, Any], *, now: datetime.datetime
) -> dict[str, Any]:
    """读取时把「超时仍未收尾的 ``running``」报成已中断。

    为什么要读侧也兜一道：节点行一旦卡在 ``running``（客户端断连、进程被替换），
    控制台的流光会**一直转**、像还在跑（实测有卡了 15 小时的行）。
    这里**不改库**，只在读取时按 ``updated_at`` 超时判定，并把原因写成一条校验缺项，
    让界面显示红点「已中断」而不是永远进行中；重试入口照旧可用。
    """

    if str(row.get("status")) != "running":
        return row
    seen = row.get("updated_at") or row.get("started_at")
    if not isinstance(seen, datetime.datetime):
        return row
    if seen.tzinfo is None:  # 兼容历史行里的 naive 时间戳
        seen = seen.replace(tzinfo=datetime.UTC)
    idle = (now - seen).total_seconds()
    if idle < STALE_RUN_SECONDS:
        return row

    stale = dict(row)
    stale["status"] = "failed"
    stale["interrupted"] = True
    stale["validation"] = {
        "ok": False,
        "level": "L1",
        "rules": ["interrupted"],
        "items": [
            {
                "rule": "interrupted",
                "level": "L1",
                "path": None,
                "message": (
                    f"执行已中断约 {int(idle // 60)} 分钟（连接断开或进程退出），"
                    "这一轮没有产出结论——可以直接重试。"
                ),
            }
        ],
    }
    return stale


@dataclass
class _ProjectView:
    id: int
    name: str
    settings: dict[str, Any]

    @property
    def execution_access(self) -> str:
        value = str(self.settings.get("execution_access") or "ask")
        return value if value in ("ask", "trusted") else "ask"


def _utcnow() -> datetime.datetime:
    return datetime.datetime.now(datetime.UTC)


async def resolve_model_ref(stage: str | None = None, project_id: int | None = None) -> str | None:
    """解析本次调用实际可用的模型引用。

    为什么需要它：本节点的 stage 名（如 ``literature_review``）在
    ``stage_model_routing`` 里通常**没有**对应的行，而 ``.env`` 的
    ``LLM_DEFAULT_API_KEY`` 可能为空。此时若直接让适配层走 env 兜底，
    只会拿到「未配置 API Key」而不是真的调用模型。

    解析顺序：

    1. ``stage_model_routing`` 里该环节 / 该项目的路由（人工显式指定优先）；
    2. ``model_configs`` 中标记为默认、且**确实带 Key** 的供应商；
    3. 第一个带 Key 的供应商。

    都拿不到就返回 ``None``——**如实失败，不拿空 Key 去撞 401**。
    """

    from llm.registry import get_registry
    from llm.types import slugify_provider

    try:
        registry = get_registry()
    except Exception as exc:  # noqa: BLE001 - 供应商注册表不可用
        logger.warning("模型供应商注册表不可用：%s", exc)
        return None

    if stage:
        try:
            routing = await registry.get_routing(stage, project_id)
        except Exception as exc:  # noqa: BLE001
            logger.warning("读取环节路由失败 stage=%s：%s", stage, exc)
            routing = None
        if routing is not None:
            config = await registry.get_config(routing.model_config_id)
            if config is not None and (config.api_key_enc or ""):
                return f"{slugify_provider(config.name)}:{routing.model_id}"

    try:
        configs = list(await registry.list_configs())
    except Exception as exc:  # noqa: BLE001
        logger.warning("读取供应商列表失败：%s", exc)
        return None

    usable = [c for c in configs if (c.api_key_enc or "") and (c.models or [])]
    for record in sorted(usable, key=lambda c: (not c.is_default, getattr(c, "id", 0))):
        for entry in record.models or []:
            model_id = entry.get("model_id") if isinstance(entry, dict) else None
            if model_id:
                return f"{slugify_provider(record.name)}:{model_id}"
    return None


# --------------------------------------------------------------------------- #
# 读取与判定
# --------------------------------------------------------------------------- #
def detect_node(text: str, *, fallback: str | None = None) -> tuple[str, str]:
    """从研究者一句话判定归属节点。返回 ``(node, 命中词)``。

    判定只是「建议」，前端必须回显并允许手动改——判错时必须看得见。
    """

    lowered = (text or "").lower()
    best_node, best_keyword, best_len = "", "", 0
    for node, keywords in NODE_KEYWORDS.items():
        for keyword in keywords:
            if keyword and keyword in lowered and len(keyword) > best_len:
                best_node, best_keyword, best_len = node, keyword, len(keyword)
    if best_node:
        return best_node, best_keyword
    return fallback or _DEFAULT_ORDER[0], ""


async def _load_project(session: AsyncSession, project_id: int) -> _ProjectView | None:
    from db.models.project import Project

    row = (
        await session.execute(select(Project).where(Project.id == project_id))
    ).scalar_one_or_none()
    if row is None:
        return None
    settings = dict(row.settings) if isinstance(row.settings, dict) else {}
    return _ProjectView(id=int(row.id), name=row.name, settings=settings)


async def _latest_run(
    session: AsyncSession, *, conversation_id: str, node: str
) -> dict[str, Any] | None:
    stmt = (
        select(ResearchNodeRun)
        .where(ResearchNodeRun.conversation_id == conversation_id, ResearchNodeRun.node == node)
        .order_by(ResearchNodeRun.entry_index.desc())
        .limit(1)
    )
    row = (await session.execute(stmt)).scalar_one_or_none()
    return store.row_to_dict(row) if row is not None else None


async def current_node(session: AsyncSession, *, conversation_id: str) -> str:
    """当前应当执行的节点。

    判定顺序：

    1. **最近一次迁移是回退** → 回到那个目标节点。
       必须放第一位：回退的语义就是「回到前面重做」，如果这里还按「已完成的最大序号 + 1」
       去算，界面会出现「模型已回退到文献调研，面板却写着当前是实验准备」这种自相矛盾。
    2. 否则取推进顺序里**最靠后的已完成节点**的下一个；
    3. 一个都没完成 → 第一个节点。
    """

    latest = await store.list_transitions(session, conversation_id=conversation_id, limit=1)
    if latest:
        row = latest[0]
        if str(row.get("kind")) == "revert":
            target = str(row.get("to_node") or "")
            if target in _DEFAULT_ORDER:
                return target
        if str(row.get("kind")) == "stop":
            return str(row.get("to_node") or _DEFAULT_ORDER[0])

    runs = await store.list_node_runs(session, conversation_id=conversation_id)
    last_index = -1
    for row in runs:
        if str(row.get("status")) != "done":
            continue
        node = str(row.get("node"))
        if node in _DEFAULT_ORDER:
            last_index = max(last_index, _DEFAULT_ORDER.index(node))
    if last_index < 0:
        return _DEFAULT_ORDER[0]
    return graph.next_node(_DEFAULT_ORDER[last_index]) or _DEFAULT_ORDER[last_index]


async def resolve_entry_index(
    session: AsyncSession, *, conversation_id: str, node: str
) -> int:
    """本次进入的 ``entry_index``。

    - 从未进入 → 1
    - 上一次是 ``running`` / ``waiting_human`` → 复用（幂等续跑，不新建进入）
    - 上一次已 ``done`` / ``failed`` / ``blocked`` → 递增（回退或重跑）
    """

    latest = await _latest_run(session, conversation_id=conversation_id, node=node)
    if latest is None:
        return 1
    current = int(latest.get("entry_index") or 1)
    if str(latest.get("status")) in ("running", "waiting_human"):
        return current
    return current + 1


async def _upstream_payload(
    session: AsyncSession, *, conversation_id: str, node: str
) -> dict[str, Any]:
    """上游成果。

    两种情况都要覆盖：

    1. **正常推进**：取推进顺序里位于本节点之前、且已完成的最近一个节点；
    2. **回退重做**：起点节点（如文献调研）前面本来没有上游，但它是被后面某个节点
       回退回来的——这时必须把**提出回退的那个节点**的成果带进来，
       否则「回去补文献」这句指令就丢了上下文，等于白退。
    """

    if node not in _DEFAULT_ORDER:
        return {}
    index = _DEFAULT_ORDER.index(node)
    for prev in reversed(_DEFAULT_ORDER[:index]):
        latest = await _latest_run(session, conversation_id=conversation_id, node=prev)
        if latest and str(latest.get("status")) == "done":
            return {
                "node": prev,
                "label": graph.NODE_LABELS.get(prev, prev),
                "entry_index": latest.get("entry_index"),
                "payload": latest.get("payload") or {},
            }

    # 回退场景：取下游里最近完成的一个（通常就是提出回退的那个节点）
    best: tuple[int, dict[str, Any]] | None = None
    for later in _DEFAULT_ORDER[index + 1 :]:
        latest = await _latest_run(session, conversation_id=conversation_id, node=later)
        if latest and str(latest.get("status")) == "done":
            order_key = int(latest.get("id") or 0)
            if best is None or order_key > best[0]:
                best = (
                    order_key,
                    {
                        "node": later,
                        "label": graph.NODE_LABELS.get(later, later),
                        "entry_index": latest.get("entry_index"),
                        "payload": latest.get("payload") or {},
                        "via": "revert",
                    },
                )
    return best[1] if best is not None else {}


async def chain_state(
    session: AsyncSession, *, conversation_id: str, project_id: int | None = None
) -> dict[str, Any]:
    """整条链的状态（前端状态条与状态查询都用它）。"""

    runs = await store.list_node_runs(session, conversation_id=conversation_id)
    by_node: dict[str, dict[str, Any]] = {}
    now = _utcnow()
    for row in runs:
        # 超时仍未收尾的 running → 如实报成「已中断」（不改库，只改读取口径）
        by_node[str(row["node"])] = _interrupted_if_stale(row, now=now)  # 后者覆盖前者 = 取最新进入

    nodes: list[dict[str, Any]] = []
    for node in _DEFAULT_ORDER:
        row = by_node.get(node) or {}
        status = str(row.get("status") or "pending")
        nodes.append(
            {
                "node": node,
                "label": graph.NODE_LABELS.get(node, node),
                "status": status,
                "display_status": graph.display_status(status),
                "implemented": node in IMPLEMENTED_NODES,
                "entry_index": int(row.get("entry_index") or 0),
                "retry_count": int(row.get("retry_count") or 0),
                "max_retry": graph.MAX_RETRY_PER_ENTRY,
                "cost_usd": float(row.get("cost_usd") or 0),
                "llm_call_count": int(row.get("llm_call_count") or 0),
                "validation": row.get("validation"),
                "finished_at": row.get("finished_at"),
            }
        )

    return {
        "conversation_id": conversation_id,
        "project_id": project_id,
        "has_chain": bool(runs),
        "nodes": nodes,
        "current_node": await current_node(session, conversation_id=conversation_id),
        "transitions": await store.list_transitions(
            session, conversation_id=conversation_id, limit=30
        ),
        "total_reverts": await store.count_total_reverts(
            session, conversation_id=conversation_id
        ),
        "max_total_reverts": graph.MAX_TOTAL_REVERTS,
        "workdir": preflight_mod.workdir_key(conversation_id, project_id),
        "preflight": preflight_mod.latest_preflight(conversation_id, project_id),
    }


# --------------------------------------------------------------------------- #
# 持久化业务实体（全部复用既有表）
# --------------------------------------------------------------------------- #
async def _persist_evidences(
    session: AsyncSession, *, node_run_id: int, items: list[Any]
) -> list[int]:
    """文献调研的证据写进既有 ``evidences`` 表（``owner_type='research_node'``）。"""

    ids: list[int] = []
    for item in items:
        row = Evidence(
            owner_type="research_node",
            owner_id=node_run_id,
            evidence_type=str(getattr(item, "evidence_type", "method")),
            paper_id=getattr(item, "paper_id", None),
            paper_span_id=getattr(item, "paper_span_id", None),
            card_field=getattr(item, "card_field", None),
            quote_text=getattr(item, "quote_text", None),
        )
        session.add(row)
        await session.flush()
        ids.append(int(row.id))
    return ids


async def _persist_idea_and_feasibility(
    session: AsyncSession,
    *,
    project_id: int | None,
    node_run_id: int,
    out: IdeaAndFeasibilityOutput,
) -> dict[str, Any]:
    """idea 与可行性写进既有 ``ideas`` / ``feasibilities`` 表。

    ``ideas.project_id`` 可空，所以**无项目对话也能落**：产出如实归属这次研究，
    不替用户造项目。
    """

    hyp = out.hypothesis
    idea = Idea(
        project_id=project_id,
        origin="research_node",
        title=hyp.statement[:200],
        content=json.dumps(
            {
                "statement": hyp.statement,
                "scope": hyp.scope,
                "baseline": hyp.baseline,
                "mechanism": hyp.mechanism,
                "expected_direction": hyp.expected_direction,
                "falsification_condition": hyp.falsification_condition,
                "effect_size": hyp.effect_size,
                "node_run_id": node_run_id,
            },
            ensure_ascii=False,
        ),
        # mechanism 列为 String(32)：超长截断，不靠数据库报错兜底
        mechanism=(hyp.mechanism[:32] if hyp.mechanism else None),
        novelty_note=("；".join(f"论文 {n.paper_id}：{n.difference}" for n in out.novelty_delta[:3]))
        or None,
        is_selected=True,
    )
    session.add(idea)
    await session.flush()

    feasibility = Feasibility(
        idea_id=int(idea.id),
        data_availability=out.feasibility.data_availability or {},
        compute_cost=out.feasibility.compute_cost or {},
        method_maturity=out.feasibility.method_maturity or {},
        novelty_gap=out.feasibility.novelty_gap or {},
        total_score=float(out.feasibility.total_score or 0),
        risk_list=list(out.feasibility.risk_list or []),
        mve_plan=out.feasibility.mve_plan or {},
    )
    session.add(feasibility)
    await session.flush()
    return {"idea_id": int(idea.id), "feasibility_id": int(feasibility.id)}


async def _persist_taskbook(
    session: AsyncSession,
    *,
    project_id: int | None,
    node_run_id: int,
    out: ExperimentPrepOutput,
    question: str,
) -> dict[str, Any]:
    """实验协议写进既有 ``taskbooks``（校验通过即 locked）。

    ``taskbooks.project_id`` 是 **NOT NULL**（既有表结构，本层不改它），
    所以无项目对话**落不了任务书**——这时如实返回 ``no_project`` 而不是
    偷偷造一个项目，也不假装写过。
    """

    if project_id is None:
        return {"taskbook_id": None, "taskbook_skipped": "no_project"}

    idea = (
        await session.execute(
            select(Idea).where(Idea.project_id == project_id).order_by(Idea.id.desc()).limit(1)
        )
    ).scalar_one_or_none()
    if idea is None:
        # 没有 idea 就不落 taskbook（idea_id NOT NULL）；如实返回原因，不编造关联
        return {"taskbook_id": None, "taskbook_skipped": "no_idea_for_project"}

    taskbook = Taskbook(
        project_id=project_id,
        idea_id=int(idea.id),
        research_question=question or "（未填写研究问题）",
        target_datasets=[d.model_dump() for d in out.datasets],
        baselines=[b.model_dump() for b in out.baselines],
        metrics=[m.model_dump() for m in out.metrics],
        compute_budget={
            "text": out.resources.compute_budget,
            "hours": out.resources.estimated_hours,
        },
        deliverables={
            "falsification": out.falsification.model_dump(),
            "node_run_id": node_run_id,
        },
        status="locked",
        locked_at=_utcnow(),
    )
    session.add(taskbook)
    await session.flush()
    return {"taskbook_id": int(taskbook.id)}


async def _persist_outputs(
    session: AsyncSession,
    *,
    node: str,
    project_id: int | None,
    node_run_id: int,
    payload: dict[str, Any],
    question: str,
) -> dict[str, Any]:
    """按节点把产出写进既有业务表。返回引用 id，供追溯。"""

    refs: dict[str, Any] = {}
    try:
        if node == "literature_review":
            out = LiteratureReviewOutput.model_validate(payload)
            refs["evidence_ids"] = await _persist_evidences(
                session, node_run_id=node_run_id, items=out.evidence
            )
        elif node == "idea_and_feasibility":
            out = IdeaAndFeasibilityOutput.model_validate(payload)
            refs.update(
                await _persist_idea_and_feasibility(
                    session, project_id=project_id, node_run_id=node_run_id, out=out
                )
            )
        elif node == "experiment_and_data_preparation":
            out = ExperimentPrepOutput.model_validate(payload)
            refs.update(
                await _persist_taskbook(
                    session, project_id=project_id, node_run_id=node_run_id, out=out,
                    question=question,
                )
            )
        await session.commit()
    except Exception as exc:  # noqa: BLE001 - 落库失败不能伪装成节点成功
        await session.rollback()
        logger.exception("节点产出落库失败 node=%s", node)
        refs["persist_error"] = str(exc)[:300]
    return refs


async def _collect_facts(session: AsyncSession, payload: dict[str, Any]) -> LibraryFacts:
    """收集产出里引用的全部论文编号，并查清卡片字段 / 原文片段是否真实存在。

    只查「编号存在」是不够的：模型可以写一个不存在的 `card_field`，
    校验就会放行一条指向空处的证据。事实必须来自数据库。
    """

    ids: list[int] = []
    for item in payload.get("evidence") or []:
        if isinstance(item, dict) and item.get("paper_id") is not None:
            ids.append(int(item["paper_id"]))
    for item in payload.get("closest_work") or []:
        if isinstance(item, dict) and item.get("paper_id") is not None:
            ids.append(int(item["paper_id"]))
    for item in payload.get("novelty_delta") or []:
        if isinstance(item, dict) and item.get("paper_id") is not None:
            ids.append(int(item["paper_id"]))
    for item in payload.get("gaps") or []:
        if isinstance(item, dict):
            for pid in item.get("raised_by_paper_ids") or []:
                if pid is not None:
                    ids.append(int(pid))
    return await store.library_facts(session, ids)


def _inject_preflight(candidate: dict[str, Any], real: dict[str, Any] | None) -> None:
    """程序用真实预检记录覆盖模型声明（模型无法伪造执行证据）。"""

    if real is None:
        candidate["preflight"] = {
            "level": "researcher_script",
            "command": "",
            "exit_code": -1,
            "duration_ms": 0,
            "artifact_path": None,
            "log_path": "",
            "note": "尚未执行小规模预检（由程序核验，模型无法代为声明）",
        }
        return
    candidate["preflight"] = {
        "level": str(real.get("level") or "researcher_script"),
        "command": str(real.get("command") or ""),
        "exit_code": int(real.get("exit_code") or 0),
        "duration_ms": int(real.get("duration_ms") or 0),
        "artifact_path": real.get("artifact_path"),
        "log_path": str(real.get("log_path") or ""),
        "note": str(real.get("note") or ""),
    }


def _parse_lenient(raw: str) -> dict[str, Any] | None:
    """宽松解析：剥 ```json 围栏、截取首个 ``{`` 到最后一个 ``}``。

    适配层用自己的严格校验决定「这次调用算不算成功」；本函数只负责在
    **原始文本仍然可用**时把它取出来。取出来的内容照样要过 R1–R14，
    所以放宽解析不会放宽判定标准。
    """

    if not raw:
        return None
    payload, _error = load_json_payload(raw)
    return payload if isinstance(payload, dict) else None


def _salvage_from_json_error(exc: Exception) -> str | None:
    """从 ``LLMJSONValidationError`` 里取出最后一次原始响应（取不到返回 None）。"""

    raw_responses = getattr(exc, "raw_responses", None)
    if not raw_responses:
        return None
    for text in reversed(list(raw_responses)):
        if isinstance(text, str) and text.strip():
            return text
    return None


async def _budget_snapshot(
    session: AsyncSession, *, conversation_id: str, node: str, retry_count: int
) -> dict[str, Any]:
    return {
        "retry_count": retry_count,
        "max_retry": graph.MAX_RETRY_PER_ENTRY,
        "revisit_count": await store.count_revisits(session, conversation_id=conversation_id, node=node),
        "max_revisit": graph.MAX_REVISIT_PER_NODE,
        "total_reverts": await store.count_total_reverts(session, conversation_id=conversation_id),
        "max_total_reverts": graph.MAX_TOTAL_REVERTS,
    }


# --------------------------------------------------------------------------- #
# 主循环
# --------------------------------------------------------------------------- #
async def run_node(
    session_factory: Any,
    *,
    conversation_id: str,
    node: str | None = None,
    text: str = "",
    project_id: int | None = None,
) -> AsyncIterator[tuple[str, dict[str, Any]]]:
    """执行一次节点（生成器，逐条产出事件）。

    事件：``meta`` / ``node`` / ``attempt`` / ``validation`` / ``notice`` /
    ``revert`` / ``migrated`` / ``waiting_human`` / ``error`` / ``done``

    **链挂在对话上**，所以本函数**不要求项目存在**：对话可以完全没有项目。
    ``project_id`` 只在拿得到时作为元信息写入，并用于读取项目级执行授权；
    拿不到就按 ``ask`` 处理——不替用户造项目，也不因此拦住他。

    失败一律如实上报：任何异常都转成 ``error`` 事件，**不返回伪造的成功**。

    这一层只做一件事：**保证那行 ``running`` 一定被收尾**。
    真正的执行在 ``_run_node_events``；这里包一层，是因为上游多是 SSE——
    客户端切页 / 关抽屉 / 浏览器回收连接都会**取消**本生成器，而取消走的是
    ``BaseException``（``CancelledError``），原先没人管，于是库里留下一行
    永远 ``running`` 的僵尸（实测最长卡了 15 小时，控制台流光一直转）。
    """

    entered: dict[str, Any] = {}
    settled = False
    source = _run_node_events(
        session_factory,
        conversation_id=conversation_id,
        node=node,
        text=text,
        project_id=project_id,
    )
    try:
        async for event, data in source:
            if event == "node":
                entered = {
                    "node": data.get("node"),
                    "entry_index": data.get("entry_index"),
                }
            elif event in ("done", "error"):
                # ``done`` = 正常收尾；``error`` 之前各失败分支都已落过终态
                settled = True
            yield event, data
    except BaseException:
        if entered and not settled:
            await _settle_interrupted(
                session_factory,
                conversation_id=conversation_id,
                project_id=project_id,
                node=str(entered.get("node") or ""),
                entry_index=int(entered.get("entry_index") or 1),
            )
        raise


async def _settle_interrupted(
    session_factory: Any,
    *,
    conversation_id: str,
    project_id: int | None,
    node: str,
    entry_index: int,
) -> None:
    """把被中断的那行 ``running`` 落成 ``failed`` 并留痕（尽力而为）。

    用 ``asyncio.shield``：调用方此刻多半已经处于**取消**状态，不 shield 的话
    收尾的第一次 ``await`` 会立刻再抛 ``CancelledError``，这行就还是收不了尾。
    收尾本身失败也**绝不掩盖**原始异常（只记日志）。
    """

    async def _write() -> None:
        async with session_factory() as session:
            latest = await _latest_run(session, conversation_id=conversation_id, node=node)
            if str((latest or {}).get("status")) != "running":
                return  # 已被正常路径收尾，别覆盖真实结论
            label = graph.NODE_LABELS.get(node, node)
            await store.upsert_node_run(
                session,
                conversation_id=conversation_id,
                project_id=project_id,
                node=node,
                entry_index=entry_index,
                status="failed",
                validation={
                    "ok": False,
                    "level": "L1",
                    "rules": ["interrupted"],
                    "items": [
                        {
                            "rule": "interrupted",
                            "level": "L1",
                            "path": None,
                            "message": (
                                "执行被中断（连接断开或进程退出），本次没有产出结论——可以直接重试。"
                            ),
                        }
                    ],
                },
                finished_at=_utcnow(),
            )
            await store.record_transition(
                session,
                conversation_id=conversation_id,
                project_id=project_id,
                from_node=node,
                to_node=node,
                kind="stop",
                trigger="program",
                reason=f"「{label}」执行被中断（客户端断开或进程退出），已如实标记为失败",
            )

    try:
        await asyncio.shield(_write())
    except BaseException:  # noqa: BLE001 - 收尾是尽力而为，绝不掩盖原始异常
        logger.warning(
            "中断收尾失败 conversation=%s node=%s", conversation_id, node, exc_info=True
        )


async def _run_node_events(
    session_factory: Any,
    *,
    conversation_id: str,
    node: str | None = None,
    text: str = "",
    project_id: int | None = None,
) -> AsyncIterator[tuple[str, dict[str, Any]]]:
    """节点执行主体（事件语义见 ``run_node`` 的文档字符串）。"""

    async with session_factory() as session:
        project = await _load_project(session, project_id) if project_id else None
        project_name = project.name if project is not None else ""
        execution_access = project.execution_access if project is not None else "ask"

        fallback = await current_node(session, conversation_id=conversation_id)
        detected, keyword = detect_node(text, fallback=fallback)
        target = node or detected
        if target not in _DEFAULT_ORDER:
            yield "error", {
                "code": "unknown_node",
                "message": f"未知节点：{target}",
                "allowed": list(_DEFAULT_ORDER),
            }
            return

        entry_index = await resolve_entry_index(
            session, conversation_id=conversation_id, node=target
        )
        max_retry = graph.MAX_RETRY_PER_ENTRY
        budget = await _budget_snapshot(
            session, conversation_id=conversation_id, node=target, retry_count=0
        )
        model_ref = await resolve_model_ref(target, project_id)

        yield "meta", {
            "conversation_id": conversation_id,
            "project_id": project_id,
            "project_name": project_name,
            "requested_node": node,
            "detected_node": detected,
            "detect_keyword": keyword,
            "node": target,
            "node_label": graph.NODE_LABELS.get(target, target),
            "entry_index": entry_index,
            "implemented": target in IMPLEMENTED_NODES,
            "execution_access": execution_access,
            "max_retry": max_retry,
            "budget": budget,
            "model_ref": model_ref,
        }

        if model_ref is None:
            await store.upsert_node_run(
                session,
                conversation_id=conversation_id,
                project_id=project_id,
                node=target,
                entry_index=entry_index,
                status="failed",
                retry_count=0,
                validation={
                    "ok": False,
                    "level": "L1",
                    "rules": ["model_not_configured"],
                    "items": [
                        {
                            "rule": "model",
                            "level": "L1",
                            "message": "没有可用的模型供应商（未配置 API Key）",
                            "path": None,
                        }
                    ],
                },
            )
            yield "error", {
                "code": "no_model_configured",
                "message": "没有可用的模型供应商：请在设置页填写供应商与 API Key 后重试",
                "node": target,
            }
            return

        await store.upsert_node_run(
            session,
            conversation_id=conversation_id,
            project_id=project_id,
            node=target,
            entry_index=entry_index,
            status="running",
            retry_count=0,
            validation=None,
            started_at=_utcnow(),
        )
        yield "node", {
            "node": target,
            "node_label": graph.NODE_LABELS.get(target, target),
            "entry_index": entry_index,
            "status": "running",
            "implemented": target in IMPLEMENTED_NODES,
        }

        upstream = await _upstream_payload(session, conversation_id=conversation_id, node=target)
        research_question = str((upstream.get("payload") or {}).get("research_question") or text or "")
        library = await store.library_overview(session)
        hits = await store.search_library(session, query=text or research_question)
        real_preflight = preflight_mod.latest_preflight(conversation_id, project_id)

        # 程序只报事实，**不替研究者或模型决定跑不跑**（2026-09-22 用户明确要求）。
        # 缺研究问题、论文库没命中，这些都会被如实写进提示词交给模型；
        # 由模型决定是继续、换个说法再检索、去网上找，还是回头问研究者。
        # 原来的写法是"程序直接拦下并转人工"，那等于用程序替模型做了判断。
        if len(research_question.strip()) < 4 and not upstream:
            yield "notice", {
                "node": target,
                "code": "missing_research_question",
                "message": (
                    "还没拿到研究问题。这一点会如实告诉模型，由它决定是追问你，"
                    "还是先按现有信息推进。"
                ),
            }
        if not hits:
            yield "notice", {
                "node": target,
                "code": "no_library_hits",
                "message": (
                    f"论文库里没有命中与「{research_question[:40]}」相关的材料。"
                    "这一点会如实告诉模型（它可以换个说法再检索、去网上找，"
                    "或者直接告诉你库里缺什么），跑不跑由它决定。"
                ),
            }

    payload: dict[str, Any] | None = None
    total_cost = 0.0
    llm_calls = 0
    last_raw = ""
    last_validation: ValidationResult | None = None
    repair: str | None = None
    # 循环的出口只有两个：模型说完成（break）或模型要人介入（return）。
    # 不再有"重试用尽"这个由程序判定出来的出口。

    # **不设次数上限**：由模型决定什么时候停（2026-09-22 研究者明确要求「让模型自己决定
    # 什么时候停下、什么时候需要人工介入」）。程序这一层的职责只剩三件：
    #   ① 把产出摆成能落库的形状（契约不过 = 产出不可用，必须再来一轮）；
    #   ② 把"验收到了什么"如实整理成事实（只报事实，不判通过）；
    #   ③ 读模型自己的决定：done / continue / need_human。
    attempt = 0
    contract_misses = 0
    self_checks = 0
    self_check_note: dict[str, Any] | None = None
    advisories: list[str] = []
    #: 模型要求搜的结果累积在这里，下一轮作为事实塞进提示词
    search_notes: list[dict[str, Any]] = []
    while True:
        yield "attempt", {
            "node": target,
            "attempt": attempt + 1,
            "max_attempts": None,
            "retry_count": attempt,
            "library_hits": len(hits or []),
            "paper_total": library.get("paper_total"),
        }

        messages = prompts.build_messages(
            node=target,
            user_text=text,
            research_question=research_question,
            project_name=project_name,
            upstream=upstream,
            library=library,
            hits=hits or [],
            budget={**budget, "retry_count": attempt},
            repair=repair,
            search_results=search_notes,
        )

        result: Any = None
        candidate: dict[str, Any] | None = None
        salvage_note: str | None = None
        llm_failed: Exception | None = None

        # 两层：schema 档一次 + （若被拒）纯文本档一次。后者不消耗节点重试额度。
        for schema_try in (0, 1):
            want_schema = schema_try == 0 and model_ref not in _SCHEMA_UNSUPPORTED
            try:
                result = await chat(
                    messages,
                    model_ref,
                    None,
                    NODE_MAX_TOKENS,
                    NODE_OUTPUT_SCHEMAS.get(target) if want_schema else None,
                    project_id=project_id,
                    stage=graph.stage_alias(target),
                    purpose=graph.purpose_for(target),
                    # 本层已自行解析出**带 Key** 的模型（resolve_model_ref），不再使用适配层的
                    # env 兜底：实测 env 的 Key 可能为空，主模型只要应答不理想就会去试它，
                    # 结果把一次本可重试的调用变成硬认证失败。
                    allow_fallback=False,
                )
                llm_failed = None
                break
            except Exception as exc:  # noqa: BLE001 - 失败如实上报，不做假成功
                llm_failed = exc
                if want_schema and _is_schema_rejection(exc):
                    # **确实是上游拒绝 schema 档**才记：改用纯文本模式再试一次
                    # （同一轮，不扣额度）。别把网络抖动 / 认证失败 / 解析失败也记进来，
                    # 那会让该模型在进程余下的生命周期里永久失去结构化输出档位。
                    _SCHEMA_UNSUPPORTED.add(model_ref)
                    yield "notice", {
                        "node": target,
                        "code": "schema_downgraded",
                        "message": "上游不接受结构化输出档位，已改用纯文本模式；契约仍由本地校验保证",
                        "detail": str(exc)[:240],
                    }
                    continue
                # 纯文本档也失败：尝试从异常里抢救原始文本
                salvaged = _salvage_from_json_error(exc)
                if salvaged is None:
                    break
                last_raw = salvaged
                salvage_note = str(exc)[:300]
                candidate = _parse_lenient(salvaged)
                llm_failed = None
                break

        llm_calls += 1

        if llm_failed is not None:
            logger.warning("节点模型调用失败 node=%s：%s", target, llm_failed)

            # 认证 / 配置类失败重试也没用（拿空 Key 再撞两次只会白花钱）→ 立即如实失败。
            # 其余失败（供应商拒绝结构化输出、上游抖动、超时）**算一次 L1 并消耗本节点的
            # 重试额度**再继续，而不是让一次调用抖动直接终结整个节点。
            # 认证/配置类失败重试没有意义（拿空 Key 再撞只会白花钱）；
            # 其余失败（供应商抖动、超时）继续交给模型，**不再受次数上限约束**。
            recoverable = not isinstance(llm_failed, LLMAuthError)
            if recoverable:
                llm_calls += 0  # 已在上面计过
                failure = ValidationResult(
                    ok=False,
                    level="L1",
                    hits=[
                        RuleHit(
                            "llm",
                            "L1",
                            f"第 {attempt + 1} 次模型调用失败：{str(llm_failed)[:240]}",
                            None,
                        )
                    ],
                )
                last_validation = failure
                yield "validation", {
                    "node": target,
                    "node_label": graph.NODE_LABELS.get(target, target),
                    "attempt": attempt + 1,
                    "max_attempts": None,
                    "ok": False,
                    "level": "L1",
                    "items": [h.to_dict() for h in failure.hits],
                }
                async with session_factory() as session:
                    await store.upsert_node_run(
                        session,
                        conversation_id=conversation_id,
                        project_id=project_id,
                        node=target,
                        entry_index=entry_index,
                        status="running",
                        retry_count=attempt + 1,
                        raw_output=last_raw[:20000],
                        validation=failure.to_dict(),
                        llm_call_count=llm_calls,
                        cost_usd=round(total_cost, 6),
                    )
                    budget = await _budget_snapshot(
                        session, conversation_id=conversation_id, node=target, retry_count=attempt + 1
                    )
                repair = (
                    "上一次模型调用没有成功返回可用结果。请直接输出符合契约的完整 JSON 对象，"
                    "不要输出解释文字，也不要使用代码围栏。"
                )
                continue

            async with session_factory() as session:
                await store.upsert_node_run(
                    session,
                    conversation_id=conversation_id,
                    project_id=project_id,
                    node=target,
                    entry_index=entry_index,
                    status="failed",
                    retry_count=attempt,
                    validation={
                        "ok": False,
                        "level": "L1",
                        "rules": ["llm"],
                        "items": [
                            {
                                "rule": "llm",
                                "level": "L1",
                                "message": str(llm_failed)[:300],
                                "path": None,
                            }
                        ],
                    },
                    llm_call_count=llm_calls,
                    cost_usd=round(total_cost, 6),
                )
            yield "error", {
                "code": getattr(llm_failed, "code", "llm_failed") or "llm_failed",
                "message": str(llm_failed)[:400],
                "node": target,
            }
            return

        if result is not None:
            if result.cost_usd is not None:
                total_cost += float(result.cost_usd)
            last_raw = result.content or ""
            candidate = result.parsed if isinstance(result.parsed, dict) else None
            if candidate is None:
                candidate = _parse_lenient(last_raw)
                if candidate is not None and salvage_note is None:
                    salvage_note = "适配层未直接给出结构化结果，已改用宽松解析"

        if salvage_note:
            yield "notice", {
                "node": target,
                "code": "schema_salvaged",
                "message": "严格结构化输出未通过，已改用宽松解析；内容仍需通过硬规则校验",
                "detail": salvage_note,
            }

        if candidate is not None and target == "experiment_and_data_preparation":
            _inject_preflight(candidate, real_preflight)

        async with session_factory() as session:
            facts = (
                await _collect_facts(session, candidate) if candidate is not None else None
            )
        validation = validate_node_output(target, candidate, facts=facts)

        # 回退申请：G1「必须带齐信息」是**结构性**要求（不然回退过去也做不下去），
        # 因此仍然保留；研究质量类的判定一律不再拦截（见下面的 advisories）。
        if candidate is not None:
            revert = candidate.get("revert_request")
            if isinstance(revert, dict) and revert.get("target"):
                gate = graph.check_revert_gate(
                    current_node=target,
                    target=str(revert.get("target")),
                    carried=revert.get("carried"),
                    retry_count=attempt,
                    revisit_count=int(budget.get("revisit_count") or 0),
                    total_reverts=int(budget.get("total_reverts") or 0),
                )
                if not gate.ok:
                    level = "L1" if gate.gate == "G1" else "L2"
                    validation = ValidationResult(
                        ok=False,
                        level=level,
                        hits=[
                            RuleHit(gate.gate or "G1", level, gate.message, "revert_request")
                        ],
                    )

        last_validation = validation

        # ---- ① 提醒：把验收情况如实说出来（对话一行 + 节点详情一份）-------- #
        advisories = _advisory_notes(validation)
        if advisories:
            yield "notice", {
                "node": target,
                "code": "validator_findings",
                "message": "本轮验收情况（只报事实，是否算完成由你判断）：\n"
                + "\n".join(advisories),
            }

        # ---- ② 产出能不能用，只看契约（能不能落库），不看研究质量 ---------- #
        usable, normalized, why = _contract_payload(target, candidate)
        if not usable:
            contract_misses += 1
            yield "notice", {
                "node": target,
                "code": "contract_miss",
                "message": f"这一版产出还不能落库（{why}），已经把要补齐的地方回给你了。",
            }
            if contract_misses >= CONTRACT_MISS_LIMIT:
                # 防挂死：连续多次给不出可落库的产出，如实停下等人 ——
                # 这不是"研究质量判定"，而是"这台机器现在产不出可用的东西"。
                reason = (
                    f"连续 {CONTRACT_MISS_LIMIT} 次未能给出可落库的产出（{why}），已停下等你处理"
                )
                await _hand_off_to_human(
                    session_factory,
                    conversation_id=conversation_id,
                    project_id=project_id,
                    node=target,
                    entry_index=entry_index,
                    retry_count=attempt,
                    reason=reason,
                    validation=last_validation,
                    raw_output=last_raw,
                    llm_calls=llm_calls,
                    total_cost=total_cost,
                    budget_snapshot=budget,
                    extra={
                        "advisory": True,
                        "decided_by": "program",
                        "state": "contract_miss",
                        "self_check": self_check_note,
                    },
                )
                yield "waiting_human", {
                    "node": target,
                    "node_label": graph.NODE_LABELS.get(target, target),
                    "retry_count": attempt,
                    "items": [h.to_dict() for h in (last_validation.hits if last_validation else [])],
                    "message": reason,
                }
                yield "done", {
                    "node": target,
                    "status": "waiting_human",
                    "display_status": graph.display_status("waiting_human"),
                    "cost_usd": round(total_cost, 6),
                    "llm_call_count": llm_calls,
                }
                return
            repair = (
                f"上一版产出不能直接用：{why}。请按契约重新输出**完整**的 JSON 对象，"
                "不要输出解释文字，也不要使用代码围栏。"
            )
            attempt += 1
            continue
        contract_misses = 0

        # ---- 替模型搜：它给了搜索词就搜（**搜什么、要不要搜都由它定**）------ #
        # ⚠️ 位置放在状态判断**之前**：即使它同一轮说 done / need_human，
        # 只要它把搜索词写出来了，那就是它要求的事实采集 ——
        # 研究者也正好能看见"为了这个结论我搜过什么、搜到了什么"。
        wanted = candidate.get("search_queries") if isinstance(candidate, dict) else None
        queries = (
            [str(item).strip() for item in wanted if str(item).strip()]
            if isinstance(wanted, list)
            else []
        )[:MAX_SEARCH_QUERIES]
        if queries:
            found = await _run_searches(queries)
            search_notes.extend(found)
            total = sum(len(block.get("results") or []) for block in found)
            yield "notice", {
                "node": target,
                "code": "web_search",
                "message": (
                    f"按你的要求上网搜了 {len(queries)} 组关键词、共 {total} 条结果"
                    "（是网页摘要，引用前请核对链接）。"
                    if total
                    else f"按你的要求搜了 {len(queries)} 组关键词，但没搜到结果。"
                ),
            }

        # ---- ③ 决策权在模型手里：done / continue / need_human ------------- #
        state = str((candidate or {}).get("state") or "").strip().lower() or "done"

        if state == "need_human":
            pending = candidate.get("pending") if isinstance(candidate.get("pending"), list) else []
            reason = str((candidate or {}).get("state_reason") or "").strip()
            if not reason:
                reason = "模型判断本节点需要研究者介入"
                if pending:
                    reason += "：" + "；".join(str(item) for item in pending[:5])
            await _hand_off_to_human(
                session_factory,
                conversation_id=conversation_id,
                project_id=project_id,
                node=target,
                entry_index=entry_index,
                retry_count=attempt,
                reason=reason,
                validation=last_validation,
                raw_output=last_raw,
                llm_calls=llm_calls,
                total_cost=total_cost,
                budget_snapshot=budget,
                extra={
                    "advisory": True,
                    "decided_by": "model",
                    "state": "need_human",
                    "self_check": self_check_note,
                },
            )
            yield "waiting_human", {
                "node": target,
                "node_label": graph.NODE_LABELS.get(target, target),
                "retry_count": attempt,
                "items": advisories,
                "message": reason,
            }
            yield "done", {
                "node": target,
                "status": "waiting_human",
                "display_status": graph.display_status("waiting_human"),
                "cost_usd": round(total_cost, 6),
                "llm_call_count": llm_calls,
            }
            return

        if state == "continue":
            pending = candidate.get("pending") if isinstance(candidate.get("pending"), list) else []
            lines = ["你自己判断本节点还没完成。"]
            if pending:
                lines.append("你说还缺的：" + "；".join(str(item) for item in pending[:8]))
            if advisories:
                lines.append("本轮验收情况（事实，供你判断）：\n" + "\n".join(advisories))
            lines.append("请继续做完，并在下一次产出的 state 里写 done 或 need_human。")
            repair = "\n".join(lines)
            attempt += 1
            continue

        # ---- 模型说完成 → **再让同一个模型回头自查一遍**（研究者点名要的）-- #
        payload = normalized if isinstance(normalized, dict) else (candidate or {})
        if self_checks < SELF_CHECK_LIMIT:
            self_checks += 1
            check = await _run_self_check(
                node=target,
                model_ref=model_ref,
                payload=payload,
                research_question=research_question,
                advisories=advisories,
                library=library,
                project_id=project_id,
            )
            llm_calls += 1
            if check:
                total_cost += float(check.get("cost_usd") or 0)
                self_check_note = {
                    "state": str(check.get("state") or "done"),
                    "summary": str(check.get("summary") or "")[:400],
                    "issues": [str(item)[:300] for item in (check.get("issues") or [])][:10],
                }
                verdict = self_check_note["state"].lower()
                summary_line = self_check_note["summary"] or "（没给结论）"
                yield "notice", {
                    "node": target,
                    "code": "self_check",
                    "message": f"自检（同一个模型回头看了一遍）：{summary_line}",
                }
                if verdict == "continue":
                    issues = self_check_note["issues"] or ["自检认为还有该做的事没做完"]
                    repair = (
                        "你刚才判定完成，但**回头自查时发现还没做完**。请把下面这些补齐后再交：\n"
                        + "\n".join(f"· {item}" for item in issues)
                    )
                    attempt += 1
                    continue
                if verdict == "need_human":
                    reason = (
                        "自检认为需要研究者介入："
                        + (summary_line if summary_line != "（没给结论）" else "缺只有研究者能提供的信息")
                    )
                    await _hand_off_to_human(
                        session_factory,
                        conversation_id=conversation_id,
                        project_id=project_id,
                        node=target,
                        entry_index=entry_index,
                        retry_count=attempt,
                        reason=reason,
                        validation=last_validation,
                        raw_output=last_raw,
                        llm_calls=llm_calls,
                        total_cost=total_cost,
                        budget_snapshot=budget,
                        extra={
                            "advisory": True,
                            "decided_by": "model",
                            "state": "need_human",
                            "self_check": self_check_note,
                        },
                    )
                    yield "waiting_human", {
                        "node": target,
                        "node_label": graph.NODE_LABELS.get(target, target),
                        "retry_count": attempt,
                        "items": [h.to_dict() for h in (last_validation.hits if last_validation else [])],
                        "message": reason,
                    }
                    yield "done", {
                        "node": target,
                        "status": "waiting_human",
                        "display_status": graph.display_status("waiting_human"),
                        "cost_usd": round(total_cost, 6),
                        "llm_call_count": llm_calls,
                    }
                    return
            else:
                yield "notice", {
                    "node": target,
                    "code": "self_check_skipped",
                    "message": "自检这一步没跑成（调用失败或返回看不懂），已跳过，不影响本轮结论。",
                }

        # 收工（程序不再用硬规则拦它）
        async with session_factory() as session:
            await store.upsert_node_run(
                session,
                conversation_id=conversation_id,
                project_id=project_id,
                node=target,
                entry_index=entry_index,
                status="done",
                retry_count=attempt,
                payload=payload,
                raw_output=last_raw[:20000],
                validation={
                    "ok": validation.ok,
                    "level": validation.level,
                    "rules": [h.rule for h in validation.hits],
                    "items": [h.to_dict() for h in validation.hits],
                    # 标记清楚：这份校验结果是**提醒**，不是通过与否的判据
                    "advisory": True,
                    "decided_by": "model",
                    "state": "done",
                    "self_check": self_check_note,
                },
                llm_call_count=llm_calls,
                cost_usd=round(total_cost, 6),
                finished_at=_utcnow(),
            )
        break


    # 通过：落业务实体 + 处理迁移
    async with session_factory() as session:
        run = await _latest_run(session, conversation_id=conversation_id, node=target)
        node_run_id = int((run or {}).get("id") or 0)
        refs = await _persist_outputs(
            session,
            node=target,
            project_id=project_id,
            node_run_id=node_run_id,
            payload=payload or {},
            question=research_question,
        )

        revert = (payload or {}).get("revert_request")
        if isinstance(revert, dict) and revert.get("target"):
            carried = revert.get("carried") if isinstance(revert.get("carried"), dict) else {}
            target_node = str(revert.get("target"))
            # G3：无条件落痕
            await store.record_transition(
                session,
                conversation_id=conversation_id,
                project_id=project_id,
                from_node=target,
                to_node=target_node,
                kind="revert",
                trigger="model",
                reason=str(revert.get("reason") or "模型建议回退"),
                required_carried=carried,
                budget_snapshot=budget,
            )
            yield "revert", {
                "from_node": target,
                "from_label": graph.NODE_LABELS.get(target, target),
                "to_node": target_node,
                "to_label": graph.NODE_LABELS.get(target_node, target_node),
                "reason": str(revert.get("reason") or ""),
                "carried": carried,
                "gate": "passed",
            }
            next_target = target_node
        else:
            next_target = graph.next_node(target) or "end"
            await store.record_transition(
                session,
                conversation_id=conversation_id,
                project_id=project_id,
                from_node=target,
                to_node=next_target,
                kind="advance",
                trigger="model",
                reason=f"「{graph.NODE_LABELS.get(target, target)}」由模型判定完成，进入下一节点",
                budget_snapshot=budget,
            )
            yield "migrated", {
                "from_node": target,
                "from_label": graph.NODE_LABELS.get(target, target),
                "to_node": next_target,
                "to_label": graph.NODE_LABELS.get(next_target, next_target),
                "kind": "advance",
            }

        yield "done", {
            "node": target,
            "status": "done",
            "display_status": graph.display_status("done"),
            "entry_index": entry_index,
            "next_node": next_target,
            "refs": refs,
            "cost_usd": round(total_cost, 6),
            "llm_call_count": llm_calls,
        }


# --------------------------------------------------------------------------- #
# 节点循环的三个辅助（2026-09-22：程序从"判定者"退成"报事实的人"）
# --------------------------------------------------------------------------- #
#: 契约连续失败多少次就如实停下等人。**这不是研究质量的上限**，
#: 而是"这台机器已经产不出可落库的东西了"的防挂死阈值 —— 没有它，
#: 一个无论如何都产不出合法 JSON 的模型会让循环永远烧下去。
CONTRACT_MISS_LIMIT = 6


def _advisory_notes(validation: Any) -> list[str]:
    """把校验器看到的东西整理成**给研究者看的事实清单**（不是判决书）。"""

    if validation is None:
        return []
    notes: list[str] = []
    for hit in getattr(validation, "hits", []) or []:
        message = str(getattr(hit, "message", "") or "").strip()
        if not message:
            continue
        notes.append(f"· {message}")
    return notes


def _contract_payload(target: str, candidate: dict[str, Any] | None) -> tuple[bool, dict[str, Any], str]:
    """产出能否落库（只看契约结构，不看研究质量）。

    返回 `(能不能用, 归一化后的产出, 不能用的原因)`。
    **占位节点没有契约** → 原样放行（它们本来就只做占位）。
    """

    if not isinstance(candidate, dict) or not candidate:
        return False, {}, "没有拿到可解析的 JSON 对象"
    model = NODE_OUTPUT_MODELS.get(target)
    if model is None:
        return True, candidate, ""
    try:
        obj = model.model_validate(candidate)
    except Exception as exc:  # noqa: BLE001 - 契约错误要如实回给模型，而不是吞掉
        return False, {}, f"产出结构不符合契约：{str(exc)[:240]}"
    return True, obj.model_dump(), ""


async def _hand_off_to_human(
    session_factory: Any,
    *,
    conversation_id: str,
    project_id: int | None,
    node: str,
    entry_index: int,
    retry_count: int,
    reason: str,
    validation: Any,
    raw_output: str,
    llm_calls: int,
    total_cost: float,
    budget_snapshot: dict[str, Any] | None,
    extra: dict[str, Any] | None = None,
) -> None:
    """转到人工：落节点行 + 留痕。**触发者写 model**（是模型判断要人介入，不是程序拦的）。

    `extra` 并进 `validation`：用来记「谁做的决定」（`decided_by`）、模型自述的 `state`、
    以及自检结论 —— 这几项是审计链上最该留住的证据，缺了就没法回答"这个节点为什么停在这"。
    """

    stored: dict[str, Any] = dict(validation.to_dict()) if validation is not None else {}
    stored.update(extra or {})
    async with session_factory() as session:
        await store.upsert_node_run(
            session,
            conversation_id=conversation_id,
            project_id=project_id,
            node=node,
            entry_index=entry_index,
            status="waiting_human",
            retry_count=retry_count,
            raw_output=raw_output[:20000],
            validation=stored or None,
            llm_call_count=llm_calls,
            cost_usd=round(total_cost, 6),
            finished_at=_utcnow(),
        )
        await store.record_transition(
            session,
            conversation_id=conversation_id,
            project_id=project_id,
            from_node=node,
            to_node=node,
            kind="stop",
            trigger="model",
            reason=reason,
            budget_snapshot=budget_snapshot,
        )


#: 同模型自检的输出契约（小、直白：state / summary / issues）。
SELF_CHECK_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "state": {"type": "string", "enum": ["done", "continue", "need_human"]},
        "summary": {"type": "string"},
        "issues": {"type": "array", "items": {"type": "string"}},
    },
    "required": ["state", "summary"],
    "additionalProperties": False,
}

#: 一次进入最多自检几轮。**这不是研究质量上限**（研究做几轮由模型说了算），
#: 而是防"自检→继续→自检→继续"的乒乓：自检本身不产出新内容，来回踢没有意义。
SELF_CHECK_LIMIT = 3


async def _run_self_check(
    *,
    node: str,
    model_ref: str,
    payload: dict[str, Any],
    research_question: str,
    advisories: list[str],
    library: dict[str, Any] | None,
    project_id: int | None,
) -> dict[str, Any]:
    """让**同一个模型**回头自查刚交的产出。拿不到结论就返回空字典（不阻断流程）。"""

    messages = prompts.build_self_check_messages(
        node=node,
        payload=payload,
        research_question=research_question,
        advisories=advisories,
        library=library,
    )
    try:
        result = await chat(
            messages,
            model_ref,
            None,
            NODE_MAX_TOKENS,
            SELF_CHECK_SCHEMA,
            project_id=project_id,
            stage=graph.stage_alias(node),
            purpose=f"{graph.purpose_for(node)}#自检"[:64],
            allow_fallback=False,
        )
    except Exception as exc:  # noqa: BLE001 - 自检失败不该拖垮节点
        logger.warning("自检调用失败 node=%s：%s", node, exc)
        return {}

    data = result.parsed if isinstance(result.parsed, dict) else _parse_lenient(result.content or "")
    if not isinstance(data, dict):
        return {}
    data["cost_usd"] = float(result.cost_usd or 0)
    return data


#: 一轮里最多替模型搜几组关键词（防一次要求 20 组把上游打爆）
MAX_SEARCH_QUERIES = 4
#: 每组关键词最多取几条（提示词塞不下更多，也没必要）
SEARCH_RESULTS_PER_QUERY = 5


async def _run_searches(queries: list[str]) -> list[dict[str, Any]]:
    """替模型执行它给出的搜索词。**搜不到也照样如实返回**（不吞、不编）。"""

    from services.agent import web_search

    blocks: list[dict[str, Any]] = []
    for query in queries:
        result = await web_search.search(query, limit=SEARCH_RESULTS_PER_QUERY)
        blocks.append(
            {
                "query": query,
                "ok": bool(result.get("ok")),
                "results": result.get("results") or [],
                "error": result.get("error") if not result.get("ok") else None,
            }
        )
    return blocks
