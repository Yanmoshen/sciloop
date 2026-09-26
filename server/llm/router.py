# Copyright 2026 SciLoop contributors
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
"""按环节的模型路由与盲评隔离保障。

解析优先级（WP02-T3）：

1. ``stage_model_routing`` 中 ``project_id = 本项目`` 的行（项目级覆盖）
2. ``stage_model_routing`` 中 ``project_id IS NULL`` 的行（全局默认）
3. 环境变量兜底 ``LLM_DEFAULT_BASE_URL / LLM_DEFAULT_API_KEY / LLM_DEFAULT_MODEL``
4. 降级备用：``LLM_FALLBACK_*``（仅当 ``model_ref`` 与主模型不同）

盲评隔离（契约红线）：

- :meth:`ModelRouter.resolve_pair` 校验「生成模型 ≠ 评审模型」，不成立抛
  :class:`IsolationViolation`，**绝不静默回退同一模型**（WP12 直接依赖）
- 两侧的降级链都会剔除对方的模型，避免「降着降着就撞车」
"""

from __future__ import annotations

import logging
import time
from typing import Any

from llm.errors import IsolationViolation, ModelRoutingError
from llm.pricing import parse_price_info
from llm.providers import detect_capability
from llm.registry import (
    STAGES,
    ModelConfigRecord,
    ModelRegistry,
    RoutingRecord,
    get_registry,
)
from llm.secret import decrypt_api_key
from llm.types import ENV_PROVIDER, ResolvedModel, slugify_provider

logger = logging.getLogger(__name__)

#: 路由表进程内缓存时长（秒）。写入路由时由 API 调 :meth:`ModelRouter.invalidate` 立即失效。
ROUTING_CACHE_TTL_SECONDS = 5.0


class ModelRouter:
    """环节 → 模型解析器。"""

    def __init__(
        self,
        registry: ModelRegistry | None = None,
        *,
        cache_ttl: float = ROUTING_CACHE_TTL_SECONDS,
    ) -> None:
        self._registry = registry
        self._cache_ttl = cache_ttl
        self._cache: dict[tuple[str, int | None], tuple[float, ResolvedModel]] = {}

    # ------------------------------------------------------------------ #
    @property
    def registry(self) -> ModelRegistry:
        if self._registry is None:
            self._registry = get_registry()
        return self._registry

    def invalidate(self) -> None:
        """清空路由缓存（设置页修改路由后必须调用，保证下一个流程立即生效）。"""
        self._cache.clear()

    # ------------------------------------------------------------------ #
    async def resolve(self, stage: str, project_id: int | None = None) -> ResolvedModel:
        """解析某环节的主模型。"""
        chain = await self.resolve_chain(stage, project_id)
        if not chain:
            raise ModelRoutingError(
                f"环节 {stage} 无可用模型：既未配置 stage_model_routing，"
                "环境变量 LLM_DEFAULT_MODEL / LLM_DEFAULT_API_KEY 也不完整",
                detail={"stage": stage, "project_id": project_id},
            )
        return chain[0]

    async def resolve_chain(
        self,
        stage: str,
        project_id: int | None = None,
        *,
        exclude_refs: set[str] | None = None,
    ) -> list[ResolvedModel]:
        """解析环节的主模型 + 备用链（已按 ``exclude_refs`` 过滤）。"""
        excluded = {_norm_ref(ref) for ref in (exclude_refs or set())}
        chain: list[ResolvedModel] = []
        seen: set[str] = set()

        primary = await self._resolve_primary(stage, project_id)
        if primary is not None:
            chain.append(primary)
            seen.add(_norm_ref(primary.model_ref))

        for fallback in self._env_fallback_models():
            key = _norm_ref(fallback.model_ref)
            if key in seen:
                continue
            chain.append(fallback)
            seen.add(key)

        # 只保留第一个可用的、且未被排除的模型 + 其余未被排除的备用
        filtered = [item for item in chain if _norm_ref(item.model_ref) not in excluded]
        return filtered

    async def resolve_explicit(
        self,
        model_ref: str,
        *,
        stage: str | None = None,
        project_id: int | None = None,
    ) -> ResolvedModel:
        """解析一个显式 ``provider:model_id``（用于连通性测试、T3 多模型对照、回放录制）。"""
        provider, model_id = str(model_ref).split(":", 1) if ":" in str(model_ref) else ("", str(model_ref))
        provider_key = slugify_provider(provider)
        if provider_key == ENV_PROVIDER or not provider_key:
            for candidate in self._env_fallback_models(include_default=True):
                if candidate.model_id == model_id:
                    return candidate
            return self._env_model(model_id, source="explicit")
        for config in await self.registry.list_configs():
            if slugify_provider(config.name) != provider_key:
                continue
            return self._build_from_config(
                config=config,
                model_id=model_id,
                temperature=None,
                max_tokens=None,
                source="explicit",
            )
        raise ModelRoutingError(
            f"找不到供应商 {provider}（model_ref={model_ref}）",
            detail={"stage": stage, "project_id": project_id, "model_ref": model_ref},
        )

    # ------------------------------------------------------------------ #
    async def resolve_pair(
        self,
        generator_stage: str,
        reviewer_stage: str,
        project_id: int | None = None,
    ) -> tuple[ResolvedModel, ResolvedModel]:
        """解析「生成 / 评审」一对路由，并强制校验盲评隔离。

        判定口径（**不做静默替换**）：

        1. 先各自解析**主路由**（项目级 > 全局 > 环境兜底），两边都解析不出来 → 抛异常
        2. 主路由 ``model_ref`` 相同 → 抛 :class:`IsolationViolation`：
           此时即使存在可用的备用模型也**不会自动顶替**（那属于掩盖配置错误，
           会让「plan 用哪个模型生成」与实际不符）
        3. 隔离成立时，两侧的降级链互相剔除对方的主模型，避免「降着降着撞车」

        返回 ``(generator, reviewer)``；无法满足隔离时抛 :class:`IsolationViolation`，
        调用方（WP12 的 ``plan_review`` 环节）应让该环节直接失败。
        """
        gen_primary = await self._resolve_primary(generator_stage, project_id)
        rev_primary = await self._resolve_primary(reviewer_stage, project_id)

        if gen_primary is None or rev_primary is None:
            raise IsolationViolation(
                f"盲评隔离无法校验：{generator_stage} 或 {reviewer_stage} 没有可用模型路由"
                f"（plan={gen_primary.model_ref if gen_primary else '未配置'}, "
                f"plan_review={rev_primary.model_ref if rev_primary else '未配置'}）",
                generator_stage=generator_stage,
                reviewer_stage=reviewer_stage,
                generator_ref=gen_primary.model_ref if gen_primary else None,
                reviewer_ref=rev_primary.model_ref if rev_primary else None,
                detail={"project_id": project_id},
            )

        if _norm_ref(gen_primary.model_ref) == _norm_ref(rev_primary.model_ref):
            raise IsolationViolation(
                f"盲评隔离校验失败：{generator_stage} 与 {reviewer_stage} 解析到同一模型 "
                f"{gen_primary.model_ref}（来源 {gen_primary.source}/{rev_primary.source}）。"
                "请在设置页为 plan_review 指定不同供应商或不同模型；"
                "系统不会自动回退到其它模型来「凑」出隔离。",
                generator_stage=generator_stage,
                reviewer_stage=reviewer_stage,
                generator_ref=gen_primary.model_ref,
                reviewer_ref=rev_primary.model_ref,
                detail={"project_id": project_id},
            )

        if gen_primary.model_id == rev_primary.model_id:
            # 不同供应商提供同名模型：隔离「形式成立、实质存疑」，记为 warning 供人工复核
            logger.warning(
                "盲评隔离告警：%s 与 %s 的 model_id 相同（%s），仅供应商不同，交叉评审价值有限",
                generator_stage,
                reviewer_stage,
                gen_primary.model_id,
            )

        gen_chain = await self.resolve_chain(
            generator_stage, project_id, exclude_refs={rev_primary.model_ref}
        )
        rev_chain = await self.resolve_chain(
            reviewer_stage, project_id, exclude_refs={gen_primary.model_ref}
        )
        if not gen_chain or not rev_chain:
            raise IsolationViolation(
                f"盲评隔离降级链冲突：剔除对方模型后，"
                f"{generator_stage}={'空' if not gen_chain else gen_chain[0].model_ref} / "
                f"{reviewer_stage}={'空' if not rev_chain else rev_chain[0].model_ref}，"
                "请补充独立的备用模型配置",
                generator_stage=generator_stage,
                reviewer_stage=reviewer_stage,
                generator_ref=gen_primary.model_ref,
                reviewer_ref=rev_primary.model_ref,
                detail={"project_id": project_id},
            )
        return gen_chain[0], rev_chain[0]

    async def effective_routing(self, project_id: int | None = None) -> list[dict[str, Any]]:
        """每个环节的生效路由（含来源与脱敏信息），供 ``GET /models/routing`` 展示。"""
        rows: list[dict[str, Any]] = []
        for stage in STAGES:
            try:
                model = await self.resolve(stage, project_id)
                rows.append(
                    {
                        "stage": stage,
                        "configured": model.source in {"project", "global"},
                        **model.describe(),
                    }
                )
            except ModelRoutingError as exc:
                rows.append(
                    {
                        "stage": stage,
                        "configured": False,
                        "model_ref": None,
                        "source": "unresolved",
                        "error": exc.message,
                    }
                )
        return rows

    # ------------------------------------------------------------------ #
    # 内部
    # ------------------------------------------------------------------ #
    async def _resolve_primary(self, stage: str, project_id: int | None) -> ResolvedModel | None:
        cache_key = (stage, project_id)
        now = time.monotonic()
        cached = self._cache.get(cache_key)
        if cached and cached[0] > now:
            return cached[1]

        resolved = await self._resolve_from_registry(stage, project_id)
        if resolved is None:
            # 环节没配路由 → **回落设置页里的默认模型**（用户口径 2026-09-26：
            # 翻译这类环节不该因为"没人给它单独配路由"就静默降级成本地桩）。
            resolved = await self._resolve_default_config()
        if resolved is None:
            resolved = self._env_model_from_settings(source="env_default")
        if resolved is None:
            return None
        if self._cache_ttl > 0:
            self._cache[cache_key] = (now + self._cache_ttl, resolved)
        return resolved

    async def _resolve_from_registry(self, stage: str, project_id: int | None) -> ResolvedModel | None:
        routing: RoutingRecord | None = await self.registry.get_routing(stage, project_id)
        if routing is None:
            return None
        config: ModelConfigRecord | None = await self.registry.get_config(routing.model_config_id)
        if config is None:
            raise ModelRoutingError(
                f"环节 {stage} 的路由指向已删除的供应商配置 id={routing.model_config_id}，"
                "请在设置页重新选择模型",
                detail={"stage": stage, "project_id": project_id, "model_config_id": routing.model_config_id},
            )
        return self._build_from_config(
            config=config,
            model_id=routing.model_id,
            temperature=routing.temperature,
            # ⚠️ **不再把路由行的 max_tokens 注入下去**（2026-09-24 用户口径：
            # 所有渠道所有模型统一，不设输出上限）。历史上它按环节/供应商各配一份，
            # 结果同一个任务换个环节就可能被截断 —— 而截断的 JSON 一定不合法。
            # 库里的 ``stage_model_routing.max_tokens`` 列保留作历史记录，但不再生效。
            max_tokens=None,
            source="project" if routing.project_id is not None else "global",
        )

    async def _resolve_default_config(self) -> ResolvedModel | None:
        """回落到「设置页里那个默认模型」（`model_configs.is_default`）。

        ⚠️ 为什么必须有这一层（2026-09-26 实测）：`stage_model_routing` 里**只有 `parse` 一行**，
        所以 `translate` 这类环节一路回落到 `.env` 的 `LLM_DEFAULT_API_KEY`（**是空的**），
        翻译于是**静默**换成本地桩 —— 产出"原文 + 【本地桩·未翻译】"，
        表面上任务"完成 100%"，实际一个字的翻译都没有。
        **凭据只有一个来源**：用户在设置页配好的那套。某个环节没人单独配路由，
        不应该等于"这个功能没接模型"。
        """

        try:
            configs = await self.registry.list_configs()
        except Exception as exc:  # noqa: BLE001 - 读不到就继续走 env 兜底
            logger.warning("读取模型配置失败，继续走环境变量兜底：%s", exc)
            return None
        if not configs:
            return None

        config = next((item for item in configs if item.is_default), configs[0])
        models = list(config.models or [])
        if not models:
            return None
        model_id = str(models[0].get("model_id") or "")
        if not model_id:
            return None
        return self._build_from_config(
            config=config,
            model_id=model_id,
            temperature=None,
            max_tokens=None,
            source="default_config",
        )

    def _build_from_config(
        self,
        *,
        config: ModelConfigRecord,
        model_id: str,
        temperature: float | None,
        max_tokens: int | None,
        source: str,
    ) -> ResolvedModel:
        provider = slugify_provider(config.name)
        entry = config.find_model(model_id) or {}
        price = parse_price_info(entry)
        capability = detect_capability(provider=provider, base_url=config.base_url)
        api_key = decrypt_api_key(config.api_key_enc)
        if not api_key:
            logger.warning(
                "供应商 %s 的 API Key 不可用（未配置或解密失败），调用将返回 401；请在设置页重新填写",
                config.name,
            )
        return ResolvedModel(
            model_ref=f"{provider}:{model_id}",
            base_url=config.base_url,
            provider=provider,
            model_id=model_id,
            api_key=api_key,
            temperature=temperature if temperature is not None else _float_or_none(entry.get("temperature")),
            # ⚠️ 同上：供应商 models[] 里那项 ``max_tokens`` 也不再兜底注入（统一不设上限）。
            max_tokens=max_tokens,
            source=source,
            model_config_id=config.id,
            provider_name=config.name,
            input_price_per_million=price.input_per_million,
            output_price_per_million=price.output_per_million,
            price_currency=price.normalized_currency,
            provider_capability=capability.key,
            extra={"models": config.models},
        )

    def _env_fallback_models(self, *, include_default: bool = True) -> list[ResolvedModel]:
        models: list[ResolvedModel] = []
        if include_default:
            default = self._env_model_from_settings(source="env_default")
            if default is not None:
                models.append(default)
        fallback = self._env_model_from_settings(source="env_fallback", use_fallback=True)
        if fallback is not None:
            models.append(fallback)
        return models

    def _env_model_from_settings(
        self, *, source: str = "env_default", use_fallback: bool = False
    ) -> ResolvedModel | None:
        settings = _settings()
        if settings is None:
            return None
        if use_fallback:
            base_url = getattr(settings, "llm_fallback_base_url", "") or ""
            api_key = getattr(settings, "llm_fallback_api_key", "") or ""
            model_id = getattr(settings, "llm_fallback_model", "") or ""
            if not (model_id and base_url):
                return None
        else:
            base_url = getattr(settings, "llm_default_base_url", "") or ""
            api_key = getattr(settings, "llm_default_api_key", "") or ""
            model_id = getattr(settings, "llm_default_model", "") or ""
            if not model_id:
                return None
        capability = detect_capability(base_url=base_url)
        return ResolvedModel(
            model_ref=f"{ENV_PROVIDER}:{model_id}",
            base_url=base_url,
            provider=ENV_PROVIDER,
            model_id=model_id,
            api_key=api_key,
            source=source,
            provider_name="环境变量兜底",
            provider_capability=capability.key,
            extra={"prices_unknown": True},
        )

    def _env_model(self, model_id: str, *, source: str) -> ResolvedModel:
        settings = _settings()
        base_url = getattr(settings, "llm_default_base_url", "") if settings else ""
        api_key = getattr(settings, "llm_default_api_key", "") if settings else ""
        return ResolvedModel(
            model_ref=f"{ENV_PROVIDER}:{model_id}",
            base_url=base_url or "",
            provider=ENV_PROVIDER,
            model_id=model_id,
            api_key=api_key or "",
            source=source,
            provider_name="环境变量兜底",
            provider_capability=detect_capability(base_url=base_url).key,
            extra={"prices_unknown": True},
        )


# --------------------------------------------------------------------------- #
# 工具
# --------------------------------------------------------------------------- #
def _norm_ref(ref: str | None) -> str:
    return str(ref or "").strip().lower()


def _settings() -> Any:
    try:
        from core.config import get_settings

        return get_settings()
    except Exception:  # pragma: no cover - 配置未就绪
        return None


def _float_or_none(value: Any) -> float | None:
    try:
        return float(value) if value is not None else None
    except (TypeError, ValueError):
        return None


def _int_or_none(value: Any) -> int | None:
    try:
        return int(value) if value is not None else None
    except (TypeError, ValueError):
        return None


# --------------------------------------------------------------------------- #
# 默认单例与模块级入口（WP12 直接 ``from llm.router import resolve_pair``）
# --------------------------------------------------------------------------- #
_default_router: ModelRouter | None = None


def get_router() -> ModelRouter:
    global _default_router
    if _default_router is None:
        _default_router = ModelRouter()
    return _default_router


def set_router(router: ModelRouter | None) -> None:
    global _default_router
    _default_router = router


def invalidate_routing_cache() -> None:
    """供 API 层在路由写入后调用。"""
    get_router().invalidate()


async def resolve(stage: str, project_id: int | None = None) -> ResolvedModel:
    return await get_router().resolve(stage, project_id)


async def resolve_chain(
    stage: str, project_id: int | None = None, *, exclude_refs: set[str] | None = None
) -> list[ResolvedModel]:
    return await get_router().resolve_chain(stage, project_id, exclude_refs=exclude_refs)


async def resolve_pair(
    generator_stage: str, reviewer_stage: str, project_id: int | None = None
) -> tuple[ResolvedModel, ResolvedModel]:
    return await get_router().resolve_pair(generator_stage, reviewer_stage, project_id)


__all__ = [
    "ROUTING_CACHE_TTL_SECONDS",
    "ModelRouter",
    "STAGES",
    "get_router",
    "invalidate_routing_cache",
    "resolve",
    "resolve_chain",
    "resolve_pair",
    "set_router",
]
