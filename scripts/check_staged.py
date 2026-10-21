"""Check the Git index in an isolated snapshot before committing."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile

ROOT = Path(__file__).resolve().parents[1]


def git(root: Path, *args: str):
    return subprocess.run(["git", "-C", str(root), *args], check=True, capture_output=True)


def run_staged_checks(root: Path = ROOT) -> int:
    root = Path(os.fsdecode(git(root, "rev-parse", "--show-toplevel").stdout).strip()).resolve()
    if git(root, "ls-files", "--unmerged").stdout:
        print("Resolve index conflicts before running staged checks.", file=sys.stderr)
        return 1
    index = git(root, "ls-files", "--stage", "-z").stdout
    entries = [item for item in index.split(b"\0") if item]
    if not entries or any(item.split(b" ", 1)[0] not in (b"100644", b"100755") for item in entries):
        print("Staged checks require regular tracked files, without submodules or symlinks.", file=sys.stderr)
        return 1
    output = root / "artifacts" / "pre-commit"
    output.mkdir(parents=True, exist_ok=True)
    report = output / "checks.json"
    log = output / "checks.log"
    if report.exists():
        report.unlink()
    print("Checking the staged source snapshot (unit and historical suites)...", flush=True)
    with tempfile.TemporaryDirectory(prefix="backtesting-staged-") as directory:
        snapshot = Path(directory).resolve()
        if not snapshot.is_relative_to(Path(tempfile.gettempdir()).resolve()):
            raise RuntimeError("Snapshot escaped the intended temporary directory.")
        git(root, "checkout-index", "--all", "--prefix=" + snapshot.as_posix() + "/")
        required = (snapshot / "scripts" / "check.py", snapshot / "src" / "market_catalog" / "__init__.py")
        if not all(path.is_file() for path in required):
            print("Stage scripts/check.py and the package source before committing.", file=sys.stderr)
            return 1
        env = dict(os.environ)
        # Git exports repository/index paths to hooks. Child tests may create their own repositories.
        for name in git(root, "rev-parse", "--local-env-vars").stdout.decode().splitlines():
            env.pop(name, None)
        env["PYTHONPATH"] = str(snapshot / "src")
        completed = subprocess.run(
            [sys.executable, "-m", "scripts.check", "--suite", "all", "--report", str(report)],
            cwd=snapshot, env=env, capture_output=True, text=True,
        )
        log.write_text(completed.stdout + completed.stderr, encoding="utf-8")
        if completed.returncode:
            print((completed.stdout + completed.stderr)[-10000:], file=sys.stderr)
            print(f"Staged checks failed. Full output: {log}", file=sys.stderr)
            return 1
        if not report.is_file():
            print("Staged checks produced no report.", file=sys.stderr)
            return 1
        result = json.loads(report.read_text(encoding="utf-8"))
        if not result.get("successful") or not Path(result["package_path"]).resolve().is_relative_to(snapshot / "src"):
            print("Staged checks failed or imported source outside the snapshot.", file=sys.stderr)
            return 1
        result["git_index_sha256"] = hashlib.sha256(index).hexdigest()
        result["source_mode"] = "git-index-snapshot"
        report.write_text(json.dumps(result, indent=2) + "\n", encoding="utf-8")
    print(f"Staged checks passed. Report: {report}")
    return 0


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repo", type=Path, default=ROOT)
    args = parser.parse_args(argv)
    try:
        return run_staged_checks(args.repo)
    except (OSError, ValueError, KeyError, subprocess.CalledProcessError) as exc:
        print(f"Cannot verify staged changes: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
