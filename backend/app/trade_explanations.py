from __future__ import annotations

from datetime import date
from math import isfinite
from typing import Any


BAD_QUOTE_TYPES = {"CLOSING", "DELAYED", "SANDBOX"}


def _num(value: Any, default: float | None = None) -> float | None:
    try:
        if value is None:
            return default
        number = float(value)
        if not isfinite(number):
            return default
        return number
    except Exception:
        return default


def _round(value: Any, digits: int = 2) -> float | None:
    number = _num(value)
    if number is None:
        return None
    return round(number, digits)


def _money(value: Any) -> str:
    number = _num(value)
    if number is None:
        return "-"
    return f"${number:.2f}"


def _pct(value: Any) -> str:
    number = _num(value)
    if number is None:
        return "-"
    return f"{number:.2f}%"


def _side_type(side: str) -> str:
    normalized = str(side or "").upper()
    if normalized == "LONG":
        return "CALL"
    if normalized == "SHORT":
        return "PUT"
    return ""


def _latest(indicators: dict[str, Any] | None, scan: dict[str, Any] | None) -> dict[str, Any]:
    indicators = indicators or {}
    latest = indicators.get("latest") if isinstance(indicators.get("latest"), dict) else {}
    scan_indicators = (scan or {}).get("indicators") if isinstance((scan or {}).get("indicators"), dict) else {}
    return {**scan_indicators, **latest, **indicators}


def _resolve_live_underlying_price(candidate: dict[str, Any] | None, contracts: dict[str, Any] | None) -> float | None:
    live = _num((candidate or {}).get("underlying_price"))
    if live is not None and live > 0:
        return live
    contract_level = _num((contracts or {}).get("underlying_price"))
    if contract_level is not None and contract_level > 0:
        return contract_level
    return None


def _market_context(side: str, indicators: dict[str, Any] | None, scan: dict[str, Any] | None, live_underlying_price: Any = None) -> dict[str, Any]:
    latest = _latest(indicators, scan)
    live = _num(live_underlying_price)
    price = live if live is not None and live > 0 else 0.0
    atr = _num(latest.get("atr"))
    if atr is None or atr <= 0:
        atr = max(price * 0.01, 0.25)
    buffer = max(atr * 0.08, price * 0.0015, 0.01)
    return {
        "side": str(side or "").upper(),
        "price": price,
        "atr": atr,
        "buffer": buffer,
        "vwap": _num(latest.get("vwap")),
        "ema_fast": _num(latest.get("ema_fast")),
        "ema_slow": _num(latest.get("ema_slow")),
        "ema_trend": _num(latest.get("ema_trend")),
        "bb_upper": _num(latest.get("bb_upper")),
        "bb_mid": _num(latest.get("bb_mid")),
        "bb_lower": _num(latest.get("bb_lower")),
    }


def _above(current: float, values: list[float | None]) -> float | None:
    valid = [value for value in values if value is not None and value > current]
    return min(valid) if valid else None


def _below(current: float, values: list[float | None]) -> float | None:
    valid = [value for value in values if value is not None and value < current]
    return max(valid) if valid else None


def calculate_entry_trigger(side: str, indicators: dict[str, Any] | None, scan: dict[str, Any] | None = None, live_underlying_price: Any = None) -> dict[str, Any]:
    ctx = _market_context(side, indicators, scan, live_underlying_price)
    price = ctx["price"]
    if price <= 0:
        return {
            "type": "breakout",
            "price": None,
            "condition": "No reliable underlying trigger is available until current price data refreshes.",
            "confirmation_needed": "Fresh candle data with volume is required before entry.",
        }

    if ctx["side"] == "SHORT":
        vwap_reject = ctx["vwap"] is not None and price >= ctx["vwap"]
        support = _below(price, [ctx["vwap"], ctx["ema_fast"], ctx["ema_slow"], ctx["bb_lower"]])
        trigger = _round((ctx["vwap"] if vwap_reject else support) or (price - max(ctx["atr"] * 0.25, ctx["buffer"])))
        return {
            "type": "vwap_reject" if vwap_reject else "breakdown",
            "price": trigger,
            "condition": f"Enter only if price breaks below {_money(trigger)} with volume and stays below VWAP.",
            "confirmation_needed": f"5-minute candle close below {_money(trigger)}, volume above recent average, EMA 8 not reclaiming EMA 21, and MACD still falling.",
        }

    vwap_reclaim = ctx["vwap"] is not None and price <= ctx["vwap"]
    resistance = _above(price, [ctx["vwap"], ctx["ema_fast"], ctx["ema_slow"], ctx["bb_upper"]])
    trigger = _round((ctx["vwap"] if vwap_reclaim else resistance) or (price + max(ctx["atr"] * 0.25, ctx["buffer"])))
    return {
        "type": "vwap_reclaim" if vwap_reclaim else "breakout",
        "price": trigger,
        "condition": f"Enter only if price breaks above {_money(trigger)} with volume and holds above VWAP.",
        "confirmation_needed": f"5-minute candle close above {_money(trigger)}, volume above recent average, EMA 8 holding above EMA 21, and MACD still rising.",
    }


def calculate_invalidation(side: str, indicators: dict[str, Any] | None, scan: dict[str, Any] | None = None, live_underlying_price: Any = None) -> dict[str, Any]:
    ctx = _market_context(side, indicators, scan, live_underlying_price)
    price = ctx["price"]
    if price <= 0:
        return {
            "price": None,
            "condition": "Setup is invalid until fresh underlying price data is available.",
        }

    if ctx["side"] == "SHORT":
        resistance = _above(price, [ctx["vwap"], ctx["ema_fast"], ctx["ema_slow"], ctx["bb_mid"]])
        invalidation = _round(resistance or (price + max(ctx["atr"] * 0.6, price * 0.0035)))
        return {
            "price": invalidation,
            "condition": f"Setup fails if price reclaims {_money(invalidation)} or closes back above VWAP.",
        }

    support = _below(price, [ctx["vwap"], ctx["ema_fast"], ctx["ema_slow"], ctx["bb_mid"]])
    invalidation = _round(support or (price - max(ctx["atr"] * 0.6, price * 0.0035)))
    return {
        "price": invalidation,
        "condition": f"Setup fails if price loses {_money(invalidation)} or closes back below VWAP.",
    }


def calculate_targets(side: str, indicators: dict[str, Any] | None, scan: dict[str, Any] | None = None, live_underlying_price: Any = None) -> dict[str, Any]:
    ctx = _market_context(side, indicators, scan, live_underlying_price)
    entry = _num(calculate_entry_trigger(side, indicators, scan, live_underlying_price).get("price"), ctx["price"])
    if not entry:
        return {
            "target_1": None,
            "target_2": None,
            "stretch_target": None,
            "basis": "current price data unavailable",
        }

    if ctx["side"] == "SHORT":
        target_1 = _round(entry - max(ctx["atr"] * 0.5, ctx["price"] * 0.004))
        target_2 = _round(entry - max(ctx["atr"], ctx["price"] * 0.008))
        stretch = _round(entry - max(ctx["atr"] * 1.5, ctx["price"] * 0.012))
        return {
            "target_1": target_1,
            "target_2": target_2,
            "stretch_target": stretch,
            "basis": "prior low, recent support, VWAP extension, Bollinger lower band, and ATR extension",
        }

    target_1 = _round(entry + max(ctx["atr"] * 0.5, ctx["price"] * 0.004))
    target_2 = _round(entry + max(ctx["atr"], ctx["price"] * 0.008))
    stretch = _round(entry + max(ctx["atr"] * 1.5, ctx["price"] * 0.012))
    return {
        "target_1": target_1,
        "target_2": target_2,
        "stretch_target": stretch,
        "basis": "prior high, recent resistance, VWAP extension, Bollinger upper band, and ATR extension",
    }


def calculate_option_execution(candidate: dict[str, Any] | None, side: str) -> dict[str, Any]:
    candidate = candidate or {}
    bid = _num(candidate.get("bid"))
    ask = _num(candidate.get("ask"))
    last = _num(candidate.get("last"))
    spread = _num(candidate.get("spread_percentage"))
    stale = bool(candidate.get("quote_stale"))
    has_bid_ask = bid is not None and ask is not None and bid > 0 and ask > 0
    fair = ((bid + ask) / 2) if has_bid_ask else last
    contract_type = str(candidate.get("type") or _side_type(side) or "").upper()
    contract_name = candidate.get("contract_symbol") or " ".join(
        str(item)
        for item in [contract_type, candidate.get("expiration"), candidate.get("strike")]
        if item
    ) or "-"
    same_day = str(candidate.get("expiration") or "")[:10] == date.today().isoformat()
    avoid = [
        "Avoid if spread widens above 5% or ask is more than 10% above last fair value.",
        f"Current spread is wide at {spread:.2f}%, so do not chase the ask." if spread is not None and spread > 5 else None,
        "Quote is stale, so premium targets are not reliable until the option quote refreshes." if stale else None,
        "Same-day expiration requires tighter entries, faster exits, and no chasing." if same_day else None,
    ]
    avoid_if = " ".join(item for item in avoid if item)

    if fair is None or fair <= 0 or stale:
        return {
            "candidate_contract": contract_name,
            "max_reasonable_entry": None,
            "ideal_entry_zone": "Wait for a fresh quote before setting an entry zone." if stale else "No reliable bid/ask/last premium is available.",
            "take_profit_1": None,
            "take_profit_2": None,
            "stop_premium": None,
            "avoid_if": avoid_if,
        }

    high_zone = ask if has_bid_ask and spread is not None and spread <= 5 else fair * 1.03
    low_zone = max(bid or 0, fair * 0.97) if has_bid_ask else fair * 0.97
    max_reasonable = min((ask or fair) * 1.02, fair * 1.08) if has_bid_ask else fair * 1.05
    return {
        "candidate_contract": contract_name,
        "max_reasonable_entry": _round(max_reasonable),
        "ideal_entry_zone": f"{_money(low_zone)} - {_money(high_zone)}",
        "take_profit_1": _round(fair * 1.25),
        "take_profit_2": _round(fair * 1.4),
        "stop_premium": _round(fair * 0.75),
        "avoid_if": avoid_if,
    }


def explain_failure_reasons(
    blockers: list[str],
    candidate: dict[str, Any] | None,
    contracts: dict[str, Any] | None,
    scan: dict[str, Any] | None,
    backtest: dict[str, Any] | None,
    ai_gate: dict[str, Any] | None,
) -> str:
    candidate = candidate or {}
    contracts = contracts or {}
    backtest = backtest or {}
    ai_gate = ai_gate or {}
    facts: list[str] = []
    spread = _num(candidate.get("spread_percentage"))
    max_spread = _num(candidate.get("recommended_max_spread_pct"), 5.0) or 5.0
    volume = _num(candidate.get("volume"))
    min_volume = _num(candidate.get("minimum_volume"), 100.0) or 100.0
    win_rate = _num(backtest.get("win_rate_pct"))
    quote_type = str(candidate.get("quote_type") or "").upper()
    underlying_price = _num(candidate.get("underlying_price"))
    if underlying_price is None or underlying_price <= 0:
        underlying_price = _num(contracts.get("underlying_price"))

    if spread is not None and spread > max_spread:
        facts.append(f"spread is {spread:.2f}% against a {max_spread:.2f}% maximum")
    if volume is not None and volume < min_volume:
        facts.append(f"volume is {int(volume)}, below the {int(min_volume)} minimum")
    if win_rate is not None and win_rate < 52:
        facts.append(f"historical win rate is {win_rate:.2f}%, below 52%")
    if candidate.get("quote_stale"):
        facts.append("the option quote is stale")
    if quote_type in BAD_QUOTE_TYPES:
        facts.append(f"quote type is not live or actionable ({quote_type})")
    if underlying_price is None or underlying_price <= 0:
        facts.append("live underlying price is unavailable")
    if ai_gate.get("decision") != "PROCEED":
        facts.append("AI Gate did not return PROCEED")
    if not facts:
        facts.extend(str(item) for item in blockers[:4])
    if not facts:
        return "the required gates have not all passed yet"
    return "; ".join(facts)


def explain_watch_conditions(
    side: str,
    indicators: dict[str, Any] | None,
    candidate: dict[str, Any] | None,
    live_underlying_price: Any = None,
) -> list[str]:
    trigger = calculate_entry_trigger(side, indicators, live_underlying_price=live_underlying_price)
    invalidation = calculate_invalidation(side, indicators, live_underlying_price=live_underlying_price)
    expected = _side_type(side)
    direction_text = "breaking below" if str(side).upper() == "SHORT" else "breaking above"
    vwap_text = "rejecting VWAP" if str(side).upper() == "SHORT" else "holding VWAP"
    min_volume = int(_num((candidate or {}).get("minimum_volume"), 100.0) or 100)
    max_spread = _num((candidate or {}).get("recommended_max_spread_pct"), 5.0) or 5.0
    if trigger.get("price") is None:
        return [
            "Watch for the live E*TRADE underlying quote to load before defining a trigger.",
            f"Do not act until the selected {expected or 'option'} confirms liquidity and the AI Gate can evaluate a live price.",
        ]
    return [
        f"Watch for price {direction_text} {_money(trigger.get('price'))} on a 5-minute candle with volume above recent average.",
        f"Watch for {vwap_text}; do not act if price immediately rejects the trigger or loses VWAP.",
        f"Watch that the selected {expected or 'option'} stays at or below a {max_spread:.2f}% spread and volume stays at or above {min_volume}.",
        f"Cancel the idea if underlying price trades through {_money(invalidation.get('price'))}.",
    ]


def build_trade_explanation(
    candidate: dict[str, Any] | None,
    scan: dict[str, Any] | None,
    indicators: dict[str, Any] | None,
    contracts: dict[str, Any] | None,
    ratios: dict[str, Any] | None,
    backtest: dict[str, Any] | None,
    ai_gate: dict[str, Any] | None,
) -> dict[str, Any]:
    del ratios
    candidate = candidate or {}
    scan = scan or {}
    contracts = contracts or {}
    backtest = backtest or {}
    ai_gate = ai_gate or {}
    side = str(ai_gate.get("side") or scan.get("side") or "").upper()
    final_decision = str(ai_gate.get("final_decision") or ("TRADE_CANDIDATE" if ai_gate.get("decision") == "PROCEED" else "NO_TRADE")).upper()
    symbol = scan.get("symbol") or candidate.get("symbol") or "This setup"
    live_underlying_price = _resolve_live_underlying_price(candidate, contracts)
    trigger = calculate_entry_trigger(side, indicators, scan, live_underlying_price)
    invalidation = calculate_invalidation(side, indicators, scan, live_underlying_price)
    targets = calculate_targets(side, indicators, scan, live_underlying_price)
    execution = calculate_option_execution(candidate, side)
    blockers = list(ai_gate.get("blocking_factors") or [])
    reason_text = explain_failure_reasons(blockers, candidate, contracts, scan, backtest, ai_gate)
    win_rate = _pct(backtest.get("win_rate_pct"))
    occurrences = backtest.get("occurrences", 0)
    historical_edge = str(backtest.get("historical_edge") or "UNKNOWN").upper()
    sample_confidence = str(backtest.get("sample_confidence") or "UNKNOWN").upper()
    expected = _side_type(side) or str(candidate.get("type") or "option").upper()
    trigger_direction = "below" if side == "SHORT" else "above"
    trigger_price = _num(trigger.get("price"))
    trigger_available = trigger_price is not None and trigger_price > 0

    approved = final_decision in {"TRADE_CANDIDATE", "HIGH_CONVICTION"} and ai_gate.get("decision") == "PROCEED"
    if approved:
        summary = (
            f"{symbol} is a {'high-conviction setup' if final_decision == 'HIGH_CONVICTION' else 'trade candidate'} "
            f"because the chart is confirmed, historical edge is {historical_edge} ({win_rate} over {occurrences} sessions), "
            f"the selected {expected} passed liquidity and data checks, and AI Gate returned PROCEED. "
            f"{'Entry is valid only ' + trigger_direction + ' ' + _money(trigger_price) + ' after confirmation.' if trigger_available else 'The live E*TRADE quote is not available yet, so the trigger and invalidation levels are not actionable.'}"
        )
        why = "This passed because Chart Signal, Historical Edge, Option Liquidity, Data Quality, and AI Gate all passed."
    elif final_decision == "WAIT_FOR_CONFIRMATION":
        summary = (
            f"{symbol} is waiting for confirmation because direction is possible but price has not cleared the required level. "
            f"{'Do not enter until a 5-minute candle closes ' + trigger_direction + ' ' + _money(trigger_price) + ' with VWAP and volume confirmation.' if trigger_available else 'Do not enter until the live E*TRADE quote loads and VWAP/volume confirm.'}"
        )
        why = f"This was downgraded to WAIT_FOR_CONFIRMATION because {reason_text}."
    elif final_decision == "WATCH":
        summary = (
            f"{symbol} is only a watch because {reason_text}. "
            f"{'A cleaner price confirmation or better ' + expected + ' is needed before this can become a trade candidate.' if trigger_available else 'A live E*TRADE quote is still needed before this can become a trade candidate.'}"
        )
        why = f"This did not receive final approval because {reason_text}."
    else:
        summary = (
            f"No trade. The setup failed because {reason_text}. "
            f"{'It needs the blocking conditions to clear before it becomes actionable.' if trigger_available else 'A live E*TRADE quote is still needed before it can be evaluated cleanly.'}"
        )
        why = f"This failed because {reason_text}."

    max_spread = _num(candidate.get("recommended_max_spread_pct") or contracts.get("recommended_max_spread_pct"), 5.0) or 5.0
    min_volume = int(_num(candidate.get("minimum_volume") or (contracts.get("filters") or {}).get("min_volume"), 100.0) or 100)
    invalidation_price = _num(invalidation.get("price"))
    invalidation_available = invalidation_price is not None and invalidation_price > 0
    upgrade_conditions = [
        (
            f"Upgrade only after a 5-minute close {trigger_direction} {_money(trigger.get('price'))} with volume above recent average."
            if trigger_available
            else "Upgrade only after the live E*TRADE quote loads and a trigger can be confirmed with volume."
        ),
        "AI Gate must return PROCEED." if ai_gate.get("decision") != "PROCEED" else None,
        "Historical win rate must be at least 52% with sample confidence MEDIUM or ENOUGH." if _num(backtest.get("win_rate_pct"), 0.0) < 52 else None,
        f"The selected {expected} must keep spread at or below {max_spread:.2f}% and volume at or above {min_volume}.",
    ]
    downgrade_conditions = [
        (
            f"Downgrade if price fails to hold the trigger near {_money(trigger.get('price'))} after the 5-minute close."
            if trigger_available
            else "Downgrade until the live E*TRADE quote loads and a trigger can be confirmed."
        ),
        (
            f"Downgrade immediately if price trades through {_money(invalidation_price)}."
            if invalidation_available
            else "Downgrade until invalidation can be measured from a live E*TRADE quote."
        ),
        f"Downgrade if spread widens above {max_spread:.2f}% or volume drops below {min_volume}.",
        "Downgrade if quote becomes stale, CLOSING, DELAYED, or SANDBOX.",
        "Downgrade if AI Gate returns DO_NOT_PROCEED after refreshed facts.",
    ]
    cancel_conditions = [
        (
            f"Cancel if underlying price violates {_money(invalidation_price)}."
            if invalidation_available
            else "Cancel until a live E*TRADE quote is available."
        ),
        "Cancel if the quote is stale or quote type is CLOSING, DELAYED, or SANDBOX.",
        "Cancel if AI Gate returns DO_NOT_PROCEED.",
        "Cancel until historical win rate is at least 52%." if _num(backtest.get("win_rate_pct"), 100.0) < 52 else None,
    ]

    return {
        "final_decision": final_decision,
        "plain_english_summary": summary,
        "why_passed_or_failed": why,
        "watch_for": explain_watch_conditions(
            side,
            {**(indicators or {}), "latest": _latest(indicators, scan)},
            candidate,
            live_underlying_price,
        ),
        "entry_trigger": trigger,
        "invalidation": invalidation,
        "targets": targets,
        "option_execution": execution,
        "underlying_reference": (
            {
                "source": "etrade_live",
                "label": "Live E*TRADE price",
                "price": live_underlying_price,
            }
            if live_underlying_price is not None and live_underlying_price > 0
            else {
                "source": "etrade_live_unavailable",
                "label": "Live E*TRADE price unavailable",
                "price": None,
            }
        ),
        "upgrade_conditions": [item for item in upgrade_conditions if item],
        "downgrade_conditions": downgrade_conditions,
        "cancel_conditions": [item for item in cancel_conditions if item],
        "historical_context": {
            "sample_confidence": sample_confidence,
            "historical_edge": historical_edge,
            "win_rate_pct": backtest.get("win_rate_pct"),
            "occurrences": occurrences,
        },
    }
