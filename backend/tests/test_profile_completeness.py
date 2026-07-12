from datetime import datetime, timezone

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app.db import Base
from app.models import Candle, TickerProfile
from app.profile_completeness import evaluate_profile_completeness


def _session():
    engine = create_engine("sqlite:///:memory:", connect_args={"check_same_thread": False})
    Base.metadata.create_all(engine)
    return sessionmaker(bind=engine)()


def test_profile_without_backfill_is_not_started_and_not_ready():
    db = _session()
    profile = TickerProfile(symbol="TEST", profile_status="NOT_STARTED", profile_state="NOT_STARTED", stats_json="{}", data_coverage_json="{}", personality_json="[]", latest_setup_state_json="{}")
    db.add(profile)
    db.commit()
    result = evaluate_profile_completeness(db, profile, persist=True)
    assert result["profile_state"] == "NOT_STARTED"
    assert result["planning_ready"] is False
    assert result["live_ready"] is False


def test_partial_backfill_is_building_not_ready():
    db = _session()
    profile = TickerProfile(symbol="TEST", profile_status="BUILDING", profile_state="BUILDING", last_backfill_requested_at=datetime.now(timezone.utc).isoformat(), stats_json="{}", data_coverage_json="{}", personality_json="[]", latest_setup_state_json="{}")
    db.add(profile)
    db.add(Candle(symbol="TEST", interval="1d", timestamp=1, open=1, high=2, low=1, close=2, volume=10, updated_at="now"))
    db.commit()
    result = evaluate_profile_completeness(db, profile, persist=True)
    assert result["profile_state"] == "BUILDING"
    assert result["planning_ready"] is False
    assert result["components"]["daily_history"]["status"] == "MISSING_DATA"


def test_missing_score_is_not_coerced_to_zero():
    db = _session()
    profile = TickerProfile(symbol="TEST", profile_status="PARTIAL", profile_state="PARTIAL", stats_json="{}", data_coverage_json="{}", personality_json="[]", latest_setup_state_json="{}")
    db.add(profile)
    db.commit()
    result = evaluate_profile_completeness(db, profile, persist=True)
    assert result["components"]["deterministic_score"]["status"] == "MISSING_DATA"
