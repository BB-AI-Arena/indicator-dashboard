"""Persistent last-actual option pricing and non-executable next-open estimates."""

from __future__ import annotations

import asyncio
import hashlib
import json
import math
import re
import threading
import uuid
from datetime import date, datetime, time, timedelta, timezone
from typing import Any
from zoneinfo import ZoneInfo

from sqlalchemy import desc, text
from sqlalchemy.orm import Session

from .auth import etrade_auth
from .config import config_manager
from .db import SessionLocal
from .decision_dashboard import core_universe, build_decision_dashboard
from .market_session import get_market_session
from .models import BrokerageOrder, OptionEstimationJobLock, OptionEstimateSnapshot, PaperOrder, PaperPosition, PaperRecommendation
from .etrade_positions import get_open_option_positions
from .providers import provider_factory
from .providers.base import ProviderError


EASTERN = ZoneInfo("America/New_York")
ESTIMATE_VERSION = "option-estimation-v1"
_worker_task: asyncio.Task | None = None
_worker_lock = asyncio.Lock()
_cycle_lock = threading.Lock()
_cycle_active = False
_lock_owner = f"option-estimator-{uuid.uuid4().hex}"
_status: dict[str, Any] = {
    "running": False,
    "started_at": None,
    "last_cycle_at": None,
    "last_cycle_duration_ms": None,
    "last_error": None,
    "cycle_count": 0,
    "snapshot_count": 0,
    "tracked_contracts": 0,
    "verified_symbols": 0,
    "unresolved_symbols": [],
    "etrade_connected": False,
}


def _cfg() -> dict[str, Any]:
    return config_manager.get("option_estimation", default={}) or {}


def _number(value: Any) -> float | None:
    try:
        parsed = float(value)
        return parsed if math.isfinite(parsed) else None
    except (TypeError, ValueError):
        return None


def _timestamp(value: Any) -> datetime | None:
    if isinstance(value, datetime):
        parsed = value
    elif isinstance(value, (int, float)):
        try:
            parsed = datetime.fromtimestamp(float(value) / 1000 if float(value) > 100_000_000_000 else float(value), timezone.utc)
        except (ValueError, OSError, OverflowError):
            return None
    else:
        text = str(value or "").strip()
        if not text:
            return None
        try:
            parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
        except ValueError:
            return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def _iso(value: datetime | None) -> str | None:
    return value.isoformat() if value else None


def _iv_decimal(value: Any) -> float | None:
    number = _number(value)
    if number is None:
        return None
    return number / 100.0 if number > 3 else number


def _norm_pdf(value: float) -> float:
    return math.exp(-0.5 * value * value) / math.sqrt(2 * math.pi)


def _norm_cdf(value: float) -> float:
    return 0.5 * (1.0 + math.erf(value / math.sqrt(2.0)))


def black_scholes_price_greeks(spot: float, strike: float, years: float, volatility: float, option_type: str, rate: float = 0.04, dividend: float = 0.0) -> dict[str, float] | None:
    if min(spot, strike, years, volatility) <= 0 or option_type not in {"CALL", "PUT"}:
        return None
    sqrt_t = math.sqrt(years)
    d1 = (math.log(spot / strike) + (rate - dividend + 0.5 * volatility * volatility) * years) / (volatility * sqrt_t)
    d2 = d1 - volatility * sqrt_t
    discount = math.exp(-rate * years)
    carry = math.exp(-dividend * years)
    if option_type == "CALL":
        price = spot * carry * _norm_cdf(d1) - strike * discount * _norm_cdf(d2)
        delta = carry * _norm_cdf(d1)
        theta = (-(spot * carry * _norm_pdf(d1) * volatility / (2 * sqrt_t)) - rate * strike * discount * _norm_cdf(d2) + dividend * spot * carry * _norm_cdf(d1)) / 365
        rho = strike * years * discount * _norm_cdf(d2) / 100
    else:
        price = strike * discount * _norm_cdf(-d2) - spot * carry * _norm_cdf(-d1)
        delta = carry * (_norm_cdf(d1) - 1)
        theta = (-(spot * carry * _norm_pdf(d1) * volatility / (2 * sqrt_t)) + rate * strike * discount * _norm_cdf(-d2) - dividend * spot * carry * _norm_cdf(-d1)) / 365
        rho = -strike * years * discount * _norm_cdf(-d2) / 100
    return {
        "price": max(0.0, price),
        "delta": delta,
        "gamma": carry * _norm_pdf(d1) / (spot * volatility * sqrt_t),
        "theta": theta,
        "vega": spot * carry * _norm_pdf(d1) * sqrt_t / 100,
        "rho": rho,
    }


def implied_volatility_from_price(price: float, spot: float, strike: float, years: float, option_type: str, rate: float = 0.04, dividend: float = 0.0) -> float | None:
    if min(price, spot, strike, years) <= 0:
        return None
    low, high = 0.0001, 5.0
    for _ in range(60):
        mid = (low + high) / 2
        model = black_scholes_price_greeks(spot, strike, years, mid, option_type, rate, dividend)
        if not model:
            return None
        if model["price"] > price:
            high = mid
        else:
            low = mid
    result = (low + high) / 2
    return result if 0.0001 <= result <= 5 else None


def _expiration_timestamp(expiration: str | None) -> datetime | None:
    try:
        return datetime.combine(date.fromisoformat(str(expiration)[:10]), time(16, 0), tzinfo=EASTERN).astimezone(timezone.utc)
    except (TypeError, ValueError):
        return None


def _parse_contract_symbol(value: Any) -> dict[str, Any]:
    text = re.sub(r"\s+", "", str(value or "").upper())
    match = re.match(r"^([A-Z][A-Z0-9.\-]*?)(\d{6})([CP])(\d{8})$", text)
    if not match:
        return {"option_symbol": text or None}
    root, expiry, option_type, strike = match.groups()
    return {
        "option_symbol": text,
        "symbol": root,
        "expiration": f"20{expiry[0:2]}-{expiry[2:4]}-{expiry[4:6]}",
        "option_type": "CALL" if option_type == "C" else "PUT",
        "strike": int(strike) / 1000.0,
    }


def _contract_key(contract: dict[str, Any]) -> str:
    option_symbol = str(contract.get("option_symbol") or contract.get("contract_symbol") or "").upper()
    if option_symbol:
        return option_symbol
    return "|".join(str(contract.get(key) or "") for key in ("symbol", "expiration", "strike", "option_type"))


def _normalize_contract(raw: dict[str, Any], source_type: str) -> dict[str, Any] | None:
    parsed = _parse_contract_symbol(raw.get("option_symbol") or raw.get("contract_symbol") or raw.get("display_symbol"))
    symbol = str(raw.get("symbol") or parsed.get("symbol") or "").upper()
    option_symbol = str(raw.get("option_symbol") or raw.get("contract_symbol") or raw.get("display_symbol") or parsed.get("option_symbol") or "").upper()
    if not symbol or not option_symbol or symbol not in set(core_universe()):
        return None
    return {
        **parsed,
        **raw,
        "symbol": symbol,
        "option_symbol": option_symbol,
        "expiration": raw.get("expiration") or parsed.get("expiration"),
        "option_type": str(raw.get("option_type") or raw.get("contract_type") or parsed.get("option_type") or "").upper() or None,
        "strike": _number(raw.get("strike")) or parsed.get("strike"),
        "source_type": source_type,
    }


def _previous_snapshot(db: Session, symbol: str, option_symbol: str) -> dict[str, Any]:
    row = db.query(OptionEstimateSnapshot).filter(OptionEstimateSnapshot.symbol == symbol, OptionEstimateSnapshot.option_symbol == option_symbol).order_by(desc(OptionEstimateSnapshot.created_at)).first()
    if not row:
        return {}
    try:
        return json.loads(row.payload_json or "{}")
    except Exception:
        return {}


def _valid_bid_ask(bid: Any, ask: Any) -> bool:
    return (_number(bid) or 0) > 0 and (_number(ask) or 0) > 0 and (_number(ask) or 0) >= (_number(bid) or 0)


def _baseline(contract: dict[str, Any], previous: dict[str, Any]) -> dict[str, Any]:
    bid, ask = _number(contract.get("bid")), _number(contract.get("ask"))
    quote_timestamp = _timestamp(contract.get("quote_timestamp") or contract.get("timestamp"))
    if _valid_bid_ask(bid, ask):
        return {"price": round((bid + ask) / 2, 6), "type": "RECENT_EXECUTABLE_MIDPOINT", "timestamp": _iso(quote_timestamp), "bid": bid, "ask": ask}
    if bid and bid > 0:
        return {"price": bid, "type": "CONSERVATIVE_BID_MARK", "timestamp": _iso(quote_timestamp), "bid": bid, "ask": ask}
    if ask and ask > 0:
        return {"price": ask, "type": "CONSERVATIVE_ASK_MARK", "timestamp": _iso(quote_timestamp), "bid": bid, "ask": ask}
    last = _number(contract.get("last") or contract.get("premium") or contract.get("last_actual_option_price"))
    if last and last > 0:
        return {"price": last, "type": "LAST_ACTUAL_TRADE", "timestamp": _iso(_timestamp(contract.get("last_trade_timestamp") or quote_timestamp)), "bid": bid, "ask": ask}
    prior = _number(previous.get("last_actual_option_price") or previous.get("baseline_price"))
    if prior and prior > 0:
        return {"price": prior, "type": "PREVIOUS_SESSION_CLOSING_VALUE", "timestamp": previous.get("last_actual_timestamp") or previous.get("baseline_timestamp"), "bid": previous.get("last_actual_bid"), "ask": previous.get("last_actual_ask")}
    return {"price": None, "type": "NONE", "timestamp": None, "bid": bid, "ask": ask}


def _provider_contract(provider: Any, contract: dict[str, Any]) -> dict[str, Any]:
    expiration = contract.get("expiration")
    if not expiration or contract.get("strike") is None or contract.get("option_type") not in {"CALL", "PUT"}:
        return {"status": "UNRESOLVED_CONTRACT", "provider": getattr(provider, "name", "etrade")}
    try:
        chain = provider.get_option_chain(contract["symbol"], expiration, noOfStrikes=20, strikePriceNear=contract["strike"])
    except Exception as exc:
        return {"status": "PROVIDER_UNAVAILABLE", "provider": getattr(provider, "name", "etrade"), "error": str(exc)}
    matches = []
    for item in chain.get("contracts") or []:
        if str(item.get("type") or "").upper() != contract.get("option_type"):
            continue
        if abs((_number(item.get("strike")) or 0) - (_number(contract.get("strike")) or 0)) > 0.001:
            continue
        matches.append(item)
    if not matches:
        return {"status": "UNRESOLVED_SYMBOL", "provider": getattr(provider, "name", "etrade"), "error": "E*TRADE returned no exact strike/type match; no substitute was selected"}
    exact = next((item for item in matches if str(item.get("contract_symbol") or item.get("osi_key") or "").upper() == str(contract.get("option_symbol") or "").upper()), matches[0])
    return {"status": "ACTUAL_DATA", **exact, "provider": getattr(provider, "name", "etrade")}


def _pricing(contract: dict[str, Any], session: dict[str, Any], now: datetime, previous: dict[str, Any]) -> dict[str, Any]:
    expiration_at = _expiration_timestamp(contract.get("expiration"))
    latest_underlying = _number(contract.get("latest_underlying_price") or contract.get("underlying_price"))
    baseline_underlying = _number(contract.get("baseline_underlying_price") or contract.get("underlying_price"))
    if not expiration_at or not latest_underlying or not baseline_underlying or latest_underlying <= 0 or baseline_underlying <= 0:
        return {"quote_state": "ESTIMATE_UNAVAILABLE", "reason": "Estimate unavailable - missing required pricing inputs."}
    baseline = _baseline(contract, previous)
    baseline_price = _number(baseline.get("price"))
    option_type = contract.get("option_type")
    strike = _number(contract.get("strike"))
    if not strike or option_type not in {"CALL", "PUT"}:
        return {"quote_state": "ESTIMATE_UNAVAILABLE", "reason": "Estimate unavailable - missing required pricing inputs.", "baseline": baseline}
    rate = _number(_cfg().get("risk_free_rate")) or 0.04
    dividend = _number(_cfg().get("dividend_yield")) or 0.0
    baseline_ts = _timestamp(baseline.get("timestamp")) or now
    baseline_years = max((expiration_at - baseline_ts).total_seconds() / 31557600, 1 / 31557600)
    current_years = max((expiration_at - now).total_seconds() / 31557600, 1 / 31557600)
    next_open = _timestamp(session.get("next_market_open")) or now
    next_years = max((expiration_at - next_open).total_seconds() / 31557600, 1 / 31557600)
    iv = _iv_decimal(contract.get("implied_volatility") or previous.get("implied_volatility"))
    if baseline_price is None and iv is not None:
        theoretical_baseline = black_scholes_price_greeks(baseline_underlying, strike, baseline_years, iv, option_type, rate, dividend)
        if theoretical_baseline:
            baseline_price = theoretical_baseline["price"]
            baseline = {**baseline, "price": baseline_price, "type": "THEORETICAL_MODEL", "timestamp": _iso(baseline_ts)}
    if not baseline_price or baseline_price <= 0:
        return {"quote_state": "ESTIMATE_UNAVAILABLE", "reason": "Estimate unavailable - no actual or theoretical baseline exists.", "baseline": baseline}
    if iv is None:
        iv = implied_volatility_from_price(baseline_price, baseline_underlying, strike, baseline_years, option_type, rate, dividend)
    if iv is None:
        return {"quote_state": "ESTIMATE_UNAVAILABLE", "reason": "Estimate unavailable - implied volatility cannot be obtained or estimated.", "baseline": baseline}
    current_model = black_scholes_price_greeks(latest_underlying, strike, current_years, iv, option_type, rate, dividend)
    next_model = black_scholes_price_greeks(latest_underlying, strike, next_years, iv, option_type, rate, dividend)
    if not current_model or not next_model:
        return {"quote_state": "ESTIMATE_UNAVAILABLE", "reason": "Estimate unavailable - pricing model inputs are invalid.", "baseline": baseline}
    actual_current = baseline_price if session.get("actionable_live_quotes") and contract.get("_actual_quote_available") else None
    approx_delta = _number(contract.get("delta"))
    approx_gamma = _number(contract.get("gamma"))
    approx_theta = _number(contract.get("theta"))
    approx_vega = _number(contract.get("vega"))
    provider_greeks_available = all(value is not None for value in (approx_delta, approx_gamma, approx_theta, approx_vega))
    if not provider_greeks_available:
        approx_delta, approx_gamma, approx_theta, approx_vega = (current_model[key] for key in ("delta", "gamma", "theta", "vega"))
        greek_source = "CALCULATED_ESTIMATE"
    else:
        greek_source = "CALCULATED_ESTIMATE"  # Reprice Greeks at the latest underlying, retaining broker values in inputs.
    elapsed_days = max((now - baseline_ts).total_seconds() / 86400, 0)
    underlying_change = latest_underlying - baseline_underlying
    assumed_iv_change = _number(contract.get("assumed_iv_change")) or 0.0
    approximation = baseline_price + approx_delta * underlying_change + 0.5 * approx_gamma * underlying_change * underlying_change + approx_theta * elapsed_days + approx_vega * assumed_iv_change
    iv_shock = _number(_cfg().get("iv_shock_points")) or 5.0
    down = black_scholes_price_greeks(latest_underlying, strike, next_years, max(0.0001, iv - iv_shock / 100), option_type, rate, dividend)
    up = black_scholes_price_greeks(latest_underlying, strike, next_years, iv + iv_shock / 100, option_type, rate, dividend)
    actual_label = "ACTUAL_CURRENT" if actual_current is not None else "PREVIOUS_SESSION_ACTUAL"
    return {
        "quote_state": actual_label if actual_current is not None else ("ESTIMATED_PREMARKET" if session.get("session_state") == "PREMARKET" else "ESTIMATED_NEXT_OPEN" if session.get("session_state") == "REGULAR" else "ESTIMATED_AFTER_HOURS"),
        "baseline": baseline,
        "last_actual_option_price": _number(contract.get("last") or previous.get("last_actual_option_price")),
        "last_actual_bid": _number(contract.get("bid") or previous.get("last_actual_bid")),
        "last_actual_ask": _number(contract.get("ask") or previous.get("last_actual_ask")),
        "last_actual_midpoint": baseline_price if baseline.get("type") == "RECENT_EXECUTABLE_MIDPOINT" else _number(previous.get("last_actual_midpoint")),
        "last_actual_timestamp": _iso(_timestamp(contract.get("quote_timestamp") or contract.get("timestamp"))) or previous.get("last_actual_timestamp"),
        "estimated_current_value": actual_current or current_model["price"],
        "estimated_next_open_value": next_model["price"],
        "iv_down_value": down["price"] if down else None,
        "iv_up_value": up["price"] if up else None,
        "approximation": approximation,
        "underlying_change": underlying_change,
        "time_to_next_open_days": max((next_open - now).total_seconds() / 86400, 0),
        "implied_volatility": iv,
        "delta": current_model["delta"],
        "gamma": current_model["gamma"],
        "theta": current_model["theta"],
        "vega": current_model["vega"],
        "rho": current_model["rho"],
        "greek_source": greek_source,
        "broker_greek_source": "ETRADE" if provider_greeks_available else None,
        "greek_timestamp": _iso(now),
        "pricing_model": "Black-Scholes approximation; American early exercise and dividends are simplified",
        "input_quality": "HIGH" if contract.get("latest_underlying_timestamp") else "MEDIUM",
        "iv_shock_points": iv_shock,
        "underlying_scenarios": {
            label: (black_scholes_price_greeks(latest_underlying * factor, strike, next_years, iv, option_type, rate, dividend) or {}).get("price")
            for label, factor in (("DOWN_2PCT", 0.98), ("DOWN_1PCT", 0.99), ("UNCHANGED", 1.0), ("UP_1PCT", 1.01), ("UP_2PCT", 1.02))
        },
        "assumptions": {
            "next_open_underlying": latest_underlying,
            "iv_unchanged": iv,
            "rate": rate,
            "dividend_yield": dividend,
            "estimate_is_executable": False,
        },
    }


def _collect_tracked(db: Session, etrade_payload: dict[str, Any] | None) -> list[dict[str, Any]]:
    collected: dict[str, dict[str, Any]] = {}
    priority = {"REAL_ETRADE": 0, "REAL_ORDER": 1, "PAPER": 2, "PAPER_ORDER": 3, "RECOMMENDATION": 4, "TOP_OPPORTUNITY": 5}

    def add(raw: dict[str, Any], source: str) -> None:
        normalized = _normalize_contract(raw, source)
        if not normalized:
            return
        key = _contract_key(normalized)
        prior = collected.get(key)
        if not prior or priority.get(source, 9) < priority.get(str(prior.get("source_type")), 9):
            collected[key] = normalized
        else:
            collected[key].update({key: value for key, value in normalized.items() if value not in (None, "")})

    for position in (etrade_payload or {}).get("positions") or []:
        add({**position, "option_symbol": position.get("display_symbol") or position.get("contract_symbol"), "baseline_underlying_price": position.get("underlying_price"), "latest_underlying_price": position.get("underlying_price"), "latest_underlying_timestamp": position.get("quote_timestamp"), "quantity": position.get("quantity"), "average_cost": position.get("cost_basis"), "_actual_quote_available": not bool(position.get("quote_stale")) and (_valid_bid_ask(position.get("bid"), position.get("ask")) or _number(position.get("last")) is not None)}, "REAL_ETRADE")
    for row in db.query(BrokerageOrder).filter(BrokerageOrder.broker == "etrade").filter(BrokerageOrder.status.notin_(["CANCELLED", "REJECTED", "FILLED", "EXECUTED"])).order_by(desc(BrokerageOrder.broker_timestamp)).limit(100).all():
        try:
            payload = json.loads(row.payload_json or "{}")
        except Exception:
            payload = {}
        add({**payload, "symbol": payload.get("symbol"), "option_symbol": payload.get("option_symbol") or payload.get("contract_symbol"), "quote_timestamp": row.broker_timestamp}, "REAL_ORDER")
    for row in db.query(PaperPosition).filter(PaperPosition.status == "OPEN").all():
        try:
            payload = json.loads(row.payload_json or "{}")
        except Exception:
            payload = {}
        add({**payload, "symbol": row.symbol, "option_symbol": row.contract_symbol, "quantity": row.quantity, "average_cost": row.cost_basis, "underlying_price": payload.get("underlying_price")}, "PAPER")
    for row in db.query(PaperOrder).filter(PaperOrder.status.notin_(["CANCELLED", "REJECTED", "FILLED", "EXECUTED"])).order_by(desc(PaperOrder.created_at)).limit(100).all():
        add({"symbol": row.symbol, "option_symbol": row.contract_symbol}, "PAPER_ORDER")
    recommendations = db.query(PaperRecommendation).filter(PaperRecommendation.status.in_(["CREATED", "TRIGGERED"])).order_by(desc(PaperRecommendation.created_at)).limit(100).all()
    for row in recommendations:
        try:
            snapshot = json.loads(row.snapshot_json or "{}")
        except Exception:
            snapshot = {}
        candidate = snapshot.get("candidate") or {}
        contract = candidate.get("preferred_option_contract") or {}
        add({**contract, "symbol": row.symbol, "option_symbol": contract.get("contract") or contract.get("contract_symbol"), "underlying_price": candidate.get("current_or_previous_session_price")}, "RECOMMENDATION")
    try:
        dashboard = build_decision_dashboard(db)
        for candidate in [dashboard.get("best_long_setup"), dashboard.get("best_short_setup")]:
            if candidate:
                contract = candidate.get("preferred_option_contract") or {}
                add({**contract, "symbol": candidate.get("ticker"), "option_symbol": contract.get("contract") or contract.get("contract_symbol"), "underlying_price": candidate.get("current_or_previous_session_price")}, "TOP_OPPORTUNITY")
    except Exception:
        pass
    return sorted(collected.values(), key=lambda item: (priority.get(str(item.get("source_type")), 9), str(item.get("symbol")), str(item.get("option_symbol"))))


def _dedupe_key(payload: dict[str, Any]) -> str:
    stable = {key: payload.get(key) for key in ("quote_state", "baseline_type", "baseline_price", "baseline_timestamp", "underlying_price", "estimated_current_value", "estimated_next_open_value", "iv_down_value", "iv_up_value", "greek_source", "session_state")}
    return hashlib.sha1(json.dumps(stable, sort_keys=True, default=str).encode()).hexdigest()


def _persist(db: Session, contract: dict[str, Any], session: dict[str, Any], result: dict[str, Any], provider: str | None) -> bool:
    now = datetime.now(timezone.utc)
    payload = {
        **contract,
        **result,
        "symbol": contract.get("symbol"),
        "option_symbol": contract.get("option_symbol"),
        "session_state": session.get("session_state"),
        "calculation_timestamp": _iso(now),
        "provider": provider or "etrade",
        "model_version": ESTIMATE_VERSION,
        "estimate_is_executable": False,
        "last_actual_label": "LAST ACTUAL TRADE" if result.get("last_actual_option_price") is not None else "LAST ACTUAL DATA UNAVAILABLE",
    }
    key = _dedupe_key(payload)
    latest = db.query(OptionEstimateSnapshot).filter(OptionEstimateSnapshot.symbol == contract["symbol"], OptionEstimateSnapshot.option_symbol == contract["option_symbol"]).order_by(desc(OptionEstimateSnapshot.created_at)).first()
    if latest and latest.dedupe_key == key:
        return False
    if result.get("quote_state") == "ACTUAL_CURRENT" and latest:
        try:
            prior_payload = json.loads(latest.payload_json or "{}")
        except Exception:
            prior_payload = {}
        estimated_open = _number(prior_payload.get("estimated_next_open_value"))
        actual_open = _number(result.get("estimated_current_value"))
        if estimated_open is not None and actual_open is not None:
            result["estimated_open_comparison"] = {
                "estimated_next_open_value": estimated_open,
                "actual_opening_value": actual_open,
                "error": actual_open - estimated_open,
                "absolute_error": abs(actual_open - estimated_open),
                "percentage_error": ((actual_open - estimated_open) / estimated_open * 100.0) if estimated_open else None,
                "prior_estimate_id": latest.id,
            }
    db.add(OptionEstimateSnapshot(
        symbol=contract["symbol"], option_symbol=contract["option_symbol"], source_type=str(contract.get("source_type") or "UNKNOWN"), provider=provider or "etrade", session_state=session.get("session_state"), quote_state=str(result.get("quote_state") or "ESTIMATE_UNAVAILABLE"), baseline_type=(result.get("baseline") or {}).get("type"), baseline_price=(result.get("baseline") or {}).get("price"), baseline_timestamp=(result.get("baseline") or {}).get("timestamp"), baseline_underlying_price=contract.get("baseline_underlying_price") or contract.get("underlying_price"), underlying_price=contract.get("latest_underlying_price") or contract.get("underlying_price"), underlying_timestamp=contract.get("latest_underlying_timestamp") or contract.get("quote_timestamp"), expiration=contract.get("expiration"), strike=_number(contract.get("strike")), option_type=contract.get("option_type"), implied_volatility=_number(result.get("implied_volatility")), delta=_number(result.get("delta")), gamma=_number(result.get("gamma")), theta=_number(result.get("theta")), vega=_number(result.get("vega")), rho=_number(result.get("rho")), greek_source=result.get("greek_source"), estimated_current_value=_number(result.get("estimated_current_value")), estimated_next_open_value=_number(result.get("estimated_next_open_value")), iv_down_value=_number(result.get("iv_down_value")), iv_up_value=_number(result.get("iv_up_value")), pricing_model=result.get("pricing_model"), input_quality=result.get("input_quality"), dedupe_key=key, payload_json=json.dumps(payload, sort_keys=True, default=str), created_at=_iso(now),
    ))
    return True


def _acquire_distributed_lock() -> bool:
    now = datetime.now(timezone.utc)
    expires = now + timedelta(minutes=10)
    db = SessionLocal()
    try:
        db.execute(text("INSERT OR IGNORE INTO option_estimation_job_locks(lock_name, owner, locked_until, updated_at) VALUES ('option_estimation', NULL, NULL, :updated_at)"), {"updated_at": _iso(now)})
        result = db.execute(text("UPDATE option_estimation_job_locks SET owner = :owner, locked_until = :locked_until, updated_at = :updated_at WHERE lock_name = 'option_estimation' AND (locked_until IS NULL OR locked_until < :now OR owner = :owner)"), {"owner": _lock_owner, "locked_until": _iso(expires), "updated_at": _iso(now), "now": _iso(now)})
        db.commit()
        return bool(result.rowcount)
    except Exception:
        db.rollback()
        return False
    finally:
        db.close()


def _release_distributed_lock() -> None:
    db = SessionLocal()
    try:
        db.execute(text("UPDATE option_estimation_job_locks SET locked_until = :now, updated_at = :now WHERE lock_name = 'option_estimation' AND owner = :owner"), {"now": _iso(datetime.now(timezone.utc)), "owner": _lock_owner})
        db.commit()
    except Exception:
        db.rollback()
    finally:
        db.close()


def run_estimation_cycle() -> dict[str, Any]:
    global _cycle_active
    if not _cycle_lock.acquire(blocking=False):
        return {"status": "SKIPPED_OVERLAPPING_CYCLE"}
    if not _acquire_distributed_lock():
        _cycle_lock.release()
        return {"status": "SKIPPED_DISTRIBUTED_LOCK"}
    _cycle_active = True
    started = datetime.now(timezone.utc)
    session = get_market_session()
    try:
        etrade_payload = None
        if etrade_auth.is_connected():
            try:
                etrade_payload = get_open_option_positions(refresh=True, market_session=session)
            except Exception:
                etrade_payload = get_open_option_positions(refresh=False, market_session=session)
        db = SessionLocal()
        try:
            contracts = _collect_tracked(db, etrade_payload)
            provider = provider_factory.get_provider("etrade") if etrade_auth.is_connected() else None
            verified_symbols: set[str] = set()
            unresolved: list[str] = []
            if provider:
                for symbol in core_universe():
                    try:
                        provider.get_quote(symbol)
                        verified_symbols.add(symbol)
                    except Exception:
                        unresolved.append(symbol)
            else:
                unresolved.extend(core_universe())
            created = 0
            for contract in contracts:
                previous = _previous_snapshot(db, contract["symbol"], contract["option_symbol"])
                quote = _provider_contract(provider, contract) if provider else {"status": "ETRADE_UNAVAILABLE"}
                if quote.get("status") == "ACTUAL_DATA":
                    contract.update(quote)
                    contract["_actual_quote_available"] = True
                elif quote.get("status") in {"UNRESOLVED_SYMBOL", "UNRESOLVED_CONTRACT"}:
                    unresolved.append(contract["symbol"])
                if provider:
                    try:
                        underlying = provider.get_quote(contract["symbol"])
                        contract["latest_underlying_price"] = underlying.get("price") or contract.get("latest_underlying_price")
                        contract["latest_underlying_timestamp"] = underlying.get("timestamp") or contract.get("latest_underlying_timestamp")
                    except Exception:
                        pass
                result = _pricing(contract, session, datetime.now(timezone.utc), previous)
                created += int(_persist(db, contract, session, result, quote.get("provider") if quote else "etrade"))
            db.commit()
            elapsed = (datetime.now(timezone.utc) - started).total_seconds() * 1000
            _status.update({"last_cycle_at": _iso(datetime.now(timezone.utc)), "last_cycle_duration_ms": round(elapsed, 2), "cycle_count": int(_status.get("cycle_count", 0)) + 1, "snapshot_count": created, "tracked_contracts": len(contracts), "verified_symbols": len(verified_symbols), "unresolved_symbols": sorted(set(unresolved)), "last_error": None})
            _status["etrade_connected"] = bool(provider)
            return {"status": "COMPLETE", "contracts": len(contracts), "snapshots_created": created, "verified_symbols": sorted(verified_symbols), "unresolved_symbols": sorted(set(unresolved)), "etrade_connected": bool(provider), "latency_ms": round(elapsed, 2), "session": session}
        finally:
            db.close()
    except Exception as exc:
        _status["last_error"] = str(exc)
        return {"status": "ERROR", "error": str(exc)}
    finally:
        _cycle_active = False
        _release_distributed_lock()
        _cycle_lock.release()


def _clock(value: Any, fallback: time) -> time:
    try:
        hour, minute = [int(part) for part in str(value).split(":", 1)]
        return time(hour, minute)
    except (TypeError, ValueError):
        return fallback


def _is_daytime(now: datetime) -> bool:
    local = now.astimezone(EASTERN)
    start = _clock(_cfg().get("refresh_day_start_et"), time(7, 0))
    end = _clock(_cfg().get("refresh_day_end_et"), time(17, 30))
    return start <= local.time().replace(second=0, microsecond=0) < end


def refresh_interval_seconds(now: datetime | None = None) -> int:
    current = now or datetime.now(timezone.utc)
    session = get_market_session(current)
    if session.get("session_state") == "HOLIDAY" or current.astimezone(EASTERN).weekday() >= 5:
        return int(_cfg().get("off_hours_interval_seconds") or 1800)
    return int(_cfg().get("daytime_interval_seconds") or 180) if _is_daytime(current) else int(_cfg().get("off_hours_interval_seconds") or 1800)


async def _run_loop() -> None:
    _status["running"] = True
    _status["started_at"] = _iso(datetime.now(timezone.utc))
    while True:
        config_manager.reload()
        await asyncio.to_thread(run_estimation_cycle)
        await asyncio.sleep(refresh_interval_seconds())


def start_if_enabled() -> None:
    global _worker_task
    if not bool(_cfg().get("enabled", True)):
        return
    if _worker_task is None or _worker_task.done():
        _worker_task = asyncio.create_task(_run_loop())


async def stop() -> None:
    global _worker_task
    if _worker_task:
        _worker_task.cancel()
        try:
            await _worker_task
        except asyncio.CancelledError:
            pass
        _worker_task = None
    _status["running"] = False


def status() -> dict[str, Any]:
    return {**_status, "cycle_active": _cycle_active, "daytime_interval_seconds": 180, "off_hours_interval_seconds": 1800, "timezone": "America/New_York"}


def latest_estimates(db: Session, symbol: str | None = None, limit: int = 100) -> dict[str, Any]:
    query = db.query(OptionEstimateSnapshot)
    if symbol:
        query = query.filter(OptionEstimateSnapshot.symbol == str(symbol).upper())
    rows = query.order_by(desc(OptionEstimateSnapshot.created_at)).limit(max(1, min(limit, 500))).all()
    latest: dict[str, dict[str, Any]] = {}
    for row in rows:
        key = f"{row.symbol}|{row.option_symbol}"
        if key in latest:
            continue
        try:
            payload = json.loads(row.payload_json or "{}")
        except Exception:
            payload = {}
        payload.update({"id": row.id, "created_at": row.created_at, "symbol": row.symbol, "option_symbol": row.option_symbol, "source_type": row.source_type, "provider": row.provider, "quote_state": row.quote_state, "baseline_type": row.baseline_type, "baseline_price": row.baseline_price, "baseline_timestamp": row.baseline_timestamp, "estimated_current_value": row.estimated_current_value, "estimated_next_open_value": row.estimated_next_open_value, "iv_down_value": row.iv_down_value, "iv_up_value": row.iv_up_value, "session_state": row.session_state, "greek_source": row.greek_source, "input_quality": row.input_quality, "calculation_timestamp": row.created_at, "estimate_is_executable": False})
        latest[key] = payload
    return {"status": "ok", "estimates": list(latest.values()), "scheduler": status(), "source": "option_estimate_snapshots"}
