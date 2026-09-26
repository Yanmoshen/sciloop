"""启动器（有界守护）：装到用户目录下的 .sciloop 里，由启动文件夹里的 .cmd 调起。

安装见同目录 install-startup-folder.sh（不需要管理员）。
本文件是**源码**；运行时用的是复制到用户目录的那一份。

原设计说明：把启动器升级成「有界守护」：起一次不成会重试，直到执行器真的活了才退出。

为什么需要（2026-09-26 实测）：
- 执行器故意设了 `allow_reuse_address = False`（避开 Windows 上"两个进程抢同一端口"的老坑），
  代价是**刚杀掉旧进程就立刻重启会被 TIME_WAIT 挡住**，绑定失败。
- 于是"杀掉旧的 → 起新的"这个再正常不过的操作，会留下**宿主上彻底没有执行器**的状态，
  而且起新进程的那一方只会看到一句"端口被占用"，很容易被忽略。
  用户看到的就又是「还没连上这台电脑的执行器」。

修法：不把"起进程"当成成功，而把"**/health 通了**"当成成功 ——
最多试 4 轮、每轮等 8 秒；通了就退出，没通就再试一次。
"""

from __future__ import annotations

import json
import subprocess
import sys
import time
import urllib.request
from pathlib import Path

HERE = Path(__file__).resolve().parent
CONFIG = HERE / "runner-launch.json"
LOG = HERE / "host-runner.log"

ATTEMPTS = 4
WAIT_S = 8.0


def _log(text: str) -> None:
    with LOG.open("a", encoding="utf-8") as handle:
        handle.write(text + "\n")


def _healthy(port: int) -> bool:
    try:
        with urllib.request.urlopen(f"http://127.0.0.1:{port}/health", timeout=3) as resp:
            return resp.status == 200
    except Exception:  # noqa: BLE001 - 连不上就是没活
        return False


def main() -> int:
    try:
        cfg = json.loads(CONFIG.read_text(encoding="utf-8"))
    except Exception as exc:  # noqa: BLE001
        _log(f"launcher: 读不到 {CONFIG}：{exc}")
        return 1

    python = str(cfg.get("python") or sys.executable)
    runner = str(cfg.get("runner") or "")
    cwd = str(cfg.get("cwd") or "")
    port = int(cfg.get("port") or 8766)

    if not runner or not Path(runner).is_file():
        _log(f"launcher: 找不到执行器脚本 {runner}")
        return 1

    if _healthy(port):
        _log("launcher: 已经在跑了，跳过")
        return 0

    flags = 0
    if sys.platform == "win32":
        # 完全脱离父进程，别跟着启动器一起退出
        flags = getattr(subprocess, "DETACHED_PROCESS", 0) | getattr(
            subprocess, "CREATE_NEW_PROCESS_GROUP", 0
        )

    for attempt in range(1, ATTEMPTS + 1):
        _log(f"launcher: 第 {attempt}/{ATTEMPTS} 次启动 {python} {runner}（cwd={cwd}）")
        with LOG.open("a", encoding="utf-8") as handle:
            subprocess.Popen(
                [python, runner],
                cwd=cwd or None,
                stdout=handle,
                stderr=subprocess.STDOUT,
                creationflags=flags,
                close_fds=True,
            )
        deadline = time.monotonic() + WAIT_S
        while time.monotonic() < deadline:
            time.sleep(1.0)
            if _healthy(port):
                _log(f"launcher: 已就绪（第 {attempt} 次尝试，端口 {port}）")
                return 0
        _log(f"launcher: 第 {attempt} 次没起来（端口 {port} 可能还在 TIME_WAIT），继续重试")

    _log(f"launcher: {ATTEMPTS} 次都没起来，需要人工看一眼（端口 {port}）")
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
