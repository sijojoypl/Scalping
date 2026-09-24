from pathlib import Path

import pytest

from scalper.cli import main
from scalper.config import ConfigError, load_config

ROOT = Path(__file__).resolve().parent.parent


def test_shipped_config_is_paper_with_pine_defaults():
    cfg = load_config(ROOT / "config" / "paper.yaml")
    assert cfg.mode == "PAPER"
    assert cfg.symbols == ["USDCHF", "CHFJPY", "AUDCAD", "GBPAUD"]
    assert cfg.timeframe_minutes == 5
    s = cfg.strategy
    assert (s.rsi_length, s.rsi_ma_length, s.upper_limit, s.lower_limit) == (20, 5, 55, 49)
    assert s.ema_lengths == [4, 10, 15, 19, 25]
    assert (s.session, s.atr_length, s.atr_mult) == ("1600-1900", 14, 2.0)
    assert (s.profit_multiple, s.sl_multiple, s.risk_per_trade) == (1.5, 1.0, 0.01)
    assert cfg.account.initial_capital == 50_000


@pytest.mark.parametrize(
    "yaml_text,message",
    [
        ("mode: LIVE\n", "only trades on paper"),
        ("strategy:\n  rsi_lenght: 14\n", "unknown config key"),
        ("symbols: [EURUSDX]\n", "six-letter"),
        ("feed:\n  provider: ftx\n", "feed.provider"),
        ("account:\n  currency: EUR\n", "USD"),
        ("risk:\n  no_entry_windows: ['16:45-17:30']\n", "no_entry_windows"),
    ],
)
def test_bad_configs_rejected(tmp_path, yaml_text, message):
    p = tmp_path / "c.yaml"
    p.write_text(yaml_text)
    with pytest.raises(ConfigError, match=message):
        load_config(p)


def write_config(tmp_path, extra=""):
    p = tmp_path / "paper.yaml"
    p.write_text(
        "feed:\n  provider: synthetic\n  synthetic_days: 6\n  history_bars: 300\n"
        f"state_dir: {tmp_path / 'state'}\nlog_dir: {tmp_path / 'logs'}\n{extra}"
    )
    return str(p)


def test_cli_backtest_synthetic(tmp_path, capsys):
    out = tmp_path / "trades.csv"
    assert main(["backtest", "--synthetic", "--days", "20", "--trades-out", str(out)]) == 0
    text = capsys.readouterr().out
    assert "Profit factor" in text and "USDCHF" in text
    assert out.exists()


def test_cli_paper_replay_then_status_and_reset(tmp_path, capsys):
    cfg = write_config(tmp_path)
    assert main(["-c", cfg, "paper"]) == 0
    assert "Replay result" in capsys.readouterr().out
    replay_dir = tmp_path / "state" / "replay-synthetic"
    assert (replay_dir / "paper_state.json").exists()

    assert main(["-c", cfg, "status", "--state-dir", str(replay_dir)]) == 0
    assert "Balance" in capsys.readouterr().out

    assert main(["-c", cfg, "reset", "--state-dir", str(replay_dir)]) == 1  # needs --yes
    assert (replay_dir / "paper_state.json").exists()
    assert main(["-c", cfg, "reset", "--state-dir", str(replay_dir), "--yes"]) == 0
    assert not (replay_dir / "paper_state.json").exists()


def test_cli_paper_refuses_live_mode(tmp_path, capsys):
    cfg = write_config(tmp_path, "mode: LIVE\n")
    assert main(["-c", cfg, "paper"]) == 2
    assert "only trades on paper" in capsys.readouterr().err


def test_cli_status_without_account(tmp_path, capsys):
    cfg = write_config(tmp_path)
    assert main(["-c", cfg, "status"]) == 1


def test_cli_fetch_then_one_month_backtest(tmp_path, capsys, monkeypatch):
    """The documented workflow: fetch ~45 days, then backtest the last 30."""
    from datetime import datetime, timedelta, timezone

    import scalper.cli as cli
    from scalper.feeds import ReplayFeed, generate_synthetic

    now = datetime.now(timezone.utc).replace(second=0, microsecond=0)
    symbols = ["USDCHF", "CHFJPY", "AUDCAD", "GBPAUD", "USDJPY", "USDCAD", "AUDUSD"]
    data = generate_synthetic(symbols, days=60, seed=5, start=now - timedelta(days=60))
    monkeypatch.setattr(cli, "_live_feed", lambda cfg: ReplayFeed(data, 5))

    out = tmp_path / "data"
    assert main(["fetch", "--out", str(out)]) == 0
    assert len(list(out.glob("*.csv"))) == 7
    capsys.readouterr()

    assert main(["backtest", "--data-dir", str(out), "--days", "30"]) == 0
    text = capsys.readouterr().out
    period = next(line for line in text.splitlines() if line.startswith("Period"))
    first_day = datetime.strptime(period.split()[2], "%Y-%m-%d").date()
    assert abs((now.date() - first_day).days - 30) <= 1
    assert "warm-up bars before" in text and "Profit factor" in text


def test_cli_backtest_without_data_explains_fetch(tmp_path, capsys):
    assert main(["backtest", "--data-dir", str(tmp_path / "empty")]) == 2
    assert "scalper fetch" in capsys.readouterr().err


def test_cli_fetch_points_to_other_sources_when_yahoo_refuses(tmp_path, capsys, monkeypatch):
    import scalper.cli as cli
    from scalper.feeds import FeedError

    class Refusing:
        def fetch_closed(self, symbol, count, now):
            raise FeedError("Yahoo request failed after 4 tries: HTTP 429 Too Many Requests")

    monkeypatch.setattr(cli, "_live_feed", lambda cfg: Refusing())
    assert main(["fetch", "--out", str(tmp_path)]) == 1
    assert "--provider dukascopy" in capsys.readouterr().err


def test_cli_stop_filter_sweep_and_until(tmp_path, capsys):
    from scalper.feeds import generate_synthetic, save_csv

    symbols = ["USDCHF", "CHFJPY", "AUDCAD", "GBPAUD", "USDJPY", "USDCAD", "AUDUSD"]
    for s, bars in generate_synthetic(symbols, days=40, seed=3).items():
        save_csv(tmp_path / f"{s}_M5.csv", bars)

    args = ["backtest", "--data-dir", str(tmp_path), "--days", "20"]
    assert main(args + ["--min-stop-spread", "0", "4", "8"]) == 0
    out = capsys.readouterr().out
    rows = [line for line in out.splitlines() if line.strip().startswith(("off", "4x", "8x"))]
    assert len(rows) == 3
    trades = [int(r.split()[1] if r.split()[0] == "off" else r.split()[2]) for r in rows]
    assert trades[0] >= trades[1] >= trades[2]

    assert main(args + ["--min-stop-spread", "5"]) == 0
    assert "under 5x the spread" in capsys.readouterr().out

    assert main(args + ["--until", "2026-01-30"]) == 0
    period = next(line for line in capsys.readouterr().out.splitlines() if line.startswith("Period"))
    assert "-> 2026-01-29" in period

    assert main(args + ["--until", "30-01-2026"]) == 2

    assert main(args + ["--no-entry", "1645-1730"]) == 0
    out = capsys.readouterr().out
    assert "No entry: 1645-1730 (America/New_York)" in out and "Signal time (New York):" in out
