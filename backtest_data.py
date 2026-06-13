"""
backtest_data.py — Load HistData.com M1 ASCII CSVs and resample them to M5.

Part of the offline backtester (backtest_engine.py / backtest_run.py).
It does NOT modify any existing project file: it only *imports* the project's
Bar dataclass and REF_TZ so the backtest works on the exact same UTC+8
timestamp convention as the live bot.

Supported input formats (auto-detected line by line):

  1) HistData "Generic ASCII" M1 (the usual DAT_ASCII_GBPUSD_M1_YYYY.csv):
         20230102 170100;1.234560;1.234610;1.234380;1.234500;0
  2) Same layout with commas:
         20230102 170100,1.23456,1.23461,1.23438,1.23450,0
  3) MT4-style export:
         2023.01.02,17:01,1.23456,1.23461,1.23438,1.23450,0

IMPORTANT — timezone: HistData ASCII files are stamped in US Eastern
Standard Time (UTC-5, fixed, NO daylight saving).  The loader shifts every
timestamp into the bot's reference timezone (UTC+8).  If your file uses a
different timezone, pass the proper tz_offset_hours (e.g. 0 for UTC data).
"""

from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone
from typing import List, Optional

from config import REF_TZ
from full_trading import Bar

log = logging.getLogger(__name__)

# HistData generic ASCII = EST (UTC-5), fixed all year.
HISTDATA_TZ_OFFSET_HOURS = -5


def _parse_line(line: str, src_tz: timezone) -> Optional[Bar]:
    line = line.strip()
    if not line:
        return None

    sep = ";" if ";" in line else ","
    p = [x.strip() for x in line.split(sep)]
    if len(p) < 5:
        return None

    try:
        if " " in p[0]:                      # "YYYYMMDD HHMMSS"
            dt = datetime.strptime(p[0], "%Y%m%d %H%M%S")
            vals = p[1:6]
        elif "." in p[0] and len(p) >= 6 and ":" in p[1]:   # "YYYY.MM.DD","HH:MM"
            dt = datetime.strptime(p[0] + " " + p[1], "%Y.%m.%d %H:%M")
            vals = p[2:7]
        else:
            return None                      # header / unknown row
        o, h, l, c = (float(v) for v in vals[:4])
    except (ValueError, IndexError):
        return None

    vol = 0.0
    if len(vals) > 4 and vals[4]:
        try:
            vol = float(vals[4])
        except ValueError:
            vol = 0.0

    return Bar(
        open=o, high=h, low=l, close=c, volume=vol,
        date=dt.replace(tzinfo=src_tz).astimezone(REF_TZ),
    )


def load_histdata_m1(path: str,
                     tz_offset_hours: int = HISTDATA_TZ_OFFSET_HOURS) -> List[Bar]:
    """
    Load a HistData M1 CSV and return REF_TZ-aware Bars, oldest first.
    Bad/header lines are skipped silently (counted in the log).
    """
    src_tz = timezone(timedelta(hours=tz_offset_hours))
    bars: List[Bar] = []
    skipped = 0

    with open(path, "r", encoding="utf-8", errors="replace") as fh:
        for line in fh:
            bar = _parse_line(line, src_tz)
            if bar is None:
                if line.strip():
                    skipped += 1
                continue
            bars.append(bar)

    bars.sort(key=lambda b: b.date)

    # de-duplicate identical timestamps (some HistData files have repeats)
    dedup: List[Bar] = []
    for b in bars:
        if dedup and dedup[-1].date == b.date:
            dedup[-1] = b
        else:
            dedup.append(b)

    log.info("Loaded %d M1 bars from %s (%d lines skipped)  range %s -> %s",
             len(dedup), path, skipped,
             dedup[0].date if dedup else "-", dedup[-1].date if dedup else "-")
    return dedup


def resample_m1_to_m5(m1: List[Bar]) -> List[Bar]:
    """
    Aggregate M1 bars into M5 bars (bar.date = open time of the 5-min window,
    matching the live bot's candle-open convention).
    """
    out: List[Bar] = []
    cur: Optional[Bar] = None

    for b in m1:
        key = b.date.replace(minute=(b.date.minute // 5) * 5,
                             second=0, microsecond=0)
        if cur is None or key != cur.date:
            if cur is not None:
                out.append(cur)
            cur = Bar(open=b.open, high=b.high, low=b.low,
                      close=b.close, volume=b.volume, date=key)
        else:
            cur.high = max(cur.high, b.high)
            cur.low = min(cur.low, b.low)
            cur.close = b.close
            cur.volume += b.volume

    if cur is not None:
        out.append(cur)

    log.info("Resampled %d M1 bars -> %d M5 bars", len(m1), len(out))
    return out
