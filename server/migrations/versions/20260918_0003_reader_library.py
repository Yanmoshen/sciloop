# Copyright 2026 SciLoop contributors
# Licensed under the Apache License, Version 2.0 (http://www.apache.org/licenses/LICENSE-2.0)
"""全文阅读器库（EasyPaper 四核心模块之 ③）：阅读文档 / 不可变版本 / 阅读状态 / 批注。

建表顺序（外键依赖决定，**紧接 0002 之后**）
------------------------------------------
1. ``reader_documents``  依赖 ``papers``（ON DELETE CASCADE）
2. ``reader_versions``   依赖 ``reader_documents``（ON DELETE CASCADE）
3. ``reader_states``     依赖 ``reader_documents``（ON DELETE CASCADE，document_id UNIQUE）
4. ``reader_annotations`` 依赖 ``reader_documents`` / ``reader_versions``（SET NULL）

为什么不放进 0001
------------------
0001 是**附录 A.0 的 30 张表**的权威实现，被 ``scripts/verify/empty_db_migration.sh``
按表清单逐条断言；阅读器四表属新模块，按 ``docs/README-工程.md`` §4 的迁移约定
以增量迁移追加，不改 0001 / 0002 一行。

不可变护栏（与 ``experiment_passports`` 同一做法，缺陷 B 的口径复用）
--------------------------------------------------------------------
``reader_versions`` 创建后**不得原地 UPDATE**：
- ORM 层：``app/services/reader/versions.py`` 的 ``before_update`` 抛
  ``ReaderVersionImmutableError(code='reader_version_immutable')``；
- 数据库层：本迁移的 ``BEFORE UPDATE FOR EACH ROW`` 触发器直接 RAISE，
  报错信息同样含 ``reader_version_immutable`` 字面量，防止绕过 ORM 的裸 SQL 改写历史。
只读端（下载 PDF / 文本索引）不产生 UPDATE，不会被误伤。

回滚
----
``downgrade()`` 先 ``DROP TRIGGER`` + ``DROP FUNCTION``，再按建表**逆序**删表，
与 ``upgrade()`` 严格互逆，保证 ``alembic downgrade base`` 干净回滚。

命名注意
--------
``reader_states`` 的偏移列命名为 ``block_offset`` 而**不是** ``offset``：
``offset`` 是 PostgreSQL 保留字，出现在列定义处会直接 ``syntax error at or near "offset"``
（首次容器内空库迁移实测即因此失败）。ORM 属性与对外 JSON 字段名仍为 ``offset``，
HTTP 契约不变。
"""

from __future__ import annotations

from alembic import op

revision = "0003_reader_library"
down_revision = "0002_passport_immutable_guard"
branch_labels = None
depends_on = None

TABLE_NAME = "reader_versions"
FUNCTION_NAME = "sciloop_reader_versions_immutable"
TRIGGER_NAME = "trg_reader_versions_immutable"

# --------------------------------------------------------------------------- #
# 1. 建表（外键依赖顺序）
# --------------------------------------------------------------------------- #
_TABLES: tuple[tuple[str, str], ...] = (
    (
        "reader_documents",
        """
        CREATE TABLE reader_documents (
            id              BIGSERIAL PRIMARY KEY,
            paper_id        BIGINT NOT NULL REFERENCES papers(id) ON DELETE CASCADE,
            title           TEXT NOT NULL,
            title_source    VARCHAR(24) NOT NULL DEFAULT 'paper'
                            CONSTRAINT ck_reader_documents_title_source
                            CHECK (title_source IN ('pdf_meta','heading','paper')),
            schema_version  SMALLINT NOT NULL DEFAULT 1,
            parse_status    VARCHAR(16) NOT NULL
                            CONSTRAINT ck_reader_documents_parse_status
                            CHECK (parse_status IN ('ok','partial','unavailable')),
            fingerprint     CHAR(64) NOT NULL,
            source_url      TEXT NOT NULL,
            page_count      INTEGER NOT NULL,
            block_count     INTEGER NOT NULL DEFAULT 0,
            section_count   INTEGER NOT NULL DEFAULT 0,
            document        JSONB NOT NULL,
            payload_sha256  CHAR(64) NOT NULL,
            warnings        JSONB NOT NULL DEFAULT '[]'::jsonb,
            created_at      TIMESTAMPTZ DEFAULT now(),
            updated_at      TIMESTAMPTZ DEFAULT now(),
            CONSTRAINT uq_reader_documents_paper_id_fingerprint UNIQUE (paper_id, fingerprint)
        )
        """,
    ),
    (
        "reader_versions",
        """
        CREATE TABLE reader_versions (
            id                    BIGSERIAL PRIMARY KEY,
            document_id           BIGINT NOT NULL REFERENCES reader_documents(id) ON DELETE CASCADE,
            version_no            INTEGER NOT NULL,
            kind                  VARCHAR(16) NOT NULL
                                  CONSTRAINT ck_reader_versions_kind
                                  CHECK (kind IN ('original','chinese','simple','bilingual')),
            task_id               VARCHAR(64),
            engine                VARCHAR(48),
            file_name             VARCHAR(160) NOT NULL,
            file_sha256           CHAR(64) NOT NULL,
            file_size_bytes       BIGINT NOT NULL,
            index_file_name       VARCHAR(160) NOT NULL,
            index_sha256          CHAR(64) NOT NULL,
            source_sha256         CHAR(64),
            pdf_sha256            CHAR(64),
            hash_verified         BOOLEAN NOT NULL DEFAULT FALSE,
            page_map              JSONB NOT NULL,
            block_count           INTEGER NOT NULL DEFAULT 0,
            layout_warnings       JSONB NOT NULL DEFAULT '[]'::jsonb,
            manifest_snapshot     JSONB,
            content_fingerprint   CHAR(64) NOT NULL,
            created_at            TIMESTAMPTZ DEFAULT now(),
            CONSTRAINT uq_reader_versions_document_id_version_no UNIQUE (document_id, version_no),
            CONSTRAINT uq_reader_versions_document_id_kind UNIQUE (document_id, kind)
        )
        """,
    ),
    (
        "reader_states",
        """
        CREATE TABLE reader_states (
            id                 BIGSERIAL PRIMARY KEY,
            document_id        BIGINT NOT NULL UNIQUE
                               REFERENCES reader_documents(id) ON DELETE CASCADE,
            current_block      VARCHAR(96),
            block_offset       INTEGER NOT NULL DEFAULT 0
                               CONSTRAINT ck_reader_states_offset_non_negative CHECK (block_offset >= 0),
            mode               VARCHAR(16) NOT NULL DEFAULT 'original'
                               CONSTRAINT ck_reader_states_mode
                               CHECK (mode IN ('original','chinese','simple','bilingual')),
            font_size          SMALLINT NOT NULL DEFAULT 16
                               CONSTRAINT ck_reader_states_font_size_range
                               CHECK (font_size BETWEEN 8 AND 48),
            understood_blocks  JSONB NOT NULL DEFAULT '[]'::jsonb,
            favorite_terms     JSONB NOT NULL DEFAULT '[]'::jsonb,
            revision           INTEGER NOT NULL DEFAULT 1,
            created_at         TIMESTAMPTZ DEFAULT now(),
            updated_at         TIMESTAMPTZ DEFAULT now()
        )
        """,
    ),
    (
        "reader_annotations",
        """
        CREATE TABLE reader_annotations (
            id                   BIGSERIAL PRIMARY KEY,
            document_id          BIGINT NOT NULL REFERENCES reader_documents(id) ON DELETE CASCADE,
            uid                  VARCHAR(64) NOT NULL,
            version_id           BIGINT REFERENCES reader_versions(id) ON DELETE SET NULL,
            block_id             VARCHAR(96),
            page                 INTEGER,
            bbox                 JSONB,
            quote_text           TEXT NOT NULL,
            quote_sha256         CHAR(64) NOT NULL,
            anchor_text          TEXT NOT NULL,
            anchor_sha256        CHAR(64) NOT NULL,
            anchor_resolved      BOOLEAN NOT NULL DEFAULT FALSE,
            kind                 VARCHAR(16) NOT NULL DEFAULT 'highlight'
                                 CONSTRAINT ck_reader_annotations_kind
                                 CHECK (kind IN ('highlight','note','question','summary')),
            note                 TEXT,
            align_status         VARCHAR(16) NOT NULL DEFAULT 'pending'
                                 CONSTRAINT ck_reader_annotations_align_status
                                 CHECK (align_status IN ('success','partial','pending')),
            projections          JSONB NOT NULL DEFAULT '[]'::jsonb,
            content_fingerprint  CHAR(64) NOT NULL,
            revision             INTEGER NOT NULL DEFAULT 1
                                 CONSTRAINT ck_reader_annotations_revision_positive CHECK (revision >= 1),
            created_at           TIMESTAMPTZ DEFAULT now(),
            updated_at           TIMESTAMPTZ DEFAULT now(),
            CONSTRAINT uq_reader_annotations_document_id_uid UNIQUE (document_id, uid)
        )
        """,
    ),
)

_INDEXES: tuple[str, ...] = (
    "CREATE INDEX idx_reader_documents_paper ON reader_documents (paper_id)",
    "CREATE INDEX idx_reader_versions_document ON reader_versions (document_id)",
    "CREATE INDEX idx_reader_annotations_document"
    " ON reader_annotations (document_id, align_status)",
)

# --------------------------------------------------------------------------- #
# 2. 数据库级不可变护栏（与 0002 的 experiment_passports 同口径）
# --------------------------------------------------------------------------- #
_CREATE_FUNCTION = f"""
CREATE OR REPLACE FUNCTION {FUNCTION_NAME}() RETURNS trigger
LANGUAGE plpgsql AS $$
BEGIN
    RAISE EXCEPTION
        'reader_version_immutable: reader_versions 创建后不可 UPDATE'
        '（EasyPaper §4.8 / reader_versions 为不可变版本凭证）；'
        '如需新版本请重新登记（kind 已存在时返回 409），禁止原地改写历史版本'
        USING ERRCODE = 'raise_exception',
              HINT = '重新调用 POST /api/v1/reader/documents/{{document_id}}/versions 登记新版本';
END;
$$;
"""

_CREATE_TRIGGER = f"""
CREATE TRIGGER {TRIGGER_NAME}
BEFORE UPDATE ON {TABLE_NAME}
FOR EACH ROW
EXECUTE FUNCTION {FUNCTION_NAME}();
"""

_DROP_TRIGGER = f"DROP TRIGGER IF EXISTS {TRIGGER_NAME} ON {TABLE_NAME}"
_DROP_FUNCTION = f"DROP FUNCTION IF EXISTS {FUNCTION_NAME}()"


def _exec(sql: str) -> None:
    """执行原文 DDL（``exec_driver_sql`` 不走 text() 解析，避免 ``$$`` 被误判为绑定参数）。"""
    op.get_bind().exec_driver_sql(sql)


def upgrade() -> None:
    for _name, ddl in _TABLES:
        _exec(ddl)
    for statement in _INDEXES:
        _exec(statement)
    _exec(_CREATE_FUNCTION)
    _exec(_DROP_TRIGGER)  # 幂等：重复执行 upgrade head 时先清掉同名触发器
    _exec(_CREATE_TRIGGER)


def downgrade() -> None:
    _exec(_DROP_TRIGGER)
    _exec(_DROP_FUNCTION)
    for name, _ddl in reversed(_TABLES):
        _exec(f"DROP TABLE IF EXISTS {name} CASCADE")
