"""Market data sources.

Every feed returns *closed* bars only, oldest first, with UTC open times.

* ``YahooFeed``  - free Yahoo Finance chart API, no key (default for paper mode)
* ``OandaFeed``  - OANDA v20 candles; needs an API token (a free practice
                   account works). Only candles are read, no orders are sent.
* ``DukascopyFeed`` - Dukascopy's free historical 1-minute candles, no key.
                   Published per finished day, so it is for ``fetch`` and
                   backtests, not live polling.
* ``ReplayFeed`` - historical bars (CSV files or synthetic data) replayed
                   against a simulated clock
"""

from __future__ import annotations

import bisect
import csv
import logging
import lzma
import math
import os
import random
import re
import struct
import threading
import time
from collections.abc import Iterable
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Protocol
from zoneinfo import ZoneInfo

import requests

from scalper.instruments import Instrument, normalize_symbol
from scalper.models import Bar

log = logging.getLogger(__name__)


class FeedError(RuntimeError):
    pass


class Feed(Protocol):
    def fetch_closed(self, symbol: str, count: int, now: datetime) -> list[Bar]: ...


def is_closed(bar_time: datetime, tf: timedelta, now: datetime) -> bool:
    return bar_time + tf <= now


# --------------------------------------------------------------------- Yahoo
BROWSER_USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/140.0.0.0 Safari/537.36"
)


def browser_session():
    """An HTTP session Yahoo (and Dukascopy) will talk to.

    Yahoo answers 429 "Too Many Requests" to clients that do not look like a
    browser, sometimes on the very first request. ``curl_cffi`` (in
    requirements.txt) makes the TLS handshake look like Chrome's, which is
    what the yfinance library switched to for the same reason. Without it,
    fall back to ``requests`` with browser headers.
    """
    try:
        from curl_cffi import requests as cffi_requests
    except ImportError:
        session = requests.Session()
        session.headers.update(
            {
                "User-Agent": BROWSER_USER_AGENT,
                "Accept": "application/json,text/plain,*/*",
                "Accept-Language": "en-US,en;q=0.9",
            }
        )
        return session
    return cffi_requests.Session(impersonate="chrome")


class YahooFeed:
    HOSTS = ("query1.finance.yahoo.com", "query2.finance.yahoo.com")
    INTERVALS = {1: "1m", 5: "5m", 15: "15m", 30: "30m", 60: "60m"}
    MAX_DAYS = {1: 7, 5: 59, 15: 59, 30: 59, 60: 700}

    def __init__(
        self,
        timeframe_minutes: int,
        timeout: float = 20.0,
        session=None,
        settle_seconds: float = 10.0,
        retries: int = 3,
        backoff_seconds: float = 3.0,
        min_interval_seconds: float = 1.0,
        sleep=time.sleep,
    ):
        self.tf_minutes = timeframe_minutes
        self.tf = timedelta(minutes=timeframe_minutes)
        # Yahoo has no "complete" flag and keeps updating a bar for a few
        # seconds after it closes, so wait this long before trusting it.
        self.settle = timedelta(seconds=settle_seconds)
        self.timeout = timeout
        self.session = session if session is not None else browser_session()
        self.retries = retries
        self.backoff = backoff_seconds
        self.min_interval = min_interval_seconds
        self._sleep = sleep
        self._last_request = -math.inf

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
        payload = self._request(normalize_symbol(symbol), params)
        try:
            bars = self.parse(payload, self.tf_minutes)
        except (KeyError, TypeError, IndexError, ValueError, AttributeError) as exc:
            raise FeedError(f"unexpected Yahoo response for {symbol}: {exc!r}") from exc
        return [b for b in bars if is_closed(b.time, self.tf + self.settle, now)][-count:]

    def _request(self, symbol: str, params: dict) -> dict:
        """GET the chart JSON, retrying 429/5xx and network errors with backoff."""
        problem = ""
        for attempt in range(self.retries + 1):
            if attempt:
                self._sleep(self.backoff * 2 ** (attempt - 1))
            wait = self._last_request + self.min_interval - time.monotonic()
            if wait > 0:
                self._sleep(wait)
            self._last_request = time.monotonic()
            url = f"https://{self.HOSTS[attempt % len(self.HOSTS)]}/v8/finance/chart/{symbol}=X"
            try:
                resp = self.session.get(url, params=params, timeout=self.timeout)
            except OSError as exc:  # requests and curl_cffi errors both derive from OSError
                problem = str(exc)
                continue
            status = resp.status_code
            if status == 429 or status >= 500:
                problem = f"HTTP {status}" + (" Too Many Requests" if status == 429 else "")
                continue
            if status >= 400:
                raise FeedError(f"Yahoo request for {symbol} failed: HTTP {status}")
            try:
                return resp.json()
            except ValueError as exc:
                raise FeedError(f"Yahoo sent a non-JSON reply for {symbol}: {exc}") from exc
        raise FeedError(f"Yahoo request for {symbol} failed after {self.retries + 1} tries: {problem}")

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

    MAX_PER_REQUEST = 5000

    def fetch_closed(self, symbol: str, count: int, now: datetime) -> list[Bar]:
        """Newest ``count`` complete candles, paging backwards 5000 at a time."""
        inst = Instrument.from_symbol(symbol)
        url = f"{self.host}/v3/instruments/{inst.base}_{inst.quote}/candles"
        found: dict[datetime, Bar] = {}
        before: datetime | None = None
        while len(found) < count:
            want = min(count - len(found) + 1, self.MAX_PER_REQUEST)
            params = {"granularity": self.GRANULARITY[self.tf_minutes], "count": want, "price": "M"}
            if before is not None:
                params["to"] = before.strftime("%Y-%m-%dT%H:%M:%SZ")
            chunk = self._get(url, params, symbol)
            older = [b for b in chunk if before is None or b.time < before]
            if not older:
                break
            found.update((b.time, b) for b in older)
            before = older[0].time
            if len(chunk) < want - 1:  # the broker has no more history
                break
        bars = [found[t] for t in sorted(found)]
        return [b for b in bars if is_closed(b.time, self.tf, now)][-count:]

    def _get(self, url: str, params: dict, symbol: str) -> list[Bar]:
        try:
            resp = self.session.get(url, params=params, timeout=self.timeout)
            resp.raise_for_status()
            payload = resp.json()
        except (requests.RequestException, ValueError) as exc:
            raise FeedError(f"OANDA request for {symbol} failed: {exc}") from exc
        try:
            return self.parse(payload)
        except (KeyError, TypeError, IndexError, ValueError, AttributeError) as exc:
            raise FeedError(f"unexpected OANDA response for {symbol}: {exc!r}") from exc

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


# ----------------------------------------------------------------- Dukascopy
class DukascopyFeed:
    """Free historical candles from Dukascopy's public datafeed.

    Each UTC day is one LZMA-compressed ``.bi5`` file of 1-minute BID candles.
    A record is 24 big-endian bytes: seconds since midnight, open, close, low
    and high as integer points, and a float volume. Minutes with no volume
    (weekends, market closed) are dropped, the rest are merged into bars of the
    configured timeframe.
    """

    URL = "https://datafeed.dukascopy.com/datafeed/{symbol}/{y}/{m:02d}/{d:02d}/BID_candles_min_1.bi5"
    RECORD = struct.Struct(">5if")

    def __init__(
        self,
        timeframe_minutes: int,
        timeout: float = 30.0,
        session=None,
        retries: int = 6,
        backoff_seconds: float = 5.0,
        workers: int = 2,
        cache_dir: str | Path | None = None,
        sleep=time.sleep,
        clock=time.monotonic,
        progress=None,
    ):
        self.tf_minutes = timeframe_minutes
        self.tf = timedelta(minutes=timeframe_minutes)
        self.timeout = timeout
        # One HTTP session per download thread; an injected session is shared.
        self._new_session = (lambda: session) if session is not None else browser_session
        self._local = threading.local()
        self.retries = retries
        self.backoff = backoff_seconds
        self.workers = workers
        self.cache_dir = Path(cache_dir) if cache_dir else None
        self._sleep = sleep
        self._clock = clock
        self._progress = progress
        # Dukascopy throttles with 503s and stalled connections. When any
        # download is pushed back, all of them pause until this moment.
        self._lock = threading.Lock()
        self._resume_at = -math.inf
        self.retry_count = 0

    def fetch_closed(self, symbol: str, count: int, now: datetime) -> list[Bar]:
        symbol = normalize_symbol(symbol)
        scale = 1_000 if symbol.endswith("JPY") else 100_000  # prices are stored as integer points
        first_day = (now - timedelta(days=math.ceil(count * self.tf_minutes / 1440) + 1)).date()
        days = []
        day = first_day
        while day <= now.date():
            if day.weekday() != 5:  # nothing trades on Saturday
                days.append(day)
            day += timedelta(days=1)

        files: dict = {}
        todo = []
        for d in days:
            cached = self._cache_get(symbol, d)
            if cached is None:
                todo.append(d)
            else:
                files[d] = cached
        self.retry_count = 0
        failed: dict = {}
        done = len(files)
        pool = ThreadPoolExecutor(max_workers=self.workers)
        try:
            futures = {pool.submit(self._download, symbol, d): d for d in todo}
            for fut in as_completed(futures):
                d = futures[fut]
                try:
                    files[d] = fut.result()
                    self._cache_put(symbol, d, files[d], now)
                except FeedError as exc:
                    failed[d] = exc
                done += 1
                if self._progress and (done % 25 == 0 or done == len(days)):
                    self._progress(symbol, done, len(days), self.retry_count)
        finally:
            pool.shutdown(wait=True, cancel_futures=True)

        for d in sorted(failed):  # one more, slower pass for days that kept failing
            try:
                files[d] = self._download(symbol, d)
                self._cache_put(symbol, d, files[d], now)
                del failed[d]
            except FeedError as exc:
                failed[d] = exc
        if failed and not any(files.values()):
            raise FeedError(f"Dukascopy sent no data for {symbol}: {next(iter(failed.values()))}")
        if failed:
            missing = ", ".join(d.isoformat() for d in sorted(failed))
            log.warning(
                "%s: %d day(s) could not be downloaded (%s). Run the same fetch again to fill them in.",
                symbol, len(failed), missing,
            )

        minutes: list[Bar] = []
        for d in days:
            if files.get(d):
                start = datetime(d.year, d.month, d.day, tzinfo=timezone.utc)
                minutes.extend(self.decode(files[d], start, scale, symbol))
        bars = aggregate(minutes, self.tf_minutes)
        return [b for b in bars if is_closed(b.time, self.tf, now)][-count:]

    # ------------------------------------------------------------ download
    def _session(self):
        if not hasattr(self._local, "session"):
            self._local.session = self._new_session()
        return self._local.session

    def _wait_turn(self) -> None:
        while True:
            with self._lock:
                wait = self._resume_at - self._clock()
            if wait <= 0:
                return
            self._sleep(wait)

    def _push_back(self, attempt: int) -> None:
        with self._lock:
            self.retry_count += 1
            pause = self.backoff * 2 ** min(attempt, 4)
            self._resume_at = max(self._resume_at, self._clock() + pause)

    def _download(self, symbol: str, day) -> bytes:
        url = self.URL.format(symbol=symbol, y=day.year, m=day.month - 1, d=day.day)  # months are 0-based
        problem = ""
        for attempt in range(self.retries + 1):
            if attempt:
                log.debug("Dukascopy %s %s: %s, retrying", symbol, day, problem)
            self._wait_turn()
            try:
                resp = self._session().get(url, timeout=self.timeout)
            except OSError as exc:
                problem = "timed out" if "timed out" in str(exc).lower() else (str(exc) or type(exc).__name__)
                self._push_back(attempt)
                continue
            if resp.status_code == 404:
                return b""  # no file for this day (holiday, or not published yet)
            if resp.status_code == 429 or resp.status_code >= 500:
                problem = f"HTTP {resp.status_code}"
                self._push_back(attempt)
                continue
            if resp.status_code >= 400:
                raise FeedError(f"Dukascopy request for {symbol} {day} failed: HTTP {resp.status_code}")
            return resp.content
        raise FeedError(f"Dukascopy request for {symbol} {day} failed after {self.retries + 1} tries: {problem}")

    # --------------------------------------------------------------- cache
    def _cache_path(self, symbol: str, day) -> Path | None:
        return self.cache_dir / symbol / f"{day.isoformat()}.bi5" if self.cache_dir else None

    def _cache_get(self, symbol: str, day) -> bytes | None:
        path = self._cache_path(symbol, day)
        if path is None or not path.exists():
            return None
        return path.read_bytes()

    def _cache_put(self, symbol: str, day, raw: bytes, now: datetime) -> None:
        path = self._cache_path(symbol, day)
        # Skip today and yesterday: their files may not be final yet.
        if path is None or day >= (now - timedelta(days=1)).date():
            return
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(".tmp")
        tmp.write_bytes(raw)
        os.replace(tmp, path)

    @classmethod
    def decode(cls, raw: bytes, day_start: datetime, scale: int, symbol: str = "") -> list[Bar]:
        try:
            data = lzma.decompress(raw)
        except lzma.LZMAError as exc:
            raise FeedError(f"Dukascopy {symbol} {day_start.date()}: not an LZMA file ({exc})") from exc
        if len(data) % cls.RECORD.size:
            raise FeedError(f"Dukascopy {symbol} {day_start.date()}: unexpected record size")
        bars = []
        for secs, o, c, lo, hi, volume in cls.RECORD.iter_unpack(data):
            if volume <= 0:
                continue
            if not (lo <= min(o, c) and hi >= max(o, c) and lo > 0):
                raise FeedError(
                    f"Dukascopy {symbol} {day_start.date()}: candle fields out of order; "
                    f"the file format may have changed"
                )
            bars.append(
                Bar(day_start + timedelta(seconds=secs), o / scale, hi / scale, lo / scale, c / scale)
            )
        return bars


def aggregate(bars: list[Bar], minutes: int) -> list[Bar]:
    """Merge consecutive bars into ``minutes``-long bars aligned to the clock."""
    step = minutes * 60
    out: list[Bar] = []
    for b in bars:
        ts = int(b.time.timestamp())
        start = datetime.fromtimestamp(ts - ts % step, tz=timezone.utc)
        if out and out[-1].time == start:
            last = out[-1]
            out[-1] = Bar(start, last.open, max(last.high, b.high), min(last.low, b.low), b.close)
        else:
            out.append(Bar(start, b.open, b.high, b.low, b.close))
    return out


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


def _parse_raw(raw: str) -> datetime:
    """Parse a CSV timestamp. Returns an aware datetime, or a naive one if the text has no zone."""
    raw = raw.strip()
    if raw.replace(".", "", 1).isdigit():
        value = float(raw)
        if value > 1e12:  # milliseconds
            value /= 1000.0
        return datetime.fromtimestamp(value, tz=timezone.utc)
    cleaned = raw.replace(" GMT", "").replace(" UTC", "")
    if cleaned.endswith("Z"):
        cleaned = cleaned[:-1] + "+00:00"
    try:
        return datetime.fromisoformat(cleaned)
    except ValueError:
        pass
    for fmt in _TIME_FORMATS:
        try:
            return datetime.strptime(cleaned, fmt)
        except ValueError:
            continue
    raise ValueError(f"unrecognised timestamp: {raw!r}")


def _localize(naive: datetime, tz: ZoneInfo, prev_utc: datetime | None) -> datetime:
    """Attach ``tz`` to a naive local time.

    In the repeated hour after a DST fall-back the same wall time happens
    twice. Rows arrive in order, so when the first reading would step back in
    time, the row belongs to the second pass (``fold=1``).
    """
    aware = naive.replace(tzinfo=tz)
    if prev_utc is not None and aware.astimezone(timezone.utc) <= prev_utc:
        second = naive.replace(tzinfo=tz, fold=1)
        if second.astimezone(timezone.utc) > prev_utc:
            return second
    return aware


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
        prev: datetime | None = None
        for row in reader:
            if not row or not any(cell.strip() for cell in row):
                continue
            raw_time = f"{row[col['date']]} {row[col['time']]}" if split_date_time else row[time_idx]
            dt = _parse_raw(raw_time)
            if dt.tzinfo is None:
                dt = _localize(dt, tz, prev)
            t = dt.astimezone(timezone.utc)
            prev = t
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
    """The CSV in ``data_dir`` for ``symbol``.

    Matches ``USDCHF_M5.csv``, ``usdchf.csv`` and TradingView export names such
    as ``FX_USDCHF, 5.csv`` or ``OANDA_USDCHF, 5.csv``.
    """
    folder = Path(data_dir)
    if not folder.is_dir():
        return None
    symbol = normalize_symbol(symbol)
    files = sorted(p for p in folder.iterdir() if p.suffix.lower() == ".csv")
    for p in files:
        stem = p.stem.upper().replace("_", "").replace("-", "").replace("/", "")
        if stem.startswith(symbol):
            return p
    pattern = re.compile(rf"(?<![A-Z]){symbol}(?![A-Z])")
    for p in files:
        if pattern.search(p.stem.upper()):
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
