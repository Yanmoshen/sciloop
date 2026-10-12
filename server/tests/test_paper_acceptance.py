from __future__ import annotations

import importlib.util
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
SCRIPT = ROOT / "tools" / "paper_acceptance.py"


def load_runner():
    spec = importlib.util.spec_from_file_location("paper_acceptance", SCRIPT)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_latest_manuscript_is_under_projects():
    runner = load_runner()
    paper = runner.discover_paper_dir(None)
    assert (paper / "main.tex").is_file()
    assert runner.PROJECTS.resolve() in paper.parents


def test_isolation_keeps_external_roots_outside_generated_project():
    runner = load_runner()
    paper = runner.discover_paper_dir(None)
    report = runner.isolation_report(paper)
    assert report["generated_root_is_project"] is True
    assert report["separated"] is True

