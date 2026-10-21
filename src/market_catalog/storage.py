"""DuckDB and Parquet storage engine for historical and real-time market data."""

from __future__ import annotations

from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path
from typing import Any, Mapping

import duckdb
import pandas as pd


class MarketDataStore:
    """High-performance columnar storage and analytics using DuckDB and Parquet."""

    def __init__(self, base_dir: str | Path | None = None):
        if base_dir is None:
            self.base_dir = Path(__file__).resolve().parents[2] / "data" / "historical"
        else:
            self.base_dir = Path(base_dir)
        self.base_dir.mkdir(parents=True, exist_ok=True)
        self._con = duckdb.connect(":memory:")

    def _parquet_path(self, instrument_id: str, timeframe: str = "1d") -> Path:
        sanitized = instrument_id.replace("/", "_").replace("=", "_").upper()
        return self.base_dir / f"{sanitized}_{timeframe}.parquet"

    def save_bars(
        self,
        instrument_id: str,
        df: pd.DataFrame,
        timeframe: str = "1d",
    ) -> Path:
        """Store standardized OHLCV bars into a compressed Parquet file."""
        if df.empty:
            raise ValueError("Cannot save an empty DataFrame.")

        data = df.copy()
        # Flatten MultiIndex columns if present (from modern yfinance)
        if isinstance(data.columns, pd.MultiIndex):
            data.columns = [col[0].lower() for col in data.columns]
        else:
            data.columns = [str(col).lower() for col in data.columns]

        if "date" not in data.columns and isinstance(data.index, pd.DatetimeIndex):
            data = data.reset_index()
            # If the reset column is named 'Date' or 'index', rename to 'date'
            if "index" in data.columns and "date" not in data.columns:
                data = data.rename(columns={"index": "date"})
            data.columns = [str(col).lower() for col in data.columns]

        required_cols = {"date", "open", "high", "low", "close"}
        if not required_cols.issubset(set(data.columns)):
            raise ValueError(f"DataFrame must contain columns: {required_cols}. Found: {list(data.columns)}")

        # Ensure types and timezone awareness
        data["date"] = pd.to_datetime(data["date"], utc=True)
        data["instrument_id"] = str(instrument_id).upper()
        if "volume" not in data.columns:
            data["volume"] = 0.0

        for col in ("open", "high", "low", "close", "volume"):
            data[col] = data[col].astype(float)

        target_path = self._parquet_path(instrument_id, timeframe)
        # Register in DuckDB and write compressed Parquet
        self._con.register("incoming_bars", data)
        self._con.execute(f"COPY incoming_bars TO '{target_path.as_posix()}' (FORMAT PARQUET, COMPRESSION ZSTD)")
        self._con.unregister("incoming_bars")
        return target_path

    def load_bars(
        self,
        instrument_id: str,
        timeframe: str = "1d",
        start_date: str | None = None,
        end_date: str | None = None,
    ) -> pd.DataFrame:
        """Load bars from Parquet via DuckDB query."""
        path = self._parquet_path(instrument_id, timeframe)
        if not path.exists():
            return pd.DataFrame()

        query = f"SELECT * FROM read_parquet('{path.as_posix()}')"
        conditions = []
        if start_date:
            conditions.append(f"date >= '{start_date}'")
        if end_date:
            conditions.append(f"date <= '{end_date}'")
        if conditions:
            query += " WHERE " + " AND ".join(conditions)
        query += " ORDER BY date ASC"

        return self._con.execute(query).df()

    def get_market_levels(
        self,
        instrument_id: str,
        timeframe: str = "1d",
        lookback_bars: int = 20,
    ) -> dict[str, Any]:
        """Compute key price action levels (Swing High, Swing Low, SMAs) with DuckDB."""
        path = self._parquet_path(instrument_id, timeframe)
        if not path.exists():
            raise FileNotFoundError(f"No historical data for {instrument_id} at {path}")

        sql = f"""
        WITH recent AS (
            SELECT * FROM read_parquet('{path.as_posix()}')
            ORDER BY date DESC
            LIMIT {lookback_bars}
        )
        SELECT
            MIN(low) as swing_low,
            MAX(high) as swing_high,
            AVG(close) as sma_lookback,
            LAST(close) as latest_close
        FROM (SELECT * FROM recent ORDER BY date ASC)
        """
        row = self._con.execute(sql).fetchone()
        if not row:
            raise ValueError(f"Not enough data for {instrument_id}")

        swing_low, swing_high, sma, latest_close = row
        return {
            "instrument_id": instrument_id,
            "lookback_bars": lookback_bars,
            "swing_low": Decimal(f"{swing_low:.2f}"),
            "swing_high": Decimal(f"{swing_high:.2f}"),
            "sma": Decimal(f"{sma:.2f}"),
            "latest_close": Decimal(f"{latest_close:.2f}"),
            "range": Decimal(f"{(swing_high - swing_low):.2f}"),
        }

    def get_multi_horizon_levels(
        self,
        instrument_id: str,
        timeframe: str = "1d",
    ) -> dict[str, Any]:
        """Compute key price action levels across multiple investment horizons (60d, 120d, 200d, 252d, and 5y) with DuckDB."""
        path = self._parquet_path(instrument_id, timeframe)
        if not path.exists():
            raise FileNotFoundError(f"No historical data for {instrument_id} at {path}")

        sql = f"""
        WITH indexed AS (
            SELECT 
                low, high, close, 
                ROW_NUMBER() OVER (ORDER BY date DESC) as rn
            FROM read_parquet('{path.as_posix()}')
        )
        SELECT
            LAST(close) as latest_close,
            -- 60 sesiones (~3 meses)
            MIN(CASE WHEN rn <= 60 THEN low END) as sup_60d,
            MAX(CASE WHEN rn <= 60 THEN high END) as res_60d,
            AVG(CASE WHEN rn <= 60 THEN close END) as sma_60d,
            -- 120 sesiones (~6 meses)
            MIN(CASE WHEN rn <= 120 THEN low END) as sup_120d,
            MAX(CASE WHEN rn <= 120 THEN high END) as res_120d,
            AVG(CASE WHEN rn <= 120 THEN close END) as sma_120d,
            -- 200 sesiones (~10 meses - Media Institucional)
            MIN(CASE WHEN rn <= 200 THEN low END) as sup_200d,
            MAX(CASE WHEN rn <= 200 THEN high END) as res_200d,
            AVG(CASE WHEN rn <= 200 THEN close END) as sma_200d,
            -- 252 sesiones (~1 ano bursatil)
            MIN(CASE WHEN rn <= 252 THEN low END) as sup_252d,
            MAX(CASE WHEN rn <= 252 THEN high END) as res_252d,
            AVG(CASE WHEN rn <= 252 THEN close END) as sma_252d,
            -- Historico completo (5 anos / 1255+ sesiones)
            MIN(low) as sup_max,
            MAX(high) as res_max,
            AVG(close) as sma_max,
            COUNT(*) as total_bars
        FROM (SELECT * FROM indexed ORDER BY rn DESC)
        """
        row = self._con.execute(sql).fetchone()
        if not row:
            raise ValueError(f"Not enough data for {instrument_id}")

        (
            latest_close,
            sup_60d, res_60d, sma_60d,
            sup_120d, res_120d, sma_120d,
            sup_200d, res_200d, sma_200d,
            sup_252d, res_252d, sma_252d,
            sup_max, res_max, sma_max, total_bars
        ) = row

        return {
            "instrument_id": instrument_id,
            "total_bars": total_bars,
            "latest_close": Decimal(f"{latest_close:.2f}"),
            "60d": {
                "swing_low": Decimal(f"{sup_60d:.2f}") if sup_60d else None,
                "swing_high": Decimal(f"{res_60d:.2f}") if res_60d else None,
                "sma": Decimal(f"{sma_60d:.2f}") if sma_60d else None,
            },
            "120d": {
                "swing_low": Decimal(f"{sup_120d:.2f}") if sup_120d else None,
                "swing_high": Decimal(f"{res_120d:.2f}") if res_120d else None,
                "sma": Decimal(f"{sma_120d:.2f}") if sma_120d else None,
            },
            "200d": {
                "swing_low": Decimal(f"{sup_200d:.2f}") if sup_200d else None,
                "swing_high": Decimal(f"{res_200d:.2f}") if res_200d else None,
                "sma": Decimal(f"{sma_200d:.2f}") if sma_200d else None,
            },
            "252d": {
                "swing_low": Decimal(f"{sup_252d:.2f}") if sup_252d else None,
                "swing_high": Decimal(f"{res_252d:.2f}") if res_252d else None,
                "sma": Decimal(f"{sma_252d:.2f}") if sma_252d else None,
            },
            "max": {
                "swing_low": Decimal(f"{sup_max:.2f}") if sup_max else None,
                "swing_high": Decimal(f"{res_max:.2f}") if res_max else None,
                "sma": Decimal(f"{sma_max:.2f}") if sma_max else None,
            },
        }


    def fetch_and_store_cme_mgc(self, period: str = "2y", timeframe: str = "1d") -> Path:
        """Download official CME Micro Gold (MGC=F) data from Yahoo Finance and store in Parquet."""
        return self.fetch_and_store_ticker("MGC=F", instrument_id="MGC", period=period, timeframe=timeframe)

    def fetch_and_store_ticker(
        self,
        ticker: str,
        instrument_id: str | None = None,
        period: str = "5y",
        timeframe: str = "1d",
    ) -> Path:
        """Download official historical data for any stock/ETF/ticker from Yahoo Finance and store in Parquet."""
        import yfinance as yf

        inst_id = instrument_id or ticker
        df = yf.download(ticker, period=period, interval=timeframe, progress=False)
        if df.empty:
            raise RuntimeError(f"Failed to download historical data for {ticker}.")

        return self.save_bars(inst_id, df, timeframe=timeframe)

    def get_price_control_zones(
        self,
        instrument_id: str,
        timeframe: str = "1d",
        min_gap_pct: float = 0.03,
        current_price: float | None = None,
    ) -> dict[str, Any]:
        """Compute historical price control zones (institutional gaps >= 3%) for an instrument."""
        df = self.load_bars(instrument_id, timeframe=timeframe)
        if df.empty:
            return {
                "instrument_id": instrument_id,
                "zones": [],
                "nearest_support": None,
                "nearest_resistance": None,
                "active_zone": None,
                "latest_close": None,
            }
        res = detect_price_control_zones(df, min_gap_pct=min_gap_pct, current_price=current_price)
        res["instrument_id"] = instrument_id
        return res


def detect_price_control_zones(
    df: pd.DataFrame,
    min_gap_pct: float = 0.03,
    current_price: float | None = None,
) -> dict[str, Any]:
    """Detecta huecos (gaps) históricos relevantes (superiores al 3%) y los registra
    como ZONAS DE CONTROL (Soportes/Resistencias institucionales estables).

    Parámetros:
      df: DataFrame con columnas OHLC (date, open, high, low, close).
      min_gap_pct: Umbral mínimo porcentual para considerar un hueco relevante (por defecto 0.03 = 3%).
      current_price: Precio actual de mercado de referencia (por defecto el último cierre).

    Retorna un diccionario con:
      - 'zones': Lista cronológica de todas las zonas de control detectadas.
      - 'nearest_support': Zona de control más cercana por debajo del precio actual (Soporte).
      - 'nearest_resistance': Zona de control más cercana por encima del precio actual (Resistencia).
      - 'active_zone': Zona de control en la que cotiza actualmente el precio (si aplica).
      - 'latest_close': Último precio de cierre evaluado.
    """
    if df is None or not isinstance(df, pd.DataFrame) or df.empty or len(df) < 2:
        return {
            "zones": [],
            "nearest_support": None,
            "nearest_resistance": None,
            "active_zone": None,
            "latest_close": None,
        }

    data = df.copy()
    if isinstance(data.columns, pd.MultiIndex):
        data.columns = [col[0].lower() for col in data.columns]
    else:
        data.columns = [str(col).lower() for col in data.columns]

    if "date" not in data.columns and isinstance(data.index, pd.DatetimeIndex):
        data = data.reset_index()
        if "index" in data.columns and "date" not in data.columns:
            data = data.rename(columns={"index": "date"})
        data.columns = [str(col).lower() for col in data.columns]

    required_cols = {"open", "high", "low", "close"}
    if not required_cols.issubset(set(data.columns)):
        raise ValueError(f"DataFrame must contain columns: {required_cols}. Found: {list(data.columns)}")

    data = data.dropna(subset=list(required_cols)).copy()
    if len(data) < 2:
        return {
            "zones": [],
            "nearest_support": None,
            "nearest_resistance": None,
            "active_zone": None,
            "latest_close": None,
        }

    if "date" in data.columns:
        data["date"] = pd.to_datetime(data["date"], utc=True)
        data = data.sort_values("date").reset_index(drop=True)

    for col in ("open", "high", "low", "close"):
        data[col] = data[col].astype(float)

    ref_price = float(current_price) if current_price is not None else float(data["close"].iloc[-1])

    zones: list[dict[str, Any]] = []
    n = len(data)

    for i in range(1, n):
        prev = data.iloc[i - 1]
        curr = data.iloc[i]

        prev_high = float(prev["high"])
        prev_low = float(prev["low"])
        curr_open = float(curr["open"])
        curr_high = float(curr["high"])
        curr_low = float(curr["low"])

        if prev_high <= 0 or prev_low <= 0:
            continue

        is_bull = curr_low > prev_high * (1.0 + min_gap_pct)
        is_bear = curr_high < prev_low * (1.0 - min_gap_pct)

        if not is_bull and not is_bear:
            continue

        date_val = curr["date"] if "date" in curr else i
        date_str = str(date_val)[:10]

        if is_bull:
            gap_type = "ALCISTA"
            magnitude_pct = round(((curr_low - prev_high) / prev_high) * 100, 2)
            z_low = round(min(curr_low, curr_open), 2)
            z_high = round(max(curr_low, curr_open), 2)
            if z_low == z_high:
                z_low = round(prev_high, 2)
                z_high = round(curr_low, 2)

            subseq = data.iloc[i + 1:]
            closed = bool((subseq["low"] <= z_low).any()) if len(subseq) > 0 else False
            tested = bool((subseq["low"] <= z_high).any()) if len(subseq) > 0 else False
        else:
            gap_type = "BAJISTA"
            magnitude_pct = round(((prev_low - curr_high) / prev_low) * 100, 2)
            z_low = round(min(curr_open, curr_high), 2)
            z_high = round(max(curr_open, curr_high), 2)
            if z_low == z_high:
                z_low = round(curr_high, 2)
                z_high = round(prev_low, 2)

            subseq = data.iloc[i + 1:]
            closed = bool((subseq["high"] >= z_high).any()) if len(subseq) > 0 else False
            tested = bool((subseq["high"] >= z_low).any()) if len(subseq) > 0 else False

        status = "CERRADO" if closed else ("TESTEADO" if tested else "ABIERTO")

        if ref_price > z_high:
            role = "SOPORTE"
            dist_pct = round(((z_high - ref_price) / ref_price) * 100, 2)
        elif ref_price < z_low:
            role = "RESISTENCIA"
            dist_pct = round(((z_low - ref_price) / ref_price) * 100, 2)
        else:
            role = "SOPORTE" if gap_type == "ALCISTA" else "RESISTENCIA"
            dist_pct = 0.0

        zones.append({
            "date": date_str,
            "type": gap_type,
            "lower_bound": z_low,
            "upper_bound": z_high,
            "magnitude_pct": magnitude_pct,
            "status": status,
            "role": role,
            "dist_pct": dist_pct,
            "description": f"${z_low:.2f} - ${z_high:.2f} (Gap {gap_type.capitalize()} {date_str}, {status})",
        })

    # Active zone: price is inside the zone bounds
    active_zones = [z for z in zones if z["lower_bound"] <= ref_price <= z["upper_bound"]]
    active_zone = active_zones[-1] if active_zones else None

    # Nearest support: zone strictly below price (closest to price, so max upper_bound)
    supports = [z for z in zones if z["upper_bound"] <= ref_price]
    nearest_support = max(supports, key=lambda z: (z["upper_bound"], z["lower_bound"])) if supports else None

    # Nearest resistance: zone strictly above price (closest to price, so min lower_bound)
    resistances = [z for z in zones if z["lower_bound"] >= ref_price]
    nearest_resistance = min(resistances, key=lambda z: (z["lower_bound"], z["upper_bound"])) if resistances else None

    return {
        "zones": zones,
        "nearest_support": nearest_support,
        "nearest_resistance": nearest_resistance,
        "active_zone": active_zone,
        "latest_close": round(ref_price, 2),
    }


