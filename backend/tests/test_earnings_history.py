from __future__ import annotations

import unittest
from datetime import date

from app.earnings_history import _normalize_event, _reaction


class EarningsHistoryTests(unittest.TestCase):
    def test_beat_and_after_close_reaction(self) -> None:
        event = _normalize_event(
            {
                "reported_date": "2026-04-30",
                "reported_eps": "1.20",
                "estimated_eps": "1.00",
                "reported_revenue": "120",
                "estimated_revenue": "110",
                "report_time": "post-market",
                "provider": "alphavantage",
            }
        )
        daily = [
            {"date": date(2026, 4, 30), "timestamp": "2026-04-30T20:00:00+00:00", "open": 100, "high": 102, "low": 99, "close": 101, "volume": 1000},
            {"date": date(2026, 5, 1), "timestamp": "2026-05-01T20:00:00+00:00", "open": 105, "high": 108, "low": 103, "close": 107, "volume": 2000},
            {"date": date(2026, 5, 4), "timestamp": "2026-05-04T20:00:00+00:00", "open": 106, "high": 109, "low": 104, "close": 108, "volume": 1500},
            {"date": date(2026, 5, 5), "timestamp": "2026-05-05T20:00:00+00:00", "open": 107, "high": 110, "low": 105, "close": 109, "volume": 1400},
            {"date": date(2026, 5, 6), "timestamp": "2026-05-06T20:00:00+00:00", "open": 108, "high": 111, "low": 106, "close": 110, "volume": 1300},
            {"date": date(2026, 5, 7), "timestamp": "2026-05-07T20:00:00+00:00", "open": 109, "high": 112, "low": 107, "close": 111, "volume": 1200},
        ]

        reaction = _reaction(event, daily)

        self.assertEqual(event["overall_result"], "BEAT")
        self.assertEqual(event["report_timing"], "AFTER_CLOSE")
        self.assertGreater(reaction["gap_pct"], 3)
        self.assertGreater(reaction["first_session_return_pct"], 5)
        self.assertIsNotNone(reaction["5_session_return_pct"])

    def test_missing_estimate_is_not_called_a_miss(self) -> None:
        event = _normalize_event(
            {
                "reported_date": "2026-04-30",
                "reported_eps": "1.20",
                "estimated_eps": "None",
                "provider": "alphavantage",
            }
        )
        self.assertEqual(event["eps_result"], "UNKNOWN")
        self.assertEqual(event["overall_result"], "UNKNOWN")


if __name__ == "__main__":
    unittest.main()
