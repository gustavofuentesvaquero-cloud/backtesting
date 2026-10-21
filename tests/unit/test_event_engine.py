from __future__ import annotations

import json
from decimal import Decimal
from pathlib import Path
from threading import Barrier, Event, Thread
from time import sleep
import unittest

from market_catalog import (
    Catalog,
    DataQualityMonitor,
    EventProcessor,
    EventProcessorConfig,
    MarketDataPipeline,
    OrderIntent,
    RawFrame,
    STAGE_NAMES,
)
from market_catalog.event_engine import _BoundedChannel
from market_catalog.normalization import JsonFixtureNormalizer


ROOT = Path(__file__).resolve().parents[2]


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
                    "provider_symbols": [
                        {"provider_id": "FIXTURE", "symbol": "SYNTH"},
                        {"provider_id": "SECOND", "symbol": "SYNTH"},
                    ],
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
            "continuous_futures": [{"id": "CONT-SYNTH"}],
        }
    )


class StubAdapter:
    def request_snapshot(self, instrument_id: str, channel: str) -> bool:
        return True


def raw_frame(
    ordinal: int,
    *,
    provider_id: str = "FIXTURE",
    sequence: int | None = None,
    event_type: str = "quote",
    channel: str = "L1",
    payload: dict | None = None,
    bid: str = "99.75",
    bid_quantity: str = "10",
    ask: str = "100.00",
    ask_quantity: str = "12",
    source_ns: int | None = None,
) -> RawFrame:
    if sequence is None:
        sequence = ordinal + 1
    if source_ns is None:
        source_ns = 1_899_999_999_999_000_000 + ordinal
    if payload is None and event_type == "quote":
        payload = {
            "bid": {"price": bid, "quantity": bid_quantity},
            "ask": {"price": ask, "quantity": ask_quantity},
        }
    message = {
        "event_type": event_type,
        "symbol": "SYNTH",
        "sequence": sequence,
        "sequence_scope": "instrument_channel",
        "source_timestamp": {
            "raw": str(source_ns),
            "unix_ns": source_ns,
            "semantics": "event_time",
        },
        "payload": payload,
    }
    return RawFrame(
        provider_id=provider_id,
        channel=channel,
        receive_time_utc_ns=1_900_000_000_000_000_000 + ordinal,
        receive_time_monotonic_ns=100 + ordinal,
        arrival_ordinal=ordinal,
        raw_payload=json.dumps(message, separators=(",", ":")).encode("utf-8"),
    )


def make_engine(strategy, config: EventProcessorConfig | None = None) -> EventProcessor:
    catalog = synthetic_catalog()
    pipeline = MarketDataPipeline(
        StubAdapter(), JsonFixtureNormalizer(catalog), DataQualityMonitor()
    )
    return EventProcessor(pipeline, catalog, strategy, config)


def run_frames(strategy, frames, config: EventProcessorConfig | None = None):
    engine = make_engine(strategy, config)
    engine.start()
    for frame in frames:
        if engine.submit(frame) is None:
            break
    return engine.finish(timeout=5)


class EventEngineTests(unittest.TestCase):
    def test_vertical_path_is_ordered_and_instruments_every_stage(self) -> None:
        class Recorder:
            def __init__(self):
                self.ordinals = []

            def on_event(self, event, book):
                self.ordinals.append(event.arrival_ordinal)

        strategy = Recorder()
        report = run_frames(strategy, [raw_frame(20), raw_frame(21), raw_frame(22)])

        self.assertEqual("completed", report.status)
        self.assertEqual([0, 1, 2], strategy.ordinals)
        self.assertEqual(3, report.received_frames)
        self.assertEqual(3, report.accepted_frames)
        self.assertEqual(3, report.processed_events)
        for stage in STAGE_NAMES:
            self.assertEqual(3, report.metrics.stages[stage].count)
            self.assertEqual(3, report.metrics.queues[stage].wait.count)
            histogram = report.metrics.stages[stage]
            self.assertLessEqual(histogram.p50_upper_ns, histogram.p90_upper_ns)
            self.assertLessEqual(histogram.p90_upper_ns, histogram.p99_upper_ns)
            self.assertLessEqual(histogram.p99_upper_ns, histogram.p999_upper_ns)
            self.assertGreaterEqual(histogram.max_ns, histogram.mean_ns)
            self.assertLessEqual(histogram.p999_upper_ns, histogram.max_ns)
        self.assertEqual(3, report.metrics.strategy_callback.count)
        self.assertEqual(3, report.metrics.end_to_end.count)
        self.assertTrue(all(queue.pending == 0 for queue in report.metrics.queues.values()))

    def test_common_sequencer_orders_multiple_sources_with_equal_timestamps(self) -> None:
        class Recorder:
            def __init__(self):
                self.seen = []

            def on_event(self, event, book):
                self.seen.append((event.provider_id, event.arrival_ordinal))

        strategy = Recorder()
        timestamp = 1_899_999_999_999_000_000
        report = run_frames(
            strategy,
            [
                raw_frame(91, provider_id="FIXTURE", sequence=1, source_ns=timestamp),
                raw_frame(12, provider_id="SECOND", sequence=1, source_ns=timestamp),
            ],
        )
        self.assertEqual("completed", report.status)
        self.assertEqual([("FIXTURE", 0), ("SECOND", 1)], strategy.seen)

    def test_concurrent_sources_follow_the_single_assigned_ordinal_order(self) -> None:
        class Recorder:
            def __init__(self):
                self.seen = []

            def on_event(self, event, book):
                self.seen.append(event.arrival_ordinal)

        strategy = Recorder()
        engine = make_engine(strategy)
        barrier = Barrier(3)
        assigned = []

        def producer(frame):
            barrier.wait()
            assigned.append(engine.submit(frame))

        engine.start()
        producers = [
            Thread(target=producer, args=(raw_frame(0, provider_id="FIXTURE", sequence=1),)),
            Thread(target=producer, args=(raw_frame(0, provider_id="SECOND", sequence=1),)),
        ]
        for thread in producers:
            thread.start()
        barrier.wait()
        for thread in producers:
            thread.join(2)
        report = engine.finish(timeout=5)

        self.assertEqual("completed", report.status)
        self.assertEqual([0, 1], sorted(assigned))
        self.assertEqual([0, 1], strategy.seen)

    def test_replay_preserves_captured_ordinals(self) -> None:
        class Recorder:
            def __init__(self):
                self.seen = []

            def on_event(self, event, book):
                self.seen.append(event.arrival_ordinal)

        strategy = Recorder()
        report = run_frames(
            strategy,
            [raw_frame(41, sequence=1), raw_frame(46, sequence=2)],
            EventProcessorConfig(preserve_arrival_ordinals=True),
        )
        self.assertEqual("completed", report.status)
        self.assertEqual([41, 46], strategy.seen)

    def test_market_order_uses_visible_liquidity_and_updates_portfolio(self) -> None:
        class BuyOnce:
            def on_event(self, event, book):
                if event.arrival_ordinal == 0:
                    return OrderIntent("market-buy", "SYNTH", "buy", "market", Decimal("15"))
                return None

        report = run_frames(BuyOnce(), [raw_frame(0)])
        self.assertEqual("completed", report.status)
        self.assertEqual(1, len(report.fills))
        fill = report.fills[0]
        self.assertEqual(Decimal("12"), fill.quantity)
        self.assertEqual(Decimal("100.00"), fill.price)
        self.assertEqual("partially_filled_cancelled", report.orders[0].status)
        self.assertEqual(Decimal("12"), report.portfolio.positions["SYNTH"])
        self.assertEqual(Decimal("-1200.00"), report.portfolio.cash_balances["USD"])

    def test_wide_spread_market_buy_uses_ask_and_records_partial_liquidity(self) -> None:
        class BuyTwo:
            def on_event(self, event, book):
                if event.arrival_ordinal == 0:
                    return OrderIntent("wide-spread-buy", "SYNTH", "buy", "market", Decimal("2"))
                return None

        report = run_frames(
            BuyTwo(),
            [raw_frame(0, bid="90.00", bid_quantity="5", ask="110.00", ask_quantity="1")],
        )
        self.assertEqual("completed", report.status)
        self.assertEqual(1, len(report.fills))
        fill = report.fills[0]
        self.assertEqual(Decimal("110.00"), fill.price)
        self.assertEqual(Decimal("20.00"), fill.spread_at_match)
        self.assertEqual(Decimal("1"), fill.quantity)
        self.assertEqual("displayed_l1_ask", fill.liquidity_source)
        self.assertEqual("partially_filled_cancelled", report.orders[0].status)

    def test_limit_price_touched_at_bid_does_not_imply_a_fill(self) -> None:
        class PassiveBuy:
            def on_event(self, event, book):
                if event.arrival_ordinal == 0:
                    return OrderIntent(
                        "passive-buy", "SYNTH", "buy", "limit", Decimal("2"), Decimal("100.00")
                    )
                return None

        report = run_frames(
            PassiveBuy(),
            [
                raw_frame(0, bid="99.75", ask="100.25"),
                raw_frame(1, bid="100.00", ask="100.25"),
            ],
        )
        self.assertEqual("completed", report.status)
        self.assertEqual((), report.fills)
        self.assertEqual("cancelled", report.orders[0].status)
        self.assertEqual("session_closed_unfilled", report.orders[0].reason)

    def test_limit_order_rechecks_later_quotes_and_fills_remaining_quantity(self) -> None:
        class LimitBuyOnce:
            def on_event(self, event, book):
                if event.arrival_ordinal == 0:
                    return OrderIntent(
                        "limit-buy", "SYNTH", "buy", "limit", Decimal("15"), Decimal("100.00")
                    )
                return None

        report = run_frames(
            LimitBuyOnce(),
            [
                raw_frame(0, sequence=1),
                raw_frame(1, sequence=2, ask="100.25", ask_quantity="5"),
                raw_frame(2, sequence=3, ask="100.00", ask_quantity="5"),
            ],
        )
        self.assertEqual("completed", report.status)
        self.assertEqual([Decimal("12"), Decimal("3")], [fill.quantity for fill in report.fills])
        self.assertEqual([Decimal("100.00"), Decimal("100.00")], [fill.price for fill in report.fills])
        self.assertEqual("filled", report.orders[0].status)
        self.assertEqual(Decimal("15"), report.portfolio.positions["SYNTH"])

    def test_bad_intent_invalidates_session_and_does_not_create_fill(self) -> None:
        class BadSize:
            def on_event(self, event, book):
                return OrderIntent("fractional", "SYNTH", "buy", "market", Decimal("1.5"))

        report = run_frames(BadSize(), [raw_frame(0)])
        self.assertEqual("invalid", report.status)
        self.assertEqual((), report.fills)
        self.assertTrue(any(item.code == "intent_rejected" for item in report.diagnostics))

    def test_continuous_future_series_cannot_be_ordered(self) -> None:
        class OrderContinuous:
            def on_event(self, event, book):
                return OrderIntent(
                    "continuous-order", "CONT-SYNTH", "buy", "market", Decimal("1")
                )

        report = run_frames(OrderContinuous(), [raw_frame(0)])
        self.assertEqual("invalid", report.status)
        self.assertEqual((), report.fills)
        rejected = next(item for item in report.diagnostics if item.code == "intent_rejected")
        self.assertIn("not executable", rejected.message)

    def test_quality_failure_blocks_strategy_and_invalidates_session(self) -> None:
        class Recorder:
            def __init__(self):
                self.calls = 0

            def on_event(self, event, book):
                self.calls += 1

        strategy = Recorder()
        report = run_frames(
            strategy,
            [raw_frame(0, sequence=1), raw_frame(1, sequence=1)],
        )
        self.assertEqual("invalid", report.status)
        self.assertEqual(1, strategy.calls)
        self.assertTrue(any(item.code == "sequence_duplicate" for item in report.diagnostics))

    def test_rejected_l1_quote_clears_previous_book_for_later_trade_events(self) -> None:
        class Recorder:
            def __init__(self):
                self.calls = []

            def on_event(self, event, book):
                self.calls.append(event.event_type)

        strategy = Recorder()
        report = run_frames(
            strategy,
            [
                raw_frame(0, sequence=1),
                raw_frame(1, sequence=2, payload={"bid": {"price": "99.75", "quantity": "10"}}),
                raw_frame(
                    2,
                    sequence=1,
                    event_type="trade",
                    channel="TRADES",
                    payload={"price": "100", "quantity": "1", "aggressor_side": "buy"},
                ),
            ],
        )
        self.assertEqual("invalid", report.status)
        self.assertEqual(["quote"], strategy.calls)

    def test_crossed_book_is_not_passed_to_strategy(self) -> None:
        class Recorder:
            def __init__(self):
                self.calls = 0

            def on_event(self, event, book):
                self.calls += 1

        strategy = Recorder()
        report = run_frames(strategy, [raw_frame(0, bid="101", ask="100")])
        self.assertEqual("invalid", report.status)
        self.assertEqual(0, strategy.calls)
        self.assertTrue(any(item.code == "crossed_l1_book" for item in report.diagnostics))

    def test_every_configured_channel_enforces_its_capacity(self) -> None:
        for stage in STAGE_NAMES:
            with self.subTest(stage=stage):
                channel = _BoundedChannel(1)
                self.assertEqual("accepted", channel.put_nowait("first"))
                self.assertEqual("full", channel.put_nowait("overflow"))
                pending, high_water, accepted, rejected = channel.snapshot()
                self.assertEqual((1, 1, 1, 1), (pending, high_water, accepted, rejected))

    def test_queue_overflow_aborts_and_reports_pending_work(self) -> None:
        entered = Event()
        release = Event()

        class BlockingStrategy:
            def on_event(self, event, book):
                if event.arrival_ordinal == 0:
                    entered.set()
                    release.wait(3)

        config = EventProcessorConfig(
            queue_capacity=64,
            queue_capacities={"strategy": 1},
        )
        engine = make_engine(BlockingStrategy(), config)
        engine.start()
        self.assertEqual(0, engine.submit(raw_frame(0)))
        self.assertTrue(entered.wait(2))
        for index in range(1, 40):
            try:
                if engine.submit(raw_frame(index)) is None:
                    break
            except RuntimeError:
                break
        release.set()
        report = engine.finish(timeout=5)
        self.assertEqual("aborted", report.status)
        self.assertTrue(any(item.code == "queue_overflow" for item in report.diagnostics))
        self.assertGreater(report.rejected_frames, 0)
        self.assertEqual(1, report.pending_by_queue["strategy"])
        self.assertEqual(1, report.metrics.queues["strategy"].capacity)

    def test_callback_budget_is_visible_and_invalidates_without_aborting(self) -> None:
        class SlowStrategy:
            def on_event(self, event, book):
                sleep(0.003)

        config = EventProcessorConfig(stage_latency_budgets_ns={"strategy_callback": 1})
        report = run_frames(SlowStrategy(), [raw_frame(0)], config)
        self.assertEqual("invalid", report.status)
        self.assertTrue(any(item.code == "strategy_callback_budget_exceeded" for item in report.diagnostics))
        self.assertEqual(1, report.processed_events)

    def test_stage_budget_is_reported_and_pipeline_continues(self) -> None:
        class NoopStrategy:
            def on_event(self, event, book):
                return None

        report = run_frames(
            NoopStrategy(),
            [raw_frame(0)],
            EventProcessorConfig(stage_latency_budgets_ns={"book": 1}),
        )
        self.assertEqual("invalid", report.status)
        self.assertEqual(1, report.processed_events)
        self.assertTrue(any(item.code == "stage_latency_budget_exceeded" for item in report.diagnostics))

    def test_stage_exception_aborts_and_is_attributed(self) -> None:
        class BrokenStrategy:
            def on_event(self, event, book):
                raise RuntimeError("synthetic failure")

        report = run_frames(BrokenStrategy(), [raw_frame(0)])
        self.assertEqual("aborted", report.status)
        self.assertTrue(any(item.code == "stage_exception" and item.stage == "strategy" for item in report.diagnostics))

    def test_queue_wait_budget_invalidates_but_drains_accepted_events(self) -> None:
        class SlowStrategy:
            def on_event(self, event, book):
                sleep(0.002)

        report = run_frames(
            SlowStrategy(),
            [raw_frame(index) for index in range(4)],
            EventProcessorConfig(queue_capacity=8, queue_wait_budgets_ns={"strategy": 1}),
        )
        self.assertEqual("invalid", report.status)
        self.assertEqual(4, report.processed_events)
        self.assertTrue(any(item.code == "queue_wait_budget_exceeded" for item in report.diagnostics))

    def test_preserved_replay_ordinals_must_be_strictly_increasing(self) -> None:
        report = run_frames(
            type("Noop", (), {"on_event": lambda self, event, book: None})(),
            [raw_frame(2), raw_frame(2)],
            EventProcessorConfig(preserve_arrival_ordinals=True),
        )
        self.assertEqual("aborted", report.status)
        self.assertTrue(any(item.code == "arrival_ordinal_invalid" for item in report.diagnostics))


if __name__ == "__main__":
    unittest.main()
