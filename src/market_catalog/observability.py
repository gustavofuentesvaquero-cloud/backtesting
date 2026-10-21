"""Out-of-band health snapshots, alerts, and structured log formatting."""

from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass, field
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
import json
import logging
import re
from pathlib import Path
from types import MappingProxyType
from typing import Any, Mapping

from .event_engine import SessionReport
from .pipeline import MarketDataPipeline


OBSERVABILITY_SCHEMA_VERSION = "1.0.0"
_SECRET_KEY_PARTS = ("secret", "password", "token", "api_key", "apikey", "credential", "authorization")
_SECRET_VALUE_PATTERNS = (
    re.compile(r"(?i)(\b(?:authorization\s*[:=]\s*bearer)\s+)[^\s,;]+"),
    re.compile(r"(?i)(\b(?:api[_-]?key|access[_-]?token|password|secret)\s*[:=]\s*)[^\s,;]+"),
)


def _redact(value: Any, key: str = "") -> Any:
    key_lower = key.lower()
    if any(part in key_lower for part in _SECRET_KEY_PARTS):
        return "[REDACTED]"
    if isinstance(value, Mapping):
        return {str(child_key): _redact(child_value, str(child_key)) for child_key, child_value in value.items()}
    if isinstance(value, (list, tuple)):
        return [_redact(item) for item in value]
    if isinstance(value, str):
        result = value
        for pattern in _SECRET_VALUE_PATTERNS:
            result = pattern.sub(r"\1[REDACTED]", result)
        return result
    if isinstance(value, Decimal):
        return format(value, "f")
    if value is None or isinstance(value, (bool, int, float)):
        return value
    return str(value)


_STANDARD_LOG_RECORD_ATTRS = {
    "name", "msg", "args", "levelname", "levelno", "pathname", "filename", "module",
    "exc_info", "exc_text", "stack_info", "lineno", "funcName", "created", "msecs",
    "relativeCreated", "thread", "threadName", "processName", "process", "message", "taskName",
}


class JsonLogFormatter(logging.Formatter):
    """Emit one structured record and redact common credential fields/values."""

    def format(self, record: logging.LogRecord) -> str:
        payload: dict[str, Any] = {
            "timestamp": datetime.fromtimestamp(record.created, timezone.utc).isoformat(),
            "severity": record.levelname.lower(),
            "logger": record.name,
            "message": record.getMessage(),
        }
        for key, value in vars(record).items():
            if not key.startswith("_") and key not in _STANDARD_LOG_RECORD_ATTRS and key != "context":
                payload[key] = value
        context = getattr(record, "context", None)
        if isinstance(context, Mapping):
            payload.update(context)
        if "instrument_id" not in payload and "instrument" in payload:
            payload["instrument_id"] = payload.pop("instrument")
        if record.exc_info:
            payload["exception_type"] = record.exc_info[0].__name__ if record.exc_info[0] else "Exception"
        return json.dumps(_redact(payload), separators=(",", ":"), sort_keys=True, allow_nan=False)


@dataclass(frozen=True)
class Alert:
    code: str
    severity: str
    message: str
    attributes: Mapping[str, Any]

    def to_mapping(self) -> dict[str, Any]:
        return {
            "code": self.code,
            "severity": self.severity,
            "message": self.message,
            "attributes": _json_mapping(self.attributes),
        }


@dataclass(frozen=True)
class AlertPolicy:
    """Operational thresholds; unset time/spread limits make no SLA claim."""

    version: str = "1.0.0"
    max_receive_silence_ns: int | None = None
    queue_utilization_threshold: Decimal = Decimal("0.90")
    quality_count_threshold: int = 1
    recording_required: bool = False
    max_spread_by_instrument: Mapping[str, Decimal] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if not isinstance(self.version, str) or not self.version:
            raise ValueError("version must be a non-empty string.")
        if self.max_receive_silence_ns is not None and (
            isinstance(self.max_receive_silence_ns, bool)
            or not isinstance(self.max_receive_silence_ns, int)
            or self.max_receive_silence_ns <= 0
        ):
            raise ValueError("max_receive_silence_ns must be positive or null.")
        threshold = self.queue_utilization_threshold
        if not isinstance(threshold, Decimal) or not threshold.is_finite() or not 0 < threshold <= 1:
            raise ValueError("queue_utilization_threshold must be a Decimal in (0, 1].")
        if isinstance(self.quality_count_threshold, bool) or not isinstance(self.quality_count_threshold, int) or self.quality_count_threshold <= 0:
            raise ValueError("quality_count_threshold must be a positive integer.")
        if not isinstance(self.recording_required, bool):
            raise ValueError("recording_required must be boolean.")
        spreads = dict(self.max_spread_by_instrument or {})
        for instrument_id, maximum in spreads.items():
            if not isinstance(instrument_id, str) or not instrument_id.strip():
                raise ValueError("spread threshold instrument ids must be non-empty strings.")
            if not isinstance(maximum, Decimal) or not maximum.is_finite() or maximum <= 0:
                raise ValueError(f"spread threshold for {instrument_id} must be a positive Decimal.")
        object.__setattr__(self, "max_spread_by_instrument", MappingProxyType(spreads))

    @classmethod
    def from_mapping(cls, value: Mapping[str, Any]) -> "AlertPolicy":
        expected = {
            "version", "max_receive_silence_ns", "queue_utilization_threshold",
            "quality_count_threshold", "recording_required", "max_spread_by_instrument",
        }
        if not isinstance(value, Mapping) or set(value) != expected:
            raise ValueError("Alert policy has missing or unknown fields.")
        try:
            threshold = Decimal(value["queue_utilization_threshold"])
            spreads = {
                key: Decimal(amount)
                for key, amount in value["max_spread_by_instrument"].items()
            }
        except (InvalidOperation, AttributeError, TypeError) as exc:
            raise ValueError("Alert policy decimal values must be decimal strings.") from exc
        return cls(
            version=value["version"],
            max_receive_silence_ns=value["max_receive_silence_ns"],
            queue_utilization_threshold=threshold,
            quality_count_threshold=value["quality_count_threshold"],
            recording_required=value["recording_required"],
            max_spread_by_instrument=spreads,
        )

    @classmethod
    def load(cls, path: str | Path) -> "AlertPolicy":
        try:
            value = json.loads(Path(path).read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise ValueError(f"Cannot load alert policy {path}: {exc}") from exc
        return cls.from_mapping(value)


@dataclass(frozen=True)
class ObservabilitySnapshot:
    schema_version: str
    session_id: str
    session_status: str
    session_valid: bool
    generated_monotonic_ns: int
    feeds: tuple[Mapping[str, Any], ...]
    quality_counters: Mapping[str, int]
    queues: Mapping[str, Mapping[str, Any]]
    stage_latency: Mapping[str, Mapping[str, int | None]]
    portfolio: Mapping[str, Any]
    recording: Mapping[str, Any] | None
    metrics: Mapping[str, int | float]
    alerts: tuple[Alert, ...]

    def to_mapping(self) -> dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "session": {
                "session_id": self.session_id,
                "status": self.session_status,
                "valid": self.session_valid,
                "generated_monotonic_ns": self.generated_monotonic_ns,
            },
            "feeds": [_json_mapping(item) for item in self.feeds],
            "quality_counters": dict(self.quality_counters),
            "queues": {name: _json_mapping(value) for name, value in self.queues.items()},
            "stage_latency": {name: dict(value) for name, value in self.stage_latency.items()},
            "portfolio": _json_mapping(self.portfolio),
            "recording": None if self.recording is None else _json_mapping(self.recording),
            "metrics": dict(self.metrics),
            "alerts": [alert.to_mapping() for alert in self.alerts],
        }


def _json_mapping(value: Mapping[str, Any]) -> dict[str, Any]:
    return {str(key): _redact(item, str(key)) for key, item in value.items()}


def log_alerts(snapshot: ObservabilitySnapshot, logger: logging.Logger) -> None:
    """Emit session alerts as structured incident records, outside event workers."""

    for alert in snapshot.alerts:
        context = {
            "session_id": snapshot.session_id,
            "event_code": alert.code,
            **dict(alert.attributes),
        }
        level = logging.CRITICAL if alert.severity == "critical" else logging.WARNING
        logger.log(level, alert.message, extra={"context": context})


def _histogram_mapping(histogram: Any) -> dict[str, int | None]:
    return {
        name: getattr(histogram, name)
        for name in ("count", "p50_upper_ns", "p90_upper_ns", "p99_upper_ns", "p999_upper_ns", "max_ns")
    }


def _quality_metric_name(code: str) -> str:
    normalized = code.replace("-", "_")
    return f"market.feed.quality.{normalized}"


def build_observability_snapshot(
    report: SessionReport,
    pipeline: MarketDataPipeline,
    *,
    now_monotonic_ns: int,
    policy: AlertPolicy | None = None,
    replay_matched: bool | None = None,
    spread_by_instrument: Mapping[str, Decimal] | None = None,
) -> ObservabilitySnapshot:
    """Build a point-in-time telemetry view outside the event hot path."""

    policy = policy or AlertPolicy()
    if isinstance(now_monotonic_ns, bool) or not isinstance(now_monotonic_ns, int) or now_monotonic_ns < 0:
        raise ValueError("now_monotonic_ns must be a non-negative integer.")

    adapter_map = dict(pipeline.adapters_by_provider)
    primary_id = getattr(pipeline.adapter, "provider_id", None)
    if not isinstance(primary_id, str) or not primary_id:
        primary_id = getattr(pipeline.adapter, "_provider_id", "primary")
    adapter_map.setdefault(primary_id, pipeline.adapter)
    stream_health = pipeline.quality_health(now_monotonic_ns)
    by_provider: dict[str, list[Any]] = defaultdict(list)
    for stream in stream_health:
        by_provider[stream.provider_id].append(stream)
    provider_ids = sorted(set(adapter_map) | set(by_provider))
    feeds: list[Mapping[str, Any]] = []
    alerts: list[Alert] = []
    metrics: dict[str, int | float] = {}
    for provider_id in provider_ids:
        adapter = adapter_map.get(provider_id)
        adapter_health = adapter.health() if adapter is not None else None
        streams = by_provider.get(provider_id, [])
        receive_times = [item.last_receive_monotonic_ns for item in streams if item.last_receive_monotonic_ns]
        if adapter_health is not None and adapter_health.last_received_monotonic_ns is not None:
            receive_times.append(adapter_health.last_received_monotonic_ns)
        last_received = max(receive_times) if receive_times else None
        age_ns = None if last_received is None else max(0, now_monotonic_ns - last_received)
        connected = bool(adapter_health and adapter_health.connected)
        feed_status = "unknown" if adapter_health is None else adapter_health.status
        awaiting_snapshot = sum(item.book_state == "awaiting_snapshot" for item in streams)
        stale_streams = sum(item.stale is True for item in streams)
        unusable_streams = sum(not item.decision_usable for item in streams)
        received = 0 if adapter_health is None else adapter_health.received_frames
        feeds.append(MappingProxyType({
            "provider_id": provider_id,
            "connection_status": feed_status,
            "connected": connected,
            "detail": "" if adapter_health is None else adapter_health.detail,
            "received_frames": received,
            "last_received_monotonic_ns": last_received,
            "last_receive_age_ns": age_ns,
            "stream_count": len(streams),
            "stale_streams": stale_streams,
            "unusable_streams": unusable_streams,
            "books_awaiting_snapshot": awaiting_snapshot,
        }))
        metrics[f"market.feed.connected.{provider_id}"] = 1 if connected else 0
        metrics[f"market.feed.frames_received.{provider_id}"] = received
        if age_ns is not None:
            metrics[f"market.feed.last_receive_age_ns.{provider_id}"] = age_ns
        metrics[f"market.feed.streams_stale.{provider_id}"] = stale_streams
        metrics[f"market.feed.books_awaiting_snapshot.{provider_id}"] = awaiting_snapshot
        if not connected:
            alerts.append(Alert("feed_disconnected", "critical", "Feed adapter is disconnected or has unknown connection state.", MappingProxyType({"provider_id": provider_id, "status": feed_status})))
        if connected and last_received is None:
            alerts.append(Alert("feed_no_data", "warning", "Feed is connected but no frame has been received.", MappingProxyType({"provider_id": provider_id})))
        if policy.max_receive_silence_ns is not None and age_ns is not None and age_ns > policy.max_receive_silence_ns:
            alerts.append(Alert("feed_data_stale", "warning", "Receive silence exceeded the configured threshold.", MappingProxyType({"provider_id": provider_id, "age_ns": age_ns, "threshold_ns": policy.max_receive_silence_ns})))
        if stale_streams:
            alerts.append(Alert("feed_stale_stream", "warning", "One or more instrument streams are marked stale by quality policy.", MappingProxyType({"provider_id": provider_id, "count": stale_streams})))
        if awaiting_snapshot:
            alerts.append(Alert("book_awaiting_snapshot", "critical", "Book decisions remain blocked until a consistent quote or snapshot arrives.", MappingProxyType({"provider_id": provider_id, "count": awaiting_snapshot})))

    quality_counts = dict(pipeline.quality.diagnostic_counts())
    total_gaps = quality_counts.get("sequence_gap", 0)
    duplicates = quality_counts.get("sequence_duplicate", 0) + quality_counts.get("duplicate_payload", 0)
    stale_quality = quality_counts.get("stale_source_timestamp", 0) + quality_counts.get("receive_silence_exceeded", 0)
    metrics["market.feed.sequence_gaps"] = total_gaps
    metrics["market.feed.duplicates"] = duplicates
    metrics["market.feed.stale_events"] = stale_quality
    for code, count in quality_counts.items():
        metrics[_quality_metric_name(code)] = count
    if total_gaps >= policy.quality_count_threshold:
        alerts.append(Alert("feed_sequence_gap", "warning", "Sequence gaps reached the configured alert threshold.", MappingProxyType({"count": total_gaps})))
    if duplicates >= policy.quality_count_threshold:
        alerts.append(Alert("feed_duplicate", "warning", "Duplicate events reached the configured alert threshold.", MappingProxyType({"count": duplicates})))

    queue_view: dict[str, Mapping[str, Any]] = {}
    for name, queue in report.metrics.queues.items():
        utilization = Decimal(queue.pending) / Decimal(queue.capacity)
        queue_view[name] = MappingProxyType({
            "capacity": queue.capacity,
            "pending": queue.pending,
            "high_watermark": queue.high_watermark,
            "rejected": queue.rejected,
            "utilization": format(utilization, "f"),
            "wait": _histogram_mapping(queue.wait),
        })
        metrics[f"market.engine.queue.pending.{name}"] = queue.pending
        metrics[f"market.engine.queue.capacity.{name}"] = queue.capacity
        metrics[f"market.engine.queue.rejected.{name}"] = queue.rejected
        metrics[f"market.engine.queue.utilization.{name}"] = float(utilization)
        if queue.rejected or utilization >= policy.queue_utilization_threshold:
            alerts.append(Alert("engine_queue_saturated", "critical" if queue.rejected else "warning", "Queue rejection or configured occupancy threshold was reached.", MappingProxyType({"queue": name, "pending": queue.pending, "capacity": queue.capacity, "rejected": queue.rejected, "threshold": format(policy.queue_utilization_threshold, "f")})))

    stage_latency = {name: MappingProxyType(_histogram_mapping(histogram)) for name, histogram in report.metrics.stages.items()}
    stage_latency["strategy_callback"] = MappingProxyType(_histogram_mapping(report.metrics.strategy_callback))
    stage_latency["end_to_end"] = MappingProxyType(_histogram_mapping(report.metrics.end_to_end))
    for stage, histogram in report.metrics.stages.items():
        metrics[f"market.engine.stage.duration_ns.count.{stage}"] = histogram.count
        metrics[f"market.engine.stage.duration_ns.p99.{stage}"] = histogram.p99_upper_ns or 0
    metrics["market.engine.events.processed"] = report.processed_events
    metrics["market.engine.fills"] = len(report.fills)
    metrics["market.engine.orders"] = len(report.orders)

    portfolio = report.portfolio
    portfolio_view: dict[str, Any] = {
        "cash_balances": {key: format(value, "f") for key, value in portfolio.cash_balances.items()},
        "positions": {key: format(value, "f") for key, value in portfolio.positions.items()},
        "realized_pnl": {key: format(value, "f") for key, value in portfolio.realized_pnl.items()},
        "unrealized_pnl": {key: format(value, "f") for key, value in portfolio.unrealized_pnl.items()},
        "commissions_paid": {key: format(value, "f") for key, value in portfolio.commissions_paid.items()},
        "version": portfolio.version,
        "as_of_ordinal": portfolio.as_of_ordinal,
    }
    metrics["market.portfolio.version"] = portfolio.version
    metrics["market.portfolio.ledger_entries"] = portfolio.ledger_entry_count
    for currency, amount in portfolio.realized_pnl.items():
        metrics[f"market.portfolio.realized_pnl.{currency}"] = float(amount)
    for currency, amount in portfolio.unrealized_pnl.items():
        metrics[f"market.portfolio.unrealized_pnl.{currency}"] = float(amount)

    recording = None
    if report.recording is not None:
        item = report.recording
        recording = MappingProxyType({
            "status": item.status,
            "complete": item.complete,
            "enqueued_records": item.enqueued_records,
            "written_records": item.written_records,
            "dropped_records": item.dropped_records,
            "pending_records": item.pending_records,
            "high_watermark": item.high_watermark,
            "max_queue_wait_ns": item.max_queue_wait_ns,
            "max_write_ns": item.max_write_ns,
            "error": item.error,
        })
        metrics["market.recording.records_written"] = item.written_records
        metrics["market.recording.records_dropped"] = item.dropped_records
        metrics["market.recording.queue.pending"] = item.pending_records
        metrics["market.recording.complete"] = 1 if item.complete else 0
        if not item.complete or item.dropped_records:
            alerts.append(Alert("recording_incomplete", "critical", "Session recording failed or is incomplete; it must not be presented as a complete replay.", MappingProxyType({"status": item.status, "dropped_records": item.dropped_records, "error": item.error})))
    elif policy.recording_required:
        alerts.append(Alert("recording_missing", "critical", "Recording is required by policy but no recorder is configured.", MappingProxyType({"session_id": report.session_id})))

    if replay_matched is False or any(item.code == "replay_divergence" for item in report.diagnostics):
        alerts.append(Alert("replay_divergence", "critical", "Replay output diverged from the expected recorded result.", MappingProxyType({"session_id": report.session_id})))
    for diagnostic in report.diagnostics:
        if diagnostic.code in {"queue_overflow", "recording_queue_overflow", "recording_writer_failed"}:
            alerts.append(Alert(diagnostic.code, "critical", diagnostic.message, MappingProxyType({"stage": diagnostic.stage, "count": diagnostic.count})))

    for instrument_id, spread in (spread_by_instrument or {}).items():
        if not isinstance(spread, Decimal) or not spread.is_finite() or spread < 0:
            raise ValueError(f"spread for {instrument_id} must be a non-negative Decimal.")
        maximum = policy.max_spread_by_instrument.get(instrument_id)
        if maximum is not None and spread > maximum:
            alerts.append(Alert("spread_anomalous", "warning", "Observed spread exceeded its configured instrument threshold.", MappingProxyType({"instrument_id": instrument_id, "spread": format(spread, "f"), "threshold": format(maximum, "f")})))

    return ObservabilitySnapshot(
        schema_version=OBSERVABILITY_SCHEMA_VERSION,
        session_id=report.session_id,
        session_status=report.status,
        session_valid=report.valid,
        generated_monotonic_ns=now_monotonic_ns,
        feeds=tuple(feeds),
        quality_counters=MappingProxyType(quality_counts),
        queues=MappingProxyType(queue_view),
        stage_latency=MappingProxyType(stage_latency),
        portfolio=MappingProxyType(portfolio_view),
        recording=recording,
        metrics=MappingProxyType(metrics),
        alerts=tuple(alerts),
    )

