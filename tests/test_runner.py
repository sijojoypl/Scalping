import bisect
from datetime import timedelta

import pytest
from helpers import no_cost_config

from scalper.clock import SimClock
from scalper.engine import build_engine
from scalper.feeds import ReplayFeed, generate_synthetic
from scalper.runner import PaperTrader
from scalper.storage import read_trades

SYMBOLS = ["USDCHF", "CHFJPY", "AUDCAD", "GBPAUD"]
AUX = ["USDJPY", "USDCAD", "AUDUSD"]


@pytest.fixture(scope="module")
def data():
    return generate_synthetic(SYMBOLS + AUX, days=25, seed=21)


def config(history=400):
    cfg = no_cost_config(symbols=list(SYMBOLS))
    cfg.feed.history_bars = history
    cfg.feed.poll_delay_seconds = 15
    return cfg


def trade_keys(trades):
    return [(t.symbol, t.side, t.entry_time, t.exit_time, t.exit_reason, t.qty) for t in trades]


def replay(cfg, data, state_dir, start=None, max_cycles=None):
    feed = ReplayFeed(data, cfg.timeframe_minutes)
    clock = SimClock(start or feed.start_time(cfg.feed.history_bars))
    trader = PaperTrader(cfg, feed, clock, state_dir)
    trader.run(max_cycles=max_cycles)
    return trader, clock


def test_replay_matches_direct_engine_run(tmp_path, data):
    cfg = config()
    trader, _ = replay(cfg, data, tmp_path)
    journal = read_trades(tmp_path / "paper_trades.csv")
    assert len(journal) > 20

    # The same bars pushed straight through an engine: warm-up before the
    # replay start, trading after it.
    feed = ReplayFeed(data, cfg.timeframe_minutes)
    start = feed.start_time(cfg.feed.history_bars)
    engine = build_engine(cfg)
    stream = sorted(
        ((b.time, engine.sort_rank(s), s, b) for s, bars in data.items() for b in bars),
        key=lambda x: x[:3],
    )
    for t, _, s, b in stream:
        if t + timedelta(minutes=5) <= start:
            engine.warmup(s, b)
        else:
            engine.on_bar(s, b)
    assert trade_keys(journal) == trade_keys(engine.broker.trades)
    assert trader.engine.broker.balance == pytest.approx(engine.broker.balance)


def test_restart_resumes_identically(tmp_path, data):
    cfg = config()
    replay(cfg, data, tmp_path / "straight")
    straight = read_trades(tmp_path / "straight" / "paper_trades.csv")

    # Run half-way, throw the process away, start a fresh one on the saved state.
    first, clock = replay(cfg, data, tmp_path / "split", max_cycles=2500)
    assert first.engine.broker.positions or first.engine.broker.trades
    feed = ReplayFeed(data, cfg.timeframe_minutes)
    second = PaperTrader(cfg, feed, SimClock(clock.now()), tmp_path / "split")
    second.run()
    resumed = read_trades(tmp_path / "split" / "paper_trades.csv")

    assert trade_keys(resumed) == trade_keys(straight)
    assert second.engine.broker.balance == pytest.approx(
        50_000 + sum(t.pnl for t in straight)
    )


def test_downtime_replays_exits_but_not_entries(tmp_path, data):
    cfg = config()
    first, clock = replay(cfg, data, tmp_path, max_cycles=3000)
    open_before = dict(first.engine.broker.positions)
    trades_before = len(read_trades(tmp_path / "paper_trades.csv"))

    # The bot comes back 12 hours later.
    feed = ReplayFeed(data, cfg.timeframe_minutes)
    later = PaperTrader(cfg, feed, SimClock(clock.now() + timedelta(hours=12)), tmp_path)
    later.bootstrap()
    after = read_trades(tmp_path / "paper_trades.csv")[trades_before:]

    assert later.engine.stats["orders"] == 0  # no entries on stale signals
    # anything that closed while offline was a position that was already open
    assert {t.id for t in after} <= {p.id for p in open_before.values()}


def test_refuses_non_paper_mode(tmp_path, data):
    cfg = config()
    cfg.mode = "LIVE"
    with pytest.raises(RuntimeError):
        PaperTrader(cfg, ReplayFeed(data, 5), SimClock(data["USDCHF"][500].time), tmp_path)


class FakeYahoo:
    """Answers Yahoo chart requests from synthetic data, including a half-built current bar."""

    def __init__(self, data, clock):
        self.data = data
        self.stamps = {s: [b.time.timestamp() for b in bars] for s, bars in data.items()}
        self.clock = clock
        self.headers = {}

    def get(self, url, params=None, timeout=None):
        symbol = url.rsplit("/", 1)[-1].replace("=X", "")
        now = self.clock.now()
        stamps = self.stamps[symbol]
        lo = bisect.bisect_left(stamps, params["period1"])
        hi = bisect.bisect_right(stamps, min(params["period2"], now.timestamp()))
        rows = self.data[symbol][lo:hi]
        if rows and rows[-1].time + timedelta(minutes=5) > now:
            last = rows[-1]  # still forming: pretend only half the move has happened
            mid = (last.open + last.close) / 2
            rows[-1] = type(last)(last.time, last.open, max(last.open, mid), min(last.open, mid), mid)
        quote = {k: [getattr(b, k) for b in rows] for k in ("open", "high", "low", "close")}
        payload = {
            "chart": {
                "result": [{"timestamp": [int(b.time.timestamp()) for b in rows], "indicators": {"quote": [quote]}}],
                "error": None,
            }
        }

        class Resp:
            status_code = 200

            def json(self):
                return payload

        return Resp()


def test_live_loop_through_yahoo_feed_matches_replay(tmp_path, data):
    from scalper.feeds import YahooFeed

    cfg = config()
    start = ReplayFeed(data, 5).start_time(cfg.feed.history_bars)
    cycles = 700

    _, _ = replay(cfg, data, tmp_path / "replay", start=start, max_cycles=cycles)
    expected = read_trades(tmp_path / "replay" / "paper_trades.csv")

    clock = SimClock(start)
    feed = YahooFeed(5, session=FakeYahoo(data, clock), min_interval_seconds=0, sleep=lambda s: None)
    PaperTrader(cfg, feed, clock, tmp_path / "yahoo").run(max_cycles=cycles)
    got = read_trades(tmp_path / "yahoo" / "paper_trades.csv")

    assert len(expected) > 5
    assert trade_keys(got) == trade_keys(expected)


class FlakyFeed:
    """Wraps a feed and fails the first ``failures`` requests for one symbol."""

    def __init__(self, inner, symbol, failures):
        self.inner, self.symbol, self.failures = inner, symbol, failures

    def fetch_closed(self, symbol, count, now):
        from scalper.feeds import FeedError

        if symbol == self.symbol and self.failures > 0:
            self.failures -= 1
            raise FeedError("simulated outage")
        return self.inner.fetch_closed(symbol, count, now)


def test_exits_missed_during_outage_still_replayed_if_startup_fetch_fails(tmp_path, data):
    cfg = config()
    straight_dir = tmp_path / "straight"
    replay(cfg, data, straight_dir)
    straight = read_trades(straight_dir / "paper_trades.csv")

    # Stop while a position is open, then restart 30 minutes later with the
    # first two history requests for that symbol failing.
    feed = ReplayFeed(data, cfg.timeframe_minutes)
    clock = SimClock(feed.start_time(cfg.feed.history_bars))
    first = PaperTrader(cfg, feed, clock, tmp_path / "flaky")
    first.bootstrap()
    while not first.engine.broker.positions:
        first._sleep_until_next_bar()
        first.cycle()
    symbol, pos = next(iter(first.engine.broker.positions.items()))
    first._save()

    clock2 = SimClock(clock.now() + timedelta(minutes=30))
    second = PaperTrader(cfg, FlakyFeed(ReplayFeed(data, 5), symbol, failures=2), clock2, tmp_path / "flaky")
    second.bootstrap()  # fails for the symbol
    assert symbol not in second.engine.last_bar_time
    second._save()  # the checkpoint must survive a save while the symbol is still missing
    saved = second.store.load()["last_bar_time"]
    assert symbol in saved
    second._sleep_until_next_bar()
    second.cycle()  # fails again
    for _ in range(400):
        second._sleep_until_next_bar()
        second.cycle()

    got = {t.id: t for t in read_trades(tmp_path / "flaky" / "paper_trades.csv")}
    want = next(t for t in straight if t.symbol == symbol and t.entry_time == pos.entry_time)
    assert pos.id in got, "the open position was never closed"
    assert (got[pos.id].exit_time, got[pos.id].exit_reason) == (want.exit_time, want.exit_reason)


class LaggingFeed:
    """Wraps a feed and leaves out the newest bar on the first request per symbol."""

    def __init__(self, inner):
        self.inner, self.seen = inner, set()

    def fetch_closed(self, symbol, count, now):
        bars = self.inner.fetch_closed(symbol, count, now)
        if symbol not in self.seen:
            self.seen.add(symbol)
            return bars[:-1]
        return bars


def run_until(trader, predicate, limit=6000):
    for _ in range(limit):
        trader._sleep_until_next_bar()
        trader.cycle()
        if predicate():
            return
    raise AssertionError("condition never reached")


def test_lagging_startup_fetch_cannot_fill_order_on_its_signal_bar(tmp_path, data):
    cfg = config()
    feed = ReplayFeed(data, cfg.timeframe_minutes)
    clock = SimClock(feed.start_time(cfg.feed.history_bars))
    first = PaperTrader(cfg, feed, clock, tmp_path)
    first.bootstrap()
    run_until(first, lambda: bool(first.engine.broker.pending))
    symbol, order = next(iter(first.engine.broker.pending.items()))

    # Restart straight away; the first fetch misses the signal bar.
    second = PaperTrader(cfg, LaggingFeed(ReplayFeed(data, 5)), SimClock(clock.now()), tmp_path)
    second.bootstrap()
    assert second.store.load()["last_bar_time"][symbol] == order.signal_time.isoformat()
    second._sleep_until_next_bar()
    second.cycle()

    pos = second.engine.broker.positions.get(symbol)
    entry_time = pos.entry_time if pos else next(
        t.entry_time for t in second.engine.broker.trades if t.symbol == symbol
    )
    assert entry_time == order.signal_time + timedelta(minutes=5)


class Crash(Exception):
    pass


def test_crash_between_journal_and_state_write_does_not_duplicate(tmp_path, data):
    cfg = config()
    replay(cfg, data, tmp_path / "straight")
    straight = read_trades(tmp_path / "straight" / "paper_trades.csv")

    feed = ReplayFeed(data, cfg.timeframe_minutes)
    clock = SimClock(feed.start_time(cfg.feed.history_bars))
    trader = PaperTrader(cfg, feed, clock, tmp_path / "crashy")
    real_save = trader.store.save
    seen = {"outbox": False}

    def save_then_maybe_crash(state):
        # Die on the save that follows a journal write, i.e. after the trade
        # reached the journal but before the account knows it was written.
        if seen["outbox"] and not state["outbox"]:
            raise Crash
        seen["outbox"] = bool(state["outbox"])
        real_save(state)

    trader.store.save = save_then_maybe_crash
    with pytest.raises(Crash):
        trader.run()
    written = read_trades(tmp_path / "crashy" / "paper_trades.csv")
    assert written, "the crash should come after at least one journal write"

    resumed = PaperTrader(cfg, ReplayFeed(data, 5), SimClock(clock.now()), tmp_path / "crashy")
    resumed.run()
    journal = read_trades(tmp_path / "crashy" / "paper_trades.csv")
    assert len({t.id for t in journal}) == len(journal)
    assert trade_keys(journal) == trade_keys(straight)


def test_refuses_to_start_with_trades_in_removed_symbol(tmp_path, data):
    from scalper.config import ConfigError

    cfg = config()
    feed = ReplayFeed(data, cfg.timeframe_minutes)
    clock = SimClock(feed.start_time(cfg.feed.history_bars))
    first = PaperTrader(cfg, feed, clock, tmp_path)
    first.bootstrap()
    run_until(first, lambda: bool(first.engine.broker.positions))
    first._save()
    symbol = next(iter(first.engine.broker.positions))

    trimmed = config()
    trimmed.symbols = [s for s in SYMBOLS if s != symbol]
    trimmed.validate()
    with pytest.raises(ConfigError, match=symbol):
        PaperTrader(trimmed, ReplayFeed(data, 5), SimClock(clock.now()), tmp_path).bootstrap()
