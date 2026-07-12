from __future__ import annotations

import os
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path
import sys
from unittest.mock import patch

import pandas as pd

TEST_ROOT = Path(tempfile.gettempdir()) / "indicator-dashboard-tests"
os.environ.setdefault("DATABASE_PATH", str(TEST_ROOT / "indicator.db"))
os.environ.setdefault("CONFIG_PATH", str(Path(__file__).resolve().parents[2] / "config" / "config.yml"))
os.environ.setdefault("ETRADE_REQUEST_TOKEN_PATH", str(TEST_ROOT / "etrade_request_token.json"))
os.environ.setdefault("ETRADE_ACCESS_TOKEN_PATH", str(TEST_ROOT / "etrade_access_token.json"))
os.environ.setdefault("CACHE_DIR", str(TEST_ROOT / "cache"))

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import app.etrade_positions as etrade_positions_module  # noqa: E402
from app.etrade_positions import get_open_option_positions  # noqa: E402


class ETradePositionsTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.env_patcher = patch.dict(
            os.environ,
            {
                "CACHE_DIR": self.temp_dir.name,
                "OPENAI_API_KEY": "test-key",
            },
            clear=False,
        )
        self.env_patcher.start()
        etrade_positions_module._POSITION_REFRESH_ACTIVE = False
        etrade_positions_module._POSITION_REFRESH_STARTED_AT = None
        etrade_positions_module._POSITION_REFRESH_LAST_ERROR = None

    def tearDown(self) -> None:
        self.env_patcher.stop()
        self.temp_dir.cleanup()

    def test_build_positions_snapshot_normalizes_option_positions_and_advice(self) -> None:
        account_list_payload = {
            "AccountListResponse": {
                "Accounts": {
                    "Account": [
                        {
                            "accountIdKey": "12345678",
                            "accountDesc": "Brokerage",
                            "accountType": "BROKERAGE",
                        }
                    ]
                }
            }
        }
        portfolio_payload_page_1 = {
            "PortfolioResponse": {
                "AccountPortfolio": [
                    {
                        "totalPages": 2,
                        "Position": [
                            {
                                "Product": {
                                    "symbol": "AAPL",
                                    "displaySymbol": "AAPL 260117C00210000",
                                    "callPut": "CALL",
                                    "strikePrice": 210.0,
                                },
                                "displaySymbol": "AAPL 260117C00210000",
                                "quantity": 2,
                                "positionType": "LONG",
                                "strikePrice": 210.0,
                                "expiration": "2026-01-17",
                                "daysToExpiration": 7,
                                "Complete": {
                                    "baseSymbolAndPrice": {"symbol": "AAPL", "price": 211.50},
                                    "bid": 1.90,
                                    "ask": 2.05,
                                    "lastTrade": 1.98,
                                    "lastTradeTime": "2026-07-10T18:00:00Z",
                                    "volume": 42,
                                    "openInterest": 123,
                                    "delta": 0.52,
                                    "theta": -0.04,
                                    "vega": 0.12,
                                    "quoteStatus": "CLOSING",
                                    "premium": 1.98,
                                },
                                "marketValue": 400.0,
                                "totalCost": 300.0,
                                "totalGain": 100.0,
                                "totalGainPct": 33.33,
                                "daysGain": 5.0,
                                "daysGainPct": 1.23,
                            }
                        ],
                    }
                ]
            }
        }
        portfolio_payload_page_2 = {
            "PortfolioResponse": {
                "AccountPortfolio": [
                    {
                        "totalPages": 2,
                        "Position": [
                            {
                                "Product": {
                                    "symbol": "AAPL",
                                    "displaySymbol": "AAPL 260117P00205000",
                                    "callPut": "PUT",
                                    "strikePrice": 205.0,
                                },
                                "displaySymbol": "AAPL 260117P00205000",
                                "quantity": -1,
                                "positionType": "SHORT",
                                "strikePrice": 205.0,
                                "expiration": "2026-01-17",
                                "daysToExpiration": 7,
                                "Complete": {
                                    "baseSymbolAndPrice": "AAPL 211.50",
                                    "bid": 2.10,
                                    "ask": 2.30,
                                    "lastTrade": 2.20,
                                    "lastTradeTime": "2026-07-10T18:05:00Z",
                                    "volume": 650,
                                    "openInterest": 88,
                                    "delta": -0.48,
                                    "theta": -0.05,
                                    "vega": 0.11,
                                    "quoteStatus": "REALTIME",
                                    "premium": 2.20,
                                },
                                "marketValue": 220.0,
                                "totalCost": 250.0,
                                "totalGain": -30.0,
                                "totalGainPct": -12.0,
                                "daysGain": -2.0,
                                "daysGainPct": -0.80,
                            }
                        ],
                    }
                ]
            }
        }

        def fake_call(endpoint, params=None, symbol=None):
            if endpoint == "/v1/accounts/list.json":
                return account_list_payload
            if endpoint == "/v1/accounts/12345678/portfolio.json":
                if params and int(params.get("pageNumber") or 1) == 2:
                    return portfolio_payload_page_2
                return portfolio_payload_page_1
            raise AssertionError(endpoint)

        def fake_ai(positions):
            self.assertEqual(len(positions), 2)
            return {
                "status": "ok",
                "model": "gpt-4.1-mini",
                "summary": {
                    "headline": "Hold the call and watch the spread.",
                    "overall_risk": "MEDIUM",
                    "priority_actions": ["Avoid adding until the spread tightens."],
                },
                "positions": [
                    {
                        "position_id": positions[0]["position_id"],
                        "action": "WATCH",
                        "confidence": "HIGH",
                        "summary": "The position is liquid enough to watch, but do not add while the quote is stale and the spread is wide.",
                        "watch_for": ["Spread under 5%", "Underlying above strike"],
                        "close_if": ["Quote stays stale", "The thesis breaks"],
                        "roll_if": ["Roll before expiration if you want to keep the idea alive"],
                        "risk_notes": ["Wide spread"],
                    }
                ],
                "blocking_reason": None,
            }

        def fake_fetch_candles(symbol, interval="5m", period="5d", provider_override=None, **_kwargs):
            base = datetime(2026, 7, 1, 14, 30, tzinfo=timezone.utc)
            if interval == "1d":
                count = 240
                provider = provider_override or "alphavantage"
                step = 86400
            elif interval == "15m":
                count = 96
                provider = provider_override or "finnhub"
                step = 900
            else:
                count = 120
                provider = provider_override or "finnhub"
                step = 300
            rows = []
            for idx in range(count):
                ts = base + (idx * pd.Timedelta(seconds=step))
                rows.append(
                    {
                        "time": ts,
                        "open": 210.0 + (idx * 0.02),
                        "high": 210.2 + (idx * 0.02),
                        "low": 209.8 + (idx * 0.02),
                        "close": 210.1 + (idx * 0.02),
                        "volume": 1000 + idx,
                    }
                )
            df = pd.DataFrame(rows)
            df.attrs.update(
                {
                    "provider": provider,
                    "source": provider,
                    "timestamp": "2026-07-10T18:00:00+00:00",
                    "last_updated": "2026-07-10T18:00:00+00:00",
                }
            )
            return df

        class FakeHistoryProvider:
            def __init__(self, name: str) -> None:
                self.name = name

            def get_candles(self, symbol, interval, period):
                return fake_fetch_candles(symbol, interval=interval, period=period, provider_override=self.name)

        real_get_provider = etrade_positions_module.provider_factory.get_provider

        def fake_get_provider(name):
            normalized = str(name or "").strip().lower()
            if normalized in {"alphavantage", "finnhub"}:
                return FakeHistoryProvider(normalized)
            return real_get_provider(name)

        with patch("app.etrade_positions._call_etrade", side_effect=fake_call), \
             patch("app.etrade_positions._generate_ai_advice", side_effect=fake_ai), \
             patch("app.etrade_positions.fetch_candles", side_effect=fake_fetch_candles), \
             patch("app.etrade_positions.provider_factory.get_provider", side_effect=fake_get_provider):
            snapshot = etrade_positions_module._build_positions_snapshot()

        self.assertEqual(snapshot["status"], "ok")
        self.assertEqual(snapshot["summary"]["account_count"], 1)
        self.assertEqual(snapshot["summary"]["position_count"], 2)
        self.assertEqual(snapshot["summary"]["ai_status"], "ok")
        self.assertEqual(snapshot["accounts"][0]["positions"][0]["advice"]["action"], "WATCH")
        self.assertEqual(snapshot["accounts"][0]["positions"][0]["underlying_quote"]["price"], 211.50)
        self.assertEqual(snapshot["accounts"][0]["positions"][0]["underlying_quote"]["source"], "etrade")
        self.assertGreater(snapshot["accounts"][0]["positions"][0]["historical_context"]["selected_bars_loaded"], 0)
        self.assertEqual(snapshot["accounts"][0]["positions"][0]["historical_context"]["selected_interval"], "15m")
        self.assertGreater(len(snapshot["accounts"][0]["positions"][0]["historical_chart"]["candles"]), 0)
        self.assertIn("ema_200", [line["key"] for line in snapshot["accounts"][0]["positions"][0]["historical_chart"]["line_indicators"]])
        self.assertIn("5m", snapshot["accounts"][0]["positions"][0]["historical_context"]["intervals"])
        self.assertIn("15m", snapshot["accounts"][0]["positions"][0]["historical_context"]["intervals"])
        self.assertIn("1d", snapshot["accounts"][0]["positions"][0]["historical_context"]["intervals"])
        self.assertIn("finnhub", snapshot["summary"]["historical_sources"])
        self.assertIn("alphavantage", snapshot["summary"]["historical_sources"])
        self.assertIn("Quote type is CLOSING.", snapshot["accounts"][0]["positions"][0]["warnings"])
        self.assertIn("Volume is below 100 (42).", snapshot["accounts"][0]["positions"][0]["warnings"])
        self.assertEqual(snapshot["accounts"][0]["positions"][1]["direction"], "SHORT")
        self.assertEqual(snapshot["accounts"][0]["positions"][1]["underlying_quote"]["price"], 211.50)
        self.assertEqual(snapshot["summary"]["quote_sources"], ["etrade"])
        self.assertGreater(snapshot["summary"]["historical_bars_loaded"], 0)

    def test_get_open_option_positions_returns_loading_when_cache_missing(self) -> None:
        with patch("app.etrade_positions._queue_positions_refresh", return_value=True) as queue_refresh:
            snapshot = get_open_option_positions(refresh=False)

        self.assertEqual(snapshot["status"], "loading")
        self.assertEqual(snapshot["summary"]["position_count"], 0)
        self.assertTrue(queue_refresh.called)
        self.assertIn("background", snapshot["message"].lower())

    def test_get_open_option_positions_returns_error_when_background_refresh_failed(self) -> None:
        etrade_positions_module._POSITION_REFRESH_LAST_ERROR = "down"
        with patch("app.etrade_positions._queue_positions_refresh", return_value=False):
            snapshot = get_open_option_positions(refresh=False)

        self.assertEqual(snapshot["status"], "error")
        self.assertEqual(snapshot["summary"]["position_count"], 0)
        self.assertIn("down", snapshot["message"])


if __name__ == "__main__":
    unittest.main()
