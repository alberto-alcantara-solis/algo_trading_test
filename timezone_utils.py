"""
timezone_utils.py — Compute dynamic market-session boundaries in UTC+8.
"""


import logging

from datetime import datetime
from zoneinfo import ZoneInfo

from dataclasses import dataclass

from config import *


log = logging.getLogger(__name__)

ASIA_TZ      = ZoneInfo("Asia/Tokyo")
LONDON_TZ   = ZoneInfo("Europe/London")
NY_TZ       = ZoneInfo("America/New_York")


def _utc_offset_hours(tz: ZoneInfo) -> int:
    """Return the current UTC offset of *tz* in whole hours (e.g. +1, -4)."""
    now = datetime.now(tz)
    return int(now.utcoffset().total_seconds() / 3600)

def _session_in_ref(local_hour: int, local_tz: ZoneInfo, is_close: bool=False) -> tuple[int, int]:
    """
    Convert a session-open/close hour expressed in *local_tz* to the equivalent hour in REF_TZ (UTC+8).
    If *is_close* is True, the returned hour is adjusted back by 5 minutes to align with our bar close times.
    """
    ref_offset   = _utc_offset_hours(REF_TZ)
    local_offset = _utc_offset_hours(local_tz)
    ref_hour = (local_hour + (ref_offset - local_offset)) % 24
    return (ref_hour, 0) if not is_close else ((ref_hour - 1) % 24, 55)

def _bar_dt(bar) -> datetime:
    """
    Convert IB bar datetime to REF_TZ-aware datetime (UTC+8). Assumes IB bar.date is UTC if naive.
    """
    dt = bar.date
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=ZoneInfo("UTC"))
    return dt.astimezone(REF_TZ)

@dataclass
class SessionBoundariesCandles:
    """All key session boundaries expressed as UTC+8 hour integers."""
    asia_open:                     tuple[int, int] = (0, 0)
    asia_close:                    tuple[int, int] = (0, 0)
    london_open:                   tuple[int, int] = (0, 0)
    london_close:                  tuple[int, int] = (0, 0)
    ny_open:                       tuple[int, int] = (0, 0)
    ny_close:                      tuple[int, int] = (0, 0)
    shut_off:                      tuple[int, int] = (0, 0)
    mid_london_asia_open:          tuple[int, int] = (0, 0)
    mid_london_close_shutoff_open: tuple[int, int] = (0, 0)

def _mid_minutes(hour_a: int, hour_b: int) -> int:
    """
    Return the midpoint between two hours. Handles overnight wrap (e.g. 17 → 02).
    """
    minutes_a = hour_a * 60
    minutes_b = hour_b * 60
    if minutes_b <= minutes_a:
        minutes_b += 24 * 60
    mid = (minutes_a + minutes_b) // 2
    return (mid - 5) % (24 * 60)

def compute_boundaries() -> SessionBoundariesCandles:
    """
    Compute all session boundaries for *today* and return a SessionBoundariesCandles.
    """
    sb = SessionBoundariesCandles()
    sb.asia_open  = _session_in_ref(ASIA_LOCAL_OPEN_HOUR, ASIA_TZ)
    sb.asia_close  = _session_in_ref(ASIA_LOCAL_CLOSE_HOUR, ASIA_TZ, is_close=True)

    sb.london_open  = _session_in_ref(LONDON_LOCAL_OPEN_HOUR,  LONDON_TZ)
    sb.london_close = _session_in_ref(LONDON_LOCAL_CLOSE_HOUR, LONDON_TZ, is_close=True)

    sb.ny_open  = _session_in_ref(NY_LOCAL_OPEN_HOUR,  NY_TZ)
    sb.ny_close = _session_in_ref(NY_LOCAL_CLOSE_HOUR, NY_TZ, is_close=True)

    sb.shut_off = (((sb.ny_close[0] - 2) % 24), sb.ny_close[1])

    mid_london_asia_minutes = _mid_minutes(sb.london_open[0], sb.asia_close[0]+1)
    bar_open_min = (mid_london_asia_minutes // 5) * 5
    sb.mid_london_asia_open = (((bar_open_min // 60) % 24), (bar_open_min % 60))

    mid_lc_so_minutes = _mid_minutes(sb.london_close[0], sb.shut_off[0]+1)
    bar_open_min2 = (mid_lc_so_minutes // 5) * 5
    sb.mid_london_close_shutoff_open = (((bar_open_min2 // 60) % 24), (bar_open_min2 % 60))

    log.info(
        "Session boundaries (UTC+8):  "
        "Asia %02d:%02d-%02d:%02d  |  London %02d:%02d-%02d:%02d  |  "
        "NY %02d:%02d-%02d:%02d  |  ShutOff %02d:%02d  |  "
        "Mid1 %02d:%02d  |  Mid2 %02d:%02d",
        sb.asia_open[0], sb.asia_open[1],
        sb.asia_close[0], sb.asia_close[1],
        sb.london_open[0], sb.london_open[1],
        sb.london_close[0], sb.london_close[1],
        sb.ny_open[0], sb.ny_open[1],
        sb.ny_close[0], sb.ny_close[1],
        sb.shut_off[0], sb.shut_off[1],
        sb.mid_london_asia_open[0], sb.mid_london_asia_open[1],
        sb.mid_london_close_shutoff_open[0], sb.mid_london_close_shutoff_open[1],
    )
    return sb

def is_trading_day(dt: datetime | None = None) -> bool:
    """
    Return True if *dt* (defaults to now in REF_TZ) falls on a Mon-Fri.
    We deliberately ignore exchange-specific holidays for simplicity.
    """
    if dt is None:
        dt = datetime.now(REF_TZ)
    return dt.weekday() < 5