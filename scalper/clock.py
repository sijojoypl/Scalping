"""Wall clock for live paper trading and a simulated clock for replays."""

from __future__ import annotations

import time
from datetime import datetime, timedelta, timezone


class RealClock:
    simulated = False

    def now(self) -> datetime:
        return datetime.now(timezone.utc)

    def sleep(self, seconds: float) -> None:
        if seconds > 0:
            time.sleep(seconds)


class SimClock:
    """Time only moves when ``sleep`` is called, so replays run instantly."""

    simulated = True

    def __init__(self, start: datetime) -> None:
        self._now = start

    def now(self) -> datetime:
        return self._now

    def sleep(self, seconds: float) -> None:
        if seconds > 0:
            self._now += timedelta(seconds=seconds)
