"""Pine-style session windows such as ``"1600-1900"`` or ``"2200-0100:23456"``."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, time, timedelta
from zoneinfo import ZoneInfo


@dataclass(frozen=True)
class SessionWindow:
    start: time
    end: time
    tz: ZoneInfo
    days: frozenset[int]  # Pine day numbers: 1 = Sunday ... 7 = Saturday

    @classmethod
    def parse(cls, spec: str, tz_name: str) -> SessionWindow:
        spec = spec.strip()
        days = frozenset(range(1, 8))
        if ":" in spec:
            spec, day_part = spec.split(":", 1)
            if not day_part.isdigit() or not set(day_part) <= set("1234567"):
                raise ValueError(f"bad session days: {day_part!r}")
            days = frozenset(int(c) for c in day_part)
        try:
            a, b = spec.split("-")
            start = time(int(a[:2]), int(a[2:]))
            end = time(int(b[:2]), int(b[2:]))
        except (ValueError, IndexError) as exc:
            raise ValueError(f"bad session {spec!r}; expected HHMM-HHMM") from exc
        if start == end:
            raise ValueError("session start and end must differ")
        return cls(start=start, end=end, tz=ZoneInfo(tz_name), days=days)

    def contains(self, moment: datetime) -> bool:
        """True when a bar opening at ``moment`` belongs to the session."""
        local = moment.astimezone(self.tz)
        t = local.time().replace(tzinfo=None)
        if self.start < self.end:
            inside = self.start <= t < self.end
            session_day = local
        else:  # overnight session, e.g. 2200-0100
            inside = t >= self.start or t < self.end
            session_day = local if t >= self.start else local - timedelta(days=1)
        if not inside:
            return False
        return _pine_day(session_day) in self.days


def _pine_day(moment: datetime) -> int:
    # Python: Monday=0 .. Sunday=6. Pine: Sunday=1 .. Saturday=7.
    return (moment.weekday() + 1) % 7 + 1
