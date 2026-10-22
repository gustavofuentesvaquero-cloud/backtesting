"""Acquire and audit public individual MGC quotes; never certify execution inputs."""

from __future__ import annotations

import argparse
from datetime import date, datetime, time, timezone
import hashlib
import json
import math
from pathlib import Path
import shutil
from urllib.error import HTTPError
from urllib.parse import urlencode
from urllib.request import Request, urlopen
from zoneinfo import ZoneInfo

from market_catalog.storage import ensure_mutable_data_path
from scripts.data_baseline import validate_freeze_locations

ROOT = Path(__file__).resolve().parents[1]
DEFAULT_TARGET = ROOT / "data/research/mgc-public-2026-10-22"
CONTRACTS = {
    "MGCV26.CMX": ("MGC-2026-10", "Micro Gold Futures,Oct-2026"),
    "MGCZ26.CMX": ("MGC-2026-12", "Micro Gold Futures,Dec-2026"),
    "MGCG27.CMX": ("MGC-2027-02", "Micro Gold Futures,Feb-2027"),
}
PROBES = ("MGCG26.CMX", "MGCM26.CMX")
POLICY = {
    "usage": "quarantined public research evidence; not an execution dataset",
    "price_basis": "provider quote OHLC unchanged; adjustment/settlement semantics unverified",
    "date_basis": "provider daily label at midnight America/New_York; not session open or publication time",
    "missing_sessions": "not assessed without a verified product session calendar; never fill",
    "zero_volume": "retain and flag; does not establish an executable quote",
}


def dump(path: Path, value) -> None:
    path.write_text(json.dumps(value, indent=2, ensure_ascii=False, allow_nan=False) + "\n", encoding="utf-8")


def digest(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def request_url(symbol: str, start: str, end: str) -> str:
    if symbol not in (*CONTRACTS, *PROBES):
        raise ValueError("Unsupported individual contract symbol")
    first, stop = date.fromisoformat(start), date.fromisoformat(end)
    if first >= stop:
        raise ValueError("The exclusive end must follow start")
    epoch = lambda d: int(datetime.combine(d, time(), timezone.utc).timestamp())
    return "https://query1.finance.yahoo.com/v8/finance/chart/" + symbol + "?" + urlencode(
        {"period1": epoch(first), "period2": epoch(stop), "interval": "1d"})


def audit_chart(raw: dict, symbol: str, start: str, end: str) -> dict:
    """Check identity/observed bars only, without inventing expected sessions."""
    if symbol not in CONTRACTS:
        raise ValueError("No reviewed identity for this symbol")
    request_url(symbol, start, end)
    chart = raw["chart"]
    if chart.get("error") or not chart.get("result") or len(chart["result"]) != 1:
        raise ValueError("Provider error or ambiguous result")
    result = chart["result"][0]
    identity = {"symbol": symbol, "shortName": CONTRACTS[symbol][1], "exchangeName": "CMX",
                "instrumentType": "FUTURE", "currency": "USD", "exchangeTimezoneName": "America/New_York"}
    for key, expected in identity.items():
        if result["meta"].get(key) != expected:
            raise ValueError(f"Contract identity mismatch: {key}")
    timestamps = result["timestamp"]
    quotes = result["indicators"]["quote"]
    if len(quotes) != 1 or not timestamps:
        raise ValueError("Empty or ambiguous quotes")
    quote = quotes[0]
    columns = ("open", "high", "low", "close", "volume")
    if any(len(quote[k]) != len(timestamps) for k in columns):
        raise ValueError("Inconsistent OHLCV lengths")
    days, zero_volume = [], []
    previous = None
    for i, timestamp in enumerate(timestamps):
        if type(timestamp) is not int or (previous is not None and timestamp <= previous):
            raise ValueError("Timestamps must be unique, ordered integer seconds")
        previous = timestamp
        local = datetime.fromtimestamp(timestamp, ZoneInfo("America/New_York"))
        day = local.date().isoformat()
        if local.time() != time() or not start <= day < end or day in days:
            raise ValueError("Unexpected daily label or date range")
        days.append(day)
        values = [quote[k][i] for k in columns]
        if any(isinstance(v, bool) or not isinstance(v, (int, float)) or not math.isfinite(v) for v in values):
            raise ValueError(f"Non-finite/non-numeric OHLCV at {day}")
        o, h, low, close, volume = values
        if min(o, h, low, close) <= 0 or not low <= min(o, close) <= max(o, close) <= h:
            raise ValueError(f"Incoherent positive OHLC at {day}")
        if volume < 0 or int(volume) != volume:
            raise ValueError(f"Invalid contract volume at {day}")
        if volume == 0:
            zero_volume.append(day)
    return {"instrument_id": CONTRACTS[symbol][0], "provider_symbol": symbol, "identity": identity,
            "rows": len(days), "first_label": days[0], "last_label": days[-1],
            "observed_labels": days, "zero_volume_labels": zero_volume,
            "structural_checks_passed": True, "expected_sessions_verified": False,
            "settlement_semantics_verified": False, "execution_eligible": False}


def download(target: Path, start: str, end: str) -> None:
    ensure_mutable_data_path(target)
    if target.exists():
        raise ValueError("Download target must be new; preserve previous acquisitions")
    for symbol in (*CONTRACTS, *PROBES):
        request_url(symbol, start, end)
    target.mkdir(parents=True)
    for symbol in (*CONTRACTS, *PROBES):
        url = request_url(symbol, start, end)
        try:
            with urlopen(Request(url, headers={"User-Agent": "Mozilla/5.0"}), timeout=40) as response:
                raw, status = response.read(), response.status
        except HTTPError as error:
            raw, status = error.read(), error.code
        path = target / f"{symbol}.json"
        path.write_bytes(raw)
        dump(target / f"{symbol}.receipt.json", {"provider": "Yahoo Finance public chart", "symbol": symbol,
             "request_url": url, "requested_start": start, "requested_end_exclusive": end,
             "downloaded_at_utc": datetime.now(timezone.utc).isoformat(), "http_status": status,
             "sha256": digest(path)})


def audit_acquisition(target: Path) -> dict:
    reports, failures = {}, {}
    ranges = set()
    for symbol in (*CONTRACTS, *PROBES):
        raw_path = target / f"{symbol}.json"
        receipt = json.loads((target / f"{symbol}.receipt.json").read_text(encoding="utf-8"))
        start, end = receipt["requested_start"], receipt["requested_end_exclusive"]
        ranges.add((start, end))
        if receipt["symbol"] != symbol or receipt["request_url"] != request_url(symbol, start, end):
            raise ValueError("Receipt identity/request mismatch")
        instant = datetime.fromisoformat(receipt["downloaded_at_utc"])
        if instant.utcoffset() != timezone.utc.utcoffset(instant) or receipt["sha256"] != digest(raw_path):
            raise ValueError("Receipt timestamp or source checksum mismatch")
        if receipt["http_status"] == 200 and symbol in CONTRACTS:
            reports[symbol] = audit_chart(json.loads(raw_path.read_bytes()), symbol, start, end)
        else:
            failures[symbol] = {"http_status": receipt["http_status"], "availability": "not an accepted contract history"}
    if set(reports) != set(CONTRACTS):
        raise ValueError("All three reviewed individual contract histories are required")
    if len(ranges) != 1:
        raise ValueError("Contract acquisitions must use the same requested date range")
    common = sorted(set.intersection(*(set(r["observed_labels"]) for r in reports.values())))
    return {"format_version": 1, "policy": POLICY, "contracts": reports, "availability_probes": failures,
            "common_observed_labels": common, "execution_eligible": False,
            "unresolved": ["CME session calendar and exceptions", "first intent and first notice per contract",
                           "broker cutoff", "provider bar boundaries and settlement semantics", "validated roll executions"]}


def freeze(source: Path, target: Path) -> dict:
    validate_freeze_locations(source, target)
    # This command is deliberately restricted to research storage, outside execution datasets.
    if target.resolve().is_relative_to((ROOT / "data/frozen").resolve()):
        raise ValueError("Unverified futures evidence belongs outside accepted execution datasets")
    report = audit_acquisition(source)
    target.mkdir(parents=True)
    for symbol in (*CONTRACTS, *PROBES):
        for name in (f"{symbol}.json", f"{symbol}.receipt.json"):
            shutil.copyfile(source / name, target / name)
    dump(target / "audit.json", report)
    files = {p.name: digest(p) for p in sorted(target.iterdir())}
    dump(target / "manifest.json", {"format_version": 1, "usage": POLICY["usage"], "files": files})
    return verify(target)


def verify(target: Path = DEFAULT_TARGET) -> dict:
    manifest = json.loads((target / "manifest.json").read_text(encoding="utf-8"))
    expected = {f"{s}{suffix}" for s in (*CONTRACTS, *PROBES) for suffix in (".json", ".receipt.json")} | {"audit.json"}
    if manifest.get("format_version") != 1 or manifest.get("usage") != POLICY["usage"] or set(manifest["files"]) != expected:
        raise ValueError("Unexpected research manifest")
    if {p.name for p in target.iterdir()} != expected | {"manifest.json"}:
        raise ValueError("Unexpected research evidence files")
    for name, sha in manifest["files"].items():
        if digest(target / name) != sha:
            raise ValueError(f"Research evidence changed: {name}")
    report = audit_acquisition(target)
    if report != json.loads((target / "audit.json").read_text(encoding="utf-8")):
        raise ValueError("Research audit differs from reconstructed source evidence")
    return report


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    acquire = commands.add_parser("download")
    acquire.add_argument("target", type=Path)
    acquire.add_argument("--start", default="2026-01-01")
    acquire.add_argument("--end-exclusive", default="2026-10-21")
    capture = commands.add_parser("freeze")
    capture.add_argument("source", type=Path)
    capture.add_argument("target", type=Path)
    check = commands.add_parser("verify")
    check.add_argument("target", type=Path, nargs="?", default=DEFAULT_TARGET)
    args = parser.parse_args()
    if args.command == "download":
        download(args.target, args.start, args.end_exclusive)
        report = audit_acquisition(args.target)
    elif args.command == "freeze":
        report = freeze(args.source, args.target)
    else:
        report = verify(args.target)
    print(json.dumps({"contracts": {s: r["rows"] for s, r in report["contracts"].items()},
                      "common_observed_labels": len(report["common_observed_labels"]),
                      "execution_eligible": report["execution_eligible"]}, indent=2))


if __name__ == "__main__":
    main()
