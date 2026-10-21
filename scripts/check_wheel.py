"""Build a wheel and smoke-test it in a clean environment outside the checkout."""

from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import venv

ROOT = Path(__file__).resolve().parents[1]


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--wheelhouse", type=Path, help="install dependencies offline from this directory")
    args = parser.parse_args(argv)
    output = ROOT / "artifacts" / "wheels"
    output.mkdir(parents=True, exist_ok=True)
    subprocess.run([sys.executable, "-m", "pip", "wheel", "--no-deps", "--no-build-isolation", "--wheel-dir", str(output), str(ROOT)], check=True)
    from importlib.metadata import version
    wheel = output / f"market_catalog-{version('market-catalog')}-py3-none-any.whl"
    if not wheel.is_file():
        raise RuntimeError(f"Expected built wheel not found: {wheel}")
    with tempfile.TemporaryDirectory(prefix="market-catalog-wheel-") as directory:
        target = Path(directory).resolve()
        if not target.is_relative_to(Path(tempfile.gettempdir()).resolve()):
            raise RuntimeError("Wheel-check temporary directory escaped the intended parent.")
        environment = target / "venv"
        venv.EnvBuilder(with_pip=True).create(environment)
        python = environment / ("Scripts/python.exe" if os.name == "nt" else "bin/python")
        flags = ["--no-index", "--find-links", str(args.wheelhouse.resolve())] if args.wheelhouse else []
        subprocess.run([str(python), "-m", "pip", "install", *flags, "-r", str(ROOT / "requirements.lock")], check=True, cwd=target)
        subprocess.run([str(python), "-m", "pip", "install", "--no-index", "--no-deps", str(wheel)], check=True, cwd=target)
        probe = '''import json, pathlib, sys
import market_catalog
from market_catalog.cli import main
package = pathlib.Path(market_catalog.__file__).resolve()
assert package.is_relative_to(pathlib.Path(sys.prefix).resolve()), package
root = pathlib.Path(sys.argv[1])
for command, file in [('validate', 'data/market_catalog.json'), ('validate-deployment', 'config/deployment.profile.json'), ('validate-alerts', 'config/alerts.json')]:
    assert main([command, str(root / file)]) == 0, command
print(json.dumps({'successful': True, 'package_path': str(package), 'checks': ['catalog', 'deployment schema', 'alerts']}))
'''
        clean_env = dict(os.environ)
        clean_env.pop("PYTHONPATH", None)
        completed = subprocess.run([str(python), "-c", probe, str(ROOT)], check=True, cwd=target, env=clean_env, capture_output=True, text=True)
        print(completed.stdout)
        report = json.loads(completed.stdout.splitlines()[-1])
        report["wheel"] = wheel.name
        report["dependencies_installed_offline"] = bool(args.wheelhouse)
        report_dir = ROOT / "artifacts" / "preparation"
        report_dir.mkdir(parents=True, exist_ok=True)
        (report_dir / "wheel-check.json").write_text(json.dumps(report, indent=2) + "\n", encoding="utf-8")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
