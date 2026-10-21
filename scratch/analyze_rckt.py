import duckdb
import numpy as np
import pandas as pd
from src.market_catalog.storage import MarketDataStore, detect_price_control_zones
from src.market_catalog.miners_strategy import evaluate_market_opportunity
from src.market_catalog.backtest_engine import RollingBacktestEngine

store = MarketDataStore()
bars = store.load_bars('RCKT')

print(f"Total Bars: {len(bars)}")
print(f"Historical Range: {bars['date'].min()} to {bars['date'].max()}")

# 1. Multi-horizon levels
multi = store.get_multi_horizon_levels('RCKT')
print("\n=== MULTI-HORIZON LEVELS ===")
for k, v in multi.items():
    print(f"  {k}: {v}")

# 2. 60d levels
levels_60 = store.get_market_levels('RCKT', lookback_bars=60)
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
print(f"  Last Close: ${last_close:.2f}")
print(f"  Current ATR(14): ${current_atr:.3f} ({atr_pct:.2f}% of price)")
print(f"  5-Year Annualized Volatility: {ann_vol:.2f}%")
print(f"  ATR percentile vs 1-year: {(bars['atr14'].iloc[-252:].rank(pct=True).iloc[-1])*100:.1f}%")

# 4. Gaps / Price Control Zones
gaps_data = detect_price_control_zones(bars, min_gap_pct=0.03)
zones = gaps_data['zones']
print(f"\n=== PRICE CONTROL ZONES (GAPS >= 3%): {len(zones)} detected ===")
print(f"  Nearest Support Zone: {gaps_data.get('nearest_support')}")
print(f"  Nearest Resistance Zone: {gaps_data.get('nearest_resistance')}")
print(f"  Active Zone: {gaps_data.get('active_zone')}")

open_gaps = [g for g in zones if g['status'] == 'ABIERTO']
print(f"  Open Gaps: {len(open_gaps)}")
for g in open_gaps:
    print(f"    {g['type']} Gap: [${g['lower_bound']:.2f} - ${g['upper_bound']:.2f}], Date: {g['date']}, Mag: {g['magnitude_pct']:.1f}%, Role: {g['role']}, Dist: {g['dist_pct']:.1f}%")

closed_recent = [g for g in zones if g['status'] == 'CERRADO' and abs(g.get('dist_pct') or 999) < 40]
print(f"  Key Closed Control Zones (Near Current Price): {len(closed_recent)}")
for g in closed_recent[:5]:
    print(f"    {g['type']} Tested: [${g['lower_bound']:.2f} - ${g['upper_bound']:.2f}], Date: {g['date']}, Role: {g['role']}, Dist: {g['dist_pct']:.1f}%")

# 5. Opportunity Evaluation
opp = evaluate_market_opportunity('RCKT', last_close, levels_60, multi, control_zones=gaps_data)
print("\n=== OPPORTUNITY EVALUATION ===")
print(f"  Dictamen: {opp.get('dictamen')}")
print(f"  Long TP 1: ${opp.get('long_tp_1')}, SL: ${opp.get('long_sl')}, R:R: {opp.get('long_rr')}")
print(f"  Short TP 1: ${opp.get('short_tp_1')}, SL: ${opp.get('short_sl')}, R:R: {opp.get('short_rr')}")
print(f"  Razonamiento: {opp.get('razonamiento')}")

# 6. 5-Year Rolling Backtest
engine = RollingBacktestEngine(store=store)
res = engine.run('RCKT', initial_capital=100000.0, lookback_bars=60, direction='both')
print("\n=== 5-YEAR ROLLING BACKTEST (RCKT) ===")
print(f"  Total Trades: {res.kpis.get('total_trades')}")
print(f"  Win Rate: {res.kpis.get('win_rate_pct')}%")
print(f"  Net Profit: ${res.kpis.get('net_profit_usd')} ({res.kpis.get('total_return_pct')}%)")
print(f"  Profit Factor: {res.kpis.get('profit_factor')}")
print(f"  Sharpe Ratio: {res.kpis.get('sharpe_ratio')}")
print(f"  Max Drawdown: {res.kpis.get('max_drawdown_pct')}% (${res.kpis.get('max_drawdown_usd')})")
