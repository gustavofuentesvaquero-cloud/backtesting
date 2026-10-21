import duckdb
import pandas as pd
import numpy as np

con = duckdb.connect()
df = con.query("SELECT * FROM 'data/historical/STMPA_1d.parquet' ORDER BY date ASC").df()
df['date'] = pd.to_datetime(df['date'])
print(f"Data range: {df['date'].min().strftime('%Y-%m-%d')} to {df['date'].max().strftime('%Y-%m-%d')}, Total bars: {len(df)}")

# Identify all major peaks and troughs (> 12% swings)
pivots = []
threshold = 0.12
high_p = df.iloc[0]['high']
low_p = df.iloc[0]['low']
high_idx = 0
low_idx = 0
trend = 1

for i in range(1, len(df)):
    c_high = df.iloc[i]['high']
    c_low = df.iloc[i]['low']
    
    if trend == 1:
        if c_high > high_p:
            high_p = c_high
            high_idx = i
        elif c_low < high_p * (1 - threshold):
            pivots.append(('HIGH', df.iloc[high_idx]['date'], high_p, high_idx))
            low_p = c_low
            low_idx = i
            trend = -1
    else:
        if c_low < low_p:
            low_p = c_low
            low_idx = i
        elif c_high > low_p * (1 + threshold):
            pivots.append(('LOW', df.iloc[low_idx]['date'], low_p, low_idx))
            high_p = c_high
            high_idx = i
            trend = 1

print(f"\nTotal major swings detected (>12%): {len(pivots)}")
for kind, dt, val, idx in pivots:
    print(f"[{kind:4s}] {dt.strftime('%Y-%m-%d')}: {val:6.2f} EUR (idx {idx})")
