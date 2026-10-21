"""Operational downloads and frozen experiments must not share a writable store."""

from pathlib import Path
import tempfile
import unittest

from market_catalog import MarketDataStore
from scripts.data_baseline import create_baseline, verify_baseline
from scripts.equity_dataset import freeze, download
from tests.unit.test_backtest_engine import scenario


class FrozenStorageTests(unittest.TestCase):
    def test_frozen_store_is_readable_but_cannot_be_overwritten(self):
        with tempfile.TemporaryDirectory() as directory:
            source = MarketDataStore(Path(directory) / "downloads")
            self.addCleanup(source._con.close)
            source.save_bars("TEST", scenario())
            target = Path(directory) / "frozen"
            original = create_baseline(source.base_dir, target, symbols=("TEST",))
            store = MarketDataStore(target)
            self.addCleanup(store._con.close)
            self.assertEqual(len(store.load_bars("TEST")), 80)
            with self.assertRaisesRegex(PermissionError, "read-only"):
                store.save_bars("TEST", scenario())
            with self.assertRaises(PermissionError):
                download(target / "nested-download")
            self.assertFalse((target / "nested-download").exists())
            self.assertEqual(verify_baseline(target), original)
            source.save_bars("TEST", scenario().iloc[:10])
            self.assertEqual(verify_baseline(target), original)

    def test_store_opened_before_freezing_cannot_write_after_manifest_appears(self):
        with tempfile.TemporaryDirectory() as directory:
            store = MarketDataStore(directory)
            self.addCleanup(store._con.close)
            store.save_bars("TEST", scenario())
            (Path(directory) / "manifest.json").write_text("{}")
            with self.assertRaises(PermissionError):
                store._assert_writable()
            with self.assertRaises(PermissionError):
                store.save_bars("TEST", scenario())

    def test_nested_download_and_frozen_directories_are_rejected(self):
        with tempfile.TemporaryDirectory() as directory:
            source = Path(directory) / "downloads"
            source.mkdir()
            with self.assertRaisesRegex(ValueError, "non-overlapping"):
                freeze(source, source / "frozen")
            with self.assertRaisesRegex(ValueError, "non-overlapping"):
                create_baseline(source, source / "legacy-frozen", symbols=("TEST",))
