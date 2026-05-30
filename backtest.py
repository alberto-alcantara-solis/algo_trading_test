"""
backtest.py — Motor de backtesting (optimizado).
─────────────────────────────────────────────────
Ejecuta la estrategia completa sobre datos históricos reales de Alpaca sin tocar ninguna orden real.

Optimización: todos los datos del día se obtienen en UNA sola llamada a la API antes de iniciar la simulación..

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
from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo

from alpaca.data.historical import StockHistoricalDataClient
from alpaca.data.requests import StockBarsRequest
from alpaca.data.timeframe import TimeFrame, TimeFrameUnit
from alpaca.data.enums import DataFeed
from alpaca.trading.client import TradingClient
from alpaca.trading.requests import GetCalendarRequest

import config
from state import BotState, S, Candle
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


# ─────────────────────────────────────────────────────────────────────────────
# Helpers
# ─────────────────────────────────────────────────────────────────────────────
def _fetch_calendar(tc: TradingClient, start: date, end: date) -> dict:
    """
    Obtiene el calendario de mercado en una sola llamada para todo el rango.
    """
    cal = tc.get_calendar(GetCalendarRequest(start=start, end=end))
    result = {}
    for day in cal:
        d = day.open.date()
        open_utc  = datetime.combine(d, day.open.time(),  tzinfo=NY).astimezone(UTC)
        close_utc = datetime.combine(d, day.close.time(), tzinfo=NY).astimezone(UTC)
        result[d] = (open_utc, close_utc)
    return result

def _fetch_day_bars(
    dc: StockHistoricalDataClient,
    symbol: str,
    open_utc: datetime,
    close_utc: datetime,
) -> list:
    """
    Obtiene TODAS las velas de 1 minuto del día en UNA sola llamada a la API.
    """
    req = StockBarsRequest(
        symbol_or_symbols = symbol,
        timeframe         = TimeFrame(1, TimeFrameUnit.Minute),
        start             = open_utc,
        end               = close_utc,
        feed              = DataFeed.IEX,
    )
    resp = dc.get_stock_bars(req)
    if symbol not in resp.data:
        return []
    return [
        Candle(
            timestamp = b.timestamp.astimezone(UTC).isoformat(),
            open      = float(b.open),
            high      = float(b.high),
            low       = float(b.low),
            close     = float(b.close),
        )
        for b in resp.data[symbol]
    ]

def _date_range(start: date, end: date):
    """Genera todas las fechas entre start y end (inclusive)."""
    d = start
    while d <= end:
        yield d
        d += timedelta(days=1)


# ─────────────────────────────────────────────────────────────────────────────
# Motor principal
# ─────────────────────────────────────────────────────────────────────────────
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
    log.info("   BACKTEST INICIADO (modo optimizado)")
    log.info(f"   Símbolo:    {config.SYMBOL}")
    log.info(f"   Dirección:  {config.TRADE_DIRECTION}")
    log.info(f"   Capital:    {capital:.2f}")
    log.info(f"   EMA:        {config.EMA_LENGTH} períodos")
    log.info(f"   R/R:        1:{config.RISK_REWARD}")
    log.info(f"   Rango:      {start_date} → {end_date}")
    log.info("=" * 65)
    log.info("Obteniendo calendario de mercado...")

    calendar = _fetch_calendar(broker.tc, start_date, end_date)
    log.info(f"  {len(calendar)} días hábiles encontrados.")

    dc = broker.dc

    for d in _date_range(start_date, end_date):
        if d.weekday() >= 5:
            continue
        if d not in calendar:
            log.info(f"{d}: Mercado cerrado (día no hábil).")
            continue

        open_utc, close_utc = calendar[d]
        date_str = d.strftime("%Y-%m-%d")

        log.info(
            f"\n{'─'*55}\n"
            f"  Procesando: {date_str} | "
            f"Apertura: {open_utc.strftime('%H:%M UTC')} | "
            f"Cierre: {close_utc.strftime('%H:%M UTC')}"
        )

        all_day_bars = _fetch_day_bars(dc, config.SYMBOL, open_utc, close_utc)
        if not all_day_bars:
            log.info(f"  Sin datos para {date_str}.")
            continue

        state = BotState()
        state.reset_for_day(date_str, open_utc.isoformat(), close_utc.isoformat())
        state.status = S.CALC_RANGE

        broker.start_day(date_str)
        broker.feed_bars(all_day_bars)
        strat = Strategy(broker, state)

        current_time = open_utc + timedelta(minutes=1)

        while current_time <= close_utc + timedelta(minutes=1):
            strat._apply_time_cutoffs(current_time)
            if state.status == S.DAY_ENDED:
                break

            cutoff_ts = (current_time - timedelta(minutes=1)).replace(
                second=0, microsecond=0
            ).isoformat()
            visible_bars = [b for b in all_day_bars if b.timestamp <= cutoff_ts]

            if visible_bars:
                broker._all_bars         = visible_bars
                broker._current_bar_idx  = len(visible_bars) - 1
                broker._check_fills()
                strat._process_bars(visible_bars)

            current_time += timedelta(minutes=1)

        broker.end_day()

    broker.print_summary()
    return broker


# ─────────────────────────────────────────────────────────────────────────────
# CLI
# ─────────────────────────────────────────────────────────────────────────────
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