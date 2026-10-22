"""Run offline tests and write a machine-readable preparation report."""

from __future__ import annotations

import argparse
from contextlib import ExitStack, contextmanager
from datetime import datetime, timezone
from importlib.metadata import version
import json
from pathlib import Path
import platform
import sys
import time
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]


def block_network(*args, **kwargs):
    raise RuntimeError("Network access is disabled during offline project checks.")


@contextmanager
def isolated_checks(group: str):
    if group not in ("unit", "historical"):
        raise ValueError(f"Unknown test group: {group}")
    with ExitStack() as stack:
        # Guard imports and discovery too, and fail before DNS or connection attempts.
        for target in ("socket.socket.connect", "socket.socket.connect_ex",
                       "socket.socket.sendto", "socket.create_connection",
                       "socket.getaddrinfo", "socket.gethostbyname", "socket.gethostbyname_ex"):
            stack.enter_context(patch(target, block_network))
        from market_catalog import MarketDataStore
        from market_catalog import storage

        for name in ("fetch_and_store_ticker", "fetch_and_store_cme_mgc"):
            stack.enter_context(patch.object(MarketDataStore, name, block_network))
        original_init = MarketDataStore.__init__
        frozen = (ROOT / "tests" / "fixtures" / "historical").resolve()
        protected = (ROOT / "data", frozen, Path(storage.__file__).resolve().parents[2] / "data")

        def checked_store(self, base_dir=None):
            if base_dir is None:
                raise RuntimeError("Tests must use an explicit temporary, synthetic or frozen data store.")
            path = Path(base_dir).resolve()
            if group == "unit" and any(path.is_relative_to(parent.resolve()) for parent in protected):
                raise RuntimeError("Unit tests cannot read operational data or frozen historical fixtures.")
            if group == "historical" and path != frozen:
                raise RuntimeError("Historical tests must use the verified frozen data directory.")
            original_init(self, base_dir=base_dir)

        stack.enter_context(patch.object(MarketDataStore, "__init__", checked_store))
        yield


def run_checks(suite: str, report_path: Path | None = None) -> bool:
    # Do not inject src into sys.path: a successful check must exercise installation.
    groups = ["unit", "historical"] if suite == "all" else [suite]
    reports = []
    for group in groups:
        start = time.perf_counter()
        with isolated_checks(group):
            directory = ROOT / "tests" / ("integration" if group == "historical" else "unit")
            tests = unittest.TestLoader().discover(str(directory), top_level_dir=str(ROOT))
            result = unittest.TextTestRunner(verbosity=2).run(tests)
        reports.append({
            "suite": group,
            "test_directory": str(directory.relative_to(ROOT)),
            "data_policy": "synthetic and temporary stores" if group == "unit" else "frozen regression/equity inputs and quarantined research evidence",
            "tests": result.testsRun,
            "failures": [{"test": str(test), "traceback": trace} for test, trace in result.failures],
            "errors": [{"test": str(test), "traceback": trace} for test, trace in result.errors],
            "skipped": [{"test": str(test), "reason": reason} for test, reason in result.skipped],
            "successful": result.wasSuccessful() and result.testsRun > 0,
            "duration_seconds": round(time.perf_counter() - start, 3),
        })
    passed = all(item["successful"] and not item["skipped"] for item in reports)
    if report_path:
        import market_catalog
        report_path.parent.mkdir(parents=True, exist_ok=True)
        report_path.write_text(json.dumps({
            "generated_at_utc": datetime.now(timezone.utc).isoformat(),
            "python": platform.python_version(),
            "platform": platform.platform(),
            "package_path": str(Path(market_catalog.__file__).resolve()),
            "dependencies": {name: version(name) for name in (
                "market-catalog", "duckdb", "jsonschema", "numpy", "pandas", "openpyxl", "tzdata"
            )},
            "network": "blocked",
            "successful": passed,
            "suites": reports,
        }, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    return passed


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--suite", choices=("unit", "historical", "all"), default="unit")
    parser.add_argument("--report", type=Path)
    args = parser.parse_args(argv)
    return 0 if run_checks(args.suite, args.report) else 1


if __name__ == "__main__":
    sys.exit(main())
