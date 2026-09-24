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
