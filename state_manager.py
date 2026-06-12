"""
state_manager.py — Persistent state for crash/restart recovery.
"""


import os
import json
import logging

from dataclasses import asdict, dataclass, field

from datetime import date
from typing import Optional

from config import STATE_FILE


log = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Data classes
# ---------------------------------------------------------------------------
@dataclass
class OrderRecord:
    ib_order_id:   int     = 0
    tp_order_id:   int     = 0
    direction:     str     = ""      # "long" | "short"
    entry_price:   float   = 0.0
    tp_price:      float   = 0.0
    sl_price:      float   = 0.0
    total_quantity: int   = 0
    is_open:       bool    = True
    has_sl:        bool    = True
    closed_reason: str     = ""      # "tp" | "sl" | "market" | "cancel"

@dataclass
class DayState:
    """Complete daily state snapshot."""
    trade_date:     str  = ""

    # ---- phase tracking ----
    # Phases (in order):
    #   WAIT_ASIA_OPEN → WAIT_LONDON_OPEN → WAIT_ORDER1 → ORDER1_ACTIVE → WAIT_ASIA_CLOSE → 
    #   FULL_TRADING → WAIT_ORDER2 → ORDER2_ACTIVE → WAIT_SHUTOFF → DONE (→ WAIT_ASIA_OPEN)
    phase: str = "WAIT_ASIA_OPEN"

    order1_placed:   bool = False
    order1_closed:   bool = False
    order1:          Optional[dict] = None

    order2_placed:   bool = False
    order2_closed:   bool = False
    order2:          Optional[dict] = None

    vp1: Optional[dict] = None   # asia_open → london_open
    vp2: Optional[dict] = None   # asia_open → asia_close
    vp3: Optional[dict] = None   # london_open → london_close

    asia_high: Optional[float] = None
    asia_low:  Optional[float] = None

    asia_bars: list = field(default_factory=list)

    slots: list = field(default_factory=list)

    time_boundaries: Optional[dict] = None

class StateManager:
    """
    Thin wrapper around a JSON file.
    """
    def __init__(self, path: str = STATE_FILE):
        self._path = path
        self._state: DayState = DayState()

    @property
    def state(self) -> DayState:
        return self._state

    def load(self) -> DayState:
        """Load state from disk; return a fresh DayState if file is absent."""
        if not os.path.exists(self._path):
            log.info("State file not found — starting fresh.")
            self._state = DayState()
            return self._state

        try:
            with open(self._path, "r") as fh:
                raw = json.load(fh)
            self._state = _dict_to_day_state(raw)
            log.info("State loaded from %s  (phase=%s  date=%s)",
                     self._path, self._state.phase, self._state.trade_date)
        except Exception as exc:
            log.error("Failed to load state (%s) — starting fresh.", exc)
            self._state = DayState()

        return self._state

    def save(self) -> None:
        """Save current state to disk."""
        tmp = self._path + ".tmp"
        try:
            with open(tmp, "w") as fh:
                json.dump(_day_state_to_dict(self._state), fh, indent=2)
            os.replace(tmp, self._path)
        except Exception as exc:
            log.error("Failed to save state: %s", exc)

    def reset_for_new_day(self, trade_date: date) -> None:
        """Wipe state and start a new trading day."""
        self._state = DayState(trade_date=trade_date.isoformat())
        self.save()
        log.info("State reset for new day: %s", trade_date)

    def set_phase(self, phase: str) -> None:
        self._state.phase = phase
        self.save()
        log.info("Phase → %s", phase)

    def set_vp(self, which: int, vah: float, poc: float, val: float) -> None:
        d = {"vah": vah, "poc": poc, "val": val}
        if which == 1:
            self._state.vp1 = d
        elif which == 2:
            self._state.vp2 = d
        elif which == 3:
            self._state.vp3 = d
        self.save()

    def set_asia_range(self, high: float, low: float) -> None:
        self._state.asia_high = high
        self._state.asia_low  = low
        self.save()

    def record_order1(self, order: OrderRecord) -> None:
        self._state.order1_placed = True
        self._state.order1 = asdict(order)
        self.save()

    def mark_order1_closed(self, reason: str) -> None:
        self._state.order1_closed = True
        if self._state.order1:
            self._state.order1["closed_reason"] = reason
            self._state.order1["is_open"] = False
        self.save()

    def record_order2(self, order: OrderRecord) -> None:
        self._state.order2_placed = True
        self._state.order2 = asdict(order)
        self.save()

    def mark_order2_closed(self, reason: str) -> None:
        self._state.order2_closed = True
        if self._state.order2:
            self._state.order2["closed_reason"] = reason
            self._state.order2["is_open"] = False
        self.save()

    def upsert_slot(self, slot_dict: dict) -> None:
        """Insert or update a slot record by slot_id."""
        slots = self._state.slots
        for i, s in enumerate(slots):
            if s.get("slot_id") == slot_dict["slot_id"]:
                slots[i] = slot_dict
                self.save()
                return
        slots.append(slot_dict)
        self.save()

    def remove_slot(self, slot_id: int) -> None:
        self._state.slots = [
            s for s in self._state.slots if s.get("slot_id") != slot_id
        ]
        self.save()

    def set_time_boundaries(self, time_bboundaries_dict: dict) -> None:
        self._state.time_boundaries = time_bboundaries_dict
        self.save()

    def set_asia_bars(self, bars: list) -> None:
        """
        Persist a list of bars (as dicts) from the Asia session.
        
        Args:
            bars: List of dicts with keys: open, high, low, close, volume, date
        """
        self._state.asia_bars = bars
        self.save()

    def get_asia_bars(self) -> list:
        """Retrieve persisted Asia bars from state."""
        return self._state.asia_bars or []


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------
def _day_state_to_dict(ds: DayState) -> dict:
    return asdict(ds)


def _dict_to_day_state(d: dict) -> DayState:
    ds = DayState()
    for k, v in d.items():
        if hasattr(ds, k):
            setattr(ds, k, v)
    return ds