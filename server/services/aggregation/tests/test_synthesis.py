# Copyright 2026 SciLoop contributors
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
"""跨篇综述：把用户确认过的口径钉成用例。

要钉住：
1. **只用卡片与速览**（聚合不吃原论文）—— 速览要从 ``experimental_setup.evidence_meta`` 里取出来；
2. 少于两篇直接判失败且**不调模型**（单篇没有"跨篇"可言）；
3. 长度/套话/LLM 异常一律落 ``status='failed'``，**绝不抛异常**（聚合是主体，综述是增强）；
4. 提示词里的硬要求（必须跨篇、长度区间、只用给定材料）真的还在。
"""

from __future__ import annotations

import asyncio
import json

from services.aggregation.synthesis import (
    MAX_CHARS,
    MIN_CHARS,
    SYNTHESIS_PURPOSE,
    SYNTHESIS_SYSTEM,
    build_synthesis_messages,
    generate_synthesis,
)

_UNIT = "跨篇比较与归纳"  # 7 字
_OK_TEXT = _UNIT * 30  # 210 字，落在 200–400 内


def _card(paper_id: int, *, summary: str | None = "速览正文", scope: str = "fulltext") -> dict:
    return {
        "paper_id": paper_id,
        "title": f"论文 {paper_id}",
        "research_problem": "研究问题",
        "core_method": "核心方法",
        "key_innovation": [{"point": "创新点", "evidence_quote": "原文"}],
        "main_conclusions": [{"conclusion": "结论", "evidence_quote": "原文"}],
        "limitations": [{"limitation": "局限", "evidence_quote": "原文"}],
        "experimental_setup": {
            "datasets": ["D"],
            "baselines": ["B"],
            "metrics": ["M"],
            "evidence_meta": {
                "available_scope": scope,
                "summary": {"status": "ok", "text": summary} if summary else None,
            },
        },
    }


class _Usage:
    prompt_tokens = 10
    completion_tokens = 20


class _Result:
    def __init__(self, content: str) -> None:
        self.content = content
        self.model_ref = "fake:model"
        self.provider = "fake"
        self.prompt_hash = "h"
        self.is_replay = False
        self.cost_usd = 0.0
        self.usage = _Usage()


def _patch(monkeypatch, replies: list[str] | None = None, *, raises: bool = False) -> dict:
    record: dict = {"calls": [], "messages": []}

    async def fake_chat(messages, **kwargs):  # noqa: ANN001, ANN003, ANN202
        record["calls"].append(kwargs)
        record["messages"].append(messages)
        if raises:
            from llm.errors import LLMError

            raise LLMError("boom")
        assert replies is not None
        return _Result(replies[min(len(record["calls"]) - 1, len(replies) - 1)])

    monkeypatch.setattr("llm.chat", fake_chat)
    return record


def _run(cards: list[dict]) -> dict:
    return asyncio.run(generate_synthesis(cards))


# --------------------------------------------------------------------------- #
# 材料：只吃卡片与速览
# --------------------------------------------------------------------------- #
def test_messages_carry_card_fields_and_summary() -> None:
    messages = build_synthesis_messages([_card(1), _card(2)])
    payload = json.loads(messages[1]["content"])

    assert len(payload["papers"]) == 2
    first = payload["papers"][0]
    assert first["title"] == "论文 1"
    assert first["core_method"] == "核心方法"
    # 速览从 evidence_meta 里取出来（聚合不吃原论文，速览是重要输入）
    assert first["summary"] == "速览正文"
    assert first["available_scope"] == "fulltext"


def test_missing_or_failed_summary_is_null_not_fabricated() -> None:
    messages = build_synthesis_messages([_card(1, summary=None), _card(2)])
    payload = json.loads(messages[1]["content"])
    assert payload["papers"][0]["summary"] is None


def test_system_prompt_pins_the_contract() -> None:
    for fragment in (
        f"{MIN_CHARS}–{MAX_CHARS} 字",
        "中文按字",
        "保留英文",
        "一段连续文字",
        "**必须跨篇**",
        "只用给定材料",
        "套话开头",
        "不要写任何免责声明",
    ):
        assert fragment in SYNTHESIS_SYSTEM, fragment


# --------------------------------------------------------------------------- #
# 失败口径：一律 failed、不抛异常、该不调就不调
# --------------------------------------------------------------------------- #
def test_single_paper_fails_without_calling_model() -> None:
    result = _run([_card(1)])
    assert result["status"] == "failed"
    assert result["reason"] == "not_enough_papers"


def test_ok_path(monkeypatch) -> None:
    record = _patch(monkeypatch, [_OK_TEXT])
    result = _run([_card(1), _card(2)])

    assert result["status"] == "ok"
    assert MIN_CHARS <= result["chars"] <= MAX_CHARS
    assert result["paper_count"] == 2
    assert record["calls"][0]["stage"] == "parse"
    assert record["calls"][0]["purpose"] == SYNTHESIS_PURPOSE


def test_too_short_then_fixed_retries(monkeypatch) -> None:
    record = _patch(monkeypatch, ["太短。", _OK_TEXT])
    result = _run([_card(1), _card(2)])
    assert result["status"] == "ok"
    assert result["attempts"] == 2
    assert any("上一次的输出是" in m["content"] for m in record["messages"][1] if m["role"] == "user")


def test_too_long_twice_fails(monkeypatch) -> None:
    huge = "跨" * 900
    _patch(monkeypatch, [huge, huge])
    result = _run([_card(1), _card(2)])
    assert result["status"] == "failed"
    assert result["reason"].startswith("too_long")


def test_banned_opener_is_rejected(monkeypatch) -> None:
    text = "本文" + _UNIT * 30
    _patch(monkeypatch, [text, text])
    result = _run([_card(1), _card(2)])
    assert result["status"] == "failed"
    assert result["reason"] == "banned_opener"


def test_llm_error_becomes_failed_and_never_raises(monkeypatch) -> None:
    _patch(monkeypatch, raises=True)
    result = _run([_card(1), _card(2)])
    assert result["status"] == "failed"
    assert result["reason"].startswith("llm_error:")
