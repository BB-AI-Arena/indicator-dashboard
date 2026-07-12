from __future__ import annotations

import sys
import unittest
from pathlib import Path
from unittest.mock import patch

import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from app.data_provider import fetch_candles


class DataProviderCacheTests(unittest.TestCase):
    def test_prefer_stored_returns_without_provider_request(self):
        stored = pd.DataFrame(
            {"open": [1.0], "high": [1.1], "low": [0.9], "close": [1.05], "volume": [100]},
            index=pd.DatetimeIndex(["2026-07-10T19:45:00Z"]),
        )
        with patch("app.data_provider.get_candles_from_sql", return_value=stored), patch("app.data_provider.provider_factory.with_fallback", side_effect=AssertionError("provider should not be called")):
            result = fetch_candles("SPY", interval="15m", period="60d", prefer_stored=True)
        self.assertEqual(len(result), 1)


if __name__ == "__main__":
    unittest.main()
