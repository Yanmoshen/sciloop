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
# 用法（仓库根目录）：
#   bash backend/app/fixtures/prewarm_papers.sh                 # 默认目标 320 篇
#   TARGET=300 WINDOWS="2026-09-10 2026-09-17" bash backend/app/fixtures/prewarm_papers.sh
#   PARSE_BATCH=8 bash backend/app/fixtures/prewarm_papers.sh   # 顺带补 8 篇全文解析

set -uo pipefail

TARGET="${TARGET:-320}"
COOLDOWN="${COOLDOWN:-25}"
PARSE_BATCH="${PARSE_BATCH:-8}"
OUT_DIR="${WP16_OUT_DIR:-tests/.reports/wp16}"
mkdir -p "$OUT_DIR"

# 默认窗口：从最近往前推，每窗 ~7 天（arXiv 按 submittedDate 倒序取该窗最新 100 篇）
DEFAULT_WINDOWS=(
  "2026-09-05 2026-09-17"
  "2026-08-25 2026-09-05"
  "2026-08-15 2026-08-25"
  "2026-08-05 2026-08-15"
  "2026-07-25 2026-08-05"
)

read -r -a WINDOWS <<<"${WINDOWS_STR:-${DEFAULT_WINDOWS[*]}}"

in_container() { docker compose exec -T backend "$@"; }

paper_count() {
  docker compose exec -T db psql -U "${POSTGRES_USER:-sciloop}" -d "${POSTGRES_DB:-sciloop}" -tAc \
    "SELECT COUNT(*) FROM papers" 2>/dev/null | tr -d '[:space:]'
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

  echo "-- 第 $i 轮：window=$from..$to"
  if [ -z "$to" ]; then
    in_container python -m app.tasks.jobs.fetch_papers --no-skip-existing --limit 100 \
      --date-from "$from" --sources arxiv,openalex --out "/tmp/prewarm_$i.json" >/dev/null 2>&1
  else
    in_container python -m app.tasks.jobs.fetch_papers --no-skip-existing --limit 100 \
      --date-from "$from" --date-to "$to" --sources arxiv,openalex --out "/tmp/prewarm_$i.json" >/dev/null 2>&1
  fi
  summary=$(in_container python -c "
import json
try:
    d = json.load(open('/tmp/prewarm_$i.json'))
except Exception as exc:
    print('report_read_failed', exc); raise SystemExit
a = (d.get('by_source') or {}).get('arxiv') or {}
print('created=%s discovered=%s total=%s arxiv_ok=%s arxiv_failed=%s err=%s' % (
    d.get('counts', {}).get('created'), d.get('discovered'), d.get('papers_total'),
    a.get('ok'), a.get('failed'), a.get('last_error')))
" 2>/dev/null)
  echo "   $summary"
  prev=$count
  count=$(paper_count)
  echo "   papers: $prev -> $count"
  if [ "$count" -lt "$TARGET" ]; then sleep "$COOLDOWN"; fi
done

echo "== 预热结果：papers=$count（目标 $TARGET） =="

if [ "${PARSE_BATCH:-0}" -gt 0 ]; then
  echo "== 顺带补全文解析（小批量 $PARSE_BATCH 篇；WP05 的 parse_fulltext job） =="
  in_container python -m app.tasks.jobs.parse_fulltext --limit "$PARSE_BATCH" \
    --delay-seconds 1.5 --out /tmp/prewarm_parse.json 2>&1 | tail -3
  in_container python -c "
import json
try:
    d = json.load(open('/tmp/prewarm_parse.json'))
except Exception as exc:
    print('parse_report_read_failed:', exc); raise SystemExit
print('parse_status_counts =', json.dumps(d.get('counts') or d.get('status_counts') or {}, ensure_ascii=False))
" 2>/dev/null
fi

echo "== 预热脚本结束 =="
