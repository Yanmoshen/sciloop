# Copyright 2026 SciLoop contributors
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
"""容器内执行环境（compose 的 `executor` 服务用它）。

`python -m exec_service` 就能起（见 :mod:`exec_service.main`）。
"""

from exec_service.main import app, main

__all__ = ["app", "main"]
