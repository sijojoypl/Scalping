"""Signal logic of ``Strategy - Reverse RSI.pine``.

Long when, on a bar that opens inside the session window:
  * the 5-bar SMA of RSI(20) crosses under 49, and
  * the EMA ribbon is stacked bearish: close < ema4 < ema10 < ema15 < ema19 < ema25.

Short is the mirror image: RSI SMA crosses over 55 with a bullish ribbon.
It is a fade: the bot buys weakness and sells strength during the quiet
Sydney hours.

Stop loss = signal close -/+ ATR(14) * atr_mult * sl_multiple.
Take profit = signal close +/- the same distance * profit_multiple.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone

from scalper.config import StrategyParams
from scalper.indicators import ATR, EMA, RSI, SMA, crossover, crossunder
from scalper.instruments import Instrument
from scalper.models import Bar, Side
from scalper.session import SessionWindow

# The Pine script skips CHF pairs around the January 2015 SNB de-peg.
_CHF_BLACKOUT = (
    datetime(2015, 1, 1, tzinfo=timezone.utc),
    datetime(2015, 1, 18, tzinfo=timezone.utc),
)


@dataclass(frozen=True)
class Snapshot:
    """Indicator values and entry decision at the close of one bar."""

    time: datetime
    close: float
    rsi: float | None
    rsi_ma: float | None
    emas: tuple[float | None, ...]
    atr: float | None
    in_session: bool
    long_trigger: bool
    short_trigger: bool
    ribbon_bear: bool
    ribbon_bull: bool
    signal: Side | None
    risk_distance: float | None
    stop_loss: float | None
    take_profit: float | None


class ReverseRSIStrategy:
    def __init__(self, params: StrategyParams, instrument: Instrument) -> None:
        self.params = params
        self.instrument = instrument
        self.session = SessionWindow.parse(params.session, params.session_timezone)
        self._rsi = RSI(params.rsi_length)
        self._rsi_ma = SMA(params.rsi_ma_length)
        self._emas = [EMA(n) for n in params.ema_lengths]
        self._atr = ATR(params.atr_length)
        self._prev_rsi_ma: float | None = None
        self.bars_seen = 0

    @property
    def ready(self) -> bool:
        return (
            self._rsi_ma.value is not None
            and self._atr.value is not None
            and all(e.value is not None for e in self._emas)
        )

    def update(self, bar: Bar) -> Snapshot:
        """Feed one closed bar; returns the state at that bar's close."""
        p = self.params
        self.bars_seen += 1
        rsi = self._rsi.update(bar.close)
        prev_rsi_ma = self._prev_rsi_ma
        rsi_ma = self._rsi_ma.update(rsi) if rsi is not None else None
        self._prev_rsi_ma = rsi_ma
        emas = tuple(e.update(bar.close) for e in self._emas)
        atr = self._atr.update(bar.high, bar.low, bar.close)

        long_trigger = crossunder(prev_rsi_ma, rsi_ma, p.lower_limit, p.lower_limit)
        short_trigger = crossover(prev_rsi_ma, rsi_ma, p.upper_limit, p.upper_limit)

        ribbon_bear = ribbon_bull = False
        if all(e is not None for e in emas):
            chain = (bar.close, *emas)
            ribbon_bear = all(a < b for a, b in zip(chain, chain[1:]))
            ribbon_bull = all(a > b for a, b in zip(chain, chain[1:]))

        in_session = self.session.contains(bar.time)
        allowed = in_session and atr is not None and atr > 0 and not self._blackout(bar.time)

        signal: Side | None = None
        if allowed and long_trigger and ribbon_bear:
            signal = Side.LONG
        elif allowed and short_trigger and ribbon_bull:
            signal = Side.SHORT

        risk = stop = target = None
        if atr is not None:
            risk = atr * p.atr_mult * p.sl_multiple
            if signal is not None:
                stop = bar.close - signal.sign * risk
                target = bar.close + signal.sign * risk * p.profit_multiple

        return Snapshot(
            time=bar.time,
            close=bar.close,
            rsi=rsi,
            rsi_ma=rsi_ma,
            emas=emas,
            atr=atr,
            in_session=in_session,
            long_trigger=long_trigger,
            short_trigger=short_trigger,
            ribbon_bear=ribbon_bear,
            ribbon_bull=ribbon_bull,
            signal=signal,
            risk_distance=risk,
            stop_loss=stop,
            take_profit=target,
        )

    def _blackout(self, moment: datetime) -> bool:
        if "CHF" not in (self.instrument.base, self.instrument.quote):
            return False
        return _CHF_BLACKOUT[0] <= moment <= _CHF_BLACKOUT[1]
