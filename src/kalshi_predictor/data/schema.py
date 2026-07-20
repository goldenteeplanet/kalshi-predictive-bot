from datetime import datetime

from sqlalchemy import DateTime, ForeignKey, Index, Integer, String, Text, UniqueConstraint
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column


class Base(DeclarativeBase):
    pass


class Market(Base):
    __tablename__ = "markets"

    ticker: Mapped[str] = mapped_column(String(128), primary_key=True)
    event_ticker: Mapped[str | None] = mapped_column(String(128))
    series_ticker: Mapped[str | None] = mapped_column(String(128))
    title: Mapped[str | None] = mapped_column(String(500))
    subtitle: Mapped[str | None] = mapped_column(String(500))
    market_type: Mapped[str | None] = mapped_column(String(100))
    status: Mapped[str | None] = mapped_column(String(100), index=True)
    result: Mapped[str | None] = mapped_column(String(100), index=True)
    open_time: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    close_time: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), index=True)
    expected_expiration_time: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    expiration_time: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    settlement_ts: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), index=True)
    settlement_value_dollars: Mapped[str | None] = mapped_column(String(80))
    volume_fp: Mapped[str | None] = mapped_column(String(80))
    open_interest_fp: Mapped[str | None] = mapped_column(String(80))
    liquidity_dollars: Mapped[str | None] = mapped_column(String(80))
    rules_primary: Mapped[str | None] = mapped_column(Text)
    rules_secondary: Mapped[str | None] = mapped_column(Text)
    raw_json: Mapped[str] = mapped_column(Text, nullable=False)
    first_seen_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    last_seen_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)


class MarketLeg(Base):
    __tablename__ = "market_legs"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    ticker: Mapped[str] = mapped_column(ForeignKey("markets.ticker"), index=True)
    leg_index: Mapped[int] = mapped_column(Integer, nullable=False)
    parsed_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, index=True)
    side: Mapped[str] = mapped_column(String(20), nullable=False, index=True)
    category: Mapped[str] = mapped_column(String(60), nullable=False, index=True)
    market_type: Mapped[str] = mapped_column(String(100), nullable=False, index=True)
    entity_name: Mapped[str | None] = mapped_column(String(300))
    operator: Mapped[str] = mapped_column(String(50), nullable=False, index=True)
    threshold_value: Mapped[str | None] = mapped_column(String(100))
    unit: Mapped[str | None] = mapped_column(String(50))
    confidence: Mapped[str] = mapped_column(String(80), nullable=False)
    raw_text: Mapped[str] = mapped_column(Text, nullable=False)
    reason: Mapped[str] = mapped_column(Text, nullable=False)
    raw_json: Mapped[str] = mapped_column(Text, nullable=False)

    __table_args__ = (
        UniqueConstraint("ticker", "leg_index", name="uq_market_legs_ticker_index"),
        Index("ix_market_legs_category_ticker", "category", "ticker"),
    )


class MarketSnapshot(Base):
    __tablename__ = "market_snapshots"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    ticker: Mapped[str] = mapped_column(ForeignKey("markets.ticker"), index=True)
    captured_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        index=True,
    )
    status: Mapped[str | None] = mapped_column(String(100), index=True)
    yes_bid_dollars: Mapped[str | None] = mapped_column(String(80))
    yes_ask_dollars: Mapped[str | None] = mapped_column(String(80))
    no_bid_dollars: Mapped[str | None] = mapped_column(String(80))
    no_ask_dollars: Mapped[str | None] = mapped_column(String(80))
    best_yes_bid: Mapped[str | None] = mapped_column(String(80))
    best_yes_ask: Mapped[str | None] = mapped_column(String(80))
    best_no_bid: Mapped[str | None] = mapped_column(String(80))
    best_no_ask: Mapped[str | None] = mapped_column(String(80))
    spread: Mapped[str | None] = mapped_column(String(80))
    last_price_dollars: Mapped[str | None] = mapped_column(String(80))
    volume_fp: Mapped[str | None] = mapped_column(String(80))
    volume_24h_fp: Mapped[str | None] = mapped_column(String(80))
    open_interest_fp: Mapped[str | None] = mapped_column(String(80))
    raw_market_json: Mapped[str] = mapped_column(Text, nullable=False)
    raw_orderbook_json: Mapped[str | None] = mapped_column(Text)

    __table_args__ = (Index("ix_market_snapshots_ticker_captured", "ticker", "captured_at"),)


class Forecast(Base):
    __tablename__ = "forecasts"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    ticker: Mapped[str] = mapped_column(ForeignKey("markets.ticker"), index=True)
    forecasted_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        index=True,
    )
    model_name: Mapped[str] = mapped_column(String(100), nullable=False, index=True)
    yes_probability: Mapped[str] = mapped_column(String(80), nullable=False)
    market_mid_probability: Mapped[str | None] = mapped_column(String(80))
    best_yes_bid: Mapped[str | None] = mapped_column(String(80))
    best_yes_ask: Mapped[str | None] = mapped_column(String(80))
    feature_json: Mapped[str] = mapped_column(Text, nullable=False)
    notes: Mapped[str | None] = mapped_column(Text)

    __table_args__ = (Index("ix_forecasts_model_time", "model_name", "forecasted_at"),)


class Settlement(Base):
    __tablename__ = "settlements"

    ticker: Mapped[str] = mapped_column(ForeignKey("markets.ticker"), primary_key=True)
    settled_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), index=True)
    result: Mapped[str | None] = mapped_column(String(100), index=True)
    yes_settlement_value: Mapped[str | None] = mapped_column(String(80))
    raw_json: Mapped[str] = mapped_column(Text, nullable=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)


class PaperOrder(Base):
    __tablename__ = "paper_orders"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    ticker: Mapped[str] = mapped_column(String(128), index=True)
    forecast_id: Mapped[int | None] = mapped_column(ForeignKey("forecasts.id"), index=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        index=True,
    )
    model_name: Mapped[str] = mapped_column(String(100), nullable=False, index=True)
    side: Mapped[str] = mapped_column(String(20), nullable=False, index=True)
    probability: Mapped[str] = mapped_column(String(80), nullable=False)
    market_price: Mapped[str] = mapped_column(String(80), nullable=False)
    limit_price: Mapped[str] = mapped_column(String(80), nullable=False)
    edge: Mapped[str] = mapped_column(String(80), nullable=False)
    quantity: Mapped[int] = mapped_column(Integer, nullable=False)
    status: Mapped[str] = mapped_column(String(20), nullable=False, index=True)
    reason: Mapped[str] = mapped_column(Text, nullable=False)
    raw_decision_json: Mapped[str] = mapped_column(Text, nullable=False)

    __table_args__ = (
        UniqueConstraint(
            "ticker",
            "model_name",
            "forecast_id",
            name="uq_paper_orders_ticker_model_forecast",
        ),
    )


class PositionSizingDecisionLog(Base):
    __tablename__ = "position_sizing_decisions"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    decision_timestamp: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        index=True,
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        index=True,
    )
    version: Mapped[str] = mapped_column(String(20), nullable=False, index=True)
    mode: Mapped[str] = mapped_column(String(20), nullable=False, index=True)
    strategy_id: Mapped[str | None] = mapped_column(String(100), index=True)
    instrument: Mapped[str | None] = mapped_column(String(128), index=True)
    ticker: Mapped[str] = mapped_column(String(128), nullable=False, index=True)
    model_name: Mapped[str | None] = mapped_column(String(100), index=True)
    trade_intent_id: Mapped[str | None] = mapped_column(String(200), index=True)
    order_correlation_id: Mapped[str | None] = mapped_column(String(200), index=True)
    paper_order_id: Mapped[int | None] = mapped_column(ForeignKey("paper_orders.id"), index=True)
    tier: Mapped[str] = mapped_column(String(50), nullable=False, index=True)
    composite_score: Mapped[str] = mapped_column(String(80), nullable=False)
    proposed_contracts: Mapped[int] = mapped_column(Integer, nullable=False)
    live_candidate_contracts: Mapped[int] = mapped_column(Integer, nullable=False)
    executed_contracts: Mapped[int] = mapped_column(Integer, nullable=False)
    factor_scores_json: Mapped[str] = mapped_column(Text, nullable=False)
    factor_weights_json: Mapped[str] = mapped_column(Text, nullable=False)
    adjusted_historical_accuracy: Mapped[str] = mapped_column(String(80), nullable=False)
    historical_sample_size: Mapped[int] = mapped_column(Integer, nullable=False)
    drawdown_utilization: Mapped[str] = mapped_column(String(80), nullable=False)
    caps_json: Mapped[str] = mapped_column(Text, nullable=False)
    limiting_factors_json: Mapped[str] = mapped_column(Text, nullable=False)
    reason_codes_json: Mapped[str] = mapped_column(Text, nullable=False)
    fallback_used: Mapped[int] = mapped_column(Integer, nullable=False, default=0, index=True)
    raw_json: Mapped[str] = mapped_column(Text, nullable=False)

    __table_args__ = (
        Index("ix_position_sizing_ticker_decision", "ticker", "decision_timestamp"),
        Index("ix_position_sizing_order", "paper_order_id", "decision_timestamp"),
        Index("ix_position_sizing_mode_tier", "mode", "tier", "decision_timestamp"),
    )


class AdvancedRiskDecisionLog(Base):
    __tablename__ = "advanced_risk_decisions"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    decision_timestamp: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        index=True,
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        index=True,
    )
    version: Mapped[str] = mapped_column(String(20), nullable=False, index=True)
    mode: Mapped[str] = mapped_column(String(20), nullable=False, index=True)
    action: Mapped[str] = mapped_column(String(20), nullable=False, index=True)
    strategy_id: Mapped[str | None] = mapped_column(String(100), index=True)
    model_id: Mapped[str | None] = mapped_column(String(100), index=True)
    category_id: Mapped[str | None] = mapped_column(String(100), index=True)
    instrument_id: Mapped[str | None] = mapped_column(String(128), index=True)
    correlation_group_id: Mapped[str | None] = mapped_column(String(128), index=True)
    ticker: Mapped[str] = mapped_column(String(128), nullable=False, index=True)
    trade_intent_id: Mapped[str] = mapped_column(String(200), nullable=False, index=True)
    order_correlation_id: Mapped[str | None] = mapped_column(String(200), index=True)
    position_sizing_decision_id: Mapped[int | None] = mapped_column(
        ForeignKey("position_sizing_decisions.id"),
        index=True,
    )
    paper_order_id: Mapped[int | None] = mapped_column(ForeignKey("paper_orders.id"), index=True)
    reservation_id: Mapped[int | None] = mapped_column(Integer, index=True)
    phase_3m_tier: Mapped[str] = mapped_column(String(50), nullable=False, index=True)
    phase_3m_proposed_contracts: Mapped[int] = mapped_column(Integer, nullable=False)
    live_candidate_contracts: Mapped[int] = mapped_column(Integer, nullable=False)
    executed_contracts: Mapped[int] = mapped_column(Integer, nullable=False)
    risk_per_contract: Mapped[str] = mapped_column(String(80), nullable=False)
    planned_trade_risk: Mapped[str] = mapped_column(String(80), nullable=False)
    raw_caps_json: Mapped[str] = mapped_column(Text, nullable=False)
    bucketed_caps_json: Mapped[str] = mapped_column(Text, nullable=False)
    limiting_factors_json: Mapped[str] = mapped_column(Text, nullable=False)
    hard_blocks_json: Mapped[str] = mapped_column(Text, nullable=False)
    reason_codes_json: Mapped[str] = mapped_column(Text, nullable=False)
    fallback_used: Mapped[int] = mapped_column(Integer, nullable=False, default=0, index=True)
    raw_json: Mapped[str] = mapped_column(Text, nullable=False)

    __table_args__ = (
        Index("ix_advanced_risk_ticker_decision", "ticker", "decision_timestamp"),
        Index("ix_advanced_risk_order", "paper_order_id", "decision_timestamp"),
        Index("ix_advanced_risk_mode_action", "mode", "action", "decision_timestamp"),
    )


class AdvancedRiskReservation(Base):
    __tablename__ = "advanced_risk_reservations"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    trade_intent_id: Mapped[str] = mapped_column(String(200), nullable=False, unique=True)
    order_correlation_id: Mapped[str | None] = mapped_column(String(200), index=True)
    decision_id: Mapped[int | None] = mapped_column(
        ForeignKey("advanced_risk_decisions.id"),
        index=True,
    )
    paper_order_id: Mapped[int | None] = mapped_column(ForeignKey("paper_orders.id"), index=True)
    status: Mapped[str] = mapped_column(String(40), nullable=False, index=True)
    reserved_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        index=True,
    )
    released_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    ticker: Mapped[str] = mapped_column(String(128), nullable=False, index=True)
    model_id: Mapped[str | None] = mapped_column(String(100), index=True)
    category_id: Mapped[str | None] = mapped_column(String(100), index=True)
    instrument_id: Mapped[str | None] = mapped_column(String(128), index=True)
    correlation_group_id: Mapped[str | None] = mapped_column(String(128), index=True)
    quantity: Mapped[int] = mapped_column(Integer, nullable=False)
    risk_per_contract: Mapped[str] = mapped_column(String(80), nullable=False)
    reserved_risk: Mapped[str] = mapped_column(String(80), nullable=False)
    raw_json: Mapped[str] = mapped_column(Text, nullable=False)

    __table_args__ = (
        Index("ix_advanced_risk_reservation_status", "status", "reserved_at"),
        Index("ix_advanced_risk_reservation_bucket", "category_id", "model_id", "instrument_id"),
    )


class AdvancedRiskHighWaterMark(Base):
    __tablename__ = "advanced_risk_high_water_marks"

    account_key: Mapped[str] = mapped_column(String(100), primary_key=True)
    high_water_equity: Mapped[str] = mapped_column(String(80), nullable=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)


class PaperFill(Base):
    __tablename__ = "paper_fills"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    paper_order_id: Mapped[int] = mapped_column(ForeignKey("paper_orders.id"), index=True)
    ticker: Mapped[str] = mapped_column(String(128), index=True)
    filled_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        index=True,
    )
    side: Mapped[str] = mapped_column(String(20), nullable=False, index=True)
    price: Mapped[str] = mapped_column(String(80), nullable=False)
    quantity: Mapped[int] = mapped_column(Integer, nullable=False)
    fee: Mapped[str] = mapped_column(String(80), nullable=False, default="0")
    raw_fill_json: Mapped[str] = mapped_column(Text, nullable=False)


class PaperPosition(Base):
    __tablename__ = "paper_positions"

    ticker: Mapped[str] = mapped_column(String(128), primary_key=True)
    yes_contracts: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    no_contracts: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    avg_yes_price: Mapped[str | None] = mapped_column(String(80))
    avg_no_price: Mapped[str | None] = mapped_column(String(80))
    realized_pnl: Mapped[str] = mapped_column(String(80), nullable=False, default="0")
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)


class PaperPnl(Base):
    __tablename__ = "paper_pnl"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    ticker: Mapped[str] = mapped_column(String(128), index=True)
    calculated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        index=True,
    )
    yes_contracts: Mapped[int] = mapped_column(Integer, nullable=False)
    no_contracts: Mapped[int] = mapped_column(Integer, nullable=False)
    avg_yes_price: Mapped[str | None] = mapped_column(String(80))
    avg_no_price: Mapped[str | None] = mapped_column(String(80))
    settlement_result: Mapped[str | None] = mapped_column(String(100))
    realized_pnl: Mapped[str] = mapped_column(String(80), nullable=False)
    unrealized_pnl: Mapped[str] = mapped_column(String(80), nullable=False)
    total_pnl: Mapped[str] = mapped_column(String(80), nullable=False)
    notes: Mapped[str | None] = mapped_column(Text)


class Feature(Base):
    __tablename__ = "features"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    ticker: Mapped[str] = mapped_column(String(128), nullable=False, index=True)
    feature_set_name: Mapped[str] = mapped_column(String(100), nullable=False, index=True)
    generated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        index=True,
    )
    source_timestamp: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), index=True)
    features_json: Mapped[str] = mapped_column(Text, nullable=False)
    raw_source_json: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)

    __table_args__ = (
        Index("ix_features_ticker_set_generated", "ticker", "feature_set_name", "generated_at"),
    )


class FeatureSnapshot(Base):
    __tablename__ = "feature_snapshots"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    ticker: Mapped[str] = mapped_column(String(128), nullable=False, index=True)
    captured_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        index=True,
    )
    market_features_json: Mapped[str] = mapped_column(Text, nullable=False)
    external_features_json: Mapped[str] = mapped_column(Text, nullable=False)
    combined_features_json: Mapped[str] = mapped_column(Text, nullable=False)

    __table_args__ = (Index("ix_feature_snapshots_ticker_captured", "ticker", "captured_at"),)


class BacktestRun(Base):
    __tablename__ = "backtest_runs"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    name: Mapped[str] = mapped_column(String(200), nullable=False)
    strategy_name: Mapped[str] = mapped_column(String(100), nullable=False, index=True)
    model_name: Mapped[str] = mapped_column(String(100), nullable=False, index=True)
    started_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    start_time: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        index=True,
    )
    end_time: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, index=True)
    config_json: Mapped[str] = mapped_column(Text, nullable=False)
    summary_json: Mapped[str | None] = mapped_column(Text)
    notes: Mapped[str | None] = mapped_column(Text)


class BacktestTrade(Base):
    __tablename__ = "backtest_trades"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    backtest_run_id: Mapped[int] = mapped_column(ForeignKey("backtest_runs.id"), index=True)
    ticker: Mapped[str] = mapped_column(String(128), nullable=False, index=True)
    forecast_id: Mapped[int] = mapped_column(ForeignKey("forecasts.id"), nullable=False, index=True)
    simulated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        index=True,
    )
    side: Mapped[str] = mapped_column(String(20), nullable=False)
    price: Mapped[str] = mapped_column(String(80), nullable=False)
    quantity: Mapped[int] = mapped_column(Integer, nullable=False)
    edge: Mapped[str] = mapped_column(String(80), nullable=False)
    settlement_result: Mapped[str | None] = mapped_column(String(100))
    pnl: Mapped[str] = mapped_column(String(80), nullable=False)
    raw_decision_json: Mapped[str] = mapped_column(Text, nullable=False)

    __table_args__ = (
        UniqueConstraint(
            "backtest_run_id",
            "forecast_id",
            name="uq_backtest_trades_run_forecast",
        ),
    )


class MarketRanking(Base):
    __tablename__ = "market_rankings"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    ticker: Mapped[str] = mapped_column(String(128), nullable=False, index=True)
    ranked_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        index=True,
    )
    title: Mapped[str | None] = mapped_column(String(500))
    status: Mapped[str | None] = mapped_column(String(100), index=True)
    series_ticker: Mapped[str | None] = mapped_column(String(128))
    event_ticker: Mapped[str | None] = mapped_column(String(128))
    volume: Mapped[str | None] = mapped_column(String(80))
    open_interest: Mapped[str | None] = mapped_column(String(80))
    liquidity: Mapped[str | None] = mapped_column(String(80))
    spread: Mapped[str | None] = mapped_column(String(80))
    midpoint: Mapped[str | None] = mapped_column(String(80))
    time_to_close_minutes: Mapped[str | None] = mapped_column(String(80))
    forecast_model: Mapped[str] = mapped_column(String(100), nullable=False, index=True)
    forecast_probability: Mapped[str | None] = mapped_column(String(80))
    best_side: Mapped[str | None] = mapped_column(String(20))
    best_price: Mapped[str | None] = mapped_column(String(80))
    estimated_edge: Mapped[str | None] = mapped_column(String(80))
    liquidity_score: Mapped[str] = mapped_column(String(80), nullable=False)
    spread_score: Mapped[str] = mapped_column(String(80), nullable=False)
    time_score: Mapped[str] = mapped_column(String(80), nullable=False)
    model_confidence_score: Mapped[str] = mapped_column(String(80), nullable=False)
    opportunity_score: Mapped[str] = mapped_column(String(80), nullable=False, index=True)
    reason: Mapped[str] = mapped_column(Text, nullable=False)
    raw_json: Mapped[str] = mapped_column(Text, nullable=False)

    __table_args__ = (
        Index("ix_market_rankings_model_score", "forecast_model", "opportunity_score"),
    )


class ForecastSkipLog(Base):
    __tablename__ = "forecast_skip_log"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    model_name: Mapped[str] = mapped_column(String(100), nullable=False, index=True)
    ticker: Mapped[str] = mapped_column(String(128), nullable=False, index=True)
    skipped_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        index=True,
    )
    reason: Mapped[str] = mapped_column(Text, nullable=False)
    required_data: Mapped[str] = mapped_column(Text, nullable=False)
    available_data: Mapped[str] = mapped_column(Text, nullable=False)
    raw_json: Mapped[str] = mapped_column(Text, nullable=False)

    __table_args__ = (
        Index("ix_forecast_skip_model_ticker_time", "model_name", "ticker", "skipped_at"),
    )


class RuntimeProvenanceEvent(Base):
    __tablename__ = "runtime_provenance_events"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    event_key: Mapped[str] = mapped_column(String(160), nullable=False, unique=True)
    stage: Mapped[str] = mapped_column(String(40), nullable=False, index=True)
    forecast_id: Mapped[int] = mapped_column(ForeignKey("forecasts.id"), nullable=False, index=True)
    ranking_id: Mapped[int | None] = mapped_column(ForeignKey("market_rankings.id"), index=True)
    market_snapshot_id: Mapped[int | None] = mapped_column(
        ForeignKey("market_snapshots.id"), index=True
    )
    ticker: Mapped[str] = mapped_column(String(128), nullable=False, index=True)
    model_name: Mapped[str] = mapped_column(String(100), nullable=False, index=True)
    model_version: Mapped[str] = mapped_column(String(40), nullable=False)
    source_observation_ref_json: Mapped[str | None] = mapped_column(Text)
    feature_source_table: Mapped[str | None] = mapped_column(String(100))
    feature_source_id: Mapped[int | None] = mapped_column(Integer)
    event_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, index=True)
    previous_digest: Mapped[str] = mapped_column(String(64), nullable=False)
    provenance_digest: Mapped[str] = mapped_column(String(64), nullable=False, unique=True)
    raw_json: Mapped[str] = mapped_column(Text, nullable=False)

    __table_args__ = (
        UniqueConstraint("stage", "forecast_id", "ranking_id",
                         name="uq_runtime_provenance_stage_forecast_ranking"),
        Index("ix_runtime_provenance_forecast_event", "forecast_id", "event_at"),
    )


class MarketOpportunity(Base):
    __tablename__ = "market_opportunities"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    ticker: Mapped[str] = mapped_column(String(128), nullable=False, index=True)
    detected_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        index=True,
    )
    model_name: Mapped[str] = mapped_column(String(100), nullable=False, index=True)
    side: Mapped[str] = mapped_column(String(20), nullable=False)
    price: Mapped[str] = mapped_column(String(80), nullable=False)
    forecast_probability: Mapped[str] = mapped_column(String(80), nullable=False)
    estimated_edge: Mapped[str] = mapped_column(String(80), nullable=False)
    opportunity_score: Mapped[str] = mapped_column(String(80), nullable=False, index=True)
    status: Mapped[str] = mapped_column(String(50), nullable=False, index=True)
    reason: Mapped[str] = mapped_column(Text, nullable=False)
    raw_json: Mapped[str] = mapped_column(Text, nullable=False)


class ModelLeaderboard(Base):
    __tablename__ = "model_leaderboard"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    model_name: Mapped[str] = mapped_column(String(100), nullable=False, index=True)
    generated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        index=True,
    )
    forecast_count: Mapped[int] = mapped_column(Integer, nullable=False)
    evaluated_forecast_count: Mapped[int] = mapped_column(Integer, nullable=False)
    paper_trade_count: Mapped[int] = mapped_column(Integer, nullable=False)
    settled_trade_count: Mapped[int] = mapped_column(Integer, nullable=False)
    brier_score: Mapped[str | None] = mapped_column(String(80))
    log_loss: Mapped[str | None] = mapped_column(String(80))
    win_rate: Mapped[str | None] = mapped_column(String(80))
    total_pnl: Mapped[str | None] = mapped_column(String(80))
    roi_on_exposure: Mapped[str | None] = mapped_column(String(80))
    avg_edge: Mapped[str | None] = mapped_column(String(80))
    max_drawdown: Mapped[str | None] = mapped_column(String(80))
    notes: Mapped[str] = mapped_column(Text, nullable=False)
    raw_json: Mapped[str] = mapped_column(Text, nullable=False)

    __table_args__ = (Index("ix_model_leaderboard_generated_model", "generated_at", "model_name"),)


class CryptoPrice(Base):
    __tablename__ = "crypto_prices"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    symbol: Mapped[str] = mapped_column(String(20), nullable=False, index=True)
    source: Mapped[str] = mapped_column(String(100), nullable=False, index=True)
    observed_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        index=True,
    )
    price_usd: Mapped[str] = mapped_column(String(80), nullable=False)
    volume_24h: Mapped[str | None] = mapped_column(String(80))
    market_cap: Mapped[str | None] = mapped_column(String(80))
    raw_json: Mapped[str] = mapped_column(Text, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)

    __table_args__ = (Index("ix_crypto_prices_symbol_observed", "symbol", "observed_at"),)


class CryptoFeature(Base):
    __tablename__ = "crypto_features"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    symbol: Mapped[str] = mapped_column(String(20), nullable=False, index=True)
    source: Mapped[str] = mapped_column(String(100), nullable=False, index=True)
    generated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        index=True,
    )
    window_minutes: Mapped[int] = mapped_column(Integer, nullable=False)
    price: Mapped[str | None] = mapped_column(String(80))
    return_5m: Mapped[str | None] = mapped_column(String(80))
    return_15m: Mapped[str | None] = mapped_column(String(80))
    return_1h: Mapped[str | None] = mapped_column(String(80))
    return_4h: Mapped[str | None] = mapped_column(String(80))
    return_24h: Mapped[str | None] = mapped_column(String(80))
    volatility_1h: Mapped[str | None] = mapped_column(String(80))
    volatility_4h: Mapped[str | None] = mapped_column(String(80))
    volatility_24h: Mapped[str | None] = mapped_column(String(80))
    momentum_score: Mapped[str | None] = mapped_column(String(80))
    trend_direction: Mapped[str] = mapped_column(String(20), nullable=False, index=True)
    raw_json: Mapped[str] = mapped_column(Text, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)

    __table_args__ = (Index("ix_crypto_features_symbol_generated", "symbol", "generated_at"),)


class CryptoMarketLink(Base):
    __tablename__ = "crypto_market_links"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    ticker: Mapped[str] = mapped_column(String(128), nullable=False, index=True)
    symbol: Mapped[str] = mapped_column(String(20), nullable=False, index=True)
    detected_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        index=True,
    )
    confidence: Mapped[str] = mapped_column(String(80), nullable=False)
    reason: Mapped[str] = mapped_column(Text, nullable=False)
    raw_json: Mapped[str] = mapped_column(Text, nullable=False)

    __table_args__ = (Index("ix_crypto_market_links_ticker_detected", "ticker", "detected_at"),)


class WeatherObservation(Base):
    __tablename__ = "weather_observations"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    location_key: Mapped[str] = mapped_column(String(100), nullable=False, index=True)
    source: Mapped[str] = mapped_column(String(100), nullable=False, index=True)
    observed_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        index=True,
    )
    latitude: Mapped[str | None] = mapped_column(String(80))
    longitude: Mapped[str | None] = mapped_column(String(80))
    temperature_f: Mapped[str | None] = mapped_column(String(80))
    dewpoint_f: Mapped[str | None] = mapped_column(String(80))
    humidity: Mapped[str | None] = mapped_column(String(80))
    wind_speed_mph: Mapped[str | None] = mapped_column(String(80))
    wind_gust_mph: Mapped[str | None] = mapped_column(String(80))
    precipitation_inches: Mapped[str | None] = mapped_column(String(80))
    raw_json: Mapped[str] = mapped_column(Text, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)

    __table_args__ = (
        Index("ix_weather_observations_location_observed", "location_key", "observed_at"),
    )


class WeatherForecast(Base):
    __tablename__ = "weather_forecasts"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    location_key: Mapped[str] = mapped_column(String(100), nullable=False, index=True)
    source: Mapped[str] = mapped_column(String(100), nullable=False, index=True)
    forecast_generated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        index=True,
    )
    forecast_time: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        index=True,
    )
    latitude: Mapped[str | None] = mapped_column(String(80))
    longitude: Mapped[str | None] = mapped_column(String(80))
    temperature_f: Mapped[str | None] = mapped_column(String(80))
    dewpoint_f: Mapped[str | None] = mapped_column(String(80))
    humidity: Mapped[str | None] = mapped_column(String(80))
    wind_speed_mph: Mapped[str | None] = mapped_column(String(80))
    wind_gust_mph: Mapped[str | None] = mapped_column(String(80))
    precipitation_probability: Mapped[str | None] = mapped_column(String(80))
    precipitation_inches: Mapped[str | None] = mapped_column(String(80))
    short_forecast: Mapped[str | None] = mapped_column(String(500))
    detailed_forecast: Mapped[str | None] = mapped_column(Text)
    raw_json: Mapped[str] = mapped_column(Text, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)

    __table_args__ = (Index("ix_weather_forecasts_location_time", "location_key", "forecast_time"),)


class WeatherFeature(Base):
    __tablename__ = "weather_features"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    location_key: Mapped[str] = mapped_column(String(100), nullable=False, index=True)
    source: Mapped[str] = mapped_column(String(100), nullable=False, index=True)
    generated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        index=True,
    )
    target_time: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        index=True,
    )
    temperature_f: Mapped[str | None] = mapped_column(String(80))
    precipitation_probability: Mapped[str | None] = mapped_column(String(80))
    expected_precipitation_inches: Mapped[str | None] = mapped_column(String(80))
    wind_speed_mph: Mapped[str | None] = mapped_column(String(80))
    wind_gust_mph: Mapped[str | None] = mapped_column(String(80))
    heat_index_f: Mapped[str | None] = mapped_column(String(80))
    freeze_risk_score: Mapped[str | None] = mapped_column(String(80))
    rain_risk_score: Mapped[str | None] = mapped_column(String(80))
    wind_risk_score: Mapped[str | None] = mapped_column(String(80))
    temp_anomaly_score: Mapped[str | None] = mapped_column(String(80))
    weather_confidence_score: Mapped[str | None] = mapped_column(String(80))
    raw_json: Mapped[str] = mapped_column(Text, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)

    __table_args__ = (Index("ix_weather_features_location_target", "location_key", "target_time"),)


class WeatherMarketLink(Base):
    __tablename__ = "weather_market_links"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    ticker: Mapped[str] = mapped_column(String(128), nullable=False, index=True)
    location_key: Mapped[str] = mapped_column(String(100), nullable=False, index=True)
    detected_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        index=True,
    )
    weather_metric: Mapped[str] = mapped_column(String(50), nullable=False, index=True)
    target_operator: Mapped[str] = mapped_column(String(50), nullable=False, index=True)
    target_value: Mapped[str | None] = mapped_column(String(80))
    target_time: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), index=True)
    confidence: Mapped[str] = mapped_column(String(80), nullable=False)
    reason: Mapped[str] = mapped_column(Text, nullable=False)
    raw_json: Mapped[str] = mapped_column(Text, nullable=False)

    __table_args__ = (Index("ix_weather_market_links_ticker_detected", "ticker", "detected_at"),)


class EconomicEvent(Base):
    __tablename__ = "economic_events"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    event_key: Mapped[str] = mapped_column(String(128), nullable=False, index=True)
    source: Mapped[str] = mapped_column(String(100), nullable=False, index=True)
    event_time: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        index=True,
    )
    category: Mapped[str] = mapped_column(String(80), nullable=False, index=True)
    title: Mapped[str] = mapped_column(String(500), nullable=False)
    actual_value: Mapped[str | None] = mapped_column(String(80))
    forecast_value: Mapped[str | None] = mapped_column(String(80))
    previous_value: Mapped[str | None] = mapped_column(String(80))
    raw_json: Mapped[str] = mapped_column(Text, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)

    __table_args__ = (Index("ix_economic_events_key_time", "event_key", "event_time"),)


class EconomicFeature(Base):
    __tablename__ = "economic_features"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    event_key: Mapped[str] = mapped_column(String(128), nullable=False, index=True)
    generated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        index=True,
    )
    category: Mapped[str] = mapped_column(String(80), nullable=False, index=True)
    surprise_score: Mapped[str | None] = mapped_column(String(80))
    direction: Mapped[str] = mapped_column(String(50), nullable=False, index=True)
    confidence_score: Mapped[str | None] = mapped_column(String(80))
    raw_json: Mapped[str] = mapped_column(Text, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)

    __table_args__ = (Index("ix_economic_features_key_generated", "event_key", "generated_at"),)


class EconomicMarketLink(Base):
    __tablename__ = "economic_market_links"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    ticker: Mapped[str] = mapped_column(String(128), nullable=False, index=True)
    event_key: Mapped[str] = mapped_column(String(128), nullable=False, index=True)
    detected_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        index=True,
    )
    category: Mapped[str] = mapped_column(String(80), nullable=False, index=True)
    confidence: Mapped[str] = mapped_column(String(80), nullable=False)
    reason: Mapped[str] = mapped_column(Text, nullable=False)
    raw_json: Mapped[str] = mapped_column(Text, nullable=False)

    __table_args__ = (Index("ix_economic_market_links_ticker_detected", "ticker", "detected_at"),)


class ModelTournamentRun(Base):
    __tablename__ = "model_tournament_runs"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    name: Mapped[str] = mapped_column(String(200), nullable=False)
    started_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        index=True,
    )
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    days: Mapped[int] = mapped_column(Integer, nullable=False)
    config_json: Mapped[str] = mapped_column(Text, nullable=False)
    summary_json: Mapped[str | None] = mapped_column(Text)
    notes: Mapped[str | None] = mapped_column(Text)


class ModelTournamentResult(Base):
    __tablename__ = "model_tournament_results"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    tournament_run_id: Mapped[int] = mapped_column(
        ForeignKey("model_tournament_runs.id"),
        index=True,
    )
    model_name: Mapped[str] = mapped_column(String(100), nullable=False, index=True)
    category: Mapped[str] = mapped_column(String(50), nullable=False, index=True)
    forecast_count: Mapped[int] = mapped_column(Integer, nullable=False)
    evaluated_forecast_count: Mapped[int] = mapped_column(Integer, nullable=False)
    simulated_trade_count: Mapped[int] = mapped_column(Integer, nullable=False)
    settled_trade_count: Mapped[int] = mapped_column(Integer, nullable=False)
    brier_score: Mapped[str | None] = mapped_column(String(80))
    log_loss: Mapped[str | None] = mapped_column(String(80))
    win_rate: Mapped[str | None] = mapped_column(String(80))
    total_pnl: Mapped[str | None] = mapped_column(String(80))
    roi_on_exposure: Mapped[str | None] = mapped_column(String(80))
    avg_edge: Mapped[str | None] = mapped_column(String(80))
    max_drawdown: Mapped[str | None] = mapped_column(String(80))
    calibration_rank: Mapped[int | None] = mapped_column(Integer)
    pnl_rank: Mapped[int | None] = mapped_column(Integer)
    overall_rank: Mapped[int | None] = mapped_column(Integer)
    status: Mapped[str] = mapped_column(String(50), nullable=False, index=True)
    notes: Mapped[str] = mapped_column(Text, nullable=False)
    raw_json: Mapped[str] = mapped_column(Text, nullable=False)

    __table_args__ = (
        Index("ix_model_tournament_results_run_category", "tournament_run_id", "category"),
    )


class ModelWeight(Base):
    __tablename__ = "model_weights"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    generated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        index=True,
    )
    model_name: Mapped[str] = mapped_column(String(100), nullable=False, index=True)
    category: Mapped[str] = mapped_column(String(50), nullable=False, index=True)
    weight: Mapped[str] = mapped_column(String(80), nullable=False)
    method: Mapped[str] = mapped_column(String(100), nullable=False)
    lookback_days: Mapped[int] = mapped_column(Integer, nullable=False)
    raw_json: Mapped[str] = mapped_column(Text, nullable=False)

    __table_args__ = (Index("ix_model_weights_category_generated", "category", "generated_at"),)


class ModelDiagnostic(Base):
    __tablename__ = "model_diagnostics"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    generated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        index=True,
    )
    model_name: Mapped[str] = mapped_column(String(100), nullable=False, index=True)
    category: Mapped[str] = mapped_column(String(50), nullable=False, index=True)
    diagnostic_type: Mapped[str] = mapped_column(String(100), nullable=False, index=True)
    metric_name: Mapped[str] = mapped_column(String(100), nullable=False)
    metric_value: Mapped[str | None] = mapped_column(String(80))
    notes: Mapped[str] = mapped_column(Text, nullable=False)
    raw_json: Mapped[str] = mapped_column(Text, nullable=False)

    __table_args__ = (Index("ix_model_diagnostics_generated_model", "generated_at", "model_name"),)


class AutopilotRun(Base):
    __tablename__ = "autopilot_runs"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    started_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        index=True,
    )
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    status: Mapped[str] = mapped_column(String(50), nullable=False, index=True)
    model_name: Mapped[str] = mapped_column(String(100), nullable=False, index=True)
    dry_run: Mapped[int] = mapped_column(Integer, nullable=False)
    max_cycles: Mapped[int] = mapped_column(Integer, nullable=False)
    cycles_completed: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    orders_attempted: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    orders_submitted: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    orders_blocked: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    stop_reason: Mapped[str | None] = mapped_column(Text)
    config_json: Mapped[str] = mapped_column(Text, nullable=False)
    summary_json: Mapped[str | None] = mapped_column(Text)
    notes: Mapped[str | None] = mapped_column(Text)


class AutopilotCycle(Base):
    __tablename__ = "autopilot_cycles"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    autopilot_run_id: Mapped[int] = mapped_column(ForeignKey("autopilot_runs.id"), index=True)
    cycle_number: Mapped[int] = mapped_column(Integer, nullable=False)
    started_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        index=True,
    )
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    status: Mapped[str] = mapped_column(String(50), nullable=False, index=True)
    model_name: Mapped[str] = mapped_column(String(100), nullable=False, index=True)
    opportunities_scanned: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    orders_attempted: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    orders_submitted: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    orders_blocked: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    stop_reason: Mapped[str | None] = mapped_column(Text)
    summary_json: Mapped[str | None] = mapped_column(Text)
    notes: Mapped[str | None] = mapped_column(Text)

    __table_args__ = (Index("ix_autopilot_cycles_run_cycle", "autopilot_run_id", "cycle_number"),)


class RiskEvent(Base):
    __tablename__ = "risk_events"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        index=True,
    )
    event_type: Mapped[str] = mapped_column(String(100), nullable=False, index=True)
    severity: Mapped[str] = mapped_column(String(50), nullable=False, index=True)
    ticker: Mapped[str | None] = mapped_column(String(128), index=True)
    model_name: Mapped[str | None] = mapped_column(String(100), index=True)
    guardrail_name: Mapped[str] = mapped_column(String(100), nullable=False, index=True)
    message: Mapped[str] = mapped_column(Text, nullable=False)
    raw_json: Mapped[str] = mapped_column(Text, nullable=False)
    autopilot_run_id: Mapped[int | None] = mapped_column(
        ForeignKey("autopilot_runs.id"),
        index=True,
    )
    autopilot_cycle_id: Mapped[int | None] = mapped_column(
        ForeignKey("autopilot_cycles.id"),
        index=True,
    )

    __table_args__ = (Index("ix_risk_events_created_guardrail", "created_at", "guardrail_name"),)


class OvernightRun(Base):
    __tablename__ = "overnight_runs"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    started_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        index=True,
    )
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    status: Mapped[str] = mapped_column(String(50), nullable=False, index=True)
    cycles_requested: Mapped[int] = mapped_column(Integer, nullable=False)
    cycles_completed: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    errors_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    config_json: Mapped[str] = mapped_column(Text, nullable=False)
    summary_json: Mapped[str | None] = mapped_column(Text)


class OvernightCycle(Base):
    __tablename__ = "overnight_cycles"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    overnight_run_id: Mapped[int] = mapped_column(ForeignKey("overnight_runs.id"), index=True)
    cycle_number: Mapped[int] = mapped_column(Integer, nullable=False)
    started_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        index=True,
    )
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    status: Mapped[str] = mapped_column(String(50), nullable=False, index=True)
    markets_collected: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    snapshots_inserted: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    forecasts_inserted: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    paper_orders_created: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    opportunities_detected: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    settlements_synced: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    reports_generated: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    errors_json: Mapped[str | None] = mapped_column(Text)
    summary_json: Mapped[str | None] = mapped_column(Text)

    __table_args__ = (Index("ix_overnight_cycles_run_cycle", "overnight_run_id", "cycle_number"),)


class ModelIterationMetric(Base):
    __tablename__ = "model_iteration_metrics"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    generated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        index=True,
    )
    cycle_number: Mapped[int] = mapped_column(Integer, nullable=False, index=True)
    model_name: Mapped[str] = mapped_column(String(100), nullable=False, index=True)
    forecast_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    opportunity_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    paper_trade_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    estimated_pnl: Mapped[str | None] = mapped_column(String(80))
    realized_pnl: Mapped[str | None] = mapped_column(String(80))
    avg_edge: Mapped[str | None] = mapped_column(String(80))
    avg_opportunity_score: Mapped[str | None] = mapped_column(String(80))
    notes: Mapped[str] = mapped_column(Text, nullable=False)
    raw_json: Mapped[str] = mapped_column(Text, nullable=False)

    __table_args__ = (
        Index("ix_model_iteration_metrics_generated_model", "generated_at", "model_name"),
    )


class LearningRun(Base):
    __tablename__ = "learning_runs"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    started_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        index=True,
    )
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    status: Mapped[str] = mapped_column(String(50), nullable=False, index=True)
    cycles_completed: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    target_settled_trades: Mapped[int] = mapped_column(Integer, nullable=False)
    starting_settled_trades: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    ending_settled_trades: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    paper_trades_created: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    settlements_synced: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    config_json: Mapped[str] = mapped_column(Text, nullable=False)
    summary_json: Mapped[str | None] = mapped_column(Text)
    notes: Mapped[str | None] = mapped_column(Text)


class LearningCycle(Base):
    __tablename__ = "learning_cycles"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    learning_run_id: Mapped[int] = mapped_column(ForeignKey("learning_runs.id"), index=True)
    cycle_number: Mapped[int] = mapped_column(Integer, nullable=False)
    started_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        index=True,
    )
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    status: Mapped[str] = mapped_column(String(50), nullable=False, index=True)
    markets_scanned: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    forecasts_generated: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    opportunities_found: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    paper_trades_created: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    settlements_synced: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    settled_paper_trades_total: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    errors_json: Mapped[str | None] = mapped_column(Text)
    summary_json: Mapped[str | None] = mapped_column(Text)

    __table_args__ = (Index("ix_learning_cycles_run_cycle", "learning_run_id", "cycle_number"),)


class LearningRejectionLog(Base):
    __tablename__ = "learning_rejection_log"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    ticker: Mapped[str] = mapped_column(String(128), nullable=False, index=True)
    model_name: Mapped[str] = mapped_column(String(100), nullable=False, index=True)
    rejected_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        index=True,
    )
    reason: Mapped[str] = mapped_column(String(100), nullable=False, index=True)
    edge: Mapped[str | None] = mapped_column(String(80))
    opportunity_score: Mapped[str | None] = mapped_column(String(80), index=True)
    spread: Mapped[str | None] = mapped_column(String(80))
    liquidity: Mapped[str | None] = mapped_column(String(80))
    settlement_eta_hours: Mapped[str | None] = mapped_column(String(80))
    raw_json: Mapped[str] = mapped_column(Text, nullable=False)

    __table_args__ = (
        Index("ix_learning_rejections_reason_time", "reason", "rejected_at"),
        Index("ix_learning_rejections_model_score", "model_name", "opportunity_score"),
    )


class ModelConfidenceScore(Base):
    __tablename__ = "model_confidence_scores"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    generated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        index=True,
    )
    model_name: Mapped[str] = mapped_column(String(100), nullable=False, index=True)
    category: Mapped[str] = mapped_column(String(50), nullable=False, index=True)
    lookback_days: Mapped[int] = mapped_column(Integer, nullable=False)
    forecast_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    evaluated_forecast_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    paper_trade_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    settled_trade_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    brier_score: Mapped[str | None] = mapped_column(String(80))
    log_loss: Mapped[str | None] = mapped_column(String(80))
    win_rate: Mapped[str | None] = mapped_column(String(80))
    roi_on_exposure: Mapped[str | None] = mapped_column(String(80))
    total_pnl: Mapped[str | None] = mapped_column(String(80))
    max_drawdown: Mapped[str | None] = mapped_column(String(80))
    sample_size_score: Mapped[str] = mapped_column(String(80), nullable=False)
    calibration_score: Mapped[str] = mapped_column(String(80), nullable=False)
    profitability_score: Mapped[str] = mapped_column(String(80), nullable=False)
    drawdown_score: Mapped[str] = mapped_column(String(80), nullable=False)
    confidence_score: Mapped[str] = mapped_column(String(80), nullable=False, index=True)
    confidence_label: Mapped[str] = mapped_column(String(100), nullable=False, index=True)
    status: Mapped[str] = mapped_column(String(50), nullable=False, index=True)
    notes: Mapped[str] = mapped_column(Text, nullable=False)
    raw_json: Mapped[str] = mapped_column(Text, nullable=False)

    __table_args__ = (
        Index(
            "ix_model_confidence_model_category_generated",
            "model_name",
            "category",
            "generated_at",
        ),
    )


class LearningTradeTarget(Base):
    __tablename__ = "learning_trade_targets"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    generated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        index=True,
    )
    ticker: Mapped[str] = mapped_column(String(128), nullable=False, index=True)
    model_name: Mapped[str] = mapped_column(String(100), nullable=False, index=True)
    category: Mapped[str] = mapped_column(String(50), nullable=False, index=True)
    settlement_speed_score: Mapped[str] = mapped_column(String(80), nullable=False)
    learning_priority_score: Mapped[str] = mapped_column(String(80), nullable=False, index=True)
    reason: Mapped[str] = mapped_column(Text, nullable=False)
    raw_json: Mapped[str] = mapped_column(Text, nullable=False)

    __table_args__ = (
        Index("ix_learning_targets_generated_score", "generated_at", "learning_priority_score"),
    )


class LearningOpportunity(Base):
    __tablename__ = "learning_opportunities"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        index=True,
    )
    ticker: Mapped[str] = mapped_column(String(128), nullable=False, index=True)
    model_name: Mapped[str] = mapped_column(String(100), nullable=False, index=True)
    side: Mapped[str | None] = mapped_column(String(20))
    price: Mapped[str | None] = mapped_column(String(80))
    forecast_probability: Mapped[str | None] = mapped_column(String(80))
    estimated_edge: Mapped[str | None] = mapped_column(String(80))
    opportunity_score: Mapped[str | None] = mapped_column(String(80), index=True)
    settlement_speed_score: Mapped[str | None] = mapped_column(String(80))
    status: Mapped[str] = mapped_column(String(50), nullable=False, index=True)
    source: Mapped[str] = mapped_column(String(100), nullable=False, index=True)
    raw_json: Mapped[str] = mapped_column(Text, nullable=False)

    __table_args__ = (
        Index("ix_learning_opportunities_created_score", "created_at", "opportunity_score"),
    )


class AutopilotOpportunity(Base):
    __tablename__ = "autopilot_opportunities"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        index=True,
    )
    autopilot_run_id: Mapped[int | None] = mapped_column(ForeignKey("autopilot_runs.id"))
    autopilot_cycle_id: Mapped[int | None] = mapped_column(ForeignKey("autopilot_cycles.id"))
    ticker: Mapped[str] = mapped_column(String(128), nullable=False, index=True)
    model_name: Mapped[str] = mapped_column(String(100), nullable=False, index=True)
    side: Mapped[str | None] = mapped_column(String(20))
    price: Mapped[str | None] = mapped_column(String(80))
    forecast_probability: Mapped[str | None] = mapped_column(String(80))
    estimated_edge: Mapped[str | None] = mapped_column(String(80))
    opportunity_score: Mapped[str | None] = mapped_column(String(80), index=True)
    model_confidence_score: Mapped[str | None] = mapped_column(String(80))
    status: Mapped[str] = mapped_column(String(50), nullable=False, index=True)
    source: Mapped[str] = mapped_column(String(100), nullable=False, index=True)
    raw_json: Mapped[str] = mapped_column(Text, nullable=False)

    __table_args__ = (
        Index("ix_autopilot_opportunities_created_score", "created_at", "opportunity_score"),
    )


class LearningPaperTrade(Base):
    __tablename__ = "learning_paper_trades"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        index=True,
    )
    paper_order_id: Mapped[int | None] = mapped_column(ForeignKey("paper_orders.id"), index=True)
    ticker: Mapped[str] = mapped_column(String(128), nullable=False, index=True)
    model_name: Mapped[str] = mapped_column(String(100), nullable=False, index=True)
    side: Mapped[str] = mapped_column(String(20), nullable=False)
    price: Mapped[str] = mapped_column(String(80), nullable=False)
    quantity: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    edge: Mapped[str | None] = mapped_column(String(80))
    status: Mapped[str] = mapped_column(String(50), nullable=False, index=True)
    raw_json: Mapped[str] = mapped_column(Text, nullable=False)

    __table_args__ = (Index("ix_learning_paper_trades_created_model", "created_at", "model_name"),)


class AutopilotPaperTrade(Base):
    __tablename__ = "autopilot_paper_trades"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        index=True,
    )
    autopilot_run_id: Mapped[int | None] = mapped_column(ForeignKey("autopilot_runs.id"))
    autopilot_cycle_id: Mapped[int | None] = mapped_column(ForeignKey("autopilot_cycles.id"))
    paper_order_id: Mapped[int | None] = mapped_column(ForeignKey("paper_orders.id"), index=True)
    ticker: Mapped[str] = mapped_column(String(128), nullable=False, index=True)
    model_name: Mapped[str] = mapped_column(String(100), nullable=False, index=True)
    side: Mapped[str] = mapped_column(String(20), nullable=False)
    price: Mapped[str] = mapped_column(String(80), nullable=False)
    quantity: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    edge: Mapped[str | None] = mapped_column(String(80))
    status: Mapped[str] = mapped_column(String(50), nullable=False, index=True)
    raw_json: Mapped[str] = mapped_column(Text, nullable=False)

    __table_args__ = (Index("ix_autopilot_paper_trades_created_model", "created_at", "model_name"),)


class LearningMetric(Base):
    __tablename__ = "learning_metrics"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    generated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        index=True,
    )
    window_days: Mapped[int] = mapped_column(Integer, nullable=False, default=30)
    opportunities_found: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    paper_trades_created: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    settled_trade_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    win_rate: Mapped[str | None] = mapped_column(String(80))
    roi_on_exposure: Mapped[str | None] = mapped_column(String(80))
    total_pnl: Mapped[str | None] = mapped_column(String(80))
    learning_confidence: Mapped[str | None] = mapped_column(String(80), index=True)
    notes: Mapped[str] = mapped_column(Text, nullable=False)
    raw_json: Mapped[str] = mapped_column(Text, nullable=False)


class AutopilotMetric(Base):
    __tablename__ = "autopilot_metrics"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    generated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        index=True,
    )
    window_days: Mapped[int] = mapped_column(Integer, nullable=False, default=30)
    opportunities_found: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    dry_run_orders: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    settled_trade_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    win_rate: Mapped[str | None] = mapped_column(String(80))
    roi_on_exposure: Mapped[str | None] = mapped_column(String(80))
    total_pnl: Mapped[str | None] = mapped_column(String(80))
    current_confidence: Mapped[str | None] = mapped_column(String(80), index=True)
    notes: Mapped[str] = mapped_column(Text, nullable=False)
    raw_json: Mapped[str] = mapped_column(Text, nullable=False)


class MicrostructureFeature(Base):
    __tablename__ = "microstructure_features"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        index=True,
    )
    ticker: Mapped[str] = mapped_column(String(128), nullable=False, index=True)
    lookback_minutes: Mapped[int] = mapped_column(Integer, nullable=False, index=True)
    snapshot_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    current_yes_bid: Mapped[str | None] = mapped_column(String(80))
    current_yes_ask: Mapped[str | None] = mapped_column(String(80))
    current_no_bid: Mapped[str | None] = mapped_column(String(80))
    current_no_ask: Mapped[str | None] = mapped_column(String(80))
    current_spread: Mapped[str | None] = mapped_column(String(80))
    avg_spread: Mapped[str | None] = mapped_column(String(80))
    min_spread: Mapped[str | None] = mapped_column(String(80))
    max_spread: Mapped[str | None] = mapped_column(String(80))
    spread_change: Mapped[str | None] = mapped_column(String(80))
    spread_change_pct: Mapped[str | None] = mapped_column(String(80))
    current_liquidity: Mapped[str | None] = mapped_column(String(80))
    avg_liquidity: Mapped[str | None] = mapped_column(String(80))
    liquidity_change: Mapped[str | None] = mapped_column(String(80))
    liquidity_change_pct: Mapped[str | None] = mapped_column(String(80))
    orderbook_imbalance: Mapped[str | None] = mapped_column(String(80))
    yes_bid_depth: Mapped[str | None] = mapped_column(String(80))
    no_bid_depth: Mapped[str | None] = mapped_column(String(80))
    price_velocity: Mapped[str | None] = mapped_column(String(80))
    price_acceleration: Mapped[str | None] = mapped_column(String(80))
    late_move_score: Mapped[str | None] = mapped_column(String(80))
    dislocation_score: Mapped[str | None] = mapped_column(String(80))
    smart_money_score: Mapped[str | None] = mapped_column(String(80))
    microstructure_confidence: Mapped[str | None] = mapped_column(String(80), index=True)
    raw_json: Mapped[str] = mapped_column(Text, nullable=False)

    __table_args__ = (Index("ix_microstructure_features_ticker_created", "ticker", "created_at"),)


class MicrostructureEvent(Base):
    __tablename__ = "microstructure_events"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        index=True,
    )
    ticker: Mapped[str] = mapped_column(String(128), nullable=False, index=True)
    event_type: Mapped[str] = mapped_column(String(100), nullable=False, index=True)
    severity: Mapped[str] = mapped_column(String(50), nullable=False, index=True)
    score: Mapped[str] = mapped_column(String(80), nullable=False, index=True)
    title: Mapped[str] = mapped_column(String(300), nullable=False)
    description: Mapped[str] = mapped_column(Text, nullable=False)
    evidence_json: Mapped[str] = mapped_column(Text, nullable=False)
    raw_json: Mapped[str] = mapped_column(Text, nullable=False)

    __table_args__ = (Index("ix_microstructure_events_type_created", "event_type", "created_at"),)


class MicrostructureSignal(Base):
    __tablename__ = "microstructure_signals"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        index=True,
    )
    ticker: Mapped[str] = mapped_column(String(128), nullable=False, index=True)
    signal_name: Mapped[str] = mapped_column(String(200), nullable=False, index=True)
    signal_strength: Mapped[str] = mapped_column(String(80), nullable=False)
    signal_direction: Mapped[str | None] = mapped_column(String(50), index=True)
    confidence: Mapped[str] = mapped_column(String(80), nullable=False)
    explanation: Mapped[str] = mapped_column(Text, nullable=False)
    raw_json: Mapped[str] = mapped_column(Text, nullable=False)

    __table_args__ = (
        Index("ix_microstructure_signals_ticker_created", "ticker", "created_at"),
        Index("ix_microstructure_signals_signal_created", "signal_name", "created_at"),
    )


class OrderbookDepthSnapshot(Base):
    __tablename__ = "orderbook_depth_snapshots"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        index=True,
    )
    ticker: Mapped[str] = mapped_column(String(128), nullable=False, index=True)
    yes_bid_depth: Mapped[str | None] = mapped_column(String(80))
    no_bid_depth: Mapped[str | None] = mapped_column(String(80))
    yes_levels_json: Mapped[str] = mapped_column(Text, nullable=False)
    no_levels_json: Mapped[str] = mapped_column(Text, nullable=False)
    imbalance: Mapped[str | None] = mapped_column(String(80), index=True)
    raw_json: Mapped[str] = mapped_column(Text, nullable=False)

    __table_args__ = (Index("ix_orderbook_depth_ticker_created", "ticker", "created_at"),)


class ForumConsensusSignal(Base):
    __tablename__ = "forum_consensus_signals"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    ticker: Mapped[str] = mapped_column(String(128), nullable=False, index=True)
    observed_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        index=True,
    )
    source: Mapped[str] = mapped_column(String(200), nullable=False, index=True)
    side: Mapped[str | None] = mapped_column(String(50), index=True)
    participant_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    winner_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    average_win_rate: Mapped[str | None] = mapped_column(String(80))
    longshot_price: Mapped[str | None] = mapped_column(String(80))
    consensus_score: Mapped[str | None] = mapped_column(String(80))
    notes: Mapped[str | None] = mapped_column(Text)
    raw_json: Mapped[str] = mapped_column(Text, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)

    __table_args__ = (Index("ix_forum_consensus_ticker_observed", "ticker", "observed_at"),)


class PositionHistory(Base):
    __tablename__ = "position_history"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    ticker: Mapped[str] = mapped_column(String(128), nullable=False, index=True)
    recorded_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        index=True,
    )
    position_size: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    avg_cost: Mapped[str | None] = mapped_column(String(80))
    market_price: Mapped[str | None] = mapped_column(String(80))
    realized_pnl: Mapped[str] = mapped_column(String(80), nullable=False)
    unrealized_pnl: Mapped[str] = mapped_column(String(80), nullable=False)
    total_pnl: Mapped[str] = mapped_column(String(80), nullable=False)
    exposure: Mapped[str] = mapped_column(String(80), nullable=False)

    __table_args__ = (Index("ix_position_history_ticker_recorded", "ticker", "recorded_at"),)


class PortfolioSnapshot(Base):
    __tablename__ = "portfolio_snapshots"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    snapshot_time: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        index=True,
    )
    total_positions: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    total_exposure: Mapped[str] = mapped_column(String(80), nullable=False)
    realized_pnl: Mapped[str] = mapped_column(String(80), nullable=False)
    unrealized_pnl: Mapped[str] = mapped_column(String(80), nullable=False)
    total_pnl: Mapped[str] = mapped_column(String(80), nullable=False)
    open_orders: Mapped[int] = mapped_column(Integer, nullable=False, default=0)


class Watchlist(Base):
    __tablename__ = "watchlists"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    name: Mapped[str] = mapped_column(String(100), nullable=False, unique=True, index=True)
    description: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)


class WatchlistMarket(Base):
    __tablename__ = "watchlist_markets"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    watchlist_id: Mapped[int] = mapped_column(ForeignKey("watchlists.id"), index=True)
    ticker: Mapped[str] = mapped_column(String(128), nullable=False, index=True)
    added_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    notes: Mapped[str | None] = mapped_column(Text)

    __table_args__ = (UniqueConstraint("watchlist_id", "ticker", name="uq_watchlist_market"),)


class Alert(Base):
    __tablename__ = "alerts"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    name: Mapped[str] = mapped_column(String(200), nullable=False, index=True)
    alert_type: Mapped[str] = mapped_column(String(100), nullable=False, index=True)
    threshold: Mapped[str | None] = mapped_column(String(80))
    enabled: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    raw_json: Mapped[str] = mapped_column(Text, nullable=False)


class AlertEvent(Base):
    __tablename__ = "alert_events"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    alert_id: Mapped[int | None] = mapped_column(ForeignKey("alerts.id"), index=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        index=True,
    )
    alert_type: Mapped[str] = mapped_column(String(100), nullable=False, index=True)
    ticker: Mapped[str | None] = mapped_column(String(128), index=True)
    severity: Mapped[str] = mapped_column(String(50), nullable=False, index=True)
    message: Mapped[str] = mapped_column(Text, nullable=False)
    raw_json: Mapped[str] = mapped_column(Text, nullable=False)
    acknowledged_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    __table_args__ = (Index("ix_alert_events_created_type", "created_at", "alert_type"),)


class ResearchNote(Base):
    __tablename__ = "research_notes"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        index=True,
    )
    ticker: Mapped[str] = mapped_column(String(128), nullable=False, index=True)
    model_name: Mapped[str] = mapped_column(String(100), nullable=False, index=True)
    note_type: Mapped[str] = mapped_column(String(100), nullable=False, index=True)
    title: Mapped[str] = mapped_column(String(500), nullable=False)
    summary: Mapped[str] = mapped_column(Text, nullable=False)
    evidence_json: Mapped[str] = mapped_column(Text, nullable=False)
    risks_json: Mapped[str] = mapped_column(Text, nullable=False)
    recommendation: Mapped[str] = mapped_column(Text, nullable=False)
    confidence_label: Mapped[str] = mapped_column(String(100), nullable=False)
    raw_json: Mapped[str] = mapped_column(Text, nullable=False)

    __table_args__ = (Index("ix_research_notes_ticker_created", "ticker", "created_at"),)


class ResearchQuestion(Base):
    __tablename__ = "research_questions"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        index=True,
    )
    question: Mapped[str] = mapped_column(Text, nullable=False)
    ticker: Mapped[str | None] = mapped_column(String(128), index=True)
    model_name: Mapped[str | None] = mapped_column(String(100), index=True)
    answer: Mapped[str] = mapped_column(Text, nullable=False)
    evidence_json: Mapped[str] = mapped_column(Text, nullable=False)
    raw_json: Mapped[str] = mapped_column(Text, nullable=False)

    __table_args__ = (Index("ix_research_questions_ticker_created", "ticker", "created_at"),)


class OpportunityResearchSnapshot(Base):
    __tablename__ = "opportunity_research_snapshots"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        index=True,
    )
    ticker: Mapped[str] = mapped_column(String(128), nullable=False, index=True)
    model_name: Mapped[str] = mapped_column(String(100), nullable=False, index=True)
    rank: Mapped[int | None] = mapped_column(Integer)
    opportunity_score: Mapped[str | None] = mapped_column(String(80))
    edge: Mapped[str | None] = mapped_column(String(80))
    market_price: Mapped[str | None] = mapped_column(String(80))
    model_probability: Mapped[str | None] = mapped_column(String(80))
    primary_driver: Mapped[str] = mapped_column(Text, nullable=False)
    supporting_signals_json: Mapped[str] = mapped_column(Text, nullable=False)
    risk_factors_json: Mapped[str] = mapped_column(Text, nullable=False)
    recommendation: Mapped[str] = mapped_column(Text, nullable=False)
    raw_json: Mapped[str] = mapped_column(Text, nullable=False)

    __table_args__ = (
        Index(
            "ix_opportunity_research_snapshots_ticker_model_created",
            "ticker",
            "model_name",
            "created_at",
        ),
    )


class Signal(Base):
    __tablename__ = "signals"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        index=True,
    )
    signal_name: Mapped[str] = mapped_column(String(200), nullable=False, unique=True, index=True)
    category: Mapped[str] = mapped_column(String(100), nullable=False, index=True)
    description: Mapped[str] = mapped_column(Text, nullable=False)
    status: Mapped[str] = mapped_column(String(50), nullable=False, index=True)
    metadata_json: Mapped[str] = mapped_column(Text, nullable=False)


class SignalEvent(Base):
    __tablename__ = "signal_events"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        index=True,
    )
    ticker: Mapped[str] = mapped_column(String(128), nullable=False, index=True)
    signal_name: Mapped[str] = mapped_column(String(200), nullable=False, index=True)
    model_name: Mapped[str | None] = mapped_column(String(100), index=True)
    signal_strength: Mapped[str] = mapped_column(String(80), nullable=False)
    signal_value: Mapped[str | None] = mapped_column(String(200))
    signal_direction: Mapped[str | None] = mapped_column(String(50), index=True)
    confidence: Mapped[str] = mapped_column(String(80), nullable=False)
    raw_json: Mapped[str] = mapped_column(Text, nullable=False)

    __table_args__ = (
        Index("ix_signal_events_signal_created", "signal_name", "created_at"),
        Index("ix_signal_events_ticker_model_created", "ticker", "model_name", "created_at"),
    )


class SignalForecast(Base):
    __tablename__ = "signal_forecasts"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        index=True,
    )
    forecast_id: Mapped[int] = mapped_column(ForeignKey("forecasts.id"), nullable=False, index=True)
    signal_name: Mapped[str] = mapped_column(String(200), nullable=False, index=True)
    contribution_score: Mapped[str] = mapped_column(String(80), nullable=False)

    __table_args__ = (
        UniqueConstraint("forecast_id", "signal_name", name="uq_signal_forecast"),
        Index("ix_signal_forecasts_signal_created", "signal_name", "created_at"),
    )


class SignalTrade(Base):
    __tablename__ = "signal_trades"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        index=True,
    )
    paper_order_id: Mapped[int] = mapped_column(
        ForeignKey("paper_orders.id"),
        nullable=False,
        index=True,
    )
    signal_name: Mapped[str] = mapped_column(String(200), nullable=False, index=True)
    contribution_score: Mapped[str] = mapped_column(String(80), nullable=False)

    __table_args__ = (
        UniqueConstraint("paper_order_id", "signal_name", name="uq_signal_trade"),
        Index("ix_signal_trades_signal_created", "signal_name", "created_at"),
    )


class SignalSkipLog(Base):
    __tablename__ = "signal_skip_log"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    signal_name: Mapped[str] = mapped_column(String(200), nullable=False, index=True)
    ticker: Mapped[str] = mapped_column(String(128), nullable=False, index=True)
    skipped_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        index=True,
    )
    reason: Mapped[str] = mapped_column(Text, nullable=False)
    required_data: Mapped[str] = mapped_column(Text, nullable=False)
    available_data: Mapped[str] = mapped_column(Text, nullable=False)
    raw_json: Mapped[str] = mapped_column(Text, nullable=False)

    __table_args__ = (
        Index("ix_signal_skip_signal_ticker_time", "signal_name", "ticker", "skipped_at"),
    )


class SignalPerformance(Base):
    __tablename__ = "signal_performance"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    generated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        index=True,
    )
    signal_name: Mapped[str] = mapped_column(String(200), nullable=False, index=True)
    category: Mapped[str] = mapped_column(String(100), nullable=False, index=True)
    forecast_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    trade_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    settled_trade_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    win_rate: Mapped[str | None] = mapped_column(String(80))
    total_pnl: Mapped[str | None] = mapped_column(String(80))
    roi: Mapped[str | None] = mapped_column(String(80))
    avg_edge: Mapped[str | None] = mapped_column(String(80))
    avg_opportunity_score: Mapped[str | None] = mapped_column(String(80))
    brier_score: Mapped[str | None] = mapped_column(String(80))
    log_loss: Mapped[str | None] = mapped_column(String(80))
    confidence_score: Mapped[str | None] = mapped_column(String(80))
    raw_json: Mapped[str] = mapped_column(Text, nullable=False)

    __table_args__ = (
        Index("ix_signal_performance_signal_generated", "signal_name", "generated_at"),
    )


class NewsItem(Base):
    __tablename__ = "news_items"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    source: Mapped[str] = mapped_column(String(200), nullable=False, index=True)
    source_url: Mapped[str | None] = mapped_column(String(1000), index=True)
    published_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), index=True)
    ingested_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        index=True,
    )
    title: Mapped[str] = mapped_column(String(1000), nullable=False)
    summary: Mapped[str | None] = mapped_column(Text)
    body: Mapped[str | None] = mapped_column(Text)
    author: Mapped[str | None] = mapped_column(String(300))
    category: Mapped[str] = mapped_column(String(100), nullable=False, index=True)
    entities_json: Mapped[str] = mapped_column(Text, nullable=False)
    sentiment_score: Mapped[str] = mapped_column(String(80), nullable=False, default="0")
    importance_score: Mapped[str] = mapped_column(String(80), nullable=False, default="0")
    freshness_score: Mapped[str] = mapped_column(String(80), nullable=False, default="0")
    raw_json: Mapped[str] = mapped_column(Text, nullable=False)

    __table_args__ = (Index("ix_news_items_category_published", "category", "published_at"),)


class NewsMarketLink(Base):
    __tablename__ = "news_market_links"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        index=True,
    )
    news_item_id: Mapped[int] = mapped_column(ForeignKey("news_items.id"), index=True)
    ticker: Mapped[str] = mapped_column(ForeignKey("markets.ticker"), index=True)
    link_confidence: Mapped[str] = mapped_column(String(80), nullable=False)
    link_reason: Mapped[str] = mapped_column(Text, nullable=False)
    matched_terms_json: Mapped[str] = mapped_column(Text, nullable=False)
    raw_json: Mapped[str] = mapped_column(Text, nullable=False)

    __table_args__ = (
        UniqueConstraint("news_item_id", "ticker", name="uq_news_market_link"),
        Index("ix_news_market_links_ticker_created", "ticker", "created_at"),
    )


class NewsFeature(Base):
    __tablename__ = "news_features"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        index=True,
    )
    ticker: Mapped[str] = mapped_column(ForeignKey("markets.ticker"), index=True)
    feature_window_minutes: Mapped[int] = mapped_column(Integer, nullable=False)
    news_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    high_importance_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    avg_sentiment: Mapped[str | None] = mapped_column(String(80))
    max_importance: Mapped[str | None] = mapped_column(String(80))
    freshness_score: Mapped[str | None] = mapped_column(String(80))
    category_counts_json: Mapped[str] = mapped_column(Text, nullable=False)
    entity_counts_json: Mapped[str] = mapped_column(Text, nullable=False)
    linked_news_json: Mapped[str] = mapped_column(Text, nullable=False)
    raw_json: Mapped[str] = mapped_column(Text, nullable=False)

    __table_args__ = (Index("ix_news_features_ticker_created", "ticker", "created_at"),)


class NewsSignal(Base):
    __tablename__ = "news_signals"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        index=True,
    )
    ticker: Mapped[str] = mapped_column(ForeignKey("markets.ticker"), index=True)
    signal_name: Mapped[str] = mapped_column(String(200), nullable=False, index=True)
    signal_strength: Mapped[str] = mapped_column(String(80), nullable=False)
    signal_direction: Mapped[str | None] = mapped_column(String(50), index=True)
    confidence: Mapped[str] = mapped_column(String(80), nullable=False)
    explanation: Mapped[str] = mapped_column(Text, nullable=False)
    raw_json: Mapped[str] = mapped_column(Text, nullable=False)

    __table_args__ = (
        Index("ix_news_signals_ticker_created", "ticker", "created_at"),
        Index("ix_news_signals_signal_created", "signal_name", "created_at"),
    )


class SportsTeam(Base):
    __tablename__ = "sports_teams"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    league: Mapped[str] = mapped_column(String(20), nullable=False, index=True)
    team_key: Mapped[str] = mapped_column(String(120), nullable=False, index=True)
    team_name: Mapped[str] = mapped_column(String(300), nullable=False)
    abbreviation: Mapped[str | None] = mapped_column(String(20), index=True)
    city: Mapped[str | None] = mapped_column(String(200))
    conference: Mapped[str | None] = mapped_column(String(100))
    division: Mapped[str | None] = mapped_column(String(100))
    venue: Mapped[str | None] = mapped_column(String(300))
    raw_json: Mapped[str] = mapped_column(Text, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)

    __table_args__ = (UniqueConstraint("league", "team_key", name="uq_sports_team_league_key"),)


class SportsGame(Base):
    __tablename__ = "sports_games"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    league: Mapped[str] = mapped_column(String(20), nullable=False, index=True)
    game_key: Mapped[str] = mapped_column(String(160), nullable=False, index=True)
    scheduled_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), index=True)
    season: Mapped[str | None] = mapped_column(String(40), index=True)
    status: Mapped[str] = mapped_column(String(80), nullable=False, index=True)
    home_team_key: Mapped[str] = mapped_column(String(120), nullable=False, index=True)
    away_team_key: Mapped[str] = mapped_column(String(120), nullable=False, index=True)
    home_score: Mapped[int | None] = mapped_column(Integer)
    away_score: Mapped[int | None] = mapped_column(Integer)
    venue: Mapped[str | None] = mapped_column(String(300))
    neutral_site: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    raw_json: Mapped[str] = mapped_column(Text, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)

    __table_args__ = (
        UniqueConstraint("league", "game_key", name="uq_sports_game_league_key"),
        Index("ix_sports_games_league_scheduled", "league", "scheduled_at"),
    )


class SportsTeamStat(Base):
    __tablename__ = "sports_team_stats"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    league: Mapped[str] = mapped_column(String(20), nullable=False, index=True)
    team_key: Mapped[str] = mapped_column(String(120), nullable=False, index=True)
    as_of: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, index=True)
    games_played: Mapped[int | None] = mapped_column(Integer)
    wins: Mapped[int | None] = mapped_column(Integer)
    losses: Mapped[int | None] = mapped_column(Integer)
    rating: Mapped[str | None] = mapped_column(String(80))
    offense_rating: Mapped[str | None] = mapped_column(String(80))
    defense_rating: Mapped[str | None] = mapped_column(String(80))
    recent_form: Mapped[str | None] = mapped_column(String(80))
    raw_json: Mapped[str] = mapped_column(Text, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)

    __table_args__ = (Index("ix_sports_stats_league_team_asof", "league", "team_key", "as_of"),)


class SportsInjury(Base):
    __tablename__ = "sports_injuries"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    league: Mapped[str] = mapped_column(String(20), nullable=False, index=True)
    team_key: Mapped[str] = mapped_column(String(120), nullable=False, index=True)
    player_name: Mapped[str] = mapped_column(String(300), nullable=False)
    status: Mapped[str] = mapped_column(String(120), nullable=False, index=True)
    impact_score: Mapped[str | None] = mapped_column(String(80))
    reported_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), index=True)
    notes: Mapped[str | None] = mapped_column(Text)
    raw_json: Mapped[str] = mapped_column(Text, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)

    __table_args__ = (
        Index("ix_sports_injuries_league_team_reported", "league", "team_key", "reported_at"),
    )


class SportsOdds(Base):
    __tablename__ = "sports_odds"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    league: Mapped[str] = mapped_column(String(20), nullable=False, index=True)
    game_key: Mapped[str] = mapped_column(String(160), nullable=False, index=True)
    sportsbook: Mapped[str] = mapped_column(String(120), nullable=False, index=True)
    observed_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        index=True,
    )
    home_moneyline: Mapped[str | None] = mapped_column(String(80))
    away_moneyline: Mapped[str | None] = mapped_column(String(80))
    spread: Mapped[str | None] = mapped_column(String(80))
    total: Mapped[str | None] = mapped_column(String(80))
    home_spread_price: Mapped[str | None] = mapped_column(String(80))
    away_spread_price: Mapped[str | None] = mapped_column(String(80))
    over_price: Mapped[str | None] = mapped_column(String(80))
    under_price: Mapped[str | None] = mapped_column(String(80))
    raw_json: Mapped[str] = mapped_column(Text, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)

    __table_args__ = (
        Index("ix_sports_odds_league_game_observed", "league", "game_key", "observed_at"),
    )


class SportsFeature(Base):
    __tablename__ = "sports_features"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        index=True,
    )
    league: Mapped[str] = mapped_column(String(20), nullable=False, index=True)
    game_key: Mapped[str] = mapped_column(String(160), nullable=False, index=True)
    ticker: Mapped[str | None] = mapped_column(String(128), index=True)
    home_team_key: Mapped[str] = mapped_column(String(120), nullable=False, index=True)
    away_team_key: Mapped[str] = mapped_column(String(120), nullable=False, index=True)
    team_strength_edge: Mapped[str] = mapped_column(String(80), nullable=False)
    injury_edge: Mapped[str] = mapped_column(String(80), nullable=False)
    rest_edge: Mapped[str] = mapped_column(String(80), nullable=False)
    travel_edge: Mapped[str] = mapped_column(String(80), nullable=False)
    odds_edge: Mapped[str] = mapped_column(String(80), nullable=False)
    weather_edge: Mapped[str] = mapped_column(String(80), nullable=False)
    total_edge: Mapped[str] = mapped_column(String(80), nullable=False)
    home_win_probability: Mapped[str] = mapped_column(String(80), nullable=False)
    away_win_probability: Mapped[str] = mapped_column(String(80), nullable=False)
    projected_total: Mapped[str | None] = mapped_column(String(80))
    confidence_score: Mapped[str] = mapped_column(String(80), nullable=False)
    raw_json: Mapped[str] = mapped_column(Text, nullable=False)

    __table_args__ = (
        Index("ix_sports_features_league_game_created", "league", "game_key", "created_at"),
        Index("ix_sports_features_ticker_created", "ticker", "created_at"),
    )


class SportsMarketLink(Base):
    __tablename__ = "sports_market_links"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        index=True,
    )
    ticker: Mapped[str] = mapped_column(ForeignKey("markets.ticker"), index=True)
    league: Mapped[str] = mapped_column(String(20), nullable=False, index=True)
    game_key: Mapped[str] = mapped_column(String(160), nullable=False, index=True)
    market_type: Mapped[str] = mapped_column(String(80), nullable=False, index=True)
    link_confidence: Mapped[str] = mapped_column(String(80), nullable=False)
    link_reason: Mapped[str] = mapped_column(Text, nullable=False)
    matched_terms_json: Mapped[str] = mapped_column(Text, nullable=False)
    raw_json: Mapped[str] = mapped_column(Text, nullable=False)

    __table_args__ = (
        UniqueConstraint("ticker", "game_key", "market_type", name="uq_sports_market_link"),
        Index("ix_sports_market_links_ticker_created", "ticker", "created_at"),
    )


class SportsSignal(Base):
    __tablename__ = "sports_signals"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        index=True,
    )
    ticker: Mapped[str] = mapped_column(ForeignKey("markets.ticker"), index=True)
    league: Mapped[str] = mapped_column(String(20), nullable=False, index=True)
    game_key: Mapped[str] = mapped_column(String(160), nullable=False, index=True)
    signal_name: Mapped[str] = mapped_column(String(200), nullable=False, index=True)
    signal_strength: Mapped[str] = mapped_column(String(80), nullable=False)
    signal_direction: Mapped[str | None] = mapped_column(String(50), index=True)
    confidence: Mapped[str] = mapped_column(String(80), nullable=False)
    explanation: Mapped[str] = mapped_column(Text, nullable=False)
    raw_json: Mapped[str] = mapped_column(Text, nullable=False)

    __table_args__ = (
        Index("ix_sports_signals_ticker_created", "ticker", "created_at"),
        Index("ix_sports_signals_signal_created", "signal_name", "created_at"),
    )


class MetaModelFeature(Base):
    __tablename__ = "meta_model_features"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        index=True,
    )
    ticker: Mapped[str] = mapped_column(ForeignKey("markets.ticker"), index=True)
    category: Mapped[str] = mapped_column(String(100), nullable=False, index=True)
    market_type: Mapped[str | None] = mapped_column(String(100))
    time_to_close_minutes: Mapped[str | None] = mapped_column(String(80))
    liquidity_score: Mapped[str | None] = mapped_column(String(80))
    spread_score: Mapped[str | None] = mapped_column(String(80))
    data_freshness_score: Mapped[str | None] = mapped_column(String(80))
    signal_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    active_signals_json: Mapped[str] = mapped_column(Text, nullable=False)
    model_probabilities_json: Mapped[str] = mapped_column(Text, nullable=False)
    model_disagreement_score: Mapped[str | None] = mapped_column(String(80))
    model_agreement_score: Mapped[str | None] = mapped_column(String(80))
    model_recent_performance_json: Mapped[str] = mapped_column(Text, nullable=False)
    category_performance_json: Mapped[str] = mapped_column(Text, nullable=False)
    microstructure_features_json: Mapped[str] = mapped_column(Text, nullable=False)
    news_features_json: Mapped[str] = mapped_column(Text, nullable=False)
    economic_features_json: Mapped[str] = mapped_column(Text, nullable=False)
    sports_features_json: Mapped[str] = mapped_column(Text, nullable=False)
    crypto_features_json: Mapped[str] = mapped_column(Text, nullable=False)
    weather_features_json: Mapped[str] = mapped_column(Text, nullable=False)
    raw_json: Mapped[str] = mapped_column(Text, nullable=False)

    __table_args__ = (Index("ix_meta_features_ticker_created", "ticker", "created_at"),)


class MetaModelDecision(Base):
    __tablename__ = "meta_model_decisions"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        index=True,
    )
    ticker: Mapped[str] = mapped_column(ForeignKey("markets.ticker"), index=True)
    selected_model_name: Mapped[str] = mapped_column(String(100), nullable=False, index=True)
    selected_probability: Mapped[str | None] = mapped_column(String(80))
    selected_confidence: Mapped[str | None] = mapped_column(String(80))
    fallback_model_name: Mapped[str | None] = mapped_column(String(100), index=True)
    decision_reason: Mapped[str] = mapped_column(Text, nullable=False)
    competing_models_json: Mapped[str] = mapped_column(Text, nullable=False)
    trust_scores_json: Mapped[str] = mapped_column(Text, nullable=False)
    raw_json: Mapped[str] = mapped_column(Text, nullable=False)

    __table_args__ = (Index("ix_meta_decisions_ticker_created", "ticker", "created_at"),)


class MetaModelTrainingExample(Base):
    __tablename__ = "meta_model_training_examples"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        index=True,
    )
    ticker: Mapped[str] = mapped_column(ForeignKey("markets.ticker"), index=True)
    forecast_id: Mapped[int] = mapped_column(ForeignKey("forecasts.id"), index=True)
    model_name: Mapped[str] = mapped_column(String(100), nullable=False, index=True)
    category: Mapped[str] = mapped_column(String(100), nullable=False, index=True)
    market_type: Mapped[str | None] = mapped_column(String(100))
    predicted_probability: Mapped[str] = mapped_column(String(80), nullable=False)
    settlement_result: Mapped[str] = mapped_column(String(100), nullable=False)
    absolute_error: Mapped[str] = mapped_column(String(80), nullable=False)
    brier_loss: Mapped[str] = mapped_column(String(80), nullable=False, index=True)
    was_best_model: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    features_json: Mapped[str] = mapped_column(Text, nullable=False)
    raw_json: Mapped[str] = mapped_column(Text, nullable=False)

    __table_args__ = (
        Index("ix_meta_training_ticker_model_created", "ticker", "model_name", "created_at"),
    )


class MetaModelPerformance(Base):
    __tablename__ = "meta_model_performance"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    generated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        index=True,
    )
    lookback_days: Mapped[int] = mapped_column(Integer, nullable=False)
    evaluated_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    meta_brier_score: Mapped[str | None] = mapped_column(String(80))
    ensemble_brier_score: Mapped[str | None] = mapped_column(String(80))
    market_implied_brier_score: Mapped[str | None] = mapped_column(String(80))
    meta_log_loss: Mapped[str | None] = mapped_column(String(80))
    ensemble_log_loss: Mapped[str | None] = mapped_column(String(80))
    market_implied_log_loss: Mapped[str | None] = mapped_column(String(80))
    meta_roi: Mapped[str | None] = mapped_column(String(80))
    ensemble_roi: Mapped[str | None] = mapped_column(String(80))
    market_implied_roi: Mapped[str | None] = mapped_column(String(80))
    notes: Mapped[str] = mapped_column(Text, nullable=False)
    raw_json: Mapped[str] = mapped_column(Text, nullable=False)


class MarketMemory(Base):
    __tablename__ = "market_memory"

    market_memory_id: Mapped[str] = mapped_column(String(80), primary_key=True)
    event_type: Mapped[str] = mapped_column(String(80), nullable=False, index=True)
    event_sequence: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    event_version: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    schema_version: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    event_time: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, index=True
    )
    observed_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    recorded_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, index=True
    )
    trace_id: Mapped[str | None] = mapped_column(String(200), index=True)
    correlation_id: Mapped[str | None] = mapped_column(String(200), index=True)
    causation_id: Mapped[str | None] = mapped_column(String(200), index=True)
    source_component: Mapped[str] = mapped_column(String(120), nullable=False, index=True)
    source_event_id: Mapped[str | None] = mapped_column(String(200), index=True)
    idempotency_key: Mapped[str] = mapped_column(String(300), nullable=False, unique=True)
    payload_hash: Mapped[str] = mapped_column(String(120), nullable=False)
    is_correction: Mapped[int] = mapped_column(Integer, nullable=False, default=0, index=True)
    supersedes_memory_event_id: Mapped[str | None] = mapped_column(String(80), index=True)
    metadata_json: Mapped[str] = mapped_column(Text, nullable=False)

    instrument_id: Mapped[str] = mapped_column(String(128), nullable=False, index=True)
    venue_id: Mapped[str | None] = mapped_column(String(80))
    asset_class: Mapped[str | None] = mapped_column(String(80))
    category_id: Mapped[str | None] = mapped_column(String(100), index=True)
    correlation_group_id: Mapped[str | None] = mapped_column(String(128), index=True)
    contract_id: Mapped[str | None] = mapped_column(String(128), index=True)
    contract_expiry: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), index=True)
    timeframe: Mapped[str] = mapped_column(String(80), nullable=False, index=True)
    snapshot_type: Mapped[str] = mapped_column(String(80), nullable=False, index=True)
    market_event_time: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        index=True,
    )
    source_name: Mapped[str] = mapped_column(String(120), nullable=False, index=True)
    source_sequence: Mapped[str | None] = mapped_column(String(200), index=True)
    provider_message_id: Mapped[str | None] = mapped_column(String(200), index=True)
    provider_latency_ms: Mapped[int | None] = mapped_column(Integer)
    bid_price: Mapped[str | None] = mapped_column(String(80))
    ask_price: Mapped[str | None] = mapped_column(String(80))
    mid_price: Mapped[str | None] = mapped_column(String(80))
    last_price: Mapped[str | None] = mapped_column(String(80))
    mark_price: Mapped[str | None] = mapped_column(String(80))
    settlement_price: Mapped[str | None] = mapped_column(String(80))
    bid_size: Mapped[str | None] = mapped_column(String(80))
    ask_size: Mapped[str | None] = mapped_column(String(80))
    spread_absolute: Mapped[str | None] = mapped_column(String(80))
    spread_bps: Mapped[str | None] = mapped_column(String(80))
    volume: Mapped[str | None] = mapped_column(String(80))
    open_interest: Mapped[str | None] = mapped_column(String(80))
    depth_bid_notional: Mapped[str | None] = mapped_column(String(80))
    depth_ask_notional: Mapped[str | None] = mapped_column(String(80))
    executable_liquidity_contracts: Mapped[int | None] = mapped_column(Integer)
    volatility: Mapped[str | None] = mapped_column(String(80))
    realized_volatility: Mapped[str | None] = mapped_column(String(80))
    implied_volatility: Mapped[str | None] = mapped_column(String(80))
    liquidity_score: Mapped[str | None] = mapped_column(String(80))
    market_regime: Mapped[str | None] = mapped_column(String(120))
    session_id: Mapped[str | None] = mapped_column(String(120), index=True)
    trading_status: Mapped[str | None] = mapped_column(String(120), index=True)
    bar_open: Mapped[str | None] = mapped_column(String(80))
    bar_high: Mapped[str | None] = mapped_column(String(80))
    bar_low: Mapped[str | None] = mapped_column(String(80))
    bar_close: Mapped[str | None] = mapped_column(String(80))
    bar_volume: Mapped[str | None] = mapped_column(String(80))
    feature_schema_version: Mapped[str | None] = mapped_column(String(120))
    feature_values_json: Mapped[str] = mapped_column(Text, nullable=False)
    raw_payload_uri: Mapped[str | None] = mapped_column(Text)
    raw_payload_hash: Mapped[str | None] = mapped_column(String(120))
    data_mode: Mapped[str] = mapped_column(String(40), nullable=False, default="AS_OBSERVED")
    ingestion_mode: Mapped[str] = mapped_column(String(40), nullable=False, default="LIVE")
    data_quality_flags_json: Mapped[str] = mapped_column(Text, nullable=False)
    event_payload_json: Mapped[str] = mapped_column(Text, nullable=False)

    __table_args__ = (
        Index("ix_market_memory_instrument_time", "instrument_id", "market_event_time"),
        Index("ix_market_memory_snapshot_time", "snapshot_type", "market_event_time"),
        Index("ix_market_memory_recorded", "recorded_at"),
    )


class ForecastMemory(Base):
    __tablename__ = "forecast_memory"

    forecast_memory_event_id: Mapped[str] = mapped_column(String(80), primary_key=True)
    forecast_id: Mapped[str] = mapped_column(String(120), nullable=False, index=True)
    event_type: Mapped[str] = mapped_column(String(80), nullable=False, index=True)
    event_sequence: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    event_version: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    schema_version: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    event_time: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, index=True
    )
    observed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    recorded_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, index=True
    )
    trace_id: Mapped[str | None] = mapped_column(String(200), index=True)
    correlation_id: Mapped[str | None] = mapped_column(String(200), index=True)
    causation_id: Mapped[str | None] = mapped_column(String(200), index=True)
    source_component: Mapped[str] = mapped_column(String(120), nullable=False, index=True)
    source_event_id: Mapped[str | None] = mapped_column(String(200), index=True)
    idempotency_key: Mapped[str] = mapped_column(String(300), nullable=False, unique=True)
    payload_hash: Mapped[str] = mapped_column(String(120), nullable=False)
    is_correction: Mapped[int] = mapped_column(Integer, nullable=False, default=0, index=True)
    supersedes_memory_event_id: Mapped[str | None] = mapped_column(String(80), index=True)
    metadata_json: Mapped[str] = mapped_column(Text, nullable=False)

    opportunity_id: Mapped[str | None] = mapped_column(String(120), index=True)
    signal_id: Mapped[str | None] = mapped_column(String(120), index=True)
    market_memory_id: Mapped[str | None] = mapped_column(String(80), index=True)
    outcome_market_memory_id: Mapped[str | None] = mapped_column(String(80), index=True)
    instrument_id: Mapped[str] = mapped_column(String(128), nullable=False, index=True)
    venue_id: Mapped[str | None] = mapped_column(String(80))
    category_id: Mapped[str | None] = mapped_column(String(100), index=True)
    strategy_id: Mapped[str] = mapped_column(String(120), nullable=False, index=True)
    setup_id: Mapped[str | None] = mapped_column(String(120))
    timeframe: Mapped[str] = mapped_column(String(80), nullable=False, index=True)
    direction: Mapped[str | None] = mapped_column(String(40), index=True)
    forecast_generated_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), index=True
    )
    forecast_valid_from: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    forecast_target_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), index=True)
    forecast_horizon_seconds: Mapped[int | None] = mapped_column(Integer)
    forecast_type: Mapped[str | None] = mapped_column(String(80))
    predicted_value: Mapped[str | None] = mapped_column(String(80))
    predicted_return: Mapped[str | None] = mapped_column(String(80))
    predicted_probability: Mapped[str | None] = mapped_column(String(80))
    probability_up: Mapped[str | None] = mapped_column(String(80))
    probability_down: Mapped[str | None] = mapped_column(String(80))
    probability_flat: Mapped[str | None] = mapped_column(String(80))
    prediction_lower_bound: Mapped[str | None] = mapped_column(String(80))
    prediction_upper_bound: Mapped[str | None] = mapped_column(String(80))
    uncertainty_score: Mapped[str | None] = mapped_column(String(80))
    confidence_score: Mapped[str | None] = mapped_column(String(80))
    opportunity_score: Mapped[str | None] = mapped_column(String(80), index=True)
    liquidity_score: Mapped[str | None] = mapped_column(String(80))
    raw_expected_value: Mapped[str | None] = mapped_column(String(80))
    risk_adjusted_expected_value: Mapped[str | None] = mapped_column(String(80))
    eligibility_status: Mapped[str | None] = mapped_column(String(80), index=True)
    decision_status: Mapped[str | None] = mapped_column(String(80), index=True)
    reason_codes_json: Mapped[str] = mapped_column(Text, nullable=False)
    phase_3m_decision_id: Mapped[str | None] = mapped_column(String(120), index=True)
    phase_3m_tier: Mapped[str | None] = mapped_column(String(50), index=True)
    phase_3m_proposed_contracts: Mapped[int | None] = mapped_column(Integer)
    phase_3m_composite_score: Mapped[str | None] = mapped_column(String(80))
    phase_3m_config_version: Mapped[str | None] = mapped_column(String(80))
    phase_3n_decision_id: Mapped[str | None] = mapped_column(String(120), index=True)
    phase_3n_action: Mapped[str | None] = mapped_column(String(40), index=True)
    phase_3n_approved_contracts: Mapped[int | None] = mapped_column(Integer)
    phase_3n_reason_codes_json: Mapped[str] = mapped_column(Text, nullable=False)
    phase_3n_config_version: Mapped[str | None] = mapped_column(String(80))
    primary_model_id: Mapped[str | None] = mapped_column(String(120), index=True)
    primary_model_family: Mapped[str | None] = mapped_column(String(120))
    primary_model_version: Mapped[str | None] = mapped_column(String(120), index=True)
    primary_model_artifact_hash: Mapped[str | None] = mapped_column(String(120))
    primary_model_artifact_uri: Mapped[str | None] = mapped_column(Text)
    model_run_id: Mapped[str | None] = mapped_column(String(120), index=True)
    training_run_id: Mapped[str | None] = mapped_column(String(120), index=True)
    training_data_cutoff: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    feature_set_id: Mapped[str | None] = mapped_column(String(120))
    feature_schema_version: Mapped[str | None] = mapped_column(String(120))
    feature_vector_hash: Mapped[str | None] = mapped_column(String(120))
    feature_observed_through: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), index=True
    )
    feature_computation_version: Mapped[str | None] = mapped_column(String(120))
    calibration_version: Mapped[str | None] = mapped_column(String(120))
    inference_runtime_version: Mapped[str | None] = mapped_column(String(120))
    code_commit_sha: Mapped[str | None] = mapped_column(String(120))
    configuration_version: Mapped[str | None] = mapped_column(String(120))
    model_lineage_json: Mapped[str] = mapped_column(Text, nullable=False)
    feature_lineage_json: Mapped[str] = mapped_column(Text, nullable=False)
    forecast_outcome_status: Mapped[str] = mapped_column(
        String(80), nullable=False, default="PENDING", index=True
    )
    label_policy_id: Mapped[str | None] = mapped_column(String(120))
    label_policy_version: Mapped[str | None] = mapped_column(String(120))
    label_available_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), index=True)
    outcome_finalized_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), index=True
    )
    actual_value: Mapped[str | None] = mapped_column(String(80))
    actual_return: Mapped[str | None] = mapped_column(String(80))
    direction_correct: Mapped[int | None] = mapped_column(Integer)
    forecast_error: Mapped[str | None] = mapped_column(String(80))
    absolute_error: Mapped[str | None] = mapped_column(String(80))
    squared_error: Mapped[str | None] = mapped_column(String(80))
    brier_component: Mapped[str | None] = mapped_column(String(80))
    max_favorable_excursion: Mapped[str | None] = mapped_column(String(80))
    max_adverse_excursion: Mapped[str | None] = mapped_column(String(80))
    outcome_class: Mapped[str | None] = mapped_column(String(80), index=True)
    censor_reason: Mapped[str | None] = mapped_column(String(200))
    ingestion_mode: Mapped[str] = mapped_column(String(40), nullable=False, default="LIVE")
    data_quality_flags_json: Mapped[str] = mapped_column(Text, nullable=False)
    event_payload_json: Mapped[str] = mapped_column(Text, nullable=False)

    __table_args__ = (
        UniqueConstraint("forecast_id", "event_sequence", name="uq_forecast_memory_sequence"),
        Index("ix_forecast_memory_model_generated", "primary_model_id", "forecast_generated_at"),
        Index("ix_forecast_memory_target_status", "forecast_target_at", "forecast_outcome_status"),
    )


class TradeMemory(Base):
    __tablename__ = "trade_memory"

    trade_memory_event_id: Mapped[str] = mapped_column(String(80), primary_key=True)
    trade_id: Mapped[str] = mapped_column(String(120), nullable=False, index=True)
    event_type: Mapped[str] = mapped_column(String(80), nullable=False, index=True)
    event_sequence: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    event_version: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    schema_version: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    event_time: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, index=True
    )
    observed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    recorded_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, index=True
    )
    trace_id: Mapped[str | None] = mapped_column(String(200), index=True)
    correlation_id: Mapped[str | None] = mapped_column(String(200), index=True)
    causation_id: Mapped[str | None] = mapped_column(String(200), index=True)
    source_component: Mapped[str] = mapped_column(String(120), nullable=False, index=True)
    source_event_id: Mapped[str | None] = mapped_column(String(200), index=True)
    idempotency_key: Mapped[str] = mapped_column(String(300), nullable=False, unique=True)
    payload_hash: Mapped[str] = mapped_column(String(120), nullable=False)
    is_correction: Mapped[int] = mapped_column(Integer, nullable=False, default=0, index=True)
    supersedes_memory_event_id: Mapped[str | None] = mapped_column(String(80), index=True)
    metadata_json: Mapped[str] = mapped_column(Text, nullable=False)

    forecast_id: Mapped[str | None] = mapped_column(String(120), index=True)
    origin_forecast_memory_event_id: Mapped[str | None] = mapped_column(String(80), index=True)
    opportunity_id: Mapped[str | None] = mapped_column(String(120), index=True)
    trade_intent_id: Mapped[str | None] = mapped_column(String(200), index=True)
    order_correlation_id: Mapped[str | None] = mapped_column(String(200), index=True)
    order_id: Mapped[str | None] = mapped_column(String(120), index=True)
    fill_id: Mapped[str | None] = mapped_column(String(120), index=True)
    position_id: Mapped[str | None] = mapped_column(String(120), index=True)
    settlement_id: Mapped[str | None] = mapped_column(String(120), index=True)
    execution_mode: Mapped[str] = mapped_column(String(40), nullable=False, index=True)
    instrument_id: Mapped[str] = mapped_column(String(128), nullable=False, index=True)
    venue_id: Mapped[str | None] = mapped_column(String(80))
    category_id: Mapped[str | None] = mapped_column(String(100), index=True)
    strategy_id: Mapped[str] = mapped_column(String(120), nullable=False, index=True)
    model_id: Mapped[str | None] = mapped_column(String(120), index=True)
    model_version: Mapped[str | None] = mapped_column(String(120))
    model_lineage_json: Mapped[str] = mapped_column(Text, nullable=False)
    direction: Mapped[str] = mapped_column(String(40), nullable=False, index=True)
    order_type: Mapped[str | None] = mapped_column(String(80))
    time_in_force: Mapped[str | None] = mapped_column(String(80))
    phase_3m_proposed_contracts: Mapped[int | None] = mapped_column(Integer)
    phase_3n_approved_contracts: Mapped[int | None] = mapped_column(Integer)
    requested_quantity: Mapped[int | None] = mapped_column(Integer)
    accepted_quantity: Mapped[int | None] = mapped_column(Integer)
    filled_quantity: Mapped[int | None] = mapped_column(Integer)
    open_quantity: Mapped[int | None] = mapped_column(Integer)
    closed_quantity: Mapped[int | None] = mapped_column(Integer)
    intended_entry_price: Mapped[str | None] = mapped_column(String(80))
    submitted_price: Mapped[str | None] = mapped_column(String(80))
    fill_price: Mapped[str | None] = mapped_column(String(80))
    average_entry_price: Mapped[str | None] = mapped_column(String(80))
    stop_price: Mapped[str | None] = mapped_column(String(80))
    target_price: Mapped[str | None] = mapped_column(String(80))
    mark_price: Mapped[str | None] = mapped_column(String(80))
    exit_price: Mapped[str | None] = mapped_column(String(80))
    settlement_price: Mapped[str | None] = mapped_column(String(80))
    point_value: Mapped[str | None] = mapped_column(String(80))
    tick_size: Mapped[str | None] = mapped_column(String(80))
    gross_notional: Mapped[str | None] = mapped_column(String(80))
    risk_per_contract: Mapped[str | None] = mapped_column(String(80))
    committed_risk: Mapped[str | None] = mapped_column(String(80))
    confidence_score: Mapped[str | None] = mapped_column(String(80))
    opportunity_score: Mapped[str | None] = mapped_column(String(80), index=True)
    kelly_fraction: Mapped[str | None] = mapped_column(String(80))
    risk_adjusted_expected_value: Mapped[str | None] = mapped_column(String(80))
    commission: Mapped[str | None] = mapped_column(String(80))
    exchange_fees: Mapped[str | None] = mapped_column(String(80))
    estimated_slippage: Mapped[str | None] = mapped_column(String(80))
    realized_slippage: Mapped[str | None] = mapped_column(String(80))
    borrow_or_carry_cost: Mapped[str | None] = mapped_column(String(80))
    total_cost: Mapped[str | None] = mapped_column(String(80))
    paper_fill_model_id: Mapped[str | None] = mapped_column(String(120))
    paper_fill_model_version: Mapped[str | None] = mapped_column(String(120))
    paper_latency_ms: Mapped[int | None] = mapped_column(Integer)
    paper_fill_policy_json: Mapped[str] = mapped_column(Text, nullable=False)
    settlement_status: Mapped[str | None] = mapped_column(String(80), index=True)
    settlement_source: Mapped[str | None] = mapped_column(String(120))
    settlement_reference: Mapped[str | None] = mapped_column(String(200))
    settlement_version: Mapped[str | None] = mapped_column(String(120))
    settled_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), index=True)
    gross_pnl: Mapped[str | None] = mapped_column(String(80))
    net_pnl: Mapped[str | None] = mapped_column(String(80))
    pnl_currency: Mapped[str | None] = mapped_column(String(40))
    return_fraction: Mapped[str | None] = mapped_column(String(80))
    r_multiple: Mapped[str | None] = mapped_column(String(80))
    max_favorable_excursion: Mapped[str | None] = mapped_column(String(80))
    max_adverse_excursion: Mapped[str | None] = mapped_column(String(80))
    holding_period_seconds: Mapped[int | None] = mapped_column(Integer)
    outcome_class: Mapped[str | None] = mapped_column(String(80), index=True)
    outcome_finalized_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), index=True
    )
    outcome_reason_codes_json: Mapped[str] = mapped_column(Text, nullable=False)
    market_memory_id: Mapped[str | None] = mapped_column(String(80), index=True)
    unmodeled_reason_code: Mapped[str | None] = mapped_column(String(120))
    ingestion_mode: Mapped[str] = mapped_column(String(40), nullable=False, default="LIVE")
    data_quality_flags_json: Mapped[str] = mapped_column(Text, nullable=False)
    event_payload_json: Mapped[str] = mapped_column(Text, nullable=False)

    __table_args__ = (
        UniqueConstraint("trade_id", "event_sequence", name="uq_trade_memory_sequence"),
        Index("ix_trade_memory_forecast", "forecast_id", "event_time"),
        Index("ix_trade_memory_order", "order_id", "event_time"),
        Index(
            "ix_trade_memory_mode_instrument_time", "execution_mode", "instrument_id", "event_time"
        ),
    )


class MemoryEventQuarantine(Base):
    __tablename__ = "memory_event_quarantine"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    store: Mapped[str] = mapped_column(String(40), nullable=False, index=True)
    idempotency_key: Mapped[str] = mapped_column(String(300), nullable=False, index=True)
    attempted_payload_hash: Mapped[str] = mapped_column(String(120), nullable=False)
    existing_payload_hash: Mapped[str | None] = mapped_column(String(120))
    reason: Mapped[str] = mapped_column(String(200), nullable=False, index=True)
    source_component: Mapped[str | None] = mapped_column(String(120), index=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, index=True
    )
    raw_json: Mapped[str] = mapped_column(Text, nullable=False)


class MemoryArchiveManifest(Base):
    __tablename__ = "memory_archive_manifests"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    archive_id: Mapped[str] = mapped_column(String(120), nullable=False, unique=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, index=True
    )
    output_uri: Mapped[str] = mapped_column(Text, nullable=False)
    status: Mapped[str] = mapped_column(String(40), nullable=False, index=True)
    row_counts_json: Mapped[str] = mapped_column(Text, nullable=False)
    checksums_json: Mapped[str] = mapped_column(Text, nullable=False)
    source_range_json: Mapped[str] = mapped_column(Text, nullable=False)
    raw_json: Mapped[str] = mapped_column(Text, nullable=False)


class SelfEvaluationRun(Base):
    __tablename__ = "self_evaluation_runs"

    evaluation_run_id: Mapped[str] = mapped_column(String(120), primary_key=True)
    trading_session_id: Mapped[str] = mapped_column(String(120), nullable=False, index=True)
    session_label: Mapped[str] = mapped_column(String(120), nullable=False, index=True)
    session_timezone: Mapped[str] = mapped_column(String(80), nullable=False)
    session_open_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    session_close_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    evaluation_as_of: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, index=True
    )
    generated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, index=True
    )
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    run_type: Mapped[str] = mapped_column(String(80), nullable=False, default="NIGHTLY", index=True)
    status: Mapped[str] = mapped_column(String(80), nullable=False, index=True)
    policy_id: Mapped[str] = mapped_column(String(120), nullable=False, index=True)
    policy_version: Mapped[str] = mapped_column(String(80), nullable=False, index=True)
    data_mode: Mapped[str] = mapped_column(String(40), nullable=False, default="AS_OBSERVED")
    source_manifest_hash: Mapped[str] = mapped_column(String(120), nullable=False, index=True)
    input_checksum: Mapped[str] = mapped_column(String(120), nullable=False)
    journal_id: Mapped[str | None] = mapped_column(String(120), index=True)
    journal_revision: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    summary_json: Mapped[str] = mapped_column(Text, nullable=False)
    source_manifest_json: Mapped[str] = mapped_column(Text, nullable=False)
    raw_json: Mapped[str] = mapped_column(Text, nullable=False)

    __table_args__ = (
        UniqueConstraint(
            "trading_session_id",
            "evaluation_as_of",
            "policy_id",
            "policy_version",
            "data_mode",
            "source_manifest_hash",
            name="uq_self_eval_run_manifest",
        ),
        Index("ix_self_eval_run_session_status", "trading_session_id", "status"),
    )


class SelfEvaluationMetric(Base):
    __tablename__ = "self_evaluation_metrics"

    metric_record_id: Mapped[str] = mapped_column(String(120), primary_key=True)
    evaluation_run_id: Mapped[str] = mapped_column(
        ForeignKey("self_evaluation_runs.evaluation_run_id"), nullable=False, index=True
    )
    trading_session_id: Mapped[str] = mapped_column(String(120), nullable=False, index=True)
    metric_name: Mapped[str] = mapped_column(String(160), nullable=False, index=True)
    metric_version: Mapped[str] = mapped_column(String(80), nullable=False)
    metric_type: Mapped[str] = mapped_column(String(80), nullable=False, index=True)
    section: Mapped[str] = mapped_column(String(120), nullable=False, index=True)
    cohort_json: Mapped[str] = mapped_column(Text, nullable=False)
    value: Mapped[str | None] = mapped_column(String(120))
    unit: Mapped[str | None] = mapped_column(String(80))
    sample_size: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    finalized_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    pending_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    baseline_json: Mapped[str] = mapped_column(Text, nullable=False)
    reliability_grade: Mapped[str] = mapped_column(String(80), nullable=False, index=True)
    evidence_json: Mapped[str] = mapped_column(Text, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    raw_json: Mapped[str] = mapped_column(Text, nullable=False)

    __table_args__ = (
        Index("ix_self_eval_metric_run_name", "evaluation_run_id", "metric_name"),
        Index("ix_self_eval_metric_session_name", "trading_session_id", "metric_name"),
    )


class SelfEvaluationFinding(Base):
    __tablename__ = "self_evaluation_findings"

    finding_id: Mapped[str] = mapped_column(String(120), primary_key=True)
    evaluation_run_id: Mapped[str] = mapped_column(
        ForeignKey("self_evaluation_runs.evaluation_run_id"), nullable=False, index=True
    )
    trading_session_id: Mapped[str] = mapped_column(String(120), nullable=False, index=True)
    finding_type: Mapped[str] = mapped_column(String(80), nullable=False, index=True)
    finding_subtype: Mapped[str] = mapped_column(String(120), nullable=False, index=True)
    severity: Mapped[str] = mapped_column(String(80), nullable=False, index=True)
    status: Mapped[str] = mapped_column(String(80), nullable=False, index=True)
    title: Mapped[str] = mapped_column(String(300), nullable=False)
    concise_statement: Mapped[str] = mapped_column(Text, nullable=False)
    detailed_explanation: Mapped[str] = mapped_column(Text, nullable=False)
    primary_metric_record_id: Mapped[str | None] = mapped_column(String(120), index=True)
    sample_size: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    current_value: Mapped[str | None] = mapped_column(String(120))
    baseline_value: Mapped[str | None] = mapped_column(String(120))
    effect_size: Mapped[str | None] = mapped_column(String(120))
    reliability_grade: Mapped[str] = mapped_column(String(80), nullable=False, index=True)
    evidence_type: Mapped[str] = mapped_column(String(80), nullable=False, index=True)
    attribution_level: Mapped[str] = mapped_column(String(80), nullable=False, index=True)
    evidence_references_json: Mapped[str] = mapped_column(Text, nullable=False)
    reason_codes_json: Mapped[str] = mapped_column(Text, nullable=False)
    recommended_follow_up_ids_json: Mapped[str] = mapped_column(Text, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    raw_json: Mapped[str] = mapped_column(Text, nullable=False)

    __table_args__ = (
        Index("ix_self_eval_finding_run_type", "evaluation_run_id", "finding_type"),
        Index("ix_self_eval_finding_session_type", "trading_session_id", "finding_type"),
    )


class SelfEvaluationJournal(Base):
    __tablename__ = "self_evaluation_journals"

    journal_id: Mapped[str] = mapped_column(String(120), primary_key=True)
    evaluation_run_id: Mapped[str] = mapped_column(
        ForeignKey("self_evaluation_runs.evaluation_run_id"), nullable=False, index=True
    )
    trading_session_id: Mapped[str] = mapped_column(String(120), nullable=False, index=True)
    journal_revision: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    journal_status: Mapped[str] = mapped_column(String(80), nullable=False, index=True)
    schema_version: Mapped[str] = mapped_column(String(80), nullable=False)
    policy_id: Mapped[str] = mapped_column(String(120), nullable=False, index=True)
    policy_version: Mapped[str] = mapped_column(String(80), nullable=False, index=True)
    generated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    evaluation_as_of: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    payload_checksum: Mapped[str] = mapped_column(String(120), nullable=False, index=True)
    markdown_checksum: Mapped[str] = mapped_column(String(120), nullable=False)
    markdown_path: Mapped[str | None] = mapped_column(Text)
    payload_json: Mapped[str] = mapped_column(Text, nullable=False)
    supersedes_journal_id: Mapped[str | None] = mapped_column(String(120), index=True)
    revision_reason_codes_json: Mapped[str] = mapped_column(Text, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)

    __table_args__ = (
        UniqueConstraint(
            "trading_session_id",
            "policy_id",
            "policy_version",
            "journal_revision",
            name="uq_self_eval_journal_revision",
        ),
        Index("ix_self_eval_journal_session_status", "trading_session_id", "journal_status"),
    )


class FeatureDiscoveryRun(Base):
    __tablename__ = "feature_discovery_run"

    run_id: Mapped[str] = mapped_column(String(120), primary_key=True)
    run_type: Mapped[str] = mapped_column(String(80), nullable=False, index=True)
    status: Mapped[str] = mapped_column(String(80), nullable=False, index=True)
    requested_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), index=True)
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), index=True)
    training_as_of: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        index=True,
    )
    source_watermarks_json: Mapped[str] = mapped_column(Text, nullable=False)
    source_manifest_id: Mapped[str] = mapped_column(String(120), nullable=False, index=True)
    dataset_spec_hash: Mapped[str] = mapped_column(String(120), nullable=False, index=True)
    candidate_policy_id: Mapped[str] = mapped_column(String(120), nullable=False, index=True)
    candidate_grammar_version: Mapped[str] = mapped_column(String(120), nullable=False)
    evaluation_policy_id: Mapped[str] = mapped_column(String(120), nullable=False, index=True)
    statistical_policy_id: Mapped[str] = mapped_column(String(120), nullable=False)
    holdout_policy_id: Mapped[str] = mapped_column(String(120), nullable=False)
    code_commit_sha: Mapped[str | None] = mapped_column(String(120))
    configuration_version: Mapped[str] = mapped_column(String(120), nullable=False)
    random_seed_manifest_json: Mapped[str] = mapped_column(Text, nullable=False)
    candidate_counts_json: Mapped[str] = mapped_column(Text, nullable=False)
    failure_reason_codes_json: Mapped[str] = mapped_column(Text, nullable=False)
    artifact_uris_json: Mapped[str] = mapped_column(Text, nullable=False)
    idempotency_key: Mapped[str] = mapped_column(String(300), nullable=False, unique=True)
    raw_json: Mapped[str] = mapped_column(Text, nullable=False)

    __table_args__ = (
        Index("ix_feature_discovery_run_type_status", "run_type", "status"),
        Index("ix_feature_discovery_training", "training_as_of", "status"),
    )


class FeatureCandidate(Base):
    __tablename__ = "feature_candidate"

    candidate_id: Mapped[str] = mapped_column(String(120), primary_key=True)
    feature_definition_id: Mapped[str] = mapped_column(String(120), nullable=False, index=True)
    candidate_batch_id: Mapped[str] = mapped_column(String(120), nullable=False, index=True)
    parent_candidate_ids_json: Mapped[str] = mapped_column(Text, nullable=False)
    origin: Mapped[str] = mapped_column(String(80), nullable=False, index=True)
    created_by_run_id: Mapped[str] = mapped_column(
        ForeignKey("feature_discovery_run.run_id"),
        nullable=False,
        index=True,
    )
    status: Mapped[str] = mapped_column(String(80), nullable=False, index=True)
    status_reason_codes_json: Mapped[str] = mapped_column(Text, nullable=False)
    supersedes_candidate_id: Mapped[str | None] = mapped_column(String(120), index=True)
    feature_name: Mapped[str] = mapped_column(String(200), nullable=False, index=True)
    feature_family: Mapped[str] = mapped_column(String(160), nullable=False, index=True)
    expression_json: Mapped[str] = mapped_column(Text, nullable=False)
    lineage_json: Mapped[str] = mapped_column(Text, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    raw_json: Mapped[str] = mapped_column(Text, nullable=False)

    __table_args__ = (
        Index("ix_feature_candidate_run_status", "created_by_run_id", "status"),
        Index("ix_feature_candidate_family_status", "feature_family", "status"),
    )


class FeatureEvaluation(Base):
    __tablename__ = "feature_evaluation"

    evaluation_id: Mapped[str] = mapped_column(String(120), primary_key=True)
    candidate_id: Mapped[str] = mapped_column(
        ForeignKey("feature_candidate.candidate_id"),
        nullable=False,
        index=True,
    )
    run_id: Mapped[str] = mapped_column(
        ForeignKey("feature_discovery_run.run_id"),
        nullable=False,
        index=True,
    )
    outcome_name: Mapped[str] = mapped_column(String(160), nullable=False, index=True)
    cohort_json: Mapped[str] = mapped_column(Text, nullable=False)
    model_family: Mapped[str | None] = mapped_column(String(120), index=True)
    evaluation_policy_id: Mapped[str] = mapped_column(String(120), nullable=False)
    baseline_metrics_json: Mapped[str] = mapped_column(Text, nullable=False)
    candidate_metrics_json: Mapped[str] = mapped_column(Text, nullable=False)
    paired_deltas_json: Mapped[str] = mapped_column(Text, nullable=False)
    intervals_json: Mapped[str] = mapped_column(Text, nullable=False)
    significance_json: Mapped[str] = mapped_column(Text, nullable=False)
    stability_json: Mapped[str] = mapped_column(Text, nullable=False)
    evidence_links_json: Mapped[str] = mapped_column(Text, nullable=False)
    status: Mapped[str] = mapped_column(String(80), nullable=False, index=True)
    composite_score: Mapped[str] = mapped_column(String(80), nullable=False, index=True)
    reason_codes_json: Mapped[str] = mapped_column(Text, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    raw_json: Mapped[str] = mapped_column(Text, nullable=False)

    __table_args__ = (
        Index("ix_feature_evaluation_run_status", "run_id", "status"),
        Index("ix_feature_evaluation_candidate_outcome", "candidate_id", "outcome_name"),
    )


class FeatureFoldResult(Base):
    __tablename__ = "feature_fold_result"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    evaluation_id: Mapped[str] = mapped_column(
        ForeignKey("feature_evaluation.evaluation_id"),
        nullable=False,
        index=True,
    )
    run_id: Mapped[str] = mapped_column(
        ForeignKey("feature_discovery_run.run_id"),
        nullable=False,
        index=True,
    )
    candidate_id: Mapped[str] = mapped_column(String(120), nullable=False, index=True)
    fold_id: Mapped[str] = mapped_column(String(120), nullable=False, index=True)
    train_start: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    train_end: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    validation_start: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    validation_end: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    train_sample_size: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    validation_sample_size: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    metrics_json: Mapped[str] = mapped_column(Text, nullable=False)
    raw_json: Mapped[str] = mapped_column(Text, nullable=False)

    __table_args__ = (Index("ix_feature_fold_eval_fold", "evaluation_id", "fold_id"),)


class FeatureSegmentResult(Base):
    __tablename__ = "feature_segment_result"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    evaluation_id: Mapped[str] = mapped_column(
        ForeignKey("feature_evaluation.evaluation_id"),
        nullable=False,
        index=True,
    )
    run_id: Mapped[str] = mapped_column(
        ForeignKey("feature_discovery_run.run_id"),
        nullable=False,
        index=True,
    )
    candidate_id: Mapped[str] = mapped_column(String(120), nullable=False, index=True)
    segment_key: Mapped[str] = mapped_column(String(120), nullable=False, index=True)
    segment_value: Mapped[str] = mapped_column(String(160), nullable=False, index=True)
    status: Mapped[str] = mapped_column(String(80), nullable=False, index=True)
    sample_size: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    metrics_json: Mapped[str] = mapped_column(Text, nullable=False)
    raw_json: Mapped[str] = mapped_column(Text, nullable=False)

    __table_args__ = (
        Index("ix_feature_segment_eval_segment", "evaluation_id", "segment_key", "segment_value"),
    )


class FeatureRelationship(Base):
    __tablename__ = "feature_relationship"

    relationship_id: Mapped[str] = mapped_column(String(120), primary_key=True)
    run_id: Mapped[str] = mapped_column(
        ForeignKey("feature_discovery_run.run_id"),
        nullable=False,
        index=True,
    )
    candidate_id: Mapped[str] = mapped_column(String(120), nullable=False, index=True)
    related_candidate_id: Mapped[str] = mapped_column(String(120), nullable=False, index=True)
    relationship_type: Mapped[str] = mapped_column(String(80), nullable=False, index=True)
    strength: Mapped[str | None] = mapped_column(String(80))
    reason_codes_json: Mapped[str] = mapped_column(Text, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    raw_json: Mapped[str] = mapped_column(Text, nullable=False)

    __table_args__ = (
        Index("ix_feature_relationship_run_type", "run_id", "relationship_type"),
    )


class FeatureRecommendation(Base):
    __tablename__ = "feature_recommendation"

    recommendation_id: Mapped[str] = mapped_column(String(120), primary_key=True)
    run_id: Mapped[str] = mapped_column(
        ForeignKey("feature_discovery_run.run_id"),
        nullable=False,
        index=True,
    )
    candidate_id: Mapped[str] = mapped_column(String(120), nullable=False, index=True)
    action: Mapped[str] = mapped_column(String(120), nullable=False, index=True)
    human_review_required: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    human_approval_reference: Mapped[str | None] = mapped_column(String(200), index=True)
    status: Mapped[str] = mapped_column(String(80), nullable=False, index=True)
    reason_codes_json: Mapped[str] = mapped_column(Text, nullable=False)
    experiment_spec_json: Mapped[str] = mapped_column(Text, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    raw_json: Mapped[str] = mapped_column(Text, nullable=False)

    __table_args__ = (
        Index("ix_feature_recommendation_run_action", "run_id", "action"),
    )


class FeatureHoldoutAccess(Base):
    __tablename__ = "feature_holdout_access"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    run_id: Mapped[str] = mapped_column(
        ForeignKey("feature_discovery_run.run_id"),
        nullable=False,
        index=True,
    )
    candidate_batch_id: Mapped[str] = mapped_column(String(120), nullable=False, index=True)
    accessed_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    holdout_policy_id: Mapped[str] = mapped_column(String(120), nullable=False)
    reason: Mapped[str] = mapped_column(Text, nullable=False)
    raw_json: Mapped[str] = mapped_column(Text, nullable=False)

    __table_args__ = (
        Index("ix_feature_holdout_run_batch", "run_id", "candidate_batch_id"),
    )


class SyntheticMarketRun(Base):
    __tablename__ = "synthetic_market_run"

    run_id: Mapped[str] = mapped_column(String(120), primary_key=True)
    run_type: Mapped[str] = mapped_column(String(80), nullable=False, index=True)
    mode: Mapped[str] = mapped_column(String(80), nullable=False, index=True)
    configuration_version: Mapped[str] = mapped_column(String(120), nullable=False)
    code_version: Mapped[str | None] = mapped_column(String(120))
    started_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), index=True)
    source_watermarks_json: Mapped[str] = mapped_column(Text, nullable=False)
    generation_policy_version: Mapped[str] = mapped_column(String(120), nullable=False)
    listing_policy_version: Mapped[str] = mapped_column(String(120), nullable=False)
    model_routing_version: Mapped[str] = mapped_column(String(120), nullable=False)
    constraint_policy_version: Mapped[str] = mapped_column(String(120), nullable=False)
    status: Mapped[str] = mapped_column(String(80), nullable=False, index=True)
    candidate_counts_json: Mapped[str] = mapped_column(Text, nullable=False)
    estimate_counts_json: Mapped[str] = mapped_column(Text, nullable=False)
    error_summary_json: Mapped[str] = mapped_column(Text, nullable=False)
    artifact_manifest_json: Mapped[str] = mapped_column(Text, nullable=False)
    idempotency_key: Mapped[str] = mapped_column(String(300), nullable=False, unique=True)
    raw_json: Mapped[str] = mapped_column(Text, nullable=False)

    __table_args__ = (
        Index("ix_synthetic_market_run_type_status", "run_type", "status"),
        Index("ix_synthetic_market_run_completed", "completed_at"),
    )


class SyntheticEventRegistry(Base):
    __tablename__ = "synthetic_event_registry"

    synthetic_event_id: Mapped[str] = mapped_column(String(120), primary_key=True)
    synthetic_event_version: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    semantic_hash: Mapped[str] = mapped_column(String(120), nullable=False, index=True)
    canonical_title: Mapped[str] = mapped_column(String(500), nullable=False, index=True)
    plain_language_summary: Mapped[str] = mapped_column(Text, nullable=False)
    category: Mapped[str] = mapped_column(String(100), nullable=False, index=True)
    subcategory: Mapped[str | None] = mapped_column(String(120), index=True)
    market_form: Mapped[str] = mapped_column(String(80), nullable=False, index=True)
    observation_start_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    observation_end_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    timezone: Mapped[str] = mapped_column(String(80), nullable=False)
    mutually_exclusive: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    collectively_exhaustive: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    settlement_rule_json: Mapped[str] = mapped_column(Text, nullable=False)
    generation_source: Mapped[str] = mapped_column(String(120), nullable=False, index=True)
    status: Mapped[str] = mapped_column(String(80), nullable=False, index=True)
    status_reason_codes_json: Mapped[str] = mapped_column(Text, nullable=False)
    created_by_run_id: Mapped[str] = mapped_column(
        ForeignKey("synthetic_market_run.run_id"),
        nullable=False,
        index=True,
    )
    supersedes_event_id: Mapped[str | None] = mapped_column(String(120), index=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    raw_json: Mapped[str] = mapped_column(Text, nullable=False)

    __table_args__ = (
        UniqueConstraint(
            "semantic_hash",
            "synthetic_event_version",
            name="uq_synthetic_event_semantic_version",
        ),
        Index("ix_synthetic_event_category_status", "category", "status"),
    )


class SyntheticContractRegistry(Base):
    __tablename__ = "synthetic_contract_registry"

    synthetic_contract_id: Mapped[str] = mapped_column(String(120), primary_key=True)
    synthetic_event_id: Mapped[str] = mapped_column(
        ForeignKey("synthetic_event_registry.synthetic_event_id"),
        nullable=False,
        index=True,
    )
    synthetic_contract_version: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    canonical_question: Mapped[str] = mapped_column(String(700), nullable=False, index=True)
    contract_type: Mapped[str] = mapped_column(String(80), nullable=False, index=True)
    outcome_code: Mapped[str] = mapped_column(String(120), nullable=False, index=True)
    condition_json: Mapped[str] = mapped_column(Text, nullable=False)
    complement_contract_id: Mapped[str | None] = mapped_column(String(120), index=True)
    constraint_group_id: Mapped[str | None] = mapped_column(String(120), index=True)
    status: Mapped[str] = mapped_column(String(80), nullable=False, index=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    raw_json: Mapped[str] = mapped_column(Text, nullable=False)

    __table_args__ = (
        Index("ix_synthetic_contract_event_status", "synthetic_event_id", "status"),
    )


class SyntheticListingCheck(Base):
    __tablename__ = "synthetic_listing_check"

    listing_check_id: Mapped[str] = mapped_column(String(120), primary_key=True)
    run_id: Mapped[str] = mapped_column(
        ForeignKey("synthetic_market_run.run_id"),
        nullable=False,
        index=True,
    )
    synthetic_event_id: Mapped[str] = mapped_column(String(120), nullable=False, index=True)
    checked_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    status: Mapped[str] = mapped_column(String(120), nullable=False, index=True)
    pagination_complete: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    live_coverage_complete: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    historical_coverage_status: Mapped[str] = mapped_column(String(120), nullable=False)
    historical_cutoff: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    warnings_json: Mapped[str] = mapped_column(Text, nullable=False)
    raw_json: Mapped[str] = mapped_column(Text, nullable=False)

    __table_args__ = (
        Index("ix_synthetic_listing_event_time", "synthetic_event_id", "checked_at"),
    )


class SyntheticListingMatch(Base):
    __tablename__ = "synthetic_listing_match"

    match_id: Mapped[str] = mapped_column(String(120), primary_key=True)
    listing_check_id: Mapped[str] = mapped_column(
        ForeignKey("synthetic_listing_check.listing_check_id"),
        nullable=False,
        index=True,
    )
    synthetic_event_id: Mapped[str] = mapped_column(String(120), nullable=False, index=True)
    kalshi_series_ticker: Mapped[str | None] = mapped_column(String(128), index=True)
    kalshi_event_ticker: Mapped[str | None] = mapped_column(String(128), index=True)
    kalshi_market_ticker: Mapped[str | None] = mapped_column(String(128), index=True)
    match_class: Mapped[str] = mapped_column(String(120), nullable=False, index=True)
    semantic_score: Mapped[str] = mapped_column(String(80), nullable=False)
    logical_comparison: Mapped[str] = mapped_column(Text, nullable=False)
    field_differences_json: Mapped[str] = mapped_column(Text, nullable=False)
    reviewer_status: Mapped[str] = mapped_column(String(120), nullable=False, index=True)
    effective_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), index=True)
    raw_json: Mapped[str] = mapped_column(Text, nullable=False)

    __table_args__ = (
        Index("ix_synthetic_listing_match_class", "match_class", "semantic_score"),
    )


class SyntheticProbabilityEstimate(Base):
    __tablename__ = "synthetic_probability_estimate"

    estimate_id: Mapped[str] = mapped_column(String(120), primary_key=True)
    estimate_version: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    run_id: Mapped[str] = mapped_column(
        ForeignKey("synthetic_market_run.run_id"),
        nullable=False,
        index=True,
    )
    synthetic_event_id: Mapped[str] = mapped_column(String(120), nullable=False, index=True)
    synthetic_contract_id: Mapped[str] = mapped_column(String(120), nullable=False, index=True)
    estimate_as_of: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    valid_until: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    raw_probability: Mapped[str] = mapped_column(String(80), nullable=False)
    coherent_probability: Mapped[str] = mapped_column(String(80), nullable=False)
    interval_json: Mapped[str] = mapped_column(Text, nullable=False)
    reliability_json: Mapped[str] = mapped_column(Text, nullable=False)
    status: Mapped[str] = mapped_column(String(80), nullable=False, index=True)
    card_json: Mapped[str] = mapped_column(Text, nullable=False)
    disclaimer: Mapped[str] = mapped_column(Text, nullable=False)
    lineage_json: Mapped[str] = mapped_column(Text, nullable=False)
    phase3o_receipts_json: Mapped[str] = mapped_column(Text, nullable=False)
    supersedes_estimate_id: Mapped[str | None] = mapped_column(String(120), index=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    raw_json: Mapped[str] = mapped_column(Text, nullable=False)

    __table_args__ = (
        Index("ix_synthetic_estimate_event_time", "synthetic_event_id", "estimate_as_of"),
        Index("ix_synthetic_estimate_status", "status", "estimate_as_of"),
    )


class SyntheticModelComponent(Base):
    __tablename__ = "synthetic_model_component"

    component_record_id: Mapped[str] = mapped_column(String(120), primary_key=True)
    estimate_id: Mapped[str] = mapped_column(
        ForeignKey("synthetic_probability_estimate.estimate_id"),
        nullable=False,
        index=True,
    )
    component_id: Mapped[str] = mapped_column(String(120), nullable=False, index=True)
    model_id: Mapped[str] = mapped_column(String(120), nullable=False, index=True)
    model_version: Mapped[str] = mapped_column(String(120), nullable=False)
    calibration_id: Mapped[str | None] = mapped_column(String(120), index=True)
    probability: Mapped[str] = mapped_column(String(80), nullable=False)
    weight: Mapped[str] = mapped_column(String(80), nullable=False)
    status: Mapped[str] = mapped_column(String(80), nullable=False, index=True)
    warnings_json: Mapped[str] = mapped_column(Text, nullable=False)
    runtime_ms: Mapped[int | None] = mapped_column(Integer)
    raw_json: Mapped[str] = mapped_column(Text, nullable=False)


class SyntheticConstraintResult(Base):
    __tablename__ = "synthetic_constraint_result"

    constraint_result_id: Mapped[str] = mapped_column(String(120), primary_key=True)
    estimate_id: Mapped[str] = mapped_column(
        ForeignKey("synthetic_probability_estimate.estimate_id"),
        nullable=False,
        index=True,
    )
    constraint_set_id: Mapped[str] = mapped_column(String(120), nullable=False, index=True)
    solver_id: Mapped[str] = mapped_column(String(120), nullable=False)
    solver_status: Mapped[str] = mapped_column(String(80), nullable=False, index=True)
    pre_values_json: Mapped[str] = mapped_column(Text, nullable=False)
    post_values_json: Mapped[str] = mapped_column(Text, nullable=False)
    maximum_adjustment: Mapped[str] = mapped_column(String(80), nullable=False)
    violations_before: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    violations_after: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    warnings_json: Mapped[str] = mapped_column(Text, nullable=False)
    raw_json: Mapped[str] = mapped_column(Text, nullable=False)


class SyntheticResolution(Base):
    __tablename__ = "synthetic_resolution"

    resolution_id: Mapped[str] = mapped_column(String(120), primary_key=True)
    synthetic_event_id: Mapped[str] = mapped_column(String(120), nullable=False, index=True)
    synthetic_contract_id: Mapped[str] = mapped_column(String(120), nullable=False, index=True)
    resolved_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    resolution_state: Mapped[str] = mapped_column(String(80), nullable=False, index=True)
    outcome_code: Mapped[str | None] = mapped_column(String(120), index=True)
    source_observations_json: Mapped[str] = mapped_column(Text, nullable=False)
    rule_application_json: Mapped[str] = mapped_column(Text, nullable=False)
    finality_policy: Mapped[str] = mapped_column(String(120), nullable=False)
    supersedes_resolution_id: Mapped[str | None] = mapped_column(String(120), index=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    raw_json: Mapped[str] = mapped_column(Text, nullable=False)


class SyntheticCalibrationResult(Base):
    __tablename__ = "synthetic_calibration_result"

    calibration_result_id: Mapped[str] = mapped_column(String(120), primary_key=True)
    estimate_id: Mapped[str | None] = mapped_column(String(120), index=True)
    synthetic_event_id: Mapped[str | None] = mapped_column(String(120), index=True)
    cohort_key: Mapped[str] = mapped_column(String(160), nullable=False, index=True)
    scored_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    brier_score: Mapped[str | None] = mapped_column(String(80))
    log_loss: Mapped[str | None] = mapped_column(String(80))
    calibration_error: Mapped[str | None] = mapped_column(String(80))
    sample_size: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    metrics_json: Mapped[str] = mapped_column(Text, nullable=False)
    raw_json: Mapped[str] = mapped_column(Text, nullable=False)


class RlRun(Base):
    __tablename__ = "rl_run"

    run_id: Mapped[str] = mapped_column(String(120), primary_key=True)
    run_type: Mapped[str] = mapped_column(String(80), nullable=False, index=True)
    formulation: Mapped[str] = mapped_column(String(80), nullable=False, index=True)
    mode: Mapped[str] = mapped_column(String(80), nullable=False, index=True)
    status: Mapped[str] = mapped_column(String(80), nullable=False, index=True)
    started_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), index=True)
    training_as_of: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    configuration_version: Mapped[str] = mapped_column(String(120), nullable=False)
    reward_definition_id: Mapped[str] = mapped_column(String(120), nullable=False, index=True)
    reward_definition_version: Mapped[str] = mapped_column(String(80), nullable=False)
    baseline_policy_id: Mapped[str] = mapped_column(String(120), nullable=False, index=True)
    baseline_policy_version: Mapped[str] = mapped_column(String(80), nullable=False)
    candidate_policy_id: Mapped[str | None] = mapped_column(String(120), index=True)
    candidate_policy_version: Mapped[str | None] = mapped_column(String(80), index=True)
    dataset_manifest_id: Mapped[str | None] = mapped_column(String(120), index=True)
    source_watermarks_json: Mapped[str] = mapped_column(Text, nullable=False)
    counts_json: Mapped[str] = mapped_column(Text, nullable=False)
    reason_codes_json: Mapped[str] = mapped_column(Text, nullable=False)
    artifact_uris_json: Mapped[str] = mapped_column(Text, nullable=False)
    idempotency_key: Mapped[str] = mapped_column(String(300), nullable=False, unique=True)
    raw_json: Mapped[str] = mapped_column(Text, nullable=False)

    __table_args__ = (
        Index("ix_rl_run_type_status", "run_type", "status"),
        Index("ix_rl_run_training", "training_as_of", "status"),
    )


class RlDatasetManifest(Base):
    __tablename__ = "rl_dataset_manifest"

    dataset_manifest_id: Mapped[str] = mapped_column(String(120), primary_key=True)
    run_id: Mapped[str] = mapped_column(ForeignKey("rl_run.run_id"), nullable=False, index=True)
    dataset_hash: Mapped[str] = mapped_column(String(120), nullable=False, index=True)
    training_as_of: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    rows_total: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    rows_included: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    rows_excluded: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    action_counts_json: Mapped[str] = mapped_column(Text, nullable=False)
    evidence_counts_json: Mapped[str] = mapped_column(Text, nullable=False)
    exclusion_counts_json: Mapped[str] = mapped_column(Text, nullable=False)
    source_watermarks_json: Mapped[str] = mapped_column(Text, nullable=False)
    feature_schema_id: Mapped[str] = mapped_column(String(120), nullable=False)
    feature_schema_version: Mapped[str] = mapped_column(String(80), nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    raw_json: Mapped[str] = mapped_column(Text, nullable=False)

    __table_args__ = (
        Index("ix_rl_dataset_run_training", "run_id", "training_as_of"),
    )


class RlRewardDefinition(Base):
    __tablename__ = "rl_reward_definition"

    reward_definition_id: Mapped[str] = mapped_column(String(120), primary_key=True)
    reward_definition_version: Mapped[str] = mapped_column(String(80), primary_key=True)
    primary_metric: Mapped[str] = mapped_column(String(120), nullable=False)
    roi_denominator: Mapped[str] = mapped_column(String(120), nullable=False)
    evidence_scope: Mapped[str] = mapped_column(String(120), nullable=False)
    cost_basis: Mapped[str] = mapped_column(String(160), nullable=False)
    clipping_policy_json: Mapped[str] = mapped_column(Text, nullable=False)
    coefficient_json: Mapped[str] = mapped_column(Text, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    raw_json: Mapped[str] = mapped_column(Text, nullable=False)


class RlRewardLedger(Base):
    __tablename__ = "rl_reward_ledger"

    reward_id: Mapped[str] = mapped_column(String(120), primary_key=True)
    run_id: Mapped[str] = mapped_column(ForeignKey("rl_run.run_id"), nullable=False, index=True)
    dataset_manifest_id: Mapped[str] = mapped_column(String(120), nullable=False, index=True)
    decision_id: Mapped[str] = mapped_column(String(120), nullable=False, index=True)
    opportunity_id: Mapped[str | None] = mapped_column(String(120), index=True)
    forecast_id: Mapped[str | None] = mapped_column(String(120), index=True)
    trade_id: Mapped[str | None] = mapped_column(String(120), index=True)
    action: Mapped[str] = mapped_column(String(40), nullable=False, index=True)
    evidence_type: Mapped[str] = mapped_column(String(80), nullable=False, index=True)
    reward_status: Mapped[str] = mapped_column(String(80), nullable=False, index=True)
    reward_definition_id: Mapped[str] = mapped_column(String(120), nullable=False)
    reward_definition_version: Mapped[str] = mapped_column(String(80), nullable=False)
    decision_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    reward_finalized_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    gross_pnl: Mapped[str | None] = mapped_column(String(80))
    net_pnl: Mapped[str | None] = mapped_column(String(80))
    total_cost: Mapped[str | None] = mapped_column(String(80))
    roi_denominator: Mapped[str | None] = mapped_column(String(80))
    raw_reward: Mapped[str | None] = mapped_column(String(80))
    transformed_reward: Mapped[str | None] = mapped_column(String(80))
    normalized_reward: Mapped[str | None] = mapped_column(String(80))
    reason_codes_json: Mapped[str] = mapped_column(Text, nullable=False)
    supersedes_reward_id: Mapped[str | None] = mapped_column(String(120), index=True)
    idempotency_key: Mapped[str] = mapped_column(String(300), nullable=False, unique=True)
    raw_json: Mapped[str] = mapped_column(Text, nullable=False)

    __table_args__ = (
        Index("ix_rl_reward_action_status", "action", "reward_status"),
        Index("ix_rl_reward_evidence", "evidence_type", "reward_status"),
    )


class RlBehaviorPolicy(Base):
    __tablename__ = "rl_behavior_policy"

    behavior_policy_id: Mapped[str] = mapped_column(String(120), primary_key=True)
    behavior_policy_version: Mapped[str] = mapped_column(String(80), primary_key=True)
    policy_family: Mapped[str] = mapped_column(String(120), nullable=False, index=True)
    status: Mapped[str] = mapped_column(String(80), nullable=False, index=True)
    action_space_json: Mapped[str] = mapped_column(Text, nullable=False)
    artifact_hash: Mapped[str | None] = mapped_column(String(120), index=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    raw_json: Mapped[str] = mapped_column(Text, nullable=False)


class RlBehaviorDecision(Base):
    __tablename__ = "rl_behavior_decision"

    decision_id: Mapped[str] = mapped_column(String(120), primary_key=True)
    dataset_manifest_id: Mapped[str] = mapped_column(String(120), nullable=False, index=True)
    opportunity_id: Mapped[str | None] = mapped_column(String(120), index=True)
    forecast_id: Mapped[str | None] = mapped_column(String(120), index=True)
    instrument_id: Mapped[str | None] = mapped_column(String(128), index=True)
    category_id: Mapped[str | None] = mapped_column(String(100), index=True)
    model_id: Mapped[str | None] = mapped_column(String(120), index=True)
    decision_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    chosen_action: Mapped[str] = mapped_column(String(40), nullable=False, index=True)
    action_set_json: Mapped[str] = mapped_column(Text, nullable=False)
    action_mask_json: Mapped[str] = mapped_column(Text, nullable=False)
    propensity_json: Mapped[str] = mapped_column(Text, nullable=False)
    propensity_quality: Mapped[str] = mapped_column(String(120), nullable=False, index=True)
    behavior_policy_id: Mapped[str] = mapped_column(String(120), nullable=False, index=True)
    behavior_policy_version: Mapped[str] = mapped_column(String(80), nullable=False)
    feature_values_json: Mapped[str] = mapped_column(Text, nullable=False)
    reason_codes_json: Mapped[str] = mapped_column(Text, nullable=False)
    raw_json: Mapped[str] = mapped_column(Text, nullable=False)

    __table_args__ = (
        Index("ix_rl_behavior_decision_time_action", "decision_at", "chosen_action"),
    )


class RlPolicyArtifact(Base):
    __tablename__ = "rl_policy_artifact"

    policy_artifact_id: Mapped[str] = mapped_column(String(120), primary_key=True)
    policy_id: Mapped[str] = mapped_column(String(120), nullable=False, index=True)
    policy_version: Mapped[str] = mapped_column(String(80), nullable=False, index=True)
    policy_family: Mapped[str] = mapped_column(String(120), nullable=False, index=True)
    status: Mapped[str] = mapped_column(String(80), nullable=False, index=True)
    artifact_hash: Mapped[str] = mapped_column(String(120), nullable=False, index=True)
    training_run_id: Mapped[str] = mapped_column(String(120), nullable=False, index=True)
    dataset_manifest_id: Mapped[str] = mapped_column(String(120), nullable=False, index=True)
    action_space_json: Mapped[str] = mapped_column(Text, nullable=False)
    parameters_json: Mapped[str] = mapped_column(Text, nullable=False)
    lineage_json: Mapped[str] = mapped_column(Text, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    raw_json: Mapped[str] = mapped_column(Text, nullable=False)

    __table_args__ = (
        UniqueConstraint("policy_id", "policy_version", name="uq_rl_policy_version"),
    )


class RlPolicyEvaluation(Base):
    __tablename__ = "rl_policy_evaluation"

    evaluation_id: Mapped[str] = mapped_column(String(120), primary_key=True)
    run_id: Mapped[str] = mapped_column(ForeignKey("rl_run.run_id"), nullable=False, index=True)
    dataset_manifest_id: Mapped[str] = mapped_column(String(120), nullable=False, index=True)
    candidate_policy_id: Mapped[str] = mapped_column(String(120), nullable=False, index=True)
    candidate_policy_version: Mapped[str] = mapped_column(String(80), nullable=False)
    baseline_policy_id: Mapped[str] = mapped_column(String(120), nullable=False, index=True)
    baseline_policy_version: Mapped[str] = mapped_column(String(80), nullable=False)
    evaluation_status: Mapped[str] = mapped_column(String(80), nullable=False, index=True)
    recommendation_status: Mapped[str] = mapped_column(String(80), nullable=False, index=True)
    evidence_scope: Mapped[str] = mapped_column(String(120), nullable=False, index=True)
    estimator_results_json: Mapped[str] = mapped_column(Text, nullable=False)
    economic_metrics_json: Mapped[str] = mapped_column(Text, nullable=False)
    risk_metrics_json: Mapped[str] = mapped_column(Text, nullable=False)
    behavior_support_json: Mapped[str] = mapped_column(Text, nullable=False)
    acceptance_gates_json: Mapped[str] = mapped_column(Text, nullable=False)
    card_json: Mapped[str] = mapped_column(Text, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    idempotency_key: Mapped[str] = mapped_column(String(300), nullable=False, unique=True)
    raw_json: Mapped[str] = mapped_column(Text, nullable=False)


class RlPolicySegmentMetric(Base):
    __tablename__ = "rl_policy_segment_metric"

    segment_metric_id: Mapped[str] = mapped_column(String(120), primary_key=True)
    evaluation_id: Mapped[str] = mapped_column(
        ForeignKey("rl_policy_evaluation.evaluation_id"), nullable=False, index=True
    )
    segment_name: Mapped[str] = mapped_column(String(160), nullable=False, index=True)
    segment_value: Mapped[str] = mapped_column(String(160), nullable=False, index=True)
    sample_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    candidate_value: Mapped[str | None] = mapped_column(String(80))
    baseline_value: Mapped[str | None] = mapped_column(String(80))
    improvement: Mapped[str | None] = mapped_column(String(80))
    support_status: Mapped[str] = mapped_column(String(80), nullable=False, index=True)
    raw_json: Mapped[str] = mapped_column(Text, nullable=False)


class RlPolicyDecision(Base):
    __tablename__ = "rl_policy_decision"

    policy_decision_id: Mapped[str] = mapped_column(String(120), primary_key=True)
    policy_id: Mapped[str] = mapped_column(String(120), nullable=False, index=True)
    policy_version: Mapped[str] = mapped_column(String(80), nullable=False, index=True)
    mode: Mapped[str] = mapped_column(String(80), nullable=False, index=True)
    opportunity_id: Mapped[str] = mapped_column(String(120), nullable=False, index=True)
    decision_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    recommended_action: Mapped[str] = mapped_column(String(40), nullable=False, index=True)
    baseline_action: Mapped[str] = mapped_column(String(40), nullable=False, index=True)
    valid_until: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    value_json: Mapped[str] = mapped_column(Text, nullable=False)
    support_json: Mapped[str] = mapped_column(Text, nullable=False)
    reason_codes_json: Mapped[str] = mapped_column(Text, nullable=False)
    idempotency_key: Mapped[str] = mapped_column(String(300), nullable=False, unique=True)
    raw_json: Mapped[str] = mapped_column(Text, nullable=False)

    __table_args__ = (
        Index("ix_rl_policy_decision_mode_action", "mode", "recommended_action"),
    )


class RlPolicyPromotion(Base):
    __tablename__ = "rl_policy_promotion"

    promotion_id: Mapped[str] = mapped_column(String(120), primary_key=True)
    policy_id: Mapped[str] = mapped_column(String(120), nullable=False, index=True)
    policy_version: Mapped[str] = mapped_column(String(80), nullable=False, index=True)
    evaluation_id: Mapped[str] = mapped_column(String(120), nullable=False, index=True)
    target_mode: Mapped[str] = mapped_column(String(80), nullable=False, index=True)
    status: Mapped[str] = mapped_column(String(80), nullable=False, index=True)
    actor: Mapped[str] = mapped_column(String(120), nullable=False)
    approved_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    reason_codes_json: Mapped[str] = mapped_column(Text, nullable=False)
    raw_json: Mapped[str] = mapped_column(Text, nullable=False)


class RlPolicyRollback(Base):
    __tablename__ = "rl_policy_rollback"

    rollback_id: Mapped[str] = mapped_column(String(120), primary_key=True)
    policy_id: Mapped[str] = mapped_column(String(120), nullable=False, index=True)
    policy_version: Mapped[str] = mapped_column(String(80), nullable=False, index=True)
    restored_policy_id: Mapped[str] = mapped_column(String(120), nullable=False)
    restored_policy_version: Mapped[str] = mapped_column(String(80), nullable=False)
    actor: Mapped[str] = mapped_column(String(120), nullable=False)
    reason: Mapped[str] = mapped_column(Text, nullable=False)
    rolled_back_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    raw_json: Mapped[str] = mapped_column(Text, nullable=False)


class RlDriftSnapshot(Base):
    __tablename__ = "rl_drift_snapshot"

    drift_snapshot_id: Mapped[str] = mapped_column(String(120), primary_key=True)
    policy_id: Mapped[str] = mapped_column(String(120), nullable=False, index=True)
    policy_version: Mapped[str] = mapped_column(String(80), nullable=False, index=True)
    generated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    window_start_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    window_end_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    status: Mapped[str] = mapped_column(String(80), nullable=False, index=True)
    metrics_json: Mapped[str] = mapped_column(Text, nullable=False)
    warnings_json: Mapped[str] = mapped_column(Text, nullable=False)
    raw_json: Mapped[str] = mapped_column(Text, nullable=False)


class RlHoldoutAccessLog(Base):
    __tablename__ = "rl_holdout_access_log"

    holdout_access_id: Mapped[str] = mapped_column(String(120), primary_key=True)
    run_id: Mapped[str] = mapped_column(String(120), nullable=False, index=True)
    accessed_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    holdout_policy_id: Mapped[str] = mapped_column(String(120), nullable=False)
    reason: Mapped[str] = mapped_column(Text, nullable=False)
    raw_json: Mapped[str] = mapped_column(Text, nullable=False)


class PersonalTraderRecommendationMemory(Base):
    __tablename__ = "personal_trader_recommendation_memory"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    event_id: Mapped[str] = mapped_column(String(120), nullable=False, unique=True)
    event_type: Mapped[str] = mapped_column(String(80), nullable=False, index=True)
    brief_id: Mapped[str] = mapped_column(String(120), nullable=False, index=True)
    recommendation_id: Mapped[str | None] = mapped_column(String(120), index=True)
    candidate_id: Mapped[str | None] = mapped_column(String(120), index=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        index=True,
    )
    execution_mode: Mapped[str] = mapped_column(String(40), nullable=False, index=True)
    account_scope_hash: Mapped[str] = mapped_column(String(120), nullable=False, index=True)
    portfolio_scope_hash: Mapped[str] = mapped_column(String(120), nullable=False, index=True)
    schema_version: Mapped[str] = mapped_column(String(40), nullable=False, index=True)
    ranking_policy_version: Mapped[str] = mapped_column(String(80), nullable=False, index=True)
    source_ids_json: Mapped[str] = mapped_column(Text, nullable=False)
    payload_json: Mapped[str] = mapped_column(Text, nullable=False)
    raw_json: Mapped[str] = mapped_column(Text, nullable=False)

    __table_args__ = (
        Index("ix_personal_trader_memory_brief_event", "brief_id", "event_type", "created_at"),
        Index(
            "ix_personal_trader_memory_recommendation",
            "recommendation_id",
            "event_type",
            "created_at",
        ),
    )


class ReadinessReviewRecord(Base):
    __tablename__ = "readiness_review"

    review_id: Mapped[str] = mapped_column(String(120), primary_key=True)
    lifecycle_state: Mapped[str] = mapped_column(String(80), nullable=False, index=True)
    target_environment: Mapped[str] = mapped_column(String(40), nullable=False, index=True)
    target_stage: Mapped[str] = mapped_column(String(40), nullable=False, index=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    frozen_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    decided_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), index=True)
    scope_json: Mapped[str] = mapped_column(Text, nullable=False)
    scope_sha256: Mapped[str] = mapped_column(String(120), nullable=False, index=True)
    decision: Mapped[str] = mapped_column(String(40), nullable=False, index=True)
    diagnostic_score: Mapped[str] = mapped_column(String(80), nullable=False)
    reason_codes_json: Mapped[str] = mapped_column(Text, nullable=False)
    raw_json: Mapped[str] = mapped_column(Text, nullable=False)

    __table_args__ = (
        Index("ix_readiness_review_stage_decision", "target_stage", "decision", "decided_at"),
    )


class ReadinessControlResult(Base):
    __tablename__ = "readiness_control_result"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    review_id: Mapped[str] = mapped_column(
        ForeignKey("readiness_review.review_id"), nullable=False, index=True
    )
    control_id: Mapped[str] = mapped_column(String(40), nullable=False, index=True)
    family: Mapped[str] = mapped_column(String(120), nullable=False, index=True)
    severity: Mapped[str] = mapped_column(String(40), nullable=False, index=True)
    status: Mapped[str] = mapped_column(String(40), nullable=False, index=True)
    evidence_ids_json: Mapped[str] = mapped_column(Text, nullable=False)
    reason_codes_json: Mapped[str] = mapped_column(Text, nullable=False)
    observed_result: Mapped[str] = mapped_column(Text, nullable=False)
    owner: Mapped[str] = mapped_column(String(120), nullable=False)
    reviewer: Mapped[str] = mapped_column(String(120), nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    raw_json: Mapped[str] = mapped_column(Text, nullable=False)

    __table_args__ = (
        UniqueConstraint("review_id", "control_id", name="uq_readiness_control_review"),
        Index("ix_readiness_control_status", "severity", "status", "created_at"),
    )


class ReadinessEvidenceManifest(Base):
    __tablename__ = "readiness_evidence_manifest"

    manifest_id: Mapped[str] = mapped_column(String(120), primary_key=True)
    review_id: Mapped[str] = mapped_column(
        ForeignKey("readiness_review.review_id"), nullable=False, index=True
    )
    generated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    frozen_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    scope_sha256: Mapped[str] = mapped_column(String(120), nullable=False, index=True)
    manifest_sha256: Mapped[str] = mapped_column(String(120), nullable=False, index=True)
    items_json: Mapped[str] = mapped_column(Text, nullable=False)
    raw_json: Mapped[str] = mapped_column(Text, nullable=False)


class ReadinessDecisionRecord(Base):
    __tablename__ = "readiness_decision"

    decision_id: Mapped[str] = mapped_column(String(120), primary_key=True)
    review_id: Mapped[str] = mapped_column(
        ForeignKey("readiness_review.review_id"), nullable=False, index=True
    )
    decision: Mapped[str] = mapped_column(String(40), nullable=False, index=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    target_stage: Mapped[str] = mapped_column(String(40), nullable=False, index=True)
    diagnostic_score: Mapped[str] = mapped_column(String(80), nullable=False)
    reason_codes_json: Mapped[str] = mapped_column(Text, nullable=False)
    launch_envelope_json: Mapped[str] = mapped_column(Text, nullable=False)
    certificate_ref: Mapped[str | None] = mapped_column(String(120), index=True)
    decision_json: Mapped[str] = mapped_column(Text, nullable=False)
    report_path: Mapped[str | None] = mapped_column(String(500))
    raw_json: Mapped[str] = mapped_column(Text, nullable=False)

    __table_args__ = (
        Index("ix_readiness_decision_stage_created", "target_stage", "created_at"),
    )


class LiveReadinessCertificate(Base):
    __tablename__ = "live_readiness_certificate"

    certificate_id: Mapped[str] = mapped_column(String(120), primary_key=True)
    review_id: Mapped[str] = mapped_column(
        ForeignKey("readiness_review.review_id"), nullable=False, index=True
    )
    decision_id: Mapped[str] = mapped_column(
        ForeignKey("readiness_decision.decision_id"), nullable=False, index=True
    )
    status: Mapped[str] = mapped_column(String(40), nullable=False, index=True)
    target_environment: Mapped[str] = mapped_column(String(40), nullable=False, index=True)
    target_stage: Mapped[str] = mapped_column(String(40), nullable=False, index=True)
    issued_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    valid_from: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    revoked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    scope_sha256: Mapped[str] = mapped_column(String(120), nullable=False, index=True)
    envelope_sha256: Mapped[str] = mapped_column(String(120), nullable=False, index=True)
    signature_payload_sha256: Mapped[str] = mapped_column(String(120), nullable=False)
    certificate_json: Mapped[str] = mapped_column(Text, nullable=False)
    raw_json: Mapped[str] = mapped_column(Text, nullable=False)

    __table_args__ = (
        Index("ix_live_cert_status_expiry", "status", "expires_at"),
    )


class LiveReadinessCertificateEvent(Base):
    __tablename__ = "live_readiness_certificate_event"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    event_id: Mapped[str] = mapped_column(String(120), nullable=False, unique=True)
    certificate_id: Mapped[str] = mapped_column(
        ForeignKey("live_readiness_certificate.certificate_id"),
        nullable=False,
        index=True,
    )
    event_type: Mapped[str] = mapped_column(String(80), nullable=False, index=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    reason: Mapped[str] = mapped_column(Text, nullable=False)
    raw_json: Mapped[str] = mapped_column(Text, nullable=False)


class SystemCertificationRun(Base):
    __tablename__ = "system_certification_run"

    certification_run_id: Mapped[str] = mapped_column(String(120), primary_key=True)
    mode: Mapped[str] = mapped_column(String(40), nullable=False, index=True)
    overall_status: Mapped[str] = mapped_column(String(60), nullable=False, index=True)
    live_trading_authorized: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    started_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    completed_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    repository_sha256: Mapped[str] = mapped_column(String(120), nullable=False, index=True)
    config_sha256: Mapped[str] = mapped_column(String(120), nullable=False, index=True)
    manifest_sha256: Mapped[str] = mapped_column(String(120), nullable=False, index=True)
    phase_count: Mapped[int] = mapped_column(Integer, nullable=False)
    connection_count: Mapped[int] = mapped_column(Integer, nullable=False)
    finding_count: Mapped[int] = mapped_column(Integer, nullable=False)
    report_json_path: Mapped[str] = mapped_column(String(500), nullable=False)
    report_md_path: Mapped[str] = mapped_column(String(500), nullable=False)
    raw_json: Mapped[str] = mapped_column(Text, nullable=False)

    __table_args__ = (
        Index("ix_system_cert_status_completed", "overall_status", "completed_at"),
    )


class SystemCertificationArtifact(Base):
    __tablename__ = "system_certification_artifact"

    artifact_id: Mapped[str] = mapped_column(String(120), primary_key=True)
    certification_run_id: Mapped[str] = mapped_column(
        ForeignKey("system_certification_run.certification_run_id"),
        nullable=False,
        index=True,
    )
    name: Mapped[str] = mapped_column(String(200), nullable=False)
    path: Mapped[str] = mapped_column(String(500), nullable=False)
    sha256: Mapped[str] = mapped_column(String(120), nullable=False, index=True)
    kind: Mapped[str] = mapped_column(String(80), nullable=False, index=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    raw_json: Mapped[str] = mapped_column(Text, nullable=False)
