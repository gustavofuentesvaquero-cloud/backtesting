"""Versioned, checksummed session recording and deterministic replay helpers.

Filesystem writes run on a dedicated bounded-queue worker. Records crossing the
queue are immutable domain objects where possible; JSON encoding and checksums
are computed by the writer thread.
"""

from __future__ import annotations

import base64
from dataclasses import dataclass, fields, is_dataclass
from datetime import datetime, timezone
from decimal import Decimal
import hashlib
from importlib.metadata import PackageNotFoundError, version as package_version
import json
import math
import os
from pathlib import Path
import platform
from queue import Empty, Full, Queue
import threading
from time import monotonic_ns
from typing import Any, Callable, Mapping
import sys

from jsonschema import Draft202012Validator

from .events import EVENT_SCHEMA_VERSION, DataQualityDiagnostic, MarketEvent, RawFrame
from .models import Catalog
from .simulation import LEDGER_SCHEMA_VERSION


SESSION_FORMAT_VERSION = "1.0.0"
SESSION_RECORD_SCHEMA_VERSION = "1.0.0"
ENGINE_VERSION = "0.1.0"
_WRITER_STOP = object()
_COMPARABLE_KINDS = {
    "normalized_event",
    "normalization_rejected",
    "validated_event",
    "book_state",
    "strategy_decision",
    "intent_validation",
    "order_state",
    "cancellation_state",
    "fill",
    "ledger_entry",
    "session_diagnostic",
    "strategy_run",
}
_NONDETERMINISTIC_DIAGNOSTICS = {
    "queue_wait_budget_exceeded",
    "stage_latency_budget_exceeded",
    "strategy_callback_budget_exceeded",
    "queue_overflow",
    "recording_queue_overflow",
    "recording_writer_failed",
    "shutdown_timeout",
}


class RecordingError(ValueError):
    """The session recording is corrupt, incomplete, or incompatible."""


class ReplayCompatibilityError(RecordingError):
    """The supplied processor does not match the recorded simulation inputs."""


@dataclass(frozen=True)
class RecordingSummary:
    status: str
    path: str
    queue_capacity: int
    enqueued_records: int
    written_records: int
    dropped_records: int
    pending_records: int
    high_watermark: int
    max_queue_wait_ns: int
    max_write_ns: int
    segment_count: int
    error: str | None = None

    @property
    def complete(self) -> bool:
        return self.status == "complete"


@dataclass(frozen=True)
class ReplayDivergence:
    arrival_ordinal: int | None
    kind: str
    expected: Mapping[str, Any] | None
    actual: Mapping[str, Any] | None
    detail: str


@dataclass(frozen=True)
class ReplayComparison:
    matched: bool
    expected_records: int
    actual_records: int
    first_divergence: ReplayDivergence | None


@dataclass(frozen=True)
class ReplayResult:
    report: Any
    comparison: ReplayComparison
    clock_mode: str
    virtual_time_ns: int | None


@dataclass(frozen=True)
class _QueuedRecord:
    record_ordinal: int
    kind: str
    arrival_ordinal: int | None
    event_time_ns: int | None
    payload: Any
    enqueued_monotonic_ns: int


def _json_value(value: Any) -> Any:
    """Convert exact domain values to stable JSON without using binary floats."""

    if value is None or isinstance(value, (str, bool, int)):
        return value
    if isinstance(value, Decimal):
        if not value.is_finite():
            raise ValueError("A recorded Decimal must be finite.")
        return format(value, "f")
    if isinstance(value, float):
        if not math.isfinite(value):
            raise ValueError("Non-finite floats cannot be recorded.")
        # Floats are accepted only for non-financial metadata such as timings.
        return value
    if isinstance(value, bytes):
        return {"bytes_b64": base64.b64encode(value).decode("ascii")}
    if isinstance(value, MarketEvent):
        return value.to_mapping()
    if isinstance(value, RawFrame):
        return {
            "provider_id": value.provider_id,
            "channel": value.channel,
            "receive_time_utc_ns": value.receive_time_utc_ns,
            "receive_time_monotonic_ns": value.receive_time_monotonic_ns,
            "arrival_ordinal": value.arrival_ordinal,
            "raw_payload_b64": base64.b64encode(value.raw_payload).decode("ascii"),
        }
    mapping_method = getattr(value, "to_mapping", None)
    if callable(mapping_method):
        return _json_value(mapping_method())
    if is_dataclass(value) and not isinstance(value, type):
        return {item.name: _json_value(getattr(value, item.name)) for item in fields(value)}
    if isinstance(value, Mapping):
        if any(not isinstance(key, str) for key in value):
            raise TypeError("Recorded JSON object keys must be strings.")
        return {key: _json_value(item) for key, item in value.items()}
    if isinstance(value, (tuple, list)):
        return [_json_value(item) for item in value]
    raise TypeError(f"Unsupported recording value {type(value).__name__}.")


def canonical_json(value: Any) -> str:
    return json.dumps(
        _json_value(value),
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
        allow_nan=False,
    )


def _sha256_json(value: Any) -> str:
    return hashlib.sha256(canonical_json(value).encode("utf-8")).hexdigest()


def _schema_validator(name: str) -> Draft202012Validator:
    schema_path = Path(__file__).resolve().parents[2] / "schemas" / name
    if not schema_path.is_file():
        schema_path = Path(sys.prefix) / "share" / "market-catalog" / "schemas" / name
    schema = json.loads(schema_path.read_text(encoding="utf-8"))
    Draft202012Validator.check_schema(schema)
    return Draft202012Validator(schema)


_MANIFEST_VALIDATOR = _schema_validator("session_manifest.schema.json")
_RECORD_VALIDATOR = _schema_validator("session_record.schema.json")


def _engine_code_sha256() -> str:
    source_root = Path(__file__).resolve().parent
    project_root = source_root.parents[1]
    schema_root = project_root / "schemas"
    if not schema_root.is_dir():
        schema_root = Path(sys.prefix) / "share" / "market-catalog" / "schemas"
    candidates = [
        (path.relative_to(project_root).as_posix(), path)
        for path in source_root.rglob("*.py")
    ] + [
        (f"schemas/{path.name}", path)
        for path in schema_root.glob("*.json")
    ]
    digest = hashlib.sha256()
    for relative_name, path in sorted(candidates, key=lambda item: item[0]):
        relative = relative_name.encode("utf-8")
        digest.update(len(relative).to_bytes(4, "big"))
        digest.update(relative)
        content = path.read_bytes()
        digest.update(len(content).to_bytes(8, "big"))
        digest.update(content)
    return digest.hexdigest()


def _runtime_versions() -> dict[str, Any]:
    try:
        jsonschema_version = package_version("jsonschema")
    except PackageNotFoundError:
        jsonschema_version = None
    return {
        "python_implementation": platform.python_implementation(),
        "python_version": platform.python_version(),
        "platform": sys.platform,
        "jsonschema_version": jsonschema_version,
    }


def _strategy_manifest(strategy: Any) -> list[dict[str, Any]]:
    instances = getattr(strategy, "_managed_strategies", ())
    if not instances:
        if isinstance(strategy, (tuple, list)):
            candidates = tuple(strategy)
        elif callable(getattr(strategy, "state_json", None)) and hasattr(strategy, "config"):
            candidates = (strategy,)
        else:
            candidates = ()
        if candidates and all(
            callable(getattr(item, "state_json", None))
            and hasattr(item, "config")
            and hasattr(item, "seed")
            and hasattr(item, "strategy")
            for item in candidates
        ):
            instances = candidates
    if instances:
        return [
            {
                "strategy_id": item.config.strategy_id,
                "instance_id": item.config.instance_id,
                "strategy_version": item.config.strategy_version,
                "api_version": item.config.api_version,
                "seed": item.seed,
                "config": item.config.to_mapping(),
                "initial_state": item.state,
                "initial_state_snapshot": json.loads(item.state_json()),
                "implementation": f"{type(item.strategy).__module__}.{type(item.strategy).__qualname__}",
            }
            for item in instances
        ]
    implementation = getattr(strategy, "__class__", type(strategy))
    return [{
        "strategy_id": f"{implementation.__module__}.{implementation.__qualname__}",
        "instance_id": "legacy",
        "strategy_version": "unversioned",
        "api_version": "legacy-on_event",
        "seed": None,
        "config": {},
        "initial_state": {},
        "implementation": f"{implementation.__module__}.{implementation.__qualname__}",
    }]


def _processor_configuration(processor: Any) -> dict[str, Any]:
    config = processor.config
    return {
        "queue_capacity": config.queue_capacity,
        "queue_capacities": dict(config.queue_capacities),
        "queue_wait_budgets_ns": dict(config.queue_wait_budgets_ns),
        "stage_latency_budgets_ns": dict(config.stage_latency_budgets_ns),
        "matching_policy": config.matching_policy.to_mapping(),
        "portfolio_policy": config.portfolio_policy.to_mapping(),
    }


class SessionRecorder:
    """Asynchronous bounded writer for one session directory."""

    def __init__(
        self,
        path: str | Path,
        *,
        recording_mode: str = "historical_replay",
        clock_policy: str = "event_time",
        queue_capacity: int = 4096,
        max_queue_wait_ns: int | None = None,
        segment_max_records: int = 10_000,
        segment_max_bytes: int = 8_000_000,
        user_code_sha256: str | None = None,
        seed: int | None = None,
    ) -> None:
        if recording_mode not in {"historical_replay", "live_simulation"}:
            raise ValueError("recording_mode must be historical_replay or live_simulation.")
        if clock_policy not in {"event_time", "controlled_step"}:
            raise ValueError("clock_policy must be event_time or controlled_step.")
        for name, value in (("queue_capacity", queue_capacity), ("segment_max_records", segment_max_records), ("segment_max_bytes", segment_max_bytes)):
            if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
                raise ValueError(f"{name} must be a positive integer.")
        if max_queue_wait_ns is not None and (
            isinstance(max_queue_wait_ns, bool)
            or not isinstance(max_queue_wait_ns, int)
            or max_queue_wait_ns <= 0
        ):
            raise ValueError("max_queue_wait_ns must be a positive integer when provided.")
        if seed is not None and (isinstance(seed, bool) or not isinstance(seed, int) or seed < 0):
            raise ValueError("seed must be a non-negative integer when provided.")
        if user_code_sha256 is not None and not _is_sha256(user_code_sha256):
            raise ValueError("user_code_sha256 must be a lowercase SHA-256 digest.")
        self.path = Path(path).expanduser().resolve()
        self.recording_mode = recording_mode
        self.clock_policy = clock_policy
        self.queue_capacity = queue_capacity
        self.max_queue_wait_ns = max_queue_wait_ns
        self.segment_max_records = segment_max_records
        self.segment_max_bytes = segment_max_bytes
        self.user_code_sha256 = user_code_sha256
        self.seed = seed
        self._queue: Queue[Any] = Queue(maxsize=queue_capacity)
        self._lock = threading.Lock()
        self._configured = False
        self._started = False
        self._closing = False
        self._closed = False
        self._failed = False
        self._error: str | None = None
        self._record_sequence = 0
        self._enqueued_records = 0
        self._written_records = 0
        self._dropped_records = 0
        self._high_watermark = 0
        self._max_queue_wait_ns = 0
        self._max_write_ns = 0
        self._segments: list[dict[str, Any]] = []
        self._current_file: Any = None
        self._current_hash: Any = None
        self._current_name: str | None = None
        self._current_count = 0
        self._current_bytes = 0
        self._channels: set[tuple[str, str]] = set()
        self._incidents: list[dict[str, Any]] = []
        self._manifest: dict[str, Any] | None = None
        self._engine_status = "running"
        self._writer = threading.Thread(target=self._write_loop, name="market-session-recorder", daemon=True)

    def configure(self, processor: Any) -> None:
        """Freeze catalog, runtime, strategy and policy metadata before event intake."""

        with self._lock:
            if self._configured:
                return
            if self.path.exists() and any(self.path.iterdir()):
                raise FileExistsError(f"Recording directory already contains files: {self.path}")
            self.path.mkdir(parents=True, exist_ok=True)
            catalog = processor.catalog.to_mapping()
            execution_config = _processor_configuration(processor)
            strategies = _strategy_manifest(processor.strategy)
            configuration = {
                "event_processor": execution_config,
                "strategies": strategies,
                "recording": {
                    "mode": self.recording_mode,
                    "clock_policy": self.clock_policy,
                    "seed": self.seed,
                    "queue_capacity": self.queue_capacity,
                    "max_queue_wait_ns": self.max_queue_wait_ns,
                    "segment_max_records": self.segment_max_records,
                    "segment_max_bytes": self.segment_max_bytes,
                },
            }
            self._manifest = {
                "manifest_schema_version": SESSION_FORMAT_VERSION,
                "record_schema_version": SESSION_RECORD_SCHEMA_VERSION,
                "status": "recording",
                "replayable": False,
                "session_id": processor.session_id,
                "recording_mode": self.recording_mode,
                "engine_version": ENGINE_VERSION,
                "runtime": _runtime_versions(),
                "event_schema_version": EVENT_SCHEMA_VERSION,
                "ledger_schema_version": LEDGER_SCHEMA_VERSION,
                "catalog_schema_version": catalog["schema_version"],
                "catalog_version": catalog["catalog_version"],
                "catalog": catalog,
                "catalog_sha256": _sha256_json(catalog),
                "configuration": configuration,
                "configuration_sha256": _sha256_json(configuration),
                "engine_code_sha256": _engine_code_sha256(),
                "user_code_sha256": self.user_code_sha256,
                "strategy_instances": strategies,
                "seed": self.seed,
                "feed_channels": [],
                "clock_policy": self.clock_policy,
                "matching_policy": execution_config["matching_policy"],
                "portfolio_policy": execution_config["portfolio_policy"],
                "started_at_utc": datetime.now(timezone.utc).isoformat(),
                "completed_at_utc": None,
                "engine_status": "running",
                "recording_queue": {
                    "capacity": self.queue_capacity,
                    "max_queue_wait_ns": self.max_queue_wait_ns,
                    "segment_max_records": self.segment_max_records,
                    "segment_max_bytes": self.segment_max_bytes,
                },
                "recording_metrics": {},
                "incidents": [],
                "segments": [],
            }
            self._configured = True
        self._write_manifest()

    def start(self) -> None:
        with self._lock:
            if not self._configured:
                raise RuntimeError("Recorder must be configured before it starts.")
            if self._started:
                return
            self._started = True
        self._writer.start()

    def record(
        self,
        kind: str,
        arrival_ordinal: int | None,
        payload: Any,
        *,
        event_time_ns: int | None = None,
    ) -> bool:
        """Enqueue an immutable record; returns false when the recording is incomplete."""

        if not isinstance(kind, str) or not kind:
            raise ValueError("record kind must be non-empty.")
        with self._lock:
            if not self._started or self._closing or self._closed:
                return False
            if self._failed:
                self._dropped_records += 1
                return False
            sequence = self._record_sequence
            self._record_sequence += 1
            item = _QueuedRecord(sequence, kind, arrival_ordinal, event_time_ns, payload, monotonic_ns())
            try:
                self._queue.put_nowait(item)
            except Full:
                self._dropped_records += 1
                self._failed = True
                self._error = "bounded recorder queue overflow"
                self._incidents.append({
                    "code": "recording_queue_overflow",
                    "record_ordinal": sequence,
                    "kind": kind,
                    "arrival_ordinal": arrival_ordinal,
                    "message": self._error,
                })
                return False
            self._enqueued_records += 1
            self._high_watermark = max(self._high_watermark, self._queue.qsize())
            if kind == "frame_received" and isinstance(payload, RawFrame):
                self._channels.add((payload.provider_id, payload.channel))
            return True

    def close(self, *, engine_status: str = "completed", timeout: float = 30.0) -> RecordingSummary:
        with self._lock:
            already_closed = self._closed
        if already_closed:
            return self.summary()
        with self._lock:
            self._engine_status = engine_status
            self._closing = True
            started = self._started
        if started and self._writer.is_alive():
            deadline = monotonic_ns() + int(timeout * 1_000_000_000)
            while self._writer.is_alive():
                try:
                    self._queue.put(_WRITER_STOP, timeout=0.05)
                    break
                except Full:
                    if monotonic_ns() >= deadline:
                        self._fail("timed out waiting for recorder queue to drain")
                        break
            self._writer.join(max(0.0, (deadline - monotonic_ns()) / 1_000_000_000))
            if self._writer.is_alive():
                self._fail("recorder writer did not stop before timeout")
        with self._lock:
            self._closed = True
            complete = not self._failed and self._dropped_records == 0 and not self._writer.is_alive()
            if complete:
                self._manifest["status"] = "complete"
            else:
                self._manifest["status"] = "incomplete"
            explicit_strategy_state = all(
                item.get("api_version") != "legacy-on_event"
                for item in self._manifest["strategy_instances"]
            )
            self._manifest["replayable"] = bool(
                complete and self._engine_status != "aborted" and explicit_strategy_state
            )
            self._manifest["engine_status"] = self._engine_status
            self._manifest["completed_at_utc"] = datetime.now(timezone.utc).isoformat()
            self._manifest["feed_channels"] = [
                {"provider_id": provider, "channel": channel}
                for provider, channel in sorted(self._channels)
            ]
            self._manifest["segments"] = list(self._segments)
            self._manifest["incidents"] = list(self._incidents)
            self._manifest["recording_metrics"] = {
                "queue_capacity": self.queue_capacity,
                "enqueued_records": self._enqueued_records,
                "written_records": self._written_records,
                "dropped_records": self._dropped_records,
                "pending_records": self._queue.qsize(),
                "high_watermark": self._high_watermark,
                "max_queue_wait_ns": self._max_queue_wait_ns,
                "max_write_ns": self._max_write_ns,
                "segment_count": len(self._segments),
                "writer_error": self._error,
            }
        if self._configured:
            self._write_manifest()
        return self.summary()

    def summary(self) -> RecordingSummary:
        with self._lock:
            if self._failed or self._dropped_records:
                status = "incomplete"
            elif self._closed:
                status = "complete"
            elif self._started:
                status = "recording"
            else:
                status = "not_started"
            return RecordingSummary(
                status=status,
                path=str(self.path),
                queue_capacity=self.queue_capacity,
                enqueued_records=self._enqueued_records,
                written_records=self._written_records,
                dropped_records=self._dropped_records,
                pending_records=self._queue.qsize(),
                high_watermark=self._high_watermark,
                max_queue_wait_ns=self._max_queue_wait_ns,
                max_write_ns=self._max_write_ns,
                segment_count=len(self._segments),
                error=self._error,
            )

    def _fail(self, message: str) -> None:
        with self._lock:
            if self._failed:
                return
            self._failed = True
            self._error = message
            self._incidents.append({"code": "recording_writer_failed", "message": message})

    def _write_loop(self) -> None:
        try:
            while True:
                try:
                    item = self._queue.get(timeout=0.1)
                except Empty:
                    continue
                if item is _WRITER_STOP:
                    break
                try:
                    write_started_ns = monotonic_ns()
                    wait_ns = max(0, monotonic_ns() - item.enqueued_monotonic_ns)
                    if self.max_queue_wait_ns is not None and wait_ns > self.max_queue_wait_ns:
                        self._fail(
                            f"recorder queue wait {wait_ns} ns exceeded configured budget "
                            f"{self.max_queue_wait_ns} ns"
                        )
                    record = {
                        "record_schema_version": SESSION_RECORD_SCHEMA_VERSION,
                        "record_ordinal": item.record_ordinal,
                        "kind": item.kind,
                        "arrival_ordinal": item.arrival_ordinal,
                        "event_time_ns": item.event_time_ns,
                        "payload": _json_value(item.payload),
                    }
                    _RECORD_VALIDATOR.validate(record)
                    line = (canonical_json(record) + "\n").encode("utf-8")
                    if self._current_file is None or self._current_count >= self.segment_max_records or (
                        self._current_count > 0 and self._current_bytes + len(line) > self.segment_max_bytes
                    ):
                        self._finish_segment()
                        self._open_segment()
                    self._current_file.write(line)
                    self._current_hash.update(line)
                    self._current_count += 1
                    self._current_bytes += len(line)
                    with self._lock:
                        self._written_records += 1
                        self._max_queue_wait_ns = max(self._max_queue_wait_ns, wait_ns)
                        self._max_write_ns = max(
                            self._max_write_ns,
                            max(0, monotonic_ns() - write_started_ns),
                        )
                finally:
                    self._queue.task_done()
            self._finish_segment()
        except Exception as exc:
            self._fail(f"{type(exc).__name__}: {exc}")
            try:
                self._finish_segment()
            except Exception:
                pass

    def _open_segment(self) -> None:
        number = len(self._segments) + 1
        name = f"segment-{number:06d}.jsonl"
        self._current_name = name
        self._current_file = (self.path / name).open("xb")
        self._current_hash = hashlib.sha256()
        self._current_count = 0
        self._current_bytes = 0

    def _finish_segment(self) -> None:
        if self._current_file is None:
            return
        self._current_file.flush()
        os.fsync(self._current_file.fileno())
        self._current_file.close()
        self._segments.append({
            "file": self._current_name,
            "record_count": self._current_count,
            "bytes": self._current_bytes,
            "sha256": self._current_hash.hexdigest(),
        })
        self._current_file = None
        self._current_hash = None
        self._current_name = None

    def _write_manifest(self) -> None:
        if self._manifest is None:
            return
        _MANIFEST_VALIDATOR.validate(_json_value(self._manifest))
        data = canonical_json(self._manifest) + "\n"
        temp = self.path / "manifest.json.tmp"
        target = self.path / "manifest.json"
        temp.write_text(data, encoding="utf-8", newline="\n")
        with temp.open("r+b") as handle:
            os.fsync(handle.fileno())
        os.replace(temp, target)
        digest = hashlib.sha256(target.read_bytes()).hexdigest()
        checksum_temp = self.path / "manifest.sha256.tmp"
        checksum_temp.write_text(digest + "  manifest.json\n", encoding="ascii", newline="\n")
        with checksum_temp.open("r+b") as handle:
            os.fsync(handle.fileno())
        os.replace(checksum_temp, self.path / "manifest.sha256")


def _is_sha256(value: str) -> bool:
    return len(value) == 64 and all(char in "0123456789abcdef" for char in value)


class SessionRecording:
    """Verified session directory; frame iteration is in captured receive order."""

    def __init__(self, path: str | Path):
        self.path = Path(path).expanduser().resolve()
        try:
            self.manifest = json.loads((self.path / "manifest.json").read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise RecordingError(f"Cannot read session manifest: {exc}") from exc
        self._verify()

    def _verify(self) -> None:
        manifest = self.manifest
        if not isinstance(manifest, Mapping):
            raise RecordingError("Session manifest must be a JSON object.")
        if manifest.get("manifest_schema_version") != SESSION_FORMAT_VERSION:
            raise RecordingError(f"Unsupported session manifest schema {manifest.get('manifest_schema_version')!r}.")
        if manifest.get("record_schema_version") != SESSION_RECORD_SCHEMA_VERSION:
            raise RecordingError(f"Unsupported session record schema {manifest.get('record_schema_version')!r}.")
        if manifest.get("event_schema_version") != EVENT_SCHEMA_VERSION:
            raise RecordingError(f"Unsupported market event schema {manifest.get('event_schema_version')!r}.")
        if manifest.get("ledger_schema_version") != LEDGER_SCHEMA_VERSION:
            raise RecordingError(f"Unsupported ledger schema {manifest.get('ledger_schema_version')!r}.")
        if manifest.get("status") != "complete":
            raise RecordingError(f"Session recording is {manifest.get('status')!r}; only complete sessions can be replayed.")
        if not manifest.get("replayable"):
            raise RecordingError("Session manifest does not mark this capture as replayable.")
        try:
            checksum_line = (self.path / "manifest.sha256").read_text(encoding="ascii").strip()
            expected_manifest_hash = checksum_line.split()[0]
            actual_manifest_hash = hashlib.sha256((self.path / "manifest.json").read_bytes()).hexdigest()
        except (OSError, IndexError) as exc:
            raise RecordingError(f"Cannot verify manifest checksum: {exc}") from exc
        if not _is_sha256(expected_manifest_hash) or expected_manifest_hash != actual_manifest_hash:
            raise RecordingError("manifest.json checksum mismatch.")
        try:
            _MANIFEST_VALIDATOR.validate(manifest)
        except Exception as exc:
            raise RecordingError(f"Session manifest does not match its schema: {exc}") from exc
        if _sha256_json(manifest["catalog"]) != manifest.get("catalog_sha256"):
            raise RecordingError("Instrument catalog hash mismatch in manifest.")
        if _sha256_json(manifest["configuration"]) != manifest.get("configuration_sha256"):
            raise RecordingError("Configuration hash mismatch in manifest.")
        records = manifest.get("segments")
        if not isinstance(records, list):
            raise RecordingError("Manifest segments must be an array.")
        expected_ordinal = 0
        for segment in records:
            filename = segment.get("file")
            if not isinstance(filename, str) or Path(filename).name != filename or not filename.endswith(".jsonl"):
                raise RecordingError("Manifest contains an invalid segment filename.")
            path = self.path / filename
            try:
                with path.open("rb") as handle:
                    content_hash = hashlib.file_digest(handle, "sha256").hexdigest()
                size = path.stat().st_size
            except OSError as exc:
                raise RecordingError(f"Cannot read segment {filename}: {exc}") from exc
            if content_hash != segment.get("sha256") or size != segment.get("bytes"):
                raise RecordingError(f"Segment checksum or size mismatch: {filename}.")
            actual_count = 0
            try:
                with path.open("r", encoding="utf-8") as handle:
                    for line_number, line in enumerate(handle, start=1):
                        row = json.loads(line)
                        try:
                            _RECORD_VALIDATOR.validate(row)
                        except Exception as exc:
                            raise RecordingError(f"{filename}:{line_number}: record schema error: {exc}") from exc
                        if row.get("record_schema_version") != SESSION_RECORD_SCHEMA_VERSION:
                            raise RecordingError(f"{filename}:{line_number}: unsupported record schema.")
                        if row.get("record_ordinal") != expected_ordinal:
                            raise RecordingError(f"{filename}:{line_number}: record ordinal gap or reordering.")
                        expected_ordinal += 1
                        actual_count += 1
            except (OSError, json.JSONDecodeError) as exc:
                raise RecordingError(f"Invalid JSONL segment {filename}: {exc}") from exc
            if actual_count != segment.get("record_count"):
                raise RecordingError(f"Record count mismatch in segment {filename}.")
        metrics = manifest.get("recording_metrics", {})
        if expected_ordinal != metrics.get("written_records") or metrics.get("dropped_records") != 0:
            raise RecordingError("Recording metrics do not agree with the complete segment set.")
        if (
            metrics.get("enqueued_records") != expected_ordinal
            or metrics.get("pending_records") != 0
            or metrics.get("segment_count") != len(records)
        ):
            raise RecordingError("Recording queue and segment metrics are inconsistent.")

    def records(self):
        for segment in self.manifest["segments"]:
            with (self.path / segment["file"]).open("r", encoding="utf-8") as handle:
                for line in handle:
                    yield json.loads(line)

    def frames(self):
        previous_ordinal: int | None = None
        for record in self.records():
            if record["kind"] != "frame_received":
                continue
            payload = record["payload"]
            try:
                frame = RawFrame(
                    provider_id=payload["provider_id"],
                    channel=payload["channel"],
                    receive_time_utc_ns=payload["receive_time_utc_ns"],
                    receive_time_monotonic_ns=payload["receive_time_monotonic_ns"],
                    arrival_ordinal=payload["arrival_ordinal"],
                    raw_payload=base64.b64decode(payload["raw_payload_b64"], validate=True),
                )
            except (KeyError, TypeError, ValueError) as exc:
                raise RecordingError(f"Invalid captured frame at record {record.get('record_ordinal')}: {exc}") from exc
            if previous_ordinal is not None and frame.arrival_ordinal <= previous_ordinal:
                raise RecordingError("Captured frame arrival ordinals are not strictly increasing.")
            if record.get("arrival_ordinal") != frame.arrival_ordinal:
                raise RecordingError("Frame envelope and RawFrame ordinals disagree.")
            previous_ordinal = frame.arrival_ordinal
            yield frame


class InMemoryRecorder:
    """Replay-only observer with the same record interface as SessionRecorder."""

    def __init__(self) -> None:
        self.items: list[dict[str, Any]] = []

    def configure(self, processor: Any) -> None:
        """Match the SessionRecorder lifecycle without opening a writer."""

    def start(self) -> None:
        """Start is intentionally a no-op for in-memory replay capture."""

    def record(self, kind: str, arrival_ordinal: int | None, payload: Any, *, event_time_ns: int | None = None) -> bool:
        self.items.append({
            "record_schema_version": SESSION_RECORD_SCHEMA_VERSION,
            "kind": kind,
            "arrival_ordinal": arrival_ordinal,
            "event_time_ns": event_time_ns,
            "payload": payload,
        })
        return True

    def summary(self) -> RecordingSummary:
        return RecordingSummary("complete", "<memory>", 0, len(self.items), len(self.items), 0, 0, 0, 0, 0, 0)

    def close(self, *, engine_status: str = "completed", timeout: float = 30.0) -> RecordingSummary:
        return self.summary()


def _comparable(records: list[Mapping[str, Any]]) -> list[dict[str, Any]]:
    selected: list[dict[str, Any]] = []
    for record in records:
        kind = record.get("kind")
        if kind not in _COMPARABLE_KINDS:
            continue
        payload = _json_value(record.get("payload"))
        if kind == "session_diagnostic" and isinstance(payload, Mapping) and payload.get("code") in _NONDETERMINISTIC_DIAGNOSTICS:
            continue
        selected.append({
            "arrival_ordinal": record.get("arrival_ordinal"),
            "kind": kind,
            "payload": payload,
        })
    # Different worker scheduling can interleave stages differently. Compare
    # canonical outcomes by arrival ordinal and kind, retaining duplicates.
    selected.sort(key=lambda item: (
        -1 if item["arrival_ordinal"] is None else item["arrival_ordinal"],
        item["kind"],
        canonical_json(item),
    ))
    return selected


def compare_recordings(expected: list[Mapping[str, Any]], actual: list[Mapping[str, Any]]) -> ReplayComparison:
    left = _comparable(expected)
    right = _comparable(actual)
    for index in range(max(len(left), len(right))):
        exp = left[index] if index < len(left) else None
        act = right[index] if index < len(right) else None
        if exp != act:
            chosen = exp or act or {}
            return ReplayComparison(
                False,
                len(left),
                len(right),
                ReplayDivergence(
                    arrival_ordinal=chosen.get("arrival_ordinal"),
                    kind=chosen.get("kind", "record_count"),
                    expected=exp,
                    actual=act,
                    detail=f"First differing canonical outcome at comparable record {index}.",
                ),
            )
    return ReplayComparison(True, len(left), len(right), None)


def replay_session(
    path: str | Path,
    processor: Any,
    *,
    clock_mode: str | None = None,
    step_callback: Callable[[RawFrame, int, int], None] | None = None,
    timeout: float = 30.0,
) -> ReplayResult:
    """Replay captured RawFrames through a matching configured EventProcessor."""

    recording = SessionRecording(path)
    if clock_mode is None:
        clock_mode = "step" if recording.manifest["clock_policy"] == "controlled_step" else "event_time"
    if clock_mode not in {"event_time", "step"}:
        raise ValueError("clock_mode must be event_time or step.")
    if not processor.config.preserve_arrival_ordinals:
        raise ReplayCompatibilityError("Replay processor must set preserve_arrival_ordinals=True.")
    if processor._started:
        raise ReplayCompatibilityError("Replay processor must not be started before replay_session().")
    catalog_hash = _sha256_json(processor.catalog.to_mapping())
    if catalog_hash != recording.manifest.get("catalog_sha256"):
        raise ReplayCompatibilityError("Processor instrument catalog differs from the recorded catalog.")
    config = _processor_configuration(processor)
    expected_config = recording.manifest["configuration"]
    proposed = {
        "event_processor": config,
        "strategies": _strategy_manifest(processor.strategy),
        "recording": expected_config["recording"],
    }
    if _sha256_json(proposed) != recording.manifest.get("configuration_sha256"):
        raise ReplayCompatibilityError("Processor policies, queue settings, strategy config, seed, or initial state differ from the recording.")
    if processor.recorder is not None:
        raise ReplayCompatibilityError("Replay processor already has a recorder attached.")
    processor.session_id = recording.manifest["session_id"]
    observer = InMemoryRecorder()
    processor.recorder = observer
    virtual_time_ns: int | None = None
    frame_count = 0
    snapshot_outcomes: dict[int, DataQualityDiagnostic] = {}
    event_times: dict[int, int | None] = {}
    for record in recording.records():
        if record.get("kind") == "normalized_event" and record.get("arrival_ordinal") is not None:
            event_times[record["arrival_ordinal"]] = record.get("event_time_ns")
        if record.get("kind") != "validated_event" or not isinstance(record.get("payload"), Mapping):
            continue
        for item in record["payload"].get("diagnostics", ()):
            if not isinstance(item, Mapping) or not str(item.get("code", "")).startswith("snapshot_request_"):
                continue
            ordinal = item.get("arrival_ordinal")
            if isinstance(ordinal, int) and not isinstance(ordinal, bool):
                snapshot_outcomes[ordinal] = DataQualityDiagnostic(
                    provider_id=item["provider_id"],
                    channel=item["channel"],
                    instrument_id=item.get("instrument_id"),
                    code=item["code"],
                    message=item["message"],
                    arrival_ordinal=ordinal,
                )
    prior_snapshot_outcomes = processor.pipeline.replay_snapshot_outcomes
    processor.pipeline.replay_snapshot_outcomes = snapshot_outcomes
    try:
        for frame in recording.frames():
            frame_count += 1
            event_time_ns = event_times.get(frame.arrival_ordinal)
            virtual_time_ns = frame.receive_time_utc_ns if event_time_ns is None else event_time_ns
            if step_callback is not None:
                step_callback(frame, frame_count, virtual_time_ns)
            accepted = processor.submit(frame)
            if accepted != frame.arrival_ordinal:
                raise RecordingError(f"Replay rejected captured frame ordinal {frame.arrival_ordinal}.")
            if not processor.wait_for_ordinal(frame.arrival_ordinal, timeout=timeout):
                raise TimeoutError(f"Replay did not complete arrival ordinal {frame.arrival_ordinal} before timeout.")
        report = processor.finish(timeout=timeout)
    finally:
        processor.pipeline.replay_snapshot_outcomes = prior_snapshot_outcomes
    expected_records = list(recording.records())
    comparison = compare_recordings(expected_records, observer.items)
    return ReplayResult(report, comparison, clock_mode, virtual_time_ns)


def recording_clock_policy(path: str | Path) -> str:
    """Read the clock policy from a verified manifest."""

    return SessionRecording(path).manifest["clock_policy"]

