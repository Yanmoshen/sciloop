# Copyright 2026 SciLoop contributors
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
"""宿主执行器的**目录选择任务协议**（计划 §7.3 / §8.3）。

这里直接加载 `tools/host-runner/host_runner.py`，把弹框实现换成假的，
只验证协议本身：受理即返回、单飞、取消、收敛、崩溃不静默。
"""

from __future__ import annotations

import importlib.util
import os
import threading
import time
from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest

_REPO_ROOT = Path(__file__).resolve().parents[4]
_HELPER_PATH = _REPO_ROOT / "tools" / "host-runner" / "host_runner.py"


def _load_helper() -> Any:
    spec = importlib.util.spec_from_file_location("sciloop_host_runner_under_test", _HELPER_PATH)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


helper = _load_helper()


@pytest.fixture()
def manager(monkeypatch: pytest.MonkeyPatch) -> Any:
    monkeypatch.setattr(helper, "_picker_impl", lambda: "native")
    monkeypatch.setattr(helper, "_load_native_picker", lambda: object())
    return helper._PickManager()


def _patch_pick(monkeypatch: pytest.MonkeyPatch, fake: Callable[[Any], dict[str, Any]]) -> None:
    monkeypatch.setattr(helper, "_execute_pick", fake)


def _wait(manager: Any, request_id: str, timeout: float = 5.0) -> dict[str, Any]:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        state = manager.status(request_id)
        if state["done"]:
            return state
        time.sleep(0.02)
    raise AssertionError(f"任务没有收束：{manager.status(request_id)}")


def test_create_returns_accepted_immediately(manager: Any, monkeypatch: pytest.MonkeyPatch) -> None:
    gate = threading.Event()

    def fake(task: Any) -> dict[str, Any]:
        gate.wait(5)
        return {"ok": True, "status": "selected", "path": os.getcwd()}

    _patch_pick(monkeypatch, fake)
    started = time.monotonic()
    reply = manager.create(request_id="pick_a", title="选择工作目录", initial="", timeout_s=60)
    elapsed = time.monotonic() - started

    assert reply["accepted"] is True
    assert reply["status"] == "pending"
    # 受理**立刻**返回：绝不阻塞在研究员挑选上 ✓
    assert elapsed < 0.5
    assert manager.status("pick_a")["status"] == "pending"

    gate.set()
    assert _wait(manager, "pick_a")["status"] == "selected"


def test_second_create_is_single_flight(manager: Any, monkeypatch: pytest.MonkeyPatch) -> None:
    gate = threading.Event()

    def fake(task: Any) -> dict[str, Any]:
        gate.wait(5)
        return {"ok": True, "status": "canceled"}

    _patch_pick(monkeypatch, fake)
    first = manager.create(request_id="pick_a", title="t", initial="", timeout_s=60)
    second = manager.create(request_id="pick_b", title="t", initial="", timeout_s=60)

    assert second["accepted"] is False
    assert second["busy"] is True
    assert second["request_id"] == first["request_id"]
    gate.set()
    _wait(manager, "pick_a")


def test_cancel_closes_pending_task_and_is_idempotent(manager: Any, monkeypatch: pytest.MonkeyPatch) -> None:
    def fake(task: Any) -> dict[str, Any]:
        # 真实实现里，这个事件会让对话框收到 WM_CLOSE
        task.cancel_event.wait(5)
        return {"ok": True, "status": "canceled"}

    _patch_pick(monkeypatch, fake)
    manager.create(request_id="pick_c", title="t", initial="", timeout_s=60)

    first = manager.cancel("pick_c")
    second = manager.cancel("pick_c")
    assert first["status"] == "canceled"
    assert second["status"] == "canceled"
    assert manager.status("pick_c")["status"] == "canceled"


@pytest.mark.parametrize("status", ["canceled", "timed_out", "unavailable", "failed", "selected"])
def test_terminal_statuses_are_passed_through(
    manager: Any, monkeypatch: pytest.MonkeyPatch, status: str
) -> None:
    payload: dict[str, Any] = {"ok": status == "selected", "status": status}
    if status == "selected":
        payload["path"] = os.getcwd()
    _patch_pick(monkeypatch, lambda task: payload)

    manager.create(request_id="pick_s", title="t", initial="", timeout_s=60)
    state = _wait(manager, "pick_s")
    assert state["status"] == status
    assert state["done"] is True


def test_selected_path_must_exist(manager: Any, monkeypatch: pytest.MonkeyPatch) -> None:
    missing = str(Path(os.getcwd()) / "definitely-not-here-9f3a")
    _patch_pick(monkeypatch, lambda task: {"ok": True, "status": "selected", "path": missing})

    manager.create(request_id="pick_m", title="t", initial="", timeout_s=60)
    state = _wait(manager, "pick_m")
    # 选完就被删 / 不是目录 → 不能返回伪成功 ✓
    assert state["status"] == "failed"
    assert state["error_class"] == "path_missing"


def test_picker_crash_is_failed_not_canceled(manager: Any, monkeypatch: pytest.MonkeyPatch) -> None:
    def boom(task: Any) -> dict[str, Any]:
        raise RuntimeError("boom")

    _patch_pick(monkeypatch, boom)
    manager.create(request_id="pick_x", title="t", initial="", timeout_s=60)
    state = _wait(manager, "pick_x")
    assert state["status"] == "failed"
    assert state["error_class"] == "picker_crashed"


def test_unknown_request_id_reads_as_expired(manager: Any) -> None:
    assert manager.status("pick_missing")["status"] == "expired"
    assert manager.cancel("pick_missing")["status"] == "expired"


def test_terminal_state_not_overwritten(manager: Any, monkeypatch: pytest.MonkeyPatch) -> None:
    _patch_pick(monkeypatch, lambda task: {"ok": True, "status": "canceled"})
    manager.create(request_id="pick_t", title="t", initial="", timeout_s=60)
    _wait(manager, "pick_t")

    # 掌上已经有终态：第二次取消**不能**改状态，也不该报错 ✓
    assert manager.cancel("pick_t")["status"] == "canceled"
    assert manager.status("pick_t")["status"] == "canceled"


def test_create_clamps_timeout_to_protection_limit(
    manager: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(helper, "_PICK", manager)
    _patch_pick(monkeypatch, lambda task: {"ok": True, "status": "canceled"})

    reply = helper.pick_create(request_id="pick_limit", title="t", timeout_s=99999)
    assert reply["accepted"] is True
    assert manager._tasks["pick_limit"].timeout_s == int(helper._PICK_MAX_LIFE_S)
    _wait(manager, "pick_limit")


def test_no_powershell_fallback_on_windows_native(monkeypatch: pytest.MonkeyPatch) -> None:
    """原生可用时绝不回落 PowerShell（计划 §7.3.1 / §10）✓。"""

    calls: list[str] = []
    monkeypatch.setattr(helper, "_load_native_picker", lambda: callable_picker(calls))
    monkeypatch.setattr(
        helper, "_pick_windows_fallback", lambda task, reason="": calls.append("fallback") or {}
    )

    task = helper._PickTask(request_id="pick_n", title="t", initial="", timeout_s=60, impl="native")
    outcome = helper._pick_windows_native(task)
    assert outcome["status"] == "selected"
    assert calls == ["native"]


def callable_picker(calls: list[str]) -> Any:
    class _Picker:
        @staticmethod
        def pick_folder_native(*, title: str, timeout_s: int, cancel_event: Any = None) -> dict[str, Any]:
            calls.append("native")
            return {"ok": True, "status": "selected", "path": os.getcwd()}

    return _Picker
