from __future__ import annotations

import os
import tempfile
from pathlib import Path

TEST_ROOT = Path(tempfile.gettempdir()) / f"indicator-dashboard-tests-{os.getpid()}"
TEST_ROOT.mkdir(parents=True, exist_ok=True)

os.environ["DATABASE_PATH"] = str(TEST_ROOT / "indicator.db")
os.environ["CONFIG_PATH"] = str(Path(__file__).resolve().parents[2] / "config" / "config.yml")
os.environ["ETRADE_REQUEST_TOKEN_PATH"] = str(TEST_ROOT / "etrade_request_token.json")
os.environ["ETRADE_ACCESS_TOKEN_PATH"] = str(TEST_ROOT / "etrade_access_token.json")
os.environ["CACHE_DIR"] = str(TEST_ROOT / "cache")
