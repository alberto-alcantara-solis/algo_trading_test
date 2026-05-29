"""
state.py — Modelos de datos y estado persistido del bot.
─────────────────────────────────────────────────────────
El estado completo se serializa a JSON después de cada vela procesada, lo que permite reanudar exactamente donde se quedó si el proceso cae.
"""

import json
import os
from dataclasses import dataclass, field, asdict
from typing import Optional, List


class S:
    """Estados posibles de la máquina de estados."""
    WAITING_OPEN   = "WAITING_OPEN"
    CALC_RANGE     = "CALC_RANGE"
    WAITING_BREAK  = "WAITING_BREAK"
    WAITING_FVG    = "WAITING_FVG"
    WAIT_1ST_CONF  = "WAIT_1ST_CONF"
    WAIT_2ND_CONF  = "WAIT_2ND_CONF"
    ORDER_LAUNCHED = "ORDER_LAUNCHED"
    MONITORING     = "MONITORING"
    DAY_ENDED      = "DAY_ENDED"

class Dir:
    """Dirección de la operación."""
    LONG  = "LONG"
    SHORT = "SHORT"

@dataclass
class Candle:
    """
    Vela de 1 minuto completamente cerrada.
    """
    timestamp: str
    open: float
    high: float
    low: float
    close: float

    @property
    def is_green(self) -> bool:
        """Cierra por encima de donde abrió (alcista)."""
        return self.close > self.open

    @property
    def is_red(self) -> bool:
        """Cierra por debajo de donde abrió (bajista)."""
        return self.close < self.open

    def to_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, d: Optional[dict]) -> Optional["Candle"]:
        return cls(**d) if d else None

@dataclass
class BotState:
    """
    Estado completo y persistido del bot de trading.
    """
    status: str           = S.WAITING_OPEN
    trade_date: str       = ""
    market_open_utc: str  = ""
    market_close_utc: str = ""

    opening_bars: List[dict] = field(default_factory=list)
    top_lim: Optional[float] = None
    bot_lim: Optional[float] = None

    last_bar_ts: Optional[str] = None

    direction: Optional[str]  = None
    c1: Optional[dict]        = None
    c2: Optional[dict]        = None
    c3: Optional[dict]        = None
    top_lim_fvg: Optional[float] = None
    bot_lim_fvg: Optional[float] = None

    min_close_fvg: Optional[float] = None
    max_close_fvg: Optional[float] = None

    entry_price: Optional[float] = None
    stop_loss:   Optional[float] = None
    take_profit: Optional[float] = None
    order_id:    Optional[str]   = None

    replay_from_ts: Optional[str] = None

    _ema_seed_cache: Optional[float] = None


    # ─────────────────────────────────────────────────────────────────────────
    # Helpers
    # ─────────────────────────────────────────────────────────────────────────
    def save(self, path: str = "state.json"):
        """Serializa el estado completo a JSON."""
        with open(path, "w") as f:
            json.dump(asdict(self), f, indent=2, default=str)

    @classmethod
    def load(cls, path: str = "state.json") -> "BotState":
        """Carga el estado desde JSON. Si no existe, devuelve estado inicial."""
        if not os.path.exists(path):
            return cls()
        with open(path) as f:
            data = json.load(f)
        valid_keys = {f.name for f in cls.__dataclass_fields__.values()}
        data = {k: v for k, v in data.items() if k in valid_keys}
        return cls(**data)

    def reset_fvg(self):
        """
        Limpia todo lo relacionado con el FVG actual. Se llama al volver a WAITING_BREAK.
        """
        self.direction       = None
        self.c1 = self.c2 = self.c3 = None
        self.top_lim_fvg = self.bot_lim_fvg = None
        self.min_close_fvg = self.max_close_fvg = None
        self.entry_price = self.stop_loss = self.take_profit = None
        self.order_id    = None

    def reset_for_day(
        self,
        date_str: str,
        market_open_utc: str,
        market_close_utc: str
    ):
        """
        Resetea el estado completo para comenzar una nueva sesión. Se mantienen solo los datos de Alpaca y configuración.
        """
        self.status           = S.WAITING_OPEN
        self.trade_date       = date_str
        self.market_open_utc  = market_open_utc
        self.market_close_utc = market_close_utc
        self.opening_bars     = []
        self.top_lim = self.bot_lim = None
        self.last_bar_ts      = None
        self.replay_from_ts   = None
        self._ema_seed_cache  = None
        self.reset_fvg()