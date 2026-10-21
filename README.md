# Market catalog

This repository contains the Phase 1 market catalog, Phase 2 market-data
contracts, and a Phase 3 instrumented event-processing and simulation path. The
catalog is language-neutral JSON; Python loads and validates it. The project
includes a deterministic JSONL fixture adapter, canonical event normalization,
data-quality checks, bounded per-stage queues, an L1 simulator, and a basic
portfolio. Phase 4 adds a versioned, state-explicit strategy API with immutable
market and portfolio snapshots, per-instance seeds, and checked state restore.
Phase 5 adds a versioned L1 order/fill policy with internal cancels, virtual
arrival latency, explicit costs, and fill audit fields. No real provider has
been selected. Phase 6 adds an append-only, replayable portfolio ledger,
currency-aware P&L, explicit conversion rates, and separate exposure/margin
calculations. Phase 7 adds checksummed session journals, a bounded background
writer, integrity verification, and deterministic replay. The repository never
routes orders to a live venue.

## Validate the starter catalog

Install the package and its single runtime dependency, then run:

```console
python -m pip install -e .
python -m market_catalog validate data/market_catalog.json
```

The starter catalog is intentionally empty because no provider, venue, region,
or symbols have been selected. Add a real instrument only after its metadata and
provenance have been verified for the intended market.

## Run tests

```console
python -m unittest discover -s tests -v
```

See [Phase 1 market conventions](docs/phase1-market-conventions.md) for the
catalog contract and [Phase 2 adapters and quality](docs/phase2-adapters-and-quality.md)
for the feed contract, event schema, L1/L2 distinctions, and recovery policy.
See [Phase 3 event engine](docs/phase3-event-engine.md) for sequencing, queue
saturation, metrics, matching assumptions, and benchmark instructions.
See [Phase 4 strategy API](docs/phase4-strategy-api.md) for the managed strategy
contract, isolation boundary, and state snapshots.

See [Phase 5 order simulation](docs/phase5-order-simulation.md) for the order
state machine, cost and latency assumptions, fill audit contract, and synthetic
replay example:

```console
python examples/strategy_replay.py
```

See [Phase 6 portfolio accounting](docs/phase6-portfolio-accounting.md) for the
ledger format, replay API, conversion rules, and accounting limits.

See [Phase 7 recording and replay](docs/phase7-recording-replay.md) for the
session format, manifest, integrity checks, and replay runner.

See [Phase 8 quality and performance](docs/phase8-quality-performance.md) for the
test matrix, benchmark results, and performance baselines.

See [Phase 9 observability and deployment](docs/phase9-observability-deployment.md) for the
OpenTelemetry metrics catalog, structured logging, alert policies, recovery manual, and fail-closed deployment profile.

```console
python -m market_catalog validate-deployment config/deployment.profile.json
python -m market_catalog validate-alerts config/alerts.json
```

