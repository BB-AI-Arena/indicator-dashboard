from __future__ import annotations

import hashlib
import json
import os
import re
from datetime import datetime, timezone
from typing import Any

import requests
from sqlalchemy.orm import Session

from .config import config_manager
from .historical_patterns import build_historical_setup_match
from .market_session import get_market_session
from .models import AdvisoryCache, AdvisorySetting, TickerProfile
from .providers.rate_limiter import call_with_rate_limit
from .ticker_profiles import refresh_ticker_profile, serialize_ticker_profile


OPENAI_RESPONSES_URL = "https://api.openai.com/v1/responses"
ADVISORY_PROMPT_VERSION = "trade-advisory-v1"
ADVISORY_ANALYSIS_VERSION = "advisory-analysis-v1"
ALLOWED_DECISIONS = {"ENTER", "WAIT", "HOLD", "REDUCE", "CLOSE", "ROLL", "AVOID", "DATA REFRESH REQUIRED"}
ALLOWED_CONVICTIONS = {"Low", "Moderate", "High"}
PROHIBITED_PATTERNS = [
    r"\bguaranteed\b",
    r"\bcannot lose\b",
    r"\bsure thing\b",
    r"\bsmart money definitely\b",
    r"\bwill double\b",
    r"\bnot financial advice\b",
    r"\bnot a financial advisor\b",
    r"\bconsult a professional\b",
    r"\bdo your own research\b",
]


ADVISORY_SYSTEM_PROMPT = """
You are a quantitative options-trading decision analyst. Your job is to evaluate the supplied market data, historical setup statistics, price structure, volume, options positioning, news, Greeks, volatility, liquidity, and portfolio risk, then provide direct and specific decision support.

You are not permitted to fabricate data, prices, probabilities, Greeks, headlines, or market conditions.

Do not give generic disclaimers, motivational language, or vague risk-management advice.

Do not begin or end with phrases such as:
- I am not a financial advisor
- This is not financial advice
- Consult a professional
- Do your own research

The user already understands that trading involves risk.

Your responsibility is to:
- identify the highest-quality interpretation supported by the data
- state what the evidence favors
- state what conflicts with the thesis
- provide exact confirmation and invalidation conditions
- compare available contracts
- explain the likely impact of delta, gamma, theta, vega, and IV
- identify whether the setup is early, confirmed, extended, weakening, or invalidated
- identify whether the trade has positive expected value
- state when no acceptable trade exists
- tell the user the hard truth when a trade is structurally weak

Never promise profits or claim that a loss cannot occur.

Never turn a weak or incomplete setup into a recommendation merely because the user requested a trade.

Use probabilities only when they are supplied by the deterministic analytics engine.

Do not create probabilities from intuition.

Prioritize:
1. Data quality
2. Setup validity
3. Entry timing
4. Liquidity
5. Expected value
6. Risk/reward
7. Position sizing
8. Contract quality
9. Market-regime alignment
10. News and options confirmation

For every current position or proposed play, return the required JSON fields for DECISION, CONVICTION, THESIS, WHY, CONFLICTS, ENTRY, CONFIRMATION, INVALIDATION, TARGETS, CONTRACT, RISK, HISTORICAL MATCH, HARD TRUTH, and NEXT ACTION.

The decision must be one of ENTER, WAIT, HOLD, REDUCE, CLOSE, ROLL, AVOID, or DATA REFRESH REQUIRED. Use ENTER only when the deterministic package has no hard gates and the supplied setup has valid confirmation, liquidity, expected value, and data quality.

Use exact supplied prices for entry, confirmation, invalidation, targets, option ranges, and risk. If a price or contract is unavailable, say it is unavailable rather than estimating it.

Use only probabilities supplied under historical_setup_statistics. Cite sample size, target-before-invalidation rate, confidence interval, MFE, MAE, and expected value when available.

The hard truth must be the single most important weakness or constraint shown by the data, not motivational commentary.

Be concise, evidence-based, and decisive.
""".strip()


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _json_load(raw: str | None, fallback: Any) -> Any:
    if not raw:
        return fallback
    try:
        return json.loads(raw)
    except Exception:
        return fallback


def _hash_payload(payload: Any) -> str:
    data = json.dumps(payload, sort_keys=True, default=str)
    return hashlib.sha256(data.encode("utf-8")).hexdigest()


def _settings_from_config() -> dict[str, Any]:
    cfg = config_manager.get("advisory", default={}) or {}
    return {
        "enabled": bool(cfg.get("enabled", True)),
        "deterministic_only": bool(cfg.get("deterministic_only", False)),
        "model": os.getenv("OPENAI_ADVISORY_MODEL", str(cfg.get("model", "gpt-5.6"))).strip() or "gpt-5.6",
        "fallback_model": os.getenv("OPENAI_ADVISORY_FALLBACK_MODEL", str(cfg.get("fallback_model", "gpt-5.4-mini"))).strip() or "gpt-5.4-mini",
        "reasoning_effort": os.getenv("OPENAI_ADVISORY_REASONING_EFFORT", str(cfg.get("reasoning_effort", "high"))).strip() or "high",
        "advisory_mode": os.getenv("OPENAI_ADVISORY_MODE", str(cfg.get("advisory_mode", "standard"))).strip() or "standard",
        "max_output_tokens": int(cfg.get("max_output_tokens", 1400) or 1400),
        "timeout_seconds": int(cfg.get("timeout_seconds", 45) or 45),
        "maximum_calls_per_hour": int(cfg.get("maximum_calls_per_hour", 20) or 20),
        "cache_duration_seconds": int(cfg.get("cache_duration_seconds", 1800) or 1800),
        "maximum_advisory_cost": float(cfg.get("maximum_advisory_cost", 5.0) or 5.0),
        "prompt_version": str(cfg.get("prompt_version", ADVISORY_PROMPT_VERSION) or ADVISORY_PROMPT_VERSION),
        "response_schema_version": str(cfg.get("response_schema_version", "advisory-response-v1") or "advisory-response-v1"),
    }


def get_advisory_settings(db: Session | None = None) -> dict[str, Any]:
    settings = _settings_from_config()
    if db is None:
        return settings
    for row in db.query(AdvisorySetting).all():
        value = _json_load(row.value, row.value)
        settings[row.key] = value
    return settings


def update_advisory_settings(db: Session, payload: dict[str, Any], username: str | None = None) -> dict[str, Any]:
    allowed = set(_settings_from_config().keys())
    now = _now_iso()
    for key, value in (payload or {}).items():
        if key not in allowed:
            continue
        row = db.query(AdvisorySetting).filter(AdvisorySetting.key == key).first()
        serialized = json.dumps(value)
        if row:
            row.value = serialized
            row.updated_at = now
            row.updated_by = username
        else:
            db.add(AdvisorySetting(key=key, value=serialized, updated_at=now, updated_by=username))
    db.commit()
    return get_advisory_settings(db)


def _response_schema() -> dict[str, Any]:
    text_value = {"type": "string"}
    return {
        "type": "object",
        "additionalProperties": False,
        "properties": {
            "decision": {"type": "string", "enum": sorted(ALLOWED_DECISIONS)},
            "conviction": {"type": "string", "enum": sorted(ALLOWED_CONVICTIONS)},
            "thesis": text_value,
            "why": {"type": "array", "items": text_value},
            "conflicts": {"type": "array", "items": text_value},
            "entry": {
                "type": "object",
                "additionalProperties": False,
                "properties": {"underlying_trigger": {"type": ["number", "null"]}, "option_price_range": text_value, "text": text_value},
                "required": ["underlying_trigger", "option_price_range", "text"],
            },
            "confirmation": text_value,
            "invalidation": {
                "type": "object",
                "additionalProperties": False,
                "properties": {"price": {"type": ["number", "null"]}, "text": text_value},
                "required": ["price", "text"],
            },
            "targets": {"type": "array", "items": text_value},
            "contract": text_value,
            "risk": text_value,
            "historical_match": {
                "type": "object",
                "additionalProperties": False,
                "properties": {
                    "sample_size": {"type": "integer"},
                    "target_before_invalidation_rate": {"type": ["number", "null"]},
                    "confidence_interval": text_value,
                    "expected_value": text_value,
                    "text": text_value,
                },
                "required": ["sample_size", "target_before_invalidation_rate", "confidence_interval", "expected_value", "text"],
            },
            "hard_truth": text_value,
            "next_action": text_value,
        },
        "required": [
            "decision",
            "conviction",
            "thesis",
            "why",
            "conflicts",
            "entry",
            "confirmation",
            "invalidation",
            "targets",
            "contract",
            "risk",
            "historical_match",
            "hard_truth",
            "next_action",
        ],
    }


def _extract_output_text(response_payload: dict[str, Any]) -> str:
    if isinstance(response_payload.get("output_text"), str):
        return response_payload["output_text"]
    for item in response_payload.get("output") or []:
        for content in item.get("content") or []:
            if isinstance(content.get("text"), str):
                return content["text"]
    return ""


def _deterministic_fallback(package: dict[str, Any], reason: str) -> dict[str, Any]:
    setup = package.get("historical_setup_statistics") or {}
    contract = package.get("selected_contracts") or {}
    status = setup.get("setup_state") or "DATA INSUFFICIENT"
    decision = "WAIT"
    if status == "DATA INSUFFICIENT":
        decision = "DATA REFRESH REQUIRED"
    if contract.get("status") == "NO_ACCEPTABLE_CONTRACT":
        decision = "AVOID"
    if (setup.get("same_symbol") or {}).get("confidence") == "INSUFFICIENT" and (setup.get("cross_symbol") or {}).get("confidence") == "INSUFFICIENT":
        decision = "DATA REFRESH REQUIRED"
    return {
        "decision": decision,
        "conviction": "Low",
        "thesis": "Deterministic analytics do not provide enough validated evidence for a model-assisted recommendation.",
        "why": ["The app is using stored statistics and hard gates only."],
        "conflicts": [reason],
        "entry": {"underlying_trigger": None, "option_price_range": "Unavailable", "text": "No model entry guidance was accepted."},
        "confirmation": str((setup.get("confirmation_condition") or {}).get("condition") or "Wait for deterministic confirmation."),
        "invalidation": {"price": (setup.get("invalidation_condition") or {}).get("price"), "text": str((setup.get("invalidation_condition") or {}).get("condition") or "No invalidation available.")},
        "targets": [],
        "contract": str(contract.get("message") or "No validated contract recommendation."),
        "risk": "Use deterministic risk controls only until advisory output validates.",
        "historical_match": {
            "sample_size": int((setup.get("same_symbol") or {}).get("examples") or 0),
            "target_before_invalidation_rate": (setup.get("same_symbol") or {}).get("raw_success_rate"),
            "confidence_interval": json.dumps((setup.get("same_symbol") or {}).get("confidence_interval") or {}),
            "expected_value": str((setup.get("same_symbol") or {}).get("expected_value_pct")),
            "text": str((setup.get("estimated_probability") or {}).get("language") or "No validated probability."),
        },
        "hard_truth": reason,
        "next_action": "Refresh or continue backfilling data, then re-evaluate the setup after a completed 15-minute candle.",
    }


def build_advisory_package(
    db: Session,
    symbol: str,
    *,
    side: str | None = None,
    candidate_id: str | None = None,
    supplied: dict[str, Any] | None = None,
) -> dict[str, Any]:
    normalized = str(symbol or "").strip().upper()
    profile = db.query(TickerProfile).filter(TickerProfile.symbol == normalized).first()
    if profile is None:
        profile = refresh_ticker_profile(db, normalized, source="advisory_package")
        db.commit()
    setup = build_historical_setup_match(normalized, side=side, include_contracts=True, persist=True)
    profile = refresh_ticker_profile(db, normalized, source="advisory_package")
    db.commit()
    return {
        "package_version": "structured-advisory-package-v1",
        "candidate_id": candidate_id or f"{normalized}:{side or setup.get('direction') or 'UNKNOWN'}",
        "current_market_session": get_market_session(),
        "quote_freshness": (supplied or {}).get("quote_freshness") or "provided by deterministic market-data layer",
        "ticker_profile_summary": serialize_ticker_profile(profile),
        "current_15_minute_setup": setup.get("current_feature_vector"),
        "daily_trend": (profile and (serialize_ticker_profile(profile).get("stats") or {}).get("price_behavior")) or {},
        "support_resistance": {
            "confirmation": setup.get("confirmation_condition"),
            "invalidation": setup.get("invalidation_condition"),
        },
        "fibonacci_state": ((serialize_ticker_profile(profile).get("stats") or {}).get("fibonacci_behavior") if profile else {}),
        "volume_and_money_flow_state": (supplied or {}).get("money_flow") or {},
        "relative_strength": (setup.get("current_feature_vector") or {}).get("vector", {}),
        "market_regime": "unavailable",
        "sector_regime": "unavailable",
        "options_chain_summary": setup.get("contract_selection", {}).get("reviewed", []),
        "selected_contracts": setup.get("contract_selection") or {},
        "greeks": "included per contract when provider supplied them",
        "iv_state": "included per contract when provider supplied it",
        "put_call_positioning": (setup.get("current_confirmation") or {}).get("options_positioning"),
        "news_and_catalyst_summary": ((serialize_ticker_profile(profile).get("stats") or {}).get("news_history") if profile else {}),
        "social_intelligence_summary": ((serialize_ticker_profile(profile).get("stats") or {}).get("social_history") if profile else {}),
        "historical_setup_statistics": setup,
        "open_position_details": (supplied or {}).get("open_position") or {},
        "account_risk_limits": (supplied or {}).get("account_risk_limits") or {},
        "conflicting_evidence": setup.get("current_feature_vector", {}).get("missing_or_unconfirmed", []),
        "missing_data": _missing_data_from_package(setup),
        "deterministic_recommendation": {
            "status": setup.get("setup_state"),
            "warnings": setup.get("warnings") or [],
            "hard_gates": _hard_gates(setup),
        },
    }


def _missing_data_from_package(setup: dict[str, Any]) -> list[str]:
    missing = list((setup.get("current_feature_vector") or {}).get("features", {}).get("unavailable_features") or [])
    if not setup.get("matches"):
        missing.append("historical_matches")
    if (setup.get("contract_selection") or {}).get("status") not in {"OK", "NO_ACCEPTABLE_CONTRACT"}:
        missing.append("option_chain_review")
    return sorted(set(str(item) for item in missing if item))


def _hard_gates(setup: dict[str, Any]) -> list[str]:
    gates = []
    same = setup.get("same_symbol") or {}
    cross = setup.get("cross_symbol") or {}
    contract = setup.get("contract_selection") or {}
    if same.get("confidence") == "INSUFFICIENT" and cross.get("confidence") == "INSUFFICIENT":
        gates.append("insufficient_historical_sample")
    if contract.get("status") == "NO_ACCEPTABLE_CONTRACT":
        gates.append("no_acceptable_contract")
    if (same.get("expected_value_pct") is not None and same.get("expected_value_pct") <= 0) and (cross.get("expected_value_pct") is not None and cross.get("expected_value_pct") <= 0):
        gates.append("non_positive_expected_value")
    return gates


def _contract_symbols(package: dict[str, Any]) -> set[str]:
    symbols = set()
    selected = package.get("selected_contracts") or {}
    for key in ["best_contract", "safer_contract", "higher_leverage_contract"]:
        contract = selected.get(key)
        if isinstance(contract, dict) and contract.get("contract"):
            symbols.add(str(contract["contract"]))
    for row in selected.get("reviewed") or []:
        if row.get("contract"):
            symbols.add(str(row["contract"]))
    return symbols


def validate_advisory_output(output: dict[str, Any], package: dict[str, Any]) -> list[str]:
    errors: list[str] = []
    if output.get("decision") not in ALLOWED_DECISIONS:
        errors.append("decision is not allowed")
    if output.get("conviction") not in ALLOWED_CONVICTIONS:
        errors.append("conviction is not allowed")
    text = json.dumps(output).lower()
    for pattern in PROHIBITED_PATTERNS:
        if re.search(pattern, text):
            errors.append(f"prohibited language: {pattern}")
    hard_gates = set((package.get("deterministic_recommendation") or {}).get("hard_gates") or [])
    if hard_gates and output.get("decision") == "ENTER":
        errors.append("model attempted to enter despite deterministic hard gates")
    deterministic = package.get("historical_setup_statistics") or {}
    primary = deterministic.get("same_symbol") if (deterministic.get("same_symbol") or {}).get("examples") else deterministic.get("cross_symbol") or {}
    expected_rate = primary.get("raw_success_rate")
    actual_rate = (output.get("historical_match") or {}).get("target_before_invalidation_rate")
    if expected_rate is None and actual_rate is not None:
        errors.append("model supplied unsupported probability")
    if expected_rate is not None and actual_rate is not None and abs(float(expected_rate) - float(actual_rate)) > 0.0001:
        errors.append("model altered deterministic probability")
    contract_text = str(output.get("contract") or "")
    mentioned = [symbol for symbol in _contract_symbols(package) if symbol and symbol in contract_text]
    if contract_text and "contract" in contract_text.lower() and _contract_symbols(package) and not mentioned and "no validated contract" not in contract_text.lower():
        errors.append("model referenced a contract outside supplied chain")
    return errors


def _request_payload(settings: dict[str, Any], package: dict[str, Any], model: str) -> dict[str, Any]:
    reasoning: dict[str, Any] = {"effort": settings.get("reasoning_effort", "high")}
    mode = str(settings.get("advisory_mode") or "standard")
    if mode and mode != "standard":
        reasoning["mode"] = mode
    return {
        "model": model,
        "reasoning": reasoning,
        "max_output_tokens": int(settings.get("max_output_tokens", 1400) or 1400),
        "input": [
            {"role": "system", "content": ADVISORY_SYSTEM_PROMPT},
            {
                "role": "user",
                "content": json.dumps(
                    {
                        "instruction": "Return structured trade advice using only this package. Do not calculate new probabilities.",
                        "package": package,
                    },
                    sort_keys=True,
                ),
            },
        ],
        "text": {
            "format": {
                "type": "json_schema",
                "name": "trade_advisory_response",
                "strict": True,
                "schema": _response_schema(),
            }
        },
    }


def generate_advisory(
    db: Session,
    package: dict[str, Any],
    *,
    force_refresh: bool = False,
) -> dict[str, Any]:
    settings = get_advisory_settings(db)
    input_hash = _hash_payload(package)
    model = str(settings.get("model") or "gpt-5.6")
    cache_key = _hash_payload(
        {
            "input_hash": input_hash,
            "model": model,
            "prompt_version": settings.get("prompt_version"),
            "analysis_version": ADVISORY_ANALYSIS_VERSION,
        }
    )
    cached = db.query(AdvisoryCache).filter(AdvisoryCache.cache_key == cache_key).first()
    if cached and not force_refresh:
        payload = _json_load(cached.advisory_json, {})
        payload["cached"] = True
        if isinstance(payload.get("metadata"), dict):
            payload["metadata"]["cached"] = True
        return payload

    if not bool(settings.get("enabled", True)) or bool(settings.get("deterministic_only", False)):
        advice = _deterministic_fallback(package, "GPT advisory is disabled or deterministic-only mode is enabled.")
        return _store_advisory_cache(db, cache_key, package, advice, settings, input_hash, deterministic=True)

    api_key = os.getenv("OPENAI_API_KEY", "").strip()
    if not api_key:
        advice = _deterministic_fallback(package, "OPENAI_API_KEY is missing on the backend.")
        return _store_advisory_cache(db, cache_key, package, advice, settings, input_hash, deterministic=True)

    fallback_model = str(settings.get("fallback_model") or "").strip()
    models_to_try = [model]
    if fallback_model and fallback_model != model:
        models_to_try.append(fallback_model)
    else:
        models_to_try.append(model)

    errors: list[str] = []
    for attempt, use_model in enumerate(models_to_try, start=1):
        request_payload = _request_payload(settings, package, use_model)

        def _post() -> requests.Response:
            response = requests.post(
                OPENAI_RESPONSES_URL,
                headers={"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"},
                json=request_payload,
                timeout=int(settings.get("timeout_seconds", 45) or 45),
            )
            response.raise_for_status()
            return response

        try:
            response = call_with_rate_limit("openai", str(package.get("ticker_profile_summary", {}).get("symbol") or ""), "responses/advisory", _post)
            parsed = json.loads(_extract_output_text(response.json()))
            validation_errors = validate_advisory_output(parsed, package)
            if validation_errors:
                errors.extend(validation_errors)
                continue
            return _store_advisory_cache(db, cache_key, package, parsed, settings, input_hash, deterministic=False, model_used=use_model)
        except Exception as exc:
            errors.append(f"attempt {attempt}: {exc}")

    advice = _deterministic_fallback(package, f"Model advisory failed validation or request: {'; '.join(errors[:4])}")
    return _store_advisory_cache(db, cache_key, package, advice, settings, input_hash, deterministic=True, validation_status="FALLBACK")


def _store_advisory_cache(
    db: Session,
    cache_key: str,
    package: dict[str, Any],
    advice: dict[str, Any],
    settings: dict[str, Any],
    input_hash: str,
    *,
    deterministic: bool,
    validation_status: str = "VALID",
    model_used: str | None = None,
) -> dict[str, Any]:
    symbol = str((package.get("ticker_profile_summary") or {}).get("symbol") or "").upper()
    payload = {
        "advice": advice,
        "metadata": {
            "generated_at": _now_iso(),
            "source_data_timestamp": package.get("historical_setup_statistics", {}).get("timestamp"),
            "prompt_version": settings.get("prompt_version"),
            "analysis_version": ADVISORY_ANALYSIS_VERSION,
            "response_schema_version": settings.get("response_schema_version"),
            "cached": False,
            "deterministic_fallback": deterministic,
            "fallback_model_configured": settings.get("fallback_model"),
            "validation_status": validation_status,
            "data_confidence_score": _data_confidence_score(package),
        },
    }
    payload["metadata"]["model_used"] = "deterministic" if deterministic else (model_used or settings.get("model"))
    existing = db.query(AdvisoryCache).filter(AdvisoryCache.cache_key == cache_key).first()
    if existing:
        existing.advisory_json = json.dumps(payload, sort_keys=True)
        existing.deterministic_fallback = deterministic
        existing.validation_status = validation_status
        existing.created_at = _now_iso()
        db.commit()
        return payload
    db.add(
        AdvisoryCache(
            cache_key=cache_key,
            symbol=symbol,
            candidate_id=str(package.get("candidate_id") or ""),
            setup_version=str((package.get("historical_setup_statistics") or {}).get("feature_version") or ""),
            market_data_version=str((package.get("historical_setup_statistics") or {}).get("timestamp") or ""),
            option_chain_version=str((package.get("selected_contracts") or {}).get("timestamp") or ""),
            news_version=str(((package.get("news_and_catalyst_summary") or {}).get("recent_events") or [{}])[0].get("publication_timestamp") if (package.get("news_and_catalyst_summary") or {}).get("recent_events") else ""),
            analysis_version=ADVISORY_ANALYSIS_VERSION,
            model=str(("deterministic" if deterministic else (model_used or settings.get("model"))) or ""),
            prompt_version=str(settings.get("prompt_version") or ADVISORY_PROMPT_VERSION),
            input_hash=input_hash,
            advisory_json=json.dumps(payload, sort_keys=True),
            deterministic_fallback=deterministic,
            validation_status=validation_status,
            created_at=_now_iso(),
        )
    )
    db.commit()
    return payload


def _data_confidence_score(package: dict[str, Any]) -> float:
    score = 0.0
    setup = package.get("historical_setup_statistics") or {}
    if (setup.get("same_symbol") or {}).get("confidence") in {"MODERATE", "HIGH", "STRONG"}:
        score += 35
    elif (setup.get("cross_symbol") or {}).get("confidence") in {"MODERATE", "HIGH", "STRONG"}:
        score += 20
    if (setup.get("contract_selection") or {}).get("status") == "OK":
        score += 25
    if not package.get("missing_data"):
        score += 20
    profile_summary = package.get("ticker_profile_summary") or {}
    if profile_summary.get("planning_ready") or profile_summary.get("live_ready"):
        score += 20
    return min(100.0, score)
