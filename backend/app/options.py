from __future__ import annotations

import json
from datetime import datetime, timezone
from typing import Any

from .data_provider import DataProviderError, normalize_symbol
from .db import SessionLocal
from .market_session import get_market_session
from .models import OptionPositioningSnapshot
from .providers import provider_factory
from .providers.base import ProviderError
from .providers.option_positioning import snapshot_from_positioning, summarize_positioning_history


def _default_positioning_payload(symbol: str, payload: dict[str, Any], market_session: dict[str, Any]) -> dict[str, Any]:
    session_label = "Live" if market_session.get("actionable_live_quotes") else "Previous session"
    session_state = str(market_session.get("session_state") or "UNKNOWN").upper()
    return {
        "symbol": symbol,
        "provider": payload.get("provider"),
        "source": payload.get("source"),
        "timestamp": payload.get("timestamp") or datetime.now(timezone.utc).isoformat(),
        "quote_type": payload.get("quote_type"),
        "quote_timestamp": payload.get("quote_timestamp"),
        "underlying_price": payload.get("underlying_price"),
        "session_state": session_state,
        "session_label": session_label,
        "actionable_live_quotes": bool(market_session.get("actionable_live_quotes", True)),
        "selected_expiration": None,
        "classification": "Insufficient data",
        "bias": "INSUFFICIENT_DATA",
        "bias_score": 0,
        "positioning_score": 0,
        "confidence": "LOW",
        "notes": [],
        "baseline": summarize_positioning_history([]),
        "scope_count": 0,
        "scopes": {"overall": None, "selected_expiration": None, "near_term": {}, "relevant_strikes": None},
        "ratios": payload.get("ratios") or [],
        "warnings": payload.get("warnings") or [],
    }


def _load_positioning_history(db, symbol: str, limit: int = 12) -> list[dict[str, Any]]:
    rows = (
        db.query(OptionPositioningSnapshot)
        .filter(OptionPositioningSnapshot.symbol == symbol)
        .order_by(OptionPositioningSnapshot.created_at.desc())
        .limit(limit)
        .all()
    )
    snapshots: list[dict[str, Any]] = []
    for row in rows:
        try:
            snapshots.append(json.loads(row.positioning_json))
        except Exception:
            continue
    return snapshots


def _persist_positioning_snapshot(db, positioning: dict[str, Any], market_session: dict[str, Any]) -> None:
    symbol = str(positioning.get("symbol") or "").strip().upper()
    if not symbol:
        return
    overall = (positioning.get("scopes") or {}).get("overall") or {}
    values = overall.get("value") or {}
    db.add(
        OptionPositioningSnapshot(
            symbol=symbol,
            provider=str(positioning.get("provider") or "").strip() or None,
            session_state=str(positioning.get("session_state") or market_session.get("session_state") or "UNKNOWN").upper(),
            reference_session_date=str(market_session.get("reference_session_date") or "") or None,
            classification=str(positioning.get("classification") or "Insufficient data"),
            bias_score=float(positioning.get("bias_score") or 0.0),
            positioning_json=json.dumps(snapshot_from_positioning(positioning), sort_keys=True),
        )
    )
    db.commit()


def _attach_positioning_baseline(positioning: dict[str, Any], history: list[dict[str, Any]]) -> dict[str, Any]:
    baseline = summarize_positioning_history(history)
    positioning = dict(positioning)
    positioning["baseline"] = baseline
    return positioning


def _error_payload(symbol: str, message: str) -> dict[str, Any]:
    return {
        "symbol": normalize_symbol(symbol),
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "source": None,
        "provider": None,
        "warning": message,
        "warnings": [message],
    }


def get_option_expirations(symbol: str) -> list[str]:
    normalized = normalize_symbol(symbol)
    selection = provider_factory.get_options_provider()
    try:
        payload, _, _ = provider_factory.with_fallback(selection, "get_option_expirations", normalized)
        return [p.get("date") if isinstance(p, dict) else str(p) for p in payload]
    except Exception as exc:
        raise DataProviderError(str(exc)) from exc


def calculate_ratios(symbol: str, expirations_to_check: int = 3) -> dict[str, Any]:
    normalized = normalize_symbol(symbol)
    selection = provider_factory.get_options_provider()
    market_session = get_market_session()
    try:
        payload, provider_name, warning = provider_factory.with_fallback(
            selection,
            "get_options_ratios",
            normalized,
            expirations_to_check=expirations_to_check,
        )
        payload = dict(payload or {})
        payload.setdefault("provider", provider_name)
        payload.setdefault("source", provider_name)
        payload.setdefault("warnings", [])
        payload.setdefault("warning", warning)
        positioning = payload.get("positioning") or _default_positioning_payload(normalized, payload, market_session)
        with SessionLocal() as db:
            history = _load_positioning_history(db, normalized)
            positioning = _attach_positioning_baseline(positioning, history)
            payload["positioning"] = positioning
            payload["market_session"] = market_session
            try:
                _persist_positioning_snapshot(db, positioning, market_session)
            except Exception:
                pass
        return payload
    except ProviderError as exc:
        base = _error_payload(normalized, str(exc))
        positioning = _default_positioning_payload(normalized, base, market_session)
        base.update(
            {
                "expirations_checked": [],
                "ratios": [],
                "aggregate": {
                    "call_volume": 0,
                    "put_volume": 0,
                    "put_call_ratio": None,
                    "call_put_ratio": None,
                    "call_open_interest": 0,
                    "put_open_interest": 0,
                    "put_call_oi_ratio": None,
                    "bias": "NEUTRAL",
                },
                "positioning": positioning,
                "market_session": market_session,
            }
        )
        return base


def ranked_contracts(
    symbol: str,
    expirations_to_check: int = 3,
    min_volume: int = 1,
    max_spread_pct: float = 15,
    min_open_interest: int = 1,
    chart_signal: dict[str, Any] | None = None,
    options_sentiment: dict[str, Any] | None = None,
) -> dict[str, Any]:
    normalized = normalize_symbol(symbol)
    selection = provider_factory.get_options_provider()
    try:
        payload, provider_name, warning = provider_factory.with_fallback(
            selection,
            "get_ranked_contracts",
            normalized,
            expirations_to_check=expirations_to_check,
            min_volume=min_volume,
            max_spread_pct=max_spread_pct,
            min_open_interest=min_open_interest,
            chart_signal=chart_signal,
            options_sentiment=options_sentiment,
        )
        payload.setdefault("provider", provider_name)
        payload.setdefault("source", provider_name)
        payload.setdefault("warning", warning)
        payload.setdefault("warnings", [])
        return payload
    except ProviderError as exc:
        base = _error_payload(normalized, str(exc))
        base.update(
            {
                "expirations_checked": [],
                "underlying_price": 0.0,
                "calls": [],
                "puts": [],
            }
        )
        return base
