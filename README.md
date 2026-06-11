# Forex Trading Bot — IBKR / ib_insync
**Strategy: session-anchored volume-profile + break/FVG slot engine (GBPUSD)**

A fully automated intraday bot for Interactive Brokers. It anchors every trading day to a fixed UTC+8 clock, computes three Fixed-Range Volume Profiles per day, places two fixed-time orders toward the Points of Control, and runs a slot-based break + Fair-Value-Gap engine from Asia close until shut-off. All state is persisted to JSON so the bot survives crashes and restarts mid-session.

---

## Project structure

```
ALGO_TRADING_TEST/
├── main.py            Entry point — IB connection, bar polling, day lifecycle
├── strategy.py        Daily orchestrator — phases, fixed-time orders, VP calls
├── full_trading.py    Slot-based break/FVG/confirmation engine + EMA tracker
├── volume_profile.py  Fixed Range Volume Profile from 1-min MIDPOINT bars
├── sizing.py          Order sizing from live account value (EUR → GBP)
├── state_manager.py   JSON persistence — survives crashes/restarts
├── timezone_utils.py  UTC+8 session boundaries with live DST detection
├── config.py          All tunable parameters
├── requirements.txt   Python dependencies (ib_insync, numpy)
├── bot_state.json     Created at runtime — persistent day state
└── bot.log            Created at runtime
```

---

## Setup

### 1. TWS / IB Gateway (paper account)
Enable the API in *Global Configuration → API → Settings*: check **Enable ActiveX and Socket Clients**, set the socket port (**7497** TWS paper / **4002** Gateway paper — the default in `config.py`), and allow connections from localhost.

### 2. Python environment

```bash
python -m venv venv
source venv/bin/activate        # Windows: venv\Scripts\activate
pip install -r requirements.txt
python main.py
```

### 3. Key configuration (`config.py`)

| Parameter | Default | Meaning |
|-----------|---------|---------|
| `IB_HOST` / `IB_PORT` / `IB_CLIENT_ID` | `127.0.0.1` / `4002` / `1` | Gateway connection |
| `SYMBOL` | `GBPUSD` | Pair traded (base GBP, quote USD) |
| `CURRENCY` / `ACCOUNT_CURRENCY` | `EUR` | IBKR **account** base currency |
| `TRADE_QUANTITY` | `0.2` | Fraction of live NetLiquidation per order (0.10–0.20 = 10–20%) |
| `TOTAL_CAPITAL` | `100000` | Fallback notional (EUR) if the live value can't be fetched |
| `EMA_MAIN` | `100` | EMA gating Full-Trading confirmations |
| `EMA_TREND` | `9` | EMA gating order 1 / order 2 direction |
| `MAX_SLOTS` | `10` | Max concurrent Full-Trading slots |
| `RISK_REWARD` | `2.75` | R:R for Full-Trading brackets |
| `VALUE_AREA_PCT` | `0.70` | FRVP value-area percentage |
| `VP_BAR_SIZE` | `1 min` | Bar size used to build volume profiles |
| `STALE_TRIGGER_MAX_MINUTES` | `10` | Freshness window for fixed-time triggers |

---

## Timing model

**Reference timezone: UTC+8** — it never observes DST, so it is the stable anchor for the whole day. London and New York *do* observe DST, so their boundaries shift ±1h twice a year; the bot detects current offsets at runtime from the OS timezone database (`zoneinfo`).

Session windows in UTC+8 (configured as local hours in `config.py`):

| Session | Local hours | UTC+8 summer | UTC+8 winter |
|---------|-------------|--------------|--------------|
| Asia (Tokyo, UTC+9) | 09–18 | 08:00 – 17:00 | 08:00 – 17:00 |
| London (UK) | 08–17 | 15:00 – 00:00 | 16:00 – 01:00 |
| New York (ET) | 08–17 | 20:00 – 05:00 | 21:00 – 06:00 |
| **Shut-off** = NY close − 2h | | **03:00** | **04:00** |

### Candle convention
"At time X" always means **the close of the candle that opened at X−5 min**. Boundary tuples in `SessionBoundariesCandles` are stored in this candle-open style: e.g. Asia close 17:00 is stored as `(16, 55)`. Consequently:

* The Asia range (AH/AL) and every volume profile **include the final candle** of their window (the bar opening at 16:55 and closing at 17:00 belongs to the Asia session).
* The fixed-time triggers fire on the candle whose **open** matches the stored mid-candle tuple, i.e. on its close.

### A trading "day"
A trading day runs **Asia open → shut-off** and therefore **crosses midnight** (e.g. Friday 08:00 → Saturday 03:00). The calendar date stored in the state file is the Asia-open date. The day only rolls to a new date once the session reaches `DONE` — Friday's tail legitimately runs into Saturday morning, and weekends simply wait for Monday.

---

## Daily phase flow

```
08:00        ASIA OPEN        WAIT_ASIA_OPEN → WAIT_LONDON_OPEN, Asia bars collected
  ↓
15/16:00     LONDON OPEN      VP1 computed (Asia open → London open)
  ↓
16:00/16:30  MID CANDLE 1     Order 1: MKT toward POC_1, TP = POC_1, no SL
  ↓                           (gated by EMA_TREND and a freshness check)
17:00        ASIA CLOSE       ① Order 1 closed at market if still open
                              ② VP2 computed (Asia open → Asia close)
                              ③ Asia High / Asia Low marked (full-session refetch)
                              ④ Full-Trading engine created, EMA(100) pre-warmed
  ↓
17:00–03/04  FULL TRADING     break + FVG slot engine on AH, AL, VAH_2, VAL_2
  ↓
00/01:00     LONDON CLOSE     VP3 computed (London open → London close)
  ↓                           Full Trading continues uninterrupted
01:30/02:30  MID CANDLE 2     Order 2: MKT toward the POCs, TP at the relevant POC
  ↓                           (both POCs below → short; both above → long;
  ↓                            price between them → toward POC_2)
03/04:00     SHUT-OFF         Everything closed at market, phase = DONE,
                              state resets and waits for the next Asia open
```

The mid-candles are the exact midpoints of *(true London open → true Asia close)* and *(true London close → true shut-off)*, rounded down to a 5-minute bar open. Example (summer): London close 00:00 and shut-off 03:00 → midpoint 01:30 → trigger candle opens **01:25** and the order fires on its close at 01:30.

### Fixed-time orders: gates
Before placing, order 1 and order 2 must pass:

1. **EMA_TREND(9) gate** — same rule as `EMA_MAIN` in Full Trading: longs only when the trigger close is **above** the trend EMA, shorts only **below** it. The tracker runs on every closed 5-min bar and is warmed from history at startup.
2. **Freshness guard** — the trigger candle must have closed within `STALE_TRIGGER_MAX_MINUTES` of *now*. After a restart the bot replays the last ~25 minutes of closed bars; a replayed trigger is skipped (logged) instead of firing a market order at a stale price. A skipped/missed trigger is not fatal: the phase machine falls through at Asia close / shut-off.

---

## Volume profiles

Three Fixed-Range profiles per day, each returning **VAH / POC / VAL** with a 70% value area:

| | Window | Used for |
|--|--------|----------|
| VP1 | Asia open → London open | Order 1 target |
| VP2 | Asia open → Asia close (final candle included) | Full-Trading levels + Order 2 |
| VP3 | London open → London close (final candle included) | Order 2 |

### Methodology: time-at-price from 1-minute MIDPOINT bars
IDEALPRO spot FX has **no traded-volume data** on IB: `TRADES` is not a valid `whatToShow` for CASH contracts (bar volume is reported as −1), and paginating `BID_ASK`/`MIDPOINT` ticks across an 8–9 hour session means hundreds of 1000-tick requests, which violates IB's historical-data pacing limits.

So the profile is built from **real 1-minute MIDPOINT bars** (one single `reqHistoricalData` call per profile): each bar distributes 1.0 unit of weight uniformly across the price buckets covered by its high–low range (200 buckets across the window's range). The POC is the heaviest bucket and the value area expands greedily from the POC until 70% of the total weight is covered — the classic FRVP algorithm with *time* replacing volume, which is the standard proxy for spot FX where no central tape exists. If 1-min bars are unavailable the computation automatically retries with 5-min bars.

The histogram math lives in `volume_profile.bars_to_profile()`, a pure function with no IB dependency, so it can be unit-tested offline.

> Future idea: CME GBP futures (6B) have real exchange volume and track GBPUSD closely — a futures-based profile would give true volume weights at the cost of a second data subscription.

---

## Position sizing

Every order (order 1, order 2 and every Full-Trading slot) is sized as **`TRADE_QUANTITY` × live NetLiquidation**, converted from the account currency into the pair's base currency:

```
qty_GBP = NetLiquidation[EUR]  ×  TRADE_QUANTITY  ×  EURGBP_midpoint
```

`sizing.compute_order_quantity()` fetches NetLiquidation from `accountSummary()` and a 1-min midpoint EURGBP rate (direct pair first, inverse as fallback), caches the result for `SIZING_CACHE_MINUTES`, and falls back to `TOTAL_CAPITAL × TRADE_QUANTITY` with a warning if IB data is unavailable — order placement never silently sizes to zero.

> **IDEALPRO minimum size:** IB may reject very small FX orders (historically ~USD 25,000 equivalent on IDEALPRO). 20% of a €100k paper account ≈ 17,100 GBP is usually fine, but if you shrink the account or the fraction, watch for rejections and set `MIN_ORDER_QUANTITY` to get warned.

## Order closing: per-order flattening

Whenever the bot closes an order (Asia close for order 1, shut-off for order 2 and slots, FVG/EMA cancellation rules), it flattens **exactly the quantity that order traded, in that order's own direction** — never the net symbol position. This matters because the net position aggregates order 1/2 *and* every Full-Trading slot: its sign and size can be unrelated to any single order, and closing "the position" from several slots at once would cascade into massive over-closing. Each close therefore: cancels that order's bracket children, short-circuits if the TP already filled, then sends a market order for the recorded/filled quantity with the action derived from the stored direction.

---

## Full Trading engine

Runs continuously from **Asia close → shut-off** (through London close and the NY session). Two independent layers:

### Parent scanner (every closed bar)
Checks the four levels — **AH, AL, VAH_2, VAL_2** — for a break: a long break opens below a level and closes above it on a green candle; a short break is the mirror image. Multiple levels crossed by one bar form a single break event. A break opens a new slot if fewer than `MAX_SLOTS` are active.

### Slot pipeline (per break event)

```
C2 closes breaking ≥1 level
  ↓ Stage 1  FVG detection on C3:  long  → C3.low  > C1.high  (gap [C1.high, C3.low])
  ↓                                short → C3.high < C1.low   (gap [C3.high, C1.low])
  ↓          no FVG → slot discarded
  ↓ Stage 2  1st confirmation: price retraces INTO the FVG
  ↓          (full close beyond the far edge of the gap → discard)
  ↓ Stage 3  2nd confirmation: price exits the FVG on the breakout side AND
  ↓          closes beyond a broken level AND on the right side of EMA(100);
  ↓          a failed level/EMA check steps back to Stage 2
  ↓ Stage 4  Bracket order: entry MKT, SL = C1 extreme,
  ↓          TP = entry ± 2.75 × risk, qty from live sizing
  ↓ Stage 5  ORDER_LAUNCHED: cancelled if price closes back through the FVG
  ↓          or the wrong side of the EMA before the fill is seen
  ↓ Stage 6  MONITORING: slot freed when the TP or SL child fills
```

### EMA warm-up
Both EMAs (`EMA_MAIN` in the engine, `EMA_TREND` in the strategy) **output a running SMA during warm-up** — from the very first bar they return the SMA of the closes seen so far, and once `period` closes have accumulated the SMA seeds the EMA. On top of that, the engine is **back-filled with ~1 day of historical 5-min bars at creation**, so EMA(100) is fully ready the moment Full Trading starts at Asia close.

---

## Crash recovery

All state is persisted to `bot_state.json` after every meaningful event. On restart, `bootstrap()` restores: phase, session boundaries, datetime anchors (from the **stored** trade date, so overnight sessions re-anchor correctly), VP1–VP3, AH/AL, the persisted Asia bars (re-fetched from IB history if missing), order 1/2 records **including their IB order ids and quantities**, all slot state machines (ids, FVG bounds, order ids, quantities; the slot-id counter is re-synced to avoid collisions), and re-attaches to in-flight IB orders by `orderId`.

Recovery behaviours worth knowing:

| Scenario | Behaviour |
|----------|-----------|
| Restart before order 1 window | Resumes waiting; trigger protected by the freshness guard |
| Restart with order 1/2 open | Re-attaches by order id; TP fill detected, or closed at the boundary using the recorded qty/direction |
| Restart mid-Full-Trading | Slots restored; a MONITORING slot whose parent Trade can't be recovered watches its bracket children (child filled → slot freed; children gone → slot freed); an ORDER_LAUNCHED slot whose parent vanished but whose children are alive is treated as filled |
| Restart at/after shut-off | Phase DONE → state resets to today, waits for the next Asia open |
| State ≥2 days old (long outage) | Stale session force-closed (orders/slots re-attached first), then a fresh day starts |

**Limitation (by design):** candles that elapsed while the bot was down are used to re-warm the EMAs and bar history, but they are **not replayed through slot state machines or the break scanner** — slots simply resume on the next live bar. Replaying them would fire market orders on stale prices.

## Day-roll rules (main loop)

Every 30 s the loop runs `_ensure_strategy()`:

* **State is for today** → resume (create the strategy if missing).
* **Mid-session state from yesterday** → the overnight tail of a session that crossed midnight (e.g. Friday→Saturday): keep running under the stored date until `DONE`. Bars keep being fetched on Saturday morning for this tail.
* **Mid-session state ≥2 days old** → stale: force the shut-off, then roll.
* **Otherwise** → roll to today: start fresh on trading days, reset-and-wait on weekends.

A **wall-clock safety net** also forces the shut-off if the clock passes the boundary by >10 minutes while the session is still open (e.g. a data outage stops the bar that would normally trigger it). Holidays are deliberately ignored (Mon–Fri = trading days).

---

## Running the bot

```bash
python main.py          # logs to stdout + bot.log, state in bot_state.json
```

Stop with Ctrl-C (state is saved before exit). To start completely fresh:

```bash
rm bot_state.json
```

## Paper trading checklist

- [ ] TWS / Gateway running on the **paper** account, API enabled, port matches `IB_PORT`
- [ ] Account base currency in IBKR matches `ACCOUNT_CURRENCY` (EUR)
- [ ] `SYMBOL` correct; `TRADE_QUANTITY` reviewed (0.10–0.20)
- [ ] `EMA_MAIN`, `EMA_TREND`, `MAX_SLOTS`, `RISK_REWARD` reviewed
- [ ] Run at least one full week in paper mode, including a Friday→Saturday tail and one deliberate mid-session restart
- [ ] Check `bot.log` after each session for `ERROR`/`CRITICAL` entries
- [ ] Verify `bot_state.json` after each restart scenario
