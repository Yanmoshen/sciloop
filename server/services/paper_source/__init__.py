# Copyright 2026 SciLoop contributors
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
"""paper_source 子包：论文取数、身份映射与留痕（WP03）。

模块归属（index.json ownership_notes）
-------------------------------------
- WP03：``arxiv_client`` / ``s2_client`` / ``openalex_client`` / ``github_client`` /
  ``identity`` / ``source_records`` / ``cache``
- WP04：``ranking`` / ``influence_score`` / ``venue``（本子包内但归 WP04，勿改）

这里只放子包文档，**不导入任何子模块**，避免 import 期产生循环依赖。
"""

__all__ = [
    "arxiv_client",
    "cache",
    "github_client",
    "identity",
    "openalex_client",
    "s2_client",
    "source_records",
]
