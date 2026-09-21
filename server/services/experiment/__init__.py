# Copyright 2026 SciLoop contributors
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.
"""实验域服务（WP11 独占）。

子模块：

- :mod:`services.experiment.registry` —— 模板注册表（白名单 / 参数 schema / 样本装配）
- :mod:`services.experiment.metrics` —— 指标采集与落库（只做解析，不生成数据）
- :mod:`services.experiment.passport` —— Experiment Passport（不可变凭证）
- :mod:`services.experiment.diff` —— replay / rerun 与父 Passport 的差异报告
- :mod:`services.experiment.templates` —— T1 / T3 模板实现与本地桩兜底

本包**不做任何子模块的 eager import**：执行器在运行期按模板 ``module`` 惰性导入，
避免 ``executor → services.experiment → executor`` 的循环依赖。
"""

from __future__ import annotations

__all__: list[str] = []
