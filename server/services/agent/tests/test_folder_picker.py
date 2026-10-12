# Copyright 2026 SciLoop contributors
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
"""目录选择任务状态机（计划 §8.3：每条边、终态幂等、迟到结果不改写终态）。

宿主执行器全部用假对象替换 —— 这里只验证**服务端**的状态收敛与文案口径。
"""

from __future__ import annotations

import asyncio
from typing import Any

import pytest

from services.agent import folder_picker


class _FakeRunner:
    """假的宿主执行器客户端：记录调用，按预设回话。"""

    def __init__(self) -> None:
        self.creates: list[dict[str, Any]] = []
        self.statuses: list[str] = []
        self.cancels: list[str] = []
        self.create_reply: dict[str, Any] = {"ok": True, "status": "pending", "accepted": True}
        self.status_reply: dict[str, Any] = {"ok": True, "status": "pending"}

    async def pick_create(
        self, *, request_id: str, title: str = "", initial: str = "", timeout_s: int = 600
    ) -> dict[str, Any]:
        self.creates.append({"request_id": request_id, "title": title, "initial": initial})
        reply = dict(self.create_reply)
        reply.setdefault("request_id", request_id)
        return reply

    async def pick_status(self, *, request_id: str) -> dict[str, Any]:
        self.statuses.append(request_id)
        return dict(self.status_reply)

    async def pick_cancel(self, *, request_id: str) -> dict[str, Any]:
        self.cancels.append(request_id)
        return {"ok": True, "status": "canceled"}


def _install(monkeypatch: pytest.MonkeyPatch, runner: _FakeRunner, **kwargs: Any) -> folder_picker.FolderPickerService:
    monkeypatch.setattr(folder_picker.host_runner, "pick_create", runner.pick_create)
    monkeypatch.setattr(folder_picker.host_runner, "pick_status", runner.pick_status)
    monkeypatch.setattr(folder_picker.host_runner, "pick_cancel", runner.pick_cancel)
    counter = {"n": 0}

    def _next_id() -> str:
        counter["n"] += 1
        return f"pick_{counter['n']}"

    return folder_picker.FolderPickerService(create_id=_next_id, **kwargs)


def test_create_then_selected(monkeypatch: pytest.MonkeyPatch) -> None:
    runner = _FakeRunner()

    async def scenario() -> None:
        service = _install(monkeypatch, runner)
        created = await service.create(title="选择工作目录", initial="D:/x")
        assert created["status"] == "pending"
        assert created["request_id"] == "pick_1"
        assert created["accepted"] is True
        assert runner.creates[0]["title"] == "选择工作目录"

        runner.status_reply = {"ok": True, "status": "selected", "path": "D:/工作 目录"}
        final = await service.status("pick_1")
        assert final["status"] == "selected"
        assert final["path"] == "D:/工作 目录"
        assert final["message"] == ""

    asyncio.run(scenario())


def test_second_create_reuses_pending_task(monkeypatch: pytest.MonkeyPatch) -> None:
    runner = _FakeRunner()

    async def scenario() -> None:
        service = _install(monkeypatch, runner)
        first = await service.create()
        second = await service.create()
        assert second["request_id"] == first["request_id"]
        assert second["busy"] is True
        assert second["accepted"] is False
        # **只弹一个窗口**：执行器只被请求建了一次 ✓
        assert len(runner.creates) == 1

    asyncio.run(scenario())


@pytest.mark.parametrize(
    ("helper_status", "expected", "message"),
    [
        ("canceled", "canceled", "已取消选择"),
        ("timed_out", "expired", "选择窗口已关闭，请重新选择"),
        ("unavailable", "unavailable", "当前设备无法打开系统选择窗口"),
        ("failed", "failed", "选择窗口发生故障，请重试"),
    ],
)
def test_helper_status_mapping(
    monkeypatch: pytest.MonkeyPatch, helper_status: str, expected: str, message: str
) -> None:
    runner = _FakeRunner()

    async def scenario() -> None:
        service = _install(monkeypatch, runner)
        await service.create()
        runner.status_reply = {"ok": True, "status": helper_status}
        state = await service.status("pick_1")
        assert state["status"] == expected
        assert state["message"] == message
        assert state["canceled"] is (expected == "canceled")

    asyncio.run(scenario())


def test_selected_without_path_becomes_canceled(monkeypatch: pytest.MonkeyPatch) -> None:
    runner = _FakeRunner()

    async def scenario() -> None:
        service = _install(monkeypatch, runner)
        await service.create()
        runner.status_reply = {"ok": True, "status": "selected", "path": ""}
        state = await service.status("pick_1")
        assert state["status"] == "canceled"

    asyncio.run(scenario())


def test_executor_unavailable_on_create(monkeypatch: pytest.MonkeyPatch) -> None:
    runner = _FakeRunner()
    runner.create_reply = {
        "ok": False,
        "supported": False,
        "error": "容器执行环境没有屏幕",
    }

    async def scenario() -> None:
        service = _install(monkeypatch, runner)
        state = await service.create()
        assert state["status"] == "unavailable"
        assert state["message"] == "当前设备无法打开系统选择窗口"

    asyncio.run(scenario())


def test_unreachable_and_unknown_route_are_unavailable(monkeypatch: pytest.MonkeyPatch) -> None:
    async def scenario() -> None:
        for reply in ({"ok": False, "unreachable": True}, {"ok": False, "unknown_route": True}):
            runner = _FakeRunner()
            runner.create_reply = reply
            service = _install(monkeypatch, runner)
            state = await service.create()
            assert state["status"] == "unavailable"

    asyncio.run(scenario())


def test_outcome_unknown_is_failed_not_replayed(monkeypatch: pytest.MonkeyPatch) -> None:
    runner = _FakeRunner()
    runner.create_reply = {"ok": False, "outcome_unknown": True}

    async def scenario() -> None:
        service = _install(monkeypatch, runner)
        state = await service.create()
        assert state["status"] == "failed"
        assert state["message"] == "选择窗口发生故障，请重试"
        # 结果未知时**不自动重放**：执行器只被调了一次 ✓
        assert len(runner.creates) == 1

    asyncio.run(scenario())


def test_terminal_state_is_idempotent_and_late_results_ignored(monkeypatch: pytest.MonkeyPatch) -> None:
    runner = _FakeRunner()

    async def scenario() -> None:
        service = _install(monkeypatch, runner)
        await service.create()
        runner.status_reply = {"ok": True, "status": "selected", "path": "D:/a"}
        first = await service.status("pick_1")
        assert first["status"] == "selected"

        # 迟到的"取消"结果**不能**改写已定终态 ✓
        runner.status_reply = {"ok": True, "status": "canceled"}
        again = await service.status("pick_1")
        assert again["status"] == "selected"
        assert again["path"] == "D:/a"
        # 终态重复读不再问执行器（结果已本地保留）✓
        assert runner.statuses == ["pick_1"]

    asyncio.run(scenario())


def test_cancel_is_idempotent(monkeypatch: pytest.MonkeyPatch) -> None:
    runner = _FakeRunner()

    async def scenario() -> None:
        service = _install(monkeypatch, runner)
        await service.create()
        first = await service.cancel("pick_1")
        second = await service.cancel("pick_1")
        assert first["status"] == "canceled" and second["status"] == "canceled"
        assert second["canceled"] is True
        assert runner.cancels == ["pick_1"]

    asyncio.run(scenario())


def test_unknown_request_id_reads_as_expired(monkeypatch: pytest.MonkeyPatch) -> None:
    runner = _FakeRunner()

    async def scenario() -> None:
        service = _install(monkeypatch, runner)
        state = await service.status("pick_missing")
        assert state["status"] == "expired"
        assert state["message"] == "选择窗口已关闭，请重新选择"
        canceled = await service.cancel("pick_missing")
        assert canceled["status"] == "expired"

    asyncio.run(scenario())


def test_max_life_expires_and_cancels_helper(monkeypatch: pytest.MonkeyPatch) -> None:
    runner = _FakeRunner()
    clock = {"now": 1000.0}

    async def scenario() -> None:
        service = _install(monkeypatch, runner, clock=lambda: clock["now"], max_life_s=60.0)
        await service.create()
        clock["now"] += 120.0
        state = await service.status("pick_1")
        assert state["status"] == "expired"
        # 系统保护期限到达时，必须**先让执行器关窗**再收束（不留孤儿窗口）✓
        assert runner.cancels == ["pick_1"]

    asyncio.run(scenario())


def test_local_max_life_expires_and_cancels_dialog(monkeypatch: pytest.MonkeyPatch) -> None:
    clock = {"now": 1000.0}
    canceled = asyncio.Event()

    async def wait_for_cancel(
        _service: folder_picker.FolderPickerService,
        task: folder_picker.PickTask,
        *,
        title: str,
        initial: str,
    ) -> None:
        try:
            await asyncio.Future()
        except asyncio.CancelledError:
            canceled.set()
            raise

    monkeypatch.setattr(folder_picker.FolderPickerService, "_run_local_dialog", wait_for_cancel)

    async def scenario() -> None:
        service = folder_picker.FolderPickerService(
            local=True, clock=lambda: clock["now"], max_life_s=60.0
        )
        created = await service.create()
        await asyncio.sleep(0)
        clock["now"] += 120.0
        state = await service.status(created["request_id"])
        await asyncio.sleep(0)
        assert state["status"] == "expired"
        assert canceled.is_set()

    asyncio.run(scenario())


def test_expired_results_are_swept_after_ttl(monkeypatch: pytest.MonkeyPatch) -> None:
    runner = _FakeRunner()
    clock = {"now": 1000.0}

    async def scenario() -> None:
        service = _install(monkeypatch, runner, clock=lambda: clock["now"], ttl_s=10.0)
        await service.create()
        runner.status_reply = {"ok": True, "status": "canceled"}
        await service.status("pick_1")
        clock["now"] += 60.0
        state = await service.status("pick_1")
        assert state["status"] == "expired"

    asyncio.run(scenario())


def test_busy_helper_id_is_adopted(monkeypatch: pytest.MonkeyPatch) -> None:
    runner = _FakeRunner()
    runner.create_reply = {
        "ok": True,
        "request_id": "pick_other",
        "status": "pending",
        "accepted": False,
        "busy": True,
    }

    async def scenario() -> None:
        service = _install(monkeypatch, runner)
        await service.create()
        runner.status_reply = {"ok": True, "status": "selected", "path": "D:/b"}
        state = await service.status("pick_1")
        assert state["status"] == "selected"
        assert runner.statuses == ["pick_other"]

    asyncio.run(scenario())


def test_view_never_leaks_internals(monkeypatch: pytest.MonkeyPatch) -> None:
    runner = _FakeRunner()
    runner.create_reply = {"ok": True, "status": "pending", "error_class": "internal_detail"}

    async def scenario() -> None:
        service = _install(monkeypatch, runner)
        state = await service.create()
        # 用户可见字段里**不许**出现内部错误类别 / 毫秒数 / 端口 ✓ §6.3
        assert set(state) == {"request_id", "status", "path", "canceled", "message", "accepted", "busy", "created"}
        assert "internal_detail" not in state["message"]

    asyncio.run(scenario())
