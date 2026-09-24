"""Bar-by-bar backtest built on the same engine the paper trader uses."""

from __future__ import annotations

import heapq
import logging
from dataclasses import dataclass

from scalper.config import Config
from scalper.engine import Engine, build_engine
from scalper.models import Bar, Trade
from scalper.report import Stats, compute_stats

log = logging.getLogger(__name__)


@dataclass
class BacktestResult:
    engine: Engine
    trades: list[Trade]
    stats: Stats
    bars_processed: int


def merged_stream(bars_by_symbol: dict[str, list[Bar]], aux: set[str]):
    """Yield ``(symbol, bar)`` in time order; conversion pairs go first on ties."""

    def keyed(symbol: str, bars: list[Bar]):
        rank = 0 if symbol in aux else 1
        return ((b.time, rank, symbol, b) for b in bars)

    iterables = [keyed(s, bars) for s, bars in bars_by_symbol.items()]
    for _, _, symbol, bar in heapq.merge(*iterables, key=lambda x: (x[0], x[1], x[2])):
        yield symbol, bar


def run_backtest(config: Config, bars_by_symbol: dict[str, list[Bar]]) -> BacktestResult:
    engine = build_engine(config)
    missing = [s for s in config.symbols if not bars_by_symbol.get(s)]
    if missing:
        raise ValueError(f"no bars for traded symbol(s): {', '.join(missing)}")
    for pair in engine.aux_symbols:
        if not bars_by_symbol.get(pair):
            ccy = pair.replace(config.account.currency, "")
            note = (
                f"using fx_fallback_rates[{ccy}]" if ccy in config.fx_fallback_rates
                else "trades needing it will be skipped (add fx_fallback_rates)"
            )
            log.warning("no data for conversion pair %s; %s", pair, note)
    wanted = set(config.symbols) | set(engine.aux_symbols)
    data = {s: sorted(b, key=lambda x: x.time) for s, b in bars_by_symbol.items() if s in wanted}
    count = 0
    for symbol, bar in merged_stream(data, set(engine.aux_symbols)):
        engine.on_bar(symbol, bar)
        count += 1
    trades = list(engine.broker.trades)
    return BacktestResult(
        engine=engine,
        trades=trades,
        stats=compute_stats(trades, config.account.initial_capital),
        bars_processed=count,
    )
