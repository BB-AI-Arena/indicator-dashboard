from __future__ import annotations

import os
import sys
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

TEST_ROOT = Path(tempfile.gettempdir()) / "indicator-dashboard-tests"
os.environ.setdefault("DATABASE_PATH", str(TEST_ROOT / "indicator.db"))
os.environ.setdefault("CONFIG_PATH", str(Path(__file__).resolve().parents[2] / "config" / "config.yml"))
os.environ.setdefault("CACHE_DIR", str(TEST_ROOT / "cache"))
os.environ.setdefault("OPENAI_API_KEY", "")

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from app.advisory import generate_advisory, update_advisory_settings, validate_advisory_output  # noqa: E402
from app.db import Base, SessionLocal, engine  # noqa: E402
from app.models import AdvisoryCache, AdvisorySetting, Candle, TickerProfile, TickerProfileStat, TickerProfileUpdate  # noqa: E402
from app.ticker_profiles import ensure_ticker_profile, refresh_ticker_profile, serialize_ticker_profile  # noqa: E402


class TickerProfileAdvisoryTests(unittest.TestCase):
    def setUp(self) -> None:
        Base.metadata.create_all(bind=engine)
        self._reset_state()

    def _reset_state(self) -> None:
        with SessionLocal() as db:
            for model in (AdvisoryCache, AdvisorySetting, TickerProfileStat, TickerProfileUpdate, TickerProfile, Candle):
                db.query(model).delete(synchronize_session=False)
            db.commit()

    def _add_candles(self, symbol: str) -> None:
        now = datetime.now(timezone.utc).replace(hour=20, minute=0, second=0, microsecond=0)
        with SessionLocal() as db:
            for idx in range(45):
                ts = now - timedelta(days=45 - idx)
                close = 100 + idx * 0.35
                db.add(
                    Candle(
                        symbol=symbol,
                        interval="1d",
                        timestamp=int(ts.timestamp()),
                        open=close - 0.2,
                        high=close + 0.8,
                        low=close - 0.6,
                        close=close,
                        volume=1_000_000 + idx * 1_000,
                        provider="test",
                        created_at=ts.isoformat(),
                        updated_at=ts.isoformat(),
                    )
                )
            start = now - timedelta(days=3)
            for idx in range(80):
                ts = start + timedelta(minutes=15 * idx)
                close = 120 + idx * 0.05
                db.add(
                    Candle(
                        symbol=symbol,
                        interval="15m",
                        timestamp=int(ts.timestamp()),
                        open=close - 0.05,
                        high=close + 0.2,
                        low=close - 0.15,
                        close=close,
                        volume=50_000 + idx * 100,
                        provider="test",
                        created_at=ts.isoformat(),
                        updated_at=ts.isoformat(),
                    )
                )
            db.commit()

    def test_ticker_profile_is_created_reused_and_refreshed_from_sql(self) -> None:
        self._add_candles("AAPL")

        with SessionLocal() as db:
            created = ensure_ticker_profile(db, "aapl", source="test")
            reused = ensure_ticker_profile(db, "AAPL", source="test_again")
            self.assertEqual(created.symbol, reused.symbol)

            refreshed = refresh_ticker_profile(db, "AAPL", source="unit_test")
            db.commit()
            payload = serialize_ticker_profile(refreshed)

            self.assertEqual(payload["symbol"], "AAPL")
            self.assertEqual(payload["profile_status"], "READY")
            self.assertGreaterEqual(payload["data_coverage"]["intervals"]["1d"]["rows"], 45)
            self.assertGreaterEqual(payload["stats"]["price_behavior"]["daily_sample"], 30)
            self.assertIn("indicator_history", payload["stats"])
            self.assertGreater(db.query(TickerProfileStat).filter(TickerProfileStat.symbol == "AAPL").count(), 0)
            self.assertGreater(db.query(TickerProfileUpdate).filter(TickerProfileUpdate.symbol == "AAPL").count(), 0)

    def _package(self, *, hard_gates: list[str] | None = None) -> dict[str, object]:
        return {
            "candidate_id": "AAPL:LONG",
            "ticker_profile_summary": {"symbol": "AAPL", "profile_status": "READY"},
            "historical_setup_statistics": {
                "same_symbol": {"examples": 52, "raw_success_rate": 0.68, "confidence": "MODERATE"},
                "cross_symbol": {"examples": 0, "confidence": "INSUFFICIENT"},
                "invalidation_condition": {"price": 195.0, "condition": "Invalid below 195."},
                "confirmation_condition": {"price": 201.0, "condition": "Confirm above 201."},
            },
            "selected_contracts": {
                "status": "OK",
                "best_contract": {"contract": "AAPL260717C00200000"},
                "reviewed": [{"contract": "AAPL260717C00200000"}],
            },
            "deterministic_recommendation": {"hard_gates": hard_gates or []},
            "missing_data": [],
        }

    def _advice(self, *, decision: str = "WAIT", rate: float | None = 0.68, contract: str = "AAPL260717C00200000") -> dict[str, object]:
        return {
            "decision": decision,
            "conviction": "Moderate",
            "thesis": "The evidence currently favors waiting for confirmation.",
            "why": ["Same-symbol history is moderate."],
            "conflicts": ["Entry is not confirmed."],
            "entry": {"underlying_trigger": 201.0, "option_price_range": "2.00 - 2.15", "text": "Wait for 201."},
            "confirmation": "Completed 15-minute close above 201.",
            "invalidation": {"price": 195.0, "text": "Invalid below 195."},
            "targets": ["Target 1 near 205."],
            "contract": f"Use supplied contract {contract} only if gates pass.",
            "risk": "Risk is controlled at invalidation.",
            "historical_match": {
                "sample_size": 52,
                "target_before_invalidation_rate": rate,
                "confidence_interval": "wide",
                "expected_value": "positive",
                "text": "52 examples.",
            },
            "hard_truth": "No entry exists until confirmation.",
            "next_action": "Wait for confirmation.",
        }

    def test_advisory_validation_rejects_altered_probability_and_prohibited_language(self) -> None:
        package = self._package()

        self.assertIn("model altered deterministic probability", validate_advisory_output(self._advice(rate=0.9), package))
        errors = validate_advisory_output({**self._advice(), "hard_truth": "This is guaranteed."}, package)

        self.assertTrue(any("prohibited language" in error for error in errors))

    def test_advisory_validation_rejects_enter_when_hard_gates_exist(self) -> None:
        errors = validate_advisory_output(self._advice(decision="ENTER"), self._package(hard_gates=["insufficient_historical_sample"]))

        self.assertIn("model attempted to enter despite deterministic hard gates", errors)

    def test_deterministic_only_advisory_is_cached_without_openai(self) -> None:
        with SessionLocal() as db:
            settings = update_advisory_settings(db, {"enabled": True, "deterministic_only": True}, username="unit-test")
            self.assertTrue(settings["deterministic_only"])

            first = generate_advisory(db, self._package(hard_gates=["no_acceptable_contract"]))
            second = generate_advisory(db, self._package(hard_gates=["no_acceptable_contract"]))

            self.assertTrue(first["metadata"]["deterministic_fallback"])
            self.assertEqual(first["metadata"]["model_used"], "deterministic")
            self.assertTrue(second["metadata"]["cached"])
            self.assertEqual(db.query(AdvisoryCache).count(), 1)


if __name__ == "__main__":
    unittest.main()
