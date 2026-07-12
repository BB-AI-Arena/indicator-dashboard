from __future__ import annotations

import csv
import io
import json
import os
import random
import re
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import requests

from ..cache_policy import market_aware_ttl
from .base import BaseMarketProvider, ProviderError
from .rate_limiter import rate_limit


_ALLOWED_SYMBOL = re.compile(r"^[A-Z0-9.^\-]{1,32}$")
_INTRADAY_INTERVALS = {
    "1m": "1min",
    "5m": "5min",
    "15m": "15min",
    "30m": "30min",
    "60m": "60min",
}


class AlphaVantageProvider(BaseMarketProvider):
    name = "alphavantage"

    def __init__(self, config: dict[str, Any]) -> None:
        self.config = config or {}
        cache_cfg = self.config.get("cache", {})
        av_cfg = self.config.get("alphavantage", {})
        data_cfg = self.config.get("data", {})
        self.cache_enabled = bool(data_cfg.get("cache_enabled", True))
        self.cache_dir = Path(os.getenv("CACHE_DIR", "/app/data/cache"))
        self.cache_dir.mkdir(parents=True, exist_ok=True)
        self.base_url = os.getenv("ALPHA_VANTAGE_BASE_URL", av_cfg.get("base_url", "https://www.alphavantage.co/query")).rstrip("/")
        self.api_key = os.getenv("ALPHA_VANTAGE_API_KEY", "").strip()
        self.output_format = str(os.getenv("ALPHA_VANTAGE_OUTPUT_FORMAT", av_cfg.get("output_format", "json")) or "json").strip().lower()
        self.timeout_seconds = int(av_cfg.get("timeout_seconds", 20) or 20)
        self.quotes_ttl = int(cache_cfg.get("quotes_ttl_seconds", 10))
        self.candles_ttl = int(cache_cfg.get("candles_ttl_seconds", 60))
        self.mode_ttl = int(av_cfg.get("mode_ttl_seconds", 86400) or 86400)
        self.daily_outputsize = str(av_cfg.get("daily_outputsize", "full") or "full").strip().lower()
        self.adjusted_outputsize = str(av_cfg.get("adjusted_outputsize", "full") or "full").strip().lower()
        self.intraday_outputsize = str(av_cfg.get("intraday_outputsize", "full") or "full").strip().lower()
        self.intraday_extended_hours = bool(av_cfg.get("intraday_extended_hours", True))
        self.intraday_adjusted = bool(av_cfg.get("intraday_adjusted", True))
        self.daily_prefer_adjusted = bool(av_cfg.get("daily_prefer_adjusted", True))

    def _norm_symbol(self, symbol: str) -> str:
        normalized = str(symbol or "").strip().upper()
        if not normalized or not _ALLOWED_SYMBOL.fullmatch(normalized):
            raise ProviderError(f"Invalid Alpha Vantage symbol: {symbol!r}", provider=self.name)
        return normalized

    def _safe_name(self, *parts: Any) -> str:
        text = "_".join(str(part) for part in parts)
        return text.replace("/", "_").replace(" ", "_").replace(":", "_")

    def _cache_path(self, key: str, ext: str = "json") -> Path:
        return self.cache_dir / f"alphavantage_{key}.{ext}"

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

    def _mode_path(self, symbol: str, interval: str) -> Path:
        return self._cache_path(self._safe_name("mode", symbol, interval))

    def _read_mode(self, symbol: str, interval: str) -> str | None:
        payload = self._read_json(self._mode_path(symbol, interval))
        if not payload:
            return None
        if not self._fresh(self._mode_path(symbol, interval), self.mode_ttl):
            return None
        mode = str(payload.get("mode") or "").strip().lower()
        return mode if mode in {"full", "compact"} else None

    def _write_mode(self, symbol: str, interval: str, mode: str, reason: str) -> None:
        self._write_json(
            self._mode_path(symbol, interval),
            {
                "symbol": symbol,
                "interval": interval,
                "mode": mode,
                "reason": reason,
                "timestamp": datetime.now(timezone.utc).isoformat(),
            },
        )

    @staticmethod
    def _normalize_outputsize(value: str, fallback: str = "full") -> str:
        text = str(value or "").strip().lower()
        return text if text in {"full", "compact"} else fallback

    def _require_key(self) -> None:
        if not self.api_key:
            raise ProviderError("Alpha Vantage API key is missing (set ALPHA_VANTAGE_API_KEY)", provider=self.name)

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
    def _normalize_iso(value: Any) -> str | None:
        if value is None:
            return None
        text = str(value).strip()
        if not text:
            return None
        try:
            parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
        except ValueError:
            try:
                parsed = datetime.strptime(text, "%Y-%m-%d")
            except Exception:
                return text
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=timezone.utc)
        return parsed.astimezone(timezone.utc).isoformat()

    @staticmethod
    def _to_epoch(value: str) -> int | None:
        try:
            parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        except Exception:
            return None
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=timezone.utc)
        return int(parsed.astimezone(timezone.utc).timestamp())

    def _api_get(self, params: dict[str, Any]) -> requests.Response:
        self._require_key()
        merged = {**params, "apikey": self.api_key}
        last_error: Exception | None = None
        rate_cfg = self.config.get("rate_limits", {}).get(self.name, {}) or {}
        max_retries = int(rate_cfg.get("max_retries", 0) or 0)
        initial_backoff = float(rate_cfg.get("backoff_initial_seconds", 10) or 10)
        max_backoff = float(rate_cfg.get("backoff_max_seconds", 300) or 300)

        for attempt in range(max_retries + 1):
            try:
                rate_limit(self.name)
                response = requests.get(self.base_url, params=merged, timeout=self.timeout_seconds)
                if response.status_code == 429:
                    raise ProviderError("Alpha Vantage rate limited (HTTP 429)", rate_limited=True, provider=self.name)
                if response.status_code >= 500:
                    raise ProviderError(f"Alpha Vantage HTTP {response.status_code}", provider=self.name)
                if response.status_code >= 400:
                    text = (response.text or "").strip().replace("\n", " ")
                    raise ProviderError(f"Alpha Vantage HTTP {response.status_code}: {text[:220]}", provider=self.name)
                return response
            except ProviderError as exc:
                if getattr(exc, "rate_limited", False):
                    raise
                last_error = exc
            except (requests.Timeout, requests.ConnectionError) as exc:
                last_error = exc
            except Exception as exc:
                last_error = exc

            if attempt >= max_retries:
                break

            wait = min(max_backoff, initial_backoff * (2 ** attempt))
            wait += random.uniform(0, min(5.0, wait * 0.25))
            time.sleep(wait)

        raise ProviderError(f"Alpha Vantage request failed after retries: {last_error}", provider=self.name) from last_error

    def _parse_payload(self, response: requests.Response, *, allow_csv: bool = False) -> dict[str, Any] | list[dict[str, Any]]:
        text = response.text or ""
        if allow_csv and self.output_format == "csv" and not text.lstrip().startswith("{"):
            return self._parse_csv(text)

        try:
            payload = response.json()
        except Exception as exc:
            raise ProviderError(f"Alpha Vantage returned malformed JSON: {exc}", provider=self.name) from exc

        if not isinstance(payload, dict):
            raise ProviderError("Alpha Vantage returned an unexpected payload", provider=self.name)

        error_message = payload.get("Error Message")
        if error_message:
            raise ProviderError(f"Alpha Vantage error: {error_message}", provider=self.name)

        note = payload.get("Note")
        if note:
            message = str(note)
            if any(marker in message.lower() for marker in ["call frequency", "rate", "limit", "thank you for using alpha vantage"]):
                raise ProviderError(f"Alpha Vantage rate limited: {message}", rate_limited=True, provider=self.name)
            raise ProviderError(f"Alpha Vantage note: {message}", provider=self.name)

        info = payload.get("Information")
        if info:
            message = str(info)
            lower = message.lower()
            if any(marker in lower for marker in ["premium", "subscribe", "full", "call frequency", "rate limit", "frequency"]):
                raise ProviderError(f"Alpha Vantage premium-only response: {message}", provider=self.name)
            raise ProviderError(f"Alpha Vantage information: {message}", provider=self.name)

        return payload

    def _parse_csv(self, text: str) -> list[dict[str, Any]]:
        reader = csv.DictReader(io.StringIO(text.strip()))
        if not reader.fieldnames:
            raise ProviderError("Alpha Vantage returned empty CSV", provider=self.name)

        rows: list[dict[str, Any]] = []
        for raw in reader:
            normalized = {str(key).strip().lower(): value for key, value in raw.items() if key is not None}
            timestamp = self._normalize_iso(
                normalized.get("timestamp") or normalized.get("time") or normalized.get("date")
            )
            if not timestamp:
                continue
            close = self._to_float(normalized.get("close"))
            adjusted_close = self._to_float(normalized.get("adjusted close"), close)
            epoch = self._to_epoch(timestamp)
            if epoch is None:
                continue
            rows.append(
                {
                    "time": epoch,
                    "open": self._to_float(normalized.get("open")),
                    "high": self._to_float(normalized.get("high")),
                    "low": self._to_float(normalized.get("low")),
                    "close": close,
                    "adjustedClose": adjusted_close,
                    "volume": self._to_int(normalized.get("volume")),
                    "dividend": self._to_float(normalized.get("dividend amount")),
                    "splitCoefficient": self._to_float(normalized.get("split coefficient"), 1.0),
                }
            )
        if not rows:
            raise ProviderError("Alpha Vantage CSV returned no usable rows", provider=self.name)
        return sorted(rows, key=lambda row: int(row.get("time", 0) or 0))

    @staticmethod
    def _parse_series_rows(
        payload: dict[str, Any],
        *,
        adjusted: bool,
    ) -> tuple[list[dict[str, Any]], dict[str, Any]]:
        meta = {}
        for key, value in payload.items():
            if str(key).lower().startswith("meta"):
                meta = value if isinstance(value, dict) else {}
                break

        series_key = next((key for key in payload.keys() if "time series" in str(key).lower()), None)
        if not series_key:
            raise ProviderError("Alpha Vantage response missing time series data", provider="alphavantage")

        series = payload.get(series_key) or {}
        if not isinstance(series, dict) or not series:
            raise ProviderError("Alpha Vantage returned empty time series data", provider="alphavantage")

        rows: list[dict[str, Any]] = []
        for timestamp_text, row in sorted(series.items(), key=lambda item: item[0]):
            if not isinstance(row, dict):
                continue
            parsed = AlphaVantageProvider._normalize_iso(timestamp_text)
            if not parsed:
                continue
            close = AlphaVantageProvider._to_float(
                row.get("4. close") or row.get("close") or row.get("05. price") or row.get("price")
            )
            adjusted_close = AlphaVantageProvider._to_float(row.get("5. adjusted close"), close)
            rows.append(
                {
                    "time": int(datetime.fromisoformat(parsed).timestamp()),
                    "open": AlphaVantageProvider._to_float(row.get("1. open") or row.get("open")),
                    "high": AlphaVantageProvider._to_float(row.get("2. high") or row.get("high")),
                    "low": AlphaVantageProvider._to_float(row.get("3. low") or row.get("low")),
                    "close": close,
                    "adjustedClose": adjusted_close if adjusted else close,
                    "volume": AlphaVantageProvider._to_int(
                        row.get("6. volume")
                        or row.get("5. volume")
                        or row.get("volume")
                    ),
                    "dividend": AlphaVantageProvider._to_float(row.get("7. dividend amount"), 0.0),
                    "splitCoefficient": AlphaVantageProvider._to_float(row.get("8. split coefficient"), 1.0),
                }
            )
        if not rows:
            raise ProviderError("Alpha Vantage returned no usable candle rows", provider="alphavantage")
        rows.sort(key=lambda row: int(row.get("time", 0) or 0))
        return rows, meta

    def _cache_history_payload(
        self,
        symbol: str,
        interval: str,
        mode: str,
        payload: dict[str, Any],
        candles: list[dict[str, Any]],
        *,
        warning: str | None = None,
    ) -> None:
        cache_path = self._cache_path(self._safe_name("history", symbol, interval, mode))
        self._write_json(
            cache_path,
            {
                "symbol": symbol,
                "interval": interval,
                "mode": mode,
                "provider": self.name,
                "source": self.name,
                "timestamp": datetime.now(timezone.utc).isoformat(),
                "meta": payload,
                "warning": warning,
                "candles": candles,
            },
        )

    def _read_history_cache(self, symbol: str, interval: str, mode: str) -> list[dict[str, Any]] | None:
        cache_path = self._cache_path(self._safe_name("history", symbol, interval, mode))
        cached = self._read_json(cache_path)
        if cached and self._fresh(cache_path, self.candles_ttl):
            return list(cached.get("candles", []))
        return None

    @staticmethod
    def _slice_for_period(candles: list[dict[str, Any]], period: str) -> list[dict[str, Any]]:
        if not candles:
            return candles
        text = str(period or "").strip().lower()
        if text in {"", "max", "full"}:
            return candles

        multipliers = {
            "y": 365 * 86400,
            "mo": 30 * 86400,
            "wk": 7 * 86400,
            "d": 86400,
        }
        seconds = None
        for suffix, factor in multipliers.items():
            if text.endswith(suffix):
                try:
                    seconds = max(1, int(text[: -len(suffix)]) * factor)
                except Exception:
                    seconds = None
                break
        if seconds is None:
            return candles
        cutoff = int(datetime.now(timezone.utc).timestamp()) - seconds
        sliced = [row for row in candles if int(row.get("time", 0) or 0) >= cutoff]
        return sliced or candles

    def _fetch_daily_series(self, symbol: str) -> tuple[list[dict[str, Any]], dict[str, Any], str]:
        normalized = self._norm_symbol(symbol)
        default_mode = self._normalize_outputsize(
            self.adjusted_outputsize if self.daily_prefer_adjusted else self.daily_outputsize
        )
        mode = self._read_mode(normalized, "1d") or default_mode
        cached = self._read_history_cache(normalized, "1d", mode)
        if cached is not None:
            return cached, {"mode": mode, "cached": True}, mode
        alternate_mode = "compact" if mode == "full" else "full"
        alternate_cached = self._read_history_cache(normalized, "1d", alternate_mode)
        if alternate_cached is not None:
            return alternate_cached, {"mode": alternate_mode, "cached": True}, alternate_mode

        cache_order = [mode] if mode == "compact" else ["full", "compact"]
        last_error: Exception | None = None

        for requested_mode in cache_order:
            cached = self._read_history_cache(normalized, "1d", requested_mode)
            if cached is not None:
                return cached, {"mode": requested_mode, "cached": True}, requested_mode

            variants = []
            if self.daily_prefer_adjusted:
                variants.append(("TIME_SERIES_DAILY_ADJUSTED", True))
            variants.append(("TIME_SERIES_DAILY", False))

            for function_name, adjusted in variants:
                params = {
                    "function": function_name,
                    "symbol": normalized,
                    "outputsize": requested_mode,
                }
                if self.output_format == "csv":
                    params["datatype"] = "csv"
                response = self._api_get(params)
                try:
                    parsed = self._parse_payload(response, allow_csv=True)
                    if isinstance(parsed, list):
                        candles = parsed
                        meta = {
                            "function": function_name,
                            "adjusted": adjusted,
                            "outputsize": requested_mode,
                            "last_refreshed": None,
                        }
                    else:
                        candles, meta = self._parse_series_rows(parsed, adjusted=adjusted)
                        meta = {
                            **meta,
                            "function": function_name,
                            "adjusted": adjusted,
                            "outputsize": requested_mode,
                            "last_refreshed": meta.get("3. Last Refreshed") or meta.get("last_refreshed"),
                        }
                    self._cache_history_payload(normalized, "1d", requested_mode, meta, candles)
                    if requested_mode != mode:
                        self._write_mode(normalized, "1d", requested_mode, "premium_only_or_unavailable")
                    return candles, meta, requested_mode
                except ProviderError as exc:
                    last_error = exc
                    if getattr(exc, "rate_limited", False):
                        raise
                    if any(marker in str(exc).lower() for marker in ["premium-only", "full", "subscribe"]):
                        continue
                    if function_name == "TIME_SERIES_DAILY_ADJUSTED" and self.daily_prefer_adjusted:
                        continue
                    if function_name == "TIME_SERIES_DAILY" and requested_mode == "full":
                        continue

        if last_error:
            raise last_error
        raise ProviderError("Alpha Vantage daily history unavailable", provider=self.name)

    def _fetch_intraday_series(self, symbol: str, interval: str) -> tuple[list[dict[str, Any]], dict[str, Any], str]:
        normalized = self._norm_symbol(symbol)
        interval_key = _INTRADAY_INTERVALS.get(interval)
        if not interval_key:
            raise ProviderError(f"Unsupported Alpha Vantage intraday interval: {interval}", provider=self.name)

        mode = self._read_mode(normalized, interval) or self._normalize_outputsize(self.intraday_outputsize)
        cached = self._read_history_cache(normalized, interval, mode)
        if cached is not None:
            return cached, {"mode": mode, "cached": True}, mode
        alternate_mode = "compact" if mode == "full" else "full"
        alternate_cached = self._read_history_cache(normalized, interval, alternate_mode)
        if alternate_cached is not None:
            return alternate_cached, {"mode": alternate_mode, "cached": True}, alternate_mode

        cache_order = [mode] if mode == "compact" else ["full", "compact"]
        last_error: Exception | None = None

        for requested_mode in cache_order:
            cached = self._read_history_cache(normalized, interval, requested_mode)
            if cached is not None:
                return cached, {"mode": requested_mode, "cached": True}, requested_mode

            params = {
                "function": "TIME_SERIES_INTRADAY",
                "symbol": normalized,
                "interval": interval_key,
                "outputsize": requested_mode,
                "extended_hours": str(self.intraday_extended_hours).lower(),
                "adjusted": str(self.intraday_adjusted).lower(),
            }
            if self.output_format == "csv":
                params["datatype"] = "csv"

            response = self._api_get(params)
            try:
                parsed = self._parse_payload(response, allow_csv=True)
                if isinstance(parsed, list):
                    candles = parsed
                    meta = {
                        "function": "TIME_SERIES_INTRADAY",
                        "interval": interval_key,
                        "outputsize": requested_mode,
                        "last_refreshed": None,
                    }
                else:
                    candles, meta = self._parse_series_rows(parsed, adjusted=self.intraday_adjusted)
                    meta = {
                        **meta,
                        "function": "TIME_SERIES_INTRADAY",
                        "interval": interval_key,
                        "outputsize": requested_mode,
                        "last_refreshed": meta.get("3. Last Refreshed") or meta.get("last_refreshed"),
                    }
                self._cache_history_payload(normalized, interval, requested_mode, meta, candles)
                if requested_mode != mode:
                    self._write_mode(normalized, interval, requested_mode, "premium_only_or_unavailable")
                return candles, meta, requested_mode
            except ProviderError as exc:
                last_error = exc
                if getattr(exc, "rate_limited", False):
                    raise
                if any(marker in str(exc).lower() for marker in ["premium-only", "full", "subscribe"]):
                    continue

        if last_error:
            raise last_error
        raise ProviderError("Alpha Vantage intraday history unavailable", provider=self.name)

    def get_quote(self, symbol: str) -> dict[str, Any]:
        sym = self._norm_symbol(symbol)
        cache_path = self._cache_path(self._safe_name("quote", sym))
        cached = self._read_json(cache_path)
        if self.cache_enabled and cached and self._fresh(cache_path, self.quotes_ttl):
            return cached

        response = self._api_get({"function": "GLOBAL_QUOTE", "symbol": sym})
        payload = self._parse_payload(response)
        if not isinstance(payload, dict):
            raise ProviderError("Alpha Vantage returned an unexpected quote payload", provider=self.name)

        quote_data = None
        for key in payload.keys():
            if "global quote" in str(key).lower():
                quote_data = payload.get(key)
                break
        if not isinstance(quote_data, dict) or not quote_data:
            raise ProviderError(f"Alpha Vantage returned no quote for {sym}", provider=self.name)

        quote = {
            "symbol": sym,
            "price": self._to_float(quote_data.get("05. price") or quote_data.get("price")),
            "open": self._to_float(quote_data.get("02. open") or quote_data.get("open")),
            "high": self._to_float(quote_data.get("03. high") or quote_data.get("high")),
            "low": self._to_float(quote_data.get("04. low") or quote_data.get("low")),
            "volume": self._to_int(quote_data.get("06. volume") or quote_data.get("volume")),
            "previous_close": self._to_float(quote_data.get("08. previous close") or quote_data.get("previous_close")),
            "change": self._to_float(quote_data.get("09. change") or quote_data.get("change")),
            "change_percent": str(quote_data.get("10. change percent") or quote_data.get("change_percent") or ""),
            "latest_trading_day": self._normalize_iso(quote_data.get("07. latest trading day") or quote_data.get("latest_trading_day")),
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "last_refreshed": self._normalize_iso(quote_data.get("07. latest trading day") or quote_data.get("latest_trading_day")),
            "quote_type": "DELAYED",
            "source": self.name,
            "provider": self.name,
        }
        self._write_json(cache_path, quote)
        return quote

    def get_earnings_history(self, symbol: str, *, force_refresh: bool = False) -> list[dict[str, Any]]:
        sym = self._norm_symbol(symbol)
        earnings_cfg = self.config.get("earnings_history", {}) or {}
        ttl = int(earnings_cfg.get("cache_ttl_seconds", 86400) or 86400)
        cache_path = self._cache_path(self._safe_name("earnings", sym))
        cached = self._read_json(cache_path)
        if cached and not force_refresh and self._fresh(cache_path, ttl):
            return list(cached.get("quarterly_earnings") or [])

        response = self._api_get({"function": "EARNINGS", "symbol": sym})
        payload = self._parse_payload(response)
        if not isinstance(payload, dict):
            raise ProviderError("Alpha Vantage earnings response was malformed", provider=self.name)
        rows = payload.get("quarterlyEarnings") or []
        if not isinstance(rows, list):
            raise ProviderError("Alpha Vantage earnings response had no quarterly rows", provider=self.name)

        normalized: list[dict[str, Any]] = []
        for row in rows:
            if not isinstance(row, dict):
                continue
            normalized.append(
                {
                    "fiscal_date_ending": str(row.get("fiscalDateEnding") or "").strip() or None,
                    "reported_date": str(row.get("reportedDate") or "").strip() or None,
                    "reported_eps": row.get("reportedEPS"),
                    "estimated_eps": row.get("estimatedEPS"),
                    "surprise": row.get("surprise"),
                    "surprise_percentage": row.get("surprisePercentage"),
                    "reported_revenue": row.get("reportedRevenue"),
                    "estimated_revenue": row.get("estimatedRevenue"),
                    "report_time": str(row.get("reportTime") or "").strip() or None,
                    "provider": self.name,
                }
            )
        if not normalized:
            raise ProviderError(f"Alpha Vantage returned no quarterly earnings for {sym}", provider=self.name)
        self._write_json(
            cache_path,
            {
                "symbol": sym,
                "provider": self.name,
                "retrieved_at": datetime.now(timezone.utc).isoformat(),
                "quarterly_earnings": normalized,
            },
        )
        return normalized

    def get_candles(self, symbol: str, interval: str, period: str) -> list[dict[str, Any]]:
        sym = self._norm_symbol(symbol)
        interval_text = str(interval or "5m").strip().lower()
        if interval_text == "1d":
            candles, meta, _ = self._fetch_daily_series(sym)
        else:
            candles, meta, _ = self._fetch_intraday_series(sym, interval_text)

        # Keep the cached payload around even when the caller only needs a slice.
        if candles:
            self._write_json(
                self._cache_path(self._safe_name("slice", sym, interval_text, period)),
                {
                    "symbol": sym,
                    "interval": interval_text,
                    "period": period,
                    "provider": self.name,
                    "source": self.name,
                    "timestamp": datetime.now(timezone.utc).isoformat(),
                    "meta": meta,
                    "candles": candles,
                },
            )
        return self._slice_for_period(candles, period)

    def get_candles_range(
        self,
        symbol: str,
        interval: str,
        start_timestamp: str,
        end_timestamp: str,
    ) -> list[dict[str, Any]]:
        sym = self._norm_symbol(symbol)
        start = datetime.fromisoformat(str(start_timestamp).replace("Z", "+00:00"))
        end = datetime.fromisoformat(str(end_timestamp).replace("Z", "+00:00"))
        if start.tzinfo is None:
            start = start.replace(tzinfo=timezone.utc)
        if end.tzinfo is None:
            end = end.replace(tzinfo=timezone.utc)
        start = start.astimezone(timezone.utc)
        end = end.astimezone(timezone.utc)
        candles = self.get_candles(sym, interval, "max")
        start_ts = int(start.timestamp())
        end_ts = int(end.timestamp())
        return [row for row in candles if start_ts <= int(row.get("time", 0) or 0) <= end_ts]

    def get_option_expirations(self, symbol: str) -> list[dict[str, Any]]:
        raise ProviderError("Alpha Vantage options expirations are not configured in this app", provider=self.name)

    def get_option_chain(self, symbol: str, expiration: dict[str, Any] | str, **kwargs) -> dict[str, Any]:
        raise ProviderError("Alpha Vantage option chains are not configured in this app", provider=self.name)

    def get_options_ratios(self, symbol: str, expirations_to_check: int = 3) -> dict[str, Any]:
        raise ProviderError("Alpha Vantage options ratios are not configured in this app", provider=self.name)

    def get_ranked_contracts(self, symbol: str, expirations_to_check: int = 3) -> dict[str, Any]:
        raise ProviderError("Alpha Vantage ranked contracts are not configured in this app", provider=self.name)
