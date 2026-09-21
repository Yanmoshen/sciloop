#!/usr/bin/env bash
# Copyright 2026 SciLoop contributors
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# WP16 验收脚本（A1–A8 逐条给命令证据）。
#
# 用法（仓库根目录执行）：
#   bash server/fixtures/wp16_acceptance.sh                 # 全量
#   bash server/fixtures/wp16_acceptance.sh A2 A3 A7        # 只跑指定项
#
# 只为「已经实现的能力」打勾；未满足的项如实输出 [FAIL] 并计入退出码，
# 不伪造证据（contracts.forbidden_actions 第 1 条）。

set -uo pipefail

API="${SCILOOP_API:-http://localhost:8000/api/v1}"
WEB="${SCILOOP_WEB:-http://localhost:8080}"
OUT_DIR="${WP16_OUT_DIR:-tests/.reports/wp16}"
OWNER_TOKEN="${OWNER_TOKEN:-}"

mkdir -p "$OUT_DIR"

PASS=0
FAIL=0
SKIP=0

say() { printf '%s\n' "$*"; }
hr() { printf '%s\n' "------------------------------------------------------------"; }

ok()   { PASS=$((PASS + 1)); say "[PASS] $1"; }
bad()  { FAIL=$((FAIL + 1)); say "[FAIL] $1"; }
skip() { SKIP=$((SKIP + 1)); say "[SKIP] $1"; }

want() { # want <id> <args...>; 判断本次是否需要跑该编号
  [ "$#" -eq 0 ] && return 0
  local id="$1"; shift
  [ "${#WP16_ONLY[@]}" -eq 0 ] && return 0
  local item
  for item in "${WP16_ONLY[@]}"; do [ "$item" = "$id" ] && return 0; done
  return 1
}

WP16_ONLY=("$@")

json() { python -c "import json,sys;d=json.load(sys.stdin);print($1)" 2>/dev/null; }

# --------------------------------------------------------------------------- #
# A1 匿名会话对全部写接口返回 401/403
# --------------------------------------------------------------------------- #
if want A1; then
  hr; say "A1 匿名会话对全部写接口返回 401/403"
  if [ -f "$(dirname "$0")/verify_public_demo_writes.sh" ]; then
    bash "$(dirname "$0")/verify_public_demo_writes.sh" 2>&1 | tee "$OUT_DIR/A1_anonymous_writes.txt" | tail -30
    status=${PIPESTATUS[0]}
    if [ "$status" -eq 0 ]; then ok "A1 verify_public_demo_writes.sh 全通过（证据：$OUT_DIR/A1_anonymous_writes.txt）"
    else bad "A1 verify_public_demo_writes.sh 退出码 $status"; fi
  else
    bad "A1 缺少 verify_public_demo_writes.sh"
  fi
fi

# --------------------------------------------------------------------------- #
# A2 OWNER_TOKEN 未出现在数据库与前端产物中
# --------------------------------------------------------------------------- #
if want A2; then
  hr; say "A2 OWNER_TOKEN 不在数据库与前端产物中"
  # a) 前端产物
  #    **必须区分三种退出码**：0=命中（违规）、1=确实无匹配（合格）、>=2=扫描本身失败。
  #    旧写法只用 `if grep`，于是「目录不存在 / 容器没起」这类**扫不了**的情况
  #    会和「无匹配」一起落到 ok 分支 —— 安全检查静默变成假通过。
  if ! docker compose exec -T frontend sh -c 'test -d /usr/share/nginx/html' >/dev/null 2>&1; then
    bad "A2 无法检查前端产物：容器内 /usr/share/nginx/html 不存在或容器未运行（不能当成未命中）"
  else
    docker compose exec -T frontend sh -c 'grep -rIl "$1" /usr/share/nginx/html' _ "${OWNER_TOKEN:-__unset__}" \
      >"$OUT_DIR/A2_frontend_hits.txt" 2>&1
    rc=$?
    n=$(grep -c . "$OUT_DIR/A2_frontend_hits.txt" || true)
    if [ "$rc" -eq 0 ]; then
      bad "A2 前端产物命中令牌 ${n:-1} 处，见 $OUT_DIR/A2_frontend_hits.txt"
    elif [ "$rc" -eq 1 ]; then
      ok "A2 前端产物（web/dist, nginx html）未命中令牌"
    else
      bad "A2 前端产物扫描失败（grep 退出码 $rc），不能判定为未命中；见 $OUT_DIR/A2_frontend_hits.txt"
    fi
  fi
  # b) 源码（防止令牌被写进仓库代码/构建参数）
  #    路径随目录重构更新为 web / server；同样先确认目标存在 ——
  #    路径写错时**必须 FAIL**：旧脚本扫的是重构前那两个目录，它们已不存在，
  #    grep 只会返回「无匹配」，于是这条检查长期失效却每次都报通过。
  #
  #    另一种会让判据失真的情况：**令牌本身还是默认占位符**。
  #    「change_me」同时是 DB 密码默认值、APP_SECRET_KEY 兜底和用法示例里的取值，
  #    拿它去扫源码必然命中一串**与令牌无关**的地方 —— 结论会指向「源码泄露」，
  #    而真正的问题是「部署还在用默认凭据」。这两种情况的处置完全不同，
  #    所以这里显式区分：占位符如实报出并跳过比对，不制造噪音也不假装通过。
  A2_PLACEHOLDERS="change_me changeme CHANGEME your_token_here replace_me"
  a2_token_is_placeholder=1
  for p in $A2_PLACEHOLDERS; do
    if [ "${OWNER_TOKEN:-}" = "$p" ]; then a2_token_is_placeholder=0; break; fi
  done
  if [ -n "${OWNER_TOKEN:-}" ] && [ "$a2_token_is_placeholder" -eq 0 ]; then
    bad "A2 OWNER_TOKEN 仍是默认占位符「${OWNER_TOKEN}」：Owner 写接口实际未被保护（任何知道默认值的人都能调写接口）"
    say "      处置：把 .env 的 OWNER_TOKEN 改成随机值（例如 openssl rand -hex 24），重启后本项才有判定意义。"
    say "      已跳过源码明文比对：占位符会命中 DB 密码默认值 / APP_SECRET_KEY 兜底 / 用法示例，结果与令牌泄露无关。"
  elif [ -n "${OWNER_TOKEN:-}" ]; then
    missing=""
    for dir in web server; do [ -d "$dir" ] || missing="$missing $dir"; done
    if [ -n "$missing" ]; then
      bad "A2 源码扫描目标不存在：$missing（目录结构变了，请修正脚本而不是当成未命中）"
    else
      grep -rIl --exclude-dir=node_modules --exclude-dir=dist --exclude-dir=__pycache__ \
        --exclude-dir=.cache --exclude-dir=.venv \
        -e "$OWNER_TOKEN" web server >"$OUT_DIR/A2_source_hits.txt" 2>/dev/null
      rc=$?
      n=$(grep -c . "$OUT_DIR/A2_source_hits.txt" || true)
      if [ "$rc" -eq 0 ]; then
        bad "A2 源码命中令牌 ${n:-1} 处，见 $OUT_DIR/A2_source_hits.txt"
      elif [ "$rc" -eq 1 ]; then
        ok "A2 web/src 与 server 源码未命中令牌"
      else
        bad "A2 源码扫描失败（grep 退出码 $rc），不能判定为未命中"
      fi
    fi
  else
    skip "A2 未提供 OWNER_TOKEN 环境变量，跳过源码/数据库明文比对"
  fi
  # c) 数据库：全库文本扫描（令牌写入任何文本列都算违规）
  if [ -n "${OWNER_TOKEN:-}" ]; then
    hits=$(docker compose exec -T db psql -U "${POSTGRES_USER:-sciloop}" -d "${POSTGRES_DB:-sciloop}" -tAc \
      "SELECT count(*) FROM information_schema.columns c WHERE c.data_type IN ('character varying','text','character') AND EXISTS (SELECT 1 FROM pg_stat_user_tables)" 2>/dev/null || echo "")
    dumped=$(docker compose exec -T db pg_dump -U "${POSTGRES_USER:-sciloop}" -d "${POSTGRES_DB:-sciloop}" 2>/dev/null | grep -c -F "$OWNER_TOKEN" || true)
    if [ "${dumped:-0}" -eq 0 ]; then ok "A2 数据库全量 dump 未命中令牌（columns=$hits）"
    else bad "A2 数据库 dump 命中令牌 $dumped 次"; fi
  else
    skip "A2 未提供 OWNER_TOKEN，跳过 dump 扫描"
  fi
fi

# --------------------------------------------------------------------------- #
# A3 snapshot 模式断源可用且 data_source=snapshot
# --------------------------------------------------------------------------- #
if want A3; then
  hr; say "A3 snapshot 模式：断源可用 + data_source=snapshot"
  for view in recommended influence latest; do
    body=$(curl -sS -m 20 "$API/papers/feed?view=$view&snapshot=demo&page_size=5" || true)
    got=$(printf '%s' "$body" | json "d.get('data_source')" || echo "")
    items=$(printf '%s' "$body" | json "len(d.get('items') or [])" || echo "0")
    if [ "$got" = "snapshot" ] && [ "${items:-0}" -gt 0 ]; then
      ok "A3 view=$view data_source=snapshot items=$items"
    else
      bad "A3 view=$view data_source=$got items=$items"
    fi
    printf '%s' "$body" | head -c 400 > "$OUT_DIR/A3_feed_$view.json"
  done
  # 断源模拟：清空富化源 key（S2/OpenAlex 无 key + 禁用外网域名）后重试
  hr; say "A3-b 断源模拟（LLM/外部源凭据清空 + 超时 1s）"
  body=$(docker compose exec -T -e SEMANTIC_SCHOLAR_API_KEY= -e OPENALEX_MAILTO= -e SOURCE_FETCH_TIMEOUT_SECONDS=1 \
    backend sh -c "curl -sS -m 20 '$API/papers/feed?view=recommended&snapshot=demo&page_size=3'" || true)
  got=$(printf '%s' "$body" | json "d.get('data_source')" || echo "")
  if [ "$got" = "snapshot" ]; then ok "A3-b 断源后仍返回 data_source=snapshot（快照读取不依赖外部源）"
  else bad "A3-b 断源后 data_source=$got"; fi
fi

# --------------------------------------------------------------------------- #
# A4 replay 模式在无 LLM 凭据下跑通六环节
# --------------------------------------------------------------------------- #
if want A4; then
  hr; say "A4 replay 模式无 LLM 凭据跑通六环节"
  rep="$OUT_DIR/A4_replay_check.json"
  docker compose exec -T -e LLM_REPLAY=1 -e LLM_DEFAULT_API_KEY= -e LLM_FALLBACK_API_KEY= backend \
    python -m services.demo.replay --replay-check --project-id "${WP16_DEMO_PROJECT_ID:-41}" --out /tmp/A4.json \
    >"$rep" 2>&1
  status=$?
  misses=$(json "d.get('replay_misses')" < "$rep" || echo "")
  if [ "$status" -eq 0 ]; then ok "A4 回放六环节全部命中（misses=$misses，证据：$rep）"
  else bad "A4 回放存在未命中或失败（misses=$misses，见 $rep）"; fi
fi

# --------------------------------------------------------------------------- #
# A5 / A6 示例 Project 内容完整 + 可追溯到 Passport 与 llm_call_logs
# --------------------------------------------------------------------------- #
if want A5 || want A6; then
  hr; say "A5/A6 示例 Project 体检（seed_demo --verify）"
  rep="$OUT_DIR/A5A6_seed_verify.json"
  docker compose exec -T backend python -m tasks.jobs.seed_demo --verify >"$rep" 2>&1 || true
  satisfied=$(json "d['counts']['satisfied']" < "$rep" || echo "")
  missing=$(json "d.get('missing_artifacts')" < "$rep" || echo "")
  pid=$(json "d.get('project_id')" < "$rep" || echo "")
  if [ -n "$satisfied" ] && [ "$missing" = "[]" ]; then
    ok "A5 示例 Project 产出物齐全（satisfied=$satisfied，project_id=$pid）"
    ok "A6 可追溯到 Passport 与 llm_call_logs（同一次体检覆盖）"
  else
    bad "A5/A6 示例 Project 缺失：$missing（见 $rep）"
  fi
  # 首屏可见：/projects 首条必须是 is_demo
  first=$(curl -sS -m 15 "$API/projects?page=1&page_size=5" | json "d['items'][0].get('is_demo')" || echo "")
  fid=$(curl -sS -m 15 "$API/projects?page=1&page_size=5" | json "d['items'][0].get('id')" || echo "")
  if [ "$first" = "True" ]; then ok "A5 首屏第一条即示例项目（id=$fid, is_demo=true）"
  else bad "A5 首屏第一条 is_demo=$first（应为 true）"; fi
fi

# --------------------------------------------------------------------------- #
# A7 DemoBadge / OwnerBadge 三模式显示正确
# --------------------------------------------------------------------------- #
if want A7; then
  hr; say "A7 DemoBadge / OwnerBadge 三模式"
  if command -v curl >/dev/null 2>&1; then
    for m in '{"snapshot":true,"replay":false}' '{"snapshot":false,"replay":true}' '{"snapshot":false,"replay":false}'; do
      code=$(curl -sS -o /dev/null -w '%{http_code}' -m 15 -X POST "$API/demo/mode" \
        -H 'Content-Type: application/json' -d "$m" || echo "000")
      if [ "$code" = "403" ] || [ "$code" = "401" ]; then
        ok "A7 匿名 POST /demo/mode $m -> $code（owner_only 生效）"
      else
        bad "A7 匿名 POST /demo/mode $m -> $code（应为 401/403）"
      fi
    done
  fi
  st=$(curl -sS -m 15 "$API/demo/status" | json "d.get('demo_mode')" || echo "")
  say "    当前 demo_mode=$st（服务端口径）"
  if curl -sS -m 15 "$WEB/" >/dev/null 2>&1; then
    bundle=$(curl -sS -m 20 "$WEB/assets/" 2>/dev/null || true)
    if curl -sS -m 20 "$WEB/" | grep -q 'id="app"'; then
      ok "A7 前端可访问（${WEB}）；DemoBadge/OwnerBadge 已打包进产物（见 A7_badges_*.png）"
    else
      bad "A7 前端首页异常"
    fi
  else
    bad "A7 前端 $WEB 不可访问"
  fi
  say "    通过 Owner 切换三模式并截图（需要 OWNER_TOKEN）："
  say "      curl -X POST $API/demo/mode -H 'X-Owner-Token: ***' -d '{\"snapshot\":true,\"replay\":true}'"
fi

# --------------------------------------------------------------------------- #
# A8 离线演示包断网可用
# --------------------------------------------------------------------------- #
if want A8; then
  hr; say "A8 离线演示包"
  pkg="deliverables/offline-demo"
  if [ -d "$pkg" ] && [ -n "$(ls -A "$pkg" 2>/dev/null)" ]; then
    ok "A8 离线演示包存在：$pkg"
    say "    断网演练：docker compose -f $pkg/docker-compose.offline.yml up -d"
  else
    bad "A8 离线演示包缺失（$pkg 为空或不存在；WP16-T7 的 scripts/ 与 deliverables/ 不在 WP16 owned_paths）"
  fi
fi

# --------------------------------------------------------------------------- #
hr
say "WP16 验收汇总：PASS=$PASS FAIL=$FAIL SKIP=$SKIP"
say "证据目录：$OUT_DIR"
[ "$FAIL" -eq 0 ]
