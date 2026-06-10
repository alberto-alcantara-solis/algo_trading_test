"""
main.py — Entry point for the Forex Trading Bot.

Responsibilities:
  1. Connect to IB (Gateway).
  2. Subscribe to 5-min real-time bars for the configured instrument.
  3. On each closed bar, route it to the active DailyStrategy.
  4. At the end of the dayly shutoff boundaries rotate to a new DailyStrategy.
  5. Handle graceful shutdown (Ctrl-C, SIGTERM).
  6. On startup, detect if we're mid-strategy and resume via bootstrap().
"""


import asyncio
import logging
import signal
import sys
from datetime import datetime, date, time
from typing import Optional

from ib_insync import IB, Forex

from config import *
from full_trading import Bar
from state_manager import StateManager
from strategy import DailyStrategy, PHASE_DONE
from timezone_utils import is_trading_day


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

        self._poll_task: Optional[asyncio.Task] = None
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

        trade_date = datetime.now(REF_TZ).date()

        if not is_trading_day():
            log.info("Today (%s) is not a trading day.  Bot will wait.", trade_date)
        else:
            self._init_or_resume_strategy(trade_date)

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

                if not is_trading_day():
                    await self._maybe_roll_day()
                    continue

                await self._maybe_roll_day()

                new_bars = self._fetch_new_bars()
                for bar in new_bars:
                    if self.strategy:
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
    def _init_or_resume_strategy(self, trade_date: date) -> None:
        """
        If state is for today → resume from current phase.
        Otherwise → reset and start fresh.
        """
        if self.sm.is_same_day(trade_date):
            log.info("Resuming strategy for %s at phase=%s",
                     trade_date, self.sm.state.phase)
            if self.sm.state.phase == PHASE_DONE:
                log.info("Today's strategy is already DONE.  Waiting for next day.")
                return
        else:
            log.info("New trading day %s — resetting state.", trade_date)
            self.sm.reset_for_new_day(trade_date)

        self.strategy = DailyStrategy(self.ib, self.contract, self.sm)
        self.strategy.bootstrap()

    async def _maybe_roll_day(self) -> None:
        """
        Check if the calendar day in REF_TZ has changed.  If so, rotate.
        """
        trade_date = datetime.now(REF_TZ).date()
        if not self.sm.is_same_day(trade_date):
            if is_trading_day():
                log.info("Day rolled to %s — starting new strategy.", trade_date)
                if self.strategy and self.sm.state.phase != PHASE_DONE:
                    log.warning("Previous day strategy was not DONE — forcing close.")
                    if self.strategy._ft_engine:
                        self.strategy._ft_engine.close_all_market()
                self._init_or_resume_strategy(trade_date)
            else:
                log.info("Day rolled to %s — not a trading day.", trade_date)
                self.sm.reset_for_new_day(trade_date)
                self.strategy = None


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
