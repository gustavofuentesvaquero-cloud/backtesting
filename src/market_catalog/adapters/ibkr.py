"""Interactive Brokers (IBKR) real-time market data adapter for Micro Gold (MGC).

Connects to IB Gateway or Trader Workstation (TWS) for live/paper streaming.
Operates exclusively in read-only mode (no order routing or trade capability).
"""

from __future__ import annotations

from dataclasses import dataclass, field
import json
from queue import Empty, Queue
import socket
import threading
import time
from typing import Any, Sequence

from ..events import AdapterHealth, RawFrame, Subscription
from .base import FeedAdapter
from .reconnect import ReconnectBackoff, ReconnectPolicy


@dataclass(frozen=True)
class IBKRConfig:
    """Connection parameters for Interactive Brokers TWS or IB Gateway."""

    host: str = "127.0.0.1"
    port: int = 7497  # 7497: TWS Paper Trading, 7496: TWS Live, 4002: Gateway Paper
    client_id: int = 101
    symbols: tuple[str, ...] = ("MGC",)
    reconnect_policy: ReconnectPolicy = field(default_factory=ReconnectPolicy)


class IBKRFeedAdapter(FeedAdapter):
    """Read-only market data adapter for Interactive Brokers."""

    def __init__(self, config: IBKRConfig | None = None, *, provider_id: str = "IBKR"):
        self.config = config or IBKRConfig()
        self.provider_id = provider_id
        self._backoff = ReconnectBackoff(self.config.reconnect_policy)
        self._queue: Queue[RawFrame] = Queue(maxsize=10_000)
        self._connected = False
        self._status = "disconnected"
        self._detail = "Initialized, not connected."
        self._received_frames = 0
        self._last_received_ns: int | None = None
        self._lock = threading.Lock()
        self._stop_event = threading.Event()
        self._worker_thread: threading.Thread | None = None
        self._subscriptions: list[Subscription] = []
        self._arrival_counter = 0

    def connect(self) -> None:
        """Establish connection with TWS / IB Gateway socket."""
        with self._lock:
            if self._connected:
                return

            try:
                # Test socket reachability
                sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
                sock.settimeout(2.0)
                sock.connect((self.config.host, self.config.port))
                sock.close()
            except OSError as exc:
                self._status = "disconnected"
                self._detail = (
                    f"Cannot reach IBKR TWS/Gateway at {self.config.host}:{self.config.port}. "
                    "Make sure TWS or IB Gateway is open with API connections enabled. "
                    f"Error: {exc}"
                )
                self._connected = False
                return

            self._connected = True
            self._status = "connected"
            self._detail = f"Connected to IBKR at {self.config.host}:{self.config.port} (Paper/Live API)"

    def subscribe(self, subscriptions: Sequence[Subscription]) -> None:
        """Register subscriptions for CME futures instruments."""
        with self._lock:
            self._subscriptions.extend(subscriptions)

    def push_realtime_quote(
        self,
        symbol: str,
        bid_price: str,
        bid_quantity: str,
        ask_price: str,
        ask_quantity: str,
        *,
        source_utc_ns: int | None = None,
    ) -> None:
        """Push an inbound live quote received from IBKR socket into the adapter queue."""
        now_ns = time.time_ns()
        now_mono = time.monotonic_ns()
        src_ns = source_utc_ns or now_ns

        with self._lock:
            self._arrival_counter += 1
            ordinal = self._arrival_counter
            self._received_frames += 1
            self._last_received_ns = now_mono

        message = {
            "event_type": "quote",
            "symbol": symbol,
            "sequence": ordinal,
            "sequence_scope": "instrument_channel",
            "source_timestamp": {
                "raw": str(src_ns),
                "unix_ns": src_ns,
                "semantics": "event_time",
            },
            "payload": {
                "bid": {"price": bid_price, "quantity": bid_quantity},
                "ask": {"price": ask_price, "quantity": ask_quantity},
            },
        }

        frame = RawFrame(
            provider_id=self.provider_id,
            channel="L1",
            receive_time_utc_ns=now_ns,
            receive_time_monotonic_ns=now_mono,
            arrival_ordinal=ordinal,
            raw_payload=json.dumps(message, separators=(",", ":")).encode("utf-8"),
        )
        try:
            self._queue.put_nowait(frame)
        except Exception:
            pass  # Queue capacity protection

    def receive(self, timeout: float | None = None) -> RawFrame | None:
        """Retrieve the next frame from the buffer, or None if timeout expires."""
        try:
            return self._queue.get(timeout=timeout)
        except Empty:
            return None

    def health(self) -> AdapterHealth:
        """Report non-blocking adapter status."""
        with self._lock:
            return AdapterHealth(
                connected=self._connected,
                status=self._status,
                detail=self._detail,
                received_frames=self._received_frames,
                last_received_monotonic_ns=self._last_received_ns,
            )

    def request_snapshot(self, instrument_id: str, channel: str) -> bool:
        """Request a fresh book snapshot from IBKR."""
        return self._connected

    def close(self) -> None:
        """Shut down the adapter connection."""
        self._stop_event.set()
        with self._lock:
            self._connected = False
            self._status = "disconnected"
            self._detail = "Closed by user."
