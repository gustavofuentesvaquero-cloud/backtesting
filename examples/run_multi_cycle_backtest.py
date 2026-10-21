"""Script CLI para ejecutar el Motor de Backtesting Continuo Multi-Ciclo (5 Años).

Permite evaluar cuantitativamente el desempeño de estrategias macro de Price Action
sobre activos clave (GOLD, NEM, MGC, BE, IBE) con cero sesgo de anticipación.

Uso:
    python examples/run_multi_cycle_backtest.py --symbol GOLD
    python examples/run_multi_cycle_backtest.py --symbol NEM --direction long
    python examples/run_multi_cycle_backtest.py --symbol MGC --shares 2
    python examples/run_multi_cycle_backtest.py --symbol BE --direction both
"""

from __future__ import annotations

import argparse
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from market_catalog import RollingBacktestEngine


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Motor Continuo Multi-Ciclo (Rolling Backtest Institucional de 5 Años)",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
    )
    parser.add_argument(
        "--symbol",
        type=str,
        default="GOLD",
        help="Símbolo a simular (GOLD, NEM, MGC, BE, IBE)",
    )
    parser.add_argument(
        "--capital",
        type=float,
        default=100_000.0,
        help="Capital inicial de la cuenta en USD",
    )
    parser.add_argument(
        "--lookback",
        type=int,
        default=60,
        help="Ventana móvil de barras para canales Donchian y medias móviles",
    )
    parser.add_argument(
        "--direction",
        type=str,
        choices=["long", "short", "both"],
        default="both",
        help="Dirección de la operativa ('long', 'short', 'both')",
    )
    parser.add_argument(
        "--shares",
        type=int,
        default=None,
        help="Cantidad fija de acciones/contratos por trade (None = dimensionamiento por riesgo)",
    )
    parser.add_argument(
        "--risk-pct",
        type=float,
        default=0.015,
        help="Porcentaje de riesgo de capital por operación (default: 0.015 = 1.5%%)",
    )
    parser.add_argument(
        "--use-atr",
        action="store_true",
        help="Usar dimensionamiento dinámico basado en ATR(14)",
    )
    parser.add_argument(
        "--start-date",
        type=str,
        default=None,
        help="Fecha inicial de evaluación (formato YYYY-MM-DD)",
    )
    parser.add_argument(
        "--end-date",
        type=str,
        default=None,
        help="Fecha final de evaluación (formato YYYY-MM-DD)",
    )
    parser.add_argument(
        "--slippage",
        type=float,
        default=5.0,
        help="Deslizamiento estimado en puntos básicos (bps, ej. 5.0 = 0.05%%)",
    )
    parser.add_argument(
        "--use-control-zones",
        action="store_true",
        help="Integrar zonas de control por gaps históricos (>=3%%)",
    )
    return parser.parse_args()


def main() -> None:
    if hasattr(sys.stdout, "reconfigure"):
        try:
            sys.stdout.reconfigure(encoding="utf-8", errors="replace")
        except Exception:
            pass

    args = parse_args()

    print("\n" + "=" * 70)
    print(" INICIANDO MOTOR DE BACKTESTING CONTINUO MULTI-CICLO (5 AÑOS)")
    print("=" * 70)
    print(f"  Activo:             {args.symbol.upper()}")
    print(f"  Dirección:          {args.direction.upper()}")
    print(f"  Capital Inicial:    ${args.capital:,.2f}")
    print(f"  Ventana Lookback:   {args.lookback} sesiones")
    print(f"  Sizing:             {'Fijo ' + str(args.shares) + ' u.' if args.shares else ('ATR(14) Dinámico' if args.use_atr else f'Riesgo Fijo {args.risk_pct*100:.1f}%')}")
    print(f"  Slippage:           {args.slippage:.1f} bps (0.{int(args.slippage*10):03d}%)")
    print(f"  Zonas de Control:   {'Habilitadas' if args.use_control_zones else 'Deshabilitadas'}")
    print("=" * 70 + "\n")

    engine = RollingBacktestEngine()

    try:
        result = engine.run(
            symbol=args.symbol,
            initial_capital=args.capital,
            lookback_bars=args.lookback,
            direction=args.direction,
            shares=args.shares,
            risk_pct=args.risk_pct,
            use_atr_sizing=args.use_atr,
            start_date=args.start_date,
            end_date=args.end_date,
            slippage_bps=args.slippage,
            use_control_zones=args.use_control_zones,
        )
    except Exception as exc:
        print(f"Error durante la simulación: {exc}", file=sys.stderr)
        sys.exit(1)

    print(result.summary())
    print()

    # Tabla de últimas operaciones ejecutadas
    trade_log = result.trade_log
    if not trade_log.empty:
        n_show = min(10, len(trade_log))
        recent_trades = trade_log.tail(n_show)
        print(f"ÚLTIMAS {n_show} OPERACIONES DEL REGISTRO DE TRADING ({len(trade_log)} totales):")
        print("-" * 110)
        header = (
            f"{'ID':<14} {'DIR':<6} {'ENTRADA':<11} {'PRECIO E.':<10} {'SALIDA':<11} "
            f"{'PRECIO S.':<10} {'MOTIVO':<12} {'UDS':<6} {'PNL NETO':<12} {'RET %':<8} {'DÍAS':<5}"
        )
        print(header)
        print("-" * 110)

        for _, row in recent_trades.iterrows():
            tid = str(row["trade_id"])
            direction = str(row["direction"]).upper()
            e_date = str(row["entry_date"])
            e_price = f"${float(row['entry_price']):.2f}"
            x_date = str(row["exit_date"])
            x_price = f"${float(row['exit_price']):.2f}"
            reason = str(row["exit_reason"])
            shares = int(row["shares"])
            pnl = f"${float(row['pnl_net']):+,.2f}"
            ret = f"{float(row['return_pct']):+.2f}%"
            h_days = f"{int(row['holding_days'])}d"

            print(
                f"{tid:<14} {direction:<6} {e_date:<11} {e_price:<10} {x_date:<11} "
                f"{x_price:<10} {reason:<12} {shares:<6} {pnl:<12} {ret:<8} {h_days:<5}"
            )
        print("-" * 110)
    else:
        print("No se generaron operaciones en el periodo especificado.")

    print("\nSimulación completada con éxito.")


if __name__ == "__main__":
    main()
