from __future__ import annotations

import hashlib
import json
from pathlib import Path

from typer.testing import CliRunner

from kalshi_predictor.cli import app
from kalshi_predictor.config import get_settings
from kalshi_predictor.data.db import get_session_factory, init_db
from kalshi_predictor.phase3bb_r25_cloud_ui_operator_smoke_test import (
    LocalHttpProbe,
    LocalHttpResult,
    build_phase3bb_r25_cloud_ui_operator_smoke_test,
    write_phase3bb_r25_cloud_ui_operator_smoke_test_report,
)


def test_phase3bb_r25_smoke_passes_with_local_tunnel(tmp_path: Path) -> None:
    session_factory = _session_factory(tmp_path)
    reports_dir = tmp_path / "reports"
    _write_r24_context(reports_dir)

    with session_factory() as session:
        artifacts = write_phase3bb_r25_cloud_ui_operator_smoke_test_report(
            session,
            output_dir=reports_dir / "phase3bb_r25",
            reports_dir=reports_dir,
            local_base_url="http://127.0.0.1:8081",
            probe_runner=_fake_runner(),
        )

    payload = json.loads(artifacts.json_path.read_text(encoding="utf-8"))
    decision = payload["smoke_decision"]
    assert payload["phase"] == "3BB-R25-CLOUD-UI-OPERATOR-SMOKE-TEST"
    assert decision["status"] == "VERIFIED_CLOUD_UI_OPERATOR_SMOKE_PASS"
    assert decision["smoke_passed"] is True
    assert decision["routes_checked"] == 11
    assert decision["routes_http_200"] == 11
    assert payload["safety_flags"]["local_http_read_only_smoke"] is True
    assert payload["safety_flags"]["remote_commands_executed"] == 0
    assert artifacts.manifest_path.exists()


def test_phase3bb_r25_blocks_non_loopback_url(tmp_path: Path) -> None:
    session_factory = _session_factory(tmp_path)
    reports_dir = tmp_path / "reports"
    _write_r24_context(reports_dir)

    with session_factory() as session:
        payload = build_phase3bb_r25_cloud_ui_operator_smoke_test(
            session,
            output_dir=reports_dir / "phase3bb_r25",
            reports_dir=reports_dir,
            local_base_url="http://159.65.35.72:8080",
            probe_runner=_fake_runner(),
        )

    decision = payload["smoke_decision"]
    assert decision["status"] == "BLOCKED_CLOUD_UI_OPERATOR_SMOKE_TEST"
    assert decision["first_failed_check"] == "local_base_url_is_loopback"
    assert any(
        row["check"] == "local_base_url_is_loopback" and not row["passed"]
        for row in payload["smoke_checks"]
    )


def test_phase3bb_r25_reports_first_route_failure(tmp_path: Path) -> None:
    session_factory = _session_factory(tmp_path)
    reports_dir = tmp_path / "reports"
    _write_r24_context(reports_dir)

    with session_factory() as session:
        payload = build_phase3bb_r25_cloud_ui_operator_smoke_test(
            session,
            output_dir=reports_dir / "phase3bb_r25",
            reports_dir=reports_dir,
            local_base_url="http://127.0.0.1:8081",
            probe_runner=_fake_runner(fail_path="/opportunities"),
        )

    decision = payload["smoke_decision"]
    assert decision["status"] == "BLOCKED_CLOUD_UI_OPERATOR_SMOKE_TEST"
    assert decision["first_failed_check"] == "route_opportunities"
    assert decision["failed_route_count"] == 1


def test_phase3bb_r25_cli_help_registered() -> None:
    result = CliRunner().invoke(
        app,
        ["phase3bb-r25-cloud-ui-operator-smoke-test", "--help"],
    )

    assert result.exit_code == 0
    assert "phase3bb-r25-cloud-ui-operator-smoke-test" in result.output
    assert "--local-base-url" in result.output


def _session_factory(tmp_path: Path):
    get_settings.cache_clear()
    engine = init_db(f"sqlite:///{tmp_path / 'phase3bb_r25.db'}")
    return get_session_factory(engine)


def _write_r24_context(reports_dir: Path) -> None:
    r24_dir = reports_dir / "phase3bb_r24"
    r24_dir.mkdir(parents=True, exist_ok=True)
    (r24_dir / "cloud_ui_start_tunnel_verification.json").write_text(
        json.dumps(
            {
                "verification_decision": {
                    "status": "VERIFIED_UI_RUNNING_SSH_TUNNEL_READY",
                    "verification_passed": True,
                    "r5_pid": 28862,
                    "ui_service_started": True,
                    "local_ui_http_ok": True,
                    "public_http_listening": False,
                    "public_https_listening": False,
                }
            }
        ),
        encoding="utf-8",
    )


def _fake_runner(*, fail_path: str | None = None):
    def runner(probe: LocalHttpProbe, base_url: str) -> LocalHttpResult:
        url = base_url.rstrip("/") + probe.path
        if probe.path == fail_path:
            return _result(probe, url, "<h1>Internal Server Error</h1>", 500, "text/html")
        if probe.kind == "json":
            body = json.dumps(
                {
                    "ok": True,
                    "read_only": True,
                    "request_id": "req-test",
                    "dashboard_snapshot_id": "snap-test",
                    "guard": {"status": "PASS"},
                    "data": {"status": "ok"},
                }
            )
            return _result(probe, url, body, 200, "application/json")
        body = f"<!doctype html><html><body>Kalshi Opportunities Markets System Coverage Portfolio Model Settings {probe.name}</body></html>"
        return _result(probe, url, body, 200, "text/html; charset=utf-8")

    return runner


def _result(
    probe: LocalHttpProbe,
    url: str,
    body: str,
    status_code: int,
    content_type: str,
) -> LocalHttpResult:
    encoded = body.encode("utf-8")
    return LocalHttpResult(
        name=probe.name,
        method=probe.method,
        path=probe.path,
        url=url,
        ok=200 <= status_code < 400,
        status_code=status_code,
        content_type=content_type,
        duration_seconds=0.01,
        final_url=url,
        body_sha256=hashlib.sha256(encoded).hexdigest(),
        body_excerpt=body,
        error="" if status_code < 400 else "HTTP Error",
    )
