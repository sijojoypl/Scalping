"""Performance summary in the spirit of TradingView's Strategy Tester overview."""

from __future__ import annotations

from dataclasses import dataclass

from scalper.models import Trade


@dataclass
class Stats:
    trades: int
    wins: int
    losses: int
    net_profit: float
    gross_profit: float
    gross_loss: float
    profit_factor: float | None
    win_rate: float | None
    avg_trade: float | None
    avg_bars: float | None
    max_drawdown: float
    max_drawdown_pct: float
    net_profit_pct: float


def compute_stats(trades: list[Trade], initial_capital: float) -> Stats:
    trades = sorted(trades, key=lambda t: (t.exit_time, t.id))
    gross_profit = sum(t.pnl for t in trades if t.pnl > 0)
    gross_loss = -sum(t.pnl for t in trades if t.pnl < 0)
    net = gross_profit - gross_loss
    wins = sum(1 for t in trades if t.pnl > 0)
    n = len(trades)

    # Drawdown on the closed-trade equity curve.
    equity = peak = initial_capital
    max_dd = max_dd_pct = 0.0
    for t in trades:
        equity += t.pnl
        peak = max(peak, equity)
        dd = peak - equity
        if dd > max_dd:
            max_dd = dd
            max_dd_pct = dd / peak * 100 if peak else 0.0

    return Stats(
        trades=n,
        wins=wins,
        losses=n - wins,
        net_profit=net,
        gross_profit=gross_profit,
        gross_loss=gross_loss,
        profit_factor=(gross_profit / gross_loss) if gross_loss > 0 else None,
        win_rate=(wins / n * 100) if n else None,
        avg_trade=(net / n) if n else None,
        avg_bars=(sum(t.bars_held for t in trades) / n) if n else None,
        max_drawdown=max_dd,
        max_drawdown_pct=max_dd_pct,
        net_profit_pct=net / initial_capital * 100,
    )


def _fmt(value: float | None, spec: str, suffix: str = "") -> str:
    return "n/a" if value is None else f"{value:{spec}}{suffix}"


def format_stats(stats: Stats, currency: str = "USD") -> str:
    rows = [
        ("Net profit", f"{stats.net_profit:,.2f} {currency} ({stats.net_profit_pct:+.2f}%)"),
        ("Closed trades", f"{stats.trades} ({stats.wins} won / {stats.losses} lost)"),
        ("Percent profitable", _fmt(stats.win_rate, ".2f", "%")),
        ("Profit factor", _fmt(stats.profit_factor, ".3f")),
        ("Max drawdown", f"{stats.max_drawdown:,.2f} {currency} ({stats.max_drawdown_pct:.2f}%)"),
        ("Avg trade", _fmt(stats.avg_trade, ",.2f", f" {currency}")),
        ("Avg bars in trade", _fmt(stats.avg_bars, ".1f")),
    ]
    width = max(len(k) for k, _ in rows)
    return "\n".join(f"  {k:<{width}}  {v}" for k, v in rows)


def per_symbol_table(trades: list[Trade], initial_capital: float, currency: str = "USD") -> str:
    symbols = sorted({t.symbol for t in trades})
    if not symbols:
        return "  (no closed trades)"
    lines = [f"  {'Symbol':<8} {'Trades':>6} {'Win %':>7} {'PF':>7} {'Net ' + currency:>14}"]
    for s in symbols:
        st = compute_stats([t for t in trades if t.symbol == s], initial_capital)
        lines.append(
            f"  {s:<8} {st.trades:>6} {_fmt(st.win_rate, '.1f'):>7} "
            f"{_fmt(st.profit_factor, '.2f'):>7} {st.net_profit:>14,.2f}"
        )
    return "\n".join(lines)
