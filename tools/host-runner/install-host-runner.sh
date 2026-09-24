#!/usr/bin/env bash
# Copyright 2026 SciLoop contributors
# Licensed under the Apache License, Version 2.0 (http://www.apache.org/licenses/LICENSE-2.0)
#
# 把「本机执行环境」装成开机自启（macOS / Linux）—— **跑一次，以后不用管**。
#
# 不装也能用：`docker compose up` 自带容器执行环境，只是它只能碰挂进去的目录。
# 装了这个之后，后端会**自动优先用你本机**（能碰这台机器的任何目录、用你装的 Python）。
#
# 用法：
#     bash scripts/install-host-runner.sh            # 安装（登记自启 + 立刻启动）
#     bash scripts/install-host-runner.sh --uninstall
#
# Linux：写一个 XDG autostart 项（~/.config/autostart）—— 桌面登录时自动起；
# macOS：写一个 LaunchAgent（~/Library/LaunchAgents/*.plist）—— 登录时自动起。
# 本文件不写死绝对路径，一切从脚本自身位置推出来。

set -eu

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
RUNNER="$REPO_ROOT/tools/host-runner/host_runner.py"
LABEL="com.sciloop.host-runner"
LOG_DIR="$HOME/.sciloop"
LOG_FILE="$LOG_DIR/host-runner.log"
PYTHON_BIN="${SCILOOP_HOST_PYTHON:-}"

if [ ! -f "$RUNNER" ]; then
  echo "找不到执行器脚本：$RUNNER（请在仓库里运行本脚本）" >&2
  exit 1
fi

if [ -z "$PYTHON_BIN" ]; then
  PYTHON_BIN="$(command -v python3 || command -v python || true)"
fi
if [ -z "$PYTHON_BIN" ]; then
  echo "没找到 python3。先装 Python 3，或用 SCILOOP_HOST_PYTHON=/path/to/python 指定。" >&2
  exit 1
fi

mkdir -p "$LOG_DIR"

if [ "${1:-}" = "--uninstall" ]; then
  case "$(uname -s)" in
    Darwin)
      launchctl unload "$HOME/Library/LaunchAgents/$LABEL.plist" 2>/dev/null || true
      rm -f "$HOME/Library/LaunchAgents/$LABEL.plist"
      echo "已移除 LaunchAgent（重启后不再自动起）。"
      ;;
    *)
      rm -f "$HOME/.config/autostart/$LABEL.desktop"
      echo "已移除 autostart 项（重启后不再自动起）。"
      ;;
  esac
  exit 0
fi

echo "仓库：$REPO_ROOT"
echo "执行器：$RUNNER"
echo "解释器：$PYTHON_BIN"

case "$(uname -s)" in
  Darwin)
    mkdir -p "$HOME/Library/LaunchAgents"
    cat > "$HOME/Library/LaunchAgents/$LABEL.plist" <<PLIST
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
  <key>Label</key><string>$LABEL</string>
  <key>ProgramArguments</key>
  <array>
    <string>$PYTHON_BIN</string>
    <string>$RUNNER</string>
  </array>
  <key>RunAtLoad</key><true/>
  <key>KeepAlive</key><true/>
  <key>StandardOutPath</key><string>$LOG_FILE</string>
  <key>StandardErrorPath</key><string>$LOG_FILE</string>
</dict>
</plist>
PLIST
    launchctl load "$HOME/Library/LaunchAgents/$LABEL.plist"
    echo "已登记 LaunchAgent：$LABEL"
    ;;
  *)
    mkdir -p "$HOME/.config/autostart"
    cat > "$HOME/.config/autostart/$LABEL.desktop" <<DESKTOP
[Desktop Entry]
Type=Application
Name=SciLoop 本机执行环境
Comment=让 SciLoop 能在本机执行命令（研究者可在设置里关掉）
Exec=$PYTHON_BIN $RUNNER
X-GNOME-Autostart-enabled=true
DESKTOP
    # 立刻起一个（不等待下次登录）
    nohup "$PYTHON_BIN" "$RUNNER" >> "$LOG_FILE" 2>&1 &
    echo "已登记 autostart：$LABEL（并已立即启动）"
    ;;
esac

echo ""
echo "验证："
echo "  docker compose exec backend python -c \"import asyncio; from services.agent import host_runner; print(asyncio.run(host_runner.health()))\""
