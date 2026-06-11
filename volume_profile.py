"""
volume_profile.py — Fixed Range Volume Profile (FRVP) with 70% Value Area.
Not really a "volume" profile.
"""

from __future__ import annotations

import logging
import math
from dataclasses import dataclass
from datetime import datetime
from typing import List, Optional, Sequence, Tuple

import numpy as np

from ib_insync import IB, Contract

from config import *

log = logging.getLogger(__name__)

PRICE_BUCKETS = 200

BarTuple = Tuple[datetime, float, float, float, float]


@dataclass
class VolumeProfile:
    """Result of a Fixed Range Volume Profile computation."""
    vah: float
    poc: float
    val: float
    label: str = ""
    total_volume: float = 0.0
    price_buckets_used: int = 0
    bars_used: int = 0


# ---------------------------------------------------------------------------
# Data fetching
# ---------------------------------------------------------------------------
def _ensure_ref_tz(dt: datetime) -> datetime:
    if dt.tzinfo is None:
        return dt.replace(tzinfo=REF_TZ)
    return dt.astimezone(REF_TZ)


def _fetch_window_bars(
    ib: IB,
    contract: Contract,
    start_dt: datetime,
    end_dt: datetime,
    bar_size: str,
) -> List[BarTuple]:
    """
    Fetch MIDPOINT bars covering [start_dt, end_dt) in one request and return them as REF_TZ-aware tuples.
    """
    span_s = (end_dt - start_dt).total_seconds()
    if span_s <= 0:
        log.error("FRVP: start_dt >= end_dt (%s >= %s)", start_dt, end_dt)
        return []

    duration = f"{int(min(math.ceil(span_s), 86400))} S"

    try:
        bars = ib.reqHistoricalData(
            contract,
            endDateTime    = end_dt,
            durationStr    = duration,
            barSizeSetting = bar_size,
            whatToShow     = "MIDPOINT",
            useRTH         = False,
            formatDate     = 2,
        )
    except Exception as exc:
        log.error("FRVP: reqHistoricalData failed: %s", exc)
        return []

    out: List[BarTuple] = []
    for b in bars or []:
        bd = b.date
        if bd.tzinfo is None:
            bd = bd.replace(tzinfo=REF_TZ)
        else:
            bd = bd.astimezone(REF_TZ)
        if start_dt <= bd < end_dt:
            out.append((bd, float(b.open), float(b.high), float(b.low), float(b.close)))
    return out


# ---------------------------------------------------------------------------
# Pure profile math (no IB)
# ---------------------------------------------------------------------------
def bars_to_profile(
    bars: Sequence[BarTuple],
    n_buckets: int = PRICE_BUCKETS,
    value_area_pct: float = VALUE_AREA_PCT,
) -> Optional[Tuple[np.ndarray, np.ndarray, float, float, float]]:
    """
    Build a time-at-price histogram from bar tuples.
    """
    if not bars:
        return None

    highs = np.array([max(b[2], b[3]) for b in bars], dtype=float)
    lows  = np.array([min(b[2], b[3]) for b in bars], dtype=float)

    price_min = float(lows.min())
    price_max = float(highs.max())

    if price_max <= price_min:
        center = np.array([price_min])
        return center, np.array([float(len(bars))]), price_min, price_min, price_min

    edges = np.linspace(price_min, price_max, n_buckets + 1)
    bucket_size = edges[1] - edges[0]
    centers = (edges[:-1] + edges[1:]) / 2.0
    hist = np.zeros(n_buckets, dtype=float)

    for _, _o, h, l, _c in bars:
        lo, hi = (l, h) if h >= l else (h, l)
        if hi <= lo:
            idx = int((lo - price_min) / (price_max - price_min) * (n_buckets - 1))
            idx = max(0, min(idx, n_buckets - 1))
            hist[idx] += 1.0
            continue

        i0 = int(np.searchsorted(edges, lo, side="right")) - 1
        i1 = int(np.searchsorted(edges, hi, side="left")) - 1
        i0 = max(0, min(i0, n_buckets - 1))
        i1 = max(0, min(i1, n_buckets - 1))
        rng = hi - lo
        for i in range(i0, i1 + 1):
            overlap = min(hi, edges[i + 1]) - max(lo, edges[i])
            if overlap > 0:
                hist[i] += overlap / rng

    total = float(hist.sum())
    if total <= 0:
        return None

    poc_idx = int(np.argmax(hist))
    poc = float(centers[poc_idx])

    target = total * value_area_pct
    accumulated = hist[poc_idx]
    lo_idx = poc_idx
    hi_idx = poc_idx

    while accumulated < target:
        expand_lo = lo_idx > 0
        expand_hi = hi_idx < n_buckets - 1
        if not expand_lo and not expand_hi:
            break
        vol_above = hist[hi_idx + 1] if expand_hi else -1.0
        vol_below = hist[lo_idx - 1] if expand_lo else -1.0
        if vol_above >= vol_below:
            hi_idx += 1
            accumulated += hist[hi_idx]
        else:
            lo_idx -= 1
            accumulated += hist[lo_idx]

    vah = float(centers[hi_idx] + bucket_size / 2.0)
    val = float(centers[lo_idx] - bucket_size / 2.0)

    return centers, hist, vah, poc, val


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------
def compute_volume_profile(
    ib: IB,
    contract: Contract,
    start_dt: datetime,
    end_dt: datetime,
    label: str = "",
    n_buckets: int = PRICE_BUCKETS,
    value_area_pct: float = VALUE_AREA_PCT,
    bar_size: str = VP_BAR_SIZE,
) -> Optional[VolumeProfile]:
    """
    Compute a Fixed Range time-at-price profile over [start_dt, end_dt).
    """
    start_dt = _ensure_ref_tz(start_dt)
    end_dt   = _ensure_ref_tz(end_dt)

    log.info("FRVP [%s]: fetching %s MIDPOINT bars  %s -> %s", label, bar_size, start_dt, end_dt)

    raw = _fetch_window_bars(ib, contract, start_dt, end_dt, bar_size)

    if len(raw) < VP_MIN_BARS and bar_size != "5 mins":
        log.warning("FRVP [%s]: only %d x %s bars — retrying with 5-min bars.",
                    label, len(raw), bar_size)
        raw = _fetch_window_bars(ib, contract, start_dt, end_dt, "5 mins")

    if len(raw) < VP_MIN_BARS:
        log.error("FRVP [%s]: insufficient bars (got %d) — no profile.", label, len(raw))
        return None

    result = bars_to_profile(raw, n_buckets, value_area_pct)
    if result is None:
        log.error("FRVP [%s]: failed to compute profile.", label)
        return None

    _centers, hist, vah, poc, val = result

    profile = VolumeProfile(
        vah=vah,
        poc=poc,
        val=val,
        label=label,
        total_volume=float(hist.sum()),
        price_buckets_used=len(hist),
        bars_used=len(raw),
    )

    log.info(
        "FRVP [%s]: VAH=%.5f  POC=%.5f  VAL=%.5f  bars=%d  weight=%.1f",
        label, vah, poc, val, len(raw), profile.total_volume,
    )
    return profile
