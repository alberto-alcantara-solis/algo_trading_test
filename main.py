"""
main.py — Entry point for the Forex Trading Bot.

Responsibilities:
  1. Connect to IB (Gateway).
  2. Poll closed 5-min bars and route them to the active DailyStrategy.
  3. Manage the trading-day lifecycle.  A trading "day" runs from Asia open
     (~08:00 UTC+8) to shut-off (~03:00 UTC+8 the NEXT calendar day), so it
     crosses midnight: the day only rolls once the previous session reached
     DONE.  Friday's session legitimately finishes on Saturday morning, and
     weekends simply wait for Monday.
  4. Wall-clock safety net: force the shut-off if bars stall past the boundary.
  5. Handle graceful shutdown (Ctrl-C, SIGTERM).
  6. On startup, detect if we're mid-strategy and resume via bootstrap().
"""


import asyncio
import logging
import signal
import sys
from datetime import datetime, date, timedelta
from typing import Optional

from ib_insync import IB, Forex, util

from config import *
from full_trading import Bar
from state_manager import StateManager
from strategy import DailyStrategy, PHASE_DONE, PHASE_WAIT_ASIA_OPEN
from timezone_utils import is_trading_day

util.patchAsyncio()


# ---------------------------------------------------------------------------
# Logging setup
# ---------------------------------------------------------------------------
logging.basicConfig(
    level    = logging.INFO,
    format   = "%(asctime)s  %(levelname)-8s  %(name)s — %(message)s",
    handlers = [
        logging.StreamHandler(sys.stdout),
        logging.FileHandler("bot.log", encoding="utf-8"),
    ],
)
log = logging.getLogger("main")


# ---------------------------------------------------------------------------
# Suppress ib_insync noise
# ---------------------------------------------------------------------------
logging.getLogger("ib_insync.ib").setLevel(logging.WARNING)
logging.getLogger("ib_insync.client").setLevel(logging.WARNING)


# ---------------------------------------------------------------------------
# Trading Bot
# ---------------------------------------------------------------------------
class ForexBot:
    def __init__(self):
        self.ib       = IB()
        self.sm       = StateManager()
        self.contract: Optional[Forex] = None
        self.strategy: Optional[DailyStrategy] = None

        self._last_bar_dt: Optional[datetime] = None
        self._running = True

    def start(self) -> None:
        """
        Startup
        """
        log.info("Starting Forex Trading Bot")

        self.ib.connect(IB_HOST, IB_PORT, clientId=IB_CLIENT_ID)
        log.info("Connected to IB  host=%s  port=%d", IB_HOST, IB_PORT)

        self.contract = Forex(SYMBOL)
        self.ib.qualifyContracts(self.contract)
        log.info("Contract qualified: %s", self.contract)

        self.sm.load()
        self._ensure_strategy()

        signal.signal(signal.SIGINT,  self._shutdown_signal)
        signal.signal(signal.SIGTERM, self._shutdown_signal)

        self.ib.run(self._main_loop())

    async def _main_loop(self) -> None:
        """
        Main async loop
        """
        log.info("Main loop started.")
        while self._running:
            try:
                await asyncio.sleep(30)

                self._ensure_strategy()
                self._safety_shutoff()

                if self.strategy:
                    new_bars = self._fetch_new_bars()
                    for bar in new_bars:
                        self.strategy.on_bar(bar)

            except Exception as exc:
                log.error("Main loop error: %s", exc, exc_info=True)

        log.info("Main loop exited.")

    def _fetch_new_bars(self) -> list[Bar]:
        """
        Fetch closed 5-min bars since the last processed bar and return any that are NEW.
        """
        try:
            bars = self.ib.reqHistoricalData(
                self.contract,
                endDateTime    = "",
                durationStr    = "1800 S",
                barSizeSetting = BAR_SIZE,
                whatToShow     = "MIDPOINT",
                useRTH         = False,
                formatDate     = 2,
                keepUpToDate   = False,
            )
        except Exception as exc:
            log.error("reqHistoricalData failed: %s", exc)
            return []

        if not bars:
            return []

        new_bars: list[Bar] = []
        for b in bars:
            bar = Bar.from_ib(b)
            new_bars.append(bar)

        closed_bars = new_bars[:-1] if len(new_bars) > 1 else []

        result = []
        for bar in closed_bars:
            if self._last_bar_dt is None or bar.date > self._last_bar_dt:
                result.append(bar)

        if result:
            self._last_bar_dt = result[-1].date
            log.debug("Processing %d new closed bars (last: %s)",
                      len(result), self._last_bar_dt)

        return result


    # ------------------------------------------------------------------
    # Strategy lifecycle
    # ------------------------------------------------------------------
    def _new_strategy(self) -> None:
        self.strategy = DailyStrategy(self.ib, self.contract, self.sm)
        self.strategy.bootstrap()

    def _ensure_strategy(self) -> None:
        """
        Create / resume / roll the DailyStrategy according to the day model.
        """
        today = datetime.now(REF_TZ).date()
        st = self.sm.state

        if st.trade_date == today.isoformat():
            if self.strategy is None:
                log.info("Resuming strategy for %s at phase=%s", today, st.phase)
                self._new_strategy()
            return

        mid_session = bool(st.trade_date) and st.phase not in (PHASE_DONE, PHASE_WAIT_ASIA_OPEN)
        if mid_session:
            try:
                stored = date.fromisoformat(st.trade_date)
            except ValueError:
                stored = None

            if stored and (today - stored).days <= 1:
                if self.strategy is None:
                    log.info("Resuming overnight session of %s (phase=%s).",
                             st.trade_date, st.phase)
                    self._new_strategy()
                return

            log.warning("State is %s (phase=%s) but today is %s — force-closing stale session.",
                        st.trade_date, st.phase, today)
            if self.strategy is None:
                self._new_strategy()
            try:
                self.strategy._handle_shutoff()
            except Exception as exc:
                log.error("Stale-session shut-off failed: %s", exc, exc_info=True)

        if is_trading_day(datetime.now(REF_TZ)):
            log.info("Rolling to new trading day %s.", today)
            self.sm.reset_for_new_day(today)
            self._new_strategy()
        else:
            log.info("Day rolled to %s — not a trading day, waiting.", today)
            self.sm.reset_for_new_day(today)
            self.strategy = None

    def _safety_shutoff(self) -> None:
        """
        Wall-clock safety net: if bars stall and the session runs past its shut-off boundary, force the shut-off anyway.
        """
        if not self.strategy:
            return
        if self.sm.state.phase in (PHASE_DONE, PHASE_WAIT_ASIA_OPEN):
            return
        so = self.strategy.shutoff_dt
        if so and datetime.now(REF_TZ) >= so + timedelta(minutes=10):
            log.warning("Wall-clock is past shut-off (%s) and the session is still open — forcing shut-off.", so)
            try:
                self.strategy._handle_shutoff()
            except Exception as exc:
                log.error("Safety shut-off failed: %s", exc, exc_info=True)


    # ------------------------------------------------------------------
    # Graceful shutdown
    # ------------------------------------------------------------------
    def _shutdown_signal(self, signum, frame) -> None:
        log.info("Shutdown signal received (%s) — stopping bot.", signum)
        self._running = False
        if self.strategy and self.sm.state.phase not in (PHASE_DONE,):
            log.info("Saving state before exit.")
            self.sm.save()
        self.ib.disconnect()
        sys.exit(0)


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------
if __name__ == "__main__":
    bot = ForexBot()
    bot.start()