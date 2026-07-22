from __future__ import annotations

import json
import os
import time
from collections import Counter
from collections.abc import Callable
from dataclasses import asdict, dataclass
from datetime import UTC, datetime, timedelta
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any

from sqlalchemy import desc, func, or_, select
from sqlalchemy.orm import Session, aliased

from kalshi_predictor.active_universe import is_inactive_market_status
from kalshi_predictor.config import Settings, get_settings
from kalshi_predictor.crypto.assets import DEFAULT_CRYPTO_SYMBOLS
from kalshi_predictor.crypto.linker import link_crypto_markets
from kalshi_predictor.crypto.repository import parse_symbols
from kalshi_predictor.data.locks import db_writer_monitor
from kalshi_predictor.data.repositories import upsert_market
from kalshi_predictor.data.schema import (
    Market,
    MarketRanking,
    MarketSnapshot,
    PaperOrder,
    WeatherMarketLink,
)
from kalshi_predictor.forecasting.registry import run_forecast_models
from kalshi_predictor.ingest.websocket_orderbooks import (
    drain_staged_websocket_orderbooks,
)
from kalshi_predictor.market_legs import parse_and_store_market_legs
from kalshi_predictor.opportunities.scanner import scan_opportunities
from kalshi_predictor.phase3ba_r3 import build_phase3ba_r3_weather_paper_gate
from kalshi_predictor.phase3bc_r5 import (
    write_phase3bc_r5_crypto_freshness_watch_report,
)
from kalshi_predictor.refresh_control_plane import write_refresh_control_plane_bundle
from kalshi_predictor.single_writer_coordinator import (
    drain_staged_crypto_quotes,
    stage_crypto_quote_fetches,
)
from kalshi_predictor.utils.time import utc_now
from kalshi_predictor.weather.features import build_weather_features
from kalshi_predictor.weather.linker import WEATHER_TICKER_PREFIXES, link_weather_markets

PHASE_GH2_VERSION = "GH-3B.0"
CRYPTO_TICKER_PREFIXES = ("KXBTC", "KXETH", "KXSOLE", "KXXRP", "KXDOGE")
ACTIONABLE_MODELS = ("crypto_v2", "weather_v2")
WEATHER_DECISION_LIMIT = 6
WEATHER_FEATURE_LOCATION_LIMIT = 2
WEATHER_FEATURE_FORECAST_LIMIT = 4
SNAPSHOT_RECOVERY_LIMIT = 20
STICKY_CANDIDATE_LIMIT = 12
R5_OWNER_FILE = "phase3bc_r5_owner.json"
PAPER_ONLY_SAFETY = "PAPER_ONLY_NO_ORDER_CREATION_OR_EXCHANGE_WRITES"
MIN_CURRENT_CRYPTO_WINDOWS = 1
MIN_CURRENT_WEATHER_LINKS = 1
MIN_FRESH_CRYPTO_WINDOWS = 1
MIN_FRESH_WEATHER_BOOKS = 1
MIN_FRESH_MANIFEST_RATIO = 0.60


@dataclass(frozen=True)
class GH2Artifacts:
    output_dir: Path
    json_path: Path
    markdown_path: Path
    history_path: Path
    candidate_manifest_path: Path


def stage_gh2_crypto_quotes(
    *,
    staging_dir: Path,
    symbols: str = DEFAULT_CRYPTO_SYMBOLS,
    sources: str = "coinbase",
    max_workers: int = 4,
) -> dict[str, Any]:
    """Fetch external quotes in parallel into files without touching SQLite."""

    result = stage_crypto_quote_fetches(
        symbols=parse_symbols(symbols),
        sources=_parse_csv(sources),
        staging_dir=staging_dir,
        max_workers=max_workers,
    )
    payload = {
        "phase": "GH-2-STAGE",
        "generated_at": utc_now().isoformat(),
        "paper_only_safety": PAPER_ONLY_SAFETY,
        "database_writes": 0,
        "orders_created": 0,
        **result,
    }
    _write_json(staging_dir / "stage_status.json", payload)
    return payload


def select_actionable_ranked_markets(
    session: Session,
    *,
    limit: int = 40,
    max_per_series: int = 6,
    max_ranking_age_hours: int = 24,
    freshness_minutes: int = 15,
    now: datetime | None = None,
    ticker_scope: list[str] | tuple[str, ...] | None = None,
) -> list[dict[str, Any]]:
    """Select active ranked books, favoring fresh executable positive-edge rows."""

    resolved_now = _aware(now or utc_now())
    cutoff = resolved_now - timedelta(hours=max(max_ranking_age_hours, 1))
    scoped_tickers = _bounded_unique(list(ticker_scope or ()), max(len(ticker_scope or ()), 1))
    if ticker_scope is not None and not scoped_tickers:
        return []
    filters = [
        MarketRanking.forecast_model.in_(ACTIONABLE_MODELS),
        MarketRanking.ranked_at >= cutoff,
    ]
    if ticker_scope is not None:
        filters.append(MarketRanking.ticker.in_(scoped_tickers))
    statement = (
        select(MarketRanking)
        .where(*filters)
        .order_by(
            desc(MarketRanking.ranked_at),
            desc(MarketRanking.opportunity_score),
            desc(MarketRanking.id),
        )
        .limit(max(limit * 100, 2000))
    )
    latest_rankings: list[MarketRanking] = []
    seen: set[str] = set()
    for ranking in session.scalars(statement):
        if ranking.ticker in seen:
            continue
        seen.add(ranking.ticker)
        latest_rankings.append(ranking)

    tickers = [ranking.ticker for ranking in latest_rankings]
    markets = (
        {
            market.ticker: market
            for market in session.scalars(select(Market).where(Market.ticker.in_(tickers)))
        }
        if tickers
        else {}
    )
    snapshots = _latest_snapshots(session, tickers)
    candidates: list[dict[str, Any]] = []
    for ranking in latest_rankings:
        market = markets.get(ranking.ticker)
        if market is None or is_inactive_market_status(market.status):
            continue
        if market.close_time is not None and _aware(market.close_time) <= resolved_now:
            continue
        snapshot = snapshots.get(ranking.ticker)
        snapshot_age_minutes = (
            max(0.0, (resolved_now - _aware(snapshot.captured_at)).total_seconds() / 60)
            if snapshot is not None
            else None
        )
        edge = _decimal(ranking.estimated_edge)
        executable = bool(ranking.best_side and ranking.best_price)
        fresh = snapshot_age_minutes is not None and snapshot_age_minutes <= freshness_minutes
        candidates.append(
            {
                "ticker": ranking.ticker,
                "series_ticker": market.series_ticker or ranking.series_ticker,
                "model": ranking.forecast_model,
                "ranked_at": _aware(ranking.ranked_at).isoformat(),
                "snapshot_at": (
                    _aware(snapshot.captured_at).isoformat() if snapshot is not None else None
                ),
                "snapshot_age_minutes": snapshot_age_minutes,
                "estimated_edge": ranking.estimated_edge,
                "opportunity_score": ranking.opportunity_score,
                "best_side": ranking.best_side,
                "best_price": ranking.best_price,
                "fresh": fresh,
                "executable": executable,
                "positive_edge": edge > 0,
                "selection_tier": (
                    "FRESH_EXECUTABLE_POSITIVE_EDGE"
                    if fresh and executable and edge > 0
                    else "RANKED_ACTIVE_FALLBACK"
                ),
                "_sort": (
                    int(fresh and executable and edge > 0),
                    int(executable and edge > 0),
                    edge,
                    _decimal(ranking.opportunity_score),
                    _aware(ranking.ranked_at).timestamp(),
                ),
            }
        )

    candidates.sort(key=lambda row: row["_sort"], reverse=True)
    selected: list[dict[str, Any]] = []
    per_series: Counter[str] = Counter()
    for row in candidates:
        series_key = str(row.get("series_ticker") or "UNKNOWN")
        if per_series[series_key] >= max_per_series:
            continue
        row.pop("_sort", None)
        selected.append(row)
        per_series[series_key] += 1
        if len(selected) >= limit:
            break
    return selected


def run_gh2_single_writer_decision_refresh(
    *,
    session_factory: Callable[[], Session],
    output_dir: Path = Path("reports/phase_gh2"),
    reports_dir: Path = Path("reports"),
    crypto_staging_dir: Path = Path("reports/phase_gh2/crypto_staging"),
    gh1_staging_dir: Path | None = None,
    candidate_manifest_path: Path = Path("reports/phase_gh1/watch/actionable_tickers.json"),
    active_market_catalog_path: Path | None = None,
    settings: Settings | None = None,
    candidate_limit: int = 40,
    active_link_limit: int = 250,
    forecast_limit: int = 250,
    opportunity_limit: int = 100,
    freshness_minutes: int = 15,
    soak_cycles_required: int = 24,
    guard_active_writer: bool = True,
    writer_monitor_fn: Callable[[], dict[str, Any]] | None = None,
) -> GH2Artifacts:
    """Run one bounded paper-only decision refresh under a single writer owner."""

    output_dir.mkdir(parents=True, exist_ok=True)
    reports_dir.mkdir(parents=True, exist_ok=True)
    json_path = output_dir / "gh2_active_candidate_refresh.json"
    markdown_path = output_dir / "gh2_active_candidate_refresh.md"
    history_path = output_dir / "gh2_paper_only_soak_history.jsonl"
    stage_path = output_dir / "gh2_stage.json"
    coverage_checkpoint_path = output_dir / "gh2_active_coverage_checkpoint.json"
    resolved_active_catalog_path = active_market_catalog_path or (
        candidate_manifest_path.parent / "active_market_catalog.json"
    )
    previous_manifest_tickers = _candidate_manifest_tickers(candidate_manifest_path)
    cycle_started_at = utc_now()
    cycle_started_monotonic = time.monotonic()
    cycle_id = f"gh2-{cycle_started_at.strftime('%Y%m%dT%H%M%S%fZ')}"
    stage_started_monotonic = cycle_started_monotonic
    stage_started_at = cycle_started_at
    current_stage: str | None = None
    stage_timings: list[dict[str, Any]] = []

    def mark_stage(stage: str) -> None:
        nonlocal current_stage, stage_started_monotonic, stage_started_at
        now_monotonic = time.monotonic()
        now = utc_now()
        if current_stage is not None:
            stage_timings.append(
                {
                    "stage": current_stage,
                    "started_at": stage_started_at.isoformat(),
                    "completed_at": now.isoformat(),
                    "duration_seconds": round(now_monotonic - stage_started_monotonic, 3),
                    "status": "COMPLETED",
                    "input_rows": None,
                    "output_rows": None,
                    "artifact_mode": "REGENERATED",
                    "reason": None,
                }
            )
        current_stage = stage
        stage_started_monotonic = now_monotonic
        stage_started_at = now
        _write_json(
            stage_path,
            {
                "phase": "GH-2",
                "cycle_id": cycle_id,
                "generated_at": utc_now().isoformat(),
                "stage": stage,
                "paper_order_creation_enabled": False,
                "live_execution_enabled": False,
            },
        )
        print(f"GH-2 stage: {stage}", flush=True)

    mark_stage("preflight_writer_gate")
    resolved = (settings or get_settings()).model_copy(
        update={
            "execution_enabled": False,
            "execution_dry_run": True,
            "autopilot_enabled": False,
            "autopilot_dry_run": True,
        }
    )
    monitor = (writer_monitor_fn or (lambda: db_writer_monitor(settings=resolved)))()
    if guard_active_writer and not bool(monitor.get("safe_to_start_write", True)):
        mark_stage("blocked_active_writer")
        payload = _blocked_payload(monitor)
        _write_cycle_artifacts(json_path, markdown_path, payload)
        return GH2Artifacts(
            output_dir, json_path, markdown_path, history_path, candidate_manifest_path
        )

    stage_errors: list[str] = []
    mark_stage("drain_websocket_stage")
    websocket_drain = drain_staged_websocket_orderbooks(
        session_factory=session_factory,
        staging_dir=gh1_staging_dir or Path(resolved.kalshi_websocket_staging_dir),
        settings=resolved,
        writer_monitor_fn=lambda: {"safe_to_start_write": True},
    )
    stage_errors.extend(str(item) for item in websocket_drain.get("errors") or [])

    mark_stage("open_single_writer_session")
    with session_factory() as session:
        mark_stage("import_active_market_rollover_catalog")
        active_catalog_import = _import_active_market_catalog(
            session,
            resolved_active_catalog_path,
            limit=active_link_limit * 2,
            max_age_minutes=max(freshness_minutes * 2, 15),
        )
        stage_errors.extend(str(item) for item in active_catalog_import.get("errors") or [])
        paper_orders_before = _paper_order_count(session)
        candidates_before = select_actionable_ranked_markets(
            session,
            limit=candidate_limit,
            freshness_minutes=freshness_minutes,
        )
        sticky_before = _fresh_sticky_candidates(
            select_actionable_ranked_markets(
                session,
                limit=min(candidate_limit, STICKY_CANDIDATE_LIMIT),
                max_per_series=candidate_limit,
                freshness_minutes=freshness_minutes,
                ticker_scope=previous_manifest_tickers,
            ),
            limit=min(candidate_limit, STICKY_CANDIDATE_LIMIT),
        )
        active_crypto = _active_market_tickers(
            session,
            prefixes=CRYPTO_TICKER_PREFIXES,
            limit=active_link_limit,
        )
        active_weather = _active_market_tickers(
            session,
            prefixes=WEATHER_TICKER_PREFIXES,
            limit=active_link_limit,
        )
        mark_stage("drain_crypto_quotes")
        crypto_drain = drain_staged_crypto_quotes(
            session,
            staging_dir=crypto_staging_dir,
            build_features_after_drain=True,
            link_crypto_after_drain=False,
        )
        stage_errors.extend(str(item) for item in crypto_drain.get("errors") or [])
        sticky_crypto = [row["ticker"] for row in sticky_before if row["model"] == "crypto_v2"]
        sticky_weather = [
            row["ticker"] for row in sticky_before if row["model"] == "weather_v2"
        ]
        ranked_crypto = [row["ticker"] for row in candidates_before if row["model"] == "crypto_v2"]
        ranked_weather = [
            row["ticker"] for row in candidates_before if row["model"] == "weather_v2"
        ]
        crypto_link_tickers = _bounded_unique(
            sticky_crypto + ranked_crypto + active_crypto,
            active_link_limit,
        )
        weather_link_tickers = _bounded_unique(
            sticky_weather + ranked_weather + active_weather,
            active_link_limit,
        )
        _write_coverage_checkpoint(
            coverage_checkpoint_path,
            stage="PARSE_ACTIVE_MARKET_LEGS",
            active_catalog_import=active_catalog_import,
            crypto_tickers=crypto_link_tickers,
            weather_tickers=weather_link_tickers,
            limit=active_link_limit,
        )
        mark_stage("parse_active_market_legs")
        active_leg_parse = parse_and_store_market_legs(
            session,
            tickers=_bounded_unique(
                crypto_link_tickers + weather_link_tickers,
                active_link_limit * 2,
            ),
            refresh=False,
        )
        mark_stage("link_active_markets")
        crypto_link = link_crypto_markets(
            session,
            tickers=crypto_link_tickers,
            limit=active_link_limit,
        )
        weather_link = link_weather_markets(
            session,
            tickers=weather_link_tickers,
            limit=active_link_limit,
        )
        _write_coverage_checkpoint(
            coverage_checkpoint_path,
            stage="LINKS_COMPLETE",
            active_catalog_import=active_catalog_import,
            crypto_tickers=crypto_link_tickers,
            weather_tickers=weather_link_tickers,
            limit=active_link_limit,
            market_legs=asdict(active_leg_parse),
            crypto=asdict(crypto_link),
            weather=asdict(weather_link),
        )
        weather_decision_tickers = _bounded_unique(
            sticky_weather + ranked_weather + weather_link_tickers,
            WEATHER_DECISION_LIMIT,
        )

        mark_stage("refresh_crypto_decisions")
        crypto_latest = _latest_snapshots(session, crypto_link_tickers)
        crypto_snapshots = [
            crypto_latest[ticker] for ticker in crypto_link_tickers if ticker in crypto_latest
        ][:forecast_limit]
        crypto_forecasts = run_forecast_models(
            session,
            model_name="crypto_v2",
            snapshots=crypto_snapshots,
        )
        crypto_opportunities = scan_opportunities(
            session,
            model_name="crypto_v2",
            limit=opportunity_limit,
            settings=resolved,
            ticker_scope=[snapshot.ticker for snapshot in crypto_snapshots],
            scan_mode="GH2_CURRENT_PAPER_ONLY_REFRESH",
        )

        mark_stage("refresh_weather_decisions")
        weather_features = _build_current_weather_features(
            session,
            weather_decision_tickers,
            settings=resolved,
            max_locations=WEATHER_FEATURE_LOCATION_LIMIT,
            forecasts_per_location=WEATHER_FEATURE_FORECAST_LIMIT,
        )
        weather_latest = _latest_snapshots(session, weather_decision_tickers)
        weather_snapshots = [
            weather_latest[ticker]
            for ticker in weather_decision_tickers
            if ticker in weather_latest
        ][:forecast_limit]
        weather_forecasts = run_forecast_models(
            session,
            model_name="weather_v2",
            snapshots=weather_snapshots,
        )
        weather_opportunities = scan_opportunities(
            session,
            model_name="weather_v2",
            limit=opportunity_limit,
            settings=resolved,
            ticker_scope=[snapshot.ticker for snapshot in weather_snapshots],
            scan_mode="GH2_CURRENT_PAPER_ONLY_REFRESH",
        )

        mark_stage("refresh_r5_diagnostics")
        r5_artifacts = write_phase3bc_r5_crypto_freshness_watch_report(
            session,
            output_dir=reports_dir / "phase3bc_r5",
            phase3bc_output_dir=reports_dir / "phase3bc",
            phase3bc_r3_output_dir=reports_dir / "phase3bc_r3",
            phase3bc_r4_output_dir=reports_dir / "phase3bc_r4",
            phase3bc_r7_output_dir=reports_dir / "phase3bc_r7",
            settings=resolved,
            refresh_open_markets=False,
            external_crypto_ingest=False,
            repair_snapshots=False,
            forecast_current_windows_only=True,
            generate_opportunity_report=False,
            crypto_market_scan_limit=active_link_limit,
            crypto_link_limit=active_link_limit,
            forecast_limit=forecast_limit,
            opportunity_limit=opportunity_limit,
            phase3bc_limit=forecast_limit,
            freshness_minutes=freshness_minutes,
            risk_preflight=False,
            ranking_repair=False,
            ranking_repair_limit=opportunity_limit,
            exact_snapshot_refresh=False,
            near_money_only=False,
            skip_phase3bc_r3_refresh=True,
        )
        r5_payload = _read_json(r5_artifacts.json_path)
        snapshot_recovery_candidates = _snapshot_recovery_candidates(
            r5_payload,
            limit=min(candidate_limit, SNAPSHOT_RECOVERY_LIMIT),
        )
        mark_stage("refresh_weather_gate")
        weather_gate = build_phase3ba_r3_weather_paper_gate(
            session,
            output_dir=reports_dir / "phase3ba_r3",
            reports_dir=reports_dir,
            settings=resolved,
            limit=forecast_limit,
            current_window_lookback_hours=3,
            tickers=weather_decision_tickers,
        )
        mark_stage("write_candidate_manifest")
        candidates_after = select_actionable_ranked_markets(
            session,
            limit=candidate_limit,
            freshness_minutes=freshness_minutes,
        )
        sticky_after = _fresh_sticky_candidates(
            select_actionable_ranked_markets(
                session,
                limit=min(candidate_limit, STICKY_CANDIDATE_LIMIT),
                max_per_series=candidate_limit,
                freshness_minutes=freshness_minutes,
                ticker_scope=previous_manifest_tickers,
            ),
            limit=min(candidate_limit, STICKY_CANDIDATE_LIMIT),
        )
        manifest_candidates = _merge_manifest_candidates(
            candidates_after,
            snapshot_recovery_candidates,
            limit=candidate_limit,
            sticky=sticky_after,
        )
        _write_candidate_manifest(candidate_manifest_path, manifest_candidates)
        paper_orders_after = _paper_order_count(session)
        mark_stage("commit_single_writer")
        session.commit()

    crypto_drain["files_archived"] = _archive_drained_files(
        [Path(path) for path in crypto_drain.get("drained_files") or []],
        archive_dir=crypto_staging_dir / "drained",
    )
    r5_summary = r5_payload.get("latest_summary") or r5_payload.get("summary") or {}
    weather_summary = weather_gate.get("summary") or {}
    crypto_paper_ready = int(r5_summary.get("paper_ready_candidates") or 0)
    weather_paper_ready = int(weather_summary.get("paper_ready_rows") or 0)
    rankings_inserted = int(crypto_opportunities.rankings_inserted) + int(
        weather_opportunities.rankings_inserted
    )
    if rankings_inserted == 0:
        rankings_inserted = sum(
            1
            for row in manifest_candidates
            if _aware(datetime.fromisoformat(row["ranked_at"]))
            >= utc_now() - timedelta(minutes=freshness_minutes)
        )
    fresh_candidate_count = sum(
        1
        for row in manifest_candidates
        if row.get("fresh")
        and _aware(datetime.fromisoformat(row["ranked_at"]))
        >= utc_now() - timedelta(minutes=freshness_minutes)
    )
    paper_orders_created = paper_orders_after - paper_orders_before
    soak_quality = _build_soak_quality(
        r5_summary=r5_summary,
        weather_summary=weather_summary,
        manifest_count=len(manifest_candidates),
        fresh_manifest_count=fresh_candidate_count,
    )
    cycle_failure_reasons = []
    if stage_errors:
        cycle_failure_reasons.append("source_or_stage_errors")
    if rankings_inserted <= 0:
        cycle_failure_reasons.append("no_rankings_inserted_or_fresh")
    if fresh_candidate_count <= 0:
        cycle_failure_reasons.append("no_fresh_ranked_candidates")
    if paper_orders_created != 0:
        cycle_failure_reasons.append("paper_orders_created_during_soak")
    cycle_failure_reasons.extend(soak_quality["failure_reasons"])
    cycle_healthy = not cycle_failure_reasons
    soak = _record_soak_cycle(
        history_path,
        healthy=cycle_healthy,
        paper_ready_candidates=crypto_paper_ready + weather_paper_ready,
        positive_ev_rows=int(r5_summary.get("positive_ev_rows") or 0)
        + int(weather_summary.get("positive_executable_ev_rows") or 0),
        rankings_inserted=rankings_inserted,
        fresh_ranked_candidates=fresh_candidate_count,
        blocker_counts=dict(
            Counter(
                str(gate)
                for row in manifest_candidates
                for gate in (row.get("blocking_gates") or [])
            )
        ),
        reset_reason=", ".join(cycle_failure_reasons) if cycle_failure_reasons else None,
        required_cycles=soak_cycles_required,
        soak_quality=soak_quality,
    )
    candidate_diagnostics = _build_candidate_diagnostics(
        r5_payload=r5_payload,
        weather_rows=list(weather_gate.get("weather_rows") or []),
    )
    payload = {
        "phase": "GH-2",
        "cycle_id": cycle_id,
        "phase_version": PHASE_GH2_VERSION,
        "generated_at": utc_now().isoformat(),
        "status": (
            "PAPER_ONLY_SOAK_COMPLETE"
            if soak["soak_complete"]
            else "PAPER_ONLY_SOAK_RUNNING"
            if cycle_healthy
            else "CYCLE_NEEDS_ATTENTION"
        ),
        "paper_only_safety": PAPER_ONLY_SAFETY,
        "writer_monitor_at_start": monitor,
        "cycle_telemetry": {
            "started_at": cycle_started_at.isoformat(),
            "completed_at": utc_now().isoformat(),
            "runtime_seconds": round(time.monotonic() - cycle_started_monotonic, 3),
            "lock_wait_seconds": _float_or_zero(os.getenv("GH2_LOCK_WAIT_SECONDS")),
            "stages": stage_timings,
        },
        "websocket_drain": websocket_drain,
        "crypto_quote_drain": crypto_drain,
        "active_linking": {
            "crypto_candidates": len(active_crypto),
            "weather_candidates": len(active_weather),
            "weather_decision_candidates": len(weather_decision_tickers),
            "market_legs": asdict(active_leg_parse),
            "crypto": asdict(crypto_link),
            "weather": asdict(weather_link),
            "rollover_catalog": active_catalog_import,
            "coverage_checkpoint_path": str(coverage_checkpoint_path),
        },
        "decision_refresh": {
            "crypto_forecasts": asdict(crypto_forecasts),
            "crypto_rankings_inserted": crypto_opportunities.rankings_inserted,
            "crypto_opportunities_detected": crypto_opportunities.opportunities_detected,
            "weather_features": weather_features,
            "weather_forecasts": asdict(weather_forecasts),
            "weather_rankings_inserted": weather_opportunities.rankings_inserted,
            "weather_opportunities_detected": weather_opportunities.opportunities_detected,
            "crypto_r5_report": str(r5_artifacts.json_path),
            "rankings_inserted_or_fresh": rankings_inserted,
            "fresh_ranked_candidates": fresh_candidate_count,
        },
        "candidate_alignment": {
            "before_count": len(candidates_before),
            "after_count": len(candidates_after),
            "ranked_candidates": len(candidates_after),
            "snapshot_recovery_candidates": len(snapshot_recovery_candidates),
            "sticky_candidates": len(sticky_after),
            "manifest_count": len(manifest_candidates),
            "manifest_path": str(candidate_manifest_path),
            "tickers": [row["ticker"] for row in manifest_candidates],
            "snapshot_recovery_tickers": [row["ticker"] for row in snapshot_recovery_candidates],
            "sticky_tickers": [row["ticker"] for row in sticky_after],
            "warmup_tickers": [
                row["ticker"]
                for row in manifest_candidates
                if row["ticker"] not in {item["ticker"] for item in sticky_after}
            ],
        },
        "paper_readiness": {
            "crypto_paper_ready_candidates": crypto_paper_ready,
            "crypto_positive_ev_rows": int(r5_summary.get("positive_ev_rows") or 0),
            "weather_paper_ready_candidates": weather_paper_ready,
            "weather_positive_ev_rows": int(
                weather_summary.get("positive_executable_ev_rows") or 0
            ),
            "total_paper_ready_candidates": crypto_paper_ready + weather_paper_ready,
        },
        "weather_gate": {
            "generated_at": weather_gate.get("generated_at"),
            "status": weather_gate.get("status"),
            "summary": weather_summary,
            "weather_rows": list(weather_gate.get("weather_rows") or []),
            "next_action": weather_gate.get("next_action") or {},
        },
        "soak_quality": soak_quality,
        "candidate_diagnostics": candidate_diagnostics,
        "soak": soak,
        "errors": stage_errors,
        "safety": {
            "paper_orders_before": paper_orders_before,
            "paper_orders_after": paper_orders_after,
            "paper_orders_created": paper_orders_created,
            "paper_order_creation_enabled": False,
            "live_execution_enabled": False,
            "autopilot_enabled": False,
            "explicit_operator_approval_required_after_soak": True,
        },
    }
    _enrich_stage_telemetry(
        stage_timings,
        websocket_drain=websocket_drain,
        crypto_drain=crypto_drain,
        active_leg_parse=asdict(active_leg_parse),
        crypto_link=asdict(crypto_link),
        weather_link=asdict(weather_link),
        crypto_forecasts=asdict(crypto_forecasts),
        weather_forecasts=asdict(weather_forecasts),
        crypto_opportunities=asdict(crypto_opportunities),
        weather_opportunities=asdict(weather_opportunities),
        candidate_alignment=payload["candidate_alignment"],
        stage_errors=stage_errors,
    )
    payload["control_plane"] = write_refresh_control_plane_bundle(
        payload,
        output_dir=output_dir,
        candidate_manifest_path=candidate_manifest_path,
    )
    mark_stage("write_cycle_report")
    _write_cycle_artifacts(json_path, markdown_path, payload)
    _write_r5_owner_status(
        reports_dir / "phase3bc_r5" / R5_OWNER_FILE,
        r5_payload=r5_payload,
        cadence_minutes=15,
        status=("SCHEDULED_OWNER_HEALTHY" if cycle_healthy else "SCHEDULED_OWNER_NEEDS_ATTENTION"),
    )
    mark_stage("complete")
    return GH2Artifacts(output_dir, json_path, markdown_path, history_path, candidate_manifest_path)


def _active_market_tickers(
    session: Session,
    *,
    prefixes: tuple[str, ...],
    limit: int,
) -> list[str]:
    now = utc_now()
    filters = [
        or_(
            Market.ticker.like(f"{prefix}%"),
            Market.series_ticker.like(f"{prefix}%"),
        )
        for prefix in prefixes
    ]
    statement = (
        select(Market.ticker)
        .where(
            func.lower(func.coalesce(Market.status, "")).in_(("active", "open")),
            or_(Market.close_time.is_(None), Market.close_time > now),
            or_(*filters),
        )
        .order_by(Market.close_time.is_(None), Market.close_time, desc(Market.last_seen_at))
        .limit(limit)
    )
    return list(session.scalars(statement))


def _import_active_market_catalog(
    session: Session,
    path: Path,
    *,
    limit: int,
    max_age_minutes: int = 30,
) -> dict[str, Any]:
    payload = _read_json(path)
    raw_markets = payload.get("markets") or []
    generated_at = _optional_datetime(payload.get("generated_at"))
    age_minutes = (
        max(0.0, (utc_now() - generated_at).total_seconds() / 60)
        if generated_at is not None
        else None
    )
    if payload and (age_minutes is None or age_minutes > max_age_minutes):
        return {
            "status": "STALE_NOT_IMPORTED",
            "path": str(path),
            "catalog_generated_at": payload.get("generated_at"),
            "catalog_age_minutes": age_minutes,
            "max_age_minutes": max_age_minutes,
            "rows_seen": len(raw_markets),
            "rows_imported": 0,
            "imported_tickers": [],
            "errors": [],
        }
    imported: list[str] = []
    errors: list[str] = []
    for raw in raw_markets[: max(limit, 0)]:
        if not isinstance(raw, dict):
            continue
        ticker = str(raw.get("ticker") or "").strip()
        if not ticker:
            continue
        try:
            normalized = dict(raw)
            normalized["ticker"] = ticker
            normalized.setdefault("source", "kalshi_rest_active_market_discovery")
            normalized.setdefault(
                "source_observed_at",
                payload.get("generated_at") or utc_now().isoformat(),
            )
            upsert_market(session, normalized)
        except (TypeError, ValueError) as exc:
            errors.append(f"{ticker}: {exc}")
            continue
        imported.append(ticker)
    return {
        "status": (
            "NOT_AVAILABLE"
            if not payload
            else "COMPLETE_WITH_ERRORS"
            if errors
            else "COMPLETE"
        ),
        "path": str(path),
        "catalog_generated_at": payload.get("generated_at"),
        "catalog_age_minutes": age_minutes,
        "max_age_minutes": max_age_minutes,
        "rows_seen": len(raw_markets),
        "rows_imported": len(imported),
        "imported_tickers": imported,
        "errors": errors,
    }


def _write_coverage_checkpoint(
    path: Path,
    *,
    stage: str,
    active_catalog_import: dict[str, Any],
    crypto_tickers: list[str],
    weather_tickers: list[str],
    limit: int,
    **progress: Any,
) -> None:
    _write_json(
        path,
        {
            "phase": "GH-3B-BOUNDED-CATEGORY-COVERAGE",
            "generated_at": utc_now().isoformat(),
            "stage": stage,
            "batch_limit_per_category": limit,
            "crypto_tickers_attempted": len(crypto_tickers),
            "weather_tickers_attempted": len(weather_tickers),
            "active_catalog_import": active_catalog_import,
            "progress": progress,
            "single_writer_required": True,
            "paper_order_creation_enabled": False,
            "live_execution_enabled": False,
        },
    )


def _build_current_weather_features(
    session: Session,
    tickers: list[str],
    *,
    settings: Settings,
    max_locations: int = WEATHER_FEATURE_LOCATION_LIMIT,
    forecasts_per_location: int = WEATHER_FEATURE_FORECAST_LIMIT,
) -> list[dict[str, Any]]:
    if not tickers:
        return []
    locations = list(
        session.scalars(
            select(WeatherMarketLink.location_key)
            .where(WeatherMarketLink.ticker.in_(tickers))
            .distinct()
            .limit(max_locations)
        )
    )[:max_locations]
    summaries = []
    for location in locations:
        summary = build_weather_features(
            session,
            location_key=location,
            settings=settings,
            limit=forecasts_per_location,
        )
        summaries.append(asdict(summary))
    return summaries


def _latest_snapshots(
    session: Session,
    tickers: list[str],
) -> dict[str, MarketSnapshot]:
    if not tickers:
        return {}
    statement = (
        select(
            MarketSnapshot,
            func.row_number()
            .over(
                partition_by=MarketSnapshot.ticker,
                order_by=(desc(MarketSnapshot.captured_at), desc(MarketSnapshot.id)),
            )
            .label("row_number"),
        )
        .where(MarketSnapshot.ticker.in_(tickers))
        .subquery()
    )
    snapshot = aliased(MarketSnapshot, statement)
    return {
        row.ticker: row
        for row in session.scalars(select(snapshot).where(statement.c.row_number == 1))
    }


def _paper_order_count(session: Session) -> int:
    return int(session.scalar(select(func.count(PaperOrder.id))) or 0)


def _archive_drained_files(files: list[Path], *, archive_dir: Path) -> int:
    archived = 0
    for path in files:
        if not path.exists():
            continue
        archive_dir.mkdir(parents=True, exist_ok=True)
        destination = archive_dir / path.name
        path.replace(destination)
        archived += 1
    return archived


def _write_candidate_manifest(path: Path, candidates: list[dict[str, Any]]) -> None:
    _write_json(
        path,
        {
            "phase": "GH-2",
            "generated_at": utc_now().isoformat(),
            "selection": "STICKY_FRESH_THEN_CURRENT_RANKINGS_WITH_SNAPSHOT_RECOVERY",
            "tickers": [row["ticker"] for row in candidates],
            "candidates": candidates,
            "paper_only_safety": PAPER_ONLY_SAFETY,
        },
    )


def _snapshot_recovery_candidates(
    r5_payload: dict[str, Any],
    *,
    limit: int,
) -> list[dict[str, Any]]:
    if limit <= 0:
        return []
    raw_rows = r5_payload.get("blocked_active_pure_examples") or []
    generated_at = str(r5_payload.get("generated_at") or utc_now().isoformat())
    rows: list[dict[str, Any]] = []
    seen: set[str] = set()
    for raw in raw_rows:
        if not isinstance(raw, dict):
            continue
        ticker = str(raw.get("ticker") or "").strip()
        missing_snapshot = str(
            raw.get("blocked_reason") or ""
        ) == "BLOCKED_MISSING_ACTIVE_SNAPSHOT" or (
            raw.get("latest_snapshot_at") is None
            and str(raw.get("readiness_status") or "") == "BLOCKED_MISSING_ACTIVE_SNAPSHOT"
        )
        if not ticker or ticker in seen or not missing_snapshot:
            continue
        if not ticker.startswith(CRYPTO_TICKER_PREFIXES):
            continue
        seen.add(ticker)
        rows.append(
            {
                "ticker": ticker,
                "series_ticker": raw.get("series_ticker"),
                "model": "crypto_v2",
                "ranked_at": generated_at,
                "snapshot_at": None,
                "snapshot_age_minutes": None,
                "estimated_edge": None,
                "opportunity_score": None,
                "best_side": None,
                "best_price": None,
                "fresh": False,
                "executable": False,
                "positive_edge": False,
                "selection_tier": "MISSING_SNAPSHOT_RECOVERY",
                "blocking_gates": ["snapshot_missing"],
            }
        )
        if len(rows) >= limit:
            break
    return rows


def _build_candidate_diagnostics(
    *,
    r5_payload: dict[str, Any],
    weather_rows: list[dict[str, Any]],
    limit_per_category: int = 12,
) -> dict[str, Any]:
    crypto_rows: list[dict[str, Any]] = []
    seen: set[str] = set()
    for section in (
        "blocked_active_pure_examples",
        "liquidity_watch_rows",
        "best_ev_candidates",
    ):
        for raw in r5_payload.get(section) or []:
            if not isinstance(raw, dict):
                continue
            ticker = str(raw.get("ticker") or "").strip()
            if not ticker or ticker in seen:
                continue
            failed = _crypto_failed_gates(raw)
            if not failed:
                continue
            seen.add(ticker)
            crypto_rows.append(
                {
                    "ticker": ticker,
                    "category": "crypto",
                    "source": raw.get("source_lineage") or "Kalshi/Coinbase",
                    "source_ready": not any("source" in gate.lower() for gate in failed),
                    "book_ready": not any(
                        token in " ".join(failed).lower()
                        for token in ("book", "snapshot", "price", "liquidity", "spread")
                    ),
                    "quote_age_minutes": raw.get("snapshot_age_minutes")
                    or raw.get("quote_age_minutes"),
                    "raw_ev": raw.get("expected_value")
                    or raw.get("expected_value_cents"),
                    "executable_ev": raw.get("expected_value"),
                    "spread": raw.get("spread"),
                    "liquidity": raw.get("liquidity_score") or raw.get("liquidity"),
                    "ranking_ready": not any("ranking" in gate.lower() for gate in failed),
                    "risk_ready": not any("risk" in gate.lower() for gate in failed),
                    "expired": not bool(raw.get("active_market", True)),
                    "failed_gates": failed,
                }
            )
            if len(crypto_rows) >= limit_per_category:
                break
        if len(crypto_rows) >= limit_per_category:
            break

    normalized_weather = []
    for raw in weather_rows[:limit_per_category]:
        failed = [str(item) for item in raw.get("failed_gates") or []]
        normalized_weather.append(
            {
                "ticker": raw.get("ticker"),
                "category": "weather",
                "source": raw.get("source_lineage"),
                "source_ready": bool(raw.get("source_identity_ready")),
                "book_ready": bool(raw.get("executable_book")),
                "quote_age_minutes": raw.get("snapshot_age_minutes"),
                "raw_ev": raw.get("raw_ev"),
                "executable_ev": raw.get("executable_ev"),
                "spread": raw.get("spread"),
                "liquidity": raw.get("liquidity_score") or raw.get("liquidity"),
                "ranking_ready": bool(raw.get("has_current_ranking")),
                "risk_ready": bool(
                    raw.get("phase3s_proceed")
                    and raw.get("phase3m_nonzero_size")
                    and raw.get("phase3n_approved")
                ),
                "expired": not bool(raw.get("current_window_eligible")),
                "failed_gates": failed or [str(raw.get("first_blocker") or "UNKNOWN")],
            }
        )
    rows = crypto_rows + normalized_weather
    return {
        "row_count": len(rows),
        "crypto_rows": len(crypto_rows),
        "weather_rows": len(normalized_weather),
        "rows": rows,
    }


def _crypto_failed_gates(row: dict[str, Any]) -> list[str]:
    failed = [
        str(item)
        for item in (row.get("preflight_blockers") or row.get("blocking_gates") or [])
        if str(item).strip()
    ]
    reason = str(row.get("blocked_reason") or row.get("readiness_status") or "").strip()
    if reason and reason != "PAPER_READY_CANDIDATE":
        failed.append(reason)
    if row.get("latest_snapshot_at") is None:
        failed.append("SNAPSHOT_MISSING")
    if row.get("best_price") is None:
        failed.append("EXECUTABLE_PRICE_MISSING")
    try:
        if Decimal(str(row.get("expected_value") or "0")) <= 0:
            failed.append("EV_NOT_POSITIVE")
    except (InvalidOperation, TypeError, ValueError):
        failed.append("EV_MISSING")
    return list(dict.fromkeys(failed))


def _merge_manifest_candidates(
    ranked: list[dict[str, Any]],
    recovery: list[dict[str, Any]],
    *,
    limit: int,
    sticky: list[dict[str, Any]] | None = None,
) -> list[dict[str, Any]]:
    if limit <= 0:
        return []
    recovery_rows = recovery[:limit]
    ranked_budget = max(limit - len(recovery_rows), 0)
    sticky_rows = list((sticky or [])[: min(STICKY_CANDIDATE_LIMIT, ranked_budget)])
    deduped: list[dict[str, Any]] = []
    seen: set[str] = set()

    def append_unique(rows: list[dict[str, Any]], *, stop_at: int) -> None:
        for row in rows:
            if len(deduped) >= stop_at:
                break
            ticker = str(row.get("ticker") or "").strip()
            if not ticker or ticker in seen:
                continue
            seen.add(ticker)
            deduped.append(row)

    ranked_rows = sticky_rows + list(ranked)
    append_unique(ranked_rows, stop_at=ranked_budget)
    append_unique(list(recovery_rows), stop_at=limit)
    append_unique(ranked_rows, stop_at=limit)
    return deduped


def _candidate_manifest_tickers(path: Path) -> list[str]:
    payload = _read_json(path)
    raw_tickers = payload.get("tickers") or []
    return _bounded_unique(
        [str(ticker).strip() for ticker in raw_tickers if str(ticker).strip()],
        STICKY_CANDIDATE_LIMIT,
    )


def _fresh_sticky_candidates(
    candidates: list[dict[str, Any]],
    *,
    limit: int,
) -> list[dict[str, Any]]:
    rows = []
    for candidate in candidates:
        if not candidate.get("fresh"):
            continue
        row = dict(candidate)
        row["selection_tier"] = "STICKY_FRESH"
        rows.append(row)
        if len(rows) >= limit:
            break
    return rows


def _float_or_zero(value: Any) -> float:
    try:
        return max(float(value or 0), 0.0)
    except (TypeError, ValueError):
        return 0.0


def _enrich_stage_telemetry(
    stages: list[dict[str, Any]],
    *,
    websocket_drain: dict[str, Any],
    crypto_drain: dict[str, Any],
    active_leg_parse: dict[str, Any],
    crypto_link: dict[str, Any],
    weather_link: dict[str, Any],
    crypto_forecasts: dict[str, Any],
    weather_forecasts: dict[str, Any],
    crypto_opportunities: dict[str, Any],
    weather_opportunities: dict[str, Any],
    candidate_alignment: dict[str, Any],
    stage_errors: list[str],
) -> None:
    evidence: dict[str, tuple[int | None, int | None, str]] = {
        "drain_websocket_stage": (
            _metric(websocket_drain, "files_found", "files_processed", "staged_files"),
            _metric(websocket_drain, "snapshots_inserted", "snapshots_written", "files_drained"),
            "REGENERATED",
        ),
        "drain_crypto_quotes": (
            _metric(crypto_drain, "files_found", "files_processed", "staged_files"),
            _metric(crypto_drain, "quotes_inserted", "features_inserted", "files_archived"),
            "REGENERATED",
        ),
        "parse_active_market_legs": (
            _metric(active_leg_parse, "markets_processed", "markets_seen"),
            _metric(active_leg_parse, "legs_inserted", "legs_parsed", "markets_linked"),
            "REGENERATED",
        ),
        "link_active_markets": (
            _metric(crypto_link, "markets_checked", "markets_processed")
            + _metric(weather_link, "markets_checked", "markets_processed"),
            _metric(crypto_link, "links_inserted", "linked")
            + _metric(weather_link, "links_inserted", "linked"),
            "REGENERATED",
        ),
        "refresh_crypto_decisions": (
            _metric(crypto_forecasts, "snapshots_processed", "forecasts_attempted"),
            _metric(crypto_forecasts, "forecasts_inserted")
            + _metric(crypto_opportunities, "rankings_inserted"),
            "REGENERATED",
        ),
        "refresh_weather_decisions": (
            _metric(weather_forecasts, "snapshots_processed", "forecasts_attempted"),
            _metric(weather_forecasts, "forecasts_inserted")
            + _metric(weather_opportunities, "rankings_inserted"),
            "REGENERATED",
        ),
        "write_candidate_manifest": (
            int(candidate_alignment.get("before_count") or 0),
            int(candidate_alignment.get("manifest_count") or 0),
            "REGENERATED",
        ),
    }
    for row in stages:
        stage = str(row.get("stage") or "")
        if stage in evidence:
            row["input_rows"], row["output_rows"], row["artifact_mode"] = evidence[stage]
        matching_errors = [error for error in stage_errors if stage in error]
        if matching_errors:
            row["status"] = "DEGRADED"
            row["reason"] = "; ".join(matching_errors[:3])


def _metric(payload: dict[str, Any], *keys: str) -> int:
    for key in keys:
        value = payload.get(key)
        if isinstance(value, (int, float)):
            return int(value)
    return 0


def _write_r5_owner_status(
    path: Path,
    *,
    r5_payload: dict[str, Any],
    cadence_minutes: int,
    status: str,
) -> None:
    _write_json(
        path,
        {
            "owner": "GH-2_SINGLE_WRITER_DECISION_REFRESH",
            "status": status,
            "generated_at": utc_now().isoformat(),
            "r5_report_generated_at": r5_payload.get("generated_at"),
            "cadence_minutes": cadence_minutes,
            "paper_only_safety": PAPER_ONLY_SAFETY,
            "paper_order_creation_enabled": False,
            "live_execution_enabled": False,
        },
    )


def _record_soak_cycle(
    path: Path,
    *,
    healthy: bool,
    paper_ready_candidates: int,
    positive_ev_rows: int,
    rankings_inserted: int,
    fresh_ranked_candidates: int,
    reset_reason: str | None,
    required_cycles: int,
    blocker_counts: dict[str, int] | None = None,
    soak_quality: dict[str, Any] | None = None,
) -> dict[str, Any]:
    history = _read_json_lines(path)[-95:]
    history.append(
        {
            "generated_at": utc_now().isoformat(),
            "healthy": healthy,
            "paper_ready_candidates": paper_ready_candidates,
            "positive_ev_rows": positive_ev_rows,
            "rankings_inserted": rankings_inserted,
            "fresh_ranked_candidates": fresh_ranked_candidates,
            "blocker_counts": blocker_counts or {},
            "reset_reason": reset_reason,
            "soak_quality": soak_quality or {},
        }
    )
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(f"{path.suffix}.tmp")
    temporary.write_text(
        "".join(json.dumps(row, sort_keys=True) + "\n" for row in history),
        encoding="utf-8",
    )
    temporary.replace(path)
    consecutive = 0
    for row in reversed(history):
        if not row.get("healthy"):
            break
        consecutive += 1
    window = history[-required_cycles:] if required_cycles > 0 else history
    has_candidate = any(int(row.get("paper_ready_candidates") or 0) > 0 for row in window)
    complete = consecutive >= required_cycles and has_candidate
    return {
        "healthy_cycle": healthy,
        "consecutive_healthy_cycles": consecutive,
        "required_healthy_cycles": required_cycles,
        "paper_ready_seen_in_required_window": has_candidate,
        "soak_complete": complete,
        "paper_order_creation_enabled": False,
        "quality_gates_passed": bool((soak_quality or {}).get("passed", healthy)),
    }


def _build_soak_quality(
    *,
    r5_summary: dict[str, Any],
    weather_summary: dict[str, Any],
    manifest_count: int,
    fresh_manifest_count: int,
) -> dict[str, Any]:
    current_crypto = int(r5_summary.get("current_active_window_rows") or 0)
    crypto_missing = int(r5_summary.get("snapshot_missing_rows") or 0)
    crypto_stale = int(r5_summary.get("snapshot_stale_rows") or 0)
    fresh_crypto = max(0, current_crypto - crypto_missing - crypto_stale)
    current_weather = int(weather_summary.get("current_weather_links") or 0)
    fresh_weather = int(weather_summary.get("fresh_snapshot_rows") or 0)
    fresh_ratio = fresh_manifest_count / manifest_count if manifest_count else 0.0
    checks = {
        "current_crypto_windows": current_crypto >= MIN_CURRENT_CRYPTO_WINDOWS,
        "fresh_crypto_windows": fresh_crypto >= MIN_FRESH_CRYPTO_WINDOWS,
        "current_weather_links": current_weather >= MIN_CURRENT_WEATHER_LINKS,
        "fresh_weather_books": fresh_weather >= MIN_FRESH_WEATHER_BOOKS,
        "manifest_present": manifest_count > 0,
        "manifest_freshness": fresh_ratio >= MIN_FRESH_MANIFEST_RATIO,
    }
    failure_reasons = [
        f"soak_quality_{name}_failed" for name, passed in checks.items() if not passed
    ]
    return {
        "passed": not failure_reasons,
        "checks": checks,
        "failure_reasons": failure_reasons,
        "observed": {
            "current_crypto_windows": current_crypto,
            "fresh_crypto_windows": fresh_crypto,
            "crypto_snapshot_missing": crypto_missing,
            "crypto_snapshot_stale": crypto_stale,
            "current_weather_links": current_weather,
            "fresh_weather_books": fresh_weather,
            "manifest_candidates": manifest_count,
            "fresh_manifest_candidates": fresh_manifest_count,
            "fresh_manifest_ratio": round(fresh_ratio, 3),
        },
        "required": {
            "current_crypto_windows": MIN_CURRENT_CRYPTO_WINDOWS,
            "fresh_crypto_windows": MIN_FRESH_CRYPTO_WINDOWS,
            "current_weather_links": MIN_CURRENT_WEATHER_LINKS,
            "fresh_weather_books": MIN_FRESH_WEATHER_BOOKS,
            "fresh_manifest_ratio": MIN_FRESH_MANIFEST_RATIO,
        },
    }


def _blocked_payload(monitor: dict[str, Any]) -> dict[str, Any]:
    return {
        "phase": "GH-2",
        "phase_version": PHASE_GH2_VERSION,
        "generated_at": utc_now().isoformat(),
        "status": "BLOCKED_ACTIVE_WRITER",
        "writer_monitor_at_start": monitor,
        "paper_only_safety": PAPER_ONLY_SAFETY,
        "errors": ["Active writer detected; GH-2 did not open SQLite."],
        "safety": {
            "paper_order_creation_enabled": False,
            "live_execution_enabled": False,
            "orders_created": 0,
        },
    }


def _render_markdown(payload: dict[str, Any]) -> str:
    readiness = payload.get("paper_readiness") or {}
    soak = payload.get("soak") or {}
    safety = payload.get("safety") or {}
    return "\n".join(
        [
            "# GH-2 Active Candidate Alignment and Decision Refresh",
            "",
            f"- Status: `{payload.get('status')}`",
            f"- Generated: `{payload.get('generated_at')}`",
            f"- Paper-ready candidates: `{readiness.get('total_paper_ready_candidates', 0)}`",
            f"- Consecutive healthy soak cycles: `{soak.get('consecutive_healthy_cycles', 0)}`",
            f"- Soak complete: `{soak.get('soak_complete', False)}`",
            f"- Paper orders created: `{safety.get('paper_orders_created', 0)}`",
            "- Paper-order creation: `DISABLED`",
            "- Live execution: `DISABLED`",
            "",
        ]
    )


def _write_cycle_artifacts(path: Path, markdown_path: Path, payload: dict[str, Any]) -> None:
    _write_json(path, payload)
    markdown_path.parent.mkdir(parents=True, exist_ok=True)
    markdown_path.write_text(_render_markdown(payload), encoding="utf-8")


def _write_json(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(f"{path.suffix}.tmp")
    temporary.write_text(
        json.dumps(payload, indent=2, sort_keys=True, default=str),
        encoding="utf-8",
    )
    temporary.replace(path)


def _read_json(path: Path) -> dict[str, Any]:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, TypeError, ValueError):
        return {}
    return payload if isinstance(payload, dict) else {}


def _read_json_lines(path: Path) -> list[dict[str, Any]]:
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except OSError:
        return []
    rows: list[dict[str, Any]] = []
    for line in lines:
        try:
            row = json.loads(line)
        except (TypeError, ValueError):
            continue
        if isinstance(row, dict):
            rows.append(row)
    return rows


def _parse_csv(value: str) -> list[str]:
    return list(dict.fromkeys(item.strip().lower() for item in value.split(",") if item.strip()))


def _bounded_unique(values: list[str], limit: int) -> list[str]:
    return list(dict.fromkeys(value for value in values if value))[:limit]


def _decimal(value: Any) -> Decimal:
    try:
        return Decimal(str(value or "0"))
    except (InvalidOperation, TypeError, ValueError):
        return Decimal("0")


def _aware(value: datetime) -> datetime:
    return value.replace(tzinfo=UTC) if value.tzinfo is None else value.astimezone(UTC)


def _optional_datetime(value: Any) -> datetime | None:
    try:
        return _aware(datetime.fromisoformat(str(value).replace("Z", "+00:00")))
    except (TypeError, ValueError):
        return None
