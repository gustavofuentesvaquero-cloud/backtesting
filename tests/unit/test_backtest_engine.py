"""Small synthetic scenarios independent of downloaded market histories."""

import unittest

import pandas as pd

from market_catalog import RollingBacktestEngine


def scenario():
    dates = pd.bdate_range("2024-01-01", periods=80, tz="UTC")
    frame = pd.DataFrame({"date": dates, "open": 110.0, "high": 120.0, "low": 100.0, "close": 110.0, "volume": 1000.0})
    frame.loc[60:, ["high", "low"]] = [115.0, 105.0]
    frame.loc[60, ["open", "high", "low", "close"]] = [110.0, 110.0, 102.0, 108.0]
    frame.loc[61, ["open", "high", "low", "close"]] = [110.0, 121.0, 108.0, 120.0]
    return frame


class MemoryStore:
    def __init__(self, bars):
        self.bars = bars

    def load_bars(self, symbol, timeframe="1d"):
        return self.bars.copy(deep=True)


def run(bars, **overrides):
    config = dict(symbol="SYNTHETIC", direction="long", initial_capital=100_000.0,
                  shares=10, slippage_bps=0, commission_per_unit=0.1)
    config.update(overrides)
    return RollingBacktestEngine(MemoryStore(bars)).run(**config)


class SyntheticBacktestTests(unittest.TestCase):
    def test_single_trade_reconciles_to_manual_cash_and_costs(self):
        result = run(scenario())
        self.assertEqual(len(result.trades), 1)
        trade = result.trades[0]
        self.assertEqual((trade.entry_price, trade.exit_price), (103.0, 120.0))
        self.assertEqual((trade.pnl_gross, trade.commission, trade.pnl_net), (170.0, 2.0, 168.0))
        self.assertEqual(result.daily_points[-1].cash, 100_168.0)
        self.assertEqual(result.daily_points[-1].nav, 100_168.0)

    def test_gap_below_stop_exits_at_open(self):
        bars = scenario()
        bars.loc[61, ["open", "high", "low", "close"]] = [90.0, 94.0, 88.0, 92.0]
        result = run(bars)
        trade = result.trades[0]
        self.assertEqual(trade.exit_reason, "stop_loss")
        self.assertEqual(trade.exit_price, 90.0)
        self.assertEqual(trade.pnl_net, -132.0)

    def test_existing_position_uses_stop_when_both_levels_are_touched(self):
        bars = scenario()
        bars.loc[61, ["high", "low"]] = [125.0, 90.0]
        trade = run(bars).trades[0]
        self.assertEqual(trade.exit_reason, "stop_loss")
        self.assertEqual(trade.exit_price, 95.0)

    def test_unchanged_prefix_with_future_mutations_without_control_zones(self):
        bars = scenario()
        changed = bars.copy(deep=True)
        changed.loc[70:, ["open", "high", "low", "close"]] *= 2
        before = run(bars, auto_close_at_end=False)
        after = run(changed, auto_close_at_end=False)
        self.assertEqual(before.daily_points[:70], after.daily_points[:70])
        cutoff = bars.loc[70, "date"].strftime("%Y-%m-%d")
        self.assertEqual([t for t in before.trades if t.exit_date < cutoff], [t for t in after.trades if t.exit_date < cutoff])

    def test_repeated_run_is_identical_and_does_not_mutate_inputs(self):
        bars = scenario()
        unchanged = bars.copy(deep=True)
        first, second = run(bars), run(bars)
        self.assertEqual(first.trades, second.trades)
        self.assertEqual(first.daily_points, second.daily_points)
        pd.testing.assert_frame_equal(bars, unchanged)

    def test_no_entry_during_warmup(self):
        result = run(scenario())
        self.assertTrue(all(point.position_shares == 0 for point in result.daily_points[:60]))
        self.assertEqual(result.trades[0].entry_date, scenario().loc[60, "date"].strftime("%Y-%m-%d"))
