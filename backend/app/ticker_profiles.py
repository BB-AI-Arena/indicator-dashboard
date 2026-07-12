from __future__ import annotations

import json
import math
from datetime import datetime, timezone
from typing import Any

import numpy as np
from sqlalchemy import asc, desc, func
from sqlalchemy.orm import Session

from .config import config_manager
from .earnings_history import build_earnings_profile
from .history import get_candles_from_sql, now_iso
from .indicators import apply_indicators
from .social_intelligence import build_social_profile
from .models import (
    Candle,
    HistoricalSetupFeature,
    HistoricalSetupFamily,
    NewsCatalystSnapshot,
    OptionPositioningSnapshot,
    TickerProfile,
    TickerProfileStat,
    TickerProfileUpdate,
)
from .profile_completeness import evaluate_profile_completeness


PROFILE_VERSION = "ticker-profile-v1"
STAT_VERSION = "ticker-stat-v1"


SECTOR_ETF_MAP = {
    "technology": "XLK",
    "communication services": "XLC",
    "consumer discretionary": "XLY",
    "consumer staples": "XLP",
    "health care": "XLV",
    "healthcare": "XLV",
    "financials": "XLF",
    "industrials": "XLI",
    "energy": "XLE",
    "utilities": "XLU",
    "materials": "XLB",
    "real estate": "XLRE",
}


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


def _confidence(sample_size: int) -> str:
    if sample_size < 10:
        return "INSUFFICIENT"
    if sample_size < 30:
        return "LOW"
    if sample_size < 100:
        return "MODERATE"
    return "HIGH"


def _json_loads(raw: str | None, fallback: Any) -> Any:
    if not raw:
        return fallback
    try:
        return json.loads(raw)
    except Exception:
        return fallback


def ensure_ticker_profile(db: Session, symbol: str, *, source: str = "watchlist") -> TickerProfile:
    normalized = str(symbol or "").strip().upper()
    if not normalized:
        raise ValueError("Symbol is required")
    row = db.query(TickerProfile).filter(TickerProfile.symbol == normalized).first()
    now = now_iso()
    if row:
        row.updated_at = now
        return row
    row = TickerProfile(
        symbol=normalized,
        benchmark="SPY",
        profile_status="NOT_STARTED",
        profile_state="NOT_STARTED",
        data_coverage_json="{}",
        personality_json="[]",
        stats_json="{}",
        latest_setup_state_json="{}",
        created_at=now,
        updated_at=now,
    )
    db.add(row)
    db.add(
        TickerProfileUpdate(
            symbol=normalized,
            update_type="profile_created",
            status="COMPLETE",
            message=f"Ticker profile created from {source}.",
            created_at=now,
        )
    )
    db.flush()
    return row


def _coverage(db: Session, symbol: str) -> dict[str, Any]:
    rows = (
        db.query(Candle.interval, func.count(Candle.id), func.min(Candle.timestamp), func.max(Candle.timestamp))
        .filter(Candle.symbol == symbol)
        .group_by(Candle.interval)
        .all()
    )
    intervals = {}
    for interval, count, min_ts, max_ts in rows:
        intervals[str(interval)] = {
            "rows": int(count or 0),
            "first": datetime.fromtimestamp(int(min_ts), timezone.utc).isoformat() if min_ts else None,
            "last": datetime.fromtimestamp(int(max_ts), timezone.utc).isoformat() if max_ts else None,
        }
    return {"intervals": intervals, "profile_version": PROFILE_VERSION}


def _price_behavior(symbol: str, db: Session) -> dict[str, Any]:
    daily = get_candles_from_sql(symbol, "1d", period="3y", db=db)
    intraday = get_candles_from_sql(symbol, "15m", period="90d", db=db)
    result: dict[str, Any] = {
        "daily_sample": int(len(daily)),
        "intraday_15m_sample": int(len(intraday)),
        "date_range": {
            "start": daily.index.min().isoformat() if not daily.empty else None,
            "end": daily.index.max().isoformat() if not daily.empty else None,
        },
    }
    if not daily.empty:
        enriched = apply_indicators(daily, config_manager.get("indicators", default={}) or {})
        returns = enriched["close"].pct_change().dropna()
        result.update(
            {
                "average_daily_volume": _safe_float(enriched["volume"].tail(60).mean(), 0.0),
                "average_dollar_volume": _safe_float((enriched["close"] * enriched["volume"]).tail(60).mean(), 0.0),
                "historical_volatility_annualized": _safe_float(returns.std() * math.sqrt(252) * 100 if len(returns) else None),
                "average_atr": _safe_float(enriched["atr"].tail(30).mean()),
                "average_atr_pct": _safe_float((enriched["atr"] / enriched["close"] * 100).tail(30).mean()),
                "gap_up_count": int((enriched["open"] > enriched["close"].shift(1) * 1.01).sum()),
                "gap_down_count": int((enriched["open"] < enriched["close"].shift(1) * 0.99).sum()),
            }
        )
    if not intraday.empty:
        enriched_15m = apply_indicators(intraday, config_manager.get("indicators", default={}) or {})
        above_vwap = enriched_15m["close"] > enriched_15m["vwap"]
        result["vwap_respect_sample"] = int(above_vwap.count())
        result["above_vwap_rate"] = _safe_float(above_vwap.mean())
        result["first_two_hour_volume_share"] = _first_two_hour_volume_share(enriched_15m)
    return result


def _first_two_hour_volume_share(df) -> float | None:
    if df.empty:
        return None
    try:
        index_et = df.index.tz_convert("America/New_York")
    except Exception:
        return None
    minutes = [(idx.hour * 60 + idx.minute) - (9 * 60 + 30) for idx in index_et]
    frame = df.copy()
    frame["_minutes"] = minutes
    regular = frame[frame["_minutes"].between(0, 390)]
    early = regular[regular["_minutes"].between(0, 120)]
    total = float(regular["volume"].sum() or 0.0)
    if total <= 0:
        return None
    return float(early["volume"].sum() / total)


def _setup_stats(symbol: str, db: Session) -> dict[str, Any]:
    rows = (
        db.query(HistoricalSetupFeature)
        .filter(HistoricalSetupFeature.symbol == symbol)
        .order_by(asc(HistoricalSetupFeature.timestamp))
        .all()
    )
    by_family: dict[str, dict[str, Any]] = {}
    for row in rows:
        family = row.setup_family or "unknown"
        item = by_family.setdefault(
            family,
            {
                "setup_family": family,
                "direction": row.direction,
                "occurrence_count": 0,
                "date_start": None,
                "date_end": None,
                "data_quality": {},
            },
        )
        item["occurrence_count"] += 1
        ts = datetime.fromtimestamp(int(row.timestamp), timezone.utc).isoformat()
        item["date_start"] = item["date_start"] or ts
        item["date_end"] = ts
        quality = row.data_quality or "UNKNOWN"
        item["data_quality"][quality] = int(item["data_quality"].get(quality, 0)) + 1

    family_rows = (
        db.query(HistoricalSetupFamily)
        .filter(HistoricalSetupFamily.setup_name.in_(list(by_family.keys()) or ["__none__"]))
        .all()
    )
    for family_row in family_rows:
        stats = _json_loads(family_row.stats_json, {})
        item = by_family.get(family_row.setup_name)
        if not item:
            continue
        item.update(
            {
                "raw_hit_rate": stats.get("raw_success_rate"),
                "out_of_sample_success_rate": stats.get("out_of_sample_success_rate"),
                "confidence_interval": stats.get("confidence_interval"),
                "average_return_pct": stats.get("average_return_pct"),
                "median_return_pct": stats.get("median_return_pct"),
                "mfe_pct": stats.get("average_mfe_pct"),
                "mae_pct": stats.get("average_mae_pct"),
                "expected_value_pct": stats.get("expected_value_pct"),
                "confidence": stats.get("confidence") or family_row.confidence,
                "last_recalculated_at": family_row.last_recalculated_at,
            }
        )
    return {
        "total_setup_records": len(rows),
        "families": sorted(by_family.values(), key=lambda item: item.get("occurrence_count", 0), reverse=True),
    }


def _news_history(symbol: str, db: Session) -> dict[str, Any]:
    rows = (
        db.query(NewsCatalystSnapshot)
        .filter(NewsCatalystSnapshot.symbol == symbol)
        .order_by(desc(NewsCatalystSnapshot.updated_at))
        .limit(20)
        .all()
    )
    events: list[dict[str, Any]] = []
    for row in rows:
        payload = _json_loads(row.payload_json, {})
        summary = payload.get("summary") or {}
        event = summary.get("most_relevant_event") or {}
        if event:
            events.append(
                {
                    "headline": event.get("headline"),
                    "publication_timestamp": event.get("publication_timestamp"),
                    "category": event.get("event_category"),
                    "impact_label": event.get("position_impact") or payload.get("impact_label"),
                    "confidence": event.get("confidence") or payload.get("confidence"),
                    "updated_at": row.updated_at,
                }
            )
    return {
        "snapshot_count": len(rows),
        "recent_events": events[:8],
        "data_status": "observed" if events else "unavailable",
    }


def _options_history(symbol: str, db: Session) -> dict[str, Any]:
    rows = (
        db.query(OptionPositioningSnapshot)
        .filter(OptionPositioningSnapshot.symbol == symbol)
        .order_by(desc(OptionPositioningSnapshot.created_at))
        .limit(30)
        .all()
    )
    if not rows:
        return {"snapshot_count": 0, "data_status": "unavailable"}
    scores = [_safe_float(row.bias_score, 0.0) or 0.0 for row in rows]
    classifications: dict[str, int] = {}
    for row in rows:
        label = row.classification or "Unknown"
        classifications[label] = int(classifications.get(label, 0)) + 1
    return {
        "snapshot_count": len(rows),
        "latest_classification": rows[0].classification,
        "latest_snapshot_at": rows[0].created_at,
        "average_bias_score": _safe_float(np.mean(scores), 0.0),
        "classification_counts": classifications,
        "data_status": "observed",
    }


def _fibonacci_behavior(symbol: str, db: Session) -> dict[str, Any]:
    # This first profile pass derives defensible proxy stats from setup records.
    # Full swing-interaction persistence can build on these rows without changing profile consumers.
    setup = _setup_stats(symbol, db)
    families = setup.get("families") or []
    fib_related = [
        item for item in families
        if "fib" in str(item.get("setup_family") or "").lower() or "retracement" in str(item.get("setup_family") or "").lower()
    ]
    return {
        "interaction_records": sum(int(item.get("occurrence_count") or 0) for item in fib_related),
        "families": fib_related,
        "classification_rules": [
            "clean support",
            "clean resistance",
            "temporary reaction",
            "false break",
            "confirmed break",
            "no meaningful response",
            "insufficient data",
        ],
        "data_status": "observed" if fib_related else "insufficient data",
        "note": "Fibonacci behavior is recorded only when swing/level context exists in setup records; touches alone are not counted as success.",
    }


def _personality(symbol: str, price: dict[str, Any], setups: dict[str, Any], options: dict[str, Any], news: dict[str, Any], social: dict[str, Any]) -> list[dict[str, Any]]:
    statements: list[dict[str, Any]] = []
    sample = int(price.get("intraday_15m_sample") or 0)
    above_vwap = price.get("above_vwap_rate")
    if above_vwap is not None and sample >= 30:
        statements.append(
            {
                "statement": "Tends to trade above VWAP during the analyzed 15-minute sample." if above_vwap >= 0.55 else "Frequently trades below VWAP during the analyzed 15-minute sample.",
                "supporting_sample_size": sample,
                "measured_outcome": {"above_vwap_rate": above_vwap},
                "confidence": _confidence(sample),
                "last_recalculated_timestamp": now_iso(),
            }
        )
    families = setups.get("families") or []
    best = next((item for item in families if item.get("expected_value_pct") is not None), None)
    if best and int(best.get("occurrence_count") or 0) >= 10:
        statements.append(
            {
                "statement": f"Most developed stored setup family is {best.get('setup_family')}.",
                "supporting_sample_size": int(best.get("occurrence_count") or 0),
                "measured_outcome": {
                    "raw_hit_rate": best.get("raw_hit_rate"),
                    "expected_value_pct": best.get("expected_value_pct"),
                },
                "confidence": best.get("confidence") or _confidence(int(best.get("occurrence_count") or 0)),
                "last_recalculated_timestamp": best.get("last_recalculated_at") or now_iso(),
            }
        )
    if int(options.get("snapshot_count") or 0) >= 5:
        statements.append(
            {
                "statement": f"Recent options positioning most recently classified as {options.get('latest_classification')}.",
                "supporting_sample_size": int(options.get("snapshot_count") or 0),
                "measured_outcome": {"average_bias_score": options.get("average_bias_score")},
                "confidence": _confidence(int(options.get("snapshot_count") or 0)),
                "last_recalculated_timestamp": options.get("latest_snapshot_at") or now_iso(),
            }
        )
    if int(news.get("snapshot_count") or 0) > 0:
        statements.append(
            {
                "statement": "Recent catalyst history is available for this ticker.",
                "supporting_sample_size": int(news.get("snapshot_count") or 0),
                "measured_outcome": {"recent_events": len(news.get("recent_events") or [])},
                "confidence": _confidence(int(news.get("snapshot_count") or 0)),
                "last_recalculated_timestamp": now_iso(),
            }
        )
    social_sample = int(social.get("unique_author_count") or 0)
    if social_sample >= 3 and social.get("data_status") == "observed":
        statements.append(
            {
                "statement": f"Social discussion is currently {social.get('classification') or 'inconclusive'}.",
                "supporting_sample_size": social_sample,
                "measured_outcome": {
                    "sentiment_score": social.get("sentiment_score"),
                    "mention_velocity": social.get("mention_velocity"),
                },
                "confidence": social.get("sentiment_confidence") or "LOW",
                "last_recalculated_timestamp": social.get("updated_at") or now_iso(),
            }
        )
    return statements


def _upsert_stat(db: Session, symbol: str, stat_type: str, stat_key: str, value: dict[str, Any], *, sample_size: int, confidence: str) -> None:
    now = now_iso()
    row = (
        db.query(TickerProfileStat)
        .filter(TickerProfileStat.symbol == symbol)
        .filter(TickerProfileStat.stat_type == stat_type)
        .filter(TickerProfileStat.stat_key == stat_key)
        .filter(TickerProfileStat.version == STAT_VERSION)
        .first()
    )
    date_start = value.get("date_range", {}).get("start") if isinstance(value.get("date_range"), dict) else None
    date_end = value.get("date_range", {}).get("end") if isinstance(value.get("date_range"), dict) else None
    if row:
        row.sample_size = sample_size
        row.date_start = date_start
        row.date_end = date_end
        row.confidence = confidence
        row.value_json = json.dumps(value, sort_keys=True)
        row.recalculated_at = now
        row.updated_at = now
        return
    db.add(
        TickerProfileStat(
            symbol=symbol,
            stat_type=stat_type,
            stat_key=stat_key,
            version=STAT_VERSION,
            sample_size=sample_size,
            date_start=date_start,
            date_end=date_end,
            confidence=confidence,
            value_json=json.dumps(value, sort_keys=True),
            recalculated_at=now,
            created_at=now,
            updated_at=now,
        )
    )


def refresh_ticker_profile(db: Session, symbol: str, *, source: str = "profile_refresh") -> TickerProfile:
    normalized = str(symbol or "").strip().upper()
    profile = ensure_ticker_profile(db, normalized, source=source)
    coverage = _coverage(db, normalized)
    price = _price_behavior(normalized, db)
    setups = _setup_stats(normalized, db)
    options = _options_history(normalized, db)
    news = _news_history(normalized, db)
    earnings = build_earnings_profile(normalized, db)
    social = build_social_profile(normalized, db)
    fibonacci = _fibonacci_behavior(normalized, db)
    personality = _personality(normalized, price, setups, options, news, social)

    stats = {
        "profile_version": PROFILE_VERSION,
        "price_behavior": price,
        "indicator_history": {
            "vwap": {"above_vwap_rate": price.get("above_vwap_rate"), "sample_size": price.get("vwap_respect_sample")},
            "atr": {"average_atr": price.get("average_atr"), "average_atr_pct": price.get("average_atr_pct")},
            "historical_volatility": price.get("historical_volatility_annualized"),
        },
        "setup_history": setups,
        "fibonacci_behavior": fibonacci,
        "news_history": news,
        "earnings_history": earnings,
        "social_history": social,
        "options_confirmation_history": options,
    }
    profile.data_coverage_json = json.dumps(coverage, sort_keys=True)
    profile.stats_json = json.dumps(stats, sort_keys=True)
    profile.personality_json = json.dumps(personality, sort_keys=True)
    # Readiness is assigned only by the centralized completeness evaluator.
    profile.profile_status = profile.profile_state or "ANALYSIS_PENDING"
    profile.average_daily_volume = price.get("average_daily_volume")
    profile.average_dollar_volume = price.get("average_dollar_volume")
    volatility = price.get("historical_volatility_annualized")
    profile.volatility_profile = "HIGH" if volatility and volatility >= 45 else "MODERATE" if volatility and volatility >= 25 else "LOW" if volatility else None
    profile.last_profile_update_at = now_iso()
    profile.updated_at = now_iso()
    profile.sector_etf = profile.sector_etf or SECTOR_ETF_MAP.get(str(profile.sector or "").lower())

    _upsert_stat(db, normalized, "price_behavior", "daily_and_intraday", price, sample_size=int(price.get("daily_sample") or 0), confidence=_confidence(int(price.get("daily_sample") or 0)))
    _upsert_stat(db, normalized, "setup_history", "families", setups, sample_size=int(setups.get("total_setup_records") or 0), confidence=_confidence(int(setups.get("total_setup_records") or 0)))
    _upsert_stat(db, normalized, "fibonacci_behavior", "interactions", fibonacci, sample_size=int(fibonacci.get("interaction_records") or 0), confidence=_confidence(int(fibonacci.get("interaction_records") or 0)))
    _upsert_stat(db, normalized, "news_history", "catalysts", news, sample_size=int(news.get("snapshot_count") or 0), confidence=_confidence(int(news.get("snapshot_count") or 0)))
    _upsert_stat(db, normalized, "earnings_history", "quarterly_reports", earnings, sample_size=int(earnings.get("event_count") or 0), confidence=_confidence(int(earnings.get("event_count") or 0)))
    _upsert_stat(db, normalized, "social_history", "narrative", social, sample_size=int(social.get("unique_author_count") or 0), confidence=social.get("sentiment_confidence") or "INSUFFICIENT")
    _upsert_stat(db, normalized, "options_history", "positioning", options, sample_size=int(options.get("snapshot_count") or 0), confidence=_confidence(int(options.get("snapshot_count") or 0)))
    db.add(
        TickerProfileUpdate(
            symbol=normalized,
            update_type="profile_refresh",
            status="COMPLETE",
            message=f"Profile refreshed from stored SQL and cached snapshots via {source}.",
            payload_json=json.dumps({"coverage": coverage, "status": profile.profile_status}, sort_keys=True),
            created_at=now_iso(),
        )
    )
    db.flush()
    evaluate_profile_completeness(db, profile, persist=True)
    return profile


def serialize_ticker_profile(profile: TickerProfile) -> dict[str, Any]:
    return {
        "symbol": profile.symbol,
        "identity": {
            "company_name": profile.company_name,
            "exchange": profile.exchange,
            "sector": profile.sector,
            "industry": profile.industry,
            "benchmark": profile.benchmark,
            "sector_etf": profile.sector_etf,
            "market_cap": profile.market_cap,
        },
        "profile_status": profile.profile_status,
        "profile_state": profile.profile_state or profile.profile_status,
        "planning_ready": bool(profile.planning_ready),
        "live_ready": bool(profile.live_ready),
        "completeness_percentage": profile.completeness_percentage,
        "readiness": {
            "components": _json_loads(profile.completeness_json, {}),
            "missing_components": _json_loads(profile.missing_components_json, []),
            "blocking_components": _json_loads(profile.blocking_components_json, []),
            "stale_components": _json_loads(profile.stale_components_json, []),
            "last_completeness_check": profile.last_completeness_check,
            "next_required_job": profile.next_required_job,
            "profile_version": profile.profile_version or PROFILE_VERSION,
        },
        "data_coverage": _json_loads(profile.data_coverage_json, {}),
        "personality": _json_loads(profile.personality_json, []),
        "stats": _json_loads(profile.stats_json, {}),
        "latest_setup_state": _json_loads(profile.latest_setup_state_json, {}),
        "last_backfill_requested_at": profile.last_backfill_requested_at,
        "last_profile_update_at": profile.last_profile_update_at,
        "created_at": profile.created_at,
        "updated_at": profile.updated_at,
    }
