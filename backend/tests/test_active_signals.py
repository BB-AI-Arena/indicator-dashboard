from __future__ import annotations

import sys
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch
from types import SimpleNamespace

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from app.active_signals import ACTIVE_SIGNAL_STATES, _candidate_is_publishable, _chase_price, _setup_type, _state_for_candidate, _terminal_reason


def candidate(**overrides):
    base = {
        "ticker": "NVDA",
        "direction": "LONG",
        "setup_name": "VWAP reclaim continuation",
        "passes_hard_gates": True,
        "hard_gates": [],
        "status": "READY FOR LIVE ANALYSIS",
        "current_or_previous_session_price": 182.4,
        "entry_trigger": {"price": 181.8, "condition": "Completed 5-minute close above 181.80."},
        "invalidation": {"price": 180.95, "condition": "Completed 5-minute close below 180.95."},
        "targets": [{"price": 184.8}, {"price": 186.2}],
        "preferred_option_contract": {"contract": "NVDA 21-DTE 182.50 CALL", "status": "VALIDATED"},
        "maximum_acceptable_option_entry": 6.10,
        "evidence_groups": {
            "price_structure": {"score": 1},
            "vwap_control": {"score": 1},
            "volume_participation": {"score": 1},
        },
    }
    base.update(overrides)
    return base


class ActiveSignalTests(unittest.TestCase):
    def test_supported_setup_classifier_is_deterministic(self):
        self.assertEqual(_setup_type(candidate()), "VWAP RECLAIM LONG")
        self.assertEqual(_setup_type(candidate(direction="SHORT", setup_name="opening range breakdown")), "OPENING-RANGE BREAKDOWN")
        self.assertIsNone(_setup_type(candidate(setup_name="discretionary idea")))

    def test_primary_evidence_and_exact_levels_are_required(self):
        live_session = {"actionable_live_quotes": True}
        approved_ai = {"decision": "APPROVE_SIGNAL", "status": "VALIDATED"}
        with patch("app.active_signals._ai_validation", return_value=approved_ai):
            self.assertTrue(_candidate_is_publishable(candidate(), live_session)[0])
            ok, reasons = _candidate_is_publishable(candidate(entry_trigger={"condition": "missing price"}), live_session)
        self.assertFalse(ok)
        self.assertIn("exact_entry_missing", reasons)
        with patch("app.active_signals._ai_validation", return_value=approved_ai):
            self.assertFalse(_candidate_is_publishable(candidate(evidence_groups={"price_structure": {"score": 1}, "vwap_control": {"score": -1}, "volume_participation": {"score": 1}}), live_session)[0])

    def test_closed_session_cannot_publish_executable_signal(self):
        with patch("app.active_signals._ai_validation", return_value={"decision": "APPROVE_SIGNAL"}):
            ok, reasons = _candidate_is_publishable(candidate(), {"actionable_live_quotes": False})
        self.assertFalse(ok)
        self.assertIn("options_market_not_actionable", reasons)

    def test_signal_states_and_chase_threshold(self):
        self.assertTrue({"READY", "TRIGGERED", "ACTIVE", "WAITING FOR RETEST", "WAITING FOR CONFIRMATION"}.issubset(ACTIVE_SIGNAL_STATES))
        self.assertEqual(_state_for_candidate(candidate(status="WAITING")), "WAITING FOR CONFIRMATION")
        self.assertEqual(_state_for_candidate(candidate(setup_name="Fibonacci pullback bounce")), "WAITING FOR RETEST")
        self.assertAlmostEqual(_chase_price(candidate()), 183.16, places=2)

    def test_ai_rejection_and_chase_remove_signal(self):
        with patch("app.active_signals._ai_validation", return_value={"decision": "WAIT_FOR_CONFIRMATION"}):
            ok, reasons = _candidate_is_publishable(candidate(), {"actionable_live_quotes": True})
        self.assertFalse(ok)
        self.assertIn("ai_validation_not_approved", reasons)

        row = SimpleNamespace(direction="LONG", valid_until=(datetime.now(timezone.utc) + timedelta(minutes=5)).isoformat())
        reason = _terminal_reason(row, candidate(current_or_previous_session_price=184.0), datetime.now(timezone.utc))
        self.assertEqual(reason[0], "DO NOT CHASE")

    def test_low_reward_to_risk_is_removed(self):
        row = SimpleNamespace(direction="LONG", valid_until=(datetime.now(timezone.utc) + timedelta(minutes=5)).isoformat())
        result = _terminal_reason(row, candidate(targets=[{"price": 182.7}, {"price": 182.9}]), datetime.now(timezone.utc))
        self.assertEqual(result[0], "REMOVED")


if __name__ == "__main__":
    unittest.main()
