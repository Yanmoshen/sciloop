# Copyright 2026 SciLoop contributors
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
"""研究者批准的契约测试。

这一层最要紧的不是"能存能取"，而是三条**防止批准被稀释**的性质：

1. **一次性**：批过的请求不能再批（否则一次批准 = 长期开关）；
2. **绑死具体调用**：指纹含工具名与全部参数（批准的是"这一次"，不是"这一类"）；
3. **不存明文令牌**：会话文件是给人看的，可用凭据不能写进去。
"""

from __future__ import annotations

from datetime import datetime, timedelta
from typing import Any

from services.agent import approvals


def _record(request: dict) -> dict:
    return {"id": "c1", "turns": [{"role": "assistant", "content": "", "approvals": [request]}]}


def _request(**kwargs) -> dict:
    return approvals.new_request(
        tool="run_command", args={"argv": ["ruff", "check", "."], "cwd": None}, **kwargs
    )


# --------------------------------------------------------------------------- #
# 请求的形状
# --------------------------------------------------------------------------- #
def test_new_request_is_pending_and_previews_the_real_invocation() -> None:
    request = _request()
    assert request["status"] == "pending"
    assert request["tool"] == "run_command"
    # 研究者**看的就是这一行**，所以它必须说清"到底要跑什么"
    assert request["preview"] == "ruff check ."
    # 原始参数也留着：批准之后要按原样执行，不能靠预览字符串反推
    assert request["args"]["argv"] == ["ruff", "check", "."]
    assert request["decided_at"] is None
    assert request["token_fingerprint"] is None


def test_preview_says_something_when_args_are_empty_or_odd() -> None:
    assert approvals.new_request(tool="run_command", args={})["preview"] == "(无参数)"
    long_args = {"argv": ["echo", "x" * 500]}
    preview = approvals.new_request(tool="run_command", args=long_args)["preview"]
    assert len(preview) <= approvals.PREVIEW_MAX_CHARS + 1
    assert preview.endswith("…")


def test_digest_binds_the_tool_and_every_argument() -> None:
    """批准绑的是**这一次调用**：换个参数、换个工具，指纹都必须变。"""

    base = approvals.args_digest("run_command", {"argv": ["ls"]})
    assert base == approvals.args_digest("run_command", {"argv": ["ls"]})  # 稳定
    assert base != approvals.args_digest("run_command", {"argv": ["ls", "-la"]})
    assert base != approvals.args_digest("fetch_url", {"argv": ["ls"]})
    # 键顺序不该影响指纹（同一份参数的不同写法要落到同一个指纹上）
    assert approvals.args_digest("t", {"a": 1, "b": 2}) == approvals.args_digest("t", {"b": 2, "a": 1})


# --------------------------------------------------------------------------- #
# 有效期：过期是**读出来的**
# --------------------------------------------------------------------------- #
def test_pending_never_expires_on_its_own() -> None:
    """待批就是待批，**不再按时间自动作废**（研究者 2026-09-22 明确说"没必要"）。

    注意这条是"行为契约"：以后若有人以"安全"为名把 15 分钟过期逻辑加回来，
    这个测试会红 —— 因为那会让研究者刚看到卡片、转头回来点批准时已经作废。
    """

    request = _request()
    assert approvals.effective_status(request) == "pending"
    much_later = datetime.fromisoformat(request["expires_at"]) + timedelta(days=30)
    assert approvals.effective_status(request, now=much_later) == "pending"

    # 已裁决的状态照旧，不会被改写成别的
    request["status"] = "approved"
    assert approvals.effective_status(request, now=much_later) == "approved"


def test_zero_ttl_still_pending() -> None:
    request = _request(ttl_seconds=0)
    assert approvals.effective_status(request) == "pending"


def test_find_pending_refuses_closed_requests() -> None:
    """一次性没有取消：**已批准/已拒绝**都不能再批一次（但待批不会自己过期）。"""

    request = _request()
    record = _record(request)
    assert approvals.find_pending(record, request["id"]) is not None

    for status in ("approved", "denied"):
        request["status"] = status
        assert approvals.find_pending(record, request["id"]) is None


def test_conversation_grants_default_off_and_are_settable() -> None:
    """按对话的授权：默认全关；可以分别打开（两个是不同的事）。"""

    record: dict[str, Any] = {"id": "c1", "turns": []}
    assert approvals.grants(record) == {"full_access": False, "allow_exec": False, "tools": []}

    approvals.set_grants(record, full_access=True)
    assert approvals.grants(record)["full_access"] is True
    assert approvals.grants(record)["allow_exec"] is False, "开完全访问模式不等于放行高危"

    approvals.set_grants(record, allow_exec=True)
    assert approvals.grants(record) == {"full_access": True, "allow_exec": True, "tools": []}

    # 关掉"高危也放行"时，不该顺手把完全访问模式也关掉
    approvals.set_grants(record, allow_exec=False)
    assert approvals.grants(record)["full_access"] is True


def test_per_tool_grant_is_its_own_thing() -> None:
    """按工具授权（2026-09-25 用户口径）：批准一次 = 本对话内允许**这个工具**。

    - 加进清单后可以重复加，不会堆重复项；
    - 清单与那两个布尔互不干扰（关掉完全访问模式不该把工具清单清掉）。
    """

    record: dict[str, Any] = {"id": "c1", "turns": []}
    approvals.set_grants(record, tool="run_command")
    approvals.set_grants(record, tool="run_command")
    assert approvals.grants(record)["tools"] == ["run_command"], "同一个工具不该重复入列"

    approvals.set_grants(record, tool="files_on_computer")
    assert approvals.grants(record)["tools"] == ["run_command", "files_on_computer"]

    approvals.set_grants(record, full_access=True)
    approvals.set_grants(record, full_access=False)
    assert approvals.grants(record)["tools"] == ["run_command", "files_on_computer"]


def test_grants_summary_says_it_in_human_words() -> None:
    record: dict[str, Any] = {"id": "c1", "turns": []}
    assert "先问" in approvals.grants_summary(record)["note"]
    approvals.set_grants(record, full_access=True)
    assert "高危" in approvals.grants_summary(record)["note"]
    approvals.set_grants(record, allow_exec=True)
    assert "高危操作也直接执行" in approvals.grants_summary(record)["note"]

    # 只授权了某个工具时，摘要里要点出**是哪个工具**（否则研究者不知道批了什么）
    only_tool: dict[str, Any] = {"id": "c2", "turns": []}
    approvals.set_grants(only_tool, tool="run_command")
    note = approvals.grants_summary(only_tool)["note"]
    assert "run_command" in note and "高危" in note


def test_allows_matrix() -> None:
    """授权 → 能不能直接执行。

    这段判定决定了"会不会不打招呼就动研究者的机器"，所以把矩阵穷举钉住：

    | 本对话默认允许 | 完全访问模式 | 按工具授权 | 高危 | 结果 |
    |---|---|---|---|---|
    | 关 | 关 | 未命中 | 任意 | 先问 |
    | 关 | **开** | 未命中 | 否 | 直接做 |
    | 关 | **开** | 未命中 | **是** | **仍然先问** |
    | 关 | 关 | **命中** | 否 | **直接做** |
    | 关 | 关 | **命中** | **是** | **仍然先问** |
    | 关 | 关 | 未命中（别的工具） | 否 | 先问 |
    | **开** | 任意 | 任意 | 任意 | 直接做 |
    """

    off = {"full_access": False, "allow_exec": False, "tools": []}
    full = {"full_access": True, "allow_exec": False, "tools": []}
    exec_ = {"full_access": False, "allow_exec": True, "tools": []}
    both = {"full_access": True, "allow_exec": True, "tools": []}
    tool_grant = {"full_access": False, "allow_exec": False, "tools": ["run_command"]}

    assert approvals.allows(off, high_risk=False) is False
    assert approvals.allows(off, high_risk=True) is False
    assert approvals.allows(full, high_risk=False) is True
    assert approvals.allows(full, high_risk=True) is False, "完全访问模式不该放行高危"
    assert approvals.allows(exec_, high_risk=True) is True
    assert approvals.allows(both, high_risk=True) is True

    # 按工具授权（2026-09-25）：命中的工具直接做；高危一律不走这一档
    assert approvals.allows(tool_grant, high_risk=False, tool="run_command") is True
    assert approvals.allows(tool_grant, high_risk=True, tool="run_command") is False, (
        "高危永远逐次批准，按工具授权也不放行"
    )
    assert approvals.allows(tool_grant, high_risk=False, tool="files_on_computer") is False
    assert approvals.allows(tool_grant, high_risk=False) is False, "不传工具名时这一档不生效"


def test_three_decisions_are_the_contract() -> None:
    """卡上就三个选择（研究者原话），多一个少一个都要在这里红。"""

    assert approvals.VALID_DECISIONS == ("approve", "approve_conversation", "deny")


def test_find_and_find_pending_report_where_the_request_lives() -> None:
    request = _request()
    record = {"id": "c1", "turns": [{"role": "user"}, {"role": "assistant", "approvals": [request]}]}
    found = approvals.find(record, request["id"])
    assert found is not None
    assert found[0] == 1  # 轮次下标：裁决时要靠它把卡片换状态
    assert approvals.find(record, "nope") is None


# --------------------------------------------------------------------------- #
# 裁决与令牌
# --------------------------------------------------------------------------- #
def test_decide_records_who_and_when_but_never_the_plaintext_token() -> None:
    request = _request()
    record = _record(request)
    token = approvals.issue_token(request["id"])
    decided = approvals.decide(record, request["id"], status="approved", actor="owner", token=token)

    assert decided is not None
    assert decided["status"] == "approved"
    assert decided["decided_by"] == "owner"
    assert decided["decided_at"]
    # **会话文件里绝不能出现可用凭据**：只留指纹
    assert decided["token_fingerprint"] == approvals.token_fingerprint(token)
    assert token not in str(record), "明文令牌漏进会话记录了"


def test_issue_token_is_prefixed_unique_and_bound_to_the_request() -> None:
    first = approvals.issue_token("abc123")
    second = approvals.issue_token("abc123")
    assert first.startswith(f"{approvals.TOKEN_PREFIX}_abc123_")
    assert first != second, "每次签发都必须是新令牌，不能复用"


def test_decide_rejects_unknown_status_and_unknown_request() -> None:
    record = _record(_request())
    request_id = record["turns"][0]["approvals"][0]["id"]
    try:
        approvals.decide(record, request_id, status="probably", actor="owner")
    except ValueError as exc:
        assert "非法裁决状态" in str(exc)
    else:  # pragma: no cover
        raise AssertionError("非法状态必须抛，不能静默写进去")
    assert approvals.decide(record, "nope", status="approved", actor="owner") is None


def test_mark_consumed_is_recorded() -> None:
    request = _request()
    record = _record(request)
    approvals.mark_consumed(record, request["id"])
    assert request["consumed_at"]
    approvals.mark_consumed(record, "nope")  # 不存在也不炸


def test_attach_appends_to_the_owning_turn() -> None:
    record = {"id": "c1", "turns": [{"role": "user"}, {"role": "assistant"}]}
    request = _request()
    approvals.attach(record, 1, request)
    assert record["turns"][1]["approvals"] == [request]
    assert "approvals" not in record["turns"][0]
