"""Provider-independent adapter contracts."""

from __future__ import annotations

from collections.abc import Sequence
from typing import Protocol

from ..events import AdapterHealth, MarketEvent, RawFrame, Subscription


class FeedAdapter(Protocol):
    """Single-consumer source interface; implementations wrap provider SDKs."""

    def connect(self) -> None:
        """Open the source connection and raise on an unrecoverable failure."""

    def subscribe(self, subscriptions: Sequence[Subscription]) -> None:
        """Subscribe using the provider symbols and channels in each item."""

    def receive(self, timeout: float | None = None) -> RawFrame | None:
        """Return one raw frame, or ``None`` when the timeout expires."""

    def health(self) -> AdapterHealth:
        """Return cached connection health without blocking on network I/O."""

    def close(self) -> None:
        """Release source resources; calling close more than once is safe."""


class EventNormalizer(Protocol):
    """Translate one source frame into the canonical event contract."""

    def normalize(self, frame: RawFrame) -> MarketEvent:
        ...


class SnapshotRecoveryAdapter(Protocol):
    """Optional adapter capability for feeds that support book resync requests."""

    def request_snapshot(self, instrument_id: str, channel: str) -> bool:
        """Request a full book snapshot; true means queued, not yet received."""
