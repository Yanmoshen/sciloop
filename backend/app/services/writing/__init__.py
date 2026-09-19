# Copyright 2026 SciLoop contributors
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
"""写作域（WP14，附录 D.5）。

对外契约（供 ``pipeline/stages/writing.py`` 与 ``api/v1/drafts.py`` 使用）::

    from app.services.writing import drafter, integrity_checker

    pool = await drafter.build_evidence_pool(session, project_id)
    doc = drafter.DraftDocument(title=..., outline=..., sections=...)
    result = await integrity_checker.verify_and_persist(
        session, draft_id=draft_id, content_md=doc.render_markdown(), pool=pool
    )

子模块（按需导入，**不在包级别 eager 导入**，避免与环节注册互相牵连）:

- :mod:`app.services.writing.drafter` —— 证据池 / 大纲 / 分节撰写 / 引用编号与合规词表
- :mod:`app.services.writing.integrity_checker` —— Claim 拆分（优先调用 WP13）与三态校验落库

红线（contracts.evidence_rules 与 WP14.hard_constraints）：

- 所有结论性句子必须挂载证据；无证据的段落标 ``insufficient`` 且不得隐藏
- 引用编号由服务端从证据池分配，模型自造的 ref 一律丢弃并留痕（禁止编造引用编号）
- 禁止生成任何指向外部出版渠道的表述；草稿顶部与导出文件必须带 AI 辅助声明
"""

from __future__ import annotations

__all__ = ["drafter", "integrity_checker"]
