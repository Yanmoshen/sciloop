# Copyright 2026 SciLoop contributors
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
"""模板共享设施（WP11-T2/T3）。

三个职责：

1. **Prompt 资产**：从 ``templates/data/*.json`` 读 Prompt 正文并渲染（``render``）
2. **出网闸门**：``prepare_targets`` 解析模型 → **每个模板的每次 LLM 调用都先过
   :mod:`app.executor.egress_proxy` 白名单**（非白名单/内网直接失败）
3. **回放闸门**：``call_model`` 在 ``ctx.fixture_source`` 非空时**只读回放源**，
   缺条目即抛 :class:`ReplaySourceMissError`，**绝不静默改走实时**；
   回放调用会写一行 ``llm_call_logs(is_replay=true)``（反事实成本，WP02 口径）
4. **本地桩兜底**（仅当解析不到任何真实模型）：``prepare_targets`` 回落到
   :class:`LocalStubTarget`，``call_model`` 产出**确定性桩输出**。桩不是模型：
   答案由 ``stub_answer`` 的极简规则生成，绝大多数与参考答案不一致——这正是桩的
   应有表现。每次桩调用都会写 ``llm_call_logs(provider='local-stub', cost_usd=0.0,
   error='local_stub: ...')`` 并追加降级说明，**绝不冒充实时实验结果**
   （``contracts.forbidden_actions``）
"""

from __future__ import annotations

import json
import logging
import os
import re
import time
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from app.executor.artifact_collector import canonical_json, sha256_hex
from app.executor.egress_proxy import EgressPolicy, get_policy, host_of
from app.executor.errors import ModelUnavailableError, ReplaySourceMissError
from app.executor.runner import RunContext

logger = logging.getLogger("sciloop.experiment.templates")

DATA_DIR = Path(__file__).resolve().parent / "data"

#: 数据集里 ``task`` → 给模型的任务说明（渲染进 Prompt 的 ``{task_hint}``）
TASK_HINTS: dict[str, str] = {
    "sentiment": "对给定短句做情感三分类，只回答 positive、negative 或 neutral 之一。",
    "arithmetic": "解出给定应用题的结果，只回答数字（不要单位、不要过程）。",
    "extraction": "从给定句子中抽取要求的实体，只回答实体文本本身。",
}

#: 回放源的键分隔符（与 :func:`fixture_key` 一致）
KEY_SEP = "|"


def load_prompt_asset(filename: str) -> dict[str, Any]:
    """读取 Prompt 资产（只读；文件缺失即如实失败）。"""
    path = DATA_DIR / str(filename)
    if not path.is_file():
        raise ModelUnavailableError(
            f"Prompt 资产缺失：{path}", detail={"asset": filename, "dir": str(DATA_DIR)}
        )
    return json.loads(path.read_text(encoding="utf-8"))


def render(text: str, **values: Any) -> str:
    """极简占位符渲染（只替换 ``{key}``；未提供的占位符保持原样，不报错也不吞错）。"""
    output = str(text or "")
    for key, value in values.items():
        output = output.replace("{" + str(key) + "}", str(value))
    return output


def task_hint(task: Any) -> str:
    """任务说明（未知任务如实退回通用说明，不编造任务类型）。"""
    key = str(task or "").strip()
    return TASK_HINTS.get(key, f"按照题目要求作答（任务类型：{key or '未标注'}）。")


def build_messages(system: str, user: str) -> list[dict[str, str]]:
    return [
        {"role": "system", "content": system},
        {"role": "user", "content": user},
    ]


def fixture_key(group: str, sample_id: str) -> str:
    """回放源键：``<group>|<sample_id>``（同一 Run 内唯一确定一次调用）。"""
    return f"{group}{KEY_SEP}{sample_id}"


def prompt_digest_payload(entries: Sequence[Mapping[str, Any]]) -> list[dict[str, Any]]:
    """参与 ``prompt_sha256`` 的规范化正文（含 system/user 全文，可完整复算）。"""
    return [
        {
            "id": str(entry.get("id")),
            "system": str(entry.get("system") or ""),
            "user_template": str(entry.get("user_template") or ""),
        }
        for entry in entries
    ]


# --------------------------------------------------------------------------- #
# 本地确定性桩（无真实 LLM 凭据时的**诚实**兜底；见模块文档第 4 条）
# --------------------------------------------------------------------------- #
#: 关闭桩兜底（置 ``0``）：之后无凭据时实验会如实失败而不是走桩
LOCAL_STUB_ENV = "EXPERIMENT_LOCAL_STUB"

#: 桩的 provider/model 命名必须自证「非真实模型」（含 stub/local 标记）
STUB_PROVIDER = "local-stub"
STUB_MODEL_IDS: tuple[str, ...] = (
    "local-stub-deterministic-a",
    "local-stub-deterministic-b",
)

_STUB_NUMBER = re.compile(r"\d+(?:\.\d+)?")
#: 只识别显式二元运算符；``-`` 不识别（与负数写法歧义，宁可不猜）
_STUB_OPS: dict[str, Any] = {
    "+": lambda a, b: a + b,
    "*": lambda a, b: a * b,
    "×": lambda a, b: a * b,
    "÷": lambda a, b: a / b,
    "/": lambda a, b: a / b,
}
#: 桩无法作答时的显式哨兵值（不是任何参考答案，仅表示「桩答不出」）
STUB_FALLBACK_ANSWER = "unknown"


@dataclass(frozen=True)
class LocalStubTarget:
    """本地桩「模型」：结构与 :class:`app.llm.types.ResolvedModel` 对齐但**不是模型**。"""

    model_ref: str
    provider: str = STUB_PROVIDER
    model_id: str = STUB_MODEL_IDS[0]
    base_url: str = ""
    source: str = "local_stub"
    is_stub: bool = True
    api_key: str = ""

    def describe(self) -> dict[str, Any]:
        return {
            "model_ref": self.model_ref,
            "provider": self.provider,
            "model_id": self.model_id,
            "source": self.source,
            "is_stub": True,
        }


def local_stub_enabled() -> bool:
    """桩兜底开关（``EXPERIMENT_LOCAL_STUB``；默认开启，置 ``0`` 关闭）。"""
    return (os.environ.get(LOCAL_STUB_ENV, "1") or "1").strip() != "0"


def stub_targets(count: int) -> list[LocalStubTarget]:
    """构造 ``count`` 个**互不相同**的桩目标（T3 的两个模型因此可区分）。"""
    targets: list[LocalStubTarget] = []
    for index in range(max(1, int(count))):
        model_id = STUB_MODEL_IDS[index % len(STUB_MODEL_IDS)]
        suffix = "" if index < len(STUB_MODEL_IDS) else f"-{index}"
        model_id = f"{model_id}{suffix}"
        targets.append(
            LocalStubTarget(model_ref=f"{STUB_PROVIDER}:{model_id}", model_id=model_id)
        )
    return targets


def _stub_format(value: float) -> str:
    if float(value).is_integer():
        return str(int(value))
    return f"{value:.6f}".rstrip("0").rstrip(".")


def stub_answer(user_text: str, *, model_id: str | None = None) -> str:
    """桩的「作答」：只做最基础的算术反射，其余返回桩的固定哨兵值。

    设计原则：**宁可答不出，也不猜**。桩不模仿任何真实模型的行为，
    其准确率应被如实理解为「本地桩的能力」，不得作为实验结果引用。

    桩 b（``stub_targets(count=2)[1]``）恒答 ``neutral``（与预置 ``wp11-eval-stub-b``
    的口径一致），使 T3 的两模型对照有可解释的差异，而不是两个完全相同的桩。
    """
    text = str(user_text or "")
    fallback = "neutral" if str(model_id or "").endswith("-b") else STUB_FALLBACK_ANSWER
    numbers = [float(item) for item in _STUB_NUMBER.findall(text)]
    operators = [char for char in text if char in _STUB_OPS]
    if len(operators) == 1 and len(numbers) >= 2:
        op = _STUB_OPS[operators[0]]
        value = numbers[0]
        try:
            for number in numbers[1:]:
                if op in (_STUB_OPS["÷"], _STUB_OPS["/"]):
                    if number == 0:
                        return fallback
                    value = value / number
                else:
                    value = op(value, number)
        except (ZeroDivisionError, OverflowError):
            return fallback
        return _stub_format(value)
    return fallback


def _message_content(messages: Sequence[Mapping[str, Any]], *, role: str) -> str:
    """取指定 role 的最后一条消息正文（回放/桩的输入口径）。"""
    for message in reversed(list(messages)):
        if str(message.get("role") or "") == role:
            return str(message.get("content") or "")
    return ""


# --------------------------------------------------------------------------- #
# 出网闸门
# --------------------------------------------------------------------------- #
async def prepare_targets(
    ctx: RunContext,
    explicit_refs: Sequence[str],
    *,
    count: int = 1,
) -> list[Any]:
    """解析模型目标并**逐个通过出网白名单**（模板内所有 LLM 调用的前置闸门）。

    - 显式 ``model_ref``：走 ``router.resolve_explicit``（解析不到即如实失败）
    - 未显式：走 ``router.resolve_chain('experiment', project_id)`` 取前 ``count`` 个
    - 回放模式（``ctx.replay_only``）：模型可能不可用，但**不做任何出网**，
      故只记录 ``egress`` 结论不阻断
    - 无 API Key 的目标（``api_key_configured=false``）视为不可调用并剔除（不发起注定 401 的请求）
    - 无任何可调用模型且 :func:`local_stub_enabled` 时：回落本地桩
      （``ctx.degrade`` 明确标注，禁止被读成实时实验结果）
    """
    from app.llm.router import get_router

    router = get_router()
    targets: list[Any] = []
    errors: list[str] = []
    if explicit_refs:
        for ref in explicit_refs:
            try:
                targets.append(await router.resolve_explicit(str(ref)))
            except Exception as exc:  # noqa: BLE001 - 解析失败即不可用
                errors.append(f"{ref}: {type(exc).__name__}: {exc}")
    else:
        chain = await router.resolve_chain(ctx.stage, ctx.project_id)
        targets = list(chain[:count])
        if len(targets) < count:
            errors.append(
                f"stage='{ctx.stage}' 路由链只有 {len(targets)} 个可用模型，需要 {count} 个"
            )

    # 无 API Key 的目标不可调用：提前剔除（否则只会拿到 401），并在降级说明中留痕
    usable: list[Any] = []
    for target in targets:
        if bool(getattr(target, "is_stub", False)):
            usable.append(target)
            continue
        if not bool(getattr(target, "api_key_configured", False)):
            errors.append(
                f"{getattr(target, 'model_ref', '?')}: 未配置 API Key（api_key_configured=false，不可调用）"
            )
            continue
        usable.append(target)
    targets = usable

    if len(targets) < count:
        message = (
            f"模板 {ctx.template_id} 无法解析足够的模型（需要 {count} 个，得到 {len(targets)} 个）："
            + "；".join(errors or ["未配置任何模型路由"])
        )
        if ctx.replay_only:
            ctx.degrade(f"回放模式：模型不可用但不影响回放（{message}）")
            return targets
        if local_stub_enabled():
            ctx.degrade(
                "无真实 LLM 凭据：本次 Run 由**本地确定性桩**（provider=local-stub）作答，"
                f"不是实时模型输出，不得作为实验结果引用（{message}）"
            )
            logger.warning(
                "模板 %s 回落本地桩模板（无可用模型路由）：%s", ctx.template_id, message
            )
            return stub_targets(count)
        raise ModelUnavailableError(
            message,
            detail={"template_id": ctx.template_id, "required": count, "errors": errors},
        )

    policy: EgressPolicy = get_policy()
    hosts = {target.model_ref: host_of(target.base_url) for target in targets}
    granted: list[str] = []
    try:
        for ref, host in hosts.items():
            if not host:
                raise ModelUnavailableError(
                    f"模型 {ref} 的 base_url 缺失，无法做越权出网判定",
                    detail={"model_ref": ref, "base_url": getattr(targets[0], "base_url", None)},
                )
            granted.append(policy.require(host))
    except Exception as exc:  # noqa: BLE001 - 出网红线：回放之外一律不得放行
        if ctx.replay_only:
            ctx.degrade(f"回放模式：出网预检未通过但不影响回放（{type(exc).__name__}: {exc}）")
            return targets
        if local_stub_enabled():
            # 关键：**不绕过白名单**——被拒的域名一律不访问；只是不再尝试该模型，
            # 改用不产生任何网络请求的进程内桩，并在降级说明中写明被拒原因。
            ctx.degrade(
                "配置的实验模型出网被拒/不可达（本 Run 未访问该域名，白名单未被放宽）："
                f"改用**进程内**本地确定性桩（provider={STUB_PROVIDER}），"
                f"不是实时模型输出，不得作为实验结果引用；原因：{type(exc).__name__}: {exc}"
            )
            logger.warning("实验模型出网被拒，回落进程内桩：%s", exc)
            return stub_targets(len(targets) or count)
        raise
    return targets


def egress_report(targets: Sequence[Any]) -> dict[str, Any]:
    """本次 Run 出网结论（写入指标的 extra，便于审计）。"""
    policy = get_policy()
    return {
        "policy": policy.name,
        "whitelist": list(policy.allowed_hosts),
        "deny_private_network": policy.deny_private_network,
        "local_stub_targets": [
            getattr(target, "model_ref", "?")
            for target in targets
            if bool(getattr(target, "is_stub", False))
        ],
        "targets": {
            getattr(target, "model_ref", "?"): host_of(getattr(target, "base_url", ""))
            for target in targets
        },
    }


# --------------------------------------------------------------------------- #
# 调用（实时 / 回放二选一）
# --------------------------------------------------------------------------- #
async def call_model(
    ctx: RunContext,
    target: Any,
    messages: list[dict[str, str]],
    *,
    group: str,
    sample_id: str,
    temperature: float | None = None,
    max_tokens: int | None = None,
) -> dict[str, Any]:
    """调用模型（或从回放源取回放输出），返回**逐样本记录**。

    返回字段：``prediction / raw / model_ref / provider / model_id / cost_usd /
    duration_ms / is_replay / is_stub / source / attempts / fallback_from / prompt_hash / error``。
    """
    key = fixture_key(group, sample_id)
    recorded = ctx.fixture_for(key)
    if recorded is not None:
        return await _replay_record(ctx, target, recorded, key=key, group=group, sample_id=sample_id)
    if bool(getattr(target, "is_stub", False)):
        return await _stub_record(
            ctx,
            target,
            messages,
            key=key,
            group=group,
            sample_id=sample_id,
        )

    from app.llm.adapter import chat

    started = time.perf_counter()
    result = await chat(
        messages,
        str(getattr(target, "model_ref", "") or "") or None,
        temperature,
        max_tokens,
        None,
        project_id=ctx.project_id,
        stage=ctx.stage,
        purpose=ctx.purpose(group),
        allow_replay=None,
        metadata={"template_id": ctx.template_id, "run_id": ctx.run_id, "sample_id": sample_id},
    )
    wall_ms = int((time.perf_counter() - started) * 1000)
    return {
        "sample_id": str(sample_id),
        "group": group,
        "prediction": str(result.content or ""),
        "model_ref": result.model_ref,
        "provider": result.provider,
        "model_id": result.model_id,
        "cost_usd": float(result.cost_usd) if result.cost_usd is not None else None,
        "cost_unknown_reason": result.cost_unknown_reason,
        "duration_ms": int(result.duration_ms or wall_ms),
        "wall_ms": wall_ms,
        "attempts": int(result.attempts or 1),
        "http_calls": int(result.http_calls or 1),
        "is_replay": bool(result.is_replay),
        "source": "live" if not result.is_replay else "llm_replay_fixture",
        "resolved_from": result.resolved_from,
        "fallback_from": list(result.fallback_from or []),
        "prompt_hash": result.prompt_hash,
        "prompt_tokens": result.usage.prompt_tokens,
        "completion_tokens": result.usage.completion_tokens,
        "error": None,
    }


async def _replay_record(
    ctx: RunContext,
    target: Any,
    recorded: Any,
    *,
    key: str,
    group: str,
    sample_id: str,
) -> dict[str, Any]:
    """从回放源构造逐样本记录，并补写一行 ``llm_call_logs(is_replay=true)``。"""
    payload = dict(recorded) if isinstance(recorded, Mapping) else {"prediction": str(recorded)}
    prediction = str(payload.get("prediction") or payload.get("raw") or "")
    if not prediction:
        raise ReplaySourceMissError(
            f"回放源命中 {key} 但没有可用的原始输出（prediction 为空）",
            detail={"fixture_key": key, "sample_id": sample_id},
        )
    model_ref = str(payload.get("model_ref") or getattr(target, "model_ref", "") or "")
    provider, _, model_id = model_ref.partition(":")
    cost_usd = payload.get("cost_usd")
    duration_ms = payload.get("duration_ms")
    await _log_replay_call(
        ctx,
        model_ref=model_ref,
        provider=provider or str(getattr(target, "provider", "") or "replay"),
        model_id=model_id or str(getattr(target, "model_id", "") or "replay"),
        purpose=ctx.purpose(group),
        cost_usd=cost_usd if isinstance(cost_usd, (int, float)) else None,
        duration_ms=duration_ms if isinstance(duration_ms, (int, float)) else None,
        note=f"replay_source={payload.get('source') or 'parent_artifact'}",
    )
    return {
        "sample_id": str(sample_id),
        "group": group,
        "prediction": prediction,
        "model_ref": model_ref,
        "provider": provider or getattr(target, "provider", None),
        "model_id": model_id or getattr(target, "model_id", None),
        "cost_usd": float(cost_usd) if isinstance(cost_usd, (int, float)) else None,
        "cost_unknown_reason": payload.get("cost_unknown_reason"),
        "duration_ms": int(duration_ms) if isinstance(duration_ms, (int, float)) else 0,
        "wall_ms": 0,
        "attempts": 1,
        "http_calls": 0,
        "is_replay": True,
        "source": str(payload.get("source") or "parent_artifact_fixture"),
        "resolved_from": "replay_fixture",
        "fallback_from": [],
        "prompt_hash": payload.get("prompt_hash"),
        "prompt_tokens": payload.get("prompt_tokens"),
        "completion_tokens": payload.get("completion_tokens"),
        "replay_fixture_key": key,
        "error": None,
    }


async def _log_replay_call(
    ctx: RunContext,
    *,
    model_ref: str,
    provider: str,
    model_id: str,
    purpose: str,
    cost_usd: float | None,
    duration_ms: float | None,
    note: str,
) -> None:
    """回放也留痕：写 ``llm_call_logs(is_replay=true)``（不产生真实花费）。"""
    try:
        from app.llm.store import get_store
        from app.llm.types import CallLogEntry

        entry = CallLogEntry(
            provider=provider or "replay",
            model=model_id or "replay",
            success=True,
            project_id=ctx.project_id,
            stage=ctx.stage,
            purpose=purpose,
            cost_usd=cost_usd,
            duration_ms=int(duration_ms) if isinstance(duration_ms, (int, float)) else None,
            is_replay=True,
            error=note,
            model_ref=model_ref or None,
        )
        await get_store().insert_call_log(entry)
    except Exception as exc:  # noqa: BLE001 - 留痕失败不得掩盖回放本身
        logger.warning("回放调用留痕失败（不影响回放结果）：%s", exc, exc_info=True)


async def _stub_record(
    ctx: RunContext,
    target: Any,
    messages: list[dict[str, str]],
    *,
    key: str,
    group: str,
    sample_id: str,
) -> dict[str, Any]:
    """本地桩的逐样本记录（**明确标注 is_stub / source=local_stub**）。

    桩不发起任何出网、不产生任何真实花费（``cost_usd=0.0`` 是事实而非估算），
    同时补写一行 ``llm_call_logs(provider='local-stub')`` 以便审计与防伪。
    """
    started = time.perf_counter()
    user_text = _message_content(messages, role="user")
    model_id = str(getattr(target, "model_id", STUB_MODEL_IDS[0]))
    prediction = stub_answer(user_text, model_id=model_id)
    wall_ms = max(1, int((time.perf_counter() - started) * 1000))
    model_ref = str(getattr(target, "model_ref", f"{STUB_PROVIDER}:{STUB_MODEL_IDS[0]}"))
    provider = str(getattr(target, "provider", STUB_PROVIDER))
    prompt_hash = sha256_hex(canonical_json([dict(message) for message in messages]))
    note = (
        "local_stub: 无真实 LLM 凭据，答案由本地确定性桩（stub_answer 极简规则）生成；"
        "cost_usd=0.0（无出网、无计费），不得作为实时实验结果引用"
    )
    ctx.degrade(note)
    await _log_stub_call(
        ctx,
        model_ref=model_ref,
        provider=provider,
        model_id=model_id,
        purpose=ctx.purpose(group),
        duration_ms=wall_ms,
        note=note,
    )
    return {
        "sample_id": str(sample_id),
        "group": group,
        "prediction": prediction,
        "model_ref": model_ref,
        "provider": provider,
        "model_id": model_id,
        "cost_usd": 0.0,
        "cost_unknown_reason": None,
        "duration_ms": wall_ms,
        "wall_ms": wall_ms,
        "attempts": 1,
        "http_calls": 0,
        "is_replay": False,
        "is_stub": True,
        "source": "local_stub",
        "resolved_from": "local_stub",
        "fallback_from": [],
        "prompt_hash": prompt_hash,
        "prompt_tokens": None,
        "completion_tokens": None,
        "stub_fixture_key": key,
        "error": None,
    }


async def _log_stub_call(
    ctx: RunContext,
    *,
    model_ref: str,
    provider: str,
    model_id: str,
    purpose: str,
    duration_ms: int | None,
    note: str,
) -> None:
    """桩调用留痕：``is_replay=false``、``cost_usd=0.0``、provider/model 自证为桩。"""
    try:
        from app.llm.store import get_store
        from app.llm.types import CallLogEntry

        entry = CallLogEntry(
            provider=provider or STUB_PROVIDER,
            model=model_id or STUB_MODEL_IDS[0],
            success=True,
            project_id=ctx.project_id,
            stage=ctx.stage,
            purpose=purpose,
            cost_usd=0.0,
            duration_ms=duration_ms,
            is_replay=False,
            error=note,
            model_ref=model_ref or None,
        )
        await get_store().insert_call_log(entry)
    except Exception as exc:  # noqa: BLE001 - 留痕失败不得掩盖桩产出
        logger.warning("桩调用留痕失败（不影响桩结果）：%s", exc, exc_info=True)


def sample_report(
    records: Sequence[Mapping[str, Any]],
    *,
    group_key: str = "group",
    prediction_key: str = "prediction",
) -> dict[str, Any]:
    """按 group 汇总逐样本记录（供模板计算指标；不做任何填补）。"""
    grouped: dict[str, list[dict[str, Any]]] = {}
    for record in records:
        grouped.setdefault(str(record.get(group_key)), []).append(dict(record))
    return grouped


__all__ = [
    "DATA_DIR",
    "KEY_SEP",
    "LOCAL_STUB_ENV",
    "STUB_FALLBACK_ANSWER",
    "STUB_MODEL_IDS",
    "STUB_PROVIDER",
    "TASK_HINTS",
    "LocalStubTarget",
    "build_messages",
    "call_model",
    "egress_report",
    "fixture_key",
    "load_prompt_asset",
    "local_stub_enabled",
    "prepare_targets",
    "prompt_digest_payload",
    "render",
    "sample_report",
    "stub_answer",
    "stub_targets",
    "task_hint",
]
