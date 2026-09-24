"""Command line interface: ``python -m scalper <command>``."""

from __future__ import annotations

import argparse
import logging
import logging.handlers
import shutil
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

from scalper.clock import RealClock, SimClock
from scalper.config import Config, ConfigError, load_config
from scalper.feeds import (
    DukascopyFeed,
    FeedError,
    OandaFeed,
    ReplayFeed,
    YahooFeed,
    generate_synthetic,
    load_csv_dir,
    save_csv,
)
from scalper.rates import RateBook
from scalper.report import compute_stats, format_stats, per_symbol_table
from scalper.runner import JOURNAL_FILE, STATE_FILE, PaperTrader
from scalper.storage import read_trades, write_trades

DEFAULT_CONFIG = "config/paper.yaml"
log = logging.getLogger("scalper")


def setup_logging(level: str, log_dir: str | None = None, filename: str = "paper.log") -> None:
    root = logging.getLogger()
    root.handlers.clear()
    root.setLevel(level.upper())
    fmt = logging.Formatter("%(asctime)s %(levelname)-7s %(message)s", "%Y-%m-%d %H:%M:%S")
    console = logging.StreamHandler(sys.stdout)
    console.setFormatter(fmt)
    root.addHandler(console)
    if log_dir:
        Path(log_dir).mkdir(parents=True, exist_ok=True)
        fh = logging.handlers.RotatingFileHandler(
            Path(log_dir) / filename, maxBytes=5_000_000, backupCount=5, encoding="utf-8"
        )
        fh.setFormatter(fmt)
        root.addHandler(fh)
    logging.getLogger("urllib3").setLevel(logging.WARNING)


def _config(args: argparse.Namespace) -> Config:
    path = args.config
    if path == DEFAULT_CONFIG and not Path(path).exists():
        path = None  # fall back to built-in defaults
    return load_config(path)


def _symbols_with_aux(cfg: Config) -> list[str]:
    return list(cfg.symbols) + RateBook(cfg.account.currency).required_aux(cfg.symbols)


def _replay_data(cfg: Config) -> dict:
    symbols = _symbols_with_aux(cfg)
    if cfg.feed.provider == "synthetic":
        return generate_synthetic(symbols, cfg.feed.synthetic_days, cfg.feed.synthetic_seed, cfg.timeframe_minutes)
    data = load_csv_dir(cfg.feed.data_dir, symbols, cfg.feed.csv_timezone)
    missing = [s for s in cfg.symbols if s not in data]
    if missing:
        raise ConfigError(f"no CSV in {cfg.feed.data_dir!r} for: {', '.join(missing)}")
    for pair in symbols[len(cfg.symbols):]:
        if pair not in data:
            log.warning("no CSV for conversion pair %s; fx_fallback_rates will be used", pair)
    return data


def _live_feed(cfg: Config):
    if cfg.feed.provider == "oanda":
        o = cfg.feed.oanda
        return OandaFeed.from_env(cfg.timeframe_minutes, o.token_env, o.environment, cfg.feed.request_timeout)
    return YahooFeed(cfg.timeframe_minutes, cfg.feed.request_timeout)


# ------------------------------------------------------------------ commands
def cmd_paper(args: argparse.Namespace) -> int:
    cfg = _config(args)
    if args.provider:
        cfg.feed.provider = args.provider
    if args.data_dir:
        cfg.feed.data_dir = args.data_dir
    cfg.validate()
    state_dir = Path(args.state_dir or cfg.state_dir)

    if cfg.feed.provider in ("csv", "synthetic"):
        # Offline replay against a simulated clock, in its own throwaway account.
        state_dir = state_dir / f"replay-{cfg.feed.provider}"
        if state_dir.exists():
            shutil.rmtree(state_dir)
        setup_logging(cfg.log_level, cfg.log_dir, "replay.log")
        feed = ReplayFeed(_replay_data(cfg), cfg.timeframe_minutes)
        clock = SimClock(feed.start_time(cfg.feed.history_bars))
        log.info("offline replay from %s; account files in %s", clock.now().isoformat(), state_dir)
        PaperTrader(cfg, feed, clock, state_dir).run()
        return _print_summary(cfg, state_dir)

    setup_logging(cfg.log_level, cfg.log_dir)
    trader = PaperTrader(cfg, _live_feed(cfg), RealClock(), state_dir)
    trader.run(once=args.once)
    return 0


def _print_summary(cfg: Config, state_dir: Path) -> int:
    trades = read_trades(state_dir / JOURNAL_FILE)
    stats = compute_stats(trades, cfg.account.initial_capital)
    print("\nReplay result (closed trades)")
    print(format_stats(stats, cfg.account.currency))
    print(per_symbol_table(trades, cfg.account.initial_capital, cfg.account.currency, cfg.strategy.profit_multiple))
    return 0


def _skip_summary(stats) -> str:
    skipped = sorted(((k[len("skipped: "):], v) for k, v in stats.items() if k.startswith("skipped: ")),
                     key=lambda kv: -kv[1])
    if not skipped:
        return ""
    return "  Skipped signals: " + ", ".join(f"{n} {reason}" for reason, n in skipped)


def cmd_backtest(args: argparse.Namespace) -> int:
    import copy

    from scalper.backtest import run_backtest

    cfg = _config(args)
    if args.symbols:
        cfg.symbols = args.symbols
    ratios = args.min_stop_spread or []
    if len(ratios) == 1:
        cfg.risk.min_stop_spread_ratio = ratios[0]
    cfg.validate()
    if len(ratios) > 1 and args.trades_out:
        raise ConfigError("--trades-out needs a single --min-stop-spread value")
    setup_logging(args.log_level or "WARNING")

    if args.synthetic:
        total_days = (args.days or 60) + (5 if args.days else 0)  # a few extra days to warm up
        data = generate_synthetic(_symbols_with_aux(cfg), total_days, args.seed, cfg.timeframe_minutes)
        source = f"synthetic data (seed {args.seed})"
    else:
        data_dir = args.data_dir or cfg.feed.data_dir
        data = load_csv_dir(data_dir, _symbols_with_aux(cfg), args.csv_tz or cfg.feed.csv_timezone)
        if not any(data.get(s) for s in cfg.symbols):
            raise ConfigError(
                f"no CSV data in {data_dir!r}. Download some with: python -m scalper fetch"
            )
        source = f"CSV files in {data_dir}"

    if args.until:
        try:
            until = datetime.strptime(args.until, "%Y-%m-%d").replace(tzinfo=timezone.utc)
        except ValueError as exc:
            raise ConfigError("--until takes a date like 2026-06-30") from exc
        data = {s: [b for b in bars if b.time < until] for s, bars in data.items()}
        if not any(data.get(s) for s in cfg.symbols):
            raise ConfigError(f"no data before {args.until}")

    traded = [b for s, b in data.items() if s in cfg.symbols and b]
    first = min(b[0].time for b in traded)
    last = max(b[-1].time for b in traded)
    start = None
    if args.days:
        start = last + timedelta(minutes=cfg.timeframe_minutes) - timedelta(days=args.days)
        if start <= first:
            print(
                f"warning: data only covers {(last - first).days} day(s); "
                f"testing all of it with no separate warm-up",
                file=sys.stderr,
            )
            start = None
    window_start = start or first
    costs_note = " (no costs)" if args.no_costs else ""
    print(f"Backtest: {', '.join(cfg.symbols)} M{cfg.timeframe_minutes} on {source}{costs_note}")
    print(f"Period  : {window_start:%Y-%m-%d %H:%M} -> {last:%Y-%m-%d %H:%M} UTC")

    if len(ratios) > 1:
        return _sweep(cfg, data, start, not args.no_costs, ratios, run_backtest, copy)

    result = run_backtest(cfg, data, start, charge_costs=not args.no_costs)
    print(
        f"Bars    : {result.bars_processed:,}"
        + (f" (+{result.warmup_bars:,} warm-up bars before)" if result.warmup_bars else "")
    )
    ratio = cfg.risk.min_stop_spread_ratio
    if ratio:
        print(f"Filter  : skip signals whose stop is under {ratio:g}x the spread")
    print(format_stats(result.stats, cfg.account.currency))
    print(
        per_symbol_table(
            result.trades,
            cfg.account.initial_capital,
            cfg.account.currency,
            cfg.strategy.profit_multiple,
            cfg.costs.spread_for,
        )
    )
    summary = _skip_summary(result.engine.stats)
    if summary:
        print(summary)
    open_pos = result.engine.broker.positions
    if open_pos:
        print(f"  Still open at the end: {', '.join(open_pos)}")
    if args.synthetic:
        print("  Note: synthetic prices are random. Use them to check the plumbing, not the edge.")
    if args.trades_out:
        write_trades(args.trades_out, result.trades)
        print(f"Trades written to {args.trades_out}")
    return 0


def _sweep(cfg, data, start, charge_costs, ratios, run_backtest, copy) -> int:
    """One backtest per --min-stop-spread value, summarised in one table."""
    cur = cfg.account.currency
    print("\nStop filter sweep (0 = off). Look for a range of values that all do well,")
    print("not the single best row: one lucky setting is usually noise.\n")
    print(f"  {'Min stop':>9} {'Trades':>7} {'Win %':>7} {'PF':>7} {'Net ' + cur:>13} {'Net %':>7} {'Max DD %':>9}")
    for ratio in ratios:
        run_cfg = copy.deepcopy(cfg)
        run_cfg.risk.min_stop_spread_ratio = ratio
        run_cfg.validate()
        st = run_backtest(run_cfg, data, start, charge_costs=charge_costs).stats
        label = "off" if not ratio else f"{ratio:g}x spr"
        pf = "n/a" if st.profit_factor is None else f"{st.profit_factor:.2f}"
        win = "n/a" if st.win_rate is None else f"{st.win_rate:.1f}"
        print(
            f"  {label:>9} {st.trades:>7} {win:>7} {pf:>7} {st.net_profit:>13,.2f} "
            f"{st.net_profit_pct:>6.2f}% {st.max_drawdown_pct:>8.2f}%",
            flush=True,
        )
    return 0


def cmd_status(args: argparse.Namespace) -> int:
    from scalper.storage import StateStore

    cfg = _config(args)
    state_dir = Path(args.state_dir or cfg.state_dir)
    state = StateStore(state_dir / STATE_FILE).load()
    if not state:
        print(f"No paper account yet in {state_dir}. Start one with: python -m scalper paper")
        return 1
    broker = state["broker"]
    cur = cfg.account.currency
    print(f"Paper account ({state_dir}), mode {state.get('mode')}, saved {state.get('saved_at')}")
    print(f"  Balance : {broker['balance']:,.2f} {cur}  (started with {broker['initial_balance']:,.2f})")
    marks = state.get("last_close", {})
    for p in broker.get("positions", []):
        mark = marks.get(p["symbol"])
        sign = 1 if p["side"] == "LONG" else -1
        move = f", last {mark}" if mark is not None else ""
        pips_to = ""
        if mark is not None:
            pip = 0.01 if p["symbol"].endswith("JPY") else 0.0001
            pips_to = f", {(mark - p['entry_price']) * sign / pip:+.1f} pips"
        print(
            f"  OPEN    {p['symbol']} {p['side']} {p['qty']:,} @ {p['entry_price']} "
            f"SL {p['stop_loss']} TP {p['take_profit']}{move}{pips_to}"
        )
    for o in broker.get("pending", []):
        print(f"  PENDING {o['symbol']} {o['side']} {o['qty']:,} (fills at next bar open)")
    trades = read_trades(state_dir / JOURNAL_FILE)
    stats = compute_stats(trades, broker["initial_balance"])
    print(format_stats(stats, cur))
    if trades:
        print(per_symbol_table(trades, broker["initial_balance"], cur, cfg.strategy.profit_multiple))
        print(f"  Last {min(args.trades, len(trades))} trade(s):")
        for t in trades[-args.trades:]:
            print(
                f"    #{t.id:<4} {t.symbol} {t.side.value:<5} {t.entry_time:%m-%d %H:%M} -> "
                f"{t.exit_time:%m-%d %H:%M} {t.exit_reason:<6} {t.pips:+7.1f} pips {t.pnl:+10.2f}"
            )
    return 0


def _fetch_feed(cfg: Config, provider: str):
    if provider == "dukascopy":
        log.info("Dukascopy keeps one file per pair per day; about 1-2 minutes per pair for a year")
        return DukascopyFeed(
            cfg.timeframe_minutes,
            progress=lambda sym, done, total: log.info("%s: %d/%d days downloaded", sym, done, total),
        )
    return _live_feed(cfg)


def cmd_fetch(args: argparse.Namespace) -> int:
    cfg = _config(args)
    provider = args.provider or cfg.feed.provider
    if provider in ("yahoo", "oanda"):
        cfg.feed.provider = provider
    cfg.validate()
    setup_logging("INFO")
    if provider not in ("yahoo", "oanda", "dukascopy"):
        print("fetch needs a data source: --provider yahoo, dukascopy or oanda", file=sys.stderr)
        return 2
    feed = _fetch_feed(cfg, provider)
    now = datetime.now(timezone.utc)
    out = Path(args.out)
    failed = []
    count = args.days * 1440 // cfg.timeframe_minutes
    cutoff = now - timedelta(days=args.days)
    symbols = _symbols_with_aux(cfg)
    for symbol in symbols:
        try:
            bars = [b for b in feed.fetch_closed(symbol, count, now) if b.time >= cutoff]
        except FeedError as exc:
            log.error("%s", exc)
            failed.append(symbol)
            continue
        path = out / f"{symbol}_M{cfg.timeframe_minutes}.csv"
        save_csv(path, bars)
        log.info("%s: %d bars -> %s", symbol, len(bars), path)
    if failed and provider == "yahoo":
        print(
            "\nYahoo refused some or all requests. Other free sources:\n"
            "  python -m scalper fetch --provider dukascopy     (no key needed)\n"
            "  or export 5-minute CSVs from TradingView into the data folder (see README)",
            file=sys.stderr,
        )
    return 1 if failed else 0


def cmd_reset(args: argparse.Namespace) -> int:
    cfg = _config(args)
    state_dir = Path(args.state_dir or cfg.state_dir)
    targets = [state_dir / STATE_FILE, state_dir / JOURNAL_FILE]
    existing = [p for p in targets if p.exists()]
    if not existing:
        print("Nothing to reset.")
        return 0
    if not args.yes:
        print("This deletes the paper account and its trade journal:")
        for p in existing:
            print(f"  {p}")
        print("Run again with --yes to confirm.")
        return 1
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    backup = state_dir / f"backup-{stamp}"
    backup.mkdir(parents=True)
    for p in existing:
        shutil.move(str(p), backup / p.name)
    print(f"Paper account reset. Old files moved to {backup}")
    return 0


# -------------------------------------------------------------------- parser
def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="scalper", description="Reverse RSI forex scalper (paper trading)")
    parser.add_argument("-c", "--config", default=DEFAULT_CONFIG, help=f"YAML config (default {DEFAULT_CONFIG})")
    sub = parser.add_subparsers(dest="command", required=True)

    p = sub.add_parser("paper", help="run the paper trader")
    p.add_argument("--provider", choices=["yahoo", "oanda", "csv", "synthetic"], help="override feed.provider")
    p.add_argument("--data-dir", help="CSV folder for --provider csv")
    p.add_argument("--state-dir", help="override state_dir")
    p.add_argument("--once", action="store_true", help="process new bars once and exit (for schedulers)")
    p.set_defaults(func=cmd_paper)

    b = sub.add_parser("backtest", help="backtest on CSV files or synthetic data")
    b.add_argument("--data-dir", help="folder with one CSV per symbol (default feed.data_dir)")
    b.add_argument("--csv-tz", help="timezone of naive CSV timestamps (default feed.csv_timezone)")
    b.add_argument("--symbols", nargs="+", help="override the configured symbols")
    b.add_argument("--synthetic", action="store_true", help="use generated random-walk data")
    b.add_argument("--days", type=int, help="test only the last N days; earlier bars warm up the indicators")
    b.add_argument("--seed", type=int, default=7, help="synthetic seed (default 7)")
    b.add_argument("--no-costs", action="store_true", help="zero spread/slippage/commission, like TradingView defaults")
    b.add_argument(
        "--min-stop-spread",
        type=float,
        nargs="+",
        metavar="X",
        help="skip signals whose stop is under X spreads; give several values to compare them (0 = off)",
    )
    b.add_argument("--until", metavar="YYYY-MM-DD", help="ignore data from this date on (to test on older data)")
    b.add_argument("--trades-out", help="write the trade list to this CSV")
    b.add_argument("--log-level", help="e.g. INFO to see every signal and fill")
    b.set_defaults(func=cmd_backtest)

    s = sub.add_parser("status", help="show the paper account")
    s.add_argument("--state-dir", help="override state_dir")
    s.add_argument("--trades", type=int, default=10, help="how many recent trades to list")
    s.set_defaults(func=cmd_status)

    f = sub.add_parser("fetch", help="download recent candles to CSV for backtesting")
    f.add_argument("--provider", choices=["yahoo", "dukascopy", "oanda"], help="data source (default feed.provider)")
    f.add_argument("--days", type=int, default=45, help="calendar days of history (default 45; Yahoo keeps ~59)")
    f.add_argument("--out", default="data", help="output folder (default data)")
    f.set_defaults(func=cmd_fetch)

    r = sub.add_parser("reset", help="start a fresh paper account (old files are backed up)")
    r.add_argument("--state-dir", help="override state_dir")
    r.add_argument("--yes", action="store_true", help="confirm")
    r.set_defaults(func=cmd_reset)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        return args.func(args)
    except (ConfigError, FeedError, ValueError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
