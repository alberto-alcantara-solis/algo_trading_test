"""
backtest_broker.py — Broker simulado para backtesting.
───────────────────────────────────────────────────────
"""


import logging
import uuid
from dataclasses import dataclass, field
from datetime import datetime, date, timedelta
from typing import List, Optional, Tuple
from zoneinfo import ZoneInfo

from alpaca.data.historical import StockHistoricalDataClient
from alpaca.data.requests import StockBarsRequest
from alpaca.data.timeframe import TimeFrame, TimeFrameUnit
from alpaca.data.enums import DataFeed
from alpaca.trading.client import TradingClient
from alpaca.trading.requests import GetCalendarRequest

import config
from state import Candle


log = logging.getLogger(__name__)
UTC = ZoneInfo("UTC")
NY  = ZoneInfo("America/New_York")


# ─────────────────────────────────────────────────────────────────────────────
# Modelos internos
# ─────────────────────────────────────────────────────────────────────────────
@dataclass
class _SimOrder:
    """Orden bracket simulada."""
    order_id:  str
    direction: str
    entry:     float
    sl:        float
    tp:        float
    qty:       int
    filled:    bool  = False
    closed:    bool  = False
    fill_price: Optional[float] = None
    exit_price: Optional[float] = None
    exit_reason: Optional[str]  = None

@dataclass
class Trade:
    """Resultado de una operación completada."""
    date:        str
    direction:   str
    entry:       float
    exit:        float
    sl:          float
    tp:          float
    qty:         int
    pnl:         float
    exit_reason: str

@dataclass
class DayResult:
    """Resumen de una sesión."""
    date:       str
    trades:     List[Trade] = field(default_factory=list)

    @property
    def pnl(self) -> float:
        return sum(t.pnl for t in self.trades)

    @property
    def n_trades(self) -> int:
        return len(self.trades)

    @property
    def n_wins(self) -> int:
        return sum(1 for t in self.trades if t.pnl > 0)


# ─────────────────────────────────────────────────────────────────────────────
# BacktestBroker
# ─────────────────────────────────────────────────────────────────────────────
class BacktestBroker:
    """
    Broker simulado. Misma interfaz que Broker; Strategy lo usa sin cambios.
    """
    def __init__(self, initial_capital: float = 100_000.0):
        self.tc = TradingClient(
            api_key    = config.ALPACA_API_KEY,
            secret_key = config.ALPACA_SECRET_KEY,
            paper      = True,
        )
        self.dc = StockHistoricalDataClient(
            api_key    = config.ALPACA_API_KEY,
            secret_key = config.ALPACA_SECRET_KEY,
        )

        self.capital    = initial_capital
        self._cash      = initial_capital

        self._all_bars:     List[Candle] = []
        self._current_bar_idx: int = -1
        self._order:        Optional[_SimOrder] = None
        self._has_position: bool = False

        self.day_results:  List[DayResult] = []
        self._current_day: Optional[DayResult] = None

    def start_day(self, date_str: str):
        """Llama el motor antes de procesar cada sesión."""
        self._current_day = DayResult(date=date_str)
        self._all_bars    = []
        self._current_bar_idx = -1
        self._order       = None
        self._has_position = False
        ###log.info(f"[BT] Sesión iniciada: {date_str} | Capital: {self._cash:.2f}")

    def end_day(self):
        """Llama el motor al terminar cada sesión. Fuerza el cierre si hay posición."""
        if self._has_position and self._order:
            self._force_close("MARKET")
        if self._current_day:
            self.day_results.append(self._current_day)
            pnl = self._current_day.pnl
            sign = "+" if pnl >= 0 else ""
            """###log.info(
                f"[BT] Sesión terminada: {self._current_day.date} | "
                f"Trades: {self._current_day.n_trades} | "
                f"P&L: {sign}{pnl:.2f} | "
                f"Capital: {self._cash:.2f}"
            )"""

    def feed_bars(self, bars: List[Candle]):
        """Actualiza el buffer de velas de la sesión (llamado por get_closed_bars)."""
        self._all_bars = bars
        if bars:
            self._current_bar_idx = len(bars) - 1
            self._check_fills()

    def get_market_hours(self, d: date) -> Tuple[datetime, datetime]:
        """Delega en el calendario real de Alpaca."""
        cal = self.tc.get_calendar(GetCalendarRequest(start=d, end=d))
        if not cal:
            raise ValueError(f"Mercado cerrado el {d}")
        day       = cal[0]
        open_utc  = datetime.combine(d, day.open.time(),  tzinfo=NY).astimezone(UTC)
        close_utc = datetime.combine(d, day.close.time(), tzinfo=NY).astimezone(UTC)
        return open_utc, close_utc

    def get_closed_bars(
        self,
        symbol: str,
        start: datetime,
        end: datetime,
    ) -> List[Candle]:
        """
        Devuelve las velas históricas desde Alpaca y actualiza la simulación.
        """
        now_minute = end.replace(second=0, microsecond=0)
        eff_end    = now_minute - timedelta(minutes=1)

        if eff_end <= start:
            return []

        req = StockBarsRequest(
            symbol_or_symbols = symbol,
            timeframe         = TimeFrame(1, TimeFrameUnit.Minute),
            start             = start,
            end               = eff_end,
            feed              = DataFeed.IEX,
        )
        bars_resp = self.dc.get_stock_bars(req)

        if symbol not in bars_resp.data:
            return []

        bars = [
            Candle(
                timestamp = b.timestamp.astimezone(UTC).isoformat(),
                open      = float(b.open),
                high      = float(b.high),
                low       = float(b.low),
                close     = float(b.close),
            )
            for b in bars_resp.data[symbol]
        ]

        self.feed_bars(bars)
        return bars

    def available_cash(self) -> float:
        return self._cash

    def has_open_position(self, symbol: str) -> bool:
        return self._has_position

    def place_long_bracket(
        self,
        symbol: str,
        entry: float,
        sl: float,
        tp: float,
    ) -> str:
        qty      = self._calculate_qty(entry)
        order_id = str(uuid.uuid4())
        self._order = _SimOrder(
            order_id  = order_id,
            direction = "LONG",
            entry     = entry,
            sl        = sl,
            tp        = tp,
            qty       = qty,
        )
        """###log.info(
            f"[BT LONG BRACKET] id={order_id[:8]} qty={qty} "
            f"entry={entry:.2f}  sl={sl:.2f}  tp={tp:.2f}"
        )"""
        self._check_fills()
        return order_id

    def place_short_bracket(
        self,
        symbol: str,
        entry: float,
        sl: float,
        tp: float,
    ) -> str:
        qty      = self._calculate_qty(entry)
        order_id = str(uuid.uuid4())
        self._order = _SimOrder(
            order_id  = order_id,
            direction = "SHORT",
            entry     = entry,
            sl        = sl,
            tp        = tp,
            qty       = qty,
        )
        """###log.info(
            f"[BT SHORT BRACKET] id={order_id[:8]} qty={qty} "
            f"entry={entry:.2f}  sl={sl:.2f}  tp={tp:.2f}"
        )"""
        self._check_fills()
        return order_id

    def cancel_order(self, order_id: str):
        if self._order and self._order.order_id == order_id:
            if not self._order.filled:
                ###log.info(f"[BT] Orden {order_id[:8]} cancelada (sin ejecutar).")
                self._order = None
            else:
                self._force_close("CANCELLED")

    def close_position_at_market(self, symbol: str):
        if self._has_position:
            self._force_close("MARKET")

    def is_order_filled(self, order_id: str) -> bool:
        if self._order and self._order.order_id == order_id:
            return self._order.filled
        return False


    # ─────────────────────────────────────────────────────────────────────────
    # Motor de simulación interno
    # ─────────────────────────────────────────────────────────────────────────
    def _calculate_qty(self, entry_price: float) -> int:
        qty = int(self._cash * config.CAPITAL_PCT / entry_price)
        return max(qty, 1)

    def _current_bar(self) -> Optional[Candle]:
        if 0 <= self._current_bar_idx < len(self._all_bars):
            return self._all_bars[self._current_bar_idx]
        return None

    def _check_fills(self):
        """
        Comprueba si la orden pendiente se ha llenado o si el SL/TP se ha tocado.
        """
        if not self._order or not self._all_bars:
            return

        o   = self._order
        bar = self._current_bar()
        if bar is None:
            return

        if not o.filled:
            if o.direction == "LONG":
                filled = bar.low <= o.entry <= bar.high
            else:
                filled = bar.low <= o.entry <= bar.high

            if filled:
                o.filled     = True
                o.fill_price = o.entry
                self._has_position = True
                cost = o.fill_price * o.qty
                self._cash -= cost
                """###log.info(
                    f"[BT] ✅ Fill [{o.direction}] {bar.timestamp[11:16]} "
                    f"@ {o.fill_price:.2f}  qty={o.qty}  coste={cost:.2f}"
                )"""
            return

        if not o.filled or o.closed:
            return

        if o.direction == "LONG":
            if bar.low <= o.sl:
                self._close_position(o, exit_price=o.sl, reason="SL", bar=bar)
            elif bar.high >= o.tp:
                self._close_position(o, exit_price=o.tp, reason="TP", bar=bar)
        else:
            if bar.high >= o.sl:
                self._close_position(o, exit_price=o.sl, reason="SL", bar=bar)
            elif bar.low <= o.tp:
                self._close_position(o, exit_price=o.tp, reason="TP", bar=bar)

    def _close_position(
        self,
        o: _SimOrder,
        exit_price: float,
        reason: str,
        bar: Candle,
    ):
        o.closed      = True
        o.exit_price  = exit_price
        o.exit_reason = reason
        self._has_position = False

        if o.direction == "LONG":
            pnl = (exit_price - o.fill_price) * o.qty
        else:
            pnl = (o.fill_price - exit_price) * o.qty

        self._cash += o.fill_price * o.qty + pnl

        sign = "+" if pnl >= 0 else ""
        """###log.info(
            f"[BT] {'✅' if pnl > 0 else '❌'} Cierre [{o.direction}] "
            f"{bar.timestamp[11:16]} @ {exit_price:.2f} ({reason}) "
            f"P&L: {sign}{pnl:.2f} | Capital: {self._cash:.2f}"
        )"""

        if self._current_day:
            self._current_day.trades.append(
                Trade(
                    date        = self._current_day.date,
                    direction   = o.direction,
                    entry       = o.fill_price,
                    exit        = exit_price,
                    sl          = o.sl,
                    tp          = o.tp,
                    qty         = o.qty,
                    pnl         = round(pnl, 2),
                    exit_reason = reason,
                )
            )

    def _force_close(self, reason: str):
        """Cierre forzado al precio de cierre de la última barra disponible."""
        if not self._order or not self._order.filled:
            self._has_position = False
            self._order = None
            return

        bar = self._current_bar()
        exit_price = bar.close if bar else self._order.entry
        self._close_position(self._order, exit_price=exit_price, reason=reason, bar=bar)


    # ─────────────────────────────────────────────────────────────────────────
    # Resumen de resultados
    # ─────────────────────────────────────────────────────────────────────────
    def print_summary(self):
        """Imprime un resumen completo del backtest."""
        print("\n" + "═" * 65)
        print("   RESUMEN DEL BACKTEST")
        print("═" * 65)

        total_pnl    = 0.0
        total_trades = 0
        total_wins   = 0

        for dr in self.day_results:
            if dr.n_trades == 0:
                continue
            sign = "+" if dr.pnl >= 0 else ""
            print(
                f"  {dr.date}  │  "
                f"Trades: {dr.n_trades}  │  "
                f"Wins: {dr.n_wins}/{dr.n_trades}  │  "
                f"P&L: {sign}{dr.pnl:.2f}"
            )
            for t in dr.trades:
                sign_t = "+" if t.pnl >= 0 else ""
                icon   = "✅" if t.pnl > 0 else "❌"
                print(
                    f"      {icon} {t.direction:5s}  "
                    f"entry={t.entry:.2f}  exit={t.exit:.2f}  "
                    f"qty={t.qty}  "
                    f"P&L: {sign_t}{t.pnl:.2f}  [{t.exit_reason}]"
                )
            total_pnl    += dr.pnl
            total_trades += dr.n_trades
            total_wins   += dr.n_wins

        print("─" * 65)
        wr = (total_wins / total_trades * 100) if total_trades else 0
        all_pnls = [
            t.pnl
            for dr in self.day_results
            for t in dr.trades
        ]
        wins = [p for p in all_pnls if p > 0]
        losses = [p for p in all_pnls if p < 0]
        avg_win = sum(wins) / len(wins) if wins else 0
        avg_loss = abs(sum(losses) / len(losses)) if losses else 0
        p_win = len(wins) / len(all_pnls) if all_pnls else 0
        p_loss = len(losses) / len(all_pnls) if all_pnls else 0
        expectancy = (p_win * avg_win) - (p_loss * avg_loss)
        sign = "+" if total_pnl >= 0 else ""
        print(f"  Capital inicial:  {self.capital:.2f}")
        print(f"  Capital final:    {self._cash:.2f}")
        print(f"  P&L total:        {sign}{total_pnl:.2f}")
        print(f"  Total trades:     {total_trades}")
        print(f"  Win rate:         {wr:.1f}%")
        print(f"  Avg win:          {avg_win:.2f}")
        print(f"  Avg loss:         {avg_loss:.2f}")
        print(f"  Expectancy:       {expectancy:+.2f} per trade")

        print("═" * 65 + "\n")