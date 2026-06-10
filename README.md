# Forex Trading Bot — IB/ib_insync  
**Strategy: Tokyo-anchored volume-profile + FVG slot engine**

---

## Table of Contents
1. [Project Structure](#project-structure)
2. [Setup](#setup)
3. [Configuration](#configuration)
4. [Strategy Overview](#strategy-overview)
5. [Daily Phase Flow](#daily-phase-flow)
6. [Full Trading Engine](#full-trading-engine)
7. [Crash Recovery](#crash-recovery)
8. [Candle Timing Convention](#candle-timing-convention)
9. [Running the Bot](#running-the-bot)
10. [Paper Trading Checklist](#paper-trading-checklist)

---

## Project Structure

```
ALGO_TRADING_TEST/
├── main.py            Entry point — IB connection, bar polling loop, day rotation
├── strategy.py        Daily orchestrator — all phases, fixed-time orders, VP calls
├── full_trading.py    Slot-based break/FVG/confirmation engine (Full Trading logic)
├── volume_profile.py  Fixed Range Volume Profile (FRVP) from IB historical bars
├── state_manager.py   JSON persistence — survives crashes/restarts
├── timezone_utils.py  UTC+9 session-boundary computation with live DST detection
├── config.py          All tunable parameters (IB host/port, R:R, EMA period, …)
├── requirements.txt   Python dependencies
└── bot.log            Created at runtime
```

---

## Setup

### 1. Install TWS / IB Gateway (paper account)
- Download from Interactive Brokers.
- Enable the **API** in TWS: *File → Global Configuration → API → Settings*
  - ✅ Enable ActiveX and Socket Clients
  - Socket port: **7497** (TWS paper) or **4002** (Gateway paper)
  - ✅ Allow connections from localhost only

### 2. Python environment

```bash
python -m venv venv
source venv/bin/activate        # Windows: venv\Scripts\activate
pip install -r requirements.txt
```

### 3. Configure `config.py`

| Parameter | Default | Meaning |
|-----------|---------|---------|
| `IB_HOST` | `127.0.0.1` | TWS/Gateway host |
| `IB_PORT` | `7497` | TWS paper port |
| `IB_CLIENT_ID` | `1` | Unique per session |
| `SYMBOL` / `CURRENCY` | `EUR` / `USD` | Instrument |
| `EMA_PERIOD` | `100` | EMA for order confirmation |
| `MAX_SLOTS` | `10` | Max concurrent Full-Trading orders |
| `RISK_REWARD` | `2.75` | R:R for Full-Trading bracket orders |
| `VALUE_AREA_PCT` | `0.70` | FRVP value area percentage |

---

## Configuration

Edit **`config.py`** for all tunable parameters.  No strategy logic lives there — it is purely numbers.

---

## Strategy Overview

All times are **UTC+9 (Tokyo)** — this timezone never observes DST, making it the stable anchor.

London and New York observe DST, so their session boundaries in UTC+9 shift by ±1 hour twice a year.  The bot detects the current offsets at runtime via the OS timezone database:

```python
tz = ZoneInfo("Europe/London")
offset_hours = datetime.now(tz).utcoffset().total_seconds() / 3600
```

Resulting session windows (UTC+9):

| Session | UTC offset | UTC+9 open | UTC+9 close |
|---------|-----------|------------|-------------|
| Asia    | UTC+9      | 09:00      | 18:00       |
| London  | UTC+1 summer / UTC+0 winter | 16:00 / 17:00 | 01:00 / 02:00 |
| New York | UTC−4 summer / UTC−5 winter | 21:00 / 22:00 | 06:00 / 07:00 |

---

## Daily Phase Flow

```
09:00  ASIA OPEN       Bot waits, collects bars for Asia range
  ↓
16/17  LONDON OPEN     VP1 computed (Asia open → London open)
                       VAH_1, POC_1, VAL_1 saved
  ↓
       MID CANDLE 1    Order 1 placed (direction toward POC_1, TP=POC_1, no SL)
       (17:00 / 17:30) Long if POC_1 > close, Short if POC_1 < close
  ↓
18:00  ASIA CLOSE      ① Order 1 closed at market if still open
                       ② VP2 computed (Asia open → Asia close)
                          VAH_2, POC_2, VAL_2 saved
                       ③ Asia High (AH) and Asia Low (AL) marked
  ↓
18:00─01/02  FULL TRADING begins (see below)
  ↓
01/02  LONDON CLOSE    VP3 computed (London open → London close)
                       VAH_3, POC_3, VAL_3 saved
                       Full Trading continues uninterrupted
  ↓
       MID CANDLE 2    Order 2 placed (direction toward closest POC, TP=closest POC, no SL)
       (02:30/03:00    Both POCs below → short to highest of them
       /03:30)         Both POCs above → long to lowest of them
                       Price between POCs → direction/TP toward POC_2
  ↓
04/05  SHUT-OFF        All orders closed at market
       (NY close − 2h) Bot waits for 09:00 next day
```

### Important: Candle timing

> "At time X" always means **the second before**, i.e. the **close** of the candle whose bar **open** is at X−5min.  
> Example: "London open at 17:00" → trigger on the candle that opened at 16:55 and closed at 17:00 (≈16:59:59).

Orders are only placed if the **trigger candle is the most recently closed candle** — staleness guard prevents executing stale signals after a restart.

---

## Full Trading Engine

Runs continuously from **Asia close → Shut-off** (including through London close and NY open).  Two independent layers:

### Parent Scanner (runs every bar)
Checks all four levels — **AH, AL, VAH_2, VAL_2** — for a break:
- **Long break**: bar opens below level, closes above it, green candle.
- **Short break**: bar opens above level, closes below it, red candle.
- Multi-level crosses on one bar = ONE break event.

### Slot Pipeline (per break event)

```
C2 closes and breaks a level
  ↓
Stage 1: FVG Detection (C3)
  Long FVG:  C3.low > C1.high   → gap = [C1.high, C3.low]
  Short FVG: C3.high < C1.low   → gap = [C3.high, C1.low]
  No FVG → slot discarded
  ↓
Stage 2: 1st Confirmation
  Price must retrace INTO the FVG.
  Long:  open > top_fvg AND close inside FVG  → confirmed
         any close < bot_fvg → discard
  Short: open < bot_fvg AND close inside FVG  → confirmed
         any close > top_fvg → discard
  ↓
Stage 3: 2nd Confirmation
  Price must EXIT FVG on original breakout side AND
  close beyond a broke level AND close confirms EMA(100).
  Fails EMA/level check → step back to Stage 2
  ↓
Stage 4: Order Placement
  Entry = bar.close
  Long SL  = C1.low,  TP = entry + 2.75 × (entry − SL)
  Short SL = C1.high, TP = entry − 2.75 × (SL − entry)
  Bracket order placed (entry MKT + TP LMT + SL STP)
  ↓
Stage 5: Order Launched
  Cancellation if: Long close < bot_fvg OR close < EMA
                   Short close > top_fvg OR close > EMA
  Timeout after ORDER_TIMEOUT_BARS bars
  Fill detected → Monitoring
  ↓
Stage 6: Monitoring
  Slot freed when IB reports position closed (TP or SL)
```

Max concurrent slots: **10** (configurable).

---

## Crash Recovery

All state is persisted to **`bot_state.json`** after every meaningful event.  On restart:

1. `StateManager.load()` reads the JSON.
2. `DailyStrategy.bootstrap()` restores:
   - Current phase
   - Session boundaries
   - Volume profile values (VP1, VP2, VP3)
   - Asia range (AH, AL)
   - All active slot states
3. The bot re-attaches to in-flight IB orders by `orderId` and resumes.

If the state file is missing or corrupt, the bot starts fresh from `WAIT_ASIA_OPEN`.

### What the bot handles automatically

| Scenario | Behaviour |
|----------|-----------|
| Restart before Order 1 window | Resumes waiting |
| Restart after Order 1 placed, before Asia close | Re-checks TP status; closes at market at 18:00 if needed |
| Restart mid-Full-Trading | Restores all slot state machines and re-attaches to open orders |
| Restart at/after shut-off | Sees phase=DONE; waits for next trading day |
| Weekend / holiday | Phase stays DONE; rolls to new day at next Mon 09:00 UTC+9 |

---

## Candle Timing Convention

IB's `reqHistoricalData` with `formatDate=2` returns bar **open** timestamps in UTC.  A 5-min bar is considered **closed** when the next bar has opened (i.e., the bar timestamp is 5 min older than the latest bar).  The bot always operates on closed bars only.

---

## Running the Bot

```bash
# Paper trading
python main.py

# Logs go to both stdout and bot.log
# State is persisted to bot_state.json
# Stop with Ctrl-C (state is saved before exit)
```

To reset state for a fresh start:
```bash
rm bot_state.json
```

---

## Paper Trading Checklist

Before going live:

- [ ] TWS / Gateway running on paper account
- [ ] API enabled in TWS settings, port matches `IB_PORT` in config
- [ ] `SYMBOL` / `CURRENCY` / `EXCHANGE` correct for your instrument
- [ ] `totalQuantity` in order placement calls tuned to desired lot size
- [ ] `EMA_PERIOD`, `MAX_SLOTS`, `RISK_REWARD` reviewed
- [ ] Run for at least one full week in paper mode
- [ ] Check `bot.log` after each session for any `ERROR` entries
- [ ] Verify state JSON after each restart scenario
