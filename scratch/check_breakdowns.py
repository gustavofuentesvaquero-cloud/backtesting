import duckdb
import pandas as pd
import numpy as np

con = duckdb.connect()
df = con.query("SELECT * FROM 'data/historical/STMPA_1d.parquet' ORDER BY date ASC").df()
df['date'] = pd.to_datetime(df['date'])

# Check behavior around breakout of swing highs/lows of 60d
lookback = 60
df['roll_high'] = df['high'].shift(1).rolling(lookback).max()
df['roll_low'] = df['low'].shift(1).rolling(lookback).min()

# When low penetrates roll_low, what happens next 10 and 20 days?
df['break_low'] = df['low'] < df['roll_low']
df['break_high'] = df['high'] > df['roll_high']

break_low_events = df[df['break_low']].copy()
# Filter events separated by at least 15 days
filtered_low_events = []
last_date = None
for idx, row in break_low_events.iterrows():
    if last_date is None or (row['date'] - last_date).days > 15:
        filtered_low_events.append(row)
        last_date = row['date']

print(f"Total separate breakdowns of 60d low: {len(filtered_low_events)}")
# For each, see if it was a continuation (trend down) or a reversal (spring/bounce back > 10%)
reversals = 0
continuations = 0

for row in filtered_low_events:
    d = row['date']
    idx = row.name
    # look forward 20 bars
    if idx + 20 < len(df):
        sub = df.iloc[idx:idx+21]
        entry = row['close']
        max_fwd = sub['high'].max()
        min_fwd = sub['low'].min()
        gain = (max_fwd - entry) / entry
        loss = (min_fwd - entry) / entry
        # If gain > 8% within 20 bars -> reversal/spring
        if gain >= 0.08 and abs(loss) <= 0.08:
            reversals += 1
        elif loss < -0.08:
            continuations += 1
        else:
            # lateral
            pass

print(f"Post-breakdown of 60d low (within 20 sessions): Reversals/Bounces: {reversals}, Continuations down: {continuations}")
