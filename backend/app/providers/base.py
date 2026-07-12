from __future__ import annotations

from abc import ABC, abstractmethod
from typing import Any


class ProviderError(Exception):
    def __init__(self, message: str, *, rate_limited: bool = False, provider: str | None = None):
        super().__init__(message)
        self.rate_limited = rate_limited
        self.provider = provider


class BaseMarketProvider(ABC):
    name = "base"

    @abstractmethod
    def get_quote(self, symbol: str) -> dict[str, Any]:
        raise NotImplementedError

    @abstractmethod
    def get_candles(self, symbol: str, interval: str, period: str) -> list[dict[str, Any]]:
        raise NotImplementedError

    def get_candles_range(
        self,
        symbol: str,
        interval: str,
        start_timestamp: str,
        end_timestamp: str,
    ) -> list[dict[str, Any]]:
        raise ProviderError(f"{self.name} does not support ranged candle backfills", provider=self.name)

    def get_earnings_history(self, symbol: str, *, force_refresh: bool = False) -> list[dict[str, Any]]:
        raise ProviderError(f"{self.name} does not support earnings history", provider=self.name)

    @abstractmethod
    def get_option_expirations(self, symbol: str) -> list[dict[str, Any]]:
        raise NotImplementedError

    @abstractmethod
    def get_option_chain(self, symbol: str, expiration: dict[str, Any] | str, **kwargs) -> dict[str, Any]:
        raise NotImplementedError

    @abstractmethod
    def get_options_ratios(self, symbol: str, expirations_to_check: int = 3) -> dict[str, Any]:
        raise NotImplementedError

    @abstractmethod
    def get_ranked_contracts(
        self,
        symbol: str,
        expirations_to_check: int = 3,
        min_volume: int = 1,
        max_spread_pct: float = 15,
        min_open_interest: int = 1,
        chart_signal: dict[str, Any] | None = None,
        options_sentiment: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        raise NotImplementedError
