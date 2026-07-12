from __future__ import annotations

import json
import os
import random
import re
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import requests

from ..cache_policy import market_aware_ttl
from ..history import period_to_timedelta
from .base import BaseMarketProvider, ProviderError
from .rate_limiter import rate_limit


_ALLOWED_SYMBOL = re.compile(r"^[A-Z0-9.^\-]{1,32}$")
_INTRADAY_RESOLUTIONS = {
    "1m": "1",
    "5m": "5",
    "15m": "15",
    "30m": "30",
    "60m": "60",
}


class FinnhubProvider(BaseMarketProvider):
    name = "finnhub"

    def __init__(self, config: dict[str, Any]) -> None:
        self.config = config or {}
        cache_cfg = self.config.get("cache", {})
        finnhub_cfg = self.config.get("finnhub", {})
        data_cfg = self.config.get("data", {})
        self.cache_enabled = bool(data_cfg.get("cache_enabled", True))
        self.cache_dir = Path(os.getenv("CACHE_DIR", "/app/data/cache"))
        self.cache_dir.mkdir(parents=True, exist_ok=True)
        self.base_url = str(
            os.getenv("FINNHUB_BASE_URL", finnhub_cfg.get("base_url", "https://finnhub.io/api/v1"))
            or "https://finnhub.io/api/v1"
        ).rstrip("/")
        self.api_key = os.getenv("FINNHUB_API_KEY", "").strip()
        self.timeout_seconds = int(finnhub_cfg.get("timeout_seconds", 20) or 20)
        self.quotes_ttl = int(cache_cfg.get("quotes_ttl_seconds", 10) or 10)
        self.candles_ttl = int(cache_cfg.get("candles_ttl_seconds", 60) or 60)

    def _norm_symbol(self, symbol: str) -> str:
        normalized = str(symbol or "").strip().upper()
        if not normalized or not _ALLOWED_SYMBOL.fullmatch(normalized):
            raise ProviderError(f"Invalid Finnhub symbol: {symbol!r}", provider=self.name)
        return normalized

    def _safe_name(self, *parts: Any) -> str:
        return "_".join(str(part) for part in parts).replace("/", "_").replace(" ", "_").replace(":", "_")

    def _cache_path(self, key: str) -> Path:
        return self.cache_dir / f"finnhub_{key}.json"

    def _fresh(self, path: Path, ttl: int) -> bool:
        effective_ttl = market_aware_ttl(ttl)
        return path.exists() and (time.time() - path.stat().st_mtime) <= effective_ttl

    def _read_json(self, path: Path) -> dict[str, Any] | None:
        if not path.exists():
            return None
        try:
            return json.loads(path.read_text())
        except Exception:
            return None

    def _write_json(self, path: Path, payload: dict[str, Any]) -> None:
        if not self.cache_enabled:
            return
        try:
            path.write_text(json.dumps(payload))
        except Exception:
            return

    def _require_key(self) -> None:
        if not self.api_key:
            raise ProviderError("Finnhub API key is missing (set FINNHUB_API_KEY)", provider=self.name)

    @staticmethod
    def _to_float(value: Any, fallback: float = 0.0) -> float:
        try:
            if value is None or value == "":
                return fallback
            return float(str(value).replace(",", ""))
        except Exception:
            return fallback

    @staticmethod
    def _to_int(value: Any, fallback: int = 0) -> int:
        try:
            if value is None or value == "":
                return fallback
            return int(float(str(value).replace(",", "")))
        except Exception:
            return fallback

    @staticmethod
    def _parse_epoch(value: Any) -> str | None:
        try:
            ts = int(float(value))
        except Exception:
            return None
        return datetime.fromtimestamp(ts, timezone.utc).isoformat()

    def _api_get(self, endpoint: str, params: dict[str, Any] | None = None) -> dict[str, Any]:
        self._require_key()
        merged = {**(params or {}), "token": self.api_key}
        last_error: Exception | None = None
        rate_cfg = self.config.get("rate_limits", {}).get(self.name, {}) or {}
        max_retries = int(rate_cfg.get("max_retries", 0) or 0)
        initial_backoff = float(rate_cfg.get("backoff_initial_seconds", 10) or 10)
        max_backoff = float(rate_cfg.get("backoff_max_seconds", 300) or 300)

        for attempt in range(max_retries + 1):
            try:
                rate_limit(self.name)
                response = requests.get(f"{self.base_url}{endpoint}", params=merged, timeout=self.timeout_seconds)
                if response.status_code == 429:
                    raise ProviderError("Finnhub rate limited (HTTP 429)", rate_limited=True, provider=self.name)
                if response.status_code >= 500:
                    raise ProviderError(f"Finnhub HTTP {response.status_code}", provider=self.name)
                if response.status_code >= 400:
                    text = (response.text or "").strip().replace("\n", " ")
                    raise ProviderError(f"Finnhub HTTP {response.status_code}: {text[:220]}", provider=self.name)

                payload = response.json()
                if isinstance(payload, dict):
                    error_text = payload.get("error") or payload.get("message")
                    if error_text:
                        raise ProviderError(str(error_text), provider=self.name)
                    if payload.get("errorCode") or payload.get("status") == "error":
                        raise ProviderError(str(payload.get("error") or payload.get("message") or "Finnhub error"), provider=self.name)
                    return payload
                raise ProviderError("Finnhub returned an unexpected payload", provider=self.name)
            except ProviderError as exc:
                last_error = exc
                if getattr(exc, "rate_limited", False):
                    raise
            except (requests.Timeout, requests.ConnectionError) as exc:
                last_error = exc
            except Exception as exc:
                last_error = exc

            if attempt >= max_retries:
                break

            wait = min(max_backoff, initial_backoff * (2 ** attempt))
            wait += random.uniform(0, min(5.0, wait * 0.25))
            time.sleep(wait)

        raise ProviderError(f"Finnhub request failed after retries: {last_error}", provider=self.name) from last_error

    def get_quote(self, symbol: str) -> dict[str, Any]:
        sym = self._norm_symbol(symbol)
        cache_path = self._cache_path(self._safe_name("quote", sym))
        cached = self._read_json(cache_path)
        if self.cache_enabled and cached and self._fresh(cache_path, self.quotes_ttl):
            return cached

        try:
            payload = self._api_get("/quote", {"symbol": sym})
            quote = {
                "symbol": sym,
                "price": self._to_float(payload.get("c")),
                "open": self._to_float(payload.get("o")),
                "high": self._to_float(payload.get("h")),
                "low": self._to_float(payload.get("l")),
                "previous_close": self._to_float(payload.get("pc")),
                "timestamp": self._parse_epoch(payload.get("t")) or datetime.now(timezone.utc).isoformat(),
                "quote_type": "REALTIME",
                "provider": self.name,
                "source": self.name,
            }
            self._write_json(cache_path, quote)
            return quote
        except Exception as exc:
            if cached:
                return cached
            if isinstance(exc, ProviderError):
                raise
            raise ProviderError(f"Finnhub quote error: {exc}", provider=self.name) from exc

    def get_earnings_history(self, symbol: str, *, force_refresh: bool = False) -> list[dict[str, Any]]:
        sym = self._norm_symbol(symbol)
        cfg = self.config.get("earnings_history", {}) or {}
        ttl = int(cfg.get("cache_ttl_seconds", 86400) or 86400)
        cache_path = self._cache_path(self._safe_name("earnings", sym))
        cached = self._read_json(cache_path)
        if cached and not force_refresh and self._fresh(cache_path, ttl):
            return list(cached.get("earnings") or [])

        payload = self._api_get("/stock/earnings", {"symbol": sym, "limit": 20})
        rows = payload if isinstance(payload, list) else payload.get("earnings") or []
        if not isinstance(rows, list):
            raise ProviderError("Finnhub earnings response was malformed", provider=self.name)
        normalized: list[dict[str, Any]] = []
        for row in rows:
            if not isinstance(row, dict):
                continue
            reported_date = str(row.get("period") or row.get("reportedDate") or "").strip() or None
            normalized.append(
                {
                    "fiscal_date_ending": str(row.get("fiscalDateEnding") or row.get("period") or "").strip() or None,
                    "reported_date": reported_date,
                    "reported_eps": row.get("actual"),
                    "estimated_eps": row.get("estimate"),
                    "surprise": row.get("surprise"),
                    "surprise_percentage": row.get("surprisePercent"),
                    "reported_revenue": None,
                    "estimated_revenue": None,
                    "report_time": None,
                    "provider": self.name,
                }
            )
        if not normalized:
            raise ProviderError(f"Finnhub returned no earnings for {sym}", provider=self.name)
        self._write_json(
            cache_path,
            {
                "symbol": sym,
                "provider": self.name,
                "retrieved_at": datetime.now(timezone.utc).isoformat(),
                "earnings": normalized,
            },
        )
        return normalized

    def _resolution_for_interval(self, interval: str) -> str:
        normalized = str(interval or "5m").strip().lower()
        if normalized == "1d":
            return "D"
        if normalized in _INTRADAY_RESOLUTIONS:
            return _INTRADAY_RESOLUTIONS[normalized]
        raise ProviderError(f"Unsupported Finnhub interval: {interval}", provider=self.name)

    @staticmethod
    def _normalize_rows(payload: dict[str, Any]) -> list[dict[str, Any]]:
        if payload.get("s") not in {"ok", "OK"}:
            return []
        times = payload.get("t") or []
        opens = payload.get("o") or []
        highs = payload.get("h") or []
        lows = payload.get("l") or []
        closes = payload.get("c") or []
        volumes = payload.get("v") or []
        rows: list[dict[str, Any]] = []
        for idx, ts in enumerate(times):
            rows.append(
                {
                    "time": int(ts),
                    "open": float(opens[idx]) if idx < len(opens) else 0.0,
                    "high": float(highs[idx]) if idx < len(highs) else 0.0,
                    "low": float(lows[idx]) if idx < len(lows) else 0.0,
                    "close": float(closes[idx]) if idx < len(closes) else 0.0,
                    "volume": float(volumes[idx]) if idx < len(volumes) else 0.0,
                    "adjustedClose": float(closes[idx]) if idx < len(closes) else 0.0,
                    "dividend": 0.0,
                    "splitCoefficient": 1.0,
                    "provider": "finnhub",
                    "source": "finnhub",
                }
            )
        return rows

    def get_candles_range(
        self,
        symbol: str,
        interval: str,
        start_timestamp: str,
        end_timestamp: str,
    ) -> list[dict[str, Any]]:
        sym = self._norm_symbol(symbol)
        resolution = self._resolution_for_interval(interval)
        start = datetime.fromisoformat(str(start_timestamp).replace("Z", "+00:00"))
        end = datetime.fromisoformat(str(end_timestamp).replace("Z", "+00:00"))
        if start.tzinfo is None:
            start = start.replace(tzinfo=timezone.utc)
        if end.tzinfo is None:
            end = end.replace(tzinfo=timezone.utc)

        payload = self._api_get(
            "/stock/candle",
            {
                "symbol": sym,
                "resolution": resolution,
                "from": int(start.astimezone(timezone.utc).timestamp()),
                "to": int(end.astimezone(timezone.utc).timestamp()),
            },
        )
        rows = self._normalize_rows(payload)
        if not rows:
            if payload.get("s") in {"no_data", "no_data"}:
                raise ProviderError(f"No Finnhub candle data returned for {sym} {interval}", provider=self.name)
            raise ProviderError(f"Finnhub returned no candle data for {sym} {interval}", provider=self.name)
        return rows

    def get_candles(self, symbol: str, interval: str, period: str) -> list[dict[str, Any]]:
        sym = self._norm_symbol(symbol)
        period_delta = period_to_timedelta(period)
        end = datetime.now(timezone.utc)
        start = end - period_delta
        cache_path = self._cache_path(self._safe_name("candles", sym, interval, period))
        cached = self._read_json(cache_path)
        if self.cache_enabled and cached and self._fresh(cache_path, self.candles_ttl):
            rows = list(cached.get("candles", []))
            if rows:
                return rows
        try:
            rows = self.get_candles_range(sym, interval, start.isoformat(), end.isoformat())
            payload = {"symbol": sym, "interval": interval, "period": period, "candles": rows, "source": self.name, "timestamp": datetime.now(timezone.utc).isoformat()}
            self._write_json(cache_path, payload)
            return rows
        except Exception as exc:
            if cached:
                return list(cached.get("candles", []))
            if isinstance(exc, ProviderError):
                raise
            raise ProviderError(f"Finnhub candle error: {exc}", provider=self.name) from exc

    def get_option_expirations(self, symbol: str) -> list[dict[str, Any]]:
        raise ProviderError("Finnhub options expirations are not implemented in this app", provider=self.name)

    def get_option_chain(self, symbol: str, expiration: dict[str, Any] | str, **kwargs) -> dict[str, Any]:
        raise ProviderError("Finnhub option chains are not implemented in this app", provider=self.name)

    def get_options_ratios(self, symbol: str, expirations_to_check: int = 3) -> dict[str, Any]:
        raise ProviderError("Finnhub options ratios are not implemented in this app", provider=self.name)

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
        raise ProviderError("Finnhub ranked option contracts are not implemented in this app", provider=self.name)
