"""Deterministic, paper-only premarket preparation.

This module reads persisted candles, profiles, scans, news, and option
snapshots. It never places an order and never turns a morning candidate into a
recommendation merely because it appears in the watchlist.
"""

from __future__ import annotations

import json
import math
from datetime import date, datetime, time, timezone
from typing import Any
from zoneinfo import ZoneInfo

from sqlalchemy import desc
from sqlalchemy.orm import Session

from .config import config_manager
from .decision_dashboard import _candidate, ensure_core_universe
from .history import now_iso
from .market_session import get_market_session
from .models import Candle, HistoricalSetupFeature, NewsCatalystSnapshot, PaperMorningCandidate, TickerProfile, Watchlist


EASTERN = ZoneInfo("America/New_York")
PREMARKET_START = time(4, 0)
REGULAR_OPEN = time(9, 30)
ROUTINE_VERSION = "morning-routine-v1"


def _cfg() -> dict[str, Any]:
    return config_manager.get("morning_routine", default={}) or {}


def _load(raw: str | None, fallback: Any) -> Any:
    if not raw:
        return fallback
    try:
        return json.loads(raw)
    except Exception:
        return fallback


def _number(value: Any) -> float | None:
    try:
        value = float(value)
        return value if math.isfinite(value) else None
    except (TypeError, ValueError):
        return None


def _round(value: float | None, digits: int = 2) -> float | None:
    return round(value, digits) if value is not None else None


def same_time_premarket_rvol(
    current_volume: float | None,
    elapsed_minutes: int | None,
    historical_same_time_volumes: list[float],
) -> float | None:
    """Compare partial premarket volume with partial prior-session volume.

    A full-day average is deliberately not accepted here: it would make an
    early premarket print look artificially insignificant.
    """
    if current_volume is None or elapsed_minutes is None or elapsed_minutes <= 0:
        return None
    samples = [float(value) for value in historical_same_time_volumes if _number(value) is not None and float(value) > 0]
    if not samples:
        return None
    return round(float(current_volume) / (sum(samples) / len(samples)), 2)


def adaptive_gap_classification(gap_pct: float | None, atr_pct: float | None, average_range_pct: float | None = None) -> dict[str, Any]:
    """Classify a gap relative to the ticker's volatility, not fixed percent bands."""
    if gap_pct is None:
        return {"classification": "INSUFFICIENT DATA", "direction": None, "gap_pct": None, "gap_vs_atr": None}
    scale = atr_pct or average_range_pct
    gap_vs_atr = abs(gap_pct) / scale if scale and scale > 0 else None
    magnitude = abs(gap_pct)
    if gap_vs_atr is None:
        classification = "TRADEABLE GAP" if magnitude >= 0.75 else "IMMATERIAL GAP"
    elif gap_vs_atr >= 3:
        classification = "EXTREME GAP"
    elif gap_vs_atr >= 1.75:
        classification = "LARGE GAP"
    elif gap_vs_atr >= 0.55:
        classification = "TRADEABLE GAP"
    else:
        classification = "IMMATERIAL GAP"
    return {
        "classification": classification,
        "direction": "UP" if gap_pct > 0 else "DOWN" if gap_pct < 0 else "FLAT",
        "gap_pct": round(gap_pct, 2),
        "gap_vs_atr": round(gap_vs_atr, 2) if gap_vs_atr is not None else None,
    }


def classify_catalyst(news_payload: dict[str, Any] | None, *, direction: str | None = None) -> dict[str, Any]:
    payload = news_payload or {}
    event = ((payload.get("summary") or {}).get("most_relevant_event") or {})
    category = str(event.get("event_category") or event.get("category") or "").upper()
    if not event:
        return {"category": "NONE", "strength": "NONE", "source_reliability": None, "freshness": "UNAVAILABLE", "price_confirmation": "UNAVAILABLE", "headline": None}
    strong_categories = {"EARNINGS", "GUIDANCE", "REGULATORY", "ACQUISITION", "MERGER", "LEGAL", "FDA"}
    moderate_categories = {"ANALYST ACTION", "ANALYST UPGRADE", "ANALYST DOWNGRADE", "PRODUCT", "PARTNERSHIP", "CONTRACT", "FILING", "MANAGEMENT"}
    strength = "STRONG" if category in strong_categories else "MODERATE" if category in moderate_categories else "WEAK"
    reaction = event.get("reaction") or event.get("actual_share_price_reaction") or {}
    reaction_value = _number(reaction.get("return_15m_pct") or reaction.get("return_1h_pct") or reaction.get("first_session_return_pct"))
    if reaction_value is None:
        price_confirmation = "UNAVAILABLE"
    elif direction == "LONG":
        price_confirmation = "CONFIRMS" if reaction_value > 0 else "CONFLICTS"
    elif direction == "SHORT":
        price_confirmation = "CONFIRMS" if reaction_value < 0 else "CONFLICTS"
    else:
        price_confirmation = "OBSERVED"
    return {
        "category": category or "OTHER",
        "strength": strength,
        "source_reliability": event.get("confidence") or payload.get("confidence"),
        "freshness": "OBSERVED" if event.get("publication_timestamp") else "UNKNOWN",
        "price_confirmation": price_confirmation,
        "headline": event.get("headline"),
        "published_at": event.get("publication_timestamp"),
        "report_type": event.get("report_type"),
        "reaction": reaction,
    }


def build_opening_scenarios(direction: str | None, levels: dict[str, Any]) -> list[dict[str, Any]]:
    side = str(direction or "").upper()
    premarket_high = levels.get("premarket_high")
    premarket_low = levels.get("premarket_low")
    support = levels.get("support")
    resistance = levels.get("resistance")
    vwap = levels.get("vwap")
    if side == "LONG":
        return [
            {"name": "BREAKOUT AND HOLD", "condition": f"Break and hold above {premarket_high:.2f} with a completed confirmation candle and expanding volume." if premarket_high else "Break above the relevant premarket high with completed-candle volume confirmation.", "action": "Watch for a valid long trigger; do not enter the first opening print."},
            {"name": "PULLBACK AND HOLD", "condition": f"Retest {support or vwap:.2f} and hold with contracting pullback volume, then reclaim the level." if (support or vwap) else "Pull back to a defined support or VWAP and hold before a long entry.", "action": "Prefer a controlled retest over chasing extension."},
            {"name": "FAILED BREAKOUT", "condition": f"Break the premarket high and close back below it with selling volume." if premarket_high else "Break the opening level and close back below it with selling volume.", "action": "Remove the long plan; evaluate a short only as a separate qualified setup."},
        ]
    if side == "SHORT":
        return [
            {"name": "BREAKDOWN AND HOLD", "condition": f"Break and hold below {premarket_low:.2f} with a completed confirmation candle and expanding volume." if premarket_low else "Break below the relevant premarket low with completed-candle volume confirmation.", "action": "Watch for a valid short trigger; do not enter the first opening print."},
            {"name": "BOUNCE INTO RESISTANCE", "condition": f"Bounce into {resistance or vwap:.2f}, fail, and close back below it." if (resistance or vwap) else "Bounce into defined resistance or VWAP, fail, and close back below it.", "action": "Prefer a failed reclaim over chasing a gap lower."},
            {"name": "FAILED BREAKDOWN", "condition": f"Break below the premarket low and reclaim it with buying volume." if premarket_low else "Break the opening level and reclaim it with buying volume.", "action": "Remove the short plan; do not reverse without a separately qualified long setup."},
        ]
    return [{"name": "NO DIRECTIONAL PLAN", "condition": "Direction, levels, or options data are not complete.", "action": "Monitor only until the missing inputs are available."}]


def _to_et(timestamp: int) -> datetime:
    return datetime.fromtimestamp(int(timestamp), timezone.utc).astimezone(EASTERN)


def _premarket_rows(db: Session, symbol: str, *, session_date: date | None = None) -> dict[date, list[Candle]]:
    # Prefer one granularity so 5m and 15m bars are never double-counted.
    preferred_interval = "5m" if db.query(Candle.id).filter(Candle.symbol == symbol, Candle.interval == "5m").first() else "15m"
    rows = db.query(Candle).filter(Candle.symbol == symbol, Candle.interval == preferred_interval).order_by(Candle.timestamp.asc()).all()
    grouped: dict[date, list[Candle]] = {}
    for row in rows:
        et = _to_et(row.timestamp)
        if PREMARKET_START <= et.time() < REGULAR_OPEN and (session_date is None or et.date() == session_date):
            grouped.setdefault(et.date(), []).append(row)
    return grouped


def _premarket_context(db: Session, symbol: str, session: dict[str, Any], previous_close: float | None, atr_pct: float | None) -> dict[str, Any]:
    reference = str(session.get("current_eastern_timestamp") or "")
    try:
        now_et = datetime.fromisoformat(reference).astimezone(EASTERN)
    except (TypeError, ValueError):
        now_et = datetime.now(EASTERN)
    current_date = now_et.date() if session.get("session_state") == "PREMARKET" else None
    grouped = _premarket_rows(db, symbol, session_date=current_date)
    current = grouped.get(current_date, []) if current_date else []
    if not current:
        return {"price": None, "volume": None, "high": None, "low": None, "rvol": None, "trend": "UNAVAILABLE", "status": "PREVIOUS_SESSION" if session.get("session_state") != "PREMARKET" else "DATA_INCOMPLETE"}
    elapsed = max(1, int((now_et.hour * 60 + now_et.minute) - 240))
    current = [row for row in current if _to_et(row.timestamp).time() <= now_et.time()]
    if not current:
        return {"price": None, "volume": None, "high": None, "low": None, "rvol": None, "trend": "UNAVAILABLE", "status": "DATA_INCOMPLETE"}
    by_date = _premarket_rows(db, symbol)
    historical = []
    for day, rows in sorted(by_date.items(), reverse=True):
        if current_date and day >= current_date:
            continue
        historical.append(sum(float(row.volume or 0) for row in rows if int((_to_et(row.timestamp).hour * 60 + _to_et(row.timestamp).minute) - 240) <= elapsed))
        if len(historical) >= int(_cfg().get("premarket_comparison_sessions", 20) or 20):
            break
    first = float(current[0].open)
    last = float(current[-1].close)
    return {
        "price": _round(last),
        "volume": _round(sum(float(row.volume or 0) for row in current), 0),
        "high": _round(max(float(row.high) for row in current)),
        "low": _round(min(float(row.low) for row in current)),
        "rvol": same_time_premarket_rvol(sum(float(row.volume or 0) for row in current), elapsed, historical),
        "elapsed_minutes": elapsed,
        "comparison_samples": len(historical),
        "trend": "RISING" if last > first else "FALLING" if last < first else "FLAT",
        "status": "CURRENT_PREMARKET",
        "gap": _round(((last - previous_close) / previous_close) * 100) if previous_close else None,
        "gap_context": adaptive_gap_classification(((last - previous_close) / previous_close) * 100 if previous_close else None, atr_pct),
    }


def _previous_close(db: Session, symbol: str, session_date: date | None) -> float | None:
    rows = db.query(Candle).filter(Candle.symbol == symbol, Candle.interval == "1d").order_by(desc(Candle.timestamp)).limit(20).all()
    for row in rows:
        if session_date is None or _to_et(row.timestamp).date() < session_date:
            return _number(row.close)
    return None


def _candidate_payload(db: Session, symbol: str, session: dict[str, Any]) -> dict[str, Any]:
    candidate = _candidate(db, symbol, session)
    profile = db.query(TickerProfile).filter(TickerProfile.symbol == symbol).first()
    feature_row = db.query(HistoricalSetupFeature).filter(HistoricalSetupFeature.symbol == symbol, HistoricalSetupFeature.interval == "15m").order_by(desc(HistoricalSetupFeature.timestamp)).first()
    feature_values = _load(feature_row.features_json if feature_row else None, {})
    stats = _load(profile.stats_json if profile else None, {})
    latest = candidate.get("profile_summary") or {}
    price_behavior = stats.get("price_behavior") or {}
    session_date = None
    try:
        session_date = datetime.fromisoformat(str(session.get("next_market_open") or "").replace("Z", "+00:00")).astimezone(EASTERN).date()
    except (TypeError, ValueError):
        pass
    previous_close = _previous_close(db, symbol, session_date)
    atr_pct = _number(price_behavior.get("average_atr_pct"))
    premarket = _premarket_context(db, symbol, session, previous_close, atr_pct)
    current_price = premarket.get("price") or _number(candidate.get("current_or_previous_session_price"))
    gap_pct = premarket.get("gap")
    gap = premarket.get("gap_context") or adaptive_gap_classification(gap_pct, atr_pct)
    direction = candidate.get("direction") if candidate.get("direction") in {"LONG", "SHORT"} else ("LONG" if (gap_pct or 0) > 0.5 else "SHORT" if (gap_pct or 0) < -0.5 else None)
    news_row = db.query(NewsCatalystSnapshot).filter(NewsCatalystSnapshot.symbol == symbol).order_by(desc(NewsCatalystSnapshot.updated_at)).first()
    news_payload = _load(news_row.payload_json if news_row else None, {})
    catalyst = classify_catalyst(news_payload, direction=direction)
    if catalyst["strength"] == "NONE":
        news_history = stats.get("news_history") or {}
        recent = (news_history.get("recent_events") or [{}])[0]
        catalyst = classify_catalyst({"summary": {"most_relevant_event": recent}}, direction=direction)
    options = candidate.get("preferred_option_contract") or {}
    pending_contract = not options or options.get("status") == "PENDING_VALIDATION" or not options.get("contract")
    levels = {
        "previous_close": _round(previous_close),
        "premarket_high": premarket.get("high"),
        "premarket_low": premarket.get("low"),
        "support": _number(feature_values.get("support") or feature_values.get("swing_low")) or premarket.get("low"),
        "resistance": _number(feature_values.get("resistance") or feature_values.get("swing_high")) or premarket.get("high"),
        "vwap": _number(feature_values.get("vwap")),
    }
    profile_summary = candidate.get("profile_summary") or {}
    missing = list((profile_summary.get("readiness") or {}).get("missing_components") or [])
    hard_gates = list(candidate.get("hard_gates") or [])
    if pending_contract:
        hard_gates.append("options_contract_not_validated")
    if direction is None:
        hard_gates.append("direction_not_defined")
    if premarket.get("status") == "DATA_INCOMPLETE":
        hard_gates.append("premarket_data_incomplete")
    if gap.get("classification") == "EXTREME GAP":
        hard_gates.append("gap_extreme_do_not_chase")
    if not levels.get("support") and not levels.get("resistance"):
        hard_gates.append("key_levels_missing")
    score_parts = {
        "catalyst_quality": 20 if catalyst["strength"] == "STRONG" else 12 if catalyst["strength"] == "MODERATE" else 5 if catalyst["strength"] == "WEAK" else None,
        "key_level_relevance": 20 if levels.get("support") or levels.get("resistance") else None,
        "premarket_participation": 15 if premarket.get("rvol") is not None and premarket["rvol"] >= 2 else 10 if premarket.get("rvol") is not None and premarket["rvol"] >= 1.2 else 5 if premarket.get("rvol") is not None else None,
        "gap_quality": 15 if gap["classification"] == "TRADEABLE GAP" else 10 if gap["classification"] == "LARGE GAP" else 5 if gap["classification"] == "IMMATERIAL GAP" else None,
        "market_sector_alignment": 10 if candidate.get("evidence_groups", {}).get("relative_behavior", {}).get("score") == 1 else 0 if candidate.get("evidence_groups", {}).get("relative_behavior", {}).get("score") is not None else None,
        "historical_behavior": 10 if (candidate.get("historical_match") or {}).get("sample_size", 0) >= 10 else 5 if (candidate.get("historical_match") or {}).get("sample_size", 0) else None,
        "option_liquidity": 10 if not pending_contract else None,
    }
    available = [value for value in score_parts.values() if value is not None]
    score = round(sum(available) / sum((20, 20, 15, 15, 10, 10, 10)[index] for index, value in enumerate(score_parts.values()) if value is not None) * 100, 1) if available else None
    if hard_gates:
        status = "EXTENDED" if "gap_extreme_do_not_chase" in hard_gates else "OPTIONS UNTRADEABLE" if pending_contract else "DATA INCOMPLETE" if missing or "profile_missing" in hard_gates else "WAIT FOR CONFIRMATION"
    elif candidate.get("status") in {"READY FOR PLANNING", "READY FOR LIVE ANALYSIS"}:
        status = "HIGH PRIORITY" if (score or 0) >= 70 else "WATCH AT OPEN"
    else:
        status = "WAIT FOR CONFIRMATION"
    if premarket.get("status") == "CURRENT_PREMARKET" and premarket.get("trend") in {"RISING", "FALLING"} and not hard_gates:
        status = "WAIT FOR PULLBACK" if gap.get("gap_vs_atr") and gap["gap_vs_atr"] >= 1.75 else status
    opening_scenarios = build_opening_scenarios(direction, levels)
    return {
        "ticker": symbol,
        "direction_bias": direction,
        "status": status,
        "score": score,
        "score_breakdown": score_parts,
        "catalyst": catalyst,
        "gap": gap,
        "premarket": premarket,
        "levels": levels,
        "current_price": _round(current_price),
        "premarket_trend": premarket.get("trend"),
        "setup": candidate.get("setup_name") or "Opening structure watch",
        "opening_scenarios": opening_scenarios,
        "entry_trigger": candidate.get("entry_trigger"),
        "invalidation": candidate.get("invalidation"),
        "targets": candidate.get("targets") or [],
        "chase_threshold": _round((levels.get("resistance") if direction == "LONG" else levels.get("support"))),
        "preferred_contract": options if not pending_contract else None,
        "option_liquidity_status": "PASS" if not pending_contract else "UNTRADEABLE / NOT VALIDATED",
        "historical_match": candidate.get("historical_match") or {},
        "profile_state": candidate.get("profile_status"),
        "readiness": profile_summary.get("readiness") or {},
        "hard_gates": sorted(set(hard_gates)),
        "data_freshness": candidate.get("data_freshness") or {},
        "confidence": candidate.get("conviction") or "INSUFFICIENT",
        "reason_included": "; ".join((candidate.get("supporting_factors") or [])[:3]) or "Stored data is being monitored for an opening confirmation.",
        "primary_risk": (candidate.get("conflicting_factors") or [None])[0] or ("Do not chase an extended gap." if status == "EXTENDED" else "Opening liquidity and confirmation are not yet known."),
        "paper_only": True,
        "triggered": False,
        "routine_version": ROUTINE_VERSION,
        "snapshot_at": now_iso(),
    }


def _symbols(db: Session) -> list[str]:
    symbols = list(ensure_core_universe(db))
    for row in db.query(Watchlist).filter(Watchlist.active.is_(True)).all():
        if row.symbol not in symbols:
            symbols.append(row.symbol)
    return symbols


def _morning_date(session: dict[str, Any]) -> str:
    raw = session.get("next_market_open") if session.get("session_state") != "PREMARKET" else session.get("current_eastern_timestamp")
    try:
        return datetime.fromisoformat(str(raw).replace("Z", "+00:00")).astimezone(EASTERN).date().isoformat()
    except (TypeError, ValueError):
        return datetime.now(EASTERN).date().isoformat()


def _market_summary(db: Session, session: dict[str, Any]) -> dict[str, Any]:
    rows = []
    for symbol in ("SPY", "QQQ"):
        try:
            row = _candidate(db, symbol, session)
            rows.append(row)
        except Exception:
            continue
    directions = [row.get("direction") for row in rows if row.get("direction") in {"LONG", "SHORT"}]
    regime = "BULLISH" if directions and directions.count("LONG") == len(directions) else "BEARISH" if directions and directions.count("SHORT") == len(directions) else "MIXED"
    return {"regime": regime, "spy_trend": next((row.get("direction") for row in rows if row.get("ticker") == "SPY"), None), "qqq_trend": next((row.get("direction") for row in rows if row.get("ticker") == "QQQ"), None), "futures": {"status": "UNAVAILABLE", "note": "Futures are shown only when a configured provider supplies them."}, "breadth": "UNAVAILABLE"}


def build_morning_brief(db: Session, *, market_session: dict[str, Any] | None = None, refresh: bool = False) -> dict[str, Any]:
    session = market_session or get_market_session()
    morning_date = _morning_date(session)
    candidates = []
    for symbol in _symbols(db):
        try:
            candidates.append(_candidate_payload(db, symbol, session))
        except Exception as exc:
            candidates.append({"ticker": symbol, "status": "DATA INCOMPLETE", "direction_bias": None, "score": None, "hard_gates": ["candidate_build_failed"], "primary_risk": str(exc), "paper_only": True, "triggered": False, "routine_version": ROUTINE_VERSION, "snapshot_at": now_iso()})
    ranked = sorted(candidates, key=lambda row: (row.get("score") is not None, row.get("score") or -1), reverse=True)
    ranked = ranked[: int(_cfg().get("max_candidates", 10) or 10)]
    existing = {row.symbol: row for row in db.query(PaperMorningCandidate).filter(PaperMorningCandidate.morning_date == morning_date).all()}
    for rank, payload in enumerate(ranked, start=1):
        if payload["ticker"] in existing:
            continue
        db.add(PaperMorningCandidate(morning_date=morning_date, symbol=payload["ticker"], rank=rank, direction=payload.get("direction_bias"), status=payload.get("status") or "DATA INCOMPLETE", score=payload.get("score"), catalyst_strength=(payload.get("catalyst") or {}).get("strength"), gap_pct=(payload.get("gap") or {}).get("gap_pct"), premarket_rvol=(payload.get("premarket") or {}).get("rvol"), payload_json=json.dumps(payload, sort_keys=True), created_at=payload.get("snapshot_at") or now_iso(), updated_at=now_iso()))
    db.commit()
    eligible = [row for row in ranked if not row.get("hard_gates") and row.get("status") in {"HIGH PRIORITY", "WATCH AT OPEN", "WAIT FOR CONFIRMATION", "WAIT FOR PULLBACK"}]
    longs = [row for row in eligible if row.get("direction_bias") == "LONG"]
    shorts = [row for row in eligible if row.get("direction_bias") == "SHORT"]
    no_long = "There is no qualified long setup this morning. The system is still monitoring."
    no_short = "There is no qualified short setup this morning. The system is still monitoring."
    message = "There is nothing good at the moment. I am still working." if not longs and not shorts else None
    return {
        "morning_date": morning_date,
        "generated_at": now_iso(),
        "routine_version": ROUTINE_VERSION,
        "paper_only": True,
        "session": session,
        "market": _market_summary(db, session),
        "best_long": longs[0] if longs else None,
        "best_short": shorts[0] if shorts else None,
        "best_long_label": "BEST LONG TO WATCH" if longs else no_long,
        "best_short_label": "BEST SHORT TO WATCH" if shorts else no_short,
        "candidates": ranked,
        "no_trade_conditions": sorted({gate for row in ranked for gate in row.get("hard_gates") or []}),
        "overall_message": message,
        "scheduled_events": [],
        "last_refresh": now_iso(),
        "refresh_mode": "stored_data_only",
    }
