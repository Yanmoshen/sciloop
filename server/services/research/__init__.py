# Copyright 2026 SciLoop contributors
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
"""研究节点编排层（迁移 ``0008_research_nodes``）。

七个研究节点的有条件流程图：**程序控制流程，模型完成研究任务**。

模块分工
--------
``contracts``     三节点的输出契约（Pydantic + 手写 JSON Schema）
``rules``         R1–R14 校验器（结构 / 硬规则；不判学术质量）
``graph``         节点定义、允许的迁移边、三类闸门、状态映射
``prompts``       提示词渲染（契约 + 运行态上下文 + 可选长文档）
``preflight``     小规模预检执行器（真实记录，模型无法伪造）
``store``         状态表读写 + 论文库只读检索
``orchestrator``  执行闭环：取上下文 → 调模型 → 校验 → 迁移 / 驳回重跑

与既有六阶段流水线的关系
------------------------
**完全并行，互不影响**：六阶段引擎（``app/services/pipeline/``）一行未改，
两套流程用不同的表（``stage_outputs`` vs ``research_node_runs``）与不同的
路由前缀（``/pipelines/*`` vs ``/research/*``）。
"""

from __future__ import annotations

__all__ = ["contracts", "graph", "orchestrator", "preflight", "prompts", "rules", "store"]
