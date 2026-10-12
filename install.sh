#!/usr/bin/env sh
set -eu

# SciLoop host installer. It bootstraps prerequisites through the host's
# package manager, builds the single frontend bundle, and starts port 8000.
REPO_URL="${SCILOOP_REPO_URL:-https://github.com/Yanmoshen/sciloop.git}"
INSTALL_DIR="${SCILOOP_HOME:-$HOME/sciloop}"
TRANSCRIPT_LOG="${INSTALL_DIR}.install.log"
LOG_PATH="$TRANSCRIPT_LOG"
mkdir -p "$(dirname "$TRANSCRIPT_LOG")"

log() { printf '%s\n' "$*" | tee -a "$LOG_PATH"; }
die() { log "SciLoop 安装失败：$*" >&2; log "详细日志：$LOG_PATH" >&2; exit 1; }
has() { command -v "$1" >/dev/null 2>&1; }

run_logged() {
  output="$(mktemp)"
  if "$@" >"$output" 2>&1; then status=0; else status=$?; fi
  cat "$output" | tee -a "$LOG_PATH"
  rm -f "$output"
  return "$status"
}

as_root() {
  if [ "$(id -u)" -eq 0 ]; then "$@"; elif has sudo; then sudo "$@"; else die "需要 root 权限或 sudo 执行：$*"; fi
}

install_prerequisites() {
  missing=''
  has git || missing="$missing git"
  has python3 || missing="$missing python3"
  has node || missing="$missing node"
  has npm || missing="$missing npm"
  [ -z "$missing" ] && return 0

  if has apt-get; then
    run_logged as_root apt-get update || die 'apt-get update 失败。'
    run_logged as_root apt-get install -y git python3 python3-venv python3-pip nodejs npm || die '系统依赖安装失败。'
  elif has dnf; then
    run_logged as_root dnf install -y git python3 python3-pip nodejs npm || die '系统依赖安装失败。'
  elif has pacman; then
    run_logged as_root pacman -Sy --needed --noconfirm git python python-pip nodejs npm || die '系统依赖安装失败。'
  elif has brew; then
    run_logged brew update || die 'Homebrew 更新失败。'
    run_logged brew install git python node || die 'Homebrew 依赖安装失败。'
  else
    die "缺少以下依赖：$missing。未找到 apt-get、dnf、pacman 或 brew，无法自动安装。"
  fi
}

install_prerequisites
has git || die '未找到 git。'
has python3 || die '未找到 python3。'
has node || die '未找到 node。'
has npm || die '未找到 npm。'

python_version="$(python3 -c 'import sys; print("%d.%d" % sys.version_info[:2])')"
python_major="${python_version%%.*}"
python_minor="${python_version#*.}"
if [ "$python_major" -lt 3 ] || { [ "$python_major" -eq 3 ] && [ "$python_minor" -lt 11 ]; }; then
  die "Python $python_version 过低，需要 3.11 或更高版本。"
fi
node_major="$(node --version | sed 's/^v//' | cut -d. -f1)"
[ "$node_major" -ge 18 ] || die "Node.js 版本过低，需要 18 或更高版本。"

if [ -d "$INSTALL_DIR/.git" ]; then
  run_logged git -C "$INSTALL_DIR" pull --ff-only || die '更新仓库失败。'
else
  if [ -e "$INSTALL_DIR" ]; then die "$INSTALL_DIR 已存在但不是 SciLoop 仓库；请更换 SCILOOP_HOME 或手动处理该目录。"; fi
  run_logged git clone "$REPO_URL" "$INSTALL_DIR" || die '克隆仓库失败。'
fi
cd "$INSTALL_DIR"

LOG_DIR="$INSTALL_DIR/.data"
LOG_PATH="$LOG_DIR/install.log"
mkdir -p "$LOG_DIR"
if [ -f "$TRANSCRIPT_LOG" ]; then cp "$TRANSCRIPT_LOG" "$LOG_PATH"; fi

[ -f .env ] || cp .env.example .env
if grep -q '^OWNER_TOKEN=change_me' .env; then
  token="$(python3 -c 'import secrets; print(secrets.token_urlsafe(32))')"
  sed -i.bak "s/^OWNER_TOKEN=change_me$/OWNER_TOKEN=$token/" .env
  rm -f .env.bak
fi

[ -x .venv/bin/python ] || run_logged python3 -m venv .venv || die '创建 Python 虚拟环境失败。'
run_logged .venv/bin/python -m pip install --upgrade pip || die '升级 pip 失败。'
run_logged .venv/bin/python -m pip install -e ./server || die '后端依赖安装失败。'

cd web
if [ -f package-lock.json ]; then run_logged npm ci --no-audit --no-fund; else run_logged npm install --no-audit --no-fund; fi
run_logged npm run build || die '前端构建失败。'
cd ..
[ -f web/dist/index.html ] || die '前端构建完成但 web/dist/index.html 不存在。'

if [ "${SCILOOP_BUILD_PAPER:-0}" = '1' ]; then
  run_logged .venv/bin/python tools/compile_paper.py --bootstrap || die '论文 PDF 编译失败，请查看论文目录下 compile.log。'
fi
if [ "${SCILOOP_RUN_PAPER_ACCEPTANCE:-0}" = '1' ]; then
  run_logged .venv/bin/python tools/run.py paper-acceptance || die '论文验收失败，请查看论文项目目录中的 paper_acceptance_report.json。'
fi

log ''
log 'SciLoop 依赖、前端和 SQLite 已准备完成。'
log '启动地址：http://127.0.0.1:8000'
log "安装日志：$LOG_PATH"
run_logged .venv/bin/python tools/run.py start || die 'SciLoop 启动失败，请查看 .data/run 和安装日志。'
