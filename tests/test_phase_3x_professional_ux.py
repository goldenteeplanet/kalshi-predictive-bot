from datetime import timedelta
from pathlib import Path

from fastapi.testclient import TestClient
from typer.testing import CliRunner

from kalshi_predictor.cli import app, build_phase_status
from kalshi_predictor.config import Settings, get_settings
from kalshi_predictor.data.db import get_session_factory, init_db
from kalshi_predictor.data.repositories import insert_market_snapshot
from kalshi_predictor.opportunities.repository import insert_market_ranking
from kalshi_predictor.professional_ux.contracts import (
    BOUNDARY_ASSERTIONS,
    DECISION_INCOMPLETE,
    ROUTE_INVENTORY,
    STATUS_GRAMMAR,
)
from kalshi_predictor.professional_ux.reports import (
    generate_phase_3x_report,
    phase_3x_card,
)
from kalshi_predictor.professional_ux.service import (
    build_shell_context,
    load_shell_status_context,
    write_shell_status_snapshot,
)
from kalshi_predictor.ui.app import create_app
from kalshi_predictor.utils.time import utc_now


def test_phase_3x_contracts_define_professional_shell_boundaries() -> None:
    assert len(ROUTE_INVENTORY) >= 10
    assert STATUS_GRAMMAR["stale"]["label"] == "Stale"
    assert STATUS_GRAMMAR["no_trade"]["label"] == "No trade"
    assert all(item["passed"] for item in BOUNDARY_ASSERTIONS)
    assert "kalshi_predictor.professional_ux" in {
        phase["module"] for phase in build_phase_status()["phases"]
    }


def test_phase_3x_status_and_report_are_incomplete_without_3w_pass(tmp_path) -> None:
    session_factory = _session_factory(tmp_path)
    output_dir = Path(tmp_path) / "phase3x"
    with session_factory() as session:
        status = phase_3x_card(session, settings=_settings(tmp_path))
        result = generate_phase_3x_report(
            session,
            output_dir=output_dir,
            settings=_settings(tmp_path),
        )

    assert status["decision"] == DECISION_INCOMPLETE
    assert status["live_trading_authorized"] is False
    assert result["decision"] == DECISION_INCOMPLETE
    assert output_dir.joinpath("UI_UX_AUDIT.md").exists()
    assert output_dir.joinpath("COMPONENT_CATALOG.md").exists()
    assert "THIS REPORT DOES NOT AUTHORIZE LIVE TRADING" in output_dir.joinpath(
        "PHASE_3X_REPORT.md"
    ).read_text(encoding="utf-8")


def test_phase_3x_today_route_renders_authority_labels(tmp_path) -> None:
    session_factory = _session_factory(tmp_path)
    _seed_opportunity(session_factory)
    client = TestClient(
        create_app(session_factory=session_factory, settings=_settings(tmp_path))
    )

    response = client.get("/today")

    assert response.status_code == 200
    assert "Market probability" in response.text
    assert "Model probability" in response.text
    assert "3S ROI gate" in response.text
    assert "Bounded portfolio view" in response.text
    assert "demo-execute" not in response.text
    assert "Live trading authorized" not in response.text


def test_phase_3x_root_uses_bounded_today_workspace(tmp_path) -> None:
    session_factory = _session_factory(tmp_path)
    _seed_opportunity(session_factory)
    client = TestClient(
        create_app(session_factory=session_factory, settings=_settings(tmp_path))
    )

    response = client.get("/")

    assert response.status_code == 200
    assert "Professional cockpit" in response.text
    assert "Market probability" in response.text
    assert "Crypto Freshness Watch" in response.text
    assert "Paper-Only Soak" in response.text
    assert "GH-4 locked" in response.text
    assert "Paper-ready" in response.text
    assert "Progress" in response.text
    assert "ETA" in response.text
    assert "Advanced Risk Engine" not in response.text
    assert "Bounded portfolio view" in response.text


def test_phase_3x_opportunities_route_uses_scanner_shell(tmp_path) -> None:
    session_factory = _session_factory(tmp_path)
    _seed_opportunity(session_factory)
    client = TestClient(
        create_app(session_factory=session_factory, settings=_settings(tmp_path))
    )

    response = client.get("/opportunities")

    assert response.status_code == 200
    assert "Opportunity Scanner" in response.text
    assert "Best Opportunities Right Now" in response.text
    assert "Today&#39;s Summary" not in response.text
    assert "Paper Health" in response.text
    assert "Market Data" in response.text
    assert "Prod Cert" in response.text
    assert "Live Ready" in response.text
    assert "Crypto Freshness Watch" in response.text
    assert "Fast bounded view" in response.text
    assert "Page generated" in response.text
    assert "UX-TEST" in response.text
    assert "styles.css?v=phase3bb-r2-model-layout-20260704a" in response.text
    assert "app.js?v=phase3bb-r2-model-layout-20260704a" in response.text
    assert response.headers["cache-control"] == "no-store"


def test_phase_3x_opportunities_route_lists_fast_bounded_rows(tmp_path) -> None:
    session_factory = _session_factory(tmp_path)
    _seed_opportunity(session_factory)
    _seed_stale_opportunity(session_factory)
    client = TestClient(
        create_app(session_factory=session_factory, settings=_settings(tmp_path))
    )

    response = client.get("/opportunities")

    assert response.status_code == 200
    assert "Fast bounded view" in response.text
    assert "UX-TEST" in response.text
    assert "UX-STALE" in response.text


def test_phase_3x_opportunities_route_explains_blocked_rows(tmp_path) -> None:
    session_factory = _session_factory(tmp_path)
    _seed_blocked_crypto_opportunity(session_factory)
    client = TestClient(
        create_app(session_factory=session_factory, settings=_settings(tmp_path))
    )

    response = client.get("/opportunities")

    assert response.status_code == 200
    assert "Blocked Research" in response.text
    assert "Below ready score" in response.text
    assert "Score 9.41 is below the 60 ready filter" in response.text
    assert "KXDOGE-26JUL1017-B0.082" in response.text
    assert "Resolve evidence" not in response.text
    assert "9.414015548343460648148148148" not in response.text


def test_phase_3x_models_route_uses_live_shell_status(tmp_path) -> None:
    session_factory = _session_factory(tmp_path)
    _seed_opportunity(session_factory)
    client = TestClient(
        create_app(session_factory=session_factory, settings=_settings(tmp_path))
    )

    response = client.get("/models")

    assert response.status_code == 200
    assert "Model Status" in response.text
    assert "Paper Health" in response.text
    assert "Market Data" in response.text
    assert "UNK Unknown" not in response.text
    assert "PAPER / READ-ONLY" in response.text


def test_phase_3x_system_page_and_api_render(tmp_path) -> None:
    client = TestClient(
        create_app(session_factory=_session_factory(tmp_path), settings=_settings(tmp_path))
    )

    page = client.get("/system")
    api = client.get("/api/phase3x/status")
    monitor_api = client.get("/api/db-writer-monitor")
    alias = client.get("/system/certification", follow_redirects=False)

    assert page.status_code == 200
    assert "Health and Release Readiness" in page.text
    assert "DB Writer / Long Job Monitor" in page.text
    assert "Route Inventory" in page.text
    assert "INCOMPLETE" in page.text
    assert api.status_code == 200
    assert api.json()["status"]["decision"] == DECISION_INCOMPLETE
    assert api.json()["live_trading_authorized"] is False
    assert monitor_api.status_code == 200
    assert monitor_api.json()["read_only"] is True
    assert "safe_to_start_write" in monitor_api.json()["monitor"]
    assert alias.status_code == 307
    assert alias.headers["location"] == "/system-certification"


def test_phase_3x_shell_context_exposes_global_status(tmp_path) -> None:
    session_factory = _session_factory(tmp_path)
    _seed_opportunity(session_factory)
    with session_factory() as session:
        shell = build_shell_context(session, settings=_settings(tmp_path))

    assert shell["environment"] == "DEMO"
    assert shell["execution_mode"] == "PAPER / READ-ONLY"
    assert shell["paper_runtime"]["label"] == "Healthy"
    assert shell["system_status"]["label"] == "Healthy"
    assert shell["phase_3x"]["decision"] == DECISION_INCOMPLETE
    assert shell["phase_3w"]["label"] != "3W PASS"
    assert shell["phase_3v"]["label"] != "3V GO"
    assert shell["command_palette_enabled"] is True
    assert shell["market_freshness"]["label"] in {"Fresh", "Stale"}
    assert shell["market_freshness"]["age_label"]


def test_phase_3x_shell_status_snapshot_loads_without_live_dashboard(tmp_path) -> None:
    session_factory = _session_factory(tmp_path)
    _seed_opportunity(session_factory)
    snapshot_path = Path(tmp_path) / "shell_status_snapshot.json"
    with session_factory() as session:
        result = write_shell_status_snapshot(
            session,
            output_path=snapshot_path,
            settings=_settings(tmp_path),
        )

    shell = load_shell_status_context(
        snapshot_path=snapshot_path,
        settings=_settings(tmp_path),
    )

    assert Path(result["path"]).exists()
    assert result["payload"]["full_dashboard_snapshot_used"] is False
    assert shell["paper_runtime"]["label"] == "Healthy"
    assert shell["system_status"]["label"] == "Healthy"
    assert shell["market_freshness"]["label"] in {"Fresh", "Stale"}
    assert shell["market_freshness"]["age_label"]
    assert shell["snapshot_as_of_label"]
    assert shell["shell_status_snapshot"]["freshness_status"] in {"FRESH", "STALE"}


def test_phase_3x_cli_smoke_and_report(tmp_path) -> None:
    runner = CliRunner()
    for command in (
        "phase3x-status",
        "phase3x-report",
        "phase3x-audit",
        "ui-shell-status-refresh",
    ):
        result = runner.invoke(app, [command, "--help"])
        assert result.exit_code == 0
        assert "Usage" in result.output

    get_settings.cache_clear()
    output_dir = Path(tmp_path) / "cli_phase3x"
    result = runner.invoke(
        app,
        [
            "phase3x-report",
            "--enable-preview",
            "--output-dir",
            str(output_dir),
        ],
        env={"DATABASE_URL": f"sqlite:///{Path(tmp_path) / 'phase3x_cli.db'}"},
    )
    get_settings.cache_clear()

    assert result.exit_code == 0
    assert output_dir.joinpath("UI_UX_AUDIT.md").exists()
    assert "Live trading authorized: false" in result.output

    shell_snapshot_path = Path(tmp_path) / "shell_status_snapshot.json"
    result = runner.invoke(
        app,
        [
            "ui-shell-status-refresh",
            "--output-path",
            str(shell_snapshot_path),
        ],
        env={"DATABASE_URL": f"sqlite:///{Path(tmp_path) / 'phase3x_cli.db'}"},
    )
    get_settings.cache_clear()

    assert result.exit_code == 0
    assert shell_snapshot_path.exists()
    assert "UI shell status snapshot refreshed" in result.output
    assert "Live/demo execution: blocked" in result.output


def _settings(tmp_path) -> Settings:
    return Settings(
        phase_3x_professional_ux_enabled=True,
        phase_3x_mode="preview",
        phase_3x_output_dir=str(Path(tmp_path) / "phase3x"),
        phase_3w_system_certification_enabled=True,
        phase_3t_institutional_dashboard_enabled=True,
        phase_3t_mode="read_only_shadow",
        execution_enabled=False,
        execution_dry_run=True,
    )


def _session_factory(tmp_path):
    engine = init_db(f"sqlite:///{Path(tmp_path) / 'phase3x.db'}")
    return get_session_factory(engine)


def _seed_opportunity(session_factory) -> None:
    with session_factory() as session:
        captured_at = utc_now()
        insert_market_snapshot(
            session,
            {
                "ticker": "UX-TEST",
                "status": "open",
                "title": "Will this UX market resolve yes?",
                "rules_primary": "This is a local UX test market.",
                "yes_bid_dollars": "0.40",
                "yes_ask_dollars": "0.50",
                "liquidity_dollars": "100",
            },
            {
                "orderbook_fp": {
                    "yes_dollars": [["0.40", "10"]],
                    "no_dollars": [["0.50", "10"]],
                }
            },
            captured_at,
        )
        insert_market_ranking(
            session,
            {
                "ticker": "UX-TEST",
                "ranked_at": captured_at,
                "title": "Will this UX market resolve yes?",
                "status": "open",
                "forecast_model": "market_implied_v1",
                "forecast_probability": "0.60",
                "best_side": "BUY_YES",
                "best_price": "0.50",
                "estimated_edge": "0.10",
                "liquidity_score": "80",
                "spread_score": "90",
                "time_score": "70",
                "model_confidence_score": "60",
                "opportunity_score": "75",
                "spread": "0.10",
                "time_to_close_minutes": "120",
                "reason": "Seeded UX test opportunity.",
            },
        )
        session.commit()


def _seed_stale_opportunity(session_factory) -> None:
    with session_factory() as session:
        captured_at = utc_now() - timedelta(minutes=90)
        ranked_at = utc_now()
        insert_market_snapshot(
            session,
            {
                "ticker": "UX-STALE",
                "status": "open",
                "title": "Stale Bitcoin Price Market",
                "rules_primary": "This is a stale local UX test market.",
                "yes_bid_dollars": "0.40",
                "yes_ask_dollars": "0.42",
                "liquidity_dollars": "100",
            },
            {
                "orderbook_fp": {
                    "yes_dollars": [["0.40", "10"]],
                    "no_dollars": [["0.58", "10"]],
                }
            },
            captured_at,
        )
        insert_market_ranking(
            session,
            {
                "ticker": "UX-STALE",
                "ranked_at": ranked_at,
                "title": "Stale Bitcoin Price Market",
                "status": "open",
                "forecast_model": "crypto_v2",
                "forecast_probability": "0.80",
                "best_side": "BUY_YES",
                "best_price": "0.42",
                "estimated_edge": "0.38",
                "liquidity_score": "80",
                "spread_score": "90",
                "time_score": "70",
                "model_confidence_score": "90",
                "opportunity_score": "95",
                "spread": "0.02",
                "time_to_close_minutes": "120",
                "reason": "Seeded stale UX test opportunity.",
            },
        )
        session.commit()


def _seed_blocked_crypto_opportunity(session_factory) -> None:
    with session_factory() as session:
        captured_at = utc_now()
        insert_market_snapshot(
            session,
            {
                "ticker": "KXDOGE-26JUL1017-B0.082",
                "status": "open",
                "title": "Dogecoin price range on Jul 10, 2026?",
                "rules_primary": "This is a local UX test market.",
                "yes_bid_dollars": "0.40",
                "yes_ask_dollars": "0.41",
                "liquidity_dollars": "100",
            },
            {
                "orderbook_fp": {
                    "yes_dollars": [["0.40", "10"]],
                    "no_dollars": [["0.59", "10"]],
                }
            },
            captured_at,
        )
        insert_market_ranking(
            session,
            {
                "ticker": "KXDOGE-26JUL1017-B0.082",
                "ranked_at": captured_at,
                "title": "Dogecoin price range on Jul 10, 2026?",
                "status": "open",
                "forecast_model": "crypto_v2",
                "forecast_probability": "0.44",
                "best_side": "BUY_YES",
                "best_price": "0.41",
                "estimated_edge": "0.03",
                "liquidity_score": "80",
                "spread_score": "90",
                "time_score": "70",
                "model_confidence_score": "90",
                "opportunity_score": "9.414015548343460648148148148",
                "spread": "0.01",
                "time_to_close_minutes": "120",
                "reason": "Seeded blocked crypto UX opportunity.",
            },
        )
        session.commit()
