from __future__ import annotations

import json
import math
from datetime import datetime, timezone
from typing import Any

from sqlalchemy import desc
from sqlalchemy.orm import Session

from .config import config_manager
from .history import now_iso
from .market_session import get_market_session
from .models import (
    Candle,
    HistoricalSetupFamily,
    HistoricalSetupFeature,
    NewsCatalystSnapshot,
    OptionPositioningSnapshot,
    Scan,
    TickerProfile,
    Watchlist,
)
from .profile_completeness import evaluate_profile_completeness
from .recommendation_performance import get_recommendation_performance, record_candidates
from .ticker_profiles import ensure_ticker_profile, serialize_ticker_profile


CORE_UNIVERSE_DEFAULT = [
    "AAPL",
    "MSFT",
    "NVDA",
    "AMZN",
    "GOOGL",
    "META",
    "TSLA",
    "PLTR",
    "SPCX",
    "CRM",
    "CAT",
    "JPM",
    "PANW",
    "CRWD",
]


def _cfg() -> dict[str, Any]:
    return config_manager.get("decision_dashboard", default={}) or {}


def core_universe() -> list[str]:
    configured = _cfg().get("core_universe") or CORE_UNIVERSE_DEFAULT
    symbols: list[str] = []
    seen: set[str] = set()
    for item in configured:
        symbol = str(item or "").strip().upper()
        if not symbol or symbol in seen:
            continue
        seen.add(symbol)
        symbols.append(symbol)
    return symbols


def _json_loads(raw: str | None, fallback: Any) -> Any:
    if not raw:
        return fallback
    try:
        return json.loads(raw)
    except Exception:
        return fallback


def _safe_float(value: Any, default: float | None = None) -> float | None:
    try:
        if value is None:
            return default
        number = float(value)
        if math.isnan(number) or math.isinf(number):
            return default
        return number
    except Exception:
        return default


def _safe_int(value: Any, default: int = 0) -> int:
    try:
        if value is None:
            return default
        return int(float(value))
    except Exception:
        return default


def _score_for_sort(item: dict[str, Any]) -> float:
    value = _safe_float(item.get("score"), None)
    return value if value is not None else float("-inf")


def _parse_iso(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except Exception:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def _now() -> datetime:
    return datetime.now(timezone.utc)


def ensure_core_universe(db: Session) -> list[str]:
    symbols = core_universe()
    now = now_iso()
    for symbol in symbols:
        row = db.query(Watchlist).filter(Watchlist.symbol == symbol).first()
        if row:
            row.active = True
            if not row.source:
                row.source = "core_universe"
        else:
            db.add(Watchlist(symbol=symbol, source="core_universe", active=True, added_at=now))
        profile = ensure_ticker_profile(db, symbol, source="core_universe")
        if not profile.last_completeness_check:
            evaluate_profile_completeness(db, profile, market_session=get_market_session(), persist=True)
    db.commit()
    return symbols


def _latest_scan(db: Session, symbol: str) -> Scan | None:
    return db.query(Scan).filter(Scan.symbol == symbol).order_by(desc(Scan.created_at)).first()


def _latest_feature(db: Session, symbol: str) -> HistoricalSetupFeature | None:
    return (
        db.query(HistoricalSetupFeature)
        .filter(HistoricalSetupFeature.symbol == symbol)
        .filter(HistoricalSetupFeature.interval == "15m")
        .order_by(desc(HistoricalSetupFeature.timestamp))
        .first()
    )


def _latest_option_positioning(db: Session, symbol: str) -> OptionPositioningSnapshot | None:
    return (
        db.query(OptionPositioningSnapshot)
        .filter(OptionPositioningSnapshot.symbol == symbol)
        .order_by(desc(OptionPositioningSnapshot.created_at))
        .first()
    )


def _latest_news(db: Session, symbol: str) -> NewsCatalystSnapshot | None:
    return (
        db.query(NewsCatalystSnapshot)
        .filter(NewsCatalystSnapshot.symbol == symbol)
        .order_by(desc(NewsCatalystSnapshot.updated_at))
        .first()
    )


def _family_stats(db: Session, setup_name: str | None) -> dict[str, Any]:
    if not setup_name:
        return {}
    family = (
        db.query(HistoricalSetupFamily)
        .filter(HistoricalSetupFamily.setup_name == setup_name)
        .order_by(desc(HistoricalSetupFamily.updated_at))
        .first()
    )
    if not family:
        return {}
    stats = _json_loads(family.stats_json, {})
    stats.setdefault("sample_size", family.sample_size)
    stats.setdefault("confidence", family.confidence)
    stats.setdefault("last_recalculated_at", family.last_recalculated_at)
    return stats


def _profile_interval_rows(profile_payload: dict[str, Any], interval: str) -> int:
    return _safe_int((((profile_payload.get("data_coverage") or {}).get("intervals") or {}).get(interval) or {}).get("rows"), 0)


def _profile_interval_last(profile_payload: dict[str, Any], interval: str) -> str | None:
    return (((profile_payload.get("data_coverage") or {}).get("intervals") or {}).get(interval) or {}).get("last")


def _profile_has_fibonacci(profile_payload: dict[str, Any]) -> bool:
    fib = ((profile_payload.get("stats") or {}).get("fibonacci_behavior") or {})
    return _safe_int(fib.get("interaction_records"), 0) > 0 or str(fib.get("data_status") or "").lower() == "observed"


def _profile_setup_stats(profile_payload: dict[str, Any], setup_name: str | None) -> dict[str, Any]:
    families = (((profile_payload.get("stats") or {}).get("setup_history") or {}).get("families") or [])
    if setup_name:
        for row in families:
            if row.get("setup_family") == setup_name:
                return dict(row)
    return dict(families[0]) if families else {}


def _news_is_current(news: NewsCatalystSnapshot | None) -> bool:
    if not news:
        return False
    max_age = int(_cfg().get("news_current_max_age_hours", 168) or 168)
    updated = _parse_iso(news.updated_at)
    if not updated:
        return False
    return (_now() - updated).total_seconds() <= max_age * 3600


def _contract_from_profile(profile_payload: dict[str, Any], side: str) -> dict[str, Any] | None:
    latest = profile_payload.get("latest_setup_state") or {}
    selection = latest.get("contract_selection") or {}
    best = selection.get("best_contract") or selection.get("selected_contract") or {}
    if isinstance(best, dict) and best.get("contract"):
        return best
    contract_type = "CALL" if side == "LONG" else "PUT" if side == "SHORT" else None
    preferred = latest.get("preferred_contract") or {}
    if isinstance(preferred, dict) and preferred.get("contract"):
        return preferred
    if contract_type:
        return {
            "contract": f"{contract_type} candidate pending live chain validation",
            "type": contract_type,
            "status": "PENDING_VALIDATION",
            "reason": "No cached validated contract snapshot is available.",
        }
    return None


def _option_snapshot_payload(snapshot: OptionPositioningSnapshot | None) -> dict[str, Any]:
    if not snapshot:
        return {"data_status": "unavailable"}
    payload = _json_loads(snapshot.positioning_json, {})
    overall = ((payload.get("scopes") or {}).get("overall") or {}).get("value") or {}
    relevant = ((payload.get("scopes") or {}).get("relevant_strikes") or {}).get("value") or {}
    selected_exp = ((payload.get("scopes") or {}).get("selected_expiration") or {}).get("value") or {}
    return {
        "provider": snapshot.provider,
        "session_state": snapshot.session_state,
        "classification": snapshot.classification or payload.get("classification"),
        "bias_score": snapshot.bias_score,
        "created_at": snapshot.created_at,
        "put_call_volume_ratio": overall.get("put_call_volume_ratio"),
        "call_put_volume_ratio": overall.get("call_put_volume_ratio"),
        "put_call_open_interest_ratio": overall.get("put_call_open_interest_ratio"),
        "near_the_money_premium_split": relevant.get("premium_split"),
        "selected_expiration_positioning": selected_exp.get("classification") or selected_exp.get("bias"),
        "positioning_bias": payload.get("classification") or snapshot.classification,
        "confidence": payload.get("confidence") or "LOW",
        "data_status": "observed",
    }


def _latest_price(feature_payload: dict[str, Any], scan: Scan | None) -> float | None:
    price = _safe_float(feature_payload.get("price"))
    if price is not None:
        return price
    return _safe_float(scan.price if scan else None)


def _targets(side: str, features: dict[str, Any], stats: dict[str, Any]) -> list[dict[str, Any]]:
    price = _safe_float(features.get("price"), 0.0) or 0.0
    atr = _safe_float(features.get("atr"), 0.0) or 0.0
    support = _safe_float(features.get("support"))
    resistance = _safe_float(features.get("resistance"))
    hit_rate = stats.get("raw_hit_rate") if stats.get("raw_hit_rate") is not None else stats.get("raw_success_rate")
    sample = _safe_int(stats.get("occurrence_count") or stats.get("sample_size") or stats.get("examples"), 0)
    confidence = stats.get("confidence") or "INSUFFICIENT"
    if side == "SHORT":
        first = support if support and support < price else price - max(atr, price * 0.008)
        second = first - max(atr, price * 0.008)
        sources = ["nearest support / ATR projection", "ATR continuation / prior support"]
    else:
        first = resistance if resistance and resistance > price else price + max(atr, price * 0.008)
        second = first + max(atr, price * 0.008)
        sources = ["nearest resistance / ATR projection", "ATR continuation / prior resistance"]
    return [
        {
            "price": round(first, 2),
            "source": sources[0],
            "likelihood_before_invalidation": hit_rate,
            "sample_size": sample,
            "estimated_option_value": None,
            "time_assumption": "next session to 3 sessions",
            "iv_assumption": "unchanged; no executable option estimate without validated contract Greeks",
            "confidence": confidence,
        },
        {
            "price": round(second, 2),
            "source": sources[1],
            "likelihood_before_invalidation": hit_rate,
            "sample_size": sample,
            "estimated_option_value": None,
            "time_assumption": "3 to 5 sessions",
            "iv_assumption": "unchanged; no executable option estimate without validated contract Greeks",
            "confidence": confidence,
        },
    ]


def _entry_and_invalidation(side: str, features: dict[str, Any]) -> tuple[dict[str, Any], dict[str, Any]]:
    price = _safe_float(features.get("price"), 0.0) or 0.0
    atr = _safe_float(features.get("atr"), 0.0) or 0.0
    support = _safe_float(features.get("support"))
    resistance = _safe_float(features.get("resistance"))
    vwap = _safe_float(features.get("vwap"))
    rel_volume = max(1.2, float(_cfg().get("confirmation_relative_volume", 1.3) or 1.3))
    if side == "SHORT":
        trigger = support if support and support > 0 else price - max(atr * 0.25, price * 0.002)
        invalidation = max(vwap or price, resistance or price, price + max(atr * 0.75, price * 0.006))
        entry_type = "breakdown"
        entry_text = f"Completed 15-minute close below {trigger:.2f} with relative volume above {rel_volume:.1f}."
        invalidation_text = f"Completed 15-minute close above {invalidation:.2f} invalidates the short thesis."
    else:
        trigger = resistance if resistance and resistance > 0 else price + max(atr * 0.25, price * 0.002)
        invalidation = min(vwap or price, support or price, price - max(atr * 0.75, price * 0.006))
        entry_type = "breakout"
        entry_text = f"Completed 15-minute close above {trigger:.2f} with relative volume above {rel_volume:.1f}."
        invalidation_text = f"Completed 15-minute close below {invalidation:.2f} invalidates the long thesis."
    return (
        {"type": entry_type, "price": round(trigger, 2), "condition": entry_text},
        {"price": round(invalidation, 2), "condition": invalidation_text},
    )


def _evidence(side: str, features: dict[str, Any], scan: Scan | None, options: dict[str, Any], news_payload: dict[str, Any]) -> tuple[list[str], list[str]]:
    supporting: list[str] = []
    conflicts: list[str] = []
    close_vwap = _safe_float(features.get("close_vwap_atr") or features.get("distance_from_vwap_atr"))
    ema_fast_slow = _safe_float(features.get("ema_fast_slow_atr"))
    relative_volume = _safe_float(features.get("relative_volume"))
    rs_qqq = _safe_float(features.get("relative_strength_qqq"))
    positioning = str(options.get("positioning_bias") or options.get("classification") or "").lower()
    news_impact = str(news_payload.get("impact_label") or news_payload.get("position_impact") or "").lower()

    if side == "LONG":
        if close_vwap is not None and close_vwap > 0:
            supporting.append("price is above VWAP")
        elif close_vwap is not None and close_vwap < 0:
            conflicts.append("price is below VWAP")
        if ema_fast_slow is not None and ema_fast_slow > 0:
            supporting.append("EMA alignment is bullish")
        if rs_qqq is not None and rs_qqq > 0:
            supporting.append("relative strength versus QQQ is positive")
        if "call" in positioning:
            supporting.append("options positioning leans call-heavy")
        elif "put" in positioning:
            conflicts.append("options positioning leans put-heavy")
        if "conflict" in news_impact or "negative" in news_impact:
            conflicts.append("recent news reaction conflicts with the long thesis")
    else:
        if close_vwap is not None and close_vwap < 0:
            supporting.append("price is below VWAP")
        elif close_vwap is not None and close_vwap > 0:
            conflicts.append("price is above VWAP")
        if ema_fast_slow is not None and ema_fast_slow < 0:
            supporting.append("EMA alignment is bearish")
        if rs_qqq is not None and rs_qqq < 0:
            supporting.append("relative weakness versus QQQ is negative")
        if "put" in positioning:
            supporting.append("options positioning leans put-heavy")
        elif "call" in positioning:
            conflicts.append("options positioning leans call-heavy")
        if "support" in news_impact or "positive" in news_impact:
            conflicts.append("recent news reaction conflicts with the short thesis")

    if relative_volume is not None and relative_volume >= 1.2:
        supporting.append(f"relative volume is {relative_volume:.1f}x")
    if scan and scan.grade in {"TRADE_CANDIDATE", "HIGH_CONVICTION"}:
        supporting.append(f"latest chart scan grade is {scan.grade}")
    elif scan and scan.grade in {"NO_TRADE", "WATCH"}:
        conflicts.append(f"latest chart scan grade is only {scan.grade}")
    return supporting[:4], conflicts[:2]


def _probability_summary(stats: dict[str, Any]) -> dict[str, Any]:
    examples = _safe_int(stats.get("occurrence_count") or stats.get("sample_size") or stats.get("examples"), 0)
    rate = stats.get("raw_hit_rate") if stats.get("raw_hit_rate") is not None else stats.get("raw_success_rate")
    successes = _safe_int(stats.get("success_count") or stats.get("successes"), 0)
    if not successes and rate is not None and examples:
        successes = int(round(float(rate) * examples))
    return {
        "sample_size": examples,
        "successes": successes,
        "target_before_invalidation_rate": rate,
        "raw_hit_rate": rate,
        "out_of_sample_result": stats.get("out_of_sample_success_rate"),
        "confidence_interval": stats.get("confidence_interval"),
        "average_next_session_move": stats.get("average_return_pct"),
        "median_next_session_move": stats.get("median_return_pct"),
        "mfe": stats.get("mfe_pct") or stats.get("average_mfe_pct"),
        "mae": stats.get("mae_pct") or stats.get("average_mae_pct"),
        "expected_value": stats.get("expected_value_pct"),
        "confidence": stats.get("confidence") or "INSUFFICIENT",
        "display": (
            f"Historically, {successes} of {examples} comparable setups reached target before invalidation. "
            f"Estimated probability: {float(rate) * 100:.1f}%, {str(stats.get('confidence') or 'insufficient').lower()} confidence."
            if examples and rate is not None
            else "Insufficient historical evidence; no probability is displayed."
        ),
    }


def _next_session_bias(side: str, stats: dict[str, Any], news_payload: dict[str, Any]) -> str:
    if not stats:
        return "INSUFFICIENT DATA"
    confidence = str(stats.get("confidence") or "").upper()
    examples = _safe_int(stats.get("occurrence_count") or stats.get("sample_size") or stats.get("examples"), 0)
    rate = stats.get("raw_hit_rate") if stats.get("raw_hit_rate") is not None else stats.get("raw_success_rate")
    if examples < int(_cfg().get("min_historical_examples", 10) or 10) or rate is None or confidence == "INSUFFICIENT":
        return "INSUFFICIENT DATA"
    news_impact = str(news_payload.get("impact_label") or news_payload.get("position_impact") or "").upper()
    if "HIGH UNCERTAINTY" in news_impact or "EVENT" in news_impact:
        return "EVENT-DRIVEN / HIGH UNCERTAINTY"
    if float(rate) < 0.52:
        return "RANGE / NO EDGE"
    if side == "SHORT":
        return "LIKELY BEARISH CONTINUATION" if float(rate) >= 0.58 else "LIKELY PULLBACK"
    return "LIKELY BULLISH CONTINUATION" if float(rate) >= 0.58 else "LIKELY BOUNCE"


def _data_freshness(profile_payload: dict[str, Any], scan: Scan | None, option_snapshot: OptionPositioningSnapshot | None) -> dict[str, Any]:
    return {
        "profile_updated_at": profile_payload.get("last_profile_update_at"),
        "latest_15m_candle": _profile_interval_last(profile_payload, "15m"),
        "latest_daily_candle": _profile_interval_last(profile_payload, "1d"),
        "latest_scan_at": scan.created_at if scan else None,
        "latest_option_snapshot_at": option_snapshot.created_at if option_snapshot else None,
    }


def _hard_gates(
    *,
    profile: TickerProfile | None,
    profile_payload: dict[str, Any] | None,
    feature: HistoricalSetupFeature | None,
    setup_stats: dict[str, Any],
    option_snapshot: OptionPositioningSnapshot | None,
    news: NewsCatalystSnapshot | None,
    contract: dict[str, Any] | None,
    live_session: bool = False,
) -> list[str]:
    cfg = _cfg()
    gates: list[str] = []
    if not profile or not profile_payload:
        return ["profile_missing"]
    if bool(cfg.get("require_profile_complete", True)):
        readiness = profile_payload.get("planning_ready") if profile_payload else False
        live_ready = profile_payload.get("live_ready") if profile_payload else False
        if live_session and not live_ready:
            gates.append("live_profile_not_ready")
        elif not readiness:
            gates.append("planning_profile_not_ready")
    required = cfg.get("required_intervals") or {}
    for interval, interval_cfg in required.items():
        rows = _profile_interval_rows(profile_payload, str(interval))
        minimum = _safe_int((interval_cfg or {}).get("min_rows"), 0)
        if rows < minimum:
            gates.append(f"history_{interval}_incomplete")
    if not feature:
        gates.append("latest_setup_missing")
    if bool(cfg.get("require_fibonacci_behavior", True)) and not _profile_has_fibonacci(profile_payload):
        gates.append("fibonacci_behavior_unavailable")
    min_examples = int(cfg.get("min_historical_examples", 10) or 10)
    sample = _safe_int(setup_stats.get("occurrence_count") or setup_stats.get("sample_size") or setup_stats.get("examples"), 0)
    if sample < min_examples:
        gates.append("insufficient_historical_sample")
    expected_value = _safe_float(setup_stats.get("expected_value_pct"))
    min_ev = _safe_float(cfg.get("min_expected_value_pct"), 0.01) or 0.01
    if expected_value is None or expected_value < min_ev:
        gates.append("expected_value_not_positive")
    if not option_snapshot:
        gates.append("options_chain_snapshot_missing")
    if bool(cfg.get("require_news_current", True)) and not _news_is_current(news):
        gates.append("news_state_not_current")
    if not contract or contract.get("status") == "PENDING_VALIDATION":
        gates.append("validated_contract_missing")
    return gates


def _candidate(db: Session, symbol: str, session: dict[str, Any]) -> dict[str, Any]:
    profile = db.query(TickerProfile).filter(TickerProfile.symbol == symbol).first()
    if not profile:
        ensure_ticker_profile(db, symbol, source="decision_dashboard")
        db.commit()
        profile = db.query(TickerProfile).filter(TickerProfile.symbol == symbol).first()
    if profile and not profile.last_completeness_check:
        evaluate_profile_completeness(db, profile, market_session=session, persist=True)
    profile_payload = serialize_ticker_profile(profile) if profile else None
    scan = _latest_scan(db, symbol)
    feature = _latest_feature(db, symbol)
    feature_payload = _json_loads(feature.features_json, {}) if feature else {}
    side = str(feature.direction if feature else (scan.side if scan else "UNKNOWN") or "UNKNOWN").upper()
    if side not in {"LONG", "SHORT"}:
        side = str(scan.side if scan else "UNKNOWN").upper()
    setup_stats = _profile_setup_stats(profile_payload or {}, feature.setup_family if feature else None)
    if not setup_stats:
        setup_stats = _family_stats(db, feature.setup_family if feature else None)
    option_snapshot = _latest_option_positioning(db, symbol)
    options_payload = _option_snapshot_payload(option_snapshot)
    news = _latest_news(db, symbol)
    news_payload = _json_loads(news.payload_json, {}) if news else {}
    social_payload = ((profile_payload or {}).get("stats") or {}).get("social_history") or {}
    contract = _contract_from_profile(profile_payload or {}, side)
    gates = _hard_gates(
        profile=profile,
        profile_payload=profile_payload,
        feature=feature,
        setup_stats=setup_stats,
        option_snapshot=option_snapshot,
        news=news,
        contract=contract,
        live_session=bool(session.get("actionable_live_quotes")),
    )
    entry, invalidation = _entry_and_invalidation(side, feature_payload)
    targets = _targets(side, feature_payload, setup_stats)
    supporting, conflicts = _evidence(side, feature_payload, scan, options_payload, news_payload)
    probability = _probability_summary(setup_stats)
    price = _latest_price(feature_payload, scan)
    setup_state = feature.setup_state if feature else "DATA INSUFFICIENT"
    passes_hard_gates = not gates and side in {"LONG", "SHORT"}
    profile_state = str((profile_payload or {}).get("profile_state") or (profile_payload or {}).get("profile_status") or "NOT_STARTED")
    if not profile_payload:
        status = "NOT_STARTED"
    elif profile_state in {"NOT_STARTED", "BUILDING", "PARTIAL", "ANALYSIS_PENDING", "BLOCKED", "ERROR", "STALE"}:
        status = profile_state
    elif gates:
        status = "DATA REFRESH REQUIRED" if session.get("actionable_live_quotes") else "NEXT-SESSION WATCH"
    elif setup_state in {"CONFIRMING", "FORMING"}:
        status = "NEXT-SESSION WATCH" if not session.get("actionable_live_quotes") else "WAITING"
    else:
        status = "READY FOR LIVE ANALYSIS" if session.get("actionable_live_quotes") else "READY FOR PLANNING"

    scoring_complete = bool(profile_payload and profile_payload.get("planning_ready") and scan and scan.score is not None and setup_stats and probability.get("expected_value") is not None)
    score: float | None = 0.0 if scoring_complete else None
    if scoring_complete:
        score += (_safe_float(probability.get("expected_value")) or 0.0) * 10
        score += (_safe_float(probability.get("target_before_invalidation_rate")) or 0.0) * 40
        score += max(0.0, _safe_float(scan.score) or 0.0) * 3
        score += max(0.0, _safe_float(options_payload.get("bias_score")) or 0.0) if side == "LONG" else max(0.0, -(_safe_float(options_payload.get("bias_score")) or 0.0))
    # Social intelligence is deliberately capped at a small contribution and
    # cannot affect hard-gate eligibility.
    if social_payload.get("classification") not in {"HYPE RISK", "PANIC RISK", "INSUFFICIENT DATA"}:
        social_score = _safe_float(social_payload.get("sentiment_score"), 0.0) or 0.0
        if score is not None:
            score += max(-5.0, min(5.0, social_score * 0.05))
    if score is not None:
        score -= len(gates) * 15

    if passes_hard_gates:
        primary_reason = supporting[0] if supporting else "deterministic gates passed"
        primary_risk = conflicts[0] if conflicts else "setup fails at invalidation or if live option spread is unacceptable"
    else:
        primary_reason = "profile and cached data are still building" if status in {"NOT_STARTED", "BUILDING", "PARTIAL", "ANALYSIS_PENDING"} else "required hard gates are incomplete"
        primary_risk = gates[0] if gates else "insufficient evidence"

    return {
        "ticker": symbol,
        "direction": side,
        "setup_name": feature.setup_family if feature else None,
        "status": status,
        "conviction": probability.get("confidence") or "INSUFFICIENT",
        "current_or_previous_session_price": price,
        "next_session_bias": _next_session_bias(side, setup_stats, news_payload),
        "historical_match": probability,
        "expected_value_estimate": probability.get("expected_value"),
        "entry_trigger": entry,
        "invalidation": invalidation,
        "targets": targets,
        "preferred_option_contract": contract,
        "maximum_acceptable_option_entry": contract.get("max_reasonable_entry") if isinstance(contract, dict) else None,
        "primary_reason": primary_reason,
        "primary_risk": primary_risk,
        "data_freshness": _data_freshness(profile_payload or {}, scan, option_snapshot),
        "supporting_factors": supporting,
        "conflicting_factors": conflicts,
        "options_positioning": options_payload,
        "news_impact": {
            "status": "current" if _news_is_current(news) else "missing_or_stale",
            "latest_updated_at": news.updated_at if news else None,
            "impact_label": news_payload.get("impact_label") or news_payload.get("position_impact"),
        },
        "social_narrative": social_payload,
        "hard_gates": gates,
        "passes_hard_gates": passes_hard_gates,
        "score": round(score, 2) if score is not None else None,
        "score_status": "COMPLETE" if scoring_complete and not gates else "PARTIAL" if profile_payload else "UNAVAILABLE",
        "profile_status": profile_state if profile else "NOT_STARTED",
        "profile_state": profile_state,
        "profile_completeness": (profile_payload or {}).get("readiness") if profile_payload else None,
        "profile_summary": profile_payload,
    }


def _market_state(candidates: list[dict[str, Any]], session: dict[str, Any]) -> dict[str, Any]:
    spy = next((item for item in candidates if item["ticker"] == "SPY"), None)
    qqq = next((item for item in candidates if item["ticker"] == "QQQ"), None)
    longs = [item for item in candidates if item["direction"] == "LONG"]
    shorts = [item for item in candidates if item["direction"] == "SHORT"]
    if spy and qqq and spy["direction"] == "LONG" and qqq["direction"] == "LONG" and len(longs) >= len(shorts):
        regime = "BULLISH"
        sentence = "Market conditions currently favor selective long setups."
    elif spy and qqq and spy["direction"] == "SHORT" and qqq["direction"] == "SHORT" and len(shorts) >= len(longs):
        regime = "BEARISH"
        sentence = "Market conditions favor bearish continuation, but extended names still require confirmation."
    elif abs(len(longs) - len(shorts)) <= 1:
        regime = "MIXED"
        sentence = "Market conditions are mixed; force no trade unless hard gates pass."
    else:
        regime = "RANGE-BOUND"
        sentence = "Market conditions favor waiting for clearer confirmation."
    sectors: dict[str, int] = {}
    for item in candidates:
        sector_etf = (((item.get("profile_summary") or {}).get("identity") or {}).get("sector_etf") or "Unknown")
        sectors[sector_etf] = sectors.get(sector_etf, 0) + (1 if item["direction"] == "LONG" else -1 if item["direction"] == "SHORT" else 0)
    leading = [key for key, value in sorted(sectors.items(), key=lambda row: row[1], reverse=True) if value > 0][:3]
    lagging = [key for key, value in sorted(sectors.items(), key=lambda row: row[1]) if value < 0][:3]
    return {
        "session_state": session.get("session_state"),
        "next_market_open": session.get("next_market_open"),
        "regular_session_close": session.get("regular_session_close"),
        "spy_trend": spy["direction"] if spy else "UNAVAILABLE",
        "qqq_trend": qqq["direction"] if qqq else "UNAVAILABLE",
        "vix_direction": "UNAVAILABLE",
        "market_breadth": {"long": len(longs), "short": len(shorts), "total": len(candidates)},
        "leading_sectors": leading,
        "lagging_sectors": lagging,
        "overall_regime": regime,
        "summary": sentence,
    }


def build_decision_dashboard(db: Session) -> dict[str, Any]:
    session = get_market_session()
    symbols = ensure_core_universe(db)
    # Include SPY/QQQ for market state even though they are not displayed as duplicate trade candidates.
    market_symbols = ["SPY", "QQQ", *symbols]
    unique_symbols: list[str] = []
    for symbol in market_symbols:
        if symbol not in unique_symbols:
            unique_symbols.append(symbol)
    candidates = [_candidate(db, symbol, session) for symbol in unique_symbols]
    display_candidates = [item for item in candidates if item["ticker"] in symbols]
    qualified = [item for item in display_candidates if item.get("passes_hard_gates")]
    qualified_longs = sorted([item for item in qualified if item["direction"] == "LONG"], key=_score_for_sort, reverse=True)
    qualified_shorts = sorted([item for item in qualified if item["direction"] == "SHORT"], key=_score_for_sort, reverse=True)
    forming = sorted(
        [
            item for item in display_candidates
            if not item.get("passes_hard_gates") and item.get("status") in {"NOT_STARTED", "BUILDING", "PARTIAL", "ANALYSIS_PENDING", "BLOCKED", "ERROR", "STALE", "DATA REFRESH REQUIRED", "NEXT-SESSION WATCH", "WAITING"}
        ],
        key=_score_for_sort,
        reverse=True,
    )
    next_best_limit = _safe_int((_cfg().get("max_cards") or {}).get("next_best"), 3)
    forming_limit = _safe_int((_cfg().get("max_cards") or {}).get("forming"), 6)
    used = {qualified_longs[0]["ticker"]} if qualified_longs else set()
    if qualified_shorts:
        used.add(qualified_shorts[0]["ticker"])
    next_best = [row for row in sorted(qualified, key=_score_for_sort, reverse=True) if row["ticker"] not in used][:next_best_limit]

    no_trade_conditions: list[str] = []
    if not qualified_longs:
        no_trade_conditions.append("No qualified long setup.")
    if not qualified_shorts:
        no_trade_conditions.append("No qualified short setup.")
    incomplete_count = len([item for item in display_candidates if item["status"] in {"NOT_STARTED", "BUILDING", "PARTIAL", "ANALYSIS_PENDING", "BLOCKED", "ERROR", "STALE", "DATA REFRESH REQUIRED"}])
    if incomplete_count:
        no_trade_conditions.append(f"{incomplete_count} core tickers are still profile-building or data-incomplete.")
    if not qualified:
        no_trade_conditions.append("No setup currently passes every profile, history, options, news, and risk gate.")

    # Persist only material candidate states. The recommendation module uses a
    # stable fingerprint, so polling the dashboard does not create duplicates.
    try:
        record_candidates(db, display_candidates, generated_at=now_iso())
    except Exception:
        # A ledger write must never take down the decision page.
        db.rollback()
    recommendation_performance = get_recommendation_performance(db)

    return {
        "generated_at": now_iso(),
        "universe": symbols,
        "market_state": _market_state(candidates, session),
        "best_long_setup": qualified_longs[0] if qualified_longs else None,
        "best_short_setup": qualified_shorts[0] if qualified_shorts else None,
        "next_best_setups": next_best,
        "forming_setups": forming[:forming_limit],
        "no_trade_conditions": no_trade_conditions,
        "all_candidates": display_candidates,
        "recommendation_performance": recommendation_performance,
        "performance_note": "Loaded from stored profiles, cached setup records, scans, news snapshots, and options snapshots. External provider refresh is not blocking this response.",
    }


def build_watchlist_intelligence(db: Session) -> dict[str, Any]:
    symbols = ensure_core_universe(db)
    rows = [row.symbol for row in db.query(Watchlist).filter(Watchlist.active.is_(True)).all()]
    for symbol in rows:
        if symbol not in symbols:
            symbols.append(symbol)
    dashboard = build_decision_dashboard(db)
    by_symbol = {item["ticker"]: item for item in dashboard.get("all_candidates") or []}
    entries = []
    for symbol in symbols:
        item = by_symbol.get(symbol) or _candidate(db, symbol, get_market_session())
        profile_stats = ((item.get("profile_summary") or {}).get("stats") or {})
        earnings_history = profile_stats.get("earnings_history") or {}
        last_earnings = earnings_history.get("last_earnings") or {}
        entries.append(
            {
                "ticker": symbol,
                "profile_status": item.get("profile_status"),
                "profile_state": item.get("profile_state"),
                "planning_ready": (item.get("profile_summary") or {}).get("planning_ready"),
                "live_ready": (item.get("profile_summary") or {}).get("live_ready"),
                "completeness_percentage": (item.get("profile_summary") or {}).get("completeness_percentage"),
                "readiness": (item.get("profile_summary") or {}).get("readiness"),
                "historical_coverage": (item.get("profile_summary") or {}).get("data_coverage"),
                "current_trend": item.get("direction"),
                "next_session_bias": item.get("next_session_bias"),
                "current_setup": item.get("setup_name"),
                "setup_state": item.get("status"),
                "historical_hit_rate": (item.get("historical_match") or {}).get("raw_hit_rate"),
                "expected_value": item.get("expected_value_estimate"),
                "money_flow_classification": "stored profile summary",
                "options_positioning_classification": (item.get("options_positioning") or {}).get("classification"),
                "news_impact": (item.get("news_impact") or {}).get("impact_label"),
                "social_narrative": item.get("social_narrative") or {},
                "social_classification": (item.get("social_narrative") or {}).get("classification"),
                "social_sentiment_score": (item.get("social_narrative") or {}).get("sentiment_score"),
                "social_mention_velocity": (item.get("social_narrative") or {}).get("mention_velocity"),
                "earnings_history": earnings_history,
                "last_earnings": last_earnings,
                "last_earnings_result": last_earnings.get("overall_result"),
                "last_earnings_date": last_earnings.get("reported_date"),
                "last_earnings_reaction": (last_earnings.get("price_reaction") or {}).get("first_session_return_pct"),
                "preferred_contract": item.get("preferred_option_contract"),
                "data_freshness": item.get("data_freshness"),
                "last_analysis_time": item.get("data_freshness", {}).get("profile_updated_at"),
                "hard_gates": item.get("hard_gates") or [],
            }
        )
    return {"generated_at": now_iso(), "symbols": symbols, "rows": entries}
