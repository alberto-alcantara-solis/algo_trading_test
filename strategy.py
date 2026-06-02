"""
Máquina de estados de la estrategia de algorithmic trading.

Implementa exactamente la lógica descrita:
    Opening Range → Break → FVG → 1ª Confirm → 2ª Confirm → Orden → Monitor

Reglas clave:
- Solo se procesan velas CERRADAS (nunca la vela en formación).
- Cuando se vuelve a WAITING_BREAK por cancelación, se hace REPLAY desde
  la vela C2 del último FVG para no perder patrones formados mientras
  el bot estaba ocupado en otro estado.
- Solo se puede lanzar una orden si la 2ª confirmación la marca la
  vela cerrada MÁS RECIENTE (no una vela pasada del replay).
"""


import logging
from datetime import datetime, timedelta
from typing import List, Optional
from zoneinfo import ZoneInfo

import config
from state import BotState, S, Dir, Candle
from broker import Broker

from indicators import ema


log = logging.getLogger(__name__)
UTC = ZoneInfo("UTC")


def _dt(iso: str) -> datetime:
    """Convierte un string ISO UTC a datetime tz-aware."""
    dt = datetime.fromisoformat(iso)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=UTC)
    return dt.astimezone(UTC)


class Strategy:
    """
    Gestiona el ciclo completo de la estrategia.
    """
    def __init__(self, broker: Broker, state: BotState):
        self.broker        = broker
        self.st            = state


    # ─────────────────────────────────────────────────────────────────────────
    # Punto de entrada
    # ─────────────────────────────────────────────────────────────────────────
    def tick(self):
        """
        Ejecuta un ciclo completo:
        1. Detecta si hay que inicializar una nueva sesión.
        2. Aplica los cortes por tiempo.
        3. Obtiene las velas cerradas de hoy.
        4. Procesa velas nuevas (o replay) a través de la máquina de estados.
        5. Guarda el estado.
        """
        now = datetime.now(UTC)

        # Nuevo día
        if self._is_new_day(now):
            self._init_day(now)
            return

        # Mercado aún no abierto
        if self.st.status in (S.WAITING_OPEN, S.DAY_ENDED):
            if self.st.market_open_utc and now < _dt(self.st.market_open_utc):
                return
            if self.st.status == S.DAY_ENDED:
                return

        # Finalización del trading del día
        self._apply_time_cutoffs(now)
        if self.st.status == S.DAY_ENDED:
            self.st.save(config.STATE_FILE)
            return

        # Get velas cerradas
        market_open = _dt(self.st.market_open_utc)
        bars        = self.broker.get_closed_bars(config.SYMBOL, market_open, now)

        if not bars:
            return

        # Cambio a CALC_RANGE si mercado acaba de abrir
        if self.st.status == S.WAITING_OPEN:
            self.st.status = S.CALC_RANGE

        # Procesado principal
        self._process_bars(bars)
        self.st.save(config.STATE_FILE)


    # ─────────────────────────────────────────────────────────────────────────
    # Inicio de sesión
    # ─────────────────────────────────────────────────────────────────────────
    def _is_new_day(self, now: datetime) -> bool:
        """True si la fecha NY de ahora es diferente a la sesión activa."""
        today_ny = now.astimezone(ZoneInfo("America/New_York")).strftime("%Y-%m-%d")
        return self.st.trade_date != today_ny

    def _init_day(self, now: datetime):
        """Inicializa el estado para la sesión de hoy y lo guarda."""
        ny_date  = now.astimezone(ZoneInfo("America/New_York")).date()
        date_str = ny_date.strftime("%Y-%m-%d")

        try:
            open_utc, close_utc = self.broker.get_market_hours(ny_date)
        except ValueError:
            log.info(f"{date_str}: Mercado cerrado (día no hábil).")
            self.st.trade_date = date_str
            self.st.status     = S.DAY_ENDED
            self.st.save(config.STATE_FILE)
            return

        self.st.reset_for_day(date_str, open_utc.isoformat(), close_utc.isoformat())
        log.info(
            f"Nueva sesión: {date_str} | "
            f"Apertura: {open_utc.strftime('%H:%M UTC')} | "
            f"Cierre: {close_utc.strftime('%H:%M UTC')}"
        )
        self.st.save(config.STATE_FILE)


    # ─────────────────────────────────────────────────────────────────────────
    # Cierre de sesión
    # ─────────────────────────────────────────────────────────────────────────
    def _apply_time_cutoffs(self, now: datetime):
        """
        Aplica las tres finalizaciones por tiempo antes del cierre de mercado.
        """
        if not self.st.market_close_utc:
            return

        close     = _dt(self.st.market_close_utc)
        mins_left = (close - now).total_seconds() / 60.0

        # Corte 1: Posición abierta → cerrar a mercado (21:55 h)
        if mins_left <= config.CUTOFF_MONITOR_MINS and self.st.status == S.MONITORING:
            log.warning(
                f"⏰ Corte {config.CUTOFF_MONITOR_MINS} min — "
                f"Cerrando posición abierta a mercado."
            )
            self.broker.close_position_at_market(config.SYMBOL)
            self._end_day()
            return

        # Corte 2: Orden lanzada sin ejecutar → cancelar (21:50 h)
        if mins_left <= config.CUTOFF_LAUNCHED_MINS and self.st.status == S.ORDER_LAUNCHED:
            log.warning(
                f"⏰ Corte {config.CUTOFF_LAUNCHED_MINS} min — "
                f"Cancelando orden pendiente {self.st.order_id}."
            )
            if self.st.order_id:
                self.broker.cancel_order(self.st.order_id)
            self._end_day()
            return

        # Corte 3: Cualquier otro estado activo → fin de sesión (21:45 h)
        if mins_left <= config.CUTOFF_ANY_MINS and self.st.status not in (
            S.MONITORING, S.ORDER_LAUNCHED, S.DAY_ENDED, S.WAITING_OPEN
        ):
            log.warning(
                f"⏰ Corte {config.CUTOFF_ANY_MINS} min — "
                f"Fin de sesión (estado: {self.st.status})."
            )
            self._end_day()

    def _end_day(self):
        """Finaliza la sesión: limpia el FVG y pone el estado en DAY_ENDED."""
        self.st.reset_fvg()
        self.st.status = S.DAY_ENDED
        log.info("Sesión terminada. Esperando al día siguiente.")


    # ─────────────────────────────────────────────────────────────────────────
    # Orquestador de la ejecución
    # ─────────────────────────────────────────────────────────────────────────
    def _process_bars(self, all_bars: List[Candle]):
        """
        Determina qué velas procesar y las pasa a la máquina de estados.
            En modo normal: solo procesa velas nuevas (posteriores a last_bar_ts).
            En modo replay: procesa todas las velas desde C2 del último FVG.
        """
        if not all_bars:
            return

        latest_ts = all_bars[-1].timestamp

        # Modo replay
        if self.st.replay_from_ts and self.st.status == S.WAITING_BREAK:
            replay_dt   = _dt(self.st.replay_from_ts)
            to_process  = [b for b in all_bars if _dt(b.timestamp) >= replay_dt]
            self.st.replay_from_ts = None
            self.st.last_bar_ts    = None
            log.info(
                f"🔄 Replay desde {replay_dt.strftime('%H:%M UTC')} "
                f"({len(to_process)} velas)"
            )

        # Modo normal
        else:
            if self.st.last_bar_ts:
                last_dt    = _dt(self.st.last_bar_ts)
                to_process = [b for b in all_bars if _dt(b.timestamp) > last_dt]
            else:
                to_process = all_bars

        if not to_process:
            return

        ts_to_idx = {b.timestamp: i for i, b in enumerate(all_bars)}

        for bar in to_process:
            idx      = ts_to_idx.get(bar.timestamp, 0)
            prev_bar = all_bars[idx - 1] if idx > 0 else None

            session_closes = [b.close for b in all_bars[: idx + 1]]

            can_place_order = (bar.timestamp == latest_ts)

            self._process_one_bar(bar, prev_bar, session_closes, can_place_order)
            self.st.last_bar_ts = bar.timestamp
            self.st.save(config.STATE_FILE)


    # ─────────────────────────────────────────────────────────────────────────
    # Ejecución de la estrategia (para cada vela cerrada)
    # ─────────────────────────────────────────────────────────────────────────
    def _process_one_bar(
        self,
        bar: Candle,
        prev: Optional[Candle],
        session_closes: List[float],
        can_place_order: bool,
    ):
        """
        Avanza los estados analizando las velas.
        """
        s = self.st.status
        log.debug(
            f"  [{s:20s}] {bar.timestamp[11:16]} "
            f"O={bar.open:.2f} H={bar.high:.2f} L={bar.low:.2f} C={bar.close:.2f}"
        )

        if s == S.CALC_RANGE:
            self.st.opening_bars.append(bar.to_dict())
            if len(self.st.opening_bars) >= config.OPENING_RANGE_BARS:
                self.st.top_lim = max(b["high"] for b in self.st.opening_bars)
                self.st.bot_lim = min(b["low"]  for b in self.st.opening_bars)
                self.st.status  = S.WAITING_BREAK
                log.info(
                    f"📐 Opening Range calculado: "
                    f"topLim={self.st.top_lim:.2f}  botLim={self.st.bot_lim:.2f}"
                )
            return

        if s == S.WAITING_BREAK:
            if prev is None:
                return

            d = config.TRADE_DIRECTION

            if d in ("LONG", "BOTH") and self._is_long_break(bar):
                self._enter_waiting_fvg(c2=bar, c1=prev, direction=Dir.LONG)
                return

            if d in ("SHORT", "BOTH") and self._is_short_break(bar):
                self._enter_waiting_fvg(c2=bar, c1=prev, direction=Dir.SHORT)
                return

            return

        if s == S.WAITING_FVG:
            c1  = Candle.from_dict(self.st.c1)
            c3  = bar

            if self.st.direction == Dir.LONG:
                fvg_ok  = c3.low > c1.high
                top_fvg = c3.low
                bot_fvg = c1.high
            else:
                fvg_ok  = c3.high < c1.low
                top_fvg = c1.low
                bot_fvg = c3.high

            if not fvg_ok:
                log.info(f"  ✗ No hay FVG [{self.st.direction}] → WAITING_BREAK")
                self.st.reset_fvg()
                self.st.status = S.WAITING_BREAK
                return

            self.st.c3          = c3.to_dict()
            self.st.top_lim_fvg = top_fvg
            self.st.bot_lim_fvg = bot_fvg
            self.st.min_close_fvg = None
            self.st.max_close_fvg = None
            self._update_fvg_extremes(c3)
            self.st.status = S.WAIT_1ST_CONF
            log.info(
                f"  ✓ FVG [{self.st.direction}] → WAIT_1ST_CONF | "
                f"topFVG={top_fvg:.2f}  botFVG={bot_fvg:.2f}"
            )
            return

        if s == S.WAIT_1ST_CONF:
            self._update_fvg_extremes(bar)

            if self.st.direction == Dir.LONG:
                if bar.close < self.st.bot_lim_fvg:
                    log.info("  ✗ 1ª conf: cierre bajo botFVG → WAITING_BREAK (replay)")
                    self._cancel_to_break()
                    return
                
                if (bar.open > self.st.top_lim_fvg and self.st.bot_lim_fvg < bar.close < self.st.top_lim_fvg):
                    self.st.status = S.WAIT_2ND_CONF
                    log.info("  ✓ 1ª confirmación [LONG] → WAIT_2ND_CONF")
                return
            else:
                if bar.close > self.st.top_lim_fvg:
                    log.info("  ✗ 1ª conf: cierre sobre topFVG → WAITING_BREAK (replay)")
                    self._cancel_to_break()
                    return
                
                if (bar.open < self.st.bot_lim_fvg and self.st.bot_lim_fvg < bar.close < self.st.top_lim_fvg):
                    self.st.status = S.WAIT_2ND_CONF
                    log.info("  ✓ 1ª confirmación [SHORT] → WAIT_2ND_CONF")
                return

        if s == S.WAIT_2ND_CONF:
            self._update_fvg_extremes(bar)
            current_ema = ema(session_closes, config.EMA_LENGTH, self._ema_seed())

            if self.st.direction == Dir.LONG:
                if bar.close < self.st.bot_lim_fvg:
                    log.info("  ✗ 2ª conf: cierre bajo botFVG → WAITING_BREAK (replay)")
                    self._cancel_to_break()
                    return

                crosses_fvg = (bar.open < self.st.top_lim_fvg and bar.close > self.st.top_lim_fvg)

                if not crosses_fvg:
                    return

                crosses_top_lim = bar.close > self.st.top_lim
                ema_ok          = (bar.close > current_ema)

                if crosses_top_lim and ema_ok:
                    if not can_place_order:
                        log.info("  ↩ 2ª conf LONG completa pero en replay → WAIT_1ST_CONF")
                        self.st.status = S.WAIT_1ST_CONF
                    else:
                        log.info("  ✓ 2ª conf LONG completa → ORDER LAUNCH")
                        self._launch_order(bar, current_ema)
                else:
                    reason = "no cruza topLim" if not crosses_top_lim else "bajo EMA"
                    log.info(f"  ↩ 2ª conf LONG parcial ({reason}) → WAIT_1ST_CONF")
                    self.st.status = S.WAIT_1ST_CONF
                return
            else:
                if bar.close > self.st.top_lim_fvg:
                    log.info("  ✗ 2ª conf: cierre sobre topFVG → WAITING_BREAK (replay)")
                    self._cancel_to_break()
                    return

                crosses_fvg = (bar.open > self.st.bot_lim_fvg and bar.close < self.st.bot_lim_fvg)

                if not crosses_fvg:
                    return

                crosses_bot_lim = bar.close < self.st.bot_lim
                ema_ok          = (bar.close < current_ema)

                if crosses_bot_lim and ema_ok:
                    if not can_place_order:
                        log.info("  ↩ 2ª conf SHORT completa pero en replay → WAIT_1ST_CONF")
                        self.st.status = S.WAIT_1ST_CONF
                    else:
                        log.info("  ✓ 2ª conf SHORT completa → ORDER LAUNCH")
                        self._launch_order(bar, current_ema)
                else:
                    reason = "no cruza botLim" if not crosses_bot_lim else "sobre EMA"
                    log.info(f"  ↩ 2ª conf SHORT parcial ({reason}) → WAIT_1ST_CONF")
                    self.st.status = S.WAIT_1ST_CONF
                return

        if s == S.ORDER_LAUNCHED:
            if self.broker.is_order_filled(self.st.order_id):
                self.st.status = S.MONITORING
                log.info(f"  ✅ Orden {self.st.order_id} ejecutada → MONITORING")
                return

            # ⚠️ Condición de seguridad crítica: cancelar si precio rompe el FVG o cruza el EMA
            current_ema = ema(session_closes, config.EMA_LENGTH, self._ema_seed())
            if self.st.direction == Dir.LONG:
                should_cancel = bar.close < self.st.bot_lim_fvg or (current_ema is not None and bar.close < current_ema)
                if should_cancel:
                    reason = "precio fuera del FVG" if (bar.close < self.st.bot_lim_fvg) else "bajo EMA"
            else:
                should_cancel = bar.close > self.st.top_lim_fvg or (current_ema is not None and bar.close > current_ema)
                if should_cancel:
                    reason = "precio fuera del FVG" if (bar.close > self.st.top_lim_fvg) else "sobre EMA"

            if should_cancel:
                log.warning(
                    f"  🚫 Condición de seguridad: cancelando orden {self.st.order_id} "
                    f"({reason}) → WAITING_BREAK (replay)"
                )
                self.broker.cancel_order(self.st.order_id)
                self._cancel_to_break()
            return

        if s == S.MONITORING:
            if not self.broker.has_open_position(config.SYMBOL):
                log.info("  ✅ Posición cerrada (TP o SL alcanzado) → WAITING_BREAK")
                self.st.reset_fvg()
                self.st.status = S.WAITING_BREAK
            return


    # ─────────────────────────────────────────────────────────────────────────
    # Helpers
    # ─────────────────────────────────────────────────────────────────────────
    def _is_long_break(self, bar: Candle) -> bool:
        """
        Ruptura alcista (C2 para Long):
        - Abre por debajo de topLim
        - Cierra por encima de topLim
        - Es verde (cierra por encima de donde abrió)
        """
        return (
            bar.open  < self.st.top_lim
            and bar.close > self.st.top_lim
            and bar.is_green
        )

    def _is_short_break(self, bar: Candle) -> bool:
        """
        Ruptura bajista (C2 para Short):
        - Abre por encima de botLim
        - Cierra por debajo de botLim
        - Es roja (cierra por debajo de donde abrió)
        """
        return (
            bar.open  > self.st.bot_lim
            and bar.close < self.st.bot_lim
            and bar.is_red
        )
    
    def _ema_seed(self) -> Optional[float]:
        if self.st._ema_seed_cache is not None:
            return self.st._ema_seed_cache
        bars = self.st.opening_bars
        if len(bars) < config.OPENING_RANGE_BARS:
            return None
        self.st._ema_seed_cache = sum(b["close"] for b in bars) / len(bars)
        return self.st._ema_seed_cache
    
    def _enter_waiting_fvg(self, c2: Candle, c1: Candle, direction: str):
        """Almacena C1 y C2, resetea el FVG anterior y avanza a WAITING_FVG."""
        self.st.reset_fvg()
        self.st.direction = direction
        self.st.c1        = c1.to_dict()
        self.st.c2        = c2.to_dict()
        self.st.status    = S.WAITING_FVG
        log.info(
            f"  🔴 Break [{direction}] → WAITING_FVG | "
            f"C2={c2.timestamp[11:16]} close={c2.close:.2f}"
        )

    def _cancel_to_break(self):
        """
        Vuelve a WAITING_BREAK y programa un replay desde C2 + 1 minuto.
        Sumarle 1 minuto asegura que la vela inicial del replay sea diferente a la del patrón detectado, evitando bucles.
        """
        if self.st.c2:
            c2_dt = _dt(self.st.c2["timestamp"])
            self.st.replay_from_ts = (c2_dt + timedelta(minutes=1)).isoformat()
            log.info(
                f"  🔄 Replay programado desde {self.st.replay_from_ts[11:16]}"
            )
        self.st.reset_fvg()
        self.st.status = S.WAITING_BREAK

    def _update_fvg_extremes(self, bar: Candle):
        """
        Rastrea el cierre mínimo (Long) y máximo (Short) de velas que han cerrado DENTRO del FVG.
        Usado para calcular el Stop Loss cuando se lanza la orden.
        """
        if self.st.bot_lim_fvg is None or self.st.top_lim_fvg is None:
            return
        if self.st.bot_lim_fvg <= bar.close <= self.st.top_lim_fvg:
            if self.st.min_close_fvg is None or bar.close < self.st.min_close_fvg:
                self.st.min_close_fvg = bar.close
            if self.st.max_close_fvg is None or bar.close > self.st.max_close_fvg:
                self.st.max_close_fvg = bar.close


    # ─────────────────────────────────────────────────────────────────────────
    # Cálculo y lanzamiento de orden
    # ─────────────────────────────────────────────────────────────────────────
    def _launch_order(self, bar: Candle, ema_val: Optional[float]):
        """
        Calcula SL y TP, lanza la orden bracket y actualiza el estado.

        Long:
            entry = bar.close
            SL    = midpoint(min_close_dentro_FVG, mínimo_de_C1)
            TP    = entry + 2.75 x (entry - SL)

        Short:
            entry = bar.close
            SL    = midpoint(max_close_dentro_FVG, máximo_de_C1)
            TP    = entry - 2.75 x (SL - entry)
        """
        entry = bar.close
        c1    = Candle.from_dict(self.st.c1)

        if self.st.direction == Dir.LONG:
            min_close = (self.st.min_close_fvg
                         if self.st.min_close_fvg is not None
                         else self.st.bot_lim_fvg)
            sl = round(c1.low, 2)
            tp = round(entry + config.RISK_REWARD * (entry - sl), 2)

            if sl >= entry:
                log.error(
                    f"SL ({sl:.2f}) >= entry ({entry:.2f}) para LONG — "
                    f"orden descartada."
                )
                self._cancel_to_break()
                return

            order_id = self.broker.place_long_bracket(config.SYMBOL, entry, sl, tp)
        else:
            max_close = (self.st.max_close_fvg
                         if self.st.max_close_fvg is not None
                         else self.st.top_lim_fvg)
            sl = round(c1.high, 2)
            tp = round(entry - config.RISK_REWARD * (sl - entry), 2)

            if sl <= entry:
                log.error(
                    f"SL ({sl:.2f}) <= entry ({entry:.2f}) para SHORT — "
                    f"orden descartada."
                )
                self._cancel_to_break()
                return

            order_id = self.broker.place_short_bracket(config.SYMBOL, entry, sl, tp)

        self.st.entry_price = entry
        self.st.stop_loss   = sl
        self.st.take_profit = tp
        self.st.order_id    = order_id
        self.st.status      = S.ORDER_LAUNCHED

        log.info(
            f"  🚀 Orden lanzada [{self.st.direction}] | "
            f"entry={entry:.2f}  sl={sl:.2f}  tp={tp:.2f}  "
            f"riesgo={entry - sl:.2f}  id={order_id}"
        )
