"""
sizing.py — Dynamic order sizing from the live IBKR account value.
"""


import logging
from datetime import datetime, timedelta
from typing import Optional

from ib_insync import IB, Contract, Forex

from config import *


log = logging.getLogger(__name__)

_cache: dict = {"qty": None, "ts": None}


def _account_net_liquidation(ib: IB) -> Optional[float]:
    """Return NetLiquidation in the account base currency, or None."""
    try:
        rows = ib.accountSummary()
    except Exception as exc:
        log.error("Sizing: accountSummary failed: %s", exc)
        return None

    fallback = None
    for row in rows:
        if row.tag != "NetLiquidation":
            continue
        try:
            value = float(row.value)
        except (TypeError, ValueError):
            continue
        if row.currency == ACCOUNT_CURRENCY:
            return value
        fallback = value

    if fallback is not None:
        log.warning(
            "Sizing: NetLiquidation not reported in %s — using account default currency value.",
            ACCOUNT_CURRENCY,
        )
    return fallback

def _fx_midpoint(ib: IB, from_ccy: str, to_ccy: str) -> Optional[float]:
    """
    Return the midpoint rate to convert 1 unit of *from_ccy* into *to_ccy*.
    Tries the direct pair first (e.g. EURGBP), then the inverse (GBPEUR -> 1/x).
    """
    if from_ccy == to_ccy:
        return 1.0

    for pair, invert in ((from_ccy + to_ccy, False), (to_ccy + from_ccy, True)):
        try:
            contract = Forex(pair)
            ib.qualifyContracts(contract)
            bars = ib.reqHistoricalData(
                contract,
                endDateTime="",
                durationStr="600 S",
                barSizeSetting="1 min",
                whatToShow="MIDPOINT",
                useRTH=False,
                formatDate=2,
            )
            if bars:
                px = float(bars[-1].close)
                if px > 0:
                    return (1.0 / px) if invert else px
        except Exception as exc:
            log.debug("Sizing: FX rate via %s failed: %s", pair, exc)
            continue

    log.error("Sizing: could not fetch FX rate %s->%s", from_ccy, to_ccy)
    return None

def compute_order_quantity(ib: IB, contract: Contract) -> int:
    """
    Quantity (in the pair's base currency units) for one order:
    TRADE_QUANTITY fraction of the live account value, FX-converted.
    """
    now = datetime.now()
    if (
        _cache["qty"] is not None
        and _cache["ts"] is not None
        and now - _cache["ts"] < timedelta(minutes=SIZING_CACHE_MINUTES)
    ):
        return _cache["qty"]

    base_ccy = getattr(contract, "symbol", None) or ""
    qty: Optional[float] = None

    net_liq = _account_net_liquidation(ib)
    if net_liq is not None and net_liq > 0 and base_ccy:
        rate = _fx_midpoint(ib, ACCOUNT_CURRENCY, base_ccy)
        if rate is not None and rate > 0:
            qty = net_liq * TRADE_QUANTITY * rate
            log.info(
                "Sizing: NetLiq=%.2f %s  x %.0f%%  x %s%s=%.5f  ->  %.0f %s",
                net_liq, ACCOUNT_CURRENCY, TRADE_QUANTITY * 100,
                ACCOUNT_CURRENCY, base_ccy, rate, qty, base_ccy,
            )

    if qty is None:
        qty = TOTAL_CAPITAL * TRADE_QUANTITY
        log.warning(
            "Sizing: falling back to static notional TOTAL_CAPITAL * TRADE_QUANTITY = %.0f",
            qty,
        )

    qty_int = int(round(qty))
    if qty_int <= 0:
        log.error("Sizing: computed quantity <= 0 — orders will be skipped.")
        return 0

    if MIN_ORDER_QUANTITY and qty_int < MIN_ORDER_QUANTITY:
        log.warning(
            "Sizing: quantity %d is below MIN_ORDER_QUANTITY=%d — IDEALPRO may reject it.",
            qty_int, MIN_ORDER_QUANTITY,
        )

    _cache["qty"] = qty_int
    _cache["ts"] = now
    return qty_int


def invalidate_cache() -> None:
    """Force the next compute_order_quantity() call to refresh from IB."""
    _cache["qty"] = None
    _cache["ts"] = None
