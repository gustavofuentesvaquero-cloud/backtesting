from __future__ import annotations

import json
from pathlib import Path
import unittest

from jsonschema import Draft202012Validator

from market_catalog import Catalog
from market_catalog.adapters import JsonLinesFixtureAdapter
from market_catalog.events import RawFrame, Subscription
from market_catalog.normalization import JsonFixtureNormalizer, NormalizationError
from market_catalog.pipeline import MarketDataPipeline
from market_catalog.quality import DataQualityMonitor, QualityPolicy


ROOT = Path(__file__).resolve().parents[1]
FIXTURE = ROOT / "tests" / "fixtures" / "market_data.jsonl"


def test_catalog() -> Catalog:
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


def raw_frame(
    *,
    event_type: str = "quote",
    payload: dict | None = None,
    sequence: int | None = 1,
    sequence_scope: str = "instrument_channel",
    source_ns: int | None = 1_899_999_999_999_000_000,
    semantics: str = "event_time",
    receive_utc_ns: int = 1_900_000_000_000_000_000,
    receive_mono_ns: int = 100,
    ordinal: int = 0,
    channel: str = "L1",
    symbol: str = "SYNTH",
) -> RawFrame:
    message = {
        "event_type": event_type,
        "symbol": symbol,
        "sequence": sequence,
        "sequence_scope": sequence_scope,
        "source_timestamp": {
            "raw": None if source_ns is None else str(source_ns),
            "unix_ns": source_ns,
            "semantics": semantics,
        },
        "payload": payload
        if payload is not None
        else {
            "bid": {"price": "99.75", "quantity": "10"},
            "ask": {"price": "100.00", "quantity": "12"},
        },
    }
    return RawFrame(
        provider_id="FIXTURE",
        channel=channel,
        receive_time_utc_ns=receive_utc_ns,
        receive_time_monotonic_ns=receive_mono_ns,
        arrival_ordinal=ordinal,
        raw_payload=json.dumps(message, separators=(",", ":")).encode("utf-8"),
    )


class AdapterContractTests(unittest.TestCase):
    def test_fixture_adapter_lifecycle_filters_and_preserves_frames(self) -> None:
        adapter = JsonLinesFixtureAdapter(FIXTURE, provider_id="FIXTURE")
        adapter.connect()
        self.assertTrue(adapter.health().connected)
        adapter.subscribe([Subscription("SYNTH", "SYNTH", ("L1",))])
        frame = adapter.receive()
        self.assertIsNotNone(frame)
        self.assertEqual("L1", frame.channel)
        self.assertIn(b'"event_type":"quote"', frame.raw_payload)
        second_quote = adapter.receive()
        self.assertIsNotNone(second_quote)
        self.assertEqual(6, second_quote.arrival_ordinal)
        self.assertIsNone(adapter.receive())
        self.assertEqual("fixture exhausted", adapter.health().detail)
        adapter.close()
        adapter.close()
        self.assertEqual("closed", adapter.health().status)

    def test_fixture_pipeline_normalizes_each_fixture_event_and_recovers_l2(self) -> None:
        adapter = JsonLinesFixtureAdapter(FIXTURE, provider_id="FIXTURE")
        adapter.connect()
        adapter.subscribe(
            [Subscription("SYNTH", "SYNTH", ("L1", "L2", "TRADES"))]
        )
        normalizer = JsonFixtureNormalizer(test_catalog())
        pipeline = MarketDataPipeline(adapter, normalizer, DataQualityMonitor())

        results = []
        while True:
            frame = adapter.receive()
            if frame is None:
                break
            results.append(pipeline.ingest(frame))

        self.assertEqual(7, len(results))
        self.assertTrue(all(result.event is not None for result in results))
        self.assertEqual("SYNTH", results[0].event.instrument_id)
        self.assertEqual("L1", results[0].event.channel)
        self.assertEqual(1_899_999_999_999_000_000, results[0].event.source_timestamp_ns)
        self.assertEqual(1_900_000_000_000_000_000, results[0].event.receive_time_utc_ns)
        self.assertIsNotNone(results[0].event.raw_frame)
        self.assertTrue(results[0].event.decision_usable)

        event_schema = json.loads(
            (ROOT / "schemas" / "market_event.schema.json").read_text(encoding="utf-8")
        )
        event_validator = Draft202012Validator(event_schema)
        for result in results:
            self.assertEqual([], list(event_validator.iter_errors(result.event.to_mapping())))

        gap_delta = results[3].event
        self.assertIn("sequence_gap", gap_delta.quality_flags)
        self.assertIn("awaiting_snapshot", gap_delta.quality_flags)
        self.assertFalse(gap_delta.decision_usable)
        self.assertEqual("awaiting_snapshot", gap_delta.book_state)
        self.assertTrue(
            any(item.code == "snapshot_request_unsupported" for item in results[3].diagnostics)
        )
        self.assertIn("awaiting_snapshot", results[4].event.quality_flags)

        recovered = results[5].event
        self.assertEqual("book_snapshot", recovered.event_type)
        self.assertEqual("valid", recovered.book_state)
        self.assertTrue(recovered.decision_usable)
        self.assertEqual("price_aggregated", recovered.payload["depth_type"])

        # A quote is a complete L1 state update, so the gap is visible while its
        # current bid/ask can restore the top-of-book view.
        self.assertIn("sequence_gap", results[6].event.quality_flags)
        self.assertTrue(results[6].event.decision_usable)

    def test_raw_frame_can_be_disabled_in_normalized_event(self) -> None:
        frame = raw_frame()
        event = JsonFixtureNormalizer(test_catalog(), preserve_raw=False).normalize(frame)
        self.assertIsNone(event.raw_frame)
        self.assertNotIn("raw_frame_b64", event.to_mapping())

    def test_pipeline_requests_one_snapshot_until_recovery(self) -> None:
        class SnapshotCapableFixtureAdapter(JsonLinesFixtureAdapter):
            def __init__(self) -> None:
                super().__init__(FIXTURE, provider_id="FIXTURE")
                self.requests: list[tuple[str, str]] = []

            def request_snapshot(self, instrument_id: str, channel: str) -> bool:
                self.requests.append((instrument_id, channel))
                return True

        adapter = SnapshotCapableFixtureAdapter()
        pipeline = MarketDataPipeline(
            adapter,
            JsonFixtureNormalizer(test_catalog()),
            DataQualityMonitor(),
        )
        snapshot_payload = {
            "depth_type": "price_aggregated",
            "bids": [{"price": "99.75", "quantity": "10"}],
            "asks": [{"price": "100", "quantity": "10"}],
        }
        delta_payload = {
            "depth_type": "price_aggregated",
            "action": "update",
            "side": "bid",
            "price": "99.75",
            "quantity": "8",
        }
        initial = pipeline.ingest(
            raw_frame(
                event_type="book_snapshot",
                payload=snapshot_payload,
                sequence=10,
                channel="L2",
                ordinal=0,
            )
        )
        gap = pipeline.ingest(
            raw_frame(
                event_type="book_delta",
                payload=delta_payload,
                sequence=12,
                channel="L2",
                ordinal=1,
                receive_mono_ns=101,
            )
        )
        following_delta = pipeline.ingest(
            raw_frame(
                event_type="book_delta",
                payload=delta_payload,
                sequence=13,
                channel="L2",
                ordinal=2,
                receive_mono_ns=102,
            )
        )
        recovered = pipeline.ingest(
            raw_frame(
                event_type="book_snapshot",
                payload=snapshot_payload,
                sequence=14,
                channel="L2",
                ordinal=3,
                receive_mono_ns=103,
            )
        )
        next_gap = pipeline.ingest(
            raw_frame(
                event_type="book_delta",
                payload=delta_payload,
                sequence=16,
                channel="L2",
                ordinal=4,
                receive_mono_ns=104,
            )
        )

        self.assertTrue(initial.event.decision_usable)
        self.assertFalse(gap.event.decision_usable)
        self.assertTrue(any(item.code == "snapshot_request_accepted" for item in gap.diagnostics))
        self.assertIn("awaiting_snapshot", following_delta.event.quality_flags)
        self.assertFalse(
            any(item.code.startswith("snapshot_request_") for item in following_delta.diagnostics)
        )
        self.assertTrue(recovered.event.decision_usable)
        self.assertFalse(next_gap.event.decision_usable)
        self.assertEqual([("SYNTH", "L2"), ("SYNTH", "L2")], adapter.requests)


class NormalizationAndQualityTests(unittest.TestCase):
    def setUp(self) -> None:
        self.normalizer = JsonFixtureNormalizer(test_catalog())

    def test_incomplete_quote_is_rejected_and_invalidates_l1_health(self) -> None:
        frame = raw_frame(payload={"bid": {"price": "99.75", "quantity": "10"}})
        pipeline = MarketDataPipeline(
            adapter=JsonLinesFixtureAdapter(FIXTURE),
            normalizer=self.normalizer,
            quality=DataQualityMonitor(),
        )
        result = pipeline.ingest(frame)
        self.assertIsNone(result.event)
        self.assertEqual("incomplete_update", result.diagnostics[0].code)
        health = pipeline.quality_health(now_monotonic_ns=100)
        self.assertEqual("awaiting_snapshot", health[0].book_state)
        self.assertFalse(health[0].decision_usable)

    def test_unknown_provider_symbol_is_a_diagnostic(self) -> None:
        with self.assertRaises(NormalizationError) as raised:
            self.normalizer.normalize(raw_frame(symbol="MISSING"))
        self.assertEqual("unknown_instrument", raised.exception.code)

    def test_duplicate_sequence_is_visible_and_not_decision_usable(self) -> None:
        monitor = DataQualityMonitor()
        first = monitor.assess(self.normalizer.normalize(raw_frame(ordinal=0)))
        duplicate = monitor.assess(self.normalizer.normalize(raw_frame(ordinal=1, receive_mono_ns=101)))
        self.assertTrue(first.decision_usable)
        self.assertIn("sequence_duplicate", duplicate.quality_flags)
        self.assertFalse(duplicate.decision_usable)

    def test_unsequenced_duplicate_fingerprint_avoids_json_round_trip(self) -> None:
        monitor = DataQualityMonitor()
        first = monitor.assess(
            self.normalizer.normalize(
                raw_frame(sequence=None, source_ns=None, semantics="unknown", ordinal=0)
            )
        )
        repeated = monitor.assess(
            self.normalizer.normalize(
                raw_frame(
                    sequence=None,
                    source_ns=None,
                    semantics="unknown",
                    ordinal=1,
                    receive_mono_ns=101,
                )
            )
        )
        self.assertTrue(first.decision_usable)
        self.assertIn("duplicate_payload", repeated.quality_flags)
        self.assertFalse(repeated.decision_usable)

    def test_sequence_gap_in_l2_requires_a_snapshot_before_deltas_resume(self) -> None:
        monitor = DataQualityMonitor()
        snapshot_payload = {
            "depth_type": "price_aggregated",
            "bids": [{"price": "99.75", "quantity": "10"}],
            "asks": [{"price": "100", "quantity": "10"}],
        }
        delta_payload = {
            "depth_type": "price_aggregated",
            "action": "update",
            "side": "bid",
            "price": "99.75",
            "quantity": "8",
        }
        snapshot = monitor.assess(
            self.normalizer.normalize(raw_frame(event_type="book_snapshot", payload=snapshot_payload, sequence=10, channel="L2"))
        )
        gap = monitor.assess(
            self.normalizer.normalize(raw_frame(event_type="book_delta", payload=delta_payload, sequence=12, receive_mono_ns=101, ordinal=1, channel="L2"))
        )
        following_delta = monitor.assess(
            self.normalizer.normalize(raw_frame(event_type="book_delta", payload=delta_payload, sequence=13, receive_mono_ns=102, ordinal=2, channel="L2"))
        )
        self.assertTrue(snapshot.decision_usable)
        self.assertFalse(gap.decision_usable)
        self.assertFalse(following_delta.decision_usable)

        recovered = monitor.assess(
            self.normalizer.normalize(raw_frame(event_type="book_snapshot", payload=snapshot_payload, sequence=14, receive_mono_ns=103, ordinal=3, channel="L2"))
        )
        self.assertTrue(recovered.decision_usable)
        self.assertEqual("valid", recovered.book_state)

    def test_source_timestamp_order_and_semantics_changes_are_detected(self) -> None:
        monitor = DataQualityMonitor()
        monitor.assess(self.normalizer.normalize(raw_frame(sequence=1, source_ns=200)))
        older = monitor.assess(
            self.normalizer.normalize(raw_frame(sequence=2, source_ns=150, receive_mono_ns=101, ordinal=1))
        )
        self.assertIn("event_time_out_of_order", older.quality_flags)
        self.assertFalse(older.decision_usable)

        changed = monitor.assess(
            self.normalizer.normalize(raw_frame(sequence=3, source_ns=210, semantics="transaction_time", receive_mono_ns=102, ordinal=2))
        )
        self.assertIn("timestamp_semantics_changed", changed.quality_flags)
        self.assertFalse(changed.decision_usable)

    def test_local_monotonic_receive_time_moving_backwards_is_detected(self) -> None:
        monitor = DataQualityMonitor()
        monitor.assess(self.normalizer.normalize(raw_frame(receive_mono_ns=200)))
        out_of_order = monitor.assess(
            self.normalizer.normalize(raw_frame(sequence=2, receive_mono_ns=199, ordinal=1))
        )
        self.assertIn("receive_time_out_of_order", out_of_order.quality_flags)
        self.assertFalse(out_of_order.decision_usable)

    def test_sequence_scope_change_is_detected(self) -> None:
        monitor = DataQualityMonitor()
        monitor.assess(
            self.normalizer.normalize(
                raw_frame(sequence=1, sequence_scope="instrument_channel", ordinal=0)
            )
        )
        changed = monitor.assess(
            self.normalizer.normalize(
                raw_frame(
                    sequence=2,
                    sequence_scope="channel",
                    receive_mono_ns=101,
                    ordinal=1,
                )
            )
        )
        self.assertIn("sequence_scope_changed", changed.quality_flags)
        self.assertFalse(changed.decision_usable)

    def test_source_age_and_receive_silence_use_explicit_thresholds(self) -> None:
        monitor = DataQualityMonitor(
            QualityPolicy(source_clock_comparable=True, max_source_age_ns=100, max_silence_ns=10)
        )
        stale = monitor.assess(
            self.normalizer.normalize(raw_frame(source_ns=0, receive_utc_ns=1000, receive_mono_ns=100))
        )
        self.assertIn("stale_source_timestamp", stale.quality_flags)
        self.assertFalse(stale.decision_usable)
        self.assertTrue(monitor.health_snapshot(now_monotonic_ns=111)[0].stale)

    def test_missing_timestamp_and_sequence_are_reported_without_assuming_them(self) -> None:
        monitor = DataQualityMonitor()
        event = self.normalizer.normalize(
            raw_frame(sequence=None, source_ns=None, semantics="unknown")
        )
        assessed = monitor.assess(event)
        self.assertIn("sequence_unavailable", assessed.quality_flags)
        self.assertIn("timestamp_unverified", assessed.quality_flags)
        self.assertTrue(assessed.decision_usable)

    def test_required_sequence_policy_blocks_events_without_sequence(self) -> None:
        monitor = DataQualityMonitor(QualityPolicy(sequence_required=True))
        event = monitor.assess(
            self.normalizer.normalize(raw_frame(sequence=None, source_ns=None, semantics="unknown"))
        )
        self.assertIn("missing_sequence", event.quality_flags)
        self.assertFalse(event.decision_usable)

    def test_order_by_order_snapshot_requires_order_ids(self) -> None:
        payload = {
            "depth_type": "order_by_order",
            "bids": [{"price": "99.75", "quantity": "10"}],
            "asks": [],
        }
        with self.assertRaises(NormalizationError) as raised:
            self.normalizer.normalize(
                raw_frame(event_type="book_snapshot", payload=payload, channel="L2")
            )
        self.assertEqual("incomplete_update", raised.exception.code)

    def test_reconnect_invalidates_l2_until_snapshot(self) -> None:
        monitor = DataQualityMonitor()
        payload = {
            "depth_type": "price_aggregated",
            "bids": [{"price": "99.75", "quantity": "10"}],
            "asks": [{"price": "100", "quantity": "10"}],
        }
        snapshot = monitor.assess(
            self.normalizer.normalize(raw_frame(event_type="book_snapshot", payload=payload, sequence=10, channel="L2"))
        )
        self.assertTrue(snapshot.decision_usable)
        monitor.mark_reconnected("FIXTURE")
        delta = {
            "depth_type": "price_aggregated",
            "action": "update",
            "side": "bid",
            "price": "99.75",
            "quantity": "8",
        }
        event = monitor.assess(
            self.normalizer.normalize(raw_frame(event_type="book_delta", payload=delta, sequence=1, channel="L2", ordinal=1))
        )
        self.assertIn("awaiting_snapshot", event.quality_flags)
        self.assertFalse(event.decision_usable)

    def test_l1_quote_recovers_after_reconnect_without_waiting_for_l2_snapshot(self) -> None:
        monitor = DataQualityMonitor()
        first_quote = monitor.assess(self.normalizer.normalize(raw_frame(channel="L1", ordinal=0)))
        first_snapshot = monitor.assess(
            self.normalizer.normalize(
                raw_frame(
                    event_type="book_snapshot",
                    payload={
                        "depth_type": "price_aggregated",
                        "bids": [{"price": "99.75", "quantity": "10"}],
                        "asks": [{"price": "100", "quantity": "10"}],
                    },
                    sequence=10,
                    channel="L2",
                    ordinal=1,
                )
            )
        )
        self.assertTrue(first_quote.decision_usable)
        self.assertTrue(first_snapshot.decision_usable)

        monitor.mark_reconnected("FIXTURE")
        recovered_quote = monitor.assess(
            self.normalizer.normalize(
                raw_frame(sequence=1, channel="L1", ordinal=2, receive_mono_ns=102)
            )
        )
        health = monitor.health_snapshot(now_monotonic_ns=102)
        self.assertTrue(recovered_quote.decision_usable)
        l1_health = next(item for item in health if item.channel == "L1")
        l2_health = next(item for item in health if item.channel == "L2")
        self.assertTrue(l1_health.decision_usable)
        self.assertEqual("valid", l1_health.book_state)
        self.assertFalse(l2_health.decision_usable)
        self.assertEqual("awaiting_snapshot", l2_health.book_state)

    def test_trade_sequence_gap_is_not_decision_usable(self) -> None:
        monitor = DataQualityMonitor()
        first = monitor.assess(
            self.normalizer.normalize(
                raw_frame(
                    event_type="trade",
                    payload={"price": "100", "quantity": "1", "aggressor_side": "buy"},
                    channel="TRADES",
                    sequence=10,
                    ordinal=0,
                )
            )
        )
        gap = monitor.assess(
            self.normalizer.normalize(
                raw_frame(
                    event_type="trade",
                    payload={"price": "100", "quantity": "1", "aggressor_side": "buy"},
                    channel="TRADES",
                    sequence=12,
                    ordinal=1,
                    receive_mono_ns=101,
                )
            )
        )
        self.assertTrue(first.decision_usable)
        self.assertIn("sequence_gap", gap.quality_flags)
        self.assertFalse(gap.decision_usable)

    def test_sequence_scope_channel_checks_global_order_without_false_symbol_gap(self) -> None:
        mapping = test_catalog().to_mapping()
        second = dict(mapping["instruments"][0])
        second["id"] = "SYNTH-2"
        second["provider_symbols"] = [{"provider_id": "FIXTURE", "symbol": "SYNTH-2"}]
        mapping["instruments"].append(second)
        normalizer = JsonFixtureNormalizer(Catalog.from_mapping(mapping))
        monitor = DataQualityMonitor()

        first = monitor.assess(
            normalizer.normalize(
                raw_frame(sequence=40, sequence_scope="channel", symbol="SYNTH", ordinal=0)
            )
        )
        other_symbol = monitor.assess(
            normalizer.normalize(
                raw_frame(
                    sequence=41,
                    sequence_scope="channel",
                    symbol="SYNTH-2",
                    ordinal=1,
                    receive_mono_ns=101,
                )
            )
        )
        gap = monitor.assess(
            normalizer.normalize(
                raw_frame(
                    sequence=43,
                    sequence_scope="channel",
                    symbol="SYNTH",
                    ordinal=2,
                    receive_mono_ns=102,
                )
            )
        )

        self.assertNotIn("sequence_gap", first.quality_flags)
        self.assertNotIn("sequence_gap", other_symbol.quality_flags)
        self.assertIn("sequence_gap", gap.quality_flags)

    def test_unknown_sequence_scope_blocks_sequence_assumptions(self) -> None:
        event = self.normalizer.normalize(
            raw_frame(sequence=1, sequence_scope="unknown")
        )
        assessed = DataQualityMonitor().assess(event)
        self.assertIn("sequence_scope_unverified", assessed.quality_flags)
        self.assertFalse(assessed.decision_usable)

    def test_quality_flags_remain_valid_under_canonical_event_schema(self) -> None:
        event = self.normalizer.normalize(
            raw_frame(sequence=None, source_ns=None, semantics="unknown")
        )
        assessed = DataQualityMonitor(
            QualityPolicy(source_timestamp_required=True)
        ).assess(event)
        schema = json.loads((ROOT / "schemas" / "market_event.schema.json").read_text(encoding="utf-8"))
        errors = list(Draft202012Validator(schema).iter_errors(assessed.to_mapping()))
        self.assertEqual([], errors)


if __name__ == "__main__":
    unittest.main()
