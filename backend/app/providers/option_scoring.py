from __future__ import annotations

from datetime import date, datetime, timezone
from typing import Any

from .option_filters import parse_expiration_date, spread_pct
from .option_positioning import alignment_score_for_side


BAD_QUOTE_TYPES = {"CLOSING", "DELAYED", "SANDBOX"}
CONFIRMING_LONG_BIASES = {"BULLISH", "NEUTRAL"}
CONFIRMING_SHORT_BIASES = {"BEARISH", "EXTREME_PUT_HEAVY", "NEUTRAL"}
CONFIRMED_SCAN_GRADES = {"TRADE_CANDIDATE", "HIGH_CONVICTION"}


def _safe_float(value: Any, fallback: float = 0.0) -> float:
    try:
        if value is None:
            return fallback
        return float(value)
    except Exception:
        return fallback


def _safe_int(value: Any, fallback: int = 0) -> int:
    try:
        if value is None:
            return fallback
        return int(value)
    except Exception:
        return fallback


def _grade(score: float) -> str:
    if score >= 85:
        return "A"
    if score >= 70:
        return "B"
    if score >= 55:
        return "C"
    return "D"


def _parse_timestamp(raw: Any) -> datetime | None:
    if raw is None:
        return None
    if isinstance(raw, datetime):
        parsed = raw
    else:
        text = str(raw).strip()
        if not text:
            return None
        try:
            parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
        except ValueError:
            return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def _downgrade(letter: str, steps: int = 1) -> str:
    order = ["A", "B", "C", "D"]
    index = min(len(order) - 1, order.index(letter) + max(0, steps))
    return order[index]


def _moneyness(option_type: str, strike: float, underlying_price: float) -> tuple[str, float, float, bool]:
    if underlying_price <= 0 or strike <= 0:
        return "UNKNOWN", 0.0, 0.0, False

    distance = strike - underlying_price
    distance_pct = (distance / underlying_price) * 100
    absolute_pct = abs(distance_pct)
    opt_type = option_type.upper()

    if absolute_pct <= 1:
        state = "ATM"
    elif opt_type == "CALL":
        state = "ITM" if strike < underlying_price else "OTM"
    elif opt_type == "PUT":
        state = "ITM" if strike > underlying_price else "OTM"
    else:
        state = "UNKNOWN"

    far_otm = state == "OTM" and absolute_pct > 5
    return state, distance, distance_pct, far_otm


def _expiration_risk(expiration: Any, today: date) -> tuple[int | None, str]:
    expiration_date = parse_expiration_date(expiration)
    if expiration_date is None:
        return None, "UNKNOWN"
    days = (expiration_date - today).days
    if days < 0:
        return days, "EXPIRED"
    if days == 0:
        return days, "0DTE"
    if days <= 2:
        return days, "VERY_SHORT_DTE"
    if days <= 7:
        return days, "SHORT_DTE"
    return days, "NORMAL"


def _chart_confirms(option_type: str, chart_signal: dict[str, Any] | None) -> bool:
    if not chart_signal:
        return False
    side = str(chart_signal.get("side") or "").upper()
    grade = str(chart_signal.get("grade") or "").upper()
    if grade not in CONFIRMED_SCAN_GRADES:
        return False
    if option_type.upper() == "CALL":
        return side == "LONG"
    if option_type.upper() == "PUT":
        return side == "SHORT"
    return False


def _sentiment_confirms(option_type: str, options_sentiment: dict[str, Any] | None) -> bool:
    if not options_sentiment:
        return False
    bias = str(options_sentiment.get("bias") or "").upper()
    if option_type.upper() == "CALL":
        return bias in CONFIRMING_LONG_BIASES
    if option_type.upper() == "PUT":
        return bias in CONFIRMING_SHORT_BIASES
    return False


def _liquidity_score(spread: float | None, volume: int, open_interest: int, quote_penalty: bool) -> float:
    score = 100.0
    if spread is None:
        score -= 45
    elif spread > 15:
        score -= 45
    elif spread > 10:
        score -= 30
    elif spread > 5:
        score -= 15

    if volume < 50:
        score -= 30
    elif volume < 100:
        score -= 18
    elif volume < 500:
        score -= 8

    if open_interest < 100:
        score -= 30
    elif open_interest < 500:
        score -= 16
    elif open_interest < 1000:
        score -= 8

    if quote_penalty:
        score -= 15

    return max(0.0, min(100.0, score))


def _risk_score(
    *,
    quote_penalty: bool,
    quote_stale: bool,
    expiration_risk: str,
    spread: float | None,
    far_otm: bool,
    delta_missing: bool,
    chart_confirmed: bool,
) -> float:
    score = 100.0
    if quote_penalty:
        score -= 25
    if quote_stale:
        score -= 25
    if expiration_risk == "0DTE":
        score -= 25
    elif expiration_risk == "VERY_SHORT_DTE":
        score -= 14
    elif expiration_risk == "SHORT_DTE":
        score -= 6
    elif expiration_risk in {"EXPIRED", "UNKNOWN"}:
        score -= 35
    if spread is None:
        score -= 20
    elif spread > 5:
        score -= 15
    if far_otm:
        score -= 12
    if delta_missing:
        score -= 10
    if not chart_confirmed:
        score -= 15
    return max(0.0, min(100.0, score))


def enrich_contract(
    contract: dict[str, Any],
    *,
    underlying_price: float,
    quote_type: str | None,
    quote_timestamp: str | None,
    today: date,
    preferred_delta_min: float,
    preferred_delta_max: float,
    chart_signal: dict[str, Any] | None,
    options_sentiment: dict[str, Any] | None,
    options_positioning: dict[str, Any] | None,
    max_quote_age_seconds: int,
    recommended_max_spread_pct: float,
    minimum_volume: int,
) -> dict[str, Any]:
    bid = _safe_float(contract.get("bid"))
    ask = _safe_float(contract.get("ask"))
    strike = _safe_float(contract.get("strike"))
    volume = _safe_int(contract.get("volume"))
    open_interest = _safe_int(contract.get("open_interest"))
    option_type = str(contract.get("type") or "").upper()
    spread = spread_pct(bid, ask)
    quote_type_upper = str(quote_type or contract.get("quote_type") or "").upper() or None

    timestamp = quote_timestamp or contract.get("timestamp")
    parsed_timestamp = _parse_timestamp(timestamp)
    quote_age_seconds = None
    if parsed_timestamp is not None:
        quote_age_seconds = max(0, int((datetime.now(timezone.utc) - parsed_timestamp).total_seconds()))

    quote_bad_type = quote_type_upper in BAD_QUOTE_TYPES if quote_type_upper else False
    quote_stale = quote_age_seconds is None or quote_age_seconds > max_quote_age_seconds
    quote_penalty = quote_bad_type or quote_stale
    moneyness, distance, distance_pct, far_otm = _moneyness(option_type, strike, underlying_price)
    days_to_expiration, expiration_risk = _expiration_risk(contract.get("expiration"), today)
    delta = contract.get("delta")
    delta_missing = delta is None
    delta_preferred = False
    if delta is not None:
        abs_delta = abs(_safe_float(delta))
        delta_preferred = preferred_delta_min <= abs_delta <= preferred_delta_max

    chart_confirmed = _chart_confirms(option_type, chart_signal)
    sentiment_confirmed_or_neutral = _sentiment_confirms(option_type, options_sentiment)
    positioning_alignment_score = alignment_score_for_side(options_positioning, "LONG" if option_type == "CALL" else "SHORT")

    liquidity_score = _liquidity_score(spread, volume, open_interest, quote_penalty)
    liquidity_grade = _grade(liquidity_score)
    risk_score = _risk_score(
        quote_penalty=quote_bad_type,
        quote_stale=quote_stale,
        expiration_risk=expiration_risk,
        spread=spread,
        far_otm=far_otm,
        delta_missing=delta_missing,
        chart_confirmed=chart_confirmed,
    )
    risk_grade = _grade(risk_score)

    setup_score = 0.0
    if chart_confirmed:
        setup_score += 45
    if sentiment_confirmed_or_neutral:
        setup_score += 20
    if delta_preferred:
        setup_score += 15
    if moneyness in {"ATM", "ITM"}:
        setup_score += 12
    elif moneyness == "OTM" and not far_otm:
        setup_score += 6
    if expiration_risk == "NORMAL":
        setup_score += 8
    setup_score += positioning_alignment_score
    setup_score = min(100.0, setup_score)

    trade_score = (liquidity_score * 0.35) + (risk_score * 0.35) + (setup_score * 0.30)
    trade_grade = _grade(trade_score)

    blockers: list[str] = []
    if not chart_confirmed:
        blockers.append("chart_signal_not_confirmed")
    chart_grade = str((chart_signal or {}).get("grade") or "").upper()
    if chart_grade not in CONFIRMED_SCAN_GRADES:
        blockers.append("chart_grade_below_trade_candidate")
    if not sentiment_confirmed_or_neutral:
        blockers.append("options_sentiment_not_confirming_or_neutral")
    if liquidity_grade not in {"A", "B"}:
        blockers.append("liquidity_grade_below_b")
    if quote_stale:
        blockers.append("quote_stale")
    if quote_bad_type:
        blockers.append("quote_type_penalized")
    if spread is None or spread > recommended_max_spread_pct:
        blockers.append("spread_not_acceptable")
    if volume < minimum_volume:
        blockers.append("volume_below_minimum")

    is_recommendation_eligible = not blockers
    if not is_recommendation_eligible and trade_grade == "A":
        trade_grade = _downgrade(trade_grade)

    contract.update(
        {
            "underlying_price": round(float(underlying_price or 0.0), 4),
            "in_the_money": moneyness == "ITM",
            "moneyness": moneyness,
            "distance_from_spot": round(distance, 4),
            "distance_from_spot_pct": round(distance_pct, 4),
            "days_to_expiration": days_to_expiration,
            "expiration_risk": expiration_risk,
            "quote_type": quote_type_upper,
            "quote_timestamp": timestamp,
            "quote_age_seconds": quote_age_seconds,
            "quote_stale": quote_stale,
            "quote_penalty": quote_penalty,
            "spread_percentage": spread,
            "delta_available": not delta_missing,
            "delta_preferred": delta_preferred,
            "far_otm": far_otm,
            "chart_signal_confirmed": chart_confirmed,
            "options_sentiment_confirmed_or_neutral": sentiment_confirmed_or_neutral,
            "options_positioning_score": positioning_alignment_score,
            "options_positioning_classification": (options_positioning or {}).get("classification"),
            "options_positioning_bias": (options_positioning or {}).get("bias"),
            "options_positioning_confidence": (options_positioning or {}).get("confidence"),
            "liquidity_score": round(liquidity_score, 2),
            "risk_score": round(risk_score, 2),
            "trade_score": round(trade_score, 2),
            "liquidity_grade": liquidity_grade,
            "risk_grade": risk_grade,
            "trade_grade": trade_grade,
            "score": round(trade_score, 2),
            "grade": trade_grade,
            "recommendation_eligible": is_recommendation_eligible,
            "recommendation_blockers": blockers,
            "recommended_max_spread_pct": recommended_max_spread_pct,
            "minimum_volume": minimum_volume,
        }
    )
    return contract
