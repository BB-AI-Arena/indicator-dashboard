from __future__ import annotations

from app.morning_routine import adaptive_gap_classification, build_opening_scenarios, classify_catalyst, same_time_premarket_rvol


def test_premarket_rvol_uses_same_elapsed_time_samples():
    assert same_time_premarket_rvol(600, 30, [300, 400]) == 1.71


def test_premarket_rvol_does_not_treat_missing_samples_as_zero():
    assert same_time_premarket_rvol(600, 30, []) is None
    assert same_time_premarket_rvol(600, 0, [300]) is None


def test_gap_classification_adapts_to_ticker_volatility():
    low_vol = adaptive_gap_classification(1.0, 0.8)
    high_vol = adaptive_gap_classification(1.0, 4.0)
    assert low_vol["classification"] == "TRADEABLE GAP"
    assert high_vol["classification"] == "IMMATERIAL GAP"


def test_catalyst_strength_is_separate_from_price_confirmation():
    payload = {
        "summary": {
            "most_relevant_event": {
                "event_category": "EARNINGS",
                "headline": "Quarterly results",
                "publication_timestamp": "2026-07-11T08:00:00-04:00",
                "reaction": {"return_15m_pct": -2.0},
                "confidence": "HIGH",
            }
        }
    }
    result = classify_catalyst(payload, direction="LONG")
    assert result["strength"] == "STRONG"
    assert result["price_confirmation"] == "CONFLICTS"


def test_opening_scenarios_include_breakout_pullback_and_failure():
    scenarios = build_opening_scenarios("LONG", {"premarket_high": 181.4, "support": 179.8, "resistance": 184.0, "vwap": 180.2})
    names = {row["name"] for row in scenarios}
    assert {"BREAKOUT AND HOLD", "PULLBACK AND HOLD", "FAILED BREAKOUT"} <= names
    assert "separately qualified" in scenarios[-1]["action"]


def test_short_plan_has_inverse_failure_scenario():
    scenarios = build_opening_scenarios("SHORT", {"premarket_low": 175.0, "support": 173.0, "resistance": 177.0, "vwap": 176.0})
    assert {row["name"] for row in scenarios} == {"BREAKDOWN AND HOLD", "BOUNCE INTO RESISTANCE", "FAILED BREAKDOWN"}
