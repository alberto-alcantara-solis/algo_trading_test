"""
indicators.py — Cálculo de indicadores técnicos.
"""


from typing import List, Optional


def ema(closes: List[float], length: int, seed: Optional[float] = None) -> Optional[float]:
    """
    Calcula el EMA estándar (Exponential Moving Average).
    """
    if len(closes) < length:
        if seed is None:
            return None
        k   = 2.0 / (length + 1)
        val = seed
        for close in closes:
            val = close * k + val * (1.0 - k)
        return val

    k   = 2.0 / (length + 1)
    val = sum(closes[:length]) / length
    for close in closes[length:]:
        val = close * k + val * (1.0 - k)
    return val
