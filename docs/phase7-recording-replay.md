# Phase 7: session recording and replay

## Session format

A session is a directory containing `manifest.json`, `manifest.sha256`, and zero or more `segment-NNNNNN.jsonl` files. JSONL is the canonical event journal for this phase. Each line has a versioned record schema, a monotonically increasing record ordinal, the market `arrival_ordinal` when applicable, an event timestamp, a record kind, and its payload. Financial decimals are serialized as exact base-10 strings; raw frame bytes are Base64 encoded.

Segments rotate after the configured record or byte limit. Their SHA-256 digest, byte length, and record count are listed in the manifest. The manifest also records the catalog and its hash, event and ledger schema versions, engine and optional user-code hashes, Python and `jsonschema` runtime versions, strategy identity/configuration/initial state/seed, queue and timing configuration, matching and portfolio policies, feed/channel pairs, clock policy, and engine status. `manifest.sha256` protects the complete manifest.

The recorder uses its own bounded queue and background writer. Queue capacity, high watermark, maximum queue wait, maximum writer service time, writes, drops, segments, and writer errors are reported. An optional `max_queue_wait_ns` budget, writer error, or queue overflow marks the recording `incomplete`, stops event intake, and prevents replay. The engine report exposes recording status separately from its processing status. No incomplete capture is treated as a valid replay.

Record kinds include received raw frames, normalized and quality-assessed events, L1 book state, strategy decisions, accepted/rejected intent validation, order and cancellation state changes, fills, ledger entries, strategy state at close, and diagnostics. A session with an aborted event pipeline is retained for diagnosis but the manifest marks it non-replayable.

Sessions using the legacy `on_event` callback are not marked replayable because that interface has no serializable state or recorded seed contract. Use managed `StrategyInstance` strategies for deterministic replay.

## Record a session

Attach `SessionRecorder` to `EventProcessor`. The recorder is configured from the processor before intake starts; file writes and JSON encoding happen on its writer thread.

```python
from market_catalog import EventProcessor, SessionRecorder

recorder = SessionRecorder(
    "recordings/session-001",
    recording_mode="historical_replay",
    clock_policy="event_time",
    queue_capacity=8192,
    max_queue_wait_ns=50_000_000,
    segment_max_records=25_000,
    segment_max_bytes=16_000_000,
    seed=7,
)
processor = EventProcessor(pipeline, catalog, strategies, recorder=recorder)
processor.start()
# Submit RawFrame values as usual.
report = processor.finish()
if report.recording is None or not report.recording.complete:
    raise RuntimeError("The session recording is incomplete")
```

The optional user-code hash should cover strategy code outside this repository. The engine code hash covers the Python package and JSON schemas. Hashes identify inputs; they do not prove that third-party code has no external side effects.

## Verify and replay

`python -m market_catalog verify-session recordings/session-001` verifies the manifest, schemas, checksums, segment sizes/counts, and record ordering. `SessionRecording` performs the same checks before it yields any input frame.

Build a fresh processor from the recorded catalog, matching/portfolio policies, strategy configuration, initial state, and seed. For replay set `preserve_arrival_ordinals=True` and call:

```python
from market_catalog import replay_session

result = replay_session("recordings/session-001", replay_processor)
if not result.comparison.matched:
    divergence = result.comparison.first_divergence
    print(divergence.arrival_ordinal, divergence.kind, divergence.detail)
```

The runner refuses an incomplete/non-replayable recording or a processor whose catalog, policy, queue configuration, strategy configuration, initial state, or seed differs. It restores raw frames and original receipt timestamps in increasing arrival order, advances one event through portfolio commit at a time, and compares canonical event, decision, order, fill, ledger, and strategy-state outcomes. Worker-thread interleaving is normalized by ordinal and record kind. Timing-budget diagnostics are not compared because real queue and callback latency varies between runs; the captured values remain in the original session.

`clock_mode="event_time"` advances from captured normalized event time, falling back to captured receipt time. `clock_mode="step"` commits one captured frame per step; the optional `step_callback(frame, index, virtual_time_ns)` can gate each step. The original event and receipt timestamps remain part of the input and are retained for matching. Replaying a `live_simulation` capture replays its recorded input sequence, receipt metadata, decisions, and outcomes; it does not recreate the external market conditions or timing that produced that sequence.

Snapshot-recovery outcomes are read from the journal during replay so replay does not send snapshot requests to a live adapter. Strategy code should still keep side effects outside `on_market`; only intents returned to the engine are part of the deterministic contract.

## Storage choices and limits

JSONL is used for the auditable event journal, not as a claim that serialization belongs in the low-latency event path. The writer queue moves serialization and storage out of that path. Arrow can be added for in-memory analytical batch exchange, and Parquet for archival/analysis, without replacing the canonical journal or requiring per-event use of either format. The model still cannot recreate unrecorded provider state, hidden liquidity, external effects, or wall-clock latency.
