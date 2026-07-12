from __future__ import annotations

import json
import os
from datetime import datetime, timezone
from typing import Any

import requests

from .config import config_manager
from .providers.option_scoring import BAD_QUOTE_TYPES, CONFIRMED_SCAN_GRADES
from .providers.rate_limiter import call_with_rate_limit
from .trade_explanations import build_trade_explanation


PREPROMPT_VERSION = "trade-gate-v1"
OPENAI_RESPONSES_URL = "https://api.openai.com/v1/responses"

TRADE_GATE_PREPROMPT = """
You are an options trade validation gate, not a trade promoter.
Return PROCEED only when the supplied facts support the setup and every required gate is satisfied.
Return DO_NOT_PROCEED when any fact is missing, stale, contradictory, risky, or below threshold.
Do not reject solely because optional Greeks, delta, or implied volatility are missing; instead treat option impact estimates as unavailable.
Use only the facts in the JSON payload. Do not invent market data, catalysts, news, fundamentals, or probabilities.
If returning DO_NOT_PROCEED, provide exactly 3 or 4 concrete sentences explaining why.
Output must match the provided JSON schema.
""".strip()


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


def _sample_confidence(occurrences: Any) -> str:
    total = _safe_int(occurrences, 0)
    if total < 20:
        return "LOW"
    if total < 50:
        return "MEDIUM"
    return "ENOUGH"


def _historical_edge(win_rate: Any) -> str:
    if win_rate is None:
        return "UNKNOWN"
    rate = _safe_float(win_rate, -1.0)
    if rate < 0:
        return "UNKNOWN"
    if rate < 52:
        return "WEAK"
    if rate <= 56:
        return "SLIGHT"
    if rate <= 60:
        return "MODERATE"
    return "STRONG"


def _historical_confidence_ok(backtest: dict[str, Any]) -> bool:
    sample = str(backtest.get("sample_confidence") or _sample_confidence(backtest.get("occurrences"))).upper()
    edge = str(backtest.get("historical_edge") or _historical_edge(backtest.get("win_rate_pct"))).upper()
    return sample in {"MEDIUM", "ENOUGH"} and edge in {"SLIGHT", "MODERATE", "STRONG"}


def _sentence_join(sentences: list[str]) -> str:
    cleaned = []
    for sentence in sentences:
        text = str(sentence).strip()
        if not text:
            continue
        if text[-1] not in ".!?":
            text = f"{text}."
        cleaned.append(text)
    return " ".join(cleaned[:4])


def _sentence_count(text: str) -> int:
    return len([part for part in str(text or "").replace("!", ".").replace("?", ".").split(".") if part.strip()])


BLOCKER_LABELS = {
    "quote_stale": "quote is stale",
    "quote_type_penalized": "quote type is not live or actionable",
    "spread_not_acceptable": "option spread is above the maximum",
    "volume_below_minimum": "option volume is below the minimum",
    "liquidity_grade_below_b": "option liquidity grade is below B",
    "historical_win_rate_below_52": "historical win rate is below 52%",
    "historical_confidence_not_satisfied": "historical confidence is not satisfied",
    "chart_signal_not_confirmed": "chart signal is not confirmed",
    "chart_grade_below_trade_candidate": "chart signal is below TRADE_CANDIDATE",
    "chart_side_not_aligned": "chart side is not aligned",
    "contract_type_does_not_match_side": "contract type does not match the chart direction",
    "options_sentiment_not_confirming_or_neutral": "options sentiment is not confirming or neutral",
    "ai_gate_unavailable": "AI Gate is unavailable",
    "ai_gate_request_failed": "AI Gate request failed",
    "ai_gate_disabled": "AI Gate is disabled",
    "openai_api_key_missing": "OpenAI API key is missing on the backend",
    "setup_data_unavailable": "setup data is unavailable",
    "underlying_price_mismatch": "live underlying price differs from chart data",
    "underlying_price_unavailable": "live underlying price is unavailable",
}


def _humanize_blocker(blocker: Any) -> str:
    key = str(blocker or "").strip().lower()
    if not key:
        return ""
    return BLOCKER_LABELS.get(key, key.replace("_", " "))


def _sanitize_summary(summary: str) -> str:
    text = str(summary or "")
    for raw, label in BLOCKER_LABELS.items():
        text = text.replace(raw, label)
    return text


def _sentiment_ok(side: str, sentiment: dict[str, Any]) -> bool:
    bias = str(sentiment.get("bias") or "").upper()
    if side == "LONG":
        return bias in {"BULLISH", "NEUTRAL"}
    if side == "SHORT":
        return bias in {"BEARISH", "EXTREME_PUT_HEAVY", "NEUTRAL"}
    return False


def _build_rejection_explanation(payload: dict[str, Any], blockers: list[str]) -> str:
    scan = payload.get("scan") or {}
    contract = payload.get("contract") or {}
    sentiment = payload.get("options_sentiment") or {}
    backtest = payload.get("backtest") or {}
    side = str(payload.get("side") or scan.get("side") or "").upper() or "UNKNOWN"
    symbol = payload.get("symbol") or scan.get("symbol") or contract.get("symbol") or "the symbol"
    sample_confidence = backtest.get("sample_confidence") or _sample_confidence(backtest.get("occurrences"))
    historical_edge = backtest.get("historical_edge") or _historical_edge(backtest.get("win_rate_pct"))
    readable_blockers = ", ".join(item for item in (_humanize_blocker(blocker) for blocker in blockers) if item)

    sentences = [
        f"Do not proceed on {symbol} {side} because the required validation gates failed: {readable_blockers or 'required gate did not pass'}",
        (
            f"The chart side is {scan.get('side', 'UNKNOWN')} with grade {scan.get('grade', 'UNKNOWN')} "
            f"and score {scan.get('score', 'UNKNOWN')}/{scan.get('max_score', 'UNKNOWN')}, so it must be "
            "TRADE_CANDIDATE or HIGH_CONVICTION and aligned with the contract direction"
        ),
        (
            f"The contract has liquidity grade {contract.get('liquidity_grade', 'UNKNOWN')}, "
            f"risk grade {contract.get('risk_grade', 'UNKNOWN')}, spread "
            f"{contract.get('spread_percentage', 'UNKNOWN')}%, quote type {contract.get('quote_type', 'UNKNOWN')}, "
            f"quote stale={contract.get('quote_stale', 'UNKNOWN')}, and volume {contract.get('volume', 'UNKNOWN')}"
        ),
        (
            f"The historical setup has win rate {backtest.get('win_rate_pct', 'UNKNOWN')}% over "
            f"{backtest.get('occurrences', 'UNKNOWN')} sessions, sample confidence {sample_confidence}, "
            f"and historical edge {historical_edge}"
        ),
        (
            f"Options sentiment is {sentiment.get('bias', 'UNKNOWN')} with put/call ratio "
            f"{sentiment.get('put_call_ratio', 'UNKNOWN')}, which must confirm the direction or be neutral before a recommendation is shown"
        ),
    ]
    if "underlying_price_mismatch" in blockers:
        underlying = contract.get("underlying_price") or (payload.get("contract_context") or {}).get("underlying_price")
        sentences.insert(
            1,
            f"The live underlying quote is {underlying or 'UNKNOWN'}, so refresh chart data before considering the setup",
        )
    return _sentence_join(sentences)


def _deterministic_blockers(payload: dict[str, Any]) -> list[str]:
    scan = payload.get("scan") or {}
    contract = payload.get("contract") or {}
    context = payload.get("contract_context") or {}
    filters = context.get("filters") or {}
    sentiment = payload.get("options_sentiment") or {}
    backtest = payload.get("backtest") or {}
    side = str(payload.get("side") or scan.get("side") or "").upper()
    contract_type = str(contract.get("type") or "").upper()
    expected_type = "CALL" if side == "LONG" else ("PUT" if side == "SHORT" else "")

    blockers: list[str] = []
    if side not in {"LONG", "SHORT"}:
        blockers.append("side_is_not_directional")
    if expected_type and contract_type != expected_type:
        blockers.append("contract_type_does_not_match_side")
    if str(scan.get("grade") or "").upper() not in CONFIRMED_SCAN_GRADES:
        blockers.append("chart_grade_below_trade_candidate")
    if str(scan.get("side") or "").upper() != side:
        blockers.append("chart_side_not_aligned")
    scan_price = _safe_float(scan.get("price"), 0.0)
    underlying_price = _safe_float(
        contract.get("underlying_price"),
        _safe_float(context.get("underlying_price"), 0.0),
    )
    max_mismatch_pct = _safe_float(
        config_manager.get("options", "max_underlying_mismatch_pct", default=1.0),
        1.0,
    )
    if scan_price > 0 and underlying_price > 0:
        mismatch_pct = abs(scan_price - underlying_price) / underlying_price * 100.0
        if mismatch_pct > max_mismatch_pct:
            blockers.append("underlying_price_mismatch")
    elif underlying_price <= 0:
        blockers.append("underlying_price_unavailable")
    if not bool(contract.get("chart_signal_confirmed")):
        blockers.append("chart_signal_not_confirmed")
    if not _sentiment_ok(side, sentiment):
        blockers.append("options_sentiment_not_confirming_or_neutral")
    if str(contract.get("liquidity_grade") or "").upper() not in {"A", "B"}:
        blockers.append("liquidity_grade_below_b")
    if bool(contract.get("quote_stale", True)):
        blockers.append("quote_stale")
    quote_type = str(contract.get("quote_type") or context.get("quote_type") or "").upper()
    if quote_type in BAD_QUOTE_TYPES:
        blockers.append("quote_type_penalized")
    max_spread = _safe_float(
        contract.get("recommended_max_spread_pct"),
        _safe_float(context.get("recommended_max_spread_pct"), 5.0),
    )
    spread = contract.get("spread_percentage")
    if spread is None or _safe_float(spread, 999.0) > max_spread:
        blockers.append("spread_not_acceptable")
    minimum_volume = _safe_int(
        contract.get("minimum_volume"),
        _safe_int(context.get("minimum_volume"), _safe_int(filters.get("min_volume"), 100)),
    )
    if _safe_int(contract.get("volume"), -1) < minimum_volume:
        blockers.append("volume_below_minimum")
    win_rate = backtest.get("win_rate_pct")
    if win_rate is None:
        blockers.append("historical_win_rate_unavailable")
    elif _safe_float(win_rate, 0.0) < 52.0:
        blockers.append("historical_win_rate_below_52")
    if backtest and not bool(backtest.get("confidence_ok", _historical_confidence_ok(backtest))):
        blockers.append("historical_confidence_not_satisfied")
    if not bool(contract.get("recommendation_eligible")):
        blockers.extend(str(item) for item in contract.get("recommendation_blockers") or [])

    return sorted(set(blockers))


def _response_schema() -> dict[str, Any]:
    return {
        "type": "object",
        "additionalProperties": False,
        "properties": {
            "decision": {"type": "string", "enum": ["PROCEED", "DO_NOT_PROCEED"]},
            "confidence": {"type": "string", "enum": ["LOW", "MEDIUM", "HIGH"]},
            "summary": {"type": "string"},
            "blocking_factors": {"type": "array", "items": {"type": "string"}},
        },
        "required": ["decision", "confidence", "summary", "blocking_factors"],
    }


def _extract_output_text(response_payload: dict[str, Any]) -> str:
    if isinstance(response_payload.get("output_text"), str):
        return response_payload["output_text"]

    for item in response_payload.get("output") or []:
        for content in item.get("content") or []:
            if isinstance(content.get("text"), str):
                return content["text"]
    return ""


def _do_not_proceed(payload: dict[str, Any], blockers: list[str], *, source: str, warning: str | None = None) -> dict[str, Any]:
    summary = _build_rejection_explanation(payload, blockers)
    result = {
        "decision": "DO_NOT_PROCEED",
        "confidence": "HIGH" if blockers else "LOW",
        "summary": summary,
        "blocking_factors": blockers,
        "source": source,
        "warning": warning,
        "preprompt_version": PREPROMPT_VERSION,
        "checked_at": datetime.now(timezone.utc).isoformat(),
    }
    return _attach_trade_explanation(result, payload, final_decision="NO_TRADE")


def _attach_trade_explanation(
    result: dict[str, Any],
    payload: dict[str, Any],
    *,
    final_decision: str,
) -> dict[str, Any]:
    try:
        gate_context = {
            **result,
            "final_decision": final_decision,
            "side": payload.get("side") or (payload.get("scan") or {}).get("side"),
        }
        result["trade_explanation"] = build_trade_explanation(
            payload.get("contract"),
            payload.get("scan"),
            payload.get("indicators") or (payload.get("scan") or {}).get("indicators") or {},
            payload.get("contract_context") or {},
            payload.get("options_sentiment") or {},
            payload.get("backtest") or {},
            gate_context,
        )
    except Exception as exc:
        result["trade_explanation_warning"] = f"Trade explanation unavailable: {exc}"
    return result


def validate_trade_gate(payload: dict[str, Any], *, preprompt: str | None = None) -> dict[str, Any]:
    payload = payload or {}
    blockers = _deterministic_blockers(payload)
    if blockers:
        return _do_not_proceed(payload, blockers, source="deterministic_precheck")

    ai_cfg = config_manager.get("ai", default={}) or {}
    if not bool(ai_cfg.get("enabled", True)):
        return _do_not_proceed(
            payload,
            ["ai_gate_disabled"],
            source="configuration",
            warning="AI trade gate is disabled in config",
        )

    api_key = os.getenv("OPENAI_API_KEY", "").strip()
    if not api_key:
        return _do_not_proceed(
            payload,
            ["openai_api_key_missing"],
            source="configuration",
            warning="OPENAI_API_KEY is not configured",
        )

    model = os.getenv("OPENAI_MODEL", str(ai_cfg.get("model", "gpt-5.6"))).strip() or "gpt-5.6"
    timeout = int(ai_cfg.get("timeout_seconds", 20))
    request_payload = {
        "model": model,
        "input": [
            {"role": "system", "content": preprompt or TRADE_GATE_PREPROMPT},
            {
                "role": "user",
                "content": json.dumps(
                    {
                        "instruction": "Decide PROCEED or DO_NOT_PROCEED for this options setup using only these facts.",
                        "candidate": payload,
                    },
                    sort_keys=True,
                ),
            },
        ],
        "text": {
            "format": {
                "type": "json_schema",
                "name": "trade_gate_decision",
                "strict": True,
                "schema": _response_schema(),
            }
        },
    }

    def _post_openai() -> requests.Response:
        res = requests.post(
            OPENAI_RESPONSES_URL,
            headers={
                "Authorization": f"Bearer {api_key}",
                "Content-Type": "application/json",
            },
            json=request_payload,
            timeout=timeout,
        )
        res.raise_for_status()
        return res

    try:
        response = call_with_rate_limit(
            "openai",
            str(payload.get("symbol") or ""),
            "responses",
            _post_openai,
        )
        response_payload = response.json()
        parsed = json.loads(_extract_output_text(response_payload))
    except Exception as exc:
        return _do_not_proceed(
            payload,
            ["ai_gate_unavailable"],
            source="openai",
            warning=f"AI trade gate unavailable: {exc}",
        )

    decision = str(parsed.get("decision") or "DO_NOT_PROCEED").upper()
    if decision != "PROCEED":
        factors = list(parsed.get("blocking_factors") or ["ai_gate_rejected"])
        summary = _sanitize_summary(str(parsed.get("summary") or "").strip())
        if _sentence_count(summary) < 3 or _sentence_count(summary) > 4:
            summary = _build_rejection_explanation(payload, factors)
        result = {
            "decision": "DO_NOT_PROCEED",
            "confidence": parsed.get("confidence") or "MEDIUM",
            "summary": summary,
            "blocking_factors": factors,
            "source": "openai",
            "model": model,
            "preprompt_version": PREPROMPT_VERSION,
            "checked_at": datetime.now(timezone.utc).isoformat(),
        }
        return _attach_trade_explanation(result, payload, final_decision="NO_TRADE")

    result = {
        "decision": "PROCEED",
        "confidence": parsed.get("confidence") or "MEDIUM",
        "summary": _sanitize_summary(str(parsed.get("summary") or "AI gate agrees with the supplied setup facts.").strip()),
        "blocking_factors": [],
        "source": "openai",
        "model": model,
        "preprompt_version": PREPROMPT_VERSION,
        "checked_at": datetime.now(timezone.utc).isoformat(),
    }
    return _attach_trade_explanation(result, payload, final_decision="TRADE_CANDIDATE")


SIGNAL_RESPONSE_SCHEMA = {
    "type": "object",
    "additionalProperties": False,
    "properties": {
        "decision": {"type": "string", "enum": ["APPROVE_SIGNAL", "WAIT_FOR_CONFIRMATION", "WAIT_FOR_RETEST", "REJECT_EXTENDED", "REJECT_LOW_VOLUME", "REJECT_BAD_RISK_REWARD", "REJECT_OPTION_QUALITY", "REJECT_DATA_QUALITY", "INVALIDATED"]},
        "signal_status": {"type": "string"},
        "thesis": {"type": "string"},
        "entry": {"type": "string"},
        "maximum_chase_price": {"type": "string"},
        "invalidation": {"type": "string"},
        "targets": {"type": "array", "items": {"type": "string"}},
        "option_contract": {"type": "string"},
        "maximum_option_price": {"type": "string"},
        "why": {"type": "array", "items": {"type": "string"}},
        "conflicts": {"type": "array", "items": {"type": "string"}},
        "expiration_time": {"type": "string"},
        "next_action": {"type": "string"},
    },
    "required": ["decision", "signal_status", "thesis", "entry", "maximum_chase_price", "invalidation", "targets", "option_contract", "maximum_option_price", "why", "conflicts", "expiration_time", "next_action"],
}


def validate_signal(payload: dict[str, Any], *, prompt: str, prompt_version: str) -> dict[str, Any]:
    """Validate a deterministic signal with a strict signal-shaped Responses result."""
    ai_cfg = config_manager.get("ai", default={}) or {}
    api_key = os.getenv("OPENAI_API_KEY", "").strip()
    if not api_key:
        return {"decision": "REJECT_DATA_QUALITY", "status": "UNAVAILABLE", "source": "configuration", "prompt_version": prompt_version, "reason": "OPENAI_API_KEY is not configured"}
    model = os.getenv("OPENAI_MODEL", str(ai_cfg.get("model", "gpt-5.6"))).strip() or "gpt-5.6"
    timeout = int(ai_cfg.get("timeout_seconds", 20) or 20)
    request_payload = {
        "model": model,
        "input": [
            {"role": "system", "content": prompt},
            {"role": "user", "content": json.dumps({"instruction": "Validate the supplied deterministic signal. Do not change any supplied price or create a setup.", "signal": payload}, sort_keys=True)},
        ],
        "text": {"format": {"type": "json_schema", "name": "active_signal_validation", "strict": True, "schema": SIGNAL_RESPONSE_SCHEMA}},
    }

    def _post_openai() -> requests.Response:
        response = requests.post(OPENAI_RESPONSES_URL, headers={"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"}, json=request_payload, timeout=timeout)
        response.raise_for_status()
        return response

    try:
        response = call_with_rate_limit("openai", str(payload.get("ticker") or ""), "responses/signal-validation", _post_openai)
        parsed = json.loads(_extract_output_text(response.json()))
        decision = str(parsed.get("decision") or "REJECT_DATA_QUALITY").upper()
        if decision not in set(SIGNAL_RESPONSE_SCHEMA["properties"]["decision"]["enum"]):
            return {"decision": "REJECT_DATA_QUALITY", "status": "REJECTED", "source": "openai", "prompt_version": prompt_version, "reason": "AI returned an unsupported decision"}
        return {**parsed, "decision": decision, "status": "VALIDATED" if decision == "APPROVE_SIGNAL" else "REJECTED", "source": "openai_responses", "model": model, "prompt_version": prompt_version, "checked_at": datetime.now(timezone.utc).isoformat()}
    except Exception as exc:
        return {"decision": "REJECT_DATA_QUALITY", "status": "UNAVAILABLE", "source": "openai_responses", "prompt_version": prompt_version, "reason": f"AI signal validation failed: {exc}", "checked_at": datetime.now(timezone.utc).isoformat()}
