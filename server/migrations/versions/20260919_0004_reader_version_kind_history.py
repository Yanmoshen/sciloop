# Copyright 2026 SciLoop contributors
# Licensed under the Apache License, Version 2.0 (http://www.apache.org/licenses/LICENSE-2.0)
"""同一 kind 允许保留历史版本：把 ``UNIQUE (document_id, kind)`` 放宽为
``UNIQUE (document_id, kind, coalesce(task_id, ''))``。

为什么改
--------
``0003`` 的 ``uq_reader_versions_document_id_kind`` 让「每个 kind 只能有一个版本」。
后果：**翻译引擎修好后，新产物登记不进来** —— ``chinese`` 已被早期产物占用，
``POST /reader/documents/{id}/versions`` 只能回 409，用户在阅读器里永远看不到修正后的译文
（2026-09-19 实测复现：`version_already_registered`）。

新口径（append-only + 最新即当前）
----------------------------------
- 同一 ``(document_id, kind, task_id)`` 重复登记 → 仍然 409 ``version_already_registered``
  （幂等重入防护不变，``scripts/verify/modules_acceptance.sh`` 的 ``C3-duplicate-409`` 仍通过）；
- 同一 ``kind`` 的**不同 task_id** 追加为更高 ``version_no`` 的新版本；
- 旧版本一行不动 —— 表上仍有 ``0003`` 的 ``BEFORE UPDATE`` 触发器，历史不可改写；
- 阅读器按 ``version_no`` 倒序展示，最新版排在前面（"当前版本"= 同 kind 的最大 ``version_no``）。

为什么用 ``coalesce(task_id, '')``
----------------------------------
``original`` 版本的 ``task_id`` 是 ``NULL``，而 PostgreSQL 唯一索引**不约束 NULL**
（多行 NULL 互不冲突）。加 ``coalesce`` 归一后 ``original`` 依旧每文档唯一，DB 层护栏不退化。

回滚
----
``downgrade()`` 重建旧约束；**若已存在同 kind 的多个版本，重建会失败并如实报错**，
不自动删除任何历史记录 —— 这种情况需要人工决策，宁可失败也不静默丢历史。
"""

from __future__ import annotations

from alembic import op

revision = "0004_reader_version_kind_history"
down_revision = "0003_reader_library"

OLD_CONSTRAINT = "uq_reader_versions_document_id_kind"
NEW_INDEX = "uq_reader_versions_document_kind_task"


def upgrade() -> None:
    op.execute(f"ALTER TABLE reader_versions DROP CONSTRAINT IF EXISTS {OLD_CONSTRAINT}")
    op.execute(
        f"CREATE UNIQUE INDEX {NEW_INDEX} "
        "ON reader_versions (document_id, kind, coalesce(task_id, ''))"
    )


def downgrade() -> None:
    op.execute(f"DROP INDEX IF EXISTS {NEW_INDEX}")
    op.execute(
        f"ALTER TABLE reader_versions ADD CONSTRAINT {OLD_CONSTRAINT} "
        "UNIQUE (document_id, kind)"
    )
