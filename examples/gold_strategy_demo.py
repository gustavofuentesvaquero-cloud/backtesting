"""Demo executable: Estrategia cuantitativa de Oro (XAU/USD).

Muestra cómo implementar una estrategia de reversión a la media / toma de beneficios
para XAU/USD utilizando el motor de eventos determinista.
"""

from __future__ import annotations

import json
from decimal import Decimal
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
    MatchingPolicy,
    MarketDataPipeline,
    OrderIntent,
    RawFrame,
    StrategyConfig,
    StrategyDecision,
    StrategyInstance,
)
from market_catalog.normalization import JsonFixtureNormalizer


class GoldDipBuyerStrategy:
    """Estrategia para XAU/USD: compra en caídas y toma beneficios en subidas."""

    api_version = "1.0.0"

    def validate_config(self, parameters: dict) -> bool:
        required = {"buy_dip_price", "take_profit_price", "lot_size"}
        if not required.issubset(parameters):
            raise ValueError(f"Faltan parámetros requeridos: {required}")
        return True

    def on_market(self, context, market, portfolio, state: dict) -> StrategyDecision:
        event = market.event
        if event.event_type != "quote":
            return StrategyDecision(state=state)

        bid = Decimal(str(event.payload["bid"]["price"]))
        ask = Decimal(str(event.payload["ask"]["price"]))

        buy_dip = Decimal(context.config.parameters["buy_dip_price"])
        take_profit = Decimal(context.config.parameters["take_profit_price"])
        lot_size = Decimal(context.config.parameters["lot_size"])

        current_position = portfolio.positions.get("XAUUSD", Decimal("0"))
        has_bought = state.get("has_bought", False)
        has_sold = state.get("has_sold", False)

        intents = []

        # 1. Condición de compra: el oro cae por debajo del precio objetivo
        if not has_bought and ask <= buy_dip:
            print(f"[ESTRATEGIA] Oportunidad de compra detectada: Ask={ask} <= Umbral={buy_dip}")
            intents.append(
                OrderIntent(
                    order_id="gold-buy-dip-1",
                    instrument_id="XAUUSD",
                    side="buy",
                    order_type="market",
                    quantity=lot_size,
                )
            )
            return StrategyDecision(
                state={**state, "has_bought": True, "entry_price": str(ask)},
                intents=tuple(intents),
            )

        # 2. Condición de venta / toma de beneficios: tenemos posición y el precio supera el objetivo
        if has_bought and not has_sold and current_position > Decimal("0") and bid >= take_profit:
            print(f"[ESTRATEGIA] Toma de beneficios: Bid={bid} >= Objetivo={take_profit}")
            intents.append(
                OrderIntent(
                    order_id="gold-take-profit-1",
                    instrument_id="XAUUSD",
                    side="sell",
                    order_type="market",
                    quantity=current_position,
                )
            )
            return StrategyDecision(
                state={**state, "has_sold": True, "exit_price": str(bid)},
                intents=tuple(intents),
            )

        return StrategyDecision(state=state)


def create_gold_catalog() -> Catalog:
    """Define las especificaciones de mercado para el par XAU/USD."""
    return Catalog.from_mapping({
        "schema_version": "1.0.0",
        "catalog_version": "1.0.0",
        "instruments": [
            {
                "id": "XAUUSD",
                "asset_class": "equity",  # Modelo estándar de unidades
                "venue": "OTC_METALS",
                "provider_symbols": [{"provider_id": "GOLD_FEED", "symbol": "XAUUSD"}],
                "valid_from": "2026-01-01",
                "valid_to": None,
                "calendar_id": "GOLD-24H",
                "price_tick": "0.01",
                "price_precision": 2,
                "quantity_unit": "oz",
                "quantity_precision": 2,
                "minimum_quantity": "0.01",
                "quantity_increment": "0.01",
                "quote_currency": "USD",
                "valuation_model": "equity_shares",
                "provenance": {
                    "status": "synthetic",
                    "sources": ["spot_gold_sim"],
                    "notes": "Par spot XAU/USD (Onza troy en USD).",
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


def create_tick_frame(ordinal: int, bid: str, ask: str) -> RawFrame:
    """Simula un tick de mercado con cotización bid/ask."""
    source_ns = 1_900_000_000_000_000_000 + ordinal * 1_000_000_000
    message = {
        "event_type": "quote",
        "symbol": "XAUUSD",
        "sequence": ordinal + 1,
        "sequence_scope": "instrument_channel",
        "source_timestamp": {
            "raw": str(source_ns),
            "unix_ns": source_ns,
            "semantics": "event_time",
        },
        "payload": {
            "bid": {"price": bid, "quantity": "100.0"},
            "ask": {"price": ask, "quantity": "100.0"},
        },
    }
    return RawFrame(
        provider_id="GOLD_FEED",
        channel="L1",
        receive_time_utc_ns=source_ns + 1_000_000,
        receive_time_monotonic_ns=ordinal * 1_000_000,
        arrival_ordinal=ordinal,
        raw_payload=json.dumps(message).encode("utf-8"),
    )


class _DummyAdapter:
    def request_snapshot(self, instrument_id: str, channel: str) -> bool:
        return False


def main():
    print("=" * 65)
    print("  SIMULADOR DE ESTRATEGIA CUANTITATIVA: ORO SPOT (XAU/USD)")
    print("=" * 65)

    catalog = create_gold_catalog()
    config = StrategyConfig(
        strategy_id="gold-mean-reversion",
        instance_id="gold-instance-01",
        strategy_version="1.0.0",
        parameters={
            "buy_dip_price": "2645.00",      # Comprar si el oro baja a $2645
            "take_profit_price": "2665.00",  # Vender si el oro sube a $2665
            "lot_size": "5.00",              # Tamaño de orden: 5 onzas troy
        },
    )

    strategy = StrategyInstance(
        GoldDipBuyerStrategy(),
        config,
        seed=42,
        initial_state={"has_bought": False, "has_sold": False},
    )

    pipeline = MarketDataPipeline(
        _DummyAdapter(),
        JsonFixtureNormalizer(catalog),
        DataQualityMonitor(),
    )

    # Configuración de matching con spread, slippage y comisiones ($0.50 por onza)
    processor = EventProcessor(
        pipeline,
        catalog,
        strategy,
        config=EventProcessorConfig(
            matching_policy=MatchingPolicy(
                commission=CommissionModel(
                    per_unit=Decimal("0.50"),
                    currency="USD",
                    currency_precision=2,
                )
            )
        ),
        session_id="session-xauusd-live-sim",
    )

    processor.start()

    # Secuencia de mercado simulada:
    # 1. Oro abre cotizando en 2655.00
    # 2. El oro sufre una caída repentina a 2642.00 (Dispara compra de 5 oz)
    # 3. El oro rebota a 2652.00
    # 4. El oro sube fuertemente a 2668.00 (Dispara toma de beneficios)
    ticks = [
        (0, "2654.80", "2655.20"),  # Mercado normal
        (1, "2641.70", "2642.10"),  # Caída (Dip) -> Comprar
        (2, "2651.90", "2652.30"),  # Recuperación
        (3, "2668.20", "2668.60"),  # Rally -> Toma de beneficios
    ]

    print("\nIniciando ingesta de ticks de mercado XAU/USD...")
    for idx, bid, ask in ticks:
        print(f"Tick #{idx+1}: Bid={bid} | Ask={ask}")
        frame = create_tick_frame(idx, bid, ask)
        processor.submit(frame)

    report = processor.finish(timeout=5)

    print("\n" + "=" * 65)
    print("  RESULTADOS DE LA SESIÓN DE BACKTESTING")
    print("=" * 65)
    print(f"Estado de la sesión : {report.status}")
    print(f"Eventos procesados  : {report.processed_events}")
    print(f"Órdenes ejecutadas  : {len(report.fills)}")

    print("\n--- DETALLE DE EJECUCIONES (FILLS) ---")
    for i, fill in enumerate(report.fills, 1):
        side = "COMPRA" if "buy" in fill.order_id else "VENTA"
        print(
            f"Fill #{i}: {side:6s} | {fill.quantity} oz @ ${fill.price:.2f} "
            f"| Comisión: ${fill.commission:.2f} {fill.commission_currency}"
        )

    portfolio = report.portfolio
    print("\n--- ESTADO DE CARTERA Y P&L ---")
    print(f"Posición final XAUUSD : {portfolio.positions.get('XAUUSD', Decimal('0'))} oz")
    print(f"Comisiones totales    : ${portfolio.commissions_paid.get('USD', Decimal('0')):.2f} USD")
    print(f"P&L Realizado neto    : ${portfolio.realized_pnl.get('USD', Decimal('0')):.2f} USD")
    print("=" * 65)


if __name__ == "__main__":
    main()
