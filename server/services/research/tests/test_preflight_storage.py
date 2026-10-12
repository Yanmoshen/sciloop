"""研究预检产物必须进入知识库，而不是瞬时缓存目录。"""

from __future__ import annotations

import asyncio
from pathlib import Path

from services import knowledge_base
from services.research import preflight


def test_default_preflight_dir_is_inside_knowledge_base(monkeypatch, tmp_path: Path) -> None:
    monkeypatch.delenv("RESEARCH_PREFLIGHT_DIR", raising=False)
    monkeypatch.setattr(preflight, "knowledge_root", lambda: tmp_path)

    path = preflight.preflight_dir("chat-42", project_id=7)

    assert path == tmp_path / "projects" / "research-preflight" / "7"
    assert path.is_dir()


def test_preflight_log_and_record_stay_under_knowledge_base(monkeypatch, tmp_path: Path) -> None:
    monkeypatch.delenv("RESEARCH_PREFLIGHT_DIR", raising=False)
    monkeypatch.setattr(preflight, "knowledge_root", lambda: tmp_path)
    monkeypatch.setattr(knowledge_base, "knowledge_root", lambda: tmp_path)

    result = asyncio.run(
        preflight.run_preflight(
            conversation_id="chat-42",
            project_id=7,
            command="python -c \"print('paper step')\"",
            timeout_s=10,
        )
    )

    output_root = tmp_path / "projects" / "research-preflight" / "7"
    assert result.exit_code == 0
    assert Path(result.log_path).is_relative_to(output_root)
    assert Path(result.artifact_path or "").is_relative_to(output_root)
    assert len(list(output_root.glob("preflight-*.json"))) == 1
    assert len(list(output_root.glob("preflight-*.log"))) == 1
    listed = knowledge_base.snapshot()["items"]
    assert any(item["name"].startswith("preflight-") for item in listed)


def test_explicit_preflight_override_remains_available(monkeypatch, tmp_path: Path) -> None:
    override = tmp_path / "isolated"
    monkeypatch.setenv("RESEARCH_PREFLIGHT_DIR", str(override))
    monkeypatch.setattr(preflight, "knowledge_root", lambda: tmp_path / "knowledge")

    path = preflight.preflight_dir("chat-42")

    assert path == override / "conv-chat-42"
    assert path.is_dir()


def test_conversation_id_cannot_escape_preflight_root(monkeypatch, tmp_path: Path) -> None:
    monkeypatch.delenv("RESEARCH_PREFLIGHT_DIR", raising=False)
    monkeypatch.setattr(preflight, "knowledge_root", lambda: tmp_path)

    path = preflight.preflight_dir("../../outside\\nested")

    root = tmp_path / "projects" / "research-preflight"
    assert path.is_relative_to(root)
    assert path.parent == root
