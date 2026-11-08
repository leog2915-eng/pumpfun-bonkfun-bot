"""Decimal parsing. Floats are accepted only via their shortest decimal string."""

from __future__ import annotations

import math
from decimal import ROUND_HALF_EVEN, Decimal, InvalidOperation

MONEY = Decimal("0.00000001")
ENTRY_AGENTS = frozenset({"scout", "whale", "sniper"})
SUPPORTED_CHAINS = frozenset({"sol", "base", "bsc"})


def dec(value: object) -> Decimal | None:
    """Parse a finite decimal. `None` for blanks, booleans, and non-numbers."""
    if isinstance(value, bool) or value is None:
        return None
    if isinstance(value, Decimal):
        out = value
    elif isinstance(value, int):
        out = Decimal(value)
    elif isinstance(value, float):
        if not math.isfinite(value):
            return None
        out = Decimal(str(value))
    elif isinstance(value, str):
        text = value.strip()
        if not text:
            return None
        try:
            out = Decimal(text)
        except InvalidOperation:
            return None
    else:
        return None
    if not out.is_finite():
        return None
    return out


def money(value: Decimal) -> Decimal:
    """Quantize a USD amount to 8 decimal places."""
    return value.quantize(MONEY, rounding=ROUND_HALF_EVEN)


def as_text(value: Decimal | None) -> str | None:
    """Store a decimal without binary float noise."""
    if value is None:
        return None
    return format(money(value), "f")
