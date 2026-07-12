from __future__ import annotations

import json
import os
import tempfile
import unittest
from pathlib import Path
import sys

TEST_ROOT = Path(tempfile.gettempdir()) / "indicator-dashboard-tests"
os.environ.setdefault("DATABASE_PATH", str(TEST_ROOT / "indicator.db"))
os.environ.setdefault("CONFIG_PATH", str(Path(__file__).resolve().parents[2] / "config" / "config.yml"))
os.environ.setdefault("ETRADE_REQUEST_TOKEN_PATH", str(TEST_ROOT / "etrade_request_token.json"))
os.environ.setdefault("ETRADE_ACCESS_TOKEN_PATH", str(TEST_ROOT / "etrade_access_token.json"))

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from app.trade_explanations import build_trade_explanation  # noqa: E402


class TradeExplanationTests(unittest.TestCase):
    def test_prefers_live_underlying_price_over_chart_price(self) -> None:
        explanation = build_trade_explanation(
            candidate={
                "symbol": "AAPL",
                "contract_symbol": "AAPL_CALL_210",
                "type": "CALL",
                "strike": 210.0,
                "expiration": "2026-07-17",
                "bid": 1.0,
                "ask": 1.2,
                "last": 1.1,
                "volume": 250,
                "open_interest": 1200,
                "spread_percentage": 1.5,
                "quote_type": "REALTIME",
                "quote_stale": False,
                "liquidity_grade": "B",
                "risk_grade": "A",
                "trade_grade": "A",
                "underlying_price": 215.25,
                "recommended_max_spread_pct": 5,
                "minimum_volume": 100,
            },
            scan={
                "symbol": "AAPL",
                "side": "LONG",
                "grade": "HIGH_CONVICTION",
                "score": 8,
                "max_score": 8,
                "price": 99.0,
                "indicators": {
                    "atr": 2.0,
                    "vwap": 214.5,
                    "ema_fast": 213.5,
                    "ema_slow": 212.0,
                    "ema_trend": 211.5,
                    "bb_upper": 216.0,
                    "bb_mid": 214.0,
                    "bb_lower": 210.0,
                },
            },
            indicators={
                "latest": {
                    "atr": 2.0,
                    "vwap": 214.5,
                    "ema_fast": 213.5,
                    "ema_slow": 212.0,
                    "ema_trend": 211.5,
                    "bb_upper": 216.0,
                    "bb_mid": 214.0,
                    "bb_lower": 210.0,
                }
            },
            contracts={
                "underlying_price": 215.25,
                "source": "etrade",
            },
            ratios={},
            backtest={
                "win_rate_pct": 59.62,
                "occurrences": 52,
                "sample_confidence": "ENOUGH",
                "historical_edge": "MODERATE",
            },
            ai_gate={
                "decision": "PROCEED",
                "final_decision": "TRADE_CANDIDATE",
                "side": "LONG",
                "blocking_factors": [],
            },
        )

        self.assertEqual(explanation["underlying_reference"]["source"], "etrade_live")
        self.assertEqual(explanation["underlying_reference"]["label"], "Live E*TRADE price")
        self.assertAlmostEqual(explanation["underlying_reference"]["price"], 215.25)
        self.assertNotIn("Stored chart price", json.dumps(explanation))

    def test_missing_live_price_does_not_fall_back_to_chart_price(self) -> None:
        explanation = build_trade_explanation(
            candidate={
                "symbol": "AAPL",
                "contract_symbol": "AAPL_CALL_210",
                "type": "CALL",
                "strike": 210.0,
                "expiration": "2026-07-17",
                "bid": 1.0,
                "ask": 1.2,
                "last": 1.1,
                "volume": 250,
                "open_interest": 1200,
                "spread_percentage": 1.5,
                "quote_type": "REALTIME",
                "quote_stale": False,
                "liquidity_grade": "B",
                "risk_grade": "A",
                "trade_grade": "A",
                "underlying_price": 0.0,
                "recommended_max_spread_pct": 5,
                "minimum_volume": 100,
            },
            scan={
                "symbol": "AAPL",
                "side": "LONG",
                "grade": "HIGH_CONVICTION",
                "score": 8,
                "max_score": 8,
                "price": 99.0,
                "indicators": {
                    "atr": 2.0,
                    "vwap": 214.5,
                    "ema_fast": 213.5,
                    "ema_slow": 212.0,
                    "ema_trend": 211.5,
                    "bb_upper": 216.0,
                    "bb_mid": 214.0,
                    "bb_lower": 210.0,
                },
            },
            indicators={"latest": {"atr": 2.0}},
            contracts={"underlying_price": 0.0, "source": "etrade"},
            ratios={},
            backtest={
                "win_rate_pct": 59.62,
                "occurrences": 52,
                "sample_confidence": "ENOUGH",
                "historical_edge": "MODERATE",
            },
            ai_gate={
                "decision": "DO_NOT_PROCEED",
                "final_decision": "NO_TRADE",
                "side": "LONG",
                "blocking_factors": ["ai_gate_unavailable"],
            },
        )

        self.assertIsNone(explanation["underlying_reference"]["price"])
        self.assertEqual(
            explanation["underlying_reference"]["label"],
            "Live E*TRADE price unavailable",
        )
        self.assertIn("live E*TRADE quote", explanation["plain_english_summary"])
        self.assertIn("live E*TRADE underlying quote", explanation["watch_for"][0])


if __name__ == "__main__":
    unittest.main()
