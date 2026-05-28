"""
Wrapper de la API de Alpaca.
"""


import logging
from datetime import datetime, date, timedelta
from zoneinfo import ZoneInfo
from typing import List, Tuple

from alpaca.trading.client import TradingClient
from alpaca.data.historical import StockHistoricalDataClient
from alpaca.data.requests import StockBarsRequest
from alpaca.data.timeframe import TimeFrame, TimeFrameUnit
from alpaca.data.enums import DataFeed
from alpaca.trading.requests import (
    GetCalendarRequest,
    LimitOrderRequest,
    GetOrdersRequest,
)
from alpaca.trading.enums import (
    OrderSide,
    TimeInForce,
    OrderClass,
    QueryOrderStatus,
)

import config
from state import Candle


log = logging.getLogger(__name__)

UTC = ZoneInfo("UTC")
NY  = ZoneInfo("America/New_York")


class Broker:
    """Interfaz unificada con Alpaca para datos y ejecución."""
    def __init__(self):
        self.tc = TradingClient(
            api_key    = config.ALPACA_API_KEY,
            secret_key = config.ALPACA_SECRET_KEY,
            paper      = config.PAPER,
        )
        self.dc = StockHistoricalDataClient(
            api_key    = config.ALPACA_API_KEY,
            secret_key = config.ALPACA_SECRET_KEY,
        )

    def get_market_hours(self, d: date) -> Tuple[datetime, datetime]:
        """
        Devuelve el horario de apertura y cierre en UTC para el día dado.
        """
        cal = self.tc.get_calendar(GetCalendarRequest(start=d, end=d))
        if not cal:
            raise ValueError(f"Mercado cerrado el {d}")

        day      = cal[0]
        open_utc  = datetime.combine(d, day.open,  tzinfo=NY).astimezone(UTC)
        close_utc = datetime.combine(d, day.close, tzinfo=NY).astimezone(UTC)
        return open_utc, close_utc

    def get_closed_bars(
        self,
        symbol: str,
        start: datetime,
        end: datetime,
    ) -> List[Candle]:
        """
        Devuelve todas las velas de 1 minuto CERRADAS entre `start` y `end`. La vela del minuto en curso (no cerrada) queda excluida automáticamente.
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
        bars = self.dc.get_stock_bars(req)

        if symbol not in bars.data:
            return []

        return [
            Candle(
                timestamp = b.timestamp.astimezone(UTC).isoformat(),
                open      = float(b.open),
                high      = float(b.high),
                low       = float(b.low),
                close     = float(b.close),
            )
            for b in bars.data[symbol]
        ]

    def available_cash(self) -> float:
        """Cash disponible en la cuenta (no incluye margen)."""
        return float(self.tc.get_account().cash)

    def has_open_position(self, symbol: str) -> bool:
        """True si hay una posición abierta para el símbolo dado."""
        try:
            pos = self.tc.get_open_position(symbol)
            return float(pos.qty) != 0
        except Exception:
            return False

    def _calculate_qty(self, entry_price: float) -> int:
        """
        Calcula la cantidad de acciones a comprar/vender según el porcentaje de capital configurado en config.CAPITAL_PCT. (minimo 1 acción)
        """
        cash  = self.available_cash()
        qty   = int(cash * config.CAPITAL_PCT / entry_price)
        return max(qty, 1)

    def place_long_bracket(
        self,
        symbol: str,
        entry: float,
        sl: float,
        tp: float,
    ) -> str:
        """
        Orden límite de compra (Long) con bracket (SL + TP).
        """
        qty = self._calculate_qty(entry)
        req = LimitOrderRequest(
            symbol         = symbol,
            qty            = qty,
            side           = OrderSide.BUY,
            time_in_force  = TimeInForce.DAY,
            limit_price    = round(entry, 2),
            order_class    = OrderClass.BRACKET,
            stop_loss      = {"stop_price": round(sl, 2)},
            take_profit    = {"limit_price": round(tp, 2)},
        )
        order = self.tc.submit_order(req)
        log.info(
            f"[LONG BRACKET] id={order.id} qty={qty} "
            f"entry={entry:.2f}  sl={sl:.2f}  tp={tp:.2f}"
        )
        return str(order.id)

    def place_short_bracket(
        self,
        symbol: str,
        entry: float,
        sl: float,
        tp: float,
    ) -> str:
        """
        Orden límite de venta en corto (Short) con bracket (SL + TP).
        """
        qty = self._calculate_qty(entry)
        req = LimitOrderRequest(
            symbol         = symbol,
            qty            = qty,
            side           = OrderSide.SELL,
            time_in_force  = TimeInForce.DAY,
            limit_price    = round(entry, 2),
            order_class    = OrderClass.BRACKET,
            stop_loss      = {"stop_price": round(sl, 2)},
            take_profit    = {"limit_price": round(tp, 2)},
        )
        order = self.tc.submit_order(req)
        log.info(
            f"[SHORT BRACKET] id={order.id} qty={qty} "
            f"entry={entry:.2f}  sl={sl:.2f}  tp={tp:.2f}"
        )
        return str(order.id)

    def cancel_order(self, order_id: str):
        """
        Cancela la orden padre y todas sus órdenes hijo (SL + TP si aún no ejecutadas).
        """
        try:
            self.tc.cancel_order_by_id(order_id)
            log.info(f"Orden {order_id} cancelada correctamente.")
        except Exception as e:
            log.warning(f"cancel_order({order_id}): {e}")

    def close_position_at_market(self, symbol: str):
        """
        Cierra toda la posición abierta a precio de mercado. Alpaca cancela automáticamente los child orders pendientes (SL/TP).
        """
        try:
            self.tc.close_position(symbol)
            log.info(f"Posición {symbol} cerrada a mercado.")
        except Exception as e:
            log.warning(f"close_position({symbol}): {e}")

    def is_order_filled(self, order_id: str) -> bool:
        """True si la orden padre está completamente ejecutada (filled)."""
        try:
            status = str(self.tc.get_order_by_id(order_id).status)
            return status == "filled"
        except Exception as e:
            log.warning(f"is_order_filled({order_id}): {e}")
            return False