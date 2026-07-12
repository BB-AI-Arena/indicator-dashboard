from __future__ import annotations

import os
import sys
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pandas as pd

TEST_ROOT = Path(tempfile.gettempdir()) / "indicator-dashboard-tests"
os.environ.setdefault("DATABASE_PATH", str(TEST_ROOT / "indicator.db"))
os.environ.setdefault("CONFIG_PATH", str(Path(__file__).resolve().parents[2] / "config" / "config.yml"))
os.environ.setdefault("ETRADE_REQUEST_TOKEN_PATH", str(TEST_ROOT / "etrade_request_token.json"))
os.environ.setdefault("ETRADE_ACCESS_TOKEN_PATH", str(TEST_ROOT / "etrade_access_token.json"))

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from app.historical_patterns import (  # noqa: E402
    _dedupe_matches,
    _feature_from_row,
    _outcome_for,
    _summarize_scope,
    wilson_interval,
)


class HistoricalPatternTests(unittest.TestCase):
    def _frame(self, rows: int = 90) -> pd.DataFrame:
        start = datetime(2026, 7, 6, 13, 30, tzinfo=timezone.utc)
        data = []
        price = 100.0
        for idx in range(rows):
            drift = 0.08 if idx < 70 else -0.02
            price += drift
            open_ = price - 0.03
            close = price
            high = price + 0.35
            low = price - 0.18
            volume = 1000 + (idx * 12)
            data.append(
                {
                    "open": open_,
                    "high": high,
                    "low": low,
                    "close": close,
                    "volume": volume,
                    "vwap": price - 0.5,
                    "ema_fast": price - 0.2,
                    "ema_slow": price - 0.35,
                    "ema_trend": price - 0.7,
                    "rsi": 58 + min(idx / 20, 8),
                    "macd_hist": 0.12,
                    "bb_upper": price + 2,
                    "bb_lower": price - 2,
                    "atr": 0.8,
                    "volume_avg": 1100,
                    "support": price - 1.2,
                    "resistance": price + 0.6,
                    "obv_slope": 800,
                    "cmf": 0.18,
                    "mfi": 62,
                    "gap_pct": 0.1,
                    "_minutes_from_open": (idx % 26) * 15,
                    "_weekday": 0,
                }
            )
        index = pd.date_range(start=start, periods=rows, freq="15min")
        frame = pd.DataFrame(data, index=index)
        frame.attrs["interval"] = "15m"
        return frame

    def test_wilson_interval_bounds_hit_rate(self) -> None:
        interval = wilson_interval(13, 18)

        self.assertLess(interval["low"], 13 / 18)
        self.assertGreater(interval["high"], 13 / 18)

    def test_feature_vector_creation_uses_completed_candle_facts(self) -> None:
        frame = self._frame()
        rel = pd.Series(0.2, index=frame.index)
        example = _feature_from_row("META", "15m", frame, 60, rel, rel)

        self.assertIsNotNone(example)
        self.assertEqual(example.symbol, "META")
        self.assertEqual(example.direction, "LONG")
        self.assertEqual(example.setup_state, "CONFIRMING")
        self.assertIn("close_vwap_atr", example.vector)
        self.assertTrue(example.features["candle_completed"])

    def test_outcome_label_does_not_look_past_requested_horizon(self) -> None:
        frame = self._frame(80)
        frame.iloc[66, frame.columns.get_loc("high")] = frame.iloc[60]["close"] + 4.0

        short_horizon = _outcome_for(frame, 60, "LONG", 4)
        long_horizon = _outcome_for(frame, 60, "LONG", 8)

        self.assertFalse(short_horizon["target_1_reached"])
        self.assertTrue(long_horizon["target_1_reached"])

    def test_scope_summary_marks_small_sample_as_insufficient(self) -> None:
        matches = [
            {
                "timestamp": f"2026-07-0{idx + 1}T14:00:00+00:00",
                "target_1_before_invalidation": idx % 2 == 0,
                "target_2_before_invalidation": False,
                "invalidation_before_target": idx % 2 == 1,
                "forward_outcome": "BULLISH",
                "forward_return_pct": 0.8,
                "mfe_pct": 1.2,
                "mae_pct": -0.4,
                "profitable_after_costs": idx % 2 == 0,
            }
            for idx in range(6)
        ]

        summary = _summarize_scope(matches, scope="same_symbol")

        self.assertEqual(summary["examples"], 6)
        self.assertEqual(summary["confidence"], "INSUFFICIENT")
        self.assertIn("Fewer than 10", summary["warning"])

    def test_event_deduplication_removes_overlapping_trend_candles(self) -> None:
        start = datetime(2026, 7, 6, 14, 0, tzinfo=timezone.utc)
        matches = [
            {
                "symbol": "NVDA",
                "setup_family": "VWAP reclaim continuation",
                "timestamp": (start + timedelta(minutes=15 * idx)).isoformat(),
                "similarity_score": 80 - idx,
            }
            for idx in range(6)
        ]

        deduped = _dedupe_matches(matches)

        self.assertEqual(len(deduped), 1)


if __name__ == "__main__":
    unittest.main()
