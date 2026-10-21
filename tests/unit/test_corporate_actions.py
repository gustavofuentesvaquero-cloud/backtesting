"""Adjustment/event exclusivity and hand-computed raw-price accounting."""

from dataclasses import replace
from decimal import Decimal as D
import unittest

from market_catalog.corporate_actions import EquityActionPolicy, INITIAL_EQUITY_POLICY
from market_catalog.simulation import Portfolio, PortfolioPolicy, Fill
from tests.unit.test_backtest_engine import run, scenario
from tests.unit.test_event_engine import synthetic_catalog


def funded_portfolio(basis="raw", side="buy"):
    portfolio = Portfolio(synthetic_catalog(), PortfolioPolicy(equity_price_basis=basis))
    portfolio.record_cash_adjustment("capital", "USD", D(1000), 0, "synthetic")
    portfolio.apply(Fill("fill", "order", "SYNTH", side, D(10), D(100), "USD", 1, match_timestamp_ns=1))
    return portfolio


class CorporateActionTests(unittest.TestCase):
    def test_incompatible_adjustment_and_event_modes_are_rejected(self):
        for basis, dividends, splits in (("total_return_adjusted", "cash_events", "embedded"),
                                          ("split_adjusted", "excluded", "position_events"),
                                          ("split_adjusted", "cash_events", "embedded"),
                                          ("unknown", "cash_events", "position_events")):
            with self.subTest(basis=basis, dividends=dividends, splits=splits), self.assertRaises(ValueError):
                EquityActionPolicy(basis, dividends, splits)

    def test_daily_engines_require_a_basis_for_events_and_never_substitute_adjusted_close(self):
        bars = scenario()
        bars["dividend"] = 0.
        bars.loc[61, "dividend"] = .25
        bars["adj_close"] = bars.close / 2
        with self.assertRaisesRegex(ValueError, "explicit price basis"):
            run(bars)
        result = run(bars, corporate_action_policy=INITIAL_EQUITY_POLICY)
        reference = run(scenario())
        self.assertEqual(result.trades, reference.trades)
        self.assertEqual(result.daily_points, reference.daily_points)
        report = result.kpis["corporate_actions"]
        self.assertEqual(report["observed_events"]["dividend"], 1)
        self.assertEqual(report["return_basis"], "price_return")
        self.assertFalse(report["dividend_cash_applied"])
        self.assertFalse(report["split_positions_applied"])
        self.assertIn("Dividendos en efectivo: no", result.summary())

    def test_raw_daily_event_accounting_is_rejected_and_legacy_is_labelled_unverified(self):
        with self.assertRaisesRegex(ValueError, "raw-price ledger"):
            run(scenario(), corporate_action_policy=EquityActionPolicy("raw", "cash_events", "position_events"))
        self.assertEqual(run(scenario()).kpis["corporate_actions"]["price_basis_status"], "unknown")

    def test_total_return_series_and_split_adjusted_series_do_not_change_units(self):
        bars = scenario()
        bars["split"] = 0.
        bars.loc[61, "split"] = 2.
        for policy in (INITIAL_EQUITY_POLICY, EquityActionPolicy("total_return_adjusted", "embedded", "embedded")):
            result = run(bars, corporate_action_policy=policy)
            self.assertEqual(result.trades, run(scenario()).trades)
            self.assertFalse(result.kpis["corporate_actions"]["split_positions_applied"])

    def test_raw_split_preserves_value_and_dividend_is_credited_once(self):
        portfolio = funded_portfolio()
        portfolio.record_split("split", "SYNTH", D(2), 2, "synthetic")
        after = portfolio.snapshot()
        self.assertEqual(after.positions["SYNTH"], D(20))
        self.assertEqual(after.cash_balances.get("USD", D(0)) + after.positions["SYNTH"] * D(50), D(1000))
        portfolio.record_dividend("dividend", "SYNTH", D(".25"), "USD", 3, "synthetic")
        after = portfolio.snapshot()
        self.assertEqual(after.cash_balances["USD"], D(5))
        self.assertEqual(after.dividends_by_currency["USD"], D(5))
        self.assertEqual(after.positions["SYNTH"] * D(50) + after.cash_balances["USD"], D(1005))
        replay = Portfolio.from_ledger(synthetic_catalog(), portfolio.ledger)
        self.assertEqual(replay.snapshot().cash_balances, after.cash_balances)
        self.assertEqual(replay.snapshot().positions, after.positions)

    def test_duplicate_action_with_new_id_is_rejected_without_mutation(self):
        portfolio = funded_portfolio()
        split = portfolio.record_split("split", "SYNTH", D(2), 2, "synthetic")
        dividend = portfolio.record_dividend("dividend", "SYNTH", D(".25"), "USD", 3, "synthetic")
        before = portfolio.ledger
        for entry in (split, dividend):
            with self.subTest(kind=entry.kind), self.assertRaisesRegex(ValueError, "Duplicate corporate"):
                portfolio.apply_ledger_entry(replace(entry, entry_id=entry.entry_id + "-other-provider"))
        self.assertEqual(portfolio.ledger, before)
        replay = Portfolio.from_ledger(synthetic_catalog(), before)
        with self.assertRaisesRegex(ValueError, "Duplicate corporate"):
            replay.apply_ledger_entry(replace(dividend, entry_id="retry-after-replay"))

    def test_adjusted_price_ledger_rejects_corporate_actions_including_direct_replay(self):
        original = funded_portfolio()
        split = original.record_split("split", "SYNTH", D(2), 2, "synthetic")
        dividend = original.record_dividend("dividend", "SYNTH", D(".25"), "USD", 3, "synthetic")
        for basis in ("split_adjusted", "total_return_adjusted"):
            portfolio = funded_portfolio(basis)
            before = portfolio.ledger
            for entry in (split, dividend):
                with self.subTest(basis=basis, kind=entry.kind), self.assertRaisesRegex(ValueError, "Adjusted equity"):
                    portfolio.apply_ledger_entry(entry)
            self.assertEqual(portfolio.ledger, before)
            with self.assertRaises(ValueError):
                Portfolio.from_ledger(synthetic_catalog(), original.ledger, PortfolioPolicy(equity_price_basis=basis))

    def test_short_dividend_is_debited_and_reverse_split_reconciles(self):
        portfolio = funded_portfolio(side="sell")
        portfolio.record_dividend("dividend", "SYNTH", D(".25"), "USD", 2, "synthetic")
        self.assertEqual(portfolio.snapshot().dividends_by_currency["USD"], D("-2.50"))
        portfolio.record_split("reverse", "SYNTH", D(".5"), 3, "synthetic")
        self.assertEqual(portfolio.snapshot().positions["SYNTH"], D(-5))

    def test_portfolio_basis_is_serialized_and_legacy_policies_default_to_raw(self):
        policy = PortfolioPolicy(equity_price_basis="total_return_adjusted")
        self.assertEqual(PortfolioPolicy.from_mapping(policy.to_mapping()), policy)
        mapping = PortfolioPolicy().to_mapping()
        mapping.pop("equity_price_basis", None)
        self.assertEqual(PortfolioPolicy.from_mapping(mapping).equity_price_basis, "raw")
