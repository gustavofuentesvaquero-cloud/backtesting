"""Glue between a source adapter, normalizer, and quality monitor."""

from __future__ import annotations

from collections.abc import Mapping

from .adapters.base import EventNormalizer, FeedAdapter
from .events import AdapterHealth, DataQualityDiagnostic, MarketEvent, ProcessingResult, RawFrame, StreamHealth
from .normalization import NormalizationError
from .quality import DataQualityMonitor


class MarketDataPipeline:
    """One source ingestion path; only quality-usable events should reach strategies."""

    def __init__(
        self,
        adapter: FeedAdapter,
        normalizer: EventNormalizer,
        quality: DataQualityMonitor,
        *,
        adapters_by_provider: Mapping[str, FeedAdapter] | None = None,
    ):
        self.adapter = adapter
        self.normalizer = normalizer
        self.quality = quality
        self.adapters_by_provider = dict(adapters_by_provider or {})
        self._snapshot_requests_attempted: set[tuple[str, str, str]] = set()
        # During disk replay, consume recorded recovery outcomes instead of
        # sending snapshot requests to a live provider adapter.
        self.replay_snapshot_outcomes: dict[int, DataQualityDiagnostic] | None = None

    def normalize(self, frame: RawFrame) -> MarketEvent:
        """Normalize one received frame without applying sequence policy."""

        return self.normalizer.normalize(frame)

    def reject(self, frame: RawFrame, error: NormalizationError) -> ProcessingResult:
        """Record a normalization rejection and request a book snapshot if needed."""

        diagnostic = self.quality.record_rejection(error, frame)
        if error.event_type in {"book_delta", "book_snapshot"} and error.instrument_id is not None:
            snapshot_diagnostic = self._submit_snapshot_request(
                error.provider_id,
                error.instrument_id,
                error.channel,
                error.arrival_ordinal,
            )
            if snapshot_diagnostic is not None:
                return ProcessingResult(event=None, diagnostics=(diagnostic, snapshot_diagnostic))
        return ProcessingResult(event=None, diagnostics=(diagnostic,))

    def validate(self, event: MarketEvent) -> ProcessingResult:
        """Apply sequence and quality rules to an already-normalized event."""

        assessed = self.quality.assess(event)
        diagnostics = self.quality.diagnostics_for(assessed) + self._request_snapshot_if_needed(assessed)
        return ProcessingResult(event=assessed, diagnostics=diagnostics)

    def _submit_snapshot_request(
        self,
        provider_id: str,
        instrument_id: str,
        channel: str,
        arrival_ordinal: int | None,
    ) -> DataQualityDiagnostic | None:
        key = (provider_id, instrument_id, channel)
        if key in self._snapshot_requests_attempted:
            return None
        self._snapshot_requests_attempted.add(key)
        if self.replay_snapshot_outcomes is not None:
            return self.replay_snapshot_outcomes.get(arrival_ordinal)
        adapter = self.adapters_by_provider.get(provider_id, self.adapter)
        request = getattr(adapter, "request_snapshot", None)
        if not callable(request):
            code = "snapshot_request_unsupported"
            message = "This adapter cannot request a snapshot; book decisions remain blocked until one arrives."
        else:
            try:
                accepted = request(instrument_id, channel)
            except Exception as exc:
                code = "snapshot_request_failed"
                message = f"Snapshot request raised {type(exc).__name__}: {exc}"
            else:
                code = "snapshot_request_accepted" if accepted else "snapshot_request_rejected"
                message = (
                    "Snapshot request was queued; wait for a complete snapshot before using the book."
                    if accepted
                    else "Adapter rejected the snapshot request; book decisions remain blocked."
                )
        return DataQualityDiagnostic(
            provider_id=provider_id,
            channel=channel,
            instrument_id=instrument_id,
            code=code,
            message=message,
            arrival_ordinal=arrival_ordinal,
        )

    def _request_snapshot_if_needed(self, event: MarketEvent) -> tuple[DataQualityDiagnostic, ...]:
        if event.event_type not in {"book_delta", "book_snapshot"} or event.book_state != "awaiting_snapshot":
            if event.event_type == "book_snapshot" and event.book_state == "valid":
                self._snapshot_requests_attempted.discard(
                    (event.provider_id, event.instrument_id, event.channel)
                )
            return ()

        diagnostic = self._submit_snapshot_request(
            event.provider_id,
            event.instrument_id,
            event.channel,
            event.arrival_ordinal,
        )
        return () if diagnostic is None else (diagnostic,)

    def ingest(self, frame: RawFrame) -> ProcessingResult:
        try:
            event = self.normalize(frame)
        except NormalizationError as exc:
            return self.reject(frame, exc)
        return self.validate(event)

    def receive_and_ingest(self, timeout: float | None = None) -> ProcessingResult | None:
        frame = self.adapter.receive(timeout=timeout)
        return None if frame is None else self.ingest(frame)

    def adapter_health(self) -> AdapterHealth:
        return self.adapter.health()

    def quality_health(self, now_monotonic_ns: int) -> tuple[StreamHealth, ...]:
        return self.quality.health_snapshot(now_monotonic_ns)

    def mark_reconnected(self, provider_id: str) -> None:
        self.quality.mark_reconnected(provider_id)
        self._snapshot_requests_attempted = {
            key for key in self._snapshot_requests_attempted if key[0] != provider_id
        }
