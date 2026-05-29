"""
backtest.py — Motor de backtesting.
─────────────────────────────────────
Ejecuta la estrategia completa sobre datos históricos reales de Alpaca sin tocar ninguna orden real.

Uso:
    python backtest.py --start 2024-01-02 --end 2024-01-31
    python backtest.py --start 2024-03-15 --end 2024-03-15
    python backtest.py --start 2023-01-01 --end 2023-12-31

Opciones:
    --start     Fecha de inicio (YYYY-MM-DD). Obligatorio.
    --end       Fecha de fin (YYYY-MM-DD). Obligatorio.
    --capital   Capital inicial simulado (por defecto: 100000.0).
    --symbol    Símbolo a usar (por defecto: el de config.py).
    --direction LONG | SHORT | BOTH (por defecto: el de config.py).
"""


import argparse
import logging
import sys
from datetime import date, timedelta
from zoneinfo import ZoneInfo

import config
from state import BotState, S
from strategy import Strategy
from backtest_broker import BacktestBroker


logging.basicConfig(
    level   = logging.INFO,
    format  = "%(asctime)s  %(levelname)-8s  %(message)s",
    datefmt = "%Y-%m-%d %H:%M:%S",
    handlers = [logging.StreamHandler(sys.stdout)],
)
log = logging.getLogger(__name__)

UTC = ZoneInfo("UTC")
NY  = ZoneInfo("America/New_York")


def _date_range(start: date, end: date):
    """Genera todas las fechas entre start y end (inclusive)."""
    d = start
    while d <= end:
        yield d
        d += timedelta(days=1)

def run_backtest(
    start_date: date,
    end_date:   date,
    capital:    float = 100_000.0,
    symbol:     str   = None,
    direction:  str   = None,
):
    if symbol:
        config.SYMBOL = symbol
    if direction:
        config.TRADE_DIRECTION = direction

    broker = BacktestBroker(initial_capital=capital)

    log.info("=" * 65)
    log.info("   BACKTEST INICIADO")
    log.info(f"   Símbolo:    {config.SYMBOL}")
    log.info(f"   Dirección:  {config.TRADE_DIRECTION}")
    log.info(f"   Capital:    {capital:.2f}")
    log.info(f"   EMA:        {config.EMA_LENGTH} períodos")
    log.info(f"   R/R:        1:{config.RISK_REWARD}")
    log.info(f"   Rango:      {start_date} → {end_date}")
    log.info("=" * 65)

    for d in _date_range(start_date, end_date):
        if d.weekday() >= 5:
            continue

        date_str = d.strftime("%Y-%m-%d")

        try:
            open_utc, close_utc = broker.get_market_hours(d)
        except ValueError:
            log.info(f"{date_str}: Mercado cerrado (día no hábil).")
            continue

        log.info(
            f"\n{'─'*55}\n"
            f"  Procesando: {date_str} | "
            f"Apertura: {open_utc.strftime('%H:%M UTC')} | "
            f"Cierre: {close_utc.strftime('%H:%M UTC')}"
        )

        state = BotState()
        state.reset_for_day(date_str, open_utc.isoformat(), close_utc.isoformat())
        state.status = S.CALC_RANGE

        broker.start_day(date_str)
        strat = Strategy(broker, state)

        current_time = open_utc + timedelta(minutes=1)

        while current_time <= close_utc + timedelta(minutes=1):
            strat._apply_time_cutoffs(current_time)
            if state.status == S.DAY_ENDED:
                break

            bars = broker.get_closed_bars(config.SYMBOL, open_utc, current_time)

            if bars:
                strat._process_bars(bars)

            current_time += timedelta(minutes=1)

        broker.end_day()

    broker.print_summary()
    return broker


def main():
    parser = argparse.ArgumentParser(
        description="Backtesting de la estrategia de trading."
    )
    parser.add_argument(
        "--start", required=True,
        help="Fecha de inicio en formato YYYY-MM-DD"
    )
    parser.add_argument(
        "--end", required=True,
        help="Fecha de fin en formato YYYY-MM-DD"
    )
    parser.add_argument(
        "--capital", type=float, default=100_000.0,
        help="Capital inicial simulado (default: 100000)"
    )
    parser.add_argument(
        "--symbol", type=str, default=None,
        help=f"Símbolo a operar (default: {config.SYMBOL})"
    )
    parser.add_argument(
        "--direction", type=str, default=None,
        choices=["LONG", "SHORT", "BOTH"],
        help=f"Dirección (default: {config.TRADE_DIRECTION})"
    )
    args = parser.parse_args()

    try:
        start = date.fromisoformat(args.start)
        end   = date.fromisoformat(args.end)
    except ValueError as e:
        print(f"Error: fecha inválida — {e}")
        sys.exit(1)

    if start > end:
        print("Error: --start debe ser anterior o igual a --end")
        sys.exit(1)

    if not config.ALPACA_API_KEY or not config.ALPACA_SECRET_KEY:
        print(
            "Error: Faltan las claves de Alpaca. "
            "Configura ALPACA_API_KEY y ALPACA_SECRET_KEY en el fichero .env"
        )
        sys.exit(1)

    run_backtest(
        start_date = start,
        end_date   = end,
        capital    = args.capital,
        symbol     = args.symbol,
        direction  = args.direction,
    )


if __name__ == "__main__":
    main()