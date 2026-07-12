from __future__ import annotations

from collections import defaultdict
from datetime import date, datetime, timezone
from statistics import mean
from typing import Any
from zoneinfo import ZoneInfo

from .option_filters import central_today, parse_expiration_date


EASTERN = ZoneInfo("America/New_York")


def _safe_float(value: Any, fallback: float | None = None) -> float | None:
    try:
        if value is None:
            return fallback
        number = float(value)
        if number != number or number in (float("inf"), float("-inf")):
            return fallback
        return number
    except Exception:
        return fallback


def _safe_int(value: Any, fallback: int | None = None) -> int | None:
    try:
        if value is None:
            return fallback
        return int(float(value))
    except Exception:
        return fallback


def _safe_text(value: Any) -> str:
    return str(value or "").strip()


def _parse_timestamp(value: Any) -> datetime | None:
    if value is None:
        return None
    if isinstance(value, datetime):
        parsed = value
    else:
        text = _safe_text(value)
        if not text:
            return None
        try:
            parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
        except Exception:
            return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def _to_et(value: Any) -> datetime | None:
    parsed = _parse_timestamp(value)
    if not parsed:
        return None
    return parsed.astimezone(EASTERN)


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _ratio(numerator: float | int | None, denominator: float | int | None) -> float | None:
    num = _safe_float(numerator)
    den = _safe_float(denominator)
    if num is None or den in (None, 0):
        return None
    return round(num / den, 4)


def _percent(part: float | int | None, total: float | int | None) -> float | None:
    numerator = _safe_float(part)
    denominator = _safe_float(total)
    if numerator is None or denominator in (None, 0):
        return None
    return round((numerator / denominator) * 100.0, 2)


def _current_option_price(contract: dict[str, Any]) -> float | None:
    bid = _safe_float(contract.get("bid"))
    ask = _safe_float(contract.get("ask"))
    last = _safe_float(contract.get("last"))
    if bid is not None and ask is not None and bid > 0 and ask > 0:
        return round((bid + ask) / 2.0, 4)
    if last is not None and last > 0:
        return round(last, 4)
    if ask is not None and ask > 0:
        return round(ask, 4)
    if bid is not None and bid > 0:
        return round(bid, 4)
    return None


def _estimated_premium_traded(contract: dict[str, Any]) -> float | None:
    volume = _safe_int(contract.get("volume"), 0) or 0
    price = _current_option_price(contract)
    if volume <= 0 or price is None:
        return None
    return round(price * volume * 100.0, 2)


def _dte(expiration: Any, now: date) -> int | None:
    parsed = parse_expiration_date(expiration)
    if not parsed:
        return None
    return (parsed - now).days


def _expiration_bucket(dte: int | None) -> str:
    if dte is None:
        return "UNKNOWN"
    if dte <= 0:
        return "EXPIRED"
    if dte <= 7:
        return "0-7"
    if dte <= 30:
        return "8-30"
    if dte <= 60:
        return "31-60"
    return "60+"


def _strike_in_range(contract: dict[str, Any], underlying_price: float | None, strike_range_pct: float) -> bool:
    strike = _safe_float(contract.get("strike"))
    if strike is None or underlying_price is None or underlying_price <= 0:
        return False
    distance_pct = abs((strike - underlying_price) / underlying_price) * 100.0
    return distance_pct <= strike_range_pct


def _classify_data_confidence(
    *,
    has_call_volume: bool,
    has_put_volume: bool,
    has_call_oi: bool,
    has_put_oi: bool,
    has_premium: bool,
    scope_count: int,
) -> str:
    coverage = sum([has_call_volume, has_put_volume, has_call_oi, has_put_oi, has_premium])
    if coverage >= 4 and scope_count >= 3:
        return "HIGH"
    if coverage >= 3 and scope_count >= 2:
        return "MEDIUM"
    if coverage >= 2:
        return "LOW"
    return "LOW"


def _scope_bias(call_volume: float, put_volume: float, call_oi: float, put_oi: float, call_premium: float, put_premium: float) -> dict[str, Any]:
    volume_total = call_volume + put_volume
    oi_total = call_oi + put_oi
    premium_total = call_premium + put_premium

    call_volume_share = _percent(call_volume, volume_total)
    put_volume_share = _percent(put_volume, volume_total)
    call_oi_share = _percent(call_oi, oi_total)
    put_oi_share = _percent(put_oi, oi_total)
    call_premium_share = _percent(call_premium, premium_total)
    put_premium_share = _percent(put_premium, premium_total)

    volume_bias = 0.0 if volume_total <= 0 else (call_volume - put_volume) / volume_total
    oi_bias = 0.0 if oi_total <= 0 else (call_oi - put_oi) / oi_total
    premium_bias = 0.0 if premium_total <= 0 else (call_premium - put_premium) / premium_total

    available = [value for value in (volume_bias, oi_bias, premium_bias) if value is not None]
    weighted_bias = round(mean(available), 4) if available else 0.0
    call_dominant = weighted_bias > 0.05
    put_dominant = weighted_bias < -0.05
    dominant_side = "CALL" if call_dominant else "PUT" if put_dominant else "NEUTRAL"

    return {
        "call_volume_share_pct": call_volume_share,
        "put_volume_share_pct": put_volume_share,
        "call_open_interest_share_pct": call_oi_share,
        "put_open_interest_share_pct": put_oi_share,
        "call_premium_share_pct": call_premium_share,
        "put_premium_share_pct": put_premium_share,
        "volume_bias": round(volume_bias, 4),
        "open_interest_bias": round(oi_bias, 4),
        "premium_bias": round(premium_bias, 4),
        "weighted_bias": weighted_bias,
        "dominant_side": dominant_side,
    }


def _scope_payload(
    *,
    label: str,
    contracts: list[dict[str, Any]],
    expiration_scope: str,
    strike_scope: str,
    source: str | None,
    quote_type: str | None,
    quote_timestamp: str | None,
    session_state: str | None,
    session_label: str,
    underlying_price: float | None,
    now: date,
) -> dict[str, Any]:
    call_contracts = [c for c in contracts if _safe_text(c.get("type")).upper() == "CALL"]
    put_contracts = [c for c in contracts if _safe_text(c.get("type")).upper() == "PUT"]

    call_volume = float(sum(_safe_int(c.get("volume"), 0) or 0 for c in call_contracts))
    put_volume = float(sum(_safe_int(c.get("volume"), 0) or 0 for c in put_contracts))
    call_oi = float(sum(_safe_int(c.get("open_interest"), 0) or 0 for c in call_contracts))
    put_oi = float(sum(_safe_int(c.get("open_interest"), 0) or 0 for c in put_contracts))
    call_premium = float(sum(_estimated_premium_traded(c) or 0.0 for c in call_contracts))
    put_premium = float(sum(_estimated_premium_traded(c) or 0.0 for c in put_contracts))

    bias = _scope_bias(call_volume, put_volume, call_oi, put_oi, call_premium, put_premium)
    volume_total = call_volume + put_volume
    oi_total = call_oi + put_oi
    premium_total = call_premium + put_premium
    has_call_volume = call_volume > 0
    has_put_volume = put_volume > 0
    has_call_oi = call_oi > 0
    has_put_oi = put_oi > 0
    has_premium = call_premium > 0 or put_premium > 0

    data_confidence = _classify_data_confidence(
        has_call_volume=has_call_volume,
        has_put_volume=has_put_volume,
        has_call_oi=has_call_oi,
        has_put_oi=has_put_oi,
        has_premium=has_premium,
        scope_count=1,
    )

    return {
        "label": label,
        "expiration_scope": expiration_scope,
        "strike_scope": strike_scope,
        "calculation_type": "volume+open_interest+estimated_premium",
        "source": source,
        "timestamp": quote_timestamp or _now_iso(),
        "session_status": session_state or "UNKNOWN",
        "session_label": session_label,
        "quote_type": quote_type,
        "underlying_price": underlying_price,
        "data_confidence": data_confidence,
        "basis": "volume, open interest, and estimated premium traded",
        "value": {
            "call_volume": int(call_volume),
            "put_volume": int(put_volume),
            "call_open_interest": int(call_oi),
            "put_open_interest": int(put_oi),
            "call_estimated_premium": round(call_premium, 2),
            "put_estimated_premium": round(put_premium, 2),
            "put_call_volume_ratio": _ratio(put_volume, call_volume),
            "call_put_volume_ratio": _ratio(call_volume, put_volume),
            "put_call_open_interest_ratio": _ratio(put_oi, call_oi),
            "call_put_open_interest_ratio": _ratio(call_oi, put_oi),
            "put_call_premium_ratio": _ratio(put_premium, call_premium),
            "call_put_premium_ratio": _ratio(call_premium, put_premium),
            "call_volume_share_pct": bias["call_volume_share_pct"],
            "put_volume_share_pct": bias["put_volume_share_pct"],
            "call_open_interest_share_pct": bias["call_open_interest_share_pct"],
            "put_open_interest_share_pct": bias["put_open_interest_share_pct"],
            "call_premium_share_pct": bias["call_premium_share_pct"],
            "put_premium_share_pct": bias["put_premium_share_pct"],
            "volume_bias": bias["volume_bias"],
            "open_interest_bias": bias["open_interest_bias"],
            "premium_bias": bias["premium_bias"],
            "weighted_bias": bias["weighted_bias"],
            "dominant_side": bias["dominant_side"],
            "total_volume": int(volume_total),
            "total_open_interest": int(oi_total),
            "total_estimated_premium": round(premium_total, 2),
            "call_contract_count": len(call_contracts),
            "put_contract_count": len(put_contracts),
        },
    }


def _score_label(weighted_bias: float, available_scopes: int, scope_conflict: bool) -> tuple[str, str, int]:
    if available_scopes <= 0:
        return "Insufficient data", "INSUFFICIENT_DATA", 0
    if scope_conflict and abs(weighted_bias) < 0.25:
        return "Conflicted", "CONFLICTED", -4
    if weighted_bias >= 0.35:
        return "Strong call bias", "CALL", 8
    if weighted_bias >= 0.15:
        return "Moderate call bias", "CALL", 4
    if weighted_bias <= -0.35:
        return "Strong put bias", "PUT", -10
    if weighted_bias <= -0.15:
        return "Moderate put bias", "PUT", -5
    return "Balanced", "NEUTRAL", 0


def _baseline_summary(snapshots: list[dict[str, Any]] | None) -> dict[str, Any]:
    if not snapshots:
        return {
            "sample_size": 0,
            "recent_average_positioning_score": None,
            "recent_average_put_call_ratio": None,
            "recent_average_call_put_ratio": None,
            "recent_average_weighted_bias": None,
            "comparison": "NO_HISTORY",
            "delta_positioning_score": None,
        }

    values = [snapshot for snapshot in snapshots if isinstance(snapshot, dict)]
    scores = [float(s.get("bias_score")) for s in values if s.get("bias_score") is not None]
    put_call = [float(s.get("put_call_ratio")) for s in values if s.get("put_call_ratio") is not None]
    call_put = [float(s.get("call_put_ratio")) for s in values if s.get("call_put_ratio") is not None]
    weighted_bias = [float(s.get("weighted_bias")) for s in values if s.get("weighted_bias") is not None]
    current = values[0] if values else {}
    current_score = float(current.get("bias_score") or 0.0)
    average_score = round(mean(scores), 4) if scores else None
    average_bias = round(mean(weighted_bias), 4) if weighted_bias else None
    average_pcr = round(mean(put_call), 4) if put_call else None
    average_cpr = round(mean(call_put), 4) if call_put else None
    delta = round(current_score - average_score, 4) if average_score is not None else None
    comparison = "NEAR_BASELINE"
    if average_score is not None:
        if delta is not None and delta > 1.5:
            comparison = "ABOVE_BASELINE"
        elif delta is not None and delta < -1.5:
            comparison = "BELOW_BASELINE"

    return {
        "sample_size": len(values),
        "recent_average_positioning_score": average_score,
        "recent_average_put_call_ratio": average_pcr,
        "recent_average_call_put_ratio": average_cpr,
        "recent_average_weighted_bias": average_bias,
        "comparison": comparison,
        "delta_positioning_score": delta,
    }


def build_option_positioning(
    *,
    symbol: str,
    contracts: list[dict[str, Any]],
    provider: str | None = None,
    underlying_price: float | None = None,
    quote_type: str | None = None,
    quote_timestamp: str | None = None,
    market_session: dict[str, Any] | None = None,
    selected_expiration: str | None = None,
    snapshots: list[dict[str, Any]] | None = None,
    strike_range_pct: float = 10.0,
) -> dict[str, Any]:
    normalized_symbol = _safe_text(symbol).upper() or None
    now_dt = central_today()
    now_iso = _now_iso()
    actionable_live_quotes = bool((market_session or {}).get("actionable_live_quotes", True))
    session_state = _safe_text((market_session or {}).get("session_state")) or "UNKNOWN"
    session_label = "Live" if actionable_live_quotes else "Previous session"
    if session_state in {"PREMARKET", "AFTER_HOURS", "MARKET_CLOSED", "HOLIDAY"}:
        session_label = "Previous session"

    contract_rows = [row for row in contracts if isinstance(row, dict)]
    expirations: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for contract in contract_rows:
        expiration = _safe_text(contract.get("expiration")) or None
        if expiration:
            expirations[expiration].append(contract)

    sorted_expirations = sorted(
        expirations.keys(),
        key=lambda exp: parse_expiration_date(exp) or date.max,
    )
    if selected_expiration is None and sorted_expirations:
        selected_expiration = sorted_expirations[0]

    scope_entries: list[dict[str, Any]] = []
    overall_scope = _scope_payload(
        label="Overall chain",
        contracts=contract_rows,
        expiration_scope="all expirations",
        strike_scope="all strikes",
        source=provider,
        quote_type=quote_type,
        quote_timestamp=quote_timestamp,
        session_state=session_state,
        session_label=session_label,
        underlying_price=underlying_price,
        now=now_dt,
    )
    scope_entries.append(overall_scope)

    if selected_expiration and selected_expiration in expirations:
        scope_entries.append(
            _scope_payload(
                label="Selected expiration",
                contracts=expirations[selected_expiration],
                expiration_scope=selected_expiration,
                strike_scope="all strikes",
                source=provider,
                quote_type=quote_type,
                quote_timestamp=quote_timestamp,
                session_state=session_state,
                session_label=session_label,
                underlying_price=underlying_price,
                now=now_dt,
            )
        )

    near_term_buckets = {
        "0-7": [],
        "8-30": [],
        "31-60": [],
    }
    for contract in contract_rows:
        dte = _dte(contract.get("expiration"), now_dt)
        bucket = _expiration_bucket(dte)
        if bucket in near_term_buckets:
            near_term_buckets[bucket].append(contract)

    relevant_strikes = []
    if underlying_price is not None and underlying_price > 0:
        for contract in contract_rows:
            if _strike_in_range(contract, underlying_price, strike_range_pct):
                relevant_strikes.append(contract)

    for bucket, rows in near_term_buckets.items():
        if rows:
            scope_entries.append(
                _scope_payload(
                    label=f"Near-term {bucket} DTE",
                    contracts=rows,
                    expiration_scope=bucket,
                    strike_scope="all strikes",
                    source=provider,
                    quote_type=quote_type,
                    quote_timestamp=quote_timestamp,
                    session_state=session_state,
                    session_label=session_label,
                    underlying_price=underlying_price,
                    now=now_dt,
                )
            )

    if relevant_strikes:
        scope_entries.append(
            _scope_payload(
                label=f"Relevant strikes within ±{strike_range_pct:.0f}%",
                contracts=relevant_strikes,
                expiration_scope="all expirations",
                strike_scope=f"±{strike_range_pct:.0f}%",
                source=provider,
                quote_type=quote_type,
                quote_timestamp=quote_timestamp,
                session_state=session_state,
                session_label=session_label,
                underlying_price=underlying_price,
                now=now_dt,
            )
        )

    overall_values = overall_scope["value"]
    weighted_bias = float(overall_values.get("weighted_bias") or 0.0)
    scope_conflict = False
    for entry in scope_entries[1:]:
        value = entry.get("value") or {}
        if not isinstance(value, dict):
            continue
        if weighted_bias == 0:
            continue
        if value.get("weighted_bias") is None:
            continue
        if float(value.get("weighted_bias") or 0.0) * weighted_bias < 0:
            scope_conflict = True
            break

    has_any_flow = bool(
        overall_values.get("total_volume", 0)
        or overall_values.get("total_open_interest", 0)
        or overall_values.get("total_estimated_premium", 0)
    )
    if not has_any_flow:
        classification, bias, bias_score = "Insufficient data", "INSUFFICIENT_DATA", 0
    else:
        classification, bias, bias_score = _score_label(weighted_bias, len(scope_entries), scope_conflict)
    if classification == "Balanced" and scope_conflict:
        classification = "Conflicted"
        bias = "CONFLICTED"
        bias_score = -4

    confidence = "LOW"
    if len(scope_entries) >= 4 and overall_values.get("total_volume", 0) and overall_values.get("total_open_interest", 0):
        confidence = "HIGH" if not scope_conflict else "MEDIUM"
    elif len(scope_entries) >= 2:
        confidence = "MEDIUM"

    baseline = _baseline_summary(snapshots)
    if baseline["comparison"] == "ABOVE_BASELINE" and bias == "CALL" and bias_score < 8:
        bias_score = min(8, bias_score + 1)
    elif baseline["comparison"] == "BELOW_BASELINE" and bias == "PUT" and bias_score > -10:
        bias_score = max(-10, bias_score - 1)

    notes: list[str] = []
    if not actionable_live_quotes:
        notes.append("Previous-session options positioning. Refresh after the options market opens before using this as an entry signal.")
    if overall_values.get("put_call_volume_ratio") is not None:
        notes.append(f"Put/call volume ratio: {overall_values['put_call_volume_ratio']:.2f}")
    if overall_values.get("put_call_premium_ratio") is not None:
        notes.append(f"Put/call premium ratio: {overall_values['put_call_premium_ratio']:.2f}")

    return {
        "symbol": normalized_symbol,
        "provider": provider,
        "source": provider,
        "timestamp": quote_timestamp or now_iso,
        "quote_type": quote_type,
        "quote_timestamp": quote_timestamp,
        "underlying_price": underlying_price,
        "session_state": session_state,
        "session_label": session_label,
        "actionable_live_quotes": actionable_live_quotes,
        "selected_expiration": selected_expiration,
        "classification": classification,
        "bias": bias,
        "bias_score": bias_score,
        "positioning_score": bias_score,
        "confidence": confidence,
        "notes": notes,
        "baseline": baseline,
        "scope_count": len(scope_entries),
        "scopes": {
            "overall": overall_scope,
            "selected_expiration": scope_entries[1] if len(scope_entries) > 1 and (scope_entries[1].get("label") == "Selected expiration") else None,
            "near_term": {entry["expiration_scope"]: entry for entry in scope_entries if str(entry.get("label", "")).startswith("Near-term")},
            "relevant_strikes": next((entry for entry in scope_entries if str(entry.get("label", "")).startswith("Relevant strikes")), None),
        },
        "ratios": [
            {
                "expiration": exp,
                "value": payload["value"],
                "label": payload["label"],
                "expiration_scope": payload["expiration_scope"],
                "strike_scope": payload["strike_scope"],
                "calculation_type": payload["calculation_type"],
                "source": payload["source"],
                "timestamp": payload["timestamp"],
                "session_status": payload["session_status"],
                "session_label": payload["session_label"],
                "data_confidence": payload["data_confidence"],
            }
            for exp, payload in (
                (
                    contract_exp,
                    _scope_payload(
                        label=f"Expiration {contract_exp}",
                        contracts=exp_rows,
                        expiration_scope=contract_exp,
                        strike_scope="all strikes",
                        source=provider,
                        quote_type=quote_type,
                        quote_timestamp=quote_timestamp,
                        session_state=session_state,
                        session_label=session_label,
                        underlying_price=underlying_price,
                        now=now_dt,
                    ),
                )
                for contract_exp, exp_rows in expirations.items()
            )
        ],
        "warnings": [],
    }


def serializable_snapshot(positioning: dict[str, Any]) -> dict[str, Any]:
    return {
        "symbol": positioning.get("symbol"),
        "provider": positioning.get("provider"),
        "session_state": positioning.get("session_state"),
        "reference_session_date": (positioning.get("baseline") or {}).get("reference_session_date"),
        "classification": positioning.get("classification"),
        "bias_score": positioning.get("bias_score"),
        "put_call_ratio": (positioning.get("scopes") or {}).get("overall", {}).get("value", {}).get("put_call_volume_ratio"),
        "call_put_ratio": (positioning.get("scopes") or {}).get("overall", {}).get("value", {}).get("call_put_volume_ratio"),
        "weighted_bias": (positioning.get("scopes") or {}).get("overall", {}).get("value", {}).get("weighted_bias"),
    }


def alignment_score_for_side(positioning: dict[str, Any] | None, side: str | None) -> int:
    if not positioning:
        return 0
    bias_score = _safe_int(positioning.get("bias_score"), 0) or 0
    if bias_score == 0:
        return 0
    normalized_side = _safe_text(side).upper()
    if normalized_side == "SHORT":
        bias_score *= -1
    return max(-10, min(8, bias_score))


def summarize_positioning_history(snapshots: list[dict[str, Any]] | None) -> dict[str, Any]:
    return _baseline_summary(snapshots)


def snapshot_from_positioning(positioning: dict[str, Any]) -> dict[str, Any]:
    overall = (positioning.get("scopes") or {}).get("overall", {}) if isinstance(positioning, dict) else {}
    values = overall.get("value") or {}
    return {
        "symbol": positioning.get("symbol"),
        "provider": positioning.get("provider"),
        "session_state": positioning.get("session_state"),
        "reference_session_date": None,
        "classification": positioning.get("classification"),
        "bias_score": positioning.get("bias_score"),
        "put_call_ratio": values.get("put_call_volume_ratio"),
        "call_put_ratio": values.get("call_put_volume_ratio"),
        "weighted_bias": values.get("weighted_bias"),
    }
