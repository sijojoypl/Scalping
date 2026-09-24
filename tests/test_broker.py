from datetime import datetime, timedelta, timezone

import pytest
from helpers import bar

from scalper.broker import PaperBroker, exit_trigger
from scalper.config import CostConfig
from scalper.instruments import Instrument
from scalper.models import EntryOrder, Position, Side
from scalper.rates import RateBook

T0 = datetime(2026, 1, 6, 21, 0, tzinfo=timezone.utc)
M5 = timedelta(minutes=5)


def make_broker(spread=0.0, slippage=0.0, commission=0.0, symbols=("EURUSD",)):
    costs = CostConfig(spread_pips={"default": spread}, slippage_pips=slippage, commission_per_100k=commission)
    rates = RateBook("USD", {"JPY": 0.0067})
    instruments = {s: Instrument.from_symbol(s) for s in symbols}
    return PaperBroker(instruments, rates, costs, 50_000.0)


def order(side=Side.LONG, symbol="EURUSD", sl=1.0950, tp=1.1075, qty=100_000, rate=1.0):
    return EntryOrder(symbol, side, qty, sl, tp, T0, 1.1000, 0.0050, rate)


def position(side, sl, tp, entry=1.1000):
    return Position(1, "EURUSD", side, 100_000, T0, entry, sl, tp, T0, entry, 1.0)


def test_entry_fills_at_next_open_with_costs():
    b = make_broker(spread=2.0, slippage=0.5)
    b.submit(order())
    assert not b.is_flat("EURUSD")
    events = b.process_bar("EURUSD", bar(T0 + M5, 1.1002, 1.1010, 1.0995, 1.1005))
    assert [e.kind for e in events] == ["ENTRY"]
    # open + half spread (1 pip) + slippage (0.5 pip)
    assert b.positions["EURUSD"].entry_price == pytest.approx(1.1002 + 0.00015)
    assert b.positions["EURUSD"].bars_held == 0


def test_take_profit_and_pnl():
    b = make_broker()
    b.submit(order())
    b.process_bar("EURUSD", bar(T0 + M5, 1.1000, 1.1010, 1.0990, 1.1005))
    b.process_bar("EURUSD", bar(T0 + 2 * M5, 1.1005, 1.1040, 1.1000, 1.1030))
    events = b.process_bar("EURUSD", bar(T0 + 3 * M5, 1.1030, 1.1080, 1.1020, 1.1060))
    trade = events[-1].trade
    assert trade.exit_reason == "TP"
    assert trade.exit_price == pytest.approx(1.1075)
    assert trade.pips == pytest.approx(75)
    assert trade.pnl == pytest.approx(750)
    assert trade.bars_held == 2
    assert b.balance == pytest.approx(50_750)
    assert b.is_flat("EURUSD")


def test_stop_loss_pays_spread_and_slippage():
    b = make_broker(spread=2.0, slippage=0.5, commission=5.0)
    b.submit(order())
    b.process_bar("EURUSD", bar(T0 + M5, 1.1000, 1.1005, 1.0940, 1.0960))
    trade = b.trades[-1]
    assert trade.exit_reason == "SL"
    assert trade.entry_price == pytest.approx(1.10015)
    assert trade.exit_price == pytest.approx(1.0950 - 0.00015)
    assert trade.commission == pytest.approx(10.0)
    assert trade.pnl == pytest.approx((1.09485 - 1.10015) * 100_000 - 10.0)


@pytest.mark.parametrize(
    "side,o,h,l,expected",
    [
        # Long, both levels inside the bar. High is closer to the open: high first -> TP.
        (Side.LONG, 1.1060, 1.1080, 1.0940, (1.1075, "TP", False)),
        # Low is closer to the open: low first -> SL.
        (Side.LONG, 1.0960, 1.1080, 1.0940, (1.0950, "SL", False)),
        # Short mirrors it: high first -> SL, low first -> TP.
        (Side.SHORT, 1.1040, 1.1060, 1.0900, (1.1050, "SL", False)),
        (Side.SHORT, 1.0930, 1.1060, 1.0900, (1.0925, "TP", False)),
        # Gaps fill at the open.
        (Side.LONG, 1.0930, 1.0990, 1.0920, (1.0930, "SL", True)),
        (Side.LONG, 1.1090, 1.1100, 1.1080, (1.1090, "TP", True)),
        (Side.SHORT, 1.1070, 1.1080, 1.1060, (1.1070, "SL", True)),
        # No level touched.
        (Side.LONG, 1.1000, 1.1070, 1.0960, None),
    ],
)
def test_intrabar_path_rule(side, o, h, l, expected):
    if side is Side.LONG:
        pos = position(side, sl=1.0950, tp=1.1075)
    else:
        pos = position(side, sl=1.1050, tp=1.0925)
    assert exit_trigger(pos, bar(T0, o, h, l, (h + l) / 2)) == expected


def test_position_can_open_and_close_on_the_same_bar():
    b = make_broker()
    b.submit(order())
    events = b.process_bar("EURUSD", bar(T0 + M5, 1.1000, 1.1002, 1.0940, 1.0945))
    assert [e.kind for e in events] == ["ENTRY", "EXIT"]
    assert b.trades[-1].bars_held == 0


def test_jpy_quote_pnl_converted_to_usd():
    b = make_broker(symbols=("CHFJPY",))
    b.rates.update("USDJPY", 150.0)
    b.submit(EntryOrder("CHFJPY", Side.SHORT, 10_000, 171.0, 169.5, T0, 170.0, 1.0, 1 / 150))
    b.process_bar("CHFJPY", bar(T0 + M5, 170.0, 170.2, 169.4, 169.6))
    t = b.trades[-1]
    assert t.exit_reason == "TP"
    assert t.pips == pytest.approx(50)  # JPY pip = 0.01
    assert t.pnl_quote == pytest.approx(5_000)  # 0.5 JPY * 10k units
    assert t.pnl == pytest.approx(5_000 / 150)


def test_state_round_trip_and_guards():
    b = make_broker()
    b.submit(order())
    b.process_bar("EURUSD", bar(T0 + M5, 1.1000, 1.1010, 1.0990, 1.1005))
    with pytest.raises(RuntimeError):
        b.submit(order())
    b2 = make_broker()
    b2.load_state(b.to_state())
    assert b2.positions == b.positions
    assert b2.balance == b.balance and b2.next_id == b.next_id
    # the restored position keeps being managed
    b2.process_bar("EURUSD", bar(T0 + 2 * M5, 1.1005, 1.1080, 1.1000, 1.1070))
    assert b2.trades[-1].exit_reason == "TP"


def test_equity_marks_open_positions():
    b = make_broker()
    b.submit(order())
    b.process_bar("EURUSD", bar(T0 + M5, 1.1000, 1.1010, 1.0990, 1.1005))
    assert b.equity({"EURUSD": 1.1020}) == pytest.approx(50_200)
