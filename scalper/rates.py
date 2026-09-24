"""Currency conversion into the account currency.

The Pine script converts the stop distance (in quote currency) to USD with
the quote currency's USD rate. The bot does the same with the latest close of
the matching USD pair, falling back to static rates from the config.
"""

from __future__ import annotations

import logging

from scalper.instruments import Instrument

log = logging.getLogger(__name__)

# Currencies that are conventionally quoted as XXXUSD (the rest as USDXXX).
_USD_QUOTED = ("EUR", "GBP", "AUD", "NZD")


def conversion_symbol(ccy: str, account: str = "USD") -> str:
    """The market-convention pair that prices ``ccy`` against ``account``."""
    return f"{ccy}{account}" if ccy in _USD_QUOTED else f"{account}{ccy}"


class RateBook:
    def __init__(self, account_currency: str = "USD", fallback: dict[str, float] | None = None) -> None:
        self.account = account_currency
        self.fallback = dict(fallback or {})
        self._prices: dict[str, float] = {}
        self._warned: set[str] = set()

    def update(self, symbol: str, price: float) -> None:
        self._prices[symbol] = price

    def value(self, ccy: str) -> float | None:
        """Account-currency value of one unit of ``ccy``."""
        if ccy == self.account:
            return 1.0
        direct = self._prices.get(ccy + self.account)
        if direct:
            return direct
        inverse = self._prices.get(self.account + ccy)
        if inverse:
            return 1.0 / inverse
        if ccy in self.fallback:
            if ccy not in self._warned:
                log.warning("no live %s/%s price yet; using fallback rate %s", ccy, self.account, self.fallback[ccy])
                self._warned.add(ccy)
            return self.fallback[ccy]
        return None

    def required_aux(self, symbols: list[str]) -> list[str]:
        """Extra pairs to follow so every quote currency can be converted."""
        traded = set(symbols)
        aux: list[str] = []
        for symbol in symbols:
            ccy = Instrument.from_symbol(symbol).quote
            if ccy == self.account:
                continue
            if ccy + self.account in traded or self.account + ccy in traded:
                continue
            pair = conversion_symbol(ccy, self.account)
            if pair not in aux:
                aux.append(pair)
        return aux
