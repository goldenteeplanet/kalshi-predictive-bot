import json
from pathlib import Path

from typer.testing import CliRunner

from kalshi_predictor.cli import app
from kalshi_predictor.phase3az import (
    write_phase3az_gap_analysis_report,
    write_phase3az_r11_non_crypto_activation_report,
)


def test_phase3az_gap_analysis_prioritizes_actionable_report_gaps(tmp_path, monkeypatch) -> None:
    monkeypatch.chdir(tmp_path)
    _write_reports(Path("reports"))

    artifacts = write_phase3az_gap_analysis_report(
        output_dir=Path("reports/phase3az"),
        reports_dir=Path("reports"),
    )

    payload = json.loads(artifacts.json_path.read_text(encoding="utf-8"))
    gap_ids = {row["gap_id"] for row in payload["gaps"]}
    assert "paper_realization_residue" in gap_ids
    assert "settled_source_without_usable_outcome" in gap_ids
    assert "market_coverage_degraded" in gap_ids
    assert "sports_round_placeholders_block_phase3ae" in gap_ids
    assert payload["summary"]["implementation_needed_count"] >= 3
    assert payload["implementation_queue"][0]["safety"] == "PAPER_ONLY_NO_LIVE_OR_DEMO_EXECUTION"
    assert "Phase 3AZ" in artifacts.markdown_path.read_text(encoding="utf-8")


def test_phase3az_suppresses_stale_unusable_outcome_gap_when_r3_cleared(
    tmp_path, monkeypatch
) -> None:
    monkeypatch.chdir(tmp_path)
    reports_dir = Path("reports")
    _write_reports(reports_dir)
    _write_json(
        reports_dir / "phase3aa" / "phase3aa_outcome_realizer.json",
        {
            "eligible_after_realize": 0,
            "eta_schedule": {"summary": {"due_or_overdue": 10}},
        },
    )
    _write_json(
        reports_dir / "phase3aa_r3" / "phase3aa_r3_residual_settlement_audit.json",
        {"summary": {"residue_cleared": True, "residual_rows": 0}},
    )
    _write_json(
        reports_dir
        / "paper_settlement_reconciliation"
        / "paper_settlement_reconciliation.json",
        {"summary": {"eligible_to_settle_now": 0}},
    )

    artifacts = write_phase3az_gap_analysis_report(
        output_dir=reports_dir / "phase3az",
        reports_dir=reports_dir,
    )

    payload = json.loads(artifacts.json_path.read_text(encoding="utf-8"))
    gap_ids = {row["gap_id"] for row in payload["gaps"]}
    assert "settled_source_without_usable_outcome" not in gap_ids


def test_phase3az_uses_phase3zr2_when_sports_degradation_is_diagnosed(
    tmp_path, monkeypatch
) -> None:
    monkeypatch.chdir(tmp_path)
    reports_dir = Path("reports")
    _write_reports(reports_dir)
    _write_json(
        reports_dir / "phase3z_r2" / "phase3z_r2_sports_provenance_repair.json",
        {
            "summary": {
                "rows_reviewed": 100,
                "rows_safe_to_repair": 0,
                "partial_legacy_markets": 20,
                "partial_legacy_link_rows": 40,
                "unlinked_parsed_markets": 5,
                "placeholder_blocked_rows": 8,
            }
        },
    )

    artifacts = write_phase3az_gap_analysis_report(
        output_dir=reports_dir / "phase3az",
        reports_dir=reports_dir,
    )

    payload = json.loads(artifacts.json_path.read_text(encoding="utf-8"))
    market_gap = next(row for row in payload["gaps"] if row["gap_id"] == "market_coverage_degraded")
    sports_gap = next(
        row for row in payload["gaps"] if row["gap_id"] == "sports_partial_provenance"
    )
    assert market_gap["implementation_needed"] is False
    assert market_gap["severity"] == "MEDIUM"
    assert "Phase 3Z-R2 reviewed 100" in market_gap["evidence"]
    assert sports_gap["implementation_needed"] is False
    assert "20 sports partial market" in sports_gap["evidence"]
    assert "20 distinct partial market" in sports_gap["evidence"]


def test_phase3az_uses_r5_closed_outcome_capture_to_avoid_stale_r2_prompt(
    tmp_path,
    monkeypatch,
) -> None:
    monkeypatch.chdir(tmp_path)
    reports_dir = Path("reports")
    _write_reports(reports_dir)
    _write_json(
        reports_dir / "phase3aa" / "phase3aa_outcome_realizer.json",
        {
            "eligible_after_realize": 0,
            "eta_schedule": {"summary": {"due_or_overdue": 42}},
        },
    )
    _write_json(
        reports_dir / "phase3aa_r2" / "phase3aa_r2_exact_settlement_harvest.json",
        {
            "summary": {
                "exact_settlements_written": 0,
                "source_closed_without_outcome": 42,
                "fetch_errors": 75,
            }
        },
    )
    _write_json(
        reports_dir / "phase3aa_r5" / "phase3aa_r5_closed_market_outcome_capture.json",
        {
            "summary": {
                "closed_without_outcome_rows": 42,
                "usable_outcome_candidate_rows": 0,
            }
        },
    )

    artifacts = write_phase3az_gap_analysis_report(
        output_dir=reports_dir / "phase3az",
        reports_dir=reports_dir,
    )

    payload = json.loads(artifacts.json_path.read_text(encoding="utf-8"))
    due_gap = next(
        row for row in payload["gaps"] if row["gap_id"] == "due_paper_without_new_exact_settlements"
    )
    closed_gap = next(
        row for row in payload["gaps"] if row["gap_id"] == "closed_markets_without_exposed_outcome"
    )
    assert "Phase 3AA-R5 found 42" in due_gap["evidence"]
    assert "Keep exact-ticker watch active" in due_gap["next_action"]
    assert closed_gap["implementation_needed"] is False


def test_phase3az_promotes_phase3bb_general_domain_work(tmp_path, monkeypatch) -> None:
    monkeypatch.chdir(tmp_path)
    reports_dir = Path("reports")
    _write_clean_reports(reports_dir)
    _write_json(
        reports_dir / "phase3bb" / "phase3bb_domain_readiness.json",
        {
            "domain_rows": [
                {
                    "domain": "economic",
                    "status": "WAITING_FOR_COMPATIBLE_MARKETS",
                    "actionable_now": False,
                },
                {
                    "domain": "news",
                    "status": "CONTEXT_READY_NO_ACTIVE_NEWS_MARKETS",
                    "actionable_now": False,
                },
                {
                    "domain": "general",
                    "status": "OBSERVED_ONLY_NO_SPECIALIZED_LINKER",
                    "actionable_now": True,
                    "counts": {
                        "parsed_markets": 14845,
                        "active_parsed_markets": 10096,
                    },
                    "taxonomy_counts": {
                        "SPORTS_OR_CROSS_CATEGORY_LEAKAGE": 14397,
                        "ECONOMIC_CANDIDATE": 215,
                        "GEOPOLITICAL_NEWS_CANDIDATE": 175,
                        "COMPANY_NEWS_CANDIDATE": 54,
                    },
                },
            ]
        },
    )

    artifacts = write_phase3az_gap_analysis_report(
        output_dir=reports_dir / "phase3az",
        reports_dir=reports_dir,
    )

    payload = json.loads(artifacts.json_path.read_text(encoding="utf-8"))
    gap_ids = {row["gap_id"] for row in payload["gaps"]}
    general_gap = next(
        row for row in payload["gaps"] if row["gap_id"] == "general_domain_taxonomy_actionable"
    )
    assert "general_domain_taxonomy_actionable" in gap_ids
    assert "economic_news_waiting_for_compatible_markets" in gap_ids
    assert general_gap["phase"] == "3BB-R2"
    assert general_gap["command"] == (
        "kalshi-bot phase3bb-r2-general-candidate-routing --output-dir reports/phase3bb_r2"
    )
    assert general_gap["implementation_needed"] is True
    assert "ECONOMIC_CANDIDATE=215" in general_gap["evidence"]
    assert payload["implementation_queue"][0]["phase"] == "3BB-R2"
    assert payload["recommended_next_action"] == (
        "Implement 3BB-R2 for general_domain_taxonomy_actionable next."
    )


def test_phase3az_promotes_phase3bb_r3_after_r2_finds_only_sports_leakage(
    tmp_path,
    monkeypatch,
) -> None:
    monkeypatch.chdir(tmp_path)
    reports_dir = Path("reports")
    _write_clean_reports(reports_dir)
    _write_json(
        reports_dir / "phase3bb" / "phase3bb_domain_readiness.json",
        {
            "domain_rows": [
                {
                    "domain": "economic",
                    "status": "WAITING_FOR_COMPATIBLE_MARKETS",
                    "actionable_now": False,
                },
                {
                    "domain": "news",
                    "status": "CONTEXT_READY_NO_ACTIVE_NEWS_MARKETS",
                    "actionable_now": False,
                },
                {
                    "domain": "general",
                    "status": "OBSERVED_ONLY_NO_SPECIALIZED_LINKER",
                    "actionable_now": True,
                    "counts": {
                        "parsed_markets": 14845,
                        "active_parsed_markets": 10096,
                    },
                    "taxonomy_counts": {
                        "GENERAL_UNCLASSIFIED": 4,
                        "SPORTS_OR_CROSS_CATEGORY_LEAKAGE": 14841,
                    },
                },
            ]
        },
    )
    _write_json(
        reports_dir / "phase3bb_r2" / "phase3bb_r2_general_candidate_routing.json",
        {
            "summary": {
                "candidate_buckets": {
                    "economic": 0,
                    "news": 0,
                    "sports_or_cross_category_leakage": 14841,
                    "unsupported_or_unclassified": 4,
                }
            }
        },
    )

    artifacts = write_phase3az_gap_analysis_report(
        output_dir=reports_dir / "phase3az",
        reports_dir=reports_dir,
    )

    payload = json.loads(artifacts.json_path.read_text(encoding="utf-8"))
    general_gap = next(
        row for row in payload["gaps"] if row["gap_id"] == "general_domain_taxonomy_actionable"
    )
    assert general_gap["phase"] == "3BB-R3"
    assert general_gap["command"] == (
        "kalshi-bot phase3bb-r3-general-reclassification --output-dir reports/phase3bb_r3"
    )
    assert "sports/cross-category" in general_gap["title"]
    assert payload["implementation_queue"][0]["phase"] == "3BB-R3"
    assert payload["recommended_next_action"] == (
        "Implement 3BB-R3 for general_domain_taxonomy_actionable next."
    )


def test_phase3az_keeps_phase3bb_r2_when_operational_candidates_remain(
    tmp_path,
    monkeypatch,
) -> None:
    monkeypatch.chdir(tmp_path)
    reports_dir = Path("reports")
    _write_clean_reports(reports_dir)
    _write_json(
        reports_dir / "phase3bb" / "phase3bb_domain_readiness.json",
        {
            "domain_rows": [
                {
                    "domain": "economic",
                    "status": "WAITING_FOR_COMPATIBLE_MARKETS",
                    "actionable_now": False,
                },
                {
                    "domain": "news",
                    "status": "CONTEXT_READY_NO_ACTIVE_NEWS_MARKETS",
                    "actionable_now": False,
                },
                {
                    "domain": "general",
                    "status": "OBSERVED_ONLY_NO_SPECIALIZED_LINKER",
                    "actionable_now": True,
                    "counts": {
                        "parsed_markets": 127,
                        "active_parsed_markets": 127,
                    },
                    "taxonomy_counts": {
                        "GENERAL_UNCLASSIFIED": 12,
                        "SPORTS_OR_CROSS_CATEGORY_LEAKAGE": 6,
                        "COMMODITY_PRICE_CANDIDATE": 7,
                        "TRANSPORTATION_OPERATION_CANDIDATE": 9,
                    },
                },
            ]
        },
    )
    _write_json(
        reports_dir / "phase3bb_r2" / "phase3bb_r2_general_candidate_routing.json",
        {
            "summary": {
                "candidate_buckets": {
                    "economic": 0,
                    "news": 0,
                    "operational_or_commodity": 16,
                    "sports_or_cross_category_leakage": 6,
                    "unsupported_or_unclassified": 12,
                }
            }
        },
    )

    artifacts = write_phase3az_gap_analysis_report(
        output_dir=reports_dir / "phase3az",
        reports_dir=reports_dir,
    )

    payload = json.loads(artifacts.json_path.read_text(encoding="utf-8"))
    general_gap = next(
        row for row in payload["gaps"] if row["gap_id"] == "general_domain_taxonomy_actionable"
    )
    assert general_gap["phase"] == "3BB-R2"
    assert general_gap["command"] == (
        "kalshi-bot phase3bb-r2-general-candidate-routing --output-dir reports/phase3bb_r2"
    )
    assert payload["implementation_queue"][0]["phase"] == "3BB-R2"


def test_phase3az_routes_phase3bb_r2_diagnostics_to_source_evidence(
    tmp_path,
    monkeypatch,
) -> None:
    monkeypatch.chdir(tmp_path)
    reports_dir = Path("reports")
    _write_clean_reports(reports_dir)
    _write_json(
        reports_dir / "phase3bb" / "phase3bb_domain_readiness.json",
        {
            "domain_rows": [
                {
                    "domain": "general",
                    "status": "OBSERVED_ONLY_NO_SPECIALIZED_LINKER",
                    "actionable_now": True,
                    "counts": {
                        "parsed_markets": 127,
                        "active_parsed_markets": 127,
                    },
                    "taxonomy_counts": {
                        "COMMODITY_PRICE_CANDIDATE": 7,
                        "TRANSPORTATION_OPERATION_CANDIDATE": 9,
                        "INFRASTRUCTURE_CAPACITY_CANDIDATE": 9,
                    },
                },
            ]
        },
    )
    _write_json(
        reports_dir / "phase3bb_r2" / "phase3bb_r2_general_candidate_routing.json",
        {
            "summary": {
                "candidate_buckets": {
                    "economic": 0,
                    "news": 0,
                    "operational_or_commodity": 25,
                    "sports_or_cross_category_leakage": 6,
                    "unsupported_or_unclassified": 12,
                },
                "general_signal_diagnostics": {
                    "diagnostic_rows": 25,
                    "safe_to_forecast_rows": 0,
                    "readiness_counts": {"SOURCE_DESIGN_REQUIRED": 25},
                    "source_adapter_counts": {
                        "commodity_advertised_price_source": 7,
                        "transportation_flight_cancellation_source": 9,
                        "infrastructure_data_center_capacity_source": 9,
                    },
                },
            }
        },
    )

    artifacts = write_phase3az_gap_analysis_report(
        output_dir=reports_dir / "phase3az",
        reports_dir=reports_dir,
    )

    payload = json.loads(artifacts.json_path.read_text(encoding="utf-8"))
    general_gap = next(
        row for row in payload["gaps"] if row["gap_id"] == "general_domain_taxonomy_actionable"
    )
    assert general_gap["phase"] == "3BB-R2"
    assert general_gap["command"] == (
        "kalshi-bot phase3bb-r2-general-source-intake "
        "--output-dir reports/phase3bb_r2_sources"
    )
    assert "source-evidence" in general_gap["next_action"]
    assert payload["implementation_queue"][0]["phase"] == "3BB-R2"


def test_phase3az_clears_phase3bb_r2_after_source_intake_bundle(
    tmp_path,
    monkeypatch,
) -> None:
    monkeypatch.chdir(tmp_path)
    reports_dir = Path("reports")
    _write_clean_reports(reports_dir)
    _write_json(
        reports_dir / "phase3bb" / "phase3bb_domain_readiness.json",
        {
            "domain_rows": [
                {
                    "domain": "general",
                    "status": "OBSERVED_ONLY_NO_SPECIALIZED_LINKER",
                    "actionable_now": True,
                    "counts": {
                        "parsed_markets": 127,
                        "active_parsed_markets": 127,
                    },
                    "taxonomy_counts": {
                        "COMMODITY_PRICE_CANDIDATE": 7,
                        "GENERAL_UNCLASSIFIED": 68,
                        "INFRASTRUCTURE_CAPACITY_CANDIDATE": 9,
                        "SPORTS_OR_CROSS_CATEGORY_LEAKAGE": 34,
                        "TRANSPORTATION_OPERATION_CANDIDATE": 9,
                    },
                },
            ]
        },
    )
    _write_json(
        reports_dir / "phase3bb_r2" / "phase3bb_r2_general_candidate_routing.json",
        {
            "summary": {
                "candidate_buckets": {
                    "economic": 0,
                    "news": 0,
                    "operational_or_commodity": 25,
                    "sports_or_cross_category_leakage": 34,
                    "unsupported_or_unclassified": 68,
                },
                "general_signal_diagnostics": {
                    "diagnostic_rows": 25,
                    "safe_to_forecast_rows": 0,
                    "readiness_counts": {"SOURCE_DESIGN_REQUIRED": 25},
                    "source_adapter_counts": {
                        "commodity_advertised_price_source": 7,
                        "transportation_flight_cancellation_source": 9,
                        "infrastructure_data_center_capacity_source": 9,
                    },
                },
            }
        },
    )
    _write_json(
        reports_dir / "phase3bb_r2_sources" / "general_source_intake.json",
        {
            "safety_mode": "REPORT_ONLY_NO_WRITES",
            "summary": {
                "general_markets_reviewed": 127,
                "active_general_markets_reviewed": 127,
                "taxonomy_counts": {
                    "COMMODITY_PRICE_CANDIDATE": 7,
                    "GENERAL_UNCLASSIFIED": 68,
                    "INFRASTRUCTURE_CAPACITY_CANDIDATE": 9,
                    "SPORTS_OR_CROSS_CATEGORY_LEAKAGE": 34,
                    "TRANSPORTATION_OPERATION_CANDIDATE": 9,
                },
                "link_writes": False,
                "feature_writes": False,
                "forecast_writes": False,
                "opportunity_writes": False,
                "paper_trade_writes": False,
                "settlement_writes": False,
                "live_or_demo_execution": False,
            },
            "safety_gate": {
                "writes_database": False,
                "writes_links": False,
                "writes_features": False,
                "writes_forecasts": False,
                "writes_opportunities": False,
                "places_paper_orders": False,
                "settles_trades": False,
                "places_demo_orders": False,
                "places_live_orders": False,
            },
            "taxonomy_review_rows": [{"market_ticker": "KXTEST"}],
            "source_evidence_requirements": [{"market_ticker": "KXTEST"}],
            "source_readiness_matrix": [{"source_name": "USDA", "link_safe": False}],
            "candidate_market_samples": {"COMMODITY_PRICE_CANDIDATE": [{"ticker": "KXTEST"}]},
            "next_actions": [{"priority": 1}],
        },
    )

    artifacts = write_phase3az_gap_analysis_report(
        output_dir=reports_dir / "phase3az",
        reports_dir=reports_dir,
    )

    payload = json.loads(artifacts.json_path.read_text(encoding="utf-8"))
    gap_ids = {row["gap_id"] for row in payload["gaps"]}
    assert "general_domain_taxonomy_actionable" not in gap_ids
    assert payload["implementation_queue"] == []
    assert payload["summary"]["implementation_needed_count"] == 0
    assert (
        payload["source_reports"]["phase3bb_r2_source_intake"]
        == "reports/phase3bb_r2_sources/general_source_intake.json"
    )


def test_phase3az_cli_help() -> None:
    result = CliRunner().invoke(app, ["phase3az-gap-analysis", "--help"])

    assert result.exit_code == 0
    assert "phase3az-gap-analysis" in result.output


def test_phase3az_r11_selects_weather_as_fastest_non_crypto_lane(
    tmp_path,
    monkeypatch,
) -> None:
    monkeypatch.chdir(tmp_path)
    reports_dir = Path("reports")
    _write_json(
        reports_dir / "market_coverage" / "market_coverage_doctor.json",
        {
            "dashboard": {
                "category_rows": [
                    {
                        "category": "weather",
                        "parsed_markets": 96,
                        "linkable_markets": 96,
                        "linked_markets": 96,
                        "coverage_percent": "100.0%",
                        "unsupported_multileg_markets": 0,
                        "partial_markets": 0,
                        "status": "CONNECTED",
                    },
                    {
                        "category": "sports",
                        "parsed_markets": 56744,
                        "linkable_markets": 50319,
                        "linked_markets": 50319,
                        "coverage_percent": "100.0%",
                        "derived_markets": 51105,
                        "unsupported_multileg_markets": 6425,
                        "partial_markets": 0,
                        "status": "DERIVED_CONNECTED",
                    },
                    {
                        "category": "economic",
                        "parsed_markets": 0,
                        "linkable_markets": 0,
                        "linked_markets": 0,
                        "coverage_percent": "n/a",
                    },
                ]
            }
        },
    )
    _write_json(
        reports_dir / "phase3ah_sports" / "phase3ah_sports_placeholder_watch.json",
        {"summary": {"still_placeholder_rows": 6}},
    )
    _write_json(
        reports_dir / "phase3aw" / "dashboard_truth.json",
        {
            "summary": {
                "r5_running": True,
                "r5_stale_report": False,
                "paper_ready_candidates": 0,
                "current_positive_ev_rows": 4,
                "true_current_blocker": "LOW_EDGE_OR_SCORE_BLOCK",
            }
        },
    )

    artifacts = write_phase3az_r11_non_crypto_activation_report(
        output_dir=reports_dir / "phase3az_r11",
        reports_dir=reports_dir,
        weather_location_counts=[
            {"location_key": "new_york", "link_count": 264},
            {"location_key": "kansas_city", "link_count": 0},
        ],
    )

    payload = json.loads(artifacts.json_path.read_text(encoding="utf-8"))
    assert payload["summary"]["selected_category"] == "weather"
    assert payload["summary"]["selected_activation_state"] == "READY_FOR_WEATHER_ACTIVATION"
    assert payload["selected_category"]["activation_location_key"] == "new_york"
    assert payload["recommended_sprint"][0]["command"] == (
        "kalshi-bot ingest-weather --location-key new_york"
    )
    assert payload["recommended_sprint"][-1]["command"].startswith(
        "kalshi-bot phase3ap-paper-ready-unblock-report"
    )


def test_phase3az_r11_cli_help() -> None:
    result = CliRunner().invoke(app, ["phase3az-r11-non-crypto-category-activation", "--help"])

    assert result.exit_code == 0
    assert "phase3az-r11-non-crypto-category-activation" in result.output


def _write_reports(reports_dir: Path) -> None:
    _write_json(
        reports_dir / "phase3ay" / "phase3ay_health_refresh.json",
        {
            "summary": {
                "steps_error": 0,
                "due_or_overdue": 10,
                "eligible_exact_settlements": 2,
            }
        },
    )
    _write_json(
        reports_dir / "phase3ay" / "phase3ay_status.json",
        {"process": {"status": "STOPPED"}},
    )
    _write_json(
        reports_dir / "phase3aa" / "phase3aa_outcome_realizer.json",
        {
            "eligible_after_realize": 2,
            "eta_schedule": {"summary": {"due_or_overdue": 10}},
        },
    )
    _write_json(
        reports_dir / "phase3aa_r2" / "phase3aa_r2_exact_settlement_harvest.json",
        {
            "summary": {
                "exact_settlements_written": 0,
                "source_settled_without_usable_outcome": 7,
                "fetch_errors": 1,
            }
        },
    )
    _write_json(
        reports_dir
        / "paper_settlement_reconciliation"
        / "paper_settlement_reconciliation.json",
        {"summary": {"sibling_different_contract_leg": 1}},
    )
    _write_json(
        reports_dir / "market_coverage" / "market_coverage_doctor.json",
        {"coverage_rows": [{"scope": "sports", "health": "LINKER_DEGRADED"}]},
    )
    _write_json(
        reports_dir / "phase3ah_sports" / "phase3ah_sports_placeholder_watch.json",
        {"summary": {"still_placeholder_rows": 8}},
    )
    _write_json(
        reports_dir / "phase_orchestrator.json",
        {"evidence": {"sports_provenance": {"partial_without_upgrade": 20}}},
    )


def _write_clean_reports(reports_dir: Path) -> None:
    _write_json(
        reports_dir / "phase3ay" / "phase3ay_health_refresh.json",
        {"summary": {"steps_error": 0}},
    )
    _write_json(
        reports_dir / "phase3ay" / "phase3ay_status.json",
        {"process": {"status": "STOPPED"}},
    )
    _write_json(
        reports_dir / "phase3aa" / "phase3aa_outcome_realizer.json",
        {
            "eligible_after_realize": 0,
            "eta_schedule": {"summary": {"due_or_overdue": 0}},
        },
    )
    _write_json(
        reports_dir / "phase3aa_r2" / "phase3aa_r2_exact_settlement_harvest.json",
        {
            "summary": {
                "exact_settlements_written": 0,
                "source_settled_without_usable_outcome": 0,
                "fetch_errors": 0,
            }
        },
    )
    _write_json(
        reports_dir / "phase3aa_r3" / "phase3aa_r3_residual_settlement_audit.json",
        {"summary": {"residue_cleared": True, "residual_rows": 0}},
    )
    _write_json(
        reports_dir
        / "paper_settlement_reconciliation"
        / "paper_settlement_reconciliation.json",
        {"summary": {"eligible_to_settle_now": 0}},
    )
    _write_json(
        reports_dir / "market_coverage" / "market_coverage_doctor.json",
        {"coverage_rows": [{"scope": "general", "health": "HEALTHY"}]},
    )
    _write_json(
        reports_dir / "phase3ah_sports" / "phase3ah_sports_placeholder_watch.json",
        {"summary": {"still_placeholder_rows": 0}},
    )
    _write_json(
        reports_dir / "phase_orchestrator.json",
        {"evidence": {"sports_provenance": {"partial_without_upgrade": 0}}},
    )


def _write_json(path: Path, payload: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload), encoding="utf-8")
