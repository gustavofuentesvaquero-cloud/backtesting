"""Offline verification of the identified, provider-sourced two-equity sample."""

import unittest

import duckdb

from scripts.equity_dataset import DEFAULT_TARGET, verify, verify_metadata, dataset_quality_report, verify_action_policy, ROOT


class FrozenEquityDatasetTests(unittest.TestCase):
    def test_complete_source_reconstruction_and_identity(self):
        manifest = verify()
        metadata = verify_metadata()
        self.assertEqual({i["instrument_id"] for i in metadata["instruments"]}, {"BE", "NEM"})
        actions = verify_action_policy(ROOT / "config" / "equity-corporate-actions.json")
        self.assertFalse(actions["NEM"]["dividend_cash_applied"])
        report = dataset_quality_report()
        self.assertTrue(report["successful"])
        for item in report["instruments"].values():
            self.assertEqual(item["sessions"]["expected_count"], 252)
            self.assertEqual(item["sessions"]["missing"], [])
            self.assertEqual(len(item["sessions"]["market_closed"]), 114)
            self.assertEqual(len(item["sessions"]["early_closes"]), 3)
        self.assertEqual(manifest["instruments"]["BE"]["name"], "Bloom Energy Corporation")
        self.assertEqual(manifest["instruments"]["NEM"]["name"], "Newmont Corporation")
        self.assertEqual(manifest["expected_sessions_per_instrument"], 252)
        for item in manifest["instruments"].values():
            self.assertEqual(item["quality"]["rows"], 252)
            self.assertFalse(any(item["quality"]["quality_checks"].values()))

    def test_actual_actions_and_close_basis(self):
        manifest = verify()
        self.assertEqual(manifest["instruments"]["BE"]["dividends"], [])
        self.assertEqual(manifest["instruments"]["NEM"]["dividends"], [
            {"session_date": day, "dividend": .25} for day in
            ("2024-03-04", "2024-06-04", "2024-09-05", "2024-11-27")])
        with duckdb.connect(":memory:") as con:
            nem = con.execute("SELECT close, adj_close FROM read_parquet(?)", [str(DEFAULT_TARGET / "NEM_1d.parquet")]).df()
        self.assertTrue((nem.close != nem.adj_close).any())
