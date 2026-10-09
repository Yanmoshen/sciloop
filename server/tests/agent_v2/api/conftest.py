"""agent.v2 API 层测试的公共夹具。

约定与 ``tests/agent_v2/runtime/conftest.py`` 一致：显式把 ``server/`` 注入 ``sys.path``，
保证在干净 clone 里 ``pytest server/tests/agent_v2`` 可直接运行。

额外提供一个**同步测试客户端** :class:`~tests.agent_v2.api.helpers.Harness`：
它持有一个常驻事件循环，从而能在多次 ``dispatch`` 之间让后台 Turn 任务继续推进——
这是测试「启动 Turn -> 事件流式推送 -> 中断/审批/完成」这条链路的前提。
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

SERVER_ROOT = Path(__file__).resolve().parents[3]
if str(SERVER_ROOT) not in sys.path:
    sys.path.insert(0, str(SERVER_ROOT))

_API_DIR = Path(__file__).resolve().parent
if str(_API_DIR) not in sys.path:
    sys.path.insert(0, str(_API_DIR))

from helpers import Harness  # noqa: E402

from api.v2.agent.service import AgentV2Service  # noqa: E402


@pytest.fixture()
def conv_root(tmp_path: Path) -> Path:
    root = tmp_path / "conversations"
    root.mkdir(parents=True, exist_ok=True)
    return root


@pytest.fixture()
def memory_root(tmp_path: Path) -> Path:
    root = tmp_path / "memories"
    root.mkdir(parents=True, exist_ok=True)
    return root


@pytest.fixture()
def service(conv_root: Path, memory_root: Path) -> AgentV2Service:
    """可控的服务实例：轮询与心跳都调到很短，方便测试。"""
    return AgentV2Service(
        conversations_root=conv_root,
        memories_root=memory_root,
        poll_interval_s=0.01,
        heartbeat_s=0.05,
        token_budget=200,
    )


@pytest.fixture()
def harness(service: AgentV2Service) -> Harness:
    client = Harness(service)
    yield client
    client.close()


@pytest.fixture()
def api(service: AgentV2Service) -> Harness:
    """``harness`` 的别名（语义化：代表一次「客户端会话」）。"""
    client = Harness(service)
    yield client
    client.close()


@pytest.fixture()
def service_factory(conv_root: Path, memory_root: Path):
    """按需构造服务的工厂（测试换场景 / 换关闭状态）。"""
    created: list[AgentV2Service] = []

    def _make(**kwargs) -> AgentV2Service:
        params = {
            "conversations_root": conv_root,
            "memories_root": memory_root,
            "poll_interval_s": 0.01,
            "heartbeat_s": 0.05,
            "token_budget": 200,
        }
        params.update(kwargs)
        svc = AgentV2Service(**params)
        created.append(svc)
        return svc

    yield _make
    for svc in created:
        svc.reopen()
