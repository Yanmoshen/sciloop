# Copyright 2026 SciLoop contributors
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
"""应用配置：全部来自环境变量，禁止硬编码密钥。"""

from __future__ import annotations

import json
from functools import lru_cache
from typing import Any, Literal

from pydantic import Field, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

AccessMode = Literal["public_demo", "owner_mode"]


class Settings(BaseSettings):
    """SciLoop 运行时配置（键名与 contracts.env_keys / 附录 F.1 对齐）。"""

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
        case_sensitive=False,
    )

    # ===== 应用 =====
    app_env: str = "development"
    app_secret_key: str = "change_me"
    app_base_url: str = "http://localhost:8080"
    app_access_mode: AccessMode = "public_demo"
    owner_token: str = ""
    public_replay_rate_limit_per_hour: int = 10

    # ===== 数据库 =====
    postgres_host: str = "db"
    postgres_port: int = 5432
    postgres_db: str = "sciloop"
    postgres_user: str = "sciloop"
    postgres_password: str = "change_me"
    database_url: str = "postgresql+asyncpg://sciloop:change_me@db:5432/sciloop"

    # ===== 论文数据源 =====
    arxiv_api_base: str = "https://export.arxiv.org/api/query"
    arxiv_fields: str = "cs.AI,cs.CL,cs.CV,cs.LG"
    semantic_scholar_api_key: str = ""
    semantic_scholar_qps: float = 1.0
    openalex_mailto: str = ""
    openalex_api_base: str = "https://api.openalex.org"
    github_token: str = ""
    source_fetch_timeout_seconds: int = 20
    source_cache_ttl_hours: int = 24

    # ===== 排序与影响力 =====
    rank_weights: dict[str, float] = Field(
        default_factory=lambda: {
            "relevance": 0.40,
            "recency": 0.25,
            "citation_trend": 0.20,
            "evidence_completeness": 0.15,
        }
    )
    recency_halflife_days: int = 180
    influence_weights: dict[str, float] = Field(
        default_factory=lambda: {"venue": 0.40, "citation_velocity": 0.40, "code_heat": 0.20}
    )
    llm_novelty_enabled: bool = True
    llm_novelty_stability_tolerance: int = 15
    venue_whitelist_file: str = "config/venue_whitelist.json"

    # ===== 全文解析 =====
    fulltext_html_first: bool = True
    fulltext_pdf_parser: str = "pymupdf"
    fulltext_min_coverage: float = 0.60
    fulltext_max_pages: int = 40
    fulltext_text_cache_dir: str = Field(
        default="",
        description=(
            "归一全文文本缓存目录（WP01 收尾新增）。空 = 沿用 text_cache 的临时目录默认值。"
            "容器内由 docker-compose.yml 固定为 /app/server/.cache/fulltext 并 bind mount "
            "到宿主 ./.data/fulltext-cache，避免容器重建后 offset 校验退化为 valid_by_hash。"
            "注意：services.fulltext.text_cache 直接读同名环境变量（不经过本设置），"
            "此处提供设置项供后续服务/脚本按配置访问同一目录。"
        ),
    )

    fetch_task_history_dir: str = Field(
        default="",
        description=(
            "抓取/同步任务历史目录（任务监控用）。空 = 默认 <backend>/.cache/tasks；"
            "容器内由 docker-compose.yml bind mount 到宿主 ./.data/tasks，"
            "以便容器重建后历史记录仍可查询（任务登记本身只存在进程内存里）。"
        ),
    )

    # ===== 批量任务调度（APScheduler）=====
    scheduler_enabled: bool = Field(
        default=False,
        description="1 = backend 启动时注册 fetch_papers / score_papers / parse_fulltext；默认关闭",
    )
    scheduler_fetch_interval_hours: int = Field(default=24, ge=1)
    scheduler_score_interval_hours: int = Field(default=12, ge=1)
    scheduler_parse_interval_hours: int = Field(default=12, ge=1)
    scheduler_job_limit: int = Field(default=50, ge=1, description="每轮任务的默认处理上限")

    # ===== LLM 兜底 =====
    llm_default_base_url: str = "https://api.deepseek.com/v1"
    llm_default_api_key: str = ""
    llm_default_model: str = "deepseek-chat"
    llm_fallback_base_url: str = ""
    llm_fallback_api_key: str = ""
    llm_fallback_model: str = ""
    llm_replay: bool = False
    llm_json_retry: int = 2

    # ===== 流水线 =====
    pipeline_max_iterations: int = 3
    pipeline_score_threshold: float = 80.0
    pipeline_marginal_gain_threshold: float = 2.0
    pipeline_max_retry: int = 2
    pipeline_max_llm_cost_usd: float = 8.0
    pipeline_demo_cost_quota_usd: float = 3.0
    pipeline_max_stage_minutes: int = 20

    # ===== 风险策略 =====
    risk_auto_max: float = 30.0
    risk_human_max: float = 70.0
    decision_confidence_auto_min: float = 0.75
    decision_confidence_break_min: float = 0.50
    decision_reversibility_auto_min: float = 0.60

    # ===== 实验执行器 =====
    executor_max_concurrency: int = 2
    executor_run_timeout_seconds: int = 300
    executor_max_output_bytes: int = 5_242_880
    executor_allowed_hosts: str = ""
    executor_deny_private_network: bool = True

    # ===== Passport =====
    passport_code_commit_cmd: str = "git rev-parse HEAD"
    passport_dependency_lock: str = "poetry.lock"
    passport_require_complete: bool = True

    # ===== 校准 =====
    calibration_human_label_min: int = 3
    calibration_metric: str = "cohen_kappa"
    calibration_report_ci: bool = True

    # ===== 演示模式 =====
    demo_snapshot_enabled: bool = True
    demo_seed_on_startup: bool = False

    # ===== 前端（构建期注入）=====
    vite_api_base: str = "/api/v1"
    vite_default_theme: str = "light"

    # ------------------------------------------------------------------ #
    @field_validator("app_access_mode", mode="before")
    @classmethod
    def _normalize_access_mode(cls, value: Any) -> Any:
        """兼容附录 F.1 的简写 ``owner``，统一归一为 ``owner_mode``。"""
        if isinstance(value, str):
            normalized = value.strip().lower()
            if normalized == "owner":
                return "owner_mode"
            return normalized
        return value

    @field_validator("rank_weights", "influence_weights", mode="before")
    @classmethod
    def _parse_json_mapping(cls, value: Any) -> Any:
        """支持以 JSON 字符串形式提供权重（.env 中常见）。"""
        if isinstance(value, str):
            text = value.strip()
            if not text:
                return {}
            return json.loads(text)
        return value

    @property
    def is_owner_mode(self) -> bool:
        return self.app_access_mode == "owner_mode"

    @property
    def allowed_hosts(self) -> list[str]:
        return [h.strip() for h in self.executor_allowed_hosts.split(",") if h.strip()]

    @property
    def sync_database_url(self) -> str:
        """同步驱动 URL（alembic 同步迁移与批量任务使用）。

        ``postgresql://`` 在 SQLAlchemy 中默认解析到 psycopg2，而本项目只装 psycopg3，
        因此这里显式改写为 ``postgresql+psycopg://``。
        """
        url = (self.database_url or "").strip()
        if url.startswith("postgres://"):
            url = "postgresql://" + url[len("postgres://") :]
        if "+asyncpg" in url:
            return url.replace("+asyncpg", "+psycopg")
        if url.startswith("postgresql://"):
            return "postgresql+psycopg://" + url[len("postgresql://") :]
        return url


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    """进程内单例配置。"""
    return Settings()


settings = get_settings()
