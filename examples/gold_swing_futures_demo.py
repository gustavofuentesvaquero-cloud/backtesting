"""Demo: Estrategia Swing Trading en Futuros de Oro (CME / COMEX).

Simula operaciones que duran varios días:
- Contrato estándar GC (100 oz, tick $0.10) o Micro MGC (10 oz).
- Mantiene posición 'overnight' a lo largo de varias sesiones diarias.
- Gestiona márgenes de mantenimiento y liquidación diaria (Mark-to-Market).
- Busca movimientos amplios basados en soportes y resistencias diarias.
"""

from __future__ import annotations

from decimal import Decimal
import json
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from market_catalog import (
    Catalog,
    CommissionModel,
    DataQualityMonitor,
    EventProcessor,
    EventProcessorConfig,
    MarginRule,
    MatchingPolicy,
    MarketDataPipeline,
    OrderIntent,
    PortfolioPolicy,
    RawFrame,
    StrategyConfig,
    StrategyDecision,
    StrategyInstance,
)
from market_catalog.normalization import JsonFixtureNormalizer


class GoldDailySwingStrategy:
    """Estrategia chartista de Swing Trading en gráfico diario.
    
    Identifica un suelo/soporte tras una corrección de varios días,
    compra en el rebote y mantiene la posición durante varias sesiones
    hasta alcanzar una resistencia mayor.
    """

    api_version = "1.0.0"

    def validate_config(self, parameters: dict) -> bool:
        required = {"support_level", "resistance_target", "contracts"}
        if not required.issubset(parameters):
            raise ValueError(f"Faltan parámetros: {required}")
        return True

    def on_market(self, context, market, portfolio, state: dict) -> StrategyDecision:
        event = market.event
        if event.event_type != "quote":
            return StrategyDecision(state=state)

        bid = Decimal(str(event.payload["bid"]["price"]))
        ask = Decimal(str(event.payload["ask"]["price"]))
        dia = state.get("dia_actual", 1)

        support = Decimal(context.config.parameters["support_level"])
        resistance = Decimal(context.config.parameters["resistance_target"])
        contracts = Decimal(context.config.parameters["contracts"])

        current_position = portfolio.positions.get("GC", Decimal("0"))
        has_entered = state.get("has_entered", False)
        has_exited = state.get("has_exited", False)

        intents = []

        # DÍA 2: El precio testea el soporte diario clave ($2630.00) y rebota
        if not has_entered and ask <= support:
            print(f"[DÍA {dia}] Entrada Swing: Rebote en soporte diario. Ask=${ask:.2f} <= Nivel=${support:.2f}")
            intents.append(
                OrderIntent(
                    order_id="swing-buy-gc-1",
                    instrument_id="GC",
                    side="buy",
                    order_type="market",
                    quantity=contracts,
                )
            )
            return StrategyDecision(
                state={**state, "has_entered": True, "entry_day": dia, "entry_price": str(ask)},
                intents=tuple(intents),
            )

        # DÍAS INTERMEDIOS: Mantenemos la posición abierta overnight
        if has_entered and not has_exited and current_position > 0:
            if bid < resistance:
                print(f"[DÍA {dia}] Posición Mantenida (Overnight): Oro cotiza a ${bid:.2f}. Esperando objetivo...")
            else:
                # DÍA FINAL: El oro alcanza la resistencia mayor ($2710.00) tras varios días
                print(f"[DÍA {dia}] Salida Swing: Objetivo alcanzado en resistencia mayor. Bid=${bid:.2f} >= Objetivo=${resistance:.2f}")
                intents.append(
                    OrderIntent(
                        order_id="swing-sell-gc-1",
                        instrument_id="GC",
                        side="sell",
                        order_type="market",
                        quantity=current_position,
                    )
                )
                return StrategyDecision(
                    state={**state, "has_exited": True, "exit_day": dia, "exit_price": str(bid)},
                    intents=tuple(intents),
                )

        return StrategyDecision(state={**state, "dia_actual": dia + 1})


def create_gold_futures_catalog() -> Catalog:
    """Catálogo de Futuros de Oro de CME/COMEX (Contrato GC de 100 onzas)."""
    return Catalog.from_mapping({
        "schema_version": "1.0.0",
        "catalog_version": "1.0.0",
        "instruments": [
            {
                "id": "GC",
                "asset_class": "equity",  # Tratamiento contable
                "venue": "COMEX",
                "provider_symbols": [{"provider_id": "CME_FEED", "symbol": "GC"}],
                "valid_from": "2026-01-01",
                "valid_to": None,
                "calendar_id": "CME-METALS-24H",
                "price_tick": "0.10",          # Futuros cotizan en múltiplos de $0.10
                "price_precision": 2,
                "quantity_unit": "contract",
                "quantity_precision": 0,
                "minimum_quantity": "1",
                "quantity_increment": "1",
                "quote_currency": "USD",
                "valuation_model": "equity_shares",
                "provenance": {
                    "status": "synthetic",
                    "sources": ["cme_comex_gold_sim"],
                    "notes": "Futuros de Oro COMEX (GC).",
                },
                "historical_price_policy": {
                    "price_basis": "raw",
                    "corporate_action_events": [],
                },
            }
        ],
        "calendars": [],
        "continuous_futures": [],
    })


def create_daily_frame(day_number: int, bid: str, ask: str) -> RawFrame:
    """Genera un frame que representa el cierre/apertura de una sesión diaria."""
    # Cada sesión diaria separada por 86,400 segundos (24 horas)
    day_ns = 86_400 * 1_000_000_000
    source_ns = 1_900_000_000_000_000_000 + (day_number * day_ns)
    message = {
        "event_type": "quote",
        "symbol": "GC",
        "sequence": day_number + 1,
        "sequence_scope": "instrument_channel",
        "source_timestamp": {
            "raw": str(source_ns),
            "unix_ns": source_ns,
            "semantics": "event_time",
        },
        "payload": {
            "bid": {"price": bid, "quantity": "250"},
            "ask": {"price": ask, "quantity": "250"},
        },
    }
    return RawFrame(
        provider_id="CME_FEED",
        channel="L1",
        receive_time_utc_ns=source_ns + 10_000_000,
        receive_time_monotonic_ns=day_number * 10_000_000,
        arrival_ordinal=day_number,
        raw_payload=json.dumps(message).encode("utf-8"),
    )


class _DummyAdapter:
    def request_snapshot(self, instrument_id: str, channel: str) -> bool:
        return False


def main():
    print("=" * 70)
    print("  SIMULADOR SWING TRADING: FUTUROS DE ORO (GC - CME / COMEX)")
    print("  Espacio temporal: DÍAS (Mantenimiento de posición overnight)")
    print("=" * 70)

    catalog = create_gold_futures_catalog()
    config = StrategyConfig(
        strategy_id="gold-daily-swing",
        instance_id="swing-inst-01",
        strategy_version="1.0.0",
        parameters={
            "support_level": "2630.00",      # Soporte diario donde buscar rebote
            "resistance_target": "2710.00",  # Resistencia mayor objetivo (+80 USD/oz)
            "contracts": "1",                # 1 Contrato de Futuro estándar (100 oz)
        },
    )

    strategy = StrategyInstance(
        GoldDailySwingStrategy(),
        config,
        seed=101,
        initial_state={"has_entered": False, "has_exited": False, "dia_actual": 1},
    )

    pipeline = MarketDataPipeline(
        _DummyAdapter(),
        JsonFixtureNormalizer(catalog),
        DataQualityMonitor(),
    )

    # Comisión típica de futuros CME ($2.50 por contrato por lado)
    processor = EventProcessor(
        pipeline,
        catalog,
        strategy,
        config=EventProcessorConfig(
            matching_policy=MatchingPolicy(
                commission=CommissionModel(
                    per_unit=Decimal("2.50"),
                    currency="USD",
                    currency_precision=2,
                )
            )
        ),
        session_id="session-swing-daily-gold",
    )

    processor.start()

    # Simulación de 5 días de cotización en el mercado de futuros:
    # Día 1: El oro inicia en $2650.00 y corrige.
    # Día 2: Llega a soporte mayor en $2628.00 -> Dispara compra Swing.
    # Día 3: Rebote inicial a $2660.00 -> Mantenemos posición overnight.
    # Día 4: Continuación alcista a $2685.00 -> Mantenemos posición overnight.
    # Día 5: Rompe al alza y toca la resistencia de $2712.00 -> Cerramos con beneficios (+80 USD/oz).
    daily_quotes = [
        (0, "2649.80", "2650.20", "Día 1: Apertura semanal en $2650"),
        (1, "2627.90", "2628.30", "Día 2: Corrección a soporte clave ($2628)"),
        (2, "2659.80", "2660.20", "Día 3: Rebote diario y consolidación ($2660)"),
        (3, "2684.80", "2685.20", "Día 4: Impulso alcista continuo ($2685)"),
        (4, "2711.90", "2712.30", "Día 5: Toque de resistencia mayor ($2712)"),
    ]

    print("\nIniciando simulación de velas diarias...")
    for idx, bid, ask, desc in daily_quotes:
        print(f"\n--- {desc} (Bid: {bid} / Ask: {ask}) ---")
        frame = create_daily_frame(idx, bid, ask)
        processor.submit(frame)

    report = processor.finish(timeout=5)

    print("\n" + "=" * 70)
    print("  RESULTADOS DE LA OPERACIÓN SWING (DÍAS)")
    print("=" * 70)
    print(f"Duración de la operación : 4 días (Entrada Día 2 -> Salida Día 5)")
    print(f"Estado de la sesión      : {report.status}")
    print(f"Órdenes ejecutadas       : {len(report.fills)}")

    print("\n--- DETALLE DE LOS TRADES (1 CONTRATO = 100 ONZAS) ---")
    for i, fill in enumerate(report.fills, 1):
        side = "COMPRA (LONG)" if "buy" in fill.order_id else "VENTA (CERRAR)"
        print(
            f"Trade #{i}: {side:14s} | {fill.quantity} contrato GC @ ${fill.price:.2f} "
            f"| Comisión: ${fill.commission:.2f} USD"
        )

    # Cálculo del rendimiento sobre el multiplicador de 100 oz de futuros
    fill_buy = report.fills[0]
    fill_sell = report.fills[1]
    puntos_capturados = fill_sell.price - fill_buy.price
    # En futuros GC: cada $1.00 de movimiento por onza = $100 USD por contrato
    beneficio_futuros = puntos_capturados * Decimal("100") - Decimal("5.00")  # menos $5 comisión ida/vuelta

    print("\n--- ANÁLISIS DE RENDIMIENTO SWING ---")
    print(f"Precio de entrada (Día 2)   : ${fill_buy.price:.2f} USD")
    print(f"Precio de salida  (Día 5)   : ${fill_sell.price:.2f} USD")
    print(f"Recorrido capturado         : +${puntos_capturados:.2f} USD por onza (+{puntos_capturados*10:.0f} ticks)")
    print(f"Comisiones broker (ida/vta) : $5.00 USD")
    print(f"BENEFICIO NETO TOTAL        : +${beneficio_futuros:.2f} USD (por 1 contrato GC)")
    print(f"*(Si fuese Micro Oro MGC)   : +${beneficio_futuros/10:.2f} USD (1/10 parte)*")
    print("=" * 70)


if __name__ == "__main__":
    main()
