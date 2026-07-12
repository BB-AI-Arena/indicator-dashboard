from app.models import RecommendationRecord
from app.recommendation_performance import _metrics


def test_full_trade_win_rate_excludes_untriggered_and_includes_resolved_neutral():
    rows = [
        RecommendationRecord(status="RESOLVED", triggered_at="2026-07-11T15:00:00+00:00", outcome="WIN", direction="LONG"),
        RecommendationRecord(status="RESOLVED", triggered_at="2026-07-11T16:00:00+00:00", outcome="LOSS", direction="SHORT"),
        RecommendationRecord(status="RESOLVED", triggered_at="2026-07-11T17:00:00+00:00", outcome="NEUTRAL", direction="LONG"),
        RecommendationRecord(status="CREATED", triggered_at=None, outcome="UNRESOLVED", direction="SHORT"),
    ]
    result = _metrics(rows)
    assert result["resolved"] == 3
    assert result["wins"] == 1
    assert result["losses"] == 1
    assert result["neutral"] == 1
    assert result["full_trade_win_rate"] == 1 / 3


def test_supporting_metrics_only_use_rows_with_explicit_measurements():
    rows = [
        RecommendationRecord(
            status="RESOLVED",
            triggered_at="2026-07-11T15:00:00+00:00",
            outcome="WIN",
            direction="LONG",
            target_before_invalidation=True,
            profitable_option=True,
            directional_correct=True,
            realized_pnl=100,
        ),
        RecommendationRecord(
            status="RESOLVED",
            triggered_at="2026-07-11T16:00:00+00:00",
            outcome="LOSS",
            direction="SHORT",
            target_before_invalidation=None,
            profitable_option=None,
            directional_correct=False,
            realized_pnl=-50,
        ),
    ]
    result = _metrics(rows)
    assert result["target_before_invalidation_rate"] == 1.0
    assert result["profitable_option_rate"] == 1.0
    assert result["directional_accuracy"] == 0.5
    assert result["profit_factor"] == 2.0
    assert result["expectancy"] == 25.0
