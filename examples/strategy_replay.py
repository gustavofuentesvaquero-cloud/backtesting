"""Executable deterministic example for the versioned strategy API."""

from __future__ import annotations

import json
from decimal import Decimal
from pathlib import Path
import sys
from tempfile import TemporaryDirectory

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from market_catalog import (  # noqa: E402
    Catalog,
    CommissionModel,
    DataQualityMonitor,
    EventProcessor,
    EventProcessorConfig,
    MatchingPolicy,
    MarketDataPipeline,
    OrderIntent,
    RawFrame,
    StrategyConfig,
    StrategyDecision,
    StrategyInstance,
)
from market_catalog.normalization import JsonFixtureNormalizer  # noqa: E402


class _NoopAdapter:
    def request_snapshot(self, instrument_id: str, channel: str) -> bool:
        return False


class BuyOnceAtLimit:
    """Synthetic strategy: submit one displayed-liquidity limit buy."""

    api_version = "1.0.0"

    def validate_config(self, parameters):
        if set(parameters) != {"quantity", "limit_price"}:
            raise ValueError("Expected only quantity and limit_price string parameters.")
        if Decimal(parameters["quantity"]) <= 0 or Decimal(parameters["limit_price"]) <= 0:
            raise ValueError("Synthetic order quantity and price must be positive.")
        return True

    def on_market(self, context, market, portfolio, state):
        if market.event.event_type != "quote":
            return StrategyDecision(state=state)
        if state.get("submitted", False):
            visible_position = portfolio.positions.get(market.event.instrument_id, Decimal(0))
            return StrategyDecision(
                state={
                    **state,
                    "position_seen_before_second_quote": format(visible_position, "f"),
                    "portfolio_as_of_ordinal": portfolio.snapshot_id.arrival_ordinal,
                }
            )
        intent = OrderIntent(
            order_id="buy-once",
            instrument_id=market.event.instrument_id,
            side="buy",
            order_type="limit",
            quantity=Decimal(context.config.parameters["quantity"]),
            limit_price=Decimal(context.config.parameters["limit_price"]),
        )
        return StrategyDecision(state={"submitted": True}, intents=(intent,))


def _config() -> StrategyConfig:
    return StrategyConfig(
        strategy_id="synthetic-limit-demo",
        instance_id="synthetic-limit-demo-1",
        strategy_version="1.0.0",
        parameters={"quantity": "2", "limit_price": "100.00"},
    )


def _catalog() -> Catalog:
    return Catalog.from_mapping(
        {
            "schema_version": "1.0.0",
            "catalog_version": "1.0.0",
            "instruments": [
                {
                    "id": "SYNTH-EQUITY",
                    "asset_class": "equity",
                    "venue": "SYNTHETIC",
                    "provider_symbols": [{"provider_id": "FIXTURE", "symbol": "SYNTH"}],
                    "valid_from": "2030-01-01",
                    "valid_to": None,
                    "calendar_id": "SYNTHETIC-CALENDAR",
                    "price_tick": "0.01",
                    "price_precision": 2,
                    "quantity_unit": "share",
                    "quantity_precision": 0,
                    "minimum_quantity": "1",
                    "quantity_increment": "1",
                    "quote_currency": "USD",
                    "valuation_model": "equity_shares",
                    "provenance": {
                        "status": "synthetic",
                        "sources": [],
                        "notes": "Example fixture only; not an exchange specification.",
                    },
                    "historical_price_policy": {
                        "price_basis": "raw",
                        "corporate_action_events": [],
                    },
                }
            ],
            "calendars": [],
            "continuous_futures": [],
        }
    )


def _frame(ordinal: int, bid: str, ask: str, ask_quantity: str) -> RawFrame:
    source_ns = 1_899_999_999_999_000_000 + ordinal
    message = {
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
            "bid": {"price": bid, "quantity": "5"},
            "ask": {"price": ask, "quantity": ask_quantity},
        },
    }
    return RawFrame(
        provider_id="FIXTURE",
        channel="L1",
        receive_time_utc_ns=1_900_000_000_000_000_000 + ordinal,
        receive_time_monotonic_ns=100 + ordinal,
        arrival_ordinal=ordinal,
        raw_payload=json.dumps(message, separators=(",", ":")).encode("utf-8"),
    )


def _run() -> tuple[object, StrategyInstance]:
    catalog = _catalog()
    strategy = StrategyInstance(BuyOnceAtLimit(), _config(), seed=7, initial_state={"submitted": False})
    pipeline = MarketDataPipeline(
        _NoopAdapter(), JsonFixtureNormalizer(catalog), DataQualityMonitor()
    )
    processor = EventProcessor(
        pipeline,
        catalog,
        strategy,
        config=EventProcessorConfig(
            matching_policy=MatchingPolicy(
                commission=CommissionModel(
                    per_unit=Decimal("0.01"),
                    currency="USD",
                    currency_precision=2,
                )
            )
        ),
        session_id="synthetic-phase4-replay",
    )
    processor.start()
    frames = (
        _frame(0, bid="99.75", ask="100.00", ask_quantity="1"),
        _frame(1, bid="99.50", ask="99.75", ask_quantity="2"),
    )
    for frame in frames:
        if processor.submit(frame) is None:
            break
    return processor.finish(timeout=5), strategy


def main() -> None:
    first, strategy = _run()
    second, _ = _run()
    if first.status != "completed" or second.status != "completed":
        raise SystemExit(f"Synthetic replay failed: {first.status}, {second.status}")
    if (first.fills, first.portfolio.positions, first.portfolio.realized_pnl,
            first.portfolio.unrealized_pnl, first.strategy_runs[0].state_json) != (
            second.fills, second.portfolio.positions, second.portfolio.realized_pnl,
            second.portfolio.unrealized_pnl, second.strategy_runs[0].state_json):
        raise SystemExit("Synthetic replay produced different deterministic results.")

    # A completed snapshot can be restored only to the same API, config, seed,
    # strategy identity and instance identity.
    with TemporaryDirectory() as temp_dir:
        snapshot_path = Path(temp_dir) / "strategy-state.json"
        strategy.save_state(snapshot_path)
        restored = StrategyInstance(
            BuyOnceAtLimit(), _config(), seed=7, initial_state={"submitted": False}
        )
        restored.restore_state(snapshot_path)
        if restored.state_json() != strategy.state_json():
            raise SystemExit("Strategy state did not survive save/restore.")

    output = {
        "status": first.status,
        "session_id": first.session_id,
        "replay_deterministic": True,
        "state_save_restore": "ok",
        "fills": [
            {
                "order_id": fill.order_id,
                "quantity": format(fill.quantity, "f"),
                "price": format(fill.price, "f"),
                "commission": format(fill.commission, "f"),
                "commission_currency": fill.commission_currency,
                "liquidity_source": fill.liquidity_source,
                "matching_rule": fill.matching_rule,
                "policy_version": fill.policy_version,
                "decision_timestamp_ns": fill.decision_timestamp_ns,
                "order_arrival_timestamp_ns": fill.order_arrival_timestamp_ns,
                "match_timestamp_ns": fill.match_timestamp_ns,
                "bid_at_match": format(fill.bid_at_match, "f"),
                "ask_at_match": format(fill.ask_at_match, "f"),
                "spread_at_match": format(fill.spread_at_match, "f"),
                "bid_quantity_at_match": format(fill.bid_quantity_at_match, "f"),
                "ask_quantity_at_match": format(fill.ask_quantity_at_match, "f"),
                "available_quantity_before_fill": format(fill.available_quantity_before_fill, "f"),
                "match_provider_id": fill.match_provider_id,
                "match_channel": fill.match_channel,
                "match_sequence": fill.match_sequence,
            }
            for fill in first.fills
        ],
        "matching_policy": json.loads(first.matching_policy_json),
        "order_outcomes": [
            {
                "order_id": order.order_id,
                "status": order.status,
                "filled_quantity": format(order.filled_quantity, "f"),
                "remaining_quantity": format(order.remaining_quantity, "f"),
                "commission_total": format(order.commission_total, "f"),
                "reason": order.reason,
            }
            for order in first.orders
        ],
        "commissions_paid": {
            key: format(value, "f") for key, value in first.portfolio.commissions_paid.items()
        },
        "positions": {key: format(value, "f") for key, value in first.portfolio.positions.items()},
        "realized_pnl": {key: format(value, "f") for key, value in first.portfolio.realized_pnl.items()},
        "unrealized_pnl": {
            key: format(value, "f") for key, value in first.portfolio.unrealized_pnl.items()
        },
        "strategy": {
            "strategy_id": first.strategy_runs[0].strategy_id,
            "instance_id": first.strategy_runs[0].instance_id,
            "seed": first.strategy_runs[0].seed,
            "callbacks": first.strategy_runs[0].callback_count,
            "lifecycle": first.strategy_runs[0].lifecycle_status,
            "state": json.loads(first.strategy_runs[0].state_json)["state"],
        },
    }
    print(json.dumps(output, indent=2, sort_keys=True))


if __name__ == "__main__":
    main()
