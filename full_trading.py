"""
full_trading.py — Slot-based "Full Trading" engine.

FullTradingEngine
├── Parent scanner  — runs on EVERY closed candle, detects level breaks,
│                     opens a new Slot if a free slot is available.
└── Slot[]          — each slot is an independent state machine:
                        FVG_DETECT → WAIT_RETRACEMENT → WAIT_EXIT_FVG →
                        ORDER_LAUNCHED → MONITORING → DONE
"""


import logging

from datetime import datetime
from typing import Optional

from dataclasses import dataclass

from ib_insync import IB, Contract, Order, Trade

from config import *


log = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Slot stages
# ---------------------------------------------------------------------------
class Stage:
    FVG_DETECT       = "FVG_DETECT"
    WAIT_RETRACEMENT = "WAIT_RETRACEMENT"
    WAIT_EXIT_FVG    = "WAIT_EXIT_FVG"
    ORDER_LAUNCHED   = "ORDER_LAUNCHED"
    MONITORING       = "MONITORING"
    DONE             = "DONE"


# ---------------------------------------------------------------------------
# Simple bar wrapper  (compatible with IB BarData and dicts)
# ---------------------------------------------------------------------------
@dataclass
class Bar:
    open:   float
    high:   float
    low:    float
    close:  float
    volume: float
    date:   datetime

    @staticmethod
    def from_ib(b) -> "Bar":
        from timezone_utils import _bar_dt
        return Bar(
            open=float(b.open),
            high=float(b.high),
            low=float(b.low),
            close=float(b.close),
            volume=float(b.volume) if b.volume else 0.0,
            date=_bar_dt(b),
        )


# ---------------------------------------------------------------------------
# EMA tracker
# ---------------------------------------------------------------------------
class EMATracker:
    """Exponential Moving Average calculator."""
    def __init__(self, period: int = EMA_MAIN):
        self.period  = period
        self.k       = 2.0 / (period + 1)
        self._ema:   Optional[float] = None
        self._buf:   list[float]     = []
        self._ready: bool            = False

    def update(self, close: float) -> Optional[float]:
        if self._ready:
            self._ema = close * self.k + self._ema * (1 - self.k)
            return self._ema
        self._buf.append(close)
        if len(self._buf) >= self.period:
            self._ema   = sum(self._buf) / len(self._buf)
            self._ready = True
        return self._ema

    @property
    def value(self) -> Optional[float]:
        return self._ema

    @property
    def ready(self) -> bool:
        return self._ready


# ---------------------------------------------------------------------------
# Slot
# ---------------------------------------------------------------------------
class Slot:
    _id_counter = 0

    def __init__(
        self,
        c1: Bar,
        c2: Bar,
        direction: str,
        broke_levels: list[float],
        state_mgr,
    ):
        Slot._id_counter += 1
        self.slot_id     = Slot._id_counter
        self.stage       = Stage.FVG_DETECT
        self.direction   = direction
        self.broke_levels = broke_levels

        self.c1 = c1
        self.c2 = c2

        self.bot_fvg: float = 0.0
        self.top_fvg: float = 0.0

        self.min_close_in_fvg = float("inf")
        self.min_low_in_fvg   = float("inf")
        self.max_close_in_fvg = float("-inf")
        self.max_high_in_fvg  = float("-inf")

        self.ib_order_id:  int   = 0
        self.entry_price:  float = 0.0
        self.sl_price:     float = 0.0
        self.tp_price:     float = 0.0
        self.bars_since_launch = 0

        self._state_mgr = state_mgr
        self._trade:  Optional[Trade] = None

        log.info(
            "[Slot %d] Created — direction=%s  levels=%s  C1=[%.5f,%.5f]  C2=%.5f",
            self.slot_id, direction, broke_levels, c1.low, c1.high, c2.close,
        )
        self._save()

    def on_bar(self, bar: Bar, ema: Optional[float], ib: IB, contract: Contract) -> bool:
        """
        Main entry: process one closed bar
        Returns True if slot is still alive, False if it should be removed.
        """
        if self.stage == Stage.FVG_DETECT:
            return self._stage_fvg_detect(bar)
        elif self.stage == Stage.WAIT_RETRACEMENT:
            return self._stage_wait_retracement(bar)
        elif self.stage == Stage.WAIT_EXIT_FVG:
            return self._stage_wait_exit_fvg(bar, ema, ib, contract)
        elif self.stage == Stage.ORDER_LAUNCHED:
            return self._stage_order_launched(bar, ema, ib, contract)
        elif self.stage == Stage.MONITORING:
            return self._stage_monitoring(ib)
        return False

    # ------------------------------------------------------------------
    # Stage Helpers
    # ------------------------------------------------------------------
    def _stage_fvg_detect(self, c3: Bar) -> bool:
        """
        FVG Detection (the bar passed in IS C3)
        """
        if self.direction == "long":
            if c3.low > self.c1.high:
                self.bot_fvg = self.c1.high
                self.top_fvg = c3.low
                log.info("[Slot %d] Long FVG found: [%.5f, %.5f]",
                         self.slot_id, self.bot_fvg, self.top_fvg)
                self.stage = Stage.WAIT_RETRACEMENT
                self._save()
                return True
        else:
            if c3.high < self.c1.low:
                self.bot_fvg = c3.high
                self.top_fvg = self.c1.low
                log.info("[Slot %d] Short FVG found: [%.5f, %.5f]",
                         self.slot_id, self.bot_fvg, self.top_fvg)
                self.stage = Stage.WAIT_RETRACEMENT
                self._save()
                return True

        log.info("[Slot %d] No FVG on C3 — discarding.", self.slot_id)
        return self._discard()

    def _stage_wait_retracement(self, bar: Bar) -> bool:
        """
        Wait for price to retrace into FVG (1st confirmation)
        """
        if self.direction == "long":
            if bar.close < self.bot_fvg:
                log.info("[Slot %d] Long: close below bot_fvg — discard.", self.slot_id)
                return self._discard()
            if bar.open > self.top_fvg and self.bot_fvg < bar.close < self.top_fvg:
                self._update_fvg_tracking_long(bar)
                log.info("[Slot %d] Long 1st confirmation.", self.slot_id)
                self.stage = Stage.WAIT_EXIT_FVG
                self._save()
                return True
            if self.bot_fvg <= bar.close <= self.top_fvg:
                self._update_fvg_tracking_long(bar)
        else:
            if bar.close > self.top_fvg:
                log.info("[Slot %d] Short: close above top_fvg — discard.", self.slot_id)
                return self._discard()
            if bar.open < self.bot_fvg and self.bot_fvg < bar.close < self.top_fvg:
                self._update_fvg_tracking_short(bar)
                log.info("[Slot %d] Short 1st confirmation.", self.slot_id)
                self.stage = Stage.WAIT_EXIT_FVG
                self._save()
                return True
            if self.bot_fvg <= bar.close <= self.top_fvg:
                self._update_fvg_tracking_short(bar)

        return True

    def _stage_wait_exit_fvg(self, bar: Bar, ema: Optional[float], ib: IB, contract: Contract) -> bool:
        """
        Wait for price to exit FVG on original side (2nd confirmation)
        """
        if self.direction == "long":
            if bar.close < self.bot_fvg:
                log.info("[Slot %d] Long: close below bot_fvg in stage3 — discard.", self.slot_id)
                return self._discard()

            if bar.open < self.top_fvg and bar.close > self.top_fvg:
                level_ok = any(bar.close > lv for lv in self.broke_levels)
                ema_ok   = ema is not None and bar.close > ema
                if level_ok and ema_ok:
                    log.info("[Slot %d] Long 2nd confirmation → placing order.", self.slot_id)
                    return self._place_order(bar, ib, contract)
                else:
                    log.info("[Slot %d] Long crosses FVG but fails level/EMA check — back to stage 2.", self.slot_id)
                    self.stage = Stage.WAIT_RETRACEMENT
                    self._save()
                    return True

            if self.bot_fvg <= bar.close <= self.top_fvg:
                self._update_fvg_tracking_long(bar)
        else:
            if bar.close > self.top_fvg:
                log.info("[Slot %d] Short: close above top_fvg in stage3 — discard.", self.slot_id)
                return self._discard()

            if bar.open > self.bot_fvg and bar.close < self.bot_fvg:
                level_ok = any(bar.close < lv for lv in self.broke_levels)
                ema_ok   = ema is not None and bar.close < ema
                if level_ok and ema_ok:
                    log.info("[Slot %d] Short 2nd confirmation → placing order.", self.slot_id)
                    return self._place_order(bar, ib, contract)
                else:
                    log.info("[Slot %d] Short crosses FVG but fails level/EMA check — back to stage 2.", self.slot_id)
                    self.stage = Stage.WAIT_RETRACEMENT
                    self._save()
                    return True

            if self.bot_fvg <= bar.close <= self.top_fvg:
                self._update_fvg_tracking_short(bar)

        return True

    def _place_order(self, bar: Bar, ib: IB, contract: Contract) -> bool:
        """
        Place bracket order
        """
        entry = bar.close

        if self.direction == "long":
            sl  = self.c1.low
            if sl >= entry:
                log.warning("[Slot %d] Long: SL >= entry — discard.", self.slot_id)
                return self._discard()
            tp = entry + RISK_REWARD * (entry - sl)
            action = "BUY"
        else:
            sl  = self.c1.high
            if sl <= entry:
                log.warning("[Slot %d] Short: SL <= entry — discard.", self.slot_id)
                return self._discard()
            tp = entry - RISK_REWARD * (sl - entry)
            action = "SELL"

        try:
            parent_order = Order()
            parent_order.action          = action
            parent_order.orderType       = "MKT"
            parent_order.totalQuantity   = TOTAL_CAPITAL * TRADE_QUANTITY
            parent_order.transmit        = False

            tp_order = Order()
            tp_order.action        = "SELL" if action == "BUY" else "BUY"
            tp_order.orderType     = "LMT"
            tp_order.lmtPrice      = round(tp, 5)
            tp_order.totalQuantity = parent_order.totalQuantity
            tp_order.transmit      = False

            sl_order = Order()
            sl_order.action        = tp_order.action
            sl_order.orderType     = "STP"
            sl_order.auxPrice      = round(sl, 5)
            sl_order.totalQuantity = parent_order.totalQuantity
            sl_order.transmit      = True

            parent_trade = ib.placeOrder(contract, parent_order)
            tp_order.parentId = parent_trade.order.orderId
            sl_order.parentId = parent_trade.order.orderId

            ib.placeOrder(contract, tp_order)
            ib.placeOrder(contract, sl_order)

            self.ib_order_id = parent_trade.order.orderId
            self._trade      = parent_trade
            self.entry_price = parent_trade.orderStatus.avgFillPrice if parent_trade.orderStatus.avgFillPrice > 0 else entry
            self.sl_price    = sl
            self.tp_price    = tp

            log.info(
                "[Slot %d] %s order placed  entry=%.5f  SL=%.5f  TP=%.5f  id=%d",
                self.slot_id, action, self.entry_price, sl, tp, self.ib_order_id,
            )
        except Exception as exc:
            log.error("[Slot %d] Order placement failed: %s", self.slot_id, exc)
            return self._discard()

        self.stage = Stage.ORDER_LAUNCHED
        self.bars_since_launch = 0
        self._save()
        return True

    def _stage_order_launched(self, bar: Bar, ema: Optional[float], ib: IB, contract: Contract) -> bool:
        """
        Order launched, waiting for fill
        """
        self.bars_since_launch += 1

        if self._trade and self._trade.orderStatus.status in ("Filled", "PreSubmitted"):
            log.info("[Slot %d] Order filled — entering MONITORING.", self.slot_id)
            self.stage = Stage.MONITORING
            self._save()
            return True

        return True

    def _stage_monitoring(self, ib: IB) -> bool:
        """
        Monitoring open position
        """
        if self._trade is None:
            return self._discard()

        status = self._trade.orderStatus.status
        if status in ("Inactive", "Cancelled", "ApiCancelled"):
            log.info("[Slot %d] Position closed (status=%s) — freeing slot.", self.slot_id, status)
            return self._discard()

        # Check if parent is fully done (TP or SL triggered closes child orders)
        positions = {p.contract.conId: p for p in ib.positions()}

        # If no remaining position for this contract, the trade resolved
        open_pos = any(
            abs(p.position) > 0
            for p in ib.positions()
            if p.contract.symbol == self._trade.contract.symbol
        )

        # Simple check: if the TP/SL child orders are inactive, position closed
        trades = ib.trades()
        for t in trades:
            if t.order.parentId == self.ib_order_id:
                if t.orderStatus.status in ("Filled",):
                    log.info("[Slot %d] Child order filled — position closed.", self.slot_id)
                    return self._discard()

        return True

    def force_close(self, ib: IB, contract: Contract) -> None:
        """
        Called at shut_off or end_time
        """
        if self.stage in (Stage.ORDER_LAUNCHED,):
            self._cancel_orders(ib)
        elif self.stage == Stage.MONITORING:
            self._close_at_market(ib, contract)
        self._discard()

    def _cancel_orders(self, ib: IB) -> None:
        if self._trade:
            try:
                ib.cancelOrder(self._trade.order)
                log.info("[Slot %d] Order cancelled.", self.slot_id)
            except Exception as exc:
                log.error("[Slot %d] Cancel failed: %s", self.slot_id, exc)

    def _close_at_market(self, ib: IB, contract: Contract) -> None:
        action = "SELL" if self.direction == "long" else "BUY"
        close_order = Order()
        close_order.action        = action
        close_order.orderType     = "MKT"
        close_order.totalQuantity = TOTAL_CAPITAL * TRADE_QUANTITY
        close_order.transmit      = True
        try:
            ib.placeOrder(contract, close_order)
            log.info("[Slot %d] Market close order placed.", self.slot_id)
        except Exception as exc:
            log.error("[Slot %d] Market close failed: %s", self.slot_id, exc)


    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------
    def _update_fvg_tracking_long(self, bar: Bar) -> None:
        if bar.close < self.min_close_in_fvg:
            self.min_close_in_fvg = bar.close
        if bar.low < self.min_low_in_fvg:
            self.min_low_in_fvg = bar.low

    def _update_fvg_tracking_short(self, bar: Bar) -> None:
        if bar.close > self.max_close_in_fvg:
            self.max_close_in_fvg = bar.close
        if bar.high > self.max_high_in_fvg:
            self.max_high_in_fvg = bar.high

    def _discard(self) -> bool:
        log.info("[Slot %d] Discarded (stage was %s).", self.slot_id, self.stage)
        self.stage = Stage.DONE
        if self._state_mgr:
            self._state_mgr.remove_slot(self.slot_id)
        return False

    def _save(self) -> None:
        if self._state_mgr is None:
            return
        d = {
            "slot_id":      self.slot_id,
            "stage":        self.stage,
            "direction":    self.direction,
            "broke_levels": self.broke_levels,
            "c1_high":      self.c1.high,
            "c1_low":       self.c1.low,
            "c2_open":      self.c2.open,
            "c2_close":     self.c2.close,
            "c2_bar_dt":    self.c2.date.isoformat(),
            "bot_fvg":      self.bot_fvg,
            "top_fvg":      self.top_fvg,
            "min_close_in_fvg":  self.min_close_in_fvg,
            "min_low_in_fvg":    self.min_low_in_fvg,
            "max_close_in_fvg":  self.max_close_in_fvg,
            "max_high_in_fvg":   self.max_high_in_fvg,
            "ib_order_id":   self.ib_order_id,
            "entry_price":   self.entry_price,
            "sl_price":      self.sl_price,
            "tp_price":      self.tp_price,
            "bars_since_launch": self.bars_since_launch,
        }
        self._state_mgr.upsert_slot(d)


# ---------------------------------------------------------------------------
# Full Trading Engine
# ---------------------------------------------------------------------------
class FullTradingEngine:
    """
    Manages the parent scanner and all slots for a Full Trading window.
    """
    def __init__(
        self,
        ib:           IB,
        contract:     Contract,
        state_mgr,
        levels:       dict,
        start_dt:     datetime,
        end_dt:       datetime,
        ema_period:   int  = EMA_MAIN,
        max_slots:    int  = MAX_SLOTS
    ):
        self.ib         = ib
        self.contract   = contract
        self.state_mgr  = state_mgr
        self.levels     = levels
        self.start_dt   = start_dt
        self.end_dt     = end_dt
        self.max_slots  = max_slots

        self._ema       = EMATracker(ema_period)
        self._slots:    list[Slot] = []
        self._bar_history: list[Bar] = []
        self._running   = False
        self._started   = False

        log.info("FullTradingEngine created: start=%s  end=%s  levels=%s", start_dt, end_dt, levels)

    def on_bar(self, bar: Bar) -> None:
        """
        Main entry: process one closed bar.
        """
        if bar.date >= self.end_dt:
            log.info("FullTradingEngine: past end_dt — ignoring bar.")
            return

        ema_value = self._ema.update(bar.close)

        self._bar_history.append(bar)
        if len(self._bar_history) > 500:
            self._bar_history.pop(0)

        if bar.date < self.start_dt:
            return

        self._started = True

        alive = []
        for slot in self._slots:
            still_alive = slot.on_bar(bar, ema_value, self.ib, self.contract)
            if still_alive:
                alive.append(slot)
        self._slots = alive

        if len(self._slots) < self.max_slots:
            self._scan_for_break(bar, ema_value)


    # ------------------------------------------------------------------
    # Parent scanner
    # ------------------------------------------------------------------
    def _scan_for_break(self, bar: Bar, ema: Optional[float]) -> None:
        level_values = list(self.levels.values())
        is_green = bar.close > bar.open
        is_red   = bar.close < bar.open

        broke_long  = []
        broke_short = []

        for lv in level_values:
            if lv is None:
                continue
            if bar.open < lv < bar.close and is_green:
                broke_long.append(lv)
            if bar.open > lv > bar.close and is_red:
                broke_short.append(lv)

        if broke_long:
            c1 = self._get_c1(bar)
            if c1:
                log.info("Break detected: LONG  levels=%s  bar=%s", broke_long, bar.date)
                slot = Slot(c1, bar, "long", broke_long, self.state_mgr)
                self._slots.append(slot)
        elif broke_short:
            c1 = self._get_c1(bar)
            if c1:
                log.info("Break detected: SHORT  levels=%s  bar=%s", broke_short, bar.date)
                slot = Slot(c1, bar, "short", broke_short, self.state_mgr)
                self._slots.append(slot)

    def _get_c1(self, c2: Bar) -> Optional[Bar]:
        """Return the bar immediately before c2 from history."""
        for i in range(len(self._bar_history) - 1, -1, -1):
            if self._bar_history[i].date < c2.date:
                return self._bar_history[i]
        log.warning("No C1 found for C2 at %s", c2.date)
        return None


    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------
    def close_all_market(self) -> None:
        """
        Force-close all open positions (shut_off, end_time, manual stop)
        """
        log.info("FullTradingEngine: closing all %d slots at market.", len(self._slots))
        for slot in self._slots:
            slot.force_close(self.ib, self.contract)
        self._slots = []

    def restore_slots(self, slot_dicts: list[dict]) -> None:
        """
        Restore slots from persisted state (used after crash recovery)
        """
        for sd in slot_dicts:
            try:
                c1 = Bar(open=0, high=sd["c1_high"], low=sd["c1_low"],
                         close=0, volume=0,
                         date=datetime.fromisoformat(sd["c2_bar_dt"]))
                c2 = Bar(open=sd["c2_open"], high=0, low=0,
                         close=sd["c2_close"], volume=0,
                         date=datetime.fromisoformat(sd["c2_bar_dt"]))
                slot = Slot(c1, c2, sd["direction"], sd["broke_levels"], self.state_mgr)
                slot.slot_id   = sd["slot_id"]
                slot.stage     = sd["stage"]
                slot.bot_fvg   = sd["bot_fvg"]
                slot.top_fvg   = sd["top_fvg"]
                slot.min_close_in_fvg = sd.get("min_close_in_fvg", float("inf"))
                slot.min_low_in_fvg   = sd.get("min_low_in_fvg",   float("inf"))
                slot.max_close_in_fvg = sd.get("max_close_in_fvg", float("-inf"))
                slot.max_high_in_fvg  = sd.get("max_high_in_fvg",  float("-inf"))
                slot.ib_order_id      = sd.get("ib_order_id", 0)
                slot.entry_price      = sd.get("entry_price", 0.0)
                slot.sl_price         = sd.get("sl_price", 0.0)
                slot.tp_price         = sd.get("tp_price", 0.0)
                slot.bars_since_launch= sd.get("bars_since_launch", 0)
                self._slots.append(slot)
                log.info("Restored slot %d at stage %s", slot.slot_id, slot.stage)
            except Exception as exc:
                log.error("Failed to restore slot %s: %s", sd, exc)
