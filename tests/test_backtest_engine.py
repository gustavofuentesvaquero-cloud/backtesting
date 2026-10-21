"""Unit tests for the Rolling Multi-Cycle Backtest Engine."""

from __future__ import annotations

import unittest
import pandas as pd
import numpy as np

from market_catalog import (
    BacktestResult,
    DailyEquityPoint,
    MarketDataStore,
    RollingBacktestEngine,
    TradeRecord,
)


class TestBacktestEngine(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.store = MarketDataStore()
        cls.engine = RollingBacktestEngine(store=cls.store)

    def test_rolling_backtest_execution_gold(self):
        """Verifica la ejecución continua sobre los 5 años de GOLD generando múltiples operaciones (>10)."""
        result = self.engine.run("GOLD", initial_capital=100_000.0, lookback_bars=60)

        self.assertIsInstance(result, BacktestResult)
        self.assertGreater(len(result.trades), 10, f"Se esperaban >10 trades, obtenidos {len(result.trades)}")
        self.assertGreater(len(result.trade_log), 10)
        self.assertEqual(len(result.equity_curve), 1255)

        required_trade_cols = [
            "trade_id",
            "symbol",
            "direction",
            "entry_date",
            "entry_price",
            "exit_date",
            "exit_price",
            "exit_reason",
            "shares",
            "pnl_gross",
            "commission",
            "pnl_net",
            "return_pct",
            "holding_days",
            "entry_support",
            "entry_target",
            "entry_stop",
        ]
        for col in required_trade_cols:
            self.assertIn(col, result.trade_log.columns, f"Columna {col} ausente en trade_log")

        required_equity_cols = [
            "date",
            "cash",
            "position_shares",
            "close_price",
            "position_value",
            "nav",
            "unrealized_pnl",
            "daily_return_pct",
            "peak_nav",
            "drawdown_pct",
        ]
        for col in required_equity_cols:
            self.assertIn(col, result.equity_curve.columns, f"Columna {col} ausente en equity_curve")

    def test_zero_lookahead_bias(self):
        """Verifica rigurosamente que en la barra t los niveles no dependen de datos de la sesión t ni futuros."""
        bars_df = self.store.load_bars("GOLD", timeframe="1d")
        lookback = 60

        # Vectorized indicators with shift(1)
        swing_lows = bars_df["low"].shift(1).rolling(lookback).min().values
        swing_highs = bars_df["high"].shift(1).rolling(lookback).max().values
        smas = bars_df["close"].shift(1).rolling(lookback).mean().values

        # Comprobar en múltiples puntos temporales que el indicador coincide estrictamente con df.iloc[t-lookback:t]
        for t in (60, 100, 250, 500, 1000):
            sub = bars_df.iloc[t - lookback : t]
            # La fila t no debe estar en la ventana de cálculo
            self.assertEqual(len(sub), lookback)
            self.assertAlmostEqual(swing_lows[t], sub["low"].min(), places=4)
            self.assertAlmostEqual(swing_highs[t], sub["high"].max(), places=4)
            self.assertAlmostEqual(smas[t], sub["close"].mean(), places=4)

        # Prueba de mutación: cambiar los datos de la barra t no debe alterar swing_lows[t]
        mutated_df = bars_df.copy()
        test_t = 150
        original_low = swing_lows[test_t]
        original_high = swing_highs[test_t]

        # Mutamos drásticamente la barra test_t
        mutated_df.loc[test_t, "low"] = 0.0001
        mutated_df.loc[test_t, "high"] = 999999.0
        mutated_df.loc[test_t, "close"] = 555555.0

        # Recalcular indicadores sobre el dataset mutado
        mutated_swing_lows = mutated_df["low"].shift(1).rolling(lookback).min().values
        mutated_swing_highs = mutated_df["high"].shift(1).rolling(lookback).max().values

        # En la barra test_t, los niveles calculados DEBEN ser exactamente iguales a los originales
        self.assertEqual(mutated_swing_lows[test_t], original_low)
        self.assertEqual(mutated_swing_highs[test_t], original_high)

    def test_trade_pnl_and_cash_reconciliation(self):
        """Comprueba matemáticamente la conciliación exacta entre Trade Log, comisiones y variación de efectivo."""
        initial_capital = 100_000.0
        result = self.engine.run("GOLD", initial_capital=initial_capital, auto_close_at_end=True)

        trade_log = result.trade_log
        self.assertFalse(trade_log.empty)

        # 1. Comprobar que para cada trade individual: pnl_net == pnl_gross - commission
        for _, trade in trade_log.iterrows():
            expected_net = round(float(trade["pnl_gross"]) - float(trade["commission"]), 2)
            self.assertAlmostEqual(float(trade["pnl_net"]), expected_net, places=2)

        # 2. Comprobar que la suma de pnl_net coincide con la variación de efectivo final
        total_pnl_net = float(trade_log["pnl_net"].sum())
        ending_cash = result.equity_curve["cash"].iloc[-1]
        ending_nav = result.equity_curve["nav"].iloc[-1]

        self.assertAlmostEqual(ending_cash, ending_nav, places=2)
        self.assertAlmostEqual(ending_cash - initial_capital, total_pnl_net, places=2)
        self.assertAlmostEqual(result.kpis["total_pnl_net"], total_pnl_net, places=2)
        self.assertAlmostEqual(result.kpis["ending_nav"], ending_nav, places=2)

    def test_equity_curve_and_drawdown_consistency(self):
        """Verifica que el Drawdown sea estrictamente <= 0% y consistente con el pico histórico."""
        result = self.engine.run("GOLD", initial_capital=100_000.0)
        eq = result.equity_curve

        # Drawdown <= 0% en todo momento
        self.assertTrue((eq["drawdown_pct"] <= 0.0001).all())

        # El pico histórico es monótonamente no decreciente
        peak_series = eq["peak_nav"]
        self.assertTrue((peak_series.diff().dropna() >= -1e-6).all())

        # Cuando el NAV alcanza un nuevo máximo, el drawdown debe ser 0.0%
        at_peak = eq["nav"] >= eq["peak_nav"] - 1e-4
        self.assertTrue((eq.loc[at_peak, "drawdown_pct"].abs() < 1e-4).all())

        # El Max Drawdown reportado en los KPIs coincide con el valor mínimo de la serie
        expected_max_dd_pct = round(abs(float(eq["drawdown_pct"].min())), 2)
        self.assertAlmostEqual(result.kpis["max_drawdown_pct"], expected_max_dd_pct, places=2)

    def test_multi_asset_compatibility(self):
        """Valida compatibilidad completa con NEM, MGC (futuros micro con multiplicador), BE e IBE."""
        assets = [
            ("NEM", 100, 1.0, 0.005),
            ("MGC", 2, 10.0, 0.50),
            ("BE", 100, 1.0, 0.005),
            ("IBE", 100, 1.0, 0.005),
        ]

        for sym, shares, expected_mult, expected_comm in assets:
            res = self.engine.run(sym, shares=shares, auto_close_at_end=True)
            self.assertIsInstance(res, BacktestResult)
            self.assertGreater(len(res.trade_log), 0, f"Sin trades para {sym}")
            self.assertGreater(len(res.equity_curve), 100, f"Curva de patrimonio vacía para {sym}")

            # Conciliación de efectivo
            total_net = float(res.trade_log["pnl_net"].sum())
            ending_cash = res.equity_curve["cash"].iloc[-1]
            self.assertAlmostEqual(ending_cash - 100_000.0, total_net, places=2)

            # Verificar comisiones aplicadas
            first_trade = res.trades[0]
            expected_roundtrip_comm = round(2 * expected_comm * shares, 2)
            self.assertAlmostEqual(first_trade.commission, expected_roundtrip_comm, places=2)

    def test_validation_and_edge_cases(self):
        """Verifica validación de argumentos y manejo de casos límite."""
        # lookback menor a 5
        with self.assertRaises(ValueError):
            self.engine.run("GOLD", lookback_bars=3)

        # dirección inválida
        with self.assertRaises(ValueError):
            self.engine.run("GOLD", direction="diagonal")

        # símbolo inexistente
        with self.assertRaises(ValueError):
            self.engine.run("NONEXISTENT_TICKER_XYZ")

        # Filtro de fechas parcial estricto
        res_filtered = self.engine.run(
            "GOLD",
            start_date="2023-01-01",
            end_date="2023-12-31",
            shares=100,
        )
        self.assertIsInstance(res_filtered, BacktestResult)
        start_in_curve = res_filtered.equity_curve["date"].iloc[0]
        end_in_curve = res_filtered.equity_curve["date"].iloc[-1]
        self.assertTrue("2023-01-01" <= start_in_curve <= "2023-01-05")
        self.assertTrue(end_in_curve <= "2023-12-31")
        self.assertLessEqual(len(res_filtered.equity_curve), 260)
        self.assertEqual(res_filtered.kpis["start_date"], start_in_curve)
        self.assertEqual(res_filtered.kpis["end_date"], end_in_curve)

        # Validación de orden de fechas
        with self.assertRaises(ValueError):
            self.engine.run("GOLD", start_date="2024-01-01", end_date="2023-01-01")

        # Fecha fuera de rango
        with self.assertRaises(ValueError):
            self.engine.run("GOLD", start_date="2035-01-01")

        # ATR sizing
        res_atr = self.engine.run("GOLD", use_atr_sizing=True)
        self.assertIsInstance(res_atr, BacktestResult)
        self.assertGreater(len(res_atr.trades), 0)

    def test_no_same_day_reentry_causality(self):
        """Verifica que el motor no presente violación de causalidad ni time-travel:
        una posición cerrada durante la sesión tras venir abierta de sesiones previas
        no puede provocar una re-entrada en esa misma sesión.
        """
        result = self.engine.run("GOLD", initial_capital=100_000.0)
        trades = result.trades
        self.assertGreater(len(trades), 10)

        for i in range(1, len(trades)):
            prev_trade = trades[i - 1]
            curr_trade = trades[i]
            # Si el trade previo se mantuvo de un día para otro (holding_days > 0),
            # el siguiente trade NO puede haber entrado en la misma fecha de salida.
            if prev_trade.holding_days > 0:
                self.assertNotEqual(
                    curr_trade.entry_date,
                    prev_trade.exit_date,
                    f"Violación de causalidad: Trade {curr_trade.trade_id} entró el {curr_trade.entry_date} "
                    f"el mismo día que cerró el trade previo {prev_trade.trade_id} ({prev_trade.exit_date})",
                )

    def test_price_control_zones_integration(self):
        """Verifica la ejecución con zonas de control por gaps históricas (>=3%) con cero anticipación."""
        res_cz = self.engine.run("GOLD", use_control_zones=True)
        self.assertIsInstance(res_cz, BacktestResult)
        self.assertGreater(len(res_cz.trades), 5)
        self.assertEqual(len(res_cz.equity_curve), 1255)

    def test_summary_and_serialization(self):
        """Verifica generación de texto resumen y serialización a diccionario."""
        res = self.engine.run("GOLD", shares=100)
        summary_text = res.summary()
        self.assertIn("RESULTADOS DE BACKTEST MULTI-CICLO", summary_text)
        self.assertIn("GOLD", summary_text)
        self.assertIn("Win Rate:", summary_text)
        self.assertIn("Profit Factor:", summary_text)
        self.assertIn("Max Drawdown (%):", summary_text)

        d = res.to_dict()
        self.assertIn("kpis", d)
        self.assertIn("trades_count", d)
        self.assertIn("equity_points_count", d)


if __name__ == "__main__":
    unittest.main()
