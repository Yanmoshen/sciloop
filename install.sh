#!/usr/bin/env sh
set -eu
REPO_URL="https://github.com/Yanmoshen/sciloop.git"
INSTALL_DIR="${SCILOOP_HOME:-$HOME/sciloop}"

if [ -d "$INSTALL_DIR/.git" ]; then
  git -C "$INSTALL_DIR" pull --ff-only
else
  git clone "$REPO_URL" "$INSTALL_DIR"
fi
cd "$INSTALL_DIR"
[ -f .env ] || cp .env.example .env
if grep -q '^OWNER_TOKEN=change_me' .env; then
  token="$(python3 -c 'import secrets; print(secrets.token_urlsafe(32))')"
  sed -i.bak "s/^OWNER_TOKEN=change_me$/OWNER_TOKEN=$token/" .env
  rm -f .env.bak
fi

if [ ! -x .venv/bin/python ]; then python3 -m venv .venv; fi
.venv/bin/python -m pip install --upgrade pip
.venv/bin/python -m pip install -e ./server

if command -v npm >/dev/null 2>&1; then
  (cd web && if [ -f package-lock.json ]; then npm ci; else npm install; fi && npm run build)
fi

# 可选论文 PDF 编译：SCILOOP_BUILD_PAPER=1 时引导固定版本 Tectonic。
if [ "${SCILOOP_BUILD_PAPER:-0}" = "1" ]; then
  .venv/bin/python tools/compile_paper.py --bootstrap
fi

if [ "${SCILOOP_RUN_PAPER_ACCEPTANCE:-0}" = "1" ]; then
  .venv/bin/python tools/run.py paper-acceptance
fi

# 正式运行只用 8000 提供页面和 API；执行器由后端负责启动与关闭。
# 重复安装复用已运行实例，失败不自动换端口。
exec .venv/bin/python tools/run.py start
