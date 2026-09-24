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
    def __init__(self, payload, status=200):
        self.payload = payload
        self.status_code = status

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
        YahooFeed(5, session=FakeSession({}, status=500)).fetch_closed("USDCHF", 10, datetime.now(UTC))
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
    feed = YahooFeed(5, session=FakeSession(yahoo_payload(int(start.timestamp()))))
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
