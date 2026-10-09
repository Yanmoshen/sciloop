"""agent_v2 工具线测试公共夹具（Agent 2 / WP-04~06）。

与 ``tests/agent_v2/runtime/conftest.py`` 同样的 sys.path 处理：本项目的导入根是
``server/``，因此这里显式注入，保证干净 clone 里 ``pytest server/tests/agent_v2`` 直接可跑。

**不依赖 Agent 3**：所有后端（搜索 / 知识库 / 技能 / MCP / 子 Agent 驱动）都用本目录内的
fake 实现注入，全部测试离线。
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

SERVER_ROOT = Path(__file__).resolve().parents[3]
if str(SERVER_ROOT) not in sys.path:
    sys.path.insert(0, str(SERVER_ROOT))

from contracts.agent_v2.clock import FakeClock, SystemClock  # noqa: E402
from services.approval_v2 import ApprovalManager  # noqa: E402
from services.host_execution_v2 import HostExecutionManager  # noqa: E402
from services.sandbox_v2 import SandboxManager, SandboxPolicy  # noqa: E402
from services.tool_registry_v2.builtin import build_default_registry  # noqa: E402


@pytest.fixture()
def clock() -> SystemClock:
    """真实时钟（宿主执行需要真实时间；虚拟时钟无法推进进程）。"""
    return SystemClock()


@pytest.fixture()
def fake_clock() -> FakeClock:
    """虚拟时钟（审批过期这类纯逻辑用）。"""
    return FakeClock()


@pytest.fixture()
def workspace(tmp_path: Path) -> Path:
    """工作区根（沙箱的 workspace-write 边界）。"""
    root = tmp_path / "workspace"
    root.mkdir(parents=True, exist_ok=True)
    return root


@pytest.fixture()
def sandbox(workspace: Path) -> SandboxManager:
    return SandboxManager(workspace, policy=SandboxPolicy.WORKSPACE_WRITE)


@pytest.fixture()
def full_access_sandbox(workspace: Path) -> SandboxManager:
    return SandboxManager(workspace, policy=SandboxPolicy.DANGER_FULL_ACCESS)


@pytest.fixture()
def approvals() -> ApprovalManager:
    return ApprovalManager()


@pytest.fixture()
def host(tmp_path: Path, sandbox: SandboxManager, clock: SystemClock) -> HostExecutionManager:
    return HostExecutionManager(
        tmp_path / "exec-records", sandbox=sandbox, clock=clock, default_timeout_s=30.0
    )


@pytest.fixture()
def registry(
    workspace: Path,
    sandbox: SandboxManager,
    host: HostExecutionManager,
    approvals: ApprovalManager,
):
    """默认注册表：全部内置工具 + 离线 fake 后端。"""
    from tool_helpers import fake_backends

    return build_default_registry(
        sandbox=sandbox,
        host=host,
        approvals=approvals,
        cwd=workspace,
        services=fake_backends(),
    )
