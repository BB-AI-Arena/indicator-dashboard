from __future__ import annotations

import os
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path
from zoneinfo import ZoneInfo
import sys

TEST_ROOT = Path(tempfile.gettempdir()) / "indicator-dashboard-tests"
os.environ.setdefault("DATABASE_PATH", str(TEST_ROOT / "indicator.db"))
os.environ.setdefault("CONFIG_PATH", str(Path(__file__).resolve().parents[2] / "config" / "config.yml"))
os.environ.setdefault("ETRADE_REQUEST_TOKEN_PATH", str(TEST_ROOT / "etrade_request_token.json"))
os.environ.setdefault("ETRADE_ACCESS_TOKEN_PATH", str(TEST_ROOT / "etrade_access_token.json"))

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from app.providers.option_positioning import (  # noqa: E402
    alignment_score_for_side,
    build_option_positioning,
    snapshot_from_positioning,
    summarize_positioning_history,
)


EASTERN = ZoneInfo("America/New_York")


class OptionPositioningTests(unittest.TestCase):
    def _contract(self, contract_type: str, strike: float, expiration: str, *, volume: int, open_interest: int, bid: float, ask: float, last: float) -> dict[str, object]:
        return {
            "type": contract_type,
            "strike": strike,
            "expiration": expiration,
            "volume": volume,
            "open_interest": open_interest,
            "bid": bid,
            "ask": ask,
            "last": last,
        }

    def test_call_heavy_chain_scores_as_call_bias(self) -> None:
        positioning = build_option_positioning(
            symbol="AAPL",
            provider="etrade",
            underlying_price=100.0,
            quote_type="REALTIME",
            quote_timestamp=datetime(2026, 7, 10, 14, 0, tzinfo=timezone.utc).isoformat(),
            market_session={"session_state": "REGULAR", "actionable_live_quotes": True},
            contracts=[
                self._contract("CALL", 100, "2026-07-17", volume=200, open_interest=500, bid=1.0, ask=1.1, last=1.05),
                self._contract("CALL", 105, "2026-07-17", volume=120, open_interest=400, bid=0.6, ask=0.7, last=0.65),
                self._contract("PUT", 95, "2026-07-17", volume=15, open_interest=40, bid=0.3, ask=0.4, last=0.35),
                self._contract("PUT", 90, "2026-07-24", volume=5, open_interest=10, bid=0.1, ask=0.2, last=0.15),
            ],
        )

        self.assertEqual(positioning["session_label"], "Live")
        self.assertEqual(positioning["classification"], "Strong call bias")
        self.assertEqual(positioning["bias"], "CALL")
        self.assertGreater(positioning["bias_score"], 0)
        self.assertEqual(positioning["scopes"]["overall"]["value"]["call_volume"], 320)
        self.assertEqual(positioning["scopes"]["overall"]["value"]["put_volume"], 20)
        self.assertGreater(positioning["scopes"]["overall"]["value"]["weighted_bias"], 0)

    def test_closed_session_uses_previous_session_label(self) -> None:
        positioning = build_option_positioning(
            symbol="AAPL",
            provider="yahoo",
            underlying_price=100.0,
            quote_type="CLOSING",
            quote_timestamp=datetime(2026, 7, 10, 20, 0, tzinfo=timezone.utc).isoformat(),
            market_session={"session_state": "AFTER_HOURS", "actionable_live_quotes": False},
            contracts=[
                self._contract("CALL", 100, "2026-07-17", volume=20, open_interest=50, bid=1.0, ask=1.2, last=1.1),
            ],
        )

        self.assertEqual(positioning["session_label"], "Previous session")
        self.assertFalse(positioning["actionable_live_quotes"])
        self.assertTrue(any("Refresh after the options market opens" in note for note in positioning["notes"]))

    def test_baseline_summary_uses_recent_snapshots(self) -> None:
        positioning = build_option_positioning(
            symbol="AAPL",
            provider="etrade",
            underlying_price=100.0,
            market_session={"session_state": "REGULAR", "actionable_live_quotes": True},
            contracts=[
                self._contract("CALL", 100, "2026-07-17", volume=100, open_interest=300, bid=1.0, ask=1.1, last=1.05),
                self._contract("PUT", 95, "2026-07-17", volume=20, open_interest=40, bid=0.3, ask=0.4, last=0.35),
            ],
        )
        current = snapshot_from_positioning(positioning)
        history = [
            dict(current, bias_score=3.0, put_call_ratio=0.8, call_put_ratio=1.25, weighted_bias=0.2),
            dict(current, bias_score=5.0, put_call_ratio=0.9, call_put_ratio=1.11, weighted_bias=0.1),
        ]

        baseline = summarize_positioning_history(history)
        self.assertEqual(baseline["sample_size"], 2)
        self.assertEqual(baseline["comparison"], "NEAR_BASELINE")
        self.assertAlmostEqual(baseline["recent_average_positioning_score"], 4.0)
        self.assertAlmostEqual(baseline["recent_average_weighted_bias"], 0.15)
        self.assertGreater(alignment_score_for_side(positioning, "LONG"), 0)
        self.assertLess(alignment_score_for_side(positioning, "SHORT"), 0)


if __name__ == "__main__":
    unittest.main()
