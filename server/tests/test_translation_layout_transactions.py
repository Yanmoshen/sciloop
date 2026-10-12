"""Real PDF regressions for non-destructive translation layout."""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

pymupdf = pytest.importorskip("pymupdf")
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from services.translate import engine  # noqa: E402


def _source(pages: int = 1) -> bytes:
    document = pymupdf.open()
    try:
        for number in range(pages):
            page = document.new_page(width=595, height=842)
            page.insert_text(
                (72, 120), f"Original scientific text on page {number + 1}",
                fontname="cour", fontsize=12, color=(0.7, 0.2, 0.1),
            )
        return document.tobytes()
    finally:
        document.close()


def _blocks(source: bytes) -> list[engine.TextBlock]:
    document = engine.load_document(source)
    try:
        return engine.extract_blocks(document, max_pages=10)[0]
    finally:
        document.close()


def _page_pixels(source: bytes, index: int = 0) -> bytes:
    document = engine.load_document(source)
    try:
        return document[index].get_pixmap().samples
    finally:
        document.close()


def test_failed_translation_preserves_original_glyphs_and_colour() -> None:
    source = _source()
    output, outcomes = engine.render_mono(source, _blocks(source), ["译" * 10000], warnings=[])
    assert not outcomes[0].written
    assert _page_pixels(output) == _page_pixels(source)


def test_post_redaction_write_failure_rolls_back_entire_page(monkeypatch) -> None:
    source = _source()
    insert = engine._insert_block

    def fail_commit(*args, **kwargs):
        if kwargs.get("commit", True):
            return False, 12.0, "insert_textbox_failed: RuntimeError"
        return insert(*args, **kwargs)

    monkeypatch.setattr(engine, "_insert_block", fail_commit)
    warnings: list[str] = []
    output, outcomes = engine.render_mono(source, _blocks(source), ["科学研究"], warnings=warnings)
    assert not outcomes[0].written
    assert outcomes[0].reason == "insert_textbox_failed: RuntimeError"
    assert _page_pixels(output) == _page_pixels(source)
    assert any("原始字体" in warning for warning in warnings)


def test_rollback_keeps_previous_successful_page(monkeypatch) -> None:
    source = _source(pages=2)
    insert = engine._insert_block

    def fail_second_page(page, *args, **kwargs):
        if page.number == 1 and kwargs.get("commit", True):
            return False, 12.0, "insert_textbox_failed: RuntimeError"
        return insert(page, *args, **kwargs)

    monkeypatch.setattr(engine, "_insert_block", fail_second_page)
    output, outcomes = engine.render_mono(
        source, _blocks(source), ["科学研究甲", "科学研究乙"], warnings=[],
    )
    assert [outcome.written for outcome in outcomes] == [True, False]
    assert _page_pixels(output, 0) != _page_pixels(source, 0)
    assert _page_pixels(output, 1) == _page_pixels(source, 1)


def test_actual_typesetting_does_not_erase_source_before_preflight(monkeypatch) -> None:
    source = _source()
    insert = engine._insert_block
    observed: list[bool] = []

    def inspect_preflight(page, *args, **kwargs):
        if not kwargs.get("commit", True):
            observed.append("Original scientific text" in page.get_text())
        return insert(page, *args, **kwargs)

    monkeypatch.setattr(engine, "_insert_block", inspect_preflight)
    _, outcomes = engine.render_mono(source, _blocks(source), ["科学研究"], warnings=[])
    assert outcomes[0].written
    assert observed and all(observed)
