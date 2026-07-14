import json
from datetime import timedelta
from pathlib import Path

import pytest
from sqlalchemy import select
from typer.testing import CliRunner

from kalshi_predictor import phase3an
from kalshi_predictor.cli import app
from kalshi_predictor.data.db import get_session_factory, init_db
from kalshi_predictor.data.repositories import (
    insert_forecast,
    insert_market_snapshot,
    upsert_market,
)
from kalshi_predictor.data.schema import (
    EconomicEvent,
    EconomicFeature,
    EconomicMarketLink,
    MarketLeg,
    MarketRanking,
)
from kalshi_predictor.ui.service import paper_trade_blocker_status
from kalshi_predictor.utils.time import utc_now


def test_crypto_watcher_overdue_classification(tmp_path, monkeypatch) -> None:
    monkeypatch.chdir(tmp_path)
    _write_crypto_status(tmp_path)
    with _session(tmp_path) as session:
        payload = phase3an.build_phase3an_crypto_watch_doctor(
            session,
            output_dir=Path("reports/phase3an"),
            reports_dir=Path("reports"),
        )
    assert payload["classification"] == "RUNNING_CYCLE_OVERDUE"


def test_crypto_slow_stage_attribution(tmp_path, monkeypatch) -> None:
    monkeypatch.chdir(tmp_path)
    _write_crypto_status(tmp_path)
    with _session(tmp_path) as session:
        payload = phase3an.build_phase3an_crypto_watch_doctor(session)
    assert payload["slowest_stage"] in {"heartbeat_or_cycle_completion", "window_sync"}
    assert payload["stage_evidence"]["current_stage"]


def test_crypto_restart_plan_remains_dry_run(tmp_path, monkeypatch) -> None:
    monkeypatch.chdir(tmp_path)
    _write_crypto_status(tmp_path)
    with _session(tmp_path) as session:
        payload = phase3an.build_phase3an_crypto_watch_restart_plan(session, dry_run=True)
    assert payload["dry_run"] is True
    assert payload["would_stop_process"] is False
    assert payload["would_start_process"] is False


def test_paper_funnel_explains_2000_rankings_zero_tradeable(tmp_path, monkeypatch) -> None:
    monkeypatch.chdir(tmp_path)
    with _session(tmp_path) as session:
        _seed_rankings(session, count=2000)
        payload = phase3an.build_phase3an_paper_funnel_explain(session, window_hours=168)
    assert payload["summary"]["rankings_reviewed"] == 2000
    assert payload["summary"]["tradeable_rows"] == 0
    assert payload["summary"]["paper_orders_created"] == 0


def test_positive_raw_ev_but_no_executable_ev_is_classified(tmp_path, monkeypatch) -> None:
    monkeypatch.chdir(tmp_path)
    with _session(tmp_path) as session:
        _seed_positive_raw_ev_lost_to_spread(session)
        payload = phase3an.build_phase3an_paper_funnel_explain(session, window_hours=168)
    assert payload["reason_counts"]["EV_LOST_TO_SPREAD"] == 1
    assert payload["summary"]["tradeable_rows"] == 0


def test_settlement_healthy_when_zero_due_overdue(tmp_path, monkeypatch) -> None:
    monkeypatch.chdir(tmp_path)
    with _session(tmp_path) as session:
        payload = phase3an.build_phase3an_settlement_health_confirm(session)
    assert payload["summary"]["due_paper_trades"] == 0
    assert payload["summary"]["overdue_paper_trades"] == 0
    assert payload["summary"]["status"] == "HEALTHY"


def test_no_settlement_apply_suggested_when_zero_exact_eligible(tmp_path, monkeypatch) -> None:
    monkeypatch.chdir(tmp_path)
    with _session(tmp_path) as session:
        payload = phase3an.build_phase3an_settlement_health_confirm(session)
    assert payload["summary"]["exact_eligible_trades"] == 0
    assert payload["summary"]["apply_command_exposed"] is False
    assert payload["exact_apply_policy"]["operator_apply_command"] is None


def test_usda_date_mismatch_is_preserved(tmp_path, monkeypatch) -> None:
    monkeypatch.chdir(tmp_path)
    _write_usda_record(tmp_path, as_of_date="June 26, 2026")
    with _session(tmp_path) as session:
        payload = phase3an.build_phase3an_usda_date_mismatch_report(session)
    assert payload["current_blocker"] == "USDA_DATE_MISMATCH"
    assert payload["local_report_date_found"] == "June 26, 2026"


def test_june_26_usda_report_is_not_used_for_july_3(tmp_path, monkeypatch) -> None:
    monkeypatch.chdir(tmp_path)
    _write_usda_record(tmp_path, as_of_date="June 26, 2026", value="1.19")
    with _session(tmp_path) as session:
        payload = phase3an.build_phase3an_usda_date_mismatch_report(session)
    assert payload["exact_expected_report_exists_locally"] is False
    assert payload["uses_wrong_date_for_evidence"] is False


def test_cushman_unavailable_remains_not_link_or_forecast_safe(tmp_path, monkeypatch) -> None:
    monkeypatch.chdir(tmp_path)
    with _session(tmp_path) as session:
        payload = phase3an.build_phase3an_general_sources_status(session)
    cushman = payload["sources"]["Cushman"]
    assert cushman["status"] == "CUSHMAN_VALUES_UNAVAILABLE"
    assert cushman["link_safe"] is False
    assert cushman["forecast_safe"] is False


def test_flightaware_ready_for_review_remains_not_link_or_forecast_safe(tmp_path, monkeypatch) -> None:
    monkeypatch.chdir(tmp_path)
    with _session(tmp_path) as session:
        payload = phase3an.build_phase3an_general_sources_status(session)
    flightaware = payload["sources"]["FlightAware"]
    assert flightaware["status"] == "FLIGHTAWARE_READY_FOR_REVIEW"
    assert flightaware["link_safe"] is False
    assert flightaware["forecast_safe"] is False


def test_general_sources_status_separates_review_gated_and_blocked_rows(
    tmp_path,
    monkeypatch,
) -> None:
    monkeypatch.chdir(tmp_path)
    _write_general_source_activation_reports(tmp_path)
    with _session(tmp_path) as session:
        payload = phase3an.build_phase3an_general_sources_status(session)

    summary = payload["summary"]
    assert summary["source_evidence_status"] == "SOURCE_EVIDENCE_CLASSIFIED_GATED"
    assert summary["source_evidence_ready_rows"] == 9
    assert summary["link_safe_rows"] == 0
    assert summary["forecast_safe_rows"] == 0
    assert summary["review_gated_rows"] == 9
    assert summary["blocked_rows"] == 16
    assert summary["wrong_date_rows"] == 7
    assert summary["proprietary_blocked_rows"] == 9
    assert summary["date_stable_missing_rows"] == 9
    assert summary["phase3ax_r5_source_activation_complete"] is True
    assert payload["sources"]["FlightAware"]["status"] == (
        "OFFICIAL_FLIGHTAWARE_HISTORICAL_AGGREGATE_UNAVAILABLE"
    )
    assert payload["sources"]["USDA"]["status"] == "SOURCE_DATE_MISMATCH_BLOCKER"
    assert payload["sources"]["Cushman"]["status"] == "PROPRIETARY_REVIEW_REQUIRED"
    assert "diagnostic-only" in payload["exact_next_action"]


def test_grouped_source_review_status_is_reported(tmp_path, monkeypatch) -> None:
    monkeypatch.chdir(tmp_path)
    with _session(tmp_path) as session:
        payload = phase3an.build_phase3an_3bb_r2_burndown(session)
    assert "grouped_source_review_status" in payload
    assert payload["grouped_source_review_status"]["helper_missing"] is False


def test_sports_placeholders_remain_blocked(tmp_path, monkeypatch) -> None:
    monkeypatch.chdir(tmp_path)
    _write_sports_reports(tmp_path, placeholders=3, partial=0)
    payload = phase3an.build_phase3an_sports_blocker_report()
    assert "ROUND_PLACEHOLDER" in payload["reason_codes"]
    assert payload["summary"]["placeholder_rows"] == 3


def test_sports_partial_provenance_is_not_upgraded(tmp_path, monkeypatch) -> None:
    monkeypatch.chdir(tmp_path)
    _write_sports_reports(tmp_path, placeholders=0, partial=4)
    payload = phase3an.build_phase3an_sports_blocker_report()
    assert "PARTIAL_PROVENANCE_ONLY" in payload["reason_codes"]
    assert payload["feature_writes"] is False
    assert payload["forecast_writes"] is False


def test_economic_news_waiting_does_not_force_links(tmp_path, monkeypatch) -> None:
    monkeypatch.chdir(tmp_path)
    with _session(tmp_path) as session:
        payload = phase3an.build_phase3an_economic_news_watch(session)
    assert payload["links_created"] == 0
    assert payload["forecasts_created"] == 0
    assert payload["summary"]["blocker_reason"]


def test_economic_news_watch_uses_cached_readiness_by_default(
    tmp_path,
    monkeypatch,
) -> None:
    monkeypatch.chdir(tmp_path)
    _write_json(
        Path("reports/phase3bb/phase3bb_domain_readiness.json"),
        {
            "domain_rows": [
                {
                    "domain": "economic",
                    "status": "CACHED_ECONOMIC_READY",
                    "counts": {"active_parsed_markets": 7},
                },
                {
                    "domain": "news",
                    "status": "CACHED_NEWS_READY",
                    "counts": {"active_parsed_markets": 3},
                },
            ]
        },
    )

    def fail_live_readiness(_session):
        raise AssertionError("live Phase 3BB readiness should not rebuild by default")

    def fail_live_preflight(*_args, **_kwargs):
        raise AssertionError("live Phase 3AN preflight should not run by default")

    import kalshi_predictor.phase3bb as phase3bb

    monkeypatch.setattr(phase3bb, "build_phase3bb_domain_readiness", fail_live_readiness)
    monkeypatch.setattr(phase3an, "build_phase3an_preflight", fail_live_preflight)
    with _session(tmp_path) as session:
        payload = phase3an.build_phase3an_economic_news_watch(
            session,
            output_dir=Path("reports/phase3an"),
        )
    assert payload["readiness_source"] == "cached_phase3bb_domain_readiness"
    assert payload["summary"]["readiness_source"] == "cached_phase3bb_domain_readiness"
    assert payload["preflight_source"] == "skipped_bounded_report_only"
    assert payload["summary"]["preflight_source"] == "skipped_bounded_report_only"
    assert payload["summary"]["economic_status"] == "CACHED_ECONOMIC_READY"
    assert payload["summary"]["news_status"] == "CACHED_NEWS_READY"
    assert payload["summary"]["economic_compatible_parsed_markets"] == 7
    assert payload["summary"]["news_compatible_parsed_markets"] == 3
    assert payload["links_created"] == 0
    assert payload["paper_trades_created"] == 0


def test_economic_news_handoff_counts_exact_current_link(tmp_path, monkeypatch) -> None:
    monkeypatch.chdir(tmp_path)
    with _session(tmp_path) as session:
        _seed_economic_context(session)
        _seed_economic_market(
            session,
            ticker="KXCPI-CURRENT",
            close_delta=timedelta(hours=2),
            exact_link=True,
        )
        payload = phase3an.build_phase3an_economic_news_watch(session)
    assert payload["summary"]["economic_current_parsed_markets"] == 1
    assert payload["summary"]["economic_exact_linked_current_markets"] == 1
    handoff = payload["current_market_handoff"]["domains"]["economic"]
    assert handoff["first_blocker"] == "READY_FOR_FORECASTS"
    assert handoff["rows"][0]["reason_codes"] == ["EXACT_CURRENT_COMPATIBLE"]
    assert payload["links_created"] == 0
    assert payload["paper_trades_created"] == 0


def test_economic_news_handoff_excludes_expired_parsed_market(tmp_path, monkeypatch) -> None:
    monkeypatch.chdir(tmp_path)
    with _session(tmp_path) as session:
        _seed_economic_context(session)
        _seed_economic_market(
            session,
            ticker="KXCPI-EXPIRED",
            close_delta=-timedelta(hours=1),
            exact_link=True,
        )
        payload = phase3an.build_phase3an_economic_news_watch(session)
    handoff = payload["current_market_handoff"]["domains"]["economic"]
    assert payload["summary"]["economic_current_parsed_markets"] == 0
    assert payload["summary"]["economic_exact_linked_current_markets"] == 0
    assert handoff["counts"]["non_current_parsed_markets"] == 1
    assert handoff["first_blocker"] == "ONLY_EXPIRED_OR_CLOSED_PARSED_MARKETS"
    assert "MARKET_CLOSE_TIME_PASSED" in handoff["rows"][0]["reason_codes"]


def test_economic_news_handoff_reports_missing_exact_link(tmp_path, monkeypatch) -> None:
    monkeypatch.chdir(tmp_path)
    with _session(tmp_path) as session:
        _seed_economic_context(session)
        _seed_economic_market(
            session,
            ticker="KXCPI-MISSING-LINK",
            close_delta=timedelta(hours=2),
            exact_link=False,
        )
        payload = phase3an.build_phase3an_economic_news_watch(session)
    handoff = payload["current_market_handoff"]["domains"]["economic"]
    assert payload["summary"]["economic_current_parsed_markets"] == 1
    assert payload["summary"]["economic_exact_linked_current_markets"] == 0
    assert handoff["counts"]["current_parsed_missing_exact_link"] == 1
    assert handoff["first_blocker"] == "EXACT_LINKS_MISSING"
    assert "MISSING_EXACT_LINK" in handoff["rows"][0]["reason_codes"]


def test_economic_news_handoff_reports_current_link_parser_backfill(tmp_path, monkeypatch) -> None:
    monkeypatch.chdir(tmp_path)
    with _session(tmp_path) as session:
        _seed_economic_context(session)
        _seed_economic_market(
            session,
            ticker="KXCPI-LINK-NO-LEG",
            close_delta=timedelta(hours=2),
            exact_link=True,
            parsed_leg=False,
        )
        payload = phase3an.build_phase3an_economic_news_watch(session)
    handoff = payload["current_market_handoff"]["domains"]["economic"]
    assert payload["summary"]["economic_current_parsed_markets"] == 0
    assert payload["summary"]["economic_exact_linked_current_markets"] == 0
    assert payload["summary"]["economic_exact_linked_current_without_parsed_leg"] == 1
    assert handoff["counts"]["exact_linked_current_without_parsed_leg"] == 1
    assert handoff["first_blocker"] == "CURRENT_EXACT_LINKS_NEED_PARSER_BACKFILL"
    assert handoff["link_only_rows"][0]["reason_codes"] == [
        "CURRENT_EXACT_LINK_WITHOUT_PARSED_LEG"
    ]
    assert payload["summary"]["first_hard_blocker"] == (
        "CURRENT_EXACT_LINKS_NEED_PARSER_BACKFILL"
    )
    assert payload["summary"]["compatibility_status"] == "PARSER_BACKFILL_REQUIRED"
    assert payload["summary"]["source_freshness"] == "CONTEXT_READY"
    assert payload["summary"]["next_registered_command"] == (
        "kalshi-bot phase3an-economic-news-parser-backfill-plan "
        "--output-dir reports/phase3an --limit 500"
    )


def test_economic_news_watch_writes_registered_next_actions(
    tmp_path,
    monkeypatch,
) -> None:
    monkeypatch.chdir(tmp_path)
    output_dir = Path("reports/phase3an")
    with _session(tmp_path) as session:
        _seed_economic_context(session)
        _seed_economic_market(
            session,
            ticker="KXCPI-LINK-NO-LEG",
            close_delta=timedelta(hours=2),
            exact_link=True,
            parsed_leg=False,
        )
        artifacts = phase3an.write_phase3an_economic_news_watch_report(
            session,
            output_dir=output_dir,
        )

    payload = json.loads(artifacts.json_path.read_text(encoding="utf-8"))
    next_actions = (output_dir / "NEXT_ACTIONS.md").read_text(encoding="utf-8")

    assert payload["summary"]["first_hard_blocker"] == (
        "CURRENT_EXACT_LINKS_NEED_PARSER_BACKFILL"
    )
    assert "phase3an-economic-news-parser-backfill-plan" in next_actions
    assert "Do not force links" in next_actions
    assert (output_dir / "ECONOMIC_NEWS_WATCH.md").exists()


def test_economic_news_parser_backfill_plan_is_report_only(tmp_path, monkeypatch) -> None:
    monkeypatch.chdir(tmp_path)
    with _session(tmp_path) as session:
        _seed_economic_context(session)
        _seed_economic_market(
            session,
            ticker="KXCPI-LINK-NO-LEG",
            close_delta=timedelta(hours=2),
            exact_link=True,
            parsed_leg=False,
        )
        payload = phase3an.build_phase3an_economic_news_parser_backfill_plan(
            session,
            limit=50,
        )
    assert payload["summary"]["economic_exact_linked_current_without_parsed_leg"] == 1
    assert payload["summary"]["first_blocker"] == "PARSER_BACKFILL_READY_DRY_RUN_ONLY"
    assert payload["summary"]["safe_to_backfill_now"] == 1
    assert payload["parser_rows_written"] == 0
    assert payload["links_created"] == 0
    assert payload["forecasts_created"] == 0
    assert payload["paper_trades_created"] == 0
    assert payload["rows"][0]["safe_to_backfill_parser_leg"] is True
    assert payload["rows"][0]["unsafe_reason"] is None
    assert payload["rows"][0]["candidate_parser_leg"]["entity_name"] == "cpi"


def test_economic_news_parser_backfill_blocks_link_parser_event_mismatch(
    tmp_path,
    monkeypatch,
) -> None:
    monkeypatch.chdir(tmp_path)
    with _session(tmp_path) as session:
        _seed_economic_context(session)
        _seed_economic_market(
            session,
            ticker="KXU3-LINK-NO-LEG",
            close_delta=timedelta(hours=2),
            exact_link=True,
            parsed_leg=False,
            title="Will the unemployment rate be above 4.3% in August?",
            event_key="fed",
        )
        payload = phase3an.build_phase3an_economic_news_parser_backfill_plan(
            session,
            limit=50,
        )
    row = payload["rows"][0]
    assert payload["summary"]["first_blocker"] == "LINK_PARSER_EVENT_MISMATCH"
    assert payload["summary"]["safe_to_backfill_now"] == 0
    assert row["safe_to_backfill_parser_leg"] is False
    assert row["unsafe_reason"] == "LINK_PARSER_EVENT_MISMATCH"
    assert row["candidate_parser_leg"]["entity_name"] == "jobs"
    assert "LINK_EXPECTED:fomc" in row["parser_reason_codes"]


def test_economic_link_event_repair_plan_separates_safe_and_mismatched_rows(
    tmp_path,
    monkeypatch,
) -> None:
    monkeypatch.chdir(tmp_path)
    with _session(tmp_path) as session:
        _seed_economic_context(session)
        _seed_economic_market(
            session,
            ticker="KXCPI-LINK-NO-LEG",
            close_delta=timedelta(hours=2),
            exact_link=True,
            parsed_leg=False,
        )
        _seed_economic_market(
            session,
            ticker="KXU3-LINK-NO-LEG",
            close_delta=timedelta(hours=3),
            exact_link=True,
            parsed_leg=False,
            title="Will the unemployment rate be above 4.3% in August?",
            event_key="fed",
        )
        payload = phase3an.build_phase3an_economic_link_event_repair_plan(
            session,
            limit=50,
        )
    rows = {row["ticker"]: row for row in payload["rows"]}
    assert payload["summary"]["rows_reviewed"] == 2
    assert payload["summary"]["safe_parser_backfill_rows"] == 1
    assert payload["summary"]["event_mismatch_rows"] == 1
    assert payload["summary"]["link_event_repair_candidates"] == 1
    assert payload["summary"]["first_blocker"] == "LINK_EVENT_REPAIR_REQUIRES_OPERATOR_APPROVAL"
    assert payload["link_rows_written"] == 0
    assert payload["parser_rows_written"] == 0
    assert payload["forecasts_created"] == 0
    assert payload["paper_trades_created"] == 0
    assert rows["KXCPI-LINK-NO-LEG"]["safe_to_backfill_parser_leg"] is True
    assert rows["KXCPI-LINK-NO-LEG"]["safe_to_repair_link_event"] is False
    assert rows["KXU3-LINK-NO-LEG"]["safe_to_repair_link_event"] is True
    assert rows["KXU3-LINK-NO-LEG"]["suggested_event_key"] == "jobs"
    assert rows["KXU3-LINK-NO-LEG"]["current_event_key"] == "fed"


def test_economic_link_event_repair_dry_run_writes_no_link_rows(
    tmp_path,
    monkeypatch,
) -> None:
    monkeypatch.chdir(tmp_path)
    with _session(tmp_path) as session:
        _seed_economic_context(session)
        _seed_economic_market(
            session,
            ticker="KXU3-LINK-NO-LEG",
            close_delta=timedelta(hours=3),
            exact_link=True,
            parsed_leg=False,
            title="Will the unemployment rate be above 4.3% in August?",
            event_key="fed",
        )
        before_links = list(
            session.scalars(
                select(EconomicMarketLink).where(EconomicMarketLink.ticker == "KXU3-LINK-NO-LEG")
            )
        )
        payload = phase3an.build_phase3an_economic_link_event_repair_apply(
            session,
            dry_run=True,
            apply=False,
            limit=50,
            max_records=5,
        )
        after_links = list(
            session.scalars(
                select(EconomicMarketLink).where(EconomicMarketLink.ticker == "KXU3-LINK-NO-LEG")
            )
        )
    assert payload["status"] == "DRY_RUN"
    assert payload["summary"]["repair_candidates_reviewed"] == 1
    assert payload["summary"]["would_write_link_rows"] == 1
    assert payload["link_rows_written"] == 0
    assert payload["parser_rows_written"] == 0
    assert payload["paper_trades_created"] == 0
    assert len(before_links) == len(after_links) == 1


def test_economic_link_event_repair_apply_requires_backup_first(
    tmp_path,
    monkeypatch,
) -> None:
    monkeypatch.chdir(tmp_path)
    with _session(tmp_path) as session:
        _seed_economic_context(session)
        _seed_economic_market(
            session,
            ticker="KXU3-LINK-NO-LEG",
            close_delta=timedelta(hours=3),
            exact_link=True,
            parsed_leg=False,
            title="Will the unemployment rate be above 4.3% in August?",
            event_key="fed",
        )
        with pytest.raises(ValueError, match="--apply requires --backup-first"):
            phase3an.build_phase3an_economic_link_event_repair_apply(
                session,
                dry_run=False,
                apply=True,
                backup_first=False,
                limit=50,
                max_records=5,
            )


def test_economic_parser_leg_backfill_dry_run_writes_no_market_legs(
    tmp_path,
    monkeypatch,
) -> None:
    monkeypatch.chdir(tmp_path)
    with _session(tmp_path) as session:
        _seed_economic_context(session)
        _seed_economic_market(
            session,
            ticker="KXCPI-LINK-NO-LEG",
            close_delta=timedelta(hours=2),
            exact_link=True,
            parsed_leg=False,
        )
        before_legs = list(
            session.scalars(select(MarketLeg).where(MarketLeg.ticker == "KXCPI-LINK-NO-LEG"))
        )
        payload = phase3an.build_phase3an_economic_parser_leg_backfill(
            session,
            dry_run=True,
            apply=False,
            limit=50,
            max_records=5,
        )
        after_legs = list(
            session.scalars(select(MarketLeg).where(MarketLeg.ticker == "KXCPI-LINK-NO-LEG"))
        )
    assert payload["status"] == "DRY_RUN"
    assert payload["summary"]["rows_reviewed"] == 1
    assert payload["summary"]["safe_parser_backfill_rows"] == 1
    assert payload["summary"]["candidate_rows_reviewed"] == 1
    assert payload["summary"]["would_write_parser_rows"] == 1
    assert payload["summary"]["first_blocker"] == "DRY_RUN_OPERATOR_APPROVAL_REQUIRED"
    assert payload["parser_rows_written"] == 0
    assert payload["link_rows_written"] == 0
    assert payload["forecasts_created"] == 0
    assert payload["paper_trades_created"] == 0
    assert len(before_legs) == len(after_legs) == 0


def test_economic_parser_leg_backfill_blocks_link_parser_mismatch(
    tmp_path,
    monkeypatch,
) -> None:
    monkeypatch.chdir(tmp_path)
    with _session(tmp_path) as session:
        _seed_economic_context(session)
        _seed_economic_market(
            session,
            ticker="KXU3-LINK-NO-LEG",
            close_delta=timedelta(hours=2),
            exact_link=True,
            parsed_leg=False,
            title="Will the unemployment rate be above 4.3% in August?",
            event_key="fed",
        )
        payload = phase3an.build_phase3an_economic_parser_leg_backfill(
            session,
            dry_run=True,
            apply=False,
            limit=50,
            max_records=5,
        )
    assert payload["summary"]["safe_parser_backfill_rows"] == 0
    assert payload["summary"]["blocked_parser_backfill_rows"] == 1
    assert payload["summary"]["blocked_reason_counts"]["LINK_PARSER_EVENT_MISMATCH"] == 1
    assert payload["summary"]["first_blocker"] == "LINK_PARSER_EVENT_MISMATCH"
    assert payload["candidate_rows"] == []
    assert payload["blocked_rows"][0]["safe_to_write_parser_leg"] is False
    assert payload["blocked_rows"][0]["parser_event"] == "jobs"
    assert payload["parser_rows_written"] == 0
    assert payload["paper_trades_created"] == 0


def test_economic_parser_leg_backfill_apply_requires_backup_first(
    tmp_path,
    monkeypatch,
) -> None:
    monkeypatch.chdir(tmp_path)
    with _session(tmp_path) as session:
        _seed_economic_context(session)
        _seed_economic_market(
            session,
            ticker="KXCPI-LINK-NO-LEG",
            close_delta=timedelta(hours=2),
            exact_link=True,
            parsed_leg=False,
        )
        with pytest.raises(ValueError, match="--apply requires --backup-first"):
            phase3an.build_phase3an_economic_parser_leg_backfill(
                session,
                dry_run=False,
                apply=True,
                backup_first=False,
                limit=50,
                max_records=5,
            )


def test_economic_operator_approval_packet_is_report_only(
    tmp_path,
    monkeypatch,
) -> None:
    monkeypatch.chdir(tmp_path)
    with _session(tmp_path) as session:
        _seed_economic_context(session)
        _seed_economic_market(
            session,
            ticker="KXCPI-LINK-NO-LEG",
            close_delta=timedelta(hours=2),
            exact_link=True,
            parsed_leg=False,
        )
        _seed_economic_market(
            session,
            ticker="KXU3-LINK-NO-LEG",
            close_delta=timedelta(hours=2),
            exact_link=True,
            parsed_leg=False,
            title="Will the unemployment rate be above 4.3% in August?",
            event_key="fed",
        )
        payload = phase3an.build_phase3an_economic_operator_approval_packet(
            session,
            limit=50,
            max_records=5,
        )
        cpi_legs = list(
            session.scalars(select(MarketLeg).where(MarketLeg.ticker == "KXCPI-LINK-NO-LEG"))
        )
        u3_legs = list(
            session.scalars(select(MarketLeg).where(MarketLeg.ticker == "KXU3-LINK-NO-LEG"))
        )
    summary = payload["summary"]
    assert payload["mode"] == "REPORT_ONLY_ECONOMIC_OPERATOR_APPROVAL_PACKET"
    assert payload["operator_approval_required"] is True
    assert payload["auto_apply_supported"] is False
    assert summary["link_repair_candidates"] == 1
    assert summary["parser_backfill_candidates"] == 1
    assert summary["parser_blocked_rows"] == 1
    assert summary["parser_blocked_reason_counts"]["LINK_PARSER_EVENT_MISMATCH"] == 1
    assert summary["first_blocker"] in {
        "OPERATOR_REVIEW_REQUIRED",
        "OPERATOR_REVIEW_READY_BUT_DB_WRITER_ACTIVE",
    }
    assert payload["link_rows_written"] == 0
    assert payload["parser_rows_written"] == 0
    assert payload["paper_trades_created"] == 0
    assert cpi_legs == []
    assert u3_legs == []
    commands = " ".join(payload["registered_operator_command_sequence"])
    assert "phase3an-economic-link-event-repair" in commands
    assert "phase3an-economic-parser-leg-backfill" in commands
    assert "--backup-first" in commands


def test_economic_approval_safety_guard_passes_report_only_packet(
    tmp_path,
    monkeypatch,
) -> None:
    monkeypatch.chdir(tmp_path)
    with _session(tmp_path) as session:
        _seed_economic_context(session)
        _seed_economic_market(
            session,
            ticker="KXCPI-LINK-NO-LEG",
            close_delta=timedelta(hours=2),
            exact_link=True,
            parsed_leg=False,
        )
        _seed_economic_market(
            session,
            ticker="KXU3-LINK-NO-LEG",
            close_delta=timedelta(hours=2),
            exact_link=True,
            parsed_leg=False,
            title="Will the unemployment rate be above 4.3% in August?",
            event_key="fed",
        )
        payload = phase3an.build_phase3an_economic_approval_safety_guard(
            session,
            limit=50,
            max_records=5,
        )
        cpi_legs = list(
            session.scalars(select(MarketLeg).where(MarketLeg.ticker == "KXCPI-LINK-NO-LEG"))
        )
    summary = payload["summary"]
    assert payload["guard_status"] == "PASS_REPORT_ONLY"
    assert payload["guard_failures"] == []
    assert summary["unregistered_commands"] == []
    assert summary["unguarded_apply_commands"] == []
    assert summary["apply_commands_missing_backup_first"] == []
    assert summary["source_write_failures"] == []
    assert payload["link_rows_written"] == 0
    assert payload["parser_rows_written"] == 0
    assert payload["paper_trades_created"] == 0
    assert cpi_legs == []


def test_economic_approval_safety_guard_can_read_existing_packet(
    tmp_path,
    monkeypatch,
) -> None:
    monkeypatch.chdir(tmp_path)
    with _session(tmp_path) as session:
        _seed_economic_context(session)
        _seed_economic_market(
            session,
            ticker="KXCPI-LINK-NO-LEG",
            close_delta=timedelta(hours=2),
            exact_link=True,
            parsed_leg=False,
        )
        packet = phase3an.build_phase3an_economic_operator_approval_packet(
            session,
            limit=50,
            max_records=5,
        )
    packet_path = Path("reports/phase3an/economic_operator_approval_packet.json")
    packet_path.parent.mkdir(parents=True, exist_ok=True)
    packet_path.write_text(json.dumps(packet), encoding="utf-8")
    artifacts = phase3an.write_phase3an_economic_approval_safety_guard_from_packet_report(
        packet_path=packet_path,
        output_dir=Path("reports/phase3an"),
    )
    payload = json.loads(artifacts.json_path.read_text(encoding="utf-8"))
    assert payload["guard_status"] == "PASS_REPORT_ONLY"
    assert payload["source_packet_path"] == str(packet_path)
    assert payload["summary"]["unregistered_commands"] == []
    assert artifacts.markdown_path.exists()


def test_economic_approval_command_audit_flags_bad_apply_command() -> None:
    payload = phase3an._audit_economic_operator_commands(
        [
            "kalshi-bot phase3an-economic-link-event-repair --apply",
            "kalshi-bot missing-command",
        ]
    )
    assert "UNREGISTERED_OPERATOR_COMMAND" in payload["failures"]
    assert "UNGUARDED_APPLY_COMMAND" in payload["failures"]
    assert "APPLY_COMMAND_MISSING_BACKUP_FIRST" in payload["failures"]
    assert payload["unregistered_commands"] == ["kalshi-bot missing-command"]
    assert payload["unguarded_apply_commands"] == [
        "kalshi-bot phase3an-economic-link-event-repair --apply"
    ]


def test_economic_morning_operator_handoff_summarizes_packet_and_watch(
    tmp_path,
    monkeypatch,
) -> None:
    monkeypatch.chdir(tmp_path)
    with _session(tmp_path) as session:
        _seed_economic_context(session)
        _seed_economic_market(
            session,
            ticker="KXCPI-LINK-NO-LEG",
            close_delta=timedelta(hours=2),
            exact_link=True,
            parsed_leg=False,
        )
        packet = phase3an.build_phase3an_economic_operator_approval_packet(
            session,
            limit=50,
            max_records=5,
        )
    guard = phase3an.build_phase3an_economic_approval_safety_guard_from_packet(packet)
    _write_json(Path("reports/phase3an/economic_operator_approval_packet.json"), packet)
    _write_json(Path("reports/phase3an/economic_approval_safety_guard.json"), guard)
    _write_json(
        Path("reports/phase3bc_r5/phase3bc_r5_status.json"),
        {
            "guard": {"status": "RUNNING", "running": True, "pid": 1234},
            "latest_summary": {
                "cycle_number": 9,
                "total_cycles": 32,
                "positive_ev_rows": 0,
                "paper_ready_candidates": 0,
            },
        },
    )
    _write_json(
        Path("reports/phase3ay/phase3ay_health_refresh.json"),
        {
            "market_health": {
                "status": "NEEDS_COVERAGE_REPAIR",
                "markets_seen": 100,
                "snapshots_inserted": 100,
                "forecasts_inserted": 100,
            },
            "paper_health": {
                "status": "HEALTHY",
                "eligible_exact_settlements": 0,
                "paper_pnl_realized": False,
            },
        },
    )
    artifacts = phase3an.write_phase3an_economic_morning_operator_handoff_report(
        output_dir=Path("reports/phase3an"),
        reports_dir=Path("reports"),
    )
    payload = json.loads(artifacts.json_path.read_text(encoding="utf-8"))
    summary = payload["summary"]
    assert payload["status"] == "READY_FOR_MORNING_OPERATOR_REVIEW"
    assert payload["link_rows_written"] == 0
    assert payload["parser_rows_written"] == 0
    assert payload["paper_trades_created"] == 0
    assert summary["approval_guard_status"] == "PASS_REPORT_ONLY"
    assert summary["r5_status"] == "RUNNING"
    assert summary["r5_cycle_number"] == 9
    assert summary["paper_health_status"] == "HEALTHY"
    assert summary["paper_ready_candidates"] == 0
    assert artifacts.markdown_path.exists()


def test_overnight_refresh_continuity_allows_safe_refresh_when_clean(
    tmp_path,
    monkeypatch,
) -> None:
    monkeypatch.chdir(tmp_path)
    _write_json(
        Path("reports/phase3bc_r5/phase3bc_r5_status.json"),
        {
            "guard": {"status": "RUNNING", "running": True, "pid": 1234},
            "latest_summary": {
                "cycle_number": 10,
                "total_cycles": 32,
                "positive_ev_rows": 0,
                "paper_ready_candidates": 0,
            },
        },
    )
    _write_json(
        Path("reports/phase3ay/phase3ay_health_refresh.json"),
        {
            "mode": "PAPER_SETTLEMENT_ONLY_REFRESH_LOOP",
            "market_health": {
                "status": "NEEDS_COVERAGE_REPAIR",
                "markets_seen": 100,
                "snapshots_inserted": 100,
                "forecasts_inserted": 100,
            },
            "paper_health": {
                "status": "HEALTHY",
                "eligible_exact_settlements": 0,
                "paper_pnl_realized": False,
            },
        },
    )
    _write_json(
        Path("reports/phase3an/economic_morning_operator_handoff.json"),
        {
            "status": "READY_FOR_MORNING_OPERATOR_REVIEW",
            "summary": {"first_blocker": "AWAITING_OPERATOR_REVIEW"},
        },
    )
    _write_json(
        Path("reports/phase3an/economic_approval_safety_guard.json"),
        {"guard_status": "PASS_REPORT_ONLY"},
    )
    artifacts = phase3an.write_phase3an_overnight_refresh_continuity_report(
        output_dir=Path("reports/phase3an"),
        reports_dir=Path("reports"),
    )
    payload = json.loads(artifacts.json_path.read_text(encoding="utf-8"))
    summary = payload["summary"]
    assert payload["status"] == "CONTINUE_SAFE_REFRESH"
    assert payload["continuity_flags"] == []
    assert payload["link_rows_written"] == 0
    assert payload["parser_rows_written"] == 0
    assert payload["paper_trades_created"] == 0
    assert summary["r5_cycle_number"] == 10
    assert summary["paper_health_status"] == "HEALTHY"
    assert any("phase3ay-health-refresh" in cmd for cmd in payload["safe_overnight_commands"])
    assert artifacts.markdown_path.exists()


def test_overnight_refresh_continuity_flags_positive_ev_for_review(
    tmp_path,
    monkeypatch,
) -> None:
    monkeypatch.chdir(tmp_path)
    _write_json(
        Path("reports/phase3bc_r5/phase3bc_r5_status.json"),
        {
            "guard": {"status": "RUNNING", "running": True, "pid": 1234},
            "latest_summary": {
                "cycle_number": 10,
                "total_cycles": 32,
                "positive_ev_rows": 2,
                "paper_ready_candidates": 1,
            },
        },
    )
    _write_json(
        Path("reports/phase3ay/phase3ay_health_refresh.json"),
        {"paper_health": {"status": "HEALTHY"}},
    )
    _write_json(
        Path("reports/phase3an/economic_morning_operator_handoff.json"),
        {"status": "READY_FOR_MORNING_OPERATOR_REVIEW"},
    )
    _write_json(
        Path("reports/phase3an/economic_approval_safety_guard.json"),
        {"guard_status": "PASS_REPORT_ONLY"},
    )
    payload = phase3an.build_phase3an_overnight_refresh_continuity(
        output_dir=Path("reports/phase3an"),
        reports_dir=Path("reports"),
    )
    assert payload["status"] == "REVIEW_BEFORE_REFRESH"
    assert "PAPER_READY_CANDIDATES_REQUIRE_OPERATOR_REVIEW" in payload["continuity_flags"]
    assert "POSITIVE_EV_ROWS_REQUIRE_OPERATOR_REVIEW" in payload["continuity_flags"]
    assert payload["paper_trades_created"] == 0


def test_unified_report_generates_all_artifacts(tmp_path, monkeypatch) -> None:
    monkeypatch.chdir(tmp_path)
    _write_crypto_status(tmp_path)
    _write_sports_reports(tmp_path, placeholders=1, partial=1)
    with _session(tmp_path) as session:
        artifacts = phase3an.write_phase3an_gap_fix_report(
            session,
            output_dir=Path("reports/phase3an"),
            reports_dir=Path("reports"),
        )
    required = {
        "runtime_identity",
        "crypto_watch_doctor",
        "paper_funnel_explain",
        "settlement_health_confirm",
        "3bb_r2_burndown",
        "usda_date_mismatch_report",
        "general_sources_status",
        "sports_blocker_report",
        "economic_news_watch",
        "phase3az_before_after",
    }
    assert required.issubset(artifacts.artifact_paths)
    assert artifacts.summary_path.exists()
    assert artifacts.next_actions_path.exists()
    assert artifacts.manifest_path.exists()


def test_dashboard_consumes_specific_status_reason_codes(tmp_path, monkeypatch) -> None:
    monkeypatch.chdir(tmp_path)
    _write_json(
        Path("reports/phase3an/phase3an_dashboard_status.json"),
        {
            "summary": {
                "crypto_watch": {"status": "RUNNING_CYCLE_OVERDUE", "next_action": "Inspect slow stage"},
                "paper_funnel": {
                    "first_hard_blocker": "NO_POSITIVE_RAW_EV",
                    "top_reason": ["NO_POSITIVE_RAW_EV", 2000],
                    "tradeable_rows": 0,
                },
                "settlement": {
                    "status": "HEALTHY",
                    "exact_eligible_trades": 0,
                    "apply_command_exposed": False,
                },
                "general_sources": {
                    "USDA": "USDA_DATE_MISMATCH",
                    "Cushman": "CUSHMAN_VALUES_UNAVAILABLE",
                    "FlightAware": "FLIGHTAWARE_READY_FOR_REVIEW",
                    "source_evidence_ready_rows": 0,
                },
                "phase3bb_r2": {"evidence_ready_rows": 0, "source_blocker": "Resolve source"},
                "sports": {"placeholder_rows": 2, "partial_provenance_markets": 1, "reason_codes": ["ROUND_PLACEHOLDER"]},
                "economic_news": {
                    "blocker_reason": "WAITING_FOR_COMPATIBLE_MARKETS",
                    "economic_compatible_parsed_markets": 0,
                    "news_compatible_parsed_markets": 0,
                },
            }
        },
    )
    status = paper_trade_blocker_status(
        crypto_freshness={
            "paper_ready_candidates": 0,
            "positive_ev_rows": 0,
            "actionability_gap": "NO_POSITIVE_EV",
        }
    )
    evidence = " ".join(row["evidence"] for row in status["blockers"])
    assert "USDA_DATE_MISMATCH" in evidence
    assert status["status_label"] == "Running Cycle Overdue"


def test_phase3an_commands_terminate_by_default_help() -> None:
    runner = CliRunner()
    for command in (
        "phase3an-preflight",
        "phase3an-crypto-watch-doctor",
        "phase3an-crypto-watch-restart-plan",
        "phase3an-paper-funnel-explain",
        "phase3an-settlement-health-confirm",
        "phase3an-3bb-r2-burndown",
        "phase3an-usda-date-mismatch-report",
        "phase3an-general-sources-status",
        "phase3an-sports-blocker-report",
        "phase3an-economic-news-watch",
        "phase3an-economic-news-parser-backfill-plan",
        "phase3an-economic-link-event-repair-plan",
        "phase3an-economic-link-event-repair",
        "phase3an-economic-parser-leg-backfill",
        "phase3an-economic-operator-approval-packet",
        "phase3an-economic-approval-safety-guard",
        "phase3an-economic-morning-operator-handoff",
        "phase3an-overnight-refresh-continuity",
        "phase3an-gap-fix-report",
    ):
        result = runner.invoke(app, [command, "--help"])
        assert result.exit_code == 0
        assert command in result.output


def test_no_live_or_demo_exchange_writes_occur(tmp_path, monkeypatch) -> None:
    monkeypatch.chdir(tmp_path)
    with _session(tmp_path) as session:
        payload = phase3an.build_phase3an_settlement_health_confirm(session)
    assert payload["live_or_demo_execution"] is False
    assert payload["safety_flags"]["live_trading_enabled"] is False
    assert payload["safety_flags"]["demo_exchange_writes_enabled"] is False


def test_no_downstream_writes_except_local_evidence_file_only(tmp_path, monkeypatch) -> None:
    monkeypatch.chdir(tmp_path)
    with _session(tmp_path) as session:
        payload = phase3an.build_phase3an_3bb_r2_burndown(session)
    summary = payload["summary"]
    assert summary["link_writes"] is False
    assert summary["feature_writes"] is False
    assert summary["forecast_writes"] is False
    assert summary["opportunity_writes"] is False
    assert summary["paper_trade_writes"] is False
    assert summary["settlement_writes"] is False


def _session(tmp_path):
    engine = init_db(f"sqlite:///{Path(tmp_path) / 'phase3an.db'}")
    return get_session_factory(engine)()


def _seed_rankings(session, *, count: int) -> None:
    now = utc_now()
    session.add_all(
        MarketRanking(
            ticker=f"KXTEST-{idx:04d}",
            ranked_at=now,
            title=f"Test ranking {idx}",
            status="open",
            series_ticker="KXTEST",
            event_ticker=f"KXTEST-E{idx}",
            volume="0",
            open_interest="0",
            liquidity="0",
            spread="0.01",
            midpoint="0.50",
            time_to_close_minutes="240",
            forecast_model="ensemble_v2",
            forecast_probability="0.51",
            best_side="BUY_YES",
            best_price="0.50",
            estimated_edge="0.01",
            liquidity_score="100",
            spread_score="100",
            time_score="100",
            model_confidence_score="80",
            opportunity_score="10",
            reason="seeded ranking",
            raw_json="{}",
        )
        for idx in range(count)
    )
    session.flush()


def _seed_positive_raw_ev_lost_to_spread(session) -> None:
    now = utc_now()
    ticker = "KXSPREADTEST"
    upsert_market(
        session,
        {
            "ticker": ticker,
            "status": "open",
            "title": "Will BTC be above 100000?",
            "series_ticker": "KXSPREAD",
            "event_ticker": "KXSPREAD-EVENT",
            "close_time": (now + timedelta(hours=4)).isoformat(),
            "yes_bid_dollars": "0.35",
            "yes_ask_dollars": "0.85",
        },
    )
    insert_market_snapshot(
        session,
        {
            "ticker": ticker,
            "status": "open",
            "yes_bid_dollars": "0.35",
            "yes_ask_dollars": "0.85",
        },
        {"yes": [[35, 10]], "no": [[15, 10]]},
        now,
    )
    insert_forecast(
        session,
        {
            "ticker": ticker,
            "forecasted_at": now,
            "model_name": "ensemble_v2",
            "yes_probability": "0.70",
            "market_mid_probability": "0.60",
            "best_yes_bid": "0.35",
            "best_yes_ask": "0.85",
            "feature_json": {},
        },
    )
    session.add(
        MarketRanking(
            ticker=ticker,
            ranked_at=now,
            title="Will BTC be above 100000?",
            status="open",
            series_ticker="KXSPREAD",
            event_ticker="KXSPREAD-EVENT",
            volume="100",
            open_interest="100",
            liquidity="100",
            spread="0.50",
            midpoint="0.60",
            time_to_close_minutes="240",
            forecast_model="ensemble_v2",
            forecast_probability="0.70",
            best_side="BUY_YES",
            best_price="0.40",
            estimated_edge="0.30",
            liquidity_score="100",
            spread_score="1",
            time_score="100",
            model_confidence_score="80",
            opportunity_score="90",
            reason="seeded positive raw EV",
            raw_json="{}",
        )
    )
    session.flush()


def _seed_economic_context(session) -> None:
    now = utc_now()
    session.add(
        EconomicEvent(
            event_key="cpi",
            source="test",
            event_time=now + timedelta(days=1),
            category="inflation",
            title="CPI release",
            actual_value=None,
            forecast_value="3.0",
            previous_value="3.1",
            raw_json=json.dumps({"source": "test"}),
            created_at=now,
        )
    )
    session.add(
        EconomicFeature(
            event_key="cpi",
            generated_at=now,
            category="inflation",
            surprise_score="0.1",
            direction="higher",
            confidence_score="0.80",
            raw_json=json.dumps({"source": "test"}),
            created_at=now,
        )
    )
    session.flush()


def _seed_economic_market(
    session,
    *,
    ticker: str,
    close_delta: timedelta,
    exact_link: bool,
    parsed_leg: bool = True,
    title: str = "Will CPI inflation be above 3.0 percent?",
    event_key: str = "cpi",
) -> None:
    now = utc_now()
    close_time = now + close_delta
    upsert_market(
        session,
        {
            "ticker": ticker,
            "status": "open",
            "title": title,
            "series_ticker": "KXCPI",
            "event_ticker": f"{ticker}-EVENT",
            "close_time": close_time.isoformat(),
            "expected_expiration_time": (close_time + timedelta(minutes=5)).isoformat(),
        },
    )
    if parsed_leg:
        session.add(
            MarketLeg(
                ticker=ticker,
                leg_index=0,
                parsed_at=now,
                side="yes",
                category="economic",
                market_type="BINARY",
                entity_name="CPI inflation",
                operator="gt",
                threshold_value="3.0",
                unit="percent",
                confidence="0.95",
                raw_text=title,
                reason="test economic parser",
                raw_json=json.dumps({"source": "test"}),
            )
        )
    if exact_link:
        session.add(
            EconomicMarketLink(
                ticker=ticker,
                event_key=event_key,
                detected_at=now,
                category="inflation",
                confidence="0.95",
                reason="exact ticker test link",
                raw_json=json.dumps({"source": "test"}),
            )
        )
    session.flush()


def _write_crypto_status(tmp_path) -> None:
    old = utc_now() - timedelta(minutes=45)
    _write_json(
        Path(tmp_path) / "reports/phase3bc_r5/phase3bc_r5_status.json",
        {
            "generated_at": utc_now().isoformat(),
            "guard": {
                "status": "RUNNING",
                "running": True,
                "pid": 1234,
                "latest_generated_at": old.isoformat(),
                "seconds_until_timeout": 120,
            },
            "latest_summary": {
                "cycle_number": 1,
                "total_cycles": 32,
                "current_stage": "forecast",
                "paper_ready_candidates": 0,
                "positive_ev_rows": 0,
            },
        },
    )


def _write_usda_record(tmp_path, *, as_of_date: str, value: str = "") -> None:
    _write_json(
        Path(tmp_path) / "data/general_source_evidence/commodity_advertised_price_source.json",
        {
            "records": [
                {
                    "source_adapter_key": "commodity_advertised_price_source",
                    "source_name": "USDA AMS",
                    "source_url": "https://example.com/usda",
                    "source_subject": "Avocados, Hass",
                    "commodity": "Avocados",
                    "variety": "Hass",
                    "as_of_date": as_of_date,
                    "price_usd_each": value,
                }
            ]
        },
    )


def _write_general_source_activation_reports(tmp_path) -> None:
    reports_dir = Path(tmp_path) / "reports"
    r2_dir = reports_dir / "phase3bb_r2_sources"
    r3_dir = reports_dir / "phase3bb_r3_source_activation"
    r4_dir = reports_dir / "phase3bb_r4_flightaware"
    r5_dir = reports_dir / "phase3bb_r5_flightaware"
    _write_json(
        r2_dir / "phase3bb_r2_general_source_evidence.json",
        {
            "summary": {
                "exact_evidence_ready_rows": 9,
                "safe_to_link_rows": 0,
                "safe_to_forecast_rows": 0,
            }
        },
    )
    _write_json(
        r2_dir / "phase3bb_r2_general_source_availability.json",
        {
            "summary": {
                "source_value_available_rows": 9,
                "safe_to_link_rows": 0,
                "safe_to_forecast_rows": 0,
            }
        },
    )
    _write_json(
        r3_dir / "source_evidence_activation.json",
        {
            "summary": {
                "activation_readiness": "NOT_READY",
                "first_hard_blocker": "SOURCE_DATE_MISMATCH_BLOCKER",
                "link_safe_rows": 0,
                "forecast_safe_rows": 0,
                "activation_candidate_rows": 0,
                "source_date_mismatch_blockers": True,
                "proprietary_review_blockers": True,
                "review_required_blockers": True,
            },
            "source_activation_decisions": [
                {
                    "source_name": "USDA",
                    "activation_status": "GATED",
                    "affected_rows": 7,
                    "evidence_ready_rows": 0,
                    "link_safe_rows": 0,
                    "forecast_safe_rows": 0,
                    "first_blocker": "SOURCE_DATE_MISMATCH_BLOCKER",
                    "blocker_codes": [
                        "SOURCE_DATE_MISMATCH_BLOCKER",
                        "LINK_SAFE_FALSE",
                        "FORECAST_SAFE_FALSE",
                    ],
                    "next_action": "Obtain exact official July 3 USDA evidence.",
                },
                {
                    "source_name": "FlightAware",
                    "activation_status": "GATED",
                    "affected_rows": 9,
                    "evidence_ready_rows": 9,
                    "link_safe_rows": 0,
                    "forecast_safe_rows": 0,
                    "source_value_available_for_review": True,
                    "first_blocker": "READY_FOR_REVIEW_NOT_LINK_SAFE",
                    "blocker_codes": [
                        "READY_FOR_REVIEW_NOT_LINK_SAFE",
                        "LINK_SAFE_FALSE",
                        "FORECAST_SAFE_FALSE",
                    ],
                    "next_action": "Build reviewed FlightAware gates before promotion.",
                },
                {
                    "source_name": "Cushman",
                    "activation_status": "GATED",
                    "affected_rows": 9,
                    "evidence_ready_rows": 0,
                    "link_safe_rows": 0,
                    "forecast_safe_rows": 0,
                    "first_blocker": "PROPRIETARY_REVIEW_REQUIRED",
                    "blocker_codes": [
                        "PROPRIETARY_REVIEW_REQUIRED",
                        "LINK_SAFE_FALSE",
                        "FORECAST_SAFE_FALSE",
                    ],
                    "next_action": "Resolve licensing review before promotion.",
                },
            ],
        },
    )
    _write_json(
        r4_dir / "flightaware_review_link_gate.json",
        {
            "summary": {
                "affected_rows": 9,
                "source_value_available_for_review": True,
                "date_stable_evidence_available": False,
                "link_safe_rows": 0,
                "forecast_safe_rows": 0,
            }
        },
    )
    _write_json(
        r5_dir / "flightaware_date_stable_evidence.json",
        {
            "summary": {
                "affected_rows": 9,
                "accepted_date_stable_evidence_rows": 0,
                "date_stable_evidence_status": "NOT_FOUND",
                "source_value_available_for_review": True,
                "first_hard_blocker": (
                    "OFFICIAL_FLIGHTAWARE_HISTORICAL_AGGREGATE_UNAVAILABLE"
                ),
                "link_safe_rows": 0,
                "forecast_safe_rows": 0,
                "next_action": "External FlightAware historical aggregate access is required.",
            }
        },
    )


def _write_sports_reports(tmp_path, *, placeholders: int, partial: int) -> None:
    _write_json(
        Path(tmp_path) / "reports/phase3ah_sports/phase3ah_sports_placeholder_watch.json",
        {
            "summary": {
                "placeholder_rows_reviewed": placeholders,
                "still_placeholder_rows": placeholders,
            }
        },
    )
    _write_json(
        Path(tmp_path) / "reports/phase3ah_sports/phase3ah_sports_evidence_backfill.json",
        {"summary": {"schedule_windows": 0, "roster_review_rows": 0, "team_alias_review_rows": 0}},
    )
    _write_json(
        Path(tmp_path) / "reports/phase3z_r2/phase3z_r2_sports_provenance_repair.json",
        {
            "summary": {
                "partial_legacy_markets": partial,
                "rows_safe_to_repair": 0,
                "placeholder_blocked_rows": placeholders,
            }
        },
    )


def _write_json(path: Path, payload: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, indent=2, sort_keys=True, default=str), encoding="utf-8")
