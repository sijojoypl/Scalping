"""Market data sources.

Every feed returns *closed* bars only, oldest first, with UTC open times.

* ``YahooFeed``  - free Yahoo Finance chart API, no key (default for paper mode)
* ``OandaFeed``  - OANDA v20 candles; needs an API token (a free practice
                   account works). Only candles are read, no orders are sent.
* ``ReplayFeed`` - historical bars (CSV files or synthetic data) replayed
                   against a simulated clock
"""

from __future__ import annotations

import bisect
import csv
import math
import os
import random
from collections.abc import Iterable
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Protocol
from zoneinfo import ZoneInfo

import requests

from scalper.instruments import Instrument, normalize_symbol
from scalper.models import Bar


class FeedError(RuntimeError):
    pass


class Feed(Protocol):
    def fetch_closed(self, symbol: str, count: int, now: datetime) -> list[Bar]: ...


def is_closed(bar_time: datetime, tf: timedelta, now: datetime) -> bool:
    return bar_time + tf <= now


# --------------------------------------------------------------------- Yahoo
class YahooFeed:
    URL = "https://query1.finance.yahoo.com/v8/finance/chart/{ticker}"
    INTERVALS = {1: "1m", 5: "5m", 15: "15m", 30: "30m", 60: "60m"}
    MAX_DAYS = {1: 7, 5: 59, 15: 59, 30: 59, 60: 700}

    def __init__(self, timeframe_minutes: int, timeout: float = 20.0, session: requests.Session | None = None):
        self.tf_minutes = timeframe_minutes
        self.tf = timedelta(minutes=timeframe_minutes)
        self.timeout = timeout
        self.session = session or requests.Session()
        self.session.headers.setdefault("User-Agent", "Mozilla/5.0 (reverse-rsi-paper-bot)")

    def fetch_closed(self, symbol: str, count: int, now: datetime) -> list[Bar]:
        # Forex trades ~5 days a week; ask for enough calendar days to cover
        # ``count`` bars plus a weekend.
        days = math.ceil(count * self.tf_minutes / 1440 * 7 / 5) + 3
        days = min(days, self.MAX_DAYS[self.tf_minutes])
        params = {
            "interval": self.INTERVALS[self.tf_minutes],
            "period1": int((now - timedelta(days=days)).timestamp()),
            "period2": int(now.timestamp()) + 60,
            "includePrePost": "false",
        }
        url = self.URL.format(ticker=f"{normalize_symbol(symbol)}=X")
        try:
            resp = self.session.get(url, params=params, timeout=self.timeout)
            resp.raise_for_status()
            payload = resp.json()
        except (requests.RequestException, ValueError) as exc:
            raise FeedError(f"Yahoo request for {symbol} failed: {exc}") from exc
        try:
            bars = self.parse(payload, self.tf_minutes)
        except (KeyError, TypeError, IndexError, ValueError, AttributeError) as exc:
            raise FeedError(f"unexpected Yahoo response for {symbol}: {exc!r}") from exc
        return [b for b in bars if is_closed(b.time, self.tf, now)][-count:]

    @staticmethod
    def parse(payload: dict, tf_minutes: int) -> list[Bar]:
        chart = payload.get("chart") or {}
        if chart.get("error"):
            raise FeedError(f"Yahoo error: {chart['error']}")
        results = chart.get("result") or []
        if not results:
            return []
        res = results[0]
        stamps = res.get("timestamp") or []
        quote = ((res.get("indicators") or {}).get("quote") or [{}])[0]
        opens, highs, lows, closes = (quote.get(k) or [] for k in ("open", "high", "low", "close"))
        step = tf_minutes * 60
        bars: dict[int, Bar] = {}
        for i, ts in enumerate(stamps):
            try:
                o, h, l, c = opens[i], highs[i], lows[i], closes[i]
            except IndexError:
                break
            if None in (o, h, l, c) or ts % step:  # skip gaps and the live partial tick
                continue
            bars[ts] = Bar(
                time=datetime.fromtimestamp(ts, tz=timezone.utc),
                open=float(o),
                high=max(float(h), float(o), float(c)),
                low=min(float(l), float(o), float(c)),
                close=float(c),
            )
        return [bars[k] for k in sorted(bars)]


# --------------------------------------------------------------------- OANDA
class OandaFeed:
    HOSTS = {
        "practice": "https://api-fxpractice.oanda.com",
        "live": "https://api-fxtrade.oanda.com",
    }
    GRANULARITY = {1: "M1", 5: "M5", 15: "M15", 30: "M30", 60: "H1"}

    def __init__(
        self,
        timeframe_minutes: int,
        token: str,
        environment: str = "practice",
        timeout: float = 20.0,
        session: requests.Session | None = None,
    ):
        if not token:
            raise FeedError("OANDA feed needs an API token (set the env var named in feed.oanda.token_env)")
        self.tf_minutes = timeframe_minutes
        self.tf = timedelta(minutes=timeframe_minutes)
        self.host = self.HOSTS[environment]
        self.timeout = timeout
        self.session = session or requests.Session()
        self.session.headers["Authorization"] = f"Bearer {token}"

    @classmethod
    def from_env(cls, timeframe_minutes: int, token_env: str, environment: str, timeout: float) -> OandaFeed:
        return cls(timeframe_minutes, os.environ.get(token_env, ""), environment, timeout)

    def fetch_closed(self, symbol: str, count: int, now: datetime) -> list[Bar]:
        inst = Instrument.from_symbol(symbol)
        url = f"{self.host}/v3/instruments/{inst.base}_{inst.quote}/candles"
        params = {"granularity": self.GRANULARITY[self.tf_minutes], "count": min(count + 1, 5000), "price": "M"}
        try:
            resp = self.session.get(url, params=params, timeout=self.timeout)
            resp.raise_for_status()
            payload = resp.json()
        except (requests.RequestException, ValueError) as exc:
            raise FeedError(f"OANDA request for {symbol} failed: {exc}") from exc
        try:
            bars = self.parse(payload)
        except (KeyError, TypeError, IndexError, ValueError, AttributeError) as exc:
            raise FeedError(f"unexpected OANDA response for {symbol}: {exc!r}") from exc
        return [b for b in bars if is_closed(b.time, self.tf, now)][-count:]

    @staticmethod
    def parse(payload: dict) -> list[Bar]:
        bars = []
        for c in payload.get("candles", []):
            if not c.get("complete", False):
                continue
            mid = c["mid"]
            bars.append(
                Bar(
                    time=datetime.strptime(c["time"][:19], "%Y-%m-%dT%H:%M:%S").replace(tzinfo=timezone.utc),
                    open=float(mid["o"]),
                    high=float(mid["h"]),
                    low=float(mid["l"]),
                    close=float(mid["c"]),
                )
            )
        return bars


# -------------------------------------------------------------------- Replay
class ReplayFeed:
    """Serves historical bars as if they were arriving live."""

    def __init__(self, bars_by_symbol: dict[str, list[Bar]], timeframe_minutes: int):
        self.tf = timedelta(minutes=timeframe_minutes)
        self.bars = {normalize_symbol(s): sorted(b, key=lambda x: x.time) for s, b in bars_by_symbol.items()}
        self._times = {s: [b.time for b in bars] for s, bars in self.bars.items()}

    def fetch_closed(self, symbol: str, count: int, now: datetime) -> list[Bar]:
        symbol = normalize_symbol(symbol)
        if symbol not in self.bars:
            return []
        end = bisect.bisect_right(self._times[symbol], now - self.tf)
        return self.bars[symbol][max(0, end - count):end]

    def start_time(self, history_bars: int) -> datetime:
        """A clock start that leaves ``history_bars`` of warm-up for every symbol."""
        starts = []
        for times in self._times.values():
            if not times:
                raise FeedError("replay data contains an empty symbol")
            starts.append(times[min(history_bars, len(times) - 1)])
        return max(starts)

    def end_time(self) -> datetime:
        return max(times[-1] for times in self._times.values()) + self.tf


# ----------------------------------------------------------------------- CSV
_TIME_COLUMNS = ("time", "datetime", "timestamp", "date", "gmt time", "local time", "open time")
_TIME_FORMATS = (
    "%Y.%m.%d %H:%M:%S",
    "%Y.%m.%d %H:%M",
    "%d.%m.%Y %H:%M:%S.%f",
    "%d.%m.%Y %H:%M:%S",
    "%Y%m%d %H%M%S",
    "%m/%d/%Y %H:%M",
    "%d/%m/%Y %H:%M",
)


def _parse_time(raw: str, tz: ZoneInfo) -> datetime:
    raw = raw.strip()
    if raw.replace(".", "", 1).isdigit():
        value = float(raw)
        if value > 1e12:  # milliseconds
            value /= 1000.0
        return datetime.fromtimestamp(value, tz=timezone.utc)
    cleaned = raw.replace(" GMT", "").replace(" UTC", "")
    if cleaned.endswith("Z"):
        cleaned = cleaned[:-1] + "+00:00"
    dt: datetime | None = None
    try:
        dt = datetime.fromisoformat(cleaned)
    except ValueError:
        for fmt in _TIME_FORMATS:
            try:
                dt = datetime.strptime(cleaned, fmt)
                break
            except ValueError:
                continue
    if dt is None:
        raise ValueError(f"unrecognised timestamp: {raw!r}")
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=tz)
    return dt.astimezone(timezone.utc)


def load_csv(path: str | Path, tz_name: str = "UTC") -> list[Bar]:
    """Read OHLC bars from a CSV with a header row.

    Works with TradingView exports (unix ``time`` column), OANDA/MT4/MT5 or
    Dukascopy style date strings, and files written by ``scalper fetch``.
    Separate ``date`` and ``time`` columns are joined.
    """
    tz = ZoneInfo(tz_name)
    path = Path(path)
    with path.open("r", encoding="utf-8-sig", newline="") as fh:
        sample = fh.read(4096)
        fh.seek(0)
        dialect = csv.Sniffer().sniff(sample, delimiters=",;\t")
        reader = csv.reader(fh, dialect)
        header = [h.strip().lower().strip("<>") for h in next(reader)]
        col = {name: i for i, name in enumerate(header)}
        missing = [k for k in ("open", "high", "low", "close") if k not in col]
        if missing:
            raise ValueError(f"{path}: missing column(s) {missing}; header was {header}")
        split_date_time = "date" in col and "time" in col
        time_idx = next((col[c] for c in _TIME_COLUMNS if c in col), None)
        if time_idx is None:
            raise ValueError(f"{path}: no time column found; header was {header}")
        bars: dict[datetime, Bar] = {}
        for row in reader:
            if not row or not any(cell.strip() for cell in row):
                continue
            raw_time = f"{row[col['date']]} {row[col['time']]}" if split_date_time else row[time_idx]
            t = _parse_time(raw_time, tz)
            o, h, l, c = (float(row[col[k]]) for k in ("open", "high", "low", "close"))
            if any(math.isnan(v) for v in (o, h, l, c)):
                continue
            bars[t] = Bar(t, o, h, l, c)
    return [bars[t] for t in sorted(bars)]


def save_csv(path: str | Path, bars: Iterable[Bar]) -> None:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8", newline="") as fh:
        writer = csv.writer(fh)
        writer.writerow(["time", "open", "high", "low", "close"])
        for b in bars:
            writer.writerow([b.time.isoformat(), repr(b.open), repr(b.high), repr(b.low), repr(b.close)])


def find_csv(data_dir: str | Path, symbol: str) -> Path | None:
    """First ``*.csv`` in ``data_dir`` whose file name starts with the symbol."""
    folder = Path(data_dir)
    if not folder.is_dir():
        return None
    symbol = normalize_symbol(symbol)
    for p in sorted(folder.iterdir()):
        if p.suffix.lower() != ".csv":
            continue
        stem = p.stem.upper().replace("_", "").replace("-", "").replace("/", "")
        if stem.startswith(symbol):
            return p
    return None


def load_csv_dir(data_dir: str | Path, symbols: Iterable[str], tz_name: str = "UTC") -> dict[str, list[Bar]]:
    """Load the CSV of each symbol that has one; symbols without a file are omitted."""
    out = {}
    for s in symbols:
        p = find_csv(data_dir, s)
        if p is not None:
            out[normalize_symbol(s)] = load_csv(p, tz_name)
    return out


# ----------------------------------------------------------------- Synthetic
_LEVELS = {
    "USDCHF": 0.88, "CHFJPY": 170.0, "AUDCAD": 0.905, "GBPAUD": 2.02,
    "USDJPY": 150.0, "USDCAD": 1.37, "AUDUSD": 0.66, "GBPUSD": 1.30,
    "EURUSD": 1.10, "NZDUSD": 0.60, "EURCHF": 0.95,
}
_NY = ZoneInfo("America/New_York")


def _market_open(t: datetime) -> bool:
    """Forex hours: closed from Friday 17:00 to Sunday 17:00 New York time."""
    local = t.astimezone(_NY)
    wd, hour = local.weekday(), local.hour
    if wd == 5:
        return False
    if wd == 4 and hour >= 17:
        return False
    if wd == 6 and hour < 17:
        return False
    return True


def generate_synthetic(
    symbols: Iterable[str],
    days: int = 30,
    seed: int = 7,
    timeframe_minutes: int = 5,
    start: datetime | None = None,
) -> dict[str, list[Bar]]:
    """Mean-reverting random walks with weekend gaps.

    Handy for offline smoke tests. The prices are made up, so the P&L they
    produce says nothing about how the strategy does on real markets.
    """
    start = start or datetime(2026, 1, 4, 22, 0, tzinfo=timezone.utc)
    tf = timedelta(minutes=timeframe_minutes)
    steps = int(days * 1440 / timeframe_minutes)
    out: dict[str, list[Bar]] = {}
    for idx, symbol in enumerate(symbols):
        symbol = normalize_symbol(symbol)
        rng = random.Random(seed * 1000 + idx)
        level = _LEVELS.get(symbol, 1.0)
        sigma = level * 0.00035 * math.sqrt(timeframe_minutes / 5)
        price = mean = level
        bars = []
        t = start
        for _ in range(steps):
            if _market_open(t):
                mean += rng.gauss(0, sigma * 0.6)
                o = price
                c = o + 0.05 * (mean - o) + rng.gauss(0, sigma)
                h = max(o, c) + abs(rng.gauss(0, sigma * 0.5))
                l = min(o, c) - abs(rng.gauss(0, sigma * 0.5))
                bars.append(Bar(t, o, h, l, c))
                price = c
            t += tf
        out[symbol] = bars
    return out
