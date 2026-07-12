from __future__ import annotations

import os
import tempfile
import unittest
from dataclasses import dataclass
from pathlib import Path
import sys
from unittest.mock import patch

TEST_ROOT = Path(tempfile.gettempdir()) / "indicator-dashboard-tests"
os.environ.setdefault("DATABASE_PATH", str(TEST_ROOT / "indicator.db"))
os.environ.setdefault("CONFIG_PATH", str(Path(__file__).resolve().parents[2] / "config" / "config.yml"))
os.environ.setdefault("ETRADE_REQUEST_TOKEN_PATH", str(TEST_ROOT / "etrade_request_token.json"))
os.environ.setdefault("ETRADE_ACCESS_TOKEN_PATH", str(TEST_ROOT / "etrade_access_token.json"))

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from app.providers.alphavantage_provider import AlphaVantageProvider
from app.providers.base import ProviderError


@dataclass
class FakeResponse:
    status_code: int = 200
    payload: dict | None = None
    text: str = ""

    def json(self):
        if self.payload is None:
            raise ValueError("no json payload")
        return self.payload


class AlphaVantageProviderTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.env_patcher = patch.dict(
            os.environ,
            {
                "CACHE_DIR": self.temp_dir.name,
                "ALPHA_VANTAGE_API_KEY": "test-key",
                "ALPHA_VANTAGE_OUTPUT_FORMAT": "json",
            },
            clear=False,
        )
        self.rate_limit_patcher = patch("app.providers.alphavantage_provider.rate_limit", lambda provider: {"provider": provider, "slept_seconds": 0.0})
        self.sleep_patcher = patch("app.providers.alphavantage_provider.time.sleep", lambda *_args, **_kwargs: None)
        self.env_patcher.start()
        self.rate_limit_patcher.start()
        self.sleep_patcher.start()

    def tearDown(self) -> None:
        self.sleep_patcher.stop()
        self.rate_limit_patcher.stop()
        self.env_patcher.stop()
        self.temp_dir.cleanup()

    def _provider(self, output_format: str = "json") -> AlphaVantageProvider:
        os.environ["ALPHA_VANTAGE_OUTPUT_FORMAT"] = output_format
        return AlphaVantageProvider(
            {
                "cache": {"quotes_ttl_seconds": 10, "candles_ttl_seconds": 10},
                "alphavantage": {
                    "timeout_seconds": 1,
                    "mode_ttl_seconds": 1,
                    "daily_prefer_adjusted": True,
                    "intraday_extended_hours": True,
                    "intraday_adjusted": True,
                },
                "rate_limits": {
                    "alphavantage": {
                        "max_retries": 0,
                        "backoff_initial_seconds": 0,
                        "backoff_max_seconds": 0,
                    }
                },
                "data": {"cache_enabled": True},
            }
        )

    def test_get_quote_parses_global_quote(self) -> None:
        provider = self._provider()
        payload = {
            "Global Quote": {
                "01. symbol": "AAPL",
                "02. open": "210.11",
                "03. high": "212.44",
                "04. low": "209.88",
                "05. price": "211.32",
                "06. volume": "1234567",
                "07. latest trading day": "2026-07-09",
                "08. previous close": "209.90",
                "09. change": "1.42",
                "10. change percent": "0.68%",
            }
        }
        with patch("app.providers.alphavantage_provider.requests.get", return_value=FakeResponse(payload=payload)):
            quote = provider.get_quote("AAPL")

        self.assertEqual(quote["symbol"], "AAPL")
        self.assertAlmostEqual(quote["price"], 211.32)
        self.assertEqual(quote["quote_type"], "DELAYED")
        self.assertEqual(quote["provider"], "alphavantage")
        self.assertTrue(quote["timestamp"])

    def test_get_candles_daily_falls_back_from_adjusted_to_daily(self) -> None:
        provider = self._provider()
        premium_only = {"Information": "The full daily history outputsize is available to premium customers only."}
        daily_payload = {
            "Meta Data": {
                "1. Information": "Daily Prices",
                "2. Symbol": "AAPL",
                "3. Last Refreshed": "2026-07-09",
            },
            "Time Series (Daily)": {
                "2026-07-08": {
                    "1. open": "200.00",
                    "2. high": "205.00",
                    "3. low": "199.00",
                    "4. close": "204.00",
                    "5. volume": "1000",
                },
                "2026-07-09": {
                    "1. open": "204.00",
                    "2. high": "206.00",
                    "3. low": "203.00",
                    "4. close": "205.00",
                    "5. volume": "1200",
                },
            },
        }

        with patch(
            "app.providers.alphavantage_provider.requests.get",
            side_effect=[
                FakeResponse(payload=premium_only),
                FakeResponse(payload=daily_payload),
            ],
        ):
            candles = provider.get_candles("AAPL", "1d", "1y")

        self.assertEqual(len(candles), 2)
        self.assertEqual(candles[0]["time"] < candles[1]["time"], True)
        self.assertIn("adjustedClose", candles[0])
        self.assertEqual(candles[0]["adjustedClose"], candles[0]["close"])
        self.assertEqual(candles[1]["volume"], 1200)

    def test_get_candles_intraday_csv_parses_rows(self) -> None:
        provider = self._provider(output_format="csv")
        csv_text = "\n".join(
            [
                "timestamp,open,high,low,close,volume",
                "2026-07-09 15:55:00,210.00,210.50,209.80,210.20,1000",
                "2026-07-09 16:00:00,210.20,210.80,210.10,210.60,1200",
            ]
        )
        with patch("app.providers.alphavantage_provider.requests.get", return_value=FakeResponse(text=csv_text)):
            candles = provider.get_candles("AAPL", "5m", "5d")

        self.assertEqual(len(candles), 2)
        self.assertEqual(candles[-1]["time"] > candles[0]["time"], True)
        self.assertEqual(candles[-1]["adjustedClose"], candles[-1]["close"])
        self.assertEqual(candles[-1]["splitCoefficient"], 1.0)

    def test_rate_limit_note_raises_rate_limited_error(self) -> None:
        provider = self._provider()
        payload = {"Note": "Thank you for using Alpha Vantage! Our standard API call frequency is 5 calls per minute."}
        with patch("app.providers.alphavantage_provider.requests.get", return_value=FakeResponse(payload=payload)):
            with self.assertRaises(ProviderError) as ctx:
                provider.get_quote("AAPL")

        self.assertTrue(getattr(ctx.exception, "rate_limited", False))


if __name__ == "__main__":
    unittest.main()
