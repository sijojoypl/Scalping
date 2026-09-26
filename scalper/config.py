"""Bot configuration: dataclasses with Pine-matching defaults plus a YAML loader.

Unknown keys in the YAML file are rejected so a typo cannot silently fall back
to a default value.
"""

from __future__ import annotations

import dataclasses
from dataclasses import dataclass, field
from datetime import time
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

import yaml

from scalper.instruments import normalize_symbol
from scalper.session import SessionWindow

SUPPORTED_MODES = ("PAPER",)
SUPPORTED_TIMEFRAMES = (1, 5, 15, 30, 60)
FEED_PROVIDERS = ("yahoo", "oanda", "csv", "synthetic")
STRATEGIES = ("reverse_rsi", "london_breakout")


class ConfigError(ValueError):
    pass


@dataclass
class StrategyParams:
    """Inputs of ``Strategy - Reverse RSI.pine`` (defaults are the Pine defaults)."""

    rsi_length: int = 20
    rsi_ma_length: int = 5
    upper_limit: float = 55.0
    lower_limit: float = 49.0
    ema_lengths: list[int] = field(default_factory=lambda: [4, 10, 15, 19, 25])
    session: str = "1600-1900"
    # TradingView evaluates Pine sessions in the exchange timezone. FXCM and
    # OANDA forex symbols on TradingView use New York time.
    session_timezone: str = "America/New_York"
    atr_length: int = 14
    atr_mult: float = 2.0
    profit_multiple: float = 1.5
    sl_multiple: float = 1.0
    risk_per_trade: float = 0.01


@dataclass
class BreakoutParams:
    """London breakout: trade the first clean break of the overnight range.

    All times are local to ``timezone`` so they follow London's clock changes.
    """

    timezone: str = "Europe/London"
    range_window: str = "0000-0700"  # the Asian-session range is built from these bars
    entry_window: str = "0700-1000"  # a close outside the range here is a signal
    exit_time: str = "1600"  # anything still open is closed at this time
    buffer_frac: float = 0.1  # the close must clear the range by this share of its height
    stop: str = "mid"  # "mid" = middle of the range, "opposite" = other side of it
    profit_multiple: float = 1.0  # target = stop distance x this
    min_range_spreads: float = 10.0  # skip days whose range is under this many spreads
    trades_per_day: int = 1  # per pair
    risk_per_trade: float = 0.01


@dataclass
class AccountConfig:
    currency: str = "USD"
    initial_capital: float = 50_000.0
    # "initial": size every trade off the starting capital, as the Pine script
    # does. "equity": size off the current paper balance (compounding).
    sizing_basis: str = "initial"


@dataclass
class CostConfig:
    # Full bid/ask spread in pips; "default" covers symbols not listed.
    spread_pips: dict[str, float] = field(default_factory=lambda: {"default": 0.0})
    slippage_pips: float = 0.0  # applied to market entries and stop exits
    commission_per_100k: float = 0.0  # account currency per 100k units, per side

    def spread_for(self, symbol: str) -> float:
        return float(self.spread_pips.get(symbol, self.spread_pips.get("default", 0.0)))


@dataclass
class RiskConfig:
    max_open_positions: int | None = None
    max_daily_loss_pct: float | None = None  # e.g. 3.0 stops new entries for the UTC day
    # Skip a signal when its stop is smaller than this many spreads (e.g. 5.0
    # needs a 7.5 pip stop at a 1.5 pip spread). Costs eat tight stops alive.
    min_stop_spread_ratio: float | None = None
    # No new entries on bars opening inside these windows (strategy session
    # timezone), e.g. ["1645-1730"] to stay out of the 17:00 New York rollover.
    no_entry_windows: list[str] = field(default_factory=list)


@dataclass
class OandaConfig:
    environment: str = "practice"  # "practice" or "live" (candles only; no orders are sent)
    token_env: str = "OANDA_API_TOKEN"


@dataclass
class FeedConfig:
    provider: str = "yahoo"
    history_bars: int = 500
    poll_delay_seconds: float = 15.0
    request_timeout: float = 20.0
    data_dir: str = "data"  # csv provider: folder with one CSV per symbol
    csv_timezone: str = "UTC"  # timezone of naive timestamps in CSV files
    synthetic_days: int = 30
    synthetic_seed: int = 7
    oanda: OandaConfig = field(default_factory=OandaConfig)


@dataclass
class Config:
    mode: str = "PAPER"
    strategy_name: str = "reverse_rsi"  # or "london_breakout"
    symbols: list[str] = field(default_factory=lambda: ["USDCHF", "CHFJPY", "AUDCAD", "GBPAUD"])
    timeframe_minutes: int = 5
    strategy: StrategyParams = field(default_factory=StrategyParams)
    account: AccountConfig = field(default_factory=AccountConfig)
    costs: CostConfig = field(default_factory=CostConfig)
    risk: RiskConfig = field(default_factory=RiskConfig)
    feed: FeedConfig = field(default_factory=FeedConfig)
    breakout: BreakoutParams = field(default_factory=BreakoutParams)
    # Used only when no live price for a conversion pair is available yet.
    # Value = account currency per 1 unit of the currency.
    fx_fallback_rates: dict[str, float] = field(default_factory=dict)
    state_dir: str = "state"
    log_dir: str = "logs"
    log_level: str = "INFO"

    @property
    def active_timezone(self) -> str:
        """Timezone the active strategy's clock runs in (for reports)."""
        return self.breakout.timezone if self.strategy_name == "london_breakout" else self.strategy.session_timezone

    @property
    def risk_per_trade(self) -> float:
        return self.breakout.risk_per_trade if self.strategy_name == "london_breakout" else self.strategy.risk_per_trade

    def describe_strategy(self) -> str:
        if self.strategy_name == "london_breakout":
            b = self.breakout
            return (
                f"london_breakout: range {b.range_window}, entries {b.entry_window}, flat at {b.exit_time} "
                f"({b.timezone}); stop {b.stop}, target {b.profit_multiple:g}R"
            )
        return f"reverse_rsi: session {self.strategy.session} ({self.strategy.session_timezone})"

    def _validate_breakout(self) -> None:
        b = self.breakout
        try:
            ZoneInfo(b.timezone)
        except Exception as exc:  # zoneinfo raises several types
            raise ConfigError(f"breakout.timezone: unknown timezone {b.timezone!r}") from exc
        b.exit_time = _hhmm_text(b.exit_time)
        try:
            rng = _window(b.range_window)
            entry = _window(b.entry_window)
            exit_t = _hhmm(b.exit_time)
        except ValueError as exc:
            raise ConfigError(f"breakout: {exc}") from exc
        if entry[0] < rng[1]:
            raise ConfigError("breakout.entry_window must start after range_window ends")
        if exit_t < entry[1]:
            raise ConfigError("breakout.exit_time must not be before entry_window ends")
        if b.stop not in ("mid", "opposite"):
            raise ConfigError("breakout.stop must be 'mid' or 'opposite'")
        if b.profit_multiple <= 0 or b.buffer_frac < 0 or b.min_range_spreads < 0:
            raise ConfigError("breakout: profit_multiple must be positive; buffer_frac and min_range_spreads >= 0")
        if b.trades_per_day < 1:
            raise ConfigError("breakout.trades_per_day must be >= 1")
        if not 0 < b.risk_per_trade <= 0.1:
            raise ConfigError("breakout.risk_per_trade must be in (0, 0.1]")

    def validate(self) -> Config:
        self.mode = self.mode.upper()
        if self.mode not in SUPPORTED_MODES:
            raise ConfigError(
                f"mode {self.mode!r} is not supported: this bot only trades on paper "
                f"(set mode: PAPER)"
            )
        if not self.symbols:
            raise ConfigError("symbols must not be empty")
        try:
            self.symbols = [normalize_symbol(s) for s in self.symbols]
        except ValueError as exc:
            raise ConfigError(str(exc)) from exc
        if len(set(self.symbols)) != len(self.symbols):
            raise ConfigError("symbols contains duplicates")
        if self.timeframe_minutes not in SUPPORTED_TIMEFRAMES:
            raise ConfigError(f"timeframe_minutes must be one of {SUPPORTED_TIMEFRAMES}")
        self.account.currency = self.account.currency.upper()
        if self.account.currency != "USD":
            raise ConfigError("only a USD account currency is supported (as in the Pine script)")
        if self.account.initial_capital <= 0:
            raise ConfigError("account.initial_capital must be positive")
        if self.account.sizing_basis not in ("initial", "equity"):
            raise ConfigError("account.sizing_basis must be 'initial' or 'equity'")
        s = self.strategy
        try:
            SessionWindow.parse(s.session, s.session_timezone)
        except ValueError as exc:
            raise ConfigError(f"strategy.session: {exc}") from exc
        if len(s.ema_lengths) != 5 or any(n < 1 for n in s.ema_lengths):
            raise ConfigError("strategy.ema_lengths needs five positive periods")
        for name in ("rsi_length", "rsi_ma_length", "atr_length"):
            if getattr(s, name) < 1:
                raise ConfigError(f"strategy.{name} must be >= 1")
        if not 0 < s.risk_per_trade <= 0.1:
            raise ConfigError("strategy.risk_per_trade must be in (0, 0.1] like the Pine input")
        if s.atr_mult <= 0 or s.sl_multiple <= 0 or s.profit_multiple <= 0:
            raise ConfigError("strategy atr_mult, sl_multiple and profit_multiple must be positive")
        self.strategy_name = self.strategy_name.lower()
        if self.strategy_name not in STRATEGIES:
            raise ConfigError(f"strategy_name must be one of {STRATEGIES}")
        self._validate_breakout()
        self.feed.provider = self.feed.provider.lower()
        if self.feed.provider not in FEED_PROVIDERS:
            raise ConfigError(f"feed.provider must be one of {FEED_PROVIDERS}")
        if self.feed.history_bars < 100:
            raise ConfigError("feed.history_bars must be >= 100 so indicators can warm up")
        ratio = self.risk.min_stop_spread_ratio
        if ratio is not None and ratio < 0:
            raise ConfigError("risk.min_stop_spread_ratio must be positive (or null to turn it off)")
        if ratio == 0:
            self.risk.min_stop_spread_ratio = None
        for window in self.risk.no_entry_windows:
            try:
                SessionWindow.parse(f"{window}:1234567", self.strategy.session_timezone)
            except ValueError as exc:
                raise ConfigError(f"risk.no_entry_windows: {exc}") from exc
        if self.feed.oanda.environment not in ("practice", "live"):
            raise ConfigError("feed.oanda.environment must be 'practice' or 'live'")
        self.fx_fallback_rates = {k.upper(): float(v) for k, v in self.fx_fallback_rates.items()}
        self.costs.spread_pips = {
            (k if k == "default" else normalize_symbol(k)): float(v)
            for k, v in self.costs.spread_pips.items()
        }
        return self


def _hhmm_text(value: Any) -> str:
    """YAML reads 0700 as the number 700; turn it back into "0700"."""
    return f"{value:04d}" if isinstance(value, int) else str(value)


def _hhmm(text: str) -> time:
    text = _hhmm_text(text)
    if len(text) != 4 or not text.isdigit():
        raise ValueError(f"bad time {text!r}; expected HHMM")
    return time(int(text[:2]), int(text[2:]))


def _window(text: str) -> tuple[time, time]:
    """``"0700-1000"`` -> (07:00, 10:00); the window must not cross midnight."""
    try:
        a, b = str(text).split("-")
        start, end = _hhmm(a), _hhmm(b)
    except ValueError as exc:
        raise ValueError(f"bad window {text!r}; expected HHMM-HHMM within one day") from exc
    if not start < end:
        raise ValueError(f"window {text!r} must start before it ends, within one day")
    return start, end


def _build(cls: type, data: dict[str, Any], path: str) -> Any:
    if not isinstance(data, dict):
        raise ConfigError(f"{path or 'config'} must be a mapping")
    known = {f.name: f for f in dataclasses.fields(cls)}
    unknown = sorted(set(data) - set(known))
    if unknown:
        where = path or "top level"
        raise ConfigError(f"unknown config key(s) at {where}: {', '.join(unknown)}")
    kwargs: dict[str, Any] = {}
    for name, value in data.items():
        f = known[name]
        default = f.default_factory() if f.default_factory is not dataclasses.MISSING else f.default
        if dataclasses.is_dataclass(default):
            kwargs[name] = _build(type(default), value or {}, f"{path}.{name}".lstrip("."))
        else:
            kwargs[name] = value
    return cls(**kwargs)


def load_config(path: str | Path | None) -> Config:
    """Load a YAML config file; ``None`` returns the defaults."""
    if path is None:
        return Config().validate()
    p = Path(path)
    if not p.exists():
        raise ConfigError(f"config file not found: {p}")
    with p.open("r", encoding="utf-8") as fh:
        data = yaml.safe_load(fh) or {}
    return _build(Config, data, "").validate()
