"""
session_summary.py — Resumen de P&L de la sesión más reciente.
───────────────────────────────────────────────────────────────
Lee bot.log y muestra cuánto se ha ganado o perdido en el día. También puede consultarse en cualquier momento mientras el bot está corriendo.

Uso:
    python session_summary.py              # Resumen del día de hoy
    python session_summary.py --all        # Todas las sesiones del log
    python session_summary.py --date 2024-03-15   # Sesión específica
"""


import argparse
import re
import sys
from collections import defaultdict
from dataclasses import dataclass
from typing import Dict, List, Optional

import config


# ─────────────────────────────────────────────────────────────────────────────
# Patrones de log
# ─────────────────────────────────────────────────────────────────────────────
# Línea de orden lanzada:
#   "🚀 Orden lanzada [LONG] | entry=480.50  sl=478.20  tp=486.54  riesgo=2.30  id=abc123"
_RE_ORDER = re.compile(
    r"(\d{4}-\d{2}-\d{2}).*"
    r"Orden lanzada \[(\w+)\].*"
    r"entry=([\d.]+).*sl=([\d.]+).*tp=([\d.]+)"
)

# Posición cerrada por TP/SL (bracket de Alpaca):
#   "✅ Posición cerrada (TP o SL alcanzado)"
_RE_CLOSED_AUTO = re.compile(
    r"(\d{4}-\d{2}-\d{2}).*Posición cerrada \(TP o SL alcanzado\)"
)

# Posición cerrada a mercado por EMA o corte de tiempo:
#   "⚡ EMA roto" o "⏰ Corte N min — Cerrando posición"
_RE_CLOSED_MARKET = re.compile(
    r"(\d{4}-\d{2}-\d{2}).*(EMA roto|Cerrando posición abierta a mercado)"
)

# Nueva sesión iniciada
_RE_SESSION = re.compile(
    r"(\d{4}-\d{2}-\d{2}).*Nueva sesión: (\d{4}-\d{2}-\d{2})"
)

# Orden cancelada sin ejecutar
_RE_CANCELLED = re.compile(
    r"(\d{4}-\d{2}-\d{2}).*cancelando orden.*→ WAITING_BREAK"
)


@dataclass
class SessionEvent:
    log_date: str
    event:    str
    detail:   str = ""

@dataclass
class OrderInfo:
    date:      str
    direction: str
    entry:     float
    sl:        float
    tp:        float
    outcome:   str  = ""

def _parse_log(log_path: str) -> Dict[str, List[OrderInfo]]:
    """Parsea bot.log y reconstruye las órdenes con su resultado."""

    orders_by_date: Dict[str, List[OrderInfo]] = defaultdict(list)
    pending: Optional[OrderInfo] = None

    try:
        with open(log_path, encoding="utf-8") as f:
            lines = f.readlines()
    except FileNotFoundError:
        print(f"No se encontró {log_path}. ¿Has ejecutado el bot al menos una vez?")
        sys.exit(1)

    for line in lines:
        m = _RE_ORDER.search(line)
        if m:
            log_date, direction, entry, sl, tp = m.groups()
            pending = OrderInfo(
                date      = log_date,
                direction = direction,
                entry     = float(entry),
                sl        = float(sl),
                tp        = float(tp),
                outcome   = "OPEN",
            )
            continue

        m = _RE_CLOSED_AUTO.search(line)
        if m and pending:
            log_date = m.group(1)
            pending.outcome = "AUTO (TP/SL)"
            orders_by_date[pending.date].append(pending)
            pending = None
            continue

        m = _RE_CLOSED_MARKET.search(line)
        if m and pending:
            reason = "EMA_EXIT" if "EMA" in m.group(2) else "MARKET_CLOSE"
            pending.outcome = reason
            orders_by_date[pending.date].append(pending)
            pending = None
            continue

        m = _RE_CANCELLED.search(line)
        if m and pending:
            pending.outcome = "CANCELLED"
            orders_by_date[pending.date].append(pending)
            pending = None
            continue

    if pending:
        orders_by_date[pending.date].append(pending)

    return orders_by_date

def _print_session(date_str: str, orders: List[OrderInfo]):
    print(f"\n  📅 Sesión: {date_str}")
    if not orders:
        print("     Sin órdenes ejecutadas.")
        return

    for i, o in enumerate(orders, 1):
        icon = {
            "AUTO (TP/SL)":  "🏁",
            "EMA_EXIT":      "⚡",
            "MARKET_CLOSE":  "⏰",
            "CANCELLED":     "🚫",
            "OPEN":          "🔄",
        }.get(o.outcome, "❓")

        print(
            f"     {i}. {icon} {o.direction:5s} | "
            f"entry={o.entry:.2f}  sl={o.sl:.2f}  tp={o.tp:.2f} | "
            f"Resultado: {o.outcome}"
        )

    print()
    print("  ℹ️  Para el P&L exacto en dinero real, consulta tu cuenta")
    print(f"     Alpaca en https://app.alpaca.markets o con:")
    print(f"     python session_summary.py --alpaca")

def _print_alpaca_pnl():
    """Conecta a Alpaca y muestra el P&L real de la cuenta."""
    try:
        from alpaca.trading.client import TradingClient
        from alpaca.trading.requests import GetOrdersRequest
        from alpaca.trading.enums import QueryOrderStatus

        tc = TradingClient(
            api_key    = config.ALPACA_API_KEY,
            secret_key = config.ALPACA_SECRET_KEY,
            paper      = config.PAPER,
        )

        account = tc.get_account()
        equity       = float(account.equity)
        last_equity  = float(account.last_equity)
        day_pnl      = equity - last_equity
        sign         = "+" if day_pnl >= 0 else ""
        mode         = "PAPER" if config.PAPER else "LIVE"

        print(f"\n{'═'*55}")
        print(f"   P&L DEL DÍA ({mode})")
        print(f"{'═'*55}")
        print(f"  Equity actual:    {equity:>12.2f}")
        print(f"  Equity ayer:      {last_equity:>12.2f}")
        print(f"  P&L del día:      {sign}{day_pnl:>11.2f}")
        print(f"  Cash disponible:  {float(account.cash):>12.2f}")
        print(f"{'═'*55}\n")

        from datetime import datetime, timezone, timedelta
        today_start = datetime.now(timezone.utc).replace(
            hour=0, minute=0, second=0, microsecond=0
        )
        orders = tc.get_orders(
            GetOrdersRequest(
                status = QueryOrderStatus.CLOSED,
                after  = today_start,
            )
        )

        relevant = [o for o in orders if str(o.symbol) == config.SYMBOL]
        if relevant:
            print(f"  Órdenes de {config.SYMBOL} hoy:")
            for o in relevant:
                print(
                    f"    {o.side} {o.qty} @ {o.filled_avg_price or '—'} "
                    f"[{o.status}]"
                )
        else:
            print(f"  Sin órdenes cerradas de {config.SYMBOL} hoy.")
        print()

    except Exception as e:
        print(f"Error al conectar con Alpaca: {e}")


def main():
    parser = argparse.ArgumentParser(
        description="Resumen de P&L del bot de trading."
    )
    parser.add_argument(
        "--all", action="store_true",
        help="Mostrar todas las sesiones del log"
    )
    parser.add_argument(
        "--date", type=str, default=None,
        help="Mostrar solo la sesión de una fecha concreta (YYYY-MM-DD)"
    )
    parser.add_argument(
        "--alpaca", action="store_true",
        help="Conectar a Alpaca y mostrar el P&L real de la cuenta"
    )
    args = parser.parse_args()

    if args.alpaca:
        if not config.ALPACA_API_KEY:
            print("Error: Faltan las claves de Alpaca en .env")
            sys.exit(1)
        _print_alpaca_pnl()
        return

    orders_by_date = _parse_log(config.LOG_FILE)

    print(f"\n{'═'*55}")
    print(f"   RESUMEN DE SESIONES — {config.SYMBOL}")
    print(f"{'═'*55}")

    if not orders_by_date:
        print("\n  No hay sesiones con órdenes en el log todavía.")
        print()
        return

    if args.date:
        if args.date in orders_by_date:
            _print_session(args.date, orders_by_date[args.date])
        else:
            print(f"\n  No hay órdenes para la fecha {args.date}.")
        print()
        return

    if args.all:
        for d in sorted(orders_by_date.keys()):
            _print_session(d, orders_by_date[d])
        print()
        return

    latest = max(orders_by_date.keys())
    _print_session(latest, orders_by_date[latest])
    print()
    print("  Otros usos:")
    print("    python session_summary.py --all           # Todas las sesiones")
    print("    python session_summary.py --date 2024-03-15")
    print("    python session_summary.py --alpaca        # P&L real de Alpaca")
    print()


if __name__ == "__main__":
    main()