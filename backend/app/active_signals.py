from __future__ import annotations

import json
import uuid
from datetime import datetime, timedelta, timezone
from typing import Any

from sqlalchemy import inspect, text
from sqlalchemy.orm import Session

from .config import config_manager
from .decision_dashboard import build_decision_dashboard
from .history import now_iso
from .market_session import get_market_session
from .models import SignalEvent, TradingSignal


def ensure_active_signal_schema() -> None:
    """Add the outcome payload to installations created before Active Signals."""
    from .db import engine
    with engine.begin() as connection:
        tables = set(inspect(connection).get_table_names())
        if "trading_signals" not in tables:
            return
        columns = {column["name"] for column in inspect(connection).get_columns("trading_signals")}
        if "outcome_json" not in columns:
            connection.execute(text("ALTER TABLE trading_signals ADD COLUMN outcome_json TEXT NOT NULL DEFAULT '{}'"))


ACTIVE_SIGNAL_STATES = {
    "READY",
    "TRIGGERED",
    "ACTIVE",
    "WAITING FOR RETEST",
    "WAITING FOR CONFIRMATION",
}
TERMINAL_SIGNAL_STATES = {
    "FORMING",
    "EXTENDED",
    "DO NOT CHASE",
    "EXPIRED",
    "INVALIDATED",
    "TARGET REACHED",
    "DATA STALE",
    "REMOVED",
}
SIGNAL_VERSION = "active-signal-v1"
AI_SIGNAL_VALIDATOR_PROMPT_VERSION = "active-signal-validator-v1"
AI_SIGNAL_VALIDATOR_PROMPT = """You are validating a deterministic options trading signal. The setup has already been detected by the analytics engine. Do not invent a setup or alter supplied prices, targets, invalidation, contract data, or probabilities. Evaluate price structure, VWAP, volume, key levels, reward-to-risk, entry quality, chase risk, historical evidence, market and sector alignment, option liquidity, option pricing, and data freshness. Return APPROVE_SIGNAL only when the supplied deterministic facts are coherent; otherwise return WAIT_FOR_CONFIRMATION, WAIT_FOR_RETEST, REJECT_EXTENDED, REJECT_LOW_VOLUME, REJECT_BAD_RISK_REWARD, REJECT_OPTION_QUALITY, REJECT_DATA_QUALITY, or INVALIDATED."""


def _cfg() -> dict[str, Any]:
    return config_manager.get("active_signals", default={}) or {}


def _load(raw: str | None, fallback: Any) -> Any:
    try:
        return json.loads(raw or "")
    except Exception:
        return fallback


def _float(value: Any) -> float | None:
    try:
        number = float(value)
        return number if number == number else None
    except (TypeError, ValueError):
        return None


def _iso(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).isoformat()


def _parse(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        dt = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        return dt.replace(tzinfo=timezone.utc) if dt.tzinfo is None else dt.astimezone(timezone.utc)
    except ValueError:
        return None


def _setup_type(candidate: dict[str, Any]) -> str | None:
    """Map existing deterministic setup families to the supported signal vocabulary."""
    name = str(candidate.get("setup_name") or "").lower()
    side = str(candidate.get("direction") or "").upper()
    if not name or side not in {"LONG", "SHORT"}:
        return None
    if "vwap" in name and ("reclaim" in name or "continu" in name):
        return "VWAP RECLAIM LONG" if side == "LONG" else "VWAP REJECTION SHORT"
    if "vwap" in name and ("reject" in name or "resist" in name):
        return "VWAP REJECTION SHORT" if side == "SHORT" else "VWAP RECLAIM LONG"
    if "failed breakout" in name:
        return "FAILED BREAKOUT SHORT" if side == "SHORT" else "BREAKOUT LONG"
    if "failed breakdown" in name:
        return "FAILED BREAKDOWN LONG" if side == "LONG" else "BREAKDOWN SHORT"
    if "breakdown" in name:
        return "BREAKDOWN SHORT" if side == "SHORT" else "BREAKOUT LONG"
    if "breakout" in name:
        return "BREAKOUT LONG" if side == "LONG" else "BREAKDOWN SHORT"
    if "support" in name or "bounce" in name:
        return "SUPPORT-HOLD LONG" if side == "LONG" else "PULLBACK CONTINUATION"
    if "resistance" in name or "reject" in name:
        return "RESISTANCE-REJECTION SHORT" if side == "SHORT" else "PULLBACK CONTINUATION"
    if "relative strength" in name:
        return "RELATIVE-STRENGTH LONG" if side == "LONG" else "MOMENTUM CONTINUATION"
    if "relative weakness" in name:
        return "RELATIVE-WEAKNESS SHORT" if side == "SHORT" else "MOMENTUM CONTINUATION"
    if "pullback" in name:
        return "PULLBACK CONTINUATION"
    if "flag" in name:
        return "BULL FLAG CONTINUATION" if side == "LONG" else "BEAR FLAG CONTINUATION"
    if "opening range" in name:
        return "OPENING-RANGE BREAKOUT" if side == "LONG" else "OPENING-RANGE BREAKDOWN"
    if "momentum" in name or "continuation" in name:
        return "MOMENTUM CONTINUATION"
    return None


def _ai_validation(candidate: dict[str, Any]) -> dict[str, Any]:
    """Keep the publish path deterministic; optional AI validation is explicit and auditable."""
    enabled = bool(_cfg().get("require_ai_validation", False))
    if not enabled:
        return {
            "decision": "APPROVE_SIGNAL",
            "status": "NOT_REQUIRED",
            "source": "deterministic_signal_gate",
            "prompt_version": AI_SIGNAL_VALIDATOR_PROMPT_VERSION,
            "reason": "AI validation is disabled; deterministic gates remain authoritative.",
        }
    # A future network validator can populate this field without changing the
    # lifecycle contract. Missing AI output never invents or promotes a setup.
    return {
        "decision": "WAIT_FOR_CONFIRMATION",
        "status": "UNAVAILABLE",
        "source": "configuration",
        "prompt_version": AI_SIGNAL_VALIDATOR_PROMPT_VERSION,
        "reason": "AI validation is required but no validated response is available.",
    }


def _candidate_is_publishable(candidate: dict[str, Any], session: dict[str, Any]) -> tuple[bool, list[str]]:
    reasons: list[str] = []
    if not session.get("actionable_live_quotes"):
        reasons.append("options_market_not_actionable")
    if candidate.get("direction") not in {"LONG", "SHORT"}:
        reasons.append("direction_missing")
    if not candidate.get("passes_hard_gates"):
        reasons.extend(candidate.get("hard_gates") or ["deterministic_hard_gate_failed"])
    if not candidate.get("setup_name"):
        reasons.append("setup_classifier_missing")
    entry = candidate.get("entry_trigger") or {}
    invalidation = candidate.get("invalidation") or {}
    targets = candidate.get("targets") or []
    if _float(entry.get("price")) is None or not entry.get("condition"):
        reasons.append("exact_entry_missing")
    if _float(invalidation.get("price")) is None or not invalidation.get("condition"):
        reasons.append("exact_invalidation_missing")
    if len(targets) < 2 or any(_float(row.get("price")) is None for row in targets[:2]):
        reasons.append("exact_targets_missing")
    contract = candidate.get("preferred_option_contract") or {}
    if not contract or contract.get("status") == "PENDING_VALIDATION":
        reasons.append("option_contract_missing")
    if candidate.get("maximum_acceptable_option_entry") is None:
        reasons.append("maximum_option_price_missing")
    groups = candidate.get("evidence_groups") or {}
    for key in ("price_structure", "vwap_control", "volume_participation"):
        if (groups.get(key) or {}).get("score") != 1:
            reasons.append(f"{key}_not_aligned")
    ai = _ai_validation(candidate)
    if ai.get("decision") != "APPROVE_SIGNAL":
        reasons.append("ai_validation_not_approved")
    return not reasons, list(dict.fromkeys(reasons))


def _chase_price(candidate: dict[str, Any]) -> float | None:
    price = _float(candidate.get("current_or_previous_session_price"))
    entry = _float((candidate.get("entry_trigger") or {}).get("price"))
    if price is None and entry is None:
        return None
    anchor = entry or price
    pct = float(_cfg().get("maximum_chase_underlying_pct", 0.75) or 0.75) / 100
    return round(anchor * (1 + pct if candidate.get("direction") == "LONG" else 1 - pct), 2)


def _state_for_candidate(candidate: dict[str, Any]) -> str:
    status = str(candidate.get("status") or "").upper()
    setup = str(candidate.get("setup_name") or "").lower()
    if status == "WAITING" or "pullback" in setup:
        return "WAITING FOR CONFIRMATION" if status == "WAITING" else "WAITING FOR RETEST"
    return "READY"


def _event(db: Session, signal: TradingSignal, event_type: str, from_state: str | None, to_state: str | None, reason: str, payload: dict[str, Any] | None = None) -> None:
    db.add(SignalEvent(
        signal_id=signal.signal_id,
        event_type=event_type,
        from_state=from_state,
        to_state=to_state,
        reason=reason,
        payload_json=json.dumps(payload or {}, sort_keys=True, default=str),
        created_at=now_iso(),
    ))


def _serialize(row: TradingSignal, *, include_payload: bool = True) -> dict[str, Any]:
    payload = _load(row.payload_json, {}) if include_payload else {}
    return {
        "signal_id": row.signal_id,
        "ticker": row.symbol,
        "direction": row.direction,
        "setup_type": row.setup_type,
        "state": row.state,
        "confidence": row.confidence,
        "score": row.score,
        "created_at": row.created_at,
        "valid_from": row.valid_from,
        "valid_until": row.valid_until,
        "expected_holding_window": row.expected_holding_window,
        "setup_timeframe": row.setup_timeframe,
        "execution_timeframe": row.execution_timeframe,
        "current_price": row.current_price,
        "entry": _load(row.entry_json, {}),
        "invalidation": _load(row.invalidation_json, {}),
        "targets": _load(row.targets_json, []),
        "preferred_option_contract": _load(row.contract_json, {}),
        "deterministic_analysis": _load(row.deterministic_json, {}),
        "ai_validation": _load(row.ai_validation_json, {}),
        "data_freshness": _load(row.freshness_json, {}),
        "outcome": _load(row.outcome_json, {}),
        "removal_reason": row.removal_reason,
        "triggered_at": row.triggered_at,
        "target_reached_at": row.target_reached_at,
        "last_validated_at": row.last_validated_at,
        "next_validation_at": row.next_validation_at,
        "paper_recommendation_id": row.paper_recommendation_id,
        "model_version": row.model_version,
        "strategy_version": row.strategy_version,
        **payload,
    }


def _create_signal(db: Session, candidate: dict[str, Any], session: dict[str, Any], now: datetime) -> TradingSignal:
    setup = _setup_type(candidate)
    if not setup:
        raise ValueError("unsupported deterministic setup type")
    valid_until = now + timedelta(minutes=int(_cfg().get("default_valid_minutes", 15) or 15))
    contract = dict(candidate.get("preferred_option_contract") or {})
    contract["maximum_acceptable_premium"] = candidate.get("maximum_acceptable_option_entry")
    payload = {
        "thesis": candidate.get("thesis"),
        "next_action": f"Wait for: {(candidate.get('entry_trigger') or {}).get('condition')}",
        "maximum_chase_underlying": _chase_price(candidate),
        "primary_conflict": (candidate.get("conflicting_factors") or [None])[0],
        "volume_condition": (candidate.get("visible_conditions") or {}).get("volume"),
        "vwap_condition": (candidate.get("visible_conditions") or {}).get("vwap"),
        "key_level": (candidate.get("visible_conditions") or {}).get("key_level"),
        "historical_match": candidate.get("historical_match"),
        "expected_value": candidate.get("expected_value_estimate"),
        "market_session": session,
    }
    ai = _ai_validation(candidate)
    row = TradingSignal(
        signal_id=f"sig-{uuid.uuid4().hex}",
        symbol=candidate["ticker"],
        direction=candidate["direction"],
        setup_type=setup,
        state=_state_for_candidate(candidate),
        confidence=candidate.get("conviction"),
        score=candidate.get("score"),
        created_at=_iso(now), valid_from=_iso(now), valid_until=_iso(valid_until),
        last_validated_at=_iso(now), next_validation_at=_iso(now + timedelta(minutes=3)),
        setup_timeframe="15m", execution_timeframe="5m", expected_holding_window="NEXT 15 MINUTES",
        current_price=_float(candidate.get("current_or_previous_session_price")),
        entry_json=json.dumps(candidate.get("entry_trigger") or {}, sort_keys=True),
        invalidation_json=json.dumps(candidate.get("invalidation") or {}, sort_keys=True),
        targets_json=json.dumps(candidate.get("targets") or [], sort_keys=True),
        contract_json=json.dumps(contract, sort_keys=True, default=str),
        deterministic_json=json.dumps({"candidate": candidate, "hard_gates": candidate.get("hard_gates") or [], "evidence_groups": candidate.get("evidence_groups") or {}}, sort_keys=True, default=str),
        ai_validation_json=json.dumps(ai, sort_keys=True),
        freshness_json=json.dumps(candidate.get("data_freshness") or {}, sort_keys=True, default=str),
        payload_json=json.dumps(payload, sort_keys=True, default=str),
        model_version="deterministic-dashboard-v1",
        strategy_version=SIGNAL_VERSION,
        updated_at=_iso(now),
    )
    db.add(row)
    db.flush()
    _event(db, row, "CREATED", None, row.state, "deterministic setup passed signal publication gates", {"candidate": candidate["ticker"]})
    return row


def _terminal_reason(row: TradingSignal, candidate: dict[str, Any] | None, now: datetime) -> tuple[str, str] | None:
    if _parse(row.valid_until) and now >= _parse(row.valid_until):
        return "EXPIRED", "signal expiration reached"
    if not candidate:
        return "REMOVED", "deterministic candidate is no longer publishable"
    price = _float(candidate.get("current_or_previous_session_price"))
    invalidation = _float((candidate.get("invalidation") or {}).get("price"))
    target = _float(((candidate.get("targets") or [{}])[0]).get("price"))
    if price is not None and invalidation is not None and ((row.direction == "LONG" and price <= invalidation) or (row.direction == "SHORT" and price >= invalidation)):
        return "INVALIDATED", "current price crossed deterministic invalidation"
    if price is not None and target is not None and ((row.direction == "LONG" and price >= target) or (row.direction == "SHORT" and price <= target)):
        return "TARGET REACHED", "first deterministic target reached"
    if not candidate.get("passes_hard_gates"):
        return "REMOVED", "; ".join(candidate.get("hard_gates") or ["hard gate failed"])
    if any((candidate.get("evidence_groups") or {}).get(key, {}).get("score") != 1 for key in ("price_structure", "vwap_control", "volume_participation")):
        return "REMOVED", "price, VWAP, and volume no longer agree"
    return None


def reconcile_signals(db: Session, *, session: dict[str, Any] | None = None, now: datetime | None = None) -> dict[str, Any]:
    session = session or get_market_session()
    now = now or datetime.now(timezone.utc)
    rows = db.query(TradingSignal).filter(TradingSignal.state.in_(list(ACTIVE_SIGNAL_STATES))).all()
    dashboard = {row.get("ticker"): row for row in (build_decision_dashboard(db).get("all_candidates") or [])}
    changed = 0
    if not session.get("actionable_live_quotes"):
        for row in rows:
            old = row.state
            row.state = "EXPIRED"
            row.removal_reason = "options market is closed; next-session planning is separate from active execution signals"
            row.updated_at = _iso(now)
            _event(db, row, "REMOVED", old, row.state, row.removal_reason)
            changed += 1
    else:
        for row in rows:
            candidate = dashboard.get(row.symbol)
            terminal = _terminal_reason(row, candidate, now)
            if terminal:
                old = row.state
                row.state, row.removal_reason = terminal
                row.updated_at = _iso(now)
                if row.state == "TARGET REACHED":
                    row.target_reached_at = _iso(now)
                _event(db, row, "REMOVED", old, row.state, row.removal_reason)
                changed += 1
                continue
            row.last_validated_at = _iso(now)
            row.next_validation_at = _iso(now + timedelta(minutes=3))
            row.current_price = _float(candidate.get("current_or_previous_session_price")) if candidate else row.current_price
            if candidate:
                contract = dict(candidate.get("preferred_option_contract") or {})
                contract["maximum_acceptable_premium"] = candidate.get("maximum_acceptable_option_entry")
                row.entry_json = json.dumps(candidate.get("entry_trigger") or {}, sort_keys=True)
                row.invalidation_json = json.dumps(candidate.get("invalidation") or {}, sort_keys=True)
                row.targets_json = json.dumps(candidate.get("targets") or [], sort_keys=True)
                row.contract_json = json.dumps(contract, sort_keys=True, default=str)
                row.deterministic_json = json.dumps({"candidate": candidate, "hard_gates": candidate.get("hard_gates") or [], "evidence_groups": candidate.get("evidence_groups") or {}}, sort_keys=True, default=str)
                row.ai_validation_json = json.dumps(_ai_validation(candidate), sort_keys=True)
                row.freshness_json = json.dumps(candidate.get("data_freshness") or {}, sort_keys=True, default=str)
                row.score = candidate.get("score")
                row.confidence = candidate.get("conviction")
                detail = _load(row.payload_json, {})
                detail.update({
                    "thesis": candidate.get("thesis"),
                    "next_action": f"Wait for: {(candidate.get('entry_trigger') or {}).get('condition')}",
                    "maximum_chase_underlying": _chase_price(candidate),
                    "primary_conflict": (candidate.get("conflicting_factors") or [None])[0],
                    "volume_condition": (candidate.get("visible_conditions") or {}).get("volume"),
                    "vwap_condition": (candidate.get("visible_conditions") or {}).get("vwap"),
                    "key_level": (candidate.get("visible_conditions") or {}).get("key_level"),
                    "historical_match": candidate.get("historical_match"),
                    "expected_value": candidate.get("expected_value_estimate"),
                })
                row.payload_json = json.dumps(detail, sort_keys=True, default=str)
            row.updated_at = _iso(now)
    created = 0
    if session.get("actionable_live_quotes"):
        existing_keys = {(row.symbol, row.direction, row.setup_type) for row in rows if row.state in ACTIVE_SIGNAL_STATES}
        for candidate in dashboard.values():
            setup = _setup_type(candidate)
            if not setup or not _candidate_is_publishable(candidate, session)[0]:
                continue
            key = (candidate["ticker"], candidate["direction"], setup)
            if key in existing_keys:
                continue
            _create_signal(db, candidate, session, now)
            created += 1
    db.commit()
    active = db.query(TradingSignal).filter(TradingSignal.state.in_(list(ACTIVE_SIGNAL_STATES))).order_by(TradingSignal.score.desc()).all()
    return {"created": created, "changed": changed, "active_count": len(active), "last_scan": _iso(now), "session": session}


def get_active_signals(db: Session, *, refresh: bool = False) -> dict[str, Any]:
    session = get_market_session()
    existing = db.query(TradingSignal).filter(TradingSignal.state.in_(list(ACTIVE_SIGNAL_STATES))).all()
    now = datetime.now(timezone.utc)
    needs_reconcile = refresh or not session.get("actionable_live_quotes") or any((_parse(row.valid_until) and now >= _parse(row.valid_until)) for row in existing)
    if needs_reconcile:
        reconcile_signals(db)
        session = get_market_session()
    rows = db.query(TradingSignal).filter(TradingSignal.state.in_(list(ACTIVE_SIGNAL_STATES))).order_by(TradingSignal.score.desc(), TradingSignal.created_at.desc()).limit(10).all()
    items = [_serialize(row) for row in rows]
    longs = [item for item in items if item["direction"] == "LONG"]
    shorts = [item for item in items if item["direction"] == "SHORT"]
    best = items[0] if items else None
    return {
        "active_signals": items,
        "best_active_long": longs[0] if longs else None,
        "best_active_short": shorts[0] if shorts else None,
        "best_next_15m": best,
        "active_count": len(items),
        "last_full_scan": max((item["last_validated_at"] for item in items), default=None),
        "market_session": session,
        "message": "There is nothing good at the moment. I am still working." if not items else None,
        "states_visible": sorted(ACTIVE_SIGNAL_STATES),
    }


def get_signal_history(db: Session, limit: int = 100) -> dict[str, Any]:
    rows = db.query(TradingSignal).filter(~TradingSignal.state.in_(list(ACTIVE_SIGNAL_STATES))).order_by(TradingSignal.updated_at.desc()).limit(max(1, min(limit, 500))).all()
    return {"signals": [_serialize(row) for row in rows], "count": len(rows), "source": "signal_history"}


def validate_signal_for_paper_entry(db: Session, signal_id: str, payload: dict[str, Any]) -> TradingSignal:
    row = db.query(TradingSignal).filter(TradingSignal.signal_id == signal_id).first()
    if not row or row.state not in {"TRIGGERED", "ACTIVE"}:
        raise ValueError("paper entry requires a TRIGGERED or ACTIVE signal")
    if str(payload.get("symbol") or "").upper() != row.symbol:
        raise ValueError("paper order symbol does not match signal")
    max_premium = _float((_load(row.contract_json, {}) or {}).get("maximum_acceptable_premium"))
    fill = _float(payload.get("fill_price"))
    if max_premium is not None and fill is not None and fill > max_premium:
        raise ValueError("paper entry exceeds the signal maximum acceptable option premium")
    if row.state == "TRIGGERED" and not row.triggered_at:
        row.triggered_at = now_iso()
        _event(db, row, "TRIGGERED", row.state, "ACTIVE", "paper order accepted from triggered signal")
        row.state = "ACTIVE"
        row.updated_at = now_iso()
    return row


def trigger_signal(db: Session, signal_id: str, payload: dict[str, Any] | None = None) -> dict[str, Any]:
    """Record a confirmed entry condition; this does not place a brokerage order."""
    row = db.query(TradingSignal).filter(TradingSignal.signal_id == signal_id).first()
    if not row or row.state not in {"READY", "WAITING FOR RETEST", "WAITING FOR CONFIRMATION"}:
        raise ValueError("signal is no longer waiting for a valid trigger")
    data = payload or {}
    expires = _parse(row.valid_until)
    if expires and datetime.now(timezone.utc) >= expires:
        raise ValueError("signal has expired")
    price = _float(data.get("underlying_price"))
    expected = _float((_load(row.entry_json, {}) or {}).get("price"))
    if price is not None and expected is not None:
        direction_ok = price >= expected if row.direction == "LONG" else price <= expected
        if not direction_ok:
            raise ValueError("trigger price does not satisfy the signal entry condition")
    old = row.state
    row.state = "TRIGGERED"
    row.triggered_at = now_iso()
    row.updated_at = now_iso()
    _event(db, row, "TRIGGERED", old, row.state, "entry condition confirmed", data)
    db.commit()
    return _serialize(row)


def signal_worker_status() -> dict[str, Any]:
    return dict(_status)


_status: dict[str, Any] = {"enabled": True, "running": False, "last_cycle_at": None, "last_error": None, "last_result": None}
_worker_task = None
_worker_lock = None


def _interval_seconds() -> int:
    session = get_market_session()
    return int(_cfg().get("active_interval_seconds", 180) if session.get("actionable_live_quotes") else _cfg().get("closed_interval_seconds", 1800))


async def _run_loop() -> None:
    import asyncio
    global _worker_lock
    if _worker_lock is None:
        _worker_lock = asyncio.Lock()
    _status["running"] = True
    try:
        while True:
            try:
                if not _worker_lock.locked():
                    async with _worker_lock:
                        from .db import SessionLocal
                        db = SessionLocal()
                        try:
                            _status["last_result"] = reconcile_signals(db)
                        finally:
                            db.close()
                        _status["last_cycle_at"] = now_iso()
                        _status["last_error"] = None
            except Exception as exc:
                _status["last_error"] = str(exc)
            await asyncio.sleep(_interval_seconds())
    except asyncio.CancelledError:
        pass
    finally:
        _status["running"] = False


def start_if_enabled() -> None:
    import asyncio
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
        except Exception:
            pass
        _worker_task = None
