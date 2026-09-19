#!/usr/bin/env bash
# WP05 真实验证样本下载（arXiv 公开链接）
# 用法: bash download_fixtures.sh [目标目录]
# 默认落到本目录下的 fixtures/（已被 .gitignore 排除，不入库）
set -u
HERE="$(cd "$(dirname "$0")" && pwd)"
DIR="${1:-$HERE/fixtures}"
mkdir -p "$DIR"
UA="SciLoop/0.1 (fulltext verification; mailto:sciloop@example.com)"

# id|说明
IDS="1810.04805:bert-2col 1907.11692:roberta-2col 2004.04906:dpr-2col 1908.10084:sbert-2col 1706.03762:attention 2010.11929:vit"

for entry in $IDS; do
  id="${entry%%:*}"
  name="${entry#*:}"
  echo "== $id ($name)"
  curl -sSL --max-time 90 -A "$UA" -o "$DIR/$name.pdf" "https://arxiv.org/pdf/$id" && echo "   pdf $(wc -c < "$DIR/$name.pdf") bytes"
  curl -sSL --max-time 90 -A "$UA" -o "$DIR/$name.ar5iv.html" "https://ar5iv.labs.arxiv.org/html/$id" && echo "   ar5iv $(wc -c < "$DIR/$name.ar5iv.html") bytes"
  curl -sSL --max-time 90 -A "$UA" -o "$DIR/$name.arxiv.html" "https://arxiv.org/html/$id" && echo "   arxivhtml $(wc -c < "$DIR/$name.arxiv.html") bytes"
done
echo "DONE"
ls -l "$DIR"
