from __future__ import annotations

import json
import os
import time
from datetime import datetime
from pathlib import Path
from typing import Any

import numpy as np

from ..auth import etrade_auth
from ..cache_policy import market_aware_ttl
from ..market_session import get_market_session
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
from .rate_limiter import rate_limit


class ETradeProvider(BaseMarketProvider):
    name = "etrade"

    def __init__(self, config: dict[str, Any]) -> None:
        self.config = config or {}
        cache_cfg = self.config.get("cache", {})
        data_cfg = self.config.get("data", {})
        self.cache_enabled = bool(data_cfg.get("cache_enabled", True))
        self.cache_dir = Path(os.getenv("CACHE_DIR", "/app/data/cache"))
        self.cache_dir.mkdir(parents=True, exist_ok=True)
        self.quotes_ttl = int(cache_cfg.get("quotes_ttl_seconds", 10))
        self.exp_ttl = int(cache_cfg.get("option_expirations_ttl_seconds", 86400))
        self.chain_ttl = int(cache_cfg.get("option_chains_ttl_seconds", 60))
        self.ratios_ttl = int(cache_cfg.get("option_chains_ttl_seconds", 60))
        self.contracts_ttl = int(cache_cfg.get("option_chains_ttl_seconds", 60))

    def _norm_symbol(self, symbol: str) -> str:
        return symbol.strip().upper()

    def _safe_name(self, *parts: Any) -> str:
        return "_".join(str(p) for p in parts).replace("/", "_").replace(" ", "_")

    def _cache_path(self, key: str) -> Path:
        return self.cache_dir / f"etrade_{key}.json"

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

    def _request(self, endpoint: str, params: dict[str, Any] | None = None) -> dict[str, Any]:
        if not etrade_auth.enabled():
            raise ProviderError("E*TRADE disabled", provider=self.name)
        if not etrade_auth.configured():
            raise ProviderError("E*TRADE credentials missing", provider=self.name)
        if not etrade_auth.is_connected():
            raise ProviderError("E*TRADE not connected", provider=self.name)

        session = etrade_auth.signed_session()
        url = f"{etrade_auth.base_url()}{endpoint}"
        rate_limit(self.name)
        timeout = int(self.config.get("etrade", {}).get("request_timeout_seconds", 8) or 8)
        response = session.get(
            url,
            params=params or {},
            headers={"Accept": "application/json"},
            timeout=timeout,
        )

        if response.status_code == 401:
            raise ProviderError("E*TRADE authorization failed", provider=self.name)
        if response.status_code == 429:
            raise ProviderError("E*TRADE rate limited", rate_limited=True, provider=self.name)
        if response.status_code >= 400:
            raise ProviderError(f"E*TRADE API error {response.status_code}", provider=self.name)

        try:
            return response.json()
        except Exception as exc:
            body = (response.text or "").strip()
            preview = body[:120].replace("\n", " ")
            raise ProviderError(
                f"E*TRADE invalid JSON response: {exc}. body={preview}",
                provider=self.name,
            ) from exc

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

    def _extract_quote_data(self, payload: dict[str, Any]) -> dict[str, Any]:
        quote_response = payload.get("QuoteResponse") or payload.get("quoteResponse") or {}
        quote_data = quote_response.get("QuoteData") or quote_response.get("quoteData") or []
        if isinstance(quote_data, dict):
            quote_data = [quote_data]
        row = quote_data[0] if quote_data else {}
        all_data = row.get("All") or row.get("all") or {}

        price = all_data.get("lastTrade")
        if price is None:
            price = all_data.get("lastTradePrice")
        if price is None:
            price = all_data.get("ask")
        if price is None:
            price = 0

        quote_type = row.get("quoteStatus") or row.get("quoteType")
        if isinstance(quote_type, str):
            quote_type = quote_type.upper()

        return {
            "symbol": (row.get("Product") or {}).get("symbol") or row.get("symbol"),
            "price": float(price or 0),
            "timestamp": datetime.utcnow().isoformat(),
            "quote_type": quote_type,
            "source": self.name,
        }

    def get_quote(self, symbol: str) -> dict[str, Any]:
        sym = self._norm_symbol(symbol)
        cache_path = self._cache_path(self._safe_name("quote", sym))
        cached = self._read_json(cache_path)
        if self.cache_enabled and cached and self._fresh(cache_path, self.quotes_ttl):
            return cached
        try:
            payload = self._request(f"/v1/market/quote/{sym}.json")
            quote = self._extract_quote_data(payload)
            quote["symbol"] = sym
            self._write_json(cache_path, quote)
            return quote
        except Exception as exc:
            if cached:
                return cached
            if isinstance(exc, ProviderError):
                raise
            raise ProviderError(f"E*TRADE quote error: {exc}", provider=self.name) from exc

    def get_candles(self, symbol: str, interval: str, period: str) -> list[dict[str, Any]]:
        raise ProviderError("E*TRADE candles not implemented in this app", provider=self.name)

    def _normalize_expiration(self, item: dict[str, Any]) -> dict[str, Any]:
        year = item.get("year") or item.get("expiryYear")
        month = item.get("month") or item.get("expiryMonth")
        day = item.get("day") or item.get("expiryDay")
        if year and month and day:
            date = f"{int(year):04d}-{int(month):02d}-{int(day):02d}"
        else:
            date = str(item.get("date") or "")
        return {
            "year": int(year) if year is not None else None,
            "month": int(month) if month is not None else None,
            "day": int(day) if day is not None else None,
            "date": date,
            "expiryType": item.get("expiryType") or item.get("type"),
            "source": self.name,
        }

    def get_option_expirations(self, symbol: str) -> list[dict[str, Any]]:
        sym = self._norm_symbol(symbol)
        cache_path = self._cache_path(self._safe_name("expirations", sym))
        cached = self._read_json(cache_path)
        if self.cache_enabled and cached and self._fresh(cache_path, self.exp_ttl):
            return current_expirations(list(cached.get("expirations", [])))

        try:
            payload = self._request("/v1/market/optionexpiredate.json", {"symbol": sym, "expiryType": "ALL"})
            root = payload.get("OptionExpireDateResponse") or payload.get("optionExpireDateResponse") or {}
            items = root.get("ExpirationDate") or root.get("expirationDate") or []
            if isinstance(items, dict):
                items = [items]
            expirations = current_expirations([self._normalize_expiration(i) for i in items])
            data = {"symbol": sym, "expirations": expirations, "timestamp": datetime.utcnow().isoformat(), "source": self.name}
            self._write_json(cache_path, data)
            return expirations
        except Exception as exc:
            if cached:
                return current_expirations(list(cached.get("expirations", [])))
            if isinstance(exc, ProviderError):
                raise
            raise ProviderError(f"E*TRADE expirations error: {exc}", provider=self.name) from exc

    def _extract_option_pairs(self, payload: dict[str, Any]) -> list[dict[str, Any]]:
        root = payload.get("OptionChainResponse") or payload.get("optionChainResponse") or {}
        pair_list = root.get("OptionPair") or root.get("optionPair") or []
        if isinstance(pair_list, dict):
            pair_list = [pair_list]
        return pair_list

    def _contract_from_leg(
        self,
        leg: dict[str, Any],
        option_type: str,
        quote_type: str | None,
        fallback_expiration: str | None = None,
    ) -> dict[str, Any]:
        product = leg.get("Product") or leg.get("product") or {}
        exp_year = product.get("expiryYear")
        exp_month = product.get("expiryMonth")
        exp_day = product.get("expiryDay")
        expiration = None
        if exp_year and exp_month and exp_day:
            expiration = f"{int(exp_year):04d}-{int(exp_month):02d}-{int(exp_day):02d}"
        elif product.get("expiryDate"):
            raw = str(product.get("expiryDate"))
            digits = "".join(ch for ch in raw if ch.isdigit())
            if len(digits) == 8:
                expiration = f"{digits[0:4]}-{digits[4:6]}-{digits[6:8]}"

        if not expiration:
            expiration = fallback_expiration

        symbol = product.get("symbol")
        strike = leg.get("strikePrice") or product.get("strikePrice") or 0

        return {
            "contract_symbol": leg.get("osiKey") or leg.get("optionSymbol") or leg.get("displaySymbol") or leg.get("optionRootSymbol") or f"{symbol}_{expiration}_{strike}_{option_type}",
            "osi_key": leg.get("osiKey") or leg.get("optionSymbol") or None,
            "expiration": expiration,
            "strike": float(strike or 0),
            "type": option_type,
            "bid": float(leg.get("bid") or 0),
            "ask": float(leg.get("ask") or 0),
            "last": float(leg.get("lastPrice") or leg.get("lastTrade") or 0),
            "volume": int(leg.get("volume") or 0),
            "open_interest": int(leg.get("openInterest") or 0),
            "implied_volatility": float(leg.get("iv") or leg.get("impliedVolatility") or 0),
            "delta": (float(leg.get("delta")) if leg.get("delta") is not None else None),
            "gamma": (float(leg.get("gamma")) if leg.get("gamma") is not None else None),
            "theta": (float(leg.get("theta")) if leg.get("theta") is not None else None),
            "vega": (float(leg.get("vega")) if leg.get("vega") is not None else None),
            "rho": (float(leg.get("rho")) if leg.get("rho") is not None else None),
            "in_the_money": bool(leg.get("inTheMoney", False)),
            "quote_type": quote_type,
            "timestamp": datetime.utcnow().isoformat(),
            "source": self.name,
        }

    def get_option_chain(self, symbol: str, expiration: dict[str, Any] | str, **kwargs) -> dict[str, Any]:
        sym = self._norm_symbol(symbol)
        if isinstance(expiration, dict):
            exp_date = expiration.get("date")
            year = expiration.get("year")
            month = expiration.get("month")
            day = expiration.get("day")
        else:
            exp_date = str(expiration)
            year, month, day = [int(x) for x in exp_date.split("-")]

        cache_parts = ["chain", sym, exp_date]
        if kwargs.get("noOfStrikes") is not None:
            cache_parts.extend(["strikes", kwargs.get("noOfStrikes")])
        if kwargs.get("strikePriceNear") is not None:
            cache_parts.extend(["near", kwargs.get("strikePriceNear")])
        cache_path = self._cache_path(self._safe_name(*cache_parts))
        cached = self._read_json(cache_path)
        if self.cache_enabled and cached and self._fresh(cache_path, self.chain_ttl):
            return cached

        params = {
            "symbol": sym,
            "chainType": "CALLPUT",
            "includeWeekly": "true",
            "skipAdjusted": "true",
            "optionCategory": "ALL",
            "priceType": "ALL",
        }
        if year:
            params["expiryYear"] = year
        if month:
            params["expiryMonth"] = month
        if day:
            params["expiryDay"] = day

        for key in ["noOfStrikes", "strikePriceNear"]:
            if key in kwargs and kwargs[key] is not None:
                params[key] = kwargs[key]

        try:
            payload = self._request("/v1/market/optionchains.json", params)
            root = payload.get("OptionChainResponse") or payload.get("optionChainResponse") or {}
            quote_type = root.get("quoteType")
            pairs = self._extract_option_pairs(payload)
            contracts: list[dict[str, Any]] = []
            for pair in pairs:
                call = pair.get("Call") or pair.get("call")
                put = pair.get("Put") or pair.get("put")
                if call:
                    contracts.append(self._contract_from_leg(call, "CALL", quote_type, exp_date))
                if put:
                    contracts.append(self._contract_from_leg(put, "PUT", quote_type, exp_date))

            data = {
                "symbol": sym,
                "source": self.name,
                "provider": self.name,
                "expiration": exp_date,
                "contracts": contracts,
                "quote_type": quote_type,
                "timestamp": datetime.utcnow().isoformat(),
            }
            self._write_json(cache_path, data)
            return data
        except Exception as exc:
            if cached:
                return cached
            if isinstance(exc, ProviderError):
                raise
            raise ProviderError(f"E*TRADE option chain error: {exc}", provider=self.name) from exc

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
        contracts_by_expiration: list[dict[str, Any]] = []

        for exp in expirations:
            chain = self.get_option_chain(sym, exp)
            contracts_by_expiration.extend(chain.get("contracts", []))

        positioning = build_option_positioning(
            symbol=sym,
            contracts=contracts_by_expiration,
            provider=self.name,
            underlying_price=underlying_price if underlying_price > 0 else None,
            quote_type=quote_type,
            quote_timestamp=quote_timestamp,
            market_session=get_market_session(),
            selected_expiration=expirations[0].get("date") if expirations else None,
        )
        overall = (positioning.get("scopes") or {}).get("overall", {})
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
                "call_volume": int((overall.get("value") or {}).get("call_volume", 0) or 0),
                "put_volume": int((overall.get("value") or {}).get("put_volume", 0) or 0),
                "put_call_ratio": (overall.get("value") or {}).get("put_call_volume_ratio"),
                "call_put_ratio": (overall.get("value") or {}).get("call_put_volume_ratio"),
                "call_open_interest": int((overall.get("value") or {}).get("call_open_interest", 0) or 0),
                "put_open_interest": int((overall.get("value") or {}).get("put_open_interest", 0) or 0),
                "put_call_oi_ratio": (overall.get("value") or {}).get("put_call_open_interest_ratio"),
                "call_put_oi_ratio": (overall.get("value") or {}).get("call_put_open_interest_ratio"),
                "put_call_premium_ratio": (overall.get("value") or {}).get("put_call_premium_ratio"),
                "call_put_premium_ratio": (overall.get("value") or {}).get("call_put_premium_ratio"),
                "bias": self._bias((overall.get("value") or {}).get("put_call_volume_ratio")),
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

    def _contract_grade(self, contract: dict[str, Any], underlying: float, preferred_delta_min: float, preferred_delta_max: float) -> tuple[float, str]:
        bid = float(contract.get("bid", 0) or 0)
        ask = float(contract.get("ask", 0) or 0)
        spread = self._spread_pct(bid, ask)
        volume = int(contract.get("volume", 0) or 0)
        oi = int(contract.get("open_interest", 0) or 0)
        strike = float(contract.get("strike", 0) or 0)
        delta = contract.get("delta")

        score = 0.0
        if bid > 0 and ask > 0 and spread is not None:
            if spread <= 10:
                score += 30
            elif spread <= 20:
                score += 18

        score += min(25, np.log10(max(volume, 1)) * 8)
        score += min(25, np.log10(max(oi, 1)) * 8)

        moneyness_dist_pct = abs((strike - max(underlying, 1e-9)) / max(underlying, 1e-9)) * 100
        if moneyness_dist_pct <= 2:
            score += 15
        elif moneyness_dist_pct <= 5:
            score += 8

        if delta is not None:
            abs_delta = abs(float(delta))
            if preferred_delta_min <= abs_delta <= preferred_delta_max:
                score += 10

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
        underlying = float(quote.get("price") or 0)
        quote_type = "SANDBOX" if bool(self.config.get("etrade", {}).get("sandbox", False)) else quote.get("quote_type")
        quote_timestamp = quote.get("timestamp")

        expirations = self.get_option_expirations(sym)[:expirations_to_check]
        strike_count = int(options_cfg.get("option_chain_strike_count", 20) or 0)
        chain_kwargs: dict[str, Any] = {}
        if strike_count > 0:
            chain_kwargs["noOfStrikes"] = strike_count
        if underlying > 0:
            chain_kwargs["strikePriceNear"] = round(underlying, 2)

        contracts: list[dict[str, Any]] = []
        for exp in expirations:
            chain = self.get_option_chain(sym, exp, **chain_kwargs)
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
                underlying_price=underlying,
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

        ranked = sorted(contracts, key=lambda c: c.get("score", 0), reverse=True)
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
            "underlying_price": underlying,
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
