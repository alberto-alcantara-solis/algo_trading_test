"""
config.py — Todos los parámetros configurables del bot.
"""

import os
from dotenv import load_dotenv

load_dotenv()

SYMBOL: str          = "QQQ"
TRADE_DIRECTION: str = "BOTH"   # "LONG" | "SHORT" | "BOTH"

CAPITAL_PCT: float = 0.20

OPENING_RANGE_BARS: int = 15
EMA_LENGTH: int          = 10
EMA_SOURCE: str          = "close"
RISK_REWARD: float       = 2.75

CUTOFF_ANY_MINS: int      = 15
CUTOFF_LAUNCHED_MINS: int = 10
CUTOFF_MONITOR_MINS: int  = 5

CHECK_INTERVAL_SEC: int = 5

STATE_FILE: str = "state.json"
LOG_FILE: str   = "bot.log"

ALPACA_API_KEY: str    = os.getenv("ALPACA_API_KEY", "")
ALPACA_SECRET_KEY: str = os.getenv("ALPACA_SECRET_KEY", "")
PAPER: bool            = True   # True | False