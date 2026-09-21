#!/usr/bin/env bash
# Copyright 2026 SciLoop contributors
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# WP16 验收脚本：public_demo 面匿名写操作必须被拒（401/403）。
#
# 对应
#   - 验收项 WP16-A1（匿名会话对全部写接口返回 401/403）
#   - 冒烟用例第 12 条（public_demo 匿名写操作被拒）
#
# 用法（宿主机 Git Bash / Linux 均可）：
#   bash server/fixtures/verify_public_demo_writes.sh
#   BASE=http://localhost:8000 OWNER_TOKEN=change_me bash server/fixtures/verify_public_demo_writes.sh
#   PROBE_ALL=1 bash server/fixtures/verify_public_demo_writes.sh   # 额外逐条探测运行时发现的全部写路由
#
# 输出：每个探针一行 `[PASS|FAIL] STATUS  METHOD PATH  code=...`，末尾给出汇总。
# 退出码：0 = 全部通过；1 = 存在未被拒绝的写操作（回归！）。
#
# 安全说明
#   - 只发送**匿名**请求（不带 X-Owner-Token），探针体故意给空/最小 JSON。
#   - 若某写路由**忘了**挂 Owner 守卫，其处理函数可能被真实执行——这正是本脚本要发现的问题。
#     因此请在演示/测试环境运行，不要在生产数据上跑。
#   - OWNER_TOKEN 只用于读取审计端点（拉取路由清单），**不会被打印、不会写入任何文件**。

set -u

BASE="${BASE:-http://localhost:8000}"
OWNER_TOKEN="${OWNER_TOKEN:-}"
PROBE_ALL="${PROBE_ALL:-0}"
OUT_DIR="${OUT_DIR:-.tmp/wp16-access}"
TIMEOUT="${TIMEOUT:-10}"
mkdir -p "$OUT_DIR" 2>/dev/null || true

PYTHON_BIN="$(command -v python3 || command -v python || true)"

# --------------------------------------------------------------------------- #
# 契约清单（contracts.api_contract.owner_only 逐条落地为可执行探针）
# --------------------------------------------------------------------------- #
CONTRACT_PROBES=(
  "POST|/api/v1/passports/1/rerun"
  "POST|/api/v1/pipelines/1/run"
  "POST|/api/v1/pipelines/1/switch-mode"
  "POST|/api/v1/models/configs"
  "POST|/api/v1/demo/mode"
  "POST|/api/v1/demo/projects/seed"
  "POST|/api/v1/pipelines/1/human-labels"
  "POST|/api/v1/decisions/1/approve"
  "DELETE|/api/v1/models/configs/1"
)

# 受控写断言：这些路径若未挂 Owner 守卫会被执行，因此只发最小无害体
EXTRA_PROBES=(
  "POST|/api/v1/papers/fetch"
  "POST|/api/v1/papers/1/parse"
  "POST|/api/v1/papers/1/card"
  "POST|/api/v1/pipelines/1/pause"
  "POST|/api/v1/pipelines/1/resume"
  "POST|/api/v1/pipelines/1/stop"
  "POST|/api/v1/pipelines/1/intervene"
  "POST|/api/v1/pipelines/1/human-labels"
  "POST|/api/v1/decisions/1/evaluate-risk"
  "PUT|/api/v1/models/routing"
  "PATCH|/api/v1/models/configs/1"
  "POST|/api/v1/models/configs/1/test"
)

body_for() {
  # $1 = method, $2 = path -> 最小请求体（避免缺字段的 422 混淆断言）
  local path="$2"
  case "$path" in
    */switch-mode) echo '{"mode":"auto"}' ;;
    */evaluate-risk) echo '{"persist":true}' ;;
    */intervene) echo '{"node":"N2","action":"approve","payload":{}}' ;;
    */human-labels) echo '{"labels":[]}' ;;
    */run|*/parse|*/card) echo '{}' ;;
    *) echo '{}' ;;
  esac
}

declare -A SEEN=()
PASS=0
FAIL=0
ROWS=()

probe() {
  local method="$1" path="$2"
  local key="${method}|${path}"
  [ -n "${SEEN[$key]:-}" ] && return 0
  SEEN[$key]=1
  local body code status
  body="$(body_for "$method" "$path")"
  # 文件名必须对 Windows/Git Bash 安全：去掉 query、把非法字符全部换成下划线
  local slug
  slug="$(printf '%s_%s' "$method" "$path" | tr -c 'A-Za-z0-9_' '_' | cut -c1-120)"
  local out="$OUT_DIR/${slug}.json"
  # 注意：**不要**写成 `$(curl ... || echo 000)`——curl 在 Windows/Git Bash 上可能
  # 因写文件返回非 0（exit 23）却已经用 -w 打印了真实状态，`|| echo 000` 会把
  # 状态拼成 "000403"，导致所有写操作被误判为 FAIL。这里直接忽略退出码。
  status="$(curl -s -o "$out" -w '%{http_code}' --connect-timeout 5 -m "$TIMEOUT" \
    -X "$method" "$BASE$path" -H 'Content-Type: application/json' -d "$body" 2>/dev/null)"
  [ -n "$status" ] || status="000"
  status="${status:0:3}"
  code=""
  if [ -n "$PYTHON_BIN" ] && [ -s "$out" ]; then
    code="$("$PYTHON_BIN" - "$out" <<'PY' 2>/dev/null || true
import json, sys
try:
    with open(sys.argv[1], "rb") as handle:
        payload = json.loads(handle.read().decode("utf-8", "replace") or "{}")
    print(str(payload.get("code") or payload.get("detail", {}).get("code") or "-"))
except Exception:
    print("-")
PY
)"
  fi
  local verdict="FAIL"
  case "$status" in
    401|403) verdict="PASS"; PASS=$((PASS + 1)) ;;
    *) FAIL=$((FAIL + 1)) ;;
  esac
  printf '[%s] HTTP %s  %-6s %-46s code=%s\n' "$verdict" "$status" "$method" "$path" "${code:--}"
  ROWS+=("$verdict|$status|$method|$path|$code")
}

echo "=== WP16-A1  public_demo anonymous write rejection matrix ==="
echo "base=$BASE  probe_all=$PROBE_ALL  time=$(date -u +%Y-%m-%dT%H:%M:%SZ 2>/dev/null || echo '-')"
echo

echo "-- contract.api_contract.owner_only --"
for item in "${CONTRACT_PROBES[@]}"; do
  probe "${item%%|*}" "${item#*|}"
done

echo
echo "-- other write endpoints (curated) --"
for item in "${EXTRA_PROBES[@]}"; do
  probe "${item%%|*}" "${item#*|}"
done

if [ "$PROBE_ALL" = "1" ]; then
  echo
  echo "-- runtime-discovered write routes (from GET /owner/audit/write-endpoints) --"
  if [ -z "$OWNER_TOKEN" ]; then
    echo "SKIP: PROBE_ALL=1 requires OWNER_TOKEN to read the audit endpoint"
  elif [ -z "$PYTHON_BIN" ]; then
    echo "SKIP: python not found for JSON parsing"
  else
    audit="$OUT_DIR/audit.json"
    curl -s -m "$TIMEOUT" -H "X-Owner-Token: $OWNER_TOKEN" \
      "$BASE/api/v1/owner/audit/write-endpoints" -o "$audit" 2>/dev/null || true
    routes="$("$PYTHON_BIN" - "$audit" <<'PY' 2>/dev/null || true
import json, re, sys
try:
    with open(sys.argv[1], "rb") as handle:
        payload = json.loads(handle.read().decode("utf-8", "replace") or "{}")
except Exception:
    raise SystemExit(0)
# 公开白名单（contracts.api_contract.public_demo_allowed）：POST /passports/{id}/replay 限额放行，
# 它不是「漏挂守卫」，不能算 FAIL，单独标 informational。
# 审计端点已给出权威布尔字段 `public_demo_allowed`；缺失时才回退到契约里的正则。
DEFAULT_PUBLIC_ALLOW = [r"POST ^/api/v1/passports/[^/]+/replay/?$"]
allow = payload.get("public_write_allowlist") or DEFAULT_PUBLIC_ALLOW
if isinstance(allow, str):
    allow = [allow]
patterns = []
for item in allow:
    if isinstance(item, dict):
        item = item.get("pattern") or item.get("rule") or ""
    text = str(item).strip()
    parts = text.split(" ", 1)
    if len(parts) == 2 and parts[1]:
        patterns.append(re.compile(parts[1]))
    elif text.startswith("^"):
        patterns.append(re.compile(text))
for route in payload.get("routes") or []:
    raw = str(route.get("path") or "")
    path = re.sub(r"\{[^}]+\}", "1", raw)
    # 白名单正则形如 "POST ^/api/v1/passports/[^/]+/replay/?$"：方法已单独匹配，
    # 这里只用 path 部分喂给正则（否则 ^ 锚点会被 "POST " 前缀顶掉）。
    path_only = "/" + raw.lstrip("/")
    for method in route.get("write_methods") or []:
        if route.get("public_demo_allowed") is True:
            public = "1"
        elif route.get("public_demo_allowed") is False:
            public = "0"
        else:
            public = "1" if any(p.search(path_only) for p in patterns) else "0"
        print(f"{method}|{path}|{public}")
PY
)"
    while IFS= read -r line; do
      [ -z "$line" ] && continue
      method="${line%%|*}"
      rest="${line#*|}"
      path="${rest%%|*}"
      public="${rest##*|}"
      if [ "$public" = "1" ]; then
        echo "[INFO] SKIP  $method $path  （public_demo_allowed 白名单：限额回放，非漏挂守卫）"
        continue
      fi
      probe "$method" "$path"
    done <<< "$routes"
  fi
fi

echo
echo "=== summary ==="
echo "pass=$PASS fail=$FAIL total=$((PASS + FAIL))"
if [ "$FAIL" -gt 0 ]; then
  echo "FAILED probes (write operations NOT rejected anonymously):"
  for row in "${ROWS[@]}"; do
    case "$row" in FAIL*) echo "  $row" ;; esac
  done
  echo "RESULT: FAIL —— public_demo 面存在匿名可执行的写操作（冒烟用例第 12 条不通过）"
  exit 1
fi
echo "RESULT: PASS —— 全部写操作在 public_demo 面被拒（401/403），冒烟用例第 12 条通过"
exit 0
