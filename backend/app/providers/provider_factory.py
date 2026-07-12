from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from ..config import config_manager
from .base import BaseMarketProvider, ProviderError
from .alphavantage_provider import AlphaVantageProvider
from .etrade_provider import ETradeProvider
from .finnhub_provider import FinnhubProvider
from .rate_limiter import call_with_rate_limit, detect_rate_limit_error
from .stooq_provider import StooqProvider
from .twelvedata_provider import TwelveDataProvider
from .yahoo_provider import YahooProvider


@dataclass
class ProviderSelection:
    primary: BaseMarketProvider
    fallback: BaseMarketProvider | None


class ProviderFactory:
    def __init__(self) -> None:
        self._instances: dict[str, BaseMarketProvider] = {}

    def _cfg(self) -> dict[str, Any]:
        return config_manager.config

    def _provider_name(self, key: str, default: str) -> str:
        return str(self._cfg().get("data", {}).get(key, default)).strip().lower()

    def _build(self, name: str) -> BaseMarketProvider:
        cfg = self._cfg()
        if name == "etrade":
            return ETradeProvider(cfg)
        if name == "alphavantage":
            return AlphaVantageProvider(cfg)
        if name == "finnhub":
            return FinnhubProvider(cfg)
        if name == "stooq":
            return StooqProvider(cfg)
        if name == "twelvedata":
            return TwelveDataProvider(cfg)
        return YahooProvider(cfg)

    def get_provider(self, name: str) -> BaseMarketProvider:
        normalized = (name or "yahoo").strip().lower()
        if normalized not in self._instances:
            self._instances[normalized] = self._build(normalized)
        return self._instances[normalized]

    def get_selection(self, provider_key: str, default: str) -> ProviderSelection:
        primary_name = self._provider_name(provider_key, default)
        specific_fallback_key = f"{provider_key}_fallback"
        fallback_name = self._provider_name(
            specific_fallback_key,
            self._provider_name("fallback_provider", "yahoo"),
        )
        if fallback_name in {"none", "disabled", "off", ""}:
            fallback_name = ""

        primary = self.get_provider(primary_name)
        fallback = None
        if fallback_name and fallback_name != primary_name:
            fallback = self.get_provider(fallback_name)
        return ProviderSelection(primary=primary, fallback=fallback)

    def with_fallback(self, selection: ProviderSelection, action_name: str, *args, **kwargs):
        symbol = str(args[0]).strip().upper() if args else None
        try:
            fn = getattr(selection.primary, action_name)
            data = call_with_rate_limit(selection.primary.name, symbol, action_name, fn, *args, **kwargs)
            return data, selection.primary.name, None
        except Exception as primary_exc:
            if detect_rate_limit_error(primary_exc):
                raise primary_exc
            if selection.fallback is None:
                raise primary_exc
            try:
                fn = getattr(selection.fallback, action_name)
                data = call_with_rate_limit(selection.fallback.name, symbol, action_name, fn, *args, **kwargs)
                warning = f"Primary provider {selection.primary.name} failed: {primary_exc}"
                if isinstance(data, dict):
                    data.setdefault("warning", warning)
                    warnings = data.get("warnings", [])
                    if isinstance(warnings, list):
                        warnings.append(warning)
                        data["warnings"] = warnings
                    data["fallback_used"] = True
                    data["provider"] = selection.fallback.name
                    data["source"] = selection.fallback.name
                return data, selection.fallback.name, warning
            except Exception as fallback_exc:
                raise ProviderError(
                    f"All providers failed ({selection.primary.name}, {selection.fallback.name}): {primary_exc} | {fallback_exc}",
                    provider=selection.primary.name,
                ) from fallback_exc

    def get_quotes_provider(self) -> ProviderSelection:
        return self.get_selection("quotes_provider", "yahoo")

    def get_options_provider(self) -> ProviderSelection:
        return self.get_selection("options_provider", "yahoo")

    def get_candles_provider(self) -> ProviderSelection:
        return self.get_selection("candles_provider", "yahoo")

    def get_historical_candles_provider(self) -> ProviderSelection:
        return self.get_selection("historical_candles_provider", "yahoo")


provider_factory = ProviderFactory()
