"""Daily-bar failures and exchange closures using hand-built synthetic scenarios."""

from contextlib import redirect_stdout
from io import StringIO
import json
from pathlib import Path
import tempfile
import unittest

import duckdb
import pandas as pd

from market_catalog.historical_quality import assess_daily_bars, validate_daily_bars, HistoricalQualityError
from scripts.equity_dataset import CALENDAR, main


def bars():
    return pd.DataFrame({
        "date": pd.to_datetime([f"2024-01-{day:02}T21:00:00Z" for day in (2, 3, 4, 5)]),
        "open": [10.] * 4, "high": [12.] * 4, "low": [9.] * 4, "close": [11.] * 4,
        "volume": [100., 0., 150., 200.], "instrument_id": ["BE"] * 4,
    })


def assess(frame):
    return assess_daily_bars(frame, "BE", CALENDAR, "2024-01-01", "2024-01-07")


class DailyHistoricalQualityTests(unittest.TestCase):
    def test_holiday_weekend_and_zero_volume_are_not_missing_data(self):
        frame = bars()
        before = frame.copy(deep=True)
        report = assess(frame)
        self.assertTrue(report["successful"])
        self.assertEqual(report["sessions"]["expected_count"], 4)
        self.assertEqual(report["sessions"]["missing"], [])
        self.assertEqual(report["sessions"]["market_closed"], [
            {"date": "2024-01-01", "reason": "exchange_holiday"},
            {"date": "2024-01-06", "reason": "weekend"},
            {"date": "2024-01-07", "reason": "weekend"},
        ])
        self.assertEqual(report["zero_volume_sessions"], ["2024-01-03"])
        pd.testing.assert_frame_equal(frame, before)

    def test_missing_session_is_reported_separately_from_confirmed_closures(self):
        frame = bars().drop(index=1)
        report = assess(frame)
        self.assertFalse(report["successful"])
        self.assertEqual(report["sessions"]["missing"], ["2024-01-03"])
        self.assertEqual(report["checks"]["missing_sessions"], 1)
        self.assertEqual(len(report["sessions"]["market_closed"]), 3)
        with self.assertRaises(HistoricalQualityError) as error:
            validate_daily_bars(frame, "BE", CALENDAR, "2024-01-01", "2024-01-07")
        self.assertEqual(error.exception.report, report)

    def test_bar_on_a_closed_market_day_is_an_error(self):
        frame = bars()
        frame.loc[0, "date"] = pd.Timestamp("2024-01-01T21:00:00Z")
        report = assess(frame)
        self.assertEqual(report["sessions"]["unexpected"], ["2024-01-01"])
        self.assertEqual(report["sessions"]["missing"], ["2024-01-02"])

    def test_duplicates_and_reversed_dates_have_distinct_row_diagnostics(self):
        frame = bars()
        frame.loc[1, "date"] = frame.loc[0, "date"]
        report = assess(frame)
        self.assertEqual(report["invalid_rows_zero_based"]["duplicate_dates"], [1])
        self.assertEqual(report["checks"]["unordered_dates"], 0)
        frame = bars().iloc[[0, 2, 1, 3]]
        report = assess(frame)
        self.assertEqual(report["invalid_rows_zero_based"]["unordered_dates"], [2])
        frame = bars()
        frame.loc[1, "date"] = frame.loc[0, "date"] + pd.Timedelta(hours=1)
        report = assess(frame)
        self.assertEqual(report["checks"]["duplicate_dates"], 0)
        self.assertEqual(report["checks"]["duplicate_sessions"], 1)

    def test_nonfinite_prices_bad_ohlc_and_invalid_volumes_identify_rows(self):
        for column, value, check in (("open", float("nan"), "nonfinite_prices"),
                                     ("close", float("inf"), "nonfinite_prices"),
                                     ("low", 0., "nonpositive_prices"),
                                     ("high", 8., "inconsistent_ohlc"),
                                     ("low", 12., "inconsistent_ohlc"),
                                     ("volume", float("inf"), "nonfinite_volume"),
                                     ("volume", -1., "negative_volume"),
                                     ("volume", 1.5, "fractional_volume")):
            with self.subTest(column=column, value=value):
                frame = bars()
                frame.loc[2, column] = value
                report = assess(frame)
                self.assertFalse(report["successful"])
                self.assertEqual(report["invalid_rows_zero_based"][check], [2])
        frame = bars()
        frame["close"] = frame["close"].astype("Float64")
        frame["volume"] = frame["volume"].astype("Float64")
        frame.loc[2, ["close", "volume"]] = pd.NA
        frame["instrument_id"] = frame["instrument_id"].astype("string")
        frame.loc[2, "instrument_id"] = pd.NA
        report = assess(frame)
        for check in ("nonfinite_prices", "nonfinite_volume", "wrong_instrument"):
            self.assertEqual(report["invalid_rows_zero_based"][check], [2])

    def test_invalid_dates_empty_inputs_and_missing_columns_fail_closed(self):
        frame = bars()
        frame.loc[0, "date"] = pd.NaT
        self.assertEqual(assess(frame)["invalid_rows_zero_based"]["invalid_dates"], [0])
        self.assertFalse(assess(bars().iloc[:0])["successful"])
        report = assess(bars().drop(columns="close"))
        self.assertEqual(report["missing_columns"], ["close"])
        self.assertFalse(report["successful"])

    def test_early_close_is_expected_and_unknown_calendar_year_is_rejected(self):
        frame = bars().iloc[:1].copy()
        frame["date"] = pd.to_datetime(["2024-07-03T17:00:00Z"])
        report = assess_daily_bars(frame, "BE", CALENDAR, "2024-07-03", "2024-07-04")
        self.assertTrue(report["successful"])
        self.assertEqual(report["sessions"]["early_closes"], [{"date": "2024-07-03", "close_local": "13:00"}])
        self.assertEqual(report["sessions"]["missing"], [])
        with self.assertRaisesRegex(ValueError, "calendar year"):
            assess_daily_bars(frame, "BE", CALENDAR, "2025-01-01", "2025-01-07")

    def test_cli_saves_failure_diagnostics_and_returns_nonzero(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            target = root / "frozen"
            target.mkdir()
            frame = bars().drop(index=1)
            with duckdb.connect(":memory:") as con:
                con.register("bars", frame)
                con.execute("COPY bars TO ? (FORMAT PARQUET)", [str(target / "BE_1d.parquet")])
            path = root / "quality.json"
            with redirect_stdout(StringIO()):
                result = main(["--target", str(target), "--quality-report", str(path)])
            self.assertEqual(result, 1)
            report = json.loads(path.read_text())
            self.assertFalse(report["successful"])
            self.assertIn("2024-01-03", report["instruments"]["BE"]["sessions"]["missing"])
            self.assertIn("read_error", report["instruments"]["NEM"])
            self.assertTrue(report["integrity_error"])
