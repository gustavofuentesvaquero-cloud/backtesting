"""Dataset contract failures, exercised exclusively with synthetic provider responses."""

from copy import deepcopy
import json
from pathlib import Path
import tempfile
import unittest

import duckdb
import pandas as pd

from scripts.data_baseline import inspect_bars
from scripts.equity_dataset import (
    CALENDAR, IDENTITIES, SYMBOLS, freeze, normalize, sessions, sha256, source_url,
    validate_catalog, verify, write_json,
)


def synthetic_chart(symbol="BE"):
    stamps = [int(pd.Timestamp(f"{day} 09:30", tz=CALENDAR["timezone"]).timestamp()) for day in sessions()]
    count = len(stamps)
    return {"chart": {"error": None, "result": [{
        "meta": {"symbol": symbol, "longName": IDENTITIES[symbol]["name"], "exchangeName": "NYQ",
                 "currency": "USD", "instrumentType": "EQUITY", "exchangeTimezoneName": CALENDAR["timezone"]},
        "timestamp": stamps,
        "indicators": {"quote": [{"open": [10.] * count, "high": [12.] * count,
                                   "low": [9.] * count, "close": [11.] * count, "volume": [100.] * count}],
                       "adjclose": [{"adjclose": [10.5] * count}]},
    }]}}


def synthetic_catalog():
    return {"instruments": [{"id": s, "asset_class": "equity", "venue": "NYSE", "quote_currency": "USD",
                             "provider_symbols": [{"provider_id": "YFINANCE", "symbol": s}],
                             "valid_from": "2020-01-01", "valid_to": None} for s in SYMBOLS]}


class EquityDatasetTests(unittest.TestCase):
    def test_identity_mismatch_rejected_for_each_metadata_field(self):
        for field in ("symbol", "longName", "exchangeName", "currency", "instrumentType", "exchangeTimezoneName"):
            with self.subTest(field=field):
                raw = synthetic_chart()
                raw["chart"]["result"][0]["meta"][field] = "WRONG"
                with self.assertRaisesRegex(ValueError, "identity mismatch"):
                    normalize(raw, "BE")

    def test_missing_duplicate_extra_and_unordered_sessions_rejected(self):
        for mode in ("missing", "duplicate", "extra", "unordered"):
            with self.subTest(mode=mode):
                raw = synthetic_chart()
                stamps = raw["chart"]["result"][0]["timestamp"]
                if mode == "missing":
                    stamps.pop()
                elif mode == "duplicate":
                    stamps[1] = stamps[0]
                elif mode == "extra":
                    stamps[0] = int(pd.Timestamp("2024-01-01 09:30", tz=CALENDAR["timezone"]).timestamp())
                else:
                    stamps[0], stamps[1] = stamps[1], stamps[0]
                with self.assertRaisesRegex(ValueError, "session mismatch"):
                    normalize(raw, "BE")

    def test_dst_and_early_closes_and_prices_are_preserved(self):
        frame = normalize(synthetic_chart(), "BE").set_index("session_date")
        self.assertEqual(str(frame.loc["2024-01-02", "date"]), "2024-01-02 21:00:00+00:00")
        self.assertEqual(str(frame.loc["2024-07-02", "date"]), "2024-07-02 20:00:00+00:00")
        self.assertEqual(str(frame.loc["2024-07-03", "date"]), "2024-07-03 17:00:00+00:00")
        self.assertEqual(str(frame.loc["2024-11-29", "date"]), "2024-11-29 18:00:00+00:00")
        self.assertEqual(str(frame.loc["2024-12-24", "date"]), "2024-12-24 18:00:00+00:00")
        self.assertTrue((frame.close == 11.).all())
        self.assertTrue((frame.adj_close == 10.5).all())

    def test_invalid_ohlcv_and_adjusted_close_rejected(self):
        for field, value in (("close", float("inf")), ("open", 0.), ("high", 8.), ("low", 12.),
                             ("volume", -1.), ("volume", 1.5), ("adjclose", float("nan"))):
            with self.subTest(field=field, value=value):
                raw = synthetic_chart()
                indicators = raw["chart"]["result"][0]["indicators"]
                vector = indicators["adjclose"][0]["adjclose"] if field == "adjclose" else indicators["quote"][0][field]
                vector[0] = value
                with self.assertRaises(ValueError):
                    normalize(raw, "BE")

    def test_dividend_ex_date_preserved_without_adjusting_prices(self):
        raw = synthetic_chart("NEM")
        result = raw["chart"]["result"][0]
        stamp = result["timestamp"][20]
        result["events"] = {"dividends": {str(stamp): {"date": stamp, "amount": .25}}}
        frame = normalize(raw, "NEM")
        self.assertEqual(frame.dividend.sum(), .25)
        self.assertEqual(frame.loc[20, "dividend"], .25)
        self.assertEqual(frame.loc[20, "close"], 11.)

    def test_splits_and_off_session_dividends_fail_closed(self):
        for events in ({"splits": {"x": {"numerator": 2, "denominator": 1}}},
                       {"dividends": {"x": {"date": 1704067200, "amount": .25}}}):
            raw = synthetic_chart()
            raw["chart"]["result"][0]["events"] = events
            with self.assertRaises(ValueError):
                normalize(raw, "BE")

    def test_catalog_wrong_mapping_and_validity_rejected(self):
        for field, value in (("venue", "ASX"), ("quote_currency", "AUD"), ("valid_from", "2024-02-01"),
                             ("valid_to", "2024-06-01"), ("asset_class", "future")):
            catalog = synthetic_catalog()
            catalog["instruments"][0][field] = value
            with self.assertRaises(ValueError):
                validate_catalog(catalog)

    def make_inputs(self, root):
        source = root / "downloads"
        source.mkdir()
        catalog = root / "catalog.json"
        write_json(catalog, synthetic_catalog())
        for symbol in SYMBOLS:
            path = source / f"{symbol}.json"
            write_json(path, synthetic_chart(symbol))
            write_json(source / f"{symbol}.receipt.json", {
                "requested_url": source_url(symbol), "retrieved_at_utc": "2026-10-21T00:00:00+00:00",
                "sha256": sha256(path),
            })
        return source, catalog

    def test_freeze_verifies_and_refuses_overwrite(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source, catalog = self.make_inputs(root)
            target = root / "frozen"
            manifest = freeze(source, target, catalog)
            self.assertEqual(verify(target), manifest)
            self.assertEqual(manifest["expected_sessions_per_instrument"], 252)
            self.assertEqual(set(manifest["instruments"]), {"BE", "NEM"})
            with self.assertRaisesRegex(ValueError, "overwrite"):
                freeze(source, target, catalog)

    def test_corruption_and_missing_files_rejected(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source, catalog = self.make_inputs(root)
            target = root / "frozen"
            freeze(source, target, catalog)
            path = target / "source" / "BE.json"
            original = path.read_bytes()
            path.write_bytes(original + b" ")
            with self.assertRaisesRegex(ValueError, "checksum mismatch"):
                verify(target)
            path.write_bytes(original)
            # Even refreshing hashes and summaries must not bless a changed price.
            parquet = target / "BE_1d.parquet"
            with duckdb.connect(":memory:") as con:
                frame = con.execute("SELECT * FROM read_parquet(?)", [str(parquet)]).df()
                frame.loc[0, "close"] += .1
                con.register("changed", frame)
                con.execute("COPY changed TO ? (FORMAT PARQUET)", [str(parquet)])
            manifest_path = target / "manifest.json"
            manifest = json.loads(manifest_path.read_text())
            manifest["files"]["BE_1d.parquet"] = sha256(parquet)
            manifest["instruments"]["BE"]["quality"] = inspect_bars(parquet, "BE")
            write_json(manifest_path, manifest)
            with self.assertRaises(AssertionError):
                verify(target)
            (target / "NEM_1d.parquet").unlink()
            with self.assertRaises(OSError):
                verify(target)

    def test_receipt_download_parameters_and_checksum_rejected(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source, catalog = self.make_inputs(root)
            path = source / "BE.receipt.json"
            receipt = json.loads(path.read_text())
            for key, value in (("requested_url", "https://wrong.example"), ("sha256", "0" * 64),
                               ("retrieved_at_utc", "2020-01-01T00:00:00+00:00")):
                changed = deepcopy(receipt)
                changed[key] = value
                write_json(path, changed)
                with self.assertRaises(ValueError):
                    freeze(source, root / "frozen", catalog)
