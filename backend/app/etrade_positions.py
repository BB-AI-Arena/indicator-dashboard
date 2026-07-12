from __future__ import annotations

import hashlib
import json
import os
import re
import time
import threading
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Any

import pandas as pd
import requests

from .auth import etrade_auth
from .cache_policy import market_aware_ttl
from .config import config_manager
from .data_provider import fetch_candles
from .db import SessionLocal
from .exit_management import build_exit_plan, evaluate_exit_management
from .indicators import apply_indicators
from .money_flow import build_money_flow
from .history import get_candles_from_sql
from .news_catalyst import build_news_catalyst_impact
from .options import calculate_ratios
from .providers.base import ProviderError
from .providers import provider_factory
from .providers.option_filters import central_today, parse_expiration_date, spread_pct
from .providers.rate_limiter import call_with_rate_limit
from .models import BrokerageAccount, BrokerageExitAuditEvent, BrokeragePosition


BAD_QUOTE_TYPES = {"CLOSING", "DELAYED", "SANDBOX"}
OPENAI_RESPONSES_URL = "https://api.openai.com/v1/responses"
POSITION_CACHE_TTL_SECONDS = 60
POSITION_ADVICE_MODEL_DEFAULT = "gpt-5.6"
_POSITION_REFRESH_LOCK = threading.Lock()
_POSITION_REFRESH_ACTIVE = False
_POSITION_REFRESH_STARTED_AT: str | None = None
_POSITION_REFRESH_LAST_ERROR: str | None = None


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _safe_float(value: Any, default: float | None = None) -> float | None:
    try:
        if value is None:
            return default
        number = float(value)
        if number != number:  # NaN
            return default
        if number in (float("inf"), float("-inf")):
            return default
        return number
    except Exception:
        return default


def _safe_int(value: Any, default: int | None = None) -> int | None:
    try:
        if value is None:
            return default
        return int(float(value))
    except Exception:
        return default


def _safe_text(value: Any) -> str:
    return str(value or "").strip()


def _as_list(value: Any) -> list[Any]:
    if value is None:
        return []
    if isinstance(value, list):
        return value
    return [value]


def _first_value(source: dict[str, Any] | None, *keys: str) -> Any:
    if not isinstance(source, dict):
        return None
    for key in keys:
        if key in source and source.get(key) not in (None, ""):
            return source.get(key)
    return None


def _nested_view(source: dict[str, Any], *names: str) -> dict[str, Any]:
    if not isinstance(source, dict):
        return {}
    for name in names:
        for key in {name, name.lower(), name.capitalize()}:
            candidate = source.get(key)
            if isinstance(candidate, dict):
                return candidate
    return {}


def _first_value_from(sources: list[dict[str, Any]], *keys: str) -> Any:
    for source in sources:
        value = _first_value(source, *keys)
        if value not in (None, ""):
            return value
    return None


def _read_json(path: Path) -> dict[str, Any] | None:
    if not path.exists():
        return None
    try:
        return json.loads(path.read_text())
    except Exception:
        return None


def _write_json(path: Path, payload: dict[str, Any]) -> None:
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(payload))
    except Exception:
        return


def _cached_missing_key_warning_is_stale(payload: dict[str, Any] | None) -> bool:
    if not payload or not os.getenv("FINNHUB_API_KEY", "").strip():
        return False
    try:
        text = json.dumps(payload)
    except Exception:
        return False
    return "Finnhub API key is missing (set FINNHUB_API_KEY)" in text


def _sanitize_cached_ai_status(payload: dict[str, Any] | None) -> dict[str, Any] | None:
    """Remove raw transport details from snapshots created by older builds."""
    if not isinstance(payload, dict):
        return payload
    summary = payload.get("summary")
    ai = payload.get("ai")
    for container in (summary, ai):
        if not isinstance(container, dict):
            continue
        reason = str(container.get("ai_blocking_reason" if container is summary else "blocking_reason") or "")
        if any(marker in reason.lower() for marker in ("httpsconnectionpool", "read timed out", "connect timeout", "read timeout")):
            safe = _ai_failure_message(RuntimeError(reason))
            if container is summary:
                container["ai_blocking_reason"] = safe
            else:
                container["blocking_reason"] = safe
    return payload


def _queue_positions_refresh(
    *,
    cache_path: Path,
    ttl_seconds: int,
    market_session: dict[str, Any] | None = None,
) -> bool:
    global _POSITION_REFRESH_ACTIVE, _POSITION_REFRESH_STARTED_AT, _POSITION_REFRESH_LAST_ERROR

    with _POSITION_REFRESH_LOCK:
        if _POSITION_REFRESH_ACTIVE:
            return False
        _POSITION_REFRESH_ACTIVE = True
        _POSITION_REFRESH_STARTED_AT = _now_iso()
        _POSITION_REFRESH_LAST_ERROR = None

    def _worker() -> None:
        global _POSITION_REFRESH_ACTIVE, _POSITION_REFRESH_LAST_ERROR
        try:
            snapshot = _build_positions_snapshot(market_session=market_session)
            snapshot["cache"] = {
                "hit": False,
                "ttl_seconds": ttl_seconds,
                "refreshed_at": _now_iso(),
            }
            _write_json(cache_path, snapshot)
            _POSITION_REFRESH_LAST_ERROR = None
        except Exception as exc:
            _POSITION_REFRESH_LAST_ERROR = str(exc)
        finally:
            with _POSITION_REFRESH_LOCK:
                _POSITION_REFRESH_ACTIVE = False

    threading.Thread(target=_worker, name="etrade-positions-refresh", daemon=True).start()
    return True


def _fresh(path: Path, ttl_seconds: int) -> bool:
    try:
        return path.exists() and (time.time() - path.stat().st_mtime) <= ttl_seconds
    except Exception:
        return False


def _cache_dir() -> Path:
    cache_dir = Path(os.getenv("CACHE_DIR", "/app/data/cache"))
    cache_dir.mkdir(parents=True, exist_ok=True)
    return cache_dir


def _cache_path(name: str) -> Path:
    return _cache_dir() / name


def _mask_account_id(account_id: str | None) -> str:
    value = _safe_text(account_id)
    if len(value) <= 4:
        return value
    return value[-4:]


def _parse_timestamp(value: Any) -> datetime | None:
    if value is None:
        return None
    if isinstance(value, datetime):
        parsed = value
    elif isinstance(value, (int, float)):
        try:
            parsed = datetime.fromtimestamp(float(value), tz=timezone.utc)
        except Exception:
            return None
    else:
        text = _safe_text(value)
        if not text:
            return None
        if text.isdigit():
            try:
                parsed = datetime.fromtimestamp(float(text), tz=timezone.utc)
            except Exception:
                parsed = None
        else:
            parsed = None
        for candidate in (text, text.replace("Z", "+00:00")):
            if parsed is not None:
                break
            try:
                parsed = datetime.fromisoformat(candidate)
                break
            except Exception:
                parsed = None
        if parsed is None:
            return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def _request_json(endpoint: str, params: dict[str, Any] | None = None) -> dict[str, Any]:
    if not etrade_auth.enabled():
        raise ProviderError("E*TRADE disabled", provider="etrade")
    if not etrade_auth.configured():
        raise ProviderError("E*TRADE credentials missing", provider="etrade")
    if not etrade_auth.is_connected():
        raise ProviderError("E*TRADE not connected", provider="etrade")

    session = etrade_auth.signed_session()
    url = f"{etrade_auth.base_url()}{endpoint}"
    timeout = int(config_manager.get("etrade", "request_timeout_seconds", default=8) or 8)
    response = session.get(
        url,
        params=params or {},
        headers={"Accept": "application/json"},
        timeout=timeout,
    )

    if response.status_code == 401:
        raise ProviderError("E*TRADE authorization failed", provider="etrade")
    if response.status_code == 429:
        raise ProviderError("E*TRADE rate limited", rate_limited=True, provider="etrade")
    if response.status_code >= 400:
        raise ProviderError(f"E*TRADE API error {response.status_code}", provider="etrade")

    try:
        return response.json()
    except Exception as exc:
        preview = (response.text or "")[:160].replace("\n", " ")
        raise ProviderError(
            f"E*TRADE invalid JSON response: {exc}. body={preview}",
            provider="etrade",
        ) from exc


def _call_etrade(endpoint: str, params: dict[str, Any] | None = None, symbol: str | None = None) -> dict[str, Any]:
    return call_with_rate_limit("etrade", symbol, endpoint, _request_json, endpoint, params)


def _fetch_portfolio_page(account_id_key: str, page_number: int | None = None) -> dict[str, Any]:
    params: dict[str, Any] = {
        "count": 100,
        "view": "COMPLETE",
        "totalsRequired": "true",
        "lotsRequired": "false",
        "sortOrder": "DESC",
    }
    if page_number is not None and page_number > 0:
        params["pageNumber"] = page_number
    return _call_etrade(f"/v1/accounts/{account_id_key}/portfolio.json", params=params, symbol=account_id_key)


def _extract_accounts(payload: dict[str, Any]) -> list[dict[str, Any]]:
    root = payload.get("AccountListResponse") or payload.get("accountListResponse") or payload
    accounts = (
        root.get("Accounts")
        or root.get("accounts")
        or root.get("Account")
        or root.get("account")
        or []
    )
    if isinstance(accounts, dict):
        accounts = accounts.get("Account") or accounts.get("account") or accounts.get("Portfolio") or accounts.get("portfolio") or [accounts]
    return [item for item in _as_list(accounts) if isinstance(item, dict)]


def _extract_portfolios(payload: dict[str, Any]) -> list[dict[str, Any]]:
    root = payload.get("PortfolioResponse") or payload.get("portfolioResponse") or payload
    portfolios = (
        root.get("AccountPortfolio")
        or root.get("accountPortfolio")
        or root.get("Portfolio")
        or root.get("portfolio")
        or root.get("Position")
        or root.get("position")
        or []
    )
    if isinstance(portfolios, dict):
        portfolios = portfolios.get("AccountPortfolio") or portfolios.get("accountPortfolio") or portfolios.get("Position") or portfolios.get("position") or [portfolios]
    return [item for item in _as_list(portfolios) if isinstance(item, dict)]


def _extract_positions(portfolio: dict[str, Any]) -> list[dict[str, Any]]:
    candidates = [
        portfolio.get("Position"),
        portfolio.get("position"),
        portfolio.get("Positions"),
        portfolio.get("positions"),
        portfolio.get("PositionList"),
        portfolio.get("positionList"),
    ]
    for candidate in candidates:
        if isinstance(candidate, dict):
            inner = candidate.get("Position") or candidate.get("position") or candidate.get("PositionData") or candidate.get("positionData")
            if inner is not None:
                return [item for item in _as_list(inner) if isinstance(item, dict)]
        if candidate is not None:
            return [item for item in _as_list(candidate) if isinstance(item, dict)]
    return []


def _is_option_position(position: dict[str, Any]) -> bool:
    product = position.get("Product") or position.get("product") or {}
    if not isinstance(product, dict):
        product = {}
    security_type = _safe_text(_first_value(product, "securityType", "typeCode", "security_type")).upper()
    call_put = _safe_text(_first_value(product, "callPut", "call_put")).upper()
    osi_key = _safe_text(_first_value(position, "osiKey", "osi_key"))
    return bool(
        osi_key
        or call_put in {"CALL", "PUT"}
        or security_type in {"OPTN", "OPTION", "EQUITY_OPTN", "EQUITY_OPTION_ETF"}
    )


def _normalize_account(account: dict[str, Any]) -> dict[str, Any]:
    account_id = _safe_text(_first_value(account, "accountIdKey", "account_id_key", "accountId", "account_id"))
    account_name = _safe_text(
        _first_value(
            account,
            "accountDesc",
            "accountDescription",
            "accountName",
            "displayName",
            "nickname",
        )
    )
    account_type = _safe_text(_first_value(account, "accountType", "account_type", "type"))
    balance = _nested_view(account, "ComputedBalance", "computedBalance", "Balance", "balance")
    balance_sources = [account, balance]
    return {
        "account_id_key": account_id or None,
        "account_id_suffix": _mask_account_id(account_id) if account_id else None,
        "account_name": account_name or None,
        "account_type": account_type or None,
        "institution_type": _safe_text(_first_value(account, "institutionType", "institution_type")) or None,
        "account_equity": _safe_float(_first_value_from(balance_sources, "accountValue", "totalAccountValue", "netAccountValue", "equity", "marketValue")),
        "cash_balance": _safe_float(_first_value_from(balance_sources, "cashBalance", "cash", "cashAvailable", "availableCash")),
        "buying_power": _safe_float(_first_value_from(balance_sources, "buyingPower", "marginBuyingPower", "availableFunds")),
        "is_brokerage": str(account_type).upper() in {"BROKERAGE", "INDIVIDUAL", "JOINT", "MARGIN"} if account_type else None,
    }


def _parse_underlying_symbol(position: dict[str, Any], product: dict[str, Any], display_symbol: str) -> str | None:
    for source in (
        _first_value(position, "underlyingSymbol", "underlying_symbol", "symbol"),
        _first_value(product, "underlyingSymbol", "underlying_symbol", "symbol"),
    ):
        text = _safe_text(source).upper()
        if text:
            return text

    symbol = _safe_text(display_symbol).upper()
    if not symbol:
        return None
    if " " in symbol:
        return symbol.split(" ", 1)[0]
    match = re.match(r"^[A-Z][A-Z0-9\.\-]*", symbol)
    return match.group(0) if match else symbol


def _parse_expiration(position: dict[str, Any], product: dict[str, Any]) -> str | None:
    direct = _first_value(position, "expiration", "expirationDate", "expiryDate", "maturityDate")
    parsed = parse_expiration_date(direct)
    if parsed:
        return parsed.isoformat()

    year = _first_value(position, "expiryYear", "expirationYear") or _first_value(product, "expiryYear", "expirationYear")
    month = _first_value(position, "expiryMonth", "expirationMonth") or _first_value(product, "expiryMonth", "expirationMonth")
    day = _first_value(position, "expiryDay", "expirationDay") or _first_value(product, "expiryDay", "expirationDay")
    if year and month and day:
        try:
            return date(int(year), int(month), int(day)).isoformat()
        except Exception:
            return None
    return None


def _parse_contract_type(position: dict[str, Any], product: dict[str, Any], display_symbol: str) -> str:
    for source in (
        _first_value(position, "typeCode", "type", "optionType", "putCall", "callPut"),
        _first_value(product, "typeCode", "type", "optionType", "putCall", "callPut"),
    ):
        text = _safe_text(source).upper()
        if text in {"CALL", "PUT"}:
            return text
        if "CALL" in text:
            return "CALL"
        if "PUT" in text:
            return "PUT"

    symbol = _safe_text(display_symbol).upper()
    if "CALL" in symbol:
        return "CALL"
    if "PUT" in symbol:
        return "PUT"
    return ""


def _determine_direction(position: dict[str, Any], quantity: int | None) -> str:
    explicit = _safe_text(_first_value(position, "positionType", "side", "direction", "position_direction")).upper()
    if explicit in {"LONG", "SHORT"}:
        return explicit
    if quantity is not None and quantity < 0:
        return "SHORT"
    return "LONG"


def _quote_issue_warning(quote_type: str | None, quote_stale: bool, spread: float | None, volume: int | None) -> list[str]:
    warnings: list[str] = []
    if quote_type in BAD_QUOTE_TYPES:
        warnings.append(f"Quote type is {quote_type}.")
    if quote_stale:
        warnings.append("Quote is stale.")
    if spread is not None and spread > 5:
        warnings.append(f"Spread is above 5% ({spread:.2f}%).")
    if volume is not None and volume < 100:
        warnings.append(f"Volume is below 100 ({volume}).")
    return warnings


def _extract_symbol_and_price(value: Any, fallback_symbol: str | None = None) -> tuple[str | None, float | None]:
    symbol = _safe_text(fallback_symbol) or None
    price = None

    if isinstance(value, dict):
        symbol = _safe_text(
            _first_value(
                value,
                "symbol",
                "underlyingSymbol",
                "baseSymbol",
                "base_symbol",
                "displaySymbol",
                "symbolDescription",
            )
        ) or symbol
        price = _safe_float(_first_value(value, "price", "last", "lastTrade", "lastTradePrice", "mark", "close", "closePrice"))
        if price is None:
            for key in ("bid", "ask"):
                price = _safe_float(value.get(key))
                if price is not None:
                    break
    elif isinstance(value, (int, float)):
        price = _safe_float(value)
    else:
        text = _safe_text(value)
        if text:
            numeric = re.search(r"(-?\d+(?:,\d{3})*(?:\.\d+)?)", text)
            if numeric:
                price = _safe_float(numeric.group(1).replace(",", ""))
            if not symbol:
                cleaned = re.sub(r"[$\d,.\s]+", " ", text).strip()
                match = re.match(r"^[A-Z][A-Z0-9\.\-]*", cleaned.upper())
                if match:
                    symbol = match.group(0)

    return symbol, price


def _build_underlying_quote(
    *,
    symbol: str | None,
    price: float | None,
    quote_type: str | None,
    quote_timestamp: str | None,
) -> dict[str, Any] | None:
    if symbol is None and price is None:
        return None
    return {
        "symbol": symbol,
        "price": price,
        "timestamp": quote_timestamp,
        "quote_type": quote_type,
        "provider": "etrade",
        "source": "etrade",
    }


def _normalize_position(
    position: dict[str, Any],
    account: dict[str, Any],
    quote_by_symbol: dict[str, dict[str, Any]],
    market_session: dict[str, Any] | None = None,
) -> dict[str, Any] | None:
    product = position.get("Product") or position.get("product") or {}
    if not isinstance(product, dict):
        product = {}
    performance = _nested_view(position, "Performance", "performance")
    fundamental = _nested_view(position, "Fundamental", "fundamental")
    options_watch = _nested_view(position, "OptionsWatch", "optionsWatch", "optionswatch")
    quick = _nested_view(position, "Quick", "quick")
    complete = _nested_view(position, "Complete", "complete")
    sources = [position, complete, quick, options_watch, performance, fundamental, product]

    display_symbol = _safe_text(
        _first_value_from(
            sources,
            "displaySymbol",
            "display_symbol",
            "optionSymbol",
            "option_symbol",
            "osiKey",
            "symbolDescription",
        )
    ) or _safe_text(_first_value(product, "symbol"))

    contract_type = _parse_contract_type(position, product, display_symbol)
    strike = _safe_float(_first_value_from(sources, "strikePrice", "strike", "strike_price"))
    expiration = _parse_expiration(position, product)
    days_to_expiration = _safe_int(_first_value(position, "daysToExpiration", "days_to_expiration"))
    if days_to_expiration is None and expiration:
        parsed_expiration = parse_expiration_date(expiration)
        if parsed_expiration:
            days_to_expiration = (parsed_expiration - central_today()).days

    quantity = _safe_int(_first_value_from(sources, "quantity", "qty", "positionQuantity", "units"))
    if quantity is None:
        quantity = 0
    if quantity == 0:
        return None

    direction = _determine_direction(position, quantity)
    absolute_quantity = abs(quantity)
    underlying_symbol = _parse_underlying_symbol(position, product, display_symbol) or _safe_text(_first_value(product, "symbol", "underlyingSymbol")).upper() or None
    base_symbol_and_price = _first_value_from(sources, "baseSymbolAndPrice", "base_symbol_and_price")
    base_symbol_from_quote, underlying_price = _extract_symbol_and_price(base_symbol_and_price, underlying_symbol)
    if not underlying_symbol:
        underlying_symbol = base_symbol_from_quote
    if underlying_price is None:
        underlying_price = _safe_float(
            _first_value_from(
                sources,
                "underlyingPrice",
                "underlying_price",
                "lastUnderlyingPrice",
                "lastUnderlyingTrade",
            )
        )
    quote_type = _safe_text(
        _first_value_from(
            sources,
            "quoteStatus",
            "quote_status",
        )
    ) or None
    quote_timestamp = _safe_text(
        _first_value_from(
            sources,
            "lastTradeTime",
            "last_trade_time",
            "dateTimeUTC",
            "date_time_utc",
        )
    ) or None
    quote_time = _parse_timestamp(quote_timestamp)
    quote_age_seconds = None
    if quote_time is not None:
        quote_age_seconds = max(0, int((datetime.now(timezone.utc) - quote_time).total_seconds()))
    quote_stale = bool(_first_value(position, "quoteStale", "quote_stale"))
    if quote_age_seconds is not None:
        quote_stale = quote_stale or quote_age_seconds > 300
    if not quote_stale and quote_type in BAD_QUOTE_TYPES:
        quote_stale = True
    underlying_quote = _build_underlying_quote(
        symbol=underlying_symbol,
        price=underlying_price,
        quote_type=quote_type or None,
        quote_timestamp=quote_timestamp or None,
    )

    bid = _safe_float(_first_value_from(sources, "bid", "bidPrice", "bestBid"))
    ask = _safe_float(_first_value_from(sources, "ask", "askPrice", "bestAsk"))
    last = _safe_float(_first_value_from(sources, "lastPrice", "lastTrade", "last", "mark"))
    premium = _safe_float(_first_value_from(sources, "premium", "currentPrice", "closePrice", "marketPrice", "price"))
    if premium is None:
        premium = last

    market_value = _safe_float(_first_value_from(sources, "marketValue", "market_value", "value"))
    cost_basis = _safe_float(_first_value_from(sources, "totalCost", "costBasis", "cost_basis", "basis"))
    unrealized_pnl = _safe_float(_first_value_from(sources, "totalGain", "unrealizedPnL", "unrealized_pnl", "gainLoss", "gain_loss"))
    unrealized_pnl_pct = _safe_float(_first_value_from(sources, "totalGainPct", "unrealizedPnLPct", "unrealized_pnl_pct", "gainPct", "gain_pct"))
    day_gain = _safe_float(_first_value_from(sources, "daysGain", "todayGainLoss", "dayGainLoss", "day_gain"))
    day_gain_pct = _safe_float(_first_value_from(sources, "daysGainPct", "todayGainLossPct", "dayGainLossPct", "day_gain_pct"))
    if unrealized_pnl_pct is None and cost_basis not in (None, 0) and unrealized_pnl is not None:
        unrealized_pnl_pct = (unrealized_pnl / cost_basis) * 100 if cost_basis else None

    open_interest = _safe_int(_first_value_from(sources, "openInterest", "open_interest"))
    volume = _safe_int(_first_value_from(sources, "volume"))
    delta = _safe_float(_first_value_from(sources, "delta"))
    gamma = _safe_float(_first_value_from(sources, "gamma"))
    theta = _safe_float(_first_value_from(sources, "theta"))
    vega = _safe_float(_first_value_from(sources, "vega"))
    rho = _safe_float(_first_value_from(sources, "rho"))
    implied_volatility = _safe_float(_first_value_from(sources, "ivPct", "iv", "impliedVolatility", "implied_volatility"))

    spread_pct_value = spread_pct(bid or 0.0, ask or 0.0) if bid and ask and bid > 0 and ask > 0 else None
    quote_warning = _quote_issue_warning(quote_type or None, quote_stale, spread_pct_value, volume)
    actionable_live_quotes = bool((market_session or {}).get("actionable_live_quotes", True))
    option_quote_session_label = "Live" if actionable_live_quotes else "Previous session"

    contract_symbol = display_symbol or _safe_text(_first_value(position, "contractSymbol", "contract_symbol", "osiKey")) or "-"

    moneyness = None
    distance_from_spot_pct = None
    if underlying_price is not None and underlying_price > 0 and strike is not None and strike > 0 and contract_type in {"CALL", "PUT"}:
        distance = strike - underlying_price
        distance_from_spot_pct = (distance / underlying_price) * 100
        if abs(distance_from_spot_pct) <= 1:
            moneyness = "ATM"
        elif contract_type == "CALL":
            moneyness = "ITM" if strike < underlying_price else "OTM"
        else:
            moneyness = "ITM" if strike > underlying_price else "OTM"

    position_id_source = "|".join(
        [
            _safe_text(account.get("account_id_key") or account.get("account_id_suffix")),
            _safe_text(contract_symbol),
            direction,
            str(absolute_quantity),
        ]
    )
    position_id = hashlib.sha1(position_id_source.encode("utf-8")).hexdigest()[:16]

    return {
        "position_id": position_id,
        "account_id_key": account.get("account_id_key"),
        "account_id_suffix": account.get("account_id_suffix"),
        "account_name": account.get("account_name"),
        "account_type": account.get("account_type"),
        "symbol": underlying_symbol,
        "display_symbol": contract_symbol,
        "underlying_price": underlying_price,
        "base_symbol_and_price": base_symbol_and_price,
        "underlying_quote": underlying_quote,
        "underlying_quote_source": "etrade" if underlying_quote else None,
        "quote_details_url": _safe_text(position.get("quoteDetails")) or None,
        "lots_details_url": _safe_text(position.get("lotsDetails")) or None,
        "position_indicator": _safe_text(_first_value_from(sources, "positionIndicator", "position_indicator")) or None,
        "position_id_raw": _safe_int(_first_value_from(sources, "positionId", "position_id")),
        "direction": direction,
        "contract_type": contract_type or None,
        "quantity": absolute_quantity,
        "signed_quantity": quantity,
        "expiration": expiration,
        "days_to_expiration": days_to_expiration,
        "strike": strike,
        "bid": bid,
        "ask": ask,
        "last": last,
        "premium": premium,
        "spread_pct": spread_pct_value,
        "volume": volume,
        "open_interest": open_interest,
        "delta": delta,
        "gamma": gamma,
        "theta": theta,
        "vega": vega,
        "rho": rho,
        "implied_volatility": implied_volatility,
        "quote_type": quote_type or None,
        "quote_stale": quote_stale,
        "quote_timestamp": quote_timestamp or None,
        "last_valid_quote_timestamp": quote_timestamp or None,
        "market_session_state": (market_session or {}).get("session_state"),
        "actionable_live_quotes": actionable_live_quotes,
        "option_quote_session_label": option_quote_session_label,
        "next_market_open": (market_session or {}).get("next_market_open"),
        "session_note": (market_session or {}).get("session_note"),
        "market_value": market_value,
        "cost_basis": cost_basis,
        "unrealized_pnl": unrealized_pnl,
        "unrealized_pnl_pct": unrealized_pnl_pct,
        "day_gain": day_gain,
        "day_gain_pct": day_gain_pct,
        "moneyness": moneyness,
        "distance_from_spot_pct": distance_from_spot_pct,
        "warnings": quote_warning,
        "strategy": _safe_text(_first_value(position, "strategy", "strategyType", "optionStrategy")) or None,
        "source": "etrade",
        "raw": position,
    }


def _deterministic_position_advice(position: dict[str, Any]) -> dict[str, Any]:
    direction = _safe_text(position.get("direction")).upper() or "LONG"
    contract_type = _safe_text(position.get("contract_type")).upper()
    days_to_expiration = _safe_int(position.get("days_to_expiration"), 0) or 0
    spread = _safe_float(position.get("spread_pct"))
    volume = _safe_int(position.get("volume"), 0) or 0
    quote_type = _safe_text(position.get("quote_type")).upper()
    quote_stale = bool(position.get("quote_stale"))
    unrealized_pnl_pct = _safe_float(position.get("unrealized_pnl_pct"))
    underlying_price = _safe_float(_first_value(position.get("underlying_quote"), "price", "last", "underlying_price"))
    if underlying_price is None:
        underlying_price = _safe_float(position.get("underlying_price"))
    strike = _safe_float(position.get("strike"))

    warnings = list(position.get("warnings") or [])
    action = "HOLD"
    confidence = "MEDIUM"
    summary = "Hold and monitor."
    watch_for: list[str] = []
    close_if: list[str] = []
    roll_if: list[str] = []

    quote_problem = quote_stale or (quote_type in BAD_QUOTE_TYPES) or (spread is not None and spread > 5) or volume < 100
    if quote_problem:
        action = "WATCH"
        confidence = "LOW"
        summary = "Watch this position closely; quote quality or liquidity is weak."
        if spread is not None and spread > 5:
            watch_for.append(f"Spread must stay under 5%; current spread is {spread:.2f}%.")
        if volume < 100:
            watch_for.append(f"Volume needs to improve above 100; current volume is {volume}.")
        if quote_type in BAD_QUOTE_TYPES:
            watch_for.append(f"Quote type is {quote_type}; treat the pricing as non-actionable.")
        if quote_stale:
            watch_for.append("Quote is stale; do not trade off this snapshot.")
        close_if.append("Close or reduce if the quote remains stale and the spread stays wide.")

    if days_to_expiration <= 3:
        roll_if.append("Roll before gamma risk gets sharper if you want to keep the thesis.")
        if unrealized_pnl_pct is not None and unrealized_pnl_pct > 0:
            action = "ROLL"
            confidence = "MEDIUM"
            summary = "The contract is close to expiration and has gains; rolling may protect the thesis while resetting decay."
        elif direction == "SHORT":
            action = "CLOSE"
            confidence = "MEDIUM"
            summary = "The short position is near expiration; consider closing or rolling before assignment risk rises."
        else:
            action = "CLOSE"
            confidence = "MEDIUM"
            summary = "The long contract is near expiration; avoid letting time decay do the work against you."

    if unrealized_pnl_pct is not None:
        if unrealized_pnl_pct >= 50 and direction == "LONG":
            action = "TRIM"
            confidence = "HIGH"
            summary = "The long position has a strong gain; taking some off can lock in premium before decay accelerates."
            close_if.append("Trim or close part of the position if momentum stalls.")
        elif unrealized_pnl_pct <= -35 and direction == "LONG":
            action = "CLOSE"
            confidence = "HIGH"
            summary = "The long position is under pressure; cutting the loser may be cleaner than waiting for theta to worsen."
            close_if.append("Close if the underlying breaks the setup and the contract keeps decaying.")
        elif unrealized_pnl_pct >= 50 and direction == "SHORT":
            action = "CLOSE"
            confidence = "HIGH"
            summary = "The short position has achieved a meaningful gain; consider closing into strength instead of holding for the last few dollars."
            close_if.append("Close while the spread is still controlled and the win is already in hand.")
        elif unrealized_pnl_pct <= -35 and direction == "SHORT":
            action = "REDUCE"
            confidence = "HIGH"
            summary = "The short position is moving against you; reduce risk before the loss expands."
            close_if.append("Reduce or close if the underlying keeps moving through the invalidation level.")

    if underlying_price is not None and strike is not None and underlying_price > 0 and strike > 0:
        if contract_type == "CALL":
            if direction == "LONG":
                watch_for.append(f"Underlying needs to stay above the strike area near {strike:.2f} to keep this call working.")
            else:
                watch_for.append(f"Underlying breaking back above {strike:.2f} would pressure the short call.")
        elif contract_type == "PUT":
            if direction == "LONG":
                watch_for.append(f"Underlying needs to stay below the strike area near {strike:.2f} to keep this put working.")
            else:
                watch_for.append(f"Underlying reclaiming {strike:.2f} would pressure the short put.")

    if underlying_price is not None and underlying_price > 0:
        watch_for.append(f"Underlying quote is {underlying_price:.2f}; use that as the live reference, not the stale contract mark alone.")

    if not watch_for:
        watch_for.append("Watch the contract spread, DTE, and whether the position still matches the original thesis.")

    if not close_if:
        close_if.append("Close if liquidity worsens, quote quality degrades, or the thesis stops matching the underlying move.")
    if not roll_if:
        roll_if.append("Roll only if you want to keep the idea alive and can improve DTE or strike placement.")

    return {
        "source": "deterministic",
        "action": action,
        "confidence": confidence,
        "summary": summary,
        "watch_for": watch_for[:4],
        "close_if": close_if[:3],
        "roll_if": roll_if[:3],
        "risk_notes": warnings[:4],
    }


def _advice_schema() -> dict[str, Any]:
    return {
        "type": "object",
        "additionalProperties": False,
        "properties": {
            "portfolio_summary": {
                "type": "object",
                "additionalProperties": False,
                "properties": {
                    "headline": {"type": "string"},
                    "overall_risk": {"type": "string", "enum": ["LOW", "MEDIUM", "HIGH"]},
                    "priority_actions": {"type": "array", "items": {"type": "string"}},
                },
                "required": ["headline", "overall_risk", "priority_actions"],
            },
            "positions": {
                "type": "array",
                "items": {
                    "type": "object",
                    "additionalProperties": False,
                    "properties": {
                        "position_id": {"type": "string"},
                        "action": {
                            "type": "string",
                            "enum": ["HOLD", "TRIM", "CLOSE", "ROLL", "REDUCE", "WATCH", "AVOID_ADDING"],
                        },
                        "confidence": {"type": "string", "enum": ["LOW", "MEDIUM", "HIGH"]},
                        "summary": {"type": "string"},
                        "watch_for": {"type": "array", "items": {"type": "string"}},
                        "close_if": {"type": "array", "items": {"type": "string"}},
                        "roll_if": {"type": "array", "items": {"type": "string"}},
                        "risk_notes": {"type": "array", "items": {"type": "string"}},
                    },
                    "required": ["position_id", "action", "confidence", "summary", "watch_for", "close_if", "roll_if", "risk_notes"],
                },
            },
        },
        "required": ["portfolio_summary", "positions"],
    }


def _advice_prompt(positions: list[dict[str, Any]]) -> dict[str, Any]:
    return {
        "instruction": (
            "Give plain-English management advice for open option positions. "
            "Assume the user already understands options. Focus on what to do next: hold, trim, close, roll, reduce, or watch. "
            "Use only the supplied facts. Do not invent catalysts, news, or price predictions. "
            "Prefer risk management over hype. Evaluate current R, peak R, target progress, VWAP, "
            "completed management-timeframe structure, volume, profit giveback, and overnight risk. "
            "Return concise HOLD, TRIM, CLOSE, REDUCE, ROLL, or WATCH guidance. "
            "Never imply that a suggested stop was placed with E*TRADE."
        ),
        "positions": [
            {
                "position_id": pos.get("position_id"),
                "account": pos.get("account_name") or pos.get("account_id_suffix"),
                "symbol": pos.get("symbol"),
                "display_symbol": pos.get("display_symbol"),
                "direction": pos.get("direction"),
                "contract_type": pos.get("contract_type"),
                "quantity": pos.get("quantity"),
                "expiration": pos.get("expiration"),
                "days_to_expiration": pos.get("days_to_expiration"),
                "strike": pos.get("strike"),
                "bid": pos.get("bid"),
                "ask": pos.get("ask"),
                "last": pos.get("last"),
                "premium": pos.get("premium"),
                "base_symbol_and_price": pos.get("base_symbol_and_price"),
                "spread_pct": pos.get("spread_pct"),
                "volume": pos.get("volume"),
                "open_interest": pos.get("open_interest"),
                "delta": pos.get("delta"),
                "theta": pos.get("theta"),
                "vega": pos.get("vega"),
                "unrealized_pnl": pos.get("unrealized_pnl"),
                "unrealized_pnl_pct": pos.get("unrealized_pnl_pct"),
                "quote_type": pos.get("quote_type"),
                "quote_stale": pos.get("quote_stale"),
                "underlying_price": pos.get("underlying_price"),
                "moneyness": pos.get("moneyness"),
                "distance_from_spot_pct": pos.get("distance_from_spot_pct"),
                "warnings": pos.get("warnings"),
                "exit_plan": pos.get("exit_plan"),
                "exit_management": pos.get("exit_management"),
            }
            for pos in positions
        ],
    }


def _extract_output_text(response_payload: dict[str, Any]) -> str:
    if isinstance(response_payload.get("output_text"), str):
        return response_payload["output_text"]
    for item in response_payload.get("output") or []:
        for content in item.get("content") or []:
            if isinstance(content.get("text"), str):
                return content["text"]
    return ""


def _ai_failure_message(error: Exception) -> str:
    """Return a useful, non-leaky status for an advisory transport failure."""
    text = str(error or "").lower()
    if isinstance(error, (requests.Timeout, requests.ConnectionError)) or "timed out" in text or "timeout" in text:
        return "AI advisory timed out. Deterministic position guidance is still available."
    if "401" in text or "unauthorized" in text or "invalid api key" in text:
        return "AI advisory authentication failed. Deterministic position guidance is still available."
    if "429" in text or "rate limit" in text or "rate limited" in text:
        return "AI advisory is rate-limited. Deterministic position guidance is still available."
    return "AI advisory is unavailable. Deterministic position guidance is still available."


def _generate_ai_advice(positions: list[dict[str, Any]]) -> dict[str, Any]:
    ai_cfg = config_manager.get("ai", default={}) or {}
    advisory_cfg = config_manager.get("advisory", default={}) or {}
    if not bool(ai_cfg.get("enabled", True)):
        return {
            "status": "disabled",
            "model": None,
            "blocking_reason": "AI advice is disabled in config",
        }

    api_key = os.getenv("OPENAI_API_KEY", "").strip()
    if not api_key:
        return {
            "status": "unavailable",
            "model": None,
            "blocking_reason": "OpenAI API key is missing on the backend",
        }

    model = (
        os.getenv("OPENAI_MODEL_POSITION_ADVICE")
        or os.getenv("OPENAI_ADVISORY_MODEL")
        or str(ai_cfg.get("position_model") or "").strip()
        or str(advisory_cfg.get("model") or "").strip()
        or POSITION_ADVICE_MODEL_DEFAULT
    )
    model = model.strip() or POSITION_ADVICE_MODEL_DEFAULT
    timeout = int(
        os.getenv("OPENAI_POSITION_ADVICE_TIMEOUT_SECONDS")
        or ai_cfg.get("position_advice_timeout_seconds", ai_cfg.get("timeout_seconds", 45))
        or 45
    )
    request_payload = {
        "model": model,
        "input": [
            {
                "role": "system",
                "content": (
                    "You are an options position-management assistant for an admin user. "
                    "Give concise, practical advice on how to manage open positions. "
                    "Do not explain what calls or puts are. "
                    "Do not invent news, catalysts, probabilities, or guaranteed outcomes. "
                    "Use only the supplied facts. "
                    "Prefer hold/trim/close/roll/reduce/watch/avoid_adding actions. "
                    "If quote quality is poor, say so directly. "
                    "Return JSON only."
                ),
            },
            {"role": "user", "content": json.dumps(_advice_prompt(positions), sort_keys=True)},
        ],
        "text": {
            "format": {
                "type": "json_schema",
                "name": "etrade_position_advice",
                "strict": True,
                "schema": _advice_schema(),
            }
        },
    }

    def _post_openai() -> requests.Response:
        response = requests.post(
            OPENAI_RESPONSES_URL,
            headers={
                "Authorization": f"Bearer {api_key}",
                "Content-Type": "application/json",
            },
            json=request_payload,
            timeout=timeout,
        )
        response.raise_for_status()
        return response

    models_to_try = [model]
    fallback_model = (
        os.getenv("OPENAI_POSITION_ADVICE_FALLBACK_MODEL")
        or os.getenv("OPENAI_ADVISORY_FALLBACK_MODEL")
        or str(ai_cfg.get("fallback_model") or advisory_cfg.get("fallback_model") or "").strip()
    )
    if fallback_model and fallback_model != model:
        models_to_try.append(fallback_model)

    last_error: Exception | None = None
    for use_model in models_to_try:
        request_payload["model"] = use_model
        try:
            response = call_with_rate_limit("openai", None, "position_advice", _post_openai)
            response_payload = response.json()
            parsed = json.loads(_extract_output_text(response_payload))
            if not isinstance(parsed, dict):
                raise ValueError("Invalid AI advice payload")
            return {
                "status": "ok",
                "model": use_model,
                "summary": parsed.get("portfolio_summary") or {},
                "positions": parsed.get("positions") or [],
                "blocking_reason": None,
            }
        except Exception as exc:
            last_error = exc

    return {
        "status": "unavailable",
        "model": model,
        "blocking_reason": _ai_failure_message(last_error or RuntimeError("unknown advisory failure")),
    }


def _merge_advice(position: dict[str, Any], advice: dict[str, Any] | None) -> dict[str, Any]:
    fallback = _deterministic_position_advice(position)
    if not advice:
        return fallback

    merged = {
        **fallback,
        **{key: value for key, value in advice.items() if key in {"action", "confidence", "summary", "watch_for", "close_if", "roll_if", "risk_notes"}},
    }
    merged["source"] = "openai" if advice else fallback.get("source", "deterministic")
    return merged


def _flatten_positions(accounts: list[dict[str, Any]]) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for account in accounts:
        for position in account.get("positions") or []:
            row = dict(position)
            row["account_name"] = account.get("account_name")
            row["account_id_suffix"] = account.get("account_id_suffix")
            row["account_type"] = account.get("account_type")
            rows.append(row)
    return rows


def _candles_from_dataframe(df) -> list[dict[str, Any]]:
    if df is None or getattr(df, "empty", True):
        return []
    candles: list[dict[str, Any]] = []
    try:
        records = df.reset_index().to_dict("records")
    except Exception:
        return []
    for row in records:
        time_value = row.get("time") or row.get("index")
        if isinstance(time_value, datetime):
            time_value = int(time_value.replace(tzinfo=timezone.utc).timestamp()) if time_value.tzinfo is None else int(time_value.timestamp())
        elif hasattr(time_value, "timestamp"):
            try:
                time_value = int(time_value.timestamp())
            except Exception:
                time_value = None
        else:
            try:
                parsed = datetime.fromisoformat(str(time_value).replace("Z", "+00:00"))
                if parsed.tzinfo is None:
                    parsed = parsed.replace(tzinfo=timezone.utc)
                time_value = int(parsed.timestamp())
            except Exception:
                time_value = None
        if time_value is None:
            continue
        candles.append(
            {
                "time": time_value,
                "open": _safe_float(row.get("open"), 0.0),
                "high": _safe_float(row.get("high"), 0.0),
                "low": _safe_float(row.get("low"), 0.0),
                "close": _safe_float(row.get("close"), 0.0),
                "volume": _safe_float(row.get("volume"), 0.0),
            }
        )
    return candles


def _history_period_for_interval(interval: str) -> str:
    history_cfg = config_manager.get("history", default={}) or {}
    normalized = str(interval or "").strip().lower()
    default_period = str(history_cfg.get("default_period", "1y") or "1y").strip() or "1y"
    intraday_days = int(history_cfg.get("intraday_initial_days", 90) or 90)
    if normalized == "1d":
        return default_period
    if normalized.endswith("m") or normalized.endswith("h"):
        return f"{max(1, intraday_days)}d"
    return default_period


def _history_plan() -> list[tuple[str, str]]:
    history_cfg = config_manager.get("history", default={}) or {}
    configured = history_cfg.get("intervals") or ["5m", "15m", "1d"]
    plan: list[tuple[str, str]] = []
    for item in configured:
        interval = str(item or "").strip().lower()
        if not interval or interval == "1m":
            continue
        plan.append((interval, _history_period_for_interval(interval)))
    if not plan:
        plan = [
            ("5m", _history_period_for_interval("5m")),
            ("15m", _history_period_for_interval("15m")),
            ("1d", _history_period_for_interval("1d")),
        ]
    deduped: list[tuple[str, str]] = []
    seen: set[str] = set()
    for interval, period in plan:
        if interval in seen:
            continue
        seen.add(interval)
        deduped.append((interval, period))
    return deduped


def _history_profile(symbol: str, interval: str, period: str, candles_df) -> dict[str, Any]:
    candles = _candles_from_dataframe(candles_df)
    attrs = getattr(candles_df, "attrs", {}) or {}
    provider = _safe_text(attrs.get("provider") or attrs.get("source")) or "unknown"
    last_updated = attrs.get("last_updated") or attrs.get("timestamp")
    first_timestamp = None
    last_timestamp = None
    if candles:
        first_timestamp = datetime.fromtimestamp(int(candles[0]["time"]), tz=timezone.utc).isoformat()
        last_timestamp = datetime.fromtimestamp(int(candles[-1]["time"]), tz=timezone.utc).isoformat()
    return {
        "symbol": symbol,
        "interval": interval,
        "requested_period": period,
        "provider": provider,
        "source": provider,
        "status": "loaded" if candles else "unavailable",
        "bars_loaded": len(candles),
        "first_timestamp": first_timestamp,
        "last_timestamp": last_timestamp,
        "loaded_at": last_updated,
        "candles": candles,
    }


def _load_history_profile(symbol: str, interval: str, period: str) -> dict[str, Any]:
    candles_df = get_candles_from_sql(symbol, interval, period=period)
    profile = _history_profile(symbol, interval, period, candles_df)
    profile["attempt"] = "stored_sql"
    if int(profile.get("bars_loaded", 0) or 0):
        return profile

    try:
        fetched = fetch_candles(symbol, interval=interval, period=period, refresh=False, historical=True, prefer_stored=True)
        fetched_profile = _history_profile(symbol, interval, period, fetched)
        fetched_profile["attempt"] = "provider_fallback"
        if int(fetched_profile.get("bars_loaded", 0) or 0):
            return fetched_profile
        fetched_profile["warning"] = "Provider returned no candles for this interval."
        return fetched_profile
    except Exception as exc:
        profile["warning"] = f"No stored candles for this interval yet; provider fetch unavailable: {exc}"
        return profile


def _chart_profile_from_history(profile: dict[str, Any]) -> dict[str, Any]:
    candles = list((profile or {}).get("candles") or [])
    if not candles:
        return {
            "symbol": profile.get("symbol"),
            "interval": profile.get("interval"),
            "requested_period": profile.get("requested_period"),
            "provider": profile.get("provider"),
            "source": profile.get("source"),
            "timestamp": profile.get("loaded_at"),
            "last_updated": profile.get("loaded_at"),
            "candles": [],
            "indicators": [],
            "latest": {},
            "line_indicators": [
                {"key": "ema_fast", "label": "EMA 9", "color": "#16c784"},
                {"key": "ema_slow", "label": "EMA 21", "color": "#f0b90b"},
                {"key": "ema_trend", "label": "EMA 50", "color": "#ef4444"},
                {"key": "ema_200", "label": "EMA 200", "color": "#a78bfa"},
                {"key": "vwap", "label": "VWAP", "color": "#60a5fa"},
            ],
            "warnings": ["Historical candle data is unavailable."],
        }

    df = pd.DataFrame(candles)
    if not df.empty and "time" in df.columns:
        df["time"] = pd.to_datetime(df["time"], unit="s", utc=True)
        df = df.set_index("time")

    cfg = {
        "ema_fast": 9,
        "ema_slow": 21,
        "ema_trend": 50,
        "rsi_period": 14,
        "atr_period": 14,
        "bollinger_period": 20,
        "bollinger_std": 2,
        "volume_avg_period": 20,
        "macd_fast": 12,
        "macd_slow": 26,
        "macd_signal": 9,
    }
    enriched = apply_indicators(df, cfg)
    enriched["ema_200"] = enriched["close"].ewm(span=200, adjust=False).mean()

    candles_json: list[dict[str, Any]] = []
    indicators_json: list[dict[str, Any]] = []
    for idx, row in enriched.iterrows():
        ts = int(idx.timestamp()) if hasattr(idx, "timestamp") else int(idx)
        candles_json.append(
            {
                "time": ts,
                "open": _safe_float(row.get("open"), 0.0),
                "high": _safe_float(row.get("high"), 0.0),
                "low": _safe_float(row.get("low"), 0.0),
                "close": _safe_float(row.get("close"), 0.0),
                "volume": _safe_float(row.get("volume"), 0.0),
            }
        )
        indicators_json.append(
            {
                "time": ts,
                "ema_fast": _safe_float(row.get("ema_fast")),
                "ema_slow": _safe_float(row.get("ema_slow")),
                "ema_trend": _safe_float(row.get("ema_trend")),
                "ema_200": _safe_float(row.get("ema_200")),
                "vwap": _safe_float(row.get("vwap")),
                "rsi": _safe_float(row.get("rsi")),
                "macd_line": _safe_float(row.get("macd_line")),
                "macd_signal": _safe_float(row.get("macd_signal")),
                "macd_hist": _safe_float(row.get("macd_hist")),
                "atr": _safe_float(row.get("atr")),
                "volume_avg": _safe_float(row.get("volume_avg")),
            }
        )

    latest = indicators_json[-1] if indicators_json else {}
    return {
        "symbol": profile.get("symbol"),
        "interval": profile.get("interval"),
        "requested_period": profile.get("requested_period"),
        "provider": profile.get("provider"),
        "source": profile.get("source"),
        "timestamp": profile.get("loaded_at"),
        "last_updated": profile.get("loaded_at"),
        "candles": candles_json,
        "indicators": indicators_json,
        "latest": latest,
        "line_indicators": [
            {"key": "ema_fast", "label": "EMA 9", "color": "#16c784"},
            {"key": "ema_slow", "label": "EMA 21", "color": "#f0b90b"},
            {"key": "ema_trend", "label": "EMA 50", "color": "#ef4444"},
            {"key": "ema_200", "label": "EMA 200", "color": "#a78bfa"},
            {"key": "vwap", "label": "VWAP", "color": "#60a5fa"},
        ],
        "warnings": [],
    }


def _select_history_interval(history_profiles: dict[str, dict[str, Any]]) -> str | None:
    preferred = ["15m", "5m", "1d"]
    for interval in preferred:
        profile = history_profiles.get(interval)
        if profile and int(profile.get("bars_loaded", 0) or 0) >= 20:
            return interval
    for interval in preferred:
        profile = history_profiles.get(interval)
        if profile and int(profile.get("bars_loaded", 0) or 0) > 0:
            return interval
    return None


def _group_positions(accounts: list[dict[str, Any]]) -> list[dict[str, Any]]:
    grouped: list[dict[str, Any]] = []
    for account in accounts:
        account_positions = list(account.get("positions") or [])
        grouped.append(
            {
                **{key: value for key, value in account.items() if key != "account_id_key"},
                "position_count": len(account_positions),
                "positions": account_positions,
            }
        )
    return grouped


def _public_position(position: dict[str, Any]) -> dict[str, Any]:
    forbidden = {"account_id_key", "paper_risk", "paper_portfolio_id", "recommendation_id", "simulated_fill_source"}
    return {key: value for key, value in position.items() if key not in forbidden}


def _persist_brokerage_positions(accounts: list[dict[str, Any]]) -> None:
    """Persist only broker-observed records; no paper tables are touched."""
    now = _now_iso()
    with SessionLocal() as db:
        for account in accounts:
            broker_id = _safe_text(account.get("account_id_key"))
            if not broker_id:
                continue
            row = db.query(BrokerageAccount).filter(
                BrokerageAccount.broker == "etrade",
                BrokerageAccount.broker_record_id == broker_id,
            ).first()
            if not row:
                row = BrokerageAccount(broker="etrade", broker_record_id=broker_id, created_at=now)
                db.add(row)
                db.flush()
            row.masked_account = account.get("account_id_suffix")
            row.account_type = account.get("account_type")
            row.account_description = account.get("account_name")
            row.institution_type = account.get("institution_type")
            row.account_equity = account.get("account_equity")
            row.cash_balance = account.get("cash_balance")
            row.buying_power = account.get("buying_power")
            row.last_synced_at = now
            row.updated_at = now
            for position in account.get("positions") or []:
                broker_record_id = _safe_text(position.get("position_id_raw") or position.get("position_id") or position.get("display_symbol"))
                if not broker_record_id:
                    continue
                stored = db.query(BrokeragePosition).filter(
                    BrokeragePosition.brokerage_account_id == row.id,
                    BrokeragePosition.broker_record_id == broker_record_id,
                ).first()
                if not stored:
                    stored = BrokeragePosition(
                        brokerage_account_id=row.id,
                        broker="etrade",
                        broker_record_id=broker_record_id,
                    )
                    db.add(stored)
                    db.flush()
                try:
                    previous_payload = json.loads(stored.payload_json or "{}")
                except Exception:
                    previous_payload = {}
                previous_management = previous_payload.get("exit_management") or {}
                current_management = position.get("exit_management") or {}
                if current_management and previous_management.get("state") != current_management.get("state"):
                    db.add(BrokerageExitAuditEvent(
                        brokerage_position_id=stored.id,
                        broker="etrade",
                        symbol=position.get("symbol"),
                        event_type="EXIT_STATE_CHANGED",
                        reason=str(current_management.get("reason") or current_management.get("state") or "Position management state updated."),
                        details_json=json.dumps(current_management, sort_keys=True, default=str),
                    ))
                stored.symbol = position.get("symbol")
                stored.contract_symbol = position.get("display_symbol")
                stored.quantity = position.get("signed_quantity")
                stored.average_cost = position.get("cost_basis")
                stored.market_value = position.get("market_value")
                stored.unrealized_pnl = position.get("unrealized_pnl")
                stored.broker_timestamp = position.get("quote_timestamp")
                stored.synced_at = now
                stored.payload_json = json.dumps(_public_position(position), default=str)
        db.commit()


def _summarize_positions(accounts: list[dict[str, Any]], ai_result: dict[str, Any]) -> dict[str, Any]:
    positions = _flatten_positions(accounts)
    total_market_value = sum(_safe_float(pos.get("market_value"), 0.0) or 0.0 for pos in positions)
    total_cost_basis = sum(_safe_float(pos.get("cost_basis"), 0.0) or 0.0 for pos in positions)
    total_unrealized_pnl = sum(_safe_float(pos.get("unrealized_pnl"), 0.0) or 0.0 for pos in positions)
    total_position_count = len(positions)
    account_equity = sum(_safe_float(account.get("account_equity"), 0.0) or 0.0 for account in accounts)
    cash_balance = sum(_safe_float(account.get("cash_balance"), 0.0) or 0.0 for account in accounts)
    buying_power = sum(_safe_float(account.get("buying_power"), 0.0) or 0.0 for account in accounts)
    long_count = sum(1 for pos in positions if _safe_text(pos.get("direction")).upper() == "LONG")
    short_count = sum(1 for pos in positions if _safe_text(pos.get("direction")).upper() == "SHORT")
    quote_sources = sorted({
        pos.get("underlying_quote_source") or pos.get("source")
        for pos in positions
        if (pos.get("underlying_quote_source") or pos.get("source"))
    })
    quote_statuses = sorted({pos.get("quote_type") for pos in positions if pos.get("quote_type")})
    historical_sources = sorted({
        profile.get("source")
        for pos in positions
        for profile in (
            (pos.get("historical_context") or {}).get("intervals").values()
            if isinstance((pos.get("historical_context") or {}).get("intervals"), dict)
            else _as_list((pos.get("historical_context") or {}).get("intervals"))
        )
        if isinstance(profile, dict) and profile.get("source")
    })
    historical_bars_loaded = sum(
        int(profile.get("bars_loaded", 0) or 0)
        for pos in positions
        for profile in (
            (pos.get("historical_context") or {}).get("intervals").values()
            if isinstance((pos.get("historical_context") or {}).get("intervals"), dict)
            else _as_list((pos.get("historical_context") or {}).get("intervals"))
        )
        if isinstance(profile, dict)
    )
    return {
        "account_count": len(accounts),
        "real_account_equity": round(account_equity, 2),
        "real_cash_balance": round(cash_balance, 2),
        "real_buying_power": round(buying_power, 2),
        "position_count": total_position_count,
        "long_count": long_count,
        "short_count": short_count,
        "total_market_value": round(total_market_value, 2),
        "total_cost_basis": round(total_cost_basis, 2),
        "total_unrealized_pnl": round(total_unrealized_pnl, 2),
        "quote_sources": quote_sources,
        "quote_statuses": quote_statuses,
        "historical_sources": historical_sources,
        "historical_bars_loaded": historical_bars_loaded,
        "ai_status": ai_result.get("status"),
        "ai_model": ai_result.get("model"),
        "ai_blocking_reason": ai_result.get("blocking_reason"),
        "portfolio_summary": ai_result.get("summary") or {},
    }


def _build_positions_snapshot(market_session: dict[str, Any] | None = None) -> dict[str, Any]:
    accounts_payload = _call_etrade("/v1/accounts/list.json")
    accounts = [_normalize_account(account) for account in _extract_accounts(accounts_payload)]
    filtered_accounts = [account for account in accounts if account.get("account_id_key")]

    account_positions: list[dict[str, Any]] = []
    for account in filtered_accounts:
        account_id = account.get("account_id_key")
        if not account_id:
            continue
        first_page = _fetch_portfolio_page(account_id, page_number=1)
        pages = _extract_portfolios(first_page)
        total_pages = 1
        if pages:
            total_pages = max(_safe_int(pages[0].get("totalPages"), 1) or 1, 1)

        all_portfolios: list[dict[str, Any]] = []
        all_portfolios.extend(pages)
        for page_number in range(2, total_pages + 1):
            next_page = _fetch_portfolio_page(account_id, page_number=page_number)
            all_portfolios.extend(_extract_portfolios(next_page))

        normalized_positions: list[dict[str, Any]] = []
        for portfolio in all_portfolios:
            portfolio_positions = _extract_positions(portfolio)
            for raw_position in portfolio_positions:
                if not _is_option_position(raw_position):
                    continue
                normalized = _normalize_position(raw_position, account, {}, market_session=market_session)
                if normalized:
                    normalized_positions.append(normalized)

        # Keep every real brokerage account in the real-account view, even
        # when it currently has no option positions.
        account_positions.append({**account, "positions": normalized_positions})

    flat_positions = _flatten_positions(account_positions)
    unique_symbols = sorted({pos.get("symbol") for pos in flat_positions if pos.get("symbol")})
    ratios_by_symbol: dict[str, dict[str, Any]] = {}
    candles_by_symbol: dict[str, list[dict[str, Any]]] = {}
    history_context_by_symbol: dict[str, dict[str, Any]] = {}
    for symbol in unique_symbols:
        try:
            ratios_by_symbol[symbol] = calculate_ratios(symbol, expirations_to_check=3)
        except Exception as exc:
            ratios_by_symbol[symbol] = {
                "symbol": symbol,
                "provider": "unavailable",
                "source": "unavailable",
                "warning": str(exc),
                "warnings": [str(exc)],
                "positioning": {
                    "symbol": symbol,
                    "classification": "Insufficient data",
                    "bias": "INSUFFICIENT_DATA",
                    "bias_score": 0,
                    "positioning_score": 0,
                    "confidence": "LOW",
                    "session_label": "Previous session" if market_session and not market_session.get("actionable_live_quotes", True) else "Live",
                    "session_state": (market_session or {}).get("session_state", "UNKNOWN"),
                    "scopes": {"overall": None, "selected_expiration": None, "near_term": {}, "relevant_strikes": None},
                },
            }

    for symbol in unique_symbols:
        history_profiles: dict[str, dict[str, Any]] = {}
        chart_profiles: dict[str, dict[str, Any]] = {}
        for interval, period in _history_plan():
            selected_profile = _load_history_profile(symbol, interval, period)
            if not int(selected_profile.get("bars_loaded", 0) or 0):
                selected_profile["warning"] = "No stored candles for this interval yet. Historical backfill will fill this over time."
            history_profiles[interval] = {
                **selected_profile,
                "candidates": [{key: value for key, value in selected_profile.items() if key != "candles"}],
            }
            chart_profiles[interval] = _chart_profile_from_history(selected_profile)

        selected_interval = _select_history_interval(history_profiles)
        selected_profile = history_profiles.get(selected_interval) if selected_interval else None
        selected_candles = list((selected_profile or {}).get("candles") or [])
        candles_by_symbol[symbol] = selected_candles
        history_context_by_symbol[symbol] = {
            "symbol": symbol,
            "selected_interval": selected_interval,
            "selected_provider": (selected_profile or {}).get("provider"),
            "selected_period": (selected_profile or {}).get("requested_period"),
            "selected_bars_loaded": int((selected_profile or {}).get("bars_loaded", 0) or 0),
            "selected_first_timestamp": (selected_profile or {}).get("first_timestamp"),
            "selected_last_timestamp": (selected_profile or {}).get("last_timestamp"),
            "selected_loaded_at": (selected_profile or {}).get("loaded_at"),
            "chart": chart_profiles.get(selected_interval) if selected_interval else None,
            "session_label": "Previous session" if market_session and not market_session.get("actionable_live_quotes", True) else "Live",
            "intervals": {
                interval: {
                    key: value
                    for key, value in profile.items()
                    if key != "candles"
                }
                for interval, profile in history_profiles.items()
            },
        }

    for account in account_positions:
        enriched_positions: list[dict[str, Any]] = []
        for position in account.get("positions") or []:
            symbol = position.get("symbol")
            ratios = ratios_by_symbol.get(symbol or "") or {}
            historical_context = history_context_by_symbol.get(symbol or "") or {}
            historical_chart = (historical_context or {}).get("chart") or {}
            news_catalyst = build_news_catalyst_impact(
                symbol or "",
                market_session=market_session,
                indicator_data=historical_chart,
                direction=position.get("direction"),
                expiration=position.get("expiration"),
                context_type="open_position",
            )
            money_flow = build_money_flow(
                symbol=symbol or "",
                side=position.get("direction"),
                market_session=market_session,
                candles=candles_by_symbol.get(symbol or ""),
                ratios=ratios,
                current_price=position.get("underlying_price") or _safe_float((position.get("underlying_quote") or {}).get("price")),
                quote_timestamp=position.get("quote_timestamp"),
                quote_type=position.get("quote_type"),
            )
            management_context = dict((historical_chart or {}).get("latest") or {})
            management_context.setdefault("completed_candle", True)
            exit_plan = build_exit_plan(position, indicators=management_context)
            exit_management = evaluate_exit_management(
                position,
                exit_plan,
                indicators=management_context,
                market_session=market_session,
                config=config_manager.get("paper_portfolio", default={}) or {},
            )
            enriched_positions.append(
                {
                    **position,
                    "options_positioning": ratios.get("positioning"),
                    "money_flow": money_flow,
                    "historical_context": historical_context,
                    "historical_chart": historical_chart,
                    "news_catalyst": news_catalyst,
                    "management_context": management_context,
                    "exit_plan": exit_plan,
                    "exit_management": {
                        **exit_management,
                        "broker_order_confirmed": False,
                        "actual_working_stop": None,
                        "note": "Suggested management only. No E*TRADE stop order is implied or placed.",
                    },
                }
            )
        account["positions"] = enriched_positions

    _persist_brokerage_positions(account_positions)

    flat_positions = _flatten_positions(account_positions)
    ai_result = _generate_ai_advice(flat_positions) if flat_positions else {"status": "unavailable", "model": None, "blocking_reason": "No open option positions found"}
    advice_by_id = {item.get("position_id"): item for item in _as_list(ai_result.get("positions")) if isinstance(item, dict)}

    if flat_positions:
        for account in account_positions:
            account_positions_list: list[dict[str, Any]] = []
            for position in account.get("positions") or []:
                advice = advice_by_id.get(position.get("position_id"))
                merged_advice = _merge_advice(position, advice)
                account_positions_list.append({**position, "advice": merged_advice})
            account["positions"] = account_positions_list

    summary = _summarize_positions(account_positions, ai_result)
    flat_with_advice = _flatten_positions(account_positions)
    public_accounts = [
        {
            **{key: value for key, value in account.items() if key != "account_id_key"},
            "positions": [_public_position(position) for position in account.get("positions") or []],
        }
        for account in account_positions
    ]
    public_positions = [_public_position(position) for position in flat_with_advice]

    return {
        "status": "ok",
        "provider": "etrade",
        "generated_at": _now_iso(),
        "market_session": market_session or {},
        "summary": summary,
        "ai": ai_result,
        "accounts": _group_positions(public_accounts),
        "positions": public_positions,
    }


def get_open_option_positions(*, refresh: bool = False, market_session: dict[str, Any] | None = None) -> dict[str, Any]:
    cache_path = _cache_path("etrade_open_option_positions.json")
    cached = _sanitize_cached_ai_status(_read_json(cache_path))
    if _cached_missing_key_warning_is_stale(cached):
        try:
            cache_path.unlink(missing_ok=True)
        except Exception:
            pass
        cached = None
    ttl_seconds = market_aware_ttl(
        int(config_manager.get("cache", "etrade_positions_ttl_seconds", default=POSITION_CACHE_TTL_SECONDS) or POSITION_CACHE_TTL_SECONDS),
        market_session=market_session,
    )
    cache_fresh = bool(cached and _fresh(cache_path, ttl_seconds))
    prior_refresh_error = _POSITION_REFRESH_LAST_ERROR

    if refresh or not cache_fresh:
        _queue_positions_refresh(cache_path=cache_path, ttl_seconds=ttl_seconds, market_session=market_session)

    refresh_state = {
        "in_progress": _POSITION_REFRESH_ACTIVE,
        "started_at": _POSITION_REFRESH_STARTED_AT,
        "last_error": _POSITION_REFRESH_LAST_ERROR,
    }

    if cache_fresh and cached:
        payload = dict(cached)
        payload["cache"] = {"hit": True, "ttl_seconds": ttl_seconds, "fresh": True}
        payload["refresh_in_progress"] = refresh_state["in_progress"]
        payload["refresh_state"] = refresh_state
        if refresh:
            payload["message"] = "Refresh requested. Showing the latest cached E*TRADE snapshot while a background rebuild runs."
        return payload

    if cached:
        payload = dict(cached)
        payload["status"] = "refreshing"
        payload["cache"] = {"hit": True, "ttl_seconds": ttl_seconds, "fresh": False}
        payload["refresh_in_progress"] = refresh_state["in_progress"]
        payload["refresh_state"] = refresh_state
        if _POSITION_REFRESH_LAST_ERROR:
            payload["last_refresh_error"] = _POSITION_REFRESH_LAST_ERROR
            payload["message"] = f"Refreshing E*TRADE positions in the background after a previous refresh error: {_POSITION_REFRESH_LAST_ERROR}"
        else:
            payload["message"] = "Refreshing E*TRADE positions in the background. Showing the last cached snapshot until it finishes."
        return payload

    if _POSITION_REFRESH_LAST_ERROR and not refresh_state["in_progress"]:
        exc = _POSITION_REFRESH_LAST_ERROR
        return {
            "status": "error",
            "provider": "etrade",
            "generated_at": _now_iso(),
            "summary": {
                "account_count": 0,
                "position_count": 0,
                "long_count": 0,
                "short_count": 0,
                "total_market_value": 0.0,
                "total_cost_basis": 0.0,
                "total_unrealized_pnl": 0.0,
                "quote_sources": [],
                "ai_status": "unavailable",
                "ai_model": None,
                "ai_blocking_reason": exc,
                "portfolio_summary": {},
            },
            "ai": {
                "status": "unavailable",
                "model": None,
                "blocking_reason": exc,
            },
            "accounts": [],
            "positions": [],
            "message": exc,
            "cache": {"hit": False, "ttl_seconds": ttl_seconds, "fresh": False},
            "refresh_in_progress": False,
            "refresh_state": refresh_state,
        }

    return {
        "status": "loading",
        "provider": "etrade",
        "generated_at": _now_iso(),
        "summary": {
            "account_count": 0,
            "position_count": 0,
            "long_count": 0,
            "short_count": 0,
            "total_market_value": 0.0,
            "total_cost_basis": 0.0,
            "total_unrealized_pnl": 0.0,
            "quote_sources": [],
            "ai_status": "unavailable",
            "ai_model": None,
            "ai_blocking_reason": "E*TRADE positions snapshot is still building in the background.",
            "portfolio_summary": {},
        },
        "ai": {
            "status": "unavailable",
            "model": None,
            "blocking_reason": "E*TRADE positions snapshot is still building in the background.",
        },
        "accounts": [],
        "positions": [],
        "message": (
            f"E*TRADE positions snapshot is building in the background after a previous refresh error: {prior_refresh_error}."
            if prior_refresh_error
            else "E*TRADE positions snapshot is building in the background. Refresh again in a moment."
        ),
        "cache": {"hit": False, "ttl_seconds": ttl_seconds, "fresh": False},
        "refresh_in_progress": refresh_state["in_progress"],
        "refresh_state": {
            **refresh_state,
            "last_error": prior_refresh_error or refresh_state["last_error"],
        },
        "last_refresh_error": prior_refresh_error,
    }
