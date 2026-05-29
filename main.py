"""
main.py — Loop principal del bot de trading.
─────────────────────────────────────────────
Arranca el bot, carga el estado guardado y ejecuta un tick cada CHECK_INTERVAL_SEC segundos.
"""


import logging
import signal
import sys
import time

import config
from broker import Broker
from state import BotState
from strategy import Strategy


logging.basicConfig(
    level   = logging.INFO,
    format  = "%(asctime)s  %(levelname)-8s  %(message)s",
    datefmt = "%Y-%m-%d %H:%M:%S",
    handlers = [
        logging.FileHandler(config.LOG_FILE, encoding="utf-8"),
        logging.StreamHandler(sys.stdout),
    ],
)
log = logging.getLogger(__name__)

_running = True


def _handle_stop(sig):
    global _running
    log.info(f"Señal {sig} recibida — parando después del tick actual...")
    _running = False

signal.signal(signal.SIGINT,  _handle_stop)
signal.signal(signal.SIGTERM, _handle_stop)


def main():
    log.info("=" * 65)
    log.info("   BOT DE TRADING INICIADO")
    log.info(f"   Símbolo:    {config.SYMBOL}")
    log.info(f"   Dirección:  {config.TRADE_DIRECTION}")
    log.info(f"   Capital:    {config.CAPITAL_PCT * 100:.0f}% por operación")
    log.info(f"   EMA:        {config.EMA_LENGTH} períodos")
    log.info(f"   R/R:        1:{config.RISK_REWARD}")
    log.info(f"   Modo:       {'🟡 PAPER TRADING' if config.PAPER else '🔴 LIVE — DINERO REAL'}")
    log.info("=" * 65)

    if not config.ALPACA_API_KEY or not config.ALPACA_SECRET_KEY:
        log.error("Faltan las claves de Alpaca. Configura ALPACA_API_KEY y "
                  "ALPACA_SECRET_KEY en el fichero .env")
        sys.exit(1)

    state  = BotState.load(config.STATE_FILE)
    broker = Broker()
    strat  = Strategy(broker, state)

    log.info(
        f"Estado cargado: {state.status} | "
        f"Fecha sesión: {state.trade_date or 'ninguna'}"
    )

    while _running:
        tick_start = time.monotonic()
        try:
            strat.tick()
        except KeyboardInterrupt:
            break
        except Exception as exc:
            log.error(f"Error inesperado en tick: {exc}", exc_info=True)

        elapsed = time.monotonic() - tick_start
        sleep_time = max(0.0, config.CHECK_INTERVAL_SEC - elapsed)
        time.sleep(sleep_time)

    log.info("Bot detenido correctamente.")


if __name__ == "__main__":
    main()