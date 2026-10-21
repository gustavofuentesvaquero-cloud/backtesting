import duckdb
import numpy as np
import pandas as pd
import sys
from pathlib import Path

# Add src to sys.path
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from market_catalog.storage import MarketDataStore, detect_price_control_zones
from market_catalog.miners_strategy import evaluate_market_opportunity
from market_catalog.backtest_engine import RollingBacktestEngine

store = MarketDataStore()
bars = store.load_bars('STMPA')

print(f"Total Bars: {len(bars)}")
print(f"Historical Range: {bars['date'].min()} to {bars['date'].max()}")

# 1. Multi-horizon levels
multi = store.get_multi_horizon_levels('STMPA')
print("\n=== MULTI-HORIZON LEVELS ===")
for k, v in multi.items():
    print(f"  {k}: {v}")

# 2. 60d levels
levels_60 = store.get_market_levels('STMPA', lookback_bars=60)
print("\n=== 60-DAY LEVELS ===")
for k, v in levels_60.items():
    print(f"  {k}: {v}")

# 3. Volatility Metrics
bars['tr'] = np.maximum(
    bars['high'] - bars['low'],
    np.maximum(abs(bars['high'] - bars['close'].shift(1)), abs(bars['low'] - bars['close'].shift(1)))
)
bars['atr14'] = bars['tr'].rolling(14).mean()
bars['ret'] = bars['close'].pct_change()
ann_vol = bars['ret'].std() * np.sqrt(252) * 100
current_atr = bars['atr14'].iloc[-1]
last_close = bars['close'].iloc[-1]
atr_pct = (current_atr / last_close) * 100

print("\n=== VOLATILITY PROFILE ===")
print(f"  Last Close: €{last_close:.2f}")
print(f"  Current ATR(14): €{current_atr:.3f} ({atr_pct:.2f}% of price)")
print(f"  5-Year Annualized Volatility: {ann_vol:.2f}%")
print(f"  ATR percentile vs 1-year: {(bars['atr14'].iloc[-252:].rank(pct=True).iloc[-1])*100:.1f}%")
print(f"  ATR percentile vs 5-year: {(bars['atr14'].rank(pct=True).iloc[-1])*100:.1f}%")

# Recent daily ATR stats
print(f"  Max ATR(14) 1y: €{bars['atr14'].iloc[-252:].max():.2f}")
print(f"  Min ATR(14) 1y: €{bars['atr14'].iloc[-252:].min():.2f}")
print(f"  Median ATR(14) 1y: €{bars['atr14'].iloc[-252:].median():.2f}")

# 4. Gaps / Price Control Zones
# Test both 0.03 (3%) and 0.02 (2%)
for min_gap in [0.03, 0.02]:
    gaps_data = detect_price_control_zones(bars, min_gap_pct=min_gap)
    zones = gaps_data['zones']
    print(f"\n=== PRICE CONTROL ZONES (GAPS >= {int(min_gap*100)}%): {len(zones)} detected ===")
    print(f"  Nearest Support Zone: {gaps_data.get('nearest_support')}")
    print(f"  Nearest Resistance Zone: {gaps_data.get('nearest_resistance')}")
    print(f"  Active Zone: {gaps_data.get('active_zone')}")

    open_gaps = [g for g in zones if g['status'] == 'ABIERTO']
    print(f"  Open Gaps: {len(open_gaps)}")
    for g in open_gaps:
        print(f"    {g['type']} Gap: [€{g['lower_bound']:.2f} - €{g['upper_bound']:.2f}], Date: {g['date']}, Mag: {g['magnitude_pct']:.1f}%, Role: {g['role']}, Dist: {g['dist_pct']:.1f}%")

    recent_tested = [g for g in zones if g['status'] != 'ABIERTO' and abs(g.get('dist_pct') or 999) < 25]
    print(f"  Recent Tested/Closed Gaps within 25% of price: {len(recent_tested)}")
    for g in recent_tested[-8:]:
        print(f"    {g['status']} {g['type']}: [€{g['lower_bound']:.2f} - €{g['upper_bound']:.2f}], Date: {g['date']}, Role: {g['role']}, Dist: {g['dist_pct']:.1f}%")

# 5. Opportunity Evaluation with 3% gaps
gaps_3pct = detect_price_control_zones(bars, min_gap_pct=0.03)
opp = evaluate_market_opportunity('STMPA', last_close, levels_60, multi, control_zones=gaps_3pct)
print("\n=== OPPORTUNITY EVALUATION ===")
for k, v in opp.items():
    print(f"  {k}: {v}")

# 6. 5-Year Rolling Backtest
engine = RollingBacktestEngine(store=store)
res = engine.run('STMPA', initial_capital=100000.0, lookback_bars=60, direction='both')
print("\n=== 5-YEAR ROLLING BACKTEST (STMPA) ===")
for k, v in res.kpis.items():
    print(f"  {k}: {v}")
