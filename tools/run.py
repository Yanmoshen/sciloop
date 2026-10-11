"""SciLoop host launcher: one instance per checkout and one public port.

The built frontend and FastAPI application are served together on port 8000.
The launcher deliberately does not start a second Vite development server.
"""

from __future__ import annotations

import argparse
import contextlib
import json
import os
import signal
import socket
import subprocess
import sys
import time
import uuid
from pathlib import Path
from urllib.request import ProxyHandler, build_opener

ROOT = Path(__file__).resolve().parents[1]
RUN = ROOT / ".data" / "run"
STATE = RUN / "state.json"
STOP = RUN / "stop.json"
URL = "http://127.0.0.1:8000"
PAPER_ACCEPTANCE = ROOT / "tools" / "paper_acceptance.py"


@contextlib.contextmanager
def instance_lock():
    RUN.mkdir(parents=True, exist_ok=True)
    with (RUN / "instance.lock").open("a+b") as handle:
        if handle.tell() == 0:
            handle.write(b"0")
            handle.flush()
        handle.seek(0)
        try:
            if os.name == "nt":
                import msvcrt

                msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl

                fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            yield False
            return
        try:
            yield True
        finally:
            handle.seek(0)
            if os.name == "nt":
                msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                fcntl.flock(handle.fileno(), fcntl.LOCK_UN)


def running() -> bool:
    with instance_lock() as acquired:
        return not acquired


def read_json(path: Path) -> dict:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}


def save_state(state: dict) -> None:
    temporary = STATE.with_suffix(".tmp")
    temporary.write_text(json.dumps(state), encoding="utf-8")
    temporary.replace(STATE)


def healthy() -> bool:
    try:
        with build_opener(ProxyHandler({})).open(URL + "/api/v1/health/live", timeout=1) as response:
            return json.load(response).get("status") == "ok"
    except (OSError, ValueError):
        return False


def require_free(port: int) -> None:
    with socket.socket() as probe:
        if os.name == "nt":
            probe.setsockopt(socket.SOL_SOCKET, socket.SO_EXCLUSIVEADDRUSE, 1)
        try:
            probe.bind(("127.0.0.1", port))
        except OSError as exc:
            raise RuntimeError(
                f"端口 {port} 已被占用。不会自动换端口或停止未知进程；请先停止原服务。"
            ) from exc


def stop_process(process: subprocess.Popen) -> None:
    # These handles belong to children we launched, never to a PID read from disk.
    if os.name == "nt":
        if process.poll() is None:
            # Give uvicorn/FastAPI a chance to run its lifespan shutdown first.
            # ``/F`` as the first signal skips that hook and leaves the backend's
            # auto-started host runner detached on port 8766.
            subprocess.run(
                ["taskkill", "/PID", str(process.pid), "/T"],
                capture_output=True,
                check=False,
                timeout=15,
                creationflags=subprocess.CREATE_NO_WINDOW,
            )
            try:
                process.wait(timeout=8)
            except subprocess.TimeoutExpired:
                pass
        if process.poll() is None:
            subprocess.run(
                ["taskkill", "/PID", str(process.pid), "/T", "/F"],
                capture_output=True, check=False, timeout=15,
                creationflags=subprocess.CREATE_NO_WINDOW,
            )
    else:
        with contextlib.suppress(ProcessLookupError):
            os.killpg(process.pid, signal.SIGTERM)
    try:
        process.wait(timeout=8)
    except subprocess.TimeoutExpired:
        if os.name != "nt":
            with contextlib.suppress(ProcessLookupError):
                os.killpg(process.pid, signal.SIGKILL)
        else:
            process.kill()
        process.wait(timeout=5)


def supervise() -> int:
    with instance_lock() as acquired:
        if not acquired:
            return 0
        state = {"pid": os.getpid(), "nonce": uuid.uuid4().hex, "status": "starting"}
        children = []
        logs = []
        stop_requested = False

        def interrupt(*_args):
            nonlocal stop_requested
            stop_requested = True

        signal.signal(signal.SIGINT, interrupt)
        signal.signal(signal.SIGTERM, interrupt)
        save_state(state)
        try:
            require_free(8000)
            if not (ROOT / "web" / "dist" / "index.html").is_file():
                raise RuntimeError("请先在 web 目录运行 npm run build。")
            env = {**os.environ, "PYTHONPATH": str(ROOT / "server"), "PYTHONUNBUFFERED": "1"}
            commands = [([sys.executable, "-m", "uvicorn", "main:app", "--app-dir", "server",
                          "--host", "127.0.0.1", "--port", "8000"], ROOT, "backend")]
            for command, cwd, name in commands:
                log = (RUN / f"{name}.log").open("ab")
                logs.append(log)
                options = {"creationflags": subprocess.CREATE_NO_WINDOW} if os.name == "nt" else {"start_new_session": True}
                process = subprocess.Popen(command, cwd=cwd, env=env, stdin=subprocess.DEVNULL,
                                           stdout=log, stderr=log, **options)
                children.append(process)
            state["children"] = [process.pid for process in children]
            save_state(state)
            deadline = time.monotonic() + 45
            while not stop_requested:
                if any(process.poll() is not None for process in children):
                    raise RuntimeError("服务进程退出，请查看 .data/run 下对应日志。")
                if read_json(STOP).get("nonce") == state["nonce"]:
                    break
                if state["status"] == "starting":
                    if healthy():
                        state["status"] = "running"
                        save_state(state)
                    elif time.monotonic() > deadline:
                        raise RuntimeError("后端未在 45 秒内启动，已收回本次启动的进程。")
                time.sleep(0.25)
        except Exception as exc:
            state.update(status="failed", error=str(exc))
            save_state(state)
            return 1
        finally:
            for process in reversed(children):
                stop_process(process)
            for log in logs:
                log.close()
            if state["status"] != "failed":
                state["status"] = "stopped"
                save_state(state)
        return 0


def stop() -> int:
    if not running():
        print("SciLoop 已停止。")
        return 0
    state = read_json(STATE)
    STOP.write_text(json.dumps({"nonce": state.get("nonce")}), encoding="utf-8")
    deadline = time.monotonic() + 30
    while running() and time.monotonic() < deadline:
        time.sleep(0.25)
    if running():
        print("停止尚未完成，请查看 .data/run/supervisor.log。")
        return 1
    print("SciLoop 已停止，所属服务端口已释放。")
    return 0


def start() -> int:
    if running():
        state = read_json(STATE)
        print("SciLoop 已运行，复用现有实例：http://127.0.0.1:8000")
        return 0
    # Fail in the caller before creating any processes.
    require_free(8000)
    with (RUN / "supervisor.log").open("ab") as log:
        options = {"creationflags": subprocess.CREATE_NO_WINDOW | subprocess.DETACHED_PROCESS} if os.name == "nt" else {"start_new_session": True}
        process = subprocess.Popen([sys.executable, str(Path(__file__).resolve()), "_serve"],
                                   cwd=ROOT, stdin=subprocess.DEVNULL, stdout=log, stderr=log, **options)
    deadline = time.monotonic() + 50
    while time.monotonic() < deadline:
        state = read_json(STATE)
        if running() and state.get("status") == "running" and healthy():
            print("SciLoop 已启动：http://127.0.0.1:8000")
            return 0
        if process.poll() is not None:
            # A simultaneous start may have obtained the lock; wait for that instance.
            if not running():
                raise RuntimeError(state.get("error") or "启动失败，请查看 .data/run/supervisor.log。")
        time.sleep(0.25)
    raise RuntimeError("启动未完成，请查看 .data/run/supervisor.log。")


def paper_acceptance() -> int:
    """Run the project-local manuscript acceptance workflow."""
    result = subprocess.run([sys.executable, str(PAPER_ACCEPTANCE)], cwd=ROOT, check=False)
    return result.returncode


def main() -> int:
    parser = argparse.ArgumentParser(description="SciLoop 启动、停止和状态管理")
    parser.add_argument("command", choices=["start", "stop", "restart", "status", "paper-acceptance", "_serve"])
    args = parser.parse_args()
    RUN.mkdir(parents=True, exist_ok=True)
    if args.command == "_serve":
        return supervise()
    if args.command == "stop":
        return stop()
    if args.command == "status":
        print(json.dumps({"managed": running(), "healthy": healthy(), "state": read_json(STATE)}, ensure_ascii=False, indent=2))
        return 0
    if args.command == "paper-acceptance":
        return paper_acceptance()
    if args.command == "restart" and stop() != 0:
        return 1
    try:
        return start()
    except RuntimeError as exc:
        print(str(exc))
        return 1


if __name__ == "__main__":
    sys.exit(main())
