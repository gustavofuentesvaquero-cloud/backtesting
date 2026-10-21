"""Deterministic reconnect backoff policy for provider adapters."""

from __future__ import annotations

from dataclasses import dataclass
import math
import random


@dataclass(frozen=True)
class ReconnectPolicy:
    """Bounded exponential backoff; jitter is seeded for reproducible sessions."""

    initial_delay_seconds: float = 0.5
    maximum_delay_seconds: float = 30.0
    multiplier: float = 2.0
    jitter_fraction: float = 0.0

    def __post_init__(self) -> None:
        if any(not math.isfinite(value) for value in (
            self.initial_delay_seconds,
            self.maximum_delay_seconds,
            self.multiplier,
            self.jitter_fraction,
        )):
            raise ValueError("Reconnect policy values must be finite numbers.")
        if self.initial_delay_seconds <= 0:
            raise ValueError("initial_delay_seconds must be positive.")
        if self.maximum_delay_seconds < self.initial_delay_seconds:
            raise ValueError("maximum_delay_seconds must be at least initial_delay_seconds.")
        if self.multiplier < 1:
            raise ValueError("multiplier must be at least 1.")
        if not 0 <= self.jitter_fraction <= 1:
            raise ValueError("jitter_fraction must be between 0 and 1.")


class ReconnectBackoff:
    """Track retry delays and invalidate feed books when connectivity returns.

    This policy does not open sockets or sleep. The adapter loop consumes the
    returned delay, then calls ``mark_connected`` only after its handshake
    succeeds. That transition clears quality sequence state and leaves books
    unusable until a new complete quote or snapshot is assessed.
    """

    def __init__(self, policy: ReconnectPolicy | None = None, *, seed: int = 0):
        if isinstance(seed, bool) or not isinstance(seed, int) or seed < 0:
            raise ValueError("seed must be a non-negative integer.")
        self.policy = policy or ReconnectPolicy()
        self._random = random.Random(seed)
        self._attempt = 0

    @property
    def attempt(self) -> int:
        return self._attempt

    def next_delay_seconds(self) -> float:
        base = min(
            self.policy.maximum_delay_seconds,
            self.policy.initial_delay_seconds * self.policy.multiplier**self._attempt,
        )
        self._attempt += 1
        if self.policy.jitter_fraction == 0:
            return base
        spread = base * self.policy.jitter_fraction
        return min(
            self.policy.maximum_delay_seconds,
            max(0.0, self._random.uniform(base - spread, base + spread)),
        )

    def mark_connected(self, pipeline: object, provider_id: str) -> None:
        """Reset retries and tell the pipeline to require a fresh book state."""

        mark_reconnected = getattr(pipeline, "mark_reconnected", None)
        if not callable(mark_reconnected):
            raise TypeError("pipeline must provide mark_reconnected(provider_id).")
        mark_reconnected(provider_id)
        self._attempt = 0

    def reset(self) -> None:
        """Reset retry count after an adapter has fully recovered."""

        self._attempt = 0
