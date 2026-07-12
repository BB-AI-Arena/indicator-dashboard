from __future__ import annotations

import csv
import io
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


class StooqProvider(BaseMarketProvider):
    name = "stooq"

    def __init__(self, config: dict[str, Any]) -> None:
        self.config = config or {}
        cache_cfg = self.config.get("cache", {})
        data_cfg = self.config.get("data", {})
        stooq_cfg = self.config.get("stooq", {})
        self.cache_enabled = bool(data_cfg.get("cache_enabled", True))
        self.cache_dir = Path(os.getenv("CACHE_DIR", "/app/data/cache"))
        self.cache_dir.mkdir(parents=True, exist_ok=True)
        self.base_url = os.getenv("STOOQ_BASE_URL", stooq_cfg.get("base_url", "https://stooq.com/q/d/l/"))
        self.candles_ttl = int(cache_cfg.get("candles_ttl_seconds", 60))
        self.timeout = int(stooq_cfg.get("timeout_seconds", 20))

    def _cache_path(self, key: str) -> Path:
        safe = key.replace("/", "_").replace(" ", "_").replace(":", "_")
        return self.cache_dir / f"stooq_{safe}.json"

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
        return str(symbol or "").strip().upper()

    @staticmethod
    def _stooq_symbol(symbol: str) -> str:
        sym = StooqProvider._norm_symbol(symbol)
        if not sym:
            return sym
        if "." in sym and not sym.endswith(".US"):
            return sym.lower()
        if sym.endswith(".US"):
            return sym.lower()
        return f"{sym}.US".lower()

    @staticmethod
    def _period_to_start(period: str, end: datetime) -> datetime:
        text = str(period or "5d").strip().lower()
        units = [("mo", 30), ("wk", 7), ("y", 365), ("d", 1)]
        for suffix, days in units:
            if text.endswith(suffix):
                try:
                    return end - timedelta(days=max(1, int(text[: -len(suffix)]) * days))
                except Exception:
                    return end - timedelta(days=5)
        return end - timedelta(days=5)

    @staticmethod
    def _source_interval(interval: str) -> str:
        text = str(interval or "5m").strip().lower()
        if text == "1d":
            return "d"
        if text in {"5m", "15m"}:
            return "5"
        raise ProviderError(f"Stooq historical candles only support 5m, 15m via 5m aggregation, and 1d; got {interval}", provider="stooq")

    @staticmethod
    def _parse_timestamp(row: dict[str, str], interval: str) -> int:
        date_text = str(row.get("Date") or row.get("date") or "").strip()
        time_text = str(row.get("Time") or row.get("time") or "").strip()
        if not date_text:
            raise ValueError("missing date")
        if interval == "d" or not time_text:
            ts = pd.to_datetime(date_text, errors="coerce")
        else:
            ts = pd.to_datetime(f"{date_text} {time_text}", errors="coerce")
        if pd.isna(ts):
            raise ValueError(f"invalid timestamp {date_text} {time_text}".strip())
        if ts.tzinfo is None:
            ts = ts.tz_localize("America/New_York")
        return int(ts.tz_convert("UTC").timestamp())

    @staticmethod
    def _is_verification_page(text: str) -> bool:
        lower = (text or "").lower()
        return any(
            marker in lower
            for marker in [
                "<!doctype html",
                "<html",
                "requires javascript to verify your browser",
                "__verify",
                "captcha",
            ]
        )

    def _download_csv(self, symbol: str, source_interval: str, start: datetime, end: datetime) -> str:
        params = {
            "s": self._stooq_symbol(symbol),
            "i": source_interval,
            "d1": start.strftime("%Y%m%d"),
            "d2": end.strftime("%Y%m%d"),
        }
        try:
            rate_limit(self.name)
            response = requests.get(
                self.base_url,
                params=params,
                headers={"User-Agent": "indicator-dashboard/1.0"},
                timeout=self.timeout,
            )
        except Exception as exc:
            raise ProviderError(f"Stooq request failed: {exc}", provider=self.name) from exc

        text = response.text or ""
        if response.status_code == 429:
            raise ProviderError("Stooq rate limited (HTTP 429)", rate_limited=True, provider=self.name)
        if response.status_code >= 400:
            raise ProviderError(f"Stooq HTTP {response.status_code}: {text[:220]}", provider=self.name)
        if self._is_verification_page(text):
            raise ProviderError(
                "Stooq returned browser verification instead of CSV; pausing provider to avoid automated retries",
                rate_limited=True,
                provider=self.name,
            )
        return text

    def _parse_csv(self, text: str, source_interval: str) -> list[dict[str, Any]]:
        reader = csv.DictReader(io.StringIO(text.strip()))
        if not reader.fieldnames:
            raise ProviderError("Stooq returned empty CSV", provider=self.name)
        required = {"Open", "High", "Low", "Close"}
        if not required.issubset(set(reader.fieldnames)):
            raise ProviderError(f"Stooq CSV missing OHLC columns: {reader.fieldnames}", provider=self.name)

        candles: list[dict[str, Any]] = []
        for row in reader:
            try:
                candles.append(
                    {
                        "time": self._parse_timestamp(row, source_interval),
                        "open": float(row.get("Open") or 0),
                        "high": float(row.get("High") or 0),
                        "low": float(row.get("Low") or 0),
                        "close": float(row.get("Close") or 0),
                        "volume": float(row.get("Volume") or 0),
                    }
                )
            except Exception:
                continue
        if not candles:
            raise ProviderError("Stooq returned no usable candles", provider=self.name)
        return sorted(candles, key=lambda row: int(row.get("time", 0) or 0))

    @staticmethod
    def _aggregate_15m(candles: list[dict[str, Any]]) -> list[dict[str, Any]]:
        if not candles:
            return []
        df = pd.DataFrame(candles)
        df["time"] = pd.to_datetime(df["time"], unit="s", utc=True)
        df = df.set_index("time").sort_index()
        frames: list[pd.DataFrame] = []
        for _, day in df.tz_convert("America/New_York").groupby(lambda idx: idx.date()):
            agg = day.resample("15min", origin="start_day", offset="9h30min", label="left", closed="left").agg(
                {"open": "first", "high": "max", "low": "min", "close": "last", "volume": "sum"}
            )
            agg = agg.dropna(subset=["open", "high", "low", "close"])
            frames.append(agg)
        if not frames:
            return []
        out = pd.concat(frames).sort_index()
        out.index = out.index.tz_convert("UTC")
        return [
            {
                "time": int(idx.timestamp()),
                "open": float(row["open"]),
                "high": float(row["high"]),
                "low": float(row["low"]),
                "close": float(row["close"]),
                "volume": float(row.get("volume", 0) or 0),
            }
            for idx, row in out.iterrows()
        ]

    @staticmethod
    def _filter_range(candles: list[dict[str, Any]], start: datetime, end: datetime) -> list[dict[str, Any]]:
        start_ts = int(start.timestamp())
        end_ts = int(end.timestamp())
        return [row for row in candles if start_ts <= int(row.get("time", 0) or 0) <= end_ts]

    def get_quote(self, symbol: str) -> dict[str, Any]:
        raise ProviderError("Stooq quotes are not configured in this app", provider=self.name)

    def get_candles(self, symbol: str, interval: str, period: str) -> list[dict[str, Any]]:
        end = datetime.now(timezone.utc)
        start = self._period_to_start(period, end)
        return self.get_candles_range(symbol, interval, start.isoformat(), end.isoformat())

    def get_candles_range(
        self,
        symbol: str,
        interval: str,
        start_timestamp: str,
        end_timestamp: str,
    ) -> list[dict[str, Any]]:
        sym = self._norm_symbol(symbol)
        start = pd.Timestamp(start_timestamp).to_pydatetime()
        end = pd.Timestamp(end_timestamp).to_pydatetime()
        if start.tzinfo is None:
            start = start.replace(tzinfo=timezone.utc)
        if end.tzinfo is None:
            end = end.replace(tzinfo=timezone.utc)
        start = start.astimezone(timezone.utc)
        end = end.astimezone(timezone.utc)
        source_interval = self._source_interval(interval)
        cache_path = self._cache_path(f"candles_{sym}_{interval}_{start.date()}_{end.date()}")
        cached = self._read_json(cache_path)
        if self.cache_enabled and cached and self._fresh(cache_path, self.candles_ttl):
            return list(cached.get("candles", []))

        text = self._download_csv(sym, source_interval, start, end)
        candles = self._parse_csv(text, source_interval)
        if str(interval).strip().lower() == "15m":
            candles = self._aggregate_15m(candles)
        candles = self._filter_range(candles, start, end)
        if not candles:
            raise ProviderError(f"Stooq returned no candles for {sym} {interval}", provider=self.name)
        self._write_json(
            cache_path,
            {
                "symbol": sym,
                "source": self.name,
                "interval": interval,
                "candles": candles,
                "timestamp": datetime.now(timezone.utc).isoformat(),
            },
        )
        return candles

    def get_option_expirations(self, symbol: str) -> list[dict[str, Any]]:
        raise ProviderError("Stooq options expirations are not configured in this app", provider=self.name)

    def get_option_chain(self, symbol: str, expiration: dict[str, Any] | str, **kwargs) -> dict[str, Any]:
        raise ProviderError("Stooq option chains are not configured in this app", provider=self.name)

    def get_options_ratios(self, symbol: str, expirations_to_check: int = 3) -> dict[str, Any]:
        raise ProviderError("Stooq options ratios are not configured in this app", provider=self.name)

    def get_ranked_contracts(self, symbol: str, expirations_to_check: int = 3) -> dict[str, Any]:
        raise ProviderError("Stooq ranked contracts are not configured in this app", provider=self.name)
