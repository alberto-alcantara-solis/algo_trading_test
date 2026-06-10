"""
config.py — All configurable parameters for the forex trading bot.
"""


from datetime import timezone, timedelta


IB_HOST = "127.0.0.1"
IB_PORT = 4002
IB_CLIENT_ID = 1

SYMBOL   = "GBPUSD"
CURRENCY = "EUR"
SECTYPE  = "FOREX"
EXCHANGE = "IDEALPRO"

TOTAL_CAPITAL = 100000.0
TRADE_QUANTITY = 0.2

REF_TZ = timezone(timedelta(hours=8))
ASIA_LOCAL_OPEN_HOUR  = 9
ASIA_LOCAL_CLOSE_HOUR = 18
LONDON_LOCAL_OPEN_HOUR  = 8
LONDON_LOCAL_CLOSE_HOUR = 17
NY_LOCAL_OPEN_HOUR  = 8
NY_LOCAL_CLOSE_HOUR = 17

BAR_SIZE = "5 mins"

VALUE_AREA_PCT = 0.70

EMA_MAIN         = 100
EMA_TREND        = 9
MAX_SLOTS        = 10
RISK_REWARD      = 2.75

STATE_FILE = "bot_state.json"
