"""Daily equity bar diagnostics against an explicitly reviewed exchange calendar."""

from __future__ import annotations

import numpy as np
import pandas as pd


class HistoricalQualityError(ValueError):
    def __init__(self, report: dict):
        self.report = report
        failures = {key: value for key, value in report["checks"].items() if value}
        prefix = "session mismatch; " if failures.keys() & {"missing_sessions", "unexpected_sessions", "duplicate_dates", "unordered_dates"} else ""
        super().__init__(f"{report['instrument_id']}: {prefix}daily bar quality failed: {failures}; "
                         f"missing sessions={report['sessions']['missing']}")


def assess_daily_bars(frame: pd.DataFrame, instrument_id: str, calendar: dict,
                      start: str, end: str) -> dict:
    """Inspect without sorting, filling, rounding or otherwise repairing the input.

    start/end are inclusive local session dates. A missing expected bar is an
    absence of data with unknown cause, not evidence of a market closure.
    """
    first, last = pd.Timestamp(start), pd.Timestamp(end)
    if first > last or first.year != calendar["year"] or last.year != calendar["year"]:
        raise ValueError("Requested period is outside the reviewed calendar year")
    days = pd.date_range(first, last, freq="D")
    holidays = set(calendar["holidays"])
    expected = [d.date().isoformat() for d in days if d.weekday() < 5 and d.date().isoformat() not in holidays]
    closed = [{"date": d.date().isoformat(), "reason": "weekend" if d.weekday() >= 5 else "exchange_holiday"}
              for d in days if d.weekday() >= 5 or d.date().isoformat() in holidays]
    required = {"date", "open", "high", "low", "close", "volume", "instrument_id"}
    missing_columns = sorted(required - set(frame.columns))
    data = frame.reset_index(drop=True).reindex(columns=sorted(required))
    dates = pd.to_datetime(data["date"], errors="coerce", utc=True)
    local_days = dates.dt.tz_convert(calendar["timezone"]).dt.strftime("%Y-%m-%d")
    observed = set(local_days.dropna())
    prices = data[["open", "high", "low", "close"]].apply(pd.to_numeric, errors="coerce").astype(float)
    volume = pd.to_numeric(data["volume"], errors="coerce").astype(float)
    finite_prices = np.isfinite(prices).all(axis=1)
    finite_volume = np.isfinite(volume)
    # Tolerance is solely for binary floating-point noise at an OHLC boundary.
    def greater(left, right):
        return (left > right) & ~np.isclose(left, right, rtol=1e-10, atol=1e-10)
    masks = {
        "invalid_dates": dates.isna(),
        "duplicate_dates": dates.notna() & dates.duplicated(),
        "duplicate_sessions": local_days.notna() & local_days.duplicated(),
        "unordered_dates": dates.diff().dt.total_seconds() < 0,
        "nonfinite_prices": ~finite_prices,
        "nonpositive_prices": finite_prices & (prices <= 0).any(axis=1),
        "inconsistent_ohlc": finite_prices & (
            greater(prices.low, prices[["open", "close"]].min(axis=1)) |
            greater(prices[["open", "close"]].max(axis=1), prices.high) | greater(prices.low, prices.high)),
        "nonfinite_volume": ~finite_volume,
        "negative_volume": finite_volume & (volume < 0),
        "fractional_volume": finite_volume & (volume != np.floor(volume)),
        "wrong_instrument": data.instrument_id.isna() | data.instrument_id.ne(instrument_id).fillna(True),
    }
    bad_rows = {name: mask[mask].index.tolist() for name, mask in masks.items()}
    missing = sorted(set(expected) - observed)
    unexpected = sorted(observed - set(expected))
    checks = {"empty_dataset": int(frame.empty), "missing_columns": len(missing_columns),
              **{name: len(rows) for name, rows in bad_rows.items()},
              "missing_sessions": len(missing), "unexpected_sessions": len(unexpected)}
    return {
        "instrument_id": instrument_id, "rows": len(frame), "successful": not any(checks.values()),
        "period": {"start_inclusive": start, "end_inclusive": end}, "timezone": calendar["timezone"],
        "checks": checks, "invalid_rows_zero_based": bad_rows, "missing_columns": missing_columns,
        "sessions": {"expected_count": len(expected), "observed_unique_count": len(observed),
                     "missing": missing, "unexpected": unexpected, "market_closed": closed,
                     "early_closes": [{"date": day, "close_local": hour} for day, hour in
                                      calendar["early_closes"].items() if start <= day <= end],
                     "missing_reason": "unknown; an expected bar is absent, not a confirmed market closure"},
        "zero_volume_sessions": sorted(local_days[volume.eq(0)].dropna().unique().tolist()),
        "zero_volume_policy": "Allowed and reported; zero volume alone does not prove missing data or a trading halt",
    }


def validate_daily_bars(frame: pd.DataFrame, instrument_id: str, calendar: dict,
                        start: str, end: str) -> dict:
    report = assess_daily_bars(frame, instrument_id, calendar, start, end)
    if not report["successful"]:
        raise HistoricalQualityError(report)
    return report
