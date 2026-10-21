"""Motor de Backtesting Continuo Multi-Ciclo (Rolling Backtest Institucional).

Simula el comportamiento de estrategias macro barra a barra a lo largo de series históricas
completas (5 años, >1.250 sesiones), re-evaluando canales Donchian, medias móviles,
ATR y zonas de control sin incurrir en sesgo de anticipación (zero lookahead bias).
"""

from __future__ import annotations

from dataclasses import asdict, dataclass
import math
from pathlib import Path
from typing import Any, Mapping

import numpy as np
import pandas as pd

from .storage import MarketDataStore, detect_price_control_zones


@dataclass(frozen=True)
class TradeRecord:
    """Registro inmutable de una operación completada en la simulación."""

    trade_id: str
    symbol: str
    direction: str  # 'long' | 'short'
    entry_date: str
    entry_price: float
    exit_date: str
    exit_price: float
    exit_reason: str  # 'take_profit' | 'stop_loss' | 'end_of_data'
    shares: int
    pnl_gross: float
    commission: float
    pnl_net: float
    return_pct: float
    holding_days: int
    entry_support: float
    entry_target: float
    entry_stop: float

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True)
class DailyEquityPoint:
    """Instantánea diaria de patrimonio, posición y drawdown (Mark-to-Market)."""

    date: str
    cash: float
    position_shares: int
    close_price: float
    position_value: float
    nav: float
    unrealized_pnl: float
    daily_return_pct: float
    peak_nav: float
    drawdown_pct: float

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class BacktestResult:
    """Contenedor de resultados de un backtest continuo multi-ciclo."""

    trade_log: pd.DataFrame
    equity_curve: pd.DataFrame
    kpis: dict[str, Any]
    trades: list[TradeRecord]
    daily_points: list[DailyEquityPoint]

    def summary(self) -> str:
        """Genera un resumen formateado de los KPIs del backtest."""
        lines = [
            "=" * 60,
            f"RESULTADOS DE BACKTEST MULTI-CICLO: {self.kpis.get('symbol', 'N/A')}",
            "=" * 60,
            f"Periodo: {self.kpis.get('start_date', '-')} a {self.kpis.get('end_date', '-')} ({self.kpis.get('total_bars', 0)} sesiones)",
            f"Dirección: {self.kpis.get('direction', 'both').upper()} | Capital Inicial: ${self.kpis.get('initial_capital', 0.0):,.2f}",
            "-" * 60,
            f"Patrimonio Final:       ${self.kpis.get('ending_nav', 0.0):,.2f}",
            f"Retorno Neto Total:     ${self.kpis.get('total_pnl_net', 0.0):,.2f} ({self.kpis.get('total_return_pct', 0.0):+.2f}%)",
            f"Comisiones Totales:     ${self.kpis.get('total_commissions', 0.0):,.2f}",
            "-" * 60,
            f"Total Operaciones:      {self.kpis.get('total_trades', 0)} (Ganadoras: {self.kpis.get('winning_trades', 0)}, Perdedoras: {self.kpis.get('losing_trades', 0)})",
            f"Win Rate:               {self.kpis.get('win_rate_pct', 0.0):.2f}%",
            f"Profit Factor:          {self.kpis.get('profit_factor', 0.0):.2f}",
            f"Esperanza Matemática:   ${self.kpis.get('expectancy', 0.0):,.2f} por trade",
            f"Ganancia Media:         ${self.kpis.get('avg_win', 0.0):,.2f}",
            f"Pérdida Media:          ${self.kpis.get('avg_loss', 0.0):,.2f}",
            f"Duración Media (días):  {self.kpis.get('avg_holding_days', 0.0):.1f} días",
            "-" * 60,
            f"Max Drawdown (%):       {self.kpis.get('max_drawdown_pct', 0.0):.2f}%",
            f"Max Drawdown ($):       ${self.kpis.get('max_drawdown_usd', 0.0):,.2f}",
            f"Sharpe Ratio (anual):   {self.kpis.get('sharpe_ratio', 0.0):.2f}",
            f"Sortino Ratio (anual):  {self.kpis.get('sortino_ratio', 0.0):.2f}",
            f"Calmar Ratio:           {self.kpis.get('calmar_ratio', 0.0):.2f}",
            "=" * 60,
        ]
        return "\n".join(lines)

    def to_dict(self) -> dict[str, Any]:
        return {
            "kpis": self.kpis,
            "trades_count": len(self.trades),
            "equity_points_count": len(self.daily_points),
        }


class RollingBacktestEngine:
    """Motor de simulación continua multi-ciclo barra a barra sobre histórico completo."""

    def __init__(self, store: MarketDataStore | None = None):
        self.store = store or MarketDataStore()

    def run(
        self,
        symbol: str,
        initial_capital: float = 100_000.0,
        lookback_bars: int = 60,
        direction: str = "both",  # 'long', 'short', 'both'
        shares: int | None = None,
        risk_pct: float = 0.015,
        use_atr_sizing: bool = False,
        start_date: str | None = None,
        end_date: str | None = None,
        slippage_bps: float = 5.0,
        commission_per_unit: float | None = None,
        multiplier: float | None = None,
        auto_close_at_end: bool = True,
        use_control_zones: bool = False,
    ) -> BacktestResult:
        """Ejecuta la simulación multi-ciclo de 5 años barra a barra con cero anticipación."""
        if lookback_bars < 5:
            raise ValueError(f"lookback_bars debe ser al menos 5, recibido: {lookback_bars}")
        if direction not in ("long", "short", "both"):
            raise ValueError(f"Dirección no válida: {direction}. Usar 'long', 'short' o 'both'")

        sym_upper = symbol.strip().upper()
        bars_df = self.store.load_bars(sym_upper, timeframe="1d")
        if bars_df.empty:
            raise ValueError(f"No hay barras históricas para el símbolo '{sym_upper}' en MarketDataStore")

        # Asegurar orden cronológico estricto
        bars_df = bars_df.copy()
        bars_df["date"] = pd.to_datetime(bars_df["date"], utc=True)
        bars_df = bars_df.sort_values("date").reset_index(drop=True)

        if len(bars_df) <= lookback_bars:
            raise ValueError(
                f"El dataset contiene {len(bars_df)} barras, insuficientes para el lookback de {lookback_bars}"
            )

        # Validación estricta del filtro temporal
        start_ts = pd.to_datetime(start_date, utc=True) if start_date else None
        end_ts = pd.to_datetime(end_date, utc=True) if end_date else None

        if start_ts is not None and end_ts is not None and end_ts < start_ts:
            raise ValueError(
                f"end_date ({end_date}) no puede ser anterior a start_date ({start_date})"
            )
        if start_ts is not None and start_ts > bars_df["date"].iloc[-1]:
            raise ValueError(
                f"start_date ({start_date}) es posterior al último dato histórico disponible ({bars_df['date'].iloc[-1].strftime('%Y-%m-%d')})"
            )
        if end_ts is not None and end_ts < bars_df["date"].iloc[0]:
            raise ValueError(
                f"end_date ({end_date}) es anterior al primer dato histórico disponible ({bars_df['date'].iloc[0].strftime('%Y-%m-%d')})"
            )

        # Configuración de comisiones y multiplicador por clase de activo
        is_future = sym_upper in ("MGC", "MGC=F")
        mult = float(multiplier) if multiplier is not None else (10.0 if is_future else 1.0)
        comm_unit = (
            float(commission_per_unit)
            if commission_per_unit is not None
            else (0.50 if is_future else 0.005)
        )
        slip_rate = float(slippage_bps) / 10_000.0

        # CÁLCULO VECTORIAL DE INDICADORES CON CERO SESGO DE ANTICIPACIÓN (Zero Lookahead Bias)
        # En la barra t, los niveles se basan ESTRICTAMENTE en la ventana móvil [t-lookback : t-1].
        # .shift(1) desplaza el histórico para que la fila t jamás contemple datos de la sesión t.
        swing_lows = bars_df["low"].shift(1).rolling(lookback_bars).min().values
        swing_highs = bars_df["high"].shift(1).rolling(lookback_bars).max().values
        smas = bars_df["close"].shift(1).rolling(lookback_bars).mean().values
        smas_200 = bars_df["close"].shift(1).rolling(200, min_periods=lookback_bars).mean().values

        # True Range y ATR(14) desplazados estrictamente por 1 sesión
        prev_close_series = bars_df["close"].shift(1)
        tr1 = bars_df["high"] - bars_df["low"]
        tr2 = (bars_df["high"] - prev_close_series).abs()
        tr3 = (bars_df["low"] - prev_close_series).abs()
        tr = pd.concat([tr1, tr2, tr3], axis=1).max(axis=1)
        atrs = tr.shift(1).rolling(14, min_periods=1).mean().values

        # Zonas de control por gaps institucionales (>=3%) calculadas con cero anticipación
        control_zones_data = None
        if use_control_zones:
            control_zones_data = detect_price_control_zones(bars_df)

        n_bars = len(bars_df)
        cash = float(initial_capital)
        peak_nav = float(initial_capital)

        # Variables de estado de posición
        in_position = False
        pos_side: str | None = None
        pos_shares: int = 0
        pos_entry_price: float = 0.0
        pos_entry_date: str = ""
        pos_entry_support: float = 0.0
        pos_entry_target: float = 0.0
        pos_entry_stop: float = 0.0
        pos_entry_comm: float = 0.0

        trades: list[TradeRecord] = []
        equity_points: list[DailyEquityPoint] = []
        trade_counter = 0

        for t in range(n_bars):
            bar_date_ts = bars_df["date"].iloc[t]
            bar_date_str = bar_date_ts.strftime("%Y-%m-%d")

            # 1. Terminar inmediatamente si superamos end_date
            if end_ts is not None and bar_date_ts > end_ts:
                break

            # 2. Si se especificó start_date y aún no hemos llegado, la barra sirve como
            # período de calentamiento para indicadores técnicos sin contaminar equity_points
            if start_ts is not None and bar_date_ts < start_ts:
                continue

            open_p = float(bars_df["open"].iloc[t])
            high_p = float(bars_df["high"].iloc[t])
            low_p = float(bars_df["low"].iloc[t])
            close_p = float(bars_df["close"].iloc[t])

            # 3. Período de calentamiento si no hay suficientes barras para indicadores
            if t < lookback_bars:
                equity_points.append(
                    DailyEquityPoint(
                        date=bar_date_str,
                        cash=round(cash, 2),
                        position_shares=0,
                        close_price=round(close_p, 2),
                        position_value=0.0,
                        nav=round(cash, 2),
                        unrealized_pnl=0.0,
                        daily_return_pct=0.0,
                        peak_nav=round(peak_nav, 2),
                        drawdown_pct=0.0,
                    )
                )
                continue

            sup_t = float(swing_lows[t])
            res_t = float(swing_highs[t])
            sma_t = float(smas[t])
            atr_t = float(atrs[t]) if not math.isnan(atrs[t]) else max(close_p * 0.02, 0.01)

            # ----------------------------------------------------
            # 1. EVALUAR SALIDAS SI ESTAMOS EN POSICIÓN AL INICIO DE LA BARRA
            # ----------------------------------------------------
            exited_this_bar = False
            if in_position:
                exited = False
                exit_reason = ""
                raw_exit_p = 0.0

                if pos_side == "long":
                    hit_sl = low_p <= pos_entry_stop
                    hit_tp = high_p >= pos_entry_target

                    # Convención conservadora: si se tocan ambos en la misma sesión, se ejecuta Stop Loss primero
                    if hit_sl:
                        exited = True
                        exit_reason = "stop_loss"
                        raw_exit_p = min(open_p, pos_entry_stop)
                    elif hit_tp:
                        exited = True
                        exit_reason = "take_profit"
                        raw_exit_p = max(open_p, pos_entry_target)

                    if exited:
                        exec_exit_p = round(raw_exit_p * (1.0 - slip_rate), 2)
                        exit_comm = round(comm_unit * pos_shares, 2)
                        pnl_gross = round((exec_exit_p - pos_entry_price) * pos_shares * mult, 2)
                        total_comm = round(pos_entry_comm + exit_comm, 2)
                        pnl_net = round(pnl_gross - total_comm, 2)
                        ret_pct = round(((exec_exit_p - pos_entry_price) / max(pos_entry_price, 1e-6)) * 100, 2)
                        h_days = (pd.to_datetime(bar_date_str) - pd.to_datetime(pos_entry_date)).days

                        trade_counter += 1
                        trades.append(
                            TradeRecord(
                                trade_id=f"TR-{sym_upper}-{trade_counter:04d}",
                                symbol=sym_upper,
                                direction="long",
                                entry_date=pos_entry_date,
                                entry_price=pos_entry_price,
                                exit_date=bar_date_str,
                                exit_price=exec_exit_p,
                                exit_reason=exit_reason,
                                shares=pos_shares,
                                pnl_gross=pnl_gross,
                                commission=total_comm,
                                pnl_net=pnl_net,
                                return_pct=ret_pct,
                                holding_days=h_days,
                                entry_support=pos_entry_support,
                                entry_target=pos_entry_target,
                                entry_stop=pos_entry_stop,
                            )
                        )
                        cash = round(cash + pnl_gross - exit_comm, 2)
                        in_position = False
                        pos_side = None
                        pos_shares = 0
                        exited_this_bar = True

                elif pos_side == "short":
                    hit_sl = high_p >= pos_entry_stop
                    hit_tp = low_p <= pos_entry_target

                    # Convención conservadora: si se tocan ambos, Stop Loss primero
                    if hit_sl:
                        exited = True
                        exit_reason = "stop_loss"
                        raw_exit_p = max(open_p, pos_entry_stop)
                    elif hit_tp:
                        exited = True
                        exit_reason = "take_profit"
                        raw_exit_p = min(open_p, pos_entry_target)

                    if exited:
                        exec_exit_p = round(raw_exit_p * (1.0 + slip_rate), 2)
                        exit_comm = round(comm_unit * pos_shares, 2)
                        pnl_gross = round((pos_entry_price - exec_exit_p) * pos_shares * mult, 2)
                        total_comm = round(pos_entry_comm + exit_comm, 2)
                        pnl_net = round(pnl_gross - total_comm, 2)
                        ret_pct = round(((pos_entry_price - exec_exit_p) / max(pos_entry_price, 1e-6)) * 100, 2)
                        h_days = (pd.to_datetime(bar_date_str) - pd.to_datetime(pos_entry_date)).days

                        trade_counter += 1
                        trades.append(
                            TradeRecord(
                                trade_id=f"TR-{sym_upper}-{trade_counter:04d}",
                                symbol=sym_upper,
                                direction="short",
                                entry_date=pos_entry_date,
                                entry_price=pos_entry_price,
                                exit_date=bar_date_str,
                                exit_price=exec_exit_p,
                                exit_reason=exit_reason,
                                shares=pos_shares,
                                pnl_gross=pnl_gross,
                                commission=total_comm,
                                pnl_net=pnl_net,
                                return_pct=ret_pct,
                                holding_days=h_days,
                                entry_support=pos_entry_support,
                                entry_target=pos_entry_target,
                                entry_stop=pos_entry_stop,
                            )
                        )
                        cash = round(cash + pnl_gross - exit_comm, 2)
                        in_position = False
                        pos_side = None
                        pos_shares = 0
                        exited_this_bar = True

            # ----------------------------------------------------
            # 2. EVALUAR ENTRADAS SI ESTAMOS EN ESPERA (IDLE)
            # Una sesión donde ya se cerró una posición previa no puede abrir una nueva
            # en la misma barra (garantía de no-anticipación ni solapamiento causal).
            # ----------------------------------------------------
            if not in_position and not exited_this_bar:
                if use_control_zones and control_zones_data and "zones" in control_zones_data:
                    past_zones = [
                        z for z in control_zones_data["zones"]
                        if pd.to_datetime(z["date"], utc=True) < bar_date_ts
                    ]
                    bull_gaps = [z for z in past_zones if z["type"] == "ALCISTA" and z["upper_bound"] < close_p]
                    if bull_gaps:
                        nearest_gap = max(z["upper_bound"] for z in bull_gaps)
                        if abs(nearest_gap - sup_t) / max(sup_t, 1.0) < 0.05:
                            sup_t = nearest_gap
                    bear_gaps = [z for z in past_zones if z["type"] == "BAJISTA" and z["lower_bound"] > close_p]
                    if bear_gaps:
                        nearest_bear = min(z["lower_bound"] for z in bear_gaps)
                        if abs(nearest_bear - res_t) / max(res_t, 1.0) < 0.05:
                            res_t = nearest_bear

                # Niveles técnicos de la sesión calculados exclusivamente hasta t-1
                entry_thresh_long = round(sup_t * 1.03, 2)
                sl_long = round(sup_t * 0.95, 2)
                tp_long = round(res_t, 2)

                entry_thresh_short = round(res_t * 0.97, 2)
                sl_short = round(res_t * 1.05, 2)
                tp_short = round(sup_t, 2)

                can_long = (
                    direction in ("long", "both")
                    and (low_p <= entry_thresh_long)
                    and (high_p > sl_long)
                    and (open_p > sl_long)
                )
                can_short = (
                    direction in ("short", "both")
                    and (high_p >= entry_thresh_short)
                    and (low_p < sl_short)
                    and (open_p < sl_short)
                )

                # Si ambos setups coinciden en volatilidad extrema, resolver por posición frente a SMA
                if can_long and can_short:
                    if open_p < sma_t:
                        can_short = False
                    else:
                        can_long = False

                if can_long:
                    # Cálculo de tamaño de posición
                    if shares is not None:
                        trade_shares = int(shares)
                    elif use_atr_sizing:
                        risk_amt = initial_capital * risk_pct
                        risk_per_share = max(2.0 * atr_t, 0.1) * mult
                        trade_shares = max(1, int(risk_amt / risk_per_share))
                    else:
                        risk_amt = initial_capital * risk_pct
                        risk_per_share = max(entry_thresh_long - sl_long, 0.1) * mult
                        trade_shares = max(1, int(risk_amt / risk_per_share))

                    raw_entry_p = open_p if open_p <= entry_thresh_long else entry_thresh_long
                    exec_entry_p = round(raw_entry_p * (1.0 + slip_rate), 2)
                    entry_comm = round(comm_unit * trade_shares, 2)

                    in_position = True
                    pos_side = "long"
                    pos_shares = trade_shares
                    pos_entry_price = exec_entry_p
                    pos_entry_date = bar_date_str
                    pos_entry_support = sup_t
                    pos_entry_target = tp_long
                    pos_entry_stop = sl_long
                    pos_entry_comm = entry_comm
                    cash = round(cash - entry_comm, 2)

                    # Comprobación de salida en la misma barra tras entrar
                    if low_p <= pos_entry_stop:
                        raw_exit_p = pos_entry_stop
                        exec_exit_p = round(raw_exit_p * (1.0 - slip_rate), 2)
                        exit_comm = round(comm_unit * pos_shares, 2)
                        pnl_gross = round((exec_exit_p - pos_entry_price) * pos_shares * mult, 2)
                        total_comm = round(pos_entry_comm + exit_comm, 2)
                        pnl_net = round(pnl_gross - total_comm, 2)
                        ret_pct = round(((exec_exit_p - pos_entry_price) / pos_entry_price) * 100, 2)

                        trade_counter += 1
                        trades.append(
                            TradeRecord(
                                trade_id=f"TR-{sym_upper}-{trade_counter:04d}",
                                symbol=sym_upper,
                                direction="long",
                                entry_date=pos_entry_date,
                                entry_price=pos_entry_price,
                                exit_date=bar_date_str,
                                exit_price=exec_exit_p,
                                exit_reason="stop_loss",
                                shares=pos_shares,
                                pnl_gross=pnl_gross,
                                commission=total_comm,
                                pnl_net=pnl_net,
                                return_pct=ret_pct,
                                holding_days=0,
                                entry_support=pos_entry_support,
                                entry_target=pos_entry_target,
                                entry_stop=pos_entry_stop,
                            )
                        )
                        cash = round(cash + pnl_gross - exit_comm, 2)
                        in_position = False
                        pos_side = None
                        pos_shares = 0

                    elif high_p >= pos_entry_target:
                        raw_exit_p = pos_entry_target
                        exec_exit_p = round(raw_exit_p * (1.0 - slip_rate), 2)
                        exit_comm = round(comm_unit * pos_shares, 2)
                        pnl_gross = round((exec_exit_p - pos_entry_price) * pos_shares * mult, 2)
                        total_comm = round(pos_entry_comm + exit_comm, 2)
                        pnl_net = round(pnl_gross - total_comm, 2)
                        ret_pct = round(((exec_exit_p - pos_entry_price) / pos_entry_price) * 100, 2)

                        trade_counter += 1
                        trades.append(
                            TradeRecord(
                                trade_id=f"TR-{sym_upper}-{trade_counter:04d}",
                                symbol=sym_upper,
                                direction="long",
                                entry_date=pos_entry_date,
                                entry_price=pos_entry_price,
                                exit_date=bar_date_str,
                                exit_price=exec_exit_p,
                                exit_reason="take_profit",
                                shares=pos_shares,
                                pnl_gross=pnl_gross,
                                commission=total_comm,
                                pnl_net=pnl_net,
                                return_pct=ret_pct,
                                holding_days=0,
                                entry_support=pos_entry_support,
                                entry_target=pos_entry_target,
                                entry_stop=pos_entry_stop,
                            )
                        )
                        cash = round(cash + pnl_gross - exit_comm, 2)
                        in_position = False
                        pos_side = None
                        pos_shares = 0

                elif can_short:
                    if shares is not None:
                        trade_shares = int(shares)
                    elif use_atr_sizing:
                        risk_amt = initial_capital * risk_pct
                        risk_per_share = max(2.0 * atr_t, 0.1) * mult
                        trade_shares = max(1, int(risk_amt / risk_per_share))
                    else:
                        risk_amt = initial_capital * risk_pct
                        risk_per_share = max(sl_short - entry_thresh_short, 0.1) * mult
                        trade_shares = max(1, int(risk_amt / risk_per_share))

                    raw_entry_p = open_p if open_p >= entry_thresh_short else entry_thresh_short
                    exec_entry_p = round(raw_entry_p * (1.0 - slip_rate), 2)
                    entry_comm = round(comm_unit * trade_shares, 2)

                    in_position = True
                    pos_side = "short"
                    pos_shares = trade_shares
                    pos_entry_price = exec_entry_p
                    pos_entry_date = bar_date_str
                    pos_entry_support = sup_t
                    pos_entry_target = tp_short
                    pos_entry_stop = sl_short
                    pos_entry_comm = entry_comm
                    cash = round(cash - entry_comm, 2)

                    # Comprobación de salida en la misma barra tras entrar
                    if high_p >= pos_entry_stop:
                        raw_exit_p = pos_entry_stop
                        exec_exit_p = round(raw_exit_p * (1.0 + slip_rate), 2)
                        exit_comm = round(comm_unit * pos_shares, 2)
                        pnl_gross = round((pos_entry_price - exec_exit_p) * pos_shares * mult, 2)
                        total_comm = round(pos_entry_comm + exit_comm, 2)
                        pnl_net = round(pnl_gross - total_comm, 2)
                        ret_pct = round(((pos_entry_price - exec_exit_p) / pos_entry_price) * 100, 2)

                        trade_counter += 1
                        trades.append(
                            TradeRecord(
                                trade_id=f"TR-{sym_upper}-{trade_counter:04d}",
                                symbol=sym_upper,
                                direction="short",
                                entry_date=pos_entry_date,
                                entry_price=pos_entry_price,
                                exit_date=bar_date_str,
                                exit_price=exec_exit_p,
                                exit_reason="stop_loss",
                                shares=pos_shares,
                                pnl_gross=pnl_gross,
                                commission=total_comm,
                                pnl_net=pnl_net,
                                return_pct=ret_pct,
                                holding_days=0,
                                entry_support=pos_entry_support,
                                entry_target=pos_entry_target,
                                entry_stop=pos_entry_stop,
                            )
                        )
                        cash = round(cash + pnl_gross - exit_comm, 2)
                        in_position = False
                        pos_side = None
                        pos_shares = 0

                    elif low_p <= pos_entry_target:
                        raw_exit_p = pos_entry_target
                        exec_exit_p = round(raw_exit_p * (1.0 + slip_rate), 2)
                        exit_comm = round(comm_unit * pos_shares, 2)
                        pnl_gross = round((pos_entry_price - exec_exit_p) * pos_shares * mult, 2)
                        total_comm = round(pos_entry_comm + exit_comm, 2)
                        pnl_net = round(pnl_gross - total_comm, 2)
                        ret_pct = round(((pos_entry_price - exec_exit_p) / pos_entry_price) * 100, 2)

                        trade_counter += 1
                        trades.append(
                            TradeRecord(
                                trade_id=f"TR-{sym_upper}-{trade_counter:04d}",
                                symbol=sym_upper,
                                direction="short",
                                entry_date=pos_entry_date,
                                entry_price=pos_entry_price,
                                exit_date=bar_date_str,
                                exit_price=exec_exit_p,
                                exit_reason="take_profit",
                                shares=pos_shares,
                                pnl_gross=pnl_gross,
                                commission=total_comm,
                                pnl_net=pnl_net,
                                return_pct=ret_pct,
                                holding_days=0,
                                entry_support=pos_entry_support,
                                entry_target=pos_entry_target,
                                entry_stop=pos_entry_stop,
                            )
                        )
                        cash = round(cash + pnl_gross - exit_comm, 2)
                        in_position = False
                        pos_side = None
                        pos_shares = 0

            # ----------------------------------------------------
            # 3. CIERRE FINAL DE POSICIÓN SI ES LA ÚLTIMA BARRA
            # ----------------------------------------------------
            is_last_bar = (t == n_bars - 1) or (end_ts is not None and t < n_bars - 1 and bars_df["date"].iloc[t+1] > end_ts)
            if in_position and is_last_bar and auto_close_at_end:
                exit_reason = "end_of_data"
                if pos_side == "long":
                    exec_exit_p = round(close_p * (1.0 - slip_rate), 2)
                    exit_comm = round(comm_unit * pos_shares, 2)
                    pnl_gross = round((exec_exit_p - pos_entry_price) * pos_shares * mult, 2)
                    ret_pct = round(((exec_exit_p - pos_entry_price) / pos_entry_price) * 100, 2)
                else:
                    exec_exit_p = round(close_p * (1.0 + slip_rate), 2)
                    exit_comm = round(comm_unit * pos_shares, 2)
                    pnl_gross = round((pos_entry_price - exec_exit_p) * pos_shares * mult, 2)
                    ret_pct = round(((pos_entry_price - exec_exit_p) / pos_entry_price) * 100, 2)

                total_comm = round(pos_entry_comm + exit_comm, 2)
                pnl_net = round(pnl_gross - total_comm, 2)
                h_days = (pd.to_datetime(bar_date_str) - pd.to_datetime(pos_entry_date)).days

                trade_counter += 1
                trades.append(
                    TradeRecord(
                        trade_id=f"TR-{sym_upper}-{trade_counter:04d}",
                        symbol=sym_upper,
                        direction=pos_side,
                        entry_date=pos_entry_date,
                        entry_price=pos_entry_price,
                        exit_date=bar_date_str,
                        exit_price=exec_exit_p,
                        exit_reason=exit_reason,
                        shares=pos_shares,
                        pnl_gross=pnl_gross,
                        commission=total_comm,
                        pnl_net=pnl_net,
                        return_pct=ret_pct,
                        holding_days=h_days,
                        entry_support=pos_entry_support,
                        entry_target=pos_entry_target,
                        entry_stop=pos_entry_stop,
                    )
                )
                cash = round(cash + pnl_gross - exit_comm, 2)
                in_position = False
                pos_side = None
                pos_shares = 0

            # ----------------------------------------------------
            # 4. MARK-TO-MARKET DIARIO Y CURVA DE PATRIMONIO
            # ----------------------------------------------------
            if in_position:
                if pos_side == "long":
                    unrealized_pnl = round((close_p - pos_entry_price) * pos_shares * mult, 2)
                    pos_sh = pos_shares
                    pos_val = round(pos_shares * close_p * mult, 2)
                else:
                    unrealized_pnl = round((pos_entry_price - close_p) * pos_shares * mult, 2)
                    pos_sh = -pos_shares
                    pos_val = round(-pos_shares * close_p * mult, 2)
                nav = round(cash + unrealized_pnl, 2)
            else:
                pos_sh = 0
                pos_val = 0.0
                unrealized_pnl = 0.0
                nav = round(cash, 2)

            prev_nav = equity_points[-1].nav if equity_points else initial_capital
            daily_ret_pct = round(((nav - prev_nav) / prev_nav) * 100, 4) if prev_nav > 0 else 0.0

            if nav > peak_nav:
                peak_nav = nav

            drawdown_pct = round(((nav - peak_nav) / peak_nav) * 100, 4) if peak_nav > 0 else 0.0
            if drawdown_pct > 0.0:
                drawdown_pct = 0.0

            equity_points.append(
                DailyEquityPoint(
                    date=bar_date_str,
                    cash=round(cash, 2),
                    position_shares=pos_sh,
                    close_price=round(close_p, 2),
                    position_value=round(pos_val, 2),
                    nav=round(nav, 2),
                    unrealized_pnl=round(unrealized_pnl, 2),
                    daily_return_pct=daily_ret_pct,
                    peak_nav=round(peak_nav, 2),
                    drawdown_pct=drawdown_pct,
                )
            )

        # Construcción de DataFrames de resultados
        trade_log_df = pd.DataFrame([t.to_dict() for t in trades])
        equity_curve_df = pd.DataFrame([e.to_dict() for e in equity_points])

        # Cálculo de KPIs institucionales
        kpis = self._compute_kpis(
            symbol=sym_upper,
            direction=direction,
            initial_capital=initial_capital,
            trade_log=trade_log_df,
            equity_curve=equity_curve_df,
        )

        return BacktestResult(
            trade_log=trade_log_df,
            equity_curve=equity_curve_df,
            kpis=kpis,
            trades=trades,
            daily_points=equity_points,
        )

    def _compute_kpis(
        self,
        symbol: str,
        direction: str,
        initial_capital: float,
        trade_log: pd.DataFrame,
        equity_curve: pd.DataFrame,
    ) -> dict[str, Any]:
        """Calcula métricas institucionales completas de rendimiento y riesgo."""
        total_trades = len(trade_log)
        start_date = equity_curve["date"].iloc[0] if not equity_curve.empty else "-"
        end_date = equity_curve["date"].iloc[-1] if not equity_curve.empty else "-"
        total_bars = len(equity_curve)

        if total_trades > 0:
            winning_trades = int((trade_log["pnl_net"] > 0).sum())
            losing_trades = int((trade_log["pnl_net"] < 0).sum())
            scratch_trades = int((trade_log["pnl_net"] == 0).sum())
            win_rate_pct = round((winning_trades / total_trades) * 100, 2)
            total_pnl_gross = round(float(trade_log["pnl_gross"].sum()), 2)
            total_commissions = round(float(trade_log["commission"].sum()), 2)
            total_pnl_net = round(float(trade_log["pnl_net"].sum()), 2)

            gross_wins = float(trade_log[trade_log["pnl_net"] > 0]["pnl_net"].sum())
            gross_losses = abs(float(trade_log[trade_log["pnl_net"] < 0]["pnl_net"].sum()))

            if gross_losses > 0:
                profit_factor = round(gross_wins / gross_losses, 2)
            elif gross_wins > 0:
                profit_factor = float("inf")
            else:
                profit_factor = 0.0

            expectancy = round(float(trade_log["pnl_net"].mean()), 2)
            avg_win = (
                round(float(trade_log[trade_log["pnl_net"] > 0]["pnl_net"].mean()), 2)
                if winning_trades > 0
                else 0.0
            )
            avg_loss = (
                round(float(trade_log[trade_log["pnl_net"] < 0]["pnl_net"].mean()), 2)
                if losing_trades > 0
                else 0.0
            )
            win_loss_ratio = round(abs(avg_win / avg_loss), 2) if avg_loss != 0 else 0.0
            avg_holding_days = round(float(trade_log["holding_days"].mean()), 1)
        else:
            winning_trades = 0
            losing_trades = 0
            scratch_trades = 0
            win_rate_pct = 0.0
            total_pnl_gross = 0.0
            total_commissions = 0.0
            total_pnl_net = 0.0
            profit_factor = 0.0
            expectancy = 0.0
            avg_win = 0.0
            avg_loss = 0.0
            win_loss_ratio = 0.0
            avg_holding_days = 0.0

        ending_nav = round(float(equity_curve["nav"].iloc[-1]), 2) if not equity_curve.empty else initial_capital
        total_return_pct = round(((ending_nav - initial_capital) / initial_capital) * 100, 2)

        # Drawdown
        if not equity_curve.empty:
            max_drawdown_pct = round(abs(float(equity_curve["drawdown_pct"].min())), 2)
            dd_usd_series = equity_curve["peak_nav"] - equity_curve["nav"]
            max_drawdown_usd = round(float(dd_usd_series.max()), 2)
        else:
            max_drawdown_pct = 0.0
            max_drawdown_usd = 0.0

        # Ratios de Rentabilidad Ajustada al Riesgo (Sharpe, Sortino, Calmar)
        if not equity_curve.empty and len(equity_curve) > 1:
            daily_returns = (equity_curve["daily_return_pct"] / 100.0).values
            mean_ret = float(np.mean(daily_returns))
            std_ret = float(np.std(daily_returns, ddof=1)) if len(daily_returns) > 1 else 0.0

            if std_ret > 1e-8:
                sharpe_ratio = round(float((mean_ret / std_ret) * math.sqrt(252)), 2)
            else:
                sharpe_ratio = 0.0

            # Desviación a la baja semi-varianza estándar (Sortino):
            # Suma de retornos negativos al cuadrado dividida por el total de observaciones N
            downside_diff = np.minimum(0.0, daily_returns)
            downside_std = float(np.sqrt(np.mean(downside_diff ** 2)))
            if downside_std > 1e-8:
                sortino_ratio = round(float((mean_ret / downside_std) * math.sqrt(252)), 2)
            else:
                sortino_ratio = 99.99 if mean_ret > 0 else 0.0

            # Calmar Ratio: CAGR / Max Drawdown
            years = max(total_bars / 252.0, 0.1)
            if ending_nav > 0:
                cagr = round(((ending_nav / initial_capital) ** (1.0 / years) - 1.0) * 100, 2)
            else:
                cagr = -100.0

            if max_drawdown_pct > 0:
                calmar_ratio = round(cagr / max_drawdown_pct, 2)
            else:
                calmar_ratio = 0.0

            # Normalizar -0.0 a 0.0 derivado del redondeo con punto flotante
            if abs(sharpe_ratio) < 1e-4:
                sharpe_ratio = 0.0
            if abs(sortino_ratio) < 1e-4:
                sortino_ratio = 0.0
            if abs(calmar_ratio) < 1e-4:
                calmar_ratio = 0.0
        else:
            sharpe_ratio = 0.0
            sortino_ratio = 0.0
            calmar_ratio = 0.0
            cagr = 0.0

        return {
            "symbol": symbol,
            "direction": direction,
            "start_date": start_date,
            "end_date": end_date,
            "total_bars": total_bars,
            "initial_capital": initial_capital,
            "ending_nav": ending_nav,
            "total_pnl_gross": total_pnl_gross,
            "total_commissions": total_commissions,
            "total_pnl_net": total_pnl_net,
            "total_return_pct": total_return_pct,
            "cagr_pct": cagr,
            "total_trades": total_trades,
            "winning_trades": winning_trades,
            "losing_trades": losing_trades,
            "scratch_trades": scratch_trades,
            "win_rate_pct": win_rate_pct,
            "profit_factor": profit_factor,
            "expectancy": expectancy,
            "avg_win": avg_win,
            "avg_loss": avg_loss,
            "win_loss_ratio": win_loss_ratio,
            "avg_holding_days": avg_holding_days,
            "max_drawdown_pct": max_drawdown_pct,
            "max_drawdown_usd": max_drawdown_usd,
            "sharpe_ratio": sharpe_ratio,
            "sortino_ratio": sortino_ratio,
            "calmar_ratio": calmar_ratio,
        }
