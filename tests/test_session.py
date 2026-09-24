from datetime import datetime, timezone

import pytest

from scalper.session import SessionWindow

UTC = timezone.utc
NY = SessionWindow.parse("1600-1900", "America/New_York")


@pytest.mark.parametrize(
    "moment,inside",
    [
        # January (EST, UTC-5): 16:00-19:00 NY = 21:00-00:00 UTC
        (datetime(2026, 1, 6, 20, 55, tzinfo=UTC), False),
        (datetime(2026, 1, 6, 21, 0, tzinfo=UTC), True),
        (datetime(2026, 1, 6, 23, 55, tzinfo=UTC), True),
        (datetime(2026, 1, 7, 0, 0, tzinfo=UTC), False),
        # July (EDT, UTC-4): 16:00-19:00 NY = 20:00-23:00 UTC
        (datetime(2026, 7, 7, 20, 0, tzinfo=UTC), True),
        (datetime(2026, 7, 7, 22, 55, tzinfo=UTC), True),
        (datetime(2026, 7, 7, 23, 0, tzinfo=UTC), False),
    ],
)
def test_new_york_session_follows_dst(moment, inside):
    assert NY.contains(moment) is inside


def test_overnight_session_and_days():
    s = SessionWindow.parse("2200-0100:23456", "UTC")  # Monday..Friday sessions
    assert s.contains(datetime(2026, 1, 5, 22, 0, tzinfo=UTC))  # Monday 22:00
    assert s.contains(datetime(2026, 1, 6, 0, 30, tzinfo=UTC))  # belongs to Monday's session
    assert not s.contains(datetime(2026, 1, 6, 1, 0, tzinfo=UTC))
    assert not s.contains(datetime(2026, 1, 4, 22, 0, tzinfo=UTC))  # Sunday
    assert s.contains(datetime(2026, 1, 10, 0, 30, tzinfo=UTC))  # Friday's session, after midnight


@pytest.mark.parametrize("bad", ["16001900", "1600-1600", "16:00-19:00", "1600-1900:8"])
def test_bad_sessions_rejected(bad):
    with pytest.raises(ValueError):
        SessionWindow.parse(bad, "UTC")


def test_default_days_follow_pine_v4_weekdays():
    # 17:00 New York on Sunday 4 Jan 2026 (the weekly open) is 22:00 UTC.
    assert not NY.contains(datetime(2026, 1, 4, 22, 0, tzinfo=UTC))
    assert NY.contains(datetime(2026, 1, 5, 22, 0, tzinfo=UTC))  # Monday
    assert NY.contains(datetime(2026, 1, 9, 21, 0, tzinfo=UTC))  # Friday 16:00 NY
    every_day = SessionWindow.parse("1600-1900:1234567", "America/New_York")
    assert every_day.contains(datetime(2026, 1, 4, 22, 0, tzinfo=UTC))
