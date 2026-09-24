"""The paper-trading loop.

Every bar close it pulls fresh candles, pushes new closed bars through the
engine, and saves the paper account so a restart picks up where it left off.
"""

from __future__ import annotations

import logging
import math
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

from scalper import __version__
from scalper.clock import RealClock, SimClock
from scalper.config import Config
from scalper.engine import Engine, build_engine
from scalper.feeds import Feed, FeedError
from scalper.models import Bar
from scalper.storage import StateStore, TradeJournal

log = logging.getLogger(__name__)

STATE_FILE = "paper_state.json"
JOURNAL_FILE = "paper_trades.csv"
# Entries are only taken on bars that closed at most this many bars ago, so a
# restart after downtime does not open trades on stale signals. Exits of open
# positions are always replayed, as resting SL/TP orders would have been.
MAX_ENTRY_AGE_BARS = 2


class PaperTrader:
    def __init__(self, config: Config, feed: Feed, clock: RealClock | SimClock, state_dir: str | Path | None = None):
        if config.mode != "PAPER":
            raise RuntimeError("PaperTrader refuses to run outside PAPER mode")
        self.config = config
        self.feed = feed
        self.clock = clock
        self.tf = timedelta(minutes=config.timeframe_minutes)
        state_dir = Path(state_dir or config.state_dir)
        self.store = StateStore(state_dir / STATE_FILE)
        self.journal = TradeJournal(state_dir / JOURNAL_FILE)
        self.engine: Engine = build_engine(config, on_trade=self.journal.append)
        self._saved_last: dict[str, datetime] = {}
        self.cycles = 0

    # ------------------------------------------------------------ lifecycle
    def bootstrap(self) -> None:
        """Restore the account, warm up indicators and replay missed bars."""
        state = self.store.load()
        last_times: dict[str, datetime] = {}
        if state:
            self.engine.broker.load_state(state["broker"])
            last_times = {s: datetime.fromisoformat(t) for s, t in state.get("last_bar_time", {}).items()}
            log.info(
                "restored paper account: balance %.2f, %d open position(s), %d pending order(s)",
                self.engine.broker.balance, len(self.engine.broker.positions), len(self.engine.broker.pending),
            )
            orphans = set(self.engine.broker.positions) - set(self.config.symbols)
            if orphans:
                log.warning("state has positions for symbols no longer configured: %s", ", ".join(sorted(orphans)))
        else:
            log.info("new paper account: %.2f %s", self.config.account.initial_capital, self.config.account.currency)

        self._saved_last = last_times
        now = self.clock.now()
        catch_up: list[tuple[str, Bar]] = []
        for symbol in self.engine.all_symbols:
            try:
                bars = self.feed.fetch_closed(symbol, self.config.feed.history_bars, now)
            except FeedError as exc:
                log.error("could not load history for %s: %s (will retry next bar)", symbol, exc)
                continue
            if not bars:
                log.warning("no history returned for %s", symbol)
                continue
            catch_up.extend(self._ingest(symbol, bars))
            strat = self.engine.strategies.get(symbol)
            if strat is not None and not strat.ready:
                log.warning("%s: only %d bars of history, indicators not ready yet", symbol, len(bars))
        if catch_up:
            log.info("replaying %d bar(s) missed while the bot was offline", len(catch_up))
        self._process(catch_up)
        self._save()

    def run(self, once: bool = False, max_cycles: int | None = None) -> None:
        self._banner()
        self.bootstrap()
        self._log_status()
        if once:
            return
        try:
            while max_cycles is None or self.cycles < max_cycles:
                self._sleep_until_next_bar()
                if isinstance(self.clock, SimClock) and self._replay_finished():
                    log.info("replay data exhausted")
                    break
                self.cycle()
        except KeyboardInterrupt:
            log.info("stopped by user")
        finally:
            self._save()
            self._log_status()

    def cycle(self) -> int:
        """Fetch and process new closed bars. Returns the number of new bars."""
        self.cycles += 1
        now = self.clock.now()
        batch: list[tuple[str, Bar]] = []
        for symbol in self.engine.all_symbols:
            last = self.engine.last_bar_time.get(symbol)
            if last is None:
                count = self.config.feed.history_bars
            else:
                missed = math.ceil((now - last) / self.tf) + 2
                count = max(20, min(missed, self.config.feed.history_bars))
            try:
                bars = self.feed.fetch_closed(symbol, count, now)
            except FeedError as exc:
                log.warning("%s: %s (will retry next bar)", symbol, exc)
                continue
            batch.extend(self._ingest(symbol, bars))
        activity = self._process(batch)
        self._save()
        hourly = not self.clock.simulated and any(
            b.time.minute == 60 - self.config.timeframe_minutes and not self.engine.is_aux(s) for s, b in batch
        )
        if activity or hourly:
            self._log_status()
        return len(batch)

    # -------------------------------------------------------------- helpers
    def _ingest(self, symbol: str, bars: list[Bar]) -> list[tuple[str, Bar]]:
        """Warm up on bars that were already processed; return the newer ones.

        "Already processed" means up to the engine's last bar, or, when the
        symbol has not loaded yet this run, the checkpoint in the saved state.
        That way a symbol whose history failed at start-up still replays the
        exits it missed instead of silently warming through them.
        """
        last = self.engine.last_bar_time.get(symbol) or self._saved_last.get(symbol)
        if last is not None and bars and bars[0].time > last + self.tf:
            log.warning(
                "%s: data resumes at %s but the last processed bar was %s; bars in between are lost",
                symbol, bars[0].time, last,
            )
        fresh: list[tuple[str, Bar]] = []
        for bar in bars:
            if last is None or bar.time <= last:
                self.engine.warmup(symbol, bar)
            else:
                fresh.append((symbol, bar))
        return fresh

    def _process(self, batch: list[tuple[str, Bar]]) -> int:
        """Run bars through the engine; returns the number of fills plus new orders."""
        batch.sort(key=lambda sb: (sb[1].time, 0 if self.engine.is_aux(sb[0]) else 1, sb[0]))
        now = self.clock.now()
        orders_before = self.engine.stats["orders"]
        fills = 0
        for symbol, bar in batch:
            fresh = now - (bar.time + self.tf) <= self.tf * MAX_ENTRY_AGE_BARS
            fills += len(self.engine.on_bar(symbol, bar, allow_entries=fresh))
        return fills + self.engine.stats["orders"] - orders_before

    def _sleep_until_next_bar(self) -> None:
        now = self.clock.now()
        step = self.tf.total_seconds()
        ts = now.timestamp()
        target = math.floor(ts / step) * step + self.config.feed.poll_delay_seconds
        if target <= ts:
            target += step
        self.clock.sleep(target - ts)

    def _replay_finished(self) -> bool:
        end = getattr(self.feed, "end_time", None)
        return end is not None and self.clock.now() > end() + self.tf

    def state(self) -> dict[str, Any]:
        return {
            "version": __version__,
            "mode": self.config.mode,
            "saved_at": self.clock.now().isoformat(),
            "symbols": list(self.config.symbols),
            "broker": self.engine.broker.to_state(),
            "last_bar_time": {
                s: t.isoformat() for s, t in {**self._saved_last, **self.engine.last_bar_time}.items()
            },
            "last_close": dict(self.engine.last_close),
        }

    def _save(self) -> None:
        self.store.save(self.state())

    def _banner(self) -> None:
        feed = type(self.feed).__name__
        log.info("=" * 68)
        log.info("Reverse RSI scalper v%s  |  MODE: PAPER (simulated fills, no real orders)", __version__)
        log.info(
            "symbols %s  |  M%d  |  session %s %s  |  feed %s",
            ",".join(self.config.symbols), self.config.timeframe_minutes,
            self.config.strategy.session, self.config.strategy.session_timezone, feed,
        )
        if self.engine.aux_symbols:
            log.info("conversion pairs: %s", ", ".join(self.engine.aux_symbols))
        log.info("=" * 68)

    def _log_status(self) -> None:
        broker = self.engine.broker
        marks = self.engine.marks()
        cur = self.config.account.currency
        parts = [f"balance {broker.balance:,.2f} {cur}", f"equity {broker.equity(marks):,.2f}"]
        for sym, pos in broker.positions.items():
            inst = self.engine.instruments[sym]
            parts.append(
                f"{sym} {pos.side.value} {pos.qty:,} @ {inst.fmt(pos.entry_price)} "
                f"(SL {inst.fmt(pos.stop_loss)} TP {inst.fmt(pos.take_profit)})"
            )
        for sym, order in broker.pending.items():
            parts.append(f"{sym} {order.side.value} {order.qty:,} pending")
        last = max(self.engine.last_bar_time.values(), default=None)
        stamp = last.strftime("%Y-%m-%d %H:%M UTC") if last else "no data"
        log.info("[%s] %s", stamp, " | ".join(parts))
