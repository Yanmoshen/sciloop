# Copyright 2026 SciLoop contributors
# Licensed under the Apache License, Version 2.0 (http://www.apache.org/licenses/LICENSE-2.0)
"""初始迁移：严格按附录 A.0 顺序建立全部 30 张表。

为什么用手写 DDL 而不是 ``op.create_table``
------------------------------------------
附录 A.0 对**建表顺序**、**延迟外键**（循环外键 + 后建表外键）与**列的可空性**有硬性规定，
autogenerate 无法保证顺序，因此这里逐条执行与附录 A 等价的原文 SQL。

三段式结构
----------
1. ``_TABLES``    建表（projects 首表且**不含 taskbook_id**）
2. ``_INDEXES``   索引与 ``paper_cards_latest`` 视图
3. ``_DEFERRED_FKS`` 末段 ``ALTER TABLE ADD COLUMN / ADD CONSTRAINT`` 补齐 5 个外键
"""

from __future__ import annotations

from alembic import op

revision = "0001_initial_schema"
down_revision = None
branch_labels = None
depends_on = None


# --------------------------------------------------------------------------- #
# 1. 建表（附录 A.0 顺序，1..30）
# --------------------------------------------------------------------------- #
_TABLES: tuple[tuple[str, str], ...] = (
    (
        "projects",
        """
        CREATE TABLE projects (
            id           BIGSERIAL PRIMARY KEY,
            name         VARCHAR(200) NOT NULL,
            idea_id      BIGINT,
            status       VARCHAR(24) NOT NULL DEFAULT 'DRAFT',
            mode         VARCHAR(16) NOT NULL DEFAULT 'manual',
            current_iteration INTEGER DEFAULT 0,
            settings     JSONB,
            is_demo      BOOLEAN DEFAULT FALSE,
            created_at   TIMESTAMPTZ DEFAULT now(),
            updated_at   TIMESTAMPTZ DEFAULT now(),
            CONSTRAINT ck_projects_mode CHECK (mode IN ('manual','auto'))
        )
        """,
    ),
    (
        "papers",
        """
        CREATE TABLE papers (
            id                BIGSERIAL PRIMARY KEY,
            source            VARCHAR(32)  NOT NULL,
            external_id       VARCHAR(128) NOT NULL,
            doi               VARCHAR(256),
            title             TEXT         NOT NULL,
            abstract          TEXT,
            authors           JSONB,
            published_at      DATE,
            updated_at_src    DATE,
            venue             VARCHAR(128),
            venue_source      VARCHAR(32),
            venue_level       SMALLINT,
            citation_count    INTEGER      DEFAULT 0,
            citation_velocity NUMERIC(8,3),
            code_url          VARCHAR(512),
            code_heat         NUMERIC(8,3),
            institution_score NUMERIC(8,3),
            llm_novelty       NUMERIC(8,3),
            llm_novelty_low   NUMERIC(8,3),
            llm_novelty_high  NUMERIC(8,3),
            llm_novelty_note  TEXT,
            llm_novelty_stable BOOLEAN,
            rank_score        NUMERIC(8,3),
            rank_breakdown    JSONB,
            influence_score   NUMERIC(8,3),
            score_breakdown   JSONB,
            score_coverage    NUMERIC(4,3),
            pdf_url           TEXT,
            is_parsed         BOOLEAN      DEFAULT FALSE,
            raw               JSONB,
            created_at        TIMESTAMPTZ  DEFAULT now(),
            updated_at        TIMESTAMPTZ  DEFAULT now(),
            CONSTRAINT uq_papers_source_external_id UNIQUE (source, external_id)
        )
        """,
    ),
    (
        "paper_identities",
        """
        CREATE TABLE paper_identities (
            id            BIGSERIAL PRIMARY KEY,
            paper_id      BIGINT NOT NULL REFERENCES papers(id) ON DELETE CASCADE,
            id_type       VARCHAR(24) NOT NULL,
            id_value      VARCHAR(256) NOT NULL,
            is_primary    BOOLEAN DEFAULT FALSE,
            created_at    TIMESTAMPTZ DEFAULT now(),
            CONSTRAINT uq_paper_identities_id_type_id_value UNIQUE (id_type, id_value)
        )
        """,
    ),
    (
        "paper_documents",
        """
        CREATE TABLE paper_documents (
            id                BIGSERIAL PRIMARY KEY,
            paper_id          BIGINT NOT NULL REFERENCES papers(id) ON DELETE CASCADE,
            document_version  VARCHAR(64) NOT NULL,
            source_type       VARCHAR(16) NOT NULL,
            source_url        TEXT NOT NULL,
            parser            VARCHAR(48) NOT NULL,
            parser_version    VARCHAR(32) NOT NULL,
            page_count        INTEGER,
            text_sha256       CHAR(64) NOT NULL,
            char_count        INTEGER,
            locatable_chars   INTEGER,
            coverage          NUMERIC(4,3),
            parse_status      VARCHAR(16) NOT NULL
                              CONSTRAINT ck_paper_documents_parse_status
                              CHECK (parse_status IN ('ok','partial','unavailable','failed')),
            parse_error       TEXT,
            parsed_at         TIMESTAMPTZ DEFAULT now(),
            created_at        TIMESTAMPTZ DEFAULT now(),
            CONSTRAINT uq_paper_documents_paper_id_document_version
                UNIQUE (paper_id, document_version)
        )
        """,
    ),
    (
        "paper_source_records",
        """
        CREATE TABLE paper_source_records (
            id           BIGSERIAL PRIMARY KEY,
            paper_id     BIGINT NOT NULL REFERENCES papers(id) ON DELETE CASCADE,
            source       VARCHAR(32) NOT NULL,
            field_name   VARCHAR(64) NOT NULL,
            raw_value    TEXT,
            confidence   NUMERIC(4,3),
            request_url  TEXT,
            http_status  INTEGER,
            fetched_at   TIMESTAMPTZ DEFAULT now(),
            created_at   TIMESTAMPTZ DEFAULT now()
        )
        """,
    ),
    (
        "paper_cards",
        """
        CREATE TABLE paper_cards (
            id            BIGSERIAL PRIMARY KEY,
            paper_id      BIGINT NOT NULL REFERENCES papers(id) ON DELETE CASCADE,
            version       INTEGER NOT NULL DEFAULT 1,
            research_problem   TEXT NOT NULL,
            core_method        TEXT NOT NULL,
            key_innovation     JSONB NOT NULL,
            technical_route    JSONB NOT NULL,
            experimental_setup JSONB NOT NULL,
            main_conclusions   JSONB NOT NULL,
            limitations        JSONB NOT NULL,
            transferable       JSONB NOT NULL,
            llm_call_log_id    BIGINT,
            created_at    TIMESTAMPTZ DEFAULT now(),
            updated_at    TIMESTAMPTZ DEFAULT now(),
            CONSTRAINT uq_paper_cards_paper_id_version UNIQUE (paper_id, version)
        )
        """,
    ),
    (
        "paper_spans",
        """
        CREATE TABLE paper_spans (
            id          BIGSERIAL PRIMARY KEY,
            paper_id    BIGINT NOT NULL REFERENCES papers(id) ON DELETE CASCADE,
            document_version VARCHAR(64) NOT NULL,
            section_name VARCHAR(64),
            page_number INTEGER,
            bbox        JSONB,
            char_start  INTEGER NOT NULL,
            char_end    INTEGER NOT NULL,
            quote_text  TEXT NOT NULL,
            quote_sha256 CHAR(64) NOT NULL,
            created_at  TIMESTAMPTZ DEFAULT now()
        )
        """,
    ),
    (
        "paper_feed_snapshots",
        """
        CREATE TABLE paper_feed_snapshots (
            id          BIGSERIAL PRIMARY KEY,
            view_type   VARCHAR(16) NOT NULL,
            filters     JSONB,
            paper_ids   JSONB NOT NULL,
            is_demo     BOOLEAN DEFAULT FALSE,
            created_at  TIMESTAMPTZ DEFAULT now()
        )
        """,
    ),
    (
        "aggregations",
        """
        CREATE TABLE aggregations (
            id             BIGSERIAL PRIMARY KEY,
            project_id     BIGINT REFERENCES projects(id) ON DELETE SET NULL,
            paper_ids      JSONB NOT NULL,
            comparison_matrix JSONB NOT NULL,
            method_evolution  JSONB NOT NULL,
            created_at     TIMESTAMPTZ DEFAULT now()
        )
        """,
    ),
    (
        "gaps",
        """
        CREATE TABLE gaps (
            id             BIGSERIAL PRIMARY KEY,
            aggregation_id BIGINT NOT NULL REFERENCES aggregations(id) ON DELETE CASCADE,
            gap_text       TEXT NOT NULL,
            raised_by_paper_ids JSONB NOT NULL,
            unsolved_evidence   JSONB NOT NULL,
            novelty_hint   TEXT,
            created_at     TIMESTAMPTZ DEFAULT now()
        )
        """,
    ),
    (
        "ideas",
        """
        CREATE TABLE ideas (
            id             BIGSERIAL PRIMARY KEY,
            project_id     BIGINT REFERENCES projects(id) ON DELETE CASCADE,
            aggregation_id BIGINT REFERENCES aggregations(id) ON DELETE SET NULL,
            origin         VARCHAR(16) NOT NULL,
            title          TEXT NOT NULL,
            content        TEXT NOT NULL,
            mechanism      VARCHAR(32),
            novelty_note   TEXT,
            is_selected    BOOLEAN DEFAULT FALSE,
            created_at     TIMESTAMPTZ DEFAULT now()
        )
        """,
    ),
    (
        "feasibilities",
        """
        CREATE TABLE feasibilities (
            id                BIGSERIAL PRIMARY KEY,
            idea_id           BIGINT NOT NULL REFERENCES ideas(id) ON DELETE CASCADE,
            data_availability JSONB NOT NULL,
            compute_cost      JSONB NOT NULL,
            method_maturity   JSONB NOT NULL,
            novelty_gap       JSONB NOT NULL,
            total_score       NUMERIC(5,2) NOT NULL,
            risk_list         JSONB NOT NULL,
            mve_plan          JSONB NOT NULL,
            created_at        TIMESTAMPTZ DEFAULT now()
        )
        """,
    ),
    (
        "taskbooks",
        """
        CREATE TABLE taskbooks (
            id             BIGSERIAL PRIMARY KEY,
            project_id     BIGINT NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
            idea_id        BIGINT NOT NULL REFERENCES ideas(id),
            research_question TEXT NOT NULL,
            target_datasets   JSONB NOT NULL,
            baselines         JSONB NOT NULL,
            metrics           JSONB NOT NULL,
            compute_budget    JSONB NOT NULL,
            deliverables      JSONB NOT NULL,
            max_iterations          INTEGER DEFAULT 3,
            score_threshold         NUMERIC(5,2) DEFAULT 80,
            marginal_gain_threshold NUMERIC(5,2) DEFAULT 2,
            max_retry               INTEGER DEFAULT 2,
            status         VARCHAR(16) NOT NULL DEFAULT 'draft',
            locked_at      TIMESTAMPTZ,
            created_at     TIMESTAMPTZ DEFAULT now()
        )
        """,
    ),
    (
        "pipeline_runs",
        """
        CREATE TABLE pipeline_runs (
            id            BIGSERIAL PRIMARY KEY,
            project_id    BIGINT NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
            iteration     INTEGER NOT NULL,
            mode          VARCHAR(16) NOT NULL,
            status        VARCHAR(24) NOT NULL,
            started_at    TIMESTAMPTZ,
            finished_at   TIMESTAMPTZ,
            total_cost_usd NUMERIC(10,4) DEFAULT 0,
            stop_reason   VARCHAR(32),
            created_at    TIMESTAMPTZ DEFAULT now()
        )
        """,
    ),
    (
        "stage_outputs",
        """
        CREATE TABLE stage_outputs (
            id           BIGSERIAL PRIMARY KEY,
            pipeline_run_id BIGINT NOT NULL REFERENCES pipeline_runs(id) ON DELETE CASCADE,
            stage        VARCHAR(24) NOT NULL,
            status       VARCHAR(16) NOT NULL,
            output_json  JSONB,
            output_text  TEXT,
            attempt      INTEGER DEFAULT 1,
            cost_usd     NUMERIC(10,4) DEFAULT 0,
            duration_ms  BIGINT,
            error        TEXT,
            started_at   TIMESTAMPTZ,
            finished_at  TIMESTAMPTZ,
            updated_at   TIMESTAMPTZ DEFAULT now(),
            CONSTRAINT ck_stage CHECK (stage IN
                ('survey','plan','plan_review','experiment','writing','review')),
            CONSTRAINT uq_stage_once UNIQUE (pipeline_run_id, stage, attempt)
        )
        """,
    ),
    (
        "experiments",
        """
        CREATE TABLE experiments (
            id            BIGSERIAL PRIMARY KEY,
            stage_output_id BIGINT REFERENCES stage_outputs(id) ON DELETE CASCADE,
            template_id   VARCHAR(48) NOT NULL,
            config        JSONB NOT NULL,
            script_path   TEXT,
            created_at    TIMESTAMPTZ DEFAULT now(),
            CONSTRAINT ck_template CHECK (template_id IN
              ('T1_prompt_variant','T2_fewshot_ablation','T3_model_compare',
               'T4_llm_as_judge','T5_rag_ablation'))
        )
        """,
    ),
    (
        "experiment_runs",
        """
        CREATE TABLE experiment_runs (
            id            BIGSERIAL PRIMARY KEY,
            experiment_id BIGINT NOT NULL REFERENCES experiments(id) ON DELETE CASCADE,
            attempt       INTEGER DEFAULT 1,
            executor_task_id VARCHAR(64),
            status        VARCHAR(16) NOT NULL,
            raw_output    TEXT,
            artifact_path TEXT,
            error         TEXT,
            duration_ms   BIGINT,
            created_at    TIMESTAMPTZ DEFAULT now()
        )
        """,
    ),
    (
        "experiment_metrics",
        """
        CREATE TABLE experiment_metrics (
            id                 BIGSERIAL PRIMARY KEY,
            experiment_run_id  BIGINT NOT NULL REFERENCES experiment_runs(id) ON DELETE CASCADE,
            metric_name        VARCHAR(64) NOT NULL,
            metric_value       NUMERIC(12,6),
            metric_unit        VARCHAR(24),
            extra              JSONB,
            created_at         TIMESTAMPTZ DEFAULT now()
        )
        """,
    ),
    (
        "review_scores",
        """
        CREATE TABLE review_scores (
            id               BIGSERIAL PRIMARY KEY,
            pipeline_run_id  BIGINT NOT NULL REFERENCES pipeline_runs(id) ON DELETE CASCADE,
            novelty          NUMERIC(5,2),
            rigor            NUMERIC(5,2),
            completeness     NUMERIC(5,2),
            reproducibility  NUMERIC(5,2),
            total            NUMERIC(5,2),
            comments         JSONB,
            created_at       TIMESTAMPTZ DEFAULT now()
        )
        """,
    ),
    (
        "decision_logs",
        """
        CREATE TABLE decision_logs (
            id              BIGSERIAL PRIMARY KEY,
            project_id      BIGINT NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
            pipeline_run_id BIGINT REFERENCES pipeline_runs(id) ON DELETE CASCADE,
            decision_point  VARCHAR(8) NOT NULL,
            stage           VARCHAR(24),
            context_digest  TEXT NOT NULL,
            options_considered JSONB NOT NULL,
            chosen          VARCHAR(64) NOT NULL,
            rationale       TEXT NOT NULL,
            risk_score      NUMERIC(5,2) NOT NULL CHECK (risk_score BETWEEN 0 AND 100),
            confidence_score NUMERIC(4,3) NOT NULL CHECK (confidence_score BETWEEN 0 AND 1),
            reversibility_score NUMERIC(4,3) NOT NULL CHECK (reversibility_score BETWEEN 0 AND 1),
            policy_action   VARCHAR(24) NOT NULL
                            CHECK (policy_action IN ('auto_execute','need_human','circuit_break')),
            policy_version  VARCHAR(32) NOT NULL,
            guardrail_checks JSONB NOT NULL,
            cost_usd        NUMERIC(10,4) DEFAULT 0,
            created_at      TIMESTAMPTZ DEFAULT now(),
            CONSTRAINT ck_dp CHECK (decision_point IN ('D1','D2','D3','D4','D5','D6'))
        )
        """,
    ),
    (
        "interventions",
        """
        CREATE TABLE interventions (
            id              BIGSERIAL PRIMARY KEY,
            project_id      BIGINT NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
            pipeline_run_id BIGINT REFERENCES pipeline_runs(id) ON DELETE CASCADE,
            node            VARCHAR(8) NOT NULL,
            action          VARCHAR(24) NOT NULL,
            payload         JSONB,
            note            TEXT,
            created_at      TIMESTAMPTZ DEFAULT now()
        )
        """,
    ),
    (
        "evidences",
        """
        CREATE TABLE evidences (
            id           BIGSERIAL PRIMARY KEY,
            owner_type   VARCHAR(32) NOT NULL,
            owner_id     BIGINT NOT NULL,
            evidence_type VARCHAR(32) NOT NULL,
            paper_id     BIGINT REFERENCES papers(id),
            paper_span_id BIGINT REFERENCES paper_spans(id),
            card_field   VARCHAR(64),
            experiment_run_id BIGINT REFERENCES experiment_runs(id),
            experiment_passport_id BIGINT,
            metric_name  VARCHAR(64),
            metric_value NUMERIC,
            decision_log_id BIGINT REFERENCES decision_logs(id),
            quote_text   TEXT,
            weight       NUMERIC(4,2) DEFAULT 1.0,
            created_at   TIMESTAMPTZ DEFAULT now()
        )
        """,
    ),
    (
        "llm_call_logs",
        """
        CREATE TABLE llm_call_logs (
            id           BIGSERIAL PRIMARY KEY,
            project_id   BIGINT REFERENCES projects(id) ON DELETE SET NULL,
            stage        VARCHAR(24),
            provider     VARCHAR(48) NOT NULL,
            model        VARCHAR(96) NOT NULL,
            purpose      VARCHAR(64),
            prompt_tokens INTEGER,
            completion_tokens INTEGER,
            cost_usd     NUMERIC(10,6),
            duration_ms  BIGINT,
            success      BOOLEAN NOT NULL,
            is_replay    BOOLEAN DEFAULT FALSE,
            error        TEXT,
            created_at   TIMESTAMPTZ DEFAULT now()
        )
        """,
    ),
    (
        "model_configs",
        """
        CREATE TABLE model_configs (
            id            BIGSERIAL PRIMARY KEY,
            name          VARCHAR(96) NOT NULL,
            base_url      TEXT NOT NULL,
            api_key_enc   TEXT NOT NULL,
            models        JSONB NOT NULL,
            is_default    BOOLEAN DEFAULT FALSE,
            last_tested_at TIMESTAMPTZ,
            test_ok       BOOLEAN,
            created_at    TIMESTAMPTZ DEFAULT now()
        )
        """,
    ),
    (
        "stage_model_routing",
        """
        CREATE TABLE stage_model_routing (
            id           BIGSERIAL PRIMARY KEY,
            project_id   BIGINT REFERENCES projects(id) ON DELETE CASCADE,
            stage        VARCHAR(32) NOT NULL,
            purpose      VARCHAR(64),
            model_config_id BIGINT NOT NULL REFERENCES model_configs(id),
            model_id     VARCHAR(96) NOT NULL,
            temperature  NUMERIC(3,2),
            max_tokens   INTEGER,
            created_at   TIMESTAMPTZ DEFAULT now()
        )
        """,
    ),
    (
        "paper_drafts",
        """
        CREATE TABLE paper_drafts (
            id              BIGSERIAL PRIMARY KEY,
            project_id      BIGINT NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
            pipeline_run_id BIGINT REFERENCES pipeline_runs(id) ON DELETE CASCADE,
            iteration       INTEGER NOT NULL,
            content_md      TEXT NOT NULL,
            claim_coverage  NUMERIC(4,3),
            created_at      TIMESTAMPTZ DEFAULT now()
        )
        """,
    ),
    (
        "draft_claims",
        """
        CREATE TABLE draft_claims (
            id              BIGSERIAL PRIMARY KEY,
            draft_id        BIGINT NOT NULL REFERENCES paper_drafts(id) ON DELETE CASCADE,
            section_heading TEXT,
            claim_text      TEXT NOT NULL,
            is_factual      BOOLEAN NOT NULL DEFAULT TRUE,
            support_status  VARCHAR(16) NOT NULL
                            CHECK (support_status IN ('supported','contradicted','insufficient')),
            status_reason   TEXT,
            evidence_count  INTEGER NOT NULL DEFAULT 0,
            created_at      TIMESTAMPTZ DEFAULT now()
        )
        """,
    ),
    (
        "experiment_passports",
        """
        CREATE TABLE experiment_passports (
            id                  BIGSERIAL PRIMARY KEY,
            passport_uid        UUID NOT NULL UNIQUE,
            experiment_run_id   BIGINT NOT NULL REFERENCES experiment_runs(id) ON DELETE RESTRICT,
            parent_passport_id  BIGINT REFERENCES experiment_passports(id) ON DELETE SET NULL,
            dataset_name        TEXT NOT NULL,
            dataset_version     TEXT NOT NULL,
            dataset_sha256      CHAR(64) NOT NULL,
            sample_manifest     JSONB NOT NULL,
            provider            VARCHAR(48) NOT NULL,
            model_id            VARCHAR(96) NOT NULL,
            prompt_version      VARCHAR(48) NOT NULL,
            prompt_sha256       CHAR(64) NOT NULL,
            generation_params   JSONB NOT NULL,
            template_id         VARCHAR(48) NOT NULL,
            template_config     JSONB NOT NULL,
            code_commit_sha     VARCHAR(64) NOT NULL,
            dependency_lock_sha256 CHAR(64) NOT NULL,
            metrics             JSONB NOT NULL,
            cost_usd            NUMERIC(10,6) NOT NULL,
            is_replay           BOOLEAN NOT NULL DEFAULT FALSE,
            artifact_manifest   JSONB NOT NULL,
            status              VARCHAR(16) NOT NULL
                                CHECK (status IN ('complete','incomplete','failed')),
            started_at          TIMESTAMPTZ NOT NULL,
            finished_at         TIMESTAMPTZ NOT NULL,
            created_at          TIMESTAMPTZ DEFAULT now()
        )
        """,
    ),
    (
        "review_calibrations",
        """
        CREATE TABLE review_calibrations (
            id                  BIGSERIAL PRIMARY KEY,
            pipeline_run_id     BIGINT NOT NULL REFERENCES pipeline_runs(id) ON DELETE CASCADE,
            generator_model_ref TEXT NOT NULL,
            reviewer_model_ref  TEXT NOT NULL,
            anonymization_version VARCHAR(32) NOT NULL,
            shuffle_seed        BIGINT NOT NULL,
            candidate_order     JSONB NOT NULL,
            model_scores        JSONB NOT NULL,
            human_labels        JSONB NOT NULL DEFAULT '[]'::jsonb,
            sample_size         INTEGER NOT NULL DEFAULT 0 CHECK (sample_size >= 0),
            agreement_metric    VARCHAR(24) CHECK (agreement_metric IN ('cohen_kappa','mae')),
            agreement_value     NUMERIC(8,4),
            confidence_interval JSONB,
            status              VARCHAR(16) NOT NULL DEFAULT 'pending'
                                CHECK (status IN ('pending','calibrated')),
            created_at          TIMESTAMPTZ DEFAULT now()
        )
        """,
    ),
    (
        "demo_fixtures",
        """
        CREATE TABLE demo_fixtures (
            id            BIGSERIAL PRIMARY KEY,
            fixture_type  VARCHAR(32) NOT NULL,
            fixture_key   VARCHAR(128) NOT NULL,
            payload       JSONB NOT NULL,
            note          TEXT,
            created_at    TIMESTAMPTZ DEFAULT now(),
            CONSTRAINT uq_demo_fixtures_fixture_type_fixture_key UNIQUE (fixture_type, fixture_key)
        )
        """,
    ),
)


# --------------------------------------------------------------------------- #
# 2. 索引与视图（附录 A.2/A.3/A.6）
# --------------------------------------------------------------------------- #
_INDEXES: tuple[str, ...] = (
    "CREATE INDEX idx_papers_influence ON papers (influence_score DESC NULLS LAST)",
    "CREATE INDEX idx_papers_rank      ON papers (rank_score DESC NULLS LAST)",
    "CREATE INDEX idx_papers_published ON papers (published_at DESC)",
    "CREATE INDEX idx_papers_venue_lvl ON papers (venue_level DESC NULLS LAST)",
    "CREATE INDEX idx_papers_doi       ON papers (doi)",
    "CREATE INDEX idx_pident_paper     ON paper_identities (paper_id)",
    "CREATE INDEX idx_pdoc_paper       ON paper_documents (paper_id, parse_status)",
    "CREATE INDEX idx_src_records_paper ON paper_source_records (paper_id, field_name)",
    "CREATE INDEX idx_evidences_owner ON evidences (owner_type, owner_id)",
    "CREATE INDEX idx_llm_logs_project ON llm_call_logs (project_id, stage)",
    "CREATE INDEX idx_claims_draft ON draft_claims (draft_id, support_status)",
    """
    CREATE VIEW paper_cards_latest AS
    SELECT * FROM paper_cards c
    WHERE version = (SELECT MAX(version) FROM paper_cards WHERE paper_id = c.paper_id)
    """,
)


# --------------------------------------------------------------------------- #
# 3. 末段：补循环外键与后建表外键（附录 A.0 第 31 步）
# --------------------------------------------------------------------------- #
_DEFERRED_FKS: tuple[str, ...] = (
    "ALTER TABLE projects ADD COLUMN taskbook_id BIGINT",
    """
    ALTER TABLE projects ADD CONSTRAINT fk_projects_taskbook
        FOREIGN KEY (taskbook_id) REFERENCES taskbooks(id) ON DELETE SET NULL
    """,
    """
    ALTER TABLE projects ADD CONSTRAINT fk_projects_idea
        FOREIGN KEY (idea_id) REFERENCES ideas(id) ON DELETE SET NULL
    """,
    """
    ALTER TABLE paper_cards ADD CONSTRAINT fk_paper_cards_llm_call
        FOREIGN KEY (llm_call_log_id) REFERENCES llm_call_logs(id) ON DELETE SET NULL
    """,
    """
    ALTER TABLE evidences ADD CONSTRAINT fk_evidences_passport
        FOREIGN KEY (experiment_passport_id) REFERENCES experiment_passports(id) ON DELETE SET NULL
    """,
    """
    ALTER TABLE paper_spans ADD CONSTRAINT fk_spans_document
        FOREIGN KEY (paper_id, document_version)
        REFERENCES paper_documents(paper_id, document_version) ON DELETE CASCADE
    """,
)

# downgrade 顺序：先删外键与补列，再按建表逆序删表
_DROP_FKS: tuple[str, ...] = (
    "ALTER TABLE paper_spans DROP CONSTRAINT IF EXISTS fk_spans_document",
    "ALTER TABLE evidences DROP CONSTRAINT IF EXISTS fk_evidences_passport",
    "ALTER TABLE paper_cards DROP CONSTRAINT IF EXISTS fk_paper_cards_llm_call",
    "ALTER TABLE projects DROP CONSTRAINT IF EXISTS fk_projects_idea",
    "ALTER TABLE projects DROP CONSTRAINT IF EXISTS fk_projects_taskbook",
    "ALTER TABLE projects DROP COLUMN IF EXISTS taskbook_id",
)


def _exec(sql: str) -> None:
    """执行原文 DDL（``exec_driver_sql`` 不走 text() 解析，避免 ``::jsonb`` 被误判为绑定参数）。"""
    op.get_bind().exec_driver_sql(sql)


def upgrade() -> None:
    for _name, ddl in _TABLES:
        _exec(ddl)
    for statement in _INDEXES:
        _exec(statement)
    for statement in _DEFERRED_FKS:
        _exec(statement)


def downgrade() -> None:
    for statement in _DROP_FKS:
        _exec(statement)
    _exec("DROP VIEW IF EXISTS paper_cards_latest")
    for name, _ddl in reversed(_TABLES):
        _exec(f"DROP TABLE IF EXISTS {name} CASCADE")
