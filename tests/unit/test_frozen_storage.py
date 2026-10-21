"""Operational downloads and frozen experiments must not share a writable store."""

from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

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

    def test_neither_creator_can_nest_another_dataset_in_an_accepted_one(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "downloads"
            source.mkdir()
            accepted = root / "accepted"
            accepted.mkdir()
            manifest = accepted / "manifest.json"
            manifest.write_text('{"purpose":"frozen test"}')
            original = manifest.read_bytes()
            target = accepted / "new-version"
            for creator in (freeze, create_baseline):
                with self.subTest(creator=creator.__name__), self.assertRaisesRegex(ValueError, "existing frozen"):
                    creator(source, target)
                self.assertFalse(target.exists())
                self.assertEqual(manifest.read_bytes(), original)

    def test_neither_creator_can_put_a_frozen_dataset_inside_operational_roots(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "source"
            source.mkdir()
            # Patch only the configured project root; all scenario paths stay temporary.
            with patch("scripts.data_baseline.ROOT", root):
                for parent in (root / "data" / "historical", root / "artifacts" / "downloads"):
                    target = parent / "accepted"
                    for creator in (freeze, create_baseline):
                        with self.subTest(parent=parent, creator=creator.__name__), self.assertRaisesRegex(ValueError, "operational"):
                            creator(source, target)
                        self.assertFalse(target.exists())
