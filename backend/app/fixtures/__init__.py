# Copyright 2026 SciLoop contributors
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
"""演示 fixture 与离线演练包（WP16）。

本包按 ``contracts.repo_layout`` 的约定承载三类演示资产：

====================== =========================================================
文件/模块               用途
====================== =========================================================
``verify_public_demo_writes.sh``  WP16-A1 验收：public_demo 面匿名写操作必须被拒
``export_offline.py``             离线演示包导出（论文库快照 + LLM fixture + 说明）
（运行时数据）                    ``paper_feed_snapshots`` / ``demo_fixtures`` 两表
====================== =========================================================

注意：**论文库快照与 LLM fixture 都落在数据库表里**（由 SQL 迁移建表），
本目录只放脚本与导出产物，不放大体积二进制。真正的导出结果写到
``deliverables/offline-demo/``（该目录归 WP17，见本包导出脚本的参数说明）。
"""

from __future__ import annotations

__all__: list[str] = []
