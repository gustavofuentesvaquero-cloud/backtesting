"""Sequence, timestamp, freshness, and book-recovery quality policy."""

from __future__ import annotations

from collections import Counter, deque
from collections.abc import Mapping
from dataclasses import dataclass, field, replace
from threading import Lock
from types import MappingProxyType
from typing import Any

from .events import DataQualityDiagnostic, MarketEvent, RawFrame, StreamHealth
from .normalization import NormalizationError


@dataclass(frozen=True)
class QualityPolicy:
    """Configurable quality thresholds; unset limits make no timing claim."""

    max_source_age_ns: int | None = None
    max_silence_ns: int | None = None
    source_clock_comparable: bool = False
    clock_skew_tolerance_ns: int = 0
    sequence_required: bool = False
    source_timestamp_required: bool = False
    recent_event_window: int = 4096

    def __post_init__(self) -> None:
        for name in ("max_source_age_ns", "max_silence_ns"):
            value = getattr(self, name)
            if value is not None and (isinstance(value, bool) or not isinstance(value, int) or value <= 0):
                raise ValueError(f"{name} must be a positive integer or None")
        if isinstance(self.clock_skew_tolerance_ns, bool) or not isinstance(self.clock_skew_tolerance_ns, int) or self.clock_skew_tolerance_ns < 0:
            raise ValueError("clock_skew_tolerance_ns must be non-negative")
        if isinstance(self.recent_event_window, bool) or not isinstance(self.recent_event_window, int) or self.recent_event_window <= 0:
            raise ValueError("recent_event_window must be positive")


@dataclass
class _StreamState:
    last_sequence: int | None = None
    sequence_scope: str | None = None
    last_source_timestamp_ns: int | None = None
    timestamp_semantics: str | None = None
    last_receive_monotonic_ns: int | None = None
    last_quality_usable: bool = False
    fingerprint_order: deque[Any] = field(default_factory=deque)
    fingerprint_set: set[Any] = field(default_factory=set)
    book_key: tuple[str, str, str] | None = None


@dataclass
class _SequenceState:
    last_sequence: int | None = None
    sequence_order: deque[int] = field(default_factory=deque)
    sequence_set: set[int] = field(default_factory=set)


class DataQualityMonitor:
    """Assess events and keep a book invalid until a complete snapshot arrives."""

    _fatal_flags = {
        "sequence_duplicate",
        "sequence_out_of_order",
        "sequence_scope_unverified",
        "sequence_scope_changed",
        "duplicate_payload",
        "event_time_out_of_order",
        "timestamp_semantics_changed",
        "stale_source_timestamp",
        "clock_skew",
        "receive_time_out_of_order",
        "missing_sequence",
        "missing_source_timestamp",
        "awaiting_snapshot",
    }
    _invalidating_delta_flags = {
        "sequence_duplicate",
        "sequence_out_of_order",
        "sequence_scope_unverified",
        "sequence_scope_changed",
        "duplicate_payload",
        "event_time_out_of_order",
        "timestamp_semantics_changed",
        "stale_source_timestamp",
        "clock_skew",
        "receive_time_out_of_order",
        "missing_sequence",
        "missing_source_timestamp",
    }

    _flag_messages = {
        "timestamp_unverified": "Source timestamp is missing or its meaning is unknown.",
        "sequence_unavailable": "The source did not provide a sequence number.",
        "missing_sequence": "A sequence number is required by the configured quality policy.",
        "sequence_gap": "Sequence gap detected; intervening source messages may be missing.",
        "sequence_duplicate": "Sequence number was already observed on this stream.",
        "sequence_out_of_order": "Sequence number moved backwards on this stream.",
        "sequence_scope_unverified": "Sequence is present but its provider scope is unknown; gap checks cannot be trusted.",
        "sequence_scope_changed": "Sequence scope changed within a stream; reconfigure or restart the adapter before using sequence checks.",
        "duplicate_payload": "Payload fingerprint was already observed in the recent duplicate window.",
        "event_time_out_of_order": "Source event timestamp moved backwards on this stream.",
        "timestamp_semantics_changed": "Timestamp semantics changed within a stream; do not compare these timestamps until the adapter is reconfigured.",
        "stale_source_timestamp": "Source timestamp is older than the configured maximum source age.",
        "clock_skew": "Source timestamp is ahead of the local receive clock beyond configured tolerance.",
        "receive_time_out_of_order": "Local monotonic receive time moved backwards on this stream.",
        "awaiting_snapshot": "Book state is invalid until a complete snapshot is received.",
        "receive_silence_exceeded": "Time since the previous message exceeded the configured silence limit.",
        "missing_source_timestamp": "A source timestamp is required by the configured quality policy.",
    }

    def __init__(self, policy: QualityPolicy | None = None):
        self.policy = policy or QualityPolicy()
        self._streams: dict[tuple[str, str, str], _StreamState] = {}
        self._sequence_states: dict[tuple[str, str, str], _SequenceState] = {}
        self._book_states: dict[tuple[str, str, str], str] = {}
        self._diagnostic_counts: Counter[str] = Counter()
        self._diagnostic_lock = Lock()

    @staticmethod
    def _book_key(event: MarketEvent) -> tuple[str, str, str] | None:
        if event.event_type == "quote":
            return event.provider_id, event.instrument_id, "L1"
        if event.event_type in {"book_snapshot", "book_delta"}:
            depth_type = event.payload.get("depth_type")
            return event.provider_id, event.instrument_id, str(depth_type or "L2")
        return None

    def _remember(self, queue: deque[Any], items: set[Any], value: Any) -> None:
        if value in items:
            return
        queue.append(value)
        items.add(value)
        while len(queue) > self.policy.recent_event_window:
            items.discard(queue.popleft())

    @classmethod
    def _freeze_payload(cls, value: Any) -> Any:
        """Build an exact, hashable fingerprint without JSON serialization."""

        if isinstance(value, Mapping):
            return tuple(sorted((key, cls._freeze_payload(item)) for key, item in value.items()))
        if isinstance(value, (list, tuple)):
            return tuple(cls._freeze_payload(item) for item in value)
        return value

    @classmethod
    def _fingerprint(cls, event: MarketEvent) -> tuple[Any, ...]:
        return (
            event.event_type,
            event.source_timestamp_raw,
            event.source_timestamp_ns,
            event.timestamp_semantics,
            cls._freeze_payload(event.payload),
        )

    def assess(self, event: MarketEvent) -> MarketEvent:
        stream_key = (event.provider_id, event.channel, event.instrument_id)
        state = self._streams.setdefault(stream_key, _StreamState())
        flags = set(event.quality_flags)
        book_key = self._book_key(event)
        if book_key is not None:
            state.book_key = book_key

        if state.last_receive_monotonic_ns is not None and self.policy.max_silence_ns is not None:
            if event.receive_time_monotonic_ns - state.last_receive_monotonic_ns > self.policy.max_silence_ns:
                flags.add("receive_silence_exceeded")
        if (
            state.last_receive_monotonic_ns is not None
            and event.receive_time_monotonic_ns < state.last_receive_monotonic_ns
        ):
            flags.add("receive_time_out_of_order")

        if event.sequence is None:
            if self.policy.sequence_required:
                flags.add("missing_sequence")
            else:
                flags.add("sequence_unavailable")
            fingerprint = self._fingerprint(event)
            if fingerprint in state.fingerprint_set:
                flags.add("duplicate_payload")
            self._remember(state.fingerprint_order, state.fingerprint_set, fingerprint)
        else:
            if event.sequence_scope == "unknown":
                flags.add("sequence_scope_unverified")
                fingerprint = self._fingerprint(event)
                if fingerprint in state.fingerprint_set:
                    flags.add("duplicate_payload")
                self._remember(state.fingerprint_order, state.fingerprint_set, fingerprint)
                state.last_sequence = event.sequence
            else:
                if state.sequence_scope is not None and state.sequence_scope != event.sequence_scope:
                    flags.add("sequence_scope_changed")
                else:
                    state.sequence_scope = event.sequence_scope
                    if event.sequence_scope == "instrument_channel":
                        sequence_key = (event.provider_id, event.channel, event.instrument_id)
                    elif event.sequence_scope == "channel":
                        sequence_key = (event.provider_id, event.channel, "*")
                    else:  # provider-wide sequence
                        sequence_key = (event.provider_id, "*", "*")
                    sequence_state = self._sequence_states.setdefault(sequence_key, _SequenceState())
                    if event.sequence in sequence_state.sequence_set:
                        flags.add("sequence_duplicate")
                    else:
                        if sequence_state.last_sequence is not None:
                            if event.sequence < sequence_state.last_sequence:
                                flags.add("sequence_out_of_order")
                            elif event.sequence > sequence_state.last_sequence + 1:
                                flags.add("sequence_gap")
                        self._remember(
                            sequence_state.sequence_order,
                            sequence_state.sequence_set,
                            event.sequence,
                        )
                        if "sequence_out_of_order" not in flags:
                            sequence_state.last_sequence = event.sequence
                            state.last_sequence = event.sequence

        if event.source_timestamp_ns is None or event.timestamp_semantics == "unknown":
            flags.add("timestamp_unverified")
            if self.policy.source_timestamp_required:
                flags.add("missing_source_timestamp")
        else:
            semantics_changed = False
            if state.timestamp_semantics is not None and state.timestamp_semantics != event.timestamp_semantics:
                flags.add("timestamp_semantics_changed")
                semantics_changed = True
            else:
                state.timestamp_semantics = event.timestamp_semantics
            if not semantics_changed:
                previous_source_ns = state.last_source_timestamp_ns
                if previous_source_ns is not None and event.source_timestamp_ns < previous_source_ns:
                    flags.add("event_time_out_of_order")
                elif previous_source_ns is None or event.source_timestamp_ns > previous_source_ns:
                    state.last_source_timestamp_ns = event.source_timestamp_ns

            if not semantics_changed and self.policy.source_clock_comparable and self.policy.max_source_age_ns is not None:
                age_ns = event.receive_time_utc_ns - event.source_timestamp_ns
                if age_ns < -self.policy.clock_skew_tolerance_ns:
                    flags.add("clock_skew")
                elif age_ns > self.policy.max_source_age_ns:
                    flags.add("stale_source_timestamp")

        if (
            state.last_receive_monotonic_ns is None
            or event.receive_time_monotonic_ns >= state.last_receive_monotonic_ns
        ):
            state.last_receive_monotonic_ns = event.receive_time_monotonic_ns

        book_state = "not_applicable"
        if book_key is not None:
            current_state = self._book_states.get(book_key, "awaiting_snapshot" if event.event_type == "book_delta" else "valid")
            if event.event_type in {"quote", "book_snapshot"}:
                unrecoverable = {
                    "sequence_duplicate",
                    "sequence_out_of_order",
                    "duplicate_payload",
                    "event_time_out_of_order",
                    "timestamp_semantics_changed",
                    "stale_source_timestamp",
                    "clock_skew",
                    "receive_time_out_of_order",
                    "missing_sequence",
                    "missing_source_timestamp",
                }
                if not unrecoverable.intersection(flags):
                    current_state = "valid"
                    self._book_states[book_key] = current_state
            elif event.event_type == "book_delta":
                if (
                    self._invalidating_delta_flags.intersection(flags)
                    or "sequence_gap" in flags
                    or "receive_silence_exceeded" in flags
                ):
                    current_state = "awaiting_snapshot"
                if current_state != "valid":
                    current_state = "awaiting_snapshot"
                    flags.add("awaiting_snapshot")
                self._book_states[book_key] = current_state
            book_state = current_state

        fatal = self._fatal_flags.intersection(flags)
        if "sequence_gap" in flags and event.event_type not in {"quote", "book_snapshot"}:
            fatal.add("sequence_gap")
        decision_usable = not fatal
        state.last_quality_usable = decision_usable
        if flags:
            with self._diagnostic_lock:
                self._diagnostic_counts.update(flags)
        return replace(
            event,
            quality_flags=tuple(sorted(flags)),
            decision_usable=decision_usable,
            book_state=book_state,
        )

    def record_rejection(self, error: NormalizationError, frame: RawFrame | None = None) -> DataQualityDiagnostic:
        with self._diagnostic_lock:
            self._diagnostic_counts[error.code] += 1
        if error.instrument_id is not None:
            stream_key = (error.provider_id, error.channel, error.instrument_id)
            state = self._streams.setdefault(stream_key, _StreamState())
            for key, state in self._streams.items():
                if key[0] == error.provider_id and key[2] == error.instrument_id:
                    state.last_quality_usable = False
            state = self._streams[stream_key]
            if frame is not None:
                state.last_receive_monotonic_ns = frame.receive_time_monotonic_ns
            if error.event_type == "quote":
                state.book_key = (error.provider_id, error.instrument_id, "L1")
                self._book_states[state.book_key] = "awaiting_snapshot"
            elif error.event_type in {"book_delta", "book_snapshot"}:
                l2_keys = [
                    key
                    for key in self._book_states
                    if key[:2] == (error.provider_id, error.instrument_id) and key[2] != "L1"
                ]
                for key in l2_keys:
                    self._book_states[key] = "awaiting_snapshot"
                state.book_key = l2_keys[0] if l2_keys else (error.provider_id, error.instrument_id, "L2")
                self._book_states[state.book_key] = "awaiting_snapshot"
        if error.instrument_id is not None and error.event_type in {"book_delta", "book_snapshot"}:
            for key in tuple(self._book_states):
                if key[:2] == (error.provider_id, error.instrument_id) and key[2] != "L1":
                    self._book_states[key] = "awaiting_snapshot"
        return DataQualityDiagnostic(
            provider_id=error.provider_id,
            channel=error.channel,
            instrument_id=error.instrument_id,
            code=error.code,
            message=f"{error.path}: {error}",
            arrival_ordinal=error.arrival_ordinal,
        )

    def diagnostic_counts(self) -> Mapping[str, int]:
        """Return cumulative quality counters for out-of-band telemetry."""

        with self._diagnostic_lock:
            return MappingProxyType(dict(self._diagnostic_counts))

    def mark_reconnected(self, provider_id: str) -> None:
        """Reset provider sequences and require fresh snapshots after reconnect."""

        for key, state in self._streams.items():
            if key[0] == provider_id:
                state.last_sequence = None
                state.sequence_scope = None
                state.last_source_timestamp_ns = None
                state.timestamp_semantics = None
                state.last_quality_usable = False
                state.fingerprint_order.clear()
                state.fingerprint_set.clear()
        self._sequence_states = {
            key: state for key, state in self._sequence_states.items() if key[0] != provider_id
        }
        for key in tuple(self._book_states):
            if key[0] == provider_id:
                self._book_states[key] = "awaiting_snapshot"

    def diagnostics_for(self, event: MarketEvent) -> tuple[DataQualityDiagnostic, ...]:
        return tuple(
            DataQualityDiagnostic(
                provider_id=event.provider_id,
                channel=event.channel,
                instrument_id=event.instrument_id,
                code=flag,
                message=self._flag_messages.get(flag, flag),
                arrival_ordinal=event.arrival_ordinal,
            )
            for flag in event.quality_flags
        )

    def health_snapshot(self, now_monotonic_ns: int) -> tuple[StreamHealth, ...]:
        snapshots: list[StreamHealth] = []
        for (provider_id, channel, instrument_id), state in sorted(self._streams.items()):
            last_receive = state.last_receive_monotonic_ns
            stale = None
            if self.policy.max_silence_ns is not None and last_receive is not None:
                stale = now_monotonic_ns - last_receive > self.policy.max_silence_ns
            book_state = self._book_states.get(state.book_key, "not_applicable") if state.book_key else "not_applicable"
            usable = state.last_quality_usable and stale is not True and book_state != "awaiting_snapshot"
            snapshots.append(
                StreamHealth(
                    provider_id=provider_id,
                    channel=channel,
                    instrument_id=instrument_id,
                    last_sequence=state.last_sequence,
                    last_receive_monotonic_ns=last_receive or 0,
                    book_state=book_state,
                    stale=stale,
                    decision_usable=usable,
                )
            )
        return tuple(snapshots)
