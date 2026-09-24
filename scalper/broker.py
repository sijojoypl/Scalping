"""Paper broker that copies TradingView's broker emulator.

* Market entries submitted on a bar's close fill at the next bar's open.
* Stop loss and take profit are checked inside each bar with TradingView's
  path assumption: if the high is closer to the open than the low, price went
  open -> high -> low -> close, otherwise open -> low -> high -> close.
* A bar that opens beyond a level fills at the open (gap).
* Levels are compared against the feed's (mid) prices. Every fill pays half
  the configured spread, market entries and stop exits also pay slippage.

Nothing here talks to a real broker.
"""

from __future__ import annotations

from datetime import date, datetime
from typing import Any

from scalper.config import CostConfig
from scalper.instruments import Instrument
from scalper.models import (
    Bar,
    EntryOrder,
    FillEvent,
    Position,
    Side,
    Trade,
    from_dict,
    to_dict,
)
from scalper.rates import RateBook


class PaperBroker:
    def __init__(
        self,
        instruments: dict[str, Instrument],
        rates: RateBook,
        costs: CostConfig,
        initial_balance: float,
    ) -> None:
        self.instruments = instruments
        self.rates = rates
        self.costs = costs
        self.initial_balance = initial_balance
        self.balance = initial_balance
        self.positions: dict[str, Position] = {}
        self.pending: dict[str, EntryOrder] = {}
        self.trades: list[Trade] = []
        self.daily_pnl: dict[str, float] = {}  # UTC date (ISO) -> realised P&L
        self.next_id = 1

    # ------------------------------------------------------------------ orders
    def is_flat(self, symbol: str) -> bool:
        return symbol not in self.positions and symbol not in self.pending

    def exposure_count(self) -> int:
        return len(self.positions) + len(self.pending)

    def submit(self, order: EntryOrder) -> None:
        if not self.is_flat(order.symbol):
            raise RuntimeError(f"{order.symbol} already has a position or pending order")
        if order.qty <= 0:
            raise ValueError("order quantity must be positive")
        self.pending[order.symbol] = order

    # --------------------------------------------------------------- matching
    def process_bar(self, symbol: str, bar: Bar) -> list[FillEvent]:
        """Match pending orders and open positions against a newly closed bar."""
        events: list[FillEvent] = []
        order = self.pending.pop(symbol, None)
        if order is not None:
            events.append(self._fill_entry(order, bar))
        position = self.positions.get(symbol)
        if position is None:
            return events
        if order is None:
            position.bars_held += 1
        hit = exit_trigger(position, bar)
        if hit is not None:
            level, reason, gapped = hit
            events.append(self._close(position, bar.time, level, reason, gapped))
        return events

    def _costs(self, inst: Instrument) -> tuple[float, float]:
        half_spread = self.costs.spread_for(inst.symbol) * inst.pip_size / 2.0
        slippage = self.costs.slippage_pips * inst.pip_size
        return half_spread, slippage

    def _commission(self, qty: int) -> float:
        return qty / 100_000 * self.costs.commission_per_100k

    def _fill_entry(self, order: EntryOrder, bar: Bar) -> FillEvent:
        inst = self.instruments[order.symbol]
        half_spread, slippage = self._costs(inst)
        price = bar.open + order.side.sign * (half_spread + slippage)
        position = Position(
            id=self.next_id,
            symbol=order.symbol,
            side=order.side,
            qty=order.qty,
            entry_time=bar.time,
            entry_price=price,
            stop_loss=order.stop_loss,
            take_profit=order.take_profit,
            signal_time=order.signal_time,
            signal_price=order.signal_price,
            quote_rate=order.quote_rate,
        )
        self.next_id += 1
        self.positions[order.symbol] = position
        return FillEvent("ENTRY", order.symbol, bar.time, order.side, order.qty, price)

    def _close(self, pos: Position, when: datetime, level: float, reason: str, gapped: bool) -> FillEvent:
        inst = self.instruments[pos.symbol]
        half_spread, slippage = self._costs(inst)
        cost = half_spread + (slippage if reason == "SL" else 0.0)
        exit_price = level - pos.side.sign * cost
        move = (exit_price - pos.entry_price) * pos.side.sign
        pnl_quote = move * pos.qty
        rate = self.rates.value(inst.quote) or pos.quote_rate
        commission = 2 * self._commission(pos.qty)
        pnl = pnl_quote * rate - commission
        trade = Trade(
            id=pos.id,
            symbol=pos.symbol,
            side=pos.side,
            qty=pos.qty,
            entry_time=pos.entry_time,
            entry_price=pos.entry_price,
            exit_time=when,
            exit_price=exit_price,
            exit_reason=reason + (" gap" if gapped else ""),
            stop_loss=pos.stop_loss,
            take_profit=pos.take_profit,
            pips=move / inst.pip_size,
            pnl_quote=pnl_quote,
            commission=commission,
            pnl=pnl,
            bars_held=pos.bars_held,
        )
        self.balance += pnl
        day = when.date().isoformat()
        self.daily_pnl[day] = self.daily_pnl.get(day, 0.0) + pnl
        self.trades.append(trade)
        del self.positions[pos.symbol]
        return FillEvent("EXIT", pos.symbol, when, pos.side, pos.qty, exit_price, trade.exit_reason, trade)

    # -------------------------------------------------------------- reporting
    def realised_on(self, day: date) -> float:
        return self.daily_pnl.get(day.isoformat(), 0.0)

    def unrealised(self, marks: dict[str, float]) -> float:
        total = 0.0
        for symbol, pos in self.positions.items():
            mark = marks.get(symbol)
            if mark is None:
                continue
            rate = self.rates.value(self.instruments[symbol].quote) or pos.quote_rate
            total += (mark - pos.entry_price) * pos.side.sign * pos.qty * rate
        return total

    def equity(self, marks: dict[str, float]) -> float:
        return self.balance + self.unrealised(marks)

    # ------------------------------------------------------------ persistence
    def to_state(self) -> dict[str, Any]:
        # Keep a couple of weeks of daily P&L; older days are no longer needed.
        recent_days = dict(sorted(self.daily_pnl.items())[-14:])
        return {
            "initial_balance": self.initial_balance,
            "balance": self.balance,
            "next_id": self.next_id,
            "positions": [to_dict(p) for p in self.positions.values()],
            "pending": [to_dict(o) for o in self.pending.values()],
            "daily_pnl": recent_days,
        }

    def load_state(self, state: dict[str, Any]) -> None:
        self.initial_balance = float(state["initial_balance"])
        self.balance = float(state["balance"])
        self.next_id = int(state["next_id"])
        self.positions = {p["symbol"]: from_dict(Position, p) for p in state.get("positions", [])}
        self.pending = {o["symbol"]: from_dict(EntryOrder, o) for o in state.get("pending", [])}
        self.daily_pnl = {k: float(v) for k, v in state.get("daily_pnl", {}).items()}


def exit_trigger(pos: Position, bar: Bar) -> tuple[float, str, bool] | None:
    """Return ``(price, "SL"|"TP", gapped)`` if the bar hits a level, else ``None``."""
    o, h, l = bar.open, bar.high, bar.low
    sl, tp = pos.stop_loss, pos.take_profit
    long = pos.side is Side.LONG

    # A bar that opens past a level fills at the open.
    if long:
        if o <= sl:
            return o, "SL", True
        if o >= tp:
            return o, "TP", True
    else:
        if o >= sl:
            return o, "SL", True
        if o <= tp:
            return o, "TP", True

    high_first = (h - o) < (o - l)
    for leg in ("high", "low") if high_first else ("low", "high"):
        if leg == "high":
            if long and h >= tp:
                return tp, "TP", False
            if not long and h >= sl:
                return sl, "SL", False
        else:
            if long and l <= sl:
                return sl, "SL", False
            if not long and l <= tp:
                return tp, "TP", False
    return None
