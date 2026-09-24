"""Streaming indicators that follow TradingView's Pine formulas.

Each indicator is fed one value per bar and returns ``None`` until it has
enough data, mirroring Pine's ``na``.
"""

from __future__ import annotations

from collections import deque


class SMA:
    def __init__(self, length: int) -> None:
        self.length = length
        self._window: deque[float] = deque(maxlen=length)
        self.value: float | None = None

    def update(self, x: float) -> float | None:
        self._window.append(x)
        self.value = sum(self._window) / self.length if len(self._window) == self.length else None
        return self.value


class RMA:
    """Pine ``rma``: Wilder smoothing (alpha = 1/length) seeded with an SMA."""

    def __init__(self, length: int) -> None:
        self.length = length
        self._seed: list[float] = []
        self.value: float | None = None

    def update(self, x: float) -> float | None:
        if self.value is None:
            self._seed.append(x)
            if len(self._seed) == self.length:
                self.value = sum(self._seed) / self.length
                self._seed = []
        else:
            alpha = 1.0 / self.length
            self.value = alpha * x + (1.0 - alpha) * self.value
        return self.value


class EMA:
    """Pine ``ema``: alpha = 2/(length+1), seeded with an SMA."""

    def __init__(self, length: int) -> None:
        self.length = length
        self._seed: list[float] = []
        self.value: float | None = None

    def update(self, x: float) -> float | None:
        if self.value is None:
            self._seed.append(x)
            if len(self._seed) == self.length:
                self.value = sum(self._seed) / self.length
                self._seed = []
        else:
            alpha = 2.0 / (self.length + 1)
            self.value = alpha * x + (1.0 - alpha) * self.value
        return self.value


class RSI:
    """The RSI written out in the Pine script::

        up   = rma(max(change(src), 0), len)
        down = rma(-min(change(src), 0), len)
        rsi  = down == 0 ? 100 : up == 0 ? 0 : 100 - 100 / (1 + up / down)
    """

    def __init__(self, length: int) -> None:
        self._prev: float | None = None
        self._up = RMA(length)
        self._down = RMA(length)
        self.value: float | None = None

    def update(self, x: float) -> float | None:
        if self._prev is None:  # change(src) is na on the first bar
            self._prev = x
            return None
        change = x - self._prev
        self._prev = x
        up = self._up.update(max(change, 0.0))
        down = self._down.update(-min(change, 0.0))
        if up is None or down is None:
            self.value = None
        elif down == 0:
            self.value = 100.0
        elif up == 0:
            self.value = 0.0
        else:
            self.value = 100.0 - 100.0 / (1.0 + up / down)
        return self.value


class ATR:
    """Pine ``atr(length)`` = ``rma(tr(true), length)``."""

    def __init__(self, length: int) -> None:
        self._rma = RMA(length)
        self._prev_close: float | None = None
        self.value: float | None = None

    def update(self, high: float, low: float, close: float) -> float | None:
        if self._prev_close is None:
            tr = high - low  # tr(true) falls back to high - low without a previous close
        else:
            tr = max(high - low, abs(high - self._prev_close), abs(low - self._prev_close))
        self._prev_close = close
        self.value = self._rma.update(tr)
        return self.value


def crossover(prev_a: float | None, a: float | None, prev_b: float, b: float) -> bool:
    """Pine ``crossover``: ``a > b and a[1] <= b[1]``."""
    return prev_a is not None and a is not None and a > b and prev_a <= prev_b


def crossunder(prev_a: float | None, a: float | None, prev_b: float, b: float) -> bool:
    """Pine ``crossunder``: ``a < b and a[1] >= b[1]``."""
    return prev_a is not None and a is not None and a < b and prev_a >= prev_b
