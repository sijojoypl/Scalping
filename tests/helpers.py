from __future__ import annotations

from datetime import datetime, timedelta, timezone

from scalper.config import Config
from scalper.models import Bar

UTC = timezone.utc


def bar(t: datetime, o: float, h: float, l: float, c: float) -> Bar:
    return Bar(t, o, h, l, c)


def series(closes: list[float], start: datetime | None = None, spread: float = 0.0002) -> list[Bar]:
    """Bars whose open is the previous close, with a small wick either side."""
    start = start or datetime(2026, 1, 6, 12, 0, tzinfo=UTC)
    out, prev = [], closes[0]
    for i, c in enumerate(closes):
        o = prev
        out.append(Bar(start + timedelta(minutes=5 * i), o, max(o, c) + spread, min(o, c) - spread, c))
        prev = c
    return out


def no_cost_config(**overrides) -> Config:
    cfg = Config()
    cfg.costs.spread_pips = {"default": 0.0}
    cfg.costs.slippage_pips = 0.0
    cfg.costs.commission_per_100k = 0.0
    for k, v in overrides.items():
        setattr(cfg, k, v)
    return cfg.validate()


# ---- independent list-based versions of the Pine formulas -----------------
def ref_sma(src: list, n: int) -> list:
    out = []
    for i in range(len(src)):
        w = src[max(0, i - n + 1): i + 1]
        out.append(sum(w) / n if len(w) == n and all(v is not None for v in w) else None)
    return out


def _ref_smoothed(src: list, n: int, alpha: float) -> list:
    seed = ref_sma(src, n)
    out, prev = [], None
    for i, x in enumerate(src):
        prev = seed[i] if prev is None else alpha * x + (1 - alpha) * prev
        out.append(prev)
    return out


def ref_rma(src: list, n: int) -> list:
    return _ref_smoothed(src, n, 1 / n)


def ref_ema(src: list, n: int) -> list:
    return _ref_smoothed(src, n, 2 / (n + 1))


def ref_rsi(closes: list[float], n: int) -> list:
    change = [None] + [closes[i] - closes[i - 1] for i in range(1, len(closes))]
    up = ref_rma([None if c is None else max(c, 0.0) for c in change], n)
    down = ref_rma([None if c is None else -min(c, 0.0) for c in change], n)
    out = []
    for u, d in zip(up, down):
        if u is None or d is None:
            out.append(None)
        else:
            out.append(100.0 if d == 0 else 0.0 if u == 0 else 100 - 100 / (1 + u / d))
    return out


def ref_atr(bars: list[Bar], n: int) -> list:
    tr = [bars[0].high - bars[0].low]
    for prev, b in zip(bars, bars[1:]):
        tr.append(max(b.high - b.low, abs(b.high - prev.close), abs(b.low - prev.close)))
    return ref_rma(tr, n)
