import json
from pathlib import Path

from typer.testing import CliRunner

from kalshi_predictor.cli import app
from kalshi_predictor.config import get_settings
from kalshi_predictor.data.db import get_session_factory, init_db
from kalshi_predictor.data.repositories import upsert_market
from kalshi_predictor.data.schema import MarketLeg
from kalshi_predictor.phase3z import (
    build_market_coverage_doctor,
    build_model_repair_audit,
    write_market_coverage_doctor,
    write_model_repair_audit,
)


def test_phase3z_audit_keeps_undefined_metrics_null(tmp_path) -> None:
    session_factory = _session_factory(tmp_path)

    with session_factory() as session:
        audit = build_model_repair_audit(session)

    market_implied = _model(audit, "market_implied_v1")
    assert market_implied["role"] == "BENCHMARK"
    assert market_implied["health_state"] in {"NEEDS_RAW_MARKET_DATA", "BENCHMARK_ONLY"}
    assert market_implied["forecast_metrics"]["evaluated_count"] == 0
    assert market_implied["forecast_metrics"]["brier_score"] is None
    assert market_implied["paper_trade_metrics"]["roi"] is None
    assert market_implied["paper_trade_metrics"]["win_rate"] is None
    assert audit["runtime_identity"]["sqlite"]["path"].endswith("phase3z.db")


def test_phase3z_coverage_doctor_uses_null_for_empty_denominator(tmp_path) -> None:
    session_factory = _session_factory(tmp_path)

    with session_factory() as session:
        doctor = build_market_coverage_doctor(session)

    crypto = next(row for row in doctor["coverage_rows"] if row["scope_key"] == "crypto")
    assert crypto["coverage_denominator"] == 0
    assert crypto["coverage"] is None
    assert crypto["health"] in {"NO_CATALOG_DATA", "NO_COMPATIBLE_ACTIVE_MARKETS"}


def test_phase3z_audit_writes_json_and_markdown(tmp_path) -> None:
    session_factory = _session_factory(tmp_path)
    output_dir = Path(tmp_path) / "model_repair"

    with session_factory() as session:
        artifacts = write_model_repair_audit(session, output_dir=output_dir)

    payload = json.loads(artifacts.json_path.read_text(encoding="utf-8"))
    markdown = artifacts.markdown_path.read_text(encoding="utf-8")
    assert payload["paper_only_safety"] == "PAPER_ONLY_NO_EXCHANGE_WRITES"
    assert "Phase 3Z Model Repair Audit" in markdown
    assert "—" in markdown


def test_market_coverage_doctor_runs_parser_before_reporting(tmp_path) -> None:
    session_factory = _session_factory(tmp_path)
    output_dir = Path(tmp_path) / "coverage"

    with session_factory() as session:
        upsert_market(
            session,
            {
                "ticker": "KXBTC-PRICE-TEST",
                "title": "yes Target Price: $62,000",
                "series_ticker": "KXMVECROSSCATEGORY",
                "status": "open",
            },
        )

        artifacts = write_market_coverage_doctor(session, output_dir=output_dir)
        leg_count = session.query(MarketLeg).count()
        rerun = build_market_coverage_doctor(session)

    payload = json.loads(artifacts.json_path.read_text(encoding="utf-8"))
    markdown = artifacts.markdown_path.read_text(encoding="utf-8")
    assert payload["parse_result"]["markets_scanned"] == 1
    assert payload["stage_counts"]["parse_attempts"] == 1
    assert payload["stage_counts"]["parsed_markets"] == 1
    assert payload["stage_counts"]["parse_failures"] == 0
    assert rerun["parse_result"]["markets_skipped_existing"] == 1
    assert rerun["parse_result"]["existing_markets_with_legs"] == 1
    assert rerun["stage_counts"]["parse_failures"] == 0
    assert leg_count == 1
    assert "Parser Pass" in markdown


def test_market_coverage_doctor_counts_active_status_as_current(tmp_path) -> None:
    session_factory = _session_factory(tmp_path)

    with session_factory() as session:
        upsert_market(
            session,
            {
                "ticker": "KXBTC-ACTIVE-CURRENT",
                "title": "yes Target Price: $62,000",
                "series_ticker": "KXBTC",
                "status": "active",
                "close_time": "2100-01-01T00:00:00+00:00",
            },
        )
        doctor = build_market_coverage_doctor(session, deep_checks=False)

    crypto = next(row for row in doctor["coverage_rows"] if row["scope_key"] == "crypto")
    assert doctor["stage_counts"]["active_eligible_markets"] == 1
    assert crypto["current_parsed_markets"] == 1
    assert crypto["current_unlinked_markets"] == 1
    assert crypto["health"] == "LINKER_NOT_RUN"


def test_market_coverage_doctor_cli_defaults_to_fast_bounded_refresh(tmp_path) -> None:
    get_settings.cache_clear()
    runner = CliRunner()
    db_url = f"sqlite:///{Path(tmp_path) / 'phase3z_fast_cli.db'}"
    output_dir = Path(tmp_path) / "coverage"

    result = runner.invoke(
        app,
        ["market-coverage-doctor", "--output-dir", str(output_dir)],
        env={"KALSHI_DB_URL": db_url},
    )

    get_settings.cache_clear()
    assert result.exit_code == 0
    payload = json.loads((output_dir / "market_coverage_doctor.json").read_text())
    assert payload["refresh_mode"] == "FAST_BOUNDED"
    assert payload["bounded_refresh"]["deep_checks"] is False
    assert payload["bounded_refresh"]["detail_exports"] == "BOUNDED_EXAMPLES"
    assert payload["parse_result"] is None
    assert payload["stage_counts"]["orphan_link_check"] == "SKIPPED_FAST_REFRESH"


def test_phase3z_cli_smoke(tmp_path) -> None:
    get_settings.cache_clear()
    runner = CliRunner()
    db_url = f"sqlite:///{Path(tmp_path) / 'phase3z_cli.db'}"
    output_dir = Path(tmp_path) / "phase3z_reports"

    audit = runner.invoke(
        app,
        ["model-repair-audit", "--output-dir", str(output_dir)],
        env={"KALSHI_DB_URL": db_url},
    )
    doctor = runner.invoke(
        app,
        ["market-coverage-doctor", "--output-dir", str(output_dir / "coverage")],
        env={"KALSHI_DB_URL": db_url},
    )

    get_settings.cache_clear()
    assert audit.exit_code == 0
    assert doctor.exit_code == 0
    assert (output_dir / "model_repair_audit.json").exists()
    assert (output_dir / "coverage" / "market_coverage_doctor.json").exists()


def _session_factory(tmp_path):
    engine = init_db(f"sqlite:///{Path(tmp_path) / 'phase3z.db'}")
    return get_session_factory(engine)


def _model(audit: dict, model_name: str) -> dict:
    return next(row for row in audit["models"] if row["model_name"] == model_name)
