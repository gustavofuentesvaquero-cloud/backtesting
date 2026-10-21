"""Download, freeze and independently revalidate the initial 2024 equity dataset."""

from __future__ import annotations

import argparse
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import shutil
from urllib.parse import urlencode
from urllib.request import Request, urlopen

import duckdb
import numpy as np
import pandas as pd

from scripts.data_baseline import inspect_bars

ROOT = Path(__file__).resolve().parents[1]
DATASET_ID = "nyse-equities-2024-v1"
DEFAULT_TARGET = ROOT / "data" / "frozen" / DATASET_ID
DEFAULT_DOWNLOADS = ROOT / "artifacts" / "downloads" / "nyse-2024-initial"
SYMBOLS = ("BE", "NEM")
IDENTITIES = {
    "BE": {"name": "Bloom Energy Corporation", "cik": "0001664703", "share_class": "Class A common stock",
           "sources": ["https://www.sec.gov/Archives/edgar/data/1664703/000162828025008747/be-20241231.htm"]},
    "NEM": {"name": "Newmont Corporation", "cik": "0001164727", "share_class": "Common stock",
            "sources": ["https://investors.newmont.com/investors/news-release/news-details/2025/Newmont-Reports-Fourth-Quarter-and-Full-Year-2024-Results-Provides-Full-Year-2025-Guidance/default.aspx"]},
}
CALENDAR = {
    "venue": "XNYS", "timezone": "America/New_York", "year": 2024,
    "open": "09:30", "close": "16:00",
    "holidays": ["2024-01-01", "2024-01-15", "2024-02-19", "2024-03-29", "2024-05-27",
                 "2024-06-19", "2024-07-04", "2024-09-02", "2024-11-28", "2024-12-25"],
    "early_closes": {"2024-07-03": "13:00", "2024-11-29": "13:00", "2024-12-24": "13:00"},
    "source": "https://s2.q4cdn.com/154085107/files/doc_news/NYSE-Group-Announces-2024-2025-and-2026-Holiday-and-Early-Closings-Calendar-2023.pdf",
}
POLICY = {
    "ohlc": "Yahoo quote OHLC, split-adjusted by provider; no dividend adjustment applied by this pipeline",
    "adjusted_close": "Yahoo adjclose retained for audit only; never substituted for close",
    "dividends": "USD per share on provider ex-date; preserved, not credited by this pipeline",
    "splits": "No splits reported in this sample; reject a source that reports any, pending explicit handling",
    "date": "UTC scheduled session close; session_date is the NYSE local trading date",
    "source_timestamp": "Yahoo daily timestamp must match 09:30 America/New_York",
    "availability": "Scheduled close is a bar label, not a guarantee of provider publication time; trade no earlier than next session",
    "missing_sessions": "Reject; do not fill or interpolate",
    "rounding": "Preserve provider floating point values without rounding",
}


def sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def write_json(path: Path, value) -> None:
    path.write_text(json.dumps(value, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")


def source_url(symbol: str) -> str:
    params = {"period1": 1704067200, "period2": 1735689600, "interval": "1d",
              "events": "div,splits", "includeAdjustedClose": "true"}
    return f"https://query1.finance.yahoo.com/v8/finance/chart/{symbol}?{urlencode(params)}"


def sessions() -> list[str]:
    return [day.date().isoformat() for day in pd.date_range("2024-01-01", "2024-12-31", freq="B")
            if day.date().isoformat() not in CALENDAR["holidays"]]


def normalize(raw: dict, symbol: str) -> pd.DataFrame:
    """Validate source identity, complete sessions, actions and OHLCV before writing."""
    if symbol not in SYMBOLS:
        raise ValueError(f"Unsupported instrument: {symbol}")
    chart = raw["chart"]
    if chart.get("error") or not chart.get("result") or len(chart["result"]) != 1:
        raise ValueError("Provider returned an error or ambiguous chart result")
    result = chart["result"][0]
    meta = result["meta"]
    identity = {"symbol": symbol, "longName": IDENTITIES[symbol]["name"], "exchangeName": "NYQ",
                "instrumentType": "EQUITY", "currency": "USD", "exchangeTimezoneName": CALENDAR["timezone"]}
    for key, expected in identity.items():
        if meta.get(key) != expected:
            raise ValueError(f"{symbol}: identity mismatch for {key}: {meta.get(key)!r}")
    timestamps = pd.to_datetime(result["timestamp"], unit="s", utc=True)
    local = timestamps.tz_convert(CALENDAR["timezone"])
    days = list(local.strftime("%Y-%m-%d"))
    expected_days = sessions()
    if days != expected_days:
        missing = sorted(set(expected_days) - set(days))
        extra = sorted(set(days) - set(expected_days))
        raise ValueError(f"{symbol}: session mismatch (missing={missing}, extra={extra}); duplicates/order also rejected")
    if set(local.strftime("%H:%M:%S")) != {"09:30:00"}:
        raise ValueError(f"{symbol}: unexpected source session timestamp")
    quote = result["indicators"]["quote"][0]
    frame = pd.DataFrame({key: quote[key] for key in ("open", "high", "low", "close", "volume")})
    if len(frame) != len(days):
        raise ValueError(f"{symbol}: inconsistent quote lengths")
    frame = frame.astype(float)
    prices = frame[["open", "high", "low", "close"]]
    if not np.isfinite(frame.to_numpy()).all() or (prices <= 0).any().any() or (frame.volume < 0).any():
        raise ValueError(f"{symbol}: nonfinite/nonpositive prices or invalid volume")
    if (frame.volume != np.floor(frame.volume)).any():
        raise ValueError(f"{symbol}: fractional share volume")
    if ((frame.low > prices[["open", "close"]].min(axis=1) + 1e-8) |
        (frame.high < prices[["open", "close"]].max(axis=1) - 1e-8) | (frame.low > frame.high)).any():
        raise ValueError(f"{symbol}: inconsistent OHLC")
    adjusted = np.asarray(result["indicators"]["adjclose"][0]["adjclose"], dtype=float)
    if len(adjusted) != len(days) or not np.isfinite(adjusted).all() or (adjusted <= 0).any():
        raise ValueError(f"{symbol}: invalid adjusted close")
    frame["adj_close"] = adjusted
    frame["session_date"] = days
    frame["source_timestamp"] = timestamps
    closes = [pd.Timestamp(f"{day} {CALENDAR['early_closes'].get(day, CALENDAR['close'])}",
                          tz=CALENDAR["timezone"]).tz_convert("UTC") for day in days]
    frame["date"] = pd.to_datetime(closes, utc=True)
    frame["instrument_id"] = symbol
    frame["dividend"] = 0.0
    events = result.get("events", {})
    if set(events) - {"dividends", "splits"}:
        raise ValueError(f"{symbol}: unsupported corporate action")
    if events.get("splits"):
        raise ValueError(f"{symbol}: splits require a new explicit dataset policy")
    seen = set()
    for event in events.get("dividends", {}).values():
        day = pd.Timestamp(event["date"], unit="s", tz="UTC").tz_convert(CALENDAR["timezone"]).date().isoformat()
        amount = float(event["amount"])
        if day not in days or day in seen or not np.isfinite(amount) or amount <= 0:
            raise ValueError(f"{symbol}: invalid dividend event")
        seen.add(day)
        frame.loc[frame.session_date == day, "dividend"] = amount
    return frame[["date", "session_date", "open", "high", "low", "close", "volume", "instrument_id",
                  "adj_close", "dividend", "source_timestamp"]]


def validate_catalog(catalog: dict) -> None:
    instruments = {item["id"]: item for item in catalog["instruments"]}
    for symbol in SYMBOLS:
        item = instruments[symbol]
        symbols = {s["provider_id"]: s["symbol"] for s in item["provider_symbols"]}
        if (item["asset_class"] != "equity" or item["venue"] != "NYSE" or
            item["quote_currency"] != "USD" or symbols.get("YFINANCE") != symbol or
            item["valid_from"] > "2024-01-02" or (item.get("valid_to") and item["valid_to"] < "2024-12-31")):
            raise ValueError(f"{symbol}: catalog identity/validity mismatch")


def download(target: Path) -> None:
    if target.exists():
        raise ValueError(f"Refusing to overwrite downloads: {target}")
    target.mkdir(parents=True)
    for symbol in SYMBOLS:
        url = source_url(symbol)
        with urlopen(Request(url, headers={"User-Agent": "Mozilla/5.0"}), timeout=60) as response:
            raw = response.read()
        normalize(json.loads(raw), symbol)
        (target / f"{symbol}.json").write_bytes(raw)
        write_json(target / f"{symbol}.receipt.json", {
            "requested_url": url, "retrieved_at_utc": datetime.now(timezone.utc).isoformat(),
            "sha256": hashlib.sha256(raw).hexdigest(),
        })


def read_downloads(source: Path) -> tuple[dict, dict]:
    frames, receipts = {}, {}
    for symbol in SYMBOLS:
        path = source / f"{symbol}.json"
        receipt = json.loads((source / f"{symbol}.receipt.json").read_text(encoding="utf-8"))
        if receipt["requested_url"] != source_url(symbol):
            raise ValueError(f"{symbol}: unexpected download parameters")
        stamp = datetime.fromisoformat(receipt["retrieved_at_utc"])
        if stamp.utcoffset() is None or stamp < datetime(2025, 1, 1, tzinfo=timezone.utc):
            raise ValueError(f"{symbol}: invalid retrieval timestamp")
        if receipt.get("sha256", sha256(path)) != sha256(path):
            raise ValueError(f"{symbol}: source download checksum mismatch")
        frames[symbol] = normalize(json.loads(path.read_text(encoding="utf-8")), symbol)
        receipts[symbol] = receipt
    return frames, receipts


def freeze(source: Path, target: Path, catalog_path: Path = ROOT / "data" / "gold_miners_stocks.json") -> dict:
    if target.exists():
        raise ValueError(f"Refusing to overwrite frozen dataset: {target}")
    catalog = json.loads(catalog_path.read_text(encoding="utf-8"))
    validate_catalog(catalog)
    frames, receipts = read_downloads(source)
    target.mkdir(parents=True)
    (target / "source").mkdir()
    write_json(target / "catalog.json", catalog)
    write_json(target / "calendar.json", CALENDAR)
    instruments = {}
    for symbol, frame in frames.items():
        for suffix in ("json", "receipt.json"):
            shutil.copyfile(source / f"{symbol}.{suffix}", target / "source" / f"{symbol}.{suffix}")
        path = target / f"{symbol}_1d.parquet"
        with duckdb.connect(":memory:") as con:
            con.register("bars", frame)
            con.execute("COPY bars TO ? (FORMAT PARQUET, COMPRESSION ZSTD)", [str(path)])
        instruments[symbol] = {**IDENTITIES[symbol], "venue": "XNYS", "currency": "USD",
            "provider": "Yahoo Finance chart v8", "provider_symbol": symbol,
            "retrieved_at_utc": receipts[symbol]["retrieved_at_utc"],
            "quality": inspect_bars(path, symbol),
            "dividends": frame.loc[frame.dividend != 0, ["session_date", "dividend"]].to_dict("records"),
            "splits_reported": 0}
    manifest = {
        "format_version": 1, "dataset_id": DATASET_ID,
        "frozen_at_utc": datetime.now(timezone.utc).isoformat(),
        "period": {"start_inclusive": "2024-01-01", "end_exclusive": "2025-01-01"},
        "expected_sessions_per_instrument": len(sessions()), "policy": POLICY,
        "identity_verification": "Issuer/SEC sources reviewed; chart metadata and catalog checked on freezing",
        "historical_verification": "Exact source-to-Parquet equality, structural checks and complete NYSE sessions",
        "limits": ["Single price provider; prices are not independently certified against exchange trades.",
                   "Downloaded vintage, not a point-in-time archive; adjusted close may reflect later actions.",
                   "Dividend events use ex-dates, not cash payment dates; total-return accounting remains separate.",
                   "Two selected surviving instruments and one year are an initial technical sample, not an unbiased universe."],
        "instruments": instruments,
        "files": {p.relative_to(target).as_posix(): sha256(p) for p in sorted(target.rglob("*")) if p.is_file()},
    }
    write_json(target / "manifest.json", manifest)
    return verify(target)


def verify(target: Path = DEFAULT_TARGET) -> dict:
    manifest = json.loads((target / "manifest.json").read_text(encoding="utf-8"))
    if (manifest["format_version"] != 1 or manifest["dataset_id"] != DATASET_ID or
        manifest["policy"] != POLICY or set(manifest["instruments"]) != set(SYMBOLS) or
        manifest["expected_sessions_per_instrument"] != len(sessions()) or
        manifest["period"] != {"start_inclusive": "2024-01-01", "end_exclusive": "2025-01-01"}):
        raise ValueError("Unsupported dataset contract")
    required = {"catalog.json", "calendar.json"} | {f"{s}_1d.parquet" for s in SYMBOLS} | {
        f"source/{s}.{suffix}" for s in SYMBOLS for suffix in ("json", "receipt.json")}
    if set(manifest["files"]) != required:
        raise ValueError("Unexpected or missing dataset file inventory")
    for name, checksum in manifest["files"].items():
        if sha256(target / name) != checksum:
            raise ValueError(f"Dataset checksum mismatch: {name}")
    if json.loads((target / "calendar.json").read_text()) != CALENDAR:
        raise ValueError("Calendar differs from reviewed NYSE calendar")
    validate_catalog(json.loads((target / "catalog.json").read_text(encoding="utf-8")))
    frames, receipts = read_downloads(target / "source")
    for symbol, expected in frames.items():
        item = manifest["instruments"][symbol]
        if any(item[key] != value for key, value in IDENTITIES[symbol].items()):
            raise ValueError(f"{symbol}: frozen identity evidence changed")
        if item["retrieved_at_utc"] != receipts[symbol]["retrieved_at_utc"]:
            raise ValueError(f"{symbol}: receipt mismatch")
        if (item["venue"] != "XNYS" or item["currency"] != "USD" or
            item["provider"] != "Yahoo Finance chart v8" or item["provider_symbol"] != symbol):
            raise ValueError(f"{symbol}: manifest provider/market identity mismatch")
        path = target / f"{symbol}_1d.parquet"
        if inspect_bars(path, symbol) != item["quality"]:
            raise ValueError(f"{symbol}: quality summary mismatch")
        with duckdb.connect(":memory:") as con:
            actual = con.execute("SELECT * FROM read_parquet(?)", [str(path)]).df()
        pd.testing.assert_frame_equal(actual, expected, check_dtype=False, check_exact=True)
        dividends = expected.loc[expected.dividend != 0, ["session_date", "dividend"]].to_dict("records")
        if item["dividends"] != dividends or item["splits_reported"] != 0:
            raise ValueError(f"{symbol}: action summary mismatch")
    return manifest


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    action = parser.add_mutually_exclusive_group()
    action.add_argument("--download", action="store_true", help="network required; refuses existing downloads")
    action.add_argument("--freeze", action="store_true", help="create from downloads; refuses existing target")
    parser.add_argument("--downloads", type=Path, default=DEFAULT_DOWNLOADS)
    parser.add_argument("--target", type=Path, default=DEFAULT_TARGET)
    args = parser.parse_args(argv)
    try:
        if args.download:
            download(args.downloads)
            print(f"Downloaded {len(SYMBOLS)} verified source responses to {args.downloads}")
        else:
            manifest = freeze(args.downloads, args.target) if args.freeze else verify(args.target)
            print(f"Verified {manifest['dataset_id']}: {len(SYMBOLS)} equities, {len(sessions())} sessions each.")
    except (OSError, ValueError, KeyError, TypeError, AssertionError, duckdb.Error) as exc:
        parser.exit(1, f"Equity dataset verification failed: {exc}\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
