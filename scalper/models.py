"""Plain data types shared by the strategy, broker and engine."""

from __future__ import annotations

from dataclasses import dataclass, fields
from datetime import datetime
from enum import Enum
from typing import Any, TypeVar


class Side(str, Enum):
    LONG = "LONG"
    SHORT = "SHORT"

    @property
    def sign(self) -> int:
        return 1 if self is Side.LONG else -1


@dataclass(frozen=True)
class Bar:
    """One OHLC candle. ``time`` is the bar's open time, timezone-aware UTC."""

    time: datetime
    open: float
    high: float
    low: float
    close: float


@dataclass
class EntryOrder:
    """Market entry queued on a signal bar; it fills at the next bar's open."""

    symbol: str
    side: Side
    qty: int
    stop_loss: float
    take_profit: float
    signal_time: datetime
    signal_price: float
    risk_distance: float
    quote_rate: float  # account-currency value of 1 unit of the quote currency


@dataclass
class Position:
    id: int
    symbol: str
    side: Side
    qty: int
    entry_time: datetime
    entry_price: float
    stop_loss: float
    take_profit: float
    signal_time: datetime
    signal_price: float
    quote_rate: float
    bars_held: int = 0


@dataclass
class Trade:
    id: int
    symbol: str
    side: Side
    qty: int
    entry_time: datetime
    entry_price: float
    exit_time: datetime
    exit_price: float
    exit_reason: str
    stop_loss: float
    take_profit: float
    pips: float
    pnl_quote: float
    commission: float
    pnl: float  # net profit in account currency
    bars_held: int


@dataclass(frozen=True)
class FillEvent:
    kind: str  # "ENTRY" or "EXIT"
    symbol: str
    time: datetime
    side: Side
    qty: int
    price: float
    reason: str = ""
    trade: Trade | None = None


T = TypeVar("T")


def to_dict(obj: Any) -> dict[str, Any]:
    """Serialise one of the dataclasses above into JSON-friendly values."""
    out: dict[str, Any] = {}
    for f in fields(obj):
        value = getattr(obj, f.name)
        if isinstance(value, datetime):
            value = value.isoformat()
        elif isinstance(value, Side):
            value = value.value
        out[f.name] = value
    return out


def from_dict(cls: type[T], data: dict[str, Any]) -> T:
    """Inverse of :func:`to_dict`."""
    kwargs: dict[str, Any] = {}
    for f in fields(cls):  # type: ignore[arg-type]
        if f.name not in data:
            continue
        value = data[f.name]
        if f.name == "side":
            value = Side(value)
        elif f.name == "time" or f.name.endswith("_time"):
            value = datetime.fromisoformat(value)
        kwargs[f.name] = value
    return cls(**kwargs)
