"""Script de Actualización Diaria Automatizada ("Botón de 1 Clic").

Descarga los últimos precios de Barrick Gold (GOLD) y Newmont (NEM),
calcula niveles macro (soporte 60d, resistencia 60d, SMA 60d) con DuckDB,
evalúa señales de compra / toma de beneficios / stop loss,
actualiza el diario multi-pestaña de trading en Excel (data/diario_trading_oro.xlsx),
y muestra un resumen visual en consola del estado del mercado y órdenes pendientes.
"""

from __future__ import annotations

import argparse
from datetime import datetime
from decimal import Decimal
from pathlib import Path
import sys

# Asegurar UTF-8 en salida por consola en Windows
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")

# Añadir directorio 'src' al path de Python
ROOT_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT_DIR / "src"))

from market_catalog import (
    COMPANY_NAMES,
    ExcelTradeJournal,
    MarketDataStore,
    load_catalog,
    run_stock_macro_backtest,
)

CONFIG_SHARES = {
    "GOLD": 200,
    "NEM": 100,
    "MGC": 1,
    "BE": 50,
}

VENUE_NAMES = {
    "GOLD": "NYSE",
    "NEM": "NYSE",
    "MGC": "CME Globex",
    "BE": "NYSE",
}

UNIT_NAMES = {
    "GOLD": "acciones",
    "NEM": "acciones",
    "MGC": "contratos",
    "BE": "acciones",
}


def download_market_data(store: MarketDataStore, tickers: list[str], force_refresh: bool = True) -> None:
    """Descarga cotizaciones oficiales más recientes desde Yahoo Finance y las guarda en Parquet."""
    print(">>> [PASO 1/3] Actualizando cotizaciones oficiales desde Yahoo Finance...")
    for ticker in tickers:
        name = COMPANY_NAMES.get(ticker, ticker)
        parquet_file = store._parquet_path(ticker, timeframe="1d")
        try:
            print(f"    Descargando cotizaciones para {ticker} ({name})...", end=" ", flush=True)
            if ticker in ("MGC", "MGC=F"):
                store.fetch_and_store_cme_mgc(period="2y")
            else:
                store.fetch_and_store_ticker(ticker, period="5y")
            print("[OK - Actualizado]")
        except Exception as exc:
            if parquet_file.exists():
                print(f"[AVISO - Usando caché local: {exc}]")
            else:
                print(f"[ERROR: {exc}]")
                raise RuntimeError(f"No fue posible descargar ni encontrar datos para {ticker}: {exc}") from exc


def main() -> int:
    parser = argparse.ArgumentParser(description="Actualizador Diario de Trading de Oro y Mineras (GOLD / NEM / MGC)")
    parser.add_argument("--no-download", action="store_true", help="Omitir la descarga desde Yahoo Finance y usar Parquet existente")
    args = parser.parse_args()

    now_str = datetime.now().strftime("%Y-%m-%d %H:%M:%S")

    print("=" * 86)
    print("   MOTOR DE TRADING Y SEGUIMIENTO DIARIO: ORO Y MINERAS (GOLD / NEM / MGC)")
    print("   Evaluación Bi-direccional: Largos (Comprar) vs Cortos (Vender)")
    print(f"   Ejecución: {now_str}")
    print("=" * 86)

    catalog_path = ROOT_DIR / "data" / "gold_miners_stocks.json"
    if not catalog_path.exists():
        print(f"[ERROR] No se encontró el catálogo en {catalog_path}")
        return 1

    catalog = load_catalog(catalog_path)
    store = MarketDataStore(base_dir=ROOT_DIR / "data" / "historical")
    tickers = ["GOLD", "NEM", "MGC", "BE"]

    # Paso 1: Descargar cotizaciones actualizadas
    if not args.no_download:
        try:
            download_market_data(store, tickers)
        except Exception as e:
            print(f"\n[ALERTA] Error durante la descarga: {e}")
            print("Intentando continuar con los datos históricos disponibles en Parquet...\n")
    else:
        print(">>> [PASO 1/3] Descarga omitida por parámetro --no-download.")

    # Paso 2: Análisis técnico y simulación de cartera
    print("\n>>> [PASO 2/3] Calculando niveles macro 60d con DuckDB y evaluando señales...")

    all_fills = []
    all_metrics = []
    all_daily_status = []
    all_control_zones = []
    portfolio_summary = {}

    total_market_value = 0.0
    total_unrealized_pnl = 0.0
    total_realized_pnl = 0.0
    total_commissions = 0.0
    pending_actions = []

    for ticker in tickers:
        shares = CONFIG_SHARES.get(ticker, 100)
        ops, metrics, p_summary, daily_status = run_stock_macro_backtest(
            symbol=ticker,
            shares=shares,
            lookback_bars=60,
            store=store,
            catalog=catalog,
        )

        all_fills.extend(ops)
        all_metrics.append(metrics)
        all_daily_status.append(daily_status)
        portfolio_summary.update(p_summary)

        cz_data = store.get_price_control_zones(ticker, min_gap_pct=0.03, current_price=daily_status["Último Precio ($)"])
        for z in cz_data.get("zones", []):
            all_control_zones.append({
                "Símbolo": ticker,
                "Activo": COMPANY_NAMES.get(ticker, ticker),
                "Fecha Gap": z["date"],
                "Tipo": z["type"],
                "Límite Inferior ($)": z["lower_bound"],
                "Límite Superior ($)": z["upper_bound"],
                "Magnitud (%)": z["magnitude_pct"],
                "Estado": z["status"],
                "Rol Actual": z["role"],
                "Distancia Cierre (%)": z["dist_pct"],
            })

        pos = daily_status["Posición Actual (Accs)"]
        mkt_val = daily_status["Valor Mercado ($ USD)"]
        unrealized = daily_status["P&L Latente ($ USD)"]
        comms = p_summary.get(f"Comisiones {ticker} ($ USD)", 0.0)
        realized = p_summary.get(f"P&L Realizado {ticker} ($ USD)", 0.0)

        total_market_value += mkt_val
        total_unrealized_pnl += unrealized
        total_realized_pnl += realized
        total_commissions += comms

        # Mostrar ficha del activo en consola
        name = COMPANY_NAMES.get(ticker, ticker)
        venue = VENUE_NAMES.get(ticker, "NYSE")
        unit = UNIT_NAMES.get(ticker, "acciones")
        print("-" * 86)
        print(f"  {name.upper()} ({venue}: {ticker})")
        print("-" * 86)
        var_val = daily_status["Var. Día ($)"]
        var_str = f"+${var_val:,.2f}" if var_val >= 0 else f"-${abs(var_val):,.2f}"
        print(
            f"  * Cotización Último Cierre : ${daily_status['Último Precio ($)']:.2f} USD "
            f"(Var: {var_str} / {daily_status['Var. Día (%)']:+.2f}%) "
            f"| Fecha: {daily_status['Fecha Cotización']}"
        )
        print(f"  * Soporte Macro (60d)      : ${daily_status['Soporte 60d ($)']:.2f} USD")
        print(f"  * Resistencia Macro (60d)  : ${daily_status['Resistencia 60d ($)']:.2f} USD")
        print(f"  * Media Móvil (SMA 60d)    : ${daily_status['SMA 60d ($)']:.2f} USD")
        print(f"  * SMA 200d Institucional   : ${daily_status['SMA 200d Institucional ($)']:.2f} USD [{daily_status['Filtro Tendencia Macro']}]")
        print(f"  * Rango 1 Año (252 ses.)   : Soporte ${daily_status['Soporte Anual 252d ($)']} | Resistencia ${daily_status['Resistencia Anual 252d ($)']}")
        print(f"  * Rango Histórico (5 Años) : Soporte ${daily_status['Soporte 5 Años ($)']} | Resistencia ${daily_status['Resistencia 5 Años ($)']}")
        zc_sup = daily_status.get("Zona Control Soporte", "-")
        zc_res = daily_status.get("Zona Control Resistencia", "-")
        dist_s = daily_status.get("Distancia Zona Soporte (%)")
        dist_r = daily_status.get("Distancia Zona Resistencia (%)")
        dist_s_str = f" [{dist_s:+.2f}%]" if isinstance(dist_s, (int, float)) else ""
        dist_r_str = f" [{dist_r:+.2f}%]" if isinstance(dist_r, (int, float)) else ""
        print(f"  * Zona Control (Gap Soporte): {zc_sup}{dist_s_str}")
        print(f"  * Zona Control (Gap Resist.): {zc_res}{dist_r_str}")

        print("\n  [EVALUACIÓN DE OPORTUNIDAD: ¿LARGOS O CORTOS?]")
        print(f"  • ESCENARIO LARGOS (COMPRA / LONG):")
        print(f"    - Precio Objetivo (TP)   : ${daily_status['Objetivo Largos ($)']:.2f} USD (+{daily_status['Potencial Largos (%)']:.2f}% recorrido)")
        print(f"    - Nivel Stop Loss        : ${daily_status['Stop Loss Largos ($)']:.2f} USD")
        print(f"    - Ratio Beneficio/Riesgo : {daily_status['Ratio B/R Largos']:.2f}:1")
        print(f"  • ESCENARIO CORTOS (VENTA / SHORT):")
        print(f"    - Precio Objetivo (TP)   : ${daily_status['Objetivo Cortos ($)']:.2f} USD (+{daily_status['Potencial Cortos (%)']:.2f}% caída potencial)")
        print(f"    - Nivel Stop Loss        : ${daily_status['Stop Loss Cortos ($)']:.2f} USD")
        print(f"    - Ratio Beneficio/Riesgo : {daily_status['Ratio B/R Cortos']:.2f}:1")
        print(f"  • VEREDICTO DE MERCADO     : >>> {daily_status['Dictamen / Oportunidad']} <<<")
        print(f"    -> {daily_status['Razonamiento Detallado']}")

        if pos != 0:
            entry_p = daily_status["Precio Entrada ($)"]
            entry_d = daily_status["Fecha Entrada"]
            pnl_str = f"+${unrealized:,.2f}" if unrealized >= 0 else f"-${abs(unrealized):,.2f}"
            side_str = "compradas" if pos > 0 else "en corto"
            entry_str = f" a ${entry_p:.2f}" if isinstance(entry_p, (int, float)) else ""
            date_str = f" el {entry_d}" if entry_d and entry_d != "-" else ""
            print(f"\n  * Posición en Cartera      : {pos} {unit} ({side_str}{entry_str}{date_str})")
            print(f"  * Valor Actual Posición    : ${mkt_val:,.2f} USD")
            print(
                f"  * Beneficio Latente (P&L)  : {pnl_str} USD "
                f"({daily_status['Rentabilidad (%)']:+.2f}%)"
            )
        else:
            print(f"\n  * Posición en Cartera      : 0 {unit} (Fuera de mercado)")

        action_str = daily_status["¿Requiere Acción Hoy?"]
        print(f"  * ¿REQUIERE ACCIÓN HOY?    : {action_str}")
        print(f"    -> {daily_status['Instrucción Operativa']}")

        if any(action_str.upper().startswith(x) for x in ["SÍ", "SI", "COMPRAR", "VENDER"]):
            pending_actions.append(f"{ticker}: {daily_status['Instrucción Operativa']}")

    # Resumen global consolidado
    net_pnl = total_unrealized_pnl + total_realized_pnl - total_commissions
    portfolio_summary["VALOR TOTAL INVERTIDO ($ USD)"] = round(total_market_value, 2)
    portfolio_summary["TOTAL BENEFICIO LATENTE ($ USD)"] = round(total_unrealized_pnl, 2)
    portfolio_summary["TOTAL BENEFICIO REALIZADO ($ USD)"] = round(total_realized_pnl, 2)
    portfolio_summary["TOTAL COMISIONES PAGADAS ($ USD)"] = round(total_commissions, 2)
    portfolio_summary["P&L NETO TOTAL ($ USD)"] = round(net_pnl, 2)
    portfolio_summary["ÚLTIMA ACTUALIZACIÓN"] = now_str

    print("=" * 86)
    print("   RESUMEN CONSOLIDADO DE CARTERA (CUENTA AUDITADA)")
    print("=" * 86)
    print(f"  * Valor Total de Mercado    : ${total_market_value:,.2f} USD")
    pnl_tot_str = f"+${total_unrealized_pnl:,.2f}" if total_unrealized_pnl >= 0 else f"-${abs(total_unrealized_pnl):,.2f}"
    print(f"  * Beneficio Latente Total   : {pnl_tot_str} USD")
    realized_str = f"+${total_realized_pnl:,.2f}" if total_realized_pnl > 0 else (f"-${abs(total_realized_pnl):,.2f}" if total_realized_pnl < 0 else "$0.00")
    print(f"  * Beneficio Realizado Total : {realized_str} USD")
    print(f"  * Comisiones Totales IBKR   : ${total_commissions:,.2f} USD")
    net_pnl_str = f"+${net_pnl:,.2f}" if net_pnl >= 0 else f"-${abs(net_pnl):,.2f}"
    print(f"  * P&L Neto Acumulado        : {net_pnl_str} USD")
    print("-" * 86)

    if pending_actions:
        print("  * ESTADO DE ÓRDENES HOY     : ¡ATENCIÓN! ACCIÓN REQUERIDA HOY:")
        for action in pending_actions:
            print(f"    - {action}")
    else:
        print("  * ESTADO DE ÓRDENES HOY     : SIN ÓRDENES PENDIENTES HOY")
        print("    (Las posiciones se mantienen dentro de los parámetros de riesgo o en espera fuera de mercado)")
    print("=" * 86)

    # Paso 3: Exportar al Diario de Trading en Excel
    print("\n>>> [PASO 3/3] Guardando y dando formato al libro Excel multi-pestaña...")
    excel_path = ROOT_DIR / "data" / "diario_trading_oro.xlsx"
    journal = ExcelTradeJournal(excel_path=excel_path)
    saved_path = journal.export_operations(
        fills=all_fills,
        market_analysis=all_metrics,
        portfolio_summary=portfolio_summary,
        daily_status=all_daily_status,
        control_zones=all_control_zones,
    )

    print(f"  [ÉXITO] Archivo Excel actualizado correctamente.")
    print(f"  Ruta: {saved_path}")
    print("=" * 86)
    return 0


if __name__ == "__main__":
    sys.exit(main())
