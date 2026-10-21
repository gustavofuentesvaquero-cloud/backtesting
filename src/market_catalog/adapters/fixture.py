"""Deterministic JSONL fixture adapter used in place of an unselected provider."""

from __future__ import annotations

import json
from collections.abc import Sequence
from pathlib import Path
from typing import Any

from ..events import AdapterHealth, RawFrame, Subscription


class JsonLinesFixtureAdapter:
    """Replay source-neutral fixture envelopes as raw provider frames.

    Each line is an envelope containing receipt metadata and ``raw_frame`` as the
    exact UTF-8 message text exposed to the normalizer.
    """

    def __init__(self, path: str | Path, provider_id: str = "FIXTURE"):
        self._path = Path(path)
        self._provider_id = provider_id
        self._frames: list[RawFrame] = []
        self._subscriptions: set[tuple[str, str]] = set()
        self._cursor = 0
        self._connected = False
        self._closed = False
        self._received = 0
        self._last_received: int | None = None

    @property
    def provider_id(self) -> str:
        return self._provider_id

    def connect(self) -> None:
        if self._closed:
            raise RuntimeError("A closed fixture adapter cannot be reconnected.")
        if self._connected:
            return
        frames: list[RawFrame] = []
        previous_ordinal = -1
        try:
            lines = self._path.read_text(encoding="utf-8").splitlines()
        except OSError as exc:
            raise RuntimeError(f"Cannot read fixture {self._path}: {exc}") from exc

        for line_number, line in enumerate(lines, start=1):
            if not line.strip():
                continue
            try:
                envelope = json.loads(line)
                frame = self._frame_from_envelope(envelope, line_number)
            except (json.JSONDecodeError, TypeError, ValueError, KeyError) as exc:
                raise ValueError(f"Invalid fixture envelope at {self._path}:{line_number}: {exc}") from exc
            if frame.arrival_ordinal <= previous_ordinal:
                raise ValueError(f"arrival_ordinal must increase in fixture line {line_number}.")
            previous_ordinal = frame.arrival_ordinal
            frames.append(frame)

        self._frames = frames
        self._connected = True

    def _frame_from_envelope(self, envelope: Any, line_number: int) -> RawFrame:
        if not isinstance(envelope, dict):
            raise TypeError("envelope must be a JSON object")
        raw = envelope["raw_frame"]
        provider_id = envelope.get("provider_id", self._provider_id)
        channel = envelope["channel"]
        utc_ns = envelope["receive_time_utc_ns"]
        monotonic_ns = envelope["receive_time_monotonic_ns"]
        ordinal = envelope["arrival_ordinal"]
        if not isinstance(raw, str) or not raw:
            raise TypeError("raw_frame must be a non-empty string")
        if not isinstance(provider_id, str) or not provider_id:
            raise TypeError("provider_id must be a non-empty string")
        if not isinstance(channel, str) or not channel:
            raise TypeError("channel must be a non-empty string")
        for name, value in (("receive_time_utc_ns", utc_ns), ("receive_time_monotonic_ns", monotonic_ns), ("arrival_ordinal", ordinal)):
            if isinstance(value, bool) or not isinstance(value, int) or value < 0:
                raise TypeError(f"{name} must be a non-negative integer")
        try:
            source_message = json.loads(raw)
        except json.JSONDecodeError as exc:
            raise ValueError(f"raw_frame must contain JSON: {exc}") from exc
        if not isinstance(source_message, dict) or not isinstance(source_message.get("symbol"), str):
            raise TypeError("raw_frame JSON must contain a string symbol")
        return RawFrame(
            provider_id=provider_id,
            channel=channel,
            receive_time_utc_ns=utc_ns,
            receive_time_monotonic_ns=monotonic_ns,
            arrival_ordinal=ordinal,
            raw_payload=raw.encode("utf-8"),
        )

    def subscribe(self, subscriptions: Sequence[Subscription]) -> None:
        if not self._connected or self._closed:
            raise RuntimeError("connect() must succeed before subscribe().")
        self._subscriptions = {
            (subscription.provider_symbol, channel)
            for subscription in subscriptions
            for channel in subscription.channels
        }

    def receive(self, timeout: float | None = None) -> RawFrame | None:
        del timeout  # Fixture reads are in-memory and deterministic.
        if not self._connected or self._closed:
            raise RuntimeError("connect() must succeed before receive().")
        while self._cursor < len(self._frames):
            frame = self._frames[self._cursor]
            self._cursor += 1
            try:
                message = json.loads(frame.raw_payload)
            except json.JSONDecodeError:
                continue  # connect() already validated every fixture line.
            if (message["symbol"], frame.channel) not in self._subscriptions:
                continue
            self._received += 1
            self._last_received = frame.receive_time_monotonic_ns
            return frame
        return None

    def health(self) -> AdapterHealth:
        if self._closed:
            return AdapterHealth("closed", False, "adapter closed", self._received, self._last_received)
        if not self._connected:
            return AdapterHealth("disconnected", False, "not connected", self._received, self._last_received)
        detail = "fixture exhausted" if self._cursor >= len(self._frames) else "fixture ready"
        return AdapterHealth("connected", True, detail, self._received, self._last_received)

    def close(self) -> None:
        self._closed = True
        self._connected = False
