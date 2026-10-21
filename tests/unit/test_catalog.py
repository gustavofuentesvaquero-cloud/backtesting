from __future__ import annotations

import json
from contextlib import redirect_stderr, redirect_stdout
from decimal import Decimal
from io import StringIO
from pathlib import Path
import tempfile
import unittest

from market_catalog import CatalogLoadError, CatalogValidationError, load_catalog, validate_catalog
from market_catalog.cli import main as cli_main


ROOT = Path(__file__).resolve().parents[2]


def empty_catalog() -> dict:
    return {
        "schema_version": "1.0.0",
        "catalog_version": "1.0.0",
        "instruments": [],
        "calendars": [],
        "continuous_futures": [],
    }


def synthetic_calendar() -> dict:
    return {
        "id": "TEST-CALENDAR",
        "timezone": "UTC",
        "weekly_sessions": [
            {
                "start_weekday": 0,
                "open_time": "09:00",
                "close_time": "17:00",
                "close_day_offset": 0,
                "kind": "regular",
            }
        ],
        "date_overrides": [
            {"date": "2030-12-25", "status": "closed", "sessions": []}
        ],
    }


def synthetic_equity() -> dict:
    return {
        "id": "SYNTH-EQ",
        "asset_class": "equity",
        "venue": "SYNTHETIC",
        "provider_symbols": [{"provider_id": "TEST", "symbol": "SYNTH"}],
        "valid_from": "2030-01-01",
        "valid_to": None,
        "calendar_id": "TEST-CALENDAR",
        "price_tick": "0.25",
        "price_precision": 2,
        "quantity_unit": "share",
        "quantity_precision": 0,
        "minimum_quantity": "1",
        "quantity_increment": "1",
        "quote_currency": "USD",
        "valuation_model": "equity_shares",
        "provenance": {"status": "synthetic", "sources": [], "notes": "Test fixture only."},
        "historical_price_policy": {"price_basis": "raw", "corporate_action_events": []},
    }


def synthetic_fx() -> dict:
    return {
        "id": "SYNTH-FX",
        "asset_class": "fx",
        "venue": "SYNTHETIC",
        "provider_symbols": [{"provider_id": "TEST", "symbol": "AAA/BBB"}],
        "valid_from": "2030-01-01",
        "valid_to": None,
        "calendar_id": "TEST-CALENDAR",
        "price_tick": "0.0001",
        "price_precision": 4,
        "quantity_unit": "base_currency_unit",
        "quantity_precision": 0,
        "minimum_quantity": "1000",
        "quantity_increment": "1000",
        "quote_currency": "BBB",
        "valuation_model": "fx_base_units",
        "provenance": {"status": "synthetic", "sources": [], "notes": "Test fixture only."},
        "fx_convention": {
            "base_currency": "AAA",
            "pip_size": "0.0001",
            "pip_value_currency": "BBB",
            "pip_value_rule": "base_quantity_times_pip_size_in_quote_currency",
        },
    }


def synthetic_future(identifier: str, month: str, expiry: str, last_trade: str) -> dict:
    contract_code = f"SYNTH-{month}"
    return {
        "id": identifier,
        "asset_class": "future",
        "venue": "SYNTHETIC",
        "provider_symbols": [{"provider_id": "TEST", "symbol": contract_code}],
        "valid_from": "2030-01-01",
        "valid_to": None,
        "calendar_id": "TEST-CALENDAR",
        "price_tick": "0.25",
        "price_precision": 2,
        "quantity_unit": "contract",
        "quantity_precision": 0,
        "minimum_quantity": "1",
        "quantity_increment": "1",
        "quote_currency": "USD",
        "valuation_model": "future_contract_multiplier",
        "provenance": {"status": "synthetic", "sources": [], "notes": "Test fixture only."},
        "future_contract": {
            "contract_code": contract_code,
            "contract_month": month,
            "expiry_date": expiry,
            "last_trade_date": last_trade,
            "first_notice_date": None,
            "settlement_method": "cash",
            "settlement_currency": "USD",
            "contract_multiplier": "10",
        },
    }


class CatalogValidationTests(unittest.TestCase):
    def assert_invalid(self, document: dict, code: str | None = None) -> list:
        issues = validate_catalog(document)
        self.assertTrue(issues, "Expected catalog to be invalid")
        if code is not None:
            self.assertIn(code, {issue.code for issue in issues})
        return issues

    def test_empty_catalog_is_valid(self) -> None:
        self.assertEqual([], validate_catalog(empty_catalog()))

    def test_complete_synthetic_equity_is_valid(self) -> None:
        document = empty_catalog()
        document["calendars"] = [synthetic_calendar()]
        document["instruments"] = [synthetic_equity()]
        self.assertEqual([], validate_catalog(document))

    def test_complete_synthetic_fx_is_valid(self) -> None:
        document = empty_catalog()
        document["calendars"] = [synthetic_calendar()]
        document["instruments"] = [synthetic_fx()]
        self.assertEqual([], validate_catalog(document))

    def test_complete_synthetic_future_and_continuous_series_are_valid(self) -> None:
        document = empty_catalog()
        document["calendars"] = [synthetic_calendar()]
        document["instruments"] = [
            synthetic_future("SYNTH-FUT-2030-03", "2030-03", "2030-03-20", "2030-03-19"),
            synthetic_future("SYNTH-FUT-2030-06", "2030-06", "2030-06-20", "2030-06-19"),
        ]
        document["continuous_futures"] = [
            {
                "id": "SYNTH-CONTINUOUS",
                "instrument_ids": ["SYNTH-FUT-2030-03", "SYNTH-FUT-2030-06"],
                "executable": False,
                "roll_policy": {"method": "days_before_expiry", "roll_before_expiry_days": 5},
                "adjustment_method": "back_adjusted_difference",
                "provenance": {"status": "synthetic", "sources": [], "notes": "Test fixture only."},
            }
        ]
        self.assertEqual([], validate_catalog(document))

    def test_missing_class_specific_fx_fields_are_reported(self) -> None:
        document = empty_catalog()
        instrument = synthetic_fx()
        del instrument["fx_convention"]
        document["calendars"] = [synthetic_calendar()]
        document["instruments"] = [instrument]
        issues = self.assert_invalid(document, "required")
        self.assertTrue(any("fx_convention" in issue.message for issue in issues))

    def test_missing_class_specific_equity_and_future_fields_are_reported(self) -> None:
        equity_document = empty_catalog()
        equity = synthetic_equity()
        del equity["historical_price_policy"]
        equity_document["calendars"] = [synthetic_calendar()]
        equity_document["instruments"] = [equity]
        equity_issues = self.assert_invalid(equity_document, "required")
        self.assertTrue(any("historical_price_policy" in issue.message for issue in equity_issues))

        future_document = empty_catalog()
        future = synthetic_future("SYNTH-FUT-2030-03", "2030-03", "2030-03-20", "2030-03-19")
        del future["future_contract"]
        future_document["calendars"] = [synthetic_calendar()]
        future_document["instruments"] = [future]
        future_issues = self.assert_invalid(future_document, "required")
        self.assertTrue(any("future_contract" in issue.message for issue in future_issues))

    def test_unknown_schema_version_is_rejected(self) -> None:
        document = empty_catalog()
        document["schema_version"] = "2.0.0"
        self.assert_invalid(document, "unsupported_schema_version")

    def test_malformed_decimal_string_is_rejected(self) -> None:
        document = empty_catalog()
        instrument = synthetic_equity()
        instrument["price_tick"] = "0.1x"
        document["calendars"] = [synthetic_calendar()]
        document["instruments"] = [instrument]
        issues = self.assert_invalid(document, "pattern")
        tick_issue = next(issue for issue in issues if issue.path.endswith("price_tick"))
        self.assertIn("decimal string", tick_issue.message)

    def test_non_positive_tick_and_quantity_are_rejected(self) -> None:
        document = empty_catalog()
        instrument = synthetic_equity()
        instrument["price_tick"] = "0"
        instrument["minimum_quantity"] = "0"
        document["calendars"] = [synthetic_calendar()]
        document["instruments"] = [instrument]
        issues = self.assert_invalid(document, "positive")
        self.assertTrue(any("price_tick" in issue.path for issue in issues))
        self.assertTrue(any("minimum_quantity" in issue.path for issue in issues))

    def test_minimum_quantity_must_be_multiple_of_increment(self) -> None:
        document = empty_catalog()
        instrument = synthetic_equity()
        instrument["minimum_quantity"] = "1.5"
        instrument["quantity_increment"] = "1"
        document["calendars"] = [synthetic_calendar()]
        document["instruments"] = [instrument]
        self.assert_invalid(document, "quantity_multiple")

    def test_quantity_precision_must_cover_minimum_and_increment(self) -> None:
        document = empty_catalog()
        instrument = synthetic_equity()
        instrument["minimum_quantity"] = "0.5"
        instrument["quantity_increment"] = "0.5"
        instrument["quantity_precision"] = 0
        document["calendars"] = [synthetic_calendar()]
        document["instruments"] = [instrument]
        self.assert_invalid(document, "quantity_precision")

    def test_broken_calendar_reference_is_rejected(self) -> None:
        document = empty_catalog()
        instrument = synthetic_equity()
        instrument["calendar_id"] = "MISSING"
        document["calendars"] = [synthetic_calendar()]
        document["instruments"] = [instrument]
        self.assert_invalid(document, "unknown_calendar")

    def test_provider_symbol_cannot_map_to_overlapping_instruments(self) -> None:
        document = empty_catalog()
        second = synthetic_equity()
        second["id"] = "SYNTH-EQ-2"
        document["calendars"] = [synthetic_calendar()]
        document["instruments"] = [synthetic_equity(), second]
        issues = self.assert_invalid(document, "ambiguous_provider_symbol")
        self.assertTrue(any("provider_symbols[0].symbol" in issue.path for issue in issues))

    def test_provider_symbol_can_be_reused_after_prior_validity_ends(self) -> None:
        document = empty_catalog()
        first = synthetic_equity()
        first["valid_to"] = "2031-01-01"
        second = synthetic_equity()
        second["id"] = "SYNTH-EQ-2"
        second["valid_from"] = "2031-01-01"
        second["valid_to"] = "2032-01-01"
        document["calendars"] = [synthetic_calendar()]
        document["instruments"] = [first, second]
        self.assertEqual([], validate_catalog(document))

    def test_fx_pip_must_be_representable_at_price_precision(self) -> None:
        document = empty_catalog()
        instrument = synthetic_fx()
        instrument["price_precision"] = 3
        document["calendars"] = [synthetic_calendar()]
        document["instruments"] = [instrument]
        self.assert_invalid(document, "pip_precision")

    def test_invalid_timezone_and_overlapping_sessions_are_rejected(self) -> None:
        document = empty_catalog()
        calendar = synthetic_calendar()
        calendar["timezone"] = "Mars/Olympus_Mons"
        calendar["weekly_sessions"].append(
            {
                "start_weekday": 0,
                "open_time": "16:00",
                "close_time": "18:00",
                "close_day_offset": 0,
                "kind": "extraordinary",
            }
        )
        document["calendars"] = [calendar]
        issues = self.assert_invalid(document, "timezone")
        self.assertIn("session_overlap", {issue.code for issue in issues})

    def test_calendar_referenced_by_instrument_needs_weekly_sessions(self) -> None:
        document = empty_catalog()
        calendar = synthetic_calendar()
        calendar["weekly_sessions"] = []
        document["calendars"] = [calendar]
        document["instruments"] = [synthetic_equity()]
        self.assert_invalid(document, "empty_calendar")

    def test_incoherent_validity_dates_are_rejected(self) -> None:
        document = empty_catalog()
        instrument = synthetic_equity()
        instrument["valid_to"] = "2029-12-31"
        document["calendars"] = [synthetic_calendar()]
        document["instruments"] = [instrument]
        self.assert_invalid(document, "validity_range")

    def test_equity_cannot_be_used_as_continuous_future_contract(self) -> None:
        document = empty_catalog()
        document["calendars"] = [synthetic_calendar()]
        document["instruments"] = [synthetic_equity()]
        document["continuous_futures"] = [
            {
                "id": "BAD-SERIES",
                "instrument_ids": ["SYNTH-EQ", "MISSING-FUT"],
                "executable": True,
                "roll_policy": {"method": "calendar_date"},
                "adjustment_method": "none",
                "provenance": {"status": "synthetic", "sources": [], "notes": "Test fixture only."},
            }
        ]
        issues = self.assert_invalid(document)
        self.assertIn("const", {issue.code for issue in issues})
        self.assertIn("not_future", {issue.code for issue in issues})
        self.assertIn("unknown_instrument", {issue.code for issue in issues})

    def test_equity_adjustment_requires_declared_events(self) -> None:
        document = empty_catalog()
        instrument = synthetic_equity()
        instrument["historical_price_policy"] = {
            "price_basis": "total_return_adjusted",
            "corporate_action_events": ["split"],
        }
        document["calendars"] = [synthetic_calendar()]
        document["instruments"] = [instrument]
        self.assert_invalid(document, "corporate_action_policy")

    def test_weekly_overnight_session_is_valid(self) -> None:
        document = empty_catalog()
        calendar = synthetic_calendar()
        calendar["weekly_sessions"] = [
            {
                "start_weekday": 6,
                "open_time": "22:00",
                "close_time": "02:00",
                "close_day_offset": 1,
                "kind": "regular",
            }
        ]
        document["calendars"] = [calendar]
        self.assertEqual([], validate_catalog(document))


class CatalogLoadingTests(unittest.TestCase):
    def test_loads_repository_starter_catalog(self) -> None:
        catalog = load_catalog(ROOT / "data" / "market_catalog.json")
        self.assertEqual("1.0.0", catalog.catalog_version)
        self.assertEqual((), catalog.instruments)

    def test_loader_converts_financial_strings_to_decimal(self) -> None:
        document = empty_catalog()
        document["calendars"] = [synthetic_calendar()]
        document["instruments"] = [synthetic_equity()]
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "catalog.json"
            path.write_text(json.dumps(document), encoding="utf-8")
            catalog = load_catalog(path)
        instrument = catalog.instruments[0]
        self.assertIsInstance(instrument["price_tick"], Decimal)
        self.assertEqual(Decimal("0.25"), instrument["price_tick"])
        self.assertEqual("0.25", catalog.to_mapping()["instruments"][0]["price_tick"])

    def test_loader_rejects_json_fractional_numbers_with_a_path(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "catalog.json"
            document = empty_catalog()
            document["calendars"] = [synthetic_calendar()]
            document["instruments"] = [synthetic_equity()]
            encoded = json.dumps(document).replace('"price_tick": "0.25"', '"price_tick": 0.25')
            path.write_text(encoded, encoding="utf-8")
            with self.assertRaises(CatalogValidationError) as raised:
                load_catalog(path)
            self.assertTrue(any(issue.path == "$.instruments[0].price_tick" for issue in raised.exception.issues))

    def test_loader_returns_validation_errors_for_invalid_document(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "catalog.json"
            document = empty_catalog()
            document["extra"] = True
            path.write_text(json.dumps(document), encoding="utf-8")
            with self.assertRaises(CatalogValidationError) as raised:
                load_catalog(path)
            self.assertTrue(any(issue.path == "$.extra" for issue in raised.exception.issues))

    def test_cli_returns_success_for_starter_catalog(self) -> None:
        output = StringIO()
        with redirect_stdout(output), redirect_stderr(StringIO()):
            result = cli_main(["validate", str(ROOT / "data" / "market_catalog.json")])
        self.assertEqual(0, result)
        self.assertIn("Valid catalog 1.0.0", output.getvalue())

    def test_cli_returns_nonzero_for_invalid_catalog(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "catalog.json"
            path.write_text('{"schema_version":"9.0.0"}', encoding="utf-8")
            error = StringIO()
            with redirect_stdout(StringIO()), redirect_stderr(error):
                result = cli_main(["validate", str(path)])
        self.assertNotEqual(0, result)
        self.assertIn("Catalog validation failed", error.getvalue())


if __name__ == "__main__":
    unittest.main()
