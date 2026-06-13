"""
backtest_engine.py — Offline simulator of the session-anchored VP strategy.

It replicates the live bot day by day WITHOUT modifying any existing file:
  * volume profiles  -> reuses volume_profile.bars_to_profile() (the exact
                        live math) fed with the M1 candles of each window;
  * EMAs             -> reuses full_trading.EMATracker (period 9 trend gate,
                        period 100 Full-Trading gate);
  * session clock    -> re-implements timezone_utils boundary math, but
                        evaluated at the HISTORICAL date (the live module
                        reads "now()" for DST, which a backtest cannot use);
  * order1 / order2 / Full-Trading slots -> same rules as strategy.py and
                        full_trading.py (breaks, FVG, retracement, exit,
                        EMA/level gates, RR bracket, boundary flattening).

Fill model (where a backtest must approximate the broker):
  * market entries fill at the close of the triggering M5 candle;
  * TP (limit) fills when an M1 candle TOUCHES the price; SL (stop) fills
    when an M1 candle touches it — both checked minute by minute, so the
    M1 data gives intrabar resolution the live bot doesn't even need;
  * if one M1 candle spans BOTH the TP and the SL the outcome is ambiguous:
    by default the pessimistic assumption (SL) is used (configurable);
  * Full-Trading parent market orders are assumed to fill instantly, so the
    live ORDER_LAUNCHED cancellation window (price closing back through the
    FVG before the fill is seen) is skipped — with MKT orders this is a
    sub-second window in reality.

Known, deliberate simplifications:
  * EMAs run continuously over the whole dataset.  The live bot re-creates
    them daily and warms them with ~1 day of history, which converges to the
    same values, so the difference is negligible after the first day.
  * No spread/slippage by default (HistData is mid/bid based).  A flat cost
    per side can be applied via cost_pips.
"""

from __future__ import annotations

import logging
from bisect import bisect_left
from dataclasses import dataclass, field
from datetime import date, datetime, time, timedelta
from typing import Dict, List, Optional, Tuple
from zoneinfo import ZoneInfo

from config import (
    REF_TZ,
    ASIA_LOCAL_OPEN_HOUR, ASIA_LOCAL_CLOSE_HOUR,
    LONDON_LOCAL_OPEN_HOUR, LONDON_LOCAL_CLOSE_HOUR,
    NY_LOCAL_CLOSE_HOUR,
    EMA_MAIN, EMA_TREND, MAX_SLOTS, RISK_REWARD, VP_MIN_BARS,
)
from full_trading import Bar, EMATracker
from volume_profile import bars_to_profile

log = logging.getLogger(__name__)

PIP = 0.0001
M5 = timedelta(minutes=5)

ASIA_TZ = ZoneInfo("Asia/Tokyo")
LONDON_TZ = ZoneInfo("Europe/London")
NY_TZ = ZoneInfo("America/New_York")

COMPONENT_ORDER1 = "order1"
COMPONENT_ORDER2 = "order2"
COMPONENT_FT = "full_trading"


# ---------------------------------------------------------------------------
# Session boundaries, evaluated at the historical date
# ---------------------------------------------------------------------------
@dataclass
class DayAnchors:
    asia_open: datetime
    london_open: datetime
    asia_close: datetime          # candle-open style: e.g. 16:55 == close 17:00
    london_close: datetime
    shutoff: datetime
    mid1: Tuple[int, int]         # (hour, minute) candle-open of order1 trigger
    mid2: Tuple[int, int]         # (hour, minute) candle-open of order2 trigger


def _tz_offset_hours(tz: ZoneInfo, d: date) -> int:
    """UTC offset of *tz* on day *d* (evaluated at local noon, so DST-correct)."""
    dt = datetime.combine(d, time(12, 0), tzinfo=tz)
    return int(dt.utcoffset().total_seconds() // 3600)


def anchors_for_date(d: date) -> DayAnchors:
    """Mirror of timezone_utils.compute_boundaries() + strategy datetime
    anchoring, but with DST resolved AT the simulated date."""
    ref_off = 8

    def hm(local_hour: int, tz: ZoneInfo, is_close: bool = False) -> Tuple[int, int]:
        h = (local_hour + (ref_off - _tz_offset_hours(tz, d))) % 24
        return ((h - 1) % 24, 55) if is_close else (h, 0)

    ao = hm(ASIA_LOCAL_OPEN_HOUR, ASIA_TZ)
    ac = hm(ASIA_LOCAL_CLOSE_HOUR, ASIA_TZ, True)
    lo = hm(LONDON_LOCAL_OPEN_HOUR, LONDON_TZ)
    lc = hm(LONDON_LOCAL_CLOSE_HOUR, LONDON_TZ, True)
    nc = hm(NY_LOCAL_CLOSE_HOUR, NY_TZ, True)
    so = ((nc[0] - 2) % 24, nc[1])

    def mid_candle(hour_a: int, hour_b: int) -> Tuple[int, int]:
        a, b = hour_a * 60, hour_b * 60
        if b <= a:
            b += 1440
        m = ((a + b) // 2 - 5) % 1440
        m = (m // 5) * 5
        return (m // 60) % 24, m % 60

    mid1 = mid_candle(lo[0], ac[0] + 1)
    mid2 = mid_candle(lc[0] + 1, so[0] + 1)

    base = datetime.combine(d, time.min, tzinfo=REF_TZ)

    def seq(prev: datetime, t: Tuple[int, int]) -> datetime:
        dt = base.replace(hour=t[0], minute=t[1])
        while dt <= prev:
            dt += timedelta(days=1)
        return dt

    ao_dt = base.replace(hour=ao[0], minute=ao[1])
    lo_dt = seq(ao_dt, lo)
    ac_dt = seq(lo_dt, ac)
    lc_dt = seq(ac_dt, lc)
    so_dt = seq(lc_dt, so)

    return DayAnchors(ao_dt, lo_dt, ac_dt, lc_dt, so_dt, mid1, mid2)


# ---------------------------------------------------------------------------
# Simulated trade
# ---------------------------------------------------------------------------
@dataclass
class SimTrade:
    component: str
    direction: str                # "long" | "short"
    entry_dt: datetime
    entry: float
    qty: float
    tp: Optional[float] = None
    sl: Optional[float] = None
    exit_dt: Optional[datetime] = None
    exit: Optional[float] = None
    reason: str = ""              # "tp" | "sl" | "market_close" | "shutoff"
    trade_date: str = ""
    slot_id: int = 0

    @property
    def is_open(self) -> bool:
        return self.exit is None

    def close(self, dt: datetime, px: float, reason: str) -> None:
        self.exit_dt = dt
        self.exit = px
        self.reason = reason

    def pips(self, cost_pips: float = 0.0) -> float:
        sign = 1.0 if self.direction == "long" else -1.0
        return (self.exit - self.entry) * sign / PIP - cost_pips

    def pnl(self, cost_pips: float = 0.0) -> float:
        return self.pips(cost_pips) * PIP * self.qty


def _check_fill(trade: SimTrade, m1: Bar, ambiguous: str) -> None:
    """Resolve TP/SL touches inside one M1 candle."""
    if not trade.is_open:
        return
    is_long = trade.direction == "long"
    tp_hit = trade.tp is not None and (m1.high >= trade.tp if is_long else m1.low <= trade.tp)
    sl_hit = trade.sl is not None and (m1.low <= trade.sl if is_long else m1.high >= trade.sl)

    if tp_hit and sl_hit:
        if ambiguous == "tp":
            trade.close(m1.date, trade.tp, "tp")
        else:
            trade.close(m1.date, trade.sl, "sl")
    elif sl_hit:
        trade.close(m1.date, trade.sl, "sl")
    elif tp_hit:
        trade.close(m1.date, trade.tp, "tp")


# ---------------------------------------------------------------------------
# Full-Trading slot — same state machine as full_trading.Slot, fills simulated
# ---------------------------------------------------------------------------
class SimSlot:
    FVG_DETECT = "FVG_DETECT"
    WAIT_RETRACEMENT = "WAIT_RETRACEMENT"
    WAIT_EXIT_FVG = "WAIT_EXIT_FVG"
    MONITORING = "MONITORING"

    _id_counter = 0

    def __init__(self, c1: Bar, c2: Bar, direction: str, broke_levels: List[float]):
        SimSlot._id_counter += 1
        self.slot_id = SimSlot._id_counter
        self.stage = self.FVG_DETECT
        self.direction = direction
        self.broke_levels = broke_levels
        self.c1 = c1
        self.c2 = c2
        self.bot_fvg = 0.0
        self.top_fvg = 0.0
        self.trade: Optional[SimTrade] = None

    # returns True if the slot stays alive
    def on_bar(self, bar: Bar, ema: Optional[float], engine: "BacktestEngine") -> bool:
        if self.stage == self.FVG_DETECT:
            return self._fvg_detect(bar)
        if self.stage == self.WAIT_RETRACEMENT:
            return self._wait_retracement(bar)
        if self.stage == self.WAIT_EXIT_FVG:
            return self._wait_exit_fvg(bar, ema, engine)
        if self.stage == self.MONITORING:
            return self.trade is not None and self.trade.is_open
        return False

    def _fvg_detect(self, c3: Bar) -> bool:
        if self.direction == "long":
            if c3.low > self.c1.high:
                self.bot_fvg, self.top_fvg = self.c1.high, c3.low
                self.stage = self.WAIT_RETRACEMENT
                return True
        else:
            if c3.high < self.c1.low:
                self.bot_fvg, self.top_fvg = c3.high, self.c1.low
                self.stage = self.WAIT_RETRACEMENT
                return True
        return False  # no FVG -> discard

    def _wait_retracement(self, bar: Bar) -> bool:
        if self.direction == "long":
            if bar.close < self.bot_fvg:
                return False
            if bar.open >= self.top_fvg and self.bot_fvg <= bar.close < self.top_fvg:
                self.stage = self.WAIT_EXIT_FVG
        else:
            if bar.close > self.top_fvg:
                return False
            if bar.open <= self.bot_fvg and self.bot_fvg < bar.close <= self.top_fvg:
                self.stage = self.WAIT_EXIT_FVG
        return True

    def _wait_exit_fvg(self, bar: Bar, ema: Optional[float], engine: "BacktestEngine") -> bool:
        if self.direction == "long":
            if bar.close < self.bot_fvg:
                return False
            if bar.open <= self.top_fvg and bar.close > self.top_fvg:
                level_ok = any(bar.close > lv for lv in self.broke_levels)
                ema_ok = ema is not None and bar.close > ema
                if level_ok and ema_ok:
                    return self._place_order(bar, engine)
                self.stage = self.WAIT_RETRACEMENT
        else:
            if bar.close > self.top_fvg:
                return False
            if bar.open >= self.bot_fvg and bar.close < self.bot_fvg:
                level_ok = any(bar.close < lv for lv in self.broke_levels)
                ema_ok = ema is not None and bar.close < ema
                if level_ok and ema_ok:
                    return self._place_order(bar, engine)
                self.stage = self.WAIT_RETRACEMENT
        return True

    def _place_order(self, bar: Bar, engine: "BacktestEngine") -> bool:
        entry = bar.close
        if self.direction == "long":
            sl = self.c1.low
            if sl >= entry:
                return False
            tp = entry + RISK_REWARD * (entry - sl)
        else:
            sl = self.c1.high
            if sl <= entry:
                return False
            tp = entry - RISK_REWARD * (sl - entry)

        self.trade = engine.open_trade(
            SimTrade(
                component=COMPONENT_FT,
                direction=self.direction,
                entry_dt=bar.date + M5,
                entry=entry,
                qty=engine.qty,
                tp=tp,
                sl=sl,
                slot_id=self.slot_id,
            )
        )
        self.stage = self.MONITORING   # MKT order: instant fill assumption
        return True


# ---------------------------------------------------------------------------
# One trading day
# ---------------------------------------------------------------------------
@dataclass
class _Day:
    d: date
    a: DayAnchors
    phase: str = "WAIT_LONDON_OPEN"
    vp1: Optional[Tuple[float, float, float]] = None   # (vah, poc, val)
    vp2: Optional[Tuple[float, float, float]] = None
    vp3: Optional[Tuple[float, float, float]] = None
    asia_bars: List[Bar] = field(default_factory=list)
    asia_high: Optional[float] = None
    asia_low: Optional[float] = None
    levels: Dict[str, float] = field(default_factory=dict)
    order1: Optional[SimTrade] = None
    order2: Optional[SimTrade] = None
    slots: List[SimSlot] = field(default_factory=list)


# ---------------------------------------------------------------------------
# Engine
# ---------------------------------------------------------------------------
class BacktestEngine:
    def __init__(self, m1: List[Bar], m5: List[Bar],
                 qty: float = 20000.0,
                 ambiguous: str = "sl",
                 cost_pips: float = 0.0):
        self.m1 = m1
        self.m1_t = [b.date for b in m1]
        self.m5 = m5
        self.qty = qty
        self.ambiguous = ambiguous
        self.cost_pips = cost_pips
        self.swap_tp_sl = False

        self.ema_trend = EMATracker(EMA_TREND)     # order1/order2 gate
        self.ema_main = EMATracker(EMA_MAIN)       # Full-Trading gate

        self.trades: List[SimTrade] = []
        self.open_trades: List[SimTrade] = []
        self.days_completed = 0
        self.days_skipped = 0

        self.day: Optional[_Day] = None
        self.prev_bar: Optional[Bar] = None

    # ---------------- public API ----------------
    def run(self, start: Optional[date] = None, end: Optional[date] = None) -> List[SimTrade]:
        for bar in self.m5:
            d = bar.date.date()
            if start and d < start:
                self.prev_bar = bar
                self.ema_trend.update(bar.close)
                self.ema_main.update(bar.close)
                continue
            if end and d > end and self.day is None:
                break

            # 1) resolve intrabar fills for the window of this M5 bar
            self._advance_fills(bar.date, bar.date + M5)

            # 2) EMAs see every closed bar (live: ema_trend always; ema_main
            #    continuously via daily warm-up — converged equivalent)
            self.ema_trend.update(bar.close)
            ema_main_val = self.ema_main.update(bar.close)

            # 3) day lifecycle + strategy logic
            if self.day is None:
                self._maybe_start_day(bar)

            if self.day is not None:
                self._on_bar(bar, ema_main_val)

            self.prev_bar = bar

        # dataset ended with a session open -> flatten at last close
        if self.day is not None and self.prev_bar is not None:
            self._shutoff(self.prev_bar, forced=True)

        return self.trades

    def open_trade(self, t: SimTrade) -> SimTrade:
        t.trade_date = self.day.d.isoformat() if self.day else ""
        if getattr(self, "swap_tp_sl", False):
            orig_tp = t.tp
            orig_sl = t.sl
            t.tp, t.sl = orig_sl, orig_tp
            t.direction = "short" if t.direction == "long" else "long"

        self.trades.append(t)
        self.open_trades.append(t)
        return t

    # ---------------- internals ----------------
    def _advance_fills(self, t0: datetime, t1: datetime) -> None:
        if not self.open_trades:
            return
        i0 = bisect_left(self.m1_t, t0)
        i1 = bisect_left(self.m1_t, t1)
        for m1bar in self.m1[i0:i1]:
            for tr in self.open_trades:
                if tr.is_open and m1bar.date >= tr.entry_dt:
                    _check_fill(tr, m1bar, self.ambiguous)
        self.open_trades = [t for t in self.open_trades if t.is_open]

    def _maybe_start_day(self, bar: Bar) -> None:
        d = bar.date.date()
        if d.weekday() >= 5:
            return
        a = anchors_for_date(d)
        if a.asia_open <= bar.date < a.asia_close:
            self.day = _Day(d=d, a=a)
            log.debug("Day %s started  (asia_open=%s shutoff=%s mid1=%s mid2=%s)",
                      d, a.asia_open, a.shutoff, a.mid1, a.mid2)

    def _vp(self, start_dt: datetime, end_dt: datetime,
            label: str) -> Optional[Tuple[float, float, float]]:
        i0 = bisect_left(self.m1_t, start_dt)
        i1 = bisect_left(self.m1_t, end_dt)
        tuples = [(b.date, b.open, b.high, b.low, b.close) for b in self.m1[i0:i1]]
        if len(tuples) < VP_MIN_BARS:
            log.warning("Day %s: %s has only %d M1 bars — profile skipped.",
                        self.day.d, label, len(tuples))
            return None
        res = bars_to_profile(tuples)
        if res is None:
            return None
        _c, _h, vah, poc, val = res
        return vah, poc, val

    def _on_bar(self, bar: Bar, ema_main_val: Optional[float]) -> None:
        day, a = self.day, self.day.a

        if a.asia_open <= bar.date <= a.asia_close:
            day.asia_bars.append(bar)

        ph = day.phase
        if ph == "WAIT_LONDON_OPEN":
            if bar.date >= a.london_open - M5:
                day.vp1 = self._vp(a.asia_open, a.london_open, "VP1")
                day.phase = "WAIT_ORDER1"

        elif ph == "WAIT_ORDER1":
            if (bar.date.hour, bar.date.minute) == a.mid1 and day.vp1 is not None:
                self._try_order1(bar)
                day.phase = "ORDER1_ACTIVE"
            if bar.date >= a.asia_close:
                self._asia_close(bar)

        elif ph == "ORDER1_ACTIVE":
            if bar.date >= a.asia_close:
                self._asia_close(bar)

        elif ph == "FULL_TRADING":
            self._ft_on_bar(bar, ema_main_val)
            if bar.date >= a.london_close:
                day.vp3 = self._vp(a.london_open, a.london_close + M5, "VP3")
                day.phase = "WAIT_ORDER2"

        elif ph == "WAIT_ORDER2":
            self._ft_on_bar(bar, ema_main_val)
            if ((bar.date.hour, bar.date.minute) == a.mid2
                    and day.vp2 is not None and day.vp3 is not None):
                self._try_order2(bar)
                day.phase = "ORDER2_ACTIVE"
            if bar.date >= a.shutoff:
                self._shutoff(bar)

        elif ph == "ORDER2_ACTIVE":
            self._ft_on_bar(bar, ema_main_val)
            if bar.date >= a.shutoff:
                self._shutoff(bar)

    # ---- fixed-time orders -------------------------------------------------
    def _ema_gate_ok(self, direction: str, close: float, which: str) -> bool:
        trend = self.ema_trend.value
        if trend is None:
            log.info("%s: EMA_TREND not ready — skipped.", which)
            return False
        if direction == "long" and not (close > trend):
            return False
        if direction == "short" and not (close < trend):
            return False
        return True

    def _try_order1(self, bar: Bar) -> None:
        close = bar.close
        poc = self.day.vp1[1]
        if close == poc:
            return
        direction = "short" if poc < close else "long"
        if not self._ema_gate_ok(direction, close, "Order1"):
            return
        self.day.order1 = self.open_trade(SimTrade(
            component=COMPONENT_ORDER1, direction=direction,
            entry_dt=bar.date + M5, entry=close, qty=self.qty,
            tp=poc, sl=None,
        ))

    def _try_order2(self, bar: Bar) -> None:
        close = bar.close
        poc2, poc3 = self.day.vp2[1], self.day.vp3[1]

        poc2_below = poc2 < close
        poc3_below = poc3 < close
        if poc2_below and poc3_below:
            direction, tp = "short", max(poc2, poc3)
        elif (not poc2_below) and (not poc3_below):
            direction, tp = "long", min(poc2, poc3)
        else:
            direction = "short" if poc2_below else "long"
            tp = poc2

        if not self._ema_gate_ok(direction, close, "Order2"):
            return
        self.day.order2 = self.open_trade(SimTrade(
            component=COMPONENT_ORDER2, direction=direction,
            entry_dt=bar.date + M5, entry=close, qty=self.qty,
            tp=tp, sl=None,
        ))

    # ---- asia close / full trading / shutoff -------------------------------
    def _asia_close(self, bar: Bar) -> None:
        day, a = self.day, self.day.a

        if day.order1 is not None and day.order1.is_open:
            day.order1.close(bar.date + M5, bar.close, "market_close")
            self.open_trades = [t for t in self.open_trades if t.is_open]

        day.vp2 = self._vp(a.asia_open, a.asia_close + M5, "VP2")

        if day.asia_bars:
            day.asia_high = max(b.high for b in day.asia_bars)
            day.asia_low = min(b.low for b in day.asia_bars)

        levels = {
            "AH": day.asia_high,
            "AL": day.asia_low,
            "VAH_2": day.vp2[0] if day.vp2 else None,
            "VAL_2": day.vp2[2] if day.vp2 else None,
        }
        day.levels = {k: v for k, v in levels.items() if v is not None}
        day.phase = "FULL_TRADING"
        if not day.levels:
            log.warning("Day %s: no levels — Full Trading idle.", day.d)

    def _ft_on_bar(self, bar: Bar, ema: Optional[float]) -> None:
        day = self.day
        if bar.date >= day.a.shutoff:      # engine end_dt guard (live behaviour)
            return

        alive: List[SimSlot] = []
        for slot in day.slots:
            if slot.on_bar(bar, ema, self):
                alive.append(slot)
        day.slots = alive

        if len(day.slots) >= MAX_SLOTS or not day.levels:
            return

        # parent scanner — identical break rules to FullTradingEngine
        is_green = bar.close > bar.open
        is_red = bar.close < bar.open
        broke_long = [lv for lv in day.levels.values()
                      if bar.open < lv < bar.close and is_green]
        broke_short = [lv for lv in day.levels.values()
                       if bar.open > lv > bar.close and is_red]

        c1 = self.prev_bar
        if broke_long and c1 is not None:
            day.slots.append(SimSlot(c1, bar, "long", broke_long))
        elif broke_short and c1 is not None:
            day.slots.append(SimSlot(c1, bar, "short", broke_short))

    def _shutoff(self, bar: Bar, forced: bool = False) -> None:
        day = self.day
        t = bar.date + M5
        px = bar.close
        reason = "shutoff" if not forced else "data_end"

        if day.order1 is not None and day.order1.is_open:
            day.order1.close(t, px, reason)
        if day.order2 is not None and day.order2.is_open:
            day.order2.close(t, px, reason)
        for slot in day.slots:
            if slot.trade is not None and slot.trade.is_open:
                slot.trade.close(t, px, reason)
        day.slots = []
        self.open_trades = [tr for tr in self.open_trades if tr.is_open]

        self.days_completed += 1
        self.day = None
