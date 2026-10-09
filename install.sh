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

export PYTHONPATH="$INSTALL_DIR/server"
nohup .venv/bin/python tools/host-runner/host_runner.py > host-runner.log 2>&1 &
if command -v npm >/dev/null 2>&1; then
  (cd web && nohup npm run dev -- --host 127.0.0.1 --port 5173 > ../frontend.log 2>&1 &)
fi
exec .venv/bin/python -m uvicorn main:app --app-dir server --host 127.0.0.1 --port 8000
