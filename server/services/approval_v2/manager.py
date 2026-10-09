# Copyright 2026 SciLoop contributors
# Licensed under the Apache License, Version 2.0 (the "License")
"""ApprovalManager：审批请求、一次性批准、持续批准、过期与中断（Agent 2 / WP-05）。

职责边界（避免与 Agent 1 重复）：

- **本模块**：风险判定、审批状态机、持续批准规则、审批令牌；
- **Agent 1**：Turn 的状态迁移与事件落盘（``repo.wait_for_approval`` /
  ``repo.resolve_approval``）；
- 两者的接合点是 :meth:`ApprovalManager.make_gate` 返回的「审批门」——
  它与 ``services.agent_runtime_v2.runtime.ApprovalGate`` 兼容（返回 ``allow`` / ``require``）。

安全约束：

- **同一 call_id 只有一个最终结论**：重复裁决幂等，不产生第二次副作用；
- **令牌只在服务端流通**：``ApprovalRequest`` 与工具输出里都不含令牌；
- 拒绝/取消/过期都会把 PENDING 收敛成终态，不会留下悬空审批。
"""

from __future__ import annotations

import secrets
import threading
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from contracts.agent_v2.clock import Clock, SystemClock
from contracts.agent_v2.enums import ApprovalStatus
from contracts.agent_v2.errors import ApprovalNotFound
from contracts.agent_v2.ids import new_id
from contracts.agent_v2.models import ApprovalRequest
from services.sandbox_v2 import normalize

from .grants import SCOPE_THREAD, CommandGrant, GrantStore
from .risk import CommandAnalysis, RiskCategory, RiskLevel, RiskPolicy

#: 与 Agent 1 的门返回值保持一致（字符串常量，避免硬耦合导入）
ALLOW = "allow"
REQUIRE = "require"

#: 审批默认有效期（秒）：超时未决 → expired
DEFAULT_APPROVAL_TTL_S = 900.0


@dataclass(frozen=True)
class RiskAssessment:
    """一次「要不要打扰研究者」的判定结论。"""

    required: bool
    risk: str
    level: RiskLevel
    categories: tuple[str, ...]
    reasons: tuple[str, ...]
    summary: str
    action: dict[str, Any]
    allowlisted: bool = False
    auto_approved: bool = False
    matched_grant: dict[str, Any] | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "required": self.required,
            "risk": self.risk,
            "level": self.level.value,
            "categories": list(self.categories),
            "reasons": list(self.reasons),
            "summary": self.summary,
            "action": dict(self.action),
            "allowlisted": self.allowlisted,
            "auto_approved": self.auto_approved,
            "matched_grant": self.matched_grant,
        }


class ApprovalManager:
    """审批状态的唯一持有者。"""

    def __init__(
        self,
        *,
        clock: Clock | None = None,
        policy: RiskPolicy | None = None,
        grants: GrantStore | None = None,
        grants_path: str | Path | None = None,
        ttl_s: float = DEFAULT_APPROVAL_TTL_S,
        full_access: bool = False,
        on_event: Callable[[str, dict[str, Any]], None] | None = None,
    ) -> None:
        self.clock = clock or SystemClock()
        self.policy = policy or RiskPolicy()
        self.grants = grants or GrantStore(path=grants_path)
        self.ttl_s = float(ttl_s)
        self._full_access = bool(full_access)
        self._on_event = on_event
        self._lock = threading.RLock()
        self._requests: dict[str, ApprovalRequest] = {}
        self._by_call: dict[str, str] = {}
        self._tokens: dict[str, str] = {}
        self._released: set[str] = set()

    # ------------------------------------------------------------------ 模式
    @property
    def full_access(self) -> bool:
        return self._full_access

    def set_full_access(self, enabled: bool) -> None:
        """切换完全访问模式（命令工具自动批准，但审计/超时/取消/输出上限不变）。"""
        self._full_access = bool(enabled)

    # ------------------------------------------------------------------ 判定
    def assess_command(
        self,
        argv: Sequence[str],
        *,
        cwd: str | Path,
        thread_id: str,
        tool: str = "host.exec",
        sandbox_reason: str | None = None,
        sandbox_verdict: dict[str, Any] | None = None,
    ) -> RiskAssessment:
        """命令类工具的风险判定（普通模式口径）。"""
        analysis: CommandAnalysis = self.policy.analyze(list(argv), cwd=cwd)
        categories = [item.value for item in analysis.categories]
        reasons = list(analysis.reasons)
        if sandbox_reason:
            reasons.append(sandbox_reason)

        # 沙箱要求授权（工作区外写入 / 敏感目标）也必须并入"需要审批"，
        # 否则 `mkdir /tmp/x` 这类**参数即写入目标**的命令会被当成普通命令放行。
        sandbox_required = bool(
            sandbox_verdict and sandbox_verdict.get("decision") == "require_approval"
        )
        if sandbox_required:
            protected = bool(sandbox_verdict.get("protected"))
            category = (
                RiskCategory.PROTECTED_RESOURCE if protected
                else RiskCategory.OUTSIDE_WORKSPACE_WRITE
            )
            if category.value not in categories:
                categories.append(category.value)

        action = {
            "tool": tool,
            "argv": [str(item) for item in argv],
            "cwd": str(analysis.cwd),
            "executable": analysis.executable,
            "normalized_argv": list(analysis.normalized_argv),
            "targets": list(analysis.targets),
            "risk_categories": categories,
            "thread_id": thread_id,
        }
        if sandbox_verdict is not None:
            action["sandbox"] = sandbox_verdict

        grant = self.grants.match(argv=list(argv), cwd=action["cwd"], scope_id=thread_id)
        if grant is not None:
            return RiskAssessment(
                required=False,
                risk=RiskLevel.NORMAL.value,
                level=RiskLevel.NORMAL,
                categories=tuple(categories),
                reasons=(*reasons, "命中本对话的持续批准"),
                summary="已在本对话批准过同一条命令",
                action=action,
                allowlisted=analysis.allowlisted,
                auto_approved=True,
                matched_grant=grant.to_dict(),
            )

        if self._full_access:
            return RiskAssessment(
                required=False,
                risk=RiskLevel.HIGH_RISK.value if categories else RiskLevel.NORMAL.value,
                level=analysis.level,
                categories=tuple(categories),
                reasons=(*reasons, "完全访问模式：命令自动批准（仍记账）"),
                summary="完全访问模式自动批准",
                action=action,
                allowlisted=analysis.allowlisted,
                auto_approved=True,
            )

        required = analysis.level is RiskLevel.HIGH_RISK or sandbox_required
        summary = (
            "高危操作，需要研究者批准：" + "；".join(reasons)
            if required
            else "普通操作，无需批准"
        )
        return RiskAssessment(
            required=required,
            risk=RiskLevel.HIGH_RISK.value if required else RiskLevel.NORMAL.value,
            level=RiskLevel.HIGH_RISK if required else RiskLevel.NORMAL,
            categories=tuple(categories),
            reasons=tuple(reasons),
            summary=summary,
            action=action,
            allowlisted=analysis.allowlisted,
        )

    def assess_tool(
        self,
        *,
        tool: str,
        category: str,
        thread_id: str,
        arguments: dict[str, Any] | None = None,
        cwd: str | Path | None = None,
        risk_categories: Sequence[str] = (),
        summary: str | None = None,
    ) -> RiskAssessment:
        """非命令工具的判定：按**工具权限类别**决定是否要批准。"""
        action = {
            "tool": tool,
            "category": category,
            "arguments": dict(arguments or {}),
            "cwd": str(cwd) if cwd is not None else None,
            "risk_categories": [str(item) for item in risk_categories],
            "thread_id": thread_id,
        }
        if self._full_access and category in {"execution", "workspace_write"}:
            return RiskAssessment(
                required=False,
                risk="normal",
                level=RiskLevel.NORMAL,
                categories=tuple(str(item) for item in risk_categories),
                reasons=("完全访问模式：自动批准（仍记账）",),
                summary="完全访问模式自动批准",
                action=action,
                auto_approved=True,
            )
        required = category in {"execution", "high_risk"}
        return RiskAssessment(
            required=required,
            risk="high_risk" if required else "normal",
            level=RiskLevel.HIGH_RISK if required else RiskLevel.NORMAL,
            categories=tuple(str(item) for item in risk_categories),
            reasons=(summary or f"{tool} 属于 {category} 类别",),
            summary=summary or ("高危操作，需要研究者批准" if required else "无需批准"),
            action=action,
        )

    def make_gate(self, thread_id: str) -> Callable[[Any], str]:
        """构造与 Agent 1 ``ApprovalGate`` 兼容的门。

        只读调用直接放行；需要批准但**已在本对话批准过同一条命令**的也放行；
        其余返回 ``require``（由运行时挂起 Turn 并发审批卡）。
        """
        from contracts.agent_v2.enums import ToolKind

        def gate(call: Any) -> str:
            kind = getattr(call, "kind", None)
            if kind is ToolKind.READ_ONLY or str(getattr(kind, "value", kind)) == "read_only":
                return ALLOW
            arguments = dict(getattr(call, "arguments", {}) or {})
            argv = arguments.get("argv")
            if isinstance(argv, (list, tuple)) and argv:
                assessment = self.assess_command(
                    [str(item) for item in argv],
                    cwd=arguments.get("cwd") or "",
                    thread_id=thread_id,
                    tool=str(getattr(call, "name", "host.exec")),
                )
                return ALLOW if not assessment.required else REQUIRE
            if self._full_access:
                return ALLOW
            return REQUIRE

        return gate

    def require_for_call(self, call: Any, *, thread_id: str) -> bool:
        """便捷判断：这个调用是否需要审批。"""
        return self.make_gate(thread_id)(call) == REQUIRE

    # ------------------------------------------------------------------ 请求
    def request(
        self,
        *,
        thread_id: str,
        turn_id: str,
        call_id: str | None,
        action: dict[str, Any],
        risk: str = "unknown",
        summary: str | None = None,
    ) -> ApprovalRequest:
        """创建（或复用）审批请求。**同一 call_id 只可能有一条请求**（幂等）。"""
        with self._lock:
            if call_id is not None:
                existing_id = self._by_call.get(call_id)
                if existing_id is not None:
                    return self._requests[existing_id]
            payload = dict(action)
            if summary:
                payload["summary"] = summary
            approval = ApprovalRequest(
                approval_id=new_id("approval"),
                thread_id=thread_id,
                turn_id=turn_id,
                call_id=call_id,
                created_at=self.clock.now_iso(),
                status=ApprovalStatus.PENDING,
                action=payload,
                risk=risk,
            )
            self._requests[approval.approval_id] = approval
            if call_id is not None:
                self._by_call[call_id] = approval.approval_id
            self._emit("approval/requested", approval)
            return approval

    def request_for_assessment(
        self,
        assessment: RiskAssessment,
        *,
        thread_id: str,
        turn_id: str,
        call_id: str | None,
    ) -> ApprovalRequest:
        """按判定结论生成审批请求（把风险类别带进 action，供界面展示）。"""
        return self.request(
            thread_id=thread_id,
            turn_id=turn_id,
            call_id=call_id,
            action=assessment.action,
            risk=assessment.risk,
            summary=assessment.summary,
        )

    # ------------------------------------------------------------------ 读取
    def adopt(self, approval: ApprovalRequest | dict[str, Any]) -> ApprovalRequest:
        """接管一条**外部创建**的审批请求（例如 Agent 1 仓储落盘的那条）。

        职责划分：审批的**持久化与 Turn 挂起/恢复**由 Agent 1 的
        ``ThreadRepository.wait_for_approval/resolve_approval`` 负责；本管理器负责
        **判定、持续批准与令牌**。为避免两套状态各说各话，决策前先 ``adopt`` 同一
        ``approval_id``，此后「批准一次」「持续批准」「令牌核销」都作用在它身上。
        """
        record = (
            approval
            if isinstance(approval, ApprovalRequest)
            else ApprovalRequest.from_dict(dict(approval))
        )
        with self._lock:
            self._requests[record.approval_id] = record
            if record.call_id is not None:
                self._by_call.setdefault(record.call_id, record.approval_id)
            return record

    def adopt_pending(self, approvals: Sequence[ApprovalRequest]) -> list[ApprovalRequest]:
        """批量接管（例如从 ``repo.state(thread).approvals`` 里筛出 PENDING 的那批）。"""
        return [self.adopt(item) for item in approvals]

    def get(self, approval_id: str) -> ApprovalRequest | None:
        with self._lock:
            return self._requests.get(approval_id)

    def by_call(self, call_id: str) -> ApprovalRequest | None:
        with self._lock:
            approval_id = self._by_call.get(call_id)
            return None if approval_id is None else self._requests.get(approval_id)

    def pending(self, *, thread_id: str | None = None) -> list[ApprovalRequest]:
        with self._lock:
            return [
                item
                for item in self._requests.values()
                if item.status is ApprovalStatus.PENDING
                and (thread_id is None or item.thread_id == thread_id)
            ]

    def decisions(self) -> dict[str, dict[str, Any]]:
        """所有已决审批的结论（``call_id -> 结论``），用于验证"一个 Call 一个结论"。"""
        return {
            item.call_id: {
                "approval_id": item.approval_id,
                "status": item.status.value,
                "scope": item.decision_scope,
            }
            for item in self._requests.values()
            if item.call_id is not None and item.status is not ApprovalStatus.PENDING
        }

    def is_released(self, call_id: str) -> bool:
        """该 Call 是否已被「批准一次」释放（只释放当前 Call）。"""
        with self._lock:
            return call_id in self._released

    # ------------------------------------------------------------------ 裁决
    def grant_once(self, approval_id: str, *, by: str = "owner", scope: str = "once") -> ApprovalRequest:
        """批准一次：**只释放当前 Call**。重复批准幂等。

        若这条审批已被裁决为 GRANTED（例如先由 Agent 1 的仓储落盘），仍然登记"释放该 Call"，
        否则会出现「界面显示已批准、执行层却没放行」的错位。
        """
        with self._lock:
            approval = self._require(approval_id)
            if approval.status is ApprovalStatus.PENDING:
                approval = self._decide(approval, granted=True, by=by, scope=scope)
            if approval.status is ApprovalStatus.GRANTED and approval.call_id is not None:
                self._released.add(approval.call_id)
            return approval

    def always_allow(
        self,
        approval_id: str,
        *,
        by: str = "owner",
        scope_id: str | None = None,
        scope: str = SCOPE_THREAD,
    ) -> tuple[ApprovalRequest, CommandGrant | None]:
        """「当前对话始终批准当前命令」：既释放当前 Call，也登记持续批准规则。

        规则保存**规范化 argv + cwd + 会话作用域**三者，任一变化都不再命中。
        """
        with self._lock:
            approval = self._require(approval_id)
            grant: CommandGrant | None = None
            if approval.status is ApprovalStatus.PENDING:
                approval = self._decide(
                    approval, granted=True, by=by, scope=f"always:{scope}"
                )
            if approval.status is ApprovalStatus.GRANTED and approval.call_id is not None:
                self._released.add(approval.call_id)
            # action 可能来自两条路径：本管理器的判定结果（argv/cwd 在顶层），
            # 或 Agent 1 运行时落盘的那条（argv/cwd 嵌在 arguments 里）——两种都要认。
            arguments = approval.action.get("arguments")
            nested = arguments if isinstance(arguments, dict) else {}
            argv = approval.action.get("argv") or nested.get("argv")
            cwd = approval.action.get("cwd") or nested.get("cwd")
            if isinstance(argv, (list, tuple)) and argv and isinstance(cwd, str):
                executable = str(
                    approval.action.get("executable") or Path(str(argv[0])).name
                )
                normalized = approval.action.get("normalized_argv")
                if not isinstance(normalized, (list, tuple)) or len(normalized) != len(argv):
                    from .risk import _normalize_tokens

                    normalized = _normalize_tokens(
                        [str(item) for item in argv], normalize(cwd)
                    )
                grant = self.grants.add(
                    CommandGrant(
                        executable=executable,
                        normalized_argv=tuple(str(item) for item in normalized),
                        cwd=str(normalize(cwd)),
                        scope=scope,
                        scope_id=scope_id or approval.thread_id,
                        created_at=self.clock.now_iso(),
                        approval_id=approval.approval_id,
                        tool=str(approval.action.get("tool") or "host.exec"),
                    )
                )
            return approval, grant

    def deny(self, approval_id: str, *, by: str = "owner") -> ApprovalRequest:
        """拒绝：结论回喂模型，工具**不执行**。"""
        with self._lock:
            approval = self._require(approval_id)
            if approval.status is not ApprovalStatus.PENDING:
                return approval
            return self._decide(approval, granted=False, by=by, scope="denied")

    def cancel(self, approval_id: str, *, reason: str = "cancelled", by: str = "system") -> ApprovalRequest:
        """取消（Turn 被中断等）：记为拒绝且作用域为 cancelled，不产生副作用。"""
        with self._lock:
            approval = self._require(approval_id)
            if approval.status is not ApprovalStatus.PENDING:
                return approval
            return self._decide(approval, granted=False, by=by, scope=f"cancelled:{reason}")

    def expire(self, approval_id: str) -> ApprovalRequest:
        """显式过期。"""
        with self._lock:
            approval = self._require(approval_id)
            if approval.status is not ApprovalStatus.PENDING:
                return approval
            return self._set_status(approval, ApprovalStatus.EXPIRED, scope="expired", by="system")

    def expire_stale(self, *, now_iso: str | None = None) -> list[ApprovalRequest]:
        """把所有超过 ``ttl_s`` 仍未决的请求置为过期。"""
        moment = now_iso or self.clock.now_iso()
        expired: list[ApprovalRequest] = []
        for approval in self.pending():
            if self._age_s(approval, moment) >= self.ttl_s:
                expired.append(self.expire(approval.approval_id))
        return expired

    def resolve_on_interrupt(
        self, thread_id: str, turn_id: str, *, reason: str = "turn_interrupted"
    ) -> list[ApprovalRequest]:
        """Turn 中断/失败时统一收敛未完成审批（不留悬空 PENDING）。"""
        resolved: list[ApprovalRequest] = []
        for approval in self.pending(thread_id=thread_id):
            if approval.turn_id != turn_id:
                continue
            resolved.append(self.cancel(approval.approval_id, reason=reason))
        return resolved

    # ------------------------------------------------------------------ 令牌
    def issue_token(self, approval_id: str) -> str:
        """签发一次性审批令牌（**只在服务端流通，绝不写进模型可见参数**）。"""
        token = secrets.token_urlsafe(24)
        with self._lock:
            self._tokens[token] = approval_id
        return token

    def consume_token(self, token: str) -> ApprovalRequest | None:
        """核销令牌并要求对应审批已批准；无效/未批准返回 None。"""
        with self._lock:
            approval_id = self._tokens.pop(token, None)
            if approval_id is None:
                return None
            approval = self._requests.get(approval_id)
            if approval is None or approval.status is not ApprovalStatus.GRANTED:
                return None
            return approval

    def token_count(self) -> int:
        with self._lock:
            return len(self._tokens)

    # ------------------------------------------------------------------ 内部
    def _require(self, approval_id: str) -> ApprovalRequest:
        approval = self._requests.get(approval_id)
        if approval is None:
            raise ApprovalNotFound(f"approval {approval_id} not found")
        return approval

    def _decide(
        self, approval: ApprovalRequest, *, granted: bool, by: str, scope: str
    ) -> ApprovalRequest:
        status = ApprovalStatus.GRANTED if granted else ApprovalStatus.DENIED
        return self._set_status(approval, status, scope=scope, by=by)

    def _set_status(
        self, approval: ApprovalRequest, status: ApprovalStatus, *, scope: str, by: str
    ) -> ApprovalRequest:
        data = approval.to_dict()
        data["status"] = status.value
        data["decided_at"] = self.clock.now_iso()
        data["decided_by"] = by
        data["decision_scope"] = scope
        updated = ApprovalRequest.from_dict(data)
        self._requests[updated.approval_id] = updated
        self._emit(
            "approval/granted" if status is ApprovalStatus.GRANTED else "approval/denied",
            updated,
        )
        return updated

    def _age_s(self, approval: ApprovalRequest, now_iso: str) -> float:
        try:
            from datetime import datetime

            created = datetime.fromisoformat(approval.created_at.replace("Z", "+00:00"))
            now = datetime.fromisoformat(now_iso.replace("Z", "+00:00"))
        except ValueError:  # pragma: no cover - 时间格式异常时不判过期
            return 0.0
        return max(0.0, (now - created).total_seconds())

    def _emit(self, kind: str, approval: ApprovalRequest) -> None:
        if self._on_event is None:
            return
        self._on_event(kind, {"approval": approval.to_dict()})


__all__ = [
    "ApprovalManager",
    "RiskAssessment",
    "ALLOW",
    "REQUIRE",
    "DEFAULT_APPROVAL_TTL_S",
    "RiskCategory",
    "RiskLevel",
]
