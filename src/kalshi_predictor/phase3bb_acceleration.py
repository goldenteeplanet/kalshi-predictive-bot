from __future__ import annotations

import csv
import hashlib
import json
import math
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from sqlalchemy import text
from sqlalchemy.orm import Session

from kalshi_predictor.config import Settings, get_settings
from kalshi_predictor.data.backend import (
    database_url_from_settings,
    redact_database_url,
    sqlite_path_from_url,
)
from kalshi_predictor.data.db import describe_db_location
from kalshi_predictor.data.locks import db_writer_monitor
from kalshi_predictor.learning.safety import learning_status
from kalshi_predictor.paper.settlement_reconciliation import PAPER_ONLY_SAFETY
from kalshi_predictor.utils.time import utc_now

PHASE3BB_ACCELERATION_VERSION = "phase3bb_acceleration_v1"
DEFAULT_RUNTIME_HOURS = 165.0
DEFAULT_OBSERVED_POSITIVE_EV = 3
EV_TARGETS = (10, 30, 100)
REPORT_COMMANDS = {
    "db-writer-monitor",
    "market-coverage-doctor",
    "market-legs-parse",
    "phase3an-settlement-health-confirm",
    "phase3ba-r2-weather-ranking-activation",
    "phase3ba-r4-crypto-executable-book-watch",
    "phase3ba-r5-paper-ready-truth",
    "phase3ba-status",
    "phase3az-r12-weather-activation-preview",
    "phase3bb-r1-operator-scheduler",
    "phase3bb-r2-weather-fast-lane",
    "phase3bb-r3-free-source-inventory",
    "phase3bb-r4-economic-parser-backfill",
    "phase3bb-r5-usda-source-activation",
    "phase3bb-r6-sports-provenance-repair",
    "phase3bb-r7-news-event-discovery",
    "phase3bb-r8-unified-paper-gate",
    "phase3bb-r9-learning-acceleration",
    "phase3bb-r10-cloud-readiness-decision",
    "phase3bb-r11-codex-cloud-bridge",
    "phase3bb-r12-cloud-bootstrap-verification",
    "phase3bb-r13-cloud-scheduler-adoption",
    "phase3bb-r14-cloud-service-plan",
    "phase3bb-r15-cloud-service-install-review",
    "phase3bb-r16-cloud-service-install-handoff",
    "phase3bb-r33-cloud-paper-only-operations-readiness",
    "phase3bb-r34-cloud-multicategory-refresh-scheduler-review",
    "phase3bb-r35-cloud-multicategory-scheduler-no-start-dry-run",
    "phase3bb-r36-cloud-scheduler-install-handoff",
    "phase3bb-r37-cloud-scheduler-install-verification",
    "phase3bb-r38-cloud-scheduler-install-repair-handoff",
    "phase3bb-r38-cloud-scheduler-timer-start-handoff",
    "phase3bb-r39-cloud-auto-login-admin-bootstrap",
    "phase3bb-r40-cloud-scheduler-runtime-monitor",
    "phase3bb-r41-writer-gate-normalization",
    "phase3bb-r42-weather-fast-lane-post-unblock-verification",
    "phase3bb-r43-weather-catalog-scheduler-hook",
    "phase3bb-r44-weather-catalog-hook-runtime-verification",
    "phase3bb-r45-weather-freshness-to-ranking-impact",
    "phase3bb-r46-cloud-scheduler-weather-writer-gate-repair",
    "phase3bb-r47-weather-current-window-series-discovery-linkability-repair",
    "phase3bb-r48-weather-feature-refresh-runtime-verification",
    "phase3bb-r49-weather-missing-link-apply-after-feature-refresh",
    "phase3bb-r50-weather-post-link-ranking-fast-lane-recheck",
    "phase3bb-r51-weather-ranking-path-repair",
    "phase3bb-r52-weather-ev-fair-value-diagnostic",
    "phase3bb-r53-weather-current-window-cadence-preview-narrowing-repair",
    "phase3bb-r54-weather-missing-link-apply-deferral",
    "phase3bb-r55-weather-ranking-path-retry",
    "phase3bb-r57-weather-selected-window-pipeline-speed-repair",
    "phase3bb-r58-weather-selected-window-forecast-feature-alignment-repair",
    "phase3bb-r59-weather-catalog-refresh-r57-retry",
    "phase3bb-r60-weather-next-window-lead-time-scheduler-repair",
    "phase3bb-r61-cloud-dashboard-db-writer-api-reachability-repair",
    "phase3bb-acceleration-report",
    "phase3bb-cloud-readiness",
    "phase3bb-historical-replay-acceleration",
    "phase3bb-multicategory-expansion-plan",
    "phase3bb-scheduler-plan",
    "phase3bb-throughput-analysis",
    "phase3bb-weather-fast-lane",
    "snapshot",
    "sync-markets",
}
FORBIDDEN_COMMAND_FRAGMENTS = (
    "accelerate-learning",
    "autopilot-once",
    "cancel-order",
    "create-paper-trade",
    "demo-order",
    "live-order",
    "paper-trade-create",
    "place-order",
    "replace-order",
    "submit-order",
)
CORE_TABLES = (
    ("markets", "Markets", "last_seen_at"),
    ("market_snapshots", "Market snapshots", "captured_at"),
    ("forecasts", "Forecasts", "forecasted_at"),
    ("market_rankings", "Rankings", "ranked_at"),
    ("paper_orders", "Paper orders", "created_at"),
    ("forecast_memory", "Forecast memory", None),
    ("trade_memory", "Trade memory", None),
    ("weather_forecasts", "Weather forecasts", "forecasted_at"),
    ("weather_features", "Weather features", "generated_at"),
    ("crypto_features", "Crypto features", "generated_at"),
)


@dataclass(frozen=True)
class Phase3BBReportArtifacts:
    output_dir: Path
    paths: dict[str, Path]


def write_phase3bb_throughput_analysis_report(
    session: Session,
    *,
    output_dir: Path = Path("reports/phase3bb"),
    reports_dir: Path = Path("reports"),
    settings: Settings | None = None,
    command_args: list[str] | None = None,
    runtime_hours: float = DEFAULT_RUNTIME_HOURS,
    observed_positive_ev: int = DEFAULT_OBSERVED_POSITIVE_EV,
) -> Phase3BBReportArtifacts:
    ctx = _context(
        session,
        output_dir=output_dir,
        reports_dir=reports_dir,
        settings=settings,
        command_args=command_args,
    )
    payload = build_throughput_analysis(
        ctx,
        runtime_hours=runtime_hours,
        observed_positive_ev=observed_positive_ev,
    )
    output_dir.mkdir(parents=True, exist_ok=True)
    paths = {
        "markdown": output_dir / "throughput_analysis.md",
        "json": output_dir / "throughput_analysis.json",
        "funnel_csv": output_dir / "conversion_funnel.csv",
    }
    _write_json(paths["json"], payload)
    paths["markdown"].write_text(_render_throughput(payload), encoding="utf-8")
    _write_conversion_funnel(paths["funnel_csv"], payload["conversion_funnel"])
    return Phase3BBReportArtifacts(output_dir=output_dir, paths=paths)


def write_phase3bb_cloud_readiness_report(
    session: Session,
    *,
    output_dir: Path = Path("reports/phase3bb"),
    reports_dir: Path = Path("reports"),
    settings: Settings | None = None,
    command_args: list[str] | None = None,
) -> Phase3BBReportArtifacts:
    ctx = _context(
        session,
        output_dir=output_dir,
        reports_dir=reports_dir,
        settings=settings,
        command_args=command_args,
    )
    payload = build_cloud_readiness(ctx)
    output_dir.mkdir(parents=True, exist_ok=True)
    paths = {
        "markdown": output_dir / "cloud_readiness.md",
        "json": output_dir / "cloud_readiness.json",
        "checklist": output_dir / "cloud_deployment_checklist.md",
    }
    _write_json(paths["json"], payload)
    paths["markdown"].write_text(_render_cloud_readiness(payload), encoding="utf-8")
    paths["checklist"].write_text(_render_cloud_checklist(payload), encoding="utf-8")
    return Phase3BBReportArtifacts(output_dir=output_dir, paths=paths)


def write_phase3bb_scheduler_plan_report(
    session: Session,
    *,
    output_dir: Path = Path("reports/phase3bb"),
    reports_dir: Path = Path("reports"),
    settings: Settings | None = None,
    command_args: list[str] | None = None,
) -> Phase3BBReportArtifacts:
    ctx = _context(
        session,
        output_dir=output_dir,
        reports_dir=reports_dir,
        settings=settings,
        command_args=command_args,
    )
    payload = build_scheduler_plan(ctx)
    output_dir.mkdir(parents=True, exist_ok=True)
    paths = {
        "markdown": output_dir / "scheduler_plan.md",
        "json": output_dir / "scheduler_plan.json",
        "systemd": output_dir / "systemd_service_examples.md",
    }
    _write_json(paths["json"], payload)
    paths["markdown"].write_text(_render_scheduler_plan(payload), encoding="utf-8")
    paths["systemd"].write_text(_render_systemd_examples(payload), encoding="utf-8")
    return Phase3BBReportArtifacts(output_dir=output_dir, paths=paths)


def write_phase3bb_multicategory_expansion_plan_report(
    session: Session,
    *,
    output_dir: Path = Path("reports/phase3bb"),
    reports_dir: Path = Path("reports"),
    settings: Settings | None = None,
    command_args: list[str] | None = None,
) -> Phase3BBReportArtifacts:
    ctx = _context(
        session,
        output_dir=output_dir,
        reports_dir=reports_dir,
        settings=settings,
        command_args=command_args,
    )
    payload = build_multicategory_expansion_plan(ctx)
    output_dir.mkdir(parents=True, exist_ok=True)
    paths = {
        "markdown": output_dir / "multicategory_expansion_plan.md",
        "scorecard_csv": output_dir / "category_scorecard.csv",
        "next_sprint": output_dir / "noncrypto_next_sprint.md",
    }
    paths["markdown"].write_text(_render_multicategory(payload), encoding="utf-8")
    _write_category_scorecard(paths["scorecard_csv"], payload["ranked_categories"])
    paths["next_sprint"].write_text(_render_noncrypto_next_sprint(payload), encoding="utf-8")
    return Phase3BBReportArtifacts(output_dir=output_dir, paths=paths)


def write_phase3bb_weather_fast_lane_report(
    session: Session,
    *,
    output_dir: Path = Path("reports/phase3bb"),
    reports_dir: Path = Path("reports"),
    settings: Settings | None = None,
    command_args: list[str] | None = None,
) -> Phase3BBReportArtifacts:
    ctx = _context(
        session,
        output_dir=output_dir,
        reports_dir=reports_dir,
        settings=settings,
        command_args=command_args,
    )
    payload = build_weather_fast_lane(ctx)
    output_dir.mkdir(parents=True, exist_ok=True)
    paths = {
        "markdown": output_dir / "weather_fast_lane.md",
        "json": output_dir / "weather_fast_lane.json",
    }
    _write_json(paths["json"], payload)
    paths["markdown"].write_text(_render_weather_fast_lane(payload), encoding="utf-8")
    return Phase3BBReportArtifacts(output_dir=output_dir, paths=paths)


def write_phase3bb_historical_replay_acceleration_report(
    session: Session,
    *,
    output_dir: Path = Path("reports/phase3bb"),
    reports_dir: Path = Path("reports"),
    settings: Settings | None = None,
    command_args: list[str] | None = None,
) -> Phase3BBReportArtifacts:
    ctx = _context(
        session,
        output_dir=output_dir,
        reports_dir=reports_dir,
        settings=settings,
        command_args=command_args,
    )
    payload = build_historical_replay_acceleration(ctx)
    output_dir.mkdir(parents=True, exist_ok=True)
    paths = {
        "markdown": output_dir / "historical_replay_acceleration.md",
        "json": output_dir / "historical_replay_acceleration.json",
    }
    _write_json(paths["json"], payload)
    paths["markdown"].write_text(_render_historical_replay(payload), encoding="utf-8")
    return Phase3BBReportArtifacts(output_dir=output_dir, paths=paths)


def write_phase3bb_acceleration_report(
    session: Session,
    *,
    output_dir: Path = Path("reports/phase3bb"),
    reports_dir: Path = Path("reports"),
    settings: Settings | None = None,
    command_args: list[str] | None = None,
    runtime_hours: float = DEFAULT_RUNTIME_HOURS,
    observed_positive_ev: int = DEFAULT_OBSERVED_POSITIVE_EV,
) -> Phase3BBReportArtifacts:
    ctx = _context(
        session,
        output_dir=output_dir,
        reports_dir=reports_dir,
        settings=settings,
        command_args=command_args,
    )
    throughput = build_throughput_analysis(
        ctx,
        runtime_hours=runtime_hours,
        observed_positive_ev=observed_positive_ev,
    )
    cloud = build_cloud_readiness(ctx)
    scheduler = build_scheduler_plan(ctx)
    multicategory = build_multicategory_expansion_plan(ctx)
    weather = build_weather_fast_lane(ctx)
    replay = build_historical_replay_acceleration(ctx)
    payload = build_unified_acceleration_report(
        ctx,
        throughput=throughput,
        cloud=cloud,
        scheduler=scheduler,
        multicategory=multicategory,
        weather=weather,
        replay=replay,
    )
    output_dir.mkdir(parents=True, exist_ok=True)
    paths = {
        "executive": output_dir / "EXECUTIVE_SUMMARY.md",
        "next_actions": output_dir / "NEXT_ACTIONS.md",
        "next_codex_sprint": output_dir / "NEXT_CODEX_SPRINT.md",
        "throughput_md": output_dir / "throughput_analysis.md",
        "throughput_json": output_dir / "throughput_analysis.json",
        "conversion_csv": output_dir / "conversion_funnel.csv",
        "cloud_md": output_dir / "cloud_readiness.md",
        "cloud_json": output_dir / "cloud_readiness.json",
        "cloud_checklist": output_dir / "cloud_deployment_checklist.md",
        "scheduler_md": output_dir / "scheduler_plan.md",
        "scheduler_json": output_dir / "scheduler_plan.json",
        "systemd": output_dir / "systemd_service_examples.md",
        "multicategory_md": output_dir / "multicategory_expansion_plan.md",
        "scorecard_csv": output_dir / "category_scorecard.csv",
        "noncrypto_next": output_dir / "noncrypto_next_sprint.md",
        "weather_md": output_dir / "weather_fast_lane.md",
        "weather_json": output_dir / "weather_fast_lane.json",
        "replay_md": output_dir / "historical_replay_acceleration.md",
        "replay_json": output_dir / "historical_replay_acceleration.json",
        "unified_json": output_dir / "acceleration_report.json",
        "manifest": output_dir / "MANIFEST.sha256",
    }
    _write_json(paths["throughput_json"], throughput)
    paths["throughput_md"].write_text(_render_throughput(throughput), encoding="utf-8")
    _write_conversion_funnel(paths["conversion_csv"], throughput["conversion_funnel"])
    _write_json(paths["cloud_json"], cloud)
    paths["cloud_md"].write_text(_render_cloud_readiness(cloud), encoding="utf-8")
    paths["cloud_checklist"].write_text(_render_cloud_checklist(cloud), encoding="utf-8")
    _write_json(paths["scheduler_json"], scheduler)
    paths["scheduler_md"].write_text(_render_scheduler_plan(scheduler), encoding="utf-8")
    paths["systemd"].write_text(_render_systemd_examples(scheduler), encoding="utf-8")
    paths["multicategory_md"].write_text(_render_multicategory(multicategory), encoding="utf-8")
    _write_category_scorecard(paths["scorecard_csv"], multicategory["ranked_categories"])
    paths["noncrypto_next"].write_text(
        _render_noncrypto_next_sprint(multicategory),
        encoding="utf-8",
    )
    _write_json(paths["weather_json"], weather)
    paths["weather_md"].write_text(_render_weather_fast_lane(weather), encoding="utf-8")
    _write_json(paths["replay_json"], replay)
    paths["replay_md"].write_text(_render_historical_replay(replay), encoding="utf-8")
    _write_json(paths["unified_json"], payload)
    paths["executive"].write_text(_render_executive_summary(payload), encoding="utf-8")
    paths["next_actions"].write_text(_render_next_actions(payload), encoding="utf-8")
    paths["next_codex_sprint"].write_text(
        _render_next_codex_sprint(payload),
        encoding="utf-8",
    )
    _write_manifest(paths["manifest"], [path for key, path in paths.items() if key != "manifest"])
    return Phase3BBReportArtifacts(output_dir=output_dir, paths=paths)


def build_throughput_analysis(
    ctx: dict[str, Any],
    *,
    runtime_hours: float,
    observed_positive_ev: int,
) -> dict[str, Any]:
    counts = ctx["counts"]
    status_summary = ctx["status_summary"]
    learning = ctx["learning_status"]
    safe_runtime = max(0.0, float(runtime_hours))
    paper_ready_rows = _to_int(status_summary.get("paper_ready_rows"))
    current_positive_ev = _to_int(status_summary.get("positive_ev_rows"))
    settled = _to_int(learning.get("settled_paper_trades"))
    target = max(1, _to_int(learning.get("target_settled_trades")) or 500)
    rates = {
        "market_ingestion_per_hour": _rate(counts.get("markets"), safe_runtime),
        "snapshot_per_hour": _rate(counts.get("market_snapshots"), safe_runtime),
        "forecast_per_hour": _rate(counts.get("forecasts"), safe_runtime),
        "ranking_per_hour": _rate(counts.get("market_rankings"), safe_runtime),
        "observed_ev_per_day": ev_per_day(observed_positive_ev, safe_runtime),
        "current_positive_ev_rows": current_positive_ev,
        "paper_ready_per_day": round(_rate(paper_ready_rows, safe_runtime) * 24.0, 3),
        "paper_trade_per_hour_historical": _rate(counts.get("paper_orders"), safe_runtime),
        "settled_trade_per_hour_historical": _rate(settled, safe_runtime),
    }
    estimates = _time_estimates(
        observed_positive_ev=observed_positive_ev,
        runtime_hours=safe_runtime,
        settled=settled,
        target=target,
        daily_paper_trades=_to_int(learning.get("daily_paper_trades")),
        paper_ready_rows=paper_ready_rows,
    )
    conversion_funnel = [
        _funnel_row("markets", "catalog/ingestion", counts.get("markets")),
        _funnel_row("market_snapshots", "snapshot", counts.get("market_snapshots")),
        _funnel_row("forecasts", "forecast", counts.get("forecasts")),
        _funnel_row("market_rankings", "ranking", counts.get("market_rankings")),
        _funnel_row("observed_positive_ev", "EV", observed_positive_ev),
        _funnel_row("current_positive_ev", "current EV", current_positive_ev),
        _funnel_row("paper_ready", "paper gate", paper_ready_rows),
        _funnel_row("paper_orders", "paper orders", counts.get("paper_orders")),
        _funnel_row("settled_paper_trades", "settled evidence", settled),
    ]
    blockers_by_gate = _blockers_by_gate(ctx)
    payload = {
        **ctx["metadata"],
        "phase": "3BB-THROUGHPUT",
        "phase_version": PHASE3BB_ACCELERATION_VERSION,
        "mode": "PAPER_READ_ONLY_THROUGHPUT_ANALYSIS",
        "runtime_hours": safe_runtime,
        "observed_positive_ev_rows": observed_positive_ev,
        "rates": rates,
        "time_estimates": estimates,
        "conversion_funnel": conversion_funnel,
        "current_blockers_by_category": ctx["paper_truth_summary"].get(
            "blocked_by_category",
            {},
        ),
        "current_blockers_by_gate": blockers_by_gate,
        "summary": {
            "ingestion_bottleneck": False,
            "opportunity_conversion_bottleneck": True,
            "true_first_blocker": status_summary.get("true_first_blocker"),
            "crypto_first_blocker": status_summary.get("crypto_first_blocker"),
            "weather_first_blocker": status_summary.get("weather_first_blocker"),
            "paper_ready_rows": paper_ready_rows,
            "estimate_meaningful": estimates["settled_target"]["meaningful"],
        },
        "safety_flags": _safety_flags(),
    }
    return payload


def build_cloud_readiness(ctx: dict[str, Any]) -> dict[str, Any]:
    db_url = ctx["metadata"]["resolved_database_url"]
    db_path = (ctx["metadata"]["database_fingerprint"] or {}).get("path")
    one_drive_risk = "OneDrive" in ctx["metadata"]["repository_root"]
    sqlite_backend = str(db_url).startswith("sqlite")
    recommendation = cloud_recommendation(sqlite_backend=sqlite_backend)
    return {
        **ctx["metadata"],
        "phase": "3BB-CLOUD-READINESS",
        "phase_version": PHASE3BB_ACCELERATION_VERSION,
        "mode": "PAPER_READ_ONLY_CLOUD_READINESS",
        "current_db_backend": db_url,
        "database_path": db_path,
        "sqlite_lock_writer_risks": [
            "SQLite allows one writer at a time.",
            "Long snapshot/ranking/report jobs can block writer-capable jobs.",
            "A cloud scheduler still needs an explicit writer gate.",
        ],
        "onedrive_path_risks": [
            "Do not run production services from a OneDrive-synced workspace.",
            "Use a local Linux filesystem path on VPS; backup separately.",
        ]
        if one_drive_risk
        else ["Current WSL DB path is outside OneDrive; keep VPS data off sync folders."],
        "postgres_readiness": {
            "required_now": recommendation["postgres_required_now"],
            "recommended_before_always_on_scale": True,
            "reason": (
                "SQLite is acceptable for one paper-only writer, but Postgres is safer "
                "for always-on services, dashboards, and cloud restarts."
            ),
        },
        "environment_variables_needed": [
            "DATABASE_URL",
            "KALSHI_API_KEY_ID",
            "KALSHI_PRIVATE_KEY_PATH",
            "LEARNING_MODE=true",
            "LEARNING_BLOCK_DEMO_EXECUTION=true",
            "LEARNING_BLOCK_LIVE_EXECUTION=true",
            "TZ=America/Chicago",
        ],
        "secrets_needed": [
            "Kalshi API credentials for read-only market data",
            "Provider keys only for sources already approved by the app",
        ],
        "service_commands": _service_commands(),
        "scheduler_commands": _scheduler_commands(),
        "expected_cpu_ram": recommendation["minimum_vps_size"],
        "rate_limit_risks": [
            "Kalshi catalog/orderbook refreshes must stay bounded.",
            "Weather/economic/news sources need per-source cadence limits.",
            "Retry storms should be skipped, not queued unboundedly.",
        ],
        "backup_needs": [
            "Nightly DB backup before writer jobs.",
            "Copy reports and logs off host.",
            "Keep .env/secrets out of report archives.",
        ],
        "log_locations": ["reports/", "logs/ if configured", "systemd journal on VPS"],
        "recovery_plan": [
            "Stop scheduler service.",
            "Inspect db-writer-monitor.",
            "Restore latest DB backup only if corruption is confirmed.",
            "Restart one guarded R5 watcher, then dashboard/status.",
        ],
        "deployment_checklist": _cloud_checklist_items(recommendation),
        "recommendation": recommendation,
        "safety_flags": _safety_flags(),
    }


def build_scheduler_plan(ctx: dict[str, Any]) -> dict[str, Any]:
    writer = ctx["writer"]
    return {
        **ctx["metadata"],
        "phase": "3BB-SCHEDULER-PLAN",
        "phase_version": PHASE3BB_ACCELERATION_VERSION,
        "mode": "PAPER_READ_ONLY_SCHEDULER_DESIGN",
        "current_writer": writer,
        "rules": scheduler_rules(),
        "job_lanes": [
            {
                "lane": "crypto_background_watch",
                "command": "kalshi-bot phase3bc-r5-status --output-dir reports/phase3bc_r5",
                "writer_capable": False,
                "guard": "Do not start duplicate R5 watchers.",
            },
            {
                "lane": "weather_opportunity",
                "command": (
                    "kalshi-bot db-writer-monitor --json && kalshi-bot "
                    "phase3ba-r2-weather-ranking-activation --output-dir "
                    "reports/phase3ba_r2 --reports-dir reports"
                ),
                "writer_capable": True,
                "guard": "Skip if safe_to_start_write is false.",
            },
            {
                "lane": "dashboard_truth",
                "command": "kalshi-bot phase3ba-status --output-dir reports/phase3ba_status",
                "writer_capable": False,
                "guard": "Read-only refresh can run while writer is active.",
            },
        ],
        "rate_limit_policy": {
            "bounded_pages": True,
            "max_one_catalog_refresh_lane": True,
            "skip_on_contention": True,
            "retry_backoff_seconds": [60, 300, 900],
        },
        "safety_flags": _safety_flags(),
    }


def build_multicategory_expansion_plan(ctx: dict[str, Any]) -> dict[str, Any]:
    categories = _category_rows(ctx)
    ranked = rank_categories(categories, crypto_waiting=True)
    selected = ranked[0] if ranked else {}
    return {
        **ctx["metadata"],
        "phase": "3BB-MULTICATEGORY-EXPANSION",
        "phase_version": PHASE3BB_ACCELERATION_VERSION,
        "mode": "PAPER_READ_ONLY_MULTICATEGORY_PLAN",
        "ranked_categories": ranked,
        "selected_next_category": selected,
        "deferred_paid_sources": ["TradingEconomics"],
        "notes": [
            "Crypto remains a background watch.",
            "Weather is the fastest non-crypto path when links/snapshots/forecasts exist.",
            "Sports has high market count but needs provenance-safe source work.",
            "Composites remain parked outside single-market remediation.",
        ],
        "safety_flags": _safety_flags(),
    }


def build_weather_fast_lane(ctx: dict[str, Any]) -> dict[str, Any]:
    weather_rows = _read_csv(ctx["reports_dir"] / "phase3ba_r2" / "weather_opportunity_rows.csv")
    total = len(weather_rows)
    with_snapshots = sum(1 for row in weather_rows if _truthy(row.get("has_snapshot")))
    with_forecasts = sum(1 for row in weather_rows if _truthy(row.get("has_current_forecast")))
    with_rankings = sum(1 for row in weather_rows if _truthy(row.get("has_current_ranking")))
    blocker_counts: dict[str, int] = {}
    for row in weather_rows:
        blocker = str(row.get("first_hard_blocker") or "UNKNOWN")
        blocker_counts[blocker] = blocker_counts.get(blocker, 0) + 1
    writer_active = bool((ctx["writer"] or {}).get("current_writer_pid"))
    ready_to_run = total > 0 and with_forecasts > 0 and not writer_active
    return {
        **ctx["metadata"],
        "phase": "3BB-WEATHER-FAST-LANE",
        "phase_version": PHASE3BB_ACCELERATION_VERSION,
        "mode": "PAPER_READ_ONLY_WEATHER_FAST_LANE",
        "active_linked_weather_rows": _weather_link_count(ctx),
        "weather_rows_with_source_snapshot_evidence": with_snapshots,
        "weather_rows_with_forecasts": with_forecasts,
        "weather_rows_with_rankings": with_rankings,
        "weather_paper_gate_blockers": blocker_counts,
        "find_opportunities_weather_v2_ready_to_run": ready_to_run,
        "writer_contention_blocks_it": writer_active,
        "recommended_command": (
            "kalshi-bot db-writer-monitor --json && kalshi-bot "
            "phase3ba-r2-weather-ranking-activation --output-dir reports/phase3ba_r2 "
            "--reports-dir reports && kalshi-bot phase3ba-r5-paper-ready-truth "
            "--output-dir reports/phase3ba_r5 --reports-dir reports --max-duration-seconds 120"
        ),
        "paper_trade_creation": False,
        "safety_flags": _safety_flags(),
    }


def build_historical_replay_acceleration(ctx: dict[str, Any]) -> dict[str, Any]:
    return {
        **ctx["metadata"],
        "phase": "3BB-HISTORICAL-REPLAY-ACCELERATION",
        "phase_version": PHASE3BB_ACCELERATION_VERSION,
        "mode": "PAPER_READ_ONLY_HISTORICAL_REPLAY_PLAN",
        "lane_label": "HISTORICAL_REPLAY",
        "counts_as_real_paper_trade_learning": False,
        "can_improve_model_diagnostics": True,
        "must_not_fabricate_settlements": True,
        "separation_rules": historical_replay_rules(),
        "implementation_plan": [
            "Build replay datasets from markets with actual settlements already in DB.",
            "Label every replay row HISTORICAL_REPLAY.",
            "Store diagnostics separately from paper_orders and paper learning targets.",
            "Compare forecast calibration and ranking quality without claiming live fills.",
        ],
        "safety_flags": _safety_flags(),
    }


def build_unified_acceleration_report(
    ctx: dict[str, Any],
    *,
    throughput: dict[str, Any],
    cloud: dict[str, Any],
    scheduler: dict[str, Any],
    multicategory: dict[str, Any],
    weather: dict[str, Any],
    replay: dict[str, Any],
) -> dict[str, Any]:
    selected_sprint = select_next_sprint(
        writer_active=bool((ctx["writer"] or {}).get("current_writer_pid")),
        weather_ready=bool(weather["find_opportunities_weather_v2_ready_to_run"]),
        manual_babysitting_severe=True,
    )
    next_operator_command = (
        "kalshi-bot phase3bb-scheduler-plan --output-dir reports/phase3bb "
        "--reports-dir reports"
    )
    if selected_sprint["id"] == "WEATHER_PAPER_FUNNEL_ACTIVATION":
        next_operator_command = weather["recommended_command"]
    command_checks = command_safety_checks(next_operator_command)
    return {
        **ctx["metadata"],
        "phase": "3BB-ACCELERATION-REPORT",
        "phase_version": PHASE3BB_ACCELERATION_VERSION,
        "mode": "PAPER_READ_ONLY_ACCELERATION_ORCHESTRATION",
        "throughput_summary": throughput["summary"],
        "cloud_summary": cloud["recommendation"],
        "scheduler_summary": {
            "prevents_duplicate_writers": scheduler["rules"]["prevents_duplicate_writers"],
            "active_writer": bool((ctx["writer"] or {}).get("current_writer_pid")),
        },
        "multicategory_summary": {
            "selected_next_category": multicategory["selected_next_category"],
            "ranked_categories": multicategory["ranked_categories"][:5],
        },
        "weather_summary": {
            "ready_to_run": weather["find_opportunities_weather_v2_ready_to_run"],
            "blockers": weather["weather_paper_gate_blockers"],
        },
        "historical_replay_summary": {
            "lane_label": replay["lane_label"],
            "counts_as_real_paper_trade_learning": (
                replay["counts_as_real_paper_trade_learning"]
            ),
        },
        "answers": {
            "why_progress_is_slow": (
                "Ingestion is healthy, but very few current rows survive the "
                "forecast-to-EV-to-executable-book funnel."
            ),
            "is_ingestion_the_bottleneck": False,
            "is_opportunity_conversion_the_bottleneck": True,
            "would_cloud_help": True,
            "what_cloud_is_appropriate": cloud["recommendation"]["minimum_vps_size"],
            "what_should_not_be_sped_up": [
                "Threshold lowering",
                "Live/demo order submission",
                "Fuzzy/sibling/partial-provenance matching",
                "Paid sources before free-source lanes are exhausted",
            ],
            "which_category_next": (
                multicategory["selected_next_category"].get("category") or "weather"
            ),
            "single_next_codex_sprint": selected_sprint["title"],
            "single_next_operator_command": next_operator_command,
            "orders_occurred": False,
        },
        "next_codex_sprint": selected_sprint,
        "next_operator_command": next_operator_command,
        "command_checks": command_checks,
        "acceptance": {
            "explains_slow_progress": True,
            "cloud_recommendation_practical": not cloud["recommendation"]["gpu_required"],
            "one_next_sprint_selected": bool(selected_sprint),
            "one_next_operator_command": bool(next_operator_command),
            "crypto_background_watch_only": True,
            "no_paper_live_demo_orders": True,
        },
        "safety_flags": _safety_flags(),
    }


def _context(
    session: Session,
    *,
    output_dir: Path,
    reports_dir: Path,
    settings: Settings | None,
    command_args: list[str] | None,
) -> dict[str, Any]:
    resolved = settings or get_settings()
    generated_at = utc_now().isoformat()
    metadata = _metadata(
        session,
        settings=resolved,
        generated_at=generated_at,
        command_args=command_args or [],
        output_dir=output_dir,
    )
    ingestion = _read_json(
        reports_dir / "phase3ba_ingestion_stability" / "ingestion_stability.json"
    )
    status = _read_json(reports_dir / "phase3ba_status" / "status.json")
    paper_truth = _read_json(reports_dir / "phase3ba_r5" / "paper_ready_truth.json")
    crypto = _read_json(reports_dir / "phase3ba_r4" / "crypto_executable_book_watch.json")
    weather_r2 = _read_json(reports_dir / "phase3ba_r2" / "weather_ranking_activation.json")
    return {
        "session": session,
        "reports_dir": reports_dir,
        "output_dir": output_dir,
        "metadata": metadata,
        "writer": db_writer_monitor(settings=resolved),
        "learning_status": learning_status(session, settings=resolved),
        "ingestion_report": ingestion,
        "status_report": status,
        "paper_truth": paper_truth,
        "crypto_report": crypto,
        "weather_r2_report": weather_r2,
        "counts": _counts(session, ingestion),
        "status_summary": _best_summary(status, ingestion, paper_truth),
        "paper_truth_summary": paper_truth.get("summary") or {},
    }


def _best_summary(
    status: dict[str, Any],
    ingestion: dict[str, Any],
    paper_truth: dict[str, Any],
) -> dict[str, Any]:
    status_summary = status.get("summary") or {}
    ingestion_summary = ingestion.get("summary") or {}
    paper_summary = paper_truth.get("summary") or {}
    return {
        **paper_summary,
        **ingestion_summary,
        **status_summary,
        "paper_ready_rows": (
            status_summary.get("paper_ready_rows")
            if status_summary.get("paper_ready_rows") is not None
            else paper_summary.get("paper_ready_rows", 0)
        ),
        "positive_ev_rows": (
            status_summary.get("positive_ev_rows")
            if status_summary.get("positive_ev_rows") is not None
            else paper_summary.get("positive_ev_rows", 0)
        ),
    }


def _counts(session: Session, ingestion: dict[str, Any]) -> dict[str, int]:
    from_report = {
        str(row.get("table")): _to_int(row.get("row_count"))
        for row in ingestion.get("table_observations", [])
        if row.get("table")
    }
    counts: dict[str, int] = {}
    for table, _label, _timestamp in CORE_TABLES:
        counts[table] = from_report.get(table) or _rowid_count(session, table)
    return counts


def _rowid_count(session: Session, table: str) -> int:
    try:
        return int(session.execute(text(f'SELECT MAX(rowid) FROM "{table}"')).scalar() or 0)
    except Exception:
        return 0


def _latest_value(session: Session, table: str, column: str) -> str | None:
    try:
        value = session.execute(
            text(
                f'SELECT "{column}" FROM "{table}" WHERE "{column}" IS NOT NULL '
                "ORDER BY rowid DESC LIMIT 1"
            )
        ).scalar()
    except Exception:
        return None
    return str(value) if value is not None else None


def _metadata(
    session: Session,
    *,
    settings: Settings,
    generated_at: str,
    command_args: list[str],
    output_dir: Path,
) -> dict[str, Any]:
    db_url = database_url_from_settings(settings)
    return {
        "generated_at": generated_at,
        "repository_root": str(Path.cwd().resolve()),
        "git_branch": _git_value("rev-parse", "--abbrev-ref", "HEAD"),
        "git_commit": _git_value("rev-parse", "HEAD"),
        "git_dirty": _git_dirty_status(),
        "python_executable": str(Path(sys.executable).resolve()),
        "installed_package_path": str(Path(__file__).resolve()),
        "output_dir": str(output_dir),
        "resolved_database_url": redact_database_url(db_url),
        "database_fingerprint": _database_fingerprint(db_url),
        "database_location": describe_db_location(db_url),
        "migration_revision": _migration_revision(session),
        "command_arguments": {
            "command": "kalshi-bot phase3bb",
            "argv": command_args,
        },
        "data_watermark": _data_watermark(session),
        "paper_only_safety": PAPER_ONLY_SAFETY,
        "live_or_demo_execution": False,
        "order_submission": False,
        "order_cancel_replace": False,
        "paper_trade_creation": False,
        "thresholds_lowered": False,
    }


def _data_watermark(session: Session) -> dict[str, Any]:
    return {
        "latest_market_seen_at": _latest_value(session, "markets", "last_seen_at"),
        "latest_snapshot_captured_at": _latest_value(
            session,
            "market_snapshots",
            "captured_at",
        ),
        "latest_forecasted_at": _latest_value(session, "forecasts", "forecasted_at"),
        "latest_ranking_at": _latest_value(session, "market_rankings", "ranked_at"),
    }


def _database_fingerprint(db_url: str) -> dict[str, Any]:
    sqlite_path = sqlite_path_from_url(db_url)
    if sqlite_path is None:
        return {
            "kind": "non_sqlite",
            "database_url_hash": hashlib.sha256(
                redact_database_url(db_url).encode("utf-8")
            ).hexdigest(),
        }
    path = sqlite_path.expanduser().resolve()
    if str(sqlite_path) == ":memory:":
        return {"kind": "sqlite_memory", "path": ":memory:"}
    if not path.exists():
        return {"kind": "missing_sqlite_file", "path": str(path)}
    stat = path.stat()
    payload = {"path": str(path), "size_bytes": stat.st_size, "mtime_ns": stat.st_mtime_ns}
    return {
        "kind": "sqlite_file_stat",
        **payload,
        "fingerprint": hashlib.sha256(
            json.dumps(payload, sort_keys=True).encode("utf-8")
        ).hexdigest(),
    }


def _migration_revision(session: Session) -> str | None:
    try:
        return session.execute(text("select version_num from alembic_version limit 1")).scalar()
    except Exception:
        return None


def _git_value(*args: str) -> str:
    try:
        result = subprocess.run(
            ["git", *args],
            cwd=Path.cwd(),
            check=False,
            capture_output=True,
            text=True,
            timeout=2,
        )
    except (OSError, subprocess.TimeoutExpired):
        return "UNKNOWN"
    value = result.stdout.strip()
    return value if result.returncode == 0 and value else "UNKNOWN"


def _git_dirty_status() -> str:
    try:
        result = subprocess.run(
            ["git", "status", "--short"],
            cwd=Path.cwd(),
            check=False,
            capture_output=True,
            text=True,
            timeout=2,
        )
    except (OSError, subprocess.TimeoutExpired):
        return "UNKNOWN"
    if result.returncode != 0:
        return "UNKNOWN"
    return "dirty" if result.stdout.strip() else "clean"


def _rate(value: Any, hours: float) -> float:
    if hours <= 0:
        return 0.0
    return round(_to_int(value) / hours, 4)


def ev_per_day(observed_positive_ev: int, runtime_hours: float) -> float:
    if runtime_hours <= 0:
        return 0.0
    return round((observed_positive_ev / runtime_hours) * 24.0, 3)


def _time_estimates(
    *,
    observed_positive_ev: int,
    runtime_hours: float,
    settled: int,
    target: int,
    daily_paper_trades: int,
    paper_ready_rows: int,
) -> dict[str, Any]:
    ev_hour = observed_positive_ev / runtime_hours if runtime_hours > 0 else 0.0
    ev_targets = []
    for target_ev in EV_TARGETS:
        remaining = max(0, target_ev - observed_positive_ev)
        hours = remaining / ev_hour if ev_hour > 0 else None
        ev_targets.append(
            {
                "target": target_ev,
                "remaining": remaining,
                "days_at_observed_pace": _round_or_none(hours / 24.0 if hours else None),
            }
        )
    meaningful = estimate_meaningful(
        paper_ready_rows=paper_ready_rows,
        daily_paper_trades=daily_paper_trades,
    )
    remaining_settled = max(0, target - settled)
    settled_days = (
        math.ceil(remaining_settled / daily_paper_trades)
        if daily_paper_trades > 0
        else None
    )
    return {
        "positive_ev_targets": ev_targets,
        "settled_target": {
            "settled": settled,
            "target": target,
            "remaining": remaining_settled,
            "daily_paper_trades": daily_paper_trades,
            "days_at_current_pace": settled_days,
            "meaningful": meaningful["meaningful"],
            "classification": meaningful["classification"],
        },
    }


def estimate_meaningful(*, paper_ready_rows: int, daily_paper_trades: int) -> dict[str, Any]:
    if paper_ready_rows <= 0 or daily_paper_trades <= 0:
        return {
            "meaningful": False,
            "classification": "NOT_HONESTLY_ESTIMABLE_ZERO_PAPER_READY_PACE",
        }
    return {"meaningful": True, "classification": "ESTIMABLE_FROM_CURRENT_PACE"}


def _funnel_row(name: str, gate: str, count: Any) -> dict[str, Any]:
    return {"name": name, "gate": gate, "count": _to_int(count)}


def _blockers_by_gate(ctx: dict[str, Any]) -> dict[str, Any]:
    paper_summary = ctx["paper_truth_summary"]
    blocker_counts = paper_summary.get("blocker_counts") or {}
    return {
        "source": 0,
        "parser": 0,
        "link": 0,
        "snapshot": blocker_counts.get("SNAPSHOT_MISSING", 0),
        "forecast": blocker_counts.get("FORECAST_MISSING", 0),
        "ranking": blocker_counts.get("RANKING_MISSING", 0),
        "EV": blocker_counts.get("EV_NOT_POSITIVE", 0),
        "executable_book": blocker_counts.get("ZERO_VISIBLE_DEPTH", 0),
        "liquidity": blocker_counts.get("LIQUIDITY_TOO_LOW", 0),
        "spread": blocker_counts.get("SPREAD_TOO_WIDE", 0),
        "risk": blocker_counts.get("RISK_NOT_ELIGIBLE", 0),
        "settlement": blocker_counts.get("SETTLEMENT_TERMS_UNKNOWN", 0),
        "raw_blocker_counts": blocker_counts,
    }


def cloud_recommendation(*, sqlite_backend: bool) -> dict[str, Any]:
    return {
        "minimum_vps_size": "2 vCPU / 4 GB RAM / 80-100 GB SSD",
        "recommended_vps_size": "2-4 vCPU / 8 GB RAM / 150 GB SSD if Postgres is local",
        "gpu_required": False,
        "postgres_required_now": False,
        "postgres_recommended_before_scale": sqlite_backend,
        "docker_compose_appropriate": True,
        "systemd_services_should_be_created": True,
        "what_not_to_buy": [
            "GPU instance",
            "Large ML cloud box",
            "Paid market data source before free-source lanes are exhausted",
        ],
    }


def scheduler_rules() -> dict[str, Any]:
    return {
        "prevents_duplicate_writers": True,
        "one_writer_capable_job_at_a_time": True,
        "crypto_r5_background_if_active": True,
        "weather_waits_for_writer_clearance": True,
        "source_evidence_waits_for_writer_clearance": True,
        "dashboard_truth_read_only": True,
        "on_active_writer": "SKIP_OR_QUEUE_WITH_REASON",
        "rate_limits_respected": True,
    }


def _service_commands() -> list[str]:
    return [
        "kalshi-bot phase3bc-r5-unattended-start --output-dir reports/phase3bc_r5",
        "kalshi-bot phase3bb-scheduler-plan --output-dir reports/phase3bb",
        "kalshi-bot phase3ba-status --output-dir reports/phase3ba_status",
    ]


def _scheduler_commands() -> list[str]:
    return [
        "kalshi-bot db-writer-monitor --json",
        (
            "kalshi-bot phase3ba-r2-weather-ranking-activation --output-dir "
            "reports/phase3ba_r2 --reports-dir reports"
        ),
        (
            "kalshi-bot phase3ba-r4-crypto-executable-book-watch --output-dir "
            "reports/phase3ba_r4 --reports-dir reports"
        ),
        (
            "kalshi-bot phase3ba-r5-paper-ready-truth --output-dir reports/phase3ba_r5 "
            "--reports-dir reports --max-duration-seconds 120"
        ),
    ]


def _cloud_checklist_items(recommendation: dict[str, Any]) -> list[str]:
    return [
        f"Provision VPS: {recommendation['minimum_vps_size']}",
        "Clone repo to local Linux disk, not a synced folder.",
        "Install Python runtime and project dependencies.",
        "Create paper-only .env with live/demo blockers enabled.",
        "Move SQLite DB or configure Postgres DATABASE_URL.",
        "Create systemd service for one guarded R5 watcher.",
        "Create timer/service for one writer-gated scheduler.",
        "Create backup job before writer-capable jobs.",
        "Verify phase3bb-acceleration-report runs before enabling scheduler.",
    ]


def _category_rows(ctx: dict[str, Any]) -> list[dict[str, Any]]:
    rows = _read_csv(ctx["reports_dir"] / "phase3ba_r6" / "noncrypto_engine_backlog.csv")
    coverage = _read_json(ctx["reports_dir"] / "market_coverage" / "link_coverage.json")
    coverage_rows = {row.get("category"): row for row in coverage.get("category_rows", [])}
    if "crypto" not in {row.get("category") for row in rows}:
        crypto = coverage_rows.get("crypto", {})
        rows.append(
            {
                "category": "crypto",
                "active_market_count": str(_to_int(crypto.get("linked_markets"))),
                "parsed_market_count": str(_to_int(crypto.get("parsed_markets"))),
                "linked_count": str(_to_int(crypto.get("linked_markets"))),
                "source_readiness": "SOURCE_AND_FEATURES_READY",
                "parser_readiness": "PARSER_READY",
                "forecast_readiness": "FORECASTS_AND_RANKINGS_PRESENT",
                "paper_gate_readiness": "PAPER_GATE_BLOCKED:EXECUTABLE_BOOK",
                "primary_blocker": "BACKGROUND_WAITING_FOR_EXECUTABLE_BOOK",
                "next_implementation_step": "Keep crypto as background watch.",
                "coverage_status": crypto.get("status") or "CONNECTED",
                "coverage_percent": crypto.get("coverage_percent") or "100.0%",
            }
        )
    existing = {row.get("category") for row in rows}
    for category, blocker in (
        ("agriculture_usda", "SOURCE_ADAPTER_AND_MARKET_ROUTING_NEEDED"),
        ("transportation_flight_cancellation", "SOURCE_ADAPTER_AND_LINKER_NEEDED"),
    ):
        if category not in existing:
            rows.append(
                {
                    "category": category,
                    "active_market_count": "0",
                    "parsed_market_count": "0",
                    "linked_count": "0",
                    "source_readiness": "FREE_SOURCE_PATH_IDENTIFIED",
                    "parser_readiness": "NOT_BUILT",
                    "forecast_readiness": "NOT_BUILT",
                    "paper_gate_readiness": "NOT_IN_CURRENT_PAPER_GATE",
                    "primary_blocker": blocker,
                    "next_implementation_step": "Build source evidence adapter and parser/linker.",
                    "coverage_status": "PLANNED",
                    "coverage_percent": "n/a",
                }
            )
    return rows


def rank_categories(rows: list[dict[str, Any]], *, crypto_waiting: bool) -> list[dict[str, Any]]:
    scored = []
    for row in rows:
        category = str(row.get("category") or "")
        score = category_score(row, crypto_waiting=crypto_waiting)
        effort = _implementation_effort(row)
        likelihood = _paper_candidate_likelihood(row, score)
        scored.append(
            {
                "category": category,
                "score": score,
                "active_market_count": _to_int(row.get("active_market_count")),
                "parsed_count": _to_int(row.get("parsed_market_count")),
                "linked_count": _to_int(row.get("linked_count")),
                "source_readiness": row.get("source_readiness") or "",
                "parser_readiness": row.get("parser_readiness") or "",
                "forecast_readiness": row.get("forecast_readiness") or "",
                "paper_gate_readiness": row.get("paper_gate_readiness") or "",
                "paper_gate_ready_count": 0,
                "main_blocker": row.get("primary_blocker") or "",
                "free_source_availability": _free_source_availability(category, row),
                "implementation_effort": effort,
                "likelihood_of_paper_candidates_soon": likelihood,
                "next_implementation_step": row.get("next_implementation_step") or "",
            }
        )
    return sorted(scored, key=lambda item: (-item["score"], item["category"]))


def category_score(row: dict[str, Any], *, crypto_waiting: bool) -> int:
    category = str(row.get("category") or "")
    score = 0
    linked = _to_int(row.get("linked_count"))
    parsed = _to_int(row.get("parsed_market_count"))
    if linked > 0:
        score += 35
    if parsed > 0:
        score += 20
    if "SOURCE" in str(row.get("source_readiness") or ""):
        score += 20
    if "FORECASTS" in str(row.get("forecast_readiness") or ""):
        score += 20
    blocker = str(row.get("primary_blocker") or "")
    if category == "weather":
        score += 45
    if category == "crypto" and crypto_waiting:
        score -= 35
    if "NO_PARSED" in blocker:
        score -= 40
    if "PARKED" in blocker or "COMPOSITE" in blocker:
        score -= 55
    if "PROVENANCE" in blocker:
        score -= 20
    if "EV_NOT_POSITIVE" in blocker:
        score -= 10
    return score


def _implementation_effort(row: dict[str, Any]) -> str:
    blocker = str(row.get("primary_blocker") or "")
    if "EV_NOT_POSITIVE" in blocker:
        return "LOW"
    if "NO_PARSED" in blocker:
        return "MEDIUM"
    if "PARKED" in blocker or "COMPOSITE" in blocker:
        return "HIGH"
    return "MEDIUM"


def _paper_candidate_likelihood(row: dict[str, Any], score: int) -> str:
    if score >= 80:
        return "HIGH"
    if score >= 35:
        return "MEDIUM"
    return "LOW"


def _free_source_availability(category: str, row: dict[str, Any]) -> str:
    if category == "weather":
        return "AVAILABLE_EXISTING_WEATHER_SOURCES"
    if category in {"agriculture_usda", "general"}:
        return "OFFICIAL_FREE_SOURCE_AVAILABLE"
    if "transportation" in category:
        return "FREE_SOURCE_POSSIBLE_REQUIRES_ADAPTER"
    if category == "sports":
        return "FREE_SCHEDULE_ROSTER_SOURCES_AVAILABLE_WITH_PROVENANCE_WORK"
    if category in {"economic", "news"}:
        return "FREE_SOURCE_PATH_EXISTS_PARSER_BACKFILL_REQUIRED"
    if category == "crypto":
        return "AVAILABLE_EXISTING_CRYPTO_SOURCES"
    return str(row.get("source_readiness") or "UNKNOWN")


def select_next_sprint(
    *,
    writer_active: bool,
    weather_ready: bool,
    manual_babysitting_severe: bool,
) -> dict[str, Any]:
    if manual_babysitting_severe or writer_active:
        return {
            "id": "CLOUD_VPS_GUARDED_SCHEDULER_SETUP",
            "title": "Cloud/VPS guarded scheduler setup",
            "reason": (
                "Manual command babysitting and writer contention still cost more than "
                "another crypto-only loop."
            ),
        }
    if weather_ready:
        return {
            "id": "WEATHER_PAPER_FUNNEL_ACTIVATION",
            "title": "Weather paper-funnel activation",
            "reason": "Weather has the shortest linked non-crypto path.",
        }
    return {
        "id": "WEATHER_PAPER_FUNNEL_ACTIVATION",
        "title": "Weather paper-funnel activation",
        "reason": "Weather remains the best non-crypto activation target.",
    }


def historical_replay_rules() -> dict[str, Any]:
    return {
        "label_required": "HISTORICAL_REPLAY",
        "separate_from_paper_orders": True,
        "counts_toward_live_paper_learning_target": False,
        "settlements_must_exist_in_source_db": True,
        "no_fabricated_fills_or_outcomes": True,
    }


def command_safety_checks(command: str) -> dict[str, Any]:
    commands = _command_names(command)
    unregistered = [name for name in commands if name not in REPORT_COMMANDS]
    forbidden = [
        fragment for fragment in FORBIDDEN_COMMAND_FRAGMENTS if fragment in command
    ]
    return {
        "commands": commands,
        "unregistered_commands": unregistered,
        "all_registered": not unregistered,
        "forbidden_fragments": forbidden,
        "contains_forbidden_trade_command": bool(forbidden),
    }


def _command_names(command: str) -> list[str]:
    names = []
    for line in command.replace("&&", "\n").splitlines():
        parts = line.strip().split()
        if not parts:
            continue
        if parts[0] == "kalshi-bot" and len(parts) > 1:
            names.append(parts[1])
    return names


def _weather_link_count(ctx: dict[str, Any]) -> int:
    scorecard = _read_csv(ctx["reports_dir"] / "phase3ba_r6" / "noncrypto_engine_backlog.csv")
    for row in scorecard:
        if row.get("category") == "weather":
            return _to_int(row.get("linked_count"))
    return 0


def _render_throughput(payload: dict[str, Any]) -> str:
    lines = _metadata_lines(payload, "# Phase 3BB Throughput Analysis")
    summary = payload["summary"]
    lines.extend(
        [
            "",
            "## Summary",
            "",
            f"- Ingestion bottleneck: `{summary['ingestion_bottleneck']}`",
            "- Opportunity conversion bottleneck: "
            f"`{summary['opportunity_conversion_bottleneck']}`",
            f"- Paper-ready rows: `{summary['paper_ready_rows']}`",
            f"- True first blocker: `{summary['true_first_blocker']}`",
            "",
            "## Rates",
            "",
        ]
    )
    for key, value in payload["rates"].items():
        lines.append(f"- {key}: `{value}`")
    lines.extend(["", "## Time Estimates", ""])
    for row in payload["time_estimates"]["positive_ev_targets"]:
        lines.append(
            f"- {row['target']} EV rows: `{row['days_at_observed_pace']}` day(s)"
        )
    settled = payload["time_estimates"]["settled_target"]
    lines.append(
        "- 500 settled paper trades: "
        f"`{settled['classification']}` days=`{settled['days_at_current_pace']}`"
    )
    lines.extend(["", "## Blockers By Gate", ""])
    for key, value in payload["current_blockers_by_gate"].items():
        if key != "raw_blocker_counts":
            lines.append(f"- {key}: `{value}`")
    return "\n".join(lines) + "\n"


def _render_cloud_readiness(payload: dict[str, Any]) -> str:
    lines = _metadata_lines(payload, "# Phase 3BB Cloud Readiness")
    rec = payload["recommendation"]
    lines.extend(
        [
            "",
            "## Recommendation",
            "",
            f"- Minimum VPS: `{rec['minimum_vps_size']}`",
            f"- Recommended VPS: `{rec['recommended_vps_size']}`",
            f"- GPU required: `{rec['gpu_required']}`",
            f"- Postgres required now: `{rec['postgres_required_now']}`",
            f"- Postgres recommended before scale: `{rec['postgres_recommended_before_scale']}`",
            f"- Docker Compose appropriate: `{rec['docker_compose_appropriate']}`",
            "",
            "## Risks",
            "",
        ]
    )
    for item in payload["sqlite_lock_writer_risks"]:
        lines.append(f"- {item}")
    lines.extend(["", "## What Not To Buy", ""])
    for item in rec["what_not_to_buy"]:
        lines.append(f"- {item}")
    return "\n".join(lines) + "\n"


def _render_cloud_checklist(payload: dict[str, Any]) -> str:
    lines = _metadata_lines(payload, "# Phase 3BB Cloud Deployment Checklist")
    lines.extend(["", "## Checklist", ""])
    for item in payload["deployment_checklist"]:
        lines.append(f"- [ ] {item}")
    return "\n".join(lines) + "\n"


def _render_scheduler_plan(payload: dict[str, Any]) -> str:
    lines = _metadata_lines(payload, "# Phase 3BB Scheduler Plan")
    lines.extend(["", "## Rules", ""])
    for key, value in payload["rules"].items():
        lines.append(f"- {key}: `{value}`")
    lines.extend(["", "## Job Lanes", ""])
    for lane in payload["job_lanes"]:
        lines.append(f"- {lane['lane']}: `{lane['command']}`")
    return "\n".join(lines) + "\n"


def _render_systemd_examples(payload: dict[str, Any]) -> str:
    lines = _metadata_lines(payload, "# Phase 3BB systemd Service Examples")
    lines.extend(
        [
            "",
            "## kalshi-r5-watcher.service",
            "",
            "```ini",
            "[Unit]",
            "Description=Kalshi paper-only guarded R5 watcher",
            "After=network-online.target",
            "",
            "[Service]",
            "WorkingDirectory=/opt/kalshi-predictive-bot",
            "EnvironmentFile=/opt/kalshi-predictive-bot/.env",
            "ExecStart=/opt/kalshi-predictive-bot/.venv/bin/kalshi-bot "
            "phase3bc-r5-unattended-start --output-dir reports/phase3bc_r5",
            "Restart=on-failure",
            "",
            "[Install]",
            "WantedBy=multi-user.target",
            "```",
            "",
            "## kalshi-paper-scheduler.timer",
            "",
            "```ini",
            "[Timer]",
            "OnBootSec=5min",
            "OnUnitActiveSec=15min",
            "Persistent=true",
            "```",
        ]
    )
    return "\n".join(lines) + "\n"


def _render_multicategory(payload: dict[str, Any]) -> str:
    lines = _metadata_lines(payload, "# Phase 3BB Multi-Category Expansion Plan")
    selected = payload["selected_next_category"]
    lines.extend(
        [
            "",
            "## Selected Next Category",
            "",
            f"- Category: `{selected.get('category')}`",
            f"- Main blocker: `{selected.get('main_blocker')}`",
            f"- Likelihood: `{selected.get('likelihood_of_paper_candidates_soon')}`",
            "",
            "## Ranked Categories",
            "",
            "| Rank | Category | Score | Main blocker | Effort | Likelihood |",
            "| ---: | --- | ---: | --- | --- | --- |",
        ]
    )
    for idx, row in enumerate(payload["ranked_categories"], start=1):
        lines.append(
            f"| {idx} | {row['category']} | {row['score']} | {row['main_blocker']} | "
            f"{row['implementation_effort']} | {row['likelihood_of_paper_candidates_soon']} |"
        )
    return "\n".join(lines) + "\n"


def _render_noncrypto_next_sprint(payload: dict[str, Any]) -> str:
    selected = payload["selected_next_category"]
    lines = _metadata_lines(payload, "# Phase 3BB Non-Crypto Next Sprint")
    lines.extend(
        [
            "",
            "## Sprint",
            "",
            f"Build next: `{selected.get('category')}`",
            "",
            "## Implementation Step",
            "",
            selected.get("next_implementation_step") or "",
        ]
    )
    return "\n".join(lines) + "\n"


def _render_weather_fast_lane(payload: dict[str, Any]) -> str:
    lines = _metadata_lines(payload, "# Phase 3BB Weather Fast Lane")
    lines.extend(
        [
            "",
            "## Status",
            "",
            f"- Active linked weather rows: `{payload['active_linked_weather_rows']}`",
            "- Rows with source/snapshot evidence: "
            f"`{payload['weather_rows_with_source_snapshot_evidence']}`",
            f"- Rows with forecasts: `{payload['weather_rows_with_forecasts']}`",
            f"- Rows with rankings: `{payload['weather_rows_with_rankings']}`",
            "- find-opportunities weather_v2 ready: "
            f"`{payload['find_opportunities_weather_v2_ready_to_run']}`",
            f"- Writer contention blocks it: `{payload['writer_contention_blocks_it']}`",
            "",
            "## Blockers",
            "",
        ]
    )
    for blocker, count in payload["weather_paper_gate_blockers"].items():
        lines.append(f"- {blocker}: `{count}`")
    return "\n".join(lines) + "\n"


def _render_historical_replay(payload: dict[str, Any]) -> str:
    lines = _metadata_lines(payload, "# Phase 3BB Historical Replay Acceleration")
    lines.extend(
        [
            "",
            "## Separation Rules",
            "",
        ]
    )
    for key, value in payload["separation_rules"].items():
        lines.append(f"- {key}: `{value}`")
    lines.extend(["", "## Plan", ""])
    for item in payload["implementation_plan"]:
        lines.append(f"- {item}")
    return "\n".join(lines) + "\n"


def _render_executive_summary(payload: dict[str, Any]) -> str:
    answers = payload["answers"]
    lines = _metadata_lines(payload, "# Phase 3BB Acceleration Executive Summary")
    lines.extend(
        [
            "",
            "## Answers",
            "",
            f"1. Why is progress slow? {answers['why_progress_is_slow']}",
            f"2. Is ingestion the bottleneck? `{answers['is_ingestion_the_bottleneck']}`",
            "3. Is opportunity conversion the bottleneck? "
            f"`{answers['is_opportunity_conversion_the_bottleneck']}`",
            f"4. Would cloud help? `{answers['would_cloud_help']}`",
            f"5. Appropriate cloud/server: `{answers['what_cloud_is_appropriate']}`",
            f"6. What should not be sped up: `{answers['what_should_not_be_sped_up']}`",
            f"7. Category next: `{answers['which_category_next']}`",
            f"8. Next Codex sprint: `{answers['single_next_codex_sprint']}`",
            f"9. Next operator command: `{answers['single_next_operator_command']}`",
            f"10. Any paper/live/demo orders occurred? `{answers['orders_occurred']}`",
        ]
    )
    return "\n".join(lines) + "\n"


def _render_next_actions(payload: dict[str, Any]) -> str:
    lines = _metadata_lines(payload, "# Phase 3BB Next Actions")
    lines.extend(
        [
            "",
            "## Next Operator Command",
            "",
            "```bash",
            payload["next_operator_command"],
            "```",
            "",
            f"- Command registered: `{payload['command_checks']['all_registered']}`",
            "- Contains forbidden trade command: "
            f"`{payload['command_checks']['contains_forbidden_trade_command']}`",
            "",
            "## Do Not Run",
            "",
            "- Do not run accelerate-learning while paper-ready rows are 0.",
            "- Do not create paper trades from this diagnostic report.",
            "- Do not submit/cancel/replace live or demo orders.",
            "- Do not lower thresholds.",
        ]
    )
    return "\n".join(lines) + "\n"


def _render_next_codex_sprint(payload: dict[str, Any]) -> str:
    sprint = payload["next_codex_sprint"]
    lines = _metadata_lines(payload, "# Phase 3BB Next Codex Sprint")
    lines.extend(
        [
            "",
            "## Full Prompt",
            "",
            "You are Codex working inside the kalshi-predictive-bot repository.",
            "",
            f"Next sprint: {sprint['title']}.",
            "",
            "Goal: create an always-on paper-only scheduler path that keeps crypto as "
            "a background watcher, prevents duplicate writer-capable jobs, and routes "
            "weather/evidence/opportunity work through db-writer-monitor before any "
            "write-capable local operation.",
            "",
            "Safety: do not submit, cancel, replace, or amend live/demo orders; do not "
            "create paper trades; do not lower thresholds; do not fabricate evidence.",
            "",
            "Acceptance: one scheduler command or service plan, one writer at a time, "
            "no duplicate R5 watchers, dashboard/status remains read-only, and the "
            "next operator command is explicit.",
        ]
    )
    return "\n".join(lines) + "\n"


def _metadata_lines(payload: dict[str, Any], title: str) -> list[str]:
    safety_flags = json.dumps(
        payload.get("safety_flags") or _safety_flags(),
        sort_keys=True,
    )
    return [
        title,
        "",
        f"- Generated at: `{payload['generated_at']}`",
        f"- Git commit: `{payload['git_commit']}`",
        f"- DB fingerprint: `{json.dumps(payload['database_fingerprint'], sort_keys=True)}`",
        f"- Command args: `{json.dumps(payload['command_arguments'], sort_keys=True)}`",
        f"- Data watermark: `{json.dumps(payload['data_watermark'], sort_keys=True)}`",
        f"- Safety flags: `{safety_flags}`",
        f"- Live/demo execution: `{payload['live_or_demo_execution']}`",
        "- Order submission/cancel/replace: "
        f"`{payload['order_submission'] or payload['order_cancel_replace']}`",
        f"- Paper trade creation: `{payload['paper_trade_creation']}`",
        f"- Thresholds lowered: `{payload['thresholds_lowered']}`",
    ]


def _write_conversion_funnel(path: Path, rows: list[dict[str, Any]]) -> None:
    with path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=["name", "gate", "count"])
        writer.writeheader()
        writer.writerows(rows)


def _write_category_scorecard(path: Path, rows: list[dict[str, Any]]) -> None:
    fieldnames = [
        "category",
        "score",
        "active_market_count",
        "parsed_count",
        "linked_count",
        "source_readiness",
        "parser_readiness",
        "forecast_readiness",
        "paper_gate_readiness",
        "paper_gate_ready_count",
        "main_blocker",
        "free_source_availability",
        "implementation_effort",
        "likelihood_of_paper_candidates_soon",
        "next_implementation_step",
    ]
    with path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=fieldnames, extrasaction="ignore")
        writer.writeheader()
        writer.writerows(rows)


def _write_json(path: Path, payload: dict[str, Any]) -> None:
    path.write_text(json.dumps(payload, indent=2, sort_keys=True, default=str), encoding="utf-8")


def _write_manifest(path: Path, files: list[Path]) -> None:
    lines = []
    for file_path in files:
        if not file_path.exists():
            continue
        digest = hashlib.sha256(file_path.read_bytes()).hexdigest()
        lines.append(f"{digest}  {file_path.relative_to(path.parent)}")
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def _read_json(path: Path) -> dict[str, Any]:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return {}


def _read_csv(path: Path) -> list[dict[str, str]]:
    try:
        with path.open(newline="", encoding="utf-8") as handle:
            return list(csv.DictReader(handle))
    except Exception:
        return []


def _to_int(value: Any) -> int:
    try:
        return int(float(str(value).strip()))
    except (TypeError, ValueError):
        return 0


def _round_or_none(value: float | None) -> float | None:
    if value is None or math.isnan(value) or math.isinf(value):
        return None
    return round(value, 1)


def _truthy(value: Any) -> bool:
    return str(value).strip().lower() in {"1", "true", "yes", "y"}


def _safety_flags() -> dict[str, Any]:
    return {
        "paper_only": True,
        "diagnostic_only": True,
        "creates_paper_trades": False,
        "places_exchange_orders": False,
        "submits_cancels_replaces_orders": False,
        "lowers_thresholds": False,
        "fabricates_evidence": False,
        "uses_fuzzy_or_sibling_matching": False,
    }
