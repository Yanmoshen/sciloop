"""Run a reproducible paper acceptance check inside a SciLoop checkout.

The command keeps generated manuscripts below ``knowledge-base/projects`` and
reports external material below ``knowledge-base/literature`` or ``uploads``.
It delegates compilation to the pinned, project-local Tectonic bootstrapper.
No container runtime is required.
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import sys
import time
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
KB = ROOT / "knowledge-base"
PROJECTS = KB / "projects"
EXTERNAL_ROOTS = (KB / "literature", KB / "uploads")


def discover_paper_dir(explicit: str | None) -> Path:
    if explicit:
        path = Path(explicit).expanduser().resolve()
        if not (path / "main.tex").is_file():
            raise SystemExit(f"paper directory has no main.tex: {path}")
        return path
    candidates = sorted(
        (path for path in PROJECTS.glob("*") if (path / "main.tex").is_file()),
        key=lambda path: path.stat().st_mtime,
        reverse=True,
    )
    if not candidates:
        raise SystemExit(f"no manuscript found below {PROJECTS}")
    return candidates[0]


def run(command: list[str], cwd: Path, timeout: int = 120) -> dict:
    started = time.monotonic()
    try:
        result = subprocess.run(
            command,
            cwd=cwd,
            env={**os.environ, "PYTHONIOENCODING": "utf-8"},
            text=True,
            encoding="utf-8",
            errors="replace",
            capture_output=True,
            timeout=timeout,
            check=False,
        )
    except (FileNotFoundError, subprocess.TimeoutExpired) as exc:
        return {
            "command": command,
            "returncode": None,
            "ok": False,
            "duration_seconds": round(time.monotonic() - started, 3),
            "stdout": "",
            "stderr": str(exc),
        }
    return {
        "command": command,
        "returncode": result.returncode,
        "ok": result.returncode == 0,
        "duration_seconds": round(time.monotonic() - started, 3),
        "stdout": result.stdout[-12000:],
        "stderr": result.stderr[-12000:],
    }


def static_check(paper_dir: Path) -> dict:
    checker = paper_dir / "check_manuscript_v2.py"
    if not checker.is_file():
        checker = paper_dir / "check_manuscript.py"
    if not checker.is_file():
        return {"ok": False, "error": "no manuscript checker found"}
    return run([sys.executable, str(checker)], paper_dir)


def tex_pipeline(engine: str) -> list[str]:
    command = [engine, "-interaction=nonstopmode", "-halt-on-error", "main.tex"]
    return command


def compile_native(paper_dir: Path, engine: str) -> list[dict]:
    steps = [run(tex_pipeline(engine), paper_dir, timeout=180)]
    if not steps[-1]["ok"]:
        return steps
    if (paper_dir / "refs.bib").is_file() and shutil.which("bibtex"):
        steps.append(run(["bibtex", "main"], paper_dir, timeout=120))
        if not steps[-1]["ok"]:
            return steps
    for _ in range(2):
        steps.append(run(tex_pipeline(engine), paper_dir, timeout=180))
        if not steps[-1]["ok"]:
            break
    return steps


def compile_project(paper_dir: Path) -> dict:
    """Use the repository's pinned compiler and retain bootstrap diagnostics."""
    compiler = ROOT / "tools" / "compile_paper.py"
    if not compiler.is_file():
        return {"status": "unavailable", "reason": "tools/compile_paper.py is missing", "steps": []}
    result = run([sys.executable, str(compiler), str(paper_dir), "--bootstrap"], ROOT, timeout=900)
    return {"status": "project-tectonic", "steps": [result]}


def isolation_report(paper_dir: Path) -> dict:
    paper_root = PROJECTS.resolve()
    generated_ok = paper_root in paper_dir.parents
    external = {}
    for root in EXTERNAL_ROOTS:
        files = []
        if root.is_dir():
            files = [str(path.relative_to(root)) for path in root.rglob("*") if path.is_file()]
        external[root.name] = {"root": str(root), "file_count": len(files), "sample": files[:20]}
    generated_files = [
        str(path.relative_to(paper_dir))
        for path in paper_dir.rglob("*")
        if path.is_file()
    ]
    return {
        "generated_root": str(paper_dir),
        "generated_root_is_project": generated_ok,
        "generated_file_count": len(generated_files),
        "generated_sample": generated_files[:50],
        "external_roots": external,
        "separated": generated_ok and all(Path(root).resolve() not in paper_dir.parents for root in EXTERNAL_ROOTS),
    }


def main() -> int:
    parser = argparse.ArgumentParser(description="SciLoop paper acceptance runner")
    parser.add_argument("--paper-dir", help="manuscript directory; defaults to newest project with main.tex")
    parser.add_argument("--no-compile", action="store_true", help="run static and isolation checks only")
    parser.add_argument("--json", action="store_true", help="print only the JSON result")
    args = parser.parse_args()

    paper_dir = discover_paper_dir(args.paper_dir)
    report = {
        "paper_dir": str(paper_dir),
        "static": static_check(paper_dir),
        "isolation": isolation_report(paper_dir),
        "compile": {"status": "skipped", "steps": []},
    }
    if not args.no_compile:
        report["compile"] = compile_project(paper_dir)
        project_ok = any(step.get("ok") for step in report["compile"].get("steps", []))
        if not project_ok:
            failure = report["compile"].get("steps", [{}])[-1]
            report["compile"] = {
                **report["compile"],
                "status": "unavailable",
                "reason": failure.get("stderr") or "project compiler bootstrap/compile failed",
            }

    report["pdf"] = {
        "path": str(paper_dir / "main.pdf"),
        "exists": (paper_dir / "main.pdf").is_file(),
        "bytes": (paper_dir / "main.pdf").stat().st_size if (paper_dir / "main.pdf").is_file() else 0,
    }
    output = paper_dir / "paper_acceptance_report.json"
    output.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    if args.json:
        # Windows PowerShell may expose a legacy GBK stdout even though the
        # report itself is UTF-8.  Keep machine-readable output printable.
        if hasattr(sys.stdout, "reconfigure"):
            sys.stdout.reconfigure(encoding="utf-8", errors="replace")
        print(json.dumps(report, ensure_ascii=False, indent=2))
    else:
        print(f"paper: {paper_dir}")
        print(f"static: {'PASS' if report['static'].get('ok') else 'FAIL'}")
        print(f"isolation: {'PASS' if report['isolation']['separated'] else 'FAIL'}")
        print(f"compile: {report['compile']['status']}")
        print(f"pdf: {'present' if report['pdf']['exists'] else 'missing'}")
        print(f"report: {output}")
    compile_ok = args.no_compile or report["pdf"]["exists"]
    return 0 if report["static"].get("ok") and report["isolation"]["separated"] and compile_ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
