from __future__ import annotations

import json
from datetime import timedelta
from pathlib import Path

from typer.testing import CliRunner

from kalshi_predictor.cli import app
from kalshi_predictor.config import get_settings
from kalshi_predictor.data.db import get_session_factory, init_db
from kalshi_predictor.data.repositories import upsert_market
from kalshi_predictor.phase3ay_free_sources import (
    build_free_source_adapter_registry,
    build_phase3ay_category_readiness,
    build_phase3ay_free_source_market_scan,
    write_phase3ay_free_source_sprint_report,
)
from kalshi_predictor.ui.service import free_source_hunt_status
from kalshi_predictor.utils.time import utc_now


def test_free_source_registry_defers_tradingeconomics() -> None:
    registry = build_free_source_adapter_registry()

    tradingeconomics = [
        row for row in registry["adapters"] if row["adapter_key"] == "tradingeconomics_deferred"
    ][0]

    assert tradingeconomics["parser_status"] == "PAID_SOURCE_DEFERRED"
    assert tradingeconomics["free_or_paid"] == "paid_or_restricted"
    assert "PAID_SOURCE_DEFERRED" in tradingeconomics["blockers"]


def test_free_source_scan_excludes_expired_and_classifies_current_categories(tmp_path) -> None:
    session_factory = _session_factory(tmp_path)
    now = utc_now()

    with session_factory() as session:
        _seed_market(
            session,
            ticker="KXRAIN-30JAN01-CHI",
            title="Will it rain in Chicago?",
            close_time=now + timedelta(days=1),
        )
        _seed_market(
            session,
            ticker="KXBTC-26JUL0809-B61750",
            title="Bitcoin above 61750",
            close_time=now - timedelta(hours=1),
        )
        payload = build_phase3ay_free_source_market_scan(
            session,
            reports_dir=tmp_path / "reports",
            limit=5000,
        )

    tickers = {row["ticker"] for row in payload["candidate_rows"]}
    rain_row = payload["candidate_rows"][0]
    assert tickers == {"KXRAIN-30JAN01-CHI"}
    assert rain_row["category"] == "weather"
    assert rain_row["main_blocker"] == "KALSHI_LINK_UNVERIFIED"
    assert payload["summary"]["paper_ready_rows"] == 0


def test_category_readiness_prefers_non_crypto_free_source(tmp_path) -> None:
    session_factory = _session_factory(tmp_path)
    now = utc_now()

    with session_factory() as session:
        _seed_market(
            session,
            ticker="KXBTC-30JAN01-B100000",
            title="Bitcoin above 100000",
            close_time=now + timedelta(days=1),
            series_ticker="KXBTC",
        )
        _seed_market(
            session,
            ticker="KXCORN-30JAN01-HIGH",
            title="Will USDA corn production exceed expectations?",
            close_time=now + timedelta(days=1),
            series_ticker="KXCORN",
        )
        payload = build_phase3ay_category_readiness(
            session,
            reports_dir=tmp_path / "reports",
            limit=5000,
        )

    assert payload["summary"]["best_next_category"] == "agriculture_commodities"
    assert payload["next_category_sprint"]["task_phase_name"] == (
        "Phase 3AY-R1 USDA/EIA Commodity Linker Sprint"
    )


def test_free_source_sprint_report_writes_required_artifacts_and_no_trades(tmp_path) -> None:
    session_factory = _session_factory(tmp_path)
    reports_dir = tmp_path / "reports"

    with session_factory() as session:
        _seed_market(
            session,
            ticker="KXFAA-30JAN01-ORD",
            title="Will more than 100 flights be cancelled at ORD?",
            close_time=utc_now() + timedelta(days=1),
            series_ticker="KXFAA",
        )
        artifacts = write_phase3ay_free_source_sprint_report(
            session,
            output_dir=reports_dir / "phase3ay",
            reports_dir=reports_dir,
            registered_commands={
                "phase3ay-free-source-market-scan",
                "phase3ay-category-readiness",
                "phase3ay-multicategory-paper-funnel",
                "phase3ay-free-source-sprint-report",
            },
        )

    payload = json.loads(artifacts.sprint_report_json_path.read_text(encoding="utf-8"))
    assert artifacts.executive_summary_path.exists()
    assert artifacts.free_source_market_candidates_path.exists()
    assert artifacts.category_scorecard_path.exists()
    assert artifacts.manifest_path.exists()
    assert payload["paper_trade_creation"] is False
    assert payload["live_or_demo_execution"] is False
    assert payload["summary"]["paper_ready_rows"] == 0
    assert payload["command_registry_audit"]["missing_command_names"] == []


def test_free_source_hunt_status_reads_sprint_report(tmp_path) -> None:
    report_path = tmp_path / "free_source_sprint_report.json"
    report_path.write_text(
        json.dumps(
            {
                "generated_at": "2030-01-01T00:00:00+00:00",
                "summary": {
                    "best_next_category": "weather",
                    "next_codex_sprint": "Phase 3AY-R1 Weather Free Source Exact Linker Sprint",
                    "first_hard_blocker": "KALSHI_LINK_UNVERIFIED",
                    "operator_next_command": "kalshi-bot phase3ay-free-source-sprint-report",
                    "markets_scanned": 3,
                    "positive_ev_rows": 0,
                    "paper_ready_rows": 0,
                },
                "category_readiness": {
                    "category_scorecard": [
                        {
                            "category": "weather",
                            "sprint_score": 42,
                            "current_active_markets": 3,
                            "free_source_available_rows": 3,
                            "linked_rows": 0,
                            "forecast_ready_rows": 0,
                            "book_ready_rows": 0,
                            "positive_ev_rows": 0,
                            "paper_ready_rows": 0,
                            "top_blocker": "KALSHI_LINK_UNVERIFIED",
                            "next_action": "Build exact weather linker.",
                        }
                    ]
                },
            }
        ),
        encoding="utf-8",
    )

    status = free_source_hunt_status(report_path=report_path)

    assert status["best_next_category"] == "weather"
    assert status["first_hard_blocker"] == "KALSHI_LINK_UNVERIFIED"
    assert status["rows"][0]["category"] == "weather"


def test_phase3ay_free_source_cli_commands_registered() -> None:
    for command in (
        "phase3ay-free-source-market-scan",
        "phase3ay-category-readiness",
        "phase3ay-multicategory-paper-funnel",
        "phase3ay-free-source-sprint-report",
    ):
        result = CliRunner().invoke(app, [command, "--help"])
        assert result.exit_code == 0
        assert "--output-dir" in result.output


def _session_factory(tmp_path: Path):
    get_settings.cache_clear()
    engine = init_db(f"sqlite:///{Path(tmp_path) / 'phase3ay_free_sources.db'}")
    return get_session_factory(engine)


def _seed_market(
    session,
    *,
    ticker: str,
    title: str,
    close_time,
    status: str = "open",
    series_ticker: str | None = None,
) -> None:
    upsert_market(
        session,
        {
            "ticker": ticker,
            "title": title,
            "event_ticker": ticker.rsplit("-", 1)[0],
            "series_ticker": series_ticker or ticker.split("-", 1)[0],
            "status": status,
            "close_time": close_time.isoformat(),
            "market_type": "binary",
        },
    )
    session.flush()
