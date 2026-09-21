# Copyright 2026 SciLoop contributors
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
"""盲评隔离与校准域（WP12，创新点 3）。

对外契约（供 ``plan_review`` 环节与 ``api/v1/calibration.py`` 使用）::

    from services.review import blind, calibration

    generator, reviewer = await blind.resolve_isolated_pair(project_id)
    pack = blind.anonymize_and_shuffle(methods, forbidden_tokens=[...])
    report = await calibration.compute(calibration_id)

子模块（按需导入，**不在包级别 eager 导入**，避免与 pipeline 环节注册互相牵连）:

- :mod:`services.review.blind` —— 隔离校验 / 匿名化 / 固定种子乱序
- :mod:`services.review.calibration` —— 人工标签、Cohen kappa / MAE、样本量披露

红线见 ``contracts.blind_review_rules`` 与计划书附录 D.3。
"""

from __future__ import annotations

__all__ = ["blind", "calibration"]
