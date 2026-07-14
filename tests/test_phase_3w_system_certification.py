from pathlib import Path

from fastapi.testclient import TestClient
from typer.testing import CliRunner

from kalshi_predictor.cli import app
from kalshi_predictor.config import Settings, get_settings
from kalshi_predictor.data.db import get_session_factory, init_db
from kalshi_predictor.data.schema import SystemCertificationRun
from kalshi_predictor.institutional_dashboard.service import build_dashboard_snapshot
from kalshi_predictor.system_certification.contracts import (
    CONNECTIONS,
    PHASES,
    SYSTEM_INCOMPLETE,
)
from kalshi_predictor.system_certification.reports import generate_system_certification_report
from kalshi_predictor.system_certification.service import (
    SystemCertificationService,
    validate_certification_report_shape,
)
from kalshi_predictor.ui.app import create_app
from kalshi_predictor.ui.routes import create_router


def test_phase_3w_registry_covers_all_phases_and_edges() -> None:
    assert len(PHASES) == 29
    assert {phase["phase_id"] for phase in PHASES} == {
        "1",
        "2",
        "2.5",
        "2.6",
        "2.7",
        "2.8",
        "2.9",
        "3A",
        "3B",
        "3C",
        "3D",
        "3E",
        "3F",
        "3G",
        "3H",
        "3I",
        "3J",
        "3K",
        "3L",
        "3M",
        "3N",
        "3O",
        "3P",
        "3Q",
        "3R",
        "3S",
        "3T",
        "3U",
        "3V",
    }
    assert len(CONNECTIONS) == 57


def test_phase_3w_report_is_incomplete_and_never_authorizes_live(tmp_path) -> None:
    session_factory = _session_factory(tmp_path)
    with session_factory() as session:
        service = SystemCertificationService(session, settings=_settings(tmp_path))
        report = service.build_report()

    errors = validate_certification_report_shape(report)
    assert errors == []
    assert report["overall_status"] == SYSTEM_INCOMPLETE
    assert report["live_trading_authorized"] is False
    assert report["phase_3v_handoff"]["human_approval_required"] is True
    assert report["phase_3v_handoff"]["live_trading_authorized"] is False
    assert report["summary_counts"]["tests"]["not_run"] >= 57


def test_phase_3w_artifacts_and_persistence(tmp_path) -> None:
    session_factory = _session_factory(tmp_path)
    output_dir = Path(tmp_path) / "cert"
    with session_factory() as session:
        report = generate_system_certification_report(
            session,
            output_dir=output_dir,
            settings=_settings(tmp_path),
        )
        run_count = session.query(SystemCertificationRun).count()

    assert report["overall_status"] == SYSTEM_INCOMPLETE
    assert run_count == 1
    assert (output_dir / "repo_map.md").exists()
    assert (output_dir / "phase_capability_inventory.json").exists()
    assert (output_dir / "order_write_path_inventory.json").exists()
    assert (output_dir / "system_certification_report.json").exists()
    assert "THIS REPORT DOES NOT AUTHORIZE LIVE TRADING" in (
        output_dir / "system_certification_report.md"
    ).read_text(encoding="utf-8")


def test_phase_3w_order_write_path_inventory_is_read_only_audit(tmp_path) -> None:
    session_factory = _session_factory(tmp_path)
    with session_factory() as session:
        inventory = SystemCertificationService(
            session,
            settings=_settings(tmp_path),
        ).order_write_path_inventory()

    assert inventory
    assert any("paper" in row["path"] or "ui" in row["path"] for row in inventory)


def test_phase_3w_cli_smoke_and_run(tmp_path) -> None:
    runner = CliRunner()
    for command in (
        "system-certification-status",
        "system-certification-run",
        "system-certification-report",
    ):
        result = runner.invoke(app, [command, "--help"])
        assert result.exit_code == 0
        assert "Usage" in result.output

    get_settings.cache_clear()
    output_dir = Path(tmp_path) / "cli_cert"
    result = runner.invoke(
        app,
        [
            "system-certification-run",
            "--enable-audit",
            "--output-dir",
            str(output_dir),
        ],
        env={"DATABASE_URL": f"sqlite:///{Path(tmp_path) / 'cli.db'}"},
    )
    get_settings.cache_clear()

    assert result.exit_code == 0
    assert output_dir.joinpath("system_certification_report.md").exists()
    assert "Live trading authorized: false" in result.output


def test_phase_3w_ui_api_and_dashboard_panel(tmp_path) -> None:
    session_factory = _session_factory(tmp_path)
    settings = _settings(tmp_path)
    client = TestClient(create_app(session_factory=session_factory, settings=settings))

    page = client.get("/system-certification")
    action = client.post("/api/system-certification/run", json={})

    assert page.status_code == 200
    assert "System Certification" in page.text
    assert "demo-execute" not in page.text
    assert "paper-trade" not in page.text
    assert action.status_code == 200
    assert action.json()["ok"] is True
    assert action.json()["live_trading_authorized"] is False

    router = create_router(session_factory=session_factory, settings=settings)
    routes = [
        route
        for route in router.routes
        if getattr(route, "path", "").startswith("/api/system-certification")
        or getattr(route, "path", "") == "/system-certification"
    ]
    for route in routes:
        assert "demo-execute" not in route.path
        assert "paper-trade" not in route.path

    with session_factory() as session:
        snapshot = build_dashboard_snapshot(session, settings=settings)
    assert snapshot["panels"]["system_certification"]["allow_order_create"] is False
    assert snapshot["panels"]["system_certification"]["live_trading_authorized"] is False


def _settings(tmp_path) -> Settings:
    return Settings(
        phase_3w_system_certification_enabled=True,
        phase_3w_mode="AUDIT_ONLY",
        phase_3w_output_dir=str(Path(tmp_path) / "cert"),
        phase_3t_institutional_dashboard_enabled=True,
        phase_3t_mode="read_only_shadow",
    )


def _session_factory(tmp_path):
    engine = init_db(f"sqlite:///{Path(tmp_path) / 'phase3w.db'}")
    return get_session_factory(engine)

