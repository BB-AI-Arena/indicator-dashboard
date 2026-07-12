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

from app.providers.base import ProviderError  # noqa: E402
from app.providers.finnhub_provider import FinnhubProvider  # noqa: E402


@dataclass
class FakeResponse:
    status_code: int = 200
    payload: dict | None = None
    text: str = ""

    def json(self):
        if self.payload is None:
            raise ValueError("no json payload")
        return self.payload


class FinnhubProviderTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.env_patcher = patch.dict(
            os.environ,
            {
                "CACHE_DIR": self.temp_dir.name,
                "FINNHUB_API_KEY": "test-key",
            },
            clear=False,
        )
        self.rate_limit_patcher = patch("app.providers.finnhub_provider.rate_limit", lambda provider: {"provider": provider, "slept_seconds": 0.0})
        self.sleep_patcher = patch("app.providers.finnhub_provider.time.sleep", lambda *_args, **_kwargs: None)
        self.env_patcher.start()
        self.rate_limit_patcher.start()
        self.sleep_patcher.start()

    def tearDown(self) -> None:
        self.sleep_patcher.stop()
        self.rate_limit_patcher.stop()
        self.env_patcher.stop()
        self.temp_dir.cleanup()

    def _provider(self) -> FinnhubProvider:
        return FinnhubProvider(
            {
                "cache": {"quotes_ttl_seconds": 10, "candles_ttl_seconds": 10},
                "finnhub": {"timeout_seconds": 1},
                "rate_limits": {
                    "finnhub": {
                        "max_retries": 0,
                        "backoff_initial_seconds": 0,
                        "backoff_max_seconds": 0,
                    }
                },
                "data": {"cache_enabled": True},
            }
        )

    def test_get_quote_parses_quote_payload(self) -> None:
        provider = self._provider()
        payload = {"c": 211.32, "h": 212.44, "l": 209.88, "o": 210.11, "pc": 209.9, "t": 1710000000}

        with patch("app.providers.finnhub_provider.requests.get", return_value=FakeResponse(payload=payload)):
            quote = provider.get_quote("AAPL")

        self.assertEqual(quote["symbol"], "AAPL")
        self.assertAlmostEqual(quote["price"], 211.32)
        self.assertEqual(quote["quote_type"], "REALTIME")
        self.assertEqual(quote["provider"], "finnhub")
        self.assertTrue(quote["timestamp"])

    def test_get_candles_range_parses_stock_candles(self) -> None:
        provider = self._provider()
        payload = {
            "s": "ok",
            "t": [1710000000, 1710000300],
            "o": [210.0, 210.2],
            "h": [210.5, 210.9],
            "l": [209.8, 210.0],
            "c": [210.3, 210.7],
            "v": [1200, 1400],
        }

        with patch("app.providers.finnhub_provider.requests.get", return_value=FakeResponse(payload=payload)):
            candles = provider.get_candles_range("AAPL", "5m", "2024-03-09T14:00:00Z", "2024-03-09T14:10:00Z")

        self.assertEqual(len(candles), 2)
        self.assertEqual(candles[0]["time"], 1710000000)
        self.assertAlmostEqual(candles[1]["close"], 210.7)
        self.assertEqual(candles[1]["provider"], "finnhub")

    def test_rate_limited_payload_raises_provider_error(self) -> None:
        provider = self._provider()
        with patch("app.providers.finnhub_provider.requests.get", return_value=FakeResponse(status_code=429, text="Too Many Requests")):
            with self.assertRaises(ProviderError) as ctx:
                provider.get_quote("AAPL")

        self.assertTrue(getattr(ctx.exception, "rate_limited", False))


if __name__ == "__main__":
    unittest.main()
