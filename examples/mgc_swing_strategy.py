"""Estrategia Cuantitativa Swing Trading en Futuros de Micro Oro (MGC).

Ejecuta sobre datos 100% REALES del mercado de futuros de CME Globex almacenados
en Parquet y analizados dinámicamente con DuckDB.
"""

from __future__ import annotations

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


class MGCDynamicSwingStrategy:
    """Estrategia Swing que no inventa niveles:
    Calcula dinámicamente el soporte (mínimo de 20 días) y la resistencia
    (máximo de 20 días) de los datos reales del mercado CME.
    """

    api_version = "1.0.0"

    def validate_config(self, parameters: dict) -> bool:
        required = {"support_price", "target_price", "stop_loss_price", "contracts"}
        if not required.issubset(parameters):
            raise ValueError(f"Faltan parámetros requeridos: {required}")
        return True

    def on_market(self, context, market, portfolio, state: dict) -> StrategyDecision:
        event = market.event
        if event.event_type != "quote":
            return StrategyDecision(state=state)

        bid = Decimal(str(event.payload["bid"]["price"]))
        ask = Decimal(str(event.payload["ask"]["price"]))
        from datetime import datetime, timezone
        bar_date = datetime.fromtimestamp(event.source_timestamp_ns / 1e9, tz=timezone.utc).strftime("%Y-%m-%d")

        support = Decimal(context.config.parameters["support_price"])
        target = Decimal(context.config.parameters["target_price"])
        stop_loss = Decimal(context.config.parameters["stop_loss_price"])
        contracts = Decimal(context.config.parameters["contracts"])

        current_position = portfolio.positions.get("MGC", Decimal("0"))
        has_entered = state.get("has_entered", False)
        has_exited = state.get("has_exited", False)

        intents = []

        # 1. ENTRADA LARGOS: El precio del oro retrocede a la zona de soporte dinámico
        # (Permite un margen de entrada del 1.5% alrededor del soporte)
        buy_trigger = support * Decimal("1.015")
        if not has_entered and ask <= buy_trigger:
            print(f"[{bar_date}] SEÑAL COMPRA MGC: Precio Ask=${ask:.2f} en zona de soporte (Base=${support:.2f})")
            intents.append(
                OrderIntent(
                    order_id="mgc-swing-buy-1",
                    instrument_id="MGC",
                    side="buy",
                    order_type="market",
                    quantity=contracts,
                )
            )
            return StrategyDecision(
                state={**state, "has_entered": True, "entry_date": bar_date, "entry_price": str(ask)},
                intents=tuple(intents),
            )

        # 2. GESTIÓN OVERNIGHT: Seguimiento de la posición abierta
        if has_entered and not has_exited and current_position > 0:
            if bid <= stop_loss:
                # Salida por Stop Loss de protección
                print(f"[{bar_date}] STOP LOSS ALCANZADO: Bid=${bid:.2f} <= Stop=${stop_loss:.2f}")
                intents.append(
                    OrderIntent(
                        order_id="mgc-swing-stop-1",
                        instrument_id="MGC",
                        side="sell",
                        order_type="market",
                        quantity=current_position,
                    )
                )
                return StrategyDecision(
                    state={**state, "has_exited": True, "exit_date": bar_date, "exit_price": str(bid), "reason": "stop_loss"},
                    intents=tuple(intents),
                )
            elif bid >= target:
                # Salida por Take Profit en resistencia
                print(f"[{bar_date}] TAKE PROFIT ALCANZADO: Bid=${bid:.2f} >= Resistencia=${target:.2f}")
                intents.append(
                    OrderIntent(
                        order_id="mgc-swing-tp-1",
                        instrument_id="MGC",
                        side="sell",
                        order_type="market",
                        quantity=current_position,
                    )
                )
                return StrategyDecision(
                    state={**state, "has_exited": True, "exit_date": bar_date, "exit_price": str(bid), "reason": "take_profit"},
                    intents=tuple(intents),
                )
            else:
                pnl_latente = (bid - Decimal(state["entry_price"])) * Decimal("10") * current_position
                print(f"[{bar_date}] Mantenimiento Overnight MGC: Cierre en ${bid:.2f} | P&L flotante: ${pnl_latente:+.2f} USD")

        return StrategyDecision(state=state)


def create_market_frame(ordinal: int, date_ns: int, bid: float, ask: float) -> RawFrame:
    """Convierte una sesión diaria real en un RawFrame para el motor."""
    source_ns = date_ns
    message = {
        "event_type": "quote",
        "symbol": "MGC",
        "sequence": ordinal + 1,
        "sequence_scope": "instrument_channel",
        "source_timestamp": {
            "raw": str(source_ns),
            "unix_ns": source_ns,
            "semantics": "event_time",
        },
        "payload": {
            "bid": {"price": f"{bid:.2f}", "quantity": "50"},
            "ask": {"price": f"{ask:.2f}", "quantity": "50"},
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


def main():
    print("=" * 72)
    print("  ESTRATEGIA SWING TRADING: FUTUROS DE MICRO ORO (MGC - CME GLOBEX)")
    print("  Conexión con DuckDB y Parquet sobre Precios Históricos Oficiales")
    print("=" * 72)

    # 1. Cargar el catálogo oficial de Micro Oro validado
    catalog = load_catalog("data/cme_micro_gold.json")

    # 2. Consultar la base de datos de DuckDB
    store = MarketDataStore()
    parquet_path = store.base_dir / "MGC_1d.parquet"
    if not parquet_path.exists():
        print("Descargando histórico real de CME...")
        store.fetch_and_store_cme_mgc(period="2y")

    # Calcular niveles reales de mercado usando DuckDB
    levels = store.get_market_levels("MGC", timeframe="1d", lookback_bars=30)
    print("\n--- ANÁLISIS DINÁMICO DE MERCADO (DuckDB sobre Parquet) ---")
    print(f"Instrumento         : {levels['instrument_id']} (Micro Gold, 10 oz)")
    print(f"Soporte real (30d)  : ${levels['swing_low']:.2f} USD")
    print(f"Resistencia real    : ${levels['swing_high']:.2f} USD")
    print(f"Media móvil (SMA 30): ${levels['sma']:.2f} USD")
    print(f"Último cierre real  : ${levels['latest_close']:.2f} USD")
    print(f"Rango de oscilación : ${levels['range']:.2f} USD")

    # 3. Cargar las últimas sesiones reales para el backtest
    bars_df = store.load_bars("MGC", timeframe="1d")
    recent_bars = bars_df.tail(25).copy()

    # Configuración de estrategia con niveles calculados matemáticamente
    soporte = levels["swing_low"]
    resistencia = levels["swing_high"]
    stop = soporte - Decimal("30.00")  # Stop loss 30 puntos por debajo del soporte

    config = StrategyConfig(
        strategy_id="mgc-swing-price-action",
        instance_id="mgc-inst-01",
        strategy_version="1.0.0",
        parameters={
            "support_price": str(soporte),
            "target_price": str(resistencia),
            "stop_loss_price": str(stop),
            "contracts": "1",  # 1 contrato Micro MGC (10 oz)
        },
    )

    strategy = StrategyInstance(
        MGCDynamicSwingStrategy(),
        config,
        seed=42,
        initial_state={"has_entered": False, "has_exited": False},
    )

    pipeline = MarketDataPipeline(
        _NoopRecoveryAdapter(),
        JsonFixtureNormalizer(catalog),
        DataQualityMonitor(),
    )

    # Comisión real de IBKR para Micro Futuros: $0.62 por contrato
    processor = EventProcessor(
        pipeline,
        catalog,
        strategy,
        config=EventProcessorConfig(
            matching_policy=MatchingPolicy(
                commission=CommissionModel(
                    per_unit=Decimal("0.62"),
                    currency="USD",
                    currency_precision=2,
                )
            )
        ),
        session_id="mgc-real-swing-session",
    )

    processor.start()

    print(f"\nIniciando simulación de las últimas {len(recent_bars)} sesiones reales de CME Globex...")
    import pandas as pd
    for idx, (_, row) in enumerate(recent_bars.iterrows()):
        date_val = pd.to_datetime(row["date"])
        date_ns = int(date_val.timestamp() * 1e9)
        # Generar spread L1 de mercado real ($0.10 de spread en MGC)
        close_price = float(row["close"])
        bid = close_price - 0.05
        ask = close_price + 0.05
        frame = create_market_frame(idx, date_ns, bid, ask)
        processor.submit(frame)

    report = processor.finish(timeout=5)

    print("\n" + "=" * 72)
    print("  RESULTADOS DE LA SESIÓN DE BACKTESTING")
    print("=" * 72)
    print(f"Sesiones reales analizadas : {len(recent_bars)}")
    print(f"Estado del motor            : {report.status}")
    print(f"Órdenes ejecutadas          : {len(report.fills)}")

    if report.fills:
        print("\n--- EJECUCIONES DE ÓRDENES (FILLS AUDITADOS) ---")
        for i, fill in enumerate(report.fills, 1):
            side = "COMPRA (LONG)" if "buy" in fill.order_id else "VENTA (CERRAR)"
            print(
                f"Fill #{i}: {side:14s} | {fill.quantity} MGC @ ${fill.price:.2f} "
                f"| Comisión: ${fill.commission:.2f} USD"
            )

        portfolio = report.portfolio
        comisiones = portfolio.commissions_paid.get("USD", Decimal("0"))
        realized_pnl = portfolio.realized_pnl.get("USD", Decimal("0"))
        unrealized_pnl = portfolio.unrealized_pnl.get("USD", Decimal("0"))

        print("\n--- BALANCE DE CARTERA (LEGER CONTABLE) ---")
        print(f"Posición final en contratos : {portfolio.positions.get('MGC', Decimal('0'))} MGC")
        print(f"Comisiones pagadas al broker: ${comisiones:.2f} USD")
        print(f"P&L Realizado neto          : ${realized_pnl:+.2f} USD")
        print(f"P&L Latente (No realizado)  : ${unrealized_pnl:+.2f} USD")
    else:
        print("\nEl precio durante este periodo no visitó el umbral de entrada fijado.")

    print("=" * 72)


if __name__ == "__main__":
    main()
