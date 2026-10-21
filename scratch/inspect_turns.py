import duckdb
import pandas as pd
import numpy as np

con = duckdb.connect()
df = con.query("SELECT * FROM 'data/historical/STMPA_1d.parquet' ORDER BY date ASC").df()
df['date'] = pd.to_datetime(df['date'])

# Let's inspect the morphology around major cyclical turns:
# 1. Top Nov 2021 - Jan 2022 (Double Top: 44.20 vs 43.56)
# 2. Bottom July - Oct 2022 (Triple bottom / Inverted H&S: 26.29, 28.89, 29.15)
# 3. Top March - July 2023 (Double Top: 47.32 vs 48.67)
# 4. Top Dec 2023 - March 2024 (Rounded top / H&S: 45.48, 43.12)
# 5. Bottom Nov 2024 - Feb 2025 (Consolidation floor at ~20-22 EUR, spring at 15.22 in April 2025)
# 6. Top June 2026 (Double top: 69.48 vs 70.70, neckline 58.76)
# 7. Current structure Sep-Oct 2026 (Double bottom attempt: 41.14 on Sep 15, 39.54 on Oct 15)

turns = [
    {"name": "Techo Doble 2021-2022", "start": "2021-11-01", "end": "2022-04-01"},
    {"name": "Suelo Complejo / HCH Inv 2022", "start": "2022-06-15", "end": "2023-01-31"},
    {"name": "Doble Techo 2023", "start": "2023-02-15", "end": "2023-11-15"},
    {"name": "Techo HCH 2023-2024", "start": "2023-11-15", "end": "2024-05-15"},
    {"name": "Doble Techo Histórico Jun 2026", "start": "2026-05-15", "end": "2026-08-15"},
    {"name": "Suelo Actual Sep-Oct 2026", "start": "2026-08-15", "end": "2026-10-19"},
]

for t in turns:
    sub = df[(df['date'] >= t['start']) & (df['date'] <= t['end'])]
    min_row = sub.loc[sub['low'].idxmin()]
    max_row = sub.loc[sub['high'].idxmax()]
    print(f"\n=======================================================")
    print(f"ANÁLISIS DE TURNO: {t['name']}")
    print(f"Periodo: {t['start']} a {t['end']}")
    print(f"Máximo del periodo: {max_row['high']:.2f} ({max_row['date'].strftime('%Y-%m-%d')})")
    print(f"Mínimo del periodo: {min_row['low']:.2f} ({min_row['date'].strftime('%Y-%m-%d')})")
    print(f"Rango del periodo: {max_row['high'] - min_row['low']:.2f} EUR ({((max_row['high'] - min_row['low'])/min_row['low'])*100:.1f}%)")
