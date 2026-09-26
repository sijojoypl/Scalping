from datetime import datetime, timedelta, timezone

import pytest
from helpers import no_cost_config

from scalper.backtest import run_backtest
from scalper.breakout import LondonBreakoutStrategy
from scalper.config import BreakoutParams, ConfigError
from scalper.feeds import generate_synthetic
from scalper.instruments import Instrument
from scalper.models import Bar, Position, Side

UTC = timezone.utc
M5 = timedelta(minutes=5)
EURUSD = Instrument.from_symbol("EURUSD")


def day_bars(day: datetime, range_hi=1.1050, range_lo=1.1000, after=()):
    """00:00-06:55 bars inside [lo, hi], then the given (close) prices from 07:00 on."""
    bars, t = [], day
    for i in range(84):  # 7 hours of M5
        mid = range_lo + (range_hi - range_lo) * (0.5 + 0.4 * (-1) ** i)
        bars.append(Bar(t, mid, range_hi if i == 10 else mid + 0.0002, range_lo if i == 20 else mid - 0.0002, mid))
        t += M5
    prev = bars[-1].close
    for c in after:
        bars.append(Bar(t, prev, max(prev, c) + 0.0001, min(prev, c) - 0.0001, c))
        prev, t = c, t + M5
    return bars


def run(bars, **params):
    strat = LondonBreakoutStrategy(BreakoutParams(**params), EURUSD, spread_pips=1.0, timeframe_minutes=5)
    return [(b.time, strat.update(b)) for b in bars], strat


def signals(results):
    return [(t, s) for t, s in results if s.signal]


def test_long_breakout_with_stop_at_range_middle():
    winter = datetime(2026, 1, 6, tzinfo=UTC)  # London = UTC in January
    # 50-pip range, buffer 5 pips: 1.1054 is not enough, 1.1060 is
    results, _ = run(day_bars(winter, after=[1.1030, 1.1054, 1.1060, 1.1080, 1.1090]))
    sigs = signals(results)
    assert len(sigs) == 1
    t, snap = sigs[0]
    assert t == winter + timedelta(hours=7, minutes=10)
    assert snap.signal is Side.LONG
    assert snap.stop_loss == pytest.approx(1.1025)
    assert snap.risk_distance == pytest.approx(0.0035)
    assert snap.take_profit == pytest.approx(1.1095)


def test_short_breakout_with_opposite_stop_and_bigger_target():
    day = datetime(2026, 1, 6, tzinfo=UTC)
    results, _ = run(day_bars(day, after=[1.1010, 1.0990]), stop="opposite", profit_multiple=2.0)
    (_, snap), = signals(results)
    assert snap.signal is Side.SHORT
    assert snap.stop_loss == pytest.approx(1.1050)
    assert snap.take_profit == pytest.approx(1.0990 - 2 * 0.0060)


def test_follows_london_summer_time():
    summer = datetime(2026, 7, 7, tzinfo=UTC) - timedelta(hours=1)  # 00:00 London (BST) = 23:00 UTC
    results, _ = run(day_bars(summer, after=[1.1060]))
    (t, _), = signals(results)
    assert t.astimezone(UTC).hour == 6  # 07:00 London in summer


def test_only_inside_entry_window_and_once_per_day():
    day = datetime(2026, 1, 6, tzinfo=UTC)
    quiet = [1.1030] * 36  # 07:00-09:55, nothing happens
    results, _ = run(day_bars(day, after=quiet + [1.1070]))  # breakout at 10:00: too late
    assert signals(results) == []
    results, _ = run(day_bars(day, after=[1.1060, 1.1030, 1.1070]))  # breaks, falls back, breaks again
    assert len(signals(results)) == 1
    results, _ = run(day_bars(day, after=[1.1060, 1.1030, 1.1070]), trades_per_day=2)
    assert len(signals(results)) == 2  # the second one needed a fresh cross
    results, _ = run(day_bars(day, after=[1.1060, 1.1065, 1.1070]), trades_per_day=2)
    assert len(signals(results)) == 1  # staying above the range is not a new breakout


def test_narrow_ranges_and_thin_days_are_skipped():
    day = datetime(2026, 1, 6, tzinfo=UTC)
    results, _ = run(day_bars(day, range_hi=1.1005, after=[1.1030]))  # ~8 pips with wicks < 10 spreads
    assert signals(results) == []
    thin = day_bars(day, after=[1.1060])[60:]  # range window mostly missing
    results, _ = run(thin)
    assert signals(results) == []


def test_flatten_at_exit_time_and_at_day_end():
    strat = LondonBreakoutStrategy(BreakoutParams(), EURUSD, 1.0, 5)
    opened = datetime(2026, 1, 6, 7, 15, tzinfo=UTC)
    pos = Position(1, "EURUSD", Side.LONG, 1000, opened, 1.1, 1.09, 1.11, opened, 1.1, 1.0)

    def bar_at(h, m, day=6):
        t = datetime(2026, 1, day, h, m, tzinfo=UTC)
        return Bar(t, 1.1, 1.1, 1.1, 1.1)

    assert not strat.should_flatten(bar_at(15, 50), pos)
    assert strat.should_flatten(bar_at(15, 55), pos)  # closes at 16:00
    assert strat.should_flatten(bar_at(0, 5, day=7), pos)


@pytest.mark.parametrize(
    "changes,message",
    [
        ({"range_window": "0700-0000"}, "start before it ends"),
        ({"entry_window": "0600-0900"}, "after range_window"),
        ({"exit_time": "0900"}, "exit_time"),
        ({"stop": "wide"}, "stop"),
        ({"timezone": "Mars/Olympus"}, "timezone"),
    ],
)
def test_bad_breakout_settings(changes, message):
    cfg = no_cost_config()
    cfg.strategy_name = "london_breakout"
    for k, v in changes.items():
        setattr(cfg.breakout, k, v)
    with pytest.raises(ConfigError, match=message):
        cfg.validate()


def breakout_config(**overrides):
    cfg = no_cost_config(symbols=["EURUSD", "GBPUSD", "USDJPY"])
    cfg.strategy_name = "london_breakout"
    cfg.breakout.min_range_spreads = 0
    for k, v in overrides.items():
        setattr(cfg.breakout, k, v)
    return cfg.validate()


def test_backtest_trades_follow_the_rules():
    from zoneinfo import ZoneInfo

    cfg = breakout_config()
    cfg.costs.spread_pips = {"default": 1.0}
    data = generate_synthetic(cfg.symbols, days=40, seed=5)
    result = run_backtest(cfg, data)
    london = ZoneInfo("Europe/London")
    assert len(result.trades) > 30
    per_day = {}
    for t in result.trades:
        signal = (t.entry_time - M5).astimezone(london)
        assert 7 <= signal.hour < 10, t
        key = (t.symbol, signal.date())
        per_day[key] = per_day.get(key, 0) + 1
        exit_local = t.exit_time.astimezone(london)
        if t.exit_reason == "TIME":
            assert (exit_local + M5).strftime("%H:%M") == "16:00"
        else:
            assert (exit_local + M5).time() <= datetime(2026, 1, 1, 16, 0).time()
    assert max(per_day.values()) == 1
    reasons = {t.exit_reason.split()[0] for t in result.trades}
    assert {"TP", "SL", "TIME"} <= reasons


def test_time_exit_pays_half_spread_and_slippage():
    cfg = breakout_config()
    cfg.costs.spread_pips = {"default": 2.0}
    cfg.costs.slippage_pips = 0.5
    data = generate_synthetic(cfg.symbols, days=40, seed=5)
    result = run_backtest(cfg, data)
    bars = {s: {b.time: b for b in bs} for s, bs in data.items()}
    timed = [t for t in result.trades if t.exit_reason == "TIME"]
    assert timed
    for t in timed:
        pip = Instrument.from_symbol(t.symbol).pip_size
        close = bars[t.symbol][t.exit_time].close
        assert t.exit_price == pytest.approx(close - t.side.sign * 1.5 * pip)


def test_breakout_paper_replay_survives_a_restart(tmp_path):
    from test_runner import replay, trade_keys

    from scalper.clock import SimClock
    from scalper.feeds import ReplayFeed
    from scalper.runner import PaperTrader
    from scalper.storage import read_trades

    cfg = breakout_config()
    cfg.feed.history_bars = 400
    cfg.feed.poll_delay_seconds = 15
    data = generate_synthetic(cfg.symbols, days=20, seed=8)
    replay(cfg, data, tmp_path / "straight")
    straight = read_trades(tmp_path / "straight" / "paper_trades.csv")
    assert len(straight) > 5

    first, clock = replay(cfg, data, tmp_path / "split", max_cycles=1800)
    PaperTrader(cfg, ReplayFeed(data, 5), SimClock(clock.now()), tmp_path / "split").run()
    assert trade_keys(read_trades(tmp_path / "split" / "paper_trades.csv")) == trade_keys(straight)
