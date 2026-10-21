from __future__ import annotations

import hashlib
import json
from decimal import Decimal
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from market_catalog import (
    Catalog,
    DataQualityMonitor,
    EventProcessor,
    EventProcessorConfig,
    MarketDataPipeline,
    OrderIntent,
    RawFrame,
    RecordingError,
    SessionRecorder,
    SessionRecording,
    StrategyConfig,
    StrategyDecision,
    StrategyInstance,
    compare_recordings,
    replay_session,
)
from market_catalog.normalization import JsonFixtureNormalizer
from market_catalog.recording import canonical_json


def synthetic_catalog() -> Catalog:
    return Catalog.from_mapping(
        {
            "schema_version": "1.0.0",
            "catalog_version": "1.0.0",
            "instruments": [
                {
                    "id": "SYNTH",
                    "asset_class": "equity",
                    "venue": "SYNTHETIC",
                    "provider_symbols": [{"provider_id": "FIXTURE", "symbol": "SYNTH"}],
                    "valid_from": "2030-01-01",
                    "valid_to": None,
                    "calendar_id": "TEST-CALENDAR",
                    "price_tick": "0.01",
                    "price_precision": 2,
                    "quantity_unit": "share",
                    "quantity_precision": 0,
                    "minimum_quantity": "1",
                    "quantity_increment": "1",
                    "quote_currency": "USD",
                    "valuation_model": "equity_shares",
                    "provenance": {"status": "synthetic", "sources": [], "notes": "Test only."},
                    "historical_price_policy": {"price_basis": "raw", "corporate_action_events": []},
                }
            ],
            "calendars": [],
            "continuous_futures": [],
        }
    )


class _NoopAdapter:
    def request_snapshot(self, instrument_id: str, channel: str) -> bool:
        return False


class _BuyOnceAtLimit:
    api_version = "1.0.0"

    def on_market(self, context, market, portfolio, state):
        if state.get("submitted"):
            return StrategyDecision(state=state)
        return StrategyDecision(
            state={"submitted": True},
            intents=(
                OrderIntent(
                    order_id="buy-once",
                    instrument_id=market.event.instrument_id,
                    side="buy",
                    order_type="limit",
                    quantity=Decimal("2"),
                    limit_price=Decimal("100.00"),
                ),
            ),
        )


def managed_strategy() -> StrategyInstance:
    config = StrategyConfig(
        strategy_id="synthetic-buy-once",
        instance_id="synthetic-buy-once-1",
        strategy_version="1.0.0",
        parameters={},
    )
    return StrategyInstance(_BuyOnceAtLimit(), config, seed=17, initial_state={"submitted": False})


def raw_frame(ordinal: int, ask: str) -> RawFrame:
    source_ns = 1_899_999_999_999_000_000 + ordinal
    payload = {
        "event_type": "quote",
        "symbol": "SYNTH",
        "sequence": ordinal + 1,
        "sequence_scope": "instrument_channel",
        "source_timestamp": {
            "raw": str(source_ns),
            "unix_ns": source_ns,
            "semantics": "event_time",
        },
        "payload": {
            "bid": {"price": "99.75", "quantity": "4"},
            "ask": {"price": ask, "quantity": "2"},
        },
    }
    return RawFrame(
        provider_id="FIXTURE",
        channel="L1",
        receive_time_utc_ns=1_900_000_000_000_000_000 + ordinal,
        receive_time_monotonic_ns=1_000 + ordinal,
        arrival_ordinal=ordinal,
        raw_payload=json.dumps(payload, separators=(",", ":")).encode("utf-8"),
    )


def processor(*, preserve_ordinals: bool, recorder=None) -> EventProcessor:
    catalog = synthetic_catalog()
    pipeline = MarketDataPipeline(
        _NoopAdapter(), JsonFixtureNormalizer(catalog), DataQualityMonitor()
    )
    return EventProcessor(
        pipeline,
        catalog,
        managed_strategy(),
        EventProcessorConfig(preserve_arrival_ordinals=preserve_ordinals),
        session_id="phase8-synthetic-recording",
        recorder=recorder,
    )


def report_digest(report) -> str:
    payload = {
        "fills": report.fills,
        "ledger": report.ledger,
        "strategy_runs": report.strategy_runs,
        "positions": dict(report.portfolio.positions),
        "cash_balances": dict(report.portfolio.cash_balances),
    }
    return hashlib.sha256(canonical_json(payload).encode("utf-8")).hexdigest()


class RecordingReplayTests(unittest.TestCase):
    def record_successful_session(self, path: Path):
        recorder = SessionRecorder(
            path,
            recording_mode="historical_replay",
            clock_policy="event_time",
            queue_capacity=256,
            segment_max_records=8,
            seed=17,
        )
        engine = processor(preserve_ordinals=False, recorder=recorder)
        engine.start()
        for frame in (raw_frame(0, "100.25"), raw_frame(1, "100.00")):
            self.assertIsNotNone(engine.submit(frame))
        return engine.finish(timeout=5)

    def test_replay_matches_decisions_fills_and_ledger_hash(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "session"
            original = self.record_successful_session(path)
            self.assertEqual("completed", original.status)
            self.assertTrue(original.recording.complete)

            capture = SessionRecording(path)
            self.assertEqual([0, 1], [frame.arrival_ordinal for frame in capture.frames()])
            replay = replay_session(path, processor(preserve_ordinals=True), timeout=5)

            self.assertTrue(replay.comparison.matched, replay.comparison.first_divergence)
            self.assertEqual(2, replay.report.processed_events)
            self.assertEqual(1, len(replay.report.fills))
            self.assertGreaterEqual(len(replay.report.ledger), 1)
            self.assertEqual(report_digest(original), report_digest(replay.report))
            self.assertEqual("event_time", replay.clock_mode)

    def test_segment_modification_fails_checksum_verification(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "session"
            self.record_successful_session(path)
            capture = SessionRecording(path)
            segment = path / capture.manifest["segments"][0]["file"]
            segment.write_bytes(segment.read_bytes() + b" ")
            with self.assertRaisesRegex(RecordingError, "checksum or size mismatch"):
                SessionRecording(path)

    def test_writer_failure_marks_recording_incomplete_and_blocks_replay(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "failed-session"
            recorder = SessionRecorder(path, queue_capacity=64)
            engine = processor(preserve_ordinals=False, recorder=recorder)
            with patch.object(SessionRecorder, "_open_segment", side_effect=OSError("injected disk failure")):
                engine.start()
                engine.submit(raw_frame(0, "100.00"))
                report = engine.finish(timeout=5)

            self.assertEqual("incomplete", report.status)
            self.assertIsNotNone(report.recording)
            self.assertEqual("incomplete", report.recording.status)
            self.assertFalse(report.recording.complete)
            with self.assertRaisesRegex(RecordingError, "only complete sessions can be replayed"):
                replay_session(path, processor(preserve_ordinals=True), timeout=5)

    def test_comparison_reports_first_fill_divergence(self) -> None:
        expected = [
            {"arrival_ordinal": 4, "kind": "strategy_decision", "payload": {"intent": "buy"}},
            {"arrival_ordinal": 4, "kind": "fill", "payload": {"price": "100.00"}},
            {"arrival_ordinal": 4, "kind": "ledger_entry", "payload": {"cash": "-100.00"}},
        ]
        actual = [
            {"arrival_ordinal": 4, "kind": "strategy_decision", "payload": {"intent": "buy"}},
            {"arrival_ordinal": 4, "kind": "fill", "payload": {"price": "100.01"}},
            {"arrival_ordinal": 4, "kind": "ledger_entry", "payload": {"cash": "-100.01"}},
        ]
        comparison = compare_recordings(expected, actual)
        self.assertFalse(comparison.matched)
        self.assertEqual(4, comparison.first_divergence.arrival_ordinal)
        self.assertEqual("fill", comparison.first_divergence.kind)


if __name__ == "__main__":
    unittest.main()
