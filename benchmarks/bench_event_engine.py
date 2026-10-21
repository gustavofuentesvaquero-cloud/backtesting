"""Reproducible synthetic event-path benchmark; it is not an HFT claim.

The benchmark reports internal monotonic-clock measurements separately from
source timestamps. All generated prices, symbols, and timestamps are synthetic.
"""

from __future__ import annotations

import argparse
import ctypes
import gc
import json
import os
import platform
from pathlib import Path
import sys
import tempfile
import tracemalloc
from time import monotonic_ns, process_time_ns

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from market_catalog import (  # noqa: E402
    Catalog,
    DataQualityMonitor,
    EventProcessor,
    EventProcessorConfig,
    MarketDataPipeline,
    RawFrame,
    SessionRecorder,
)
from market_catalog.normalization import JsonFixtureNormalizer  # noqa: E402


class _NoopAdapter:
    def request_snapshot(self, instrument_id: str, channel: str) -> bool:
        return False


class _NoopStrategy:
    def on_event(self, event, book):
        return None


def _catalog() -> Catalog:
    return Catalog.from_mapping(
        {
            "schema_version": "1.0.0",
            "catalog_version": "1.0.0",
            "instruments": [
                {
                    "id": "SYNTH",
                    "asset_class": "equity",
                    "venue": "SYNTHETIC",
                    "provider_symbols": [{"provider_id": "BENCH", "symbol": "SYNTH"}],
                    "valid_from": "2030-01-01",
                    "valid_to": None,
                    "calendar_id": "BENCH-CALENDAR",
                    "price_tick": "0.01",
                    "price_precision": 2,
                    "quantity_unit": "share",
                    "quantity_precision": 0,
                    "minimum_quantity": "1",
                    "quantity_increment": "1",
                    "quote_currency": "USD",
                    "valuation_model": "equity_shares",
                    "provenance": {"status": "synthetic", "sources": [], "notes": "Benchmark only."},
                    "historical_price_policy": {"price_basis": "raw", "corporate_action_events": []},
                }
            ],
            "calendars": [],
            "continuous_futures": [],
        }
    )


def _frame(index: int) -> RawFrame:
    # These timestamps are deliberately synthetic and are not used to estimate
    # source-to-receive latency.
    source_ns = 1_800_000_000_000_000_000 + index
    payload = {
        "event_type": "quote",
        "symbol": "SYNTH",
        "sequence": index + 1,
        "sequence_scope": "instrument_channel",
        "source_timestamp": {
            "raw": str(source_ns),
            "unix_ns": source_ns,
            "semantics": "event_time",
        },
        "payload": {
            "bid": {"price": "99.99", "quantity": "10"},
            "ask": {"price": "100.00", "quantity": "10"},
        },
    }
    return RawFrame(
        provider_id="BENCH",
        channel="L1",
        receive_time_utc_ns=source_ns + 1_000,
        receive_time_monotonic_ns=1_000 + index,
        arrival_ordinal=index,
        raw_payload=json.dumps(payload, separators=(",", ":")).encode("utf-8"),
    )


def _histogram(value) -> dict[str, int | None]:
    return {
        "count": value.count,
        "mean_ns": value.mean_ns,
        "stddev_ns": value.stddev_ns,
        "p50_upper_ns": value.p50_upper_ns,
        "p90_upper_ns": value.p90_upper_ns,
        "p95_upper_ns": value.p95_upper_ns,
        "p99_upper_ns": value.p99_upper_ns,
        "p999_upper_ns": value.p999_upper_ns,
        "max_ns": value.max_ns,
    }


def _gc_delta(before: list[dict], after: list[dict]) -> dict[str, list[int]]:
    return {
        "collections": [right["collections"] - left["collections"] for left, right in zip(before, after)],
        "collected": [right["collected"] - left["collected"] for left, right in zip(before, after)],
        "uncollectable": [right["uncollectable"] - left["uncollectable"] for left, right in zip(before, after)],
    }


def _process_memory() -> dict[str, int | None]:
    """Best-effort process RSS; Python heap is measured separately by tracemalloc."""

    if os.name == "nt":
        try:
            class _ProcessMemoryCountersEx(ctypes.Structure):
                _fields_ = [
                    ("cb", ctypes.c_uint32),
                    ("PageFaultCount", ctypes.c_uint32),
                    ("PeakWorkingSetSize", ctypes.c_size_t),
                    ("WorkingSetSize", ctypes.c_size_t),
                    ("QuotaPeakPagedPoolUsage", ctypes.c_size_t),
                    ("QuotaPagedPoolUsage", ctypes.c_size_t),
                    ("QuotaPeakNonPagedPoolUsage", ctypes.c_size_t),
                    ("QuotaNonPagedPoolUsage", ctypes.c_size_t),
                    ("PagefileUsage", ctypes.c_size_t),
                    ("PeakPagefileUsage", ctypes.c_size_t),
                    ("PrivateUsage", ctypes.c_size_t),
                ]

            counters = _ProcessMemoryCountersEx()
            counters.cb = ctypes.sizeof(counters)
            kernel32 = ctypes.WinDLL("kernel32")
            kernel32.GetCurrentProcess.restype = ctypes.c_void_p
            get_memory_info = ctypes.WinDLL("psapi").GetProcessMemoryInfo
            get_memory_info.argtypes = [
                ctypes.c_void_p,
                ctypes.POINTER(_ProcessMemoryCountersEx),
                ctypes.c_uint32,
            ]
            get_memory_info.restype = ctypes.c_int
            ok = get_memory_info(
                kernel32.GetCurrentProcess(), ctypes.byref(counters), counters.cb
            )
            if ok:
                return {
                    "rss_bytes": int(counters.WorkingSetSize),
                    "peak_rss_bytes": int(counters.PeakWorkingSetSize),
                }
        except (AttributeError, OSError, TypeError, ValueError):
            pass
    try:
        import resource

        peak = int(resource.getrusage(resource.RUSAGE_SELF).ru_maxrss)
        return {"rss_bytes": None, "peak_rss_bytes": peak if sys.platform == "darwin" else peak * 1024}
    except (ImportError, AttributeError, OSError, ValueError):
        return {"rss_bytes": None, "peak_rss_bytes": None}


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--events", type=int, default=10_000)
    parser.add_argument("--queue-capacity", type=int, help="engine capacity for each stage; defaults to at least --events")
    parser.add_argument("--recording-queue-capacity", type=int, help="bounded recorder capacity; defaults to 16 records per event plus headroom")
    parser.add_argument("--no-recorder", action="store_true", help="omit the recorder measurement")
    args = parser.parse_args()
    if args.events <= 0:
        parser.error("--events must be positive")
    if args.queue_capacity is not None and args.queue_capacity <= 0:
        parser.error("--queue-capacity must be positive")
    if args.recording_queue_capacity is not None and args.recording_queue_capacity <= 0:
        parser.error("--recording-queue-capacity must be positive")

    engine_capacity = args.queue_capacity or max(64, args.events)
    recorder_capacity = args.recording_queue_capacity or max(4096, args.events * 16)
    catalog = _catalog()
    def run(recording_path: Path | None, *, trace_allocations: bool) -> dict:
        recorder = None if recording_path is None else SessionRecorder(
            recording_path,
            recording_mode="historical_replay",
            clock_policy="event_time",
            queue_capacity=recorder_capacity,
            segment_max_records=25_000,
        )
        processor = EventProcessor(
            MarketDataPipeline(
                _NoopAdapter(),
                JsonFixtureNormalizer(catalog, preserve_raw=False),
                DataQualityMonitor(),
            ),
            catalog,
            _NoopStrategy(),
            EventProcessorConfig(queue_capacity=engine_capacity),
            recorder=recorder,
            session_id="phase8-synthetic-benchmark",
        )
        if trace_allocations:
            tracemalloc.start()
            allocation_start = tracemalloc.take_snapshot()
        gc_before = gc.get_stats()
        gc_counts_before = gc.get_count()
        allocated_blocks_before = getattr(sys, "getallocatedblocks", lambda: None)()
        processor.start()
        cpu_started = process_time_ns()
        wall_started = monotonic_ns()
        for index in range(args.events):
            if processor.submit(_frame(index)) is None:
                break
        report = processor.finish(timeout=120)
        elapsed_ns = max(1, monotonic_ns() - wall_started)
        cpu_ns = process_time_ns() - cpu_started
        if trace_allocations:
            current_memory, peak_memory = tracemalloc.get_traced_memory()
            allocation_end = tracemalloc.take_snapshot()
            allocation_diff = allocation_end.compare_to(allocation_start, "lineno")
            positive_allocation_count = sum(max(0, stat.count_diff) for stat in allocation_diff)
        else:
            current_memory = peak_memory = positive_allocation_count = None
        allocated_blocks_after = getattr(sys, "getallocatedblocks", lambda: None)()
        gc_after = gc.get_stats()
        gc_counts_after = gc.get_count()
        process_memory = _process_memory()
        if trace_allocations:
            tracemalloc.stop()

        output = {
            "status": report.status,
            "scenario": "synthetic_l1_quotes_noop_strategy",
            "events_requested": args.events,
            "events_received": report.received_frames,
            "events_accepted": report.accepted_frames,
            "events_processed": report.processed_events,
            "events_discarded": report.rejected_frames,
            "elapsed_ns": elapsed_ns,
            "throughput_events_per_second": report.processed_events * 1_000_000_000 / elapsed_ns,
            "cpu_process_time_ns": cpu_ns,
            "python_runtime": platform.python_version(),
            "platform": platform.platform(),
            "internal_latency_clock": "perf_counter_ns monotonic histogram; reported values are approximate log2 bucket upper bounds",
            "source_to_receive_latency": {
                "measured": False,
                "reason": "synthetic source and receive timestamps are not assumed to share a comparable clock or publication semantics",
            },
            "ingress": _histogram(report.metrics.ingress),
            "stages": {name: _histogram(value) for name, value in report.metrics.stages.items()},
            "strategy_callback": _histogram(report.metrics.strategy_callback),
            "end_to_end_receive_to_portfolio": _histogram(report.metrics.end_to_end),
            "queues": {
                name: {
                    "capacity": value.capacity,
                    "accepted": value.enqueued,
                    "rejected": value.rejected,
                    "pending": value.pending,
                    "high_watermark": value.high_watermark,
                    "wait": _histogram(value.wait),
                }
                for name, value in report.metrics.queues.items()
            },
            "memory_and_allocations": {
                "tracemalloc_current_bytes_at_end": current_memory,
                "tracemalloc_peak_bytes": peak_memory,
                "net_positive_allocation_count_by_source_location": positive_allocation_count,
                "allocated_blocks_delta": None if allocated_blocks_before is None else allocated_blocks_after - allocated_blocks_before,
                "gc_count_before": list(gc_counts_before),
                "gc_count_after": list(gc_counts_after),
                "gc_stats_delta": _gc_delta(gc_before, gc_after),
                "rss_bytes": process_memory["rss_bytes"],
                "peak_rss_bytes": process_memory["peak_rss_bytes"],
            },
            "memory_probe_mode": "same run with tracemalloc" if trace_allocations else "untraced throughput run",
            "recording": None if report.recording is None else {
                "status": report.recording.status,
                "queue_capacity": report.recording.queue_capacity,
                "records_enqueued": report.recording.enqueued_records,
                "records_written": report.recording.written_records,
                "records_dropped": report.recording.dropped_records,
                "pending": report.recording.pending_records,
                "high_watermark": report.recording.high_watermark,
                "max_queue_wait_ns": report.recording.max_queue_wait_ns,
                "max_write_service_ns": report.recording.max_write_ns,
                "segments": report.recording.segment_count,
            },
        }
        return output

    if args.no_recorder:
        result = run(None, trace_allocations=False)
        memory_probe = run(None, trace_allocations=True)
    else:
        with tempfile.TemporaryDirectory(prefix="market-catalog-benchmark-") as temp_dir:
            result = run(Path(temp_dir) / "timed-session", trace_allocations=False)
            memory_probe = run(Path(temp_dir) / "traced-session", trace_allocations=True)
    timed_resources = result["memory_and_allocations"]
    result["gc_during_timed_run"] = {
        "gc_count_before": timed_resources["gc_count_before"],
        "gc_count_after": timed_resources["gc_count_after"],
        "gc_stats_delta": timed_resources["gc_stats_delta"],
    }
    result["memory_and_allocations"] = memory_probe["memory_and_allocations"]
    result["memory_probe_mode"] = "separate full run with tracemalloc; latency and throughput are from the untraced run"
    print(json.dumps(result, indent=2, sort_keys=True))
    return 0 if result["status"] == "completed" and result["events_processed"] == args.events and result["events_discarded"] == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
