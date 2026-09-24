"""Glue between bars, the strategy and the paper broker.

The backtester and the live paper trader both drive this same class, so a
backtest and a paper session follow identical rules.
"""

from __future__ import annotations

import logging
import math
from collections import Counter
from collections.abc import Callable
from datetime import datetime

from scalper.broker import PaperBroker
from scalper.config import Config, CostConfig
from scalper.instruments import Instrument
from scalper.models import Bar, EntryOrder, FillEvent, Trade
from scalper.rates import RateBook
from scalper.session import SessionWindow
from scalper.strategy import ReverseRSIStrategy, Snapshot

log = logging.getLogger(__name__)


def pine_round(x: float) -> int:
    """Pine ``round``: nearest integer, ties away from zero."""
    return int(math.floor(abs(x) + 0.5)) * (1 if x >= 0 else -1)


class Engine:
    def __init__(
        self,
        config: Config,
        broker: PaperBroker,
        rates: RateBook,
        on_trade: Callable[[Trade], None] | None = None,
    ) -> None:
        self.config = config
        self.broker = broker
        self.rates = rates
        self.on_trade = on_trade
        self.instruments = {s: Instrument.from_symbol(s) for s in config.symbols}
        self.strategies = {s: ReverseRSIStrategy(config.strategy, i) for s, i in self.instruments.items()}
        self.aux_symbols = rates.required_aux(config.symbols)
        tz = config.strategy.session_timezone
        self.no_entry = [SessionWindow.parse(f"{w}:1234567", tz) for w in config.risk.no_entry_windows]
        self.last_bar_time: dict[str, datetime] = {}
        self.last_close: dict[str, float] = {}
        self.last_snapshot: dict[str, Snapshot] = {}
        self.stats: Counter[str] = Counter()
        self._missing_rates: set[str] = set()

    @property
    def all_symbols(self) -> list[str]:
        return list(self.config.symbols) + self.aux_symbols

    def is_aux(self, symbol: str) -> bool:
        return symbol not in self.strategies

    def sort_rank(self, symbol: str) -> int:
        """Bars sharing a timestamp go USD pairs first, so crosses convert at the fresh rate."""
        cur = self.config.account.currency
        return 0 if symbol.startswith(cur) or symbol.endswith(cur) else 1

    # ------------------------------------------------------------------ bars
    def warmup(self, symbol: str, bar: Bar) -> None:
        """Feed history through the indicators without trading."""
        if not self._accept(symbol, bar):
            return
        strategy = self.strategies.get(symbol)
        if strategy is not None:
            self.last_snapshot[symbol] = strategy.update(bar)

    def on_bar(self, symbol: str, bar: Bar, allow_entries: bool = True) -> list[FillEvent]:
        """Process one closed bar: fills and exits first, then a new signal."""
        if not self._accept(symbol, bar):
            return []
        strategy = self.strategies.get(symbol)
        if strategy is None:  # conversion pair, only updates rates
            return []
        events = self.broker.process_bar(symbol, bar)
        for event in events:
            self._log_fill(event)
            if event.trade is not None and self.on_trade is not None:
                self.on_trade(event.trade)
        snap = strategy.update(bar)
        self.last_snapshot[symbol] = snap
        if snap.signal is not None:
            self._on_signal(symbol, bar, snap, allow_entries)
        return events

    def _accept(self, symbol: str, bar: Bar) -> bool:
        last = self.last_bar_time.get(symbol)
        if last is not None and bar.time <= last:
            return False
        self.last_bar_time[symbol] = bar.time
        self.last_close[symbol] = bar.close
        self.rates.update(symbol, bar.close)
        return True

    # --------------------------------------------------------------- signals
    def _on_signal(self, symbol: str, bar: Bar, snap: Snapshot, allow_entries: bool) -> None:
        inst = self.instruments[symbol]
        assert snap.signal is not None and snap.risk_distance is not None
        self.stats["signals"] += 1
        desc = (
            f"{symbol} {snap.signal.value} signal @ {inst.fmt(bar.close)} "
            f"(rsi_ma {snap.rsi_ma:.2f}, atr {inst.fmt(snap.atr or 0)})"
        )

        skip = self._entry_block_reason(symbol, bar, allow_entries, snap.risk_distance)
        if skip:
            self.stats[f"skipped: {skip}"] += 1
            log.info("%s skipped: %s", desc, skip)
            return

        quote_rate = self.rates.value(inst.quote)
        if quote_rate is None:
            self.stats["skipped: no conversion rate"] += 1
            if inst.quote not in self._missing_rates:  # once per currency, not per signal
                self._missing_rates.add(inst.quote)
                log.warning(
                    "%s signals are skipped: no %s/USD price and no fx_fallback_rates[%s]",
                    symbol, inst.quote, inst.quote,
                )
            return

        capital = (
            self.config.account.initial_capital
            if self.config.account.sizing_basis == "initial"
            else self.broker.balance
        )
        risk_amount = capital * self.config.strategy.risk_per_trade
        qty = pine_round(risk_amount / snap.risk_distance / quote_rate)
        if qty <= 0:
            self.stats["skipped: zero size"] += 1
            log.info("%s skipped: position size rounds to zero", desc)
            return

        assert snap.stop_loss is not None and snap.take_profit is not None
        self.broker.submit(
            EntryOrder(
                symbol=symbol,
                side=snap.signal,
                qty=qty,
                stop_loss=snap.stop_loss,
                take_profit=snap.take_profit,
                signal_time=bar.time,
                signal_price=bar.close,
                risk_distance=snap.risk_distance,
                quote_rate=quote_rate,
            )
        )
        self.stats["orders"] += 1
        log.info(
            "%s -> order %s units, SL %s, TP %s (fills at next bar open)",
            desc, f"{qty:,}", inst.fmt(snap.stop_loss), inst.fmt(snap.take_profit),
        )

    def _entry_block_reason(
        self, symbol: str, bar: Bar, allow_entries: bool, risk_distance: float
    ) -> str | None:
        if not self.broker.is_flat(symbol):
            return "position already open"
        if not allow_entries:
            return "stale bar (catch-up after downtime)"
        risk = self.config.risk
        if any(w.contains(bar.time) for w in self.no_entry):
            return "inside a no-entry window"
        if risk.min_stop_spread_ratio is not None:
            # Uses the configured spread even in a --no-costs backtest, so both
            # runs take the same trades and only the charges differ.
            spread = self.config.costs.spread_for(symbol)
            stop_pips = risk_distance / self.instruments[symbol].pip_size
            if spread > 0 and stop_pips < risk.min_stop_spread_ratio * spread:
                return "stop too small for the spread"
        if risk.max_open_positions is not None and self.broker.exposure_count() >= risk.max_open_positions:
            return "max open positions reached"
        if risk.max_daily_loss_pct is not None:
            limit = self.config.account.initial_capital * risk.max_daily_loss_pct / 100.0
            if self.broker.realised_on(bar.time.date()) <= -limit:
                return "daily loss limit hit"
        return None

    def _log_fill(self, event: FillEvent) -> None:
        inst = self.instruments[event.symbol]
        if event.kind == "ENTRY":
            pos = self.broker.positions.get(event.symbol)
            extra = f" SL {inst.fmt(pos.stop_loss)} TP {inst.fmt(pos.take_profit)}" if pos else ""
            log.info(
                "FILL  %s %s %s units @ %s%s",
                event.symbol, event.side.value, f"{event.qty:,}", inst.fmt(event.price), extra,
            )
        else:
            t = event.trade
            assert t is not None
            log.info(
                "EXIT  %s %s @ %s [%s] %+.1f pips, P&L %+.2f %s, balance %.2f",
                event.symbol, event.side.value, inst.fmt(event.price), event.reason,
                t.pips, t.pnl, self.config.account.currency, self.broker.balance,
            )

    # ------------------------------------------------------------- reporting
    def marks(self) -> dict[str, float]:
        return {s: p for s, p in self.last_close.items() if s in self.strategies}


def build_engine(
    config: Config, on_trade: Callable[[Trade], None] | None = None, charge_costs: bool = True
) -> Engine:
    """``charge_costs=False`` fills without spread, slippage or commission (TradingView's
    default), while filters that look at the configured spread still see it."""
    rates = RateBook(config.account.currency, config.fx_fallback_rates)
    instruments = {s: Instrument.from_symbol(s) for s in config.symbols}
    costs = config.costs if charge_costs else CostConfig(spread_pips={"default": 0.0})
    broker = PaperBroker(instruments, rates, costs, config.account.initial_capital)
    return Engine(config, broker, rates, on_trade=on_trade)
