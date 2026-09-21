# Copyright 2026 SciLoop contributors
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
"""流水线引擎包（WP09；WP10 的决策/护栏模块亦坐落于此）。

本文件仅作**包标记**，不做任何 import：避免 ``services.pipeline.*`` 的
子模块互相导入时产生环。子模块按需自行导入。

- 状态机：:mod:`services.pipeline.state_machine`
- 环节协议与注册表：:mod:`services.pipeline.stages`
- 调度引擎：:mod:`services.pipeline.engine`
- 断点续跑：:mod:`services.pipeline.resume`
- 停止条件：:mod:`services.pipeline.stop_conditions`
- 失败分级：:mod:`services.pipeline.failure_handler`
- SSE 事件总线：:mod:`services.pipeline.sse`
"""

from __future__ import annotations

__all__: list[str] = []
