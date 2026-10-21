"""Record a synthetic session, then replay it with first-divergence reporting."""

from __future__ import annotations

import argparse
from decimal import Decimal
from pathlib import Path
import sys
from tempfile import TemporaryDirectory

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from market_catalog import (  # noqa: E402
    CommissionModel,
    DataQualityMonitor,
    EventProcessor,
    EventProcessorConfig,
    MatchingPolicy,
    MarketDataPipeline,
    SessionRecorder,
    StrategyInstance,
    replay_session,
)
from market_catalog.normalization import JsonFixtureNormalizer  # noqa: E402
from strategy_replay import BuyOnceAtLimit, _NoopAdapter, _catalog, _config, _frame  # noqa: E402


def _processor(*, preserve_ordinals: bool, recorder=None) -> EventProcessor:
    catalog = _catalog()
    strategy = StrategyInstance(
        BuyOnceAtLimit(), _config(), seed=7, initial_state={"submitted": False}
    )
    pipeline = MarketDataPipeline(
        _NoopAdapter(), JsonFixtureNormalizer(catalog), DataQualityMonitor()
    )
    return EventProcessor(
        pipeline,
        catalog,
        strategy,
        config=EventProcessorConfig(
            preserve_arrival_ordinals=preserve_ordinals,
            matching_policy=MatchingPolicy(
                commission=CommissionModel(
                    per_unit=Decimal("0.01"), currency="USD", currency_precision=2
                )
            ),
        ),
        session_id="synthetic-phase7-session",
        recorder=recorder,
    )


def _run(recording_path: Path) -> dict[str, object]:
    recorder = SessionRecorder(
        recording_path,
        recording_mode="historical_replay",
        clock_policy="event_time",
        queue_capacity=128,
        segment_max_records=16,
        seed=7,
    )
    original = _processor(preserve_ordinals=False, recorder=recorder)
    for frame in (
        _frame(0, bid="99.75", ask="100.00", ask_quantity="1"),
        _frame(1, bid="99.50", ask="99.75", ask_quantity="2"),
    ):
        original.submit(frame)
    original_report = original.finish(timeout=5)
    if original_report.recording is None or not original_report.recording.complete:
        raise RuntimeError("Synthetic recording did not finish completely.")

    replay = replay_session(
        recording_path,
        _processor(preserve_ordinals=True),
        clock_mode="event_time",
        timeout=5,
    )
    if not replay.comparison.matched:
        divergence = replay.comparison.first_divergence
        raise RuntimeError(f"Synthetic replay diverged: {divergence}")
    return {
        "recording_status": original_report.recording.status,
        "recorded_frames": original_report.accepted_frames,
        "replayed_frames": replay.report.accepted_frames,
        "comparison": "matched",
        "fills": len(replay.report.fills),
        "ledger_entries": len(replay.report.ledger),
        "virtual_time_ns": replay.virtual_time_ns,
    }


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--output", type=Path, help="keep the session directory at this path")
    args = parser.parse_args()
    if args.output is not None:
        print(_run(args.output.resolve()))
    else:
        with TemporaryDirectory(prefix="market-session-") as temp_dir:
            print(_run(Path(temp_dir) / "session"))


if __name__ == "__main__":
    main()
