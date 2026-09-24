"""Forex instrument metadata (base/quote currency and pip size)."""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class Instrument:
    symbol: str
    base: str
    quote: str
    pip_size: float

    @classmethod
    def from_symbol(cls, symbol: str) -> Instrument:
        s = normalize_symbol(symbol)
        base, quote = s[:3], s[3:]
        pip = 0.01 if quote == "JPY" else 0.0001
        return cls(symbol=s, base=base, quote=quote, pip_size=pip)

    @property
    def price_decimals(self) -> int:
        return 3 if self.quote == "JPY" else 5

    def fmt(self, price: float) -> str:
        return f"{price:.{self.price_decimals}f}"


def normalize_symbol(symbol: str) -> str:
    """``usd/chf``, ``USD_CHF`` and ``USDCHF`` all become ``USDCHF``."""
    s = symbol.upper().replace("/", "").replace("_", "").replace("-", "").strip()
    if len(s) != 6 or not s.isalpha():
        raise ValueError(f"not a six-letter forex pair: {symbol!r}")
    return s
