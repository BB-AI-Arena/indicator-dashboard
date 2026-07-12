from __future__ import annotations

import hashlib
import json
from collections import Counter
from datetime import datetime, timezone
from typing import Any, Iterable
from zoneinfo import ZoneInfo

from sqlalchemy.orm import Session

from .config import config_manager
from .history import now_iso
from .models import (
    PaperPortfolio,
    PaperPortfolioEvent,
    PaperRecommendation,
    RecommendationEvent,
    RecommendationRecord,
)


RECOMMENDATION_VERSION = "recommendation-v1"
_EASTERN = ZoneInfo("America/New_York")


def _ensure_paper_portfolio(db: Session) -> PaperPortfolio:
    portfolio = db.query(PaperPortfolio).filter(PaperPortfolio.name == "Default Paper Challenge").first()
    if portfolio:
        return portfolio
    now = now_iso()
    portfolio = PaperPortfolio(
        name="Default Paper Challenge",
        starting_balance=100000.0,
        cash=100000.0,
        buying_power=100000.0,
        status="ACTIVE",
        created_at=now,
        updated_at=now,
    )
    db.add(portfolio)
    db.flush()
    return portfolio


def migrate_legacy_recommendations(db: Session) -> int:
    """Move legacy generic recommendation rows into the paper ledger once."""
    portfolio = _ensure_paper_portfolio(db)
    moved = 0
    legacy_rows = db.query(RecommendationRecord).all()
    for old in legacy_rows:
        if db.query(PaperRecommendation).filter(PaperRecommendation.recommendation_id == old.recommendation_id).first():
            continue
        new = PaperRecommendation(
            paper_portfolio_id=portfolio.id,
            recommendation_id=old.recommendation_id,
            symbol=old.symbol,
            direction=old.direction,
            setup_type=old.setup_type,
            status=old.status,
            outcome=old.outcome,
            created_at=old.created_at,
            triggered_at=old.triggered_at,
            resolved_at=old.resolved_at,
            model_version=old.model_version,
            strategy_version=old.snapshot_version,
            confidence_tier=old.confidence_tier,
            aggression_mode=old.aggression_mode,
            overnight=old.overnight,
            dte=old.dte,
            delta=old.delta,
            market_regime=old.market_regime,
            entry_price=old.entry_price,
            invalidation_price=old.invalidation_price,
            target_1_price=old.target_1_price,
            target_2_price=old.target_2_price,
            option_contract=old.option_contract,
            option_entry_price=old.option_entry_price,
            option_exit_price=old.option_exit_price,
            realized_pnl=old.realized_pnl,
            underlying_return_pct=old.underlying_return_pct,
            option_return_pct=old.option_return_pct,
            target_before_invalidation=old.target_before_invalidation,
            profitable_option=old.profitable_option,
            directional_correct=old.directional_correct,
            trigger_source=old.trigger_source,
            simulated_fill_source="LEGACY_MIGRATION",
            snapshot_version=old.snapshot_version,
            snapshot_json=old.snapshot_json,
            outcome_json=old.outcome_json,
            created_by=old.created_by,
        )
        db.add(new)
        db.add(PaperPortfolioEvent(
            paper_portfolio_id=portfolio.id,
            event_type="LEGACY_RECOMMENDATION_MIGRATED",
            recommendation_id=old.recommendation_id,
            details_json=_json({"legacy_table": "recommendation_records", "legacy_id": old.id}),
        ))
        moved += 1
    if moved or portfolio.id:
        db.commit()
    return moved


def _json(value: Any) -> str:
    return json.dumps(value, default=str, sort_keys=True, separators=(",", ":"))


def _float(value: Any) -> float | None:
    try:
        if value is None or value == "":
            return None
        number = float(value)
        return number if number == number and abs(number) != float("inf") else None
    except (TypeError, ValueError):
        return None


def _timestamp(value: Any) -> str | None:
    if value is None:
        return None
    if isinstance(value, datetime):
        parsed = value
    else:
        try:
            parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        except (TypeError, ValueError):
            return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc).isoformat()


def _price(level: Any) -> float | None:
    if isinstance(level, dict):
        return _float(level.get("price"))
    return _float(level)


def _contract_value(contract: dict[str, Any] | None, *keys: str) -> Any:
    if not isinstance(contract, dict):
        return None
    for key in keys:
        if contract.get(key) is not None:
            return contract.get(key)
    return None


def _record_fingerprint(candidate: dict[str, Any]) -> str:
    contract = candidate.get("preferred_option_contract") or {}
    payload = {
        "version": RECOMMENDATION_VERSION,
        "ticker": candidate.get("ticker"),
        "direction": candidate.get("direction"),
        "setup_name": candidate.get("setup_name"),
        "status": candidate.get("status"),
        "entry": _price(candidate.get("entry_trigger")),
        "invalidation": _price(candidate.get("invalidation")),
        "targets": [_price(item) for item in (candidate.get("targets") or [])[:2]],
        "contract": _contract_value(contract, "contract", "symbol"),
        "expiration": _contract_value(contract, "expiration"),
        "strike": _contract_value(contract, "strike"),
    }
    return hashlib.sha256(_json(payload).encode("utf-8")).hexdigest()[:40]


def _snapshot_allowed(candidate: dict[str, Any]) -> bool:
    return bool(
        candidate.get("ticker")
        and candidate.get("direction") in {"LONG", "SHORT"}
        and candidate.get("setup_name")
        and candidate.get("status") not in {"NOT_STARTED", "BUILDING", "PARTIAL", "ANALYSIS_PENDING", "BLOCKED", "ERROR", "STALE", "DATA REFRESH REQUIRED", "DATA INSUFFICIENT"}
    )


def record_candidates(db: Session, candidates: Iterable[dict[str, Any]], generated_at: str | None = None) -> int:
    """Persist new recommendation snapshots without rewriting prior snapshots."""
    created = 0
    created_at = _timestamp(generated_at) or now_iso()
    risk_cfg = config_manager.get("paper_portfolio", default={}) or {}
    aggression = str(risk_cfg.get("concentration_mode") or "normal").upper()
    for candidate in candidates:
        if not _snapshot_allowed(candidate):
            continue
        recommendation_id = _record_fingerprint(candidate)
        portfolio = _ensure_paper_portfolio(db)
        if db.query(PaperRecommendation).filter(PaperRecommendation.recommendation_id == recommendation_id).first():
            continue
        contract = candidate.get("preferred_option_contract") or {}
        match = candidate.get("historical_match") or {}
        market_state = candidate.get("market_state") or {}
        snapshot = {
            "recommendation_version": RECOMMENDATION_VERSION,
            "created_at": created_at,
            "candidate": candidate,
        }
        record = PaperRecommendation(
            paper_portfolio_id=portfolio.id,
            recommendation_id=recommendation_id,
            symbol=str(candidate.get("ticker")).upper(),
            direction=str(candidate.get("direction")).upper(),
            setup_type=str(candidate.get("setup_name")),
            status="CREATED",
            outcome="UNRESOLVED",
            created_at=created_at,
            model_version=str(candidate.get("model_version") or RECOMMENDATION_VERSION),
            strategy_version=RECOMMENDATION_VERSION,
            confidence_tier=str(candidate.get("conviction") or match.get("confidence") or "INSUFFICIENT"),
            aggression_mode=aggression,
            overnight=bool(candidate.get("overnight")),
            dte=_float(_contract_value(contract, "dte", "days_to_expiration")),
            delta=_float(_contract_value(contract, "delta")),
            market_regime=str(market_state.get("overall_regime") or candidate.get("market_regime") or "UNKNOWN"),
            entry_price=_price(candidate.get("entry_trigger")),
            invalidation_price=_price(candidate.get("invalidation")),
            target_1_price=_price((candidate.get("targets") or [None])[0]),
            target_2_price=_price((candidate.get("targets") or [None, None])[1]),
            option_contract=_contract_value(contract, "contract", "symbol"),
            snapshot_version=RECOMMENDATION_VERSION,
            snapshot_json=_json(snapshot),
            simulated_fill_source="RECOMMENDATION_SNAPSHOT",
        )
        db.add(record)
        db.add(RecommendationEvent(
            recommendation_id=recommendation_id,
            event_type="CREATED",
            payload_json=_json({"status": candidate.get("status"), "score": candidate.get("score")}),
        ))
        db.add(PaperPortfolioEvent(
            paper_portfolio_id=portfolio.id,
            event_type="RECOMMENDATION_CREATED",
            recommendation_id=recommendation_id,
            details_json=_json({"status": candidate.get("status"), "score": candidate.get("score")}),
        ))
        created += 1
    if created:
        db.commit()
    return created


def _event(db: Session, recommendation_id: str, event_type: str, payload: dict[str, Any], created_by: str | None = None) -> None:
    db.add(RecommendationEvent(
        recommendation_id=recommendation_id,
        event_type=event_type,
        payload_json=_json(payload),
        created_by=created_by,
    ))


def trigger_recommendation(db: Session, recommendation_id: str, payload: dict[str, Any] | None = None, created_by: str | None = None) -> PaperRecommendation:
    record = db.query(PaperRecommendation).filter(PaperRecommendation.recommendation_id == recommendation_id).first()
    if not record:
        raise ValueError("Recommendation not found")
    if record.status == "RESOLVED":
        raise ValueError("Resolved recommendations cannot be triggered again")
    if record.status != "TRIGGERED":
        record.status = "TRIGGERED"
        record.triggered_at = _timestamp((payload or {}).get("triggered_at")) or now_iso()
        record.trigger_source = str((payload or {}).get("trigger_source") or "paper")
        record.simulated_fill_source = str((payload or {}).get("simulated_fill_source") or "PAPER_SIMULATION")
        entry_price = _float((payload or {}).get("entry_price"))
        option_entry = _float((payload or {}).get("option_entry_price"))
        if entry_price is not None:
            record.entry_price = entry_price
        if option_entry is not None:
            record.option_entry_price = option_entry
        _event(db, recommendation_id, "TRIGGERED", payload or {}, created_by)
        db.add(PaperPortfolioEvent(
            paper_portfolio_id=record.paper_portfolio_id,
            event_type="RECOMMENDATION_TRIGGERED",
            recommendation_id=recommendation_id,
            details_json=_json(payload or {}),
        ))
        db.commit()
    return record


def resolve_recommendation(db: Session, recommendation_id: str, payload: dict[str, Any], created_by: str | None = None) -> PaperRecommendation:
    record = db.query(PaperRecommendation).filter(PaperRecommendation.recommendation_id == recommendation_id).first()
    if not record:
        raise ValueError("Recommendation not found")
    if record.status != "TRIGGERED":
        raise ValueError("Only triggered recommendations can be resolved")
    outcome = str(payload.get("outcome") or "").upper()
    if outcome not in {"WIN", "LOSS", "NEUTRAL"}:
        raise ValueError("Outcome must be WIN, LOSS, or NEUTRAL")
    record.status = "RESOLVED"
    record.outcome = outcome
    record.resolved_at = _timestamp(payload.get("resolved_at")) or now_iso()
    for field in ("option_exit_price", "realized_pnl", "underlying_return_pct", "option_return_pct"):
        value = _float(payload.get(field))
        if value is not None:
            setattr(record, field, value)
    for field in ("target_before_invalidation", "profitable_option", "directional_correct"):
        if payload.get(field) is not None:
            setattr(record, field, bool(payload.get(field)))
    record.outcome_json = _json(payload)
    _event(db, recommendation_id, "RESOLVED", payload, created_by)
    db.add(PaperPortfolioEvent(
        paper_portfolio_id=record.paper_portfolio_id,
        event_type="RECOMMENDATION_RESOLVED",
        recommendation_id=recommendation_id,
        details_json=_json(payload),
    ))
    db.commit()
    return record


def _bucket_dte(value: float | None) -> str:
    if value is None:
        return "UNKNOWN"
    if value <= 7:
        return "0-7"
    if value <= 30:
        return "8-30"
    if value <= 60:
        return "31-60"
    return "61+"


def _bucket_delta(value: float | None) -> str:
    if value is None:
        return "UNKNOWN"
    absolute = abs(value)
    if absolute < 0.40:
        return "<0.40"
    if absolute < 0.55:
        return "0.40-0.54"
    if absolute < 0.70:
        return "0.55-0.69"
    return "0.70+"


def _resolved(records: Iterable[PaperRecommendation]) -> list[PaperRecommendation]:
    return [row for row in records if row.status == "RESOLVED" and row.triggered_at and row.outcome in {"WIN", "LOSS", "NEUTRAL"}]


def _metrics(records: list[PaperRecommendation]) -> dict[str, Any]:
    resolved = _resolved(records)
    wins = sum(row.outcome == "WIN" for row in resolved)
    losses = sum(row.outcome == "LOSS" for row in resolved)
    neutral = sum(row.outcome == "NEUTRAL" for row in resolved)
    pnl = [_float(row.realized_pnl) for row in resolved]
    pnl = [value for value in pnl if value is not None]
    winning_pnl = [value for value in pnl if value > 0]
    losing_pnl = [value for value in pnl if value < 0]
    target_rows = [row for row in resolved if row.target_before_invalidation is not None]
    option_rows = [row for row in resolved if row.profitable_option is not None]
    direction_rows = [row for row in resolved if row.directional_correct is not None]
    positive = sum(winning_pnl)
    negative = abs(sum(losing_pnl))
    return {
        "resolved": len(resolved),
        "wins": wins,
        "losses": losses,
        "neutral": neutral,
        # The denominator intentionally includes neutral resolved outcomes,
        # matching the product definition of full-trade win rate.
        "full_trade_win_rate": wins / len(resolved) if resolved else None,
        "directional_accuracy": sum(row.directional_correct for row in direction_rows) / len(direction_rows) if direction_rows else None,
        "target_before_invalidation_rate": sum(row.target_before_invalidation for row in target_rows) / len(target_rows) if target_rows else None,
        "profitable_option_rate": sum(row.profitable_option for row in option_rows) / len(option_rows) if option_rows else None,
        "average_win": sum(winning_pnl) / len(winning_pnl) if winning_pnl else None,
        "average_loss": sum(losing_pnl) / len(losing_pnl) if losing_pnl else None,
        "profit_factor": positive / negative if negative else (None if not positive else None),
        "expectancy": sum(pnl) / len(resolved) if resolved and pnl else None,
        "pnl_observations": len(pnl),
    }


def _dimension_value(row: PaperRecommendation, dimension: str) -> str:
    if dimension == "dte_range":
        return _bucket_dte(_float(row.dte))
    if dimension == "delta_range":
        return _bucket_delta(_float(row.delta))
    if dimension == "calls_puts":
        return "CALLS" if row.direction == "LONG" else "PUTS"
    value = getattr(row, dimension, None)
    return str(value or "UNKNOWN")


def _breakdown(records: list[PaperRecommendation], dimension: str) -> list[dict[str, Any]]:
    groups: dict[str, list[PaperRecommendation]] = {}
    for row in records:
        groups.setdefault(_dimension_value(row, dimension), []).append(row)
    output = []
    for key, rows in sorted(groups.items(), key=lambda item: (-len(_resolved(item[1])), item[0])):
        metrics = _metrics(rows)
        output.append({"value": key, "sample_size": metrics["resolved"], **metrics})
    return output


def _rolling(resolved: list[PaperRecommendation], count: int) -> dict[str, Any]:
    return _metrics(sorted(resolved, key=lambda row: _timestamp(row.resolved_at) or "")[-count:])


def get_recommendation_performance(db: Session, now: datetime | None = None) -> dict[str, Any]:
    records = db.query(PaperRecommendation).order_by(PaperRecommendation.created_at.asc()).all()
    resolved = _resolved(records)
    current = now or datetime.now(timezone.utc)
    current_et = current.astimezone(_EASTERN)
    month_rows = []
    for row in resolved:
        timestamp = _timestamp(row.resolved_at)
        if not timestamp:
            continue
        parsed = datetime.fromisoformat(timestamp).astimezone(_EASTERN)
        if parsed.year == current_et.year and parsed.month == current_et.month:
            month_rows.append(row)
    dimensions = {
        "calls_puts": "calls_puts",
        "ticker": "symbol",
        "setup_type": "setup_type",
        "market_regime": "market_regime",
        "dte_range": "dte_range",
        "delta_range": "delta_range",
        "confidence_tier": "confidence_tier",
        "aggression_mode": "aggression_mode",
        "overnight": "overnight",
        "model_version": "model_version",
    }
    breakdowns = {name: _breakdown(resolved, field) for name, field in dimensions.items()}
    best_setup = next((row for row in breakdowns["setup_type"] if row["sample_size"] >= 1), None)
    weakest_setup = next((row for row in reversed(breakdowns["setup_type"]) if row["sample_size"] >= 1), None)
    return {
        "version": RECOMMENDATION_VERSION,
        "total_recommendations_created": len(records),
        "total_recommendations_triggered": sum(row.status in {"TRIGGERED", "RESOLVED"} for row in records),
        "total_recommendations_resolved": len(resolved),
        "non_triggered_recommendations": sum(row.status == "CREATED" for row in records),
        "invalidated_before_entry": sum(row.status == "INVALIDATED_BEFORE_ENTRY" for row in records),
        "active_triggered": sum(row.status == "TRIGGERED" for row in records),
        "wins": sum(row.outcome == "WIN" for row in resolved),
        "losses": sum(row.outcome == "LOSS" for row in resolved),
        "neutral_or_unresolved": sum(row.outcome == "NEUTRAL" for row in resolved) + sum(row.status != "RESOLVED" for row in records),
        "all_time": _metrics(resolved),
        "rolling": {
            "last_10": _rolling(resolved, 10),
            "last_20": _rolling(resolved, 20),
            "last_50": _rolling(resolved, 50),
            "current_month": _metrics(month_rows),
        },
        "breakdowns": breakdowns,
        "best_setup": best_setup,
        "weakest_setup": weakest_setup,
        "last_updated_at": now_iso(),
    }


def list_recommendations(db: Session, limit: int = 100) -> list[dict[str, Any]]:
    rows = db.query(PaperRecommendation).order_by(PaperRecommendation.created_at.desc()).limit(max(1, min(limit, 500))).all()
    return [
        {
            "recommendation_id": row.recommendation_id,
            "symbol": row.symbol,
            "direction": row.direction,
            "setup_type": row.setup_type,
            "status": row.status,
            "outcome": row.outcome,
            "created_at": row.created_at,
            "triggered_at": row.triggered_at,
            "resolved_at": row.resolved_at,
            "model_version": row.model_version,
            "confidence_tier": row.confidence_tier,
            "realized_pnl": row.realized_pnl,
            "option_return_pct": row.option_return_pct,
            "snapshot_version": row.snapshot_version,
        }
        for row in rows
    ]
