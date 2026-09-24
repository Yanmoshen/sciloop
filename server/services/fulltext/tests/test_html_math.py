# Copyright 2026 SciLoop contributors
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
"""公式解析（1.1.0）：**不丢、不拍平、加定界**。

要钉住的四件事（都是实测出来的崩坏类型）：
1. 块级公式容器（``ltx_equation``）**不再被整块丢弃** → 输出 ``$$…$$``；
2. 行内公式输出 ``$…$``（带定界，前端才能渲染，否则会和正文糊在一起）；
3. 没有 ``alttext`` 时从 MathML 结构重建 —— ``α^2`` **不能**退化成 ``α2``（语义错误）；
4. 实在还原不出来 → 如实写「公式未能还原」，**绝不用拼凑文本冒充公式**。
"""

from __future__ import annotations

from services.fulltext.html_parser import MATH_UNRECOVERED_MARKER, parse_html

_DOC = """
<html><body><article class="ltx_document">
<section>
  <h2>Method</h2>
  <p class="ltx_para">Inline math <math alttext="w_t = 2\\lambda_t\\sigma_t"><mi>w</mi></math> tail.</p>
  <div class="ltx_equation ltx_eqn_center_padleft">
    <math display="block" alttext="\\mathbb{E}[X] = \\sum_i x_i"><mi>E</mi></math>
  </div>
  <p class="ltx_para">Rebuilt: <math><msup><mi>&#945;</mi><mn>2</mn></msup></math> end.</p>
  <p class="ltx_para">Annotation: <math><semantics><msup><mi>x</mi><mn>2</mn></msup><annotation encoding="application/x-tex">x^{2}+y</annotation></semantics></math> end.</p>
</section>
</article></body></html>
"""


def _parsed_text() -> str:
    document = parse_html(
        _DOC,
        source_url="https://arxiv.org/html/0000.00000",
        content_sha256="0" * 64,
    )
    return "\n".join(block.text for block in document.blocks)


def test_inline_math_keeps_latex_with_dollar_delimiters() -> None:
    text = _parsed_text()
    assert "$w_t = 2\\lambda_t\\sigma_t$" in text, text


def test_block_equation_is_not_dropped_and_uses_display_delimiters() -> None:
    text = _parsed_text()
    assert "$$\\mathbb{E}[X] = \\sum_i x_i$$" in text, text


def test_mathml_without_alttext_is_rebuilt_with_subsup() -> None:
    """**核心回归**：``α^2`` 必须保住上标，不能变成 ``α2``。"""

    text = _parsed_text()
    assert "$\u03b1^{2}$" in text, text
    assert "\u03b12" not in text, text


def test_tex_annotation_wins_over_structural_rebuild() -> None:
    text = _parsed_text()
    assert "$x^{2}+y$" in text, text


def test_unrecoverable_math_is_marked_truthfully() -> None:
    """空的 ``<math>`` 什么都给不出来 → 必须如实标注，不能静默消失也不能编。"""

    document = parse_html(
        '<html><body><article class="ltx_document"><section><h2>Method</h2>'
        '<p class="ltx_para">A <math></math> B</p></section></article></body></html>',
        source_url="https://arxiv.org/html/0000.00001",
        content_sha256="1" * 64,
    )
    text = "\n".join(block.text for block in document.blocks)
    assert MATH_UNRECOVERED_MARKER in text, text


def test_container_level_equation_becomes_display_math() -> None:
    """``ltx_equation`` 容器本身就是块级：即使 ``<math>`` 没写 ``display="block"``，
    也必须输出 ``$$…$$``（否则渲染出来是挤在段落里的一小串）。"""

    document = parse_html(
        '<html><body><article class="ltx_document"><section><h2>Method</h2>'
        '<div class="ltx_equation"><math alttext="\\alpha + \\beta"><mi>a</mi></math></div>'
        "</section></article></body></html>",
        source_url="https://arxiv.org/html/0000.00002",
        content_sha256="2" * 64,
    )
    texts = [block.text for block in document.blocks]
    assert any(text.startswith("$$") and text.endswith("$$") for text in texts), texts
    assert any("\\alpha + \\beta" in text for text in texts), texts


def test_equation_container_with_inner_cell_still_becomes_display_block() -> None:
    """arXiv 的公式容器里常有 ``ltx_eqn_cell > ltx_para`` ——
    必须**整块**收成 ``$$…$$``，而不是落到内层段落上被当行内公式（实测：那样 ``$$`` 数为 0）。"""

    document = parse_html(
        '<html><body><article class="ltx_document"><section><h2>Method</h2>'
        '<div class="ltx_equation ltx_eqn_table">'
        '<div class="ltx_eqn_cell"><div class="ltx_para">'
        '<math alttext="E = mc^2"><mi>E</mi></math>'
        "</div></div>"
        '<div class="ltx_eqn_cell"><span class="ltx_tag ltx_tag_equation">(5)</span></div>'
        "</div>"
        "</section></article></body></html>",
        source_url="https://arxiv.org/html/0000.00003",
        content_sha256="3" * 64,
    )
    texts = [block.text for block in document.blocks]
    display = [text for text in texts if text.startswith("$$")]
    assert len(display) == 1, texts
    assert "E = mc^2" in display[0], texts
    # 公式编号（ltx_tag_equation）不该混进公式源码
    assert "(5)" not in display[0], texts


def test_table_wrapped_equation_is_not_dropped() -> None:
    """**最关键的一条**：真实 arXiv 把块级公式渲染成
    ``<table class="ltx_equation ltx_eqn_table">``，而 ``table/tr/td`` 在"丢弃数据表格"的
    排除表里。公式容器必须**先于**排除规则命中 —— 否则 143 个块级公式一个都留不下。

    同时：公式编号（``ltx_tag_equation``）不许混进公式源码。
    """

    document = parse_html(
        '<html><body><article class="ltx_document"><section><h2>Method</h2>'
        '<table class="ltx_equation ltx_eqn_table"><tbody><tr>'
        '<td class="ltx_eqn_cell">'
        '<math display="block" alttext="\\nabla \\cdot E = \\rho"><mi>n</mi></math>'
        "</td>"
        '<td class="ltx_eqn_cell ltx_eqn_eqno">'
        '<span class="ltx_tag ltx_tag_equation">(7)</span>'
        "</td>"
        "</tr></tbody></table>"
        "</section></article></body></html>",
        source_url="https://arxiv.org/html/0000.00004",
        content_sha256="4" * 64,
    )
    display = [block for block in document.blocks if block.text.startswith("$$")]
    assert len(display) == 1, [block.text for block in document.blocks]
    assert display[0].kind == "equation"
    assert "\\nabla \\cdot E = \\rho" in display[0].text
    assert "(7)" not in display[0].text


def test_unparsed_math_plain_latex_gets_delimiters() -> None:
    """**第五类**：arXiv 自己没解析成功的公式（``ltx_math_unparsed``）是**纯文本 LaTeX**，
    不是 ``<math>`` 元素，走不到 ``<math>`` 分支 —— 实测仍有 4 篇论文正文里留着
    ``{\\color[rgb]{…}\\mathbf{c}}`` 这类裸 LaTeX，必须单独补定界。"""

    document = parse_html(
        '<html><body><article class="ltx_document"><section><h2>Method</h2>'
        '<p class="ltx_para">where '
        '<span class="ltx_math_unparsed">\\mathcal{T}_{K}</span>'
        " denotes whichever class is under consideration.</p>"
        "</section></article></body></html>",
        source_url="https://arxiv.org/html/0000.00005",
        content_sha256="5" * 64,
    )
    text = "\n".join(block.text for block in document.blocks)
    assert "$\\mathcal{T}_{K}$" in text, text


def test_parser_version_is_bumped() -> None:
    """版本号是"解析结果变了"的对外信号，必须跟着改（document_version 会随之变化）。"""

    from services.fulltext.html_parser import PARSER_VERSION

    assert PARSER_VERSION >= "1.1.1"
