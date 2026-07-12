from __future__ import annotations

import json
import math
from datetime import datetime, time as dtime, timezone
from typing import Any
from zoneinfo import ZoneInfo

from .config import config_manager
from .db import SessionLocal
from .exit_management import build_exit_plan, evaluate_exit_management
from .models import PaperPositionRiskState, PaperRiskAuditEvent


EASTERN = ZoneInfo("America/New_York")


def _float(value: Any, default: float | None = None) -> float | None:
    try:
        number = float(value)
        return number if math.isfinite(number) else default
    except Exception:
        return default


def _int(value: Any, default: int = 0) -> int:
    try:
        return int(float(value))
    except Exception:
        return default


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _cfg() -> dict[str, Any]:
    return config_manager.get("paper_portfolio", default={}) or {}


def _position_entry_price(position: dict[str, Any]) -> float | None:
    explicit = _float(position.get("entry_option_price") or position.get("average_entry_price"))
    if explicit and explicit > 0:
        return explicit
    cost_basis = abs(_float(position.get("cost_basis"), 0.0) or 0.0)
    quantity = abs(_int(position.get("quantity"), 0))
    if cost_basis > 0 and quantity > 0:
        return cost_basis / (quantity * 100.0)
    return None


def _executable_mark(position: dict[str, Any]) -> tuple[float | None, str, str | None]:
    direction = str(position.get("direction") or "LONG").upper()
    if direction == "LONG":
        bid = _float(position.get("bid"))
        if bid is not None and bid > 0:
            return bid, "BID", position.get("quote_timestamp")
    else:
        ask = _float(position.get("ask"))
        if ask is not None and ask > 0:
            return ask, "ASK", position.get("quote_timestamp")
    return None, "UNAVAILABLE", position.get("quote_timestamp")


def _conservative_return(entry: float | None, mark: float | None, direction: str) -> float | None:
    if entry is None or mark is None or entry <= 0:
        return None
    if str(direction or "LONG").upper() == "SHORT":
        return (entry - mark) / entry * 100.0
    return (mark - entry) / entry * 100.0


def _audit(db, position_id: str, symbol: str, event_type: str, reason: str, details: dict[str, Any], paper_portfolio_id: int | None = None) -> None:
    db.add(
        PaperRiskAuditEvent(
            paper_portfolio_id=paper_portfolio_id,
            position_id=position_id,
            symbol=symbol,
            event_type=event_type,
            reason=reason,
            details_json=json.dumps(details, sort_keys=True),
            created_at=_now_iso(),
        )
    )


def _evaluate_position(db, position: dict[str, Any], session: dict[str, Any], cfg: dict[str, Any]) -> dict[str, Any]:
    position_id = str(position.get("position_id") or "unknown")
    paper_portfolio_id = _int(position.get("paper_portfolio_id"), 0) or None
    symbol = str(position.get("symbol") or "").upper()
    direction = str(position.get("direction") or "LONG").upper()
    entry = _position_entry_price(position)
    mark, mark_source, quote_timestamp = _executable_mark(position)
    executable_return_pct = _conservative_return(entry, mark, direction)
    reported_return_pct = _float(position.get("unrealized_pnl_pct"))
    return_pct = executable_return_pct if executable_return_pct is not None else reported_return_pct
    state = db.query(PaperPositionRiskState).filter(PaperPositionRiskState.position_id == position_id).first()
    now = _now_iso()
    if not state:
        state = PaperPositionRiskState(
            position_id=position_id,
            paper_portfolio_id=paper_portfolio_id,
            symbol=symbol,
            entry_price=entry,
            trail_status="INACTIVE",
            last_evaluated_at=now,
            state_json="{}",
            created_at=now,
            updated_at=now,
        )
        db.add(state)
        db.flush()
    elif state.entry_price is None and entry is not None:
        state.entry_price = entry
    if state.paper_portfolio_id is None and paper_portfolio_id is not None:
        state.paper_portfolio_id = paper_portfolio_id

    try:
        stored_state = json.loads(state.state_json or "{}")
    except Exception:
        stored_state = {}
    indicators = position.get("management_context") or ((position.get("historical_chart") or {}).get("latest") if isinstance(position.get("historical_chart"), dict) else {}) or {}
    exit_plan = position.get("exit_plan") or stored_state.get("exit_plan") or build_exit_plan(position, indicators=indicators)

    trail_mode = str(cfg.get("trailing_mode", "HYBRID") or "HYBRID").upper()
    activation_return = float(cfg.get("profit_trail_activation_pct", 15.0) or 15.0)
    trail_pct = float(cfg.get("profit_trail_pct", 5.0) or 5.0)
    peak = _float(state.highest_executable_price)
    activation_price = _float(state.activation_price)
    stop = _float(state.trailing_stop_price)
    trail_status = state.trail_status or "INACTIVE"
    audit_details: dict[str, Any] = {}

    if direction == "LONG" and mark is not None and entry is not None and return_pct is not None:
        if return_pct + 1e-9 >= activation_return and activation_price is None:
            activation_price = mark
            peak = mark
            stop = mark * (1.0 - trail_pct / 100.0)
            trail_status = "PROFIT TRAIL ACTIVE"
            _audit(db, position_id, symbol, "PROFIT_TRAIL_ACTIVATED", "Long option reached the configured activation return.", {"activation_price": mark, "activation_return_pct": return_pct, "trailing_stop_price": stop}, paper_portfolio_id)
        elif activation_price is not None:
            peak = max(peak or mark, mark)
            candidate_stop = peak * (1.0 - trail_pct / 100.0)
            if stop is None or candidate_stop > stop:
                stop = candidate_stop
                trail_status = "PROFIT TRAIL ACTIVE"
                _audit(db, position_id, symbol, "PROFIT_TRAIL_RATCHET", "Highest executable bid increased; trailing stop only moved upward.", {"highest_executable_price": peak, "trailing_stop_price": stop}, paper_portfolio_id)
            if stop is not None and mark <= stop:
                trail_status = "TRAIL TRIGGERED"
                _audit(db, position_id, symbol, "PROFIT_TRAIL_TRIGGERED", "Executable bid reached the mechanical trailing stop.", {"trigger_price": mark, "trailing_stop_price": stop, "simulated_fill": mark, "fill_basis": mark_source}, paper_portfolio_id)

    spread_pct = _float(position.get("spread_pct"))
    gamma = abs(_float(position.get("gamma"), 0.0) or 0.0)
    dte = _int(position.get("days_to_expiration"), 0)
    structural_warning = None
    if trail_status == "PROFIT TRAIL ACTIVE" and (gamma >= 0.08 or (spread_pct is not None and spread_pct > 5) or dte <= 3):
        structural_warning = "Mechanical 5% trail may sit inside normal option noise; review the structural alternative before enabling structure-aware trailing."

    current_return = return_pct
    losing = executable_return_pct is not None and executable_return_pct < 0
    state_name = "DATA INSUFFICIENT"
    overnight_probability = _float(position.get("overnight_directional_probability"))
    overnight_sample = _int(position.get("overnight_sample_size"), 0)
    current_et = datetime.fromisoformat(str(session.get("current_eastern_timestamp")).replace("Z", "+00:00")) if session.get("current_eastern_timestamp") else datetime.now(timezone.utc).astimezone(EASTERN)
    if current_et.tzinfo is None:
        current_et = current_et.replace(tzinfo=EASTERN)
    review_time = dtime(15, 30)
    cutoff_time = dtime(15, 50)
    regular = session.get("session_state") in {"REGULAR", "EARLY_CLOSE"}
    before_close_review = regular and current_et.time() >= review_time
    liquidation_due = regular and current_et.time() >= cutoff_time

    if mark is None:
        state_name = "DATA INSUFFICIENT — CLOSE"
        overnight_status = "DATA INSUFFICIENT — CLOSE"
    elif losing:
        state_name = "CLOSE BEFORE MARKET END" if before_close_review else "LOSING — SAME-DAY CLOSE REQUIRED"
        overnight_status = "LOSER_OVERNIGHT_PROHIBITED"
        if liquidation_due:
            _audit(db, position_id, symbol, "LOSER_LIQUIDATION_DUE", "Losing option position reached the configured liquidation cutoff.", {"mark": mark, "return_pct": current_return, "fill_basis": mark_source}, paper_portfolio_id)
    elif current_return is not None and current_return > 0:
        overnight_ok = (
            overnight_probability is not None
            and overnight_probability >= float(cfg.get("overnight_probability_threshold_pct", 70.0) or 70.0)
            and overnight_sample >= int(cfg.get("minimum_overnight_sample", 30) or 30)
            and dte >= int(cfg.get("minimum_overnight_dte", 7) or 7)
            and (spread_pct is None or spread_pct <= float(cfg.get("overnight_max_spread_pct", 5.0) or 5.0))
        )
        overnight_status = "HOLD OVERNIGHT" if overnight_ok else "OVERNIGHT CONFIDENCE INSUFFICIENT"
        state_name = overnight_status
    if mark is not None and not regular and not losing and current_return is not None and current_return > 0:
        state_name = "OVERNIGHT CONFIDENCE INSUFFICIENT"

    theoretical_protected = _conservative_return(entry, stop, "LONG") if stop is not None and entry is not None else None
    state.highest_executable_price = peak
    state.activation_price = activation_price
    state.trailing_stop_price = stop
    state.trail_status = trail_status
    state.overnight_status = state_name
    state.last_quote_timestamp = quote_timestamp
    state.last_evaluated_at = now
    state.updated_at = now
    exit_management = evaluate_exit_management(
        position,
        exit_plan,
        indicators=indicators,
        state=stored_state.get("exit_management") or {},
        market_session=session,
        config=cfg,
    )
    if exit_management.get("state_changed"):
        _audit(
            db,
            position_id,
            symbol,
            "EXIT_STATE_CHANGED",
            str(exit_management.get("reason") or exit_management.get("state")),
            exit_management,
            paper_portfolio_id,
        )
    state.state_json = json.dumps(
        {
            "mark_source": mark_source,
            "mode": trail_mode,
            "exit_plan": exit_plan,
            "exit_management": exit_management,
        },
        sort_keys=True,
    )

    return {
        "position_id": position_id,
        "entry_price": entry,
        "conservative_executable_price": mark,
        "executable_mark_basis": mark_source,
        "conservative_return_pct": current_return,
        "executable_return_pct": executable_return_pct,
        "reported_return_pct": reported_return_pct,
        "capital_committed": abs(_float(position.get("cost_basis"), 0.0) or 0.0),
        "realistic_capital_at_risk": _realistic_risk(position, cfg),
        "profit_trail": {
            "status": trail_status,
            "mode": trail_mode,
            "activation_return_pct": activation_return,
            "activation_price": activation_price,
            "highest_executable_price": peak,
            "trailing_stop_price": stop,
            "theoretical_protected_return_pct": theoretical_protected,
            "protected_return_note": f"Current theoretical protected gain: approximately {theoretical_protected:.2f}% before slippage and gap risk." if theoretical_protected is not None else "Protected return unavailable until an executable mark exists.",
            "quote_timestamp": quote_timestamp,
            "spread_pct": spread_pct,
            "structural_warning": structural_warning,
        },
        "exit_plan": exit_plan,
        "exit_management": exit_management,
        "losing_position": losing,
        "same_day_review": before_close_review,
        "liquidation_cutoff_reached": liquidation_due,
        "same_day_liquidation_deadline": session.get("regular_session_close"),
        "overnight": {
            "status": state_name,
            "directional_probability": overnight_probability,
            "sample_size": overnight_sample,
            "dte": dte,
            "action": "CLOSE BEFORE MARKET END" if losing or mark is None else state_name,
            "reason": "LOSER_OVERNIGHT_PROHIBITED" if losing else "DATA INSUFFICIENT — CLOSE" if mark is None else "Overnight gate requires fresh probability, sample, regime, catalyst, liquidity, and gap-risk evidence.",
        },
    }


def _realistic_risk(position: dict[str, Any], cfg: dict[str, Any]) -> float:
    committed = abs(_float(position.get("cost_basis"), 0.0) or 0.0)
    market_value = abs(_float(position.get("market_value"), 0.0) or 0.0)
    base = committed or market_value
    spread_pct = _float(position.get("spread_pct"), 0.0) or 0.0
    slippage_pct = float(cfg.get("simulated_slippage_pct", 1.0) or 1.0)
    gap_pct = float(cfg.get("gap_risk_pct", 5.0) or 5.0)
    iv_pct = float(cfg.get("iv_contraction_risk_pct", 3.0) or 3.0)
    liquidity_pct = float(cfg.get("liquidity_failure_risk_pct", 5.0) or 5.0) if spread_pct > 5 else 0.0
    return round(base * (1.0 + (spread_pct + slippage_pct + gap_pct + iv_pct + liquidity_pct) / 100.0), 2)


def evaluate_paper_portfolio(positions: list[dict[str, Any]], market_session: dict[str, Any] | None = None) -> dict[str, Any]:
    cfg = _cfg()
    session = market_session or {}
    db = SessionLocal()
    try:
        evaluated = [_evaluate_position(db, position, session, cfg) for position in positions]
        db.commit()
    finally:
        db.close()
    equity = float(cfg.get("account_equity", 100000.0) or 100000.0)
    max_deployment_pct = float(cfg.get("max_deployment_pct", 75.0) or 75.0)
    committed = sum(row["capital_committed"] for row in evaluated)
    realistic_risk = sum(row["realistic_capital_at_risk"] for row in evaluated)
    by_symbol: dict[str, float] = {}
    for position, row in zip(positions, evaluated):
        symbol = str(position.get("symbol") or "UNKNOWN").upper()
        by_symbol[symbol] = by_symbol.get(symbol, 0.0) + row["capital_committed"]
    concentration = {symbol: round(value / equity * 100.0, 2) for symbol, value in by_symbol.items()}
    mode = str(cfg.get("concentration_mode", "aggressive") or "aggressive").lower()
    ticker_cap = float(
        cfg.get("exceptional_ticker_exposure_pct", 50.0)
        if mode == "exceptional"
        else cfg.get("aggressive_ticker_exposure_pct", 35.0)
        if mode == "aggressive"
        else cfg.get("normal_ticker_exposure_pct", 20.0)
    )
    concentration_violations = [symbol for symbol, value in concentration.items() if value > ticker_cap]
    deployment_violation = committed > equity * max_deployment_pct / 100.0 if equity else False
    return {
        "enabled": bool(cfg.get("enabled", True)),
        "account_equity": equity,
        "capital_deployed": round(committed, 2),
        "maximum_capital_deployed": round(equity * max_deployment_pct / 100.0, 2),
        "deployment_pct": round(committed / equity * 100.0, 2) if equity else 0.0,
        "reserve_pct": round(max(0.0, 100.0 - committed / equity * 100.0), 2) if equity else 100.0,
        "realistic_open_risk": round(realistic_risk, 2),
        "positions_above_15_pct": sum(1 for row in evaluated if (row.get("executable_return_pct") or -999) + 1e-9 >= 15),
        "profit_trails_active": sum(1 for row in evaluated if row["profit_trail"]["status"] == "PROFIT TRAIL ACTIVE"),
        "losing_positions_requiring_same_day_close": sum(1 for row in evaluated if row["losing_position"] or row["overnight"]["status"] == "DATA INSUFFICIENT — CLOSE"),
        "green_positions_eligible_for_overnight_review": sum(1 for row in evaluated if (row.get("executable_return_pct") or -999) > 0),
        "overnight_holds_approved": sum(1 for row in evaluated if row["overnight"]["status"] == "HOLD OVERNIGHT"),
        "concentration_pct": concentration,
        "concentration_mode": mode,
        "ticker_exposure_cap_pct": ticker_cap,
        "concentration_violations": concentration_violations,
        "deployment_cap_breached": deployment_violation,
        "additional_capital_available": round(max(0.0, equity * max_deployment_pct / 100.0 - committed), 2),
        "deployment_warning": "Cash is acceptable; never deploy capital merely to reach the 75% maximum." if committed < equity * max_deployment_pct / 100.0 else "Deployment cap reached; no additional paper premium may be committed.",
        "positions": evaluated,
    }
