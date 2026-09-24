# Copyright 2026 SciLoop contributors
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
"""L1 解析：arXiv HTML（ar5iv / arXiv HTML5）→ 章节 + 段落 + 字符偏移（WP05-T2）。

设计要点
--------

1. **不引入第三方 DOM 依赖**：用标准库 ``html.parser`` 建一棵极简 DOM，
   保证 austin 环境下也能跑（Docker 镜像只装 pyproject 里的依赖）。
2. **阅读顺序 = 文档顺序**：HTML 的 DOM 顺序本身就是阅读顺序，
   不需要像 PDF 那样做坐标聚类。
3. **字符偏移**：所有块的 ``char_start/char_end`` 都相对本文档的
   "归一全文"（``full_text``，块之间以 ``\\n\\n`` 连接）。同一次解析结果
   可以逐字节复现，因此偏移是可审计的。
4. **页码**：优先用 HTML 里的分页标记（``ltx_page`` / ``ltx_page_number``），
   取不到就置 ``None`` 并写 warning —— 不编造页码。``bbox`` 恒为 ``None``。
"""

from __future__ import annotations

import re
from html.parser import HTMLParser

from services.fulltext.records import (
    ParsedBlock,
    ParsedDocument,
    normalize_block_text,
)
from services.fulltext.section_map import looks_like_heading, normalize_section_name

PARSER_NAME = "ar5iv_html"
#: 1.0.1：修复 arXiv HTML5 文档根（``article.ltx_document.ltx_authors_1line``）
#: 被 ``ltx_authors`` 子串规则整体排除导致"解析后没有可用正文块"的问题。
#: 1.0.2：补上 ``handle_data`` —— 原实现未接管文本节点，DOM 里只剩元素与属性，
#: 正文文字全部丢失（只有 ``<math alttext>`` 残留），会产出公式源码级假段落。
#: 1.1.0：**公式不再被丢弃/拍平**。原先 ``ltx_equation`` 等公式容器在排除清单里 →
#: 块级公式整块消失（正文只剩编号）；``<math>`` 无 ``alttext`` 时退化成"把可见文本拼起来" →
#: ``w_t`` 变 ``wt``、``α^2`` 变 ``α2``（**语义错误**，不只是排版问题）。
#: 现在改为：公式一律还原成 **LaTeX 源码并加定界**（行内 ``$…$``、块级 ``$$…$$``），
#: 还原不出来就如实写「公式未能还原」，绝不输出错的。
#: 1.1.1：补上**第五类** —— arXiv 自己没解析成功的公式（``ltx_math_unparsed``）是
#: **纯文本 LaTeX**，不是 ``<math>`` 元素，走不到上面的分支；实测仍有 4 篇论文的
#: 正文里留着 ``{\color[rgb]{…}\mathbf{c}}`` 这类裸 LaTeX，现在给它们补定界。
PARSER_VERSION = "1.1.1"

VOID_TAGS = frozenset(
    {
        "area",
        "base",
        "br",
        "col",
        "embed",
        "hr",
        "img",
        "input",
        "link",
        "meta",
        "param",
        "source",
        "track",
        "wbr",
    }
)

HEADING_TAGS = frozenset({"h1", "h2", "h3", "h4", "h5", "h6"})
#: 只有 h1–h3 可以"切换章节"（WP05-T2 明确要求），h4–h6 仅作为文本保留
SECTION_HEADING_TAGS = frozenset({"h1", "h2", "h3"})
PARAGRAPH_TAGS = frozenset({"p"})
CAPTION_TAGS = frozenset({"figcaption"})

#: 直接丢弃整棵子树的标签（不会是正文证据）
EXCLUDE_SUBTREE_TAGS = frozenset(
    {"script", "style", "noscript", "head", "nav", "svg", "table", "thead", "tbody", "tfoot", "tr", "td", "th", "iframe", "form", "button", "select", "textarea"}
)

#: class 命中即丢弃整棵子树（参考文献、页眉页脚、作者信息等）
#: ⚠️ **公式容器不在这里**：``ltx_equation`` / ``ltx_eqn`` / ``ltx_align`` 原先被排除，
#: 导致块级公式整块消失（正文只剩编号）。现在它们由 ``EQUATION_CLASS_SUBSTRINGS`` 单独处理。
EXCLUDE_CLASS_SUBSTRINGS = (
    "bibliograph",
    "bibitem",
    "ltx_bib",
    "references",
    "citation",
    "ltx_page_footer",
    "ltx_page_header",
    "ltx_page_logo",
    "ltx_page_navbar",
    "ltx_pagination",
    "ltx_authors",
    "ltx_role_affiliation",
    "ltx_title_document",
    "ltx_subtitle",
    "ltx_note",
    "ltx_toc",
    "ltx_tag_",
    "ltx_rule",
    "screen-reader",
    "sr-only",
    "visually-hidden",
    "nav",
    "footer",
    "header",
    "sidebar",
    "toc",
)

#: 公式容器：**不丢** —— 独立成块，交给前端按 ``$$…$$`` 渲染成数学体。
#: 注意它**不参与** ``_is_block_element`` 的块判定：否则含公式的段落会被
#: ``_has_block_descendant`` 判成"有子块"从而整段被跳过，反而丢正文。
EQUATION_CLASS_SUBSTRINGS = ("ltx_equation", "ltx_eqn", "ltx_align")

#: arXiv 自己**没解析成功**的公式：以 **纯文本 LaTeX** 留在正文里（不是 ``<math>`` 元素），
#: 例如 ``where \mathcal{T}_{K} denotes…``、``{\color[rgb]{0.27,0.46,1}\mathbf{c}}``。
#: 这类走不到 ``<math>`` 分支，必须单独识别并补上定界，否则正文里就是裸 LaTeX。
MATH_UNPARSED_CLASS_SUBSTRINGS = ("ltx_math_unparsed",)

#: class 命中即整棵子树视为摘要
ABSTRACT_CLASS_SUBSTRINGS = ("ltx_abstract", "abstract")

#: 文档根骨架的 class 标记（LaTeXML / arXiv HTML5 把整篇论文挂在
#: ``<article class="ltx_document ...">`` 上）。
#:
#: 必须**先**保护文档根再看排除名单：根节点上还挂着布局变体类
#: （如 ``ltx_authors_1line`` 表示"作者排一行"），而 ``ltx_authors`` 在
#: 排除名单里用于丢弃作者块 —— 若按子串直接判排除，整篇正文会被当成
#: 作者块整体丢弃（实测 arXiv HTML5 的 ``<article class="ltx_document
#: ltx_authors_1line">`` 就会命中）。文档根内部真正的作者/参考文献块
#: 仍会被逐层排除，语义不变。
DOCUMENT_ROOT_CLASS_MARKERS = ("ltx_document",)
DOCUMENT_ROOT_TAGS = frozenset({"article", "main", "body", "div"})

_PAGE_NUMBER_INT_RE = re.compile(r"^\s*(\d{1,4})\s*$")


class _Element:
    """极简 DOM 节点。"""

    __slots__ = ("tag", "attrs", "children", "parent")

    def __init__(self, tag: str, attrs: dict[str, str], parent: _Element | None) -> None:
        self.tag = tag
        self.attrs = attrs
        self.children: list[_Element | str] = []
        self.parent = parent

    @property
    def classes(self) -> tuple[str, ...]:
        raw = self.attrs.get("class") or ""
        return tuple(part.lower() for part in raw.split() if part)

    @property
    def element_id(self) -> str:
        return (self.attrs.get("id") or "").lower()

    def class_blob(self) -> str:
        return " ".join(self.classes) + " " + self.element_id


class _DomBuilder(HTMLParser):
    """把 HTML 事件流转成一棵最小 DOM（容错：标签不闭合也能收敛）。"""

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.root = _Element("#document", {}, None)
        self.stack: list[_Element] = [self.root]

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        element = _Element(tag.lower(), {k.lower(): (v or "") for k, v in attrs}, self.stack[-1])
        self.stack[-1].children.append(element)
        if tag.lower() not in VOID_TAGS:
            self.stack.append(element)

    def handle_startendtag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        element = _Element(tag.lower(), {k.lower(): (v or "") for k, v in attrs}, self.stack[-1])
        self.stack[-1].children.append(element)

    def handle_data(self, data: str) -> None:
        """文本节点：必须显式实现。

        ``html.parser`` 的基类 ``handle_data`` 是空实现，若不接管，
        整棵 DOM 里将只剩元素与属性（例如只有 ``<math alttext>``），
        正文文字全部丢失 —— 实测会导致每个 ``<p>`` 只剩公式源码。
        """
        if data:
            self.stack[-1].children.append(data)

    def handle_endtag(self, tag: str) -> None:
        tag = tag.lower()
        if tag in VOID_TAGS:
            return
        for index in range(len(self.stack) - 1, 0, -1):
            if self.stack[index].tag == tag:
                del self.stack[index:]
                return
        # 未匹配的闭合标签：忽略（HTML 容错）


def _is_document_root(element: _Element) -> bool:
    """是否整篇文档的根骨架（命中者永不整体排除）。

    只认 ``article/main/body/div`` 这几种容器标签，避免把正文里某个
    恰好带 ``ltx_document`` 字样的内联元素误判为文档根。
    """
    if element.tag not in DOCUMENT_ROOT_TAGS:
        return False
    blob = element.class_blob()
    return any(marker in blob for marker in DOCUMENT_ROOT_CLASS_MARKERS)


def _is_excluded(element: _Element) -> bool:
    if element.tag in EXCLUDE_SUBTREE_TAGS:
        return True
    if _is_document_root(element):
        return False
    blob = element.class_blob()
    return any(token in blob for token in EXCLUDE_CLASS_SUBSTRINGS)


def _is_abstract_container(element: _Element) -> bool:
    if element.tag in HEADING_TAGS or element.tag in PARAGRAPH_TAGS:
        return False
    blob = element.class_blob()
    return any(token in blob for token in ABSTRACT_CLASS_SUBSTRINGS)


def _has_abstract_container(node: _Element) -> bool:
    for child in node.children:
        if isinstance(child, str):
            continue
        if _is_abstract_container(child):
            return True
        if _has_abstract_container(child):
            return True
    return False


#: MathML 里"只做容器"的标签：递归即可
_MATH_PASSTHROUGH_TAGS = frozenset(
    {"math", "mrow", "mstyle", "mpadded", "mphantom", "merror", "menclose", "mfenced"}
)
#: MathML 里的文本令牌
_MATH_TOKEN_TAGS = frozenset({"mi", "mn", "mo", "mtext", "ms"})
#: MathML 递归深度上限（防御性：畸形文档可能极深）
_MATH_MAX_DEPTH = 12

#: 还原不出来时的**如实标注**（绝不用拼凑的文本冒充公式）
MATH_UNRECOVERED_MARKER = "[公式未能还原]"


def _text_of(node: _Element) -> str:
    """节点下全部文本（含嵌套）。"""
    parts: list[str] = []
    for child in node.children:
        parts.append(child if isinstance(child, str) else _text_of(child))
    return "".join(parts)


def _tex_annotation(node: _Element) -> str:
    """取 MathML 里的 TeX annotation —— ar5iv 常用 ``<annotation encoding="application/x-tex">``，
    它就是作者写的 LaTeX，比"从结构重建"准得多。"""
    for child in node.children:
        if isinstance(child, str):
            continue
        if child.tag == "annotation" and "tex" in (child.attrs.get("encoding") or "").lower():
            return _text_of(child).strip()
        found = _tex_annotation(child)
        if found:
            return found
    return ""


def _render_mathml(node: _Element, depth: int = 0) -> str:
    """MathML → LaTeX（覆盖常用结构；覆盖不到返回空串，由调用方如实标注）。

    只做"结构保真"，不猜语义：``msup`` → ``^{}``、``msub`` → ``_{}``、``mfrac`` → ``\\frac``。
    """
    if depth > _MATH_MAX_DEPTH:
        return ""
    tag = node.tag
    if tag in _MATH_TOKEN_TAGS:
        return _text_of(node).strip()

    children = [child for child in node.children if not isinstance(child, str)]

    if tag in {"msup", "msub", "msubsup"}:
        if len(children) < 2:
            return ""
        base = _render_mathml(children[0], depth + 1)
        if not base:
            return ""
        sub = _render_mathml(children[1], depth + 1)
        if tag == "msup":
            return f"{base}^{{{sub}}}" if sub else ""
        if not sub:
            return ""
        if tag == "msub":
            return f"{base}_{{{sub}}}"
        sup = _render_mathml(children[2], depth + 1) if len(children) > 2 else ""
        return f"{base}_{{{sub}}}^{{{sup}}}" if sup else f"{base}_{{{sub}}}"

    if tag == "mfrac":
        if len(children) < 2:
            return ""
        numerator = _render_mathml(children[0], depth + 1)
        denominator = _render_mathml(children[1], depth + 1)
        return f"\\frac{{{numerator}}}{{{denominator}}}" if numerator and denominator else ""

    if tag == "msqrt":
        inner = "".join(_render_mathml(child, depth + 1) for child in children)
        return f"\\sqrt{{{inner}}}" if inner else ""

    if tag == "mroot":
        if len(children) < 2:
            return ""
        base = _render_mathml(children[0], depth + 1)
        degree = _render_mathml(children[1], depth + 1)
        return f"\\sqrt[{degree}]{{{base}}}" if base and degree else ""

    if tag in {"mover", "munder", "munderover"}:
        if len(children) < 2:
            return ""
        base = _render_mathml(children[0], depth + 1)
        if not base:
            return ""
        script = _render_mathml(children[1], depth + 1)
        if tag == "mover":
            return f"{base}^{{{script}}}" if script else ""
        if not script:
            return ""
        if tag == "munder":
            return f"{base}_{{{script}}}"
        over = _render_mathml(children[2], depth + 1) if len(children) > 2 else ""
        return f"{base}_{{{script}}}^{{{over}}}" if over else f"{base}_{{{script}}}"

    if tag in _MATH_PASSTHROUGH_TAGS or tag == "semantics":
        # semantics 的 annotation 分支是"人看的注释"，不是公式内容 —— 排除掉，避免重复
        inner_children = [c for c in children if c.tag != "annotation"] if tag == "semantics" else children
        return "".join(_render_mathml(child, depth + 1) for child in inner_children)

    if not children:
        return _text_of(node).strip()
    return "".join(_render_mathml(child, depth + 1) for child in children)


def math_to_latex(node: _Element) -> str:
    """``<math>`` → LaTeX 源码。

    优先级：``alttext`` → MathML 里的 TeX annotation → 从 MathML 结构重建；都拿不到返回空串。
    **绝不退化成"把可见文本拼起来"** —— 那会把 ``w_t`` 变成 ``wt``、``α^2`` 变成 ``α2``，
    是语义错误而不只是排版问题。
    """
    alt = (node.attrs.get("alttext") or "").strip()
    if alt:
        return alt
    tex = _tex_annotation(node)
    if tex:
        return tex
    return _render_mathml(node).strip()


def _collect_equation_text(node: _Element, out: list[str]) -> None:
    """公式容器内部取 LaTeX：**只认 ``<math>``**。

    为什么不能复用 :func:`_collect_text`：arXiv 把块级公式渲染成
    ``<table class="ltx_equation ltx_eqn_table">``，而 ``table/tr/td`` 在
    ``EXCLUDE_SUBTREE_TAGS`` 里（那是为了丢弃**数据表格**）→ 公式会被连带跳过。
    这里把表格当排版壳，另外排除公式编号（``ltx_tag``）。
    """
    for child in node.children:
        if isinstance(child, str):
            continue
        if child.tag == "math":
            latex = math_to_latex(child)
            if latex:
                out.append(latex)
            continue
        if "ltx_tag" in child.class_blob():
            continue
        _collect_equation_text(child, out)


def _as_display_math(text: str) -> str:
    """把"整块就是公式"的文本统一成 ``$$…$$``。

    为什么不能只看 ``<math display="block">``：arXiv 的 ``display`` 常常标在
    **容器 class**（``ltx_equation``）上而不是 ``<math>`` 属性上，于是块级公式会被
    当成行内 ``$…$`` 输出 —— 渲染出来是挤在段落里的一小串，不是独立公式。
    """
    stripped = text.strip()
    if stripped.startswith("$$") and stripped.endswith("$$") and len(stripped) > 4:
        return stripped
    if stripped.startswith("$") and stripped.endswith("$") and len(stripped) > 2:
        return f"$${stripped[1:-1]}$$"
    return f"$${stripped}$$"


def _is_unparsed_math(element: _Element) -> bool:
    """是否是 arXiv 自己没解析成功的公式（纯文本 LaTeX，不是 ``<math>`` 元素）。"""
    blob = element.class_blob()
    return any(marker in blob for marker in MATH_UNPARSED_CLASS_SUBSTRINGS)


def _is_equation_container(element: _Element) -> bool:
    """是否是公式容器（独立成块的 ``ltx_equation`` / ``ltx_eqn`` / ``ltx_align``）。"""
    blob = element.class_blob()
    return any(marker in blob for marker in EQUATION_CLASS_SUBSTRINGS)


def _collect_text(node: _Element, out: list[str]) -> None:
    """收集块内文本（inline 直接拼接，``<math>`` 一律还原成带定界的 LaTeX）。"""
    for child in node.children:
        if isinstance(child, str):
            out.append(child)
            continue
        if _is_excluded(child):
            continue
        if child.tag == "br":
            out.append("\n")
            continue
        if child.tag == "math":
            latex = math_to_latex(child)
            if not latex:
                out.append(f" {MATH_UNRECOVERED_MARKER} ")
            elif (child.attrs.get("display") or "").strip() == "block":
                # 块级公式用 $$…$$（前端按数学体渲染，独占一行）
                out.append(f"\n$${latex}$$\n")
            else:
                out.append(f" ${latex}$ ")
            continue
        if _is_unparsed_math(child):
            # arXiv 自己没解析成功的公式：正文里就是**裸 LaTeX** —— 补上定界才能渲染
            latex = _text_of(child).strip()
            if latex:
                out.append(f" ${latex}$ ")
            continue
        if child.tag in {"img", "object", "embed", "input", "source"}:
            continue
        _collect_text(child, out)


def block_text(element: _Element) -> str:
    buffer: list[str] = []
    _collect_text(element, buffer)
    # 恢复被 inline 拼接拆散的词间空格：只处理 "字-字" 直接相连且原本有元素边界的情况
    return normalize_block_text("".join(buffer))


def _is_block_element(element: _Element) -> str | None:
    """返回块的 kind；``None`` 表示不是块。"""
    if element.tag in HEADING_TAGS:
        return "heading"
    if element.tag in PARAGRAPH_TAGS:
        return "paragraph"
    if element.tag in CAPTION_TAGS:
        return "caption"
    blob = element.class_blob()
    if "caption" in blob and element.tag in {"div", "span", "figure"}:
        return "caption"
    if "ltx_para" in blob:
        return "paragraph"
    return None


def _has_block_descendant(element: _Element) -> bool:
    """块内是否还有块（避免父子重复计入）。"""
    for child in element.children:
        if isinstance(child, str):
            continue
        if _is_excluded(child):
            continue
        if _is_block_element(child) is not None:
            return True
        if _has_block_descendant(child):
            return True
    return False


class _BlockCollector:
    """按文档顺序收集块，并维护"当前章节"与"当前页码"。

    章节归属规则（确定性、可复述）：

    1. 摘要容器（``class`` 含 ``abstract``）内的块 → ``abstract``；
    2. 命中受控章节名的标题（或带编号的 h1–h3 标题）切换当前章节；
    3. 首个章节标题之前的正文：文档里存在显式摘要容器时归 ``other``，
       否则归 ``abstract``（论文首个正文块即摘要，这是通行约定）；
    4. 全文没有任何章节标题且没有摘要容器 → 一律 ``other``（不编造摘要）。
    """

    def __init__(self, *, has_abstract_container: bool) -> None:
        self.blocks: list[ParsedBlock] = []
        self.has_abstract_container = has_abstract_container
        self.current_section = "other"
        self.heading_seen = False
        self.page_counter = 0
        self.current_page: int | None = None
        self.page_markers_found = False

    # -- 页码 -----------------------------------------------------------
    def _enter_page_context(self, element: _Element) -> None:
        blob = element.class_blob()
        if "ltx_page_number" in blob:
            match = _PAGE_NUMBER_INT_RE.match(block_text(element))
            if match:
                self.page_markers_found = True
                self.current_page = int(match.group(1))
            return
        if element.tag == "div" and "ltx_page_main" in blob:
            self.page_counter += 1
            self.current_page = self.page_counter
            self.page_markers_found = True

    # -- 收集 -----------------------------------------------------------
    def walk(self, element: _Element, in_abstract: bool = False) -> None:
        child_abstract = in_abstract or _is_abstract_container(element)

        # ⚠️ **公式容器优先于排除规则**：arXiv 把块级公式渲染成
        # ``<table class="ltx_equation ltx_eqn_table">``，而 ``table`` 在
        # ``EXCLUDE_SUBTREE_TAGS`` 里（为了丢数据表格）→ 若先走排除，公式会被整棵丢掉
        # （实测：143 个块级公式一个都没留下，$$ 数为 0）。
        if _is_equation_container(element):
            self._enter_page_context(element)
            self._emit(element, "equation", child_abstract)
            return

        if _is_excluded(element):
            return
        self._enter_page_context(element)

        kind = _is_block_element(element)
        if kind is not None and not _has_block_descendant(element):
            self._emit(element, kind, child_abstract)
            return

        for child in element.children:
            if isinstance(child, str):
                continue
            self.walk(child, child_abstract)

    def _resolve_section(self, element: _Element, kind: str, text: str, in_abstract: bool) -> str:
        if in_abstract:
            return "abstract"
        if kind == "heading":
            mapped = normalize_section_name(text)
            is_section_level = (
                element.tag in SECTION_HEADING_TAGS
                or mapped != "other"
                or looks_like_heading(text)
            )
            if not is_section_level:
                # h4–h6 的非章节级标题：仅作正文保留，不改变当前章节
                return self.current_section if self.heading_seen else "other"
            if mapped == "abstract":
                return "abstract"
            self.current_section = mapped
            self.heading_seen = True
            return mapped
        if not self.heading_seen:
            return "other" if self.has_abstract_container else "abstract"
        return self.current_section

    def _emit(self, element: _Element, kind: str, in_abstract: bool) -> None:
        if kind == "equation":
            # 公式容器：内部只认 <math>（表格壳与公式编号都不算），整块统一成 $$…$$
            parts: list[str] = []
            _collect_equation_text(element, parts)
            text = _as_display_math(" \\\\ ".join(part for part in parts if part.strip()))
        else:
            text = block_text(element)
        if not text:
            return
        section = self._resolve_section(element, kind, text, in_abstract)
        self.blocks.append(
            ParsedBlock(
                text=text,
                char_start=0,
                char_end=0,
                section_name=section,
                page_number=self.current_page,
                bbox=None,
                kind=kind,
            )
        )


def parse_html(
    content: bytes | str,
    *,
    source_url: str,
    content_sha256: str,
    parser: str = PARSER_NAME,
    parser_version: str = PARSER_VERSION,
    encoding: str | None = None,
) -> ParsedDocument:
    """解析 arXiv HTML，产出带字符偏移的块序列。

    :raises ValueError: HTML 不含任何可提取正文（调用方据此回退下一级来源）
    """
    if isinstance(content, bytes):
        text = content.decode(encoding or "utf-8", errors="replace")
    else:
        text = content
    if not text or "<" not in text:
        raise ValueError("内容不是 HTML（未找到标签）")

    builder = _DomBuilder()
    builder.feed(text)
    builder.close()
    root = builder.root

    has_abstract_container = _has_abstract_container(root)
    # 先探测是否存在章节标题，决定"标题前正文"的归属
    has_headings = _detect_headings(root)

    collector = _BlockCollector(has_abstract_container=has_abstract_container)
    collector.walk(root)

    raw_blocks = collector.blocks
    if not raw_blocks:
        raise ValueError("HTML 解析后没有可用正文块")

    # 计算 char_start / char_end：块之间以 \n\n 连接
    cursor = 0
    pieces: list[str] = []
    blocks: list[ParsedBlock] = []
    for block in raw_blocks:
        if cursor:
            cursor += 2  # "\n\n"
        start = cursor
        pieces.append(block.text)
        cursor = start + len(block.text)
        block.char_start = start
        block.char_end = cursor
        blocks.append(block)
    full_text = "\n\n".join(pieces)

    warnings: list[str] = []
    page_count: int | None = None
    if collector.page_markers_found:
        page_count = collector.page_counter or (collector.current_page or None)
    else:
        warnings.append("该 HTML 无分页锚点，page_number 不可用（置 null，以引用文本为准）")
    if not has_headings:
        warnings.append("未识别到章节标题，全部段落归入 other/abstract")

    return ParsedDocument(
        source_type="html",
        source_url=source_url,
        parser=parser,
        parser_version=parser_version,
        content_sha256=content_sha256,
        full_text=full_text,
        blocks=blocks,
        char_count=len(full_text),
        page_count=page_count,
        truncated=False,
        warnings=warnings,
    )


def _detect_headings(node: _Element) -> bool:
    for child in node.children:
        if isinstance(child, str):
            continue
        if _is_excluded(child):
            continue
        if child.tag in SECTION_HEADING_TAGS and block_text(child):
            return True
        if _detect_headings(child):
            return True
    return False
