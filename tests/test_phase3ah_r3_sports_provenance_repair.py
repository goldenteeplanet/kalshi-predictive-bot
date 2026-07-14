from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path

from typer.testing import CliRunner

from kalshi_predictor.cli import app
from kalshi_predictor.config import Settings, get_settings
from kalshi_predictor.data.db import get_session_factory, init_db
from kalshi_predictor.data.repositories import upsert_market
from kalshi_predictor.data.schema import MarketLeg, SportsMarketLink
from kalshi_predictor.phase3ah_r3 import (
    build_phase3ah_r3_sports_provenance_repair,
    write_phase3ah_r3_sports_provenance_repair_report,
)
from kalshi_predictor.phase3ax import build_phase3ax_gap_analysis
from kalshi_predictor.utils.time import utc_now


def test_phase3ah_r3_blocks_placeholder_rows_and_routes_to_next_gap(tmp_path) -> None:
    session_factory = _session_factory(tmp_path)
    reports_dir = tmp_path / "reports"
    ticker = "KXMVECROSSCATEGORY-S203001010000-ABC123"
    _write_upstream_sports_reports(reports_dir, placeholder_tickers=[ticker])

    with session_factory() as session:
        _seed_sports_market(session, ticker=ticker)
        _add_leg(session, ticker=ticker, index=0, text="yes rd16-w1")
        _add_partial_link(session, ticker=ticker, league="SOCCER")
        payload = build_phase3ah_r3_sports_provenance_repair(
            session,
            reports_dir=reports_dir,
            registered_commands={
                "phase3ah-r3-sports-provenance-repair",
                "phase3ah-r3-bounded-scan-expansion",
                "phase3z-r2-sports-provenance-repair",
                "phase3ax-gap-analysis",
                "phase-orchestrator",
            },
        )

    assert payload["summary"]["rows_safe_to_repair"] == 0
    assert payload["summary"]["auto_upgrades_created"] == 0
    assert payload["summary"]["verified_link_auto_upgrades"] is False
    assert payload["summary"]["paper_trade_creation"] is False
    assert payload["summary"]["first_hard_blocker"] == "HOLD_PLACEHOLDER_UPGRADES"
    assert payload["next_codex_task"]["task_phase_name"] == (
        "Phase 3AN Economic/News Compatibility Watch"
    )
    assert payload["command_registry_audit"]["next_actions_reference_only_registered_commands"]


def test_phase3ah_r3_writer_and_phase3ax_handoff_do_not_repeat_completed_r3(tmp_path) -> None:
    session_factory = _session_factory(tmp_path)
    reports_dir = tmp_path / "reports"
    ticker = "KXMVECROSSCATEGORY-S203001010000-DEF456"
    _write_upstream_sports_reports(reports_dir, placeholder_tickers=[ticker])
    _write_r5_ev_not_positive_status(reports_dir)
    _write_source_next_task_is_sports_r3(reports_dir)

    with session_factory() as session:
        _seed_sports_market(session, ticker=ticker)
        _add_leg(session, ticker=ticker, index=0, text="yes rd16-w1")
        _add_partial_link(session, ticker=ticker, league="SOCCER")
        artifacts = write_phase3ah_r3_sports_provenance_repair_report(
            session,
            output_dir=reports_dir / "phase3ah_r3",
            reports_dir=reports_dir,
            registered_commands={
                "phase3ah-r3-sports-provenance-repair",
                "phase3ah-r3-bounded-scan-expansion",
                "phase3z-r2-sports-provenance-repair",
                "phase3ax-gap-analysis",
                "phase-orchestrator",
            },
        )
        gap = build_phase3ax_gap_analysis(
            session,
            reports_dir=reports_dir,
            settings=Settings(opportunity_min_time_to_close_minutes=1),
            registered_commands={
                "phase3ah-r3-sports-provenance-repair",
                "phase3ah-r3-bounded-scan-expansion",
                "phase3z-r2-sports-provenance-repair",
                "phase3ax-gap-analysis",
                "phase3bc-r5-status",
                "phase-orchestrator",
                "phase3ar-link-repair-report",
                "phase3ar-refresh-catalog-for-opportunities",
                "phase3ar-url-audit",
                "phase3at-handoff-report",
            },
            db_writer_status={"safe_to_start_write": True, "current_writer_pid": None},
        )

    assert artifacts.json_path.exists()
    assert gap["sports_gap_status"]["phase3ah_r3_completed"] is True
    assert gap["next_codex_task"]["task_phase_name"] == (
        "Phase 3AN Economic/News Compatibility Watch"
    )


def test_phase3ah_r3_cli_help_registered() -> None:
    result = CliRunner().invoke(app, ["phase3ah-r3-sports-provenance-repair", "--help"])
    assert result.exit_code == 0
    assert "--max-rows" in result.output
    assert "--ticker-prefix" in result.output


def test_phase3ah_r3_bounded_scan_expansion_cli_help_registered() -> None:
    result = CliRunner().invoke(app, ["phase3ah-r3-bounded-scan-expansion", "--help"])
    assert result.exit_code == 0
    assert "--max-rows" in result.output
    assert "--ticker-prefix" in result.output


def _session_factory(tmp_path: Path):
    get_settings.cache_clear()
    engine = init_db(f"sqlite:///{Path(tmp_path) / 'phase3ah_r3.db'}")
    return get_session_factory(engine)


def _seed_sports_market(session, *, ticker: str) -> None:
    upsert_market(
        session,
        {
            "ticker": ticker,
            "title": "yes rd16-w1, yes Brazil, yes Vinicius Junior: 1+",
            "event_ticker": ticker.rsplit("-", 1)[0],
            "series_ticker": "KXMVECROSSCATEGORY",
            "status": "open",
            "close_time": "2030-01-01T19:00:00Z",
            "market_type": "binary",
        },
    )


def _add_leg(session, *, ticker: str, index: int, text: str) -> None:
    session.add(
        MarketLeg(
            ticker=ticker,
            leg_index=index,
            parsed_at=utc_now(),
            side="YES",
            category="sports",
            market_type="PLAYER_PROP",
            entity_name="rd16-w1",
            operator="AT_LEAST",
            threshold_value="1",
            unit="COUNT",
            confidence="0.95",
            raw_text=text,
            reason="test sports leg",
            raw_json=json.dumps({"phase": "3ah-r3-test"}),
        )
    )


def _add_partial_link(session, *, ticker: str, league: str) -> None:
    session.add(
        SportsMarketLink(
            created_at=utc_now(),
            ticker=ticker,
            league=league,
            game_key=f"{league}:market-derived:{ticker.lower()}:main",
            market_type="PLAYER_PROP",
            link_confidence="0.50",
            link_reason="Market-derived fallback; no verified schedule evidence.",
            matched_terms_json=json.dumps({"matched_terms": [league.lower()]}),
            raw_json=json.dumps({"source": "market-derived-fallback"}),
        )
    )


def _write_upstream_sports_reports(
    reports_dir: Path,
    *,
    placeholder_tickers: list[str],
) -> None:
    _write_json(
        reports_dir / "market_coverage" / "coverage_rows.json",
        [
            {
                "scope_key": "sports",
                "health": "LINKER_DEGRADED",
                "parsed_markets": 1,
                "partial_markets": 1,
                "partial_link_rows": 1,
                "derived_usable_markets": 0,
                "verified_schedule_markets": 0,
                "verified_schedule_link_rows": 0,
            }
        ],
    )
    _write_json(
        reports_dir / "market_coverage" / "link_coverage.json",
        {"reconciliation": {"sports": {"unresolved_partial_markets": 1}}},
    )
    _write_json(
        reports_dir / "phase3az" / "phase3az_gap_analysis.json",
        {"generated_at": _iso(), "gaps": []},
    )
    _write_json(
        reports_dir / "phase3ah_sports" / "phase3ah_sports_placeholder_watch.json",
        {
            "summary": {
                "phase3ae_blocked_placeholder_rows": len(placeholder_tickers),
                "still_placeholder_rows": len(placeholder_tickers),
                "sports_partial_links_without_upgrade": 1,
            },
            "placeholder_watch_rows": [
                {
                    "source_status": "SOURCE_STILL_PLACEHOLDER",
                    "safe_to_apply": False,
                    "blocks_phase3ae_upgrade": True,
                    "example_tickers": placeholder_tickers,
                }
            ],
        },
    )
    _write_json(reports_dir / "phase_orchestrator.json", {"generated_at": _iso()})


def _write_r5_ev_not_positive_status(reports_dir: Path) -> None:
    r5_dir = reports_dir / "phase3bc_r5"
    r5_dir.mkdir(parents=True, exist_ok=True)
    now = utc_now().isoformat()
    summary = {
        "watch_state": "WAITING_FOR_POSITIVE_EV",
        "active_pure_crypto_rows": 1,
        "current_active_window_rows": 1,
        "snapshot_stale_rows": 0,
        "forecast_stale_rows": 0,
        "ranking_coverage_gap_after_repair": 0,
        "primary_gap_after_refresh": "EV_NOT_POSITIVE",
        "phase3bc_main_blocker": "WATCH_NO_POSITIVE_EXPECTED_VALUE",
        "positive_ev_rows": 0,
        "paper_ready_candidates": 0,
        "best_ev_gap_to_positive_cents": "1.0",
    }
    status = {
        "generated_at": now,
        "latest_report_generated_at": now,
        "guard": {"status": "RUNNING", "running": True, "stale_report": False},
        "latest_summary": summary,
    }
    watch = {"generated_at": now, "summary": summary}
    _write_json(r5_dir / "phase3bc_r5_status.json", status)
    _write_json(r5_dir / "phase3bc_r5_crypto_freshness_watch.json", watch)


def _write_source_next_task_is_sports_r3(reports_dir: Path) -> None:
    _write_json(
        reports_dir / "phase3bb_r5_flightaware" / "flightaware_date_stable_evidence.json",
        {
            "generated_at": utc_now().isoformat(),
            "summary": {
                "date_stable_evidence_status": "GATED",
                "accepted_date_stable_evidence_rows": 0,
                "link_safe_rows": 0,
                "forecast_safe_rows": 0,
                "first_hard_blocker": "SPORTS_PROVENANCE_REQUIRES_R3",
            },
            "next_codex_task": {
                "task_phase_name": "Phase 3AH-R3 Sports Provenance Repair",
                "reason": "Sports provenance is the next bounded source follow-up.",
                "problem_statement": "Complete sports R3 before routing elsewhere.",
            },
        },
    )


def _write_json(path: Path, payload: dict | list) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload), encoding="utf-8")


def _iso() -> str:
    return datetime(2030, 1, 1, tzinfo=UTC).isoformat()
