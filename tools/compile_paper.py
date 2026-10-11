#!/usr/bin/env python3
"""Compile a SciLoop paper with a reproducible, opt-in toolchain.

The command prefers an engine already installed on PATH.  With ``--bootstrap``
it downloads the pinned Tectonic release for the current platform into
``.data/latex`` and verifies its SHA-256 before use.  No executable is checked
into the repository and a failed compilation never produces a success marker.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import platform
import shutil
import subprocess
import sys
import tempfile
import urllib.error
import urllib.request
import zipfile
from pathlib import Path


TECTONIC_VERSION = "0.17.0"
RELEASE_BASE = (
    "https://github.com/tectonic-typesetting/tectonic/releases/download/"
    f"tectonic@{TECTONIC_VERSION}"
)
ASSETS = {
    "windows-x86_64": (
        f"tectonic-{TECTONIC_VERSION}-x86_64-pc-windows-msvc.zip",
        "f61ce51f0b0ade1015b7de7ef368541c5424e9756ecbd0d7af97d6d48030845f",
    ),
    "linux-x86_64": (
        f"tectonic-{TECTONIC_VERSION}-x86_64-unknown-linux-gnu.tar.gz",
        "1a715688baf591e650c8aeb160ae934e181685eecbb38b317de30b269ac5d606",
    ),
}


def _repo_root() -> Path:
    return Path(__file__).resolve().parents[1]


def _platform_key() -> str:
    system = platform.system().lower()
    machine = platform.machine().lower()
    if system == "windows" and machine in {"amd64", "x86_64", "x64"}:
        return "windows-x86_64"
    if system == "linux" and machine in {"amd64", "x86_64", "x64"}:
        return "linux-x86_64"
    raise RuntimeError(f"没有为 {system}/{machine} 提供固定 Tectonic 包")


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def _find_engine(root: Path, explicit: str | None) -> str | None:
    candidates = [explicit] if explicit else []
    candidates.extend(["xelatex", "tectonic"])
    local = root / ".data" / "latex" / ("tectonic.exe" if os.name == "nt" else "tectonic")
    candidates.append(str(local))
    for candidate in candidates:
        if not candidate:
            continue
        found = shutil.which(candidate) or (candidate if Path(candidate).is_file() else None)
        if found:
            return str(found)
    return None


def _bootstrap(root: Path) -> str:
    key = _platform_key()
    asset, expected = ASSETS[key]
    target_dir = root / ".data" / "latex"
    target_dir.mkdir(parents=True, exist_ok=True)
    archive = target_dir / asset
    if not archive.exists() or _sha256(archive) != expected:
        if archive.exists():
            archive.unlink()
        print(f"下载固定 Tectonic {TECTONIC_VERSION}: {asset}", flush=True)
        request = urllib.request.Request(f"{RELEASE_BASE}/{asset}", headers={"User-Agent": "sciloop-paper-builder"})
        try:
            with urllib.request.urlopen(request, timeout=60) as response, archive.open("wb") as output:
                while block := response.read(1024 * 1024):
                    output.write(block)
        except Exception as error:
            archive.unlink(missing_ok=True)
            raise RuntimeError(f"Tectonic 下载失败（网络或代理不可用）: {error}") from error
        actual = _sha256(archive)
        if actual != expected:
            archive.unlink(missing_ok=True)
            raise RuntimeError(f"Tectonic 校验失败: 期望 {expected}，实际 {actual}")
    executable = target_dir / ("tectonic.exe" if os.name == "nt" else "tectonic")
    if not executable.exists():
        with tempfile.TemporaryDirectory(dir=target_dir) as temporary:
            temp = Path(temporary)
            if archive.suffix == ".zip":
                with zipfile.ZipFile(archive) as package:
                    package.extractall(temp)
            else:
                subprocess.run(["tar", "-xzf", str(archive), "-C", str(temp)], check=True)
            matches = list(temp.rglob("tectonic.exe" if os.name == "nt" else "tectonic"))
            if not matches:
                raise RuntimeError("固定 Tectonic 包中没有可执行文件")
            shutil.copy2(matches[0], executable)
    if os.name != "nt":
        executable.chmod(executable.stat().st_mode | 0o111)
    return str(executable)


def _compile(engine: str, paper: Path, timeout: int) -> dict[str, object]:
    is_tectonic = Path(engine).stem.lower().startswith("tectonic")
    command = [engine, "--keep-logs", "--synctex", "main.tex"] if is_tectonic else [engine, "-interaction=nonstopmode", "-halt-on-error", "main.tex"]
    logs: list[str] = []
    (paper / "main.pdf").unlink(missing_ok=True)
    commands = [command] if is_tectonic else [command, ["bibtex", "main"], command, command]
    result = subprocess.CompletedProcess(command, 1)
    timed_out = False
    for current in commands:
        try:
            result = subprocess.run(
                current,
                cwd=paper,
                text=True,
                encoding="utf-8",
                errors="replace",
                capture_output=True,
                timeout=timeout,
            )
        except subprocess.TimeoutExpired as error:
            timed_out = True
            stdout = error.stdout.decode(errors="replace") if isinstance(error.stdout, bytes) else (error.stdout or "")
            stderr = error.stderr.decode(errors="replace") if isinstance(error.stderr, bytes) else (error.stderr or "")
            result = subprocess.CompletedProcess(current, 124, stdout, stderr)
            logs.append(f"编译超过 {timeout} 秒，已终止当前步骤。请检查宏包下载或网络连接。")
        logs.append(result.stdout or "")
        logs.append(result.stderr or "")
        if result.returncode or timed_out:
            break
    log_path = paper / "compile.log"
    log_path.write_text("\n".join(logs), encoding="utf-8")
    pdf = paper / "main.pdf"
    success = pdf.is_file() and pdf.stat().st_size > 0 and result.returncode == 0
    report = {
        "engine": engine,
        "command": commands,
        "returncode": result.returncode,
        "timed_out": timed_out,
        "pdf": str(pdf) if success else None,
        "log": str(log_path),
        "success": success,
    }
    (paper / "compile-result.json").write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    return report


def main() -> int:
    parser = argparse.ArgumentParser(description="编译知识库论文并记录可复现结果")
    parser.add_argument("paper", nargs="?", help="论文目录，默认使用当前验收论文")
    parser.add_argument("--engine", help="指定 xelatex/tectonic 的完整路径")
    parser.add_argument("--bootstrap", action="store_true", help="下载固定 Tectonic 到 .data/latex")
    parser.add_argument("--timeout", type=int, default=300, help="每个编译步骤最大秒数（默认 300）")
    parser.add_argument("--check", action="store_true", help="只检查引擎，不编译")
    args = parser.parse_args()
    root = _repo_root()
    paper = Path(args.paper) if args.paper else root / "knowledge-base/projects/复杂度优化论文验收 20261011__2026101108"
    paper = paper.resolve()
    if not (paper / "main.tex").is_file():
        print(f"论文目录缺少 main.tex: {paper}", file=sys.stderr)
        return 2
    engine = _find_engine(root, args.engine)
    if not engine and args.bootstrap:
        try:
            engine = _bootstrap(root)
        except (OSError, RuntimeError, urllib.error.URLError) as error:
            print(f"论文编译工具链不可用: {error}", file=sys.stderr)
            return 3
    if not engine:
        print("未找到 xelatex 或 tectonic。可运行: python tools/compile_paper.py --bootstrap", file=sys.stderr)
        return 3
    print(f"编译引擎: {engine}")
    if args.check:
        return 0
    if args.timeout < 1:
        print("--timeout 必须为正整数", file=sys.stderr)
        return 2
    report = _compile(engine, paper, args.timeout)
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return 0 if report["success"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
