# Copyright 2026 SciLoop contributors
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
"""全文总结速览：把**用户确认过的口径**钉成用例。

要钉住三件事：
1. **字数口径**是"中文按字、英文术语按词"，不是"字符数"——这条最容易在后续改动里走样；
2. 提示词里的硬要求（100–200 / 保留英文 / 禁套话开头 / 只输出一段）真的还在；
3. **没有全文时判失败且不调模型**（产品口径：速览必须基于全文，不做摘要级降级、
   也不该为此花一次调用）。
"""

from __future__ import annotations

import asyncio

from services.parsing.summary import (
    MAX_CHARS,
    MIN_CHARS,
    SUMMARY_PURPOSE,
    SUMMARY_SYSTEM,
    build_summary_messages,
    generate_summary,
    summary_char_count,
)


def test_char_count_is_cjk_chars_plus_latin_words() -> None:
    """中文按字、英文（含数字）按词 —— 不是字符总数。"""

    # 汉字 8 个（我们用/做了/组消融）；BPE、3 各算 1 个词 → 10
    assert summary_char_count("我们用 BPE 做了 3 组消融") == 10
    # 纯中文按字
    assert summary_char_count("子词分词公平性") == 7
    # 中文标点不计
    assert summary_char_count("公平性，评测。") == 5
    # `**` 这类排版标记不计
    assert summary_char_count("**稀疏**注意力") == 5
    # 连字符词算一个词
    assert summary_char_count("off-policy 评估") == 3


def test_char_count_ignores_empty_input() -> None:
    assert summary_char_count("") == 0
    assert summary_char_count("   ") == 0


def test_system_prompt_pins_the_confirmed_contract() -> None:
    """口径按用户给的 skill ``paper-summary-300``（2026-09-24 确认）。"""

    for fragment in (
        f"{MIN_CHARS}–{MAX_CHARS} 字",
        "中文按字",
        "保留英文",
        "必须覆盖完整逻辑链",
        "研究痛点",
        "性能结论",
        "自然段",
        "不要用列表",
        "套话开头",
        "免责声明",
        "只用给定材料",
    ):
        assert fragment in SUMMARY_SYSTEM, fragment


def test_user_message_carries_title_abstract_and_sections() -> None:
    messages = build_summary_messages(
        title="T", abstract="A", blocks=[{"section": "method", "text": "M"}]
    )
    assert messages[0]["role"] == "system"
    assert messages[1]["role"] == "user"
    assert '"title": "T"' in messages[1]["content"]
    assert '"section": "method"' in messages[1]["content"]


def test_no_fulltext_fails_without_calling_the_model() -> None:
    """没有全文 → 直接判失败；**不调模型**（否则会白花一次调用）。"""

    result = asyncio.run(
        generate_summary(
            paper_id=1,
            document_version=None,
            title="标题",
            abstract="摘要",
            blocks=[],
        )
    )
    assert result["status"] == "failed"
    assert result["reason"] == "no_fulltext"
    assert "text" not in result


def test_blocks_without_document_version_also_fail() -> None:
    """有 blocks 但没有 document_version 同样算"没有全文"（版本是定位的前提）。"""

    result = asyncio.run(
        generate_summary(
            paper_id=1,
            document_version="",
            title="标题",
            abstract=None,
            blocks=[{"section": "method", "text": "M"}],
        )
    )
    assert result["status"] == "failed"
    assert result["reason"] == "no_fulltext"


# --------------------------------------------------------------------------- #
# 打桩测 llm.chat：长度重试 / 失败不抛异常 —— **不依赖任何 API Key**
# --------------------------------------------------------------------------- #
_BLOCK = [{"section": "method", "text": "我们提出一种两车道网格世界的最优估计量"}]

#: 42 个汉字，用于拼出长度确定落在区间内的样本
_UNIT = "作者证明当记录策略依赖历史时评估目标值无法从有限数据中识别并给出匹配下界的最优估计量"


def _ok_text() -> str:
    """长度确定落在 252–352 之间的中文（按字算）：42 × 7 = 294 字。"""

    return _UNIT * 7


class _Usage:
    prompt_tokens = 11
    completion_tokens = 22


class _Result:
    def __init__(self, content: str) -> None:
        self.content = content
        self.model_ref = "fake:model"
        self.provider = "fake"
        self.prompt_hash = "fakehash"
        self.is_replay = False
        self.cost_usd = 0.0
        self.usage = _Usage()


def _patch_chat(monkeypatch, replies: list[str] | None = None, *, raises: bool = False) -> dict:
    """把 ``llm.chat`` 换成桩；返回可观测的调用记录。"""

    record: dict = {"calls": [], "messages": []}

    async def fake_chat(messages, **kwargs):  # noqa: ANN001, ANN003, ANN202
        record["calls"].append(kwargs)
        record["messages"].append(messages)
        if raises:
            from llm.errors import LLMError

            raise LLMError("boom")
        assert replies is not None
        index = min(len(record["calls"]) - 1, len(replies) - 1)
        return _Result(replies[index])

    monkeypatch.setattr("llm.chat", fake_chat)
    return record


def _generate() -> dict:
    return asyncio.run(
        generate_summary(
            paper_id=1,
            document_version="v1",
            title="标题",
            abstract="摘要",
            blocks=_BLOCK,
        )
    )


def test_ok_path_returns_normalized_text(monkeypatch) -> None:
    record = _patch_chat(monkeypatch, [_ok_text()])
    result = _generate()

    assert result["status"] == "ok"
    assert MIN_CHARS <= result["chars"] <= MAX_CHARS
    assert result["model_ref"] == "fake:model"
    assert result["attempts"] == 1
    # 路由口径：premise 用 parse 环节 + 独立 purpose
    assert record["calls"][0]["stage"] == "parse"
    assert record["calls"][0]["purpose"] == SUMMARY_PURPOSE


def test_multi_paragraph_output_keeps_paragraph_breaks(monkeypatch) -> None:
    """**允许多段**（用户 2026-09-24 口径）：段间换行必须保留，段内换行才折成空格。

    这条是回归用例：``_normalize`` 以前会把所有换行折成空格，
    于是"可以分成 2–3 个自然段"这条要求等于白提。
    """

    part = _UNIT * 2 + "。"  # 84 字
    tail = "实验在三车道网格世界上完成并给出匹配下界。"  # 20 字
    _patch_chat(monkeypatch, [f"{part}\n\n{part}\n\n{part}{tail}"])
    result = _generate()

    assert result["status"] == "ok"
    assert MIN_CHARS <= result["chars"] <= MAX_CHARS, result["chars"]
    assert result["text"].count("\n\n") == 2, result["text"]
    assert "\n" not in result["text"].replace("\n\n", ""), "段内不该再有换行"


def test_too_many_paragraphs_is_rejected(monkeypatch) -> None:
    """段落最多 3 段（口径是"可以分成 2–3 个自然段"，不是随便分）。"""

    part = _UNIT + "。"
    too_many = "\n\n".join([part] * 8)
    _patch_chat(monkeypatch, [too_many, too_many])
    result = _generate()

    assert result["status"] == "failed"
    assert result["reason"].startswith("too_many_paragraphs")


def test_too_short_then_fixed_retries_once_with_corrective_message(monkeypatch) -> None:
    record = _patch_chat(monkeypatch, ["太短了。", _ok_text()])
    result = _generate()

    assert result["status"] == "ok"
    assert result["attempts"] == 2
    # 第二次调用必须带上"上一次不合格"的纠正消息
    second = record["messages"][1]
    assert any("上一次的输出是" in m["content"] for m in second if m["role"] == "user")


def test_too_long_twice_fails_with_reason(monkeypatch) -> None:
    huge = "作" * 400
    _patch_chat(monkeypatch, [huge, huge])
    result = _generate()

    assert result["status"] == "failed"
    assert result["reason"].startswith("too_long")


def test_banned_opener_is_rejected(monkeypatch) -> None:
    """长度合格但用套话开头 → 仍算不合格（长度检查在前，所以样本必须先够长）。"""

    text = "本文" + _UNIT * 7
    _patch_chat(monkeypatch, [text, text])
    result = _generate()

    assert result["status"] == "failed"
    assert result["reason"] == "banned_opener"


def test_llm_error_becomes_failed_and_never_raises(monkeypatch) -> None:
    _patch_chat(monkeypatch, raises=True)
    result = _generate()

    assert result["status"] == "failed"
    assert result["reason"].startswith("llm_error:")

