"""Fail-closed checks of public futures acquisitions using synthetic quotes only."""

from copy import deepcopy
from datetime import datetime
import json
from pathlib import Path
import tempfile
import unittest
from zoneinfo import ZoneInfo

from scripts.mgc_public_data import (CONTRACTS, PROBES, POLICY, audit_chart, audit_acquisition,
                                     digest, dump, freeze, request_url, verify)


class PublicMgcTests(unittest.TestCase):
    def raw(self, symbol="MGCZ26.CMX"):
        labels = [int(datetime(2026, 9, day, tzinfo=ZoneInfo("America/New_York")).timestamp()) for day in (21, 22)]
        return {"chart": {"error": None, "result": [{"meta": {
            "symbol": symbol, "shortName": CONTRACTS[symbol][1], "exchangeName": "CMX", "instrumentType": "FUTURE",
            "currency": "USD", "exchangeTimezoneName": "America/New_York"}, "timestamp": labels,
            "indicators": {"quote": [{"open": [100, 101], "high": [102, 103], "low": [99, 100],
                                      "close": [101, 102], "volume": [0, 4]}]}}]}}

    def source(self, path):
        path.mkdir()
        for symbol in (*CONTRACTS, *PROBES):
            dump(path / f"{symbol}.json", self.raw(symbol) if symbol in CONTRACTS else {"chart": {"result": None, "error": "not found"}})
            dump(path / f"{symbol}.receipt.json", {"symbol": symbol, "requested_start": "2026-09-21",
                 "requested_end_exclusive": "2026-09-23", "request_url": request_url(symbol, "2026-09-21", "2026-09-23"),
                 "downloaded_at_utc": "2026-10-22T00:00:00+00:00", "http_status": 200 if symbol in CONTRACTS else 404,
                 "sha256": digest(path / f"{symbol}.json")})

    def audit(self, raw):
        return audit_chart(raw, "MGCZ26.CMX", "2026-09-21", "2026-09-23")

    def test_zero_volume_is_retained_but_does_not_authorize_execution(self):
        result = self.audit(self.raw())
        self.assertEqual(result["zero_volume_labels"], ["2026-09-21"])
        self.assertTrue(result["structural_checks_passed"])
        self.assertFalse(result["expected_sessions_verified"])
        self.assertFalse(result["execution_eligible"])

    def test_root_continuous_wrong_month_and_equity_metadata_are_rejected(self):
        for field, value in (("symbol", "MGC=F"), ("shortName", "Micro Gold Futures,Feb-2027"),
                             ("exchangeName", "NYQ"), ("instrumentType", "EQUITY"), ("currency", "EUR")):
            raw = self.raw()
            raw["chart"]["result"][0]["meta"][field] = value
            with self.subTest(field=field), self.assertRaises(ValueError):
                self.audit(raw)

    def test_null_infinite_incoherent_and_fractional_volume_are_rejected(self):
        for column, value in (("open", None), ("close", float("inf")), ("high", 98),
                              ("low", 104), ("volume", -1), ("volume", .5), ("volume", True)):
            raw = self.raw()
            raw["chart"]["result"][0]["indicators"]["quote"][0][column][0] = value
            with self.subTest(column=column, value=value), self.assertRaises(ValueError):
                self.audit(raw)

    def test_duplicate_reversed_and_non_midnight_labels_are_rejected(self):
        for kind in ("duplicate", "reverse", "not_midnight"):
            raw = self.raw()
            stamps = raw["chart"]["result"][0]["timestamp"]
            if kind == "duplicate":
                stamps[1] = stamps[0]
            elif kind == "reverse":
                stamps.reverse()
            else:
                stamps[0] += 3600
            with self.subTest(kind=kind), self.assertRaises(ValueError):
                self.audit(raw)

    def test_incomplete_quote_columns_and_out_of_range_rows_are_rejected(self):
        raw = self.raw()
        raw["chart"]["result"][0]["indicators"]["quote"][0]["close"].pop()
        with self.assertRaises(ValueError):
            self.audit(raw)
        with self.assertRaises(ValueError):
            audit_chart(self.raw(), "MGCZ26.CMX", "2026-09-21", "2026-09-22")

    def test_freeze_and_offline_reconstruction_preserve_quarantine(self):
        with tempfile.TemporaryDirectory() as temp:
            source, target = Path(temp) / "downloads", Path(temp) / "research"
            self.source(source)
            report = freeze(source, target)
            self.assertEqual(report, verify(target))
            self.assertEqual(report["policy"], POLICY)
            self.assertFalse(report["execution_eligible"])
            self.assertEqual(report["availability_probes"][PROBES[0]]["http_status"], 404)
            with self.assertRaises(ValueError):
                freeze(source, target)
            (target / "MGCZ26.CMX.json").write_bytes(b"tampered")
            with self.assertRaisesRegex(ValueError, "evidence changed"):
                verify(target)

    def test_source_receipt_must_match_identity_request_time_and_hash(self):
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / "source"
            self.source(path)
            receipt_path = path / "MGCZ26.CMX.receipt.json"
            original = json.loads(receipt_path.read_text())
            for field, value in (("symbol", "MGC=F"), ("request_url", "https://example.invalid"),
                                 ("sha256", "0" * 64), ("downloaded_at_utc", "2026-10-22T00:00:00")):
                receipt = deepcopy(original)
                receipt[field] = value
                dump(receipt_path, receipt)
                with self.subTest(field=field), self.assertRaises(ValueError):
                    audit_acquisition(path)

    def test_modified_diagnostic_cannot_claim_execution_eligibility(self):
        with tempfile.TemporaryDirectory() as temp:
            source, target = Path(temp) / "downloads", Path(temp) / "research"
            self.source(source)
            freeze(source, target)
            report = json.loads((target / "audit.json").read_text())
            report["execution_eligible"] = True
            dump(target / "audit.json", report)
            manifest = json.loads((target / "manifest.json").read_text())
            manifest["files"]["audit.json"] = digest(target / "audit.json")
            dump(target / "manifest.json", manifest)
            with self.assertRaisesRegex(ValueError, "reconstructed"):
                verify(target)

    def test_mixed_acquisition_windows_are_not_a_comparable_chain(self):
        with tempfile.TemporaryDirectory() as temp:
            source = Path(temp) / "source"
            self.source(source)
            receipt_path = source / "MGCG27.CMX.receipt.json"
            receipt = json.loads(receipt_path.read_text())
            receipt["requested_start"] = "2026-09-20"
            receipt["request_url"] = request_url("MGCG27.CMX", "2026-09-20", "2026-09-23")
            dump(receipt_path, receipt)
            with self.assertRaisesRegex(ValueError, "same requested date range"):
                audit_acquisition(source)
