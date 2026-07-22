from decimal import Decimal
from functools import lru_cache

from pydantic import AliasChoices, Field, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """Runtime settings loaded from environment variables or an optional .env file."""

    model_config = SettingsConfigDict(
        env_file=".env",
        extra="ignore",
        case_sensitive=False,
        populate_by_name=True,
    )

    kalshi_base_url: str = Field(
        default="https://external-api.kalshi.com/trade-api/v2",
        validation_alias="KALSHI_BASE_URL",
    )
    kalshi_env: str = Field(default="demo", validation_alias="KALSHI_ENV")
    kalshi_websocket_enabled: bool = Field(
        default=False,
        validation_alias="KALSHI_WEBSOCKET_ENABLED",
    )
    kalshi_websocket_url: str = Field(
        default="wss://external-api-ws.demo.kalshi.co/trade-api/ws/v2",
        validation_alias="KALSHI_WEBSOCKET_URL",
    )
    kalshi_api_key_id: str | None = Field(default=None, validation_alias="KALSHI_API_KEY_ID")
    kalshi_private_key_path: str | None = Field(
        default=None,
        validation_alias="KALSHI_PRIVATE_KEY_PATH",
    )
    kalshi_websocket_staging_dir: str = Field(
        default="reports/phase_gh1/staging",
        validation_alias="KALSHI_WEBSOCKET_STAGING_DIR",
    )
    kalshi_db_url: str = Field(
        default="sqlite:///data/kalshi_phase1.db",
        validation_alias=AliasChoices("DATABASE_URL", "KALSHI_DB_URL"),
    )
    db_backend: str = Field(default="sqlite", validation_alias="DB_BACKEND")
    postgres_host: str = Field(default="localhost", validation_alias="POSTGRES_HOST")
    postgres_port: int = Field(default=5432, validation_alias="POSTGRES_PORT")
    postgres_db: str = Field(
        default="kalshi_predictive_bot",
        validation_alias="POSTGRES_DB",
    )
    postgres_user: str = Field(default="kalshi", validation_alias="POSTGRES_USER")
    postgres_password: str = Field(
        default="kalshi_dev_password",
        validation_alias="POSTGRES_PASSWORD",
    )
    require_postgres_for_overnight: bool = Field(
        default=False,
        validation_alias="REQUIRE_POSTGRES_FOR_OVERNIGHT",
    )
    prov11_dashboard_preview_enabled: bool = Field(
        default=False,
        validation_alias="PROV11_DASHBOARD_PREVIEW_ENABLED",
    )
    prov12_decision_trace_preview_enabled: bool = Field(
        default=False,
        validation_alias="PROV12_DECISION_TRACE_PREVIEW_ENABLED",
    )
    prov12_provenance_stale_after_minutes: int = Field(
        default=60,
        ge=5,
        le=1440,
        validation_alias="PROV12_PROVENANCE_STALE_AFTER_MINUTES",
    )
    phase_3o_market_memory_enabled: bool = Field(
        default=True,
        validation_alias="PHASE_3O_MARKET_MEMORY_ENABLED",
    )
    phase_3o_market_memory_mode: str = Field(
        default="shadow_capture",
        validation_alias="PHASE_3O_MARKET_MEMORY_MODE",
    )
    phase_3o_schema_version: int = Field(default=1, validation_alias="PHASE_3O_SCHEMA_VERSION")
    phase_3o_default_data_mode: str = Field(
        default="AS_OBSERVED",
        validation_alias="PHASE_3O_DEFAULT_DATA_MODE",
    )
    phase_3o_forecast_label_policy_id: str = Field(
        default="kalshi_binary_result",
        validation_alias="PHASE_3O_FORECAST_LABEL_POLICY_ID",
    )
    phase_3o_forecast_label_policy_version: str = Field(
        default="v1",
        validation_alias="PHASE_3O_FORECAST_LABEL_POLICY_VERSION",
    )
    phase_3o_archive_dir: str = Field(
        default="data/memory_archive",
        validation_alias="PHASE_3O_ARCHIVE_DIR",
    )
    phase_3p_self_evaluation_enabled: bool = Field(
        default=False,
        validation_alias="PHASE_3P_SELF_EVALUATION_ENABLED",
    )
    phase_3p_mode: str = Field(default="shadow", validation_alias="PHASE_3P_MODE")
    phase_3p_evaluation_policy_id: str = Field(
        default="phase_3p_default",
        validation_alias="PHASE_3P_EVALUATION_POLICY_ID",
    )
    phase_3p_evaluation_policy_version: str = Field(
        default="1.0.0",
        validation_alias="PHASE_3P_EVALUATION_POLICY_VERSION",
    )
    phase_3p_session_calendar_id: str = Field(
        default="local_calendar_v1",
        validation_alias="PHASE_3P_SESSION_CALENDAR_ID",
    )
    phase_3p_session_timezone: str = Field(
        default="America/Chicago",
        validation_alias="PHASE_3P_SESSION_TIMEZONE",
    )
    phase_3p_data_mode: str = Field(
        default="AS_OBSERVED",
        validation_alias="PHASE_3P_DATA_MODE",
    )
    phase_3p_minimum_current_sample: int = Field(
        default=10,
        validation_alias="PHASE_3P_MINIMUM_CURRENT_SAMPLE",
    )
    phase_3p_minimum_baseline_sample: int = Field(
        default=30,
        validation_alias="PHASE_3P_MINIMUM_BASELINE_SAMPLE",
    )
    phase_3p_minimum_practical_effect_size: float = Field(
        default=0.20,
        validation_alias="PHASE_3P_MINIMUM_PRACTICAL_EFFECT_SIZE",
    )
    phase_3p_baseline_completed_session_windows: str = Field(
        default="5,20,60",
        validation_alias="PHASE_3P_BASELINE_COMPLETED_SESSION_WINDOWS",
    )
    phase_3p_publish_no_activity_journal: bool = Field(
        default=True,
        validation_alias="PHASE_3P_PUBLISH_NO_ACTIVITY_JOURNAL",
    )
    phase_3q_feature_discovery_enabled: bool = Field(
        default=True,
        validation_alias="PHASE_3Q_FEATURE_DISCOVERY_ENABLED",
    )
    phase_3q_mode: str = Field(
        default="shadow_research",
        validation_alias="PHASE_3Q_MODE",
    )
    phase_3q_min_samples: int = Field(default=5, validation_alias="PHASE_3Q_MIN_SAMPLES")
    phase_3q_max_candidates: int = Field(
        default=50,
        validation_alias="PHASE_3Q_MAX_CANDIDATES",
    )
    phase_3q_min_practical_effect: Decimal = Field(
        default=Decimal("0.05"),
        validation_alias="PHASE_3Q_MIN_PRACTICAL_EFFECT",
    )
    phase_3q_q_value_threshold: Decimal = Field(
        default=Decimal("0.20"),
        validation_alias="PHASE_3Q_Q_VALUE_THRESHOLD",
    )
    phase_3q_embargo_seconds: int = Field(
        default=0,
        validation_alias="PHASE_3Q_EMBARGO_SECONDS",
    )
    phase_3q_purge_seconds: int = Field(
        default=0,
        validation_alias="PHASE_3Q_PURGE_SECONDS",
    )
    phase_3q_report_limit: int = Field(default=25, validation_alias="PHASE_3Q_REPORT_LIMIT")
    phase_3r_synthetic_markets_enabled: bool = Field(
        default=False,
        validation_alias="PHASE_3R_SYNTHETIC_MARKETS_ENABLED",
    )
    phase_3r_mode: str = Field(default="disabled", validation_alias="PHASE_3R_MODE")
    phase_3r_max_candidates_per_run: int = Field(
        default=25,
        validation_alias="PHASE_3R_MAX_CANDIDATES_PER_RUN",
    )
    phase_3r_max_contracts_per_event: int = Field(
        default=4,
        validation_alias="PHASE_3R_MAX_CONTRACTS_PER_EVENT",
    )
    phase_3r_max_horizon_days: int = Field(
        default=365,
        validation_alias="PHASE_3R_MAX_HORIZON_DAYS",
    )
    phase_3r_probability_floor: Decimal = Field(
        default=Decimal("0.01"),
        validation_alias="PHASE_3R_PROBABILITY_FLOOR",
    )
    phase_3r_probability_ceiling: Decimal = Field(
        default=Decimal("0.99"),
        validation_alias="PHASE_3R_PROBABILITY_CEILING",
    )
    phase_3r_coherence_tolerance: Decimal = Field(
        default=Decimal("0.001"),
        validation_alias="PHASE_3R_COHERENCE_TOLERANCE",
    )
    phase_3r_max_publishable_adjustment: Decimal = Field(
        default=Decimal("0.10"),
        validation_alias="PHASE_3R_MAX_PUBLISHABLE_ADJUSTMENT",
    )
    phase_3r_listing_stale_after_hours: int = Field(
        default=24,
        validation_alias="PHASE_3R_LISTING_STALE_AFTER_HOURS",
    )
    phase_3s_reinforcement_learning_enabled: bool = Field(
        default=False,
        validation_alias="PHASE_3S_REINFORCEMENT_LEARNING_ENABLED",
    )
    phase_3s_mode: str = Field(default="disabled", validation_alias="PHASE_3S_MODE")
    phase_3s_min_training_rows: int = Field(
        default=25,
        validation_alias="PHASE_3S_MIN_TRAINING_ROWS",
    )
    phase_3s_min_action_support: int = Field(
        default=3,
        validation_alias="PHASE_3S_MIN_ACTION_SUPPORT",
    )
    phase_3s_baseline_opportunity_score: Decimal = Field(
        default=Decimal("45"),
        validation_alias="PHASE_3S_BASELINE_OPPORTUNITY_SCORE",
    )
    phase_3s_candidate_opportunity_score: Decimal = Field(
        default=Decimal("55"),
        validation_alias="PHASE_3S_CANDIDATE_OPPORTUNITY_SCORE",
    )
    phase_3s_min_lcb_improvement: Decimal = Field(
        default=Decimal("0.001"),
        validation_alias="PHASE_3S_MIN_LCB_IMPROVEMENT",
    )
    phase_3s_allow_online_exploration: bool = Field(
        default=False,
        validation_alias="PHASE_3S_ALLOW_ONLINE_EXPLORATION",
    )
    phase_3s_governed_gate_enabled: bool = Field(
        default=False,
        validation_alias="PHASE_3S_GOVERNED_GATE_ENABLED",
    )
    phase_3t_institutional_dashboard_enabled: bool = Field(
        default=False,
        validation_alias="PHASE_3T_INSTITUTIONAL_DASHBOARD_ENABLED",
    )
    phase_3t_mode: str = Field(default="disabled", validation_alias="PHASE_3T_MODE")
    phase_3t_dashboard_definition_version: str = Field(
        default="phase_3t_dashboard_v1",
        validation_alias="PHASE_3T_DASHBOARD_DEFINITION_VERSION",
    )
    phase_3t_panel_registry_version: str = Field(
        default="phase_3t_panel_registry_v1",
        validation_alias="PHASE_3T_PANEL_REGISTRY_VERSION",
    )
    phase_3t_metric_catalog_version: str = Field(
        default="phase_3t_metric_catalog_v1",
        validation_alias="PHASE_3T_METRIC_CATALOG_VERSION",
    )
    phase_3t_snapshot_validity_seconds: int = Field(
        default=60,
        validation_alias="PHASE_3T_SNAPSHOT_VALIDITY_SECONDS",
    )
    phase_3t_fresh_after_seconds: int = Field(
        default=300,
        validation_alias="PHASE_3T_FRESH_AFTER_SECONDS",
    )
    phase_3t_stale_after_seconds: int = Field(
        default=1800,
        validation_alias="PHASE_3T_STALE_AFTER_SECONDS",
    )
    phase_3t_max_source_skew_seconds: int = Field(
        default=1800,
        validation_alias="PHASE_3T_MAX_SOURCE_SKEW_SECONDS",
    )
    phase_3t_max_rows_per_panel: int = Field(
        default=50,
        validation_alias="PHASE_3T_MAX_ROWS_PER_PANEL",
    )
    phase_3u_personal_ai_trader_enabled: bool = Field(
        default=False,
        validation_alias="PHASE_3U_PERSONAL_AI_TRADER_ENABLED",
    )
    phase_3u_mode: str = Field(default="DISABLED", validation_alias="PHASE_3U_MODE")
    phase_3u_schema_version: str = Field(
        default="1.0.0",
        validation_alias="PHASE_3U_SCHEMA_VERSION",
    )
    phase_3u_ranking_policy_version: str = Field(
        default="3u-rank-v1",
        validation_alias="PHASE_3U_RANKING_POLICY_VERSION",
    )
    phase_3u_eligibility_policy_version: str = Field(
        default="3u-eligibility-v1",
        validation_alias="PHASE_3U_ELIGIBILITY_POLICY_VERSION",
    )
    phase_3u_explanation_policy_version: str = Field(
        default="3u-explain-v1",
        validation_alias="PHASE_3U_EXPLANATION_POLICY_VERSION",
    )
    phase_3u_default_timezone: str = Field(
        default="America/Chicago",
        validation_alias="PHASE_3U_DEFAULT_TIMEZONE",
    )
    phase_3u_default_max_recommendations: int = Field(
        default=3,
        validation_alias="PHASE_3U_DEFAULT_MAX_RECOMMENDATIONS",
    )
    phase_3u_absolute_max_recommendations: int = Field(
        default=5,
        validation_alias="PHASE_3U_ABSOLUTE_MAX_RECOMMENDATIONS",
    )
    phase_3u_min_net_ev_per_contract: Decimal = Field(
        default=Decimal("0.01"),
        validation_alias="PHASE_3U_MIN_NET_EV_PER_CONTRACT",
    )
    phase_3u_min_expected_roi: Decimal = Field(
        default=Decimal("0"),
        validation_alias="PHASE_3U_MIN_EXPECTED_ROI",
    )
    phase_3u_min_risk_adjusted_ev_lcb_per_contract: Decimal = Field(
        default=Decimal("0.001"),
        validation_alias="PHASE_3U_MIN_RISK_ADJUSTED_EV_LCB_PER_CONTRACT",
    )
    phase_3u_max_spread: Decimal = Field(
        default=Decimal("0.08"),
        validation_alias="PHASE_3U_MAX_SPREAD",
    )
    phase_3u_max_quote_age_seconds: int = Field(
        default=900,
        validation_alias="PHASE_3U_MAX_QUOTE_AGE_SECONDS",
    )
    phase_3u_max_forecast_age_seconds: int = Field(
        default=1800,
        validation_alias="PHASE_3U_MAX_FORECAST_AGE_SECONDS",
    )
    phase_3u_max_opportunity_age_seconds: int = Field(
        default=1800,
        validation_alias="PHASE_3U_MAX_OPPORTUNITY_AGE_SECONDS",
    )
    phase_3u_max_risk_age_seconds: int = Field(
        default=1800,
        validation_alias="PHASE_3U_MAX_RISK_AGE_SECONDS",
    )
    phase_3u_max_advisory_lifetime_seconds: int = Field(
        default=300,
        validation_alias="PHASE_3U_MAX_ADVISORY_LIFETIME_SECONDS",
    )
    phase_3u_candidate_limit: int = Field(
        default=100,
        validation_alias="PHASE_3U_CANDIDATE_LIMIT",
    )
    phase_3u_allow_phase_3s_fallback: bool = Field(
        default=True,
        validation_alias="PHASE_3U_ALLOW_PHASE_3S_FALLBACK",
    )
    phase_3u_llm_renderer_enabled: bool = Field(
        default=False,
        validation_alias="PHASE_3U_LLM_RENDERER_ENABLED",
    )
    phase_3v_live_readiness_enabled: bool = Field(
        default=False,
        validation_alias="PHASE_3V_LIVE_READINESS_ENABLED",
    )
    phase_3v_mode: str = Field(default="disabled", validation_alias="PHASE_3V_MODE")
    phase_3v_default_target_stage: str = Field(
        default="MICRO",
        validation_alias="PHASE_3V_DEFAULT_TARGET_STAGE",
    )
    phase_3v_certificate_issuance_enabled: bool = Field(
        default=False,
        validation_alias="PHASE_3V_CERTIFICATE_ISSUANCE_ENABLED",
    )
    phase_3v_certificate_max_lifetime_hours: int = Field(
        default=4,
        validation_alias="PHASE_3V_CERTIFICATE_MAX_LIFETIME_HOURS",
    )
    phase_3v_evidence_stale_after_days: int = Field(
        default=7,
        validation_alias="PHASE_3V_EVIDENCE_STALE_AFTER_DAYS",
    )
    phase_3v_required_approval_roles: str = Field(
        default="owner,risk,operator",
        validation_alias="PHASE_3V_REQUIRED_APPROVAL_ROLES",
    )
    phase_3v_micro_max_contracts_per_order: int = Field(
        default=1,
        validation_alias="PHASE_3V_MICRO_MAX_CONTRACTS_PER_ORDER",
    )
    phase_3v_constrained_max_contracts_per_order: int = Field(
        default=3,
        validation_alias="PHASE_3V_CONSTRAINED_MAX_CONTRACTS_PER_ORDER",
    )
    phase_3v_full_max_contracts_per_order: int = Field(
        default=5,
        validation_alias="PHASE_3V_FULL_MAX_CONTRACTS_PER_ORDER",
    )
    phase_3w_system_certification_enabled: bool = Field(
        default=False,
        validation_alias="PHASE_3W_SYSTEM_CERTIFICATION_ENABLED",
    )
    phase_3w_mode: str = Field(default="AUDIT_ONLY", validation_alias="PHASE_3W_MODE")
    phase_3w_safe_repair_enabled: bool = Field(
        default=False,
        validation_alias="PHASE_3W_SAFE_REPAIR_ENABLED",
    )
    phase_3w_output_dir: str = Field(
        default="reports/system_certification",
        validation_alias="PHASE_3W_OUTPUT_DIR",
    )
    phase_3x_professional_ux_enabled: bool = Field(
        default=False,
        validation_alias="PHASE_3X_PROFESSIONAL_UX_ENABLED",
    )
    phase_3x_mode: str = Field(default="audit_only", validation_alias="PHASE_3X_MODE")
    phase_3x_output_dir: str = Field(
        default="docs/phase_3x",
        validation_alias="PHASE_3X_OUTPUT_DIR",
    )
    phase_3x_default_route: str = Field(
        default="/today",
        validation_alias="PHASE_3X_DEFAULT_ROUTE",
    )
    phase_3x_theme: str = Field(default="system", validation_alias="PHASE_3X_THEME")
    phase_3x_density: str = Field(
        default="comfortable",
        validation_alias="PHASE_3X_DENSITY",
    )
    phase_3x_timezone: str = Field(
        default="America/Chicago",
        validation_alias="PHASE_3X_TIMEZONE",
    )
    phase_3x_command_palette_enabled: bool = Field(
        default=True,
        validation_alias="PHASE_3X_COMMAND_PALETTE_ENABLED",
    )
    kalshi_request_timeout_seconds: float = Field(
        default=15.0,
        validation_alias="KALSHI_REQUEST_TIMEOUT_SECONDS",
    )
    kalshi_max_retries: int = Field(default=3, validation_alias="KALSHI_MAX_RETRIES")
    kalshi_max_command_retries: int = Field(
        default=12,
        validation_alias="KALSHI_MAX_COMMAND_RETRIES",
    )
    kalshi_retry_backoff_seconds: float = Field(
        default=1.0,
        validation_alias="KALSHI_RETRY_BACKOFF_SECONDS",
    )
    kalshi_public_api_throttle_seconds: float = Field(
        default=0.25,
        validation_alias="KALSHI_PUBLIC_API_THROTTLE_SECONDS",
    )
    kalshi_user_agent: str = Field(
        default="kalshi-predictive-bot/phase1",
        validation_alias="KALSHI_USER_AGENT",
    )
    log_level: str = Field(default="INFO", validation_alias="LOG_LEVEL")
    paper_min_edge: Decimal = Field(default=Decimal("0.05"), validation_alias="PAPER_MIN_EDGE")
    paper_max_order_quantity: int = Field(
        default=1,
        validation_alias="PAPER_MAX_ORDER_QUANTITY",
    )
    paper_max_position_per_market: int = Field(
        default=5,
        validation_alias="PAPER_MAX_POSITION_PER_MARKET",
    )
    paper_max_open_orders: int = Field(default=100, validation_alias="PAPER_MAX_OPEN_ORDERS")
    paper_default_fee_per_contract: Decimal = Field(
        default=Decimal("0"),
        validation_alias="PAPER_DEFAULT_FEE_PER_CONTRACT",
    )
    paper_liquidity_starting_capital: Decimal = Field(
        default=Decimal("100"),
        validation_alias="PAPER_LIQUIDITY_STARTING_CAPITAL",
    )
    paper_liquidity_growth_target: Decimal = Field(
        default=Decimal("250"),
        validation_alias="PAPER_LIQUIDITY_GROWTH_TARGET",
    )
    paper_liquidity_max_position_fraction: Decimal = Field(
        default=Decimal("0.10"),
        validation_alias="PAPER_LIQUIDITY_MAX_POSITION_FRACTION",
    )
    paper_allow_buy_no: bool = Field(default=True, validation_alias="PAPER_ALLOW_BUY_NO")
    paper_allow_selling: bool = Field(default=False, validation_alias="PAPER_ALLOW_SELLING")
    paper_order_ttl_minutes: int = Field(default=120, validation_alias="PAPER_ORDER_TTL_MINUTES")
    paper_order_creation_enabled: bool = Field(
        default=False,
        validation_alias="PAPER_ORDER_CREATION_ENABLED",
    )
    paper_order_kill_switch: bool = Field(
        default=True,
        validation_alias="PAPER_ORDER_KILL_SWITCH",
    )
    dynamic_position_sizing_mode: str = Field(
        default="disabled",
        validation_alias="DYNAMIC_POSITION_SIZING_MODE",
    )
    dynamic_position_sizing_version: str = Field(
        default="3M",
        validation_alias="DYNAMIC_POSITION_SIZING_VERSION",
    )
    dynamic_position_sizing_live_max_contracts: int = Field(
        default=1,
        validation_alias="DYNAMIC_POSITION_SIZING_LIVE_MAX_CONTRACTS",
    )
    dynamic_position_sizing_global_max_contracts: int = Field(
        default=5,
        validation_alias="DYNAMIC_POSITION_SIZING_GLOBAL_MAX_CONTRACTS",
    )
    dynamic_position_sizing_weight_confidence: Decimal = Field(
        default=Decimal("0.35"),
        validation_alias="DYNAMIC_POSITION_SIZING_WEIGHT_CONFIDENCE",
    )
    dynamic_position_sizing_weight_opportunity: Decimal = Field(
        default=Decimal("0.25"),
        validation_alias="DYNAMIC_POSITION_SIZING_WEIGHT_OPPORTUNITY",
    )
    dynamic_position_sizing_weight_liquidity: Decimal = Field(
        default=Decimal("0.15"),
        validation_alias="DYNAMIC_POSITION_SIZING_WEIGHT_LIQUIDITY",
    )
    dynamic_position_sizing_weight_historical_accuracy: Decimal = Field(
        default=Decimal("0.15"),
        validation_alias="DYNAMIC_POSITION_SIZING_WEIGHT_HISTORICAL_ACCURACY",
    )
    dynamic_position_sizing_weight_drawdown_health: Decimal = Field(
        default=Decimal("0.10"),
        validation_alias="DYNAMIC_POSITION_SIZING_WEIGHT_DRAWDOWN_HEALTH",
    )
    dynamic_position_sizing_medium_score: Decimal = Field(
        default=Decimal("0.65"),
        validation_alias="DYNAMIC_POSITION_SIZING_MEDIUM_SCORE",
    )
    dynamic_position_sizing_high_score: Decimal = Field(
        default=Decimal("0.80"),
        validation_alias="DYNAMIC_POSITION_SIZING_HIGH_SCORE",
    )
    dynamic_position_sizing_medium_min_confidence: Decimal = Field(
        default=Decimal("0.65"),
        validation_alias="DYNAMIC_POSITION_SIZING_MEDIUM_MIN_CONFIDENCE",
    )
    dynamic_position_sizing_medium_min_opportunity: Decimal = Field(
        default=Decimal("0.60"),
        validation_alias="DYNAMIC_POSITION_SIZING_MEDIUM_MIN_OPPORTUNITY",
    )
    dynamic_position_sizing_high_min_confidence: Decimal = Field(
        default=Decimal("0.80"),
        validation_alias="DYNAMIC_POSITION_SIZING_HIGH_MIN_CONFIDENCE",
    )
    dynamic_position_sizing_high_min_opportunity: Decimal = Field(
        default=Decimal("0.75"),
        validation_alias="DYNAMIC_POSITION_SIZING_HIGH_MIN_OPPORTUNITY",
    )
    dynamic_position_sizing_high_min_adjusted_accuracy: Decimal = Field(
        default=Decimal("0.55"),
        validation_alias="DYNAMIC_POSITION_SIZING_HIGH_MIN_ADJUSTED_ACCURACY",
    )
    dynamic_position_sizing_liquidity_one_contract_below: Decimal = Field(
        default=Decimal("0.45"),
        validation_alias="DYNAMIC_POSITION_SIZING_LIQUIDITY_ONE_CONTRACT_BELOW",
    )
    dynamic_position_sizing_liquidity_three_contracts_below: Decimal = Field(
        default=Decimal("0.70"),
        validation_alias="DYNAMIC_POSITION_SIZING_LIQUIDITY_THREE_CONTRACTS_BELOW",
    )
    dynamic_position_sizing_drawdown_three_contracts_at: Decimal = Field(
        default=Decimal("0.50"),
        validation_alias="DYNAMIC_POSITION_SIZING_DRAWDOWN_THREE_CONTRACTS_AT",
    )
    dynamic_position_sizing_drawdown_one_contract_at: Decimal = Field(
        default=Decimal("0.75"),
        validation_alias="DYNAMIC_POSITION_SIZING_DRAWDOWN_ONE_CONTRACT_AT",
    )
    dynamic_position_sizing_drawdown_kill_at: Decimal = Field(
        default=Decimal("1.00"),
        validation_alias="DYNAMIC_POSITION_SIZING_DRAWDOWN_KILL_AT",
    )
    dynamic_position_sizing_history_prior_accuracy: Decimal = Field(
        default=Decimal("0.50"),
        validation_alias="DYNAMIC_POSITION_SIZING_HISTORY_PRIOR_ACCURACY",
    )
    dynamic_position_sizing_history_prior_weight: int = Field(
        default=20,
        validation_alias="DYNAMIC_POSITION_SIZING_HISTORY_PRIOR_WEIGHT",
    )
    dynamic_position_sizing_minimum_samples_for_high: int = Field(
        default=30,
        validation_alias="DYNAMIC_POSITION_SIZING_MINIMUM_SAMPLES_FOR_HIGH",
    )
    dynamic_position_sizing_external_risk_cap: int | None = Field(
        default=None,
        validation_alias="DYNAMIC_POSITION_SIZING_EXTERNAL_RISK_CAP",
    )
    dynamic_position_sizing_margin_cap: int | None = Field(
        default=None,
        validation_alias="DYNAMIC_POSITION_SIZING_MARGIN_CAP",
    )
    dynamic_position_sizing_portfolio_cap: int | None = Field(
        default=None,
        validation_alias="DYNAMIC_POSITION_SIZING_PORTFOLIO_CAP",
    )
    dynamic_position_sizing_missing_external_risk_cap_defaults_to_one: bool = Field(
        default=True,
        validation_alias="DYNAMIC_POSITION_SIZING_MISSING_EXTERNAL_RISK_CAP_DEFAULTS_TO_ONE",
    )
    advanced_risk_engine_mode: str = Field(
        default="disabled",
        validation_alias="ADVANCED_RISK_ENGINE_MODE",
    )
    advanced_risk_engine_version: str = Field(
        default="3N",
        validation_alias="ADVANCED_RISK_ENGINE_VERSION",
    )
    advanced_risk_live_max_contracts: int = Field(
        default=1,
        validation_alias="ADVANCED_RISK_LIVE_MAX_CONTRACTS",
    )
    advanced_risk_global_max_contracts: int = Field(
        default=5,
        validation_alias="ADVANCED_RISK_GLOBAL_MAX_CONTRACTS",
    )
    advanced_risk_default_account_equity: Decimal = Field(
        default=Decimal("10000"),
        validation_alias="ADVANCED_RISK_DEFAULT_ACCOUNT_EQUITY",
    )
    advanced_risk_portfolio_snapshot_max_age_ms: int = Field(
        default=300000,
        validation_alias="ADVANCED_RISK_PORTFOLIO_SNAPSHOT_MAX_AGE_MS",
    )
    advanced_risk_quote_max_age_ms: int = Field(
        default=900000,
        validation_alias="ADVANCED_RISK_QUOTE_MAX_AGE_MS",
    )
    advanced_risk_unknown_category_action: str = Field(
        default="block",
        validation_alias="ADVANCED_RISK_UNKNOWN_CATEGORY_ACTION",
    )
    advanced_risk_unknown_model_action: str = Field(
        default="block",
        validation_alias="ADVANCED_RISK_UNKNOWN_MODEL_ACTION",
    )
    advanced_risk_missing_edge_statistics_action: str = Field(
        default="cap_to_one",
        validation_alias="ADVANCED_RISK_MISSING_EDGE_STATISTICS_ACTION",
    )
    advanced_risk_missing_optional_liquidity_data_action: str = Field(
        default="cap_to_one",
        validation_alias="ADVANCED_RISK_MISSING_OPTIONAL_LIQUIDITY_DATA_ACTION",
    )
    advanced_risk_max_total_open_risk_fraction: Decimal = Field(
        default=Decimal("0.20"),
        validation_alias="ADVANCED_RISK_MAX_TOTAL_OPEN_RISK_FRACTION",
    )
    advanced_risk_default_category_risk_fraction: Decimal = Field(
        default=Decimal("0.10"),
        validation_alias="ADVANCED_RISK_DEFAULT_CATEGORY_RISK_FRACTION",
    )
    advanced_risk_default_model_risk_fraction: Decimal = Field(
        default=Decimal("0.10"),
        validation_alias="ADVANCED_RISK_DEFAULT_MODEL_RISK_FRACTION",
    )
    advanced_risk_max_daily_loss_fraction: Decimal | None = Field(
        default=Decimal("0.05"),
        validation_alias="ADVANCED_RISK_MAX_DAILY_LOSS_FRACTION",
    )
    advanced_risk_max_daily_loss_fixed_amount: Decimal | None = Field(
        default=None,
        validation_alias="ADVANCED_RISK_MAX_DAILY_LOSS_FIXED_AMOUNT",
    )
    advanced_risk_unrealized_pnl_weight: Decimal = Field(
        default=Decimal("1.0"),
        validation_alias="ADVANCED_RISK_UNREALIZED_PNL_WEIGHT",
    )
    advanced_risk_daily_loss_reserve_amount: Decimal = Field(
        default=Decimal("0"),
        validation_alias="ADVANCED_RISK_DAILY_LOSS_RESERVE_AMOUNT",
    )
    advanced_risk_session_timezone: str = Field(
        default="UTC",
        validation_alias="ADVANCED_RISK_SESSION_TIMEZONE",
    )
    advanced_risk_session_reset_time: str = Field(
        default="00:00",
        validation_alias="ADVANCED_RISK_SESSION_RESET_TIME",
    )
    advanced_risk_max_drawdown_fraction: Decimal = Field(
        default=Decimal("0.20"),
        validation_alias="ADVANCED_RISK_MAX_DRAWDOWN_FRACTION",
    )
    advanced_risk_drawdown_reserve_amount: Decimal = Field(
        default=Decimal("0"),
        validation_alias="ADVANCED_RISK_DRAWDOWN_RESERVE_AMOUNT",
    )
    advanced_risk_drawdown_warning_one_utilization: Decimal = Field(
        default=Decimal("0.50"),
        validation_alias="ADVANCED_RISK_DRAWDOWN_WARNING_ONE_UTILIZATION",
    )
    advanced_risk_drawdown_warning_two_utilization: Decimal = Field(
        default=Decimal("0.75"),
        validation_alias="ADVANCED_RISK_DRAWDOWN_WARNING_TWO_UTILIZATION",
    )
    advanced_risk_drawdown_warning_one_cap: int = Field(
        default=3,
        validation_alias="ADVANCED_RISK_DRAWDOWN_WARNING_ONE_CAP",
    )
    advanced_risk_drawdown_warning_two_cap: int = Field(
        default=1,
        validation_alias="ADVANCED_RISK_DRAWDOWN_WARNING_TWO_CAP",
    )
    advanced_risk_drawdown_kill_utilization: Decimal = Field(
        default=Decimal("1.00"),
        validation_alias="ADVANCED_RISK_DRAWDOWN_KILL_UTILIZATION",
    )
    advanced_risk_max_instrument_risk_fraction: Decimal = Field(
        default=Decimal("0.05"),
        validation_alias="ADVANCED_RISK_MAX_INSTRUMENT_RISK_FRACTION",
    )
    advanced_risk_max_correlation_group_risk_fraction: Decimal | None = Field(
        default=None,
        validation_alias="ADVANCED_RISK_MAX_CORRELATION_GROUP_RISK_FRACTION",
    )
    advanced_risk_spread_preferred_max_ticks: Decimal = Field(
        default=Decimal("2"),
        validation_alias="ADVANCED_RISK_SPREAD_PREFERRED_MAX_TICKS",
    )
    advanced_risk_spread_elevated_max_ticks: Decimal = Field(
        default=Decimal("5"),
        validation_alias="ADVANCED_RISK_SPREAD_ELEVATED_MAX_TICKS",
    )
    advanced_risk_spread_executable_max_ticks: Decimal = Field(
        default=Decimal("10"),
        validation_alias="ADVANCED_RISK_SPREAD_EXECUTABLE_MAX_TICKS",
    )
    advanced_risk_max_depth_participation_fraction: Decimal = Field(
        default=Decimal("0.10"),
        validation_alias="ADVANCED_RISK_MAX_DEPTH_PARTICIPATION_FRACTION",
    )
    advanced_risk_max_recent_volume_participation_fraction: Decimal = Field(
        default=Decimal("0.05"),
        validation_alias="ADVANCED_RISK_MAX_RECENT_VOLUME_PARTICIPATION_FRACTION",
    )
    advanced_risk_max_adv_participation_fraction: Decimal | None = Field(
        default=None,
        validation_alias="ADVANCED_RISK_MAX_ADV_PARTICIPATION_FRACTION",
    )
    advanced_risk_max_open_interest_participation_fraction: Decimal | None = Field(
        default=None,
        validation_alias="ADVANCED_RISK_MAX_OPEN_INTEREST_PARTICIPATION_FRACTION",
    )
    advanced_risk_required_liquidity_sources: str = Field(
        default="depth,recent_volume",
        validation_alias="ADVANCED_RISK_REQUIRED_LIQUIDITY_SOURCES",
    )
    advanced_risk_depth_price_band_ticks: Decimal = Field(
        default=Decimal("5"),
        validation_alias="ADVANCED_RISK_DEPTH_PRICE_BAND_TICKS",
    )
    advanced_risk_confidence_defensive_medium_floor: Decimal = Field(
        default=Decimal("0.65"),
        validation_alias="ADVANCED_RISK_CONFIDENCE_DEFENSIVE_MEDIUM_FLOOR",
    )
    advanced_risk_confidence_defensive_high_floor: Decimal = Field(
        default=Decimal("0.80"),
        validation_alias="ADVANCED_RISK_CONFIDENCE_DEFENSIVE_HIGH_FLOOR",
    )
    advanced_risk_kelly_enabled: bool = Field(
        default=False,
        validation_alias="ADVANCED_RISK_KELLY_ENABLED",
    )
    advanced_risk_kelly_prior_win_probability: Decimal = Field(
        default=Decimal("0.50"),
        validation_alias="ADVANCED_RISK_KELLY_PRIOR_WIN_PROBABILITY",
    )
    advanced_risk_kelly_prior_weight: int = Field(
        default=20,
        validation_alias="ADVANCED_RISK_KELLY_PRIOR_WEIGHT",
    )
    advanced_risk_kelly_minimum_sample_size: int = Field(
        default=30,
        validation_alias="ADVANCED_RISK_KELLY_MINIMUM_SAMPLE_SIZE",
    )
    advanced_risk_fractional_kelly_multiplier: Decimal = Field(
        default=Decimal("0.25"),
        validation_alias="ADVANCED_RISK_FRACTIONAL_KELLY_MULTIPLIER",
    )
    advanced_risk_max_applied_kelly_fraction: Decimal = Field(
        default=Decimal("0.02"),
        validation_alias="ADVANCED_RISK_MAX_APPLIED_KELLY_FRACTION",
    )
    advanced_risk_max_trade_risk_fraction: Decimal = Field(
        default=Decimal("0.01"),
        validation_alias="ADVANCED_RISK_MAX_TRADE_RISK_FRACTION",
    )
    advanced_risk_kelly_insufficient_data_cap: int = Field(
        default=1,
        validation_alias="ADVANCED_RISK_KELLY_INSUFFICIENT_DATA_CAP",
    )
    advanced_risk_ev_enabled: bool = Field(
        default=False,
        validation_alias="ADVANCED_RISK_EV_ENABLED",
    )
    advanced_risk_ev_minimum_sample_size: int = Field(
        default=30,
        validation_alias="ADVANCED_RISK_EV_MINIMUM_SAMPLE_SIZE",
    )
    advanced_risk_ev_probability_z_score: Decimal = Field(
        default=Decimal("1.0"),
        validation_alias="ADVANCED_RISK_EV_PROBABILITY_Z_SCORE",
    )
    advanced_risk_cvar_weight: Decimal = Field(
        default=Decimal("1.0"),
        validation_alias="ADVANCED_RISK_CVAR_WEIGHT",
    )
    advanced_risk_minimum_trade_ev_to_risk: Decimal = Field(
        default=Decimal("0.00"),
        validation_alias="ADVANCED_RISK_MINIMUM_TRADE_EV_TO_RISK",
    )
    advanced_risk_medium_ev_to_risk: Decimal = Field(
        default=Decimal("0.10"),
        validation_alias="ADVANCED_RISK_MEDIUM_EV_TO_RISK",
    )
    advanced_risk_high_ev_to_risk: Decimal = Field(
        default=Decimal("0.25"),
        validation_alias="ADVANCED_RISK_HIGH_EV_TO_RISK",
    )
    advanced_risk_ev_insufficient_data_cap: int = Field(
        default=1,
        validation_alias="ADVANCED_RISK_EV_INSUFFICIENT_DATA_CAP",
    )
    advanced_risk_estimated_slippage_per_contract: Decimal = Field(
        default=Decimal("0"),
        validation_alias="ADVANCED_RISK_ESTIMATED_SLIPPAGE_PER_CONTRACT",
    )
    advanced_risk_gap_tail_buffer_per_contract: Decimal = Field(
        default=Decimal("0"),
        validation_alias="ADVANCED_RISK_GAP_TAIL_BUFFER_PER_CONTRACT",
    )
    opportunity_min_edge: Decimal = Field(
        default=Decimal("0.03"),
        validation_alias="OPPORTUNITY_MIN_EDGE",
    )
    opportunity_min_score: Decimal = Field(
        default=Decimal("60"),
        validation_alias="OPPORTUNITY_MIN_SCORE",
    )
    opportunity_max_spread: Decimal = Field(
        default=Decimal("0.10"),
        validation_alias="OPPORTUNITY_MAX_SPREAD",
    )
    opportunity_min_liquidity: Decimal = Field(
        default=Decimal("0"),
        validation_alias="OPPORTUNITY_MIN_LIQUIDITY",
    )
    opportunity_min_time_to_close_minutes: Decimal = Field(
        default=Decimal("30"),
        validation_alias="OPPORTUNITY_MIN_TIME_TO_CLOSE_MINUTES",
    )
    opportunity_max_results: int = Field(
        default=20,
        validation_alias="OPPORTUNITY_MAX_RESULTS",
    )
    crypto_v2_max_adjustment: Decimal = Field(
        default=Decimal("0.08"),
        validation_alias="CRYPTO_V2_MAX_ADJUSTMENT",
    )
    crypto_v2_min_link_confidence: Decimal = Field(
        default=Decimal("0.6"),
        validation_alias="CRYPTO_V2_MIN_LINK_CONFIDENCE",
    )
    crypto_v2_min_history_minutes: int = Field(
        default=60,
        validation_alias="CRYPTO_V2_MIN_HISTORY_MINUTES",
    )
    weather_v2_max_adjustment: Decimal = Field(
        default=Decimal("0.10"),
        validation_alias="WEATHER_V2_MAX_ADJUSTMENT",
    )
    weather_v2_min_link_confidence: Decimal = Field(
        default=Decimal("0.6"),
        validation_alias="WEATHER_V2_MIN_LINK_CONFIDENCE",
    )
    weather_v2_max_forecast_age_hours: int = Field(
        default=24,
        validation_alias="WEATHER_V2_MAX_FORECAST_AGE_HOURS",
    )
    weather_v2_default_location_key: str = Field(
        default="kansas_city",
        validation_alias="WEATHER_V2_DEFAULT_LOCATION_KEY",
    )
    weather_v2_knyc_observation_enabled: bool = Field(
        default=False,
        validation_alias="WEATHER_V2_KNYC_OBSERVATION_ENABLED",
    )
    runtime_provenance_dual_write_enabled: bool = Field(
        default=False,
        validation_alias="RUNTIME_PROVENANCE_DUAL_WRITE_ENABLED",
    )
    ui_read_only: bool = Field(default=True, validation_alias="UI_READ_ONLY")
    execution_enabled: bool = Field(default=False, validation_alias="EXECUTION_ENABLED")
    execution_dry_run: bool = Field(default=True, validation_alias="EXECUTION_DRY_RUN")
    execution_kill_switch: bool = Field(default=False, validation_alias="EXECUTION_KILL_SWITCH")
    execution_gateway_mode: str = Field(
        default="disabled",
        validation_alias="EXECUTION_GATEWAY_MODE",
    )
    execution_confirmation_token: str = Field(
        default="DEMO ONLY",
        validation_alias="EXECUTION_CONFIRMATION_TOKEN",
    )
    autopilot_enabled: bool = Field(default=False, validation_alias="AUTOPILOT_ENABLED")
    autopilot_dry_run: bool = Field(default=True, validation_alias="AUTOPILOT_DRY_RUN")
    autopilot_model: str = Field(default="ensemble_v2", validation_alias="AUTOPILOT_MODEL")
    autopilot_interval_seconds: int = Field(
        default=300,
        validation_alias="AUTOPILOT_INTERVAL_SECONDS",
    )
    autopilot_max_cycles: int = Field(default=0, validation_alias="AUTOPILOT_MAX_CYCLES")
    autopilot_max_orders_per_cycle: int = Field(
        default=1,
        validation_alias="AUTOPILOT_MAX_ORDERS_PER_CYCLE",
    )
    autopilot_max_daily_orders: int = Field(
        default=10,
        validation_alias="AUTOPILOT_MAX_DAILY_ORDERS",
    )
    autopilot_min_edge: Decimal = Field(
        default=Decimal("0.05"),
        validation_alias="AUTOPILOT_MIN_EDGE",
    )
    autopilot_min_opportunity_score: Decimal = Field(
        default=Decimal("75"),
        validation_alias="AUTOPILOT_MIN_OPPORTUNITY_SCORE",
    )
    autopilot_stop_on_drawdown: bool = Field(
        default=True,
        validation_alias="AUTOPILOT_STOP_ON_DRAWDOWN",
    )
    autopilot_max_daily_drawdown: Decimal = Field(
        default=Decimal("5.00"),
        validation_alias="AUTOPILOT_MAX_DAILY_DRAWDOWN",
    )
    autopilot_max_open_demo_orders: int = Field(
        default=5,
        validation_alias="AUTOPILOT_MAX_OPEN_DEMO_ORDERS",
    )
    autopilot_require_fresh_data_minutes: int = Field(
        default=15,
        validation_alias="AUTOPILOT_REQUIRE_FRESH_DATA_MINUTES",
    )
    overnight_enabled: bool = Field(default=False, validation_alias="OVERNIGHT_ENABLED")
    overnight_interval_minutes: int = Field(
        default=15,
        validation_alias="OVERNIGHT_INTERVAL_MINUTES",
    )
    overnight_max_cycles: int = Field(default=32, validation_alias="OVERNIGHT_MAX_CYCLES")
    overnight_model: str = Field(default="ensemble_v2", validation_alias="OVERNIGHT_MODEL")
    overnight_run_paper: bool = Field(
        default=True,
        validation_alias="OVERNIGHT_RUN_PAPER",
    )
    overnight_run_demo: bool = Field(
        default=False,
        validation_alias="OVERNIGHT_RUN_DEMO",
    )
    overnight_run_backtest: bool = Field(
        default=True,
        validation_alias="OVERNIGHT_RUN_BACKTEST",
    )
    overnight_run_reports: bool = Field(
        default=True,
        validation_alias="OVERNIGHT_RUN_REPORTS",
    )
    overnight_min_free_disk_mb: int = Field(
        default=500,
        validation_alias="OVERNIGHT_MIN_FREE_DISK_MB",
    )
    overnight_stop_on_error: bool = Field(
        default=False,
        validation_alias="OVERNIGHT_STOP_ON_ERROR",
    )
    overnight_require_market_data: bool = Field(
        default=True,
        validation_alias="OVERNIGHT_REQUIRE_MARKET_DATA",
    )
    forum_consensus_enabled: bool = Field(
        default=True,
        validation_alias="FORUM_CONSENSUS_ENABLED",
    )
    forum_consensus_min_winners: int = Field(
        default=5,
        validation_alias="FORUM_CONSENSUS_MIN_WINNERS",
    )
    forum_consensus_min_win_rate: Decimal = Field(
        default=Decimal("0.55"),
        validation_alias="FORUM_CONSENSUS_MIN_WIN_RATE",
    )
    forum_consensus_longshot_max_price: Decimal = Field(
        default=Decimal("0.25"),
        validation_alias="FORUM_CONSENSUS_LONGSHOT_MAX_PRICE",
    )
    forum_consensus_max_age_hours: int = Field(
        default=24,
        validation_alias="FORUM_CONSENSUS_MAX_AGE_HOURS",
    )
    news_enabled: bool = Field(default=False, validation_alias="NEWS_ENABLED")
    news_default_window_minutes: int = Field(
        default=360,
        validation_alias="NEWS_DEFAULT_WINDOW_MINUTES",
    )
    news_max_items_per_feed: int = Field(
        default=50,
        validation_alias="NEWS_MAX_ITEMS_PER_FEED",
    )
    news_min_importance_score: Decimal = Field(
        default=Decimal("0.40"),
        validation_alias="NEWS_MIN_IMPORTANCE_SCORE",
    )
    news_min_link_confidence: Decimal = Field(
        default=Decimal("0.50"),
        validation_alias="NEWS_MIN_LINK_CONFIDENCE",
    )
    news_rss_feeds_json: str = Field(default="", validation_alias="NEWS_RSS_FEEDS_JSON")
    news_user_agent: str = Field(
        default="kalshi-predictive-bot-news/phase3h",
        validation_alias="NEWS_USER_AGENT",
    )
    news_v1_max_adjustment: Decimal = Field(
        default=Decimal("0.06"),
        validation_alias="NEWS_V1_MAX_ADJUSTMENT",
    )
    learning_mode: bool = Field(default=True, validation_alias="LEARNING_MODE")
    learning_model_name: str = Field(
        default="ensemble_v2",
        validation_alias="LEARNING_MODEL_NAME",
    )
    learning_target_settled_trades: int = Field(
        default=500,
        validation_alias="LEARNING_TARGET_SETTLED_TRADES",
    )
    learning_min_edge: Decimal = Field(
        default=Decimal("0.01"),
        validation_alias="LEARNING_MIN_EDGE",
    )
    learning_min_opportunity_score: Decimal = Field(
        default=Decimal("35"),
        validation_alias="LEARNING_MIN_OPPORTUNITY_SCORE",
    )
    learning_max_paper_order_qty: int = Field(
        default=1,
        validation_alias="LEARNING_MAX_PAPER_ORDER_QTY",
    )
    learning_max_paper_positions_per_market: int = Field(
        default=3,
        validation_alias="LEARNING_MAX_PAPER_POSITIONS_PER_MARKET",
    )
    learning_max_daily_paper_trades: int = Field(
        default=100,
        validation_alias="LEARNING_MAX_DAILY_PAPER_TRADES",
    )
    learning_min_trades_per_cycle: int = Field(
        default=5,
        validation_alias="LEARNING_MIN_TRADES_PER_CYCLE",
    )
    learning_target_trades_per_cycle: int = Field(
        default=10,
        validation_alias="LEARNING_TARGET_TRADES_PER_CYCLE",
    )
    learning_prioritize_fast_settlement: bool = Field(
        default=True,
        validation_alias="LEARNING_PRIORITIZE_FAST_SETTLEMENT",
    )
    learning_max_days_to_settlement: int = Field(
        default=3,
        validation_alias="LEARNING_MAX_DAYS_TO_SETTLEMENT",
    )
    learning_allowed_categories: str = Field(
        default="crypto,weather,economic,general",
        validation_alias="LEARNING_ALLOWED_CATEGORIES",
    )
    learning_block_demo_execution: bool = Field(
        default=True,
        validation_alias="LEARNING_BLOCK_DEMO_EXECUTION",
    )
    learning_block_live_execution: bool = Field(
        default=True,
        validation_alias="LEARNING_BLOCK_LIVE_EXECUTION",
    )
    learning_include_watchlist: bool = Field(
        default=True,
        validation_alias="LEARNING_INCLUDE_WATCHLIST",
    )
    learning_min_liquidity: Decimal = Field(
        default=Decimal("0"),
        validation_alias="LEARNING_MIN_LIQUIDITY",
    )
    learning_max_spread: Decimal = Field(
        default=Decimal("0.15"),
        validation_alias="LEARNING_MAX_SPREAD",
    )
    learning_duplicate_cooldown_hours: int = Field(
        default=24,
        validation_alias="LEARNING_DUPLICATE_COOLDOWN_HOURS",
    )
    learning_candidate_scan_limit: int = Field(
        default=500,
        validation_alias="LEARNING_CANDIDATE_SCAN_LIMIT",
    )
    model_confidence_min_settled_trades: int = Field(
        default=25,
        validation_alias="MODEL_CONFIDENCE_MIN_SETTLED_TRADES",
    )
    model_confidence_exploration_weight: Decimal = Field(
        default=Decimal("0.10"),
        validation_alias="MODEL_CONFIDENCE_EXPLORATION_WEIGHT",
    )
    sports_enabled: bool = Field(default=False, validation_alias="SPORTS_ENABLED")
    sports_leagues: str = Field(default="MLB,NBA,NFL,NHL", validation_alias="SPORTS_LEAGUES")
    sports_default_lookahead_days: int = Field(
        default=7,
        validation_alias="SPORTS_DEFAULT_LOOKAHEAD_DAYS",
    )
    sports_default_lookback_days: int = Field(
        default=30,
        validation_alias="SPORTS_DEFAULT_LOOKBACK_DAYS",
    )
    sports_min_link_confidence: Decimal = Field(
        default=Decimal("0.50"),
        validation_alias="SPORTS_MIN_LINK_CONFIDENCE",
    )
    sports_max_direct_links_per_market: int = Field(
        default=8,
        validation_alias="SPORTS_MAX_DIRECT_LINKS_PER_MARKET",
    )
    sports_require_specific_game_match: bool = Field(
        default=True,
        validation_alias="SPORTS_REQUIRE_SPECIFIC_GAME_MATCH",
    )
    sports_min_signal_confidence: Decimal = Field(
        default=Decimal("0.40"),
        validation_alias="SPORTS_MIN_SIGNAL_CONFIDENCE",
    )
    sports_user_agent: str = Field(
        default="kalshi-predictive-bot-sports/phase3j",
        validation_alias="SPORTS_USER_AGENT",
    )
    sports_odds_enabled: bool = Field(default=False, validation_alias="SPORTS_ODDS_ENABLED")
    sports_weather_enabled: bool = Field(default=True, validation_alias="SPORTS_WEATHER_ENABLED")
    sports_v1_max_adjustment: Decimal = Field(
        default=Decimal("0.08"),
        validation_alias="SPORTS_V1_MAX_ADJUSTMENT",
    )
    mlb_v1_max_adjustment: Decimal = Field(
        default=Decimal("0.08"),
        validation_alias="MLB_V1_MAX_ADJUSTMENT",
    )
    nba_v1_max_adjustment: Decimal = Field(
        default=Decimal("0.08"),
        validation_alias="NBA_V1_MAX_ADJUSTMENT",
    )
    nfl_v1_max_adjustment: Decimal = Field(
        default=Decimal("0.08"),
        validation_alias="NFL_V1_MAX_ADJUSTMENT",
    )
    nhl_v1_max_adjustment: Decimal = Field(
        default=Decimal("0.08"),
        validation_alias="NHL_V1_MAX_ADJUSTMENT",
    )
    microstructure_enabled: bool = Field(
        default=True,
        validation_alias="MICROSTRUCTURE_ENABLED",
    )
    microstructure_lookback_minutes: int = Field(
        default=60,
        validation_alias="MICROSTRUCTURE_LOOKBACK_MINUTES",
    )
    microstructure_short_lookback_minutes: int = Field(
        default=15,
        validation_alias="MICROSTRUCTURE_SHORT_LOOKBACK_MINUTES",
    )
    microstructure_min_snapshots: int = Field(
        default=3,
        validation_alias="MICROSTRUCTURE_MIN_SNAPSHOTS",
    )
    microstructure_spread_widen_threshold: Decimal = Field(
        default=Decimal("0.05"),
        validation_alias="MICROSTRUCTURE_SPREAD_WIDEN_THRESHOLD",
    )
    microstructure_spread_tighten_threshold: Decimal = Field(
        default=Decimal("0.03"),
        validation_alias="MICROSTRUCTURE_SPREAD_TIGHTEN_THRESHOLD",
    )
    microstructure_liquidity_change_threshold: Decimal = Field(
        default=Decimal("0.25"),
        validation_alias="MICROSTRUCTURE_LIQUIDITY_CHANGE_THRESHOLD",
    )
    microstructure_imbalance_threshold: Decimal = Field(
        default=Decimal("0.60"),
        validation_alias="MICROSTRUCTURE_IMBALANCE_THRESHOLD",
    )
    microstructure_late_move_threshold: Decimal = Field(
        default=Decimal("0.08"),
        validation_alias="MICROSTRUCTURE_LATE_MOVE_THRESHOLD",
    )
    microstructure_dislocation_threshold: Decimal = Field(
        default=Decimal("0.05"),
        validation_alias="MICROSTRUCTURE_DISLOCATION_THRESHOLD",
    )
    microstructure_smart_money_threshold: Decimal = Field(
        default=Decimal("0.70"),
        validation_alias="MICROSTRUCTURE_SMART_MONEY_THRESHOLD",
    )
    microstructure_v1_max_adjustment: Decimal = Field(
        default=Decimal("0.06"),
        validation_alias="MICROSTRUCTURE_V1_MAX_ADJUSTMENT",
    )

    @model_validator(mode="after")
    def validate_dynamic_position_sizing(self) -> "Settings":
        backend = self.db_backend.strip().lower()
        if backend not in {"sqlite", "postgres"}:
            raise ValueError("DB_BACKEND must be sqlite or postgres.")
        self.db_backend = backend
        if self.postgres_port <= 0:
            raise ValueError("POSTGRES_PORT must be positive.")
        mode_3o = self.phase_3o_market_memory_mode.strip().lower()
        if mode_3o not in {"disabled", "shadow_capture", "production_capture"}:
            raise ValueError(
                "PHASE_3O_MARKET_MEMORY_MODE must be disabled, shadow_capture, "
                "or production_capture."
            )
        self.phase_3o_market_memory_mode = mode_3o
        data_mode_3o = self.phase_3o_default_data_mode.strip().upper()
        if data_mode_3o not in {"AS_OBSERVED", "RECONCILED"}:
            raise ValueError("PHASE_3O_DEFAULT_DATA_MODE must be AS_OBSERVED or RECONCILED.")
        self.phase_3o_default_data_mode = data_mode_3o
        if self.phase_3o_schema_version <= 0:
            raise ValueError("PHASE_3O_SCHEMA_VERSION must be positive.")

        mode_3p = self.phase_3p_mode.strip().lower()
        if mode_3p not in {"disabled", "shadow", "production_journal"}:
            raise ValueError("PHASE_3P_MODE must be disabled, shadow, or production_journal.")
        self.phase_3p_mode = mode_3p
        data_mode_3p = self.phase_3p_data_mode.strip().upper()
        if data_mode_3p not in {"AS_OBSERVED", "RECONCILED"}:
            raise ValueError("PHASE_3P_DATA_MODE must be AS_OBSERVED or RECONCILED.")
        self.phase_3p_data_mode = data_mode_3p
        if self.phase_3p_minimum_current_sample < 0:
            raise ValueError("PHASE_3P_MINIMUM_CURRENT_SAMPLE cannot be negative.")
        if self.phase_3p_minimum_baseline_sample < 0:
            raise ValueError("PHASE_3P_MINIMUM_BASELINE_SAMPLE cannot be negative.")
        if self.phase_3p_minimum_practical_effect_size < 0:
            raise ValueError("PHASE_3P_MINIMUM_PRACTICAL_EFFECT_SIZE cannot be negative.")
        mode_3v = self.phase_3v_mode.strip().lower()
        if mode_3v not in {"disabled", "offline_review", "shadow_review"}:
            raise ValueError("PHASE_3V_MODE must be disabled, offline_review, or shadow_review.")
        self.phase_3v_mode = mode_3v
        stage_3v = self.phase_3v_default_target_stage.strip().upper()
        if stage_3v not in {"MICRO", "CONSTRAINED", "FULL"}:
            raise ValueError("PHASE_3V_DEFAULT_TARGET_STAGE must be MICRO, CONSTRAINED, or FULL.")
        self.phase_3v_default_target_stage = stage_3v
        if self.phase_3v_certificate_max_lifetime_hours <= 0:
            raise ValueError("PHASE_3V_CERTIFICATE_MAX_LIFETIME_HOURS must be positive.")
        if self.phase_3v_evidence_stale_after_days <= 0:
            raise ValueError("PHASE_3V_EVIDENCE_STALE_AFTER_DAYS must be positive.")
        if not (
            1
            <= self.phase_3v_micro_max_contracts_per_order
            <= self.phase_3v_constrained_max_contracts_per_order
            <= self.phase_3v_full_max_contracts_per_order
            <= 5
        ):
            raise ValueError("Phase 3V live readiness contract caps must be ordered 1..5.")
        mode_3w = self.phase_3w_mode.strip().upper()
        if mode_3w not in {
            "AUDIT_ONLY",
            "LOCAL_INTEGRATION",
            "STAGING_READ_ONLY",
            "SAFE_REPAIR",
        }:
            raise ValueError(
                "PHASE_3W_MODE must be AUDIT_ONLY, LOCAL_INTEGRATION, "
                "STAGING_READ_ONLY, or SAFE_REPAIR."
            )
        if mode_3w == "SAFE_REPAIR" and not self.phase_3w_safe_repair_enabled:
            raise ValueError("SAFE_REPAIR mode requires PHASE_3W_SAFE_REPAIR_ENABLED=true.")
        self.phase_3w_mode = mode_3w
        mode_3x = self.phase_3x_mode.strip().lower()
        if mode_3x not in {"audit_only", "preview", "staging", "production"}:
            raise ValueError(
                "PHASE_3X_MODE must be audit_only, preview, staging, or production."
            )
        if mode_3x == "production" and not self.phase_3w_system_certification_enabled:
            raise ValueError("Phase 3X production mode requires Phase 3W enabled evidence.")
        self.phase_3x_mode = mode_3x
        theme_3x = self.phase_3x_theme.strip().lower()
        if theme_3x not in {"light", "dark", "system"}:
            raise ValueError("PHASE_3X_THEME must be light, dark, or system.")
        self.phase_3x_theme = theme_3x
        density_3x = self.phase_3x_density.strip().lower()
        if density_3x not in {"comfortable", "compact"}:
            raise ValueError("PHASE_3X_DENSITY must be comfortable or compact.")
        self.phase_3x_density = density_3x

        mode = self.dynamic_position_sizing_mode.strip().lower()
        if mode not in {"disabled", "shadow", "live"}:
            raise ValueError(
                "DYNAMIC_POSITION_SIZING_MODE must be disabled, shadow, or live."
            )
        self.dynamic_position_sizing_mode = mode

        weights = (
            self.dynamic_position_sizing_weight_confidence,
            self.dynamic_position_sizing_weight_opportunity,
            self.dynamic_position_sizing_weight_liquidity,
            self.dynamic_position_sizing_weight_historical_accuracy,
            self.dynamic_position_sizing_weight_drawdown_health,
        )
        if any(weight < 0 or not weight.is_finite() for weight in weights):
            raise ValueError("Dynamic position sizing weights must be finite and non-negative.")
        if abs(sum(weights, Decimal("0")) - Decimal("1.0")) > Decimal("0.000000001"):
            raise ValueError("Dynamic position sizing weights must sum to 1.0.")

        if not (
            Decimal("0")
            <= self.dynamic_position_sizing_medium_score
            < self.dynamic_position_sizing_high_score
            <= Decimal("1")
        ):
            raise ValueError("Dynamic position sizing score thresholds are invalid.")
        if not (
            Decimal("0")
            <= self.dynamic_position_sizing_liquidity_one_contract_below
            < self.dynamic_position_sizing_liquidity_three_contracts_below
            <= Decimal("1")
        ):
            raise ValueError("Dynamic position sizing liquidity thresholds are invalid.")
        if not (
            Decimal("0")
            <= self.dynamic_position_sizing_drawdown_three_contracts_at
            < self.dynamic_position_sizing_drawdown_one_contract_at
            < self.dynamic_position_sizing_drawdown_kill_at
        ):
            raise ValueError("Dynamic position sizing drawdown thresholds are invalid.")
        if self.dynamic_position_sizing_history_prior_weight < 0:
            raise ValueError("Dynamic position sizing prior weight cannot be negative.")
        if self.dynamic_position_sizing_minimum_samples_for_high < 0:
            raise ValueError("Dynamic position sizing minimum sample count cannot be negative.")
        if self.dynamic_position_sizing_history_prior_accuracy < 0 or (
            self.dynamic_position_sizing_history_prior_accuracy > 1
        ):
            raise ValueError("Dynamic position sizing prior accuracy must be in [0, 1].")
        for name, value in (
            (
                "DYNAMIC_POSITION_SIZING_LIVE_MAX_CONTRACTS",
                self.dynamic_position_sizing_live_max_contracts,
            ),
            (
                "DYNAMIC_POSITION_SIZING_GLOBAL_MAX_CONTRACTS",
                self.dynamic_position_sizing_global_max_contracts,
            ),
        ):
            if value not in {1, 3, 5}:
                raise ValueError(f"{name} must be one of 1, 3, or 5.")
        for name, value in (
            (
                "DYNAMIC_POSITION_SIZING_EXTERNAL_RISK_CAP",
                self.dynamic_position_sizing_external_risk_cap,
            ),
            ("DYNAMIC_POSITION_SIZING_MARGIN_CAP", self.dynamic_position_sizing_margin_cap),
            (
                "DYNAMIC_POSITION_SIZING_PORTFOLIO_CAP",
                self.dynamic_position_sizing_portfolio_cap,
            ),
        ):
            if value is not None and value < 0:
                raise ValueError(f"{name} cannot be negative.")
        self._validate_advanced_risk_engine()
        return self

    def _validate_advanced_risk_engine(self) -> None:
        mode = self.advanced_risk_engine_mode.strip().lower()
        if mode not in {"disabled", "shadow", "live"}:
            raise ValueError("ADVANCED_RISK_ENGINE_MODE must be disabled, shadow, or live.")
        self.advanced_risk_engine_mode = mode

        for name, value in (
            ("ADVANCED_RISK_LIVE_MAX_CONTRACTS", self.advanced_risk_live_max_contracts),
            ("ADVANCED_RISK_GLOBAL_MAX_CONTRACTS", self.advanced_risk_global_max_contracts),
            (
                "ADVANCED_RISK_DRAWDOWN_WARNING_ONE_CAP",
                self.advanced_risk_drawdown_warning_one_cap,
            ),
            (
                "ADVANCED_RISK_DRAWDOWN_WARNING_TWO_CAP",
                self.advanced_risk_drawdown_warning_two_cap,
            ),
            (
                "ADVANCED_RISK_KELLY_INSUFFICIENT_DATA_CAP",
                self.advanced_risk_kelly_insufficient_data_cap,
            ),
            (
                "ADVANCED_RISK_EV_INSUFFICIENT_DATA_CAP",
                self.advanced_risk_ev_insufficient_data_cap,
            ),
        ):
            if value not in {0, 1, 3, 5}:
                raise ValueError(f"{name} must be one of 0, 1, 3, or 5.")

        for name, value in (
            ("ADVANCED_RISK_DEFAULT_ACCOUNT_EQUITY", self.advanced_risk_default_account_equity),
            (
                "ADVANCED_RISK_MAX_TOTAL_OPEN_RISK_FRACTION",
                self.advanced_risk_max_total_open_risk_fraction,
            ),
            (
                "ADVANCED_RISK_DEFAULT_CATEGORY_RISK_FRACTION",
                self.advanced_risk_default_category_risk_fraction,
            ),
            (
                "ADVANCED_RISK_DEFAULT_MODEL_RISK_FRACTION",
                self.advanced_risk_default_model_risk_fraction,
            ),
            ("ADVANCED_RISK_UNREALIZED_PNL_WEIGHT", self.advanced_risk_unrealized_pnl_weight),
            (
                "ADVANCED_RISK_DAILY_LOSS_RESERVE_AMOUNT",
                self.advanced_risk_daily_loss_reserve_amount,
            ),
            ("ADVANCED_RISK_MAX_DRAWDOWN_FRACTION", self.advanced_risk_max_drawdown_fraction),
            ("ADVANCED_RISK_DRAWDOWN_RESERVE_AMOUNT", self.advanced_risk_drawdown_reserve_amount),
            (
                "ADVANCED_RISK_MAX_INSTRUMENT_RISK_FRACTION",
                self.advanced_risk_max_instrument_risk_fraction,
            ),
            (
                "ADVANCED_RISK_SPREAD_PREFERRED_MAX_TICKS",
                self.advanced_risk_spread_preferred_max_ticks,
            ),
            (
                "ADVANCED_RISK_SPREAD_ELEVATED_MAX_TICKS",
                self.advanced_risk_spread_elevated_max_ticks,
            ),
            (
                "ADVANCED_RISK_SPREAD_EXECUTABLE_MAX_TICKS",
                self.advanced_risk_spread_executable_max_ticks,
            ),
            (
                "ADVANCED_RISK_MAX_DEPTH_PARTICIPATION_FRACTION",
                self.advanced_risk_max_depth_participation_fraction,
            ),
            (
                "ADVANCED_RISK_MAX_RECENT_VOLUME_PARTICIPATION_FRACTION",
                self.advanced_risk_max_recent_volume_participation_fraction,
            ),
            (
                "ADVANCED_RISK_CONFIDENCE_DEFENSIVE_MEDIUM_FLOOR",
                self.advanced_risk_confidence_defensive_medium_floor,
            ),
            (
                "ADVANCED_RISK_CONFIDENCE_DEFENSIVE_HIGH_FLOOR",
                self.advanced_risk_confidence_defensive_high_floor,
            ),
            (
                "ADVANCED_RISK_KELLY_PRIOR_WIN_PROBABILITY",
                self.advanced_risk_kelly_prior_win_probability,
            ),
            (
                "ADVANCED_RISK_FRACTIONAL_KELLY_MULTIPLIER",
                self.advanced_risk_fractional_kelly_multiplier,
            ),
            (
                "ADVANCED_RISK_MAX_APPLIED_KELLY_FRACTION",
                self.advanced_risk_max_applied_kelly_fraction,
            ),
            ("ADVANCED_RISK_MAX_TRADE_RISK_FRACTION", self.advanced_risk_max_trade_risk_fraction),
            ("ADVANCED_RISK_EV_PROBABILITY_Z_SCORE", self.advanced_risk_ev_probability_z_score),
            ("ADVANCED_RISK_CVAR_WEIGHT", self.advanced_risk_cvar_weight),
            (
                "ADVANCED_RISK_ESTIMATED_SLIPPAGE_PER_CONTRACT",
                self.advanced_risk_estimated_slippage_per_contract,
            ),
            (
                "ADVANCED_RISK_GAP_TAIL_BUFFER_PER_CONTRACT",
                self.advanced_risk_gap_tail_buffer_per_contract,
            ),
        ):
            if value < 0 or not value.is_finite():
                raise ValueError(f"{name} must be finite and non-negative.")

        for name, value in (
            ("ADVANCED_RISK_MAX_DAILY_LOSS_FRACTION", self.advanced_risk_max_daily_loss_fraction),
            (
                "ADVANCED_RISK_MAX_DAILY_LOSS_FIXED_AMOUNT",
                self.advanced_risk_max_daily_loss_fixed_amount,
            ),
            (
                "ADVANCED_RISK_MAX_CORRELATION_GROUP_RISK_FRACTION",
                self.advanced_risk_max_correlation_group_risk_fraction,
            ),
            (
                "ADVANCED_RISK_MAX_ADV_PARTICIPATION_FRACTION",
                self.advanced_risk_max_adv_participation_fraction,
            ),
            (
                "ADVANCED_RISK_MAX_OPEN_INTEREST_PARTICIPATION_FRACTION",
                self.advanced_risk_max_open_interest_participation_fraction,
            ),
        ):
            if value is not None and (value < 0 or not value.is_finite()):
                raise ValueError(f"{name} must be finite and non-negative when set.")

        if (
            self.advanced_risk_max_daily_loss_fraction is not None
            and self.advanced_risk_max_daily_loss_fixed_amount is not None
        ):
            raise ValueError(
                "Configure either ADVANCED_RISK_MAX_DAILY_LOSS_FRACTION or "
                "ADVANCED_RISK_MAX_DAILY_LOSS_FIXED_AMOUNT, not both."
            )
        if (
            self.advanced_risk_max_daily_loss_fraction is None
            and self.advanced_risk_max_daily_loss_fixed_amount is None
        ):
            raise ValueError("Advanced risk daily loss requires a fraction or fixed amount.")

        unit_fraction_fields = (
            self.advanced_risk_max_total_open_risk_fraction,
            self.advanced_risk_default_category_risk_fraction,
            self.advanced_risk_default_model_risk_fraction,
            self.advanced_risk_max_daily_loss_fraction or Decimal("0"),
            self.advanced_risk_unrealized_pnl_weight,
            self.advanced_risk_max_drawdown_fraction,
            self.advanced_risk_max_instrument_risk_fraction,
            self.advanced_risk_max_correlation_group_risk_fraction or Decimal("0"),
            self.advanced_risk_max_depth_participation_fraction,
            self.advanced_risk_max_recent_volume_participation_fraction,
            self.advanced_risk_max_adv_participation_fraction or Decimal("0"),
            self.advanced_risk_max_open_interest_participation_fraction or Decimal("0"),
            self.advanced_risk_confidence_defensive_medium_floor,
            self.advanced_risk_confidence_defensive_high_floor,
            self.advanced_risk_kelly_prior_win_probability,
            self.advanced_risk_fractional_kelly_multiplier,
            self.advanced_risk_max_applied_kelly_fraction,
            self.advanced_risk_max_trade_risk_fraction,
        )
        if any(value > 1 for value in unit_fraction_fields):
            raise ValueError("Advanced risk fraction settings must be within [0, 1].")
        if not (
            self.advanced_risk_drawdown_warning_one_utilization
            < self.advanced_risk_drawdown_warning_two_utilization
            < self.advanced_risk_drawdown_kill_utilization
        ):
            raise ValueError("Advanced risk drawdown warning thresholds are not ordered.")
        if not (
            self.advanced_risk_spread_preferred_max_ticks
            < self.advanced_risk_spread_elevated_max_ticks
            < self.advanced_risk_spread_executable_max_ticks
        ):
            raise ValueError("Advanced risk spread thresholds are not ordered.")
        if not (
            self.advanced_risk_minimum_trade_ev_to_risk
            < self.advanced_risk_medium_ev_to_risk
            < self.advanced_risk_high_ev_to_risk
        ):
            raise ValueError("Advanced risk EV thresholds are not ordered.")
        if self.advanced_risk_kelly_prior_weight < 0:
            raise ValueError("ADVANCED_RISK_KELLY_PRIOR_WEIGHT cannot be negative.")
        if self.advanced_risk_kelly_minimum_sample_size < 0:
            raise ValueError("ADVANCED_RISK_KELLY_MINIMUM_SAMPLE_SIZE cannot be negative.")
        if self.advanced_risk_ev_minimum_sample_size < 0:
            raise ValueError("ADVANCED_RISK_EV_MINIMUM_SAMPLE_SIZE cannot be negative.")
        if self.advanced_risk_portfolio_snapshot_max_age_ms <= 0:
            raise ValueError("ADVANCED_RISK_PORTFOLIO_SNAPSHOT_MAX_AGE_MS must be positive.")
        if self.advanced_risk_quote_max_age_ms <= 0:
            raise ValueError("ADVANCED_RISK_QUOTE_MAX_AGE_MS must be positive.")
        if self.advanced_risk_unknown_category_action not in {"block", "cap_to_one"}:
            raise ValueError("ADVANCED_RISK_UNKNOWN_CATEGORY_ACTION must be block or cap_to_one.")
        if self.advanced_risk_unknown_model_action not in {"block", "cap_to_one"}:
            raise ValueError("ADVANCED_RISK_UNKNOWN_MODEL_ACTION must be block or cap_to_one.")
        if self.advanced_risk_missing_edge_statistics_action not in {"block", "cap_to_one"}:
            raise ValueError(
                "ADVANCED_RISK_MISSING_EDGE_STATISTICS_ACTION must be block or cap_to_one."
            )
        if self.advanced_risk_missing_optional_liquidity_data_action not in {
            "block",
            "cap_to_one",
        }:
            raise ValueError(
                "ADVANCED_RISK_MISSING_OPTIONAL_LIQUIDITY_DATA_ACTION must be block "
                "or cap_to_one."
            )


@lru_cache
def get_settings() -> Settings:
    return Settings()
