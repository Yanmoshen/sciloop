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
def test_expired_is_computed_on_read() -> None:
    request = _request()
    assert approvals.effective_status(request) == "pending"
    later = datetime.fromisoformat(request["expires_at"]) + timedelta(seconds=1)
    assert approvals.effective_status(request, now=later) == "expired"
    # 已裁决的状态不会被"过期"覆盖掉 —— 批过就是批过
    request["status"] = "approved"
    assert approvals.effective_status(request, now=later) == "approved"


def test_zero_ttl_expires_immediately() -> None:
    request = _request(ttl_seconds=0)
    assert approvals.effective_status(request) == "expired"


def test_find_pending_refuses_closed_requests() -> None:
    """**一次性**的关键就在这条：已批准/已拒绝/已过期都不能再批一次。"""

    request = _request()
    record = _record(request)
    assert approvals.find_pending(record, request["id"]) is not None

    for status in ("approved", "denied"):
        request["status"] = status
        assert approvals.find_pending(record, request["id"]) is None

    request["status"] = "pending"
    record2 = _record(_request(ttl_seconds=0))
    assert approvals.find_pending(record2, record2["turns"][0]["approvals"][0]["id"]) is None


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
