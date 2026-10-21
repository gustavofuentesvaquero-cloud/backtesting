"""Failure cases for dataset freezing and offline verification."""

from contextlib import redirect_stderr
from io import StringIO
import json
from pathlib import Path
import tempfile
import unittest

import duckdb

from market_catalog import MarketDataStore
from scripts.data_baseline import create_baseline, inspect_bars, main, verify_baseline
from tests.unit.test_backtest_engine import scenario


class DataPreparationTests(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.root = Path(directory.name)
        self.store = MarketDataStore(base_dir=self.root / "source")
        self.addCleanup(self.store._con.close)
        self.store.save_bars("TEST", scenario())

    def test_frozen_copy_survives_changes_to_working_cache(self):
        target = self.root / "frozen"
        manifest = create_baseline(self.store.base_dir, target, symbols=("TEST",))
        altered = scenario()
        altered["close"] = 110.0
        self.store.save_bars("TEST", altered)
        self.assertEqual(verify_baseline(target), manifest)
        self.assertEqual(manifest["provenance"]["price_adjustment"], "unknown")

    def test_tampered_fixture_and_missing_file_are_rejected(self):
        target = self.root / "frozen"
        create_baseline(self.store.base_dir, target, symbols=("TEST",))
        altered = scenario()
        altered["volume"] *= 2
        store = MarketDataStore(base_dir=target)
        self.addCleanup(store._con.close)
        # Corrupt the bytes directly: the public store must refuse frozen writes.
        with duckdb.connect(":memory:") as con:
            altered["instrument_id"] = "TEST"
            con.register("changed", altered)
            con.execute("COPY changed TO ? (FORMAT PARQUET)", [str(target / "TEST_1d.parquet")])
        with self.assertRaisesRegex(ValueError, "changed"):
            verify_baseline(target)
        (target / "TEST_1d.parquet").unlink()
        with self.assertRaises(duckdb.IOException):
            verify_baseline(target)

    def test_existing_baseline_cannot_be_overwritten(self):
        target = self.root / "frozen"
        create_baseline(self.store.base_dir, target, symbols=("TEST",))
        with self.assertRaisesRegex(ValueError, "overwrite"):
            create_baseline(self.store.base_dir, target, symbols=("TEST",))

    def test_bad_ohlc_duplicates_nonfinite_prices_and_volume_are_rejected(self):
        for name, column, value in (("ohlc", "high", 80.0), ("nonfinite", "close", float("inf")),
                                    ("volume", "volume", -1), ("duplicate", "date", scenario().loc[1, "date"])):
            with self.subTest(name=name):
                bars = scenario()
                bars.loc[0, column] = value
                path = self.store.save_bars("TEST", bars)
                with self.assertRaisesRegex(ValueError, "quality"):
                    inspect_bars(path, "TEST")

    def test_floating_roundoff_at_ohlc_boundary_is_tolerated(self):
        bars = scenario()
        bars.loc[0, "close"] = bars.loc[0, "high"] + 1e-12
        path = self.store.save_bars("TEST", bars)
        self.assertEqual(inspect_bars(path, "TEST")["quality_checks"]["invalid_ohlc"], 0)

    def test_invalid_fixture_path_and_empty_manifest_are_rejected(self):
        target = self.root / "frozen"
        manifest = create_baseline(self.store.base_dir, target, symbols=("TEST",))
        manifest["files"] = {"../escape.parquet": next(iter(manifest["files"].values()))}
        (target / "manifest.json").write_text(json.dumps(manifest), encoding="utf-8")
        with self.assertRaisesRegex(ValueError, "filename"):
            verify_baseline(target)
        manifest["files"] = {}
        (target / "manifest.json").write_text(json.dumps(manifest), encoding="utf-8")
        with self.assertRaisesRegex(ValueError, "empty"):
            verify_baseline(target)

    def test_cli_missing_manifest_returns_failure(self):
        with redirect_stderr(StringIO()), self.assertRaises(SystemExit) as error:
            main(["--target", str(self.root / "absent")])
        self.assertEqual(error.exception.code, 1)
