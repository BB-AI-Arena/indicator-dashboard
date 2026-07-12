from __future__ import annotations

import json
import os
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import numpy as np
import pandas as pd
import yfinance as yf

from .base import BaseMarketProvider, ProviderError
from .option_filters import (
    FILTER_VERSION,
    central_today,
    current_expirations,
    filter_contracts,
    filter_signature,
    spread_pct,
)
from .option_scoring import enrich_contract
from .option_positioning import build_option_positioning
from ..cache_policy import market_aware_ttl
from ..market_session import get_market_session
from .rate_limiter import rate_limit


class YahooProvider(BaseMarketProvider):
    name = "yahoo"

    def __init__(self, config: dict[str, Any]) -> None:
        self.config = config or {}
        cache_cfg = self.config.get("cache", {})
        data_cfg = self.config.get("data", {})
        self.cache_enabled = bool(data_cfg.get("cache_enabled", True))
        self.cache_dir = Path(os.getenv("CACHE_DIR", "/app/data/cache"))
        self.cache_dir.mkdir(parents=True, exist_ok=True)
        self.quotes_ttl = int(cache_cfg.get("quotes_ttl_seconds", 10))
        self.candles_ttl = int(cache_cfg.get("candles_ttl_seconds", 60))
        self.exp_ttl = int(cache_cfg.get("option_expirations_ttl_seconds", 86400))
        self.chain_ttl = int(cache_cfg.get("option_chains_ttl_seconds", 60))
        self.ratios_ttl = int(cache_cfg.get("option_chains_ttl_seconds", 60))
        self.contracts_ttl = int(cache_cfg.get("option_chains_ttl_seconds", 60))

    def _norm_symbol(self, symbol: str) -> str:
        return symbol.strip().upper()

    def _safe_name(self, *parts: Any) -> str:
        text = "_".join(str(p) for p in parts)
        return text.replace("/", "_").replace(" ", "_")

    def _cache_path(self, key: str, ext: str = "json") -> Path:
        return self.cache_dir / f"yahoo_{key}.{ext}"

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

    def _rate_limit_error(self, exc: Exception) -> ProviderError:
        msg = str(exc)
        lower = msg.lower()
        is_rate = "too many requests" in lower or "rate limit" in lower or "429" in lower
        return ProviderError(f"Yahoo error: {msg}", rate_limited=is_rate, provider=self.name)

    def _history_cache_path(self, symbol: str, interval: str) -> Path:
        return self._cache_path(self._safe_name("history", symbol, interval))

    @staticmethod
    def _period_to_seconds(period: str) -> int | None:
        if not period:
            return None
        text = period.strip().lower()
        units = {"m": 60, "h": 3600, "d": 86400, "wk": 86400 * 7, "mo": 86400 * 30, "y": 86400 * 365}
        for suffix, factor in units.items():
            if text.endswith(suffix):
                try:
                    return int(text[: -len(suffix)]) * factor
                except Exception:
                    return None
        return None

    def _slice_for_period(self, candles: list[dict[str, Any]], period: str) -> list[dict[str, Any]]:
        if not candles:
            return candles
        seconds = self._period_to_seconds(period)
        if not seconds:
            return candles
        max_ts = max(int(c.get("time", 0) or 0) for c in candles)
        cutoff = max_ts - seconds
        sliced = [c for c in candles if int(c.get("time", 0) or 0) >= cutoff]
        return sliced or candles

    def _merge_history(self, symbol: str, interval: str, candles: list[dict[str, Any]]) -> list[dict[str, Any]]:
        path = self._history_cache_path(symbol, interval)
        existing = self._read_json(path) or {}
        rows = list(existing.get("candles", [])) + list(candles)
        by_time: dict[int, dict[str, Any]] = {}
        for row in rows:
            ts = int(row.get("time", 0) or 0)
            if ts <= 0:
                continue
            by_time[ts] = {
                "time": ts,
                "open": float(row.get("open", 0) or 0),
                "high": float(row.get("high", 0) or 0),
                "low": float(row.get("low", 0) or 0),
                "close": float(row.get("close", 0) or 0),
                "volume": float(row.get("volume", 0) or 0),
            }
        merged = [by_time[k] for k in sorted(by_time.keys())]
        # Keep last ~120 trading days of 1m bars at most; enough for intraday analysis/backtests.
        if len(merged) > 50000:
            merged = merged[-50000:]
        payload = {
            "symbol": symbol,
            "interval": interval,
            "candles": merged,
            "source": self.name,
            "timestamp": datetime.now(timezone.utc).isoformat(),
        }
        self._write_json(path, payload)
        return merged

    def _read_history(self, symbol: str, interval: str) -> list[dict[str, Any]]:
        payload = self._read_json(self._history_cache_path(symbol, interval)) or {}
        return list(payload.get("candles", []))

    def get_quote(self, symbol: str) -> dict[str, Any]:
        sym = self._norm_symbol(symbol)
        cache_path = self._cache_path(self._safe_name("quote", sym))
        cached = self._read_json(cache_path)
        if self.cache_enabled and cached and self._fresh(cache_path, self.quotes_ttl):
            return cached

        try:
            rate_limit(self.name)
            ticker = yf.Ticker(sym)
            fi = ticker.fast_info or {}
            price = float(fi.get("lastPrice") or fi.get("last_price") or 0)
            quote = {
                "symbol": sym,
                "price": price,
                "timestamp": datetime.utcnow().isoformat(),
                "quote_type": None,
                "source": self.name,
            }
            self._write_json(cache_path, quote)
            return quote
        except Exception as exc:
            if cached:
                return cached
            raise self._rate_limit_error(exc) from exc

    def get_candles(self, symbol: str, interval: str, period: str) -> list[dict[str, Any]]:
        sym = self._norm_symbol(symbol)
        key = self._safe_name("candles", sym, interval, period)
        cache_path = self._cache_path(key)
        cached = self._read_json(cache_path)
        history = self._read_history(sym, interval)
        if self.cache_enabled and cached and self._fresh(cache_path, self.candles_ttl):
            rows = list(cached.get("candles", []))
            if rows and not history:
                self._merge_history(sym, interval, rows)
            return rows

        try:
            rate_limit(self.name)
            ticker = yf.Ticker(sym)
            frame = ticker.history(interval=interval, period=period, auto_adjust=False)
            if frame is None or frame.empty:
                raise ProviderError(f"No candle data returned for {sym}", provider=self.name)
            frame = frame.rename(columns={"Open": "open", "High": "high", "Low": "low", "Close": "close", "Volume": "volume"})
            frame = frame[[c for c in ["open", "high", "low", "close", "volume"] if c in frame.columns]].dropna(subset=["open", "high", "low", "close"])
            candles: list[dict[str, Any]] = []
            for idx, row in frame.iterrows():
                ts = int(idx.timestamp()) if hasattr(idx, "timestamp") else int(idx)
                candles.append(
                    {
                        "time": ts,
                        "open": float(row["open"]),
                        "high": float(row["high"]),
                        "low": float(row["low"]),
                        "close": float(row["close"]),
                        "volume": float(row.get("volume", 0) or 0),
                    }
                )
            merged = self._merge_history(sym, interval, candles)
            sliced = self._slice_for_period(merged, period)
            self._write_json(cache_path, {"symbol": sym, "candles": sliced, "source": self.name, "timestamp": datetime.utcnow().isoformat()})
            return sliced
        except Exception as exc:
            if cached:
                rows = list(cached.get("candles", []))
                if rows and not history:
                    self._merge_history(sym, interval, rows)
                return rows
            if history:
                return self._slice_for_period(history, period)
            raise self._rate_limit_error(exc) from exc

    def get_candles_range(
        self,
        symbol: str,
        interval: str,
        start_timestamp: str,
        end_timestamp: str,
    ) -> list[dict[str, Any]]:
        sym = self._norm_symbol(symbol)
        try:
            start = pd.Timestamp(start_timestamp).to_pydatetime()
            end = pd.Timestamp(end_timestamp).to_pydatetime()
            rate_limit(self.name)
            ticker = yf.Ticker(sym)
            frame = ticker.history(interval=interval, start=start, end=end, auto_adjust=False)
            if frame is None or frame.empty:
                raise ProviderError(f"No candle data returned for {sym} {interval} range", provider=self.name)
            frame = frame.rename(columns={"Open": "open", "High": "high", "Low": "low", "Close": "close", "Volume": "volume"})
            frame = frame[[c for c in ["open", "high", "low", "close", "volume"] if c in frame.columns]].dropna(subset=["open", "high", "low", "close"])
            candles: list[dict[str, Any]] = []
            for idx, row in frame.iterrows():
                ts = int(idx.timestamp()) if hasattr(idx, "timestamp") else int(idx)
                candles.append(
                    {
                        "time": ts,
                        "open": float(row["open"]),
                        "high": float(row["high"]),
                        "low": float(row["low"]),
                        "close": float(row["close"]),
                        "volume": float(row.get("volume", 0) or 0),
                    }
                )
            self._merge_history(sym, interval, candles)
            return candles
        except Exception as exc:
            raise self._rate_limit_error(exc) from exc

    def get_option_expirations(self, symbol: str) -> list[dict[str, Any]]:
        sym = self._norm_symbol(symbol)
        cache_path = self._cache_path(self._safe_name("expirations", sym))
        cached = self._read_json(cache_path)
        if self.cache_enabled and cached and self._fresh(cache_path, self.exp_ttl):
            return current_expirations(list(cached.get("expirations", [])))

        try:
            rate_limit(self.name)
            ticker = yf.Ticker(sym)
            expirations = [
                {"date": exp, "source": self.name}
                for exp in list(ticker.options or [])
            ]
            expirations = current_expirations(expirations)
            self._write_json(cache_path, {"symbol": sym, "expirations": expirations, "timestamp": datetime.utcnow().isoformat()})
            return expirations
        except Exception as exc:
            if cached:
                return current_expirations(list(cached.get("expirations", [])))
            raise self._rate_limit_error(exc) from exc

    def _chain_from_frames(self, symbol: str, expiration: str, calls: pd.DataFrame, puts: pd.DataFrame) -> dict[str, Any]:
        rows: list[dict[str, Any]] = []
        for opt_type, frame in (("CALL", calls), ("PUT", puts)):
            if frame is None or frame.empty:
                continue
            for _, row in frame.iterrows():
                rows.append(
                    {
                        "contract_symbol": row.get("contractSymbol"),
                        "osi_key": row.get("contractSymbol"),
                        "expiration": expiration,
                        "strike": float(row.get("strike", 0) or 0),
                        "type": opt_type,
                        "bid": float(row.get("bid", 0) or 0),
                        "ask": float(row.get("ask", 0) or 0),
                        "last": float(row.get("lastPrice", 0) or 0),
                        "volume": int(0 if pd.isna(row.get("volume")) else (row.get("volume") or 0)),
                        "open_interest": int(0 if pd.isna(row.get("openInterest")) else (row.get("openInterest") or 0)),
                        "implied_volatility": float(row.get("impliedVolatility", 0) or 0),
                        "delta": None,
                        "gamma": None,
                        "theta": None,
                        "vega": None,
                        "rho": None,
                        "in_the_money": bool(row.get("inTheMoney", False)),
                        "quote_type": None,
                        "timestamp": datetime.utcnow().isoformat(),
                        "source": self.name,
                    }
                )
        return {
            "symbol": symbol,
            "source": self.name,
            "expiration": expiration,
            "contracts": rows,
            "timestamp": datetime.utcnow().isoformat(),
            "quote_type": None,
        }

    def get_option_chain(self, symbol: str, expiration: dict[str, Any] | str, **kwargs) -> dict[str, Any]:
        sym = self._norm_symbol(symbol)
        exp = expiration.get("date") if isinstance(expiration, dict) else str(expiration)
        cache_path = self._cache_path(self._safe_name("chain", sym, exp))
        cached = self._read_json(cache_path)
        if self.cache_enabled and cached and self._fresh(cache_path, self.chain_ttl):
            return cached

        try:
            rate_limit(self.name)
            ticker = yf.Ticker(sym)
            chain = ticker.option_chain(exp)
            data = self._chain_from_frames(sym, exp, chain.calls.copy(), chain.puts.copy())
            self._write_json(cache_path, data)
            return data
        except Exception as exc:
            if cached:
                return cached
            raise self._rate_limit_error(exc) from exc

    @staticmethod
    def _ratio(numerator: float, denominator: float) -> float | None:
        return None if denominator == 0 else float(numerator / denominator)

    @staticmethod
    def _bias(put_call_ratio: float | None) -> str:
        if put_call_ratio is None:
            return "NEUTRAL"
        if put_call_ratio < 0.70:
            return "BULLISH"
        if put_call_ratio <= 1.20:
            return "NEUTRAL"
        if put_call_ratio <= 1.80:
            return "BEARISH"
        return "EXTREME_PUT_HEAVY"

    def _aggregate_sentiment(
        self,
        symbol: str,
        contracts: list[dict[str, Any]],
        expirations: list[dict[str, Any]],
    ) -> dict[str, Any]:
        calls = [c for c in contracts if c.get("type") == "CALL"]
        puts = [c for c in contracts if c.get("type") == "PUT"]
        call_volume = sum(int(c.get("volume", 0) or 0) for c in calls)
        put_volume = sum(int(c.get("volume", 0) or 0) for c in puts)
        call_oi = sum(int(c.get("open_interest", 0) or 0) for c in calls)
        put_oi = sum(int(c.get("open_interest", 0) or 0) for c in puts)
        pcr = self._ratio(put_volume, call_volume)
        return {
            "symbol": symbol,
            "source": self.name,
            "provider": self.name,
            "derived_from": "ranked_contracts",
            "expirations_checked": [e.get("date") for e in expirations],
            "call_volume": call_volume,
            "put_volume": put_volume,
            "put_call_ratio": pcr,
            "call_put_ratio": self._ratio(call_volume, put_volume),
            "call_open_interest": call_oi,
            "put_open_interest": put_oi,
            "put_call_oi_ratio": self._ratio(put_oi, call_oi),
            "bias": self._bias(pcr),
        }

    def get_options_ratios(self, symbol: str, expirations_to_check: int = 3) -> dict[str, Any]:
        sym = self._norm_symbol(symbol)
        cache_path = self._cache_path(self._safe_name("ratios", sym, expirations_to_check))
        cached = self._read_json(cache_path)
        today = central_today()
        if (
            self.cache_enabled
            and cached
            and self._fresh(cache_path, self.ratios_ttl)
            and cached.get("generated_for_date") == today.isoformat()
        ):
            return cached

        quote = self.get_quote(sym)
        underlying_price = float(quote.get("price") or 0.0)
        quote_type = quote.get("quote_type")
        quote_timestamp = quote.get("timestamp")
        expirations = self.get_option_expirations(sym)[:expirations_to_check]
        all_contracts: list[dict[str, Any]] = []
        for exp_obj in expirations:
            chain = self.get_option_chain(sym, exp_obj)
            all_contracts.extend(chain.get("contracts", []))

        positioning = build_option_positioning(
            symbol=sym,
            contracts=all_contracts,
            provider=self.name,
            underlying_price=underlying_price if underlying_price > 0 else None,
            quote_type=quote_type,
            quote_timestamp=quote_timestamp,
            market_session=get_market_session(),
            selected_expiration=expirations[0].get("date") if expirations else None,
        )
        ratios = []
        for row in positioning.get("ratios") or []:
            metrics = row.get("value") or {}
            ratios.append(
                {
                    "expiration": row.get("expiration"),
                    "call_volume": metrics.get("call_volume", 0),
                    "put_volume": metrics.get("put_volume", 0),
                    "put_call_ratio": metrics.get("put_call_volume_ratio"),
                    "call_put_ratio": metrics.get("call_put_volume_ratio"),
                    "call_open_interest": metrics.get("call_open_interest", 0),
                    "put_open_interest": metrics.get("put_open_interest", 0),
                    "put_call_oi_ratio": metrics.get("put_call_open_interest_ratio"),
                    "call_put_oi_ratio": metrics.get("call_put_open_interest_ratio"),
                    "call_estimated_premium": metrics.get("call_estimated_premium"),
                    "put_estimated_premium": metrics.get("put_estimated_premium"),
                    "put_call_premium_ratio": metrics.get("put_call_premium_ratio"),
                    "call_put_premium_ratio": metrics.get("call_put_premium_ratio"),
                    "bias": self._bias(metrics.get("put_call_volume_ratio")),
                    "source": self.name,
                    "quote_type": quote_type,
                    "session_status": row.get("session_status"),
                    "session_label": row.get("session_label"),
                    "strike_scope": row.get("strike_scope"),
                    "calculation_type": row.get("calculation_type"),
                    "data_confidence": row.get("data_confidence"),
                    "value": metrics,
                }
            )

        payload = {
            "symbol": sym,
            "source": self.name,
            "provider": self.name,
            "timestamp": quote_timestamp or datetime.utcnow().isoformat(),
            "expirations_checked": [e.get("date") for e in expirations],
            "ratios": ratios,
            "aggregate": {
                "call_volume": int((positioning.get("scopes") or {}).get("overall", {}).get("value", {}).get("call_volume", 0) or 0),
                "put_volume": int((positioning.get("scopes") or {}).get("overall", {}).get("value", {}).get("put_volume", 0) or 0),
                "put_call_ratio": (positioning.get("scopes") or {}).get("overall", {}).get("value", {}).get("put_call_volume_ratio"),
                "call_put_ratio": (positioning.get("scopes") or {}).get("overall", {}).get("value", {}).get("call_put_volume_ratio"),
                "call_open_interest": int((positioning.get("scopes") or {}).get("overall", {}).get("value", {}).get("call_open_interest", 0) or 0),
                "put_open_interest": int((positioning.get("scopes") or {}).get("overall", {}).get("value", {}).get("put_open_interest", 0) or 0),
                "put_call_oi_ratio": (positioning.get("scopes") or {}).get("overall", {}).get("value", {}).get("put_call_open_interest_ratio"),
                "call_put_oi_ratio": (positioning.get("scopes") or {}).get("overall", {}).get("value", {}).get("call_put_open_interest_ratio"),
                "put_call_premium_ratio": (positioning.get("scopes") or {}).get("overall", {}).get("value", {}).get("put_call_premium_ratio"),
                "call_put_premium_ratio": (positioning.get("scopes") or {}).get("overall", {}).get("value", {}).get("call_put_premium_ratio"),
                "bias": self._bias((positioning.get("scopes") or {}).get("overall", {}).get("value", {}).get("put_call_volume_ratio")),
            },
            "quote_type": quote_type,
            "quote_timestamp": quote_timestamp,
            "underlying_price": underlying_price,
            "positioning": positioning,
            "generated_for_date": today.isoformat(),
            "warning": None,
            "warnings": [],
        }
        self._write_json(cache_path, payload)
        return payload

    @staticmethod
    def _spread_pct(bid: float, ask: float) -> float | None:
        return spread_pct(bid, ask)

    def _score_contract(self, contract: dict[str, Any], underlying_price: float, preferred_delta_min: float, preferred_delta_max: float) -> tuple[float, str]:
        bid = float(contract.get("bid", 0) or 0)
        ask = float(contract.get("ask", 0) or 0)
        vol = int(contract.get("volume", 0) or 0)
        oi = int(contract.get("open_interest", 0) or 0)
        strike = float(contract.get("strike", 0) or 0)
        spread = self._spread_pct(bid, ask)

        score = 0.0
        if bid > 0 and ask > 0 and spread is not None:
            if spread <= 15:
                score += 30
            elif spread <= 25:
                score += 18

        score += min(25, np.log10(max(vol, 1)) * 8)
        score += min(25, np.log10(max(oi, 1)) * 8)

        moneyness = abs((strike - max(underlying_price, 1e-9)) / max(underlying_price, 1e-9)) * 100
        if moneyness <= 2:
            score += 15
        elif moneyness <= 5:
            score += 8

        delta = contract.get("delta")
        if delta is not None:
            d = abs(float(delta))
            if preferred_delta_min <= d <= preferred_delta_max:
                score += 8

        if score >= 75:
            grade = "A"
        elif score >= 55:
            grade = "B"
        elif score >= 35:
            grade = "C"
        else:
            grade = "D"
        return score, grade

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
        sym = self._norm_symbol(symbol)
        cache_path = self._cache_path(self._safe_name("ranked", sym, expirations_to_check))
        cached = self._read_json(cache_path)

        options_cfg = self.config.get("options", {})
        preferred_delta_min = float(options_cfg.get("preferred_delta_min", 0.30))
        preferred_delta_max = float(options_cfg.get("preferred_delta_max", 0.55))
        min_volume = int(min_volume if min_volume is not None else options_cfg.get("min_volume", 1))
        min_open_interest = int(min_open_interest if min_open_interest is not None else options_cfg.get("min_open_interest", 1))
        max_spread_pct = float(max_spread_pct if max_spread_pct is not None else options_cfg.get("max_spread_pct", 15))
        max_quote_age_seconds = int(options_cfg.get("max_quote_age_seconds", 300))
        recommended_max_spread_pct = float(options_cfg.get("recommended_max_spread_pct", 5))
        today = central_today()
        active_filters = filter_signature(min_volume, min_open_interest, max_spread_pct)
        context_signature = {
            "chart_side": (chart_signal or {}).get("side"),
            "chart_grade": (chart_signal or {}).get("grade"),
            "chart_score": (chart_signal or {}).get("score"),
            "sentiment_bias": (options_sentiment or {}).get("bias"),
            "sentiment_put_call_ratio": (options_sentiment or {}).get("put_call_ratio"),
        }
        if (
            self.cache_enabled
            and cached
            and self._fresh(cache_path, self.contracts_ttl)
            and cached.get("filter_version") == FILTER_VERSION
            and cached.get("filters") == active_filters
            and cached.get("context_signature") == context_signature
            and cached.get("generated_for_date") == today.isoformat()
        ):
            return cached

        quote = self.get_quote(sym)
        underlying_price = float(quote.get("price") or 0.0)
        quote_type = quote.get("quote_type")
        quote_timestamp = quote.get("timestamp")

        expirations = self.get_option_expirations(sym)[:expirations_to_check]
        contracts: list[dict[str, Any]] = []
        for exp in expirations:
            chain = self.get_option_chain(sym, exp)
            contracts.extend(chain.get("contracts", []))

        raw_contract_count = len(contracts)
        scoring_sentiment = options_sentiment or self._aggregate_sentiment(sym, contracts, expirations)
        positioning = build_option_positioning(
            symbol=sym,
            contracts=contracts,
            provider=self.name,
            underlying_price=underlying_price if underlying_price > 0 else None,
            quote_type=quote_type,
            quote_timestamp=quote_timestamp,
            market_session=get_market_session(),
            selected_expiration=expirations[0].get("date") if expirations else None,
        )
        contracts, filtered_counts = filter_contracts(
            contracts,
            min_volume=min_volume,
            min_open_interest=min_open_interest,
            max_spread_pct=max_spread_pct,
            today=today,
        )

        for c in contracts:
            enrich_contract(
                c,
                underlying_price=underlying_price,
                quote_type=quote_type,
                quote_timestamp=quote_timestamp,
                today=today,
                preferred_delta_min=preferred_delta_min,
                preferred_delta_max=preferred_delta_max,
                chart_signal=chart_signal,
                options_sentiment=scoring_sentiment,
                options_positioning=positioning,
                max_quote_age_seconds=max_quote_age_seconds,
                recommended_max_spread_pct=recommended_max_spread_pct,
                minimum_volume=min_volume,
            )

        ranked = sorted(contracts, key=lambda x: x.get("score", 0), reverse=True)
        calls = [c for c in ranked if c.get("type") == "CALL"]
        puts = [c for c in ranked if c.get("type") == "PUT"]
        warnings: list[str] = []
        filtered_out_count = raw_contract_count - len(contracts)
        if filtered_out_count:
            warnings.append(f"Filtered out {filtered_out_count} contracts that failed expiration/liquidity/spread rules")
        if not calls:
            warnings.append("No call candidates passed option filters")
        if not puts:
            warnings.append("No put candidates passed option filters")

        payload = {
            "symbol": sym,
            "source": self.name,
            "provider": self.name,
            "timestamp": datetime.utcnow().isoformat(),
            "quote_type": quote_type,
            "quote_timestamp": quote_timestamp,
            "underlying_price": underlying_price,
            "expirations_checked": [e.get("date") for e in expirations],
            "calls": calls[:25],
            "puts": puts[:25],
            "filters": active_filters,
            "chart_signal": chart_signal,
            "options_sentiment": scoring_sentiment,
            "options_positioning": positioning,
            "context_signature": context_signature,
            "recommended_max_spread_pct": recommended_max_spread_pct,
            "max_quote_age_seconds": max_quote_age_seconds,
            "filtered_out_count": filtered_out_count,
            "filtered_counts": filtered_counts,
            "filter_version": FILTER_VERSION,
            "generated_for_date": today.isoformat(),
            "warning": " | ".join(warnings) if warnings else None,
            "warnings": warnings,
        }
        self._write_json(cache_path, payload)
        return payload
