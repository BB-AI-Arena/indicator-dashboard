from __future__ import annotations

import unittest
from datetime import datetime
from pathlib import Path
import sys
from zoneinfo import ZoneInfo

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from app.market_session import get_market_session


EASTERN = ZoneInfo("America/New_York")


class MarketSessionTests(unittest.TestCase):
    def test_weekend_is_market_closed(self) -> None:
        session = get_market_session(datetime(2026, 7, 11, 10, 0, tzinfo=EASTERN))
        self.assertEqual(session["session_state"], "MARKET_CLOSED")
        self.assertFalse(session["actionable_live_quotes"])
        self.assertEqual(session["option_quote_session_label"], "Previous session")

    def test_holiday_is_market_closed(self) -> None:
        session = get_market_session(datetime(2026, 7, 3, 12, 0, tzinfo=EASTERN))
        self.assertEqual(session["session_state"], "HOLIDAY")
        self.assertFalse(session["actionable_live_quotes"])
        self.assertGreater(session["minutes_until_open"], 0)

    def test_premarket_is_not_actionable(self) -> None:
        session = get_market_session(datetime(2026, 12, 1, 8, 15, tzinfo=EASTERN))
        self.assertEqual(session["session_state"], "PREMARKET")
        self.assertFalse(session["actionable_live_quotes"])
        self.assertEqual(session["option_quote_session_label"], "Previous session")
        self.assertEqual(session["regular_session_open"], "2026-12-01T09:30:00-05:00")

    def test_regular_hours_are_actionable(self) -> None:
        session = get_market_session(datetime(2026, 12, 1, 10, 15, tzinfo=EASTERN))
        self.assertEqual(session["session_state"], "REGULAR")
        self.assertTrue(session["actionable_live_quotes"])
        self.assertEqual(session["option_quote_session_label"], "Live")

    def test_after_hours_are_not_actionable(self) -> None:
        session = get_market_session(datetime(2026, 12, 1, 17, 15, tzinfo=EASTERN))
        self.assertEqual(session["session_state"], "AFTER_HOURS")
        self.assertFalse(session["actionable_live_quotes"])
        self.assertEqual(session["underlying_session_label"], "Previous session")

    def test_early_close_day_is_identified(self) -> None:
        session = get_market_session(datetime(2026, 11, 27, 12, 0, tzinfo=EASTERN))
        self.assertEqual(session["session_state"], "EARLY_CLOSE")
        self.assertTrue(session["actionable_live_quotes"])
        self.assertTrue(session["is_early_close_day"])
        self.assertEqual(session["regular_session_close"], "2026-11-27T13:00:00-05:00")

    def test_dst_transition_does_not_break_time_conversion(self) -> None:
        session = get_market_session(datetime(2026, 3, 9, 10, 0, tzinfo=EASTERN))
        self.assertEqual(session["session_state"], "REGULAR")
        self.assertTrue(session["current_eastern_timestamp"].endswith("-04:00"))


if __name__ == "__main__":
    unittest.main()
