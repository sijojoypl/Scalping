from datetime import datetime, timedelta, timezone

import pytest

from scalper.feeds import (
    FeedError,
    OandaFeed,
    ReplayFeed,
    YahooFeed,
    find_csv,
    generate_synthetic,
    load_csv,
    save_csv,
)
from scalper.models import Bar

UTC = timezone.utc


class FakeResponse:
    def __init__(self, payload, status=200, content=b""):
        self.payload = payload
        self.status_code = status
        self.content = content

    def raise_for_status(self):
        if self.status_code >= 400:
            import requests

            raise requests.HTTPError(f"HTTP {self.status_code}")

    def json(self):
        return self.payload


class FakeSession:
    def __init__(self, payload, status=200):
        self.payload = payload
        self.status = status
        self.headers = {}
        self.calls = []

    def get(self, url, params=None, timeout=None):
        self.calls.append((url, params))
        return FakeResponse(self.payload, self.status)


def yahoo_payload(start_ts):
    ts = [start_ts, start_ts + 300, start_ts + 600, start_ts + 900, start_ts + 1000]
    return {
        "chart": {
            "result": [
                {
                    "timestamp": ts,
                    "indicators": {
                        "quote": [
                            {
                                "open": [0.88, None, 0.8802, 0.8803, 0.8804],
                                "high": [0.8805, 0.8806, 0.8806, 0.8807, 0.8808],
                                "low": [0.8795, 0.8796, 0.8799, 0.8800, 0.8801],
                                "close": [0.8801, 0.8802, 0.8803, 0.8804, 0.8805],
                            }
                        ]
                    },
                }
            ],
            "error": None,
        }
    }


def test_yahoo_parses_and_returns_only_closed_aligned_bars():
    start = datetime(2026, 1, 6, 21, 0, tzinfo=UTC)
    session = FakeSession(yahoo_payload(int(start.timestamp())))
    feed = YahooFeed(5, session=session)
    now = start + timedelta(minutes=17)  # the 21:15 bar is still forming
    bars = feed.fetch_closed("usd/chf", 100, now)
    assert [b.time for b in bars] == [start, start + timedelta(minutes=10)]  # null row skipped
    assert bars[0].close == 0.8801
    url, params = session.calls[0]
    assert url.endswith("/USDCHF=X") and params["interval"] == "5m"


def test_yahoo_errors_become_feed_errors():
    with pytest.raises(FeedError):
        YahooFeed(5, session=FakeSession({}, status=500), sleep=lambda s: None).fetch_closed(
            "USDCHF", 10, datetime.now(UTC)
        )
    with pytest.raises(FeedError):
        YahooFeed.parse({"chart": {"error": {"code": "Not Found"}}}, 5)


def test_oanda_parses_complete_candles():
    payload = {
        "candles": [
            {"complete": True, "time": "2026-01-06T21:00:00.000000000Z", "mid": {"o": "170.1", "h": "170.3", "l": "170.0", "c": "170.2"}},
            {"complete": False, "time": "2026-01-06T21:05:00.000000000Z", "mid": {"o": "170.2", "h": "170.4", "l": "170.1", "c": "170.3"}},
        ]
    }
    session = FakeSession(payload)
    feed = OandaFeed(5, token="t0ken", session=session)
    bars = feed.fetch_closed("CHFJPY", 50, datetime(2026, 1, 6, 21, 7, tzinfo=UTC))
    assert len(bars) == 1 and bars[0].high == 170.3
    assert session.headers["Authorization"] == "Bearer t0ken"
    url, params = session.calls[0]
    assert url == "https://api-fxpractice.oanda.com/v3/instruments/CHF_JPY/candles"
    assert params["granularity"] == "M5"


def test_oanda_requires_token():
    with pytest.raises(FeedError):
        OandaFeed(5, token="")


@pytest.mark.parametrize(
    "content,tz",
    [
        ("time,open,high,low,close,Volume\n1767733200,1.1,1.2,1.0,1.15,0\n", "UTC"),  # TradingView export
        ("Time,Open,High,Low,Close\n2026-01-06T21:00:00Z,1.1,1.2,1.0,1.15\n", "UTC"),
        ("<DATE>;<TIME>;<OPEN>;<HIGH>;<LOW>;<CLOSE>\n2026.01.06;21:00;1.1;1.2;1.0;1.15\n", "UTC"),  # MT4/5
        ("Gmt time,Open,High,Low,Close,Volume\n06.01.2026 21:00:00.000,1.1,1.2,1.0,1.15,10\n", "UTC"),  # Dukascopy
        ("datetime,open,high,low,close\n2026-01-06 16:00:00,1.1,1.2,1.0,1.15\n", "America/New_York"),
    ],
)
def test_csv_formats(tmp_path, content, tz):
    p = tmp_path / "EURUSD_M5.csv"
    p.write_text(content)
    bars = load_csv(p, tz)
    assert bars == [Bar(datetime(2026, 1, 6, 21, 0, tzinfo=UTC), 1.1, 1.2, 1.0, 1.15)]


def test_csv_round_trip_and_lookup(tmp_path):
    bars = generate_synthetic(["GBPAUD"], days=2)["GBPAUD"]
    save_csv(tmp_path / "GBPAUD_M5.csv", bars)
    assert find_csv(tmp_path, "GBP/AUD").name == "GBPAUD_M5.csv"
    assert find_csv(tmp_path, "USDCHF") is None
    assert load_csv(tmp_path / "GBPAUD_M5.csv") == bars


def test_replay_feed_serves_closed_bars_only():
    bars = generate_synthetic(["USDCHF"], days=3)["USDCHF"]
    feed = ReplayFeed({"USDCHF": bars}, 5)
    now = bars[100].time + timedelta(minutes=7)  # bar 100 closed at +5, bar 101 still open
    got = feed.fetch_closed("USDCHF", 10, now)
    assert got == bars[91:101]
    assert feed.start_time(50) == bars[50].time
    assert feed.fetch_closed("EURUSD", 10, now) == []


def test_synthetic_data_skips_the_weekend():
    bars = generate_synthetic(["EURUSD"], days=8)["EURUSD"]
    assert not any(b.time.weekday() == 5 for b in bars)  # no Saturday bars
    gaps = [b.time - a.time for a, b in zip(bars, bars[1:])]
    assert max(gaps) >= timedelta(hours=40)


def test_csv_naive_times_across_dst_fall_back(tmp_path):
    # New York falls back on 1 Nov 2026: 01:00-01:55 local happens twice.
    wall = [f"2026-11-01 00:{m:02d}" for m in range(0, 60, 5)]
    wall += [f"2026-11-01 01:{m:02d}" for m in range(0, 60, 5)] * 2
    wall += [f"2026-11-01 02:{m:02d}" for m in range(0, 60, 5)]
    rows = "\n".join(f"{w},1,1,1,1" for w in wall)
    p = tmp_path / "EURUSD.csv"
    p.write_text("time,open,high,low,close\n" + rows + "\n")
    bars = load_csv(p, "America/New_York")
    assert len(bars) == 48
    assert bars[0].time == datetime(2026, 11, 1, 4, 0, tzinfo=UTC)  # 00:00 EDT
    steps = {b.time - a.time for a, b in zip(bars, bars[1:])}
    assert steps == {timedelta(minutes=5)}


def test_yahoo_waits_for_bar_to_settle():
    start = datetime(2026, 1, 6, 21, 0, tzinfo=UTC)
    feed = YahooFeed(5, session=FakeSession(yahoo_payload(int(start.timestamp()))), sleep=lambda s: None)
    just_closed = start + timedelta(minutes=15, seconds=3)  # 21:10 bar closed 3s ago
    assert [b.time for b in feed.fetch_closed("USDCHF", 10, just_closed)][-1] == start
    later = start + timedelta(minutes=15, seconds=15)
    assert [b.time for b in feed.fetch_closed("USDCHF", 10, later)][-1] == start + timedelta(minutes=10)


class FakeOanda:
    """Serves synthetic candles the way OANDA does: at most 5000, newest last, ``to`` exclusive."""

    def __init__(self, bars, now):
        self.bars, self.now, self.headers, self.calls = bars, now, {}, []

    def get(self, url, params=None, timeout=None):
        self.calls.append(dict(params))
        assert params["count"] <= 5000
        to = params.get("to")
        limit = datetime.fromisoformat(to.replace("Z", "+00:00")) if to else self.now
        rows = [b for b in self.bars if b.time < limit][-params["count"]:]
        candles = [
            {
                "complete": b.time + timedelta(minutes=5) <= self.now,
                "time": b.time.strftime("%Y-%m-%dT%H:%M:%S.000000000Z"),
                "mid": {"o": str(b.open), "h": str(b.high), "l": str(b.low), "c": str(b.close)},
            }
            for b in rows
        ]
        return FakeResponse({"candles": candles})


def test_oanda_pages_back_past_5000_candles():
    bars = generate_synthetic(["USDCHF"], days=60, seed=3)["USDCHF"]
    now = bars[-1].time + timedelta(minutes=2)  # last bar still forming
    session = FakeOanda(bars, now)
    got = OandaFeed(5, token="x", session=session).fetch_closed("USDCHF", 12_000, now)
    closed = [b for b in bars if b.time + timedelta(minutes=5) <= now]
    assert [b.time for b in got] == [b.time for b in closed[-12_000:]]
    assert len(session.calls) == 3
    assert "to" not in session.calls[0] and "to" in session.calls[1]


def test_oanda_stops_when_history_runs_out():
    bars = generate_synthetic(["USDCHF"], days=3, seed=3)["USDCHF"]
    now = bars[-1].time + timedelta(minutes=5)
    got = OandaFeed(5, token="x", session=FakeOanda(bars, now)).fetch_closed("USDCHF", 9_000, now)
    assert got == bars


class ScriptedSession:
    """Plays back a list of responses (or exceptions), recording each URL."""

    def __init__(self, script):
        self.script, self.urls, self.headers = list(script), [], {}

    def get(self, url, params=None, timeout=None):
        self.urls.append(url)
        item = self.script.pop(0)
        if isinstance(item, Exception):
            raise item
        return item


def test_yahoo_retries_429_on_the_other_host_then_succeeds():
    start = datetime(2026, 1, 6, 21, 0, tzinfo=UTC)
    ok = FakeResponse(yahoo_payload(int(start.timestamp())))
    session = ScriptedSession([FakeResponse({}, 429), ConnectionError("reset"), ok])
    waits = []
    feed = YahooFeed(5, session=session, sleep=waits.append, min_interval_seconds=0)
    bars = feed.fetch_closed("USDCHF", 10, start + timedelta(minutes=30))
    assert len(bars) == 3  # the null row and the unaligned live tick are skipped
    assert [u.split("/")[2] for u in session.urls] == [
        "query1.finance.yahoo.com", "query2.finance.yahoo.com", "query1.finance.yahoo.com",
    ]
    assert waits == [3.0, 6.0]  # exponential backoff between tries


def test_yahoo_gives_up_after_retries_and_does_not_retry_404():
    session = ScriptedSession([FakeResponse({}, 429)] * 4)
    feed = YahooFeed(5, session=session, sleep=lambda s: None)
    with pytest.raises(FeedError, match="after 4 tries: HTTP 429 Too Many Requests"):
        feed.fetch_closed("USDCHF", 10, datetime.now(UTC))
    session = ScriptedSession([FakeResponse({}, 404)])
    with pytest.raises(FeedError, match="HTTP 404"):
        YahooFeed(5, session=session, sleep=lambda s: None).fetch_closed("USDCHF", 10, datetime.now(UTC))
    assert len(session.urls) == 1


def test_browser_session_falls_back_to_requests_with_browser_headers(monkeypatch):
    import sys

    from scalper.feeds import browser_session

    monkeypatch.setitem(sys.modules, "curl_cffi", None)  # simulate "not installed"
    session = browser_session()
    assert session.headers["User-Agent"].startswith("Mozilla/5.0 (Windows NT 10.0")


# ---- Dukascopy ------------------------------------------------------------
import lzma  # noqa: E402
import struct  # noqa: E402

from scalper.feeds import DukascopyFeed, aggregate  # noqa: E402


def bi5(records):
    raw = b"".join(struct.pack(">5if", *r) for r in records)
    return lzma.compress(raw, format=lzma.FORMAT_ALONE)


def test_dukascopy_decodes_minutes_and_builds_five_minute_bars():
    # 09:00-09:09 on 6 Jan 2026; fields are open, close, low, high in points.
    records = []
    for i in range(10):
        o = 88000 + i
        records.append((9 * 3600 + 60 * i, o, o + 1, o - 2, o + 3, 1.5))
    records.append((9 * 3600 + 600, 1, 1, 1, 1, 0.0))  # no volume: market closed, dropped
    day = datetime(2026, 1, 6, tzinfo=UTC)
    minutes = DukascopyFeed.decode(bi5(records), day, 100_000, "USDCHF")
    assert len(minutes) == 10
    assert minutes[0] == Bar(day + timedelta(hours=9), 0.88, 0.88003, 0.87998, 0.88001)
    bars = aggregate(minutes, 5)
    assert [b.time.minute for b in bars] == [0, 5]
    assert bars[0].open == pytest.approx(0.88) and bars[0].close == pytest.approx(0.88005)
    assert bars[0].high == pytest.approx(0.88007) and bars[0].low == pytest.approx(0.87998)


def test_dukascopy_rejects_garbled_files():
    day = datetime(2026, 1, 6, tzinfo=UTC)
    with pytest.raises(FeedError, match="out of order"):
        DukascopyFeed.decode(bi5([(0, 100, 101, 105, 99, 1.0)]), day, 100_000)  # low above high
    with pytest.raises(FeedError, match="LZMA"):
        DukascopyFeed.decode(b"<html>blocked</html>", day, 100_000)


def test_dukascopy_fetch_walks_days_with_zero_based_months():
    now = datetime(2026, 2, 2, 12, 0, tzinfo=UTC)  # Monday
    candles = [(60 * i, 170000 + i, 170000 + i, 169990 + i, 170010 + i, 2.0) for i in range(600)]
    files = {"2026/00/30": bi5(candles), "2026/01/02": bi5(candles)}  # Friday 30 Jan, Monday 2 Feb

    class DaySession:
        headers = {}

        def __init__(self):
            self.urls = []

        def get(self, url, params=None, timeout=None):
            self.urls.append(url)
            key = "/".join(url.split("/")[-4:-1])
            return FakeResponse({}, 200, files[key]) if key in files else FakeResponse({}, 404)

    session = DaySession()
    progress = []
    feed = DukascopyFeed(5, session=session, sleep=lambda s: None, progress=lambda *a: progress.append(a))
    bars = feed.fetch_closed("CHFJPY", 5000, now)
    assert any("/CHFJPY/2026/00/30/BID_candles_min_1.bi5" in u for u in session.urls)
    assert not any("/2026/00/31/" in u for u in session.urls)  # Saturday skipped
    assert bars[0].time == datetime(2026, 1, 30, tzinfo=UTC)
    assert bars[0].open == pytest.approx(170.0)  # JPY pairs use 0.001 points
    assert bars[-1].time == datetime(2026, 2, 2, 9, 55, tzinfo=UTC)
    assert len(bars) == 240
    assert progress[-1] == ("CHFJPY", len(session.urls), len(session.urls), 0)


class FakeTime:
    """A clock that only moves when something sleeps, so backoff costs no real time."""

    def __init__(self):
        self.t = 0.0

    def now(self):
        return self.t

    def sleep(self, seconds):
        self.t += seconds


def day_candles():
    return bi5([(60 * i, 88000, 88001, 87990, 88010, 1.0) for i in range(60)])


class ThrottlingSession:
    """Answers 503 to the first ``throttle`` requests and to every request for ``bad_days``."""

    headers = {}

    def __init__(self, throttle=0, bad_days=()):
        self.throttle, self.bad_days, self.urls = throttle, set(bad_days), []

    def get(self, url, params=None, timeout=None):
        self.urls.append(url)
        if len(self.urls) <= self.throttle or any(f"/{d}/" in url for d in self.bad_days):
            return FakeResponse({}, 503)
        return FakeResponse({}, 200, day_candles())


def test_dukascopy_pauses_everyone_when_throttled_then_carries_on():
    clock = FakeTime()
    session = ThrottlingSession(throttle=3)
    feed = DukascopyFeed(5, session=session, workers=1, sleep=clock.sleep, clock=clock.now)
    bars = feed.fetch_closed("USDCHF", 1500, datetime(2026, 2, 6, 12, tzinfo=UTC))
    assert bars and feed.retry_count == 3
    assert clock.t >= 5 + 10 + 20  # backoff grew with each push-back


def test_dukascopy_keeps_going_when_a_day_keeps_failing(caplog):
    clock = FakeTime()
    session = ThrottlingSession(bad_days=["2026/01/03"])  # 3 Feb 2026 (months are 0-based)
    feed = DukascopyFeed(5, session=session, workers=2, retries=1, sleep=clock.sleep, clock=clock.now)
    with caplog.at_level("WARNING"):
        bars = feed.fetch_closed("USDCHF", 1500, datetime(2026, 2, 6, 12, tzinfo=UTC))
    assert bars
    assert not any(b.time.date().isoformat() == "2026-02-03" for b in bars)
    assert "1 day(s) could not be downloaded (2026-02-03)" in caplog.text


def test_dukascopy_gives_up_when_nothing_arrives():
    class Dead:
        headers = {}

        def get(self, url, params=None, timeout=None):
            raise TimeoutError("Operation timed out after 30000 milliseconds")

    clock = FakeTime()
    feed = DukascopyFeed(5, session=Dead(), workers=2, retries=1, sleep=clock.sleep, clock=clock.now)
    with pytest.raises(FeedError, match=r"stopped answering USDCHF \(timed out\).*run the same fetch again"):
        feed.fetch_closed("USDCHF", 300, datetime(2026, 2, 4, tzinfo=UTC))


def test_dukascopy_reuses_cached_days(tmp_path):
    now = datetime(2026, 2, 13, 12, tzinfo=UTC)
    first = ThrottlingSession()
    DukascopyFeed(5, session=first, cache_dir=tmp_path).fetch_closed("USDCHF", 3000, now)
    second = ThrottlingSession()
    bars = DukascopyFeed(5, session=second, cache_dir=tmp_path).fetch_closed("USDCHF", 3000, now)
    assert bars
    # Only the last two days (possibly not final yet) are asked for again.
    assert sorted(u.split("/")[-2] for u in second.urls) == ["12", "13"]
    assert len(first.urls) > len(second.urls)


def test_find_csv_accepts_tradingview_export_names(tmp_path):
    for name in ("FX_USDCHF, 5.csv", "OANDA_CHFJPY, 5.csv", "notes.txt", "EURUSDCHF.csv"):
        (tmp_path / name).write_text("time,open,high,low,close\n")
    assert find_csv(tmp_path, "USDCHF").name == "FX_USDCHF, 5.csv"
    assert find_csv(tmp_path, "CHFJPY").name == "OANDA_CHFJPY, 5.csv"
    assert find_csv(tmp_path, "AUDCAD") is None


def test_dukascopy_stops_quickly_when_the_server_blocks_us(tmp_path, caplog):
    """Many days in, the server starts refusing everything: give up in minutes, keep the cache."""
    now = datetime(2026, 3, 13, 12, tzinfo=UTC)

    class BlocksAfter:
        headers = {}

        def __init__(self, good):
            self.good, self.urls = good, []

        def get(self, url, params=None, timeout=None):
            self.urls.append(url)
            if len(self.urls) <= self.good:
                return FakeResponse({}, 200, day_candles())
            return FakeResponse({}, 503)

    clock = FakeTime()
    session = BlocksAfter(good=20)
    feed = DukascopyFeed(5, session=session, workers=2, cache_dir=tmp_path, sleep=clock.sleep, clock=clock.now)
    with caplog.at_level("WARNING"), pytest.raises(FeedError, match=r"20 day\(s\) of USDCHF are cached"):
        feed.fetch_closed("USDCHF", 20_000, now)  # ~70 weekdays wanted
    assert len(session.urls) <= 20 + feed.max_failures_in_a_row + 2
    assert clock.t < 15 * 60  # simulated minutes, not hours
    assert "pausing downloads" in caplog.text

    # Later the block has lifted: only the missing days are fetched.
    retry = ThrottlingSession()
    bars = DukascopyFeed(5, session=retry, cache_dir=tmp_path).fetch_closed("USDCHF", 20_000, now)
    fetched_before = set(session.urls[:20])
    assert bars and retry.urls
    assert fetched_before.isdisjoint(retry.urls)  # cached days are not downloaded again
    assert bars[0].time.date().isoformat() == "2026-01-01"  # and the full range is there


def test_dukascopy_block_keeps_pairs_missing_only_today_and_yesterday(tmp_path, caplog):
    now = datetime(2026, 3, 13, 12, tzinfo=UTC)
    # Fill the cache with a full download first...
    DukascopyFeed(5, session=ThrottlingSession(), cache_dir=tmp_path).fetch_closed("USDCHF", 3000, now)

    class AlwaysBusy:
        headers = {}

        def __init__(self):
            self.urls = []

        def get(self, url, params=None, timeout=None):
            self.urls.append(url)
            return FakeResponse({}, 503)

    # ...then the server blocks us: the uncached last two days fail, the rest is kept.
    clock = FakeTime()
    busy = AlwaysBusy()
    feed = DukascopyFeed(5, session=busy, cache_dir=tmp_path, sleep=clock.sleep, clock=clock.now)
    with caplog.at_level("WARNING"):
        bars = feed.fetch_closed("USDCHF", 3000, now)
    assert bars and bars[-1].time.date().isoformat() == "2026-03-11"
    assert {u.split("/")[-2] for u in busy.urls} == {"12", "13"}
    assert "the last 2 day(s) are missing" in caplog.text
