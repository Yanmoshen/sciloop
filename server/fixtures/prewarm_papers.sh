#!/usr/bin/env bash
# Copyright 2026 SciLoop contributors
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# WP16 演示前论文预热（附录 H.3 清单第 1 项：抓取 ≥300 篇 + 全文解析）。
#
# 为什么不是简单跑一次 `--limit 300`
# ----------------------------------
# arXiv 对「同一进程内的连续分页请求」会间歇性返回 **406 Not Acceptable**：
# 实测 `iter_papers` 第 1 页成功（100 篇）、第 2 页立即 406 中断，
# 因此单次调用上限约 100 篇（诊断记录见 _progress.json 的 contract_changes_requested）。
#
# 本脚本用**分窗 + 间隔**绕开该限制：每个窗口的「最新 100 篇」互不相同，
# 窗口之间 sleep 给 arXiv 限流窗口留出冷却时间，从而增量累加到 ≥300 篇。
# 所有取数都是真实 arXiv / OpenAlex 调用，**不写入任何编造的论文元数据**。
#
# 2026-10-11：不再使用 Docker —— 任务直接在本机 server/ 下以 `python -m tasks.jobs.*` 执行，
# 报告写到仓库内的 `tests/.reports/wp16/`（不再往容器 /tmp 里丢）。
#
# 用法（仓库根目录）：
#   bash server/fixtures/prewarm_papers.sh                 # 默认目标 320 篇
#   TARGET=300 WINDOWS_STR="2026-09-10 2026-09-17" bash server/fixtures/prewarm_papers.sh
#   PARSE_BATCH=8 bash server/fixtures/prewarm_papers.sh   # 顺带补 8 篇全文解析

set -uo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
cd "$REPO_ROOT" || exit 3

TARGET="${TARGET:-320}"
COOLDOWN="${COOLDOWN:-25}"
PARSE_BATCH="${PARSE_BATCH:-8}"
OUT_DIR="${WP16_OUT_DIR:-tests/.reports/wp16}"
mkdir -p "$OUT_DIR"
OUT_ABS="$REPO_ROOT/$OUT_DIR"

REPO_ROOT_WIN="$(cd "$REPO_ROOT" && pwd -W 2>/dev/null)"
win_path() {
  local p="$1"
  if command -v cygpath >/dev/null 2>&1; then
    cygpath -w "$p" 2>/dev/null && return
  fi
  if [ -n "$REPO_ROOT_WIN" ]; then
    case "$p" in
      "$REPO_ROOT"/*) printf '%s%s' "$REPO_ROOT_WIN" "${p#"$REPO_ROOT"}"; return ;;
    esac
  fi
  printf '%s' "$p"
}

detect_python() {
  local cand
  if [ -n "${SCILOOP_PYTHON:-}" ] && [ -x "${SCILOOP_PYTHON}" ]; then
    printf '%s' "$SCILOOP_PYTHON"; return 0
  fi
  for cand in python python3 py; do
    if command -v "$cand" >/dev/null 2>&1; then printf '%s' "$cand"; return 0; fi
  done
  if [ -n "${LOCALAPPDATA:-}" ]; then
    for cand in "$LOCALAPPDATA"/Programs/Python/Python3*/python.exe; do
      [ -x "$cand" ] && { printf '%s' "$cand"; return 0; }
    done
  fi
  return 1
}

PY_BIN="$(detect_python || true)"
if [ -z "$PY_BIN" ]; then
  echo "未找到 Python 解释器（可设 SCILOOP_PYTHON 指定绝对路径）" >&2
  exit 3
fi

# 任务入口：在本机 server/ 下执行（原 `docker compose exec backend` 的对应动作）
backend() { ( cd "$REPO_ROOT/server" && "$PY_BIN" "$@" ); }

# Git Bash 偶尔没有 sleep：退化为 Python 计时
cool_down() {
  sleep "$1" 2>/dev/null || "$PY_BIN" -c "import sys, time; time.sleep(float(sys.argv[1]))" "$1"
}

# 默认窗口：从最近往前推，每窗 ~7 天（arXiv 按 submittedDate 倒序取该窗最新 100 篇）
DEFAULT_WINDOWS=(
  "2026-09-05 2026-09-17"
  "2026-08-25 2026-09-05"
  "2026-08-15 2026-08-25"
  "2026-08-05 2026-08-15"
  "2026-07-25 2026-08-05"
)

read -r -a WINDOWS <<<"${WINDOWS_STR:-${DEFAULT_WINDOWS[*]}}"

paper_count() {
  backend python -c 'from sqlalchemy import text; from db.session import sync_engine; print(sync_engine.connect().execute(text("SELECT COUNT(*) FROM papers")).scalar_one())' 2>/dev/null | tr -d '[:space:]'
}

count=$(paper_count)
echo "== 预热起点：papers=$count，目标=$TARGET =="

i=0
while [ "${count:-0}" -lt "$TARGET" ]; do
  # windows 按 (from, to) 两两成对
  idx=$(( (i * 2) % ${#WINDOWS[@]} ))
  from="${WINDOWS[$idx]}"
  to="${WINDOWS[$((idx + 1))]:-}"
  i=$((i + 1))
  if [ "$i" -gt 12 ]; then
    echo "-- 已达最大轮次（12），停止；当前 papers=$count"
    break
  fi

  report="$OUT_ABS/prewarm_$i.json"
  echo "-- 第 $i 轮：window=$from..$to"
  if [ -z "$to" ]; then
    backend python -m tasks.jobs.fetch_papers --no-skip-existing --limit 100 \
      --date-from "$from" --sources arxiv,openalex --out "$(win_path "$report")" >/dev/null 2>&1
  else
    backend python -m tasks.jobs.fetch_papers --no-skip-existing --limit 100 \
      --date-from "$from" --date-to "$to" --sources arxiv,openalex --out "$(win_path "$report")" >/dev/null 2>&1
  fi
  summary="$("$PY_BIN" - "$(win_path "$report")" <<'PY' 2>/dev/null
import json
import sys

try:
    with open(sys.argv[1], "r", encoding="utf-8") as fh:
        data = json.load(fh)
except Exception as exc:
    print("report_read_failed", exc)
    raise SystemExit

arxiv = (data.get("by_source") or {}).get("arxiv") or {}
print("created=%s discovered=%s total=%s arxiv_ok=%s arxiv_failed=%s err=%s" % (
    (data.get("counts") or {}).get("created"), data.get("discovered"), data.get("papers_total"),
    arxiv.get("ok"), arxiv.get("failed"), arxiv.get("last_error")))
PY
)"
  echo "   $summary"
  prev=$count
  count=$(paper_count)
  echo "   papers: $prev -> $count"
  if [ "$count" -lt "$TARGET" ]; then cool_down "$COOLDOWN"; fi
done

echo "== 预热结果：papers=$count（目标 $TARGET） =="

if [ "${PARSE_BATCH:-0}" -gt 0 ]; then
  parse_report="$OUT_ABS/prewarm_parse.json"
  echo "== 顺带补全文解析（小批量 $PARSE_BATCH 篇；WP05 的 parse_fulltext job） =="
  backend python -m tasks.jobs.parse_fulltext --limit "$PARSE_BATCH" \
    --delay-seconds 1.5 --out "$(win_path "$parse_report")" 2>&1 | tail -3
  "$PY_BIN" - "$(win_path "$parse_report")" <<'PY' 2>/dev/null
import json
import sys

try:
    with open(sys.argv[1], "r", encoding="utf-8") as fh:
        data = json.load(fh)
except Exception as exc:
    print("parse_report_read_failed:", exc)
    raise SystemExit
print("parse_status_counts =", json.dumps(data.get("counts") or data.get("status_counts") or {}, ensure_ascii=False))
PY
fi

echo "== 预热脚本结束 =="
