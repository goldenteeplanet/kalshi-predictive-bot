from pathlib import Path

from kalshi_predictor.ui.dashboard_deployment_preview import certify_dashboard_deployment_preview


def test_exact_dashboard_bundle_and_unit_pass():
    report = certify_dashboard_deployment_preview(
        Path.cwd(), Path("deploy/systemd/kalshi-ui.service.ui-obs5ia.preview")
    )
    assert report["status"] == "PASSED"
    assert report["failures"] == []
    assert report["exact_scope"]["legacy_dependency_removed"] is True
    assert report["guardrails"]["deployment_performed"] is False


def test_legacy_dependency_fails_visible(tmp_path: Path):
    source = Path("deploy/systemd/kalshi-ui.service.ui-obs5ia.preview").read_text()
    unit = tmp_path / "bad.service"
    unit.write_text(source.replace("After=network-online.target", "After=network-online.target kalshi-r5-watcher.service"))
    report = certify_dashboard_deployment_preview(Path.cwd(), unit)
    assert report["status"] == "FAILED"
    assert "LEGACY_WATCHER_DEPENDENCY_PRESENT" in report["failures"]
