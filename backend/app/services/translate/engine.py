# Copyright 2026 SciLoop contributors
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
"""PyMuPDF「块级翻译 + 原位回写」引擎（``engine=pymupdf-block-v1``）。

流程（对应 EasyPaper §3.3，但**不使用 pdf2zh**）::

    读取原始 PDF
      → pymupdf 逐页提取文本块（bbox + 字号 + 颜色）
      → 逐块送 LLM 翻译（translate→中文 / simplify→A2-B1 英文）
      → 原位覆盖回写（redact 原文本 + insert_textbox 写译文，保留图片与页面尺寸）
      → mono 单语 PDF；仅 translate 模式再拼装 dual 双语 PDF（原文页/译文页交替）

版式策略（**可读性优先于"塞进去"**）
-----------------------------------

1. **旋转文本块不回写**：``get_text("dict")`` 里 ``line["dir"] != (1, 0)`` 的块
   （arXiv 侧栏水印、竖排表头等）**跳过**并保留原文。把旋转文本按横排塞进窄 bbox
   会退化成"一行一个字"，是本引擎最严重的观感缺陷（实测 ``bbox=[10.9,213.9,37.6,555.0]``
   的 arXiv 侧栏宽 26.7pt，12pt 被压到 4.6pt 仍是一列单字）。
2. **块框互相重叠的块不回写**：同一行的公式碎片（上下标、根号）会被切成多个 bbox 重叠的块，
   此时**不存在"原位"**，逐块回写必然叠印。这类块跳过并保留原文。
3. **字号有可读下限**：绝不把译文压到 ``MIN_READABLE_FONT_SIZE`` 以下，也不低于原字号的
   ``READABILITY_FLOOR_RATIO``（原字号本身更小时以原字号为界，不做放大）。
4. **放不下先扩容再放弃**：原位放不下时，沿**同栏内、不与任何文本块/图像重叠**的纵向空隙把
   bbox 向下扩（扩到下一个同栏障碍之前或页底边距），在扩容后的框里按可读下限重算字号。
5. **兜底保留原文**：上述都放不下 → **保留原文**并把页码 + bbox + 原因逐条写进 ``layout_warnings``，
   同时给出按原因汇总的计数行，**不假装版式完美**。
6. 无真实 LLM 凭据时使用本地桩（``provider=local-stub``），桩**保留原文并加显式标记**，
   绝不伪造中文译文；每次桩调用都会写 ``llm_call_logs`` 自证。
"""

from __future__ import annotations

import html
import logging
import math
import re
import tempfile
import time
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Protocol

from app.services.translate import prompts

logger = logging.getLogger("sciloop.translate.engine")

#: 单页块数上限（超大页只取前 N 块，避免异常 PDF 拖垮任务）
MAX_BLOCKS_PER_PAGE = 400
#: 整个任务参与翻译的块数上限
MAX_BLOCKS_TOTAL = 2000
#: 回写字体：PyMuPDF 内置简体中文字体名 + 注册到 PDF 时使用的别名
CJK_FONTNAME = "china-s"
CJK_REGISTERED_FONTNAME = "sciloopcjk"
#: ASCII 相对字宽（内置 CJK 字体的 ASCII 度量接近全角，注册 TTF 后为比例宽度）
ASCII_WIDTH_BUILTIN = 1.0
ASCII_WIDTH_REGISTERED = 0.52
#: 回写字号的**绝对**技术下限（低于此值 PyMuPDF 已无法正常排字）
MIN_FONT_SIZE = 4.0
#: 回写字号的**可读性**下限（pt）：低于此值不再缩小，改为扩容或保留原文
MIN_READABLE_FONT_SIZE = 7.0
#: 可读性下限的相对约束：不得低于原字号的该比例
READABILITY_FLOOR_RATIO = 0.75
#: 纵向扩容时与相邻文本块保留的安全间距（pt）
EXPAND_GAP = 2.0
#: 纵向扩容时距页面下边距保留的余量（pt）
EXPAND_BOTTOM_MARGIN = 18.0
#: 字号缩小到该比例以下时如实记一条 layout_warning（可读性下降）
SHRINK_WARN_RATIO = 0.90

#: 本地桩的 provider / model 命名（必须自证「非真实模型」）
STUB_PROVIDER = "local-stub"
STUB_MODEL_ID = "local-stub-block-translator"
#: 桩译文标记：显式告诉读者「这段没有被真正翻译」
STUB_MARK = "【本地桩·未翻译】"
STUB_NOTE = (
    f"local_stub: 无真实 LLM 凭据（LLM_DEFAULT_API_KEY 为空），译文由本地桩生成"
    f"（保留原文并加标记 {STUB_MARK}），不是真实模型输出，不得作为翻译质量证据引用"
)

_CJK_RE = re.compile(r"[\u2e80-\u9fff\uf900-\ufaff\uff00-\uffef]")


class TranslationCancelled(RuntimeError):
    """任务被取消（由 jobs 层捕获并置 ``cancelled``）。"""


class TranslatorUnavailable(RuntimeError):
    """真实模型不可用（鉴权/网络/路由），由 jobs 层如实降级为本地桩。"""


@dataclass(slots=True)
class TextBlock:
    """一个待翻译的 PDF 文本块。"""

    page: int
    bbox: tuple[float, float, float, float]
    source_text: str
    font_size: float
    color: tuple[float, float, float]
    #: 该块是否含旋转文本（``line["dir"] != (1, 0)``）——旋转块不做原位回写
    rotated: bool = False


@dataclass(slots=True)
class BlockOutcome:
    """块级翻译结果（写进 manifest.blocks）。"""

    page: int
    bbox: tuple[float, float, float, float]
    source_text: str
    target_text: str | None
    written: bool
    reason: str | None = None


@dataclass
class RenderResult:
    """渲染结果。"""

    mono_bytes: bytes
    warnings: list[str] = field(default_factory=list)


class Translator(Protocol):
    """翻译器接口（LLM 与本地桩两种实现）。"""

    name: str
    is_stub: bool

    async def translate(self, text: str, *, mode: str, page: int) -> str: ...


def _get_pymupdf():  # pragma: no cover - 依赖运行环境
    try:
        import pymupdf  # type: ignore
    except ImportError:
        try:
            import fitz as pymupdf  # type: ignore
        except ImportError as exc:
            raise RuntimeError("缺少 pymupdf：论文翻译模块需要 pymupdf 依赖") from exc
    return pymupdf


def load_document(source_bytes: bytes) -> Any:
    """打开 PDF（内存字节流）。调用方负责 ``close()``。"""
    pymupdf = _get_pymupdf()
    return pymupdf.open(stream=source_bytes, filetype="pdf")


def _now_ms() -> int:
    return int(time.perf_counter() * 1000)


def _normalize_text(raw: str) -> str:
    text = str(raw or "")
    text = text.replace("\u00ad", "").replace("\ufb01", "fi").replace("\ufb02", "fl")
    text = re.sub(r"\s+", " ", text)
    return text.strip()


def _int_to_rgb(value: Any) -> tuple[float, float, float]:
    try:
        number = int(value) & 0xFFFFFF
    except (TypeError, ValueError):
        number = 0
    return (
        ((number >> 16) & 0xFF) / 255.0,
        ((number >> 8) & 0xFF) / 255.0,
        (number & 0xFF) / 255.0,
    )


def _html_color(color: tuple[float, float, float]) -> str:  # pragma: no cover - 备用调试
    channels = [int(round(max(0.0, min(1.0, float(value))) * 255)) for value in color[:3]]
    return "#" + "".join(f"{channel:02x}" for channel in channels)


_FONT_FILE_CACHE: Path | None = None
_FONT_FILE_RESOLVED = False


def _cjk_font_file() -> Path | None:
    """把 PyMuPDF 内置 CJK 字体导出成临时 TTF（进程内缓存一次）。

    为什么要导文件：``insert_textbox(fontname="china-s")`` 对 ASCII 采用**全角**度量
    （实测 ``get_text_length("Hello World", "china-s", 10) == 110``），会让英文文本
    严重高估宽度而溢出；而 ``insert_htmlbox`` 会在**每个块**重复嵌入 CJK 字体，
    实测把 16 页产物从 0.36MB 撑到 677MB。注册一份 TTF 后，度量按真实字形比例，
    且整篇只嵌入一次，是最稳的组合。
    """
    global _FONT_FILE_CACHE, _FONT_FILE_RESOLVED
    if _FONT_FILE_RESOLVED:
        return _FONT_FILE_CACHE
    _FONT_FILE_RESOLVED = True
    try:
        pymupdf = _get_pymupdf()
        buffer = getattr(pymupdf.Font(CJK_FONTNAME), "buffer", None)
        if not buffer:
            return None
        path = Path(tempfile.gettempdir()) / "sciloop-cjk-fallback.ttf"
        if not path.is_file() or path.stat().st_size != len(buffer):
            path.write_bytes(buffer)
        _FONT_FILE_CACHE = path
    except Exception as exc:  # noqa: BLE001 - 取不到字体时退回内置字名（保守度量）
        logger.warning("CJK 字体导出失败，回退内置字体名（ASCII 记为全角）：%s", exc)
        _FONT_FILE_CACHE = None
    return _FONT_FILE_CACHE


def _text_width(text: str, size: float, ascii_ratio: float) -> float:
    """估算文本宽度（CJK 按 1.0 字宽、ASCII 按 ``ascii_ratio``）。"""
    width = 0.0
    for char in text:
        width += size if _CJK_RE.match(char) else size * ascii_ratio
    return width


def _readable_floor(base: float) -> float:
    """该块允许的**最小可读字号**。

    ``max(7.0pt, 原字号 × 0.75)``，但**不超过原字号**（原字号本身很小的脚注/图注
    不做放大，否则会比原文更醒目、反而破坏版式）。
    """
    base = max(MIN_FONT_SIZE, float(base or 0.0))
    return round(min(base, max(MIN_READABLE_FONT_SIZE, base * READABILITY_FLOOR_RATIO)), 2)


def _required_font_size(
    rect: Any, text: str, base: float, ascii_ratio: float, floor: float | None = None
) -> float | None:
    """在 ``rect`` 内放得下的最大字号（从 base 逐步缩小，不低于 ``floor``）；放不下返回 ``None``。"""
    limit = max(1.0, float(rect.width) - 1.0)
    height_limit = max(1.0, float(rect.height) + 1.5)
    lower = max(MIN_FONT_SIZE, float(floor) if floor is not None else MIN_FONT_SIZE)
    size = max(lower, min(float(base), 24.0))
    while size >= lower:
        lines = 0
        for piece in text.split("\n"):
            lines += max(1, math.ceil(_text_width(piece, size, ascii_ratio) / limit)) if piece else 1
        if lines * size * 1.22 <= height_limit:
            return size
        size = round(size - 0.25, 2)
    return None


def _overlap_area(first: Sequence[float], second: Sequence[float]) -> float:
    """两个 bbox 的重叠面积（不依赖 PyMuPDF，便于纯函数测试）。"""
    width = min(float(first[2]), float(second[2])) - max(float(first[0]), float(second[0]))
    height = min(float(first[3]), float(second[3])) - max(float(first[1]), float(second[1]))
    return width * height if width > 0 and height > 0 else 0.0


def _colliding_block_indexes(
    rects: Sequence[Sequence[float]], *, min_area: float = 4.0
) -> set[int]:
    """块框**互相重叠**的块下标集合。

    学术 PDF 里同一行的公式碎片（上下标、根号、花体符号）会被切成多个文本块，
    彼此的 bbox 本身就重叠（实测样张 496 块里有 28 块如此）。这种情况下**不存在"原位"**：
    逐块回写会让同一行多个块的新文本叠在一起（实测 page=5 出现 2308pt² 的叠印）。
    这些块一律跳过、保留原文，比强行回写更诚实。
    """
    colliding: set[int] = set()
    for i in range(len(rects)):
        for j in range(i + 1, len(rects)):
            if _overlap_area(rects[i], rects[j]) > min_area:
                colliding.add(i)
                colliding.add(j)
    return colliding


def _page_text_rects(page: Any) -> list[tuple[float, float, float, float]]:
    """本页**全部**文本块占位框（含单字符页码、页眉页脚）。

    不能用"翻译工作集里的块"当障碍物：``extract_blocks`` 会丢掉 ``len(text) < 2`` 的块，
    而页码（"5"）正是一个字符 —— 用它当边界会让最后一个块扩容时**盖住页码**
    （实测：审计脚本抓到 3 处回写文字压住页脚数字）。
    """
    try:
        payload = page.get_text("dict")
    except Exception as exc:  # noqa: BLE001 - 取不到就退化为无文本障碍
        logger.debug("get_text('dict') 失败，扩容不避让文本：%s", exc)
        return []
    rects: list[tuple[float, float, float, float]] = []
    for block in payload.get("blocks") or []:
        if int(block.get("type", 0)) != 0:
            continue
        bbox = block.get("bbox")
        if not bbox:
            continue
        rects.append((float(bbox[0]), float(bbox[1]), float(bbox[2]), float(bbox[3])))
    return rects


def _page_image_rects(page: Any) -> list[tuple[float, float, float, float]]:
    """本页**图像**占位框。

    扩容只能避让文本块是不够的：双栏论文里的图常常独占一栏下方，
    只看文本块会把译文压到图上。取不到图像信息时退化为空表（只避让文本块）。
    """
    try:
        info = page.get_image_info() or []
    except Exception as exc:  # noqa: BLE001 - 拿不到图像信息不该阻断回写
        logger.debug("get_image_info 失败，扩容仅避让文本块：%s", exc)
        return []
    rects: list[tuple[float, float, float, float]] = []
    for item in info:
        bbox = item.get("bbox") if isinstance(item, dict) else None
        if not bbox:
            continue
        rects.append((float(bbox[0]), float(bbox[1]), float(bbox[2]), float(bbox[3])))
    return rects


def _expand_rect(
    pymupdf: Any,
    rect: Any,
    occupied: Sequence[tuple[float, float, float, float]],
    page_bottom: float,
) -> Any | None:
    """把 ``rect`` 沿纵向**向下扩容**到同栏内下一个文本块之前（不与任何块重叠）。

    「同栏」用水平投影是否相交判断：只有横向与自身重叠、且顶部在自身下方的块才构成下界，
    避免把双栏排版里另一栏的块误当成障碍。没有任何空间可扩时返回 ``None``。
    """
    bottom = float(page_bottom)
    for other in occupied:
        top = float(other[1])
        if top < float(rect.y1) - 1.0:
            continue
        if float(other[2]) <= float(rect.x0) + 1.0 or float(other[0]) >= float(rect.x1) - 1.0:
            continue
        bottom = min(bottom, top - EXPAND_GAP)
    if bottom <= float(rect.y1) + 1.0:
        return None
    return pymupdf.Rect(float(rect.x0), float(rect.y0), float(rect.x1), bottom)


def _insert_block(
    page: Any,
    rect: Any,
    text: str,
    size: float,
    color: tuple[float, float, float],
    font_file: Path | None,
    floor: float | None = None,
) -> tuple[bool, float, str | None]:
    """把一个文本块写进 ``rect``；返回 ``(是否写入, 实际字号, 失败原因)``。

    估算字号只是起点：``insert_textbox`` 返回负数表示放不下且**不会写入任何内容**，
    因此这里按 0.85 的步长继续缩小重试（纯测量、无副作用），直到放下或触到**可读下限**。
    """
    payload = str(text or "")
    if not payload.strip():
        return False, 0.0, "empty_target"
    if font_file is not None:
        kwargs: dict[str, Any] = {
            "fontname": CJK_REGISTERED_FONTNAME,
            "fontfile": str(font_file),
        }
    else:  # pragma: no cover - 极端回退路径
        kwargs = {"fontname": CJK_FONTNAME}
    lower = max(MIN_FONT_SIZE, float(floor) if floor is not None else MIN_FONT_SIZE)
    attempt = max(lower, min(float(size), 24.0))
    while True:
        try:
            leftover = page.insert_textbox(
                rect, payload, fontsize=attempt, color=color, align=0, **kwargs
            )
        except Exception as exc:  # noqa: BLE001 - 单块失败不阻断该页
            return False, attempt, f"insert_textbox_failed: {type(exc).__name__}"
        if leftover >= 0:
            return True, attempt, None
        if attempt <= lower + 1e-9:
            break
        # 0.85 步长可能直接跨过「下限与上一档之间」的可行字号（如下限 9.0、
        # 12 → 10.2 → 8.67 就漏掉了 9.5），因此最后一档**必须精确落在下限上**再试一次。
        nxt = round(attempt * 0.85, 2)
        if nxt < lower:
            nxt = lower
        if nxt >= attempt:
            break
        attempt = nxt
    return False, lower, "insert_textbox_overflow_at_readable_floor"


# --------------------------------------------------------------------------- #
# 文本块提取
# --------------------------------------------------------------------------- #
def extract_blocks(document: Any, *, max_pages: int) -> tuple[list[TextBlock], list[str]]:
    """逐页提取文本块（``page.get_text("dict")``），返回 ``(blocks, warnings)``。"""
    blocks: list[TextBlock] = []
    warnings: list[str] = []
    page_count = int(document.page_count)
    for page_index in range(min(page_count, max_pages)):
        page = document.load_page(page_index)
        page_blocks: list[TextBlock] = []
        try:
            payload = page.get_text("dict")
        except Exception as exc:  # noqa: BLE001 - 单页异常不阻断整篇
            warnings.append(f"page={page_index + 1} 文本块提取失败（{type(exc).__name__}），该页未翻译")
            continue
        for raw_block in payload.get("blocks", []):
            if int(raw_block.get("type", 0)) != 0:
                continue
            pieces: list[str] = []
            size = 0.0
            color = 0
            rotated = False
            for line in raw_block.get("lines") or []:
                direction = line.get("dir") or (1.0, 0.0)
                try:
                    dx, dy = float(direction[0]), float(direction[1])
                except (TypeError, ValueError):
                    dx, dy = 1.0, 0.0
                # 非水平行（侧栏水印、竖排表头等）→ 该块标记为旋转块，回写阶段跳过
                if abs(dx - 1.0) > 0.01 or abs(dy) > 0.01:
                    rotated = True
                for span in line.get("spans") or []:
                    chunk = str(span.get("text") or "")
                    if chunk.strip():
                        pieces.append(chunk)
                    size = max(size, float(span.get("size") or 0.0))
                    color = int(span.get("color") or 0)
            text = _normalize_text(" ".join(pieces))
            if len(text) < 2:
                continue
            bbox = raw_block.get("bbox") or (0, 0, 0, 0)
            page_blocks.append(
                TextBlock(
                    page=page_index + 1,
                    bbox=(float(bbox[0]), float(bbox[1]), float(bbox[2]), float(bbox[3])),
                    source_text=text,
                    font_size=size or 10.0,
                    color=_int_to_rgb(color),
                    rotated=rotated,
                )
            )
        if len(page_blocks) > MAX_BLOCKS_PER_PAGE:
            warnings.append(
                f"page={page_index + 1} 文本块 {len(page_blocks)} 个，超过单页上限 "
                f"{MAX_BLOCKS_PER_PAGE}，仅处理前 {MAX_BLOCKS_PER_PAGE} 块"
            )
            page_blocks = page_blocks[:MAX_BLOCKS_PER_PAGE]
        # 阅读顺序：先上后下、先左后右（双栏论文的跨栏顺序不做保证，见 layout_warnings）
        page_blocks.sort(key=lambda item: (round(item.bbox[1], 1), item.bbox[0]))
        blocks.extend(page_blocks)

    if page_count > max_pages:
        warnings.append(
            f"PDF 共 {page_count} 页，超过上限 {max_pages} 页，仅翻译前 {max_pages} 页"
        )
    if len(blocks) > MAX_BLOCKS_TOTAL:
        warnings.append(
            f"文本块总数 {len(blocks)} 超过上限 {MAX_BLOCKS_TOTAL}，仅翻译前 {MAX_BLOCKS_TOTAL} 块"
        )
        blocks = blocks[:MAX_BLOCKS_TOTAL]
    return blocks, warnings


# --------------------------------------------------------------------------- #
# 翻译器：真实 LLM（走 app.llm 适配层）/ 本地桩
# --------------------------------------------------------------------------- #
def _clean_translation(raw: str) -> str:
    """清洗模型输出：去代码围栏、去「译文：」前缀、压缩空白。"""
    text = str(raw or "").strip()
    if text.startswith("```"):
        text = re.sub(r"^```[A-Za-z0-9_-]*\s*", "", text)
        text = re.sub(r"\s*```$", "", text)
    text = re.sub(r"^\s*(?:译文|翻译|Translation|Simplified)\s*[:：]\s*", "", text)
    text = text.strip().strip('"').strip("“”").strip()
    return text.strip()


@dataclass
class LLMTranslator:
    """真实模型翻译：**唯一**入口是 ``app.llm.chat``（成本登记 + 降级 + 回放）。"""

    model: Any
    name: str = "llm"
    is_stub: bool = False
    model_ref: str = ""
    project_id: int | None = None

    def __post_init__(self) -> None:
        self.model_ref = str(getattr(self.model, "model_ref", "") or "")

    async def translate(self, text: str, *, mode: str, page: int) -> str:
        from app.llm import chat

        messages = prompts.build_translate_messages(text, mode=mode, page=page)
        try:
            result = await chat(
                messages,
                model_ref=self.model_ref or None,
                stage="translate",
                purpose=f"block_{mode}",
                project_id=self.project_id,
                temperature=0.2,
                max_tokens=2048,
                allow_fallback=True,
            )
        except Exception as exc:  # noqa: BLE001 - 统一转成「模型不可用」，由 jobs 如实降级
            raise TranslatorUnavailable(f"{type(exc).__name__}: {exc}") from exc
        cleaned = _clean_translation(result.content)
        if not cleaned:
            raise TranslatorUnavailable("模型返回空译文")
        return cleaned


@dataclass
class StubTranslator:
    """本地桩：无真实凭据时**诚实兜底**（保留原文 + 显式标记，并写 llm_call_logs）。"""

    reason: str = "no_real_llm_credential"
    mode_hint: str = "translate"
    project_id: int | None = None
    name: str = STUB_PROVIDER
    is_stub: bool = True
    model_ref: str = f"{STUB_PROVIDER}:{STUB_MODEL_ID}"
    calls: int = 0

    async def translate(self, text: str, *, mode: str, page: int) -> str:
        started = _now_ms()
        target = f"{STUB_MARK}{text}"
        self.calls += 1
        await self._log_call(duration_ms=max(0, _now_ms() - started), mode=mode)
        return target

    async def _log_call(self, *, duration_ms: int, mode: str) -> None:
        """桩调用留痕：``provider=local-stub``、``cost_usd=0.0``、``is_replay=false``。"""
        try:
            from app.llm.store import get_store
            from app.llm.types import CallLogEntry

            entry = CallLogEntry(
                provider=STUB_PROVIDER,
                model=STUB_MODEL_ID,
                success=True,
                project_id=self.project_id,
                stage="translate",
                purpose=f"block_{mode}",
                cost_usd=0.0,
                duration_ms=duration_ms,
                is_replay=False,
                error=STUB_NOTE,
                model_ref=self.model_ref,
            )
            await get_store().insert_call_log(entry)
        except Exception as exc:  # noqa: BLE001 - 留痕失败不得掩盖桩产出
            logger.warning("翻译桩调用留痕失败（不影响产物）：%s", exc)


async def resolve_translator(*, project_id: int | None, mode: str) -> Translator:
    """解析可用的翻译器：优先真实模型（有 API Key），否则回落本地桩。

    - ``LLM_REPLAY`` 打开时同样返回 :class:`LLMTranslator`：回放由适配层负责，
      未命中 fixture 会抛 ``ReplayMissError``（绝不会静默改走实时）。
    - 无可用模型 → :class:`StubTranslator`（``provider=local-stub`` 自证）。
    """
    chain: list[Any] = []
    replay = False
    try:
        from app.llm.replay import is_replay_enabled
        from app.llm.router import get_router

        replay = is_replay_enabled(None)
        chain = list(await get_router().resolve_chain("translate", project_id))
    except Exception as exc:  # noqa: BLE001 - 路由不可用即视为无模型
        logger.warning("翻译模型路由解析失败，按无可用模型处理：%s", exc)

    usable = [item for item in chain if bool(getattr(item, "api_key_configured", False))]
    if usable:
        return LLMTranslator(model=usable[0], project_id=project_id)
    if replay and chain:
        return LLMTranslator(model=chain[0], project_id=project_id)
    reason = (
        "LLM_REPLAY 已打开但路由链为空"
        if replay
        else "LLM_DEFAULT_API_KEY / LLM_FALLBACK_API_KEY 均为空"
    )
    return StubTranslator(reason=reason, mode_hint=mode, project_id=project_id)


# --------------------------------------------------------------------------- #
# 原位覆盖回写
# --------------------------------------------------------------------------- #
def render_mono(
    source_bytes: bytes,
    blocks: Sequence[TextBlock],
    targets: Sequence[str | None],
    *,
    warnings: list[str],
    cancel_check: Callable[[], bool] | None = None,
) -> tuple[bytes, list[BlockOutcome]]:
    """把译文原位覆盖回写到原文 PDF，返回 ``(mono_bytes, outcomes)``。

    每个块按以下顺序处置：

    1. 旋转块（侧栏/水印/竖排）→ **跳过**，保留原文；
    2. 与同页其他块框重叠的块（公式碎片/上下标）→ **跳过**，保留原文（无"原位"可言）；
    3. 译文在原 bbox 内、字号不低于可读下限即可放下 → 原位覆盖；
    4. 放不下 → 沿**同栏纵向空隙**扩容后再试（扩展高度不与他人/图像重叠）；
    5. 仍放不下或写入失败 → **保留原文**，逐条记录 ``layout_warnings`` 并按原因汇总计数。

    绝不为了"看起来塞进去了"把字号压到不可读（旧版会一路缩到 4pt，
    把 ``bbox=[10.9,213.9,37.6,555.0]`` 的 arXiv 侧栏压成一列单字）。
    """
    pymupdf = _get_pymupdf()
    document = pymupdf.open(stream=source_bytes, filetype="pdf")
    font_file = _cjk_font_file()
    ascii_ratio = ASCII_WIDTH_REGISTERED if font_file is not None else ASCII_WIDTH_BUILTIN
    outcomes: list[BlockOutcome] = []
    skipped_rotated = 0
    skipped_unreadable = 0
    skipped_overlap = 0
    expanded_blocks = 0
    try:
        by_page: dict[int, list[int]] = {}
        for index, block in enumerate(blocks):
            by_page.setdefault(block.page, []).append(index)

        for page_number in sorted(by_page):
            if cancel_check is not None and cancel_check():
                raise TranslationCancelled("任务已取消")
            page = document.load_page(page_number - 1)
            page_rects = [blocks[item].bbox for item in by_page[page_number]]
            # 障碍物 = 本页全部文本占位（含页码）+ 全部图像：扩容不得压到任何一方上
            obstacles = _page_text_rects(page) + _page_image_rects(page)
            # 可扩容到的下边界 = 页面下边距。**不要再加"最后一个文本块 +6pt"的上限**：
            # 那会让每页最后一个块几乎无法扩容；页脚/页码由 obstacles 守住。
            page_bottom = float(page.rect.height) - EXPAND_BOTTOM_MARGIN
            # (index, redact_rect, write_rect, target, size, color, grew)
            pending: list[tuple[int, Any, Any, str, float, tuple[float, float, float], bool]] = []
            colliding = _colliding_block_indexes(page_rects)
            for position, index in enumerate(by_page[page_number]):
                block = blocks[index]
                target = targets[index] if index < len(targets) else None
                if not target or target == block.source_text:
                    outcomes.append(
                        BlockOutcome(
                            page=block.page,
                            bbox=block.bbox,
                            source_text=block.source_text,
                            target_text=target if target else block.source_text,
                            written=False,
                            reason=None if not target else "target_equals_source",
                        )
                    )
                    continue
                if block.rotated:
                    reason = "rotated_block_skipped"
                    skipped_rotated += 1
                    warnings.append(
                        f"page={block.page} bbox={_fmt_bbox(block.bbox)} 译文未回写：{reason}"
                        "（侧栏/水印/竖排文本按原方向保留，不按横排塞入窄框）"
                    )
                    outcomes.append(
                        BlockOutcome(block.page, block.bbox, block.source_text, target, False, reason)
                    )
                    continue
                if position in colliding:
                    reason = "overlaps_neighbour_block_skipped"
                    skipped_overlap += 1
                    warnings.append(
                        f"page={block.page} bbox={_fmt_bbox(block.bbox)} 译文未回写：{reason}"
                        "（与同页其他文本块的框重叠，原位回写会与邻块新文本叠印）"
                    )
                    outcomes.append(
                        BlockOutcome(block.page, block.bbox, block.source_text, target, False, reason)
                    )
                    continue
                rect = pymupdf.Rect(block.bbox)
                if rect.width <= 2 or rect.height <= 2:
                    reason = "block_bbox_too_small"
                    warnings.append(
                        f"page={block.page} bbox={_fmt_bbox(block.bbox)} 译文未回写：{reason}"
                    )
                    outcomes.append(
                        BlockOutcome(block.page, block.bbox, block.source_text, target, False, reason)
                    )
                    continue
                floor = _readable_floor(block.font_size)
                write_rect = rect
                size = _required_font_size(rect, target, block.font_size, ascii_ratio, floor)
                grew = False
                if size is None:
                    grown = _expand_rect(pymupdf, rect, obstacles, page_bottom)
                    if grown is not None:
                        grown_size = _required_font_size(
                            grown, target, block.font_size, ascii_ratio, floor
                        )
                        if grown_size is not None:
                            size, write_rect, grew = grown_size, grown, True
                if size is None:
                    reason = "translated_text_does_not_fit_at_readable_size"
                    skipped_unreadable += 1
                    warnings.append(
                        f"page={block.page} bbox={_fmt_bbox(block.bbox)} 译文未回写（在可读下限 "
                        f"{floor:.1f}pt 与原块/同栏空隙内均放不下，已保留原文）：{reason}"
                    )
                    outcomes.append(
                        BlockOutcome(block.page, block.bbox, block.source_text, target, False, reason)
                    )
                    continue
                pending.append((index, rect, write_rect, target, size, block.color, grew))

            if not pending:
                continue
            try:
                for _index, rect, _write_rect, _target, _size, _color, _grew in pending:
                    page.add_redact_annot(rect)
                # images=NONE：红色遮盖只删该块文本，绝不删除正文图片（PDF_REDACT_IMAGE_NONE）
                page.apply_redactions(
                    images=pymupdf.PDF_REDACT_IMAGE_NONE,
                    graphics=pymupdf.PDF_REDACT_LINE_ART_NONE,
                )
            except Exception as exc:  # noqa: BLE001 - 整页 redact 失败则整页保留原文
                reason = f"redaction_failed: {type(exc).__name__}"
                warnings.append(f"page={page_number} 原文本清除失败（{reason}），该页保留原文")
                for index, _rect, _write_rect, target, _size, _color, _grew in pending:
                    block = blocks[index]
                    outcomes.append(
                        BlockOutcome(block.page, block.bbox, block.source_text, target, False, reason)
                    )
                continue

            for index, rect, write_rect, target, size, color, grew in pending:
                block = blocks[index]
                written, used_size, failure = _insert_block(
                    page, write_rect, target, size, color, font_file, floor=_readable_floor(block.font_size)
                )
                if not written:
                    # 回写失败 → 立即把**原文**写回该块，避免「清了原文什么都没写」的内容丢失
                    restored, _restore_size, restore_failure = _insert_block(
                        page,
                        rect,
                        block.source_text,
                        block.font_size,
                        color,
                        font_file,
                        floor=_readable_floor(block.font_size),
                    )
                    reason = failure or "insert_failed"
                    warnings.append(
                        f"page={block.page} bbox={_fmt_bbox(block.bbox)} 译文未回写（{reason}），"
                        + ("已把原文写回" if restored else f"且原文回写亦失败（{restore_failure}）")
                    )
                    outcomes.append(
                        BlockOutcome(block.page, block.bbox, block.source_text, target, False, reason)
                    )
                    continue
                if grew:
                    expanded_blocks += 1
                if used_size < block.font_size * SHRINK_WARN_RATIO:
                    warnings.append(
                        f"page={block.page} bbox={_fmt_bbox(block.bbox)} 译文回写字号由 "
                        f"{block.font_size:.1f}pt 缩到 {used_size:.1f}pt（受原块限制，仍在可读下限内）"
                    )
                outcomes.append(
                    BlockOutcome(block.page, block.bbox, block.source_text, target, True, None)
                )

        if skipped_rotated:
            warnings.append(
                f"共 {skipped_rotated} 块旋转文本（侧栏/水印/竖排）未回写，PDF 中按原方向保留"
            )
        if skipped_unreadable:
            warnings.append(
                f"共 {skipped_unreadable} 块因可读下限（{MIN_READABLE_FONT_SIZE:g}pt 且不低于原字号 "
                f"{READABILITY_FLOOR_RATIO:.0%}）内放不下而未回写，PDF 中保留原文"
            )
        if skipped_overlap:
            warnings.append(
                f"共 {skipped_overlap} 块与同页其他文本块框重叠（公式碎片/上下标），"
                "未回写以避免叠印，PDF 中保留原文"
            )
        if expanded_blocks:
            warnings.append(
                f"共 {expanded_blocks} 块通过同栏纵向扩容回写（译文占用了原块下方空白，未与他人重叠）"
            )

        document.set_metadata(
            {
                "producer": "SciLoop translate (pymupdf-block-v1)",
                "title": "SciLoop AI-assisted translation draft",
                "subject": "本内容由 AI 辅助生成，需研究者自行核验",
            }
        )
        mono = document.tobytes(deflate=True, garbage=3)
    finally:
        document.close()
    return mono, outcomes


def render_dual(source_bytes: bytes, mono_bytes: bytes) -> bytes:
    """拼装双语 PDF：原文页 / 译文页交替（仅 ``translate`` 模式产出）。"""
    pymupdf = _get_pymupdf()
    source = pymupdf.open(stream=source_bytes, filetype="pdf")
    mono = pymupdf.open(stream=mono_bytes, filetype="pdf")
    out = pymupdf.open()
    try:
        total = min(source.page_count, mono.page_count)
        for index in range(total):
            out.insert_pdf(source, from_page=index, to_page=index)
            out.insert_pdf(mono, from_page=index, to_page=index)
        out.set_metadata(
            {
                "producer": "SciLoop translate (pymupdf-block-v1, dual)",
                "title": "SciLoop AI-assisted bilingual draft",
                "subject": "本内容由 AI 辅助生成，需研究者自行核验",
            }
        )
        # clean=True + garbage=4：去重被逐页复制的字体资源（否则双语产物会膨胀到数十 MB）
        return out.tobytes(deflate=True, garbage=4, clean=True)
    finally:
        source.close()
        mono.close()
        out.close()


def _fmt_bbox(bbox: Sequence[float]) -> str:
    return "[" + ",".join(f"{float(value):.1f}" for value in bbox[:4]) + "]"


# --------------------------------------------------------------------------- #
# 预览 HTML（原文/译文逐块对照）
# --------------------------------------------------------------------------- #
def build_preview(
    *,
    task_id: str,
    mode: str,
    engine_name: str,
    page_count: int,
    outcomes: Sequence[BlockOutcome],
    warnings: Sequence[str],
    stub_used: bool,
    stub_note: str | None = None,
    highlight_summary: dict[str, Any] | None = None,
) -> str:
    """生成 ``preview.html``：逐块原文/译文对照（含合规横幅与降级说明）。"""
    style = (
        "body{font-family:-apple-system,'Segoe UI','Microsoft YaHei',sans-serif;margin:0;"
        "background:#f7f8fa;color:#1f2329}"
        "header{background:#fff;border-bottom:1px solid #e5e6eb;padding:14px 20px;position:sticky;top:0}"
        ".banner{background:#fff7e6;border:1px solid #ffd591;color:#ad6800;padding:8px 12px;"
        "border-radius:4px;margin-top:8px;font-size:13px}"
        ".meta{font-size:13px;color:#646a73;margin-top:6px}"
        "main{padding:16px 20px 48px}"
        ".page{margin-bottom:24px}"
        ".page h2{font-size:15px;color:#2563eb;border-left:3px solid #2563eb;padding-left:8px}"
        ".block{display:grid;grid-template-columns:1fr 1fr;gap:0;border:1px solid #e5e6eb;"
        "background:#fff;margin-bottom:8px;border-radius:4px;overflow:hidden}"
        ".cell{padding:8px 10px;font-size:13px;line-height:1.55;white-space:pre-wrap;word-break:break-word}"
        ".src{color:#646a73;border-right:1px solid #e5e6eb;background:#fcfcfd}"
        ".tgt{color:#1f2329}"
        ".fail{background:#fff1f0;color:#cf1322;font-size:12px;padding:4px 10px;grid-column:1/-1}"
        ".warn{background:#fff1f0;border:1px solid #ffccc7;color:#cf1322;padding:10px 12px;"
        "border-radius:4px;font-size:13px;margin-bottom:16px}"
        ".warn li{margin:2px 0}"
    )
    parts: list[str] = [
        "<!DOCTYPE html><html lang=\"zh-CN\"><head><meta charset=\"utf-8\"/>",
        f"<title>SciLoop 翻译预览 · {html.escape(task_id)}</title>",
        f"<style>{style}</style></head><body>",
        "<header>",
        "<strong>SciLoop 论文翻译预览</strong>",
        "<div class=\"banner\">本内容由 AI 辅助生成，需研究者自行核验。</div>",
        "<div class=\"meta\">",
        f"task_id={html.escape(task_id)}｜mode={html.escape(mode)}｜engine={html.escape(engine_name)}"
        f"｜页数={int(page_count)}｜块数={len(outcomes)}",
        "</div>",
    ]
    if stub_used:
        parts.append(
            "<div class=\"banner\">降级说明："
            f"{html.escape(stub_note or STUB_NOTE)}</div>"
        )
    if highlight_summary:
        parts.append(
            "<div class=\"meta\">高亮统计："
            f"core_conclusion={int(highlight_summary.get('core_conclusion') or 0)}，"
            f"method_innovation={int(highlight_summary.get('method_innovation') or 0)}，"
            f"key_data={int(highlight_summary.get('key_data') or 0)}，"
            f"status={html.escape(str(highlight_summary.get('status') or 'skipped'))}</div>"
        )
    parts.append("</header><main>")

    if warnings:
        items = "".join(f"<li>{html.escape(str(item))}</li>" for item in warnings[:60])
        more = "" if len(warnings) <= 60 else f"<li>…共 {len(warnings)} 条，完整列表见 manifest.json</li>"
        parts.append(f"<div class=\"warn\"><strong>版式与降级提示</strong><ul>{items}{more}</ul></div>")

    grouped: dict[int, list[BlockOutcome]] = {}
    for outcome in outcomes:
        grouped.setdefault(outcome.page, []).append(outcome)

    for page_number in sorted(grouped):
        parts.append("<section class=\"page\">")
        parts.append(f"<h2>第 {page_number} 页</h2>")
        for outcome in grouped[page_number]:
            parts.append("<div class=\"block\">")
            parts.append(f"<div class=\"cell src\">{html.escape(outcome.source_text)}</div>")
            target = outcome.target_text or ""
            parts.append(f"<div class=\"cell tgt\">{html.escape(target)}</div>")
            if not outcome.written and outcome.reason:
                parts.append(
                    f"<div class=\"fail\">未回写：{html.escape(str(outcome.reason))}"
                    "（PDF 中该块保留原文）</div>"
                )
            parts.append("</div>")
        parts.append("</section>")

    parts.append("</main></body></html>")
    return "".join(parts)


__all__ = [
    "EXPAND_BOTTOM_MARGIN",
    "EXPAND_GAP",
    "MAX_BLOCKS_PER_PAGE",
    "MAX_BLOCKS_TOTAL",
    "MIN_FONT_SIZE",
    "MIN_READABLE_FONT_SIZE",
    "READABILITY_FLOOR_RATIO",
    "SHRINK_WARN_RATIO",
    "STUB_MARK",
    "STUB_MODEL_ID",
    "STUB_NOTE",
    "STUB_PROVIDER",
    "BlockOutcome",
    "LLMTranslator",
    "RenderResult",
    "StubTranslator",
    "TextBlock",
    "TranslationCancelled",
    "Translator",
    "TranslatorUnavailable",
    "build_preview",
    "extract_blocks",
    "load_document",
    "render_dual",
    "render_mono",
    "resolve_translator",
]
