"""Bounded, ordered event processing from raw frames through portfolio updates."""

from __future__ import annotations

from collections import deque
from dataclasses import dataclass, field, replace
import json
from math import isqrt
from threading import Condition, Lock, Thread
from time import perf_counter_ns
from types import MappingProxyType
from typing import Any, Mapping

from .events import MarketEvent, RawFrame
from .models import Catalog
from .normalization import NormalizationError
from .pipeline import MarketDataPipeline
from .recording import RecordingSummary
from .simulation import (
    BookBuilder,
    CancellationOutcome,
    CancelOrderIntent,
    Fill,
    IntentValidator,
    L1Matcher,
    MatchingPolicy,
    LedgerEntry,
    OrderIntent,
    OrderOutcome,
    OrderValidationError,
    Portfolio,
    PortfolioPolicy,
    PortfolioSnapshot,
    Strategy,
    TopOfBook,
)
from .strategies import StrategyInstance, StrategyRunRecord


STAGE_NAMES = (
    "normalize",
    "validate_sequence",
    "book",
    "strategy",
    "validate_intent",
    "matching",
    "portfolio",
)
_INFO_QUALITY_FLAGS = {"timestamp_unverified", "sequence_unavailable"}
_STOP = object()


@dataclass(frozen=True)
class EventProcessorConfig:
    """Queue limits and optional timing budgets for one processing session."""

    queue_capacity: int = 1024
    queue_capacities: Mapping[str, int] = field(default_factory=dict)
    queue_wait_budgets_ns: Mapping[str, int] = field(default_factory=dict)
    stage_latency_budgets_ns: Mapping[str, int] = field(default_factory=dict)
    preserve_arrival_ordinals: bool = False
    matching_policy: MatchingPolicy = field(default_factory=MatchingPolicy)
    portfolio_policy: PortfolioPolicy = field(default_factory=PortfolioPolicy)

    def __post_init__(self) -> None:
        if isinstance(self.queue_capacity, bool) or not isinstance(self.queue_capacity, int) or self.queue_capacity <= 0:
            raise ValueError("queue_capacity must be a positive integer.")
        if not isinstance(self.matching_policy, MatchingPolicy):
            raise TypeError("matching_policy must be a MatchingPolicy.")
        if not isinstance(self.portfolio_policy, PortfolioPolicy):
            raise TypeError("portfolio_policy must be a PortfolioPolicy.")
        valid_queue_names = set(STAGE_NAMES)
        for name, capacity in self.queue_capacities.items():
            if name not in valid_queue_names:
                raise ValueError(f"Unknown queue name {name!r}.")
            if isinstance(capacity, bool) or not isinstance(capacity, int) or capacity <= 0:
                raise ValueError(f"Queue capacity for {name} must be a positive integer.")
        for name, budget in self.queue_wait_budgets_ns.items():
            if name not in valid_queue_names:
                raise ValueError(f"Unknown queue wait budget name {name!r}.")
            _positive_budget(name, budget)
        for name, budget in self.stage_latency_budgets_ns.items():
            if name not in {*STAGE_NAMES, "strategy_callback"}:
                raise ValueError(f"Unknown stage latency budget name {name!r}.")
            _positive_budget(name, budget)
        object.__setattr__(self, "queue_capacities", MappingProxyType(dict(self.queue_capacities)))
        object.__setattr__(
            self, "queue_wait_budgets_ns", MappingProxyType(dict(self.queue_wait_budgets_ns))
        )
        object.__setattr__(
            self, "stage_latency_budgets_ns", MappingProxyType(dict(self.stage_latency_budgets_ns))
        )

    def capacity_for(self, queue_name: str) -> int:
        return self.queue_capacities.get(queue_name, self.queue_capacity)


def _positive_budget(name: str, value: int) -> None:
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        raise ValueError(f"Latency budget for {name} must be a positive integer.")


@dataclass(frozen=True)
class HistogramSnapshot:
    count: int
    total_ns: int
    max_ns: int
    mean_ns: int | None
    stddev_ns: int | None
    p50_upper_ns: int | None
    p90_upper_ns: int | None
    p95_upper_ns: int | None
    p99_upper_ns: int | None
    p999_upper_ns: int | None


class _Histogram:
    """Fixed-memory log2 histogram; percentile values are upper bucket bounds."""

    __slots__ = ("_buckets", "count", "total_ns", "sum_squares_ns", "max_ns")

    def __init__(self) -> None:
        self._buckets = [0] * 64
        self.count = 0
        self.total_ns = 0
        self.sum_squares_ns = 0
        self.max_ns = 0

    def record(self, value_ns: int) -> None:
        value = max(0, value_ns)
        index = 0 if value <= 1 else min(value.bit_length() - 1, 63)
        self._buckets[index] += 1
        self.count += 1
        self.total_ns += value
        self.sum_squares_ns += value * value
        if value > self.max_ns:
            self.max_ns = value

    def _percentile_upper(self, numerator: int, denominator: int) -> int | None:
        if self.count == 0:
            return None
        target = max(1, (self.count * numerator + denominator - 1) // denominator)
        seen = 0
        for index, bucket_count in enumerate(self._buckets):
            seen += bucket_count
            if seen >= target:
                bucket_upper = 1 if index == 0 else (1 << (index + 1)) - 1
                return min(bucket_upper, self.max_ns)
        return self.max_ns

    def snapshot(self) -> HistogramSnapshot:
        if self.count:
            mean_ns = self.total_ns // self.count
            variance_numerator = max(
                0,
                self.sum_squares_ns * self.count - self.total_ns * self.total_ns,
            )
            stddev_ns = isqrt(variance_numerator) // self.count
        else:
            mean_ns = None
            stddev_ns = None
        return HistogramSnapshot(
            count=self.count,
            total_ns=self.total_ns,
            max_ns=self.max_ns,
            mean_ns=mean_ns,
            stddev_ns=stddev_ns,
            p50_upper_ns=self._percentile_upper(50, 100),
            p90_upper_ns=self._percentile_upper(90, 100),
            p95_upper_ns=self._percentile_upper(95, 100),
            p99_upper_ns=self._percentile_upper(99, 100),
            p999_upper_ns=self._percentile_upper(999, 1000),
        )


@dataclass(frozen=True)
class QueueMetricsSnapshot:
    capacity: int
    enqueued: int
    rejected: int
    pending: int
    high_watermark: int
    wait: HistogramSnapshot


@dataclass(frozen=True)
class EngineMetricsSnapshot:
    ingress: HistogramSnapshot
    stages: Mapping[str, HistogramSnapshot]
    strategy_callback: HistogramSnapshot
    end_to_end: HistogramSnapshot
    queues: Mapping[str, QueueMetricsSnapshot]
    processed_events: int


@dataclass(frozen=True)
class SessionDiagnostic:
    code: str
    stage: str
    severity: str
    message: str
    count: int = 1
    arrival_ordinals: tuple[int, ...] = ()


@dataclass(frozen=True)
class SessionReport:
    status: str
    received_frames: int
    accepted_frames: int
    rejected_frames: int
    processed_events: int
    pending_by_queue: Mapping[str, int]
    in_flight_by_stage: Mapping[str, int]
    unprocessed_by_stage: Mapping[str, int]
    fills: tuple[Fill, ...]
    orders: tuple[OrderOutcome, ...]
    portfolio: PortfolioSnapshot
    metrics: EngineMetricsSnapshot
    diagnostics: tuple[SessionDiagnostic, ...]
    strategy_runs: tuple[StrategyRunRecord, ...] = ()
    session_id: str = "session"
    cancellations: tuple[CancellationOutcome, ...] = ()
    matching_policy_json: str = "{}"
    ledger: tuple[LedgerEntry, ...] = ()
    portfolio_policy_json: str = "{}"
    recording: RecordingSummary | None = None

    @property
    def valid(self) -> bool:
        return self.status == "completed" and (self.recording is None or self.recording.complete)


@dataclass(slots=True)
class _EventContext:
    frame: RawFrame
    session_index: int
    submitted_ns: int
    enqueued_ns: int
    normalization_error: NormalizationError | None = None
    invalidate_l1_instrument: str | None = None
    event: MarketEvent | None = None
    book: TopOfBook | None = None
    intents: list[Any] | None = None
    valid_intents: list[OrderIntent] | None = None
    valid_cancellations: list[CancelOrderIntent] | None = None
    rejected_orders: list[tuple[OrderIntent, str]] | None = None
    rejected_cancellations: list[tuple[CancelOrderIntent, str]] | None = None
    decision_timestamp_ns: int | None = None
    fills: tuple[Fill, ...] = ()


class _ChannelAborted(Exception):
    pass


class _BoundedChannel:
    """Small condition-backed FIFO with exact occupancy and a hard capacity."""

    def __init__(self, capacity: int):
        self.capacity = capacity
        self._items: deque[Any] = deque()
        self._condition = Condition()
        self._aborted = False
        self._accepted = 0
        self._rejected = 0
        self._high_watermark = 0

    def put_nowait(self, item: Any) -> str:
        with self._condition:
            if self._aborted:
                return "aborted"
            if len(self._items) >= self.capacity:
                self._rejected += 1
                return "full"
            self._items.append(item)
            self._accepted += 1
            if len(self._items) > self._high_watermark:
                self._high_watermark = len(self._items)
            self._condition.notify()
            return "accepted"

    def put_control(self, item: Any) -> bool:
        """Enqueue an end marker, waiting for space during orderly shutdown."""

        with self._condition:
            while len(self._items) >= self.capacity and not self._aborted:
                self._condition.wait()
            if self._aborted:
                return False
            self._items.append(item)
            if len(self._items) > self._high_watermark:
                self._high_watermark = len(self._items)
            self._condition.notify()
            return True

    def get(self) -> Any:
        with self._condition:
            while not self._items and not self._aborted:
                self._condition.wait()
            if self._aborted:
                raise _ChannelAborted
            item = self._items.popleft()
            self._condition.notify_all()
            return item

    def abort(self) -> None:
        with self._condition:
            self._aborted = True
            self._condition.notify_all()

    def snapshot(self) -> tuple[int, int, int, int]:
        with self._condition:
            return len(self._items), self._high_watermark, self._accepted, self._rejected


class EventProcessor:
    """One deterministic ordered session, with one worker for each pipeline stage.

    Producers call :meth:`submit` after their adapter returns a ``RawFrame``.
    Concurrent producers share the same ordinal sequencer and ingress queue.
    """

    def __init__(
        self,
        pipeline: MarketDataPipeline,
        catalog: Catalog,
        strategy: Strategy | StrategyInstance | tuple[StrategyInstance, ...] | list[StrategyInstance],
        config: EventProcessorConfig | None = None,
        *,
        session_id: str = "session",
        recorder: Any | None = None,
    ):
        if not isinstance(session_id, str) or not session_id.strip():
            raise ValueError("session_id must be a non-empty string.")
        self.pipeline = pipeline
        self.catalog = catalog
        self.strategy = strategy
        self.session_id = session_id
        self.recorder = recorder
        if isinstance(strategy, StrategyInstance):
            managed = (strategy,)
        elif isinstance(strategy, (tuple, list)):
            managed = tuple(strategy)
            if not managed or any(not isinstance(item, StrategyInstance) for item in managed):
                raise TypeError("A strategy sequence must contain StrategyInstance values.")
        else:
            managed = ()
        instance_ids = [item.config.instance_id for item in managed]
        if len(set(instance_ids)) != len(instance_ids):
            raise ValueError("Strategy instance_id values must be unique within a session.")
        if len({id(item.strategy) for item in managed}) != len(managed):
            raise ValueError("Each strategy instance must own a distinct strategy object.")
        self._managed_strategies = managed
        self._managed_strategies_closed = False
        self.config = config or EventProcessorConfig()
        self._channels = {
            stage: _BoundedChannel(self.config.capacity_for(stage)) for stage in STAGE_NAMES
        }
        self._stage_histograms = {stage: _Histogram() for stage in STAGE_NAMES}
        self._queue_wait_histograms = {stage: _Histogram() for stage in STAGE_NAMES}
        self._callback_histogram = _Histogram()
        self._end_to_end_histogram = _Histogram()
        self._ingress_histogram = _Histogram()
        self._book_builder = BookBuilder()
        self._intent_validator = IntentValidator(catalog)
        self._matcher = L1Matcher(catalog, self.config.matching_policy)
        self._portfolio = Portfolio(catalog, self.config.portfolio_policy)
        self._threads: list[Thread] = []
        self._ingress_lock = Lock()
        self._state_lock = Lock()
        self._portfolio_condition = Condition(self._state_lock)
        self._next_ordinal = 0
        self._ordinal_to_session_index: dict[int, int] = {}
        self._submitted_session_events = 0
        self._completed_session_index = -1
        self._last_preserved_ordinal: int | None = None
        self._started = False
        self._finished = False
        self._accepting = False
        self._aborted = False
        self._invalid = False
        self._received_frames = 0
        self._accepted_frames = 0
        self._rejected_frames = 0
        self._processed_events = 0
        self._active_by_stage = {stage: 0 for stage in STAGE_NAMES}
        self._unprocessed_by_stage = {stage: 0 for stage in STAGE_NAMES}
        self._fills: list[Fill] = []
        self._diagnostics: dict[tuple[str, str, str], dict[str, Any]] = {}
        self._recording_failed = False
        self._last_recorded_order_states: dict[str, OrderOutcome] = {}
        self._last_recorded_cancellations: dict[str, CancellationOutcome] = {}

    def start(self) -> None:
        with self._ingress_lock:
            if self._started:
                return
            if self.recorder is not None:
                self.recorder.configure(self)
                self.recorder.start()
            for instance in self._managed_strategies:
                instance.start()
            with self._state_lock:
                self._started = True
                self._accepting = True
            # Start downstream first so the initial producer never races an
            # unstarted consumer.
            for index in reversed(range(len(STAGE_NAMES))):
                stage = STAGE_NAMES[index]
                source = self._channels[stage]
                target = self._channels[STAGE_NAMES[index + 1]] if index + 1 < len(STAGE_NAMES) else None
                thread = Thread(
                    target=self._worker,
                    args=(stage, source, target),
                    name=f"market-event-{stage}",
                    daemon=False,
                )
                self._threads.append(thread)
                thread.start()

    def submit(self, frame: RawFrame) -> int | None:
        """Sequence and enqueue a frame; return its session ordinal or ``None`` on overflow."""

        if not self._started:
            self.start()
        with self._ingress_lock:
            if not self._accepting:
                raise RuntimeError("This event session is no longer accepting frames.")
            self._received_frames += 1
            if self.config.preserve_arrival_ordinals:
                ordinal = frame.arrival_ordinal
                if (
                    isinstance(ordinal, bool)
                    or not isinstance(ordinal, int)
                    or ordinal < 0
                    or (
                        self._last_preserved_ordinal is not None
                        and ordinal <= self._last_preserved_ordinal
                    )
                ):
                    self._rejected_frames += 1
                    self._record_diagnostic(
                        code="arrival_ordinal_invalid",
                        stage="receive",
                        severity="error",
                        message="Preserved arrival_ordinal values must strictly increase across the session.",
                        arrival_ordinal=ordinal,
                        invalidate=True,
                    )
                    self._abort("arrival_ordinal_invalid", "receive", ordinal, "non-increasing preserved ordinal")
                    return None
                self._last_preserved_ordinal = ordinal
            else:
                ordinal = self._next_ordinal
                self._next_ordinal += 1
                frame = replace(frame, arrival_ordinal=ordinal)

            if not self._capture("frame_received", ordinal, frame):
                self._rejected_frames += 1
                return None

            now = perf_counter_ns()
            context = _EventContext(
                frame=frame,
                session_index=self._submitted_session_events,
                submitted_ns=now,
                enqueued_ns=now,
            )
            enqueue_result = self._channels["normalize"].put_nowait(context)
            self._ingress_histogram.record(perf_counter_ns() - now)
            if enqueue_result != "accepted":
                self._rejected_frames += 1
                if enqueue_result == "full":
                    self._abort(
                        "queue_overflow",
                        "normalize",
                        ordinal,
                        "ingress queue is full; frame was rejected and the session was stopped",
                    )
                else:
                    self._record_diagnostic(
                        code="input_rejected_after_abort",
                        stage="receive",
                        severity="error",
                        message="The session aborted before this received frame could enter the queue.",
                        arrival_ordinal=ordinal,
                        invalidate=True,
                    )
                return None
            self._accepted_frames += 1
            self._submitted_session_events += 1
            self._ordinal_to_session_index[ordinal] = context.session_index
            return ordinal

    def apply_ledger_entry(self, entry: LedgerEntry, timeout: float | None = 30.0) -> PortfolioSnapshot:
        """Insert an external accounting fact between fully committed market frames.

        The caller controls the event timestamp, source, identifier, and ordering.
        While this operation waits, ingress is held so a later frame cannot pass it.
        """
        if not isinstance(entry, LedgerEntry):
            raise TypeError("entry must be a LedgerEntry.")
        if not self._started:
            self.start()
        if entry.kind == "checkpoint":
            raise ValueError("Checkpoint entries are managed by the event processor and cannot be injected externally.")
        deadline = None if timeout is None else perf_counter_ns() + int(timeout * 1_000_000_000)
        with self._ingress_lock:
            with self._portfolio_condition:
                target_index = self._submitted_session_events - 1
                while self._completed_session_index < target_index and not self._aborted:
                    remaining = None if deadline is None else (deadline - perf_counter_ns()) / 1_000_000_000
                    if remaining is not None and remaining <= 0:
                        raise TimeoutError("Timed out waiting for previously submitted market events to commit.")
                    self._portfolio_condition.wait(remaining)
                if self._aborted:
                    raise RuntimeError("Cannot append a ledger event to an aborted session.")
                self._portfolio.apply_ledger_entry(entry)
                self._capture("ledger_entry", entry.arrival_ordinal, entry, event_time_ns=entry.timestamp_ns)
                return self._portfolio.snapshot()

    def finish(self, timeout: float | None = 30.0) -> SessionReport:
        """Stop input, drain accepted work, and return the bounded session report."""

        if self._finished:
            return self.report()
        if not self._started:
            self.start()
        with self._ingress_lock:
            if self._accepting:
                self._accepting = False
                self._channels["normalize"].put_control(_STOP)

        deadline = None if timeout is None else perf_counter_ns() + int(timeout * 1_000_000_000)
        for thread in self._threads:
            remaining = None if deadline is None else max(0, (deadline - perf_counter_ns()) / 1_000_000_000)
            thread.join(remaining)
        if any(thread.is_alive() for thread in self._threads):
            self._abort("shutdown_timeout", "finish", None, "workers did not drain before timeout")
            for thread in self._threads:
                thread.join(1.0)
        if not any(thread.is_alive() for thread in self._threads):
            self._matcher.finish()
            self._capture_changed_orders(None)
            self._close_managed_strategies()
        if self.recorder is not None:
            for instance in self._managed_strategies:
                self._capture(
                    "strategy_run",
                    None,
                    instance.run_record(),
                )
            current = self._engine_status()
            self.recorder.close(engine_status=current, timeout=timeout or 30.0)
        self._finished = True
        return self.report()

    def wait_for_ordinal(self, arrival_ordinal: int, *, timeout: float | None = 30.0) -> bool:
        """Wait until one accepted event has committed through portfolio stage."""

        if isinstance(arrival_ordinal, bool) or not isinstance(arrival_ordinal, int) or arrival_ordinal < 0:
            raise ValueError("arrival_ordinal must be a non-negative integer.")
        with self._portfolio_condition:
            target = self._ordinal_to_session_index.get(arrival_ordinal)
            if target is None:
                return False
            deadline = None if timeout is None else perf_counter_ns() + int(timeout * 1_000_000_000)
            while self._completed_session_index < target and not self._aborted:
                remaining = None if deadline is None else (deadline - perf_counter_ns()) / 1_000_000_000
                if remaining is not None and remaining <= 0:
                    return False
                self._portfolio_condition.wait(remaining)
            return self._completed_session_index >= target

    def _engine_status(self) -> str:
        with self._state_lock:
            if self._aborted:
                return "aborted"
            if self._invalid:
                return "invalid"
            return "completed"

    def _capture(
        self,
        kind: str,
        arrival_ordinal: int | None,
        payload: Any,
        *,
        event_time_ns: int | None = None,
    ) -> bool:
        if self.recorder is None:
            return True
        try:
            accepted = self.recorder.record(
                kind,
                arrival_ordinal,
                payload,
                event_time_ns=event_time_ns,
            )
        except Exception as exc:
            accepted = False
            recorder_error = f"{type(exc).__name__}: {exc}"
        else:
            recorder_error = "bounded recorder queue overflow or writer failure"
        if accepted:
            return True
        with self._state_lock:
            if self._recording_failed:
                return False
            self._recording_failed = True
            self._invalid = True
            self._aborted = True
            self._accepting = False
            key = ("recording_incomplete", "recording", "error")
            self._diagnostics[key] = {
                "message": f"Session recording became incomplete: {recorder_error}.",
                "count": 1,
                "ordinals": [] if arrival_ordinal is None else [arrival_ordinal],
            }
            self._portfolio_condition.notify_all()
        for channel in self._channels.values():
            channel.abort()
        return False

    def _capture_changed_orders(self, arrival_ordinal: int | None) -> None:
        if self.recorder is None:
            return
        for outcome in self._matcher.snapshot():
            if self._last_recorded_order_states.get(outcome.order_id) == outcome:
                continue
            self._last_recorded_order_states[outcome.order_id] = outcome
            self._capture("order_state", arrival_ordinal, outcome)
        for outcome in self._matcher.cancellations:
            key = outcome.request_id
            if self._last_recorded_cancellations.get(key) == outcome:
                continue
            self._last_recorded_cancellations[key] = outcome
            self._capture("cancellation_state", arrival_ordinal, outcome)

    def _close_managed_strategies(self) -> None:
        if self._managed_strategies_closed:
            return
        self._managed_strategies_closed = True
        for instance in self._managed_strategies:
            try:
                instance.close()
            except Exception as exc:
                self._record_diagnostic(
                    code="strategy_close_failed",
                    stage="strategy",
                    severity="error",
                    message=f"{type(exc).__name__}: {exc}",
                    arrival_ordinal=None,
                    invalidate=True,
                )

    def report(self) -> SessionReport:
        with self._state_lock:
            if self._aborted:
                engine_status = "aborted"
            elif self._invalid:
                engine_status = "invalid"
            else:
                engine_status = "completed"
            received = self._received_frames
            accepted = self._accepted_frames
            processed = self._processed_events
            active = MappingProxyType(dict(self._active_by_stage))
            unprocessed = MappingProxyType(dict(self._unprocessed_by_stage))
            diagnostics = tuple(
                SessionDiagnostic(
                    code=key[0],
                    stage=key[1],
                    severity=key[2],
                    message=value["message"],
                    count=value["count"],
                    arrival_ordinals=tuple(value["ordinals"]),
                )
                for key, value in self._diagnostics.items()
            )
            fills = tuple(self._fills)
            rejected_frames = self._rejected_frames
        queue_snapshots: dict[str, QueueMetricsSnapshot] = {}
        pending: dict[str, int] = {}
        rejected = 0
        for name, channel in self._channels.items():
            queued, high_water, enqueued, queue_rejected = channel.snapshot()
            pending[name] = queued
            rejected += queue_rejected
            queue_snapshots[name] = QueueMetricsSnapshot(
                capacity=channel.capacity,
                enqueued=enqueued,
                rejected=queue_rejected,
                pending=queued,
                high_watermark=high_water,
                wait=self._queue_wait_histograms[name].snapshot(),
            )
        metrics = EngineMetricsSnapshot(
            ingress=self._ingress_histogram.snapshot(),
            stages=MappingProxyType(
                {name: histogram.snapshot() for name, histogram in self._stage_histograms.items()}
            ),
            strategy_callback=self._callback_histogram.snapshot(),
            end_to_end=self._end_to_end_histogram.snapshot(),
            queues=MappingProxyType(queue_snapshots),
            processed_events=processed,
        )
        recording = None if self.recorder is None else self.recorder.summary()
        status = "incomplete" if recording is not None and not recording.complete and recording.status != "recording" else engine_status
        return SessionReport(
            status=status,
            received_frames=received,
            accepted_frames=accepted,
            rejected_frames=max(rejected, rejected_frames),
            processed_events=processed,
            pending_by_queue=MappingProxyType(pending),
            in_flight_by_stage=active,
            unprocessed_by_stage=unprocessed,
            fills=fills,
            orders=self._matcher.snapshot(),
            portfolio=self._portfolio.snapshot(),
            metrics=metrics,
            diagnostics=diagnostics,
            strategy_runs=tuple(instance.run_record() for instance in self._managed_strategies),
            session_id=self.session_id,
            cancellations=self._matcher.cancellations,
            ledger=self._portfolio.ledger,
            portfolio_policy_json=json.dumps(
                self.config.portfolio_policy.to_mapping(),
                sort_keys=True,
                separators=(",", ":"),
                allow_nan=False,
            ),
            matching_policy_json=json.dumps(
                self.config.matching_policy.to_mapping(),
                sort_keys=True,
                separators=(",", ":"),
                allow_nan=False,
            ),
            recording=recording,
        )

    def _worker(self, stage: str, source: _BoundedChannel, target: _BoundedChannel | None) -> None:
        while True:
            try:
                item = source.get()
            except _ChannelAborted:
                return
            if item is _STOP:
                if target is not None:
                    target.put_control(_STOP)
                return
            if not isinstance(item, _EventContext):
                self._abort("invalid_work_item", stage, None, "internal queue contained an invalid work item")
                return
            with self._state_lock:
                self._active_by_stage[stage] += 1
                stopped_before_start = self._aborted
            if stopped_before_start:
                with self._state_lock:
                    self._active_by_stage[stage] -= 1
                    self._unprocessed_by_stage[stage] += 1
                    self._rejected_frames += 1
                self._record_diagnostic(
                    code="work_stopped_after_dequeue",
                    stage=stage,
                    severity="error",
                    message="The session aborted after this item left its queue; it was not processed.",
                    arrival_ordinal=item.frame.arrival_ordinal,
                    invalidate=True,
                )
                return
            try:
                queue_wait = max(0, perf_counter_ns() - item.enqueued_ns)
                self._queue_wait_histograms[stage].record(queue_wait)
                wait_budget = self.config.queue_wait_budgets_ns.get(stage)
                if wait_budget is not None and queue_wait > wait_budget:
                    self._record_diagnostic(
                        code="queue_wait_budget_exceeded",
                        stage=stage,
                        severity="error",
                        message=f"Queue wait {queue_wait} ns exceeded configured budget {wait_budget} ns.",
                        arrival_ordinal=item.frame.arrival_ordinal,
                        invalidate=True,
                    )

                started_ns = perf_counter_ns()
                try:
                    self._process(stage, item)
                finally:
                    elapsed = max(0, perf_counter_ns() - started_ns)
                    self._stage_histograms[stage].record(elapsed)
                stage_budget = self.config.stage_latency_budgets_ns.get(stage)
                if stage_budget is not None and elapsed > stage_budget:
                    self._record_diagnostic(
                        code="stage_latency_budget_exceeded",
                        stage=stage,
                        severity="error",
                        message=f"Stage latency {elapsed} ns exceeded configured budget {stage_budget} ns.",
                        arrival_ordinal=item.frame.arrival_ordinal,
                        invalidate=True,
                    )
                if target is not None:
                    item.enqueued_ns = perf_counter_ns()
                    enqueue_result = target.put_nowait(item)
                    if enqueue_result != "accepted":
                        with self._state_lock:
                            self._rejected_frames += 1
                        next_stage = STAGE_NAMES[STAGE_NAMES.index(stage) + 1]
                        if enqueue_result == "full":
                            self._abort(
                                "queue_overflow",
                                next_stage,
                                item.frame.arrival_ordinal,
                                f"queue after {stage} is full; session stopped",
                            )
                        else:
                            with self._state_lock:
                                self._unprocessed_by_stage[next_stage] += 1
                            self._record_diagnostic(
                                code="work_stopped_before_enqueue",
                                stage=next_stage,
                                severity="error",
                                message="The session aborted before this processed item could enter the next queue.",
                                arrival_ordinal=item.frame.arrival_ordinal,
                                invalidate=True,
                            )
                        return
            except Exception as exc:
                self._abort(
                    "stage_exception",
                    stage,
                    item.frame.arrival_ordinal,
                    f"{type(exc).__name__}: {exc}",
                )
                return
            finally:
                with self._state_lock:
                    self._active_by_stage[stage] -= 1

    def _process(self, stage: str, context: _EventContext) -> None:
        if stage == "normalize":
            try:
                context.event = self.pipeline.normalize(context.frame)
            except NormalizationError as exc:
                context.normalization_error = exc
                self._capture(
                    "normalization_rejected",
                    context.frame.arrival_ordinal,
                    {
                        "provider_id": context.frame.provider_id,
                        "channel": context.frame.channel,
                        "code": getattr(exc, "code", "normalization_error"),
                        "message": str(exc),
                    },
                    event_time_ns=context.frame.receive_time_utc_ns,
                )
            else:
                self._capture(
                    "normalized_event",
                    context.event.arrival_ordinal,
                    context.event,
                    event_time_ns=context.event.source_timestamp_ns,
                )
            return

        if stage == "validate_sequence":
            if context.normalization_error is not None:
                result = self.pipeline.reject(context.frame, context.normalization_error)
                if (
                    context.normalization_error.event_type == "quote"
                    and context.normalization_error.instrument_id is not None
                ):
                    context.invalidate_l1_instrument = context.normalization_error.instrument_id
            elif context.event is not None:
                result = self.pipeline.validate(context.event)
            else:
                raise RuntimeError("normalization stage produced neither event nor error")
            context.event = result.event
            self._capture(
                "validated_event",
                context.frame.arrival_ordinal,
                {
                    "event": context.event,
                    "diagnostics": result.diagnostics,
                },
                event_time_ns=(
                    context.event.source_timestamp_ns
                    if context.event is not None
                    else context.frame.receive_time_utc_ns
                ),
            )
            for diagnostic in result.diagnostics:
                is_data_error = diagnostic.code not in _INFO_QUALITY_FLAGS
                self._record_diagnostic(
                    code=diagnostic.code,
                    stage=stage,
                    severity="error" if is_data_error else "info",
                    message=diagnostic.message,
                    arrival_ordinal=diagnostic.arrival_ordinal,
                    invalidate=is_data_error or result.event is None,
                )
            if context.event is not None:
                if not context.event.decision_usable:
                    self._record_diagnostic(
                        code="event_not_decision_usable",
                        stage=stage,
                        severity="error",
                        message="Quality checks blocked this event from influencing strategy decisions.",
                        arrival_ordinal=context.event.arrival_ordinal,
                        invalidate=True,
                    )
            return

        if stage == "book":
            if context.invalidate_l1_instrument is not None:
                self._book_builder.invalidate(context.invalidate_l1_instrument)
                context.book = None
                self._capture(
                    "book_state",
                    context.frame.arrival_ordinal,
                    {"book": None, "diagnostic": "invalidated_after_normalization_error"},
                    event_time_ns=context.frame.receive_time_utc_ns,
                )
                return
            context.book = self._book_builder.update(context.event)
            self._capture(
                "book_state",
                None if context.event is None else context.event.arrival_ordinal,
                {"book": context.book, "diagnostic": self._book_builder.last_diagnostic},
                event_time_ns=(None if context.event is None else context.event.source_timestamp_ns),
            )
            if self._book_builder.last_diagnostic is not None:
                self._record_diagnostic(
                    code=self._book_builder.last_diagnostic,
                    stage=stage,
                    severity="error",
                    message="The L1 book is unavailable for this event; no strategy decision will use it.",
                    arrival_ordinal=None if context.event is None else context.event.arrival_ordinal,
                    invalidate=True,
                )
            return

        if stage == "strategy":
            event = context.event
            book = context.book
            if event is None or book is None or not event.decision_usable:
                self._capture(
                    "strategy_decision",
                    context.frame.arrival_ordinal,
                    {
                        "invoked": False,
                        "reason": "missing_event_or_usable_book",
                        "intents": (),
                    },
                    event_time_ns=context.frame.receive_time_utc_ns,
                )
                return
            context.decision_timestamp_ns = (
                event.source_timestamp_ns
                if (
                    isinstance(event.source_timestamp_ns, int)
                    and event.timestamp_semantics != "unknown"
                )
                else event.receive_time_utc_ns
            )
            if self._managed_strategies:
                with self._portfolio_condition:
                    while (
                        self._completed_session_index < context.session_index - 1
                        and not self._aborted
                    ):
                        self._portfolio_condition.wait()
                    if self._aborted:
                        return
                    portfolio = self._portfolio.snapshot()
            else:
                portfolio = None
            callback = getattr(self.strategy, "on_event", None)
            if not self._managed_strategies and not callable(callback):
                raise TypeError("legacy strategy must provide callable on_event(event, book)")
            callback_started = perf_counter_ns()
            try:
                if self._managed_strategies:
                    assert portfolio is not None
                    context.intents = []
                    for instance in self._managed_strategies:
                        context.intents.extend(
                            instance.on_market(
                                event,
                                book,
                                portfolio,
                                session_id=self.session_id,
                            )
                        )
                else:
                    outputs = callback(event, book)
                    if outputs is None:
                        context.intents = []
                    elif isinstance(outputs, (OrderIntent, CancelOrderIntent)):
                        context.intents = [outputs]
                    else:
                        context.intents = list(outputs)
            finally:
                callback_elapsed = max(0, perf_counter_ns() - callback_started)
                self._callback_histogram.record(callback_elapsed)
                callback_budget = self.config.stage_latency_budgets_ns.get("strategy_callback")
                if callback_budget is not None and callback_elapsed > callback_budget:
                    self._record_diagnostic(
                        code="strategy_callback_budget_exceeded",
                        stage="strategy",
                        severity="error",
                        message=f"Strategy callback latency {callback_elapsed} ns exceeded configured budget {callback_budget} ns.",
                        arrival_ordinal=event.arrival_ordinal,
                        invalidate=True,
                    )
            self._capture(
                "strategy_decision",
                event.arrival_ordinal,
                {"intents": context.intents or ()},
                event_time_ns=context.decision_timestamp_ns,
            )
            return

        if stage == "validate_intent":
            if context.event is None:
                self._capture(
                    "intent_validation",
                    context.frame.arrival_ordinal,
                    {"accepted_orders": (), "accepted_cancellations": (), "rejected_orders": (), "rejected_cancellations": ()},
                    event_time_ns=context.frame.receive_time_utc_ns,
                )
                return
            context.valid_intents = []
            context.valid_cancellations = []
            context.rejected_orders = []
            context.rejected_cancellations = []
            for intent in context.intents or ():
                try:
                    if isinstance(intent, OrderIntent):
                        self._intent_validator.validate(intent, context.event)
                        context.valid_intents.append(intent)
                    elif isinstance(intent, CancelOrderIntent):
                        self._intent_validator.validate_cancel(intent)
                        context.valid_cancellations.append(intent)
                    else:
                        raise OrderValidationError("strategy output must be an order or cancellation intent.")
                except (OrderValidationError, AttributeError, TypeError) as exc:
                    if isinstance(intent, OrderIntent):
                        context.rejected_orders.append((intent, str(exc)))
                    elif isinstance(intent, CancelOrderIntent):
                        context.rejected_cancellations.append((intent, str(exc)))
                    self._record_diagnostic(
                        code="intent_rejected",
                        stage=stage,
                        severity="error",
                        message=str(exc),
                        arrival_ordinal=context.event.arrival_ordinal,
                        invalidate=True,
                    )
            self._capture(
                "intent_validation",
                context.event.arrival_ordinal,
                {
                    "accepted_orders": context.valid_intents,
                    "accepted_cancellations": context.valid_cancellations,
                    "rejected_orders": context.rejected_orders,
                    "rejected_cancellations": context.rejected_cancellations,
                },
                event_time_ns=context.decision_timestamp_ns,
            )
            return

        if stage == "matching":
            context.fills = self._matcher.process(
                context.event,
                context.book,
                context.valid_intents if context.valid_intents is not None else (),
                context.valid_cancellations if context.valid_cancellations is not None else (),
                context.rejected_orders if context.rejected_orders is not None else (),
                context.rejected_cancellations if context.rejected_cancellations is not None else (),
                decision_timestamp_ns=context.decision_timestamp_ns,
            )
            self._capture_changed_orders(
                None if context.event is None else context.event.arrival_ordinal
            )
            for fill in context.fills:
                self._capture(
                    "fill",
                    fill.arrival_ordinal,
                    fill,
                    event_time_ns=fill.match_timestamp_ns,
                )
            return

        if stage == "portfolio":
            ledger_start = len(self._portfolio.ledger)
            for fill in context.fills:
                self._portfolio.apply(fill)
                with self._state_lock:
                    self._fills.append(fill)
            if (
                context.event is not None
                and context.event.event_type == "quote"
                and context.event.decision_usable
                and context.book is not None
            ):
                self._portfolio.mark_to_market(context.book, context.event)
            event_timestamp_ns = (
                context.event.source_timestamp_ns
                if context.event is not None
                and isinstance(context.event.source_timestamp_ns, int)
                and context.event.timestamp_semantics != "unknown"
                else context.frame.receive_time_utc_ns
            )
            self._portfolio.complete_event(context.frame.arrival_ordinal, event_timestamp_ns)
            for entry in self._portfolio.ledger[ledger_start:]:
                self._capture(
                    "ledger_entry",
                    entry.arrival_ordinal,
                    entry,
                    event_time_ns=entry.timestamp_ns,
                )
            self._end_to_end_histogram.record(max(0, perf_counter_ns() - context.submitted_ns))
            with self._portfolio_condition:
                self._processed_events += 1
                self._completed_session_index = context.session_index
                self._portfolio_condition.notify_all()
            return

        raise RuntimeError(f"Unknown processing stage {stage!r}")

    def _record_diagnostic(
        self,
        *,
        code: str,
        stage: str,
        severity: str,
        message: str,
        arrival_ordinal: int | None,
        invalidate: bool,
    ) -> None:
        key = (code, stage, severity)
        with self._state_lock:
            item = self._diagnostics.get(key)
            if item is None:
                item = {"message": message, "count": 0, "ordinals": []}
                self._diagnostics[key] = item
            item["count"] += 1
            if arrival_ordinal is not None and len(item["ordinals"]) < 8:
                item["ordinals"].append(arrival_ordinal)
            if invalidate:
                self._invalid = True
        self._capture(
            "session_diagnostic",
            arrival_ordinal,
            {"code": code, "stage": stage, "severity": severity, "message": message},
        )

    def _abort(self, code: str, stage: str, ordinal: int | None, message: str) -> None:
        self._record_diagnostic(
            code=code,
            stage=stage,
            severity="error",
            message=message,
            arrival_ordinal=ordinal,
            invalidate=True,
        )
        with self._state_lock:
            if self._aborted:
                return
            self._aborted = True
            self._accepting = False
            self._portfolio_condition.notify_all()
        for channel in self._channels.values():
            channel.abort()
