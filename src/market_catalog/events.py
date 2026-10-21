"""Transport, canonical event, and ingestion result models."""

from __future__ import annotations

from base64 import b64encode
from dataclasses import dataclass
from decimal import Decimal
from typing import Any, Mapping


EVENT_SCHEMA_VERSION = "1.0.0"


def _json_value(value: Any) -> Any:
    if isinstance(value, Decimal):
        return format(value, "f")
    if isinstance(value, Mapping):
        return {key: _json_value(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_json_value(item) for item in value]
    return value


@dataclass(frozen=True)
class Subscription:
    """One internal instrument mapped to the symbol/channel names of a source."""

    instrument_id: str
    provider_symbol: str
    channels: tuple[str, ...]


@dataclass(frozen=True)
class RawFrame:
    """A provider frame plus local receipt metadata, before normalization."""

    provider_id: str
    channel: str
    receive_time_utc_ns: int
    receive_time_monotonic_ns: int
    arrival_ordinal: int
    raw_payload: bytes


@dataclass(frozen=True)
class AdapterHealth:
    status: str
    connected: bool
    detail: str = ""
    received_frames: int = 0
    last_received_monotonic_ns: int | None = None


@dataclass(frozen=True)
class MarketEvent:
    provider_id: str
    channel: str
    instrument_id: str
    event_type: str
    source_timestamp_raw: str | None
    source_timestamp_ns: int | None
    timestamp_semantics: str
    receive_time_utc_ns: int
    receive_time_monotonic_ns: int
    arrival_ordinal: int
    sequence: int | None
    sequence_scope: str
    payload: Mapping[str, Any]
    quality_flags: tuple[str, ...] = ()
    decision_usable: bool = True
    book_state: str = "not_applicable"
    raw_frame: bytes | None = None
    schema_version: str = EVENT_SCHEMA_VERSION

    def to_mapping(self) -> dict[str, Any]:
        value: dict[str, Any] = {
            "schema_version": self.schema_version,
            "provider_id": self.provider_id,
            "channel": self.channel,
            "instrument_id": self.instrument_id,
            "event_type": self.event_type,
            "source_timestamp_raw": self.source_timestamp_raw,
            "source_timestamp_ns": self.source_timestamp_ns,
            "timestamp_semantics": self.timestamp_semantics,
            "receive_time_utc_ns": self.receive_time_utc_ns,
            "receive_time_monotonic_ns": self.receive_time_monotonic_ns,
            "arrival_ordinal": self.arrival_ordinal,
            "sequence": self.sequence,
            "sequence_scope": self.sequence_scope,
            "payload": _json_value(self.payload),
            "quality_flags": list(self.quality_flags),
            "decision_usable": self.decision_usable,
            "book_state": self.book_state,
        }
        if self.raw_frame is not None:
            value["raw_frame_b64"] = b64encode(self.raw_frame).decode("ascii")
        return value


@dataclass(frozen=True)
class DataQualityDiagnostic:
    provider_id: str
    channel: str
    instrument_id: str | None
    code: str
    message: str
    arrival_ordinal: int | None = None


@dataclass(frozen=True)
class ProcessingResult:
    event: MarketEvent | None
    diagnostics: tuple[DataQualityDiagnostic, ...] = ()


@dataclass(frozen=True)
class StreamHealth:
    provider_id: str
    channel: str
    instrument_id: str
    last_sequence: int | None
    last_receive_monotonic_ns: int
    book_state: str
    stale: bool | None
    decision_usable: bool
