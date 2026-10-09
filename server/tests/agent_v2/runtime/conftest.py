"""agent.v2 测试公共夹具。

sys.path 处理：本项目的导入根是 ``server/``（新代码一律 ``from services.x import y``），
而 ``server/`` 下没有 pytest 配置（根目录的 pytest.ini / tests/ 属本地未跟踪文件，
不随仓库分发）。因此这里显式把 ``server/`` 注入 sys.path，
并自带 rootdir 边界，保证在干净 clone 里 ``pytest server/tests/agent_v2`` 直接可跑。
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

SERVER_ROOT = Path(__file__).resolve().parents[3]
if str(SERVER_ROOT) not in sys.path:
    sys.path.insert(0, str(SERVER_ROOT))

from contracts.agent_v2.clock import FakeClock  # noqa: E402


@pytest.fixture()
def clock() -> FakeClock:
    """可控时钟：``sleep`` 不真实等待，只推进虚拟时间。"""
    return FakeClock()


@pytest.fixture()
def conv_root(tmp_path: Path) -> Path:
    """对话持久化根：knowledge-base/conversations。"""
    root = tmp_path / "conversations"
    root.mkdir(parents=True, exist_ok=True)
    return root


@pytest.fixture()
def memory_root(tmp_path: Path) -> Path:
    """记忆持久化根：knowledge-base/memories。"""
    root = tmp_path / "memories"
    root.mkdir(parents=True, exist_ok=True)
    return root
