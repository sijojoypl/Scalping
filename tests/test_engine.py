from datetime import datetime, timedelta, timezone

import pytest
from helpers import no_cost_config

from scalper.backtest import merged_stream, run_backtest
from scalper.engine import build_engine, pine_round
from scalper.feeds import generate_synthetic
from scalper.models import Bar, Side

UTC = timezone.utc


def first_signal_run(cfg, symbol="USDCHF", seed=3, days=40):
    """Feed bars until the first order is queued; returns engine, bars, index."""
    data = generate_synthetic(cfg.symbols + ["USDJPY"], days=days, seed=seed)
    engine = build_engine(cfg)
    for i, b in enumerate(data[symbol]):
        engine.on_bar(symbol, b)
        if engine.broker.pending:
            return engine, data[symbol], i
    raise AssertionError("no signal in test data")


def test_pine_round():
    assert pine_round(2.5) == 3
    assert pine_round(3.5) == 4
    assert pine_round(2.49) == 2
    assert pine_round(-2.5) == -3


def test_position_size_risks_one_percent_of_initial_capital():
    cfg = no_cost_config(symbols=["USDCHF"])
    engine, bars, i = first_signal_run(cfg)
    order = engine.broker.pending["USDCHF"]
    snap = engine.last_snapshot["USDCHF"]
    chf_usd = 1 / bars[i].close
    assert order.quote_rate == pytest.approx(chf_usd)
    assert order.qty == pine_round(50_000 * 0.01 / snap.risk_distance / chf_usd)
    # a full stop-out loses ~1% (the rate drifts a little between entry and exit)
    assert order.qty * snap.risk_distance * chf_usd == pytest.approx(500, abs=1)


def test_cross_pair_uses_conversion_pair_rate():
    cfg = no_cost_config(symbols=["CHFJPY"])
    engine = build_engine(cfg)
    assert engine.aux_symbols == ["USDJPY"]
    data = generate_synthetic(["CHFJPY", "USDJPY"], days=40, seed=4)
    for symbol, b in merged_stream(data, {"USDJPY"}):
        engine.on_bar(symbol, b)
        if engine.broker.pending:
            break
    order = engine.broker.pending["CHFJPY"]
    usd_jpy = engine.last_close["USDJPY"]
    assert order.quote_rate == pytest.approx(1 / usd_jpy)


def test_one_position_per_symbol():
    cfg = no_cost_config(symbols=["USDCHF"])
    data = generate_synthetic(["USDCHF"], days=60, seed=7)
    result = run_backtest(cfg, data)
    trades = sorted(result.trades, key=lambda t: t.entry_time)
    for a, b in zip(trades, trades[1:]):
        assert b.entry_time >= a.exit_time
    assert result.engine.stats["skipped: position already open"] > 0


def test_stale_bars_never_open_trades():
    cfg = no_cost_config(symbols=["USDCHF"])
    data = generate_synthetic(["USDCHF"], days=40, seed=3)
    engine = build_engine(cfg)
    for b in data["USDCHF"]:
        engine.on_bar("USDCHF", b, allow_entries=False)
    assert engine.stats["orders"] == 0
    assert engine.stats["skipped: stale bar (catch-up after downtime)"] > 0


def test_max_open_positions_guard():
    cfg = no_cost_config()
    cfg.risk.max_open_positions = 1
    data = generate_synthetic(cfg.symbols + ["USDJPY", "USDCAD", "AUDUSD"], days=60, seed=7)
    result = run_backtest(cfg, data)
    trades = sorted(result.trades, key=lambda t: t.entry_time)
    for a, b in zip(trades, trades[1:]):
        assert b.entry_time >= a.exit_time
    assert result.engine.stats["skipped: max open positions reached"] > 0


def test_daily_loss_limit_blocks_new_entries():
    cfg = no_cost_config(symbols=["USDCHF"])
    cfg.risk.max_daily_loss_pct = 0.5  # one full stop-out is ~1%
    _, bars, i = first_signal_run(cfg)
    signal_day = bars[i].time.date().isoformat()

    # Replay the same bars on a fresh engine that already lost 0.6% that day.
    engine = build_engine(cfg)
    engine.broker.daily_pnl[signal_day] = -300.0
    for b in bars[: i + 1]:
        engine.on_bar("USDCHF", b)
    assert not engine.broker.pending
    assert engine.stats["skipped: daily loss limit hit"] == 1


def test_duplicate_and_old_bars_ignored():
    cfg = no_cost_config(symbols=["USDCHF"])
    engine = build_engine(cfg)
    t = datetime(2026, 1, 6, tzinfo=UTC)
    b1 = Bar(t, 1, 1, 1, 1)
    b0 = Bar(t - timedelta(minutes=5), 1, 1, 1, 1)
    engine.on_bar("USDCHF", b1)
    engine.on_bar("USDCHF", b1)
    engine.on_bar("USDCHF", b0)
    assert engine.strategies["USDCHF"].bars_seen == 1


def test_merged_stream_keeps_symbols_apart_and_orders_aux_first():
    t = datetime(2026, 1, 6, tzinfo=UTC)
    data = {
        "USDCHF": [Bar(t, 1, 1, 1, 1), Bar(t + timedelta(minutes=5), 2, 2, 2, 2)],
        "USDJPY": [Bar(t, 150, 150, 150, 150)],
        "AUDCAD": [Bar(t, 0.9, 0.9, 0.9, 0.9)],
    }
    out = [(s, b.close) for s, b in merged_stream(data, {"USDJPY"})]
    assert out == [("USDJPY", 150), ("AUDCAD", 0.9), ("USDCHF", 1), ("USDCHF", 2)]


def test_no_cost_backtest_has_one_to_one_point_five_payoff():
    cfg = no_cost_config()
    data = generate_synthetic(cfg.symbols + ["USDJPY", "USDCAD", "AUDUSD"], days=60, seed=7)
    result = run_backtest(cfg, data)
    assert result.stats.trades > 50
    for t in result.trades:
        target = 750 if t.exit_reason == "TP" else -500
        assert t.pnl == pytest.approx(target, rel=0.03), t
        assert t.side in (Side.LONG, Side.SHORT)


def test_backtest_window_only_trades_after_start():
    cfg = no_cost_config()
    data = generate_synthetic(cfg.symbols + ["USDJPY", "USDCAD", "AUDUSD"], days=40, seed=7)
    last = max(bars[-1].time for bars in data.values())
    start = last - timedelta(days=30)
    full = run_backtest(cfg, data)
    window = run_backtest(cfg, data, start=start)
    assert window.trades and window.warmup_bars > 0
    assert min(t.entry_time for t in window.trades) > start
    # Same rules as the full run once both are flat: every windowed trade that
    # starts after the full run's last pre-window trade closed is identical.
    settled = max((t.exit_time for t in full.trades if t.entry_time <= start), default=start)
    key = lambda t: (t.symbol, t.entry_time, t.exit_time, t.exit_reason, t.qty)  # noqa: E731
    later_full = [key(t) for t in full.trades if t.entry_time > settled]
    later_window = [key(t) for t in window.trades if t.entry_time > settled]
    assert later_window == later_full


def test_report_shows_average_stop_and_spread_share():
    from scalper.report import avg_stop_pips, per_symbol_table

    cfg = no_cost_config(symbols=["USDCHF"])
    data = generate_synthetic(["USDCHF"], days=30, seed=7)
    trades = run_backtest(cfg, data).trades
    risks = [abs(t.take_profit - t.stop_loss) / 2.5 / 0.0001 for t in trades]
    assert avg_stop_pips(trades, 1.5) == pytest.approx(sum(risks) / len(risks))
    table = per_symbol_table(trades, 50_000, "USD", 1.5, lambda s: 1.5)
    row = table.splitlines()[1]
    stop = avg_stop_pips(trades, 1.5)
    assert f"{stop:.1f}p" in row and f"{1.5 / stop * 100:.0f}%" in row


def test_stop_filter_skips_small_stops_and_counts_them():
    cfg = no_cost_config(symbols=["USDCHF"])
    cfg.costs.spread_pips = {"default": 1.5}
    data = generate_synthetic(["USDCHF"], days=40, seed=7)
    base = run_backtest(cfg, data)
    stops = sorted(abs(t.take_profit - t.stop_loss) / 2.5 / 0.0001 for t in base.trades)
    ratio = stops[len(stops) // 2] / 1.5  # a threshold between the smallest and largest stops

    cfg.risk.min_stop_spread_ratio = ratio
    filtered = run_backtest(cfg, data)
    assert filtered.engine.stats["skipped: stop too small for the spread"] > 0
    assert 0 < len(filtered.trades) < len(base.trades)
    for t in filtered.trades:
        assert abs(t.take_profit - t.stop_loss) / 2.5 / 0.0001 >= ratio * 1.5 - 1e-9


def test_no_cost_run_takes_the_same_filtered_trades():
    cfg = no_cost_config(symbols=["USDCHF"])
    cfg.costs.spread_pips = {"default": 1.5}
    cfg.risk.min_stop_spread_ratio = 7  # 10.5 pips; synthetic USDCHF stops sit around 10
    data = generate_synthetic(["USDCHF"], days=40, seed=7)
    charged = run_backtest(cfg, data)
    assert charged.engine.stats["skipped: stop too small for the spread"] > 0
    free = run_backtest(cfg, data, charge_costs=False)
    key = lambda t: (t.entry_time, t.side)  # noqa: E731
    assert [key(t) for t in free.trades][:5] == [key(t) for t in charged.trades][:5]
    assert free.engine.stats["skipped: stop too small for the spread"] > 0
    assert free.stats.net_profit > charged.stats.net_profit


def test_no_entry_windows_block_signals_inside_them():
    from datetime import time as dtime
    from zoneinfo import ZoneInfo

    cfg = no_cost_config()
    data = generate_synthetic(cfg.symbols + ["USDJPY", "USDCAD", "AUDUSD"], days=40, seed=7)
    base = run_backtest(cfg, data)

    cfg.risk.no_entry_windows = ["1645-1730"]
    cfg.validate()
    result = run_backtest(cfg, data)
    ny = ZoneInfo("America/New_York")
    assert result.engine.stats["skipped: inside a no-entry window"] > 0
    assert 0 < len(result.trades) < len(base.trades)
    for t in result.trades:
        signal = (t.entry_time - timedelta(minutes=5)).astimezone(ny).time()
        assert not (dtime(16, 45) <= signal < dtime(17, 30)), t

    cfg.risk.no_entry_windows = ["1600-1900"]  # the whole session
    assert run_backtest(cfg, data).trades == []


def test_breakdown_table_splits_by_side_and_signal_time():
    from scalper.report import breakdown_table

    cfg = no_cost_config()
    data = generate_synthetic(cfg.symbols + ["USDJPY", "USDCAD", "AUDUSD"], days=40, seed=7)
    trades = run_backtest(cfg, data).trades
    table = breakdown_table(trades, 50_000, "America/New_York")
    rows = {line.split()[0]: int(line.split()[1]) for line in table.splitlines()[1:] if "Signal" not in line}
    assert rows["LONG"] + rows["SHORT"] == len(trades)
    buckets = {k: v for k, v in rows.items() if ":" in k}
    assert sum(buckets.values()) == len(trades)
    assert set(buckets) <= {"16:00-16:30", "16:30-17:00", "17:00-17:30", "17:30-18:00", "18:00-18:30", "18:30-19:00"}


def test_missing_conversion_rate_warns_once_per_currency(caplog):
    cfg = no_cost_config(symbols=["EURGBP"])
    cfg.fx_fallback_rates = {}
    data = generate_synthetic(["EURGBP"], days=40, seed=7)
    with caplog.at_level("WARNING"):
        result = run_backtest(cfg, data)
    assert result.trades == []
    assert result.engine.stats["skipped: no conversion rate"] > 1
    assert caplog.text.count("signals are skipped: no GBP/USD price") == 1
