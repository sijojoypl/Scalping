"""London breakout: trade the first clean break of the overnight range.

Each day, in the strategy's timezone (London by default):

1. Bars opening inside ``range_window`` (00:00-07:00) set the day's high and low.
2. Inside ``entry_window`` (07:00-10:00) the first bar that *closes* beyond the
   range by ``buffer_frac`` of its height is a signal: long above, short below.
   It fills at the next bar's open, like every entry in this bot.
3. The stop goes to the middle of the range (or its far side), the target is
   ``profit_multiple`` times that distance, and whatever is still open at
   ``exit_time`` is closed at the market.

Days whose range is narrower than ``min_range_spreads`` spreads are skipped:
on those the spread would eat too much of the stop.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo

from scalper.config import BreakoutParams, _hhmm, _window
from scalper.instruments import Instrument
from scalper.models import Bar, Position, Side

# A range needs at least this share of its window's bars (holiday-thin days are skipped).
MIN_RANGE_COVERAGE = 0.5


@dataclass(frozen=True)
class BreakoutSnapshot:
    time: datetime
    close: float
    range_high: float | None
    range_low: float | None
    signal: Side | None = None
    risk_distance: float | None = None
    stop_loss: float | None = None
    take_profit: float | None = None
    note: str = ""


class LondonBreakoutStrategy:
    def __init__(self, params: BreakoutParams, instrument: Instrument, spread_pips: float, timeframe_minutes: int):
        self.params = params
        self.instrument = instrument
        self.tz = ZoneInfo(params.timezone)
        self.range_start, self.range_end = _window(params.range_window)
        self.entry_start, self.entry_end = _window(params.entry_window)
        self.exit_time = _hhmm(params.exit_time)
        self.tf = timedelta(minutes=timeframe_minutes)
        self.spread = spread_pips * instrument.pip_size
        window_minutes = (
            self.range_end.hour * 60 + self.range_end.minute - self.range_start.hour * 60 - self.range_start.minute
        )
        self.min_range_bars = max(1, int(window_minutes / timeframe_minutes * MIN_RANGE_COVERAGE))
        self.bars_seen = 0
        self._day: date | None = None
        self._high: float | None = None
        self._low: float | None = None
        self._range_bars = 0
        self._signals_today = 0
        self._prev_close: float | None = None

    @property
    def ready(self) -> bool:
        return self.bars_seen > 0  # no indicators to warm up

    def update(self, bar: Bar) -> BreakoutSnapshot:
        self.bars_seen += 1
        local = bar.time.astimezone(self.tz)
        if local.date() != self._day:
            self._day = local.date()
            self._high = self._low = None
            self._range_bars = 0
            self._signals_today = 0
        prev_close, self._prev_close = self._prev_close, bar.close
        t = local.time().replace(tzinfo=None)

        if self.range_start <= t < self.range_end:
            self._high = bar.high if self._high is None else max(self._high, bar.high)
            self._low = bar.low if self._low is None else min(self._low, bar.low)
            self._range_bars += 1
            return self._snap(bar)

        if not (self.entry_start <= t < self.entry_end) or not self._range_ok():
            return self._snap(bar)
        if self._signals_today >= self.params.trades_per_day:
            return self._snap(bar)

        assert self._high is not None and self._low is not None
        height = self._high - self._low
        buffer = height * self.params.buffer_frac
        above, below = self._high + buffer, self._low - buffer
        side: Side | None = None
        if bar.close > above and (prev_close is None or prev_close <= above):
            side = Side.LONG
        elif bar.close < below and (prev_close is None or prev_close >= below):
            side = Side.SHORT
        if side is None:
            return self._snap(bar)

        if self.params.stop == "mid":
            stop = (self._high + self._low) / 2
        else:
            stop = self._low if side is Side.LONG else self._high
        risk = abs(bar.close - stop)
        if risk <= 0:
            return self._snap(bar)
        self._signals_today += 1
        inst = self.instrument
        return self._snap(
            bar,
            signal=side,
            risk_distance=risk,
            stop_loss=stop,
            take_profit=bar.close + side.sign * risk * self.params.profit_multiple,
            note=f"range {inst.fmt(self._low)}-{inst.fmt(self._high)}, {height / inst.pip_size:.1f} pips",
        )

    def should_flatten(self, bar: Bar, position: Position) -> bool:
        """Close at ``exit_time``, and never carry a trade into the next day."""
        closes_at = (bar.time + self.tf).astimezone(self.tz)
        opened = position.entry_time.astimezone(self.tz)
        if closes_at.date() != opened.date():
            return True
        return closes_at.time().replace(tzinfo=None) >= self.exit_time

    def _range_ok(self) -> bool:
        if self._high is None or self._low is None or self._range_bars < self.min_range_bars:
            return False
        return self._high - self._low >= self.params.min_range_spreads * self.spread

    def _snap(self, bar: Bar, **kw) -> BreakoutSnapshot:
        return BreakoutSnapshot(time=bar.time, close=bar.close, range_high=self._high, range_low=self._low, **kw)
