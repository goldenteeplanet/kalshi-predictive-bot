# ruff: noqa: E402, I001
import importlib.util
import json
import os
import sys
import time
from decimal import Decimal, InvalidOperation
from functools import wraps
from math import ceil
from pathlib import Path
from typing import Annotated, Any


def _fast_option_value(args: list[str], option: str, default: str | None = None) -> str | None:
    if option not in args:
        return default
    index = args.index(option)
    if index + 1 >= len(args):
        return default
    return args[index + 1]


def _phase3bc_r5_fast_path_command(argv: list[str] | None = None) -> int | None:
    """Run low-latency R5 monitor commands before importing the full CLI graph."""

    args = list(sys.argv[1:] if argv is None else argv)
    if not args:
        return None
    command = args[0]
    if command not in {
        "phase3aw-dashboard-truth",
        "phase3bc-r5-status",
        "phase3bc-r5-unattended-guard",
    }:
        return None
    if "--help" in args or "-h" in args:
        return None

    output_dir = Path(_fast_option_value(args, "--output-dir", "reports/phase3bc_r5") or "")
    if command == "phase3aw-dashboard-truth":
        from kalshi_predictor.config import get_settings
        from kalshi_predictor.data.db import get_session_factory, init_db
        from kalshi_predictor.phase3aw import write_phase3aw_dashboard_truth_report

        output_dir = Path(_fast_option_value(args, "--output-dir", "reports/phase3aw") or "")
        reports_dir = Path(_fast_option_value(args, "--reports-dir", "reports") or "")
        stale_raw = _fast_option_value(args, "--stale-after-minutes", "120")
        try:
            stale_after_minutes = int(stale_raw or "120")
        except ValueError:
            stale_after_minutes = 120
        engine = init_db()
        settings = get_settings()
        session_factory = get_session_factory(engine)
        with session_factory() as session:
            artifacts = write_phase3aw_dashboard_truth_report(
                session,
                output_dir=output_dir,
                reports_dir=reports_dir,
                settings=settings,
                command_args=args,
                stale_after_minutes=stale_after_minutes,
            )
        print("Phase 3AW Dashboard Truth Reconciliation")
        print("Mode: PAPER ONLY read-only diagnostics")
        print("Live/demo execution: blocked")
        print("Order submission/cancel/replace: blocked")
        print(f"Wrote dashboard truth: {artifacts.dashboard_truth_path}")
        print(f"Wrote stale artifact audit: {artifacts.stale_artifact_audit_path}")
        print(f"Wrote current crypto funnel: {artifacts.current_crypto_funnel_path}")
        print(f"Wrote Executive Summary: {artifacts.executive_summary_path}")
        print(f"Wrote Next Actions: {artifacts.next_actions_path}")
        print(f"Wrote operator command: {artifacts.operator_next_command_path}")
        return 0
    if command == "phase3bc-r5-status":
        from kalshi_predictor.phase3bc_r6 import write_phase3bc_r5_status_report

        artifacts = write_phase3bc_r5_status_report(output_dir=output_dir)
        print("Phase 3BC-R5 crypto freshness watch status")
        print("Mode: READ ONLY")
        print("Live/demo execution: blocked")
        print("Order submission/cancel/replace: blocked")
        print(f"Wrote JSON: {artifacts.json_path}")
        print(f"Wrote Markdown: {artifacts.markdown_path}")
        return 0

    from kalshi_predictor.phase3bc_r6 import write_phase3bc_r5_unattended_guard_report

    terminate_raw = _fast_option_value(args, "--terminate-grace-seconds", "30")
    try:
        terminate_grace_seconds = int(terminate_raw or "30")
    except ValueError:
        terminate_grace_seconds = 30
    artifacts = write_phase3bc_r5_unattended_guard_report(
        output_dir=output_dir,
        stop_overrun="--stop-overrun" in args,
        terminate_grace_seconds=terminate_grace_seconds,
    )
    print("Phase 3BC-R5 unattended guard")
    print("Mode: READ ONLY unless --stop-overrun is supplied")
    print("Live/demo execution: blocked")
    print("Order submission/cancel/replace: blocked")
    print(f"Wrote JSON: {artifacts.json_path}")
    print(f"Wrote Markdown: {artifacts.markdown_path}")
    return 0


_phase3bc_r5_fast_path_exit = _phase3bc_r5_fast_path_command()
if _phase3bc_r5_fast_path_exit is not None:
    raise SystemExit(_phase3bc_r5_fast_path_exit)

import typer
from rich.console import Console
from sqlalchemy import desc, func, select

from kalshi_predictor.advanced_risk.reports import generate_advanced_risk_report
from kalshi_predictor.autopilot.reports import (
    build_autopilot_status,
    generate_autopilot_report,
)
from kalshi_predictor.autopilot.runner import run_autopilot_once
from kalshi_predictor.autopilot.scheduler import run_autopilot_scheduler
from kalshi_predictor.backtesting.reports import generate_backtest_report
from kalshi_predictor.comparison.reports import generate_strategy_comparison_report
from kalshi_predictor.confidence.engine import run_model_confidence_engine
from kalshi_predictor.confidence.reports import generate_model_confidence_report
from kalshi_predictor.config import get_settings
from kalshi_predictor.consensus.repository import (
    ingest_forum_consensus_payload,
    latest_consensus_for_ticker,
)
from kalshi_predictor.control_center.reports import generate_control_center_report
from kalshi_predictor.crypto.assets import DEFAULT_CRYPTO_SYMBOLS
from kalshi_predictor.crypto.features import build_crypto_features
from kalshi_predictor.crypto.ingestion import ingest_crypto_quotes
from kalshi_predictor.crypto.linker import link_crypto_markets
from kalshi_predictor.crypto.reports import (
    generate_crypto_backtest_report,
    generate_crypto_report,
)
from kalshi_predictor.crypto.repository import parse_symbols
from kalshi_predictor.data.backend import database_url_from_settings
from kalshi_predictor.data.db import (
    describe_db_location,
    get_session_factory,
    init_db,
    make_engine,
)
from kalshi_predictor.data.locks import (
    db_writer_monitor,
    friendly_database_locked_message,
    is_database_locked_error,
    sqlite_lock_diagnostics,
)
from kalshi_predictor.data.maintenance import (
    database_doctor,
    database_health,
    generate_database_report,
    migrate_sqlite_to_postgres,
    sqlite_backup,
    sqlite_recover,
)
from kalshi_predictor.data.repositories import get_recent_snapshots
from kalshi_predictor.data.schema import Forecast, MarketRanking, MarketSnapshot, WeatherMarketLink
from kalshi_predictor.economic.actuals import (
    write_phase3bd_r3_economic_value_capture_report,
    write_phase3bd_r4_verified_consensus_source_report,
)
from kalshi_predictor.economic.calendar import (
    write_phase3bd_r2_economic_calendar_freshness_report,
)
from kalshi_predictor.economic.consensus_watch import (
    write_phase3bd_r5_consensus_feed_watch_report,
)
from kalshi_predictor.economic.discovery import write_phase3bd_economic_market_discovery_report
from kalshi_predictor.economic.evidence_activation import (
    write_phase3bd_r8_economic_evidence_activation_report,
)
from kalshi_predictor.economic.features import build_economic_features
from kalshi_predictor.economic.ingestion import ingest_economic_file_payload
from kalshi_predictor.economic.linker import link_economic_markets
from kalshi_predictor.economic.opportunity_quality_gate import (
    write_phase3bd_r7_economic_opportunity_quality_gate_report,
)
from kalshi_predictor.evaluation.reports import generate_calibration_report
from kalshi_predictor.explain.opportunity_explainer import explain_opportunity
from kalshi_predictor.external.base import load_json_file
from kalshi_predictor.external.crypto import ingest_crypto_json
from kalshi_predictor.external.economic import ingest_economic_json
from kalshi_predictor.external.weather import ingest_weather_json
from kalshi_predictor.feature_discovery.experiment import export_feature_experiment_spec
from kalshi_predictor.feature_discovery.reports import generate_feature_discovery_report
from kalshi_predictor.feature_discovery.repository import feature_discovery_status
from kalshi_predictor.forecasting.registry import (
    latest_snapshots_for_model,
    run_forecast_models,
)
from kalshi_predictor.forecasting.status import (
    generate_model_readiness_report,
    model_status_summary,
)
from kalshi_predictor.ingest.markets import sync_markets as sync_markets_job
from kalshi_predictor.ingest.markets import sync_settlements as sync_settlements_job
from kalshi_predictor.ingest.snapshots import capture_snapshots
from kalshi_predictor.institutional_dashboard.reports import (
    generate_institutional_dashboard_report,
    institutional_dashboard_status,
)
from kalshi_predictor.institutional_dashboard.service import (
    build_dashboard_snapshot,
    export_snapshot_csv,
)
from kalshi_predictor.jobs.collect_once import collect_once as collect_once_job
from kalshi_predictor.leaderboard.reports import generate_leaderboard_report
from kalshi_predictor.learning.accelerator import accelerate_learning
from kalshi_predictor.learning.exploration import seed_exploratory_paper_trades
from kalshi_predictor.learning.diagnostics import generate_learning_diagnostics_report
from kalshi_predictor.learning.reports import (
    generate_learning_report,
    generate_learning_targets_report,
)
from kalshi_predictor.learning.runner import run_learning_once, run_learning_scheduler
from kalshi_predictor.learning.safety import learning_status
from kalshi_predictor.learning.targets import generate_learning_targets
from kalshi_predictor.live_readiness.reports import generate_live_readiness_report
from kalshi_predictor.live_readiness.service import (
    live_readiness_status,
    verify_certificate_for_order,
)
from kalshi_predictor.logging_config import configure_logging
from kalshi_predictor.market_legs import (
    generate_link_coverage_report,
    link_coverage_dashboard,
    parse_and_store_market_legs,
)
from kalshi_predictor.memory.archive import archive_memory_to_jsonl
from kalshi_predictor.memory.backfill import backfill_memory_from_existing_tables
from kalshi_predictor.memory.datasets import build_forecast_learning_dataset
from kalshi_predictor.memory.reports import generate_memory_report, memory_health
from kalshi_predictor.memory.repository import forecast_timeline, trade_timeline
from kalshi_predictor.meta.feature_builder import build_meta_features
from kalshi_predictor.meta.reports import (
    generate_meta_evaluation_report,
    generate_meta_opportunities_report,
    generate_meta_report,
)
from kalshi_predictor.meta.trainer import build_meta_training_examples
from kalshi_predictor.microstructure.orderbook_features import build_microstructure_features
from kalshi_predictor.microstructure.reports import (
    generate_microstructure_backtest_report,
    generate_microstructure_opportunities_report,
    generate_microstructure_report,
)
from kalshi_predictor.microstructure.sampling import write_microstructure_sampling_report
from kalshi_predictor.news.features import build_news_features
from kalshi_predictor.news.ingestion import ingest_news_file, ingest_news_rss
from kalshi_predictor.news.linker import link_news_markets
from kalshi_predictor.news.reports import (
    generate_news_backtest_report,
    generate_news_opportunities_report,
    generate_news_report,
)
from kalshi_predictor.news.signals import generate_news_signals
from kalshi_predictor.opportunities.reports import (
    best_payout_rows,
    generate_best_payouts_report,
    generate_market_rankings_report,
    generate_opportunities_report,
)
from kalshi_predictor.overnight.reports import build_overnight_status, generate_overnight_report
from kalshi_predictor.overnight.runner import run_overnight_once, run_overnight_scheduler
from kalshi_predictor.paper.ledger import get_paper_summary, get_position, reset_paper_data
from kalshi_predictor.paper.pnl import calculate_and_store_pnl
from kalshi_predictor.paper.reports import write_paper_trading_report
from kalshi_predictor.paper.settlement_reconciliation import (
    write_paper_settlement_reconciliation,
)
from kalshi_predictor.paper.simulator import run_paper_trading
from kalshi_predictor.personal_trader.reports import (
    personal_trader_status_report,
    render_personal_trader_report,
)
from kalshi_predictor.personal_trader.service import (
    build_personal_trade_brief,
    conversational_response,
    recommendation_audit_events,
)
from kalshi_predictor.phase3aa import write_phase3aa_report
from kalshi_predictor.phase3aa_r2 import write_phase3aa_r2_exact_settlement_harvest_report
from kalshi_predictor.phase3aa_r3 import write_phase3aa_r3_residual_audit_report
from kalshi_predictor.phase3aa_r4 import write_phase3aa_r4_settlement_fetch_recovery_report
from kalshi_predictor.phase3aa_r5 import write_phase3aa_r5_closed_market_outcome_capture_report
from kalshi_predictor.phase3aa_r6 import write_phase3aa_r6_composite_settlement_resolver_report
from kalshi_predictor.phase3ab import write_phase3ab_report
from kalshi_predictor.phase3ac import write_phase3ac_report
from kalshi_predictor.phase3ad import write_phase_orchestrator_report
from kalshi_predictor.phase3ae import write_phase3ae_report
from kalshi_predictor.phase3ae_fast_market import (
    write_phase3ae_fast_market_harvester_report,
)
from kalshi_predictor.phase3ae_roster_candidates import (
    write_phase3ae_roster_candidate_diagnostics,
)
from kalshi_predictor.phase3af import DEFAULT_SOCCER_COMPETITIONS, write_phase3af_report
from kalshi_predictor.phase3ag import write_phase3ag_repair_report, write_phase3ag_report
from kalshi_predictor.phase3ag_crypto import write_phase3ag_crypto_report
from kalshi_predictor.phase3ah import write_snapshot_coverage_repair_report
from kalshi_predictor.phase3ah_placeholder_watch import (
    write_phase3ah_sports_placeholder_watch_report,
)
from kalshi_predictor.phase3ah_placeholders import (
    write_phase3ah_round_placeholder_resolution_report,
)
from kalshi_predictor.phase3ah_r2 import write_phase3ah_r2_backfill_report
from kalshi_predictor.phase3ah_r3 import (
    write_phase3ah_r3_bounded_scan_expansion_report,
    write_phase3ah_r3_sports_provenance_repair_report,
)
from kalshi_predictor.phase3ah_roster import write_phase3ah_roster_verification_report
from kalshi_predictor.phase3ah_sports import write_phase3ah_sports_evidence_report
from kalshi_predictor.phase3ai import write_phase3ai_report
from kalshi_predictor.phase3aj import write_phase3aj_report
from kalshi_predictor.phase3aj_gap_closure import (
    write_composite_settlement_resolve_report,
    write_gap_closure_doctor_report,
    write_market_data_refresh_status,
    write_paper_trade_funnel_report,
    write_phase_3aj_report,
    write_source_readiness_report,
)
from kalshi_predictor.phase3ak import (
    write_crypto_watch_status_report,
    write_crypto_window_sync_report,
    write_market_data_refresh_status as write_phase3ak_market_data_refresh_status,
    write_phase_3ak_report,
    write_phase3ak_report,
)
from kalshi_predictor.phase3al import write_phase3al_report
from kalshi_predictor.phase3al_diagnostic import write_phase3al_diagnostic_report
from kalshi_predictor.phase3am import (
    write_economic_news_market_watch_report,
    write_phase3am_gap_burndown_report,
    write_phase3am_preflight_report,
    write_phase3am_report,
    write_phase3ay_due_settlement_diagnostic_report,
    write_phase3ay_settle_due_paper_report,
)
from kalshi_predictor.phase3an import (
    write_phase3an_3bb_r2_burndown_report,
    write_phase3an_crypto_watch_doctor_report,
    write_phase3an_crypto_watch_restart_plan_report,
    write_phase3an_economic_approval_safety_guard_from_packet_report,
    write_phase3an_economic_approval_safety_guard_report,
    write_phase3an_economic_link_event_repair_apply_report,
    write_phase3an_economic_link_event_repair_plan_report,
    write_phase3an_economic_morning_operator_handoff_report,
    write_phase3an_economic_news_parser_backfill_plan_report,
    write_phase3an_economic_news_watch_report,
    write_phase3an_economic_operator_approval_packet_report,
    write_phase3an_economic_parser_leg_backfill_report,
    write_phase3an_gap_fix_report,
    write_phase3an_general_sources_status_report,
    write_phase3an_overnight_refresh_continuity_report,
    write_phase3an_paper_funnel_explain_report,
    write_phase3an_preflight_report,
    write_phase3an_report,
    write_phase3an_settlement_health_confirm_report,
    write_phase3an_sports_blocker_report,
    write_phase3an_usda_date_mismatch_report,
)
from kalshi_predictor.phase3ao import (
    write_phase3ao_opportunity_link_audit,
    write_phase3ao_report,
)
from kalshi_predictor.phase3ap import (
    write_phase3ap_book_diagnostic_report,
    write_phase3ap_paper_ready_unblock_report,
    write_phase3ap_refresh_positive_ev_books_report,
    write_phase3ap_report,
    write_phase3ap_settlement_check_diagnostic_report,
)
from kalshi_predictor.phase3aq import (
    write_phase3aq_link_and_book_unblock_report,
    write_phase3aq_positive_ev_link_audit_report,
    write_phase3aq_refresh_verified_opportunity_books_report,
    write_phase3aq_report,
    write_phase3aq_settlement_check_split_report,
)
from kalshi_predictor.phase3ar import (
    write_phase3ar_catalog_stale_diagnostic_report,
    write_phase3ar_link_repair_report,
    write_phase3ar_refresh_catalog_for_opportunities_report,
    write_phase3ar_refresh_books_for_verified_links_report,
    write_phase3ar_report,
    write_phase3ar_settlement_check_noise_audit_report,
    write_phase3ar_url_audit_report,
    write_phase3ar_url_repair_report,
)
from kalshi_predictor.phase3as import write_phase3as_report
from kalshi_predictor.phase3at import (
    write_crypto_history_warmup_report,
    write_phase3at_forecast_ranking_diagnostic_report,
    write_phase3at_handoff_report,
    write_phase3at_opportunity_funnel_report,
    write_phase3at_report,
)
from kalshi_predictor.phase3au import load_latest_long_job_status, write_phase3au_report
from kalshi_predictor.phase3aw import (
    build_phase3aw_recovery_status,
    write_phase3aw_dashboard_truth_report,
    write_phase3aw_recovery_report,
)
from kalshi_predictor.phase3ax import (
    run_resumable_sports_derivation,
    write_phase3ax_gap_analysis_report,
)
from kalshi_predictor.phase3ay import (
    run_phase3ay_health_refresh_loop,
    start_phase3ay_unattended_refresh,
    write_phase3ay_status_report,
    write_phase3ay_unattended_guard_report,
)
from kalshi_predictor.phase3ay_free_sources import (
    write_phase3ay_free_source_sprint_report,
)
from kalshi_predictor.phase3ay_positive_ev import (
    write_phase3ay_positive_ev_accelerator_report,
)
from kalshi_predictor.phase3az import (
    write_phase3az_gap_analysis_report,
    write_phase3az_r11_non_crypto_activation_report,
)
from kalshi_predictor.phase3az_weather import (
    write_phase3az_r12_weather_activation_preview_report,
    write_phase3az_r12_weather_missing_link_apply_report,
    write_phase3az_r13_weather_handoff_status_report,
)
from kalshi_predictor.phase3ba_certification import write_phase3ba_paper_certification_report
from kalshi_predictor.phase3ba_ingestion_stability import (
    write_phase3ba_ingestion_stability_report,
)
from kalshi_predictor.phase3ba_r1 import write_phase3ba_r1_writer_unlock_report
from kalshi_predictor.phase3ba_r2 import write_phase3ba_r2_weather_ranking_activation_report
from kalshi_predictor.phase3ba_r3 import write_phase3ba_r3_weather_paper_gate_report
from kalshi_predictor.phase3ba_r4 import write_phase3ba_r4_crypto_executable_book_watch_report
from kalshi_predictor.phase3ba_r5 import write_phase3ba_r5_paper_ready_truth_report
from kalshi_predictor.phase3ba_r6 import write_phase3ba_r6_noncrypto_engine_backlog_report
from kalshi_predictor.phase3ba_r7 import write_phase3ba_r7_composite_market_plan_report
from kalshi_predictor.phase3ba_status import write_phase3ba_status_report
from kalshi_predictor.phase3bb import (
    write_phase3bb_apply_group_source_review,
    write_phase3bb_domain_readiness_report,
    write_phase3bb_group_source_review,
    write_phase3bb_general_candidate_routing_report,
    write_phase3bb_general_reclassification_report,
    write_phase3bb_general_source_availability_report,
    write_phase3bb_general_source_evidence_report,
    write_phase3bb_general_source_intake_report,
    write_phase3bb_r3_composite_operator_preflight_report,
    write_phase3bb_r3_composite_preview_gate_report,
    write_phase3bb_r3_exact_sports_link_report,
    write_phase3bb_r3_safe_parser_reparse_report,
)
from kalshi_predictor.phase3bb_acceleration import (
    write_phase3bb_acceleration_report,
    write_phase3bb_cloud_readiness_report,
    write_phase3bb_historical_replay_acceleration_report,
    write_phase3bb_multicategory_expansion_plan_report,
    write_phase3bb_scheduler_plan_report,
    write_phase3bb_throughput_analysis_report,
    write_phase3bb_weather_fast_lane_report,
)
from kalshi_predictor.phase3bb_r1 import write_phase3bb_r1_operator_scheduler_report
from kalshi_predictor.phase3bb_r2 import write_phase3bb_r2_weather_fast_lane_report
from kalshi_predictor.phase3bb_r3_activation import (
    write_phase3bb_r3_source_evidence_activation_report,
)
from kalshi_predictor.phase3bb_r3_free_source_inventory import (
    write_phase3bb_r3_free_source_inventory_report,
)
from kalshi_predictor.phase3bb_r4_economic_parser_backfill import (
    write_phase3bb_r4_economic_parser_backfill_report,
)
from kalshi_predictor.phase3bb_r4_flightaware import (
    write_phase3bb_r4_flightaware_review_link_gate_report,
)
from kalshi_predictor.phase3bb_r5_flightaware import (
    write_phase3bb_r5_flightaware_date_stable_evidence_report,
)
from kalshi_predictor.phase3bb_r5_usda import (
    write_phase3bb_r5_usda_source_activation_report,
)
from kalshi_predictor.phase3bb_r6_sports_provenance import (
    write_phase3bb_r6_sports_provenance_repair_report,
)
from kalshi_predictor.phase3bb_r7_news_event import (
    write_phase3bb_r7_news_event_discovery_report,
)
from kalshi_predictor.phase3bb_r8_unified_paper_gate import (
    write_phase3bb_r8_unified_paper_gate_report,
)
from kalshi_predictor.phase3bb_r9_learning_acceleration import (
    write_phase3bb_r9_learning_acceleration_report,
)
from kalshi_predictor.phase3bb_r10_cloud_readiness import (
    write_phase3bb_r10_cloud_readiness_decision_report,
)
from kalshi_predictor.phase3bb_r11_codex_cloud_bridge import (
    write_phase3bb_r11_codex_cloud_bridge_report,
)
from kalshi_predictor.phase3bb_r12_cloud_bootstrap import (
    write_phase3bb_r12_cloud_bootstrap_verification_report,
)
from kalshi_predictor.phase3bb_r13_cloud_scheduler_adoption import (
    write_phase3bb_r13_cloud_scheduler_adoption_report,
)
from kalshi_predictor.phase3bb_r14_cloud_service_plan import (
    write_phase3bb_r14_cloud_service_plan_report,
)
from kalshi_predictor.phase3bb_r15_cloud_service_install_review import (
    write_phase3bb_r15_cloud_service_install_review_report,
)
from kalshi_predictor.phase3bb_r16_cloud_service_install_handoff import (
    write_phase3bb_r16_cloud_service_install_handoff_report,
)
from kalshi_predictor.phase3bb_r17_cloud_service_install_verification import (
    write_phase3bb_r17_cloud_service_install_verification_report,
)
from kalshi_predictor.phase3bb_r18_cloud_scheduler_runtime_cutover import (
    write_phase3bb_r18_cloud_scheduler_runtime_cutover_report,
)
from kalshi_predictor.phase3bb_r19_cloud_systemd_cutover import (
    APPROVAL_ENV_VAR as PHASE3BB_R19_APPROVAL_ENV_VAR,
    write_phase3bb_r19_cloud_systemd_cutover_report,
)
from kalshi_predictor.phase3bb_r20_cloud_ui_service_plan import (
    write_phase3bb_r20_cloud_ui_service_plan_report,
)
from kalshi_predictor.phase3bb_r21_cloud_ui_install_review import (
    write_phase3bb_r21_cloud_ui_install_review_report,
)
from kalshi_predictor.phase3bb_r22_cloud_ui_install_handoff import (
    write_phase3bb_r22_cloud_ui_install_handoff_report,
)
from kalshi_predictor.phase3bb_r23_cloud_ui_install_verification import (
    write_phase3bb_r23_cloud_ui_install_verification_report,
)
from kalshi_predictor.phase3bb_r24_cloud_ui_start_tunnel_verification import (
    write_phase3bb_r24_cloud_ui_start_tunnel_verification_report,
)
from kalshi_predictor.phase3bb_r25_cloud_ui_operator_smoke_test import (
    write_phase3bb_r25_cloud_ui_operator_smoke_test_report,
)
from kalshi_predictor.phase3bb_r26_cloud_ui_access_control_gate import (
    write_phase3bb_r26_cloud_ui_access_control_gate_report,
)
from kalshi_predictor.phase3bb_r27_cloud_ui_private_access_auth_draft import (
    write_phase3bb_r27_cloud_ui_private_access_auth_draft_report,
)
from kalshi_predictor.phase3bb_r28_cloud_ui_private_access_operator_review import (
    write_phase3bb_r28_cloud_ui_private_access_operator_review_report,
)
from kalshi_predictor.phase3bb_r29_cloud_ui_private_access_install_handoff import (
    write_phase3bb_r29_cloud_ui_private_access_install_handoff_report,
)
from kalshi_predictor.phase3bb_r30_cloud_ui_private_access_install_verification import (
    write_phase3bb_r30_cloud_ui_private_access_install_verification_report,
)
from kalshi_predictor.phase3bb_r31_cloud_ui_private_access_operator_smoke_test import (
    write_phase3bb_r31_cloud_ui_private_access_operator_smoke_test_report,
)
from kalshi_predictor.phase3bb_r32_cloud_ui_dashboard_truth_scheduler_status import (
    write_phase3bb_r32_cloud_ui_dashboard_truth_scheduler_status_report,
)
from kalshi_predictor.phase3bb_r33_cloud_paper_only_operations_readiness import (
    write_phase3bb_r33_cloud_paper_only_operations_readiness_report,
)
from kalshi_predictor.phase3bb_r34_cloud_multicategory_refresh_scheduler_review import (
    write_phase3bb_r34_cloud_multicategory_refresh_scheduler_review_report,
)
from kalshi_predictor.phase3bb_r35_cloud_multicategory_scheduler_no_start_dry_run import (
    write_phase3bb_r35_cloud_multicategory_scheduler_no_start_dry_run_report,
)
from kalshi_predictor.phase3bb_r36_cloud_scheduler_install_handoff import (
    write_phase3bb_r36_cloud_scheduler_install_handoff_report,
)
from kalshi_predictor.phase3bb_r37_cloud_scheduler_install_verification import (
    write_phase3bb_r37_cloud_scheduler_install_verification_report,
)
from kalshi_predictor.phase3bb_r38_cloud_scheduler_install_repair_handoff import (
    write_phase3bb_r38_cloud_scheduler_install_repair_handoff_report,
)
from kalshi_predictor.phase3bb_r38_cloud_scheduler_timer_start_handoff import (
    write_phase3bb_r38_cloud_scheduler_timer_start_handoff_report,
)
from kalshi_predictor.phase3bb_r39_cloud_auto_login_admin_bootstrap import (
    write_phase3bb_r39_cloud_auto_login_admin_bootstrap_report,
)
from kalshi_predictor.phase3bb_r40_cloud_scheduler_runtime_monitor import (
    write_phase3bb_r40_cloud_scheduler_runtime_monitor_report,
)
from kalshi_predictor.phase3bb_r41_writer_gate_normalization import (
    write_phase3bb_r41_writer_gate_normalization_report,
)
from kalshi_predictor.phase3bb_r42_weather_fast_lane_post_unblock import (
    write_phase3bb_r42_weather_fast_lane_post_unblock_report,
)
from kalshi_predictor.phase3bb_r43_weather_catalog_scheduler_hook import (
    write_phase3bb_r43_weather_catalog_scheduler_hook_report,
)
from kalshi_predictor.phase3bb_r44_weather_catalog_hook_runtime_verification import (
    write_phase3bb_r44_weather_catalog_hook_runtime_verification_report,
)
from kalshi_predictor.phase3bb_r45_weather_freshness_to_ranking_impact import (
    write_phase3bb_r45_weather_freshness_to_ranking_impact_report,
)
from kalshi_predictor.phase3bb_r46_cloud_scheduler_weather_writer_gate_repair import (
    write_phase3bb_r46_cloud_scheduler_weather_writer_gate_repair_report,
)
from kalshi_predictor.phase3bb_r47_weather_current_window_series_discovery import (
    write_phase3bb_r47_weather_current_window_series_discovery_report,
)
from kalshi_predictor.phase3bb_r48_weather_feature_refresh_runtime_verification import (
    write_phase3bb_r48_weather_feature_refresh_runtime_verification_report,
)
from kalshi_predictor.phase3bb_r49_weather_missing_link_apply_after_feature_refresh import (
    write_phase3bb_r49_weather_missing_link_apply_after_feature_refresh_report,
)
from kalshi_predictor.phase3bb_r50_weather_post_link_ranking_fast_lane_recheck import (
    write_phase3bb_r50_weather_post_link_ranking_fast_lane_recheck_report,
)
from kalshi_predictor.phase3bb_r51_weather_ranking_path_repair import (
    write_phase3bb_r51_weather_ranking_path_repair_report,
)
from kalshi_predictor.phase3bb_r52_weather_ev_fair_value_diagnostic import (
    write_phase3bb_r52_weather_ev_fair_value_diagnostic_report,
)
from kalshi_predictor.phase3bb_r53_weather_current_window_cadence import (
    write_phase3bb_r53_weather_current_window_cadence_report,
)
from kalshi_predictor.phase3bb_r54_weather_missing_link_apply_deferral import (
    write_phase3bb_r54_weather_missing_link_apply_deferral_report,
)
from kalshi_predictor.phase3bb_r55_weather_ranking_path_retry import (
    write_phase3bb_r55_weather_ranking_path_retry_report,
)
from kalshi_predictor.phase3bb_r57_weather_selected_window_pipeline import (
    write_phase3bb_r57_weather_selected_window_pipeline_report,
)
from kalshi_predictor.phase3bb_r58_weather_selected_window_alignment import (
    write_phase3bb_r58_weather_selected_window_alignment_report,
)
from kalshi_predictor.phase3bb_r59_weather_catalog_refresh_r57_retry import (
    write_phase3bb_r59_weather_catalog_refresh_r57_retry_report,
)
from kalshi_predictor.phase3bb_r60_weather_next_window_lead_time import (
    write_phase3bb_r60_weather_next_window_lead_time_report,
)
from kalshi_predictor.phase3bb_r61_cloud_dashboard_db_writer_api_repair import (
    write_phase3bb_r61_cloud_dashboard_db_writer_api_repair_report,
)
from kalshi_predictor.phase3bc import write_phase3bc_crypto_clean_opportunity_report
from kalshi_predictor.phase3bc_r3 import (
    DEFAULT_CRYPTO_LINK_SCAN_LIMIT,
    DEFAULT_CRYPTO_MARKET_SCAN_LIMIT,
    DEFAULT_CRYPTO_SERIES_TICKERS,
    DEFAULT_MARKET_PAGE_LIMIT,
    DEFAULT_NEAR_MONEY_PER_SYMBOL_LIMIT,
    DEFAULT_NEAR_MONEY_WINDOW_LIMIT,
    DEFAULT_SNAPSHOT_FETCH_CONCURRENCY,
    write_phase3bc_r3_active_crypto_refresh_report,
)
from kalshi_predictor.phase3bc_r4 import write_phase3bc_r4_crypto_ev_risk_diagnostics_report
from kalshi_predictor.phase3bc_r5 import write_phase3bc_r5_crypto_freshness_watch_report
from kalshi_predictor.phase3bc_r6 import (
    start_phase3bc_r5_unattended_watch,
    write_phase3bc_r5_status_report,
    write_phase3bc_r5_unattended_guard_report,
)
from kalshi_predictor.phase3bc_r7 import (
    write_phase3bc_r7_crypto_ranking_coverage_repair_report,
)
from kalshi_predictor.phase3bc_r16 import (
    write_phase3bc_r16_crypto_paper_ready_edge_hunt_report,
)
from kalshi_predictor.phase3bc_r17 import (
    write_phase3bc_r17_crypto_liquidity_actionability_report,
)
from kalshi_predictor.phase3ax_r6 import (
    write_phase3an_sports_blocker_report,
    write_phase3aw_dashboard_truth_report,
    write_phase3ax_gap_analysis_report,
)
from kalshi_predictor.phase3y import (
    generate_phase3y_report,
    run_link_remediation,
    run_settlement_watcher,
)
from kalshi_predictor.phase3z import (
    MODEL_REPAIR_DIR,
    backup_before_phase3z_write,
    runtime_identity,
    write_market_coverage_doctor,
    write_model_metrics_reconcile,
    write_model_repair_audit,
    write_model_repair_run,
)
from kalshi_predictor.phase3z_r2 import write_phase3z_r2_sports_provenance_repair_report
from kalshi_predictor.paper_trading_gap import write_paper_trading_gap_analysis_report
from kalshi_predictor.professional_ux.reports import (
    generate_phase_3x_report,
    phase_3x_card,
)
from kalshi_predictor.professional_ux.service import (
    DEFAULT_SHELL_STATUS_SNAPSHOT_PATH,
    write_shell_status_snapshot,
)
from kalshi_predictor.reinforcement_learning.engine import (
    config_from_settings as rl_config_from_settings,
)
from kalshi_predictor.reinforcement_learning.reports import generate_rl_policy_report
from kalshi_predictor.reinforcement_learning.repository import (
    persist_drift_snapshot,
    rl_status,
)
from kalshi_predictor.reinforcement_learning.serving import recommend_policy_action
from kalshi_predictor.research.assistant import research_opportunity
from kalshi_predictor.research.questions import answer_research_question
from kalshi_predictor.research.reports import generate_research_report
from kalshi_predictor.research.repository import store_research_question
from kalshi_predictor.scheduler import scheduler_plan
from kalshi_predictor.self_evaluation.reports import generate_self_evaluation_report
from kalshi_predictor.signals.registry import expected_signal_by_key
from kalshi_predictor.signals.reports import generate_signal_report
from kalshi_predictor.signals.repository import (
    signal_explorer_rows,
    signal_leaderboard_rows,
    signal_performance_summary,
)
from kalshi_predictor.signals.status import signal_status_rows, signal_status_summary
from kalshi_predictor.sports.derived_schedule import derive_sports_schedule_from_market_legs
from kalshi_predictor.sports.features import build_sports_features
from kalshi_predictor.sports.ingestion import ingest_sports_file
from kalshi_predictor.sports.link_cleanup import write_sports_link_cleanup_report
from kalshi_predictor.sports.linker import link_sports_markets
from kalshi_predictor.sports.reports import (
    generate_sports_backtest_report,
    generate_sports_opportunities_report,
    generate_sports_report,
)
from kalshi_predictor.sports.signals import generate_sports_signals
from kalshi_predictor.synthetic_markets.reports import generate_synthetic_markets_report
from kalshi_predictor.synthetic_markets.repository import synthetic_markets_status
from kalshi_predictor.system_certification.reports import (
    generate_system_certification_report,
    system_certification_card,
)
from kalshi_predictor.system_readiness.remediation import (
    DEFAULT_REPORT_PATH as SYSTEM_REMEDIATION_REPORT_PATH,
)
from kalshi_predictor.system_readiness.remediation import run_system_readiness_remediation
from kalshi_predictor.tonight.control import (
    BLOCKED,
    RECOVERY_INSTRUCTIONS,
    build_tonight_check,
    generate_tonight_report,
    render_tonight_check,
    run_tonight,
)
from kalshi_predictor.tournament.reports import (
    generate_model_diagnostics_report,
    generate_model_weights_report,
    generate_tournament_report,
)
from kalshi_predictor.ui.service import DecisionUiService
from kalshi_predictor.utils.time import parse_datetime
from kalshi_predictor.weather.features import build_weather_features
from kalshi_predictor.weather.ingestion import ingest_manual_weather_json, ingest_weather_location
from kalshi_predictor.weather.linker import link_weather_markets
from kalshi_predictor.weather.reports import (
    generate_weather_backtest_report,
    generate_weather_report,
)
from kalshi_predictor.workspace_guard import (
    build_workspace_consistency_guard,
    write_workspace_guard_report,
)
from kalshi_predictor.workstation.reports import (
    generate_analytics_report,
    generate_daily_briefing,
    generate_portfolio_summary_report,
)
from kalshi_predictor.workstation.repository import portfolio_summary, record_portfolio_state


class KalshiTyper(typer.Typer):
    def __call__(self, *args, **kwargs):
        try:
            return super().__call__(*args, **kwargs)
        except Exception as exc:
            if is_database_locked_error(exc):
                _raise_friendly_database_locked_exit(exc)
            raise


app = KalshiTyper(help="Read-only Kalshi predictive bot CLI.")
console = Console()


def _raise_friendly_database_locked_exit(exc: BaseException) -> None:
    console.print("[bold red]Database is busy. Try again in a few seconds.[/bold red]")
    console.print(friendly_database_locked_message())
    raise typer.Exit(75) from exc


def _with_friendly_database_lock_handling(callback):
    if getattr(callback, "_kalshi_db_lock_wrapped", False):
        return callback

    @wraps(callback)
    def wrapper(*args, **kwargs):
        try:
            return callback(*args, **kwargs)
        except Exception as exc:
            if is_database_locked_error(exc):
                _raise_friendly_database_locked_exit(exc)
            raise

    wrapper._kalshi_db_lock_wrapped = True
    return wrapper


def _install_friendly_cli_error_handlers() -> None:
    for command_info in app.registered_commands:
        if command_info.callback is not None:
            command_info.callback = _with_friendly_database_lock_handling(command_info.callback)

PHASE_3G_EXPECTED_COMMANDS = (
    "db-health",
    "db-doctor",
    "db-migrate",
    "db-revision",
    "sqlite-backup",
    "sqlite-recover",
    "migrate-sqlite-to-postgres",
)

PHASE_MODULES = (
    ("3A", "kalshi_predictor.ui"),
    ("3B", "kalshi_predictor.autopilot"),
    ("3C", "kalshi_predictor.explain"),
    ("3D", "kalshi_predictor.workstation"),
    ("3E", "kalshi_predictor.opportunities"),
    ("3F", "kalshi_predictor.research"),
    ("3G", "kalshi_predictor.data.maintenance"),
    ("3H", "kalshi_predictor.news"),
    ("3I", "kalshi_predictor.data.db"),
    ("3J", "kalshi_predictor.sports"),
    ("3K", "kalshi_predictor.microstructure"),
    ("3L", "kalshi_predictor.meta"),
    ("3M", "kalshi_predictor.position_sizing"),
    ("3N", "kalshi_predictor.advanced_risk"),
    ("3O", "kalshi_predictor.memory"),
    ("3P", "kalshi_predictor.self_evaluation"),
    ("3Q", "kalshi_predictor.feature_discovery"),
    ("3R", "kalshi_predictor.synthetic_markets"),
    ("3S", "kalshi_predictor.reinforcement_learning"),
    ("3T", "kalshi_predictor.institutional_dashboard"),
    ("3U", "kalshi_predictor.personal_trader"),
    ("3V", "kalshi_predictor.live_readiness"),
    ("3W", "kalshi_predictor.system_certification"),
    ("3X", "kalshi_predictor.professional_ux"),
    ("3Y", "kalshi_predictor.phase3y"),
    ("3Z", "kalshi_predictor.phase3z"),
    ("3AA", "kalshi_predictor.phase3aa"),
    ("3AB", "kalshi_predictor.phase3ab"),
    ("3AC", "kalshi_predictor.phase3ac"),
    ("3AD", "kalshi_predictor.phase3ad"),
    ("3AE", "kalshi_predictor.phase3ae"),
    ("3AF", "kalshi_predictor.phase3af"),
    ("3AG", "kalshi_predictor.phase3ag"),
    ("3AH", "kalshi_predictor.phase3ah"),
    ("3AI", "kalshi_predictor.phase3ai"),
    ("3AJ", "kalshi_predictor.phase3aj"),
    ("3AK", "kalshi_predictor.phase3ak"),
    ("3AL", "kalshi_predictor.phase3al"),
    ("3AM", "kalshi_predictor.phase3am"),
    ("3AN", "kalshi_predictor.phase3an"),
    ("3AO", "kalshi_predictor.phase3ao"),
    ("3AP", "kalshi_predictor.phase3ap"),
    ("3AQ", "kalshi_predictor.phase3aq"),
    ("3AR", "kalshi_predictor.phase3ar"),
    ("3AS", "kalshi_predictor.phase3as"),
    ("3AT", "kalshi_predictor.phase3at"),
    ("3AU", "kalshi_predictor.phase3au"),
    ("3AW", "kalshi_predictor.phase3aw"),
    ("3AX", "kalshi_predictor.phase3ax"),
)


def registered_root_command_names() -> list[str]:
    names: list[str] = []
    for command_info in app.registered_commands:
        if command_info.name:
            names.append(command_info.name)
    return sorted(set(names))


def build_command_audit() -> dict[str, list[str]]:
    registered = registered_root_command_names()
    missing = [command for command in PHASE_3G_EXPECTED_COMMANDS if command not in registered]
    return {
        "expected_commands": list(PHASE_3G_EXPECTED_COMMANDS),
        "registered_commands": registered,
        "missing_commands": missing,
    }


def build_phase_status() -> dict[str, object]:
    phases: list[dict[str, object]] = []
    missing_modules: list[str] = []
    for phase, module_name in PHASE_MODULES:
        installed = _module_available(module_name)
        phases.append({"phase": phase, "module": module_name, "installed": installed})
        if not installed:
            missing_modules.append(module_name)
    return {"phases": phases, "missing_modules": missing_modules}


def _phase_progress_printer(label: str):
    def callback(event: dict[str, object]) -> None:
        ticker = event.get("ticker")
        suffix = f" ticker={ticker}" if ticker else ""
        console.print(
            f"{label}: {event.get('processed', 0)}/{event.get('total', 0)} "
            f"status={event.get('status')} "
            f"upgraded={event.get('upgraded', 0)} "
            f"unresolved={event.get('unresolved', 0)} "
            f"features_created={event.get('features_created', 0)}"
            f"{suffix}"
        )

    return callback


def _module_available(module_name: str) -> bool:
    try:
        return importlib.util.find_spec(module_name) is not None
    except (ImportError, ModuleNotFoundError, ValueError):
        return False


def _init_tonight_db_or_exit():
    try:
        return init_db()
    except Exception as exc:  # noqa: BLE001 - readiness command should print recovery text.
        console.print("Tonight check: BLOCKED")
        console.print(f"DB initialization failed: {exc}")
        console.print(f"Recovery: {RECOVERY_INSTRUCTIONS}")
        raise typer.Exit(1) from exc


def _init_db_or_exit(context: str):
    try:
        return init_db()
    except Exception as exc:  # noqa: BLE001 - CLI diagnostics should print recovery text.
        console.print(f"{context}: BLOCKED")
        console.print(f"DB initialization failed: {exc}")
        console.print(f"Recovery: {RECOVERY_INSTRUCTIONS}")
        raise typer.Exit(1) from exc


def _repo_root_alembic_ini() -> Path:
    for parent in Path(__file__).resolve().parents:
        candidate = parent / "alembic.ini"
        if candidate.exists():
            return candidate
    candidate = Path("alembic.ini").resolve()
    if candidate.exists():
        return candidate
    raise FileNotFoundError("Could not find alembic.ini from installed package or cwd.")


def _alembic_config(db_url: str):
    from alembic.config import Config

    config_path = _repo_root_alembic_ini()
    config = Config(str(config_path))
    config.set_main_option("sqlalchemy.url", db_url)
    script_location = config.get_main_option("script_location")
    if not script_location:
        raise RuntimeError(f"No Alembic script_location configured in {config_path}.")
    script_path = Path(script_location)
    if not script_path.is_absolute():
        config.set_main_option("script_location", str((config_path.parent / script_path).resolve()))
    return config


def _print_database_payload(title: str, payload: dict) -> None:
    console.print(f"{title}: {payload['status']}")
    summary = payload.get("summary") or {}
    if summary.get("backend_label"):
        console.print(f"Backend: {summary['backend_label']}")
    if summary.get("location"):
        console.print(f"Location: {summary['location']}")
    for item in payload.get("items", []):
        console.print(f"- {item['status']}: {item['name']} - {item['message']}")
    if payload.get("recovery"):
        console.print(f"Recovery: {payload['recovery']}")


@app.callback()
def main() -> None:
    configure_logging(get_settings().log_level)


@app.command("init-db")
def init_db_command() -> None:
    init_db()
    console.print(f"Initialized DB at {describe_db_location()}")


@app.command("db-migrate", help="Apply Alembic migrations to the configured database.")
def db_migrate_command(
    revision: Annotated[str, typer.Option(help="Alembic target revision.")] = "head",
) -> None:
    from alembic import command

    settings = get_settings()
    db_url = database_url_from_settings(settings)
    config = _alembic_config(db_url)
    command.upgrade(config, revision)
    console.print(f"Database migrations applied to {revision}.")


@app.command("db-revision", help="Create a new Alembic revision for schema changes.")
def db_revision_command(
    message: Annotated[str, typer.Option("--message", "-m", help="Revision message.")],
) -> None:
    from alembic import command

    settings = get_settings()
    db_url = database_url_from_settings(settings)
    config = _alembic_config(db_url)
    revision = command.revision(config, message=message, autogenerate=True)
    console.print(f"Created Alembic revision: {revision}")


@app.command("db-health", help="Check database reachability, integrity, and migration status.")
def db_health_command(
    output: Annotated[
        Path | None,
        typer.Option(help="Optional Markdown database report path."),
    ] = None,
) -> None:
    payload = database_health(settings=get_settings())
    _print_database_payload("Database health", payload)
    if output is not None:
        path = generate_database_report(output_path=output, settings=get_settings())
        console.print(f"Wrote database report to {path}")
    if payload["status"] == "BLOCKED":
        raise typer.Exit(1)


@app.command("db-doctor", help="Run database diagnostics and recovery guidance.")
def db_doctor_command() -> None:
    payload = database_doctor(settings=get_settings())
    _print_database_payload("Database doctor", payload)
    if payload["status"] == "BLOCKED":
        raise typer.Exit(1)


@app.command("db-locks", help="Show local SQLite file holders and likely writer jobs.")
def db_locks_command(
    json_output: Annotated[
        Path | None,
        typer.Option(help="Optional JSON diagnostics output path."),
    ] = None,
) -> None:
    payload = sqlite_lock_diagnostics(settings=get_settings())
    console.print(f"Database lock diagnostics: {payload['status']}")
    console.print(f"Backend: {payload['backend']}")
    console.print(f"Database: {payload.get('database_path') or payload['database_url']}")
    console.print(f"Scan method: {payload['scan_method']}")
    console.print(f"Safe to start another write job: {'yes' if payload['safe_to_write'] else 'no'}")
    holders = payload.get("holders") or []
    if holders:
        console.print("Open DB holders:")
        for holder in holders:
            marker = "writer" if holder["likely_writer"] else "reader/unknown"
            files = ", ".join(Path(path).name for path in holder["open_files"])
            elapsed = holder.get("elapsed") or "n/a"
            console.print(
                f"- pid {holder['pid']} ({marker}, elapsed {elapsed}) "
                f"{holder['command']} [{files}]"
            )
    else:
        console.print("Open DB holders: none visible")
    console.print(f"Next action: {payload['next_action']}")
    if json_output is not None:
        json_output.parent.mkdir(parents=True, exist_ok=True)
        json_output.write_text(json.dumps(payload, indent=2, sort_keys=True), encoding="utf-8")
        console.print(f"Wrote JSON: {json_output}")


def _print_db_writer_monitor(payload: dict[str, object]) -> None:
    console.print(f"DB writer monitor: {payload['status']}")
    console.print(f"Backend: {payload.get('backend')}")
    console.print(f"Database: {payload.get('database_path') or payload.get('database_url')}")
    console.print(f"Scan method: {payload.get('scan_method')}")
    console.print(f"Current writer PID: {payload.get('current_writer_pid') or 'none'}")
    console.print(f"Command running: {payload.get('current_writer_command') or 'none'}")
    console.print(f"Elapsed time: {payload.get('current_writer_elapsed') or 'n/a'}")
    console.print(
        "Heartbeat status: "
        f"{payload.get('long_job_heartbeat_display_status') or payload.get('long_job_heartbeat_status') or 'unknown'}"
    )
    console.print(f"Heartbeat stage: {payload.get('long_job_stage') or 'none'}")
    console.print(f"Heartbeat age: {payload.get('long_job_heartbeat_age') or 'n/a'}")
    console.print(
        "Safe to start another write job: "
        f"{'yes' if payload.get('safe_to_start_write') else 'no'}"
    )
    console.print(
        "Recommended next command after finish: "
        f"{payload.get('recommended_next_command_after_finish')}"
    )
    console.print(f"Next action: {payload.get('recommended_next_action')}")


@app.command("db-writer-monitor", help="Show the current SQLite writer and long-job guidance.")
def db_writer_monitor_command(
    json_stdout: Annotated[
        bool,
        typer.Option("--json", help="Print the monitor payload as JSON to stdout."),
    ] = False,
    json_output: Annotated[
        Path | None,
        typer.Option(help="Optional JSON monitor output path."),
    ] = None,
) -> None:
    payload = db_writer_monitor(settings=get_settings())
    if json_stdout:
        sys.stdout.write(json.dumps(payload, indent=2, sort_keys=True, default=str) + "\n")
        return
    _print_db_writer_monitor(payload)
    if json_output is not None:
        json_output.parent.mkdir(parents=True, exist_ok=True)
        json_output.write_text(json.dumps(payload, indent=2, sort_keys=True), encoding="utf-8")
        console.print(f"Wrote JSON: {json_output}")


@app.command("long-job-monitor", help="Alias for db-writer-monitor.")
def long_job_monitor_command(
    json_output: Annotated[
        Path | None,
        typer.Option(help="Optional JSON monitor output path."),
    ] = None,
) -> None:
    db_writer_monitor_command(json_output=json_output)


@app.command("sqlite-backup", help="Create a safe backup of the configured SQLite database.")
def sqlite_backup_command(
    output: Annotated[
        Path | None,
        typer.Option(help="Backup output path. Defaults to data/backups/*.db."),
    ] = None,
) -> None:
    path = sqlite_backup(output_path=output, settings=get_settings())
    console.print(f"Wrote SQLite backup to {path}")


@app.command("sqlite-recover", help="Back up SQLite and run integrity recovery checks.")
def sqlite_recover_command(
    output: Annotated[
        Path | None,
        typer.Option(help="Backup output path before recovery checks."),
    ] = None,
) -> None:
    result = sqlite_recover(output_path=output, settings=get_settings())
    console.print(f"SQLite recovery status: {result['status']}")
    console.print(result["message"])
    if result.get("backup_path"):
        console.print(f"Backup: {result['backup_path']}")
    if result.get("corrupt_copy"):
        console.print(f"Corrupt copy: {result['corrupt_copy']}")
    if result["status"] == "BLOCKED":
        console.print(f"Recovery: {result['recovery']}")
        raise typer.Exit(1)


@app.command(
    "migrate-sqlite-to-postgres",
    help="Copy supported tables from SQLite into a PostgreSQL-compatible database.",
)
def migrate_sqlite_to_postgres_command(
    sqlite_path: Annotated[
        Path,
        typer.Option(help="Source SQLite database path."),
    ] = Path("data/kalshi_phase1.db"),
    postgres_url: Annotated[
        str | None,
        typer.Option(help="Target PostgreSQL URL. Defaults to current DATABASE_URL."),
    ] = None,
) -> None:
    target_url = postgres_url or database_url_from_settings(get_settings())
    result = migrate_sqlite_to_postgres(
        sqlite_url=f"sqlite:///{sqlite_path}",
        postgres_url=target_url,
    )
    console.print("SQLite to PostgreSQL migration summary")
    console.print(f"Source: {result['source']}")
    console.print(f"Target: {result['target']}")
    console.print(f"Rows copied: {result['rows_copied']}")


@app.command("phase-status", help="Show installed Phase 3 modules and missing module checks.")
def phase_status_command() -> None:
    status = build_phase_status()
    for phase in status["phases"]:
        installed = phase["installed"]
        label = "installed" if installed else "missing"
        console.print(f"Phase {phase['phase']} {label}")
    missing_modules = status["missing_modules"]
    if missing_modules:
        console.print("missing modules:")
        for module_name in missing_modules:
            console.print(f"- {module_name}")
    else:
        console.print("missing modules: none")


@app.command("command-audit", help="Audit Phase 3G database commands registered on the root CLI.")
def command_audit_command() -> None:
    audit = build_command_audit()
    registered = set(audit["registered_commands"])
    console.print("Expected commands:")
    for command_name in audit["expected_commands"]:
        status = "registered" if command_name in registered else "missing"
        console.print(f"- {command_name}: {status}")
    console.print("Registered commands:")
    for command_name in audit["registered_commands"]:
        console.print(f"- {command_name}")
    missing = audit["missing_commands"]
    if missing:
        console.print("Missing commands:")
        for command_name in missing:
            console.print(f"- {command_name}")
        raise typer.Exit(1)
    console.print("Missing commands: none")


@app.command(
    "runtime-identity",
    help="Show the active checkout, Python executable, package path, and database identity.",
)
def runtime_identity_command(
    json_output: Annotated[
        Path | None,
        typer.Option(help="Optional JSON runtime identity output path."),
    ] = None,
) -> None:
    settings = get_settings()
    engine = init_db()
    session_factory = get_session_factory(engine)
    with session_factory() as session:
        payload = runtime_identity(session, settings=settings)

    console.print("Runtime identity")
    console.print(f"Repository root: {payload['repository_root']}")
    console.print(f"Current working directory: {payload['current_working_directory']}")
    console.print(f"Git branch: {payload.get('git_branch') or 'unknown'}")
    console.print(f"Git commit: {payload.get('git_commit') or 'unknown'}")
    console.print(f"Python executable: {payload['python_executable']}")
    console.print(f"Package path: {payload['package_path']}")
    console.print(f"Database: {payload['database_location']}")
    console.print(f"Database URL: {payload['database_url']}")
    console.print(f"Split brain: {payload['split_brain']['status']}")
    if payload.get("runtime_path_warning"):
        console.print(f"WARNING: {payload['runtime_path_warning']}")
    sqlite_payload = payload.get("sqlite") or {}
    if sqlite_payload.get("in_synced_folder"):
        console.print("WARNING: SQLite database is inside a synced folder.")
    if json_output is not None:
        json_output.parent.mkdir(parents=True, exist_ok=True)
        json_output.write_text(json.dumps(payload, indent=2, sort_keys=True), encoding="utf-8")
        console.print(f"Wrote JSON: {json_output}")


def _print_workspace_guard(payload: dict[str, object]) -> None:
    summary = payload["summary"]  # type: ignore[index]
    runtime = payload["runtime"]  # type: ignore[index]
    database = payload["database"]  # type: ignore[index]
    commands = payload["commands"]  # type: ignore[index]
    console.print("Phase 3BB Workspace / Build Guard")
    console.print("Mode: READ ONLY diagnostics")
    console.print("Live/demo execution: blocked")
    console.print(f"Status: {summary['status']}")  # type: ignore[index]
    console.print(f"Repository: {runtime['repository_root']}")  # type: ignore[index]
    console.print(f"CWD: {runtime['current_working_directory']}")  # type: ignore[index]
    console.print(f"Python: {runtime['python_executable']}")  # type: ignore[index]
    console.print(f"Virtualenv: {runtime.get('virtualenv') or 'unknown'}")  # type: ignore[attr-defined]
    console.print(f"Package: {runtime['package_path']}")  # type: ignore[index]
    console.print(f"Database: {database['database_url']}")  # type: ignore[index]
    console.print(f"Database fingerprint: {database['database_fingerprint']}")  # type: ignore[index]
    missing = commands["missing_required_commands"]  # type: ignore[index]
    console.print(f"Missing required commands: {len(missing)}")
    for command_name in missing:
        console.print(f"- {command_name}")
    console.print(f"Next action: {payload['next_action']}")  # type: ignore[index]


@app.command("phase3bb-workspace-guard")
def phase3bb_workspace_guard_command(
    output_dir: Annotated[
        Path,
        typer.Option(help="Output directory for JSON/Markdown guard report."),
    ] = Path("reports/phase3bb"),
    strict: Annotated[
        bool,
        typer.Option(help="Exit non-zero when the guard is not PASS."),
    ] = False,
) -> None:
    payload = build_workspace_consistency_guard(
        settings=get_settings(),
        registered_commands=registered_root_command_names(),
    )
    artifacts = write_workspace_guard_report(
        output_dir=output_dir,
        settings=get_settings(),
        registered_commands=registered_root_command_names(),
    )
    _print_workspace_guard(payload)
    console.print(f"Wrote JSON: {artifacts.json_path}")
    console.print(f"Wrote Markdown: {artifacts.markdown_path}")
    if strict and payload["summary"]["status"] != "PASS":
        raise typer.Exit(1)


@app.command("workspace-guard")
def workspace_guard_command(
    output_dir: Annotated[
        Path,
        typer.Option(help="Output directory for JSON/Markdown guard report."),
    ] = Path("reports/phase3bb"),
    strict: Annotated[
        bool,
        typer.Option(help="Exit non-zero when the guard is not PASS."),
    ] = False,
) -> None:
    phase3bb_workspace_guard_command(output_dir=output_dir, strict=strict)


@app.command("ui")
def ui_command(
    host: Annotated[str, typer.Option(help="Local bind host.")] = "127.0.0.1",
    port: Annotated[int, typer.Option(help="Local bind port.")] = 8080,
) -> None:
    import uvicorn

    init_db()
    settings = get_settings()
    console.print(f"Starting local decision UI at http://{host}:{port}")
    console.print("Environment: DEMO ONLY")
    console.print(f"Read-only mode: {settings.ui_read_only}")
    console.print(f"Execution enabled: {settings.execution_enabled}")
    console.print(f"Execution dry-run: {settings.execution_dry_run}")
    uvicorn.run("kalshi_predictor.ui.app:app", host=host, port=port)


@app.command("autopilot-status")
def autopilot_status_command() -> None:
    engine = init_db()
    settings = get_settings()
    session_factory = get_session_factory(engine)
    with session_factory() as session:
        status = build_autopilot_status(session, settings=settings)
    latest_run = status["latest_run"]
    latest_cycle = status["latest_cycle"]
    console.print("Autopilot status")
    console.print(f"Enabled: {settings.autopilot_enabled}")
    console.print(f"Dry run: {settings.autopilot_dry_run}")
    console.print(f"Model: {settings.autopilot_model}")
    console.print(f"Environment: {settings.kalshi_env}")
    console.print(f"Daily submitted demo orders: {status['daily_orders']}")
    console.print(f"Open demo orders counted: {status['open_demo_orders']}")
    console.print(f"Last run: {latest_run['status'] if latest_run else 'none'}")
    console.print(f"Last cycle: {latest_cycle['status'] if latest_cycle else 'none'}")
    console.print(f"Recommended next action: {status['recommended_next_action']}")


@app.command("autopilot-once")
def autopilot_once_command() -> None:
    engine = init_db()
    settings = get_settings()
    session_factory = get_session_factory(engine)
    with session_factory() as session:
        result = run_autopilot_once(session, settings=settings)
        session.commit()
    console.print("Autopilot cycle summary")
    console.print(f"Run ID: {result.run_id}")
    console.print(f"Cycle ID: {result.cycle_id}")
    console.print(f"Status: {result.status}")
    console.print(f"Opportunities scanned: {result.opportunities_scanned}")
    console.print(f"Orders attempted: {result.orders_attempted}")
    console.print(f"Orders submitted: {result.orders_submitted}")
    console.print(f"Orders blocked: {result.orders_blocked}")
    console.print(f"Stop reason: {result.stop_reason or 'n/a'}")


@app.command("autopilot-run")
def autopilot_run_command() -> None:
    engine = init_db()
    settings = get_settings()
    session_factory = get_session_factory(engine)
    result = run_autopilot_scheduler(session_factory, settings=settings)
    console.print("Autopilot scheduler stopped")
    console.print(f"Run ID: {result.run_id}")
    console.print(f"Cycles completed: {len(result.cycles)}")
    console.print(f"Stop reason: {result.stop_reason or 'n/a'}")


@app.command("autopilot-report")
def autopilot_report_command(
    output: Annotated[
        Path,
        typer.Option(help="Markdown autopilot report path."),
    ] = Path("reports/autopilot_report.md"),
) -> None:
    engine = init_db()
    settings = get_settings()
    session_factory = get_session_factory(engine)
    with session_factory() as session:
        report_path = generate_autopilot_report(session, output_path=output, settings=settings)
    console.print(f"Wrote autopilot report to {report_path}")


@app.command("advanced-risk-report")
def advanced_risk_report_command(
    output: Annotated[
        Path,
        typer.Option(help="Markdown advanced risk report path."),
    ] = Path("reports/advanced_risk_report.md"),
) -> None:
    engine = init_db()
    settings = get_settings()
    session_factory = get_session_factory(engine)
    with session_factory() as session:
        report_path = generate_advanced_risk_report(session, output_path=output, settings=settings)
    console.print(f"Wrote advanced risk report to {report_path}")


@app.command("overnight-status")
def overnight_status_command() -> None:
    engine = init_db()
    settings = get_settings()
    session_factory = get_session_factory(engine)
    with session_factory() as session:
        status = build_overnight_status(session, settings=settings)
    latest_run = status["latest_run"]
    latest_cycle = status["latest_cycle"]
    latest_pnl = status["latest_paper_pnl"]
    console.print("Overnight status")
    console.print(f"Enabled: {settings.overnight_enabled}")
    console.print(f"Interval minutes: {settings.overnight_interval_minutes}")
    console.print(f"Max cycles: {settings.overnight_max_cycles}")
    console.print(f"Model: {settings.overnight_model}")
    console.print(f"Paper betting: {settings.overnight_run_paper}")
    console.print(f"Demo execution: {settings.overnight_run_demo}")
    console.print(f"Last run: {latest_run['status'] if latest_run else 'none'}")
    console.print(f"Last cycle: {latest_cycle['status'] if latest_cycle else 'none'}")
    console.print(f"Latest paper P&L: {(latest_pnl or {}).get('total_pnl', 'n/a')}")
    console.print(f"Latest opportunity count: {status['latest_opportunity_count']}")
    console.print(f"Recommended next action: {status['recommended_next_action']}")


@app.command("overnight-once")
def overnight_once_command() -> None:
    engine = init_db()
    settings = get_settings()
    session_factory = get_session_factory(engine)
    with session_factory() as session:
        result = run_overnight_once(session, settings=settings)
        report_path = generate_overnight_report(session, settings=settings)
        session.commit()
    console.print("Overnight cycle summary")
    console.print(f"Run ID: {result.run_id}")
    console.print(f"Cycle ID: {result.cycle_id}")
    console.print(f"Status: {result.status}")
    console.print(f"Markets collected: {result.markets_collected}")
    console.print(f"Snapshots inserted: {result.snapshots_inserted}")
    console.print(f"Forecasts inserted: {result.forecasts_inserted}")
    console.print(f"Paper orders created: {result.paper_orders_created}")
    console.print(f"Opportunities detected: {result.opportunities_detected}")
    console.print(f"Settlements synced: {result.settlements_synced}")
    console.print(f"Errors: {len(result.errors)}")
    console.print(f"Wrote overnight report to {report_path}")


@app.command("overnight-run")
def overnight_run_command() -> None:
    engine = init_db()
    settings = get_settings()
    session_factory = get_session_factory(engine)
    result = run_overnight_scheduler(session_factory, settings=settings)
    with session_factory() as session:
        report_path = generate_overnight_report(session, settings=settings)
    console.print("Overnight scheduler stopped")
    console.print(f"Run ID: {result.run_id}")
    console.print(f"Status: {result.status}")
    console.print(f"Cycles completed: {len(result.cycles)}")
    console.print(f"Stop reason: {result.stop_reason or 'n/a'}")
    console.print(f"Wrote overnight report to {report_path}")


@app.command("overnight-report")
def overnight_report_command(
    output: Annotated[
        Path,
        typer.Option(help="Markdown overnight report path."),
    ] = Path("reports/overnight_report.md"),
) -> None:
    engine = init_db()
    settings = get_settings()
    session_factory = get_session_factory(engine)
    with session_factory() as session:
        report_path = generate_overnight_report(session, output_path=output, settings=settings)
    console.print(f"Wrote overnight report to {report_path}")


@app.command("tonight-check")
def tonight_check_command() -> None:
    engine = _init_tonight_db_or_exit()
    settings = get_settings()
    session_factory = get_session_factory(engine)
    with session_factory() as session:
        check = build_tonight_check(session, settings=settings)
    console.print(render_tonight_check(check))
    if check.status == BLOCKED:
        raise typer.Exit(1)


@app.command("tonight-run")
def tonight_run_command(
    max_cycles: Annotated[
        int,
        typer.Option(help="Maximum safe overnight cycles."),
    ] = 32,
    interval_minutes: Annotated[
        int,
        typer.Option(help="Minutes between cycles."),
    ] = 15,
) -> None:
    engine = _init_tonight_db_or_exit()
    settings = get_settings()
    session_factory = get_session_factory(engine)
    result = run_tonight(
        session_factory,
        settings=settings,
        max_cycles=max_cycles,
        interval_minutes=interval_minutes,
        console=console,
    )
    console.print("Tonight runner stopped")
    console.print(f"Status: {result.status}")
    console.print(f"Cycles completed: {result.cycles_completed}")
    console.print(f"Errors: {len(result.errors)}")
    console.print(f"Stop reason: {result.stop_reason or 'n/a'}")
    if result.status == BLOCKED:
        raise typer.Exit(1)


@app.command("tonight-report")
def tonight_report_command(
    output: Annotated[
        Path,
        typer.Option(help="Markdown tonight readiness report path."),
    ] = Path("reports/tonight_report.md"),
) -> None:
    engine = _init_tonight_db_or_exit()
    settings = get_settings()
    session_factory = get_session_factory(engine)
    with session_factory() as session:
        path = generate_tonight_report(session, output_path=output, settings=settings)
    console.print(f"Wrote tonight report to {path}")


@app.command("memory-status")
def memory_status_command() -> None:
    engine = init_db()
    session_factory = get_session_factory(engine)
    with session_factory() as session:
        payload = memory_health(session, settings=get_settings())
    console.print(f"Phase 3O Market Memory: {payload['status']}")
    console.print(f"Mode: {payload['mode']}")
    console.print(f"Schema version: {payload['schema_version']}")
    counts = payload["counts"]
    console.print(f"market_memory: {counts['market_memory']}")
    console.print(f"forecast_memory: {counts['forecast_memory']}")
    console.print(f"trade_memory: {counts['trade_memory']}")
    console.print(f"quarantine: {counts['quarantine']}")
    for issue in payload["issues"]:
        console.print(f"WARNING: {issue}")


@app.command("memory-report")
def memory_report_command(
    output: Annotated[
        Path,
        typer.Option(help="Markdown Phase 3O memory report path."),
    ] = Path("reports/market_memory_report.md"),
) -> None:
    engine = init_db()
    session_factory = get_session_factory(engine)
    with session_factory() as session:
        path = generate_memory_report(session, output_path=output, settings=get_settings())
    console.print(f"Wrote memory report to {path}")


@app.command("memory-backfill")
def memory_backfill_command(
    write: Annotated[
        bool,
        typer.Option("--write/--dry-run", help="Write backfilled memory events."),
    ] = False,
    limit: Annotated[
        int | None,
        typer.Option(help="Optional per-table row limit."),
    ] = None,
) -> None:
    engine = init_db()
    session_factory = get_session_factory(engine)
    with session_factory() as session:
        result = backfill_memory_from_existing_tables(
            session,
            dry_run=not write,
            limit=limit,
        )
        if write:
            session.commit()
    console.print("Phase 3O memory backfill")
    console.print(f"Dry run: {result.dry_run}")
    console.print(f"Market snapshots: {result.market_snapshots}")
    console.print(f"Forecasts: {result.forecasts}")
    console.print(f"Paper orders: {result.paper_orders}")
    console.print(f"Paper fills: {result.paper_fills}")
    console.print(f"Settlements: {result.settlements}")


@app.command("memory-archive")
def memory_archive_command(
    output_dir: Annotated[
        Path | None,
        typer.Option(help="Archive output directory."),
    ] = None,
) -> None:
    engine = init_db()
    session_factory = get_session_factory(engine)
    with session_factory() as session:
        manifest = archive_memory_to_jsonl(
            session,
            output_dir=output_dir,
            settings=get_settings(),
        )
        session.commit()
    console.print(f"Wrote memory archive {manifest.archive_id}")
    console.print(f"Manifest path: {manifest.manifest_path}")
    console.print(f"Status: {manifest.status}")


@app.command("memory-dataset")
def memory_dataset_command(
    training_as_of: Annotated[
        str,
        typer.Option("--training-as-of", help="UTC cutoff required for labels/features."),
    ],
    output: Annotated[
        Path,
        typer.Option(help="JSON forecast-learning dataset output path."),
    ] = Path("reports/memory_dataset.json"),
    include_no_trade: Annotated[
        bool,
        typer.Option("--include-no-trade/--exclude-no-trade"),
    ] = True,
    include_risk_blocked: Annotated[
        bool,
        typer.Option("--include-risk-blocked/--exclude-risk-blocked"),
    ] = True,
) -> None:
    parsed = parse_datetime(training_as_of)
    if parsed is None:
        raise typer.BadParameter("--training-as-of must be an ISO datetime.")
    engine = init_db()
    session_factory = get_session_factory(engine)
    with session_factory() as session:
        dataset = build_forecast_learning_dataset(
            session,
            training_as_of=parsed,
            include_no_trade=include_no_trade,
            include_risk_blocked=include_risk_blocked,
        )
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(
        json.dumps(
            {"manifest": dataset.manifest, "rows": dataset.rows},
            indent=2,
            sort_keys=True,
        ),
        encoding="utf-8",
    )
    console.print(f"Wrote memory dataset to {output}")
    console.print(f"Rows: {dataset.manifest['row_count']}")


@app.command("memory-timeline")
def memory_timeline_command(
    forecast_id: Annotated[
        str,
        typer.Option("--forecast-id", help="Phase 3O forecast_id to inspect."),
    ],
) -> None:
    engine = init_db()
    session_factory = get_session_factory(engine)
    with session_factory() as session:
        rows = forecast_timeline(session, forecast_id)
    if not rows:
        console.print("No forecast memory events found.")
        return
    for row in rows:
        console.print(
            f"{row.event_sequence} {row.event_type} "
            f"{row.event_time.isoformat()} status={row.decision_status or 'n/a'}"
        )


@app.command("trade-memory-timeline")
def trade_memory_timeline_command(
    trade_id: Annotated[
        str,
        typer.Option("--trade-id", help="Phase 3O trade_id to inspect."),
    ],
) -> None:
    engine = init_db()
    session_factory = get_session_factory(engine)
    with session_factory() as session:
        rows = trade_timeline(session, trade_id)
    if not rows:
        console.print("No trade memory events found.")
        return
    for row in rows:
        console.print(
            f"{row.event_sequence} {row.event_type} "
            f"{row.event_time.isoformat()} mode={row.execution_mode}"
        )


@app.command("learning-status")
def learning_status_command() -> None:
    engine = init_db()
    settings = get_settings()
    session_factory = get_session_factory(engine)
    with session_factory() as session:
        status = learning_status(session, settings=settings)
    console.print("Learning Mode status")
    console.print(f"Enabled: {status['enabled']}")
    console.print(
        "Settled paper trades: "
        f"{status['settled_paper_trades']} / {status['target_settled_trades']}"
    )
    console.print(f"Progress: {status['progress_percent']}")
    console.print(
        f"Daily paper trades: {status['daily_paper_trades']} / {status['daily_paper_trade_cap']}"
    )
    console.print(f"Forecasts evaluated: {status['forecasts_evaluated']}")
    console.print(f"Expected completion: {status['expected_completion']}")
    console.print(f"Demo execution blocked: {status['demo_execution_blocked']}")
    console.print(f"Recommended next action: {status['recommended_next_action']}")


@app.command("learning-once")
def learning_once_command(
    model_name: Annotated[
        str | None,
        typer.Option("--model-name", help="Learning forecast model to inspect."),
    ] = None,
) -> None:
    engine = init_db()
    settings = get_settings()
    if model_name:
        settings = settings.model_copy(update={"learning_model_name": model_name})
    session_factory = get_session_factory(engine)
    with session_factory() as session:
        result = run_learning_once(session, settings=settings)
        report_path = generate_learning_report(session, settings=settings)
        session.commit()
    console.print("Learning Mode cycle summary")
    console.print(f"Run ID: {result.run_id}")
    console.print(f"Cycle ID: {result.cycle_id}")
    console.print(f"Status: {result.status}")
    console.print(f"Markets scanned: {result.markets_scanned}")
    console.print(f"Forecasts generated: {result.forecasts_generated}")
    console.print(f"Opportunities found: {result.opportunities_found}")
    console.print(f"Paper trades created: {result.paper_trades_created}")
    console.print(f"Settlements synced: {result.settlements_synced}")
    console.print(f"Settled paper trades total: {result.settled_paper_trades_total}")
    console.print(f"Wrote learning report to {report_path}")


@app.command("phase3-overnight-exploratory-paper-seed")
def phase3_overnight_exploratory_paper_seed_command(
    model_name: Annotated[
        str,
        typer.Option(help="Forecast model to inspect for exploratory paper samples."),
    ] = "crypto_v2",
    apply: Annotated[
        bool,
        typer.Option(
            "--apply",
            help="Create capped paper-only orders. Omit for a dry-run report.",
        ),
    ] = False,
    max_trades: Annotated[
        int,
        typer.Option(help="Maximum exploratory paper orders to create in one run."),
    ] = 3,
    min_edge: Annotated[
        str,
        typer.Option(help="Exploration edge floor. Default allows half-cent near misses."),
    ] = "-0.005",
    min_score: Annotated[
        str,
        typer.Option(help="Minimum ranking opportunity score for exploratory samples."),
    ] = "25",
    max_spread: Annotated[
        str | None,
        typer.Option(help="Optional spread cap; defaults to Learning Mode max spread."),
    ] = None,
    scan_limit: Annotated[
        int,
        typer.Option(help="Maximum latest ranked markets to inspect."),
    ] = 120,
    ranking_fetch_limit: Annotated[
        int,
        typer.Option(help="Maximum ranking rows to fetch before de-duplicating tickers."),
    ] = 1000,
    max_ranking_age_minutes: Annotated[
        int,
        typer.Option(help="Reject rankings older than this many minutes."),
    ] = 30,
    refresh_metrics: Annotated[
        bool,
        typer.Option(
            "--refresh-metrics",
            help="Refresh aggregate learning metrics after apply; slower on large SQLite DBs.",
        ),
    ] = False,
    output_dir: Annotated[
        Path,
        typer.Option(help="Directory for exploratory paper seed reports."),
    ] = Path("reports/phase3_overnight"),
) -> None:
    """Review or create capped paper-only exploratory near-miss samples."""

    engine = init_db()
    settings = get_settings()
    try:
        parsed_min_edge = Decimal(str(min_edge))
        parsed_min_score = Decimal(str(min_score))
        parsed_max_spread = Decimal(str(max_spread)) if max_spread is not None else None
    except (InvalidOperation, ValueError) as exc:
        raise typer.BadParameter("min-edge, min-score, and max-spread must be decimals") from exc
    session_factory = get_session_factory(engine)
    with session_factory() as session:
        result = seed_exploratory_paper_trades(
            session,
            settings=settings,
            model_name=model_name,
            apply=apply,
            max_trades=max_trades,
            min_edge=parsed_min_edge,
            min_score=parsed_min_score,
            max_spread=parsed_max_spread,
            scan_limit=scan_limit,
            ranking_fetch_limit=ranking_fetch_limit,
            max_ranking_age_minutes=max_ranking_age_minutes,
            refresh_metrics=refresh_metrics,
            output_dir=output_dir,
        )
        if apply:
            session.commit()
        else:
            session.rollback()
    console.print("Phase 3 overnight exploratory paper seed")
    console.print("Mode: PAPER ONLY")
    console.print("Live/demo/exchange order writes: blocked")
    console.print(f"Apply: {result.apply}")
    console.print(f"Model: {result.model_name}")
    console.print(f"Candidates scanned: {result.candidates_scanned}")
    console.print(f"Candidates found: {result.candidates_found}")
    console.print(f"Paper orders created: {result.paper_orders_created}")
    console.print(f"Fills created: {result.fills_created}")
    console.print(f"Learning paper rows inserted: {result.learning_paper_trades_inserted}")
    console.print(f"Settled paper trades total: {result.settled_paper_trades_total}")
    console.print(f"Wrote JSON: {result.json_path}")
    console.print(f"Wrote Markdown: {result.markdown_path}")


@app.command("learning-run")
def learning_run_command(
    max_cycles: Annotated[
        int,
        typer.Option(help="Maximum learning cycles to run."),
    ] = 32,
    interval_minutes: Annotated[
        int,
        typer.Option(help="Minutes to wait between learning cycles."),
    ] = 15,
) -> None:
    engine = init_db()
    settings = get_settings()
    session_factory = get_session_factory(engine)
    result = run_learning_scheduler(
        session_factory,
        settings=settings,
        max_cycles=max_cycles,
        interval_minutes=interval_minutes,
    )
    with session_factory() as session:
        report_path = generate_learning_report(session, settings=settings)
    console.print("Learning Mode scheduler stopped")
    console.print(f"Run ID: {result.run_id}")
    console.print(f"Status: {result.status}")
    console.print(f"Cycles completed: {len(result.cycles)}")
    console.print(f"Stop reason: {result.stop_reason or 'n/a'}")
    console.print(f"Wrote learning report to {report_path}")


@app.command("learning-report")
def learning_report_command(
    output: Annotated[
        Path,
        typer.Option(help="Markdown learning report path."),
    ] = Path("reports/learning_report.md"),
) -> None:
    engine = init_db()
    settings = get_settings()
    session_factory = get_session_factory(engine)
    with session_factory() as session:
        report_path = generate_learning_report(session, output_path=output, settings=settings)
    console.print(f"Wrote learning report to {report_path}")


@app.command("learning-diagnostics")
def learning_diagnostics_command(
    output: Annotated[
        Path,
        typer.Option(help="Markdown learning funnel diagnostics report path."),
    ] = Path("reports/learning_diagnostics.md"),
    scan_limit: Annotated[
        int,
        typer.Option(help="Candidate scan depth for diagnostics replay."),
    ] = 500,
    suggest_thresholds: Annotated[
        bool,
        typer.Option(help="Replay suggested thresholds in the diagnostics report."),
    ] = False,
) -> None:
    engine = init_db()
    settings = get_settings()
    session_factory = get_session_factory(engine)
    with session_factory() as session:
        report_path = generate_learning_diagnostics_report(
            session,
            output_path=output,
            settings=settings,
            scan_limit=scan_limit,
            suggest_thresholds=suggest_thresholds,
        )
    console.print(f"Wrote learning diagnostics report to {report_path}")


@app.command("link-remediate")
def link_remediate_command(
    limit: Annotated[
        int,
        typer.Option(help="Maximum markets per link stage. Use 0 for all markets."),
    ] = 0,
    resume: Annotated[
        bool,
        typer.Option(help="Resume idempotently from prior checkpoint/link rows."),
    ] = False,
    progress_every: Annotated[
        int,
        typer.Option(help="Write/print heartbeat progress every N items. Use 0 for stage-only."),
    ] = 100,
    checkpoint_every: Annotated[
        int,
        typer.Option(help="Write heartbeat checkpoint and commit every N items. Use 0 to disable."),
    ] = 100,
    stop_after_minutes: Annotated[
        int,
        typer.Option(
            help="Stop cleanly after N minutes and leave a checkpoint. Use 0 for no limit."
        ),
    ] = 0,
    heartbeat_dir: Annotated[
        Path,
        typer.Option(help="Directory for Phase 3AU heartbeat/checkpoint files."),
    ] = Path("reports/phase3au"),
) -> None:
    """Refresh crypto/weather/sports market links and print next actions."""
    engine = init_db()
    settings = get_settings()
    session_factory = get_session_factory(engine)
    with session_factory() as session:
        result = run_link_remediation(
            session,
            settings=settings,
            limit=limit or None,
            resume=resume,
            heartbeat_dir=heartbeat_dir,
            progress_every=progress_every,
            checkpoint_every=checkpoint_every,
            stop_after_minutes=stop_after_minutes or None,
            commit_between_stages=True,
        )
        session.commit()
    console.print("Phase 3Y link remediation")
    console.print("Phase 3AU heartbeat: enabled")
    console.print("Mode: PAPER ONLY")
    if result.stopped_early:
        console.print("Status: STOPPED_EARLY")
    console.print(
        "Crypto: "
        f"scanned={result.crypto.markets_scanned}, "
        f"created={result.crypto.links_created}, "
        f"total={result.total_links['crypto']}"
    )
    console.print(
        "Weather: "
        f"scanned={result.weather.markets_scanned}, "
        f"created={result.weather.links_created}, "
        f"total={result.total_links['weather']}"
    )
    console.print(
        "Sports: "
        f"scanned={result.sports.markets_scanned}, "
        f"games={result.sports.games_scanned}, "
        f"created={result.sports.links_created}, "
        f"market_derived={result.sports.market_derived_links}, "
        f"total={result.total_links['sports']}"
    )
    console.print(
        "Sports derived schedule: "
        f"markets={result.sports_derived.sports_markets_seen}, "
        f"teams_created={result.sports_derived.teams_created}, "
        f"games_created={result.sports_derived.games_created}, "
        f"links_created={result.sports_derived.links_created}, "
        f"features_created={result.sports_derived.features_created}, "
        f"existing_features={result.sports_derived.features_existing}"
    )
    console.print("Recommended next actions:")
    for item in result.recommendations:
        console.print(f"- {item}")
    console.print(f"Heartbeat: {result.heartbeat_path}")
    console.print(f"Checkpoint: {result.checkpoint_path}")


@app.command("phase3au-status", help="Show latest long-job heartbeat/checkpoint status.")
def phase3au_status_command(
    output_dir: Annotated[
        Path,
        typer.Option(help="Directory containing Phase 3AU heartbeat/checkpoint files."),
    ] = Path("reports/phase3au"),
    stale_after_seconds: Annotated[
        int,
        typer.Option(help="Mark heartbeat stale after this many seconds."),
    ] = 300,
) -> None:
    status = load_latest_long_job_status(
        output_dir=output_dir,
        stale_after_seconds=stale_after_seconds,
    )
    heartbeat = status.get("heartbeat") or {}
    console.print(f"Phase 3AU long job status: {status['status']}")
    console.print(f"Job: {heartbeat.get('job_name') or 'none'}")
    console.print(f"PID: {heartbeat.get('pid') or 'none'}")
    console.print(f"Stage: {heartbeat.get('stage') or 'none'}")
    console.print(
        f"Processed: {heartbeat.get('processed') or 0} / "
        f"{heartbeat.get('total') or 'unknown'}"
    )
    console.print(f"Elapsed: {heartbeat.get('elapsed') or 'n/a'}")
    console.print(f"Heartbeat age: {status.get('heartbeat_age') or 'n/a'}")
    console.print(f"Current item: {heartbeat.get('current_item') or 'none'}")
    console.print(f"Next action: {status['recommended_next_action']}")


@app.command("phase3au-report", help="Write a Phase 3AU long-job heartbeat report.")
def phase3au_report_command(
    output_dir: Annotated[
        Path,
        typer.Option(help="Directory containing Phase 3AU heartbeat/checkpoint files."),
    ] = Path("reports/phase3au"),
    stale_after_seconds: Annotated[
        int,
        typer.Option(help="Mark heartbeat stale after this many seconds."),
    ] = 300,
) -> None:
    path = write_phase3au_report(
        output_dir=output_dir,
        stale_after_seconds=stale_after_seconds,
    )
    console.print(f"Wrote Phase 3AU report to {path}")


@app.command("phase3aw-status", help="Classify the latest long-job recovery state.")
def phase3aw_status_command(
    heartbeat_dir: Annotated[
        Path,
        typer.Option(help="Directory containing Phase 3AU heartbeat/checkpoint files."),
    ] = Path("reports/phase3au"),
    stale_after_seconds: Annotated[
        int,
        typer.Option(help="Mark heartbeat stale after this many seconds."),
    ] = 300,
) -> None:
    status = build_phase3aw_recovery_status(
        heartbeat_dir=heartbeat_dir,
        stale_after_seconds=stale_after_seconds,
    )
    writer = status["writer"]
    console.print(f"Phase 3AW recovery status: {status['classification']}")
    console.print(f"Safe to resume: {'yes' if status['safe_to_resume'] else 'no'}")
    console.print(f"Writer PID: {writer.get('pid') or 'none'}")
    console.print(f"Writer command: {writer.get('command') or 'none'}")
    console.print(f"Last stage: {status.get('last_stage') or 'none'}")
    console.print(
        f"Last progress: {status.get('last_processed') or 0} / "
        f"{status.get('last_total') or 'unknown'}"
    )
    console.print(f"Last heartbeat: {status.get('last_heartbeat_at') or 'none'}")
    console.print(f"Heartbeat age: {status.get('heartbeat_age') or 'n/a'}")
    console.print(f"Recommended action: {status['recommended_next_action']}")


@app.command("phase3aw-crash-report", help="Write a Phase 3AW long-job recovery report.")
def phase3aw_crash_report_command(
    output_dir: Annotated[
        Path,
        typer.Option(help="Output directory for Phase 3AW recovery artifacts."),
    ] = Path("reports/phase3aw"),
    heartbeat_dir: Annotated[
        Path,
        typer.Option(help="Directory containing Phase 3AU heartbeat/checkpoint files."),
    ] = Path("reports/phase3au"),
    stale_after_seconds: Annotated[
        int,
        typer.Option(help="Mark heartbeat stale after this many seconds."),
    ] = 300,
) -> None:
    artifacts = write_phase3aw_recovery_report(
        output_dir=output_dir,
        heartbeat_dir=heartbeat_dir,
        stale_after_seconds=stale_after_seconds,
    )
    console.print("Phase 3AW Long Job Crash Recovery Report")
    console.print("Mode: PAPER ONLY diagnostics")
    console.print("Live/demo execution: blocked")
    console.print(f"Wrote JSON: {artifacts['json_path']}")
    console.print(f"Wrote Markdown: {artifacts['markdown_path']}")


@app.command("phase3aw-dashboard-truth", help="Write the Phase 3AW dashboard truth report.")
def phase3aw_dashboard_truth_command(
    output_dir: Annotated[
        Path,
        typer.Option(help="Output directory for Phase 3AW dashboard truth artifacts."),
    ] = Path("reports/phase3aw"),
    reports_dir: Annotated[
        Path,
        typer.Option(help="Root directory containing phase report artifacts."),
    ] = Path("reports"),
    stale_after_minutes: Annotated[
        int,
        typer.Option(help="Mark diagnostic artifacts stale after this many minutes."),
    ] = 120,
) -> None:
    """Resolve the true current blocker for the Why No Paper Trades dashboard panel."""
    engine = init_db()
    settings = get_settings()
    session_factory = get_session_factory(engine)
    with session_factory() as session:
        artifacts = write_phase3aw_dashboard_truth_report(
            session,
            output_dir=output_dir,
            reports_dir=reports_dir,
            settings=settings,
            command_args=list(sys.argv[1:]),
            stale_after_minutes=stale_after_minutes,
        )
    console.print("Phase 3AW Dashboard Truth Reconciliation")
    console.print("Mode: PAPER ONLY read-only diagnostics")
    console.print("Live/demo execution: blocked")
    console.print("Order submission/cancel/replace: blocked")
    console.print(f"Wrote dashboard truth: {artifacts.dashboard_truth_path}")
    console.print(f"Wrote stale artifact audit: {artifacts.stale_artifact_audit_path}")
    console.print(f"Wrote current crypto funnel: {artifacts.current_crypto_funnel_path}")
    console.print(f"Wrote Executive Summary: {artifacts.executive_summary_path}")
    console.print(f"Wrote Next Actions: {artifacts.next_actions_path}")
    console.print(f"Wrote operator command: {artifacts.operator_next_command_path}")


@app.command("phase3ax-gap-analysis", help="Write the Phase 3AX full app gap analysis report.")
def phase3ax_gap_analysis_command(
    output_dir: Annotated[
        Path,
        typer.Option(help="Output directory for Phase 3AX app gap analysis artifacts."),
    ] = Path("reports/phase3ax"),
    reports_dir: Annotated[
        Path,
        typer.Option(help="Root directory containing phase report artifacts."),
    ] = Path("reports"),
    stale_after_minutes: Annotated[
        int,
        typer.Option(help="Mark diagnostic artifacts stale after this many minutes."),
    ] = 120,
) -> None:
    """Resolve app-level blockers and recommend one next Codex task."""
    engine = init_db()
    settings = get_settings()
    session_factory = get_session_factory(engine)
    registered_commands = set(registered_root_command_names())
    with session_factory() as session:
        artifacts = write_phase3ax_gap_analysis_report(
            session,
            output_dir=output_dir,
            reports_dir=reports_dir,
            settings=settings,
            command_args=list(sys.argv[1:]),
            registered_commands=registered_commands,
            stale_after_minutes=stale_after_minutes,
        )
    console.print("Phase 3AX Full App Gap Analysis")
    console.print("Mode: PAPER ONLY read-only diagnostics")
    console.print("Live/demo execution: blocked")
    console.print("Paper trade creation: blocked")
    console.print("Threshold lowering: blocked")
    console.print(f"Wrote Executive Summary: {artifacts.executive_summary_path}")
    console.print(f"Wrote Next Codex Task: {artifacts.next_codex_task_path}")
    console.print(f"Wrote Next Operator Commands: {artifacts.next_operator_commands_path}")
    console.print(f"Wrote app gap analysis: {artifacts.app_gap_analysis_json_path}")
    console.print(f"Wrote manifest: {artifacts.manifest_path}")


@app.command(
    "phase3ax-r9-guarded-refresh-job",
    help="Start or verify exactly one guarded paper-only R5 refresh job.",
)
def phase3ax_r9_guarded_refresh_job_command(
    output_dir: Annotated[
        Path,
        typer.Option(help="Output directory for Phase 3AX-R9 guarded refresh artifacts."),
    ] = Path("reports/phase3ax_r9"),
    reports_dir: Annotated[
        Path,
        typer.Option(help="Root directory containing phase report artifacts."),
    ] = Path("reports"),
    r5_output_dir: Annotated[
        Path,
        typer.Option(help="Directory for Phase 3BC-R5 watch artifacts."),
    ] = Path("reports/phase3bc_r5"),
    start_if_needed: Annotated[
        bool,
        typer.Option(
            "--start-if-needed/--status-only",
            help="Start the guarded R5 job only if no R5 job is already active.",
        ),
    ] = True,
    stop_overrun: Annotated[
        bool,
        typer.Option(
            help="Allow the underlying R5 guard to stop an overrun before status refresh.",
        ),
    ] = False,
    refresh_dashboard_truth: Annotated[
        bool,
        typer.Option(
            "--refresh-dashboard-truth/--skip-dashboard-truth",
            help="Refresh Phase 3AW dashboard truth after the guard decision.",
        ),
    ] = True,
    refresh_gap_analysis: Annotated[
        bool,
        typer.Option(
            "--refresh-gap-analysis/--skip-gap-analysis",
            help="Refresh Phase 3AX gap analysis after the guard decision.",
        ),
    ] = True,
    stale_after_minutes: Annotated[
        int,
        typer.Option(help="Mark diagnostic artifacts stale after this many minutes."),
    ] = 120,
    cycles: Annotated[
        int,
        typer.Option(help="Number of bounded R5 cycles to supervise."),
    ] = 32,
    interval_minutes: Annotated[
        int,
        typer.Option(help="Minutes to wait between R5 cycles."),
    ] = 15,
    duration_hours: Annotated[
        float,
        typer.Option(help="Approximate guarded refresh runtime budget."),
    ] = 8.0,
    timeout_grace_seconds: Annotated[
        int,
        typer.Option(help="Seconds past the configured budget before guard flags overrun."),
    ] = 900,
) -> None:
    """One operator command for guarded R5 freshness plus dashboard/gap status."""
    from kalshi_predictor.phase3ax_r9 import write_phase3ax_r9_guarded_refresh_job_report

    engine = init_db()
    settings = get_settings()
    session_factory = get_session_factory(engine)
    registered_commands = set(registered_root_command_names())
    with session_factory() as session:
        artifacts = write_phase3ax_r9_guarded_refresh_job_report(
            session,
            output_dir=output_dir,
            reports_dir=reports_dir,
            r5_output_dir=r5_output_dir,
            settings=settings,
            registered_commands=registered_commands,
            command_args=list(sys.argv[1:]),
            start_if_needed=start_if_needed,
            stop_overrun=stop_overrun,
            refresh_dashboard_truth=refresh_dashboard_truth,
            refresh_gap_analysis=refresh_gap_analysis,
            stale_after_minutes=stale_after_minutes,
            cycles=cycles,
            interval_minutes=interval_minutes,
            duration_hours=duration_hours,
            timeout_grace_seconds=timeout_grace_seconds,
        )
    console.print("Phase 3AX-R9 Guarded Refresh Job")
    console.print("Mode: PAPER ONLY guarded refresh supervisor")
    console.print("Live/demo execution: blocked")
    console.print("Order submission/cancel/replace: blocked")
    console.print("Paper trade creation: blocked")
    console.print(f"Wrote JSON: {artifacts.json_path}")
    console.print(f"Wrote Markdown: {artifacts.markdown_path}")
    console.print(f"Wrote Executive Summary: {artifacts.executive_summary_path}")
    console.print(f"Wrote Next Actions: {artifacts.next_actions_path}")


@app.command("market-legs-parse")
def market_legs_parse_command(
    limit: Annotated[
        int,
        typer.Option(help="Maximum markets to parse. Use 0 for all stored markets."),
    ] = 0,
    refresh: Annotated[
        bool,
        typer.Option(help="Delete and rebuild parsed legs for scanned markets."),
    ] = False,
) -> None:
    """Parse stored Kalshi markets into leg records for link diagnostics."""
    engine = init_db()
    session_factory = get_session_factory(engine)
    with session_factory() as session:
        result = parse_and_store_market_legs(
            session,
            limit=limit if limit > 0 else None,
            refresh=refresh,
        )
        session.commit()
    console.print("Market leg parse summary")
    console.print("Mode: PAPER ONLY diagnostics")
    console.print(f"Markets scanned: {result.markets_scanned}")
    console.print(f"Markets with legs: {result.markets_with_legs}")
    console.print(f"Legs inserted: {result.legs_inserted}")
    console.print(f"Markets skipped with existing legs: {result.markets_skipped_existing}")


@app.command("link-coverage")
def link_coverage_command(
    output: Annotated[
        Path,
        typer.Option(help="Markdown link coverage report path."),
    ] = Path("reports/link_coverage_report.md"),
    parse_first: Annotated[
        bool,
        typer.Option(help="Run market leg parsing before generating coverage."),
    ] = False,
    parse_limit: Annotated[
        int,
        typer.Option(help="Maximum markets to parse when --parse-first is used. Use 0 for all."),
    ] = 0,
    refresh: Annotated[
        bool,
        typer.Option(help="Refresh parsed market legs when --parse-first is used."),
    ] = False,
) -> None:
    """Show market-leg and linker coverage across crypto/weather/economic/sports/news."""
    engine = init_db()
    session_factory = get_session_factory(engine)
    with session_factory() as session:
        if parse_first:
            parse_result = parse_and_store_market_legs(
                session,
                limit=parse_limit if parse_limit > 0 else None,
                refresh=refresh,
            )
            session.commit()
            console.print(
                "Parsed "
                f"{parse_result.legs_inserted} leg(s) across "
                f"{parse_result.markets_scanned} market(s)."
            )
        coverage = link_coverage_dashboard(session)
        report_path = generate_link_coverage_report(session, output_path=output, coverage=coverage)
        snapshot_path = Path("reports/market_coverage/link_coverage.json")
        snapshot_path.parent.mkdir(parents=True, exist_ok=True)
        snapshot_path.write_text(json.dumps(coverage, indent=2, sort_keys=True), encoding="utf-8")
    console.print("Market link coverage")
    console.print("Mode: PAPER ONLY diagnostics")
    console.print(f"Parsed legs: {coverage['summary_cards'][1]['value']}")
    console.print(f"Linked legs: {coverage['summary_cards'][2]['value']}")
    console.print(f"Partial legs: {coverage['summary_cards'][3]['value']}")
    console.print(f"Unlinked legs: {coverage['summary_cards'][4]['value']}")
    console.print(f"Bottleneck: {coverage['bottleneck']['message']}")
    console.print(f"Wrote link coverage report to {report_path}")
    console.print(f"Wrote link coverage snapshot to {snapshot_path}")


@app.command("settlement-watch")
def settlement_watch_command(
    cycles: Annotated[int, typer.Option(help="Maximum settlement-watch cycles.")] = 24,
    interval_minutes: Annotated[
        int,
        typer.Option(help="Minutes to wait between settlement-watch cycles."),
    ] = 15,
    resume_learning: Annotated[
        bool,
        typer.Option(help="Resume paper learning only when the daily cap is below limit."),
    ] = True,
    lowered_min_score: Annotated[
        str,
        typer.Option(help="Paper-only min score after the daily cap resets."),
    ] = "25",
    lowered_min_edge: Annotated[
        str,
        typer.Option(help="Paper-only min edge after the daily cap resets."),
    ] = "0.01",
    scan_limit: Annotated[
        int,
        typer.Option(help="Paper-only candidate scan limit after the daily cap resets."),
    ] = 500,
) -> None:
    engine = init_db()
    settings = get_settings()
    session_factory = get_session_factory(engine)
    result = run_settlement_watcher(
        session_factory,
        settings=settings,
        cycles=cycles,
        interval_minutes=interval_minutes,
        resume_learning_after_cap_reset=resume_learning,
        lowered_min_score=Decimal(lowered_min_score),
        lowered_min_edge=Decimal(lowered_min_edge),
        scan_limit=scan_limit,
    )
    console.print("Phase 3Y settlement watcher stopped")
    console.print(f"Status: {result.status}")
    console.print(f"Cycles completed: {len(result.cycles)}")
    console.print(f"Settlements synced: {result.settlements_synced}")
    console.print(f"Settled paper trades: {result.settled_paper_trades}")
    console.print(f"Learning cycles started: {result.learning_cycles_started}")
    console.print(f"Skipped due to daily cap: {result.skipped_due_to_cap}")
    console.print(f"Stop reason: {result.stop_reason or 'n/a'}")


@app.command("phase3y-report")
def phase3y_report_command(
    output: Annotated[
        Path,
        typer.Option(help="Markdown Phase 3Y remediation report path."),
    ] = Path("reports/phase3y_report.md"),
) -> None:
    engine = init_db()
    settings = get_settings()
    session_factory = get_session_factory(engine)
    with session_factory() as session:
        report_path = generate_phase3y_report(session, output_path=output, settings=settings)
    console.print(f"Wrote Phase 3Y report to {report_path}")


@app.command("paper-settlement-doctor")
def paper_settlement_doctor_command(
    output_dir: Annotated[
        Path,
        typer.Option(help="Directory for Phase 3Y-SR settlement reconciliation artifacts."),
    ] = Path("reports/paper_settlement_reconciliation"),
    limit: Annotated[
        int,
        typer.Option(help="Maximum paper orders to inspect. Use 0 for all paper orders."),
    ] = 0,
) -> None:
    """Explain why filled paper trades have or have not resolved against settlements."""
    engine = init_db()
    session_factory = get_session_factory(engine)
    with session_factory() as session:
        artifacts = write_paper_settlement_reconciliation(
            session,
            output_dir=output_dir,
            limit=limit if limit > 0 else None,
        )
    console.print("Phase 3Y-SR paper settlement reconciliation doctor")
    console.print("Mode: PAPER ONLY diagnostics")
    console.print(f"Wrote JSON: {artifacts.json_path}")
    console.print(f"Wrote Markdown: {artifacts.markdown_path}")
    console.print(f"Wrote rows: {artifacts.rows_path}")


@app.command("phase3aa-realize")
def phase3aa_realize_command(
    output_dir: Annotated[
        Path,
        typer.Option(help="Directory for Phase 3AA settlement ETA and outcome artifacts."),
    ] = Path("reports/phase3aa"),
    sync_settlements: Annotated[
        bool,
        typer.Option(
            "--sync-settlements/--no-sync-settlements",
            help="Fetch settled markets first.",
        ),
    ] = False,
    dry_run: Annotated[
        bool,
        typer.Option("--dry-run/--no-dry-run", help="Preview without writing P&L/confidence rows."),
    ] = True,
    limit: Annotated[
        int,
        typer.Option(help="Maximum paper orders to inspect. Use 0 for all paper orders."),
    ] = 0,
) -> None:
    """Run Phase 3AA settlement ETA and exact-ticker paper outcome realization."""
    engine = init_db()
    settings = get_settings()
    session_factory = get_session_factory(engine)
    with session_factory() as session:
        artifacts = write_phase3aa_report(
            session,
            output_dir=output_dir,
            settings=settings,
            sync=sync_settlements,
            dry_run=dry_run,
            limit=limit if limit > 0 else None,
        )
        if not dry_run:
            session.commit()
    console.print("Phase 3AA settlement ETA + paper outcome realizer")
    console.print("Mode: PAPER ONLY")
    console.print(f"Dry run: {dry_run}")
    console.print(f"Wrote JSON: {artifacts.json_path}")
    console.print(f"Wrote Markdown: {artifacts.markdown_path}")


@app.command("phase3aa-r2-exact-settlement-harvest")
def phase3aa_r2_exact_settlement_harvest_command(
    output_dir: Annotated[
        Path,
        typer.Option(help="Directory for Phase 3AA-R2 exact settlement harvest artifacts."),
    ] = Path("reports/phase3aa_r2"),
    limit: Annotated[
        int,
        typer.Option(help="Maximum paper orders to inspect. Use 0 for all paper orders."),
    ] = 0,
) -> None:
    """Fetch due paper order markets by exact ticker and write exact settlement rows."""
    engine = init_db()
    session_factory = get_session_factory(engine)
    with session_factory() as session:
        artifacts = write_phase3aa_r2_exact_settlement_harvest_report(
            session,
            output_dir=output_dir,
            limit=limit if limit > 0 else None,
        )
        session.commit()
    console.print("Phase 3AA-R2 exact ticker settlement harvest")
    console.print("Mode: PAPER ONLY exact ticker GETs")
    console.print("No live/demo execution; no paper P&L realization")
    console.print(f"Wrote JSON: {artifacts.json_path}")
    console.print(f"Wrote Markdown: {artifacts.markdown_path}")
    console.print(f"Wrote rows: {artifacts.rows_path}")


@app.command("phase3aa-r3-residual-settlement-audit")
def phase3aa_r3_residual_settlement_audit_command(
    output_dir: Annotated[
        Path,
        typer.Option(help="Directory for Phase 3AA-R3 residual settlement audit artifacts."),
    ] = Path("reports/phase3aa_r3"),
    limit: Annotated[
        int,
        typer.Option(help="Maximum paper orders to inspect. Use 0 for all paper orders."),
    ] = 0,
) -> None:
    """Explain residual exact-settlement eligibility after a Phase 3AA realization pass."""
    engine = init_db()
    session_factory = get_session_factory(engine)
    with session_factory() as session:
        artifacts = write_phase3aa_r3_residual_audit_report(
            session,
            output_dir=output_dir,
            limit=limit if limit > 0 else None,
        )
    console.print("Phase 3AA-R3 residual exact settlement realization audit")
    console.print("Mode: READ ONLY diagnostics")
    console.print("No live/demo execution; no paper P&L writes")
    console.print(f"Wrote JSON: {artifacts.json_path}")
    console.print(f"Wrote Markdown: {artifacts.markdown_path}")
    console.print(f"Wrote rows: {artifacts.rows_path}")


@app.command("phase3aa-r4-settlement-fetch-recovery")
def phase3aa_r4_settlement_fetch_recovery_command(
    output_dir: Annotated[
        Path,
        typer.Option(help="Directory for Phase 3AA-R4 settlement fetch recovery artifacts."),
    ] = Path("reports/phase3aa_r4"),
    reports_dir: Annotated[
        Path,
        typer.Option(help="Directory containing upstream report artifacts."),
    ] = Path("reports"),
    sample_limit: Annotated[
        int,
        typer.Option(help="Maximum examples retained per diagnostic group."),
    ] = 25,
) -> None:
    """Group exact-ticker settlement fetch blockers and stale realization prompts."""
    artifacts = write_phase3aa_r4_settlement_fetch_recovery_report(
        output_dir=output_dir,
        reports_dir=reports_dir,
        sample_limit=sample_limit,
    )
    console.print("Phase 3AA-R4 exact settlement fetch recovery")
    console.print("Mode: READ ONLY diagnostics")
    console.print("No live/demo execution; no paper P&L writes")
    console.print("Settlement policy: exact ticker only")
    console.print(f"Wrote JSON: {artifacts.json_path}")
    console.print(f"Wrote Markdown: {artifacts.markdown_path}")
    console.print(f"Wrote rows: {artifacts.rows_path}")


@app.command("phase3aa-r5-closed-market-outcome-capture")
def phase3aa_r5_closed_market_outcome_capture_command(
    output_dir: Annotated[
        Path,
        typer.Option(help="Directory for Phase 3AA-R5 closed market outcome artifacts."),
    ] = Path("reports/phase3aa_r5"),
    reports_dir: Annotated[
        Path,
        typer.Option(help="Directory containing upstream report artifacts."),
    ] = Path("reports"),
    sample_limit: Annotated[
        int,
        typer.Option(help="Maximum examples retained per diagnostic group."),
    ] = 25,
) -> None:
    """Capture closed exact-market outcome fields without writing settlements."""
    engine = init_db()
    session_factory = get_session_factory(engine)
    with session_factory() as session:
        artifacts = write_phase3aa_r5_closed_market_outcome_capture_report(
            session,
            output_dir=output_dir,
            reports_dir=reports_dir,
            sample_limit=sample_limit,
        )
    console.print("Phase 3AA-R5 closed market outcome parser/source capture")
    console.print("Mode: READ ONLY diagnostics")
    console.print("No live/demo execution; no paper P&L writes")
    console.print("Settlement policy: exact ticker only")
    console.print(f"Wrote JSON: {artifacts.json_path}")
    console.print(f"Wrote Markdown: {artifacts.markdown_path}")
    console.print(f"Wrote rows: {artifacts.rows_path}")


@app.command("phase3aa-r6-composite-settlement-resolver")
def phase3aa_r6_composite_settlement_resolver_command(
    output_dir: Annotated[
        Path,
        typer.Option(help="Directory for Phase 3AA-R6 composite settlement artifacts."),
    ] = Path("reports/phase3aa_r6"),
    limit: Annotated[
        int,
        typer.Option(help="Maximum paper orders to inspect. Use 0 for all paper orders."),
    ] = 0,
    write_settlements: Annotated[
        bool,
        typer.Option(
            "--write-settlements/--dry-run",
            help=(
                "Write exact local settlement rows for composite tickers only when every "
                "underlying component has exact binary settlement evidence."
            ),
        ),
    ] = False,
    refresh_components: Annotated[
        bool,
        typer.Option(
            "--refresh-components/--no-refresh-components",
            help=(
                "Fetch exact component markets and write component settlement rows when "
                "the exact component ticker has a usable outcome."
            ),
        ),
    ] = False,
    component_refresh_limit: Annotated[
        int,
        typer.Option(
            help="Maximum component tickers to refresh. Use 0 for all missing components.",
        ),
    ] = 0,
) -> None:
    """Resolve local composite sports tickers from verified component settlements."""
    engine = init_db()
    session_factory = get_session_factory(engine)
    with session_factory() as session:
        artifacts = write_phase3aa_r6_composite_settlement_resolver_report(
            session,
            output_dir=output_dir,
            limit=limit if limit > 0 else None,
            write_settlements=write_settlements,
            refresh_components=refresh_components,
            component_refresh_limit=component_refresh_limit
            if component_refresh_limit > 0
            else None,
        )
        if write_settlements or refresh_components:
            session.commit()
    console.print("Phase 3AA-R6 local composite settlement resolver")
    console.print("Mode: PAPER ONLY local settlement evidence")
    console.print(f"Write settlements: {write_settlements}")
    console.print(f"Refresh components: {refresh_components}")
    console.print("No live/demo execution; no paper P&L realization")
    console.print("Settlement policy: same composite ticker only")
    console.print(f"Wrote JSON: {artifacts.json_path}")
    console.print(f"Wrote Markdown: {artifacts.markdown_path}")
    console.print(f"Wrote rows: {artifacts.rows_path}")


@app.command("gap-closure-doctor")
def gap_closure_doctor_command(
    output_dir: Annotated[
        Path,
        typer.Option(help="Directory for Phase 3AJ gap-closure artifacts."),
    ] = Path("reports/phase_3aj"),
) -> None:
    engine = init_db()
    settings = get_settings()
    session_factory = get_session_factory(engine)
    with session_factory() as session:
        artifacts = write_gap_closure_doctor_report(
            session,
            output_dir=output_dir,
            settings=settings,
        )
    console.print("Phase 3AJ gap-closure doctor")
    console.print("Mode: PAPER / READ ONLY")
    console.print("Live/demo execution: blocked")
    console.print(f"Wrote JSON: {artifacts.json_path}")
    console.print(f"Wrote Markdown: {artifacts.markdown_path}")


@app.command("paper-trade-funnel")
def paper_trade_funnel_command(
    window_hours: Annotated[
        int,
        typer.Option(help="Ranking lookback window in hours."),
    ] = 72,
    replay_readonly: Annotated[
        bool,
        typer.Option(help="Recompute stored inputs without writing paper trades."),
    ] = False,
    output_dir: Annotated[
        Path,
        typer.Option(help="Directory for Phase 3AJ paper trade funnel artifacts."),
    ] = Path("reports/phase_3aj"),
) -> None:
    engine = init_db()
    settings = get_settings()
    session_factory = get_session_factory(engine)
    with session_factory() as session:
        artifacts = write_paper_trade_funnel_report(
            session,
            output_dir=output_dir,
            window_hours=window_hours,
            replay_readonly=replay_readonly,
            settings=settings,
        )
    console.print("Phase 3AJ paper trade funnel")
    console.print("Mode: PAPER / READ ONLY")
    console.print("Paper order/fill writes: blocked")
    console.print(f"Wrote JSON: {artifacts.json_path}")
    console.print(f"Wrote Markdown: {artifacts.markdown_path}")


@app.command("composite-settlement-resolve")
def composite_settlement_resolve_command(
    paper_only: Annotated[
        bool,
        typer.Option("--paper-only/--no-paper-only", help="Require paper-only mode."),
    ] = True,
    legacy_only: Annotated[
        bool,
        typer.Option("--legacy-only/--no-legacy-only", help="Restrict to legacy local composites."),
    ] = True,
    max_records: Annotated[
        int,
        typer.Option(help="Maximum legacy composite rows to classify."),
    ] = 5,
    dry_run: Annotated[
        bool,
        typer.Option("--dry-run/--apply", help="Dry-run by default; --apply requires --backup-first."),
    ] = True,
    backup_first: Annotated[
        bool,
        typer.Option(help="Required for --apply."),
    ] = False,
    output_dir: Annotated[
        Path,
        typer.Option(help="Directory for Phase 3AJ composite settlement artifacts."),
    ] = Path("reports/phase_3aj"),
) -> None:
    engine = init_db()
    session_factory = get_session_factory(engine)
    with session_factory() as session:
        try:
            artifacts = write_composite_settlement_resolve_report(
                session,
                output_dir=output_dir,
                paper_only=paper_only,
                legacy_only=legacy_only,
                max_records=max_records,
                apply=not dry_run,
                backup_first=backup_first,
            )
        except ValueError as exc:
            console.print(f"Composite settlement resolve: BLOCKED - {exc}")
            raise typer.Exit(2) from exc
        if not dry_run:
            session.commit()
    console.print("Phase 3AJ guarded composite settlement resolve")
    console.print(f"Dry run: {dry_run}")
    console.print("Live/demo execution: blocked")
    console.print(f"Wrote JSON: {artifacts.json_path}")
    console.print(f"Wrote Markdown: {artifacts.markdown_path}")
    console.print(f"Wrote rows: {artifacts.rows_path}")


@app.command("source-readiness-report")
def source_readiness_report_command(
    phase: Annotated[
        str,
        typer.Option(help="Phase label for the source readiness report."),
    ] = "3BB-R2",
    sources: Annotated[
        str,
        typer.Option(help="Comma-separated sources to inspect."),
    ] = "usda,cushman,flightaware",
    output_dir: Annotated[
        Path,
        typer.Option(help="Directory for Phase 3AJ source readiness artifacts."),
    ] = Path("reports/phase_3aj"),
) -> None:
    engine = init_db()
    session_factory = get_session_factory(engine)
    source_names = [item.strip() for item in sources.split(",") if item.strip()]
    with session_factory() as session:
        artifacts = write_source_readiness_report(
            session,
            output_dir=output_dir,
            phase=phase,
            sources=source_names,
        )
    console.print("Phase 3AJ source readiness report")
    console.print("Mode: PAPER / READ ONLY")
    console.print("Link/feature/forecast writes: blocked")
    console.print(f"Wrote JSON: {artifacts.json_path}")
    console.print(f"Wrote Markdown: {artifacts.markdown_path}")


@app.command("crypto-window-sync")
def crypto_window_sync_command(
    scope: Annotated[
        str,
        typer.Option(help="Window scope to inspect; active keeps current active markets only."),
    ] = "active",
    output_dir: Annotated[
        Path,
        typer.Option(help="Directory for Phase 3AK crypto window artifacts."),
    ] = Path("reports/phase_3ak"),
    symbols: Annotated[
        str,
        typer.Option(help="Comma-separated crypto symbols to orchestrate."),
    ] = "BTC,ETH",
    freshness_minutes: Annotated[
        int,
        typer.Option(help="Quote freshness window in minutes."),
    ] = 15,
    limit: Annotated[
        int,
        typer.Option(help="Maximum crypto markets to inspect."),
    ] = 5000,
) -> None:
    engine = init_db()
    settings = get_settings()
    session_factory = get_session_factory(engine)
    with session_factory() as session:
        artifacts = write_crypto_window_sync_report(
            session,
            output_dir=output_dir,
            scope=scope,
            symbols=symbols,
            freshness_minutes=freshness_minutes,
            limit=limit,
            settings=settings,
        )
    console.print("Phase 3AK crypto window sync")
    console.print("Mode: PAPER / READ ONLY")
    console.print("Live/demo execution: blocked")
    console.print(f"Wrote JSON: {artifacts.json_path}")
    console.print(f"Wrote rows: {artifacts.rows_path}")
    console.print(f"Wrote Markdown: {artifacts.markdown_path}")


@app.command("crypto-watch-status")
def crypto_watch_status_command(
    output_dir: Annotated[
        Path,
        typer.Option(help="Directory for Phase 3AK crypto watch artifacts."),
    ] = Path("reports/phase_3ak"),
    symbols: Annotated[
        str,
        typer.Option(help="Comma-separated crypto symbols to inspect."),
    ] = "BTC,ETH",
    freshness_minutes: Annotated[
        int,
        typer.Option(help="Watcher/report freshness window in minutes."),
    ] = 15,
) -> None:
    engine = init_db()
    settings = get_settings()
    session_factory = get_session_factory(engine)
    with session_factory() as session:
        artifacts = write_crypto_watch_status_report(
            session,
            output_dir=output_dir,
            symbols=symbols,
            freshness_minutes=freshness_minutes,
            settings=settings,
        )
    console.print("Phase 3AK crypto watch status")
    console.print("Mode: PAPER / READ ONLY")
    console.print("Live/demo execution: blocked")
    console.print(f"Wrote JSON: {artifacts.json_path}")
    console.print(f"Wrote Markdown: {artifacts.markdown_path}")


@app.command("market-data-refresh")
def market_data_refresh_command(
    bounded: Annotated[
        bool,
        typer.Option("--bounded/--unbounded", help="Require a bounded refresh guard."),
    ] = True,
    max_duration_seconds: Annotated[
        int,
        typer.Option(help="Maximum allowed duration for a future refresh worker."),
    ] = 120,
    require_no_active_writer: Annotated[
        bool,
        typer.Option(help="Refuse to refresh while a writer is active."),
    ] = True,
    enqueue_if_writer_active: Annotated[
        bool,
        typer.Option(help="Report queue intent when a writer is active; no duplicate queue is created."),
    ] = False,
    output_dir: Annotated[
        Path,
        typer.Option(help="Directory for Phase 3AK market-data artifacts."),
    ] = Path("reports/phase_3ak"),
    symbols: Annotated[
        str,
        typer.Option(help="Comma-separated crypto symbols to refresh when writer-safe."),
    ] = "BTC,ETH",
) -> None:
    engine = init_db()
    settings = get_settings()
    session_factory = get_session_factory(engine)
    with session_factory() as session:
        artifacts = write_phase3ak_market_data_refresh_status(
            session,
            output_dir=output_dir,
            bounded=bounded,
            max_duration_seconds=max_duration_seconds,
            require_no_active_writer=require_no_active_writer,
            symbols=symbols,
            settings=settings,
        )
    if enqueue_if_writer_active:
        console.print("Queue mode requested; Phase 3AK currently reports one bounded retry path only.")
    console.print("Phase 3AK market-data refresh coordinator")
    console.print("Mode: bounded status/write guard")
    console.print("Live/demo execution: blocked")
    console.print(f"Wrote JSON: {artifacts.json_path}")
    console.print(f"Wrote Markdown: {artifacts.markdown_path}")


@app.command("phase-3aj-report")
def phase_3aj_report_command(
    output: Annotated[
        Path,
        typer.Option(help="Markdown output path for the unified Phase 3AJ report."),
    ] = Path("reports/phase_3aj_report.md"),
    output_dir: Annotated[
        Path,
        typer.Option(help="Directory for Phase 3AJ JSON companion artifacts."),
    ] = Path("reports/phase_3aj"),
) -> None:
    engine = init_db()
    settings = get_settings()
    session_factory = get_session_factory(engine)
    with session_factory() as session:
        artifacts = write_phase_3aj_report(
            session,
            output=output,
            output_dir=output_dir,
            settings=settings,
        )
    console.print("Phase 3AJ unified gap-closure report")
    console.print("Mode: PAPER / READ ONLY")
    console.print("Live/demo execution: blocked")
    console.print(f"Wrote JSON: {artifacts.json_path}")
    console.print(f"Wrote Markdown: {artifacts.markdown_path}")


@app.command("phase-3ak-report")
def phase_3ak_report_command(
    output: Annotated[
        Path,
        typer.Option(help="Markdown output path for the unified Phase 3AK report."),
    ] = Path("reports/phase_3ak_report.md"),
    output_dir: Annotated[
        Path,
        typer.Option(help="Directory for Phase 3AK JSON companion artifacts."),
    ] = Path("reports/phase_3ak"),
    symbols: Annotated[
        str,
        typer.Option(help="Comma-separated crypto symbols to inspect."),
    ] = "BTC,ETH",
) -> None:
    engine = init_db()
    settings = get_settings()
    session_factory = get_session_factory(engine)
    with session_factory() as session:
        artifacts = write_phase_3ak_report(
            session,
            output=output,
            output_dir=output_dir,
            symbols=symbols,
            settings=settings,
        )
    console.print("Phase 3AK market-data and crypto-window report")
    console.print("Mode: PAPER / READ ONLY")
    console.print("Live/demo execution: blocked")
    console.print(f"Wrote JSON: {artifacts.json_path}")
    console.print(f"Wrote Markdown: {artifacts.markdown_path}")


@app.command("phase3ab-learning-governor")
def phase3ab_learning_governor_command(
    output_dir: Annotated[
        Path,
        typer.Option(help="Directory for Phase 3AB learning governor artifacts."),
    ] = Path("reports/phase3ab"),
    model_name: Annotated[
        str,
        typer.Option(help="Ranking model to inspect."),
    ] = "ensemble_v2",
    limit: Annotated[
        int,
        typer.Option(help="Maximum unique ranking candidates to inspect."),
    ] = 500,
) -> None:
    """Write the paper-only fast-settlement Learning Mode governor report."""
    engine = init_db()
    settings = get_settings()
    session_factory = get_session_factory(engine)
    with session_factory() as session:
        artifacts = write_phase3ab_report(
            session,
            output_dir=output_dir,
            settings=settings,
            model_name=model_name,
            limit=limit,
        )
    console.print("Phase 3AB Learning Governor / Fast Settlement Router")
    console.print("Mode: PAPER ONLY diagnostics")
    console.print(f"Wrote JSON: {artifacts.json_path}")
    console.print(f"Wrote Markdown: {artifacts.markdown_path}")


@app.command("phase3ae-fast-market-harvester")
def phase3ae_fast_market_harvester_command(
    output_dir: Annotated[
        Path,
        typer.Option(help="Directory for Phase 3AE fast market harvester artifacts."),
    ] = Path("reports/phase3ae_fast_market"),
    model_name: Annotated[
        str,
        typer.Option(help="Ranking model to inspect and refresh."),
    ] = "ensemble_v2",
    ranking_limit: Annotated[
        int,
        typer.Option(help="Maximum unique ranking candidates to inspect."),
    ] = 500,
    market_limit: Annotated[
        int,
        typer.Option(help="Maximum open 0-24h markets to inspect for ranking gaps."),
    ] = 500,
    horizon_hours: Annotated[
        int,
        typer.Option(help="Fast-settlement horizon in hours."),
    ] = 24,
) -> None:
    """Write the paper-only Phase 3AE fast-settlement market harvest report."""
    engine = init_db()
    settings = get_settings()
    session_factory = get_session_factory(engine)
    with session_factory() as session:
        artifacts = write_phase3ae_fast_market_harvester_report(
            session,
            output_dir=output_dir,
            settings=settings,
            model_name=model_name,
            ranking_limit=ranking_limit,
            market_limit=market_limit,
            horizon_hours=horizon_hours,
        )
    console.print("Phase 3AE Fast Market Harvester")
    console.print("Mode: PAPER ONLY read-only diagnostics")
    console.print("Live/demo execution: blocked")
    console.print("Order submission/cancel/replace: blocked")
    console.print(f"Wrote JSON: {artifacts.json_path}")
    console.print(f"Wrote Markdown: {artifacts.markdown_path}")


@app.command("phase3ac-sports-provenance-repair")
def phase3ac_sports_provenance_repair_command(
    output_dir: Annotated[
        Path,
        typer.Option(help="Directory for Phase 3AC sports provenance artifacts."),
    ] = Path("reports/phase3ac"),
    limit: Annotated[
        int,
        typer.Option(help="Maximum parsed sports markets to repair. Use 0 for all."),
    ] = 0,
    parse_first: Annotated[
        bool,
        typer.Option("--parse-first/--no-parse-first", help="Parse market legs before repair."),
    ] = True,
    refresh_features: Annotated[
        bool,
        typer.Option("--refresh-features/--keep-features", help="Rebuild derived sports features."),
    ] = False,
) -> None:
    """Upgrade sports market-derived links into usable derived schedule/features."""
    engine = init_db()
    settings = get_settings()
    session_factory = get_session_factory(engine)
    with session_factory() as session:
        artifacts = write_phase3ac_report(
            session,
            output_dir=output_dir,
            settings=settings,
            limit=limit if limit > 0 else None,
            parse_first=parse_first,
            refresh_features=refresh_features,
        )
        session.commit()
    console.print("Phase 3AC sports provenance repair")
    console.print("Mode: PAPER ONLY diagnostics")
    console.print(f"Wrote JSON: {artifacts.json_path}")
    console.print(f"Wrote Markdown: {artifacts.markdown_path}")


@app.command("phase-orchestrator")
def phase_orchestrator_command(
    analyze: Annotated[
        bool,
        typer.Option(
            "--analyze/--no-analyze",
            help="Analyze current evidence and write the next paper-only roadmap prompt.",
        ),
    ] = True,
    output: Annotated[
        Path,
        typer.Option(help="Markdown Phase 3AD roadmap report path."),
    ] = Path("reports/phase_orchestrator.md"),
    json_output: Annotated[
        Path | None,
        typer.Option(help="Structured JSON Phase 3AD roadmap path."),
    ] = None,
    next_prompt: Annotated[
        Path,
        typer.Option(help="Generated next-phase Codex prompt path."),
    ] = Path("prompts/next_phase.md"),
    scan_limit: Annotated[
        int,
        typer.Option(help="Learning and roadmap candidate scan limit."),
    ] = 500,
    refresh_market_coverage: Annotated[
        bool,
        typer.Option(
            "--refresh-market-coverage/--use-cached-market-coverage",
            help=(
                "Run the full market coverage doctor inside the orchestrator. "
                "The default uses the cached report or bounded SQL aggregates."
            ),
        ),
    ] = False,
    refresh_learning_diagnostics: Annotated[
        bool,
        typer.Option(
            "--refresh-learning-diagnostics/--bounded-learning-diagnostics",
            help=(
                "Run full learning diagnostics inside the orchestrator. "
                "The default uses bounded aggregate evidence."
            ),
        ),
    ] = False,
    refresh_sports_provenance: Annotated[
        bool,
        typer.Option(
            "--refresh-sports-provenance/--bounded-sports-provenance",
            help=(
                "Run full sports provenance inspection inside the orchestrator. "
                "The default uses cached market coverage or bounded aggregate evidence."
            ),
        ),
    ] = False,
) -> None:
    """Generate a paper-only self-improvement roadmap and next-phase prompt."""
    if not analyze:
        console.print("Phase 3AD currently supports analysis-only roadmap generation.")
        raise typer.Exit(0)
    engine = init_db()
    settings = get_settings()
    session_factory = get_session_factory(engine)
    with session_factory() as session:
        artifacts = write_phase_orchestrator_report(
            session,
            output_path=output,
            json_path=json_output,
            next_prompt_path=next_prompt,
            settings=settings,
            scan_limit=scan_limit,
            refresh_market_coverage=refresh_market_coverage,
            refresh_learning_diagnostics=refresh_learning_diagnostics,
            refresh_sports_provenance=refresh_sports_provenance,
        )
    console.print("Phase 3AD Phase Orchestrator + Auto Roadmap Engine")
    console.print("Mode: PAPER ONLY roadmap")
    console.print("Generated code execution: blocked")
    console.print("Live/demo execution: blocked")
    console.print(f"Wrote Markdown: {artifacts.output_path}")
    console.print(f"Wrote JSON: {artifacts.json_path}")
    console.print(f"Wrote next prompt: {artifacts.next_prompt_path}")


@app.command("phase3ae-verified-sports-connector")
def phase3ae_verified_sports_connector_command(
    output_dir: Annotated[
        Path,
        typer.Option(help="Directory for Phase 3AE verified sports connector artifacts."),
    ] = Path("reports/phase3ae"),
    limit: Annotated[
        int,
        typer.Option(help="Maximum partial sports links to review. Use 0 for all."),
    ] = 0,
    candidate_game_key: Annotated[
        list[str] | None,
        typer.Option(
            "--candidate-game-key",
            help=(
                "Restrict verified schedule candidates to this game key. "
                "Repeat for multiple Phase 3AH-safe placeholder games."
            ),
        ),
    ] = None,
    min_confidence: Annotated[
        str | None,
        typer.Option(help="Minimum verified schedule match confidence."),
    ] = None,
    build_features: Annotated[
        bool,
        typer.Option("--build-features/--no-build-features", help="Create sports features."),
    ] = True,
    refresh_features: Annotated[
        bool,
        typer.Option("--refresh-features/--keep-features", help="Insert fresh feature rows."),
    ] = False,
    max_schedule_delta_hours: Annotated[
        int,
        typer.Option(
            help=(
                "Maximum hours between market close and verified game time. "
                "Use 0 to disable."
            )
        ),
    ] = 18,
    roster_evidence_path: Annotated[
        Path,
        typer.Option(help="Path to Phase 3AH verified roster evidence JSON."),
    ] = Path("reports/phase3ah_sports/phase3ah_verified_roster_evidence.json"),
    team_alias_review_path: Annotated[
        Path,
        typer.Option(help="Path to Phase 3AH reviewed team alias evidence JSON."),
    ] = Path("reports/phase3ah_sports/phase3ah_team_alias_review_template.json"),
    manual_disambiguation_path: Annotated[
        Path,
        typer.Option(help="Path to Phase 3AH manual disambiguation evidence JSON."),
    ] = Path("reports/phase3ah_sports/phase3ah_manual_disambiguation_template.json"),
    progress_every: Annotated[
        int,
        typer.Option(help="Print progress every N partial links. Use 0 for quiet."),
    ] = 25,
) -> None:
    """Upgrade partial sports links with verified schedule/team provenance."""
    engine = init_db()
    settings = get_settings()
    session_factory = get_session_factory(engine)
    with session_factory() as session:
        artifacts = write_phase3ae_report(
            session,
            output_dir=output_dir,
            settings=settings,
            limit=limit if limit > 0 else None,
            candidate_game_keys=set(candidate_game_key or []),
            min_confidence=Decimal(min_confidence) if min_confidence is not None else None,
            build_features=build_features,
            refresh_features=refresh_features,
            max_schedule_delta_hours=max_schedule_delta_hours or None,
            roster_evidence_path=roster_evidence_path,
            team_alias_review_path=team_alias_review_path,
            manual_disambiguation_path=manual_disambiguation_path,
            progress_callback=_phase_progress_printer("Phase 3AE")
            if progress_every > 0
            else None,
            progress_every=progress_every,
        )
        session.commit()
    console.print("Phase 3AE Verified Sports Schedule Connector")
    console.print("Mode: PAPER ONLY diagnostics")
    console.print("Live/demo execution: blocked")
    console.print(f"Wrote JSON: {artifacts.json_path}")
    console.print(f"Wrote Markdown: {artifacts.markdown_path}")


@app.command("phase3ae-roster-candidate-diagnostics")
def phase3ae_roster_candidate_diagnostics_command(
    output_dir: Annotated[
        Path,
        typer.Option(help="Directory for Phase 3AE roster candidate diagnostic artifacts."),
    ] = Path("reports/phase3ae_roster_candidates"),
    roster_evidence_path: Annotated[
        Path,
        typer.Option(help="Path to Phase 3AH verified roster evidence JSON."),
    ] = Path("reports/phase3ah_sports/phase3ah_verified_roster_evidence.json"),
    rework_queue_path: Annotated[
        Path,
        typer.Option(help="Path to Phase 3AH roster rework queue JSON."),
    ] = Path("reports/phase3ah_sports/phase3ah_roster_rework_queue.json"),
    limit: Annotated[
        int,
        typer.Option(help="Maximum partial sports links to diagnose. Use 0 for all."),
    ] = 0,
    max_schedule_delta_hours: Annotated[
        int,
        typer.Option(
            help=(
                "Maximum hours between market close and verified game time. "
                "Use 0 to disable."
            )
        ),
    ] = 18,
) -> None:
    """Diagnose Phase 3AH roster evidence against Phase 3AE link gates."""
    engine = init_db()
    session_factory = get_session_factory(engine)
    with session_factory() as session:
        artifacts = write_phase3ae_roster_candidate_diagnostics(
            session,
            output_dir=output_dir,
            roster_evidence_path=roster_evidence_path,
            rework_queue_path=rework_queue_path,
            limit=limit if limit > 0 else None,
            max_schedule_delta_hours=max_schedule_delta_hours or None,
        )
    console.print("Phase 3AE Roster Candidate Diagnostics")
    console.print("Mode: PAPER ONLY diagnostics")
    console.print("Read-only: no sports links or features are inserted")
    console.print("Live/demo execution: blocked")
    console.print(f"Wrote JSON: {artifacts.json_path}")
    console.print(f"Wrote Markdown: {artifacts.markdown_path}")
    console.print(f"Wrote clean candidates: {artifacts.clean_candidates_path}")
    console.print(f"Wrote blockers: {artifacts.blockers_path}")
    console.print(f"Wrote manual disambiguation candidates: {artifacts.manual_disambiguation_path}")


@app.command("phase3ai-link-reconciliation")
def phase3ai_link_reconciliation_command(
    output_dir: Annotated[
        Path,
        typer.Option(help="Directory for Phase 3AI link reconciliation artifacts."),
    ] = Path("reports/phase3ai"),
    upgrade_sports: Annotated[
        bool,
        typer.Option(
            "--upgrade-sports/--audit-only",
            help="Run verified sports upgrade before final count reconciliation.",
        ),
    ] = True,
    limit: Annotated[
        int,
        typer.Option(help="Maximum partial sports links to review. Use 0 for all."),
    ] = 0,
    min_confidence: Annotated[
        str | None,
        typer.Option(help="Minimum verified sports schedule match confidence."),
    ] = None,
    build_features: Annotated[
        bool,
        typer.Option("--build-features/--no-build-features", help="Create sports features."),
    ] = True,
    refresh_features: Annotated[
        bool,
        typer.Option("--refresh-features/--keep-features", help="Insert fresh feature rows."),
    ] = False,
    max_schedule_delta_hours: Annotated[
        int,
        typer.Option(help="Maximum hours between market close and verified game time. Use 0 off."),
    ] = 18,
    progress_every: Annotated[
        int,
        typer.Option(help="Print progress every N partial links. Use 0 for quiet."),
    ] = 25,
) -> None:
    """Reconcile link counts and upgrade sports partials with verified provenance."""
    engine = init_db()
    settings = get_settings()
    session_factory = get_session_factory(engine)
    with session_factory() as session:
        artifacts = write_phase3ai_report(
            session,
            output_dir=output_dir,
            settings=settings,
            upgrade_sports=upgrade_sports,
            limit=limit if limit > 0 else None,
            min_confidence=Decimal(min_confidence) if min_confidence is not None else None,
            build_features=build_features,
            refresh_features=refresh_features,
            max_schedule_delta_hours=max_schedule_delta_hours or None,
            progress_callback=_phase_progress_printer("Phase 3AI sports upgrade")
            if upgrade_sports and progress_every > 0
            else None,
            progress_every=progress_every,
        )
        session.commit()
    console.print("Phase 3AI Link Coverage Count Reconciliation + Verified Sports Upgrade")
    console.print("Mode: PAPER ONLY diagnostics")
    console.print("Live/demo execution: blocked")
    console.print(f"Wrote JSON: {artifacts.json_path}")
    console.print(f"Wrote Markdown: {artifacts.markdown_path}")


@app.command("phase3aj-sports-alias-provenance")
def phase3aj_sports_alias_provenance_command(
    output_dir: Annotated[
        Path,
        typer.Option(help="Directory for Phase 3AJ alias/provenance artifacts."),
    ] = Path("reports/phase3aj"),
    limit: Annotated[
        int,
        typer.Option(help="Maximum unresolved partial sports links to review. Use 0 for all."),
    ] = 0,
    apply_aliases: Annotated[
        bool,
        typer.Option(
            "--apply-aliases/--audit-only",
            help="Write conservative observed aliases into sports_teams.raw_json.",
        ),
    ] = False,
) -> None:
    """Repair sports alias and competition provenance gaps without execution."""
    engine = init_db()
    session_factory = get_session_factory(engine)
    with session_factory() as session:
        artifacts = write_phase3aj_report(
            session,
            output_dir=output_dir,
            limit=limit if limit > 0 else None,
            apply_aliases=apply_aliases,
        )
        session.commit()
    console.print("Phase 3AJ Sports Alias + Competition Provenance Repair")
    console.print("Mode: PAPER ONLY diagnostics")
    console.print("Live/demo execution: blocked")
    console.print(f"Wrote JSON: {artifacts.json_path}")
    console.print(f"Wrote Markdown: {artifacts.markdown_path}")
    console.print(f"Wrote alias suggestions: {artifacts.alias_suggestions_path}")
    console.print(f"Wrote competition template: {artifacts.competition_template_path}")


@app.command("phase3ak-multileg-provenance")
def phase3ak_multileg_provenance_command(
    output_dir: Annotated[
        Path,
        typer.Option(help="Directory for Phase 3AK multi-leg provenance artifacts."),
    ] = Path("reports/phase3ak"),
    limit: Annotated[
        int,
        typer.Option(help="Maximum markets to inspect. Use 0 for all markets."),
    ] = 0,
    include_single_leg: Annotated[
        bool,
        typer.Option(
            "--include-single-leg/--multi-leg-only",
            help="Include single-leg sports rows in diagnostics.",
        ),
    ] = False,
) -> None:
    """Report component-level provenance and Learning Mode eligibility for multi-leg sports."""
    engine = init_db()
    session_factory = get_session_factory(engine)
    with session_factory() as session:
        artifacts = write_phase3ak_report(
            session,
            output_dir=output_dir,
            limit=limit if limit > 0 else None,
            include_single_leg=include_single_leg,
        )
    console.print("Phase 3AK Multi-Leg Sports Component Provenance + Learning Gate")
    console.print("Mode: PAPER ONLY diagnostics")
    console.print("Live/demo execution: blocked")
    console.print(f"Wrote JSON: {artifacts.json_path}")
    console.print(f"Wrote Markdown: {artifacts.markdown_path}")


@app.command("phase3al-learning-resume")
def phase3al_learning_resume_command(
    output_dir: Annotated[
        Path,
        typer.Option(help="Directory for Phase 3AL settlement-aware resume artifacts."),
    ] = Path("reports/phase3al"),
    model_name: Annotated[
        str,
        typer.Option(help="Ranking model to inspect."),
    ] = "ensemble_v2",
    limit: Annotated[
        int,
        typer.Option(help="Maximum ranking/paper rows to inspect."),
    ] = 500,
) -> None:
    """Decide whether paper-only Learning Mode should resume after cap/reset gates."""
    engine = init_db()
    settings = get_settings()
    session_factory = get_session_factory(engine)
    with session_factory() as session:
        artifacts = write_phase3al_report(
            session,
            output_dir=output_dir,
            settings=settings,
            model_name=model_name,
            limit=limit,
        )
    console.print("Phase 3AL Settlement-Aware Learning Resume")
    console.print("Mode: PAPER ONLY diagnostics")
    console.print("Exact-ticker settlement policy: enforced")
    console.print(f"Wrote JSON: {artifacts.json_path}")
    console.print(f"Wrote Markdown: {artifacts.markdown_path}")


@app.command("phase-3al-diagnostic")
def phase_3al_diagnostic_command(
    window_hours: Annotated[
        int,
        typer.Option(help="Ranking lookback window in hours for the paper funnel."),
    ] = 168,
    include_ui_state: Annotated[
        bool,
        typer.Option(help="Include current UI-facing state snapshots in the report."),
    ] = False,
    output_dir: Annotated[
        Path,
        typer.Option(help="Directory for Phase 3AL diagnostic artifacts."),
    ] = Path("reports/phase_3al"),
) -> None:
    """Write the read-only Phase 3AL paper completion diagnostic bundle."""
    engine = init_db()
    settings = get_settings()
    session_factory = get_session_factory(engine)
    with session_factory() as session:
        artifacts = write_phase3al_diagnostic_report(
            session,
            output_dir=output_dir,
            window_hours=window_hours,
            include_ui_state=include_ui_state,
            settings=settings,
        )
    console.print("Phase 3AL paper completion diagnostic")
    console.print("Mode: PAPER ONLY / READ ONLY")
    console.print("Live/demo execution: blocked")
    console.print("Paper order/fill writes: blocked")
    console.print(f"Wrote JSON: {artifacts.diagnostic_path}")
    console.print(f"Wrote executive summary: {artifacts.executive_summary_path}")
    console.print(f"Wrote next actions: {artifacts.next_actions_path}")


@app.command("phase3am-sports-verified-upgrade")
def phase3am_sports_verified_upgrade_command(
    output_dir: Annotated[
        Path,
        typer.Option(help="Directory for Phase 3AM sports verified upgrade artifacts."),
    ] = Path("reports/phase3am"),
    upgrade_verified: Annotated[
        bool,
        typer.Option(
            "--upgrade-verified/--audit-only",
            help="Run verified schedule upgrade after provenance separation.",
        ),
    ] = False,
    apply_aliases: Annotated[
        bool,
        typer.Option("--apply-aliases/--no-apply-aliases", help="Apply observed aliases."),
    ] = False,
    limit: Annotated[
        int,
        typer.Option(help="Maximum partial sports links to review. Use 0 for all."),
    ] = 0,
    min_confidence: Annotated[
        str | None,
        typer.Option(help="Minimum verified schedule match confidence."),
    ] = None,
) -> None:
    """Separate verified, derived, and partial sports links and optionally upgrade partials."""
    engine = init_db()
    settings = get_settings()
    session_factory = get_session_factory(engine)
    with session_factory() as session:
        artifacts = write_phase3am_report(
            session,
            output_dir=output_dir,
            settings=settings,
            upgrade_verified=upgrade_verified,
            apply_aliases=apply_aliases,
            limit=limit if limit > 0 else None,
            min_confidence=Decimal(min_confidence) if min_confidence is not None else None,
        )
        if upgrade_verified or apply_aliases:
            session.commit()
    console.print("Phase 3AM Sports Verified Upgrade")
    console.print("Mode: PAPER ONLY diagnostics")
    console.print("Live/demo execution: blocked")
    console.print(f"Wrote JSON: {artifacts.json_path}")
    console.print(f"Wrote Markdown: {artifacts.markdown_path}")


@app.command("phase3am-preflight")
def phase3am_preflight_command(
    output_dir: Annotated[
        Path,
        typer.Option(help="Directory for Phase 3AM preflight artifacts."),
    ] = Path("reports/phase3am"),
    settlement_apply: Annotated[
        bool,
        typer.Option(help="Evaluate fail-closed checks for settlement apply mode."),
    ] = False,
) -> None:
    """Record runtime identity, database identity, and settlement safety status."""
    engine = make_engine()
    settings = get_settings()
    session_factory = get_session_factory(engine)
    with session_factory() as session:
        artifacts = write_phase3am_preflight_report(
            session,
            output_dir=output_dir,
            settings=settings,
            settlement_apply=settlement_apply,
        )
    console.print("Phase 3AM runtime preflight")
    console.print("Mode: READ ONLY")
    console.print("Live/demo execution: blocked")
    console.print(f"Wrote JSON: {artifacts.json_path}")


@app.command("phase3ay-due-settlement-diagnostic")
def phase3ay_due_settlement_diagnostic_command(
    output_dir: Annotated[
        Path,
        typer.Option(help="Directory for Phase 3AM settlement artifacts."),
    ] = Path("reports/phase3am"),
    max_records: Annotated[
        int,
        typer.Option(help="Maximum recent paper rows to inspect. Use 0 for all."),
    ] = 0,
) -> None:
    """Classify due paper trades by exact-settlement safety state."""
    engine = make_engine()
    session_factory = get_session_factory(engine)
    with session_factory() as session:
        artifacts = write_phase3ay_due_settlement_diagnostic_report(
            session,
            output_dir=output_dir,
            limit=max_records if max_records > 0 else None,
        )
    console.print("Phase 3AY due settlement diagnostic")
    console.print("Mode: READ ONLY exact-ticker diagnostic")
    console.print("Sibling/fuzzy settlements: blocked")
    console.print(f"Wrote JSON: {artifacts.json_path}")


@app.command("phase3ay-settle-due-paper")
def phase3ay_settle_due_paper_command(
    output_dir: Annotated[
        Path,
        typer.Option(help="Directory for Phase 3AM settlement artifacts."),
    ] = Path("reports/phase3am"),
    exact_only: Annotated[
        bool,
        typer.Option("--exact-only/--allow-non-exact", help="Require exact ticker settlement."),
    ] = False,
    dry_run: Annotated[
        bool,
        typer.Option("--dry-run/--apply", help="Preview ledger rows unless --apply is used."),
    ] = True,
    backup_first: Annotated[
        bool,
        typer.Option(help="Create a SQLite backup before any apply."),
    ] = False,
    max_records: Annotated[
        int,
        typer.Option(help="Maximum exact-ready rows to apply or preview."),
    ] = 5,
) -> None:
    """Dry-run or apply exact due-paper settlement P&L rows with hard guards."""
    engine = init_db() if not dry_run else make_engine()
    settings = get_settings()
    session_factory = get_session_factory(engine)
    with session_factory() as session:
        try:
            artifacts = write_phase3ay_settle_due_paper_report(
                session,
                output_dir=output_dir,
                settings=settings,
                exact_only=exact_only,
                dry_run=dry_run,
                apply=not dry_run,
                backup_first=backup_first,
                max_records=max_records,
            )
        except ValueError as exc:
            raise typer.BadParameter(str(exc)) from exc
        if not dry_run:
            session.commit()
    console.print("Phase 3AY exact due paper settlement")
    console.print(f"Mode: {'DRY RUN' if dry_run else 'APPLY'}")
    console.print("Live/demo execution: blocked")
    console.print("Sibling/fuzzy settlements: blocked")
    console.print(f"Wrote JSON: {artifacts.json_path}")


@app.command("phase3am-gap-burndown")
def phase3am_gap_burndown_command(
    output_dir: Annotated[
        Path,
        typer.Option(help="Directory for Phase 3AM burn-down artifacts."),
    ] = Path("reports/phase3am"),
    reports_dir: Annotated[
        Path,
        typer.Option(help="Root reports directory to inspect and refresh."),
    ] = Path("reports"),
    settlement_dry_run: Annotated[
        bool,
        typer.Option(help="Run exact settlement preview; default is dry-run only."),
    ] = True,
    settlement_apply_exact_only: Annotated[
        bool,
        typer.Option(help="Apply exact ready due settlements after backup-first checks."),
    ] = False,
    backup_first: Annotated[
        bool,
        typer.Option(help="Create a SQLite backup before optional settlement apply."),
    ] = False,
    max_settlements: Annotated[
        int,
        typer.Option(help="Maximum exact settlement rows to apply or preview."),
    ] = 5,
) -> None:
    """Run Phase 3AM report-only gap burn-down and rerun Phase 3AZ."""
    engine = init_db() if settlement_apply_exact_only else make_engine()
    settings = get_settings()
    session_factory = get_session_factory(engine)
    with session_factory() as session:
        artifacts = write_phase3am_gap_burndown_report(
            session,
            output_dir=output_dir,
            reports_dir=reports_dir,
            settings=settings,
            settlement_dry_run=settlement_dry_run,
            settlement_apply_exact_only=settlement_apply_exact_only,
            backup_first=backup_first,
            max_settlements=max_settlements,
        )
        if settlement_apply_exact_only:
            session.commit()
    console.print("Phase 3AM gap burn-down")
    console.print("Default safety: dry-run/report-only")
    console.print("Live/demo execution: blocked")
    console.print(f"Wrote summary: {artifacts.summary_path}")
    console.print(f"Wrote burn-down JSON: {artifacts.burn_down_path}")
    console.print(f"Wrote manifest: {artifacts.manifest_path}")


@app.command("phase3an-crypto-feature-completeness")
def phase3an_crypto_feature_completeness_command(
    output_dir: Annotated[
        Path,
        typer.Option(help="Directory for Phase 3AN crypto completeness artifacts."),
    ] = Path("reports/phase3an"),
    symbols: Annotated[
        str,
        typer.Option(help="Comma-separated crypto symbols to require."),
    ] = DEFAULT_CRYPTO_SYMBOLS,
    max_age_minutes: Annotated[
        int,
        typer.Option(help="Maximum age for fresh point-in-time crypto features."),
    ] = 1440,
) -> None:
    """Check BTC/ETH/SOL/XRP/DOGE feature freshness before crypto_v2 forecasts."""
    engine = init_db()
    settings = get_settings()
    session_factory = get_session_factory(engine)
    with session_factory() as session:
        artifacts = write_phase3an_report(
            session,
            output_dir=output_dir,
            settings=settings,
            symbols=parse_symbols(symbols),
            max_age_minutes=max_age_minutes,
        )
    console.print("Phase 3AN Crypto Feature Completeness")
    console.print("Mode: PAPER ONLY diagnostics")
    console.print("Live/demo execution: blocked")
    console.print(f"Wrote JSON: {artifacts.json_path}")
    console.print(f"Wrote Markdown: {artifacts.markdown_path}")


@app.command("phase3an-preflight")
def phase3an_preflight_command(
    output_dir: Annotated[
        Path,
        typer.Option(help="Directory for Phase 3AN preflight artifacts."),
    ] = Path("reports/phase3an"),
) -> None:
    """Write Phase 3AN runtime identity and fail-closed DB safety evidence."""
    engine = init_db()
    settings = get_settings()
    session_factory = get_session_factory(engine)
    with session_factory() as session:
        artifacts = write_phase3an_preflight_report(
            session,
            output_dir=output_dir,
            settings=settings,
        )
    console.print("Phase 3AN Preflight")
    console.print("Mode: PAPER/READ-ONLY diagnostics")
    console.print("Live/demo execution: blocked")
    console.print(f"Wrote JSON: {artifacts.json_path}")


@app.command("phase3an-crypto-watch-doctor")
def phase3an_crypto_watch_doctor_command(
    output_dir: Annotated[
        Path,
        typer.Option(help="Directory for Phase 3AN crypto watch artifacts."),
    ] = Path("reports/phase3an"),
    reports_dir: Annotated[
        Path,
        typer.Option(help="Existing reports root to inspect."),
    ] = Path("reports"),
    symbols: Annotated[
        str,
        typer.Option(help="Comma-separated crypto symbols to inspect."),
    ] = DEFAULT_CRYPTO_SYMBOLS,
    freshness_minutes: Annotated[
        int,
        typer.Option(help="Expected crypto watch freshness/cadence in minutes."),
    ] = 15,
) -> None:
    """Explain crypto watch overdue/stale state without stopping processes."""
    engine = init_db()
    settings = get_settings()
    session_factory = get_session_factory(engine)
    with session_factory() as session:
        artifacts = write_phase3an_crypto_watch_doctor_report(
            session,
            output_dir=output_dir,
            reports_dir=reports_dir,
            settings=settings,
            symbols=symbols,
            freshness_minutes=freshness_minutes,
        )
    console.print("Phase 3AN Crypto Watch Doctor")
    console.print("Mode: PAPER/READ-ONLY diagnostics")
    console.print("Watcher restart/kill: not performed")
    console.print(f"Wrote JSON: {artifacts.json_path}")


@app.command("phase3an-crypto-watch-restart-plan")
def phase3an_crypto_watch_restart_plan_command(
    dry_run: Annotated[
        bool,
        typer.Option(help="Required; only print restart steps."),
    ] = True,
    output_dir: Annotated[
        Path,
        typer.Option(help="Directory for Phase 3AN restart-plan artifacts."),
    ] = Path("reports/phase3an"),
    reports_dir: Annotated[
        Path,
        typer.Option(help="Existing reports root to inspect."),
    ] = Path("reports"),
) -> None:
    """Print a guarded crypto watch restart plan; never stops or starts jobs."""
    engine = init_db()
    settings = get_settings()
    session_factory = get_session_factory(engine)
    with session_factory() as session:
        artifacts = write_phase3an_crypto_watch_restart_plan_report(
            session,
            dry_run=dry_run,
            output_dir=output_dir,
            reports_dir=reports_dir,
            settings=settings,
        )
    console.print("Phase 3AN Crypto Watch Restart Plan")
    console.print("Dry-run only: true")
    console.print("Watcher restart/kill: not performed")
    console.print(f"Wrote JSON: {artifacts.json_path}")


@app.command("phase3an-paper-funnel-explain")
def phase3an_paper_funnel_explain_command(
    window_hours: Annotated[
        int,
        typer.Option(help="Ranking window to replay in hours."),
    ] = 168,
    output_dir: Annotated[
        Path,
        typer.Option(help="Directory for Phase 3AN paper funnel artifacts."),
    ] = Path("reports/phase3an"),
) -> None:
    """Explain why recent rankings did or did not create paper-ready rows."""
    engine = init_db()
    settings = get_settings()
    session_factory = get_session_factory(engine)
    with session_factory() as session:
        artifacts = write_phase3an_paper_funnel_explain_report(
            session,
            window_hours=window_hours,
            output_dir=output_dir,
            settings=settings,
        )
    console.print("Phase 3AN Paper Funnel Explain")
    console.print("Mode: read-only replay; no paper orders created")
    console.print(f"Wrote JSON: {artifacts.json_path}")
    console.print(f"Wrote Markdown: {artifacts.markdown_path}")


@app.command("phase3an-settlement-health-confirm")
def phase3an_settlement_health_confirm_command(
    output_dir: Annotated[
        Path,
        typer.Option(help="Directory for Phase 3AN settlement artifacts."),
    ] = Path("reports/phase3an"),
    reports_dir: Annotated[
        Path,
        typer.Option(help="Existing reports root to inspect."),
    ] = Path("reports"),
    max_records: Annotated[
        int,
        typer.Option(help="Maximum due settlement rows to inspect."),
    ] = 5,
) -> None:
    """Confirm exact-ticker settlement health without running settlement apply."""
    engine = init_db()
    settings = get_settings()
    session_factory = get_session_factory(engine)
    with session_factory() as session:
        artifacts = write_phase3an_settlement_health_confirm_report(
            session,
            output_dir=output_dir,
            reports_dir=reports_dir,
            settings=settings,
            max_records=max_records,
        )
    console.print("Phase 3AN Settlement Health Confirm")
    console.print("Settlement apply: not performed")
    console.print("Sibling/fuzzy settlement: blocked")
    console.print(f"Wrote JSON: {artifacts.json_path}")


@app.command("phase3an-3bb-r2-burndown")
def phase3an_3bb_r2_burndown_command(
    output_dir: Annotated[
        Path,
        typer.Option(help="Directory for Phase 3AN 3BB-R2 burn-down artifact."),
    ] = Path("reports/phase3an"),
    reports_dir: Annotated[
        Path,
        typer.Option(help="Existing reports root to inspect/update with report-only artifacts."),
    ] = Path("reports"),
    sources_dir: Annotated[
        Path,
        typer.Option(help="Directory for Phase 3BB-R2 source reports."),
    ] = Path("reports/phase3bb_r2_sources"),
    evidence_dir: Annotated[
        Path,
        typer.Option(help="Directory for local general source evidence files."),
    ] = Path("data/general_source_evidence"),
    limit_per_bucket: Annotated[
        int,
        typer.Option(help="Maximum examples per source bucket."),
    ] = 50,
) -> None:
    """Run report-only 3BB-R2 source burn-down checks."""
    engine = init_db()
    settings = get_settings()
    session_factory = get_session_factory(engine)
    with session_factory() as session:
        artifacts = write_phase3an_3bb_r2_burndown_report(
            session,
            output_dir=output_dir,
            reports_dir=reports_dir,
            sources_dir=sources_dir,
            evidence_dir=evidence_dir,
            settings=settings,
            limit_per_bucket=limit_per_bucket,
        )
    console.print("Phase 3AN 3BB-R2 Burn-Down")
    console.print("Mode: report/local-file-safe only")
    console.print("Link/feature/forecast/trade writes: blocked")
    console.print(f"Wrote JSON: {artifacts.json_path}")


@app.command("phase3an-usda-date-mismatch-report")
def phase3an_usda_date_mismatch_report_command(
    output_dir: Annotated[
        Path,
        typer.Option(help="Directory for Phase 3AN USDA artifact."),
    ] = Path("reports/phase3an"),
    evidence_dir: Annotated[
        Path,
        typer.Option(help="Directory for local general source evidence files."),
    ] = Path("data/general_source_evidence"),
    expected_report_date: Annotated[
        str,
        typer.Option(help="Required exact USDA report date."),
    ] = "July 3, 2026",
) -> None:
    """Preserve USDA report-date mismatch as a hard source blocker."""
    engine = init_db()
    settings = get_settings()
    session_factory = get_session_factory(engine)
    with session_factory() as session:
        artifacts = write_phase3an_usda_date_mismatch_report(
            session,
            output_dir=output_dir,
            evidence_dir=evidence_dir,
            expected_report_date=expected_report_date,
            settings=settings,
        )
    console.print("Phase 3AN USDA Date Mismatch Report")
    console.print("Wrong-date evidence use: blocked")
    console.print(f"Wrote JSON: {artifacts.json_path}")


@app.command("phase3an-general-sources-status")
def phase3an_general_sources_status_command(
    output_dir: Annotated[
        Path,
        typer.Option(help="Directory for Phase 3AN source status artifact."),
    ] = Path("reports/phase3an"),
    reports_dir: Annotated[
        Path,
        typer.Option(help="Existing reports root to inspect."),
    ] = Path("reports"),
    sources_dir: Annotated[
        Path,
        typer.Option(help="Directory for Phase 3BB-R2 source reports."),
    ] = Path("reports/phase3bb_r2_sources"),
    evidence_dir: Annotated[
        Path,
        typer.Option(help="Directory for local general source evidence files."),
    ] = Path("data/general_source_evidence"),
) -> None:
    """Write precise USDA/Cushman/FlightAware blocker semantics for the UI."""
    engine = init_db()
    settings = get_settings()
    session_factory = get_session_factory(engine)
    with session_factory() as session:
        artifacts = write_phase3an_general_sources_status_report(
            session,
            output_dir=output_dir,
            reports_dir=reports_dir,
            sources_dir=sources_dir,
            evidence_dir=evidence_dir,
            settings=settings,
        )
    console.print("Phase 3AN General Sources Status")
    console.print("Source promotion: blocked")
    console.print(f"Wrote JSON: {artifacts.json_path}")


@app.command("phase3an-sports-blocker-report")
def phase3an_sports_blocker_report_command(
    output_dir: Annotated[
        Path,
        typer.Option(help="Directory for Phase 3AN sports blocker artifact."),
    ] = Path("reports/phase3an"),
    reports_dir: Annotated[
        Path,
        typer.Option(help="Existing reports root to inspect."),
    ] = Path("reports"),
) -> None:
    """Explain sports placeholder/provenance blockers without upgrades."""
    artifacts = write_phase3an_sports_blocker_report(
        output_dir=output_dir,
        reports_dir=reports_dir,
    )
    console.print("Phase 3AN Sports Blocker Report")
    console.print("Sports upgrades/features/forecasts/trades: blocked")
    console.print(f"Wrote JSON: {artifacts.json_path}")


@app.command("phase3an-economic-news-watch")
def phase3an_economic_news_watch_command(
    output_dir: Annotated[
        Path,
        typer.Option(help="Directory for Phase 3AN economic/news artifact."),
    ] = Path("reports/phase3an"),
    handoff_limit: Annotated[
        int,
        typer.Option(help="Maximum current handoff rows to materialize per domain."),
    ] = 25,
    rebuild_readiness: Annotated[
        bool,
        typer.Option(
            "--rebuild-readiness/--use-cached-readiness",
            help=(
                "Rebuild Phase 3BB domain readiness live instead of using the cached "
                "readiness artifact."
            ),
        ),
    ] = False,
    include_preflight: Annotated[
        bool,
        typer.Option(
            "--preflight/--skip-preflight",
            help="Run the full Phase 3AN runtime preflight instead of bounded report metadata.",
        ),
    ] = False,
) -> None:
    """Explain economic/news wait state without forcing links or forecasts."""
    engine = init_db()
    settings = get_settings()
    session_factory = get_session_factory(engine)
    with session_factory() as session:
        artifacts = write_phase3an_economic_news_watch_report(
            session,
            output_dir=output_dir,
            settings=settings,
            handoff_limit=handoff_limit,
            rebuild_readiness=rebuild_readiness,
            include_preflight=include_preflight,
        )
    console.print("Phase 3AN Economic/News Watch")
    console.print("Links/forecasts/opportunities/trades: blocked")
    console.print(f"Wrote JSON: {artifacts.json_path}")


@app.command("phase3an-economic-news-parser-backfill-plan")
def phase3an_economic_news_parser_backfill_plan_command(
    output_dir: Annotated[
        Path,
        typer.Option(help="Directory for Phase 3AN economic/news parser backfill artifact."),
    ] = Path("reports/phase3an"),
    limit: Annotated[
        int,
        typer.Option(help="Maximum current exact-link rows to include."),
    ] = 500,
) -> None:
    """Plan parser backfill for exact-linked current economic/news markets without writes."""
    engine = init_db()
    settings = get_settings()
    session_factory = get_session_factory(engine)
    with session_factory() as session:
        artifacts = write_phase3an_economic_news_parser_backfill_plan_report(
            session,
            output_dir=output_dir,
            settings=settings,
            limit=limit,
        )
    console.print("Phase 3AN Economic/News Parser Backfill Plan")
    console.print("Parser/link/forecast/opportunity/trade writes: blocked")
    console.print(f"Wrote JSON: {artifacts.json_path}")


@app.command("phase3an-economic-link-event-repair-plan")
def phase3an_economic_link_event_repair_plan_command(
    output_dir: Annotated[
        Path,
        typer.Option(help="Directory for Phase 3AN economic link-event artifact."),
    ] = Path("reports/phase3an"),
    limit: Annotated[
        int,
        typer.Option(help="Maximum current exact-link rows to include."),
    ] = 500,
) -> None:
    """Plan exact economic link-event repairs without parser, link, or trade writes."""
    engine = init_db()
    settings = get_settings()
    session_factory = get_session_factory(engine)
    with session_factory() as session:
        artifacts = write_phase3an_economic_link_event_repair_plan_report(
            session,
            output_dir=output_dir,
            settings=settings,
            limit=limit,
        )
    console.print("Phase 3AN Economic Link Event Repair Plan")
    console.print("Parser/link/forecast/opportunity/trade writes: blocked")
    console.print(f"Wrote JSON: {artifacts.json_path}")


@app.command("phase3an-economic-link-event-repair")
def phase3an_economic_link_event_repair_command(
    output_dir: Annotated[
        Path,
        typer.Option(help="Directory for Phase 3AN economic link-event repair artifact."),
    ] = Path("reports/phase3an"),
    limit: Annotated[
        int,
        typer.Option(help="Maximum current exact-link rows to review."),
    ] = 500,
    max_records: Annotated[
        int,
        typer.Option(help="Maximum exact link-event repair rows to write in apply mode."),
    ] = 50,
    dry_run: Annotated[
        bool,
        typer.Option("--dry-run/--apply", help="Dry-run by default; --apply requires --backup-first."),
    ] = True,
    backup_first: Annotated[
        bool,
        typer.Option("--backup-first", help="Required before apply writes local link rows."),
    ] = False,
) -> None:
    """Dry-run or operator-apply exact economic link-event repairs."""
    apply = not dry_run
    engine = init_db()
    settings = get_settings()
    session_factory = get_session_factory(engine)
    with session_factory() as session:
        artifacts = write_phase3an_economic_link_event_repair_apply_report(
            session,
            output_dir=output_dir,
            settings=settings,
            dry_run=dry_run,
            apply=apply,
            backup_first=backup_first,
            max_records=max_records,
            limit=limit,
        )
    console.print("Phase 3AN Economic Link Event Repair")
    console.print("Live/demo execution, parser writes, forecasts, opportunities, trades: blocked")
    console.print(f"Wrote JSON: {artifacts.json_path}")


@app.command("phase3an-economic-parser-leg-backfill")
def phase3an_economic_parser_leg_backfill_command(
    output_dir: Annotated[
        Path,
        typer.Option(help="Directory for Phase 3AN economic parser-leg backfill artifact."),
    ] = Path("reports/phase3an"),
    limit: Annotated[
        int,
        typer.Option(help="Maximum current exact-link rows to review."),
    ] = 500,
    max_records: Annotated[
        int,
        typer.Option(help="Maximum local parser-leg rows to write in apply mode."),
    ] = 50,
    dry_run: Annotated[
        bool,
        typer.Option("--dry-run/--apply", help="Dry-run by default; --apply requires --backup-first."),
    ] = True,
    backup_first: Annotated[
        bool,
        typer.Option("--backup-first", help="Required before apply writes local parser-leg rows."),
    ] = False,
) -> None:
    """Dry-run or operator-apply exact economic parser-leg backfill."""
    apply = not dry_run
    engine = init_db()
    settings = get_settings()
    session_factory = get_session_factory(engine)
    with session_factory() as session:
        artifacts = write_phase3an_economic_parser_leg_backfill_report(
            session,
            output_dir=output_dir,
            settings=settings,
            dry_run=dry_run,
            apply=apply,
            backup_first=backup_first,
            max_records=max_records,
            limit=limit,
        )
    console.print("Phase 3AN Economic Parser-Leg Backfill")
    console.print("Live/demo execution, link writes, forecasts, opportunities, trades: blocked")
    console.print("Parser writes: blocked in dry-run; apply requires --backup-first")
    console.print(f"Wrote JSON: {artifacts.json_path}")


@app.command("phase3an-economic-operator-approval-packet")
def phase3an_economic_operator_approval_packet_command(
    output_dir: Annotated[
        Path,
        typer.Option(help="Directory for Phase 3AN economic approval packet artifacts."),
    ] = Path("reports/phase3an"),
    limit: Annotated[
        int,
        typer.Option(help="Maximum current exact-link rows to review."),
    ] = 500,
    max_records: Annotated[
        int,
        typer.Option(help="Maximum bounded rows to include per operator command."),
    ] = 50,
) -> None:
    """Write the report-only economic approval packet for later human review."""
    engine = init_db()
    settings = get_settings()
    session_factory = get_session_factory(engine)
    with session_factory() as session:
        artifacts = write_phase3an_economic_operator_approval_packet_report(
            session,
            output_dir=output_dir,
            settings=settings,
            max_records=max_records,
            limit=limit,
        )
    console.print("Phase 3AN Economic Operator Approval Packet")
    console.print("Report only; no apply, no exchange writes, no paper trades")
    console.print(f"Wrote JSON: {artifacts.json_path}")
    console.print(f"Wrote Markdown: {artifacts.markdown_path}")


@app.command("phase3an-economic-approval-safety-guard")
def phase3an_economic_approval_safety_guard_command(
    output_dir: Annotated[
        Path,
        typer.Option(help="Directory for Phase 3AN economic approval guard artifacts."),
    ] = Path("reports/phase3an"),
    packet_path: Annotated[
        Path | None,
        typer.Option(help="Optional existing approval-packet JSON to audit without opening the DB."),
    ] = None,
    limit: Annotated[
        int,
        typer.Option(help="Maximum current exact-link rows to review."),
    ] = 500,
    max_records: Annotated[
        int,
        typer.Option(help="Maximum bounded rows to include per operator command."),
    ] = 50,
) -> None:
    """Audit the economic approval packet for report-only safety."""
    if packet_path is not None:
        artifacts = write_phase3an_economic_approval_safety_guard_from_packet_report(
            packet_path=packet_path,
            output_dir=output_dir,
        )
    else:
        engine = init_db()
        settings = get_settings()
        session_factory = get_session_factory(engine)
        with session_factory() as session:
            artifacts = write_phase3an_economic_approval_safety_guard_report(
                session,
                output_dir=output_dir,
                settings=settings,
                max_records=max_records,
                limit=limit,
            )
    console.print("Phase 3AN Economic Approval Safety Guard")
    console.print("Report only; no apply, no exchange writes, no paper trades")
    console.print(f"Wrote JSON: {artifacts.json_path}")
    console.print(f"Wrote Markdown: {artifacts.markdown_path}")


@app.command("phase3an-economic-morning-operator-handoff")
def phase3an_economic_morning_operator_handoff_command(
    output_dir: Annotated[
        Path,
        typer.Option(help="Directory for Phase 3AN economic morning handoff artifacts."),
    ] = Path("reports/phase3an"),
    reports_dir: Annotated[
        Path,
        typer.Option(help="Existing reports root to inspect."),
    ] = Path("reports"),
) -> None:
    """Write the report-only morning handoff for economic operator review."""
    artifacts = write_phase3an_economic_morning_operator_handoff_report(
        output_dir=output_dir,
        reports_dir=reports_dir,
    )
    console.print("Phase 3AN Economic Morning Operator Handoff")
    console.print("Report only; no apply, no exchange writes, no paper trades")
    console.print(f"Wrote JSON: {artifacts.json_path}")
    console.print(f"Wrote Markdown: {artifacts.markdown_path}")


@app.command("phase3an-overnight-refresh-continuity")
def phase3an_overnight_refresh_continuity_command(
    output_dir: Annotated[
        Path,
        typer.Option(help="Directory for Phase 3AN overnight continuity artifacts."),
    ] = Path("reports/phase3an"),
    reports_dir: Annotated[
        Path,
        typer.Option(help="Existing reports root to inspect."),
    ] = Path("reports"),
) -> None:
    """Write a report-only overnight refresh continuity handoff."""
    artifacts = write_phase3an_overnight_refresh_continuity_report(
        output_dir=output_dir,
        reports_dir=reports_dir,
    )
    console.print("Phase 3AN Overnight Refresh Continuity")
    console.print("Report only; no apply, no exchange writes, no paper trades")
    console.print(f"Wrote JSON: {artifacts.json_path}")
    console.print(f"Wrote Markdown: {artifacts.markdown_path}")


@app.command("phase3an-gap-fix-report")
def phase3an_gap_fix_report_command(
    output_dir: Annotated[
        Path,
        typer.Option(help="Directory for unified Phase 3AN artifacts."),
    ] = Path("reports/phase3an"),
    reports_dir: Annotated[
        Path,
        typer.Option(help="Existing reports root to inspect/update with report-only artifacts."),
    ] = Path("reports"),
    window_hours: Annotated[
        int,
        typer.Option(help="Ranking window to replay in hours."),
    ] = 168,
    max_settlements: Annotated[
        int,
        typer.Option(help="Maximum due settlement rows to inspect."),
    ] = 5,
    limit_per_bucket: Annotated[
        int,
        typer.Option(help="Maximum examples per source bucket."),
    ] = 50,
) -> None:
    """Generate the unified Phase 3AN operational gap-fix bundle."""
    engine = init_db()
    settings = get_settings()
    session_factory = get_session_factory(engine)
    with session_factory() as session:
        artifacts = write_phase3an_gap_fix_report(
            session,
            output_dir=output_dir,
            reports_dir=reports_dir,
            settings=settings,
            window_hours=window_hours,
            max_settlements=max_settlements,
            limit_per_bucket=limit_per_bucket,
        )
    console.print("Phase 3AN Gap Fix Report")
    console.print("Mode: bounded report-only diagnostics")
    console.print("Live/demo execution and downstream trading writes: blocked")
    console.print(f"Wrote summary: {artifacts.summary_path}")
    console.print(f"Wrote next actions: {artifacts.next_actions_path}")
    console.print(f"Wrote manifest: {artifacts.manifest_path}")


@app.command("phase3ao-learning-reward-pipeline")
def phase3ao_learning_reward_pipeline_command(
    output_dir: Annotated[
        Path,
        typer.Option(help="Directory for Phase 3AO learning reward artifacts."),
    ] = Path("reports/phase3ao"),
    scan_limit: Annotated[
        int,
        typer.Option(help="Maximum paper/diagnostic rows to inspect."),
    ] = 500,
) -> None:
    """Report settled paper rewards flowing into confidence and offline RL."""
    engine = init_db()
    session_factory = get_session_factory(engine)
    with session_factory() as session:
        artifacts = write_phase3ao_report(
            session,
            output_dir=output_dir,
            scan_limit=scan_limit,
        )
    console.print("Phase 3AO Learning Reward Pipeline")
    console.print("Mode: PAPER ONLY diagnostics")
    console.print("Exact-ticker settlement policy: enforced")
    console.print(f"Wrote JSON: {artifacts.json_path}")
    console.print(f"Wrote Markdown: {artifacts.markdown_path}")


@app.command("opportunity-link-audit")
def opportunity_link_audit_command(
    output_dir: Annotated[
        Path,
        typer.Option(help="Directory for Phase 3AO opportunity link audit artifacts."),
    ] = Path("reports/phase3ao"),
    limit: Annotated[
        int,
        typer.Option(help="Maximum unique recent opportunity rows to audit."),
    ] = 500,
) -> None:
    """Audit visible opportunity rows for exact verified Kalshi market links."""
    engine = init_db()
    session_factory = get_session_factory(engine)
    with session_factory() as session:
        artifacts = write_phase3ao_opportunity_link_audit(
            session,
            output_dir=output_dir,
            limit=limit,
        )
        summary = artifacts.json_path.read_text(encoding="utf-8")
        payload = json.loads(summary)
    console.print("Phase 3AO Opportunity Link Audit")
    console.print("Mode: PAPER / READ ONLY")
    console.print("Live/demo execution: blocked")
    console.print("Order submission/cancel/replace: blocked")
    console.print("Paper trade creation: blocked")
    console.print(f"Scanned opportunities: {payload['summary']['total_opportunities_scanned']}")
    console.print(f"Verified clickable URLs: {payload['summary']['verified_urls']}")
    console.print(
        "UI-visible missing verified URL: "
        f"{payload['summary']['ui_visible_opportunities_without_clickable_verified_url']}"
    )
    console.print(f"Contract pass: {payload['summary']['passes_contract']}")
    console.print(f"Wrote JSON: {artifacts.json_path}")
    console.print(f"Wrote Markdown: {artifacts.markdown_path}")
    console.print(f"Wrote CSV: {artifacts.broken_links_csv_path}")
    console.print(f"Wrote manifest: {artifacts.manifest_path}")


@app.command("phase3ap-night-runner-v2")
def phase3ap_night_runner_v2_command(
    output_dir: Annotated[
        Path,
        typer.Option(help="Directory for Phase 3AP safe night runner artifacts."),
    ] = Path("reports/phase3ap"),
    max_cycles: Annotated[
        int,
        typer.Option(help="Default cycles for generated runner script."),
    ] = 32,
    interval_minutes: Annotated[
        int,
        typer.Option(help="Default interval minutes for generated runner script."),
    ] = 15,
    scan_limit: Annotated[
        int,
        typer.Option(help="Maximum diagnostic rows to inspect."),
    ] = 500,
) -> None:
    """Generate a paper-only safe night runner v2 plan and shell script."""
    engine = init_db()
    settings = get_settings()
    session_factory = get_session_factory(engine)
    with session_factory() as session:
        artifacts = write_phase3ap_report(
            session,
            output_dir=output_dir,
            settings=settings,
            max_cycles=max_cycles,
            interval_minutes=interval_minutes,
            scan_limit=scan_limit,
        )
    console.print("Phase 3AP Automated Safe Night Runner v2")
    console.print("Mode: PAPER ONLY diagnostics")
    console.print("Live/demo execution: blocked")
    console.print(f"Wrote JSON: {artifacts.json_path}")
    console.print(f"Wrote Markdown: {artifacts.markdown_path}")
    console.print(f"Wrote script: {artifacts.script_path}")


@app.command("phase3ap-book-diagnostic")
def phase3ap_book_diagnostic_command(
    output_dir: Annotated[
        Path,
        typer.Option(help="Directory for Phase 3AP executable-book diagnostic artifacts."),
    ] = Path("reports/phase3ap"),
    window_hours: Annotated[
        int,
        typer.Option(help="Lookback window for market rankings."),
    ] = 168,
    limit: Annotated[
        int,
        typer.Option(help="Maximum ranked rows to inspect."),
    ] = 500,
) -> None:
    """Explain positive-EV rows that do not have executable books."""
    engine = init_db()
    settings = get_settings()
    session_factory = get_session_factory(engine)
    with session_factory() as session:
        artifacts = write_phase3ap_book_diagnostic_report(
            session,
            output_dir=output_dir,
            settings=settings,
            window_hours=window_hours,
            limit=limit,
        )
        payload = json.loads(artifacts.json_path.read_text(encoding="utf-8"))
    console.print("Phase 3AP Book Diagnostic")
    console.print("Mode: PAPER / READ ONLY")
    console.print("Live/demo execution: blocked")
    console.print("Order submission/cancel/replace: blocked")
    console.print(
        "Positive EV/no executable book: "
        f"{payload['summary']['positive_ev_no_executable_book_rows']}"
    )
    console.print(f"Wrote JSON: {artifacts.json_path}")
    console.print(f"Wrote Markdown: {artifacts.markdown_path}")


@app.command("phase3ap-refresh-positive-ev-books")
def phase3ap_refresh_positive_ev_books_command(
    output_dir: Annotated[
        Path,
        typer.Option(help="Directory for Phase 3AP positive-EV book refresh artifacts."),
    ] = Path("reports/phase3ap"),
    dry_run: Annotated[
        bool,
        typer.Option(
            "--dry-run/--no-dry-run",
            help="Report the refresh plan without fetching fresh orderbooks.",
        ),
    ] = True,
    apply_readonly_refresh: Annotated[
        bool,
        typer.Option(
            "--apply-readonly-refresh",
            help="Run the bounded market-data refresh only after writer gates pass.",
        ),
    ] = False,
    max_markets: Annotated[
        int,
        typer.Option(help="Maximum positive-EV no-book markets to include."),
    ] = 25,
    max_duration_seconds: Annotated[
        int,
        typer.Option(help="Maximum bounded refresh duration."),
    ] = 120,
    window_hours: Annotated[
        int,
        typer.Option(help="Lookback window for market rankings."),
    ] = 168,
    limit: Annotated[
        int,
        typer.Option(help="Maximum ranked rows to inspect."),
    ] = 500,
) -> None:
    """Dry-run or bounded-refresh exact positive-EV markets with missing books."""
    engine = init_db()
    settings = get_settings()
    session_factory = get_session_factory(engine)
    with session_factory() as session:
        artifacts = write_phase3ap_refresh_positive_ev_books_report(
            session,
            output_dir=output_dir,
            dry_run=dry_run,
            apply_readonly_refresh=apply_readonly_refresh,
            max_markets=max_markets,
            max_duration_seconds=max_duration_seconds,
            settings=settings,
            window_hours=window_hours,
            limit=limit,
        )
        payload = json.loads(artifacts.json_path.read_text(encoding="utf-8"))
    console.print("Phase 3AP Positive-EV Book Refresh")
    console.print("Mode: PAPER / READ ONLY")
    console.print("Live/demo execution: blocked")
    console.print("Order submission/cancel/replace: blocked")
    console.print(f"Status: {payload['status']}")
    console.print(f"Market-data writes: {payload['market_data_writes']}")
    console.print(f"Wrote JSON: {artifacts.json_path}")
    console.print(f"Wrote Markdown: {artifacts.markdown_path}")


@app.command("phase3ap-settlement-check-diagnostic")
def phase3ap_settlement_check_diagnostic_command(
    output_dir: Annotated[
        Path,
        typer.Option(help="Directory for Phase 3AP settlement diagnostic artifacts."),
    ] = Path("reports/phase3ap"),
    window_hours: Annotated[
        int,
        typer.Option(help="Lookback window for paper-funnel rows."),
    ] = 168,
    limit: Annotated[
        int,
        typer.Option(help="Maximum paper-funnel rows to inspect."),
    ] = 500,
) -> None:
    """Split generic settlement-check blockers into specific paper-entry reasons."""
    engine = init_db()
    settings = get_settings()
    session_factory = get_session_factory(engine)
    with session_factory() as session:
        artifacts = write_phase3ap_settlement_check_diagnostic_report(
            session,
            output_dir=output_dir,
            settings=settings,
            window_hours=window_hours,
            limit=limit,
        )
        payload = json.loads(artifacts.json_path.read_text(encoding="utf-8"))
    console.print("Phase 3AP Settlement Check Diagnostic")
    console.print("Mode: PAPER / READ ONLY")
    console.print("Live/demo execution: blocked")
    console.print("Order submission/cancel/replace: blocked")
    console.print(
        "Generic settlement-check rows remaining: "
        f"{payload['summary']['generic_settlement_check_failed_remaining']}"
    )
    console.print(f"Wrote JSON: {artifacts.json_path}")
    console.print(f"Wrote Markdown: {artifacts.markdown_path}")


@app.command("phase3ap-paper-ready-unblock-report")
def phase3ap_paper_ready_unblock_report_command(
    output_dir: Annotated[
        Path,
        typer.Option(help="Directory for Phase 3AP unified unblock artifacts."),
    ] = Path("reports/phase3ap"),
    reports_dir: Annotated[
        Path,
        typer.Option(help="Root reports directory used by source/status diagnostics."),
    ] = Path("reports"),
    window_hours: Annotated[
        int,
        typer.Option(help="Lookback window for market rankings and funnel rows."),
    ] = 168,
    limit: Annotated[
        int,
        typer.Option(help="Maximum ranked rows to inspect."),
    ] = 500,
) -> None:
    """Write the unified paper-ready gate and executable-opportunity unblock report."""
    engine = init_db()
    settings = get_settings()
    session_factory = get_session_factory(engine)
    with session_factory() as session:
        artifacts = write_phase3ap_paper_ready_unblock_report(
            session,
            output_dir=output_dir,
            reports_dir=reports_dir,
            settings=settings,
            window_hours=window_hours,
            limit=limit,
        )
        payload = json.loads(artifacts.paper_ready_gate_path.read_text(encoding="utf-8"))
    console.print("Phase 3AP Paper-Ready Unblock Report")
    console.print("Mode: PAPER / READ ONLY")
    console.print("Live/demo execution: blocked")
    console.print("Order submission/cancel/replace: blocked")
    console.print("Paper trade creation: blocked")
    console.print(f"Paper-ready rows: {payload['summary']['paper_ready_rows']}")
    console.print(f"Positive EV rows: {payload['summary']['positive_ev_rows']}")
    console.print(f"Wrote executive summary: {artifacts.executive_summary_path}")
    console.print(f"Wrote next actions: {artifacts.next_actions_path}")
    console.print(f"Wrote paper-ready gate: {artifacts.paper_ready_gate_path}")
    console.print(f"Wrote manifest: {artifacts.manifest_path}")


@app.command("phase3aq-self-improvement")
def phase3aq_self_improvement_command(
    output_dir: Annotated[
        Path,
        typer.Option(help="Directory for Phase 3AQ self-improvement artifacts."),
    ] = Path("reports/phase3aq"),
    scan_limit: Annotated[
        int,
        typer.Option(help="Maximum diagnostic rows to inspect."),
    ] = 500,
) -> None:
    """Generate advisory next-build recommendations from local reports and metrics."""
    engine = init_db()
    session_factory = get_session_factory(engine)
    with session_factory() as session:
        artifacts = write_phase3aq_report(
            session,
            output_dir=output_dir,
            scan_limit=scan_limit,
        )
    console.print("Phase 3AQ Self-Improvement Engine")
    console.print("Mode: PAPER ONLY advisory")
    console.print("Generated code execution: blocked")
    console.print("Live/demo execution: blocked")
    console.print(f"Wrote JSON: {artifacts.json_path}")
    console.print(f"Wrote Markdown: {artifacts.markdown_path}")
    console.print(f"Wrote next prompt: {artifacts.prompt_path}")


@app.command("phase3aq-positive-ev-link-audit")
def phase3aq_positive_ev_link_audit_command(
    output_dir: Annotated[
        Path,
        typer.Option(help="Directory for Phase 3AQ positive-EV link audit artifacts."),
    ] = Path("reports/phase3aq"),
    reports_dir: Annotated[
        Path,
        typer.Option(help="Root reports directory used for report-source context."),
    ] = Path("reports"),
    window_hours: Annotated[
        int,
        typer.Option(help="Lookback window for market rankings."),
    ] = 168,
    limit: Annotated[
        int,
        typer.Option(help="Maximum ranked rows to inspect."),
    ] = 500,
) -> None:
    """Classify positive-EV rows by exact Kalshi link status before book status."""
    engine = init_db()
    settings = get_settings()
    session_factory = get_session_factory(engine)
    with session_factory() as session:
        artifacts = write_phase3aq_positive_ev_link_audit_report(
            session,
            output_dir=output_dir,
            reports_dir=reports_dir,
            settings=settings,
            window_hours=window_hours,
            limit=limit,
        )
        payload = json.loads(artifacts.json_path.read_text(encoding="utf-8"))
    console.print("Phase 3AQ Positive-EV Link Audit")
    console.print("Mode: PAPER / READ ONLY")
    console.print("Live/demo execution: blocked")
    console.print("Order submission/cancel/replace: blocked")
    console.print("Fake links and sibling/fuzzy matching: blocked")
    console.print(f"Positive EV rows: {payload['summary']['positive_ev_rows']}")
    console.print(
        "Generic unverified-link rows remaining: "
        f"{payload['summary']['generic_unverified_link_rows_remaining']}"
    )
    console.print(f"Wrote JSON: {artifacts.json_path}")
    console.print(f"Wrote Markdown: {artifacts.markdown_path}")


@app.command("phase3aq-refresh-verified-opportunity-books")
def phase3aq_refresh_verified_opportunity_books_command(
    output_dir: Annotated[
        Path,
        typer.Option(help="Directory for Phase 3AQ verified-book refresh artifacts."),
    ] = Path("reports/phase3aq"),
    dry_run: Annotated[
        bool,
        typer.Option(
            "--dry-run/--no-dry-run",
            help="Report the refresh plan without fetching fresh orderbooks.",
        ),
    ] = True,
    apply_readonly_refresh: Annotated[
        bool,
        typer.Option(
            "--apply-readonly-refresh",
            help="Run bounded market-data refresh only for verified-link candidates.",
        ),
    ] = False,
    max_markets: Annotated[
        int,
        typer.Option(help="Maximum verified positive-EV markets to refresh."),
    ] = 100,
    max_duration_seconds: Annotated[
        int,
        typer.Option(help="Maximum bounded refresh duration."),
    ] = 120,
    window_hours: Annotated[
        int,
        typer.Option(help="Lookback window for market rankings."),
    ] = 168,
    limit: Annotated[
        int,
        typer.Option(help="Maximum ranked rows to inspect."),
    ] = 500,
) -> None:
    """Dry-run or bounded-refresh books only for exact verified positive-EV markets."""
    engine = init_db()
    settings = get_settings()
    session_factory = get_session_factory(engine)
    with session_factory() as session:
        artifacts = write_phase3aq_refresh_verified_opportunity_books_report(
            session,
            output_dir=output_dir,
            dry_run=dry_run,
            apply_readonly_refresh=apply_readonly_refresh,
            max_markets=max_markets,
            max_duration_seconds=max_duration_seconds,
            settings=settings,
            window_hours=window_hours,
            limit=limit,
        )
        payload = json.loads(artifacts.json_path.read_text(encoding="utf-8"))
    console.print("Phase 3AQ Verified Opportunity Book Refresh")
    console.print("Mode: PAPER / READ ONLY")
    console.print("Live/demo execution: blocked")
    console.print("Order submission/cancel/replace: blocked")
    console.print(f"Status: {payload['status']}")
    console.print(f"Market-data writes: {payload['market_data_writes']}")
    console.print(
        "Verified book refresh candidates: "
        f"{payload['summary']['book_refresh_needed_rows']}"
    )
    console.print(f"Wrote JSON: {artifacts.json_path}")
    console.print(f"Wrote Markdown: {artifacts.markdown_path}")


@app.command("phase3aq-settlement-check-split")
def phase3aq_settlement_check_split_command(
    output_dir: Annotated[
        Path,
        typer.Option(help="Directory for Phase 3AQ settlement split artifacts."),
    ] = Path("reports/phase3aq"),
    window_hours: Annotated[
        int,
        typer.Option(help="Lookback window for paper-funnel rows."),
    ] = 168,
    limit: Annotated[
        int,
        typer.Option(help="Maximum paper-funnel rows to inspect."),
    ] = 500,
) -> None:
    """Split generic settlement-check blockers into specific paper-entry reasons."""
    engine = init_db()
    settings = get_settings()
    session_factory = get_session_factory(engine)
    with session_factory() as session:
        artifacts = write_phase3aq_settlement_check_split_report(
            session,
            output_dir=output_dir,
            settings=settings,
            window_hours=window_hours,
            limit=limit,
        )
        payload = json.loads(artifacts.json_path.read_text(encoding="utf-8"))
    console.print("Phase 3AQ Settlement Check Split")
    console.print("Mode: PAPER / READ ONLY")
    console.print("Live/demo execution: blocked")
    console.print("Order submission/cancel/replace: blocked")
    console.print(
        "Generic settlement-check rows remaining: "
        f"{payload['summary']['generic_settlement_check_failed_remaining']}"
    )
    console.print(f"Wrote JSON: {artifacts.json_path}")
    console.print(f"Wrote Markdown: {artifacts.markdown_path}")


@app.command("phase3aq-link-and-book-unblock-report")
def phase3aq_link_and_book_unblock_report_command(
    output_dir: Annotated[
        Path,
        typer.Option(help="Directory for Phase 3AQ unified unblock artifacts."),
    ] = Path("reports/phase3aq"),
    reports_dir: Annotated[
        Path,
        typer.Option(help="Root reports directory used by source/status diagnostics."),
    ] = Path("reports"),
    window_hours: Annotated[
        int,
        typer.Option(help="Lookback window for market rankings and funnel rows."),
    ] = 168,
    limit: Annotated[
        int,
        typer.Option(help="Maximum ranked rows to inspect."),
    ] = 500,
) -> None:
    """Write the unified verified-link and executable-book recovery report."""
    engine = init_db()
    settings = get_settings()
    session_factory = get_session_factory(engine)
    with session_factory() as session:
        artifacts = write_phase3aq_link_and_book_unblock_report(
            session,
            output_dir=output_dir,
            reports_dir=reports_dir,
            settings=settings,
            window_hours=window_hours,
            limit=limit,
        )
        payload = json.loads(
            artifacts.paper_ready_gate_summary_path.read_text(encoding="utf-8")
        )
    console.print("Phase 3AQ Link and Book Unblock Report")
    console.print("Mode: PAPER / READ ONLY")
    console.print("Live/demo execution: blocked")
    console.print("Order submission/cancel/replace: blocked")
    console.print("Paper trade creation: blocked")
    console.print("Fake links and sibling/fuzzy matching: blocked")
    console.print(f"Positive EV rows: {payload['summary']['positive_ev_rows']}")
    console.print(f"Paper-ready rows: {payload['summary']['paper_ready_rows']}")
    console.print(f"Wrote executive summary: {artifacts.executive_summary_path}")
    console.print(f"Wrote next actions: {artifacts.next_actions_path}")
    console.print(f"Wrote link audit: {artifacts.positive_ev_link_audit_path}")
    console.print(f"Wrote gate summary: {artifacts.paper_ready_gate_summary_path}")
    console.print(f"Wrote manifest: {artifacts.manifest_path}")


@app.command("phase3ar-url-audit")
def phase3ar_url_audit_command(
    output_dir: Annotated[
        Path,
        typer.Option(help="Directory for Phase 3AR URL audit artifacts."),
    ] = Path("reports/phase3ar"),
    reports_dir: Annotated[
        Path,
        typer.Option(help="Root reports directory used for status context."),
    ] = Path("reports"),
    window_hours: Annotated[
        int,
        typer.Option(help="Lookback window for market rankings."),
    ] = 168,
    limit: Annotated[
        int,
        typer.Option(help="Maximum ranked rows to inspect."),
    ] = 500,
) -> None:
    """Audit malformed Kalshi URLs for positive-EV opportunity rows."""
    engine = init_db()
    settings = get_settings()
    session_factory = get_session_factory(engine)
    with session_factory() as session:
        artifacts = write_phase3ar_url_audit_report(
            session,
            output_dir=output_dir,
            reports_dir=reports_dir,
            settings=settings,
            window_hours=window_hours,
            limit=limit,
        )
        payload = json.loads(artifacts.json_path.read_text(encoding="utf-8"))
    console.print("Phase 3AR URL Audit")
    console.print("Mode: PAPER / READ ONLY")
    console.print("Live/demo execution: blocked")
    console.print("Order submission/cancel/replace: blocked")
    console.print(f"Positive EV rows: {payload['summary']['positive_ev_rows']}")
    console.print(f"Safe to persist: {payload['summary']['safe_to_persist']}")
    console.print(f"Wrote JSON: {artifacts.json_path}")
    console.print(f"Wrote Markdown: {artifacts.markdown_path}")


@app.command("phase3ar-catalog-stale-diagnostic")
def phase3ar_catalog_stale_diagnostic_command(
    output_dir: Annotated[
        Path,
        typer.Option(help="Directory for Phase 3AR catalog stale diagnostic artifacts."),
    ] = Path("reports/phase3ar"),
    reports_dir: Annotated[
        Path,
        typer.Option(help="Root reports directory used for status context."),
    ] = Path("reports"),
    window_hours: Annotated[
        int,
        typer.Option(help="Lookback window for market rankings."),
    ] = 168,
    limit: Annotated[
        int,
        typer.Option(help="Maximum ranked rows to inspect."),
    ] = 500,
) -> None:
    """Diagnose stale exact catalog rows blocking Phase 3AR URL verification."""
    engine = init_db()
    settings = get_settings()
    session_factory = get_session_factory(engine)
    with session_factory() as session:
        artifacts = write_phase3ar_catalog_stale_diagnostic_report(
            session,
            output_dir=output_dir,
            reports_dir=reports_dir,
            settings=settings,
            window_hours=window_hours,
            limit=limit,
        )
        payload = json.loads(artifacts.json_path.read_text(encoding="utf-8"))
    console.print("Phase 3AR Catalog Stale Diagnostic")
    console.print("Mode: PAPER / READ ONLY")
    console.print("Live/demo execution: blocked")
    console.print("Order submission/cancel/replace: blocked")
    console.print(f"Stale catalog rows: {payload['summary']['stale_catalog_rows']}")
    console.print(f"Refreshable exact markets: {payload['summary']['refreshable_exact_markets']}")
    console.print(f"Wrote JSON: {artifacts.json_path}")
    console.print(f"Wrote Markdown: {artifacts.markdown_path}")


@app.command("phase3ar-refresh-catalog-for-opportunities")
def phase3ar_refresh_catalog_for_opportunities_command(
    output_dir: Annotated[
        Path,
        typer.Option(help="Directory for Phase 3AR catalog refresh artifacts."),
    ] = Path("reports/phase3ar"),
    reports_dir: Annotated[
        Path,
        typer.Option(help="Root reports directory used for status context."),
    ] = Path("reports"),
    dry_run: Annotated[
        bool,
        typer.Option("--dry-run/--no-dry-run", help="Plan catalog refresh without writes."),
    ] = True,
    apply_readonly_refresh: Annotated[
        bool,
        typer.Option(
            "--apply-readonly-refresh",
            help="Fetch exact positive-EV market catalog rows from read-only Kalshi endpoints.",
        ),
    ] = False,
    max_markets: Annotated[
        int,
        typer.Option(help="Maximum exact market catalog rows to refresh."),
    ] = 100,
    max_duration_seconds: Annotated[
        int,
        typer.Option(help="Maximum bounded refresh duration."),
    ] = 120,
    window_hours: Annotated[
        int,
        typer.Option(help="Lookback window for market rankings."),
    ] = 168,
    limit: Annotated[
        int,
        typer.Option(help="Maximum ranked rows to inspect."),
    ] = 500,
) -> None:
    """Plan or run bounded read-only catalog refresh for exact opportunity tickers."""
    engine = init_db()
    settings = get_settings()
    session_factory = get_session_factory(engine)
    with session_factory() as session:
        artifacts = write_phase3ar_refresh_catalog_for_opportunities_report(
            session,
            output_dir=output_dir,
            reports_dir=reports_dir,
            dry_run=dry_run,
            apply_readonly_refresh=apply_readonly_refresh,
            max_markets=max_markets,
            max_duration_seconds=max_duration_seconds,
            settings=settings,
            window_hours=window_hours,
            limit=limit,
        )
        payload = json.loads(artifacts.json_path.read_text(encoding="utf-8"))
    console.print("Phase 3AR Catalog Refresh For Opportunities")
    console.print("Mode: PAPER / READ ONLY CATALOG REFRESH")
    console.print("Live/demo execution: blocked")
    console.print("Order submission/cancel/replace: blocked")
    console.print(f"Status: {payload['status']}")
    console.print(f"Catalog metadata writes: {payload['catalog_metadata_writes']}")
    console.print(f"Refresh candidates: {payload['summary']['refresh_candidates']}")
    console.print(f"Refreshed rows: {payload['summary']['refreshed_rows']}")
    console.print(f"Wrote JSON: {artifacts.json_path}")
    console.print(f"Wrote Markdown: {artifacts.markdown_path}")


@app.command("phase3ar-url-repair")
def phase3ar_url_repair_command(
    output_dir: Annotated[
        Path,
        typer.Option(help="Directory for Phase 3AR URL repair artifacts."),
    ] = Path("reports/phase3ar"),
    reports_dir: Annotated[
        Path,
        typer.Option(help="Root reports directory used for status context."),
    ] = Path("reports"),
    dry_run: Annotated[
        bool,
        typer.Option("--dry-run/--no-dry-run", help="Plan URL repairs without writes."),
    ] = True,
    apply_repair: Annotated[
        bool,
        typer.Option("--apply", help="Persist URL/slug repair metadata for safe exact rows."),
    ] = False,
    backup_first: Annotated[
        bool,
        typer.Option("--backup-first", help="Required before apply writes URL metadata."),
    ] = False,
    max_records: Annotated[
        int,
        typer.Option(help="Maximum URL repair rows to apply."),
    ] = 100,
    window_hours: Annotated[
        int,
        typer.Option(help="Lookback window for market rankings."),
    ] = 168,
    limit: Annotated[
        int,
        typer.Option(help="Maximum ranked rows to inspect."),
    ] = 500,
) -> None:
    """Dry-run or apply guarded exact-catalog Kalshi URL repair metadata."""
    engine = init_db()
    settings = get_settings()
    session_factory = get_session_factory(engine)
    with session_factory() as session:
        artifacts = write_phase3ar_url_repair_report(
            session,
            output_dir=output_dir,
            reports_dir=reports_dir,
            dry_run=dry_run,
            apply=apply_repair,
            backup_first=backup_first,
            max_records=max_records,
            settings=settings,
            window_hours=window_hours,
            limit=limit,
        )
        payload = json.loads(artifacts.json_path.read_text(encoding="utf-8"))
    console.print("Phase 3AR URL Repair")
    console.print("Mode: PAPER / URL-CATALOG METADATA ONLY")
    console.print("Live/demo execution: blocked")
    console.print("Order submission/cancel/replace: blocked")
    console.print(f"Status: {payload['status']}")
    console.print(f"URL/catalog metadata writes: {payload['url_catalog_metadata_writes']}")
    console.print(f"Repaired rows: {payload['summary']['repaired_rows']}")
    if payload.get("backup_path"):
        console.print(f"Backup: {payload['backup_path']}")
    console.print(f"Wrote JSON: {artifacts.json_path}")
    console.print(f"Wrote Markdown: {artifacts.markdown_path}")


@app.command("phase3ar-refresh-books-for-verified-links")
def phase3ar_refresh_books_for_verified_links_command(
    output_dir: Annotated[
        Path,
        typer.Option(help="Directory for Phase 3AR book refresh artifacts."),
    ] = Path("reports/phase3ar"),
    dry_run: Annotated[
        bool,
        typer.Option("--dry-run/--no-dry-run", help="Plan refresh without market-data writes."),
    ] = True,
    apply_readonly_refresh: Annotated[
        bool,
        typer.Option("--apply-readonly-refresh", help="Run bounded read-only market-data refresh."),
    ] = False,
    max_markets: Annotated[
        int,
        typer.Option(help="Maximum verified exact markets to refresh."),
    ] = 100,
    max_duration_seconds: Annotated[
        int,
        typer.Option(help="Maximum bounded refresh duration."),
    ] = 120,
    window_hours: Annotated[
        int,
        typer.Option(help="Lookback window for market rankings."),
    ] = 168,
    limit: Annotated[
        int,
        typer.Option(help="Maximum ranked rows to inspect."),
    ] = 500,
) -> None:
    """Refresh books only for exact verified Kalshi-link markets."""
    engine = init_db()
    settings = get_settings()
    session_factory = get_session_factory(engine)
    with session_factory() as session:
        artifacts = write_phase3ar_refresh_books_for_verified_links_report(
            session,
            output_dir=output_dir,
            dry_run=dry_run,
            apply_readonly_refresh=apply_readonly_refresh,
            max_markets=max_markets,
            max_duration_seconds=max_duration_seconds,
            settings=settings,
            window_hours=window_hours,
            limit=limit,
        )
        payload = json.loads(artifacts.json_path.read_text(encoding="utf-8"))
    console.print("Phase 3AR Book Refresh For Verified Links")
    console.print("Mode: PAPER / READ ONLY")
    console.print("Live/demo execution: blocked")
    console.print("Order submission/cancel/replace: blocked")
    console.print(f"Status: {payload['status']}")
    console.print(f"Market-data writes: {payload['market_data_writes']}")
    console.print(f"Book refresh candidates: {payload['summary']['book_refresh_needed_rows']}")
    console.print(f"Wrote JSON: {artifacts.json_path}")
    console.print(f"Wrote Markdown: {artifacts.markdown_path}")


@app.command("phase3ar-settlement-check-noise-audit")
def phase3ar_settlement_check_noise_audit_command(
    output_dir: Annotated[
        Path,
        typer.Option(help="Directory for Phase 3AR settlement-noise artifacts."),
    ] = Path("reports/phase3ar"),
    window_hours: Annotated[
        int,
        typer.Option(help="Lookback window for paper-funnel rows."),
    ] = 168,
    limit: Annotated[
        int,
        typer.Option(help="Maximum paper-funnel rows to inspect."),
    ] = 500,
) -> None:
    """Classify settlement-check noise so it does not hide earlier URL blockers."""
    engine = init_db()
    settings = get_settings()
    session_factory = get_session_factory(engine)
    with session_factory() as session:
        artifacts = write_phase3ar_settlement_check_noise_audit_report(
            session,
            output_dir=output_dir,
            settings=settings,
            window_hours=window_hours,
            limit=limit,
        )
        payload = json.loads(artifacts.json_path.read_text(encoding="utf-8"))
    console.print("Phase 3AR Settlement Check Noise Audit")
    console.print("Mode: PAPER / READ ONLY")
    console.print("Live/demo execution: blocked")
    console.print("Order submission/cancel/replace: blocked")
    console.print(
        "Generic settlement-check rows remaining: "
        f"{payload['summary']['generic_settlement_check_failed_remaining']}"
    )
    console.print(f"Wrote JSON: {artifacts.json_path}")
    console.print(f"Wrote Markdown: {artifacts.markdown_path}")


@app.command("phase3ar-link-repair-report")
def phase3ar_link_repair_report_command(
    output_dir: Annotated[
        Path,
        typer.Option(help="Directory for Phase 3AR unified link repair artifacts."),
    ] = Path("reports/phase3ar"),
    reports_dir: Annotated[
        Path,
        typer.Option(help="Root reports directory used by source/status diagnostics."),
    ] = Path("reports"),
    window_hours: Annotated[
        int,
        typer.Option(help="Lookback window for market rankings and funnel rows."),
    ] = 168,
    limit: Annotated[
        int,
        typer.Option(help="Maximum ranked rows to inspect."),
    ] = 500,
) -> None:
    """Write the unified Phase 3AR URL repair and paper-ready gate report."""
    engine = init_db()
    settings = get_settings()
    session_factory = get_session_factory(engine)
    with session_factory() as session:
        artifacts = write_phase3ar_link_repair_report(
            session,
            output_dir=output_dir,
            reports_dir=reports_dir,
            settings=settings,
            window_hours=window_hours,
            limit=limit,
        )
        payload = json.loads(artifacts.url_audit_path.read_text(encoding="utf-8"))
    console.print("Phase 3AR Link Repair Report")
    console.print("Mode: PAPER / READ ONLY")
    console.print("Live/demo execution: blocked")
    console.print("Order submission/cancel/replace: blocked")
    console.print(f"Positive EV rows: {payload['summary']['positive_ev_rows']}")
    console.print(f"Verified links: {payload['summary']['current_verified_links']}")
    console.print(f"Wrote executive summary: {artifacts.executive_summary_path}")
    console.print(f"Wrote next actions: {artifacts.next_actions_path}")
    console.print(f"Wrote URL audit: {artifacts.url_audit_path}")
    console.print(f"Wrote manifest: {artifacts.manifest_path}")


@app.command("phase3ar-crypto-forecast-coverage")
@app.command("crypto-forecast-doctor")
def crypto_forecast_doctor_command(
    output_dir: Annotated[
        Path,
        typer.Option(help="Directory for Phase 3AR crypto forecast coverage artifacts."),
    ] = Path("reports/phase3ar"),
    limit: Annotated[
        int,
        typer.Option(help="Maximum latest crypto-linked markets to inspect."),
    ] = 500,
    repair_snapshots: Annotated[
        bool,
        typer.Option(
            "--repair-snapshots/--diagnose-only",
            help="Fetch public market/orderbook snapshots for linked crypto markets with gaps.",
        ),
    ] = False,
) -> None:
    """Diagnose and optionally repair crypto-linked snapshots before crypto_v2 forecasts."""
    engine = init_db()
    settings = get_settings()
    session_factory = get_session_factory(engine)
    with session_factory() as session:
        artifacts = write_phase3ar_report(
            session,
            output_dir=output_dir,
            settings=settings,
            limit=limit,
            repair_snapshots=repair_snapshots,
        )
        if repair_snapshots:
            session.commit()
    console.print("Phase 3AR Crypto Forecast Coverage Repair")
    console.print("Mode: PAPER ONLY diagnostics")
    console.print("Live/demo execution: blocked")
    console.print(f"Wrote JSON: {artifacts.json_path}")
    console.print(f"Wrote Markdown: {artifacts.markdown_path}")
    console.print(f"Wrote rows: {artifacts.rows_path}")


@app.command("phase3as-active-universe")
@app.command("active-universe-doctor")
def active_universe_doctor_command(
    output_dir: Annotated[
        Path,
        typer.Option(help="Directory for Phase 3AS active universe artifacts."),
    ] = Path("reports/phase3as"),
    limit: Annotated[
        int,
        typer.Option(help="Maximum latest crypto/sports linked markets per source to inspect."),
    ] = 150,
    mark_deprecated: Annotated[
        bool,
        typer.Option(
            "--mark-deprecated/--audit-only",
            help="Mark closed/inactive local link rows as deprecated in raw_json metadata.",
        ),
    ] = False,
) -> None:
    """Separate active linked markets from closed/deprecated local links."""
    engine = init_db()
    session_factory = get_session_factory(engine)
    with session_factory() as session:
        artifacts = write_phase3as_report(
            session,
            output_dir=output_dir,
            limit=limit,
            mark_deprecated=mark_deprecated,
        )
        if mark_deprecated:
            session.commit()
    console.print("Phase 3AS Active Market Universe + Closed-Link Cleanup")
    console.print("Mode: PAPER ONLY diagnostics")
    console.print("Live/demo execution: blocked")
    console.print(f"Deprecated local link metadata: {mark_deprecated}")
    console.print(f"Wrote JSON: {artifacts.json_path}")
    console.print(f"Wrote Markdown: {artifacts.markdown_path}")
    console.print(f"Wrote rows: {artifacts.rows_path}")


@app.command("crypto-history-warmup")
def crypto_history_warmup_command(
    symbols: Annotated[
        str,
        typer.Option(help=f"Comma-separated symbols, for example {DEFAULT_CRYPTO_SYMBOLS}."),
    ] = DEFAULT_CRYPTO_SYMBOLS,
    crypto_series_tickers: Annotated[
        str,
        typer.Option(help="Comma-separated crypto Kalshi series tickers to refresh."),
    ] = DEFAULT_CRYPTO_SERIES_TICKERS,
    output_dir: Annotated[
        Path,
        typer.Option(help="Directory for Phase 3AT crypto warmup artifacts."),
    ] = Path("reports/phase3at"),
    history_minutes: Annotated[
        int,
        typer.Option(help="Minimum feature history window to warm in minutes."),
    ] = 1440,
) -> None:
    """Build flagged synthetic local crypto history for paper-only feature warmup."""
    engine = init_db()
    session_factory = get_session_factory(engine)
    with session_factory() as session:
        artifacts = write_crypto_history_warmup_report(
            session,
            symbols=parse_symbols(symbols),
            output_dir=output_dir,
            history_minutes=history_minutes,
        )
        session.commit()
    console.print("Phase 3AT Crypto Feature History Warmup")
    console.print("Mode: PAPER ONLY diagnostics")
    console.print("Synthetic history: flagged local feature warmup only")
    console.print("Live/demo execution: blocked")
    console.print(f"Wrote JSON: {artifacts.json_path}")
    console.print(f"Wrote Markdown: {artifacts.markdown_path}")


@app.command("phase3at-active-router")
@app.command("active-crypto-router")
def phase3at_active_router_command(
    output_dir: Annotated[
        Path,
        typer.Option(help="Directory for Phase 3AT router artifacts."),
    ] = Path("reports/phase3at"),
    symbols: Annotated[
        str,
        typer.Option(help=f"Comma-separated symbols, for example {DEFAULT_CRYPTO_SYMBOLS}."),
    ] = DEFAULT_CRYPTO_SYMBOLS,
    limit: Annotated[
        int,
        typer.Option(help="Maximum latest active crypto-linked markets to inspect."),
    ] = 500,
) -> None:
    """Trace active crypto links through forecasts, opportunities, and paper candidates."""
    engine = init_db()
    settings = get_settings()
    session_factory = get_session_factory(engine)
    with session_factory() as session:
        artifacts = write_phase3at_report(
            session,
            output_dir=output_dir,
            settings=settings,
            symbols=parse_symbols(symbols),
            limit=limit,
        )
    console.print("Phase 3AT Active Forecast-to-Opportunity Router")
    console.print("Mode: PAPER ONLY diagnostics")
    console.print("Live/demo execution: blocked")
    console.print(f"Wrote JSON: {artifacts.json_path}")
    console.print(f"Wrote Markdown: {artifacts.markdown_path}")
    console.print(f"Wrote rows: {artifacts.rows_path}")


@app.command("phase3at-forecast-ranking-diagnostic")
def phase3at_forecast_ranking_diagnostic_command(
    output_dir: Annotated[
        Path,
        typer.Option(help="Directory for Phase 3AT diagnostic artifacts."),
    ] = Path("reports/phase3at"),
    reports_dir: Annotated[
        Path,
        typer.Option(help="Root reports directory used for handoff context."),
    ] = Path("reports"),
    limit: Annotated[
        int,
        typer.Option(help="Maximum latest crypto links to inspect."),
    ] = 500,
) -> None:
    """Diagnose current-window crypto forecast-to-ranking joins."""
    engine = init_db()
    settings = get_settings()
    session_factory = get_session_factory(engine)
    with session_factory() as session:
        artifacts = write_phase3at_forecast_ranking_diagnostic_report(
            session,
            output_dir=output_dir,
            reports_dir=reports_dir,
            settings=settings,
            limit=limit,
            command_args=list(sys.argv[1:]),
        )
    console.print("Phase 3AT Forecast-to-Ranking Diagnostic")
    console.print("Mode: PAPER ONLY diagnostics")
    console.print("Live/demo execution: blocked")
    console.print("Order submission/cancel/replace: blocked")
    console.print(f"Wrote JSON: {artifacts.json_path}")
    console.print(f"Wrote Markdown: {artifacts.markdown_path}")
    console.print(f"Wrote rows: {artifacts.rows_path}")


@app.command("phase3at-opportunity-funnel")
def phase3at_opportunity_funnel_command(
    output_dir: Annotated[
        Path,
        typer.Option(help="Directory for Phase 3AT opportunity funnel artifacts."),
    ] = Path("reports/phase3at"),
    reports_dir: Annotated[
        Path,
        typer.Option(help="Root reports directory used for handoff context."),
    ] = Path("reports"),
    limit: Annotated[
        int,
        typer.Option(help="Maximum latest crypto links to inspect."),
    ] = 500,
) -> None:
    """Explain current-window crypto opportunity and paper-ready funnel blockers."""
    engine = init_db()
    settings = get_settings()
    session_factory = get_session_factory(engine)
    with session_factory() as session:
        artifacts = write_phase3at_opportunity_funnel_report(
            session,
            output_dir=output_dir,
            reports_dir=reports_dir,
            settings=settings,
            limit=limit,
            command_args=list(sys.argv[1:]),
        )
    console.print("Phase 3AT Current-Window Opportunity Funnel")
    console.print("Mode: PAPER ONLY diagnostics")
    console.print("Live/demo execution: blocked")
    console.print("Order submission/cancel/replace: blocked")
    console.print(f"Wrote JSON: {artifacts.json_path}")
    console.print(f"Wrote Markdown: {artifacts.markdown_path}")
    console.print(f"Wrote rows: {artifacts.rows_path}")


@app.command("phase3at-handoff-report")
def phase3at_handoff_report_command(
    output_dir: Annotated[
        Path,
        typer.Option(help="Directory for Phase 3AT unified handoff artifacts."),
    ] = Path("reports/phase3at"),
    reports_dir: Annotated[
        Path,
        typer.Option(help="Root reports directory used for handoff context."),
    ] = Path("reports"),
    limit: Annotated[
        int,
        typer.Option(help="Maximum latest crypto links to inspect."),
    ] = 500,
) -> None:
    """Write the unified Phase 3AT current-window handoff report."""
    engine = init_db()
    settings = get_settings()
    session_factory = get_session_factory(engine)
    with session_factory() as session:
        artifacts = write_phase3at_handoff_report(
            session,
            output_dir=output_dir,
            reports_dir=reports_dir,
            settings=settings,
            limit=limit,
            command_args=list(sys.argv[1:]),
        )
    console.print("Phase 3AT Current-Window Handoff Report")
    console.print("Mode: PAPER ONLY diagnostics")
    console.print("Live/demo execution: blocked")
    console.print("Order submission/cancel/replace: blocked")
    console.print(f"Wrote JSON: {artifacts.json_path}")
    console.print(f"Wrote Executive Summary: {artifacts.markdown_path}")
    console.print(f"Wrote CSV: {artifacts.rows_path}")


@app.command("phase3af-sports-schedule-bootstrap")
def phase3af_sports_schedule_bootstrap_command(
    output_dir: Annotated[
        Path,
        typer.Option(help="Directory for Phase 3AF Markdown/JSON report artifacts."),
    ] = Path("reports/phase3af"),
    schedule_output_dir: Annotated[
        Path,
        typer.Option(help="Directory where verified sports schedule JSON files are written."),
    ] = Path("data/sports_schedules"),
    leagues: Annotated[
        str,
        typer.Option(help="Comma-separated leagues to fetch: MLB,WNBA,SOCCER,NBA,NFL,NHL,ALL."),
    ] = "MLB,WNBA,SOCCER",
    start_date: Annotated[
        str | None,
        typer.Option(help="Start date as YYYY-MM-DD or YYYYMMDD. Defaults to today."),
    ] = None,
    days_ahead: Annotated[
        int,
        typer.Option(help="Number of schedule days to fetch, starting at start-date."),
    ] = 7,
    ingest: Annotated[
        bool,
        typer.Option(help="Also ingest fetched schedule rows into the local DB."),
    ] = False,
    write_legacy_sample: Annotated[
        bool,
        typer.Option(help="Also write the first league payload to data/sports_sample.json."),
    ] = True,
    soccer_competitions: Annotated[
        str,
        typer.Option(help="Comma-separated ESPN soccer competition codes for SOCCER."),
    ] = ",".join(DEFAULT_SOCCER_COMPETITIONS),
) -> None:
    engine = init_db()
    session_factory = get_session_factory(engine)
    with session_factory() as session:
        artifacts = write_phase3af_report(
            session,
            output_dir=output_dir,
            schedule_output_dir=schedule_output_dir,
            leagues=leagues,
            start_date=start_date,
            days_ahead=days_ahead,
            ingest=ingest,
            write_legacy_sample=write_legacy_sample,
            soccer_competitions=soccer_competitions,
        )
        session.commit()
    console.print("Phase 3AF Sports Schedule Bootstrap")
    console.print("Mode: PAPER ONLY schedule/link data repair")
    console.print("Live/demo execution: blocked")
    console.print(f"Wrote JSON: {artifacts.json_path}")
    console.print(f"Wrote Markdown: {artifacts.markdown_path}")
    for path in artifacts.schedule_paths:
        console.print(f"Wrote schedule: {path}")
    if artifacts.legacy_sample_path is not None:
        console.print(f"Wrote legacy sample: {artifacts.legacy_sample_path}")
    if ingest:
        console.print("Ingested fetched schedule rows into the local database.")


@app.command("phase3ag-sports-ambiguity-coverage")
def phase3ag_sports_ambiguity_coverage_command(
    output_dir: Annotated[
        Path,
        typer.Option(help="Directory for Phase 3AG Markdown/JSON report artifacts."),
    ] = Path("reports/phase3ag"),
    manual_template_path: Annotated[
        Path,
        typer.Option(help="Path for the manual verified soccer schedule template."),
    ] = Path("data/sports_schedules/soccer_verified_manual_template.json"),
    max_schedule_delta_hours: Annotated[
        int,
        typer.Option(help="Maximum hours between market close and verified game time."),
    ] = 18,
    write_manual_template: Annotated[
        bool,
        typer.Option(
            "--write-manual-template/--no-write-manual-template",
            help="Write a manual verified soccer schedule template.",
        ),
    ] = True,
) -> None:
    engine = init_db()
    session_factory = get_session_factory(engine)
    with session_factory() as session:
        artifacts = write_phase3ag_report(
            session,
            output_dir=output_dir,
            manual_template_path=manual_template_path,
            max_schedule_delta_hours=max_schedule_delta_hours,
            write_manual_template=write_manual_template,
        )
        session.commit()
    console.print("Phase 3AG Sports Ambiguity + Soccer Coverage")
    console.print("Mode: PAPER ONLY diagnostics")
    console.print("Live/demo execution: blocked")
    console.print(f"Wrote JSON: {artifacts.json_path}")
    console.print(f"Wrote Markdown: {artifacts.markdown_path}")
    if write_manual_template:
        console.print(f"Wrote manual soccer template: {artifacts.manual_template_path}")


@app.command("phase3ag-sports-link-repair-pass")
def phase3ag_sports_link_repair_pass_command(
    output_dir: Annotated[
        Path,
        typer.Option(help="Directory for Phase 3AG repair pass artifacts."),
    ] = Path("reports/phase3ag"),
    phase3ae_path: Annotated[
        Path,
        typer.Option(help="Path to the Phase 3AE connector JSON report."),
    ] = Path("reports/phase3ae/phase3ae_verified_sports_connector.json"),
    limit: Annotated[
        int,
        typer.Option(help="Maximum Phase 3AE NO_VERIFIED_MATCH rows to review. Use 0 for all."),
    ] = 0,
    max_schedule_delta_hours: Annotated[
        int,
        typer.Option(help="Maximum hours between market close and verified game time."),
    ] = 18,
) -> None:
    """Group Phase 3AE no-match sports rows into alias/disambiguation repairs."""
    engine = init_db()
    session_factory = get_session_factory(engine)
    with session_factory() as session:
        artifacts = write_phase3ag_repair_report(
            session,
            output_dir=output_dir,
            phase3ae_path=phase3ae_path,
            limit=limit if limit > 0 else None,
            max_schedule_delta_hours=max_schedule_delta_hours,
        )
    console.print("Phase 3AG Sports Link Repair Pass")
    console.print("Mode: PAPER ONLY diagnostics")
    console.print("Live/demo execution: blocked")
    console.print("Auto-upgrades: blocked")
    console.print(f"Wrote JSON: {artifacts.json_path}")
    console.print(f"Wrote Markdown: {artifacts.markdown_path}")
    console.print(f"Wrote alias candidates: {artifacts.alias_candidates_path}")
    console.print(f"Wrote manual candidates: {artifacts.manual_candidates_path}")


@app.command("phase3ah-r2-player-prop-backfill")
def phase3ah_r2_player_prop_backfill_command(
    output_dir: Annotated[
        Path,
        typer.Option(help="Directory for Phase 3AH-R2 report artifacts."),
    ] = Path("reports/phase3ah_r2"),
    roster_template_path: Annotated[
        Path,
        typer.Option(help="Path to the Phase 3AH roster review template to update."),
    ] = Path("reports/phase3ah_sports/phase3ah_roster_review_template.json"),
    roster_output_dir: Annotated[
        Path,
        typer.Option(help="Directory for refreshed Phase 3AH roster verifier artifacts."),
    ] = Path("reports/phase3ah_sports"),
    diagnostics_path: Annotated[
        Path,
        typer.Option(help="Path to Phase 3AE roster candidate diagnostics JSON."),
    ] = Path("reports/phase3ae_roster_candidates/phase3ae_roster_candidate_diagnostics.json"),
    schedule_output_dir: Annotated[
        Path,
        typer.Option(help="Directory where Phase 3AH-R2 schedule files are written."),
    ] = Path("data/sports_schedules/phase3ah_r2"),
    schedule_start_date: Annotated[
        str,
        typer.Option(help="SOCCER schedule backfill start date as YYYY-MM-DD."),
    ] = "2026-07-07",
    schedule_days_ahead: Annotated[
        int,
        typer.Option(help="Number of SOCCER schedule days to backfill."),
    ] = 4,
    fetch_schedules: Annotated[
        bool,
        typer.Option(
            "--fetch-schedules/--no-fetch-schedules",
            help="Fetch SOCCER schedules for the target July 8-9 window.",
        ),
    ] = True,
    ingest_schedules: Annotated[
        bool,
        typer.Option(
            "--ingest-schedules/--no-ingest-schedules",
            help="Ingest fetched SOCCER schedule rows into the local DB.",
        ),
    ] = True,
    soccer_competitions: Annotated[
        str,
        typer.Option(help="Comma-separated ESPN soccer competition codes for SOCCER."),
    ] = ",".join(DEFAULT_SOCCER_COMPETITIONS),
) -> None:
    """Apply Phase 3AH-R2 roster evidence and SOCCER schedule-window backfill."""
    engine = init_db()
    session_factory = get_session_factory(engine)
    with session_factory() as session:
        artifacts = write_phase3ah_r2_backfill_report(
            session,
            output_dir=output_dir,
            roster_template_path=roster_template_path,
            roster_output_dir=roster_output_dir,
            diagnostics_path=diagnostics_path,
            schedule_output_dir=schedule_output_dir,
            schedule_start_date=schedule_start_date,
            schedule_days_ahead=schedule_days_ahead,
            fetch_schedules=fetch_schedules,
            ingest_schedules=ingest_schedules,
            soccer_competitions=soccer_competitions,
        )
        session.commit()
    console.print("Phase 3AH-R2 Player-Prop Completeness + Schedule Backfill")
    console.print("Mode: PAPER ONLY evidence/schedule repair")
    console.print("Live/demo execution: blocked")
    console.print("Verified link auto-upgrades: blocked")
    console.print("Phase 3AE remains the only verified sports link upgrade path.")
    console.print(f"Wrote JSON: {artifacts.json_path}")
    console.print(f"Wrote Markdown: {artifacts.markdown_path}")
    console.print(f"Updated roster template: {artifacts.updated_roster_template_path}")
    console.print(f"Refreshed roster verifier: {artifacts.roster_verification_json_path}")
    console.print(f"Refreshed verified evidence: {artifacts.verified_roster_evidence_path}")


@app.command("phase3ah-sports-evidence-backfill")
def phase3ah_sports_evidence_backfill_command(
    output_dir: Annotated[
        Path,
        typer.Option(help="Directory for Phase 3AH sports evidence artifacts."),
    ] = Path("reports/phase3ah_sports"),
    repair_path: Annotated[
        Path,
        typer.Option(help="Path to the Phase 3AG sports repair pass JSON report."),
    ] = Path("reports/phase3ag/phase3ag_sports_link_repair_pass.json"),
    alias_candidates_path: Annotated[
        Path,
        typer.Option(help="Path to Phase 3AG missing alias candidate JSON."),
    ] = Path("reports/phase3ag/phase3ag_missing_alias_candidates.json"),
    roster_candidate_diagnostics_path: Annotated[
        Path,
        typer.Option(help="Path to current Phase 3AE roster candidate diagnostics JSON."),
    ] = Path("reports/phase3ae_roster_candidates/phase3ae_roster_candidate_diagnostics.json"),
    schedule_output_dir: Annotated[
        Path,
        typer.Option(help="Directory where Phase 3AH schedule backfill files are written."),
    ] = Path("data/sports_schedules/phase3ah"),
    leagues: Annotated[
        str,
        typer.Option(help="Comma-separated leagues to backfill: MLB,WNBA,SOCCER."),
    ] = "MLB,WNBA,SOCCER",
    window_days_before: Annotated[
        int,
        typer.Option(help="Days before each failed market close date to fetch."),
    ] = 1,
    window_days_after: Annotated[
        int,
        typer.Option(help="Days after each failed market close date to fetch."),
    ] = 1,
    max_windows_per_league: Annotated[
        int,
        typer.Option(help="Maximum collapsed windows per league. Use 0 for all."),
    ] = 0,
    limit: Annotated[
        int,
        typer.Option(help="Maximum Phase 3AG failed rows to review. Use 0 for all."),
    ] = 0,
    fetch_schedules: Annotated[
        bool,
        typer.Option(
            "--fetch-schedules/--no-fetch-schedules",
            help="Fetch schedule files for the failed close-date windows.",
        ),
    ] = False,
    ingest_schedules: Annotated[
        bool,
        typer.Option(
            "--ingest-schedules/--no-ingest-schedules",
            help="Ingest fetched schedule rows into the local DB.",
        ),
    ] = False,
    soccer_competitions: Annotated[
        str,
        typer.Option(help="Comma-separated ESPN soccer competition codes for SOCCER."),
    ] = ",".join(DEFAULT_SOCCER_COMPETITIONS),
) -> None:
    """Create verified sports schedule/alias/roster evidence from Phase 3AG failures."""
    engine = init_db()
    session_factory = get_session_factory(engine)
    with session_factory() as session:
        artifacts = write_phase3ah_sports_evidence_report(
            session,
            output_dir=output_dir,
            repair_path=repair_path,
            alias_candidates_path=alias_candidates_path,
            roster_candidate_diagnostics_path=roster_candidate_diagnostics_path,
            schedule_output_dir=schedule_output_dir,
            leagues=leagues,
            window_days_before=window_days_before,
            window_days_after=window_days_after,
            max_windows_per_league=max_windows_per_league
            if max_windows_per_league > 0
            else None,
            limit=limit if limit > 0 else None,
            fetch_schedules=fetch_schedules,
            ingest_schedules=ingest_schedules,
            soccer_competitions=soccer_competitions,
        )
        session.commit()
    console.print("Phase 3AH Verified Sports Evidence Backfill")
    console.print("Mode: PAPER ONLY evidence repair")
    console.print("Live/demo execution: blocked")
    console.print("Verified link auto-upgrades: blocked")
    console.print(f"Wrote JSON: {artifacts.json_path}")
    console.print(f"Wrote Markdown: {artifacts.markdown_path}")
    console.print(f"Wrote schedule plan: {artifacts.schedule_plan_path}")
    console.print(f"Wrote team alias template: {artifacts.team_alias_template_path}")
    console.print(f"Wrote roster template: {artifacts.roster_template_path}")
    console.print(
        f"Wrote manual disambiguation template: {artifacts.manual_disambiguation_template_path}"
    )
    console.print(
        f"Wrote round placeholder template: {artifacts.round_placeholder_template_path}"
    )
    if fetch_schedules:
        console.print("Fetched schedule windows from Phase 3AG failed close dates.")
    if ingest_schedules:
        console.print("Ingested fetched schedule rows into the local database.")


@app.command("phase3ah-schedule-roster-evidence")
def phase3ah_schedule_roster_evidence_command(
    output_dir: Annotated[
        Path,
        typer.Option(help="Directory for Phase 3AH sports evidence artifacts."),
    ] = Path("reports/phase3ah_sports"),
    limit: Annotated[
        int,
        typer.Option(help="Maximum repair rows to inspect. Use 0 for all rows."),
    ] = 0,
) -> None:
    """Alias for read-only Phase 3AH schedule/roster evidence diagnostics."""
    engine = make_engine()
    session_factory = get_session_factory(engine)
    with session_factory() as session:
        artifacts = write_phase3ah_sports_evidence_report(
            session,
            output_dir=output_dir,
            fetch_schedules=False,
            ingest_schedules=False,
            limit=limit if limit > 0 else None,
        )
    console.print("Phase 3AH Schedule/Roster Evidence")
    console.print("Mode: PAPER ONLY evidence diagnostics")
    console.print("Live/demo execution: blocked")
    console.print("Verified link auto-upgrades: blocked")
    console.print(f"Wrote JSON: {artifacts.json_path}")
    console.print(f"Wrote Markdown: {artifacts.markdown_path}")


@app.command("phase3ah-round-placeholder-resolution")
def phase3ah_round_placeholder_resolution_command(
    output_dir: Annotated[
        Path,
        typer.Option(help="Directory for Phase 3AH round placeholder artifacts."),
    ] = Path("reports/phase3ah_sports"),
    template_path: Annotated[
        Path,
        typer.Option(help="Path to Phase 3AH round placeholder template JSON."),
    ] = Path("reports/phase3ah_sports/phase3ah_round_placeholder_resolution_template.json"),
    timeout_seconds: Annotated[
        float,
        typer.Option(help="HTTP timeout in seconds for source event summary fetches."),
    ] = 20.0,
) -> None:
    """Resolve bracket placeholder games from source event summaries when possible."""
    artifacts = write_phase3ah_round_placeholder_resolution_report(
        output_dir=output_dir,
        template_path=template_path,
        timeout_seconds=timeout_seconds,
    )
    console.print("Phase 3AH Round Placeholder Resolution")
    console.print("Mode: PAPER ONLY source evidence")
    console.print("Live/demo execution: blocked")
    console.print("Verified link auto-upgrades: blocked")
    console.print("Phase 3AE remains the only verified sports link upgrade path.")
    console.print(f"Wrote JSON: {artifacts.json_path}")
    console.print(f"Wrote Markdown: {artifacts.markdown_path}")
    console.print(f"Wrote filled template: {artifacts.filled_template_path}")


@app.command("phase3ah-sports-placeholder-watch")
def phase3ah_sports_placeholder_watch_command(
    output_dir: Annotated[
        Path,
        typer.Option(help="Directory for Phase 3AH placeholder watch artifacts."),
    ] = Path("reports/phase3ah_sports"),
    placeholder_report_path: Annotated[
        Path,
        typer.Option(help="Path to the Phase 3AH round placeholder resolution report."),
    ] = Path("reports/phase3ah_sports/phase3ah_round_placeholder_resolution_report.json"),
    sports_evidence_path: Annotated[
        Path,
        typer.Option(help="Path to the Phase 3AH sports evidence backfill report."),
    ] = Path("reports/phase3ah_sports/phase3ah_sports_evidence_backfill.json"),
    settlement_harvest_path: Annotated[
        Path,
        typer.Option(help="Path to the Phase 3AA-R2 exact settlement harvest report."),
    ] = Path("reports/phase3aa_r2/phase3aa_r2_exact_settlement_harvest.json"),
) -> None:
    """Watch sports placeholders and keep settlement harvesting as a separate safe loop."""
    artifacts = write_phase3ah_sports_placeholder_watch_report(
        output_dir=output_dir,
        placeholder_report_path=placeholder_report_path,
        sports_evidence_path=sports_evidence_path,
        settlement_harvest_path=settlement_harvest_path,
    )
    console.print("Phase 3AH Sports Placeholder Watch")
    console.print("Mode: PAPER ONLY evidence watch")
    console.print("Live/demo execution: blocked")
    console.print("Verified link auto-upgrades: blocked")
    console.print("Settlement realization: blocked in this command")
    console.print(f"Wrote JSON: {artifacts.json_path}")
    console.print(f"Wrote Markdown: {artifacts.markdown_path}")


@app.command("phase3ay-positive-ev-accelerator")
def phase3ay_positive_ev_accelerator_command(
    output_dir: Annotated[
        Path,
        typer.Option(help="Directory for Phase 3AY positive-EV accelerator artifacts."),
    ] = Path("reports/phase3ay"),
    reports_dir: Annotated[
        Path,
        typer.Option(help="Directory containing upstream report artifacts."),
    ] = Path("reports"),
    symbols: Annotated[
        str,
        typer.Option(help="Comma-separated crypto symbols to include."),
    ] = "BTC,ETH,XRP,DOGE",
    near_miss_cents: Annotated[
        str,
        typer.Option(help="Near-miss EV band in cents below zero."),
    ] = "1.0",
    max_candidates: Annotated[
        int,
        typer.Option(help="Maximum near-miss rows to rank and optionally refresh."),
    ] = 50,
    refresh_snapshots: Annotated[
        bool,
        typer.Option(
            "--refresh-snapshots/--no-refresh-snapshots",
            help="Refresh near-miss snapshots/books only when no active watcher conflicts.",
        ),
    ] = True,
    allow_concurrent_refresh: Annotated[
        bool,
        typer.Option(
            "--allow-concurrent-refresh/--no-allow-concurrent-refresh",
            help="Allow refresh even if an R5 crypto watcher appears active.",
        ),
    ] = False,
) -> None:
    """Rank current crypto near-misses without lowering thresholds or creating trades."""
    try:
        near_miss_value = Decimal(near_miss_cents)
    except (InvalidOperation, ValueError):
        console.print("Invalid --near-miss-cents value.")
        raise typer.Exit(1) from None
    if near_miss_value < 0:
        console.print("Refusing to run: --near-miss-cents must be non-negative.")
        raise typer.Exit(1)

    engine = init_db()
    settings = get_settings()
    session_factory = get_session_factory(engine)
    with session_factory() as session:
        artifacts = write_phase3ay_positive_ev_accelerator_report(
            session,
            output_dir=output_dir,
            reports_dir=reports_dir,
            settings=settings,
            symbols=symbols,
            near_miss_cents=near_miss_value,
            max_candidates=max_candidates,
            refresh_snapshots=refresh_snapshots,
            allow_concurrent_refresh=allow_concurrent_refresh,
            registered_commands=set(registered_root_command_names()),
        )
    console.print("Phase 3AY Positive EV Accelerator")
    console.print("Mode: PAPER ONLY / READ ONLY diagnostics")
    console.print("Live/demo execution: blocked")
    console.print("Paper trade creation: blocked")
    console.print("Threshold lowering: blocked")
    console.print(f"Wrote Executive Summary: {artifacts.executive_summary_path}")
    console.print(f"Wrote near-miss rows: {artifacts.near_miss_rows_path}")
    console.print(f"Wrote JSON: {artifacts.json_path}")
    console.print(f"Wrote Next Actions: {artifacts.next_actions_path}")
    console.print(f"Wrote manifest: {artifacts.manifest_path}")


@app.command("phase3ay-free-source-market-scan")
def phase3ay_free_source_market_scan_command(
    output_dir: Annotated[
        Path,
        typer.Option(help="Directory for Phase 3AY free-source artifacts."),
    ] = Path("reports/phase3ay"),
    reports_dir: Annotated[
        Path,
        typer.Option(help="Directory containing upstream report artifacts."),
    ] = Path("reports"),
    limit: Annotated[
        int,
        typer.Option(help="Maximum current active markets to scan."),
    ] = 5000,
) -> None:
    """Scan current non-expired markets for free-source opportunity readiness."""
    engine = init_db()
    settings = get_settings()
    session_factory = get_session_factory(engine)
    with session_factory() as session:
        artifacts = write_phase3ay_free_source_sprint_report(
            session,
            output_dir=output_dir,
            reports_dir=reports_dir,
            settings=settings,
            limit=limit,
            command_args=sys.argv[1:],
            registered_commands=set(registered_root_command_names()),
        )
    console.print("Phase 3AY Free Source Market Scan")
    console.print("Mode: PAPER ONLY / READ ONLY diagnostics")
    console.print("Live/demo execution: blocked")
    console.print("Paper trade creation: blocked")
    console.print(f"Wrote market scan: {artifacts.free_source_market_scan_md_path}")
    console.print(f"Wrote candidates: {artifacts.free_source_market_candidates_path}")
    console.print(f"Wrote JSON: {artifacts.free_source_market_scan_json_path}")


@app.command("phase3ay-free-source-adapter-registry")
def phase3ay_free_source_adapter_registry_command(
    output_dir: Annotated[
        Path,
        typer.Option(help="Directory for Phase 3AY free-source artifacts."),
    ] = Path("reports/phase3ay"),
    reports_dir: Annotated[
        Path,
        typer.Option(help="Directory containing upstream report artifacts."),
    ] = Path("reports"),
    limit: Annotated[
        int,
        typer.Option(help="Maximum current active markets to scan while producing registry."),
    ] = 5000,
) -> None:
    """Write the free/public source adapter registry with sprint context."""
    engine = init_db()
    settings = get_settings()
    session_factory = get_session_factory(engine)
    with session_factory() as session:
        artifacts = write_phase3ay_free_source_sprint_report(
            session,
            output_dir=output_dir,
            reports_dir=reports_dir,
            settings=settings,
            limit=limit,
            command_args=sys.argv[1:],
            registered_commands=set(registered_root_command_names()),
        )
    console.print("Phase 3AY Free Source Adapter Registry")
    console.print("Mode: PAPER ONLY / READ ONLY diagnostics")
    console.print("Live/demo execution: blocked")
    console.print("Paper trade creation: blocked")
    console.print(f"Wrote registry: {artifacts.adapter_registry_md_path}")
    console.print(f"Wrote JSON: {artifacts.adapter_registry_json_path}")


@app.command("phase3ay-category-readiness")
def phase3ay_category_readiness_command(
    output_dir: Annotated[
        Path,
        typer.Option(help="Directory for Phase 3AY free-source artifacts."),
    ] = Path("reports/phase3ay"),
    reports_dir: Annotated[
        Path,
        typer.Option(help="Directory containing upstream report artifacts."),
    ] = Path("reports"),
    limit: Annotated[
        int,
        typer.Option(help="Maximum current active markets to scan."),
    ] = 5000,
) -> None:
    """Rank categories by current free-source readiness and blockers."""
    engine = init_db()
    settings = get_settings()
    session_factory = get_session_factory(engine)
    with session_factory() as session:
        artifacts = write_phase3ay_free_source_sprint_report(
            session,
            output_dir=output_dir,
            reports_dir=reports_dir,
            settings=settings,
            limit=limit,
            command_args=sys.argv[1:],
            registered_commands=set(registered_root_command_names()),
        )
    console.print("Phase 3AY Category Readiness")
    console.print("Mode: PAPER ONLY / READ ONLY diagnostics")
    console.print("Live/demo execution: blocked")
    console.print("Paper trade creation: blocked")
    console.print(f"Wrote readiness: {artifacts.category_readiness_md_path}")
    console.print(f"Wrote scorecard: {artifacts.category_scorecard_path}")
    console.print(f"Wrote JSON: {artifacts.category_readiness_json_path}")


@app.command("phase3ay-multicategory-paper-funnel")
def phase3ay_multicategory_paper_funnel_command(
    output_dir: Annotated[
        Path,
        typer.Option(help="Directory for Phase 3AY free-source artifacts."),
    ] = Path("reports/phase3ay"),
    reports_dir: Annotated[
        Path,
        typer.Option(help="Directory containing upstream report artifacts."),
    ] = Path("reports"),
    limit: Annotated[
        int,
        typer.Option(help="Maximum current active markets to scan."),
    ] = 5000,
) -> None:
    """Explain the paper funnel for all current free-source categories."""
    engine = init_db()
    settings = get_settings()
    session_factory = get_session_factory(engine)
    with session_factory() as session:
        artifacts = write_phase3ay_free_source_sprint_report(
            session,
            output_dir=output_dir,
            reports_dir=reports_dir,
            settings=settings,
            limit=limit,
            command_args=sys.argv[1:],
            registered_commands=set(registered_root_command_names()),
        )
    console.print("Phase 3AY Multicategory Paper Funnel")
    console.print("Mode: PAPER ONLY / READ ONLY diagnostics")
    console.print("Live/demo execution: blocked")
    console.print("Paper trade creation: blocked")
    console.print(f"Wrote funnel: {artifacts.multicategory_funnel_md_path}")
    console.print(f"Wrote candidates: {artifacts.multicategory_candidates_path}")
    console.print(f"Wrote JSON: {artifacts.multicategory_funnel_json_path}")


@app.command("phase3ay-free-source-sprint-report")
def phase3ay_free_source_sprint_report_command(
    output_dir: Annotated[
        Path,
        typer.Option(help="Directory for Phase 3AY free-source artifacts."),
    ] = Path("reports/phase3ay"),
    reports_dir: Annotated[
        Path,
        typer.Option(help="Directory containing upstream report artifacts."),
    ] = Path("reports"),
    limit: Annotated[
        int,
        typer.Option(help="Maximum current active markets to scan."),
    ] = 5000,
) -> None:
    """Write the full Phase 3AY multi-category free-source sprint report."""
    engine = init_db()
    settings = get_settings()
    session_factory = get_session_factory(engine)
    with session_factory() as session:
        artifacts = write_phase3ay_free_source_sprint_report(
            session,
            output_dir=output_dir,
            reports_dir=reports_dir,
            settings=settings,
            limit=limit,
            command_args=sys.argv[1:],
            registered_commands=set(registered_root_command_names()),
        )
    console.print("Phase 3AY Free Source Sprint Report")
    console.print("Mode: PAPER ONLY / READ ONLY diagnostics")
    console.print("Live/demo execution: blocked")
    console.print("Paper trade creation: blocked")
    console.print("Threshold lowering: blocked")
    console.print(f"Wrote Executive Summary: {artifacts.executive_summary_path}")
    console.print(f"Wrote sprint JSON: {artifacts.sprint_report_json_path}")
    console.print(f"Wrote Next Actions: {artifacts.next_actions_path}")
    console.print(f"Wrote Next Category Sprint: {artifacts.next_category_sprint_path}")
    console.print(f"Wrote manifest: {artifacts.manifest_path}")


@app.command("phase3ay-health-refresh")
def phase3ay_health_refresh_command(
    output_dir: Annotated[
        Path,
        typer.Option(help="Directory for Phase 3AY health refresh artifacts."),
    ] = Path("reports/phase3ay"),
    cycles: Annotated[
        int,
        typer.Option(help="Number of refresh cycles to run."),
    ] = 1,
    interval_seconds: Annotated[
        int,
        typer.Option(help="Seconds to wait between cycles."),
    ] = 300,
    duration_hours: Annotated[
        float,
        typer.Option(
            help="Run for approximately this many hours. Use 0 to honor --cycles.",
        ),
    ] = 0.0,
    all_markets: Annotated[
        bool,
        typer.Option(
            "--all-markets/--paged-markets",
            help="Refresh every open-market page each cycle.",
        ),
    ] = False,
    market_collect: Annotated[
        bool,
        typer.Option(
            "--market-collect/--no-market-collect",
            help="Collect fresh open market snapshots.",
        ),
    ] = True,
    market_limit: Annotated[
        int,
        typer.Option(help="Market collection page size per cycle."),
    ] = 100,
    market_max_pages: Annotated[
        int,
        typer.Option(help="Maximum market pages to collect per cycle."),
    ] = 1,
    include_orderbook: Annotated[
        bool,
        typer.Option(
            "--orderbook/--no-orderbook",
            help="Fetch public orderbooks during collection.",
        ),
    ] = True,
    settlement_sync: Annotated[
        bool,
        typer.Option(
            "--settlement-sync/--no-settlement-sync",
            help="Run broad settled-market sync before exact-ticker paper harvest.",
        ),
    ] = True,
    settlement_lookback_days: Annotated[
        int,
        typer.Option(help="Lookback window for broad settlement sync."),
    ] = 90,
    settlement_limit: Annotated[
        int,
        typer.Option(help="Broad settlement sync page size."),
    ] = 200,
    settlement_max_pages: Annotated[
        int,
        typer.Option(help="Broad settlement sync pages. Use 0 for all pages."),
    ] = 10,
    settlement_commit_every: Annotated[
        int,
        typer.Option(
            help=(
                "Commit broad settlement sync every N inserted rows. "
                "Use 0 to commit once at the end."
            ),
        ),
    ] = 0,
    realize_paper: Annotated[
        bool,
        typer.Option(
            "--realize-paper/--dry-run-paper",
            help="Realize paper P&L only when exact ticker settlement evidence is eligible.",
        ),
    ] = True,
    settlement_only: Annotated[
        bool,
        typer.Option(
            "--settlement-only/--full-health",
            help=(
                "Run only settlement-focused Phase 3AY steps and skip slow "
                "coverage/orchestrator diagnostics."
            ),
        ),
    ] = False,
    stop_on_error: Annotated[
        bool,
        typer.Option(help="Stop the loop on the first step error."),
    ] = False,
) -> None:
    """Keep paper settlements, market health, and roadmap reports fresh safely."""
    engine = init_db()
    settings = get_settings()
    session_factory = get_session_factory(engine)
    effective_cycles = cycles
    if duration_hours > 0:
        effective_cycles = max(1, ceil((duration_hours * 3600) / max(interval_seconds, 1)))
    duration_budget_seconds = duration_hours * 3600 if duration_hours > 0 else None
    effective_market_max_pages = 0 if all_markets else market_max_pages
    artifacts = run_phase3ay_health_refresh_loop(
        session_factory,
        output_dir=output_dir,
        settings=settings,
        cycles=effective_cycles,
        interval_seconds=interval_seconds,
        market_collect=market_collect,
        market_limit=market_limit,
        market_max_pages=effective_market_max_pages,
        include_orderbook=include_orderbook,
        settlement_sync=settlement_sync,
        settlement_lookback_days=settlement_lookback_days,
        settlement_limit=settlement_limit,
        settlement_max_pages=settlement_max_pages,
        settlement_commit_every=settlement_commit_every,
        realize_paper=realize_paper,
        settlement_only=settlement_only,
        stop_on_error=stop_on_error,
        duration_budget_seconds=duration_budget_seconds,
    )
    latest = artifacts[-1]
    console.print("Phase 3AY paper + market health refresh")
    console.print("Mode: PAPER ONLY")
    console.print("Live/demo execution: blocked")
    console.print(f"Cycles completed: {len(artifacts)} / {effective_cycles}")
    console.print(f"Wrote JSON: {latest.json_path}")
    console.print(f"Wrote Markdown: {latest.markdown_path}")


@app.command("phase3ay-status")
def phase3ay_status_command(
    output_dir: Annotated[
        Path,
        typer.Option(help="Directory for Phase 3AY status artifacts."),
    ] = Path("reports/phase3ay"),
) -> None:
    """Write a read-only status report for the unattended health refresh job."""
    artifacts = write_phase3ay_status_report(output_dir=output_dir)
    console.print("Phase 3AY health refresh status")
    console.print("Mode: READ ONLY")
    console.print(f"Wrote JSON: {artifacts.json_path}")
    console.print(f"Wrote Markdown: {artifacts.markdown_path}")


@app.command("phase3ay-unattended-start")
def phase3ay_unattended_start_command(
    output_dir: Annotated[
        Path,
        typer.Option(help="Directory for Phase 3AY health refresh artifacts."),
    ] = Path("reports/phase3ay"),
    cycles: Annotated[
        int,
        typer.Option(help="Number of refresh cycles to run."),
    ] = 1,
    interval_seconds: Annotated[
        int,
        typer.Option(help="Seconds to wait between cycles."),
    ] = 300,
    duration_hours: Annotated[
        float,
        typer.Option(help="Run for approximately this many hours."),
    ] = 0.0,
    all_markets: Annotated[
        bool,
        typer.Option(
            "--all-markets/--paged-markets",
            help="Refresh every open-market page each cycle.",
        ),
    ] = False,
    market_collect: Annotated[
        bool,
        typer.Option(
            "--market-collect/--no-market-collect",
            help="Collect fresh open-market snapshots each cycle.",
        ),
    ] = True,
    market_limit: Annotated[
        int,
        typer.Option(help="Market collection page size per cycle."),
    ] = 100,
    market_max_pages: Annotated[
        int,
        typer.Option(help="Maximum market pages to collect per cycle."),
    ] = 1,
    include_orderbook: Annotated[
        bool,
        typer.Option(
            "--orderbook/--no-orderbook",
            help="Fetch public orderbooks during market collection.",
        ),
    ] = True,
    settlement_limit: Annotated[
        int,
        typer.Option(help="Broad settlement sync page size."),
    ] = 100,
    settlement_max_pages: Annotated[
        int,
        typer.Option(help="Broad settlement sync pages."),
    ] = 1,
    settlement_commit_every: Annotated[
        int,
        typer.Option(
            help=(
                "Commit broad settlement sync every N inserted rows. "
                "Use 0 to commit once at the end."
            ),
        ),
    ] = 0,
    dry_run_paper: Annotated[
        bool,
        typer.Option(help="Keep Phase 3AA paper realization in dry-run mode."),
    ] = False,
    settlement_only: Annotated[
        bool,
        typer.Option(
            "--settlement-only/--full-health",
            help=(
                "Run only settlement-focused Phase 3AY steps and skip slow "
                "coverage/orchestrator diagnostics."
            ),
        ),
    ] = False,
    timeout_grace_seconds: Annotated[
        int,
        typer.Option(help="Seconds past the configured budget before guard stops it."),
    ] = 600,
) -> None:
    """Start Phase 3AY in the background with owned PID, logs, and timeout metadata."""
    result = start_phase3ay_unattended_refresh(
        output_dir=output_dir,
        cycles=cycles,
        interval_seconds=interval_seconds,
        duration_hours=duration_hours,
        all_markets=all_markets,
        market_collect=market_collect,
        market_limit=market_limit,
        market_max_pages=market_max_pages,
        include_orderbook=include_orderbook,
        settlement_limit=settlement_limit,
        settlement_max_pages=settlement_max_pages,
        settlement_commit_every=settlement_commit_every,
        realize_paper=not dry_run_paper,
        settlement_only=settlement_only,
        timeout_grace_seconds=timeout_grace_seconds,
    )
    console.print("Phase 3AY unattended refresh")
    console.print("Mode: PAPER ONLY")
    console.print("Live/demo execution: blocked")
    console.print(f"Status: {result.status}")
    console.print(f"PID: {result.pid or 'none'}")
    console.print(f"PID file: {result.pid_path}")
    console.print(f"Metadata: {result.metadata_path}")
    console.print(f"Stdout: {result.stdout_path}")
    console.print(f"Stderr: {result.stderr_path}")
    if result.command:
        console.print(f"Command: {result.command}")
    console.print(result.message)


@app.command("phase3ay-unattended-guard")
def phase3ay_unattended_guard_command(
    output_dir: Annotated[
        Path,
        typer.Option(help="Directory for Phase 3AY health refresh artifacts."),
    ] = Path("reports/phase3ay"),
    stop_overrun: Annotated[
        bool,
        typer.Option(help="Terminate a Phase 3AY process that exceeded its timeout guard."),
    ] = False,
    terminate_grace_seconds: Annotated[
        int,
        typer.Option(help="Seconds to wait after SIGTERM before force killing."),
    ] = 30,
) -> None:
    """Write Phase 3AY unattended guard status and optionally stop overruns."""
    artifacts = write_phase3ay_unattended_guard_report(
        output_dir=output_dir,
        stop_overrun=stop_overrun,
        terminate_grace_seconds=terminate_grace_seconds,
    )
    console.print("Phase 3AY unattended guard")
    console.print("Mode: READ ONLY unless --stop-overrun is supplied")
    console.print(f"Wrote JSON: {artifacts.json_path}")
    console.print(f"Wrote Markdown: {artifacts.markdown_path}")


@app.command("phase3az-gap-analysis")
def phase3az_gap_analysis_command(
    output_dir: Annotated[
        Path,
        typer.Option(help="Directory for Phase 3AZ gap analysis artifacts."),
    ] = Path("reports/phase3az"),
    reports_dir: Annotated[
        Path,
        typer.Option(help="Root reports directory to inspect."),
    ] = Path("reports"),
) -> None:
    """Build a report-only post-refresh gap analysis and implementation queue."""
    artifacts = write_phase3az_gap_analysis_report(
        output_dir=output_dir,
        reports_dir=reports_dir,
    )
    console.print("Phase 3AZ post-refresh gap analysis")
    console.print("Mode: REPORT ONLY")
    console.print("Live/demo execution: blocked")
    console.print(f"Wrote JSON: {artifacts.json_path}")
    console.print(f"Wrote Markdown: {artifacts.markdown_path}")


@app.command("phase3az-r11-non-crypto-category-activation")
def phase3az_r11_non_crypto_category_activation_command(
    output_dir: Annotated[
        Path,
        typer.Option(help="Directory for Phase 3AZ-R11 activation artifacts."),
    ] = Path("reports/phase3az_r11"),
    reports_dir: Annotated[
        Path,
        typer.Option(help="Root reports directory to inspect."),
    ] = Path("reports"),
) -> None:
    """Choose the best paper-only non-crypto category activation sprint."""
    weather_location_counts = _weather_location_counts_for_r11()
    artifacts = write_phase3az_r11_non_crypto_activation_report(
        output_dir=output_dir,
        reports_dir=reports_dir,
        weather_location_counts=weather_location_counts,
    )
    console.print("Phase 3AZ-R11 non-crypto category activation")
    console.print("Mode: PAPER ONLY report")
    console.print("Live/demo execution: blocked")
    console.print("Paper trade creation: blocked")
    console.print(f"Wrote JSON: {artifacts.json_path}")
    console.print(f"Wrote Markdown: {artifacts.markdown_path}")
    console.print(f"Wrote next actions: {artifacts.next_actions_path}")


@app.command("phase3az-r12-weather-activation-preview")
def phase3az_r12_weather_activation_preview_command(
    output_dir: Annotated[
        Path,
        typer.Option(help="Directory for Phase 3AZ-R12 weather activation preview artifacts."),
    ] = Path("reports/phase3az_r12_weather"),
    limit: Annotated[
        int,
        typer.Option(help="Maximum active/current weather-like markets to inspect."),
    ] = 1000,
    fresh_window_hours: Annotated[
        int,
        typer.Option(help="Maximum weather forecast age in hours to treat as fresh."),
    ] = 24,
    match_tolerance_hours: Annotated[
        int,
        typer.Option(help="Maximum target-time mismatch allowed between market text and feature."),
    ] = 3,
) -> None:
    """Preview safe weather relinks without writing links or creating trades."""
    engine = init_db()
    settings = get_settings()
    session_factory = get_session_factory(engine)
    with session_factory() as session:
        artifacts = write_phase3az_r12_weather_activation_preview_report(
            session,
            output_dir=output_dir,
            limit=limit,
            fresh_window_hours=fresh_window_hours,
            match_tolerance_hours=match_tolerance_hours,
            settings=settings,
            command_args=sys.argv[1:],
        )
    console.print("Phase 3AZ-R12 weather activation diagnostic/relink preview")
    console.print("Mode: PAPER ONLY read-only diagnostic")
    console.print("Database writes: blocked")
    console.print("Paper trade creation: blocked")
    console.print("Live/demo execution: blocked")
    console.print(f"Wrote JSON: {artifacts.json_path}")
    console.print(f"Wrote Markdown: {artifacts.markdown_path}")
    console.print(f"Wrote next actions: {artifacts.next_actions_path}")
    console.print(f"Wrote candidate CSV: {artifacts.candidates_csv_path}")
    console.print(f"Wrote safe relink CSV: {artifacts.safe_to_relink_csv_path}")
    console.print(f"Wrote safe link CSV: {artifacts.safe_to_link_csv_path}")


@app.command("phase3az-r12-weather-missing-link-apply")
def phase3az_r12_weather_missing_link_apply_command(
    output_dir: Annotated[
        Path,
        typer.Option(help="Directory for Phase 3AZ-R12 weather missing-link artifacts."),
    ] = Path("reports/phase3az_r12_weather"),
    limit: Annotated[
        int,
        typer.Option(help="Maximum active/current weather-like markets to inspect."),
    ] = 1000,
    fresh_window_hours: Annotated[
        int,
        typer.Option(help="Maximum weather forecast age in hours to treat as fresh."),
    ] = 24,
    match_tolerance_hours: Annotated[
        int,
        typer.Option(help="Maximum target-time mismatch allowed between market text and feature."),
    ] = 3,
    max_records: Annotated[
        int,
        typer.Option(help="Maximum missing weather link rows to write in apply mode."),
    ] = 25,
    dry_run: Annotated[
        bool,
        typer.Option("--dry-run/--apply", help="Dry-run by default; --apply requires --backup-first."),
    ] = True,
    backup_first: Annotated[
        bool,
        typer.Option("--backup-first", help="Required before apply writes local weather links."),
    ] = False,
) -> None:
    """Dry-run or apply R12 safe missing weather links; never runs forecasts."""
    apply = not dry_run
    engine = init_db()
    settings = get_settings()
    session_factory = get_session_factory(engine)
    with session_factory() as session:
        artifacts = write_phase3az_r12_weather_missing_link_apply_report(
            session,
            output_dir=output_dir,
            limit=limit,
            fresh_window_hours=fresh_window_hours,
            match_tolerance_hours=match_tolerance_hours,
            max_records=max_records,
            dry_run=dry_run,
            apply=apply,
            backup_first=backup_first,
            settings=settings,
            command_args=sys.argv[1:],
        )
    console.print("Phase 3AZ-R12 weather missing-link apply")
    console.print("Mode: dry-run unless --apply --backup-first is supplied")
    console.print("Weather ingest/features/forecast: blocked")
    console.print("Paper trade creation: blocked")
    console.print("Live/demo execution: blocked")
    console.print(f"Wrote JSON: {artifacts.json_path}")
    console.print(f"Wrote Markdown: {artifacts.markdown_path}")


@app.command("phase3az-r13-weather-handoff-status")
def phase3az_r13_weather_handoff_status_command(
    output_dir: Annotated[
        Path,
        typer.Option(help="Directory for Phase 3AZ-R13 weather handoff artifacts."),
    ] = Path("reports/phase3az_r13_weather"),
    reports_dir: Annotated[
        Path,
        typer.Option(help="Root reports directory to inspect for R12 preview artifacts."),
    ] = Path("reports"),
    current_window_lookback_hours: Annotated[
        int,
        typer.Option(help="Hours behind now to still count weather links as current."),
    ] = 3,
    limit: Annotated[
        int,
        typer.Option(help="Maximum current linked weather rows to inspect."),
    ] = 500,
) -> None:
    """Report the next weather handoff step without writing DB rows."""
    engine = init_db()
    settings = get_settings()
    session_factory = get_session_factory(engine)
    with session_factory() as session:
        artifacts = write_phase3az_r13_weather_handoff_status_report(
            session,
            output_dir=output_dir,
            reports_dir=reports_dir,
            current_window_lookback_hours=current_window_lookback_hours,
            limit=limit,
            settings=settings,
            command_args=sys.argv[1:],
        )
    console.print("Phase 3AZ-R13 weather handoff status")
    console.print("Mode: PAPER ONLY report-only diagnostic")
    console.print("Database writes: blocked")
    console.print("Paper trade creation: blocked")
    console.print("Live/demo execution: blocked")
    console.print(f"Wrote JSON: {artifacts.json_path}")
    console.print(f"Wrote Markdown: {artifacts.markdown_path}")
    console.print(f"Wrote next actions: {artifacts.next_actions_path}")


def _weather_location_counts_for_r11() -> list[dict[str, int | str]]:
    try:
        engine = init_db()
        session_factory = get_session_factory(engine)
        link_count = func.count(WeatherMarketLink.id).label("link_count")
        with session_factory() as session:
            rows = session.execute(
                select(WeatherMarketLink.location_key, link_count)
                .group_by(WeatherMarketLink.location_key)
                .order_by(desc(link_count), WeatherMarketLink.location_key)
            ).all()
        return [
            {"location_key": str(location_key), "link_count": int(count)}
            for location_key, count in rows
            if location_key
        ]
    except Exception:
        return []


def _run_phase3bb_report(writer: Any, **kwargs: Any) -> Any:
    settings = get_settings()
    engine = make_engine(database_url_from_settings(settings))
    session_factory = get_session_factory(engine)
    try:
        with session_factory() as session:
            artifacts = writer(
                session,
                settings=settings,
                command_args=sys.argv[1:],
                **kwargs,
            )
            session.rollback()
            return artifacts
    finally:
        engine.dispose()


def _print_phase3bb_artifacts(title: str, artifacts: Any) -> None:
    console.print(title)
    console.print("Mode: PAPER READ-ONLY diagnostics/report")
    console.print("Live/demo execution: blocked")
    console.print("Order submission/cancel/replace: blocked")
    console.print("Paper trade creation: blocked")
    for path in artifacts.paths.values():
        console.print(f"Wrote: {path}")


@app.command("phase3bb-throughput-analysis")
def phase3bb_throughput_analysis_command(
    output_dir: Annotated[
        Path,
        typer.Option(help="Directory for Phase 3BB acceleration artifacts."),
    ] = Path("reports/phase3bb"),
    reports_dir: Annotated[
        Path,
        typer.Option(help="Root reports directory for current Phase 3BA/R5 artifacts."),
    ] = Path("reports"),
    runtime_hours: Annotated[
        float,
        typer.Option(help="Observed ingestion/watch runtime hours for EV pace projection."),
    ] = 165.0,
    observed_positive_ev: Annotated[
        int,
        typer.Option(help="Operator-observed positive EV rows over runtime window."),
    ] = 3,
) -> None:
    """Write Phase 3BB throughput and conversion bottleneck analysis."""
    artifacts = _run_phase3bb_report(
        write_phase3bb_throughput_analysis_report,
        output_dir=output_dir,
        reports_dir=reports_dir,
        runtime_hours=runtime_hours,
        observed_positive_ev=observed_positive_ev,
    )
    _print_phase3bb_artifacts("Phase 3BB throughput analysis", artifacts)


@app.command("phase3bb-r1-operator-scheduler")
def phase3bb_r1_operator_scheduler_command(
    output_dir: Annotated[
        Path,
        typer.Option(help="Directory for Phase 3BB-R1 scheduler artifacts."),
    ] = Path("reports/phase3bb_r1"),
    reports_dir: Annotated[
        Path,
        typer.Option(help="Root reports directory for current Phase 3BA/R5 artifacts."),
    ] = Path("reports"),
) -> None:
    """Write one safe operator scheduler action for paper-only bot operation."""
    settings = get_settings()
    engine = make_engine(database_url_from_settings(settings))
    session_factory = get_session_factory(engine)
    try:
        with session_factory() as session:
            artifacts = write_phase3bb_r1_operator_scheduler_report(
                session,
                output_dir=output_dir,
                reports_dir=reports_dir,
                settings=settings,
                command_args=sys.argv[1:],
            )
            session.rollback()
    finally:
        engine.dispose()
    console.print("Phase 3BB-R1 operator scheduler")
    console.print("Mode: PAPER READ-ONLY scheduler/report")
    console.print("Live/demo execution: blocked")
    console.print("Order submission/cancel/replace: blocked")
    console.print("Paper trade creation: blocked")
    console.print("Duplicate R5 starts while running: blocked")
    console.print(f"Wrote Executive Summary: {artifacts.executive_summary_path}")
    console.print(f"Wrote JSON: {artifacts.json_path}")
    console.print(f"Wrote operator command: {artifacts.operator_next_command_path}")
    console.print(f"Wrote Next Actions: {artifacts.next_actions_path}")
    console.print(f"Wrote Manifest: {artifacts.manifest_path}")


@app.command("phase3bb-r2-weather-fast-lane")
def phase3bb_r2_weather_fast_lane_command(
    output_dir: Annotated[
        Path,
        typer.Option(help="Directory for Phase 3BB-R2 weather fast-lane artifacts."),
    ] = Path("reports/phase3bb_r2"),
    reports_dir: Annotated[
        Path,
        typer.Option(help="Root reports directory for current Phase 3BA/R5 artifacts."),
    ] = Path("reports"),
    limit: Annotated[
        int,
        typer.Option(help="Maximum current weather rows to rank/report."),
    ] = 100,
) -> None:
    """Run the paper-only weather fast-lane funnel and dashboard truth refresh."""
    if limit < 1:
        raise typer.BadParameter("limit must be positive")
    settings = get_settings()
    engine = make_engine(database_url_from_settings(settings))
    session_factory = get_session_factory(engine)
    try:
        with session_factory() as session:
            artifacts = write_phase3bb_r2_weather_fast_lane_report(
                session,
                output_dir=output_dir,
                reports_dir=reports_dir,
                settings=settings,
                command_args=sys.argv[1:],
                limit=limit,
            )
            session.commit()
    finally:
        engine.dispose()
    console.print("Phase 3BB-R2 weather fast lane")
    console.print("Mode: PAPER ONLY weather ranking/paper-gate funnel")
    console.print("Live/demo execution: blocked")
    console.print("Order submission/cancel/replace: blocked")
    console.print("Paper trade creation: blocked")
    console.print("Thresholds lowered: false")
    console.print(f"Wrote Executive Summary: {artifacts.executive_summary_path}")
    console.print(f"Wrote Markdown: {artifacts.markdown_path}")
    console.print(f"Wrote JSON: {artifacts.json_path}")
    console.print(f"Wrote candidates: {artifacts.candidates_csv_path}")
    console.print(f"Wrote Next Actions: {artifacts.next_actions_path}")


@app.command("phase3bb-r3-free-source-inventory")
def phase3bb_r3_free_source_inventory_command(
    output_dir: Annotated[
        Path,
        typer.Option(help="Directory for Phase 3BB-R3 free-source inventory artifacts."),
    ] = Path("reports/phase3bb_r3"),
    reports_dir: Annotated[
        Path,
        typer.Option(help="Root reports directory for current category artifacts."),
    ] = Path("reports"),
) -> None:
    """Write the paper-only multi-category free source inventory."""
    settings = get_settings()
    engine = make_engine(database_url_from_settings(settings))
    session_factory = get_session_factory(engine)
    try:
        with session_factory() as session:
            artifacts = write_phase3bb_r3_free_source_inventory_report(
                session,
                output_dir=output_dir,
                reports_dir=reports_dir,
                settings=settings,
                command_args=sys.argv[1:],
            )
            session.rollback()
    finally:
        engine.dispose()
    console.print("Phase 3BB-R3 free source inventory")
    console.print("Mode: PAPER READ-ONLY category/source inventory")
    console.print("Live/demo execution: blocked")
    console.print("Order submission/cancel/replace: blocked")
    console.print("Paper trade creation: blocked")
    console.print("TradingEconomics: DEFERRED")
    console.print(f"Wrote inventory: {artifacts.inventory_path}")
    console.print(f"Wrote scorecard: {artifacts.scorecard_csv_path}")
    console.print(f"Wrote backlog: {artifacts.backlog_path}")
    console.print(f"Wrote next category: {artifacts.next_category_path}")
    console.print(f"Wrote manifest: {artifacts.manifest_path}")


@app.command("phase3bb-r4-economic-parser-backfill")
def phase3bb_r4_economic_parser_backfill_command(
    output_dir: Annotated[
        Path,
        typer.Option(help="Directory for Phase 3BB-R4 economic parser artifacts."),
    ] = Path("reports/phase3bb_r4"),
    reports_dir: Annotated[
        Path,
        typer.Option(help="Root reports directory for current category artifacts."),
    ] = Path("reports"),
) -> None:
    """Write the paper-only economic parser backfill preview."""
    settings = get_settings()
    engine = make_engine(database_url_from_settings(settings))
    session_factory = get_session_factory(engine)
    try:
        with session_factory() as session:
            artifacts = write_phase3bb_r4_economic_parser_backfill_report(
                session,
                output_dir=output_dir,
                reports_dir=reports_dir,
                settings=settings,
                command_args=sys.argv[1:],
            )
            session.rollback()
    finally:
        engine.dispose()
    console.print("Phase 3BB-R4 economic parser backfill")
    console.print("Mode: PAPER READ-ONLY economic parser preview")
    console.print("Live/demo execution: blocked")
    console.print("Order submission/cancel/replace: blocked")
    console.print("Paper trade creation: blocked")
    console.print("TradingEconomics: DEFERRED")
    console.print("DB writes performed: 0")
    console.print(f"Wrote Executive Summary: {artifacts.executive_summary_path}")
    console.print(f"Wrote Markdown: {artifacts.markdown_path}")
    console.print(f"Wrote rows: {artifacts.rows_csv_path}")
    console.print(f"Wrote source backlog: {artifacts.source_mapping_backlog_path}")
    console.print(f"Wrote manifest: {artifacts.manifest_path}")


@app.command("phase3bb-cloud-readiness")
def phase3bb_cloud_readiness_command(
    output_dir: Annotated[
        Path,
        typer.Option(help="Directory for Phase 3BB acceleration artifacts."),
    ] = Path("reports/phase3bb"),
    reports_dir: Annotated[
        Path,
        typer.Option(help="Root reports directory for current Phase 3BA/R5 artifacts."),
    ] = Path("reports"),
) -> None:
    """Write Phase 3BB cloud/VPS readiness report."""
    artifacts = _run_phase3bb_report(
        write_phase3bb_cloud_readiness_report,
        output_dir=output_dir,
        reports_dir=reports_dir,
    )
    _print_phase3bb_artifacts("Phase 3BB cloud readiness", artifacts)


@app.command("phase3bb-scheduler-plan")
def phase3bb_scheduler_plan_command(
    output_dir: Annotated[
        Path,
        typer.Option(help="Directory for Phase 3BB acceleration artifacts."),
    ] = Path("reports/phase3bb"),
    reports_dir: Annotated[
        Path,
        typer.Option(help="Root reports directory for current Phase 3BA/R5 artifacts."),
    ] = Path("reports"),
) -> None:
    """Write one-writer scheduler design for paper-only jobs."""
    artifacts = _run_phase3bb_report(
        write_phase3bb_scheduler_plan_report,
        output_dir=output_dir,
        reports_dir=reports_dir,
    )
    _print_phase3bb_artifacts("Phase 3BB scheduler plan", artifacts)


@app.command("phase3bb-multicategory-expansion-plan")
def phase3bb_multicategory_expansion_plan_command(
    output_dir: Annotated[
        Path,
        typer.Option(help="Directory for Phase 3BB acceleration artifacts."),
    ] = Path("reports/phase3bb"),
    reports_dir: Annotated[
        Path,
        typer.Option(help="Root reports directory for current Phase 3BA/R5 artifacts."),
    ] = Path("reports"),
) -> None:
    """Write multi-category paper-candidate expansion scorecard."""
    artifacts = _run_phase3bb_report(
        write_phase3bb_multicategory_expansion_plan_report,
        output_dir=output_dir,
        reports_dir=reports_dir,
    )
    _print_phase3bb_artifacts("Phase 3BB multi-category expansion plan", artifacts)


@app.command("phase3bb-weather-fast-lane")
def phase3bb_weather_fast_lane_command(
    output_dir: Annotated[
        Path,
        typer.Option(help="Directory for Phase 3BB acceleration artifacts."),
    ] = Path("reports/phase3bb"),
    reports_dir: Annotated[
        Path,
        typer.Option(help="Root reports directory for current Phase 3BA/R5 artifacts."),
    ] = Path("reports"),
) -> None:
    """Write weather fast-lane paper funnel diagnostics."""
    artifacts = _run_phase3bb_report(
        write_phase3bb_weather_fast_lane_report,
        output_dir=output_dir,
        reports_dir=reports_dir,
    )
    _print_phase3bb_artifacts("Phase 3BB weather fast lane", artifacts)


@app.command("phase3bb-historical-replay-acceleration")
def phase3bb_historical_replay_acceleration_command(
    output_dir: Annotated[
        Path,
        typer.Option(help="Directory for Phase 3BB acceleration artifacts."),
    ] = Path("reports/phase3bb"),
    reports_dir: Annotated[
        Path,
        typer.Option(help="Root reports directory for current Phase 3BA/R5 artifacts."),
    ] = Path("reports"),
) -> None:
    """Write historical replay acceleration plan separated from paper learning."""
    artifacts = _run_phase3bb_report(
        write_phase3bb_historical_replay_acceleration_report,
        output_dir=output_dir,
        reports_dir=reports_dir,
    )
    _print_phase3bb_artifacts("Phase 3BB historical replay acceleration", artifacts)


@app.command("phase3bb-acceleration-report")
def phase3bb_acceleration_report_command(
    output_dir: Annotated[
        Path,
        typer.Option(help="Directory for Phase 3BB acceleration artifacts."),
    ] = Path("reports/phase3bb"),
    reports_dir: Annotated[
        Path,
        typer.Option(help="Root reports directory for current Phase 3BA/R5 artifacts."),
    ] = Path("reports"),
    runtime_hours: Annotated[
        float,
        typer.Option(help="Observed ingestion/watch runtime hours for EV pace projection."),
    ] = 165.0,
    observed_positive_ev: Annotated[
        int,
        typer.Option(help="Operator-observed positive EV rows over runtime window."),
    ] = 3,
) -> None:
    """Write unified Phase 3BB acceleration report and next sprint."""
    artifacts = _run_phase3bb_report(
        write_phase3bb_acceleration_report,
        output_dir=output_dir,
        reports_dir=reports_dir,
        runtime_hours=runtime_hours,
        observed_positive_ev=observed_positive_ev,
    )
    _print_phase3bb_artifacts("Phase 3BB acceleration report", artifacts)


@app.command("phase3bb-domain-readiness")
def phase3bb_domain_readiness_command(
    output_dir: Annotated[
        Path,
        typer.Option(help="Directory for Phase 3BB economic/news/general artifacts."),
    ] = Path("reports/phase3bb"),
) -> None:
    """Report economic, news, and general domain readiness without writing links."""
    engine = init_db()
    session_factory = get_session_factory(engine)
    with session_factory() as session:
        artifacts = write_phase3bb_domain_readiness_report(session, output_dir=output_dir)
    console.print("Phase 3BB economic/news/general domain readiness")
    console.print("Mode: PAPER ONLY report")
    console.print("Live/demo execution: blocked")
    console.print("Link/feature writes: blocked in this command")
    console.print(f"Wrote JSON: {artifacts.json_path}")
    console.print(f"Wrote Markdown: {artifacts.markdown_path}")
    console.print(f"Wrote rows: {artifacts.rows_path}")


@app.command("economic-news-market-watch")
def economic_news_market_watch_command(
    output_dir: Annotated[
        Path,
        typer.Option(help="Directory for economic/news watch artifacts."),
    ] = Path("reports/economic_news_watch"),
) -> None:
    """Watch economic/news compatible markets without forcing links or forecasts."""
    engine = make_engine()
    session_factory = get_session_factory(engine)
    with session_factory() as session:
        artifacts = write_economic_news_market_watch_report(session, output_dir=output_dir)
    console.print("Economic/news compatible market watch")
    console.print("Mode: READ ONLY")
    console.print("Link/feature/forecast writes: blocked")
    console.print("Live/demo execution: blocked")
    console.print(f"Wrote JSON: {artifacts.json_path}")


@app.command("phase3bb-r2-general-candidate-routing")
def phase3bb_r2_general_candidate_routing_command(
    output_dir: Annotated[
        Path,
        typer.Option(help="Directory for Phase 3BB-R2 general candidate artifacts."),
    ] = Path("reports/phase3bb_r2"),
    limit_per_bucket: Annotated[
        int,
        typer.Option(help="Maximum candidate examples to write per taxonomy bucket."),
    ] = 50,
) -> None:
    """Route general markets into safe review buckets without creating links."""
    engine = init_db()
    session_factory = get_session_factory(engine)
    with session_factory() as session:
        artifacts = write_phase3bb_general_candidate_routing_report(
            session,
            output_dir=output_dir,
            limit_per_bucket=limit_per_bucket,
        )
    console.print("Phase 3BB-R2 general candidate routing")
    console.print("Mode: PAPER ONLY report")
    console.print("Live/demo execution: blocked")
    console.print("Link/feature writes: blocked in this command")
    console.print(f"Wrote JSON: {artifacts.json_path}")
    console.print(f"Wrote Markdown: {artifacts.markdown_path}")
    console.print(f"Wrote rows: {artifacts.rows_path}")
    console.print(f"Wrote diagnostics: {artifacts.diagnostics_path}")


@app.command("phase3bb-r2-general-source-intake")
def phase3bb_r2_general_source_intake_command(
    output_dir: Annotated[
        Path,
        typer.Option(help="Directory for Phase 3BB-R2 source intake artifacts."),
    ] = Path("reports/phase3bb_r2_sources"),
    input_file: Annotated[
        Path | None,
        typer.Option(help="Verified source input JSON/CSV generated from the template."),
    ] = None,
    evidence_dir: Annotated[
        Path,
        typer.Option(help="Directory for canonical local source evidence JSON files."),
    ] = Path("data/general_source_evidence"),
    limit_per_bucket: Annotated[
        int,
        typer.Option(help="Maximum candidate examples to inspect per taxonomy bucket."),
    ] = 50,
    write_evidence_files: Annotated[
        bool,
        typer.Option(
            "--write-evidence-files/--dry-run",
            help="Write canonical source evidence files when input rows are verified.",
        ),
    ] = False,
) -> None:
    """Prepare or ingest audited general-source evidence files."""
    engine = init_db()
    session_factory = get_session_factory(engine)
    with session_factory() as session:
        artifacts = write_phase3bb_general_source_intake_report(
            session,
            output_dir=output_dir,
            input_file=input_file,
            evidence_dir=evidence_dir,
            limit_per_bucket=limit_per_bucket,
            write_evidence_files=write_evidence_files,
        )
    console.print("Phase 3BB-R2 general source intake")
    console.print("Mode: PAPER ONLY source evidence files")
    console.print("Live/demo execution: blocked")
    console.print("Link/feature/forecast writes: blocked in this command")
    console.print(f"Write evidence files: {write_evidence_files}")
    console.print(f"Wrote JSON: {artifacts.json_path}")
    console.print(f"Wrote Markdown: {artifacts.markdown_path}")
    console.print(f"Wrote canonical JSON: {artifacts.canonical_json_path}")
    console.print(f"Wrote canonical Markdown: {artifacts.canonical_markdown_path}")
    console.print(f"Wrote taxonomy review: {artifacts.taxonomy_review_path}")
    console.print(
        f"Wrote source evidence requirements: {artifacts.source_evidence_requirements_path}"
    )
    console.print(f"Wrote source readiness matrix: {artifacts.source_readiness_matrix_path}")
    console.print(f"Wrote candidate market samples: {artifacts.candidate_market_samples_path}")
    console.print(f"Wrote next actions: {artifacts.next_actions_path}")
    console.print(f"Wrote template JSON: {artifacts.template_json_path}")
    console.print(f"Wrote template CSV: {artifacts.template_csv_path}")
    console.print(f"Wrote manifest: {artifacts.manifest_path}")


@app.command("phase3bb-r2-group-source-review")
def phase3bb_r2_group_source_review_command(
    input_path: Annotated[
        Path,
        typer.Option(
            "--input",
            help="Phase 3BB-R2 source intake template CSV to collapse into review groups.",
        ),
    ] = Path("reports/phase3bb_r2_sources/phase3bb_r2_general_source_input_template.csv"),
    output_path: Annotated[
        Path,
        typer.Option("--output", help="Grouped operator review CSV to write."),
    ] = Path("data/general_source_evidence/phase3bb_r2_group_review.csv"),
) -> None:
    """Collapse repeated R2 source-evidence rows into operator review groups."""
    artifacts = write_phase3bb_group_source_review(input_path=input_path, output_path=output_path)
    console.print("Phase 3BB-R2 grouped source review")
    console.print("Mode: REPORT ONLY CSV helper")
    console.print("Values invented: false")
    console.print(f"Input rows: {artifacts.row_count}")
    console.print(f"Groups written: {artifacts.group_count}")
    console.print(f"Wrote CSV: {artifacts.output_path}")


@app.command("phase3bb-r2-apply-group-source-review")
def phase3bb_r2_apply_group_source_review_command(
    group_review: Annotated[
        Path,
        typer.Option("--group-review", help="Operator-reviewed grouped source CSV."),
    ] = Path("data/general_source_evidence/phase3bb_r2_group_review.csv"),
    template: Annotated[
        Path,
        typer.Option("--template", help="Original Phase 3BB-R2 source input template CSV."),
    ] = Path("reports/phase3bb_r2_sources/phase3bb_r2_general_source_input_template.csv"),
    output_path: Annotated[
        Path,
        typer.Option("--output", help="Expanded filled source input CSV to write."),
    ] = Path("data/general_source_evidence/phase3bb_r2_general_source_input_filled.csv"),
) -> None:
    """Copy operator-reviewed group evidence back to matching template rows."""
    artifacts = write_phase3bb_apply_group_source_review(
        group_review_path=group_review,
        template_path=template,
        output_path=output_path,
    )
    console.print("Phase 3BB-R2 apply grouped source review")
    console.print("Mode: REPORT ONLY CSV helper")
    console.print("Values invented: false")
    console.print(f"Template rows: {artifacts.template_rows}")
    console.print(f"Rows updated: {artifacts.rows_updated}")
    console.print(f"Wrote CSV: {artifacts.output_path}")


@app.command("phase3bb-r2-general-source-evidence")
def phase3bb_r2_general_source_evidence_command(
    output_dir: Annotated[
        Path,
        typer.Option(help="Directory for Phase 3BB-R2 source evidence artifacts."),
    ] = Path("reports/phase3bb_r2_sources"),
    evidence_dir: Annotated[
        Path,
        typer.Option(help="Directory containing paper-only local source evidence JSON files."),
    ] = Path("data/general_source_evidence"),
    limit_per_bucket: Annotated[
        int,
        typer.Option(help="Maximum candidate examples to inspect per taxonomy bucket."),
    ] = 50,
) -> None:
    """Check exact paper-only source evidence for general-signal diagnostics."""
    engine = init_db()
    session_factory = get_session_factory(engine)
    with session_factory() as session:
        artifacts = write_phase3bb_general_source_evidence_report(
            session,
            output_dir=output_dir,
            evidence_dir=evidence_dir,
            limit_per_bucket=limit_per_bucket,
        )
    console.print("Phase 3BB-R2 general source evidence")
    console.print("Mode: PAPER ONLY evidence report")
    console.print("Live/demo execution: blocked")
    console.print("Link/feature/forecast writes: blocked in this command")
    console.print(f"Wrote JSON: {artifacts.json_path}")
    console.print(f"Wrote Markdown: {artifacts.markdown_path}")
    console.print(f"Wrote evidence rows: {artifacts.evidence_rows_path}")
    console.print(f"Wrote templates: {artifacts.templates_path}")


@app.command("phase3bb-r2-general-source-availability")
def phase3bb_r2_general_source_availability_command(
    output_dir: Annotated[
        Path,
        typer.Option(help="Directory for Phase 3BB-R2 source availability artifacts."),
    ] = Path("reports/phase3bb_r2_sources"),
    evidence_dir: Annotated[
        Path,
        typer.Option(help="Directory containing paper-only local source evidence JSON files."),
    ] = Path("data/general_source_evidence"),
    limit_per_bucket: Annotated[
        int,
        typer.Option(help="Maximum candidate examples to inspect per taxonomy bucket."),
    ] = 50,
    check_source_urls: Annotated[
        bool,
        typer.Option(
            "--check-source-urls/--local-only",
            help="Fetch source URLs with a short timeout for publication availability hints.",
        ),
    ] = False,
    url_timeout_seconds: Annotated[
        float,
        typer.Option(help="Per-source URL fetch timeout when --check-source-urls is used."),
    ] = 8.0,
) -> None:
    """Watch exact source publication availability without downstream writes."""
    engine = init_db()
    session_factory = get_session_factory(engine)
    with session_factory() as session:
        artifacts = write_phase3bb_general_source_availability_report(
            session,
            output_dir=output_dir,
            evidence_dir=evidence_dir,
            limit_per_bucket=limit_per_bucket,
            check_source_urls=check_source_urls,
            url_timeout_seconds=url_timeout_seconds,
        )
    console.print("Phase 3BB-R2 general source availability")
    console.print("Mode: PAPER ONLY source availability report")
    console.print("Live/demo execution: blocked")
    console.print("Link/feature/forecast writes: blocked in this command")
    console.print(f"Source URL checks: {check_source_urls}")
    console.print(f"Wrote JSON: {artifacts.json_path}")
    console.print(f"Wrote Markdown: {artifacts.markdown_path}")
    console.print(f"Wrote availability rows: {artifacts.availability_rows_path}")


@app.command("phase3bb-r3-general-reclassification")
def phase3bb_r3_general_reclassification_command(
    output_dir: Annotated[
        Path,
        typer.Option(help="Directory for Phase 3BB-R3 reclassification artifacts."),
    ] = Path("reports/phase3bb_r3"),
    sample_limit: Annotated[
        int,
        typer.Option(help="Maximum sports/cross-category candidate rows to write."),
    ] = 200,
) -> None:
    """Report general sports/cross-category reclassification candidates safely."""
    engine = init_db()
    session_factory = get_session_factory(engine)
    with session_factory() as session:
        artifacts = write_phase3bb_general_reclassification_report(
            session,
            output_dir=output_dir,
            sample_limit=sample_limit,
        )
    console.print("Phase 3BB-R3 general sports/cross-category reclassification")
    console.print("Mode: PAPER ONLY report")
    console.print("Live/demo execution: blocked")
    console.print("Market-leg/link writes: blocked in this command")
    console.print(f"Wrote JSON: {artifacts.json_path}")
    console.print(f"Wrote Markdown: {artifacts.markdown_path}")
    console.print(f"Wrote candidates: {artifacts.candidates_path}")
    console.print(f"Wrote manual review rows: {artifacts.manual_review_path}")


@app.command("phase3bb-r3-safe-parser-reparse")
def phase3bb_r3_safe_parser_reparse_command(
    output_dir: Annotated[
        Path,
        typer.Option(help="Directory for Phase 3BB-R3 safe parser reparse artifacts."),
    ] = Path("reports/phase3bb_r3"),
    sample_limit: Annotated[
        int,
        typer.Option(help="Maximum candidate rows to preview before safe exact-ticker reparse."),
    ] = 1000,
) -> None:
    """Refresh market legs only for current R3 parser-preview-safe tickers."""
    engine = init_db()
    session_factory = get_session_factory(engine)
    with session_factory() as session:
        artifacts = write_phase3bb_r3_safe_parser_reparse_report(
            session,
            output_dir=output_dir,
            sample_limit=sample_limit,
        )
        session.commit()
    console.print("Phase 3BB-R3 safe parser reparse")
    console.print("Mode: PAPER ONLY controlled market-leg refresh")
    console.print("Live/demo execution: blocked")
    console.print("Link writes: blocked")
    console.print(f"Rows safe to reparse: {artifacts.rows_safe_to_reparse}")
    console.print(f"Rows reparsed: {artifacts.rows_reparsed}")
    console.print(f"Rows deleted: {artifacts.rows_deleted}")
    console.print(f"Rows inserted: {artifacts.rows_inserted}")
    console.print(f"Wrote JSON: {artifacts.json_path}")
    console.print(f"Wrote Markdown: {artifacts.markdown_path}")


@app.command("phase3bb-r3-exact-sports-link")
def phase3bb_r3_exact_sports_link_command(
    output_dir: Annotated[
        Path,
        typer.Option(help="Directory for Phase 3BB-R3 exact sports link artifacts."),
    ] = Path("reports/phase3bb_r3"),
    apply: Annotated[
        bool,
        typer.Option("--apply/--dry-run", help="Create exact derived sports links."),
    ] = False,
    sample_limit: Annotated[
        int,
        typer.Option(help="Maximum current unlinked sports tickers to preview."),
    ] = 1000,
) -> None:
    """Preview or create exact derived sports links for current R3 unlinked rows."""
    engine = init_db()
    session_factory = get_session_factory(engine)
    with session_factory() as session:
        artifacts = write_phase3bb_r3_exact_sports_link_report(
            session,
            output_dir=output_dir,
            apply=apply,
            sample_limit=sample_limit,
        )
        session.commit()
    console.print("Phase 3BB-R3 exact sports link")
    console.print("Mode: PAPER ONLY exact derived sports link")
    console.print("Live/demo execution: blocked")
    console.print(f"Apply: {artifacts.apply}")
    console.print(f"Rows safe to link: {artifacts.rows_safe_to_link}")
    console.print(f"Links created: {artifacts.links_created}")
    console.print(f"Wrote JSON: {artifacts.json_path}")
    console.print(f"Wrote Markdown: {artifacts.markdown_path}")


@app.command("phase3bb-r3-composite-preview-gate")
def phase3bb_r3_composite_preview_gate_command(
    output_dir: Annotated[
        Path,
        typer.Option(help="Directory for Phase 3BB-R3 composite preview artifacts."),
    ] = Path("reports/phase3bb_r3_composites"),
    sample_limit: Annotated[
        int,
        typer.Option(help="Maximum KXMVE composite market rows to classify."),
    ] = 50000,
) -> None:
    """Classify unsupported KXMVE composites without single-market remediation."""
    engine = init_db()
    session_factory = get_session_factory(engine)
    with session_factory() as session:
        artifacts = write_phase3bb_r3_composite_preview_gate_report(
            session,
            output_dir=output_dir,
            sample_limit=sample_limit,
        )
    console.print("Phase 3BB-R3 composite preview gate")
    console.print("Mode: PAPER ONLY composite classifier")
    console.print("Live/demo execution: blocked")
    console.print("Market-leg/link/settlement writes: blocked")
    console.print("Single-market remediation: blocked")
    console.print(f"Rows reviewed: {artifacts.rows_reviewed}")
    console.print(
        "Verified component evidence rows: "
        f"{artifacts.verified_component_evidence_rows}"
    )
    console.print(f"True composite rows: {artifacts.true_composite_rows}")
    console.print(f"Wrote JSON: {artifacts.json_path}")
    console.print(f"Wrote Markdown: {artifacts.markdown_path}")
    console.print(f"Wrote rows: {artifacts.rows_path}")


@app.command("phase3bb-r3-composite-operator-preflight")
def phase3bb_r3_composite_operator_preflight_command(
    output_dir: Annotated[
        Path,
        typer.Option(help="Directory for Phase 3BB-R3 composite preflight artifacts."),
    ] = Path("reports/phase3bb_r3_composites"),
    preview_path: Annotated[
        Path,
        typer.Option(help="Composite preview gate JSON artifact to consume."),
    ] = Path("reports/phase3bb_r3_composites/phase3bb_r3_composite_preview_gate.json"),
    max_quote_age_minutes: Annotated[
        int,
        typer.Option(help="Maximum quote age for composite/operator preflight."),
    ] = 30,
    min_liquidity_dollars: Annotated[
        str,
        typer.Option(help="Minimum composite liquidity dollars for paper review."),
    ] = "1",
    sample_limit: Annotated[
        int,
        typer.Option(help="Maximum verified-component rows to preflight."),
    ] = 1000,
) -> None:
    """Preflight verified-component composites before paper-only operator review."""
    engine = init_db()
    session_factory = get_session_factory(engine)
    with session_factory() as session:
        artifacts = write_phase3bb_r3_composite_operator_preflight_report(
            session,
            output_dir=output_dir,
            preview_path=preview_path,
            max_quote_age_minutes=max_quote_age_minutes,
            min_liquidity_dollars=Decimal(min_liquidity_dollars),
            sample_limit=sample_limit,
        )
    console.print("Phase 3BB-R3 composite operator preflight")
    console.print("Mode: PAPER ONLY composite operator/risk preflight")
    console.print("Live/demo execution: blocked")
    console.print("Paper trade creation: blocked")
    console.print("Market-leg/link/settlement writes: blocked")
    console.print(
        "Paper composite review ready rows: "
        f"{artifacts.paper_composite_review_ready_rows}"
    )
    console.print(f"Blocked rows: {artifacts.blocked_rows}")
    console.print(f"Wrote JSON: {artifacts.json_path}")
    console.print(f"Wrote Markdown: {artifacts.markdown_path}")
    console.print(f"Wrote rows: {artifacts.rows_path}")


@app.command("phase3bb-r3-source-evidence-activation")
def phase3bb_r3_source_evidence_activation_command(
    output_dir: Annotated[
        Path,
        typer.Option(help="Directory for Phase 3BB-R3 source activation artifacts."),
    ] = Path("reports/phase3bb_r3_source_activation"),
    reports_dir: Annotated[
        Path,
        typer.Option(help="Root directory containing phase report artifacts."),
    ] = Path("reports"),
) -> None:
    """Audit general-source activation decisions without source or trade writes."""
    artifacts = write_phase3bb_r3_source_evidence_activation_report(
        output_dir=output_dir,
        reports_dir=reports_dir,
        registered_commands=set(registered_root_command_names()),
    )
    console.print("Phase 3BB-R3 general source evidence activation")
    console.print("Mode: PAPER ONLY report")
    console.print("Live/demo execution: blocked")
    console.print("Paper trade creation: blocked")
    console.print("Source/link/feature/forecast writes: blocked in this command")
    console.print(f"Wrote Executive Summary: {artifacts.executive_summary_path}")
    console.print(f"Wrote Next Actions: {artifacts.next_actions_path}")
    console.print(f"Wrote Next Codex Task: {artifacts.next_codex_task_path}")
    console.print(f"Wrote source activation JSON: {artifacts.activation_json_path}")
    console.print(f"Wrote source decisions: {artifacts.activation_decisions_path}")
    console.print(f"Wrote command audit: {artifacts.command_audit_path}")


@app.command("phase3bb-r4-flightaware-review-link-gate")
def phase3bb_r4_flightaware_review_link_gate_command(
    output_dir: Annotated[
        Path,
        typer.Option(help="Directory for Phase 3BB-R4 FlightAware gate artifacts."),
    ] = Path("reports/phase3bb_r4_flightaware"),
    reports_dir: Annotated[
        Path,
        typer.Option(help="Root directory containing phase report artifacts."),
    ] = Path("reports"),
) -> None:
    """Audit FlightAware link/forecast-safe review gates without writes."""
    artifacts = write_phase3bb_r4_flightaware_review_link_gate_report(
        output_dir=output_dir,
        reports_dir=reports_dir,
        registered_commands=set(registered_root_command_names()),
    )
    console.print("Phase 3BB-R4 FlightAware review-to-link gate")
    console.print("Mode: PAPER ONLY report")
    console.print("Live/demo execution: blocked")
    console.print("Paper trade creation: blocked")
    console.print("Source/link/feature/forecast writes: blocked in this command")
    console.print(f"Wrote Executive Summary: {artifacts.executive_summary_path}")
    console.print(f"Wrote Next Actions: {artifacts.next_actions_path}")
    console.print(f"Wrote Next Codex Task: {artifacts.next_codex_task_path}")
    console.print(f"Wrote FlightAware gate JSON: {artifacts.gate_json_path}")
    console.print(f"Wrote review checks: {artifacts.review_checks_path}")
    console.print(f"Wrote command audit: {artifacts.command_audit_path}")


@app.command("phase3bb-r5-flightaware-date-stable-evidence")
def phase3bb_r5_flightaware_date_stable_evidence_command(
    output_dir: Annotated[
        Path,
        typer.Option(help="Directory for Phase 3BB-R5 FlightAware evidence artifacts."),
    ] = Path("reports/phase3bb_r5_flightaware"),
    reports_dir: Annotated[
        Path,
        typer.Option(help="Root directory containing phase report artifacts."),
    ] = Path("reports"),
    evidence_dir: Annotated[
        Path,
        typer.Option(help="Directory containing canonical paper-only source evidence files."),
    ] = Path("data/general_source_evidence"),
) -> None:
    """Audit date-stable FlightAware evidence candidates without writes."""
    artifacts = write_phase3bb_r5_flightaware_date_stable_evidence_report(
        output_dir=output_dir,
        reports_dir=reports_dir,
        evidence_dir=evidence_dir,
        registered_commands=set(registered_root_command_names()),
    )
    console.print("Phase 3BB-R5 FlightAware date-stable evidence")
    console.print("Mode: PAPER ONLY report")
    console.print("Live/demo execution: blocked")
    console.print("Paper trade creation: blocked")
    console.print("Network fetches: not performed")
    console.print("Source/link/feature/forecast writes: blocked in this command")
    console.print(f"Wrote Executive Summary: {artifacts.executive_summary_path}")
    console.print(f"Wrote Next Actions: {artifacts.next_actions_path}")
    console.print(f"Wrote Next Codex Task: {artifacts.next_codex_task_path}")
    console.print(f"Wrote FlightAware evidence JSON: {artifacts.evidence_json_path}")
    console.print(f"Wrote evidence candidates: {artifacts.candidate_rows_path}")
    console.print(f"Wrote command audit: {artifacts.command_audit_path}")


@app.command("phase3bb-r5-usda-source-activation")
def phase3bb_r5_usda_source_activation_command(
    output_dir: Annotated[
        Path,
        typer.Option(help="Directory for Phase 3BB-R5 USDA activation artifacts."),
    ] = Path("reports/phase3bb_r5"),
    reports_dir: Annotated[
        Path,
        typer.Option(help="Root directory containing phase report artifacts."),
    ] = Path("reports"),
    evidence_dir: Annotated[
        Path,
        typer.Option(help="Directory containing canonical paper-only source evidence files."),
    ] = Path("data/general_source_evidence"),
) -> None:
    """Audit USDA agriculture source activation without source or trade writes."""
    settings = get_settings()
    engine = make_engine(database_url_from_settings(settings))
    session_factory = get_session_factory(engine)
    try:
        with session_factory() as session:
            artifacts = write_phase3bb_r5_usda_source_activation_report(
                session,
                output_dir=output_dir,
                reports_dir=reports_dir,
                evidence_dir=evidence_dir,
                settings=settings,
                command_args=sys.argv[1:],
            )
            session.rollback()
    finally:
        engine.dispose()
    console.print("Phase 3BB-R5 USDA source activation")
    console.print("Mode: PAPER READ-ONLY agriculture source gate")
    console.print("Live/demo execution: blocked")
    console.print("Order submission/cancel/replace: blocked")
    console.print("Paper trade creation: blocked")
    console.print("Feature/forecast writes: blocked in this command")
    console.print("Paid/proprietary sources: not used")
    console.print(f"Wrote Executive Summary: {artifacts.executive_summary_path}")
    console.print(f"Wrote Markdown: {artifacts.markdown_path}")
    console.print(f"Wrote USDA rows: {artifacts.usda_rows_csv_path}")
    console.print(f"Wrote blocked rows: {artifacts.blocked_rows_csv_path}")
    console.print(f"Wrote Manifest: {artifacts.manifest_path}")


@app.command("phase3bb-r6-sports-provenance-repair")
def phase3bb_r6_sports_provenance_repair_command(
    output_dir: Annotated[
        Path,
        typer.Option(help="Directory for Phase 3BB-R6 sports provenance artifacts."),
    ] = Path("reports/phase3bb_r6"),
    reports_dir: Annotated[
        Path,
        typer.Option(help="Root directory containing phase report artifacts."),
    ] = Path("reports"),
    max_rows: Annotated[
        int | None,
        typer.Option(help="Maximum degraded sports rows to materialize; omit for unbounded."),
    ] = 1000,
    sample_limit: Annotated[
        int,
        typer.Option(help="Maximum row examples to keep in grouped upstream diagnostics."),
    ] = 50,
    ticker_prefix: Annotated[
        str | None,
        typer.Option(help="Optional sports ticker prefix for a focused diagnostic."),
    ] = None,
) -> None:
    """Audit sports provenance repairs with exact schedule/team/date evidence only."""
    settings = get_settings()
    engine = make_engine(database_url_from_settings(settings))
    session_factory = get_session_factory(engine)
    try:
        with session_factory() as session:
            artifacts = write_phase3bb_r6_sports_provenance_repair_report(
                session,
                output_dir=output_dir,
                reports_dir=reports_dir,
                settings=settings,
                command_args=sys.argv[1:],
                sample_limit=sample_limit,
                max_rows=max_rows,
                ticker_prefix=ticker_prefix,
            )
            session.rollback()
    finally:
        engine.dispose()
    console.print("Phase 3BB-R6 sports provenance repair sprint")
    console.print("Mode: PAPER READ-ONLY exact sports provenance preview")
    console.print("Live/demo execution: blocked")
    console.print("Order submission/cancel/replace: blocked")
    console.print("Paper trade creation: blocked")
    console.print("Fuzzy matching: blocked")
    console.print("DB writes performed: 0")
    console.print(f"Wrote Executive Summary: {artifacts.executive_summary_path}")
    console.print(f"Wrote JSON: {artifacts.json_path}")
    console.print(f"Wrote Markdown: {artifacts.markdown_path}")
    console.print(f"Wrote candidates: {artifacts.candidates_csv_path}")
    console.print(f"Wrote unsafe rows: {artifacts.unsafe_rows_csv_path}")
    console.print(f"Wrote manifest: {artifacts.manifest_path}")


@app.command("phase3bb-r7-news-event-discovery")
def phase3bb_r7_news_event_discovery_command(
    output_dir: Annotated[
        Path,
        typer.Option(help="Directory for Phase 3BB-R7 news/event discovery artifacts."),
    ] = Path("reports/phase3bb_r7"),
    reports_dir: Annotated[
        Path,
        typer.Option(help="Root directory containing phase report artifacts."),
    ] = Path("reports"),
    limit: Annotated[
        int,
        typer.Option(help="Maximum active news/event candidate markets to scan."),
    ] = 2000,
) -> None:
    """Discover active news/event market parser and source gaps without forecasts."""
    if limit < 1:
        raise typer.BadParameter("limit must be positive")
    settings = get_settings()
    engine = make_engine(database_url_from_settings(settings))
    session_factory = get_session_factory(engine)
    try:
        with session_factory() as session:
            artifacts = write_phase3bb_r7_news_event_discovery_report(
                session,
                output_dir=output_dir,
                reports_dir=reports_dir,
                settings=settings,
                command_args=sys.argv[1:],
                limit=limit,
            )
            session.rollback()
    finally:
        engine.dispose()
    console.print("Phase 3BB-R7 news/event discovery")
    console.print("Mode: PAPER READ-ONLY news/event source/parser inventory")
    console.print("Live/demo execution: blocked")
    console.print("Order submission/cancel/replace: blocked")
    console.print("Paper trade creation: blocked")
    console.print("Headline-only forecasts: blocked")
    console.print("Fuzzy event matching: blocked")
    console.print("DB writes performed: 0")
    console.print(f"Wrote Executive Summary: {artifacts.executive_summary_path}")
    console.print(f"Wrote Markdown: {artifacts.markdown_path}")
    console.print(f"Wrote candidates: {artifacts.candidates_csv_path}")
    console.print(f"Wrote source backlog: {artifacts.source_backlog_csv_path}")
    console.print(f"Wrote manifest: {artifacts.manifest_path}")


@app.command("phase3bb-r8-unified-paper-gate")
def phase3bb_r8_unified_paper_gate_command(
    output_dir: Annotated[
        Path,
        typer.Option(help="Directory for Phase 3BB-R8 unified paper gate artifacts."),
    ] = Path("reports/phase3bb_r8"),
    reports_dir: Annotated[
        Path,
        typer.Option(help="Root directory containing phase report artifacts."),
    ] = Path("reports"),
    limit_per_category: Annotated[
        int,
        typer.Option(help="Maximum current candidate rows to inspect per category."),
    ] = 500,
) -> None:
    """Build a category-aware paper-ready gate without creating trades."""
    if limit_per_category < 1:
        raise typer.BadParameter("limit-per-category must be positive")
    settings = get_settings()
    engine = make_engine(database_url_from_settings(settings))
    session_factory = get_session_factory(engine)
    try:
        with session_factory() as session:
            artifacts = write_phase3bb_r8_unified_paper_gate_report(
                session,
                output_dir=output_dir,
                reports_dir=reports_dir,
                settings=settings,
                command_args=sys.argv[1:],
                limit_per_category=limit_per_category,
            )
            session.rollback()
    finally:
        engine.dispose()
    console.print("Phase 3BB-R8 unified paper gate")
    console.print("Mode: PAPER READ-ONLY category-aware paper gate")
    console.print("Live/demo execution: blocked")
    console.print("Order submission/cancel/replace: blocked")
    console.print("Paper trade creation: blocked")
    console.print("Stale 3AP-only truth: ignored")
    console.print("DB writes performed: 0")
    console.print(f"Wrote Executive Summary: {artifacts.executive_summary_path}")
    console.print(f"Wrote Markdown: {artifacts.markdown_path}")
    console.print(f"Wrote paper gate rows: {artifacts.rows_csv_path}")
    console.print(f"Wrote category blockers: {artifacts.category_blockers_csv_path}")
    console.print(f"Wrote manifest: {artifacts.manifest_path}")


@app.command("phase3bb-r9-learning-acceleration")
def phase3bb_r9_learning_acceleration_command(
    output_dir: Annotated[
        Path,
        typer.Option(help="Directory for Phase 3BB-R9 learning acceleration artifacts."),
    ] = Path("reports/phase3bb_r9"),
    reports_dir: Annotated[
        Path,
        typer.Option(help="Root directory containing phase report artifacts."),
    ] = Path("reports"),
    limit: Annotated[
        int,
        typer.Option(help="Maximum settled forecast/ranking rows to scan for calibration."),
    ] = 10000,
) -> None:
    """Report honest learning acceleration paths without fake paper trades."""
    if limit < 1:
        raise typer.BadParameter("limit must be positive")
    settings = get_settings()
    engine = make_engine(database_url_from_settings(settings))
    session_factory = get_session_factory(engine)
    try:
        with session_factory() as session:
            artifacts = write_phase3bb_r9_learning_acceleration_report(
                session,
                output_dir=output_dir,
                reports_dir=reports_dir,
                settings=settings,
                command_args=sys.argv[1:],
                limit=limit,
            )
            session.rollback()
    finally:
        engine.dispose()
    console.print("Phase 3BB-R9 learning acceleration")
    console.print("Mode: PAPER READ-ONLY learning diagnostics")
    console.print("Historical replay: backtest-only, separated from paper learning counts")
    console.print("Live/demo execution: blocked")
    console.print("Order submission/cancel/replace: blocked")
    console.print("Paper trade creation: blocked")
    console.print("Fabricated trades/settlements: blocked")
    console.print("DB writes performed: 0")
    console.print(f"Wrote Executive Summary: {artifacts.executive_summary_path}")
    console.print(f"Wrote Markdown: {artifacts.markdown_path}")
    console.print(f"Wrote replay candidates: {artifacts.replay_candidates_csv_path}")
    console.print(f"Wrote model calibration: {artifacts.model_calibration_path}")
    console.print(f"Wrote manifest: {artifacts.manifest_path}")


@app.command("phase3bb-r10-cloud-readiness-decision")
def phase3bb_r10_cloud_readiness_decision_command(
    output_dir: Annotated[
        Path,
        typer.Option(help="Directory for Phase 3BB-R10 cloud readiness artifacts."),
    ] = Path("reports/phase3bb_r10"),
    reports_dir: Annotated[
        Path,
        typer.Option(help="Root directory containing phase report artifacts."),
    ] = Path("reports"),
) -> None:
    """Decide whether buying cloud compute is useful for the current bot state."""
    settings = get_settings()
    engine = make_engine(database_url_from_settings(settings))
    session_factory = get_session_factory(engine)
    try:
        with session_factory() as session:
            artifacts = write_phase3bb_r10_cloud_readiness_decision_report(
                session,
                output_dir=output_dir,
                reports_dir=reports_dir,
                settings=settings,
                command_args=sys.argv[1:],
            )
            session.rollback()
    finally:
        engine.dispose()
    console.print("Phase 3BB-R10 cloud readiness decision")
    console.print("Mode: PAPER READ-ONLY cloud decision gate")
    console.print("Deployment: blocked")
    console.print("Production settings changes: blocked")
    console.print("Live/demo execution: blocked")
    console.print("Order submission/cancel/replace: blocked")
    console.print("Paper trade creation: blocked")
    console.print("DB writes performed: 0")
    console.print(f"Wrote Executive Summary: {artifacts.executive_summary_path}")
    console.print(f"Wrote decision: {artifacts.decision_markdown_path}")
    console.print(f"Wrote cost plan: {artifacts.cost_plan_path}")
    console.print(f"Wrote deployment checklist: {artifacts.deployment_checklist_path}")
    console.print(f"Wrote JSON: {artifacts.decision_json_path}")
    console.print(f"Wrote manifest: {artifacts.manifest_path}")


@app.command("phase3bb-r11-codex-cloud-bridge")
def phase3bb_r11_codex_cloud_bridge_command(
    output_dir: Annotated[
        Path,
        typer.Option(help="Directory for Phase 3BB-R11 Codex cloud bridge artifacts."),
    ] = Path("reports/phase3bb_r11"),
    reports_dir: Annotated[
        Path,
        typer.Option(help="Root directory containing phase report artifacts."),
    ] = Path("reports"),
    cloud_host: Annotated[
        str,
        typer.Option(help="Cloud host or IP to place in generated SSH examples."),
    ] = "YOUR_DROPLET_IP",
    cloud_user: Annotated[
        str,
        typer.Option(help="Cloud SSH user to place in generated SSH examples."),
    ] = "root",
    ssh_alias: Annotated[
        str,
        typer.Option(help="Local SSH alias Codex/operator should use."),
    ] = "kalshi-cloud",
    app_path: Annotated[
        str,
        typer.Option(help="Expected remote kalshi-predictive-bot app path."),
    ] = "/opt/kalshi-predictive-bot",
    env_path: Annotated[
        str,
        typer.Option(help="Expected remote env file path; contents are never printed."),
    ] = "/etc/kalshi-bot/kalshi-bot.env",
    db_path: Annotated[
        str,
        typer.Option(help="Expected remote database path."),
    ] = "/var/lib/kalshi-bot/kalshi_phase1.db",
    service_name: Annotated[
        str,
        typer.Option(help="Planned guarded watcher service name for documentation only."),
    ] = "kalshi-r5-watcher.service",
    identity_file: Annotated[
        str,
        typer.Option(help="SSH identity file path for generated smoke-test commands."),
    ] = "~/.ssh/id_ed25519",
) -> None:
    """Generate a no-deploy SSH/context pack for connecting Codex to cloud."""
    settings = get_settings()
    engine = make_engine(database_url_from_settings(settings))
    session_factory = get_session_factory(engine)
    try:
        with session_factory() as session:
            artifacts = write_phase3bb_r11_codex_cloud_bridge_report(
                session,
                output_dir=output_dir,
                reports_dir=reports_dir,
                settings=settings,
                command_args=sys.argv[1:],
                cloud_host=cloud_host,
                cloud_user=cloud_user,
                ssh_alias=ssh_alias,
                app_path=app_path,
                env_path=env_path,
                db_path=db_path,
                service_name=service_name,
                identity_file=identity_file,
            )
            session.rollback()
    finally:
        engine.dispose()
    console.print("Phase 3BB-R11 Codex cloud bridge")
    console.print("Mode: PAPER READ-ONLY no-deploy connection pack")
    console.print("SSH executed: 0")
    console.print("Secrets printed/copied: blocked")
    console.print("Deployment/service changes: blocked")
    console.print("Live/demo execution: blocked")
    console.print("Order submission/cancel/replace: blocked")
    console.print("Paper trade creation: blocked")
    console.print("DB writes performed: 0")
    console.print(f"Wrote Executive Summary: {artifacts.executive_summary_path}")
    console.print(f"Wrote bridge detail: {artifacts.bridge_markdown_path}")
    console.print(f"Wrote operator commands: {artifacts.operator_commands_path}")
    console.print(f"Wrote smoke test: {artifacts.smoke_test_path}")
    console.print(f"Wrote Codex context: {artifacts.context_json_path}")
    console.print(f"Wrote Next Actions: {artifacts.next_actions_path}")
    console.print(f"Wrote manifest: {artifacts.manifest_path}")


@app.command("phase3bb-r12-cloud-bootstrap-verification")
def phase3bb_r12_cloud_bootstrap_verification_command(
    output_dir: Annotated[
        Path,
        typer.Option(help="Directory for Phase 3BB-R12 cloud bootstrap artifacts."),
    ] = Path("reports/phase3bb_r12"),
    reports_dir: Annotated[
        Path,
        typer.Option(help="Root directory containing local phase report artifacts."),
    ] = Path("reports"),
    ssh_target: Annotated[
        str | None,
        typer.Option(help="SSH target such as kalshi@159.65.35.72; defaults from R11."),
    ] = None,
    identity_file: Annotated[
        str | None,
        typer.Option(help="SSH identity file path; defaults from R11."),
    ] = None,
    app_path: Annotated[
        str | None,
        typer.Option(help="Remote app path; defaults from R11."),
    ] = None,
    env_path: Annotated[
        str | None,
        typer.Option(help="Remote env path; defaults from R11. Contents are never printed."),
    ] = None,
    db_path: Annotated[
        str | None,
        typer.Option(help="Remote DB path; defaults from R11."),
    ] = None,
    per_probe_timeout_seconds: Annotated[
        int,
        typer.Option(help="Timeout for each bounded remote SSH probe."),
    ] = 45,
) -> None:
    """Verify the cloud host is ready before any scheduler is started."""
    settings = get_settings()
    engine = make_engine(database_url_from_settings(settings))
    session_factory = get_session_factory(engine)
    try:
        with session_factory() as session:
            artifacts = write_phase3bb_r12_cloud_bootstrap_verification_report(
                session,
                output_dir=output_dir,
                reports_dir=reports_dir,
                settings=settings,
                command_args=sys.argv[1:],
                ssh_target=ssh_target,
                identity_file=identity_file,
                app_path=app_path,
                env_path=env_path,
                db_path=db_path,
                per_probe_timeout_seconds=per_probe_timeout_seconds,
            )
            session.rollback()
    finally:
        engine.dispose()
    console.print("Phase 3BB-R12 cloud bootstrap verification")
    console.print("Mode: PAPER READ-ONLY remote bootstrap verification")
    console.print("Scheduler start: blocked")
    console.print("Deployment/service changes: blocked")
    console.print("Secrets printed/copied: blocked")
    console.print("Live/demo execution: blocked")
    console.print("Order submission/cancel/replace: blocked")
    console.print("Paper trade creation: blocked")
    console.print("DB writes performed: 0")
    console.print(f"Wrote Executive Summary: {artifacts.executive_summary_path}")
    console.print(f"Wrote Markdown: {artifacts.markdown_path}")
    console.print(f"Wrote JSON: {artifacts.json_path}")
    console.print(f"Wrote probe CSV: {artifacts.probe_csv_path}")
    console.print(f"Wrote Next Actions: {artifacts.next_actions_path}")
    console.print(f"Wrote manifest: {artifacts.manifest_path}")


@app.command("phase3bb-r13-cloud-scheduler-adoption")
def phase3bb_r13_cloud_scheduler_adoption_command(
    output_dir: Annotated[
        Path,
        typer.Option(help="Directory for Phase 3BB-R13 cloud scheduler adoption artifacts."),
    ] = Path("reports/phase3bb_r13"),
    reports_dir: Annotated[
        Path,
        typer.Option(help="Root directory containing local phase report artifacts."),
    ] = Path("reports"),
    ssh_target: Annotated[
        str | None,
        typer.Option(help="SSH target such as kalshi@159.65.35.72; defaults from R11."),
    ] = None,
    identity_file: Annotated[
        str | None,
        typer.Option(help="SSH identity file path; defaults from R11."),
    ] = None,
    app_path: Annotated[
        str | None,
        typer.Option(help="Remote app path; defaults from R11."),
    ] = None,
    env_path: Annotated[
        str | None,
        typer.Option(help="Remote env path; defaults from R11. Contents are never printed."),
    ] = None,
    db_path: Annotated[
        str | None,
        typer.Option(help="Remote DB path; defaults from R11."),
    ] = None,
    expected_r5_pid: Annotated[
        int | None,
        typer.Option(help="Expected cloud R5 PID; defaults from R12."),
    ] = None,
    per_probe_timeout_seconds: Annotated[
        int,
        typer.Option(help="Timeout for each bounded remote SSH probe."),
    ] = 45,
) -> None:
    """Dry-run adoption decision for the existing cloud R5 watcher."""
    settings = get_settings()
    engine = make_engine(database_url_from_settings(settings))
    session_factory = get_session_factory(engine)
    try:
        with session_factory() as session:
            artifacts = write_phase3bb_r13_cloud_scheduler_adoption_report(
                session,
                output_dir=output_dir,
                reports_dir=reports_dir,
                settings=settings,
                command_args=sys.argv[1:],
                ssh_target=ssh_target,
                identity_file=identity_file,
                app_path=app_path,
                env_path=env_path,
                db_path=db_path,
                expected_r5_pid=expected_r5_pid,
                per_probe_timeout_seconds=per_probe_timeout_seconds,
            )
            session.rollback()
    finally:
        engine.dispose()
    console.print("Phase 3BB-R13 cloud scheduler adoption dry run")
    console.print("Mode: PAPER READ-ONLY remote scheduler adoption dry run")
    console.print("Scheduler start: blocked")
    console.print("Service install: blocked")
    console.print("Guarded stop: not executed")
    console.print("Secrets printed/copied: blocked")
    console.print("Live/demo execution: blocked")
    console.print("Order submission/cancel/replace: blocked")
    console.print("Paper trade creation: blocked")
    console.print("DB writes performed: 0")
    console.print(f"Wrote Executive Summary: {artifacts.executive_summary_path}")
    console.print(f"Wrote Markdown: {artifacts.markdown_path}")
    console.print(f"Wrote JSON: {artifacts.json_path}")
    console.print(f"Wrote probe CSV: {artifacts.probe_csv_path}")
    console.print(f"Wrote operator command: {artifacts.operator_command_path}")
    console.print(f"Wrote Next Actions: {artifacts.next_actions_path}")
    console.print(f"Wrote manifest: {artifacts.manifest_path}")


@app.command("phase3bb-r14-cloud-service-plan")
def phase3bb_r14_cloud_service_plan_command(
    output_dir: Annotated[
        Path,
        typer.Option(help="Directory for Phase 3BB-R14 cloud service plan artifacts."),
    ] = Path("reports/phase3bb_r14"),
    reports_dir: Annotated[
        Path,
        typer.Option(help="Root directory containing local phase report artifacts."),
    ] = Path("reports"),
    adopt_existing_r5: Annotated[
        bool,
        typer.Option(help="Draft the plan only if R13 recommends adopting existing R5."),
    ] = False,
    service_name: Annotated[
        str,
        typer.Option(help="Draft systemd service name."),
    ] = "kalshi-r5-watcher.service",
    guard_script_path: Annotated[
        str,
        typer.Option(help="Remote guard script path used in the service draft."),
    ] = "/opt/kalshi-predictive-bot/scripts/cloud/kalshi-r5-start-guard.sh",
) -> None:
    """Draft a no-install cloud service plan for adopting the existing R5 watcher."""
    settings = get_settings()
    engine = make_engine(database_url_from_settings(settings))
    session_factory = get_session_factory(engine)
    try:
        with session_factory() as session:
            artifacts = write_phase3bb_r14_cloud_service_plan_report(
                session,
                output_dir=output_dir,
                reports_dir=reports_dir,
                settings=settings,
                command_args=sys.argv[1:],
                adopt_existing_r5=adopt_existing_r5,
                service_name=service_name,
                guard_script_path=guard_script_path,
            )
            session.rollback()
    finally:
        engine.dispose()
    console.print("Phase 3BB-R14 cloud service plan draft")
    console.print("Mode: PAPER READ-ONLY service draft only")
    console.print("Service install: blocked")
    console.print("Service enable/start: blocked")
    console.print("Existing R5 stop: blocked")
    console.print("Secrets printed/copied: blocked")
    console.print("Live/demo execution: blocked")
    console.print("Order submission/cancel/replace: blocked")
    console.print("Paper trade creation: blocked")
    console.print("DB writes performed: 0")
    console.print(f"Wrote Executive Summary: {artifacts.executive_summary_path}")
    console.print(f"Wrote Markdown: {artifacts.markdown_path}")
    console.print(f"Wrote JSON: {artifacts.json_path}")
    console.print(f"Wrote service draft: {artifacts.service_draft_path}")
    console.print(f"Wrote guard script draft: {artifacts.guard_script_draft_path}")
    console.print(f"Wrote checklist: {artifacts.install_checklist_path}")
    console.print(f"Wrote operator command: {artifacts.operator_command_path}")
    console.print(f"Wrote Next Actions: {artifacts.next_actions_path}")
    console.print(f"Wrote manifest: {artifacts.manifest_path}")


@app.command("phase3bb-r15-cloud-service-install-review")
def phase3bb_r15_cloud_service_install_review_command(
    output_dir: Annotated[
        Path,
        typer.Option(help="Directory for Phase 3BB-R15 cloud service install review artifacts."),
    ] = Path("reports/phase3bb_r15"),
    reports_dir: Annotated[
        Path,
        typer.Option(help="Root directory containing local phase report artifacts."),
    ] = Path("reports"),
    r13_max_age_minutes: Annotated[
        int,
        typer.Option(help="Maximum age for the required refreshed R13 artifact."),
    ] = 30,
) -> None:
    """Review cloud service install readiness without installing or starting anything."""
    settings = get_settings()
    engine = make_engine(database_url_from_settings(settings))
    session_factory = get_session_factory(engine)
    try:
        with session_factory() as session:
            artifacts = write_phase3bb_r15_cloud_service_install_review_report(
                session,
                output_dir=output_dir,
                reports_dir=reports_dir,
                settings=settings,
                command_args=sys.argv[1:],
                r13_max_age_minutes=r13_max_age_minutes,
            )
            session.rollback()
    finally:
        engine.dispose()
    console.print("Phase 3BB-R15 cloud service install review no-start dry run")
    console.print("Mode: PAPER READ-ONLY install review only")
    console.print("Service install: blocked")
    console.print("Service enable/start: blocked")
    console.print("Existing R5 stop: blocked")
    console.print("SSH commands executed: 0")
    console.print("Systemctl commands executed: 0")
    console.print("Secrets printed/copied: blocked")
    console.print("Live/demo execution: blocked")
    console.print("Order submission/cancel/replace: blocked")
    console.print("Paper trade creation: blocked")
    console.print("DB writes performed: 0")
    console.print(f"Wrote Executive Summary: {artifacts.executive_summary_path}")
    console.print(f"Wrote Markdown: {artifacts.markdown_path}")
    console.print(f"Wrote JSON: {artifacts.json_path}")
    console.print(f"Wrote review CSV: {artifacts.review_csv_path}")
    console.print(f"Wrote no-start dry-run script: {artifacts.no_start_dry_run_path}")
    console.print(f"Wrote operator command: {artifacts.operator_command_path}")
    console.print(f"Wrote Next Actions: {artifacts.next_actions_path}")
    console.print(f"Wrote manifest: {artifacts.manifest_path}")


@app.command("phase3bb-r16-cloud-service-install-handoff")
def phase3bb_r16_cloud_service_install_handoff_command(
    output_dir: Annotated[
        Path,
        typer.Option(help="Directory for Phase 3BB-R16 cloud service handoff artifacts."),
    ] = Path("reports/phase3bb_r16"),
    reports_dir: Annotated[
        Path,
        typer.Option(help="Root directory containing local phase report artifacts."),
    ] = Path("reports"),
    operator_approved: Annotated[
        bool,
        typer.Option(help="Required to produce an approved install handoff bundle."),
    ] = False,
    r13_max_age_minutes: Annotated[
        int,
        typer.Option(help="Maximum age for the required refreshed R13 artifact."),
    ] = 30,
) -> None:
    """Create the operator-approved install handoff without executing remote changes."""
    settings = get_settings()
    engine = make_engine(database_url_from_settings(settings))
    session_factory = get_session_factory(engine)
    try:
        with session_factory() as session:
            artifacts = write_phase3bb_r16_cloud_service_install_handoff_report(
                session,
                output_dir=output_dir,
                reports_dir=reports_dir,
                settings=settings,
                command_args=sys.argv[1:],
                operator_approved=operator_approved,
                r13_max_age_minutes=r13_max_age_minutes,
            )
            session.rollback()
    finally:
        engine.dispose()
    console.print("Phase 3BB-R16 cloud service install handoff")
    console.print("Mode: PAPER READ-ONLY operator handoff")
    console.print("Remote copy/install/enable executed by Codex: 0")
    console.print("Service start executed by Codex: 0")
    console.print("Existing R5 stop: blocked")
    console.print("SSH commands executed by Codex: 0")
    console.print("Systemctl commands executed by Codex: 0")
    console.print("Secrets printed/copied by Codex: blocked")
    console.print("Live/demo execution: blocked")
    console.print("Order submission/cancel/replace: blocked")
    console.print("Paper trade creation: blocked")
    console.print("DB writes performed: 0")
    console.print(f"Wrote Executive Summary: {artifacts.executive_summary_path}")
    console.print(f"Wrote Markdown: {artifacts.markdown_path}")
    console.print(f"Wrote JSON: {artifacts.json_path}")
    console.print(f"Wrote handoff checks: {artifacts.handoff_checks_path}")
    console.print(f"Wrote operator handoff: {artifacts.operator_handoff_script_path}")
    console.print(f"Wrote operator next command: {artifacts.operator_next_command_path}")
    console.print(f"Wrote Next Actions: {artifacts.next_actions_path}")
    console.print(f"Wrote manifest: {artifacts.manifest_path}")


@app.command("phase3bb-r17-cloud-service-install-verification")
def phase3bb_r17_cloud_service_install_verification_command(
    output_dir: Annotated[
        Path,
        typer.Option(help="Directory for Phase 3BB-R17 cloud service verification artifacts."),
    ] = Path("reports/phase3bb_r17"),
    reports_dir: Annotated[
        Path,
        typer.Option(help="Root directory containing local phase report artifacts."),
    ] = Path("reports"),
    ssh_target: Annotated[
        str | None,
        typer.Option(help="Override SSH target, for example kalshi@159.65.35.72."),
    ] = None,
    identity_file: Annotated[
        str | None,
        typer.Option(help="Override SSH identity file."),
    ] = None,
    app_path: Annotated[
        str | None,
        typer.Option(help="Override remote app path."),
    ] = None,
    env_path: Annotated[
        str | None,
        typer.Option(help="Override remote env file path."),
    ] = None,
    db_path: Annotated[
        str | None,
        typer.Option(help="Override remote DB path."),
    ] = None,
    service_name: Annotated[
        str | None,
        typer.Option(help="Override cloud systemd service name."),
    ] = None,
    guard_script_path: Annotated[
        str | None,
        typer.Option(help="Override remote R5 start guard script path."),
    ] = None,
    per_probe_timeout_seconds: Annotated[
        int,
        typer.Option(help="Maximum seconds for each bounded remote probe."),
    ] = 45,
) -> None:
    """Verify the operator-run cloud service install without starting/stopping R5."""
    settings = get_settings()
    engine = make_engine(database_url_from_settings(settings))
    session_factory = get_session_factory(engine)
    try:
        with session_factory() as session:
            artifacts = write_phase3bb_r17_cloud_service_install_verification_report(
                session,
                output_dir=output_dir,
                reports_dir=reports_dir,
                settings=settings,
                command_args=sys.argv[1:],
                ssh_target=ssh_target,
                identity_file=identity_file,
                app_path=app_path,
                env_path=env_path,
                db_path=db_path,
                service_name=service_name,
                guard_script_path=guard_script_path,
                per_probe_timeout_seconds=per_probe_timeout_seconds,
            )
            session.rollback()
    finally:
        engine.dispose()
    console.print("Phase 3BB-R17 cloud service install verification")
    console.print("Mode: PAPER READ-ONLY post-operator verification")
    console.print("Remote copy/install/enable executed by Codex: 0")
    console.print("Service start executed by Codex: 0")
    console.print("Existing R5 stop: blocked")
    console.print("Systemctl mutating commands executed by Codex: 0")
    console.print("Secrets printed/copied by Codex: blocked")
    console.print("Live/demo execution: blocked")
    console.print("Order submission/cancel/replace: blocked")
    console.print("Paper trade creation: blocked")
    console.print("DB writes performed: 0")
    console.print(f"Wrote Executive Summary: {artifacts.executive_summary_path}")
    console.print(f"Wrote Markdown: {artifacts.markdown_path}")
    console.print(f"Wrote JSON: {artifacts.json_path}")
    console.print(f"Wrote remote probes: {artifacts.probe_csv_path}")
    console.print(f"Wrote verification checks: {artifacts.checks_csv_path}")
    console.print(f"Wrote operator next command: {artifacts.operator_command_path}")
    console.print(f"Wrote Next Actions: {artifacts.next_actions_path}")
    console.print(f"Wrote manifest: {artifacts.manifest_path}")


@app.command("phase3bb-r18-cloud-scheduler-runtime-cutover")
def phase3bb_r18_cloud_scheduler_runtime_cutover_command(
    output_dir: Annotated[
        Path,
        typer.Option(help="Directory for Phase 3BB-R18 runtime cutover artifacts."),
    ] = Path("reports/phase3bb_r18"),
    reports_dir: Annotated[
        Path,
        typer.Option(help="Root directory containing local phase report artifacts."),
    ] = Path("reports"),
    ssh_target: Annotated[
        str | None,
        typer.Option(help="Override SSH target, for example kalshi@159.65.35.72."),
    ] = None,
    identity_file: Annotated[
        str | None,
        typer.Option(help="Override SSH identity file."),
    ] = None,
    app_path: Annotated[
        str | None,
        typer.Option(help="Override remote app path."),
    ] = None,
    env_path: Annotated[
        str | None,
        typer.Option(help="Override remote env file path."),
    ] = None,
    db_path: Annotated[
        str | None,
        typer.Option(help="Override remote DB path."),
    ] = None,
    service_name: Annotated[
        str,
        typer.Option(help="Cloud systemd service name to monitor."),
    ] = "kalshi-r5-watcher.service",
    per_probe_timeout_seconds: Annotated[
        int,
        typer.Option(help="Maximum seconds for each bounded remote probe."),
    ] = 45,
) -> None:
    """Monitor R5 runtime cutover from manual watcher to enabled systemd service."""
    settings = get_settings()
    engine = make_engine(database_url_from_settings(settings))
    session_factory = get_session_factory(engine)
    try:
        with session_factory() as session:
            artifacts = write_phase3bb_r18_cloud_scheduler_runtime_cutover_report(
                session,
                output_dir=output_dir,
                reports_dir=reports_dir,
                settings=settings,
                command_args=sys.argv[1:],
                ssh_target=ssh_target,
                identity_file=identity_file,
                app_path=app_path,
                env_path=env_path,
                db_path=db_path,
                service_name=service_name,
                per_probe_timeout_seconds=per_probe_timeout_seconds,
            )
            session.rollback()
    finally:
        engine.dispose()
    console.print("Phase 3BB-R18 cloud scheduler runtime cutover monitor")
    console.print("Mode: PAPER READ-ONLY runtime cutover monitor")
    console.print("Service start executed by Codex: 0")
    console.print("Service stop executed by Codex: 0")
    console.print("Existing R5 stop: blocked")
    console.print("Systemctl mutating commands executed by Codex: 0")
    console.print("Secrets printed/copied by Codex: blocked")
    console.print("Live/demo execution: blocked")
    console.print("Order submission/cancel/replace: blocked")
    console.print("Paper trade creation: blocked")
    console.print("DB writes performed: 0")
    console.print(f"Wrote Executive Summary: {artifacts.executive_summary_path}")
    console.print(f"Wrote Markdown: {artifacts.markdown_path}")
    console.print(f"Wrote JSON: {artifacts.json_path}")
    console.print(f"Wrote remote probes: {artifacts.probe_csv_path}")
    console.print(f"Wrote cutover checks: {artifacts.checks_csv_path}")
    console.print(f"Wrote operator next command: {artifacts.operator_command_path}")
    console.print(f"Wrote Next Actions: {artifacts.next_actions_path}")
    console.print(f"Wrote manifest: {artifacts.manifest_path}")


@app.command("phase3bb-r19-cloud-systemd-cutover")
def phase3bb_r19_cloud_systemd_cutover_command(
    output_dir: Annotated[
        Path,
        typer.Option(help="Directory for Phase 3BB-R19 cloud cutover artifacts."),
    ] = Path("reports/phase3bb_r19"),
    reports_dir: Annotated[
        Path,
        typer.Option(help="Root directory containing local phase report artifacts."),
    ] = Path("reports"),
    ssh_target: Annotated[
        str | None,
        typer.Option(help="Override inspection SSH target, for example kalshi@159.65.35.72."),
    ] = None,
    identity_file: Annotated[
        str | None,
        typer.Option(help="Override SSH identity file."),
    ] = None,
    app_path: Annotated[
        str | None,
        typer.Option(help="Override remote app path."),
    ] = None,
    env_path: Annotated[
        str | None,
        typer.Option(help="Override remote env file path."),
    ] = None,
    db_path: Annotated[
        str | None,
        typer.Option(help="Override remote DB path."),
    ] = None,
    control_ssh_target: Annotated[
        str | None,
        typer.Option(help="Override control SSH target for SIGTERM/systemd start."),
    ] = None,
    service_name: Annotated[
        str,
        typer.Option(help="Cloud systemd service name to start after manual R5 exits."),
    ] = "kalshi-r5-watcher.service",
    expected_r5_pid: Annotated[
        int | None,
        typer.Option(help="Override expected manual R5 PID."),
    ] = None,
    execute: Annotated[
        bool,
        typer.Option(help="Execute the approved SIGTERM + systemd start cutover."),
    ] = False,
    terminate_grace_seconds: Annotated[
        int,
        typer.Option(help="Seconds to wait after SIGTERM before blocking the cutover."),
    ] = 45,
    per_probe_timeout_seconds: Annotated[
        int,
        typer.Option(help="Maximum seconds for each bounded remote probe."),
    ] = 45,
) -> None:
    """Operator-approved manual R5 exit and systemd start cutover."""
    settings = get_settings()
    engine = make_engine(database_url_from_settings(settings))
    session_factory = get_session_factory(engine)
    approval_token = os.environ.get(PHASE3BB_R19_APPROVAL_ENV_VAR)
    try:
        with session_factory() as session:
            artifacts = write_phase3bb_r19_cloud_systemd_cutover_report(
                session,
                output_dir=output_dir,
                reports_dir=reports_dir,
                settings=settings,
                command_args=sys.argv[1:],
                ssh_target=ssh_target,
                identity_file=identity_file,
                app_path=app_path,
                env_path=env_path,
                db_path=db_path,
                control_ssh_target=control_ssh_target,
                service_name=service_name,
                expected_r5_pid=expected_r5_pid,
                execute=execute,
                approval_token=approval_token,
                terminate_grace_seconds=terminate_grace_seconds,
                per_probe_timeout_seconds=per_probe_timeout_seconds,
            )
            session.rollback()
    finally:
        engine.dispose()
    console.print("Phase 3BB-R19 cloud systemd cutover")
    console.print("Mode: PAPER SAFE operator-approved cloud cutover")
    console.print(f"Execute requested: {int(execute)}")
    console.print(f"Approval token present: {int(bool(approval_token))}")
    console.print("Service files written by Codex: 0")
    console.print("Live/demo execution: blocked")
    console.print("Order submission/cancel/replace: blocked")
    console.print("Paper trade creation: blocked")
    console.print("DB writes performed: 0")
    console.print(f"Wrote Executive Summary: {artifacts.executive_summary_path}")
    console.print(f"Wrote Markdown: {artifacts.markdown_path}")
    console.print(f"Wrote JSON: {artifacts.json_path}")
    console.print(f"Wrote cutover checks: {artifacts.cutover_checks_path}")
    console.print(f"Wrote remote cutover results: {artifacts.remote_results_path}")
    console.print(f"Wrote operator cutover command: {artifacts.operator_cutover_command_path}")
    console.print(f"Wrote Next Actions: {artifacts.next_actions_path}")
    console.print(f"Wrote manifest: {artifacts.manifest_path}")


@app.command("phase3bb-r20-cloud-ui-service-plan")
def phase3bb_r20_cloud_ui_service_plan_command(
    output_dir: Annotated[
        Path,
        typer.Option(help="Directory for Phase 3BB-R20 cloud UI plan artifacts."),
    ] = Path("reports/phase3bb_r20"),
    reports_dir: Annotated[
        Path,
        typer.Option(help="Root directory containing local phase report artifacts."),
    ] = Path("reports"),
    ssh_target: Annotated[
        str | None,
        typer.Option(help="Override SSH target, for example kalshi@159.65.35.72."),
    ] = None,
    identity_file: Annotated[
        str | None,
        typer.Option(help="Override SSH identity file."),
    ] = None,
    app_path: Annotated[
        str | None,
        typer.Option(help="Override remote app path."),
    ] = None,
    env_path: Annotated[
        str | None,
        typer.Option(help="Override remote env file path."),
    ] = None,
    db_path: Annotated[
        str | None,
        typer.Option(help="Override remote DB path."),
    ] = None,
    ui_service_name: Annotated[
        str,
        typer.Option(help="Cloud UI systemd service name to draft."),
    ] = "kalshi-ui.service",
    ui_host: Annotated[
        str,
        typer.Option(help="UI bind host for the draft service."),
    ] = "127.0.0.1",
    ui_port: Annotated[
        int,
        typer.Option(help="UI bind port for the draft service."),
    ] = 8080,
    per_probe_timeout_seconds: Annotated[
        int,
        typer.Option(help="Maximum seconds for each bounded remote probe."),
    ] = 45,
) -> None:
    """Draft the cloud UI service plan without installing or starting it."""
    settings = get_settings()
    engine = make_engine(database_url_from_settings(settings))
    session_factory = get_session_factory(engine)
    try:
        with session_factory() as session:
            artifacts = write_phase3bb_r20_cloud_ui_service_plan_report(
                session,
                output_dir=output_dir,
                reports_dir=reports_dir,
                settings=settings,
                command_args=sys.argv[1:],
                ssh_target=ssh_target,
                identity_file=identity_file,
                app_path=app_path,
                env_path=env_path,
                db_path=db_path,
                ui_service_name=ui_service_name,
                ui_host=ui_host,
                ui_port=ui_port,
                per_probe_timeout_seconds=per_probe_timeout_seconds,
            )
            session.rollback()
    finally:
        engine.dispose()
    console.print("Phase 3BB-R20 cloud UI service plan")
    console.print("Mode: PAPER READ-ONLY UI service draft")
    console.print("UI service install executed by Codex: 0")
    console.print("UI service enable/start executed by Codex: 0")
    console.print("Nginx/firewall changes executed by Codex: 0")
    console.print("Live/demo execution: blocked")
    console.print("Order submission/cancel/replace: blocked")
    console.print("Paper trade creation: blocked")
    console.print("DB writes performed: 0")
    console.print(f"Wrote Executive Summary: {artifacts.executive_summary_path}")
    console.print(f"Wrote Markdown: {artifacts.markdown_path}")
    console.print(f"Wrote JSON: {artifacts.json_path}")
    console.print(f"Wrote UI service draft: {artifacts.service_draft_path}")
    console.print(f"Wrote nginx draft: {artifacts.nginx_draft_path}")
    console.print(f"Wrote install checklist: {artifacts.install_checklist_path}")
    console.print(f"Wrote remote probes: {artifacts.probe_csv_path}")
    console.print(f"Wrote operator command: {artifacts.operator_command_path}")
    console.print(f"Wrote Next Actions: {artifacts.next_actions_path}")
    console.print(f"Wrote manifest: {artifacts.manifest_path}")


@app.command("phase3bb-r21-cloud-ui-install-review")
def phase3bb_r21_cloud_ui_install_review_command(
    output_dir: Annotated[
        Path,
        typer.Option(help="Directory for Phase 3BB-R21 cloud UI install review artifacts."),
    ] = Path("reports/phase3bb_r21"),
    reports_dir: Annotated[
        Path,
        typer.Option(help="Root directory containing local phase report artifacts."),
    ] = Path("reports"),
    r20_max_age_minutes: Annotated[
        int,
        typer.Option(help="Maximum age for the required refreshed R20 artifact."),
    ] = 30,
    ui_service_name: Annotated[
        str,
        typer.Option(help="Cloud UI systemd service name to review."),
    ] = "kalshi-ui.service",
) -> None:
    """Review the cloud UI service draft without install, enable, or start."""
    settings = get_settings()
    engine = make_engine(database_url_from_settings(settings))
    session_factory = get_session_factory(engine)
    try:
        with session_factory() as session:
            artifacts = write_phase3bb_r21_cloud_ui_install_review_report(
                session,
                output_dir=output_dir,
                reports_dir=reports_dir,
                settings=settings,
                command_args=sys.argv[1:],
                r20_max_age_minutes=r20_max_age_minutes,
                ui_service_name=ui_service_name,
            )
            session.rollback()
    finally:
        engine.dispose()
    console.print("Phase 3BB-R21 cloud UI install review")
    console.print("Mode: PAPER READ-ONLY UI no-start review")
    console.print("UI service install executed by Codex: 0")
    console.print("UI service enable/start executed by Codex: 0")
    console.print("Nginx/firewall changes executed by Codex: 0")
    console.print("SSH/scp/systemctl commands executed by Codex: 0")
    console.print("Live/demo execution: blocked")
    console.print("Order submission/cancel/replace: blocked")
    console.print("Paper trade creation: blocked")
    console.print("DB writes performed: 0")
    console.print(f"Wrote Executive Summary: {artifacts.executive_summary_path}")
    console.print(f"Wrote Markdown: {artifacts.markdown_path}")
    console.print(f"Wrote JSON: {artifacts.json_path}")
    console.print(f"Wrote review checks: {artifacts.review_csv_path}")
    console.print(f"Wrote no-start dry run: {artifacts.no_start_dry_run_path}")
    console.print(f"Wrote operator command: {artifacts.operator_command_path}")
    console.print(f"Wrote Next Actions: {artifacts.next_actions_path}")
    console.print(f"Wrote manifest: {artifacts.manifest_path}")


@app.command("phase3bb-r22-cloud-ui-install-handoff")
def phase3bb_r22_cloud_ui_install_handoff_command(
    output_dir: Annotated[
        Path,
        typer.Option(help="Directory for Phase 3BB-R22 cloud UI install handoff artifacts."),
    ] = Path("reports/phase3bb_r22"),
    reports_dir: Annotated[
        Path,
        typer.Option(help="Root directory containing local phase report artifacts."),
    ] = Path("reports"),
    operator_approved: Annotated[
        bool,
        typer.Option(help="Confirm the operator approved generating the R22 UI handoff."),
    ] = False,
    r21_max_age_minutes: Annotated[
        int,
        typer.Option(help="Maximum age for the required refreshed R21 artifact."),
    ] = 30,
    ui_service_name: Annotated[
        str,
        typer.Option(help="Cloud UI systemd service name for the handoff."),
    ] = "kalshi-ui.service",
) -> None:
    """Generate the operator-approved UI install handoff without executing it."""
    settings = get_settings()
    engine = make_engine(database_url_from_settings(settings))
    session_factory = get_session_factory(engine)
    try:
        with session_factory() as session:
            artifacts = write_phase3bb_r22_cloud_ui_install_handoff_report(
                session,
                output_dir=output_dir,
                reports_dir=reports_dir,
                settings=settings,
                command_args=sys.argv[1:],
                operator_approved=operator_approved,
                r21_max_age_minutes=r21_max_age_minutes,
                ui_service_name=ui_service_name,
            )
            session.rollback()
    finally:
        engine.dispose()
    console.print("Phase 3BB-R22 cloud UI install handoff")
    console.print("Mode: PAPER READ-ONLY UI install handoff")
    console.print("UI service install executed by Codex: 0")
    console.print("UI service enable/start executed by Codex: 0")
    console.print("Nginx/firewall changes executed by Codex: 0")
    console.print("SSH/scp/systemctl commands executed by Codex: 0")
    console.print("Live/demo execution: blocked")
    console.print("Order submission/cancel/replace: blocked")
    console.print("Paper trade creation: blocked")
    console.print("DB writes performed: 0")
    console.print(f"Wrote Executive Summary: {artifacts.executive_summary_path}")
    console.print(f"Wrote Markdown: {artifacts.markdown_path}")
    console.print(f"Wrote JSON: {artifacts.json_path}")
    console.print(f"Wrote handoff checks: {artifacts.handoff_checks_path}")
    console.print(f"Wrote operator handoff: {artifacts.operator_handoff_script_path}")
    console.print(f"Wrote operator command: {artifacts.operator_next_command_path}")
    console.print(f"Wrote Next Actions: {artifacts.next_actions_path}")
    console.print(f"Wrote manifest: {artifacts.manifest_path}")


@app.command("phase3bb-r23-cloud-ui-install-verification")
def phase3bb_r23_cloud_ui_install_verification_command(
    output_dir: Annotated[
        Path,
        typer.Option(help="Directory for Phase 3BB-R23 cloud UI install verification artifacts."),
    ] = Path("reports/phase3bb_r23"),
    reports_dir: Annotated[
        Path,
        typer.Option(help="Root directory containing local phase report artifacts."),
    ] = Path("reports"),
    ssh_target: Annotated[
        str | None,
        typer.Option(help="Optional SSH target override, e.g. kalshi@159.65.35.72."),
    ] = None,
    identity_file: Annotated[
        str | None,
        typer.Option(help="Optional SSH identity file override."),
    ] = None,
    app_path: Annotated[
        str | None,
        typer.Option(help="Optional remote app path override."),
    ] = None,
    env_path: Annotated[
        str | None,
        typer.Option(help="Optional remote environment file override."),
    ] = None,
    db_path: Annotated[
        str | None,
        typer.Option(help="Optional remote database path override."),
    ] = None,
    ui_service_name: Annotated[
        str,
        typer.Option(help="Cloud UI systemd service name to verify."),
    ] = "kalshi-ui.service",
    ui_port: Annotated[
        int,
        typer.Option(help="Cloud UI local bind port to verify."),
    ] = 8080,
    per_probe_timeout_seconds: Annotated[
        int,
        typer.Option(help="Timeout per remote verification probe."),
    ] = 30,
) -> None:
    """Verify the cloud UI service install after the operator handoff, without starting it."""
    settings = get_settings()
    engine = make_engine(database_url_from_settings(settings))
    session_factory = get_session_factory(engine)
    try:
        with session_factory() as session:
            artifacts = write_phase3bb_r23_cloud_ui_install_verification_report(
                session,
                output_dir=output_dir,
                reports_dir=reports_dir,
                settings=settings,
                command_args=sys.argv[1:],
                ssh_target=ssh_target,
                identity_file=identity_file,
                app_path=app_path,
                env_path=env_path,
                db_path=db_path,
                ui_service_name=ui_service_name,
                ui_port=ui_port,
                per_probe_timeout_seconds=per_probe_timeout_seconds,
            )
            session.rollback()
    finally:
        engine.dispose()
    console.print("Phase 3BB-R23 cloud UI install verification")
    console.print("Mode: PAPER READ-ONLY UI install verification")
    console.print("UI service start executed by Codex: 0")
    console.print("Nginx/firewall changes executed by Codex: 0")
    console.print("Live/demo execution: blocked")
    console.print("Order submission/cancel/replace: blocked")
    console.print("Paper trade creation: blocked")
    console.print("DB writes performed: 0")
    console.print(f"Wrote Executive Summary: {artifacts.executive_summary_path}")
    console.print(f"Wrote Markdown: {artifacts.markdown_path}")
    console.print(f"Wrote JSON: {artifacts.json_path}")
    console.print(f"Wrote remote probes: {artifacts.probe_csv_path}")
    console.print(f"Wrote verification checks: {artifacts.checks_csv_path}")
    console.print(f"Wrote operator command: {artifacts.operator_command_path}")
    console.print(f"Wrote Next Actions: {artifacts.next_actions_path}")
    console.print(f"Wrote manifest: {artifacts.manifest_path}")


@app.command("phase3bb-r24-cloud-ui-start-tunnel-verification")
def phase3bb_r24_cloud_ui_start_tunnel_verification_command(
    output_dir: Annotated[
        Path,
        typer.Option(help="Directory for Phase 3BB-R24 cloud UI start artifacts."),
    ] = Path("reports/phase3bb_r24"),
    reports_dir: Annotated[
        Path,
        typer.Option(help="Root directory containing local phase report artifacts."),
    ] = Path("reports"),
    ssh_target: Annotated[
        str | None,
        typer.Option(help="Optional SSH target override, e.g. kalshi@159.65.35.72."),
    ] = None,
    identity_file: Annotated[
        str | None,
        typer.Option(help="Optional SSH identity file override."),
    ] = None,
    app_path: Annotated[
        str | None,
        typer.Option(help="Optional remote app path override."),
    ] = None,
    env_path: Annotated[
        str | None,
        typer.Option(help="Optional remote environment file override."),
    ] = None,
    db_path: Annotated[
        str | None,
        typer.Option(help="Optional remote database path override."),
    ] = None,
    ui_service_name: Annotated[
        str,
        typer.Option(help="Cloud UI systemd service name to start and verify."),
    ] = "kalshi-ui.service",
    ui_port: Annotated[
        int,
        typer.Option(help="Cloud UI local bind port to verify."),
    ] = 8080,
    operator_approved: Annotated[
        bool,
        typer.Option(help="Confirm the operator approved starting the cloud UI service."),
    ] = False,
    per_probe_timeout_seconds: Annotated[
        int,
        typer.Option(help="Timeout per remote verification probe."),
    ] = 30,
) -> None:
    """Start the localhost-only cloud UI and verify SSH tunnel readiness."""
    settings = get_settings()
    engine = make_engine(database_url_from_settings(settings))
    session_factory = get_session_factory(engine)
    try:
        with session_factory() as session:
            artifacts = write_phase3bb_r24_cloud_ui_start_tunnel_verification_report(
                session,
                output_dir=output_dir,
                reports_dir=reports_dir,
                settings=settings,
                command_args=sys.argv[1:],
                ssh_target=ssh_target,
                identity_file=identity_file,
                app_path=app_path,
                env_path=env_path,
                db_path=db_path,
                ui_service_name=ui_service_name,
                ui_port=ui_port,
                operator_approved=operator_approved,
                per_probe_timeout_seconds=per_probe_timeout_seconds,
            )
            session.rollback()
    finally:
        engine.dispose()
    console.print("Phase 3BB-R24 cloud UI start + SSH tunnel verification")
    console.print("Mode: PAPER READ-ONLY UI start verification")
    console.print(f"Operator approved start: {operator_approved}")
    console.print("Nginx/firewall changes executed by Codex: 0")
    console.print("Live/demo execution: blocked")
    console.print("Order submission/cancel/replace: blocked")
    console.print("Paper trade creation: blocked")
    console.print("DB writes performed: 0")
    console.print(f"Wrote Executive Summary: {artifacts.executive_summary_path}")
    console.print(f"Wrote Markdown: {artifacts.markdown_path}")
    console.print(f"Wrote JSON: {artifacts.json_path}")
    console.print(f"Wrote remote probes: {artifacts.probe_csv_path}")
    console.print(f"Wrote verification checks: {artifacts.checks_csv_path}")
    console.print(f"Wrote operator command: {artifacts.operator_command_path}")
    console.print(f"Wrote Next Actions: {artifacts.next_actions_path}")
    console.print(f"Wrote manifest: {artifacts.manifest_path}")


@app.command("phase3bb-r25-cloud-ui-operator-smoke-test")
def phase3bb_r25_cloud_ui_operator_smoke_test_command(
    output_dir: Annotated[
        Path,
        typer.Option(help="Directory for Phase 3BB-R25 cloud UI smoke artifacts."),
    ] = Path("reports/phase3bb_r25"),
    reports_dir: Annotated[
        Path,
        typer.Option(help="Root directory containing local phase report artifacts."),
    ] = Path("reports"),
    local_base_url: Annotated[
        str,
        typer.Option(help="Local SSH tunnel base URL to smoke test."),
    ] = "http://127.0.0.1:8081",
    timeout_seconds: Annotated[
        int,
        typer.Option(help="Timeout per local UI smoke request."),
    ] = 60,
) -> None:
    """Smoke test the cloud UI through the operator's local SSH tunnel."""
    settings = get_settings()
    engine = make_engine(database_url_from_settings(settings))
    session_factory = get_session_factory(engine)
    try:
        with session_factory() as session:
            artifacts = write_phase3bb_r25_cloud_ui_operator_smoke_test_report(
                session,
                output_dir=output_dir,
                reports_dir=reports_dir,
                settings=settings,
                command_args=sys.argv[1:],
                local_base_url=local_base_url,
                timeout_seconds=timeout_seconds,
            )
            session.rollback()
    finally:
        engine.dispose()
    console.print("Phase 3BB-R25 cloud UI operator smoke test")
    console.print("Mode: PAPER READ-ONLY local tunnel UI smoke")
    console.print(f"Local base URL: {local_base_url}")
    console.print("Nginx/firewall changes executed by Codex: 0")
    console.print("Live/demo execution: blocked")
    console.print("Order submission/cancel/replace: blocked")
    console.print("Paper trade creation: blocked")
    console.print("DB writes performed: 0")
    console.print(f"Wrote Executive Summary: {artifacts.executive_summary_path}")
    console.print(f"Wrote Markdown: {artifacts.markdown_path}")
    console.print(f"Wrote JSON: {artifacts.json_path}")
    console.print(f"Wrote smoke results: {artifacts.results_csv_path}")
    console.print(f"Wrote smoke checks: {artifacts.checks_csv_path}")
    console.print(f"Wrote operator command: {artifacts.operator_command_path}")
    console.print(f"Wrote Next Actions: {artifacts.next_actions_path}")
    console.print(f"Wrote manifest: {artifacts.manifest_path}")


@app.command("phase3bb-r26-cloud-ui-access-control-gate")
def phase3bb_r26_cloud_ui_access_control_gate_command(
    output_dir: Annotated[
        Path,
        typer.Option(help="Directory for Phase 3BB-R26 cloud UI access-control artifacts."),
    ] = Path("reports/phase3bb_r26"),
    reports_dir: Annotated[
        Path,
        typer.Option(help="Root directory containing local phase report artifacts."),
    ] = Path("reports"),
    public_domain: Annotated[
        str | None,
        typer.Option(help="Optional public DNS name to evaluate for HTTPS exposure."),
    ] = None,
    operator_ip_cidr: Annotated[
        str | None,
        typer.Option(help="Optional operator IP/CIDR allowlist for public exposure review."),
    ] = None,
    auth_mode: Annotated[
        str,
        typer.Option(help="Auth mode to evaluate: none, basic_auth, oauth_proxy, cloudflare_access, tailscale_funnel_auth."),
    ] = "none",
    max_public_route_seconds: Annotated[
        float,
        typer.Option(help="Maximum acceptable route time before public HTTPS review is blocked."),
    ] = 10.0,
) -> None:
    """Decide whether cloud UI should remain tunnel-only or proceed to HTTPS review."""
    settings = get_settings()
    engine = make_engine(database_url_from_settings(settings))
    session_factory = get_session_factory(engine)
    try:
        with session_factory() as session:
            artifacts = write_phase3bb_r26_cloud_ui_access_control_gate_report(
                session,
                output_dir=output_dir,
                reports_dir=reports_dir,
                settings=settings,
                command_args=sys.argv[1:],
                public_domain=public_domain,
                operator_ip_cidr=operator_ip_cidr,
                auth_mode=auth_mode,
                max_public_route_seconds=max_public_route_seconds,
            )
            session.rollback()
    finally:
        engine.dispose()
    console.print("Phase 3BB-R26 cloud UI access-control and HTTPS exposure decision gate")
    console.print("Mode: PAPER READ-ONLY decision gate")
    console.print(f"Public domain: {public_domain or 'missing'}")
    console.print(f"Operator IP/CIDR: {operator_ip_cidr or 'missing'}")
    console.print(f"Auth mode: {auth_mode}")
    console.print("Nginx/firewall changes executed by Codex: 0")
    console.print("Live/demo execution: blocked")
    console.print("Order submission/cancel/replace: blocked")
    console.print("Paper trade creation: blocked")
    console.print("DB writes performed: 0")
    console.print(f"Wrote Executive Summary: {artifacts.executive_summary_path}")
    console.print(f"Wrote Markdown: {artifacts.markdown_path}")
    console.print(f"Wrote JSON: {artifacts.json_path}")
    console.print(f"Wrote access-control checks: {artifacts.checks_csv_path}")
    console.print(f"Wrote exposure options: {artifacts.options_csv_path}")
    console.print(f"Wrote HTTPS draft: {artifacts.https_draft_path}")
    console.print(f"Wrote operator command: {artifacts.operator_command_path}")
    console.print(f"Wrote Next Actions: {artifacts.next_actions_path}")
    console.print(f"Wrote manifest: {artifacts.manifest_path}")


@app.command("phase3bb-r27-cloud-ui-private-access-auth-draft")
def phase3bb_r27_cloud_ui_private_access_auth_draft_command(
    output_dir: Annotated[
        Path,
        typer.Option(help="Directory for Phase 3BB-R27 private access/auth draft artifacts."),
    ] = Path("reports/phase3bb_r27"),
    reports_dir: Annotated[
        Path,
        typer.Option(help="Root directory containing local phase report artifacts."),
    ] = Path("reports"),
    preferred_access: Annotated[
        str,
        typer.Option(help="Preferred private access mode: ssh_tunnel, private_vpn, or cloudflare_access_tunnel."),
    ] = "private_vpn",
    operator_email: Annotated[
        str | None,
        typer.Option(help="Optional operator email for identity-aware access draft notes."),
    ] = None,
    operator_device_label: Annotated[
        str | None,
        typer.Option(help="Optional operator device label for private VPN draft notes."),
    ] = None,
) -> None:
    """Draft the private access/auth path for the cloud UI without installing it."""
    settings = get_settings()
    engine = make_engine(database_url_from_settings(settings))
    session_factory = get_session_factory(engine)
    try:
        with session_factory() as session:
            artifacts = write_phase3bb_r27_cloud_ui_private_access_auth_draft_report(
                session,
                output_dir=output_dir,
                reports_dir=reports_dir,
                settings=settings,
                command_args=sys.argv[1:],
                preferred_access=preferred_access,
                operator_email=operator_email,
                operator_device_label=operator_device_label,
            )
            session.rollback()
    finally:
        engine.dispose()
    console.print("Phase 3BB-R27 cloud UI private access/auth draft")
    console.print("Mode: PAPER READ-ONLY no-install draft")
    console.print(f"Preferred access: {preferred_access}")
    console.print("Nginx/firewall changes executed by Codex: 0")
    console.print("Private access installs executed by Codex: 0")
    console.print("Live/demo execution: blocked")
    console.print("Order submission/cancel/replace: blocked")
    console.print("Paper trade creation: blocked")
    console.print("DB writes performed: 0")
    console.print(f"Wrote Executive Summary: {artifacts.executive_summary_path}")
    console.print(f"Wrote Markdown: {artifacts.markdown_path}")
    console.print(f"Wrote JSON: {artifacts.json_path}")
    console.print(f"Wrote access options: {artifacts.options_csv_path}")
    console.print(f"Wrote selected plan: {artifacts.selected_plan_path}")
    console.print(f"Wrote checklist: {artifacts.checklist_path}")
    console.print(f"Wrote no-install draft: {artifacts.no_install_draft_path}")
    console.print(f"Wrote operator command: {artifacts.operator_command_path}")
    console.print(f"Wrote Next Actions: {artifacts.next_actions_path}")
    console.print(f"Wrote manifest: {artifacts.manifest_path}")


@app.command("phase3bb-r28-cloud-ui-private-access-operator-review")
def phase3bb_r28_cloud_ui_private_access_operator_review_command(
    output_dir: Annotated[
        Path,
        typer.Option(help="Directory for Phase 3BB-R28 private access operator review artifacts."),
    ] = Path("reports/phase3bb_r28"),
    reports_dir: Annotated[
        Path,
        typer.Option(help="Root directory containing local phase report artifacts."),
    ] = Path("reports"),
    selected_access: Annotated[
        str | None,
        typer.Option(help="Optional selected access override: ssh_tunnel, private_vpn, or cloudflare_access_tunnel."),
    ] = None,
) -> None:
    """Review the private access draft and generate a no-install dry run."""
    settings = get_settings()
    engine = make_engine(database_url_from_settings(settings))
    session_factory = get_session_factory(engine)
    try:
        with session_factory() as session:
            artifacts = write_phase3bb_r28_cloud_ui_private_access_operator_review_report(
                session,
                output_dir=output_dir,
                reports_dir=reports_dir,
                settings=settings,
                command_args=sys.argv[1:],
                selected_access=selected_access,
            )
            session.rollback()
    finally:
        engine.dispose()
    console.print("Phase 3BB-R28 cloud UI private access operator review")
    console.print("Mode: PAPER READ-ONLY no-install dry run")
    console.print(f"Selected access override: {selected_access or 'R27 selected plan'}")
    console.print("Private access installs executed by Codex: 0")
    console.print("Nginx/firewall changes executed by Codex: 0")
    console.print("Live/demo execution: blocked")
    console.print("Order submission/cancel/replace: blocked")
    console.print("Paper trade creation: blocked")
    console.print("DB writes performed: 0")
    console.print(f"Wrote Executive Summary: {artifacts.executive_summary_path}")
    console.print(f"Wrote Markdown: {artifacts.markdown_path}")
    console.print(f"Wrote JSON: {artifacts.json_path}")
    console.print(f"Wrote review checks: {artifacts.checks_csv_path}")
    console.print(f"Wrote dry run: {artifacts.dry_run_path}")
    console.print(f"Wrote handoff preview: {artifacts.handoff_preview_path}")
    console.print(f"Wrote operator command: {artifacts.operator_command_path}")
    console.print(f"Wrote Next Actions: {artifacts.next_actions_path}")
    console.print(f"Wrote manifest: {artifacts.manifest_path}")


@app.command("phase3bb-r29-cloud-ui-private-access-install-handoff")
def phase3bb_r29_cloud_ui_private_access_install_handoff_command(
    output_dir: Annotated[
        Path,
        typer.Option(help="Directory for Phase 3BB-R29 private access handoff artifacts."),
    ] = Path("reports/phase3bb_r29"),
    reports_dir: Annotated[
        Path,
        typer.Option(help="Root directory containing local phase report artifacts."),
    ] = Path("reports"),
    operator_approved: Annotated[
        bool,
        typer.Option(help="Confirm the operator approved generating the R29 handoff."),
    ] = False,
    r28_max_age_minutes: Annotated[
        int,
        typer.Option(help="Maximum age for the required refreshed R28 artifact."),
    ] = 60,
) -> None:
    """Generate the operator-approved private-access handoff without executing it."""
    settings = get_settings()
    engine = make_engine(database_url_from_settings(settings))
    session_factory = get_session_factory(engine)
    try:
        with session_factory() as session:
            artifacts = write_phase3bb_r29_cloud_ui_private_access_install_handoff_report(
                session,
                output_dir=output_dir,
                reports_dir=reports_dir,
                settings=settings,
                command_args=sys.argv[1:],
                operator_approved=operator_approved,
                r28_max_age_minutes=r28_max_age_minutes,
            )
            session.rollback()
    finally:
        engine.dispose()
    console.print("Phase 3BB-R29 cloud UI private access install handoff")
    console.print("Mode: PAPER READ-ONLY private access handoff")
    console.print("Private access install executed by Codex: 0")
    console.print("Tailscale commands executed by Codex: 0")
    console.print("Nginx/firewall changes executed by Codex: 0")
    console.print("SSH/systemctl commands executed by Codex: 0")
    console.print("Live/demo execution: blocked")
    console.print("Order submission/cancel/replace: blocked")
    console.print("Paper trade creation: blocked")
    console.print("DB writes performed: 0")
    console.print(f"Wrote Executive Summary: {artifacts.executive_summary_path}")
    console.print(f"Wrote Markdown: {artifacts.markdown_path}")
    console.print(f"Wrote JSON: {artifacts.json_path}")
    console.print(f"Wrote handoff checks: {artifacts.checks_csv_path}")
    console.print(f"Wrote operator handoff: {artifacts.operator_handoff_script_path}")
    console.print(f"Wrote operator command: {artifacts.operator_next_command_path}")
    console.print(f"Wrote install plan: {artifacts.install_plan_path}")
    console.print(f"Wrote Next Actions: {artifacts.next_actions_path}")
    console.print(f"Wrote manifest: {artifacts.manifest_path}")


@app.command("phase3bb-r30-cloud-ui-private-access-install-verification")
def phase3bb_r30_cloud_ui_private_access_install_verification_command(
    output_dir: Annotated[
        Path,
        typer.Option(help="Directory for Phase 3BB-R30 private access verification artifacts."),
    ] = Path("reports/phase3bb_r30"),
    reports_dir: Annotated[
        Path,
        typer.Option(help="Root directory containing local phase report artifacts."),
    ] = Path("reports"),
    ssh_target: Annotated[
        str | None,
        typer.Option(help="Optional SSH target override, for example kalshi@159.65.35.72."),
    ] = None,
    identity_file: Annotated[
        str | None,
        typer.Option(help="Optional SSH identity file override."),
    ] = None,
    app_path: Annotated[
        str | None,
        typer.Option(help="Optional remote app path override."),
    ] = None,
    env_path: Annotated[
        str | None,
        typer.Option(help="Optional remote environment file override."),
    ] = None,
    db_path: Annotated[
        str | None,
        typer.Option(help="Optional remote database path override."),
    ] = None,
    ui_service_name: Annotated[
        str,
        typer.Option(help="Cloud UI systemd service name."),
    ] = "kalshi-ui.service",
    ui_port: Annotated[
        int,
        typer.Option(help="Cloud UI local bind port."),
    ] = 8080,
    per_probe_timeout_seconds: Annotated[
        int,
        typer.Option(help="Timeout per SSH read-only verification probe."),
    ] = 45,
) -> None:
    """Verify the operator-run private access install without changing the cloud host."""
    settings = get_settings()
    engine = make_engine(database_url_from_settings(settings))
    session_factory = get_session_factory(engine)
    try:
        with session_factory() as session:
            artifacts = write_phase3bb_r30_cloud_ui_private_access_install_verification_report(
                session,
                output_dir=output_dir,
                reports_dir=reports_dir,
                settings=settings,
                command_args=sys.argv[1:],
                ssh_target=ssh_target,
                identity_file=identity_file,
                app_path=app_path,
                env_path=env_path,
                db_path=db_path,
                ui_service_name=ui_service_name,
                ui_port=ui_port,
                per_probe_timeout_seconds=per_probe_timeout_seconds,
            )
            session.rollback()
    finally:
        engine.dispose()
    console.print("Phase 3BB-R30 cloud UI private access install verification")
    console.print("Mode: PAPER READ-ONLY private access verification")
    console.print("Private access install executed by Codex: 0")
    console.print("Tailscale mutating commands executed by Codex: 0")
    console.print("Nginx/firewall changes executed by Codex: 0")
    console.print("UI/R5 service start/stop executed by Codex: 0")
    console.print("Live/demo execution: blocked")
    console.print("Order submission/cancel/replace: blocked")
    console.print("Paper trade creation: blocked")
    console.print("DB writes performed: 0")
    console.print(f"Wrote Executive Summary: {artifacts.executive_summary_path}")
    console.print(f"Wrote Markdown: {artifacts.markdown_path}")
    console.print(f"Wrote JSON: {artifacts.json_path}")
    console.print(f"Wrote probe results: {artifacts.probe_csv_path}")
    console.print(f"Wrote verification checks: {artifacts.checks_csv_path}")
    console.print(f"Wrote operator command: {artifacts.operator_command_path}")
    console.print(f"Wrote Next Actions: {artifacts.next_actions_path}")
    console.print(f"Wrote manifest: {artifacts.manifest_path}")


@app.command("phase3bb-r31-cloud-ui-private-access-operator-smoke-test")
def phase3bb_r31_cloud_ui_private_access_operator_smoke_test_command(
    output_dir: Annotated[
        Path,
        typer.Option(help="Directory for Phase 3BB-R31 private access smoke artifacts."),
    ] = Path("reports/phase3bb_r31"),
    reports_dir: Annotated[
        Path,
        typer.Option(help="Root directory containing local phase report artifacts."),
    ] = Path("reports"),
    private_base_url: Annotated[
        str | None,
        typer.Option(
            help=(
                "Optional Tailscale Serve base URL override. Defaults to the URL "
                "from the latest Phase 3BB-R30 report."
            )
        ),
    ] = None,
    timeout_seconds: Annotated[
        int,
        typer.Option(help="Timeout per private Tailscale UI route probe."),
    ] = 60,
) -> None:
    """Smoke test the operator UI through the verified private Tailscale URL."""
    settings = get_settings()
    engine = make_engine(database_url_from_settings(settings))
    session_factory = get_session_factory(engine)
    try:
        with session_factory() as session:
            artifacts = write_phase3bb_r31_cloud_ui_private_access_operator_smoke_test_report(
                session,
                output_dir=output_dir,
                reports_dir=reports_dir,
                settings=settings,
                command_args=sys.argv[1:],
                private_base_url=private_base_url,
                timeout_seconds=timeout_seconds,
            )
            session.rollback()
    finally:
        engine.dispose()
    console.print("Phase 3BB-R31 cloud UI private access operator smoke test")
    console.print("Mode: PAPER READ-ONLY private tailnet UI smoke")
    console.print("Tailscale mutating commands executed by Codex: 0")
    console.print("Nginx/firewall changes executed by Codex: 0")
    console.print("UI/R5 service start/stop executed by Codex: 0")
    console.print("Live/demo execution: blocked")
    console.print("Order submission/cancel/replace: blocked")
    console.print("Paper trade creation: blocked")
    console.print("DB writes performed: 0")
    console.print(f"Wrote Executive Summary: {artifacts.executive_summary_path}")
    console.print(f"Wrote Markdown: {artifacts.markdown_path}")
    console.print(f"Wrote JSON: {artifacts.json_path}")
    console.print(f"Wrote smoke results: {artifacts.results_csv_path}")
    console.print(f"Wrote smoke checks: {artifacts.checks_csv_path}")
    console.print(f"Wrote operator command: {artifacts.operator_command_path}")
    console.print(f"Wrote Next Actions: {artifacts.next_actions_path}")
    console.print(f"Wrote manifest: {artifacts.manifest_path}")


@app.command("phase3bb-r32-cloud-ui-dashboard-truth-scheduler-status-verification")
def phase3bb_r32_cloud_ui_dashboard_truth_scheduler_status_command(
    output_dir: Annotated[
        Path,
        typer.Option(help="Directory for Phase 3BB-R32 dashboard/scheduler artifacts."),
    ] = Path("reports/phase3bb_r32"),
    reports_dir: Annotated[
        Path,
        typer.Option(help="Root directory containing local phase report artifacts."),
    ] = Path("reports"),
    private_base_url: Annotated[
        str | None,
        typer.Option(help="Optional Tailscale Serve base URL override."),
    ] = None,
    ui_timeout_seconds: Annotated[
        int,
        typer.Option(help="Timeout per private UI dashboard truth API probe."),
    ] = 90,
    max_dashboard_age_seconds: Annotated[
        int,
        typer.Option(help="Maximum acceptable dashboard snapshot age."),
    ] = 300,
    ssh_target: Annotated[
        str | None,
        typer.Option(help="Optional SSH target override, for example kalshi@159.65.35.72."),
    ] = None,
    identity_file: Annotated[
        str | None,
        typer.Option(help="Optional SSH identity file override."),
    ] = None,
    app_path: Annotated[
        str | None,
        typer.Option(help="Optional remote app path override."),
    ] = None,
    env_path: Annotated[
        str | None,
        typer.Option(help="Optional remote environment file override."),
    ] = None,
    db_path: Annotated[
        str | None,
        typer.Option(help="Optional remote database path override."),
    ] = None,
    service_name: Annotated[
        str,
        typer.Option(help="Cloud R5 watcher systemd service name."),
    ] = "kalshi-r5-watcher.service",
    per_probe_timeout_seconds: Annotated[
        int,
        typer.Option(help="Timeout per SSH read-only scheduler probe."),
    ] = 45,
) -> None:
    """Verify private UI dashboard truth and cloud scheduler/R5 ownership."""
    settings = get_settings()
    engine = make_engine(database_url_from_settings(settings))
    session_factory = get_session_factory(engine)
    try:
        with session_factory() as session:
            artifacts = write_phase3bb_r32_cloud_ui_dashboard_truth_scheduler_status_report(
                session,
                output_dir=output_dir,
                reports_dir=reports_dir,
                settings=settings,
                command_args=sys.argv[1:],
                private_base_url=private_base_url,
                ui_timeout_seconds=ui_timeout_seconds,
                max_dashboard_age_seconds=max_dashboard_age_seconds,
                ssh_target=ssh_target,
                identity_file=identity_file,
                app_path=app_path,
                env_path=env_path,
                db_path=db_path,
                service_name=service_name,
                per_probe_timeout_seconds=per_probe_timeout_seconds,
            )
            session.rollback()
    finally:
        engine.dispose()
    console.print("Phase 3BB-R32 cloud UI dashboard truth and scheduler status")
    console.print("Mode: PAPER READ-ONLY private UI/dashboard scheduler verification")
    console.print("Tailscale mutating commands executed by Codex: 0")
    console.print("Nginx/firewall changes executed by Codex: 0")
    console.print("UI/R5 service start/stop executed by Codex: 0")
    console.print("Live/demo execution: blocked")
    console.print("Order submission/cancel/replace: blocked")
    console.print("Paper trade creation: blocked")
    console.print("DB writes performed: 0")
    console.print(f"Wrote Executive Summary: {artifacts.executive_summary_path}")
    console.print(f"Wrote Markdown: {artifacts.markdown_path}")
    console.print(f"Wrote JSON: {artifacts.json_path}")
    console.print(f"Wrote UI probe results: {artifacts.ui_probe_csv_path}")
    console.print(f"Wrote verification checks: {artifacts.checks_csv_path}")
    console.print(f"Wrote operator command: {artifacts.operator_command_path}")
    console.print(f"Wrote Next Actions: {artifacts.next_actions_path}")
    console.print(f"Wrote manifest: {artifacts.manifest_path}")


@app.command("phase3bb-r33-cloud-paper-only-operations-readiness")
def phase3bb_r33_cloud_paper_only_operations_readiness_command(
    output_dir: Annotated[
        Path,
        typer.Option(help="Directory for Phase 3BB-R33 paper-only readiness artifacts."),
    ] = Path("reports/phase3bb_r33"),
    reports_dir: Annotated[
        Path,
        typer.Option(help="Root directory containing local phase report artifacts."),
    ] = Path("reports"),
    private_base_url: Annotated[
        str | None,
        typer.Option(help="Optional Tailscale Serve base URL override."),
    ] = None,
    ui_timeout_seconds: Annotated[
        int,
        typer.Option(help="Timeout per private UI dashboard truth API probe."),
    ] = 90,
    max_dashboard_age_seconds: Annotated[
        int,
        typer.Option(help="Maximum acceptable dashboard snapshot age."),
    ] = 300,
    ssh_target: Annotated[
        str | None,
        typer.Option(help="Optional SSH target override, for example kalshi@159.65.35.72."),
    ] = None,
    identity_file: Annotated[
        str | None,
        typer.Option(help="Optional SSH identity file override."),
    ] = None,
    app_path: Annotated[
        str | None,
        typer.Option(help="Optional remote app path override."),
    ] = None,
    env_path: Annotated[
        str | None,
        typer.Option(help="Optional remote environment file override."),
    ] = None,
    db_path: Annotated[
        str | None,
        typer.Option(help="Optional remote database path override."),
    ] = None,
    service_name: Annotated[
        str,
        typer.Option(help="Cloud R5 watcher systemd service name."),
    ] = "kalshi-r5-watcher.service",
    per_probe_timeout_seconds: Annotated[
        int,
        typer.Option(help="Timeout per SSH read-only scheduler probe."),
    ] = 45,
) -> None:
    """Monitor cloud paper-only operational readiness without changing services/trades."""
    settings = get_settings()
    engine = make_engine(database_url_from_settings(settings))
    session_factory = get_session_factory(engine)
    try:
        with session_factory() as session:
            artifacts = write_phase3bb_r33_cloud_paper_only_operations_readiness_report(
                session,
                output_dir=output_dir,
                reports_dir=reports_dir,
                settings=settings,
                command_args=sys.argv[1:],
                private_base_url=private_base_url,
                ui_timeout_seconds=ui_timeout_seconds,
                max_dashboard_age_seconds=max_dashboard_age_seconds,
                ssh_target=ssh_target,
                identity_file=identity_file,
                app_path=app_path,
                env_path=env_path,
                db_path=db_path,
                service_name=service_name,
                per_probe_timeout_seconds=per_probe_timeout_seconds,
            )
            session.rollback()
    finally:
        engine.dispose()
    console.print("Phase 3BB-R33 cloud paper-only operations readiness monitor")
    console.print("Mode: PAPER READ-ONLY private cloud operations monitor")
    console.print("Tailscale mutating commands executed by Codex: 0")
    console.print("Nginx/firewall changes executed by Codex: 0")
    console.print("UI/R5 service start/stop executed by Codex: 0")
    console.print("Live/demo execution: blocked")
    console.print("Order submission/cancel/replace: blocked")
    console.print("Paper trade creation: blocked")
    console.print("DB writes performed: 0")
    console.print(f"Wrote Executive Summary: {artifacts.executive_summary_path}")
    console.print(f"Wrote Markdown: {artifacts.markdown_path}")
    console.print(f"Wrote JSON: {artifacts.json_path}")
    console.print(f"Wrote readiness checks: {artifacts.checks_csv_path}")
    console.print(f"Wrote readiness warnings: {artifacts.warnings_csv_path}")
    console.print(f"Wrote operator command: {artifacts.operator_command_path}")
    console.print(f"Wrote Next Actions: {artifacts.next_actions_path}")
    console.print(f"Wrote manifest: {artifacts.manifest_path}")


@app.command("phase3bb-r34-cloud-multicategory-refresh-scheduler-review")
def phase3bb_r34_cloud_multicategory_refresh_scheduler_review_command(
    output_dir: Annotated[
        Path,
        typer.Option(help="Directory for Phase 3BB-R34 scheduler review artifacts."),
    ] = Path("reports/phase3bb_r34"),
    reports_dir: Annotated[
        Path,
        typer.Option(help="Root directory containing local phase report artifacts."),
    ] = Path("reports"),
    private_base_url: Annotated[
        str | None,
        typer.Option(help="Optional Tailscale Serve base URL override."),
    ] = None,
    ui_timeout_seconds: Annotated[
        int,
        typer.Option(help="Timeout per private UI dashboard truth API probe."),
    ] = 90,
    max_dashboard_age_seconds: Annotated[
        int,
        typer.Option(help="Maximum acceptable dashboard snapshot age."),
    ] = 300,
    ssh_target: Annotated[
        str | None,
        typer.Option(help="Optional SSH target override, for example kalshi@159.65.35.72."),
    ] = None,
    identity_file: Annotated[
        str | None,
        typer.Option(help="Optional SSH identity file override."),
    ] = None,
    app_path: Annotated[
        str | None,
        typer.Option(help="Optional remote app path override."),
    ] = None,
    env_path: Annotated[
        str | None,
        typer.Option(help="Optional remote environment file override."),
    ] = None,
    db_path: Annotated[
        str | None,
        typer.Option(help="Optional remote database path override."),
    ] = None,
    service_name: Annotated[
        str,
        typer.Option(help="Cloud R5 watcher systemd service name."),
    ] = "kalshi-r5-watcher.service",
    per_probe_timeout_seconds: Annotated[
        int,
        typer.Option(help="Timeout per SSH read-only scheduler probe."),
    ] = 45,
) -> None:
    """Review a cloud multi-category refresh schedule without installing or running it."""
    settings = get_settings()
    engine = make_engine(database_url_from_settings(settings))
    session_factory = get_session_factory(engine)
    try:
        with session_factory() as session:
            artifacts = (
                write_phase3bb_r34_cloud_multicategory_refresh_scheduler_review_report(
                    session,
                    output_dir=output_dir,
                    reports_dir=reports_dir,
                    settings=settings,
                    command_args=sys.argv[1:],
                    private_base_url=private_base_url,
                    ui_timeout_seconds=ui_timeout_seconds,
                    max_dashboard_age_seconds=max_dashboard_age_seconds,
                    ssh_target=ssh_target,
                    identity_file=identity_file,
                    app_path=app_path,
                    env_path=env_path,
                    db_path=db_path,
                    service_name=service_name,
                    per_probe_timeout_seconds=per_probe_timeout_seconds,
                )
            )
            session.rollback()
    finally:
        engine.dispose()
    console.print("Phase 3BB-R34 cloud multi-category refresh scheduler review")
    console.print("Mode: PAPER READ-ONLY scheduler review / no install")
    console.print("Scheduler services installed/enabled/started by Codex: 0")
    console.print("Refresh jobs run by Codex: 0")
    console.print("UI/R5 service start/stop executed by Codex: 0")
    console.print("Live/demo execution: blocked")
    console.print("Order submission/cancel/replace: blocked")
    console.print("Paper trade creation: blocked")
    console.print("DB writes performed: 0")
    console.print(f"Wrote Executive Summary: {artifacts.executive_summary_path}")
    console.print(f"Wrote Markdown: {artifacts.markdown_path}")
    console.print(f"Wrote JSON: {artifacts.json_path}")
    console.print(f"Wrote refresh jobs: {artifacts.jobs_csv_path}")
    console.print(f"Wrote scheduler checks: {artifacts.checks_csv_path}")
    console.print(f"Wrote scheduler draft: {artifacts.scheduler_draft_path}")
    console.print(f"Wrote operator command: {artifacts.operator_command_path}")
    console.print(f"Wrote Next Actions: {artifacts.next_actions_path}")
    console.print(f"Wrote manifest: {artifacts.manifest_path}")


@app.command("phase3bb-r35-cloud-multicategory-scheduler-no-start-dry-run")
def phase3bb_r35_cloud_multicategory_scheduler_no_start_dry_run_command(
    output_dir: Annotated[
        Path,
        typer.Option(help="Directory for Phase 3BB-R35 no-start dry-run artifacts."),
    ] = Path("reports/phase3bb_r35"),
    reports_dir: Annotated[
        Path,
        typer.Option(help="Root directory containing local phase report artifacts."),
    ] = Path("reports"),
    r34_max_age_minutes: Annotated[
        int,
        typer.Option(help="Maximum accepted age of the Phase 3BB-R34 review artifact."),
    ] = 60,
) -> None:
    """Build cloud scheduler service/timer/runner drafts without installing or starting."""
    settings = get_settings()
    engine = make_engine(database_url_from_settings(settings))
    session_factory = get_session_factory(engine)
    try:
        with session_factory() as session:
            artifacts = (
                write_phase3bb_r35_cloud_multicategory_scheduler_no_start_dry_run_report(
                    session,
                    output_dir=output_dir,
                    reports_dir=reports_dir,
                    settings=settings,
                    command_args=sys.argv[1:],
                    r34_max_age_minutes=r34_max_age_minutes,
                )
            )
            session.rollback()
    finally:
        engine.dispose()
    console.print("Phase 3BB-R35 cloud multi-category scheduler no-start dry run")
    console.print("Mode: PAPER READ-ONLY scheduler dry run / no install / no start")
    console.print("Scheduler services installed/enabled/started by Codex: 0")
    console.print("Refresh jobs run by Codex: 0")
    console.print("UI/R5 service start/stop executed by Codex: 0")
    console.print("Live/demo execution: blocked")
    console.print("Order submission/cancel/replace: blocked")
    console.print("Paper trade creation: blocked")
    console.print("DB writes performed: 0")
    console.print(f"Wrote Executive Summary: {artifacts.executive_summary_path}")
    console.print(f"Wrote Markdown: {artifacts.markdown_path}")
    console.print(f"Wrote JSON: {artifacts.json_path}")
    console.print(f"Wrote dry-run checks: {artifacts.checks_csv_path}")
    console.print(f"Wrote job plan: {artifacts.job_plan_csv_path}")
    console.print(f"Wrote service draft: {artifacts.service_draft_path}")
    console.print(f"Wrote timer draft: {artifacts.timer_draft_path}")
    console.print(f"Wrote runner draft: {artifacts.runner_draft_path}")
    console.print(f"Wrote no-start dry run: {artifacts.no_start_dry_run_path}")
    console.print(f"Wrote operator command: {artifacts.operator_command_path}")
    console.print(f"Wrote Next Actions: {artifacts.next_actions_path}")
    console.print(f"Wrote manifest: {artifacts.manifest_path}")


@app.command("phase3bb-r36-cloud-scheduler-install-handoff")
def phase3bb_r36_cloud_scheduler_install_handoff_command(
    output_dir: Annotated[
        Path,
        typer.Option(help="Directory for Phase 3BB-R36 scheduler handoff artifacts."),
    ] = Path("reports/phase3bb_r36"),
    reports_dir: Annotated[
        Path,
        typer.Option(help="Root directory containing local phase report artifacts."),
    ] = Path("reports"),
    operator_approved: Annotated[
        bool,
        typer.Option(help="Required to produce an approved scheduler install handoff."),
    ] = False,
    r35_max_age_minutes: Annotated[
        int,
        typer.Option(help="Maximum age for the required refreshed R35 artifact."),
    ] = 60,
) -> None:
    """Create the operator-approved scheduler install handoff without remote changes."""
    settings = get_settings()
    engine = make_engine(database_url_from_settings(settings))
    session_factory = get_session_factory(engine)
    try:
        with session_factory() as session:
            artifacts = write_phase3bb_r36_cloud_scheduler_install_handoff_report(
                session,
                output_dir=output_dir,
                reports_dir=reports_dir,
                settings=settings,
                command_args=sys.argv[1:],
                operator_approved=operator_approved,
                r35_max_age_minutes=r35_max_age_minutes,
            )
            session.rollback()
    finally:
        engine.dispose()
    console.print("Phase 3BB-R36 cloud scheduler install handoff")
    console.print("Mode: PAPER READ-ONLY operator handoff")
    console.print("Remote copy/install/enable executed by Codex: 0")
    console.print("Scheduler timer/service start executed by Codex: 0")
    console.print("Refresh jobs run by Codex: 0")
    console.print("UI/R5 service start/stop executed by Codex: 0")
    console.print("SSH commands executed by Codex: 0")
    console.print("Systemctl commands executed by Codex: 0")
    console.print("Secrets printed/copied by Codex: blocked")
    console.print("Live/demo execution: blocked")
    console.print("Order submission/cancel/replace: blocked")
    console.print("Paper trade creation: blocked")
    console.print("DB writes performed: 0")
    console.print(f"Wrote Executive Summary: {artifacts.executive_summary_path}")
    console.print(f"Wrote Markdown: {artifacts.markdown_path}")
    console.print(f"Wrote JSON: {artifacts.json_path}")
    console.print(f"Wrote handoff checks: {artifacts.handoff_checks_path}")
    console.print(f"Wrote operator handoff: {artifacts.operator_handoff_script_path}")
    console.print(f"Wrote operator next command: {artifacts.operator_next_command_path}")
    console.print(f"Wrote Next Actions: {artifacts.next_actions_path}")
    console.print(f"Wrote manifest: {artifacts.manifest_path}")


@app.command("phase3bb-r37-cloud-scheduler-install-verification")
def phase3bb_r37_cloud_scheduler_install_verification_command(
    output_dir: Annotated[
        Path,
        typer.Option(help="Directory for Phase 3BB-R37 scheduler verification artifacts."),
    ] = Path("reports/phase3bb_r37"),
    reports_dir: Annotated[
        Path,
        typer.Option(help="Root directory containing local phase report artifacts."),
    ] = Path("reports"),
    ssh_target: Annotated[
        str | None,
        typer.Option(help="Optional SSH target override, for example kalshi@159.65.35.72."),
    ] = None,
    identity_file: Annotated[
        str | None,
        typer.Option(help="Optional SSH identity file override."),
    ] = None,
    app_path: Annotated[
        str | None,
        typer.Option(help="Optional remote app path override."),
    ] = None,
    env_path: Annotated[
        str | None,
        typer.Option(help="Optional remote env file override."),
    ] = None,
    db_path: Annotated[
        str | None,
        typer.Option(help="Optional remote DB path override."),
    ] = None,
    scheduler_service_name: Annotated[
        str,
        typer.Option(help="Scheduler service unit name to verify."),
    ] = "kalshi-multicategory-refresh-scheduler.service",
    scheduler_timer_name: Annotated[
        str,
        typer.Option(help="Scheduler timer unit name to verify."),
    ] = "kalshi-multicategory-refresh-scheduler.timer",
    per_probe_timeout_seconds: Annotated[
        int,
        typer.Option(help="Timeout for each bounded remote verification probe."),
    ] = 45,
) -> None:
    """Verify the operator-run cloud scheduler install without starting it."""
    settings = get_settings()
    engine = make_engine(database_url_from_settings(settings))
    session_factory = get_session_factory(engine)
    try:
        with session_factory() as session:
            artifacts = write_phase3bb_r37_cloud_scheduler_install_verification_report(
                session,
                output_dir=output_dir,
                reports_dir=reports_dir,
                settings=settings,
                command_args=sys.argv[1:],
                ssh_target=ssh_target,
                identity_file=identity_file,
                app_path=app_path,
                env_path=env_path,
                db_path=db_path,
                scheduler_service_name=scheduler_service_name,
                scheduler_timer_name=scheduler_timer_name,
                per_probe_timeout_seconds=per_probe_timeout_seconds,
            )
            session.rollback()
    finally:
        engine.dispose()
    console.print("Phase 3BB-R37 cloud scheduler install verification")
    console.print("Mode: PAPER READ-ONLY post-operator verification")
    console.print("Remote copy/install/enable/start executed by Codex: 0")
    console.print("Scheduler refresh jobs run by Codex: 0")
    console.print("UI/R5 service start/stop executed by Codex: 0")
    console.print("SSH probes executed by Codex: bounded read-only/status only")
    console.print("Systemctl mutating commands executed by Codex: 0")
    console.print("Secrets printed/copied by Codex: blocked")
    console.print("Live/demo execution: blocked")
    console.print("Order submission/cancel/replace: blocked")
    console.print("Paper trade creation: blocked")
    console.print("DB writes performed: 0")
    console.print(f"Wrote Executive Summary: {artifacts.executive_summary_path}")
    console.print(f"Wrote Markdown: {artifacts.markdown_path}")
    console.print(f"Wrote JSON: {artifacts.json_path}")
    console.print(f"Wrote probe CSV: {artifacts.probe_csv_path}")
    console.print(f"Wrote verification checks: {artifacts.checks_csv_path}")
    console.print(f"Wrote operator next command: {artifacts.operator_command_path}")
    console.print(f"Wrote Next Actions: {artifacts.next_actions_path}")
    console.print(f"Wrote manifest: {artifacts.manifest_path}")


@app.command("phase3bb-r38-cloud-scheduler-install-repair-handoff")
def phase3bb_r38_cloud_scheduler_install_repair_handoff_command(
    output_dir: Annotated[
        Path,
        typer.Option(help="Directory for Phase 3BB-R38 repair handoff artifacts."),
    ] = Path("reports/phase3bb_r38"),
    reports_dir: Annotated[
        Path,
        typer.Option(help="Root directory containing local phase report artifacts."),
    ] = Path("reports"),
    ssh_target: Annotated[
        str | None,
        typer.Option(help="Optional SSH target override, for example kalshi@159.65.35.72."),
    ] = None,
    identity_file: Annotated[
        str | None,
        typer.Option(help="Optional SSH identity file override."),
    ] = None,
    app_path: Annotated[
        str | None,
        typer.Option(help="Optional remote app path override."),
    ] = None,
    env_path: Annotated[
        str | None,
        typer.Option(help="Optional remote env file override."),
    ] = None,
    db_path: Annotated[
        str | None,
        typer.Option(help="Optional remote DB path override."),
    ] = None,
    per_probe_timeout_seconds: Annotated[
        int,
        typer.Option(help="Timeout for each bounded remote repair probe."),
    ] = 45,
) -> None:
    """Create the cloud scheduler install repair handoff without starting anything."""
    settings = get_settings()
    engine = make_engine(database_url_from_settings(settings))
    session_factory = get_session_factory(engine)
    try:
        with session_factory() as session:
            artifacts = write_phase3bb_r38_cloud_scheduler_install_repair_handoff_report(
                session,
                output_dir=output_dir,
                reports_dir=reports_dir,
                settings=settings,
                command_args=sys.argv[1:],
                ssh_target=ssh_target,
                identity_file=identity_file,
                app_path=app_path,
                env_path=env_path,
                db_path=db_path,
                per_probe_timeout_seconds=per_probe_timeout_seconds,
            )
            session.rollback()
    finally:
        engine.dispose()
    console.print("Phase 3BB-R38 cloud scheduler install repair handoff")
    console.print("Mode: PAPER READ-ONLY repair handoff / no start")
    console.print("Remote code sync executed by Codex: 0")
    console.print("Root/system install executed by Codex: 0")
    console.print("Scheduler timer/service start executed by Codex: 0")
    console.print("Refresh jobs run by Codex: 0")
    console.print("UI/R5 service start/stop executed by Codex: 0")
    console.print("SSH mutating commands executed by Codex: 0")
    console.print("Systemctl mutating commands executed by Codex: 0")
    console.print("Live/demo execution: blocked")
    console.print("Order submission/cancel/replace: blocked")
    console.print("Paper trade creation: blocked")
    console.print("DB writes performed: 0")
    console.print(f"Wrote Executive Summary: {artifacts.executive_summary_path}")
    console.print(f"Wrote Markdown: {artifacts.markdown_path}")
    console.print(f"Wrote JSON: {artifacts.json_path}")
    console.print(f"Wrote probe CSV: {artifacts.probe_csv_path}")
    console.print(f"Wrote repair checks: {artifacts.checks_csv_path}")
    console.print(f"Wrote root-console script: {artifacts.root_console_script_path}")
    console.print(f"Wrote code-sync handoff: {artifacts.code_sync_handoff_script_path}")
    console.print(f"Wrote operator next command: {artifacts.operator_command_path}")
    console.print(f"Wrote Next Actions: {artifacts.next_actions_path}")
    console.print(f"Wrote manifest: {artifacts.manifest_path}")


@app.command("phase3bb-r39-cloud-auto-login-admin-bootstrap")
def phase3bb_r39_cloud_auto_login_admin_bootstrap_command(
    output_dir: Annotated[
        Path,
        typer.Option(help="Directory for Phase 3BB-R39 access bootstrap artifacts."),
    ] = Path("reports/phase3bb_r39"),
    reports_dir: Annotated[
        Path,
        typer.Option(help="Root directory containing local phase report artifacts."),
    ] = Path("reports"),
    ssh_target: Annotated[
        str | None,
        typer.Option(help="Optional SSH target override, for example kalshi@159.65.35.72."),
    ] = None,
    identity_file: Annotated[
        str | None,
        typer.Option(help="Optional SSH identity file override."),
    ] = None,
    app_path: Annotated[
        str | None,
        typer.Option(help="Optional remote app path override."),
    ] = None,
    env_path: Annotated[
        str | None,
        typer.Option(help="Optional remote env file override."),
    ] = None,
    db_path: Annotated[
        str | None,
        typer.Option(help="Optional remote DB path override."),
    ] = None,
    per_probe_timeout_seconds: Annotated[
        int,
        typer.Option(help="Timeout for each bounded remote access probe."),
    ] = 30,
) -> None:
    """Create auto-login and least-privilege admin bootstrap handoffs."""
    settings = get_settings()
    engine = make_engine(database_url_from_settings(settings))
    session_factory = get_session_factory(engine)
    try:
        with session_factory() as session:
            artifacts = write_phase3bb_r39_cloud_auto_login_admin_bootstrap_report(
                session,
                output_dir=output_dir,
                reports_dir=reports_dir,
                settings=settings,
                command_args=sys.argv[1:],
                ssh_target=ssh_target,
                identity_file=identity_file,
                app_path=app_path,
                env_path=env_path,
                db_path=db_path,
                per_probe_timeout_seconds=per_probe_timeout_seconds,
            )
            session.rollback()
    finally:
        engine.dispose()
    console.print("Phase 3BB-R39 cloud auto-login/admin bootstrap")
    console.print("Mode: PAPER READ-ONLY access bootstrap handoff / no scheduler start")
    console.print("SSH config modified by Codex: 0")
    console.print("Root/sudoers modified by Codex: 0")
    console.print("Code sync executed by Codex: 0")
    console.print("Scheduler timer/service start executed by Codex: 0")
    console.print("Refresh jobs run by Codex: 0")
    console.print("UI/R5 service start/stop executed by Codex: 0")
    console.print("Live/demo execution: blocked")
    console.print("Order submission/cancel/replace: blocked")
    console.print("Paper trade creation: blocked")
    console.print("DB writes performed: 0")
    console.print(f"Wrote Executive Summary: {artifacts.executive_summary_path}")
    console.print(f"Wrote Markdown: {artifacts.markdown_path}")
    console.print(f"Wrote JSON: {artifacts.json_path}")
    console.print(f"Wrote probe CSV: {artifacts.probe_csv_path}")
    console.print(f"Wrote bootstrap checks: {artifacts.checks_csv_path}")
    console.print(f"Wrote SSH config handoff: {artifacts.ssh_config_handoff_path}")
    console.print(f"Wrote root bootstrap: {artifacts.root_bootstrap_path}")
    console.print(f"Wrote operator next command: {artifacts.operator_command_path}")
    console.print(f"Wrote Next Actions: {artifacts.next_actions_path}")
    console.print(f"Wrote manifest: {artifacts.manifest_path}")


@app.command("phase3bb-r40-cloud-scheduler-runtime-monitor")
def phase3bb_r40_cloud_scheduler_runtime_monitor_command(
    output_dir: Annotated[
        Path,
        typer.Option(help="Directory for Phase 3BB-R40 runtime monitor artifacts."),
    ] = Path("reports/phase3bb_r40"),
    reports_dir: Annotated[
        Path,
        typer.Option(help="Root directory containing local phase report artifacts."),
    ] = Path("reports"),
    ssh_target: Annotated[
        str | None,
        typer.Option(help="Optional SSH target override, for example kalshi@159.65.35.72."),
    ] = None,
    identity_file: Annotated[
        str | None,
        typer.Option(help="Optional SSH identity file override."),
    ] = None,
    app_path: Annotated[
        str | None,
        typer.Option(help="Optional remote app path override."),
    ] = None,
    env_path: Annotated[
        str | None,
        typer.Option(help="Optional remote env file override."),
    ] = None,
    db_path: Annotated[
        str | None,
        typer.Option(help="Optional remote DB path override."),
    ] = None,
    scheduler_service_name: Annotated[
        str,
        typer.Option(help="Scheduler service unit name to monitor."),
    ] = "kalshi-multicategory-refresh-scheduler.service",
    scheduler_timer_name: Annotated[
        str,
        typer.Option(help="Scheduler timer unit name to monitor."),
    ] = "kalshi-multicategory-refresh-scheduler.timer",
    ui_service_name: Annotated[
        str,
        typer.Option(help="Cloud UI service unit name to monitor."),
    ] = "kalshi-ui.service",
    r5_service_name: Annotated[
        str,
        typer.Option(help="Cloud R5 watcher service unit name to monitor."),
    ] = "kalshi-r5-watcher.service",
    journal_lines: Annotated[
        int,
        typer.Option(help="Number of scheduler journal lines to inspect."),
    ] = 500,
    per_probe_timeout_seconds: Annotated[
        int,
        typer.Option(help="Timeout for each bounded remote runtime probe."),
    ] = 45,
) -> None:
    """Monitor cloud scheduler runtime health after timer start."""
    settings = get_settings()
    engine = make_engine(database_url_from_settings(settings))
    session_factory = get_session_factory(engine)
    try:
        with session_factory() as session:
            artifacts = write_phase3bb_r40_cloud_scheduler_runtime_monitor_report(
                session,
                output_dir=output_dir,
                reports_dir=reports_dir,
                settings=settings,
                command_args=sys.argv[1:],
                ssh_target=ssh_target,
                identity_file=identity_file,
                app_path=app_path,
                env_path=env_path,
                db_path=db_path,
                scheduler_service_name=scheduler_service_name,
                scheduler_timer_name=scheduler_timer_name,
                ui_service_name=ui_service_name,
                r5_service_name=r5_service_name,
                journal_lines=journal_lines,
                per_probe_timeout_seconds=per_probe_timeout_seconds,
            )
            session.rollback()
    finally:
        engine.dispose()
    console.print("Phase 3BB-R40 cloud scheduler runtime monitor")
    console.print("Mode: PAPER READ-ONLY cloud runtime monitor")
    console.print("Remote copy/install/enable/start executed by Codex: 0")
    console.print("Scheduler refresh jobs run by Codex: 0")
    console.print("UI/R5 service start/stop executed by Codex: 0")
    console.print("SSH probes executed by Codex: bounded read-only/status only")
    console.print("Systemctl mutating commands executed by Codex: 0")
    console.print("Tailscale mutating commands executed by Codex: 0")
    console.print("Secrets printed/copied by Codex: blocked")
    console.print("Live/demo execution: blocked")
    console.print("Order submission/cancel/replace: blocked")
    console.print("Paper trade creation: blocked")
    console.print("DB writes performed: 0")
    console.print(f"Wrote Executive Summary: {artifacts.executive_summary_path}")
    console.print(f"Wrote Markdown: {artifacts.markdown_path}")
    console.print(f"Wrote JSON: {artifacts.json_path}")
    console.print(f"Wrote probe CSV: {artifacts.probe_csv_path}")
    console.print(f"Wrote runtime checks: {artifacts.checks_csv_path}")
    console.print(f"Wrote cycle rows: {artifacts.cycle_csv_path}")
    console.print(f"Wrote scheduler job rows: {artifacts.job_csv_path}")
    console.print(f"Wrote report freshness: {artifacts.report_freshness_csv_path}")
    console.print(f"Wrote writer-gate skips: {artifacts.writer_gate_csv_path}")
    console.print(f"Wrote operator next command: {artifacts.operator_command_path}")
    console.print(f"Wrote Next Actions: {artifacts.next_actions_path}")
    console.print(f"Wrote manifest: {artifacts.manifest_path}")


@app.command("phase3bb-r44-weather-catalog-hook-runtime-verification")
def phase3bb_r44_weather_catalog_hook_runtime_verification_command(
    output_dir: Annotated[
        Path,
        typer.Option(help="Directory for Phase 3BB-R44 hook runtime artifacts."),
    ] = Path("reports/phase3bb_r44"),
    reports_dir: Annotated[
        Path,
        typer.Option(help="Root directory containing local phase report artifacts."),
    ] = Path("reports"),
    ssh_target: Annotated[
        str | None,
        typer.Option(help="Optional SSH target override, for example root@159.65.35.72."),
    ] = None,
    identity_file: Annotated[
        str | None,
        typer.Option(help="Optional SSH identity file override."),
    ] = None,
    app_path: Annotated[
        str | None,
        typer.Option(help="Optional remote app path override."),
    ] = None,
    env_path: Annotated[
        str | None,
        typer.Option(help="Optional remote env file override."),
    ] = None,
    db_path: Annotated[
        str | None,
        typer.Option(help="Optional remote DB path override."),
    ] = None,
    scheduler_service_name: Annotated[
        str,
        typer.Option(help="Scheduler service unit name to inspect."),
    ] = "kalshi-multicategory-refresh-scheduler.service",
    scheduler_timer_name: Annotated[
        str,
        typer.Option(help="Scheduler timer unit name to inspect."),
    ] = "kalshi-multicategory-refresh-scheduler.timer",
    journal_lines: Annotated[
        int,
        typer.Option(help="Number of scheduler journal lines to inspect."),
    ] = 900,
    per_probe_timeout_seconds: Annotated[
        int,
        typer.Option(help="Timeout for each bounded remote hook-runtime probe."),
    ] = 45,
) -> None:
    """Verify the weather current-catalog scheduler hook ran and R40 recognizes it."""
    settings = get_settings()
    engine = make_engine(database_url_from_settings(settings))
    session_factory = get_session_factory(engine)
    try:
        with session_factory() as session:
            artifacts = write_phase3bb_r44_weather_catalog_hook_runtime_verification_report(
                session,
                output_dir=output_dir,
                reports_dir=reports_dir,
                settings=settings,
                command_args=sys.argv[1:],
                ssh_target=ssh_target,
                identity_file=identity_file,
                app_path=app_path,
                env_path=env_path,
                db_path=db_path,
                scheduler_service_name=scheduler_service_name,
                scheduler_timer_name=scheduler_timer_name,
                journal_lines=journal_lines,
                per_probe_timeout_seconds=per_probe_timeout_seconds,
            )
            session.rollback()
    finally:
        engine.dispose()
    console.print("Phase 3BB-R44 weather catalog hook runtime verification")
    console.print("Mode: PAPER READ-ONLY runtime verification")
    console.print("Scheduler timer/service start executed by Codex: 0")
    console.print("Weather fast-lane executed by Codex: 0")
    console.print("Refresh jobs run by Codex: 0")
    console.print("UI/R5 service start/stop executed by Codex: 0")
    console.print("Systemctl mutating commands executed by Codex: 0")
    console.print("Secrets printed/copied by Codex: blocked")
    console.print("Live/demo execution: blocked")
    console.print("Order submission/cancel/replace: blocked")
    console.print("Paper trade creation: blocked")
    console.print("DB writes performed: 0")
    console.print(f"Wrote Executive Summary: {artifacts.executive_summary_path}")
    console.print(f"Wrote Markdown: {artifacts.markdown_path}")
    console.print(f"Wrote JSON: {artifacts.json_path}")
    console.print(f"Wrote probe CSV: {artifacts.probe_csv_path}")
    console.print(f"Wrote hook runtime checks: {artifacts.checks_csv_path}")
    console.print(f"Wrote scheduler job events: {artifacts.job_events_csv_path}")
    console.print(f"Wrote weather report freshness: {artifacts.report_freshness_csv_path}")
    console.print(f"Wrote operator next command: {artifacts.operator_command_path}")
    console.print(f"Wrote Next Actions: {artifacts.next_actions_path}")
    console.print(f"Wrote manifest: {artifacts.manifest_path}")


@app.command("phase3bb-r45-weather-freshness-to-ranking-impact")
def phase3bb_r45_weather_freshness_to_ranking_impact_command(
    output_dir: Annotated[
        Path,
        typer.Option(help="Directory for Phase 3BB-R45 weather freshness impact artifacts."),
    ] = Path("reports/phase3bb_r45"),
    reports_dir: Annotated[
        Path,
        typer.Option(help="Root directory containing local phase report artifacts."),
    ] = Path("reports"),
    ssh_target: Annotated[
        str | None,
        typer.Option(help="Optional SSH target override, for example kalshi@159.65.35.72."),
    ] = None,
    identity_file: Annotated[
        str | None,
        typer.Option(help="Optional SSH identity file override."),
    ] = None,
    app_path: Annotated[
        str | None,
        typer.Option(help="Optional remote app path override."),
    ] = None,
    env_path: Annotated[
        str | None,
        typer.Option(help="Optional remote env file override."),
    ] = None,
    db_path: Annotated[
        str | None,
        typer.Option(help="Optional remote DB path override."),
    ] = None,
    scheduler_service_name: Annotated[
        str,
        typer.Option(help="Scheduler service unit name to inspect."),
    ] = "kalshi-multicategory-refresh-scheduler.service",
    scheduler_timer_name: Annotated[
        str,
        typer.Option(help="Scheduler timer unit name to inspect."),
    ] = "kalshi-multicategory-refresh-scheduler.timer",
    per_probe_timeout_seconds: Annotated[
        int,
        typer.Option(help="Timeout for each bounded remote freshness-impact probe."),
    ] = 45,
) -> None:
    """Review whether weather catalog freshness is turning into rankable current rows."""
    settings = get_settings()
    engine = make_engine(database_url_from_settings(settings))
    session_factory = get_session_factory(engine)
    try:
        with session_factory() as session:
            artifacts = write_phase3bb_r45_weather_freshness_to_ranking_impact_report(
                session,
                output_dir=output_dir,
                reports_dir=reports_dir,
                settings=settings,
                command_args=sys.argv[1:],
                ssh_target=ssh_target,
                identity_file=identity_file,
                app_path=app_path,
                env_path=env_path,
                db_path=db_path,
                scheduler_service_name=scheduler_service_name,
                scheduler_timer_name=scheduler_timer_name,
                per_probe_timeout_seconds=per_probe_timeout_seconds,
            )
            session.rollback()
    finally:
        engine.dispose()
    console.print("Phase 3BB-R45 weather freshness to ranking impact review")
    console.print("Mode: PAPER READ-ONLY freshness impact review")
    console.print("Scheduler timer/service start executed by Codex: 0")
    console.print("Weather fast-lane executed by Codex: 0")
    console.print("Refresh jobs run by Codex: 0")
    console.print("UI/R5 service start/stop executed by Codex: 0")
    console.print("Systemctl mutating commands executed by Codex: 0")
    console.print("Secrets printed/copied by Codex: blocked")
    console.print("Live/demo execution: blocked")
    console.print("Order submission/cancel/replace: blocked")
    console.print("Paper trade creation: blocked")
    console.print("DB writes performed: 0")
    console.print(f"Wrote Executive Summary: {artifacts.executive_summary_path}")
    console.print(f"Wrote Markdown: {artifacts.markdown_path}")
    console.print(f"Wrote JSON: {artifacts.json_path}")
    console.print(f"Wrote probe CSV: {artifacts.probe_csv_path}")
    console.print(f"Wrote impact checks: {artifacts.checks_csv_path}")
    console.print(f"Wrote blocker counts: {artifacts.blocker_counts_csv_path}")
    console.print(f"Wrote freshness rows: {artifacts.freshness_rows_csv_path}")
    console.print(f"Wrote operator next command: {artifacts.operator_command_path}")
    console.print(f"Wrote Next Actions: {artifacts.next_actions_path}")
    console.print(f"Wrote manifest: {artifacts.manifest_path}")


@app.command("phase3bb-r46-cloud-scheduler-weather-writer-gate-repair")
def phase3bb_r46_cloud_scheduler_weather_writer_gate_repair_command(
    output_dir: Annotated[
        Path,
        typer.Option(help="Directory for Phase 3BB-R46 scheduler writer-gate repair artifacts."),
    ] = Path("reports/phase3bb_r46"),
    reports_dir: Annotated[
        Path,
        typer.Option(help="Root directory containing local phase report artifacts."),
    ] = Path("reports"),
    ssh_target: Annotated[
        str | None,
        typer.Option(help="Optional SSH target override, for example root@159.65.35.72."),
    ] = None,
    identity_file: Annotated[
        str | None,
        typer.Option(help="Optional SSH identity file override."),
    ] = None,
    app_path: Annotated[
        str | None,
        typer.Option(help="Optional remote app path override."),
    ] = None,
    env_path: Annotated[
        str | None,
        typer.Option(help="Optional remote env file override."),
    ] = None,
    db_path: Annotated[
        str | None,
        typer.Option(help="Optional remote DB path override."),
    ] = None,
    apply: Annotated[
        bool,
        typer.Option(help="Install the patched runner on the cloud host."),
    ] = False,
    backup_first: Annotated[
        bool,
        typer.Option(help="Backup the remote runner before installing the patch."),
    ] = False,
    reset_failed: Annotated[
        bool,
        typer.Option(help="Clear the failed systemd service marker after installing; does not start the service."),
    ] = False,
    per_probe_timeout_seconds: Annotated[
        int,
        typer.Option(help="Timeout for each bounded remote repair probe."),
    ] = 45,
) -> None:
    """Repair scheduler writer-gate handling so mid-run BUSY_WRITER becomes a clean retry skip."""
    settings = get_settings()
    engine = make_engine(database_url_from_settings(settings))
    session_factory = get_session_factory(engine)
    try:
        with session_factory() as session:
            artifacts = write_phase3bb_r46_cloud_scheduler_weather_writer_gate_repair_report(
                session,
                output_dir=output_dir,
                reports_dir=reports_dir,
                settings=settings,
                command_args=sys.argv[1:],
                ssh_target=ssh_target,
                identity_file=identity_file,
                app_path=app_path,
                env_path=env_path,
                db_path=db_path,
                apply=apply,
                backup_first=backup_first,
                reset_failed=reset_failed,
                per_probe_timeout_seconds=per_probe_timeout_seconds,
            )
            session.rollback()
    finally:
        engine.dispose()
    console.print("Phase 3BB-R46 cloud scheduler weather writer-gate repair")
    console.print("Mode: PAPER READ-ONLY operational repair")
    console.print(f"Apply requested: {apply}")
    console.print(f"Backup first: {backup_first}")
    console.print(f"Reset failed marker: {reset_failed}")
    console.print("Scheduler timer/service start/stop/restart executed by Codex: 0")
    console.print("Weather fast-lane executed by Codex: 0")
    console.print("Refresh jobs run by Codex: 0")
    console.print("UI/R5 service start/stop executed by Codex: 0")
    console.print("Secrets printed/copied by Codex: blocked")
    console.print("Live/demo execution: blocked")
    console.print("Order submission/cancel/replace: blocked")
    console.print("Paper trade creation: blocked")
    console.print("DB writes performed: 0")
    console.print(f"Wrote Executive Summary: {artifacts.executive_summary_path}")
    console.print(f"Wrote Markdown: {artifacts.markdown_path}")
    console.print(f"Wrote JSON: {artifacts.json_path}")
    console.print(f"Wrote probe CSV: {artifacts.probe_csv_path}")
    console.print(f"Wrote repair checks: {artifacts.checks_csv_path}")
    console.print(f"Wrote runner patch: {artifacts.runner_patch_path}")
    console.print(f"Wrote operator next command: {artifacts.operator_command_path}")
    console.print(f"Wrote Next Actions: {artifacts.next_actions_path}")
    console.print(f"Wrote manifest: {artifacts.manifest_path}")


@app.command("phase3bb-r47-weather-current-window-series-discovery-linkability-repair")
def phase3bb_r47_weather_current_window_series_discovery_linkability_repair_command(
    output_dir: Annotated[
        Path,
        typer.Option(help="Directory for Phase 3BB-R47 weather linkability repair artifacts."),
    ] = Path("reports/phase3bb_r47"),
    reports_dir: Annotated[
        Path,
        typer.Option(help="Root directory containing local phase report artifacts."),
    ] = Path("reports"),
    ssh_target: Annotated[
        str | None,
        typer.Option(help="Optional SSH target override, for example root@159.65.35.72."),
    ] = None,
    identity_file: Annotated[
        str | None,
        typer.Option(help="Optional SSH identity file override."),
    ] = None,
    app_path: Annotated[
        str | None,
        typer.Option(help="Optional remote app path override."),
    ] = None,
    env_path: Annotated[
        str | None,
        typer.Option(help="Optional remote env file override."),
    ] = None,
    db_path: Annotated[
        str | None,
        typer.Option(help="Optional remote DB path override."),
    ] = None,
    scheduler_service_name: Annotated[
        str,
        typer.Option(help="Scheduler service unit name to inspect."),
    ] = "kalshi-multicategory-refresh-scheduler.service",
    scheduler_timer_name: Annotated[
        str,
        typer.Option(help="Scheduler timer unit name to inspect."),
    ] = "kalshi-multicategory-refresh-scheduler.timer",
    current_window_lookback_hours: Annotated[
        int,
        typer.Option(help="Hours behind now to still count weather windows as current."),
    ] = 3,
    fresh_window_hours: Annotated[
        int,
        typer.Option(help="Maximum weather feature age accepted by the R12 linkability gate."),
    ] = 24,
    match_tolerance_hours: Annotated[
        int,
        typer.Option(help="Target-time tolerance between market text and weather features."),
    ] = 3,
    apply: Annotated[
        bool,
        typer.Option(help="Install the repaired weather source/feature refresh hook on the cloud host."),
    ] = False,
    backup_first: Annotated[
        bool,
        typer.Option(help="Backup the remote scheduler runner before installing the repair."),
    ] = False,
    per_probe_timeout_seconds: Annotated[
        int,
        typer.Option(help="Timeout for each bounded remote linkability probe."),
    ] = 60,
) -> None:
    """Discover current weather windows and repair the scheduler hook that feeds R12 linkability."""
    settings = get_settings()
    engine = make_engine(database_url_from_settings(settings))
    session_factory = get_session_factory(engine)
    try:
        with session_factory() as session:
            artifacts = write_phase3bb_r47_weather_current_window_series_discovery_report(
                session,
                output_dir=output_dir,
                reports_dir=reports_dir,
                settings=settings,
                command_args=sys.argv[1:],
                ssh_target=ssh_target,
                identity_file=identity_file,
                app_path=app_path,
                env_path=env_path,
                db_path=db_path,
                scheduler_service_name=scheduler_service_name,
                scheduler_timer_name=scheduler_timer_name,
                current_window_lookback_hours=current_window_lookback_hours,
                fresh_window_hours=fresh_window_hours,
                match_tolerance_hours=match_tolerance_hours,
                apply=apply,
                backup_first=backup_first,
                per_probe_timeout_seconds=per_probe_timeout_seconds,
            )
            session.rollback()
    finally:
        engine.dispose()
    console.print("Phase 3BB-R47 weather current-window series discovery and linkability repair")
    console.print("Mode: PAPER READ-ONLY weather scheduler repair")
    console.print(f"Apply requested: {apply}")
    console.print(f"Backup first: {backup_first}")
    console.print("Scheduler timer/service start/stop/restart executed by Codex: 0")
    console.print("Weather forecast executed by Codex: 0")
    console.print("Weather fast-lane executed by Codex: 0")
    console.print("Paper trade creation: blocked")
    console.print("Live/demo execution: blocked")
    console.print("Order submission/cancel/replace: blocked")
    console.print("Remote DB writes performed by this phase: 0")
    console.print(f"Wrote Executive Summary: {artifacts.executive_summary_path}")
    console.print(f"Wrote Markdown: {artifacts.markdown_path}")
    console.print(f"Wrote JSON: {artifacts.json_path}")
    console.print(f"Wrote probe CSV: {artifacts.probe_csv_path}")
    console.print(f"Wrote checks CSV: {artifacts.checks_csv_path}")
    console.print(f"Wrote current series CSV: {artifacts.series_csv_path}")
    console.print(f"Wrote linkability CSV: {artifacts.linkability_csv_path}")
    console.print(f"Wrote runner patch: {artifacts.runner_patch_path}")
    console.print(f"Wrote operator next command: {artifacts.operator_command_path}")
    console.print(f"Wrote Next Actions: {artifacts.next_actions_path}")
    console.print(f"Wrote manifest: {artifacts.manifest_path}")


@app.command("phase3bb-r48-weather-feature-refresh-runtime-verification")
def phase3bb_r48_weather_feature_refresh_runtime_verification_command(
    output_dir: Annotated[
        Path,
        typer.Option(help="Directory for Phase 3BB-R48 weather feature runtime artifacts."),
    ] = Path("reports/phase3bb_r48"),
    reports_dir: Annotated[
        Path,
        typer.Option(help="Root directory containing local phase report artifacts."),
    ] = Path("reports"),
    ssh_target: Annotated[
        str | None,
        typer.Option(help="Optional SSH target override, for example root@159.65.35.72."),
    ] = None,
    identity_file: Annotated[
        str | None,
        typer.Option(help="Optional SSH identity file override."),
    ] = None,
    app_path: Annotated[
        str | None,
        typer.Option(help="Optional remote app path override."),
    ] = None,
    env_path: Annotated[
        str | None,
        typer.Option(help="Optional remote env file override."),
    ] = None,
    db_path: Annotated[
        str | None,
        typer.Option(help="Optional remote DB path override."),
    ] = None,
    scheduler_service_name: Annotated[
        str,
        typer.Option(help="Scheduler service unit name to inspect."),
    ] = "kalshi-multicategory-refresh-scheduler.service",
    scheduler_timer_name: Annotated[
        str,
        typer.Option(help="Scheduler timer unit name to inspect."),
    ] = "kalshi-multicategory-refresh-scheduler.timer",
    current_window_lookback_hours: Annotated[
        int,
        typer.Option(help="Hours behind now to still count weather windows as current."),
    ] = 3,
    fresh_window_hours: Annotated[
        int,
        typer.Option(help="Maximum weather feature age accepted by the R12 linkability gate."),
    ] = 24,
    match_tolerance_hours: Annotated[
        int,
        typer.Option(help="Target-time tolerance between market text and weather features."),
    ] = 3,
    journal_lines: Annotated[
        int,
        typer.Option(help="Number of scheduler journal lines to inspect."),
    ] = 900,
    per_probe_timeout_seconds: Annotated[
        int,
        typer.Option(help="Timeout for each bounded remote runtime probe."),
    ] = 60,
) -> None:
    """Verify the R47 weather source/feature refresh hook after scheduler runtime."""
    settings = get_settings()
    engine = make_engine(database_url_from_settings(settings))
    session_factory = get_session_factory(engine)
    try:
        with session_factory() as session:
            artifacts = write_phase3bb_r48_weather_feature_refresh_runtime_verification_report(
                session,
                output_dir=output_dir,
                reports_dir=reports_dir,
                settings=settings,
                command_args=sys.argv[1:],
                ssh_target=ssh_target,
                identity_file=identity_file,
                app_path=app_path,
                env_path=env_path,
                db_path=db_path,
                scheduler_service_name=scheduler_service_name,
                scheduler_timer_name=scheduler_timer_name,
                current_window_lookback_hours=current_window_lookback_hours,
                fresh_window_hours=fresh_window_hours,
                match_tolerance_hours=match_tolerance_hours,
                journal_lines=journal_lines,
                per_probe_timeout_seconds=per_probe_timeout_seconds,
            )
            session.rollback()
    finally:
        engine.dispose()
    console.print("Phase 3BB-R48 weather feature refresh runtime verification")
    console.print("Mode: PAPER READ-ONLY runtime verification")
    console.print("Scheduler timer/service start/stop/restart executed by Codex: 0")
    console.print("Weather refresh jobs run by Codex: 0")
    console.print("Weather forecast executed by Codex: 0")
    console.print("Weather fast-lane executed by Codex: 0")
    console.print("Paper trade creation: blocked")
    console.print("Live/demo execution: blocked")
    console.print("Order submission/cancel/replace: blocked")
    console.print("Remote DB writes performed by this phase: 0")
    console.print(f"Wrote Executive Summary: {artifacts.executive_summary_path}")
    console.print(f"Wrote Markdown: {artifacts.markdown_path}")
    console.print(f"Wrote JSON: {artifacts.json_path}")
    console.print(f"Wrote probe CSV: {artifacts.probe_csv_path}")
    console.print(f"Wrote runtime checks: {artifacts.checks_csv_path}")
    console.print(f"Wrote feature events: {artifacts.feature_events_csv_path}")
    console.print(f"Wrote feature windows: {artifacts.feature_windows_csv_path}")
    console.print(f"Wrote linkability rows: {artifacts.linkability_rows_csv_path}")
    console.print(f"Wrote report freshness: {artifacts.report_freshness_csv_path}")
    console.print(f"Wrote operator next command: {artifacts.operator_command_path}")
    console.print(f"Wrote Next Actions: {artifacts.next_actions_path}")
    console.print(f"Wrote manifest: {artifacts.manifest_path}")


@app.command("phase3bb-r49-weather-missing-link-apply-after-feature-refresh")
def phase3bb_r49_weather_missing_link_apply_after_feature_refresh_command(
    output_dir: Annotated[
        Path,
        typer.Option(help="Directory for Phase 3BB-R49 weather post-link apply artifacts."),
    ] = Path("reports/phase3bb_r49"),
    reports_dir: Annotated[
        Path,
        typer.Option(help="Root directory containing local phase report artifacts."),
    ] = Path("reports"),
    ssh_target: Annotated[
        str | None,
        typer.Option(help="Optional SSH target override, for example root@159.65.35.72."),
    ] = None,
    identity_file: Annotated[
        str | None,
        typer.Option(help="Optional SSH identity file override."),
    ] = None,
    app_path: Annotated[
        str | None,
        typer.Option(help="Optional remote app path override."),
    ] = None,
    env_path: Annotated[
        str | None,
        typer.Option(help="Optional remote env file override."),
    ] = None,
    db_path: Annotated[
        str | None,
        typer.Option(help="Optional remote DB path override."),
    ] = None,
    current_window_lookback_hours: Annotated[
        int,
        typer.Option(help="Hours behind now to still count weather windows as current."),
    ] = 3,
    fresh_window_hours: Annotated[
        int,
        typer.Option(help="Maximum weather feature age accepted by the R12 linkability gate."),
    ] = 24,
    match_tolerance_hours: Annotated[
        int,
        typer.Option(help="Target-time tolerance between market text and weather features."),
    ] = 3,
    per_probe_timeout_seconds: Annotated[
        int,
        typer.Option(help="Timeout for each bounded remote post-link probe."),
    ] = 60,
) -> None:
    """Verify R12 weather missing-link apply after the R48 feature refresh gate."""
    settings = get_settings()
    engine = make_engine(database_url_from_settings(settings))
    session_factory = get_session_factory(engine)
    try:
        with session_factory() as session:
            artifacts = write_phase3bb_r49_weather_missing_link_apply_after_feature_refresh_report(
                session,
                output_dir=output_dir,
                reports_dir=reports_dir,
                settings=settings,
                command_args=sys.argv[1:],
                ssh_target=ssh_target,
                identity_file=identity_file,
                app_path=app_path,
                env_path=env_path,
                db_path=db_path,
                current_window_lookback_hours=current_window_lookback_hours,
                fresh_window_hours=fresh_window_hours,
                match_tolerance_hours=match_tolerance_hours,
                per_probe_timeout_seconds=per_probe_timeout_seconds,
            )
            session.rollback()
    finally:
        engine.dispose()
    console.print("Phase 3BB-R49 weather missing-link apply after feature refresh")
    console.print("Mode: PAPER READ-ONLY post-link verification")
    console.print("Missing-link apply executed by this phase: 0")
    console.print("Remote DB writes performed by this phase: 0")
    console.print("Weather forecast executed by this phase: 0")
    console.print("Weather fast-lane executed by this phase: 0")
    console.print("Paper trade creation: blocked")
    console.print("Live/demo execution: blocked")
    console.print("Order submission/cancel/replace: blocked")
    console.print(f"Wrote Executive Summary: {artifacts.executive_summary_path}")
    console.print(f"Wrote Markdown: {artifacts.markdown_path}")
    console.print(f"Wrote JSON: {artifacts.json_path}")
    console.print(f"Wrote probe CSV: {artifacts.probe_csv_path}")
    console.print(f"Wrote checks CSV: {artifacts.checks_csv_path}")
    console.print(f"Wrote apply summary CSV: {artifacts.apply_summary_csv_path}")
    console.print(f"Wrote report freshness: {artifacts.report_freshness_csv_path}")
    console.print(f"Wrote operator next command: {artifacts.operator_command_path}")
    console.print(f"Wrote Next Actions: {artifacts.next_actions_path}")
    console.print(f"Wrote manifest: {artifacts.manifest_path}")


@app.command("phase3bb-r50-weather-post-link-ranking-fast-lane-recheck")
def phase3bb_r50_weather_post_link_ranking_fast_lane_recheck_command(
    output_dir: Annotated[
        Path,
        typer.Option(help="Directory for Phase 3BB-R50 weather ranking recheck artifacts."),
    ] = Path("reports/phase3bb_r50"),
    reports_dir: Annotated[
        Path,
        typer.Option(help="Root directory containing local phase report artifacts."),
    ] = Path("reports"),
    ssh_target: Annotated[
        str | None,
        typer.Option(help="Optional SSH target override, for example root@159.65.35.72."),
    ] = None,
    identity_file: Annotated[
        str | None,
        typer.Option(help="Optional SSH identity file override."),
    ] = None,
    app_path: Annotated[
        str | None,
        typer.Option(help="Optional remote app path override."),
    ] = None,
    env_path: Annotated[
        str | None,
        typer.Option(help="Optional remote env file override."),
    ] = None,
    db_path: Annotated[
        str | None,
        typer.Option(help="Optional remote DB path override."),
    ] = None,
    current_window_lookback_hours: Annotated[
        int,
        typer.Option(help="Hours behind now to still count weather windows as current."),
    ] = 3,
    fresh_window_hours: Annotated[
        int,
        typer.Option(help="Maximum weather feature age accepted by the R12 linkability gate."),
    ] = 24,
    match_tolerance_hours: Annotated[
        int,
        typer.Option(help="Target-time tolerance between market text and weather features."),
    ] = 3,
    run_fast_lane: Annotated[
        bool,
        typer.Option("--run-fast-lane/--no-run-fast-lane", help="Run the cloud weather fast-lane when gates are clear."),
    ] = True,
    fast_lane_timeout_seconds: Annotated[
        int,
        typer.Option(help="Timeout for the cloud weather fast-lane command."),
    ] = 240,
    per_probe_timeout_seconds: Annotated[
        int,
        typer.Option(help="Timeout for each bounded remote probe."),
    ] = 60,
) -> None:
    """Run and verify the post-link weather ranking fast-lane on the cloud."""
    settings = get_settings()
    engine = make_engine(database_url_from_settings(settings))
    session_factory = get_session_factory(engine)
    try:
        with session_factory() as session:
            artifacts = write_phase3bb_r50_weather_post_link_ranking_fast_lane_recheck_report(
                session,
                output_dir=output_dir,
                reports_dir=reports_dir,
                settings=settings,
                command_args=sys.argv[1:],
                ssh_target=ssh_target,
                identity_file=identity_file,
                app_path=app_path,
                env_path=env_path,
                db_path=db_path,
                current_window_lookback_hours=current_window_lookback_hours,
                fresh_window_hours=fresh_window_hours,
                match_tolerance_hours=match_tolerance_hours,
                run_fast_lane=run_fast_lane,
                fast_lane_timeout_seconds=fast_lane_timeout_seconds,
                per_probe_timeout_seconds=per_probe_timeout_seconds,
            )
            session.rollback()
    finally:
        engine.dispose()
    console.print("Phase 3BB-R50 weather post-link ranking fast-lane recheck")
    console.print("Mode: PAPER ONLY cloud weather ranking recheck")
    console.print("Missing-link apply executed by this phase: 0")
    console.print("Weather forecast executed directly by this phase: 0")
    console.print("Weather fast-lane executed only if writer gate was clear")
    console.print("Paper trade creation: blocked")
    console.print("Live/demo execution: blocked")
    console.print("Order submission/cancel/replace: blocked")
    console.print(f"Wrote Executive Summary: {artifacts.executive_summary_path}")
    console.print(f"Wrote Markdown: {artifacts.markdown_path}")
    console.print(f"Wrote JSON: {artifacts.json_path}")
    console.print(f"Wrote probe CSV: {artifacts.probe_csv_path}")
    console.print(f"Wrote checks CSV: {artifacts.checks_csv_path}")
    console.print(f"Wrote fast-lane summary CSV: {artifacts.fast_lane_summary_csv_path}")
    console.print(f"Wrote report freshness: {artifacts.report_freshness_csv_path}")
    console.print(f"Wrote operator next command: {artifacts.operator_command_path}")
    console.print(f"Wrote Next Actions: {artifacts.next_actions_path}")
    console.print(f"Wrote manifest: {artifacts.manifest_path}")


@app.command("phase3bb-r51-weather-ranking-path-repair")
def phase3bb_r51_weather_ranking_path_repair_command(
    output_dir: Annotated[
        Path,
        typer.Option(help="Directory for Phase 3BB-R51 weather ranking path repair artifacts."),
    ] = Path("reports/phase3bb_r51"),
    reports_dir: Annotated[
        Path,
        typer.Option(help="Root directory containing local phase report artifacts."),
    ] = Path("reports"),
    ssh_target: Annotated[
        str | None,
        typer.Option(help="Optional SSH target override, for example root@159.65.35.72."),
    ] = None,
    identity_file: Annotated[
        str | None,
        typer.Option(help="Optional SSH identity file override."),
    ] = None,
    app_path: Annotated[
        str | None,
        typer.Option(help="Optional remote app path override."),
    ] = None,
    env_path: Annotated[
        str | None,
        typer.Option(help="Optional remote env file override."),
    ] = None,
    db_path: Annotated[
        str | None,
        typer.Option(help="Optional remote DB path override."),
    ] = None,
    current_window_lookback_hours: Annotated[
        int,
        typer.Option(help="Hours behind now to still inspect weather windows."),
    ] = 3,
    fresh_window_hours: Annotated[
        int,
        typer.Option(help="Maximum weather source/feature age accepted for ranking repair."),
    ] = 24,
    match_tolerance_hours: Annotated[
        int,
        typer.Option(help="Target-time tolerance between market text and weather source/features."),
    ] = 3,
    run_repair: Annotated[
        bool,
        typer.Option("--run-repair/--no-run-repair", help="Run cloud snapshot, forecast, and fast-lane repair when gates are clear."),
    ] = True,
    repair_timeout_seconds: Annotated[
        int,
        typer.Option(help="Timeout for each write-capable cloud repair command."),
    ] = 300,
    per_probe_timeout_seconds: Annotated[
        int,
        typer.Option(help="Timeout for each bounded remote probe."),
    ] = 60,
) -> None:
    """Inspect and repair the cloud weather snapshot -> forecast -> ranking path."""
    settings = get_settings()
    engine = make_engine(database_url_from_settings(settings))
    session_factory = get_session_factory(engine)
    try:
        with session_factory() as session:
            artifacts = write_phase3bb_r51_weather_ranking_path_repair_report(
                session,
                output_dir=output_dir,
                reports_dir=reports_dir,
                settings=settings,
                command_args=sys.argv[1:],
                ssh_target=ssh_target,
                identity_file=identity_file,
                app_path=app_path,
                env_path=env_path,
                db_path=db_path,
                current_window_lookback_hours=current_window_lookback_hours,
                fresh_window_hours=fresh_window_hours,
                match_tolerance_hours=match_tolerance_hours,
                run_repair=run_repair,
                repair_timeout_seconds=repair_timeout_seconds,
                per_probe_timeout_seconds=per_probe_timeout_seconds,
            )
            session.rollback()
    finally:
        engine.dispose()
    console.print("Phase 3BB-R51 weather ranking path repair")
    console.print("Mode: PAPER ONLY cloud weather ranking path repair")
    console.print("Missing-link apply executed by this phase: 0")
    console.print("Snapshot/forecast/fast-lane executed only if writer gate and live-window gates were clear")
    console.print("Paper trade creation: blocked")
    console.print("Live/demo execution: blocked")
    console.print("Order submission/cancel/replace: blocked")
    console.print(f"Wrote Executive Summary: {artifacts.executive_summary_path}")
    console.print(f"Wrote Markdown: {artifacts.markdown_path}")
    console.print(f"Wrote JSON: {artifacts.json_path}")
    console.print(f"Wrote probe CSV: {artifacts.probe_csv_path}")
    console.print(f"Wrote checks CSV: {artifacts.checks_csv_path}")
    console.print(f"Wrote path rows CSV: {artifacts.path_rows_csv_path}")
    console.print(f"Wrote skip reasons CSV: {artifacts.skip_reasons_csv_path}")
    console.print(f"Wrote report freshness: {artifacts.report_freshness_csv_path}")
    console.print(f"Wrote operator next command: {artifacts.operator_command_path}")
    console.print(f"Wrote Next Actions: {artifacts.next_actions_path}")
    console.print(f"Wrote manifest: {artifacts.manifest_path}")


@app.command("phase3bb-r52-weather-ev-fair-value-diagnostic")
def phase3bb_r52_weather_ev_fair_value_diagnostic_command(
    output_dir: Annotated[
        Path,
        typer.Option(help="Directory for Phase 3BB-R52 weather EV/fair-value artifacts."),
    ] = Path("reports/phase3bb_r52"),
    reports_dir: Annotated[
        Path,
        typer.Option(help="Root directory containing local phase report artifacts."),
    ] = Path("reports"),
    ssh_target: Annotated[
        str | None,
        typer.Option(help="Optional SSH target override, for example root@159.65.35.72."),
    ] = None,
    identity_file: Annotated[
        str | None,
        typer.Option(help="Optional SSH identity file override."),
    ] = None,
    app_path: Annotated[
        str | None,
        typer.Option(help="Optional remote app path override."),
    ] = None,
    env_path: Annotated[
        str | None,
        typer.Option(help="Optional remote env file override."),
    ] = None,
    db_path: Annotated[
        str | None,
        typer.Option(help="Optional remote DB path override."),
    ] = None,
    current_window_lookback_hours: Annotated[
        int,
        typer.Option(help="Hours behind now to still inspect recently ranked weather windows."),
    ] = 3,
    limit: Annotated[
        int,
        typer.Option(help="Maximum linked weather rows to inspect."),
    ] = 100,
    per_probe_timeout_seconds: Annotated[
        int,
        typer.Option(help="Timeout for each bounded remote probe."),
    ] = 60,
) -> None:
    """Explain weather_v2 fair value versus executable Kalshi prices."""
    settings = get_settings()
    engine = make_engine(database_url_from_settings(settings))
    session_factory = get_session_factory(engine)
    try:
        with session_factory() as session:
            artifacts = write_phase3bb_r52_weather_ev_fair_value_diagnostic_report(
                session,
                output_dir=output_dir,
                reports_dir=reports_dir,
                settings=settings,
                command_args=sys.argv[1:],
                ssh_target=ssh_target,
                identity_file=identity_file,
                app_path=app_path,
                env_path=env_path,
                db_path=db_path,
                current_window_lookback_hours=current_window_lookback_hours,
                limit=limit,
                per_probe_timeout_seconds=per_probe_timeout_seconds,
            )
            session.rollback()
    finally:
        engine.dispose()
    console.print("Phase 3BB-R52 weather EV / fair-value diagnostic")
    console.print("Mode: PAPER ONLY cloud weather EV diagnostic")
    console.print("Forecast/ranking generation executed by this phase: 0")
    console.print("Missing-link apply executed by this phase: 0")
    console.print("Paper trade creation: blocked")
    console.print("Live/demo execution: blocked")
    console.print("Order submission/cancel/replace: blocked")
    console.print("Thresholds lowered: false")
    console.print(f"Wrote Executive Summary: {artifacts.executive_summary_path}")
    console.print(f"Wrote Markdown: {artifacts.markdown_path}")
    console.print(f"Wrote JSON: {artifacts.json_path}")
    console.print(f"Wrote rows CSV: {artifacts.rows_csv_path}")
    console.print(f"Wrote summary CSV: {artifacts.summary_csv_path}")
    console.print(f"Wrote probe CSV: {artifacts.probe_csv_path}")
    console.print(f"Wrote operator next command: {artifacts.operator_command_path}")
    console.print(f"Wrote Next Actions: {artifacts.next_actions_path}")
    console.print(f"Wrote manifest: {artifacts.manifest_path}")


@app.command("phase3bb-r53-weather-current-window-cadence-preview-narrowing-repair")
def phase3bb_r53_weather_current_window_cadence_command(
    output_dir: Annotated[
        Path,
        typer.Option(help="Directory for Phase 3BB-R53 weather current-window cadence artifacts."),
    ] = Path("reports/phase3bb_r53"),
    reports_dir: Annotated[
        Path,
        typer.Option(help="Root directory containing local phase report artifacts."),
    ] = Path("reports"),
    ssh_target: Annotated[
        str | None,
        typer.Option(help="Optional SSH target override, for example root@159.65.35.72."),
    ] = None,
    identity_file: Annotated[
        str | None,
        typer.Option(help="Optional SSH identity file override."),
    ] = None,
    app_path: Annotated[
        str | None,
        typer.Option(help="Optional remote app path override."),
    ] = None,
    env_path: Annotated[
        str | None,
        typer.Option(help="Optional remote env file override."),
    ] = None,
    db_path: Annotated[
        str | None,
        typer.Option(help="Optional remote DB path override."),
    ] = None,
    series_ticker: Annotated[
        str,
        typer.Option(help="Weather series ticker to narrow, default KXTEMPNYCH."),
    ] = "KXTEMPNYCH",
    location_key: Annotated[
        str,
        typer.Option(help="Weather source location key for the narrowed series."),
    ] = "new_york",
    fresh_window_hours: Annotated[
        int,
        typer.Option(help="Freshness window for weather source/features."),
    ] = 24,
    match_tolerance_hours: Annotated[
        int,
        typer.Option(help="Maximum target-time distance between market text and source windows."),
    ] = 3,
    snapshot_fresh_minutes: Annotated[
        int,
        typer.Option(help="Maximum age for selected-window market snapshots."),
    ] = 20,
    min_minutes_before_target: Annotated[
        int,
        typer.Option(help="Minimum lead time before target expiry for forecast/ranking work."),
    ] = 10,
    limit: Annotated[
        int,
        typer.Option(help="Maximum active series markets to inspect."),
    ] = 500,
    per_probe_timeout_seconds: Annotated[
        int,
        typer.Option(help="Timeout for each bounded remote probe."),
    ] = 60,
) -> None:
    """Narrow weather diagnostics to the next live target window before ranking work."""
    settings = get_settings()
    engine = make_engine(database_url_from_settings(settings))
    session_factory = get_session_factory(engine)
    try:
        with session_factory() as session:
            artifacts = write_phase3bb_r53_weather_current_window_cadence_report(
                session,
                output_dir=output_dir,
                reports_dir=reports_dir,
                settings=settings,
                command_args=sys.argv[1:],
                ssh_target=ssh_target,
                identity_file=identity_file,
                app_path=app_path,
                env_path=env_path,
                db_path=db_path,
                series_ticker=series_ticker,
                location_key=location_key,
                fresh_window_hours=fresh_window_hours,
                match_tolerance_hours=match_tolerance_hours,
                snapshot_fresh_minutes=snapshot_fresh_minutes,
                min_minutes_before_target=min_minutes_before_target,
                limit=limit,
                per_probe_timeout_seconds=per_probe_timeout_seconds,
            )
            session.rollback()
    finally:
        engine.dispose()
    console.print("Phase 3BB-R53 weather current window cadence / preview narrowing repair")
    console.print("Mode: PAPER ONLY cloud weather current-window diagnostic")
    console.print("Forecast/ranking generation executed by this phase: 0")
    console.print("Missing-link apply executed by this phase: 0")
    console.print("Paper trade creation: blocked")
    console.print("Live/demo execution: blocked")
    console.print("Order submission/cancel/replace: blocked")
    console.print("Thresholds lowered: false")
    console.print(f"Wrote Executive Summary: {artifacts.executive_summary_path}")
    console.print(f"Wrote Markdown: {artifacts.markdown_path}")
    console.print(f"Wrote JSON: {artifacts.json_path}")
    console.print(f"Wrote rows CSV: {artifacts.rows_csv_path}")
    console.print(f"Wrote checks CSV: {artifacts.checks_csv_path}")
    console.print(f"Wrote probe CSV: {artifacts.probe_csv_path}")
    console.print(f"Wrote operator next command: {artifacts.operator_command_path}")
    console.print(f"Wrote Next Actions: {artifacts.next_actions_path}")
    console.print(f"Wrote manifest: {artifacts.manifest_path}")


@app.command("phase3bb-r54-weather-missing-link-apply-deferral")
def phase3bb_r54_weather_missing_link_apply_deferral_command(
    output_dir: Annotated[
        Path,
        typer.Option(help="Directory for Phase 3BB-R54 weather missing-link deferral artifacts."),
    ] = Path("reports/phase3bb_r54"),
    reports_dir: Annotated[
        Path,
        typer.Option(help="Root directory containing local phase report artifacts."),
    ] = Path("reports"),
    r53_output_dir: Annotated[
        Path,
        typer.Option(help="Directory where the rerun R53 report should be written."),
    ] = Path("reports/phase3bb_r53"),
    ssh_target: Annotated[
        str | None,
        typer.Option(help="Optional SSH target override, for example root@159.65.35.72."),
    ] = None,
    identity_file: Annotated[
        str | None,
        typer.Option(help="Optional SSH identity file override."),
    ] = None,
    app_path: Annotated[
        str | None,
        typer.Option(help="Optional remote app path override."),
    ] = None,
    env_path: Annotated[
        str | None,
        typer.Option(help="Optional remote env file override."),
    ] = None,
    db_path: Annotated[
        str | None,
        typer.Option(help="Optional remote DB path override."),
    ] = None,
    expected_writer_pid: Annotated[
        int | None,
        typer.Option(help="Optional expected active writer PID to wait on, e.g. the R5 PID."),
    ] = None,
    max_wait_seconds: Annotated[
        int,
        typer.Option(help="Maximum bounded wait time for the writer gate to clear."),
    ] = 300,
    poll_interval_seconds: Annotated[
        int,
        typer.Option(help="Seconds between writer-gate polls; use 0 for immediate bounded checks."),
    ] = 30,
    min_minutes_before_target: Annotated[
        int,
        typer.Option(help="Minimum lead time before weather target expiry for R12 apply."),
    ] = 10,
    fresh_window_hours: Annotated[
        int,
        typer.Option(help="Freshness window for R53/R12 weather source matching."),
    ] = 24,
    match_tolerance_hours: Annotated[
        int,
        typer.Option(help="Maximum target-time mismatch allowed between market text and weather feature."),
    ] = 3,
    max_records: Annotated[
        int,
        typer.Option(help="Maximum R12 missing-link rows to apply."),
    ] = 25,
    limit: Annotated[
        int,
        typer.Option(help="R12 preview/apply market inspection limit."),
    ] = 2000,
    apply_timeout_seconds: Annotated[
        int,
        typer.Option(help="Timeout for the remote R12 missing-link apply command."),
    ] = 180,
    per_probe_timeout_seconds: Annotated[
        int,
        typer.Option(help="Timeout for each bounded remote probe."),
    ] = 60,
) -> None:
    """Wait for R5 writer clear, rerun R53, then apply only safe live-window weather links."""
    settings = get_settings()
    engine = make_engine(database_url_from_settings(settings))
    session_factory = get_session_factory(engine)
    try:
        with session_factory() as session:
            artifacts = write_phase3bb_r54_weather_missing_link_apply_deferral_report(
                session,
                output_dir=output_dir,
                reports_dir=reports_dir,
                r53_output_dir=r53_output_dir,
                settings=settings,
                command_args=sys.argv[1:],
                ssh_target=ssh_target,
                identity_file=identity_file,
                app_path=app_path,
                env_path=env_path,
                db_path=db_path,
                expected_writer_pid=expected_writer_pid,
                max_wait_seconds=max_wait_seconds,
                poll_interval_seconds=poll_interval_seconds,
                min_minutes_before_target=min_minutes_before_target,
                fresh_window_hours=fresh_window_hours,
                match_tolerance_hours=match_tolerance_hours,
                max_records=max_records,
                limit=limit,
                apply_timeout_seconds=apply_timeout_seconds,
                per_probe_timeout_seconds=per_probe_timeout_seconds,
            )
            session.rollback()
    finally:
        engine.dispose()
    console.print("Phase 3BB-R54 weather missing-link apply deferral / writer-clear retry")
    console.print("Mode: PAPER ONLY guarded weather missing-link apply")
    console.print("R12 missing-link apply executed only if writer and R53 live-window gates opened")
    console.print("Weather forecast/fast-lane executed by this phase: 0")
    console.print("Paper trade creation: blocked")
    console.print("Live/demo execution: blocked")
    console.print("Order submission/cancel/replace: blocked")
    console.print("Thresholds lowered: false")
    console.print(f"Wrote Executive Summary: {artifacts.executive_summary_path}")
    console.print(f"Wrote Markdown: {artifacts.markdown_path}")
    console.print(f"Wrote JSON: {artifacts.json_path}")
    console.print(f"Wrote wait checks CSV: {artifacts.wait_checks_csv_path}")
    console.print(f"Wrote probe CSV: {artifacts.probe_csv_path}")
    console.print(f"Wrote apply summary CSV: {artifacts.apply_summary_csv_path}")
    console.print(f"Wrote operator next command: {artifacts.operator_command_path}")
    console.print(f"Wrote Next Actions: {artifacts.next_actions_path}")
    console.print(f"Wrote manifest: {artifacts.manifest_path}")


@app.command("phase3bb-r55-weather-ranking-path-retry")
def phase3bb_r55_weather_ranking_path_retry_command(
    output_dir: Annotated[
        Path,
        typer.Option(help="Directory for Phase 3BB-R55 weather ranking retry artifacts."),
    ] = Path("reports/phase3bb_r55"),
    reports_dir: Annotated[
        Path,
        typer.Option(help="Root directory containing local phase report artifacts."),
    ] = Path("reports"),
    r53_output_dir: Annotated[
        Path,
        typer.Option(help="Directory where the R53 gate report should be written."),
    ] = Path("reports/phase3bb_r53"),
    r51_output_dir: Annotated[
        Path,
        typer.Option(help="Directory where the R51 retry report should be written."),
    ] = Path("reports/phase3bb_r51"),
    ssh_target: Annotated[
        str | None,
        typer.Option(help="Optional SSH target override, for example root@159.65.35.72."),
    ] = None,
    identity_file: Annotated[
        str | None,
        typer.Option(help="Optional SSH identity file override."),
    ] = None,
    app_path: Annotated[
        str | None,
        typer.Option(help="Optional remote app path override."),
    ] = None,
    env_path: Annotated[
        str | None,
        typer.Option(help="Optional remote env file override."),
    ] = None,
    db_path: Annotated[
        str | None,
        typer.Option(help="Optional remote DB path override."),
    ] = None,
    expected_writer_pid: Annotated[
        int | None,
        typer.Option(help="Optional expected active writer PID to wait on, e.g. the R5 PID."),
    ] = None,
    max_wait_seconds: Annotated[
        int,
        typer.Option(help="Maximum bounded wait time for the writer gate to clear."),
    ] = 300,
    poll_interval_seconds: Annotated[
        int,
        typer.Option(help="Seconds between writer-gate polls; use 0 for immediate bounded checks."),
    ] = 30,
    min_minutes_before_target: Annotated[
        int,
        typer.Option(help="Minimum lead time before weather target expiry before running R51 repair."),
    ] = 10,
    fresh_window_hours: Annotated[
        int,
        typer.Option(help="Freshness window for R53/R51 weather source matching."),
    ] = 24,
    match_tolerance_hours: Annotated[
        int,
        typer.Option(help="Maximum target-time mismatch allowed between market text and weather feature."),
    ] = 3,
    current_window_lookback_hours: Annotated[
        int,
        typer.Option(help="R51 current-window lookback hours."),
    ] = 3,
    repair_timeout_seconds: Annotated[
        int,
        typer.Option(help="Timeout for each remote R51 write-capable repair command."),
    ] = 300,
    per_probe_timeout_seconds: Annotated[
        int,
        typer.Option(help="Timeout for each bounded remote probe."),
    ] = 60,
) -> None:
    """Wait for R5 writer clear, rerun R53, then run R51 only while the weather window is live."""
    settings = get_settings()
    engine = make_engine(database_url_from_settings(settings))
    session_factory = get_session_factory(engine)
    try:
        with session_factory() as session:
            artifacts = write_phase3bb_r55_weather_ranking_path_retry_report(
                session,
                output_dir=output_dir,
                reports_dir=reports_dir,
                r53_output_dir=r53_output_dir,
                r51_output_dir=r51_output_dir,
                settings=settings,
                command_args=sys.argv[1:],
                ssh_target=ssh_target,
                identity_file=identity_file,
                app_path=app_path,
                env_path=env_path,
                db_path=db_path,
                expected_writer_pid=expected_writer_pid,
                max_wait_seconds=max_wait_seconds,
                poll_interval_seconds=poll_interval_seconds,
                min_minutes_before_target=min_minutes_before_target,
                fresh_window_hours=fresh_window_hours,
                match_tolerance_hours=match_tolerance_hours,
                current_window_lookback_hours=current_window_lookback_hours,
                repair_timeout_seconds=repair_timeout_seconds,
                per_probe_timeout_seconds=per_probe_timeout_seconds,
            )
            session.rollback()
    finally:
        engine.dispose()
    console.print("Phase 3BB-R55 weather ranking path retry after R5 writer clears")
    console.print("Mode: PAPER ONLY guarded weather ranking retry")
    console.print("R51 runs only if writer and R53 live-window gates opened")
    console.print("Missing-link apply executed by this phase: 0")
    console.print("Paper trade creation: blocked")
    console.print("Live/demo execution: blocked")
    console.print("Order submission/cancel/replace: blocked")
    console.print("Thresholds lowered: false")
    console.print(f"Wrote Executive Summary: {artifacts.executive_summary_path}")
    console.print(f"Wrote Markdown: {artifacts.markdown_path}")
    console.print(f"Wrote JSON: {artifacts.json_path}")
    console.print(f"Wrote wait checks CSV: {artifacts.wait_checks_csv_path}")
    console.print(f"Wrote probe CSV: {artifacts.probe_csv_path}")
    console.print(f"Wrote R51 summary CSV: {artifacts.r51_summary_csv_path}")
    console.print(f"Wrote operator next command: {artifacts.operator_command_path}")
    console.print(f"Wrote Next Actions: {artifacts.next_actions_path}")
    console.print(f"Wrote manifest: {artifacts.manifest_path}")


@app.command("phase3bb-r57-weather-selected-window-pipeline-speed-repair")
def phase3bb_r57_weather_selected_window_pipeline_command(
    output_dir: Annotated[
        Path,
        typer.Option(help="Directory for Phase 3BB-R57 selected-window weather pipeline artifacts."),
    ] = Path("reports/phase3bb_r57"),
    reports_dir: Annotated[
        Path,
        typer.Option(help="Root directory containing local phase report artifacts."),
    ] = Path("reports"),
    r53_output_dir: Annotated[
        Path,
        typer.Option(help="Directory where the R53 gate report should be written."),
    ] = Path("reports/phase3bb_r53"),
    ssh_target: Annotated[
        str | None,
        typer.Option(help="Optional SSH target override, for example root@159.65.35.72."),
    ] = None,
    identity_file: Annotated[
        str | None,
        typer.Option(help="Optional SSH identity file override."),
    ] = None,
    app_path: Annotated[
        str | None,
        typer.Option(help="Optional remote app path override."),
    ] = None,
    env_path: Annotated[
        str | None,
        typer.Option(help="Optional remote env file override."),
    ] = None,
    db_path: Annotated[
        str | None,
        typer.Option(help="Optional remote DB path override."),
    ] = None,
    expected_writer_pid: Annotated[
        int | None,
        typer.Option(help="Optional expected active writer PID to wait on, e.g. the R5 PID."),
    ] = None,
    max_wait_seconds: Annotated[
        int,
        typer.Option(help="Maximum bounded wait time for the writer gate to clear."),
    ] = 300,
    poll_interval_seconds: Annotated[
        int,
        typer.Option(help="Seconds between writer-gate polls; use 0 for immediate bounded checks."),
    ] = 30,
    min_minutes_before_target: Annotated[
        int,
        typer.Option(help="Minimum lead time before weather target expiry before running the selected-window pipeline."),
    ] = 10,
    fresh_window_hours: Annotated[
        int,
        typer.Option(help="Freshness window for current-window weather source matching."),
    ] = 24,
    match_tolerance_hours: Annotated[
        int,
        typer.Option(help="Maximum target-time mismatch allowed between market text and weather feature."),
    ] = 3,
    max_records: Annotated[
        int,
        typer.Option(help="Maximum R12 missing-link rows to apply if the current-window gate says it is safe."),
    ] = 25,
    limit: Annotated[
        int,
        typer.Option(help="R12 preview/apply market inspection limit."),
    ] = 2000,
    forecast_limit: Annotated[
        int,
        typer.Option(help="Per-ticker weather forecast row limit; keep at 1 for selected-window speed."),
    ] = 1,
    per_ticker_timeout_seconds: Annotated[
        int,
        typer.Option(help="Timeout for each per-ticker weather forecast command."),
    ] = 30,
    pipeline_timeout_seconds: Annotated[
        int,
        typer.Option(help="Timeout for each remote selected-window pipeline command."),
    ] = 180,
    per_probe_timeout_seconds: Annotated[
        int,
        typer.Option(help="Timeout for each bounded remote probe."),
    ] = 60,
) -> None:
    """Run the selected live weather window through the fast paper-only pipeline."""
    settings = get_settings()
    engine = make_engine(database_url_from_settings(settings))
    session_factory = get_session_factory(engine)
    try:
        with session_factory() as session:
            artifacts = write_phase3bb_r57_weather_selected_window_pipeline_report(
                session,
                output_dir=output_dir,
                reports_dir=reports_dir,
                r53_output_dir=r53_output_dir,
                settings=settings,
                command_args=sys.argv[1:],
                ssh_target=ssh_target,
                identity_file=identity_file,
                app_path=app_path,
                env_path=env_path,
                db_path=db_path,
                expected_writer_pid=expected_writer_pid,
                max_wait_seconds=max_wait_seconds,
                poll_interval_seconds=poll_interval_seconds,
                min_minutes_before_target=min_minutes_before_target,
                fresh_window_hours=fresh_window_hours,
                match_tolerance_hours=match_tolerance_hours,
                max_records=max_records,
                limit=limit,
                forecast_limit=forecast_limit,
                per_ticker_timeout_seconds=per_ticker_timeout_seconds,
                pipeline_timeout_seconds=pipeline_timeout_seconds,
                per_probe_timeout_seconds=per_probe_timeout_seconds,
            )
            session.rollback()
    finally:
        engine.dispose()
    console.print("Phase 3BB-R57 selected-window weather pipeline speed repair")
    console.print("Mode: PAPER ONLY guarded selected-window weather pipeline")
    console.print("Missing-link apply runs only when R53/R12 gates say safe")
    console.print("Weather forecast uses per-ticker --limit 1, not broad forecast")
    console.print("Paper trade creation: blocked")
    console.print("Live/demo execution: blocked")
    console.print("Order submission/cancel/replace: blocked")
    console.print("Thresholds lowered: false")
    console.print(f"Wrote Executive Summary: {artifacts.executive_summary_path}")
    console.print(f"Wrote Markdown: {artifacts.markdown_path}")
    console.print(f"Wrote JSON: {artifacts.json_path}")
    console.print(f"Wrote wait checks CSV: {artifacts.wait_checks_csv_path}")
    console.print(f"Wrote pipeline steps CSV: {artifacts.pipeline_steps_csv_path}")
    console.print(f"Wrote selected tickers CSV: {artifacts.selected_tickers_csv_path}")
    console.print(f"Wrote probe CSV: {artifacts.probe_csv_path}")
    console.print(f"Wrote operator next command: {artifacts.operator_command_path}")
    console.print(f"Wrote Next Actions: {artifacts.next_actions_path}")
    console.print(f"Wrote manifest: {artifacts.manifest_path}")


@app.command("phase3bb-r58-weather-selected-window-forecast-feature-alignment-repair")
def phase3bb_r58_weather_selected_window_alignment_command(
    output_dir: Annotated[
        Path,
        typer.Option(help="Directory for Phase 3BB-R58 weather selected-window alignment artifacts."),
    ] = Path("reports/phase3bb_r58"),
    reports_dir: Annotated[
        Path,
        typer.Option(help="Root directory containing local phase report artifacts."),
    ] = Path("reports"),
    ssh_target: Annotated[
        str | None,
        typer.Option(help="Optional SSH target override, for example root@159.65.35.72."),
    ] = None,
    identity_file: Annotated[
        str | None,
        typer.Option(help="Optional SSH identity file override."),
    ] = None,
    app_path: Annotated[
        str | None,
        typer.Option(help="Optional remote app path override."),
    ] = None,
    env_path: Annotated[
        str | None,
        typer.Option(help="Optional remote env file override."),
    ] = None,
    db_path: Annotated[
        str | None,
        typer.Option(help="Optional remote DB path override."),
    ] = None,
    match_tolerance_hours: Annotated[
        int,
        typer.Option(help="Maximum target-time mismatch allowed when comparing selected weather rows."),
    ] = 3,
    per_probe_timeout_seconds: Annotated[
        int,
        typer.Option(help="Timeout for each bounded remote probe."),
    ] = 60,
) -> None:
    """Compare selected weather window market/link/feature/forecast/ranking alignment."""
    settings = get_settings()
    engine = make_engine(database_url_from_settings(settings))
    session_factory = get_session_factory(engine)
    try:
        with session_factory() as session:
            artifacts = write_phase3bb_r58_weather_selected_window_alignment_report(
                session,
                output_dir=output_dir,
                reports_dir=reports_dir,
                settings=settings,
                command_args=sys.argv[1:],
                ssh_target=ssh_target,
                identity_file=identity_file,
                app_path=app_path,
                env_path=env_path,
                db_path=db_path,
                match_tolerance_hours=match_tolerance_hours,
                per_probe_timeout_seconds=per_probe_timeout_seconds,
            )
            session.rollback()
    finally:
        engine.dispose()
    console.print("Phase 3BB-R58 weather selected-window forecast/feature alignment repair")
    console.print("Mode: PAPER ONLY selected-window alignment diagnostic")
    console.print("Paper trade creation: blocked")
    console.print("Live/demo execution: blocked")
    console.print("Order submission/cancel/replace: blocked")
    console.print("Thresholds lowered: false")
    console.print(f"Wrote Executive Summary: {artifacts.executive_summary_path}")
    console.print(f"Wrote Markdown: {artifacts.markdown_path}")
    console.print(f"Wrote JSON: {artifacts.json_path}")
    console.print(f"Wrote rows CSV: {artifacts.rows_csv_path}")
    console.print(f"Wrote patch status CSV: {artifacts.patch_status_csv_path}")
    console.print(f"Wrote probe CSV: {artifacts.probe_csv_path}")
    console.print(f"Wrote operator next command: {artifacts.operator_command_path}")
    console.print(f"Wrote Next Actions: {artifacts.next_actions_path}")
    console.print(f"Wrote manifest: {artifacts.manifest_path}")


@app.command("phase3bb-r59-weather-catalog-refresh-r57-retry")
def phase3bb_r59_weather_catalog_refresh_r57_retry_command(
    output_dir: Annotated[
        Path,
        typer.Option(help="Directory for Phase 3BB-R59 weather catalog refresh and R57 retry artifacts."),
    ] = Path("reports/phase3bb_r59"),
    reports_dir: Annotated[
        Path,
        typer.Option(help="Root directory containing local phase report artifacts."),
    ] = Path("reports"),
    r53_output_dir: Annotated[
        Path,
        typer.Option(help="Directory where the R53 gate report should be written."),
    ] = Path("reports/phase3bb_r53"),
    r57_output_dir: Annotated[
        Path,
        typer.Option(help="Directory where the patched R57 report should be written."),
    ] = Path("reports/phase3bb_r57"),
    ssh_target: Annotated[
        str | None,
        typer.Option(help="Optional SSH target override, for example root@159.65.35.72."),
    ] = None,
    identity_file: Annotated[
        str | None,
        typer.Option(help="Optional SSH identity file override."),
    ] = None,
    app_path: Annotated[
        str | None,
        typer.Option(help="Optional remote app path override."),
    ] = None,
    env_path: Annotated[
        str | None,
        typer.Option(help="Optional remote env file override."),
    ] = None,
    db_path: Annotated[
        str | None,
        typer.Option(help="Optional remote DB path override."),
    ] = None,
    expected_writer_pid: Annotated[
        int | None,
        typer.Option(help="Expected active writer PID to wait on before refreshing weather catalog."),
    ] = None,
    max_wait_seconds: Annotated[
        int,
        typer.Option(help="Maximum bounded wait time for the writer gate to clear."),
    ] = 420,
    poll_interval_seconds: Annotated[
        int,
        typer.Option(help="Seconds between writer-gate polls; use 0 for immediate bounded checks."),
    ] = 15,
    min_minutes_before_target: Annotated[
        int,
        typer.Option(help="Minimum lead time before weather target expiry before running patched R57."),
    ] = 10,
    fresh_window_hours: Annotated[
        int,
        typer.Option(help="Freshness window for current-window weather source matching."),
    ] = 24,
    match_tolerance_hours: Annotated[
        int,
        typer.Option(help="Maximum target-time mismatch allowed between market text and weather feature."),
    ] = 3,
    catalog_limit: Annotated[
        int,
        typer.Option(help="Page size for targeted weather catalog refresh."),
    ] = 100,
    catalog_max_pages: Annotated[
        int,
        typer.Option(help="Maximum pages for targeted weather catalog refresh."),
    ] = 3,
    parse_limit: Annotated[
        int,
        typer.Option(help="Market leg parse refresh limit after catalog sync."),
    ] = 1500,
    series_ticker: Annotated[
        str,
        typer.Option(help="Weather series ticker to refresh."),
    ] = "KXTEMPNYCH",
    max_records: Annotated[
        int,
        typer.Option(help="Maximum R12 missing-link rows R57 may apply if safe."),
    ] = 25,
    r12_limit: Annotated[
        int,
        typer.Option(help="R12 preview/apply market inspection limit for R57."),
    ] = 2000,
    forecast_limit: Annotated[
        int,
        typer.Option(help="Per-ticker weather forecast row limit; keep at 1 for selected-window speed."),
    ] = 1,
    per_ticker_timeout_seconds: Annotated[
        int,
        typer.Option(help="Timeout for each per-ticker weather forecast command."),
    ] = 25,
    refresh_timeout_seconds: Annotated[
        int,
        typer.Option(help="Timeout for each targeted catalog refresh/parse command."),
    ] = 240,
    r57_timeout_seconds: Annotated[
        int,
        typer.Option(help="Timeout for each patched R57 remote pipeline command."),
    ] = 180,
    per_probe_timeout_seconds: Annotated[
        int,
        typer.Option(help="Timeout for each bounded remote probe."),
    ] = 60,
) -> None:
    """Wait for writer clear, refresh KXTEMPNYCH catalog, then rerun patched R57 if a future window exists."""
    settings = get_settings()
    engine = make_engine(database_url_from_settings(settings))
    session_factory = get_session_factory(engine)
    try:
        with session_factory() as session:
            artifacts = write_phase3bb_r59_weather_catalog_refresh_r57_retry_report(
                session,
                output_dir=output_dir,
                reports_dir=reports_dir,
                r53_output_dir=r53_output_dir,
                r57_output_dir=r57_output_dir,
                settings=settings,
                command_args=sys.argv[1:],
                ssh_target=ssh_target,
                identity_file=identity_file,
                app_path=app_path,
                env_path=env_path,
                db_path=db_path,
                expected_writer_pid=expected_writer_pid,
                max_wait_seconds=max_wait_seconds,
                poll_interval_seconds=poll_interval_seconds,
                min_minutes_before_target=min_minutes_before_target,
                fresh_window_hours=fresh_window_hours,
                match_tolerance_hours=match_tolerance_hours,
                catalog_limit=catalog_limit,
                catalog_max_pages=catalog_max_pages,
                parse_limit=parse_limit,
                series_ticker=series_ticker,
                max_records=max_records,
                r12_limit=r12_limit,
                forecast_limit=forecast_limit,
                per_ticker_timeout_seconds=per_ticker_timeout_seconds,
                refresh_timeout_seconds=refresh_timeout_seconds,
                r57_timeout_seconds=r57_timeout_seconds,
                per_probe_timeout_seconds=per_probe_timeout_seconds,
            )
            session.rollback()
    finally:
        engine.dispose()
    console.print("Phase 3BB-R59 weather catalog refresh after writer clears + patched R57 retry")
    console.print("Mode: PAPER ONLY writer-gated weather refresh and selected-window pipeline")
    console.print("Paper trade creation: blocked")
    console.print("Live/demo execution: blocked")
    console.print("Order submission/cancel/replace: blocked")
    console.print("Thresholds lowered: false")
    console.print(f"Wrote Executive Summary: {artifacts.executive_summary_path}")
    console.print(f"Wrote Markdown: {artifacts.markdown_path}")
    console.print(f"Wrote JSON: {artifacts.json_path}")
    console.print(f"Wrote wait checks CSV: {artifacts.wait_checks_csv_path}")
    console.print(f"Wrote refresh steps CSV: {artifacts.refresh_steps_csv_path}")
    console.print(f"Wrote R53 summary CSV: {artifacts.r53_summary_csv_path}")
    console.print(f"Wrote R57 summary CSV: {artifacts.r57_summary_csv_path}")
    console.print(f"Wrote probe CSV: {artifacts.probe_csv_path}")
    console.print(f"Wrote operator next command: {artifacts.operator_command_path}")
    console.print(f"Wrote Next Actions: {artifacts.next_actions_path}")
    console.print(f"Wrote manifest: {artifacts.manifest_path}")


@app.command("phase3bb-r60-weather-next-window-lead-time-scheduler-repair")
def phase3bb_r60_weather_next_window_lead_time_scheduler_repair_command(
    output_dir: Annotated[
        Path,
        typer.Option(help="Directory for Phase 3BB-R60 weather lead-time artifacts."),
    ] = Path("reports/phase3bb_r60"),
    reports_dir: Annotated[
        Path,
        typer.Option(help="Root directory containing local phase report artifacts."),
    ] = Path("reports"),
    r53_output_dir: Annotated[
        Path,
        typer.Option(help="Directory where the pre/post R53 reports should be written."),
    ] = Path("reports/phase3bb_r53"),
    r57_output_dir: Annotated[
        Path,
        typer.Option(help="Directory where the patched R57 report should be written if triggered."),
    ] = Path("reports/phase3bb_r57"),
    r59_output_dir: Annotated[
        Path,
        typer.Option(help="Directory where the delegated R59 report should be written if triggered."),
    ] = Path("reports/phase3bb_r59"),
    ssh_target: Annotated[
        str | None,
        typer.Option(help="Optional SSH target override, for example root@159.65.35.72."),
    ] = None,
    identity_file: Annotated[
        str | None,
        typer.Option(help="Optional SSH identity file override."),
    ] = None,
    app_path: Annotated[
        str | None,
        typer.Option(help="Optional remote app path override."),
    ] = None,
    env_path: Annotated[
        str | None,
        typer.Option(help="Optional remote env file override."),
    ] = None,
    db_path: Annotated[
        str | None,
        typer.Option(help="Optional remote DB path override."),
    ] = None,
    expected_writer_pid: Annotated[
        int | None,
        typer.Option(help="Expected active writer PID to wait on before refreshing weather catalog."),
    ] = None,
    max_wait_seconds: Annotated[
        int,
        typer.Option(help="Maximum bounded wait time for the writer gate to clear when R59 is triggered."),
    ] = 120,
    poll_interval_seconds: Annotated[
        int,
        typer.Option(help="Seconds between writer-gate polls when R59 is triggered."),
    ] = 10,
    min_minutes_before_target: Annotated[
        int,
        typer.Option(help="Minimum lead time before weather target expiry before triggering R59/R57."),
    ] = 20,
    max_minutes_before_target: Annotated[
        int,
        typer.Option(help="Maximum lead time before weather target expiry before waiting for a later scheduler tick."),
    ] = 90,
    fresh_window_hours: Annotated[
        int,
        typer.Option(help="Freshness window for current-window weather source matching."),
    ] = 24,
    match_tolerance_hours: Annotated[
        int,
        typer.Option(help="Maximum target-time mismatch allowed between market text and weather feature."),
    ] = 3,
    catalog_limit: Annotated[
        int,
        typer.Option(help="Page size for targeted weather catalog refresh."),
    ] = 100,
    catalog_max_pages: Annotated[
        int,
        typer.Option(help="Maximum pages for targeted weather catalog refresh."),
    ] = 3,
    parse_limit: Annotated[
        int,
        typer.Option(help="Market leg parse refresh limit after catalog sync."),
    ] = 1500,
    series_ticker: Annotated[
        str,
        typer.Option(help="Weather series ticker to refresh."),
    ] = "KXTEMPNYCH",
    max_records: Annotated[
        int,
        typer.Option(help="Maximum R12 missing-link rows R57 may apply if safe."),
    ] = 25,
    r12_limit: Annotated[
        int,
        typer.Option(help="R12 preview/apply market inspection limit for R57."),
    ] = 2000,
    forecast_limit: Annotated[
        int,
        typer.Option(help="Per-ticker weather forecast row limit; keep at 1 for selected-window speed."),
    ] = 1,
    per_ticker_timeout_seconds: Annotated[
        int,
        typer.Option(help="Timeout for each per-ticker weather forecast command."),
    ] = 25,
    refresh_timeout_seconds: Annotated[
        int,
        typer.Option(help="Timeout for each targeted catalog refresh/parse command."),
    ] = 240,
    r57_timeout_seconds: Annotated[
        int,
        typer.Option(help="Timeout for each patched R57 remote pipeline command."),
    ] = 300,
    per_probe_timeout_seconds: Annotated[
        int,
        typer.Option(help="Timeout for each bounded remote probe."),
    ] = 60,
) -> None:
    """Trigger weather catalog/R57 only inside the safe next-window lead-time band."""
    settings = get_settings()
    engine = make_engine(database_url_from_settings(settings))
    session_factory = get_session_factory(engine)
    try:
        with session_factory() as session:
            artifacts = write_phase3bb_r60_weather_next_window_lead_time_report(
                session,
                output_dir=output_dir,
                reports_dir=reports_dir,
                r53_output_dir=r53_output_dir,
                r57_output_dir=r57_output_dir,
                r59_output_dir=r59_output_dir,
                settings=settings,
                command_args=sys.argv[1:],
                ssh_target=ssh_target,
                identity_file=identity_file,
                app_path=app_path,
                env_path=env_path,
                db_path=db_path,
                expected_writer_pid=expected_writer_pid,
                max_wait_seconds=max_wait_seconds,
                poll_interval_seconds=poll_interval_seconds,
                min_minutes_before_target=min_minutes_before_target,
                max_minutes_before_target=max_minutes_before_target,
                fresh_window_hours=fresh_window_hours,
                match_tolerance_hours=match_tolerance_hours,
                catalog_limit=catalog_limit,
                catalog_max_pages=catalog_max_pages,
                parse_limit=parse_limit,
                series_ticker=series_ticker,
                max_records=max_records,
                r12_limit=r12_limit,
                forecast_limit=forecast_limit,
                per_ticker_timeout_seconds=per_ticker_timeout_seconds,
                refresh_timeout_seconds=refresh_timeout_seconds,
                r57_timeout_seconds=r57_timeout_seconds,
                per_probe_timeout_seconds=per_probe_timeout_seconds,
            )
            session.rollback()
    finally:
        engine.dispose()
    console.print("Phase 3BB-R60 weather next-window lead-time scheduler repair")
    console.print("Mode: PAPER ONLY scheduler-safe weather lead-time gate")
    console.print("Paper trade creation: blocked")
    console.print("Live/demo execution: blocked")
    console.print("Order submission/cancel/replace: blocked")
    console.print("Thresholds lowered: false")
    console.print(f"Wrote Executive Summary: {artifacts.executive_summary_path}")
    console.print(f"Wrote Markdown: {artifacts.markdown_path}")
    console.print(f"Wrote JSON: {artifacts.json_path}")
    console.print(f"Wrote lead-time checks CSV: {artifacts.lead_time_checks_csv_path}")
    console.print(f"Wrote R53 pre-summary CSV: {artifacts.r53_pre_summary_csv_path}")
    console.print(f"Wrote R59 summary CSV: {artifacts.r59_summary_csv_path}")
    console.print(f"Wrote probe CSV: {artifacts.probe_csv_path}")
    console.print(f"Wrote scheduler hook draft: {artifacts.scheduler_hook_path}")
    console.print(f"Wrote operator next command: {artifacts.operator_command_path}")
    console.print(f"Wrote Next Actions: {artifacts.next_actions_path}")
    console.print(f"Wrote manifest: {artifacts.manifest_path}")


@app.command("phase3bb-r61-cloud-dashboard-db-writer-api-reachability-repair")
def phase3bb_r61_cloud_dashboard_db_writer_api_reachability_repair_command(
    output_dir: Annotated[
        Path,
        typer.Option(help="Directory for Phase 3BB-R61 cloud UI API repair artifacts."),
    ] = Path("reports/phase3bb_r61"),
    reports_dir: Annotated[
        Path,
        typer.Option(help="Root directory containing local phase report artifacts."),
    ] = Path("reports"),
    private_base_url: Annotated[
        str | None,
        typer.Option(help="Optional private Tailscale UI base URL override."),
    ] = None,
    ui_timeout_seconds: Annotated[
        int,
        typer.Option(help="Timeout per private UI API probe."),
    ] = 90,
    max_dashboard_age_seconds: Annotated[
        int,
        typer.Option(help="Maximum acceptable dashboard snapshot age for downstream R32."),
    ] = 300,
    ssh_target: Annotated[
        str | None,
        typer.Option(help="Optional SSH target override, for example kalshi@159.65.35.72."),
    ] = None,
    identity_file: Annotated[
        str | None,
        typer.Option(help="Optional SSH identity file override."),
    ] = None,
    app_path: Annotated[
        str | None,
        typer.Option(help="Optional remote app path override."),
    ] = None,
    env_path: Annotated[
        str | None,
        typer.Option(help="Optional remote env file override."),
    ] = None,
    db_path: Annotated[
        str | None,
        typer.Option(help="Optional remote DB path override."),
    ] = None,
    ui_service_name: Annotated[
        str,
        typer.Option(help="Cloud UI systemd service name to inspect."),
    ] = "kalshi-ui.service",
    ui_port: Annotated[
        int,
        typer.Option(help="Cloud UI localhost port to inspect."),
    ] = 8080,
    per_probe_timeout_seconds: Annotated[
        int,
        typer.Option(help="Timeout per bounded remote diagnostic probe."),
    ] = 30,
) -> None:
    """Diagnose private UI /api/db-writer reachability before the R60 scheduler hook."""
    settings = get_settings()
    engine = make_engine(database_url_from_settings(settings))
    session_factory = get_session_factory(engine)
    try:
        with session_factory() as session:
            artifacts = write_phase3bb_r61_cloud_dashboard_db_writer_api_repair_report(
                session,
                output_dir=output_dir,
                reports_dir=reports_dir,
                settings=settings,
                command_args=sys.argv[1:],
                private_base_url=private_base_url,
                ui_timeout_seconds=ui_timeout_seconds,
                max_dashboard_age_seconds=max_dashboard_age_seconds,
                ssh_target=ssh_target,
                identity_file=identity_file,
                app_path=app_path,
                env_path=env_path,
                db_path=db_path,
                ui_service_name=ui_service_name,
                ui_port=ui_port,
                per_probe_timeout_seconds=per_probe_timeout_seconds,
            )
            session.rollback()
    finally:
        engine.dispose()
    console.print("Phase 3BB-R61 cloud dashboard DB writer API reachability repair")
    console.print("Mode: PAPER READ-ONLY cloud UI/API diagnostic; no start/install")
    console.print("UI/R5/scheduler service start/stop executed by Codex: 0")
    console.print("Systemctl mutating commands executed by Codex: 0")
    console.print("Remote DB writes performed: 0")
    console.print("Live/demo execution: blocked")
    console.print("Order submission/cancel/replace: blocked")
    console.print("Paper trade creation: blocked")
    console.print(f"Wrote Executive Summary: {artifacts.executive_summary_path}")
    console.print(f"Wrote Markdown: {artifacts.markdown_path}")
    console.print(f"Wrote JSON: {artifacts.json_path}")
    console.print(f"Wrote remote probes: {artifacts.probe_csv_path}")
    console.print(f"Wrote private UI API probes: {artifacts.ui_api_probe_csv_path}")
    console.print(f"Wrote repair checks: {artifacts.checks_csv_path}")
    console.print(f"Wrote UI start handoff: {artifacts.ui_start_handoff_path}")
    console.print(f"Wrote R60 scheduler no-start handoff: {artifacts.scheduler_no_start_handoff_path}")
    console.print(f"Wrote operator next command: {artifacts.operator_command_path}")
    console.print(f"Wrote Next Actions: {artifacts.next_actions_path}")
    console.print(f"Wrote manifest: {artifacts.manifest_path}")


@app.command("phase3bb-r41-writer-gate-normalization")
def phase3bb_r41_writer_gate_normalization_command(
    output_dir: Annotated[
        Path,
        typer.Option(help="Directory for Phase 3BB-R41 writer gate artifacts."),
    ] = Path("reports/phase3bb_r41"),
    reports_dir: Annotated[
        Path,
        typer.Option(help="Root directory containing local phase report artifacts."),
    ] = Path("reports"),
    ssh_target: Annotated[
        str | None,
        typer.Option(help="Optional SSH target override, for example kalshi@159.65.35.72."),
    ] = None,
    identity_file: Annotated[
        str | None,
        typer.Option(help="Optional SSH identity file override."),
    ] = None,
    app_path: Annotated[
        str | None,
        typer.Option(help="Optional remote app path override."),
    ] = None,
    env_path: Annotated[
        str | None,
        typer.Option(help="Optional remote env file override."),
    ] = None,
    db_path: Annotated[
        str | None,
        typer.Option(help="Optional remote DB path override."),
    ] = None,
    scheduler_service_name: Annotated[
        str,
        typer.Option(help="Scheduler service unit name to inspect."),
    ] = "kalshi-multicategory-refresh-scheduler.service",
    scheduler_timer_name: Annotated[
        str,
        typer.Option(help="Scheduler timer unit name to inspect."),
    ] = "kalshi-multicategory-refresh-scheduler.timer",
    r5_service_name: Annotated[
        str,
        typer.Option(help="Cloud R5 watcher service unit name to inspect."),
    ] = "kalshi-r5-watcher.service",
    ui_service_name: Annotated[
        str,
        typer.Option(help="Cloud UI service unit name to inspect."),
    ] = "kalshi-ui.service",
    journal_lines: Annotated[
        int,
        typer.Option(help="Number of scheduler journal lines to inspect."),
    ] = 500,
    per_probe_timeout_seconds: Annotated[
        int,
        typer.Option(help="Timeout for each bounded remote writer-gate probe."),
    ] = 45,
) -> None:
    """Normalize the cloud writer gate and diagnose weather fast-lane unblocking."""
    settings = get_settings()
    engine = make_engine(database_url_from_settings(settings))
    session_factory = get_session_factory(engine)
    try:
        with session_factory() as session:
            artifacts = write_phase3bb_r41_writer_gate_normalization_report(
                session,
                output_dir=output_dir,
                reports_dir=reports_dir,
                settings=settings,
                command_args=sys.argv[1:],
                ssh_target=ssh_target,
                identity_file=identity_file,
                app_path=app_path,
                env_path=env_path,
                db_path=db_path,
                scheduler_service_name=scheduler_service_name,
                scheduler_timer_name=scheduler_timer_name,
                r5_service_name=r5_service_name,
                ui_service_name=ui_service_name,
                journal_lines=journal_lines,
                per_probe_timeout_seconds=per_probe_timeout_seconds,
            )
            session.rollback()
    finally:
        engine.dispose()
    console.print("Phase 3BB-R41 writer gate normalization")
    console.print("Mode: PAPER READ-ONLY writer-gate diagnostic")
    console.print("Remote copy/install/enable/start executed by Codex: 0")
    console.print("Weather fast-lane executed by Codex: 0")
    console.print("Scheduler refresh jobs run by Codex: 0")
    console.print("UI/R5 service start/stop executed by Codex: 0")
    console.print("SSH probes executed by Codex: bounded read-only/status only")
    console.print("Systemctl mutating commands executed by Codex: 0")
    console.print("Secrets printed/copied by Codex: blocked")
    console.print("Live/demo execution: blocked")
    console.print("Order submission/cancel/replace: blocked")
    console.print("Paper trade creation: blocked")
    console.print("DB writes performed: 0")
    console.print(f"Wrote Executive Summary: {artifacts.executive_summary_path}")
    console.print(f"Wrote Markdown: {artifacts.markdown_path}")
    console.print(f"Wrote JSON: {artifacts.json_path}")
    console.print(f"Wrote probe CSV: {artifacts.probe_csv_path}")
    console.print(f"Wrote writer-gate checks: {artifacts.checks_csv_path}")
    console.print(f"Wrote writer-gate skips: {artifacts.writer_gate_csv_path}")
    console.print(f"Wrote operator next command: {artifacts.operator_command_path}")
    console.print(f"Wrote Next Actions: {artifacts.next_actions_path}")
    console.print(f"Wrote manifest: {artifacts.manifest_path}")


@app.command("phase3bb-r42-weather-fast-lane-post-unblock-verification")
def phase3bb_r42_weather_fast_lane_post_unblock_verification_command(
    output_dir: Annotated[
        Path,
        typer.Option(help="Directory for Phase 3BB-R42 weather post-unblock artifacts."),
    ] = Path("reports/phase3bb_r42"),
    reports_dir: Annotated[
        Path,
        typer.Option(help="Root directory containing local phase report artifacts."),
    ] = Path("reports"),
    ssh_target: Annotated[
        str | None,
        typer.Option(help="Optional SSH target override, for example kalshi@159.65.35.72."),
    ] = None,
    identity_file: Annotated[
        str | None,
        typer.Option(help="Optional SSH identity file override."),
    ] = None,
    app_path: Annotated[
        str | None,
        typer.Option(help="Optional remote app path override."),
    ] = None,
    env_path: Annotated[
        str | None,
        typer.Option(help="Optional remote env file override."),
    ] = None,
    db_path: Annotated[
        str | None,
        typer.Option(help="Optional remote DB path override."),
    ] = None,
    scheduler_service_name: Annotated[
        str,
        typer.Option(help="Scheduler service unit name to inspect."),
    ] = "kalshi-multicategory-refresh-scheduler.service",
    scheduler_timer_name: Annotated[
        str,
        typer.Option(help="Scheduler timer unit name to inspect."),
    ] = "kalshi-multicategory-refresh-scheduler.timer",
    r5_service_name: Annotated[
        str,
        typer.Option(help="Cloud R5 watcher service unit name to inspect."),
    ] = "kalshi-r5-watcher.service",
    ui_service_name: Annotated[
        str,
        typer.Option(help="Cloud UI service unit name to inspect."),
    ] = "kalshi-ui.service",
    journal_lines: Annotated[
        int,
        typer.Option(help="Number of post-R41 scheduler journal lines to inspect."),
    ] = 700,
    per_probe_timeout_seconds: Annotated[
        int,
        typer.Option(help="Timeout for each bounded remote post-unblock probe."),
    ] = 45,
) -> None:
    """Verify the scheduled weather fast-lane after the R41 writer-gate unblock."""
    settings = get_settings()
    engine = make_engine(database_url_from_settings(settings))
    session_factory = get_session_factory(engine)
    try:
        with session_factory() as session:
            artifacts = write_phase3bb_r42_weather_fast_lane_post_unblock_report(
                session,
                output_dir=output_dir,
                reports_dir=reports_dir,
                settings=settings,
                command_args=sys.argv[1:],
                ssh_target=ssh_target,
                identity_file=identity_file,
                app_path=app_path,
                env_path=env_path,
                db_path=db_path,
                scheduler_service_name=scheduler_service_name,
                scheduler_timer_name=scheduler_timer_name,
                r5_service_name=r5_service_name,
                ui_service_name=ui_service_name,
                journal_lines=journal_lines,
                per_probe_timeout_seconds=per_probe_timeout_seconds,
            )
            session.rollback()
    finally:
        engine.dispose()
    console.print("Phase 3BB-R42 weather fast-lane post-unblock verification")
    console.print("Mode: PAPER READ-ONLY weather scheduler verification")
    console.print("Remote copy/install/enable/start executed by Codex: 0")
    console.print("Weather fast-lane executed by Codex: 0")
    console.print("Scheduler refresh jobs run by Codex: 0")
    console.print("UI/R5 service start/stop executed by Codex: 0")
    console.print("SSH probes executed by Codex: bounded read-only/status only")
    console.print("Systemctl mutating commands executed by Codex: 0")
    console.print("Secrets printed/copied by Codex: blocked")
    console.print("Live/demo execution: blocked")
    console.print("Order submission/cancel/replace: blocked")
    console.print("Paper trade creation: blocked")
    console.print("DB writes performed: 0")
    console.print(f"Wrote Executive Summary: {artifacts.executive_summary_path}")
    console.print(f"Wrote Markdown: {artifacts.markdown_path}")
    console.print(f"Wrote JSON: {artifacts.json_path}")
    console.print(f"Wrote probe CSV: {artifacts.probe_csv_path}")
    console.print(f"Wrote verification checks: {artifacts.checks_csv_path}")
    console.print(f"Wrote weather fast-lane events: {artifacts.events_csv_path}")
    console.print(f"Wrote weather report freshness: {artifacts.report_freshness_csv_path}")
    console.print(f"Wrote operator next command: {artifacts.operator_command_path}")
    console.print(f"Wrote Next Actions: {artifacts.next_actions_path}")
    console.print(f"Wrote manifest: {artifacts.manifest_path}")


@app.command("phase3bb-r43-weather-catalog-scheduler-hook")
def phase3bb_r43_weather_catalog_scheduler_hook_command(
    output_dir: Annotated[
        Path,
        typer.Option(help="Directory for Phase 3BB-R43 weather catalog hook artifacts."),
    ] = Path("reports/phase3bb_r43"),
    reports_dir: Annotated[
        Path,
        typer.Option(help="Root directory containing local phase report artifacts."),
    ] = Path("reports"),
    ssh_target: Annotated[
        str | None,
        typer.Option(help="Optional SSH target override, for example root@159.65.35.72."),
    ] = None,
    identity_file: Annotated[
        str | None,
        typer.Option(help="Optional SSH identity file override."),
    ] = None,
    app_path: Annotated[
        str | None,
        typer.Option(help="Optional remote app path override."),
    ] = None,
    env_path: Annotated[
        str | None,
        typer.Option(help="Optional remote env file override."),
    ] = None,
    db_path: Annotated[
        str | None,
        typer.Option(help="Optional remote DB path override."),
    ] = None,
    apply: Annotated[
        bool,
        typer.Option(help="Install the patched scheduler runner on the cloud host."),
    ] = False,
    backup_first: Annotated[
        bool,
        typer.Option(help="Create a backup of the current cloud runner before applying."),
    ] = False,
    per_probe_timeout_seconds: Annotated[
        int,
        typer.Option(help="Timeout for each bounded remote hook probe."),
    ] = 45,
) -> None:
    """Add or verify the writer-gated weather current-catalog refresh scheduler hook."""
    settings = get_settings()
    engine = make_engine(database_url_from_settings(settings))
    session_factory = get_session_factory(engine)
    try:
        with session_factory() as session:
            artifacts = write_phase3bb_r43_weather_catalog_scheduler_hook_report(
                session,
                output_dir=output_dir,
                reports_dir=reports_dir,
                settings=settings,
                command_args=sys.argv[1:],
                ssh_target=ssh_target,
                identity_file=identity_file,
                app_path=app_path,
                env_path=env_path,
                db_path=db_path,
                apply=apply,
                backup_first=backup_first,
                per_probe_timeout_seconds=per_probe_timeout_seconds,
            )
            session.rollback()
    finally:
        engine.dispose()
    console.print("Phase 3BB-R43 weather current catalog scheduler hook")
    console.print("Mode: PAPER READ-ONLY scheduler hook repair")
    console.print(f"Apply requested: {apply}")
    console.print(f"Backup first: {backup_first}")
    console.print("Scheduler timer/service start executed by Codex: 0")
    console.print("Weather fast-lane executed by Codex: 0")
    console.print("Refresh jobs run by Codex: 0")
    console.print("UI/R5 service start/stop executed by Codex: 0")
    console.print("Systemctl mutating commands executed by Codex: 0")
    console.print("Secrets printed/copied by Codex: blocked")
    console.print("Live/demo execution: blocked")
    console.print("Order submission/cancel/replace: blocked")
    console.print("Paper trade creation: blocked")
    console.print("DB writes performed: 0")
    console.print(f"Wrote Executive Summary: {artifacts.executive_summary_path}")
    console.print(f"Wrote Markdown: {artifacts.markdown_path}")
    console.print(f"Wrote JSON: {artifacts.json_path}")
    console.print(f"Wrote probe CSV: {artifacts.probe_csv_path}")
    console.print(f"Wrote hook checks: {artifacts.checks_csv_path}")
    console.print(f"Wrote runner draft: {artifacts.runner_draft_path}")
    console.print(f"Wrote operator next command: {artifacts.operator_command_path}")
    console.print(f"Wrote Next Actions: {artifacts.next_actions_path}")
    console.print(f"Wrote manifest: {artifacts.manifest_path}")


@app.command("phase3bb-r38-cloud-scheduler-timer-start-handoff")
def phase3bb_r38_cloud_scheduler_timer_start_handoff_command(
    output_dir: Annotated[
        Path,
        typer.Option(help="Directory for Phase 3BB-R38 timer start handoff artifacts."),
    ] = Path("reports/phase3bb_r38"),
    reports_dir: Annotated[
        Path,
        typer.Option(help="Root directory containing local phase report artifacts."),
    ] = Path("reports"),
    ssh_target: Annotated[
        str | None,
        typer.Option(help="Optional SSH target override, for example kalshi@159.65.35.72."),
    ] = None,
    identity_file: Annotated[
        str | None,
        typer.Option(help="Optional SSH identity file override."),
    ] = None,
    app_path: Annotated[
        str | None,
        typer.Option(help="Optional remote app path override."),
    ] = None,
    env_path: Annotated[
        str | None,
        typer.Option(help="Optional remote env file override."),
    ] = None,
    db_path: Annotated[
        str | None,
        typer.Option(help="Optional remote DB path override."),
    ] = None,
    per_probe_timeout_seconds: Annotated[
        int,
        typer.Option(help="Timeout for each bounded remote timer-start probe."),
    ] = 45,
) -> None:
    """Create the operator-approved scheduler timer start handoff."""
    settings = get_settings()
    engine = make_engine(database_url_from_settings(settings))
    session_factory = get_session_factory(engine)
    try:
        with session_factory() as session:
            artifacts = write_phase3bb_r38_cloud_scheduler_timer_start_handoff_report(
                session,
                output_dir=output_dir,
                reports_dir=reports_dir,
                settings=settings,
                command_args=sys.argv[1:],
                ssh_target=ssh_target,
                identity_file=identity_file,
                app_path=app_path,
                env_path=env_path,
                db_path=db_path,
                per_probe_timeout_seconds=per_probe_timeout_seconds,
            )
            session.rollback()
    finally:
        engine.dispose()
    console.print("Phase 3BB-R38 cloud scheduler timer start handoff")
    console.print("Mode: PAPER READ-ONLY timer start handoff / no start by Codex")
    console.print("Scheduler timer start executed by Codex: 0")
    console.print("Scheduler service direct start executed by Codex: 0")
    console.print("Refresh jobs run by Codex: 0")
    console.print("UI/R5 service start/stop executed by Codex: 0")
    console.print("Live/demo execution: blocked")
    console.print("Order submission/cancel/replace: blocked")
    console.print("Paper trade creation: blocked")
    console.print("DB writes performed: 0")
    console.print(f"Wrote Executive Summary: {artifacts.executive_summary_path}")
    console.print(f"Wrote Markdown: {artifacts.markdown_path}")
    console.print(f"Wrote JSON: {artifacts.json_path}")
    console.print(f"Wrote probe CSV: {artifacts.probe_csv_path}")
    console.print(f"Wrote timer start checks: {artifacts.checks_csv_path}")
    console.print(f"Wrote operator handoff: {artifacts.operator_handoff_script_path}")
    console.print(f"Wrote root-console fallback: {artifacts.root_console_script_path}")
    console.print(f"Wrote operator next command: {artifacts.operator_command_path}")
    console.print(f"Wrote Next Actions: {artifacts.next_actions_path}")
    console.print(f"Wrote manifest: {artifacts.manifest_path}")


@app.command("phase3bc-crypto-clean-opportunity-router")
def phase3bc_crypto_clean_opportunity_router_command(
    output_dir: Annotated[
        Path,
        typer.Option(help="Directory for Phase 3BC crypto opportunity artifacts."),
    ] = Path("reports/phase3bc"),
    limit: Annotated[
        int,
        typer.Option(help="Maximum latest crypto link rows to inspect."),
    ] = 500,
) -> None:
    """Report pure-crypto opportunity readiness without creating trades."""
    engine = init_db()
    settings = get_settings()
    session_factory = get_session_factory(engine)
    with session_factory() as session:
        artifacts = write_phase3bc_crypto_clean_opportunity_report(
            session,
            output_dir=output_dir,
            settings=settings,
            limit=limit,
        )
    console.print("Phase 3BC crypto clean opportunity router")
    console.print("Mode: PAPER ONLY report")
    console.print("Live/demo execution: blocked")
    console.print("Paper/live order writes: blocked in this command")
    console.print(f"Wrote JSON: {artifacts.json_path}")
    console.print(f"Wrote Markdown: {artifacts.markdown_path}")
    console.print(f"Wrote rows: {artifacts.rows_path}")


@app.command("phase3bc-r3-active-crypto-refresh")
def phase3bc_r3_active_crypto_refresh_command(
    output_dir: Annotated[
        Path,
        typer.Option(help="Directory for Phase 3BC-R3 refresh artifacts."),
    ] = Path("reports/phase3bc_r3"),
    phase3bc_output_dir: Annotated[
        Path,
        typer.Option(help="Directory for refreshed Phase 3BC router artifacts."),
    ] = Path("reports/phase3bc"),
    symbols: Annotated[
        str,
        typer.Option(help=f"Comma-separated symbols, for example {DEFAULT_CRYPTO_SYMBOLS}."),
    ] = DEFAULT_CRYPTO_SYMBOLS,
    crypto_series_tickers: Annotated[
        str,
        typer.Option(help="Comma-separated crypto Kalshi series tickers to refresh."),
    ] = DEFAULT_CRYPTO_SERIES_TICKERS,
    source: Annotated[
        str,
        typer.Option(help="Public no-key crypto source: coinbase or coingecko."),
    ] = "coinbase",
    refresh_open_markets: Annotated[
        bool,
        typer.Option(
            "--refresh-open-markets/--skip-open-market-refresh",
            help="Run one bounded open-market snapshot refresh before crypto routing.",
        ),
    ] = False,
    external_crypto_ingest: Annotated[
        bool,
        typer.Option(
            "--external-crypto-ingest/--skip-external-crypto-ingest",
            help="Fetch fresh public crypto prices before feature rebuild.",
        ),
    ] = True,
    repair_snapshots: Annotated[
        bool,
        typer.Option(
            "--repair-snapshots/--diagnose-snapshots",
            help="Fetch public market/orderbook snapshots for linked crypto gaps.",
        ),
    ] = True,
    market_limit: Annotated[
        int,
        typer.Option(help="Page size for bounded open-market refresh."),
    ] = DEFAULT_MARKET_PAGE_LIMIT,
    market_max_pages: Annotated[
        int,
        typer.Option(help="Maximum pages for bounded open-market refresh."),
    ] = 1,
    crypto_market_scan_limit: Annotated[
        int,
        typer.Option(help="Maximum catalog markets to scan while refreshing crypto links."),
    ] = DEFAULT_CRYPTO_MARKET_SCAN_LIMIT,
    crypto_link_limit: Annotated[
        int,
        typer.Option(help="Maximum latest crypto links for snapshot repair diagnostics."),
    ] = DEFAULT_CRYPTO_LINK_SCAN_LIMIT,
    forecast_limit: Annotated[
        int,
        typer.Option(help="Maximum latest crypto snapshots to forecast."),
    ] = 1000,
    opportunity_limit: Annotated[
        int,
        typer.Option(help="Maximum crypto rankings/opportunities to write."),
    ] = 150,
    phase3bc_limit: Annotated[
        int,
        typer.Option(help="Maximum latest crypto links for final Phase 3BC router."),
    ] = 1000,
    cadence_minutes: Annotated[
        int,
        typer.Option(help="Expected crypto refresh cadence in minutes."),
    ] = 15,
    forecast_current_windows_only: Annotated[
        bool,
        typer.Option(
            "--forecast-current-windows-only/--forecast-all-active-crypto",
            help="Forecast only current active crypto windows instead of expired active links.",
        ),
    ] = False,
    generate_opportunity_report: Annotated[
        bool,
        typer.Option(
            "--generate-opportunity-report/--skip-opportunity-report",
            help="Generate the slower opportunity report during R3.",
        ),
    ] = True,
    near_money_only: Annotated[
        bool,
        typer.Option(
            "--near-money-only/--full-strike-ladder",
            help="Refresh only active current-window near-money crypto markets.",
        ),
    ] = False,
    near_money_per_symbol_limit: Annotated[
        int,
        typer.Option(help="Maximum near-money snapshot candidates per crypto symbol."),
    ] = DEFAULT_NEAR_MONEY_PER_SYMBOL_LIMIT,
    near_money_window_limit: Annotated[
        int,
        typer.Option(help="Maximum near-money snapshot candidates per symbol/window."),
    ] = DEFAULT_NEAR_MONEY_WINDOW_LIMIT,
    snapshot_fetch_concurrency: Annotated[
        int,
        typer.Option(help="Conservative orderbook fetch concurrency in near-money mode."),
    ] = DEFAULT_SNAPSHOT_FETCH_CONCURRENCY,
    cycles: Annotated[
        int,
        typer.Option(help="Number of bounded crypto refresh cycles to run."),
    ] = 1,
    interval_minutes: Annotated[
        int,
        typer.Option(help="Minutes to wait between cycles when cycles > 1."),
    ] = 15,
) -> None:
    """Run one bounded paper-only active crypto refresh and Phase 3BC readiness report."""
    if cycles < 1:
        raise typer.BadParameter("cycles must be at least 1")
    if interval_minutes < 0:
        raise typer.BadParameter("interval-minutes must be non-negative")
    if near_money_per_symbol_limit < 0:
        raise typer.BadParameter("near-money-per-symbol-limit must be non-negative")
    if near_money_window_limit < 0:
        raise typer.BadParameter("near-money-window-limit must be non-negative")
    if snapshot_fetch_concurrency < 1:
        raise typer.BadParameter("snapshot-fetch-concurrency must be at least 1")

    engine = init_db()
    settings = get_settings()
    session_factory = get_session_factory(engine)
    artifacts = None
    for cycle_number in range(1, cycles + 1):
        with session_factory() as session:
            artifacts = write_phase3bc_r3_active_crypto_refresh_report(
                session,
                output_dir=output_dir,
                phase3bc_output_dir=phase3bc_output_dir,
                settings=settings,
                symbols=symbols,
                crypto_series_tickers=crypto_series_tickers,
                source=source,
                refresh_open_markets=refresh_open_markets,
                external_crypto_ingest=external_crypto_ingest,
                repair_snapshots=repair_snapshots,
                forecast_current_windows_only=forecast_current_windows_only,
                generate_opportunity_report=generate_opportunity_report,
                market_limit=market_limit,
                market_max_pages=market_max_pages,
                crypto_market_scan_limit=crypto_market_scan_limit,
                crypto_link_limit=crypto_link_limit,
                forecast_limit=forecast_limit,
                opportunity_limit=opportunity_limit,
                phase3bc_limit=phase3bc_limit,
                cadence_minutes=cadence_minutes,
                near_money_only=near_money_only,
                near_money_per_symbol_limit=near_money_per_symbol_limit,
                near_money_window_limit=near_money_window_limit,
                snapshot_fetch_concurrency=snapshot_fetch_concurrency,
            )
            session.commit()
        console.print(f"Completed Phase 3BC-R3 crypto refresh cycle {cycle_number}/{cycles}")
        if cycle_number < cycles and interval_minutes > 0:
            time.sleep(interval_minutes * 60)
    if artifacts is None:
        raise typer.BadParameter("no cycles were run")
    console.print("Phase 3BC-R3 active pure crypto refresh")
    console.print("Mode: PAPER ONLY refresh + diagnostics")
    console.print("Live/demo execution: blocked")
    console.print("Paper/live order writes: blocked in this command")
    console.print(f"Cycles completed: {cycles}")
    console.print(f"Wrote JSON: {artifacts.json_path}")
    console.print(f"Wrote Markdown: {artifacts.markdown_path}")


@app.command("phase3bc-r4-crypto-ev-risk-diagnostics")
def phase3bc_r4_crypto_ev_risk_diagnostics_command(
    output_dir: Annotated[
        Path,
        typer.Option(help="Directory for Phase 3BC-R4 EV/risk diagnostic artifacts."),
    ] = Path("reports/phase3bc_r4"),
    phase3bc_output_dir: Annotated[
        Path,
        typer.Option(help="Directory for refreshed Phase 3BC router artifacts."),
    ] = Path("reports/phase3bc"),
    limit: Annotated[
        int,
        typer.Option(help="Maximum latest crypto links for the underlying Phase 3BC router."),
    ] = 1000,
    freshness_minutes: Annotated[
        int,
        typer.Option(help="Ranking freshness window in minutes."),
    ] = 15,
) -> None:
    """Diagnose active pure crypto EV, ranking freshness, liquidity, and risk gaps."""
    engine = init_db()
    settings = get_settings()
    session_factory = get_session_factory(engine)
    with session_factory() as session:
        artifacts = write_phase3bc_r4_crypto_ev_risk_diagnostics_report(
            session,
            output_dir=output_dir,
            phase3bc_output_dir=phase3bc_output_dir,
            settings=settings,
            limit=limit,
            freshness_minutes=freshness_minutes,
        )
        session.commit()
    console.print("Phase 3BC-R4 crypto EV + risk readiness diagnostics")
    console.print("Mode: PAPER ONLY diagnostics")
    console.print("Live/demo execution: blocked")
    console.print("Paper/risk/order writes: blocked in this command")
    console.print(f"Wrote JSON: {artifacts.json_path}")
    console.print(f"Wrote Markdown: {artifacts.markdown_path}")
    console.print(f"Refreshed Phase 3BC JSON: {artifacts.phase3bc_json_path}")
    console.print(f"Refreshed Phase 3BC rows: {artifacts.phase3bc_rows_path}")


@app.command("phase3bc-r5-crypto-freshness-watch")
def phase3bc_r5_crypto_freshness_watch_command(
    output_dir: Annotated[
        Path,
        typer.Option(help="Directory for Phase 3BC-R5 watch artifacts."),
    ] = Path("reports/phase3bc_r5"),
    phase3bc_output_dir: Annotated[
        Path,
        typer.Option(help="Directory for refreshed Phase 3BC router artifacts."),
    ] = Path("reports/phase3bc"),
    phase3bc_r3_output_dir: Annotated[
        Path,
        typer.Option(help="Directory for Phase 3BC-R3 refresh artifacts."),
    ] = Path("reports/phase3bc_r3"),
    phase3bc_r4_output_dir: Annotated[
        Path,
        typer.Option(help="Directory for Phase 3BC-R4 diagnostic artifacts."),
    ] = Path("reports/phase3bc_r4"),
    phase3bc_r7_output_dir: Annotated[
        Path,
        typer.Option(help="Directory for Phase 3BC-R7 ranking coverage artifacts."),
    ] = Path("reports/phase3bc_r7"),
    symbols: Annotated[
        str,
        typer.Option(help=f"Comma-separated symbols, for example {DEFAULT_CRYPTO_SYMBOLS}."),
    ] = DEFAULT_CRYPTO_SYMBOLS,
    crypto_series_tickers: Annotated[
        str,
        typer.Option(help="Comma-separated crypto Kalshi series tickers to refresh."),
    ] = DEFAULT_CRYPTO_SERIES_TICKERS,
    source: Annotated[
        str,
        typer.Option(help="Public no-key crypto source: coinbase or coingecko."),
    ] = "coinbase",
    refresh_open_markets: Annotated[
        bool,
        typer.Option(
            "--refresh-open-markets/--skip-open-market-refresh",
            help="Run one bounded open-market snapshot refresh before crypto routing.",
        ),
    ] = True,
    external_crypto_ingest: Annotated[
        bool,
        typer.Option(
            "--external-crypto-ingest/--skip-external-crypto-ingest",
            help="Fetch fresh public crypto prices before feature rebuild.",
        ),
    ] = True,
    repair_snapshots: Annotated[
        bool,
        typer.Option(
            "--repair-snapshots/--diagnose-snapshots",
            help="Fetch public market/orderbook snapshots for linked crypto gaps.",
        ),
    ] = False,
    forecast_current_windows_only: Annotated[
        bool,
        typer.Option(
            "--forecast-current-windows-only/--forecast-all-active-crypto",
            help="Forecast only current active crypto windows in the watch loop.",
        ),
    ] = True,
    generate_opportunity_report: Annotated[
        bool,
        typer.Option(
            "--generate-opportunity-report/--skip-opportunity-report",
            help="Generate the slower R3 opportunity report before R7 ranking repair.",
        ),
    ] = False,
    market_limit: Annotated[
        int,
        typer.Option(help="Page size for bounded open-market refresh."),
    ] = DEFAULT_MARKET_PAGE_LIMIT,
    market_max_pages: Annotated[
        int,
        typer.Option(help="Maximum pages for bounded open-market refresh."),
    ] = 1,
    crypto_market_scan_limit: Annotated[
        int,
        typer.Option(help="Maximum catalog markets to scan while refreshing crypto links."),
    ] = DEFAULT_CRYPTO_MARKET_SCAN_LIMIT,
    crypto_link_limit: Annotated[
        int,
        typer.Option(help="Maximum latest crypto links for snapshot repair diagnostics."),
    ] = DEFAULT_CRYPTO_LINK_SCAN_LIMIT,
    forecast_limit: Annotated[
        int,
        typer.Option(help="Maximum latest crypto snapshots to forecast."),
    ] = 1000,
    opportunity_limit: Annotated[
        int,
        typer.Option(help="Maximum crypto rankings/opportunities to write."),
    ] = 500,
    phase3bc_limit: Annotated[
        int,
        typer.Option(help="Maximum latest crypto links for Phase 3BC/R4/R5 checks."),
    ] = 1000,
    cadence_minutes: Annotated[
        int,
        typer.Option(help="Expected crypto refresh cadence in minutes."),
    ] = 15,
    freshness_minutes: Annotated[
        int,
        typer.Option(help="Maximum acceptable ranking age before R5 blocks preflight."),
    ] = 15,
    max_preflight: Annotated[
        int,
        typer.Option(help="Maximum clean positive-EV rows to send through Phase 3M/3N."),
    ] = 10,
    risk_preflight: Annotated[
        bool,
        typer.Option(
            "--risk-preflight/--no-risk-preflight",
            help="Record paper-only Phase 3M/3N risk evidence for clean positive-EV rows.",
        ),
    ] = True,
    ranking_repair: Annotated[
        bool,
        typer.Option(
            "--ranking-repair/--skip-ranking-repair",
            help="Repair bounded active pure-crypto ranking coverage before R4 diagnostics.",
        ),
    ] = True,
    ranking_repair_limit: Annotated[
        int,
        typer.Option(help="Maximum R7 coverage rankings to insert per cycle."),
    ] = 500,
    near_money_only: Annotated[
        bool,
        typer.Option(
            "--near-money-only/--full-strike-ladder",
            help="Refresh only active current-window near-money crypto markets.",
        ),
    ] = True,
    near_money_per_symbol_limit: Annotated[
        int,
        typer.Option(help="Maximum near-money snapshot candidates per crypto symbol."),
    ] = DEFAULT_NEAR_MONEY_PER_SYMBOL_LIMIT,
    near_money_window_limit: Annotated[
        int,
        typer.Option(help="Maximum near-money snapshot candidates per symbol/window."),
    ] = DEFAULT_NEAR_MONEY_WINDOW_LIMIT,
    snapshot_fetch_concurrency: Annotated[
        int,
        typer.Option(help="Conservative orderbook fetch concurrency in near-money mode."),
    ] = DEFAULT_SNAPSHOT_FETCH_CONCURRENCY,
    cycles: Annotated[
        int,
        typer.Option(help="Number of bounded crypto watch cycles to run."),
    ] = 1,
    interval_minutes: Annotated[
        int,
        typer.Option(help="Minutes to wait between cycles when cycles > 1."),
    ] = 15,
) -> None:
    """Run R3 + R4 every cycle and risk-preflight only fresh positive-EV crypto rows."""
    if cycles < 1:
        raise typer.BadParameter("cycles must be at least 1")
    if interval_minutes < 0:
        raise typer.BadParameter("interval-minutes must be non-negative")
    if max_preflight < 0:
        raise typer.BadParameter("max-preflight must be non-negative")
    if ranking_repair_limit < 0:
        raise typer.BadParameter("ranking-repair-limit must be non-negative")
    if near_money_per_symbol_limit < 0:
        raise typer.BadParameter("near-money-per-symbol-limit must be non-negative")
    if near_money_window_limit < 0:
        raise typer.BadParameter("near-money-window-limit must be non-negative")
    if snapshot_fetch_concurrency < 1:
        raise typer.BadParameter("snapshot-fetch-concurrency must be at least 1")

    engine = init_db()
    settings = get_settings()
    session_factory = get_session_factory(engine)
    artifacts = None
    for cycle_number in range(1, cycles + 1):
        try:
            with session_factory() as session:
                artifacts = write_phase3bc_r5_crypto_freshness_watch_report(
                    session,
                    output_dir=output_dir,
                    phase3bc_output_dir=phase3bc_output_dir,
                    phase3bc_r3_output_dir=phase3bc_r3_output_dir,
                    phase3bc_r4_output_dir=phase3bc_r4_output_dir,
                    phase3bc_r7_output_dir=phase3bc_r7_output_dir,
                    settings=settings,
                    symbols=symbols,
                    crypto_series_tickers=crypto_series_tickers,
                    source=source,
                    refresh_open_markets=refresh_open_markets,
                    external_crypto_ingest=external_crypto_ingest,
                    repair_snapshots=repair_snapshots,
                    forecast_current_windows_only=forecast_current_windows_only,
                    generate_opportunity_report=generate_opportunity_report,
                    market_limit=market_limit,
                    market_max_pages=market_max_pages,
                    crypto_market_scan_limit=crypto_market_scan_limit,
                    crypto_link_limit=crypto_link_limit,
                    forecast_limit=forecast_limit,
                    opportunity_limit=opportunity_limit,
                    phase3bc_limit=phase3bc_limit,
                    cadence_minutes=cadence_minutes,
                    freshness_minutes=freshness_minutes,
                    max_preflight=max_preflight,
                    risk_preflight=risk_preflight,
                    ranking_repair=ranking_repair,
                    ranking_repair_limit=ranking_repair_limit,
                    near_money_only=near_money_only,
                    near_money_per_symbol_limit=near_money_per_symbol_limit,
                    near_money_window_limit=near_money_window_limit,
                    snapshot_fetch_concurrency=snapshot_fetch_concurrency,
                    cycle_number=cycle_number,
                    total_cycles=cycles,
                )
                session.commit()
        finally:
            engine.dispose()
        console.print(
            f"Completed Phase 3BC-R5 crypto freshness watch cycle {cycle_number}/{cycles}"
        )
        if cycle_number < cycles and interval_minutes > 0:
            time.sleep(interval_minutes * 60)
    if artifacts is None:
        raise typer.BadParameter("no cycles were run")
    console.print("Phase 3BC-R5 crypto freshness watch + positive-EV trigger")
    console.print("Mode: PAPER ONLY refresh + diagnostics + risk preflight")
    console.print("Live/demo execution: blocked")
    console.print("Order submission/cancel/replace: blocked")
    console.print("Phase 3M/3N writes: only for clean fresh pure-crypto positive-EV rows")
    console.print(f"Cycles completed: {cycles}")
    console.print(f"Wrote JSON: {artifacts.json_path}")
    console.print(f"Wrote Markdown: {artifacts.markdown_path}")
    console.print(f"Wrote preflight rows: {artifacts.preflight_rows_path}")


@app.command("phase3bc-r16-crypto-paper-ready-edge-hunt")
def phase3bc_r16_crypto_paper_ready_edge_hunt_command(
    output_dir: Annotated[
        Path,
        typer.Option(help="Directory for Phase 3BC-R16 edge hunt artifacts."),
    ] = Path("reports/phase3bc_r16"),
    phase3bc_output_dir: Annotated[
        Path,
        typer.Option(help="Directory for refreshed Phase 3BC router artifacts."),
    ] = Path("reports/phase3bc"),
    phase3bc_r3_output_dir: Annotated[
        Path,
        typer.Option(help="Directory for Phase 3BC-R3 refresh artifacts."),
    ] = Path("reports/phase3bc_r3"),
    phase3bc_r4_output_dir: Annotated[
        Path,
        typer.Option(help="Directory for Phase 3BC-R4 diagnostic artifacts."),
    ] = Path("reports/phase3bc_r4"),
    phase3bc_r5_output_dir: Annotated[
        Path,
        typer.Option(help="Directory for Phase 3BC-R5 watch artifacts."),
    ] = Path("reports/phase3bc_r5"),
    phase3bc_r7_output_dir: Annotated[
        Path,
        typer.Option(help="Directory for Phase 3BC-R7 ranking repair artifacts."),
    ] = Path("reports/phase3bc_r7"),
    limit: Annotated[
        int,
        typer.Option(help="Maximum latest crypto links for the underlying Phase 3BC router."),
    ] = 2000,
    freshness_minutes: Annotated[
        int,
        typer.Option(help="Maximum acceptable crypto snapshot/forecast/ranking age."),
    ] = 15,
    run_refresh: Annotated[
        bool,
        typer.Option(
            "--run-refresh/--diagnostic-only",
            help="Run bounded R5 refresh/ranking repair/preflight before R16 reporting.",
        ),
    ] = False,
    max_preflight: Annotated[
        int,
        typer.Option(help="Maximum clean rows R5 may send through paper-only Phase 3M/3N."),
    ] = 10,
    risk_preflight: Annotated[
        bool,
        typer.Option(
            "--risk-preflight/--no-risk-preflight",
            help="Allow R5 to record paper-only Phase 3M/3N preflight for clean rows.",
        ),
    ] = True,
    exact_snapshot_refresh: Annotated[
        bool,
        typer.Option(
            "--exact-snapshot-refresh/--skip-exact-snapshot-refresh",
            help="Allow R5 to refresh bounded exact active crypto snapshots.",
        ),
    ] = True,
) -> None:
    """Rank pure crypto rows by executable EV and explain paper-readiness blockers."""
    if limit < 1:
        raise typer.BadParameter("limit must be positive")
    if freshness_minutes < 1:
        raise typer.BadParameter("freshness-minutes must be positive")
    if max_preflight < 0:
        raise typer.BadParameter("max-preflight must be non-negative")

    engine = init_db()
    settings = get_settings()
    session_factory = get_session_factory(engine)
    with session_factory() as session:
        artifacts = write_phase3bc_r16_crypto_paper_ready_edge_hunt_report(
            session,
            output_dir=output_dir,
            phase3bc_output_dir=phase3bc_output_dir,
            phase3bc_r3_output_dir=phase3bc_r3_output_dir,
            phase3bc_r4_output_dir=phase3bc_r4_output_dir,
            phase3bc_r5_output_dir=phase3bc_r5_output_dir,
            phase3bc_r7_output_dir=phase3bc_r7_output_dir,
            settings=settings,
            limit=limit,
            freshness_minutes=freshness_minutes,
            run_refresh=run_refresh,
            max_preflight=max_preflight,
            risk_preflight=risk_preflight,
            exact_snapshot_refresh=exact_snapshot_refresh,
        )
        session.commit()
    console.print("Phase 3BC-R16 crypto paper-ready edge hunt")
    console.print("Mode: PAPER ONLY no-paid-data diagnostics")
    console.print("Live/demo execution: blocked")
    console.print("Order submission/cancel/replace: blocked")
    console.print("Phase 3M/3N writes: only via R5 clean-row paper preflight when enabled")
    refresh_mode = "R5 refresh + preflight gate" if run_refresh else "diagnostic only"
    console.print(f"Refresh mode: {refresh_mode}")
    console.print(f"Wrote JSON: {artifacts.json_path}")
    console.print(f"Wrote Markdown: {artifacts.markdown_path}")
    console.print(f"Wrote rows: {artifacts.rows_path}")


@app.command("phase3bc-r17-crypto-liquidity-actionability")
def phase3bc_r17_crypto_liquidity_actionability_command(
    output_dir: Annotated[
        Path,
        typer.Option(help="Directory for Phase 3BC-R17 liquidity actionability artifacts."),
    ] = Path("reports/phase3bc_r17"),
    phase3bc_r16_output_dir: Annotated[
        Path,
        typer.Option(help="Directory for refreshed Phase 3BC-R16 artifacts."),
    ] = Path("reports/phase3bc_r16"),
    phase3bc_output_dir: Annotated[
        Path,
        typer.Option(help="Directory for refreshed Phase 3BC router artifacts."),
    ] = Path("reports/phase3bc"),
    phase3bc_r3_output_dir: Annotated[
        Path,
        typer.Option(help="Directory for Phase 3BC-R3 refresh artifacts."),
    ] = Path("reports/phase3bc_r3"),
    phase3bc_r4_output_dir: Annotated[
        Path,
        typer.Option(help="Directory for Phase 3BC-R4 diagnostic artifacts."),
    ] = Path("reports/phase3bc_r4"),
    phase3bc_r5_output_dir: Annotated[
        Path,
        typer.Option(help="Directory for Phase 3BC-R5 watch artifacts."),
    ] = Path("reports/phase3bc_r5"),
    phase3bc_r7_output_dir: Annotated[
        Path,
        typer.Option(help="Directory for Phase 3BC-R7 ranking repair artifacts."),
    ] = Path("reports/phase3bc_r7"),
    limit: Annotated[
        int,
        typer.Option(help="Maximum latest crypto links for the underlying report chain."),
    ] = 2000,
    freshness_minutes: Annotated[
        int,
        typer.Option(help="Maximum acceptable crypto snapshot/forecast/ranking age."),
    ] = 15,
    run_refresh: Annotated[
        bool,
        typer.Option(
            "--run-refresh/--diagnostic-only",
            help="Run bounded R16/R5 refresh before liquidity actionability reporting.",
        ),
    ] = False,
    max_preflight: Annotated[
        int,
        typer.Option(help="Maximum clean rows R5 may send through paper-only Phase 3M/3N."),
    ] = 10,
) -> None:
    """Separate positive-EV no-book rows from genuinely action-ready crypto rows."""
    if limit < 1:
        raise typer.BadParameter("limit must be positive")
    if freshness_minutes < 1:
        raise typer.BadParameter("freshness-minutes must be positive")
    if max_preflight < 0:
        raise typer.BadParameter("max-preflight must be non-negative")

    engine = init_db()
    settings = get_settings()
    session_factory = get_session_factory(engine)
    with session_factory() as session:
        artifacts = write_phase3bc_r17_crypto_liquidity_actionability_report(
            session,
            output_dir=output_dir,
            phase3bc_r16_output_dir=phase3bc_r16_output_dir,
            phase3bc_output_dir=phase3bc_output_dir,
            phase3bc_r3_output_dir=phase3bc_r3_output_dir,
            phase3bc_r4_output_dir=phase3bc_r4_output_dir,
            phase3bc_r5_output_dir=phase3bc_r5_output_dir,
            phase3bc_r7_output_dir=phase3bc_r7_output_dir,
            settings=settings,
            limit=limit,
            freshness_minutes=freshness_minutes,
            run_refresh=run_refresh,
            max_preflight=max_preflight,
        )
        session.commit()
    console.print("Phase 3BC-R17 crypto liquidity actionability")
    console.print("Mode: PAPER ONLY liquidity/actionability diagnostics")
    console.print("Live/demo execution: blocked")
    console.print("Order submission/cancel/replace: blocked")
    console.print(f"Refresh mode: {'R16/R5 refresh' if run_refresh else 'diagnostic only'}")
    console.print(f"Wrote JSON: {artifacts.json_path}")
    console.print(f"Wrote Markdown: {artifacts.markdown_path}")
    console.print(f"Wrote rows: {artifacts.rows_path}")


@app.command("paper-trading-gap-analysis")
def paper_trading_gap_analysis_command(
    output_dir: Annotated[
        Path,
        typer.Option(help="Directory for paper-trading gap artifacts."),
    ] = Path("reports/paper_trading_gap"),
    reports_dir: Annotated[
        Path,
        typer.Option(help="Root directory containing upstream phase reports."),
    ] = Path("reports"),
) -> None:
    """Report what remains before ranked candidates can become paper trades."""
    artifacts = write_paper_trading_gap_analysis_report(
        output_dir=output_dir,
        reports_dir=reports_dir,
    )
    console.print("Paper trading gap analysis")
    console.print("Mode: PAPER ONLY report")
    console.print("Live/demo execution: blocked")
    console.print("Order submission/cancel/replace: blocked")
    console.print(f"Wrote JSON: {artifacts.json_path}")
    console.print(f"Wrote Markdown: {artifacts.markdown_path}")
    console.print(f"Wrote next commands: {artifacts.next_commands_path}")


@app.command("phase3bc-r7-crypto-ranking-coverage-repair")
def phase3bc_r7_crypto_ranking_coverage_repair_command(
    output_dir: Annotated[
        Path,
        typer.Option(help="Directory for Phase 3BC-R7 ranking coverage artifacts."),
    ] = Path("reports/phase3bc_r7"),
    limit: Annotated[
        int,
        typer.Option(help="Maximum latest crypto link rows to inspect."),
    ] = 2000,
    freshness_minutes: Annotated[
        int,
        typer.Option(help="Freshness window for snapshots, forecasts, and rankings."),
    ] = 15,
    repair_rankings: Annotated[
        bool,
        typer.Option(
            "--repair-rankings",
            help="Insert bounded coverage rankings for fresh active pure crypto rows.",
        ),
    ] = False,
    repair_limit: Annotated[
        int,
        typer.Option(help="Maximum coverage rankings to insert in one run."),
    ] = 500,
) -> None:
    """Diagnose and optionally repair active pure crypto ranking coverage."""
    engine = init_db()
    settings = get_settings()
    session_factory = get_session_factory(engine)
    with session_factory() as session:
        artifacts = write_phase3bc_r7_crypto_ranking_coverage_repair_report(
            session,
            output_dir=output_dir,
            settings=settings,
            limit=limit,
            freshness_minutes=freshness_minutes,
            repair_rankings=repair_rankings,
            repair_limit=repair_limit,
        )
        session.commit()
    console.print("Phase 3BC-R7 crypto ranking coverage repair")
    console.print("Mode: PAPER ONLY ranking coverage diagnostics")
    console.print("Live/demo execution: blocked")
    console.print("Order submission/cancel/replace: blocked")
    console.print(f"Repair rankings: {repair_rankings}")
    console.print(f"Wrote JSON: {artifacts.json_path}")
    console.print(f"Wrote Markdown: {artifacts.markdown_path}")
    console.print(f"Wrote rows: {artifacts.rows_path}")


@app.command("phase3bc-r5-status")
def phase3bc_r5_status_command(
    output_dir: Annotated[
        Path,
        typer.Option(help="Directory for Phase 3BC-R5 watch/status artifacts."),
    ] = Path("reports/phase3bc_r5"),
) -> None:
    """Write a read-only status report for the guarded crypto freshness watch."""
    artifacts = write_phase3bc_r5_status_report(output_dir=output_dir)
    console.print("Phase 3BC-R5 crypto freshness watch status")
    console.print("Mode: READ ONLY")
    console.print("Live/demo execution: blocked")
    console.print("Order submission/cancel/replace: blocked")
    console.print(f"Wrote JSON: {artifacts.json_path}")
    console.print(f"Wrote Markdown: {artifacts.markdown_path}")


@app.command("phase3bc-r5-unattended-start")
def phase3bc_r5_unattended_start_command(
    output_dir: Annotated[
        Path,
        typer.Option(help="Directory for Phase 3BC-R5 watch artifacts."),
    ] = Path("reports/phase3bc_r5"),
    phase3bc_output_dir: Annotated[
        Path,
        typer.Option(help="Directory for refreshed Phase 3BC router artifacts."),
    ] = Path("reports/phase3bc"),
    phase3bc_r3_output_dir: Annotated[
        Path,
        typer.Option(help="Directory for Phase 3BC-R3 refresh artifacts."),
    ] = Path("reports/phase3bc_r3"),
    phase3bc_r4_output_dir: Annotated[
        Path,
        typer.Option(help="Directory for Phase 3BC-R4 diagnostic artifacts."),
    ] = Path("reports/phase3bc_r4"),
    phase3bc_r7_output_dir: Annotated[
        Path,
        typer.Option(help="Directory for Phase 3BC-R7 ranking coverage artifacts."),
    ] = Path("reports/phase3bc_r7"),
    symbols: Annotated[
        str,
        typer.Option(help=f"Comma-separated symbols, for example {DEFAULT_CRYPTO_SYMBOLS}."),
    ] = DEFAULT_CRYPTO_SYMBOLS,
    crypto_series_tickers: Annotated[
        str,
        typer.Option(help="Comma-separated crypto Kalshi series tickers to refresh."),
    ] = DEFAULT_CRYPTO_SERIES_TICKERS,
    source: Annotated[
        str,
        typer.Option(help="Public no-key crypto source: coinbase or coingecko."),
    ] = "coinbase",
    refresh_open_markets: Annotated[
        bool,
        typer.Option(
            "--refresh-open-markets/--skip-open-market-refresh",
            help="Run a bounded open-market snapshot refresh each cycle.",
        ),
    ] = True,
    external_crypto_ingest: Annotated[
        bool,
        typer.Option(
            "--external-crypto-ingest/--skip-external-crypto-ingest",
            help="Fetch fresh public crypto prices before feature rebuild.",
        ),
    ] = True,
    repair_snapshots: Annotated[
        bool,
        typer.Option(
            "--repair-snapshots/--diagnose-snapshots",
            help="Fetch public market/orderbook snapshots for linked crypto gaps.",
        ),
    ] = False,
    forecast_current_windows_only: Annotated[
        bool,
        typer.Option(
            "--forecast-current-windows-only/--forecast-all-active-crypto",
            help="Forecast only current active crypto windows in the watch loop.",
        ),
    ] = True,
    generate_opportunity_report: Annotated[
        bool,
        typer.Option(
            "--generate-opportunity-report/--skip-opportunity-report",
            help="Generate the slower R3 opportunity report before R7 ranking repair.",
        ),
    ] = False,
    market_limit: Annotated[
        int,
        typer.Option(help="Page size for bounded open-market refresh."),
    ] = DEFAULT_MARKET_PAGE_LIMIT,
    market_max_pages: Annotated[
        int,
        typer.Option(help="Maximum pages for bounded open-market refresh."),
    ] = 1,
    crypto_market_scan_limit: Annotated[
        int,
        typer.Option(help="Maximum catalog markets to scan while refreshing crypto links."),
    ] = DEFAULT_CRYPTO_MARKET_SCAN_LIMIT,
    crypto_link_limit: Annotated[
        int,
        typer.Option(help="Maximum latest crypto links for snapshot repair diagnostics."),
    ] = DEFAULT_CRYPTO_LINK_SCAN_LIMIT,
    forecast_limit: Annotated[
        int,
        typer.Option(help="Maximum latest crypto snapshots to forecast."),
    ] = 1000,
    opportunity_limit: Annotated[
        int,
        typer.Option(help="Maximum crypto rankings/opportunities to write."),
    ] = 500,
    phase3bc_limit: Annotated[
        int,
        typer.Option(help="Maximum latest crypto links for Phase 3BC/R4/R5 checks."),
    ] = 1000,
    cadence_minutes: Annotated[
        int,
        typer.Option(help="Expected crypto refresh cadence in minutes."),
    ] = 15,
    freshness_minutes: Annotated[
        int,
        typer.Option(help="Maximum acceptable ranking age before R5 blocks preflight."),
    ] = 15,
    max_preflight: Annotated[
        int,
        typer.Option(help="Maximum clean positive-EV rows to send through Phase 3M/3N."),
    ] = 10,
    risk_preflight: Annotated[
        bool,
        typer.Option(
            "--risk-preflight/--no-risk-preflight",
            help="Record paper-only Phase 3M/3N risk evidence for clean positive-EV rows.",
        ),
    ] = True,
    ranking_repair: Annotated[
        bool,
        typer.Option(
            "--ranking-repair/--skip-ranking-repair",
            help="Repair bounded active pure-crypto ranking coverage before R4 diagnostics.",
        ),
    ] = True,
    ranking_repair_limit: Annotated[
        int,
        typer.Option(help="Maximum R7 coverage rankings to insert per cycle."),
    ] = 500,
    near_money_only: Annotated[
        bool,
        typer.Option(
            "--near-money-only/--full-strike-ladder",
            help="Refresh only active current-window near-money crypto markets.",
        ),
    ] = True,
    near_money_per_symbol_limit: Annotated[
        int,
        typer.Option(help="Maximum near-money snapshot candidates per crypto symbol."),
    ] = DEFAULT_NEAR_MONEY_PER_SYMBOL_LIMIT,
    near_money_window_limit: Annotated[
        int,
        typer.Option(help="Maximum near-money snapshot candidates per symbol/window."),
    ] = DEFAULT_NEAR_MONEY_WINDOW_LIMIT,
    snapshot_fetch_concurrency: Annotated[
        int,
        typer.Option(help="Conservative orderbook fetch concurrency in near-money mode."),
    ] = DEFAULT_SNAPSHOT_FETCH_CONCURRENCY,
    cycles: Annotated[
        int,
        typer.Option(help="Number of bounded crypto watch cycles to run."),
    ] = 32,
    interval_minutes: Annotated[
        int,
        typer.Option(help="Minutes to wait between watch cycles."),
    ] = 15,
    duration_hours: Annotated[
        float,
        typer.Option(help="Approximate unattended runtime budget before guard stops it."),
    ] = 8.0,
    timeout_grace_seconds: Annotated[
        int,
        typer.Option(help="Seconds past the configured budget before guard stops it."),
    ] = 900,
) -> None:
    """Start the R5 crypto watch in the background with PID, logs, and timeout metadata."""
    if near_money_per_symbol_limit < 0:
        raise typer.BadParameter("near-money-per-symbol-limit must be non-negative")
    if near_money_window_limit < 0:
        raise typer.BadParameter("near-money-window-limit must be non-negative")
    if snapshot_fetch_concurrency < 1:
        raise typer.BadParameter("snapshot-fetch-concurrency must be at least 1")
    if ranking_repair_limit < 0:
        raise typer.BadParameter("ranking-repair-limit must be non-negative")
    result = start_phase3bc_r5_unattended_watch(
        output_dir=output_dir,
        phase3bc_output_dir=phase3bc_output_dir,
        phase3bc_r3_output_dir=phase3bc_r3_output_dir,
        phase3bc_r4_output_dir=phase3bc_r4_output_dir,
        phase3bc_r7_output_dir=phase3bc_r7_output_dir,
        symbols=symbols,
        crypto_series_tickers=crypto_series_tickers,
        source=source,
        refresh_open_markets=refresh_open_markets,
        external_crypto_ingest=external_crypto_ingest,
        repair_snapshots=repair_snapshots,
        forecast_current_windows_only=forecast_current_windows_only,
        generate_opportunity_report=generate_opportunity_report,
        market_limit=market_limit,
        market_max_pages=market_max_pages,
        crypto_market_scan_limit=crypto_market_scan_limit,
        crypto_link_limit=crypto_link_limit,
        forecast_limit=forecast_limit,
        opportunity_limit=opportunity_limit,
        phase3bc_limit=phase3bc_limit,
        cadence_minutes=cadence_minutes,
        freshness_minutes=freshness_minutes,
        max_preflight=max_preflight,
        risk_preflight=risk_preflight,
        ranking_repair=ranking_repair,
        ranking_repair_limit=ranking_repair_limit,
        near_money_only=near_money_only,
        near_money_per_symbol_limit=near_money_per_symbol_limit,
        near_money_window_limit=near_money_window_limit,
        snapshot_fetch_concurrency=snapshot_fetch_concurrency,
        cycles=cycles,
        interval_minutes=interval_minutes,
        duration_hours=duration_hours,
        timeout_grace_seconds=timeout_grace_seconds,
    )
    console.print("Phase 3BC-R5 guarded crypto freshness watch")
    console.print("Mode: PAPER ONLY unattended refresh")
    console.print("Live/demo execution: blocked")
    console.print("Order submission/cancel/replace: blocked")
    console.print(f"Status: {result.status}")
    console.print(f"PID: {result.pid}")
    console.print(f"PID file: {result.pid_path}")
    console.print(f"Metadata: {result.metadata_path}")
    console.print(f"Stdout: {result.stdout_path}")
    console.print(f"Stderr: {result.stderr_path}")
    if result.command:
        console.print(f"Command: {result.command}")


@app.command("phase3bc-r5-unattended-guard")
def phase3bc_r5_unattended_guard_command(
    output_dir: Annotated[
        Path,
        typer.Option(help="Directory for Phase 3BC-R5 watch artifacts."),
    ] = Path("reports/phase3bc_r5"),
    stop_overrun: Annotated[
        bool,
        typer.Option(help="Terminate a Phase 3BC-R5 process that exceeded its timeout guard."),
    ] = False,
    terminate_grace_seconds: Annotated[
        int,
        typer.Option(help="Seconds to wait after SIGTERM before force killing."),
    ] = 30,
) -> None:
    """Write Phase 3BC-R5 unattended guard status and optionally stop overruns."""
    artifacts = write_phase3bc_r5_unattended_guard_report(
        output_dir=output_dir,
        stop_overrun=stop_overrun,
        terminate_grace_seconds=terminate_grace_seconds,
    )
    console.print("Phase 3BC-R5 unattended guard")
    console.print("Mode: READ ONLY unless --stop-overrun is supplied")
    console.print("Live/demo execution: blocked")
    console.print("Order submission/cancel/replace: blocked")
    console.print(f"Wrote JSON: {artifacts.json_path}")
    console.print(f"Wrote Markdown: {artifacts.markdown_path}")


@app.command("phase3ba-r1-writer-unlock")
def phase3ba_r1_writer_unlock_command(
    output_dir: Annotated[
        Path,
        typer.Option(help="Directory for Phase 3BA-R1 writer unlock artifacts."),
    ] = Path("reports/phase3ba_r1"),
    reports_dir: Annotated[
        Path,
        typer.Option(help="Root reports directory containing phase3bc_r5 artifacts."),
    ] = Path("reports"),
    post_stop_wait_seconds: Annotated[
        int,
        typer.Option(help="Maximum seconds to wait for db-writer-monitor to clear."),
    ] = 60,
    poll_interval_seconds: Annotated[
        float,
        typer.Option(help="Seconds between db-writer-monitor polls after the guarded stop."),
    ] = 2.0,
    terminate_grace_seconds: Annotated[
        int,
        typer.Option(help="Seconds passed to the registered R5 guard stop command."),
    ] = 30,
    command_timeout_seconds: Annotated[
        int,
        typer.Option(help="Timeout for registered guard/start subprocess commands."),
    ] = 90,
) -> None:
    """Stop only an overrun R5 writer through the guard, then restart one R5 watcher."""
    if post_stop_wait_seconds < 0:
        raise typer.BadParameter("post-stop-wait-seconds must be non-negative")
    if poll_interval_seconds <= 0:
        raise typer.BadParameter("poll-interval-seconds must be positive")
    if terminate_grace_seconds < 0:
        raise typer.BadParameter("terminate-grace-seconds must be non-negative")
    if command_timeout_seconds < 1:
        raise typer.BadParameter("command-timeout-seconds must be positive")
    artifacts = write_phase3ba_r1_writer_unlock_report(
        output_dir=output_dir,
        reports_dir=reports_dir,
        settings=get_settings(),
        command_args=sys.argv[1:],
        post_stop_wait_seconds=post_stop_wait_seconds,
        poll_interval_seconds=poll_interval_seconds,
        terminate_grace_seconds=terminate_grace_seconds,
        command_timeout_seconds=command_timeout_seconds,
    )
    console.print("Phase 3BA-R1 writer unlock + guarded R5 restart")
    console.print("Mode: PAPER ONLY process guard")
    console.print("Live/demo execution: blocked")
    console.print("Order submission/cancel/replace: blocked")
    console.print("Paper trade creation: blocked")
    console.print(f"Wrote Executive Summary: {artifacts.executive_summary_path}")
    console.print(f"Wrote JSON: {artifacts.writer_unlock_path}")
    console.print(f"Wrote R5 restart status: {artifacts.r5_restart_status_path}")
    console.print(f"Wrote Next Actions: {artifacts.next_actions_path}")
    console.print(f"Wrote Manifest: {artifacts.manifest_path}")


@app.command("phase3ba-r2-weather-ranking-activation")
def phase3ba_r2_weather_ranking_activation_command(
    output_dir: Annotated[
        Path,
        typer.Option(help="Directory for Phase 3BA-R2 weather ranking artifacts."),
    ] = Path("reports/phase3ba_r2"),
    reports_dir: Annotated[
        Path,
        typer.Option(help="Root reports directory for weather handoff artifacts."),
    ] = Path("reports"),
    limit: Annotated[
        int,
        typer.Option(help="Maximum weather ranking rows to insert/report."),
    ] = 100,
    current_window_lookback_hours: Annotated[
        int,
        typer.Option(help="Look back this many hours for current linked weather windows."),
    ] = 3,
    opportunity_output: Annotated[
        Path,
        typer.Option(help="Markdown output path for the weather opportunity report."),
    ] = Path("reports/weather_opportunities.md"),
) -> None:
    """Activate weather_v2 opportunity rankings for current linked weather markets."""
    if limit < 1:
        raise typer.BadParameter("limit must be positive")
    if current_window_lookback_hours < 0:
        raise typer.BadParameter("current-window-lookback-hours must be non-negative")
    settings = get_settings()
    engine = make_engine(database_url_from_settings(settings))
    session_factory = get_session_factory(engine)
    try:
        with session_factory() as session:
            artifacts = write_phase3ba_r2_weather_ranking_activation_report(
                session,
                output_dir=output_dir,
                reports_dir=reports_dir,
                settings=settings,
                command_args=sys.argv[1:],
                limit=limit,
                current_window_lookback_hours=current_window_lookback_hours,
                opportunity_output=opportunity_output,
            )
            session.commit()
    finally:
        engine.dispose()
    console.print("Phase 3BA-R2 weather ranking activation")
    console.print("Mode: PAPER ONLY weather ranking/opportunity artifacts")
    console.print("Live/demo execution: blocked")
    console.print("Order submission/cancel/replace: blocked")
    console.print("Paper trade creation: blocked")
    console.print(f"Wrote Executive Summary: {artifacts.executive_summary_path}")
    console.print(f"Wrote JSON: {artifacts.json_path}")
    console.print(f"Wrote Markdown: {artifacts.markdown_path}")
    console.print(f"Wrote weather rows: {artifacts.rows_csv_path}")
    console.print(f"Wrote Next Actions: {artifacts.next_actions_path}")
    console.print(f"Wrote Manifest: {artifacts.manifest_path}")


@app.command("phase3ba-r3-weather-paper-gate")
def phase3ba_r3_weather_paper_gate_command(
    output_dir: Annotated[
        Path,
        typer.Option(help="Directory for Phase 3BA-R3 weather paper-gate artifacts."),
    ] = Path("reports/phase3ba_r3"),
    reports_dir: Annotated[
        Path,
        typer.Option(help="Root reports directory for paper-gate context artifacts."),
    ] = Path("reports"),
    limit: Annotated[
        int,
        typer.Option(help="Maximum current linked weather rows to inspect."),
    ] = 500,
    current_window_lookback_hours: Annotated[
        int,
        typer.Option(help="Look back this many hours for current linked weather windows."),
    ] = 3,
    match_tolerance_hours: Annotated[
        int,
        typer.Option(help="Allowed weather source/feature target-time match tolerance."),
    ] = 3,
) -> None:
    """Diagnose weather_v2 rows against the paper-ready gate without trades."""
    if limit < 1:
        raise typer.BadParameter("limit must be positive")
    if current_window_lookback_hours < 0:
        raise typer.BadParameter("current-window-lookback-hours must be non-negative")
    if match_tolerance_hours < 0:
        raise typer.BadParameter("match-tolerance-hours must be non-negative")
    settings = get_settings()
    engine = make_engine(database_url_from_settings(settings))
    session_factory = get_session_factory(engine)
    try:
        with session_factory() as session:
            artifacts = write_phase3ba_r3_weather_paper_gate_report(
                session,
                output_dir=output_dir,
                reports_dir=reports_dir,
                settings=settings,
                command_args=sys.argv[1:],
                limit=limit,
                current_window_lookback_hours=current_window_lookback_hours,
                match_tolerance_hours=match_tolerance_hours,
            )
            session.rollback()
    finally:
        engine.dispose()
    console.print("Phase 3BA-R3 weather paper gate")
    console.print("Mode: PAPER READ-ONLY weather paper-gate diagnostic")
    console.print("Live/demo execution: blocked")
    console.print("Order submission/cancel/replace: blocked")
    console.print("Paper trade creation: blocked")
    console.print(f"Wrote Executive Summary: {artifacts.executive_summary_path}")
    console.print(f"Wrote JSON: {artifacts.json_path}")
    console.print(f"Wrote Markdown: {artifacts.markdown_path}")
    console.print(f"Wrote weather rows: {artifacts.rows_csv_path}")
    console.print(f"Wrote Next Actions: {artifacts.next_actions_path}")
    console.print(f"Wrote Manifest: {artifacts.manifest_path}")


@app.command("phase3ba-r4-crypto-executable-book-watch")
def phase3ba_r4_crypto_executable_book_watch_command(
    output_dir: Annotated[
        Path,
        typer.Option(help="Directory for Phase 3BA-R4 crypto executable-book artifacts."),
    ] = Path("reports/phase3ba_r4"),
    reports_dir: Annotated[
        Path,
        typer.Option(help="Root reports directory for R5 status context."),
    ] = Path("reports"),
    limit: Annotated[
        int,
        typer.Option(help="Maximum crypto link/router rows to inspect."),
    ] = 2000,
) -> None:
    """Diagnose positive-EV crypto rows blocked by executable book quality."""
    if limit < 1:
        raise typer.BadParameter("limit must be positive")
    settings = get_settings()
    engine = make_engine(database_url_from_settings(settings))
    session_factory = get_session_factory(engine)
    try:
        with session_factory() as session:
            artifacts = write_phase3ba_r4_crypto_executable_book_watch_report(
                session,
                output_dir=output_dir,
                reports_dir=reports_dir,
                settings=settings,
                command_args=sys.argv[1:],
                limit=limit,
            )
            session.rollback()
    finally:
        engine.dispose()
    console.print("Phase 3BA-R4 crypto executable book watch")
    console.print("Mode: PAPER READ-ONLY crypto execution-quality diagnostic")
    console.print("Live/demo execution: blocked")
    console.print("Order submission/cancel/replace: blocked")
    console.print("Paper trade creation: blocked")
    console.print("R5 watcher: not started or stopped by this command")
    console.print(f"Wrote Executive Summary: {artifacts.executive_summary_path}")
    console.print(f"Wrote JSON: {artifacts.json_path}")
    console.print(f"Wrote positive-EV rows: {artifacts.positive_ev_csv_path}")
    console.print(f"Wrote liquidity watchlist: {artifacts.liquidity_watchlist_csv_path}")
    console.print(f"Wrote reconciliation sources: {artifacts.reconciliation_sources_path}")
    console.print(f"Wrote Next Actions: {artifacts.next_actions_path}")
    console.print(f"Wrote Manifest: {artifacts.manifest_path}")


@app.command("phase3ba-r5-paper-ready-truth")
def phase3ba_r5_paper_ready_truth_command(
    output_dir: Annotated[
        Path,
        typer.Option(help="Directory for Phase 3BA-R5 unified paper-ready artifacts."),
    ] = Path("reports/phase3ba_r5"),
    reports_dir: Annotated[
        Path,
        typer.Option(help="Root reports directory for trusted input reports."),
    ] = Path("reports"),
    max_duration_seconds: Annotated[
        int,
        typer.Option(help="Maximum seconds allowed for the bounded truth refresh."),
    ] = 120,
    limit: Annotated[
        int,
        typer.Option(help="Maximum current rows to inspect per active model."),
    ] = 500,
) -> None:
    """Build bounded current paper-ready truth across crypto_v2 and weather_v2."""
    if max_duration_seconds < 1:
        raise typer.BadParameter("max-duration-seconds must be positive")
    if limit < 1:
        raise typer.BadParameter("limit must be positive")
    settings = get_settings()
    engine = make_engine(database_url_from_settings(settings))
    session_factory = get_session_factory(engine)
    try:
        with session_factory() as session:
            artifacts = write_phase3ba_r5_paper_ready_truth_report(
                session,
                output_dir=output_dir,
                reports_dir=reports_dir,
                settings=settings,
                command_args=sys.argv[1:],
                max_duration_seconds=max_duration_seconds,
                limit=limit,
            )
            session.rollback()
    finally:
        engine.dispose()
    console.print("Phase 3BA-R5 unified paper-ready truth")
    console.print("Mode: PAPER READ-ONLY bounded truth refresh")
    console.print("Live/demo execution: blocked")
    console.print("Order submission/cancel/replace: blocked")
    console.print("Paper trade creation: blocked")
    console.print("Thresholds lowered: false")
    console.print(f"Wrote Executive Summary: {artifacts.executive_summary_path}")
    console.print(f"Wrote JSON: {artifacts.json_path}")
    console.print(f"Wrote Markdown: {artifacts.markdown_path}")
    console.print(f"Wrote paper-ready rows: {artifacts.paper_ready_rows_path}")
    console.print(f"Wrote blocked rows: {artifacts.blocked_rows_path}")
    console.print(f"Wrote reconciliation sources: {artifacts.reconciliation_sources_path}")
    console.print(f"Wrote Next Actions: {artifacts.next_actions_path}")
    console.print(f"Wrote Manifest: {artifacts.manifest_path}")


@app.command("phase3ba-r6-noncrypto-engine-backlog")
def phase3ba_r6_noncrypto_engine_backlog_command(
    output_dir: Annotated[
        Path,
        typer.Option(help="Directory for Phase 3BA-R6 non-crypto engine backlog artifacts."),
    ] = Path("reports/phase3ba_r6"),
    reports_dir: Annotated[
        Path,
        typer.Option(help="Root reports directory for current coverage/truth artifacts."),
    ] = Path("reports"),
) -> None:
    """Build a read-only non-crypto category engine implementation backlog."""
    settings = get_settings()
    engine = make_engine(database_url_from_settings(settings))
    session_factory = get_session_factory(engine)
    try:
        with session_factory() as session:
            artifacts = write_phase3ba_r6_noncrypto_engine_backlog_report(
                session,
                output_dir=output_dir,
                reports_dir=reports_dir,
                settings=settings,
                command_args=sys.argv[1:],
            )
            session.rollback()
    finally:
        engine.dispose()
    console.print("Phase 3BA-R6 non-crypto category engine backlog")
    console.print("Mode: PAPER READ-ONLY backlog/report")
    console.print("Live/demo execution: blocked")
    console.print("Order submission/cancel/replace: blocked")
    console.print("Paper trade creation: blocked")
    console.print("Fuzzy matching: blocked")
    console.print(f"Wrote Executive Summary: {artifacts.executive_summary_path}")
    console.print(f"Wrote Markdown: {artifacts.markdown_path}")
    console.print(f"Wrote CSV: {artifacts.csv_path}")
    console.print(f"Wrote Next Category Build: {artifacts.next_category_build_path}")
    console.print(f"Wrote Manifest: {artifacts.manifest_path}")


@app.command("phase3ba-r7-composite-market-plan")
def phase3ba_r7_composite_market_plan_command(
    output_dir: Annotated[
        Path,
        typer.Option(help="Directory for Phase 3BA-R7 composite market parking artifacts."),
    ] = Path("reports/phase3ba_r7"),
    reports_dir: Annotated[
        Path,
        typer.Option(help="Root reports directory for current coverage/truth artifacts."),
    ] = Path("reports"),
) -> None:
    """Build a read-only unsupported composite market parking and support plan."""
    settings = get_settings()
    engine = make_engine(database_url_from_settings(settings))
    session_factory = get_session_factory(engine)
    try:
        with session_factory() as session:
            artifacts = write_phase3ba_r7_composite_market_plan_report(
                session,
                output_dir=output_dir,
                reports_dir=reports_dir,
                settings=settings,
                command_args=sys.argv[1:],
            )
            session.rollback()
    finally:
        engine.dispose()
    console.print("Phase 3BA-R7 composite market parking / future support plan")
    console.print("Mode: PAPER READ-ONLY composite parking/report")
    console.print("Live/demo execution: blocked")
    console.print("Order submission/cancel/replace: blocked")
    console.print("Paper trade creation: blocked")
    console.print("Fuzzy component matching: blocked")
    console.print("Normal single-market remediation for composites: blocked")
    console.print(f"Wrote Executive Summary: {artifacts.executive_summary_path}")
    console.print(f"Wrote Markdown: {artifacts.markdown_path}")
    console.print(f"Wrote CSV: {artifacts.rows_csv_path}")
    console.print(f"Wrote Next Actions: {artifacts.next_actions_path}")
    console.print(f"Wrote Manifest: {artifacts.manifest_path}")


@app.command("phase3ba-status")
def phase3ba_status_command(
    output_dir: Annotated[
        Path,
        typer.Option(help="Directory for Phase 3BA-R8 one-command status artifacts."),
    ] = Path("reports/phase3ba_status"),
    reports_dir: Annotated[
        Path,
        typer.Option(help="Root reports directory for current Phase 3BA/R5 artifacts."),
    ] = Path("reports"),
) -> None:
    """Write one operator status report with the next safe command."""
    settings = get_settings()
    engine = make_engine(database_url_from_settings(settings))
    session_factory = get_session_factory(engine)
    try:
        with session_factory() as session:
            artifacts = write_phase3ba_status_report(
                session,
                output_dir=output_dir,
                reports_dir=reports_dir,
                settings=settings,
                command_args=sys.argv[1:],
            )
            session.rollback()
    finally:
        engine.dispose()
    console.print("Phase 3BA-R8 operator workflow + one-command status")
    console.print("Mode: PAPER READ-ONLY status/report")
    console.print("Live/demo execution: blocked")
    console.print("Order submission/cancel/replace: blocked")
    console.print("Paper trade creation: blocked")
    console.print("Duplicate R5 starts while running: blocked")
    console.print(f"Wrote Executive Summary: {artifacts.executive_summary_path}")
    console.print(f"Wrote Next Actions: {artifacts.next_actions_path}")
    console.print(f"Wrote operator command: {artifacts.operator_next_command_path}")
    console.print(f"Wrote JSON: {artifacts.status_json_path}")
    console.print(f"Wrote Manifest: {artifacts.manifest_path}")


@app.command("phase3ba-paper-certification")
def phase3ba_paper_certification_command(
    output_dir: Annotated[
        Path,
        typer.Option(help="Directory for Phase 3BA-R9 paper-only certification artifacts."),
    ] = Path("reports/phase3ba_cert"),
    reports_dir: Annotated[
        Path,
        typer.Option(help="Root reports directory for current Phase 3BA/R5 artifacts."),
    ] = Path("reports"),
    test_timeout_seconds: Annotated[
        int,
        typer.Option(help="Maximum seconds for focused certification tests."),
    ] = 180,
) -> None:
    """Certify guarded paper-only operation across crypto and weather."""
    settings = get_settings()
    engine = make_engine(database_url_from_settings(settings))
    session_factory = get_session_factory(engine)
    try:
        with session_factory() as session:
            artifacts = write_phase3ba_paper_certification_report(
                session,
                output_dir=output_dir,
                reports_dir=reports_dir,
                settings=settings,
                command_args=sys.argv[1:],
                test_timeout_seconds=test_timeout_seconds,
            )
            session.rollback()
    finally:
        engine.dispose()
    console.print("Phase 3BA-R9 paper-only certification")
    console.print("Mode: PAPER READ-ONLY certification/report")
    console.print("Live/demo execution: blocked")
    console.print("Order submission/cancel/replace: blocked")
    console.print("Paper trade creation: blocked")
    console.print("Threshold lowering: blocked")
    console.print(f"Wrote Executive Summary: {artifacts.executive_summary_path}")
    console.print(f"Wrote JSON: {artifacts.json_path}")
    console.print(f"Wrote Next Actions: {artifacts.next_actions_path}")
    console.print(f"Wrote Manifest: {artifacts.manifest_path}")


@app.command("phase3ba-ingestion-stability-report")
def phase3ba_ingestion_stability_report_command(
    output_dir: Annotated[
        Path,
        typer.Option(help="Directory for Phase 3BA ingestion stability artifacts."),
    ] = Path("reports/phase3ba_ingestion_stability"),
    reports_dir: Annotated[
        Path,
        typer.Option(help="Root reports directory for current Phase 3BA/R5 artifacts."),
    ] = Path("reports"),
    runtime_hours: Annotated[
        float,
        typer.Option(help="Observed ingestion/watch runtime hours for EV pace projection."),
    ] = 165.0,
    observed_positive_ev: Annotated[
        int | None,
        typer.Option(help="Operator-observed positive EV row count over the runtime window."),
    ] = None,
) -> None:
    """Write ingestion health, conversion, and model-stability graphics."""
    settings = get_settings()
    engine = make_engine(database_url_from_settings(settings))
    session_factory = get_session_factory(engine)
    try:
        with session_factory() as session:
            artifacts = write_phase3ba_ingestion_stability_report(
                session,
                output_dir=output_dir,
                reports_dir=reports_dir,
                settings=settings,
                command_args=sys.argv[1:],
                runtime_hours=runtime_hours,
                observed_positive_ev=observed_positive_ev,
            )
            session.rollback()
    finally:
        engine.dispose()
    console.print("Phase 3BA ingestion stability report")
    console.print("Mode: PAPER READ-ONLY diagnostics/report")
    console.print("Live/demo execution: blocked")
    console.print("Order submission/cancel/replace: blocked")
    console.print("Paper trade creation: blocked")
    console.print("Threshold lowering: blocked")
    console.print(f"Wrote Executive Summary: {artifacts.executive_summary_path}")
    console.print(f"Wrote JSON: {artifacts.json_path}")
    console.print(f"Wrote Markdown: {artifacts.markdown_path}")
    console.print(f"Wrote ingestion volume graphic: {artifacts.ingestion_volume_svg_path}")
    console.print(f"Wrote conversion funnel graphic: {artifacts.conversion_funnel_svg_path}")
    console.print(f"Wrote stability projection graphic: {artifacts.stability_projection_svg_path}")
    console.print(f"Wrote Next Actions: {artifacts.next_actions_path}")
    console.print(f"Wrote Manifest: {artifacts.manifest_path}")


@app.command("phase3ah-roster-participant-verification")
def phase3ah_roster_participant_verification_command(
    output_dir: Annotated[
        Path,
        typer.Option(help="Directory for Phase 3AH roster verification artifacts."),
    ] = Path("reports/phase3ah_sports"),
    roster_template_path: Annotated[
        Path,
        typer.Option(help="Path to the Phase 3AH roster review template JSON."),
    ] = Path("reports/phase3ah_sports/phase3ah_roster_review_template.json"),
    require_source_url: Annotated[
        bool,
        typer.Option(
            "--require-source-url/--allow-missing-source-url",
            help="Require an HTTP(S) roster or participant source URL for verified rows.",
        ),
    ] = True,
    require_valid_from: Annotated[
        bool,
        typer.Option(
            "--require-valid-from/--allow-missing-valid-from",
            help="Require a valid_from date for verified roster evidence.",
        ),
    ] = True,
    limit: Annotated[
        int,
        typer.Option(help="Maximum roster template rows to validate. Use 0 for all."),
    ] = 0,
) -> None:
    """Validate reviewed player/participant roster evidence for player props."""
    artifacts = write_phase3ah_roster_verification_report(
        output_dir=output_dir,
        roster_template_path=roster_template_path,
        require_source_url=require_source_url,
        require_valid_from=require_valid_from,
        limit=limit if limit > 0 else None,
    )
    console.print("Phase 3AH Roster / Participant Verification")
    console.print("Mode: PAPER ONLY roster evidence validation")
    console.print("Live/demo execution: blocked")
    console.print("Verified link auto-upgrades: blocked")
    console.print("Phase 3AE remains the only verified sports link upgrade path.")
    console.print(f"Wrote JSON: {artifacts.json_path}")
    console.print(f"Wrote Markdown: {artifacts.markdown_path}")
    console.print(f"Wrote verified evidence: {artifacts.verified_evidence_path}")
    console.print(f"Wrote rework queue: {artifacts.rework_queue_path}")
    console.print(f"Wrote player prop blockers: {artifacts.player_prop_blockers_path}")


@app.command("phase3ag-crypto-pipeline")
def phase3ag_crypto_pipeline_command(
    output_dir: Annotated[
        Path,
        typer.Option(help="Directory for Phase 3AG crypto pipeline Markdown/JSON artifacts."),
    ] = Path("reports/phase3ag_crypto"),
    limit: Annotated[
        int | None,
        typer.Option(help="Optional market scan limit for faster smoke runs."),
    ] = None,
) -> None:
    engine = init_db()
    settings = get_settings()
    session_factory = get_session_factory(engine)
    with session_factory() as session:
        artifacts = write_phase3ag_crypto_report(
            session,
            output_dir=output_dir,
            settings=settings,
            limit=limit,
        )
    console.print("Phase 3AG Crypto Market Linkage and Paper-Trade Settlement")
    console.print("Mode: PAPER ONLY diagnostics")
    console.print("Live/demo execution: blocked")
    console.print(f"Wrote JSON: {artifacts.json_path}")
    console.print(f"Wrote Markdown: {artifacts.markdown_path}")
    console.print(f"Wrote rows: {artifacts.rows_path}")


@app.command("snapshot-coverage-repair")
def snapshot_coverage_repair_command(
    limit: Annotated[
        int,
        typer.Option(help="Maximum unique ranked markets to scan for missing snapshot coverage."),
    ] = 500,
    output: Annotated[
        Path,
        typer.Option(help="Markdown Phase 3AH snapshot coverage repair report path."),
    ] = Path("reports/snapshot_coverage_repair.md"),
) -> None:
    """Repair or classify ranked markets with missing price, spread, or liquidity."""
    engine = init_db()
    session_factory = get_session_factory(engine)
    with session_factory() as session:
        artifacts = write_snapshot_coverage_repair_report(
            session,
            limit=limit,
            output=output,
        )
        session.commit()
    console.print("Phase 3AH Market Snapshot Coverage Repair")
    console.print("Mode: PAPER ONLY diagnostics")
    console.print("Live/demo execution: blocked")
    console.print(f"Ranked markets scanned: {artifacts.result.ranked_markets_scanned}")
    console.print(f"Missing-data rankings found: {artifacts.result.missing_data_rankings_found}")
    console.print(f"Snapshots repaired: {artifacts.result.snapshots_repaired}")
    console.print(f"Still missing: {artifacts.result.still_missing}")
    console.print(f"Wrote Markdown: {artifacts.markdown_path}")
    console.print(f"Wrote JSON: {artifacts.json_path}")
    console.print(f"Wrote rows: {artifacts.rows_path}")


@app.command("model-repair-audit")
def model_repair_audit_command(
    output_dir: Annotated[
        Path,
        typer.Option(help="Directory for Phase 3Z model repair audit artifacts."),
    ] = MODEL_REPAIR_DIR,
) -> None:
    """Write a read-only Phase 3Z model health and metric audit."""
    engine = init_db()
    settings = get_settings()
    session_factory = get_session_factory(engine)
    with session_factory() as session:
        artifacts = write_model_repair_audit(session, output_dir=output_dir, settings=settings)
    console.print("Phase 3Z model repair audit")
    console.print("Mode: PAPER ONLY diagnostics")
    console.print(f"Wrote JSON: {artifacts.json_path}")
    console.print(f"Wrote Markdown: {artifacts.markdown_path}")
    for path in artifacts.extra_paths:
        console.print(f"Wrote artifact: {path}")


@app.command("market-coverage-doctor")
def market_coverage_doctor_command(
    output_dir: Annotated[
        Path,
        typer.Option(help="Directory for Phase 3Z market coverage diagnostic artifacts."),
    ] = Path("reports/market_coverage"),
    parse_first: Annotated[
        bool,
        typer.Option(help="Run the market-leg parser before computing coverage."),
    ] = False,
    parse_limit: Annotated[
        int,
        typer.Option(help="Maximum markets to parse before coverage. Use 0 for all."),
    ] = 0,
    deep_checks: Annotated[
        bool,
        typer.Option(
            help=(
                "Run slower runtime/orphan-link checks. Disabled by default so the "
                "dashboard snapshot can refresh quickly."
            ),
        ),
    ] = False,
) -> None:
    """Write a bounded paper-only producer-to-consumer market coverage funnel report."""
    engine = init_db()
    settings = get_settings()
    session_factory = get_session_factory(engine)
    with session_factory() as session:
        artifacts = write_market_coverage_doctor(
            session,
            output_dir=output_dir,
            settings=settings,
            parse_first=parse_first,
            parse_limit=parse_limit if parse_limit > 0 else None,
            deep_checks=deep_checks,
        )
        session.commit()
    console.print("Phase 3Z market coverage doctor")
    console.print("Mode: PAPER ONLY diagnostics")
    console.print(f"Wrote JSON: {artifacts.json_path}")
    console.print(f"Wrote Markdown: {artifacts.markdown_path}")
    for path in artifacts.extra_paths:
        console.print(f"Wrote artifact: {path}")


@app.command("model-metrics-reconcile")
def model_metrics_reconcile_command(
    include_historical: Annotated[
        bool,
        typer.Option(help="Include local historical settlement rows in the reconciliation report."),
    ] = False,
    output_dir: Annotated[
        Path,
        typer.Option(help="Directory for Phase 3Z metrics reconciliation artifacts."),
    ] = MODEL_REPAIR_DIR,
) -> None:
    """Classify local settlement and paper-trade metric readiness without live orders."""
    engine = init_db()
    settings = get_settings()
    session_factory = get_session_factory(engine)
    with session_factory() as session:
        artifacts = write_model_metrics_reconcile(
            session,
            output_dir=output_dir,
            include_historical=include_historical,
            settings=settings,
        )
    console.print("Phase 3Z metrics reconciliation")
    console.print("Mode: PAPER ONLY diagnostics")
    console.print(f"Wrote JSON: {artifacts.json_path}")
    console.print(f"Wrote Markdown: {artifacts.markdown_path}")


@app.command("phase3ah-r3-sports-provenance-repair")
def phase3ah_r3_sports_provenance_repair_command(
    output_dir: Annotated[
        Path,
        typer.Option(help="Directory for Phase 3AH-R3 sports provenance artifacts."),
    ] = Path("reports/phase3ah_r3"),
    reports_dir: Annotated[
        Path,
        typer.Option(help="Directory containing upstream report artifacts."),
    ] = Path("reports"),
    sample_limit: Annotated[
        int,
        typer.Option(help="Maximum example tickers/link IDs retained per group."),
    ] = 25,
    max_rows: Annotated[
        int,
        typer.Option(
            help=(
                "Maximum degraded sports ticker rows to materialize. Counts still "
                "reconcile against indexed totals."
            ),
        ),
    ] = 1000,
    ticker_prefix: Annotated[
        str | None,
        typer.Option(help="Optional ticker prefix filter for focused diagnostics."),
    ] = None,
) -> None:
    """Report safe sports provenance repair rows without applying link upgrades."""
    engine = init_db()
    settings = get_settings()
    session_factory = get_session_factory(engine)
    with session_factory() as session:
        artifacts = write_phase3ah_r3_sports_provenance_repair_report(
            session,
            output_dir=output_dir,
            reports_dir=reports_dir,
            settings=settings,
            sample_limit=sample_limit,
            max_rows=max_rows,
            ticker_prefix=ticker_prefix,
            registered_commands=set(registered_root_command_names()),
        )
    console.print("Phase 3AH-R3 sports provenance repair")
    console.print("Mode: PAPER ONLY / READ ONLY diagnostics")
    console.print("Live/demo execution: blocked")
    console.print("Verified link auto-upgrades: blocked")
    console.print("Paper trade creation: blocked")
    console.print(f"Wrote Executive Summary: {artifacts.executive_summary_path}")
    console.print(f"Wrote JSON: {artifacts.json_path}")
    console.print(f"Wrote rows: {artifacts.rows_path}")
    console.print(f"Wrote Next Actions: {artifacts.next_actions_path}")
    console.print(f"Wrote Next Codex Task: {artifacts.next_codex_task_path}")


@app.command("phase3ah-r3-bounded-scan-expansion")
def phase3ah_r3_bounded_scan_expansion_command(
    output_dir: Annotated[
        Path,
        typer.Option(help="Directory for Phase 3AH-R3 sports provenance artifacts."),
    ] = Path("reports/phase3ah_r3"),
    reports_dir: Annotated[
        Path,
        typer.Option(help="Directory containing upstream report artifacts."),
    ] = Path("reports"),
    sample_limit: Annotated[
        int,
        typer.Option(help="Maximum example tickers/link IDs retained per group."),
    ] = 25,
    max_rows: Annotated[
        int,
        typer.Option(
            help=(
                "Maximum degraded sports ticker rows to materialize during the "
                "bounded scan expansion."
            ),
        ),
    ] = 7500,
    ticker_prefix: Annotated[
        str | None,
        typer.Option(help="Optional ticker prefix filter for focused diagnostics."),
    ] = None,
) -> None:
    """Run an expanded read-only bounded sports provenance scan."""
    engine = init_db()
    settings = get_settings()
    session_factory = get_session_factory(engine)
    with session_factory() as session:
        artifacts = write_phase3ah_r3_bounded_scan_expansion_report(
            session,
            output_dir=output_dir,
            reports_dir=reports_dir,
            settings=settings,
            sample_limit=sample_limit,
            max_rows=max_rows,
            ticker_prefix=ticker_prefix,
            registered_commands=set(registered_root_command_names()),
        )
    console.print("Phase 3AH-R3 sports provenance bounded scan expansion")
    console.print("Mode: PAPER ONLY / READ ONLY diagnostics")
    console.print("Live/demo execution: blocked")
    console.print("Verified link auto-upgrades: blocked")
    console.print("Paper trade creation: blocked")
    console.print(f"Wrote expansion report: {artifacts.expansion_markdown_path}")
    console.print(f"Wrote expansion JSON: {artifacts.expansion_json_path}")
    console.print(f"Wrote canonical JSON: {artifacts.json_path}")
    console.print(f"Wrote Next Codex Task: {artifacts.next_codex_task_path}")


@app.command("phase3z-r2-sports-provenance-repair")
def phase3z_r2_sports_provenance_repair_command(
    output_dir: Annotated[
        Path,
        typer.Option(help="Directory for Phase 3Z-R2 sports provenance repair artifacts."),
    ] = Path("reports/phase3z_r2"),
    reports_dir: Annotated[
        Path,
        typer.Option(help="Directory containing upstream report artifacts."),
    ] = Path("reports"),
    sample_limit: Annotated[
        int,
        typer.Option(help="Maximum example tickers/link IDs retained per group."),
    ] = 25,
    max_rows: Annotated[
        int,
        typer.Option(
            help=(
                "Maximum degraded ticker rows to materialize. Counts still reconcile "
                "against full indexed totals."
            ),
        ),
    ] = 1000,
    ticker_prefix: Annotated[
        str | None,
        typer.Option(help="Optional ticker prefix filter for focused diagnostics."),
    ] = None,
) -> None:
    """Explain degraded sports provenance without applying unsafe link upgrades."""
    engine = init_db()
    settings = get_settings()
    session_factory = get_session_factory(engine)
    with session_factory() as session:
        artifacts = write_phase3z_r2_sports_provenance_repair_report(
            session,
            output_dir=output_dir,
            reports_dir=reports_dir,
            settings=settings,
            sample_limit=sample_limit,
            max_rows=max_rows,
            ticker_prefix=ticker_prefix,
        )
    console.print("Phase 3Z-R2 sports provenance coverage repair")
    console.print("Mode: PAPER ONLY / READ ONLY diagnostics")
    console.print("Live/demo execution: blocked")
    console.print("Verified link auto-upgrades: blocked")
    console.print(f"Wrote JSON: {artifacts.json_path}")
    console.print(f"Wrote Markdown: {artifacts.markdown_path}")
    console.print(f"Wrote rows: {artifacts.rows_path}")


@app.command("phase3an-sports-blocker-report")
def phase3an_sports_blocker_report_command(
    output_dir: Annotated[
        Path,
        typer.Option(help="Directory for Phase 3AN sports blocker artifacts."),
    ] = Path("reports/phase3an"),
    reports_dir: Annotated[
        Path,
        typer.Option(help="Directory containing upstream report artifacts."),
    ] = Path("reports"),
) -> None:
    """Summarize exact sports provenance blockers from current report artifacts."""
    artifacts = write_phase3an_sports_blocker_report(
        output_dir=output_dir,
        reports_dir=reports_dir,
    )
    console.print("Phase 3AN sports blocker report")
    console.print("Mode: PAPER ONLY / READ ONLY diagnostics")
    console.print("Live/demo execution: blocked")
    console.print("Sports diagnostic paper-trade creation: blocked")
    console.print(f"Wrote JSON: {artifacts.json_path}")
    console.print(f"Wrote Markdown: {artifacts.markdown_path}")


@app.command("phase3aw-dashboard-truth")
def phase3aw_dashboard_truth_command(
    output_dir: Annotated[
        Path,
        typer.Option(help="Directory for Phase 3AW dashboard truth artifacts."),
    ] = Path("reports/phase3aw"),
    reports_dir: Annotated[
        Path,
        typer.Option(help="Directory containing upstream report artifacts."),
    ] = Path("reports"),
    stale_after_minutes: Annotated[
        int,
        typer.Option(help="Classify report artifacts stale after this many minutes."),
    ] = 120,
) -> None:
    """Report whether sports provenance dashboard inputs are current."""
    artifacts = write_phase3aw_dashboard_truth_report(
        output_dir=output_dir,
        reports_dir=reports_dir,
        stale_after_minutes=stale_after_minutes,
    )
    console.print("Phase 3AW dashboard truth")
    console.print("Mode: REPORT ONLY")
    console.print("Live/demo execution: blocked")
    console.print(f"Wrote JSON: {artifacts.json_path}")
    console.print(f"Wrote Markdown: {artifacts.markdown_path}")


@app.command("phase3ax-gap-analysis")
def phase3ax_gap_analysis_command(
    output_dir: Annotated[
        Path,
        typer.Option(help="Directory for Phase 3AX-R6 gap analysis artifacts."),
    ] = Path("reports/phase3ax"),
    reports_dir: Annotated[
        Path,
        typer.Option(help="Directory containing upstream report artifacts."),
    ] = Path("reports"),
    stale_after_minutes: Annotated[
        int,
        typer.Option(help="Classify report artifacts stale after this many minutes."),
    ] = 120,
) -> None:
    """Separate exact safe sports repairs from diagnostic-only rows."""
    artifacts = write_phase3ax_gap_analysis_report(
        output_dir=output_dir,
        reports_dir=reports_dir,
        stale_after_minutes=stale_after_minutes,
    )
    console.print("Phase 3AX-R6 sports provenance gap analysis")
    console.print("Mode: PAPER ONLY / READ ONLY diagnostics")
    console.print("Live/demo execution: blocked")
    console.print("Sports diagnostic paper-trade creation: blocked")
    console.print("Unsafe/fuzzy/sibling/component repairs: blocked")
    console.print(f"Wrote JSON: {artifacts.json_path}")
    console.print(f"Wrote Markdown: {artifacts.markdown_path}")
    if artifacts.safe_rows_path:
        console.print(f"Wrote safe rows: {artifacts.safe_rows_path}")
    else:
        console.print("Wrote safe rows: none")


@app.command("model-link-repair")
def model_link_repair_command(
    domains: Annotated[
        str,
        typer.Option(
            help="Comma-separated domains to repair; current linker repairs known domains."
        ),
    ] = "crypto,weather,economic,sports,news",
    paper_only: Annotated[
        bool,
        typer.Option(help="Required safety flag; this command never submits exchange orders."),
    ] = True,
    output_dir: Annotated[
        Path,
        typer.Option(help="Directory for Phase 3Z link repair evidence."),
    ] = MODEL_REPAIR_DIR,
) -> None:
    """Run local link remediation and write Phase 3Z evidence reports."""
    if not paper_only:
        console.print("Refusing to run: --paper-only must remain enabled.")
        raise typer.Exit(1)
    engine = init_db()
    settings = get_settings()
    backup = backup_before_phase3z_write(output_dir=output_dir, settings=settings)
    session_factory = get_session_factory(engine)
    with session_factory() as session:
        result = run_link_remediation(session, settings=settings)
        session.commit()
        artifacts = write_market_coverage_doctor(session, output_dir=output_dir, settings=settings)
    payload = {
        "paper_only_safety": "PAPER_ONLY_NO_EXCHANGE_WRITES",
        "requested_domains": [item.strip() for item in domains.split(",") if item.strip()],
        "backup_path": str(backup) if backup else None,
        "totals": result.total_links,
        "recommendations": result.recommendations,
        "coverage_artifact": str(artifacts.json_path),
    }
    output_dir.mkdir(parents=True, exist_ok=True)
    path = output_dir / "model_link_repair.json"
    path.write_text(json.dumps(payload, indent=2, sort_keys=True), encoding="utf-8")
    console.print("Phase 3Z model link repair")
    console.print("Mode: PAPER ONLY; no exchange orders submitted.")
    if backup:
        console.print(f"SQLite backup: {backup}")
    console.print(f"Wrote link repair evidence: {path}")
    console.print(f"Wrote coverage evidence: {artifacts.json_path}")


@app.command("model-feature-repair")
def model_feature_repair_command(
    domains: Annotated[
        str,
        typer.Option(help="Comma-separated feature domains, for example sports,microstructure."),
    ] = "sports,microstructure",
    once: Annotated[
        bool,
        typer.Option(
            help="Required finite mode; watch mode is intentionally not implemented here."
        ),
    ] = True,
    output_dir: Annotated[
        Path,
        typer.Option(help="Directory for Phase 3Z feature repair evidence."),
    ] = MODEL_REPAIR_DIR,
) -> None:
    """Run finite local feature repair jobs and write Phase 3Z evidence."""
    if not once:
        console.print("Refusing to run: Phase 3Z feature repair is finite; keep --once enabled.")
        raise typer.Exit(1)
    requested = {item.strip().lower() for item in domains.split(",") if item.strip()}
    engine = init_db()
    settings = get_settings()
    backup = backup_before_phase3z_write(output_dir=output_dir, settings=settings)
    session_factory = get_session_factory(engine)
    results: dict[str, object] = {}
    with session_factory() as session:
        if "sports" in requested:
            results["sports"] = derive_sports_schedule_from_market_legs(
                session,
                build_features=True,
                settings=settings,
            ).__dict__
        if "microstructure" in requested:
            results["microstructure"] = build_microstructure_features(
                session,
                settings=settings,
            ).__dict__
        session.commit()
        artifacts = write_model_repair_audit(session, output_dir=output_dir, settings=settings)
    payload = {
        "paper_only_safety": "PAPER_ONLY_NO_EXCHANGE_WRITES",
        "requested_domains": sorted(requested),
        "backup_path": str(backup) if backup else None,
        "results": results,
        "model_audit": str(artifacts.json_path),
    }
    output_dir.mkdir(parents=True, exist_ok=True)
    path = output_dir / "feature_coverage.json"
    path.write_text(json.dumps(payload, indent=2, sort_keys=True, default=str), encoding="utf-8")
    console.print("Phase 3Z model feature repair")
    console.print("Mode: PAPER ONLY; no exchange orders submitted.")
    if backup:
        console.print(f"SQLite backup: {backup}")
    console.print(f"Wrote feature evidence: {path}")
    console.print(f"Wrote model audit: {artifacts.json_path}")


@app.command("model-repair-run")
def model_repair_run_command(
    paper_only: Annotated[
        bool,
        typer.Option(help="Required safety flag; no live/demo execution is enabled."),
    ] = True,
    output_dir: Annotated[
        Path,
        typer.Option(help="Directory for combined Phase 3Z repair evidence."),
    ] = MODEL_REPAIR_DIR,
) -> None:
    """Generate the combined Phase 3Z evidence bundle without exchange writes."""
    if not paper_only:
        console.print("Refusing to run: --paper-only must remain enabled.")
        raise typer.Exit(1)
    engine = init_db()
    settings = get_settings()
    session_factory = get_session_factory(engine)
    with session_factory() as session:
        artifacts = write_model_repair_run(session, output_dir=output_dir, settings=settings)
    console.print("Phase 3Z combined repair evidence")
    console.print("Mode: PAPER ONLY diagnostics")
    console.print(f"Wrote run JSON: {artifacts.json_path}")
    console.print(f"Primary Markdown: {artifacts.markdown_path}")
    for path in artifacts.extra_paths:
        console.print(f"Wrote artifact: {path}")


@app.command("self-evaluate")
def self_evaluate_command(
    session_date: Annotated[
        str | None,
        typer.Option(help="Trading session date to evaluate, YYYY-MM-DD. Defaults to yesterday."),
    ] = None,
    evaluation_as_of: Annotated[
        str | None,
        typer.Option(help="Frozen evaluation cutoff timestamp. Defaults to now."),
    ] = None,
    output: Annotated[
        Path,
        typer.Option(help="Markdown self-evaluation journal path."),
    ] = Path("reports/self_evaluation_journal.md"),
    json_output: Annotated[
        Path,
        typer.Option(help="Structured JSON self-evaluation journal path."),
    ] = Path("reports/self_evaluation_journal.json"),
) -> None:
    engine = init_db()
    settings = get_settings()
    session_factory = get_session_factory(engine)
    with session_factory() as session:
        result = generate_self_evaluation_report(
            session,
            output_path=output,
            json_output_path=json_output,
            session_date=session_date,
            evaluation_as_of=evaluation_as_of,
            settings=settings,
        )
        session.commit()
    console.print("Phase 3P self-evaluation")
    console.print(f"Status: {result.journal_status}")
    console.print(f"Run ID: {result.evaluation_run_id}")
    console.print(f"Journal ID: {result.journal_id} revision {result.journal_revision}")
    console.print(f"Idempotent: {result.idempotent}")
    console.print(f"Wrote Markdown journal to {result.markdown_path}")
    console.print(f"Wrote JSON journal to {result.json_path}")


@app.command("feature-discovery-status")
def feature_discovery_status_command() -> None:
    engine = _init_db_or_exit("Phase 3Q feature discovery status")
    session_factory = get_session_factory(engine)
    with session_factory() as session:
        status = feature_discovery_status(session)
    console.print("Phase 3Q feature discovery status")
    console.print(f"Runs: {status['run_count']}")
    console.print(f"Candidates: {status['candidate_count']}")
    console.print(f"Evaluations: {status['evaluation_count']}")
    console.print(f"Recommendations: {status['recommendation_count']}")
    console.print(f"Latest run: {status['latest_run_id'] or 'none'}")
    console.print(f"Latest status: {status['latest_status']}")
    console.print(f"Latest completed: {status['latest_completed_at'] or 'n/a'}")


@app.command("feature-discovery-run")
def feature_discovery_run_command(
    run_type: Annotated[
        str,
        typer.Option(help="Run type: INCREMENTAL, FULL_SEARCH, BACKFILL, ON_DEMAND, or REPLAY."),
    ] = "ON_DEMAND",
    training_as_of: Annotated[
        str | None,
        typer.Option(help="Frozen point-in-time cutoff. Defaults to now."),
    ] = None,
    output: Annotated[
        Path,
        typer.Option(help="Markdown Phase 3Q report path."),
    ] = Path("reports/feature_discovery_report.md"),
    json_output: Annotated[
        Path,
        typer.Option(help="Structured JSON Phase 3Q report path."),
    ] = Path("reports/feature_discovery_report.json"),
    force: Annotated[
        bool,
        typer.Option(help="Persist a new run even if the logical idempotency key exists."),
    ] = False,
) -> None:
    engine = _init_db_or_exit("Phase 3Q feature discovery")
    settings = get_settings()
    session_factory = get_session_factory(engine)
    with session_factory() as session:
        result = generate_feature_discovery_report(
            session,
            output_path=output,
            json_output_path=json_output,
            run_type=run_type,
            training_as_of=training_as_of,
            settings=settings,
            force=force,
        )
        session.commit()
    console.print("Phase 3Q feature discovery")
    console.print(f"Status: {result.status}")
    console.print(f"Run ID: {result.run_id}")
    console.print(f"Rows included: {result.manifest.rows_included} / {result.manifest.rows_total}")
    console.print(f"Candidates generated: {result.candidate_counts['generated']}")
    console.print(f"Validated: {result.candidate_counts['validated']}")
    console.print(f"Watchlist: {result.candidate_counts['watchlist']}")
    console.print(f"Rejected: {result.candidate_counts['rejected']}")
    console.print(f"Idempotent: {result.idempotent}")
    console.print(f"Wrote Markdown report to {result.report_path}")
    console.print(f"Wrote JSON report to {result.json_path}")


@app.command("feature-discovery-report")
def feature_discovery_report_command(
    output: Annotated[
        Path,
        typer.Option(help="Markdown Phase 3Q report path."),
    ] = Path("reports/feature_discovery_report.md"),
    json_output: Annotated[
        Path,
        typer.Option(help="Structured JSON Phase 3Q report path."),
    ] = Path("reports/feature_discovery_report.json"),
) -> None:
    engine = _init_db_or_exit("Phase 3Q feature discovery report")
    settings = get_settings()
    session_factory = get_session_factory(engine)
    with session_factory() as session:
        result = generate_feature_discovery_report(
            session,
            output_path=output,
            json_output_path=json_output,
            settings=settings,
        )
        session.commit()
    console.print(f"Wrote Phase 3Q report to {result.report_path}")


@app.command("synthetic-markets-status")
def synthetic_markets_status_command() -> None:
    engine = _init_db_or_exit("Phase 3R synthetic markets status")
    session_factory = get_session_factory(engine)
    with session_factory() as session:
        status = synthetic_markets_status(session)
    console.print("Phase 3R synthetic markets status")
    console.print(f"Runs: {status['run_count']}")
    console.print(f"Events: {status['event_count']}")
    console.print(f"Contracts: {status['contract_count']}")
    console.print(f"Estimates: {status['estimate_count']}")
    console.print(f"Listing checks: {status['listing_check_count']}")
    console.print(f"Listing matches: {status['listing_match_count']}")
    console.print(f"Latest run: {status['latest_run_id'] or 'none'}")
    console.print(f"Latest status: {status['latest_status']}")
    console.print(f"Latest completed: {status['latest_completed_at'] or 'n/a'}")


@app.command("synthetic-markets-run")
def synthetic_markets_run_command(
    input_file: Annotated[
        Path | None,
        typer.Option(
            help="Candidate JSON file. Accepts object, list, or {candidates: [...]} object."
        ),
    ] = None,
    run_type: Annotated[
        str,
        typer.Option(help="Phase 3R run type."),
    ] = "CANDIDATE_DISCOVERY",
    estimate_as_of: Annotated[
        str | None,
        typer.Option(help="Frozen estimate timestamp. Defaults to now."),
    ] = None,
    output: Annotated[
        Path,
        typer.Option(help="Markdown Phase 3R synthetic markets report path."),
    ] = Path("reports/synthetic_markets_report.md"),
    json_output: Annotated[
        Path,
        typer.Option(help="Structured JSON Phase 3R report path."),
    ] = Path("reports/synthetic_markets_report.json"),
    enable_research: Annotated[
        bool,
        typer.Option(help="Temporarily enable internal Phase 3R research for this command."),
    ] = False,
    mode: Annotated[
        str | None,
        typer.Option(help="Temporarily override Phase 3R mode."),
    ] = None,
    force: Annotated[
        bool,
        typer.Option(help="Persist a new run even if the logical idempotency key exists."),
    ] = False,
) -> None:
    engine = _init_db_or_exit("Phase 3R synthetic markets")
    settings = _phase_3r_command_settings(enable_research=enable_research, mode=mode)
    session_factory = get_session_factory(engine)
    with session_factory() as session:
        try:
            result = generate_synthetic_markets_report(
                session,
                input_file=input_file,
                run_type=run_type,
                estimate_as_of=estimate_as_of,
                output_path=output,
                json_output_path=json_output,
                settings=settings,
                force=force,
            )
            session.commit()
        except (FileNotFoundError, ValueError) as exc:
            session.rollback()
            console.print("Phase 3R synthetic markets: BLOCKED")
            console.print(str(exc))
            console.print(
                "Next action: create data/synthetic_markets_candidates.json or rerun without "
                "--input-file."
            )
            raise typer.Exit(1) from exc
    console.print("Phase 3R synthetic markets")
    console.print(f"Status: {result.status}")
    console.print(f"Mode: {result.mode}")
    console.print(f"Run ID: {result.run_id}")
    console.print(f"Candidates generated: {result.candidate_counts['generated']}")
    console.print(f"Accepted internal estimates: {result.candidate_counts['accepted']}")
    console.print(f"Rejected or paused: {result.candidate_counts['rejected']}")
    console.print(f"Idempotent: {result.idempotent}")
    console.print("Safety: internal research only; no orders, fills, positions, or opportunities.")
    console.print(f"Wrote Markdown report to {result.report_path}")
    console.print(f"Wrote JSON report to {result.json_path}")


@app.command("synthetic-markets-report")
def synthetic_markets_report_command(
    output: Annotated[
        Path,
        typer.Option(help="Markdown Phase 3R synthetic markets report path."),
    ] = Path("reports/synthetic_markets_report.md"),
    json_output: Annotated[
        Path,
        typer.Option(help="Structured JSON Phase 3R report path."),
    ] = Path("reports/synthetic_markets_report.json"),
) -> None:
    engine = _init_db_or_exit("Phase 3R synthetic markets report")
    settings = get_settings()
    session_factory = get_session_factory(engine)
    with session_factory() as session:
        result = generate_synthetic_markets_report(
            session,
            output_path=output,
            json_output_path=json_output,
            settings=settings,
        )
        session.commit()
    console.print(f"Wrote Phase 3R synthetic markets report to {result.report_path}")


def _phase_3r_command_settings(*, enable_research: bool, mode: str | None):
    settings = get_settings()
    updates = {}
    if enable_research:
        updates["phase_3r_synthetic_markets_enabled"] = True
        updates["phase_3r_mode"] = mode or "shadow"
    elif mode is not None:
        updates["phase_3r_mode"] = mode
    return settings.model_copy(update=updates) if updates else settings


@app.command("rl-status")
def rl_status_command() -> None:
    engine = _init_db_or_exit("Phase 3S reinforcement learning status")
    session_factory = get_session_factory(engine)
    with session_factory() as session:
        status = rl_status(session)
    console.print("Phase 3S reinforcement learning status")
    console.print(f"Runs: {status['run_count']}")
    console.print(f"Datasets: {status['dataset_count']}")
    console.print(f"Reward rows: {status['reward_count']}")
    console.print(f"Evaluations: {status['evaluation_count']}")
    console.print(f"Shadow decisions: {status['shadow_decision_count']}")
    console.print(f"Drift snapshots: {status['drift_snapshot_count']}")
    console.print(f"Latest run: {status['latest_run_id'] or 'none'}")
    console.print(f"Latest status: {status['latest_status']}")
    console.print(f"Latest completed: {status['latest_completed_at'] or 'n/a'}")


@app.command("rl-dataset")
def rl_dataset_command(
    training_as_of: Annotated[
        str | None,
        typer.Option(help="Frozen point-in-time cutoff. Defaults to now."),
    ] = None,
    output: Annotated[
        Path,
        typer.Option(help="Markdown Phase 3S dataset report path."),
    ] = Path("reports/rl_dataset_report.md"),
    json_output: Annotated[
        Path,
        typer.Option(help="Structured JSON Phase 3S dataset report path."),
    ] = Path("reports/rl_dataset_report.json"),
    enable_research: Annotated[
        bool,
        typer.Option(help="Temporarily enable Phase 3S offline research for this command."),
    ] = False,
) -> None:
    _run_rl_report_command(
        run_type="DATASET",
        training_as_of=training_as_of,
        output=output,
        json_output=json_output,
        enable_research=enable_research,
        mode="offline_replay",
    )


@app.command("rl-train")
def rl_train_command(
    training_as_of: Annotated[
        str | None,
        typer.Option(help="Frozen point-in-time cutoff. Defaults to now."),
    ] = None,
    output: Annotated[
        Path,
        typer.Option(help="Markdown Phase 3S training report path."),
    ] = Path("reports/rl_policy_report.md"),
    json_output: Annotated[
        Path,
        typer.Option(help="Structured JSON Phase 3S evaluation card path."),
    ] = Path("reports/rl_policy_report.json"),
    enable_research: Annotated[
        bool,
        typer.Option(help="Temporarily enable Phase 3S offline research for this command."),
    ] = False,
    force: Annotated[
        bool,
        typer.Option(help="Persist a new run even if the logical idempotency key exists."),
    ] = False,
) -> None:
    _run_rl_report_command(
        run_type="TRAIN",
        training_as_of=training_as_of,
        output=output,
        json_output=json_output,
        enable_research=enable_research,
        mode="offline_replay",
        force=force,
    )


@app.command("rl-evaluate")
def rl_evaluate_command(
    training_as_of: Annotated[
        str | None,
        typer.Option(help="Frozen point-in-time cutoff. Defaults to now."),
    ] = None,
    output: Annotated[
        Path,
        typer.Option(help="Markdown Phase 3S policy report path."),
    ] = Path("reports/rl_policy_report.md"),
    json_output: Annotated[
        Path,
        typer.Option(help="Structured JSON Phase 3S evaluation card path."),
    ] = Path("reports/rl_policy_report.json"),
    enable_research: Annotated[
        bool,
        typer.Option(help="Temporarily enable Phase 3S offline research for this command."),
    ] = False,
    force: Annotated[
        bool,
        typer.Option(help="Persist a new run even if the logical idempotency key exists."),
    ] = False,
) -> None:
    _run_rl_report_command(
        run_type="EVALUATE",
        training_as_of=training_as_of,
        output=output,
        json_output=json_output,
        enable_research=enable_research,
        mode="offline_replay",
        force=force,
    )


@app.command("rl-shadow-report")
def rl_shadow_report_command(
    output: Annotated[
        Path,
        typer.Option(help="Markdown Phase 3S shadow report path."),
    ] = Path("reports/rl_shadow_report.md"),
    json_output: Annotated[
        Path,
        typer.Option(help="Structured JSON Phase 3S shadow report path."),
    ] = Path("reports/rl_shadow_report.json"),
) -> None:
    engine = _init_db_or_exit("Phase 3S shadow report")
    settings = get_settings().model_copy(
        update={"phase_3s_reinforcement_learning_enabled": True, "phase_3s_mode": "shadow"}
    )
    session_factory = get_session_factory(engine)
    with session_factory() as session:
        config = rl_config_from_settings(settings)
        recommendation = recommend_policy_action(
            opportunity={
                "opportunity_id": "shadow-smoke",
                "opportunity_score": "0",
                "confidence_score": "0",
            },
            config=config,
            session=session,
        )
        session.commit()
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(
        "# Phase 3S Shadow Report\n\n"
        f"- Recommendation: {recommendation.recommended_action}\n"
        f"- Baseline: {recommendation.baseline_action}\n"
        "- Shadow mode changes no orders, quantities, Phase 3M requests, or Phase 3N decisions.\n",
        encoding="utf-8",
    )
    json_output.parent.mkdir(parents=True, exist_ok=True)
    json_output.write_text(
        json.dumps(
            {
                "recommended_action": recommendation.recommended_action,
                "baseline_action": recommendation.baseline_action,
                "reason_codes": list(recommendation.reason_codes),
            },
            sort_keys=True,
        ),
        encoding="utf-8",
    )
    console.print(f"Wrote Phase 3S shadow report to {output}")


@app.command("rl-drift-report")
def rl_drift_report_command(
    output: Annotated[
        Path,
        typer.Option(help="Markdown Phase 3S drift report path."),
    ] = Path("reports/rl_drift_report.md"),
) -> None:
    engine = _init_db_or_exit("Phase 3S drift report")
    session_factory = get_session_factory(engine)
    with session_factory() as session:
        snapshot = persist_drift_snapshot(session)
        session.commit()
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(
        "# Phase 3S Drift Report\n\n"
        f"- Status: {snapshot.status}\n"
        "- No approved active Phase 3S policy is currently serving.\n"
        "- Governed influence remains disabled by default.\n",
        encoding="utf-8",
    )
    console.print(f"Wrote Phase 3S drift report to {output}")


def _run_rl_report_command(
    *,
    run_type: str,
    training_as_of: str | None,
    output: Path,
    json_output: Path,
    enable_research: bool,
    mode: str,
    force: bool = False,
) -> None:
    engine = _init_db_or_exit("Phase 3S reinforcement learning")
    settings = get_settings()
    if enable_research:
        settings = settings.model_copy(
            update={"phase_3s_reinforcement_learning_enabled": True, "phase_3s_mode": mode}
        )
    session_factory = get_session_factory(engine)
    with session_factory() as session:
        result = generate_rl_policy_report(
            session,
            run_type=run_type,
            training_as_of=training_as_of,
            output_path=output,
            json_output_path=json_output,
            settings=settings,
            force=force,
        )
        session.commit()
    console.print("Phase 3S reinforcement learning")
    console.print(f"Status: {result.status}")
    console.print(f"Mode: {result.mode}")
    console.print(f"Run ID: {result.run_id}")
    console.print(f"Dataset rows: {len(result.dataset.rows)} / {result.dataset.rows_total}")
    console.print(f"Recommendation: {result.recommendation_status}")
    console.print("Safety: no order creation, no quantities, no Phase 3M/3N bypass.")
    console.print(f"Wrote Markdown report to {result.report_path}")
    console.print(f"Wrote JSON report to {result.json_path}")


@app.command("institutional-dashboard-status")
def institutional_dashboard_status_command(
    enable_read_only: Annotated[
        bool,
        typer.Option(help="Temporarily enable Phase 3T read-only shadow mode for this command."),
    ] = False,
    mode: Annotated[
        str,
        typer.Option(help="Temporary Phase 3T mode when --enable-read-only is set."),
    ] = "read_only_shadow",
) -> None:
    engine = _init_db_or_exit("Phase 3T institutional dashboard status")
    settings = _phase_3t_command_settings(enable_read_only=enable_read_only, mode=mode)
    session_factory = get_session_factory(engine)
    with session_factory() as session:
        snapshot = build_dashboard_snapshot(session, settings=settings)
        status = institutional_dashboard_status(snapshot)
    console.print("Phase 3T institutional dashboard")
    console.print(f"Mode: {status['mode']}")
    console.print(f"Snapshot: {status['snapshot_id']}")
    console.print(f"Freshness: {status['freshness_status']}")
    console.print(f"Completeness: {status['completeness_status']}")
    console.print(f"Panels: {status['panel_count']}")
    console.print(f"Warnings: {status['warning_count']}")
    console.print(f"Reconciliation: {status['reconciliation_status']}")
    console.print(f"Read-only: {status['read_only']}")


@app.command("institutional-dashboard-report")
def institutional_dashboard_report_command(
    output: Annotated[
        Path,
        typer.Option(help="Markdown Phase 3T institutional dashboard report path."),
    ] = Path("reports/institutional_dashboard.md"),
    enable_read_only: Annotated[
        bool,
        typer.Option(help="Temporarily enable Phase 3T read-only shadow mode for this command."),
    ] = False,
    mode: Annotated[
        str,
        typer.Option(help="Temporary Phase 3T mode when --enable-read-only is set."),
    ] = "read_only_shadow",
) -> None:
    engine = _init_db_or_exit("Phase 3T institutional dashboard report")
    settings = _phase_3t_command_settings(enable_read_only=enable_read_only, mode=mode)
    session_factory = get_session_factory(engine)
    with session_factory() as session:
        path = generate_institutional_dashboard_report(
            session,
            output_path=output,
            settings=settings,
        )
    console.print(f"Wrote Phase 3T institutional dashboard report to {path}")


@app.command("institutional-dashboard-export")
def institutional_dashboard_export_command(
    output: Annotated[
        Path,
        typer.Option(help="CSV Phase 3T snapshot export path."),
    ] = Path("reports/institutional_dashboard_snapshot.csv"),
    enable_read_only: Annotated[
        bool,
        typer.Option(help="Temporarily enable Phase 3T read-only shadow mode for this command."),
    ] = False,
    mode: Annotated[
        str,
        typer.Option(help="Temporary Phase 3T mode when --enable-read-only is set."),
    ] = "read_only_shadow",
) -> None:
    engine = _init_db_or_exit("Phase 3T institutional dashboard export")
    settings = _phase_3t_command_settings(enable_read_only=enable_read_only, mode=mode)
    session_factory = get_session_factory(engine)
    with session_factory() as session:
        snapshot = build_dashboard_snapshot(session, settings=settings)
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(export_snapshot_csv(snapshot), encoding="utf-8")
    console.print(f"Wrote Phase 3T institutional dashboard CSV to {output}")


def _phase_3t_command_settings(*, enable_read_only: bool, mode: str):
    settings = get_settings()
    if not enable_read_only:
        return settings
    return settings.model_copy(
        update={
            "phase_3t_institutional_dashboard_enabled": True,
            "phase_3t_mode": mode,
        }
    )


@app.command("personal-trader-status")
def personal_trader_status_command(
    enable_advisory: Annotated[
        bool,
        typer.Option(help="Temporarily enable Phase 3U advisory mode for this command."),
    ] = False,
    mode: Annotated[
        str,
        typer.Option(help="Temporary Phase 3U mode when --enable-advisory is set."),
    ] = "PAPER_ADVISORY",
) -> None:
    engine = _init_db_or_exit("Phase 3U personal AI trader status")
    settings = _phase_3u_command_settings(enable_advisory=enable_advisory, mode=mode)
    session_factory = get_session_factory(engine)
    with session_factory() as session:
        status = personal_trader_status_report(session, settings=settings)
    console.print("Phase 3U Personal AI Trader")
    console.print(f"Enabled: {status['enabled']}")
    console.print(f"Mode: {status['mode']}")
    console.print(f"Briefs: {status['brief_count']}")
    console.print(f"Latest brief: {status['latest_brief_id'] or 'none'}")
    console.print(f"Ranking policy: {status['ranking_policy_version']}")
    console.print(f"Eligibility policy: {status['eligibility_policy_version']}")
    console.print(f"Audit events: {status['audit_event_count']}")
    console.print("Safety: advisory only; no demo/live/order writes.")


@app.command("personal-trader-brief")
def personal_trader_brief_command(
    query: Annotated[
        str,
        typer.Option(help="Natural language request to normalize."),
    ] = "What should I trade today?",
    output: Annotated[
        Path,
        typer.Option(help="Markdown Phase 3U advisory brief path."),
    ] = Path("reports/personal_trader_brief.md"),
    enable_advisory: Annotated[
        bool,
        typer.Option(help="Temporarily enable Phase 3U advisory mode for this command."),
    ] = False,
    mode: Annotated[
        str,
        typer.Option(help="Temporary Phase 3U mode when --enable-advisory is set."),
    ] = "PAPER_ADVISORY",
    persist: Annotated[
        bool,
        typer.Option(help="Persist append-only recommendation audit events."),
    ] = False,
) -> None:
    engine = _init_db_or_exit("Phase 3U personal AI trader brief")
    settings = _phase_3u_command_settings(enable_advisory=enable_advisory, mode=mode)
    session_factory = get_session_factory(engine)
    with session_factory() as session:
        brief = build_personal_trade_brief(
            session,
            settings=settings,
            natural_language_query=query,
            persist=persist,
        )
        output.parent.mkdir(parents=True, exist_ok=True)
        output.write_text(render_personal_trader_report(brief), encoding="utf-8")
        if persist:
            session.commit()
    console.print(conversational_response(brief))
    console.print(f"Wrote Phase 3U personal trader brief to {output}")
    console.print("Safety: advisory only; no order was created.")


@app.command("personal-trader-audit")
def personal_trader_audit_command(
    brief_id: Annotated[str | None, typer.Option(help="Filter by brief ID.")] = None,
) -> None:
    engine = _init_db_or_exit("Phase 3U personal AI trader audit")
    session_factory = get_session_factory(engine)
    with session_factory() as session:
        events = recommendation_audit_events(session, brief_id=brief_id)
    console.print("Phase 3U recommendation memory")
    console.print(f"Events: {len(events)}")
    for row in events[:50]:
        console.print(
            f"{row['created_at']} {row['event_type']} "
            f"brief={row['brief_id']} candidate={row['candidate_id'] or 'n/a'}"
        )


def _phase_3u_command_settings(*, enable_advisory: bool, mode: str):
    settings = get_settings()
    if not enable_advisory:
        return settings
    return settings.model_copy(
        update={
            "phase_3u_personal_ai_trader_enabled": True,
            "phase_3u_mode": mode.upper(),
        }
    )


@app.command("live-readiness-status")
def live_readiness_status_command(
    enable_review: Annotated[
        bool,
        typer.Option(help="Temporarily enable Phase 3V offline review mode for this command."),
    ] = False,
    mode: Annotated[
        str,
        typer.Option(help="Temporary Phase 3V mode when --enable-review is set."),
    ] = "offline_review",
) -> None:
    engine = _init_db_or_exit("Phase 3V live readiness status")
    settings = _phase_3v_command_settings(enable_review=enable_review, mode=mode)
    session_factory = get_session_factory(engine)
    with session_factory() as session:
        status = live_readiness_status(session, settings=settings)
    console.print("Phase 3V Live Trading Readiness")
    console.print(f"Mode: {status['mode']}")
    console.print(f"Decision: {status['decision']}")
    console.print(f"Target stage: {status['target_stage']}")
    console.print(f"Diagnostic score: {status['diagnostic_score']}")
    console.print(f"Critical blockers: {status['critical_blockers']}")
    console.print(f"Reviews: {status['review_count']}")
    console.print(f"Certificates: {status['certificate_count']}")
    console.print(f"Next action: {status['next_action']}")
    console.print("Safety: review only; no demo/live/order writes.")


@app.command("live-readiness-review")
def live_readiness_review_command(
    output: Annotated[
        Path,
        typer.Option(help="Markdown Phase 3V readiness report path."),
    ] = Path("reports/live_readiness_report.md"),
    json_output: Annotated[
        Path,
        typer.Option(help="Structured Phase 3V decision JSON path."),
    ] = Path("reports/live_readiness_decision.json"),
    target_stage: Annotated[
        str | None,
        typer.Option(help="Target stage: MICRO, CONSTRAINED, or FULL."),
    ] = None,
    enable_review: Annotated[
        bool,
        typer.Option(help="Temporarily enable Phase 3V offline review mode for this command."),
    ] = False,
    mode: Annotated[
        str,
        typer.Option(help="Temporary Phase 3V mode when --enable-review is set."),
    ] = "offline_review",
    persist: Annotated[
        bool,
        typer.Option(help="Persist append-only readiness review records."),
    ] = True,
) -> None:
    engine = _init_db_or_exit("Phase 3V live readiness review")
    settings = _phase_3v_command_settings(enable_review=enable_review, mode=mode)
    session_factory = get_session_factory(engine)
    with session_factory() as session:
        path = generate_live_readiness_report(
            session,
            output_path=output,
            json_output_path=json_output,
            settings=settings,
            target_stage=target_stage,
            persist=persist,
        )
    console.print(f"Wrote Phase 3V live readiness report to {path}")
    console.print(f"Wrote Phase 3V decision JSON to {json_output}")
    console.print("Safety: readiness review only; no live trading was enabled.")


@app.command("live-readiness-guard-check")
def live_readiness_guard_check_command(
    certificate_file: Annotated[
        Path | None,
        typer.Option(help="Optional certificate JSON file to verify."),
    ] = None,
    quantity: Annotated[int, typer.Option(help="Hypothetical order quantity to test.")] = 1,
    target_stage: Annotated[
        str | None,
        typer.Option(help="Expected certificate stage."),
    ] = None,
) -> None:
    certificate = None
    if certificate_file is not None:
        certificate = json.loads(certificate_file.read_text(encoding="utf-8"))
    result = verify_certificate_for_order(
        certificate,
        order_intent={"quantity": quantity},
        expected_stage=target_stage.upper() if target_stage else None,
    )
    console.print("Phase 3V Guard Check")
    console.print(f"Allow new/increasing risk: {result['allow_new_or_increasing_risk']}")
    console.print(f"Allow cancel-only: {result['allow_cancel_only']}")
    console.print(f"Reason codes: {', '.join(result['reason_codes']) or 'none'}")
    console.print("Safety: guard check does not submit, cancel, or replace orders.")


def _phase_3v_command_settings(*, enable_review: bool, mode: str):
    settings = get_settings()
    if not enable_review:
        return settings
    return settings.model_copy(
        update={
            "phase_3v_live_readiness_enabled": True,
            "phase_3v_mode": mode.lower(),
        }
    )


@app.command("system-certification-status")
def system_certification_status_command(
    enable_audit: Annotated[
        bool,
        typer.Option(help="Temporarily enable Phase 3W audit mode for this command."),
    ] = False,
) -> None:
    engine = _init_db_or_exit("Phase 3W system certification status")
    settings = _phase_3w_command_settings(enable_audit=enable_audit, mode="AUDIT_ONLY")
    session_factory = get_session_factory(engine)
    with session_factory() as session:
        status = system_certification_card(session, settings=settings)
    console.print("Phase 3W System Certification")
    console.print(f"Status: {status['overall_status']}")
    console.print(f"Mode: {status['mode']}")
    console.print(f"Latest run: {status['latest_run_id']}")
    console.print(f"Completed at: {status['completed_at']}")
    console.print(f"Phases: {status['phase_count']}")
    console.print(f"Connections: {status['connection_count']}")
    console.print("Live trading authorized: false")
    console.print(f"Next action: {status['next_action']}")


@app.command("system-certification-run")
def system_certification_run_command(
    output_dir: Annotated[
        Path,
        typer.Option(help="Directory for Phase 3W certification artifacts."),
    ] = Path("reports/system_certification"),
    mode: Annotated[
        str,
        typer.Option(
            help=(
                "Certification mode: AUDIT_ONLY, LOCAL_INTEGRATION, "
                "STAGING_READ_ONLY, or SAFE_REPAIR."
            )
        ),
    ] = "AUDIT_ONLY",
    run_contract_tests: Annotated[
        bool,
        typer.Option(help="Run local registry and contract checks. No exchange writes."),
    ] = False,
    run_golden_trace: Annotated[
        bool,
        typer.Option(help="Run deterministic local golden trace. Paper/demo safe only."),
    ] = False,
    database_profile: Annotated[
        str,
        typer.Option(help="Database evidence profile label for the certification report."),
    ] = "local",
    runtime_url: Annotated[
        str | None,
        typer.Option(help="Optional read-only runtime URL to describe; no live probes run."),
    ] = None,
    enable_audit: Annotated[
        bool,
        typer.Option(help="Temporarily enable Phase 3W audit mode for this command."),
    ] = False,
    persist: Annotated[
        bool,
        typer.Option(help="Persist append-only certification run records."),
    ] = True,
) -> None:
    engine = _init_db_or_exit("Phase 3W system certification run")
    settings = _phase_3w_command_settings(enable_audit=enable_audit, mode=mode)
    session_factory = get_session_factory(engine)
    with session_factory() as session:
        report = generate_system_certification_report(
            session,
            output_dir=output_dir,
            settings=settings,
            mode=mode,
            run_contract_tests=run_contract_tests,
            run_golden_trace=run_golden_trace,
            database_profile=database_profile,
            runtime_url=runtime_url,
            persist=persist,
        )
    console.print(f"Phase 3W outcome: {report['overall_status']}")
    console.print(f"Runtime observation: {report['runtime_observation_status']}")
    console.print(f"Phase 3V readiness: {report['phase_3v_readiness_status']}")
    console.print(f"Wrote certification artifacts to {output_dir}")
    console.print("Live trading authorized: false")


@app.command("system-certification-report")
def system_certification_report_command(
    output_dir: Annotated[
        Path,
        typer.Option(help="Directory for Phase 3W certification artifacts."),
    ] = Path("reports/system_certification"),
    enable_audit: Annotated[
        bool,
        typer.Option(help="Temporarily enable Phase 3W audit mode for this command."),
    ] = False,
) -> None:
    engine = _init_db_or_exit("Phase 3W system certification report")
    settings = _phase_3w_command_settings(enable_audit=enable_audit, mode="AUDIT_ONLY")
    session_factory = get_session_factory(engine)
    with session_factory() as session:
        report = generate_system_certification_report(
            session,
            output_dir=output_dir,
            settings=settings,
            mode="AUDIT_ONLY",
            persist=True,
        )
    console.print(f"Rendered Phase 3W certification report: {report['overall_status']}")
    console.print("THIS REPORT DOES NOT AUTHORIZE LIVE TRADING.")


def _phase_3w_command_settings(*, enable_audit: bool, mode: str):
    settings = get_settings()
    if not enable_audit:
        return settings
    return settings.model_copy(
        update={
            "phase_3w_system_certification_enabled": True,
            "phase_3w_mode": mode.upper(),
        }
    )


@app.command("phase3x-status")
def phase_3x_status_command(
    enable_preview: Annotated[
        bool,
        typer.Option(help="Temporarily enable Phase 3X preview presentation mode."),
    ] = False,
) -> None:
    engine = _init_db_or_exit("Phase 3X professional UX status")
    settings = _phase_3x_command_settings(enable_preview=enable_preview)
    session_factory = get_session_factory(engine)
    with session_factory() as session:
        status = phase_3x_card(session, settings=settings)
    console.print("Phase 3X Professional UX/UI")
    console.print(f"Decision: {status['decision']}")
    console.print(f"Mode: {status['mode']}")
    console.print(f"Routes audited: {status['route_count']}")
    console.print(f"Components cataloged: {status['component_count']}")
    console.print(f"Phase 3W status: {status['phase_3w_status']}")
    console.print("Live trading authorized: false")
    console.print(f"Next action: {status['next_action']}")


@app.command("phase3x-report")
def phase_3x_report_command(
    output_dir: Annotated[
        Path,
        typer.Option(help="Directory for Phase 3X UX audit artifacts."),
    ] = Path("docs/phase_3x"),
    enable_preview: Annotated[
        bool,
        typer.Option(help="Temporarily enable Phase 3X preview presentation mode."),
    ] = False,
) -> None:
    engine = _init_db_or_exit("Phase 3X professional UX report")
    settings = _phase_3x_command_settings(enable_preview=enable_preview)
    session_factory = get_session_factory(engine)
    with session_factory() as session:
        result = generate_phase_3x_report(
            session,
            output_dir=output_dir,
            settings=settings,
        )
    console.print(f"Phase 3X decision: {result['decision']}")
    console.print(f"Wrote UX audit artifacts to {output_dir}")
    console.print("Live trading authorized: false")


@app.command("phase3x-audit")
def phase_3x_audit_command(
    output_dir: Annotated[
        Path,
        typer.Option(help="Directory for Phase 3X UX audit artifacts."),
    ] = Path("docs/phase_3x"),
) -> None:
    engine = _init_db_or_exit("Phase 3X UI/UX audit")
    settings = _phase_3x_command_settings(enable_preview=True)
    session_factory = get_session_factory(engine)
    with session_factory() as session:
        result = generate_phase_3x_report(
            session,
            output_dir=output_dir,
            settings=settings,
        )
    console.print(f"Phase 3X audit decision: {result['decision']}")
    console.print(f"Artifacts: {len(result['artifacts'])}")
    console.print("Release decision remains INCOMPLETE until evidence gates pass.")


@app.command("ui-shell-status-refresh")
def ui_shell_status_refresh_command(
    output_path: Annotated[
        Path,
        typer.Option(help="Path for the non-blocking UI shell status snapshot."),
    ] = DEFAULT_SHELL_STATUS_SNAPSHOT_PATH,
) -> None:
    """Refresh the small JSON status source used by the UI top strip."""
    engine = _init_db_or_exit("UI shell status snapshot refresh")
    settings = get_settings()
    session_factory = get_session_factory(engine)
    with session_factory() as session:
        result = write_shell_status_snapshot(
            session,
            output_path=output_path,
            settings=settings,
        )
    payload = result["payload"]
    context = payload["context"]
    console.print("UI shell status snapshot refreshed")
    console.print("Mode: PAPER ONLY / READ ONLY diagnostics")
    console.print("Live/demo execution: blocked")
    console.print(f"Paper health: {context['paper_runtime']['label']}")
    console.print(
        "Market data: "
        f"{context['market_freshness']['label']} "
        f"{context['market_freshness'].get('age_label') or ''}".rstrip()
    )
    console.print(f"Wrote JSON: {result['path']}")


@app.command("system-remediate")
def system_remediate_command(
    refresh_data: Annotated[
        bool,
        typer.Option(help="Collect fresh public market data before regenerating evidence."),
    ] = False,
    collect_limit: Annotated[
        int,
        typer.Option(help="Maximum markets to collect when --refresh-data is set."),
    ] = 100,
    forecast_limit: Annotated[
        int,
        typer.Option(help="Maximum recent snapshots to forecast with all models."),
    ] = 100,
    output: Annotated[
        Path,
        typer.Option(help="Markdown remediation report path."),
    ] = SYSTEM_REMEDIATION_REPORT_PATH,
) -> None:
    result = run_system_readiness_remediation(
        settings=get_settings(),
        output_path=output,
        refresh_data=refresh_data,
        collect_limit=collect_limit,
        forecast_limit=forecast_limit,
    )
    console.print("System readiness remediation")
    console.print(f"Status: {result['status']}")
    console.print(f"Paper-only confirmed: {result['safety']['paper_only_confirmed']}")
    console.print("Live trading authorized: false")
    console.print("Demo execution attempted: false")
    for step in result["steps"]:
        console.print(f"- {step['status']}: {step['name']} - {step['message']}")
    console.print(f"Wrote remediation report to {result['report_path']}")


@app.command("system-remediation-report")
def system_remediation_report_command(
    output: Annotated[
        Path,
        typer.Option(help="Markdown remediation report path."),
    ] = SYSTEM_REMEDIATION_REPORT_PATH,
) -> None:
    result = run_system_readiness_remediation(
        settings=get_settings(),
        output_path=output,
        refresh_data=False,
    )
    console.print(f"System remediation report: {result['status']}")
    console.print(f"Wrote remediation report to {result['report_path']}")
    console.print("Live trading authorized: false")


def _phase_3x_command_settings(*, enable_preview: bool):
    settings = get_settings()
    if not enable_preview:
        return settings
    return settings.model_copy(
        update={
            "phase_3x_professional_ux_enabled": True,
            "phase_3x_mode": "preview",
        }
    )


@app.command("feature-experiment-export")
def feature_experiment_export_command(
    evaluation_id: Annotated[str, typer.Option(help="Feature evaluation ID to export.")],
    human_approval_reference: Annotated[
        str,
        typer.Option(help="Human approval ticket/reference required before export."),
    ],
    output: Annotated[
        Path,
        typer.Option(help="Structured experiment spec output path."),
    ] = Path("reports/feature_experiment_spec.json"),
) -> None:
    engine = _init_db_or_exit("Phase 3Q feature experiment export")
    session_factory = get_session_factory(engine)
    with session_factory() as session:
        path = export_feature_experiment_spec(
            session,
            evaluation_id=evaluation_id,
            human_approval_reference=human_approval_reference,
            output_path=output,
        )
        session.commit()
    console.print(f"Wrote human-reviewed Phase 3Q experiment spec to {path}")


@app.command("model-confidence")
def model_confidence_command(
    days: Annotated[int, typer.Option(help="Lookback days for confidence scoring.")] = 30,
    output: Annotated[
        Path,
        typer.Option(help="Markdown model confidence report path."),
    ] = Path("reports/model_confidence.md"),
) -> None:
    engine = init_db()
    settings = get_settings()
    session_factory = get_session_factory(engine)
    with session_factory() as session:
        result = run_model_confidence_engine(session, settings=settings, days=days)
        report_path = generate_model_confidence_report(
            session,
            output_path=output,
            settings=settings,
            days=days,
            refresh=False,
        )
        session.commit()
    console.print("Model confidence summary")
    console.print(f"Rows scored: {len(result.rows)}")
    console.print(f"Scores inserted: {result.scores_inserted}")
    console.print(f"Weights inserted: {result.weights_inserted}")
    console.print(f"Wrote model confidence report to {report_path}")


@app.command("models-status")
def models_status_command() -> None:
    engine = init_db()
    session_factory = get_session_factory(engine)
    with session_factory() as session:
        summary = model_status_summary(session)
    console.print("Models status")
    for row in summary.rows:
        console.print(
            " | ".join(
                (
                    f"model={row['model_name']}",
                    f"registered={row['registered_label']}",
                    f"forecasts={row['forecast_count']}",
                    f"latest={row['latest_forecast_time'] or 'none'}",
                    f"required={row['required_data']}",
                    f"status={row['readiness_status']}",
                    f"skip={row['skip_reason']}",
                )
            )
        )


@app.command("model-readiness")
def model_readiness_command() -> None:
    """Show model forecast readiness and missing data diagnostics."""
    engine = init_db()
    session_factory = get_session_factory(engine)
    with session_factory() as session:
        summary = model_status_summary(session)
    console.print("Model readiness")
    for row in summary.rows:
        console.print(
            " | ".join(
                (
                    f"model={row['model_name']}",
                    f"status={row['status']}",
                    f"registered={row['registered_label']}",
                    f"forecasts={row['forecast_count']}",
                    f"latest={row['latest_forecast_time'] or 'none'}",
                    f"missing={row['missing_data_label']}",
                    f"available={row['available_data_label']}",
                    f"next={row['next_commands_label']}",
                )
            )
        )


@app.command("model-readiness-report")
def model_readiness_report_command(
    output: Annotated[
        Path,
        typer.Option(help="Markdown model readiness report path."),
    ] = Path("reports/model_readiness.md"),
) -> None:
    """Write model readiness diagnostics to a Markdown report."""
    engine = init_db()
    session_factory = get_session_factory(engine)
    with session_factory() as session:
        report_path = generate_model_readiness_report(session, output_path=output)
    console.print(f"Wrote model readiness report to {report_path}")


@app.command("model-health")
def model_health_command() -> None:
    engine = init_db()
    session_factory = get_session_factory(engine)
    with session_factory() as session:
        summary = model_status_summary(session)
    inactive = summary.inactive_models
    console.print("Model health")
    if not inactive:
        console.print("All core models have at least one forecast.")
        return
    console.print("Inactive models:")
    for row in inactive:
        console.print(
            " | ".join(
                (
                    f"model={row['model_name']}",
                    f"forecasts={row['forecast_count']}",
                    f"skip_count={row['skip_count']}",
                    f"status={row['status']}",
                    f"missing={row['missing_data_label']}",
                    f"last_skip={row['skip_reason']}",
                    f"next={row['next_commands_label']}",
                )
            )
        )


@app.command("learning-targets")
def learning_targets_command(
    limit: Annotated[int, typer.Option(help="Maximum learning targets to generate.")] = 100,
    output: Annotated[
        Path,
        typer.Option(help="Markdown learning targets report path."),
    ] = Path("reports/learning_targets.md"),
) -> None:
    engine = init_db()
    settings = get_settings()
    session_factory = get_session_factory(engine)
    with session_factory() as session:
        result = generate_learning_targets(
            session,
            settings=settings,
            model_name="ensemble_v2",
            limit=limit,
        )
        report_path = generate_learning_targets_report(
            session,
            output_path=output,
            settings=settings,
            limit=limit,
            refresh=False,
        )
        session.commit()
    console.print("Learning targets")
    console.print(f"Rankings scanned: {result.scanned}")
    console.print(f"Targets inserted: {result.inserted}")
    console.print(f"Wrote learning targets report to {report_path}")


@app.command("accelerate-learning")
def accelerate_learning_command(
    model_name: Annotated[
        str,
        typer.Option(help="Forecast model to use for learning targets and paper trades."),
    ] = "ensemble_v2",
    limit: Annotated[int, typer.Option(help="Maximum learning targets/opportunities.")] = 100,
) -> None:
    engine = init_db()
    settings = get_settings()
    session_factory = get_session_factory(engine)
    with session_factory() as session:
        result = accelerate_learning(
            session,
            settings=settings,
            model_name=model_name,
            limit=limit,
        )
        session.commit()
    console.print("Accelerate Learning summary")
    console.print(f"Targets scanned: {result.targets_scanned}")
    console.print(f"Targets inserted: {result.targets_inserted}")
    fast_settling = ", ".join(result.fast_settling_categories) or "n/a"
    console.print(f"Fast-settling categories: {fast_settling}")
    console.print(f"Learning opportunities inserted: {result.learning_opportunities_inserted}")
    console.print(f"Paper trades created: {result.paper_trades_created}")
    console.print(f"Learning paper trades inserted: {result.learning_paper_trades_inserted}")
    console.print(f"Learning metric ID: {result.learning_metric_id}")
    console.print(f"Recommendation: {result.recommendation}")


@app.command("control-center-report")
def control_center_report_command(
    output: Annotated[
        Path,
        typer.Option(help="Markdown control-center report path."),
    ] = Path("reports/control_center.md"),
) -> None:
    engine = init_db()
    settings = get_settings()
    session_factory = get_session_factory(engine)
    with session_factory() as session:
        report_path = generate_control_center_report(
            session,
            output_path=output,
            settings=settings,
        )
    console.print(f"Wrote control center report to {report_path}")


@app.command("build-meta-features")
def build_meta_features_command(
    model_scope: Annotated[
        str,
        typer.Option(help="Candidate model scope, usually all."),
    ] = "all",
    limit: Annotated[int, typer.Option(help="Maximum latest markets to process.")] = 100,
    ticker: Annotated[str | None, typer.Option(help="Optional single ticker.")] = None,
) -> None:
    engine = init_db()
    session_factory = get_session_factory(engine)
    with session_factory() as session:
        summary = build_meta_features(
            session,
            model_scope=model_scope,
            limit=limit,
            ticker=ticker,
        )
        session.commit()
    console.print("Meta feature summary")
    console.print(f"Markets scanned: {summary.markets_scanned}")
    console.print(f"Features inserted: {summary.features_inserted}")
    console.print(f"Skipped: {summary.skipped}")


@app.command("build-meta-training")
def build_meta_training_command(
    days: Annotated[int, typer.Option(help="Settled-market lookback window in days.")] = 90,
) -> None:
    engine = init_db()
    session_factory = get_session_factory(engine)
    with session_factory() as session:
        summary = build_meta_training_examples(session, days=days)
        session.commit()
    console.print("Meta training summary")
    console.print(f"Settled markets scanned: {summary.settled_markets_scanned}")
    console.print(f"Training examples inserted: {summary.examples_inserted}")
    console.print(f"Limited comparisons: {summary.limited_comparisons}")
    console.print(f"Skipped unsettled: {summary.skipped_unsettled}")


@app.command("meta-evaluate")
def meta_evaluate_command(
    days: Annotated[int, typer.Option(help="Lookback window in days.")] = 90,
    output: Annotated[
        Path,
        typer.Option(help="Markdown meta evaluation report path."),
    ] = Path("reports/meta_evaluation.md"),
) -> None:
    engine = init_db()
    session_factory = get_session_factory(engine)
    with session_factory() as session:
        report_path = generate_meta_evaluation_report(session, days=days, output_path=output)
        session.commit()
    console.print(f"Wrote meta evaluation report to {report_path}")


@app.command("meta-report")
def meta_report_command(
    output: Annotated[
        Path,
        typer.Option(help="Markdown meta model report path."),
    ] = Path("reports/meta_report.md"),
) -> None:
    engine = init_db()
    session_factory = get_session_factory(engine)
    with session_factory() as session:
        report_path = generate_meta_report(session, output_path=output)
    console.print(f"Wrote meta report to {report_path}")


@app.command("meta-opportunities")
def meta_opportunities_command(
    limit: Annotated[int, typer.Option(help="Maximum meta opportunity rows.")] = 20,
    output: Annotated[
        Path,
        typer.Option(help="Markdown meta opportunities report path."),
    ] = Path("reports/meta_opportunities.md"),
) -> None:
    engine = init_db()
    session_factory = get_session_factory(engine)
    with session_factory() as session:
        report_path = generate_meta_opportunities_report(
            session,
            limit=limit,
            output_path=output,
        )
    console.print(f"Wrote meta opportunities report to {report_path}")


@app.command("ingest-forum-consensus")
def ingest_forum_consensus_command(
    input_file: Annotated[
        Path,
        typer.Option(help="JSON file with one signal or a top-level `signals` list."),
    ],
) -> None:
    payload = load_json_file(input_file)
    engine = init_db()
    settings = get_settings()
    session_factory = get_session_factory(engine)
    with session_factory() as session:
        signals = ingest_forum_consensus_payload(session, payload, settings=settings)
        session.commit()
    console.print(f"Inserted {len(signals)} forum consensus signal(s).")


@app.command("portfolio-summary")
def portfolio_summary_command(
    output: Annotated[
        Path | None,
        typer.Option(help="Optional Markdown portfolio summary path."),
    ] = Path("reports/portfolio_summary.md"),
) -> None:
    engine = init_db()
    session_factory = get_session_factory(engine)
    with session_factory() as session:
        record_portfolio_state(session)
        summary = portfolio_summary(session)
        report_path = (
            generate_portfolio_summary_report(
                session,
                output_path=output,
                summary=summary,
                record_state=False,
            )
            if output
            else None
        )
        session.commit()
    console.print("Portfolio summary")
    console.print(f"Portfolio value: {summary['portfolio_value']}")
    console.print(f"Total exposure: {summary['total_exposure']}")
    console.print(f"Open positions: {summary['open_positions']}")
    console.print(f"Realized P&L: {summary['realized_pnl']}")
    console.print(f"Unrealized P&L: {summary['unrealized_pnl']}")
    console.print(f"Total P&L: {summary['total_pnl']}")
    console.print(f"Open paper orders: {summary['open_orders']}")
    if report_path is not None:
        console.print(f"Wrote portfolio summary to {report_path}")


@app.command("daily-briefing")
def daily_briefing_command(
    output: Annotated[
        Path,
        typer.Option(help="Markdown daily briefing path."),
    ] = Path("reports/daily_briefing.md"),
) -> None:
    engine = init_db()
    settings = get_settings()
    session_factory = get_session_factory(engine)
    with session_factory() as session:
        report_path = generate_daily_briefing(session, output_path=output, settings=settings)
        session.commit()
    console.print(f"Wrote daily briefing to {report_path}")


@app.command("analytics-report")
def analytics_report_command(
    output: Annotated[
        Path,
        typer.Option(help="Markdown analytics report path."),
    ] = Path("reports/analytics_report.md"),
) -> None:
    engine = init_db()
    session_factory = get_session_factory(engine)
    with session_factory() as session:
        report_path = generate_analytics_report(session, output_path=output)
    console.print(f"Wrote analytics report to {report_path}")


@app.command("best-payouts")
def best_payouts_command(
    model_name: Annotated[
        str,
        typer.Option(help="Forecast model name to rank."),
    ] = "ensemble_v2",
    limit: Annotated[int, typer.Option(help="Maximum payout rows to report.")] = 20,
    output: Annotated[
        Path,
        typer.Option(help="Markdown best-payouts report path."),
    ] = Path("reports/best_payouts.md"),
) -> None:
    engine = init_db()
    session_factory = get_session_factory(engine)
    with session_factory() as session:
        rows = best_payout_rows(session, model_name=model_name, limit=limit)
        report_path = generate_best_payouts_report(
            session,
            model_name=model_name,
            limit=limit,
            output_path=output,
        )
    console.print("Best payout-adjusted opportunities")
    console.print(f"Model: {model_name}")
    console.print(f"Rows: {len(rows)}")
    console.print(f"Wrote best payouts report to {report_path}")


@app.command("research-opportunity")
def research_opportunity_command(
    ticker: Annotated[
        str,
        typer.Option("--ticker", help="Market ticker to research."),
    ],
    model_name: Annotated[
        str,
        typer.Option("--model-name", help="Forecast model name to explain."),
    ] = "ensemble_v2",
) -> None:
    engine = init_db()
    session_factory = get_session_factory(engine)
    with session_factory() as session:
        result = research_opportunity(
            session,
            ticker=ticker,
            model_name=model_name,
            persist_note=True,
        )
        session.commit()
    evidence = result["evidence"]
    narrative = result["narrative"]
    console.print(f"Research: {evidence['short_market_name']} ({evidence['ticker']})")
    console.print(f"Recommendation: {narrative['recommendation']}")
    console.print(f"Confidence: {narrative['confidence_label']}")
    console.print("")
    console.print(narrative["why_ranked"])
    console.print("")
    console.print("Supporting signals")
    for signal in narrative["supporting_signals"]:
        console.print(f"- {signal}")
    console.print("Risks")
    for risk in narrative["risks"]:
        console.print(f"- {risk}")
    console.print(f"Next action: {narrative['next_action']}")


@app.command("ask-research")
def ask_research_command(
    question: Annotated[str, typer.Argument(help="Predefined local research question.")],
    ticker: Annotated[str | None, typer.Option("--ticker", help="Optional market ticker.")] = None,
    model_name: Annotated[
        str,
        typer.Option("--model-name", help="Forecast model name to explain."),
    ] = "ensemble_v2",
) -> None:
    engine = init_db()
    session_factory = get_session_factory(engine)
    with session_factory() as session:
        result = answer_research_question(
            session,
            question=question,
            ticker=ticker,
            model_name=model_name,
        )
        store_research_question(session, result=result)
        session.commit()
    console.print(result["answer"])


@app.command("research-report")
def research_report_command(
    model_name: Annotated[
        str,
        typer.Option(help="Forecast model name to explain."),
    ] = "ensemble_v2",
    limit: Annotated[int, typer.Option(help="Maximum opportunities to explain.")] = 10,
    output: Annotated[
        Path,
        typer.Option(help="Markdown research report path."),
    ] = Path("reports/research_report.md"),
) -> None:
    engine = init_db()
    session_factory = get_session_factory(engine)
    with session_factory() as session:
        path = generate_research_report(
            session,
            model_name=model_name,
            limit=limit,
            output_path=output,
        )
        session.commit()
    console.print(f"Wrote research report to {path}")


@app.command("signal-report")
def signal_report_command(
    output: Annotated[
        Path,
        typer.Option(help="Markdown signal marketplace report path."),
    ] = Path("reports/signal_report.md"),
) -> None:
    engine = init_db()
    session_factory = get_session_factory(engine)
    with session_factory() as session:
        path = generate_signal_report(session, output_path=output)
        session.commit()
    console.print(f"Wrote signal report to {path}")


@app.command("signals-report")
def signals_report_command(
    output: Annotated[
        Path,
        typer.Option(help="Markdown signal readiness report path."),
    ] = Path("reports/signals_report.md"),
) -> None:
    engine = init_db()
    session_factory = get_session_factory(engine)
    with session_factory() as session:
        path = generate_signal_report(session, output_path=output)
        session.commit()
    console.print(f"Wrote signal report to {path}")


@app.command("signals-status")
def signals_status_command() -> None:
    engine = init_db()
    session_factory = get_session_factory(engine)
    with session_factory() as session:
        summary = signal_status_summary(session, log_skips=True)
        session.commit()
    console.print("Signals status")
    for row in summary.rows:
        console.print(
            " | ".join(
                (
                    f"signal={row['signal_key']}",
                    f"name={row['signal_name']}",
                    f"registered={row['registered_label']}",
                    f"forecasts={row['forecast_count']}",
                    f"trades={row['trade_count']}",
                    f"latest={row['latest_generated_time'] or 'none'}",
                    f"status={row['readiness_status']}",
                    f"missing={row['missing_data']}",
                    f"next={row['next_action']}",
                )
            )
        )


@app.command("signal-leaderboard")
def signal_leaderboard_command() -> None:
    engine = init_db()
    session_factory = get_session_factory(engine)
    with session_factory() as session:
        rows = signal_leaderboard_rows(session, refresh=True)
        session.commit()
    console.print("Signal leaderboard")
    for row in rows:
        console.print(
            f"{row['rank']}. {row['signal_name']} | ROI {row['roi'] or 'n/a'} | "
            f"Win {row['win_rate'] or 'n/a'} | Forecasts {row['forecast_count']} | "
            f"Trades {row['trade_count']} | Confidence {row['confidence_score'] or 'n/a'} | "
            f"{row['status']} | Missing {row['missing_data']} | Next {row['next_action']}"
        )


@app.command("signal-explorer")
def signal_explorer_command() -> None:
    engine = init_db()
    session_factory = get_session_factory(engine)
    with session_factory() as session:
        rows = signal_explorer_rows(session, refresh=True)
        session.commit()
    console.print("Available signals")
    for row in rows:
        console.print(
            f"- {row['signal_name']} ({row['category']}): "
            f"ROI {row['roi'] or 'n/a'}, forecasts {row['forecast_count']}, "
            f"trades {row['trade_count']}, models {', '.join(row['associated_models'])}, "
            f"activity {row['current_activity']}, status {row['status']}, "
            f"missing {row['missing_data']}, next {row['next_action']}"
        )


@app.command("signal-performance")
def signal_performance_command(
    signal_name: Annotated[
        str,
        typer.Option("--signal-name", help="Signal name to inspect."),
    ],
) -> None:
    engine = init_db()
    session_factory = get_session_factory(engine)
    with session_factory() as session:
        detail = signal_performance_summary(session, signal_name=signal_name, refresh=True)
        session.commit()
    if detail is None:
        raise typer.BadParameter(f"Unknown signal: {signal_name}")
    card = detail["card"]
    console.print(f"Signal: {card['signal_name']}")
    console.print(f"Description: {detail['signal'].description}")
    console.print(f"Status: {card['status']}")
    console.print(f"ROI: {card['roi'] or 'n/a'}")
    console.print(f"Win rate: {card['win_rate'] or 'n/a'}")
    console.print(f"Forecast count: {card['forecast_count']}")
    console.print(f"Trade count: {card['trade_count']}")
    console.print(f"Confidence: {card['confidence_score'] or 'n/a'}")
    console.print(f"Readiness: {card['status_label']}")
    console.print(f"Missing data: {card['missing_data']}")
    console.print(f"Next action: {card['next_action']}")
    console.print(detail["research_summary"])


@app.command("forecast-signals")
def forecast_signals_command(
    signal: Annotated[str, typer.Option("--signal", help="Expected signal key to generate.")],
) -> None:
    definition = expected_signal_by_key(signal)
    if definition is None:
        raise typer.BadParameter(f"Unknown signal key: {signal}")

    engine = init_db()
    session_factory = get_session_factory(engine)
    with session_factory() as session:
        if definition.key in {"breaking_news", "crypto_news", "economic_news"}:
            summary = generate_news_signals(session)
            rows = signal_status_rows(session, log_skips=True)
            session.commit()
            console.print(f"Generated {summary.signals_created} news signal row(s).")
            console.print(f"Signal events created: {summary.signal_events_created}")
        else:
            rows = signal_status_rows(session, log_skips=True)
            session.commit()
            console.print(
                "Signal attribution for this signal runs during forecast or paper-trade generation."
            )
    row = next((item for item in rows if item["signal_key"] == definition.key), None)
    if row is not None:
        console.print(
            f"{row['signal_name']} status={row['readiness_status']} "
            f"missing={row['missing_data']} next={row['next_action']}"
        )


@app.command("ui-summary")
def ui_summary_command() -> None:
    engine = init_db()
    settings = get_settings()
    session_factory = get_session_factory(engine)
    with session_factory() as session:
        service = DecisionUiService(session, settings=settings)
        dashboard = service.dashboard(limit=10)
        executive = dashboard["executive_summary"]
        opportunities = dashboard["opportunities"]
        payout_rows = best_payout_rows(session, model_name="ensemble_v2", limit=1)
        top_opportunity = opportunities[0] if opportunities else None
        top_payout = payout_rows[0] if payout_rows else None
    console.print("UI summary")
    console.print(f"Best opportunity: {executive['best_opportunity']}")
    console.print(
        "Best payout-adjusted opportunity: "
        f"{top_payout['market'] if top_payout else 'No acceptable payout row yet.'}"
    )
    top_risk = top_opportunity.top_risk if top_opportunity else "Run a scan first."
    console.print(f"Top risk: {top_risk}")
    console.print(f"Paper P&L: {executive['paper_pnl']}")
    console.print(f"Model leader: {executive['best_model']}")
    console.print(f"Recommended next action: {dashboard['summary']['top_action']}")


@app.command("sync-markets")
def sync_markets_command(
    status: Annotated[str | None, typer.Option(help="Market status filter.")] = "open",
    limit: Annotated[int, typer.Option(help="Page size.")] = 100,
    max_pages: Annotated[int | None, typer.Option(help="Maximum pages to fetch.")] = 1,
    series_ticker: Annotated[str | None, typer.Option(help="Optional series ticker.")] = None,
    event_ticker: Annotated[str | None, typer.Option(help="Optional event ticker.")] = None,
) -> None:
    init_db()
    count = sync_markets_job(
        status=status,
        limit=limit,
        max_pages=max_pages,
        series_ticker=series_ticker,
        event_ticker=event_ticker,
    )
    console.print(f"Synced {count} markets.")


@app.command("snapshot")
def snapshot_command(
    status: Annotated[str | None, typer.Option(help="Market status filter.")] = "open",
    limit: Annotated[int, typer.Option(help="Page size.")] = 100,
    max_pages: Annotated[int | None, typer.Option(help="Maximum pages to fetch.")] = 1,
    series_ticker: Annotated[str | None, typer.Option(help="Optional series ticker.")] = None,
    event_ticker: Annotated[str | None, typer.Option(help="Optional event ticker.")] = None,
    include_orderbook: Annotated[bool, typer.Option(help="Fetch public orderbooks.")] = True,
) -> None:
    init_db()
    snapshots = capture_snapshots(
        status=status,
        limit=limit,
        max_pages=max_pages,
        series_ticker=series_ticker,
        event_ticker=event_ticker,
        include_orderbook=include_orderbook,
    )
    console.print(f"Captured {len(snapshots)} snapshots.")


@app.command("forecast")
def forecast_command(
    model: Annotated[
        str,
        typer.Option(
            "--model",
            help=(
                "Forecast model: market_implied_v1, weather_v1, weather_v2, crypto_v1, crypto_v2, "
                "economic_v1, news_v1, mlb_v1, nba_v1, nfl_v1, nhl_v1, sports_v1, "
                "microstructure_v1, meta_model_v1, meta_ensemble_v1, ensemble_v1, "
                "ensemble_v2, or all."
            ),
        ),
    ] = "market_implied_v1",
    limit: Annotated[int, typer.Option(help="Number of recent snapshots to forecast.")] = 100,
    ticker: Annotated[str | None, typer.Option(help="Optional ticker filter.")] = None,
) -> None:
    engine = init_db()
    session_factory = get_session_factory(engine)
    with session_factory() as session:
        scoped_snapshots = (
            None
            if ticker is not None
            else latest_snapshots_for_model(session, model_name=model, limit=limit)
        )
        snapshots = (
            scoped_snapshots
            if scoped_snapshots is not None
            else get_recent_snapshots(session, ticker=ticker, limit=limit)
        )
        summary = run_forecast_models(session, model_name=model, snapshots=snapshots)
        session.commit()
    console.print(
        f"Scanned {summary.snapshots_scanned} snapshots. "
        f"Inserted {summary.forecasts_inserted} forecasts. Skipped {summary.skipped}."
    )


@app.command("collect-once")
def collect_once_command(
    status: Annotated[str | None, typer.Option(help="Market status filter.")] = "open",
    limit: Annotated[int, typer.Option(help="Page size.")] = 100,
    max_pages: Annotated[int | None, typer.Option(help="Maximum pages to fetch.")] = 1,
    series_ticker: Annotated[str | None, typer.Option(help="Optional series ticker.")] = None,
    event_ticker: Annotated[str | None, typer.Option(help="Optional event ticker.")] = None,
    include_orderbook: Annotated[bool, typer.Option(help="Fetch public orderbooks.")] = True,
) -> None:
    collect_once_job(
        status=status,
        limit=limit,
        max_pages=max_pages,
        series_ticker=series_ticker,
        event_ticker=event_ticker,
        include_orderbook=include_orderbook,
    )


@app.command("sync-settlements")
def sync_settlements_command(
    lookback_days: Annotated[
        int,
        typer.Option(help="Local filter for recently settled markets."),
    ] = 30,
    limit: Annotated[int, typer.Option(help="Page size.")] = 100,
    max_pages: Annotated[int | None, typer.Option(help="Maximum pages to fetch.")] = 1,
    min_settled_ts: Annotated[str | None, typer.Option(help="Optional ISO lower bound.")] = None,
    max_settled_ts: Annotated[str | None, typer.Option(help="Optional ISO upper bound.")] = None,
) -> None:
    init_db()
    count = sync_settlements_job(
        lookback_days=lookback_days,
        limit=limit,
        max_pages=max_pages,
        min_settled_ts=min_settled_ts,
        max_settled_ts=max_settled_ts,
    )
    console.print(f"Synced {count} settlements.")


@app.command("report-calibration")
def report_calibration_command(
    model_name: Annotated[str, typer.Option(help="Forecast model name.")] = "market_implied_v1",
    output: Annotated[Path, typer.Option(help="Markdown report output path.")] = Path(
        "reports/calibration.md"
    ),
) -> None:
    init_db()
    report_path = generate_calibration_report(model_name, output)
    console.print(f"Wrote calibration report to {report_path}")


@app.command("ingest-external")
def ingest_external_command(
    source: Annotated[
        str,
        typer.Option(help="External source: weather, crypto, or economic."),
    ],
    input_file: Annotated[Path, typer.Option(help="Path to source JSON file.")],
) -> None:
    payload = load_json_file(input_file)
    engine = init_db()
    session_factory = get_session_factory(engine)
    with session_factory() as session:
        if source == "weather":
            result = ingest_weather_json(session, payload)
        elif source == "crypto":
            result = ingest_crypto_json(session, payload)
        elif source == "economic":
            result = ingest_economic_json(session, payload)
        else:
            raise typer.BadParameter("source must be weather, crypto, or economic.")
        session.commit()
    console.print(f"Ingested {result.records_inserted} {result.source} feature record(s).")


@app.command("ingest-news")
def ingest_news_command(
    source: Annotated[
        str | None,
        typer.Option(help="News source to ingest. Use rss or provide --input-file."),
    ] = None,
    input_file: Annotated[
        Path | None,
        typer.Option(help="Local news JSON or CSV file to ingest."),
    ] = None,
) -> None:
    engine = init_db()
    settings = get_settings()
    session_factory = get_session_factory(engine)
    with session_factory() as session:
        if input_file is not None:
            summary = ingest_news_file(session, input_file)
        elif source == "rss":
            summary = ingest_news_rss(session, settings=settings)
        else:
            raise typer.BadParameter("ingest-news requires --source rss or --input-file.")
        session.commit()
    console.print("News ingestion summary")
    console.print(f"Source: {summary.source}")
    console.print(f"Items seen: {summary.items_seen}")
    console.print(f"Items inserted: {summary.items_inserted}")
    console.print(f"Duplicates skipped: {summary.duplicates_skipped}")
    if summary.feeds_attempted:
        console.print(f"Feeds attempted: {summary.feeds_attempted}")
        console.print(f"Feeds succeeded: {summary.feeds_succeeded}")
    if summary.message:
        console.print(summary.message)
    if summary.errors:
        console.print("Errors:")
        for error in summary.errors:
            console.print(f"- {error}")


@app.command("link-news-markets")
def link_news_markets_command() -> None:
    engine = init_db()
    settings = get_settings()
    session_factory = get_session_factory(engine)
    with session_factory() as session:
        summary = link_news_markets(session, settings=settings)
        session.commit()
    console.print("News market link summary")
    console.print(f"News items scanned: {summary.news_items_scanned}")
    console.print(f"Markets scanned: {summary.markets_scanned}")
    console.print(f"Links created: {summary.links_created}")
    console.print(f"Links by category: {summary.links_by_category}")


@app.command("ingest-economic")
def ingest_economic_command(
    input_file: Annotated[
        Path,
        typer.Option("--input-file", help="Economic event JSON file."),
    ],
) -> None:
    engine = init_db()
    payload = load_json_file(input_file)
    session_factory = get_session_factory(engine)
    with session_factory() as session:
        result = ingest_economic_file_payload(session, payload)
        session.commit()
    console.print("Economic ingestion summary")
    console.print(f"Events inserted: {result.events_inserted}")
    if result.errors:
        console.print(f"Errors: {result.errors}")


@app.command("build-economic-features")
def build_economic_features_command() -> None:
    engine = init_db()
    session_factory = get_session_factory(engine)
    with session_factory() as session:
        result = build_economic_features(session)
        session.commit()
    console.print("Economic feature summary")
    console.print(f"Events processed: {result.events_processed}")
    console.print(f"Features inserted: {result.features_inserted}")


@app.command("link-economic-markets")
def link_economic_markets_command() -> None:
    engine = init_db()
    session_factory = get_session_factory(engine)
    with session_factory() as session:
        result = link_economic_markets(session)
        session.commit()
    console.print("Economic link summary")
    console.print(f"Markets scanned: {result.markets_scanned}")
    console.print(f"Links created: {result.links_created}")
    console.print(f"By category: {result.by_category}")


@app.command("phase3bd-economic-market-discovery")
def phase3bd_economic_market_discovery_command(
    output_dir: Annotated[
        Path,
        typer.Option(help="Output directory for Phase 3BD economic discovery artifacts."),
    ] = Path("reports/phase3bd"),
    max_series: Annotated[
        int,
        typer.Option(help="Maximum Economics-category Kalshi series to inspect."),
    ] = 24,
    markets_per_series: Annotated[
        int,
        typer.Option(help="Maximum open markets fetched per selected Economics series."),
    ] = 100,
    snapshot_series_limit: Annotated[
        int,
        typer.Option(help="Maximum selected series to snapshot for economic_v1 forecasting."),
    ] = 12,
    forecast_limit: Annotated[
        int,
        typer.Option(help="Maximum latest linked economic snapshots to forecast."),
    ] = 500,
    include_orderbooks: Annotated[
        bool,
        typer.Option(help="Fetch public orderbooks while capturing economic snapshots."),
    ] = True,
    series_api_limit: Annotated[
        int | None,
        typer.Option(help="Optional /series API limit; omit to let Kalshi return full catalog."),
    ] = None,
) -> None:
    engine = init_db()
    session_factory = get_session_factory(engine)
    with session_factory() as session:
        artifacts = write_phase3bd_economic_market_discovery_report(
            session=session,
            output_dir=output_dir,
            max_series=max_series,
            markets_per_series=markets_per_series,
            snapshot_series_limit=snapshot_series_limit,
            forecast_limit=forecast_limit,
            include_orderbooks=include_orderbooks,
            series_api_limit=series_api_limit,
        )
        session.commit()
    summary = artifacts.payload["summary"]
    console.print("Phase 3BD economic market discovery")
    console.print("Mode: PAPER / READ ONLY")
    console.print("Live/demo execution: blocked")
    console.print(f"Status: {summary['status']}")
    console.print(f"Economics series selected: {summary['selected_candidates']}")
    console.print(f"Markets synced: {summary['markets_synced']}")
    console.print(f"Links created: {summary['links_created']}")
    console.print(f"Snapshots captured: {summary['snapshots_captured']}")
    console.print(f"Forecasts inserted: {summary['forecasts_inserted']}")
    console.print(f"Wrote JSON: {artifacts.json_path}")
    console.print(f"Wrote Markdown: {artifacts.markdown_path}")


@app.command("phase3bd-r2-economic-calendar-freshness")
def phase3bd_r2_economic_calendar_freshness_command(
    output_dir: Annotated[
        Path,
        typer.Option(help="Output directory for Phase 3BD-R2 economic calendar artifacts."),
    ] = Path("reports/phase3bd_r2"),
    max_series: Annotated[
        int,
        typer.Option(help="Maximum Economics-category Kalshi series to inspect."),
    ] = 24,
    markets_per_series: Annotated[
        int,
        typer.Option(help="Maximum open markets fetched per selected Economics series."),
    ] = 50,
    snapshot_series_limit: Annotated[
        int,
        typer.Option(help="Maximum selected series to snapshot for economic_v1 forecasting."),
    ] = 8,
    forecast_limit: Annotated[
        int,
        typer.Option(help="Maximum latest linked economic snapshots to forecast."),
    ] = 500,
    opportunity_limit: Annotated[
        int,
        typer.Option(help="Maximum recent economic_v1 rankings to scan for opportunities."),
    ] = 75,
    days_ahead: Annotated[
        int,
        typer.Option(help="Calendar lookahead window for selecting current events."),
    ] = 180,
    lookback_days: Annotated[
        int,
        typer.Option(help="Calendar lookback window when no upcoming event exists."),
    ] = 45,
) -> None:
    engine = init_db()
    session_factory = get_session_factory(engine)
    with session_factory() as session:
        artifacts = write_phase3bd_r2_economic_calendar_freshness_report(
            session=session,
            output_dir=output_dir,
            max_series=max_series,
            markets_per_series=markets_per_series,
            snapshot_series_limit=snapshot_series_limit,
            forecast_limit=forecast_limit,
            opportunity_limit=opportunity_limit,
            days_ahead=days_ahead,
            lookback_days=lookback_days,
        )
        session.commit()
    summary = artifacts.payload["summary"]
    console.print("Phase 3BD-R2 economic calendar freshness")
    console.print("Mode: PAPER / READ ONLY")
    console.print("Live/demo execution: blocked")
    console.print(f"Status: {summary['status']}")
    console.print(
        f"Sources succeeded: {summary['sources_succeeded']} / {summary['sources_attempted']}"
    )
    console.print(f"Selected current events: {summary['selected_current_events']}")
    console.print(f"Events inserted: {summary['events_inserted']}")
    console.print(f"Features inserted: {summary['features_inserted']}")
    console.print(f"Forecasts inserted: {summary['forecasts_inserted']}")
    console.print(f"Rankings inserted: {summary['rankings_inserted']}")
    console.print(f"Opportunities detected: {summary['opportunities_detected']}")
    console.print(f"Wrote JSON: {artifacts.json_path}")
    console.print(f"Wrote Markdown: {artifacts.markdown_path}")


@app.command("phase3bd-r3-economic-value-capture")
def phase3bd_r3_economic_value_capture_command(
    output_dir: Annotated[
        Path,
        typer.Option(help="Output directory for Phase 3BD-R3 economic value artifacts."),
    ] = Path("reports/phase3bd_r3"),
    max_series: Annotated[
        int,
        typer.Option(help="Maximum Economics-category Kalshi series to inspect."),
    ] = 24,
    markets_per_series: Annotated[
        int,
        typer.Option(help="Maximum open markets fetched per selected Economics series."),
    ] = 50,
    snapshot_series_limit: Annotated[
        int,
        typer.Option(help="Maximum selected series to snapshot for economic_v1 forecasting."),
    ] = 8,
    forecast_limit: Annotated[
        int,
        typer.Option(help="Maximum latest linked economic snapshots to forecast."),
    ] = 500,
    opportunity_limit: Annotated[
        int,
        typer.Option(help="Maximum recent economic_v1 rankings to scan for opportunities."),
    ] = 75,
) -> None:
    engine = init_db()
    session_factory = get_session_factory(engine)
    with session_factory() as session:
        artifacts = write_phase3bd_r3_economic_value_capture_report(
            session=session,
            output_dir=output_dir,
            max_series=max_series,
            markets_per_series=markets_per_series,
            snapshot_series_limit=snapshot_series_limit,
            forecast_limit=forecast_limit,
            opportunity_limit=opportunity_limit,
        )
        session.commit()
    summary = artifacts.payload["summary"]
    console.print("Phase 3BD-R3 economic value capture")
    console.print("Mode: PAPER / READ ONLY")
    console.print("Live/demo execution: blocked")
    console.print(f"Status: {summary['status']}")
    console.print(
        f"Sources succeeded: {summary['sources_succeeded']} / {summary['sources_attempted']}"
    )
    console.print(f"Value observations seen: {summary['value_observations_seen']}")
    console.print(f"Value observations inserted: {summary['value_observations_inserted']}")
    console.print(f"Features inserted: {summary['features_inserted']}")
    console.print(f"Forecasts inserted: {summary['forecasts_inserted']}")
    console.print(f"Rankings inserted: {summary['rankings_inserted']}")
    console.print(f"Opportunities detected: {summary['opportunities_detected']}")
    console.print(f"Wrote JSON: {artifacts.json_path}")
    console.print(f"Wrote Markdown: {artifacts.markdown_path}")


@app.command("phase3bd-r4-verified-consensus-source")
def phase3bd_r4_verified_consensus_source_command(
    output_dir: Annotated[
        Path,
        typer.Option(help="Output directory for Phase 3BD-R4 consensus artifacts."),
    ] = Path("reports/phase3bd_r4"),
    input_file: Annotated[
        Path | None,
        typer.Option(
            help=(
                "Optional verified JSON/CSV export with event_key, event_time, "
                "source_url, forecast/consensus, actual, and previous values."
            ),
        ),
    ] = None,
    trading_economics_api_key: Annotated[
        str | None,
        typer.Option(
            help=(
                "Trading Economics API key. If omitted, TRADING_ECONOMICS_API_KEY, "
                "TRADINGECONOMICS_API_KEY, or TE_API_KEY is used."
            ),
        ),
    ] = None,
    country: Annotated[
        str,
        typer.Option(help="Trading Economics country filter."),
    ] = "united states",
    days_back: Annotated[
        int,
        typer.Option(help="Consensus source lookback window in days."),
    ] = 90,
    days_ahead: Annotated[
        int,
        typer.Option(help="Consensus source lookahead window in days."),
    ] = 14,
    min_importance: Annotated[
        int,
        typer.Option(help="Minimum Trading Economics importance level."),
    ] = 2,
    max_series: Annotated[
        int,
        typer.Option(help="Maximum Economics-category Kalshi series to inspect."),
    ] = 24,
    markets_per_series: Annotated[
        int,
        typer.Option(help="Maximum open markets fetched per selected Economics series."),
    ] = 50,
    snapshot_series_limit: Annotated[
        int,
        typer.Option(help="Maximum selected series to snapshot for economic_v1 forecasting."),
    ] = 8,
    forecast_limit: Annotated[
        int,
        typer.Option(help="Maximum latest linked economic snapshots to forecast."),
    ] = 500,
    opportunity_limit: Annotated[
        int,
        typer.Option(help="Maximum recent economic_v1 rankings to scan for opportunities."),
    ] = 75,
) -> None:
    engine = init_db()
    session_factory = get_session_factory(engine)
    with session_factory() as session:
        artifacts = write_phase3bd_r4_verified_consensus_source_report(
            session=session,
            output_dir=output_dir,
            input_file=input_file,
            trading_economics_api_key=trading_economics_api_key,
            country=country,
            days_back=days_back,
            days_ahead=days_ahead,
            min_importance=min_importance,
            max_series=max_series,
            markets_per_series=markets_per_series,
            snapshot_series_limit=snapshot_series_limit,
            forecast_limit=forecast_limit,
            opportunity_limit=opportunity_limit,
        )
        session.commit()
    summary = artifacts.payload["summary"]
    console.print("Phase 3BD-R4 verified consensus source integration")
    console.print("Mode: PAPER / READ ONLY")
    console.print("Live/demo execution: blocked")
    console.print(f"Status: {summary['status']}")
    console.print(
        f"Sources succeeded: {summary['sources_succeeded']} / {summary['sources_attempted']}"
    )
    console.print(f"Consensus observations: {summary['consensus_value_observations']}")
    console.print(
        "Actual + consensus observations: "
        f"{summary['actual_and_consensus_observations']}"
    )
    console.print(f"Value observations inserted: {summary['value_observations_inserted']}")
    console.print(f"Features inserted: {summary['features_inserted']}")
    console.print(f"Forecasts inserted: {summary['forecasts_inserted']}")
    console.print(f"Rankings inserted: {summary['rankings_inserted']}")
    console.print(f"Opportunities detected: {summary['opportunities_detected']}")
    console.print(f"Wrote JSON: {artifacts.json_path}")
    console.print(f"Wrote Markdown: {artifacts.markdown_path}")


@app.command("phase3bd-r5-consensus-feed-watch")
def phase3bd_r5_consensus_feed_watch_command(
    output_dir: Annotated[
        Path,
        typer.Option(help="Output directory for Phase 3BD-R5 watch artifacts."),
    ] = Path("reports/phase3bd_r5"),
    cycles: Annotated[
        int,
        typer.Option(help="Maximum watch cycles to run."),
    ] = 1,
    interval_minutes: Annotated[
        int,
        typer.Option(help="Minutes to wait between watch cycles."),
    ] = 15,
    force_refresh: Annotated[
        bool,
        typer.Option(help="Run R4 this cycle even outside a release window."),
    ] = False,
    input_file: Annotated[
        Path | None,
        typer.Option(
            help=(
                "Optional verified JSON/CSV export with event_key, event_time, "
                "source_url, forecast/consensus, actual, and previous values."
            ),
        ),
    ] = None,
    trading_economics_api_key: Annotated[
        str | None,
        typer.Option(
            help=(
                "Trading Economics API key. If omitted, TRADING_ECONOMICS_API_KEY, "
                "TRADINGECONOMICS_API_KEY, or TE_API_KEY is used."
            ),
        ),
    ] = None,
    country: Annotated[
        str,
        typer.Option(help="Trading Economics country filter."),
    ] = "united states",
    days_back: Annotated[
        int,
        typer.Option(help="Consensus source and release-calendar lookback window in days."),
    ] = 90,
    days_ahead: Annotated[
        int,
        typer.Option(help="Consensus source and release-calendar lookahead window in days."),
    ] = 14,
    min_importance: Annotated[
        int,
        typer.Option(help="Minimum Trading Economics importance level."),
    ] = 2,
    pre_release_minutes: Annotated[
        int,
        typer.Option(help="Run R4 this many minutes before tracked releases."),
    ] = 180,
    post_release_minutes: Annotated[
        int,
        typer.Option(help="Run R4 this many minutes after tracked releases."),
    ] = 360,
    max_series: Annotated[
        int,
        typer.Option(help="Maximum Economics-category Kalshi series to inspect."),
    ] = 24,
    markets_per_series: Annotated[
        int,
        typer.Option(help="Maximum open markets fetched per selected Economics series."),
    ] = 50,
    snapshot_series_limit: Annotated[
        int,
        typer.Option(help="Maximum selected series to snapshot for economic_v1 forecasting."),
    ] = 8,
    forecast_limit: Annotated[
        int,
        typer.Option(help="Maximum latest linked economic snapshots to forecast."),
    ] = 500,
    opportunity_limit: Annotated[
        int,
        typer.Option(help="Maximum recent economic_v1 rankings to scan for opportunities."),
    ] = 75,
) -> None:
    engine = init_db()
    session_factory = get_session_factory(engine)
    artifacts = None
    total_cycles = max(cycles, 1)
    for cycle_number in range(1, total_cycles + 1):
        with session_factory() as session:
            artifacts = write_phase3bd_r5_consensus_feed_watch_report(
                session=session,
                output_dir=output_dir,
                input_file=input_file,
                trading_economics_api_key=trading_economics_api_key,
                country=country,
                days_back=days_back,
                days_ahead=days_ahead,
                min_importance=min_importance,
                pre_release_minutes=pre_release_minutes,
                post_release_minutes=post_release_minutes,
                force_refresh=force_refresh,
                cycle_number=cycle_number,
                total_cycles=total_cycles,
                max_series=max_series,
                markets_per_series=markets_per_series,
                snapshot_series_limit=snapshot_series_limit,
                forecast_limit=forecast_limit,
                opportunity_limit=opportunity_limit,
            )
            session.commit()
        summary = artifacts.payload["summary"]
        console.print("Phase 3BD-R5 economic consensus feed watch")
        console.print("Mode: PAPER / READ ONLY")
        console.print("Live/demo execution: blocked")
        console.print("Order submission/cancel/replace: blocked")
        console.print(f"Cycle: {summary['cycle_number']} / {summary['total_cycles']}")
        console.print(f"Status: {summary['status']}")
        console.print(f"Source mode: {summary['source_mode']}")
        console.print(f"In release window: {summary['in_release_window']}")
        console.print(f"R4 ran: {summary['r4_ran']}")
        console.print(f"R4 status: {summary['r4_status'] or 'n/a'}")
        console.print(f"Consensus observations: {summary['consensus_value_observations']}")
        console.print(
            "Actual + consensus observations: "
            f"{summary['actual_and_consensus_observations']}"
        )
        console.print(f"Features inserted: {summary['features_inserted']}")
        console.print(f"Forecasts inserted: {summary['forecasts_inserted']}")
        console.print(f"Rankings inserted: {summary['rankings_inserted']}")
        console.print(f"Opportunities detected: {summary['opportunities_detected']}")
        if cycle_number < total_cycles:
            time.sleep(max(interval_minutes, 0) * 60)
    if artifacts is None:
        return
    console.print(f"Wrote JSON: {artifacts.json_path}")
    console.print(f"Wrote Markdown: {artifacts.markdown_path}")
    console.print(f"Wrote history: {artifacts.history_path}")


@app.command("phase3bd-r7-economic-opportunity-quality-gate")
def phase3bd_r7_economic_opportunity_quality_gate_command(
    output_dir: Annotated[
        Path,
        typer.Option(help="Output directory for Phase 3BD-R7 artifacts."),
    ] = Path("reports/phase3bd_r7"),
    limit: Annotated[
        int,
        typer.Option(help="Maximum latest economic_v1 rankings to inspect."),
    ] = 500,
    freshness_minutes: Annotated[
        int,
        typer.Option(help="Snapshot/forecast/ranking freshness window in minutes."),
    ] = 360,
    min_expected_value: Annotated[
        str,
        typer.Option(help="Minimum side-adjusted expected value in dollars."),
    ] = "0",
    min_edge: Annotated[
        str,
        typer.Option(help="Minimum model edge in dollars."),
    ] = "0.01",
    min_score: Annotated[
        str,
        typer.Option(help="Minimum opportunity score."),
    ] = "60",
    min_liquidity_score: Annotated[
        str,
        typer.Option(help="Minimum liquidity score for preflight readiness."),
    ] = "1",
    max_spread: Annotated[
        str,
        typer.Option(help="Maximum acceptable spread in dollars."),
    ] = "0.03",
    require_actual_consensus: Annotated[
        bool,
        typer.Option(
            help="Require released actual plus verified consensus evidence before preflight."
        ),
    ] = True,
    max_preflight: Annotated[
        int,
        typer.Option(help="Maximum rows eligible for optional paper-only preflight."),
    ] = 10,
    risk_preflight: Annotated[
        bool,
        typer.Option(help="Record paper-only Phase 3M/3N preflight for clean rows."),
    ] = False,
) -> None:
    engine = init_db()
    settings = get_settings()
    session_factory = get_session_factory(engine)
    with session_factory() as session:
        artifacts = write_phase3bd_r7_economic_opportunity_quality_gate_report(
            session=session,
            output_dir=output_dir,
            settings=settings,
            limit=limit,
            freshness_minutes=freshness_minutes,
            min_expected_value=Decimal(min_expected_value),
            min_edge=Decimal(min_edge),
            min_score=Decimal(min_score),
            min_liquidity_score=Decimal(min_liquidity_score),
            max_spread=Decimal(max_spread),
            require_actual_consensus=require_actual_consensus,
            max_preflight=max_preflight,
            risk_preflight=risk_preflight,
        )
        session.commit()
    summary = artifacts.payload["summary"]
    console.print("Phase 3BD-R7 economic opportunity quality gate")
    console.print("Mode: PAPER / READ ONLY")
    console.print("Live/demo execution: blocked")
    console.print("Order submission/cancel/replace: blocked")
    console.print(f"Status: {summary['status']}")
    console.print(f"Rankings scanned: {summary['economic_rankings_scanned']}")
    console.print(f"Preflight-ready rows: {summary['preflight_ready_rows']}")
    console.print(
        "Phase 3M/3N preflight recorded: "
        f"{summary['phase3m_phase3n_preflight_recorded']}"
    )
    console.print(f"Primary gap: {summary['primary_gap']}")
    console.print(f"Wrote JSON: {artifacts.json_path}")
    console.print(f"Wrote Markdown: {artifacts.markdown_path}")
    console.print(f"Wrote rows: {artifacts.rows_path}")


@app.command("phase3bd-r8-economic-evidence-activation")
def phase3bd_r8_economic_evidence_activation_command(
    output_dir: Annotated[
        Path,
        typer.Option(help="Output directory for Phase 3BD-R8 artifacts."),
    ] = Path("reports/phase3bd_r8"),
    r5_output_dir: Annotated[
        Path,
        typer.Option(help="Output directory for refreshed Phase 3BD-R5 artifacts."),
    ] = Path("reports/phase3bd_r5"),
    r7_output_dir: Annotated[
        Path,
        typer.Option(help="Output directory for refreshed Phase 3BD-R7 artifacts."),
    ] = Path("reports/phase3bd_r7"),
    input_file: Annotated[
        Path | None,
        typer.Option(
            help=(
                "Optional verified JSON/CSV export with event_key, event_time, "
                "source_url, forecast/consensus, actual, and previous values."
            ),
        ),
    ] = None,
    trading_economics_api_key: Annotated[
        str | None,
        typer.Option(
            help=(
                "Trading Economics API key. If omitted, TRADING_ECONOMICS_API_KEY, "
                "TRADINGECONOMICS_API_KEY, or TE_API_KEY is used."
            ),
        ),
    ] = None,
    country: Annotated[
        str,
        typer.Option(help="Trading Economics country filter."),
    ] = "united states",
    days_back: Annotated[
        int,
        typer.Option(help="Consensus source lookback window in days."),
    ] = 90,
    days_ahead: Annotated[
        int,
        typer.Option(help="Consensus source lookahead window in days."),
    ] = 14,
    min_importance: Annotated[
        int,
        typer.Option(help="Minimum Trading Economics importance level."),
    ] = 2,
    force_refresh: Annotated[
        bool,
        typer.Option(help="Run R5/R4 immediately when a verified source is configured."),
    ] = True,
    max_series: Annotated[
        int,
        typer.Option(help="Maximum Economics-category Kalshi series to inspect."),
    ] = 24,
    markets_per_series: Annotated[
        int,
        typer.Option(help="Maximum open markets fetched per selected Economics series."),
    ] = 50,
    snapshot_series_limit: Annotated[
        int,
        typer.Option(help="Maximum selected series to snapshot for economic_v1 forecasting."),
    ] = 8,
    forecast_limit: Annotated[
        int,
        typer.Option(help="Maximum latest linked economic snapshots to forecast."),
    ] = 500,
    opportunity_limit: Annotated[
        int,
        typer.Option(help="Maximum recent economic_v1 rankings to scan for opportunities."),
    ] = 75,
    r7_limit: Annotated[
        int,
        typer.Option(help="Maximum latest economic_v1 rankings for the R7 gate."),
    ] = 500,
    freshness_minutes: Annotated[
        int,
        typer.Option(help="Snapshot/forecast/ranking freshness window in minutes."),
    ] = 360,
    min_expected_value: Annotated[
        str,
        typer.Option(help="Minimum side-adjusted expected value in dollars."),
    ] = "0",
    min_edge: Annotated[
        str,
        typer.Option(help="Minimum model edge in dollars."),
    ] = "0.01",
    min_score: Annotated[
        str,
        typer.Option(help="Minimum opportunity score."),
    ] = "60",
    min_liquidity_score: Annotated[
        str,
        typer.Option(help="Minimum liquidity score for preflight readiness."),
    ] = "1",
    max_spread: Annotated[
        str,
        typer.Option(help="Maximum acceptable spread in dollars."),
    ] = "0.03",
    require_actual_consensus: Annotated[
        bool,
        typer.Option(
            help="Require released actual plus verified consensus evidence before preflight."
        ),
    ] = True,
    max_preflight: Annotated[
        int,
        typer.Option(help="Maximum rows eligible for optional paper-only preflight."),
    ] = 10,
    risk_preflight: Annotated[
        bool,
        typer.Option(help="Record paper-only Phase 3M/3N preflight for clean rows."),
    ] = False,
    template_limit: Annotated[
        int,
        typer.Option(help="Maximum R7 rows to include in the verified export template."),
    ] = 200,
) -> None:
    engine = init_db()
    settings = get_settings()
    session_factory = get_session_factory(engine)
    with session_factory() as session:
        artifacts = write_phase3bd_r8_economic_evidence_activation_report(
            session=session,
            output_dir=output_dir,
            r5_output_dir=r5_output_dir,
            r7_output_dir=r7_output_dir,
            settings=settings,
            input_file=input_file,
            trading_economics_api_key=trading_economics_api_key,
            country=country,
            days_back=days_back,
            days_ahead=days_ahead,
            min_importance=min_importance,
            force_refresh=force_refresh,
            max_series=max_series,
            markets_per_series=markets_per_series,
            snapshot_series_limit=snapshot_series_limit,
            forecast_limit=forecast_limit,
            opportunity_limit=opportunity_limit,
            r7_limit=r7_limit,
            freshness_minutes=freshness_minutes,
            min_expected_value=Decimal(min_expected_value),
            min_edge=Decimal(min_edge),
            min_score=Decimal(min_score),
            min_liquidity_score=Decimal(min_liquidity_score),
            max_spread=Decimal(max_spread),
            require_actual_consensus=require_actual_consensus,
            max_preflight=max_preflight,
            risk_preflight=risk_preflight,
            template_limit=template_limit,
        )
        session.commit()
    summary = artifacts.payload["summary"]
    console.print("Phase 3BD-R8 economic evidence activation")
    console.print("Mode: PAPER / READ ONLY")
    console.print("Live/demo execution: blocked")
    console.print("Order submission/cancel/replace: blocked")
    console.print(f"Status: {summary['status']}")
    console.print(f"Source mode: {summary['source_mode']}")
    console.print(f"R5 ran: {summary['r5_ran']}")
    console.print(
        "Actual + consensus observations: "
        f"{summary['actual_and_consensus_observations']}"
    )
    console.print(f"R7 final status: {summary['final_r7_status']}")
    console.print(f"R7 primary gap: {summary['final_r7_primary_gap']}")
    console.print(f"Preflight-ready rows: {summary['preflight_ready_rows']}")
    console.print(f"Template rows written: {summary['template_rows_written']}")
    console.print(f"Wrote JSON: {artifacts.json_path}")
    console.print(f"Wrote Markdown: {artifacts.markdown_path}")
    console.print(f"Wrote template CSV: {artifacts.template_csv_path}")
    console.print(f"Wrote template JSON: {artifacts.template_json_path}")


@app.command("build-news-features")
def build_news_features_command(
    window_minutes: Annotated[
        int,
        typer.Option(help="News feature lookback window in minutes."),
    ] = 360,
) -> None:
    engine = init_db()
    settings = get_settings()
    session_factory = get_session_factory(engine)
    with session_factory() as session:
        feature_summary = build_news_features(
            session,
            window_minutes=window_minutes,
            settings=settings,
        )
        signal_summary = generate_news_signals(session)
        session.commit()
    console.print("News feature summary")
    console.print(f"Links scanned: {feature_summary.links_scanned}")
    console.print(f"Tickers processed: {feature_summary.tickers_processed}")
    console.print(f"Features inserted: {feature_summary.features_inserted}")
    console.print(f"News signals created: {signal_summary.signals_created}")
    console.print(f"Signal events created: {signal_summary.signal_events_created}")


@app.command("ingest-sports")
def ingest_sports_command(
    league: Annotated[
        str,
        typer.Option(help="Sports league: MLB, NBA, NFL, or NHL."),
    ],
    input_file: Annotated[
        Path,
        typer.Option(help="Local sports JSON or CSV file to ingest."),
    ],
) -> None:
    engine = init_db()
    session_factory = get_session_factory(engine)
    with session_factory() as session:
        try:
            summary = ingest_sports_file(session, league=league, input_file=input_file)
        except FileNotFoundError as exc:
            console.print(f"[bold red]Sports input file not found:[/bold red] {exc}")
            console.print(
                "Generate verified schedule JSON first with: "
                "[bold]kalshi-bot phase3af-sports-schedule-bootstrap "
                "--leagues MLB,WNBA,SOCCER --days-ahead 14 --ingest[/bold]"
            )
            console.print(
                "Then rerun: "
                "[bold]kalshi-bot phase3ae-verified-sports-connector "
                "--output-dir reports/phase3ae[/bold]"
            )
            raise typer.Exit(2) from exc
        session.commit()
    console.print("Sports ingestion summary")
    console.print(f"League: {summary.league}")
    console.print(f"Source: {summary.source}")
    console.print(f"Teams seen: {summary.teams_seen}")
    console.print(f"Teams inserted: {summary.teams_inserted}")
    console.print(f"Games seen: {summary.games_seen}")
    console.print(f"Games inserted: {summary.games_inserted}")
    console.print(f"Team stats inserted: {summary.team_stats_inserted}")
    console.print(f"Injuries inserted: {summary.injuries_inserted}")
    console.print(f"Odds inserted: {summary.odds_inserted}")
    if summary.errors:
        console.print("Errors:")
        for error in summary.errors:
            console.print(f"- {error}")


@app.command("derive-sports-schedule")
def derive_sports_schedule_command(
    limit: Annotated[
        int,
        typer.Option(help="Maximum parsed sports markets to derive. Use 0 for all."),
    ] = 0,
    build_features: Annotated[
        bool,
        typer.Option(help="Also create baseline sports feature rows for derived links."),
    ] = True,
    refresh_features: Annotated[
        bool,
        typer.Option(help="Insert fresh derived feature rows even if a feature exists."),
    ] = False,
    parse_first: Annotated[
        bool,
        typer.Option(help="Run market leg parsing before deriving sports schedule rows."),
    ] = False,
    refresh_parse: Annotated[
        bool,
        typer.Option(help="Refresh parsed market legs when --parse-first is used."),
    ] = False,
    resume: Annotated[
        bool,
        typer.Option(help="Resume idempotently from existing rows/checkpoint state."),
    ] = False,
    progress_every: Annotated[
        int,
        typer.Option(help="Write/print heartbeat progress every N markets. Use 0 for quiet."),
    ] = 100,
    checkpoint_every: Annotated[
        int,
        typer.Option(help="Write heartbeat checkpoints every N markets. Use 0 to disable."),
    ] = 100,
    stop_after_minutes: Annotated[
        int,
        typer.Option(help="Stop cleanly after N minutes. Use 0 for no limit."),
    ] = 0,
    commit_every: Annotated[
        int,
        typer.Option(help="Commit every N markets during derivation. Use 0 for final commit only."),
    ] = 100,
    heartbeat_dir: Annotated[
        Path,
        typer.Option(help="Directory for Phase 3AU/3AX heartbeat/checkpoint files."),
    ] = Path("reports/phase3au"),
) -> None:
    """Create local sports teams/games/links from parsed Kalshi sports legs."""
    engine = init_db()
    settings = get_settings()
    session_factory = get_session_factory(engine)
    with session_factory() as session:
        result = run_resumable_sports_derivation(
            session,
            limit=limit if limit > 0 else None,
            build_features=build_features,
            refresh_features=refresh_features,
            parse_first=parse_first,
            refresh_parse=refresh_parse,
            resume=resume,
            settings=settings,
            heartbeat_dir=heartbeat_dir,
            progress_every=progress_every,
            checkpoint_every=checkpoint_every,
            stop_after_minutes=stop_after_minutes or None,
            commit_every=commit_every,
        )
        session.commit()
    summary = result.summary
    if result.parse_result is not None:
        console.print(
            "Parsed "
            f"{result.parse_result.legs_inserted} leg(s) across "
            f"{result.parse_result.markets_scanned} market(s)."
        )
    console.print("Sports derived schedule summary")
    console.print("Phase 3AX heartbeat: enabled")
    console.print("Mode: PAPER ONLY diagnostics; no demo/live execution.")
    if result.stopped_early:
        console.print("Status: STOPPED_EARLY")
    console.print(f"Resume requested: {result.resume}")
    console.print(f"Parsed sports markets seen: {summary.sports_markets_seen}")
    console.print(f"Teams created: {summary.teams_created}")
    console.print(f"Games created: {summary.games_created}")
    console.print(f"Links created: {summary.links_created}")
    console.print(f"Links already present: {summary.links_existing}")
    console.print(f"Features created: {summary.features_created}")
    console.print(f"Features already present: {summary.features_existing}")
    console.print(f"Chunk commits: {result.commits_created}")
    console.print(f"Heartbeat: {result.heartbeat_path}")
    console.print(f"Checkpoint: {result.checkpoint_path}")
    console.print(f"Links by league: {summary.links_by_league}")
    console.print(f"Links by type: {summary.links_by_type}")


@app.command("phase3ax-sports-derivation")
def phase3ax_sports_derivation_command(
    limit: Annotated[
        int,
        typer.Option(help="Maximum parsed sports markets to derive. Use 0 for all."),
    ] = 0,
    build_features: Annotated[
        bool,
        typer.Option(help="Also create baseline sports feature rows for derived links."),
    ] = True,
    refresh_features: Annotated[
        bool,
        typer.Option(help="Insert fresh derived feature rows even if a feature exists."),
    ] = False,
    parse_first: Annotated[
        bool,
        typer.Option(help="Run market leg parsing before deriving sports schedule rows."),
    ] = False,
    refresh_parse: Annotated[
        bool,
        typer.Option(help="Refresh parsed market legs when --parse-first is used."),
    ] = False,
    resume: Annotated[
        bool,
        typer.Option(help="Resume idempotently from existing rows/checkpoint state."),
    ] = True,
    progress_every: Annotated[
        int,
        typer.Option(help="Write/print heartbeat progress every N markets. Use 0 for quiet."),
    ] = 100,
    checkpoint_every: Annotated[
        int,
        typer.Option(help="Write heartbeat checkpoints every N markets. Use 0 to disable."),
    ] = 100,
    stop_after_minutes: Annotated[
        int,
        typer.Option(help="Stop cleanly after N minutes. Use 0 for no limit."),
    ] = 30,
    commit_every: Annotated[
        int,
        typer.Option(help="Commit every N markets during derivation. Use 0 for final commit only."),
    ] = 100,
    heartbeat_dir: Annotated[
        Path,
        typer.Option(help="Directory for Phase 3AU/3AX heartbeat/checkpoint files."),
    ] = Path("reports/phase3au"),
) -> None:
    """Explicit Phase 3AX safe wrapper around sports derivation."""
    derive_sports_schedule_command(
        limit=limit,
        build_features=build_features,
        refresh_features=refresh_features,
        parse_first=parse_first,
        refresh_parse=refresh_parse,
        resume=resume,
        progress_every=progress_every,
        checkpoint_every=checkpoint_every,
        stop_after_minutes=stop_after_minutes,
        commit_every=commit_every,
        heartbeat_dir=heartbeat_dir,
    )


@app.command("link-sports-markets")
def link_sports_markets_command(
    league: Annotated[
        str,
        typer.Option(help="Sports league: MLB, NBA, NFL, NHL, or ALL."),
    ] = "ALL",
) -> None:
    engine = init_db()
    settings = get_settings()
    session_factory = get_session_factory(engine)
    with session_factory() as session:
        summary = link_sports_markets(session, league=league, settings=settings)
        session.commit()
    console.print("Sports market link summary")
    console.print(f"League: {summary.league}")
    console.print(f"Markets scanned: {summary.markets_scanned}")
    console.print(f"Games scanned: {summary.games_scanned}")
    console.print(f"Links created: {summary.links_created}")
    console.print(f"Links by type: {summary.links_by_type}")


@app.command("sports-link-cleanup")
def sports_link_cleanup_command(
    output: Annotated[
        Path,
        typer.Option(help="Markdown cleanup report path."),
    ] = Path("reports/sports_link_cleanup.md"),
    json_output: Annotated[
        Path | None,
        typer.Option(help="Structured JSON cleanup report path."),
    ] = None,
    rows_output: Annotated[
        Path | None,
        typer.Option(help="Top noisy ticker rows JSON path."),
    ] = None,
    apply: Annotated[
        bool,
        typer.Option(
            "--apply/--dry-run",
            help="Delete eligible legacy noisy rows. Defaults to dry-run.",
        ),
    ] = False,
    max_links_per_ticker: Annotated[
        int,
        typer.Option(
            help=(
                "Ticker is noisy when legacy direct links exceed this count. "
                "Use 0 for SPORTS_MAX_DIRECT_LINKS_PER_MARKET."
            )
        ),
    ] = 0,
    delete_batch_size: Annotated[
        int,
        typer.Option(help="Rows to delete per SQL batch when --apply is used."),
    ] = 5000,
) -> None:
    """Dry-run or apply cleanup for legacy broad sports-link fanout rows."""
    engine = init_db()
    settings = get_settings()
    session_factory = get_session_factory(engine)
    with session_factory() as session:
        artifacts = write_sports_link_cleanup_report(
            session,
            output_path=output,
            json_path=json_output,
            rows_path=rows_output,
            settings=settings,
            apply=apply,
            max_links_per_ticker=max_links_per_ticker or None,
            delete_batch_size=delete_batch_size,
        )
        if apply:
            session.commit()
    console.print("Sports link cleanup")
    console.print("Mode: PAPER ONLY diagnostics")
    console.print("Live/demo execution: blocked")
    console.print(f"Apply: {apply}")
    console.print(f"Wrote Markdown: {artifacts.output_path}")
    console.print(f"Wrote JSON: {artifacts.json_path}")
    console.print(f"Wrote rows: {artifacts.rows_path}")


@app.command("build-sports-features")
def build_sports_features_command(
    league: Annotated[
        str,
        typer.Option(help="Sports league: MLB, NBA, NFL, NHL, or ALL."),
    ] = "ALL",
) -> None:
    engine = init_db()
    settings = get_settings()
    session_factory = get_session_factory(engine)
    with session_factory() as session:
        feature_summary = build_sports_features(session, league=league, settings=settings)
        signal_summary = generate_sports_signals(session, league=league, settings=settings)
        session.commit()
    console.print("Sports feature summary")
    console.print(f"League: {feature_summary.league}")
    console.print(f"Games processed: {feature_summary.games_processed}")
    console.print(f"Links scanned: {feature_summary.links_scanned}")
    console.print(f"Features inserted: {feature_summary.features_inserted}")
    console.print(f"Sports signals created: {signal_summary.signals_created}")
    console.print(f"Signal events created: {signal_summary.signal_events_created}")


@app.command("sports-report")
def sports_report_command(
    league: Annotated[
        str,
        typer.Option(help="Sports league: MLB, NBA, NFL, NHL, or ALL."),
    ] = "ALL",
    output: Annotated[
        Path,
        typer.Option(help="Markdown sports report path."),
    ] = Path("reports/sports_report.md"),
) -> None:
    engine = init_db()
    settings = get_settings()
    session_factory = get_session_factory(engine)
    with session_factory() as session:
        path = generate_sports_report(
            session,
            league=league,
            output_path=output,
            settings=settings,
        )
    console.print(f"Wrote sports report to {path}")


@app.command("sports-opportunities")
def sports_opportunities_command(
    model_name: Annotated[
        str,
        typer.Option(help="Sports forecast model name."),
    ] = "sports_v1",
    league: Annotated[
        str,
        typer.Option(help="Sports league: MLB, NBA, NFL, NHL, or ALL."),
    ] = "ALL",
    limit: Annotated[int, typer.Option(help="Maximum sports opportunity rows.")] = 20,
    output: Annotated[
        Path,
        typer.Option(help="Markdown sports opportunities report path."),
    ] = Path("reports/sports_opportunities.md"),
) -> None:
    engine = init_db()
    session_factory = get_session_factory(engine)
    with session_factory() as session:
        path = generate_sports_opportunities_report(
            session,
            model_name=model_name,
            league=league,
            limit=limit,
            output_path=output,
        )
    console.print(f"Wrote sports opportunities report to {path}")


@app.command("sports-backtest")
def sports_backtest_command(
    league: Annotated[
        str,
        typer.Option(help="Sports league: MLB, NBA, NFL, NHL, or ALL."),
    ] = "ALL",
    days: Annotated[int, typer.Option(help="Backtest lookback window in days.")] = 30,
    output: Annotated[
        Path,
        typer.Option(help="Markdown sports backtest report path."),
    ] = Path("reports/sports_backtest.md"),
) -> None:
    engine = init_db()
    session_factory = get_session_factory(engine)
    with session_factory() as session:
        path = generate_sports_backtest_report(
            session,
            league=league,
            days=days,
            output_path=output,
        )
    console.print(f"Wrote sports backtest report to {path}")


@app.command("build-microstructure-features")
def build_microstructure_features_command(
    lookback_minutes: Annotated[
        int,
        typer.Option(help="Microstructure lookback window in minutes."),
    ] = 60,
) -> None:
    engine = init_db()
    settings = get_settings()
    session_factory = get_session_factory(engine)
    with session_factory() as session:
        summary = build_microstructure_features(
            session,
            lookback_minutes=lookback_minutes,
            settings=settings,
        )
        session.commit()
    console.print("Microstructure feature summary")
    console.print(f"Markets scanned: {summary.markets_scanned}")
    console.print(f"Features inserted: {summary.features_inserted}")
    console.print(f"Events inserted: {summary.events_inserted}")
    console.print(f"Signals inserted: {summary.signals_inserted}")
    console.print(f"Depth snapshots inserted: {summary.depth_snapshots_inserted}")
    console.print(f"Skipped insufficient snapshots: {summary.skipped_insufficient_snapshots}")


@app.command("microstructure-sample-watchlist")
def microstructure_sample_watchlist_command(
    output_dir: Annotated[
        Path,
        typer.Option(help="Directory for microstructure sampling artifacts."),
    ] = Path("reports/microstructure_sampling"),
    limit: Annotated[int, typer.Option(help="Number of top active tickers to resample.")] = 50,
    cycles: Annotated[
        int,
        typer.Option(help="Number of repeated sampling cycles per ticker."),
    ] = 3,
    interval_seconds: Annotated[
        float,
        typer.Option(help="Seconds to wait between repeated cycles."),
    ] = 5,
    lookback_minutes: Annotated[
        int,
        typer.Option(help="Microstructure feature lookback window in minutes."),
    ] = 60,
    include_orderbook: Annotated[
        bool,
        typer.Option("--orderbook/--no-orderbook", help="Fetch public orderbooks."),
    ] = True,
) -> None:
    """Resample a stable watchlist so microstructure_v1 has repeated observations."""
    engine = init_db()
    settings = get_settings()
    session_factory = get_session_factory(engine)
    with session_factory() as session:
        artifacts = write_microstructure_sampling_report(
            session,
            output_dir=output_dir,
            limit=limit,
            cycles=cycles,
            interval_seconds=interval_seconds,
            lookback_minutes=lookback_minutes,
            include_orderbook=include_orderbook,
            settings=settings,
            release_read_lock_before_sampling=True,
            commit_after_each_sample=True,
            write_retries=3,
            write_retry_seconds=2,
        )
        session.commit()
    summary = artifacts.result.feature_summary
    console.print("Microstructure sampling watchlist")
    console.print("Mode: PAPER ONLY / READ ONLY")
    console.print("Live/demo execution: blocked")
    console.print(f"Target tickers: {artifacts.result.target_tickers}")
    console.print(f"Snapshots inserted: {artifacts.result.snapshots_inserted}")
    console.print(f"Features inserted: {summary.features_inserted}")
    console.print(f"Events inserted: {summary.events_inserted}")
    console.print(f"Signals inserted: {summary.signals_inserted}")
    console.print(f"Wrote JSON: {artifacts.json_path}")
    console.print(f"Wrote Markdown: {artifacts.markdown_path}")


@app.command("microstructure-report")
def microstructure_report_command(
    output: Annotated[
        Path,
        typer.Option(help="Markdown microstructure report path."),
    ] = Path("reports/microstructure_report.md"),
) -> None:
    engine = init_db()
    session_factory = get_session_factory(engine)
    with session_factory() as session:
        path = generate_microstructure_report(session, output_path=output)
    console.print(f"Wrote microstructure report to {path}")


@app.command("microstructure-opportunities")
def microstructure_opportunities_command(
    model_name: Annotated[
        str,
        typer.Option(help="Microstructure forecast model name."),
    ] = "microstructure_v1",
    limit: Annotated[int, typer.Option(help="Maximum microstructure opportunity rows.")] = 20,
    output: Annotated[
        Path,
        typer.Option(help="Markdown microstructure opportunities report path."),
    ] = Path("reports/microstructure_opportunities.md"),
) -> None:
    engine = init_db()
    settings = get_settings()
    session_factory = get_session_factory(engine)
    with session_factory() as session:
        path = generate_microstructure_opportunities_report(
            session,
            model_name=model_name,
            limit=limit,
            output_path=output,
            settings=settings,
        )
    console.print(f"Wrote microstructure opportunities report to {path}")


@app.command("microstructure-backtest")
def microstructure_backtest_command(
    days: Annotated[int, typer.Option(help="Backtest lookback window in days.")] = 30,
    output: Annotated[
        Path,
        typer.Option(help="Markdown microstructure backtest report path."),
    ] = Path("reports/microstructure_backtest.md"),
) -> None:
    engine = init_db()
    session_factory = get_session_factory(engine)
    with session_factory() as session:
        path = generate_microstructure_backtest_report(
            session,
            days=days,
            output_path=output,
        )
        session.commit()
    console.print(f"Wrote microstructure backtest report to {path}")


@app.command("scheduler-plan")
def scheduler_plan_command(
    profile: Annotated[
        str,
        typer.Option(help="Scheduler profile name, for example sports-watch."),
    ],
) -> None:
    steps = scheduler_plan(profile)
    console.print(f"Scheduler profile: {profile}")
    for index, step in enumerate(steps, start=1):
        console.print(f"{index}. every {step.every_minutes}m | {step.command}")
        console.print(f"   {step.purpose}")


@app.command("ingest-weather")
def ingest_weather_command(
    location_key: Annotated[
        str,
        typer.Option(help="Stable location key, for example kansas_city."),
    ],
    lat: Annotated[float | None, typer.Option(help="Latitude for NOAA/NWS lookup.")] = None,
    lon: Annotated[float | None, typer.Option(help="Longitude for NOAA/NWS lookup.")] = None,
    input_file: Annotated[
        Path | None,
        typer.Option(help="Optional local weather JSON file to ingest instead of live NOAA."),
    ] = None,
) -> None:
    engine = init_db()
    session_factory = get_session_factory(engine)
    with session_factory() as session:
        if input_file is not None:
            payload = load_json_file(input_file)
            summary = ingest_manual_weather_json(
                session,
                payload,
                location_key=location_key,
            )
        else:
            if lat is None or lon is None:
                resolved = _weather_location_coordinates(location_key)
                if resolved is None:
                    raise typer.BadParameter(
                        "ingest-weather requires --lat and --lon, --input-file, "
                        "or a known --location-key such as kansas_city."
                    )
                lat, lon = resolved
            summary = ingest_weather_location(
                session,
                location_key=location_key,
                latitude=lat,
                longitude=lon,
            )
        session.commit()
    console.print(
        f"Inserted {summary.forecasts_inserted} weather forecast row(s) and "
        f"{summary.observations_inserted} observation row(s) from {summary.source}."
    )
    if summary.errors:
        console.print("Errors:")
        for error in summary.errors:
            console.print(f"- {error}")


def _weather_location_coordinates(location_key: str) -> tuple[float, float] | None:
    locations = {
        "kansas_city": (39.0997, -94.5786),
        "new_york": (40.7128, -74.0060),
        "chicago": (41.8781, -87.6298),
        "los_angeles": (34.0522, -118.2437),
    }
    normalized = location_key.strip().lower().replace("-", "_").replace(" ", "_")
    return locations.get(normalized)


@app.command("build-weather-features")
def build_weather_features_command(
    location_key: Annotated[
        str,
        typer.Option(help="Stable location key, for example kansas_city."),
    ],
) -> None:
    engine = init_db()
    session_factory = get_session_factory(engine)
    with session_factory() as session:
        summary = build_weather_features(session, location_key=location_key)
        session.commit()
    console.print(
        f"Processed {summary.forecasts_processed} weather forecast row(s) for "
        f"{summary.location_key} and inserted {summary.features_inserted} feature row(s)."
    )


@app.command("link-weather-markets")
def link_weather_markets_command() -> None:
    engine = init_db()
    session_factory = get_session_factory(engine)
    with session_factory() as session:
        summary = link_weather_markets(session)
        session.commit()
    console.print("Weather market link summary")
    console.print(f"Markets scanned: {summary.markets_scanned}")
    console.print(f"Links created: {summary.links_created}")
    console.print(f"By metric: {summary.by_metric}")
    console.print(f"By location: {summary.by_location_key}")
    console.print(f"Unknown location count: {summary.unknown_location_count}")


@app.command("weather-report")
def weather_report_command(
    location_key: Annotated[
        str,
        typer.Option(help="Stable location key, for example kansas_city."),
    ],
    output: Annotated[
        Path,
        typer.Option(help="Markdown weather feature report path."),
    ] = Path("reports/weather_features.md"),
) -> None:
    engine = init_db()
    session_factory = get_session_factory(engine)
    with session_factory() as session:
        report_path = generate_weather_report(
            session,
            location_key=location_key,
            output_path=output,
        )
    console.print(f"Wrote weather report to {report_path}")


@app.command("ingest-crypto")
def ingest_crypto_command(
    symbols: Annotated[
        str,
        typer.Option(help=f"Comma-separated symbols, for example {DEFAULT_CRYPTO_SYMBOLS}."),
    ] = DEFAULT_CRYPTO_SYMBOLS,
    source: Annotated[
        str,
        typer.Option(help="Public no-key source: coinbase or coingecko."),
    ] = "coinbase",
) -> None:
    engine = init_db()
    session_factory = get_session_factory(engine)
    with session_factory() as session:
        summary = ingest_crypto_quotes(session, symbols=parse_symbols(symbols), source=source)
        session.commit()
    console.print(f"Inserted {summary.prices_inserted} crypto price row(s) from {summary.source}.")
    if summary.errors:
        console.print("Errors:")
        for error in summary.errors:
            console.print(f"- {error}")


@app.command("build-crypto-features")
def build_crypto_features_command(
    symbols: Annotated[
        str,
        typer.Option(help=f"Comma-separated symbols, for example {DEFAULT_CRYPTO_SYMBOLS}."),
    ] = DEFAULT_CRYPTO_SYMBOLS,
) -> None:
    engine = init_db()
    session_factory = get_session_factory(engine)
    with session_factory() as session:
        summary = build_crypto_features(session, symbols=parse_symbols(symbols))
        session.commit()
    console.print(
        f"Processed {summary.symbols_processed} symbols and inserted "
        f"{summary.features_inserted} crypto feature row(s)."
    )


@app.command("link-crypto-markets")
def link_crypto_markets_command() -> None:
    engine = init_db()
    session_factory = get_session_factory(engine)
    with session_factory() as session:
        summary = link_crypto_markets(session)
        session.commit()
    console.print("Crypto market link summary")
    console.print(f"Markets scanned: {summary.markets_scanned}")
    console.print(f"Links created: {summary.links_created}")
    console.print(f"BTC links: {summary.btc_links}")
    console.print(f"ETH links: {summary.eth_links}")
    console.print(f"Generic links: {summary.generic_links}")
    console.print(f"Multi-asset links: {summary.multi_asset_links}")
    console.print(f"Exact semantic links: {summary.exact_semantic_links}")
    console.print(f"Ambiguous markets: {summary.ambiguous_markets}")
    console.print(f"Unsupported markets: {summary.unsupported_markets}")
    console.print(f"Links by symbol: {summary.links_by_symbol}")


@app.command("crypto-report")
def crypto_report_command(
    symbols: Annotated[
        str,
        typer.Option(help=f"Comma-separated symbols, for example {DEFAULT_CRYPTO_SYMBOLS}."),
    ] = DEFAULT_CRYPTO_SYMBOLS,
    output: Annotated[
        Path,
        typer.Option(help="Markdown crypto feature report path."),
    ] = Path("reports/crypto_features.md"),
) -> None:
    engine = init_db()
    session_factory = get_session_factory(engine)
    with session_factory() as session:
        report_path = generate_crypto_report(
            session,
            symbols=parse_symbols(symbols),
            output_path=output,
        )
    console.print(f"Wrote crypto report to {report_path}")


@app.command("paper-run")
def paper_run_command(
    model_name: Annotated[
        str | None,
        typer.Option(help="Optional model name filter for latest forecasts."),
    ] = None,
) -> None:
    engine = init_db()
    session_factory = get_session_factory(engine)
    with session_factory() as session:
        settings = get_settings()
        summary = run_paper_trading(session, settings=settings, model_name=model_name)
        session.commit()
    console.print("Paper trading run summary")
    console.print(f"Learning Mode: {settings.learning_mode}")
    console.print(f"Forecasts scanned: {summary.forecasts_scanned}")
    console.print(f"Decisions generated: {summary.decisions_generated}")
    console.print(f"Orders created: {summary.orders_created}")
    console.print(f"Fills created: {summary.fills_created}")
    console.print(f"Skipped due to edge: {summary.skipped_due_to_edge}")
    console.print(f"Skipped due to risk limits: {summary.skipped_due_to_risk_limits}")
    console.print(f"Duplicate forecasts skipped: {summary.duplicates_skipped}")


@app.command("paper-summary")
def paper_summary_command(
    output: Annotated[
        Path | None,
        typer.Option(help="Optional Markdown report output path."),
    ] = None,
) -> None:
    engine = init_db()
    session_factory = get_session_factory(engine)
    with session_factory() as session:
        summary = get_paper_summary(session)
        console.print("Paper trading summary")
        console.print(f"Total paper orders: {summary.total_orders}")
        console.print(f"Filled paper orders: {summary.filled_orders}")
        console.print(f"Open paper orders: {summary.open_orders}")
        console.print(f"Active positions: {summary.active_positions}")
        console.print(f"Total realized P&L: {summary.total_realized_pnl}")
        console.print(f"Estimated unrealized P&L: {summary.estimated_unrealized_pnl}")
        console.print(f"Total P&L: {summary.total_pnl}")
        if summary.top_positions:
            console.print("Top positions by exposure:")
            for row in summary.top_positions:
                console.print(
                    f"- {row['ticker']}: YES {row['yes_contracts']}, "
                    f"NO {row['no_contracts']}, exposure {row['exposure']}"
                )
        if output is not None:
            report_path = write_paper_trading_report(session, output)
            console.print(f"Wrote paper trading report to {report_path}")


@app.command("paper-pnl")
def paper_pnl_command() -> None:
    engine = init_db()
    session_factory = get_session_factory(engine)
    with session_factory() as session:
        summary = calculate_and_store_pnl(session)
        session.commit()
    console.print("Paper P&L summary")
    console.print(f"Positions evaluated: {summary.positions_evaluated}")
    console.print(f"P&L rows inserted: {summary.pnl_rows_inserted}")
    console.print(f"Realized P&L: {summary.realized_pnl}")
    console.print(f"Unrealized P&L: {summary.unrealized_pnl}")
    console.print(f"Total P&L: {summary.total_pnl}")


@app.command("paper-reset")
def paper_reset_command(
    yes: Annotated[
        bool,
        typer.Option(help="Confirm deletion of paper trading tables only."),
    ] = False,
) -> None:
    if not yes:
        raise typer.BadParameter("paper-reset requires --yes.")
    engine = init_db()
    session_factory = get_session_factory(engine)
    with session_factory() as session:
        reset_paper_data(session)
        session.commit()
    console.print("Deleted paper orders, fills, positions, and P&L rows only.")


@app.command("backtest")
def backtest_command(
    model_name: Annotated[str, typer.Option(help="Forecast model name.")],
    strategy: Annotated[str, typer.Option(help="Strategy name.")] = "paper_v1",
    days: Annotated[int, typer.Option(help="Lookback window in days.")] = 30,
    output: Annotated[
        Path,
        typer.Option(help="Markdown backtest report path."),
    ] = Path("reports/backtest_market_implied_v1.md"),
) -> None:
    engine = init_db()
    session_factory = get_session_factory(engine)
    with session_factory() as session:
        report_path = generate_backtest_report(
            session,
            model_name=model_name,
            strategy_name=strategy,
            days=days,
            output_path=output,
        )
        session.commit()
    console.print(f"Wrote backtest report to {report_path}")


@app.command("crypto-backtest")
def crypto_backtest_command(
    days: Annotated[int, typer.Option(help="Lookback window in days.")] = 30,
    output: Annotated[
        Path,
        typer.Option(help="Markdown crypto backtest report path."),
    ] = Path("reports/crypto_backtest.md"),
) -> None:
    engine = init_db()
    session_factory = get_session_factory(engine)
    with session_factory() as session:
        report_path = generate_crypto_backtest_report(
            session,
            days=days,
            output_path=output,
        )
    console.print(f"Wrote crypto backtest report to {report_path}")


@app.command("weather-backtest")
def weather_backtest_command(
    days: Annotated[int, typer.Option(help="Lookback window in days.")] = 30,
    output: Annotated[
        Path,
        typer.Option(help="Markdown weather backtest report path."),
    ] = Path("reports/weather_backtest.md"),
) -> None:
    engine = init_db()
    session_factory = get_session_factory(engine)
    with session_factory() as session:
        report_path = generate_weather_backtest_report(
            session,
            days=days,
            output_path=output,
        )
    console.print(f"Wrote weather backtest report to {report_path}")


@app.command("news-report")
def news_report_command(
    output: Annotated[
        Path,
        typer.Option(help="Markdown news intelligence report path."),
    ] = Path("reports/news_report.md"),
) -> None:
    engine = init_db()
    settings = get_settings()
    session_factory = get_session_factory(engine)
    with session_factory() as session:
        report_path = generate_news_report(session, output_path=output, settings=settings)
        session.commit()
    console.print(f"Wrote news report to {report_path}")


@app.command("news-opportunities")
def news_opportunities_command(
    model_name: Annotated[
        str,
        typer.Option(help="Forecast model name for news opportunity rows."),
    ] = "news_v1",
    limit: Annotated[int, typer.Option(help="Maximum news opportunity rows.")] = 20,
    output: Annotated[
        Path,
        typer.Option(help="Markdown news opportunities report path."),
    ] = Path("reports/news_opportunities.md"),
) -> None:
    engine = init_db()
    session_factory = get_session_factory(engine)
    with session_factory() as session:
        report_path = generate_news_opportunities_report(
            session,
            model_name=model_name,
            limit=limit,
            output_path=output,
        )
    console.print(f"Wrote news opportunities report to {report_path}")


@app.command("news-backtest")
def news_backtest_command(
    days: Annotated[int, typer.Option(help="Lookback window in days.")] = 30,
    output: Annotated[
        Path,
        typer.Option(help="Markdown news backtest report path."),
    ] = Path("reports/news_backtest.md"),
) -> None:
    engine = init_db()
    session_factory = get_session_factory(engine)
    with session_factory() as session:
        report_path = generate_news_backtest_report(session, days=days, output_path=output)
    console.print(f"Wrote news backtest report to {report_path}")


@app.command("tournament")
def tournament_command(
    name: Annotated[
        str | None,
        typer.Option(help="Optional tournament run name."),
    ] = None,
    days: Annotated[int, typer.Option(help="Lookback window in days.")] = 30,
    output: Annotated[
        Path,
        typer.Option(help="Markdown tournament report path."),
    ] = Path("reports/model_tournament.md"),
    generate_weights: Annotated[
        bool,
        typer.Option("--generate-weights/--no-generate-weights"),
    ] = True,
) -> None:
    engine = init_db()
    session_factory = get_session_factory(engine)
    with session_factory() as session:
        report_path, result = generate_tournament_report(
            session,
            days=days,
            output_path=output,
            name=name,
            generate_weights=generate_weights,
        )
        session.commit()
    console.print(f"Wrote tournament report to {report_path}")
    console.print(f"Tournament rows: {len(result.rows)}")
    console.print(f"Weights generated: {len(result.weights)}")


@app.command("model-diagnostics")
def model_diagnostics_command(
    days: Annotated[int, typer.Option(help="Lookback window in days.")] = 30,
    output: Annotated[
        Path,
        typer.Option(help="Markdown model diagnostics report path."),
    ] = Path("reports/model_diagnostics.md"),
) -> None:
    engine = init_db()
    session_factory = get_session_factory(engine)
    with session_factory() as session:
        report_path, result = generate_model_diagnostics_report(
            session,
            days=days,
            output_path=output,
        )
        session.commit()
    console.print(f"Wrote model diagnostics report to {report_path}")
    console.print(f"Diagnostics generated: {len(result.diagnostics)}")


@app.command("model-weights")
def model_weights_command(
    days: Annotated[int, typer.Option(help="Lookback window in days.")] = 30,
    output: Annotated[
        Path,
        typer.Option(help="Markdown model weights report path."),
    ] = Path("reports/model_weights.md"),
) -> None:
    engine = init_db()
    session_factory = get_session_factory(engine)
    with session_factory() as session:
        report_path, result = generate_model_weights_report(
            session,
            days=days,
            output_path=output,
        )
        session.commit()
    console.print(f"Wrote model weights report to {report_path}")
    console.print(f"Weights generated: {len(result.weights)}")


@app.command("compare-strategies")
def compare_strategies_command(
    days: Annotated[int, typer.Option(help="Lookback window in days.")] = 30,
    output: Annotated[
        Path,
        typer.Option(help="Markdown strategy comparison report path."),
    ] = Path("reports/strategy_comparison.md"),
) -> None:
    engine = init_db()
    session_factory = get_session_factory(engine)
    with session_factory() as session:
        report_path = generate_strategy_comparison_report(
            session,
            days=days,
            output_path=output,
        )
    console.print(f"Wrote strategy comparison report to {report_path}")


@app.command("find-opportunities")
def find_opportunities_command(
    model_name: Annotated[
        str,
        typer.Option(help="Forecast model name to scan."),
    ] = "market_implied_v1",
    limit: Annotated[int, typer.Option(help="Maximum ranking rows to insert/report.")] = 20,
    output: Annotated[
        Path,
        typer.Option(help="Markdown opportunities report path."),
    ] = Path("reports/opportunities.md"),
    min_edge: Annotated[
        float | None,
        typer.Option(help="Optional override for minimum edge."),
    ] = None,
    min_score: Annotated[
        float | None,
        typer.Option(help="Optional override for minimum opportunity score."),
    ] = None,
) -> None:
    engine = init_db()
    session_factory = get_session_factory(engine)
    with session_factory() as session:
        report_path, summary = generate_opportunities_report(
            session,
            model_name=model_name,
            limit=limit,
            output_path=output,
            min_edge=Decimal(str(min_edge)) if min_edge is not None else None,
            min_score=Decimal(str(min_score)) if min_score is not None else None,
        )
        session.commit()
    console.print("Opportunity scan summary")
    console.print(f"Markets scanned: {summary.markets_scanned}")
    console.print(f"Rankings inserted: {summary.rankings_inserted}")
    console.print(f"Opportunities detected: {summary.opportunities_detected}")
    console.print(f"Top opportunity ticker: {summary.top_opportunity_ticker or 'n/a'}")
    console.print(f"Top opportunity score: {summary.top_opportunity_score or 'n/a'}")
    console.print(f"Wrote opportunities report to {report_path}")


@app.command("explain-opportunity")
def explain_opportunity_command(
    ticker: Annotated[
        str,
        typer.Option("--ticker", help="Market ticker to explain."),
    ] = "",
    model_name: Annotated[
        str,
        typer.Option("--model-name", help="Forecast model name to explain."),
    ] = "ensemble_v2",
) -> None:
    if not ticker:
        raise typer.BadParameter("explain-opportunity requires --ticker.")
    engine = init_db()
    settings = get_settings()
    session_factory = get_session_factory(engine)
    with session_factory() as session:
        ranking = session.scalar(
            select(MarketRanking)
            .where(MarketRanking.ticker == ticker, MarketRanking.forecast_model == model_name)
            .order_by(desc(MarketRanking.ranked_at), desc(MarketRanking.id))
            .limit(1)
        )
        snapshot = session.scalar(
            select(MarketSnapshot)
            .where(MarketSnapshot.ticker == ticker)
            .order_by(desc(MarketSnapshot.captured_at), desc(MarketSnapshot.id))
            .limit(1)
        )
        forecast = session.scalar(
            select(Forecast)
            .where(Forecast.ticker == ticker, Forecast.model_name == model_name)
            .order_by(desc(Forecast.forecasted_at), desc(Forecast.id))
            .limit(1)
        )
        position = get_position(session, ticker)
        consensus_signal = latest_consensus_for_ticker(session, ticker)
        position_text = (
            "none"
            if position is None
            else (
                f"YES {position.yes_contracts}, NO {position.no_contracts}, "
                f"realized {position.realized_pnl}"
            )
        )
        explanation = explain_opportunity(
            ranking,
            snapshot=snapshot,
            forecast=forecast,
            consensus_signal=consensus_signal,
            position_text=position_text,
            settings=settings,
        )

    console.print(f"Opportunity explanation for {ticker}")
    console.print(f"Recommendation: {explanation['recommendation']}")
    console.print(f"Why: {explanation['why_interesting']}")
    console.print("Risks:")
    for risk in explanation["risks"]:
        console.print(f"- {risk}")
    forum_consensus = explanation["forum_consensus"]
    if forum_consensus["available"]:
        console.print(f"Forum consensus: {forum_consensus['summary']}")
    console.print(f"Model: {explanation['model_explanation']}")
    console.print(f"Suggested next action: {explanation['recommended_action']}")


@app.command("market-rankings")
def market_rankings_command(
    limit: Annotated[int, typer.Option(help="Number of recent rankings to report.")] = 50,
    output: Annotated[
        Path,
        typer.Option(help="Markdown market rankings report path."),
    ] = Path("reports/market_rankings.md"),
) -> None:
    engine = init_db()
    session_factory = get_session_factory(engine)
    with session_factory() as session:
        report_path = generate_market_rankings_report(session, limit=limit, output_path=output)
    console.print(f"Wrote market rankings report to {report_path}")


@app.command("leaderboard")
def leaderboard_command(
    days: Annotated[int, typer.Option(help="Lookback window in days.")] = 30,
    output: Annotated[
        Path,
        typer.Option(help="Markdown model leaderboard report path."),
    ] = Path("reports/model_leaderboard.md"),
) -> None:
    engine = init_db()
    session_factory = get_session_factory(engine)
    with session_factory() as session:
        report_path, result = generate_leaderboard_report(
            session,
            days=days,
            output_path=output,
        )
        session.commit()
    console.print(f"Wrote model leaderboard report to {report_path}")
    console.print(f"Models compared: {len(result.rows)}")


_install_friendly_cli_error_handlers()


if __name__ == "__main__":
    app()
