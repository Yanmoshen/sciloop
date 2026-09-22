# Copyright 2026 SciLoop contributors
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
"""动作裁决：把「研究者定的边界」变成可测的纯函数。

研究者的原话（2026-09-22）落成三层边界
--------------------------------------
1. **SciLoop 这套代码本身**：能读、能改，**但绝对不能删**（"删"是直接禁止，不是等你批准）——
   理由：这里被删过一次（48 个前端文件全没了），产品会当场坏掉。
2. **研究工作项目**：能读、能写、**也能删**，但**删除与被覆盖都算高危**（弹批准卡）。
3. **数据库**：查询随便；能改（模型决策后要写证据/假设/协议）；**删数据、删表、改表结构**算高危。

另有四类**高危静态黑名单**（不依赖模型判断，可复现、可解释）：
删除/覆盖文件 · 改系统与权限 · 数据库破坏性操作 · 下载即执行。

三个决定
--------
``allow``   可以直接执行（「完全访问模式」开着时一律放行）
``approve`` 高危：必须研究者点头（即使完全访问模式开着也要点头）
``forbid``  直接拒绝（连批准入口都不给）

本模块**只做判断，不执行任何东西**，也**不碰网络与数据库** —— 所以它可以被穷举测试。
真正的执行在宿主执行器（`tools/host-runner/`），它不做业务判断，只认本模块的结论。
"""

from __future__ import annotations

import os
import re
from collections.abc import Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

__all__ = [
    "DECISION_ALLOW",
    "DECISION_APPROVE",
    "DECISION_FORBID",
    "LAYER_DATABASE",
    "LAYER_OTHER",
    "LAYER_PROJECT",
    "LAYER_SCILOOP",
    "Verdict",
    "classify_layer",
    "clear_host_roots",
    "judge_command",
    "judge_fs",
    "judge_sql",
    "learned_host_root",
    "project_roots",
    "sciloop_root",
    "sciloop_roots",
    "set_host_roots",
]

DECISION_ALLOW = "allow"
DECISION_APPROVE = "approve"
DECISION_FORBID = "forbid"

LAYER_SCILOOP = "sciloop"
LAYER_PROJECT = "project"
LAYER_DATABASE = "database"
LAYER_OTHER = "other"

#: 高危类别（给界面显示的四个名字，与研究者点名的四类一一对应）
CATEGORY_DELETE = "删除或覆盖文件"
CATEGORY_SYSTEM = "改系统与权限"
CATEGORY_DATABASE = "数据库破坏性操作"
CATEGORY_DOWNLOAD_EXEC = "下载后直接执行"

#: 「研究工作项目」的根目录。部署时用环境变量指定（可多个，逗号分隔）。
#: 默认取项目根下的 `research-workspaces`（新建项目时在这里建目录）。
PROJECT_ROOTS_ENV = "SCILOOP_PROJECT_ROOTS"
DEFAULT_PROJECT_DIRNAME = "research-workspaces"

#: ⚠️ **宿主视角的代码树根**（例如 `D:/aicoding竞赛`）。
#: 为什么必须有它：判断逻辑跑在**容器**里，容器里代码树是 `/app`；
#: 但 agent 递给宿主执行器的路径是**宿主机上的真实路径** —— 只按容器视角判的话，
#: `D:/aicoding竞赛/web/...` 会被当成"其他地方"，于是"删代码"从**硬拒**悄悄降成"弹卡"。
#: 所以两套根都要认（有宿主根就一起判）。
HOST_ROOT_ENV = "SCILOOP_HOST_ROOT"

#: 是否把「代码树内覆盖已有文件」也算高危。
#: 研究者的原话是「代码层能读能改」→ 因此**默认不**把它算高危（改代码是明确允许的）；
#: 高危清单里的"覆盖"落在**研究工作项目**里（那里的文件是研究数据，覆盖不可逆）。
SCILOOP_OVERWRITE_NEEDS_APPROVAL = False

#: 执行器在 `/health` 里报上来的宿主路径（运行期学习，优先级最高）。
#: 它是"跨机器可移植"的关键：盘符/家目录都由执行器自报，不在配置里写死。
_LEARNED: dict[str, tuple[Path, ...]] = {}


# --------------------------------------------------------------------------- #
# 边界：哪些路径属于哪一层
# --------------------------------------------------------------------------- #
def sciloop_root() -> Path:
    """SciLoop 代码树的根（`server/` 的上一层）——**容器/进程视角**。"""

    return Path(__file__).resolve().parents[3]


def sciloop_roots() -> tuple[Path, ...]:
    """代码树的根：**执行器告知的宿主路径** > 环境变量 > 容器/进程视角。"""

    roots: list[Path] = list(_LEARNED.get("sciloop", ()))
    raw = os.environ.get(HOST_ROOT_ENV, "").strip()
    if raw:
        roots.append(Path(raw))
    roots.append(sciloop_root())
    return tuple(roots)


def project_roots() -> tuple[Path, ...]:
    """研究工作项目的根目录们：**执行器告知的宿主路径** > 环境变量 > 默认目录。"""

    learned = list(_LEARNED.get("project", ()))
    if learned:
        return tuple(learned)
    raw = os.environ.get(PROJECT_ROOTS_ENV, "").strip()
    if raw:
        return tuple(Path(item.strip()).expanduser() for item in raw.split(",") if item.strip())
    return (sciloop_root() / DEFAULT_PROJECT_DIRNAME,)


def set_host_roots(
    *,
    host_root: str | Path | None = None,
    project_roots_learned: Sequence[str | Path] = (),
) -> None:
    """记录执行器报上来的宿主路径（`/health` 里带回）。

    为什么由执行器来报：后端跑在容器里，它**看不到宿主的路径长什么样**；
    而"删 SciLoop 自己的代码 = 硬拒"这条边界必须用宿主真实路径才判得出来。
    让执行器自报（它知道自己在哪）比在编排文件里写死一个盘符更稳 ——
    换机器、换盘符都不用改配置。
    """

    if host_root:
        _LEARNED["sciloop"] = (Path(str(host_root)),)
    if project_roots_learned:
        _LEARNED["project"] = tuple(Path(str(item)) for item in project_roots_learned if str(item).strip())


def clear_host_roots() -> None:
    """忘掉学到的宿主路径（执行器换机器/测试隔离用）。"""

    _LEARNED.clear()


def learned_host_root() -> str | None:
    roots = _LEARNED.get("sciloop") or ()
    return str(roots[0]) if roots else None


def _norm(path: str | Path, *, base: str | Path | None = None) -> str:
    """把路径归一成可跨平台比较的字符串键。

    为什么不用 `Path.resolve()`：
    - 判断跑在**容器**（Linux）里，而路径是**宿主**路径（`D:/aicoding竞赛/...`）——
      `resolve()` 在 Linux 下会把它当成相对路径拼到 cwd 上，两边拼法一不一致就判错；
    - Windows 路径大小写不敏感、`/` 与 `\\` 混用，直接比字符串会漏。

    归一规则：反斜杠转正斜杠 → 相对路径按 base 补全 → 折叠 `.` 与 `..` →
    去掉末尾斜杠 → **统一小写**。判"在不在里面"只认这个键的前缀关系。
    """

    text = str(path).strip().strip('"').strip("'").replace("\\", "/")
    absolute = bool(re.match(r"^([A-Za-z]:|/)", text))
    if base is not None and not absolute:
        text = f"{str(base).strip().replace(chr(92), '/')}/{text}"
    root = "/" if text.startswith("/") else ""
    parts: list[str] = []
    for piece in text.split("/"):
        if piece in ("", "."):
            continue
        if piece == ".." and parts and parts[-1] != "..":
            parts.pop()
            continue
        parts.append(piece)
    return (root + "/".join(parts)).rstrip("/").casefold()


def _inside(child: str, parent: str) -> bool:
    """child 是否就是 parent、或在 parent 里面（两者都已归一）。"""

    if not parent:
        return False
    return child == parent or child.startswith(f"{parent}/")


def classify_layer(path: str | Path, *, base: str | Path | None = None) -> str:
    """一个路径属于哪一层。

    ⚠️ **必须先判"研究工作项目"，再判代码树**（顺序是有原因的，不是随手写的）：
    默认的项目根 `<代码树>/research-workspaces` 就**嵌在代码树里面**。
    先判代码树的话，删自己项目里的数据会被当成"删代码"→ **硬拒**（本意是"弹卡待批准"），
    研究者在自己的项目里反而什么都清理不了 —— 边界判反了比不判还糟。
    """

    target = _norm(path, base=base)
    for root in project_roots():
        if _inside(target, _norm(root)):
            return LAYER_PROJECT
    if any(_inside(target, _norm(root)) for root in sciloop_roots()):
        return LAYER_SCILOOP
    return LAYER_OTHER


# --------------------------------------------------------------------------- #
# 结论对象
# --------------------------------------------------------------------------- #
@dataclass(frozen=True)
class Verdict:
    """一次裁决的结论（面向研究者的话术也在这里，**不许出现内部术语**）。

    ``harmless`` 单独标出来，是为了把两件事分清：
    「看东西」（列目录 / 读文件 / 查论文库）**任何时候都放行** —— 它不改变任何状态；
    「动手」（跑命令 / 写文件 / 删东西）要不要问人，**取决于研究者的开关**：
    完全访问模式开着就直接做，关着就弹批准卡（高危无论如何都要点头）。
    """

    decision: str
    layer: str
    message: str
    categories: tuple[str, ...] = field(default_factory=tuple)
    detail: str = ""
    harmless: bool = False

    @property
    def allowed(self) -> bool:
        """不用等人点头就能跑。"""

        return self.decision == DECISION_ALLOW

    @property
    def forbidden(self) -> bool:
        return self.decision == DECISION_FORBID

    @property
    def needs_approval(self) -> bool:
        return self.decision == DECISION_APPROVE

    def to_dict(self) -> dict[str, Any]:
        return {
            "decision": self.decision,
            "layer": self.layer,
            "message": self.message,
            "categories": list(self.categories),
            "detail": self.detail,
            "harmless": self.harmless,
        }


def _allow(layer: str, message: str, *, harmless: bool = False) -> Verdict:
    return Verdict(DECISION_ALLOW, layer, message, harmless=harmless)


def _approve(layer: str, message: str, *categories: str) -> Verdict:
    return Verdict(DECISION_APPROVE, layer, message, tuple(categories))


def _forbid(layer: str, message: str, *categories: str) -> Verdict:
    return Verdict(DECISION_FORBID, layer, message, tuple(categories))


# --------------------------------------------------------------------------- #
# 四类高危：静态规则（可复现、可解释，不靠模型）
# --------------------------------------------------------------------------- #
#: ① 删除 / 覆盖文件（含"把磁盘抹掉"这类不可逆操作）
_DELETE_PATTERNS: tuple[re.Pattern[str], ...] = (
    re.compile(r"(^|[;&|]\s*)(rm|rmdir|del|erase|rd|unlink|deltree|shred|truncate)\b", re.I),
    re.compile(r"\bremove-item\b|\bri\b\s+-|clear-content", re.I),
    re.compile(r"\b(rmtree|unlink|remove|rmdir|removedirs)\s*\(", re.I),  # python 里的删除调用
    re.compile(r"\b(format|mkfs(\.\w+)?|diskpart|fdisk|dd|wipefs)\b", re.I),
)

#: ② 改系统与权限
_SYSTEM_PATTERNS: tuple[re.Pattern[str], ...] = (
    re.compile(
        r"(^|[;&|]\s*)(sudo|su|doas|runas|takeown|icacls|cacls|attrib|chmod|chown|chgrp|setfacl)\b",
        re.I,
    ),
    re.compile(r"\b(reg|regedit)\b\s+(add|delete|import|restore)", re.I),
    re.compile(r"\b(setx|systemctl|service|sc)\b\s+(create|config|delete|start|stop|enable|disable)", re.I),
    re.compile(r"\bnet\b\s+(user|localgroup)\b", re.I),
    re.compile(r"\b(ufw|firewall-cmd|iptables|netsh)\b", re.I),
    re.compile(
        r"(^|[;&|]\s*)(apt|apt-get|yum|dnf|apk|pacman|zypper|brew|choco|winget|snap)\b", re.I
    ),
    re.compile(r"(^|[;&|]\s*)(shutdown|reboot|poweroff|halt)\b", re.I),
)

#: ③ 数据库破坏性操作（SQL 与命令行两种形态都要管）
_SQL_DESTRUCTIVE: tuple[re.Pattern[str], ...] = (
    re.compile(r"\bdrop\s+(table|database|schema|index|view|user|role)\b", re.I),
    re.compile(r"\btruncate\b", re.I),
    re.compile(r"\bdelete\s+from\b", re.I),
    re.compile(r"\balter\s+table\b[^;]*\bdrop\b", re.I),
    re.compile(r"\bdropdb\b|\bdropuser\b", re.I),
)

#: ④ 下载后直接执行（把从网上下来的东西直接喂给解释器）
_DOWNLOAD_EXEC: tuple[re.Pattern[str], ...] = (
    re.compile(r"(curl|wget|iwr|invoke-webrequest)[^\n|;]*\|\s*(ba|z|d|k)?sh\b", re.I),
    re.compile(r"(curl|wget|iwr|invoke-webrequest)[^\n|;]*\|\s*(python|node|perl|ruby|pwsh)\b", re.I),
    re.compile(r"\b(iex|invoke-expression)\b", re.I),
    re.compile(r"powershell[^\n]*\s-(enc|encodedcommand)\b", re.I),
)


def _text_of(argv: list[str] | None, command: str | None) -> str:
    if argv:
        return " ".join(str(part) for part in argv)
    return command or ""


def _path_arguments(text: str) -> list[str]:
    """从命令里挑出"看起来是路径"的参数（用于判断删除是否落在代码树内）。

    只做保守识别：带路径分隔符、或以常见盘符/家目录开头的 token。
    识别不出来就不硬猜 —— 宁可让人来点头，也不假装看懂了。
    """

    found: list[str] = []
    for token in re.split(r"[\s;&|]+", text):
        raw = token.strip("'\"")
        if not raw or raw.startswith("-"):
            continue
        if re.search(r"[\\/]", raw) or re.match(r"^[A-Za-z]:$", raw) or raw.startswith("~"):
            found.append(raw)
    return found


def _scan(text: str) -> list[str]:
    """返回命中的高危类别（可能多个）。"""

    hits: list[str] = []
    if any(pattern.search(text) for pattern in _DELETE_PATTERNS):
        hits.append(CATEGORY_DELETE)
    if any(pattern.search(text) for pattern in _SYSTEM_PATTERNS):
        hits.append(CATEGORY_SYSTEM)
    if any(pattern.search(text) for pattern in _SQL_DESTRUCTIVE):
        hits.append(CATEGORY_DATABASE)
    if any(pattern.search(text) for pattern in _DOWNLOAD_EXEC):
        hits.append(CATEGORY_DOWNLOAD_EXEC)
    return hits


# --------------------------------------------------------------------------- #
# 裁决入口：命令 / 文件 / SQL
# --------------------------------------------------------------------------- #
def judge_command(
    *,
    argv: list[str] | None = None,
    command: str | None = None,
    cwd: str | Path | None = None,
) -> Verdict:
    """裁决"跑一条命令"。"""

    text = _text_of(argv, command)
    workdir_layer = classify_layer(cwd) if cwd else LAYER_OTHER
    hits = _scan(text)

    if not hits:
        return _allow(workdir_layer, "普通命令，可以直接跑")

    # 删除类命令如果指名道姓落在 SciLoop 代码树里 → 直接拒绝（不是"等你批准"）
    if CATEGORY_DELETE in hits:
        for raw in _path_arguments(text):
            # 相对路径按命令的工作目录解；没给工作目录就不硬猜（宁可让人点头）
            if cwd and classify_layer(raw, base=cwd) == LAYER_SCILOOP:
                return _forbid(
                    LAYER_SCILOOP,
                    "这条命令会删掉 SciLoop 自己的代码文件——产品会因此坏掉，我不能执行。"
                    "如果你的目标是研究项目里的文件，请把路径指到项目目录下。",
                    *hits,
                )
            if not cwd and classify_layer(raw) == LAYER_SCILOOP:
                return _forbid(
                    LAYER_SCILOOP,
                    "这条命令会删掉 SciLoop 自己的代码文件——产品会因此坏掉，我不能执行。"
                    "如果你的目标是研究项目里的文件，请把路径指到项目目录下。",
                    *hits,
                )

    return _approve(
        workdir_layer,
        "这条命令涉及删除/系统设置/数据库改动/从网上下载后直接运行，属于高危操作，请你确认后我再执行。",
        *hits,
    )


def judge_fs(
    *,
    action: str,
    path: str | Path,
    to: str | Path | None = None,
    exists: bool | None = None,
    recursive: bool = False,
) -> Verdict:
    """裁决一次文件操作。

    ``exists`` = 目标当前是否已存在（调用方先看一眼再传进来）：
    覆盖已有文件在工作项目里算高危，新建文件不算。
    """

    act = (action or "").strip().lower()
    layer = classify_layer(path)

    if act in ("list", "read", "stat"):
        return _allow(layer, "读取文件或目录，可以直接做", harmless=True)

    if act in ("write", "mkdir", "move", "copy"):
        if layer == LAYER_SCILOOP:
            if act == "write" and exists and SCILOOP_OVERWRITE_NEEDS_APPROVAL:
                return _approve(layer, "这条会覆盖 SciLoop 已有的代码文件，请你确认。", CATEGORY_DELETE)
            return _allow(layer, "改自己的代码是你允许的（但删文件不行）")
        if layer == LAYER_PROJECT:
            if act == "move" or (act in ("write", "copy") and exists):
                return _approve(
                    layer,
                    "这会覆盖或移动研究项目里已有的文件，覆盖之后无法还原，请你确认。",
                    CATEGORY_DELETE,
                )
            return _allow(layer, "在你自己给的研究项目里新建内容，可以直接做")
        # 其他地方（项目目录之外的磁盘位置）：写新文件放行，覆盖要先问
        if act == "move" or (act in ("write", "copy") and exists):
            return _approve(
                layer,
                "这会覆盖或移动项目目录之外的文件，请你确认。",
                CATEGORY_DELETE,
            )
        return _allow(layer, "在项目目录之外新建文件，可以直接做")

    if act == "delete":
        if layer == LAYER_SCILOOP:
            return _forbid(
                LAYER_SCILOOP,
                "这是 SciLoop 自己的代码文件，删掉产品就坏了——我不能执行。"
                "要清理研究数据请指向研究项目目录。",
                CATEGORY_DELETE,
            )
        target = "目录" if recursive else "文件"
        return _approve(
            layer,
            f"这会删除{target}，删掉无法还原，请你确认。",
            CATEGORY_DELETE,
        )

    return _approve(LAYER_OTHER, f"不认识的操作「{action}」，请你确认后我再做。")


def judge_sql(sql: str) -> Verdict:
    """裁决一条 SQL（写操作放行、破坏性操作要人点头）。"""

    text = (sql or "").strip()
    if not text:
        return _allow(LAYER_DATABASE, "空语句，无需处理", harmless=True)
    hits = [c for c in _scan(text) if c == CATEGORY_DATABASE]
    if hits:
        return _approve(
            LAYER_DATABASE,
            "这句会删掉或改动数据库结构，属于不可逆操作，请你确认。",
            *hits,
        )
    read_only = bool(re.match(r"^(select|with|explain|show|table)\b", text, re.I))
    return _allow(
        LAYER_DATABASE,
        "读写数据，可以直接做（改结构、删数据才需要你确认）",
        harmless=read_only,
    )
