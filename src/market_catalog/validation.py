"""JSON Schema and semantic validation for market catalogs."""

from __future__ import annotations

from datetime import date, time
from decimal import Decimal, InvalidOperation
from functools import lru_cache
from importlib.resources import files
from pathlib import Path
import sys
from typing import Any, Mapping
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from jsonschema import Draft202012Validator, FormatChecker

from .models import Catalog, ValidationIssue

SCHEMA_VERSION = "1.0.0"
SCHEMA_FILENAME = "market_catalog.schema.json"
SECONDS_PER_DAY = 24 * 60 * 60
SECONDS_PER_WEEK = 7 * SECONDS_PER_DAY


def _schema_path() -> Path:
    """Find the source-tree schema or its installed data-file copy."""

    source_tree_path = Path(__file__).resolve().parents[2] / "schemas" / SCHEMA_FILENAME
    if source_tree_path.is_file():
        return source_tree_path

    installed_path = Path(sys.prefix) / "share" / "market-catalog" / "schemas" / SCHEMA_FILENAME
    if installed_path.is_file():
        return installed_path

    # ``files`` is retained as a final option for future package-data layouts.
    resource_path = files("market_catalog").joinpath("schemas", SCHEMA_FILENAME)
    if resource_path.is_file():
        return Path(str(resource_path))
    raise FileNotFoundError(f"Could not locate {SCHEMA_FILENAME}")


@lru_cache(maxsize=1)
def _schema() -> dict[str, Any]:
    import json

    schema = json.loads(_schema_path().read_text(encoding="utf-8"))
    Draft202012Validator.check_schema(schema)
    return schema


def _json_path(parts: Any) -> str:
    result = "$"
    for part in parts:
        if isinstance(part, int):
            result += f"[{part}]"
        elif isinstance(part, str) and part.isidentifier():
            result += f".{part}"
        else:
            result += f"[{part!r}]"
    return result


def _as_mapping(value: Catalog | Mapping[str, Any]) -> Mapping[str, Any]:
    if isinstance(value, Catalog):
        return value.to_mapping()
    return value


def _issue(issues: list[ValidationIssue], path: str, code: str, message: str) -> None:
    issues.append(ValidationIssue(path=path, code=code, message=message))


def _schema_error_message(error: Any) -> str:
    """Add a direct correction hint to common JSON Schema failures."""

    keyword = error.validator
    if keyword == "pattern":
        pattern = error.schema.get("pattern") if isinstance(error.schema, Mapping) else None
        if pattern == "^(0|[1-9][0-9]*)(\\.[0-9]+)?$":
            return "Use a non-negative base-10 decimal string such as '0.25'; do not use a JSON number or exponent notation."
        return f"Use a string that matches the required format ({pattern!r})."
    if keyword == "format":
        if error.validator_value == "date":
            return "Use a valid calendar date in ISO format YYYY-MM-DD."
        if error.validator_value == "uri":
            return "Use an absolute source URI, for example an official documentation URL."
        return f"Use a value with the required {error.validator_value!r} format."
    if keyword == "enum":
        choices = ", ".join(repr(value) for value in error.validator_value)
        return f"Choose one of the allowed values: {choices}."
    if keyword == "const":
        return f"Set this field to {error.validator_value!r}."
    if keyword == "type":
        expected = error.validator_value
        if isinstance(expected, list):
            expected_text = " or ".join(expected)
        else:
            expected_text = str(expected)
        return f"Provide a JSON value of type {expected_text}; the current value has the wrong type."
    if keyword == "minItems":
        return f"Provide at least {error.validator_value} item(s)."
    if keyword == "maxItems":
        return f"Provide no more than {error.validator_value} item(s)."
    if keyword == "minimum":
        return f"Use a value greater than or equal to {error.validator_value}."
    return f"{error.message} See schemas/{SCHEMA_FILENAME} for the field contract."


def _decimal(value: Any, path: str, issues: list[ValidationIssue]) -> Decimal | None:
    if not isinstance(value, str):
        return None
    try:
        return Decimal(value)
    except InvalidOperation:
        return None  # The schema reports the malformed representation.


def _positive_decimal(value: Any, path: str, issues: list[ValidationIssue]) -> Decimal | None:
    parsed = _decimal(value, path, issues)
    if parsed is not None and (not parsed.is_finite() or parsed <= 0):
        _issue(issues, path, "positive", "Use a decimal string greater than zero.")
        return None
    return parsed


def _decimal_places(value: Decimal) -> int:
    normalized = value.normalize()
    return max(0, -normalized.as_tuple().exponent)


def _time_seconds(value: Any) -> int | None:
    if not isinstance(value, str):
        return None
    try:
        parsed = time.fromisoformat(value)
    except ValueError:
        return None
    return parsed.hour * 3600 + parsed.minute * 60 + parsed.second


def _intervals_overlap(left: tuple[int, int], right: tuple[int, int]) -> bool:
    return left[0] < right[1] and right[0] < left[1]


def _session_interval(
    session: Mapping[str, Any],
    path: str,
    issues: list[ValidationIssue],
    *,
    weekday_anchored: bool,
) -> tuple[int, int] | None:
    open_seconds = _time_seconds(session.get("open_time"))
    close_seconds = _time_seconds(session.get("close_time"))
    offset = session.get("close_day_offset")
    if open_seconds is None or close_seconds is None or not isinstance(offset, int):
        return None

    day_offset = offset * SECONDS_PER_DAY
    if weekday_anchored:
        weekday = session.get("start_weekday")
        if not isinstance(weekday, int):
            return None
        start = weekday * SECONDS_PER_DAY + open_seconds
    else:
        start = open_seconds
    end = (start // SECONDS_PER_DAY * SECONDS_PER_DAY) + day_offset + close_seconds

    if end <= start:
        _issue(
            issues,
            path,
            "session_order",
            "The close must follow the open; set close_day_offset to 1 for an overnight session.",
        )
        return None
    return start, end


def _split_week_interval(interval: tuple[int, int]) -> list[tuple[int, int]]:
    start, end = interval
    if end <= SECONDS_PER_WEEK:
        return [(start, end)]
    return [(start, SECONDS_PER_WEEK), (0, end - SECONDS_PER_WEEK)]


def _validate_session_collection(
    sessions: Any,
    path: str,
    issues: list[ValidationIssue],
    *,
    weekday_anchored: bool,
) -> None:
    if not isinstance(sessions, list):
        return
    intervals: list[tuple[int, int, str]] = []
    for index, session in enumerate(sessions):
        if not isinstance(session, Mapping):
            continue
        session_path = f"{path}[{index}]"
        interval = _session_interval(
            session, session_path, issues, weekday_anchored=weekday_anchored
        )
        if interval is not None:
            intervals.append((interval[0], interval[1], session_path))

    for left_index, (left_start, left_end, left_path) in enumerate(intervals):
        left_parts = (
            _split_week_interval((left_start, left_end))
            if weekday_anchored
            else [(left_start, left_end)]
        )
        for right_start, right_end, right_path in intervals[left_index + 1 :]:
            right_parts = (
                _split_week_interval((right_start, right_end))
                if weekday_anchored
                else [(right_start, right_end)]
            )
            if any(_intervals_overlap(a, b) for a in left_parts for b in right_parts):
                _issue(
                    issues,
                    right_path,
                    "session_overlap",
                    f"This session overlaps {left_path}.",
                )


def _validate_semantics(document: Mapping[str, Any], issues: list[ValidationIssue]) -> None:
    calendars = document.get("calendars", [])
    instruments = document.get("instruments", [])
    continuous = document.get("continuous_futures", [])
    if not isinstance(calendars, list) or not isinstance(instruments, list) or not isinstance(continuous, list):
        return

    schema_version = document.get("schema_version")
    if schema_version != SCHEMA_VERSION:
        _issue(
            issues,
            "$.schema_version",
            "unsupported_schema_version",
            f"Supported schema_version is {SCHEMA_VERSION!r}; migrate the catalog before loading it.",
        )

    calendar_by_id: dict[str, Mapping[str, Any]] = {}
    for index, calendar in enumerate(calendars):
        if not isinstance(calendar, Mapping):
            continue
        path = f"$.calendars[{index}]"
        calendar_id = calendar.get("id")
        if isinstance(calendar_id, str):
            if calendar_id in calendar_by_id:
                _issue(issues, f"{path}.id", "duplicate_id", f"Calendar id {calendar_id!r} is already defined.")
            calendar_by_id[calendar_id] = calendar

        timezone = calendar.get("timezone")
        if isinstance(timezone, str):
            try:
                ZoneInfo(timezone)
            except (ZoneInfoNotFoundError, ValueError):
                _issue(issues, f"{path}.timezone", "timezone", "Use a valid IANA timezone name available to this Python runtime.")

        _validate_session_collection(
            calendar.get("weekly_sessions"),
            f"{path}.weekly_sessions",
            issues,
            weekday_anchored=True,
        )

        overrides = calendar.get("date_overrides", [])
        if isinstance(overrides, list):
            seen_dates: set[str] = set()
            for override_index, override in enumerate(overrides):
                if not isinstance(override, Mapping):
                    continue
                override_path = f"{path}.date_overrides[{override_index}]"
                override_date = override.get("date")
                if isinstance(override_date, str):
                    if override_date in seen_dates:
                        _issue(issues, f"{override_path}.date", "duplicate_date", f"Date override {override_date!r} is already defined.")
                    seen_dates.add(override_date)
                if override.get("status") == "override":
                    _validate_session_collection(
                        override.get("sessions"),
                        f"{override_path}.sessions",
                        issues,
                        weekday_anchored=False,
                    )

    instrument_by_id: dict[str, Mapping[str, Any]] = {}
    provider_symbol_owners: dict[tuple[str, str], list[tuple[date, date | None, str]]] = {}
    for index, instrument in enumerate(instruments):
        if not isinstance(instrument, Mapping):
            continue
        path = f"$.instruments[{index}]"
        instrument_id = instrument.get("id")
        if isinstance(instrument_id, str):
            if instrument_id in instrument_by_id:
                _issue(issues, f"{path}.id", "duplicate_id", f"Instrument id {instrument_id!r} is already defined.")
            instrument_by_id[instrument_id] = instrument

        calendar_id = instrument.get("calendar_id")
        if isinstance(calendar_id, str) and calendar_id not in calendar_by_id:
            _issue(issues, f"{path}.calendar_id", "unknown_calendar", f"Calendar {calendar_id!r} is not defined.")
        elif isinstance(calendar_id, str) and not calendar_by_id[calendar_id].get("weekly_sessions"):
            calendar_index = next(
                index for index, item in enumerate(calendars)
                if isinstance(item, Mapping) and item.get("id") == calendar_id
            )
            _issue(
                issues,
                f"$.calendars[{calendar_index}].weekly_sessions",
                "empty_calendar",
                "A calendar referenced by an instrument must define at least one weekly session.",
            )

        valid_from = instrument.get("valid_from")
        valid_to = instrument.get("valid_to")
        if isinstance(valid_from, str) and isinstance(valid_to, str):
            try:
                if date.fromisoformat(valid_to) <= date.fromisoformat(valid_from):
                    _issue(issues, f"{path}.valid_to", "validity_range", "valid_to is exclusive and must be later than valid_from.")
            except ValueError:
                pass  # JSON Schema reports invalid dates.

        price_tick = _positive_decimal(instrument.get("price_tick"), f"{path}.price_tick", issues)
        minimum = _positive_decimal(instrument.get("minimum_quantity"), f"{path}.minimum_quantity", issues)
        increment = _positive_decimal(instrument.get("quantity_increment"), f"{path}.quantity_increment", issues)
        if increment is not None and minimum is not None and increment > 0:
            if minimum % increment != 0:
                _issue(issues, f"{path}.minimum_quantity", "quantity_multiple", "minimum_quantity must be an exact multiple of quantity_increment.")

        precision = instrument.get("price_precision")
        if price_tick is not None and isinstance(precision, int):
            if _decimal_places(price_tick) > precision:
                _issue(issues, f"{path}.price_precision", "price_precision", "price_precision must represent every decimal place in price_tick.")

        quantity_precision = instrument.get("quantity_precision")
        if isinstance(quantity_precision, int):
            for field_name, quantity in (
                ("minimum_quantity", minimum),
                ("quantity_increment", increment),
            ):
                if quantity is not None and _decimal_places(quantity) > quantity_precision:
                    _issue(
                        issues,
                        f"{path}.quantity_precision",
                        "quantity_precision",
                        f"quantity_precision must represent every decimal place in {field_name}.",
                    )

        providers = instrument.get("provider_symbols", [])
        if isinstance(providers, list):
            seen_providers: set[str] = set()
            for provider_index, provider in enumerate(providers):
                if not isinstance(provider, Mapping):
                    continue
                provider_id = provider.get("provider_id")
                if isinstance(provider_id, str):
                    if provider_id in seen_providers:
                        _issue(issues, f"{path}.provider_symbols[{provider_index}].provider_id", "duplicate_provider", f"Provider {provider_id!r} is listed more than once for this instrument.")
                    seen_providers.add(provider_id)

                symbol = provider.get("symbol")
                if isinstance(provider_id, str) and isinstance(symbol, str):
                    try:
                        valid_from_date = date.fromisoformat(instrument["valid_from"])
                        valid_to_date = (
                            date.fromisoformat(instrument["valid_to"])
                            if isinstance(instrument.get("valid_to"), str)
                            else None
                        )
                    except (KeyError, TypeError, ValueError):
                        # JSON Schema reports absent or malformed validity dates.
                        continue
                    owner_key = (provider_id, symbol)
                    for prior_start, prior_end, prior_path in provider_symbol_owners.get(owner_key, []):
                        if (prior_end is None or valid_from_date < prior_end) and (
                            valid_to_date is None or prior_start < valid_to_date
                        ):
                            _issue(
                                issues,
                                f"{path}.provider_symbols[{provider_index}].symbol",
                                "ambiguous_provider_symbol",
                                f"Provider symbol {symbol!r} for {provider_id!r} overlaps the validity period at {prior_path}; use a distinct symbol or non-overlapping validity dates.",
                            )
                    provider_symbol_owners.setdefault(owner_key, []).append(
                        (valid_from_date, valid_to_date, f"{path}.provider_symbols[{provider_index}].symbol")
                    )

        asset_class = instrument.get("asset_class")
        if asset_class == "fx":
            convention = instrument.get("fx_convention")
            if isinstance(convention, Mapping):
                base = convention.get("base_currency")
                quote = instrument.get("quote_currency")
                if base == quote:
                    _issue(issues, f"{path}.fx_convention.base_currency", "currency_pair", "Base and quote currencies must differ.")
                if convention.get("pip_value_currency") != quote:
                    _issue(issues, f"{path}.fx_convention.pip_value_currency", "pip_currency", "The initial pip value rule is denominated in the instrument quote currency.")
                pip_size = _positive_decimal(convention.get("pip_size"), f"{path}.fx_convention.pip_size", issues)
                if pip_size is not None and isinstance(instrument.get("price_precision"), int):
                    if _decimal_places(pip_size) > instrument["price_precision"]:
                        _issue(
                            issues,
                            f"{path}.fx_convention.pip_size",
                            "pip_precision",
                            "pip_size must be representable at the instrument's declared price_precision.",
                        )

        if asset_class == "future":
            contract = instrument.get("future_contract")
            if isinstance(contract, Mapping):
                _positive_decimal(contract.get("contract_multiplier"), f"{path}.future_contract.contract_multiplier", issues)
                last_trade = contract.get("last_trade_date")
                expiry = contract.get("expiry_date")
                if isinstance(last_trade, str) and isinstance(expiry, str):
                    try:
                        if date.fromisoformat(last_trade) > date.fromisoformat(expiry):
                            _issue(issues, f"{path}.future_contract.last_trade_date", "future_dates", "last_trade_date must not be later than expiry_date.")
                    except ValueError:
                        pass
        if asset_class == "equity":
            history = instrument.get("historical_price_policy")
            if isinstance(history, Mapping):
                basis = history.get("price_basis")
                actions = history.get("corporate_action_events", [])
                declared_actions = set(actions) if isinstance(actions, (list, tuple)) else set()
                if basis == "split_adjusted" and "split" not in declared_actions:
                    _issue(issues, f"{path}.historical_price_policy.corporate_action_events", "corporate_action_policy", "split_adjusted prices must declare the split event policy.")
                if basis == "total_return_adjusted" and not {"split", "dividend"}.issubset(declared_actions):
                    _issue(issues, f"{path}.historical_price_policy.corporate_action_events", "corporate_action_policy", "total_return_adjusted prices must declare both split and dividend event policies.")

    seen_series: set[str] = set()
    for index, series in enumerate(continuous):
        if not isinstance(series, Mapping):
            continue
        path = f"$.continuous_futures[{index}]"
        series_id = series.get("id")
        if isinstance(series_id, str):
            if series_id in seen_series:
                _issue(issues, f"{path}.id", "duplicate_id", f"Continuous series id {series_id!r} is already defined.")
            seen_series.add(series_id)
        ids = series.get("instrument_ids", [])
        if isinstance(ids, list):
            for contract_index, contract_id in enumerate(ids):
                contract = instrument_by_id.get(contract_id) if isinstance(contract_id, str) else None
                if contract is None:
                    _issue(issues, f"{path}.instrument_ids[{contract_index}]", "unknown_instrument", f"Instrument {contract_id!r} is not defined.")
                elif contract.get("asset_class") != "future":
                    _issue(issues, f"{path}.instrument_ids[{contract_index}]", "not_future", f"Instrument {contract_id!r} is not a listed future contract.")


def validate_catalog(catalog: Catalog | Mapping[str, Any]) -> list[ValidationIssue]:
    """Return schema and semantic issues; an empty list means the catalog is valid."""

    document = _as_mapping(catalog)
    issues: list[ValidationIssue] = []
    schema = _schema()
    validator = Draft202012Validator(schema, format_checker=FormatChecker())
    for error in validator.iter_errors(document):
        path = _json_path(error.absolute_path)
        if error.validator == "required" and isinstance(error.instance, Mapping):
            missing = [name for name in error.validator_value if name not in error.instance]
            if missing:
                for name in missing:
                    _issue(
                        issues,
                        _json_path([*error.absolute_path, name]),
                        "required",
                        f"Missing required field {name!r}; provide it for this catalog record.",
                    )
                continue
        if error.validator == "additionalProperties" and isinstance(error.instance, Mapping):
            allowed = error.schema.get("properties", {})
            unexpected = sorted(set(error.instance) - set(allowed))
            if unexpected:
                for name in unexpected:
                    _issue(
                        issues,
                        _json_path([*error.absolute_path, name]),
                        "additionalProperties",
                        f"Unknown field {name!r}; remove it or update the catalog schema.",
                    )
                continue
        _issue(
            issues,
            path,
            str(error.validator or "schema"),
            _schema_error_message(error),
        )
    if isinstance(document, Mapping):
        _validate_semantics(document, issues)
    else:
        _issue(issues, "$", "type", "Catalog root must be a JSON object.")

    unique: dict[tuple[str, str, str], ValidationIssue] = {}
    for item in issues:
        unique[(item.path, item.code, item.message)] = item
    return sorted(unique.values(), key=lambda item: (item.path, item.code, item.message))
