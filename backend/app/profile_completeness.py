from __future__ import annotations

import json
from datetime import datetime, timezone
from typing import Any

from sqlalchemy import func, inspect, text
from sqlalchemy.orm import Session

from .config import config_manager
from .history import now_iso
from .models import (
    Candle,
    HistoricalSetupFeature,
    HistoricalSetupOutcome,
    NewsCatalystSnapshot,
    OptionPositioningSnapshot,
    ProviderErrorLog,
    Scan,
    TickerProfile,
    TickerProfileUpdate,
)


PROFILE_STATES = {
    "NOT_STARTED",
    "BUILDING",
    "PARTIAL",
    "ANALYSIS_PENDING",
    "READY_FOR_PLANNING",
    "READY_FOR_LIVE_ANALYSIS",
    "STALE",
    "BLOCKED",
    "ERROR",
}
COMPONENTS = (
    "symbol_profile",
    "daily_history",
    "intraday_history",
    "candle_gaps",
    "indicators",
    "support_resistance",
    "fibonacci",
    "setup_history",
    "historical_sample",
    "market_regime",
    "relative_strength",
    "news",
    "news_reaction",
    "options_chain",
    "options_positioning",
    "deterministic_score",
    "data_quality",
    "current_quote",
    "current_candle",
    "live_option_chain",
    "live_liquidity",
    "current_setup",
    "entry_readiness",
    "provider_conflict",
)


def _cfg() -> dict[str, Any]:
    return config_manager.get("decision_dashboard", default={}) or {}


def _profile_cfg() -> dict[str, Any]:
    return config_manager.get("profile_completeness", default={}) or {}


def _load(raw: str | None, fallback: Any) -> Any:
    if not raw:
        return fallback
    try:
        return json.loads(raw)
    except Exception:
        return fallback


def _component(status: str, *, detail: str, required: bool = True, **extra: Any) -> dict[str, Any]:
    return {"status": status, "detail": detail, "required": required, **extra}


def _age_seconds(value: Any) -> float | None:
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=timezone.utc)
        return max(0.0, (datetime.now(timezone.utc) - parsed.astimezone(timezone.utc)).total_seconds())
    except (TypeError, ValueError):
        return None


def _range_quality(db: Session, symbol: str, interval: str, minimum_rows: int) -> dict[str, Any]:
    rows = (
        db.query(Candle.timestamp)
        .filter(Candle.symbol == symbol, Candle.interval == interval)
        .order_by(Candle.timestamp.asc())
        .all()
    )
    timestamps = [int(row[0]) for row in rows if row[0] is not None]
    if len(timestamps) < minimum_rows:
        return {"status": "MISSING_DATA", "rows": len(timestamps), "minimum": minimum_rows, "gaps": None}
    max_gap = 5 * 86400 if interval == "1d" else 4 * 86400
    gaps = sum(1 for previous, current in zip(timestamps, timestamps[1:]) if current - previous > max_gap)
    return {
        "status": "COMPLETE" if gaps == 0 else "MISSING_DATA",
        "rows": len(timestamps),
        "minimum": minimum_rows,
        "gaps": gaps,
        "first": datetime.fromtimestamp(timestamps[0], timezone.utc).isoformat(),
        "last": datetime.fromtimestamp(timestamps[-1], timezone.utc).isoformat(),
    }


def _latest_feature(db: Session, symbol: str) -> HistoricalSetupFeature | None:
    return (
        db.query(HistoricalSetupFeature)
        .filter(HistoricalSetupFeature.symbol == symbol, HistoricalSetupFeature.interval == "15m")
        .order_by(HistoricalSetupFeature.timestamp.desc())
        .first()
    )


def _latest_scan(db: Session, symbol: str) -> Scan | None:
    return db.query(Scan).filter(Scan.symbol == symbol).order_by(Scan.created_at.desc()).first()


def _latest_option(db: Session, symbol: str) -> OptionPositioningSnapshot | None:
    return db.query(OptionPositioningSnapshot).filter(OptionPositioningSnapshot.symbol == symbol).order_by(OptionPositioningSnapshot.created_at.desc()).first()


def _latest_news(db: Session, symbol: str) -> NewsCatalystSnapshot | None:
    return db.query(NewsCatalystSnapshot).filter(NewsCatalystSnapshot.symbol == symbol).order_by(NewsCatalystSnapshot.updated_at.desc()).first()


def _sample(profile_stats: dict[str, Any], minimum: int) -> dict[str, Any]:
    setup = profile_stats.get("setup_history") or {}
    families = setup.get("families") or []
    occurrences = [int(row.get("occurrence_count") or row.get("sample_size") or 0) for row in families]
    total = int(setup.get("total_setup_records") or sum(occurrences))
    best = max(occurrences or [0])
    if best >= minimum:
        status = "COMPLETE"
        detail = f"{best} examples found; minimum {minimum} met."
    elif total > 0:
        status = "INSUFFICIENT_SAMPLE"
        detail = f"{best} examples found; minimum {minimum} required."
    else:
        status = "MISSING_DATA"
        detail = "No historical setup examples have been evaluated."
    return {"status": status, "detail": detail, "examples_found": best, "total_records": total, "minimum_required": minimum}


def _planning_required() -> tuple[str, ...]:
    return tuple(_profile_cfg().get("planning_required_components") or (
        "symbol_profile", "daily_history", "intraday_history", "candle_gaps", "indicators",
        "support_resistance", "fibonacci", "setup_history", "historical_sample", "market_regime",
        "relative_strength", "news", "news_reaction", "options_chain", "options_positioning",
        "deterministic_score", "data_quality",
    ))


def _provider_blocked(db: Session, symbol: str) -> dict[str, Any] | None:
    row = (
        db.query(ProviderErrorLog)
        .filter(ProviderErrorLog.symbol == symbol)
        .order_by(ProviderErrorLog.id.desc())
        .first()
    )
    if not row:
        return None
    message = str(row.error_message or "").lower()
    if any(term in message for term in ("unsupported", "invalid symbol", "permission", "oauth", "authentication")):
        return {"provider": row.provider, "message": row.error_message}
    return None


def evaluate_profile_completeness(
    db: Session,
    profile: TickerProfile,
    *,
    market_session: dict[str, Any] | None = None,
    persist: bool = True,
) -> dict[str, Any]:
    symbol = profile.symbol.upper()
    cfg = _cfg()
    required_intervals = cfg.get("required_intervals") or {"15m": {"min_rows": 40}, "1d": {"min_rows": 30}}
    minimum_sample = int(cfg.get("min_historical_examples", 10) or 10)
    stats = _load(profile.stats_json, {})
    price = stats.get("price_behavior") or {}
    feature = _latest_feature(db, symbol)
    feature_values = _load(feature.features_json if feature else None, {})
    scan = _latest_scan(db, symbol)
    option = _latest_option(db, symbol)
    news = _latest_news(db, symbol)
    coverage_15m = _range_quality(db, symbol, "15m", int((required_intervals.get("15m") or {}).get("min_rows", 40) or 40))
    coverage_daily = _range_quality(db, symbol, "1d", int((required_intervals.get("1d") or {}).get("min_rows", 30) or 30))
    setup_sample = _sample(stats, minimum_sample)
    feature_count = db.query(func.count(HistoricalSetupFeature.id)).filter(HistoricalSetupFeature.symbol == symbol).scalar() or 0
    outcome_count = (
        db.query(func.count(HistoricalSetupOutcome.id))
        .join(HistoricalSetupFeature, HistoricalSetupFeature.id == HistoricalSetupOutcome.feature_id)
        .filter(HistoricalSetupFeature.symbol == symbol)
        .scalar()
        or 0
    )
    fib = stats.get("fibonacci_behavior") or {}
    indicator_history = stats.get("indicator_history") or {}
    session = market_session or {}
    actionable = bool(session.get("actionable_live_quotes"))
    now = now_iso()
    options_payload = _load(option.positioning_json if option else None, {})
    option_timestamp = (options_payload.get("quote_timestamp") or options_payload.get("timestamp") or (option.created_at if option else None)) if isinstance(options_payload, dict) else (option.created_at if option else None)
    option_age = _age_seconds(option_timestamp)
    scan_age = _age_seconds(scan.created_at if scan else None)
    last_candle = db.query(func.max(Candle.timestamp)).filter(Candle.symbol == symbol, Candle.interval == "15m").scalar()
    candle_age = (datetime.now(timezone.utc).timestamp() - int(last_candle)) if last_candle else None
    components: dict[str, dict[str, Any]] = {
        "symbol_profile": _component("COMPLETE" if profile else "MISSING_DATA", detail="Ticker profile exists." if profile else "Ticker profile has not been created."),
        "daily_history": _component("COMPLETE" if coverage_daily["status"] == "COMPLETE" else "MISSING_DATA", detail=f"{coverage_daily['rows']} rows; {coverage_daily['minimum']} required.", **{key: value for key, value in coverage_daily.items() if key != "status"}),
        "intraday_history": _component("COMPLETE" if coverage_15m["status"] == "COMPLETE" else "MISSING_DATA", detail=f"{coverage_15m['rows']} rows; {coverage_15m['minimum']} required.", **{key: value for key, value in coverage_15m.items() if key != "status"}),
        "candle_gaps": _component("COMPLETE" if coverage_15m["status"] == "COMPLETE" and coverage_daily["status"] == "COMPLETE" else "MISSING_DATA", detail="No unresolved gaps in the required lookback." if coverage_15m["status"] == coverage_daily["status"] == "COMPLETE" else "Required candle coverage has unresolved gaps or insufficient rows."),
        "indicators": _component("COMPLETE" if price.get("daily_sample", 0) >= coverage_daily["minimum"] and price.get("intraday_15m_sample", 0) >= coverage_15m["minimum"] and indicator_history else "MISSING_DATA", detail="Stored indicator history is available." if indicator_history else "Indicators have not been calculated."),
        "support_resistance": _component("COMPLETE" if feature_values.get("support") is not None and feature_values.get("resistance") is not None else "MISSING_DATA", detail="Latest setup contains support and resistance." if feature_values.get("support") is not None and feature_values.get("resistance") is not None else "Support/resistance is not available."),
        "fibonacci": _component("COMPLETE" if fib.get("data_status") == "observed" and int(fib.get("interaction_records") or 0) > 0 else "MISSING_DATA", detail="Fibonacci structure has analyzed interactions." if fib.get("data_status") == "observed" else "Fibonacci behavior has not been analyzed."),
        "setup_history": _component("COMPLETE" if feature_count > 0 and outcome_count > 0 else "MISSING_DATA", detail=f"{feature_count} setup features and {outcome_count} outcomes stored."),
        "historical_sample": setup_sample,
        "market_regime": _component("COMPLETE" if feature_values.get("market_regime") not in (None, "", "unavailable", "UNKNOWN") else "MISSING_DATA", detail="Market regime is present." if feature_values.get("market_regime") not in (None, "", "unavailable", "UNKNOWN") else "Market regime is unavailable."),
        "relative_strength": _component("COMPLETE" if any(feature_values.get(key) is not None for key in ("spy_relative_return", "qqq_relative_return", "sector_relative_return")) else "MISSING_DATA", detail="Relative-strength context is present." if any(feature_values.get(key) is not None for key in ("spy_relative_return", "qqq_relative_return", "sector_relative_return")) else "Relative-strength context is unavailable."),
        "news": _component("COMPLETE" if news else "MISSING_DATA", detail="Latest news sync exists." if news else "News sync has not completed."),
        "news_reaction": _component("COMPLETE" if news and _load(news.payload_json, {}).get("summary") is not None else "MISSING_DATA", detail="News reaction analysis exists." if news and _load(news.payload_json, {}).get("summary") is not None else "News reaction analysis is pending."),
        "options_chain": _component("COMPLETE" if option else "MISSING_DATA", detail="Prior-session/current option-chain snapshot exists." if option else "Option-chain snapshot is missing."),
        "options_positioning": _component("COMPLETE" if option and options_payload else "MISSING_DATA", detail="Options positioning has been calculated." if option and options_payload else "Options positioning is missing."),
        "deterministic_score": _component("COMPLETE" if scan and scan.score is not None else "MISSING_DATA", detail="Deterministic scan score is available." if scan else "Deterministic score has not completed."),
        "data_quality": _component("COMPLETE" if feature and feature.data_quality not in (None, "", "UNKNOWN", "LOW") else "MISSING_DATA", detail=f"Latest data quality: {feature.data_quality}" if feature else "Data-quality score is unavailable."),
        "current_quote": _component("COMPLETE" if actionable and scan and scan.price is not None and scan_age is not None and scan_age <= float(_profile_cfg().get("live_quote_max_age_seconds", 120) or 120) else "STALE" if actionable and scan else "MISSING_DATA", detail="Current quote/scan is fresh." if actionable and scan and scan_age is not None and scan_age <= 120 else "Current quote is unavailable or stale."),
        "current_candle": _component("COMPLETE" if actionable and candle_age is not None and candle_age <= float(_profile_cfg().get("live_candle_max_age_seconds", 1800) or 1800) else "STALE" if actionable and last_candle else "MISSING_DATA", detail="Latest completed candle is current." if actionable and candle_age is not None and candle_age <= 1800 else "Latest completed candle is unavailable or stale."),
        "live_option_chain": _component("COMPLETE" if actionable and option and option_age is not None and option_age <= float(_profile_cfg().get("live_option_max_age_seconds", 180) or 180) else "STALE" if actionable and option else "MISSING_DATA", detail="Live option-chain snapshot is fresh." if actionable and option and option_age is not None and option_age <= 180 else "Live option-chain data is unavailable or stale."),
        "live_liquidity": _component("COMPLETE" if actionable and option and options_payload else "STALE" if actionable and option else "MISSING_DATA", detail="Live liquidity checks are available." if actionable and option else "Live liquidity checks are pending."),
        "current_setup": _component("COMPLETE" if feature and feature.setup_state not in (None, "", "DATA INSUFFICIENT") else "MISSING_DATA", detail="Current setup state is calculated." if feature and feature.setup_state not in (None, "", "DATA INSUFFICIENT") else "Current setup state is unavailable."),
        "entry_readiness": _component("COMPLETE" if feature and feature.setup_state in {"CONFIRMING", "CONFIRMED", "FORMING"} and feature_values else "MISSING_DATA", detail="Entry-readiness inputs are present." if feature and feature_values else "Entry-readiness score is pending."),
        "provider_conflict": _component("BLOCKED" if stats.get("provider_conflict") or stats.get("data_conflict") else "COMPLETE", detail="Provider conflict requires reconciliation." if stats.get("provider_conflict") or stats.get("data_conflict") else "No unresolved provider conflict recorded."),
    }
    planning_required = _planning_required()
    planning_missing = [name for name in planning_required if components.get(name, {}).get("status") != "COMPLETE"]
    planning_ready = not planning_missing
    live_required = planning_required + ("current_quote", "current_candle", "live_option_chain", "live_liquidity", "current_setup", "entry_readiness", "provider_conflict")
    live_missing = [name for name in live_required if components.get(name, {}).get("status") != "COMPLETE"]
    live_ready = not live_missing
    blocked = _provider_blocked(db, symbol)
    blocking = []
    if blocked:
        blocking.append({"component": "provider", **blocked})
    stale = [name for name, item in components.items() if item.get("status") == "STALE"]
    missing = [name for name in planning_missing if components.get(name, {}).get("status") in {"MISSING_DATA", "INSUFFICIENT_SAMPLE"}]
    raw_ready = components["daily_history"]["status"] == components["intraday_history"]["status"] == "COMPLETE"
    analysis_names = ("indicators", "support_resistance", "fibonacci", "setup_history", "market_regime", "relative_strength", "news", "news_reaction", "options_chain", "options_positioning", "deterministic_score", "data_quality")
    analysis_pending = raw_ready and any(components[name]["status"] != "COMPLETE" for name in analysis_names)
    previous = profile.profile_state or profile.profile_status or "NOT_STARTED"
    if blocked or components["provider_conflict"]["status"] == "BLOCKED":
        state = "BLOCKED"
    elif not raw_ready:
        state = "BUILDING" if any(components[name]["rows"] > 0 for name in ("daily_history", "intraday_history")) or profile.last_backfill_requested_at else "NOT_STARTED"
    elif planning_ready and live_ready:
        state = "READY_FOR_LIVE_ANALYSIS"
    elif planning_ready:
        state = "STALE" if previous in {"READY_FOR_LIVE_ANALYSIS", "STALE"} and stale else "READY_FOR_PLANNING"
    elif analysis_pending:
        state = "ANALYSIS_PENDING"
    else:
        state = "PARTIAL"
    if any(item.get("status") == "ERROR" for item in components.values()):
        state = "ERROR"
    completeness = round(sum(1 for name in planning_required if components.get(name, {}).get("status") == "COMPLETE") / max(1, len(planning_required)) * 100.0, 2)
    result = {
        "profile_state": state,
        "planning_ready": planning_ready,
        "live_ready": live_ready,
        "completeness_percentage": completeness,
        "components": components,
        "missing_components": missing,
        "blocking_components": blocking,
        "stale_components": stale,
        "planning_missing_components": planning_missing,
        "live_missing_components": live_missing,
        "historical_sample": setup_sample,
        "last_completeness_check": now,
        "next_required_job": planning_missing[0] if planning_missing else (live_missing[0] if live_missing else None),
        "profile_version": _profile_cfg().get("profile_version", "ticker-profile-v2"),
    }
    if persist:
        profile.profile_state = state
        profile.profile_status = state
        profile.planning_ready = planning_ready
        profile.live_ready = live_ready
        profile.completeness_percentage = completeness
        profile.completeness_json = json.dumps(components, sort_keys=True)
        profile.missing_components_json = json.dumps(missing, sort_keys=True)
        profile.blocking_components_json = json.dumps(blocking, sort_keys=True)
        profile.stale_components_json = json.dumps(stale, sort_keys=True)
        profile.last_completeness_check = now
        profile.next_required_job = result["next_required_job"]
        profile.profile_version = result["profile_version"]
    return result


def ensure_profile_schema() -> None:
    from .db import engine

    additions = {
        "profile_state": "VARCHAR(32)",
        "planning_ready": "BOOLEAN",
        "live_ready": "BOOLEAN",
        "completeness_percentage": "FLOAT",
        "completeness_json": "TEXT NOT NULL DEFAULT '{}'",
        "missing_components_json": "TEXT NOT NULL DEFAULT '[]'",
        "blocking_components_json": "TEXT NOT NULL DEFAULT '[]'",
        "stale_components_json": "TEXT NOT NULL DEFAULT '[]'",
        "last_completeness_check": "VARCHAR(64)",
        "next_required_job": "VARCHAR(128)",
        "profile_version": "VARCHAR(64)",
    }
    with engine.begin() as connection:
        if "ticker_profiles" not in set(inspect(connection).get_table_names()):
            return
        existing = {column["name"] for column in inspect(connection).get_columns("ticker_profiles")}
        for name, definition in additions.items():
            if name not in existing:
                connection.execute(text(f"ALTER TABLE ticker_profiles ADD COLUMN {name} {definition}"))
