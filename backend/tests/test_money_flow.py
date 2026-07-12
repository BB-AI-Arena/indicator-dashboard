from __future__ import annotations

import os
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
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

from app.money_flow import build_money_flow  # noqa: E402


EASTERN = ZoneInfo("America/New_York")


class MoneyFlowTests(unittest.TestCase):
    def _candle(self, ts: datetime, open_: float, high: float, low: float, close: float, volume: float) -> dict[str, object]:
        return {
            "time": int(ts.timestamp()),
            "open": open_,
            "high": high,
            "low": low,
            "close": close,
            "volume": volume,
        }

    def test_bullish_flow_uses_live_label_and_positive_score(self) -> None:
        now = datetime.now(timezone.utc)
        candles = [
            self._candle(now.replace(microsecond=0) - timedelta(minutes=25), 100.0, 100.6, 99.8, 100.4, 1000),
            self._candle(now.replace(microsecond=0) - timedelta(minutes=20), 100.4, 101.2, 100.2, 101.1, 1200),
            self._candle(now.replace(microsecond=0) - timedelta(minutes=15), 101.1, 102.0, 100.9, 101.9, 1500),
            self._candle(now.replace(microsecond=0) - timedelta(minutes=10), 101.9, 103.0, 101.7, 102.8, 1700),
            self._candle(now.replace(microsecond=0) - timedelta(minutes=5), 102.8, 103.8, 102.5, 103.7, 2100),
            self._candle(now.replace(microsecond=0), 103.7, 105.0, 103.5, 104.8, 2500),
        ]

        flow = build_money_flow(
            symbol="AAPL",
            side="LONG",
            market_session={"session_state": "REGULAR", "actionable_live_quotes": True},
            candles=candles,
            indicator_data={
                "latest": {
                    "time": candles[-1]["time"],
                    "close": 104.8,
                    "vwap": 101.5,
                    "volume": 2500,
                    "volume_avg": 1600,
                    "rsi": 68,
                    "timestamp": now.isoformat(),
                },
                "indicators": [
                    {"time": candles[-2]["time"], "close": 103.7, "vwap": 100.8, "volume": 2100},
                    {"time": candles[-1]["time"], "close": 104.8, "vwap": 101.5, "volume": 2500},
                ],
            },
            ratios={
                "positioning": {
                    "classification": "Strong call bias",
                    "bias": "CALL",
                    "bias_score": 8,
                    "confidence": "HIGH",
                }
            },
        )

        self.assertEqual(flow["session_label"], "Live")
        self.assertEqual(flow["market_status"], "FRESH")
        self.assertGreater(flow["score"], 0)
        self.assertIn(flow["classification"], {"MODERATE ACCUMULATION", "STRONG ACCUMULATION"})
        self.assertTrue(flow["position_aligned"])
        self.assertTrue(flow["price_confirmation"]["rising_volume_on_up_candles"])
        self.assertEqual(flow["options_alignment"]["classification"], "Strong call bias")
        self.assertGreater(len(flow["evidence_of_buying_pressure"]), 0)

    def test_after_hours_sets_previous_session_planning_label(self) -> None:
        flow = build_money_flow(
            symbol="AAPL",
            side="SHORT",
            market_session={"session_state": "AFTER_HOURS", "actionable_live_quotes": False},
            candles=[
                self._candle(datetime(2026, 7, 10, 19, 55, tzinfo=timezone.utc), 100.0, 100.4, 99.8, 100.1, 900),
                self._candle(datetime(2026, 7, 10, 20, 0, tzinfo=timezone.utc), 100.1, 100.3, 99.7, 99.9, 800),
            ],
            indicator_data={
                "latest": {
                    "time": int(datetime(2026, 7, 10, 20, 0, tzinfo=timezone.utc).timestamp()),
                    "close": 99.9,
                    "vwap": 100.0,
                    "volume": 800,
                    "timestamp": datetime(2026, 7, 10, 20, 0, tzinfo=timezone.utc).isoformat(),
                }
            },
        )

        self.assertEqual(flow["session_label"], "Previous session")
        self.assertEqual(flow["market_status"], "PREVIOUS_SESSION")
        self.assertEqual(flow["data_quality"]["session"], "AFTER_HOURS")

    def test_missing_data_returns_insufficient_data(self) -> None:
        flow = build_money_flow(symbol="AAPL", side="LONG")

        self.assertEqual(flow["classification"], "INSUFFICIENT DATA")
        self.assertEqual(flow["score"], 0.0)
        self.assertEqual(flow["confidence"], "LOW")
        self.assertEqual(flow["options_alignment"]["data_status"], "unavailable")


if __name__ == "__main__":
    unittest.main()
