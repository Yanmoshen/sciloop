# Copyright 2026 SciLoop contributors
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
"""演示工程（WP16）：三层演示保险 + 两级访问面。

模块划分
--------
``state``     演示运行时状态（access_mode / snapshot / replay 三开关，环境变量为默认值）
``access``    访问面策略：public_demo 只读写操作拒绝、Owner 校验、回放限额、路由审计
``snapshot``  论文库快照（``GET /papers/feed?snapshot=demo`` 的数据来源）
``replay``    LLM 响应录制与回放（``demo_fixtures``，``fixture_key = prompt_hash``）

红线
----
- ``OWNER_TOKEN`` / API Key 只从环境变量读取，**禁止**入库、禁止进前端产物、接口返回必须脱敏
- ``public_demo`` 面**不得**暴露任何写操作（``POST /passports/{id}/replay`` 是唯一例外且限额）
- 回放/快照结果必须如实标注 ``is_replay`` / ``data_source``，禁止冒充实时实验
"""

from __future__ import annotations

__all__ = [
    "access",
    "replay",
    "snapshot",
    "state",
]
