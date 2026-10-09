"""OS-enforced workspace writes for host subprocesses.

Windows uses a write-restricted access token and a per-run restricting SID granted
only on the workspace. Linux uses bubblewrap with a read-only host filesystem.
No implementation may fall back to an unrestricted process in workspace mode.
"""

from __future__ import annotations

import os
import secrets
import shutil
import subprocess
import sys
import threading
import time
from pathlib import Path
from typing import Any


def available() -> dict[str, Any]:
    if os.name == "nt":
        try:
            import win32security  # noqa: F401
        except ImportError:
            return {"available": False, "backend": "windows-token", "reason": "请安装后端依赖 pywin32"}
        return {"available": True, "backend": "windows-token"}
    if sys.platform.startswith("linux"):
        return {"available": bool(shutil.which("bwrap")), "backend": "bubblewrap", "reason": "需要 bubblewrap 和启用用户命名空间"}
    return {"available": False, "backend": None, "reason": "当前系统尚未实现受限执行器"}


def _argv(argv: list[str] | None, command: str | None) -> list[str]:
    if argv:
        return argv
    if os.name == "nt":
        # cmd.exe initializes correctly under WRITE_RESTRICTED; PowerShell's
        # module host rejects the restricted token before running user code.
        return ["cmd.exe", "/d", "/s", "/c", command or ""]
    return ["/bin/sh", "-c", command or ""]


def run(*, argv: list[str] | None = None, command: str | None = None, cwd: str,
        workspace_root: str, timeout_s: int, env: dict[str, str], access_mode: str) -> dict[str, Any]:
    start = time.monotonic()
    root = Path(workspace_root).expanduser().resolve(strict=True)
    workdir = Path(cwd).expanduser().resolve(strict=True)
    if access_mode != "full" and not workdir.is_relative_to(root):
        return {"ok": False, "error": "只允许在已选择的工作目录内执行命令", "code": "workdir_outside"}
    args = _argv(argv, command)
    run_env = dict(env)
    # 子进程仅需要运行环境；移除平台的模型密钥和 Owner 令牌。
    for key in list(run_env):
        if any(word in key.upper() for word in ("API_KEY", "OWNER_TOKEN", "APP_SECRET_KEY", "GITHUB_TOKEN", "DATABASE_URL")):
            run_env.pop(key)
    runtime = root / ".sciloop-runtime"
    runtime.mkdir(exist_ok=True)
    run_env.update(TMP=str(runtime), TEMP=str(runtime), TMPDIR=str(runtime), PYTHONDONTWRITEBYTECODE="1")
    try:
        if os.name == "nt":
            code, stdout, stderr, timed_out = _windows(args, workdir, root, run_env, timeout_s, access_mode)
        else:
            if access_mode != "full":
                bwrap = shutil.which("bwrap")
                if not bwrap:
                    raise RuntimeError("受限执行需要 bubblewrap；安装 bubblewrap 后重试")
                args = [bwrap, "--die-with-parent", "--new-session", "--unshare-pid", "--unshare-user", "--ro-bind", "/", "/",
                        "--dev", "/dev", "--proc", "/proc", "--bind", str(root), str(root), "--chdir", str(workdir), "--", *args]
            proc = subprocess.Popen(args, cwd=workdir, env=run_env, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                    stdin=subprocess.DEVNULL, start_new_session=True)
            try:
                stdout, stderr = proc.communicate(timeout=timeout_s)
                timed_out = False
            except subprocess.TimeoutExpired:
                import signal
                os.killpg(proc.pid, signal.SIGKILL)
                stdout, stderr = proc.communicate()
                timed_out = True
            code = proc.returncode
    except (OSError, RuntimeError, ImportError) as exc:
        return {"ok": False, "code": "sandbox_unavailable", "error": str(exc), "workdir": str(workdir)}
    return {"ok": code == 0 and not timed_out, "exit_code": code, "stdout": _decode(stdout), "stderr": _decode(stderr),
            "timed_out": timed_out, "duration_ms": int((time.monotonic() - start) * 1000), "workdir": str(workdir),
            "sandbox": available()["backend"] if access_mode != "full" else "full-access"}


def _decode(value: bytes) -> str:
    import locale
    for codec in ("utf-8", locale.getpreferredencoding(False), "gbk"):
        try:
            return value.decode(codec)[:200_000]
        except UnicodeDecodeError:
            pass
    return value.decode("utf-8", "replace")[:200_000]


def _windows(args: list[str], cwd: Path, root: Path, env: dict[str, str], timeout: int,
             mode: str) -> tuple[int, bytes, bytes, bool]:
    import win32api
    import win32con
    import win32event
    import win32job
    import win32process
    import win32security
    import pywintypes
    import msvcrt

    token = None
    job = win32job.CreateJobObject(None, "")
    process = thread = None
    handles: list[Any] = []
    pipes: list[int] = []
    buffers = [bytearray(), bytearray()]
    readers: list[threading.Thread] = []
    timed_out = False
    changed_dacls: list[Path] = []
    restricting_sid = None
    try:
        if mode != "full":
            # WRITE_RESTRICTED 让文件系统同时检查一个临时限制 SID。只给所选目录
            # 及其现有内容授予这个 SID，目录外没有该 ACE，因此保持只读。
            restricting_sid = win32security.ConvertStringSidToSid(
                "S-1-5-21-" + "-".join(str(secrets.randbelow(2**30) + 1) for _ in range(3)) + "-1001"
            )
            for path in (root, *root.rglob("*")):
                if path.is_symlink() or getattr(path, "is_junction", lambda: False)():
                    continue
                sd = win32security.GetNamedSecurityInfo(
                    str(path), win32security.SE_FILE_OBJECT, win32security.DACL_SECURITY_INFORMATION
                )
                old_dacl = sd.GetSecurityDescriptorDacl()
                if old_dacl is None:
                    raise RuntimeError(f"受限执行不支持空 DACL：{path}")
                old_dacl.AddAccessAllowedAceEx(
                    win32security.ACL_REVISION,
                    win32con.OBJECT_INHERIT_ACE | win32con.CONTAINER_INHERIT_ACE,
                    win32con.GENERIC_ALL,
                    restricting_sid,
                )
                win32security.SetNamedSecurityInfo(
                    str(path), win32security.SE_FILE_OBJECT, win32security.DACL_SECURITY_INFORMATION,
                    None, None, old_dacl, None,
                )
                changed_dacls.append(path)
            original = win32security.OpenProcessToken(win32api.GetCurrentProcess(), win32con.TOKEN_ALL_ACCESS)
            handles.append(original)
            user_sid = win32security.GetTokenInformation(original, win32security.TokenUser)[0]
            groups = win32security.GetTokenInformation(original, win32security.TokenGroups)
            preserving_sids = [
                (group_sid, 0)
                for group_sid, attributes in groups
                if attributes & win32con.SE_GROUP_ENABLED
            ]
            token = win32security.CreateRestrictedToken(
                original, 8, None, None, [(restricting_sid, 0), (user_sid, 0), *preserving_sids]
            )
            handles.append(token)

        info = win32job.QueryInformationJobObject(job, win32job.JobObjectExtendedLimitInformation)
        info["BasicLimitInformation"]["LimitFlags"] = win32job.JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE
        win32job.SetInformationJobObject(job, win32job.JobObjectExtendedLimitInformation, info)
        startup = win32process.STARTUPINFO()
        startup.dwFlags = win32con.STARTF_USESTDHANDLES
        for index in range(2):
            read_fd, write_fd = os.pipe()
            pipes.extend((read_fd, write_fd))
            os.set_inheritable(write_fd, True)
            handle = msvcrt.get_osfhandle(write_fd)
            if index == 0:
                startup.hStdOutput = handle
            else:
                startup.hStdError = handle

            def consume(fd: int = read_fd, dest: bytearray = buffers[index]) -> None:
                while chunk := os.read(fd, 8192):
                    if len(dest) < 1_000_000:
                        dest.extend(chunk[:1_000_000 - len(dest)])

            reader = threading.Thread(target=consume, daemon=True)
            reader.start()
            readers.append(reader)
        with open(os.devnull, "rb") as stdin:
            os.set_inheritable(stdin.fileno(), True)
            startup.hStdInput = msvcrt.get_osfhandle(stdin.fileno())
            flags = win32con.CREATE_SUSPENDED | win32con.CREATE_NO_WINDOW
            creator = win32process.CreateProcessAsUser if token is not None else win32process.CreateProcess
            params = (None, subprocess.list2cmdline(args), None, None, True, flags, env, str(cwd), startup)
            process, thread, _, _ = creator(token, *params) if token is not None else creator(*params)
        for write_fd in pipes[1::2]:
            os.close(write_fd)
        win32job.AssignProcessToJobObject(job, process)
        win32process.ResumeThread(thread)
        timed_out = win32event.WaitForSingleObject(process, max(1, timeout) * 1000) == win32con.WAIT_TIMEOUT
        # Also terminate background descendants after the main command completes.
        win32job.TerminateJobObject(job, 124 if timed_out else 0)
        win32event.WaitForSingleObject(process, 5000)
        code = win32process.GetExitCodeProcess(process)
        for reader in readers:
            reader.join(5)
        return code, bytes(buffers[0]), bytes(buffers[1]), timed_out
    except pywintypes.error as exc:
        raise RuntimeError(f"Windows 系统沙箱启动失败：{exc}") from exc
    finally:
        job.Close()
        for handle in [process, thread, *handles]:
            if handle is not None:
                if hasattr(handle, "CloseDesktop"):
                    handle.CloseDesktop()
                else:
                    handle.Close()
        for fd in pipes:
            try:
                os.close(fd)
            except OSError:
                pass
        cleanup_paths = list(changed_dacls)
        if restricting_sid is not None and root.exists():
            cleanup_paths.extend(
                path for path in (root, *root.rglob("*"))
                if path not in cleanup_paths and not path.is_symlink()
            )
        for path in reversed(cleanup_paths):
            if path.exists():
                sd = win32security.GetNamedSecurityInfo(
                    str(path), win32security.SE_FILE_OBJECT, win32security.DACL_SECURITY_INFORMATION
                )
                current_dacl = sd.GetSecurityDescriptorDacl()
                if current_dacl is None:
                    continue
                for index in reversed(range(current_dacl.GetAceCount())):
                    if current_dacl.GetAce(index)[-1] == restricting_sid:
                        current_dacl.DeleteAce(index)
                win32security.SetNamedSecurityInfo(
                    str(path), win32security.SE_FILE_OBJECT, win32security.DACL_SECURITY_INFORMATION,
                    None, None, current_dacl, None,
                )
