"""Freeze and verify local historical fixtures without downloading market data."""

from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path
import shutil

import duckdb
import numpy as np
import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
DEFAULT_TARGET = ROOT / "tests" / "fixtures" / "historical"
SYMBOLS = ("BE", "GOLD", "IBE", "MGC", "NEM")
NUMERIC_TOLERANCE = {"relative": 1e-10, "absolute": 1e-10}


def inspect_bars(path: Path, symbol: str) -> dict:
    with duckdb.connect(":memory:") as connection:
        frame = connection.execute("SELECT * FROM read_parquet(?)", [str(path)]).df()
    required = {"date", "open", "high", "low", "close", "volume", "instrument_id"}
    if not required.issubset(frame.columns) or frame.empty:
        raise ValueError(f"{path.name}: empty dataset or missing columns: {sorted(required - set(frame.columns))}")
    dates = pd.to_datetime(frame["date"], errors="coerce", utc=True)
    prices = frame[["open", "high", "low", "close"]].apply(pd.to_numeric, errors="coerce")
    volume = pd.to_numeric(frame["volume"], errors="coerce")
    def greater(left, right):
        return (left > right) & ~np.isclose(left, right, rtol=NUMERIC_TOLERANCE["relative"], atol=NUMERIC_TOLERANCE["absolute"])

    problems = {
        "invalid_dates": int(dates.isna().sum()),
        "duplicate_dates": int(dates.duplicated().sum()),
        "unordered_dates": int(not dates.is_monotonic_increasing),
        "invalid_prices": int((~np.isfinite(prices) | (prices <= 0)).any(axis=1).sum()),
        "invalid_ohlc": int((greater(prices["low"], prices[["open", "close"]].min(axis=1)) |
                             greater(prices[["open", "close"]].max(axis=1), prices["high"]) |
                             greater(prices["low"], prices["high"])).sum()),
        "invalid_volume": int((~np.isfinite(volume) | (volume < 0)).sum()),
        "wrong_instrument": int((frame["instrument_id"] != symbol).sum()),
    }
    if any(problems.values()):
        raise ValueError(f"{path.name}: data quality checks failed: {problems}")
    return {
        "symbol": symbol,
        "rows": len(frame),
        "first_date": dates.min().isoformat(),
        "last_date": dates.max().isoformat(),
        "sha256": hashlib.sha256(path.read_bytes()).hexdigest(),
        "quality_checks": problems,
    }


def verify_baseline(target: Path = DEFAULT_TARGET) -> dict:
    manifest = json.loads((target / "manifest.json").read_text(encoding="utf-8"))
    if manifest.get("format_version") != 1 or not manifest.get("files"):
        raise ValueError("Unsupported or empty historical fixture manifest.")
    if manifest.get("numeric_tolerance") != NUMERIC_TOLERANCE:
        raise ValueError("Historical fixture tolerance differs from the current validation policy.")
    for name, expected in manifest["files"].items():
        if Path(name).name != name or not name.endswith(".parquet"):
            raise ValueError(f"Invalid fixture filename: {name}")
        actual = inspect_bars(target / name, expected["symbol"])
        if actual != expected:
            raise ValueError(f"Historical fixture changed: {name}. Review the dataset and manifest together.")
    return manifest


def validate_freeze_locations(source: Path, target: Path) -> None:
    """A new accepted dataset must be outside writable caches and older datasets."""
    if target.exists() or target.is_symlink():
        raise ValueError(f"Refusing to overwrite existing baseline directory: {target}")
    if target.resolve().is_relative_to(source.resolve()) or source.resolve().is_relative_to(target.resolve()):
        raise ValueError("Downloads and frozen datasets must have separate, non-overlapping directories")
    path = target.resolve()
    operational = (ROOT / "data" / "historical", ROOT / "artifacts" / "downloads")
    if any(path.is_relative_to(parent.resolve()) for parent in operational):
        raise ValueError("Frozen dataset destination cannot be inside an operational download/cache directory")
    if any((parent / "manifest.json").is_file() for parent in path.parents):
        raise ValueError("Cannot nest a new dataset inside an existing frozen dataset")


def create_baseline(source: Path, target: Path, symbols=SYMBOLS) -> dict:
    validate_freeze_locations(source, target)
    # Inspect every input before copying, and do not infer provider or adjustment metadata.
    files = {f"{symbol}_1d.parquet": inspect_bars(source / f"{symbol}_1d.parquet", symbol) for symbol in symbols}
    manifest = {
        "format_version": 1,
        "numeric_tolerance": NUMERIC_TOLERANCE,
        "purpose": "Frozen regression fixtures; market provenance remains unverified.",
        "provenance": {"source": "legacy-local-cache", "provider": None, "price_adjustment": "unknown", "downloaded_at": None},
        "limits": ["Session calendar completeness is not checked.", "Instrument identity and corporate actions require independent verification."],
        "files": files,
    }
    target.mkdir(parents=True)
    for name in files:
        shutil.copyfile(source / name, target / name)
    (target / "manifest.json").write_text(json.dumps(manifest, indent=2) + "\n", encoding="utf-8")
    return verify_baseline(target)


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--create", action="store_true", help="create a new baseline; refuses to overwrite")
    parser.add_argument("--source", type=Path, default=ROOT / "data" / "historical")
    parser.add_argument("--target", type=Path, default=DEFAULT_TARGET)
    args = parser.parse_args(argv)
    try:
        manifest = create_baseline(args.source, args.target) if args.create else verify_baseline(args.target)
    except (OSError, ValueError, KeyError, duckdb.Error) as exc:
        parser.exit(1, f"Historical baseline verification failed: {exc}\n")
    print(f"Verified {len(manifest['files'])} frozen historical fixtures.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
