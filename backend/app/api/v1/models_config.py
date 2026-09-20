# Copyright 2026 SciLoop contributors
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
"""``/models`` 路由层：供应商 CRUD、连通性测试、环节路由读写。

红线：

- ``api_key`` **只入不出**：写入时加密（``fernet:v1:``）或存 ``env:VAR`` 引用；任何响应只返回脱敏值
- 全部写操作要求 ``X-Owner-Token``（``public_demo`` 面一律 403）
- 路由写入后立即失效路由缓存，保证「改完即生效」
- 连通性测试**强制实时**（``allow_replay=False``）：回放命中不能证明供应商真的可达
"""

from __future__ import annotations

import logging
from datetime import datetime
from typing import Any

from fastapi import APIRouter, Body, Depends, Query, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field, field_validator

from app.llm import adapter
from app.llm.errors import IsolationViolation, LLMError, SecretBackendUnavailable
from app.llm.pricing import parse_price_info
from app.llm.providers import models_url
from app.llm.registry import STAGES, get_registry
from app.llm.router import get_router
from app.llm.secret import (
    decrypt_api_key,
    encrypt_api_key,
    is_env_ref,
    key_fingerprint,
    mask_api_key,
)
from app.services.paper_source.cache import get_source_http

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/models", tags=["models"])

CONNECTIVITY_TEST_PURPOSE = "connectivity_test"

#: 「获取模型列表」所用的受限出网客户端来源名（进程内共享限流/缓存）
SYNC_MODELS_SOURCE = "model_registry"


# --------------------------------------------------------------------------- #
# 模型定义
# --------------------------------------------------------------------------- #
class PricingSide(BaseModel):
    """定价的一端（输入或输出）。Cherry Studio 口径：每百万 token + 币种。"""

    currency: str = "USD"
    perMillionTokens: float | None = Field(default=None, ge=0)


class Pricing(BaseModel):
    """模型定价（Cherry Studio 口径）。留空表示未知 -> cost_usd 记 null。"""

    input: PricingSide | None = None
    output: PricingSide | None = None


class ModelEntry(BaseModel):
    """供应商下的一个可路由模型（对应 ``model_configs.models[]`` 的一项）。"""

    model_id: str = Field(min_length=1, max_length=96)
    label: str | None = None
    context_window: int | None = Field(default=None, ge=0)
    #: 定价。缺任一端表示未知 -> cost_usd 记 null（禁止估算）
    pricing: Pricing | None = None
    temperature: float | None = Field(default=None, ge=0, le=2)
    max_tokens: int | None = Field(default=None, gt=0)

    @property
    def pricing_complete(self) -> bool:
        pricing = self.pricing
        if pricing is None or pricing.input is None or pricing.output is None:
            return False
        return pricing.input.perMillionTokens is not None and pricing.output.perMillionTokens is not None


class ModelConfigCreate(BaseModel):
    name: str = Field(min_length=1, max_length=96)
    base_url: str = Field(min_length=1)
    #: 明文 Key 或 ``env:变量名`` 引用
    api_key: str = Field(min_length=1)
    models: list[ModelEntry] = Field(default_factory=list)
    is_default: bool = False
    #: 端点类型（``openai`` / ``anthropic`` / …）。不做枚举校验，未知值原样接受
    type: str | None = Field(default=None, max_length=32)

    @field_validator("base_url")
    @classmethod
    def _check_base_url(cls, value: str) -> str:
        text = value.strip().rstrip("/")
        if not text.startswith(("http://", "https://")):
            raise ValueError("base_url 必须以 http:// 或 https:// 开头")
        return text


class ModelConfigUpdate(BaseModel):
    name: str | None = Field(default=None, min_length=1, max_length=96)
    base_url: str | None = None
    api_key: str | None = None
    models: list[ModelEntry] | None = None
    is_default: bool | None = None
    type: str | None = Field(default=None, max_length=32)


class ModelConfigOut(BaseModel):
    id: int
    name: str
    base_url: str
    models: list[dict[str, Any]] = Field(default_factory=list)
    is_default: bool = False
    api_key_masked: str = ""
    api_key_source: str = "empty"
    api_key_fingerprint: str | None = None
    pricing_complete: bool = True
    warning: str | None = None
    last_tested_at: datetime | None = None
    test_ok: bool | None = None
    created_at: datetime | None = None
    #: 端点类型（``openai`` / ``anthropic`` / …）；None = 按 OpenAI 兼容处理
    type: str | None = None


class ModelConfigList(BaseModel):
    items: list[ModelConfigOut]
    total: int
    page: int
    page_size: int


class ConnectivityTestRequest(BaseModel):
    model_id: str | None = None


class ConnectivityTestResult(BaseModel):
    ok: bool
    model_ref: str | None = None
    model_id: str | None = None
    latency_ms: int | None = None
    reply_preview: str | None = None
    error_kind: str | None = None
    message: str | None = None
    #: 本次测试同样写入 llm_call_logs 的说明
    logged_to: str = "llm_call_logs"


class SyncModelsResult(BaseModel):
    """``POST /models/configs/{id}/sync-models`` 的结果（如实回传上游计数）。"""

    fetched: int
    added: list[str] = Field(default_factory=list)
    existing: list[str] = Field(default_factory=list)
    #: 实际请求的 URL（便于排障；不含任何凭据）
    endpoint: str
    test_ok: bool = True


class RoutingEntryIn(BaseModel):
    stage: str
    model_config_id: int
    model_id: str
    temperature: float | None = Field(default=None, ge=0, le=2)
    max_tokens: int | None = Field(default=None, gt=0)
    purpose: str | None = None

    @field_validator("stage")
    @classmethod
    def _check_stage(cls, value: str) -> str:
        if value not in STAGES:
            raise ValueError(f"stage 必须是 {', '.join(STAGES)} 之一")
        return value


class RoutingPutRequest(BaseModel):
    project_id: int | None = None
    entries: list[RoutingEntryIn] = Field(default_factory=list)


class RoutingTableOut(BaseModel):
    project_id: int | None = None
    stages: list[dict[str, Any]]
    entries: list[dict[str, Any]]


# --------------------------------------------------------------------------- #
# 访问控制
# --------------------------------------------------------------------------- #
def _owner_dependency() -> Any:
    """优先复用 WP01 的 ``app.core.security`` 依赖；未就绪时用等价本地实现。"""
    try:
        from app.core import security  # type: ignore

        for name in ("require_owner", "require_owner_token", "owner_required"):
            dependency = getattr(security, name, None)
            if callable(dependency):
                return dependency
    except Exception:  # noqa: BLE001 - WP01 尚未提供安全模块
        pass
    return None


async def _local_owner_guard(request: Request) -> None:
    """本地兜底实现：``X-Owner-Token`` 必须与环境变量一致。"""
    expected = ""
    try:
        from app.core.config import get_settings

        expected = get_settings().owner_token or ""
    except Exception:  # pragma: no cover
        import os

        expected = os.environ.get("OWNER_TOKEN", "")
    provided = request.headers.get("X-Owner-Token", "")
    if not expected or provided != expected:
        raise _auth_error()


def _auth_error() -> Any:
    from fastapi import HTTPException

    return HTTPException(
        status_code=403,
        detail={
            "code": "owner_token_required",
            "message": "该操作仅限 Owner 面（需要有效的 X-Owner-Token）",
            "detail": None,
        },
    )


_owner_dep = _owner_dependency()
require_owner = _owner_dep if _owner_dep is not None else _local_owner_guard


# --------------------------------------------------------------------------- #
# 工具
# --------------------------------------------------------------------------- #
def _fail(status_code: int, code: str, message: str, detail: Any = None) -> JSONResponse:
    return JSONResponse(status_code=status_code, content={"code": code, "message": message, "detail": detail})


def _api_key_source(stored: str) -> str:
    if not stored:
        return "empty"
    if is_env_ref(stored):
        return "env_ref"
    return "encrypted"


def _normalize_type(value: str | None) -> str | None:
    """归一 ``type``：去空白；空串视为未设置（None）。未知取值原样保留，不做枚举校验。"""
    if value is None:
        return None
    text = str(value).strip()
    return text or None


def _extract_model_ids(rows: list[Any]) -> list[str]:
    """从上游 ``data`` 数组提取模型 id（保序去重）。

    上游条目不是 ``{"id": ...}`` 形状时抛 :class:`ValueError`，由调用方转
    ``invalid_response``——**不猜测**、不伪造列表。
    """
    ids: list[str] = []
    seen: set[str] = set()
    for row in rows:
        if not isinstance(row, dict):
            raise ValueError(f"data[] 元素不是对象：{type(row).__name__}")
        raw = row.get("id")
        model_id = str(raw).strip() if raw is not None else ""
        if not model_id:
            raise ValueError("data[] 元素缺少非空 id")
        if model_id in seen:
            continue
        seen.add(model_id)
        ids.append(model_id)
    return ids


def _entry_pricing_complete(entry: dict[str, Any]) -> bool:
    """按 Cherry 口径判定一个 ``models[]`` 项的定价是否完整（兼容旧键）。"""
    price = parse_price_info(entry)
    return price.input_per_million is not None and price.output_per_million is not None


def _to_out(record: Any) -> ModelConfigOut:
    models = [dict(entry) for entry in (record.models or [])]
    pricing_complete = all(_entry_pricing_complete(entry) for entry in models)
    warning = None
    if not models:
        warning = "尚未登记任何模型，无法用于路由"
    elif not pricing_complete:
        warning = (
            "存在未填写单价的模型：这些模型的 cost_usd 会记为 null（禁止估算），"
            "成本护栏将无法精确判定，建议补齐单价后再启用自动模式"
        )
    return ModelConfigOut(
        id=record.id,
        name=record.name,
        base_url=record.base_url,
        models=models,
        is_default=bool(record.is_default),
        api_key_masked=mask_api_key(record.api_key_enc),
        api_key_source=_api_key_source(record.api_key_enc),
        # 指纹取自「存储值」（密文/引用），不接触明文
        api_key_fingerprint=key_fingerprint(record.api_key_enc),
        pricing_complete=pricing_complete,
        warning=warning,
        last_tested_at=record.last_tested_at,
        test_ok=record.test_ok,
        created_at=record.created_at,
        type=getattr(record, "type", None),
    )


def _model_ref(record: Any, model_id: str) -> str:
    from app.llm.types import slugify_provider

    return f"{slugify_provider(record.name)}:{model_id}"


async def _clear_other_defaults(registry: Any, *, keep_id: int | None) -> list[int]:
    """默认供应商**互斥**：把除 ``keep_id`` 外的 ``is_default`` 全部取消。

    为什么必须在服务端做：前端开关只写自己那一条，历史上已经出现过两个供应商
    同时 ``is_default=true``（默认供应商不唯一），导致「默认供应商的第一个模型」
    这种用法没有确定含义。放在这里，任何写入入口（新增 / 修改）都一致。
    """
    cleared: list[int] = []
    for record in await registry.list_configs():
        if record.id == keep_id or not record.is_default:
            continue
        await registry.update_config(record.id, {"is_default": False})
        cleared.append(record.id)
    return cleared


# --------------------------------------------------------------------------- #
# 供应商 CRUD
# --------------------------------------------------------------------------- #
@router.get("/configs", response_model=ModelConfigList, summary="供应商列表（api_key 脱敏）")
async def list_model_configs(
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=100),
) -> ModelConfigList:
    records = await get_registry().list_configs()
    start = (page - 1) * page_size
    return ModelConfigList(
        items=[_to_out(record) for record in records[start : start + page_size]],
        total=len(records),
        page=page,
        page_size=page_size,
    )


@router.post(
    "/configs",
    response_model=ModelConfigOut,
    status_code=201,
    summary="新增供应商",
    dependencies=[Depends(require_owner)],
)
async def create_model_config(payload: ModelConfigCreate) -> Any:
    try:
        api_key_enc = encrypt_api_key(payload.api_key)
    except SecretBackendUnavailable as exc:
        return _fail(503, "secret_backend_unavailable", exc.message, exc.detail)
    except ValueError as exc:
        return _fail(400, "invalid_api_key", str(exc))

    registry = get_registry()
    if payload.is_default:
        await _clear_other_defaults(registry, keep_id=None)
    config_id = await registry.create_config(
        name=payload.name.strip(),
        base_url=payload.base_url,
        api_key_enc=api_key_enc,
        models=[entry.model_dump() for entry in payload.models],
        is_default=payload.is_default,
        type=_normalize_type(payload.type),
    )
    record = await registry.get_config(config_id)
    if record is None:  # pragma: no cover - 刚写入不可能查不到
        return _fail(500, "persist_failed", "供应商写入后无法读回")
    return _to_out(record)


@router.patch(
    "/configs/{config_id}",
    response_model=ModelConfigOut,
    summary="修改供应商",
    dependencies=[Depends(require_owner)],
)
async def update_model_config(config_id: int, payload: ModelConfigUpdate) -> Any:
    registry = get_registry()
    record = await registry.get_config(config_id)
    if record is None:
        return _fail(404, "not_found", f"供应商 {config_id} 不存在")

    fields: dict[str, Any] = {}
    if payload.name is not None:
        fields["name"] = payload.name.strip()
    if payload.base_url is not None:
        base_url = payload.base_url.strip().rstrip("/")
        if not base_url.startswith(("http://", "https://")):
            return _fail(400, "invalid_base_url", "base_url 必须以 http:// 或 https:// 开头")
        fields["base_url"] = base_url
    if payload.models is not None:
        fields["models"] = [entry.model_dump() for entry in payload.models]
    if payload.is_default is not None:
        fields["is_default"] = payload.is_default
        if payload.is_default:
            await _clear_other_defaults(registry, keep_id=config_id)
    if payload.type is not None:
        fields["type"] = _normalize_type(payload.type)
    if payload.api_key is not None:
        try:
            fields["api_key_enc"] = encrypt_api_key(payload.api_key)
        except SecretBackendUnavailable as exc:
            return _fail(503, "secret_backend_unavailable", exc.message, exc.detail)

    await registry.update_config(config_id, fields)
    get_router().invalidate()
    updated = await registry.get_config(config_id)
    if updated is None:  # pragma: no cover
        return _fail(404, "not_found", f"供应商 {config_id} 不存在")
    return _to_out(updated)


@router.delete(
    "/configs/{config_id}",
    summary="删除供应商",
    dependencies=[Depends(require_owner)],
)
async def delete_model_config(config_id: int) -> Any:
    registry = get_registry()
    record = await registry.get_config(config_id)
    if record is None:
        return _fail(404, "not_found", f"供应商 {config_id} 不存在")

    referenced = [
        row for row in await registry.list_all_routing() if row.model_config_id == config_id
    ]
    if referenced:
        stages = ", ".join(sorted({row.stage for row in referenced}))
        return _fail(
            409,
            "config_in_use",
            f"供应商 {config_id} 仍被环节路由引用（{stages}），请先改路由再删除",
            {"stages": sorted({row.stage for row in referenced})},
        )

    await registry.delete_config(config_id)
    get_router().invalidate()
    return {"deleted": True, "id": config_id}


# --------------------------------------------------------------------------- #
# 连通性测试
# --------------------------------------------------------------------------- #
@router.post(
    "/configs/{config_id}/test",
    response_model=ConnectivityTestResult,
    summary="供应商连通性测试（强制实时调用，结果写入 llm_call_logs）",
    dependencies=[Depends(require_owner)],
)
async def test_model_config(
    config_id: int,
    payload: ConnectivityTestRequest = Body(default=ConnectivityTestRequest()),
) -> Any:
    registry = get_registry()
    record = await registry.get_config(config_id)
    if record is None:
        return _fail(404, "not_found", f"供应商 {config_id} 不存在")

    model_id = payload.model_id or next(
        (str(entry.get("model_id")) for entry in (record.models or []) if entry.get("model_id")), ""
    )
    if not model_id:
        return _fail(400, "no_model", "该供应商未登记任何模型，无法测试连通性")

    model_ref = _model_ref(record, model_id)
    try:
        result = await adapter.chat(
            [
                {"role": "system", "content": "You are a connectivity probe. Reply with the single word: ok"},
                {"role": "user", "content": "ping"},
            ],
            model_ref,
            temperature=0.0,
            max_tokens=8,
            stage=None,
            purpose=CONNECTIVITY_TEST_PURPOSE,
            allow_fallback=False,
            allow_replay=False,  # 回放命中不能证明供应商可达
            strict_logging=True,
        )
    except LLMError as exc:
        await registry.mark_tested(config_id, False)
        logger.warning("供应商 %s 连通性测试失败：%s", model_ref, exc.message)
        return ConnectivityTestResult(
            ok=False,
            model_ref=model_ref,
            model_id=model_id,
            error_kind=exc.kind,
            message=exc.message,
        )

    await registry.mark_tested(config_id, True)
    return ConnectivityTestResult(
        ok=True,
        model_ref=result.model_ref,
        model_id=result.model_id,
        latency_ms=result.duration_ms,
        reply_preview=(result.content or "")[:60],
    )


# --------------------------------------------------------------------------- #
# 获取模型列表（后端出网，Owner 专属）
# --------------------------------------------------------------------------- #
@router.post(
    "/configs/{config_id}/sync-models",
    response_model=SyncModelsResult,
    summary="从供应商拉取模型列表并合并（Owner 专属；新增项 pricing 留空，不猜价格）",
    dependencies=[Depends(require_owner)],
)
async def sync_models(config_id: int) -> Any:
    """后端出网调该供应商的 OpenAI 兼容 ``/v1/models``，把 ``data[].id`` 并入本地 ``models``。

    失败一律如实回传，**不伪造列表**：

    - 未配置 Key → ``409 api_key_missing``
    - 连不上/超时 → ``502 upstream_unreachable``
    - 上游非 2xx → ``502 upstream_http_error``
    - 响应不是 ``{"data": [...]}`` → ``502 invalid_response``

    新增模型的 ``pricing`` 留空（前端需引导用户补齐）；Key 只用于 ``Authorization`` 头，
    任何响应都不回显。
    """
    registry = get_registry()
    record = await registry.get_config(config_id)
    if record is None:
        return _fail(404, "not_found", f"供应商 {config_id} 不存在")

    api_key = decrypt_api_key(record.api_key_enc)
    if not api_key:
        return _fail(
            409,
            "api_key_missing",
            "该供应商尚未配置可用的 API Key（请在设置页填写，或改用 env:变量名 引用）",
            {"config_id": config_id, "api_key_source": _api_key_source(record.api_key_enc)},
        )

    try:
        endpoint = models_url(record.base_url)
    except ValueError as exc:
        return _fail(400, "invalid_base_url", str(exc), {"base_url": record.base_url})

    client = get_source_http(SYNC_MODELS_SOURCE, qps=0, max_retries=0, use_cache=False)
    result = await client.get_json(
        endpoint,
        headers={"Authorization": f"Bearer {api_key}", "Accept": "application/json"},
        use_cache=False,
        max_retries=0,
        expect_json=True,
    )
    if not result.ok:
        if result.status_code is None:
            return _fail(
                502,
                "upstream_unreachable",
                "无法连接供应商模型列表端点（网络不可达或超时）",
                {"endpoint": endpoint, "error": result.error or result.error_kind or "unknown"},
            )
        return _fail(
            502,
            "upstream_http_error",
            f"供应商模型列表端点返回 HTTP {result.status_code}",
            {"status": result.status_code, "endpoint": endpoint},
        )

    payload = result.payload
    rows = payload.get("data") if isinstance(payload, dict) else None
    if not isinstance(rows, list):
        return _fail(
            502,
            "invalid_response",
            '供应商响应不是预期的 {"data": [...]} 形态',
            {"endpoint": endpoint},
        )
    try:
        fetched_ids = _extract_model_ids(rows)
    except ValueError as exc:
        return _fail(
            502,
            "invalid_response",
            f"供应商响应中的 data[] 不符合预期：{exc}",
            {"endpoint": endpoint},
        )

    local_models = [dict(entry) for entry in (record.models or [])]
    known = {str(entry.get("model_id")) for entry in local_models if entry.get("model_id")}
    added = [model_id for model_id in fetched_ids if model_id not in known]
    existing = [model_id for model_id in fetched_ids if model_id in known]

    if added:
        # pricing 留空（禁止猜价）；label 用模型 id，用户可在设置页补齐
        merged = local_models + [{"model_id": model_id, "label": model_id, "pricing": {}} for model_id in added]
        await registry.update_config(config_id, {"models": merged})
        get_router().invalidate()

    await registry.mark_tested(config_id, True)
    logger.info(
        "sync-models config=%s endpoint=%s fetched=%d added=%d",
        config_id,
        endpoint,
        len(fetched_ids),
        len(added),
    )
    return SyncModelsResult(
        fetched=len(fetched_ids),
        added=added,
        existing=existing,
        endpoint=endpoint,
        test_ok=True,
    )


# --------------------------------------------------------------------------- #
# 环节路由
# --------------------------------------------------------------------------- #
@router.get("/routing", response_model=RoutingTableOut, summary="环节路由表（生效值 + 已配置行）")
async def get_routing(project_id: int | None = Query(default=None)) -> RoutingTableOut:
    registry = get_registry()
    stages = await get_router().effective_routing(project_id)
    entries = [
        {
            "id": row.id,
            "project_id": row.project_id,
            "stage": row.stage,
            "purpose": row.purpose,
            "model_config_id": row.model_config_id,
            "model_id": row.model_id,
            "temperature": row.temperature,
            "max_tokens": row.max_tokens,
        }
        for row in await registry.list_routing(project_id)
    ]
    return RoutingTableOut(project_id=project_id, stages=stages, entries=entries)


@router.put(
    "/routing",
    response_model=RoutingTableOut,
    summary="更新环节路由（全量替换指定作用域）",
    dependencies=[Depends(require_owner)],
)
async def put_routing(payload: RoutingPutRequest) -> Any:
    registry = get_registry()
    configs = {record.id: record for record in await registry.list_configs()}

    missing = [entry.model_config_id for entry in payload.entries if entry.model_config_id not in configs]
    if missing:
        return _fail(400, "unknown_model_config", f"供应商配置不存在：{sorted(set(missing))}")

    mismatched: list[str] = []
    for entry in payload.entries:
        record = configs[entry.model_config_id]
        if not any(str(item.get("model_id")) == entry.model_id for item in (record.models or [])):
            mismatched.append(f"{entry.stage}->{record.name}:{entry.model_id}")
    if mismatched:
        return _fail(
            400,
            "model_not_in_config",
            f"以下路由指定的模型不在该供应商的 models 列表中：{mismatched}",
            {"items": mismatched},
        )

    await registry.replace_routing(
        [entry.model_dump() for entry in payload.entries], project_id=payload.project_id
    )
    get_router().invalidate()

    stages = await get_router().effective_routing(payload.project_id)
    entries = [
        {
            "id": row.id,
            "project_id": row.project_id,
            "stage": row.stage,
            "purpose": row.purpose,
            "model_config_id": row.model_config_id,
            "model_id": row.model_id,
            "temperature": row.temperature,
            "max_tokens": row.max_tokens,
        }
        for row in await registry.list_routing(payload.project_id)
    ]
    return RoutingTableOut(project_id=payload.project_id, stages=stages, entries=entries)


@router.get(
    "/routing/isolation",
    summary="盲评隔离自检（plan/plan_review 是否为两个独立路由）",
)
async def check_isolation(project_id: int | None = Query(default=None)) -> Any:
    try:
        generator, reviewer = await get_router().resolve_pair("plan", "plan_review", project_id)
    except IsolationViolation as exc:
        return {
            "isolated": False,
            "generator_stage": "plan",
            "reviewer_stage": "plan_review",
            "code": exc.kind,
            "message": exc.message,
            "generator": None,
            "reviewer": None,
        }
    return {
        "isolated": True,
        "generator_stage": "plan",
        "reviewer_stage": "plan_review",
        "code": None,
        "message": None,
        "generator": generator.describe(),
        "reviewer": reviewer.describe(),
    }


__all__ = ["router"]
