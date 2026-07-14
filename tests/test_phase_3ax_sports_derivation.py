import json
from datetime import timedelta
from pathlib import Path

from sqlalchemy import func, select
from typer.testing import CliRunner

from kalshi_predictor.cli import app
from kalshi_predictor.config import Settings, get_settings
from kalshi_predictor.data.db import get_session_factory, init_db
from kalshi_predictor.data.repositories import encode_json, upsert_market
from kalshi_predictor.data.schema import MarketLeg, MarketSnapshot, PaperOrder, SportsMarketLink
from kalshi_predictor.market_legs import CATEGORY_SPORTS
from kalshi_predictor.phase3ax import (
    _command_references_from_reports,
    _economic_news_gap_status,
    _select_next_codex_task,
    build_phase3ax_gap_analysis,
    run_resumable_sports_derivation,
    write_phase3ax_gap_analysis_report,
)
from kalshi_predictor.utils.time import utc_now


def test_phase3ax_writes_heartbeat_and_commits_in_chunks(tmp_path) -> None:
    session_factory = _session_factory(tmp_path)
    heartbeat_dir = Path(tmp_path) / "phase3au"
    with session_factory() as session:
        _seed_sports_leg(session, "KXSPORT-AX-1", 0)
        _seed_sports_leg(session, "KXSPORT-AX-2", 0)

        result = run_resumable_sports_derivation(
            session,
            settings=Settings(overnight_require_market_data=False),
            build_features=False,
            heartbeat_dir=heartbeat_dir,
            progress_every=1,
            checkpoint_every=1,
            commit_every=1,
            resume=True,
        )
        session.commit()
        link_count = session.scalar(select(func.count(SportsMarketLink.id)))

    heartbeat = json.loads(Path(result.heartbeat_path).read_text(encoding="utf-8"))
    checkpoint = json.loads(Path(result.checkpoint_path).read_text(encoding="utf-8"))
    assert result.stopped_early is False
    assert result.summary.sports_markets_seen == 2
    assert result.summary.links_created == 2
    assert result.commits_created >= 2
    assert link_count == 2
    assert heartbeat["stage"] == "COMPLETE"
    assert checkpoint["stage"] == "COMPLETE"


def test_phase3ax_cli_writes_heartbeat(tmp_path) -> None:
    get_settings.cache_clear()
    db_url = f"sqlite:///{Path(tmp_path) / 'phase3ax_cli.db'}"
    engine = init_db(db_url)
    session_factory = get_session_factory(engine)
    with session_factory() as session:
        _seed_sports_leg(session, "KXSPORT-AX-CLI", 0)
        session.commit()

    heartbeat_dir = Path(tmp_path) / "heartbeats"
    result = CliRunner().invoke(
        app,
        [
            "phase3ax-sports-derivation",
            "--limit",
            "1",
            "--no-build-features",
            "--progress-every",
            "1",
            "--checkpoint-every",
            "1",
            "--commit-every",
            "1",
            "--heartbeat-dir",
            str(heartbeat_dir),
        ],
        env={"KALSHI_DB_URL": db_url},
    )

    get_settings.cache_clear()
    assert result.exit_code == 0
    assert "Phase 3AX heartbeat: enabled" in result.output
    assert (heartbeat_dir / "link_remediate_heartbeat.json").exists()
    assert (heartbeat_dir / "link_remediate_checkpoint.json").exists()


def test_phase3ax_cli_help_exposes_safety_flags() -> None:
    result = CliRunner().invoke(app, ["derive-sports-schedule", "--help"])
    assert result.exit_code == 0
    assert "--stop-after-minutes" in result.output
    assert "--checkpoint-every" in result.output
    assert "--resume" in result.output


def test_phase3ax_gap_analysis_detects_missing_commands_and_stale_reports(tmp_path) -> None:
    reports_dir = Path(tmp_path) / "reports"
    _write_r5_ev_not_positive_status(reports_dir)
    _write_stale_phase3ar_catalog_report(reports_dir)
    _write_stale_phase3at_report(reports_dir)
    _write_missing_command_markdown(reports_dir)
    session_factory = _session_factory(tmp_path)

    with session_factory() as session:
        before_orders = session.scalar(select(func.count(PaperOrder.id)))
        artifacts = write_phase3ax_gap_analysis_report(
            session,
            output_dir=reports_dir / "phase3ax",
            reports_dir=reports_dir,
            settings=Settings(opportunity_min_time_to_close_minutes=1),
            registered_commands={"phase3ax-gap-analysis", "phase3bc-r5-status"},
            db_writer_status={"safe_to_start_write": True, "current_writer_pid": None},
        )
        after_orders = session.scalar(select(func.count(PaperOrder.id)))

    payload = json.loads(artifacts.app_gap_analysis_json_path.read_text(encoding="utf-8"))
    assert payload["summary"]["true_current_blocker"] == "EV_NOT_POSITIVE"
    assert payload["summary"]["paper_ready_candidates"] == 0
    assert payload["command_registry_audit"]["missing_command_names"]
    assert "phase3at-handoff-report" in payload["command_registry_audit"]["missing_command_names"]
    assert any(
        row["classification"] == "CONFLICTS_WITH_R5"
        and "phase3ar" in row["path"]
        for row in payload["report_freshness_audit"]["rows"]
    )
    assert any(
        row["classification"] == "STALE_ARTIFACT"
        and "phase3at" in row["path"]
        for row in payload["report_freshness_audit"]["rows"]
    )
    assert "phase3at-handoff-report" not in artifacts.next_operator_commands_path.read_text(
        encoding="utf-8"
    )
    assert before_orders == after_orders == 0


def test_phase3ax_crypto_truth_excludes_expired_historical_rows(tmp_path) -> None:
    reports_dir = Path(tmp_path) / "reports"
    _write_r5_ev_not_positive_status(reports_dir, active_rows=0)
    session_factory = _session_factory(tmp_path)
    with session_factory() as session:
        _seed_expired_crypto_market(session)
        session.commit()
        payload = build_phase3ax_gap_analysis(
            session,
            reports_dir=reports_dir,
            settings=Settings(opportunity_min_time_to_close_minutes=1),
            registered_commands={"phase3ax-gap-analysis", "phase3bc-r5-status"},
            db_writer_status={"safe_to_start_write": True, "current_writer_pid": None},
        )

    crypto = payload["crypto_pipeline_truth"]
    assert crypto["db_current_active_pure_crypto_markets"] == 0
    assert crypto["expired_or_historical_rows_excluded"] is True


def test_phase3ax_stale_markdown_book_missing_does_not_override_current_truth(tmp_path) -> None:
    reports_dir = Path(tmp_path) / "reports"
    _write_r5_ev_not_positive_status(reports_dir)
    (reports_dir / "phase3at").mkdir(parents=True, exist_ok=True)
    (reports_dir / "phase3at" / "NEXT_ACTIONS.md").write_text(
        "Old diagnostic says BOOK_MISSING and BLOCKED_FORECAST_NOT_RANKED.",
        encoding="utf-8",
    )
    session_factory = _session_factory(tmp_path)
    with session_factory() as session:
        payload = build_phase3ax_gap_analysis(
            session,
            reports_dir=reports_dir,
            settings=Settings(opportunity_min_time_to_close_minutes=1),
            registered_commands={"phase3ax-gap-analysis", "phase3bc-r5-status"},
            db_writer_status={"safe_to_start_write": True, "current_writer_pid": None},
        )

    assert payload["crypto_pipeline_truth"]["true_current_blocker"] == "EV_NOT_POSITIVE"
    assert payload["ui_dashboard_truth_status"]["dashboard_gap"] == "DASHBOARD_TRUTH_ALIGNED"


def test_phase3ax_next_task_selection_prioritizes_dashboard_conflict() -> None:
    task = _select_next_codex_task(
        crypto_truth={"true_current_blocker": "EV_NOT_POSITIVE"},
        command_audit={"missing_commands": []},
        dashboard_status={"status": "MISLEADING"},
        source_status={"activation_readiness": "NOT_READY"},
        sports_status={"implementation_needed": True},
        economic_news_status={"status": "WAITING"},
    )

    assert "Dashboard Truth" in task["task_phase_name"]


def test_phase3ax_economic_news_gap_reads_exact_parser_backfill_blocker(
    tmp_path,
) -> None:
    reports_dir = Path(tmp_path) / "reports"
    phase3an_dir = reports_dir / "phase3an"
    phase3an_dir.mkdir(parents=True, exist_ok=True)
    (phase3an_dir / "economic_news_watch.json").write_text(
        json.dumps(
            {
                "summary": {
                    "economic_compatible_parsed_markets": 0,
                    "news_compatible_parsed_markets": 0,
                    "economic_current_parsed_markets": 0,
                    "economic_exact_linked_current_without_parsed_leg": 274,
                    "exact_linked_current_without_parsed_leg": 274,
                    "context_ready_count": 259,
                    "source_freshness": "CONTEXT_READY_FROM_CACHED_READINESS",
                    "first_hard_blocker": "CURRENT_EXACT_LINKS_NEED_PARSER_BACKFILL",
                    "compatibility_status": "PARSER_BACKFILL_REQUIRED",
                    "next_registered_command": (
                        "kalshi-bot phase3an-economic-news-parser-backfill-plan "
                        "--output-dir reports/phase3an --limit 500"
                    ),
                },
                "exact_next_action": (
                    "Run the registered report-only parser backfill plan."
                ),
            }
        ),
        encoding="utf-8",
    )

    status = _economic_news_gap_status(reports_dir)

    assert status["status"] == "PARSER_BACKFILL_REQUIRED"
    assert status["first_hard_blocker"] == (
        "CURRENT_EXACT_LINKS_NEED_PARSER_BACKFILL"
    )
    assert status["context_ready_count"] == 259
    assert status["exact_linked_current_without_parsed_leg"] == 274
    assert "phase3an-economic-news-parser-backfill-plan" in (
        status["next_registered_command"]
    )


def test_phase3ax_selects_guarded_refresh_before_source_followup() -> None:
    task = _select_next_codex_task(
        crypto_truth={"true_current_blocker": "EV_NOT_POSITIVE"},
        command_audit={"missing_commands": []},
        dashboard_status={"status": "ALIGNED"},
        source_status={
            "activation_readiness": "NOT_READY",
            "source_gap_reported_with_exact_evidence": True,
            "next_codex_task_phase_name": "Phase 3BB-R4 FlightAware Review-to-Link Gate",
            "next_codex_task_reason": "FlightAware evidence is review-ready.",
            "next_codex_task_problem": "Build reviewed link-safe and forecast-safe gates.",
        },
        sports_status={"implementation_needed": True},
        economic_news_status={"status": "WAITING"},
    )

    assert task["task_phase_name"] == "Phase 3AX-R9 Guarded Refresh Job Setup"
    assert "manual WSL command babysitting" in task["reason"]


def test_phase3ax_advances_to_economic_news_after_r9_and_r8_complete() -> None:
    task = _select_next_codex_task(
        crypto_truth={"true_current_blocker": "EV_NOT_POSITIVE"},
        command_audit={"missing_commands": []},
        dashboard_status={"status": "ALIGNED"},
        source_status={"activation_readiness": "NOT_READY"},
        sports_status={"implementation_needed": True},
        economic_news_status={"status": "WAITING"},
        guarded_refresh_status={
            "complete": True,
            "status": "ALREADY_RUNNING_NO_DUPLICATE_STARTED",
            "r5_running": True,
        },
    )

    assert task["task_phase_name"] == "Phase 3AX-R7 Economic/News Parser Compatibility"
    assert "R8 dashboard truth is aligned" in task["reason"]
    assert "phase3an-economic-news-watch" in task["full_codex_prompt"]


def test_phase3ax_advances_to_source_evidence_after_r7_exact_blocker() -> None:
    task = _select_next_codex_task(
        crypto_truth={"true_current_blocker": "EV_NOT_POSITIVE"},
        command_audit={"missing_commands": []},
        dashboard_status={"status": "ALIGNED"},
        source_status={"activation_readiness": "NOT_READY"},
        sports_status={"implementation_needed": True},
        economic_news_status={
            "status": "PARSER_BACKFILL_REQUIRED",
            "first_hard_blocker": "CURRENT_EXACT_LINKS_NEED_PARSER_BACKFILL",
            "context_ready_count": 259,
            "source_freshness": "CONTEXT_READY_FROM_CACHED_READINESS",
        },
        guarded_refresh_status={
            "complete": True,
            "status": "ALREADY_RUNNING_NO_DUPLICATE_STARTED",
            "r5_running": True,
        },
    )

    assert task["task_phase_name"] == "Phase 3AX-R5 General Source Evidence Activation"
    assert "R7 now reports an exact economic/news compatibility blocker" in task["reason"]


def test_phase3ax_advances_to_sports_after_r5_source_classification() -> None:
    task = _select_next_codex_task(
        crypto_truth={"true_current_blocker": "EV_NOT_POSITIVE"},
        command_audit={"missing_commands": []},
        dashboard_status={"status": "ALIGNED"},
        source_status={
            "source_gap_reported_with_exact_evidence": True,
            "phase3ax_r5_source_activation_complete": True,
            "activation_readiness": "NOT_READY",
            "first_hard_blocker": "OFFICIAL_FLIGHTAWARE_HISTORICAL_AGGREGATE_UNAVAILABLE",
            "link_safe_rows": 0,
            "forecast_safe_rows": 0,
            "review_gated_rows": 9,
            "blocked_rows": 16,
            "date_stable_missing_rows": 9,
        },
        sports_status={"implementation_needed": True},
        economic_news_status={
            "status": "PARSER_BACKFILL_REQUIRED",
            "first_hard_blocker": "CURRENT_EXACT_LINKS_NEED_PARSER_BACKFILL",
            "context_ready_count": 259,
            "source_freshness": "CONTEXT_READY_FROM_CACHED_READINESS",
        },
        guarded_refresh_status={
            "complete": True,
            "status": "ALREADY_RUNNING_NO_DUPLICATE_STARTED",
            "r5_running": True,
        },
    )

    assert task["task_phase_name"] == "Phase 3AX-R6 Sports Provenance Repair"
    assert "phase3an-sports-blocker-report" in task["full_codex_prompt"]


def test_phase3ax_stops_after_completed_sports_r6_no_safe_rows() -> None:
    task = _select_next_codex_task(
        crypto_truth={"true_current_blocker": "EV_NOT_POSITIVE"},
        command_audit={"missing_commands": []},
        dashboard_status={"status": "ALIGNED"},
        source_status={
            "source_gap_reported_with_exact_evidence": True,
            "phase3ax_r5_source_activation_complete": True,
            "activation_readiness": "NOT_READY",
            "first_hard_blocker": "OFFICIAL_FLIGHTAWARE_HISTORICAL_AGGREGATE_UNAVAILABLE",
            "link_safe_rows": 0,
            "forecast_safe_rows": 0,
            "review_gated_rows": 9,
            "blocked_rows": 16,
            "date_stable_missing_rows": 9,
        },
        sports_status={
            "phase3ax_r6_completed": True,
            "phase3ax_r6_gate": "HOLD_DIAGNOSTIC_ONLY",
            "safe_exact_repair_rows": 0,
            "diagnostic_only_rows": 1000,
        },
        economic_news_status={
            "status": "PARSER_BACKFILL_REQUIRED",
            "first_hard_blocker": "CURRENT_EXACT_LINKS_NEED_PARSER_BACKFILL",
            "context_ready_count": 259,
            "source_freshness": "CONTEXT_READY_FROM_CACHED_READINESS",
        },
        guarded_refresh_status={
            "complete": True,
            "status": "ALREADY_RUNNING_NO_DUPLICATE_STARTED",
            "r5_running": True,
        },
    )

    assert task["task_phase_name"] == "Phase 3AX-R10 Evidence Change Stop Gate"
    assert "completed repair phases" in task["full_codex_prompt"]


def test_phase3ax_routes_running_overdue_watcher_to_dashboard_truth() -> None:
    task = _select_next_codex_task(
        crypto_truth={"true_current_blocker": "WATCHER_NOT_RUNNING_OR_STALE"},
        command_audit={"missing_commands": []},
        dashboard_status={"status": "ALIGNED"},
        source_status={"activation_readiness": "NOT_READY"},
        sports_status={"implementation_needed": True},
        economic_news_status={"status": "WAITING"},
        guarded_refresh_status={
            "complete": True,
            "status": "ALREADY_RUNNING_NO_DUPLICATE_STARTED",
            "r5_running": True,
            "r5_stale_report": True,
        },
    )

    assert task["task_phase_name"] == "Phase 3AX-R8 Dashboard Truth / Operator Workflow"
    assert "guarded R5 job is running" in task["reason"]


def test_phase3ax_command_reference_scan_ignores_generic_commands_phrase(tmp_path) -> None:
    reports_dir = Path(tmp_path) / "reports"
    path = reports_dir / "phase3bb_r5_flightaware" / "NEXT_CODEX_TASK.md"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        "Audit kalshi-bot commands, then run `kalshi-bot phase3ax-gap-analysis`.",
        encoding="utf-8",
    )

    references = _command_references_from_reports(reports_dir)

    assert "commands" not in references
    assert "phase3ax-gap-analysis" in references


def test_phase3ax_cli_help_registered() -> None:
    result = CliRunner().invoke(app, ["phase3ax-gap-analysis", "--help"])
    assert result.exit_code == 0
    assert "Usage" in result.output


def _session_factory(tmp_path):
    engine = init_db(f"sqlite:///{Path(tmp_path) / 'phase3ax.db'}")
    return get_session_factory(engine)


def _seed_sports_leg(session, ticker: str, leg_index: int) -> None:
    now = utc_now()
    upsert_market(
        session,
        {
            "ticker": ticker,
            "title": "yes Chicago wins by over 1.5 runs",
            "series_ticker": "KXMLB",
            "event_ticker": f"{ticker}-EVENT",
            "market_type": "binary",
            "status": "open",
            "close_time": now.isoformat(),
        },
    )
    session.add(
        MarketLeg(
            ticker=ticker,
            leg_index=leg_index,
            parsed_at=now,
            side="YES",
            category=CATEGORY_SPORTS,
            market_type="SPREAD",
            entity_name="Chicago wins by runs",
            operator="ABOVE",
            threshold_value="1.5",
            unit="runs",
            confidence="0.95",
            raw_text="yes Chicago wins by over 1.5 runs",
            reason="test sports leg",
            raw_json=encode_json({"source": "test"}),
        )
    )
    session.flush()


def _write_r5_ev_not_positive_status(reports_dir: Path, *, active_rows: int = 27) -> None:
    r5_dir = reports_dir / "phase3bc_r5"
    r5_dir.mkdir(parents=True, exist_ok=True)
    now = utc_now().isoformat()
    summary = {
        "watch_state": "WAITING_FOR_POSITIVE_EV",
        "active_pure_crypto_rows": active_rows,
        "current_active_window_rows": active_rows,
        "snapshot_stale_rows": 0,
        "snapshot_missing_rows": 0,
        "forecast_stale_rows": 0,
        "forecast_missing_rows": 0,
        "ranking_missing_rows": 0,
        "ranking_stale_rows": 0,
        "ranking_before_forecast_rows": 0,
        "true_ranking_gap_after_repair": 0,
        "ranking_coverage_gap_after_repair": 0,
        "primary_gap_after_refresh": "EV_NOT_POSITIVE",
        "phase3bc_main_blocker": "WATCH_NO_POSITIVE_EXPECTED_VALUE",
        "positive_ev_rows": 0,
        "clean_execution_rows": 14,
        "paper_ready_candidates": 0,
        "best_ev_candidate_ticker": "KXBTC-TEST",
        "best_current_expected_value_cents": "-1.0",
        "best_ev_gap_to_positive_cents": "1.0",
    }
    status = {
        "generated_at": now,
        "latest_report_generated_at": now,
        "guard": {"status": "RUNNING", "running": True, "stale_report": False},
        "latest_summary": summary,
    }
    watch = {"generated_at": now, "summary": summary}
    (r5_dir / "phase3bc_r5_status.json").write_text(json.dumps(status), encoding="utf-8")
    (r5_dir / "phase3bc_r5_crypto_freshness_watch.json").write_text(
        json.dumps(watch),
        encoding="utf-8",
    )


def _write_stale_phase3ar_catalog_report(reports_dir: Path) -> None:
    phase3ar = reports_dir / "phase3ar"
    phase3ar.mkdir(parents=True, exist_ok=True)
    old = (utc_now() - timedelta(hours=6)).isoformat()
    payload = {
        "generated_at": old,
        "summary": {
            "positive_ev_rows": 17,
            "paper_ready_rows": 0,
            "first_hard_blocker": "STALE_CATALOG",
        },
    }
    for name in (
        "paper_ready_gate_after_url_repair.json",
        "url_audit.json",
        "catalog_refresh_plan.json",
    ):
        (phase3ar / name).write_text(json.dumps(payload), encoding="utf-8")


def _write_stale_phase3at_report(reports_dir: Path) -> None:
    phase3at = reports_dir / "phase3at"
    phase3at.mkdir(parents=True, exist_ok=True)
    old = (utc_now() - timedelta(hours=4)).isoformat()
    payload = {"generated_at": old, "summary": {"main_gap_before": "SNAPSHOT_STALE"}}
    for name in (
        "phase3at_active_router.json",
        "forecast_ranking_diagnostic.json",
        "opportunity_funnel.json",
    ):
        (phase3at / name).write_text(json.dumps(payload), encoding="utf-8")


def _write_missing_command_markdown(reports_dir: Path) -> None:
    phase3at = reports_dir / "phase3at"
    phase3at.mkdir(parents=True, exist_ok=True)
    (phase3at / "NEXT_ACTIONS.md").write_text(
        "Run `kalshi-bot phase3at-handoff-report --output-dir reports/phase3at`.",
        encoding="utf-8",
    )


def _seed_expired_crypto_market(session) -> None:
    now = utc_now()
    upsert_market(
        session,
        {
            "ticker": "KXBTC-EXPIRED",
            "title": "Expired BTC window",
            "series_ticker": "KXBTC",
            "event_ticker": "KXBTC-EXPIRED-EVENT",
            "market_type": "binary",
            "status": "active",
            "close_time": (now - timedelta(hours=1)).isoformat(),
            "expected_expiration_time": (now - timedelta(minutes=55)).isoformat(),
        },
    )
    session.add(
        MarketSnapshot(
            ticker="KXBTC-EXPIRED",
            captured_at=now,
            status="active",
            raw_market_json="{}",
        )
    )
    session.flush()
