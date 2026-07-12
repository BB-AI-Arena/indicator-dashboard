from __future__ import annotations

import json
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

from app.db import Base, SessionLocal, engine  # noqa: E402
from app.decision_dashboard import _grouped_score, build_decision_dashboard, core_universe, ensure_core_universe  # noqa: E402
from app.models import (  # noqa: E402
    Candle,
    HistoricalSetupFeature,
    NewsCatalystSnapshot,
    OptionPositioningSnapshot,
    Scan,
    TickerProfile,
    TickerProfileUpdate,
    Watchlist,
)
from app.ticker_profiles import ensure_ticker_profile  # noqa: E402


class DecisionDashboardTests(unittest.TestCase):
    def setUp(self) -> None:
        Base.metadata.create_all(bind=engine)
        self._reset_state()

    def _reset_state(self) -> None:
        with SessionLocal() as db:
            for model in (
                NewsCatalystSnapshot,
                OptionPositioningSnapshot,
                HistoricalSetupFeature,
                Scan,
                Candle,
                TickerProfileUpdate,
                TickerProfile,
                Watchlist,
            ):
                db.query(model).delete(synchronize_session=False)
            db.commit()

    def _ready_profile(self, symbol: str, *, side: str, expected_value: float, hit_rate: float, sample: int, score: int = 7) -> None:
        now = datetime.now(timezone.utc).replace(microsecond=0)
        coverage = {
            "intervals": {
                "15m": {"rows": 96, "first": (now - timedelta(days=7)).isoformat(), "last": now.isoformat()},
                "1d": {"rows": 240, "first": (now - timedelta(days=300)).isoformat(), "last": now.isoformat()},
            }
        }
        setup_name = "VWAP reclaim continuation" if side == "LONG" else "VWAP rejection continuation"
        stats = {
            "setup_history": {
                "families": [
                    {
                        "setup_family": setup_name,
                        "occurrence_count": sample,
                        "success_count": int(round(sample * hit_rate)),
                        "raw_hit_rate": hit_rate,
                        "out_of_sample_success_rate": max(0.0, hit_rate - 0.04),
                        "confidence_interval": {"low": max(0.0, hit_rate - 0.15), "high": min(1.0, hit_rate + 0.15)},
                        "average_return_pct": 1.1 if side == "LONG" else -1.1,
                        "median_return_pct": 0.8 if side == "LONG" else -0.8,
                        "mfe_pct": 1.8,
                        "mae_pct": -0.7,
                        "expected_value_pct": expected_value,
                        "confidence": "MODERATE",
                    }
                ]
            },
            "fibonacci_behavior": {"interaction_records": 14, "data_status": "observed"},
        }
        latest_setup_state = {
            "contract_selection": {
                "best_contract": {
                    "contract": f"{symbol}260717{'C' if side == 'LONG' else 'P'}00100000",
                    "type": "CALL" if side == "LONG" else "PUT",
                    "expiration": "2026-07-17",
                    "strike": 100,
                    "delta": 0.61,
                    "max_reasonable_entry": 2.1,
                }
            }
        }
        with SessionLocal() as db:
            profile = ensure_ticker_profile(db, symbol, source="unit_test")
            profile.profile_status = "READY_FOR_PLANNING"
            profile.profile_state = "READY_FOR_PLANNING"
            profile.planning_ready = True
            profile.live_ready = False
            profile.last_completeness_check = now.isoformat()
            profile.data_coverage_json = json.dumps(coverage)
            profile.stats_json = json.dumps(stats)
            profile.latest_setup_state_json = json.dumps(latest_setup_state)
            profile.last_profile_update_at = now.isoformat()
            db.add(
                Scan(
                    symbol=symbol,
                    side=side,
                    score=score,
                    max_score=8,
                    grade="TRADE_CANDIDATE",
                    price=101.0,
                    reasons="[]",
                    warnings="[]",
                    created_at=now.isoformat(),
                )
            )
            features = {
                "price": 101.0,
                "atr": 1.4,
                "support": 99.6,
                "resistance": 102.4,
                "vwap": 100.2,
                "relative_volume": 1.5,
                "close_vwap_atr": 0.6 if side == "LONG" else -0.6,
                "ema_fast_slow_atr": 0.4 if side == "LONG" else -0.4,
                "relative_strength_qqq": 0.3 if side == "LONG" else -0.3,
            }
            db.add(
                HistoricalSetupFeature(
                    symbol=symbol,
                    interval="15m",
                    timestamp=int(now.timestamp()),
                    feature_version="test-v1",
                    setup_family=setup_name,
                    direction=side,
                    setup_state="CONFIRMING",
                    data_quality="VALID",
                    features_json=json.dumps(features),
                    created_at=now.isoformat(),
                    updated_at=now.isoformat(),
                )
            )
            db.add(
                OptionPositioningSnapshot(
                    symbol=symbol,
                    provider="test",
                    session_state="REGULAR",
                    reference_session_date=now.date().isoformat(),
                    classification="Moderate call bias" if side == "LONG" else "Moderate put bias",
                    bias_score=4 if side == "LONG" else -4,
                    positioning_json=json.dumps(
                        {
                            "classification": "Moderate call bias" if side == "LONG" else "Moderate put bias",
                            "confidence": "MEDIUM",
                            "scopes": {
                                "overall": {
                                    "value": {
                                        "put_call_volume_ratio": 0.6 if side == "LONG" else 1.8,
                                        "call_put_volume_ratio": 1.7 if side == "LONG" else 0.55,
                                        "put_call_open_interest_ratio": 0.9 if side == "LONG" else 1.2,
                                    }
                                }
                            },
                        }
                    ),
                    created_at=now.isoformat(),
                )
            )
            db.add(
                NewsCatalystSnapshot(
                    key=f"{symbol}-candidate-news",
                    symbol=symbol,
                    context_type="candidate",
                    payload_json=json.dumps({"impact_label": "SUPPORTS POSITION"}),
                    created_at=now.isoformat(),
                    updated_at=now.isoformat(),
                )
            )
            db.commit()

    def test_core_universe_removes_duplicates_and_creates_profiles(self) -> None:
        self.assertEqual(core_universe().count("TSLA"), 1)
        with SessionLocal() as db:
            symbols = ensure_core_universe(db)
            self.assertIn("AAPL", symbols)
            self.assertEqual(db.query(TickerProfile).filter(TickerProfile.symbol == "TSLA").count(), 1)

    def test_dashboard_excludes_incomplete_profiles_from_top_slots(self) -> None:
        with SessionLocal() as db:
            ensure_core_universe(db)
            payload = build_decision_dashboard(db)

        self.assertIsNone(payload["best_long_setup"])
        self.assertIsNone(payload["best_short_setup"])
        self.assertIn("No qualified long setup.", payload["no_trade_conditions"])
        self.assertIn("No qualified short setup.", payload["no_trade_conditions"])

    def test_valid_cached_long_and_short_can_rank_as_top_setups(self) -> None:
        self._ready_profile("AAPL", side="LONG", expected_value=0.84, hit_rate=0.68, sample=52, score=8)
        self._ready_profile("PANW", side="SHORT", expected_value=0.72, hit_rate=0.65, sample=46, score=7)

        with SessionLocal() as db:
            payload = build_decision_dashboard(db)

        self.assertEqual(payload["best_long_setup"]["ticker"], "AAPL")
        self.assertEqual(payload["best_short_setup"]["ticker"], "PANW")
        self.assertTrue(payload["best_long_setup"]["passes_hard_gates"])
        self.assertIn("52", payload["best_long_setup"]["historical_match"]["display"])
        self.assertEqual(payload["best_short_setup"]["next_session_bias"], "LIKELY BEARISH CONTINUATION")
        self.assertLessEqual(len(payload["best_long_setup"]["supporting_factors"]), 3)
        self.assertLessEqual(len(payload["best_long_setup"]["conflicting_factors"]), 2)
        self.assertIn("price_structure", payload["best_long_setup"]["evidence_groups"])

    def test_grouped_score_caps_correlated_evidence(self) -> None:
        groups = {
            "price_structure": {"score": 1, "weight": 25},
            "vwap_control": {"score": 1, "weight": 20},
            "volume_participation": {"score": 1, "weight": 20},
            "relative_behavior": {"score": 1, "weight": 10},
            "historical_evidence": {"score": -1, "weight": 10},
            "options_structure": {"score": -1, "weight": 10},
            "catalyst_context": {"score": 0, "weight": 3},
            "social_sentiment": {"score": 1, "weight": 2},
        }
        score = _grouped_score(groups)
        self.assertIsNotNone(score)
        self.assertGreater(score, 50)
        self.assertLess(score, 80)


if __name__ == "__main__":
    unittest.main()
