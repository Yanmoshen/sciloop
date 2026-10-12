"""Regression checks for failures observed in the real knowledge/translation UI."""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from api.v1.translate import preview_translate, preview_translate_blocks
from services import knowledge_base as kb
from services.translate import artifacts, jobs


@pytest.fixture
def isolated_storage(tmp_path, monkeypatch):
    monkeypatch.setattr(kb, "knowledge_root", lambda: tmp_path)
    monkeypatch.setenv("TRANSLATE_ARTIFACT_DIR", str(tmp_path / "translations"))
    monkeypatch.setattr(jobs, "_TASKS", {})
    return tmp_path


def test_pipeline_metadata_hidden_but_user_same_name_kept(isolated_storage):
    kb._save([
        {"id": "kb-migrated-metadata", "name": "manifest.json", "stored_name": "manifest.json", "relative_path": "translations/old-task/manifest.json", "bucket": "paper", "folder": ["translations", "old-task"]},
        {"id": "kb-user-metadata", "name": "manifest.json", "stored_name": "manifest.json", "bucket": "paper", "folder": ["my-files"], "source_label": "本地上传"},
        {"id": "kb-migrated-known-probe", "name": "999999__wp01-probe#persist.txt", "stored_name": "probe.txt", "relative_path": "parses/fulltext-cache/999999__wp01-probe#persist.txt", "bucket": "paper", "folder": ["parses", "fulltext-cache"]},
    ])
    view = kb.snapshot(bucket="paper")
    assert [item["id"] for item in view["items"]] == ["kb-user-metadata"]
    assert kb.get("kb-migrated-metadata") is not None
    assert len(kb._entries()) == 3
    assert "kb-migrated-known-probe" not in {item["id"] for item in kb.snapshot()["items"]}


def test_legacy_duplicate_ids_are_stable_and_file_addressable(isolated_storage):
    first = isolated_storage / "uploads" / "first.pdf"
    second = isolated_storage / "uploads" / "second.pdf"
    first.parent.mkdir(parents=True)
    first.write_bytes(b"first")
    second.write_bytes(b"second")
    kb._save([
        {
            "id": "kb-migrated-same",
            "name": first.name,
            "stored_name": first.name,
            "relative_path": "uploads/first.pdf",
            "bucket": "literature",
            "folder": ["uploads"],
        },
        {
            "id": "kb-migrated-same",
            "name": second.name,
            "stored_name": second.name,
            "relative_path": "uploads/second.pdf",
            "bucket": "literature",
            "folder": ["uploads"],
        },
    ])

    before = kb._manifest_path().read_text(encoding="utf-8")
    items = kb.snapshot()["items"]
    ids = [item["id"] for item in items]
    assert len(ids) == 2
    assert len(set(ids)) == 2
    assert all(item["id"].startswith("kb-migrated-same-path-") for item in items)
    assert kb._manifest_path().read_text(encoding="utf-8") == before

    by_name = {item["name"]: item for item in items}
    assert kb.get(by_name["first.pdf"]["id"])["name"] == "first.pdf"
    assert kb.payload_path(by_name["second.pdf"]["id"]) == second


def test_generated_paper_source_and_experiment_separated(isolated_storage):
    base = isolated_storage / "projects/generated-papers/窗口求和__20261010"
    for relative, text in (("manuscript/paper.md", "Abstract\nMethods\nResults"), ("results/timings.csv", "method,seconds\nrolling,0.01"), ("sources/external/reference.pdf", "external source")):
        path = base / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")
    all_items = kb.snapshot()["items"]
    categories = {item["name"]: (item["bucket"], item["provenance"]) for item in all_items}
    assert categories == {"paper.md": ("paper", "generated"), "timings.csv": ("experiment", "generated"), "reference.pdf": ("literature", "external")}
    manuscript = next(item for item in all_items if item["name"] == "paper.md")
    assert kb.get(manuscript["id"])["content"].startswith("Abstract")
    assert kb.payload_path(manuscript["id"]) == base / "manuscript/paper.md"
    kb.update(manuscript["id"], {"name": manuscript["name"], "folder": manuscript["folder"], "bucket": manuscript["bucket"], "tags": ["complete"]})
    assert kb.payload_path(manuscript["id"]) == base / "manuscript/paper.md"
    assert kb.trash([manuscript["id"]]) == 1
    assert kb.get(manuscript["id"])["trashed_at"]
    assert (base / "manuscript/paper.md").is_file()
    assert kb.trash([manuscript["id"]], restore=True) == 1
    assert kb.get(manuscript["id"])["trashed_at"] is None


def test_generated_latex_manuscript_is_readable_in_knowledge_reader(isolated_storage):
    """LaTeX papers are code-rendered in the KB, so details must include their text."""
    base = isolated_storage / "projects/generated-papers/demo/manuscript"
    base.mkdir(parents=True)
    tex = base / "main.tex"
    bib = base / "refs.bib"
    tex.write_text(r"\\documentclass{article}\n\\begin{document}\nResult\n\\end{document}\n", encoding="utf-8")
    bib.write_text("@article{demo, title={Result}}\n", encoding="utf-8")

    items = kb.snapshot()["items"]
    by_name = {item["name"]: item for item in items}
    assert by_name["main.tex"]["has_content"] is True
    assert by_name["refs.bib"]["has_content"] is True
    assert kb.get(by_name["main.tex"]["id"])["content"].startswith(r"\\documentclass")
    assert kb.get(by_name["refs.bib"]["id"])["content"].startswith("@article")


def test_generated_project_manifest_explicitly_maps_numeric_project_filter(isolated_storage):
    """Only an explicit manifest mapping may expose generated files to a project filter."""
    base = isolated_storage / "projects/generated-papers/a-readable-project"
    (base / "manuscript").mkdir(parents=True)
    (base / "project_manifest.json").write_text(
        '{"project_id":"a-readable-project","db_project_id":42}', encoding="utf-8"
    )
    (base / "manuscript" / "main.tex").write_text("\\section{Result}\n", encoding="utf-8")

    mapped = kb.snapshot(project_id=42)["items"]
    assert {item["name"] for item in mapped} == {"project_manifest.json", "main.tex"}
    assert kb.snapshot(project_id=43)["items"] == []


def test_uploaded_paper_never_shares_generated_directory(isolated_storage):
    imported = kb.create(name="paper.pdf", bucket="paper", data=b"external PDF", folder=["example"])
    generated = kb.create(name="paper.md", bucket="paper", content="my work", source_label="手动新建", folder=["example"])
    assert kb.payload_path(imported["id"]).is_relative_to(isolated_storage / "literature/imported-papers")
    assert kb.payload_path(generated["id"]).is_relative_to(isolated_storage / "projects/generated-papers")
    assert imported["provenance"] == "external"
    assert generated["provenance"] == "generated"


def test_disk_sync_indexes_all_directories_and_is_idempotent(isolated_storage):
    project = isolated_storage / "projects" / "算法资料整理__2026101019"
    (project / "notes").mkdir(parents=True)
    (project / "notes" / "plan.md").write_text("plan", encoding="utf-8")
    (isolated_storage / "projects" / "research-preflight").mkdir(parents=True)
    metadata = isolated_storage / "metadata"
    metadata.mkdir(parents=True)
    (metadata / "entries.json").write_text("[]", encoding="utf-8")
    (metadata / "folders.json").write_text("[]", encoding="utf-8")

    first = kb.sync_disk_index()
    assert first["files_added"] == 1
    assert "projects" in kb.snapshot()["folders"]
    assert "projects/算法资料整理__2026101019" in kb.snapshot()["folders"]
    assert "projects/算法资料整理__2026101019/notes" in kb.snapshot()["folders"]
    assert "projects/research-preflight" in kb.snapshot()["folders"]
    items = kb.snapshot()["items"]
    assert [item["name"] for item in items] == ["plan.md"]
    assert items[0]["provenance"] == "disk"
    assert items[0]["source_label"] == "磁盘索引"
    assert "metadata" not in " ".join(kb.snapshot()["folders"])

    second = kb.sync_disk_index()
    assert second["files_added"] == 0
    assert len(kb._entries()) == 1


def test_translation_history_merges_disk_and_live(isolated_storage):
    for task_id, created in (("old-history", "2026-10-09T00:00:00Z"), ("live-task", "2026-10-08T00:00:00Z")):
        artifacts.write_manifest(task_id, {"task_id": task_id, "created_at": created, "status": "completed", "files": {}})
    live = jobs.TranslateTask(task_id="live-task", mode="translate", highlight_requested=False, filename="current.pdf", status="rewriting", created_at="2026-10-10T00:00:00Z")
    jobs._TASKS[live.task_id] = live
    history = jobs.list_tasks()
    assert [row["task_id"] for row in history] == ["live-task", "old-history"]
    assert history[0]["status"] == "rewriting"


def test_structured_preview_reads_real_manifest_and_preserves_html(isolated_storage):
    task_id = "finished-paper"
    artifacts.write_manifest(task_id, {"task_id": task_id, "status": "completed", "source_filename": "窗口求和.pdf", "files": {}, "blocks": [{"page": 1, "bbox": [0, 0, 20, 10], "source_text": "Sliding window", "target_text": "滑动窗口"}]})
    artifacts.artifact_path(task_id, artifacts.PREVIEW_NAME).write_text("<html>Sliding window / 滑动窗口</html>", encoding="utf-8")
    preview = asyncio.run(preview_translate_blocks(task_id))
    assert preview["total"] == 1
    assert preview["blocks"] == [{"page": 1, "source_text": "Sliding window", "target_text": "滑动窗口"}]
    html = asyncio.run(preview_translate(task_id))
    assert html.media_type.startswith("text/html")
    assert "滑动窗口" in html.body.decode("utf-8")
    assert jobs.get_task(task_id)["source"]["filename"] == "窗口求和.pdf"


def test_running_translation_does_not_return_empty_completed_preview(isolated_storage):
    task = jobs.TranslateTask(task_id="working-task", mode="translate", highlight_requested=False, filename="input.pdf")
    jobs._TASKS[task.task_id] = task
    with pytest.raises(Exception) as error:
        asyncio.run(preview_translate_blocks(task.task_id))
    assert error.value.status_code == 409
