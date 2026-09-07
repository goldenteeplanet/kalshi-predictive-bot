from datetime import timedelta
from pathlib import Path

from fastapi.testclient import TestClient
from typer.testing import CliRunner

from kalshi_predictor.cli import app
from kalshi_predictor.config import Settings, get_settings
from kalshi_predictor.data.db import get_session_factory, init_db
from kalshi_predictor.data.schema import ReadinessDecisionRecord, ReadinessReviewRecord
from kalshi_predictor.institutional_dashboard.service import build_dashboard_snapshot
from kalshi_predictor.live_readiness.catalog import CONTROL_CATALOG, catalog_summary
from kalshi_predictor.live_readiness.contracts import (
    DECISION_INCOMPLETE,
    DECISION_NO_GO,
    REASON_CANCEL_ONLY,
    REASON_CERTIFICATE_INVALID,
    REASON_CRITICAL_CONTROL_FAILED,
    REASON_MANDATORY_CONTROL_NOT_TESTED,
    REASON_REQUIRED_APPROVAL_MISSING,
    STATUS_FAIL,
    STATUS_PASS,
)
from kalshi_predictor.live_readiness.reports import generate_live_readiness_report
from kalshi_predictor.live_readiness.service import (
    evaluate_live_readiness,
    issue_live_readiness_certificate,
    verify_certificate_for_order,
)
from kalshi_predictor.ui.app import create_app
from kalshi_predictor.ui.routes import create_router
from kalshi_predictor.utils.time import utc_now


def test_phase_3v_catalog_has_expected_controls() -> None:
    summary = catalog_summary()

    assert len(CONTROL_CATALOG) == 114
    assert summary["control_count"] == 114
    assert summary["critical_count"] == 50


def test_phase_3v_default_review_is_incomplete_and_persists(tmp_path) -> None:
    session_factory = _session_factory(tmp_path)
    with session_factory() as session:
        result = evaluate_live_readiness(session, settings=_settings(), persist=True)
        session.commit()
        review_count = session.query(ReadinessReviewRecord).count()
        decision_count = session.query(ReadinessDecisionRecord).count()

    decision = result["review"]
    assert decision["decision"] == DECISION_INCOMPLETE
    assert decision["launch_envelope"] is None
    assert decision["certificate_ref"] is None
    assert REASON_MANDATORY_CONTROL_NOT_TESTED in decision["reason_codes"]
    assert review_count == 1
    assert decision_count == 1


def test_phase_3v_hard_veto_overrides_high_score(tmp_path) -> None:
    session_factory = _session_factory(tmp_path)
    overrides = {control.control_id: STATUS_PASS for control in CONTROL_CATALOG}
    overrides["REV-001"] = STATUS_FAIL
    with session_factory() as session:
        result = evaluate_live_readiness(
            session,
            settings=_settings(),
            control_status_overrides=overrides,
            approvals=[
                {"role": "owner", "status": "APPROVED"},
                {"role": "risk", "status": "APPROVED"},
                {"role": "operator", "status": "APPROVED"},
            ],
            persist=False,
        )

    decision = result["review"]
    assert decision["diagnostic_score"]["score"] > 95
    assert decision["decision"] == DECISION_NO_GO
    assert REASON_CRITICAL_CONTROL_FAILED in decision["reason_codes"]
    assert decision["launch_envelope"] is None


def test_phase_3v_approval_and_certificate_are_required(tmp_path) -> None:
    session_factory = _session_factory(tmp_path)
    overrides = {control.control_id: STATUS_PASS for control in CONTROL_CATALOG}
    with session_factory() as session:
        result = evaluate_live_readiness(
            session,
            settings=_settings(),
            control_status_overrides=overrides,
            persist=False,
        )

    decision = result["review"]
    assert decision["decision"] == DECISION_NO_GO
    assert REASON_REQUIRED_APPROVAL_MISSING in decision["reason_codes"]
    assert decision["certificate_ref"] is None


def test_phase_3v_certificate_guard_blocks_without_valid_certificate() -> None:
    result = verify_certificate_for_order(None, order_intent={"quantity": 1})

    assert result["allow_new_or_increasing_risk"] is False
    assert result["allow_cancel_only"] is True
    assert REASON_CERTIFICATE_INVALID in result["reason_codes"]
    assert REASON_CANCEL_ONLY in result["reason_codes"]


def test_phase_3v_certificate_guard_blocks_expired_and_oversized(tmp_path) -> None:
    session_factory = _session_factory(tmp_path)
    overrides = {control.control_id: STATUS_PASS for control in CONTROL_CATALOG}
    settings = _settings(phase_3v_certificate_issuance_enabled=True)
    with session_factory() as session:
        result = evaluate_live_readiness(
            session,
            settings=settings,
            control_status_overrides=overrides,
            approvals=[
                {"role": "owner", "status": "APPROVED"},
                {"role": "risk", "status": "APPROVED"},
                {"role": "operator", "status": "APPROVED"},
            ],
            persist=False,
        )
    certificate = issue_live_readiness_certificate(
        result["review"],
        issuer="local-owner",
        signature_reference="sig-local-test",
        valid_hours=1,
    )

    expired = verify_certificate_for_order(
        certificate,
        order_intent={"quantity": 1},
        now=utc_now() + timedelta(hours=2),
    )
    oversized = verify_certificate_for_order(certificate, order_intent={"quantity": 2})

    assert expired["allow_new_or_increasing_risk"] is False
    assert oversized["allow_new_or_increasing_risk"] is False
    assert oversized["allow_cancel_only"] is True


def test_phase_3v_report_ui_and_routes_are_read_only(tmp_path) -> None:
    session_factory = _session_factory(tmp_path)
    client = TestClient(create_app(session_factory=session_factory, settings=_settings()))

    page = client.get("/live-readiness")
    action = client.post("/api/live-readiness/review", json={"target_stage": "MICRO"})

    assert page.status_code == 200
    assert "Live Readiness Review" in page.text
    assert "paper-trade" not in page.text
    assert "demo-execute" not in page.text
    assert action.status_code == 200
    assert action.json()["ok"] is True
    assert action.json()["decision"]["decision"] == DECISION_INCOMPLETE

    router = create_router(session_factory=session_factory, settings=_settings())
    live_routes = [
        route
        for route in router.routes
        if getattr(route, "path", "").startswith("/api/live-readiness")
        or getattr(route, "path", "") == "/live-readiness"
    ]
    for route in live_routes:
        assert "paper-trade" not in route.path
        assert "demo-execute" not in route.path


def test_phase_3v_cli_report_and_dashboard_panel(tmp_path) -> None:
    session_factory = _session_factory(tmp_path)
    report = Path(tmp_path) / "live_readiness.md"
    with session_factory() as session:
        path = generate_live_readiness_report(
            session,
            output_path=report,
            json_output_path=Path(tmp_path) / "live_readiness.json",
            settings=_settings(),
        )
        snapshot = build_dashboard_snapshot(session, settings=_settings())

    assert path.exists()
    assert "Phase 3V Live Trading Readiness Review" in path.read_text(encoding="utf-8")
    assert snapshot["panels"]["live_readiness"]["read_only"] is True
    assert snapshot["panels"]["live_readiness"]["allow_order_create"] is False

    runner = CliRunner()
    for command in (
        "live-readiness-status",
        "live-readiness-review",
        "live-readiness-guard-check",
    ):
        result = runner.invoke(app, [command, "--help"])
        assert result.exit_code == 0
        assert "Usage" in result.output

    get_settings.cache_clear()
    output = Path(tmp_path) / "cli_live_readiness.md"
    result = runner.invoke(
        app,
        [
            "live-readiness-review",
            "--enable-review",
            "--output",
            str(output),
            "--json-output",
            str(Path(tmp_path) / "cli_live_readiness.json"),
        ],
        env={"DATABASE_URL": f"sqlite:///{Path(tmp_path) / 'cli.db'}"},
    )
    get_settings.cache_clear()

    assert result.exit_code == 0
    assert output.exists()
    assert "no live trading was enabled" in result.output


def _settings(**overrides) -> Settings:
    base = {
        "phase_3v_live_readiness_enabled": True,
        "phase_3v_mode": "offline_review",
        "phase_3v_default_target_stage": "MICRO",
        "phase_3t_institutional_dashboard_enabled": True,
        "phase_3t_mode": "read_only_shadow",
    }
    base.update(overrides)
    return Settings(**base)


def _session_factory(tmp_path):
    engine = init_db(f"sqlite:///{Path(tmp_path) / 'phase3v.db'}")
    return get_session_factory(engine)
