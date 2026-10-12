"""Scientific interpreter selection and host boundary regression checks."""

import importlib.util
import os
from pathlib import Path
from types import SimpleNamespace

import pytest


@pytest.fixture
def runner():
    path = Path(__file__).resolve().parents[4] / "tools" / "host-runner" / "host_runner.py"
    spec = importlib.util.spec_from_file_location("host_runner_runtime_test", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_selected_python_used_by_bare_argv_not_explicit_path(runner, monkeypatch, tmp_path):
    chosen = tmp_path / "scientific-python.exe"
    chosen.touch()
    monkeypatch.setenv(runner.HOST_PYTHON_ENV, str(chosen))
    seen = []
    monkeypatch.setattr(runner, "_load_sandbox_module", lambda: SimpleNamespace(
        run=lambda **kw: seen.append(kw) or {"ok": True},
    ))
    runner.run_command(argv=["python", "-c", "print(1)"], cwd=str(tmp_path))
    runner.run_command(argv=["C:/explicit/python.exe", "-c", "print(1)"], cwd=str(tmp_path))
    assert seen[0]["argv"][0] == str(chosen.resolve())
    assert seen[1]["argv"][0] == "C:/explicit/python.exe"
    assert seen[0]["access_mode"] == "workspace"


def test_project_environment_is_discovered_without_changing_config(runner, monkeypatch, tmp_path):
    monkeypatch.delenv(runner.HOST_PYTHON_ENV, raising=False)
    monkeypatch.setattr(runner, "read_env_value", lambda _: "")
    monkeypatch.setattr(runner, "REPO_ROOT", tmp_path)
    candidate = tmp_path / ".venv" / "Scripts" / "python.exe"
    candidate.parent.mkdir(parents=True)
    candidate.touch()
    assert runner._python_selection() == (str(candidate.resolve()), "project")


def test_health_python_fact_is_selected_environment(runner, monkeypatch):
    monkeypatch.setattr(runner, "_python_selection", lambda: ("selected-python", "configured"))
    monkeypatch.setattr(runner, "_python_version", lambda _: "3.9.25")
    assert runner.python_environment() == {
        "executable": "selected-python", "source": "configured", "version": "3.9.25",
    }


@pytest.mark.skipif(os.name != "nt", reason="Windows process boundary")
def test_workspace_process_cannot_modify_outside_file(runner, tmp_path):
    workspace = tmp_path / "inside"
    workspace.mkdir()
    outside = tmp_path / "outside.txt"
    outside.write_text("keep")
    result = runner.run_command(
        argv=["python", "-c", f"from pathlib import Path; Path({str(outside)!r}).write_text('escaped')"],
        cwd=str(workspace), workspace_root=str(workspace), timeout_s=5,
    )
    assert result["ok"] is False
    assert outside.read_text() == "keep"


def test_approved_call_executes_without_workspace_isolation(runner, monkeypatch, tmp_path):
    import sys

    monkeypatch.setattr(runner, "_load_sandbox_module", lambda: pytest.fail("approved call entered sandbox"))
    workspace = tmp_path / "inside"
    workspace.mkdir()
    outside = tmp_path / "approved.txt"
    result = runner.run_command(
        argv=[sys.executable, "-c", f"from pathlib import Path; Path({str(outside)!r}).write_text('approved')"],
        cwd=str(workspace), workspace_root=str(workspace), access_mode="approved", timeout_s=10,
    )
    assert result["ok"], result
    assert outside.read_text() == "approved"


def test_fs_rejects_invalid_access_mode(runner, tmp_path):
    result = runner.fs_action(action="write", path=str(tmp_path / "no.txt"), to=None,
                              content="x", encoding=None, recursive=False, access_mode="bad")
    assert not result["ok"]
    assert not (tmp_path / "no.txt").exists()
