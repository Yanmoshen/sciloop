"""Project artifact projection stays separate from research-node completion."""

from __future__ import annotations

from pathlib import Path

from services.research import orchestrator


def _project(tmp_path: Path) -> orchestrator._ProjectView:
    return orchestrator._ProjectView(
        id=7,
        name="paper",
        settings={"workspace_dir": str(tmp_path)},
    )


def test_artifact_scan_does_not_promote_missing_node_run(tmp_path: Path) -> None:
    manuscript = tmp_path / "manuscript" / "main.tex"
    manuscript.parent.mkdir()
    manuscript.write_text("\\section{Results}\n", encoding="utf-8")

    state = orchestrator._artifact_status(_project(tmp_path))

    assert state["state"] == "available"
    assert state["alignment"] == "artifact_detected_without_node_run"
    assert state["node_recorded"] is False
    assert state["paper_writing_status"] == "pending"
    assert "manuscript/main.tex" in state["manuscript_files"]
    assert "不会被推断为节点完成" in state["note"]


def test_artifact_scan_reports_alignment_after_completed_node(tmp_path: Path) -> None:
    (tmp_path / "main.tex").write_text("paper", encoding="utf-8")

    state = orchestrator._artifact_status(
        _project(tmp_path),
        paper_writing_run={"status": "done", "entry_index": 1},
    )

    assert state["alignment"] == "node_and_artifact_present"
    assert state["node_recorded"] is True
    assert state["paper_writing_status"] == "done"


def test_artifact_scan_reports_completed_node_without_file(tmp_path: Path) -> None:
    state = orchestrator._artifact_status(
        _project(tmp_path),
        paper_writing_run={"status": "done"},
    )

    assert state["state"] == "empty"
    assert state["alignment"] == "node_completed_artifact_not_found"


def test_artifact_scan_handles_missing_workspace(tmp_path: Path) -> None:
    project = orchestrator._ProjectView(
        id=7,
        name="paper",
        settings={"workspace_dir": str(tmp_path / "gone")},
    )

    state = orchestrator._artifact_status(project)

    assert state["state"] == "missing"
    assert state["alignment"] == "no_artifact_scan"
    assert state["file_count"] == 0

