from __future__ import annotations

from sqlalchemy import Boolean, Column, Float, ForeignKey, Index, Integer, String, Text, UniqueConstraint, text

from .db import Base


class Scan(Base):
    __tablename__ = "scans"

    id = Column(Integer, primary_key=True, index=True)
    symbol = Column(String(16), index=True, nullable=False)
    side = Column(String(16), nullable=False)
    score = Column(Integer, nullable=False)
    max_score = Column(Integer, nullable=False)
    grade = Column(String(32), nullable=False)
    price = Column(Float, nullable=False)
    reasons = Column(Text, nullable=True)
    warnings = Column(Text, nullable=True)
    created_at = Column(String(64), nullable=False, index=True)


class Alert(Base):
    __tablename__ = "alerts"

    id = Column(Integer, primary_key=True, index=True)
    symbol = Column(String(16), index=True, nullable=False)
    side = Column(String(16), nullable=False)
    score = Column(Integer, nullable=False)
    price = Column(Float, nullable=False)
    reasons = Column(Text, nullable=True)
    created_at = Column(String(64), nullable=False, index=True)


class Watchlist(Base):
    __tablename__ = "watchlist"

    symbol = Column(String(16), primary_key=True, index=True)
    source = Column(String(32), nullable=False, default="user")
    active = Column(Boolean, default=True, nullable=False)
    added_at = Column(String(64), nullable=False)


class SignalOutcome(Base):
    __tablename__ = "signal_outcomes"

    id = Column(Integer, primary_key=True, index=True)
    symbol = Column(String(16), index=True, nullable=False)
    side = Column(String(16), nullable=False)
    score = Column(Integer, nullable=False)
    outcome = Column(String(32), nullable=True)
    notes = Column(Text, nullable=True)
    created_at = Column(String(64), nullable=False, index=True)


class RecommendationRecord(Base):
    """Immutable recommendation snapshot with mutable lifecycle fields.

    snapshot_json is written once at creation and is never replaced by later
    scoring rules. Lifecycle changes are preserved separately in events.
    """

    __tablename__ = "recommendation_records"
    __table_args__ = (
        UniqueConstraint("recommendation_id", name="uq_recommendation_records_recommendation_id"),
        Index("ix_recommendation_records_symbol_created_at", "symbol", "created_at"),
    )

    id = Column(Integer, primary_key=True, index=True)
    recommendation_id = Column(String(96), nullable=False, index=True)
    symbol = Column(String(16), nullable=False, index=True)
    direction = Column(String(16), nullable=False)
    setup_type = Column(String(128), nullable=True, index=True)
    status = Column(String(32), nullable=False, default="CREATED", index=True)
    outcome = Column(String(16), nullable=False, default="UNRESOLVED", index=True)
    created_at = Column(String(64), nullable=False, index=True)
    triggered_at = Column(String(64), nullable=True)
    resolved_at = Column(String(64), nullable=True)
    model_version = Column(String(64), nullable=True, index=True)
    confidence_tier = Column(String(32), nullable=True, index=True)
    aggression_mode = Column(String(32), nullable=True, index=True)
    overnight = Column(Boolean, nullable=False, default=False, index=True)
    dte = Column(Float, nullable=True, index=True)
    delta = Column(Float, nullable=True, index=True)
    market_regime = Column(String(64), nullable=True, index=True)
    entry_price = Column(Float, nullable=True)
    invalidation_price = Column(Float, nullable=True)
    target_1_price = Column(Float, nullable=True)
    target_2_price = Column(Float, nullable=True)
    option_contract = Column(String(160), nullable=True)
    option_entry_price = Column(Float, nullable=True)
    option_exit_price = Column(Float, nullable=True)
    realized_pnl = Column(Float, nullable=True)
    underlying_return_pct = Column(Float, nullable=True)
    option_return_pct = Column(Float, nullable=True)
    target_before_invalidation = Column(Boolean, nullable=True)
    profitable_option = Column(Boolean, nullable=True)
    directional_correct = Column(Boolean, nullable=True)
    trigger_source = Column(String(64), nullable=True)
    snapshot_version = Column(String(64), nullable=False, default="recommendation-v1")
    snapshot_json = Column(Text, nullable=False)
    outcome_json = Column(Text, nullable=True)
    created_by = Column(String(64), nullable=True)


class RecommendationEvent(Base):
    """Append-only audit trail for recommendation lifecycle transitions."""

    __tablename__ = "recommendation_events"
    __table_args__ = (
        Index("ix_recommendation_events_recommendation_created_at", "recommendation_id", "created_at"),
    )

    id = Column(Integer, primary_key=True, index=True)
    recommendation_id = Column(String(96), nullable=False, index=True)
    event_type = Column(String(32), nullable=False)
    payload_json = Column(Text, nullable=False, default="{}")
    created_at = Column(String(64), nullable=False, server_default=text("CURRENT_TIMESTAMP"))
    created_by = Column(String(64), nullable=True)


class Candle(Base):
    __tablename__ = "candles"
    __table_args__ = (
        UniqueConstraint("symbol", "interval", "timestamp", name="uq_candles_symbol_interval_timestamp"),
        Index("ix_candles_symbol_interval_timestamp", "symbol", "interval", "timestamp"),
    )

    id = Column(Integer, primary_key=True, index=True)
    symbol = Column(String(16), nullable=False, index=True)
    interval = Column(String(16), nullable=False, index=True)
    timestamp = Column(Integer, nullable=False, index=True)
    open = Column(Float, nullable=False)
    high = Column(Float, nullable=False)
    low = Column(Float, nullable=False)
    close = Column(Float, nullable=False)
    volume = Column(Float, nullable=False, default=0.0)
    provider = Column(String(32), nullable=True)
    created_at = Column(String(64), nullable=False, server_default=text("CURRENT_TIMESTAMP"))
    updated_at = Column(String(64), nullable=False)


class ProviderErrorLog(Base):
    __tablename__ = "provider_errors"
    __table_args__ = (
        Index("ix_provider_errors_provider_symbol_created_at", "provider", "symbol", "created_at"),
    )

    id = Column(Integer, primary_key=True, index=True)
    provider = Column(String(32), nullable=False)
    symbol = Column(String(16), nullable=True)
    endpoint = Column(String(128), nullable=True)
    error_message = Column(Text, nullable=False)
    error_type = Column(String(64), nullable=True)
    retry_after_seconds = Column(Integer, nullable=True)
    created_at = Column(String(64), nullable=False, server_default=text("CURRENT_TIMESTAMP"))


class BackfillRun(Base):
    __tablename__ = "backfill_runs"

    id = Column(Integer, primary_key=True, index=True)
    status = Column(String(32), nullable=False, index=True)
    symbols = Column(Text, nullable=True)
    intervals = Column(Text, nullable=True)
    period = Column(String(32), nullable=True)
    started_at = Column(String(64), nullable=False, index=True, server_default=text("CURRENT_TIMESTAMP"))
    finished_at = Column(String(64), nullable=True)
    rows_inserted = Column(Integer, nullable=False, default=0)
    rows_updated = Column(Integer, nullable=False, default=0)
    chunks_total = Column(Integer, nullable=False, default=0)
    chunks_completed = Column(Integer, nullable=False, default=0)
    chunks_failed = Column(Integer, nullable=False, default=0)
    error_count = Column(Integer, nullable=False, default=0)
    message = Column(Text, nullable=True)


class BackfillChunk(Base):
    __tablename__ = "backfill_chunks"
    __table_args__ = (
        UniqueConstraint(
            "run_id",
            "symbol",
            "interval",
            "start_timestamp",
            "end_timestamp",
            name="uq_backfill_chunk_range",
        ),
        Index("ix_backfill_chunks_run_status", "run_id", "status"),
    )

    id = Column(Integer, primary_key=True, index=True)
    run_id = Column(Integer, ForeignKey("backfill_runs.id"), nullable=False)
    symbol = Column(String(16), nullable=False, index=True)
    interval = Column(String(16), nullable=False, index=True)
    start_timestamp = Column(String(64), nullable=False)
    end_timestamp = Column(String(64), nullable=False)
    status = Column(String(32), nullable=False, index=True)
    provider = Column(String(32), nullable=True)
    rows_inserted = Column(Integer, nullable=False, default=0)
    rows_updated = Column(Integer, nullable=False, default=0)
    error_message = Column(Text, nullable=True)
    started_at = Column(String(64), nullable=True)
    finished_at = Column(String(64), nullable=True)
    created_at = Column(String(64), nullable=False, index=True, server_default=text("CURRENT_TIMESTAMP"))


class OptionPositioningSnapshot(Base):
    __tablename__ = "option_positioning_snapshots"
    __table_args__ = (
        Index("ix_option_positioning_snapshots_symbol_created_at", "symbol", "created_at"),
    )

    id = Column(Integer, primary_key=True, index=True)
    symbol = Column(String(16), nullable=False, index=True)
    provider = Column(String(32), nullable=True)
    session_state = Column(String(32), nullable=True, index=True)
    reference_session_date = Column(String(16), nullable=True, index=True)
    classification = Column(String(64), nullable=True)
    bias_score = Column(Float, nullable=True)
    positioning_json = Column(Text, nullable=False)
    created_at = Column(String(64), nullable=False, server_default=text("CURRENT_TIMESTAMP"))


class SocialMention(Base):
    __tablename__ = "social_mentions"
    __table_args__ = (
        UniqueConstraint("symbol", "source", "external_id", name="uq_social_mentions_symbol_source_external_id"),
        Index("ix_social_mentions_symbol_published_at", "symbol", "published_at"),
        Index("ix_social_mentions_symbol_source", "symbol", "source"),
    )

    id = Column(Integer, primary_key=True, index=True)
    symbol = Column(String(16), nullable=False, index=True)
    source = Column(String(64), nullable=False)
    external_id = Column(String(256), nullable=False)
    author_hash = Column(String(128), nullable=True)
    published_at = Column(String(64), nullable=True, index=True)
    retrieved_at = Column(String(64), nullable=False)
    title = Column(Text, nullable=True)
    text_excerpt = Column(Text, nullable=True)
    url = Column(Text, nullable=True)
    engagement_count = Column(Integer, nullable=True)
    replies = Column(Integer, nullable=True)
    upvotes = Column(Integer, nullable=True)
    relevance_score = Column(Float, nullable=True)
    stance = Column(String(24), nullable=True)
    topics_json = Column(Text, nullable=False, default="[]")
    spam_probability = Column(Float, nullable=True)
    bot_indicator = Column(String(32), nullable=True)
    duplicate_group = Column(String(128), nullable=True, index=True)
    source_credibility = Column(Float, nullable=True)
    language = Column(String(16), nullable=True)
    data_version = Column(String(64), nullable=False, default="social-v1")


class SocialSnapshot(Base):
    __tablename__ = "social_snapshots"
    __table_args__ = (
        Index("ix_social_snapshots_symbol_created_at", "symbol", "created_at"),
    )

    id = Column(Integer, primary_key=True, index=True)
    symbol = Column(String(16), nullable=False, index=True)
    session_state = Column(String(32), nullable=True)
    sentiment_score = Column(Float, nullable=True)
    mention_count = Column(Integer, nullable=False, default=0)
    unique_author_count = Column(Integer, nullable=False, default=0)
    source_diversity = Column(Float, nullable=True)
    spam_risk = Column(Float, nullable=True)
    summary_json = Column(Text, nullable=False)
    data_version = Column(String(64), nullable=False, default="social-v1")
    created_at = Column(String(64), nullable=False, server_default=text("CURRENT_TIMESTAMP"))


class PaperPositionRiskState(Base):
    __tablename__ = "paper_position_risk_states"
    __table_args__ = (
        UniqueConstraint("position_id", name="uq_paper_position_risk_state_position_id"),
        Index("ix_paper_position_risk_states_symbol_updated_at", "symbol", "updated_at"),
    )

    id = Column(Integer, primary_key=True, index=True)
    paper_portfolio_id = Column(Integer, ForeignKey("paper_portfolios.id"), nullable=True, index=True)
    position_id = Column(String(160), nullable=False)
    symbol = Column(String(16), nullable=False, index=True)
    entry_price = Column(Float, nullable=True)
    activation_price = Column(Float, nullable=True)
    highest_executable_price = Column(Float, nullable=True)
    trailing_stop_price = Column(Float, nullable=True)
    trail_status = Column(String(32), nullable=False, default="INACTIVE")
    overnight_status = Column(String(48), nullable=True)
    last_quote_timestamp = Column(String(64), nullable=True)
    last_evaluated_at = Column(String(64), nullable=False)
    state_json = Column(Text, nullable=False, default="{}")
    created_at = Column(String(64), nullable=False, server_default=text("CURRENT_TIMESTAMP"))
    updated_at = Column(String(64), nullable=False)


class PaperRiskAuditEvent(Base):
    __tablename__ = "paper_risk_audit_events"
    __table_args__ = (
        Index("ix_paper_risk_audit_events_position_created_at", "position_id", "created_at"),
    )

    id = Column(Integer, primary_key=True, index=True)
    paper_portfolio_id = Column(Integer, ForeignKey("paper_portfolios.id"), nullable=True, index=True)
    position_id = Column(String(160), nullable=False, index=True)
    symbol = Column(String(16), nullable=True)
    event_type = Column(String(64), nullable=False)
    reason = Column(Text, nullable=False)
    details_json = Column(Text, nullable=False)
    created_at = Column(String(64), nullable=False, server_default=text("CURRENT_TIMESTAMP"))


class BrokerageAccount(Base):
    __tablename__ = "brokerage_accounts"
    __table_args__ = (
        UniqueConstraint("broker", "broker_record_id", name="uq_brokerage_accounts_broker_record_id"),
        Index("ix_brokerage_accounts_broker_updated_at", "broker", "updated_at"),
    )

    id = Column(Integer, primary_key=True, index=True)
    broker = Column(String(32), nullable=False, default="etrade")
    broker_record_id = Column(String(160), nullable=False)
    masked_account = Column(String(64), nullable=True)
    account_type = Column(String(64), nullable=True)
    account_description = Column(String(256), nullable=True)
    institution_type = Column(String(64), nullable=True)
    account_equity = Column(Float, nullable=True)
    cash_balance = Column(Float, nullable=True)
    buying_power = Column(Float, nullable=True)
    last_synced_at = Column(String(64), nullable=True)
    created_at = Column(String(64), nullable=False, server_default=text("CURRENT_TIMESTAMP"))
    updated_at = Column(String(64), nullable=False, server_default=text("CURRENT_TIMESTAMP"))


class BrokeragePosition(Base):
    __tablename__ = "brokerage_positions"
    __table_args__ = (
        UniqueConstraint("brokerage_account_id", "broker_record_id", name="uq_brokerage_positions_account_record"),
        Index("ix_brokerage_positions_account_symbol", "brokerage_account_id", "symbol"),
    )

    id = Column(Integer, primary_key=True, index=True)
    brokerage_account_id = Column(Integer, ForeignKey("brokerage_accounts.id"), nullable=False, index=True)
    broker = Column(String(32), nullable=False, default="etrade")
    broker_record_id = Column(String(160), nullable=False)
    symbol = Column(String(32), nullable=True, index=True)
    contract_symbol = Column(String(160), nullable=True)
    quantity = Column(Float, nullable=True)
    average_cost = Column(Float, nullable=True)
    market_value = Column(Float, nullable=True)
    unrealized_pnl = Column(Float, nullable=True)
    realized_pnl = Column(Float, nullable=True)
    broker_timestamp = Column(String(64), nullable=True)
    synced_at = Column(String(64), nullable=False)
    payload_json = Column(Text, nullable=True)


class BrokerageOrder(Base):
    __tablename__ = "brokerage_orders"
    __table_args__ = (
        UniqueConstraint("brokerage_account_id", "broker_record_id", name="uq_brokerage_orders_account_record"),
        Index("ix_brokerage_orders_account_timestamp", "brokerage_account_id", "broker_timestamp"),
    )

    id = Column(Integer, primary_key=True, index=True)
    brokerage_account_id = Column(Integer, ForeignKey("brokerage_accounts.id"), nullable=False, index=True)
    broker = Column(String(32), nullable=False, default="etrade")
    broker_record_id = Column(String(160), nullable=False)
    status = Column(String(64), nullable=True)
    broker_timestamp = Column(String(64), nullable=True)
    payload_json = Column(Text, nullable=True)
    synced_at = Column(String(64), nullable=False)


class BrokerageFill(Base):
    __tablename__ = "brokerage_fills"
    __table_args__ = (
        UniqueConstraint("brokerage_account_id", "broker_record_id", name="uq_brokerage_fills_account_record"),
        Index("ix_brokerage_fills_account_timestamp", "brokerage_account_id", "broker_timestamp"),
    )

    id = Column(Integer, primary_key=True, index=True)
    brokerage_account_id = Column(Integer, ForeignKey("brokerage_accounts.id"), nullable=False, index=True)
    broker = Column(String(32), nullable=False, default="etrade")
    broker_record_id = Column(String(160), nullable=False)
    order_record_id = Column(String(160), nullable=True)
    symbol = Column(String(32), nullable=True)
    quantity = Column(Float, nullable=True)
    fill_price = Column(Float, nullable=True)
    fees = Column(Float, nullable=True)
    broker_timestamp = Column(String(64), nullable=True)
    payload_json = Column(Text, nullable=True)
    synced_at = Column(String(64), nullable=False)


class BrokerageTransaction(Base):
    __tablename__ = "brokerage_transactions"
    __table_args__ = (
        UniqueConstraint("brokerage_account_id", "broker_record_id", name="uq_brokerage_transactions_account_record"),
        Index("ix_brokerage_transactions_account_timestamp", "brokerage_account_id", "broker_timestamp"),
    )

    id = Column(Integer, primary_key=True, index=True)
    brokerage_account_id = Column(Integer, ForeignKey("brokerage_accounts.id"), nullable=False, index=True)
    broker = Column(String(32), nullable=False, default="etrade")
    broker_record_id = Column(String(160), nullable=False)
    transaction_type = Column(String(64), nullable=True)
    symbol = Column(String(32), nullable=True)
    amount = Column(Float, nullable=True)
    broker_timestamp = Column(String(64), nullable=True)
    payload_json = Column(Text, nullable=True)
    synced_at = Column(String(64), nullable=False)


class PaperPortfolio(Base):
    __tablename__ = "paper_portfolios"
    __table_args__ = (Index("ix_paper_portfolios_status", "status"),)

    id = Column(Integer, primary_key=True, index=True)
    name = Column(String(128), nullable=False, unique=True)
    starting_balance = Column(Float, nullable=False, default=100000.0)
    cash = Column(Float, nullable=False, default=100000.0)
    buying_power = Column(Float, nullable=False, default=100000.0)
    status = Column(String(32), nullable=False, default="ACTIVE")
    created_at = Column(String(64), nullable=False, server_default=text("CURRENT_TIMESTAMP"))
    updated_at = Column(String(64), nullable=False, server_default=text("CURRENT_TIMESTAMP"))


class PaperRecommendation(Base):
    __tablename__ = "paper_recommendations"
    __table_args__ = (
        UniqueConstraint("recommendation_id", name="uq_paper_recommendations_recommendation_id"),
        Index("ix_paper_recommendations_portfolio_created_at", "paper_portfolio_id", "created_at"),
    )

    id = Column(Integer, primary_key=True, index=True)
    paper_portfolio_id = Column(Integer, ForeignKey("paper_portfolios.id"), nullable=False, index=True)
    recommendation_id = Column(String(96), nullable=False, index=True)
    symbol = Column(String(16), nullable=False, index=True)
    direction = Column(String(16), nullable=False)
    setup_type = Column(String(128), nullable=True, index=True)
    status = Column(String(32), nullable=False, default="CREATED", index=True)
    outcome = Column(String(16), nullable=False, default="UNRESOLVED", index=True)
    created_at = Column(String(64), nullable=False, index=True)
    triggered_at = Column(String(64), nullable=True)
    resolved_at = Column(String(64), nullable=True)
    model_version = Column(String(64), nullable=True, index=True)
    strategy_version = Column(String(64), nullable=True, index=True)
    confidence_tier = Column(String(32), nullable=True, index=True)
    aggression_mode = Column(String(32), nullable=True, index=True)
    overnight = Column(Boolean, nullable=False, default=False, index=True)
    dte = Column(Float, nullable=True, index=True)
    delta = Column(Float, nullable=True, index=True)
    market_regime = Column(String(64), nullable=True, index=True)
    entry_price = Column(Float, nullable=True)
    invalidation_price = Column(Float, nullable=True)
    target_1_price = Column(Float, nullable=True)
    target_2_price = Column(Float, nullable=True)
    option_contract = Column(String(160), nullable=True)
    option_entry_price = Column(Float, nullable=True)
    option_exit_price = Column(Float, nullable=True)
    realized_pnl = Column(Float, nullable=True)
    underlying_return_pct = Column(Float, nullable=True)
    option_return_pct = Column(Float, nullable=True)
    target_before_invalidation = Column(Boolean, nullable=True)
    profitable_option = Column(Boolean, nullable=True)
    directional_correct = Column(Boolean, nullable=True)
    trigger_source = Column(String(64), nullable=True)
    simulated_fill_source = Column(String(64), nullable=True)
    snapshot_version = Column(String(64), nullable=False, default="recommendation-v1")
    snapshot_json = Column(Text, nullable=False)
    outcome_json = Column(Text, nullable=True)
    created_by = Column(String(64), nullable=True)


class PaperPosition(Base):
    __tablename__ = "paper_positions"
    __table_args__ = (
        UniqueConstraint("paper_portfolio_id", "position_key", name="uq_paper_positions_portfolio_key"),
        Index("ix_paper_positions_portfolio_status", "paper_portfolio_id", "status"),
    )

    id = Column(Integer, primary_key=True, index=True)
    paper_portfolio_id = Column(Integer, ForeignKey("paper_portfolios.id"), nullable=False, index=True)
    recommendation_id = Column(String(96), nullable=True, index=True)
    position_key = Column(String(160), nullable=False)
    symbol = Column(String(16), nullable=False, index=True)
    contract_symbol = Column(String(160), nullable=True)
    direction = Column(String(16), nullable=False)
    quantity = Column(Float, nullable=False, default=0.0)
    entry_price = Column(Float, nullable=True)
    current_price = Column(Float, nullable=True)
    cost_basis = Column(Float, nullable=True)
    market_value = Column(Float, nullable=True)
    status = Column(String(32), nullable=False, default="OPEN", index=True)
    simulated_fill_source = Column(String(64), nullable=False, default="PAPER_SIMULATION")
    model_version = Column(String(64), nullable=True)
    strategy_version = Column(String(64), nullable=True)
    opened_at = Column(String(64), nullable=True)
    closed_at = Column(String(64), nullable=True)
    realized_pnl = Column(Float, nullable=True)
    payload_json = Column(Text, nullable=True)
    created_at = Column(String(64), nullable=False, server_default=text("CURRENT_TIMESTAMP"))
    updated_at = Column(String(64), nullable=False)


class PaperOrder(Base):
    __tablename__ = "paper_orders"
    __table_args__ = (Index("ix_paper_orders_portfolio_created_at", "paper_portfolio_id", "created_at"),)

    id = Column(Integer, primary_key=True, index=True)
    paper_portfolio_id = Column(Integer, ForeignKey("paper_portfolios.id"), nullable=False, index=True)
    recommendation_id = Column(String(96), nullable=True, index=True)
    order_id = Column(String(160), nullable=False, unique=True)
    symbol = Column(String(16), nullable=False)
    contract_symbol = Column(String(160), nullable=True)
    side = Column(String(32), nullable=False)
    quantity = Column(Float, nullable=False)
    limit_price = Column(Float, nullable=True)
    status = Column(String(32), nullable=False, default="SIMULATED")
    simulated_fill_source = Column(String(64), nullable=False, default="PAPER_SIMULATION")
    model_version = Column(String(64), nullable=True)
    strategy_version = Column(String(64), nullable=True)
    created_at = Column(String(64), nullable=False, server_default=text("CURRENT_TIMESTAMP"))


class PaperFill(Base):
    __tablename__ = "paper_fills"
    __table_args__ = (Index("ix_paper_fills_portfolio_created_at", "paper_portfolio_id", "created_at"),)

    id = Column(Integer, primary_key=True, index=True)
    paper_portfolio_id = Column(Integer, ForeignKey("paper_portfolios.id"), nullable=False, index=True)
    recommendation_id = Column(String(96), nullable=True, index=True)
    order_id = Column(String(160), nullable=False, index=True)
    fill_id = Column(String(160), nullable=False, unique=True)
    symbol = Column(String(16), nullable=False)
    quantity = Column(Float, nullable=False)
    fill_price = Column(Float, nullable=False)
    simulated_fill_source = Column(String(64), nullable=False, default="PAPER_SIMULATION")
    model_version = Column(String(64), nullable=True)
    strategy_version = Column(String(64), nullable=True)
    created_at = Column(String(64), nullable=False, server_default=text("CURRENT_TIMESTAMP"))


class PaperPortfolioEvent(Base):
    __tablename__ = "paper_portfolio_events"
    __table_args__ = (Index("ix_paper_portfolio_events_portfolio_created_at", "paper_portfolio_id", "created_at"),)

    id = Column(Integer, primary_key=True, index=True)
    paper_portfolio_id = Column(Integer, ForeignKey("paper_portfolios.id"), nullable=False, index=True)
    event_type = Column(String(64), nullable=False)
    recommendation_id = Column(String(96), nullable=True, index=True)
    details_json = Column(Text, nullable=False, default="{}")
    created_at = Column(String(64), nullable=False, server_default=text("CURRENT_TIMESTAMP"))


class PaperPerformanceSnapshot(Base):
    __tablename__ = "paper_performance_snapshots"
    __table_args__ = (Index("ix_paper_performance_snapshots_portfolio_created_at", "paper_portfolio_id", "created_at"),)

    id = Column(Integer, primary_key=True, index=True)
    paper_portfolio_id = Column(Integer, ForeignKey("paper_portfolios.id"), nullable=False, index=True)
    equity = Column(Float, nullable=True)
    cash = Column(Float, nullable=True)
    realized_pnl = Column(Float, nullable=True)
    unrealized_pnl = Column(Float, nullable=True)
    payload_json = Column(Text, nullable=False, default="{}")
    created_at = Column(String(64), nullable=False, server_default=text("CURRENT_TIMESTAMP"))


class PaperMigrationReview(Base):
    __tablename__ = "paper_migration_review"
    __table_args__ = (Index("ix_paper_migration_review_status_created_at", "status", "created_at"),)

    id = Column(Integer, primary_key=True, index=True)
    source_table = Column(String(96), nullable=False)
    source_record_id = Column(String(160), nullable=False)
    reason = Column(Text, nullable=False)
    status = Column(String(24), nullable=False, default="REVIEW")
    details_json = Column(Text, nullable=False, default="{}")
    created_at = Column(String(64), nullable=False, server_default=text("CURRENT_TIMESTAMP"))


class HistoricalSetupFeature(Base):
    __tablename__ = "historical_setup_features"
    __table_args__ = (
        UniqueConstraint("symbol", "interval", "timestamp", "feature_version", name="uq_historical_setup_feature"),
        Index("ix_historical_setup_features_symbol_interval_timestamp", "symbol", "interval", "timestamp"),
        Index("ix_historical_setup_features_family_direction", "setup_family", "direction"),
    )

    id = Column(Integer, primary_key=True, index=True)
    symbol = Column(String(16), nullable=False, index=True)
    interval = Column(String(16), nullable=False, index=True)
    timestamp = Column(Integer, nullable=False, index=True)
    feature_version = Column(String(64), nullable=False, index=True)
    setup_family = Column(String(96), nullable=True, index=True)
    direction = Column(String(16), nullable=True, index=True)
    setup_state = Column(String(32), nullable=True)
    data_quality = Column(String(32), nullable=True)
    features_json = Column(Text, nullable=False)
    created_at = Column(String(64), nullable=False, server_default=text("CURRENT_TIMESTAMP"))
    updated_at = Column(String(64), nullable=False, server_default=text("CURRENT_TIMESTAMP"))


class HistoricalSetupOutcome(Base):
    __tablename__ = "historical_setup_outcomes"
    __table_args__ = (
        UniqueConstraint("feature_id", "outcome_version", "horizon", name="uq_historical_setup_outcome"),
        Index("ix_historical_setup_outcomes_feature_horizon", "feature_id", "horizon"),
    )

    id = Column(Integer, primary_key=True, index=True)
    feature_id = Column(Integer, ForeignKey("historical_setup_features.id"), nullable=False, index=True)
    outcome_version = Column(String(64), nullable=False, index=True)
    horizon = Column(String(32), nullable=False, index=True)
    forward_return_pct = Column(Float, nullable=True)
    mfe_pct = Column(Float, nullable=True)
    mae_pct = Column(Float, nullable=True)
    time_to_mfe_minutes = Column(Integer, nullable=True)
    time_to_mae_minutes = Column(Integer, nullable=True)
    target_1_reached = Column(Boolean, nullable=False, default=False)
    target_2_reached = Column(Boolean, nullable=False, default=False)
    invalidation_reached = Column(Boolean, nullable=False, default=False)
    target_1_before_invalidation = Column(Boolean, nullable=False, default=False)
    target_2_before_invalidation = Column(Boolean, nullable=False, default=False)
    invalidation_before_target = Column(Boolean, nullable=False, default=False)
    directional_outcome = Column(String(32), nullable=True)
    profitable_after_costs = Column(Boolean, nullable=False, default=False)
    outcome_json = Column(Text, nullable=False)
    created_at = Column(String(64), nullable=False, server_default=text("CURRENT_TIMESTAMP"))
    updated_at = Column(String(64), nullable=False, server_default=text("CURRENT_TIMESTAMP"))


class HistoricalSetupFamily(Base):
    __tablename__ = "historical_setup_families"
    __table_args__ = (
        UniqueConstraint("setup_name", "setup_version", name="uq_historical_setup_family"),
    )

    id = Column(Integer, primary_key=True, index=True)
    setup_name = Column(String(96), nullable=False, index=True)
    setup_version = Column(String(64), nullable=False, index=True)
    direction = Column(String(16), nullable=True, index=True)
    definition_json = Column(Text, nullable=False)
    stats_json = Column(Text, nullable=False)
    sample_size = Column(Integer, nullable=False, default=0)
    confidence = Column(String(32), nullable=True)
    last_recalculated_at = Column(String(64), nullable=False, server_default=text("CURRENT_TIMESTAMP"))
    created_at = Column(String(64), nullable=False, server_default=text("CURRENT_TIMESTAMP"))
    updated_at = Column(String(64), nullable=False, server_default=text("CURRENT_TIMESTAMP"))


class TickerProfile(Base):
    __tablename__ = "ticker_profiles"
    __table_args__ = (
        Index("ix_ticker_profiles_sector_industry", "sector", "industry"),
    )

    symbol = Column(String(16), primary_key=True, index=True)
    company_name = Column(String(128), nullable=True)
    exchange = Column(String(64), nullable=True)
    sector = Column(String(96), nullable=True, index=True)
    industry = Column(String(128), nullable=True)
    benchmark = Column(String(16), nullable=True, default="SPY")
    sector_etf = Column(String(16), nullable=True)
    market_cap = Column(Float, nullable=True)
    average_daily_volume = Column(Float, nullable=True)
    average_dollar_volume = Column(Float, nullable=True)
    beta = Column(Float, nullable=True)
    volatility_profile = Column(String(32), nullable=True)
    profile_status = Column(String(32), nullable=False, default="CREATED", index=True)
    profile_state = Column(String(32), nullable=True, index=True)
    planning_ready = Column(Boolean, nullable=True, default=False)
    live_ready = Column(Boolean, nullable=True, default=False)
    completeness_percentage = Column(Float, nullable=True)
    completeness_json = Column(Text, nullable=False, default="{}")
    missing_components_json = Column(Text, nullable=False, default="[]")
    blocking_components_json = Column(Text, nullable=False, default="[]")
    stale_components_json = Column(Text, nullable=False, default="[]")
    last_completeness_check = Column(String(64), nullable=True)
    next_required_job = Column(String(128), nullable=True)
    profile_version = Column(String(64), nullable=True)
    data_coverage_json = Column(Text, nullable=False, default="{}")
    personality_json = Column(Text, nullable=False, default="[]")
    stats_json = Column(Text, nullable=False, default="{}")
    latest_setup_state_json = Column(Text, nullable=False, default="{}")
    last_backfill_requested_at = Column(String(64), nullable=True)
    last_profile_update_at = Column(String(64), nullable=True)
    created_at = Column(String(64), nullable=False, server_default=text("CURRENT_TIMESTAMP"))
    updated_at = Column(String(64), nullable=False, server_default=text("CURRENT_TIMESTAMP"))


class TickerProfileUpdate(Base):
    __tablename__ = "ticker_profile_updates"
    __table_args__ = (
        Index("ix_ticker_profile_updates_symbol_created_at", "symbol", "created_at"),
    )

    id = Column(Integer, primary_key=True, index=True)
    symbol = Column(String(16), ForeignKey("ticker_profiles.symbol"), nullable=False, index=True)
    update_type = Column(String(64), nullable=False)
    status = Column(String(32), nullable=False)
    message = Column(Text, nullable=True)
    rows_added = Column(Integer, nullable=False, default=0)
    source = Column(String(64), nullable=True)
    payload_json = Column(Text, nullable=True)
    created_at = Column(String(64), nullable=False, server_default=text("CURRENT_TIMESTAMP"))


class TickerProfileStat(Base):
    __tablename__ = "ticker_profile_stats"
    __table_args__ = (
        UniqueConstraint("symbol", "stat_type", "stat_key", "version", name="uq_ticker_profile_stat"),
        Index("ix_ticker_profile_stats_symbol_type", "symbol", "stat_type"),
    )

    id = Column(Integer, primary_key=True, index=True)
    symbol = Column(String(16), ForeignKey("ticker_profiles.symbol"), nullable=False, index=True)
    stat_type = Column(String(64), nullable=False, index=True)
    stat_key = Column(String(128), nullable=False, index=True)
    version = Column(String(64), nullable=False, default="v1")
    sample_size = Column(Integer, nullable=False, default=0)
    date_start = Column(String(64), nullable=True)
    date_end = Column(String(64), nullable=True)
    confidence = Column(String(32), nullable=True)
    value_json = Column(Text, nullable=False)
    recalculated_at = Column(String(64), nullable=False, server_default=text("CURRENT_TIMESTAMP"))
    created_at = Column(String(64), nullable=False, server_default=text("CURRENT_TIMESTAMP"))
    updated_at = Column(String(64), nullable=False, server_default=text("CURRENT_TIMESTAMP"))


class AdvisorySetting(Base):
    __tablename__ = "advisory_settings"

    key = Column(String(96), primary_key=True)
    value = Column(Text, nullable=False)
    updated_at = Column(String(64), nullable=False, server_default=text("CURRENT_TIMESTAMP"))
    updated_by = Column(String(64), nullable=True)


class AdvisoryCache(Base):
    __tablename__ = "advisory_cache"
    __table_args__ = (
        UniqueConstraint("cache_key", name="uq_advisory_cache_key"),
        Index("ix_advisory_cache_symbol_created_at", "symbol", "created_at"),
    )

    id = Column(Integer, primary_key=True, index=True)
    cache_key = Column(String(128), nullable=False)
    symbol = Column(String(16), nullable=False, index=True)
    candidate_id = Column(String(128), nullable=True)
    setup_version = Column(String(64), nullable=True)
    market_data_version = Column(String(128), nullable=True)
    option_chain_version = Column(String(128), nullable=True)
    news_version = Column(String(128), nullable=True)
    analysis_version = Column(String(64), nullable=False)
    model = Column(String(96), nullable=False)
    prompt_version = Column(String(64), nullable=False)
    input_hash = Column(String(128), nullable=False, index=True)
    advisory_json = Column(Text, nullable=False)
    deterministic_fallback = Column(Boolean, nullable=False, default=False)
    validation_status = Column(String(32), nullable=False, default="VALID")
    created_at = Column(String(64), nullable=False, server_default=text("CURRENT_TIMESTAMP"))


class TradeReviewAccount(Base):
    __tablename__ = "trade_review_accounts"
    __table_args__ = (
        Index("ix_trade_review_accounts_last_successful_sync_at", "last_successful_sync_at"),
    )

    account_ref = Column(String(64), primary_key=True)
    account_id_key = Column(String(64), nullable=False, unique=True)
    account_mask = Column(String(32), nullable=False)
    account_desc = Column(String(128), nullable=True)
    account_name = Column(String(128), nullable=True)
    account_type = Column(String(64), nullable=True)
    account_mode = Column(String(32), nullable=True)
    institution_type = Column(String(64), nullable=True)
    last_successful_sync_at = Column(String(64), nullable=True)
    oldest_available_history_at = Column(String(64), nullable=True)
    last_sync_status = Column(String(32), nullable=True)
    last_error_message = Column(Text, nullable=True)
    imported_at = Column(String(64), nullable=False, server_default=text("CURRENT_TIMESTAMP"))
    updated_at = Column(String(64), nullable=False, server_default=text("CURRENT_TIMESTAMP"))


class TradeReviewSelection(Base):
    __tablename__ = "trade_review_selections"

    username = Column(String(64), ForeignKey("auth_users.username"), primary_key=True)
    selection_mode = Column(String(16), nullable=False, default="EXPLICIT")
    selected_account_refs = Column(Text, nullable=False, default="[]")
    updated_at = Column(String(64), nullable=False, server_default=text("CURRENT_TIMESTAMP"))
    created_at = Column(String(64), nullable=False, server_default=text("CURRENT_TIMESTAMP"))


class TradeReviewSyncRun(Base):
    __tablename__ = "trade_review_sync_runs"
    __table_args__ = (
        Index("ix_trade_review_sync_runs_status_started_at", "status", "started_at"),
    )

    id = Column(Integer, primary_key=True, index=True)
    username = Column(String(64), nullable=True, index=True)
    selection_mode = Column(String(16), nullable=False, default="EXPLICIT")
    selected_account_refs = Column(Text, nullable=False, default="[]")
    status = Column(String(32), nullable=False, index=True)
    from_date = Column(String(16), nullable=True)
    to_date = Column(String(16), nullable=True)
    accounts_total = Column(Integer, nullable=False, default=0)
    accounts_completed = Column(Integer, nullable=False, default=0)
    accounts_failed = Column(Integer, nullable=False, default=0)
    transactions_imported = Column(Integer, nullable=False, default=0)
    orders_imported = Column(Integer, nullable=False, default=0)
    fills_imported = Column(Integer, nullable=False, default=0)
    trades_reconstructed = Column(Integer, nullable=False, default=0)
    unresolved_fills = Column(Integer, nullable=False, default=0)
    errors_count = Column(Integer, nullable=False, default=0)
    current_account_ref = Column(String(64), nullable=True, index=True)
    current_stage = Column(String(64), nullable=True)
    current_message = Column(Text, nullable=True)
    last_error = Column(Text, nullable=True)
    started_at = Column(String(64), nullable=False, server_default=text("CURRENT_TIMESTAMP"))
    finished_at = Column(String(64), nullable=True)
    created_at = Column(String(64), nullable=False, server_default=text("CURRENT_TIMESTAMP"))
    updated_at = Column(String(64), nullable=False, server_default=text("CURRENT_TIMESTAMP"))


class TradeReviewFill(Base):
    __tablename__ = "trade_review_fills"
    __table_args__ = (
        UniqueConstraint("source_hash", name="uq_trade_review_fills_source_hash"),
        Index("ix_trade_review_fills_account_contract_time", "account_ref", "occ_symbol", "execution_timestamp_utc"),
        Index("ix_trade_review_fills_account_source", "account_ref", "source_type", "source_record_id"),
    )

    id = Column(Integer, primary_key=True, index=True)
    account_ref = Column(String(64), nullable=False, index=True)
    account_id_key = Column(String(64), nullable=False, index=True)
    account_mask = Column(String(32), nullable=False)
    source_type = Column(String(32), nullable=False)
    source_record_id = Column(String(128), nullable=True)
    order_id = Column(String(128), nullable=True, index=True)
    execution_id = Column(String(128), nullable=True, index=True)
    parent_order_id = Column(String(128), nullable=True)
    execution_timestamp_utc = Column(String(64), nullable=True, index=True)
    execution_timestamp_et = Column(String(64), nullable=True)
    underlying_symbol = Column(String(16), nullable=True, index=True)
    occ_symbol = Column(String(128), nullable=True, index=True)
    option_symbol = Column(String(128), nullable=True)
    call_put = Column(String(8), nullable=True)
    strike = Column(Float, nullable=True)
    expiration = Column(String(16), nullable=True, index=True)
    dte_at_entry = Column(Integer, nullable=True)
    action = Column(String(8), nullable=True, index=True)
    quantity = Column(Integer, nullable=False, default=0)
    fill_price = Column(Float, nullable=True)
    commission = Column(Float, nullable=True)
    fees = Column(Float, nullable=True)
    net_cash_effect = Column(Float, nullable=True)
    bid = Column(Float, nullable=True)
    ask = Column(Float, nullable=True)
    midpoint = Column(Float, nullable=True)
    spread_pct = Column(Float, nullable=True)
    underlying_price = Column(Float, nullable=True)
    quote_source = Column(String(32), nullable=True)
    data_status = Column(String(16), nullable=False, default="observed")
    confidence_level = Column(String(16), nullable=False, default="LOW")
    match_status = Column(String(16), nullable=False, default="UNRESOLVED")
    raw_payload_json = Column(Text, nullable=True)
    source_hash = Column(String(64), nullable=False)
    created_at = Column(String(64), nullable=False, server_default=text("CURRENT_TIMESTAMP"))
    updated_at = Column(String(64), nullable=False, server_default=text("CURRENT_TIMESTAMP"))


class TradeReviewTrade(Base):
    __tablename__ = "trade_review_trades"
    __table_args__ = (
        UniqueConstraint("trade_key", name="uq_trade_review_trades_trade_key"),
        Index("ix_trade_review_trades_account_status", "account_ref", "status"),
        Index("ix_trade_review_trades_underlying_expiration", "underlying_symbol", "expiration"),
    )

    id = Column(Integer, primary_key=True, index=True)
    trade_key = Column(String(128), nullable=False)
    account_ref = Column(String(64), nullable=False, index=True)
    account_id_key = Column(String(64), nullable=False, index=True)
    account_mask = Column(String(32), nullable=False)
    underlying_symbol = Column(String(16), nullable=True, index=True)
    occ_symbol = Column(String(128), nullable=True, index=True)
    option_symbol = Column(String(128), nullable=True)
    call_put = Column(String(8), nullable=True)
    strike = Column(Float, nullable=True)
    expiration = Column(String(16), nullable=True, index=True)
    dte_at_entry = Column(Integer, nullable=True)
    direction = Column(String(8), nullable=True)
    setup_type = Column(String(32), nullable=True, index=True)
    total_quantity = Column(Integer, nullable=False, default=0)
    open_fill_ids_json = Column(Text, nullable=False, default="[]")
    close_fill_ids_json = Column(Text, nullable=False, default="[]")
    opening_timestamp_utc = Column(String(64), nullable=True, index=True)
    closing_timestamp_utc = Column(String(64), nullable=True, index=True)
    opening_timestamp_et = Column(String(64), nullable=True)
    closing_timestamp_et = Column(String(64), nullable=True)
    average_entry_price = Column(Float, nullable=True)
    average_exit_price = Column(Float, nullable=True)
    realized_pnl = Column(Float, nullable=True)
    return_on_premium = Column(Float, nullable=True)
    total_fees = Column(Float, nullable=True)
    holding_seconds = Column(Integer, nullable=True)
    maximum_capital_at_risk = Column(Float, nullable=True)
    expiration_outcome = Column(String(32), nullable=True)
    assignment_outcome = Column(String(32), nullable=True)
    exercise_outcome = Column(String(32), nullable=True)
    confidence_level = Column(String(16), nullable=False, default="LOW")
    data_confidence_label = Column(String(16), nullable=False, default="LOW")
    data_confidence_score = Column(Float, nullable=True)
    status = Column(String(16), nullable=False, default="COMPLETE", index=True)
    grade = Column(String(2), nullable=True)
    grade_breakdown_json = Column(Text, nullable=True)
    what_went_well = Column(Text, nullable=True)
    what_went_poorly = Column(Text, nullable=True)
    hard_truth = Column(Text, nullable=True)
    should_have_been_skipped = Column(Boolean, default=False, nullable=False)
    better_entry = Column(Text, nullable=True)
    better_invalidation = Column(Text, nullable=True)
    better_stop_plan = Column(Text, nullable=True)
    better_contract_profile = Column(Text, nullable=True)
    better_exit_plan = Column(Text, nullable=True)
    lesson = Column(Text, nullable=True)
    admin_notes = Column(Text, nullable=True)
    missing_data_json = Column(Text, nullable=True)
    pattern_tags_json = Column(Text, nullable=True)
    market_context_json = Column(Text, nullable=True)
    analysis_status = Column(String(16), nullable=False, default="PENDING")
    analysis_version = Column(Integer, nullable=False, default=1)
    data_version = Column(String(64), nullable=True, index=True)
    reviewed = Column(Boolean, default=False, nullable=False)
    reviewed_at = Column(String(64), nullable=True)
    created_at = Column(String(64), nullable=False, server_default=text("CURRENT_TIMESTAMP"))
    updated_at = Column(String(64), nullable=False, server_default=text("CURRENT_TIMESTAMP"))


class TradeReviewAnalysisCache(Base):
    __tablename__ = "trade_review_analysis_cache"
    __table_args__ = (
        UniqueConstraint("trade_id", "analysis_version", "data_version", name="uq_trade_review_analysis_cache"),
        Index("ix_trade_review_analysis_cache_trade_version", "trade_id", "analysis_version", "data_version"),
    )

    id = Column(Integer, primary_key=True, index=True)
    trade_id = Column(Integer, ForeignKey("trade_review_trades.id"), nullable=False, index=True)
    analysis_version = Column(Integer, nullable=False, default=1)
    data_version = Column(String(64), nullable=False)
    model = Column(String(64), nullable=True)
    analysis_json = Column(Text, nullable=False)
    created_at = Column(String(64), nullable=False, server_default=text("CURRENT_TIMESTAMP"))


class NewsCatalystSnapshot(Base):
    __tablename__ = "news_catalyst_snapshots"
    __table_args__ = (
        Index("ix_news_catalyst_snapshots_symbol_context_updated_at", "symbol", "context_type", "updated_at"),
    )

    key = Column(String(256), primary_key=True)
    symbol = Column(String(16), nullable=False, index=True)
    context_type = Column(String(32), nullable=False, index=True)
    payload_json = Column(Text, nullable=False)
    created_at = Column(String(64), nullable=False, server_default=text("CURRENT_TIMESTAMP"))
    updated_at = Column(String(64), nullable=False, server_default=text("CURRENT_TIMESTAMP"))


class TradeReviewAuditLog(Base):
    __tablename__ = "trade_review_audit_logs"
    __table_args__ = (
        Index("ix_trade_review_audit_logs_username_created_at", "username", "created_at"),
    )

    id = Column(Integer, primary_key=True, index=True)
    username = Column(String(64), nullable=False, index=True)
    action = Column(String(64), nullable=False)
    resource_type = Column(String(64), nullable=True)
    resource_id = Column(String(128), nullable=True)
    detail = Column(Text, nullable=True)
    created_at = Column(String(64), nullable=False, server_default=text("CURRENT_TIMESTAMP"))


class AuthUser(Base):
    __tablename__ = "auth_users"
    __table_args__ = (
        Index("ix_auth_users_role_active", "role", "active"),
    )

    username = Column(String(64), primary_key=True)
    password_hash = Column(Text, nullable=False)
    role = Column(String(16), nullable=False, default="user")
    active = Column(Boolean, default=True, nullable=False)
    must_change_password = Column(Boolean, default=False, nullable=False)
    created_at = Column(String(64), nullable=False, server_default=text("CURRENT_TIMESTAMP"))
    updated_at = Column(String(64), nullable=False, server_default=text("CURRENT_TIMESTAMP"))
    last_login_at = Column(String(64), nullable=True)


class AuthSession(Base):
    __tablename__ = "auth_sessions"
    __table_args__ = (
        Index("ix_auth_sessions_token_hash", "token_hash"),
        Index("ix_auth_sessions_username_expires_at", "username", "expires_at"),
    )

    id = Column(Integer, primary_key=True, index=True)
    username = Column(String(64), ForeignKey("auth_users.username"), nullable=False, index=True)
    token_hash = Column(String(128), nullable=False, unique=True)
    ip_address = Column(String(64), nullable=True)
    user_agent = Column(Text, nullable=True)
    created_at = Column(String(64), nullable=False, server_default=text("CURRENT_TIMESTAMP"))
    last_seen_at = Column(String(64), nullable=False, server_default=text("CURRENT_TIMESTAMP"))
    expires_at = Column(String(64), nullable=False)
    revoked_at = Column(String(64), nullable=True)


class AuthLoginAttempt(Base):
    __tablename__ = "auth_login_attempts"
    __table_args__ = (
        Index("ix_auth_login_attempts_ip_created_at", "ip_address", "created_at"),
        Index("ix_auth_login_attempts_username_created_at", "username", "created_at"),
    )

    id = Column(Integer, primary_key=True, index=True)
    ip_address = Column(String(64), nullable=False, index=True)
    username = Column(String(64), nullable=True, index=True)
    success = Column(Boolean, default=False, nullable=False)
    reason = Column(Text, nullable=True)
    created_at = Column(String(64), nullable=False, server_default=text("CURRENT_TIMESTAMP"))


class AuthIpBlock(Base):
    __tablename__ = "auth_ip_blocks"
    __table_args__ = (
        Index("ix_auth_ip_blocks_blocked_at", "blocked_at"),
    )

    ip_address = Column(String(64), primary_key=True)
    reason = Column(Text, nullable=False)
    fail_count = Column(Integer, nullable=False, default=0)
    blocked_at = Column(String(64), nullable=False, server_default=text("CURRENT_TIMESTAMP"))
    updated_at = Column(String(64), nullable=False, server_default=text("CURRENT_TIMESTAMP"))
    last_failed_at = Column(String(64), nullable=True)
