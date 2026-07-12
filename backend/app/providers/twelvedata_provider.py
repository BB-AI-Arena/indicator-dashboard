from __future__ import annotations

import json
import os
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import pandas as pd
import requests

from ..cache_policy import market_aware_ttl
from .base import BaseMarketProvider, ProviderError
from .rate_limiter import rate_limit


class TwelveDataProvider(BaseMarketProvider):
    name = "twelvedata"

    def __init__(self, config: dict[str, Any]) -> None:
        self.config = config or {}
        cache_cfg = self.config.get("cache", {})
        data_cfg = self.config.get("data", {})
        self.cache_enabled = bool(data_cfg.get("cache_enabled", True))
        self.cache_dir = Path(os.getenv("CACHE_DIR", "/app/data/cache"))
        self.cache_dir.mkdir(parents=True, exist_ok=True)
        self.base_url = os.getenv("TWELVEDATA_BASE_URL", "https://api.twelvedata.com").rstrip("/")
        self.api_key = os.getenv("TWELVEDATA_API_KEY", "").strip()
        self.candles_ttl = int(cache_cfg.get("candles_ttl_seconds", 60))
        self.quotes_ttl = int(cache_cfg.get("quotes_ttl_seconds", 10))

    def _cache_path(self, key: str) -> Path:
        safe = key.replace("/", "_").replace(" ", "_")
        return self.cache_dir / f"twelvedata_{safe}.json"

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

    @staticmethod
    def _norm_symbol(symbol: str) -> str:
        return symbol.strip().upper()

    def _require_key(self) -> None:
        if not self.api_key:
            raise ProviderError("TwelveData API key is missing (set TWELVEDATA_API_KEY)", provider=self.name)

    def _request(self, path: str, params: dict[str, Any]) -> dict[str, Any]:
        self._require_key()
        url = f"{self.base_url}/{path.lstrip('/')}"
        merged = {**params, "apikey": self.api_key}
        try:
            rate_limit(self.name)
            resp = requests.get(url, params=merged, timeout=20)
        except Exception as exc:
            raise ProviderError(f"TwelveData request failed: {exc}", provider=self.name) from exc

        text = resp.text or ""
        if resp.status_code == 429:
            raise ProviderError("TwelveData rate limited (HTTP 429)", rate_limited=True, provider=self.name)
        if resp.status_code >= 400:
            raise ProviderError(f"TwelveData HTTP {resp.status_code}: {text[:220]}", provider=self.name)

        try:
            payload = resp.json()
        except Exception as exc:
            raise ProviderError(f"TwelveData invalid JSON response: {exc}", provider=self.name) from exc

        if isinstance(payload, dict) and payload.get("status") == "error":
            msg = str(payload.get("message") or "Unknown TwelveData error")
            lower = msg.lower()
            rate_limited = "limit" in lower or "quota" in lower or "429" in lower
            raise ProviderError(f"TwelveData error: {msg}", rate_limited=rate_limited, provider=self.name)
        return payload

    @staticmethod
    def _interval_to_twelve(interval: str) -> str:
        i = (interval or "5m").strip().lower()
        mapping = {
            "1m": "1min",
            "5m": "5min",
            "15m": "15min",
            "30m": "30min",
            "45m": "45min",
            "1h": "1h",
            "2h": "2h",
            "4h": "4h",
            "1d": "1day",
            "1wk": "1week",
            "1mo": "1month",
        }
        return mapping.get(i, "5min")

    @staticmethod
    def _period_to_outputsize(period: str, interval: str) -> int:
        p = (period or "5d").strip().lower()
        multipliers = {"d": 1, "wk": 7, "mo": 30, "y": 365}
        days = 5
        for suffix, mult in multipliers.items():
            if p.endswith(suffix):
                try:
                    days = max(1, int(p[: -len(suffix)]) * mult)
                except Exception:
                    days = 5
                break

        per_day = 78
        if interval in {"1min"}:
            per_day = 390
        elif interval in {"5min"}:
            per_day = 78
        elif interval in {"15min"}:
            per_day = 26
        elif interval in {"30min"}:
            per_day = 13
        elif interval in {"45min"}:
            per_day = 9
        elif interval in {"1h"}:
            per_day = 7
        elif interval in {"2h"}:
            per_day = 4
        elif interval in {"4h"}:
            per_day = 2
        elif interval in {"1day"}:
            per_day = 1
        elif interval in {"1week"}:
            per_day = 1 / 7
        elif interval in {"1month"}:
            per_day = 1 / 30

        output = int(days * per_day * 1.15) + 20
        return max(60, min(output, 5000))

    @staticmethod
    def _parse_time_to_epoch(dt_text: str, tz_name: str | None) -> int:
        ts = pd.to_datetime(dt_text, errors="coerce")
        if pd.isna(ts):
            raise ValueError(f"Invalid candle datetime: {dt_text}")
        if ts.tzinfo is None:
            if tz_name:
                ts = ts.tz_localize(tz_name)
            else:
                ts = ts.tz_localize("UTC")
        ts = ts.tz_convert("UTC")
        return int(ts.timestamp())

    def get_quote(self, symbol: str) -> dict[str, Any]:
        sym = self._norm_symbol(symbol)
        cache_path = self._cache_path(f"quote_{sym}")
        cached = self._read_json(cache_path)
        if self.cache_enabled and cached and self._fresh(cache_path, self.quotes_ttl):
            return cached

        payload = self._request("quote", {"symbol": sym})
        price = float(payload.get("close") or payload.get("price") or payload.get("previous_close") or 0.0)
        quote = {
            "symbol": sym,
            "price": price,
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "quote_type": "DELAYED",
            "source": self.name,
        }
        self._write_json(cache_path, quote)
        return quote

    def get_candles(self, symbol: str, interval: str, period: str) -> list[dict[str, Any]]:
        sym = self._norm_symbol(symbol)
        td_interval = self._interval_to_twelve(interval)
        outputsize = self._period_to_outputsize(period, td_interval)
        cache_path = self._cache_path(f"candles_{sym}_{td_interval}_{period}")
        cached = self._read_json(cache_path)
        if self.cache_enabled and cached and self._fresh(cache_path, self.candles_ttl):
            return list(cached.get("candles", []))

        end_dt = datetime.now(timezone.utc)
        start_dt = end_dt - timedelta(days=400)
        payload = self._request(
            "time_series",
            {
                "symbol": sym,
                "interval": td_interval,
                "outputsize": outputsize,
                "start_date": start_dt.strftime("%Y-%m-%d %H:%M:%S"),
                "end_date": end_dt.strftime("%Y-%m-%d %H:%M:%S"),
                "timezone": "UTC",
                "format": "JSON",
                "order": "ASC",
            },
        )
        values = payload.get("values") or []
        if not values:
            raise ProviderError(f"TwelveData returned no candles for {sym}", provider=self.name)

        meta = payload.get("meta") or {}
        tz_name = meta.get("exchange_timezone") or meta.get("timezone") or "UTC"
        candles: list[dict[str, Any]] = []
        for row in values:
            try:
                candles.append(
                    {
                        "time": self._parse_time_to_epoch(str(row.get("datetime")), tz_name),
                        "open": float(row.get("open") or 0),
                        "high": float(row.get("high") or 0),
                        "low": float(row.get("low") or 0),
                        "close": float(row.get("close") or 0),
                        "volume": float(row.get("volume") or 0),
                    }
                )
            except Exception:
                continue

        if not candles:
            raise ProviderError(f"TwelveData returned malformed candles for {sym}", provider=self.name)

        result = {
            "symbol": sym,
            "source": self.name,
            "interval": interval,
            "period": period,
            "candles": candles,
            "timestamp": datetime.now(timezone.utc).isoformat(),
        }
        self._write_json(cache_path, result)
        return candles

    def get_option_expirations(self, symbol: str) -> list[dict[str, Any]]:
        raise ProviderError("TwelveData options expirations not configured in this app", provider=self.name)

    def get_option_chain(self, symbol: str, expiration: dict[str, Any] | str, **kwargs) -> dict[str, Any]:
        raise ProviderError("TwelveData option chains not configured in this app", provider=self.name)

    def get_options_ratios(self, symbol: str, expirations_to_check: int = 3) -> dict[str, Any]:
        raise ProviderError("TwelveData options ratios not configured in this app", provider=self.name)

    def get_ranked_contracts(self, symbol: str, expirations_to_check: int = 3) -> dict[str, Any]:
        raise ProviderError("TwelveData ranked contracts not configured in this app", provider=self.name)
