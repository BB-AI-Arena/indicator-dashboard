from __future__ import annotations

from typing import Any

from datetime import datetime, timezone

import pandas as pd

from .config import config_manager
from .cache_policy import market_aware_ttl
from .db import SessionLocal
from .history import get_candles_from_sql, interval_seconds, period_to_timedelta, range_coverage_complete, upsert_candles
from .providers import provider_factory
from .providers.base import ProviderError


class DataProviderError(Exception):
    pass


def normalize_symbol(symbol: str) -> str:
    return symbol.strip().upper()


def candles_to_dataframe(candles: list[dict[str, Any]]) -> pd.DataFrame:
    if not candles:
        return pd.DataFrame(columns=["open", "high", "low", "close", "volume"])
    df = pd.DataFrame(candles)
    if "time" in df.columns:
        df["time"] = pd.to_datetime(df["time"], unit="s", utc=True)
        df = df.set_index("time")
    for col in ["open", "high", "low", "close", "volume"]:
        if col not in df.columns:
            df[col] = 0
    return df[["open", "high", "low", "close", "volume"]]


def _stored_candles_usable(symbol: str, df: pd.DataFrame, interval: str, period: str, *, prefer_stored: bool, historical: bool) -> bool:
    if df.empty:
        return False
    if prefer_stored and not historical:
        return True

    if historical:
        session = SessionLocal()
        try:
            end = datetime.now(timezone.utc)
            start = end - period_to_timedelta(period or "5d")
            return range_coverage_complete(symbol, interval, start, end, session)
        finally:
            session.close()

    latest = df.index.max()
    if latest is None:
        return False
    latest_ts = pd.Timestamp(latest)
    if latest_ts.tzinfo is None:
        latest_ts = latest_ts.tz_localize("UTC")
    age_seconds = (pd.Timestamp.now(tz="UTC") - latest_ts).total_seconds()
    ttl = market_aware_ttl(int(config_manager.get("cache", "candles_ttl_seconds", default=60) or 60))
    freshness_window = max(ttl, interval_seconds(interval) * 2)
    return age_seconds <= freshness_window


def fetch_candles(
    symbol: str,
    interval: str = "5m",
    period: str = "5d",
    *,
    refresh: bool = False,
    prefer_stored: bool = False,
    historical: bool = False,
) -> pd.DataFrame:
    normalized = normalize_symbol(symbol)
    stored = get_candles_from_sql(normalized, interval, period=period)
    if not refresh and _stored_candles_usable(normalized, stored, interval, period, prefer_stored=prefer_stored, historical=historical):
        return stored

    selection = provider_factory.get_historical_candles_provider() if historical else provider_factory.get_candles_provider()

    try:
        candles, provider_name, _ = provider_factory.with_fallback(
            selection,
            "get_candles",
            normalized,
            interval,
            period,
        )
        if isinstance(candles, list):
            upsert_candles(normalized, interval, candles, provider_name)
        df = candles_to_dataframe(candles)
        now = datetime.now(timezone.utc).isoformat()
        df.attrs.update(
            {
                "provider": provider_name,
                "source": provider_name,
                "timestamp": now,
                "last_updated": now,
            }
        )
        if df.empty:
            if not stored.empty:
                return stored
            raise DataProviderError(f"No candle data returned for {normalized}")
        return df
    except ProviderError as exc:
        if not stored.empty:
            return stored
        raise DataProviderError(str(exc)) from exc
    except Exception as exc:
        if not stored.empty:
            return stored
        raise DataProviderError(f"Failed to fetch candles for {normalized}: {exc}") from exc


def fetch_quote(symbol: str) -> dict[str, Any]:
    normalized = normalize_symbol(symbol)
    selection = provider_factory.get_quotes_provider()
    try:
        quote, provider_name, warning = provider_factory.with_fallback(selection, "get_quote", normalized)
        quote["provider"] = provider_name
        quote["source"] = provider_name
        if warning:
            quote["warning"] = warning
        return quote
    except ProviderError as exc:
        raise DataProviderError(str(exc)) from exc
