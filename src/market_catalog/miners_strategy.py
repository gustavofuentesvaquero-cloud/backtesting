"""Estrategia de Position Trading / Medio-Largo Plazo en Mineras de Oro (GOLD y NEM).

Implementa la lógica cuantitativa de seguimiento de tendencias macro,
rebotes en soportes de 60 sesiones, objetivos de toma de beneficios en resistencia,
y cálculo de órdenes y niveles para el diario de trading.
"""

from __future__ import annotations

from datetime import datetime, timezone
from decimal import Decimal
import json
from pathlib import Path
from typing import Any

import pandas as pd

from .adapters import SnapshotRecoveryAdapter
from .event_engine import EventProcessor, EventProcessorConfig
from .events import RawFrame
from .loader import load_catalog
from .models import Catalog
from .corporate_actions import EquityActionPolicy
from .normalization import JsonFixtureNormalizer
from .pipeline import MarketDataPipeline
from .quality import DataQualityMonitor
from .simulation import CommissionModel, MatchingPolicy, OrderIntent
from .storage import MarketDataStore, detect_price_control_zones
from .strategies import StrategyConfig, StrategyDecision, StrategyInstance


COMPANY_NAMES: dict[str, str] = {
    "GOLD": "Barrick Gold Corporation",
    "NEM": "Newmont Corporation",
    "MGC": "Micro Gold Futures (CME)",
    "BE": "Bloom Energy Corporation",
}


class GoldMinersMacroStrategy:
    """Estrategia de medio/largo plazo (Position Trading) en mineras de oro y futuros.

    Reglas de Trading Chartista / Price Action Macro Bi-direccional:
      1. Operativa en Largos (COMPRA / LONG):
         - Entrada en retroceso o consolidación en zona de soporte clave (swing low de 60 sesiones).
         - Salida por Objetivo (Take Profit): Resistencia macro de 60 sesiones (swing high).
         - Stop Loss: Perforación por debajo del soporte estructural (~5% holgura).
      2. Operativa en Cortos (VENTA / SHORT):
         - Entrada en rechazo, test o agotamiento en zona de resistencia macro (swing high de 60 sesiones).
         - Salida por Objetivo (Take Profit): Soporte macro de 60 sesiones (swing low).
         - Stop Loss: Perforación por encima de la resistencia estructural (~5% holgura).
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
        direction = context.config.parameters.get("direction", "long")

        has_entered = state.get("has_entered", False)
        has_exited = state.get("has_exited", False)

        intents = []
        new_state = dict(state)

        if direction in ("long", "both"):
            # 1. LÓGICA DE LARGOS
            entry_threshold = support * Decimal("1.03")

            if not has_entered and not has_exited:
                if ask <= entry_threshold and ask > stop_loss:
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
                    new_state["pos_side"] = "long"
                    new_state["entry_price"] = str(ask)
                    new_state["entry_date"] = bar_date

            elif has_entered and not has_exited and new_state.get("pos_side", "long") == "long":
                # Take Profit largos: rotura o alcance de la resistencia
                if bid >= target:
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
                # Stop Loss largos: perforación del soporte
                elif bid <= stop_loss:
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

        if direction == "short" or (direction == "both" and not new_state.get("has_entered", False) and not new_state.get("has_exited", False)):
            # 2. LÓGICA DE CORTOS
            short_entry_threshold = target * Decimal("0.97")
            short_stop = Decimal(context.config.parameters.get("short_stop_loss_price", str((target * Decimal("1.05")).quantize(Decimal("0.01")))))
            short_target = Decimal(context.config.parameters.get("short_target_price", str(support)))

            if not new_state.get("has_entered", False) and not new_state.get("has_exited", False):
                if bid >= short_entry_threshold and bid < short_stop:
                    intents.append(
                        OrderIntent(
                            order_id=f"sell-short-macro-{symbol.lower()}-{bar_date}",
                            instrument_id=symbol,
                            side="sell",
                            quantity=shares,
                            order_type="limit",
                            limit_price=bid,
                        )
                    )
                    new_state["has_entered"] = True
                    new_state["pos_side"] = "short"
                    new_state["entry_price"] = str(bid)
                    new_state["entry_date"] = bar_date

            elif has_entered and not has_exited and new_state.get("pos_side") == "short":
                # Take Profit cortos: precio alcanza el soporte inferior
                if ask <= short_target:
                    intents.append(
                        OrderIntent(
                            order_id=f"buy-cover-tp-{symbol.lower()}-{bar_date}",
                            instrument_id=symbol,
                            side="buy",
                            quantity=shares,
                            order_type="limit",
                            limit_price=ask,
                        )
                    )
                    new_state["has_exited"] = True
                    new_state["exit_reason"] = "take_profit"
                # Stop Loss cortos: precio perfora la resistencia hacia arriba
                elif ask >= short_stop:
                    intents.append(
                        OrderIntent(
                            order_id=f"buy-cover-sl-{symbol.lower()}-{bar_date}",
                            instrument_id=symbol,
                            side="buy",
                            quantity=shares,
                            order_type="limit",
                            limit_price=ask,
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


def evaluate_market_opportunity(
    symbol: str,
    last_close: float,
    levels: dict[str, Any],
    multi_levels: dict[str, Any],
    current_position: int = 0,
    entry_price: float | None = None,
    unrealized_pnl: float | None = None,
    control_zones: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Evalúa cuantitativa y chartistamente si AHORA es buen momento para entrar en LARGOS o CORTOS.

    Calcula explícitamente para ambas direcciones:
      - Precio Objetivo (Take Profit) basado en niveles técnicos multitemporales (60d, 252d, 5A).
      - Nivel de Invalidación (Stop Loss técnico).
      - Distancia, Potencial de beneficio % y Riesgo %.
      - Ratio Beneficio / Riesgo (R:R).
      - Dictamen claro: 'COMPRAR (LONG)', 'VENDER / CORTO (SHORT)', o 'MANTENER / ESPERAR'.
      - Justificación detallada.
    """
    soporte_60d = float(levels["swing_low"])
    resistencia_60d = float(levels["swing_high"])
    sma_60d = float(levels["sma"])

    def _val(horizon: str, key: str, fallback: float) -> float:
        h = multi_levels.get(horizon, {})
        val = h.get(key)
        try:
            return float(val) if val is not None else fallback
        except (ValueError, TypeError):
            return fallback

    sma_200d = _val("200d", "sma", sma_60d)
    soporte_120d = _val("120d", "swing_low", soporte_60d)
    resistencia_120d = _val("120d", "swing_high", resistencia_60d)
    soporte_252d = _val("252d", "swing_low", soporte_120d)
    resistencia_252d = _val("252d", "swing_high", resistencia_120d)
    soporte_5y = _val("max", "swing_low", soporte_252d)
    resistencia_5y = _val("max", "swing_high", resistencia_252d)

    # 1. ESCENARIO LARGOS (COMPRAR / LONG)
    # Stop Loss de protección por debajo del soporte estructural relevante (5% de margen)
    if last_close >= soporte_60d * 0.98:
        # Precio sobre o testeando soporte de 60d
        sl_long = round(soporte_60d * 0.95, 2)
    elif last_close >= soporte_252d * 0.98:
        # Soporte de 60d perforado; testeando soporte de 1 año
        sl_long = round(soporte_252d * 0.95, 2)
    elif last_close >= soporte_5y * 0.98:
        sl_long = round(soporte_5y * 0.95, 2)
    else:
        sl_long = round(last_close * 0.95, 2)

    # Asegurar que el SL quede estrictamente por debajo del precio actual
    if sl_long >= last_close:
        sl_long = round(last_close * 0.95, 2)

    # Take Profit 1 y 2 para Largos
    if last_close < resistencia_60d * 0.99:
        tp1_long = resistencia_60d
    elif resistencia_252d > last_close * 1.01:
        tp1_long = resistencia_252d
    elif resistencia_5y > last_close * 1.01:
        tp1_long = resistencia_5y
    else:
        tp1_long = round(last_close * 1.10, 2)
    if tp1_long <= last_close:
        tp1_long = round(last_close * 1.10, 2)

    cands_long = [r for r in [resistencia_252d, resistencia_5y, round(last_close * 1.15, 2)] if r > tp1_long * 1.005]
    tp2_long = round(min(cands_long) if cands_long else round(tp1_long * 1.10, 2), 2)

    risk_long = max(round(last_close - sl_long, 2), 0.01)
    reward_long = max(round(tp1_long - last_close, 2), 0.0)
    risk_long_pct = round((risk_long / last_close) * 100, 2) if last_close > 0 else 0.0
    reward_long_pct = round((reward_long / last_close) * 100, 2) if last_close > 0 else 0.0
    rr_long = round(reward_long / risk_long, 2) if risk_long > 0 else 0.0

    # 2. ESCENARIO CORTOS (VENDER / SHORT)
    # Stop Loss de protección por encima de la resistencia estructural relevante (5% de margen)
    if last_close <= resistencia_60d * 1.02:
        # Precio bajo o testeando techo de 60d
        sl_short = round(resistencia_60d * 1.05, 2)
    elif last_close <= resistencia_252d * 1.02:
        # Techo 60d perforado; testeando techo de 1 año
        sl_short = round(resistencia_252d * 1.05, 2)
    elif last_close <= resistencia_5y * 1.02:
        sl_short = round(resistencia_5y * 1.05, 2)
    else:
        sl_short = round(last_close * 1.05, 2)

    # Asegurar que el SL quede estrictamente por encima del precio actual
    if sl_short <= last_close:
        sl_short = round(last_close * 1.05, 2)

    # Take Profit 1 y 2 para Cortos (caída hacia soportes estructurales)
    if last_close > soporte_60d * 1.01:
        tp1_short = soporte_60d
    elif soporte_252d < last_close * 0.99:
        tp1_short = soporte_252d
    elif soporte_5y < last_close * 0.99:
        tp1_short = soporte_5y
    else:
        tp1_short = round(last_close * 0.90, 2)
    if tp1_short >= last_close:
        tp1_short = round(last_close * 0.90, 2)

    cands_short = [s for s in [soporte_252d, soporte_5y, round(last_close * 0.85, 2)] if s < tp1_short * 0.995]
    tp2_short = round(max(cands_short) if cands_short else round(tp1_short * 0.90, 2), 2)

    risk_short = max(round(sl_short - last_close, 2), 0.01)
    reward_short = max(round(last_close - tp1_short, 2), 0.0)
    risk_short_pct = round((risk_short / last_close) * 100, 2) if last_close > 0 else 0.0
    reward_short_pct = round((reward_short / last_close) * 100, 2) if last_close > 0 else 0.0
    rr_short = round(reward_short / risk_short, 2) if risk_short > 0 else 0.0

    nearest_sup_zone = control_zones.get("nearest_support") if control_zones else None
    nearest_res_zone = control_zones.get("nearest_resistance") if control_zones else None
    active_zone = control_zones.get("active_zone") if control_zones else None

    # Si no se definió nearest_sup_zone pero existe una zona activa alcista, usarla como soporte
    if not nearest_sup_zone and active_zone and active_zone.get("type") == "ALCISTA":
        nearest_sup_zone = active_zone
    if not nearest_res_zone and active_zone and active_zone.get("type") == "BAJISTA":
        nearest_res_zone = active_zone

    sup_zone_str = nearest_sup_zone["description"] if nearest_sup_zone else "-"
    dist_sup_zone_pct = nearest_sup_zone["dist_pct"] if nearest_sup_zone else None

    res_zone_str = nearest_res_zone["description"] if nearest_res_zone else "-"
    dist_res_zone_pct = nearest_res_zone["dist_pct"] if nearest_res_zone else None

    # 3. ZONAS TÉCNICAS Y PROXIMIDAD
    # dist_soporte_pct: positivo si está por encima de soporte, negativo si perforó por debajo
    dist_soporte_pct = round(((last_close - soporte_60d) / soporte_60d) * 100, 2) if soporte_60d > 0 else 0.0
    # dist_resistencia_pct: positivo si está por debajo de resistencia, negativo si perforó por encima
    dist_resistencia_pct = round(((resistencia_60d - last_close) / resistencia_60d) * 100, 2) if resistencia_60d > 0 else 0.0

    rango_60d = max(resistencia_60d - soporte_60d, 0.01)
    pos_en_rango_pct = round(((last_close - soporte_60d) / rango_60d) * 100, 1)

    filtro_tendencia_macro = "Alcista (Precio > SMA 200)" if last_close >= sma_200d else "Bajista/Correctiva (Precio < SMA 200)"

    # Zona válida: el precio debe estar dentro de una vecindad técnica de la banda (-2.0% a +3.5%)
    # y respetar el stop loss estructural, evitando recomendar compras en desplomes o ventas en roturas alcistas.
    in_donchian_long_zone = (-2.0 <= dist_soporte_pct <= 3.5 and last_close > sl_long)
    in_donchian_short_zone = (-2.0 <= dist_resistencia_pct <= 3.5 and last_close < sl_short)

    in_long_zone = in_donchian_long_zone
    in_short_zone = in_donchian_short_zone

    # Evaluación de Zona de Control de Alta Convicción (Gaps relevantes / Soportes institucionales)
    in_control_zone_long = False
    in_control_zone_short = False

    if nearest_sup_zone:
        z_high = nearest_sup_zone["upper_bound"]
        z_low = nearest_sup_zone["lower_bound"]
        dist_to_zc_sup = round(((last_close - z_high) / z_high) * 100, 2)
        if (-2.0 <= dist_to_zc_sup <= 4.0) or (z_low <= last_close <= z_high):
            sl_zc = round(z_low * 0.95, 2)
            if sl_zc < last_close:
                risk_zc = max(round(last_close - sl_zc, 2), 0.01)
                rr_zc = round(reward_long / risk_zc, 2) if risk_zc > 0 else 0.0
                if rr_zc >= 1.5:
                    in_control_zone_long = True
                    if not in_long_zone:
                        sl_long = sl_zc
                        risk_long = risk_zc
                        risk_long_pct = round((risk_long / last_close) * 100, 2) if last_close > 0 else 0.0
                        rr_long = rr_zc
                        in_long_zone = True

    if nearest_res_zone:
        z_high = nearest_res_zone["upper_bound"]
        z_low = nearest_res_zone["lower_bound"]
        dist_to_zc_res = round(((z_low - last_close) / z_low) * 100, 2)
        if (-2.0 <= dist_to_zc_res <= 4.0) or (z_low <= last_close <= z_high):
            sl_zc_s = round(z_high * 1.05, 2)
            if sl_zc_s > last_close:
                risk_zc_s = max(round(sl_zc_s - last_close, 2), 0.01)
                rr_zc_s = round(reward_short / risk_zc_s, 2) if risk_zc_s > 0 else 0.0
                if rr_zc_s >= 1.5:
                    in_control_zone_short = True
                    if not in_short_zone:
                        sl_short = sl_zc_s
                        risk_short = risk_zc_s
                        risk_short_pct = round((risk_short / last_close) * 100, 2) if last_close > 0 else 0.0
                        rr_short = rr_zc_s
                        in_short_zone = True

    dist_sup_str = f"+{dist_soporte_pct:.2f}%" if dist_soporte_pct >= 0 else f"{dist_soporte_pct:.2f}%"
    dist_res_str = f"-{dist_resistencia_pct:.2f}%" if dist_resistencia_pct >= 0 else f"+{abs(dist_resistencia_pct):.2f}%"

    # 4. DICTAMEN DE NUEVA OPORTUNIDAD (LARGOS vs CORTOS)
    if in_long_zone and rr_long >= 1.5:
        veredicto_oportunidad = "COMPRAR (LONG)"
        zc_info_str = f" [Zona de Control Soporte: {sup_zone_str}]" if in_control_zone_long else ""
        if in_control_zone_long and not in_donchian_long_zone and nearest_sup_zone:
            razonamiento = (
                f"MOMENTO ÓPTIMO PARA LARGOS{zc_info_str}: El precio cotiza a ${last_close:.2f}, testeando Zona de Control "
                f"institucional de Soporte (${nearest_sup_zone['lower_bound']:.2f}-${nearest_sup_zone['upper_bound']:.2f}). "
                f"Asimetría R:R favorable de {rr_long:.2f}:1. "
                f"Precio Objetivo: ${tp1_long:.2f} (+{reward_long_pct:.2f}%). "
                f"Stop Loss: ${sl_long:.2f} (-{risk_long_pct:.2f}%)."
            )
        else:
            razonamiento = (
                f"MOMENTO ÓPTIMO PARA LARGOS{zc_info_str}: El precio cotiza a ${last_close:.2f}, en zona de soporte clave "
                f"(${soporte_60d:.2f}, a {dist_sup_str}). Asimetría R:R favorable de {rr_long:.2f}:1. "
                f"Precio Objetivo: ${tp1_long:.2f} (+{reward_long_pct:.2f}%). "
                f"Stop Loss: ${sl_long:.2f} (-{risk_long_pct:.2f}%)."
            )
    elif in_short_zone and rr_short >= 1.5:
        veredicto_oportunidad = "VENDER / CORTO (SHORT)"
        zc_info_str = f" [Zona de Control Resistencia: {res_zone_str}]" if in_control_zone_short else ""
        if in_control_zone_short and not in_donchian_short_zone and nearest_res_zone:
            razonamiento = (
                f"MOMENTO ÓPTIMO PARA CORTOS{zc_info_str}: El precio cotiza a ${last_close:.2f}, testeando Zona de Control "
                f"institucional de Resistencia (${nearest_res_zone['lower_bound']:.2f}-${nearest_res_zone['upper_bound']:.2f}). "
                f"Asimetría R:R favorable de {rr_short:.2f}:1. "
                f"Precio Objetivo: ${tp1_short:.2f} (+{reward_short_pct:.2f}% de caída potencial). "
                f"Stop Loss: ${sl_short:.2f} (-{risk_short_pct:.2f}%)."
            )
        else:
            razonamiento = (
                f"MOMENTO ÓPTIMO PARA CORTOS{zc_info_str}: El precio cotiza a ${last_close:.2f}, testeando zona de techo/resistencia "
                f"(${resistencia_60d:.2f}, a {dist_res_str} del techo). Asimetría R:R favorable de {rr_short:.2f}:1. "
                f"Precio Objetivo: ${tp1_short:.2f} (+{reward_short_pct:.2f}% de caída potencial). "
                f"Stop Loss: ${sl_short:.2f} (-{risk_short_pct:.2f}%)."
            )
    else:
        veredicto_oportunidad = "MANTENER / ESPERAR"
        if pos_en_rango_pct > 100.0:
            pos_desc = f"por encima del canal 60d ({pos_en_rango_pct:.1f}%)"
        elif pos_en_rango_pct < 0.0:
            pos_desc = f"por debajo del canal 60d ({pos_en_rango_pct:.1f}%)"
        else:
            pos_desc = f"en zona neutral ({pos_en_rango_pct:.1f}% del canal 60d)"

        zc_hint_list = []
        if nearest_sup_zone:
            zc_hint_list.append(f"Zona Control Soporte: {sup_zone_str}")
        if nearest_res_zone:
            zc_hint_list.append(f"Zona Control Resistencia: {res_zone_str}")
        zc_note = f" [{' | '.join(zc_hint_list)}]." if zc_hint_list else "."

        razonamiento = (
            f"SITUACIÓN {pos_desc.upper()}. El precio (${last_close:.2f}) cotiza entre soporte "
            f"(${soporte_60d:.2f}, {dist_sup_str}) y resistencia (${resistencia_60d:.2f}, {dist_res_str}){zc_note} "
            f"No hay ventaja estadística inmediata para abrir posición (R:R Long: {rr_long:.2f}:1 | R:R Short: {rr_short:.2f}:1). "
            f"RECOMENDACIÓN: ESPERAR retroceso hacia ${soporte_60d:.2f}"
            + (f" (o Zona de Control institucional ${nearest_sup_zone['lower_bound']:.2f}-${nearest_sup_zone['upper_bound']:.2f})" if nearest_sup_zone else "")
            + f" para buscar COMPRA (LONG) o test de ${resistencia_60d:.2f} para buscar VENTA / CORTO (SHORT)."
        )

    # 5. EVALUACIÓN DE ACCIÓN OPERATIVA PARA HOY (Teniendo en cuenta cartera activa)
    unit_name = "contratos" if symbol in ("MGC", "MGC=F") else "acciones"
    multiplier = 10.0 if symbol in ("MGC", "MGC=F") else 1.0

    if current_position > 0:
        entry_ref = entry_price or last_close
        rentabilidad_pct = round(((last_close - entry_ref) / entry_ref) * 100, 2)
        pnl_val = unrealized_pnl if unrealized_pnl is not None else round((last_close - entry_ref) * current_position * multiplier, 2)
        sign_str = "+$" if pnl_val >= 0 else "-$"
        diff_abs = abs(pnl_val)

        if last_close >= resistencia_60d:
            requiere_accion = "SÍ — VENDER (Take Profit)"
            instruccion = f"Objetivo de resistencia alcanzado (${resistencia_60d:.2f}). Vender {current_position} {unit_name} con beneficio ({rentabilidad_pct:+.2f}%)."
        elif last_close <= sl_long:
            requiere_accion = "SÍ — VENDER (Stop Loss)"
            instruccion = f"Stop Loss perforado (${sl_long:.2f}). Vender {current_position} {unit_name} para proteger capital."
        else:
            requiere_accion = "NO — MANTENER"
            instruccion = (
                f"Mantener {current_position} {unit_name}. Beneficio latente: {sign_str}{diff_abs:,.2f} USD ({rentabilidad_pct:+.2f}%). "
                f"Objetivo: ${tp1_long:.2f} | Stop: ${sl_long:.2f}"
            )
    elif current_position < 0:
        entry_ref = entry_price or last_close
        rentabilidad_pct = round(((entry_ref - last_close) / entry_ref) * 100, 2)
        pnl_val = unrealized_pnl if unrealized_pnl is not None else round((entry_ref - last_close) * abs(current_position) * multiplier, 2)
        sign_str = "+$" if pnl_val >= 0 else "-$"
        diff_abs = abs(pnl_val)

        if last_close <= soporte_60d:
            requiere_accion = "SÍ — RECOMPRAR (Take Profit Corto)"
            instruccion = f"Objetivo de soporte alcanzado (${soporte_60d:.2f}). Recomprar {abs(current_position)} {unit_name} para consolidar ganancias ({rentabilidad_pct:+.2f}%)."
        elif last_close >= sl_short:
            requiere_accion = "SÍ — RECOMPRAR (Stop Loss Corto)"
            instruccion = f"Stop Loss de corto perforado (${sl_short:.2f}). Recomprar {abs(current_position)} {unit_name} para limitar pérdidas."
        else:
            requiere_accion = "NO — MANTENER CORTO"
            instruccion = (
                f"Mantener posición corta ({abs(current_position)} {unit_name}). Beneficio latente: {sign_str}{diff_abs:,.2f} USD ({rentabilidad_pct:+.2f}%). "
                f"Objetivo: ${tp1_short:.2f} | Stop: ${sl_short:.2f}"
            )
    else:
        rentabilidad_pct = 0.0
        if "COMPRAR" in veredicto_oportunidad:
            requiere_accion = "SÍ — COMPRAR"
            instruccion = (
                f"Precio en zona de soporte (${soporte_60d:.2f}). Comprar {symbol} "
                f"con SL en ${sl_long:.2f} y TP en ${tp1_long:.2f} (R:R {rr_long:.2f}:1)."
            )
        elif "CORTO" in veredicto_oportunidad or "VENDER" in veredicto_oportunidad:
            requiere_accion = "SÍ — VENDER / CORTO"
            instruccion = (
                f"Precio en zona de resistencia (${resistencia_60d:.2f}). Ponerse CORTO "
                f"con SL en ${sl_short:.2f} y TP en ${tp1_short:.2f} (R:R {rr_short:.2f}:1)."
            )
        else:
            requiere_accion = "NO — ESPERAR"
            instruccion = (
                f"Sin posición. Precio fuera de zona de entrada. "
                f"Esperar retroceso a zona ${soporte_60d:.2f} para largos o test de ${resistencia_60d:.2f} para cortos."
            )

    return {
        "symbol": symbol,
        "last_close": last_close,
        # Largos
        "long_target": tp1_long,
        "long_target_2": tp2_long,
        "long_stop_loss": sl_long,
        "long_risk_usd": risk_long,
        "long_reward_usd": reward_long,
        "long_risk_pct": risk_long_pct,
        "long_reward_pct": reward_long_pct,
        "long_rr_ratio": rr_long,
        "in_long_zone": in_long_zone,
        # Cortos
        "short_target": tp1_short,
        "short_target_2": tp2_short,
        "short_stop_loss": sl_short,
        "short_risk_usd": risk_short,
        "short_reward_usd": reward_short,
        "short_risk_pct": risk_short_pct,
        "short_reward_pct": reward_short_pct,
        "short_rr_ratio": rr_short,
        "in_short_zone": in_short_zone,
        # Niveles base
        "soporte_60d": soporte_60d,
        "resistencia_60d": resistencia_60d,
        "sma_60d": sma_60d,
        "sma_200d": sma_200d,
        "dist_soporte_pct": dist_soporte_pct,
        "dist_resistencia_pct": dist_resistencia_pct,
        "pos_en_rango_pct": pos_en_rango_pct,
        "filtro_tendencia_macro": filtro_tendencia_macro,
        # Zonas de Control (Gaps Históricos)
        "control_zone_soporte": sup_zone_str,
        "control_zone_soporte_dist_pct": dist_sup_zone_pct,
        "control_zone_resistencia": res_zone_str,
        "control_zone_resistencia_dist_pct": dist_res_zone_pct,
        "nearest_support_zone": nearest_sup_zone,
        "nearest_resistance_zone": nearest_res_zone,
        "active_control_zone": active_zone,
        # Veredicto y operativa
        "veredicto": veredicto_oportunidad,
        "requiere_accion": requiere_accion,
        "instruccion": instruccion,
        "razonamiento": razonamiento,
        "rentabilidad_pct": rentabilidad_pct,
    }


def run_stock_macro_backtest(
    symbol: str,
    shares: int = 100,
    lookback_bars: int = 60,
    store: MarketDataStore | None = None,
    catalog: Catalog | None = None,
    base_dir: Path | None = None,
    direction: str = "long",
    corporate_action_policy: EquityActionPolicy | None = None,
) -> tuple[list[dict[str, Any]], dict[str, Any], dict[str, Any], dict[str, Any]]:
    """Ejecuta la simulación auditada en un activo (GOLD, NEM, MGC) y devuelve métricas completas."""
    root = Path(__file__).resolve().parents[2]
    catalog_path = root / "data" / "gold_miners_stocks.json"

    if catalog is None or not any(inst.get("id") == symbol for inst in catalog.instruments):
        if symbol in ("MGC", "MGC=F"):
            mgc_path = root / "data" / "cme_micro_gold.json"
            catalog = load_catalog(mgc_path) if mgc_path.exists() else load_catalog(catalog_path)
        else:
            catalog = load_catalog(catalog_path)

    if store is None:
        store = MarketDataStore(base_dir=base_dir)

    # 1. Asegurar histórico en Parquet
    parquet_path = store._parquet_path(symbol, timeframe="1d")
    if not parquet_path.exists():
        if symbol in ("MGC", "MGC=F"):
            store.fetch_and_store_cme_mgc(period="2y")
        else:
            store.fetch_and_store_ticker(symbol, period="5y")

    # 2. Análisis DuckDB de niveles macro
    levels = store.get_market_levels(symbol, timeframe="1d", lookback_bars=lookback_bars)
    bars_df = store.load_bars(symbol, timeframe="1d")
    if bars_df.empty:
        raise ValueError(f"No hay barras históricas para {symbol}")

    action_report = (corporate_action_policy or EquityActionPolicy()).validate_daily(bars_df)

    recent_bars = bars_df.tail(lookback_bars).copy()

    # Obtener fecha más reciente y cierre previo para calcular variación del día
    last_row = recent_bars.iloc[-1]
    prev_row = recent_bars.iloc[-2] if len(recent_bars) > 1 else last_row

    last_date = pd.to_datetime(last_row["date"]).strftime("%Y-%m-%d")
    last_close = round(float(last_row["close"]), 2)
    prev_close = round(float(prev_row["close"]), 2)
    day_change = round(last_close - prev_close, 2)
    day_change_pct = round((day_change / prev_close) * 100, 2) if prev_close > 0 else 0.0

    soporte = levels["swing_low"]
    resistencia = levels["swing_high"]
    stop = (soporte * Decimal("0.95")).quantize(Decimal("0.01"))
    short_stop = (resistencia * Decimal("1.05")).quantize(Decimal("0.01"))

    config = StrategyConfig(
        strategy_id=f"{symbol.lower()}-macro-trend",
        instance_id=f"{symbol.lower()}-inst-01",
        strategy_version="1.0.0",
        parameters={
            "symbol": symbol,
            "support_price": str(soporte),
            "target_price": str(resistencia),
            "stop_loss_price": str(stop),
            "short_stop_loss_price": str(short_stop),
            "short_target_price": str(soporte),
            "shares": str(shares),
            "direction": direction,
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

    is_future = symbol in ("MGC", "MGC=F")
    multiplier_val = 10.0 if is_future else 1.0
    commission_per_unit = Decimal("0.50") if is_future else Decimal("0.005")

    processor = EventProcessor(
        pipeline,
        catalog,
        strategy,
        config=EventProcessorConfig(
            matching_policy=MatchingPolicy(
                commission=CommissionModel(
                    per_unit=commission_per_unit,
                    currency="USD",
                    currency_precision=2,
                )
            )
        ),
        session_id=f"{symbol.lower()}-macro-session",
    )

    processor.start()

    for idx, (_, row) in enumerate(recent_bars.iterrows()):
        date_val = pd.to_datetime(row["date"])
        date_ns = int(date_val.timestamp() * 1e9)
        close_price = float(row["close"])
        bid = close_price - 0.01
        ask = close_price + 0.01
        frame = create_market_frame(idx, date_ns, symbol, bid, ask)
        processor.submit(frame)

    report = processor.finish(timeout=5)
    portfolio = report.portfolio

    # 3. Extraer operaciones ejecutadas con trazabilidad bi-direccional
    ops: list[dict[str, Any]] = []
    entry_price_val: float | None = None
    entry_date_val: str | None = None

    if report.fills:
        for fill in report.fills:
            oid = fill.order_id
            clean_oid = oid.split("/")[-1] if "/" in oid else oid
            parts = clean_oid.split("-")
            fill_date = "-".join(parts[-3:]) if len(parts) >= 4 else last_date
            qty = int(fill.quantity)
            price = float(fill.price)
            comm = float(fill.commission)
            val_total = round(qty * price * multiplier_val, 2)

            is_long_entry = clean_oid.startswith("buy-macro-")
            is_long_exit = clean_oid.startswith("sell-tp-") or clean_oid.startswith("sell-sl-")
            is_short_entry = clean_oid.startswith("sell-short-")
            is_short_exit = clean_oid.startswith("buy-cover-")

            pnl_op: float | str = "-"
            if is_long_entry:
                tipo = "COMPRA"
                motivo = "Rebote en soporte de 60 días"
                entry_price_val = price
                entry_date_val = fill_date
                sug_sl = float(stop)
                sug_tp = float(resistencia)
            elif is_short_entry:
                tipo = "VENTA (CORTO)"
                motivo = "Rechazo en resistencia de 60 días"
                entry_price_val = price
                entry_date_val = fill_date
                sug_sl = float(short_stop)
                sug_tp = float(soporte)
            elif is_long_exit:
                tipo = "VENTA (CIERRE)"
                motivo = "Take Profit alcanzado" if "tp" in oid else "Stop Loss ejecutado"
                if entry_price_val is not None:
                    pnl_op = round((price - entry_price_val) * qty * multiplier_val - comm, 2)
                sug_sl = float(stop)
                sug_tp = float(resistencia)
            elif is_short_exit:
                tipo = "COMPRA (COBERTURA)"
                motivo = "Take Profit corto alcanzado" if "tp" in oid else "Stop Loss corto ejecutado"
                if entry_price_val is not None:
                    pnl_op = round((entry_price_val - price) * qty * multiplier_val - comm, 2)
                sug_sl = float(short_stop)
                sug_tp = float(soporte)
            else:
                side_str = "COMPRA" if "buy" in oid else "VENTA"
                tipo = side_str
                motivo = "Operación de mercado"
                sug_sl = float(stop)
                sug_tp = float(resistencia)

            ops.append({
                "Fecha": fill_date,
                "Símbolo": symbol,
                "Tipo": tipo,
                "Cantidad (Acciones)": qty,
                "Precio ($ USD)": price,
                "Valor Total ($ USD)": val_total,
                "Comisión ($ USD)": comm,
                "Stop Loss Sugerido ($)": sug_sl,
                "Take Profit Sugerido ($)": sug_tp,
                "P&L Operación ($ USD)": pnl_op,
                "Motivo / Señal": motivo,
            })

    # 4. Métricas multi-horizonte y evaluación bi-direccional completa
    soporte_f = float(soporte)
    resistencia_f = float(resistencia)
    dist_soporte_pct = round(((last_close - soporte_f) / soporte_f) * 100, 2) if soporte_f > 0 else 0.0
    dist_resistencia_pct = round(((resistencia_f - last_close) / resistencia_f) * 100, 2) if resistencia_f > 0 else 0.0

    multi = store.get_multi_horizon_levels(symbol)
    control_zones = store.get_price_control_zones(symbol, min_gap_pct=0.03, current_price=last_close)

    # 5. Resumen de cartera
    pos_actual = int(portfolio.positions.get(symbol, Decimal("0")))
    unrealized_pnl = float(portfolio.unrealized_pnl.get("USD", Decimal("0")))
    realized_pnl = float(portfolio.realized_pnl.get("USD", Decimal("0")))
    comisiones = float(portfolio.commissions_paid.get("USD", Decimal("0")))

    unit_header = "Contratos" if is_future else "Acciones"
    resumen_cartera = {
        f"Posición {symbol} ({unit_header})": pos_actual,
        f"P&L Latente {symbol} ($ USD)": unrealized_pnl,
        f"P&L Realizado {symbol} ($ USD)": realized_pnl,
        f"Comisiones {symbol} ($ USD)": comisiones,
    }

    # 6. Evaluación analítica de oportunidad de entrada y acción requerida hoy
    opp = evaluate_market_opportunity(
        symbol=symbol,
        last_close=last_close,
        levels=levels,
        multi_levels=multi,
        current_position=pos_actual,
        entry_price=entry_price_val,
        unrealized_pnl=unrealized_pnl,
        control_zones=control_zones,
    )

    metricas_mercado = {
        "Símbolo": symbol,
        "Activo": COMPANY_NAMES.get(symbol, symbol),
        "Último Cierre Oficial ($)": last_close,
        # Oportunidad Bi-direccional
        "Dictamen Oportunidad": opp["veredicto"],
        "Objetivo Largos ($)": opp["long_target"],
        "Stop Loss Largos ($)": opp["long_stop_loss"],
        "Ratio B/R Largos": opp["long_rr_ratio"],
        "Objetivo Cortos ($)": opp["short_target"],
        "Stop Loss Cortos ($)": opp["short_stop_loss"],
        "Ratio B/R Cortos": opp["short_rr_ratio"],
        # Corto/Medio Plazo (60d ~3m)
        "Soporte 60d ($)": soporte_f,
        "Resistencia 60d ($)": resistencia_f,
        "SMA 60d ($)": float(levels["sma"]),
        # Semestral (120d ~6m)
        "Soporte 120d ($)": float(multi["120d"]["swing_low"]) if multi["120d"]["swing_low"] else "-",
        "Resistencia 120d ($)": float(multi["120d"]["swing_high"]) if multi["120d"]["swing_high"] else "-",
        "SMA 120d ($)": float(multi["120d"]["sma"]) if multi["120d"]["sma"] else "-",
        # Media Institucional (200d ~10m)
        "Soporte 200d ($)": float(multi["200d"]["swing_low"]) if multi["200d"]["swing_low"] else "-",
        "Resistencia 200d ($)": float(multi["200d"]["swing_high"]) if multi["200d"]["swing_high"] else "-",
        "SMA 200d Institucional ($)": opp["sma_200d"],
        # Anual (252d ~1 año)
        "Soporte 1 Año ($)": float(multi["252d"]["swing_low"]) if multi["252d"]["swing_low"] else "-",
        "Resistencia 1 Año ($)": float(multi["252d"]["swing_high"]) if multi["252d"]["swing_high"] else "-",
        "SMA 252d Anual ($)": float(multi["252d"]["sma"]) if multi["252d"]["sma"] else "-",
        # Histórico Máximo (5 años / 1255 sesiones)
        "Soporte Histórico 5A ($)": float(multi["max"]["swing_low"]) if multi["max"]["swing_low"] else "-",
        "Resistencia Histórica 5A ($)": float(multi["max"]["swing_high"]) if multi["max"]["swing_high"] else "-",
        "SMA Histórica 5A ($)": float(multi["max"]["sma"]) if multi["max"]["sma"] else "-",
        # Zonas de Control (Gaps Históricos)
        "Zona Control Soporte": opp["control_zone_soporte"],
        "Distancia Zona Soporte (%)": opp["control_zone_soporte_dist_pct"] if opp["control_zone_soporte_dist_pct"] is not None else "-",
        "Zona Control Resistencia": opp["control_zone_resistencia"],
        "Distancia Zona Resistencia (%)": opp["control_zone_resistencia_dist_pct"] if opp["control_zone_resistencia_dist_pct"] is not None else "-",
        # Parámetros operativos
        "Distancia a Soporte (%)": dist_soporte_pct,
        "Distancia a Resistencia (%)": dist_resistencia_pct,
        "Stop Loss Recomendado ($)": opp["long_stop_loss"],
    }

    estado_diario = {
        "Símbolo": symbol,
        "Empresa": COMPANY_NAMES.get(symbol, symbol),
        "Fecha Cotización": last_date,
        "Último Precio ($)": last_close,
        "Var. Día ($)": day_change,
        "Var. Día (%)": day_change_pct,
        "Dictamen / Oportunidad": opp["veredicto"],
        "Objetivo Largos ($)": opp["long_target"],
        "Stop Loss Largos ($)": opp["long_stop_loss"],
        "Ratio B/R Largos": opp["long_rr_ratio"],
        "Potencial Largos (%)": opp["long_reward_pct"],
        "Objetivo Cortos ($)": opp["short_target"],
        "Stop Loss Cortos ($)": opp["short_stop_loss"],
        "Ratio B/R Cortos": opp["short_rr_ratio"],
        "Potencial Cortos (%)": opp["short_reward_pct"],
        "Soporte 60d ($)": soporte_f,
        "Resistencia 60d ($)": resistencia_f,
        "SMA 60d ($)": float(levels["sma"]),
        "SMA 200d Institucional ($)": opp["sma_200d"],
        "Filtro Tendencia Macro": opp["filtro_tendencia_macro"],
        "Soporte Anual 252d ($)": float(multi["252d"]["swing_low"]) if multi["252d"]["swing_low"] else "-",
        "Resistencia Anual 252d ($)": float(multi["252d"]["swing_high"]) if multi["252d"]["swing_high"] else "-",
        "Soporte 5 Años ($)": float(multi["max"]["swing_low"]) if multi["max"]["swing_low"] else "-",
        "Resistencia 5 Años ($)": float(multi["max"]["swing_high"]) if multi["max"]["swing_high"] else "-",
        "Zona Control Soporte": opp["control_zone_soporte"],
        "Distancia Zona Soporte (%)": opp["control_zone_soporte_dist_pct"] if opp["control_zone_soporte_dist_pct"] is not None else "-",
        "Zona Control Resistencia": opp["control_zone_resistencia"],
        "Distancia Zona Resistencia (%)": opp["control_zone_resistencia_dist_pct"] if opp["control_zone_resistencia_dist_pct"] is not None else "-",
        "Stop Loss Sugerido ($)": opp["long_stop_loss"],
        "Posición Actual (Accs)": pos_actual,
        "Precio Entrada ($)": entry_price_val if entry_price_val else "-",
        "Fecha Entrada": entry_date_val if entry_date_val else "-",
        "Valor Mercado ($ USD)": round(abs(pos_actual) * last_close * multiplier_val, 2),
        "P&L Latente ($ USD)": unrealized_pnl,
        "Rentabilidad (%)": opp["rentabilidad_pct"],
        "¿Requiere Acción Hoy?": opp["requiere_accion"],
        "Instrucción Operativa": opp["instruccion"],
        "Razonamiento Detallado": opp["razonamiento"],
    }

    resumen_cartera["Base de rendimiento"] = action_report["return_basis"]
    resumen_cartera["Base de precios declarada"] = action_report["policy"]["price_basis"]
    resumen_cartera["Dividendos en efectivo incluidos"] = False
    estado_diario["Base de rendimiento"] = action_report["return_basis"]
    estado_diario["Dividendos en efectivo incluidos"] = False

    return ops, metricas_mercado, resumen_cartera, estado_diario
