"""
indicators.py — Cálculo de indicadores técnicos.
"""


from typing import List, Optional


def ema(closes: List[float], length: int) -> Optional[float]:
    """
    Calcula el EMA estándar (Exponential Moving Average).
    """
    if len(closes) < length:
        return None

    k    = 2.0 / (length + 1)
    val  = sum(closes[:length]) / length   # Seed: SMA inicial

    for close in closes[length:]:
        val = close * k + val * (1.0 - k)

    return val
