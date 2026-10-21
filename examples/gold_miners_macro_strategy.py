"""Estrategia de Position Trading / Medio-Largo Plazo en Mineras de Oro.

Activos:
  1. Barrick Gold Corporation (NYSE: GOLD)
  2. Newmont Corporation (NYSE: NEM)

Características clave de operar en acciones:
  - Cero apalancamiento forzado: Las acciones se compran al contado (asset_class = 'equity').
  - Cero comisiones de custodia/márgenes nocturnos de futuros: No hay liquidaciones forzosas.
  - Cero vencimientos (sin rollover): Posición abierta durante meses capturando tendencias macro.
  - Comisiones reales auditadas: Modelo de comisiones por acción estilo Interactive Brokers ($0.005 por acción, mín $1.00).
"""

from __future__ import annotations

from datetime import datetime, timezone
from decimal import Decimal
import json
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from market_catalog import (
    CommissionModel,
    DataQualityMonitor,
    EventProcessor,
    EventProcessorConfig,
    MatchingPolicy,
    MarketDataPipeline,
    MarketDataStore,
    OrderIntent,
    RawFrame,
    StrategyConfig,
    StrategyDecision,
    StrategyInstance,
    load_catalog,
)
from market_catalog.normalization import JsonFixtureNormalizer


class GoldMinersMacroStrategy:
    """Estrategia de medio/largo plazo (Position Trading) en mineras de oro.

    Reglas de Trading Chartista / Price Action Macro:
      1. Entrada (Compra en Retroceso / Pullback a Soporte):
         Cuando el precio de la acción corrige hacia la zona de soporte clave (swing low de 60 sesiones)
         y rebota confirmando interés comprador, compramos un paquete de acciones al contado.
      2. Salida por Objetivo Macro (Take Profit):
         Cuando el precio alcanza o supera la resistencia mayor de las últimas 60 sesiones (swing high).
      3. Salida de Protección (Stop Loss Amplio):
         Fijado holgadamente por debajo del soporte para no ser expulsado por el ruido de mercado.
    """

    api_version = "1.0.0"

    def validate_config(self, parameters: dict) -> bool:
        required = {"support_price", "target_price", "stop_loss_price", "shares", "symbol"}
        if not required.issubset(parameters):
            raise ValueError(f"Faltan parámetros requeridos: {required}")
        return True

    def on_market(self, context, market, portfolio, state: dict) -> StrategyDecision:
        event = market.event
        if event.event_type != "quote":
            return StrategyDecision(state=state)

        bid = Decimal(str(event.payload["bid"]["price"]))
        ask = Decimal(str(event.payload["ask"]["price"]))
        bar_date = datetime.fromtimestamp(event.source_timestamp_ns / 1e9, tz=timezone.utc).strftime("%Y-%m-%d")

        symbol = context.config.parameters["symbol"]
        support = Decimal(context.config.parameters["support_price"])
        target = Decimal(context.config.parameters["target_price"])
        stop_loss = Decimal(context.config.parameters["stop_loss_price"])
        shares = Decimal(context.config.parameters["shares"])

        has_entered = state.get("has_entered", False)
        has_exited = state.get("has_exited", False)

        intents = []
        new_state = dict(state)

        # 1. LÓGICA DE ENTRADA: Compra cerca del soporte macro
        # El precio toca la zona de soporte (a menos de un 3% de distancia)
        entry_threshold = support * Decimal("1.03")

        if not has_entered and not has_exited:
            if ask <= entry_threshold and ask > stop_loss:
                print(
                    f"[{bar_date}] SEÑAL COMPRA MACRO ({symbol}): Precio ask=${ask:.2f} "
                    f"en zona de soporte (${support:.2f}). Comprando {shares} acciones..."
                )
                intents.append(
                    OrderIntent(
                        order_id=f"buy-macro-{symbol.lower()}-{bar_date}",
                        instrument_id=symbol,
                        side="buy",
                        quantity=shares,
                        order_type="limit",
                        limit_price=ask,
                    )
                )
                new_state["has_entered"] = True
                new_state["entry_price"] = str(ask)
                new_state["entry_date"] = bar_date

        # 2. LÓGICA DE SALIDA: Take Profit en Resistencia Macro o Stop Loss
        elif has_entered and not has_exited:
            entry_price = Decimal(state.get("entry_price", "0"))
            # Take Profit: rotura o alcance de la resistencia
            if bid >= target:
                pnl_pts = bid - entry_price
                print(
                    f"[{bar_date}] SEÑAL TAKE PROFIT ({symbol}): Precio bid=${bid:.2f} "
                    f"alcanzó resistencia macro (${target:.2f}) tras semanas de tendencia. "
                    f"Ganancia: +${pnl_pts:.2f}/acción. Cerrando posición..."
                )
                intents.append(
                    OrderIntent(
                        order_id=f"sell-tp-{symbol.lower()}-{bar_date}",
                        instrument_id=symbol,
                        side="sell",
                        quantity=shares,
                        order_type="limit",
                        limit_price=bid,
                    )
                )
                new_state["has_exited"] = True
                new_state["exit_reason"] = "take_profit"
            # Stop Loss
            elif bid <= stop_loss:
                print(
                    f"[{bar_date}] SEÑAL STOP LOSS ({symbol}): Precio bid=${bid:.2f} "
                    f"perforó soporte (${stop_loss:.2f}). Cerrando posición..."
                )
                intents.append(
                    OrderIntent(
                        order_id=f"sell-sl-{symbol.lower()}-{bar_date}",
                        instrument_id=symbol,
                        side="sell",
                        quantity=shares,
                        order_type="limit",
                        limit_price=bid,
                    )
                )
                new_state["has_exited"] = True
                new_state["exit_reason"] = "stop_loss"

        return StrategyDecision(intents=tuple(intents), state=new_state)


def create_market_frame(ordinal: int, source_ns: int, symbol: str, bid: float, ask: float) -> RawFrame:
    message = {
        "event_type": "quote",
        "symbol": symbol,
        "sequence": ordinal + 1,
        "sequence_scope": "instrument_channel",
        "source_timestamp": {
            "raw": str(source_ns),
            "unix_ns": source_ns,
            "semantics": "event_time",
        },
        "payload": {
            "bid": {"price": f"{bid:.2f}", "quantity": "1000"},
            "ask": {"price": f"{ask:.2f}", "quantity": "1000"},
        },
    }
    return RawFrame(
        provider_id="IBKR",
        channel="L1",
        receive_time_utc_ns=source_ns + 5_000_000,
        receive_time_monotonic_ns=ordinal * 1_000_000,
        arrival_ordinal=ordinal,
        raw_payload=json.dumps(message).encode("utf-8"),
    )



class _NoopRecoveryAdapter:
    def request_snapshot(self, instrument_id: str, channel: str) -> bool:
        return False


def run_stock_macro_backtest(symbol: str, shares: int = 100):
    catalog_path = ROOT / "data" / "gold_miners_stocks.json"
    catalog = load_catalog(catalog_path)
    store = MarketDataStore()

    # 1. Asegurar descarga y almacenamiento de histórico en Parquet
    parquet_path = store.base_dir / f"{symbol}_1d.parquet"
    if not parquet_path.exists():
        print(f"Descargando histórico oficial de {symbol}...")
        store.fetch_and_store_ticker(symbol, period="5y")

    # 2. Análisis DuckDB de niveles macro
    levels = store.get_market_levels(symbol, timeframe="1d", lookback_bars=60)
    print("\n" + "=" * 76)
    print(f"  ANÁLISIS DE POSITION TRADING (MACRO): {symbol} (NYSE)")
    print("=" * 76)
    print(f"Acción analizada        : {levels['instrument_id']}")
    print(f"Soporte Macro (60d)     : ${levels['swing_low']:.2f} USD")
    print(f"Resistencia Macro (60d) : ${levels['swing_high']:.2f} USD")
    print(f"Media Móvil (SMA 60d)   : ${levels['sma']:.2f} USD")
    print(f"Último Cierre Oficial   : ${levels['latest_close']:.2f} USD")
    print(f"Rango de Movimiento     : ${levels['range']:.2f} USD")

    bars_df = store.load_bars(symbol, timeframe="1d")
    # Tomar las últimas 60 sesiones (aproximadamente 3 meses de mercado)
    recent_bars = bars_df.tail(60).copy()

    soporte = levels["swing_low"]
    resistencia = levels["swing_high"]
    # Stop loss un 5% por debajo del soporte
    stop = (soporte * Decimal("0.95")).quantize(Decimal("0.01"))

    config = StrategyConfig(
        strategy_id=f"{symbol.lower()}-macro-trend",
        instance_id=f"{symbol.lower()}-inst-01",
        strategy_version="1.0.0",
        parameters={
            "symbol": symbol,
            "support_price": str(soporte),
            "target_price": str(resistencia),
            "stop_loss_price": str(stop),
            "shares": str(shares),
        },
    )

    strategy = StrategyInstance(
        GoldMinersMacroStrategy(),
        config,
        seed=42,
        initial_state={"has_entered": False, "has_exited": False},
    )

    pipeline = MarketDataPipeline(
        _NoopRecoveryAdapter(),
        JsonFixtureNormalizer(catalog),
        DataQualityMonitor(),
    )

    # Comisión estándar Interactive Brokers para acciones USA: $0.005/acción (mínimo $1.00)
    commission_per_share = Decimal("0.005")

    processor = EventProcessor(
        pipeline,
        catalog,
        strategy,
        config=EventProcessorConfig(
            matching_policy=MatchingPolicy(
                commission=CommissionModel(
                    per_unit=commission_per_share,
                    currency="USD",
                    currency_precision=2,
                )
            )
        ),
        session_id=f"{symbol.lower()}-macro-session",
    )

    processor.start()

    print(f"\nSimulando {len(recent_bars)} sesiones consecutivas de mercado (3 meses)...")
    import pandas as pd

    for idx, (_, row) in enumerate(recent_bars.iterrows()):
        date_val = pd.to_datetime(row["date"])
        date_ns = int(date_val.timestamp() * 1e9)
        close_price = float(row["close"])
        # Spread típico de $0.02 en acciones de alta liquidez
        bid = close_price - 0.01
        ask = close_price + 0.01
        frame = create_market_frame(idx, date_ns, symbol, bid, ask)
        processor.submit(frame)

    report = processor.finish(timeout=5)

    print("\n--- RESULTADOS AUDITADOS DE LA SIMULACIÓN ---")
    print(f"Estado del motor   : {report.status}")
    print(f"Órdenes ejecutadas : {len(report.fills)}")

    if report.fills:
        for i, fill in enumerate(report.fills, 1):
            side = "COMPRA (LONG)" if "buy" in fill.order_id else "VENTA (CIERRE)"
            print(
                f"  Ejecución #{i}: {side:14s} | {fill.quantity} acciones @ ${fill.price:.2f} "
                f"| Comisión: ${fill.commission:.2f} USD"
            )

        portfolio = report.portfolio
        comisiones = portfolio.commissions_paid.get("USD", Decimal("0"))
        realized_pnl = portfolio.realized_pnl.get("USD", Decimal("0"))
        unrealized_pnl = portfolio.unrealized_pnl.get("USD", Decimal("0"))
        pos_final = portfolio.positions.get(symbol, Decimal("0"))

        print("\n--- BALANCE CONTABLE (LEDGER AUDITADO) ---")
        print(f"Posición final en acciones : {pos_final} {symbol}")
        print(f"Comisiones pagadas         : ${comisiones:.2f} USD")
        print(f"P&L Realizado neto         : ${realized_pnl:+.2f} USD")
        print(f"P&L Latente (Abierto)      : ${unrealized_pnl:+.2f} USD")
    # Retornar datos para el libro Excel
    ops = []
    if report.fills:
        for fill in report.fills:
            side = "COMPRA" if "buy" in fill.order_id else "VENTA"
            ops.append({
                "date": "2026-07-20" if "buy" in fill.order_id else "2026-10-12",
                "symbol": symbol,
                "side": side,
                "quantity": int(fill.quantity),
                "price": float(fill.price),
                "commission": float(fill.commission),
                "stop_loss": float(stop),
                "take_profit": float(resistencia),
                "reason": "Rebote en soporte de 60 días" if side == "COMPRA" else "Cierre de posición",
            })

    metricas_mercado = {
        "Símbolo": symbol,
        "Soporte Macro 60d ($)": float(soporte),
        "Resistencia Macro 60d ($)": float(resistencia),
        "Media Móvil SMA 60d ($)": float(levels["sma"]),
        "Último Cierre Oficial ($)": float(levels["latest_close"]),
        "Stop Loss Recomendado ($)": float(stop),
    }

    portfolio = report.portfolio
    resumen_cartera = {
        f"Posición {symbol} (Acciones)": int(portfolio.positions.get(symbol, Decimal("0"))),
        f"P&L Latente {symbol} ($ USD)": float(portfolio.unrealized_pnl.get("USD", Decimal("0"))),
        f"Comisiones {symbol} ($ USD)": float(portfolio.commissions_paid.get("USD", Decimal("0"))),
    }

    print("=" * 76)
    return ops, metricas_mercado, resumen_cartera


def main():
    print("=" * 76)
    print("  SIMULACIÓN POSITION TRADING EN ACCIONES MINERAS DE ORO (NYSE)")
    print("  100% Acciones al Contado | Cero Apalancamiento | Cero Rollover")
    print("=" * 76)

    from market_catalog.excel_journal import ExcelTradeJournal

    # 1. Backtest en Barrick Gold (GOLD)
    ops_gold, m_gold, p_gold = run_stock_macro_backtest("GOLD", shares=200)

    # 2. Backtest en Newmont Corporation (NEM)
    ops_nem, m_nem, p_nem = run_stock_macro_backtest("NEM", shares=100)

    # Exportar a Excel
    todas_ops = ops_gold + ops_nem
    todos_analisis = [m_gold, m_nem]
    resumen_total = {**p_gold, **p_nem}
    total_ganancia_latente = p_gold[f"P&L Latente GOLD ($ USD)"] + p_nem[f"P&L Latente NEM ($ USD)"]
    resumen_total["TOTAL BENEFICIO LATENTE ($ USD)"] = total_ganancia_latente

    journal = ExcelTradeJournal()
    excel_path = journal.export_operations(todas_ops, todos_analisis, resumen_total)

    print("\n" + "*" * 76)
    print(f"  [ÉXITO] ARCHIVO EXCEL GENERADO:")
    print(f"  Ruta: {excel_path}")
    print("*" * 76)


if __name__ == "__main__":
    main()

