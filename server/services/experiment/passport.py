# Copyright 2026 SciLoop contributors
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.
"""Experiment Passport 生成（WP11-T5）。

Passport 是「这次实验到底跑了什么」的**不可变凭证**。本模块只做三件事：

1. **算哈希**：``dataset_sha256``（样本归一后）、``prompt_sha256``（Prompt 正文 +
   生成参数）、``code_commit_sha``、``dependency_lock_sha256``、``artifact_manifest``
   （逐文件 sha256）。全部可**离线复算**——哈希输入都随 Passport 一起落库。
2. **判状态**：``contracts.passport_rules.required_fields`` 任一关键字段缺失
   → ``status='incomplete'``，**禁止宣称可复现**；Run 本身失败则 ``status='failed'``。
3. **落库**：INSERT-only。``experiment_passports`` 创建后**禁止 UPDATE**，
   本模块注册 SQLAlchemy ``before_update`` 钩子把这条约束变成运行期错误，
   而不是一句注释（重跑一律生成新记录 + ``parent_passport_id``）。

诚实性红线
----------
- 哈希取不到就置 ``None`` 并把字段名记进 ``missing_fields``，**绝不生成占位哈希**
- ``code_commit_sha`` / ``dependency_lock_sha256`` 的来源（git commit / 源码树摘要 /
  依赖声明文件）写进 ``artifact_manifest.passport_provenance``，避免被误读成别的口径
- ``is_replay=True`` 的 Passport 永远不是实时实验结果（``contracts.forbidden_actions``）
"""

from __future__ import annotations

import logging
import os
import shlex
import uuid
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path
from typing import Any

from sqlalchemy import event, select

from db.models.review import ExperimentPassport
from executor.artifact_collector import canonical_json, sha256_hex

logger = logging.getLogger("sciloop.experiment.passport")

#: ``contracts.passport_rules.required_fields``（逐字一致；任一缺失 -> incomplete）
REQUIRED_FIELDS: tuple[str, ...] = (
    "dataset_name",
    "dataset_version",
    "dataset_sha256",
    "sample_manifest",
    "provider",
    "model_id",
    "prompt_version",
    "prompt_sha256",
    "generation_params",
    "template_id",
    "template_config",
    "code_commit_sha",
    "dependency_lock_sha256",
    "metrics",
    "cost_usd",
    "is_replay",
    "artifact_manifest",
    "status",
    "started_at",
    "finished_at",
)

#: ``contracts`` 允许的 status（与 DB CHECK 约束一致）
STATUSES: tuple[str, ...] = ("complete", "incomplete", "failed")

#: Run 状态 → Passport 状态（失败的 Run 不产生 complete 凭证）
FAILED_RUN_STATUSES: frozenset[str] = frozenset({"failed", "timeout", "rejected"})

#: 代码版本：git 不可用时的显式兜底环境变量（由运维提供，**不接受模板/LLM 输入**）
CODE_COMMIT_ENV = "PASSPORT_CODE_COMMIT_SHA"
#: 计算源码树摘要时的相对目录（相对 backend 工作目录）
SOURCE_TREE_DIRS: tuple[str, ...] = ("app",)
SOURCE_TREE_SUFFIXES: tuple[str, ...] = (".py",)

#: 依赖清单候选（按顺序尝试；``PASSPORT_DEPENDENCY_LOCK`` 优先）
DEFAULT_LOCK_PATH = "poetry.lock"
LOCK_CANDIDATES: tuple[str, ...] = (
    "poetry.lock",
    "uv.lock",
    "Pipfile.lock",
    "requirements.txt",
    "requirements-dev.txt",
    "pyproject.toml",
)
#: 只有这些文件名才算「已解析的锁文件」；其余（如 pyproject.toml）只是**版本区间声明**
RESOLVED_LOCK_NAMES: frozenset[str] = frozenset(
    {"poetry.lock", "uv.lock", "pipfile.lock", "requirements.txt", "requirements-dev.txt"}
)

COMMAND_TIMEOUT_SECONDS = 10.0


# --------------------------------------------------------------------------- #
# 错误
# --------------------------------------------------------------------------- #
class PassportError(RuntimeError):
    """Passport 层可预期错误。"""

    code = "passport_error"

    def __init__(self, message: str, *, detail: Any = None) -> None:
        self.detail = detail
        super().__init__(message)

    def to_dict(self) -> dict[str, Any]:
        return {"code": self.code, "message": str(self), "detail": self.detail}


class PassportNotFoundError(PassportError):
    """凭证不存在。"""

    code = "passport_not_found"


class PassportImmutableError(PassportError):
    """试图 UPDATE 已创建的 Passport（``contracts.passport_rules.immutability``）。"""

    code = "passport_immutable"


class PassportInvalidError(PassportError):
    """入参不足以生成 Passport（如 Run 无实验记录）。"""

    code = "passport_invalid"


# --------------------------------------------------------------------------- #
# 不可变性护栏：把「禁止 UPDATE」变成运行期错误
# --------------------------------------------------------------------------- #
@event.listens_for(ExperimentPassport, "before_update")
def _block_passport_update(mapper: Any, connection: Any, target: Any) -> None:  # noqa: ARG001
    """ORM 层拦截 UPDATE（迁移脚本走原生 SQL，不受影响）。"""
    raise PassportImmutableError(
        "Experiment Passport 创建后不可 UPDATE "
        "（contracts.passport_rules.immutability）：重跑请生成新记录并设置 parent_passport_id",
        detail={"passport_id": getattr(target, "id", None), "table": "experiment_passports"},
    )


def assert_immutable_update() -> None:
    """供测试/验收显式断言「UPDATE 被拒」的入口。"""
    raise PassportImmutableError(
        "Experiment Passport 创建后不可 UPDATE（contracts.passport_rules.immutability）"
    )


# --------------------------------------------------------------------------- #
# 哈希：可复算、可审计
# --------------------------------------------------------------------------- #
def backend_root() -> Path:
    """backend 工作目录（``services/experiment/passport.py`` → 上溯三层）。"""
    return Path(__file__).resolve().parents[2]


@dataclass(frozen=True)
class HashSource:
    """一个哈希及其来源（**来源必须如实标注**，避免口径被误读）。"""

    value: str | None
    kind: str
    reason: str | None = None
    inputs: tuple[str, ...] = ()

    def to_dict(self) -> dict[str, Any]:
        return {
            "value": self.value,
            "kind": self.kind,
            "reason": self.reason,
            "inputs": list(self.inputs),
        }


def normalize_samples(samples: Iterable[Mapping[str, Any]]) -> list[dict[str, Any]]:
    """样本归一（按 id 升序；只保留参与判分与复现的字段）。"""
    rows: list[dict[str, Any]] = []
    for item in samples:
        row = dict(item)
        rows.append(
            {
                "id": str(row.get("id") or ""),
                "task": row.get("task"),
                "input": str(row.get("input") or ""),
                "reference": row.get("reference"),
            }
        )
    return sorted(rows, key=lambda row: row["id"])


def dataset_digest(samples: Iterable[Mapping[str, Any]]) -> HashSource:
    """``dataset_sha256``：**归一后样本（含正文与参考答案）** 的 SHA-256。

    同一批样本 → 同一摘要；换一条样本或改一个参考答案 → 摘要必变。
    """
    rows = normalize_samples(samples)
    if not rows:
        return HashSource(None, "samples_sha256", "本次 Run 没有样本，无法计算 dataset_sha256")
    return HashSource(sha256_hex(canonical_json(rows)), "samples_sha256", inputs=[r["id"] for r in rows])


def prompt_digest(entries: Sequence[Mapping[str, Any]]) -> HashSource:
    """``prompt_sha256``：Prompt 正文（system + user 模板）的 SHA-256。

    正文随 ``generation_params.prompt_entries`` 一并落库，可离线复算。
    """
    normalized = [
        {
            "id": str(entry.get("id")),
            "system": str(entry.get("system") or ""),
            "user_template": str(entry.get("user_template") or ""),
        }
        for entry in entries
    ]
    if not normalized or not any(item["system"] or item["user_template"] for item in normalized):
        return HashSource(None, "prompt_entries_sha256", "模板未提供 Prompt 正文，无法计算 prompt_sha256")
    return HashSource(
        sha256_hex(canonical_json(normalized)),
        "prompt_entries_sha256",
        inputs=[item["id"] for item in normalized],
    )


def source_tree_digest() -> HashSource:
    """源码树摘要：``app/**/*.py`` 逐文件 SHA-256 后再聚合。

    **这不是 git commit sha**（本仓库工作区可能没有 ``.git``）：它只保证「同样的源码
    得到同样的摘要」，因此来源标注为 ``source_tree_sha256``。
    """
    root = backend_root()
    entries: list[dict[str, str]] = []
    for directory in SOURCE_TREE_DIRS:
        base = root / directory
        if not base.is_dir():
            continue
        for path in sorted(base.rglob("*")):
            if not path.is_file() or path.suffix not in SOURCE_TREE_SUFFIXES:
                continue
            if "__pycache__" in path.parts:
                continue
            relative = path.relative_to(root).as_posix()
            entries.append({"path": relative, "sha256": sha256_hex(path.read_bytes())})
    if not entries:
        return HashSource(None, "source_tree_sha256", f"源码树为空或不可读：{root}")
    digest = sha256_hex(canonical_json(entries))
    return HashSource(digest, "source_tree_sha256", inputs=[item["path"] for item in entries])


async def _run_command(command: str) -> tuple[int | None, str, str | None]:
    """执行运维配置的只读命令（``shlex`` 切分 + ``exec``，**不经 shell**）。"""
    import asyncio

    parts = shlex.split(command)
    if not parts:
        return None, "", "empty_command"
    try:
        process = await asyncio.create_subprocess_exec(
            *parts,
            cwd=str(backend_root()),
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
    except (FileNotFoundError, PermissionError, OSError) as exc:
        return None, "", f"{type(exc).__name__}: {exc}"
    try:
        stdout, stderr = await asyncio.wait_for(process.communicate(), timeout=COMMAND_TIMEOUT_SECONDS)
    except TimeoutError:
        process.kill()
        await process.wait()
        return None, "", f"timeout>{COMMAND_TIMEOUT_SECONDS}s"
    return (
        process.returncode,
        stdout.decode("utf-8", errors="replace").strip(),
        stderr.decode("utf-8", errors="replace").strip() or None,
    )


async def resolve_code_commit() -> HashSource:
    """``code_commit_sha``：git commit → 环境变量显式值 → 源码树摘要。

    三级来源都会如实标注 ``kind``；三级全失败时返回 ``None``
    （→ ``status='incomplete'``，**不生成假 sha**）。
    """
    from core.config import get_settings

    command = ""
    try:
        command = str(get_settings().passport_code_commit_cmd or "").strip()
    except Exception as exc:  # noqa: BLE001 - 配置不可用时继续走兜底
        logger.warning("读取 PASSPORT_CODE_COMMIT_CMD 失败：%s", exc)

    if command:
        returncode, stdout, stderr = await _run_command(command)
        sha = stdout.splitlines()[0].strip() if stdout else ""
        if returncode == 0 and len(sha) == 40 and all(char in "0123456789abcdef" for char in sha.lower()):
            return HashSource(sha.lower(), "git_commit", inputs=[command])
        logger.info(
            "PASSPORT_CODE_COMMIT_CMD 未产出 commit sha（rc=%s）：%s", returncode, stderr or stdout
        )

    explicit = os.environ.get(CODE_COMMIT_ENV, "").strip()
    if explicit:
        return HashSource(explicit, "env_passthrough", inputs=[CODE_COMMIT_ENV])

    tree = source_tree_digest()
    if tree.value:
        return HashSource(
            tree.value,
            tree.kind,
            reason=(
                "工作区无 git 仓库（PASSPORT_CODE_COMMIT_CMD 无 commit 输出）且未设置 "
                f"{CODE_COMMIT_ENV}：改用源码树内容摘要（kind=source_tree_sha256，"
                "**不代表任何 git 提交**）"
            ),
            inputs=tree.inputs,
        )
    return HashSource(None, "unavailable", tree.reason or "无法确定代码版本")


def dependency_lock_digest() -> HashSource:
    """``dependency_lock_sha256``：依赖清单文件的 SHA-256（逐文件摘要后聚合）。

    优先 ``PASSPORT_DEPENDENCY_LOCK``；缺失时按 :data:`LOCK_CANDIDATES` 回退。
    ``kind`` 区分「已解析的锁文件」（``lock_file``）与「仅版本区间的声明」
    （``manifest_pins``）——后者不得被称作完整锁文件。
    """
    from core.config import get_settings

    configured = DEFAULT_LOCK_PATH
    try:
        configured = str(get_settings().passport_dependency_lock or "").strip() or DEFAULT_LOCK_PATH
    except Exception as exc:  # noqa: BLE001
        logger.warning("读取 PASSPORT_DEPENDENCY_LOCK 失败，按默认值处理：%s", exc)

    ordered: list[str] = []
    for name in (configured, *LOCK_CANDIDATES):
        if name and name not in ordered:
            ordered.append(name)

    root = backend_root()
    found: list[dict[str, Any]] = []
    for name in ordered:
        for base in (root, root.parent):
            path = (base / name).resolve()
            if path.is_file():
                found.append(
                    {
                        "path": name,
                        "sha256": sha256_hex(path.read_bytes()),
                        "bytes": path.stat().st_size,
                    }
                )
                break
    if not found:
        return HashSource(
            None,
            "unavailable",
            reason=(
                f"未找到任何依赖清单（PASSPORT_DEPENDENCY_LOCK={configured}；"
                f"候选={list(LOCK_CANDIDATES)}）"
            ),
        )
    resolved = any(str(item["path"]).lower() in RESOLVED_LOCK_NAMES for item in found)
    kind = "lock_file" if resolved else "manifest_pins"
    digest = sha256_hex(canonical_json(found))
    reason = (
        None
        if resolved
        else "仅找到依赖**声明**（版本区间），未找到已解析的锁文件：本字段不是完整锁定快照"
    )
    return HashSource(digest, kind, reason=reason, inputs=[str(item["path"]) for item in found])


# --------------------------------------------------------------------------- #
# 字段完整性 / 状态判定
# --------------------------------------------------------------------------- #
def _is_missing(value: Any) -> bool:
    if value is None:
        return True
    if isinstance(value, str):
        return value.strip() == ""
    if isinstance(value, (Mapping, Sequence)) and not isinstance(value, (str, bytes)):
        return len(value) == 0
    return False


def missing_fields(payload: Mapping[str, Any]) -> list[str]:
    """按 ``contracts.passport_rules.required_fields`` 逐项判缺（顺序稳定）。

    ``status`` 自身是被判定对象，不参与缺失判定（否则永远是缺项）。
    """
    return [
        name
        for name in REQUIRED_FIELDS
        if name != "status" and _is_missing(payload.get(name))
    ]


def compute_status(payload: Mapping[str, Any], *, run_status: str | None = None) -> str:
    """状态判定：失败 Run → ``failed``；关键字段缺项 → ``incomplete``；否则 ``complete``。

    ``PASSPORT_REQUIRE_COMPLETE`` **不放松**本判定（契约优先级高于配置）：
    置 0 只会在 provenance 中留下 ``require_complete=false`` 的审计痕迹。
    """
    if str(run_status or "").lower() in FAILED_RUN_STATUSES:
        return "failed"
    if missing_fields(payload):
        return "incomplete"
    return "complete"


def require_complete_flag() -> bool:
    try:
        from core.config import get_settings

        return bool(get_settings().passport_require_complete)
    except Exception:  # noqa: BLE001 - 配置读不到时按「要求完整」处理（fail-closed）
        return True


# --------------------------------------------------------------------------- #
# 模型身份（provider / model_id 如实取自真实调用记录）
# --------------------------------------------------------------------------- #
def model_identities(records: Sequence[Mapping[str, Any]]) -> dict[str, Any]:
    """从逐样本记录汇总模型身份（出现次数最多者为主口径）。"""
    buckets: dict[str, dict[str, Any]] = {}
    for record in records:
        provider = str(record.get("provider") or "").strip()
        model_id = str(record.get("model_id") or "").strip()
        if not provider and not model_id:
            continue
        key = f"{provider}:{model_id}"
        bucket = buckets.setdefault(key, {"provider": provider, "model_id": model_id, "calls": 0})
        bucket["calls"] += 1
    ordered = sorted(buckets.values(), key=lambda item: (-int(item["calls"]), str(item["model_id"])))
    primary = ordered[0] if ordered else {"provider": "", "model_id": "", "calls": 0}
    return {
        "primary": primary,
        "all": ordered,
        "distinct": len(ordered),
        "model_scope": (
            "multi_model: provider/model_id 取自调用次数最多的模型，完整清单见 all"
            if len(ordered) > 1
            else "single_model"
        ),
    }


# --------------------------------------------------------------------------- #
# 生成
# --------------------------------------------------------------------------- #
def build_payload(
    outcome: Any,
    *,
    parent_passport_id: int | None = None,
    code_commit: HashSource | None = None,
    dependency_lock: HashSource | None = None,
    extra_provenance: Mapping[str, Any] | None = None,
    note: str | None = None,
) -> dict[str, Any]:
    """由 :class:`executor.runner.RunOutcome` 组装 Passport 字段。

    本函数是**纯函数**（除哈希来源解析）：同样的 outcome + 同样的哈希来源
    → 同样的字段，便于测试与复算。
    """
    prompt_payload = dict(getattr(outcome, "prompt_payload", {}) or {})
    dataset = dict(getattr(outcome, "dataset", {}) or {})
    samples = normalize_samples(getattr(outcome, "samples", []) or [])
    records = list(getattr(outcome, "records", []) or [])
    identities = model_identities(records)
    primary = identities["primary"]

    dataset_hash = dataset_digest(samples)
    prompt_hash = prompt_digest(list(prompt_payload.get("prompt_entries") or []))

    replay_calls = sum(1 for record in records if record.get("is_replay"))
    live_calls = len(records) - replay_calls
    is_replay = bool(getattr(outcome, "replay_only", False)) or replay_calls > 0

    sample_manifest = {
        "dataset_name": dataset.get("dataset_name") or prompt_payload.get("dataset", {}).get("dataset_name"),
        "dataset_version": (
            dataset.get("dataset_version") or prompt_payload.get("dataset", {}).get("dataset_version")
        ),
        "source_file": dataset.get("source_file"),
        "provenance": dataset.get("provenance"),
        "sample_size": len(samples),
        "sample_ids": [row["id"] for row in samples],
        "samples_sha256": dataset_hash.value,
        "samples": samples,
    }

    generation_params = {
        **dict(prompt_payload.get("generation_params") or {}),
        "template_id": getattr(outcome, "template_id", None),
        "template_version": prompt_payload.get("template_version"),
        "sample_size": len(samples),
        "sample_offset": (getattr(outcome, "template_config", {}) or {}).get("params", {}).get("sample_offset"),
        "models": identities["all"],
        "model_scope": identities["model_scope"],
        "is_replay": is_replay,
        "replay_only": bool(getattr(outcome, "replay_only", False)),
        "live_calls": live_calls,
        "replay_calls": replay_calls,
        "judge_rule": (prompt_payload.get("generation_params") or {}).get("judge_rule"),
        "egress": prompt_payload.get("egress"),
        "llm_log_window": prompt_payload.get("llm_log_window"),
        "degradations": list(getattr(outcome, "degradations", []) or []),
        "notes": list(getattr(outcome, "notes", []) or []),
        "prompt_entries": list(prompt_payload.get("prompt_entries") or []),
    }

    artifact_manifest = dict(getattr(outcome, "artifact_manifest", {}) or {})
    artifact_manifest["passport_provenance"] = {
        "dataset_sha256": dataset_hash.to_dict(),
        "prompt_sha256": prompt_hash.to_dict(),
        "code_commit_sha": (code_commit or HashSource(None, "unavailable")).to_dict(),
        "dependency_lock_sha256": (dependency_lock or HashSource(None, "unavailable")).to_dict(),
        "require_complete": require_complete_flag(),
        "run_status": getattr(outcome, "status", None),
        "error": getattr(outcome, "error", None),
        "note": note,
    }
    if extra_provenance:
        artifact_manifest["passport_provenance"].update(dict(extra_provenance))

    payload: dict[str, Any] = {
        "passport_uid": uuid.uuid4(),
        "experiment_run_id": int(getattr(outcome, "run_id", 0) or 0),
        "parent_passport_id": parent_passport_id,
        "dataset_name": sample_manifest["dataset_name"] or "",
        "dataset_version": sample_manifest["dataset_version"] or "",
        "dataset_sha256": dataset_hash.value or "",
        "sample_manifest": sample_manifest,
        "provider": primary["provider"],
        "model_id": primary["model_id"],
        "prompt_version": str(prompt_payload.get("prompt_version") or ""),
        "prompt_sha256": prompt_hash.value or "",
        "generation_params": generation_params,
        "template_id": str(getattr(outcome, "template_id", "") or ""),
        "template_config": dict(getattr(outcome, "template_config", {}) or {}),
        "code_commit_sha": (code_commit.value if code_commit else None) or "",
        "dependency_lock_sha256": (dependency_lock.value if dependency_lock else None) or "",
        "metrics": dict(getattr(outcome, "metrics", {}) or {}),
        "cost_usd": getattr(outcome, "cost_usd", None),
        "is_replay": is_replay,
        "artifact_manifest": artifact_manifest,
        "started_at": getattr(outcome, "started_at", None) or datetime.now(UTC),
        "finished_at": getattr(outcome, "finished_at", None) or datetime.now(UTC),
    }
    payload["status"] = compute_status(payload, run_status=getattr(outcome, "status", None))
    return payload


async def create_passport(
    outcome: Any,
    *,
    session: Any = None,
    parent_passport_id: int | None = None,
    note: str | None = None,
    metric_meta: Mapping[str, Any] | None = None,
    persist: bool = True,
) -> dict[str, Any]:
    """生成并落库一条 Passport（INSERT-only），返回序列化结果。

    :param parent_passport_id: replay / rerun 生成的子凭证指向父凭证
    :param persist: ``False`` 时只组装不落库（验收脚本可用来核对字段）
    """
    if not getattr(outcome, "run_id", None):
        raise PassportInvalidError(
            "RunOutcome 缺少 run_id：``experiment_passports.experiment_run_id`` 为必填外键",
            detail={"template_id": getattr(outcome, "template_id", None)},
        )

    code_commit = await resolve_code_commit()
    dependency_lock = dependency_lock_digest()
    payload = build_payload(
        outcome,
        parent_passport_id=parent_passport_id,
        code_commit=code_commit,
        dependency_lock=dependency_lock,
        extra_provenance={"metric_meta": dict(metric_meta or {})} if metric_meta else None,
        note=note,
    )

    if payload["status"] != "complete":
        logger.warning(
            "Passport 非 complete：status=%s missing=%s run=%s",
            payload["status"],
            missing_fields(payload),
            outcome.run_id,
        )

    if not persist:
        return serialize(payload)

    own_session, should_close = await _session_or_new(session)
    row = ExperimentPassport(**{**payload, "cost_usd": _decimal_or_none(payload["cost_usd"])})
    if row.cost_usd is None:
        # cost_usd 列 NOT NULL：单价缺失时如实写 0 并在 metrics/provenance 标注「成本不完整」，
        # 避免用「估算值」冒充真实成本。
        row.cost_usd = Decimal("0")
        payload["cost_usd"] = None
        payload["status"] = compute_status(payload, run_status=getattr(outcome, "status", None))
        row.status = payload["status"]
        artifact = payload["artifact_manifest"]
        artifact["cost_usd_missing"] = True
        row.artifact_manifest = artifact
        logger.warning(
            "cost_usd 缺失（llm_call_logs 单价为空）：Passport status=%s run=%s",
            payload["status"],
            outcome.run_id,
        )
    try:
        own_session.add(row)
        await own_session.flush()
        passport_id = int(row.id)
        await own_session.commit()
    finally:
        if should_close:
            await own_session.close()

    logger.info(
        "Passport 已创建 id=%s uid=%s run=%s status=%s is_replay=%s parent=%s",
        passport_id,
        payload["passport_uid"],
        outcome.run_id,
        payload["status"],
        payload["is_replay"],
        parent_passport_id,
    )
    stored = dict(payload)
    stored["id"] = passport_id
    return serialize(stored)


def _decimal_or_none(value: Any) -> Decimal | None:
    if value is None:
        return None
    try:
        return Decimal(str(round(float(value), 6)))
    except (TypeError, ValueError):
        return None


async def _session_or_new(session: Any) -> tuple[Any, bool]:
    if session is not None:
        return session, False
    from db.session import AsyncSessionLocal

    if AsyncSessionLocal is None:  # pragma: no cover - 部署期驱动缺失
        raise PassportError("数据库会话不可用（DATABASE_URL 未就绪）")
    return AsyncSessionLocal(), True


# --------------------------------------------------------------------------- #
# 读取 / 序列化
# --------------------------------------------------------------------------- #
def serialize(row: Any, *, missing: Sequence[str] | None = None) -> dict[str, Any]:
    """行/载荷 → API 结构（``missing_fields`` 永远给出，前端不猜）。"""
    if isinstance(row, Mapping):
        data = dict(row)
    else:
        data = {
            "id": int(row.id),
            "passport_uid": str(row.passport_uid),
            "experiment_run_id": int(row.experiment_run_id),
            "parent_passport_id": int(row.parent_passport_id) if row.parent_passport_id else None,
            "dataset_name": row.dataset_name,
            "dataset_version": row.dataset_version,
            "dataset_sha256": row.dataset_sha256,
            "sample_manifest": row.sample_manifest,
            "provider": row.provider,
            "model_id": row.model_id,
            "prompt_version": row.prompt_version,
            "prompt_sha256": row.prompt_sha256,
            "generation_params": row.generation_params,
            "template_id": row.template_id,
            "template_config": row.template_config,
            "code_commit_sha": row.code_commit_sha,
            "dependency_lock_sha256": row.dependency_lock_sha256,
            "metrics": row.metrics,
            "cost_usd": float(row.cost_usd) if row.cost_usd is not None else None,
            "is_replay": bool(row.is_replay),
            "artifact_manifest": row.artifact_manifest,
            "status": row.status,
            "started_at": _iso(row.started_at),
            "finished_at": _iso(row.finished_at),
            "created_at": _iso(row.created_at),
        }
    missing_list = list(missing) if missing is not None else missing_fields(data)
    data["missing_fields"] = missing_list
    if isinstance(data.get("artifact_manifest"), Mapping) and data["artifact_manifest"].get(
        "cost_usd_missing"
    ):
        # 单价缺失时列里写 0（NOT NULL 约束），对外一律回 None：不得让 0 被读成「真实零成本」
        data["cost_usd"] = None
    return data


def _iso(value: Any) -> str | None:
    if value is None:
        return None
    if isinstance(value, datetime):
        return value.isoformat()
    return str(value)


async def get_passport(passport_id: int, *, session: Any = None) -> dict[str, Any]:
    """按 id 读取（不存在即抛 :class:`PassportNotFoundError`）。"""
    own_session, should_close = await _session_or_new(session)
    try:
        row = (
            await own_session.execute(
                select(ExperimentPassport).where(ExperimentPassport.id == int(passport_id))
            )
        ).scalar_one_or_none()
    finally:
        if should_close:
            await own_session.close()
    if row is None:
        raise PassportNotFoundError(
            f"Passport id={passport_id} 不存在", detail={"passport_id": passport_id}
        )
    return serialize(row)


async def get_passport_by_run(run_id: int, *, session: Any = None) -> dict[str, Any] | None:
    """按 Run 读取（一个 Run 至多一条；没有则返回 ``None``）。"""
    own_session, should_close = await _session_or_new(session)
    try:
        rows = (
            (
                await own_session.execute(
                    select(ExperimentPassport)
                    .where(ExperimentPassport.experiment_run_id == int(run_id))
                    .order_by(ExperimentPassport.id.desc())
                )
            )
            .scalars()
            .all()
        )
    finally:
        if should_close:
            await own_session.close()
    return serialize(rows[0]) if rows else None


async def children_of(passport_id: int, *, session: Any = None) -> list[dict[str, Any]]:
    """派生子凭证（replay / rerun 结果），按 id 升序。"""
    own_session, should_close = await _session_or_new(session)
    try:
        rows = (
            (
                await own_session.execute(
                    select(ExperimentPassport)
                    .where(ExperimentPassport.parent_passport_id == int(passport_id))
                    .order_by(ExperimentPassport.id)
                )
            )
            .scalars()
            .all()
        )
    finally:
        if should_close:
            await own_session.close()
    return [serialize(row) for row in rows]


__all__ = [
    "CODE_COMMIT_ENV",
    "DEFAULT_LOCK_PATH",
    "LOCK_CANDIDATES",
    "REQUIRED_FIELDS",
    "STATUSES",
    "HashSource",
    "PassportError",
    "PassportImmutableError",
    "PassportInvalidError",
    "PassportNotFoundError",
    "assert_immutable_update",
    "build_payload",
    "children_of",
    "compute_status",
    "create_passport",
    "dataset_digest",
    "dependency_lock_digest",
    "get_passport",
    "get_passport_by_run",
    "missing_fields",
    "model_identities",
    "normalize_samples",
    "prompt_digest",
    "require_complete_flag",
    "resolve_code_commit",
    "serialize",
    "source_tree_digest",
]
