# ruff: noqa: E501

from __future__ import annotations

import importlib.util
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

PHASE_REGISTRY_VERSION = "phase_3w_r_authoritative_phase_registry_v1"

IMPLEMENTED_VERIFIED = "IMPLEMENTED_VERIFIED"
IMPLEMENTED_UNVERIFIED = "IMPLEMENTED_UNVERIFIED"
PARTIALLY_IMPLEMENTED = "PARTIALLY_IMPLEMENTED"
NOT_IMPLEMENTED = "NOT_IMPLEMENTED"
MAPPING_ERROR = "MAPPING_ERROR"
OUT_OF_SCOPE_FOR_REPOSITORY = "OUT_OF_SCOPE_FOR_REPOSITORY"

PHASE_STATES = {
    IMPLEMENTED_VERIFIED,
    IMPLEMENTED_UNVERIFIED,
    PARTIALLY_IMPLEMENTED,
    NOT_IMPLEMENTED,
    MAPPING_ERROR,
    OUT_OF_SCOPE_FOR_REPOSITORY,
}

MANDATORY_PHASE_IDS: tuple[str, ...] = (
    "1",
    "2",
    "2.5",
    "2.6",
    "2.7",
    "2.8",
    "2.9",
    "3A",
    "3B",
    "3C",
    "3D",
    "3E",
    "3F",
    "3G",
    "3H",
    "3I",
    "3J",
    "3K",
    "3L",
    "3M",
    "3N",
    "3O",
    "3P",
    "3Q",
    "3R",
    "3S",
    "3T",
    "3U",
    "3V",
)


@dataclass(frozen=True)
class PhaseRegistryEntry:
    phase_id: str
    name: str
    capability: str
    implementation_modules: tuple[str, ...]
    config_sources: tuple[str, ...]
    schema_or_table_refs: tuple[str, ...]
    cli_or_service_entrypoints: tuple[str, ...]
    producer_contracts: tuple[str, ...]
    consumer_contracts: tuple[str, ...]
    health_probe: str
    test_selectors: tuple[str, ...]
    replay_selector: str
    minimum_evidence_grade: str
    runtime_required: bool
    safety_classification: str
    owner_or_todo: str
    implementation_state: str = IMPLEMENTED_UNVERIFIED

    def to_dict(self, *, root: Path | None = None) -> dict[str, Any]:
        payload = asdict(self)
        payload["implementation_evidence"] = implementation_evidence(self, root=root)
        return payload

    def to_legacy_phase(self) -> dict[str, Any]:
        return {
            "phase_id": self.phase_id,
            "name": self.name,
            "locations": [
                _module_to_location(module_name) for module_name in self.implementation_modules
            ],
            "feature_flags": list(self.config_sources),
            "inputs": list(self.consumer_contracts),
            "outputs": list(self.producer_contracts),
        }


def entry(
    phase_id: str,
    name: str,
    capability: str,
    implementation_modules: tuple[str, ...],
    config_sources: tuple[str, ...],
    schema_or_table_refs: tuple[str, ...],
    cli_or_service_entrypoints: tuple[str, ...],
    producer_contracts: tuple[str, ...],
    consumer_contracts: tuple[str, ...],
    health_probe: str,
    test_selectors: tuple[str, ...],
    replay_selector: str,
    minimum_evidence_grade: str,
    runtime_required: bool,
    safety_classification: str,
    owner_or_todo: str = "Phase owner to attach E2+ evidence during certification.",
    implementation_state: str = IMPLEMENTED_UNVERIFIED,
) -> PhaseRegistryEntry:
    return PhaseRegistryEntry(
        phase_id=phase_id,
        name=name,
        capability=capability,
        implementation_modules=implementation_modules,
        config_sources=config_sources,
        schema_or_table_refs=schema_or_table_refs,
        cli_or_service_entrypoints=cli_or_service_entrypoints,
        producer_contracts=producer_contracts,
        consumer_contracts=consumer_contracts,
        health_probe=health_probe,
        test_selectors=test_selectors,
        replay_selector=replay_selector,
        minimum_evidence_grade=minimum_evidence_grade,
        runtime_required=runtime_required,
        safety_classification=safety_classification,
        owner_or_todo=owner_or_todo,
        implementation_state=implementation_state,
    )


PHASE_REGISTRY: tuple[PhaseRegistryEntry, ...] = (
    entry(
        "1",
        "Data ingestion, snapshots, settlements",
        "Collects market snapshots and settlement records used by downstream models.",
        ("kalshi_predictor.ingest", "kalshi_predictor.kalshi"),
        ("KALSHI_ENV", "KALSHI_BASE_URL"),
        ("markets", "market_snapshots", "settlements"),
        ("kalshi-bot collect-once", "kalshi_predictor.ingest"),
        ("market_snapshot_contract", "settlement_contract"),
        ("kalshi_market_feed",),
        "kalshi-bot db-health",
        ("tests/test_phase_3w_system_certification.py",),
        "golden_trace.ingestion",
        "E2_TEST",
        True,
        "READ_ONLY_INGESTION",
    ),
    entry(
        "2",
        "Paper trading ledger",
        "Creates and settles paper-only orders, fills, and positions.",
        ("kalshi_predictor.paper",),
        ("PAPER_MIN_EDGE", "PAPER_MAX_ORDER_QUANTITY"),
        ("paper_orders", "paper_fills", "paper_positions"),
        ("kalshi-bot paper-summary", "kalshi_predictor.paper"),
        ("paper_trade_contract", "paper_pnl_contract"),
        ("ranking_contract", "snapshot_contract"),
        "kalshi-bot paper-summary",
        ("tests/test_learning_mode.py", "tests/test_phase_3w_system_certification.py"),
        "golden_trace.paper_trade",
        "E3_REPLAY",
        True,
        "PAPER_ONLY",
    ),
    entry(
        "2.5",
        "Backtesting and feature store",
        "Builds point-in-time features and backtest datasets.",
        ("kalshi_predictor.backtesting", "kalshi_predictor.features"),
        ("FEATURE_STORE_ENABLED",),
        ("feature_vectors", "backtest_runs"),
        ("kalshi-bot backtest", "kalshi_predictor.features"),
        ("point_in_time_feature_contract",),
        ("historical_snapshot_contract",),
        "kalshi-bot model-diagnostics",
        ("tests/test_phase_3w_system_certification.py",),
        "golden_trace.features",
        "E2_TEST",
        False,
        "RESEARCH_ONLY",
    ),
    entry(
        "2.6",
        "Opportunity scanner and leaderboard",
        "Ranks forecasted markets and produces candidate opportunity records.",
        ("kalshi_predictor.opportunities", "kalshi_predictor.leaderboard"),
        ("LEARNING_MIN_OPPORTUNITY_SCORE",),
        ("market_rankings", "opportunity_reports"),
        ("kalshi-bot find-opportunities", "kalshi-bot leaderboard"),
        ("candidate_universe_contract", "opportunity_score_contract"),
        ("forecast_probability_contract", "snapshot_contract"),
        "kalshi-bot learning-diagnostics",
        ("tests/test_learning_mode.py",),
        "golden_trace.opportunity",
        "E3_REPLAY",
        True,
        "PAPER_ONLY",
    ),
    entry(
        "2.7",
        "Crypto v2",
        "Links crypto-relevant markets to external crypto features.",
        ("kalshi_predictor.crypto",),
        ("CRYPTO_FEATURES_ENABLED",),
        ("crypto_market_links", "crypto_features"),
        ("kalshi-bot ingest-crypto", "kalshi-bot build-crypto-features"),
        ("crypto_prediction_contract",),
        ("crypto_feed_contract", "market_link_contract"),
        "kalshi-bot signals-status",
        ("tests/test_signals_readiness.py",),
        "golden_trace.crypto",
        "E2_TEST",
        False,
        "PAPER_ONLY",
    ),
    entry(
        "2.8",
        "Weather v2",
        "Links weather markets to weather features and forecasts.",
        ("kalshi_predictor.weather",),
        ("WEATHER_FEATURES_ENABLED",),
        ("weather_market_links", "weather_features"),
        ("kalshi-bot ingest-weather", "kalshi-bot build-weather-features"),
        ("weather_prediction_contract",),
        ("weather_feed_contract", "market_link_contract"),
        "kalshi-bot tonight-check",
        ("tests/test_phase_3w_system_certification.py",),
        "golden_trace.weather",
        "E2_TEST",
        False,
        "PAPER_ONLY",
    ),
    entry(
        "2.9",
        "Tournament and ensemble v2",
        "Combines model outputs and maintains model weights.",
        ("kalshi_predictor.tournament", "kalshi_predictor.forecasting"),
        ("ENSEMBLE_MODEL_NAME",),
        ("model_weights", "model_diagnostics"),
        ("kalshi-bot model-health", "kalshi-bot forecast --model all"),
        ("ensemble_forecast_contract",),
        ("domain_forecast_contract", "historical_outcome_contract"),
        "kalshi-bot model-health",
        ("tests/test_phase_3w_system_certification.py",),
        "golden_trace.ensemble",
        "E2_TEST",
        True,
        "PAPER_ONLY",
    ),
    entry(
        "3A",
        "Demo execution and decision UI",
        "Presents decision detail and demo-only execution controls.",
        ("kalshi_predictor.ui",),
        ("EXECUTION_ENABLED", "EXECUTION_DRY_RUN", "UI_READ_ONLY"),
        ("execution_reports", "decision_events"),
        ("kalshi-bot ui",),
        ("ui_action_contract",),
        ("risk_approval_contract",),
        "GET /system/health",
        ("tests/test_phase_3w_system_certification.py",),
        "golden_trace.ui_decision",
        "E3_REPLAY",
        True,
        "DEMO_GATED",
    ),
    entry(
        "3B",
        "Autopilot and guardrails",
        "Runs guarded autopilot cycles without enabling live execution.",
        ("kalshi_predictor.autopilot",),
        ("AUTOPILOT_ENABLED", "AUTOPILOT_DRY_RUN"),
        ("autopilot_runs", "risk_events"),
        ("kalshi-bot autopilot-once",),
        ("guarded_intent_contract",),
        ("ranking_contract", "risk_guardrail_contract"),
        "kalshi-bot tonight-check",
        ("tests/test_phase_3w_system_certification.py",),
        "golden_trace.autopilot",
        "E3_REPLAY",
        True,
        "PAPER_DEMO_ONLY",
    ),
    entry(
        "3C",
        "Human-readable UI",
        "Renders operator views and explanations.",
        ("kalshi_predictor.ui", "kalshi_predictor.explain"),
        ("UI_READ_ONLY",),
        ("explanations", "operator_views"),
        ("kalshi-bot ui",),
        ("operator_view_contract",),
        ("decision_explanation_contract",),
        "GET /today",
        ("tests/test_phase_3x_professional_ux.py",),
        "golden_trace.operator_ui",
        "E2_TEST",
        True,
        "READ_ONLY_UI",
    ),
    entry(
        "3D",
        "Trader workstation",
        "Builds portfolio, watchlist, and workstation reports.",
        ("kalshi_predictor.workstation",),
        ("WORKSTATION_ENABLED",),
        ("portfolio_snapshots", "watchlists"),
        ("kalshi-bot daily-briefing",),
        ("workstation_report_contract",),
        ("portfolio_contract", "alert_contract"),
        "kalshi-bot portfolio-summary",
        ("tests/test_phase_3w_system_certification.py",),
        "golden_trace.workstation",
        "E2_TEST",
        False,
        "READ_ONLY_REPORTING",
    ),
    entry(
        "3E",
        "Opportunity intelligence and best payouts",
        "Calculates payout-aware opportunity quality.",
        ("kalshi_predictor.opportunities",),
        ("BEST_PAYOUTS_ENABLED",),
        ("best_payouts", "opportunity_scores"),
        ("kalshi-bot find-opportunities",),
        ("best_payout_contract",),
        ("candidate_universe_contract",),
        "GET /opportunities/best-payouts",
        ("tests/test_opportunities_routes.py",),
        "golden_trace.best_payouts",
        "E2_TEST",
        True,
        "PAPER_ONLY",
    ),
    entry(
        "3F",
        "Learning Mode and model confidence",
        "Generates paper learning trades and model confidence state.",
        ("kalshi_predictor.learning", "kalshi_predictor.confidence"),
        ("LEARNING_MODE",),
        ("learning_cycles", "model_confidence", "paper_trades"),
        ("kalshi-bot learning-once", "kalshi-bot model-confidence"),
        ("learning_cycle_contract", "confidence_contract"),
        ("settled_paper_trade_contract",),
        "kalshi-bot learning-status",
        ("tests/test_learning_mode.py",),
        "golden_trace.learning",
        "E3_REPLAY",
        True,
        "PAPER_ONLY",
    ),
    entry(
        "3G",
        "PostgreSQL and database hardening",
        "Owns database backend selection, migration, backup, and restore checks.",
        ("kalshi_predictor.data", "kalshi_predictor.data.maintenance"),
        ("DB_BACKEND", "DATABASE_URL"),
        ("alembic_version", "database_health"),
        ("kalshi-bot db-health", "kalshi-bot db-migrate"),
        ("durable_storage_contract", "migration_contract"),
        ("all_durable_phase_contracts",),
        "kalshi-bot db-health",
        ("tests/test_phase_3g_database_hardening.py",),
        "golden_trace.database",
        "E2_TEST",
        True,
        "PLATFORM_SERVICE",
    ),
    entry(
        "3H",
        "Quick-settlement hunter",
        "Prioritizes fast-settlement learning targets.",
        ("kalshi_predictor.learning", "kalshi_predictor.tonight"),
        ("LEARNING_PRIORITIZE_FAST_SETTLEMENT",),
        ("learning_targets", "tonight_runs"),
        ("kalshi-bot tonight-run", "kalshi-bot learning-once"),
        ("fast_settlement_target_contract",),
        ("market_close_contract", "settlement_eta_contract"),
        "kalshi-bot tonight-check",
        ("tests/test_learning_mode.py",),
        "golden_trace.fast_settlement",
        "E2_TEST",
        True,
        "PAPER_ONLY",
    ),
    entry(
        "3I",
        "News intelligence",
        "Builds news items and news signal features.",
        ("kalshi_predictor.news",),
        ("NEWS_INGESTION_ENABLED",),
        ("news_items", "news_signal_features"),
        ("kalshi-bot ingest-news", "kalshi-bot build-news-features"),
        ("news_feature_contract",),
        ("news_item_contract",),
        "kalshi-bot signals-status",
        ("tests/test_signals_readiness.py",),
        "golden_trace.news",
        "E2_TEST",
        False,
        "PAPER_ONLY",
    ),
    entry(
        "3J",
        "Sports intelligence",
        "Builds sports schedules, market links, and sports forecasts.",
        ("kalshi_predictor.sports",),
        ("SPORTS_ENABLED",),
        ("sports_events", "sports_market_links", "sports_features"),
        ("kalshi-bot ingest-sports", "kalshi-bot sports-report"),
        ("sports_prediction_contract",),
        ("sports_schedule_contract", "market_link_contract"),
        "kalshi-bot sports-report",
        ("tests/test_sports.py",),
        "golden_trace.sports",
        "E2_TEST",
        False,
        "PAPER_ONLY",
    ),
    entry(
        "3K",
        "Market microstructure",
        "Calculates liquidity, spread, and slippage quality.",
        ("kalshi_predictor.microstructure",),
        ("MICROSTRUCTURE_ENABLED",),
        ("microstructure_features", "order_book_snapshots"),
        ("kalshi-bot microstructure-report",),
        ("liquidity_quality_contract",),
        ("order_book_contract", "snapshot_contract"),
        "kalshi-bot microstructure-report",
        ("tests/test_phase_3w_system_certification.py",),
        "golden_trace.microstructure",
        "E2_TEST",
        True,
        "PAPER_ONLY",
    ),
    entry(
        "3L",
        "Meta model",
        "Produces final probabilities and uncertainty from domain features.",
        ("kalshi_predictor.meta",),
        ("META_MODEL_ENABLED",),
        ("meta_decisions", "meta_reports"),
        ("kalshi-bot forecast --model all",),
        ("final_probability_contract",),
        ("domain_prediction_contract", "feature_contract"),
        "kalshi-bot model-health",
        ("tests/test_phase_3w_system_certification.py",),
        "golden_trace.meta",
        "E3_REPLAY",
        True,
        "PAPER_ONLY",
    ),
    entry(
        "3M",
        "Dynamic position sizing",
        "Converts opportunities into immutable size proposals.",
        ("kalshi_predictor.position_sizing",),
        ("DYNAMIC_POSITION_SIZING_MODE",),
        ("position_sizing_decisions",),
        ("kalshi-bot position-sizing-report",),
        ("size_proposal_contract",),
        ("opportunity_contract", "confidence_contract", "liquidity_contract"),
        "kalshi-bot position-sizing-report",
        ("tests/test_position_sizing.py",),
        "golden_trace.position_sizing",
        "E3_REPLAY",
        True,
        "PAPER_ONLY",
    ),
    entry(
        "3N",
        "Advanced risk engine",
        "Allows, reduces, or blocks proposed risk before any execution path.",
        ("kalshi_predictor.advanced_risk",),
        ("ADVANCED_RISK_ENGINE_MODE",),
        ("advanced_risk_decisions", "risk_reservations"),
        ("kalshi-bot advanced-risk-report",),
        ("risk_decision_contract", "risk_reservation_contract"),
        ("size_proposal_contract", "portfolio_state_contract"),
        "kalshi-bot advanced-risk-report",
        ("tests/test_phase_3n_advanced_risk.py",),
        "golden_trace.risk",
        "E3_REPLAY",
        True,
        "LIVE_BLOCKING_SAFETY",
    ),
    entry(
        "3O",
        "Market Memory",
        "Stores market, forecast, trade, and settlement memory.",
        ("kalshi_predictor.memory",),
        ("PHASE_3O_MARKET_MEMORY_ENABLED",),
        ("market_memory", "forecast_memory", "trade_memory"),
        ("kalshi-bot market-memory-report",),
        ("memory_event_contract",),
        ("market_event_contract", "trade_event_contract"),
        "kalshi-bot market-memory-status",
        ("tests/test_phase_3o_market_memory.py",),
        "golden_trace.market_memory",
        "E2_TEST",
        True,
        "PAPER_ONLY_MEMORY",
    ),
    entry(
        "3P",
        "Self-Evaluation Engine",
        "Writes self-evaluation journals from memory and outcomes.",
        ("kalshi_predictor.self_evaluation",),
        ("PHASE_3P_SELF_EVALUATION_ENABLED",),
        ("self_evaluation_runs", "self_evaluation_journals"),
        ("kalshi-bot self-evaluation-run",),
        ("evaluation_journal_contract",),
        ("memory_dataset_contract", "settled_outcome_contract"),
        "kalshi-bot self-evaluation-status",
        ("tests/test_phase_3p_self_evaluation.py",),
        "golden_trace.self_evaluation",
        "E2_TEST",
        False,
        "RESEARCH_ONLY",
    ),
    entry(
        "3Q",
        "Auto Feature Discovery",
        "Runs governed feature discovery without auto-promotion.",
        ("kalshi_predictor.feature_discovery",),
        ("PHASE_3Q_FEATURE_DISCOVERY_ENABLED",),
        ("feature_discovery_runs", "feature_candidates"),
        ("kalshi-bot feature-discovery-run",),
        ("feature_candidate_contract",),
        ("memory_dataset_contract", "outcome_contract"),
        "kalshi-bot feature-discovery-status",
        ("tests/test_phase_3q_feature_discovery.py",),
        "golden_trace.feature_discovery",
        "E2_TEST",
        False,
        "GOVERNED_RESEARCH",
    ),
    entry(
        "3R",
        "Synthetic Markets",
        "Researches non-tradable synthetic markets with isolation flags.",
        ("kalshi_predictor.synthetic_markets",),
        ("PHASE_3R_SYNTHETIC_MARKETS_ENABLED",),
        ("synthetic_market_runs", "synthetic_market_events"),
        ("kalshi-bot synthetic-markets-run",),
        ("synthetic_market_contract",),
        ("synthetic_candidate_contract",),
        "kalshi-bot synthetic-markets-status",
        ("tests/test_phase_3r_synthetic_markets.py",),
        "golden_trace.synthetic_markets",
        "E2_TEST",
        False,
        "NON_TRADABLE_RESEARCH",
    ),
    entry(
        "3S",
        "Reinforcement Learning Layer",
        "Produces shadow SKIP/PROCEED behavior policy decisions.",
        ("kalshi_predictor.reinforcement_learning",),
        ("PHASE_3S_REINFORCEMENT_LEARNING_ENABLED",),
        ("rl_policy_runs", "rl_policy_decisions"),
        ("kalshi-bot rl-run",),
        ("behavior_policy_contract",),
        ("context_reward_contract",),
        "kalshi-bot rl-status",
        ("tests/test_phase_3s_reinforcement_learning.py",),
        "golden_trace.rl_policy",
        "E2_TEST",
        True,
        "SHADOW_POLICY_ONLY",
    ),
    entry(
        "3T",
        "Institutional Dashboard",
        "Provides read-only institutional snapshots and drilldowns.",
        ("kalshi_predictor.institutional_dashboard",),
        ("PHASE_3T_INSTITUTIONAL_DASHBOARD_ENABLED",),
        ("institutional_dashboard_snapshots",),
        ("kalshi-bot phase3t-report", "GET /institutional"),
        ("dashboard_read_model_contract",),
        ("typed_read_model_contract", "freshness_contract"),
        "GET /system/health",
        ("tests/test_phase_3t_institutional_dashboard.py",),
        "golden_trace.institutional_dashboard",
        "E2_TEST",
        True,
        "READ_ONLY_UI",
    ),
    entry(
        "3U",
        "Personal AI Trader",
        "Builds advisory-only ranked briefings for the user.",
        ("kalshi_predictor.personal_trader",),
        ("PHASE_3U_PERSONAL_AI_TRADER_ENABLED",),
        ("personal_trader_briefs",),
        ("kalshi-bot personal-trader-brief",),
        ("advisory_brief_contract",),
        ("opportunity_contract", "risk_contract", "portfolio_scope_contract"),
        "kalshi-bot personal-trader-status",
        ("tests/test_phase_3u_personal_trader.py",),
        "golden_trace.personal_trader",
        "E2_TEST",
        False,
        "ADVISORY_ONLY",
    ),
    entry(
        "3V",
        "Live Trading Readiness Review",
        "Reviews evidence and guards any future live trading envelope.",
        ("kalshi_predictor.live_readiness",),
        ("PHASE_3V_LIVE_READINESS_ENABLED",),
        ("live_readiness_run", "live_readiness_certificate"),
        ("kalshi-bot live-readiness-run",),
        ("readiness_certificate_contract",),
        ("evidence_manifest_contract", "control_catalog_contract"),
        "kalshi-bot live-readiness-status",
        ("tests/test_phase_3v_live_readiness.py",),
        "golden_trace.live_readiness",
        "E6_HUMAN_APPROVAL",
        True,
        "LIVE_AUTHORIZATION_GATE",
    ),
)


def phase_registry_entries() -> tuple[PhaseRegistryEntry, ...]:
    return PHASE_REGISTRY


def validate_phase_registry(
    entries: tuple[PhaseRegistryEntry, ...] = PHASE_REGISTRY,
) -> list[str]:
    errors: list[str] = []
    ids = [entry.phase_id for entry in entries]
    duplicate_ids = sorted({phase_id for phase_id in ids if ids.count(phase_id) > 1})
    if duplicate_ids:
        errors.append(f"duplicate phase ids: {', '.join(duplicate_ids)}")
    missing = sorted(set(MANDATORY_PHASE_IDS) - set(ids), key=_phase_sort_key)
    extra = sorted(set(ids) - set(MANDATORY_PHASE_IDS), key=_phase_sort_key)
    if missing:
        errors.append(f"missing mandatory phases: {', '.join(missing)}")
    if extra:
        errors.append(f"unexpected phase ids: {', '.join(extra)}")
    for row in entries:
        if row.implementation_state not in PHASE_STATES:
            errors.append(f"{row.phase_id}: invalid implementation_state {row.implementation_state}")
        for field_name in (
            "name",
            "capability",
            "implementation_modules",
            "config_sources",
            "schema_or_table_refs",
            "cli_or_service_entrypoints",
            "producer_contracts",
            "consumer_contracts",
            "health_probe",
            "test_selectors",
            "replay_selector",
            "minimum_evidence_grade",
            "safety_classification",
            "owner_or_todo",
        ):
            value = getattr(row, field_name)
            if value in ("", (), []):
                errors.append(f"{row.phase_id}: missing required registry field {field_name}")
    return errors


def phase_registry_payload(*, root: Path | None = None) -> dict[str, Any]:
    return {
        "registry_version": PHASE_REGISTRY_VERSION,
        "phase_count": len(PHASE_REGISTRY),
        "required_phase_count": len(MANDATORY_PHASE_IDS),
        "validation_errors": validate_phase_registry(),
        "phases": [entry.to_dict(root=root) for entry in PHASE_REGISTRY],
    }


def implementation_evidence(
    entry: PhaseRegistryEntry,
    *,
    root: Path | None = None,
) -> dict[str, Any]:
    module_rows = []
    for module_name in entry.implementation_modules:
        available = module_available(module_name)
        location = _module_to_location(module_name)
        path_exists = (root / location).exists() if root is not None else None
        module_rows.append(
            {
                "module": module_name,
                "available": available,
                "path": location,
                "path_exists": path_exists,
            }
        )
    mapped = any(row["available"] or row["path_exists"] for row in module_rows)
    observed_state = entry.implementation_state if mapped else MAPPING_ERROR
    return {
        "mapped": mapped,
        "declared_state": entry.implementation_state,
        "observed_state": observed_state,
        "modules": module_rows,
    }


def module_available(module_name: str) -> bool:
    try:
        return importlib.util.find_spec(module_name) is not None
    except (ImportError, ModuleNotFoundError, ValueError):
        return False


def legacy_phases() -> tuple[dict[str, Any], ...]:
    return tuple(entry.to_legacy_phase() for entry in PHASE_REGISTRY)


def _module_to_location(module_name: str) -> str:
    return "src/" + module_name.replace(".", "/")


def _phase_sort_key(phase_id: str) -> tuple[int, float | str]:
    if phase_id[0].isdigit():
        try:
            return (0, float(phase_id))
        except ValueError:
            return (0, phase_id)
    return (1, phase_id)
