from datetime import datetime, timezone

import pytest
from helpers import ref_atr, ref_ema, ref_rsi, ref_sma

from scalper.config import StrategyParams
from scalper.feeds import generate_synthetic
from scalper.instruments import Instrument
from scalper.models import Bar, Side
from scalper.session import SessionWindow
from scalper.strategy import ReverseRSIStrategy


def reference_signals(bars: list[Bar], p: StrategyParams) -> dict[datetime, Side]:
    """The Pine entry conditions, recomputed from scratch with list-based formulas."""
    closes = [b.close for b in bars]
    rsi_ma = ref_sma(ref_rsi(closes, p.rsi_length), p.rsi_ma_length)
    emas = [ref_ema(closes, n) for n in p.ema_lengths]
    atr = ref_atr(bars, p.atr_length)
    session = SessionWindow.parse(p.session, p.session_timezone)
    out = {}
    for i in range(1, len(bars)):
        if rsi_ma[i] is None or rsi_ma[i - 1] is None or atr[i] is None:
            continue
        if any(e[i] is None for e in emas) or not session.contains(bars[i].time):
            continue
        chain = [closes[i]] + [e[i] for e in emas]
        bear = all(a < b for a, b in zip(chain, chain[1:]))
        bull = all(a > b for a, b in zip(chain, chain[1:]))
        if rsi_ma[i] < p.lower_limit <= rsi_ma[i - 1] and bear:
            out[bars[i].time] = Side.LONG
        elif rsi_ma[i] > p.upper_limit >= rsi_ma[i - 1] and bull:
            out[bars[i].time] = Side.SHORT
    return out


@pytest.mark.parametrize("symbol,seed", [("USDCHF", 3), ("CHFJPY", 4), ("GBPAUD", 5)])
def test_signals_match_reference_implementation(symbol, seed):
    p = StrategyParams()
    bars = generate_synthetic([symbol], days=40, seed=seed)[symbol]
    strat = ReverseRSIStrategy(p, Instrument.from_symbol(symbol))
    got = {}
    for b in bars:
        snap = strat.update(b)
        if snap.signal:
            got[b.time] = snap.signal
    want = reference_signals(bars, p)
    assert len(want) > 10, "test data should produce plenty of signals"
    assert got == want
    assert {Side.LONG, Side.SHORT} <= set(got.values())


def test_signals_only_inside_session_and_levels_follow_atr():
    p = StrategyParams()
    bars = generate_synthetic(["AUDCAD"], days=30, seed=11)["AUDCAD"]
    strat = ReverseRSIStrategy(p, Instrument.from_symbol("AUDCAD"))
    ny = SessionWindow.parse("1600-1900", "America/New_York")
    signals = 0
    for b in bars:
        snap = strat.update(b)
        if not snap.signal:
            continue
        signals += 1
        local = b.time.astimezone(ny.tz)
        assert 16 <= local.hour < 19
        risk = snap.atr * p.atr_mult * p.sl_multiple
        assert snap.risk_distance == pytest.approx(risk)
        sign = snap.signal.sign
        assert snap.stop_loss == pytest.approx(b.close - sign * risk)
        assert snap.take_profit == pytest.approx(b.close + sign * risk * 1.5)
        if snap.signal is Side.LONG:
            assert snap.long_trigger and snap.ribbon_bear
        else:
            assert snap.short_trigger and snap.ribbon_bull
    assert signals > 0


def test_session_parameter_changes_trading_hours():
    p = StrategyParams(session="0200-0500", session_timezone="UTC")
    bars = generate_synthetic(["USDCHF"], days=30, seed=2)["USDCHF"]
    strat = ReverseRSIStrategy(p, Instrument.from_symbol("USDCHF"))
    hours = {b.time.hour for b in bars if strat.update(b).signal}
    assert hours and hours <= {2, 3, 4}


def test_chf_blackout_january_2015():
    p = StrategyParams(session="0000-2359", session_timezone="UTC")
    start = datetime(2014, 12, 20, tzinfo=timezone.utc)
    synthetic = generate_synthetic(["USDCHF", "AUDCAD"], days=40, seed=9, start=start)
    for symbol, bars in synthetic.items():
        strat = ReverseRSIStrategy(p, Instrument.from_symbol(symbol))
        times = [b.time for b in bars if strat.update(b).signal]
        in_blackout = [t for t in times if datetime(2015, 1, 1, tzinfo=timezone.utc) <= t <= datetime(2015, 1, 18, tzinfo=timezone.utc)]
        if symbol == "USDCHF":
            assert not in_blackout
        else:
            assert in_blackout  # non-CHF pairs keep trading
