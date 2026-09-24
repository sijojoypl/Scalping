"""Paper account persistence: a JSON state file plus a CSV trade journal."""

from __future__ import annotations

import csv
import json
import os
from dataclasses import fields
from pathlib import Path
from typing import Any

from scalper.models import Trade, from_dict, to_dict

TRADE_FIELDS = [f.name for f in fields(Trade)]


class StateStore:
    def __init__(self, path: str | Path) -> None:
        self.path = Path(path)

    def load(self) -> dict[str, Any] | None:
        if not self.path.exists():
            return None
        with self.path.open("r", encoding="utf-8") as fh:
            return json.load(fh)

    def save(self, state: dict[str, Any]) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_suffix(self.path.suffix + ".tmp")
        with tmp.open("w", encoding="utf-8") as fh:
            json.dump(state, fh, indent=2, sort_keys=True)
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(tmp, self.path)  # atomic, so a crash never leaves half a file


class TradeJournal:
    def __init__(self, path: str | Path) -> None:
        self.path = Path(path)

    def append(self, trade: Trade) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        new_file = not self.path.exists() or self.path.stat().st_size == 0
        with self.path.open("a", encoding="utf-8", newline="") as fh:
            writer = csv.DictWriter(fh, fieldnames=TRADE_FIELDS)
            if new_file:
                writer.writeheader()
            writer.writerow(to_dict(trade))

    def read(self) -> list[Trade]:
        return read_trades(self.path)


def read_trades(path: str | Path) -> list[Trade]:
    path = Path(path)
    if not path.exists():
        return []
    trades = []
    with path.open("r", encoding="utf-8", newline="") as fh:
        for row in csv.DictReader(fh):
            data: dict[str, Any] = dict(row)
            for key in ("id", "qty", "bars_held"):
                data[key] = int(data[key])
            for key in ("entry_price", "exit_price", "stop_loss", "take_profit", "pips", "pnl_quote", "commission", "pnl"):
                data[key] = float(data[key])
            trades.append(from_dict(Trade, data))
    return trades


def write_trades(path: str | Path, trades: list[Trade]) -> None:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8", newline="") as fh:
        writer = csv.DictWriter(fh, fieldnames=TRADE_FIELDS)
        writer.writeheader()
        for t in trades:
            writer.writerow(to_dict(t))
