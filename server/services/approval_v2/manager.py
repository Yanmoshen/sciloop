# Copyright 2026 SciLoop contributors
# Licensed under the Apache License, Version 2.0 (the "License")
"""ApprovalManager：审批事实、状态机、作用域与令牌（Agent 2 / WP-03）。

职责边界（避免与 Agent 1 重复）：

- **本模块**：风险判定、审批状态机、持续批准、令牌、重启收敛；
- **Agent 1**：Turn 状态迁移与事件落盘（``repo.wait_for_approval`` / ``repo.resolve_approval``）；
- 接合点：:meth:`ApprovalManager.adopt` —— 裁决前先接管执行层的那条 ``approval_id``，
  防止"界面显示批准而执行器仍认为 pending"。

六种裁决（文档 WP-03）：

    approve_once            只释放当前 call_id
    approve_for_thread      绑定 executable + argv 前缀 + 参数限制 + cwd 范围 + thread
    deny                    工具不启动，结构化拒绝结果回运行时
    cancel                  取消等待并通知执行器
    expire                  超过截止时间自动拒绝
    escalate_full_access    只有用户显式操作才能切到 danger-full-access

安全约束：同一 ``call_id`` 只有一个最终结论；重复裁决幂等；令牌只在服务端流通。
"""

from __future__ import annotations

import threading
from collections.abc import Callable, Sequence
from pathlib import Path
from typing import Any

from contracts.agent_v2.clock import Clock, SystemClock
from contracts.agent_v2.enums import ApprovalStatus, ToolKind
from contracts.agent_v2.errors import ApprovalNotFound
from contracts.agent_v2.ids import new_id
from contracts.agent_v2.models import ApprovalRequest
from services.sandbox_v2 import SandboxManager, SandboxPolicy, match_protected_rule

from .grants import (
    ARG_EXACT,
    ARG_PREFIX,
    CWD_EXACT,
    SCOPE_THREAD,
    ArgConstraints,
    CommandGrant,
    GrantStore,
)
from .models import (
    ApprovalView,
    DecisionScope,
    RiskAssessment,
    RiskCategory,
    RiskLevel,
    analysis_view,
)
from .rules import RiskPolicy, basename, normalize_argv
from .store import ApprovalStore
from .tokens import DEFAULT_TOKEN_TTL_S, ApprovalToken, ApprovalTokenStore, shift_iso

#: 与 Agent 1 的门返回值保持一致（字符串常量，避免硬耦合导入）
ALLOW = "allow"
REQUIRE = "require"

#: 审批默认有效期（秒）
DEFAULT_APPROVAL_TTL_S = 900.0


class ApprovalManager:
    """审批状态的唯一持有者（持久化 + 内存视图一致）。"""

    def __init__(
        self,
        *,
        clock: Clock | None = None,
        policy: RiskPolicy | None = None,
        grants: GrantStore | None = None,
        grants_path: str | Path | None = None,
        store: ApprovalStore | None = None,
        approvals_path: str | Path | None = None,
        tokens: ApprovalTokenStore | None = None,
        ttl_s: float = DEFAULT_APPROVAL_TTL_S,
        token_ttl_s: float = DEFAULT_TOKEN_TTL_S,
        full_access: bool = False,
        sandbox: SandboxManager | None = None,
        on_event: Callable[[str, dict[str, Any]], None] | None = None,
    ) -> None:
        self.clock = clock or SystemClock()
        self.policy = policy if policy is not None else RiskPolicy()
        self.grants = grants if grants is not None else GrantStore(path=grants_path)
        self.store = store if store is not None else ApprovalStore(path=approvals_path)
        self.tokens = tokens if tokens is not None else ApprovalTokenStore()
        self.ttl_s = float(ttl_s)
        self.token_ttl_s = float(token_ttl_s)
        self._full_access = bool(full_access)
        self.sandbox = sandbox
        self._on_event = on_event
        self._lock = threading.RLock()
        self._requests: dict[str, ApprovalRequest] = {}
        self._by_call: dict[str, str] = {}
        self._released: set[str] = set()
        self._views_cache: dict[str, ApprovalView] = {}
        self._sync_from_store()

    def _sync_from_store(self) -> None:
        """把落盘的 pending 审批拉回内存视图（重启后仍能查到未完成与历史结论）。"""
        for view in self.store.pending():
            self._views_cache[view.approval_id] = view
            if view.call_id is not None:
                self._by_call.setdefault(view.call_id, view.approval_id)

    # ------------------------------------------------------------------ 模式
    @property
    def full_access(self) -> bool:
        return self._full_access

    def set_full_access(self, enabled: bool) -> None:
        """直接设置（用于配置加载）。**模型不能调用**——升级必须走 escalate_full_access。"""
        self._full_access = bool(enabled)

    def escalate_full_access(
        self, approval_id: str, *, by: str = "owner", sandbox: SandboxManager | None = None
    ) -> ApprovalView:
        """由用户显式操作把沙箱升到 ``danger-full-access``。

        这是**唯一**的升级入口：模型/研究节点无法自行调用，且升级动作会留裁决记录。
        """
        with self._lock:
            view = self._require_view(approval_id)
            data = view.to_dict()
            data["status"] = ApprovalStatus.GRANTED.value
            data["decided_at"] = self.clock.now_iso()
            data["decided_by"] = by
            data["decision_scope"] = DecisionScope.ESCALATED.value
            data["summary"] = "用户显式升级为完全访问模式"
            updated = self.store.save(ApprovalView.from_dict(data))
            self._views_cache[updated.approval_id] = updated
        self.set_full_access(True)
        target = sandbox or self.sandbox
        if target is not None:
            target.set_policy(SandboxPolicy.DANGER_FULL_ACCESS)
        self._emit("approval/granted", updated.to_dict())
        return updated

    # ------------------------------------------------------------------ 判定
    def assess_command(
        self,
        argv: Sequence[str],
        *,
        cwd: str | Path,
        thread_id: str,
        tool: str = "host.command",
        sandbox_verdict: dict[str, Any] | None = None,
        requested_scope: str = DecisionScope.ONCE.value,
    ) -> RiskAssessment:
        """命令类工具的风险判定（普通模式口径）。"""
        tokens = [str(item) for item in argv]
        analysis = self.policy.analyze(tokens, cwd=cwd)
        categories = [item.value for item in analysis.categories]
        reasons = list(analysis.reasons)

        verdict = sandbox_verdict or {}
        if verdict.get("decision") == "require_approval":
            if verdict.get("reason"):
                reasons.append(str(verdict["reason"]))
            category = (
                RiskCategory.CREDENTIAL_ACCESS.value
                if verdict.get("protected")
                else RiskCategory.OUTSIDE_WORKSPACE_WRITE.value
            )
            if category not in categories:
                categories.append(category)
        if verdict.get("escalation"):
            reasons.append(str(verdict["escalation"]))

        # 命令是否指向敏感目标（凭据 / 密钥）→ 凭据访问类别
        if self.sandbox is not None:
            for token in tokens[1:]:
                rule = match_protected_rule(token, self.sandbox.protected_rules)
                if rule is not None:
                    if RiskCategory.CREDENTIAL_ACCESS.value not in categories:
                        categories.append(RiskCategory.CREDENTIAL_ACCESS.value)
                    reasons.append(f"命令触及敏感目标（规则 {rule.rule_id}）")
                    break

        sandbox_required = verdict.get("decision") == "require_approval"
        argv_norm = normalize_argv(tokens, cwd=cwd)
        executable = analysis.executable
        action = {
            **analysis_view(analysis),
            "tool": tool,
            "tool_name": tool,
            "thread_id": thread_id,
            "requested_scope": requested_scope,
        }
        if verdict:
            action["sandbox"] = verdict

        grant = self.grants.match(
            executable=executable, normalized_argv=argv_norm, cwd=cwd, scope_id=thread_id
        )
        if grant is not None:
            return RiskAssessment(
                required=False,
                risk=RiskLevel.NORMAL.value,
                level=RiskLevel.NORMAL,
                categories=tuple(categories),
                reasons=(*reasons, "命中本对话的持续批准"),
                summary="已在本对话长期放行同一条命令",
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
                reasons=(*reasons, "完全访问模式：命令自动放行（仍记账）"),
                summary="完全访问模式自动放行",
                action=action,
                allowlisted=analysis.allowlisted,
                auto_approved=True,
            )

        required = analysis.level is RiskLevel.HIGH_RISK or sandbox_required
        escalation = verdict.get("escalation")
        summary = (
            "高危操作，需要研究者决定：" + "；".join(reasons)
            if required
            else "普通操作，无需决定"
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
            escalation=str(escalation) if escalation else None,
        )

    def assess_tool(
        self,
        *,
        tool: str,
        permission_class: str,
        thread_id: str,
        arguments: dict[str, Any] | None = None,
        cwd: str | Path | None = None,
        risk_categories: Sequence[str] = (),
        summary: str | None = None,
        requested_scope: str = DecisionScope.ONCE.value,
    ) -> RiskAssessment:
        """非命令工具：按**权限类别**决定是否要裁决。"""
        action = {
            "kind": permission_class,
            "tool": tool,
            "tool_name": tool,
            "arguments": dict(arguments or {}),
            "cwd": str(cwd) if cwd is not None else None,
            "risk_categories": [str(item) for item in risk_categories],
            "thread_id": thread_id,
            "requested_scope": requested_scope,
        }
        if self._full_access and permission_class in {"exec", "workspace_write", "dangerous"}:
            return RiskAssessment(
                required=False,
                risk=RiskLevel.NORMAL.value,
                level=RiskLevel.NORMAL,
                categories=tuple(str(item) for item in risk_categories),
                reasons=("完全访问模式：自动放行（仍记账）",),
                summary="完全访问模式自动放行",
                action=action,
                auto_approved=True,
            )
        required = permission_class in {"exec", "dangerous"}
        return RiskAssessment(
            required=required,
            risk=RiskLevel.HIGH_RISK.value if required else RiskLevel.NORMAL.value,
            level=RiskLevel.HIGH_RISK if required else RiskLevel.NORMAL,
            categories=tuple(str(item) for item in risk_categories),
            reasons=(summary or f"{tool} 属于 {permission_class} 类别",),
            summary=summary or ("高危操作，需要研究者决定" if required else "无需决定"),
            action=action,
        )

    def make_gate(self, thread_id: str) -> Callable[[Any], str]:
        """构造与 Agent 1 ``ApprovalGate`` 兼容的门。"""
        from contracts.agent_v2.models import ToolCall as _ToolCall  # noqa: F401 运行时类型提示

        def gate(call: Any) -> str:
            kind = getattr(call, "kind", None)
            if kind is ToolKind.READ_ONLY or str(getattr(kind, "value", kind)) == "read_only":
                return ALLOW
            arguments = dict(getattr(call, "arguments", {}) or {})
            argv = arguments.get("argv")
            if isinstance(argv, (list, tuple)) and argv:
                assessment = self.assess_command(
                    [str(item) for item in argv],
                    cwd=arguments.get("cwd") or (self.sandbox.workspace_root if self.sandbox else "."),
                    thread_id=thread_id,
                    tool=str(getattr(call, "name", "host.command")),
                )
                return ALLOW if not assessment.required else REQUIRE
            if self._full_access:
                return ALLOW
            return REQUIRE

        return gate

    def require_for_call(self, call: Any, *, thread_id: str) -> bool:
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
        requested_scope: str = DecisionScope.ONCE.value,
    ) -> ApprovalView:
        """创建（或复用）审批请求。同一 ``call_id`` 只可能有一条请求。"""
        with self._lock:
            if call_id is not None:
                existing = self._by_call.get(call_id)
                if existing is not None:
                    return self.view(existing) or self.store.get(existing)  # type: ignore[return-value]
            payload = dict(action)
            if summary:
                payload["summary"] = summary
            payload.setdefault("requested_scope", requested_scope)
            created_at = self.clock.now_iso()
            contract = ApprovalRequest(
                approval_id=new_id("approval"),
                thread_id=thread_id,
                turn_id=turn_id,
                call_id=call_id,
                created_at=created_at,
                status=ApprovalStatus.PENDING,
                action=payload,
                risk=risk,
            )
            view = ApprovalView.from_contract(
                contract, expires_at=shift_iso(created_at, seconds=self.ttl_s), summary=summary
            )
            self._requests[view.approval_id] = contract
            self._views_cache[view.approval_id] = view
            self.store.save(view)
            if call_id is not None:
                self._by_call[call_id] = view.approval_id
            self._emit("approval/requested", view.to_dict())
            return view

    def request_for_assessment(
        self,
        assessment: RiskAssessment,
        *,
        thread_id: str,
        turn_id: str,
        call_id: str | None,
    ) -> ApprovalView:
        action = dict(assessment.action)
        if assessment.categories:
            action["risk_categories"] = list(assessment.categories)
        return self.request(
            thread_id=thread_id,
            turn_id=turn_id,
            call_id=call_id,
            action=action,
            risk=assessment.risk,
            summary=assessment.summary,
            requested_scope=str(action.get("requested_scope") or DecisionScope.ONCE.value),
        )

    # ------------------------------------------------------------------ 接管
    def adopt(self, approval: ApprovalRequest | ApprovalView | dict[str, Any]) -> ApprovalView:
        """接管执行层创建的审批（Agent 1 仓储落盘的那条），保证两边同一个 approval_id。"""
        with self._lock:
            if isinstance(approval, ApprovalView):
                view = approval
            elif isinstance(approval, ApprovalRequest):
                view = ApprovalView.from_contract(approval)
            else:
                view = ApprovalView.from_dict(dict(approval))
            existing = self.store.get(view.approval_id)
            if existing is not None and existing.decided_at and not view.decided_at:
                view = existing  # 已有最终结论时以本地结论为准
            self._views_cache[view.approval_id] = view
            self.store.save(view)
            if view.call_id is not None:
                self._by_call.setdefault(view.call_id, view.approval_id)
            if isinstance(approval, ApprovalRequest):
                self._requests[view.approval_id] = approval
            return view

    def adopt_pending(self, approvals: Sequence[ApprovalRequest | ApprovalView]) -> list[ApprovalView]:
        return [self.adopt(item) for item in approvals]

    # ------------------------------------------------------------------ 读取
    def get(self, approval_id: str) -> ApprovalRequest | None:
        with self._lock:
            return self._requests.get(approval_id)

    def view(self, approval_id: str) -> ApprovalView | None:
        with self._lock:
            cached = self._views_cache.get(approval_id)
            return cached if cached is not None else self.store.get(approval_id)

    def by_call(self, call_id: str) -> ApprovalView | None:
        with self._lock:
            approval_id = self._by_call.get(call_id)
            if approval_id is None:
                found = [item for item in self.store.list() if item.call_id == call_id]
                return found[-1] if found else None
            return self.view(approval_id)

    def pending(self, *, thread_id: str | None = None) -> list[ApprovalView]:
        with self._lock:
            return self.store.pending(thread_id=thread_id)

    def decisions(self) -> dict[str, dict[str, Any]]:
        """``call_id -> 最终结论``（验证"一个 Call 一个结论"）。"""
        return {
            item.call_id: {
                "approval_id": item.approval_id,
                "status": item.status,
                "scope": item.decision_scope,
            }
            for item in self.store.list()
            if item.call_id is not None and item.status != ApprovalStatus.PENDING.value
        }

    def is_released(self, call_id: str) -> bool:
        with self._lock:
            return call_id in self._released

    def audit(self) -> list[dict[str, Any]]:
        return self.store.audit()

    # ------------------------------------------------------------------ 裁决
    def approve_once(self, approval_id: str, *, by: str = "owner") -> ApprovalView:
        """只释放当前 call_id；重复裁决幂等。"""
        with self._lock:
            view = self._require_view(approval_id)
            if view.pending:
                view = self._decide(view, granted=True, by=by, scope=DecisionScope.ONCE.value)
            if view.status == ApprovalStatus.GRANTED.value and view.call_id is not None:
                self._released.add(view.call_id)
            return view

    def approve_for_thread(
        self,
        approval_id: str,
        *,
        by: str = "owner",
        scope_id: str | None = None,
        arg_mode: str = ARG_EXACT,
        max_extra_args: int = 2,
        cwd_mode: str = CWD_EXACT,
    ) -> tuple[ApprovalView, CommandGrant | None]:
        """「当前对话持续放行」：绑定 executable + argv 前缀 + 参数限制 + cwd 范围 + thread。"""
        with self._lock:
            view = self._require_view(approval_id)
            grant: CommandGrant | None = None
            if view.pending:
                view = self._decide(
                    view, granted=True, by=by, scope=f"{DecisionScope.THREAD.value}:{ARG_EXACT}"
                )
            if view.status == ApprovalStatus.GRANTED.value and view.call_id is not None:
                self._released.add(view.call_id)

            argv = tuple(str(item) for item in view.normalized_argv)
            cwd = view.cwd
            if argv and cwd:
                prefix = tuple(argv[:2]) if len(argv) > 1 else tuple(argv)
                constraints = (
                    ArgConstraints.prefix(argv, max_extra_args=max_extra_args)
                    if arg_mode == ARG_PREFIX
                    else ArgConstraints.exact(argv)
                )
                grant = self.grants.add(
                    CommandGrant(
                        executable=(view.executable or basename(argv[0])).casefold(),
                        prefix=prefix,
                        arg_constraints=constraints,
                        cwd=str(cwd),
                        cwd_mode=cwd_mode,
                        scope=SCOPE_THREAD,
                        scope_id=scope_id or view.thread_id,
                        created_at=self.clock.now_iso(),
                        approval_id=view.approval_id,
                        tool=view.tool_name or "host.command",
                    )
                )
            return view, grant

    def deny(self, approval_id: str, *, by: str = "owner") -> ApprovalView:
        """拒绝：工具不启动，结构化拒绝结果回运行时。"""
        with self._lock:
            view = self._require_view(approval_id)
            if not view.pending:
                return view
            return self._decide(view, granted=False, by=by, scope=DecisionScope.DENIED.value)

    def cancel(self, approval_id: str, *, reason: str = "cancelled", by: str = "system") -> ApprovalView:
        """取消等待并通知执行器（结果收敛为拒绝，不产生副作用）。"""
        with self._lock:
            view = self._require_view(approval_id)
            if not view.pending:
                return view
            self.tokens.revoke(approval_id)
            return self._decide(
                view, granted=False, by=by, scope=f"{DecisionScope.CANCELLED.value}:{reason}"
            )

    def expire(self, approval_id: str, *, now_iso: str | None = None) -> ApprovalView:
        """显式过期。"""
        with self._lock:
            view = self._require_view(approval_id)
            if not view.pending:
                return view
            self.tokens.revoke(approval_id)
            return self._set_status(
                view, ApprovalStatus.EXPIRED, scope=DecisionScope.EXPIRED.value, by="system",
                now_iso=now_iso,
            )

    def expire_stale(self, *, now_iso: str | None = None) -> list[ApprovalView]:
        """把所有超过 ``ttl_s`` 仍未决的请求置为过期。"""
        moment = now_iso or self.clock.now_iso()
        expired: list[ApprovalView] = []
        for view in self.pending():
            if _age_s(view.created_at, moment) >= self.ttl_s:
                expired.append(self.expire(view.approval_id, now_iso=moment))
        self.tokens.purge_expired(moment)
        return expired

    def resolve_on_interrupt(
        self, thread_id: str, turn_id: str, *, reason: str = "turn_interrupted"
    ) -> list[ApprovalView]:
        """Turn 中断/失败时统一收敛未完成审批。"""
        resolved: list[ApprovalView] = []
        for view in self.pending(thread_id=thread_id):
            if view.turn_id != turn_id:
                continue
            resolved.append(self.cancel(view.approval_id, reason=reason))
        return resolved

    def converge_on_restart(self, *, reason: str = "service_restart") -> list[ApprovalView]:
        """服务重启：pending 收敛为过期（不放行、不重放）。"""
        with self._lock:
            converged = self.store.converge_on_restart(reason=reason)
            for view in converged:
                self._views_cache[view.approval_id] = view
            return converged

    # ------------------------------------------------------------------ 令牌
    def issue_token(self, approval_id: str) -> str:
        """签发一次性令牌（**只在服务端流通**）。"""
        with self._lock:
            view = self._require_view(approval_id)
            if view.status != ApprovalStatus.GRANTED.value:
                raise ValueError(f"approval {approval_id} is not granted; token refused")
            token = self.tokens.issue(
                approval_id=view.approval_id,
                thread_id=view.thread_id,
                turn_id=view.turn_id,
                call_id=view.call_id,
                tool=view.tool_name,
                now_iso=self.clock.now_iso(),
                ttl_s=self.token_ttl_s,
            )
            return token.token

    def verify_token(
        self,
        token: str,
        *,
        thread_id: str,
        turn_id: str,
        call_id: str | None,
        tool: str,
    ) -> ApprovalToken | None:
        return self.tokens.verify(
            token,
            thread_id=thread_id,
            turn_id=turn_id,
            call_id=call_id,
            tool=tool,
            now_iso=self.clock.now_iso(),
        )

    def consume_token(
        self,
        token: str,
        *,
        thread_id: str,
        turn_id: str,
        call_id: str | None,
        tool: str,
    ) -> ApprovalToken | None:
        return self.tokens.consume(
            token,
            thread_id=thread_id,
            turn_id=turn_id,
            call_id=call_id,
            tool=tool,
            now_iso=self.clock.now_iso(),
        )

    def token_count(self) -> int:
        return len(self.tokens)

    # ------------------------------------------------------------------ 内部
    def _require_view(self, approval_id: str) -> ApprovalView:
        view = self.view(approval_id)
        if view is None:
            raise ApprovalNotFound(f"approval {approval_id} not found")
        return view

    def _decide(
        self, view: ApprovalView, *, granted: bool, by: str, scope: str
    ) -> ApprovalView:
        status = ApprovalStatus.GRANTED if granted else ApprovalStatus.DENIED
        return self._set_status(view, status, scope=scope, by=by)

    def _set_status(
        self,
        view: ApprovalView,
        status: ApprovalStatus,
        *,
        scope: str,
        by: str,
        now_iso: str | None = None,
    ) -> ApprovalView:
        data = view.to_dict()
        data["status"] = status.value
        data["decided_at"] = now_iso or self.clock.now_iso()
        data["decided_by"] = by
        data["decision_scope"] = scope
        updated = self.store.save(ApprovalView.from_dict(data))
        self._views_cache[updated.approval_id] = updated
        contract = self._requests.get(updated.approval_id)
        if contract is not None:
            contract_data = contract.to_dict()
            contract_data["status"] = status.value
            contract_data["decided_at"] = data["decided_at"]
            contract_data["decided_by"] = by
            contract_data["decision_scope"] = scope
            self._requests[updated.approval_id] = ApprovalRequest.from_dict(contract_data)
        if status is not ApprovalStatus.GRANTED:
            self.tokens.revoke(updated.approval_id)
        self._emit(
            "approval/granted" if status is ApprovalStatus.GRANTED else "approval/denied",
            updated.to_dict(),
        )
        return updated

    def _emit(self, kind: str, payload: dict[str, Any]) -> None:
        if self._on_event is None:
            return
        self._on_event(kind, payload)


def _age_s(created_at: str, now_iso: str) -> float:
    from datetime import datetime

    try:
        created = datetime.fromisoformat(created_at.replace("Z", "+00:00"))
        now = datetime.fromisoformat(now_iso.replace("Z", "+00:00"))
    except ValueError:  # pragma: no cover
        return 0.0
    return max(0.0, (now - created).total_seconds())


__all__ = [
    "ApprovalManager",
    "RiskAssessment",
    "ApprovalView",
    "DecisionScope",
    "ALLOW",
    "REQUIRE",
    "DEFAULT_APPROVAL_TTL_S",
    "RiskCategory",
    "RiskLevel",
]
