"""
backtest_run.py — CLI runner + report for the offline backtester.

Usage
-----
    python backtest_run.py DATA.csv [options]

    DATA.csv               HistData M1 ASCII file (e.g. DAT_ASCII_GBPUSD_M1_2023.csv)

Options
-------
    --qty N                Units of base currency per order        (default 20000,
                           ~ 20% of a 100k account like the live bot)
    --tz-offset H          UTC offset of the CSV timestamps         (default -5,
                           HistData ASCII = EST without DST; use 0 for UTC data)
    --start YYYY-MM-DD     First trading day to simulate (UTC+8 date)
    --end   YYYY-MM-DD     Last trading day to simulate  (UTC+8 date)
    --ambiguous sl|tp      When one M1 candle spans both TP and SL  (default sl,
                           i.e. pessimistic)
    --cost-pips P          Round-trip cost (spread+slippage) charged per trade,
                           in pips                                  (default 0)
    --out FILE.csv         Write the full trade list to a CSV       (default
                           backtest_trades.csv)

Examples
--------
    python backtest_run.py DAT_ASCII_GBPUSD_M1_2023.csv
    python backtest_run.py DAT_ASCII_GBPUSD_M1_2024.csv --start 2024-03-01 --end 2024-06-30
    python backtest_run.py mydata_utc.csv --tz-offset 0 --cost-pips 1.0 --qty 17000
"""

from __future__ import annotations

import argparse
import csv
import logging
import sys
from datetime import date
from pathlib import Path
from typing import Dict, List

from backtest_data import load_histdata_m1, resample_m1_to_m5
from backtest_engine import (
    BacktestEngine, SimTrade,
    COMPONENT_ORDER1, COMPONENT_ORDER2, COMPONENT_FT,
)

COMPONENTS = [COMPONENT_ORDER1, COMPONENT_ORDER2, COMPONENT_FT]
NICE = {COMPONENT_ORDER1: "Order 1", COMPONENT_ORDER2: "Order 2",
        COMPONENT_FT: "Full Trading"}


# ---------------------------------------------------------------------------
# Stats
# ---------------------------------------------------------------------------
def _stats(trades: List[SimTrade], cost_pips: float) -> Dict:
    closed = [t for t in trades if not t.is_open]
    pips = [t.pips(cost_pips) for t in closed]
    pnls = [t.pnl(cost_pips) for t in closed]
    wins = [p for p in pips if p > 0]
    losses = [p for p in pips if p < 0]
    gross_win = sum(p for p in pnls if p > 0)
    gross_loss = -sum(p for p in pnls if p < 0)
    reasons: Dict[str, int] = {}
    for t in closed:
        reasons[t.reason] = reasons.get(t.reason, 0) + 1
    return {
        "trades": len(closed),
        "wins": len(wins),
        "losses": len(losses),
        "win_rate": 100.0 * len(wins) / len(closed) if closed else 0.0,
        "total_pips": sum(pips),
        "avg_pips": sum(pips) / len(closed) if closed else 0.0,
        "total_pnl": sum(pnls),
        "profit_factor": (gross_win / gross_loss) if gross_loss > 0 else float("inf"),
        "best_pips": max(pips) if pips else 0.0,
        "worst_pips": min(pips) if pips else 0.0,
        "reasons": reasons,
    }


def _print_report(trades: List[SimTrade], engine: BacktestEngine,
                  cost_pips: float, qty: float) -> None:
    line = "=" * 86
    print()
    print(line)
    print("BACKTEST REPORT — session-anchored VP strategy (GBPUSD, M5 logic on M1 data)")
    print(line)
    print(f"Trading days simulated : {engine.days_completed}")
    print(f"Order size             : {qty:,.0f} units of base currency")
    print(f"Cost per trade         : {cost_pips:.1f} pips round-trip")
    print(f"Ambiguous M1 TP+SL bar : resolved as {engine.ambiguous.upper()} "
          f"({'pessimistic' if engine.ambiguous == 'sl' else 'optimistic'})")
    print()

    hdr = f"{'Component':<14}{'Trades':>7}{'Wins':>6}{'Loss':>6}{'Win %':>8}" \
          f"{'Pips':>10}{'Avg pips':>10}{'PnL (quote)':>14}{'PF':>7}"
    print(hdr)
    print("-" * len(hdr))

    total_pnl = 0.0
    total_pips = 0.0
    for comp in COMPONENTS + ["TOTAL"]:
        if comp == "TOTAL":
            sub = trades
        else:
            sub = [t for t in trades if t.component == comp]
        s = _stats(sub, cost_pips)
        pf = f"{s['profit_factor']:.2f}" if s["profit_factor"] != float("inf") else "inf"
        name = NICE.get(comp, "TOTAL")
        print(f"{name:<14}{s['trades']:>7}{s['wins']:>6}{s['losses']:>6}"
              f"{s['win_rate']:>7.1f}%{s['total_pips']:>10.1f}{s['avg_pips']:>10.2f}"
              f"{s['total_pnl']:>14.2f}{pf:>7}")
        if comp == "TOTAL":
            total_pnl, total_pips = s["total_pnl"], s["total_pips"]

    print()
    print("Exit reasons per component:")
    for comp in COMPONENTS:
        s = _stats([t for t in trades if t.component == comp], cost_pips)
        if s["trades"] == 0:
            print(f"  {NICE[comp]:<13}: no trades")
            continue
        rs = "  ".join(f"{k}={v}" for k, v in sorted(s["reasons"].items()))
        print(f"  {NICE[comp]:<13}: {rs}   (best {s['best_pips']:+.1f} / "
              f"worst {s['worst_pips']:+.1f} pips)")

    print()
    print("Comparison (share of total PnL):")
    for comp in COMPONENTS:
        s = _stats([t for t in trades if t.component == comp], cost_pips)
        share = (100.0 * s["total_pnl"] / abs(total_pnl)) if total_pnl else 0.0
        bar = "#" * max(0, min(40, int(abs(share) * 0.4)))
        print(f"  {NICE[comp]:<13}: {s['total_pnl']:>12.2f}  "
              f"({share:+6.1f}% of total)  {bar}")
    print(f"  {'TOTAL':<13}: {total_pnl:>12.2f}  ({total_pips:+.1f} pips)")
    print(line)
    print("NOTE: results are mid-price based. Run with --cost-pips ~0.5-1.5 to")
    print("approximate the GBPUSD spread; intrabar fills use real M1 candles, but")
    print("entries assume MKT fills at the M5 close (same as the live bot's design).")
    print(line)


def _format_report(trades: List[SimTrade], engine: BacktestEngine,
                   cost_pips: float, qty: float, title: str | None = None) -> str:
    line = "=" * 86
    lines = ["", line]
    lines.append(title or "BACKTEST REPORT — session-anchored VP strategy (GBPUSD, M5 logic on M1 data)")
    lines.append(line)
    lines.append(f"Trading days simulated : {engine.days_completed}")
    lines.append(f"Order size             : {qty:,.0f} units of base currency")
    lines.append(f"Cost per trade         : {cost_pips:.1f} pips round-trip")
    lines.append(f"Ambiguous M1 TP+SL bar : resolved as {engine.ambiguous.upper()} "
                 f"({'pessimistic' if engine.ambiguous == 'sl' else 'optimistic'})")
    lines.append("")

    hdr = f"{'Component':<14}{'Trades':>7}{'Wins':>6}{'Loss':>6}{'Win %':>8}"
    hdr += f"{'Pips':>10}{'Avg pips':>10}{'PnL (quote)':>14}{'PF':>7}"
    lines.append(hdr)
    lines.append("-" * len(hdr))

    total_pnl = 0.0
    for comp in COMPONENTS + ["TOTAL"]:
        if comp == "TOTAL":
            sub = trades
        else:
            sub = [t for t in trades if t.component == comp]
        s = _stats(sub, cost_pips)
        pf = f"{s['profit_factor']:.2f}" if s['profit_factor'] != float('inf') else "inf"
        name = NICE.get(comp, "TOTAL")
        lines.append(
            f"{name:<14}{s['trades']:>7}{s['wins']:>6}{s['losses']:>6}"
            f"{s['win_rate']:>7.1f}%{s['total_pips']:>10.1f}{s['avg_pips']:>10.2f}"
            f"{s['total_pnl']:>14.2f}{pf:>7}"
        )
        if comp == "TOTAL":
            total_pnl = s['total_pnl']

    lines.append("")
    lines.append("Exit reasons per component:")
    for comp in COMPONENTS:
        s = _stats([t for t in trades if t.component == comp], cost_pips)
        if s['trades'] == 0:
            lines.append(f"  {NICE[comp]:<13}: no trades")
            continue
        rs = "  ".join(f"{k}={v}" for k, v in sorted(s['reasons'].items()))
        lines.append(
            f"  {NICE[comp]:<13}: {rs}   (best {s['best_pips']:+.1f} / "
            f"worst {s['worst_pips']:+.1f} pips)"
        )

    lines.append("")
    lines.append("Comparison (share of total PnL):")
    for comp in COMPONENTS:
        s = _stats([t for t in trades if t.component == comp], cost_pips)
        share = (100.0 * s['total_pnl'] / abs(total_pnl)) if total_pnl else 0.0
        bar = "#" * max(0, min(40, int(abs(share) * 0.4)))
        lines.append(
            f"  {NICE[comp]:<13}: {s['total_pnl']:>12.2f}  "
            f"({share:+6.1f}% of total)  {bar}"
        )
    lines.append(f"  {'TOTAL':<13}: {total_pnl:>12.2f}  ({total_pnl:+.1f} pips)")
    lines.append(line)
    lines.append("NOTE: results are mid-price based. Run with --cost-pips ~0.5-1.5 to")
    lines.append("approximate the GBPUSD spread; intrabar fills use real M1 candles, but")
    lines.append("entries assume MKT fills at the M5 close (same as the live bot's design).")
    lines.append(line)
    return "\n".join(lines)


def _print_report(trades: List[SimTrade], engine: BacktestEngine,
                  cost_pips: float, qty: float) -> None:
    print(_format_report(trades, engine, cost_pips, qty))


def _write_csv(trades: List[SimTrade], path: str, cost_pips: float) -> None:
    with open(path, "w", newline="") as fh:
        w = csv.writer(fh)
        w.writerow(["trade_date", "component", "slot_id", "direction",
                    "entry_dt", "entry", "exit_dt", "exit", "reason",
                    "tp", "sl", "qty", "pips", "pnl"])
        for t in trades:
            if t.is_open:
                continue
            w.writerow([t.trade_date, t.component, t.slot_id or "", t.direction,
                        t.entry_dt.isoformat(), f"{t.entry:.5f}",
                        t.exit_dt.isoformat(), f"{t.exit:.5f}", t.reason,
                        f"{t.tp:.5f}" if t.tp is not None else "",
                        f"{t.sl:.5f}" if t.sl is not None else "",
                        f"{t.qty:.0f}", f"{t.pips(cost_pips):.1f}",
                        f"{t.pnl(cost_pips):.2f}"])
    print(f"Trade list written to: {path}")


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------
def _get_csv_files(path: str) -> List[str]:
    p = Path(path)
    if p.is_dir():
        files = sorted([str(f) for f in p.iterdir() if f.is_file() and f.suffix.lower() == '.csv'])
        return files
    if p.is_file():
        return [str(p)]
    return []


def _output_path(base_output: str, csv_path: str, multiple: bool) -> str:
    if not multiple:
        return base_output
    out_path = Path(base_output)
    base_name = Path(csv_path).stem
    if out_path.suffix:
        return str(out_path.with_name(f"{base_name}_{out_path.name}"))
    return str(out_path / f"{base_name}_trades.csv")


def main() -> int:
    ap = argparse.ArgumentParser(description="Offline backtest of the VP strategy "
                                             "on HistData M1 CSVs or a folder of CSVs.")
    ap.add_argument("csvfile", help="HistData M1 ASCII csv or folder containing CSV files")
    ap.add_argument("--qty", type=float, default=20000.0)
    ap.add_argument("--tz-offset", type=int, default=-5,
                    help="UTC offset of the CSV timestamps (HistData=-5)")
    ap.add_argument("--start", type=date.fromisoformat, default=None)
    ap.add_argument("--end", type=date.fromisoformat, default=None)
    ap.add_argument("--ambiguous", choices=["sl", "tp"], default="sl")
    ap.add_argument("--cost-pips", type=float, default=0.0)
    ap.add_argument("--out", default="backtest_trades.csv",
                    help="Base output filename for trade CSVs; when processing a folder, each file gets its own CSV")
    ap.add_argument("--report", default="report.txt",
                    help="Write the combined results report to a text file")
    ap.add_argument("--swap-tp-sl", action="store_true",
                    help="Brutal test: swap every trade's TP and SL and flip direction")
    ap.add_argument("-v", "--verbose", action="store_true")
    args = ap.parse_args()

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s  %(levelname)-7s %(name)s — %(message)s",
    )
    logging.getLogger("ib_insync").setLevel(logging.WARNING)

    csv_files = _get_csv_files(args.csvfile)
    if not csv_files:
        print("No CSV files found at the path — check format / path.")
        return 1

    report_sections: List[str] = []
    for csv_path in csv_files:
        print(f"\nProcessing: {csv_path}")
        m1 = load_histdata_m1(csv_path, tz_offset_hours=args.tz_offset)
        if not m1:
            print(f"No bars could be parsed from {csv_path} — skipping.")
            continue
        m5 = resample_m1_to_m5(m1)

        engine = BacktestEngine(m1, m5, qty=args.qty,
                                ambiguous=args.ambiguous, cost_pips=args.cost_pips)
        engine.swap_tp_sl = args.swap_tp_sl
        trades = engine.run(start=args.start, end=args.end)

        title = f"BACKTEST REPORT — {Path(csv_path).name}"
        section = _format_report(trades, engine, args.cost_pips, args.qty, title=title)
        print(section)
        report_sections.append(section)

        out_csv = _output_path(args.out, csv_path, len(csv_files) > 1)
        _write_csv(trades, out_csv, args.cost_pips)

    if not report_sections:
        print("No backtests completed successfully.")
        return 1

    with open(args.report, "w", encoding="utf-8", newline="") as fh:
        fh.write("\n\n".join(report_sections))
        fh.write("\n")
    print(f"\nCombined report written to: {args.report}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
