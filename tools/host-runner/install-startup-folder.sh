#!/usr/bin/env bash
# Copyright 2026 SciLoop contributors
# Licensed under the Apache License, Version 2.0 (http://www.apache.org/licenses/LICENSE-2.0)
#
# 把「本机执行环境」装成**登录自启** —— **不需要管理员**（Windows 启动文件夹方案）。
#
# 为什么不走计划任务：实测 `Register-ScheduledTask` 在受限环境下直接「拒绝访问」
# （提权也一样），而**启动文件夹只需要用户权限**，一样能实现"开机就有"。
#
# ⚠️ 两条硬约定（都踩过）：
#   1) **启动文件夹里那个 .cmd 只能出现 ASCII 路径**。`.cmd` 在非 ANSI 代码页下会把
#      中文路径**写坏**（这个仓库路径含中文）→ 含中文的路径一律放 UTF-8 的
#      `runner-launch.json` 里，由 `runner_supervisor.py` 读（Python 读 JSON 没有编码坑）。
#   2) **交给 Windows Python 的路径必须是 `C:/...` 形式**。Git Bash 给的是 `/c/...`，
#      Windows Python 打不开（报 No such file or directory），所以统一转一次。
#
# 用法：
#     bash tools/host-runner/install-startup-folder.sh              # 安装（并立刻启一次）
#     bash tools/host-runner/install-startup-folder.sh --uninstall  # 卸载（删掉那个 .cmd）
#
# 与 `install-host-runner.ps1` 的关系：那个走计划任务（需要权限），这个是**零权限**兜底。

set -eu

# Git Bash 的 /c/xxx → C:/xxx（Windows 侧程序只认后者）
win_path() {
  case "$1" in
    /[a-zA-Z]/*) printf '%s' "$1" | sed 's|^/\([a-zA-Z]\)/|\1:/|' ;;
    *) printf '%s' "$1" ;;
  esac
}

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
REPO_WIN="$(win_path "$REPO_ROOT")"
RUNNER_WIN="$REPO_WIN/tools/host-runner/host_runner.py"
SUPERVISOR="$REPO_ROOT/tools/host-runner/runner_supervisor.py"

STATE_DIR="$(win_path "${USERPROFILE:-$HOME}")/.sciloop"
STARTUP_DIR="$(win_path "${APPDATA:-$HOME/AppData/Roaming}")/Microsoft/Windows/Start Menu/Programs/Startup"
CMD_NAME="sciloop-host-runner.cmd"

if [ ! -f "$SUPERVISOR" ]; then
  echo "找不到启动器：$SUPERVISOR（请在仓库里运行本脚本）" >&2
  exit 1
fi

if [ "${1:-}" = "--uninstall" ]; then
  rm -f "$STARTUP_DIR/$CMD_NAME"
  echo "已移除启动项：$STARTUP_DIR/$CMD_NAME"
  echo "重启后不再自动起；正在跑的那个不会被杀掉。"
  exit 0
fi

PYTHON_BIN="${SCILOOP_HOST_PYTHON:-}"
if [ -z "$PYTHON_BIN" ]; then
  # ⚠️ 必须挑到 **.exe**：`command -v python3` 会命中一个没有扩展名的 shell 包装，
  # Windows 侧 exec 它必失败（实测踩过）。
  for candidate in python.exe python python3.exe python3; do
    resolved="$(command -v "$candidate" 2>/dev/null || true)"
    if [ -n "$resolved" ]; then
      case "$resolved" in
        *.exe) PYTHON_BIN="$resolved" ;;
        *) if [ -f "$resolved.exe" ]; then PYTHON_BIN="$resolved.exe"; else PYTHON_BIN="$resolved"; fi ;;
      esac
      break
    fi
  done
fi
if [ -z "$PYTHON_BIN" ]; then
  echo "没找到 python。先装 Python 3，或用 SCILOOP_HOST_PYTHON=/path/to/python 指定。" >&2
  exit 1
fi
PYTHON_WIN="$(win_path "$PYTHON_BIN")"

mkdir -p "$STATE_DIR" "$STARTUP_DIR"
cp "$SUPERVISOR" "$STATE_DIR/runner_supervisor.py"

# 端口与执行器自己同源：都取 .env 里的 SCILOOP_HOST_RUNNER_URL
PORT="$(grep -m1 '^SCILOOP_HOST_RUNNER_URL=' "$REPO_ROOT/.env" 2>/dev/null \
  | cut -d= -f2- | sed 's/[[:space:]]*#.*$//' | tr -d '\r\n"' | sed 's|.*:||;s|/.*||')"
case "$PORT" in
  ''|*[!0-9]*) PORT=8766 ;;
esac

# 用 python 写这份 JSON：保证 UTF-8，中文路径不经过 shell 的编码层
"$PYTHON_BIN" - "$STATE_DIR/runner-launch.json" "$PYTHON_WIN" "$RUNNER_WIN" "$REPO_WIN" "$PORT" <<'PY'
import json
import sys

target, python, runner, cwd, port = sys.argv[1:6]
with open(target, "w", encoding="utf-8") as handle:
    json.dump(
        {
            "python": python,
            "runner": runner,
            "cwd": cwd,
            "port": int(port),
            "_说明": (
                "汉字路径只允许出现在这里（UTF-8）。启动文件夹里的 .cmd 保持纯 ASCII，"
                "避免 .cmd 编码把中文路径写坏。"
            ),
        },
        handle,
        ensure_ascii=False,
        indent=2,
    )
PY

# 启动文件夹里的那一行：**全 ASCII**
{
  printf '@echo off\r\n'
  printf 'rem SciLoop host runner - auto start at logon (Startup folder, no admin needed).\r\n'
  printf 'rem Keep this file ASCII-only: cmd.exe mangles non-ASCII paths.\r\n'
  printf 'rem Chinese paths live in %%USERPROFILE%%\\.sciloop\\runner-launch.json (read by Python).\r\n'
  printf 'rem To disable: delete this file.\r\n'
  printf 'start "" /b "%s" "%%USERPROFILE%%\\.sciloop\\runner_supervisor.py"\r\n' \
    "$(printf '%s' "$PYTHON_WIN" | sed 's|/|\\|g')"
} > "$STARTUP_DIR/$CMD_NAME"

echo "仓库：$REPO_WIN"
echo "解释器：$PYTHON_WIN"
echo "端口：$PORT（取自 .env 的 SCILOOP_HOST_RUNNER_URL）"
echo "启动项：$STARTUP_DIR/$CMD_NAME"
echo ""
echo "现在启一次（起不来会自动重试，最多 4 次）："
"$PYTHON_BIN" "$STATE_DIR/runner_supervisor.py" || true
echo ""
echo "验证："
echo "  curl -s http://127.0.0.1:$PORT/health"
echo "卸载：bash tools/host-runner/install-startup-folder.sh --uninstall"
