# Copyright 2026 SciLoop contributors
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
"""三类硬护栏（WP10-T1，计划书 §2.4.2 / contracts.guardrails）。

判定顺序 **固定为 safety → time → cost**，且**短路求值**：任一失败立即熔断，
后续护栏不再评估（:func:`check_guardrails` 的 ``evaluated`` / ``skipped`` 字段是短路证据）。

红线：

- 阈值来自配置（``PIPELINE_MAX_LLM_COST_USD`` / ``EXECUTOR_RUN_TIMEOUT_SECONDS`` 等），
  **不接受调用方（更不接受 LLM）覆盖**：``action_plan`` 里同名键只作为**被校验的对象**，
  不能放宽上限；一旦 action_plan 请求放宽，视为违规而不是放宽阈值。
- 演示配额（默认 3.0 USD）**只告警不熔断**（计划书 §2.4.2）。
- 成本累计直接复用 WP02 的 :func:`services.cost.check_cost`，本模块不重复实现累计。

``action_plan`` 是「待执行动作的声明」，不是「护栏参数」。可用键：

===================  ====================================================
键                   含义
===================  ====================================================
decision_point       D1–D6（决定需要校验哪些必填动作字段）
stage                环节名（survey/plan/plan_review/experiment/writing/review）
template_id          实验模板 ID（D3 必填，必须在白名单内）
params               模板参数（JSON 可序列化 dict；做 schema 与危险模式扫描）
sample_size          样本量（D3 必填，1..50）
estimated_cost_usd   本次动作预估成本（与累计成本相加后比护栏值）
run_timeout_seconds  单 Run 超时（<=300）
stage_timeout_seconds 单环节超时（<=1200）
egress_hosts         需要出网的域名列表（必须在白名单内且不得为内网）
===================  ====================================================
"""

from __future__ import annotations

import ipaddress
import logging
import os
import re
from collections import Counter
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any
from urllib.parse import urlparse

logger = logging.getLogger("sciloop.pipeline.guardrails")

WP_ID = "WP10"

#: 护栏版本（写入 decision_logs.policy_version 的护栏部分，变更需递增）
GUARDRAIL_VERSION = "wp10-guardrails-v1.0.0"

#: 判定顺序（contracts.guardrails.judgement_order），禁止改动顺序
JUDGEMENT_ORDER: tuple[str, ...] = ("safety", "time", "cost")

#: 模板白名单（contracts.enums.template_id）
TEMPLATE_WHITELIST: tuple[str, ...] = (
    "T1_prompt_variant",
    "T2_fewshot_ablation",
    "T3_model_compare",
    "T4_llm_as_judge",
    "T5_rag_ablation",
)

#: D3 必填动作字段（其余决策点按域内字段可选校验）
REQUIRED_FIELDS_BY_DECISION_POINT: Mapping[str, tuple[str, ...]] = {
    "D3": ("template_id", "params", "sample_size"),
}

SAMPLE_SIZE_MIN = 1
SAMPLE_SIZE_MAX = 50  # contracts.guardrails.safety

#: 契约默认值（配置不可用时兜底；两者层级差必须保持 run < stage）
DEFAULT_RUN_TIMEOUT_SECONDS = 300
DEFAULT_STAGE_TIMEOUT_SECONDS = 1200

#: 危险模式（任意代码执行 / 运行时安装依赖 / 宿主文件系统 / 内网）
FORBIDDEN_PATTERNS: tuple[tuple[str, re.Pattern[str]], ...] = (
    (
        "shell_exec",
        re.compile(r"\b(subprocess|os\.system|os\.popen|pty\.spawn|commands\.getoutput)\b"),
    ),
    ("shell_flag", re.compile(r"shell\s*=\s*true", re.IGNORECASE)),
    (
        "shell_invocation",
        re.compile(r"\b(bash|sh|zsh|cmd|powershell|pwsh)\s+(-c|/c|-Command)\b", re.I),
    ),
    ("dynamic_eval", re.compile(r"\b(eval|exec|compile|__import__)\s*\(")),
    ("builtins_access", re.compile(r"__builtins__|__subclasses__|__globals__")),
    (
        "runtime_install",
        re.compile(r"\b(pip3?|apt-get|apt|conda|poetry|uv)\s+(install|add)\b", re.I),
    ),
    ("runtime_install_npm", re.compile(r"\bnpm\s+(install|i|add)\b", re.I)),
    ("dangerous_rm", re.compile(r"\brm\s+-[a-zA-Z]*r[a-zA-Z]*f|Remove-Item\s+-Recurse", re.I)),
    ("curl_pipe_shell", re.compile(r"\b(curl|wget)\b[^\n|]*\|\s*(bash|sh)\b", re.I)),
)

#: 宿主/系统路径与内网探测目标
HOST_PATH_PATTERNS: tuple[tuple[str, re.Pattern[str]], ...] = (
    ("file_scheme", re.compile(r"\bfile://")),
    ("unix_system_path", re.compile(r"(^|[\s'\"(=])/(etc|proc|sys|root|dev|boot|var/lib)/")),
    ("windows_drive", re.compile(r"\b[A-Za-z]:[\\/]")),
    ("unc_path", re.compile(r"\\\\[A-Za-z0-9_.$-]+\\")),
    ("home_ssh", re.compile(r"~/\.(ssh|aws|config)|\.ssh/")),
    ("metadata_endpoint", re.compile(r"169\.254\.169\.254|metadata\.google\.internal")),
)

#: 出网白名单：仅这些「配置来源」可被放宽（禁止硬编码第三方域名）
_EGRESS_EXTRA_ENV = "GUARDRAIL_EGRESS_EXTRA_HOSTS"


# --------------------------------------------------------------------------- #
# 结果结构
# --------------------------------------------------------------------------- #
@dataclass
class GuardrailItem:
    """单条护栏检查项（可审计：判据、期望、实际、来源）。"""

    name: str
    ok: bool | None
    expected: Any = None
    actual: Any = None
    source: str | None = None
    note: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "ok": self.ok,
            "expected": self.expected,
            "actual": self.actual,
            "source": self.source,
            "note": self.note,
        }


@dataclass
class _Counter:
    """护栏评估计数器（短路求值的可验证证据）。"""

    hits: Counter[str] = field(default_factory=Counter)
    last_run: dict[str, int] = field(default_factory=dict)

    def reset(self) -> None:
        self.last_run = dict.fromkeys(JUDGEMENT_ORDER, 0)

    def hit(self, name: str) -> None:
        self.hits[name] += 1
        self.last_run[name] = self.last_run.get(name, 0) + 1


_COUNTER = _Counter()


def evaluation_counters() -> dict[str, Any]:
    """返回护栏评估计数：``last_run`` 可证明某项护栏是否真的被求值过。"""
    return {"last_run": dict(_COUNTER.last_run), "total": dict(_COUNTER.hits)}


def reset_evaluation_counters() -> None:
    _COUNTER.hits.clear()
    _COUNTER.reset()


# --------------------------------------------------------------------------- #
# 阈值（只来自配置，不接受调用方覆盖）
# --------------------------------------------------------------------------- #
def _settings() -> Any:
    try:
        from core.config import get_settings

        return get_settings()
    except Exception:  # noqa: BLE001 - 配置不可用时用契约默认值
        logger.warning("读取配置失败，护栏使用契约默认阈值", exc_info=True)
        return None


def guardrail_limits() -> dict[str, Any]:
    """当前生效的护栏阈值（UI 的「成本双线」与审计展示用）。"""
    settings = _settings()
    limit, quota = 8.0, 3.0
    run_timeout, stage_timeout = DEFAULT_RUN_TIMEOUT_SECONDS, DEFAULT_STAGE_TIMEOUT_SECONDS
    if settings is not None:
        try:
            from services.cost import default_limits

            limit, quota = default_limits()
        except Exception:  # noqa: BLE001
            limit = float(getattr(settings, "pipeline_max_llm_cost_usd", limit))
            quota = float(getattr(settings, "pipeline_demo_cost_quota_usd", quota))
        run_timeout = int(
            getattr(settings, "executor_run_timeout_seconds", DEFAULT_RUN_TIMEOUT_SECONDS)
        )
        stage_minutes = int(getattr(settings, "pipeline_max_stage_minutes", 20))
        stage_timeout = max(stage_minutes * 60, run_timeout + 1)
    return {
        "version": GUARDRAIL_VERSION,
        "run_timeout_seconds": run_timeout,
        "stage_timeout_seconds": stage_timeout,
        "cost_hard_limit_usd": round(float(limit), 4),
        "cost_demo_quota_usd": round(float(quota), 4),
        "sample_size_max": SAMPLE_SIZE_MAX,
        "template_whitelist": list(TEMPLATE_WHITELIST),
        "judgement_order": list(JUDGEMENT_ORDER),
        "thresholds_overridable": False,
        "demo_quota_is_warning_only": True,
    }


def egress_whitelist() -> tuple[str, ...]:
    """出网白名单：来自**配置**的 LLM 域名 + 执行器白名单 + 显式补充项。"""
    hosts: list[str] = []
    settings = _settings()
    if settings is not None:
        for key in ("llm_default_base_url", "llm_fallback_base_url"):
            host = _host_of(str(getattr(settings, key, "") or ""))
            if host:
                hosts.append(host)
        for host in getattr(settings, "allowed_hosts", []) or []:
            hosts.append(str(host).strip().lower())
    extra = os.environ.get(_EGRESS_EXTRA_ENV, "") if settings is not None else ""
    for host in extra.split(","):
        if host.strip():
            hosts.append(host.strip().lower())
    return tuple(sorted({h for h in hosts if h}))


def _host_of(url: str) -> str:
    text = (url or "").strip()
    if not text:
        return ""
    if "://" not in text:
        text = f"//{text}"
    try:
        return (urlparse(text).hostname or "").lower()
    except ValueError:
        return ""


# --------------------------------------------------------------------------- #
# 通用工具
# --------------------------------------------------------------------------- #
def _as_mapping(value: Any) -> dict[str, Any]:
    return dict(value) if isinstance(value, Mapping) else {}


def _walk_strings(value: Any, *, path: str = "") -> Iterable[tuple[str, str]]:
    """递归产出 ``(路径, 字符串)``；只用于危险模式扫描。"""
    if isinstance(value, str):
        yield path or "<str>", value
    elif isinstance(value, Mapping):
        for key, item in value.items():
            yield from _walk_strings(item, path=f"{path}.{key}" if path else str(key))
    elif isinstance(value, Sequence) and not isinstance(value, (bytes, bytearray)):
        for index, item in enumerate(value):
            yield from _walk_strings(item, path=f"{path}[{index}]")


def _is_private_host(host: str) -> bool:
    """字面量内网/回环 IP 或本地别名（不做 DNS 解析，避免网络依赖）。"""
    text = (host or "").strip().lower().strip("[]")
    if not text:
        return False
    if text in {"localhost", "localhost.localdomain"} or text.endswith((".local", ".internal")):
        return True
    try:
        address = ipaddress.ip_address(text)
    except ValueError:
        return False
    return bool(
        address.is_private or address.is_loopback or address.is_link_local or address.is_reserved
    )


def _schema_violation(value: Any, expected: str) -> bool:
    if expected == "str":
        return not isinstance(value, str)
    if expected == "int":
        return not isinstance(value, int) or isinstance(value, bool)
    if expected == "number":
        return not isinstance(value, (int, float)) or isinstance(value, bool)
    if expected == "list[str]":
        return not (isinstance(value, list) and all(isinstance(i, str) for i in value))
    if expected == "dict":
        return not isinstance(value, Mapping)
    return False


# --------------------------------------------------------------------------- #
# ① 安全护栏
# --------------------------------------------------------------------------- #
def check_safety(action_plan: Mapping[str, Any] | None) -> dict[str, Any]:
    """安全护栏（最先判）：模板白名单 / 参数 schema / sample_size / 无任意代码 / 出网白名单。"""
    plan = _as_mapping(action_plan)
    decision_point = str(plan.get("decision_point") or "").strip().upper()
    items: list[GuardrailItem] = []

    # 1) 模板白名单
    template_id = plan.get("template_id")
    if template_id is None or str(template_id).strip() == "":
        required = bool(REQUIRED_FIELDS_BY_DECISION_POINT.get(decision_point))
        items.append(
            GuardrailItem(
                name="template_whitelist",
                ok=not required,
                expected={"whitelist": list(TEMPLATE_WHITELIST), "required": required},
                actual=None,
                source="contracts.enums.template_id",
                note="未声明 template_id"
                + ("（该决策点必填）" if required else "（非实验动作，跳过）"),
            )
        )
    else:
        normalized = str(template_id).strip()
        items.append(
            GuardrailItem(
                name="template_whitelist",
                ok=normalized in TEMPLATE_WHITELIST,
                expected=list(TEMPLATE_WHITELIST),
                actual=normalized,
                source="contracts.enums.template_id",
                note=(
                    None if normalized in TEMPLATE_WHITELIST else "模板不在注册表白名单内，禁止执行"
                ),
            )
        )

    # 2) 参数 schema（必填 + 类型）
    required_fields = REQUIRED_FIELDS_BY_DECISION_POINT.get(decision_point, ())
    missing = [key for key in required_fields if plan.get(key) in (None, "")]
    type_errors: list[dict[str, Any]] = []
    schema_expectations = {
        "template_id": "str",
        "params": "dict",
        "sample_size": "int",
        "run_timeout_seconds": "number",
        "stage_timeout_seconds": "number",
        "egress_hosts": "list[str]",
    }
    for key, expected in schema_expectations.items():
        if key in plan and plan[key] is not None and _schema_violation(plan[key], expected):
            type_errors.append(
                {"field": key, "expected": expected, "actual": type(plan[key]).__name__}
            )
    schema_ok = not missing and not type_errors
    items.append(
        GuardrailItem(
            name="param_schema",
            ok=schema_ok,
            expected={"required": list(required_fields), "types": schema_expectations},
            actual={"missing": missing, "type_errors": type_errors},
            source="contracts.guardrails.safety",
            note="缺少必填字段或字段类型不符" if not schema_ok else None,
        )
    )

    # 3) sample_size <= 50
    sample_size = plan.get("sample_size")
    required_sample = "sample_size" in required_fields
    if sample_size is None and not required_sample:
        items.append(
            GuardrailItem(
                name="sample_size_limit",
                ok=True,
                expected={"max": SAMPLE_SIZE_MAX, "required": False},
                actual=None,
                source="contracts.guardrails.safety",
                note="本次动作未声明样本量（非实验动作）",
            )
        )
    elif not isinstance(sample_size, int) or isinstance(sample_size, bool):
        items.append(
            GuardrailItem(
                name="sample_size_limit",
                ok=False,
                expected={"max": SAMPLE_SIZE_MAX, "required": required_sample},
                actual=sample_size,
                source="contracts.guardrails.safety",
                note="样本量必须是整数",
            )
        )
    else:
        ok = SAMPLE_SIZE_MIN <= sample_size <= SAMPLE_SIZE_MAX
        items.append(
            GuardrailItem(
                name="sample_size_limit",
                ok=ok,
                expected={"min": SAMPLE_SIZE_MIN, "max": SAMPLE_SIZE_MAX},
                actual=sample_size,
                source="contracts.guardrails.safety",
                note=None if ok else f"样本量 {sample_size} 超出 1..{SAMPLE_SIZE_MAX}",
            )
        )

    # 4) 无任意代码 / 无运行时安装依赖 / 无宿主路径
    scanned_keys = ("params", "code", "command", "script", "args", "shell", "entrypoint", "env")
    probe: dict[str, Any] = {key: plan[key] for key in scanned_keys if key in plan}
    violations: list[dict[str, Any]] = []
    for path, text in _walk_strings(probe):
        for name, pattern in FORBIDDEN_PATTERNS:
            if pattern.search(text):
                violations.append({"rule": name, "path": path, "pattern": pattern.pattern})
        for name, pattern in HOST_PATH_PATTERNS:
            if pattern.search(text):
                violations.append({"rule": name, "path": path, "pattern": pattern.pattern})
    if plan.get("arbitrary_code") or plan.get("allow_arbitrary_code"):
        violations.append(
            {"rule": "arbitrary_code_flag", "path": "<action_plan>", "pattern": "flag"}
        )
    items.append(
        GuardrailItem(
            name="no_arbitrary_code",
            ok=not violations,
            expected="禁止任意 shell/eval/exec、禁止运行时安装依赖、禁止宿主文件系统路径",
            actual=violations,
            source="contracts.guardrails.safety",
            note="命中危险模式，安全护栏失败" if violations else None,
        )
    )

    # 5) 出网白名单 + 禁止内网
    whitelist = set(egress_whitelist())
    requested = plan.get("egress_hosts") or plan.get("allowed_hosts") or []
    if isinstance(requested, str):
        requested = [requested]
    requested_hosts = [_host_of(str(item)) or str(item).strip().lower() for item in requested]
    requested_hosts = [host for host in requested_hosts if host]
    egress_violations: list[dict[str, Any]] = []
    for host in requested_hosts:
        if host not in whitelist:
            egress_violations.append({"host": host, "reason": "not_in_egress_whitelist"})
        elif _is_private_host(host):
            egress_violations.append({"host": host, "reason": "private_network_denied"})
    items.append(
        GuardrailItem(
            name="egress_whitelist",
            ok=not egress_violations,
            expected={"whitelist": sorted(whitelist)},
            actual={"requested": requested_hosts, "violations": egress_violations},
            source="EXECUTOR_ALLOWED_HOSTS / LLM_DEFAULT_BASE_URL / LLM_FALLBACK_BASE_URL",
            note="出网目标不在白名单或属内网" if egress_violations else None,
        )
    )

    failed = [item.name for item in items if item.ok is False]
    return {
        "ok": not failed,
        "items": [item.to_dict() for item in items],
        "failed_checks": failed,
        "reason": None if not failed else f"safety_violation:{','.join(failed)}",
        "whitelist": {
            "templates": list(TEMPLATE_WHITELIST),
            "egress_hosts": sorted(whitelist),
            "sample_size_max": SAMPLE_SIZE_MAX,
        },
    }


# --------------------------------------------------------------------------- #
# ② 时长护栏
# --------------------------------------------------------------------------- #
def check_time(action_plan: Mapping[str, Any] | None = None) -> dict[str, Any]:
    """时长护栏：单 Run <=300s、单环节 <=1200s，且必须保持层级差（run < stage，不得相等）。"""
    plan = _as_mapping(action_plan)
    limits = guardrail_limits()
    max_run = int(limits["run_timeout_seconds"])
    max_stage = int(limits["stage_timeout_seconds"])

    requested_run = plan.get("run_timeout_seconds")
    requested_stage = plan.get("stage_timeout_seconds")
    run_timeout = int(requested_run) if isinstance(requested_run, (int, float)) else max_run
    stage_timeout = int(requested_stage) if isinstance(requested_stage, (int, float)) else max_stage

    items = [
        GuardrailItem(
            name="run_timeout",
            ok=run_timeout <= max_run and run_timeout > 0,
            expected={"max": max_run},
            actual=run_timeout,
            source="EXECUTOR_RUN_TIMEOUT_SECONDS",
            note=None if run_timeout <= max_run else "单 Run 超时超过上限（阈值不可放宽）",
        ),
        GuardrailItem(
            name="stage_timeout",
            ok=stage_timeout <= max_stage and stage_timeout > 0,
            expected={"max": max_stage},
            actual=stage_timeout,
            source="PIPELINE_MAX_STAGE_MINUTES×60",
            note=None if stage_timeout <= max_stage else "单环节超时超过上限（阈值不可放宽）",
        ),
        GuardrailItem(
            name="timeout_hierarchy",
            ok=run_timeout < stage_timeout,
            expected="run_timeout < stage_timeout（层级差不得相等）",
            actual={"run_timeout_seconds": run_timeout, "stage_timeout_seconds": stage_timeout},
            source="contracts.guardrails.time.rule / 计划书 §2.4.2",
            note=(
                None
                if run_timeout < stage_timeout
                else "层级差被破坏：单 Run 与单环节边界相等或倒置"
            ),
        ),
    ]
    failed = [item.name for item in items if item.ok is False]
    return {
        "ok": not failed,
        "items": [item.to_dict() for item in items],
        "failed_checks": failed,
        "reason": None if not failed else f"time_violation:{','.join(failed)}",
        "limits": {
            "run_timeout_seconds": run_timeout,
            "stage_timeout_seconds": stage_timeout,
            "max_run_timeout_seconds": max_run,
            "max_stage_timeout_seconds": max_stage,
        },
    }


# --------------------------------------------------------------------------- #
# ③ 成本护栏
# --------------------------------------------------------------------------- #
async def check_cost(project_id: int | None, *, estimated_usd: float = 0.0) -> dict[str, Any]:
    """成本护栏：累计 + 本次预估 <= 硬线（默认 8.0）；演示配额只告警不熔断。

    直接调用 WP02 的 :func:`services.cost.check_cost`，不重复实现累计口径。
    """
    if project_id is None:
        return {
            "ok": None,
            "evaluated": False,
            "items": [],
            "failed_checks": [],
            "reason": "skipped:project_id_required",
            "note": "未提供 project_id，无法读取真实累计成本；不编造成本数据（该护栏不参与判定）",
        }
    try:
        from services.cost import check_cost as _wp02_check_cost
    except Exception as exc:  # noqa: BLE001 - 记账层缺失时不得伪造 ok
        logger.warning("成本护栏依赖不可用：%s", exc, exc_info=True)
        return {
            "ok": False,
            "evaluated": True,
            "items": [],
            "failed_checks": ["cost_backend_unavailable"],
            "reason": f"cost_backend_unavailable:{type(exc).__name__}",
            "note": "成本累计服务不可用 → 宁可不放行（禁止用 0 冒充成本）",
        }

    result = await _wp02_check_cost(project_id, estimated_usd=max(0.0, float(estimated_usd or 0.0)))
    payload = result.to_dict()
    items = [
        GuardrailItem(
            name="cost_hard_limit",
            ok=bool(result.ok),
            expected={"projected_usd<=": result.limit_usd},
            actual={"used_usd": result.used_usd, "estimated_usd": result.estimated_usd},
            source="PIPELINE_MAX_LLM_COST_USD / llm_call_logs(累计)",
            note=None if result.ok else (result.reason or "累计成本 + 本次预估已超硬线"),
        ),
        GuardrailItem(
            name="cost_demo_quota",
            ok=True,  # 配额只告警
            expected={"quota_usd": result.quota_usd, "warning_only": True},
            actual={"used_usd": result.used_usd, "quota_exceeded": result.quota_exceeded},
            source="PIPELINE_DEMO_COST_QUOTA_USD",
            note="已超演示配额（仅告警，不熔断）" if result.quota_exceeded else None,
        ),
    ]
    return {
        "ok": bool(result.ok),
        "evaluated": True,
        "items": [item.to_dict() for item in items],
        "failed_checks": [] if result.ok else ["cost_hard_limit"],
        "reason": None if result.ok else (result.reason or "cost_limit_exceeded"),
        "check": payload,
        "warning": "demo_quota_exceeded" if result.quota_exceeded else None,
    }


# --------------------------------------------------------------------------- #
# 编排（短路求值）
# --------------------------------------------------------------------------- #
async def check_guardrails(
    action_plan: Mapping[str, Any] | None,
    *,
    project_id: int | None = None,
    estimated_cost_usd: float | None = None,
) -> dict[str, Any]:
    """按 safety → time → cost 顺序判定，任一失败立即熔断（后续不再评估）。

    返回 ``{safety_ok, time_ok, cost_ok, detail}``（契约字段）+ 审计字段：

    - ``evaluated`` / ``skipped``：短路证据（未评估项既不是 True 也不是 False，而是 ``None``）
    - ``counters``：本次实际求值次数（``cost`` 为 0 即证明未评估成本）
    - ``detail``：三项护栏的逐条判据与失败原因
    """
    plan = _as_mapping(action_plan)
    estimated = estimated_cost_usd
    if estimated is None:
        raw_estimate = plan.get("estimated_cost_usd")
        estimated = float(raw_estimate) if isinstance(raw_estimate, (int, float)) else 0.0

    _COUNTER.reset()
    evaluated: list[str] = []
    skipped: list[str] = []
    detail: dict[str, Any] = {"version": GUARDRAIL_VERSION, "action_plan_keys": sorted(plan.keys())}

    # ① safety（最先判）
    _COUNTER.hit("safety")
    evaluated.append("safety")
    safety = check_safety(plan)
    detail["safety"] = safety
    if not safety["ok"]:
        skipped.extend(["time", "cost"])
        detail["time"] = {"ok": None, "evaluated": False, "reason": "short_circuit:safety_failed"}
        detail["cost"] = {"ok": None, "evaluated": False, "reason": "short_circuit:safety_failed"}
        return _report(
            safety_ok=False,
            time_ok=None,
            cost_ok=None,
            detail=detail,
            evaluated=evaluated,
            skipped=skipped,
            first_failure="safety",
            reason=f"correlated_failure_started_at:safety｜{safety['reason']}",
            limits=guardrail_limits(),
        )

    # ② time
    _COUNTER.hit("time")
    evaluated.append("time")
    time_check = check_time(plan)
    detail["time"] = time_check
    if not time_check["ok"]:
        skipped.append("cost")
        detail["cost"] = {"ok": None, "evaluated": False, "reason": "short_circuit:time_failed"}
        return _report(
            safety_ok=True,
            time_ok=False,
            cost_ok=None,
            detail=detail,
            evaluated=evaluated,
            skipped=skipped,
            first_failure="time",
            reason=f"correlated_failure_started_at:time｜{time_check['reason']}",
            limits=guardrail_limits(),
        )

    # ③ cost（最后判；直接复用 WP02）
    _COUNTER.hit("cost")
    evaluated.append("cost")
    cost_check = await check_cost(project_id, estimated_usd=float(estimated or 0.0))
    detail["cost"] = cost_check
    if cost_check["ok"] is False:
        return _report(
            safety_ok=True,
            time_ok=True,
            cost_ok=False,
            detail=detail,
            evaluated=evaluated,
            skipped=[],
            first_failure="cost",
            reason=f"correlated_failure_started_at:cost｜{cost_check['reason']}",
            limits=guardrail_limits(),
        )

    return _report(
        safety_ok=True,
        time_ok=True,
        cost_ok=cost_check["ok"],
        detail=detail,
        evaluated=evaluated,
        skipped=[],
        first_failure=None,
        reason=None,
        limits=guardrail_limits(),
    )


def _report(
    *,
    safety_ok: bool | None,
    time_ok: bool | None,
    cost_ok: bool | None,
    detail: dict[str, Any],
    evaluated: list[str],
    skipped: list[str],
    first_failure: str | None,
    reason: str | None,
    limits: dict[str, Any],
) -> dict[str, Any]:
    ok = safety_ok is not False and time_ok is not False and cost_ok is not False
    return {
        "safety_ok": safety_ok,
        "time_ok": time_ok,
        "cost_ok": cost_ok,
        "ok": ok,
        "detail": detail,
        "evaluation_order": list(JUDGEMENT_ORDER),
        "evaluated": evaluated,
        "skipped": skipped,
        "short_circuited": bool(skipped),
        "first_failure": first_failure,
        "reason": reason,
        "version": GUARDRAIL_VERSION,
        "limits": limits,
        "counters": evaluation_counters()["last_run"],
    }


def failure_report_hint(report: Mapping[str, Any]) -> dict[str, Any]:
    """熔断时写入 decision_logs.guardrail_checks 的报告提示（不含任何推测数值）。"""
    return {
        "guardrail_version": str(report.get("version") or GUARDRAIL_VERSION),
        "first_failure": report.get("first_failure"),
        "reason": report.get("reason"),
        "evaluated": list(report.get("evaluated") or []),
        "skipped": list(report.get("skipped") or []),
        "short_circuited": bool(report.get("short_circuited")),
        "report_url_hint": "/api/v1/reports/{project_id}/failure",
    }


__all__ = [
    "GUARDRAIL_VERSION",
    "JUDGEMENT_ORDER",
    "SAMPLE_SIZE_MAX",
    "TEMPLATE_WHITELIST",
    "GuardrailItem",
    "check_cost",
    "check_guardrails",
    "check_safety",
    "check_time",
    "egress_whitelist",
    "evaluation_counters",
    "failure_report_hint",
    "guardrail_limits",
    "reset_evaluation_counters",
]
