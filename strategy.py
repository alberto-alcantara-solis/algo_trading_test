"""
strategy.py — Daily strategy orchestrator.

Full cycle:
  WAIT_ASIA_OPEN
  WAIT_LONDON_OPEN          (wait, then compute VP1)
  WAIT_ORDER1               (wait for mid candle between london_open and asia_close)
  ORDER1_ACTIVE             (monitor order1, wait for asia_close)
  WAIT_ASIA_CLOSE           (compute VP2, mark AH/AL, close order1 if open)
  FULL_TRADING              (asia_close shut_off, with sub-phase for london_close VP3)
  WAIT_ORDER2               (wait for mid candle between london_close and shutoff)
  ORDER2_ACTIVE             (monitor order2, wait for shutoff)
  WAIT_SHUTOFF              (all orders closed)
  DONE                      (reset state, reset day, wait for next asia_open)

The orchestrator is called with every closed bar and determines what to do
based on the bar's timestamp versus the session boundaries.
"""


import logging
from datetime import datetime, timedelta, date, time
from typing import Optional, Tuple

from dataclasses import asdict

from ib_insync import IB, Contract, Order

from full_trading import Bar, EMATracker, FullTradingEngine
from state_manager import OrderRecord, StateManager
from timezone_utils import SessionBoundariesCandles

from timezone_utils import compute_boundaries
from volume_profile import compute_volume_profile
from sizing import compute_order_quantity

from config import *


log = logging.getLogger(__name__)

PHASE_WAIT_ASIA_OPEN       = "WAIT_ASIA_OPEN"
PHASE_WAIT_LONDON_OPEN     = "WAIT_LONDON_OPEN"
PHASE_WAIT_ORDER1          = "WAIT_ORDER1"
PHASE_ORDER1_ACTIVE        = "ORDER1_ACTIVE"
PHASE_WAIT_ASIA_CLOSE      = "WAIT_ASIA_CLOSE"
PHASE_FULL_TRADING         = "FULL_TRADING"
PHASE_WAIT_ORDER2          = "WAIT_ORDER2"
PHASE_ORDER2_ACTIVE        = "ORDER2_ACTIVE"
PHASE_WAIT_SHUTOFF         = "WAIT_SHUTOFF"
PHASE_DONE                 = "DONE"


class DailyStrategy:
    """
    One instance per trading day.  Receives every closed 5-min bar via `on_bar`.
    """
    def __init__(self, ib: IB, contract: Contract, state_mgr: StateManager):
        self.ib         = ib
        self.contract   = contract
        self.sm         = state_mgr

        self.sb: Optional[SessionBoundariesCandles] = SessionBoundariesCandles()

        self.vah1 = self.poc1 = self.val1 = None
        self.vah2 = self.poc2 = self.val2 = None
        self.vah3 = self.poc3 = self.val3 = None
        self.asia_high = self.asia_low = None

        self.asia_open_dt:    Optional[datetime] = None
        self.london_open_dt:  Optional[datetime] = None
        self.asia_close_dt:   Optional[datetime] = None
        self.london_close_dt: Optional[datetime] = None
        self.shutoff_dt:      Optional[datetime] = None

        self._ft_engine: Optional[FullTradingEngine] = None

        self._ema_trend = EMATracker(EMA_TREND)
        self._trend_last_dt: Optional[datetime] = None

        self._order1_ib_id: int = 0
        self._order2_ib_id: int = 0

        self._asia_bars: list[Bar] = []

    # ------------------------------------------------------------------
    # Bootstrap from persisted state (crash recovery / manual restart)
    # ------------------------------------------------------------------
    def bootstrap(self) -> None:
        """
        Called once at startup BEFORE the first on_bar call.
        Restores all in-memory state from the StateManager so the bot can resume mid-strategy.
        """
        st = self.sm.state
        phase = st.phase
        log.info("Bootstrapping from phase=%s  date=%s", phase, st.trade_date)

        if st.time_boundaries:
            self.sb = _dict_to_sb(st.time_boundaries)
        else:
            self.sb = compute_boundaries()
            self.sm.set_time_boundaries(asdict(self.sb))

        trade_date = None
        if st.trade_date:
            trade_date = date.fromisoformat(st.trade_date)
            self._set_datetime_anchors(self.sb, trade_date)
        else:
            self._set_datetime_anchors(self.sb)

        if st.vp1:
            self.vah1, self.poc1, self.val1 = st.vp1["vah"], st.vp1["poc"], st.vp1["val"]
        if st.vp2:
            self.vah2, self.poc2, self.val2 = st.vp2["vah"], st.vp2["poc"], st.vp2["val"]
        if st.vp3:
            self.vah3, self.poc3, self.val3 = st.vp3["vah"], st.vp3["poc"], st.vp3["val"]

        if st.asia_high is not None:
            self.asia_high = st.asia_high
            self.asia_low  = st.asia_low

        if st.order1 and st.order1.get("ib_order_id"):
            self._order1_ib_id = st.order1["ib_order_id"]
            log.info("Restored order1 ID: %d", self._order1_ib_id)
        if st.order2 and st.order2.get("ib_order_id"):
            self._order2_ib_id = st.order2["ib_order_id"]
            log.info("Restored order2 ID: %d", self._order2_ib_id)

        if st.asia_bars:
            self._asia_bars = [
                Bar(
                    open=b["open"],
                    high=b["high"],
                    low=b["low"],
                    close=b["close"],
                    volume=b["volume"],
                    date=datetime.fromisoformat(b["date"]),
                )
                for b in st.asia_bars
            ]
            log.info("Restored %d Asia bars from state.", len(self._asia_bars))
        
        if phase in (PHASE_FULL_TRADING, PHASE_WAIT_ORDER2, PHASE_ORDER2_ACTIVE, PHASE_WAIT_SHUTOFF):
            if not self._asia_bars:
                log.info("Phase is %s but no Asia bars in state. Fetching historical bars...", phase)
                fetched = self._fetch_historical_asia_bars()
                if fetched:
                    self._asia_bars = fetched
                    self.sm.set_asia_bars(_bars_to_dicts(fetched))
                    if self.asia_high is None:
                        self.asia_high = max(b.high for b in fetched)
                        self.asia_low  = min(b.low  for b in fetched)
                        self.sm.set_asia_range(self.asia_high, self.asia_low)
                        log.info("Recalculated Asia range: high=%.5f  low=%.5f",
                                 self.asia_high, self.asia_low)

        warm = self._fetch_recent_bars()
        for b in warm:
            self._ema_trend.update(b.close)
            self._trend_last_dt = b.date
        if warm:
            log.info("EMA_TREND warmed with %d bars (value=%.5f, ready=%s)",
                     len(warm), self._ema_trend.value, self._ema_trend.ready)

        if phase in (PHASE_FULL_TRADING, PHASE_WAIT_ORDER2, PHASE_ORDER2_ACTIVE, PHASE_WAIT_SHUTOFF):
            self._create_full_trading_engine()
            if self._ft_engine and st.slots:
                self._ft_engine.restore_slots(st.slots)

        log.info("Bootstrap complete.  phase=%s", phase)

    # ------------------------------------------------------------------
    # Main entry point: call on every closed 5-min bar
    # ------------------------------------------------------------------
    def on_bar(self, bar: Bar) -> None:
        phase = self.sm.state.phase

        if phase == PHASE_DONE:
            self._reset_and_restart()
            return

        if self.asia_open_dt and self.asia_close_dt:
            if self.asia_open_dt <= bar.date <= self.asia_close_dt:
                self._asia_bars.append(bar)

        if self._trend_last_dt is None or bar.date > self._trend_last_dt:
            self._ema_trend.update(bar.close)
            self._trend_last_dt = bar.date

        dispatch = {
            PHASE_WAIT_ASIA_OPEN:   self._ph_wait_asia_open,
            PHASE_WAIT_LONDON_OPEN: self._ph_wait_london_open,
            PHASE_WAIT_ORDER1:      self._ph_wait_order1,
            PHASE_ORDER1_ACTIVE:    self._ph_order1_active,
            PHASE_WAIT_ASIA_CLOSE:  self._ph_wait_asia_close,
            PHASE_FULL_TRADING:     self._ph_full_trading,
            PHASE_WAIT_ORDER2:      self._ph_wait_order2,
            PHASE_ORDER2_ACTIVE:    self._ph_order2_active,
            PHASE_WAIT_SHUTOFF:     self._ph_wait_shutoff,
        }
        handler = dispatch.get(phase)
        if handler:
            handler(bar)


    # ====================================================================
    # Phase handlers
    # ====================================================================
    def _ph_wait_asia_open(self, bar: Bar) -> None:
        """
        Compute today's session boundaries the first time we see a bar
        """
        if self.sb is None:
            self.sb = compute_boundaries()
            self.sm.set_time_boundaries(asdict(self.sb))
            self._set_datetime_anchors(self.sb)

        if bar.date >= self.asia_open_dt:
            log.info("Asia opened at %s", bar.date)
            self.sm.set_phase(PHASE_WAIT_LONDON_OPEN)

    def _ph_wait_london_open(self, bar: Bar) -> None:
        """
        Collect Asia bars, then compute VP1
        """
        if bar.date >= self.london_open_dt-timedelta(minutes=5):
            log.info("London opened — computing VP1 (Asia session VP)")
            vp1 = compute_volume_profile(
                self.ib, self.contract,
                start_dt=self.asia_open_dt,
                end_dt=self.london_open_dt,
                label="VP1",
            )
            if vp1:
                self.vah1, self.poc1, self.val1 = vp1.vah, vp1.poc, vp1.val
                self.sm.set_vp(1, vp1.vah, vp1.poc, vp1.val)
            self.sm.set_phase(PHASE_WAIT_ORDER1)

    def _ph_wait_order1(self, bar: Bar) -> None:
        """
        Wait for the middle candle between london_open and asia_close
        """
        if self.sm.state.order1_placed:
            self.sm.set_phase(PHASE_ORDER1_ACTIVE)
            return

        bar_open = bar.date
        if (bar_open.hour == self.sb.mid_london_asia_open[0] and bar_open.minute == self.sb.mid_london_asia_open[1] and self.poc1 is not None):
            if self._bar_is_fresh(bar):
                log.info("Order1 trigger candle closed at %s  close=%.5f  poc1=%.5f", bar.date, bar.close, self.poc1)
                self._place_order1(bar)
            else:
                log.warning("Order1 trigger candle %s is stale (replayed after a restart) — skipping order1 today.", bar.date)
            self.sm.set_phase(PHASE_ORDER1_ACTIVE)
        
        if bar.date >= self.asia_close_dt:
            self._handle_asia_close(bar)

    def _ph_order1_active(self, bar: Bar) -> None:
        """
        Monitor order1, wait for asia_close.
        """
        if not self.sm.state.order1_closed:
            self._check_order1_tp(bar)
        
        if bar.date >= self.asia_close_dt:
            self._handle_asia_close(bar)

    def _ph_wait_asia_close(self, bar: Bar) -> None:
        """
        Handle asia_close events
        """
        if bar.date >= self.asia_close_dt:
            self._handle_asia_close(bar)

    def _ph_full_trading(self, bar: Bar) -> None:
        """
        Route each bar to the engine; watch for london_close
        """
        if self._ft_engine is None:
            self._create_full_trading_engine()

        self._ft_engine.on_bar(bar)

        if bar.date >= self.london_close_dt:
            log.info("London closed — computing VP3")
            vp3 = compute_volume_profile(
                self.ib, self.contract,
                start_dt=self.london_open_dt,
                end_dt=self.london_close_dt + timedelta(minutes=5),
                label="VP3",
            )
            if vp3:
                self.vah3, self.poc3, self.val3 = vp3.vah, vp3.poc, vp3.val
                self.sm.set_vp(3, vp3.vah, vp3.poc, vp3.val)
            self.sm.set_phase(PHASE_WAIT_ORDER2)

    def _ph_wait_order2(self, bar: Bar) -> None:
        """
        Keep Full Trading engine running, wait for middle candle between london_close and shut_off
        """
        if self._ft_engine:
            self._ft_engine.on_bar(bar)

        if self.sm.state.order2_placed:
            self.sm.set_phase(PHASE_ORDER2_ACTIVE)
            return

        bar_open = bar.date
        if (bar_open.hour == self.sb.mid_london_close_shutoff_open[0] and bar_open.minute == self.sb.mid_london_close_shutoff_open[1] and self.poc2 is not None and self.poc3 is not None):
            if self._bar_is_fresh(bar):
                log.info("Order2 trigger candle closed at %s  close=%.5f", bar.date, bar.close)
                self._place_order2(bar)
            else:
                log.warning("Order2 trigger candle %s is stale (replayed after a restart) — skipping order2 today.", bar.date)
            self.sm.set_phase(PHASE_ORDER2_ACTIVE)

        if bar.date >= self.shutoff_dt:
            self._handle_shutoff()

    def _ph_order2_active(self, bar: Bar) -> None:
        """
        Keep Full Trading engine running, monitor order2, wait for shutoff.
        """
        if self._ft_engine:
            self._ft_engine.on_bar(bar)

        if not self.sm.state.order2_closed:
            self._check_order2_tp(bar)

        if bar.date >= self.shutoff_dt:
            self._handle_shutoff()

    def _ph_wait_shutoff(self, bar: Bar) -> None:
        """
        Keep Full Trading engine running, wait for shutoff.
        """
        if self._ft_engine:
            self._ft_engine.on_bar(bar)

        if bar.date >= self.shutoff_dt:
            self._handle_shutoff()


    # ====================================================================
    # Event handlers
    # ====================================================================
    def _handle_asia_close(self, bar: Bar) -> None:
        """Actions at asia_close: close order1 if open, compute VP2, mark AH/AL."""
        log.info("Asia close triggered at %s", bar.date)

        if self.sm.state.order1_placed and not self.sm.state.order1_closed:
            log.info("Closing order1 at market (asia_close)")
            self._market_close_order(self._order1_ib_id)
            self.sm.mark_order1_closed("market")

        vp2 = compute_volume_profile(
            self.ib, self.contract,
            start_dt=self.asia_open_dt,
            end_dt=self.asia_close_dt + timedelta(minutes=5),
            label="VP2",
        )
        if vp2:
            self.vah2, self.poc2, self.val2 = vp2.vah, vp2.poc, vp2.val
            self.sm.set_vp(2, vp2.vah, vp2.poc, vp2.val)

        fetched = self._fetch_historical_asia_bars()
        if fetched:
            self._asia_bars = fetched

        if self._asia_bars:
            self.asia_high = max(b.high for b in self._asia_bars)
            self.asia_low  = min(b.low  for b in self._asia_bars)
            self.sm.set_asia_range(self.asia_high, self.asia_low)

            self.sm.set_asia_bars(_bars_to_dicts(self._asia_bars))
            log.info("Asia range: high=%.5f  low=%.5f  (saved %d bars)", self.asia_high, self.asia_low, len(self._asia_bars))

        self._create_full_trading_engine()
        self.sm.set_phase(PHASE_FULL_TRADING)

    def _handle_shutoff(self) -> None:
        """Actions at shut_off: close all orders at market, wait for new day."""
        log.info("Shut-off triggered.")

        if self.sm.state.order2_placed and not self.sm.state.order2_closed:
            self._market_close_order(self._order2_ib_id)
            self.sm.mark_order2_closed("market")

        if self._ft_engine:
            self._ft_engine.close_all_market()

        self.sm.set_phase(PHASE_DONE)
        log.info("All orders closed.  Waiting for next trading day.")

    def _reset_and_restart(self) -> None:
        """Reset all in-memory strategy state and restart from WAIT_ASIA_OPEN."""
        log.info("Resetting strategy after DONE.")

        self.sb = None
        self.vah1 = self.poc1 = self.val1 = None
        self.vah2 = self.poc2 = self.val2 = None
        self.vah3 = self.poc3 = self.val3 = None
        self.asia_high = self.asia_low = None

        self.asia_open_dt = None
        self.london_open_dt = None
        self.asia_close_dt = None
        self.london_close_dt = None
        self.shutoff_dt = None

        self._ft_engine = None
        self._order1_ib_id = 0
        self._order2_ib_id = 0
        self._asia_bars = []

        try:
            trade_date = datetime.now(REF_TZ).date()
            self.sm.reset_for_new_day(trade_date)
        except Exception as exc:
            log.error("Failed to reset persistent state: %s", exc)
            self.sm.set_phase(PHASE_WAIT_ASIA_OPEN)
        


    # ====================================================================
    # Order placement
    # ====================================================================
    def _place_order1(self, bar: Bar) -> None:
        """
        Place the first fixed-time order (mid of london_open and asia_close).
        Direction is toward POC_1 from current close.  TP at POC_1.  No SL.
        Managed by bot (bot monitors price and closes if needed at asia_close).
        """
        close = bar.close
        poc   = self.poc1

        if poc is None:
            log.warning("Order1: poc1 is None — skipping.")
            return

        if close == poc:
            log.warning("Order1: close == poc1 — no edge, skipping.")
            return

        if poc < close:
            direction = "short"
            action    = "SELL"
        else:
            direction = "long"
            action    = "BUY"

        trend = self._ema_trend.value
        if trend is None:
            log.warning("Order1: EMA_TREND not ready — skipping order1.")
            return
        if direction == "long" and not (close > trend):
            log.info("Order1: long blocked by EMA_TREND (close=%.5f <= ema=%.5f).", close, trend)
            return
        if direction == "short" and not (close < trend):
            log.info("Order1: short blocked by EMA_TREND (close=%.5f >= ema=%.5f).", close, trend)
            return

        qty = compute_order_quantity(self.ib, self.contract)
        if qty <= 0:
            log.error("Order1: sizing returned 0 — skipping.")
            return

        log.info("Order1: %s  entry=%.5f  TP=%.5f  qty=%d", direction, close, poc, qty)

        parent = Order()
        parent.action        = action
        parent.orderType     = "MKT"
        parent.totalQuantity = qty
        parent.transmit      = False

        tp = Order()
        tp.action        = "SELL" if action == "BUY" else "BUY"
        tp.orderType     = "LMT"
        tp.lmtPrice      = round(poc, 5)
        tp.totalQuantity = qty
        tp.transmit      = True

        try:
            parent_trade  = self.ib.placeOrder(self.contract, parent)
            real_entry_price = close
            tp.parentId   = parent_trade.order.orderId
            self.ib.placeOrder(self.contract, tp)
            self._order1_ib_id = parent_trade.order.orderId

            rec = OrderRecord(
                ib_order_id    = self._order1_ib_id,
                direction      = direction,
                entry_price    = real_entry_price,
                tp_price       = poc,
                sl_price       = 0.0,
                total_quantity = qty,
                is_open        = True,
                has_sl         = False,
            )
            self.sm.record_order1(rec)
            log.info("Order1 placed: id=%d", self._order1_ib_id)
        except Exception as exc:
            log.error("Order1 placement failed: %s", exc)

    def _place_order2(self, bar: Bar) -> None:
        """
        Place the second fixed-time order (mid of london_close and shut_off).
        Direction depends on relative position of poc2 and poc3.
        TP at closest POC.  No SL.
        """
        close = bar.close
        poc2  = self.poc2
        poc3  = self.poc3

        if poc2 is None or poc3 is None:
            log.error("Order2: poc2 or poc3 is None — skipping.")
            return

        poc2_below = poc2 < close
        poc3_below = poc3 < close
        if poc2_below and poc3_below:
            direction = "short"
            tp = max(poc2, poc3)
        elif (not poc2_below) and (not poc3_below):
            direction = "long"
            tp = min(poc2, poc3)
        else:
            direction = "short" if poc2_below else "long"
            tp = poc2

        action = "SELL" if direction == "short" else "BUY"

        trend = self._ema_trend.value
        if trend is None:
            log.warning("Order2: EMA_TREND not ready — skipping order2.")
            return
        if direction == "long" and not (close > trend):
            log.info("Order2: long blocked by EMA_TREND (close=%.5f <= ema=%.5f).", close, trend)
            return
        if direction == "short" and not (close < trend):
            log.info("Order2: short blocked by EMA_TREND (close=%.5f >= ema=%.5f).", close, trend)
            return

        qty = compute_order_quantity(self.ib, self.contract)
        if qty <= 0:
            log.error("Order2: sizing returned 0 — skipping.")
            return

        log.info("Order2: %s  entry=%.5f  TP=%.5f  qty=%d", direction, close, tp, qty)

        parent = Order()
        parent.action        = action
        parent.orderType     = "MKT"
        parent.totalQuantity = qty
        parent.transmit      = False

        tp_order = Order()
        tp_order.action        = "SELL" if action == "BUY" else "BUY"
        tp_order.orderType     = "LMT"
        tp_order.lmtPrice      = round(tp, 5)
        tp_order.totalQuantity = qty
        tp_order.transmit      = True

        try:
            parent_trade      = self.ib.placeOrder(self.contract, parent)
            real_entry_price = close
            tp_order.parentId = parent_trade.order.orderId
            self.ib.placeOrder(self.contract, tp_order)
            self._order2_ib_id = parent_trade.order.orderId

            rec = OrderRecord(
                ib_order_id    = self._order2_ib_id,
                direction      = direction,
                entry_price    = real_entry_price,
                tp_price       = tp,
                sl_price       = 0.0,
                total_quantity = qty,
                is_open        = True,
                has_sl         = False,
            )
            self.sm.record_order2(rec)
            log.info("Order2 placed: id=%d", self._order2_ib_id)
        except Exception as exc:
            log.error("Order2 placement failed: %s", exc)


    # ====================================================================
    # Order monitoring helpers
    # ====================================================================
    def _check_order1_tp(self, bar: Bar) -> None:
        """Check if order1's TP has been hit via IB trade status."""
        if self._order1_ib_id == 0:
            return
        for trade in self.ib.trades():
            if (trade.order.parentId == self._order1_ib_id and
                    trade.orderStatus.status == "Filled"):
                log.info("Order1 TP filled.")
                self.sm.mark_order1_closed("tp")
                self.sm.set_phase(PHASE_WAIT_ASIA_CLOSE)
                return

    def _check_order2_tp(self, bar: Bar) -> None:
        """Check if order2's TP has been hit via IB trade status."""
        if self._order2_ib_id == 0:
            return
        for trade in self.ib.trades():
            if (trade.order.parentId == self._order2_ib_id and
                    trade.orderStatus.status == "Filled"):
                log.info("Order2 TP filled.")
                self.sm.mark_order2_closed("tp")
                self.sm.set_phase(PHASE_WAIT_SHUTOFF)
                return

    def _get_order_record(self, parent_id: int) -> Optional[dict]:
        for order_record in (self.sm.state.order1, self.sm.state.order2):
            if order_record and order_record.get("ib_order_id") == parent_id:
                return order_record
        return None

    def _market_close_order(self, parent_id: int) -> None:
        """
        Close ONE order's position:
        Cancel its bracket children, then flatten exactly the quantity that order bought/sold, in the opposite of the order's OWN direction.
        """
        if parent_id <= 0:
            log.warning("Market-close requested with no order id — skipping.")
            return

        for trade in self.ib.trades():
            if trade.order.parentId == parent_id and trade.orderStatus.status == "Filled":
                log.info("Market-close: TP child of order %d already filled — nothing to close.", parent_id)
                return

        _DONE = ("Filled", "Cancelled", "ApiCancelled", "Inactive")
        for trade in self.ib.trades():
            if trade.order.parentId == parent_id and trade.orderStatus.status not in _DONE:
                try:
                    self.ib.cancelOrder(trade.order)
                except Exception:
                    pass

        order_record = self._get_order_record(parent_id)
        if not order_record:
            log.critical("Market-close: no order record for id=%d — cannot determine "
                         "quantity/direction; NOT flattening the whole symbol. "
                         "Close manually in TWS if a position remains.", parent_id)
            return

        direction = order_record.get("direction", "")
        qty = int(order_record.get("total_quantity", 0) or 0)

        for trade in self.ib.trades():
            if trade.order.orderId == parent_id:
                try:
                    filled = int(abs(trade.orderStatus.filled or 0))
                except (TypeError, ValueError):
                    filled = 0
                if filled > 0:
                    qty = min(qty, filled) if qty > 0 else filled
                break

        if qty <= 0 or direction not in ("long", "short"):
            log.error("Market-close: unusable record for id=%d (qty=%s, direction=%r) — skipping.",
                      parent_id, qty, direction)
            return

        action = "SELL" if direction == "long" else "BUY"
        flat = Order()
        flat.action        = action
        flat.orderType     = "MKT"
        flat.totalQuantity = qty
        flat.transmit      = True
        try:
            self.ib.placeOrder(self.contract, flat)
            log.info("Market-close placed for order %d (action=%s qty=%d)", parent_id, action, qty)
        except Exception as exc:
            log.error("Market close failed: %s", exc)


    # ====================================================================
    # Helpers
    # ====================================================================
    def _create_full_trading_engine(self) -> None:
        if self._ft_engine is not None:
            return

        levels = {
            "AH":    self.asia_high,
            "AL":    self.asia_low,
            "VAH_2": self.vah2,
            "VAL_2": self.val2,
        }
        levels = {k: v for k, v in levels.items() if v is not None}

        if not levels:
            log.warning("No levels available for Full-Trading engine.")
            return

        self._ft_engine = FullTradingEngine(
            ib          = self.ib,
            contract    = self.contract,
            state_mgr   = self.sm,
            levels      = levels,
            start_dt    = self.asia_close_dt,
            end_dt      = self.shutoff_dt,
        )
        log.info("FullTradingEngine created with levels: %s", levels)

        warm = self._fetch_recent_bars()
        if warm:
            self._ft_engine.warmup(warm)

    def _set_datetime_anchors(self, sb: SessionBoundariesCandles, trade_date: Optional[date] = None) -> None:
        """
        Convert session boundary hour/minute tuples to actual datetime objects relative to the trading day.
        Uses REF_TZ for the reference date, and accepts the persisted trade_date when available.
        """
        if trade_date is None:
            trade_date = datetime.now(REF_TZ).date()

        reference_date = datetime.combine(trade_date, time.min, tzinfo=REF_TZ)
        control_offset = 0
        
        def _to_datetime(hour: int, minute: int, day_offset: int = 0) -> datetime:
            dt = reference_date + timedelta(days=day_offset)
            return dt.replace(hour=hour, minute=minute, second=0, microsecond=0)
        
        def _handle_cross_day(current_hour: int, hour: int, minute: int, day_offset: int) -> Tuple[datetime, int]:
            if day_offset == 0:
                if hour < current_hour:
                    day_offset = 1
                elif hour == current_hour and minute < current_minute:
                    day_offset = 1
            return _to_datetime(hour, minute, day_offset), day_offset
        
        asia_open_dt = _to_datetime(sb.asia_open[0], sb.asia_open[1], 0)
        current_hour = asia_open_dt.hour
        current_minute = asia_open_dt.minute

        london_open_dt, result_offset = _handle_cross_day(current_hour, sb.london_open[0], sb.london_open[1], control_offset)
        if result_offset == 0:
            current_hour, current_minute = london_open_dt.hour, london_open_dt.minute
        else:
            control_offset = result_offset
        
        asia_close_dt, result_offset = _handle_cross_day(current_hour, sb.asia_close[0], sb.asia_close[1], control_offset)
        if result_offset == 0:
            current_hour, current_minute = asia_close_dt.hour, asia_close_dt.minute
        else:
            control_offset = result_offset

        # From here on, dates might be on the next day.
        
        london_close_dt, result_offset = _handle_cross_day(current_hour, sb.london_close[0], sb.london_close[1], control_offset)
        if result_offset == 0:
            current_hour, current_minute = london_close_dt.hour, london_close_dt.minute
        else:
            control_offset = result_offset
        
        shut_off_dt, result_offset = _handle_cross_day(current_hour, sb.shut_off[0], sb.shut_off[1], control_offset)
        if result_offset == 0:
            current_hour, current_minute = shut_off_dt.hour, shut_off_dt.minute
        else:
            control_offset = result_offset
        
        self.asia_open_dt = asia_open_dt
        self.london_open_dt = london_open_dt
        self.asia_close_dt = asia_close_dt
        self.london_close_dt = london_close_dt
        self.shutoff_dt = shut_off_dt

        return

    def _fetch_recent_bars(self, duration: str = WARMUP_DURATION) -> list[Bar]:
        """
        Fetch recent closed 5-min MIDPOINT bars (oldest first) for EMA warm-up.
        """
        try:
            bars = self.ib.reqHistoricalData(
                self.contract,
                endDateTime    = "",
                durationStr    = duration,
                barSizeSetting = BAR_SIZE,
                whatToShow     = "MIDPOINT",
                useRTH         = False,
                formatDate     = 2,
            )
        except Exception as exc:
            log.warning("Warm-up bar fetch failed: %s", exc)
            return []
        if not bars:
            return []
        out = [Bar.from_ib(b) for b in bars]
        return out[:-1] if len(out) > 1 else []

    def _bar_is_fresh(self, bar: Bar) -> bool:
        """
        True if the bar closed within STALE_TRIGGER_MAX_MINUTES of now.
        """
        bar_close = bar.date + timedelta(minutes=5)
        return datetime.now(REF_TZ) - bar_close <= timedelta(minutes=STALE_TRIGGER_MAX_MINUTES)

    def _fetch_historical_asia_bars(self) -> list[Bar]:
        """
        Fetch the current Asia session's 5-min bars (asia_open .. asia_close.
        """
        if self.asia_open_dt is None or self.asia_close_dt is None:
            log.warning("Cannot fetch Asia bars: session boundaries not set.")
            return []

        log.info("Fetching historical Asia bars from %s to %s", self.asia_open_dt, self.asia_close_dt)

        try:
            bars = self.ib.reqHistoricalData(
                self.contract,
                endDateTime    = self.asia_close_dt + timedelta(minutes=5),
                durationStr    = "1 D",
                barSizeSetting = "5 mins",
                whatToShow     = "MIDPOINT",
                useRTH         = False,
                formatDate     = 2,
            )
        except Exception as exc:
            log.error("Failed to fetch historical Asia bars: %s", exc)
            return []

        out: list[Bar] = []
        for ib_bar in bars or []:
            b = Bar.from_ib(ib_bar)
            if self.asia_open_dt <= b.date <= self.asia_close_dt:
                out.append(b)
        log.info("Recovered %d historical Asia bars.", len(out))
        return out


def _bars_to_dicts(bars: list[Bar]) -> list[dict]:
    """Serialise bars for the state file."""
    return [
        {
            "open": b.open,
            "high": b.high,
            "low": b.low,
            "close": b.close,
            "volume": b.volume,
            "date": b.date.isoformat(),
        }
        for b in bars
    ]


def _dict_to_sb(d: dict) -> SessionBoundariesCandles:
    """
    Deserialise boundaries dict → SessionBoundariesCandles
    """
    sb = SessionBoundariesCandles()
    for k, v in d.items():
        if hasattr(sb, k):
            setattr(sb, k, v)
    return sb
