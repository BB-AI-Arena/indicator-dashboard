"""Deterministic, structure-aware exit planning for paper and real positions."""

from __future__ import annotations

import math
from datetime import datetime, time as dtime, timezone
from typing import Any
from zoneinfo import ZoneInfo


EASTERN = ZoneInfo("America/New_York")
EXIT_PLAN_VERSION = "exit-plan-v1"


def _number(value: Any) -> float | None:
    try:
        number = float(value)
        return number if math.isfinite(number) else None
    except (TypeError, ValueError):
        return None


def _integer(value: Any, default: int = 0) -> int:
    number = _number(value)
    return int(number) if number is not None else default


def _level(value: Any) -> float | None:
    if isinstance(value, dict):
        return _number(value.get("price"))
    return _number(value)


def _side(position: dict[str, Any]) -> str:
    return str(position.get("direction") or position.get("side") or "LONG").upper()


def _first(position: dict[str, Any], *keys: str) -> Any:
    for key in keys:
        if position.get(key) is not None:
            return position.get(key)
    return None


def _current_underlying(position: dict[str, Any], indicators: dict[str, Any]) -> float | None:
    return _number(_first(position, "current_underlying_price", "underlying_price", "current_price_underlying") or indicators.get("close") or indicators.get("price"))


def _option_entry(position: dict[str, Any], contract: dict[str, Any]) -> float | None:
    return _number(_first(position, "entry_option_price", "average_entry_price", "option_entry_price") or contract.get("entry_price") or contract.get("max_reasonable_entry"))


def _management_timeframe(position: dict[str, Any]) -> str:
    return str(position.get("management_timeframe") or position.get("execution_timeframe") or "5m")


def _target_from(position: dict[str, Any], key: str, targets: list[Any], index: int) -> float | None:
    explicit = _level(position.get(key))
    if explicit is not None:
        return explicit
    return _level(targets[index]) if len(targets) > index else None


def build_exit_plan(
    position: dict[str, Any] | None = None,
    *,
    candidate: dict[str, Any] | None = None,
    indicators: dict[str, Any] | None = None,
    contract: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Build an exit plan from supplied levels without inventing option prices."""
    source = {**(candidate or {}), **(position or {})}
    indicators = indicators or source.get("management_context") or {}
    contract = contract or source.get("preferred_option_contract") or {}
    entry_trigger = source.get("entry_trigger") or {}
    invalidation = source.get("invalidation") or {}
    targets = source.get("targets") or []
    underlying_entry = _number(_first(source, "entry_underlying_price", "underlying_entry_price", "planned_entry_price") or _level(entry_trigger))
    invalidation_price = _number(_first(source, "initial_invalidation", "invalidation_price", "structural_invalidation") or _level(invalidation))
    target_1 = _target_from(source, "target_1_price", targets, 0)
    target_2 = _target_from(source, "target_2_price", targets, 1)
    runner = _number(_first(source, "runner_target_price", "stretch_target"))
    option_entry = _option_entry(source, contract)
    option_stop = _number(_first(source, "option_stop_price", "stop_premium"))
    option_target_1 = _number(source.get("option_target_1_price"))
    option_target_2 = _number(source.get("option_target_2_price"))
    side = _side(source)
    risk_distance = abs(underlying_entry - invalidation_price) if underlying_entry is not None and invalidation_price is not None else None
    risk_per_contract = risk_distance * 100.0 if risk_distance is not None else None
    option_risk_per_contract = abs(option_entry - option_stop) * 100.0 if option_entry is not None and option_stop is not None else None
    max_holding_days = _integer(source.get("maximum_holding_days"), 5)
    time_stop = source.get("time_stop") or f"Exit after {max_holding_days} trading days if target has not been reached or the thesis has not strengthened."
    vwap_condition = "Completed management-timeframe close below VWAP with a lower high and negative volume confirmation." if side == "LONG" else "Completed management-timeframe close above VWAP with a higher low and positive volume confirmation."
    trend_condition = "Exit on a confirmed break of higher-low structure or key support." if side == "LONG" else "Exit on a confirmed break of lower-high structure or key resistance."
    plan = {
        "version": EXIT_PLAN_VERSION,
        "status": "COMPLETE" if all(value is not None for value in (underlying_entry, invalidation_price, target_1, target_2)) else "INCOMPLETE",
        "side": side,
        "setup_timeframe": str(source.get("setup_timeframe") or "15m"),
        "execution_timeframe": str(source.get("execution_timeframe") or "5m"),
        "management_timeframe": _management_timeframe(source),
        "entry_trigger": entry_trigger or {"price": underlying_entry},
        "planned_entry_underlying": underlying_entry,
        "structural_invalidation": invalidation_price,
        "initial_stop": invalidation_price,
        "risk_distance_underlying": risk_distance,
        "risk_per_contract_underlying": risk_per_contract,
        "option_entry_price": option_entry,
        "option_stop_price": option_stop,
        "option_risk_per_contract": option_risk_per_contract,
        "target_1": target_1,
        "target_2": target_2,
        "runner_target": runner,
        "option_value_at_levels": {
            "entry": option_entry,
            "invalidation": option_stop,
            "target_1": option_target_1,
            "target_2": option_target_2,
            "runner": _number(source.get("option_runner_target_price")),
            "status": "observed" if any(value is not None for value in (option_stop, option_target_1, option_target_2)) else "unavailable_without_historical_or_model_inputs",
        },
        "maximum_holding_days": max_holding_days,
        "time_stop": time_stop,
        "vwap_exit_condition": vwap_condition,
        "trend_break_exit_condition": trend_condition,
        "catalyst_exit_condition": str(source.get("catalyst_exit_condition") or "Exit or reduce before an unmodeled binary catalyst; do not hold through thesis-changing news."),
        "end_of_day_exit_rule": "Losing options positions must be closed before the regular-session cutoff; green positions require a separate overnight gate.",
        "overnight_eligible": bool(source.get("overnight_eligible") or source.get("overnight")),
        "missing_fields": [
            name for name, value in {
                "planned_entry_underlying": underlying_entry,
                "structural_invalidation": invalidation_price,
                "target_1": target_1,
                "target_2": target_2,
            }.items() if value is None
        ],
    }
    return plan


def _conservative_option_mark(position: dict[str, Any]) -> tuple[float | None, str]:
    side = _side(position)
    bid = _number(position.get("bid"))
    ask = _number(position.get("ask"))
    current = _number(position.get("current_option_price") or position.get("current_price") or position.get("last"))
    if side == "LONG" and bid is not None and bid > 0:
        return bid, "BID"
    if side == "SHORT" and ask is not None and ask > 0:
        return ask, "ASK"
    return current, "CONSERVATIVE_MARK" if current is not None else "UNAVAILABLE"


def _r_value(entry: float | None, current: float | None, invalidation: float | None, side: str) -> float | None:
    if entry is None or current is None or invalidation is None:
        return None
    risk = abs(entry - invalidation)
    if risk <= 0:
        return None
    favorable = current - entry if side == "LONG" else entry - current
    return favorable / risk


def _parse_now(value: Any = None) -> datetime:
    if isinstance(value, datetime):
        parsed = value
    else:
        try:
            parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00")) if value else datetime.now(timezone.utc)
        except (TypeError, ValueError):
            parsed = datetime.now(timezone.utc)
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(EASTERN)


def _structural_stop(side: str, current: float | None, indicators: dict[str, Any]) -> tuple[float | None, str]:
    if current is None:
        return None, "UNAVAILABLE"
    candidates = []
    if side == "LONG":
        for key, label in (("swing_low", "SWING_LOW"), ("ema21", "EMA_21"), ("vwap", "VWAP")):
            value = _number(indicators.get(key) or indicators.get({"ema21": "ema_slow", "swing_low": "support", "vwap": "vwap"}.get(key)))
            if value is not None and value < current:
                candidates.append((value, label))
        return max(candidates, default=(None, "UNAVAILABLE"), key=lambda item: item[0])
    for key, label in (("swing_high", "SWING_HIGH"), ("ema21", "EMA_21"), ("vwap", "VWAP")):
        value = _number(indicators.get(key) or indicators.get({"ema21": "ema_slow", "swing_high": "resistance", "vwap": "vwap"}.get(key)))
        if value is not None and value > current:
            candidates.append((value, label))
    return min(candidates, default=(None, "UNAVAILABLE"), key=lambda item: item[0])


def evaluate_exit_management(
    position: dict[str, Any],
    plan: dict[str, Any],
    *,
    indicators: dict[str, Any] | None = None,
    state: dict[str, Any] | None = None,
    market_session: dict[str, Any] | None = None,
    now: Any = None,
    config: dict[str, Any] | None = None,
) -> dict[str, Any]:
    indicators = indicators or position.get("management_context") or {}
    state = state or {}
    config = config or {}
    side = str(plan.get("side") or _side(position)).upper()
    current_underlying = _current_underlying(position, indicators)
    entry_underlying = _number(plan.get("planned_entry_underlying"))
    invalidation = _number(plan.get("structural_invalidation"))
    current_r = _r_value(entry_underlying, current_underlying, invalidation, side)
    peak_r = max(_number(state.get("peak_r")) or float("-inf"), current_r if current_r is not None else float("-inf"))
    peak_r = peak_r if math.isfinite(peak_r) else None
    mae_r = min(_number(state.get("maximum_adverse_excursion_r")) or 0.0, current_r if current_r is not None else 0.0)
    option_mark, mark_basis = _conservative_option_mark(position)
    option_entry = _number(plan.get("option_entry_price"))
    option_return = ((option_mark - option_entry) / option_entry * 100.0) if option_mark is not None and option_entry and side == "LONG" else ((option_entry - option_mark) / option_entry * 100.0) if option_mark is not None and option_entry and side == "SHORT" else None
    peak_option = max(_number(state.get("peak_option_value")) or float("-inf"), option_mark if option_mark is not None else float("-inf"))
    peak_option = peak_option if math.isfinite(peak_option) else None
    peak_return = ((peak_option - option_entry) / option_entry * 100.0) if peak_option is not None and option_entry and side == "LONG" else ((option_entry - peak_option) / option_entry * 100.0) if peak_option is not None and option_entry and side == "SHORT" else None
    giveback_r = max(0.0, (peak_r or 0.0) - (current_r or 0.0))
    giveback_pct = max(0.0, (peak_return or 0.0) - (option_return or 0.0))
    target_1 = _number(plan.get("target_1"))
    target_2 = _number(plan.get("target_2"))
    target_1_reached = current_underlying is not None and target_1 is not None and ((current_underlying >= target_1) if side == "LONG" else (current_underlying <= target_1))
    invalidated = current_underlying is not None and invalidation is not None and ((current_underlying <= invalidation) if side == "LONG" else (current_underlying >= invalidation))
    vwap = _number(indicators.get("vwap"))
    vwap_lost = current_underlying is not None and vwap is not None and ((current_underlying < vwap) if side == "LONG" else (current_underlying > vwap))
    candle_confirmed = bool(indicators.get("completed_candle", True))
    structural_stop, structural_method = _structural_stop(side, current_underlying, indicators)
    activation_return = float(config.get("profit_trail_activation_pct", 15.0) or 15.0)
    activated = bool((option_return is not None and option_return >= activation_return) or (current_r is not None and current_r >= 1.0) or target_1_reached)
    trail_mode = str(config.get("trailing_mode", "HYBRID") or "HYBRID").upper()
    mechanical_option_stop = peak_option * (1.0 - float(config.get("profit_trail_pct", 5.0) or 5.0) / 100.0) if activated and peak_option is not None and side == "LONG" else None
    prior_structural_stop = _number(state.get("structural_stop_price"))
    stop_level = structural_stop if activated else invalidation
    if prior_structural_stop is not None:
        stop_level = max(stop_level or prior_structural_stop, prior_structural_stop) if side == "LONG" else min(stop_level or prior_structural_stop, prior_structural_stop)
    if not activated:
        stop_method = "INITIAL_INVALIDATION"
    elif trail_mode == "STRICT_5_PERCENT":
        stop_method = "OPTION_5_PERCENT_TRAIL"
    elif trail_mode in {"STRUCTURE_AWARE", "HYBRID", "TIGHTER_OF_BOTH"}:
        stop_method = f"STRUCTURE_AWARE_{structural_method}"
    else:
        stop_method = "STRUCTURE_AWARE"

    if invalidated:
        state_name, decision = "THESIS INVALIDATED", "CLOSE"
        reason = "Underlying reached the original structural invalidation."
    elif candle_confirmed and vwap_lost and (activated or (current_r is not None and current_r <= 0)):
        state_name, decision = "TREND BROKEN", "CLOSE"
        reason = "Completed management-timeframe candle lost VWAP after profit protection was active."
    elif target_1_reached and activated and giveback_pct >= float(config.get("max_profit_giveback_pct", 5.0) or 5.0):
        state_name, decision = "EXIT REQUIRED", "TAKE PARTIAL"
        reason = "Target 1 was reached and the position is giving back more than the allowed profit giveback."
    elif target_1_reached:
        state_name, decision = "TARGET 1 REACHED", "TAKE PARTIAL"
        reason = "Price entered the first planned target zone."
    elif activated and giveback_pct >= float(config.get("max_profit_giveback_pct", 5.0) or 5.0):
        state_name, decision = "MOMENTUM WEAKENING", "PROTECT PROFIT"
        reason = "Peak option profit is giving back faster than the active protection limit."
    elif activated:
        state_name, decision = "PROFIT PROTECTION", "HOLD RUNNER"
        reason = "The position reached a profit-protection threshold; the stop may only tighten."
    elif current_r is not None and current_r > 0:
        state_name, decision = "TRADE WORKING", "HOLD"
        reason = "The position remains favorable and has not reached the first target."
    elif current_r is not None:
        state_name, decision = "INITIAL RISK", "HOLD"
        reason = "The trade has not reached a profit-protection threshold."
    else:
        state_name, decision = "DATA INSUFFICIENT", "DATA REFRESH REQUIRED"
        reason = "Underlying entry, invalidation, or current price is unavailable."

    current_et = _parse_now(now)
    session_state = str((market_session or {}).get("session_state") or "").upper()
    eod_review = session_state in {"REGULAR", "EARLY_CLOSE"} and current_et.time() >= dtime(15, 30)
    eod_cutoff = session_state in {"REGULAR", "EARLY_CLOSE"} and current_et.time() >= dtime(15, 50)
    losing = (option_return is not None and option_return < 0) or (current_r is not None and current_r < 0)
    overnight_probability = _number(position.get("overnight_directional_probability"))
    overnight_sample = _integer(position.get("overnight_sample_size"))
    overnight_ok = bool(not losing and overnight_probability is not None and overnight_probability >= float(config.get("overnight_probability_threshold_pct", 70.0) or 70.0) and overnight_sample >= _integer(config.get("minimum_overnight_sample"), 30) and bool(plan.get("overnight_eligible")))
    overnight_action = "CLOSE BEFORE MARKET END" if losing else "HOLD REDUCED SIZE" if overnight_ok else "OVERNIGHT CONFIDENCE INSUFFICIENT"
    if eod_review and losing:
        decision = "CLOSE"
        state_name = "EXIT REQUIRED"
        reason = "Losing option positions are not eligible to remain open overnight."

    warnings = []
    max_giveback = float(config.get("max_profit_giveback_pct", 5.0) or 5.0)
    if giveback_pct >= max_giveback:
        warnings.append("EXCESSIVE PROFIT GIVEBACK")
    if (peak_r or 0.0) > 0 and (current_r or 0.0) < 0:
        warnings.append("WINNER AT RISK OF TURNING INTO LOSER")
    elif (peak_r or 0.0) >= 1.0 and giveback_pct > 0:
        warnings.append("MODERATE PROFIT GIVEBACK")

    return {
        "version": EXIT_PLAN_VERSION,
        "decision": decision,
        "state": state_name,
        "reason": reason,
        "entry": entry_underlying,
        "current_underlying": current_underlying,
        "current_option_value": option_mark,
        "option_mark_basis": mark_basis,
        "current_r": current_r,
        "peak_r": peak_r,
        "realized_r": _number(state.get("realized_r")),
        "mfe_r": max(0.0, peak_r or 0.0),
        "mae_r": mae_r,
        "peak_option_return_pct": peak_return,
        "peak_option_value": peak_option,
        "current_option_return_pct": option_return,
        "profit_giveback_r": giveback_r,
        "profit_giveback_pct": giveback_pct,
        "target_progress": "TARGET 1 REACHED" if target_1_reached else "BEFORE TARGET 1",
        "target_1_reached": target_1_reached,
        "invalidated": invalidated,
        "vwap_status": "LOST" if vwap_lost else "HOLDING" if vwap is not None else "UNAVAILABLE",
        "trend_status": "BROKEN" if vwap_lost and candle_confirmed else "INTACT" if current_underlying is not None else "UNAVAILABLE",
        "volume_status": str(indicators.get("volume_status") or "UNAVAILABLE"),
        "stop_level": stop_level,
        "structural_stop_price": stop_level,
        "stop_method": stop_method,
        "mechanical_option_stop": mechanical_option_stop,
        "structural_stop": structural_stop,
        "structural_stop_method": structural_method,
        "trailing_mode": trail_mode,
        "profit_trail_active": activated,
        "next_exit_condition": plan.get("vwap_exit_condition") if not invalidated else plan.get("structural_invalidation"),
        "hard_truth": "The active stop is only theoretical until an executable quote and actual order fill exist." if decision not in {"CLOSE", "DATA REFRESH REQUIRED"} else reason,
        "next_action": reason,
        "warnings": warnings,
        "eod_review": eod_review,
        "eod_cutoff_reached": eod_cutoff,
        "losing_position": losing,
        "overnight_action": overnight_action,
        "overnight_probability": overnight_probability,
        "overnight_sample_size": overnight_sample,
        "state_changed": state.get("state") != state_name,
    }


def detect_exit_behavior(trade: dict[str, Any]) -> dict[str, Any]:
    """Classify fear-based early exits and greed-based late exits from known path metrics."""
    peak_r = _number(trade.get("peak_r"))
    exit_r = _number(trade.get("exit_r") or trade.get("realized_r"))
    invalidated_before_exit = bool(trade.get("invalidation_before_exit") or trade.get("thesis_invalidated"))
    target_reached = bool(trade.get("target_1_reached") or trade.get("target_reached"))
    early = exit_r is not None and exit_r < 1.0 and not invalidated_before_exit and not target_reached
    late = peak_r is not None and peak_r >= 1.0 and exit_r is not None and (exit_r <= 0 or peak_r - exit_r >= float(trade.get("max_allowed_giveback_r") or 0.75))
    labels = []
    if early:
        labels.append("FEAR_BASED_EARLY_EXIT")
    if late:
        labels.append("GREED_BASED_LATE_EXIT")
    captured = (exit_r / peak_r) if peak_r and peak_r > 0 and exit_r is not None else None
    return {
        "labels": labels,
        "fear_based_early_exit": early,
        "greed_based_late_exit": late,
        "profit_captured_vs_mfe": captured,
        "explanation": "Exit occurred before the thesis failed." if early else "Exit surrendered a material portion of peak profit." if late else "No early or late exit pattern detected from supplied path metrics.",
        "data_status": "observed" if peak_r is not None and exit_r is not None else "unavailable",
    }


def is_complete_exit_plan(plan: dict[str, Any] | None) -> bool:
    return bool(plan and plan.get("status") == "COMPLETE" and not plan.get("missing_fields"))
