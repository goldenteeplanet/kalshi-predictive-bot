from datetime import timedelta
from decimal import Decimal
from pathlib import Path

from fastapi.testclient import TestClient
from typer.testing import CliRunner

from kalshi_predictor.cli import app
from kalshi_predictor.config import Settings, get_settings
from kalshi_predictor.data.db import get_session_factory, init_db
from kalshi_predictor.data.repositories import insert_forecast, insert_market_snapshot
from kalshi_predictor.forecasting.base import ForecastOutput
from kalshi_predictor.institutional_dashboard.contracts import API_SCHEMA_VERSION
from kalshi_predictor.institutional_dashboard.reports import (
    generate_institutional_dashboard_report,
)
from kalshi_predictor.institutional_dashboard.service import (
    build_dashboard_snapshot,
    export_snapshot_csv,
    sanitize_csv_cell,
)
from kalshi_predictor.opportunities.repository import insert_market_ranking
from kalshi_predictor.professional_ux.service import build_shell_context
from kalshi_predictor.ui.app import create_app
from kalshi_predictor.ui.routes import create_router
from kalshi_predictor.utils.time import utc_now


def test_phase_3t_empty_snapshot_marks_sources_unknown_not_zero(tmp_path) -> None:
    session_factory = _session_factory(tmp_path)
    with session_factory() as session:
        snapshot = build_dashboard_snapshot(session, settings=_settings())

    required = {row["source_id"]: row for row in snapshot["source_watermarks"] if row["required"]}

    assert snapshot["schema_version"] == "phase-3t-dashboard-snapshot-v1"
    assert snapshot["dashboard_mode"] == "READ_ONLY_SHADOW"
    assert snapshot["completeness_status"] == "UNAVAILABLE"
    assert required["market_state"]["latest_at"] is None
    assert required["market_state"]["freshness_status"] == "UNKNOWN"
    assert required["market_state"]["row_count"] == 0
    assert "market_state has no rows." in snapshot["warnings"]
    assert snapshot["source_statuses"] == snapshot["source_watermarks"]
    assert required["market_state"]["database_fingerprint"].startswith("sha256:")
    assert required["market_state"]["git_commit"]
    optional = {
        row["source_id"]: row
        for row in snapshot["source_watermarks"]
        if not row["required"]
    }
    assert optional["phase_3p_self_evaluation"]["lifecycle_state"] == "DISABLED"
    assert optional["phase_3p_self_evaluation"]["completeness_status"] == "NOT_APPLICABLE"
    assert snapshot["read_only_boundary"]["allow_order_actions"] is False


def test_phase_3t_snapshot_panels_and_reconciliation(tmp_path) -> None:
    session_factory = _session_factory(tmp_path)
    _seed_market(session_factory)
    with session_factory() as session:
        snapshot = build_dashboard_snapshot(session, settings=_settings())
        report = generate_institutional_dashboard_report(
            session,
            output_path=Path(tmp_path) / "institutional.md",
            settings=_settings(),
        )

    panel_ids = {panel["panel_id"] for panel in snapshot["panel_registry"]}

    assert "market_heatmap" in panel_ids
    assert "opportunity_scanner" in panel_ids
    assert "model_matrix" in panel_ids
    assert "risk_waterfall" in panel_ids
    assert snapshot["panels"]["opportunity_scanner"][0]["ticker"] == "P3T-TEST"
    assert snapshot["panels"]["research_layers"]["phase_3r"]["order_actions_allowed"] is False
    assert snapshot["panels"]["research_layers"]["phase_3s"]["phase_3s_action_is_order"] is False
    assert snapshot["reconciliation"]["status"] == "PASS"
    assert "Read-Only Proof" in report.read_text(encoding="utf-8")


def test_phase_3t_cross_panel_skew_degrades_consistency(tmp_path) -> None:
    session_factory = _session_factory(tmp_path)
    _seed_market(session_factory, market_age=timedelta(hours=2))
    with session_factory() as session:
        snapshot = build_dashboard_snapshot(
            session,
            settings=_settings(phase_3t_max_source_skew_seconds=30),
        )

    assert snapshot["cross_panel_skew"]["status"] == "DEGRADED"
    assert snapshot["consistency_mode"] == "CONSISTENCY_DEGRADED"
    assert snapshot["consistency_mode"] != "WATERMARK_ALIGNED"
    assert snapshot["cross_panel_skew"]["unit"] == "seconds"
    assert snapshot["cross_panel_skew"]["oldest_source_id"] == "market_state"
    assert snapshot["cross_panel_skew"]["newest_source_id"]
    assert snapshot["cross_panel_skew"]["source_pair"]["oldest_source_id"] == "market_state"


def test_phase_3t_shell_reuses_snapshot_timestamp(tmp_path) -> None:
    session_factory = _session_factory(tmp_path)
    _seed_market(session_factory)
    with session_factory() as session:
        snapshot = build_dashboard_snapshot(session, settings=_settings())
        shell = build_shell_context(session, settings=_settings(), snapshot=snapshot)

    assert shell["snapshot_as_of"] == snapshot["generated_at"]
    assert shell["market_freshness"]["label"] != "Unknown"
    assert shell["database_fingerprint"] == snapshot["runtime_context"]["database_fingerprint"]


def test_phase_3t_ui_and_api_contracts(tmp_path) -> None:
    session_factory = _session_factory(tmp_path)
    _seed_market(session_factory)
    client = TestClient(create_app(session_factory=session_factory, settings=_settings()))

    page = client.get("/institutional")
    current = client.get("/api/dashboard/v1/snapshots/current")
    opportunities = client.post(
        "/api/dashboard/v1/query/opportunities",
        json={"schema_version": API_SCHEMA_VERSION, "filters": {"model_id": "ensemble_v2"}},
    )
    bad_schema = client.post(
        "/api/dashboard/v1/query/snapshot",
        json={"schema_version": "wrong-version"},
    )
    stream = client.get("/api/dashboard/v1/stream")

    assert page.status_code == 200
    assert "Institutional Cockpit" in page.text
    assert "paper-trade" not in page.text
    assert "demo-execute" not in page.text
    assert current.status_code == 200
    assert current.json()["schema_version"] == API_SCHEMA_VERSION
    assert current.json()["read_only_boundary"]["read_only"] is True
    assert current.json()["data"]["read_only"] is True
    assert opportunities.status_code == 200
    assert opportunities.json()["data"][0]["ticker"] == "P3T-TEST"
    assert bad_schema.status_code == 400
    assert bad_schema.json()["detail"]["code"] == "DASHBOARD_SCHEMA_UNSUPPORTED"
    assert stream.status_code == 200
    assert "event: snapshot" in stream.text


def test_phase_3t_csv_export_sanitizes_formula_cells(tmp_path) -> None:
    session_factory = _session_factory(tmp_path)
    with session_factory() as session:
        snapshot = build_dashboard_snapshot(session, settings=_settings())

    csv_payload = export_snapshot_csv(snapshot)

    assert "panel_id,title,freshness_status" in csv_payload
    assert sanitize_csv_cell("=HYPERLINK(\"x\")").startswith("'=")
    assert sanitize_csv_cell("@cmd").startswith("'@")


def test_phase_3t_routes_are_read_only_and_cli_smoke(tmp_path) -> None:
    router = create_router(session_factory=_session_factory(tmp_path), settings=_settings())
    dashboard_routes = [
        route
        for route in router.routes
        if getattr(route, "path", "").startswith("/api/dashboard/v1")
        or getattr(route, "path", "") == "/institutional"
    ]

    for route in dashboard_routes:
        path = route.path
        assert "paper-trade" not in path
        assert "demo-execute" not in path
        if "POST" in getattr(route, "methods", set()):
            assert "/query/" in path

    runner = CliRunner()
    for command in (
        "institutional-dashboard-status",
        "institutional-dashboard-report",
        "institutional-dashboard-export",
    ):
        result = runner.invoke(app, [command, "--help"])
        assert result.exit_code == 0
        assert "Usage" in result.output

    get_settings.cache_clear()
    output = Path(tmp_path) / "institutional.md"
    result = runner.invoke(
        app,
        [
            "institutional-dashboard-report",
            "--enable-read-only",
            "--output",
            str(output),
        ],
        env={"DATABASE_URL": f"sqlite:///{Path(tmp_path) / 'cli.db'}"},
    )
    get_settings.cache_clear()

    assert result.exit_code == 0
    assert output.exists()
    assert "Phase 3T Institutional Dashboard Report" in output.read_text(encoding="utf-8")


def _settings(**overrides) -> Settings:
    return Settings(
        phase_3t_institutional_dashboard_enabled=True,
        phase_3t_mode="read_only_shadow",
        phase_3t_fresh_after_seconds=3600,
        phase_3t_stale_after_seconds=7200,
        phase_3t_max_rows_per_panel=25,
        **overrides,
    )


def _session_factory(tmp_path):
    engine = init_db(f"sqlite:///{Path(tmp_path) / 'phase3t.db'}")
    return get_session_factory(engine)


def _seed_market(session_factory, *, market_age: timedelta | None = None) -> None:
    with session_factory() as session:
        now = utc_now()
        market_time = now - (market_age or timedelta())
        snapshot = insert_market_snapshot(
            session,
            {
                "ticker": "P3T-TEST",
                "status": "open",
                "title": "Will the Phase 3T institutional dashboard render?",
                "series_ticker": "KXTEST",
                "event_ticker": "KXTEST-EVENT",
                "close_time": (now + timedelta(hours=3)).isoformat(),
                "volume_fp": "1000",
                "open_interest_fp": "500",
                "liquidity_dollars": "12000",
            },
            {
                "orderbook_fp": {
                    "yes_dollars": [["0.42", "20"]],
                    "no_dollars": [["0.48", "20"]],
                }
            },
            market_time,
        )
        insert_forecast(
            session,
            ForecastOutput(
                ticker="P3T-TEST",
                forecasted_at=now,
                model_name="ensemble_v2",
                yes_probability=Decimal("0.66"),
                market_mid_probability=None,
                best_yes_bid=Decimal("0.42"),
                best_yes_ask=Decimal(snapshot.best_yes_ask),
                feature_json={"source": "phase3t_test"},
            ),
        )
        insert_market_ranking(
            session,
            {
                "ticker": "P3T-TEST",
                "ranked_at": now,
                "title": "Will the Phase 3T institutional dashboard render?",
                "status": "open",
                "forecast_model": "ensemble_v2",
                "market_probability": "0.45",
                "forecast_probability": "0.66",
                "best_side": "BUY_YES",
                "best_price": "0.48",
                "estimated_edge": "0.18",
                "liquidity_score": "85",
                "spread_score": "90",
                "time_score": "80",
                "model_confidence_score": "82",
                "opportunity_score": "88",
                "spread": "0.06",
                "time_to_close_minutes": "180",
                "reason": "Seeded Phase 3T test opportunity.",
            },
        )
        session.commit()
