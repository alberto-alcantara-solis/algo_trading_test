"""
volume_profile.py — Fixed Range Volume Profile (FRVP) with 70 % Value Area.
Returns VAH, POC, VAL for a given time range.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Optional, List, Tuple

import numpy as np
import pandas as pd

from ib_insync import IB, Contract, HistoricalTick

from config import VALUE_AREA_PCT, REF_TZ


log = logging.getLogger(__name__)

PRICE_BUCKETS = 200
MAX_TICKS_PER_REQUEST = 1000


@dataclass
class VolumeProfile:
    """Result of a Fixed Range Volume Profile computation."""
    vah: float
    poc: float
    val: float
    label: str = ""
    total_volume: float = 0.0
    price_buckets_used: int = 0

def _fetch_ticks(
    ib: IB,
    contract: Contract,
    start_dt: datetime,
    end_dt: datetime,
    what_to_show: str = "TRADES",
) -> List[HistoricalTick]:
    """
    Fetch historical tick data from IB using reqHistoricalTicks.
    """
    all_ticks = []
    current_end = end_dt
    
    def dt_to_ib_str(dt: datetime) -> str:
        return dt.strftime("%Y%m%d %H:%M:%S") + " UTC+8"
    
    max_requests = 1000
    request_count = 0
    
    while request_count < max_requests:
        request_count += 1
        end_str = dt_to_ib_str(current_end)
        log.debug(f"Fetching ticks up to {end_str}, request #{request_count}")
        
        try:
            ticks = ib.reqHistoricalTicks(
                contract=contract,
                startDateTime="",
                endDateTime=end_str,
                numberOfTicks=MAX_TICKS_PER_REQUEST,
                whatToShow=what_to_show,
                useRth=False,
                ignoreSize=False,
                miscOptions=[]
            )
            
            if not ticks:
                log.debug("No more ticks returned")
                break
            
            filtered_ticks = [t for t in ticks if start_dt <= _tick_dt(t) <= end_dt]
            
            if filtered_ticks:
                all_ticks.extend(filtered_ticks)
                earliest_tick_time = _tick_dt(ticks[0])
                log.debug(f"Got {len(filtered_ticks)} ticks in range, earliest: {earliest_tick_time}")
                
                if earliest_tick_time <= start_dt:
                    break
                
                current_end = earliest_tick_time - timedelta(microseconds=1)
            else:
                break
                
        except Exception as e:
            log.error(f"Error fetching ticks: {e}")
            break
    
    log.info(f"Total ticks fetched: {len(all_ticks)}")
    return all_ticks

def _tick_dt(tick: HistoricalTick) -> datetime:
    """Extract datetime from HistoricalTick object."""
    if hasattr(tick, 'time') and tick.time:
        if isinstance(tick.time, datetime):
            return tick.time
        return pd.to_datetime(tick.time).tz_localize('UTC')
    return None

def _ticks_to_volume_profile(
    ticks: List[HistoricalTick],
    n_buckets: int = PRICE_BUCKETS,
    value_area_pct: float = VALUE_AREA_PCT,
) -> Optional[Tuple[np.ndarray, np.ndarray, float, float, float]]:
    """
    Convert tick data to volume profile histogram. Returns a tuple of (bucket_prices, volume_histogram, vah, poc, val) or None if insufficient data
    """
    if not ticks:
        log.error("No ticks provided for volume profile")
        return None
    
    prices = np.array([tick.price for tick in ticks], dtype=float)
    volumes = np.array([tick.size for tick in ticks], dtype=float)
    
    price_min = prices.min()
    price_max = prices.max()
    
    if price_max <= price_min:
        log.error("Price range is zero or negative")
        return None
    
    total_volume_raw = volumes.sum()
    log.info(f"Total volume from ticks: {total_volume_raw:,.0f} units")
    
    bucket_size = (price_max - price_min) / n_buckets
    bucket_prices = np.linspace(
        price_min + bucket_size / 2,
        price_max - bucket_size / 2,
        n_buckets
    )
    volume_histogram = np.zeros(n_buckets, dtype=float)
    
    for price, vol in zip(prices, volumes):
        idx = int((price - price_min) / (price_max - price_min) * (n_buckets - 1))
        idx = max(0, min(idx, n_buckets - 1))
        volume_histogram[idx] += vol
    
    total_volume = volume_histogram.sum()
    if total_volume == 0:
        log.error("Total volume in histogram is zero")
        return None
    
    poc_idx = int(np.argmax(volume_histogram))
    poc = bucket_prices[poc_idx]
    
    target = total_volume * value_area_pct
    accumulated = volume_histogram[poc_idx]
    lo_idx = poc_idx
    hi_idx = poc_idx
    
    while accumulated < target:
        expand_lo = (lo_idx > 0)
        expand_hi = (hi_idx < n_buckets - 1)
        
        if not expand_lo and not expand_hi:
            break
        
        vol_above = volume_histogram[hi_idx + 1] if expand_hi else -1
        vol_below = volume_histogram[lo_idx - 1] if expand_lo else -1
        
        if vol_above >= vol_below:
            hi_idx += 1
            accumulated += volume_histogram[hi_idx]
        else:
            lo_idx -= 1
            accumulated += volume_histogram[lo_idx]
    
    vah = bucket_prices[hi_idx] + bucket_size / 2
    val = bucket_prices[lo_idx] - bucket_size / 2
    
    return bucket_prices, volume_histogram, vah, poc, val

def compute_volume_profile(
    ib: IB,
    contract: Contract,
    start_dt: datetime,
    end_dt: datetime,
    label: str = "",
    n_buckets: int = PRICE_BUCKETS,
    value_area_pct: float = VALUE_AREA_PCT,
    what_to_show: str = "TRADES",
) -> Optional[VolumeProfile]:
    """
    Compute a Fixed Range Volume Profile using tick-level trade data.
    """
    log.info(f"FRVP [{label}]: Fetching tick data from {start_dt} to {end_dt}")
    
    if start_dt.tzinfo is None:
        start_dt = start_dt.replace(tzinfo=REF_TZ)
    if end_dt.tzinfo is None:
        end_dt = end_dt.replace(tzinfo=REF_TZ)
    
    ticks = _fetch_ticks(ib, contract, start_dt, end_dt, what_to_show)
    
    if len(ticks) < 10:
        log.error(f"FRVP [{label}]: Insufficient tick data (got {len(ticks)} ticks)")
        return None
    
    result = _ticks_to_volume_profile(ticks, n_buckets, value_area_pct)
    
    if result is None:
        log.error(f"FRVP [{label}]: Failed to compute volume profile")
        return None
    
    bucket_prices, volume_histogram, vah, poc, val = result
    
    total_volume = sum(tick.size for tick in ticks)
    
    profile = VolumeProfile(
        vah=vah,
        poc=poc,
        val=val,
        label=label,
        total_volume=total_volume,
        price_buckets_used=n_buckets
    )
    
    log.info(
        f"FRVP [{label}]: VAH={vah:.5f}  POC={poc:.5f}  VAL={val:.5f}  "
        f"Total Volume={total_volume:,.0f}  Ticks={len(ticks)}"
    )
    
    return profile

def compute_volume_profile_with_ohlcv_fallback(
    ib: IB,
    contract: Contract,
    start_dt: datetime,
    end_dt: datetime,
    label: str = "",
    n_buckets: int = PRICE_BUCKETS,
    value_area_pct: float = VALUE_AREA_PCT,
    fallback_to_ohlcv: bool = True,
) -> Optional[VolumeProfile]:
    """
    Compute volume profile with automatic fallback to OHLCV method if tick data fails.
    """
    result = compute_volume_profile(
        ib, contract, start_dt, end_dt, label,
        n_buckets, value_area_pct
    )
    
    if result is not None:
        return result
    else:
        log.error(f"Unable to compute tick-based volume profile for [{label}].")
    
    return None

def get_price_range_stats(
    ib: IB,
    contract: Contract,
    start_dt: datetime,
    end_dt: datetime,
) -> Optional[dict]:
    """
    Get price range statistics from tick data for analysis.
    """
    ticks = _fetch_ticks(ib, contract, start_dt, end_dt)
    
    if not ticks:
        return None
    
    prices = [tick.price for tick in ticks]
    volumes = [tick.size for tick in ticks]
    
    vwap = sum(p * v for p, v in zip(prices, volumes)) / sum(volumes)
    
    return {
        'min_price': min(prices),
        'max_price': max(prices),
        'vwap': vwap,
        'total_volume': sum(volumes),
        'tick_count': len(ticks),
        'mean_price': np.mean(prices),
        'std_price': np.std(prices),
    }